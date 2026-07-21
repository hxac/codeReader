# 项目背景与目标：星载 SAR 船舶检测

## 1. 本讲目标

本讲是整本学习手册的第一篇，面向**完全不了解本项目**的读者。读完本讲后，你应当能够：

- 说清楚这个项目**到底在解决什么问题**：为什么要把人工智能放到卫星上、为什么要用 SAR（合成孔径雷达）去检测船舶。
- 解释**为什么本项目选择 FPGA（Kria KV260）而不是 GPU**，理解功耗、体积、星载环境带来的硬约束。
- 复述项目在论文中给出的**两项关键对比指标**：与最先进 GPU 模型相比的精度差距，以及计算效率的倍数优势。
- 认识 **xView3-SAR 数据集**的三通道输入（VV、VH、bathymetry）与目录组织方式，为后续数据准备讲义建立直觉。

本讲**不涉及任何代码细节**，只建立全局认知。后续单元才会逐步进入源码。

---

## 2. 前置知识

本讲假设你具备以下常识即可，不需要任何 FPGA 或深度学习经验。

- **什么是遥感图像**：卫星或飞机从高空拍下的地球表面图像。和手机照片不同，遥感图像常常非常大（上亿像素）、可能是多通道的（不止 RGB）。
- **什么是机器学习模型**：简单理解为「从大量数据中学习规律、然后对新数据做预测」的程序。本项目用的是一类专门做「目标检测」的模型（YOLOv8），它能在图里框出船舶的位置。
- **GPU 与 FPGA 的通俗区别**：
  - **GPU（图形处理器）** 算力强、灵活，但功耗高、体积大，常见于数据中心和桌面电脑。
  - **FPGA（现场可编程门阵列）** 是一种可以通过编程改变内部电路的芯片，功耗低、可定制，常用于嵌入式和边缘设备。
- **功耗（瓦特 / W）**：设备运行时消耗的电力。卫星上的电力来自太阳能板和电池，非常有限，所以「每瓦能做多少计算」比「绝对算力」更重要。

> 如果你已经熟悉以上概念，可以直接跳到第 3 节。

---

## 3. 本讲源码地图

本讲只涉及「项目大门」级别的文件，作为后续所有讲义的导航。

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md) | 项目总说明：动机、方法、核心成果指标、仓库结构、论文引用。是本讲最主要的精读对象。 |
| [assets/multi_panel_v2.jpg](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/assets/multi_panel_v2.jpg) | 多面板示意图：展示 xView3-SAR 数据集里的 SAR 场景、船舶真值标注，以及 VV/VH/bathymetry 三通道输入的样子。 |
| [dataset/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/README.md) | 数据集下载与目录组织说明。本讲用它来认识 xView3-SAR 的结构。 |

> 说明：本手册中所有形如 `[文件名](长链接)` 的链接都指向**当前 HEAD（`a318ec9`）的固定版本**，即使仓库日后更新，你点开的也是本讲编写时的同一份代码。

---

## 4. 核心概念与源码讲解

本讲包含三个最小模块：

- **4.1 项目定位与动机**（为什么要把 ML 放到卫星上）
- **4.2 xView3-SAR 数据集简介**（我们用什么数据训练和评测）
- **4.3 KV260 与功耗约束**（为什么是 FPGA 而不是 GPU）

---

### 4.1 项目定位与动机

#### 4.1.1 概念说明

这个项目要解决的核心问题是：**让卫星在天上就能快速分析自己拍到的雷达图像，自动找出海上的船舶。**

为什么要这么做？传统流程是：

1. 卫星拍下图像；
2. 等到卫星飞过地面站（ground station）时，把图像传回地面；
3. 地面服务器跑 AI 模型分析；
4. 把结果反馈给用户。

问题在于第 2 步——卫星和地面站之间的连接是**断断续续**的（intermittent connectivity），可能要等几小时甚至更久才能传数据。对于海事安全这类**对时间极度敏感**（time-sensitive）的应用，这种延迟往往不可接受。

> 直觉一句话：**与其等图像传回地面再分析，不如让卫星在轨道上「就地」分析，几分钟内就能出结果。** 这就是「星载机器学习」（on-satellite ML / on-board ML）的动机。

但星载 ML 有个硬骨头：**最先进的 AI 模型通常又大又耗电**，卫星上根本带不动。本项目就是要在「精度尽量不掉」的前提下，把模型瘦身到能放进一颗低功耗芯片里。

#### 4.1.2 核心流程

整个项目的逻辑链条可以这样理解：

```text
应用需求            技术障碍                      本项目的应对
─────────          ─────────────                ──────────────
海事安全需要       卫星↔地面站连接断续，          把分析放到卫星上
快速获知船舶位置   传图延迟长达数小时            （on-satellite ML）
    │
    └─► 但 SOTA 模型太大、太耗电 ─► 用 FPGA + 模型优化，压到 <10W
                                          │
                                          └─► 在 xView3-SAR 上验证：
                                              精度只低 ~2%/3%，效率高 ~50×/2500×
```

关键的技术选择有三个，后续每个单元都会展开：

1. **任务**：用 SAR（而不是光学相机）做船舶检测，因为 SAR 能全天候、全天时成像（见 4.2）。
2. **模型**：基于 YOLOv8 做一系列架构改造，专门适配 SAR 小目标 + FPGA。
3. **硬件**：选 Kria KV260 MPSoC（一颗低功耗 FPGA 平台），把模型压到 <10W 运行（见 4.3）。

#### 4.1.3 源码精读

项目的全部动机都浓缩在 README 开头的三段话里。我们逐段来看。

**第一段：点出「星载 ML」的机遇与矛盾。**

[README.md:L3-L3](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L3-L3) —— 这段话说明：对卫星图像做「几分钟到几小时内」的快速分析越来越重要，而星载 ML 能绕开卫星到地面站的连接延迟，但**最先进的模型往往太大、太耗电**，无法部署到星上。这就是本项目存在的根本理由。

**第二段：把「SAR 船舶检测」作为典型的时间敏感场景。**

[README.md:L5-L5](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L5-L5) —— 这段话指出船舶检测是海事安全中**典型的时间敏感应用**；并直接点明以往工作的三类不足：模型太大、没针对低功耗硬件、只在过小的数据集上测过。本项目要同时解决这三点。

**第三段：给出方法与核心成果指标（最重要的一段）。**

[README.md:L7-L7](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L7-L7) —— 这一段几乎包含了本讲所有「数字结论」，请重点记住：

- 部署平台：**Kria KV260 MPSoC**；
- 性能：能在 **1 分钟内**分析一张约 **7 亿像素（~700 megapixel）** 的 SAR 图像；
- 功耗：在常见卫星功耗约束 **<10W** 之内；
- 精度：检测与分类性能仅比最先进的 GPU 模型低 **~2% 和 3%**；
- 效率：计算效率高出约 **~50 倍和 ~2500 倍**；
- 数据集：在最大、最具多样性的开源 SAR 船舶数据集 **xView3-SAR** 上评测。

#### 4.1.4 代码实践

> 这是本讲的主实践任务，也是 manifest 指定的实践。

**实践目标**：用你自己的话把「为什么不用 GPU 而用 FPGA」讲清楚，并准确复述论文的两项关键对比指标。这个练习看似没有代码，但它检验你是否真正读懂了 README——后续每一篇讲义都建立在这个全局认知之上。

**操作步骤**：

1. 打开并通读 [README.md:L1-L10](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L1-L10)。
2. 在笔记里写一段 **不超过 200 字** 的中文摘要，必须包含以下要素：
   - 一个**应用场景**（海事安全 / 时间敏感）；
   - 一个**星载 ML 的障碍**（连接延迟 / 模型太大太耗电）；
   - 一个**硬件选择**（KV260、<10W）；
   - **两项关键对比指标**：精度差距（检测 ~2%、分类 3%）与效率倍数（~50×、~2500×）。

**需要观察的现象**：写完后自查——如果你的摘要里出现了「GPU 更精确所以更好」之类的反向结论，说明你把对比指标搞反了。本项目的主张是：**在几乎不掉精度的前提下，FPGA 的效率远高于 GPU**。

**预期结果**：一段类似下面这样的摘要（仅作格式参考，请用自己的话写）：

> 本项目针对海事安全中对时效敏感的船舶检测需求，把 SAR 图像分析放到卫星上做（on-satellite ML），以规避卫星到地面站的传输延迟。由于最先进模型太大太耗电，项目选用低功耗的 Kria KV260 FPGA（<10W）部署改造后的 YOLOv8。在 xView3-SAR 上，其检测与分类精度仅比 SOTA GPU 模型低约 2% 与 3%，但计算效率分别高出约 50× 与 2500×。

**待本地验证**：本实践无需运行任何命令，纯阅读与写作。如果你的摘要涵盖了上述 5 个要素，即视为完成。

#### 4.1.5 小练习与答案

**练习 1**：本项目选择「星载 ML」最主要是为了解决什么问题？

> **参考答案**：解决卫星图像传回地面分析时，因卫星与地面站连接断续而造成的**数小时级延迟**，使海事安全等时间敏感应用能在「几分钟到几小时」内得到结果。

**练习 2**：README 第二段（[L5](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L5-L5)）提到以往 SAR 船舶检测的 ML 模型有哪三类不足？

> **参考答案**：①模型太大、无法星载部署；②没有针对足够低功耗的硬件开发；③只在规模过小、不足以代表真实任务难度的 SAR 数据集上测试过。

---

### 4.2 xView3-SAR 数据集简介

#### 4.2.1 概念说明

要训练和评测一个船舶检测模型，首先要有大量「带标注的 SAR 图像」。本项目用的是 **xView3-SAR** 数据集——README 里称它是**最大、最具多样性的开源 SAR 船舶数据集**。

这里需要先理解 **SAR（Synthetic Aperture Radar，合成孔径雷达）** 是什么：

- 普通光学卫星相机靠太阳光拍照，**晚上或云层厚重时就拍不了**。
- SAR 是一种**主动式雷达**：它自己向地面发射微波，再接收回波成像。所以 SAR **能全天候、全天时**工作——这对海事监测尤其重要（船不会因为天黑就停下来）。

xView3-SAR 的图像来自欧空局（ESA）的 **Sentinel-1** 卫星（见 [README.md:L10-L10](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L10-L10) 的图注）。每张场景不是一张普通照片，而是**三个通道**的组合：

| 通道 | 含义 |
| --- | --- |
| **VV** | 垂直发射、垂直接收的雷达极化信号，反映地表对 VV 极化波的反射强度。 |
| **VH** | 垂直发射、水平接收的交叉极化信号，常对船舶等金属目标更敏感。 |
| **bathymetry** | 水深数据，提供海域深度信息，帮助模型区分近岸/远海环境。 |

你可以在 [assets/multi_panel_v2.jpg](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/assets/multi_panel_v2.jpg) 里直观看到这三个通道的样子，以及船舶真值标注的位置。

> 直觉一句话：**模型看一张「SAR 场景」，其实是在看三张配准好的图（VV、VH、水深）叠加在一起。** 这和普通三通道 RGB 图像的「形状」类似，但物理含义完全不同——后续训练讲义（u3-l2）会专门讲怎么把它归一化成模型能消化的形式。

#### 4.2.2 核心流程

数据集的组织方式在 [dataset/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/README.md) 里有明确的目录树约定，三大子目录各司其职：

```text
xView3-SAR/
├── data/        <- SAR .tif 场景（每个场景含 VV/VH/bathymetry 等 .tif）
│   ├── training/
│   ├── validation/
│   └── public/
├── labels/      <- 检测标注 .csv（scene_id, 坐标, 是否船舶, 是否渔船 等）
│   ├── training.csv
│   ├── validation.csv
│   └── public.csv
└── shoreline/   <- 海岸线 .npy 坐标（用于近岸/远海筛选）
    ├── training/
    ├── validation/
    └── public/
```

数据流向是这样的：

1. 从 xView3-SAR 竞赛页面下载 `.tar.gz` 场景包并解压，得到上面的目录结构。
2. 原始场景**非常大**（单张可达上亿像素），YOLOv8 无法直接吃整张图，所以要用 `generate_xview3.py` 把场景**裁剪成固定大小的芯片（chips，如 800×800）**，并生成对应的 YOLO 格式标签。
3. 这些芯片才是真正喂给训练 / 推理流程的输入。

> 裁剪与标签生成的细节是单元 2（数据准备）的主题，本讲只需建立「原始场景 → 裁剪芯片 → 训练输入」的整体印象。

#### 4.2.3 源码精读

**数据集目录约定。**

[dataset/README.md:L7-L35](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/README.md#L7-L35) —— 这段目录树明确划分了 `data`（SAR 场景）、`labels`（CSV 标注）、`shoreline`（海岸线 npy）三类资产，以及 `training / validation / public` 三种划分。注意 `data` 目录下的场景文件夹名带有后缀字母（如 `...t`、`...v`、`...p`），分别对应 train/val/public。

**三通道输入的可视化说明。**

[README.md:L9-L10](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L9-L10) —— README 用 `multi_panel_v2.jpg` 这张图，并配文字说明：图里展示的是 xView3-SAR 数据集中 Sentinel-1 图像的船舶检测示例，包括**船舶真值位置**和**多通道模型输入（VV、VH、bathymetry）**。这是理解「模型输入长什么样」最直观的入口。

**芯片生成的命令入口。**

[dataset/README.md:L36-L39](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/README.md#L36-L39) —— 这段说明 YOLOv8 需要裁剪好的芯片和单独的 `.txt` 标签文件，并给出预处理命令 `python generate_xview3.py --labels ... --source ... --save_dir ... --imgsz <800|640> --name ...`。本讲只需记住 `--imgsz`（芯片尺寸）这个参数，它在后续多个单元都会出现。

#### 4.2.4 代码实践

**实践目标**：动手搭一个空的「数据集骨架目录」，并写一小段 Python 用 `rasterio` 读取一个 SAR 场景，建立对数据物理形态的直觉。这一步对应单元 2 的前置准备。

**操作步骤**：

1. 按 [dataset/README.md:L7-L35](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/README.md#L7-L35) 的目录树，在本地创建空骨架（命令供参考）：

   ```bash
   # 示例命令：仅创建目录结构，不下载真实数据
   mkdir -p xview3_sar/{data,labels,shoreline}/{training,validation,public}
   ```

2. 安装 `rasterio`（若未安装）：`pip install rasterio numpy`。
3. 写一段读取示例 SAR 场景的代码（**示例代码**，非项目原有文件）：

   ```python
   # 示例代码：用 rasterio 打开一个 VH_dB.tif，查看形状与数据类型
   import rasterio
   import numpy as np

   tif_path = "xview3_sar/data/training/<某场景文件夹>/VH_dB.tif"  # 替换为真实路径
   with rasterio.open(tif_path) as src:
       arr = src.read(1)          # 读第 1 个波段
       print("shape:", arr.shape) # 单波段 SAR 场景通常很大，如 (M, N)
       print("dtype:", arr.dtype) # SAR dB 数据常见为 float32 或 int16
       print("min/max:", np.nanmin(arr), np.nanmax(arr))
   ```

**需要观察的现象**：

- 目录骨架应包含 3×3 = 9 个最末级子目录（`data/labels/shoreline` × `training/validation/public`）。
- 若能拿到真实 `.tif`，会看到单张 SAR 场景尺寸非常大（千万到亿级像素），这正是后面要裁剪成芯片的原因。

**预期结果**：目录骨架创建成功；若读到真实文件，能打印出大尺寸的 `shape` 和一个数值范围（dB 数据通常为负值，如约 -50 到 +20）。

**待本地验证**：是否拿到真实 xView3-SAR 数据取决于你是否从竞赛页面下载。若未下载，本实践的代码可先保存，待单元 2 再实际运行；`shape` 与 `dtype` 的具体值需以本地实际文件为准。

#### 4.2.5 小练习与答案

**练习 1**：相比光学相机，SAR 做船舶检测的最大优势是什么？

> **参考答案**：SAR 是主动式雷达，**不依赖太阳光、能穿透云层**，可全天候、全天时成像，适合海事这类需要持续监测的应用。

**练习 2**：xView3-SAR 场景的三个输入通道是什么？其中哪个**不是**雷达信号？

> **参考答案**：VV、VH、bathymetry（水深）。其中 **bathymetry 不是雷达信号**，它是水深数据，提供海域深度信息。

**练习 3**：为什么不能直接把整张 xView3-SAR 场景喂给 YOLOv8 训练？

> **参考答案**：单张场景尺寸极大（可达上亿像素），超出模型与显存的承受范围；需要先用 `generate_xview3.py` 把场景裁剪成固定大小的芯片（如 800×800）并生成对应标签，才能作为训练输入。

---

### 4.3 KV260 与功耗约束

#### 4.3.1 概念说明

理解本节，要先建立两个概念。

**第一，什么是「功耗约束」。**
卫星上的电力来自太阳能板和电池，总量非常有限。一台服务器级 GPU 动辄上百瓦甚至几百瓦，在星载环境里根本不可行。因此「星载 ML」的核心矛盾不是「算得不够快」，而是「**每瓦能算多少**」（能效，efficiency）。README 里强调的「常见卫星功耗约束 <10W」就是这条硬红线。

**第二，什么是 Kria KV260 MPSoC。**
KV260 是赛灵思（Xilinx / AMD）推出的一块**边缘 AI 开发板**，核心是一颗 **MPSoC**（多处理器系统级芯片）。它把两种计算资源集成在同一颗芯片上：

- **PS（Processing System，处理系统）**：一颗 ARM Cortex-A53 CPU，跑 Linux 和普通程序；
- **PL（Programmable Logic，可编程逻辑）**：即 FPGA 部分，可以在硬件电路上跑定制的加速器。

本项目的深度学习推理，主要跑在 PL 侧的一个叫 **DPU（Deep Learning Processor Unit，深度学习处理单元）** 的硬件 IP 上——它是一块专门为神经网络运算设计的 FPGA 电路。DPU 的细节、资源占用、如何编译模型给它跑，是单元 4（量化）和单元 5（硬件平台）的主题。

> 直觉一句话：**GPU 用「大功率通用算力」碾压问题；FPGA 用「低功率定制电路」精打细算。** 当你的红线是 <10W 时，后者是更合理的选择——代价是开发难度高得多（要量化模型、要编译到 DPU、甚至要写 HLS 加速核），这正是本项目后续所有单元的工作内容。

#### 4.3.2 核心流程

「为什么是 FPGA」可以用一个简单的权衡关系来表达。定义能效为「单位功耗下完成的计算量」：

\[
\text{能效} \;=\; \frac{\text{完成的有效计算（或吞吐）}}{\text{功耗}}
\]

- GPU：分子（算力）很大，但分母（功耗）也很大（数百瓦）；
- KV260 FPGA：分子比 GPU 小，但分母压到 <10W，因此**整体能效反而高得多**。

README 给出的量化结论是：在精度几乎持平的前提下，本项目的计算效率约为 SOTA GPU 模型的 **~50× 与 ~2500×**。这两个倍数对应不同的对比口径（详见论文），本讲只需记住「**效率高出一到两个数量级以上**」这个量级感。

把功耗约束串联到整个工程链路里：

```text
功耗红线 <10W
    │
    ├─► 必须用低功耗平台 ─► KV260 MPSoC（PS: ARM A53 跑 Linux / PL: FPGA 跑 DPU）
    │
    ├─► 模型必须瘦身       ─► int8 量化（单元 4）+ 架构改造 + 剪枝
    │
    └─► 耗时的后处理也要加速 ─► 用 HLS 把解码核下放到 PL 侧（单元 8）
```

可以看到，**功耗约束是整本手册几乎所有工程决策的总根源**。

#### 4.3.3 源码精读

**核心成果指标（再次精读，聚焦硬件与功耗）。**

[README.md:L7-L7](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L7-L7) —— 这一段同时给出了硬件平台（KV260 MPSoC）、性能（~700 megapixel/分钟）、功耗红线（<10W）和效率对比（~50× / ~2500×）。它是本模块最重要的引用点。

**仓库里的硬件平台组件。**

[README.md:L15-L16](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L15-L16) —— 这两行列出两个与 KV260 直接相关的仓库组件：

- `platform/kv260/`：构建 KV260 上 Vitis AI 3.5 固件所需的硬件设计文件、DPU 配置和脚本；
- `platform/post_processing/`：用 **Vitis HLS** 实现的 YOLOv8 边界框解码后处理内核，专门为 PL 侧执行优化。

这条信息预告了单元 5（硬件平台）和单元 8（HLS 后处理）的存在。

**精度—模型大小权衡曲线。**

[README.md:L22-L23](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L22-L23) —— README 还配了一张 `xview3_models.jpg`，画出 F1 分数随模型大小的变化。这张图说明本项目在「精度—模型大小」权衡曲线上找到了一个对边缘部署非常友好的位置（精度接近 SOTA，但模型小得多）。本讲暂不展开，单元 9 会专门分析。

#### 4.3.4 代码实践

**实践目标**：用一个简单的计算，把「FPGA 的能效优势」从文字结论变成可感知的数字，加深对功耗约束的理解。

**操作步骤**：

1. 假设一台典型服务器 GPU 功耗约为 300W，KV260 运行本项目时功耗 <10W（取 10W 为上限）。
2. 在笔记里回答以下计算题（**示例计算**，非项目代码）：

   - 问：仅从功耗看，KV260 的功耗约为该 GPU 的几分之几？
   - 已知本项目计算效率约为 GPU 的 ~50×（按一种口径）。结合功耗比，估算「每瓦效率」的相对倍数。

   参考算式：

   \[
   \text{功耗比} = \frac{10}{300} \approx 0.033,\qquad
   \text{每瓦效率倍数} \approx 50 \times \frac{300}{10} = 1500
   \]

3. 打开 [README.md:L7-L7](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L7-L7)，核对你在步骤 2 里用到的两个原始数字（<10W、~50×）是否和 README 一致。

**需要观察的现象**：把「绝对算力优势」折算成「每瓦优势」后，倍数会被进一步放大（从 ~50× 放大到 ~1500× 量级）。这说明在功耗受限场景下，FPGA 的相对优势比「裸算力对比」还要显著。

**预期结果**：得到功耗比 ≈ 0.033，每瓦效率倍数在千倍量级；并确认 README 原文确实给出了 <10W 与 ~50×/~2500× 这两个数字。

**待本地验证**：本实践为估算性质，GPU 功耗 300W 仅为典型假设，不同 GPU 型号数值不同；结论的量级（「每瓦优势远大于裸算力优势」）是稳健的，具体倍数以论文为准。

#### 4.3.5 小练习与答案

**练习 1**：KV260 MPSoC 的 PS 和 PL 分别指什么？本项目的神经网络推理主要跑在哪一侧？

> **参考答案**：PS（Processing System）是 ARM Cortex-A53 CPU，跑 Linux 与普通程序；PL（Programmable Logic）是 FPGA 可编程逻辑。本项目推理主要跑在 PL 侧的 **DPU**（深度学习处理单元）硬件 IP 上。

**练习 2**：为什么 README 反复强调 <10W 这个数字？它和「选择 FPGA 而非 GPU」是什么关系？

> **参考答案**：<10W 是星载环境的功耗红线。GPU 功耗通常远超此值，无法星载部署；KV260 这类低功耗 FPGA 能在 <10W 内完成推理，是满足该红线的可行选择。功耗约束是选 FPGA 的根本原因。

**练习 3**：本项目相对 SOTA GPU 模型的两项关键对比指标分别是什么？

> **参考答案**：①**精度差距**：检测与分类性能仅低约 ~2% 和 3%；②**效率优势**：计算效率高出约 ~50× 与 ~2500×。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「一页项目画像」任务。

**任务**：制作一张「SAR 船舶检测 FPGA 项目」的一页画像（可以是 Markdown、文本或手绘），必须包含以下四个区块，且每个区块都要**引用至少一条 README 原文链接**作为依据：

1. **一句话定位**：用一句话说清项目做什么（提示：on-satellite ML + SAR 船舶检测 + KV260）。
2. **问题与动机**：列出星载 ML 要解决的两个矛盾（传输延迟、模型过大过耗电），引用 [README.md:L3-L7](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L3-L7)。
3. **数据与输入**：说明 xView3-SAR 的三个通道（VV/VH/bathymetry）与目录结构，引用 [dataset/README.md:L7-L35](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/README.md#L7-L35) 和 [README.md:L9-L10](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L9-L10)。
4. **硬件与指标**：列出 KV260、<10W、~700 megapixel/分钟、精度低 ~2%/3%、效率高 ~50×/2500×，引用 [README.md:L7-L7](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L7-L7)。

**完成标准**：这张画像应当能让你在 30 秒内向一个完全不懂本项目的同事讲清楚「这是什么、为什么重要、做到了什么程度」。把它保存好——它会成为你阅读后续所有讲义时的「速查地图」。

---

## 6. 本讲小结

- 本项目要做的是**星载 SAR 船舶检测**：让卫星在轨道上就地分析雷达图像，规避传回地面的数小时延迟。
- 核心障碍是**最先进模型太大、太耗电**，无法直接星载部署。
- 数据集是 **xView3-SAR**（Sentinel-1），输入是 **VV / VH / bathymetry** 三个通道，按 `data / labels / shoreline` 三类组织，需裁剪成芯片（如 800×800）才能训练。
- 硬件选择 **Kria KV260 MPSoC**（PS=ARM CPU、PL=FPGA 跑 DPU），功耗压在 **<10W** 的卫星红线内。
- 核心成果：精度仅比 SOTA GPU 模型低 **~2%（检测）/ 3%（分类）**，计算效率却高出约 **~50× / ~2500×**。
- **功耗约束是后续所有工程决策（量化、剪枝、HLS 加速）的总根源**。

---

## 7. 下一步学习建议

本讲只建立了全局认知，还没有进入任何代码细节。建议按以下顺序继续：

1. **下一篇讲义 `u1-l2-repo-structure.md`（仓库结构与各模块职责）**：带你逐个走遍仓库的六大顶层目录，搞清楚 `framework / platform / software` 各自承担什么，为阅读真实源码做导航。
2. 接着读 `u1-l3-end-to-end-pipeline.md`（端到端工作流总览）：把六个组件串成一条流水线，建立「数据 → 训练 → 量化 → 硬件 → 推理 → HLS 后处理」的全景图。
3. 如果你已经等不及想看代码，可以先把 [README.md:L12-L19](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L12-L19) 的仓库结构与本讲的「一页画像」对照一遍，这能帮你快速定位后续每个单元对应的目录。
