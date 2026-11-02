# u9-l1 VideoMaskFormer 与视频训练入口

## 1. 本讲目标

从本讲开始，我们离开图像版包 `mask2former`，进入视频版包 `mask2former_video`。学完本讲，你应该能够：

1. 说出 `add_maskformer2_video_config` 注册的全部视频专属配置键，以及 `INPUT.SAMPLING_FRAME_NUM` 在代码里的三个消费点。
2. 解释 `VideoMaskFormer.forward` 如何把「B 个视频 × T 帧」折叠成 `BT` 的图像批次，从而零改动复用图像版的 backbone 与像素解码器；并知道帧间交互被推迟到哪一层才发生。
3. 对照 `train_net_video.py` 与 `train_net.py`，列出视频版 Trainer 新增与覆写的 `build_*` 方法，并解释为什么视频版**必须**覆写 `build_test_loader` 而图像版不必。
4. 会用 YTVIS 配置构建 `VideoMaskFormer` 并打印模块树，用假视频数据分别跑通训练/推理两种模式的前向。

本讲依赖 u3-l1（MaskFormer 顶层 Meta Architecture）与 u2-l1（训练入口 train_net.py 主流程）的结论，并大量复用 u1-l3 的「import 即注册」与「视频包是薄层」两个判断。

## 2. 前置知识

**视频实例分割（VIS, Video Instance Segmentation）**：给定一段视频，模型要同时完成「检测、分割、分类、跨帧关联」——不仅要说出每一帧里有哪些物体、掩码在哪，还要把第 1 帧的"这只狗"和第 36 帧的"这只狗"标成同一个实例（同一个 id）。本仓库用的数据集是 YouTube-VIS 2019/2021（40 个类别）。

**clip（视频片段）**：模型一次前向处理的最小视频单位。本项目中一个 clip = 从视频里采出的 `SAMPLING_FRAME_NUM` 帧组成的帧序列（默认 2 帧）。

**(B, T) 折叠成 BT**：这是本讲最核心的一个直觉。视频输入天然是四维批次概念「B 个视频 × 每个视频 T 帧」。如果让 backbone 和像素解码器继续把它们当图像看，最简单的办法就是**把帧全部摊平**：B×T 帧就是一个大小为 BT 的普通图像 batch。这样 backbone、FPN、MSDeformAttn 像素解码器**一行代码都不用改**。帧与帧之间的时序交互被推迟到 Transformer 解码器里：那里把「帧维 × 空间维」一起展平进注意力序列，query 一次就能看遍所有帧的所有位置。

**gt_ids（跨帧关联标签）**：训练数据里每个实例带一个贯穿全视频的整数 id。某帧看不到该实例时，mapper 会填一个 `id=-1` 的占位标注（dummy anno）。损失与匹配正是靠这组 id 把「同一个物体在不同帧的 mask」绑成一个时空监督单位。

**薄层复用**（承接 u1-l3）：`mask2former_video` 不 import 图像包 `mask2former` 的模型代码，两者靠入口脚本 `train_net_video.py` **同时 import 两个包**来拼装——视频包只重写顶层模型、Transformer 解码器、匹配器/损失和数据管线这四块，backbone、像素解码器、head 组装器全部白嫖图像包。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mask2former_video/config.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/config.py) | 注册 4 个视频专属配置键，全文件仅 13 行 |
| [mask2former_video/video_maskformer_model.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py) | 视频版 meta arch：帧折叠、prepare_targets、inference_video |
| [mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py) | 视频解码器（本讲只看它的输入折叠与注册方式，精读留给 u9-l2） |
| [train_net_video.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py) | 视频训练入口：Trainer 覆写与 setup 的六层配置叠加 |
| [train_net.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py) | 图像版入口，本讲作 diff 的对照组 |
| [mask2former_video/data_video/dataset_mapper.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py) | YTVISDatasetMapper：训练采帧 / 推理全帧（本讲只看采帧逻辑，精读留给 u9-l3） |
| [mask2former_video/data_video/build.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/build.py) | 视频版 train/test DataLoader 构建函数 |
| [configs/youtubevis_2019/video_maskformer2_R50_bs16_8ep.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/youtubevis_2019/video_maskformer2_R50_bs16_8ep.yaml) | YTVIS R50 模型配置，实践任务用它构建模型 |

## 4. 核心概念与源码讲解

### 4.1 add_maskformer2_video_config：视频专属配置

#### 4.1.1 概念说明

图像版的 `add_maskformer2_config` 注册了上百个键（见 u1-l5）；视频版要「加装」的东西其实非常少——模型结构几乎照搬，唯一的新维度是**帧**。所以视频配置层只新增 4 个键，全部挂在 `INPUT` 下，因为「视频性」首先体现在数据如何采样帧，而不是网络结构。

#### 4.1.2 核心流程

```text
setup(cfg) 中六层叠加（视频版）：
get_cfg()                    # detectron2 默认配置树
  → add_deeplab_config       # poly lr 等键
  → add_maskformer2_config   # 图像版全部专属键
  → add_maskformer2_video_config  # ★ 本讲：4 个视频键
  → cfg.merge_from_file(yaml)     # 配置文件覆盖
  → cfg.merge_from_list(opts)     # 命令行覆盖，优先级最高
  → cfg.freeze()
```

`SAMPLING_FRAME_NUM` 的三个消费点（这是本模块的主线）：

```text
cfg.INPUT.SAMPLING_FRAME_NUM
  ├─→ YTVISDatasetMapper.from_config     # 训练时每个 clip 采几帧
  ├─→ VideoMaskFormer.from_config        # 存为模型属性 num_frames
  │      └─→ 训练时解码器用它把 BT 切回 B×T
  └─→ VideoMultiScaleMaskedTransformerDecoder.from_config
                                         # 解码器自己也存一份 num_frames
```

#### 4.1.3 源码精读

视频配置注册的全部内容只有这几行：

- [mask2former_video/config.py:L6-L12](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/config.py#L6-L12)：注册 4 个键——`SAMPLING_FRAME_NUM = 2`（一个 clip 的帧数）、`SAMPLING_FRAME_RANGE = 20`（训练采帧窗口，参考帧前后各 20 帧内采样）、`SAMPLING_FRAME_SHUFFLE = False`（是否打乱 clip 内帧序）、`AUGMENTATIONS = []`（可选光度增强列表，默认关闭）。

这个函数在入口脚本的 setup 中被叠加进配置链：

- [train_net_video.py:L241-L257](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L241-L257)：视频版 `setup()`。对比图像版（[train_net.py:L281-L295](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L281-L295)），唯一的结构性差异是多调了一次 `add_maskformer2_video_config(cfg)`（[L249](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L249)），以及多挂一个 `mask2former_video` 名字的 logger（[L256](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L256)）。

第一个消费点——mapper 的采帧逻辑：

- [mask2former_video/data_video/dataset_mapper.py:L188-L203](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L188-L203)：训练时（`is_train=True`）随机选一个参考帧 `ref_frame`，在 `ref_frame ± SAMPLING_FRAME_RANGE` 的窗口内再随机抽 `SAMPLING_FRAME_NUM - 1` 帧，凑成一个 clip；推理时（`else` 分支）`selected_idx = range(video_length)`——**整个视频的所有帧一次性作为一个 clip 送入模型**。注意：本仓库的推理实现是「整段视频一次前向」，并没有实现自动的滑窗分块与结果拼接；长视频受显存约束时的滑窗策略需要使用者自行实现。

第二、三个消费点——模型的 `num_frames`：

- [mask2former_video/video_maskformer_model.py:L145-L148](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L145-L148)：`from_config` 的返回字典里，`num_frames` 取自 `cfg.INPUT.SAMPLING_FRAME_NUM`，与 `pixel_mean` 等常规项并列注入构造器。
- [mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py:L366](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L366)：解码器的 `from_config` 同样读取该键并存为 `self.num_frames`（[L276](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L276)），训练时用它做 BT 切分（见 4.2.3）。

一个容易被忽略的事实：整条配置继承链（`Base-YouTubeVIS-VideoInstanceSegmentation.yaml` → `video_maskformer2_R50_bs16_8ep.yaml`，后者见 [configs/youtubevis_2019/video_maskformer2_R50_bs16_8ep.yaml:L1-L46](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/youtubevis_2019/video_maskformer2_R50_bs16_8ep.yaml#L1-L46)，其中没有任何 `INPUT:` 段）都**没有覆盖** `SAMPLING_FRAME_NUM`，所以 YTVIS 模型训练时每个 clip 就是默认的 **2 帧**。另外 Base yaml 里的 `MIN_SIZE_TRAIN_SAMPLING: "choice_by_clip"` 与 `RANDOM_FLIP: "flip_by_clip"`（[Base-YouTubeVIS-VideoInstanceSegmentation.yaml:L37-L39](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/youtubevis_2019/Base-YouTubeVIS-VideoInstanceSegmentation.yaml#L37-L39)）是视频专属的增强方式——同一个 clip 的所有帧共享同一次缩放/翻转决策，保证跨帧几何一致。

#### 4.1.4 代码实践

**实践目标**：亲眼确认 4 个视频键的默认值与 `SAMPLING_FRAME_NUM` 的三个消费点。

**操作步骤**（示例代码，在仓库根目录运行）：

```python
# check_video_cfg.py（示例代码）
from detectron2.config import get_cfg
from detectron2.projects.deeplab import add_deeplab_config
from mask2former import add_maskformer2_config
from mask2former_video import add_maskformer2_video_config

cfg = get_cfg()
add_deeplab_config(cfg)
add_maskformer2_config(cfg)
before = cfg.clone()
add_maskformer2_video_config(cfg)
print("视频版新增的键：", before.diff(cfg))
print("SAMPLING_FRAME_NUM =", cfg.INPUT.SAMPLING_FRAME_NUM)
```

再用文本搜索确认消费点：

```bash
grep -rn "SAMPLING_FRAME_NUM" mask2former_video/ --include="*.py"
```

**需要观察的现象**：`diff` 输出恰好 4 个 `INPUT.` 下的键；grep 命中 3 处消费（`dataset_mapper.py` 的 `from_config`、`video_maskformer_model.py` 的 `from_config`、`video_mask2former_transformer_decoder.py` 的 `from_config`）加上 `config.py` 的注册处。

**预期结果**：与 4.1.2 的流向图完全一致。若环境缺少 detectron2 依赖无法运行脚本，可只做 grep 部分，配置默认值直接读 [mask2former_video/config.py:L9-L11](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/config.py#L9-L11) 核对（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SAMPLING_FRAME_NUM` 注册在 `INPUT` 下，而不是 `MODEL` 下？

**答案**：它的字面职责确实是「数据采样」——mapper 用它决定一个 clip 采几帧，属于数据管线配置。模型侧只是「顺手」复用同一个键来获得切分 BT 所需的 T 值，避免两个键不同步。这也解释了为什么模型和解码器都要各自 `from_config` 读一次：它们需要的不是新的结构超参，而是与数据侧严格一致的时间长度。

**练习 2**：把 `SAMPLING_FRAME_SHUFFLE` 打开会发生什么？模型的时序位置信息会丢失吗？

**答案**：训练 clip 内的帧序会被 `random.shuffle` 打乱（[dataset_mapper.py:L200-L201](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L200-L201)），相当于一种「顺序增强」。时序位置信息不会丢：视频解码器使用带时间维的 3D 位置编码 `PositionEmbeddingSine3D`（u9-l2 精读），帧的时间戳仍随特征一起进入注意力，打乱的只是物理播放顺序。

### 4.2 VideoMaskFormer.forward：(B, T) 折叠与两条分支

#### 4.2.1 概念说明

`VideoMaskFormer` 是视频版 meta arch，注册方式与图像版 `MaskFormer` 完全一致（同一个 `META_ARCH_REGISTRY`）。它的设计哲学可以概括成一句话：**能用 2D 图像组件的地方一律当图像处理，时序只在最后一层「解压」**。

- 前半段（backbone + 像素解码器）：帧全部摊平成 `BT` 大小的批次，逐帧独立计算——Swin/ResNet、FPN、MSDeformAttn 全部零改动复用图像包。
- 后半段（Transformer 解码器）：把特征 reshape 回 `(B, T, ...)`，再把「帧 × 空间」一起展平进注意力序列——这是全模型唯一发生帧间信息交换的地方，注意力序列长度从图像版的 \( S = H \times W \) 变为 \( S = T \times H \times W \)。

与图像版的另外两个关键差异：

1. **推理输出不走三开关**。图像版 `MaskFormer.forward` 按 `SEMANTIC_ON / INSTANCE_ON / PANOPTIC_ON` 分发三个推理分支（u6 系列）；视频版固定走 `inference_video`，输出 YTVIS 格式的实例结果。配置文件里的三开关（[video_maskformer2_R50_bs16_8ep.yaml:L41-L43](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/youtubevis_2019/video_maskformer2_R50_bs16_8ep.yaml#L41-L43)）在 `VideoMaskFormer` 中没有被读取——视频实例分割只有一种输出形态。
2. **`sem_seg_postprocess_before_inference` 被硬编码为 `True`**（[video_maskformer_model.py:L143](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L143)）：图像版里这个开关用来权衡「先物化 Q 张高清 mask」还是「先融合省显存」（u6-l1），视频版只有一条 mask 物化路径，开关失去意义。

#### 4.2.2 核心流程

```text
forward(batched_inputs)          # batched_inputs: B 个视频 dict
│
├─ 1. 帧折叠：双层循环把 B×T 帧摊平成 list，归一化后打包 ImageList
│      images.tensor 形状 (B*T, 3, H_pad, W_pad)
│
├─ 2. 2D 复用：backbone(images.tensor) → sem_seg_head(features)
│      此时一切与图像版无异，batch 维就是 BT
│
├─ 3a. 训练分支（self.training=True）
│      prepare_targets: 每个视频组装 {labels, ids, masks(N,T,H,W)}
│      criterion(outputs, targets) → 按 weight_dict 加权过滤 → 返回 loss dict
│
└─ 3b. 推理分支（self.training=False）
       pred_masks: (1, Q, T, H/4, W/4) → retry_if_cuda_oom 上采样到 padding 尺寸
       只取 batched_inputs[0]（推理 batch 恒为 1 个视频）
       inference_video: softmax 去 no-object → topk(10) → mask>0 二值化
       → {image_size, pred_scores, pred_labels, pred_masks}
```

#### 4.2.3 源码精读

**注册与构造**：

- [video_maskformer_model.py:L24-L25](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L24-L25)：`@META_ARCH_REGISTRY.register()` 注册 `VideoMaskFormer`——与图像版共用同一个 meta arch 注册表，`META_ARCHITECTURE: "VideoMaskFormer"` 一行即可切换（[video_maskformer2_R50_bs16_8ep.yaml:L4](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/youtubevis_2019/video_maskformer2_R50_bs16_8ep.yaml#L4)）。
- [video_maskformer_model.py:L45-L47](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L45-L47) 与 [L88](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L88)：构造参数与图像版逐项对齐后，只多了一个 `num_frames`。这就是「视频版 = 图像版 + 帧维」在类签名上的体现。
- [video_maskformer_model.py:L105-L132](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L105-L132)：`from_config` 组装的匹配器与损失分别是**视频版**的 `VideoHungarianMatcher` 与 `VideoSetCriterion`（精读留给 u9-l4），深监督把 `weight_dict` 从 3 键展开成 30 键的逻辑与图像版一致（u5-l3）。

**帧折叠——本讲最重要的一段代码**：

- [video_maskformer_model.py:L180-L185](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L180-L185)：双层 for 循环遍历「每个视频的每一帧」，把 `video["image"]`（长度 T 的帧 tensor 列表）摊平成一个 `images` 列表，逐帧减均值除方差后交给 `ImageList.from_tensors` 打 pad。从此处开始到解码器之前，代码里**不再有「视频」这个概念**——只有 `BT` 张普通的图。
- [video_maskformer_model.py:L187-L188](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L187-L188)：`backbone(images.tensor)` 与 `sem_seg_head(features)`，与图像版 `MaskFormer.forward` 的同段代码一字不差。这里复用的证据链在配置文件里：`SEM_SEG_HEAD.NAME: "MaskFormerHead"`（[yaml:L6](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/youtubevis_2019/video_maskformer2_R50_bs16_8ep.yaml#L6)）取的是图像包注册的 head 组装器（u3-l2），`PIXEL_DECODER_NAME: "MSDeformAttnPixelDecoder"`（[yaml:L14](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/youtubevis_2019/video_maskformer2_R50_bs16_8ep.yaml#L14)）取的是图像包的像素解码器（u3-l5），只有 `TRANSFORMER_DECODER_NAME: "VideoMultiScaleMaskedTransformerDecoder"`（[yaml:L20](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/youtubevis_2019/video_maskformer2_R50_bs16_8ep.yaml#L20)）换成视频包的实现。
- [video_mask2former_transformer_decoder.py:L13](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L13) 与 [L208-L209](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L208-L209)：视频解码器直接 `from mask2former.modeling.transformer_decoder... import TRANSFORMER_DECODER_REGISTRY` 并注册——**跨包共用图像包自建的那张解码器注册表**（u8-l3 讲过的内部名 `"TRANSFORMER_MODULE"`）。这是「视频包不 import 图像包」总规则的唯一一处显式例外，目的是让图像包的 `MaskFormerHead.build_transformer_decoder` 工厂能按名字查到视频解码器。

**解码器入口处的时间维解压**：

- [video_mask2former_transformer_decoder.py:L370-L374](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L370-L374)：解码器 `forward` 的开头三行是折叠的逆操作——`mask_features` 进来时是 `(BT, C, H, W)`；训练时 `bs = bt // self.num_frames`，推理时 `bs = 1`，于是 `t = bt // bs`（推理时 T = 整段视频的帧数），再 `view(bs, t, c_m, h_m, w_m)` 还原出帧维。
- [video_mask2former_transformer_decoder.py:L390-L393](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L390-L393)：注释 `NTxCxHW => NxTxCxHW => (TxHW)xNxC` 说明关键变换——每个尺度的特征被重排成 `(T×HW, N, C)` 的序列，**帧维与空间维合并**。这正是帧间交互的实现载体：query 的 cross-attention 一次 attend 所有帧的所有空间位置。
- [video_mask2former_transformer_decoder.py:L449](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L449)：mask 预测的 einsum 从图像版的 `"bqc,bchw->bqhw"` 变为 `"bqc,btchw->bqthw"`——每个 query 的 mask 嵌入与全部 T 帧的 `mask_features` 点积，直接产出时空掩码 `(B, Q, T, H, W)`。细节留待 u9-l2。

**训练分支**：

- [video_maskformer_model.py:L190-L203](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L190-L203)：与图像版同构——`prepare_targets` 组装目标、criterion 算损失、按 `weight_dict` 加权并丢弃白名单外的键。日志里的 loss 键同样是 30 个（3 键 × DEC_LAYERS=10 组深监督，见 u5-l3）。

- [video_maskformer_model.py:L227-L253](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L227-L253)：`prepare_targets` 把每帧的 `Instances` 列表组装成**视频级**目标，分四步：
  1. 以第 0 帧的实例数 N 开一个 `(N, T, H_pad, W_pad)` 的全零 bool 容器（[L231-L233](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L231-L233)）；
  2. 逐帧把 `gt_masks.tensor` 左上角对齐写进对应帧切片，同时收集每帧的 `gt_ids` 拼成 `(N, T)`（[L236-L243](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L236-L243)）——GT mask 的 zero-pad 方式与图像版严格对称（u3-l1）；
  3. `valid_idx = (gt_ids != -1).any(dim=-1)` 过滤「一帧都没出现」的实例（[L244](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L244)）；`-1` 来自 mapper 的占位标注 `_get_dummy_anno`（[dataset_mapper.py:L57-L65](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L57-L65)，其中 `"id": -1`）——实例在某一帧不可见时，mapper 就用 dummy anno 占住它在序列中的位置；
  4. 输出 `{"labels", "ids", "masks"}`（[L246-L251](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L246-L251)）。注意两个细节：`labels` 取自循环结束后残留的 `targets_per_frame`（最后一帧），这依赖「同一实例类别跨帧不变」的数据事实；`ids` 是图像版目标里没有的新键，视频版匹配器靠它知道「同一行内的 T 张 mask 是同一个物体」。

**推理分支**：

- [video_maskformer_model.py:L204-L225](https://github.com/facebooksearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L204-L225)：取 `pred_logits`/`pred_masks` 后**只保留第 0 个元素**（`mask_cls_results[0]`）——推理 batch 恒为 1 个视频（test loader 的 `BatchSampler(sampler, 1, ...)` 见 [build.py:L240-L249](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/build.py#L240-L249)，且解码器推理时 `bs=1`）。随后 `retry_if_cuda_oom(F.interpolate)` 把 `(Q, T, H/4, W/4)` 的整段时空 mask 一次上采样到 padding 尺寸（[L210-L215](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L210-L215)）——OOM 兜底装饰器来自视频包的 `mask2former_video/utils/memory.py`（u10-l2 精读），这里上采样的对象是「整个视频所有帧」，显存压力远大于图像版单图，所以全程包在重试逻辑里。最后 `retry_if_cuda_oom(self.inference_video)` 连后处理也包了一层（[L225](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L225)）。
- [video_maskformer_model.py:L255-L274](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L255-L274)：`inference_video` 是视频版的「实例推理分支」简化版——`softmax` 后切掉 no-object 通道、query×class 展平后取 `topk(10)`（**硬编码 10**，不读 `TEST.DETECTIONS_PER_IMAGE`）、扁平索引整除类别数还原 query 索引（与图像版 `instance_inference` 的 topk 逻辑同构，见 u6-l2）；mask 先裁掉 padding（`[:, :, :img_size[0], :img_size[1]]`）再双线性上采样到原图 `height/width`，最后 `> 0` 阈值二值化。
- [video_maskformer_model.py:L280-L287](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L280-L287)：输出契约是一个**单个 dict**（不是图像版的 list[dict]），含 `image_size / pred_scores / pred_labels / pred_masks`；`pred_masks` 是长度为 topk 数的列表，每个元素 `(T, H, W)` 是一个实例的时空二值 mask。下游 `YTVISEvaluator.process` 拿到它后调用 `instances_to_coco_json_video` 转成逐帧 RLE 的 YTVIS json（[ytvis_eval.py:L103-L113](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/ytvis_eval.py#L103-L113)，按 `video_id` 组织，见 [L269](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/ytvis_eval.py#L269)、[L274](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/ytvis_eval.py#L274)），评估细节在 u9-l4。

#### 4.2.4 代码实践

**实践目标**：构建 `VideoMaskFormer`，打印顶层模块树验证「三大组件两个来自图像包」，再用假视频输入分别跑通训练/推理两种模式。

**前置**：需要按 u1-l2 完成 detectron2 安装与 MSDeformAttn CUDA 扩展编译（`MSDeformAttnPixelDecoder` 在 import 期强依赖编译扩展，CPU 机器也要先编译，运行时可回退 CPU）。无需下载 YTVIS 数据集——构建模型只读 config，不碰数据。

**操作步骤**（示例代码，保存为 `try_video_model.py` 在仓库根目录运行）：

```python
# try_video_model.py（示例代码）
import torch
from detectron2.config import get_cfg
from detectron2.projects.deeplab import add_deeplab_config
from detectron2.modeling import build_model
from detectron2.structures import Instances, BitMasks

import mask2former  # noqa: F401  import 即注册（图像包）
import mask2former_video  # noqa: F401  import 即注册（视频包）
from mask2former import add_maskformer2_config
from mask2former_video import add_maskformer2_video_config

cfg = get_cfg()
add_deeplab_config(cfg)
add_maskformer2_config(cfg)
add_maskformer2_video_config(cfg)
cfg.merge_from_file("configs/youtubevis_2019/video_maskformer2_R50_bs16_8ep.yaml")
cfg.MODEL.WEIGHTS = ""  # 随机初始化，只看结构
cfg.freeze()

model = build_model(cfg)  # META_ARCHITECTURE: VideoMaskFormer
print(model)  # 顶层模块树：backbone / sem_seg_head / criterion

T, H, W = 2, 240, 320  # 与 SAMPLING_FRAME_NUM=2 对齐
frames = [torch.randn(3, H, W) for _ in range(T)]

# —— 训练模式：需要逐帧 Instances（gt_classes / gt_ids / gt_masks）——
def fake_frame(n_visible):
    inst = Instances((H, W))
    inst.gt_classes = torch.tensor([3, 5][:n_visible])
    inst.gt_ids = torch.tensor([0, 1][:n_visible])
    masks = torch.zeros(2, H, W, dtype=torch.bool)
    masks[0, :50, :50] = True                      # 实例 0 可见
    if n_visible > 1:
        masks[1, 100:150, 100:150] = True          # 实例 1 可见
    inst.gt_masks = BitMasks(masks[:n_visible])
    return inst

model.train()
losses = model([{"image": frames, "instances": [fake_frame(2), fake_frame(1)]}])
print("loss keys =", sorted(losses.keys()))
print("loss_ce =", losses["loss_ce"].item())

# —— 推理模式：需要 height / width，mask 上采样目标 ——
model.eval()
with torch.no_grad():
    out = model([{"image": frames, "height": 720, "width": 1280}])
print("output keys =", list(out.keys()))
print("num instances =", len(out["pred_masks"]),
      "mask shape =", out["pred_masks"][0].shape)
```

**需要观察的现象**：

1. `print(model)` 的模块树：`backbone`（ResNet-50，来自 detectron2）、`sem_seg_head.pixel_decoder`（MSDeformAttnPixelDecoder）、`sem_seg_head.predictor`（VideoMultiScaleMaskedTransformerDecoder）、`criterion`（VideoSetCriterion）。
2. 训练模式返回 30 个键（`loss_ce`、`loss_mask`、`loss_dice` 及 `_0`..`_8` 后缀的 aux 键）。
3. 推理模式输出 dict 的四个键；`pred_masks[0]` 形状为 `(2, 720, 1280)`——即 `(T, 原图高, 原图宽)`。

**预期结果**：训练分支 `len(losses) == 30`；推理分支 `out["pred_masks"]` 长度 ≤ 10（topk 硬编码 10），每个元素 `(2, 720, 1280)` bool。若本机尚未编译 CUDA 扩展或未装 detectron2，请先回到 u1-l2 完成环境（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `VideoMaskFormer.forward` 的推理分支可以直接写 `mask_cls_results[0]`、`batched_inputs[0]`，而不像图像版那样遍历整个 batch？

**答案**：两个约束共同保证推理 batch 恒为 1 个视频。数据侧，视频版 test loader 用 `BatchSampler(sampler, 1, drop_last=False)`（[build.py:L241-L249](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/build.py#L241-L249)），每个 batch 只有一个视频的所有帧；模型侧，解码器推理时强制 `bs = 1`（[video_mask2former_transformer_decoder.py:L372](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/transformer_decoder/video_mask2former_transformer_decoder.py#L372)），`bt` 全部解释为帧数。整段视频一次前向意味着 batch 维退化，索引用 `[0]` 即可。

**练习 2**：视频版 `prepare_targets` 输出的目标字典比图像版多一个 `"ids"` 键。如果删掉它、只保留 `labels` 和 `masks`，训练还能进行吗？

**答案**：能跑通 forward，但监督语义会错。`masks` 的形状是 `(N, T, H, W)`——同一个实例在 T 帧的 mask 已经沿第 1 维捆在一起，匹配与损失天然按「时空 mask 整体」计算（u9-l4 精读），跨帧关联信息其实已经编码在形状里。`ids` 是显式冗余：它让 `valid_idx` 能过滤「从未出现」的实例（dummy anno 的 `-1`），也让目标结构自解释。真正不可删的是 `(N, T, ...)` 这个维度设计，而不是 `ids` 键本身。

**练习 3**：图像版推理有语义/实例/全景三个分支，视频版为什么只剩一种输出？

**答案**：视频实例分割任务本身只有一个输出形态——「带跨帧 id 的实例掩码序列」；YTVIS 评估也只吃这一种格式。`inference_video` 因此被写死为「softmax 去 no-object → topk → 二值化」这一条路径，配置文件里的三开关与 `TEST.DETECTIONS_PER_IMAGE` 对视频模型均不生效（topk 数硬编码 10）。

### 4.3 视频 Trainer：train_net_video.py 与 train_net.py 的 diff

#### 4.3.1 概念说明

u2-l1 讲过图像版 `Trainer` 的四个覆写方法（`build_train_loader / build_optimizer / build_lr_scheduler / build_evaluator`）。视频版 `Trainer` 同样继承 `DefaultTrainer`，但覆写集合变了。理解 diff 的钥匙是一个事实：**detectron2 引擎假设数据是「单图 dict」**——视频数据是「一个视频 dict 内嵌 T 帧」，于是所有「引擎默认帮你做数据侧事情」的地方都必须接管。

#### 4.3.2 核心流程

两个 Trainer 的逐方法对照表：

| 方法 | train_net.py（图像版） | train_net_video.py（视频版） |
| --- | --- | --- |
| `build_evaluator` | 按 `evaluator_type` 大 if-else 组合多评估器（L68-147） | 一律返回 `YTVISEvaluator`（L62-74） |
| `build_train_loader` | 按 `INPUT.DATASET_MAPPER_NAME` 分发 5 种 mapper，用 detectron2 原生 loader（L150-173） | 固定 `YTVISDatasetMapper` + 视频包自己的 `get_detection_dataset_dicts` / `build_detection_train_loader`（L76-87） |
| `build_test_loader` | **不覆写**，继承 `DefaultTrainer` 默认 | **必须覆写**（L89-93） |
| `build_optimizer` | 参数分组工厂（L184-262） | 几乎逐行复制（L103-182） |
| `build_lr_scheduler` | deeplab `build_lr_scheduler`（L175-181） | 完全相同（L95-101） |
| `test` | 不覆写；另有 `test_with_TTA`（L264-278） | 覆写 `test`：autocast 包裹、无 TTA（L184-238） |
| `setup` | 五层配置叠加（L281-295） | 六层：多 `add_maskformer2_video_config`（L249） |

#### 4.3.3 源码精读

**import 双包——薄层复用的接线处**：

- [train_net_video.py:L46-L54](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L46-L54)：先 `from mask2former import add_maskformer2_config`（触发图像包注册：backbone、pixel decoder、`MaskFormerHead` 等），再 `from mask2former_video import ...`（触发视频包注册：`VideoMaskFormer`、视频解码器等，见 [mask2former_video/__init__.py:L1-L17](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/__init__.py#L1-L17)）。对照图像版 import（[train_net.py:L50-L59](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L50-L59)）：图像版要 import 5 种 mapper 与 TTA 类，视频版一个 mapper 就够，换成 import 数据 loader 构建函数。

**build_evaluator——从分发器到一行返回**：

- [train_net_video.py:L62-L74](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L62-L74)：不再需要 u7-l1 那套「两把钥匙」分发逻辑，直接 `return YTVISEvaluator(dataset_name, cfg, True, output_folder)`。YTVIS 任务只有一种评估方式，分发退化为直返。
- 对照图像版 [train_net.py:L67-L147](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L67-L147)：70 余行的 if-else 组合（COCO/ADE20K/Cityscapes/LVIS × 三开关）在视频侧完全消失。

**build_train_loader——三件套全换视频版**：

- [train_net_video.py:L76-L87](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L76-L87)：`YTVISDatasetMapper(cfg, is_train=True)` 做帧采样与标注重组（u9-l3 精读）；`get_detection_dataset_dicts` 是视频包复制改造的版本（[build.py:L74-L111](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/build.py#L74-L111)，其 `filter_images_with_only_crowd_annotations` 被改成能识别「帧 × 实例」嵌套的 annotations，[L52-L63](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/build.py#L52-L63)）；`build_detection_train_loader` 也是视频包版本（[L142-L186](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/build.py#L142-L186)）。对照图像版 [train_net.py:L150-L173](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L150-L173)：图像版按 `DATASET_MAPPER_NAME` 走 if-elif 五分支，且直接用 detectron2 原生 loader。
- 视频包 loader 里 `build_batch_data_loader(..., aspect_ratio_grouping=cfg.DATALOADER.ASPECT_RATIO_GROUPING, ...)`（[build.py:L180-L186](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/build.py#L180-L186)）保留了按宽高比分组的能力——分组读的是视频 dict 的 `width/height` 键，mapper 处理每帧时用 `check_image_size` 维护了这两个键。

**build_test_loader——视频版新增的覆写**：

- [train_net_video.py:L89-L93](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L89-L93)：用 `YTVISDatasetMapper(cfg, is_train=False)`（推理时全帧通过，见 4.1.3）+ 视频包 `build_detection_test_loader`。图像版**没有**这个方法——`DefaultTrainer` 默认实现用 detectron2 原生 `DatasetMapper`（单图假设）构建测试 loader；YTVIS 的 dataset dict 里 `annotations` 是「帧 × 实例」的嵌套 list、`file_names` 是整段视频的帧路径列表，原生 mapper 无法处理，所以必须连 test 侧一起接管。

**build_optimizer / build_lr_scheduler——原样搬运**：

- [train_net_video.py:L103-L182](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L103-L182)：与图像版 [train_net.py:L184-L262](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L184-L262) 逐行几乎一致：同样的参数分组规则（backbone lr × `BACKBONE_MULTIPLIER`、Swin 位置参数免 decay、norm/embed 单独 decay）、同样的 `maybe_add_full_model_gradient_clipping` 动态子类化（[L152-L167](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L152-L167)）。细节已在 u2-l4 讲透，此处不重复。
- [train_net_video.py:L95-L101](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L95-L101)：`build_lr_scheduler` 与图像版一字不差。

**test 覆写与 TTA 的缺席**：

- [train_net_video.py:L184-L238](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L184-L238)：视频版覆写了 `test`，主体是把 detectron2 默认逻辑复制一遍，差别在于用 `with autocast():` 包裹 `inference_on_dataset`（[L224-L225](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L224-L225)，与 Base yaml 的 `AMP.ENABLED: True` 训练设定相呼应）。图像版不覆写 `test`，而是提供 `test_with_TTA`（[train_net.py:L264-L278](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L264-L278)，包装 `SemanticSegmentorWithTTA`，u10-l3）；视频版没有 TTA 路径，`main` 里开了 `TEST.AUG.ENABLED` 直接抛 `NotImplementedError`（[train_net_video.py:L269-L270](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L269-L270)）——对照图像版在同位置的 TTA 调用（[train_net.py:L307-L308](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L307-L308)）。

**main 与 launch——完全同构**：

- [train_net_video.py:L260-L290](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L260-L290)：`main` 的「eval-only 分支 / 训练分支」与 `launch` 分布式启动和图像版（[train_net.py:L298-L315](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L298-L315)）结构一致，u2-l1 的结论原样适用：`--eval-only` 不实例化 Trainer，`resume=True` 连优化器状态一起恢复。

#### 4.3.4 代码实践

**实践目标**：亲手做一次两入口脚本的 diff，归纳视频版新增/覆写的 `build_*` 方法清单。

**操作步骤**：

```bash
# 在仓库根目录执行（只读操作）
git diff --no-index --stat train_net.py train_net_video.py
git diff --no-index -U2 train_net.py train_net_video.py | head -120
```

然后只用 grep 提取两边的类方法骨架做对照：

```bash
grep -n "    def \|    @classmethod" train_net.py
grep -n "    def \|    @classmethod" train_net_video.py
```

**需要观察的现象**：diff 中 `build_optimizer` 段几乎无变化；变化集中在三处——`build_evaluator` 大幅缩短、`build_train_loader` 换了数据侧三件套、多出一个 `build_test_loader`；此外多了 `test` 覆写、少了 `test_with_TTA`。

**预期结果**：归纳出与 4.3.2 表格一致的结论——视频版覆写 6 个类方法（`build_evaluator / build_train_loader / build_test_loader / build_lr_scheduler / build_optimizer / test`），其中 `build_test_loader` 与 `test` 是图像版没有的，`test_with_TTA` 是图像版独有的。

#### 4.3.5 小练习与答案

**练习 1**：为什么图像版可以不覆写 `build_test_loader`，视频版却必须？

**答案**：`DefaultTrainer.build_test_loader` 的默认实现用 detectron2 原生 `DatasetMapper(cfg, False)`，它假设 dataset dict 是「单张图 + 平铺 annotations」。图像版 mapper 产出的 dict 恰好符合这个假设（u2-l3），所以默认实现可用；YTVIS 的 dict 是「视频级」的——`file_names` 是全帧路径列表、`annotations` 按帧嵌套，原生 mapper 既不会采帧也不会把标注转成逐帧 `Instances`，必须换成 `YTVISDatasetMapper(cfg, is_train=False)` 并接管 test loader 构建。

**练习 2**：视频版 `build_train_loader` 里的 `get_detection_dataset_dicts` 为什么不直接用 detectron2 的同名函数，而要在 `mask2former_video/data_video/build.py` 里复制一份？

**答案**：训练前的「过滤无标注样本」逻辑对数据格式有假设。detectron2 版本的 `filter_images_with_only_crowd_annotations` 假设 `annotations` 是平铺的实例列表；视频包版本（[build.py:L52-L71](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/build.py#L52-L71)）加了 `isinstance(anns, list)` 分支来处理「每个元素又是一帧标注列表」的嵌套结构。这是薄层复用的典型代价：凡是引擎代码里写死了图像假设的工具函数，都要复制修改。

## 5. 综合实践

把本讲三个模块串成一个完整的「结构考古」任务：

1. **纸面 diff**：执行 4.3.4 的两条 grep 命令，自己画出两入口脚本的「覆写方法对照表」（不要偷看 4.3.2 的表格）。
2. **构建与打印**：运行 4.2.4 的 `try_video_model.py`，从打印出的模块树里标注每个子模块来自哪个包——`backbone`（detectron2）、`sem_seg_head.pixel_decoder`（mask2former 图像包）、`sem_seg_head.predictor`（mask2former_video 视频包）、`criterion`（视频包），并用「哪些组件被视频版复用、哪些被重写」写一段三句话总结。
3. **帧数敏感性实验**：在 4.2.4 脚本基础上用 `cfg.merge_from_list(["INPUT.SAMPLING_FRAME_NUM", "3"])` 把 clip 改成 3 帧（构造输入时 `frames` 也给 3 帧、`instances` 给 3 帧的列表），重跑训练分支。观察：loss 键集合不变（仍是 30 个），因为帧数只影响数据的 T 维，不影响深监督结构；再跑推理分支，确认 `pred_masks[0]` 形状变为 `(3, 720, 1280)`。

预期（待本地验证）：三步全部完成后，你应该能不看讲义说出 `SAMPLING_FRAME_NUM` 从配置键到模型行为的完整传导路径——mapper 采帧 → `VideoMaskFormer.num_frames` → 解码器训练时的 `bs = bt // num_frames` 切分与推理时的 `bs = 1` 全帧假设。

## 6. 本讲小结

- 视频配置层极薄：`add_maskformer2_video_config` 只注册 4 个 `INPUT.` 键，其中 `SAMPLING_FRAME_NUM`（默认 2，YTVIS 配置链未覆盖）是唯一贯穿「数据采帧 → 模型切分 → 解码器」三个消费点的核心键。
- `VideoMaskFormer.forward` 用双层循环把 B×T 帧摊平成 `BT` 图像批次，backbone 与像素解码器零改动复用图像包；帧间交互全部集中在 Transformer 解码器的 `(T×HW)` 展平注意力里。
- 训练目标多了 `ids` 键：`(N, T, H, W)` 的时空 mask 沿帧维把同一实例捆成一个监督单位，dummy anno（`id=-1`）标记「该帧不可见」，`valid_idx` 过滤从未出现的实例。
- 推理是「整段视频一次前向」：mapper 全帧通过、解码器 `bs=1`、forward 直接取 `[0]`；上采样与后处理都包 `retry_if_cuda_oom`；`inference_video` 硬编码 topk(10)，三开关与 `TEST.DETECTIONS_PER_IMAGE` 均不被视频模型读取。
- 视频 Trainer 覆写 6 个方法：`build_test_loader` 与 `test` 是新增覆写（detectron2 引擎的单图假设失效所致），`build_evaluator` 退化为直返 `YTVISEvaluator`，TTA 被显式 `NotImplementedError` 拒绝。
- 视频解码器注册进图像包的 `TRANSFORMER_DECODER_REGISTRY`（跨包共用一张表），这是「视频包不 import 图像包」总规则的唯一显式例外，目的是让图像包 head 工厂能按名字查到视频解码器。

## 7. 下一步学习建议

下一讲 **u9-l2 视频解码器与 3D 位置编码** 将深入本讲留下的黑盒 `VideoMultiScaleMaskedTransformerDecoder.forward`：`(T×HW)` 展平如何让 masked attention 天然获得时序感受野、`einsum("bqc,btchw->bqthw")` 的时空 mask 预测、以及 `PositionEmbeddingSine3D` 如何在帧维注入位置信息。若想先补数据侧背景，可跳读 [mask2former_video/data_video/dataset_mapper.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py) 的 `__call__`（u9-l3 的主题）；想对照图像版解码器主线的话，复习 u4-l3 的尺度轮转与 attn_mask 兜底逻辑即可——视频版逐行对应，只是序列里多了帧维。
