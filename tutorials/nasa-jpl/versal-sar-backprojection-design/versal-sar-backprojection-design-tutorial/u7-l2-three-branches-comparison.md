# 三个分支：main / host_stride / pl_stride

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `main`、`host_stride`、`pl_stride` 三个分支**到底差在哪里**——以及更重要的，它们**哪里完全一样**。
- 理解"输入侧数据预排序（pre-sorting / input reordering）"这件事，为什么可以放在三个不同的域（AIE / ARM / PL）里做，各自的代价是什么。
- 掌握 PL **DMA Stride Controller**（步进 DMA 控制器）内核的作用、它如何按"步进（stride）"模式从 DDR 取数，以及为什么本仓库 `main` 分支里找不到它的源码。
- 解释一个反直觉的系统不变量：**无论输入侧谁排序，输出侧的后处理（把乱序图像包重排回连续 DDR）永远由 PL 包路由器承担**。
- 说清 `pl_stride` 分支当前被锁死在 `AIE_SWITCHES=1` 的根因：`system.cfg` 无法表达"数组化的 AXI4-Stream 端口"。

本讲是 **advanced** 层级，承接 u7-l1（系统集成的编译/链接/打包主线）。本讲不再重复 `system.cfg` 是怎么自动生成的，而是聚焦于"三个分支在架构与职责上的取舍"。

## 2. 前置知识

在读本讲前，请确认你已经理解下列概念（它们来自前面的讲义，本讲直接使用，不再展开）：

- **三引擎分工**：ARM（`design/host`）管控制编排、AIE（`design/aie`）管核心反投影计算、PL（`design/pl`）管 DMA 重排与拼接（u2-l1）。
- **AIE 图的两层结构**：顶层 `BackProjectionGraph` 有 1 个 Data Broadcast + `AIE_SWITCHES` 个 `bpCluster` 子图；每个子图有 1 个 Pixel Demux + `IMG_SOLVERS_PER_SWITCH` 个 Image Reconstruction 内核（u4-l1）。
- **包交换 pktstream**：Pixel Demux 用 `pktsplit<32>` / `pktmerge<32>` 在一条物理流上按 `pkt_id` 复用多路逻辑数据，上限 32 路（u4-l2、u5-l2）。
- **PL 包路由器 `dma_pkt_router`**：解析包头里的 `instance_id`，按 `ddr_offset = instance_id × SAMPLES_PER_KERN` 把乱序图像包写回连续 DDR（u6-l1）。
- **`system.cfg`**：描述 PL↔AIE 连接（`nk`/`stream_connect`/`sp`）的接线说明文件，由 Makefile 根据 `common.h` 自动生成，是构建产物（u7-l1）。

一个贯穿全讲的关键词是**"排序（sorting / reordering）"**。这里的排序**不是**排序算法里的比大小排序，而是指：把目标像素（target pixel）数据按"每个 Image Reconstruction 内核各取所需、且能尽早并行启动"的顺序，安排好再送进 AIE。本讲的核心问题就是：**这件"排序"差事，该交给谁做？**

## 3. 本讲源码地图

本讲主要依据项目的 LaTeX 设计文档与 README，因为三个分支的差异是**系统级架构差异**，文档是权威说明；具体内核实现散落在不同分支。

| 文件 | 作用 |
|------|------|
| [doc/sections/implementation.tex](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex) | 给出三分支职责划分、每分支 Pixel Demux 的具体操作步骤、DMA Stride Controller 内核行为、"后处理恒在 PL"的论证 |
| [doc/sections/future_work.tex](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex) | 解释 `pl_stride` 为何被锁在 `AIE_SWITCHES=1`：`system.cfg` 无法表达数组化 AXI4-Stream 端口，并列出三条候选解法 |
| [README.md](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/README.md) | 在 `## Branches` 一节列出三个分支的链接，并指向 PDF 文档 |
| [design/common.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h) | `AIE_SWITCHES=7`、`IMG_SOLVERS_PER_SWITCH=32`、`IMG_SOLVERS` 派生关系，是讨论并行度的基准 |
| `design/pl/`（main 分支） | 仅含 `dma_pkt_router` 包路由器——**没有** stride controller，证实该内核不在 main 分支 |

## 4. 核心概念与源码讲解

### 4.1 三分支职责划分：输入侧"预排序"交给谁

#### 4.1.1 概念说明

三个分支共享**同一套反投影算法、同一批 AIE 内核类型、同一个 PL 包路由器做后处理**。它们唯一不同的是一件叫**"输入侧预排序"**的差事：在目标像素数据进入 AIE 的 Image Reconstruction 内核之前，由谁把数据排成"每个内核各拿自己那一份"的顺序。

文档把这件事的三个落点写得非常直白：

- **main**：AIE 内核吃进**未排序**数据，自己内部完成输入重排。
- **host_stride**：ARM Cortex-A72 在 DDR 里**预先排好**数据，减轻 AIE 内部的排序工作。
- **pl_stride**：PL 实现一个 DMA stride controller，在数据抵达 AIE **之前**完成预排序。

> 三种分支的区别仅在于"输入预处理由谁负责"，输出后处理三者一致——这点会在 4.3 展开。

#### 4.1.2 核心流程

把三个分支放进同一条数据通路里对比，差异只出现在"输入侧"那一段：

```text
                 ┌─────────────── 输入侧（三个分支在这里分道扬镳）───────────────┐
DDR 原始像素数据 │                                                              │
            └──▶ │ main       : 不排序 ──▶ AIE Pixel Demux 内部排序（整块投递） │ ──▶ AIE 重建内核
                 │ host_stride: ARM 预排序 ──▶ AIE Pixel Demux 轮询 16 像素投递   │ ──▶ AIE 重建内核
                 │ pl_stride  : PL Stride Controller 预排序 ──▶ AIE Pixel Demux │ ──▶ AIE 重建内核
                 └──────────────────────────────────────────────────────────────┘
                                            │
                                            ▼  (输出侧，三分支完全相同)
                                   PL 包路由器重排 ──▶ 连续 DDR 图像
```

关键认知是：**main 与 host_stride 共用同一张系统架构图**，差别纯属软件（ARM 是否在 DDR 里把数据排好）；而 `pl_stride` 是**真的多了一片 PL 硬件内核**（DMA Stride Controller），架构图都换了一张。

#### 4.1.3 源码精读

三分支职责的权威定义在 implementation.tex：

[doc/sections/implementation.tex:L132-L143](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex#L132-L143) —— 这段直接给出三个分支的对比定义：main 是"AIE 自排"、host_stride 是"ARM 预排"、pl_stride 是"PL 预排"。

文档里还有一处把"排序"职责明确写进**内核职责表**的地方。在 AIE 内核清单中，Pixel Demux 的描述特意注明了分支差异：

[doc/sections/implementation.tex:L64-L68](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex#L64-L68) —— 说明在 `main` 分支里 Pixel Demux 还兼任"输入排序"，而在 `host_stride` / `pl_stride` 里排序改由 ARM 或 PL 完成。

把这件事放在整个系统职责划分里看（ARM/AIE/PL 各自的 task list），文档特别标注了两处"based on which branch of the code is being ran"：

[doc/sections/implementation.tex:L110-L130](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex#L110-L130) —— AIE 阵列与 PL 各自的职责清单里，"按像素进特定 AIE 内核（排序）"和"PL 入口侧预处理重排"都被标注为**分支相关**。

main 与 host_stride 共用一张架构图、pl_stride 单独一张，体现在两幅图的 caption 上：

[doc/sections/implementation.tex:L161-L169](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex#L161-L169) —— `default_system_arch.png` 的标题明说它同时服务于 `main` 和 `host_stride`，并点出"ARM 装载到 DDR 的数据在 main 里未排序、在 host_stride 里已预排序"。

[doc/sections/implementation.tex:L192-L199](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex#L192-L199) —— `pl_stride_system_arch.png` 是 pl_stride 专属，多出"PL stride controller 选择并重排所需数据段"这一段。

两个分支在 Pixel Demux **操作步骤**上的具体差异（这是"输入排序"落到代码层面的样子）：

[doc/sections/implementation.tex:L363-L375](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex#L363-L375) —— **main 分支**的 Pixel Demux：写包头后，对一个内核连续投递 \((3 \times \texttt{PULSES} \times \texttt{RC\_SAMPLES})/\texttt{IMG\_SOLVERS}\) 个 float（即一次性灌满一个内核所需的全部像素），再轮到下一个内核。这是"整块投递"。

[doc/sections/implementation.tex:L377-L393](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex#L377-L393) —— **host_stride 分支**的 Pixel Demux：写包头后只投递 48 个 float（16 像素 × 3 分量），轮询遍历所有 `IMG_SOLVERS_PER_SWITCH` 个内核，外层再循环 `PULSES` 次。这是"每次 16 像素、轮流喂"。

两种投递方式的后果，文档在 Image Reconstruction 内核那段做了总结：

[doc/sections/implementation.tex:L598-L607](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex#L598-L607) —— host_stride / pl_stride 不再做 main 那一步"先读全部像素进 heap 缓冲"，而是每次直接从流里读 16 像素；这让 Pixel Demux 能"轮流给每个重建内核 16 像素，再绕回来"，从而**所有重建内核能更早并行开工**。

#### 4.1.4 代码实践

**实践目标**：用文档里的数字算一算"整块投递"与"轮流投递"在一次循环里各搬多少像素，体会并行启动差异。

**操作步骤**（源码阅读型实践）：

1. 打开 `design/common.h`，确认默认配置 `AIE_SWITCHES=7`、`IMG_SOLVERS_PER_SWITCH=32`（[design/common.h:L31-L38](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L31-L38)），故 `IMG_SOLVERS = 224`。
2. 在 main 分支 Pixel Demux 步骤里（L366-L375），每个内核一次拿 \((\texttt{PULSES}\times\texttt{RC\_SAMPLES})/\texttt{IMG\_SOLVERS} = (602\times512)/224 = 1376\) 个像素。
3. 在 host_stride 分支步骤里（L380-L393），每个内核一次只拿 16 个像素，但 32 个内核都能在"一轮"里各拿到 16 个。

**需要观察的现象**：main 分支要先把 1376 个像素全喂给内核 0，内核 1 才开始有数据；host_stride 一轮（32×16=512 像素）之后 32 个内核都拿到了首批 16 像素、都能开工。

**预期结果**：你会直观看到"轮流投递"把内核启动串行→并行，这正是把排序搬出 AIE 的收益所在。

#### 4.1.5 小练习与答案

**练习 1**：为什么文档说 main 与 host_stride "共用一张架构图"，它们到底差在哪？

> **答**：两者硬件拓扑完全相同（都只有 PL 包路由器，没有 stride controller）。差别是纯软件层面的数据布局：main 把未排序的像素数组原样装进 DDR，由 AIE 内部的 Pixel Demux 在运行时重排；host_stride 则是 ARM 在装载阶段就把像素按"轮流喂"的顺序排进 DDR。架构图区分不出这种软件差异，所以共用一张。

**练习 2**：把"输入排序"从 AIE 挪到 ARM 或 PL，换来了什么、付出了什么？

> **答**：换来的是 Pixel Demux 可以"每轮给每个重建内核 16 像素"，让所有重建内核更早并行启动（见 L685-L697 列举的三条好处）。代价是：host_stride 把排序负担转嫁给 ARM（占用主机算力与取数时间）；pl_stride 则要新增一片 PL 硬件内核，并因此引入 `AIE_SWITCHES=1` 的限制（见 4.2）。

---

### 4.2 DMA Stride Controller：PL 侧的预排序引擎

#### 4.2.1 概念说明

`pl_stride` 分支多出来的那片 PL 内核叫 **DMA Stride Controller（步进 DMA 控制器）**。它的职责用一句话讲：**从 DDR 里的目标像素大数组中，按一种"步进式（strided）"的取址模式，挑出每个重建内核当前需要的那些像素，排好序后用 AXI4-Stream 喂给 Pixel Demux**。

之所以叫"stride（步进）"，是因为它不是顺序读取 DDR，而是**指针周期性地跳跃**：先取一簇像素、跳过一大段、再取一簇……这种非连续取址正是"重排"的本质——用读地址的顺序来定义输出顺序，免去 AIE 内核自己重排。

注意一个容易混淆的点：stride controller **只服务于 pl_stride 分支**，而且它的产物是喂给 **Pixel Demux 的输入**（`px_xyz_in`），不是喂给重建内核的 RC 数据。RC 数据（距离压缩样本）和 slowtime 仍走原来的 Data Broadcast 路径，三分支一致。

#### 4.2.2 核心流程

stride controller 是一个多层嵌套循环、靠"移动 DDR 指针"来遍历的 DMA 引擎。把它的工作抽象出来（结合文档 L710-L757）：

```text
for pulse in 0 .. PULSES:                         # 最外层：逐脉冲
  shift 指针 by "一个脉冲的像素量"
  for sw in 0 .. AIE_SWITCHES:                    # 逐 bpCluster
    shift 指针 by TOTAL_TARGET_PIXELS / AIE_SWITCHES
    for block in 0 .. (每核像素数 / 16):           # 逐 16-像素块
      rollback 指针到本 bpCluster 起点，但偏移 +16·block
      for kern in 0 .. IMG_SOLVERS_PER_SWITCH:    # 逐重建内核
        shift 指针 by TOTAL_TARGET_PIXELS / IMG_SOLVERS
        for i in 0 .. 16:                         # 取 16 个像素
          从 DDR 读 128 bit (X,Y,Z,pad) ──▶ pl_stream_out
```

几个关键量：

- `TOTAL_TARGET_PIXELS = AZ_SAMPLES × RC_SAMPLES`（文档 L725-L726）。
- 每个重建内核分到的像素数 \(= \texttt{TOTAL\_TARGET\_PIXELS}/\texttt{IMG\_SOLVERS}\)。
- 128 bit 对齐：DDR 按 64 bit 边界操作，所以一拍 128 bit = 两个 64 bit，前 64 bit 装 X、Y，后 64 bit 装 Z 和一个零填充（文档 L427-L435、L715-L718）。

stride controller 这样取址的直接收益（文档 L685-L697 总结了三条）：

1. Pixel Demux 改成"轮流给每个内核 16 像素"，而不是一次性灌满一个内核。
2. 重建内核收到 16 像素就能开工，不必等收齐全部像素。
3. 更多重建内核能更早并行，因为不必等前一个内核被喂饱。

#### 4.2.3 源码精读

stride controller 的端口与逐步行为在 implementation.tex 有专门小节：

[doc/sections/implementation.tex:L674-L683](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex#L674-L683) —— 点明这个 PL 内核**只用于 pl_stride 分支**，且只负责给 Pixel Demux 喂"已预排序的目标像素"。

[doc/sections/implementation.tex:L699-L706](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex#L699-L706) —— 端口表：`ddr_mem`（64 bit DDR 读口）与 `pl_stream_out`（送给 Pixel Demux 的 AXI4-Stream）。

[doc/sections/implementation.tex:L710-L757](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex#L710-L757) —— 九步操作流程，刻画了"取 16 像素 → 按每核像素数移指针 → 按 16 像素回卷并偏移 → 按 bpCluster 移指针 → 按脉冲移指针"的层层嵌套指针移动。

收益的三条总结：

[doc/sections/implementation.tex:L684-L697](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex#L684-L697) —— 明确把"轮流 16 像素投递、内核更早并行"列为 stride controller 带来的三项收益。

**`AIE_SWITCHES=1` 限制**——这是 stride controller 最重要的边界条件。文档在两处都点了它：

[doc/sections/implementation.tex:L759-L767](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex#L759-L767) —— 实现篇承认"当前设计被限制在 `AIE_SWITCHES=1`，未来工作希望能增大此值"，并把详情指向 future_work。

[doc/sections/implementation.tex:L446-L463](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex#L446-L463) —— pl_stride 分支的 `common.h` 配置示例里，`AIE_SWITCHES=1`，并附 NOTE 说明此限制。

**限制的根因**在 future_work.tex 里讲得最透彻。stride controller 的顶层函数签名把输出端口声明成了一个**数组**：

[doc/sections/future_work.tex:L70-L76](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L70-L76) —— 指出当前内核只支持单输出流、`AIE_SWITCHES` 固定为 1，且顶层签名把 `pl_stream_out` 定义为 AXI4-Stream **数组**接口。

[doc/sections/future_work.tex:L88-L97](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L88-L97) —— 函数签名：`hls::stream<ap_axiu<128,0,0,0>> pl_stream_out[AIE_SWITCHES]`，用 `#pragma HLS INTERFACE axis port=pl_stream_out` 声明为数组化流端口。

然后是关键的根因段落：

[doc/sections/future_work.tex:L100-L107](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L100-L107) —— 虽然仿真与 testbench 证明 `AIE_SWITCHES > 1` 在**功能上**能跑，但系统集成被 Vitis 工具链卡住：`system.cfg`（描述 PL↔AIE 连接的文件）**无法充分引用像 `pl_stream_out[AIE_SWITCHES]` 这样的数组化 AXI4-Stream 端口**，工具链要求每条连接都用显式、唯一的命名端口，不允许在综合/链接时使用参数化的数组引用。

文档随后给出三条候选解法（留给未来工作）：

[doc/sections/future_work.tex:L113-L123](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L113-L123) —— 三条路径：① 内核原型法（为每个目标 `AIE_SWITCHES` 值各做一个内核、显式列出 `pl_stream_out_N` 端口）；② 内核复制法（改成单簇输出流、为每个 AIE 簇实例化一个内核实例）；③ 工具链法（等更新版本的 Vitis 支持 `system.cfg` 里的数组化流接口）。

> 这正好与 u7-l1 的认知衔接：`system.cfg` 由 Makefile 按 `common.h` 自动生成，里面用 `stream_connect` 逐条写死 PL 端口↔AIE PLIO 的连接。包路由器（单输出流、每实例一个 `pl_stream_in`）能被 `system.cfg` 表达，所以可以 `AIE_SWITCHES=7`；而 stride controller 的"一个数组端口要展开成 N 条流"的写法，恰恰是 `system.cfg` 表达不了的。

#### 4.2.4 代码实践

**实践目标**：用 `main` 分支仓库里**不存在** stride controller 源码这一事实，反推它为什么只活在 `pl_stride` 分支。

**操作步骤**（源码阅读型实践）：

1. 在本仓库（`main` 分支）执行 `git ls-files design/pl`，列出 PL 目录下的全部受控文件。
2. 你会看到只有 `dma_pkt_router.cpp`、`dma_pkt_router.h`、`pkt_router_config.cfg`、`tb/dma_pkt_router_tb.cpp`、`tb/run_dma_pkt_router_tb.tcl`——**没有** `dma_stride_controller.*`。
3. 对照 future_work.tex 的函数签名（L88-L97），确认 stride controller 的输出是一个数组流端口 `pl_stream_out[AIE_SWITCHES]`。

**需要观察的现象 / 预期结果**：`main` 分支不需要 stride controller（它走 AIE 自排），所以源码不在本分支；该内核源码只存在于 `pl_stride` 分支。本讲引用的 stride controller 行为来自文档（implementation.tex + future_work.tex），而非 main 分支的源码文件——这是阅读跨分支架构对比时常见的情形。

#### 4.2.5 小练习与答案

**练习 1**：stride controller 的输出 `pl_stream_out` 为什么声明成数组 `pl_stream_out[AIE_SWITCHES]`？

> **答**：因为每个 `bpCluster`（即每个 AIE switch）需要一个独立的、已预排序的像素流喂给它自己的 Pixel Demux。理想情况下 `AIE_SWITCHES` 个簇要 `AIE_SWITCHES` 条流，所以用数组化端口表达"每簇一条流"。

**练习 2**：文档说"`AIE_SWITCHES > 1` 在仿真里功能正常，但系统集成被工具链卡住"。这两者为什么会出现矛盾？

> **答**：HLS 仿真（csim/cosim）只验证内核自身的功能与 testbench 接线，数组化流端口在 testbench 里可以手动连；而"系统集成"要用 `system.cfg` 把 PL 端口接到 AIE 的 PLIO，再交给 Vitis 链接成 XSA。`system.cfg` 的 `stream_connect` 语法要求每条连接引用一个显式命名的端口，无法展开参数化的数组端口 `pl_stream_out[AIE_SWITCHES]`，于是链接阶段过不去。功能正确 ≠ 可集成。

**练习 3**：三条件候选解法里，哪一条最接近 main 分支里包路由器的现有做法？

> **答**："内核复制法"（L117-L119）。包路由器正是"单输出概念、按 `AIE_SWITCHES` 实例化多份、每份一个命名端口"，从而能被 `system.cfg` 逐条表达、支持 `AIE_SWITCHES=7`。把 stride controller 改成同样的"每簇一个内核实例"，就能绕开数组端口的限制。

---

### 4.3 后处理恒在 PL：包路由器的不变量角色

#### 4.3.1 概念说明

读到这里你可能有个疑问：既然输入侧可以灵活分配给三个域，输出侧能不能也省掉 PL？答案文档给得很干脆——**不能**。无论输入侧是 AIE 自排、ARM 预排还是 PL 预排，**输出侧的后处理（把图像包重排回连续 DDR）永远由 PL 包路由器承担**。这是三个分支共享的系统不变量。

原因是 AIE 内部用了**包流合并器（packet stream merger）**：224 个重建内核的输出要汇聚回有限的 GMIO 端口（最多 32 入 32 出），靠 `pktmerge<32>` 把多条流并成一条。合并提升了端口利用率，但**打乱了到达顺序**——哪个内核先算完、哪个包先到，是运行时调度决定的，不可预测。如果不重排，ARM 从 DDR 读出来的图像就是乱序的。所以必须有一片 PL 内核读包头、按 `instance_id` 把每个包写回正确的 DDR 偏移，拼成连续图像。

#### 4.3.2 核心流程

后处理这条链三个分支完全一致：

```text
AIE 重建内核 ──img_out 包流──▶ pktmerge<32> (合并, 但打乱顺序)
                                     │
                                     ▼  PLIO (128-bit AXI4-Stream)
                          PL 包路由器 dma_pkt_router
                          ├─ 读包头: pkt_id / instance_id
                          ├─ ddr_offset = instance_id × SAMPLES_PER_KERN
                          └─ 把图像数据写到 DDR[ddr_offset : ...]
                                     │
                                     ▼
                          DDR 中连续、有序的完整聚焦图像
                                     │
                                     ▼  bo.sync(FROM_DEVICE)
                                   ARM 读回
```

注意：包路由器真正用于重排的"身份证"是**全局唯一的 `instance_id`**（0~223），而 `pkt_id` 只在单个 switch 内唯一、被解析但不用（详见 u6-l1）。这条后处理链与分支无关。

#### 4.3.3 源码精读

"后处理恒在 PL"这一论断的原文：

[doc/sections/implementation.tex:L145-L152](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex#L145-L152) —— 这段是本讲最重要的不变量陈述：无论预处理在 AIE / ARM / PL 哪里做，**后处理永远在 PL**；原因是 AIE 内部用了 packet stream merger 把多路内核输出并到有限的 GMIO 端口（32 入 32 出），合并提升了端口利用率但打乱了输出顺序，必须由 PL 包路由器读包 ID、重排成连续 DDR，ARM 才能不加额外后处理就读到连贯图像。

PL 内核清单里也把"包路由器（后处理）"与"stride controller（预处理，分支相关）"明确区分开：

[doc/sections/implementation.tex:L76-L85](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex#L76-L85) —— PL 内核表：包路由器（1x-7x）负责把合并后的 AXI4-Stream 重排进连续 DDR；stride controller（1x-7x）负责在送入 AIE 前从 DDR 读出并重排——后者被注明"offloading the input-sorting responsibility"，即它是为分担输入排序而存在。

两幅系统图的 caption 也都把"结果流到 PL 包路由器、重排后写回连续 DDR"作为收尾，与分支无关：

[doc/sections/implementation.tex:L192-L199](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex#L192-L199) —— pl_stride 架构图 caption 的最后一句同样是"结果流到 PL 包路由器，重排后写回连续 DDR"，与 main/host_stride 图（L161-L169）的收尾一致。

> 与 u6-l1 / u7-l1 的衔接：包路由器是 `design/pl/` 里 main 分支唯一的 PL 内核，`system.cfg` 里每个 PL 实例的 `ddr_mem` 主写口由 `sp` 绑到 DDR、`pl_stream_in` 由 `stream_connect` 接到对应 AIE PLIO 输出（u7-l1）。正因为它是"每实例一个命名输入流"，`system.cfg` 能逐条表达，所以包路由器可以 `AIE_SWITCHES=7` 而不受 stride controller 那种数组端口限制。

#### 4.3.4 代码实践

**实践目标**：在 `system.cfg` 层面理解"为什么后处理必须每簇一个 PL 实例、且必须独享一段 DDR"。

**操作步骤**（源码阅读型实践）：

1. 回顾 u6-l1 的偏移公式：`ddr_offset = instance_id × SAMPLES_PER_KERN`，默认 `SAMPLES_PER_KERN = 1376`。
2. 算一下 7 个 PL 实例各写 1376 个 cfloat，首尾相接拼成 \(7 \times 1376 = 9632\) 个 cfloat（这是单 switch 视角；全图 224 核共 \(224 \times 1376 = 308224\) 个 cfloat）。
3. 对照 u7-l1 讲过的 `system.cfg` 三类指令（`nk` 实例化、`stream_connect` 接 PLIO、`sp` 绑 DDR），说明每个包路由器实例如何独占一段不重叠 DDR。

**需要观察的现象 / 预期结果**：每个 PL 实例的写入区段由它服务的那些内核的 `instance_id` 决定、互不重叠，所以 7 个实例可并行写同一块 buffer 而不冲突——这正是后处理"恒在 PL"且能并行的设计依据。

#### 4.3.5 小练习与答案

**练习 1**：为什么不能干脆去掉 `pktmerge`、让每个重建内核各占一个 GMIO 输出，从而免去 PL 重排？

> **答**：因为 GMIO 端口数量有硬上限（32 入 32 出，见 L148），而重建内核最多有 224 个，远超 32。必须用 `pktmerge` 把多路输出合并到少数端口上，合并的代价就是顺序被打乱，于是必须有 PL 包路由器重排。这是一个"端口利用率 ↔ 输出顺序"的权衡，设计选择了前者并用 PL 兜底后者。

**练习 2**：如果未来 `system.cfg` 支持了数组化流端口（4.2 的工具链解法），后处理链需要改吗？

> **答**：不需要。后处理链（pktmerge → PLIO → 包路由器 → DDR）与输入侧排序无关，也不依赖 stride controller。工具链解法只影响 stride controller（预处理）能否扩展到多簇，不触及后处理。

## 5. 综合实践

**任务**：画出三张系统数据通路图，分别对应 `main`、`host_stride`、`pl_stride`，并在每张图上明确标出"谁负责输入排序"；然后用 `system.cfg` 的语法限制解释 `pl_stride` 为何当前只能 `AIE_SWITCHES=1`。

**操作步骤**：

1. **main 分支图**。参考 [default_system_arch 的 caption](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex#L161-L169)。通路：ARM 把**未排序**像素装 DDR → NoC → AIE 重建簇。在图上把"输入排序"标签贴在 **AIE Pixel Demux** 上，并注明它的投递方式是"整块投递（每核 1376 像素）"。后处理：AIE 输出 → PL 包路由器 → 连续 DDR。

2. **host_stride 分支图**。拓扑与 main **完全相同**（同一张架构图）。唯一差别：ARM 在装 DDR 前先把像素**预排序**好。把"输入排序"标签贴在 **ARM** 上。Pixel Demux 改为"轮流 16 像素投递"。后处理同上。

3. **pl_stride 分支图**。参考 [pl_stride_system_arch 的 caption](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex#L192-L199)。通路：ARM 装 DDR → **PL Stride Controller** 选段重排 → NoC → AIE 重建簇。把"输入排序"标签贴在 **PL DMA Stride Controller** 上。注意此图比前两张多一片 PL 内核。后处理同上。

4. **解释 `AIE_SWITCHES=1` 限制**。在 pl_stride 图的 stride controller 旁加一条注释，写明：该内核的输出端口是数组化流 `pl_stream_out[AIE_SWITCHES]`（见 [future_work.tex:L88-L97](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L88-L97)）；`system.cfg` 的 `stream_connect` 只能引用显式命名的单一端口、无法展开这种参数化数组端口（[future_work.tex:L100-L107](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L100-L107)）；因此当前只能驱动 1 个 `bpCluster`（`AIE_SWITCHES=1`），并行度被压低。对照之下，包路由器是"每实例一个命名输入流"，能被 `system.cfg` 逐条表达，所以 main/host_stride 可以 `AIE_SWITCHES=7`。

**预期结果**：

- 三张图的**输出侧（后处理）完全一致**，都经过 PL 包路由器——直观体现"后处理恒在 PL"。
- 三张图的**输入侧**分别把排序职责落在 AIE / ARM / PL，且只有 pl_stride 多了一片 PL 硬件。
- 你能用一句话说清：pl_stride 的限制不是算法问题、不是仿真问题，而是 `system.cfg` 连接描述语言的表达力问题。

> 若想验证你的理解，可以尝试口头给出 future_work 列出的三条解法（[L113-L123](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L113-L123)）分别对应图上的什么改动。

## 6. 本讲小结

- 三个分支**只在"输入侧预排序由谁做"上不同**：main 是 AIE 自排（Pixel Demux 整块投递）、host_stride 是 ARM 预排（Pixel Demux 轮流 16 像素投递）、pl_stride 是 PL DMA Stride Controller 预排。
- **main 与 host_stride 共用同一张系统架构图**，差别纯在软件（ARM 是否在 DDR 里预排数据）；`pl_stride` 多了一片 PL 硬件内核，是真正的架构差异。
- 把排序搬出 AIE 的收益是：Pixel Demux 改为轮流投递，所有重建内核能更早并行启动。
- **DMA Stride Controller** 用"步进式取址"从 DDR 挑像素、排好序喂给 Pixel Demux；它只存在于 `pl_stride` 分支，main 分支仓库里没有它的源码。
- **后处理恒在 PL** 是系统不变量：`pktmerge` 提升端口利用率但打乱顺序，必须由 PL 包路由器按 `instance_id` 重排回连续 DDR——三个分支无一例外。
- `pl_stride` 被锁在 `AIE_SWITCHES=1` 的根因是工具链限制：`system.cfg` 无法表达数组化的 AXI4-Stream 端口 `pl_stream_out[AIE_SWITCHES]`；包路由器因是"每实例一个命名端口"而无此限制。

## 7. 下一步学习建议

- **接 u7-l3（硬件部署）**：三个分支最终都要打包成 SD 卡镜像上板运行，下一讲讲 Yocto/NFS/TFTP/JTAG 部署链，正好把"分支差异如何落到可运行的 `sar_backproject.elf` + BOOT.BIN"补全。
- **回顾 u8-l3（优化与未来工作）**：本讲引用的 future_work 解法（内核原型法 / 内核复制法 / 工具链法）属于项目优化的未竟事项，u8-l3 会把所有优化方向（ILP、选择性 RC 分发、动态 buffer 索引、DSP FFT、全孔径、stripmap、XD100 对比）串成一张全景图。
- **想动手的读者**：可在本地 `git branch -a` 查看 `origin/host_stride` 与 `origin/pl_stride`，对照本讲列的差异去 diff 三个分支的 `design/pl/` 与 `design/aie/backprojection.cc`，亲眼确认 stride controller 源码与 Pixel Demux 投递循环的差异（注意：跨分支源码仅供阅读，请勿在 main 分支改动）。
