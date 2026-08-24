# 计算引擎与 GEMM 数据流水线

## 1. 本讲目标

本讲是单元二「GPU 执行模型」的收尾。前两讲我们分别弄清了「线程如何组织」（u2-l1）和「数据放在哪里」（u2-l2），本讲回答第三个问题：**真正干活的硬件是什么，它们如何配合**。学完本讲，你应该能够：

1. 区分 CUDA Core、Tensor Core、TMA 引擎三类硬件单元各自的职责。
2. 描述一个 GEMM tile 从 GMEM 到最终写回 GMEM 的三段式流水线（Load → Compute → Epilogue），并展开成七步数据路径。
3. 解释「串行执行导致硬件轮流空闲」与「流水线让多个引擎同时忙碌」的差别，并能识别每个阶段交接处需要的完成信号（completion barrier）。

这三个能力是后续所有 GEMM 优化步骤（单元十一至十三）和 Flash Attention 4（单元十四）的直接地基。

## 2. 前置知识

本讲默认你已读过 u2-l1（线程执行层级）和 u2-l2（内存空间）。需要回忆的概念：

- **执行层级与 scope**：thread、warp、warpgroup、CTA、cluster、grid 六级层级；一个操作由谁发起、谁受益，叫这个操作的 scope。例如 TMA copy 由单个 thread 发起，而完整 TMEM 累加器要 4 个 warp 各读自己的 32-lane 窗口。
- **四种存储空间**：GMEM（device 级大容量 HBM）、SMEM（CTA 私有低延迟暂存，B200 每 SM 最多 228 KB）、TMEM（CTA 私有、专存 MMA 累加器，128 lane × 最多 512 列）、RF（每 thread 私有寄存器）。

本讲新引入的概念，用通俗方式先解释：

| 术语 | 通俗解释 |
|------|----------|
| **流水线（pipeline）** | 像洗衣店：洗衣机（TMA）、烘干机（Tensor Core）、折叠台（epilogue）是三台独立机器。串行做法是洗完一批、烘干、叠好，才开始洗下一批；流水线做法是烘干第一批的同时洗第二批、叠第一批。机器台数没变，吞吐却接近原来成倍提升 |
| **重叠（overlap）** | 流水线的本质：不同硬件引擎在同一时刻各自处理**不同 tile**，互不违反数据依赖 |
| **空闲（idle）** | 引擎没事可做的时段。串行内核里，TMA 搬数据时 Tensor Core 在等，Tensor Core 算时 TMA 在等——这就是要消除的浪费 |
| **完成信号（completion barrier）** | 异步交接的「签收单」。TMA 是异步的：发起拷贝的线程不能假设数据已经到位，必须等硬件报告「所有预期字节都写进 SMEM 了」才能让计算开始。这类信号的具体机制（mbarrier、phase、commit group）在单元八展开，本讲只需要识别「哪里需要信号」 |
| **epilogue（收尾阶段）** | 矩阵乘法主体完成后的收尾工作：把 fp32 累加结果从 TMEM 读回寄存器、转成输出 dtype（如 fp16）、写回 GMEM |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [chapter_background/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md) | 本讲对应的原书章节 *GPU Execution Model*，本讲主要精读其中「Compute」与「The GEMM Data Pipeline」两节 |
| [zh/chapter_background/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_background/index.md) | 上述章节的中文镜像，术语对照（三阶段、完成 barrier、流水线） |
| [`_extra/demo/pipeline_arch.html`](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/pipeline_arch.html) | 交互式演示：点击 5 个动作（TMA load / tcgen05.mma / tcgen05.ld / store / TMA store），高亮对应的数据路径与硬件单元 |
| [img/scripts/README.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/README.md) | 仓库自己用 matplotlib + numpy 重新生成全书插图的脚本清单，本讲综合实践沿用同一套惯例 |

> 提示：`pipeline_arch.html` 是自包含的交互页面（只引用仓库内 `_extra/viz-base.css` 与 `_extra/viz-base.js`），用浏览器直接打开该文件即可交互，无需先构建书站。

## 4. 核心概念与源码讲解

### 4.1 三类引擎的分工：CUDA Core、Tensor Core 与 TMA

#### 4.1.1 概念说明

一个 SM 里真正执行工作的硬件单元不止一种，本讲关注三类：

- **CUDA Core**：通用 SIMT ALU（算术逻辑单元）。它跑标量和向量指令，负责内核里所有「非矩阵乘」的工作——地址计算、循环与分支等控制流、elementwise 运算、reduction。
- **Tensor Core**：固定功能单元，以 **tile 为粒度**做稠密矩阵乘累加，一条指令完成 \( D = AB + C \)。它的算术吞吐量通常是 CUDA Core 的 10 倍以上（FLOP/s 口径）。GEMM、卷积、attention 这类稠密线性代数负载，只有把主体计算喂给 Tensor Core，才可能接近峰值性能。
- **TMA 引擎**：专职数据搬运的硬件。它替线程完成整块 tile 的地址计算与异步传输（本讲只把它当作「搬运引擎」使用，描述符细节在单元六展开）。

一句话概括分工原则：**矩阵乘法交给 Tensor Core，围绕矩阵乘法的杂务交给 CUDA Core，整块搬运交给 TMA**。这三类引擎彼此独立、可同时工作——这正是 4.3 节流水线重叠的物理基础。

Tensor Core 本身也在跨代演进，演进主线是「编程接口与累加器位置」：

| 架构 | 指令 | 累加器位置 |
|------|------|-----------|
| Ampere | `mma.sync` | 寄存器 fragment（每线程持有一小块） |
| Hopper | `wgmma.mma_async` | 仍在寄存器，但四个 warp 协作、可直读 SMEM |
| Blackwell | `tcgen05` | **TMEM**（卸下寄存器压力，承接 u2-l2） |

此外，cluster 机制让两个 CTA 各出一部分 SMEM 操作数、合成一个更大的 Tensor Core MMA tile（2-CTA cooperative MMA），也让一次 GMEM 读取通过 TMA multicast 同时送到多个 CTA。这是后续 GEMM Step 8 的伏笔，本讲记住结论即可。

#### 4.1.2 核心流程

把一个 SM 内的引擎和存储画成一张「岗位表」：

| 引擎/单元 | 职责 | 在 GEMM 中的典型工作 |
|-----------|------|---------------------|
| TMA 引擎 | 异步批量搬运 | Load 阶段 GMEM→SMEM；Epilogue 阶段 SMEM→GMEM |
| Tensor Core（tcgen05） | tile 级矩阵乘累加 | Compute 阶段读 SMEM 的 A/B，累加进 TMEM |
| CUDA Core | 标量/向量/控制流 | 地址与循环、epilogue 的 fp32→fp16 转换 |
| SMEM / TMEM / RF | 存储（u2-l2） | tile 暂存 / 累加器 / 每 thread 的切片 |

分析任何一个内核，都可以按这个顺序提问：

1. 主体计算是否在 Tensor Core 上？（否则性能上限立刻被压低一个量级）
2. 数据搬运是否交给了 TMA，而不是让大量线程手搬？
3. CUDA Core 只剩杂务，还是被迫做了本该由引擎做的事？

#### 4.1.3 源码精读

原书在 `Compute: CUDA Cores and Tensor Cores` 一节给出两类计算单元的定义，指出 CUDA Core 是跑标量/向量指令的通用 SIMT ALU，而 Tensor Core 是以 tile 粒度计算 \( D = AB + C \) 的固定功能单元：

- [chapter_background/index.md:L127-L130](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L127-L130)：CUDA Core 负责地址计算、elementwise、reduction 和控制流；Tensor Core 一条指令完成 tile 级稠密矩阵乘累加。

紧接着一段说明吞吐差距与「喂料」要求——Tensor Core 吞吐通常是 CUDA Core 的 10 倍以上，高性能内核必须**及时备好数据**，别让 Tensor Core 等数据或等依赖而空转：

- [chapter_background/index.md:L132-L135](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L132-L135)：这句「不要让 Tensor Core 空转」正是 4.3 节流水线的动机。

关于三代演进，书中点名 Hopper 的 `wgmma.mma_async` 与 Blackwell 第五代 Tensor Core `tcgen05` 把累加器放进 Tensor Memory 而非寄存器：

- [chapter_background/index.md:L137-L140](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L137-L140)：架构代际改变的不只是吞吐，还有编程接口与累加器位置。

cluster 带来的两种协作（2-CTA cooperative MMA 与 TMA multicast）：

- [chapter_background/index.md:L142-L146](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L142-L146)：两个 CTA 各提供一部分 SMEM 操作数合成更大 MMA tile；一次 GMEM load 多播给多个 CTA，避免重复的全局内存流量。

交互演示把这套硬件画了出来，SM 内一共有六类单元：Tensor Core（标注 5th-gen MMA）、CUDA Core（FP/INT units）、SMEM（228 KB per SM）、TMEM（128 lanes）、Register File、TMA Engine（Data Mover），SM 外是 Global Memory（HBM）：

- [`_extra/demo/pipeline_arch.html:L209-L241`](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/pipeline_arch.html#L209-L241)：SM 内六类硬件单元的 HTML 定义，每类单元都是一个可点击的盒子。
- [`_extra/demo/pipeline_arch.html:L249-L251`](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/pipeline_arch.html#L249-L251)：SM 下方的 GMEM（HBM）盒子，是 TMA 搬运的起点与终点。

> 中文对照可读 [zh/chapter_background/index.md:L85-L96](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_background/index.md#L85-L96)。

#### 4.1.4 代码实践：用交互演示核对引擎分工

这是一个纯浏览器实践，无需 GPU、无需安装任何东西。

1. **实践目标**：验证「每个流水线动作由哪些引擎和存储参与」，把课堂结论变成自己核对过的事实。
2. **操作步骤**：
   - 用浏览器打开仓库中的 `_extra/demo/pipeline_arch.html`（直接双击文件即可，它只引用仓库内相对路径的 `viz-base.css` / `viz-base.js`）。
   - 依次点击顶部流水线条上的 5 个动作标签：`TMA load`、`tcgen05.mma`、`tcgen05.ld`、`store`、`TMA store`。
   - 每次点击后观察下方架构图：被点亮的硬件单元就是该动作的参与者，同时底部面板显示一句话说明。
   - 把结果填进一张四列表格：动作 | 参与引擎 | 涉及的存储空间 | 一句话说明。
3. **需要观察的现象**：例如点击 `store` 时，点亮的应该是 Register File、CUDA Core、SMEM 三者——因为这一步是 CUDA Core 在寄存器里做 fp32→fp16 转换后写入 SMEM 回写缓冲，而不是 TMA 参与的动作。
4. **预期结果**：5 行表格。可与演示源码中的定义对照——每个动作的说明、涉及的流水线阶段（`pipeStages`）与硬件单元（`archUnits`）都写死在 [`_extra/demo/pipeline_arch.html:L300-L326`](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/pipeline_arch.html#L300-L326) 的 `actions` 对象里，例如 `tma_load` 条目列出的 `['boxHBM', 'boxTMA', 'boxSMEM']` 与 `store` 条目列出的 `['boxRF', 'boxCUDA', 'boxSMEM']`。
5. 本实践不依赖运行环境，可立即完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么循环控制、地址计算这些工作不在 Tensor Core 上做？

**答案**：Tensor Core 是固定功能单元，只会做 tile 级的稠密矩阵乘累加（\( D = AB + C \)）；控制流与标量逻辑是 CUDA Core 这种通用 SIMT ALU 的职责（[chapter_background/index.md:L127-L128](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L127-L128)）。

**练习 2**：相比 Hopper，Blackwell `tcgen05` 把累加器放到了哪里？这缓解了什么压力？

**答案**：从寄存器改到 Tensor Memory（TMEM）。随着 MMA tile 变大，寄存器形式的累加器会占用寄存器堆的很大份额，搬进 TMEM 后寄存器压力显著下降（[chapter_background/index.md:L137-L140](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L137-L140)，并承接 u2-l2 对 TMEM 结构的讲解）。

**练习 3**：epilogue 中把 fp32 累加结果转成 fp16 的 cast 操作由谁执行？

**答案**：CUDA Core。演示中 `store` 动作明确写明「CUDA cores cast fp32 → fp16 in registers, then store results to SMEM writeback buffer」（[`_extra/demo/pipeline_arch.html:L316-L320`](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/pipeline_arch.html#L316-L320)）。这是「Tensor Core 管乘、CUDA Core 管杂务」的典型例子。

### 4.2 三段式 GEMM tile 流水线：Load → Compute → Epilogue

#### 4.2.1 概念说明

GEMM（矩阵乘 \( D = A \times B \)）在 GPU 上不是一行一行算的，而是把输出切成一个个 **tile**（例如 128×128 的块），每个 tile 由一个 CTA 负责。一个输出 tile 的生命期分三段：

1. **Load（加载）**：TMA copy 把 A 或 B 的 operand tile 从 GMEM 搬到 SMEM。一个 thread 发起拷贝并登记「预计到达的字节数」；数据陆续写入 SMEM 时 TMA 引擎更新进度计数，**只有所有预期字节到齐，完成 barrier 才算完成**。
2. **Compute（计算）**：`tcgen05` MMA 从 SMEM 读 operand tile，把乘积累加进一块 TMEM tile。由一个选定的 thread 提交这条 MMA；计算完成时硬件向对应的 barrier 发完成信号。
3. **Epilogue（收尾）**：warpgroup 把 TMEM 累加器读回寄存器，转换成输出 dtype 后写回 GMEM。这一步通常先在 SMEM 中转（staging），最终写回可以用 TMA store。

为什么恰好分三段？因为**每段由不同引擎主导、占用不同存储**：Load 主导权在 TMA 引擎、落在 SMEM；Compute 主导权在 Tensor Core、落在 TMEM；Epilogue 主导权回到 CUDA Core 与 TMA、数据经寄存器回 GMEM。段与段之间有真实的数据依赖，但——这是关键——**依赖只存在于同一个 tile 内部**，不同 tile 的三段彼此独立，所以可以错开执行（4.3 节）。

#### 4.2.2 核心流程

把三段展开成数据路径上的七步（与交互演示的流水线条一一对应）：

```text
GMEM(A,B) --① TMA load--> SMEM --② tcgen05.mma--> TMEM(累加)
         --③ tcgen05.ld--> 寄存器 --④ cast+store--> SMEM(回写缓冲)
         --⑤ TMA store--> GMEM(D)
```

| 步 | 动作 | 源 → 目的 | 执行引擎 | 发起方式（scope，承接 u2-l1） |
|----|------|-----------|----------|-------------------------------|
| ① | TMA load | GMEM → SMEM | TMA 引擎 | 单个 thread 发起 |
| ② | tcgen05.mma | SMEM(A,B) → TMEM | Tensor Core | 一个选定的 thread 提交 |
| ③ | tcgen05.ld | TMEM → 寄存器 | TMEM 读端口 | 每 warp 读自己的 32-lane 窗口 |
| ④ | cast + store | 寄存器 → SMEM | CUDA Core | warpgroup 全体 thread |
| ⑤ | TMA store | SMEM → GMEM | TMA 引擎 | 单个 thread 发起 |

其中 ①② 构成 Load 与 Compute 两段，③④⑤ 合起来是 Epilogue 段（④⑤ 即「经 SMEM 中转、用 TMA store 写回」）。

三段的伪代码骨架：

```text
# 对每个输出 tile（伪代码，仅示意三段结构）
load(tile k):    发起 TMA copy；等待完成 barrier（预期字节全部到达）
compute(tile k): 提交 tcgen05 MMA；等待 MMA 完成 barrier
epilogue(tile k): tcgen05.ld 读回寄存器 → cast → 写 SMEM → TMA store 回 GMEM
```

#### 4.2.3 源码精读

原书 `The GEMM Data Pipeline` 一节开宗明义：前面几节分别讲了线程层级、内存空间、数据搬运与计算单元，现在用一条 GEMM 流水线把它们串起来，并给出交互图：

- [chapter_background/index.md:L148-L161](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L148-L161)：章节标题与嵌入 `pipeline_arch.html` 的 iframe，说明「点击阶段可查看从 load、MMA 到 epilogue 的数据路径」。

三段的正式定义（逐段对应 4.2.1 的编号）：

- [chapter_background/index.md:L165-L168](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L165-L168)：**Load**——一个 thread 发起 TMA copy 并登记预期到达字节数，所有预期字节到达后完成 barrier 才完成。
- [chapter_background/index.md:L169-L171](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L169-L171)：**Compute**——`tcgen05` MMA 从 SMEM 读 operand、累加进 TMEM，完成后硬件向对应 barrier 发信号。
- [chapter_background/index.md:L172-L174](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L172-L174)：**Epilogue**——warpgroup 读回 TMEM 累加器、转输出 dtype、写回 GMEM，通常经 SMEM 中转并可用 TMA store。

交互演示顶部的流水线条把七步路径画成一排盒子与箭头：

- [`_extra/demo/pipeline_arch.html:L155-L194`](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/pipeline_arch.html#L155-L194)：`GMEM(A,B) → [TMA load] → SMEM → [tcgen05.mma] → TMEM(accum) → [tcgen05.ld] → Reg(cast) → [store] → SMEM → [TMA store] → GMEM(D)`，每个箭头都是可点击的动作标签。

每个动作的一句话说明写在演示的 `actions` 对象里，与上表逐条对应：

- [`_extra/demo/pipeline_arch.html:L301-L305`](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/pipeline_arch.html#L301-L305)：`tma_load`——TMA 引擎把 tile 从 GMEM 拷到 SMEM，一个 thread 发指令，硬件负责地址计算、swizzle 与异步传输。
- [`_extra/demo/pipeline_arch.html:L306-L310`](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/pipeline_arch.html#L306-L310)：`mma`——Tensor Core 从 SMEM 读 A/B，把 \( C \mathrel{+}= A \times B \) 累加进 TMEM，硬件把工作分布到 128 条 TMEM lane 上。
- [`_extra/demo/pipeline_arch.html:L311-L315`](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/pipeline_arch.html#L311-L315)：`ld`——把累加好的 fp32 结果从 TMEM 读进寄存器，每个线程拿到输出 tile 中自己的切片。
- [`_extra/demo/pipeline_arch.html:L316-L320`](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/pipeline_arch.html#L316-L320)：`store`——CUDA Core 在寄存器里做 fp32→fp16 转换，然后写入供 TMA 冲刷到 GMEM 的 SMEM 回写缓冲。
- [`_extra/demo/pipeline_arch.html:L321-L325`](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/pipeline_arch.html#L321-L325)：`tma_store`——TMA 引擎把 fp16 结果 tile 从 SMEM 回写缓冲拷回 GMEM，异步执行、可与下一迭代重叠。

> 阅读提示：演示中 `mma` 的说明写着「Single warp issues」，而书正文写「一个选定的 thread 提交」（[chapter_background/index.md:L169-L171](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L169-L171)，与 u2-l1 的 scope 表一致）。以正文为准：tcgen05 MMA 的提交者是单个选定 thread，演示文案是简化说法。
>
> 中文对照可读 [zh/chapter_background/index.md:L112-L116](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_background/index.md#L112-L116)。

#### 4.2.4 代码实践：纸面跟踪一个 tile 走完七步

这是一个源码阅读型实践，无需 GPU。

1. **实践目标**：把「三段」落实到「每一步谁发起、数据从哪到哪」，为 4.3 的重叠分析打好基础。
2. **操作步骤**：
   - 设定场景：一个 CTA 负责计算 128×128 的 fp16 输出 tile，K 维分块为 64（记该 K 块为 tile `k`，只跟踪这一个 tile）。
   - 对照 4.2.2 的五行动作表，逐步写出：这一步的发起者（哪个 scope 的线程）、执行引擎、源存储空间、目的存储空间、下一步要等谁。
   - 对照 `_extra/demo/pipeline_arch.html` 逐个点击动作核对你的答案（重点核对 `pipeStages` 与 `archUnits`）。
   - 最后回答：哪几步属于 Load 段？哪几步属于 Compute 段？哪几步属于 Epilogue 段？
3. **需要观察的现象**：每一步的「执行引擎」与「发起线程 scope」不是一回事——例如 ① 由单个 thread 发起、却由 TMA 引擎执行；③ 由每个 warp 各自发起、各读 32-lane 窗口。
4. **预期结果**：得到一张五行（或按你细分的七行）表格，段归属为：① = Load；② = Compute；③④⑤ = Epilogue。与 [chapter_background/index.md:L163-L174](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L163-L174) 的三段定义一致。
5. 本实践为纸面推演，可立即完成。

#### 4.2.5 小练习与答案

**练习 1**：七步数据路径中，哪几步用到了 TMA 引擎？

**答案**：两步——① TMA load（GMEM→SMEM）与 ⑤ TMA store（SMEM→GMEM）。对应演示 `actions` 里 `archUnits` 含 `boxTMA` 的两个条目（[`_extra/demo/pipeline_arch.html:L301-L305`](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/pipeline_arch.html#L301-L305) 与 [`L321-L325`](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/pipeline_arch.html#L321-L325)）。

**练习 2**：为什么 epilogue 不直接从 TMEM 写 GMEM，而要绕道寄存器和 SMEM？

**答案**：TMEM 是 CTA 私有的片上空间，warp 只能通过 `tcgen05.ld` 把数据读进自己的寄存器（u2-l2）；读回后还要做 dtype 转换（fp32→fp16，由 CUDA Core 完成），批量写回 GMEM 则通常先在 SMEM 里拼好回写缓冲、再由 TMA store 一次搬出（[chapter_background/index.md:L172-L174](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L172-L174)）。

**练习 3**：Load 段的完成条件是什么？为什么「TMA 指令返回」不等于「数据可用」？

**答案**：完成条件是登记的预期字节数全部到达 SMEM，即完成 barrier 被 TMA 引擎的进度计数满足。TMA 是异步的：发起线程只提交了拷贝请求，硬件还在传输中，所以必须等字节计数到齐（[chapter_background/index.md:L165-L168](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L165-L168)）。

### 4.3 重叠与空闲：从串行到流水线

#### 4.3.1 概念说明

三段之间有数据依赖，但依赖只在**同一个 tile** 内部。两种执行方式的差别：

- **朴素（naive）内核**：`load → wait → compute → wait → store` 逐 tile 串行。任何时刻只有一个引擎在忙，其余硬件轮流空闲——TMA 搬数据时 Tensor Core 在等，Tensor Core 计算时 TMA 在等，epilogue 期间两者都在等。
- **流水线内核**：让三段错开，分别处理**相邻的不同 tile**。原书的表述是：Tensor Core 计算第 `k` 个 tile 时，TMA 引擎可以搬第 `k+1` 个 tile，epilogue 处理第 `k-1` 个 tile 的输出。三个引擎同时忙碌，吞吐由最慢的一段决定。

重叠能够安全进行的前提，是每个交接点都有**完成信号**。本讲只需要识别三类信号的存在（机制细节留给后续单元）：

| 交接点 | 需要回答的问题 | 信号机制（预告） |
|--------|---------------|-----------------|
| Load → Compute | tile k 的 A/B 是否全部写进 SMEM？ | mbarrier 的预期字节计数（单元六/八） |
| Compute → Epilogue | tile k 的 MMA 是否算完、TMEM 可读？ | MMA 完成后硬件向对应 barrier 发信号（单元七/八） |
| Epilogue → 下一轮 Load | 回写缓冲/流水线 stage 何时可复用？ | TMA store 的 commit group / wait group（单元六） |

注意最后一条：一旦 SMEM 缓冲被多个 tile 的不同阶段**轮流复用**（stage 化），就需要「缓冲空闲」信号防止新数据覆盖还没搬走的旧数据。这正是书中所说「barrier 和 phase 模型负责这些异步阶段之间的安全交接」的含义，phase 机制在单元八专讲。

#### 4.3.2 核心流程

两种执行方式的伪代码对比：

```text
# 朴素：三段串行，逐 tile 执行
for tile in tiles:
    load(tile);    wait_load_done(tile)     # TMA 忙，TC 与 epilogue 空闲
    compute(tile); wait_mma_done(tile)      # TC 忙，TMA 与 epilogue 空闲
    epilogue(tile); wait_store_done(tile)   # epilogue 忙，TMA 与 TC 空闲

# 流水线：三段并行处理相邻 tile（示意）
for k in tiles:
    load(k)        # 与 compute(k-1)、epilogue(k-2) 同时进行
    compute(k)     # 前提：load(k) 完成信号已到
    epilogue(k)    # 前提：compute(k) 完成信号已到
```

用时间公式表达（设三段耗时分别为 \( T_{\text{load}} \)、\( T_{\text{mma}} \)、\( T_{\text{epi}} \)，共 \( N \) 个 tile）：

串行总时间：

\[
T_{\text{serial}} \;\approx\; N \times \left( T_{\text{load}} + T_{\text{mma}} + T_{\text{epi}} \right)
\]

流水线进入稳态后，每个 tile 的间隔（即吞吐的倒数）由最慢的一段决定：

\[
T_{\text{steady}} \;\approx\; \max\left( T_{\text{load}},\; T_{\text{mma}},\; T_{\text{epi}} \right)
\]

直觉：三台机器同时开动后，整体速度被最慢的那台限制。例如 \( T_{\text{load}}=4\mu s \)、\( T_{\text{mma}}=6\mu s \)、\( T_{\text{epi}}=3\mu s \) 时，串行每 tile 需 13 µs，流水线稳态只需 6 µs——瓶颈是 Tensor Core。这也解释了为什么后续 GEMM 优化会不断「围绕 Tensor Core 喂料」。

#### 4.3.3 源码精读

原书在给出三段定义后，点明依赖与串行问题：

- [chapter_background/index.md:L176-L179](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L176-L179)：三段有数据依赖但不必完全串行；朴素内核依次执行 load、wait、compute、wait、store，导致各硬件单元轮流空闲。

随后一句是整章的点题句，给出 k-1 / k / k+1 的重叠关系：

- [chapter_background/index.md:L180-L183](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L180-L183)：高性能内核把它组织成流水线——Tensor Core 算 tile `k` 时，TMA 引擎搬 tile `k+1`，epilogue 处理 tile `k-1`；barrier 与 phase 模型负责异步阶段间的安全交接，后续 GEMM 优化都建立在这一机制之上。

两处完成信号的原文（在 4.2.3 引用的三段定义内部，这里单独点出）：

- [chapter_background/index.md:L165-L168](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L165-L168)：Load 段的完成 = 预期字节数全部到达（TMA 引擎维护进度计数）。
- [chapter_background/index.md:L169-L171](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L169-L171)：Compute 段的完成 = 硬件向对应 barrier 发出 MMA 完成信号。

演示中也能找到重叠的注脚——`tma_store` 的说明明确写了「Async — overlaps with next iteration」（异步，可与下一次迭代重叠）：

- [`_extra/demo/pipeline_arch.html:L321-L325`](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/pipeline_arch.html#L321-L325)：TMA store 异步执行，回写期间其他引擎可以继续处理后续 tile。

> 中文对照可读 [zh/chapter_background/index.md:L118-L120](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_background/index.md#L118-L120)。

#### 4.3.4 代码实践：写一份稳态快照时刻表

纸面实践，无需 GPU。

1. **实践目标**：在流水的稳态中，任取一个时刻，说清「每个引擎正在处理哪个 tile、它开工前等了哪道信号」。
2. **操作步骤**：
   - 取稳态中的某一时刻 \( t \)（此时三个引擎都在忙），填下面这张表：

     | 引擎 | 正在处理的 tile | 开工前提（需要等到的信号） |
     |------|----------------|---------------------------|
     | TMA 引擎 | tile `k+1`（Load） | ？ |
     | Tensor Core | tile `k`（Compute） | ？ |
     | CUDA Core + 回写 | tile `k-1`（Epilogue） | ？ |

   - 为每个问号写出信号名与含义（参考 4.3.1 的三行信号表：Load 的字节计数 barrier、MMA 完成 barrier、store 完成后的缓冲复用信号）。
   - 再补一行：若 Load/Epilogue 共用同一块 SMEM stage 缓冲（轮流复用），第 4 道信号应该出现在哪里？
3. **需要观察的现象**：每个引擎的开工前提都是「上一个 tile 在该阶段的完成信号」，而不是「上一条指令返回」——异步引擎之间只能靠信号交接。
4. **预期结果**：Tensor Core 开工前提 = tile `k` 的 A/B 字节全部到达 SMEM（Load 完成 barrier）；Epilogue 开工前提 = tile `k-1` 的 MMA 完成 barrier；TMA 引擎搬 `k+1` 的前提 = 目标 SMEM stage 已被释放（上一轮 epilogue/store 完成的信号）。第 4 行答案：需要一个「stage 空闲（empty）」信号防止新 tile 的数据覆盖尚未搬走的旧数据——这正是单元八 phase 机制要解决的问题。本条为推演结果，机制细节待单元八本地验证。
5. 本实践为纸面推演，可立即完成。

#### 4.3.5 小练习与答案

**练习 1**：设 \( T_{\text{load}}=4\mu s \)、\( T_{\text{mma}}=6\mu s \)、\( T_{\text{epi}}=3\mu s \)。流水线稳态下每 tile 的间隔约是多少？瓶颈是哪个引擎？

**答案**：\( \max(4,6,3)=6\mu s \)，瓶颈是 Tensor Core（Compute 段）。此时 TMA 与 epilogue 各有空闲时间，属正常现象——优化方向是进一步提高 Tensor Core 占用（这也是单元三 roofline 与优化阶梯要量化的内容）。

**练习 2**：如果删掉 Load 段与 Compute 段之间的 wait，会发生什么？

**答案**：Compute 可能读到尚未写完的 SMEM 数据（TMA 仍在传输中），产生数据竞争、结果错误。完成 barrier 不是可省略的形式主义，而是异步引擎间唯一的「签收」手段（[chapter_background/index.md:L165-L168](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L165-L168)）。

**练习 3**：三段重叠为什么不算违反数据依赖？

**答案**：因为重叠的是**不同 tile** 的三段：tile `k` 的 Compute 依赖 tile `k` 自己的 Load 完成，与 tile `k+1` 的 Load、tile `k-1` 的 Epilogue 没有冲突，各自使用各自的 stage 缓冲（[chapter_background/index.md:L180-L183](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L180-L183)）。真正需要小心的只是缓冲复用时的「空闲」信号。

## 5. 综合实践

**任务：用 matplotlib 画出三段式 GEMM 流水线在 tile `k-1` / `k` / `k+1` 上的重叠时序图，并标注每个交接处的同步信号。**

这是本讲的总实践：上图画出「串行 vs 流水线」两个甘特图对比，下图标出完成信号的位置。仓库本身的全部插图都用 matplotlib 脚本生成（见 [img/scripts/README.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/README.md)，依赖仅为 `matplotlib` 与 `numpy`），本实践沿用同一套惯例。

以下是**示例代码**（非项目原有代码），保存为任意 `.py` 文件直接运行，无需 GPU：

```python
# 示例代码：三段式 GEMM 流水线时序图（串行 vs 流水线 + 完成信号标注）
# 依赖：pip install matplotlib
import matplotlib.pyplot as plt

T = {"load": 4, "mma": 6, "epi": 3}          # 各段耗时（微秒），可修改观察变化
STAGES = ["load", "mma", "epi"]
TILES = ["k-1", "k", "k+1"]
COLOR = {"load": "#3b82f6", "mma": "#059669", "epi": "#f59e0b"}
LANE = {"load": 2, "mma": 1, "epi": 0}        # y 轴：epilogue 在下、load 在上

# ---- 调度 1：串行（load -> wait -> mma -> wait -> epi，逐 tile 依次）----
serial, t = [], 0.0
for tile in TILES:
    for stage in STAGES:
        serial.append((stage, tile, t, t + T[stage]))
        t += T[stage]
serial_total = t

# ---- 调度 2：流水线（引擎空闲 + 输入就绪 + stage 缓冲可复用，三者都满足才开工）----
# STAGE_DEPTH = SMEM 中流水线 stage 缓冲的个数（双缓冲=2）。tile i 的 load 要写入
# tile i-STAGE_DEPTH 用过的那块缓冲，因此必须等它的 epilogue（含 TMA store）完成——信号 ③。
STAGE_DEPTH = 2
pipe = []
engine_free = {s: 0.0 for s in STAGES}
input_ready = {tile: 0.0 for tile in TILES}   # 该 tile 上一段的完成时刻
epi_done = {}                                  # tile -> epilogue 完成时刻
for i, tile in enumerate(TILES):
    for stage in STAGES:
        start = max(engine_free[stage], input_ready[tile])
        if stage == "load" and i >= STAGE_DEPTH:   # 复用更早 tile 的 stage 缓冲
            start = max(start, epi_done[TILES[i - STAGE_DEPTH]])
        end = start + T[stage]
        pipe.append((stage, tile, start, end))
        engine_free[stage], input_ready[tile] = end, end
        if stage == "epi":
            epi_done[tile] = end
pipe_total = max(e for *_, e in pipe)

# ---- 绘图：上=串行，下=流水线 ----
fig, axes = plt.subplots(2, 1, figsize=(11, 5.5), sharex=True)
for ax, sched, title in ((axes[0], serial, "naive: 串行（硬件轮流空闲）"),
                         (axes[1], pipe, "pipelined: 重叠（多引擎同时忙碌）")):
    for stage, tile, s, e in sched:
        ax.barh(LANE[stage], e - s, left=s, height=0.55,
                color=COLOR[stage], edgecolor="white")
        ax.text(s + (e - s) / 2, LANE[stage], tile,
                ha="center", va="center", color="white", fontsize=9, fontweight="bold")
    ax.set_yticks([2, 1, 0], ["TMA (load)", "TensorCore (mma)", "epilogue"])
    ax.set_title(f"{title}   total = {max(e for *_, e in sched):.0f} µs")
    ax.set_xlabel("time (µs)")
    ax.invert_yaxis()

# ---- 在流水线图上标注 tile k 的三处交接信号 ----
ax = axes[1]
t_mma_k   = next(s for st, tl, s, e in pipe if st == "mma" and tl == "k")
t_epi_k   = next(s for st, tl, s, e in pipe if st == "epi" and tl == "k")
t_ld_k1   = next(s for st, tl, s, e in pipe if st == "load" and tl == "k+1")
ax.annotate("① Load 完成：k 的 A/B 字节全部到达 SMEM\n（mbarrier 预期字节计数满足）",
            xy=(t_mma_k, 1), xytext=(t_mma_k + 7, 0.15), fontsize=8,
            arrowprops=dict(arrowstyle="->", color="#333"))
ax.annotate("② MMA 完成：硬件向 barrier 发信号",
            xy=(t_epi_k, 0), xytext=(t_epi_k + 7, 0.75), fontsize=8,
            arrowprops=dict(arrowstyle="->", color="#333"))
ax.annotate("③ stage 空闲：k-1 的 epilogue（含 TMA store）完成后，\n双缓冲中它占用的那块 SMEM 才能被 k+1 复用",
            xy=(t_ld_k1, 2), xytext=(t_ld_k1 + 7, 1.7), fontsize=8,
            arrowprops=dict(arrowstyle="->", color="#333"))

print(f"串行总时间   = {serial_total} µs  (每 tile {serial_total/len(TILES)} µs)")
print(f"流水线总时间 = {pipe_total} µs  (稳态每 tile {max(T.values())} µs = max 段耗时)")
plt.tight_layout()
plt.savefig("gemm_pipeline_timeline.png", dpi=150)
print("已保存 gemm_pipeline_timeline.png")
```

**操作步骤**：

1. 把示例代码存为 `pipeline_timeline.py` 并运行（`pip install matplotlib` 后 `python pipeline_timeline.py`）。
2. 核对控制台输出与图中甘特条：串行版任何时刻只有一条泳道有方块；流水线版（默认双缓冲）中 `mma(k)` 与 `epi(k-1)` 重叠、`mma(k)` 与 `load(k+1)` 重叠。
3. 检查三处信号标注的落点：①②分别落在 `mma(k)`、`epi(k)` 方块的左边界；③落在 `epi(k-1)` 结束（也就是 `load(k+1)` 开工）的边界——双缓冲下 `load(k+1)` 必须等这块缓冲被腾空。
4. 先修改 `T` 字典（例如把 `"load"` 改成 8），重跑观察：稳态间隔由 `max(T.values())` 决定，瓶颈换人了吗？再把 `STAGE_DEPTH` 从 2 改成 3 重跑，观察总时间与「三引擎同时忙碌」窗口的变化。

**预期结果**（按默认参数 4/6/3 µs、`STAGE_DEPTH=2`）：

- 串行总时间 = 3 × (4+6+3) = **39 µs**，任何时刻仅一个引擎忙碌；
- 流水线总时间 = **26 µs**，稳态每 tile 间隔趋向 \( \max = 6 \) µs（个别 tile 受双缓冲复用约束多等 1 µs）；
- 三处信号分别出现在 t=10（`k` 的 A/B 字节到齐，`mma(k)` 开工）、t=16（`mma(k)` 完成信号，`epi(k)` 开工）、t=13（`epi(k-1)` 完成，`load(k+1)` 才能复用该 stage）；
- 把 `STAGE_DEPTH` 改为 3：总时间降为 **25 µs**，且在 t∈[10,12] 出现「TMA 搬 `k+1`、Tensor Core 算 `k`、epilogue 处理 `k-1`」三引擎同忙的窗口——多一块 SMEM 缓冲换来更完整的重叠。这正是单元十二 GEMM Step 5 在 `PIPE_DEPTH` 与 B200 每 SM 228 KB SMEM 预算之间做取舍的预演。

以上数值为脚本推演结果；脚本为纯计算、无随机性，你在本机运行的输出应与上述数字一致。

**延伸（可选）**：翻阅仓库自带的 [img/scripts/gen_memory_dataflow.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_memory_dataflow.py)（生成 GEMM 章的 memory dataflow 插图），对照它如何用 matplotlib 表达「数据在存储层级间流动」，吸收其配色与标注风格。

## 6. 本讲小结

- 一个 SM 内有三类各司其职的引擎：**CUDA Core**（标量/向量/控制流）、**Tensor Core**（tile 级 \( D=AB+C \)，吞吐高一个量级）、**TMA 引擎**（异步整块搬运）；高性能内核的第一原则是把矩阵乘交给 Tensor Core、杂务交给 CUDA Core、搬运交给 TMA。
- 一个 GEMM tile 的生命期分三段：**Load**（TMA：GMEM→SMEM，靠预期字节计数判定完成）、**Compute**（tcgen05：SMEM→TMEM 累加，靠硬件完成信号）、**Epilogue**（TMEM→寄存器→SMEM→GMEM，含 dtype 转换与 TMA store）。
- 数据依赖只存在于同一 tile 内部：Tensor Core 算 tile `k` 时，TMA 可以搬 `k+1`，epilogue 可以处理 `k-1`——重叠不违反依赖，反而让三个引擎同时忙碌。
- 串行内核的时间约为 \( N \times (T_{\text{load}}+T_{\text{mma}}+T_{\text{epi}}) \)，流水线稳态每 tile 间隔收敛到 \( \max(T_{\text{load}}, T_{\text{mma}}, T_{\text{epi}}) \)，瓶颈段决定吞吐。
- 每个阶段交接处都需要完成信号：Load 的字节计数 barrier、MMA 完成 barrier、store 完成后的 stage 复用信号；barrier 与 phase 模型是后续全部 GEMM 优化的基础设施。

## 7. 下一步学习建议

本讲完成了「理解硬件」部分（原书 Part I 的执行模型主线）最后一块拼图。建议路线：

1. **下一讲 u3-l1（Roofline 模型与算术强度）**：把本讲的定性结论定量化——学会计算一个内核的算术强度，判断它到底该「喂饱 Tensor Core」还是「省带宽」，为优化排序提供依据。
2. **先行阅读（可选）**：原书性能一章 [chapter_performance/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md) 的开头，带着「串行为什么慢」的问题去读会非常顺。
3. **远期预告**：本讲刻意留下的三个问号各有专讲——TMA 的描述符与完成机制在单元六（chapter_tma），mbarrier 与 phase 相位在单元八（chapter_async_barriers），而三段流水线真正落地成代码，是单元十一/十二的 GEMM Step 1–5：从单 tile 同步内核起步，逐步加入 K 循环、TMA 加载、双缓冲流水线，把本讲的时序图一行行变成 TIRx 内核。
