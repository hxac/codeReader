# 第 1 讲：项目定位与整体概览

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标是帮你建立一个「全局观」。读完本讲后，你应该能够：

- 用一两句话说清楚 `tilelang-benchmark` 这个仓库到底是做什么的。
- 知道项目当前的维护状态——已归档，并已迁移到 TileOps。
- 列出仓库包含的四大 GPU 架构目录，以及每个架构下都跑了哪些算子。
- 读懂 `README.md` 里的三张性能对比图，以及 V / M / FA / CC / CT 五类 shape（形状）配置表分别代表什么测试场景。

本讲只依赖 `README.md` 一个文件，不涉及任何代码运行，重点在于「读懂项目入口文档」。

## 2. 前置知识

本讲面向零基础读者，但有几个名词最好先有个直觉：

- **GPU（图形处理器）**：一种拥有大量并行计算核心的芯片，特别擅长做大量相似的计算，比如矩阵乘法。本仓库基准测试的对象就是 GPU。
- **算子（operator / kernel）**：可以理解为「一段在 GPU 上跑的、完成某个数学运算的程序」。矩阵乘（GEMM）、注意力（Attention）都是算子。
- **GEMM（矩阵乘法）**：计算 \( C = A \times B \)，其中 A、B、C 都是矩阵。这是深度学习里最核心、也最吃性能的运算之一。
- **TFLOPS（每秒万亿次浮点运算）**：衡量 GPU 算力的单位。1 TFLOPS = 每秒 \(10^{12}\) 次浮点运算。数值越大越快。
- **shape（形状）**：一个张量（tensor）的尺寸，比如「4096 行 × 8192 列」。基准测试需要事先规定好用哪些尺寸来跑。
- **Git 仓库 / Markdown**：本项目是一个 Git 仓库，入口文档 `README.md` 用 Markdown 语法写成。看懂基本的标题、表格、图片语法即可。

如果上面有名词还不熟，不用担心，本讲会在用到时再用大白话解释一遍。

## 3. 本讲源码地图

本讲唯一需要精读的源码文件是：

| 文件 | 作用 |
|------|------|
| `README.md` | 项目入口文档，包含项目定位、性能汇总图、shape 配置表，以及归档提示与 TileOps 迁移链接。 |

此外，为了让你对项目有个整体印象，本讲会**引用仓库的顶层目录结构**作为背景知识（这些目录是真实存在的，但本讲不深入它们，后续讲义才会逐个剖析）：

```
tilelang-benchmark/
├── README.md                  ← 本讲精读对象
├── ada_benchmark/             ← NVIDIA Ada 架构（如 RTX 4090）的基准
├── ampere_benchmark/          ← NVIDIA Ampere 架构（如 A100）的基准
├── hopper_benchmark/          ← NVIDIA Hopper 架构（如 H100）的基准
├── cdna_benchmark/            ← AMD CDNA 架构（如 MI300X）的基准
└── images/                    ← README 引用的性能对比图
```

可以看到，仓库用「四大 GPU 架构目录」来组织所有内容——这是本项目最核心的组织方式，后面所有讲义都围绕它展开。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：

1. README 性能汇总（Benchmark Summary）
2. shape 配置表 V / M / FA / CC / CT
3. 归档提示与 TileOps 链接

### 4.1 README 性能汇总（Benchmark Summary）

#### 4.1.1 概念说明

打开任何一个开源性能项目的 `README.md`，最先看到的往往是一段「性能汇总（Benchmark Summary）」。它的作用是：**用最直观的方式告诉你——本项目要测的东西，结果怎么样。**

在 `tilelang-benchmark` 里，这段汇总回答的核心问题是：

> 用 **TileLang**（一种专门写 GPU 算子的领域专用语言，DSL）写出来的算子，和业界已有的成熟方案（比如 NVIDIA 官方的 **cuBLAS**、OpenAI 的 **Triton**、清华的 **BitBLAS** 等）相比，到底快不快？

为了让结果一目了然，README 用**对比图**来呈现：同一张图里画多条曲线，每条曲线代表一个框架，横轴是不同的测试尺寸，纵轴是性能（TFLOPS 或延迟）。曲线越高、越靠上，说明那个框架越快。

> **直觉理解**：你可以把这段汇总想成「产品首页的宣传图」——它不教你细节，但让你立刻知道这个项目在比什么、TileLang 表现如何。

#### 4.1.2 核心流程

要读懂一张性能对比图，按这个顺序看：

1. **先看图标题/说明文字**：确认这张图测的是哪个算子、在哪块 GPU 上测的。
2. **再看横轴**：每个刻度是一个具体的 shape（测试尺寸），对应后面要讲的 V/M/FA 等配置表。
3. **看纵轴单位**：是 TFLOPS（越高越好）还是延迟 latency（越低越好）。
4. **对比曲线**：找出代表 TileLang 的那条曲线，看它相对于 cuBLAS、Triton 等基线的位置。

整个 README 的「Benchmark Summary」一共放了三张图，覆盖三类典型场景（见 4.1.3）。

#### 4.1.3 源码精读

README 的 Benchmark Summary 部分用三段带图片的列表给出结果。第一张是 H100 上的 Flash Attention 性能：

> 这段位于 [README.md:9-12](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/README.md#L9-L12)，引用了图片 `./images/mha_performance_h100.png`，说明它测的是**在 NVIDIA H100 上、Flash Attention（融合注意力）算子**的性能。

第二张是跨四款 GPU 的矩阵乘（fp16）性能：

> 位于 [README.md:14-18](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/README.md#L14-L18)，引用 `./images/op_benchmark_consistent_gemm_fp16.png`，覆盖 **RTX 4090、A100、H100、MI300X** 四款卡，测的是 **fp16 精度的 GEMM（矩阵乘）**。注意这里同时出现了 NVIDIA（前三款）和 AMD（MI300X）的卡，说明项目支持跨厂商对比。

第三张是 A100 上的反量化矩阵乘（dequantize gemv）：

> 位于 [README.md:20-24](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/README.md#L20-L24)，引用 `./images/op_benchmark_a100_wq_gemv.png`，测的是 **A100 上、权重被量化（压缩）后的 GEMV（矩阵-向量乘）**。`wq` 表示 weight-quantized（权重量化），这是大模型推理里很常见的场景。

这三张图覆盖了项目的三大类核心算子：**注意力（Attention）、普通矩阵乘（GEMM）、量化矩阵乘（Dequantize MatMul）**，正好对应后面四个架构目录下的算子分类。

#### 4.1.4 代码实践

**实践目标**：亲手打开 README 里的三张性能图，确认每张图测的是什么。

**操作步骤**：

1. 在浏览器打开仓库主页，或直接访问永久链接 [README.md:9-24](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/README.md#L9-L24)。
2. 分别点击三张图片（或访问 `images/` 目录下的 `mha_performance_h100.png`、`op_benchmark_consistent_gemm_fp16.png`、`op_benchmark_a100_wq_gemv.png`）。
3. 对每张图，记录：算子类型、GPU 型号、图中出现了哪几个框架的曲线。

**需要观察的现象**：每张图里应该有多条不同颜色的曲线，分别标注 cuBLAS、Triton、TileLang 等框架。

**预期结果**：你能用一句话概括每张图，例如「图 1：H100 上 Flash Attention，TileLang 与 cuBLAS/FA3 等对比」。

> 待本地验证：具体每条曲线代表哪个框架、TileLang 排在第几，需要你实际打开图片确认，本讲不预设结论。

#### 4.1.5 小练习与答案

**练习 1**：README 的三张性能图分别测了哪三类算子？

> **参考答案**：Flash Attention（注意力）、fp16 GEMM（普通矩阵乘）、Dequantize GEMV（量化矩阵-向量乘）。

**练习 2**：第二张图（GEMM fp16）覆盖了哪几款 GPU？其中哪一款是 AMD 的？

> **参考答案**：RTX 4090、A100、H100、MI300X；其中 MI300X 是 AMD 的（其余三款是 NVIDIA）。

---

### 4.2 shape 配置表 V / M / FA / CC / CT

#### 4.2.1 概念说明

性能对比要公平，就必须**固定一套统一的测试尺寸**——否则你测 1024×1024、我测 8192×8192，结果没法比。这套统一的测试尺寸就叫 **shape 配置（shape set）**。

README 用三张表（共五组列族）定义了本项目的测试集，每组用字母前缀命名：

| 前缀 | 含义 | 算子类型 | 出现在 README 哪张表 |
|------|------|----------|----------------------|
| **V** | Vector（向量）场景，\(m=1\) | GEMV（矩阵 × 向量） | Table 1 上半 |
| **M** | Matrix（矩阵）场景，\(m\) 较大 | GEMM（矩阵 × 矩阵） | Table 1 下半 |
| **FA** | Flash Attention | 注意力（前向） | Table 2 |
| **CC / CT** | 两类 Linear Attention（线性注意力）变体 | 线性注意力 | Table 3 |

> **直觉理解**：你可以把每组 shape 配置想成「一套考卷」。V 卷专门考「矩阵乘向量」这种偏瘦的形状；M 卷考「大方阵相乘」；FA 卷考注意力；CC/CT 卷考线性注意力。所有框架做同一套卷子，分数才有可比性。

每组里的每一列（如 V0、V1、M3、FA2）就是一道「具体的题」，对应一组具体的尺寸。

#### 4.2.2 核心流程

给定一个 shape 编号（比如 M3），还原出实际张量尺寸的流程是：

1. 在对应表格里找到那一列。
2. 读出该列每一行（m、n、k 等）的数值。
3. 按算子的语义组装成张量：对 GEMM，A 是 `m×k`，B 是 `k×n`，C 是 `m×n`；对 Attention，则是 `batch、nheads、seq_len、head_dim` 这几个维度。

例如查 M3：m=4096、n=8192、k=28672，于是这个测试用例的矩阵 A 大小是 4096×28672，B 是 28672×8192。

> **为什么 V 全是 \(m=1\)？** 因为 \(m=1\) 时矩阵乘退化成「矩阵乘一个向量」，也就是 GEMV，这正是大模型「单条请求推理」（batch=1 decoding）的典型形状，所以单独列一组。

#### 4.2.3 源码精读

**Table 1 —— 矩阵 shape（V 与 M 两组）**：

> 位于 [README.md:31-45](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/README.md#L31-L45)。
>
> - V0–V7（[L34-L38](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/README.md#L34-L38)）：所有行的 `m` 都是 1，`n`/`k` 在 9216 到 57344 之间变化——这是 **GEMV**（矩阵-向量乘）尺寸集。
> - M0–M7（[L41-L45](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/README.md#L41-L45)）：`m` 为 4096 或 8192，`n`/`k` 在 1024 到 28672 之间——这是**真·GEMM**（矩阵-矩阵乘）尺寸集。

**Table 2 —— FlashAttention shape（FA 组）**：

> 位于 [README.md:49-57](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/README.md#L49-L57)，FA0–FA4 固定 `batch=1、nheads=32、head_dim=128`，让 `seq_len`（512 / 1024 / 4096）和 `causal`（true / false）变化。`causal=true` 表示带因果掩码（只看前面的 token），是语言模型的标准注意力形式。

**Table 3 —— Linear Attention shape（CC 与 CT 两组）**：

> 位于 [README.md:61-79](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/README.md#L61-L79)。
>
> - CC0–CC5（[L64-L70](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/README.md#L64-L70)）
> - CT0–CT5（[L73-L79](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/README.md#L73-L79)）
>
> 这两组都固定 `nheads=64、head_dim=64、d_state=128`，让 `batch`（1 或 64）与 `seq_len`（1024 / 2048 / 8192）变化，区别在于 CC 与 CT 代表线性注意力的两种不同实现变体。

#### 4.2.4 代码实践

**实践目标**：从 shape 表里挑几个编号，亲手还原出张量尺寸，检验自己看懂了表格。

**操作步骤**：

1. 打开 [README.md:31-45](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/README.md#L31-L45)。
2. 查 V0：m=1, n=16384, k=16384。写出矩阵 A（1×16384）和 B（16384×16384）的形状。
3. 查 M3：m=4096, n=8192, k=28672。写出 A、B、C 的形状。
4. 查 FA4（[L51-L57](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/README.md#L51-L57)）：写出 batch、seq_len、head_dim，并判断它是否带因果掩码。

**需要观察的现象**：V 组所有行的 m 恒为 1；M 组的 m 明显更大。

**预期结果**：
- V0 → A: 1×16384，B: 16384×16384（GEMV）。
- M3 → A: 4096×28672，B: 28672×8192，C: 4096×8192（GEMM）。
- FA4 → batch=1, nheads=32, seq_len=4096, head_dim=128, causal=true。

> 这是纯阅读型实践，不需要运行任何代码，自己核对即可。

#### 4.2.5 小练习与答案

**练习 1**：V 组和 M 组都是矩阵乘的 shape，它们最本质的区别是什么？

> **参考答案**：V 组的 \(m=1\)，对应矩阵-向量乘（GEMV），模拟单条推理请求；M 组的 \(m\) 为 4096/8192，对应真正的矩阵-矩阵乘（GEMM），模拟较大的 batch 或训练场景。

**练习 2**：FA 这组 shape 里，哪两个维度是「真正在变化」的？哪几个是固定的？

> **参考答案**：变化的是 `seq_len`（512/1024/4096）和 `causal`（true/false）；固定的是 `batch=1`、`nheads=32`、`head_dim=128`。

**练习 3**：CC 和 CT 两组表的尺寸完全一样，为什么还要分成两组？

> **参考答案**：它们尺寸相同但代表**两种不同的 Linear Attention 实现变体**（不同的内核算法），分开列是为了分别对比各自的性能。

---

### 4.3 归档提示与 TileOps 链接

#### 4.3.1 概念说明

开源项目通常有一个生命周期：活跃开发 → 维护 → 归档（archived）。**「归档」意味着项目作者不再继续在这个仓库里开发新功能、修 bug**，仓库被「冻结」在某个历史状态。

`tilelang-benchmark` 目前就处于已归档状态。但归档不等于「没用了」——它的价值在于：

1. 它是一份**完整的、可复现的历史基准数据**，记录了 TileLang 在某个时间点相对于各基线的性能。
2. 它的代码结构、内核写法、对比方法论，依然是学习 GPU 算子开发的**绝佳教材**——这正是本学习手册存在的原因。
3. 作者把后续工作迁移到了一个新仓库 **TileOps**，继续维护。

> **直觉理解**：把 `tilelang-benchmark` 想成一本「已经定稿的旧教材」，TileOps 是「作者正在写的新教材」。旧教材不再更新，但内容依然值得学；想看最新进展，去新教材。

#### 4.3.2 核心流程

找到归档提示与迁移去向的流程：

1. 打开 `README.md` 最顶部（第 1 行起的标题区）。
2. 标题里会有 `(Archived, please checkout ...)` 字样。
3. 括号里的链接就是新仓库地址。
4. （补充）GitHub 还会在仓库主页顶部显示一条官方的「This repository has been archived」横幅，那是 GitHub 平台层面的标记。

#### 4.3.3 源码精读

归档提示直接写在 README 的最大标题里：

> 位于 [README.md:1-3](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/README.md#L1-L3)，标题原文是：
>
> > **Tile Lang Benchmark (Archived, please checkout [TileOps](https://github.com/tile-ai/TileOPs))**
>
> 这一行同时传达了三件事：① 项目名叫 Tile Lang Benchmark；② 状态是 Archived（已归档）；③ 后续工作请看 [TileOps](https://github.com/tile-ai/TileOPs)。

注意链接里的大小写是 `TileOPs`（OP 大写），点击后会跳转到 `https://github.com/tile-ai/TileOPs`。

#### 4.3.4 代码实践

**实践目标**：确认项目的归档状态，并找到迁移目标仓库。

**操作步骤**：

1. 访问 [README.md:1-3](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/README.md#L1-L3)。
2. 在标题中找到 `(Archived, please checkout ...)` 提示。
3. 点击 `TileOps` 链接，或直接打开 `https://github.com/tile-ai/TileOPs`。
4. （可选）回到仓库主页 `https://github.com/tile-ai/tilelang-benchmark`，观察 GitHub 是否在顶部显示「archived」横幅。

**需要观察的现象**：README 标题明确写了 Archived；点击链接能正常跳转到 TileOps 仓库。

**预期结果**：你确认了本项目已归档、后续工作在 TileOps，并记下了两个仓库的地址。

> 待本地验证：TileOps 仓库当前的具体内容与发展状态需你访问后自行确认，本讲只负责指出迁移方向。

#### 4.3.5 小练习与答案

**练习 1**：项目标题里 `Archived` 这个词，对使用者意味着什么？

> **参考答案**：意味着作者已停止在该仓库的主动维护（不再加新功能、不再修 bug），仓库处于冻结的历史状态；但仍可作为参考资料和可复现的基准使用。

**练习 2**：如果想看 TileLang 基准测试的最新进展，应该去哪个仓库？

> **参考答案**：去 [TileOps](https://github.com/tile-ai/TileOPs)（`https://github.com/tile-ai/TileOPs`）。

---

## 5. 综合实践

把本讲三个模块串起来，完成一份「项目一页纸速览」。

**任务**：假设你要给同事用 5 分钟介绍 `tilelang-benchmark` 这个项目，请基于 `README.md` 写一份不超过一页的速览，必须包含以下信息：

1. **一句话定位**：这个项目是做什么的？（提示：按 GPU 架构组织、用 TileLang 与 cuBLAS/Triton/BitBLAS 等多框架对比的算子性能基准套件。）
2. **架构与算子**：列出四大架构目录（ada / ampere / hopper / cdna），并各举一个该架构下出现的算子。可参考本讲「3. 本讲源码地图」给出的目录结构与「4.1.3」提到的三大算子类别。
3. **shape 体系**：说明 V / M / FA / CC / CT 五组分别测什么类型的算子。
4. **项目状态**：标注已归档，并写出迁移目标 TileOps 的链接。

**预期产出**（示例骨架，供你对照）：

```
项目：tilelang-benchmark
定位：按 GPU 架构组织、TileLang vs cuBLAS/Triton/BitBLAS 等多框架对比的算子性能基准套件。
架构目录：ada(RTX 4090)、ampere(A100)、hopper(H100)、cdna(MI300X)。
算子类别：Attention / GEMM / 量化(Dequantize) MatMul 等。
shape 体系：V=GEMV、M=GEMM、FA=FlashAttention、CC/CT=Linear Attention。
状态：已归档，迁移至 https://github.com/tile-ai/TileOPs 。
```

> 这是纯阅读 + 整理型实践，不需要运行代码。完成后你就把本讲的三个最小模块融会贯通了。

## 6. 本讲小结

- `tilelang-benchmark` 是一套**按 GPU 架构组织**、用 **TileLang DSL** 与 **cuBLAS / Triton / BitBLAS** 等多框架做对比的**算子性能基准套件**。
- 仓库顶层有四大架构目录：`ada_benchmark`、`ampere_benchmark`、`hopper_benchmark`（NVIDIA）与 `cdna_benchmark`（AMD）。
- README 的 Benchmark Summary 用三张图覆盖三大类算子：Flash Attention、fp16 GEMM、Dequantize GEMV。
- shape 配置表用五组列族定义统一测试集：**V**（GEMV）、**M**（GEMM）、**FA**（FlashAttention）、**CC/CT**（Linear Attention），保证各框架对比公平。
- 项目**已归档**，后续工作迁移到了 [TileOps](https://github.com/tile-ai/TileOPs)；本仓库作为历史基准与学习教材依然有价值。

## 7. 下一步学习建议

本讲只读了 README，建立的是「顶层全局观」。接下来建议：

1. **第 2 讲《目录组织约定》(u1-l2)**：进入四大架构目录内部，搞清楚「架构 → 算子 → 编号化框架子目录（0.cublas / 1.triton / 2.tilelang…）」的层次约定，学会精确定位某个内核文件。
2. **第 3 讲《运行一次基准测试》(u1-l3)**：以 `dense_matmul` 为例，看懂 `benchmark.sh` 是如何依次调用 cuBLAS、Triton、TileLang 完成一次对比运行的。
3. 如果你想先建立「怎么公平对比」的方法论直觉，也可以跳到第 2 单元（u2）的《性能度量方法论》。

后续所有讲义都会频繁引用本讲提到的架构目录与 shape 体系，所以请确保你已经能熟练说出 V/M/FA/CC/CT 各自测什么。
