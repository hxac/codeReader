# u9-l3 视频数据管线：YTVIS 数据集与 Mapper

## 1. 本讲目标

u9-l1 讲了 `VideoMaskFormer` 怎么把「B 个视频 × T 帧」折叠成 `BT` 批次，u9-l2 讲了解码器怎么用 `(T×HW)` 展平的注意力建模时序。本讲回到数据侧，回答一个上游问题：**这些 (B, T) 形状的输入和带跨帧关联的监督信号，究竟是谁、在哪里、以什么格式造出来的？** 学完本讲，你应该能够：

1. 画出 YTVIS json 的字段结构，并解释 `load_ytvis_json` 如何把它解析成「一条视频一个 dict、标注按 `[帧][实例]` 嵌套」的 detectron2 视频格式。
2. 逐行解释 `YTVISDatasetMapper.__call__`：训练时如何在参考帧附近采样 T 帧、如何用「槽位表 + dummy 标注」把不同帧的实例行对齐、`gt_ids` 如何承载跨帧关联。
3. 说明 `gt_ids` 的完整生命周期：mapper 产出 → `filter_empty_instances` 置 -1 → `VideoMaskFormer.prepare_targets` 用 `id != -1` 过滤整段无效的轨道。
4. 描述 `builtin.py` 注册 YouTubeVIS 2019/2021 六个子集的命名规则（含 `val` 对应 `valid/` 目录这个易踩的坑），以及 `build.py` 为什么测试加载器恒为 batch=1。
5. 知道 `ytvis_annotations_to_instances` 与 `CocoClipDatasetMapper` 是从 IFC 继承的「遗产件」——当前主流程并未调用它们，但前者是理解目标 `Instances` 结构最短的可运行切片。

本讲依赖 u2-l3（图像版 DatasetMapper：统一出口是 `Instances`）与 u9-l1（视频配置键、BT 折叠、`prepare_targets` 概貌），并承接 u2-l2 的 DatasetCatalog/MetadataCatalog 双目录结论。

## 2. 前置知识

**从 COCO json 到 YTVIS json**。图像版数据（u2-l2/u2-l3）用的 COCO 格式是「一张图片一条记录，一条标注对应一个物体」。YTVIS（YouTube-VIS）格式把两层都升维：**一条视频一条记录**（`videos` 表：`file_names` 是帧路径列表、`length` 是帧数、`height/width` 全视频恒定）；**一条标注对应一条贯穿全视频的轨道（track）**——`bboxes` 和 `segmentations` 字段是**逐帧等长的列表**，某帧看不到该物体时对应位置为 `null`。

**轨道 / 时空管道（tube）**：同一条轨道在 T 帧里的 mask 序列拼起来，就是一个形状为 \((T, H, W)\) 的时空管道。u9-l2 的核心结论「一个 query 负责一条时空管道」，其监督信号正是数据侧把同一 `id` 的逐帧 mask 绑在一起提供的。

**`gt_ids`（跨帧关联标签，承接 u9-l1）**：每帧的 `Instances` 上多出一个图像版没有的字段 `gt_ids`，存放该行实例所属轨道的原始 json 标注 `id`；该帧看不到这条轨道时，这一行被填成 dummy 标注且 `gt_ids` 为 -1。**行对齐**是关键：一个 clip 内每帧的 `Instances` 行数恒等于该 clip 出现过的轨道数 N，第 i 行在所有帧里都是同一条轨道——这正是 `prepare_targets` 能直接按行堆叠出 `(N, T, H, W)` mask 的前提。

**clip 一致增强（clip-consistent augmentation）**：一个 clip 的 T 帧必须使用**同一组几何变换参数**（同样的缩放尺度、同样翻或不翻）。否则第 1 帧的狗水平翻转了、第 2 帧没翻，时空管道就被增强撕碎了。实现靠的是一个简单的计数器技巧，本讲 4.2 会精读。

**遗产件（heritage helper）**：`mask2former_video` 的数据代码移植自 IFC 项目（文件头 `Modified by Bowen Cheng from https://github.com/sukjunhwang/IFC`）。移植时保留了少量当前主流程不再调用的函数/类（`ytvis_annotations_to_instances`、`CocoClipDatasetMapper`），读源码时要把「在用的主线」和「留着的备件」分开。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mask2former_video/data_video/datasets/ytvis.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis.py) | YTVIS 类目表（2019/2021 各 40 类）、`load_ytvis_json` 解析器、`register_ytvis_instances` 注册函数，尾部附带一个可视化自测入口 |
| [mask2former_video/data_video/datasets/builtin.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/builtin.py) | 预定义 6 个子集（ytvis_2019/2021 × train/val/test）的路径表与批量注册 |
| [mask2former_video/data_video/datasets/ytvis_api/](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis_api/ytvos.py) | 从 youtubevos/cocoapi 移植的 `YTVOS` 读写 api，只负责 json 索引，不读图片 |
| [mask2former_video/data_video/dataset_mapper.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py) | 本讲主角：`YTVISDatasetMapper`（采帧+对齐）、`CocoClipDatasetMapper`（图转伪视频）、`ytvis_annotations_to_instances`、`filter_empty_instances`、`_get_dummy_anno` |
| [mask2former_video/data_video/augmentation.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/augmentation.py) | 改造过的 `ResizeShortestEdge`/`RandomFlip`（加 `clip_frame_cnt` 参数实现 clip 一致增强）与 `build_augmentation` |
| [mask2former_video/data_video/build.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/build.py) | 视频版 train/test DataLoader 构建函数与 `get_detection_dataset_dicts` |
| [train_net_video.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py) | Trainer 的 `build_train_loader`/`build_test_loader` 把上述零件接线 |
| [mask2former_video/video_maskformer_model.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py) | `prepare_targets`：gt_ids 的消费端（本讲只看 L227-L253 这一段） |
| [configs/youtubevis_2019/Base-YouTubeVIS-VideoInstanceSegmentation.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/youtubevis_2019/Base-YouTubeVIS-VideoInstanceSegmentation.yaml) | YTVIS 数据侧配置：`by_clip` 增强样式、`FILTER_EMPTY_ANNOTATIONS: False` |

## 4. 核心概念与源码讲解

### 4.1 load_ytvis_json：把 YTVIS json 解析成「视频 dict」

#### 4.1.1 概念说明

`load_ytvis_json` 是视频管线的第一站，职责与图像版的 `load_coco_json` 对应：**读 json → 产出 detectron2 格式的 `list[dict]`，一条视频一个 dict**。它不读任何图片（图片留给 mapper 按需读），所以**没有数据集也能完整 dry-run 这个函数**——这给本讲的综合实践留了空间。

它要解决三个视频特有的问题：

1. 轨道标注是「逐帧列表」，要拆成「每帧一组标注」的嵌套结构；
2. 某帧轨道缺席（`bboxes[t]` 或 `segmentations[t]` 为 `null`）时，要在该帧的标注列表里**直接跳过**这条轨道——注意是「这一帧没有它」，不是「整条轨道作废」；
3. 类别 id 与 COCO 一样可能不连续，要映射到 `[0, 40)` 的连续 id。

#### 4.1.2 核心流程

```text
load_ytvis_json(json_file, image_root, dataset_name)
  1. YTVOS(json_file)                 # 建 vid/ann/cat 索引（不读图）
  2. 若 dataset_name 非空：
       从 json 的 categories 重建 thing_classes
       id_map = {原始类别id: 连续id}   # 写入 MetadataCatalog
  3. 逐视频（按 vid 排序，保证可复现）：
       record = {file_names(拼 image_root), height, width, length, video_id}
       逐帧 t in [0, length)：
         逐轨道 anno：
           取 iscrowd / category_id / id 三个键
           若 bboxes[t] 或 segmentations[t] 为空 → 该帧跳过此轨道
           bbox 记为 XYWH_ABS；segm 规整（dict RLE 压缩 / polygon 过滤）
           category_id 经 id_map 映射为连续 id
       record["annotations"] = [帧0的标注列表, 帧1的, ...]   # 二层嵌套
  4. 返回 list[record]
```

#### 4.1.3 源码精读

- [ytvis.py:L27-L68](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis.py#L27-L68)：YTVIS 2019 的 40 个类目硬编码表，每项含 `color`（可视化用）、`isthing`（恒为 1，VIS 全是可数物体）、`id`、`name`。
- [ytvis.py:L71-L112](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis.py#L71-L112)：YTVIS 2021 的 40 类。注意 **2019 与 2021 是两套不同的分类学**：2021 删掉了 `hand`、`sedan`（并入 `car`）、`giraffe` 等，新增 `bird`、`flying_disc`、`squirrel`、`whale` 等，且类目按字母序重排——两版不能混用同一份类别映射。
- [ytvis.py:L115-L142](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis.py#L115-L142)：两个 `_get_ytvis_*_instances_meta` 从类目表构造元数据：`thing_dataset_id_to_contiguous_id`（1..40 → 0..39）与 `thing_classes`。因为 1..40 本身连续，这个映射是恒等平移——但保留它使代码与 COCO 的不连续 id 处理保持同一套模式。
- [ytvis.py:L145-L153](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis.py#L145-L153)：函数入口。`YTVOS` api 来自仓库自带的移植副本 [ytvis_api/ytvos.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis_api/ytvos.py)（其 `createIndex` 在 [ytvos.py:L68-L97](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis_api/ytvos.py#L68-L97)，只对 json 建字典索引，绝不碰磁盘图片）。
- [ytvis.py:L155-L180](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis.py#L155-L180)：当传入 `dataset_name` 时，从 json 的 `categories` 字段**重建** `thing_classes` 与 `id_map` 并写回 `MetadataCatalog`——也就是说注册时（4.5）填的静态元数据在真正加载时会被 json 的实际内容覆盖一遍；[L172-L178](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis.py#L172-L178) 检测到类别 id 不在 `[1, N]` 时打印映射告警（数据集名含 `coco` 则豁免）。
- [ytvis.py:L183-L205](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis.py#L183-L205)：`vid_ids = sorted(...)` 保证遍历顺序可复现；`vidToAnns` 把轨道按视频分组；源码注释里贴了一条真实 video 记录的结构（`file_names` 列表 / `length: 36` / `height: 720` 等），是理解 YTVIS json 最直观的样例。
- [ytvis.py:L209](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis.py#L209)：`ann_keys = ["iscrowd", "category_id", "id"]`——每帧标注只保留这三个键外加 bbox/segmentation，json 里的其余字段（如 `avg_area`）被丢弃。
- [ytvis.py:L213-L219](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis.py#L213-L219)：组装 record。`file_names` 在这里就拼上 `image_root`（[L215](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis.py#L215)），mapper 后面直接读。
- [ytvis.py:L229-L236](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis.py#L229-L236)：**缺帧跳过**的核心判断 `if not (_bboxes and _segm and _bboxes[frame_idx] and _segm[frame_idx]): continue`——轨道在该帧没有有效 bbox 或 mask 就跳过。`bbox_mode` 固定为 `XYWH_ABS`（YTVIS 惯例）。
- [ytvis.py:L241-L251](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis.py#L241-L251)：分割格式规整——RLE dict 若是未压缩的 `counts` 列表则用 `frPyObjects` 压缩；polygon 列表则滤掉长度为奇数或不足 6 个坐标（<3 个点）的无效多边形，整条无效则整条轨道跳过并计数告警（[L260-L267](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis.py#L260-L267)）。
- [ytvis.py:L253-L254](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis.py#L253-L254)：`obj["category_id"] = id_map[obj["category_id"]]` 完成连续 id 映射。注意 **轨道的 `id` 不做映射**——`gt_ids` 里存的就是 json 原始轨道 id（可能是任意整数）。
- [ytvis.py:L257](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis.py#L257)：`record["annotations"] = video_objs`，**二层嵌套列表 `[帧][实例]`**——这是视频 dict 与图像 dict 最大的结构差异，也是 4.5 中 `filter_images_with_only_crowd_annotations` 要专门加嵌套分支的原因。
- [ytvis.py:L271-L293](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis.py#L271-L293)：`register_ytvis_instances` 与 u2-l2 的 `register_*_instances` 完全同构：`DatasetCatalog.register(name, lambda: load_ytvis_json(...))` 登记惰性加载函数；`MetadataCatalog` 写 `json_file`、`image_root`、**`evaluator_type="ytvis"`**（u9-l1 讲过，视频 Trainer 的 `build_evaluator` 见到它直接返回 `YTVISEvaluator`）。
- [ytvis.py:L296-L335](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis.py#L296-L335)：`__main__` 自测入口——加载后逐帧用 detectron2 `Visualizer` 把标注画出来存盘，是检查自己数据格式是否正确的现成工具。

#### 4.1.4 代码实践

**实践目标**：不下载任何数据集，用一个手写的迷你 YTVIS json 跑通 `load_ytvis_json`，亲眼看到「二层嵌套」与「缺帧跳过」。

**操作步骤**（示例代码，保存为 `mini_ytvis_load.py` 在仓库根目录外的任意目录运行；需已安装 detectron2）：

```python
# 示例代码：用 3 帧的迷你 json 验证 load_ytvis_json 的解析行为
import json, os, tempfile
from mask2former_video.data_video.datasets.ytvis import load_ytvis_json

root = tempfile.mkdtemp()
def poly(x, y, s=12):            # 一个 12x12 的方形多边形（扁平坐标列表）
    return [x, y, x + s, y, x + s, y + s, x, y + s]

db = {
    "videos": [{"id": 1, "length": 3, "height": 48, "width": 64,
                "file_names": ["v1/00000.jpg", "v1/00001.jpg", "v1/00002.jpg"]}],
    "annotations": [
        {   # 轨道 1：3 帧全在场，类别 person(json id=1)
            "video_id": 1, "id": 101, "category_id": 1, "iscrowd": 0,
            "bboxes": [[5, 5, 12, 12], [6, 5, 12, 12], [7, 6, 12, 12]],
            "segmentations": [[poly(5, 5)], [poly(6, 5)], [poly(7, 6)]],
        },
        {   # 轨道 2：只在第 0 帧在场，类别 dog(json id=8)
            "video_id": 1, "id": 202, "category_id": 8, "iscrowd": 0,
            "bboxes": [[30, 30, 10, 10], None, None],
            "segmentations": [[poly(30, 30)], None, None],
        },
    ],
    "categories": [{"id": 1, "name": "person"}, {"id": 8, "name": "dog"}],
}
json_file = os.path.join(root, "mini.json")
json.dump(db, open(json_file, "w"))

dicts = load_ytvis_json(json_file, root, dataset_name="ytvis_mini_demo")
rec = dicts[0]
print("keys:", sorted(rec.keys()))
for t, frame in enumerate(rec["annotations"]):
    print(f"frame {t}:",
          [(a["id"], a["category_id"], a["bbox"]) for a in frame])
```

**需要观察的现象 / 预期结果**（按源码推演，待本地验证）：

- `rec["annotations"]` 长度为 3（`length`）；第 0 帧有 2 条标注、第 1/2 帧各只有 1 条——轨道 2 的 `None` 缺帧被跳过而不是补空。
- 打印出的 `category_id` 是映射后的连续 id：person → 0、dog → 1（`id_map = {1:0, 8:1}`）。
- 标注 `id` 保留原始轨道 id：101 与 202（这两个数稍后会在 `gt_ids` 里再次出现）。
- 日志出现一条「Category ids ... not in [1, #categories]」告警——因为 json 类别 id 是 {1, 8} 不连续（数据集名不含 `coco`）。

#### 4.1.5 小练习与答案

**练习 1**：把轨道 1 的 `segmentations` 改成 `[[poly(5,5)], [], []]`（第 1、2 帧给空列表而不是 `None`），解析结果会怎样？
**答案**：`_segm[frame_idx]` 为空列表时同样 falsy，[L232](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis.py#L232) 的判断会跳过该帧，效果与 `None` 相同；但若整条轨道所有帧都是空列表，则它一张标注都不会产出（等效于轨道不存在）。

**练习 2**：为什么 `load_ytvis_json` 要在 `dataset_name` 非空时用 json 内容重建 `thing_classes`，而注册时（4.5）已经写了一份？
**答案**：注册时的元数据来自硬编码类目表（ ytvis.py 里的 2019/2021 表），只对官方数据正确；用户自带的 json 类目可能不同。[L155-L180](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/ytvis.py#L155-L180) 用 json 实际内容覆盖元数据，保证「模型见到的连续 id」与「json 实际类目」一致。

### 4.2 YTVISDatasetMapper（上）：采帧窗口与 clip 一致增强

#### 4.2.1 概念说明

`YTVISDatasetMapper` 的输入是 4.1 产出的「一条视频一个 dict」，输出是模型直接吃的样本：

```text
{ "image":      [Tensor(C,H,W)] * T,      # T 帧已增强的图像
  "instances":  [Instances] * T,          # 每帧一份、行对齐的 GT（4.3）
  "file_names": [str] * T,                # 实际采到的帧路径
  ... 原 record 的其余键（video_id/height/width/length） }
```

训练与推理走**同一入口、两套策略**：训练采 T 帧缓解显存与算力（u9-l2 说过注意力开销随 T 线性增长），推理取全部帧保证评估完整性。采帧之外，它还必须保证一件事：**一个 clip 内 T 帧的几何增强参数一致**，否则时空管道会被撕碎。

#### 4.2.2 核心流程

训练采帧（`sampling_frame_num = T`，`sampling_frame_range = R`）：

```text
ref = random.randrange(length)                    # 1. 均匀抽参考帧
窗口 = [max(0, ref-R), min(length, ref+R+1))      # 2. 参考帧前后各 R 帧
池  = 窗口中除 ref 外的所有帧索引
selected = np.random.choice(池, T-1)              # 3. 有放回抽 T-1 帧 ★
selected = sorted(selected + [ref])               # 4. 补上 ref 并恢复时间顺序
若 sampling_frame_shuffle: random.shuffle(selected)  # 5. 可选打乱帧序
```

推理采帧：`selected_idx = range(video_length)`，全帧。

逐帧处理循环：

```text
for frame_idx in selected_idx:
    读图 → check_image_size（须等于视频级 height/width）
    augmentations(image) → 得到本帧 transforms 与增强后图像
    image 转 CHW Tensor 入列表
    （仅训练）标注 deepcopy → transform_instanceAnnotations → 槽位对齐（4.3）
```

#### 4.2.3 源码精读

- [dataset_mapper.py:L120-L153](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L120-L153)：构造器保存五个采样相关参数：`sampling_frame_num`（默认 2）、`sampling_frame_range`（默认 5，注意配置层默认是 20，见 [config.py:L10](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/config.py#L10)）、`sampling_frame_shuffle`、`num_classes`（来自 `SEM_SEG_HEAD.NUM_CLASSES`，YTVIS 为 40）。顺带一提：`use_instance_mask` 只被存起来，`__call__` 里从未读它——又一个「存而不用」的旋钮。
- [dataset_mapper.py:L155-L174](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L155-L174)：`from_config` 把 `INPUT.SAMPLING_FRAME_NUM/RANGE/SHUFFLE` 三个键（u9-l1 的三个视频数据配置中的三个）读进来；增强列表由 `build_augmentation` 组装。
- [dataset_mapper.py:L187-L199](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L187-L199)：采帧主逻辑，对应上面伪代码的 1-4 步。两个值得注意的细节：**其一**，[L194-L197](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L194-L197) 的 `np.random.choice` 没写 `replace=False`，是**有放回采样**——T=2 时只抽 1 帧无所谓，T>2 时同一帧可能在一个 clip 里出现两次（IFC 移植以来如此，算历史行为而非刻意设计）。**其二**，参考帧一定入选，保证 clip 不会全部落在远离标注密集区的位置。
- [dataset_mapper.py:L200-L203](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L200-L203)：默认 `sorted` 恢复时间先后（时空管道的方向性对监督有意义）；开 `sampling_frame_shuffle` 则打乱——等价于把视频当无序帧集合训练，官方配置未开启。
- [dataset_mapper.py:L205-L206](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L205-L206)：`pop("annotations")` 与 `pop("file_names")`——从 dataset dict 里摘出来逐帧消费，避免它们残留在输出里被 DataLoader pickle。
- [dataset_mapper.py:L216-L234](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L216-L234)：逐帧读图。[L224](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L224) 的 `check_image_size` 要求每帧实尺寸等于视频级 `width/height`——**YTVIS 假设一段视频内分辨率恒定**。[L234](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L234) 转成 CHW 连续 Tensor（注释解释了 DataLoader 对 Tensor 走共享内存、对大杂烩走 pickle 的效率差异，与图像版 mapper 同款注释）。
- [dataset_mapper.py:L236-L237](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L236-L237)：`if (video_annos is None) or (not self.is_train): continue`——推理分支到此为止，只装图片不装标注；所以测试样本没有 `instances` 键。
- clip 一致增强的实现不在本文件，而在 [augmentation.py:L48-L58](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/augmentation.py#L48-L58) 与 [L98-L102](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/augmentation.py#L98-L102)：改造版 `ResizeShortestEdge`/`RandomFlip` 各带一个自增计数器 `_cnt`，**每 `clip_frame_cnt` 次调用才重新抽一次随机参数**，期间复用上次的 `self.size` / `self.do`。mapper 的 `augmentations(aug_input)` 每帧调用一次，于是同一 clip 的 T 帧自动共享同一缩放尺度与同一翻转决定。
- [augmentation.py:L127-L135](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/augmentation.py#L127-L135)：`clip_frame_cnt` 的取值由配置驱动——`MIN_SIZE_TRAIN_SAMPLING` 含 `by_clip` 时缩放按 clip 共享，`RANDOM_FLIP == "flip_by_clip"` 时翻转按 clip 共享。YTVIS 基线配置 [Base-YouTubeVIS-VideoInstanceSegmentation.yaml:L37-L42](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/youtubevis_2019/Base-YouTubeVIS-VideoInstanceSegmentation.yaml#L37-L42) 正是 `choice_by_clip` + `flip_by_clip`，两者都按 clip 共享。
- 一个值得推演的边界情况：`_cnt` 计数器挂在 mapper 实例上、跨视频持续累计，clip 对齐依赖「每次 `__call__` 恰好消费 T 次增强调用」。训练分支成立（循环 T 帧）；若某天有人让训练分支每帧调用两次增强，对齐就会错位——这是隐藏的时序耦合，改动时需要小心。

#### 4.2.4 代码实践

**实践目标**：不依赖图片数据，单独验证「采帧窗口」这一段纯逻辑，直观感受 `RANGE`/`NUM`/`SHUFFLE` 三个参数的行为。

**操作步骤**（示例代码，把采帧段抄成独立函数并做统计）：

```python
# 示例代码：离线复现 __call__ 的采帧逻辑并统计分布
import random
import numpy as np

def sample_clip(video_length, T=2, R=5, shuffle=False):
    ref = random.randrange(video_length)
    start, end = max(0, ref - R), min(video_length, ref + R + 1)
    pool = np.array(list(range(start, ref)) + list(range(ref + 1, end)))
    idx = np.random.choice(pool, T - 1).tolist() + [ref]
    idx = sorted(idx)
    if shuffle:
        random.shuffle(idx)
    return idx

random.seed(0); np.random.seed(0)
length = 36                          # YTVIS 常见量级
print([sample_clip(length) for _ in range(5)])
# 换参数观察：
#   T=5 时可能看到重复帧索引（有放回采样的直接证据）
#   R=1 时窗口收窄到 3 帧可选
from collections import Counter
cnt = Counter(f for _ in range(2000) for f in sample_clip(length, T=5, R=2))
print("参考帧附近帧被采中的频次分布（应集中在中间若干帧）:", sorted(cnt.items())[:8])
```

**需要观察的现象 / 预期结果**（待本地验证）：

- 每次输出长度恰为 T、且默认按时间升序；`shuffle=True` 后顺序随机。
- `T=5` 的多次运行中，偶见同一帧索引在一个 clip 里出现两次（有放回）。
- 帧频次直方图呈「中间高两端低」：越靠近随机参考帧越可能落入窗口。

#### 4.2.5 小练习与答案

**练习 1**：若 `video_length=1` 且 `T=2`，采帧代码会发生什么？
**答案**：参考帧只能是 0，候选池（除 ref 外的窗口帧）为空列表，`np.random.choice` 对空数组抽 1 个元素会抛 `ValueError`。YTVIS 视频至少数帧，实际不触发；但自己接短视频数据时要注意。

**练习 2**：为什么「clip 内增强参数一致」只对**几何**增强（缩放/翻转）是硬约束，光度增强（亮度/对比度）则无所谓？
**答案**：几何增强同时作用于图像与 GT mask（`transform_instance_annotations` 用同一 `transforms` 变换两者），若帧间不一致，同一轨道的 mask 序列不再处于同一坐标系，时空管道被破坏；光度增强不改变坐标，帧间独立只会造成光照抖动，不破坏关联（`build_augmentation` 里 `T.RandomBrightness` 等也没有 clip 分组，见 [augmentation.py:L147-L159](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/augmentation.py#L147-L159)）。

### 4.3 YTVISDatasetMapper（下）：跨帧实例对齐与 gt_ids

#### 4.3.1 概念说明

这是本讲的核心模块。一个问题：第 3 帧里有 A、B 两条轨道，第 7 帧里只有 A（B 离开了画面）——两帧的 `Instances` 行数不同、顺序也不定，怎么堆成 `(N, T, H, W)` 的监督管道？答案是**槽位表 + dummy 填充**：

1. 扫一遍**选中的 T 帧**，收集出现过的所有轨道 id，建 `ids = {轨道id: 槽位}`；
2. 每帧先造 N 个 dummy 标注占满所有槽位，再把该帧真实出现的轨道写进对应槽位；
3. 于是**每帧 `Instances` 恒为 N 行，第 i 行在所有帧里都是同一条轨道**；缺席帧该行保持 dummy（mask 全空、id=-1）；
4. `filter_empty_instances` 对空 mask 行**不删行、只把 gt_ids 置 -1**，维持对齐。

`gt_ids` 因此有两重身份：对 mapper 是「行对齐后的轨道身份牌」，对 `prepare_targets` 是「这一帧该轨道是否在场」的掩码（-1 = 不在场）。

#### 4.3.2 核心流程

```text
（训练分支，每帧）
_ids = ∪ 选中帧的所有轨道 id               # 跨帧并集
ids  = {轨道id: 槽位 0..N-1}               # 槽位表
sorted_annos = [dummy] * N                 # 先占位
for anno in 本帧有效标注（iscrowd==0）:
    sorted_annos[ids[anno["id"]]] = anno   # 按槽位回填
_gt_ids = [各槽位当前标注的 id]            # dummy 槽位即 -1

instances = utils.annotations_to_instances(sorted_annos, shape, "bitmask")
instances.gt_ids = tensor(_gt_ids)         # ★ 手动挂 gt_ids（原始 json 轨道 id）
instances.gt_boxes = gt_masks.get_bounding_boxes()   # 框由 mask 重算
instances = filter_empty_instances(instances)        # 空 mask 行 → gt_ids=-1
```

消费端（`VideoMaskFormer.prepare_targets`）：

```text
gt_ids 堆叠成 (N, T)；valid = (gt_ids != -1).any(帧维)   # 整个 clip 都缺席的轨道丢弃
masks 堆叠成 (N, T, H_pad, W_pad)；labels 取 gt_classes[valid]
```

#### 4.3.3 源码精读

- [dataset_mapper.py:L208-L214](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L208-L214)：`_ids = set()` 收集**选中帧**（而非全视频）出现过的轨道 id——所以 N 由采样结果决定，同一个视频两次采帧的 N 可以不同（综合实践会亲眼看到）。`ids` 字典把任意轨道 id 映射为 `0..N-1` 槽位；遍历顺序来自 set，不可控但对齐只需要「本次调用内一致」。
- [dataset_mapper.py:L57-L65](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L57-L65)：`_get_dummy_anno` 的四个字段各有用意：`id=-1`（缺席标记，最终进 `gt_ids`）；`category_id=num_classes`（即 40，**越界值**——落在类别 logits 的 no-object 通道上，避免误标成真实类）；`bbox=[0,0,0,0]`（零框）；`segmentation=[np.array([0.0]*6)]`（三个重合原点构成的退化多边形，栅格化后是空 mask）。
- [dataset_mapper.py:L239-L252](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L239-L252)：每帧先 deepcopy 标注（注释说明是防增强改到共享对象），再 `transform_instance_annotations` 应用本帧几何变换（bbox 与 polygon 同步变换，iscrowd 的标注被 [L251](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L251) 过滤）。
- [dataset_mapper.py:L253-L258](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L253-L258)：dummy 占位与按槽位回填；`_gt_ids` 在回填**之后**取自 `sorted_annos`——真实槽位拿到原始轨道 id，dummy 槽位保持 -1。
- [dataset_mapper.py:L260-L264](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L260-L264)：注意这条**主流程真实路径**：用的是 detectron2 的 `utils.annotations_to_instances(..., mask_format="bitmask")`（生成 `gt_boxes/gt_classes/gt_masks`），然后 [L261](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L261) **手动**把 `gt_ids` 挂上去；[L263](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L263) 用 mask 的外接框覆盖变换得到的框（mask 是更权威的几何来源）；[L264](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L264) 过滤空实例。顺序敏感：`gt_ids` 必须在 `filter_empty_instances` 之前挂上，否则后者改 `instances.gt_ids` 会抛 `AttributeError`。
- [dataset_mapper.py:L27-L54](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L27-L54)：`filter_empty_instances` 的「过滤」是**名不副实**的——它不删任何行，只把空 box/空 mask 行的 `gt_ids` 置 -1（[L53](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L53)）。这与图像版 mapper「真删行」的语义相反，目的就是保住跨帧行对齐。
- [dataset_mapper.py:L265-L266](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L265-L266)：无 mask 时的兜底（空 BitMasks），保证 `instances` 字段形状契约不破。
- [video_maskformer_model.py:L227-L253](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L227-L253)：消费端 `prepare_targets`。[L240-L241](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L240-L241) 逐帧取 `gt_ids` 与 `gt_masks.tensor` 按**行索引**写进 `(N, T, H, W)` 缓冲——这就是行对齐的回报：不需要任何匹配逻辑，直接按下标堆叠。[L244](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L244) `valid_idx = (gt_ids != -1).any(dim=-1)` 丢掉**整段 clip 都缺席**的轨道（例如其唯一出现帧恰好被 iscrowd 过滤）。产出的 `{"labels", "ids", "masks"}` 里，u9-l4 的视频匹配器/损失只消费 `labels` 和 `masks`——`ids` 在 prepare_targets 内部完成使命后随 dict 带走但不参与损失。
- [video_maskformer_model.py:L246](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L246)：一个值得注意的细节——`gt_classes_per_video` 取自循环结束后残留的 `targets_per_frame`，即**最后一帧**。若某轨道在最后一帧缺席（dummy 行），它的 label 会是越界值 40（正好落在 no-object 通道，`loss_labels` 不会崩，但该轨道的分类监督被「让给」no-object）。YTVIS 轨道大多贯穿采样帧，此边角在官方数据上罕见，读码时知道即可。
- **考据：`ytvis_annotations_to_instances` 在主流程中无人调用。** [dataset_mapper.py:L68-L111](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L68-L111) 定义的这个函数能一步从「标注列表」造出含 `gt_boxes/gt_classes/gt_ids/gt_masks` 的 `Instances`（[L84-L94](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L84-L94) 依次装三个字段），但全仓库检索它只有定义没有调用点——`__call__` 用的是「`utils.annotations_to_instances` + 手动挂 `gt_ids`」的组合。它要求 `segmentation` 已经是**2D ndarray 位掩码**（[L100-L101](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L100-L101) 断言 `segm.ndim == 2`），而主流程标注里是 polygon/RLE，需要 `annotations_to_instances` 的栅格化能力——这大概率就是它被旁置的原因。它仍是理解「模型要什么样的 Instances」最短的可运行切片，4.3.4 直接用它做实验。

#### 4.3.4 代码实践

**实践目标**：构造 2 帧、2 个实例的假视频标注，调用 `ytvis_annotations_to_instances`，逐字段核对 `gt_classes / gt_ids / gt_masks` 的内容与形状（这是规格书指定的实践）。

**操作步骤**（示例代码，需 detectron2 环境）：

```python
# 示例代码：用假标注直接驱动 ytvis_annotations_to_instances
import numpy as np, torch
from mask2former_video.data_video.dataset_mapper import (
    ytvis_annotations_to_instances, _get_dummy_anno,
)

H, W = 48, 64
def mask_a():                      # 轨道 1：左上角 12x12
    m = np.zeros((H, W), dtype=bool); m[5:17, 5:17] = True;  return m
def mask_b():                      # 轨道 2：右下角 10x10
    m = np.zeros((H, W), dtype=bool); m[30:40, 30:40] = True; return m

frame0 = [  # 两轨道都在场（category_id 已是连续 id；id 为 json 轨道 id）
    {"bbox": [5,5,12,12], "bbox_mode": 0, "category_id": 0, "id": 101, "segmentation": mask_a()},
    {"bbox": [30,30,10,10], "bbox_mode": 0, "category_id": 1, "id": 202, "segmentation": mask_b()},
]
frame1 = [  # 轨道 1 在场；轨道 2 用 dummy 占位
    {"bbox": [7,6,12,12], "bbox_mode": 0, "category_id": 0, "id": 101, "segmentation": mask_a()},
    _get_dummy_anno(num_classes=40),
]

for name, annos in [("frame0", frame0), ("frame1", frame1)]:
    inst = ytvis_annotations_to_instances(annos, (H, W))
    print(name,
          "gt_classes =", inst.gt_classes.tolist(),
          "| gt_ids =", inst.gt_ids.tolist(),
          "| masks shape =", tuple(inst.gt_masks.tensor.shape),
          "| mask 非零像素 =", inst.gt_masks.tensor.sum(dim=(1,2)).tolist())
```

**需要观察的现象 / 预期结果**（按源码推演，待本地验证）：

- `frame0`：`gt_classes = [0, 1]`，`gt_ids = [101, 202]`（**原始 json 轨道 id 原样保留**），mask 形状 `(2, 48, 64)`，非零像素 `[144, 100]`。
- `frame1`：`gt_classes = [0, 40]`（dummy 的越界类别），`gt_ids = [101, -1]`（dummy 的缺席标记），mask 形状仍为 `(2, 48, 64)`、第二行全零——**行数与行序和 frame0 对齐**，这正是消费端能按行堆叠的原因。
- `gt_boxes` 自动由 bbox 转成 XYXY（[L84](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L84)），dummy 行是 `[0,0,0,0]`。

**注意**：`bbox_mode` 此处直接用了枚举值 `0`（`BoxMode.XYXY_ABS`）；示例刻意直接给 2D 位掩码来满足该函数的断言——若把 polygon 列表传进去会在 [L100](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L100) 处断言失败，这本身就是「该函数为何不在主流程」的最好证明。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `filter_empty_instances` 改成真的删除空行（像图像版那样），训练会在哪一步坏掉？
**答案**：不会立刻在 mapper 里坏，而是在 `prepare_targets` 的 [video_maskformer_model.py:L231-L233](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L231-L233)——`_num_instance` 取第 0 帧行数开辟 `(N,T,H,W)` 缓冲，而后面某帧行数变少/错位，`gt_masks_per_video[:, f_i] = ...` 会因形状不匹配报错，或更糟：静默地把 A 轨道的 mask 写到 B 轨道的行上。

**练习 2**：为什么 dummy 的 `category_id` 要取 `num_classes`（40）而不是 0？
**答案**：0 是真实类别（person）。取 40 使 dummy 行的类别落在 `pred_logits` 的第 41 个通道——恰好是 no-object 通道。这样即便 dummy 行的类别被下游读到（见 4.3.3 的「最后一帧 label」细节），语义也是「此帧无物体」，不会把错误类别灌进交叉熵。

**练习 3**：`prepare_targets` 已经用 `valid_idx` 丢掉了全缺席轨道，为什么 mapper 不干脆别把「只在未选中帧出现」的轨道放进槽位表？
**答案**：它确实这么做了——[L208-L211](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L208-L211) 只扫描**选中的帧**。`valid_idx` 兜的是另一种情况：轨道进了槽位表，但它的有效标注全被 `iscrowd` 过滤或几何裁剪清空，导致每帧它都是空 mask 行——这类轨道由消费端统一清除。

### 4.4 CocoClipDatasetMapper：把一张 COCO 图当一段视频

#### 4.4.1 概念说明

`CocoClipDatasetMapper` 是 IFC 一脉的「视频预训练」技巧：没有真实视频时，**把同一张 COCO 图片重复 T 次当成一个 clip**。每帧独立过一遍增强，靠随机增强的差异制造「伪运动」，让模型先在大体量的 COCO 上学会时空 query 的用法，再迁到 YTVIS 微调。

需要先亮明身份：**当前仓库里它没有任何接线**——检索 `CocoClip` 只有定义与 `__init__` 导出两处，`train_net_video.py` 的 `build_train_loader` 写死了 `YTVISDatasetMapper`（[train_net_video.py:L76-L87](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L76-L87)）。想用它得自己改 loader，因此把它当「备件 + 对照组」读：对照它和 `YTVISDatasetMapper` 的差异，能反过来看清视频 mapper 每处设计的理由。

#### 4.4.2 核心流程

```text
输入：一条 COCO 图像 dict（一条 file_name、一份标注）
for _ in range(T):
    对同一张原图独立执行一次增强          # 伪运动的来源
    标注同样过 transform（iscrowd 过滤）
    gt_ids = [0, 1, ..., N-1]            # 每帧所有实例都在场，直接顺次编号
    空 segmentation 用退化多边形修补
    annotations_to_instances(bitmask) → Instances
输出：与 YTVIS mapper 相同的 {image[T], instances[T], file_names[T]}
```

#### 4.4.3 源码精读

- [dataset_mapper.py:L307-L321](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L307-L321)：`from_config` 比 YTVIS 版少两个采样参数——没有参考帧窗口，因为「视频」只有一帧可采。
- [dataset_mapper.py:L333-L339](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L333-L339)：原图只读一次（[L335](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L335)），`file_names` 复制成 T 份（[L339](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L339)）。
- [dataset_mapper.py:L340-L351](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L340-L351)：T 次循环里每帧重新调 `self.augmentations(aug_input)`。参数是否帧间共享取决于配置：若用普通 `choice`/`horizontal`（`clip_frame_cnt=1`），每帧独立抽参 → 帧间真有差异（伪运动）；若配了 `by_clip`，T 帧会拿到同一组参数而变成完全相同的复帧。**想用它造伪运动，配置上应避开 by_clip**。
- [dataset_mapper.py:L369-L372](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L369-L372)：与 YTVIS 版最大的结构差异——没有 dummy 填充。每帧所有实例都在场（同一张图），`gt_ids` 直接 `range(N)`；增强（如裁剪）把某实例裁没了时，靠 `filter_empty_instances` 置 -1 兜底（此时帧间空 mask 行由裁剪造成，而非标注缺席）。[L371-L372](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L371-L372) 还把空 polygon 修补成退化多边形，防止栅格化报错。
- [dataset_mapper.py:L374-L381](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L374-L381)：Instances 组装与 YTVIS 版逐行相同（bitmask、mask 重算框、过滤空实例）。

#### 4.4.4 代码实践

**实践目标**：源码阅读型对照——不改代码，写出两个 mapper 在「gt_ids 来源、行数保障、增强次数」上的三点差异。

**操作步骤**：

1. 对照读 [dataset_mapper.py:L208-L214](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L208-L214) 与 [L369](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L369)。
2. 回答：YTVIS 版 gt_ids 为什么不能直接 `range(N)`？CocoClip 版为什么可以？
3. 回答：两者分别靠什么机制保证「每帧 Instances 行数一致」？

**预期结果**：YTVIS 版的轨道会缺席，id 必须取 json 原始轨道 id 才能跨帧对上号（`range(N)` 无法表达「这帧没有 3 号轨道」）；CocoClip 版同一张图重复，实例永不在场缺席，顺次编号即可。行数保障：前者靠 dummy 填充（标注层），后者靠「原图全实例每帧都在」（数据层）+ `filter_empty_instances` 只置 -1 不删行。

#### 4.4.5 小练习与答案

**练习 1**：用 CocoClip 做预训练时，同一实例在 T 帧里的 mask 完全一样（除了增强扰动），模型能从中学到什么、学不到什么？
**答案**：能学到 query 与时空管道的绑定方式、masked attention 在 `(T×HW)` 序列上的行为、损失与匹配的流程（迁移到真视频时直接可用）；学不到真实运动模式（形变、遮挡、进出画面），因为伪 clip 里没有轨道缺席与真实形变。

**练习 2**：若把 `sampling_frame_num` 设为 1，两个 mapper 的输出还有区别吗？
**答案**：结构上几乎退化为图像版 mapper 的输出（单元素列表），但字段仍带 `gt_ids`、且 `image`/`instances` 是列表——模型侧仍按「T=1 的视频」处理，这与图像版直接给单个 Tensor/Instances 不同。

### 4.5 builtin.py 注册与 build.py 视频加载器

#### 4.5.1 概念说明

数据侧最后一公里：**注册**（哪些名字 → 哪些 json/图片路径）与**加载**（怎么把 mapper 接进 DataLoader）。注册沿用 u2-l2 的「import 即注册」与双目录机制，视频版唯一的新东西是路径表；加载沿用了 detectron2 的构建骨架，但为了「一条视频一个样本」改了两处关键行为：过滤函数要认嵌套标注，测试加载器恒为 batch=1。

#### 4.5.2 核心流程

```text
import mask2former_video
  → data_video/__init__ → datasets/__init__ → from . import builtin
  → __name__.endswith(".builtin") 成立
  → root = os.getenv("DETECTRON2_DATASETS", "datasets")
  → register_all_ytvis_2019(root); register_all_ytvis_2021(root)
       各自遍历 3 个预定义子集 → register_ytvis_instances（见 4.1）

训练：Trainer.build_train_loader
  → get_detection_dataset_dicts(name, filter_empty=cfg.DATALOADER.FILTER_EMPTY_ANNOTATIONS)
  → build_detection_train_loader(cfg, mapper=YTVISDatasetMapper(cfg, True), dataset=...)
测试：Trainer.build_test_loader
  → build_detection_test_loader(cfg, name, mapper=YTVISDatasetMapper(cfg, False))
  → InferenceSampler + BatchSampler(batch=1) + trivial_batch_collator
```

#### 4.5.3 源码精读

- [builtin.py:L13-L20](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/builtin.py#L13-L20) 与 [L24-L31](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/builtin.py#L24-L31)：**命名规则** `ytvis_{2019|2021}_{train|val|test}` → `(image_root, json_file)` 二元组。三个易踩的点：① **`val` 子集的磁盘目录叫 `valid/`**（`ytvis_2019/valid/JPEGImages`），json 也叫 `valid.json`，名字与注册名不一致；② `test` 子集没有公开标注，json 里无 annotations，只能跑推理提交；③ 2021 与 2019 是**平行的两套目录**（`ytvis_2021/`），互不兼容（4.1 讲过类目不同）。
- [builtin.py:L34-L53](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/builtin.py#L34-L53)：`register_all_ytvis_2019/2021` 遍历各自路径表调 `register_ytvis_instances`；`"://" not in json_file` 的分支允许路径表直接填远程 URL（fvcore PathManager 能读），本仓库未用。
- [builtin.py:L56-L60](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/builtin.py#L56-L60)：触发条件 `if __name__.endswith(".builtin")`——包内正常 import 时模块名是 `mask2former_video.data_video.datasets.builtin`，条件成立即注册；root 取 `DETECTRON2_DATASETS` 环境变量，默认 `datasets`（与图像版一致）。
- [train_net_video.py:L76-L87](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L76-L87)：`build_train_loader` 显式取 `DATASETS.TRAIN[0]`、构造 `YTVISDatasetMapper(cfg, is_train=True)`，再把 `dataset` 与 `mapper` 一起喂给 `build_detection_train_loader`——注意它**绕过了** build.py 里 `mapper=None` 时用图像版 `DatasetMapper` 的默认路径，显式注入视频 mapper。
- [train_net_video.py:L89-L93](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L89-L93)：`build_test_loader` 同理，`is_train=False` 的 mapper 会走「全帧 + 无标注」分支（4.2）。
- [build.py:L38-L71](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/build.py#L38-L71)：`filter_images_with_only_crowd_annotations` 的 `valid` 函数里 [L54-L58](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/build.py#L54-L58) 有一个 `isinstance(ann, list)` 分支——就是为视频 dict 的二层嵌套 `[帧][实例]` 加的：视频「有效」当且仅当**至少一帧**有非 crowd 标注。不过 YTVIS 基线配置把 `DATALOADER.FILTER_EMPTY_ANNOTATIONS` 关了（[Base-YouTubeVIS-VideoInstanceSegmentation.yaml:L50-L52](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/youtubevis_2019/Base-YouTubeVIS-VideoInstanceSegmentation.yaml#L50-L52)），这条路径实际不生效——视频本就稀缺，按视频过滤太粗。
- [build.py:L74-L111](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/build.py#L74-L111)：`get_detection_dataset_dicts` 从 `DatasetCatalog` 取惰性加载结果（首次调用才解析 json）、多数据集拼接、按需过滤。视频版与图像版的差异全部被 Catalog 抽象掉了——注册进去的 loader 返回视频 dict，调用方拿到什么就是什么。
- [build.py:L142-L186](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/build.py#L142-L186)：`build_detection_train_loader` 与 detectron2 原版几乎逐行相同（DatasetFromList → MapDataset(mapper) → TrainingSampler → build_batch_data_loader），区别只在它住在 `mask2former_video` 名下、由视频 Trainer 显式传 mapper。**批的单位是「视频」**：`IMS_PER_BATCH=16` 指 16 个 clip（每个 clip 含 T 帧），实际进 backbone 的图像数是 16×T。
- [build.py:L240-L249](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/build.py#L240-L249)：测试加载器三件套 `InferenceSampler`（分布式不重不漏）+ `BatchSampler(sampler, 1)`（**每个 batch 恰一段完整视频**，帧数不一没法拼批）+ `trivial_batch_collator`（不做默认堆叠，原样透传 dict）。这是 u9-l1「视频版必须覆写 build_test_loader」结论的落地处。

#### 4.5.4 代码实践

**实践目标**：dry-run 验证「import 即注册」并核对 6 个子集的命名/路径/元数据（规格书指定的第二项实践）。

**操作步骤**（示例代码，不需要下载数据集——注册只写字典不碰磁盘）：

```python
# 示例代码：观察 YouTubeVIS 的注册结果
import mask2former_video                       # 触发注册
from detectron2.data import DatasetCatalog, MetadataCatalog

names = sorted(n for n in DatasetCatalog.list() if n.startswith("ytvis_"))
print("注册的 YTVIS 子集:", names)              # 预期 6 个
for n in names:
    meta = MetadataCatalog.get(n)
    print(f"{n}: evaluator_type={meta.evaluator_type}, "
          f"#thing_classes={len(meta.thing_classes)}, "
          f"json={meta.json_file}, images={meta.image_root}")
```

**需要观察的现象 / 预期结果**（待本地验证）：

- 打印出 `ytvis_2019_test / ytvis_2019_train / ytvis_2019_val / ytvis_2021_test / ytvis_2021_train / ytvis_2021_val` 共 6 个名字。
- 每个子集 `evaluator_type="ytvis"`、`#thing_classes=40`；`ytvis_2019_val` 的 `json` 指向 `.../ytvis_2019/valid.json`、`images` 指向 `.../ytvis_2019/valid/JPEGImages`——**注册名 `val` 与目录名 `valid` 的错位**亲眼可见。
- 若设置 `DETECTRON2_DATASETS=/some/path` 再跑，路径前缀随之改变。

**摘录与命名规则说明**（对应规格书要求）：2019/2021 的注册代码分别位于 [builtin.py:L34-L42](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/builtin.py#L34-L42) 与 [L45-L53](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/builtin.py#L45-L53)，二者除类目元数据（`_get_ytvis_2019_instances_meta` vs `_get_ytvis_2021_instances_meta`）与路径表外完全对称。train/val/test 子集命名规则：`ytvis_{年份}_{split}`，split ∈ {train, val, test}；**split 名沿用 COCO 习惯（val 而非 valid）**，但磁盘上的目录与 json 文件名用官方发布的 `valid/`、`valid.json`——查数据问题时务必按 `valid` 找目录、按 `val` 写配置。

#### 4.5.5 小练习与答案

**练习 1**：为什么测试加载器必须 batch=1，而训练可以做多视频一批？
**答案**：训练的 mapper 输出恒定 T 帧、且 `build_batch_data_loader` 只把样本放进列表（collate 是 trivial 的），帧尺寸差异留给模型的 `ImageList` padding；推理时一个样本=整段视频（帧数 = 视频长度，各不相同），任何张量级拼接都不可行，batch=1 + 原样透传是唯一可行方案。

**练习 2**：`DatasetCatalog.get("ytvis_2019_train")` 第一次与第二次调用，行为有何不同？
**答案**：第一次真正执行 `load_ytvis_json`（读 json、建索引，可能数秒）并把结果**缓存**在 Catalog 里；第二次直接返回缓存列表（detectron2 的 Catalog 语义）。这也是 u2-l2「注册不碰磁盘、取数才读盘」在视频版的体现。

## 5. 综合实践

把本讲三个模块串成一条端到端链路：**手写迷你 YTVIS json → 注册加载 → mapper 采帧对齐 → 检查 gt_ids 的跨帧关联**。全程只需 detectron2 环境，不需要下载 YouTubeVIS（图片用两张随机噪声图伪造）。

**任务**：造一段 3 帧、2 条轨道的视频（轨道 2 只在第 0 帧在场），跑通 `YTVISDatasetMapper.__call__`，验证「同帧数、行对齐、缺席轨道 -1」三件事，并回答：当采样窗口没采到第 0 帧时，输出有什么变化、为什么？

**操作步骤**（示例代码）：

```python
# 示例代码：端到端跑通视频数据管线（无 GPU、无真实数据集）
import json, os, random, tempfile
import numpy as np, torch
from PIL import Image
from detectron2.data import transforms as T
from mask2former_video.data_video.datasets.ytvis import load_ytvis_json
from mask2former_video.data_video.dataset_mapper import YTVISDatasetMapper

root = tempfile.mkdtemp()
frames = os.path.join(root, "v1"); os.makedirs(frames)
for i in range(3):   # 3 帧噪声图（48x64，与 json 的 height/width 一致）
    Image.fromarray(np.random.randint(0, 255, (48, 64, 3), dtype=np.uint8)).save(
        os.path.join(frames, f"{i:05d}.jpg"))

def poly(x, y, s=12): return [x, y, x+s, y, x+s, y+s, x, y+s]
db = {
    "videos": [{"id": 1, "length": 3, "height": 48, "width": 64,
                "file_names": ["v1/00000.jpg", "v1/00001.jpg", "v1/00002.jpg"]}],
    "annotations": [
        {"video_id": 1, "id": 101, "category_id": 1, "iscrowd": 0,
         "bboxes": [[5,5,12,12]]*3, "segmentations": [[poly(5,5)]]*3},
        {"video_id": 1, "id": 202, "category_id": 8, "iscrowd": 0,
         "bboxes": [[30,30,10,10], None, None],
         "segmentations": [[poly(30,30)], None, None]},
    ],
    "categories": [{"id": 1, "name": "person"}, {"id": 8, "name": "dog"}],
}
json_file = os.path.join(root, "mini.json"); json.dump(db, open(json_file, "w"))
video_dict = load_ytvis_json(json_file, root, dataset_name="ytvis_mini_demo")[0]

mapper = YTVISDatasetMapper(           # 直接用关键字构造，绕开 cfg（@configurable 允许）
    is_train=True, augmentations=[T.ResizeShortestEdge(360, 640)],
    image_format="RGB", sampling_frame_num=2, sampling_frame_range=5,
    sampling_frame_shuffle=False, num_classes=40,
)

for seed in range(6):                  # 多跑几次覆盖不同采样结果
    random.seed(seed); np.random.seed(seed)
    out = mapper(video_dict)
    print(f"--- seed {seed}: 采到帧 {os.path.basename(out['file_names'][0])}, "
          f"{os.path.basename(out['file_names'][1])}, 图像形状 {tuple(out['image'][0].shape)}")
    for f, inst in enumerate(out["instances"]):
        print(f"  frame{f}: gt_ids={inst.gt_ids.tolist()} "
              f"gt_classes={inst.gt_classes.tolist()} "
              f"mask形状={tuple(inst.gt_masks.tensor.shape)} "
              f"每行非零像素={inst.gt_masks.tensor.sum(dim=(1,2)).tolist()}")
```

**需要观察的现象 / 预期结果**（按源码推演，待本地验证）：

1. `out["image"]` 长度 2，每帧 `(3, 720, 960)` 左右（ResizeShortestEdge 把 48 提到 360，长边按 64/48 比例放大）。两帧 shape 完全相同——本例里这是「平凡成立」的（同一张图、固定目标尺寸）；换成随机尺寸范围或加上翻转时，就必须靠仓库改造版增强的 `clip_frame_cnt` 计数器（4.2.3）来强制帧间参数一致了。
2. **采到第 0 帧**的 seed：每帧 `Instances` 都是 2 行；第 0 帧 `gt_ids=[101, 202]`（顺序由 set 遍历决定，也可能是 `[202, 101]`，但**两帧的行序一致**）；另一帧轨道 202 所在行为 dummy——`gt_id=-1`、`gt_class=40`、mask 全零。
3. **没采到第 0 帧**的 seed：`_ids` 只含轨道 101，每帧只有 1 行，`gt_ids=[101]`——轨道集合由**采样到的帧**决定，这正是 4.3 练习 3 的结论落地。
4. `gt_ids` 里的 101/202 是 json 原始轨道 id，从未被映射成 0/1。

**验收标准**：能口头解释观察点 2 与 3 的差异来源（`_ids` 的收集范围），并指出 dummy 行三个字段的取值（-1 / 40 / 全零 mask）分别由哪行代码决定（[L57-L65](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L57-L65) 与 [L53](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/dataset_mapper.py#L53)）。

## 6. 本讲小结

- `load_ytvis_json` 把「一条视频一条记录、一条标注一条轨道」的 YTVIS json 解析成 detectron2 视频格式：标注按 `[帧][实例]` 二层嵌套，某帧轨道缺席直接跳过，类别 id 映射为连续 id 但**轨道 id 原样保留**。
- `YTVISDatasetMapper` 训练时「均匀抽参考帧 + 窗口内有放回补 T-1 帧 + 排序」，推理时全帧输出；clip 一致增强靠 `clip_frame_cnt` 计数器让 T 帧共享同一组几何变换参数。
- 跨帧对齐三件套：`_ids` 槽位表（N = 选中帧出现过的轨道数）、`_get_dummy_anno` 占位（id=-1 / 类别=40 / 空 mask）、`filter_empty_instances` 只置 -1 不删行——保住「每帧 N 行、第 i 行同轨道」，`prepare_targets` 才能按行堆出 `(N, T, H, W)` 管道。
- `gt_ids` 的生命周期：mapper 写入原始轨道 id → 空行被置 -1 → `prepare_targets` 用 `any(id != -1)` 丢弃整段缺席的轨道；匹配与损失实际只消费 `labels` 与 `masks`。
- `ytvis_annotations_to_instances` 与 `CocoClipDatasetMapper` 是 IFC 遗产件，当前主流程均未调用（前者要求输入已是 2D 位掩码，后者未接线任何配置）；builtin 注册遵循「import 即注册」，`ytvis_{2019|2021}_{train|val|test}` 六个子集中 **val 的磁盘目录叫 `valid/`**；测试加载器恒 batch=1（一段视频一个 batch）。

## 7. 下一步学习建议

数据侧就位后，下一讲 **u9-l4（视频损失与 YTVIS 评估）** 讲这批 `{"labels", "ids", "masks"}` 目标如何被消费：`VideoSetCriterion` 与 `VideoHungarianMatcher` 怎么把 `(N, T, H, W)` 的管道整体送去匹配和逐点监督（对照 u5-l1/u5-l2 的图像版），以及 `YTVISEvaluator` 如何基于 `ytvis_api` 输出官方指标。读码顺序建议：先对照 [mask2former_video/modeling/matcher.py:L95-L154](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/matcher.py#L95-L154) 与图像版 matcher 找出「匹配粒度从 mask 变 tube」的那几行，再读本讲的下游 `prepare_targets`（已读）衔接。若想继续在数据侧深挖，可以读 [ytvis_eval.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/ytvis_eval.py) 看 `YTVISEvaluator` 如何反向把 `Instances` 序列化回 YTVIS json——那是本讲管线的逆过程。
