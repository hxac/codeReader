# 目录组织约定

## 1. 本讲目标

上一讲我们通过 README 建立了对 tilelang-benchmark 的全局观：它是按 GPU 架构组织、用 TileLang 与 cuBLAS/Triton 等多框架对比的算子性能基准套件。本讲我们要走进仓库内部，搞清楚**这些基准测试在磁盘上是怎么摆放的**。

学完本讲，你应当能够：

- 说出仓库的三层目录层次：架构目录 → 算子目录 → 编号框架子目录。
- 给定一个「架构 × 算子 × 框架」的组合，能快速定位到对应的内核文件。
- 看懂 `benchmark.sh` 编排脚本如何按编号顺序依次驱动各框架，以及 `data/`、`pdf/`、`png/` 在可视化管线中的角色。
- 识别并理解仓库中存在的**命名不一致**现象（`2.tilelang` vs `3.tilelang`、`-benchmark` vs `_benchmark`），避免被它误导。

## 2. 前置知识

- **算子（operator）**：GPU 上一个具体的计算原语，例如矩阵乘（GEMM）、矩阵-向量乘（GEMV）、FlashAttention。本仓库的每一个基准测试都是围绕一个算子展开的。
- **框架（framework / provider）**：实现这个算子的「某一套代码」。例如同一个 fp16 GEMM，可以用 cuBLAS、Triton、BitBLAS、TileLang 各写一份，再比谁快。本仓库把每一份实现称为一个 provider。
- **基线（baseline）**：作为对照标的成熟实现。cuBLAS、Triton 通常被当作「标尺」，TileLang 是被衡量的对象。
- **GPU 架构（microarchitecture）**：NVIDIA 的 Ada（RTX 4090, sm_89）、Ampere（A100, sm_80）、Hopper（H100, sm_90）；AMD 的 CDNA（MI300X, gfx942）。本仓库顶层目录就是按这些架构族划分的。

> 提示：本讲只读不改，所有「源码」其实是目录结构与少量编排脚本。不要被「源码精读」这个词吓到——它指的是「精读这些约定本身」。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
|---|---|
| `README.md` | 提到参与测试的 GPU 型号，是架构命名的权威出处。 |
| `hopper_benchmark/` 等四个顶层架构目录 | 第一层：按 GPU 架构族划分。 |
| `hopper_benchmark/dense_matmul/` 等算子目录 | 第二层：按算子划分。 |
| `hopper_benchmark/dense_matmul/3.tilelang-benchmark/` 等编号子目录 | 第三层：按框架编号划分，每份是一个 provider。 |
| [hopper_benchmark/dense_matmul/benchmark.sh](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/benchmark.sh#L1-L13) | 编排脚本：按编号顺序依次跑各框架。 |
| [hopper_benchmark/dense_matmul/plot.sh](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/plot.sh#L1-L7) | 可视化编排脚本：依次调用各 `plot_*.py`。 |
| [hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L1-L12) | 一个「住在编号子目录里」的 TileLang 内核驱动示例。 |

## 4. 核心概念与源码讲解

### 4.1 架构目录

#### 4.1.1 概念说明

仓库最外层用四个目录把所有基准测试按 **GPU 架构族** 切开。为什么先按架构分？因为同一个算子内核在不同架构上的张量核心（TensorCore / MFMA）、共享内存大小、指令集都不一样，性能数字无法跨架构直接比较，所以最自然的「第一刀」就是架构。

四个架构目录分别是：

| 目录 | GPU 架构族 | 代表显卡 | 厂商 |
|---|---|---|---|
| `ada_benchmark/` | Ada Lovelace (sm_89) | RTX 4090 | NVIDIA |
| `ampere_benchmark/` | Ampere (sm_80) | A100 | NVIDIA |
| `hopper_benchmark/` | Hopper (sm_90) | H100 | NVIDIA |
| `cdna_benchmark/` | CDNA (gfx942) | MI300X | AMD |

注意命名约定：**所有架构目录都以 `_benchmark` 结尾**，这一点在四个目录之间是统一的。

README 也把这套架构清单写在了性能图标题里：

[README.md:14](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/README.md#L14) ——「Matmul Performance on GPUs (RTX 4090, A100, H100, MI300X)」，正好对应 ada / ampere / hopper / cdna 四个目录。

#### 4.1.2 核心流程

进入仓库后的定位流程：

```
我的目标 GPU 是什么？  →  进入对应的 <arch>_benchmark/ 目录
   RTX 4090              →  ada_benchmark/
   A100                  →  ampere_benchmark/
   H100                  →  hopper_benchmark/
   MI300X                →  cdna_benchmark/
```

#### 4.1.3 源码精读

四个架构目录是仓库根目录下仅有的、带 `_benchmark` 后缀的顶层目录。它们的存在本身就是约定的一部分——你只要在仓库根目录看到 `*_benchmark/`，就知道那是「某一类 GPU 上的全部基准测试」。

需要留意的是：**算子并不是在每个架构目录下都齐全**。例如 `dense_matmul`（稠密矩阵乘）只出现在 NVIDIA 的三个目录下，而 `conv_benchmark`（卷积）、`mla_benchmark`（MLA 注意力）目前只出现在 `cdna_benchmark/` 下。这反映的是「哪个算子在哪种卡上更值得对比」的工程取舍，而不是遗漏。跨架构的算子分布问题我们会在第 7 单元专门讨论。

#### 4.1.4 代码实践

1. **实践目标**：用一条命令看清四个架构目录，并把它们和具体 GPU 对应起来。
2. **操作步骤**：在仓库根目录执行 `ls -d *_benchmark/`。
3. **需要观察的现象**：应输出 `ada_benchmark/  ampere_benchmark/  cdna_benchmark/  hopper_benchmark/` 四项。
4. **预期结果**：四个目录全部以 `_benchmark` 结尾，命名一致；与 README 性能图标题里的四款 GPU 一一对应。
5. 若实际输出多出或少于四项，请以本地实际目录为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习**：为什么仓库要先按架构切目录，而不是先按算子切？

**参考答案**：因为不同 GPU 架构的张量核心与指令集差异巨大，同一算子在不同架构上的性能不可直接比较；先按架构分能保证「同一目录下的所有数字都来自同一种卡」，对比才公平。此外，每个架构目录下收录的算子集合也不同（如 conv、MLA 仅在 cdna），按架构分也方便按卡维护各自适用的算子。

---

### 4.2 算子目录

#### 4.2.1 概念说明

进入某个架构目录后，第二层是**算子目录**，每个目录对应一个被测试的算子。例如 `hopper_benchmark/dense_matmul/` 就是「H100 上的稠密矩阵乘基准」，`hopper_benchmark/flashattention/` 就是「H100 上的 FlashAttention 基准」。

这里有一个容易踩坑的**命名不一致**：

- 大多数算子目录**没有**后缀：`dense_matmul`、`dequantize_matmul`、`flashattention`、`blocksparse_attention`、`dequant_matmul`、`lowprecision_matmul`、`contiguous_dequant_matmul`、`deepgemm`。
- 但 `cdna_benchmark/` 下的部分算子目录**带 `_benchmark` 后缀**：`conv_benchmark`、`gemm_benchmark`、`mha_benchmark`、`mla_benchmark`。

也就是说，「算子目录到底叫 `xxx` 还是 `xxx_benchmark`」并不统一。定位文件时不要假设某一种写法，最好用 tab 补全或 `ls` 确认。

#### 4.2.2 核心流程

定位某个算子的流程：

```
<arch>_benchmark/  →  找到 <operator> 或 <operator>_benchmark/ 目录
                       （命名不统一，先 ls 一下最稳妥）
```

#### 4.2.3 源码精读

以 `hopper_benchmark/` 为例，它下面挂着 5 个算子目录，全部不带 `_benchmark` 后缀：

```
hopper_benchmark/
├── blocksparse_attention/
├── deepgemm/
├── dense_matmul/
├── dequantize_matmul/
└── flashattention/
```

而 `cdna_benchmark/` 下的算子目录则混用了两种写法（这是历史上不同贡献者按各自习惯命名的结果）：

```
cdna_benchmark/
├── blocksparse_attention      （无后缀）
├── conv_benchmark             （带 _benchmark 后缀）
├── dequantize_matmul          （无后缀）
├── gemm_benchmark             （带 _benchmark 后缀）
├── mha_benchmark              （带 _benchmark 后缀）
└── mla_benchmark              （带 _benchmark 后缀）
```

一个算子目录内部通常长这样（以 `dense_matmul` 为例）：若干编号框架子目录 + 编排脚本 + 数据与出图目录：

```
hopper_benchmark/dense_matmul/
├── 0.cublas-benchmark/
├── 1.triton-benchmark/
├── 2.bitblas-benchmark/
├── 3.tilelang-benchmark/
├── benchmark.sh          ← 跑全部框架的编排脚本
├── plot.sh               ← 出图编排脚本
├── plot_operator_figures_*.py
├── data/                 ← 从日志解析出的数据
├── pdf/                  ← 输出的 PDF 图
└── png/                  ← 输出的 PNG 图
```

编号框架子目录是下一节的主角。

#### 4.2.4 代码实践

1. **实践目标**：任选一个算子目录，确认它的内部布局。
2. **操作步骤**：执行 `ls -1 hopper_benchmark/flashattention/`。
3. **需要观察的现象**：应看到三个编号子目录 `0.torch_benchmark`、`1.tilelang_benchmark`、`2.triton_benchmark`，但**没有** `benchmark.sh` 或 `data/`（说明并非每个算子目录都自带完整可视化管线）。
4. **预期结果**：体会到「不同算子目录的完整程度不同」——有的只有内核，有的带完整编排与出图。
5. 待本地验证：你可以再多 `ls` 几个算子目录，对比哪些带 `data/`、哪些不带。

#### 4.2.5 小练习与答案

**练习 1**：在 `cdna_benchmark/` 下，哪几个算子目录带 `_benchmark` 后缀，哪几个不带？

**参考答案**：带后缀的有 `conv_benchmark`、`gemm_benchmark`、`mha_benchmark`、`mla_benchmark`；不带的有 `blocksparse_attention`、`dequantize_matmul`。

**练习 2**：如果要找「H100 上的 FlashAttention 的 TileLang 实现」，应该进入哪个算子目录？

**参考答案**：进入 `hopper_benchmark/flashattention/`（注意是无后缀写法），再在里面找 TileLang 的编号子目录（下一节讲）。

---

### 4.3 编号框架子目录

#### 4.3.1 概念说明

第三层是本仓库最核心、也最容易让人困惑的约定：**每个算子目录下，按框架拆成若干个编号子目录**，形如 `N.<framework>-benchmark` 或 `N.<framework>_benchmark`。这一层把「同一个算子的不同实现」并列摆在一起，方便对比。

拆解一个编号子目录的名字 `3.tilelang-benchmark`：

| 片段 | 含义 |
|---|---|
| `3` | 编号，决定该框架在 `benchmark.sh` 里的**运行顺序**（从 0 开始）。 |
| `tilelang` | 框架名，说明这份实现用 TileLang 写。 |
| `-benchmark` 或 `_benchmark` | 固定后缀（但分隔符不统一，见下）。 |

常见的框架名有：`cublas`、`torch`、`triton`、`bitblas`、`tilelang`、`marlin`、`cutlass`、`bitsandbytes`、`deepgemm`、`ck`、`ladder`、`aiter`、`fa3`。

#### 4.3.2 核心流程

```
<operator>/  →  ls 看到若干 N.<framework>{-,_}benchmark/
                →  编号 N 决定运行先后（0 通常是最稳的参考基线）
                →  framework 名决定这是哪份实现
                →  进子目录，里面是一个自包含的 .py 内核 + .sh 驱动
```

#### 4.3.3 源码精读

这里有**三处真实的命名不一致**，是本讲最重要的提醒。

**不一致一：分隔符（连字符 vs 下划线）**。同样是「框架 + benchmark 后缀」，有的用连字符，有的用下划线。最夸张的是 `hopper_benchmark/dequantize_matmul/` 里**同一个算子目录下混用两种**：

```
hopper_benchmark/dequantize_matmul/
├── 0.cublas-benchmark       （连字符）
├── 1.triton-benchmark       （连字符）
├── 3.tilelang-benchmark     （连字符）
├── 4.bitblas_benchmark      （下划线！）
└── 5.marlin-benchmark       （连字符）
```

而 `hopper_benchmark/flashattention/` 三个子目录**全部用下划线**：`0.torch_benchmark`、`1.tilelang_benchmark`、`2.triton_benchmark`。

**不一致二：TileLang 的编号不固定**。TileLang 不总是同一个编号——在 flashattention 里它是 `1.tilelang_benchmark`（下划线），在 dense_matmul 里它是 `3.tilelang-benchmark`（连字符）。我们直接看两个真实文件路径就能体会到这种差异：

- [hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L1-L12) —— 编号 1、下划线。
- [hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L1-L1) —— 编号 3、连字符。

所以**不要假设 TileLang 一定在某个固定编号下**，定位时一定要 `ls`。

**不一致三：编号会跳号**。上面的 `dequantize_matmul` 编号是 `0,1,3,4,5`，**跳过了 2**——这通常是因为某个基线被删掉后编号没重新排齐。看到跳号不必惊讶。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看一遍同一个 TileLang 在不同算子目录里的编号与分隔符差异。
2. **操作步骤**：
   - `ls -d hopper_benchmark/flashattention/*tilelang*`
   - `ls -d hopper_benchmark/dense_matmul/*tilelang*`
   - `ls -d hopper_benchmark/dequantize_matmul/*tilelang*`
3. **需要观察的现象**：三条命令分别应输出 `1.tilelang_benchmark`、`3.tilelang-benchmark`、`3.tilelang-benchmark`。
4. **预期结果**：确认 TileLang 编号可能是 1 也可能是 3，分隔符可能是 `_` 也可能是 `-`。
5. 待本地验证：以本地实际目录名为准。

#### 4.3.5 小练习与答案

**练习 1**：编号 `0.` 通常是什么角色？

**参考答案**：通常是「最权威的参考基线」，比如 dense_matmul 里的 `0.cublas-benchmark`、flashattention 里的 `0.torch_benchmark`。把它放在最前，意味着先确立标尺，再让别的实现去对标。

**练习 2**：在 `hopper_benchmark/dequantize_matmul/` 下，编号 2 去哪了？

**参考答案**：被跳过了（目录里只有 0,1,3,4,5）。这通常是删除某个基线后未重排编号留下的历史痕迹，属于仓库的命名不一致现象之一。

---

### 4.4 benchmark.sh 编排

#### 4.4.1 概念说明

每个算子目录（如果完整的话）会带一个 `benchmark.sh`，它是**编排脚本**：按照编号顺序，依次进入每个框架子目录、调用该框架的 `.sh` 驱动、再退出来。换句话说，它把「分别跑 cuBLAS / Triton / BitBLAS / TileLang」串成一条流水线。与之配套的还有 `plot.sh`（出图编排）和 `data/`、`pdf/`、`png/` 三个目录（分别存放解析出的数据、PDF 图、PNG 图）。

#### 4.4.2 核心流程

```
benchmark.sh 的典型结构：
  cd 0.<baseline>-benchmark ; ./<驱动>.sh ; cd ..
  cd 1.<framework>-benchmark ; ./<驱动>.sh ; cd ..
  cd 2.<framework>-benchmark ; ./<驱动>.sh ; cd ..
  ...（顺序 = 编号顺序）

可视化侧：
  data/*.py   从各框架日志里解析 latency → 内存中的数据结构
  plot.sh     依次调用 plot_*.py
  pdf/、png/  生成的图表产物
```

#### 4.4.3 源码精读

`hopper_benchmark/dense_matmul/benchmark.sh` 是最典型的编排脚本，全文只有 13 行，就是三段「进入 → 运行 → 退出」：

[hopper_benchmark/dense_matmul/benchmark.sh:3-13](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/benchmark.sh#L3-L13) —— 这是它的核心，三段 `cd`/`./xxx.sh`/`cd ..` 对应三个框架。

注意第 11 行：

[hopper_benchmark/dense_matmul/benchmark.sh:11](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/benchmark.sh#L11) —— 写的是 `cd 2.tilelang-benchmark`，随后执行 `./benchmark_bitblas_matmul.sh`。

这里藏着一个**真实的命名不一致缺陷**：dense_matmul 目录下**根本没有 `2.tilelang-benchmark` 这个目录**，实际的目录是 `2.bitblas-benchmark`（BitBLAS）和 `3.tilelang-benchmark`（TileLang）。也就是说：

- 脚本第 11 行想进入的目录名（`2.tilelang-benchmark`）不存在；
- 它紧接着要执行的脚本名（`benchmark_bitblas_matmul.sh`）其实属于 BitBLAS。

这段脚本要么是早期目录改组后忘了同步、要么是手误。它生动地示范了本讲反复强调的「命名不一致」会带来什么后果——**直接照着脚本名找目录会被误导**。学到这里，你应当明白为什么前面反复强调「定位时一定要 `ls` 确认实际目录名」。

可视化侧的编排由 `plot.sh` 完成，结构同样简单：先建好 `pdf/`、`png/` 两个输出目录，再依次调用四个出图脚本：

[hopper_benchmark/dense_matmul/plot.sh:1-7](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/plot.sh#L1-L7) —— 先 `mkdir -p pdf png`，再依次跑 `plot_operator_figures_*.py`。

而 `data/` 目录里放的是 `data_*.py`（例如 `data_float16_gemm.py`、`data_int8_gemm.py`），它们用正则从各框架产生的日志里解析出 latency，组织成 `(provider, [times])` 这样的数据结构，供 `plot_*.py` 画图。这条「日志 → data/ → plot_*.py → pdf/png」的管线，我们会在第 2 单元（u2-l7）专门拆解。

#### 4.4.4 代码实践

1. **实践目标**：跟踪 `dense_matmul/benchmark.sh` 的三步调用，并发现其中的命名不一致。
2. **操作步骤**：
   - 打开 `hopper_benchmark/dense_matmul/benchmark.sh`，逐行读出每个 `cd` 的目标目录与随后执行的 `.sh`。
   - 用 `ls -1 hopper_benchmark/dense_matmul/` 对照，检查脚本里出现的目录名是否都真实存在。
3. **需要观察的现象**：第一段进入 `0.cublas-benchmark` 跑 `compile_and_run.sh`；第二段进入 `1.triton-benchmark` 跑 `benchmark_float16.sh`；第三段声称进入 `2.tilelang-benchmark`、却跑 `benchmark_bitblas_matmul.sh`。对照 `ls` 结果，`2.tilelang-benchmark` 并不存在，真实存在的是 `2.bitblas-benchmark` 和 `3.tilelang-benchmark`。
4. **预期结果**：亲手发现这个命名不一致缺陷，并理解它的成因（目录改组后脚本未同步）。
5. 待本地验证：本实践只做「读脚本 + ls 对照」，不实际运行；若你尝试运行 `benchmark.sh`，预计会在第三段因 `cd` 到不存在的目录而表现异常（具体行为以本地 shell 为准）。

#### 4.4.5 小练习与答案

**练习 1**：`benchmark.sh` 里三段调用的运行顺序由什么决定？

**参考答案**：由框架子目录的**编号**（`0.`、`1.`、`2.`…）决定。编号小的先跑。这也是为什么参考基线（如 cuBLAS）通常编号为 0。

**练习 2**：如果要修复 `dense_matmul/benchmark.sh` 第 11 行的命名不一致，最小改动是什么？

**参考答案**：把 `cd 2.tilelang-benchmark` 改成 `cd 2.bitblas-benchmark`（与它随后执行的 `./benchmark_bitblas_matmul.sh` 以及实际目录名保持一致）。本讲只读不改，实际修复请在确认意图后另开任务进行。

---

## 5. 综合实践

**任务**：任选一个算子目录（推荐 `hopper_benchmark/dequantize_matmul/`），画出它从架构目录到各框架的完整目录树，并完成以下标注。

要求：

1. 从仓库根目录开始，画出 `<arch>_benchmark/ → <operator>/ → N.<framework>{-,_}benchmark/` 的三层树。
2. 在每个叶子（编号子目录）旁标注：它是 **TileLang** 还是 **基线**（cuBLAS/Triton/BitBLAS/Marlin…）。
3. 标出该算子目录里出现的命名不一致：哪些子目录用连字符、哪些用下划线？TileLang 的编号是几？有没有跳号？
4. 检查该算子目录是否有 `benchmark.sh`；若有，读一遍，看其中引用的目录名是否都与实际 `ls` 结果一致（仿照 4.4 的方法）。

**示例产出（以 dequantize_matmul 为例，可用 `tree` 或手画）**：

```
hopper_benchmark/                         ← 第一层：架构目录
└── dequantize_matmul/                    ← 第二层：算子目录（无 _benchmark 后缀）
    ├── 0.cublas-benchmark/      [基线]   ← 连字符
    ├── 1.triton-benchmark/      [基线]   ← 连字符
    ├── 3.tilelang-benchmark/    [TileLang] ← 连字符，编号 3，跳过了 2
    ├── 4.bitblas_benchmark/     [基线]   ← 下划线（与上面混用！）
    └── 5.marlin-benchmark/      [基线]   ← 连字符
```

通过这个练习，你会对仓库「架构 → 算子 → 编号框架」的三层约定形成肌肉记忆，并对命名不一致保持警觉。

## 6. 本讲小结

- 仓库目录分三层：**架构目录**（`*_benchmark/`）→ **算子目录**（`<operator>` 或 `<operator>_benchmark`）→ **编号框架子目录**（`N.<framework>{-,_}benchmark`）。
- 架构目录命名统一（都带 `_benchmark` 后缀），分别对应 ada / ampere / hopper（NVIDIA）与 cdna（AMD）。
- 算子目录命名不统一：部分带 `_benchmark` 后缀（多见于 cdna），定位时先 `ls`。
- 编号框架子目录的编号决定运行顺序（`0.` 通常是参考基线），但**分隔符（`-` vs `_`）**、**TileLang 的编号（1 或 3）**、**是否跳号**都不固定。
- `benchmark.sh` 按编号顺序串联各框架；`data/`、`plot.sh`、`pdf/`、`png/` 共同构成「日志 → 数据 → 图表」的可视化管线。
- 仓库存在真实的命名不一致缺陷（如 `dense_matmul/benchmark.sh` 引用了不存在的 `2.tilelang-benchmark`），阅读时以实际 `ls` 为准，不要盲信脚本字面量。

## 7. 下一步学习建议

- 下一讲 **u1-l3 运行一次基准测试** 会以 `dense_matmul` 为例，真正跟踪 `benchmark.sh` 的每一步实际命令、所需环境与依赖，以及 cuBLAS 用 CMake 编译、Python 基线用 shell 调用的差异。本讲发现的「`2.tilelang-benchmark` 路径缺陷」在下一讲运行时还会再次碰到，值得记住。
- 想提前了解可视化管线细节，可以跳到 **u2-l7 数据提取与可视化**，看 `data/*.py` 如何用正则从日志里抽出 latency。
- 想直接看 TileLang 内核长什么样，可以在学完 u1-l3 后进入第 3 单元，从 **u3-l8 TileLang 内核骨架** 开始。
