# 系统集成、架构取舍与工程实践

> 前置讲义：[u2-l4 圆柱面投影的硬件实现：定点Verilog](u2-l4-cylindrical-projection-hardware.md)、[u3-l1 DDR3突发传输控制器 mem_burst](u3-l1-ddr3-mem-burst.md)、[u4-l1 动态规划法寻找最佳缝合线](u4-l1-dynamic-seam.md)、[u5-l1 定点数运算与位宽设计深入](u5-l1-fixed-point-arithmetic.md)、[u5-l2 CORDIC与MIG/时钟IP的集成](u5-l2-cordic-mig-ip-integration.md)。
>
> 这是全册的收尾讲。前十几讲我们把项目拆成了零件：软件流水线、投影数学、双线性插值表、定点 Verilog、DDR3 突发控制器、24→64 异步 FIFO、缝合线 DP、CORDIC/MIG/时钟 IP……每一讲都只盯着一个模块。本讲把这些零件**重新装回一台完整的机器**——画出七路摄像头从感光到全景输出的端到端数据通路，回答三个工程问题：**资源够不够用、质量能不能达标、半成品怎么补成量产品**。换句话说，前面是「读懂每一块砖」，本讲是「看完图纸、评估造价、列出返工清单」。

## 1. 本讲目标

学完本讲，你应当能够：

- 画出七路摄像头图像拼接系统的**完整数据通路**：采集 → 位宽转换 → DDR3 存储 → 圆柱面投影 → 缝合线 → 融合 → 输出，并标注每个环节的**时钟域**与「已实现 / 仅 README 描述 / 不可综合」三种状态。
- 把 OpenCV 软件流水线的八阶段，映射到「**离线一次性标定**（烧成定点常数）」与「**每帧实时处理**（FPGA 加速）」两类，说清哪些搬上了硬件、哪些没有。
- 针对三个关键决策点——**插值方法（双线性 vs 最近邻）、双线性取四像素的存储结构、24→64 位宽转换的存储体选型**——说出资源与质量的取舍理由，并用源码佐证。
- 针对 README 点名的遗留问题（白平衡、小数权重融合）以及代码层面的遗留（DynamicSeam 不可综合、FIFO 未收录、投影丢弃小数权重），各给出一个**可落地的 FPGA 改进思路**。

## 2. 前置知识

### 2.1 从「单模块」到「系统」要补的两件事

前面每一讲都在一个 `.v` 或 `.cpp` 文件内部读代码。系统级思考要补两件新事：

1. **接口对齐**：模块 A 的输出端口，能不能直接连到模块 B 的输入端口？位宽、握手协议、时钟域是否一致？本项目没有顶层集成文件（见 u1-l2），所以「对不对齐」要靠我们**人工把端口拼起来**判断。
2. **资源总量**：单个模块用几块 Block RAM、几个 DSP、几行查找表（LUT）看起来不多，但七个摄像头、一帧几百万像素乘起来，FPGA 芯片可能装不下。「能不能装下」是系统级才暴露的问题。

### 2.2 三种「实现状态」要分清

本项目是「源码片段集」（u1-l2 结论），并非所有环节都有可运行代码。阅读系统时必须区分三种状态，避免把「README 说了」误当成「代码做了」：

| 状态 | 含义 | 本项目示例 |
|------|------|-----------|
| ✅ 已实现 | 有完整、可读（未必可综合）的源码 | `mem_burst.v`、`圆柱面投影.v`、`uart_rx/tx.v` |
| ⚠️ 草稿/不可综合 | 有源码，但写法综合会失败 | `DynamicSeam.v`（u4-l1 已分析） |
| ❓ 仅描述 | 只在 README 里提到，代码未收录 | 采集模块、24→64 异步 FIFO、融合模块 |

### 2.3 时钟域跨域的两种武器

系统里多个时钟在同时跑，数据从一个时钟域进另一个时钟域必须做**跨时钟域（CDC, Clock Domain Crossing）处理**，否则会采到亚稳态（见 u1-l3、u3-l3）。两种常用武器：

- **异步 FIFO**：双端口 RAM + 格雷码指针，适合**批量、跨域、可反压**的数据流。本项目 24→64 转换正是这种（u3-l3），但因指针非 +1 递增，格雷码方案失效，改用「写端驱动读端」的电平触发。
- **两级同步寄存器**：适合**单 bit 控制信号**跨域。UART 接收用它抑制亚稳态（u1-l3）。

### 2.4 资源三件套：LUT / FF / Block RAM / DSP

Xilinx 7 系列 FPGA 的片上资源主要有四类，系统评估时要分别算：

| 资源 | 用途 | 本项目里谁吃得多 |
|------|------|-----------------|
| **LUT**（查找表） | 组合逻辑、地址译码 | 二维寄存器数组 FIFO、状态机 |
| **FF**（触发器） | 寄存器、流水线 | 大位宽累加器（`x/y/z` 56 位）、寄存器数组 FIFO |
| **Block RAM**（块存储） | 大容量片上 RAM，位宽须为 2 的幂比 | 投影模块（若用双线性）、图像行缓存 |
| **DSP48**（乘加硬核） | 高速乘法、乘累加 | 定点矩阵乘（`k_inv * x_`）、CORDIC |

记住一条铁律：**Block RAM 的读写端口位宽比必须是 2 的幂**（如 8:16、16:32）。这条铁律直接决定了本项目 24→64 FIFO 的存储体选型，是 4.2 节的核心。

## 3. 本讲源码地图

本讲横跨全部五个关键文件，但视角不再是「逐行读」，而是「在系统图里定位」：

| 文件 | 在系统中的位置 | 本讲关注点 |
|------|---------------|-----------|
| [README.md](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md) | 全系统说明书 | 遗留问题清单、资源取舍的第一手自述 |
| [圆柱面投影.cpp](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp) | 软件参考实现 | 八阶段流水线、`weight` 表与 `FeatherBlender` 融合 |
| [圆柱面投影.v](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v) | 硬件投影模块 | 丢弃小数权重的简化、定点乘加链时序 |
| [DDR3控制/mem_burst.v](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v) | 存储子系统 | 突发接口如何把投影、缝合线接到 DDR3 |
| [动态规划法寻找最佳缝合线/DynamicSeam.v](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v) | 缝合线草稿 | 不可综合写法、MIG 接线错误 |

一句话总览：`README.md` 是需求与遗留问题的源头；`.cpp` 是「正确答案」标尺；`.v` 们是硬件实现，其中 `mem_burst` 是唯一干净可复用的积木，`project` 是可读但简化的原型，`DynamicSeam` 是反面教材。本讲要把它们拼成一张图、算清一笔账、列出一张返工单。

## 4. 核心概念与源码讲解

### 4.1 系统架构：七路摄像头拼接的完整数据通路

#### 4.1.1 概念说明

「系统集成」要回答的第一个问题是：**一张全景图，从光子进来到像素出去，到底流经了哪些硬件模块？**

七路摄像头全景拼接的系统架构，本质是一条**时分复用的单像素流水线**：七路摄像头虽然并行采集，但受限于单片 DDR3 的带宽，投影之后的处理是**逐像素、逐图**串行进行的。这与软件 OpenCV「整张图存内存、随便随机访问」的自由度完全不同——FPGA 必须**按光栅扫描顺序、流式地**处理像素，这是所有时序难点的总根源。

要建立系统视图，需要把三件事对齐：

1. **空间上的数据通路**：数据从哪个模块到哪个模块，位宽多少。
2. **时间上的软件↔硬件分工**：软件做一次、硬件做每帧。
3. **时钟域**：每个模块跑在哪个时钟下，跨域在哪发生。

#### 4.1.2 核心流程

**（a）端到端数据通路**

```
┌─────────┐ 24bit  ┌──────────────┐ 64bit  ┌──────────┐ 64bit   ┌──────────────┐
│ 7路摄像头 │──────▶│ 24→64bit 异步 │───────▶│  mem_burst │────────▶│  圆柱面投影   │
│ 采集(❓)  │       │  FIFO (❓)    │  写    │  +MIG(✅) │  读/写   │  project(⚠️) │
└─────────┘       └──────────────┘        └──────────┘          └──────┬───────┘
                                                                     │ 投影后回写
                                                                     ▼
┌─────────┐  ┌──────────────┐  像素   ┌──────────────┐  像素   ┌──────────────┐
│ 显示/输出 │◀─│  融合(❓)     │◀───────│ 缝合线DP(⚠️)  │◀───────│   DDR3 重叠区 │
│  (❓)    │  │ 小数权重没做好 │        │ DynamicSeam  │ 行数据  │   像素回读    │
└─────────┘  └──────────────┘        └──────────────┘        └──────────────┘
```

图例：✅ 已实现且可综合　⚠️ 有源码但不可综合或简化　❓ 仅 README 描述、代码未收录

**（b）软件↔硬件分工：一次性标定 vs 每帧实时**

软件流水线（`圆柱面投影.cpp` 的 `main`）共八阶段，按「是否每帧都跑」一分为二：

| 软件阶段 | C++ 代码 | 是否每帧 | 硬件归宿 |
|---------|---------|---------|---------|
| ① 特征提取 ORB | `OrbFeaturesFinder` | 否（标定一次） | **不做**，离线跑 |
| ② 特征匹配 2NN | `BestOf2NearestMatcher` | 否 | **不做**，离线跑 |
| ③ 相机参数估计 | `HomographyBasedEstimator` | 否 | **不做**，产出 K、R |
| ④ 光束平差 | `BundleAdjusterRay` | 否 | **不做**，精修 R |
| ⑤ 圆柱面投影 | `CylindricalWarper`+`warp` | **是** | `project` 模块（✅简化） |
| ⑥ 曝光补偿 | `ExposureCompensator` | 否（估增益） | **未实现** |
| ⑦ 缝合线 | `GraphCutSeamFinder` | **是** | `DynamicSeam`（⚠️草稿） |
| ⑧ 羽化融合 | `FeatherBlender` | **是** | **未实现**（小数权重没做好） |

关键认知：**阶段 ①–④ 是一次性相机标定，算力大但只做一次**，产出相机内参矩阵 \(K\)、旋转矩阵 \(R\)。这两个矩阵被「烧死」成硬件定点常数——焦距倒数 `coe` 与矩阵系数 `k_inv0~k_inv8`，每帧不再重算（见 u5-l1）。**阶段 ⑤⑦⑧ 才是每帧实时处理**，是 FPGA 加速对象。阶段 ⑥（曝光补偿）对应 README 抱怨的「白平衡没做好」。

**（c）时钟域拓扑**

继承 u5-l2 的结论，系统里有三个时钟域：

```
板载 200MHz 差分晶振 ──► clk_wiz_0 ──► sys_clk ──► MIG(sys_clk_i) ──► ui_clk (=mem_clk)
                         (MMCM)                                          │
                                                                         ▼
                                                                  mem_burst / DynamicSeam
                                                                  的 app_* 接口域

摄像头采集域(❓) ──写(100MHz)──► [24→64 异步 FIFO] ──读(ui_clk)──► DDR3 域

project 模块域 ──► 独立 clk（与 CORDIC 同频，待确认）
```

跨域点有三处：摄像头域 → FIFO 写端、FIFO 读端 → DDR3 域、DDR3 域 → project 域。前两处由异步 FIFO 兜底，第三处本项目没明确处理（投影模块从 DDR3 取数的接口未收录）。

#### 4.1.3 源码精读

**（a）软件八阶段的代码锚点**

软件流水线的阶段顺序直接写在 `main` 里，逐行可对照（[圆柱面投影.cpp:267-497](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L267-L497)）：

- 阶段①②：特征提取与匹配，`matcher(features, pairwise_matches)` 在 [圆柱面投影.cpp:290](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L290)。
- 阶段③：估计，`estimator(...)` 在 [圆柱面投影.cpp:298](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L298)，产出 `cameras[i].R`。
- 阶段④：光束平差，`(*adjuster)(...)` 在 [圆柱面投影.cpp:315](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L315)。
- 阶段⑤：投影，`warp(imgs[i], K, cameras[i].R, ...)` 在 [圆柱面投影.cpp:355](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L355)。
- 阶段⑦：缝合线，`GraphCutSeamFinder` 在 [圆柱面投影.cpp:388](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L388)，`seam_finder->find(...)` 在 [圆柱面投影.cpp:395](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L395)。
- 阶段⑧：融合，`FeatherBlender` + `setSharpness(0.1)` 在 [圆柱面投影.cpp:451-L453](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L451-L453)，最终全景图 `imwrite("pano.jpg", result)` 在 [圆柱面投影.cpp:492](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L492)。

注意 `main` 里 `num_images = 2`（[圆柱面投影.cpp:271](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L271)），软件参考实现实际只拼了两幅图，七路是硬件目标。

**（b）硬件投影把哪几阶段固化成了常数**

`project` 模块（[圆柱面投影.v:23-119](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L23-L119)）只实现了阶段⑤，并把阶段①–④的产物烧成两条常数：

- 焦距 `coe = 24'b0_0_0000_0000_0001_1000_0011_01`（[圆柱面投影.v:30](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L30)），即 C++ 里 `scale = 2707.47f`（[圆柱面投影.cpp:30](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L30)）的倒数，Q2.22 定点。
- 矩阵系数 `k_inv0~k_inv8`（[圆柱面投影.v:62-70](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L62-L70)），即 C++ 里 \(K\cdot R^{-1}\)（[圆柱面投影.cpp:116-119](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L116-L119)）的九个定点元素，Q13.8 有符号。

这条「软件标定 → 定点常数 → 硬件烧死」的链路，是整个项目**软件参考实现存在的根本理由**——`.cpp` 不是用来跑全景的，是用来**算系数、当标尺**的。

**（c）mem_burst 是系统里唯一干净的「总线积木」**

数据通路里，`mem_burst`（[DDR3控制/mem_burst.v:3-234](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v#L3-L234)）是「突发级用户接口」与「MIG 应用接口」之间的翻译层。它的用户侧端口（`rd_burst_req/wr_burst_req/rd_burst_addr/...`，[mem_burst.v:11-23](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v#L11-L23)）是**系统里所有需要访问 DDR3 的模块（投影、缝合线、融合）的共同接入点**。换句话说，`project` 和 `DynamicSeam` 想从 DDR3 取数，本应都按 `mem_burst` 的突发协议来——但 `DynamicSeam` 偏偏自己又例化了一颗 MIG（见 4.3 节），这是系统集成的重大失误。

#### 4.1.4 代码实践：绘制端到端数据通路与时钟域标注图

**实践目标**：把上面三张散图合并成一张完整的系统总图，亲手把每个端口拼一遍，暴露接口不对齐的地方。

**操作步骤**：

1. 在纸上或绘图工具里画出七个矩形：`采集`、`24→64 FIFO`、`mem_burst+MIG`、`project`、`DynamicSeam`、`融合`、`输出`。
2. 查 `mem_burst` 的用户侧端口（[mem_burst.v:11-23](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v#L11-L23)），把 `wr_burst_data[63:0]`/`rd_burst_data[63:0]` 标到箭头上。
3. 在 `24→64 FIFO` 与 `mem_burst` 的连接处，标注「写端 100MHz、读端 ui_clk」两个时钟域，并画一条虚线表示异步跨域。
4. 在 `DynamicSeam` 与 `mem_burst` 之间画一个**红色叉号**——因为 `DynamicSeam` 没有用 `mem_burst`，而是自己例化了 MIG（[DynamicSeam.v:192-232](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L192-L232)），破坏了单一存储入口的架构。
5. 在 `融合` 矩形上打上问号，注明「代码未收录、小数权重没做好」（[README.md:4](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L4)）。

**需要观察的现象**：你会发现图里有**三处断点**（采集、FIFO、融合标❓）和**一处架构冲突**（DynamicSeam 自带 MIG）。一张图就把「为什么这个项目不能一键综合」可视化了出来。

**预期结果**：得到一张带时钟域标注与状态图例（✅/⚠️/❓）的系统总图。本实践为「源码阅读型实践」，不需运行命令。

#### 4.1.5 小练习与答案

**练习 1**：软件阶段 ⑥（曝光补偿）在硬件系统里对应 README 抱怨的哪个问题？
> **答**：对应 README 第 3 行「图像采集模块没有做得怎么好，白平衡之类的」。曝光补偿与白平衡本质都是**让多路摄像机的亮度/色温一致**，软件用 `ExposureCompensator` 估增益，硬件采集端应做白平衡，但两处都没做好。

**练习 2**：为什么说 `mem_burst` 是系统里「唯一干净的积木」？它把什么复杂性封装掉了？
> **答**：`mem_burst` 把 MIG 应用层 `app_*` 接口的「一次一条命令、自管 `app_en`、自递增地址、自数数据、写操作还要单独喂数据」的底层复杂性，封装成「起始地址 + 长度」的批量突发接口（见 u3-l1）。其他模块只要按 `rd_burst_req/wr_burst_req` 握手就能用 DDR3，不必各自重写一套 MIG 驱动。

**练习 3**：系统里跨时钟域的点有几处？分别用什么手段处理？
> **答**：主要有两处明确的：① 摄像头采集域 → DDR3 域，用 24→64 异步 FIFO（但因指针 +3/+8 非线性，格雷码失效，改用电平触发，见 u3-l3）；② DDR3 域内 `sys_clk` 与 `ui_clk` 不同频，跨域需同步（见 u5-l2）。UART 收发器内部还有一处单 bit 跨域，用两级同步寄存器（u1-l3）。

---

### 4.2 资源取舍：插值方法、存储结构与 Block RAM 占用

#### 4.2.1 概念说明

FPGA 设计是「戴着镣铐跳舞」——芯片面积（资源）和图像质量是一对永恒的矛盾。本项目 README 用了整整两段话讲这个矛盾，集中体现在三个决策点上：

1. **投影用哪种插值**：双线性（质量好、资源大）还是最近邻（简单、质量差）。
2. **双线性要取四个相邻像素，怎么取**：四份相同 Block RAM（资源大、简单）还是奇偶行列分存（资源省、时序难）。
3. **24→64 位宽转换的存储体用什么**：Block RAM（用不了）还是二维寄存器数组（能用、但写端频率受限）。

这三个决策**互相耦合**：决策 1 选双线性会放大决策 2 的资源压力；决策 3 因为位宽比非 2 的幂被迫弃用 Block RAM，又把存储压力转嫁给了寄存器（LUT/FF）。理清这条耦合链，是「资源取舍」这一模块的核心。

#### 4.2.2 核心流程

**（a）双线性 vs 最近邻：质量与资源的权衡**

双线性插值用浮点坐标周围的 4 个整数邻居按距离加权（四权重之和为 1，见 u2-l3），输出平滑；最近邻插值直接取最近的 1 个整数像素，输出有锯齿但只需取 1 个像素。

关键矛盾在硬件：双线性要求**一个时钟周期内同时取出 4 个像素**。DDR3 突发读是顺序的，无法一拍给 4 个任意地址，所以必须先把像素搬到片上 RAM，再并行读 4 个端口。

README 把这件事讲得很直白（[README.md:12-14](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L12-L14)）：双线性有两种实现，一是「四个相同的 Block RAM」（资源大、简单），二是「奇偶行奇偶列存到不同 Block RAM」（仿真通过、**下板时序不对**），作者最终用第一种；最近邻「简单实用，工程应用推荐」。

**（b）双线性取四像素：两种存储结构**

```
方案A：四份相同 BRAM（作者采用）         方案B：奇偶行/列分四块 BRAM
┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐    ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐
│BRAM0 │ │BRAM1 │ │BRAM2 │ │BRAM3 │    │偶行偶列│ │偶行奇列│ │奇行偶列│ │奇行奇列│
│=全图 │ │=全图 │ │=全图 │ │=全图 │    └───┬────┘ └───┬────┘ └───┬────┘ └───┬────┘
└──┬───┘ └──┬───┘ └──┬───┘ └──┬───┘        4个不同地址各取1像素 → 拼成2×2邻域
   addr0   addr1   addr2   addr3
   4个不同地址各取1像素 = 2×2邻域

资源：×4（每份存整图）                 资源：×1（四块合起来存一份整图）
时序：4 端口独立，简单                 时序：地址须按奇偶拆分，下板时序不对
```

方案 A 用 4 倍存储换时序简单——这是典型的「**拿资源换可行性**」决策。

**（c）24→64 位宽转换：Block RAM 为何用不了**

这条决策链是全项目最精妙的资源推理，README 一句话点透（[README.md:7](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L7)）：

- 摄像头像素 24bit，DDR3 接口 64bit，要做异步 FIFO 位宽转换。
- Block RAM 的读写端口位宽比必须是 2 的幂（如 1:2、1:4、1:8）。
- 24:64 = 3:8，含因子 3，**不是 2 的幂比**，Block RAM 物理上做不到。
- 于是存储宽度取 \(\gcd(24,64)=8\)bit（[README.md:8](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L8)），写端指针一次 +3、读端一次 +8。
- 既然 Block RAM 用不了，改用**二维寄存器数组**。代价：寄存器比 BRAM 慢，写端 200MHz 出错、100MHz 可用（[README.md:7](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L7)）。
- 又因为指针 +3/+8 非线性，经典格雷码防亚稳态失效，改用「写端驱动读端」的电平触发（[README.md:9](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L9)）。

这是一条由「数学约束（位宽比）」级联到「物理约束（频率）」再到「设计约束（CDC 方案）」的完整推理链。

**（d）资源耦合总览**

把三个决策放一起看：

| 决策点 | 选项 A | 选项 B | 项目选择 | 主要代价 |
|--------|-------|-------|---------|---------|
| 投影插值 | 双线性（4 像素加权） | 最近邻（1 像素） | project 实为最近邻（简化） | 质量降级为锯齿 |
| 双线性取四像素 | 4 份相同 BRAM | 奇偶行列分存 | 4 份相同 BRAM | Block RAM ×4 |
| 24→64 FIFO 存储体 | Block RAM | 二维寄存器数组 | 二维寄存器数组 | LUT/FF 暴涨、写频限 100MHz |
| FIFO 跨域方案 | 格雷码指针 | 写端驱动读端 | 写端驱动读端 | 设计复杂、无标准 IP |

#### 4.2.3 源码精读

**（a）README 把权衡写在了哪里**

三条核心自述集中在 README 第 7–14 行，逐条对应上面的决策：

- Block RAM 不可用、寄存器数组、200MHz/100MHz：[README.md:7](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L7)。
- width=8bit、depth 自定、须 2 的幂、编译时间：[README.md:8](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L8)。
- 读指针 +3、写指针 +8、格雷码失效、写端驱动读端：[README.md:9](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L9)。
- 双线性两种实现、奇偶方案下板时序不对、最近邻推荐：[README.md:13-L14](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L13-L14)。

**（b）硬件 project 模块为何「实为最近邻」**

`project` 模块算出定点源坐标 `x/y`（[圆柱面投影.v:90-92](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L90-L92)），然后只取了整数坐标：

```verilog
weight_y00 = y[55:30];       // floor
weight_x00 = x[55:30];       // floor
weight_y01 = y[55:30] + 1;   // ceil
...
```

这段在 [圆柱面投影.v:93-100](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L93-L100)。注意名字虽叫 `weight_*`，实际赋的是 **floor/ceil 整数坐标**（即软件里的 addr 地址表，见 u2-l3/u2-l4），**真正的小数权重（坐标小数部分）被完全丢弃**。而且模块还跳过了软件 `mapBackward` 里的透视除法 `x /= z`（[圆柱面投影.cpp:65](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L65)）。两个简化叠加，`project` 实际输出的是「取整后的最近邻坐标」，不是双线性——这正是「资源取舍」落到代码上的直接证据：**为了不取四像素，干脆只取一像素**。

**（c）软件 weight 表是双线性的「正确标尺」**

软件 `warp` 函数手写了双线性 weight 表（[圆柱面投影.cpp:198-205](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L198-L205)），并按四权重加权求和（[圆柱面投影.cpp:239-254](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L239-L254)）。这是硬件想恢复双线性时要对标的标准。注意 u2-l3 已指出该表后两项的 y 因子存在错位（右列互换），求和虽仍为 1 但有轻微瑕疵——这也是 README「小数权重没做好」在软件侧的伏笔。

**（d）为什么投影时序最难**

`project` 把整条「算角度 → CORDIC → 三路矩阵乘 → 取邻居坐标」**全部塞进一个 `always @(posedge clk)` 的阻塞赋值链**（[圆柱面投影.v:72-111](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L72-L111)）。阻塞赋值在综合时意味着「这一拍内必须完成全部组合逻辑」，于是 CORDIC 延迟 + 三个乘法 + 三个加法 + 取整串成一条长组合路径，关键路径极长，时序难收敛。这就是 README 第 15 行「圆柱面投影的时序问题……最难」的微观根源（详见 u5-l1）。

#### 4.2.4 代码实践：Block RAM 占用估算

**实践目标**：用真实图像尺寸，手算「方案 A（四份相同 BRAM）」要吃掉多少 Block RAM，体会资源压力。

**操作步骤**：

1. 从软件代码读出图像尺寸。`warp` 里 `addr.create(1100, 1086*8, ...)`（[圆柱面投影.cpp:167](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L167)），可见单幅投影图约 1086 × 1100 像素，每像素 24bit（RGB 各 8bit）。
2. 算单幅图字节：\(1086 \times 1100 \times 3 \text{ 字节} \approx 3.58\text{ MB}\)。
3. 方案 A 要把整幅图复制 4 份进 4 块 BRAM：\(3.58 \times 4 \approx 14.3\text{ MB}\)。
4. 7 系列典型 Block RAM（RAMB36）每块 36Kbit = 4.5KB。所需块数 \(\approx 14.3\text{MB} / 4.5\text{KB} \approx 3188\) 块。
5. 对照一款主流器件：Spartan-7 XC7S50 约 60 块、Artix-7 XC7A100T 约 135 块、Kintex-7 XC7K325T 约 445 块——**没有任何一款消费级 7 系列装得下 3000 多块**。

**需要观察的现象**：即便只算单幅图、单路摄像头，方案 A 的 Block RAM 需求也远超中端器件容量。这解释了为什么作者说「片上资源占用率很大」，以及为什么投影模块最终简化成了最近邻。

**预期结果**：写下一行结论——「全分辨率双线性在 BRAM 上不可行，必须降采样、分行缓存或回退到最近邻」。本实践为「估算型实践」，数字为粗略估算，**待本地用具体器件手册精确核算**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 24→64 的异步 FIFO 不能直接用 Block RAM？请用一句话说清数学原因。
> **答**：Block RAM 要求读写端口位宽比为 2 的幂，而 24:64 = 3:8 含因子 3，不满足，所以物理上无法配置出这种端口比。

**练习 2**：方案 B（奇偶行列分存）「仿真通过、下板时序不对」，请猜测时序不对的可能原因。
> **答**：奇偶拆分后，四个 BRAM 的读地址要由同一浮点坐标实时算出奇偶并分别译码，地址生成逻辑深、走线不均衡，四个端口的读出数据**到达时间不一致**，导致采样窗口错位；仿真忽略布线延迟，故通过，下板则失败。

**练习 3**：`project` 模块如果想让结果更接近软件双线性，最少要补回哪两件事？
> **答**：① 补回透视除法 `x/=z; y/=z`（[圆柱面投影.cpp:65](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L65)）；② 保留 `x/y` 的小数部分（当前只取了 `x[55:30]` 整数位，[圆柱面投影.v:93-94](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L93-L94)），用于生成四个邻居的双线性权重。

---

### 4.3 遗留问题改进：从「半成品」到「可量产」的工程路径

#### 4.3.1 概念说明

读开源硬件项目，有一项关键能力是**识别遗留问题并给出改进路径**。本项目作者在 README 里坦率列出了两条「做得不好」（[README.md:2-4](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L2-L4)），前几讲我们又从代码里挖出了几条结构性的遗留。汇总如下：

| # | 遗留问题 | 来源 | 严重度 |
|---|---------|------|-------|
| 1 | 采集模块白平衡没做好 | README L3 | 中（影响色彩一致性） |
| 2 | 融合的小数权重没做好 | README L4 | 高（影响拼接缝可见度） |
| 3 | `DynamicSeam` 不可综合 | u4-l1 代码分析 | 致命（缝合线根本跑不了） |
| 4 | 24→64 异步 FIFO 代码未收录 | u3-l3 | 高（采集入口缺失） |
| 5 | `project` 丢弃小数权重、跳过透视除法 | u2-l4/u5-l1 | 高（投影退化为最近邻） |

「改进」不是空谈，要落到**具体硬件结构**：用什么存储、什么握手、哪个时钟域、占多少资源。本模块针对其中三个最影响画质与可用性的问题——**小数权重融合、白平衡采集、插值方法选择**——各给出一个可落地的 FPGA 思路。

#### 4.3.2 核心流程

**改进一：小数权重融合（对应遗留 #2、#5）**

现状：`project` 只输出 floor/ceil 整数坐标（[圆柱面投影.v:93-100](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L93-L100)），小数权重被丢；融合模块未实现。改进思路是把定点坐标的**小数部分**留下来当权重：

1. `x/y` 是 56 位、30 位小数（u5-l1）。`x[55:30]` 是整数 floor，则 `x[29:0]` 就是纯小数 \(f_x \in [0,1)\)。
2. 取 \(f_x\) 的高 8 位作为定点权重 \(w_x\)（精度足够、省资源）：\(w_x = x[29:22]\)。
3. 四个双线性权重按软件公式（对照 [圆柱面投影.cpp:198-205](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L198-L205)）：

\[
\begin{aligned}
w_{00} &= (1-f_x)(1-f_y) \quad &\text{(左上)}\\
w_{01} &= (1-f_x)\,f_y         \quad &\text{(左下)}\\
w_{10} &= f_x\,(1-f_y)         \quad &\text{(右上)}\\
w_{11} &= f_x\,f_y             \quad &\text{(右下)}
\end{aligned}
\]

4. 用 4 块 BRAM（方案 A）或双端口 BRAM 分两拍取四个邻居像素 \(p_{00},p_{01},p_{10},p_{11}\)，加权求和：

\[
p = \sum w_{ij}\cdot p_{ij}
\]

5. 注意**修正软件 weight 表后两项 y 因子错位**（u2-l3 指出），否则缝边会有轻微色偏。

**改进二：白平衡采集（对应遗留 #1）**

现状：采集模块未收录，README 抱怨白平衡没做。改进思路用最简单的**灰度世界法（Gray World）**：假设场景各通道平均亮度应相等。

1. 在采集路径（24→64 FIFO 之前）插一级「统计 + 增益」模块。
2. 统计：用三个累加器分别累加一帧的 R、G、B 之和（24bit 像素拆成三个 8bit 通道），帧末得到 \(S_R, S_G, S_B\)。
3. 求增益：\(g_R = \bar{G}/\bar{R}\)、\(g_B = \bar{G}/\bar{B}\)，其中 \(\bar{C}=S_C/\text{像素数}\)。除法用查表（LUT）或迭代，每帧只算一次。
4. 应用：下一帧每个像素的 R、B 通道乘 \(g_R/g_B\)（定点乘法 + 截断），G 不变。
5. 七路摄像头各自独立做一次，保证多机色彩一致，拼接缝处不出现色差。

**改进三：插值方法选择（对应遗留 #5、资源取舍）**

现状：全图统一用最近邻（锯齿）或双线性（资源爆炸）都不可取。改进思路是**分区插值**：

1. **重叠区/缝合线附近**用双线性——这是人眼最敏感、最容易出现锯齿和色带的地方，值得花资源。
2. **非重叠区（单图独占）**用最近邻——这些区域无拼接缝，锯齿不明显，省下 BRAM。
3. 实现上用一个 `region` 选择信号：缝合线 mask（来自 DynamicSeam 的结果）置 1 时走双线性通路，置 0 时走最近邻通路，输出二选一。
4. 进一步省资源：双线性不预存整图，而用**两行 BRAM 行缓存**（line buffer）流式取邻居——只需要存两行像素而非整图，BRAM 占用从「整图×4」降到「两行」。

#### 4.3.3 源码精读

**（a）README 的遗留问题原文**

两条「做得不好」紧挨在 README 开头（[README.md:2-4](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L2-L4)）：

```
1.图像采集模块没有做得怎么好，白平衡之类的；
2.图像融合部分的小数权重没有怎么做好，后面补上。
```

这两条是本模块改进一、改进二的直接依据。「后面补上」说明作者自己也知道这是未完工项。

**（b）DynamicSeam 的致命写法（遗留 #3）**

`DynamicSeam.v` 文件顶部注释即声明这是「外部代码不能综合」（[DynamicSeam.v:21-23](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L21-L23)）。代码里至少三处致命写法（u4-l1 详析）：

- **MIG 例化写进了 `always` 块**：`mig_7series_0 u_mig_7series_0 (...)` 出现在 `always@(posedge sys_clk)` 内部的 `READ` 分支里（[DynamicSeam.v:192-232](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L192-L232)）。模块例化是**模块级**语句，不能放进过程块，综合直接报错。
- **`parameter` 当变量自增**：`parameter row = 0; parameter col = 0; parameter read_col = 0;`（[DynamicSeam.v:57-59](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L57-L59)），随后在 `always` 里 `row <= row + 1`（[DynamicSeam.v:142-150](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L142-L150)）。`parameter` 是编译期常数，不可在运行时赋值。
- **`localparam` 在 `always` 内声明并赋值**：`localparam index = coordinate[col]; localparam min <= row2[index];`（[DynamicSeam.v:248-249](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L248-L249)），既不能在过程块内声明，也不能用 `<=`。

**（c）DynamicSeam 的架构级错误：自带 MIG**

除了语法，DynamicSeam 在**系统集成**上也犯了错：它没有走 `mem_burst` 这条统一存储入口，而是自己例化了一颗完整的 `mig_7series_0`（[DynamicSeam.v:192-232](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L192-L232)）。后果是：系统里若投影和缝合线都要用 DDR3，就会出现**两颗 MIG 抢同一组物理 DDR3 引脚**的冲突——物理引脚只能由一颗 MIG 驱动。正确的架构是：**全局唯一一颗 MIG**，所有模块通过 `mem_burst`（或一个仲裁器）分时共享。此外例化处 `ddr3_ck_n` 端口名损坏（[DynamicSeam.v:195](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L195)），`clk_in1_p` 误写成 `cl_p`（[DynamicSeam.v:46](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L46)），接线本身也连不通。

**（d）project 丢弃小数权重的代码点**

改进一的依据就在这里。`x/y` 的整数位是 `[55:30]`，那么小数位就是 `[29:0]`，但代码完全没用它（[圆柱面投影.v:93-100](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L93-L100)）。改进时只需把 `x[29:22]`、`y[29:22]` 引出当权重即可，**不需要重算坐标**——这是最低成本的升级点。

#### 4.3.4 代码实践：撰写系统设计改进建议

**实践目标**：把本模块的三个改进思路，写成一份能给团队评审的「系统设计改进建议」，要求每个方向都有具体的硬件结构、数据流和资源估算。这是本讲的主实践任务。

**操作步骤**：

请针对以下三个方向，各写一段不少于 150 字的改进建议，每段必须包含：① 现状与问题（引用源码行号或 README 行号）；② 改进架构（用什么存储、什么握手、哪个时钟域）；③ 资源/质量代价估算。

1. **方向一：小数权重融合**
   - 现状依据：[圆柱面投影.v:93-100](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L93-L100)（丢弃小数）、[README.md:4](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L4)（作者自述）。
   - 提示：参考 4.3.2 改进一，写出 \(f_x/f_y\) 的提取位段、四权重公式、取邻居像素的存储方案（行缓存 vs 四份 BRAM）。

2. **方向二：白平衡采集**
   - 现状依据：[README.md:3](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L3)。
   - 提示：参考 4.3.2 改进二，写出灰度世界法的「统计—求增益—应用」三段流水，以及它插入在数据通路的哪个位置（24→64 FIFO 之前还是之后）。

3. **方向三：插值方法选择**
   - 现状依据：[README.md:13-14](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L13-L14)、[圆柱面投影.v:90-92](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L90-L92)。
   - 提示：参考 4.3.2 改进三，写出「重叠区双线性 + 非重叠区最近邻」的分区选择逻辑，以及行缓存如何把 BRAM 占用从整图降到两行。

**需要观察的现象**：写完后自检——每个方向是否都给出了**可综合的硬件结构**（不是「用 OpenCV 算一下」），是否标注了**时钟域**，是否估算了**资源或质量的得失**。

**预期结果**：一份三段式的改进建议文档。本实践为「设计型实践」，无需运行命令；具体资源数字标注「待本地核算」即可。

#### 4.3.5 小练习与答案

**练习 1**：改进一里，为什么取 `x[29:22]` 这 8 位当权重，而不是用全部 30 位小数？
> **答**：30 位小数精度远超人眼可辨（\(2^{-30}\approx 10^{-9}\)），且 30×30 位乘法占大量 DSP。取高 8 位（\(2^{-8}\approx 0.004\) 精度）已足够双线性权重使用，乘法降到 8×8 位，资源大幅节省，是典型的「按需定精度」。

**练习 2**：灰度世界法白平衡为什么要在「帧间」而不是「帧内」应用增益？
> **答**：增益 \(g_R/g_B\) 要先用**整帧**的 R/G/B 之和算出，当前帧还没累加完时增益未知，所以只能「上一帧统计、下一帧应用」。这是统计型算法固有的**一帧延迟**，对静态全景拼接可接受，对高速运动场景需改用分区统计或固定增益。

**练习 3**：DynamicSeam 的「自带 MIG」为什么是架构错误，而不仅仅是语法错误？
> **答**：DDR3 的物理引脚（`ddr3_dq/ddr3_addr/...`）是全局唯一资源，只能由一颗 MIG 驱动。DynamicSeam 自己例化 MIG，意味着它和投影模块的 MIG 会抢同一组引脚，综合时引脚冲突；即便不冲突，两颗 MIG 也无法共享同一片 DDR3 地址空间。正确做法是全局一颗 MIG + 一个仲裁器，所有模块经 `mem_burst` 分时访问。

---

## 5. 综合实践

把本讲三节的内容串成一份**完整的工程评审报告**，假设你是接手这个项目的下一位工程师，要向上级汇报「这套系统能不能量产、还差什么、怎么补」。

报告需包含四个部分：

1. **系统现状总图**（对应 4.1）：复制并完善 4.1.4 的数据通路图，用三种颜色标注「✅已实现 / ⚠️草稿 / ❓未收录」，列出所有时钟域与跨域点。
2. **资源账本**（对应 4.2）：基于 4.2.4 的估算方法，列出「投影（双线性 vs 最近邻）」「24→64 FIFO（寄存器数组）」「缝合线 DP 数组」三个模块各自的 LUT/FF/BRAM/DSP 粗估，得出「现状能否装进某款具体器件」的结论（器件自选，结论标「待本地核算」）。
3. **返工清单**（对应 4.3）：把 4.3.1 表格里的五条遗留问题按「致命/高/中」排序，每条给出「修复所需工时（人天）粗估」和「修复后收益」。至少包含：DynamicSeam 重写为可综合、project 补回透视除法与小数权重、补 24→64 FIFO、补白平衡、补融合。
4. **最小可行路径**：如果只有两周时间，你会优先修哪两项、为什么，并给出修完后的验证方法（建议复用 `mem_test.v` 的「写后读回」思路做自检）。

报告写完后，回头对照 README 作者的话（[README.md:17](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L17)「七路摄像头拼接还是不容易的，即便现在还要好多路要走」），评估你的返工清单是否覆盖了作者隐含的「好多路」。本实践为「综合设计型实践」，全程无需运行命令，但每条结论必须有源码或 README 行号支撑。

## 6. 本讲小结

- **系统是时分复用的单像素流水线**：七路并行采集，但投影之后逐像素串行处理；受 DDR3 带宽限制，必须按光栅扫描流式处理，这是时序难点的总根源。
- **软件八阶段一分为二**：①–④标定（一次性、烧成 `coe`/`k_inv` 定点常数），⑤⑦⑧实时（FPGA 加速对象）；⑥曝光补偿对应白平衡遗留问题。
- **三个资源耦合决策**：插值选最近邻（质量降级）→ 双线性取四像素要四份 BRAM（资源爆炸）→ 24→64 FIFO 因位宽比含因子 3 弃用 BRAM 改用寄存器数组（写频限 100MHz），三者级联。
- **`mem_burst` 是唯一干净的存储积木**：封装 MIG 应用接口为突发协议，本应作为全局唯一存储入口；DynamicSeam 自带 MIG 是架构错误。
- **遗留问题分三档**：致命（DynamicSeam 不可综合）、高（小数权重丢弃、FIFO 未收录、透视除法缺失）、中（白平衡），每条都有对应的可落地 FPGA 改进路径。
- **改进的最低成本切入点**：`project` 模块的小数权重只需引出 `x[29:22]/y[29:22]` 即可恢复，不需重算坐标——这是性价比最高的升级。

## 7. 下一步学习建议

本讲是全册收尾，你已经把 ImageStitchBasedOnFPGA 从零件读到了系统。要继续深入，建议三个方向：

1. **动手重写一个可综合的最小投影模块**：以 `圆柱面投影.v` 为蓝本，补回透视除法与小数权重，用「两行 BRAM 行缓存」取邻居，目标是通过 Vivado 综合（不要求下板）。这会逼你真正消化 u5-l1 的定点推导与本讲的资源取舍。
2. **研读 OpenCV `stitching` 模块源码**：本项目 `.cpp` 只是 OpenCV 流水线的薄封装。去读 `opencv/modules/stitching/src/` 下的 `warpers.cpp`、`seam_finders.cpp`、`blenders.cpp`，理解 GraphCut 缝合线与 FeatherBlender 的真实算法，再回头看 DynamicSeam 的 DP 草稿与软件的差距。
3. **学习 DDR3 视频系统的标准范式**：本项目用 `mem_burst` 手搓突发，业界更常见的是 Xilinx 官方的 **Video Frame Buffer**（AXI4-Stream + VDMA/MM2S+S2MM）。对照学习 VDMA 如何用标准 AXI 接口做帧缓存，能帮你理解「为什么 DynamicSeam 自带 MIG 是错的」「标准做法应该长什么样」。

读到这里，你已经具备独立阅读一个「半成品 FPGA 项目」、识别其架构与遗留问题、并给出改进路径的能力——这比读一个完美工程更能锻炼真实工程判断力。
