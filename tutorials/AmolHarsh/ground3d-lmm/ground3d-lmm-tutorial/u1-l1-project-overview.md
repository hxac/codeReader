# Ground3D-LMM 是什么：项目定位与总体架构

> 所属手册：ground3d-lmm 学习手册 · 单元 1 第 1 讲（beginner）
> 本讲是整套手册的起点，不假设你读过任何代码，也不需要先装好环境。

## 1. 本讲目标

读完本讲，你应该能够：

1. 用自己的话说出 Ground3D-LMM 要解决的问题：在室内 3D 点云上做**细粒度点级接地（grounding）**与**空间推理**，并同时输出自然语言回答和逐点分割掩码。
2. 列出它覆盖的子任务类型（README 小节中枚举的子任务清单），并说出每类任务的输入输出形式。
3. 顺着「点云 → SpConvUNet → 超点池化 → Qwen3-VL + 查询解码器 → `<SEG>` 掩码」这条数据流，把 README 里的每个组件对应到仓库中的真实源码文件。
4. 区分发布的两个模型变体：**Joint（3D 点云 + RGB 帧）** 与 **3D-only（仅点云）**，并知道它们的配置文件只差三个字段。

## 2. 前置知识

本讲用到的概念都会在下文通俗解释。如果你已熟悉，可以跳过本节。

- **点云（point cloud）**：3D 空间中的一堆点，每个点至少有 XYZ 坐标。本项目用的是 XYZ + RGB 共 6 维的点（README 中写作 `load_dim=6`）。
- **接地 / grounding**：把语言里的指代表达（如「椅子的扶手」）定位到传感器数据中的具体区域。3D 点接地就是把一句话映射成点云上的一个逐点掩码（mask），即每个点一个 0/1 标签。
- **超点（superpoint）**：把场景点云预先过聚类（如 SSTNet），得到若干个「局部连贯的小块」。每个超点包含许多点。用超点而不是点作为基本单元，能把成千上万个点压缩成几百到几千个 token，是大模型吃下 3D 场景的关键技巧。
- **LMM / VLM（大型多模态模型）**：能同时理解多种模态（文本、图像等）并生成文本的大模型。本项目用 **Qwen3-VL-4B** 作为语言底座。
- **LoRA（Low-Rank Adaptation）**：一种参数高效微调方法。冻结原模型权重，只在旁边插入两个低秩矩阵 \(A\in\mathbb{R}^{r\times d}\)、\(B\in\mathbb{R}^{d\times r}\)，训练时只更新 \(BA\)。秩 \(r\) 越大，可训练参数越多。缩放系数记为 \(\alpha\)。README 中两个变体分别用 r=16/α=32 与 r=32/α=64。
- **Mask2Former 式解码器**：一类「查询（query）+ 交叉注意力 + 掩码预测」的分割头。一组可学习的查询向量不断从特征里「取信息」，最后每个查询打出一个掩码。
- **token / 特殊 token**：大模型的输入被切成 token。可以在词表里注册**特殊 token**（如 `<|point|>`、`<SEG>`），让模型学会「这个位置有特殊含义」。
- **OpenMMLab（mmengine / mmdet3d）**：一套用 Python 配置文件 + 注册表来组装数据集、模型、训练循环的框架。配置里写 `type='Grounded_LMM_main'` 这样的字符串，框架会去注册表里找同名的类并实例化它。第 3 讲会细讲。

## 3. 本讲源码地图

本讲以 README 为主，辅以少量「入口级」源码，只看类定义行，不深入实现。

| 文件 | 作用 | 本讲怎么看 |
|---|---|---|
| [README.md](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md) | 项目定位、架构说明、数据集统计、模型动物园 | 全读，本讲主教材 |
| [configs/ground3dlmm_eval_ground3d_scannet.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py) | Joint 变体在 ScanNet 上的评估配置 | 看 `model = dict(...)` 如何点名各组件 |
| [configs/ground3dlmm_eval_ground3d_scannet_3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet_3d.py) | 3D-only 变体配置，仅覆盖三个字段 | 用来理解两种变体的差异 |
| [tools/evaluate_text_metrics.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py) | 文本指标评估脚本 | 只看任务名集合，交叉验证子任务清单 |
| [uniseg3d/uniseg3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py) | 模型主入口 `Grounded_LMM_main` | 记住类名和行号即可 |
| [uniseg3d/spconv_unet.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/spconv_unet.py) | 稀疏卷积点云编码器 | 同上 |
| [uniseg3d/point_qwen3_vl.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py) | 接收点云 token 的 Qwen3-VL | 同上 |
| [uniseg3d/query_decoder.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py) | Mask2Former 式查询解码器 + 语言分支 | 同上 |

## 4. 核心概念与源码讲解

### 4.1 项目定位与子任务

#### 4.1.1 概念说明

Ground3D-LMM 是一个「会看 3D 房间、会答话、还会指出来」的大模型。给定一段室内场景点云（XYZ + RGB）和一个问题，它同时输出：

1. **自然语言回答**（由 Qwen3-VL 生成）；
2. **逐点分割掩码**（回答中出现 `<SEG>` 标记时，每个 `<SEG>` 对应点云上一块区域）。

项目定位见 [README.md:L16-L18](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L16-L18)：这是 ECCV 2026 论文的官方代码，在 ScanNet 与 ScanNet++ 两个室内数据集上训练，训练数据是 247 万问答对。

「细粒度」体现在它不止能指**物体**（object，如「那把椅子」），还能指物体的**部件**（part，如「椅子的扶手」「桌子的抽屉」），并要求**数量级/尺寸/距离**等空间推理能力。

#### 4.1.2 核心流程

一个典型的问答样本可以这样理解（示意，非项目原文）：

```text
输入：场景点云 P ∈ ℝ^(N×6)（XYZ+RGB），问题 q = "How wide is the seat of the chair?"
输出：回答 a = "The seat is about 0.45 m wide. <SEG>"
      掩码 m ∈ {0,1}^N，标记出「座椅面」对应的点
```

README 的「Sub-tasks」小节枚举了子任务名，[README.md:L137-L146](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L137-L146) 列出 9 个名字，可按输入输出形式分成三组：

| 分组 | 子任务 | 输入 | 输出 |
|---|---|---|---|
| 部件/物体接地 | `functional_part_grounding`、`functional_object_grounding` | 功能描述（「用来放杯子的是哪部分」） | 文本 + `<SEG>` 部件/物体掩码 |
| 数值测量 | `scale_estimation`、`grounded_dimension_reasoning`、`distance_estimation`、`scale_comparison_size` | 关于尺寸/距离的问题 | 数值（米）回答，通常带 `<SEG>` |
| 空间关系与判断 | `relative_depth_forward`、`relative_position_forward_reasoning`、`existence_verification` | 前后/左右/存在性问题 | 文本判断；`existence_verification` 的纯是/否回答**不带掩码** |

> ⚠️ 阅读时注意一个真实的文档不一致：README 开头 [L18](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L18) 和数据集小节 [L124](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L124) 都写「8 sub-tasks」，但子任务清单实际枚举了 **9** 个名字。代码侧（见 4.1.3）能识别全部 9 个名字。以哪个为准？本手册建议：**记任务名本身，不纠结计数**；论文正文以 arXiv 版为准。这是读开源项目常见的「README 漂移」，学会交叉验证比背数字更重要。

#### 4.1.3 源码精读

**① 项目一句话定位**（作者列表 + 摘要句）：

[README.md:L16-L18](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L16-L18) 说明模型产出「per-point segmentation masks alongside grounded natural-language answers」，即**掩码与接地回答同时输出**——这是理解整个架构的钥匙。

**② 子任务清单**：

[README.md:L137-L146](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L137-L146) 枚举子任务名，完整的 schema 在 HuggingFace 数据集卡上（README [L148](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L148) 给出链接）。

**③ 代码侧的任务名集合**（交叉验证用）：

[tools/evaluate_text_metrics.py:L44-L50](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L44-L50) 定义了 4 个「数值任务」：

```python
NUMERIC_TASKS = {
    "distance_estimation",
    "grounded_dimension_reasoning",
    "scale_estimation",
    "scale_comparison_size",
}
```

这 4 个任务会做 LLM 数字抽取并计算 APE / δ@1.25 误差指标。

[tools/evaluate_text_metrics.py:L83-L88](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L83-L88) 把其余任务名也列进 `_KNOWN_TASKS`，用于从预测 JSON 的目录名里反推每个样本属于哪个任务：

```python
_KNOWN_TASKS = NUMERIC_TASKS | {
    "functional_part_grounding", "functional_object_grounding",
    "relative_position_forward_reasoning", "relative_depth_forward",
    "existence_verification", "multi_turn_qa_data",
}
```

对照可知：代码认识 README 清单里的全部 9 个子任务名，外加一个 `multi_turn_qa_data`（这是**多轮对话层级**的名字，不是第 10 个子任务）。

#### 4.1.4 代码实践：用 grep 交叉验证子任务清单

这是一个「源码阅读型实践」，不需要安装环境，只要仓库在本机即可。

1. **实践目标**：不依赖记忆，用工具确认「README 说了几个子任务、代码认识哪几个」。
2. **操作步骤**：
   - 在仓库根目录执行：
     ```bash
     grep -n "sub-task" README.md | head
     grep -n -A6 "_KNOWN_TASKS = " tools/evaluate_text_metrics.py
     grep -rn "scale_estimation" configs/ uniseg3d/ | head
     ```
3. **需要观察的现象**：
   - README 中「8 sub-tasks」出现两处（L18、L124），而清单是 9 行；
   - `_KNOWN_TASKS` 覆盖 9 个子任务名 + `multi_turn_qa_data`；
   - `scale_estimation` 同时出现在 README、configs、tools、uniseg3d 四处，说明一个任务名会贯穿「数据标注 → 数据管线 → 评估」全链路。
4. **预期结果**：你得到一张「任务名 → 出现文件」的对照表，后续讲义（u2 数据侧、u6 评估）会反复用到这些名字。
5. 运行结果依赖你本机的 grep 输出，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`existence_verification` 与其他子任务在「输出形式」上有什么本质区别？

> **答案**：它问的是「场景里有没有 X」，纯是/否回答不需要指认区域，所以不产生 `<SEG>` 掩码。README [L327-L329](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L327-L329) 明确说明：计算 mIoU 时这类无掩码样本被直接排除在该子任务的 mIoU 之外。

**练习 2**：哪 4 个子任务属于「数值任务」？依据是哪个文件的哪几行？

> **答案**：`distance_estimation`、`grounded_dimension_reasoning`、`scale_estimation`、`scale_comparison_size`，定义在 [tools/evaluate_text_metrics.py:L44-L50](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L44-L50) 的 `NUMERIC_TASKS`；README [L285-L287](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L285-L287) 也列出了同样的 4 个。

**练习 3**：如果让你给「细粒度（fine-grained）」找一个反义词来形容传统 3D 检测/分割，会是什么？

> **答案**：粗粒度的「物体级」输出（bounding box 或整物体掩码）。Ground3D-LMM 下探到部件级（part-level），并且把接地与语言回答绑在同一个模型里，而不只是输出一个类别标签。

### 4.2 四组件架构与 `<SEG>` 接地机制

#### 4.2.1 概念说明

README [L24-L28](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L24-L28) 把架构拆成四个组件：

1. **SpConvUNet**：稀疏 3D 卷积 U-Net，把原始点云编码成逐点特征（继承自 UniSeg3D）。
2. **Qwen3-VL-4B**：语言底座，用 LoRA 微调，负责理解问题、生成回答。
3. **Mask2Former 式查询解码器**：6 层、256 维，把「查询」变成逐点掩码。
4. **超点池化（superpoint pooling）**：把逐点特征压成逐超点特征，让跨注意力在可控长度上做。

四个组件里最「巧」的设计是 **`<SEG>` 机制**：模型生成回答时，在需要指认的位置输出特殊标记 `<SEG>`；随后取语言模型最后一层隐状态中 `<SEG>` 位置的那个向量，作为分割解码器的查询。也就是说，**语言模型「说出的那个词」本身就被复用为分割查询**，语言与分割在同一个前向里自然对齐，不需要单独的指代解析模块。

#### 4.2.2 核心流程

README [L38-L40](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L38-L40) 用一段话写清了整条数据流，可以画成：

```text
                     ┌────────────────────────────────────────────┐
 点云 P ∈ ℝ^(N×6) ──▶│ ① SpConvUNet 稀疏编码                      │
                     │    逐点特征 f ∈ ℝ^(N×d)                     │
                     └───────────────┬────────────────────────────┘
                                     │ 按预计算超点做 scatter-mean 池化
                                     ▼
                     超点特征 Fp ∈ ℝ^(M×d)   （M ≪ N，README 记号）
                                     │
              ┌──────────────────────┴──────────────────────┐
              ▼ 投影①：作为 <|point|> token 嵌入             ▼ 投影②：作为 K/V
   ┌────────────────────────┐                    ┌────────────────────────┐
   │ ② Qwen3-VL-4B (LoRA)   │  文本 + <|point|>   │ ③ 查询解码器（6 层      │
   │  (+ 可选 RGB 帧)        │  (+ 可选图像)       │   Mask2Former 式）      │
   │  生成回答，含 <SEG>      │                    │  Cross-Attn→Self-Attn  │
   └───────────┬────────────┘                    │  →FFN × 6              │
               │ 取 <SEG> 处隐状态 → 细化查询      └───────────┬────────────┘
               └───────────────────────┬─────────────────────┘
                                       ▼
                          逐点特征点积打分 → 分割掩码 mask ∈ ℝ^N
```

要点（对应 README 原文措辞）：

- `Fp` 通过 `scatter-mean` 池化得到，M 是超点数；
- `Fp` **并行**投影成两路：一路进 LMM 当 `<|point|>` token，一路进掩码解码器当 K/V；
- 解码器每层是 Cross-Attn → Self-Attn → FFN，共 6 层；
- 每个 `<SEG>` 的隐状态成为一个「细化后的查询」，与逐点特征做点积得到掩码。

#### 4.2.3 源码精读

**① 架构总述原文**：

[README.md:L36-L40](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L36-L40) 是全项目最浓缩的一段技术描述（架构图 `images/method.png` 也在这里）。上面 4.2.2 的流程图就是它的逐句展开。

**② 组件在配置里如何被点名**：

评估配置 [configs/ground3dlmm_eval_ground3d_scannet.py:L25-L30](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L25-L30) 里，顶层模型类型就是主入口类：

```python
model = dict(
    type='Grounded_LMM_main',
    data_preprocessor=dict(type='Det3DDataPreprocessor_'),
    in_channels=6,          # 点的维度：XYZ + RGB
    ...
```

`decoder=dict(...)` 一大块（[L44-L74](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L44-L74)）同时容纳了「LMM + 查询解码器」：`type='Grounded_Decoder_Eval'`、`num_layers=6`、`d_model=256` 正对应 README 说的「6 layers, 256-d」；`qwen_model_path`、`lora_r` 等 LMM 字段也在这一块里。稀疏主干则在更上方的 `unet=dict(...)`（其中 `num_planes` 是 5 级通道数列表）。

**③ 组件对应的真实类**（本讲只记位置，后续单元再逐个精读）：

| 组件 | 类定义位置 | 一句话职责 |
|---|---|---|
| 模型主入口 | [uniseg3d/uniseg3d.py:L906](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L906) `class Grounded_LMM_main` | 组装 unet + decoder + criterion，实现 `loss()` / `predict()` |
| 稀疏主干 | [uniseg3d/spconv_unet.py:L95](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/spconv_unet.py#L95) `class SpConvUNet` | 点云 → 逐点特征 |
| 点云版 Qwen3-VL | [uniseg3d/point_qwen3_vl.py:L67](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py#L67) `class PointQwen3VLForConditionalGeneration` | 继承官方 Qwen3-VL，注入 `<\|point\|>` 嵌入 |
| 解码器（训练/推理） | [uniseg3d/query_decoder.py:L881](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L881) `class Grounded_Decoder_Joint`；[L1982](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1982) `class Grounded_Decoder_Eval` | 语言分支 + 6 层查询解码器，训练/评估各一个变体 |

> 注意 README「Repository structure」小节（[L368-L400](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L368-L400)）把解码器写作 `Grounded_Decoder_Joint / _Eval`，实际类名是 `Grounded_Decoder_Joint` 与 `Grounded_Decoder_Eval`（无 `_Joint_Eval` 这种类）。

**④ 数据集规模**（架构的「燃料」）：

[README.md:L122-L130](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L122-L130) 的统计表：总计 **2,475,307** 问答对；ScanNet 1,433 训练 / 80 验证场景，ScanNet++ 857 / 48；按层级分 object QA 995,156、part QA 1,408,304、multi-turn 71,847。ScanNet++ 场景数与论文略有出入的原因见 [L132-L135](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L132-L135)（部分场景缺深度/网格文件或 QA 在 VLM 校验阶段丢失），但问答对总数与论文一致。

#### 4.2.4 代码实践：把配置文件读成一张组件表

1. **实践目标**：验证 4.2.2 流程图中的每个组件都能在配置文件里找到对应字段，建立「图 ↔ 配置 ↔ 类」三向映射。
2. **操作步骤**：
   ```bash
   sed -n '25,80p' configs/ground3dlmm_eval_ground3d_scannet.py
   ```
   然后边读边填下面这张表（示例答案已给出，请亲自核对行号）：

   | 流程图组件 | 配置字段 | 取值 |
   |---|---|---|
   | 主入口 | `model.type` | `Grounded_LMM_main` |
   | ① 稀疏主干 | `model.unet.type`（块内 `num_planes=[...]`，5 级通道） | `SpConvUNet` |
   | ②+③ 解码器块 | `model.decoder.type` | `Grounded_Decoder_Eval` |
   | ③ 层数/维度 | `model.decoder.num_layers` / `d_model` | 6 / 256 |
   | ② 语言底座 | `model.decoder.qwen_model_path` | `Qwen/Qwen3-VL-4B-Instruct` |
   | ② LoRA | `model.decoder.lora_r` / `lora_alpha` | 16 / 32 |
   | 点 token 预算 | `model.decoder.max_point_tokens` | 2000 |
3. **需要观察的现象**：LMM 相关字段（`qwen_model_path`、`lora_*`、`max_point_tokens`、`image_dir`）全部长在 `decoder` 这一块里，而不是顶层 `model` 下——这是因为「语言分支 + 查询解码器」在代码里由同一个类（`Grounded_Decoder_*`）实现。
4. **预期结果**：你能不翻讲义，指着配置文件说出每个字段对应流程图中的哪个框。
5. 表中取值均取自真实文件，可对照 [configs/ground3dlmm_eval_ground3d_scannet.py:L44-L74](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L44-L74)。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Fp` 要「并行」投影两路，而不是让 LMM 直接输出掩码？

> **答案**：语言模型的输出空间是文本 token，不天然带空间定位能力。一路把超点特征送进 LMM 供其「看见」场景，另一路把同样的特征送进掩码解码器当 K/V；两边共享同一份几何表征，再用 `<SEG>` 隐状态把两路缝合起来。这样语言与分割对齐，且掩码预测交给擅长空间打分的解码器。

**练习 2**：超点池化解决的是什么问题？如果没有它会怎样？

> **答案**：一个室内场景动辄十几万个点，逐点当 token 会远超 LMM 可接受的序列长度（配置里 `max_point_tokens=2000`）。按超点做 scatter-mean 后，token 数从 N 降到 M（超点数，通常几百~几千），注意力开销大幅下降；代价是超点内部分辨率受聚类粒度限制。

**练习 3**：`<|point|>` 和 `<SEG>` 两个特殊 token 分别出现在数据流的哪一端？

> **答案**：`<|point|>` 在**输入端**——超点嵌入被写进输入序列中 `<|point|>` token 的位置（实现见 `uniseg3d/point_qwen3_vl.py`，第 4 单元精读）；`<SEG>` 在**输出端**——模型生成回答时打出它，其位置的最后一层隐状态被取出来当分割查询（实现见 `uniseg3d/query_decoder.py`）。

### 4.3 模型动物园与数据集规模：Joint 与 3D-only 两种变体

#### 4.3.1 概念说明

项目发布了两个权重（[README.md:L202-L213](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L202-L213)）：

| 模型 | 输入模态 | LoRA (r/α) | 评估配置 |
|---|---|---|---|
| Ground3D-LMM-4B (Joint) | 3D 点云 + RGB 帧 | 16/32 | `ground3dlmm_eval_ground3d_{scannet,scannetpp}.py` |
| Ground3D-LMM-4B (3D-only) | 仅 3D 点云 | 32/64 | `..._{scannet,scannetpp}_3d.py` |

- **Joint** 是论文主结果：Qwen3-VL 本来就是视觉-语言模型，把与点云对齐的 RGB 帧一起喂进去，语言模型可以同时利用 2D 外观与 3D 几何。
- **3D-only** 不需要图像：ScanNet/ScanNet++ 原始图像不可再分发（README [L195-L200](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L195-L200)），想省去下载原始数据集麻烦的用户用这个变体即可，配置里 `image_dir=None`。
- 两个底座都是 **Qwen3-VL-4B**（4.47B 参数，约 400M LoRA 可训练参数）。

#### 4.3.2 核心流程

两个变体的配置关系是「继承 + 只改差异字段」：

```text
configs/ground3dlmm_eval_ground3d_scannet.py      （Joint：image_dir='data/scannet/frame_images'，r=16）
        ▲  _base_ 继承
configs/ground3dlmm_eval_ground3d_scannet_3d.py   （3D-only：只覆盖 lora_r / lora_alpha / image_dir 三个字段）
```

为什么 LoRA rank 必须与 checkpoint 匹配？3D-only 配置的 docstring 给出了非常实用的解释（见下），这也是本讲值得记住的一个工程坑。

#### 4.3.3 源码精读

**① 模型动物园与自动变体探测**：

[README.md:L202-L213](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L202-L213) 给出两个 checkpoint 的表格，并说明 `eval_ground3d.sh` 会探测 checkpoint 的 LoRA rank（r=16 → Joint，r=32 → 3D-only）自动选配置。

**② Joint 配置中的关键字段**：

[configs/ground3dlmm_eval_ground3d_scannet.py:L65-L74](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L65-L74) 中 `qwen_model_path='Qwen/Qwen3-VL-4B-Instruct'`、`lora_r=16`、`lora_alpha=32`、`max_point_tokens=2000`、`image_dir='data/scannet/frame_images'`——这就是 Joint 变体的全部「身份信息」。

**③ 3D-only 配置的全部内容**：

[configs/ground3dlmm_eval_ground3d_scannet_3d.py:L1-L18](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet_3d.py#L1-L18) 的 docstring 解释了为什么必须单独一份配置：把 r=32 的权重装进 r=16 的模型时，不匹配的 adapter 会被 `strict=False` **静默忽略**，模型悄悄退回 base Qwen 而不报错。这是 LoRA 工程里极隐蔽的坑。

[configs/ground3dlmm_eval_ground3d_scannet_3d.py:L20-L27](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet_3d.py#L20-L27) 是这个文件除注释外的**全部代码**：

```python
_base_ = ['./ground3dlmm_eval_ground3d_scannet.py']

model = dict(
    decoder=dict(
        lora_r=32,
        lora_alpha=64,
        image_dir=None,
    ),
)
```

只覆盖 `lora_r`、`lora_alpha`、`image_dir` 三个字段，与 README [L92-L93](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L92-L93) 的说法完全一致。

**④ 训练数据规模回顾**：见 4.2.3 ④ 引用的统计表；训练入口与超参在 README [L215-L241](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L215-L241)（第 5 单元精读）。

#### 4.3.4 代码实践：diff 两个变体的配置

1. **实践目标**：亲眼验证「Joint 与 3D-only 只差三个字段」，并理解 `_base_` 继承如何让差异保持最小。
2. **操作步骤**：
   ```bash
   cat configs/ground3dlmm_eval_ground3d_scannet_3d.py
   grep -c "" configs/ground3dlmm_eval_ground3d_scannet.py   # 数一数主配置有多少行
   grep -n "image_dir\|lora_r\|lora_alpha" configs/ground3dlmm_eval_ground3d_scannet.py
   ```
3. **需要观察的现象**：`_3d` 配置去掉注释后只有约 8 行有效代码；三个覆盖字段在主配置中都有同名默认值（16/32/`'data/scannet/frame_images'`）。
4. **预期结果**：你能说出「换一个 checkpoint 时，配置侧需要动什么」——答案是尽量不动，靠继承覆盖；这正是 OpenMMLab 配置体系的典型用法（第 3 讲展开）。
5. 输出行号以你本地仓库为准，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果你把 3D-only 的 checkpoint（r=32）直接塞进 Joint 配置（r=16）里评估，最可能出现什么现象？

> **答案**：不是报错，而是 adapter 形状不匹配被 `strict=False` 静默跳过，模型退化成未微调的 base Qwen3-VL，指标明显掉但不报错。依据 [configs/ground3dlmm_eval_ground3d_scannet_3d.py:L7-L13](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet_3d.py#L7-L13) 的 docstring。

**练习 2**：3D-only 变体存在的现实理由之一是什么？

> **答案**：Joint 变体需要与点云对齐的 RGB 帧，而 ScanNet/ScanNet++ 图像不可再分发（[README.md:L195-L200](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L195-L200)），用户须自行下载原始数据集并跑 `tools/build_frame_images.py` 抽帧。3D-only 完全绕开这一步。

**练习 3**：`eval_ground3d.sh --variant auto` 是靠什么区分两个变体的？

> **答案**：读 checkpoint 里的 LoRA rank：r=16 判为 Joint，r=32 判为 3D-only，然后自动选择匹配的评估配置（[README.md:L259-L263](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L259-L263)）。

## 5. 综合实践

**任务：写一页「Ground3D-LMM 速览卡」，并向同伴讲解 60 秒。**

具体做法：

1. 打开 [README.md:L36-L40](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L36-L40) 和 [configs/ground3dlmm_eval_ground3d_scannet.py:L25-L74](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L25-L74)，在纸上手抄（不要复制粘贴）这条链路，并在每个箭头旁标注张量语义：
   - 点云 \(P \in \mathbb{R}^{N\times 6}\) → SpConvUNet → 逐点特征 \(f \in \mathbb{R}^{N\times d}\)
   - → 超点 scatter-mean → \(F_p \in \mathbb{R}^{M\times d}\)
   - → 两路投影（`<|point|>` token / 解码器 K-V）
   - → Qwen3-VL 生成含 `<SEG>` 的回答 → `<SEG>` 隐状态作查询 → 6 层解码器 → 掩码 \(\in \{0,1\}^N\)
2. 在速览卡背面写下三件「我验证过的事实」，每件附文件与行号，例如：
   - 解码器是 6 层、256 维（配置 `num_layers=6, d_model=256`）；
   - Joint 与 3D-only 只差 3 个字段（`_3d` 配置 L20-L27）；
   - 数值任务有 4 个（`NUMERIC_TASKS`）。
3. 用 60 秒向同伴（或自言自语录音）讲一遍：**输入是什么、输出是什么、中间四个组件各自干什么、`<SEG>` 为什么妙**。讲不顺的地方，就是下一讲该带着问题读的地方。

预期成果：一张可长期保存的速览卡。当你在第 3~5 单元深入源码时，这张卡就是你的「地图」，每学完一个模块就在对应箭头旁补上真实类名与行号。

## 6. 本讲小结

- Ground3D-LMM 在 ScanNet/ScanNet++ 上做**细粒度 3D 接地 + 空间推理**，同时输出语言回答与逐点掩码，训练数据 2,475,307 对 QA（README 清单枚举 9 个子任务名，正文写 8，代码以任务名为准）。
- 架构四组件：SpConvUNet 稀疏编码 → 超点池化（scatter-mean，\(N\) 点压成 \(M\) 超点）→ Qwen3-VL-4B（LoRA）+ 6 层 Mask2Former 式查询解码器（256 维）。
- **`<SEG>` 机制是核心巧思**：语言模型生成回答时 `<SEG>` 位置的最后一层隐状态被直接复用为分割查询，语言与分割在同一个前向中对齐。
- 超点特征 `Fp` **并行**投影两路：作为 `<|point|>` token 进 LMM，作为 K/V 进掩码解码器。
- 两个发布变体 **Joint（3D+RGB，r=16）** 与 **3D-only（仅点云，r=32）**，配置差异仅 `lora_r`/`lora_alpha`/`image_dir` 三字段；LoRA rank 与 checkpoint 不匹配会被静默忽略，是隐蔽的工程坑。
- 读 README 要学会交叉验证：任务名清单、代码常量、配置文件三方对齐后再下结论。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：按 `docs/INSTALL.md` 的 Option B 搭建 OpenMMLab 栈与 MinkowskiEngine 源码编译环境——README 已警告 `environment.yml` 不能独立完成安装，这是本仓库最大的上手门槛。
- **u1-l3** 将梳理仓库目录与 OpenMMLab 注册机制（`type='Grounded_LMM_main'` 字符串如何变成 `uniseg3d/uniseg3d.py:906` 的类实例）。
- 想先「看它跑起来」的读者可在装好环境后直接跳到 **u1-l4**（用 HuggingFace checkpoint 跑一次 `scale_estimation` 推理），再回头补中间两讲。
- 延伸阅读：README [Acknowledgments](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L402-L409) 列出的 UniSeg3D / Mask2Former / Qwen3-VL 三个上游项目，是理解本仓库三个关键组件设计来源的最短路径。
