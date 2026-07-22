# 性能-精度-功耗权衡分析

> 本讲是全册最后一篇，也是把前八单元所有工程决策「收口」的一篇。前面每一讲都在解决一个具体子问题（怎么切片、怎么训练、怎么量化、怎么编译、怎么搭硬件、怎么写 HLS 核），本讲退后一步，回答一个统领性问题：**这些决策加在一起，到底换来了一份怎样的「精度—吞吐—功耗」账单？为什么这份账单让 FPGA 击败 GPU 成为星载场景的最优解？**

## 1. 本讲目标

学完本讲，你应当能够：

1. 读懂 `assets/xview3_models.jpg` 这张「F1 vs 模型大小」四联图，说清楚本项目相对 SOTA GPU 模型在精度—规模版图上的位置，以及为什么 int8 量化「几乎不掉点」。
2. 读懂 `assets/inference_breakdown.jpg` 这张「推理耗时分解」条形图，把 P2 头、Ghost 模块、模型宽度、量化、HLS 后处理核**各自的贡献与代价**逐项拆开。
3. 基于「<10W 卫星功耗红线」这一约束，论证为什么在星载 SAR 场景里 FPGA（KV260 + int8 DPU）比 GPU 更合理，并能用 50×/2500× 的效率数字与 ~2%/3% 的精度差距支撑这一论证。
4. 面对一个未实现的改动（如「砍掉 P2 头」或「在功耗不变前提下提精度」），能用本讲的账单模型做定量推理，而不是拍脑袋。

## 2. 前置知识

本讲不引入新的源码机制，而是**把前八单元的结论当作已知量来用**。开始前请确认你接受以下三条心智模型：

**① 三轴权衡（trade-off）。** 任何边缘部署都不可能同时把三个旋钮拧到最大：

- **精度轴**：xView3 的四项 F1（Detection / Near-shore / Vessel / Fishing），越高越好。
- **吞吐/延迟轴**：每秒处理多少张 800×800 芯片（FPS），或处理一张 ~700 百万像素整景要多久，越高/越短越好。
- **功耗轴**：瓦特数，星载场景被钉死在 **<10W**（见 [README.md:L7](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L7)）。

这三者相互掣肘：加检测头提精度→计算量上升→吞吐下降/功耗上升；量化降功耗→可能掉精度。本项目的全部工程艺术，就是在 <10W 的硬约束下，把精度轴顶到只比无约束的 SOTA GPU 模型低 ~2%/3%。

**② F1 的定义。** xView3 用 F1 衡量检测/分类质量，\( F1 = \frac{2PR}{P+R} \)，其中 P 是精确率、R 是召回率。四项 F1 的含义与计算细节见 u3-l4；本讲只把 F1 当作「越高越好」的标量结论使用。

**③ 前八单元的产物链。** 数据切片(u2)→训练 `.pt`(u3)→量化 int8 `.xmodel`(u4)→DPU 编译(u4)→KV260 硬件/固件(u5)→Vitis AI 框架补丁(u6)→板载推理应用(u7)→HLS 解码核(u8)。本讲的每一项「优化手段贡献」，都对应这条链上某一环的工程决策。

> 名词速查：**SOTA**（State-of-the-Art，当前最优）；**MPSoC**（多处理器片上系统，KV260 的芯片形态）；**DPU**（Deep-learning Processing Unit，Xilinx 的 int8 定点神经网络加速 IP）；**FPS**（Frames Per Second，每秒帧数）；**P2 头**（stride=4 的高分辨率检测头，专为小目标设计，见 u6-l3/u8）。

## 3. 本讲源码地图

本讲以**两张图 + 两份 README** 为主证据，辅以前几讲已读过的硬件/内核文件做交叉印证：

| 文件 | 作用 | 本讲用法 |
| :--- | :--- | :--- |
| [README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md) | 项目总说明 | 取「<10W、700MPx/<1min、~2%/3% 精度差、50×/2500× 效率」四组权威数字 |
| [assets/xview3_models.jpg](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/assets/xview3_models.jpg) | F1 vs 模型大小四联图 | 论证「精度—规模」位置与「量化不掉点」 |
| [assets/inference_breakdown.jpg](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/assets/inference_breakdown.jpg) | 推理耗时分解条形图 | 拆解各优化手段对预处理/DPU/后处理的贡献 |
| [software/inference_app/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md) | 板载推理应用说明 | 取 benchmark/performance 用法与 breakdown 图注 |
| [platform/kv260/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md) | 硬件平台构建 | 取资源利用率表与 `xdputil query` 输出，支撑功耗/可行性论证 |
| [platform/post_processing/decode_krnl/decode_kernel.cpp](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp) | HLS 解码核 | 取可配置常量，论证「后处理核」为何能砍掉 159ms |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，恰好对应规格要求的「F1-大小权衡曲线」「效率优化手段贡献」「功耗约束论证」。

### 4.1 F1-大小权衡曲线：本项目在精度—规模版图上的位置

#### 4.1.1 概念说明

机器学习里有一条朴素的「精度—规模」经验律：**模型越大，精度往往越高**。把一堆模型画在「横轴=模型大小、纵轴=F1」的散点图上，通常会看到一条向右上倾斜的趋势带。

这条律对云端 GPU 不是问题——云上几乎不限制模型大小。但对星载场景，模型大小直接关联**显存/内存占用、加载时间、功耗**，是被严格约束的。于是问题变成：**能不能在曲线的极左端（模型很小）依然把 F1 顶到接近曲线右端（大模型）的水平？**

`xview3_models.jpg` 这张图就是用 xView3-SAR 数据集回答这个问题。它把两类模型画在一起：

- **xView3-SAR Benchmark**（蓝色系）：竞赛榜上的 SOTA GPU 模型——CircleNet（第 1）、UNet（第 2）、HRNet（第 3）、Faster R-CNN（第 4）等，体量大、功耗高、不可能上卫星。
- **This Study**（本项目，绿/黄色系）：YOLOv8 的小型化变体——`YOLOv8n-Ghost-P2-PIoU2` 与 `YOLOv8s-Ghost-P2-PIoU2`。

图里有四种子图，分别对应 xView3 的四项核心指标：(a) Detection、(b) Near-shore、(c) Vessel Classification、(d) Fishing Classification。本项目的全部「精度」论断，都要落到这四个面板上看。

#### 4.1.2 核心流程

读懂这张图的流程：

1. **看横纵轴**：横轴是模型大小（MB），范围 0–3000；纵轴是 F1。四个子图纵轴范围不同（Detection 约 0.45–0.75，Vessel 约 0.75–0.95 等），因为四项任务难度不同。
2. **分两组**：左下/左上图例是 SOTA 大模型（圆/方/菱形等蓝色标记），右下图例是本项目（圆点 = GPU 浮点，星形 = FPGA int8 部署）。
3. **读「本项目」点的位置**：它们集中在横轴**极左侧**（模型很小），但纵轴（F1）只比最顶部的 SOTA 点低一截——这一截就是 README 说的 ~2%（检测）/ ~3%（分类）。
4. **对比同架构的圆点与星形**：同一个 `YOLOv8x-Ghost-P2-PIoU2`，圆点是 GPU 浮点推理、星形是 FPGA int8 推理。两者几乎重合，说明**量化到 int8 几乎不损失精度**。

用一句话概括这张图讲述的故事：

\[ \underbrace{\text{F1}_{\text{本项目}}}_{\text{星形点}} \approx \underbrace{\text{F1}_{\text{本项目,GPU}}}_{\text{圆点}} \;=\; \underbrace{\text{F1}_{\text{SOTA}}}_{\text{蓝色大模型}} - (2\%\sim 3\%) \quad\text{而}\quad \text{Size}_{\text{本项目}} \ll \text{Size}_{\text{SOTA}} \]

#### 4.1.3 源码精读

权威的精度与效率数字来自项目总说明，集中在 [README.md:L3-L7](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L3-L7)。这段话给出了本讲全部定量论证的「地基」：

- **应用动机**：星载 ML 为了规避卫星与地面站断续连接的数小时延迟（[README.md:L3](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L3)）。
- **核心成果**：部署在 KV260 上，**在 <10W 功耗约束内、不到一分钟分析完一张 ~700 百万像素的 SAR 图像**（[README.md:L7](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L7)）。
- **精度差距**：检测与分类性能分别只比 SOTA GPU 模型低 **~2% 和 ~3%**。
- **效率倍数**：计算效率高出 **~50× 和 ~2500×**。

> 关于 50× 与 2500× 两个数字：它们对应与**两个不同体量**的 SOTA 基准模型相比（一个较接近、一个体量最大/最复杂），所以倍数相差悬殊。README 未点名具体配对，精确配对见论文 [SEC '25](https://doi.org/10.1145/3769102.3772713)；本讲只把「数量级的效率优势」作为结论使用。

图本身嵌在 README 的「Repository Structure」之后，图注写明这是 F1-Score vs Model Size 对比：见 [README.md:L22-L23](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L22-L23)，原始大图在 [assets/xview3_models.jpg](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/assets/xview3_models.jpg)。

「量化几乎不掉点」这一关键结论，视觉上由**同架构圆点与星形几乎重合**体现。其工程根源在第四单元：int8 对称量化只引入最多半个量化步长的误差（\( \le s/2 \)，见 u4-l1），加上 QAT 让权重主动适应量化噪声（u4-l2），并把不友好的 sigmoid/silu 换成 DPU 友好的 hsigmoid/hswish（u4-l3）。这三步合力，才让「星形 ≈ 圆点」成立。

#### 4.1.4 代码实践

**实践目标**：在 `xview3_models.jpg` 上亲手定位本项目的点，并用视觉证据复述 README 的 ~2%/3% 论断。

**操作步骤**：

1. 打开 [assets/xview3_models.jpg](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/assets/xview3_models.jpg)，放大到能看清图例。
2. 在 (a) Detection 面板，找到绿色/黄色的星形点（`YOLOv8n/s-Ghost-P2-PIoU2 (FPGA)`）与同色圆点（GPU 浮点）。
3. 把星形点的纵坐标（F1）与最顶部的蓝色 SOTA 点（如 CircleNet 第 1）的纵坐标相减，估出 Detection 的精度差。
4. 重复 (c) Vessel Classification、(d) Fishing Classification 两个面板。

**需要观察的现象**：

- 本项目的点（星形与圆点）都挤在横轴**左侧**，而 SOTA 蓝色点散布在右侧到远右侧——模型小了一个数量级以上。
- 星形与同色圆点**几乎贴在一起**——int8 量化掉的精度肉眼难辨。

**预期结果**：四个面板上，本项目星形点的 F1 都只比顶部 SOTA 点低 ~2%（检测类）到 ~3%（分类类）的一小截，与 README 数字吻合。

**关于精确数值**：图片像素坐标难以读出可靠的模型大小绝对值（MB），因此**不要**从图上反推具体 MB 数；以 README 的 ~2%/3% 与 50×/2500× 文字数字为准。图的作用是「定性看相对位置与趋势」，不是「定量读数」。精确数值待本地用论文表格核对。

#### 4.1.5 小练习与答案

**练习 1**：假如把本项目的 `YOLOv8s-Ghost-P2-PIoU2` 换成竞赛冠军 CircleNet 同等大小的大模型上 FPGA，根据本节的「精度—规模」曲线，精度会显著提升吗？代价是什么？

> **参考答案**：精度提升会很有限（曲线在右端趋于平缓，大模型的边际精度收益递减），但代价是模型体积、DPU 计算量、内存带宽、功耗全部上升，很可能突破 <10W 红线。这正是本项目选择「小模型 + 极致优化」而非「堆大模型」的根本原因。

**练习 2**：图上「同架构圆点（GPU 浮点）与星形（FPGA int8）几乎重合」说明了什么？如果两者差距很大，最可能是在 u4 的哪一步出了问题？

> **参考答案**：说明 int8 量化对精度几乎无损。若差距大，最可能是校准/QAT 出问题（scale 选错）、或激活替换标志 `--nndct_convert_sigmoid_to_hsigmoid` / `--nndct_convert_silu_to_hswish` 在 calib/QAT/导出/评估四类命令中不一致（见 u4-l3），导致量化参数与模型结构错配。

---

### 4.2 效率优化手段贡献拆解：P2 头、Ghost、量化、HLS 后处理

#### 4.2.1 概念说明

`inference_breakdown.jpg` 把一次推理的耗时拆成三段：**预处理（Pre）+ DPU 推理（DPU）+ 后处理（Post）**。它对比四个 YOLOv8 变体，构成一组干净的**消融实验（ablation）**——每相邻两个变体只改一个因素，于是耗时的变化就归因于那个因素：

| 变体 | 相对前一变体改了什么 |
| :--- | :--- |
| YOLOv8n | 基线（标准 nano） |
| YOLOv8n-**Ghost** | 把部分标准卷积换成 Ghost 模块（结构压缩） |
| YOLOv8n-Ghost-**P2** | 增加 stride=4 的 P2 高分辨率检测头 |
| YOLOv8**s**-Ghost-P2 | 宽度从 nano 提到 small（容量更大） |

> **关于「剪枝」的澄清**：本讲的规格里提到「剪枝」，但需要诚实区分——仓库里的 [software/training/prune.py](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/prune.py) 是**点距离 NMS**（u3-l5），**不是**经典的意义上的「权重剪枝（weight pruning）」。本项目真正让模型变小的手段是 **Ghost 模块**（用更廉价的 GhostConv 替换重卷积）与**选择 nano 宽度**这两类**结构性压缩**。下文「压缩」一词均指 Ghost + 小宽度，不指权重稀疏化。

三段耗时的语义（详见 u7-l3）：

- **预处理（Pre）**：读 TIFF + SAR 归一化 + 转 signed int8（u6-l1）。**与模型无关**，所以四个变体这一段应当相同。
- **DPU 推理（DPU）**：纯硬件算力消耗，**随模型计算量单调变化**——这是衡量「模型有多重」最干净的标尺。
- **后处理（Post）**：把 DPU 输出的特征图解码成候选框（u6-l3/u8）。**随检测头输出的 anchor 数量变化**——头越多越密，后处理越重。

#### 4.2.2 核心流程

从图中读出的耗时（毫秒，每段为 100 次连续推理的均值，见 [software/inference_app/README.md:L28-L29](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md#L28-L29)）：

| 变体 | 预处理 | DPU | 后处理 | 总计 |
| :--- | :---: | :---: | :---: | :---: |
| YOLOv8n | 71 | 33 | 41 | 145 |
| YOLOv8n-Ghost | 71 | 30 | 40 | 141 |
| YOLOv8n-Ghost-P2 | 71 | 48 | **159** | 278 |
| YOLOv8s-Ghost-P2 | 71 | 84 | **159** | 315 |

把相邻行相减，就得到每个手段的**贡献/代价**：

1. **Ghost 模块**（n → n-Ghost）：DPU 33→30、后处理 41→40。**轻微降耗**，是「几乎免费的压缩」。
2. **P2 头**（n-Ghost → n-Ghost-P2）：DPU 30→48、**后处理 40→159（≈4×）**。这是全表最剧烈的一跳——P2 是 stride=4 的高分辨率头，输出网格是 200×200（见 u8 的 `layer_size=200`），anchor 数量远多于 P3/P4/P5，**后处理解码量暴涨 4 倍**。
3. **宽度 n→s**（n-Ghost-P2 → s-Ghost-P2）：DPU 48→84、后处理不变 159。**只增加 DPU 计算**，后处理与头结构无关所以不变。

由此得到一张「手段—影响维度—证据—代价」对照表：

| 手段 | 主要影响 | 证据（消融表） | 代价 |
| :--- | :--- | :--- | :--- |
| Ghost 模块 | 计算量↓ | DPU 33→30 | 几乎无 |
| P2 高分辨率头 | 小目标精度↑ | 后处理 40→159, DPU 30→48 | 后处理/计算大增 |
| 宽度 n→s | 容量/精度↑ | DPU 48→84 | DPU 时间增加 |
| int8 量化 | 体积/功耗↓ | 星形点≈圆点（4.1 节） | ~2%/3% 精度 |
| HLS 解码核 | 后处理时间↓ | 专门攻击 159ms 段 | BRAM 资源（u5-l1/u8-l3） |
| PIoU2 | 回归精度↑ | 训推一致 IoU（u3-l3/u6-l2） | 计算开销小 |

**核心结论**：P2 头是精度的功臣，却也是后处理的「罪魁祸首」——它让后处理从 40ms 飙到 159ms，成为 P2 变体总耗时的**主体**（278ms 中占 57%，315ms 中占 50%）。这正是 u8 HLS 解码核存在的全部理由：**用 PL 硬件把这段 159ms 砍下去**。

#### 4.2.3 源码精读

**后处理核的「靶子」有多大**——看 HLS 解码核的可配置常量 [decode_kernel.cpp:L12-L18](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L12-L18)：

```cpp
#define MAX_BOXES         2048   // max number of total boxes before NMS
#define DIST_BINS          16    // 16 bins in distance softmax branch
#define NUM_CLASSES        3
#define OUTPUT_DIM         67
```

`OUTPUT_DIM = 67 = 4×16 + 3`（4 条边各 16 个 DFL bin + 3 个类别，见 u8-l2）。`MAX_BOXES = 2048` 是单层 NMS 前候选框上限。这些常量刻画了 P2 头带来的解码规模——每个 anchor 要做 4 次 16-bin softmax，而 P2 层有 200×200 个 anchor。软件逐 anchor 解码就是那 159ms 的来源；u6-l3 的软件优化（整型阈值提前剔除背景 anchor、约 27×）已经砍掉一大半，剩下的就是 u8 HLS 核要搬到 PL 上加速的部分。

**量化如何把模型压到 DPU 能跑**——四个命令里都必须带两个激活替换标志，例如校准命令 [software/quantization/README.md:L40](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L40)：

```bash
yolo detect val ... nndct_quant=True quant_mode=calib imgsz=800 \
  --nndct_convert_sigmoid_to_hsigmoid --nndct_convert_silu_to_hswish
```

`imgsz=800`（与切片尺寸一致）、两个 `--nndct_convert_*` 标志，在 calib / QAT / 导出 / 评估四类命令中**逐字一致**（u4-l3）。这一致性既是「能不能量化」的前提，也是 4.1 节「星形≈圆点」的工程保障。

**PIoU2 的精度贡献**——训练侧用 PIoU2 替换默认 CIoU 做框回归，见 [software/training/README.md:L40](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L40)（在 `metrics.py` 的 `bbox_iou` 里加 `compute_piou`）。它对 SAR 极小点目标更稳定（u3-l3），并在推理侧 C++ NMS 以同一度量重现（u6-l2，`PIOU2_NMS=1`），构成训推一致暗线。

#### 4.2.4 代码实践

**实践目标**：用消融表把每个手段的 DPU 耗时变化算出来，验证「DPU 时间是模型重量的干净标尺」。

**操作步骤**：

1. 复制本节 4.2.2 的四行耗时表到本地。
2. 依次计算三组差值：Ghost 的 DPU 差 = 33−30；P2 的 DPU 差 = 48−30；宽度 n→s 的 DPU 差 = 84−48。
3. 再算 P2 对后处理的影响 = 159−40。
4. 回答：哪个手段对「总耗时」冲击最大？冲击落在哪一段？

**需要观察的现象**：

- Ghost 的 DPU 差只有 3ms（压缩收益小但几乎免费）。
- P2 让 DPU +18ms、但后处理 +119ms——**后处理增量是 DPU 增量的 ~6.6 倍**。
- 宽度 n→s 让 DPU +36ms、后处理 +0ms。

**预期结果**：P2 头对总耗时的冲击最大（+137ms），且绝大部分（119ms）落在后处理段而非 DPU 段。这直接证明了「后处理是 P2 变体的瓶颈、是最该用硬件加速的一段」，为 u8 HLS 核提供了存在依据。

> 此实践为「源码阅读 + 图表推理型」，无需运行硬件；数字取自 [assets/inference_breakdown.jpg](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/assets/inference_breakdown.jpg) 的近似读数，精确值待本地用 `xview3_performance` 实测（见 4.3.4）。

#### 4.2.5 小练习与答案

**练习 1**：如果只用 `YOLOv8n-Ghost`（不带 P2），总耗时降到 ~141ms，吞吐大增。为什么项目仍然坚持用 P2？

> **参考答案**：SAR 船舶是**点状小目标**，P2 的 stride=4 高分辨率头能看清更细的特征，显著提升小目标检出率（即 Detection/Near-shore F1）。吞吐的损失用 HLS 解码核补回来，而小目标精度无法用别的手段廉价换回——所以 P2 是「精度不可妥协、耗时用硬件找补」的典型取舍。

**练习 2**：从「s-Ghost-P2」到「n-Ghost-P2」，DPU 从 84ms 降到 48ms，后处理却都是 159ms 不变。为什么宽度不影响后处理？

> **参考答案**：后处理的工作量取决于**检测头结构（anchor 数量 = 网格尺寸）**，而非通道宽度。n 与 s 的 P2 头都是 200×200 网格、同样的 `OUTPUT_DIM=67`，要解码的 anchor 总数不变，所以后处理耗时不变；只有卷积通道数变了，故只影响 DPU 段。

---

### 4.3 功耗约束论证：为什么 FPGA（KV260）而非 GPU

#### 4.3.1 概念说明

星载场景的功耗被钉死在 **<10W**（[README.md:L7](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L7)）——这是卫星供电、散热、体积三重约束的合并红线。在这个红线前，GPU 与 FPGA 的较量并不是「谁算得快」，而是「**谁能在 10W 内把活干完**」。

GPU 的强项是**峰值算力**（TFLOPS），但代价是高功耗（几十到几百瓦）、需要主机与主动散热、且通用核心里有大量对 CNN 推理无用却仍在耗电的电路。FPGA（KV260 的 PL 侧）则把电路**专门定制**成 int8 神经网络所需的乘加阵列（DPU），没有指令译码与通用核心的浪费，静态功耗低、延迟确定，还能进一步在 PL 里塞入自定义核（如 u8 解码核）——这是 GPU 在同等功耗下做不到的。

本项目的核心论断正是：**在 <10W 内，KV260 + int8 DPU 达到了只比无约束 SOTA GPU 模型低 ~2%/3% 的精度，却把计算效率拉高了 ~50×/2500×。** 这一节的任务是用源码里的硬件事实把这条论断支撑起来。

#### 4.3.2 核心流程

论证分三步：

1. **算力来源**：推理跑在 PL 侧的 DPU（DPUCZDX8G，int8 定点），频率 325MHz，开启 softmax——这些参数是「<10W 内高效推理」的硬件基础。
2. **资源可行性**：DPU 把 KV260 的资源吃到什么程度？还剩多少能给后处理核？这决定「能不能再加一个 HLS 核」。
3. **端到端时效**：用耗时分解估算「一张 ~700 百万像素整景要多久」，验证 README 的「<1 分钟」论断，并说明为什么必须靠多线程 + HLS 核才能达成。

#### 4.3.3 源码精读

**DPU 的硬件身份**——板载 `xdputil query` 报告（部署后的验收输出），见 [platform/kv260/README.md:L239-L264](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L239-L264)：

```json
"DPU IP Spec": { "DPU Core Count":1, "IP version":"v4.1.0", "enable softmax":"True" }
"kernels":[{
    "DPU Arch":"DPUCZDX8G_ISA1_B4096",
    "DPU Frequency (MHz)":325,
    "fingerprint":"0x101000056010407",
    "is_vivado_flow":true
}]
```

`DPUCZDX8G_ISA1_B4096`：B4096 表示单拍 4096 个 MAC、ISA1 是不含非线性指令的精简指令集（这正是 u4-l3 必须把 sigmoid/silu 换成 hsigmoid/hswish 的硬件根源）。325MHz、单核、使能 softmax。这套配置是 KV260 SOM 在个位数瓦特功耗包络内的标准高效档位（u5-l1）。

> 关于精确瓦特数：仓库未给出 DPU 实测功耗绝对值，<10W 是 README 对「卫星可行」的约束陈述。KV260 SOM 在典型 DPU 负载下整体功耗处于个位数瓦特量级，满足该红线；精确数值待本地用板载功率计实测验证。

**资源利用率——能不能再塞一个 HLS 核？** 看 Vivado 实现后的资源表 [platform/kv260/README.md:L43-L62](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L43-L62)：

| 资源 | 已用 | 可用 | 利用率 |
| :--- | ---: | ---: | ---: |
| CLB LUTs | 62104 | 117120 | 53.03% |
| CLB Registers | 106965 | 234240 | 45.66% |
| **Block RAM Tile** | **109** | **144** | **75.69%** |
| URAM | 40 | 64 | 62.50% |

LUT/寄存器较宽裕（~50%），但 **BRAM 75.69% 是最紧的**（只剩 35 块），URAM 62.5% 几乎全归 DPU。这意味着：再加一个 HLS 后处理核是**可能的**（u8-l3 综合报告显示该核 BRAM 占用很低，仅 ~2%），但必须盯紧 BRAM 预算——BRAM 是这条设计里扩展 PL 加速的**决定性瓶颈资源**（u5-l1）。

#### 4.3.4 代码实践

**实践目标**：用耗时分解做一次「~700 百万像素整景」的端到端时效粗估，理解 README「<1 分钟」需要哪些条件。

**操作步骤**：

1. 计算整景折合多少张 800×800 芯片：\( N_{\text{chips}} = \lceil 700\times10^6 / (800\times800) \rceil \approx 1094 \)。
2. 取部署变体 `YOLOv8s-Ghost-P2` 的单芯片端到端耗时 315ms（4.2 表），算单线程总时间：\( 1094 \times 0.315 \approx 345\,\text{s} \approx 5.7\,\text{min} \)。
3. 单线程 5.7 分钟显然超过 1 分钟。回答：要压到 <1 分钟，缺了哪两件事？

**需要观察的现象**：

- 单线程、纯软件后处理下，700MPx 要 ~5.7 分钟，**远超** README 的「<1 分钟」。
- 差距来自两段：① 端到端 315ms 里，预处理 71ms 与后处理 159ms 都跑在 PS（ARM CPU）上，与 DPU 的 84ms 是**串行**的；② 后处理 159ms 占了总耗时的 50%。

**预期结果**：要达成 <1 分钟，必须同时满足——

- **多线程流水线**（u7-l2/u7-l3）：让 PS 的预处理/后处理与 PL 的 DPU 计算在时间上**重叠**，把每芯片的有效耗时从「Pre+DPU+Post 串行」压向「max(主机开销, DPU)」。
- **HLS 解码核**（u8）：把 159ms 后处理从 PS 搬到 PL，进一步缩短主机串行段。

只有这两步都到位，单 DPU 核的 KV260 才能在 <10W 内把 ~700MPx 压进一分钟。这一估算清楚地揭示了「为什么本项目既要多线程应用、又非写 HLS 核不可」。

> 此估算为粗略上界，**待本地验证**：实际多线程吞吐、HLS 核加速比、I/O 与队列开销都会影响最终墙钟。精确值需在板载用 `xview3_performance`（[software/inference_app/README.md:L20-L25](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md#L20-L25)）实测。

**示例代码**（非项目代码，仅做上述估算的 Python 复现）：

```python
# 示例代码：700MPx 整景端到端耗时粗估（单线程、纯软件后处理）
mpx_total = 700e6
chip = 800 * 800
n_chips = -(-int(mpx_total) // chip)        # 向上取整 ≈ 1094
per_chip_ms = 71 + 84 + 159                  # s-Ghost-P2: Pre+DPU+Post
total_s = n_chips * per_chip_ms / 1000
print(n_chips, total_s / 60)                 # 1094, ~5.75 min
```

#### 4.3.5 小练习与答案

**练习 1**：假设 HLS 核把 159ms 后处理降到 20ms，单芯片端到端降到 71+84+20=175ms。单线程下 700MPx 要多久？还需要多线程吗？

> **参考答案**：\( 1094 \times 0.175 \approx 191\,\text{s} \approx 3.2\,\text{min} \)，仍超 1 分钟。所以即使有 HLS 核，仍需多线程把 PS 的预处理（71ms）与 DPU（84ms）重叠，才能进一步压到 <1 分钟。结论：HLS 核与多线程**互补**，缺一不可。

**练习 2**：用「功耗—精度」两轴论证：为什么不直接在卫星上放一块低功耗 GPU（如移动端 GPU）？

> **参考答案**：移动端 GPU 即便峰值功耗较低，仍是通用核心架构，对 int8 CNN 存在指令译码与无用电路的能耗浪费；且无法像 PL 那样把后处理解码定制成专用电路（u8）。KV260 的 DPU 是 int8 专用定点阵列 + 可定制 PL，在同等 <10W 下效费比远高于通用 GPU——这正是 50×/2500× 效率优势与 ~2%/3% 精度差距能同时成立的物理根源。

---

## 5. 综合实践

本实践是规格指定的收口任务，把 4.1（精度曲线）与 4.2（耗时分解）合起来做一次「假设性改动」的定量推理，并提出一个改进方向。请在本地用文字完成（无需硬件）。

**任务 A：预测「去掉 P2 头」的连锁后果。**

对照 [assets/xview3_models.jpg](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/assets/xview3_models.jpg) 与 [assets/inference_breakdown.jpg](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/assets/inference_breakdown.jpg)，写一段分析：

1. **吞吐侧**：从 `YOLOv8s-Ghost-P2` 退回 `YOLOv8n-Ghost`（最接近「去掉 P2」的可见变体），单芯片端到端从 315ms 降到 141ms，后处理从 159ms 降到 40ms。估算 700MPx 单线程耗时从 ~5.7min 降到多少。
2. **精度侧**：在 `xview3_models.jpg` 的 (a) Detection 与 (b) Near-shore 面板上，预测去掉 P2 后小目标检出 F1 会**明显下滑**（因为 P2 的 stride=4 高分辨率头正是为点状小目标而设）。说明这一下滑很可能比 ~2%/3% 的量化损失大得多。
3. **结论**：用「三轴权衡」语言总结——去 P2 换来了大幅吞吐提升，却丢掉了本项目相对 SOTA 还能保住精度的核心资本（小目标检出），得不偿失；正确的做法是**保 P2、用 HLS 核消化它的后处理代价**（即项目实际选择）。

**任务 B：提出一个「功耗不变、精度可提升」的改进方向。**

基于本讲所学，从以下两类中选一个，写出可行性与代价分析（任选其一即可）：

- **更好的量化校准/QAT**：扩大校准集覆盖更多近岸/复杂场景、或对 P2 头的激活单独标定 scale（u4-l1/u4-l2），目标是把「星形点」进一步贴向「圆点」，在不增功耗的前提下把 ~2%/3% 的量化差距再压小。
- **HLS 核扩展**：利用 BRAM 还有 ~24% 余量（4.3.3 资源表），把排序/top-k/距离 NMS（u3-l5/u6-l2）中计算密集、规则的部分也搬上 PL，进一步缩短主机串行段，腾出功耗预算给更宽的模型（如 n→s），换取精度。

**交付物**：一段 300 字以内的分析文字 + 一张「手段→影响轴→预期收益→代价」的小表格（可参照 4.2.2 的对照表格式）。

> 诚实声明：本综合实践为「图表推理 + 设计论证」型，所有吞吐数字均为基于 breakdown 图近似读数的**粗估**，精确结论待本地在 KV260 上用 `xview3_performance` 与 `xview3_benchmark` 实测后核定。

## 6. 本讲小结

- 本项目在「精度—规模」版图上占据**极左侧（模型很小）但 F1 只比 SOTA 低 ~2%/3%** 的位置；同架构的 FPGA 星形点与 GPU 圆点几乎重合，证明 int8 量化几乎不掉点。
- `inference_breakdown.jpg` 的四变体消融干净地拆出各手段的贡献：Ghost 几乎免费地降耗；**P2 头是小目标精度的功臣、却让后处理从 40ms 飙到 159ms（≈4×）**；宽度 n→s 只增加 DPU 时间。
- **P2 带来的 159ms 后处理是 P2 变体的耗时主体**，这是 u8 HLS 解码核存在的全部理由——用 PL 硬件把这段砍下去。
- 功耗论证的三块基石：DPU 是 int8 专用定点阵列（`DPUCZDX8G_ISA1_B4096`/325MHz）、BRAM 75.69% 是扩展 PL 核的瓶颈资源、多线程 + HLS 核合力才能把 ~700MPx 压进 <1 分钟。
- 仓库里的 `prune.py` 是点距离 NMS，**不是权重剪枝**；本项目真正的「压缩」来自 Ghost 模块 + nano 宽度的结构性精简。
- 50×/2500× 效率与 ~2%/3% 精度差距能同时成立，根因是「<10W 红线下 int8 专用 DPU + 可定制 PL」相对通用 GPU 的效费比优势——这就是星载场景 FPGA 击败 GPU 的底层逻辑。

## 7. 下一步学习建议

本讲是全册终点，没有「下一讲」。建议你按以下方向继续深化：

1. **复现实测**：如果你有 KV260 硬件，按 u9-l1 的端到端 checklist 部署后，用 `DEEPHI_PROFILING=1 ./xview3_performance`（[software/inference_app/README.md:L20-L25](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md#L20-L25)）实测本讲所有粗估数字（Pre/DPU/Post 三段、700MPx 整景墙钟），与本讲的图表对账。
2. **读论文**：本讲的 50×/2500× 配对、四项 F1 的精确数值、以及 P2/Ghost/量化各自的**精度**消融（不只是耗时消融），都在 SEC '25 论文 [https://doi.org/10.1145/3769102.3772713](https://doi.org/10.1145/3769102.3772713) 里，建议对照阅读以补全本讲标注「待本地验证」之处。
3. **二次开发实验**：尝试综合实现本讲综合实践 B 的改进方向——例如修改 [decode_kernel.cpp:L12-L18](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L12-L18) 的常量或扩展核内逻辑，在 BRAM 预算内把更多后处理步骤搬上 PL，实测吞吐变化。
4. **横向对照**：把本讲的「三轴权衡」框架套用到你自己的边缘 ML 项目，问自己——你的精度轴、吞吐轴、功耗轴各自的红线在哪？哪个手段（量化/结构压缩/硬件加速）最值得投入。
