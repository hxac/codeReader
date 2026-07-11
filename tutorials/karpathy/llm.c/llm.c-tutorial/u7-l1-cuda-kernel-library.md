# dev/cuda 内核库：每层多版本与性能对比

## 1. 本讲目标

本讲带读者进入 llm.c 的「CUDA 实验场」——`dev/cuda/` 目录。这里和主线 `train_gpt2.cu`、`llmc/` 的定位完全不同：主线追求「能跑、够快、可读」，而 `dev/cuda/` 追求「把同一个算子写出 N 个版本，逐一对比谁更快、为什么快」。读懂这个目录，就等于读懂了 llm.c 里那些「拍脑袋想不出」的 GPU 优化技巧是怎么被一步步试出来的。

学完本讲你应该能够：

- 说清 `dev/cuda/` 作为「逐步优化的内核教学库」的设计哲学，以及它与主线代码的关系。
- 以 `layernorm_forward.cu` 的 kernel1~6 为标本，复述一个朴素 CUDA kernel 是如何一步步演进到 cooperative groups、方差巧算、共享内存 + 128 位向量化访存的。
- 理解「方差巧算」\(\mathrm{var}(x)=\mathbb{E}[x^2]-\mathbb{E}[x]^2\) 为什么能把两遍扫描压成一遍，以及它的数值代价。
- 看懂每个 `.cu` 文件里那套统一的「先 CPU 参考、再逐 block_size 校验、最后计时 + napkin math」的测试脚手架，并能独立跑出一个 kernel 的性能对比。

## 2. 前置知识

在进入本讲前，请确认你已掌握以下概念（它们都来自前置讲义）：

- **CUDA 三层执行模型**：thread / warp（32 个线程为一组）/ block / grid。一个 kernel 由若干 grid 启动，每个 grid 含若干 block，每个 block 含若干 thread。这是 u4-l3 与 u5-l4 的基础。
- **warp 级归约与 `__shfl_xor_sync`**：在 warp 内用 5 步二叉树把 32 个线程的局部和加起来，这是几乎所有 GPU 归约的公共地基（见 u5-l4 的 `warpReduceSum`）。
- **LayerNorm 的数学定义**：对每个位置 \((b,t)\) 的 \(C\) 维向量做「去均值、除标准差、再缩放平移」，并缓存 `mean` 与 `rstd` 供反向使用（见 u2-l2）。
- **`Packed128` / `x128` 向量化访存**：用 128 位（16 字节）一次性 load/store 多个元素，强迫编译器生成 `LDG.128`/`STS.128` 指令以提升显存带宽利用率（见 u5-l4）。
- **cuBLAS / cuBLASLt**：NVIDIA 提供的高度优化的矩阵乘库，手写 GEMM kernel 最终往往比不过它（见 u5-l3）。

本讲会反复用到的一个心智模型：**访存带宽是 GPU 上最稀缺的资源**。对 LayerNorm 这种「算术强度低」（每个元素只做几次乘加）的算子，性能瓶颈几乎总是「把数据从显存搬进搬出」，而不是「算得不够快」。所以本讲里 kernel1→6 的演进主线，本质就是在「减少对全局显存的重复读写」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [dev/cuda/README.md](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/README.md) | 整个目录的用法说明：怎么编译、怎么跑、kernel 版本号约定 |
| [dev/cuda/layernorm_forward.cu](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/layernorm_forward.cu) | 本讲主角：LayerNorm 前向的 6 个版本 + CPU 参考 + 统一 main |
| [dev/cuda/common.h](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/common.h) | 所有 `.cu` 共享的脚手架：`cudaCheck`、`warpReduceSum`、`Packed128`、`validate_result`、`benchmark_kernel` |
| [dev/cuda/matmul_forward.cu](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/matmul_forward.cu) | 对照样本：matmul 的「朴素 kernel → cuBLAS → cuBLASLt」三版演进，napkin math 用 TFLOPS |
| [dev/cuda/attention_forward.cu](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/attention_forward.cu) | 对照样本：注意力的「朴素 → 朴素 flash → cuBLAS+softmax → online softmax → cuDNN」演进 |
| [dev/cuda/Makefile](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/Makefile) | 把所有 `make xxx` 命令集中起来的构建文件，含 `%: %.cu` 通用规则 |

整本讲义以 `layernorm_forward.cu` 为主线精读，用 `matmul_forward.cu`、`attention_forward.cu` 作为「同样的多版本思路也适用于其它算子」的旁证。

## 4. 核心概念与源码讲解

### 4.1 多版本内核演进：dev/cuda 的设计哲学与 layernorm 六版

#### 4.1.1 概念说明

`dev/cuda/` 的定位在 [README.md](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/README.md) 开篇一句话就讲清楚了：

> This directory is scratch space for developing various versions of the needed CUDA kernels. Each file develops a kernel, and usually multiple versions of that kernel that could have different running times and of different code or time complexity.

关键词是 **scratch space（草稿区）** 和 **multiple versions（多版本）**。这里的每个 `.cu` 文件对应模型里的一个算子（layernorm、matmul、attention、gelu、encoder、adamw、softmax……），但每个文件里**同一个算子通常有 3 到 6 个实现版本**，版本号越大通常越快、也越复杂。

这种「一个算子写 N 遍」的做派，和主线 `train_gpt2.cu` / `llmc/` 截然不同——主线每个算子只保留「最终胜出的那一个」。`dev/cuda/` 之所以保留全部败者，是因为它的使命是**教学与选型**：让读者亲眼看到「朴素写法慢在哪、每一步优化带来了多少收益、最终版本快了几个数量级」。

这套目录的产物最后如何反哺主线？README 最后一句点明了工作流：

> The typical process from here on is we copy paste the kernel that ran fastest, adjust it manually (e.g. to hardcode the best block size) and drop it into the training code file, e.g. `train_gpt2.cu`.

也就是说：在 `dev/cuda/` 里跑遍所有版本、扫描出最快的 block_size，然后把那个 kernel 抠出来、把最佳 block_size 写死，粘进主线。**`dev/cuda/` 是主线 kernel 的「选型试验田」**。

#### 4.1.2 核心流程

每个 `.cu` 文件都遵循同一套「五段式」结构，这是理解整个目录的钥匙：

1. **文件头注释**：列出全部版本，给出每个版本的编译命令和 `./xxx N` 运行命令，并用一句话标注每个版本的优化点。
2. **CPU 参考实现**（如 `layernorm_forward_cpu`）：朴素的多重循环，作为正确性「标准答案」。
3. **若干 GPU kernel**（`layernorm_forward_kernel1` ... `kernel6`）：版本递进。
4. **kernel 启动器 + 版本分发**（`layernorm_forward1` ... `layernorm_forward6` + `layernorm_forward` 的 `switch`）：用一个命令行参数 `kernel_num` 选择跑哪个版本。
5. **统一的 `main`**：先跑 CPU 参考取标准答案，再对所有候选 `block_size` 校验 GPU 结果，最后对所有 `block_size` 计时并打印 napkin math。

`layernorm_forward.cu` 顶部注释把六个版本的定位说得很清楚：

[dev/cuda/layernorm_forward.cu:L1-L22](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/layernorm_forward.cu#L1-L22) —— 这里中文说明：v1 是 CPU 代码到 kernel 的朴素平移（按 B、T 并行，循环扫 C）；v2 在整个 B、T、C 上并行；v3 用 cooperative groups 在 B、T、C 上并行；v4 用「\(\mathrm{var}(x)=\mathrm{mean}(x^2)-\mathrm{mean}(x)^2\)」的方差巧算，只需对 x 做一遍扫描；v5 改成「每个 block 处理一行」而非「每个 warp 处理一行」，其余与 v4 相同。

下面这张表是六版演进的地图，本讲 4.2 节会展开「方差巧算」这条最关键的优化线：

| 版本 | 并行粒度 | 统计量算法 | 关键优化 | 备注 |
|------|----------|------------|----------|------|
| kernel1 | 一线程处理一行 (b,t) | 两遍扫 x（mean 一遍、var 一遍） | 无，朴素平移 | 最慢，但最易懂 |
| kernel2 | mean/rstd 一个 block 一行；normalize 全展开 | 拆成三个独立 kernel | 拆分归一化 | 中间过渡 |
| kernel3 | 一个 warp 处理一行 | 两遍扫 x | cooperative groups 归约 | 引入 cg |
| kernel4 | 一个 warp 处理一行 | **一遍扫 x**（方差巧算） | 单遍 mean+var | 关键提速 |
| kernel5 | 一个 block 处理一行 | 一遍扫 x | 跨 warp 归约（共享内存） | 大通道时占优 |
| kernel6 | 一个 warp 一行 + 共享内存 | 一遍扫 x | 预载 weight/bias 到 smem、x128 向量化、x 只读一次 | 最快，主线候选 |

#### 4.1.3 源码精读

先看 CPU 参考实现，它是所有 GPU 版本的正确性基准：

[dev/cuda/layernorm_forward.cu:L35-L70](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/layernorm_forward.cu#L35-L70) —— 这里中文说明：对每个位置 (b,t)，先用一个 `for i` 累加求均值 \(m\)，再用第二个 `for i` 累加 \((x_i-m)^2\) 求方差 \(v\)，算出 `rstd`，再用第三个 `for i` 做归一化与缩放平移写出 `out`，并把 `mean`、`rstd` 缓存供反向使用。注意 CPU 版本对 x **读了三遍**（一遍 mean、一遍 var、一遍 normalize）。

再看最朴素的 kernel1，它几乎是 CPU 版本的逐行平移：

[dev/cuda/layernorm_forward.cu:L76-L111](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/layernorm_forward.cu#L76-L111) —— 这里中文说明：每个线程负责「一行」(idx 从 0 到 N=B*T)，线程内部用三个串行 `for i` 循环分别算 mean、var、normalize。问题在于：每个线程都要把同一行 x 从全局显存读三遍，而它独自工作时其余 31 个同 warp 线程各读各的行，**带宽利用率极低、且没有任何线程协作**。

版本分发用一个朴素的 `switch` 把命令行参数路由到对应启动器：

[dev/cuda/layernorm_forward.cu:L505-L533](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/layernorm_forward.cu#L505-L533) —— 这里中文说明：`layernorm_forward(kernel_num, ...)` 按 `kernel_num` 分发到 `layernorm_forward1` ~ `layernorm_forward6`，非法值则报错退出。这种「单函数分发」是每个 `.cu` 文件的标准入口，让 `./layernorm_forward 4` 这种命令行选版本成为可能。

构建层面，[dev/cuda/Makefile](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/Makefile) 用一条通用规则 `%: %.cu`（L29-L30）把所有 `.cu` 一键编译成同名可执行文件，所以 `make layernorm_forward` 等价于文件头注释里的那条 `nvcc` 命令。TARGETS 列表（L33）枚举了全部算子，`make all` 一次编译所有，`make run_all` 一次跑完所有。

#### 4.1.4 代码实践

**实践目标**：建立「目录—主线」的对应关系，理解 `dev/cuda/` 的产物最终去了哪。

**操作步骤**：

1. 在仓库根目录执行 `ls dev/cuda/*.cu`，列出全部 22 个 kernel 文件。
2. 打开 [dev/cuda/Makefile:L33](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/Makefile#L33)，把 TARGETS 里的算子与主线 `llmc/` 头文件一一对应（例如 `layernorm_forward.cu` ↔ `llmc/layernorm.cuh`、`attention_forward.cu` ↔ `llmc/attention.cuh`、`matmul_forward.cu` ↔ `llmc/matmul.cuh`）。
3. 选一个算子（例如 layernorm），打开 `dev/cuda/layernorm_forward.cu` 顶部注释，对照 `llmc/layernorm.cuh`，找出主线最终采纳的是哪个版本（提示：主线 layernorm kernel 与本讲的 kernel4/kernel5 思路一致——一遍扫描 + warp 归约）。

**需要观察的现象**：你会发现 `dev/cuda/` 里那些「慢版本」(kernel1/2) 在主线里完全不存在——主线只保留了胜出者。

**预期结果**：能画出一张「dev/cuda 算子 ↔ llmc 头文件」的对照表，并说出主线 layernorm 采纳了「方差巧算 + warp 归约」这一脉。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `dev/cuda/` 要保留 kernel1 这种「明显很慢」的版本，而不直接删掉？

> **参考答案**：因为它是教学与调试的基线。kernel1 是 CPU 逻辑最直接的平移，最容易读懂、也最容易和 CPU 参考对齐验证正确性；后续优化版本一旦出错，可以退回 kernel1 这种「白盒」实现排查。删掉它就失去了「优化从哪里出发」的参照系。

**练习 2**：`make layernorm_forward` 背后实际执行了什么命令？为什么 matmul_forward 需要额外的 `-Xcompiler -fopenmp`？

> **参考答案**：执行 `nvcc -O3 --use_fast_math --generate-code ... -lcublas -lcublasLt -std=c++17 layernorm_forward.cu -o layernorm_forward`（通用规则 `%: %.cu`，见 [Makefile:L29-L30](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/Makefile#L29-L30)）。matmul 的 CPU 参考 `matmul_forward_cpu` 用了 `#pragma omp parallel for`（见 matmul_forward.cu L35），CPU 端矩阵乘太慢会拖累校验环节，所以要开 OpenMP 多核加速 CPU 参考。

### 4.2 方差巧算与 cooperative groups：从两遍扫描到单遍归约

#### 4.2.1 概念说明

这是本讲技术含量最高的一节，也是 kernel1→kernel6 演进里**收益最大的一步**（kernel3→kernel4）。它回答一个问题：求一行的均值和方差，必须把这一行数据从显存读两遍吗？

先回顾 LayerNorm 需要的两个统计量（与 CPU 参考一致）：

\[
\mu = \frac{1}{C}\sum_{i=0}^{C-1} x_i, \qquad
\sigma^2 = \frac{1}{C}\sum_{i=0}^{C-1}(x_i-\mu)^2
\]

按这个定义，必须**先有 \(\mu\) 才能算 \(\sigma^2\)**，所以朴素实现要两遍扫描：第一遍累加 \(x_i\) 得 \(\mu\)，第二遍累加 \((x_i-\mu)^2\) 得 \(\sigma^2\)。kernel1 和 kernel3 都是这么做的。

但方差有一个等价表达式（概率论里的「平方的期望减期望的平方」）：

\[
\sigma^2 = \mathbb{E}[x^2] - (\mathbb{E}[x])^2 = \frac{1}{C}\sum_{i}x_i^2 - \left(\frac{1}{C}\sum_{i}x_i\right)^2
\]

这个式子的妙处在于：\(\sum x_i\) 和 \(\sum x_i^2\) **互不依赖**，可以在**同一次遍历**里一起累加！于是读一遍 x 就能同时拿到 mean 和 var。这就是 [layernorm_forward.cu 顶部注释 L16-L18](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/layernorm_forward.cu#L16-L18) 所说的「方差巧算，允许我们对 x 只做一遍 load」。

**数值代价**（必须知道）：\(\mathbb{E}[x^2]-\mathbb{E}[x]^2\) 在数学上等于方差，但在浮点下当均值较大时会出现**灾难性抵消**（两个相近的大数相减丢精度）。对 LayerNorm 而言输入已被前层限制在合理量级，这点误差可接受；但这是一个真实的取舍，不是免费午餐。

另一个核心概念是 **cooperative groups（协作组，简称 cg）**：CUDA 自带的一套「对线程组分组的抽象 API」。本讲用到的核心是：

- `cg::this_thread_block()` 拿到当前 block。
- `cg::tiled_partition<32>(block)` 把 block 切成 32 线程一组的 warp（tile）。
- `cg::reduce(warp, val, cg::plus<float>{})` 一行代码完成 warp 内归约，等价于手写的 `warpReduceSum`，但更可读、编译器更易优化。

#### 4.2.2 核心流程

**kernel3（两遍扫描 + cooperative groups）** 的执行流程：

1. 每个 warp 负责一行：`idx = blockIdx.x * warp.meta_group_size() + warp.meta_group_rank()`，即一个 block 里塞多个 warp，每个 warp 处理不同行。
2. **第一遍**：warp 内线程合作，用 stride 循环把整行 x 累加成 `sum`，再用 `cg::reduce` 归约到标量，除以 C 得均值 \(m\)。
3. **第二遍**：再读一遍 x，累加 \((x_i-m)^2\)，`cg::reduce` 归约得方差，算 rstd。
4. 第三段：每个线程用 stride 循环写出归一化结果。

**kernel4（一遍扫描，方差巧算）** 的流程：

1. 同样每个 warp 一行。
2. **一遍**：stride 循环里同时累加 `sum += x[i]` 和 `sum2 += x[i]*x[i]`。
3. 两个 `cg::reduce`：`sum` 归约成 \(\sum x\)、`sum2` 归约成 \(\sum x^2\)。
4. `var = sum2/C - (sum/C)*(sum/C)`，即 \(\mathbb{E}[x^2]-\mathbb{E}[x]^2\)，一步出 var，再 `rsqrtf` 得 rstd。
5. 写出归一化结果。

用伪代码对比两版对 x 的访问次数：

```
kernel3:  读 x 一遍(求 sum) → 读 x 第二遍(求 sum of (x-m)^2)   // x 读 2 次
kernel4:  读 x 一遍(同时累加 sum 和 sum2)                       // x 读 1 次
```

省下的一遍显存读取，就是 kernel4 相对 kernel3 的主要提速来源——对一个访存受限的算子，这往往是 30%~50% 量级的收益。

#### 4.2.3 源码精读

先看 kernel3 的「两遍扫描 + cooperative groups 归约」：

[dev/cuda/layernorm_forward.cu:L181-L228](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/layernorm_forward.cu#L181-L228) —— 这里中文说明：用 `cg::tiled_partition<32>` 取出当前 warp，`idx` 由「block 内的 warp 序号」算出本 warp 负责的行；随后**两个独立的 for 循环**——第一个求 `sum` 经 `cg::reduce` 得 mean，第二个再读一遍 x 求 \(\sum(x_i-m)^2\) 经 `cg::reduce` 得 rstd；最后写出归一化。注意 `__ldcs` / `__stcs` 是「流式缓存提示」，告诉硬件这些数据不会很快复用、可直接穿过 cache，从而把 cache 留给共享的 weight/bias。

再看 kernel4，注意它如何把两次归约合并到一次遍历：

[dev/cuda/layernorm_forward.cu:L231-L278](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/layernorm_forward.cu#L231-L278) —— 这里中文说明：单个 `for` 循环里同时 `sum += xi` 与 `sum2 += xi*xi`，循环结束后分别 `cg::reduce` 出 \(\sum x\) 与 \(\sum x^2\)；然后 `var = sum2/C - sum*sum/C/C`（即 \(\mathbb{E}[x^2]-\mathbb{E}[x]^2\)），一步出方差。对比 kernel3，x 从全局显存**只被读了一遍**——这就是「single pass over x」的全部秘密。

> 关键对比：kernel3 的 L198-L213 有两个分别累加 `sum` 与 \((x_i-m)^2\) 的循环，kernel4 的 L248-L255 只有一个同时累加 `sum` 与 `sum2` 的循环。两个循环体对 x 的访问是同样的 stride 模式，但 kernel4 把「两遍」压成了「一遍」。

`cg::reduce` 底层等价于手写的 `warpReduceSum`（见 [common.h:L16-L21](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/common.h#L16-L21) 的 `__shfl_xor_sync` 五步二叉归约），只是封装成了更易读的 API。

补充一点：kernel5（[L281-L337](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/layernorm_forward.cu#L281-L337)）与 kernel4 算法完全相同（都用方差巧算、都一遍扫描），区别在于**协作粒度从 warp 升级到整个 block**：用 `shared_sum[32]` / `shared_sum2[32]` 共享内存把 block 内各 warp 的局部和再归约一次。当通道数 C 很大、一个 warp 的线程不够覆盖时，block 级归约能摊薄每个线程的工作量。

最复杂的 kernel6（[L340-L413](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/layernorm_forward.cu#L340-L413)）则把优化推到极致：启动前用 `cudaFuncSetAttribute` 申请超过 48 KiB 的动态共享内存（L492），把 weight/bias 预载进 `s_weight`/`s_bias`、把输入缓存进 `s_in`（这样归一化写出阶段不必再从显存读 x），全程用 `load128cs` / `store128cs`（128 位向量化，见 common.h 的 `Packed128`）。若申请大共享内存失败，则回退到 kernel5（L496-L500）——这是「能跑」优先于「最快」的工程兜底。

#### 4.2.4 代码实践

**实践目标**：亲手验证「方差巧算」与「两遍扫描」在数值上等价，并理解为什么一遍扫描更快。

**操作步骤**：

1. 在纸上取一个长度 \(C=4\) 的小向量，例如 \(x = [1, 2, 3, 4]\)。
2. 用**两遍法定义**算：\(\mu = (1+2+3+4)/4 = 2.5\)，\(\sigma^2 = ((1-2.5)^2+(2-2.5)^2+(3-2.5)^2+(4-2.5)^2)/4 = 1.25\)。
3. 用**方差巧算**算：\(\mathbb{E}[x^2] = (1+4+9+16)/4 = 7.5\)，\(\mathbb{E}[x]^2 = 6.25\)，\(\sigma^2 = 7.5 - 6.25 = 1.25\)。
4. 确认两者结果一致。
5. 打开 [kernel3（L198-L213）](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/layernorm_forward.cu#L198-L213)与 [kernel4（L248-L255）](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/layernorm_forward.cu#L248-L255)，数一数每个版本里出现 `x[i]` 或 `x+c` 这类对输入的全局显存读取共有几轮循环。

**需要观察的现象**：两遍法与方差巧算在 \(C=4\) 上完全相等；kernel3 有两段「读 x 的 for」，kernel4 只有一段。

**预期结果**：手算两种方法都得到 \(\sigma^2=1.25\)；源码层面确认 kernel3 读 x 两遍、kernel4 读 x 一遍。

**注**：若想观察数值差异，可构造均值很大的向量（如 \(x=[1000,1001,1002,1003]\)），在 float 下两种方法结果可能出现极小的尾数不同——这就是「灾难性抵消」的影子，也是 kernel4 用更宽类型累加（实际代码用 float，依赖输入量级可控）的原因。具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：kernel3 和 kernel4 都用「一个 warp 处理一行」，算法主体几乎一样，为什么 kernel4 更快？

> **参考答案**：kernel3 算方差需要先有均值，所以分两个循环：先读 x 求 mean，再读一遍 x 求 \(\sum(x_i-m)^2\)，x 从显存读两遍。kernel4 用 \(\mathrm{var}=\mathbb{E}[x^2]-\mathbb{E}[x]^2\)，在一个循环里同时累加 \(\sum x\) 和 \(\sum x^2\)，x 只读一遍。对一个访存受限的算子，省下一半的显存读取就是主要提速来源。

**练习 2**：为什么 kernel5 要在 kernel4 的基础上改成「一个 block 一行」？

> **参考答案**：一个 warp 只有 32 个线程，当通道数 C 很大（如 768、1600）时，每个线程要用 stride 循环串行处理 C/32 个元素，内层串行段较长。改成「一个 block 一行」后（block 可达 1024 线程），每个线程分担的元素数下降，并行度更高；代价是需要用共享内存把 block 内多个 warp 的局部和再归约一次（`shared_sum[32]`）。

**练习 3**：方差巧算 \(\mathrm{var}=\mathbb{E}[x^2]-\mathbb{E}[x]^2\) 在什么情况下会出数值问题？

> **参考答案**：当数据均值 \(\mu\) 的绝对值远大于标准差时，\(\mathbb{E}[x^2]\) 和 \(\mathbb{E}[x]^2\) 是两个相近的大数，相减会发生「灾难性抵消」丢失有效位。LayerNorm 的输入一般量级可控，这点误差可接受；但若直接用在数值范围很大的场景（如未归一化的原始 embedding），应谨慎。

### 4.3 benchmark 与正确性比对：统一的测试脚手架与 napkin math

#### 4.3.1 概念说明

`dev/cuda/` 的第二个核心价值，是它给每个 kernel 都配了一套**完全统一的测试与计时脚手架**，全部实现在 [common.h](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/common.h) 里。理解了这套脚手架，你就能读懂任何一个 `.cu` 文件的 `main` 函数，也能照葫芦画瓢给自己新写的 kernel 加上同样的对比。

脚手架有三件套：

1. **`validate_result`**：正确性比对。把 GPU 结果拷回 host，逐元素与 CPU 参考比对，超容差即报错退出。容差是「绝对容差 + 相对容差」的组合：\(t_{\mathrm{eff}} = \text{tolerance} + |x_{\mathrm{cpu}}|\cdot\epsilon\)，其中 \(\epsilon\) 是浮点精度（fp32 用 `FLT_EPSILON`，BF16 放宽到 0.079）。
2. **`benchmark_kernel`**：计时。关键技巧是**每次计时前先 flush L2 cache**——分配一块和 L2 一样大的 buffer，用 `cudaMemset` 把它写满，把上一次 kernel 的数据从 L2 里挤出去，避免「数据正好在 cache 里」带来的虚高。然后用 `cudaEvent` 精确计时，重复 `repeats` 次取平均。
3. **napkin math（餐巾纸估算）**：把计时结果换算成「硬件利用率」——访存受限的算子换算成显存带宽（GB/s），计算受限的算子换算成算力（TFLOPS）。这让「0.05 ms」这种孤立数字变成「达到 A100 标称带宽的 80%」这种有意义的判断。

#### 4.3.2 核心流程

每个 `.cu` 文件的 `main` 都遵循同一个流程（以 [layernorm_forward.cu 的 main](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/layernorm_forward.cu#L537-L629) 为模板）：

```
1. srand(0);                          // 确定性随机数，保证可复现
2. 设定 B, T, C（如 B=8, T=1024, C=768）
3. malloc + 随机初始化 host 数组
4. cudaMalloc + cudaMemcpy 把数据搬上 GPU
5. 读命令行 kernel_num（./layernorm_forward 4 → kernel_num=4）
6. layernorm_forward_cpu(...)          // 跑 CPU 参考，得到「标准答案」
7. for 每个 block_size in {32,64,128,256,512,1024}:
       layernorm_forward(kernel_num, ...)      // 跑一次 GPU kernel
       validate_result(d_out, out_cpu, ...)    // 逐元素比对
       validate_result(d_mean, mean_cpu, ...)
       validate_result(d_rstd, rstd_cpu, ...)
   // 全部通过才打印 "All results match. Starting benchmarks."
8. for 每个 block_size:
       elapsed = benchmark_kernel(2000, layernorm_forward, kernel_num, ..., block_size)
       bandwidth = 2*B*T*C*4 / elapsed          // napkin math: 显存带宽
       printf("block_size %4d | time ... | bandwidth ... GB/s")
```

这套流程的核心思想是 **block size 扫描（sweep）**：同一个 kernel 在不同 `block_size` 下性能差异可能很大，所以要把候选值都跑一遍，挑出最优配置。这正是 README 所说的「runs a number of configurations of this kernel (most often and most notably the block size), to time the kernel in these launch configurations」。

不同算子的 napkin math 指标不同，反映了它们的瓶颈性质：

- **layernorm（访存受限）**：用显存带宽 GB/s（[L606-L611](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/layernorm_forward.cu#L606-L611)），分子是读写总量 `2*B*T*C*4` 字节。
- **matmul（计算受限）**：用算力 TFLOPS（[matmul_forward.cu L421-L424](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/matmul_forward.cu#L421-L424)），分子是 `B*T*C*OC*2` 次浮点运算。
- **matmul 还多一层**：它的「block size」是二维的 `sqrt_block_size`（一个 block 是 `sqrt_block_size × sqrt_block_size` 的线程网格），扫描值是 `{4,8,16,32}`（[L402](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/matmul_forward.cu#L402)）。

#### 4.3.3 源码精读

先看正确性比对 `validate_result`，注意它的「相对 + 绝对」组合容差：

[dev/cuda/common.h:L310-L349](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/common.h#L310-L349) —— 这里中文说明：把 GPU 结果 D2H 拷回，逐元素与 CPU 参考比较；有效容差是 `tolerance + |cpu_ref|*epsilon`（既给绝对下限、又按数值大小放大），跳过非有限值（如被 mask 成 -inf 的位置），累计到 10 个不匹配或结尾有任意不匹配就 `exit(EXIT_FAILURE)`。调用方传的 `tolerance` 因算子而异：layernorm 用 `1e-5f`（[L590-L592](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/layernorm_forward.cu#L590-L592)），matmul 因 TF32 放宽到 `1e-1f`（[matmul L408](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/matmul_forward.cu#L408)）。

再看计时函数 `benchmark_kernel`，关键是 **L2 cache flush**：

[dev/cuda/common.h:L351-L385](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/common.h#L351-L385) —— 这里中文说明：先分配一块与设备 `l2CacheSize` 等大的 buffer，每次计时前用 `cudaMemset` 把它写满，把上一个 kernel 残留在 L2 的数据挤掉（否则连续跑同一个 kernel 会因数据命中 cache 而虚高）；然后用 `cudaEventRecord` 在 kernel 前后打点、`cudaEventSynchronize` 等完成、`cudaEventElapsedTime` 算单次耗时，重复 `repeats` 次取平均返回。这个模板函数接收任意 kernel 及其参数，所以每个算子都能复用。

最后看 layernorm main 里的 napkin math：

[dev/cuda/layernorm_forward.cu:L606-L611](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/layernorm_forward.cu#L606-L611) —— 这里中文说明：估算实现的显存带宽。分子 `2*B*T*C*4` 是「读入 inp + 写出 out」的字节数（`*4` 是 float 字节数，未计入 weight/bias 的小头），除以耗时得到 GB/s。注释里写了 A100 40GB PCIe 标称 1555 GB/s，读者可据此判断 kernel 离带宽上限还有多远——这是「我的 kernel 是不是已经访存受限、还能不能更快」的最直接判据。

对照 matmul 的 napkin math，它用算力而非带宽，因为 matmul 是计算受限的：

[dev/cuda/matmul_forward.cu:L421-L424](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/matmul_forward.cu#L421-L424) —— 这里中文说明：分子 `B*T*C*OC*2` 是矩阵乘的浮点运算次数（每个输出元素做 C 次乘加 = 2C 次浮点运算），除以耗时换算成 TFLOPS。注释里给了 A100 的 19.5 TFLOPS fp32 标称值作参照。kernel1（朴素手写）的 TFLOPS 会很低，kernel2/kernel3（cuBLAS/cuBLASLt）会逼近标称值——这就是「手写 GEMM 干不过库」的量化体现。

#### 4.3.4 代码实践

**实践目标**：亲手跑一遍「编译 → 校验 → 扫描 block_size → 计时 → napkin math」全流程，对比 kernel1 与 kernel4 的性能差异。

**操作步骤**：

1. 进入目录：`cd dev/cuda`。
2. 编译（二选一）：
   - 直接用 make：`make layernorm_forward`
   - 或照文件头注释手敲：`nvcc -O3 --use_fast_math -lcublas -lcublasLt layernorm_forward.cu -o layernorm_forward`
3. 跑朴素版本：`./layernorm_forward 1`，记录输出里每个 block_size 对应的 `time` 和 `bandwidth`。
4. 跑优化版本：`./layernorm_forward 4`，同样记录每个 block_size 的 `time` 和 `bandwidth`。
5. （可选）再跑 `./layernorm_forward 5` 和 `./layernorm_forward 6`，看 block-per-row 与共享内存版本是否更快。
6. 在表格里对比 kernel1 与 kernel4 在同一个 block_size（如 256）下的耗时与带宽。

**需要观察的现象**：

- 每个 kernel 都会先打印前 5 个元素的「CPU 参考值 vs GPU 值」并显示几近相等，随后打印 `All results match. Starting benchmarks.`（正确性校验通过）。
- benchmark 表里会列出 6 个 block_size 各自的耗时与带宽。kernel4 的耗时应明显小于 kernel1，带宽更接近标称值。

**预期结果**：在典型 GPU（如 A100/4090）上，kernel4 的耗时应显著低于 kernel1，且 kernel4 的实测带宽会比 kernel1 更逼近硬件标称值。**具体数字待本地验证**（取决于你的 GPU 型号与 CUDA 版本）。

**用一句话总结 kernel4 快在哪**：kernel4 用方差巧算 \(\mathrm{var}=\mathbb{E}[x^2]-\mathbb{E}[x]^2\) 把对输入 x 的显存读取从两遍压成一遍，对访存受限的 LayerNorm 直接省下一半带宽，所以快。

> **无 GPU 怎么办**：README 指出可以在 [Modal](http://modal.com) 上跑基准，例如（见 [README.md:L35-L39](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/README.md#L35-L39)）：`GPU_MEM=80 modal run benchmark_on_modal.py --compile-command "..." --run-command "./layernorm_forward 4"`。

#### 4.3.5 小练习与答案

**练习 1**：`benchmark_kernel` 为什么在每次计时前都要 `cudaMemset` 一块和 L2 一样大的 buffer？

> **参考答案**：为了 flush（冲刷）L2 cache。连续重复跑同一个 kernel 时，上一次的数据可能还留在 L2 里，下一次命中 cache 会快得不真实。先用一块与 L2 等大的 memset 把 cache 挤干净，再计时，才能测出 kernel 真实从显存读数据的性能。

**练习 2**：layernorm 的 napkin math 用「显存带宽 GB/s」，matmul 却用「算力 TFLOPS」，为什么指标不同？

> **参考答案**：两者的瓶颈性质不同。LayerNorm 算术强度低（每个元素只做几次乘加），瓶颈是把数据搬进搬出显存，所以用带宽衡量、与硬件标称带宽比。matmul 算术强度高（每个数据复用很多次做乘加），瓶颈是算力，所以用 TFLOPS 衡量、与硬件标称算力比。选对指标才能判断「我的 kernel 还有多少优化空间」。

**练习 3**：为什么 `validate_result` 的有效容差是 `tolerance + |cpu_ref|*epsilon`，而不是一个固定值？

> **参考答案**：因为浮点误差与数值大小成正比——数值越大，最低有效位代表的绝对值越大，允许的绝对误差也应越大。`|cpu_ref|*epsilon` 提供「按数值缩放」的相对容差部分，加上一个固定下限 `tolerance`（防止 cpu_ref 接近 0 时容差过严）。这种组合容差既不误杀大数值的正常误差，也不放过小数值的真实错误。

## 5. 综合实践

把本讲三节的知识串起来，完成一次「完整的 kernel 选型实验」：

**任务**：为 LayerNorm 前向选出「本机最快的版本 + 最佳 block_size」，并解释为什么是它。

**步骤**：

1. 在 `dev/cuda/` 下 `make layernorm_forward`。
2. 依次运行 `./layernorm_forward 1`、`./layernorm_forward 3`、`./layernorm_forward 4`、`./layernorm_forward 5`、`./layernorm_forward 6`，把每个版本每个 block_size 的 `time` 和 `bandwidth` 填进一张表。
3. 找出全局最小的耗时，记录它对应的版本号与 block_size。
4. 算出该配置的实测带宽占你 GPU 标称带宽的百分比（标称值可查 `cudaGetDeviceProperties` 或厂商规格，注释里给 A100 40GB PCIe 是 1555 GB/s）。
5. 写一段话解释演进链条：kernel1 为什么慢（单线程扫一行、读 x 三遍）→ kernel3 引入了什么（cooperative groups、warp 合作）→ kernel4 为什么是关键提速（方差巧算、x 只读一遍）→ kernel5/kernel6 在此基础上又做了什么（block 级归约、共享内存预载 weight/bias、x128 向量化）。
6. （进阶）打开 `llmc/layernorm.cuh`，对比主线最终采纳的 kernel 与你选出的最快版本，看作者是否做了额外调整（如写死 block_size、改用 `Packed128`、加 `__ldcs` 提示）。

**验收标准**：你能用一句话说出「最快的是 kernelX、block_size=Y，因为它达到了标称带宽的 Z%」，并能复述从 kernel1 到它的每一步优化动机。这正是 README 描述的「跑出最快 kernel → 微调 → 粘进主线」的真实工作流。

> **注意**：本实践需要 NVIDIA GPU 与 CUDA 工具链。若无 GPU，可改做「源码阅读型实践」：只完成步骤 5 的纯阅读分析，用源码注释和本讲内容论证每个版本的优化点；性能数字标注「待本地验证」。

## 6. 本讲小结

- `dev/cuda/` 是 llm.c 的 **kernel 选型试验田**：每个算子一个 `.cu` 文件，每个文件含同一算子的多个版本，版本号越大通常越快也越复杂；跑出最快的那个会被抠出来粘进主线 `train_gpt2.cu` / `llmc/`。
- 每个 `.cu` 文件遵循**统一的五段式结构**：文件头版本说明 → CPU 参考 → 若干 GPU kernel → 启动器+版本分发 → 统一 main（校验+扫描+计时+napkin math）。
- LayerNorm 六版演进的主线是**减少对输入 x 的重复显存读取**：kernel1 读三遍、kernel3 读两遍、kernel4 用方差巧算 \(\mathrm{var}=\mathbb{E}[x^2]-\mathbb{E}[x]^2\) 压成一遍、kernel6 用共享内存缓存 x 与 weight/bias 并做 128 位向量化访存。
- **cooperative groups（cg）** 提供了比手写 `__shfl_xor_sync` 更可读的归约抽象（`cg::reduce`），是 kernel3/4/5 的公共基础；kernel5 把协作粒度从 warp 升级到 block（用共享内存归约各 warp 的局部和）。
- **方差巧算有数值代价**：\(\mathbb{E}[x^2]-\mathbb{E}[x]^2\) 在均值大时会有灾难性抵消，LayerNorm 输入量级可控故可接受，这是一个真实的「速度 vs 精度」取舍。
- **统一的测试脚手架**（`validate_result` + `benchmark_kernel` + napkin math）让任何 kernel 都能被正确性校验、按 block_size 扫描计时、并换算成带宽/算力利用率；访存受限算子用 GB/s、计算受限算子用 TFLOPS，选对指标才能判断优化空间。

## 7. 下一步学习建议

本讲授完了 `dev/cuda/` 的设计哲学与以 layernorm 为标本的多版本演进。建议接下来：

- **横向迁移到其它算子**：用本讲学到的方法独立阅读 [dev/cuda/attention_forward.cu](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/attention_forward.cu)（朴素 → 朴素 flash → cuBLAS+softmax → online softmax → cuDNN，共 6+ 版）与 [dev/cuda/matmul_forward.cu](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/matmul_forward.cu)（朴素 → cuBLAS → cuBLASLt），体会「online softmax」「调库 vs 手写」这两种截然不同的优化路线。这部分与 u5-l5（Attention CUDA）紧密呼应。
- **顺藤摸瓜到主线**：对照 [llmc/layernorm.cuh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/layernorm.cuh)，看作者最终把哪个版本、哪个 block_size 写死进了训练代码——这是 u5-l4 的内容，可在那里验证你的理解。
- **掌握更高级的内核优化**：kernel6 里出现的 `Packed128` 向量化、`__ldcs`/`__stcs` 缓存提示、动态共享内存申请（`cudaFuncSetAttribute`），以及 attention 里的 online softmax，是后续学习「手写高性能 CUDA」的基础。若想深入，可专门研究 [dev/cuda/fused_residual_forward.cu](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/fused_residual_forward.cu)（kernel6 注释里说自己的灵感就来自这里的 kernel5）。
