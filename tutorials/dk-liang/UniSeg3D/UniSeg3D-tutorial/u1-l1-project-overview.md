# 项目总览：一个模型如何统一六类 3D 分割任务

> 所属单元：u1 初识 UniSeg3D（beginner） · 讲义编号：u1-l1 · 前置依赖：无

## 1. 本讲目标

读完本讲，你应该能够：

1. 说出 UniSeg3D 是什么、解决什么问题：用**一个模型、一次训练**，同时完成全景、语义、实例、交互式、指代、开放词汇六类 3D 分割任务。
2. 用自己的话解释六类任务各自的**输入提示**与**输出形式**差异，并对照 README 指标表说出每个任务用什么指标（PQ / mIoU / mAP / AP）衡量、对应哪个数据集与 checkpoint。
3. 理解整体架构的三块拼图：**稀疏卷积骨干 + 统一查询解码器 + CLIP 语言分支**，以及「超点即查询」这一统一表示的直觉。
4. 看懂 `uniseg3d/__init__.py` 这份 19 行的「注册清单」，初步建立「配置里的 `type='XXX'` 字符串 ↔ 源码里的类」的对应能力，为后续所有源码阅读打底。

本讲不要求你运行训练，只要求你**读懂 README、数清注册表、画一张对照表**。

## 2. 前置知识

### 2.1 什么是 3D 点云与 3D 分割

- **点云（point cloud）**：激光扫描或 RGB-D 相机重建出的三维点集合，每个点通常有坐标 \( (x, y, z) \) 和颜色 \( (r, g, b) \)。ScanNet 数据集里一个室内场景约有几十万个点。本项目输入是 6 维点（坐标 + 颜色），配置里 `in_channels=6`。
- **分割（segmentation）**：给每个点打标签。标签可以是「类别」（墙、椅子……），也可以是「属于哪个物体实例」（椅子 #1、椅子 #2……）。
- **超点（superpoint）**：先用过分割算法（本项目用 segmentator / SSTNet 产物）把点云切成若干「小块」，每块内的点大概率属于同一物体表面。一个场景几十万个点 → 几千个超点。超点是本项目一切统一表示的基石，后续每一讲都会反复出现。

### 2.2 六类分割任务一览（直觉版）

| 任务 | 通俗理解 | 额外输入提示 |
| --- | --- | --- |
| 语义分割 Semantic (SS) | 每个点属于什么**类别** | 无 |
| 实例分割 Instance (IS) | 找出每个**物体实例**的掩码 | 无 |
| 全景分割 Panoptic (PS) | 语义 + 实例的合体：可数物体（things）和不可数背景（stuff，如墙、地板）统一编号 | 无 |
| 交互式分割 Interactive | 用户**点一下**某个物体，模型分割出整个物体 | 一个点坐标 |
| 指代分割 Referring | 用户用**一句话**描述物体（"紧挨沙发的木椅"），模型分割出它 | 一句自然语言 |
| 开放词汇分割 Open-Vocabulary (OV) | 类别不限于训练词表，可识别**训练时没见过的类** | 任意类别词表 |

传统做法是六个任务各训一个专用模型；UniSeg3D 的核心主张是：**六类任务共享同一套「超点查询」表示，由同一个 Transformer 解码器处理**，任务之间还能互相「教」对方（知识蒸馏 + 对比学习）。

### 2.3 常见评价指标速查

- **mIoU**（mean Intersection over Union）：逐类计算预测点集与真值点集的交并比再取平均。

  \[ \text{IoU}_c = \frac{|P_c \cap G_c|}{|P_c \cup G_c|}, \qquad \text{mIoU} = \frac{1}{C}\sum_{c=1}^{C} \text{IoU}_c \]

- **mAP / AP**（mean Average Precision）：按置信度排序的预测序列，对精度-召回曲线积分。3D 实例分割常按 IoU 阈值 0.25 / 0.50 分别报告 AP。
- **PQ**（Panoptic Quality）：全景分割专属，把匹配（IoU > 0.5）的实例对记为 TP：

  \[ \text{PQ} = \frac{\sum_{(p,g)\in \text{TP}} \text{IoU}(p,g)}{|\text{TP}| + 0.5|\text{FP}| + 0.5|\text{FN}|} \]

不需要现在记住公式细节，只需记住「不同任务用不同尺子量」。

### 2.4 OpenMMLab 与「注册器」一词

UniSeg3D 基于 **MMDetection3D + mmengine** 框架构建。这类框架的核心机制是**注册器（Registry）**：每个类用 `@MODELS.register_module()` 之类装饰器登记自己的**类名字符串**，之后配置文件里写 `type='UniSeg3D'`，框架就能按字符串找到类并实例化。本讲只需建立这个直觉，第 2 单元（u2-l1）会深入拆解。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [README.md](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md) | 项目说明书：论文、安装、数据、训练推理命令、指标表 | News / Abstract / Overview / Models 四个章节 |
| [uniseg3d/\_\_init\_\_.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py) | 包入口：19 行 import，触发所有类注册 | 全部注册类的清单与分组 |
| [uniseg3d/uniseg3d.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py) | 主模型 `UniSeg3D`：组装骨干/解码器/语言分支/损失，含训练 `loss()` 与推理 `predict()` | 类定义、`__init__` 组件组装、整体数据流 |
| [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py) | 主配置：用 `type='XXX'` 字符串拼装整个系统 | 验证「配置字符串 ↔ 注册类」对应关系 |

## 4. 核心概念与源码讲解

### 4.1 README 章节导读：从论文到仓库

#### 4.1.1 概念说明

README 是了解一个开源项目最快速的入口。UniSeg3D 的 README 按时间线记录了项目演进（News）、论文摘要（Abstract）、方法总览图（Overview）、开发计划（TODO）和结果表（Models）。学会按「倒金字塔」读 README——先知道它宣称什么，再去源码里验证——是源码阅读的第一项基本功。

#### 4.1.2 核心流程

推荐的 README 阅读路线：

```text
1. 标题区（作者/机构/论文链接/项目主页）
2. News          → 项目成熟度：代码、checkpoint 是否齐全
3. Abstract      → 一段话读懂研究主张
4. Overview 图   → 直观建立架构印象
5. TODO          → 哪些已交付、哪些没有（避免踩坑）
6. Models 指标表 → 六任务 × 指标 × checkpoint 的对照
7. Installation / Dataset / Training / Inference → 后续讲义（u1-l2、u1-l4）再逐行精读
```

#### 4.1.3 源码精读

**① News：项目交付状态**。[README.md:L13-L17](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L13-L17) 显示推理代码、checkpoint、训练代码与日志、多卡训练、开放词汇流程均已释放，仅 Demo 未发布——说明这是一份**可以完整复现训练**的仓库，学习价值高。

**② Abstract：一段话的研究主张**。[README.md:L19-L20](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L19-L20) 中三个关键句值得逐个划出：

- "achieves panoptic, semantic, instance, interactive, referring, and open-vocabulary segmentation tasks **within a single model**" —— 一个模型六任务；
- "unifies six tasks into **unified representations** processed by **the same Transformer**" —— 统一表示 + 同一个 Transformer；
- "knowledge distillation and contrastive learning methods to **transfer task-specific knowledge across different tasks**" —— 任务间互相蒸馏/对比，这是第 5 单元（u5-l6）的主角。

**③ Overview 图**。[README.md:L22-L23](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L22-L23) 引用了 `content/introduction.png`，建议打开仓库时对照看：左侧是六类任务汇入同一个 Transformer，右侧是超点查询表示。

**④ TODO 与开放词汇入口**。[README.md:L25-L31](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L25-L31) 勾选了 5 项；其中 "Steps for Open-Vocabulary Segmentation" 对应 [README.md:L187-L198](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L187-L198) 的两阶段流程：先取消 `uniseg3d/uniseg3d.py` 中 L840-L872 的注释生成**类无关掩码**，再交给 Open3DIS 做类别判定——也就是说**开放词汇任务不在主推理循环里评估**，这是它与其余五任务的重要区别。

**⑤ Models 指标表**。[README.md:L200-L254](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L200-L254) 是本讲最重要的信息源，4.2 节会把它展开成对照表。

**⑥ 一次推理出五任务**。[README.md:L185](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L185) 明确写道："The PS, SS, IS, Interactive, Referring segmentation tasks are evaluated in **once inference**"——五个任务共享一次前向，这是「统一」二字的直接工程体现。

#### 4.1.4 代码实践

**实践：用 git 历史交叉验证 News 章节**

1. **实践目标**：把 README 的 News 文字与仓库真实提交时间线对上，建立「README 也是代码演进的一部分」的意识。
2. **操作步骤**：在仓库根目录运行：

   ```bash
   git log --oneline --date=short --pretty=format:"%h %ad %s" | head -20
   ```

3. **需要观察的现象**：提交信息中出现 "Support Multi-GPU training"、"Steps for Open-Vocabulary Segmentation"、"Update README.md" 等字样，与 News 中勾选的条目一一呼应。
4. **预期结果**：能找到至少 2 条与 News 条目对应的提交。具体提交哈希与日期取决于本地仓库状态，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：README 的 Abstract 说「统一表示 + 同一个 Transformer」，请指出这句承诺在配置文件里对应哪个 `type` 字符串？

**答案**：`'UniSeg3D'`（模型总装，[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L21](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L21)）与其内部的 `decoder=dict(type='UnifiedQueryDecoder', ...)`（[L47-L48](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L47-L48)）。「同一个 Transformer」即六类任务的查询都进这一个解码器。

**练习 2**：如果你想复现论文指标，需要下载哪几个文件？分别放在哪里？

**答案**：依据 [README.md:L172-L185](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L172-L185)：① CLIP 预训练权重 `open_clip_pytorch_model.bin` → `work_dirs/pretrained/convnext_large_d_320`；② checkpoint `model_best_2.pth` → `work_dirs/ckpts`；③ 数据侧还需 `uniseg3d_infos_train.pkl` / `uniseg3d_infos_val.pkl` / `scannet_cls_embedding.pth` → `data/scannet`（见 [README.md:L111-L127](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L111-L127)）。

### 4.2 六任务与评价指标对照表

#### 4.2.1 概念说明

「统一框架」并不意味着六任务输出相同——它们的**输入提示**（要不要额外条件）和**输出形式**（逐点类别 / 实例列表 / 单目标掩码）各不相同。把这张对照表刻进脑子，后面读任何模块都能立刻对号入座：这个模块在为哪个任务服务？

#### 4.2.2 核心流程

一次 `predict()` 前向中，六任务结果的产生路径：

```text
点云 + 超点
   │  稀疏化(spconv) → SpConvUNet → scatter_mean 聚合到超点
   ▼
超点特征列表（= 原始查询）
   │  拼接点提示查询（若有 point_prompts）
   │  拼接文本提示查询（若有 text_token）
   ▼
UnifiedQueryDecoder（同一个 Transformer，interaction_masks 隔离不同组）
   ├─ 原始查询 → 实例预测 / 语义预测 / 全景预测（PS·SS·IS 三任务）
   ├─ 点提示查询 → 交互式分割结果（Interactive）
   ├─ 文本提示查询 → 指代分割结果（Referring）
   └─ 类无关掩码（取消注释后保存）→ 交给 Open3DIS 分类（OV，两阶段）
```

关键点：**前五个任务共用一次前向**；OV 是「掩码生成 + 外部分类」的两阶段范式。

#### 4.2.3 源码精读

**① 六任务对照表（本讲核心产出）**。数据来自 [README.md:L200-L254](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L200-L254) 的 Models 表（表头行列于 [L202-L231](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L202-L231)，UniSeg3D 行于 [L232-L242](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L232-L242)，UniSeg3D\* 行于 [L243-L253](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L243-L253)）：

| 任务 | 数据集 | 输入提示 | 输出形式 | 指标 | UniSeg3D | UniSeg3D\* |
| --- | --- | --- | --- | --- | --- | --- |
| 全景 Panoptic (Pan.) | ScanNet20 | 无 | 每点的 stuff 语义 + things 实例统一编号 | PQ | 71.3 | 71.3 |
| 语义 Semantic (Sem.) | ScanNet20 | 无 | 每点一个语义类别 | mIoU | 76.3 | 76.9 |
| 实例 Instance (Inst.) | ScanNet20 | 无 | 实例掩码 + 类别 + 分数 | mAP | 59.1 | 59.3 |
| 交互 Interactive (Inter.) | ScanNet20 | 点提示（点坐标） | 点提示目标的掩码 | AP | 54.1 | 54.5 |
| 指代 Referring (Ref.) | ScanRefer | 文本提示（一句话） | 描述目标的掩码 | mIoU | 29.5 | 29.6 |
| 开放词汇 OV | ScanNet200 | 无（类别词表可扩展） | 类无关掩码 + 任意词表类别 | AP | 19.6 | 19.7 |

checkpoint 对应：UniSeg3D 行链接 `model_best.pth`，UniSeg3D\* 行链接 `model_best_2.pth`；而 [README.md:L176-L184](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L176-L184) 的推理命令用的正是 `model_best_2.pth`。星号版本的差异 README 未说明（**待确认**，仅知是不同 checkpoint）。

**② PS / SS / IS：同一次前向的三种后处理**。[uniseg3d/uniseg3d.py:L62-L91](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L62-L91) 的 `predict_by_feat` 依次调用三个函数——同一份解码输出 `out`，分别走实例、语义、全景三条后处理链，说明三个任务**共享同一组查询预测**，只是解读方式不同：

```python
inst_res = self.predict_by_feat_instance(...)   # 实例：topk + NMS + 阈值过滤
sem_res = self.predict_by_feat_semantic(...)    # 语义：sem_preds 逐查询 argmax
pan_res = self.predict_by_feat_panoptic(...)    # 全景：stuff 语义图 + things 实例合并
```

**③ Interactive / Referring：提示查询的直通输出**。[uniseg3d/uniseg3d.py:L20-L31](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L20-L31)（点提示）与 [uniseg3d/uniseg3d.py:L41-L52](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L41-L52)（文本提示）：提示查询的掩码预测直接 sigmoid 后按 `sp_score_thr` 二值化输出，不需要 NMS 与 topk——因为**一条提示天然对应一个目标**。

**④ OV：类无关掩码的两阶段范式**。[README.md:L187-L198](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L187-L198)：主模型只负责「把可能是物体的掩码都找出来」，类别判定交给 Open3DIS 里的基础模型。这解释了为什么 OV 指标无法像其他五任务一样在 `dist_test.sh` 里直接得到。

#### 4.2.4 代码实践

**实践：从配置里找出六任务的「数据侧开关」**

1. **实践目标**：验证「点提示 / 文本提示是数据管线注入的」，从而理解 Interactive 与 Referring 任务在推理时如何被激活。
2. **操作步骤**：打开 [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L201-L244](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L201-L244) 的 `test_pipeline`，找到 `MultiScaleFlipAug3D` 内部的四个 `uniseg3d` 自定义 transform；再用文本搜索在 [L177-L190](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L177-L190) 的 `train_pipeline` 里找出它们的训练版对应物。
3. **需要观察的现象**：`TextPromptTest` / `PointPromptTest`（测试）对应 `TextPromptGeneration` / `PointPromptGeneration`（训练）；`Pack3DDetInputs_` 的 `keys` 里出现了 `point_prompts`、`text_token` 等键名。
4. **预期结果**：整理出一张 4 行 2 列的小表（训练版 / 测试版 transform 名），并注意到这些键名与 [uniseg3d/uniseg3d.py:L465-L468](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L465-L468) 中 `if 'point_prompts' in batch_inputs_dict` / `if 'text_token' in batch_inputs_dict` 的判断条件一一对应——**模型按「批次里有没有这个键」来决定要不要启用提示分支**。

#### 4.2.5 小练习与答案

**练习 1**：为什么交互式分割的后处理（[uniseg3d/uniseg3d.py:L33-L39](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L33-L39)）不需要 `mask_matrix_nms`，而实例分割（[L137-L140](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L137-L140)）需要？

**答案**：实例分割会从几百上千个查询中挑出 topk 个候选，它们可能彼此重叠（同一物体被重复预测），需要 NMS 去重；点提示查询与目标是一一对应的（一次点击只求一个掩码），没有「多选一」问题，直接按阈值二值化即可。

**练习 2**：对照表中「Inter. 用 AP、Ref. 用 mIoU」，同样是「给一个提示、分割一个目标」，为什么指标不同？

**答案**：这是论文沿用的任务惯例——交互式分割通常按不同 IoU 阈值下的精度报告 AP，指代分割（ScanRefer 基准）惯例报告 mask IoU 均值；二者都衡量「单目标掩码质量」，只是尺子不同。具体到本仓库的实现细节（阈值取值、precision 计算）在第 6 单元 u6-l2 的 `PromptSupportedUnifiedSegMetric` 中精读。

**练习 3**：README 表格里 OV 列的 AP=19.6 是在哪个数据集上测的？为什么不能与其余五任务在同一次 `dist_test.sh` 中得到？

**答案**：ScanNet200。因为 OV 采用两阶段范式（[README.md:L187-L198](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L187-L198)）：先由本模型生成类无关掩码，再由 Open3DIS 完成类别判定与评估，评估流程在主仓库之外。

### 4.3 uniseg3d/\_\_init\_\_.py 注册清单速览

#### 4.3.1 概念说明

`uniseg3d/` 是项目唯一的自研代码包，而 [uniseg3d/\_\_init\_\_.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py) 只有 19 行 import——但这 19 行是整个项目的「总目录」：**每 import 一个模块，该模块里的 `@xxx.register_module()` 装饰器就执行一次，类名字符串进入注册表**。配置文件中的 `type='UniSeg3D'` 等字符串之所以能变成对象，全靠这份清单先被加载。

触发加载的开关在配置第一部分：[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L5](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L5) 的 `custom_imports = dict(imports=['uniseg3d'])`。

#### 4.3.2 核心流程

```text
train.py 启动
  → mmengine 读取 config，发现 custom_imports=['uniseg3d']
  → import uniseg3d 包 → __init__.py 逐行 import 子模块
  → 各子模块的 @MODELS/@TRANSFORMS/@DATASETS/@METRICS/@TASK_UTILS.register_module() 执行
  → 类名字符串进入各注册表
  → 之后 MODELS.build(cfg['model']) 按 type='UniSeg3D' 字符串实例化整棵模型树
```

#### 4.3.3 源码精读

**① 注册清单全貌**（[uniseg3d/\_\_init\_\_.py:L1-L19](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L1-L19)），按职能分组解读：

| 行号 | 注册的类 | 所属文件 | 职能与最相关的任务 |
| --- | --- | --- | --- |
| [L1](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L1) | `UniSeg3D` | uniseg3d.py | 主模型，六任务总装 |
| [L2](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L2) | `SpConvUNet` | spconv_unet.py | 稀疏卷积骨干，六任务共享 |
| [L3](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L3) | `UnifiedQueryDecoder`、`QueryDecoder` | query_decoder.py | 统一 Transformer 解码器；`sphere_cls` 与 OV 分类相关 |
| [L4](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L4) | `ScanNetUnifiedCriterion` | unified_criterion.py | 损失总调度，六任务联合监督 |
| [L5](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L5) | `ScanNetSemanticCriterion` | semantic_criterion.py | 语义分割（SS）损失 |
| [L6-L7](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L6-L7) | `QueryClassificationCost`、`MaskBCECost`、`MaskDiceCost`、`SparseMatcher` | instance_criterion.py | 查询-GT 匹配器（TASK_UTILS），服务 IS/PS/OV 损失 |
| [L8](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L8) | `LoadAnnotations3D_`、`NormalizePointsColor_` | loading.py | 数据加载/颜色归一化（带下划线 = 改造版） |
| [L9](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L9) | `Pack3DDetInputs_` | formatting.py | 打包批次，透传提示键 |
| [L10](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L10) | `ElasticTransfrom`、`AddSuperPointAnnotations` | transforms_3d.py | 弹性增广 / 超点标注构造（全任务地基） |
| [L11](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L11) | `Det3DDataPreprocessor_` | data_preprocessor.py | 批预处理外壳 |
| [L12](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L12) | `PromptSupportedUnifiedSegMetric` | unified_metric.py | 六任务统一评估器 |
| [L13](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L13) | `ScanNetUnifiedSegDataset` | scannet_dataset.py | 数据集类 |
| [L14](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L14) | `InstanceData_` | structures.py | 改造版实例标注结构 |
| [L15](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L15) | `LangModule` | lang_module.py | 语言分支封装，Referring/OV 的文本入口 |
| [L16](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L16) | `PointPromptInstanceCriterion` | point_prompt_instance_criterion.py | 交互式（Interactive）损失 |
| [L17](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L17) | `TextPromptInstanceCriterion` | text_prompt_instance_criterion.py | 指代（Referring）损失 |
| [L18](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L18) | `OpenCLIPBackboneText` | openclip_backbone.py | CLIP 文本塔加载，Referring/OV |
| [L19](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L19) | `ContrastiveCriterion` | contrastive_criterion.py | 跨任务对比/蒸馏损失（点↔文本） |

注意 `transforms_3d.py` 里还有 4 个类（`TextPromptGeneration`、`TextPromptTest`、`PointPromptGeneration`、`PointPromptTest`）虽未出现在 `__init__.py`，但随模块 import 一并注册（装饰器在 [transforms_3d.py:L12-L312](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L12-L312) 的各 class 定义上方）——**注册发生在模块被 import 时，与是否写在 `__init__.py` 里无关**，`__init__.py` 只是确保「这个模块被 import」。

**② 主模型如何消费这份清单**。[uniseg3d/uniseg3d.py:L285-L314](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L285-L314) 的 `__init__` 连续四次 `MODELS.build(...)`，把配置里的四个子 dict 变成四个子模块——这正是注册表机制的「消费端」：

```python
self.unet = MODELS.build(backbone)      # SpConvUNet
self.decoder = MODELS.build(decoder)    # UnifiedQueryDecoder
self.lang = MODELS.build(lang)          # LangModule
self.criterion = MODELS.build(criterion)  # ScanNetUnifiedCriterion
```

**③ 架构三件套在代码中的落点**。同一 `__init__` 里还能看到三块拼图的开关：`type_embedding`（[uniseg3d/uniseg3d.py:L326-L327](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L326-L327)）——`Embedding(3, num_channels)` 的"3"对应**三类查询**（原始 / 点提示 / 文本提示），是「同一个 Transformer 处理多来源查询」的直接证据；`pred_iou_proj`（[L332-L338](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L332-L338)）——三层 MLP 回归掩码 IoU，推理时用于加权实例分数。

**④ 统一表示的证据链**。`loss()`（[uniseg3d/uniseg3d.py:L419-L497](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L419-L497)）中的一段浓缩了全文思想：

```python
x = self.extract_feat(...)                    # 超点特征（L456-L457）
queries, sp_gt_instances = self._select_queries(x, ...)  # 超点特征直接当查询（L458）
if 'point_prompts' in batch_inputs_dict.keys():
    queries, point_prompt_instances = self.add_point_prompt_queries(...)  # 拼点提示查询（L465-L466）
if 'text_token' in batch_inputs_dict.keys():
    queries, text_prompt_instances = self.add_text_prompt_queries(...)    # 拼文本提示查询（L467-L468）
x = self.decoder(x, queries, interaction_masks=interaction_masks)        # 同一个 Transformer（L486）
```

配合 `_select_queries`（[uniseg3d/uniseg3d.py:L226-L256](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L226-L256)）可见：**查询不是可学习参数，而是从超点特征中采样而来**（配置里 `num_instance_queries=0`，即「超点即查询」），这就是 Abstract 所说 unified representations 的真正含义。

#### 4.3.4 代码实践

**实践：亲手数一遍注册表，并与配置对账**

1. **实践目标**：验证「`__init__.py` 清单 = 配置里出现的所有 `type` 字符串的超集」，并标注每个类挂在哪个注册器（MODELS/TRANSFORMS/DATASETS/METRICS/TASK_UTILS）。
2. **操作步骤**：在仓库根目录运行（只读操作，不修改任何源码）：

   ```bash
   grep -rn "register_module()" uniseg3d/ --include="*.py" | sort
   grep -n "type='" configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py | sort | uniq -c | sort -rn | head
   ```

3. **需要观察的现象**：第一条命令输出约 25 个装饰器（分布在 uniseg3d/ 的 10 个文件里）；第二条命令列出配置中高频出现的 `type`，如 `LoadPointsFromFile`、`AddSuperPointAnnotations`、`TextPromptTest`、`PointPromptTest` 等。
4. **预期结果**：把两份输出并排核对——配置中的每个自定义 `type` 都能在第一条命令的结果中找到对应类；再按注册器分组统计（`@MODELS` 约 14 个、`@TRANSFORMS` 8 个、`@METRICS` 2 个、`@DATASETS` 1 个、`@TASK_UTILS` 4 个）。具体数字**待本地验证**，以你的 grep 输出为准。
5. 一个值得记录的发现：`PromptSupportedSegMetric`（[uniseg3d/unified_metric.py:L24-L25](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L24-L25)）也被注册了，但配置里用的是它的子类 `PromptSupportedUnifiedSegMetric`——注册表允许「注册了但配置未使用」。

#### 4.3.5 小练习与答案

**练习 1**：配置里 `type='UniSeg3D'` 这个字符串是怎么找到 `uniseg3d/uniseg3d.py` 里的类的？请写出完整触发链。

**答案**：mmengine 读配置发现 `custom_imports=['uniseg3d']`（配置 L5）→ import `uniseg3d` 包，`__init__.py` 第 1 行 `from .uniseg3d import UniSeg3D` → 模块加载时类定义上方的 `@MODELS.register_module()`（[uniseg3d/uniseg3d.py:L259](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L259)）执行，`'UniSeg3D'` 进入 MODELS 注册表 → `MODELS.build(dict(type='UniSeg3D', ...))` 查表实例化。

**练习 2**：`LoadAnnotations3D_`、`Pack3DDetInputs_`、`Det3DDataPreprocessor_`、`InstanceData_` 这些类名都带下划线后缀，为什么？

**答案**：它们是对 mmdet3d 原生类（`LoadAnnotations3D`、`Pack3DDetInputs`、`Det3DDataPreprocessor`、`InstanceData`）的**继承改造版**（如 [uniseg3d/loading.py:L12](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py#L12) 的 `class LoadAnnotations3D_(LoadAnnotations3D)`），加下划线区分以避免覆盖原生注册。改造目的主要是让超点掩码与点/文本提示相关键能流过数据管线，细节在第 3 单元（u3-l2）精读。

**练习 3**：如果注释掉 `uniseg3d/__init__.py` 的第 16 行（`PointPromptInstanceCriterion` 的 import），训练还能启动吗？

**答案**：不能正常启动。配置 `criterion.point_prompt_inst_criterion=dict(type='PointPromptInstanceCriterion', ...)`（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L101-L108](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L101-L108)）会在构建 criterion 时按该字符串查注册表，查不到会抛出「xxx is not in the MODELS registry」类错误。可自行注释后运行 `python tools/train.py configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py --cfg-options train_cfg.max_epochs=1` 验证报错信息（**待本地验证**）。

## 5. 综合实践

**任务：制作你的「六任务 × 提示 × 输出 × 指标 × 相关类」总对照表**

这是本讲的核心动手任务，产出一张贯穿全手册的速查表。

1. **实践目标**：把 4.1～4.3 三个模块的信息收敛为一张表，作为后续所有讲义的「地图例」。
2. **操作步骤**：
   - 重读 [README.md:L200-L254](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L200-L254) 的 Models 表与 [README.md:L187-L198](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L187-L198) 的 OV 章节，打开项目主页（[README.md:L9](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L9) 给出的链接）查看架构总览图。
   - 为每个任务填写 6 列：输入提示 / 输出形式 / 评估指标（含数值）/ 数据集 / 数据侧 transform 类 / 模型侧最相关类。
   - 数据侧类名从配置 `train_pipeline`/`test_pipeline` 找（如 `PointPromptGeneration`↔Interactive、`TextPromptGeneration`↔Referring）；模型侧类名从 `__init__.py` 清单找（如 `PointPromptInstanceCriterion`↔Interactive、`TextPromptInstanceCriterion`↔Referring、`OpenCLIPBackboneText`/`LangModule`↔Referring+OV、`ScanNetSemanticCriterion`↔SS、`OVInstanceCriterion`↔IS/PS/OV）。
3. **需要观察的现象**：填写过程中你会发现 PS/SS/IS 三个任务在数据侧和模型侧**几乎共享全部类**——它们本就共享同一组查询预测，只是后处理与损失不同；而 Interactive/Referring 各自有专属的 transform 与 criterion。
4. **预期结果**：一张 6 行 6 列的表。参考骨架（请自行补全空格）：

   | 任务 | 输入提示 | 输出形式 | 指标(数值) | 数据侧 transform | 模型侧类 |
   | --- | --- | --- | --- | --- | --- |
   | 全景 PS | 无 | stuff 语义 + things 实例统一编号 | PQ (71.3) | AddSuperPointAnnotations | UniSeg3D + UnifiedQueryDecoder + OVInstanceCriterion |
   | 语义 SS | … | … | … | … | … |
   | 实例 IS | … | … | … | … | … |
   | 交互 Inter. | … | … | … | PointPromptGeneration/Test | … |
   | 指代 Ref. | … | … | … | … | … |
   | 开放词汇 OV | … | … | … | … | … |

5. 把这张表保存在你的笔记里——第 6 单元讲评估时（u6-l2）你会回来把「指标」一列补上精确的阈值定义。

## 6. 本讲小结

- UniSeg3D 用**一个模型、一次训练**统一六类 3D 分割任务（PS/SS/IS/Interactive/Referring/OV），核心是「超点即查询」的统一表示 + 同一个 `UnifiedQueryDecoder`。
- 六任务输入提示与输出形式各异：三个无提示任务共享同一组查询预测、只差后处理；两个提示任务由数据管线注入点/文本提示查询；OV 是「类无关掩码 + Open3DIS 外部分类」的两阶段范式。
- 各任务的尺子不同：PQ（全景）、mIoU（语义/指代）、mAP（实例）、AP（交互/OV）；README Models 表给出全部数值与两个 checkpoint 的对应。
- `uniseg3d/__init__.py` 的 19 行 import 是全项目注册清单；`custom_imports` 触发加载，配置中的 `type` 字符串经 `Registry` 变成对象——这是阅读一切 OpenMMLab 系仓库的基本功。
- 主模型 `UniSeg3D.__init__` 用四次 `MODELS.build` 组装骨干/解码器/语言分支/损失；`Embedding(3, ...)` 型嵌入区分三类查询来源，是「统一 Transformer」的直接代码证据。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：环境搭建与训练/推理脚本——亲手装好 PyTorch 1.13、OpenMMLab、MinkowskiEngine、segmentator，逐行读懂 `tools/dist_train.sh` 与 `tools/dist_test.sh`。
- **提前阅读的源码**：`tools/train.py`（入口极简，主角是 mmengine 的 Runner）与 `tools/dist_train.sh`（不足 10 行，值得逐行注释）。
- **延伸阅读**：论文 [arXiv:2407.03263](https://arxiv.org/abs/2407.03263) 的 Introduction 与 Method 章节，把本讲的六任务对照表与论文 Figure 1 对照看。
- **若想先睹为快**：浏览 `uniseg3d/query_decoder.py` 的 `UnifiedQueryDecoder`（第 4 单元 u4-l3/u4-l4 的主角），只看类注释与 `__init__` 参数，感受「统一解码器」的规模。
