# 第 4 讲：线程执行层级：thread 到 cluster

## 1. 本讲目标

从本讲起，我们正式进入 Part I（理解硬件）的第一个主题。读完本讲，你应该能够：

- 说出 GPU 的**六级执行层级**——thread、warp、warpgroup、CTA、cluster、grid——每一级的**规模**（多少线程）与**协作能力**（能共享什么、能互相通信什么）。
- 解释 **SIMT**（single instruction, multiple threads，单指令多线程）执行模型：一个 warp 的 32 个 lane 如何"一起发指令、各拿各的数据、各走各的分支"。
- 对照 Blackwell **SM（Streaming Multiprocessor，流式多处理器）** 内部的硬件单元——CUDA core、Tensor Core、SMEM、TMEM、寄存器堆、TMA 引擎——说明每个单元在数据通路上的角色。
- 建立**操作 scope（作用范围）**概念：一个操作由**哪个层级的线程发起**、**覆盖哪些线程**。这是理解后续 TIRx 三要素之一 scope 的硬件基础。
- 亲手产出一分**四行「操作—发起者—协作范围」表格**，并用章节原文与书站交互演示逐条核对。

本讲是纯硬件概念课：不需要 GPU、不需要安装 TVM，所有实践都可以在浏览器和源码里完成。如果你在 u1-l2 中已本地构建过书站，本讲的实践体验会更顺畅。

## 2. 前置知识

本讲需要的背景很少，以下术语用通俗语言解释：

- **程序计数器（PC，program counter）**：CPU/GPU 里记录"下一条要执行的指令在哪"的寄存器。说"每个线程有自己的 PC"，就是在说每个线程可以独立地走到程序的不同位置。
- **SIMD vs SIMT**：SIMD（single instruction, multiple data，单指令多数据）是 CPU 向量指令的风格——一条指令明确地对一组数据做同样的事。SIMT 是 GPU 的变体——一组线程**各自持有自己的寄存器和 PC**，由硬件保证它们**同步发射同一条指令**，但每个线程可以有自己的地址、自己的数据，甚至被单独"掩蔽"（mask）掉。一句话：SIMD 是"一条指令操作一个向量"，SIMT 是"一组独立线程碰巧一起走"。
- **分支发散（branch divergence）**：同一个 warp 里的线程如果走进不同的 if 分支，硬件无法同时发射两条指令，只能先掩蔽一部分线程执行分支 A，再掩蔽另一部分执行分支 B，串行走完。这是 SIMT 的代价，也是理解"为什么 warp 内最好走相同分支"的关键。
- **CUDA 的 thread block / grid 术语**：如果你写过 CUDA，thread block 就是本讲的 CTA，grid 就是本讲的 grid；warpgroup 和 cluster 是 Hopper/Blackwell 世代加入的中间层级。没写过 CUDA 也不影响——本讲会从零定义。
- **DMA（direct memory access，直接内存访问）**：由专用搬运引擎（而不是 CPU/线程亲自循环拷贝）完成的数据传输。本讲的 TMA 引擎就是 GPU 上的 DMA 引擎，这个概念会在模块三反复出现。

与本手册前几讲的衔接：u1-l1 说过本书主线是"理解硬件 → 学会编程 → 写出 SOTA 内核"，本讲是"理解硬件"的第一块基石；u1-l3 的环境课与本讲无关，无 GPU 的读者完全不受影响。

## 3. 本讲源码地图

本仓库是"教材即代码"（见 u1-l1 的说明），本讲的"源码"是书稿正文与两个交互演示页面：

| 文件 | 作用 | 本讲用途 |
| --- | --- | --- |
| [chapter_background/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md) | 《GPU Execution Model》章正文：执行层级、内存空间、计算引擎、GEMM 流水线四节 | 精读执行层级一节（L31-L65）与 SM 架构图引文（L19-L29）；内存空间与流水线留给 u2-l2 / u2-l3 |
| [_extra/demo/thread_hierarchy.html](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/thread_hierarchy.html) | 六级线程层级的嵌套框图交互演示：点击任一级显示该级定义 | 模块一（线程层级）的核对材料；每级的一句话定义就藏在它的 JS 数据里 |
| [_extra/demo/sm_architecture.html](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html) | Blackwell SM 硬件单元框图：点击单元显示说明，并绘制加载/回写两条数据通路箭头 | 模块二（SM 架构组件）与模块三（scope）的核对材料 |
| [_extra/viz-base.js](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/viz-base.js) / viz-base.css | 全部演示共用的公共样式与脚本库（u1-l2 已介绍 html_extra_path 拷贝机制） | 只需知道：演示是自包含的，无需服务器逻辑 |

> 打开演示的两种方式（u1-l2 已讲过构建流程）：① 本地构建后访问 `_build/html/demo/thread_hierarchy.html`；② 直接用浏览器打开克隆仓库里的 `_extra/demo/thread_hierarchy.html`——两个演示只以相对路径引用同目录上层的 `viz-base.css/js`，离线双击即可交互。

## 4. 核心概念与源码讲解

本讲的三个最小模块按"**人怎么组织**（线程层级）→ **房子里有什么**（SM 组件）→ **活儿怎么派**（操作 scope）"的顺序展开。

### 4.1 模块一：六级线程执行层级

#### 4.1.1 概念说明

一颗 GPU 动辄同时跑几十万个线程，但它**不是把线程当一个扁平的大池子管理**，而是逐级打包：每打包一级，就获得一种新的**协作能力**。章节正文开头就说得很直白：

> A GPU does not manage thousands of threads as one flat collection. Instead, it organizes them into several levels, each with a different cooperation granularity.
> （GPU 不会把成千上万个线程当作一个扁平集合来管理，而是把它们组织成若干层级，每级对应不同的协作粒度。）

先把六级一次列全（规模与能力是本讲必须记住的硬知识）：

| 层级 | 规模 | 关键协作能力 | 一句话记忆 |
| --- | --- | --- | --- |
| **thread** | 1 个标量执行单元 | 自己的 PC、自己的寄存器，用 warp 内 **lane ID** 标识 | 最小的"一个人" |
| **warp** | **32** 个线程 | **SIMT** 同步发射同一指令；每 lane 可独立掩蔽 | 最小的"一起干活"单位 |
| **warpgroup** | **4** 个连续 warp = **128** 线程 | Hopper 起作为 `wgmma` 的发起单位；Blackwell 上四个 warp 恰好覆盖 TMEM 的四个 32-lane 窗口 | Tensor Core 时代的"一个班组" |
| **CTA** | 若干 warpgroup（常见 128/256/384 线程） | 调度到**单个 SM** 上运行，**独占一份私有 SMEM**；同 SM 可驻留多个 CTA 分摊 SMEM | "一个工位" |
| **cluster** | 一组协作 CTA（可跨 SM） | CTA 之间可互相同步、可读写彼此 SMEM（**DSMEM**，distributed shared memory） | "跨工位协作组" |
| **grid** | 一次 kernel launch 的全部线程 | 按 cluster（或直接按 CTA）组织，把问题的 tile 映射上去 | "整个工地" |

几个容易混淆的点，先在这里钉死：

- **lane ID 是 warp 内的编号**（0–31），不是 CTA 内编号。跨 warp 定位一个线程要"哪个 warp + 哪个 lane"，这正是后续 TMEM 窗口、寄存器 fragment 布局反复用的坐标记号（u4-l2 会正式展开）。
- **warpgroup 不是 CUDA 的传统概念**，它是 Hopper 引入的：四条连续 warp 捆在一起，成为 `wgmma` 的发起单位；到 Blackwell，这个 128 线程的规模又和 TMEM 的 128 条 lane 天然对齐（模块三详述）。
- **CTA 是硬件调度的基本单位**：一个 CTA 整体住进一个 SM，不会拆到两个 SM。多个 CTA 可以同时住在同一个 SM，此时它们**瓜分**这个 SM 的 SMEM 容量。
- **cluster 里的 CTA 可以在不同 SM 上**。cluster 之所以重要，是因为它解锁了两样 GEMM 利器：2-CTA 协作 MMA 与 TMA multicast（本讲先记住名字，u2-l2/u13 展开）。

#### 4.1.2 核心流程

六级的**包含关系**是一棵树（这也是交互演示画出来的形状）：

```text
grid（一次 kernel launch）
 └─ cluster（跨 SM 的协作 CTA 组，组内可经 DSMEM 互访 SMEM）
     ├─ CTA 0（整个住进一个 SM，私有 SMEM）      ├─ CTA 1（另一个 SM）
     │   ├─ warpgroup 0 = warp 0..3（128 线程）   │   └─ …
     │   │   ├─ warp 0 = lane 0..31（SIMT 同步）  │
     │   │   │   └─ thread(lane 0..31)           │
     │   │   └─ warp 1..3                        │
     │   └─ warpgroup 1..N                       │
     └─ …
```

规模换算只需两个常数：warp = 32 线程，warpgroup = 4 warp = 128 线程。例如某 CTA 含 8 个 warpgroup，就是 8 × 128 = 1024 线程、256 warp。

**从层级到问题映射**的心智模型（为 u11 的 GEMM 空间分块埋伏笔）：

```text
输出矩阵 D 的每个 tile  ←→  一个 CTA（或一个 2-CTA cluster）
tile 内的数据搬运与计算 ←→  CTA 内的 warp / warpgroup 分工
```

#### 4.1.3 源码精读

**（1）正文对六级的逐条定义。** 章节正文用五条列表给出定义，位于 [chapter_background/index.md:L43-L57](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L43-L57)。逐条对照（英文为原文，括号内为要点解读）：

- **Thread**（[L43-L44](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L43-L44)）：标量执行单元，自有 PC 与寄存器，以 warp 内 lane ID 标识。
- **Warp**（[L45-L47](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L45-L47)）：32 线程 SIMT 执行——"lanes issue the same instruction together, yet each keeps its own registers and can be masked off on its own"（各 lane 一起发射同一指令，但各自持有寄存器、可被单独掩蔽），这正是 4.2.1 里分支发散机制的原文依据。
- **Warpgroup**（[L48-L50](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L48-L50)）：四个连续 warp、128 线程；Hopper 引入它作为 `wgmma` 的发起单位，Blackwell 上它的四个 warp 还能覆盖 TMEM 的四个 32-lane 窗口。
- **CTA**（[L51-L54](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L51-L54)）：即 CUDA 的 thread block；硬件调度的基本单位，跑在单个 SM 上、拥有该 SM 内一份私有 SMEM；同 SM 的多个驻留 CTA 分摊 SMEM。
- **Cluster**（[L55-L57](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L55-L57)）：一组可跨 SM 的协作 CTA；可互相同步、可读写彼此 SMEM（DSMEM）。

> 注意正文列表只写了五条（thread/warp/warpgroup/CTA/cluster），**grid 由交互演示与图注补全**——阅读英文教材时要习惯"正文 + 图"互补的信息分布。

**（2）交互演示：嵌套结构。** 演示页面 [thread_hierarchy.html:L50-L75](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/thread_hierarchy.html#L50-L75) 用嵌套的圆角框把这棵包含树画了出来，每个框的标签就是该级的"身份卡"：

```html
<div class="lvl lvl-grid" data-k="grid">
  <span class="tag">GRID — one kernel launch</span>
  <div class="lvl lvl-cluster" data-k="cluster">
    <span class="tag">CLUSTER — CTAs across SMs (DSMEM)</span>
    ...
      <div class="lvl lvl-cta" data-k="cta">
        <span class="tag">CTA — thread block · one SM</span>
        <div class="lvl lvl-wg" data-k="wg">
          <span class="tag">WARPGROUP — 4 warps · 128 threads</span>
          <div class="warprow">
            <div class="lvl lvl-warp" data-k="warp">
              <span class="tag">WARP — 32 threads (SIMT)</span>
              <div class="threads" id="threads" data-k="thread"></div>
```

要点：HTML 的**嵌套层次本身就是层级的包含关系**（示例代码位置如上，框图标签为原文）；`warprow` 里的 warp 1/2/3 与 CTA 1 画成虚线幽灵框（[L65-L67](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/thread_hierarchy.html#L65-L67)、[L71](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/thread_hierarchy.html#L71)），表示"省略未画"，避免图爆炸。

**（3）交互演示：每级的官方一句话定义。** 点击某个框时，页面从 `INFO` 字典里取文字填进下方说明面板，见 [thread_hierarchy.html:L84-L91](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/thread_hierarchy.html#L84-L91)。这几行英文短句是对正文最好的浓缩，其中两条信息**正文里没有、只在演示里出现**：

- grid（[L90](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/thread_hierarchy.html#L90)）：一次 kernel launch 的全部线程，组织成 cluster（或直接 CTA），"Tiles of the problem are mapped onto CTAs/clusters"（问题的 tile 被映射到 CTA/cluster 上）。
- cluster（[L89](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/thread_hierarchy.html#L89)）：除 DSMEM 外，还点名了 Blackwell 新增的动态调度（CLC）与 2-CTA 协作 MMA——它们分别在 u8-l3 与 u13-l2 展开，此处先"认脸"。

另外两处实现细节值得一提：脚本在 [L94](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/thread_hierarchy.html#L94) 用循环生成 32 个小格表示 warp 里的 32 个 lane；[L108](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/thread_hierarchy.html#L108) 的注释 `select('wg'); // default: the key Hopper/Blackwell unit` 说明作者把 **warpgroup 选作默认高亮**——因为它正是 Hopper/Blackwell 上承上启下的关键单位，本讲模块三会反复回到它。

#### 4.1.4 代码实践

**实践 1：把六级定义"点"出来。**

1. **目标**：用交互演示核对 4.1.1 的表格，并亲眼确认包含树的形状。
2. **步骤**：
   - 用浏览器打开 `thread_hierarchy.html`（方式见第 3 节的说明）；
   - 依次点击 GRID → CLUSTER → CTA → WARPGROUP → WARP → THREAD 六个框（点最里层的 32 个红色小格之一就是 thread）；
   - 每点一级，把下方说明面板里的英文原句抄进自己的笔记，并在旁边用中文写一句自己的话。
3. **观察现象**：点击时选中框会出现高亮描边（[L97-L103](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/thread_hierarchy.html#L97-L103) 的 `select` 函数做选中与填字）；初始状态默认选中的是 warpgroup。
4. **预期结果**：六条笔记与 4.1.1 表格的"一句话记忆"列一一对应；特别确认 thread 条目里出现 **lane ID**、warp 条目里出现 **SIMT**、warpgroup 条目里出现 **wgmma 与 Tensor Memory**、cluster 条目里出现 **DSMEM**。
5. 本实践纯浏览器操作，必然可完成，无需"待本地验证"标注。

#### 4.1.5 小练习与答案

**练习 1**：一个 CTA 配置了 2 个 warpgroup。它一共有多少线程、多少个 warp？一个具体线程的"身份证"需要哪几个量？

**答案**：2 × 128 = **256 线程**，2 × 4 = **8 个 warp**。身份证 =（CTA 内的 warpgroup 编号 0–1，warp 在 warpgroup 内编号 0–3，lane ID 0–31）三级；对应到硬件就是（warp id, lane id）。这也解释了为什么 lane ID 单独拿出来不够定位线程。

**练习 2**：warp 0 和 warp 1 分别走进 if 和 else 分支，会发生什么？同一 warp 内 10 个线程走 if、22 个线程走 else，又会发生什么？

**答案**：前者**没有任何问题**——不同 warp 是独立的调度单位，各走各的。后者触发**分支发散**：硬件无法对同一 warp 同时发射两条指令，只能先掩蔽 22 个 lane 执行 if 分支、再掩蔽 10 个 lane 执行 else 分支，两段串行完成，该 warp 在这段代码里的有效并行度降了一半。依据是正文 [L45-L47](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L45-L47)：warp 的 lane 可 "be masked off on its own"。

**练习 3**：判断正误：① cluster 中的 CTA 共享同一块 SMEM；② 同一 SM 上的两个 CTA 共享 SMEM。

**答案**：两者都**错**。① cluster 内每个 CTA 仍各自拥有私有 SMEM，"共享"指的是**可以通过 DSMEM 访问对方的 SMEM**（正文 [L114-L116](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L114-L116) 明确说 sharing 并不合并两块 SMEM 分配）；② 同 SM 的驻留 CTA 是**分摊容量**的竞争关系（[L51-L54](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L51-L54)），互相不可见对方 SMEM。

### 4.2 模块二：SIMT 执行模型与 SM 架构组件

#### 4.2.1 概念说明

模块一回答了"线程怎么编队"，本模块回答"编好队的线程住在什么房子里、用什么工具干活"。房子就是 **SM（Streaming Multiprocessor，流式多处理器）**——GPU 由 N 个 SM 组成（B200 上是 148 个，u11-l4 会用到这个数），CTA 被调度进去住下。

先固化 SIMT（它是理解一切 GPU 执行行为的第一性原理）：

- **发射维度**：以 warp 为单位发射指令，32 个 lane **同一时刻执行同一条指令**；
- **数据维度**：每个 lane 用**自己的寄存器**里的地址和数据，所以同一条指令可以处理 32 份不同的数据；
- **控制维度**：lane 可被逐个掩蔽，于是允许 warp 内部分歧（代价见练习 2）；
- **推论**：GPU 的"几千个线程"在发射层面从来不是几千条独立指令流，而是"每 32 个一组、组内锁步"的大量 warp。

SM 内部，本讲需要认得**六种硬件单元**（名称与说明直接来自交互演示，见 4.2.3）：

| 单元 | 类别 | 职责（一句话） |
| --- | --- | --- |
| **CUDA Core** | 计算 | 通用 SIMT 标量 ALU：地址计算、控制流、逐元素数学、类型转换（如 fp32→fp16） |
| **Tensor Core（tcgen05）** | 计算 | 固定功能矩阵乘累加单元，按 **tile** 粒度一条指令算 \( D = AB + C \) |
| **SMEM（Shared Memory）** | 存储 | CTA 内共享的片上低延迟暂存区（B200 每 SM 最多 228 KB），TMA 装载与回写的中转站 |
| **TMEM（Tensor Memory）** | 存储 | Blackwell 新增，128 lane × 若干 32-bit 列的二维片上存储，专放 MMA 累加器 |
| **Register File（RF）** | 存储 | 每线程私有的最快存储，放标量与每线程的 tile fragment |
| **TMA 引擎** | 搬运 | 专用 DMA 引擎，在 HBM 与 SMEM 之间整块搬运 tensor tile |

其中 CUDA core 与 Tensor core 的分工，正文 [L127-L135](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L127-L135) 有精确表述：CUDA core 跑标量/向量指令（索引算术、逐元素、规约、控制流），Tensor core 是固定功能单元、以 tile 粒度算 \( D = AB + C \)，吞吐常高出 CUDA core 10 倍以上——这是"为什么要费尽心思把数据喂给 Tensor Core"的根本原因。存储三兄弟（SMEM/TMEM/RF）的容量-延迟-作用域权衡是**下一讲 u2-l2** 的主角，本讲只需认脸。

#### 4.2.2 核心流程

把六个单元连起来，就是一条 GEMM 数据通路。交互演示用**实线箭头画加载路径、虚线箭头画回写路径**：

```text
加载路径（load path，实线）：
  GMEM(HBM) ──> TMA 引擎 ──> SMEM ──> Tensor Core(tcgen05) ──> TMEM ──> RF
             cp.async.bulk   暂存      tcgen05.mma           累加    tcgen05.ld

回写路径（store path，虚线）：
  RF ──> SMEM ──> TMA 引擎 ──> GMEM(HBM)
       类型转换后暂存     cp.async.bulk
```

三个"引擎各干各的"要点（重叠执行的雏形，u2-l3 展开）：

1. **搬运归 TMA**：线程只负责"下订单"，地址计算与 swizzle 由硬件完成；
2. **矩阵乘归 Tensor Core**：一条 `tcgen05.mma` 指令处理一整个 tile，结果直接累加进 TMEM，不占寄存器；
3. **杂活归 CUDA core**：地址算术、循环控制、fp32→fp16 转换，操作对象在寄存器里。

#### 4.2.3 源码精读

**（1）正文如何嵌入这张图。** 章节正文在 [chapter_background/index.md:L19-L29](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L19-L29) 用 `{raw} html` 指令把 `sm_architecture.html` 以 iframe 嵌进书页，并配图注 "Click a component to inspect the warps, warpgroups, shared memory, Tensor Memory, Tensor Cores, and TMA engine inside a Blackwell SM"（点击组件可检视 Blackwell SM 内的 warp、warpgroup、SMEM、TMEM、Tensor Core 与 TMA 引擎）。u1-l2 讲过的 `html_extra_path` 机制保证这个相对路径在构建后的站点里依然可用。

**（2）演示里的硬件单元清单。** [sm_architecture.html:L134-L170](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html#L134-L170) 按三行摆放单元：计算行（Tensor Core tcgen05 "5th-gen MMA"、CUDA Core "FP/INT units"）、存储行（SMEM "228 KB per SM"、TMEM "TMEM — 128 lanes"、Register File）、搬运行（TMA Engine "Data Mover"）；SM 框外画着 HBM（GMEM）与 "SM × N" 的幽灵框（[L172-L181](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html#L172-L181)）——提醒读者：GMEM 不在 SM 里，SM 只是通过 TMA 与之相连。

**（3）点击单元后显示的六段官方说明。** 演示的全部文案集中在 `info` 字典 [sm_architecture.html:L238-L269](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html#L238-L269)，逐条摘译（这些句子同时是模块三 scope 分析的论据）：

- **tma**（[L239-L243](https://github.com/mlc-ai/modern-gpu-programming-for-mlsms/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html#L239-L243)）：在 HBM 与 SMEM 间拷贝 tensor tile 的专用 DMA 引擎；"Only 1 thread dispatches; HW handles address calculation and swizzling"，对应指令 `cp.async.bulk.tensor`。
- **tc**（[L244-L248](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html#L244-L248)）：从 SMEM 读 A、B，累加进 TMEM；一条指令支持 128×256 tile；"Single thread issues MMA; HW distributes across 128 lanes"，对应 `tcgen05.mma`、`tcgen05.commit`。
- **smem**（[L249-L253](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html#L249-L253)）：CTA 全体线程共享的片上暂存区，是 TMA load（HBM→SMEM）与 TMA store（SMEM→HBM）的中转；swizzle 布局用于避免 bank conflict。
- **tmem**（[L254-L258](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html#L254-L258)）：SM 私有的硬件管理存储，128 lane × N 列 fp32；tcgen05 MMA 在此累加，"no register pressure"；`tcgen05.ld` 把数据读出去做回写。
- **rf**（[L259-L263](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html#L259-L263)）：每线程私有；回写路径为 TMEM → 寄存器（`tcgen05.ld`，fp32）→ 转 fp32→fp16 → 存 SMEM → TMA 写回 HBM。
- **cuda**（[L264-L268](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html#L264-L268)）：通用计算单元，处理 fp32→fp16 转换、地址算术、循环控制与回写时的数据重排，操作寄存器中的数据。

**（4）数据通路箭头的实现。** 演示用 SVG 箭头把 4.2.2 的流程画出来，`positionArrows` 函数（[sm_architecture.html:L303-L368](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html#L303-L368)）按八个箭头逐一连线并打标签：HBM→TMA、TMA→SMEM（标签 `cp.async.bulk`）、SMEM→TC（标签 `tcgen05.mma`）、TC→TMEM（标签 `accumulate`）、TMEM→RF（标签 `tcgen05.ld`）、RF→SMEM（标签 `store`）、SMEM→TMA、TMA→HBM（[L331-L367](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html#L331-L367)）。把这些标签串起来读，正好得到一条带指令名的完整数据链——建议把这条链抄进笔记，它是后续 GEMM 九步（u11–u13）每一步的物理背景。

#### 4.2.4 代码实践

**实践 2：走一遍 SM 数据通路。**

1. **目标**：把 4.2.2 的文字流程图与演示的可视化箭头互相验证。
2. **步骤**：
   - 打开 `sm_architecture.html`，依次点击 Tensor Core、CUDA Core、SMEM、TMEM、Register File、TMA Engine 六个单元；
   - 每次点击观察两处变化：下方说明面板（[L278-L292](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html#L278-L292) 的点击处理器填充 `info[key]`）与图上的通路箭头；
   - 最后对照面板初始提示（[L231](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html#L231)）："Solid arrows = load path, dashed arrows = store path"（实线为加载路径，虚线为回写路径）。
3. **观察现象**：任一单元被选中时，面板边框颜色会变成该单元的主题色；两条通路箭头常驻显示，不需要点选也可见。
4. **预期结果**：能在图上用手指沿"实线五步"（HBM→TMA→SMEM→TC→TMEM→RF）与"虚线三步"（RF→SMEM→TMA→HBM）各走一遍，并说出每段箭头上的指令标签。
5. 本实践纯浏览器操作，必然可完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么说"GPU 上的 for 循环里每个线程算自己的下标"和 SIMT 不矛盾？

**答案**：SIMT 锁步的是**指令**，不是**数据**。所有 lane 同时执行"load A[idx]"这条指令，但 `idx` 存在各自寄存器里、值各不相同，于是同一指令取回了 32 份不同数据。这正是 [L45-L47](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L45-L47) "issue the same instruction together, yet each keeps its own registers" 的含义。

**练习 2**：fp32 累加结果转 fp16 再写回 GMEM，这一步由哪个单元完成？数据此刻在哪里？

**答案**：由 **CUDA Core** 完成（演示 cuda 条目：fp32→fp16 casts），操作对象在**寄存器**里——数据先经 `tcgen05.ld` 从 TMEM 读回 RF，转换后再存入 SMEM、交给 TMA 写回 HBM。这也是 4.2.3（4）中 TMEM→RF→SMEM→TMA→HBM 一串标签的含义。

**练习 3**：TMEM 的出现解决了什么问题？

**答案**：在 Blackwell 之前的架构上，MMA 累加器放在寄存器里；随着 MMA tile 变大，累加器会吃掉寄存器堆的一大块。Blackwell 的 `tcgen05` 把累加器写进 TMEM（128 lane × 最多 512 列 × 32-bit 的独立片上存储），降低了寄存器压力（正文 [L80-L87](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L80-L87)）。TMEM 的精确结构、分配与读写是 u2-l2 与 u7-l3 的主题。

### 4.3 模块三：操作 scope——谁发起、谁执行

#### 4.3.1 概念说明

本模块是全讲的落点，也是后续所有硬件课与 TIRx 课的枢纽概念。

传统 CUDA 直觉是"所有线程都在跑同一份内核代码，谁都能干任何事"。但 Blackwell 的关键操作**各有各的自然参与范围**——它们不由同一组线程发起，也不在同一层协作。看正文的关键一段（[chapter_background/index.md:L59-L62](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L59-L62)）：

> Blackwell's key operations are not all issued by the same group of threads. A single thread launches a TMA copy, which the hardware then executes. Each warp issues warp-level TMEM loads for its own 32-lane window. One designated thread commits a `tcgen05` MMA, while a 2-CTA cooperative MMA spans two CTAs.
> （Blackwell 的关键操作并非由同一组线程发起：单个线程发起 TMA 拷贝、由硬件执行；每个 warp 为自己的 32-lane 窗口发起 warp 级 TMEM 读；一个指定线程提交 `tcgen05` MMA；而 2-CTA 协作 MMA 跨两个 CTA。）

紧接着正文给出定义（[L64-L65](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L64-L65)）：

> We call the set of threads involved in an operation its **scope**. Analyzing a kernel requires considering the operation's scope together with its data layout and dispatch mechanism.
> （我们把一个操作所涉及的线程集合称为它的 scope。分析内核时，要把操作的 scope 与数据布局、派发机制放在一起考虑。）

这段话有三层含义，分别对应现在与未来：

1. **scope = 参与线程的集合**。注意"参与"包含两个不同角色：**发起者**（issuer，谁执行那条触发指令）与**受益/协作范围**（该操作覆盖哪些线程的数据或执行）。两者经常不同——最典型的是 TMA：发起者只有 1 个线程，数据却供整个 CTA 使用。
2. **发起 ≠ 执行**。TMA 拷贝由单线程"下单"，实际搬运算力和地址计算在 TMA 引擎；MMA 由单线程 commit，计算在 Tensor Core 并由硬件分发到 128 条 lane。这是"异步"的硬件根源。
3. **scope 只是三要素之一**。原文点名分析内核需要 scope + data layout（数据布局）+ dispatch mechanism（派发机制）三者合看——这正是 u9-l3 将正式讲解的 TIRx 三要素，本讲先把"scope"这一维的地基打好。

为什么初学就必须建立 scope？因为后续章节的每一条关键指令都活在不同的 scope 上：`cp.async.bulk.tensor` 是 thread 级发起、CTA 级受益；`tcgen05.ld` 是 warp 级；`tcgen05.mma` 是 thread 级发起、（`cta_group::2` 时）cluster 内 CTA 对级协作。**读 Blackwell 内核的第一件事，就是给每条指令标注 scope**——这也是本讲主实践的任务。

#### 4.3.2 核心流程

把"操作 scope"的判断固化为三步法：

```text
给定一条操作，依次回答：
① 发起者是谁？—— 需要多少线程执行"触发指令"（1 个？1 个 warp？1 个 warpgroup？）
② 硬件在哪执行？—— CUDA core / Tensor Core / TMA 引擎（发起与执行分离吗？）
③ 协作范围多大？—— 结果或数据覆盖到哪一层（lane / warp / warpgroup / CTA / CTA 对 / grid）
（①+③ 即该操作的 scope；② 决定它是同步指令还是异步操作）
```

用它扫一遍本讲遇到的四个操作（详细核对在 4.3.4 实践中完成）：

| 操作 | ① 发起者 | ② 执行引擎 | ③ 协作范围 |
| --- | --- | --- | --- |
| TMA copy（`cp.async.bulk.tensor`） | 单个线程 | TMA 引擎（异步） | 整个 CTA 使用这份数据 |
| `tcgen05.mma` | 一个指定线程 | Tensor Core（异步），硬件分发到 128 lane | CTA（`cta_group::1`） |
| TMEM 累加器读回（`tcgen05.ld`） | 每个 warp 各自发起 | TMEM→RF 搬运通道 | 一个 warpgroup 的 4 个 warp 各覆盖一个 32-lane 窗口 |
| 2-CTA 协作 MMA（`cta_group::2`） | 一个指定线程 | Tensor Core（异步） | cluster 内一对 CTA |

#### 4.3.3 源码精读

**（1）总纲句（章 Overview）。** 章首 Overview 的第一条就把"层级 + scope"定为全章主题（[chapter_background/index.md:L7](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L7)）：线程六级"each correspond to a different scale of cooperation"（各自对应不同的协作规模），并预告三个 scope 实例——TMA 拷贝由单线程发起、完整 TMEM 累加器由四个 warp 在各自 32-lane 窗口上读回、2-CTA 协作 MMA 跨两个 CTA。这一句就是本讲实践表格的"标准答案出处"。

**（2）TMEM 读回为什么恰好是"四个 warp"。** 正文在内存空间一节给出量化说明（[chapter_background/index.md:L89-L91](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L89-L91)）：TMEM 逻辑上属于 CTA、共 **128 行（lane）**；内核必须显式分配/释放 TMEM，epilogue 必须显式把累加器读回寄存器——"To read a full 128-lane accumulator, the four warps in a warpgroup each load their own 32-lane TMEM window"（要读满 128-lane 累加器，warpgroup 里的四个 warp 各自加载自己的 32-lane TMEM 窗口）。数字对得很工整：4 warp × 32 lane = 128 lane = TMEM 行数 = warpgroup 线程数。这不是巧合，而是 Blackwell 把 warpgroup 与 TMEM 物理结构对齐的设计（演示 thread_hierarchy 的 wg 条目也说 "128 threads move a TMEM tile together"，见 [thread_hierarchy.html:L87](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/thread_hierarchy.html#L87)）。

**（3）演示文案里的 scope 证据。** 模块二摘译过的两段话，在这里换个角度再读一次——它们是 scope 表格第 ① 列的直接出处：

- TMA：[sm_architecture.html:L242](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html#L242) ——"Only 1 thread dispatches; HW handles address calculation and swizzling"（仅 1 个线程派发；地址计算与 swizzle 由硬件完成）。
- Tensor Core：[sm_architecture.html:L247](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html#L247) ——"Single thread issues MMA; HW distributes across 128 lanes"（单线程发起 MMA；硬件分发到 128 条 lane）。

**（4）2-CTA 协作 MMA 的落点。** 正文 [L118-L119](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L118-L119)：两个 CTA 可以组成 CTA pair，以 `cta_group::2` 模式执行协作 MMA，产出更大的输出 tile；[L142-L146](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L142-L146) 进一步指出 cluster 带来的两种 GEMM 协作形态：2-CTA 协作 MMA（两 CTA 各出一部分 SMEM 操作数）与 TMA multicast（一次 GMEM 读把同一 tile 送给多个 CTA）。本讲只需记住：**这俩操作的 scope 是"CTA 对"，是六级层级里 cluster 一层的红利**。

#### 4.3.4 代码实践

**实践 3（本讲主实践）：四行「操作—发起者—协作范围」表格。**

1. **目标**：独立填写 TMA copy、`tcgen05.mma`、TMEM 累加器读回、2-CTA 协作 MMA 四个操作的 scope 表格，再用章节原文与交互演示逐行核对。
2. **操作步骤**：
   - **先闭卷填表**：不看任何材料，凭 4.3.2 的三步法写出下面表格的每一格；
     ```text
     | 操作 | 发起者（issuer） | 协作范围（scope） | 原文依据（文件:行号） |
     |------|------------------|-------------------|------------------------|
     | TMA copy                 | （自填） | （自填） | （自填） |
     | tcgen05.mma              | （自填） | （自填） | （自填） |
     | TMEM 累加器读回 tcgen05.ld | （自填） | （自填） | （自填） |
     | 2-CTA 协作 MMA            | （自填） | （自填） | （自填） |
     ```
   - **再开卷核对**，核对材料清单（都在本讲引用过的源码里）：
     - TMA copy → 正文 [L59-L60](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L59-L60)（"A single thread launches a TMA copy"）+ 演示 [sm_architecture.html:L242](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html#L242)；
     - tcgen05.mma → 正文 [L60-L62](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L60-L62)（"One designated thread commits a tcgen05 MMA"）+ 演示 [L247](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html#L247)；
     - TMEM 读回 → 正文 [L89-L91](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L89-L91)（四 warp 各读 32-lane 窗口）+ 演示 tmem 条目 [L257](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html#L257)；
     - 2-CTA 协作 MMA → 正文 [L61-L62](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L61-L62)（"spans two CTAs"）+ [L118-L119](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L118-L119)（`cta_group::2`）；
   - **最后对照参考答案**（4.3.5 练习 1）。
3. **需要观察的现象**：核对时重点关注自己"发起者"与"协作范围"两列容易混淆的格子——最容易错的是把 TMA 的发起者写成"一个 CTA"（实际是 1 个线程），或把 TMEM 读回的发起者写成"一个线程"（实际是每个 warp 各自发起）。
4. **预期结果**：四行全部与参考答案一致，且每行都能指出至少一条原文出处（文件与行号）。
5. 本实践纯阅读与填写，必然可完成；无 GPU 也能做。

#### 4.3.5 小练习与答案

**练习 1**：给出实践 3 的参考答案表。

**答案**：

| 操作 | 发起者（issuer） | 协作范围（scope） | 原文依据 |
|------|------------------|-------------------|----------|
| TMA copy（`cp.async.bulk.tensor`） | **单个线程** | 数据落到 SMEM，供**整个 CTA**使用；搬运由 TMA 引擎异步执行 | 正文 L7、L59-L60；演示 sm_architecture L242 |
| `tcgen05.mma` | **一个指定线程**（single designated thread） | **CTA**（`cta_group::1`）；计算由 Tensor Core 执行并硬件分发到 128 lane，累加器进 TMEM | 正文 L60-L62；演示 L247 |
| TMEM 累加器读回（`tcgen05.ld`） | **每个 warp 各自发起**（warp 级指令） | 一个 **warpgroup**：4 个 warp 各负责一个 32-lane 窗口，合起来读满 128-lane 累加器 | 正文 L60-L61、L89-L91；thread_hierarchy L87 |
| 2-CTA 协作 MMA（`cta_group::2`） | **一个指定线程** | **cluster 内的一对 CTA**，两 CTA 各出部分 SMEM 操作数，产出更大输出 tile | 正文 L7、L61-L62、L118-L119、L142-L146 |

**练习 2**：`tcgen05.mma` 只由 1 个线程发起，那其它 127 个线程此刻在干什么？这个设计的意义是什么？

**答案**：其余线程不必闲着——发起 MMA 后它是一条异步指令，MMA 在 Tensor Core 上执行，其余 warp 可以同时去做别的事（例如准备下一份数据、做 epilogue）。意义正是**角色分工**：让少量线程当"指挥"（发起异步操作、跟踪完成信号），其余线程当"工人"，让搬运、计算、回写三类引擎同时忙碌。这就是 u2-l3 的 GEMM 流水线与 u13 的 warp 特化的硬件前提。

**练习 3**：把"操作 scope"与六级层级连线：`tcgen05.ld`（warp）、TMA copy 发起（thread）、DSMEM 互访（cluster）、CTA 内全体可用 SMEM（CTA）。再想一想：哪一层的"协作能力"被 `tcgen05.mma` 用到了？

**答案**：连线如题干括号所示。`tcgen05.mma` 用到的是 **CTA 层**（`cta_group::1` 时操作数与累加器都归一个 CTA；`cta_group::2` 时升到 CTA 对，即 cluster 层的一部分）。这说明同一个指令家族的 scope 还可以随执行模式改变——这正是 u7-l2（cta_group 与 block-scaled MMA）要展开的内容。

## 5. 综合实践

**综合任务：为一次具体的 kernel launch 画一张「层级 × 单元 × scope」三合一地图。**

假设你启动了一个书中风格的 GEMM 内核，launch 配置为：**grid = 4 个 cluster，每 cluster 2 个 CTA，每 CTA 4 个 warpgroup**（每 warpgroup 4 warp 不变）。请完成三问：

1. **层级换算**：这次 launch 总共多少 CTA、多少 warp、多少线程？一个 CTA 驻留进一个 SM 后，同一 SM 上还有别的 CTA 吗（依据 4.1.1 的 CTA 定义回答）？
2. **单元对号**：沿 `sm_architecture.html` 的数据通路（HBM→TMA→SMEM→TC→TMEM→RF，再虚线回写），为每一段标注：搬运/计算由哪个单元执行、对应演示箭头上的指令标签是什么（参考 4.2.3（4））。
3. **scope 标注**：对这条通路上的三个关键操作——TMA 加载、`tcgen05.mma`、`tcgen05.ld` 读回——各写一行「发起者 / 执行引擎 / 协作范围」，并注明本次 launch 配置下协作范围具体落在哪（例如 2-CTA 协作 MMA 的 CTA 对就来自同一个 cluster 的 2 个 CTA）。

**产出要求**：一张手绘或文本表格 + 一段 100 字左右的结论，说明"为什么 Blackwell 内核的分析必须同时看层级、单元、scope 三件事"。

**参考要点（做完后自查）**：

- 第 1 问：4 × 2 = 8 个 CTA；每 CTA 4 × 4 = 16 warp、4 × 128 = 512 线程；总计 128 warp、4096 线程。同一 SM 可以驻留多个 CTA，它们分摊 SMEM（正文 [L51-L54](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L51-L54)），所以"一 SM 一 CTA"不是必然。
- 第 2 问：指令标签依次为 `cp.async.bulk`（两处）、`tcgen05.mma`、`accumulate`、`tcgen05.ld`、`store`（见 [sm_architecture.html:L331-L367](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html#L331-L367)）。
- 第 3 问：与 4.3.5 练习 1 的表一致；结论应能落到"发起≠执行、scope 决定同步边界"这类要点上。
- 本任务无需 GPU；若想把配置换成书中真实内核的规模再算一遍，可待 u11（GEMM Step 3 的 grid 映射）学完后回来重做。

## 6. 本讲小结

- GPU 以六级层级组织线程：**thread**（标量执行、lane ID）→ **warp**（32 线程 SIMT 锁步、可逐 lane 掩蔽）→ **warpgroup**（4 warp/128 线程，Hopper `wgmma` 的发起单位，Blackwell 上对齐 TMEM 的 128 lane）→ **CTA**（硬件调度单位，住进单 SM、私有 SMEM）→ **cluster**（跨 SM 协作 CTA，DSMEM 互访）→ **grid**（一次 launch 全体线程）。
- SIMT = 同一指令 + 各自寄存器 + 逐 lane 掩蔽；它解释了 GPU 大规模并行的形态，也埋下了分支发散的代价。
- Blackwell SM 的六类单元各司其职：**CUDA core** 干杂活（地址、控制流、类型转换），**Tensor Core** 干矩阵乘，**SMEM/TMEM/RF** 三级存储各有作用域，**TMA 引擎**负责 HBM↔SMEM 的整块搬运；实线加载通路与虚线回写通路串成一条带指令标签的数据链。
- **操作 scope = 一个操作涉及的线程集合**：TMA copy 单线程发起、CTA 级受益；`tcgen05.mma` 单线程提交、硬件分发 128 lane；TMEM 累加器读回由 4 个 warp 各读 32-lane 窗口；2-CTA 协作 MMA 跨 CTA 对（`cta_group::2`）。发起与执行分离是异步的硬件根源。
- 分析内核 = scope + data layout + dispatch 三者合看——本讲打下了第一维，后两维在 u4 与 u9-l3 展开。
- 本讲两个交互演示（thread_hierarchy、sm_architecture）既是学习材料也是核对工具，所有英文文案都能在正文与演示源码中找到行号级出处。

## 7. 下一步学习建议

本讲回答了"线程怎么组织、住在哪、活儿怎么派"，下一讲 **u2-l2《内存空间：GMEM、SMEM、TMEM 与 DSMEM》** 顺着正文第二章深入：四种存储空间的容量-延迟-作用域权衡表、TMEM 的 128 lane × 512 列二维结构、以及 cluster 内 DSMEM 的数据流向。建议阅读 [chapter_background/index.md:L67-L119](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L67-L119)（内存空间一节全文）并打开 `cta_cluster.html` 演示预习。

更远的路线：u2-l3 把本讲的"单元"与"scope"串成三段式 GEMM 流水线（重叠执行的起点）；u4 的数据布局记号会把本讲的 lane/warp 坐标形式化；u7-l1 到 u7-l2 会把 `tcgen05.mma` 的指令细节补全；u9-l3 则把本讲的 scope 升级为 TIRx 三要素之一。带着本讲产出的四行 scope 表格继续走，后面每遇到一条新指令，都先问一句：它的发起者是谁、协作范围在哪一层。
