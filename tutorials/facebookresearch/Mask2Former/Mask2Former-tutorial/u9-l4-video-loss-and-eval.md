# u9-l4 视频损失与 YTVIS 评估

## 1. 本讲目标

本讲是视频单元（单元九）的收官，把图像版「先匹配、后监督」的训练监督机制（单元五）推广到视频时空管道（tube），并补齐视频侧的评估闭环。读完本讲，你应该能够：

1. 对比 `VideoSetCriterion.loss_masks` 与图像版 `SetCriterion.loss_masks`，说出两处真实差异（GT 打包方式、`flatten(0,1)` 拆帧）以及一处**容易被误传的差异**（逐点采样其实被完整保留了）。
2. 理解 `num_masks` 归一化在视频下的语义：按「管道数」而非「帧数」归一化，一条 tube 的 T 帧损失相加后除以 tube 数。
3. 解释 `VideoHungarianMatcher` 为什么必须**按视频整体做一次 LSAP**，以及 (Q, T, H, W) 的 4D mask 如何被 `point_sample` 直接消费。
4. 读懂 `YTVISEvaluator` 的三段式评估流程，说清时空 IoU 的计算方式（逐帧交/并累加）、`avg_area` 面积分桶口径，以及 AP/AP50/AP75/APs/APm/APl/AR1/AR10 八项指标的含义。
5. 在 `train_net_video.py` 的 `--eval-only` 路径中定位 YTVIS 评估结果的三处日志输出与两处文件落盘位置。

## 2. 前置知识

本讲默认你已读完 u5-l1（匈牙利匹配器）、u5-l2（图像版逐点 mask 损失）与 u9-l2（视频解码器）。下面把几个关键概念串一遍。

**时空管道（tube）**。视频实例分割（VIS）的输出单位不是「一帧里的一个 mask」，而是「贯穿 T 帧的一条实例管道」：形状为 \((T, H, W)\) 的 mask 序列，同一实例在所有帧中共享同一个 query（u9-l2 讲过 query 跨帧共享）。GT 侧的跨帧关联靠 `gt_ids`（u9-l3）。

**「先匹配、后监督」回顾**。mask classification 范式输出固定 Q 个 query，每视频 GT 管道数不定，须先解 LSAP（线性分配问题）做一对一匹配，未匹配 query 视为 no-object。图像版的流程是：匹配（分类 + 采样点 mask/dice 代价）→ 按匹配对监督（交叉熵 + 逐点 BCE/Dice）。视频版把这两步的对象从「2D mask」换成「tube」，框架一行未大改——这正是本讲要展示的「薄层复用」设计。

**逐点采样损失回顾（u5-l2）**。图像版 `loss_masks` 不在全部像素上算 BCE/Dice，而是用 PointRend 式 K + β 重要性采样：对每个预测 mask 取 12544 个归一化坐标点，用 `point_sample`（双线性插值）同时取预测 logit 与 GT 标签，再算逐点损失。归一化坐标的好处：1/4 分辨率预测与全分辨率 GT 可以直接「对采」，无需上采样。

**RLE 编码回顾（u7-l2）**。二值 mask 压缩成行程编码（Run-Length Encoding）的字典 `{"size": [h, w], "counts": bytes}`，pycocotools 的 `mask_util.encode` 生成。YTVIS 格式里一条标注的 `segmentations` 字段是 T 个 RLE 组成的列表，缺失帧为 `null`。

**PR 曲线与 AP 回顾（u7-l2）**。按置信度排序预测，在不同阈值下统计 TP/FP 得到 PR 曲线，101 点插值求平均精度。YTVIS 的 AP 沿用这套流程，唯一改变是「一个预测」的单位从 image mask 变成 tube，IoU 也换成时空版本（见 4.3.2）。

**本讲涉及的关键术语**：

| 术语 | 含义 |
| --- | --- |
| tube / 时空管道 | 一个实例贯穿 T 帧的 mask 序列，形状 (T, H, W) |
| 时空 IoU | 两条 tube 的逐帧交集像素总数除以逐帧并集像素总数 |
| LSAP | 线性分配问题，由 `scipy.optimize.linear_sum_assignment` 求解 |
| `num_masks` | 损失归一化分母；视频下等于批内 GT 管道总数（跨卡平均） |
| `avg_area` | tube 的逐帧平均面积，YTVIS 面积分桶（APs/APm/APl）的口径 |
| YTVOS / YTVOSeval | YTVIS 数据集的评估 api（从 IFC 项目移植的 cocotools 变体） |

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `mask2former_video/modeling/criterion.py` | `VideoSetCriterion`：视频版损失，含 `loss_masks` 的拆帧逐点监督 |
| `mask2former_video/modeling/matcher.py` | `VideoHungarianMatcher`：按视频整体的 LSAP 匹配 |
| `mask2former/modeling/criterion.py` | 图像版 `SetCriterion`，用于逐行对比 |
| `mask2former/modeling/matcher.py` | 图像版 `HungarianMatcher`，用于逐行对比 |
| `mask2former_video/data_video/ytvis_eval.py` | `YTVISEvaluator`：预测转 YTVIS json、调 api 评估、指标表 |
| `mask2former_video/data_video/datasets/ytvis_api/ytvos.py` | YTVOS api：加载 GT/预测，`loadRes` 计算 `avg_area` |
| `mask2former_video/data_video/datasets/ytvis_api/ytvoseval.py` | YTVOSeval：时空 IoU、PR 累积、12 项 stats |
| `mask2former_video/video_maskformer_model.py` | `VideoMaskFormer`：criterion 的构建、`inference_video` 的输出契约 |
| `train_net_video.py` | 视频 Trainer：`build_evaluator`、`test`、`--eval-only` 主分支 |

## 4. 核心概念与源码讲解

### 4.1 VideoSetCriterion.loss_masks：把时空管道拆成帧，逐点监督

#### 4.1.1 概念说明

`VideoSetCriterion` 是图像版 `SetCriterion` 的视频推广。它要解决的问题是：监督对象从「每图 N 个 (H, W) 的 2D mask」变成「每视频 N 条 (T, H, W) 的时空管道」后，逐点采样损失机制如何延续。

先给一个重要结论（也纠正一个常见误传）：**当前 HEAD 的视频版 `loss_masks` 完整保留了 `point_sample` 逐点采样**，并没有退回「全 mask 像素损失」。本讲规格中「找出视频版没有 point_sample 的位置」的预设与源码不符（规格也标注了「待确认：以源码为准」）——以源码为准：`mask2former_video/modeling/criterion.py` 的 L157-177 与图像版一样调用了 `get_uncertain_point_coords_with_randomness` 与 `point_sample`。真实差异有两处：

1. **拆帧**：把 (N, T, H, W) 的 tube 用 `flatten(0, 1)` 拆成 (N·T, 1, H, W) 的独立帧 mask，逐帧采样点、逐帧算损失。
2. **GT 打包**：图像版要用 `nested_tensor_from_tensor_list` 处理批内不同尺寸的 GT；视频版因为 `prepare_targets` 已把所有 GT mask 统一 pad 到网络输入尺寸（u9-l1 讲过的 `mask_shape` 硬对齐），直接 `torch.cat` 即可。

而归一化语义的变化才是「取舍」所在：分母 `num_masks` 数的是**管道数**，不是帧数。一条 tube 的 T 帧 loss 全部加起来，才除以 tube 数。

#### 4.1.2 核心流程

`VideoSetCriterion.forward`（与图像版几乎逐字相同）：

```
1. 剔除 aux_outputs，先用主输出做一次匹配
2. num_masks = 批内所有视频的 GT 管道数之和
   → all_reduce 跨卡求和 → 除以 world_size → clamp(min=1)
3. 对 self.losses 中的每项（labels、masks）计算损失
4. 若有 aux_outputs：逐层重新匹配、重新算损失，键名加 "_i" 后缀
5. 返回未加权的损失 dict（乘权重由 VideoMaskFormer.forward 完成）
```

`loss_masks` 的数据流：

```
pred_masks (B, Q, T, H/4, W/4)          GT masks (M, T, H_pad, W_pad)
        │ 按匹配索引选取                       │ 按匹配索引选取、跨视频 cat
        ▼                                     ▼
  (N, T, H/4, W/4) ── flatten(0,1) ──► (N·T, 1, H/4, W/4)
  (N, T, H_pad, W_pad) ─ flatten(0,1) ─► (N·T, 1, H_pad, W_pad)
        │                                     │
        └──────────────┬──────────────────────┘
                       ▼
   不确定性引导采样 K=12544 个归一化坐标点
   point_sample 分别在两个分辨率上插值取值
                       ▼
              point_logits/point_labels: (N·T, K)
                       ▼
     loss_mask = BCE(point_logits, point_labels).mean(1).sum() / N
     loss_dice = dice(point_logits, point_labels).sum() / N
```

逐点 BCE 的数学形式（N 为批内管道总数，K 为采样点数）：

\[
\mathcal{L}_{\text{mask}} = \frac{1}{N}\sum_{n=1}^{N}\sum_{t=1}^{T}\frac{1}{K}\sum_{k=1}^{K}\mathrm{BCE}\big(p_{n,t}(x_k),\, g_{n,t}(x_k)\big)
\]

注意分母是 \(N\)（管道数）而不是 \(N \cdot T\)（帧数）。这个选择的含义：

- **一条 tube = 一个样本**：不管视频采了 2 帧还是 36 帧，一个实例对总损失的贡献量级相当（帧数越多单帧权重自然被摊薄，但整条 tube 的贡献不变量级）。
- **与匹配语义对齐**：匹配是把一条 tube 分给一个 query（见 4.2），监督时也把这条 tube 当作一个整体计数单位，两者口径一致。
- **分布式正确性**：与图像版相同，`num_masks` 先 all_reduce 再除以 world_size，保证多卡各自算的 loss 加起来等于「全局批」的损失。

#### 4.1.3 源码精读

**损失入口与拆帧**。[criterion.py:140-155](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/criterion.py#L140-L155)：按匹配索引选出被匹配的预测 mask 与 GT mask，然后 `flatten(0, 1)` 把 (N, T, H, W) 拆成 (N·T, H, W)、`[:, None]` 补上通道维变成 (N·T, 1, H, W)——注释明确写着 `# NT x 1 x H x W`。

```python
src_masks = src_masks[src_idx]
# Modified to handle video
target_masks = torch.cat([t['masks'][i] for t, (_, i) in zip(targets, indices)]).to(src_masks)

# No need to upsample predictions as we are using normalized coordinates :)
# NT x 1 x H x W
src_masks = src_masks.flatten(0, 1)[:, None]
target_masks = target_masks.flatten(0, 1)[:, None]
```

`# Modified to handle video` 这一行注释就是视频版相对图像版的全部改动痕迹之一：GT 用 `torch.cat` 直接拼接。对比图像版 [criterion.py:150-159](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L150-L159)：图像版必须用 `nested_tensor_from_tensor_list` 把批内不同尺寸的 GT mask pad 成嵌套张量（还留了一个 `TODO use valid to mask invalid areas`），且只做 `[:, None]` 没有 `flatten(0, 1)`——因为图像版没有时间维。

**逐点采样被完整保留**。[criterion.py:157-177](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/criterion.py#L157-L177)：在 `torch.no_grad()` 下用 `get_uncertain_point_coords_with_randomness`（3K 候选中按不确定度 \(-|\text{logit}|\) 取 top-0.75K、随机补齐共 12544 点，见 u5-l2）对 N·T 个帧 mask 各自独立采样；`point_sample` 用归一化坐标分别在预测（1/4 分辨率）与 GT（全尺寸）上双线性插值，天然对齐、无需上采样。

**损失与归一化**。[criterion.py:179-186](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/criterion.py#L179-L186)：`sigmoid_ce_loss_jit` 内部是 `loss.mean(1).sum() / num_masks`——先对每个帧 mask 的 K 个点取均值，再把 N·T 个帧级损失**求和**，除以管道数 `num_masks`。这就是 4.1.2 公式的代码形态。

**num_masks 数的是管道**。[criterion.py:219-227](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/criterion.py#L219-L227)：`num_masks = sum(len(t["labels"]) for t in targets)`。每个 target dict 的 `labels` 长度是该视频的 GT 管道数（`prepare_targets` 中 `valid_idx` 过滤后保留的实例行数），随后 all_reduce、除以 world_size、clamp(min=1)，与图像版逻辑一致但计数单位换成了管道。

**深监督逐层重匹配**。[criterion.py:234-241](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/criterion.py#L234-L241)：对每组 `aux_outputs` 重新调用 matcher 再算损失，键名加 `_i` 后缀——与图像版（u5-l3）完全同构。YTVIS 配置 `DEC_LAYERS: 10`，因此损失 dict 共 3 × 10 = 30 个键。

**分类损失与 no-object 降权**。[criterion.py:113-115](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/criterion.py#L113-L115) 构造 `empty_weight`（最后一维 no-object 置 `eos_coef=0.1`），[criterion.py:122-138](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/criterion.py#L122-L138) 的 `loss_labels` 用它做加权交叉熵——与图像版逐字相同，不再展开。

**criterion 在哪里被构建与加权**。[video_maskformer_model.py:104-132](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L104-L132)：`from_config` 用 YTVIS 配置的权重（`CLASS_WEIGHT: 2.0`、`MASK_WEIGHT: 5.0`、`DICE_WEIGHT: 5.0`）同时构建 matcher 代价与 `weight_dict`，并把 `weight_dict` 按 `DEC_LAYERS - 1` 展开成带 `_i` 后缀的 30 键（与图像版 u5-l3 同构）；[video_maskformer_model.py:190-203](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L190-L203)：训练分支对 criterion 返回的损失按键加权、丢弃白名单外的键。

**GT masks 从哪来**。[video_maskformer_model.py:227-253](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L227-L253)：`prepare_targets` 为每个视频分配 `[_num_instance, num_frames, h_pad, w_pad]` 的全零 bool 张量（L232），逐帧把 `gt_masks.tensor` 写入左上角（L241），再按 `valid_idx`（`gt_ids != -1` 至少一帧有效，L244）过滤并转 float（L250）。这一步解释了 4.1.1 的第二处差异：**GT 已 pad 到网络输入尺寸且长度统一**，criterion 里才能直接 `torch.cat`。

#### 4.1.4 代码实践

**实践目标**：用随机输入跑一次 `VideoSetCriterion` 前向，确认损失键的构成（主输出 3 键 + 每层 aux 3 键），并手工验证 `num_masks` 的管道计数语义。

**操作步骤**（示例代码，保存为仓库外任意脚本，如 `/tmp/run_video_criterion.py`）：

```python
# 示例代码：随机输入跑 VideoSetCriterion
import torch
from mask2former_video.modeling.criterion import VideoSetCriterion
from mask2former_video.modeling.matcher import VideoHungarianMatcher

B, Q, K, T, H, W, M = 2, 10, 5, 2, 64, 64, 3  # 2 视频、10 query、5 类、2 帧、3 条管道

# 权重取自 configs/youtubevis_2019/video_maskformer2_R50_bs16_8ep.yaml
matcher = VideoHungarianMatcher(cost_class=2.0, cost_mask=5.0, cost_dice=5.0,
                                num_points=12544)
weight_dict = {"loss_ce": 2.0, "loss_mask": 5.0, "loss_dice": 5.0}
for i in range(10 - 1):  # DEC_LAYERS=10，深监督展开 9 层
    weight_dict.update({k + f"_{i}": v for k, v in weight_dict.items()})

criterion = VideoSetCriterion(K, matcher=matcher, weight_dict=weight_dict,
                              eos_coef=0.1, losses=["labels", "masks"],
                              num_points=12544, oversample_ratio=3.0,
                              importance_sample_ratio=0.75)

outputs = {
    "pred_logits": torch.randn(B, Q, K + 1),                 # (B, Q, K+1) 含 no-object
    "pred_masks": torch.randn(B, Q, T, H // 4, W // 4),      # 1/4 分辨率
    "aux_outputs": [
        {"pred_logits": torch.randn(B, Q, K + 1),
         "pred_masks": torch.randn(B, Q, T, H // 4, W // 4)}
        for _ in range(9)
    ],
}
# GT：管道数 M、已 pad 到全尺寸的 (M, T, H, W) —— 模拟 prepare_targets 的输出
targets = [{"labels": torch.randint(0, K, (M,)),
            "masks": (torch.rand(M, T, H, W) > 0.7).float()} for _ in range(B)]

losses = criterion(outputs, targets)
print(len(losses), sorted(losses.keys()))
print({k: round(v.item(), 4) for k, v in losses.items() if "_" not in k})
```

**需要观察的现象**：

1. 损失 dict 共 **30 个键**：`loss_ce` / `loss_mask` / `loss_dice` 及其后缀 `_0` … `_8`。
2. 单卡运行时 `num_masks = 2 × 3 = 6`（管道数），与帧数 T=2 无关——可在 criterion 里加一行 `print(num_masks)` 验证。
3. 断点检查（可选）：在 `loss_masks` 内打印 `src_masks.shape`，进入函数时是 `(N, T, H/4, W/4)`，`flatten(0, 1)` 后变成 `(N·T, 1, H/4, W/4)`，其中 N = 6（两视频管道之和）、N·T = 12。

**预期结果**：主输出损失为三个有限标量（随机权重下数值量级不定属正常），梯度可回传（`losses["loss_mask"].backward()` 不报错）。具体数值**待本地验证**（需要已编译 MSDeformAttn 的环境，且本脚本依赖 `import mask2former_video` 的注册副作用之外还依赖 detectron2 的 point_rend 项目）。

#### 4.1.5 小练习与答案

**练习 1**：如果把视频版 `loss_masks` 的 `flatten(0, 1)` 删掉（即直接监督 (N, T, H, W) 的 4D 张量），会发生什么？

**答案**：`get_uncertain_point_coords_with_randomness` 与 `sigmoid_ce_loss_jit` 的归约维度都是按「batch 维 + 空间维」设计的（`loss.mean(1)` 只均值空间维），4D 输入会使 `mean(1)` 均值到 T 维、逐帧独立采样也无法进行，形状与语义都会错乱。`flatten(0, 1)` 的本质是把「时间维并入 batch 维」，让一整套图像版逐点损失代码零改动复用。

**练习 2**：`num_masks` 为什么不按 `N·T`（帧数）归一化？

**答案**：因为匹配与监督的单位是 tube 而不是帧——一条 tube 只被分配给一个 query，监督时也应以 tube 为计数单位。若按 N·T 归一化，帧数多的样本（或采样帧数变化时）单条 tube 的梯度贡献会被稀释，训练目标随 T 漂移；按 N 归一化则「一条 tube 恒等于一个样本」，与 LSAP 的分配语义严格对齐。

**练习 3**：视频版 criterion 里为什么可以直接 `torch.cat` 各视频的 GT mask，而图像版要用 `nested_tensor_from_tensor_list`？

**答案**：视频版 `prepare_targets` 在模型内部已把每个视频的 GT mask 写入形状 `[N, num_frames, h_pad, w_pad]` 的统一张量（pad 到网络输入尺寸），批内天然对齐；图像版的 GT mask 直接来自 dataset dict，批内各图原始尺寸不同，必须在损失内部现做 padding（并遗留了 padding 区域参与损失的 TODO）。

### 4.2 VideoHungarianMatcher：一次 LSAP 匹配整条时空管道

#### 4.2.1 概念说明

`VideoHungarianMatcher` 与图像版 `HungarianMatcher` 逐行对比后会发现：**代价函数、随机采样、fp32 防抖、LSAP 求解全部相同**，唯一的实质差异是 mask 代价的计算维度——图像版把 mask 升成 (Q, 1, H, W) 后采样得到 (Q, K)；视频版把 4D 的 (Q, T, H, W) **直接**交给 `point_sample`，T 被当作「通道维」，采样输出 flatten 后是 (Q, K·T)。

为什么必须**按视频整体**做一次匹配，而不是逐帧独立匹配？因为 query 跨帧共享（u9-l2）：一个 query 对一条 tube 的全部 T 帧负责。若逐帧匹配，同一 GT 实例可能在第 1 帧分给 query A、第 2 帧分给 query B，监督信号自相矛盾，模型永远学不出稳定的「一个 query 一条管道」。把整条 tube 的 T 帧代价压缩进代价矩阵的一个元素，LSAP 一旦把 query A 与 GT 管道 g 绑定，A 就要在**所有帧**上同时逼近 g——跨帧身份一致性由此从监督机制层面得到保障。

#### 4.2.2 核心流程

对 batch 中每个视频 b：

```
1. 分类代价：out_prob = pred_logits[b].softmax(-1)          → (Q, K+1)
   cost_class = -out_prob[:, tgt_ids]                        → (Q, M)
2. mask 代价：
   out_mask = pred_masks[b]            # (Q, T, H/4, W/4)  不加通道维！
   tgt_mask = targets[b]["masks"]      # (M, T, H_pad, W_pad) 已 pad
   point_coords = torch.rand(1, K, 2)  # 一组共享的随机归一化坐标
   tgt = point_sample(tgt_mask, coords.repeat(M,1,1)).flatten(1)  # (M, K·T)
   out = point_sample(out_mask, coords.repeat(Q,1,1)).flatten(1)  # (Q, K·T)
   关 autocast 转 fp32：
   cost_mask = batch_sigmoid_ce_loss(out, tgt)   # (Q, M)
   cost_dice = batch_dice_loss(out, tgt)         # (Q, M)
3. C = 2.0·cost_class + 5.0·cost_mask + 5.0·cost_dice   # (Q, M)
4. linear_sum_assignment(C.cpu()) → 一对一匹配对
```

`batch_sigmoid_ce_loss` 中的归一化因子 `hw = inputs.shape[1]` 在视频下变成 \(K \cdot T\)——即每个代价元素是「整条 tube 的所有帧 × 所有点」的平均 BCE，一条 query 与一条 GT 的相似度由全部 T 帧联合决定：

\[
C_{\text{mask}}(q, m) = \frac{1}{K T}\sum_{t=1}^{T}\sum_{k=1}^{K}\mathrm{BCE}\big(p_q(x_k, t),\, g_m(x_k, t)\big)
\]

两个值得注意的细节：

- **matcher 用纯随机采样，loss 用不确定性引导采样**。matcher 的 `torch.rand`（L120）与图像版一致；`get_uncertain_point_coords_with_randomness` 只出现在 criterion 侧。匹配发生在梯度更新之前，随机采样足够稳定且更便宜。
- **分辨率不一致无碍**。预测是 1/4 分辨率、GT 是网络输入全尺寸，`point_sample` 的坐标是归一化的 \([0,1]\)，各自乘以各自的空间尺寸后插值，天然对齐——这就是源码注释 `# No need to upsample predictions as we are using normalized coordinates :)` 的含义。

#### 4.2.3 源码精读

**分类代价**。[matcher.py:105-111](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/matcher.py#L105-L111)：softmax 后取负的目标类概率近似 NLL，与图像版逐字相同。

**4D mask 直接进 point_sample**。[matcher.py:113-120](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/matcher.py#L113-L120)：`out_mask` 形状为 `[num_queries, T, H_pred, W_pred]`，GT `.to(out_mask)` 只转 dtype/device；图像版的 `out_mask[:, None]` 两行被**注释保留**（L117-118），采样点是 `torch.rand(1, num_points, 2)`——所有 query 与 GT 共享同一组坐标，保证代价在相同像素位置间比较。

**采样与展平**。[matcher.py:122-132](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/matcher.py#L122-L132)：`point_sample` 按 (N, P, 2) 坐标对 (N, C, H, W) 输入采样，输出 (N, P, C)。这里输入的「通道维 C」就是 T，输出 (Q, K, T) 经 `flatten(1)` 变 (Q, K·T)。对比图像版 [matcher.py:117-126](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/matcher.py#L117-L126)：图像版先 `out_mask[:, None]` 升成 (Q, 1, H, W)，输出 (Q, K) 后 `squeeze(1)`，最终是 (Q, K) 的二维代价输入。

**fp32 防抖与 LSAP**。[matcher.py:134-151](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/matcher.py#L134-L151)：`autocast(enabled=False)` 内转 float32 计算 BCE/Dice 代价（半精度下逐点插值误差会造成匹配抖动，u5-l1 讲过同一动机）；三项代价按 2.0/5.0/5.0 加权合成 (Q, M) 矩阵，`linear_sum_assignment` 求解一对一分配。整个函数被 `@torch.no_grad()` 包裹（L95），不建计算图。

**谁消费匹配结果**：`VideoSetCriterion.forward` 对主输出与每组 aux 各调一次（criterion.py L218、L237），每次都是「整条 tube」级别的重新匹配。

#### 4.2.4 代码实践

**实践目标**：用玩具数据验证「按视频整体匹配」与「逐帧独立匹配」会给出不同的分配结果，直观理解时空联合代价。

**操作步骤**（示例代码）：

```python
# 示例代码：tube 匹配 vs 逐帧匹配
import torch
from mask2former_video.modeling.matcher import VideoHungarianMatcher

Q, M, T, H, W = 2, 1, 2, 32, 32
# 构造「query0 在第 0 帧像 GT、query1 在第 1 帧像 GT」的对抗场景：
# GT 管道 = 第 0 帧左边方块 + 第 1 帧右边方块
gt = torch.zeros(M, T, H, W)
gt[0, 0, :, : W // 2] = 1.0   # 第 0 帧：左半
gt[0, 1, :, W // 2:] = 1.0    # 第 1 帧：右半

out = torch.full((Q, T, H, W), -8.0)   # logit -8 ≈ sigmoid 0.0003（背景）
out[0, 0, :, : W // 2] = 8.0           # query0 只在第 0 帧复现 GT
out[1, 1, :, W // 2:] = 8.0            # query1 只在第 1 帧复现 GT

matcher = VideoHungarianMatcher(cost_class=0.0, cost_mask=5.0, cost_dice=5.0,
                                num_points=400)
outputs = {"pred_logits": torch.zeros(1, Q, 2), "pred_masks": out[None]}
targets = [{"labels": torch.tensor([0]), "masks": gt}]
print(matcher(outputs, targets))   # 期望 query0 与 query1 中「整体代价更低」者胜出
```

**需要观察的现象**：匹配输出是单元素列表 `[(tensor([i]), tensor([0]))]`，即整条 GT 管道只分配给**一个** query；两个「各对一半帧」的 query 谁都不会得到低成本——时空联合代价天然惩罚这种「帧间分裂」的预测。

**预期结果**：任一 query 的 tube 都只覆盖 GT 的一半帧，mask 代价都不低，LSAP 仍会选相对更优者；关键观察点是**不存在「第 0 帧给 query0、第 1 帧给 query1」的分裂分配**——这正是 tube 匹配与逐帧匹配的本质区别。具体选中哪个 query 取决于随机采样点与 Dice/BCE 的相对量级，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：视频 matcher 中 `flatten(1)` 之后张量形状是什么？`batch_sigmoid_ce_loss` 里的 `hw` 等于多少？

**答案**：`point_sample` 输出 (Q, K, T)（T 充当通道维），`flatten(1)` 后为 (Q, K·T)；`hw = inputs.shape[1] = K·T`，即每个代价元素是全部帧 × 全部采样点的平均损失。

**练习 2**：为什么 GT mask 与预测 mask 分辨率不同（全尺寸 vs 1/4）却可以直接一起采样？

**答案**：`point_sample` 接收归一化坐标 \([0,1]^2\)，内部按各自输入的 (H, W) 换算像素位置再做双线性插值，两个分辨率在「同一相对位置」取值，语义对齐，省去上采样的显存与计算开销。

**练习 3**：matcher 的采样点生成方式与 criterion 的有何不同？为什么可以不同？

**答案**：matcher 用 `torch.rand` 纯随机均匀采样；criterion 用 `get_uncertain_point_coords_with_randomness` 做 K+β 重要性采样（在不确定度高的边界区过采样）。匹配只影响分配（离散决策），随机采样已足够稳定且便宜；损失要引导梯度，在难分区域加密采样能提升收敛质量与边界质量。

### 4.3 YTVISEvaluator：从 pred_masks 到 YTVIS AP

#### 4.3.1 概念说明

`YTVISEvaluator` 是从 IFC 项目移植的 `DatasetEvaluator`，底层依赖仓库自带的 `YTVOS` / `YTVOSeval` api（cocotools 的视频版变体）。它解决的问题是：模型输出的是「每视频 top-10 个 (score, label, T 帧 mask)」，评估器要把它转成 YTVIS json、与 GT 做**tube 级**匹配，输出 AP 系列指标。

评估与训练监督是两个独立环节，口径有一处重要差别值得点明：训练的匈牙利匹配在「采样点」上比较 mask（省显存），评估的 IoU 在**全部帧的全部像素**上比较（要权威）。

YTVIS 格式的预测条目：

```json
{
  "video_id": 1,
  "score": 0.92,
  "category_id": 3,
  "segmentations": [RLE_t0, RLE_t1, null, RLE_t3, ...]   // 每帧一个 RLE，缺失帧为 null
}
```

GT 标注同构（多了 `areas`、`iscrowd` 等字段），一条 GT 标注就是一条贯穿全视频的实例轨道（u9-l3）。

#### 4.3.2 核心流程

评估器生命周期（`reset → process × N → evaluate`）与三段式计算：

```
process（每视频一次）
  inputs（单视频 dict，含 video_id/length）+ outputs（inference_video 的 dict）
    → instances_to_coco_json_video
    → [{video_id, score, category_id, segmentations: [T 个 RLE]}, ...]

evaluate（数据集跑完一次）
  gather 到主进程 → 落盘 instances_predictions.pth
  → _eval_predictions
      ① category_id 反映射：模型连续 id → 数据集原始 id
      ② 落盘 results.json
      ③ _evaluate_predictions_on_coco：
           删除 bbox 字段（面积按 mask 算）
           YTVOS api loadRes（计算 avg_area）
           YTVOSeval(gt, dt)：maxDets=[1,10,100]
           evaluate() → accumulate() → summarize()
      ④ _derive_coco_results：从 stats 取 8 项指标 ×100 + per-category AP
```

**时空 IoU** 是整个评估的基石，YTVOSeval 对每对（预测 tube, GT tube）计算：

\[
\text{IoU}_{\text{tube}}(p, g) = \frac{\displaystyle\sum_{t=1}^{T}|p_t \cap g_t|}{\displaystyle\sum_{t=1}^{T}|p_t \cup g_t|}
\]

逐帧累加交集与并集的像素数再相除。三种帧状态的处理：

| 该帧预测 | 该帧 GT | 交集 | 并集 |
| --- | --- | --- | --- |
| 有 | 有 | \(|p_t \cap g_t|\) | \(|p_t \cup g_t|\) |
| 无（null） | 有 | 0 | \(|g_t|\)（漏检帧全额惩罚） |
| 有 | 无（GT 缺失帧） | 0 | \(|p_t|\)（虚检惩罚） |

这个定义意味着：预测只要**漏掉一帧**，该帧整个 GT 面积就进入分母——tube IoU 断崖式下跌。这是 VIS 远难于图像实例分割、AP 数值普遍偏低的算法根源，也是「跨帧身份一致性」被直接量化的方式：一个 query 若中途「换目标」，整条 tube 同时变成一条 FP（对真目标）和一条 FN。

**面积分桶口径**：APs/APm/APl 按 `avg_area`（tube 的**逐帧平均面积**，而非全帧总面积）划分，阈值 128²/256²。长视频里的小物体不会因为帧数多、总面积大而被错划为 large。

#### 4.3.3 源码精读

**process 的输入契约与图像版完全不同**。[ytvis_eval.py:103-113](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/ytvis_eval.py#L103-L113)：断言 `len(inputs) == 1`（视频测试加载器恒 batch=1，u9-l3），`outputs` 是 dict 而非图像版的 `Instances` 列表。dict 的三个键正是 `inference_video` 的输出（[video_maskformer_model.py:280-285](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L280-L285)）：`pred_scores` / `pred_labels` / `pred_masks`（每个元素是 (T, H, W) 的 bool 张量）。

**逐帧 RLE 编码**。[ytvis_eval.py:277-291](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/ytvis_eval.py#L277-L291)：对每个实例的 T 帧 mask 逐帧 `mask_util.encode(np.array(_mask[:, :, None], order="F", dtype="uint8"))`（Fortran 序即列优先，pycocotools 的要求），再把 `counts` 从 bytes 解码成 utf-8 字符串以便 json 序列化；条目含 `video_id`、`score`、`category_id`、`segmentations`。

**category id 反映射**。[ytvis_eval.py:152-166](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/ytvis_eval.py#L152-L166)：模型输出连续 id（0..K-1），写回 json 前要反映射成数据集原始 id。这里的密集断言 `min==0 and max==num_classes-1` 能通过（YTVIS 40 类 1..40 → 0..39 的映射是密集的），不像图像版 ADE20K 全景的稀疏映射会触发 u7-l2 讲过的断言问题。

**删 bbox 与调 api**。[ytvis_eval.py:296-327](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/ytvis_eval.py#L296-L327)：`c.pop("bbox", None)`（若结果带 bbox，cocotools 会用框面积做大小分桶，删掉以强制按 mask 面积）；`loadRes` 载入预测、`YTVOSeval(coco_gt, coco_dt)` 建评估器、`maxDets = [1, 10, 100]`，然后三段式 `evaluate/accumulate/summarize`。注意变量名 `max_dets_per_image` 中的 "image" 是从 COCO 复制来的历史叫法，实际单位是**视频**。

**时空 IoU 实现**。[ytvoseval.py:203-217](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis_api/ytvoseval.py#L203-L217)：`iou_seq` 内联定义在 `computeIoU` 中，逐帧 `zip` 预测与 GT 的 RLE：双方都有则 `merge([d,g], True)`（交集）与 `merge([d,g], False)`（并集）面积累加；单方有则该方面积计入并集；最后 `i/u`。外层 [ytvoseval.py:218-220](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis_api/ytvoseval.py#L218-L220) 双重循环填出 (len(dt), len(gt)) 的 IoU 矩阵。真实 cocotools 的 `maskUtils.iou` 被注释掉（L202），换成了这个时空版本——这就是「cocotools 视频变体」的全部核心。

**avg_area 的来源**。[ytvos.py:233-254](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis_api/ytvos.py#L233-L254)：`loadRes` 对每条预测逐帧算 `maskUtils.area(seg)`（缺失帧记 None），再对非空面积取均值得 `ann['avg_area']`（L250-254）；GT 侧分桶判断同样使用 `avg_area`（[ytvoseval.py:282-286](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis_api/ytvoseval.py#L282-L286)：`g['avg_area']<aRng[0] or g['avg_area']>aRng[1]` 则该 GT 记 ignore），面积档位定义在 [ytvoseval.py:542-544](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis_api/ytvoseval.py#L542-L544)：small < 128²、medium 128²–256²、large > 256²。

**指标表只取 stats 前 8 项**。[ytvoseval.py:490-504](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis_api/ytvoseval.py#L490-L504)：`summarize` 产出 12 项 stats（AP 系列 6 项 + AR 系列 6 项，maxDets 分别取 [1,10,100] 与面积分档）；而评估器 [ytvis_eval.py:205-215](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/ytvis_eval.py#L205-L215) 只取前 8 项并乘 100：

| 指标 | stats 下标 | 含义 |
| --- | --- | --- |
| AP | 0 | tube IoU 0.50:0.95（步长 0.05）平均精度，全面积、maxDets=100 |
| AP50 / AP75 | 1 / 2 | IoU 阈值 0.5 / 0.75 的 AP |
| APs / APm / APl | 3 / 4 / 5 | 按 avg_area 分桶的小 / 中 / 大实例 AP |
| AR1 / AR10 | 6 / 7 | 每视频最多保留 1 / 10 条预测的平均召回 |

注意 `inference_video` 硬编码 `topk(10)`（[video_maskformer_model.py:259-263](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L259-L263)），每视频最多 10 条预测，因此 AR100（stats[8]）与 AR10 相同，评估器干脆不展示它。per-category AP 从 `eval["precision"]` 张量（iou × recall × cls × area × maxdets）逐类均值（[ytvis_eval.py:224-252](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/ytvis_eval.py#L224-L252)），结果挂到 `self._results["segm"]`。

**--eval-only 路径中结果出现在哪**。入口 [train_net_video.py:260-273](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L260-L273)：`Trainer.build_model` → `DetectionCheckpointer.resume_or_load(cfg.MODEL.WEIGHTS)` → `Trainer.test(cfg, model)`；`TEST.AUG.ENABLED` 为真时直接 `raise NotImplementedError`（视频无 TTA）。`Trainer.test`（[train_net_video.py:184-238](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L184-L238)）覆写父类：对每个 `DATASETS.TEST` 数据集建测试加载器与评估器（[train_net_video.py:62-74](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L62-L74)，默认输出目录 `OUTPUT_DIR/inference`，构造 `YTVISEvaluator(dataset_name, cfg, True, output_folder)`——第二个参数传 cfg 会触发评估器内的 deprecated 用法警告并把 tasks 置 None），在 `autocast()` 下 `inference_on_dataset`。结果共五处可见：

1. **终端标准表**：`YTVOSeval.summarize()` 打印的 12 行 `Average Precision/Recall @ [ IoU=... | area=... | maxDets=... ]`。
2. **日志小表**：[ytvis_eval.py:216-218](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/ytvis_eval.py#L216-L218) 的 `Evaluation results for segm:` 8 项指标表。
3. **日志分类表**：[ytvis_eval.py:250](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/ytvis_eval.py#L250) 的 per-category AP 表。
4. **csv 日志**：[train_net_video.py:233-234](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L233-L234) 的 `Evaluation results for {} in csv format:` + `print_csv_format`。
5. **文件落盘**：`OUTPUT_DIR/inference/results.json`（YTVIS 格式预测，[ytvis_eval.py:168-172](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/ytvis_eval.py#L168-L172)）与 `OUTPUT_DIR/inference/instances_predictions.pth`（原始预测，[ytvis_eval.py:134-138](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/ytvis_eval.py#L134-L138)）。

#### 4.3.4 代码实践

**实践目标**：不跑完整模型，直接用玩具 GT/预测驱动 `YTVOS` + `YTVOSeval`，观察时空 IoU 与各项指标的行为，从而理解评估口径。

**操作步骤**（示例代码，需要 `pycocotools`；`mask2former_video` 包内的 ytvis_api 无需编译扩展即可 import）：

```python
# 示例代码：玩具数据直接跑 YTVOSeval
import json, numpy as np, contextlib, io
import pycocotools.mask as mask_util
from mask2former_video.data_video.datasets.ytvis_api.ytvos import YTVOS
from mask2former_video.data_video.datasets.ytvis_api.ytvoseval import YTVOSeval

H = W = 64
def rle(fill):  # 整帧全 1 或全 0 的 RLE
    return mask_util.encode(np.asfortranarray(np.full((H, W), fill, dtype=np.uint8)))

gt_json = {
    "videos": [{"id": 1, "length": 3, "height": H, "width": W, "file_names": []}],
    "annotations": [
        {"id": 1, "video_id": 1, "category_id": 1, "iscrowd": 0,
         "segmentations": [rle(1), rle(1), rle(1)],
         "areas": [H*W, H*W, H*W]},
    ],
    "categories": [{"id": 1, "name": "toy"}],
}
# 预测 1：完美复现 GT → IoU=1；预测 2：只覆盖前 2 帧 → 交集 2HW、并集 3HW，IoU=2/3
dt = [
    {"video_id": 1, "score": 0.9, "category_id": 1,
     "segmentations": [rle(1), rle(1), rle(1)]},
    {"video_id": 1, "score": 0.8, "category_id": 1,
     "segmentations": [rle(1), rle(1), None]},
]
with contextlib.redirect_stdout(io.StringIO()):
    ytvis = YTVOS()
    ytvis.dataset = gt_json
    ytvis.createIndex()
    dt_loaded = ytvis.loadRes(dt)

evaluator = YTVOSeval(ytvis, dt_loaded)
evaluator.params.maxDets = [1, 10, 100]
evaluator.evaluate(); evaluator.accumulate(); evaluator.summarize()
print("IoU 矩阵:", {k: v.round(3).tolist() for k, v in evaluator.ious.items()})
```

**需要观察的现象**：

1. `evaluator.ious` 中预测 1 与 GT 的 IoU 为 1.0，预测 2 为 2/3 ≈ 0.667（漏掉的第 3 帧把整帧 GT 面积计入并集）——逐项对照 4.3.2 的公式。
2. `summarize` 打印 12 行标准表；AP 系列在玩具数据上只有 IoU≤0.667 的档位可命中。

**预期结果**：IoU 矩阵如上；具体 AP 数值取决于玩具结构（单 GT 下 AP 是阶跃值），**待本地验证**（`YTVOS()` 空 json 的初始化行为、`areas` 字段是否必填等细节需实际运行确认）。

**定位 --eval-only 输出（阅读型实践）**：对照 [train_net_video.py:263-273](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L263-L273) 写出调用链 `main → Trainer.test → build_test_loader + build_evaluator → inference_on_dataset(autocast) → YTVISEvaluator.evaluate`，并在 `MODEL_ZOO.md` 中任选一个 YTVIS 权重，列出你会去检查的五个结果位置（4.3.3 末尾的五处）。若暂无 YTVIS 数据集，此步为纯源码阅读，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：一条预测 tube 漏掉了 3 帧中的 1 帧（该帧 GT 面积为 A，其余帧完美重叠、每帧面积同为 A），它的 tube IoU 是多少？

**答案**：交集 \(= 2A\)（重叠的两帧），并集 \(= 2A + A = 3A\)（漏检帧 GT 全额计入并集），IoU \(= 2/3 \approx 0.667\)。即使其余帧完美，也拿不到 0.75 以上的档位——时空 IoU 对漏帧极其敏感。

**练习 2**：`inference_video` 硬编码 `topk(10)`，这对评估指标有什么影响？

**答案**：每视频最多 10 条预测进入评估，`maxDets=100` 的 AR100 与 AR10 必然相同（stats[8]==stats[7]），评估器因此只展示到 AR10。同时单一视频实例数通常少于 10，topk 一般不构成瓶颈；但若在实例密集的长视频上评估，可考虑调大该值（需改源码，无配置开关）。

**练习 3**：为什么 `_evaluate_predictions_on_coco` 要 `c.pop("bbox", None)`？

**答案**：cocotools 体系里若结果带 bbox，面积分桶会改用框面积而非 mask 面积，导致 APs/APm/APl 的口径漂移；Mask2Former 的视频预测本来就没有框（mask classification 范式，u7-l2 讲过占位框问题），删掉 bbox 字段强制 api 按 mask 面积（`avg_area`）分桶，保证与 GT 侧同口径。

## 5. 综合实践

**任务：把「损失侧」与「评估侧」串成一条最小闭环**。目标是在不下载 YTVIS 数据集的前提下，用一个玩具视频走完「criterion 算损失 → 模拟推理输出 → 评估算 AP」的全链路，验证你对两侧口径差异的理解。

**步骤**：

1. 复用 4.1.4 的脚本构造 B=1、M=2、T=3 的玩具 targets 与 outputs，跑 `VideoSetCriterion`，记录 30 个损失键与 `num_masks`（应为 2）。
2. 从 `outputs["pred_masks"]` 取第 0 个视频，模拟 `inference_video` 的后处理：`(pred_masks[:2] > 0)` 得到 2 条 (T, H, W) 的二值 tube（示例代码，模拟 topk 与阈值化两步即可，不必跑完整模型）。
3. 把这两条 tube 与 targets 的 GT mask（`> 0.5` 二值化）按 4.3.4 的方式分别写成 YTVIS 的 dt 与 gt json（GT 补上 `areas` 字段），跑 `YTVOSeval` 打印 IoU 矩阵与指标表。
4. **对照观察**：训练侧匹配只看 12544 个采样点上的代价，评估侧 IoU 看全部帧全部像素——同一个 (预测, GT) 对，两边的「相似度」数值并不同，但都把「整条 tube」当作原子单位。把两边的数值并排列出，写一段 3-5 句的观察笔记。
5. 最后在 `train_net_video.py` 中用 grep/阅读确认 `--eval-only` 路径（L263-273），把 4.3.3 末尾五处输出位置抄录成自己的 checklist。

**预期结果**：损失 dict 30 键、`num_masks=2`；评估侧能打印出 IoU 矩阵与（阶跃式的）AP 值。整个流程 CPU 即可运行，但依赖已安装的 detectron2（criterion 侧），全部**待本地验证**；若环境不可用，第 3-4 步可单独运行（只依赖 pycocotools）。

## 6. 本讲小结

- `VideoSetCriterion.loss_masks` **完整保留**了图像版的逐点采样损失（不确定性引导 + `point_sample`），真实差异只有两处：GT 因 `prepare_targets` 已 pad 而直接 `torch.cat`，以及 `flatten(0, 1)` 把 (N, T, H, W) 的 tube 拆成 (N·T, 1, H, W) 的帧 mask 逐帧监督。
- 归一化分母 `num_masks` 数的是**管道数**而非帧数：一条 tube 的 T 帧 loss 求和后除以 tube 数，「一条 tube = 一个样本」，与匹配语义严格对齐，且跨卡 all_reduce 平均保证分布式正确。
- `VideoHungarianMatcher` 与图像版唯一的实质差异是 mask 代价的维度：4D 的 (Q, T, H, W) 直接交给 `point_sample`（T 充当通道维），`flatten(1)` 后每个代价元素是 K·T 个采样点的联合损失——一次 LSAP 把整条 tube 绑定给一个 query，跨帧身份一致性从监督机制上得到保障。
- `YTVISEvaluator` 基于 IFC 移植的 `YTVOS`/`YTVOSeval` api：预测逐帧 RLE 编码成 `segmentations` 列表，时空 IoU 按「逐帧交集像素和 ÷ 逐帧并集像素和」计算，漏帧全额惩罚；指标取 stats 前 8 项（AP/AP50/AP75/APs/APm/APl/AR1/AR10），面积分桶按 `avg_area`（逐帧平均面积）。
- `--eval-only` 路径：`main → Trainer.test → inference_on_dataset(autocast) → YTVISEvaluator.evaluate`，结果见于两份文件（`inference/results.json`、`inference/instances_predictions.pth`）与三处日志（12 行标准表、segm 小表 + 分类表、csv 打印）；视频无 TTA（`NotImplementedError`）。
- 训练与评估对「mask 相似度」的口径不同：匹配在采样点上比（省显存），评估在全部像素上比（要权威），但两者都以 tube 为原子单位。

## 7. 下一步学习建议

至此单元九（视频实例分割）完结，Mask2Former 图像与视频两条主线、训练监督与评估闭环都已覆盖。建议：

1. **横向对比 MaskFormer/IFC 的视频实现**：本讲的 criterion/matcher 与 IFC（`sukjunhwang/IFC`）一脉相承，`ytvis_eval.py` 头部注明移植来源；对比 IFC 的损失实现可以看出 Mask2Former 视频版「图像代码薄层复用」的演化路径。
2. **回到单元十的工程主题**：u10-l2 的 `retry_if_cuda_oom` 在视频推理（整段视频一次前向）中尤其关键，可结合本讲 `inference_video` 的上采样位置（L210-215）理解显存热点。
3. **数据侧再挖掘**：重读 u9-l3 的 `load_ytvis_json` 与本讲的 `loadRes`，画一张「YTVIS json → dataset dict → Instances → criterion targets → YTVIS 结果 json」的完整数据流图，作为视频单元的总复习。
4. **若要二次开发**：尝试把 4.1.4 的玩具脚本扩展成正式的 overfit 测试（单视频过拟合验证视频训练闭环），这是接入自定义视频数据集前的标准自检手段。
