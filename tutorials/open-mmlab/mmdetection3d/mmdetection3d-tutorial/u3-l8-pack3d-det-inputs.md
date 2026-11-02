# Pack3DDetInputs：数据管线的出口

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `Pack3DDetInputs` 的 `keys` 与 `meta_keys` 两个参数各自的收集规则：`keys` 是「白名单 + 四张路由表」决定数据字段的归宿，`meta_keys` 是「根级 → images → lidar_points」三级查找决定元信息的来源。
2. 说出训练与推理（测试）阶段在打包内容上的差异：为什么 KITTI 训练打包 `['points', 'gt_bboxes_3d', 'gt_labels_3d']` 而测试只打包 `['points']`。
3. 画出打包产物 `dict(inputs=..., data_samples=...)` 的最终形态：`inputs` 里最多只有 `points` 与 `img` 两个键，`data_samples` 是一个 `Det3DDataSample`，并指出其中每个字段会被谁消费（模型 / 损失 / 评估 / 可视化）。

本讲是单元 3（数据集与数据管线）的收官：前七讲我们把「原始数据 → info pkl → 数据集类 → 加载 → 增强 → GT 采样」一路讲到了管线倒数第二个环节，本讲补上最后一环——**平铺的 data dict 如何变成模型认识的输入**。

## 2. 前置知识

**管线中间产物：一个平铺的大 dict。** u3-l5 到 u3-l7 讲过的所有 transform 都遵守同一个约定：接收一个 `results` dict，往里面**加键、改键**，再把整个 dict 传给下一个 transform。走到管线末端时，这个 dict 里同时躺着几十个键——`points`（点云对象）、`gt_bboxes_3d`（3D 框对象）、`gt_labels_3d`（numpy 标签数组）、`pcd_rotation`（增强矩阵）、`images`（v2 格式的相机信息树）……这种「平铺」结构对增强类很友好（它们要随机读写任意键），但对模型很不友好——模型的 `forward` 不可能接受一个形状不定的 dict。

**模型侧的契约：inputs + data_samples。** u2-l4 已经详细讲过 `Det3DDataSample` 这份契约：metainfo 分区 + data fields 分区，实例级数据装 `InstanceData`，点级数据装 `PointData`。MMEngine 的训练循环要求 DataLoader 每产出一个样本都形如：

```python
{'inputs': ..., 'data_samples': ...}
```

本讲要讲的就是**翻译官** `Pack3DDetInputs`：把平铺 dict 里散落的几十个键，按路由规则搬进这个两键结构。

**为什么必须在这里转 torch.Tensor。** 管线运行在 DataLoader 的 worker 进程里，数据以 numpy 为主；一旦离开管线进入模型，就要上 GPU、参与自动求导。MMEngine 默认使用 `pseudo_collate`（按样本原样拼成 list，不做张量堆叠），因此**张量化必须发生在打包时**，collate 只负责搬运。这也是 `to_tensor` 辅助函数存在的理由。

**与前两讲的分工。** u2-l4 讲「契约本身长什么样」（字段声明、类型断言），本讲讲「契约怎么被生产出来」（路由规则、张量化、meta 查找）；u3-l5 讲「加载类往 results 里写了什么」，本讲讲「这些键最后被谁收走」。如果对 `BasePoints`、`BaseInstance3DBoxes` 的对象形态不熟悉，建议先回顾 u2-l2、u2-l3。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `mmdet3d/datasets/transforms/formating.py` | **本讲主角**。`to_tensor` 辅助函数 + `Pack3DDetInputs`（四张路由表、meta 三级查找、张量化） |
| `mmdet3d/structures/det3d_data_sample.py` | 契约本体：属性三件套（getter/setter/deleter）与类型断言，u2-l4 已精读，本讲只看它被填充的方式 |
| `configs/_base_/datasets/kitti-3d-3class.py` | LiDAR 检测的 keys 配置实例（训练 / 测试 / eval 三条管线） |
| `configs/_base_/datasets/scannet-3d.py` | 室内检测的 keys 配置实例（多打包两个点级 mask） |
| `configs/_base_/datasets/nus-mono3d.py` | 单目 3D 的 keys 配置实例（打包 img 与 2D 标注） |
| `mmdet3d/datasets/det3d_dataset.py` | `parse_data_info` 把 `lidar_path`/`num_pts_feats` 提升到根级——meta 一级查找能命中的原因 |
| `mmdet3d/models/data_preprocessors/data_preprocessor.py` | 打包产物的第一站消费者：collate、图像堆叠、可选体素化、追加 metainfo |
| `mmdet3d/models/detectors/base.py` / `single_stage.py` | 第二站消费者：`forward` 三模式分发、`loss` 读 gt、`add_pred_to_datasample` 写预测 |
| `mmdet3d/evaluation/metrics/indoor_metric.py` | `eval_ann_info` 的真实消费者（室内指标） |
| `mmdet3d/testing/data_utils.py` | 官方假数据构造器 `create_data_info_after_loading`，实践脚本的起点 |
| `tests/test_datasets/test_transforms/test_formating.py` | `Pack3DDetInputs` 的单元测试，实践结果的对答案依据 |

## 4. 核心概念与源码讲解

### 4.1 管线出口的定位：从平铺 dict 到两键契约

#### 4.1.1 概念说明

`Pack3DDetInputs` 是一个注册在 `TRANSFORMS` 上的 `BaseTransform`（来自 MMCV），它必须放在管线的**最后一个**位置。原因很简单：它会把 `points` 从 `BasePoints` 对象剥成裸张量、把几十个键搬进 `Det3DDataSample`，之后再想读 `results['gt_bboxes_3d']` 这种平铺键的增强类就拿不到东西了。所以管线的排序惯例是「加载 → 内容增改 → 几何增强 → 范围过滤 → 打包」，u3-l6 讲过的「过滤必须在增强之后」再加上本讲的「打包必须在一切之后」，合起来就是完整的排序约束。

它解决的问题是：**把「为数据增强优化的平铺结构」翻译成「为模型与 collate 优化的契约结构」**，同时顺手完成 numpy → torch 的类型归一。

#### 4.1.2 核心流程

```text
dataset[idx]
  └─ pipeline = [Load…, ObjectSample, RandomFlip3D, GlobalRotScaleTrans,
                 …RangeFilter, PointShuffle, Pack3DDetInputs]
                        │
                        ▼  平铺 results dict（约 20~40 个键）
        Pack3DDetInputs.transform(results)
        ├── results 是 list？ → 测试时增强（augtest）：
        │       list 里每个 dict 单独打包，返回 list[packed]     ← u7-l4 展开
        ├── results 是 dict → pack_single_results(results)
        │     ① 张量化：points 剥壳、img HWC→CHW、标签转 Tensor
        │     ② 收集 metainfo：按 meta_keys 三级查找 → set_metainfo
        │     ③ 路由数据键：keys × 四张表 → inputs / gt_instances_3d /
        │        gt_instances / gt_pts_seg
        │     ④ 特例：eval_ann_info 直接挂到 data_sample
        └── 返回 dict(inputs=…, data_samples=Det3DDataSample)
```

返回的 dict **只有两个键**：`inputs` 与 `data_samples`。这就是 DataLoader 交给 collate 的最终形态，也是 4.5 节消费者地图的起点。

#### 4.1.3 源码精读

**类注册与两个构造参数**。[mmdet3d/datasets/transforms/formating.py:L48-L82](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L48-L82) 定义类并把 `keys` 与 `meta_keys` 存为实例属性。注意 `keys` 是**必填**位置参数（没有默认值），`meta_keys` 则给了一份多达 36 个键的默认清单（L67-L79），覆盖了图像路径、各类标定矩阵（`lidar2img`/`cam2img`/`lidar2cam`…）、增强审计键（`pcd_rotation`/`pcd_scale_factor`/`transformation_3d_flow`…，u3-l6 讲过它们的用途）、框类型（`box_type_3d`/`box_mode_3d`）与样本身份（`sample_idx`/`lidar_path`）。默认清单长，是因为它要同时伺候 LiDAR / 单目 / 多模态 / 室内所有场景；找不到的键会被静默跳过，不会报错。

**入口的三路分发**。[mmdet3d/datasets/transforms/formating.py:L89-L122](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L89-L122) 是 `transform` 方法：入参是 list 时进入 augtest 分支——只有一个元素就当普通样本打包，多个元素（来自 `MultiScaleFlipAug3D` 产出的多增强副本）就逐个打包成 list 返回，这正是 u7-l4 测试时增强「一个样本 × N 种增强」的数据基础；入参是 dict 时走正常训练 / 简单测试路径，转调 `pack_single_results`；其余类型直接 `NotImplementedError`。

**收尾的两键返回**。[mmdet3d/datasets/transforms/formating.py:L251-L255](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L251-L255) 构造并返回 `packed_results`：先放 `data_samples` 再放 `inputs`。注意 `inputs` 在这里还是**单样本视角**的 dict（键可能是 `points` 和/或 `img`），到 4.5 节的预处理器里才会变成批量视角（`imgs` 张量、可选 `voxels`）。

#### 4.1.4 代码实践

**实践目标**：直观感受「打包前几十个键、打包后两个键」的落差，确认 `Pack3DDetInputs` 是管线出口。

**操作步骤**：

1. 在仓库根目录新建 `pack_probe.py`（自写脚本，可放在仓库外或临时目录，不要提交）：

```python
# 示例代码：观察打包前后的结构落差
from mmdet3d.datasets.transforms.formating import Pack3DDetInputs
from mmdet3d.testing import create_data_info_after_loading

results = create_data_info_after_loading()   # 官方假数据：已完成点云与标注加载
print('打包前的键数量:', len(results))
print(sorted(results.keys()))

pack = Pack3DDetInputs(keys=['points', 'gt_labels_3d', 'gt_bboxes_3d'])
packed = pack(results)
print('打包后的键:', list(packed.keys()))
print('inputs 的键:', list(packed['inputs'].keys()))
print(packed['data_samples'])
```

2. 运行 `python pack_probe.py`（需要已按 u1-l2 装好环境；假数据引用的 `tests/data/kitti/training/velodyne_reduced/000000.bin` 仓库自带）。

**需要观察的现象**：打包前 results 约 15~20 个键（含 `images`、`lidar_points`、`instances` 等大树）；打包后只剩 `inputs`、`data_samples` 两个键；打印 `data_samples` 时会看到 `META INFORMATION` 与 `DATA FIELDS` 分层显示。

**预期结果**：`inputs` 只含 `points` 一个键；`data_samples.gt_instances_3d` 中能看到 `labels_3d` 与 `bboxes_3d`。具体打印值待本地验证，但类型断言可以对照单元测试 [tests/test_datasets/test_transforms/test_formating.py:L23-L34](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_datasets/test_transforms/test_formating.py#L23-L34)：官方断言 `inputs['points']` 是 Tensor 且元素和为 13062.6436、`labels_3d` 等于 `tensor([1])`、`bboxes_3d` 是 `LiDARInstance3DBoxes` 实例。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `Pack3DDetInputs` 放在 `RandomFlip3D` 之前会发生什么？

**答案**：`RandomFlip3D` 会从 results 里读 `points`（期望 `BasePoints` 对象）与 `gt_bboxes_3d`（期望框对象）并做同步翻转。打包之后 `points` 已被剥成裸张量搬进 `inputs`，`gt_bboxes_3d` 已搬进 `data_samples.gt_instances_3d`，results 里不再有这些键，`RandomFlip3D` 要么读不到键要么对错误类型调用 `.flip()` 而报错。这解释了「打包必须是最后一步」。

**练习 2**：`Pack3DDetInputs` 的 `transform` 为什么专门处理 list 入参？

**答案**：list 入参来自测试时增强的 `MultiScaleFlipAug3D`（u3-l5 提过它在测试管线里包装一组增强变换）：它把同一帧的多个增强版本放进一个 list。逐个打包成 `list[dict(inputs, data_samples)]` 后，模型 `forward` 里 `data_samples[0]` 是 list 这一特征就成了 augtest 的识别标志（见 [mmdet3d/models/detectors/base.py:L77-L84](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/base.py#L77-L84)）。

### 4.2 张量化：to_tensor 与三类货物的预处理

#### 4.2.1 概念说明

打包的第一步不是搬键，而是**就地改类型**：把 results 里的值统一变成模型能吃的张量。这一步发生在路由之前、直接写回 `results[key]`，所以路由循环搬运的已经是张量。要处理的三类「货物」各有讲究：

- **points**：`BasePoints` 对象（u2-l3）只是管线内的「增强友好包装」，模型骨干只需要 `(N, C)` 裸张量，所以这里调用 `.tensor` 剥壳，属性列（强度、Δt）随张量一起带走；
- **img**：numpy 图像是 `(H, W, C)` 布局（通道在最后，适合 cv2 生态），PyTorch 卷积要 `(C, H, W)`，必须转置；多视角/多帧图像是 list，先堆叠再转置；
- **标签与框**：numpy 标签数组转张量；3D 框如果已是 `BaseInstance3DBoxes` 就**原样保留**（不剥壳），这是 3D 框与点云命运最大的不同——框对象要一路活着进损失函数，因为 assigner/coder 需要它的 `corners`、`bev` 等方法。

#### 4.2.2 核心流程

```text
对 results 就地张量化（按处理顺序）：
① points: BasePoints → .tensor                      (N, C) Tensor
② img:    list[ndarray] → stack → permute(0,3,1,2)  (V, C, H, W) Tensor
          ndarray      → expand_dims(若缺通道) → permute(2,0,1)  (C,H,W)
③ 标签族（proposals/gt_bboxes/gt_labels/gt_bboxes_labels/
   attr_labels/pts_*_mask/centers_2d/depths/gt_labels_3d）:
   逐键 to_tensor（list 则逐元素转）
④ gt_bboxes_3d: 若不是 BaseInstance3DBoxes 才 to_tensor（是则保留对象）
⑤ gt_semantic_seg: to_tensor 后加通道维 [None]；gt_seg_map 同样加维
```

`to_tensor` 的类型分派规则：

| 输入类型 | 输出 |
| --- | --- |
| `torch.Tensor` | 原样返回 |
| `np.ndarray`（float64） | 先 `astype(np.float32)` 再 `torch.from_numpy` |
| `np.ndarray`（其他 dtype） | `torch.from_numpy` |
| `Sequence`（非字符串） | `torch.tensor(data)` |
| `int` | `torch.LongTensor([data])` |
| `float` | `torch.FloatTensor([data])` |

注意 int 被包成长度 1 的 `LongTensor`——这保证标签类标量进 collate 后仍是整数张量，`cross_entropy` 才不会因 float 标签报错。

#### 4.2.3 源码精读

**to_tensor 辅助函数**。[mmdet3d/datasets/transforms/formating.py:L16-L45](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L16-L45) 按上表分派。float64 → float32 的降精度不是随意的：numpy 默认浮点是 float64，而 GPU 上双精度训练又慢又费显存，与其让每个模型自己转，不如在出口统一。

**points 剥壳**。[mmdet3d/datasets/transforms/formating.py:L144-L146](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L144-L146) 判断 `points` 是 `BasePoints` 就取 `.tensor`。u2-l3 讲过 `BasePoints` 内部本来就是 `(N, points_dim)` 张量加属性登记，这里只是丢掉包装层。

**img 的两条路径**。[mmdet3d/datasets/transforms/formating.py:L148-L171](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L148-L171)：list 分支处理多视角/多帧（`np.stack` 后 `permute(0, 3, 1, 2)`）；单图分支先给灰度图补通道维（`len(img.shape) < 3` 时 `expand_dims(-1)`），再 HWC→CHW。L162-L165 的注释值得读：优先用 `torch.permute` 而非 `np.transpose`，因为 torch 侧转置视图可以被后续操作更好的优化（官方引用了 mmdet 的 PR #9533，实测快 3~5 倍）；`c_contiguous` 检查决定走 permute 快路径还是先 `ascontiguousarray` 再转。

**标签族循环与框的豁免**。[mmdet3d/datasets/transforms/formating.py:L173-L186](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L173-L186) 对一组标签类键逐个 `to_tensor`（list 值则逐元素转）；L184-L186 单独处理 `gt_bboxes_3d`：`not isinstance(..., BaseInstance3DBoxes)` 时才转张量——即「裸 numpy 框转张量、框对象放行」。一个值得注意的细节：这个循环的键列表（含 `proposals`、`gt_bboxes_ignore`、`gt_labels`）**大于** 4.3 节的可路由键集合，也就是说有些键会被张量化却无法通过 `keys` 路由（传入会触发 `NotImplementedError`），张量化循环只是为将来扩展预留的统一出口。

**分割图加维**。[mmdet3d/datasets/transforms/formating.py:L188-L192](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L188-L192) 给 `gt_semantic_seg` 转 Tensor 并加 channel 维（`[None]`），`gt_seg_map` 同样加维——语义分割监督习惯 `(1, H, W)` 布局，与图像输入对齐。

#### 4.2.4 代码实践

**实践目标**：验证 `to_tensor` 的类型分派规则，特别是 int → LongTensor 与 float64 → float32。

**操作步骤**：

```python
# 示例代码：探测 to_tensor 的行为
import numpy as np
from mmdet3d.datasets.transforms.formating import to_tensor

print(to_tensor(3))                      # int
print(to_tensor(0.5))                    # float
print(to_tensor(np.array([1.0, 2.0])))   # float64 数组
print(to_tensor([1, 2, 3]))              # list（Sequence）
```

**需要观察的现象**：`to_tensor(3)` 打印 `tensor([3])` 且 dtype 为 int64；numpy float64 数组转出的 dtype 是 torch.float32。

**预期结果**：与上表一一对应；若传字符串会落入 `Sequence` 分支吗？不会——`mmengine.is_str` 排除了字符串，最终走 `TypeError`。具体输出待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `points` 要在打包时剥掉 `BasePoints` 包装，而 `gt_bboxes_3d` 的 `LiDARInstance3DBoxes` 却要保留到模型里？

**答案**：点云进入模型后只需要做矩阵运算（MLP/卷积），裸张量足够；而 3D 框在训练时要被 assigner（算 3D IoU）、coder（encode/decode）反复调用 `bev`、`corners`、`overlaps` 等方法（u2-l2），这些方法住在框对象上。所以点云「到模型门口脱壳」，框「穿壳进损失函数」。

**练习 2**：`to_tensor(np.array([1.5, 2.5], dtype=np.float64))` 和 `to_tensor(np.array([1.5, 2.5], dtype=np.float32))` 的 dtype 分别是什么？

**答案**：前者 float64 会被显式降为 float32 再 `from_numpy`，输出 `torch.float32`；后者本来就是 float32，直接 `from_numpy`，同样 `torch.float32`。即该函数保证浮点数组输出一律 float32。

### 4.3 keys 路由表：四张表决定每个键的归宿

#### 4.3.1 概念说明

配置里的 `keys` 列表是**白名单**：只有列出的键才会被收走，没列出的留在 results 里随 dict 一起被丢弃。而类属性里的**四张路由表**决定每个白名单键的去向：

| 路由表（类属性） | 成员键 | 目的地 |
| --- | --- | --- |
| `INPUTS_KEYS` | `points`、`img` | `packed['inputs'][key]`——模型的正向输入 |
| `INSTANCEDATA_3D_KEYS` | `gt_bboxes_3d`、`gt_labels_3d`、`attr_labels`、`depths`、`centers_2d` | `data_samples.gt_instances_3d[去前缀键]`——3D 实例监督 |
| `INSTANCEDATA_2D_KEYS` | `gt_bboxes`、`gt_bboxes_labels` | `data_samples.gt_instances[...]`——2D 实例监督 |
| `SEG_KEYS` | `gt_seg_map`、`pts_instance_mask`、`pts_semantic_mask`、`gt_semantic_seg` | `data_samples.gt_pts_seg[去前缀键]`——点级监督 |

「去前缀」由 `_remove_prefix` 完成：剥掉 `gt_` 前缀。于是 `gt_labels_3d` 进容器后改叫 `labels_3d`，`gt_bboxes_3d` 改叫 `bboxes_3d`——这与 u2-l4 讲过的消费端键名（`pred_instances_3d.labels_3d`）严格对仗。特例是 `gt_bboxes_labels`：剥前缀会得到 `bboxes_labels`，但 2D 容器沿用了 mmdet 的 `labels` 键名，所以源码显式映射到 `labels`。

若白名单里的键**不在任何一张表**中，直接 `NotImplementedError`——这是一个刻意的设计：路由规则是封闭集合，防止新键悄悄丢失。

#### 4.3.2 核心流程

路由可以概括为一个查表函数。对每个 \( k \in \text{keys} \cap \text{results} \)：

```text
k 的去向 =
    INPUTS_KEYS          → inputs[k]
    INSTANCEDATA_3D_KEYS → gt_instances_3d[strip(k)]        # strip 剥 'gt_'
    INSTANCEDATA_2D_KEYS → gt_instances['labels' if k=='gt_bboxes_labels' else strip(k)]
    SEG_KEYS             → gt_pts_seg[strip(k)]
    其他                  → NotImplementedError（代码 defensive fail）
```

三个容器（`gt_instances_3d`、`gt_instances`、`gt_pts_seg`）在路由开始前就被创建为空对象，路由完**无条件**挂到 data_sample 上——所以即使是测试样本（没有任何 gt 键），`data_samples.gt_instances_3d` 也存在，只是 `len == 0`，下游不会遇到 AttributeError。

**训练与测试的 keys 差异**（本讲学习目标 2）全部体现在配置里：

| 场景 | keys | 出处 |
| --- | --- | --- |
| KITTI LiDAR 训练 | `['points', 'gt_bboxes_3d', 'gt_labels_3d']` | [configs/_base_/datasets/kitti-3d-3class.py:L64-L66](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L64-L66) |
| KITTI LiDAR 测试 | `['points']` | [configs/_base_/datasets/kitti-3d-3class.py:L90](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L90) |
| ScanNet 室内训练 | `['points', 'gt_bboxes_3d', 'gt_labels_3d', 'pts_semantic_mask', 'pts_instance_mask']` | [configs/_base_/datasets/scannet-3d.py:L54-L59](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/scannet-3d.py#L54-L59) |
| nuScenes 单目训练 | `['img', 'gt_bboxes', 'gt_bboxes_labels', 'attr_labels', 'gt_bboxes_3d', 'gt_labels_3d', 'centers_2d', 'depths']` | [configs/_base_/datasets/nus-mono3d.py:L40-L44](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/nus-mono3d.py#L40-L44) |

规律：**测试管线永远只打包 `INPUTS_KEYS`**（预测不需要真值），**训练管线按任务加监督键**（LiDAR 检测只要 3D 框标签，室内 VoteNet 类还要点级 mask，单目还要 2D 框与属性标签）。

#### 4.3.3 源码精读

**四张路由表的定义**。[mmdet3d/datasets/transforms/formating.py:L50-L62](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L50-L62) 以类属性形式给出四张封闭表。注意 `INSTANCEDATA_3D_KEYS` 里的 `attr_labels`、`depths`、`centers_2d` 没有 `gt_` 前缀——它们是 nuScenes 属性分类、单目 3D 的投影中心与深度监督，进容器时键名原样保留（`_remove_prefix` 对无前缀键是幂等的）。

**前缀剥离函数**。[mmdet3d/datasets/transforms/formating.py:L84-L87](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L84-L87) 三行实现 `startswith('gt_')` 则截掉前 3 个字符。

**路由主循环与 fail-fast**。[mmdet3d/datasets/transforms/formating.py:L223-L245](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L223-L245) 逐键查表分发；L238-L241 的 `NotImplementedError` 提示「请修改 Pack3DDetInputs 把这个键放进对应字段」——遇到自定义新键时的正确做法不是绕过，而是扩展路由表（u8-l3 的扩展实践会用到）。L243-L245 把三个容器无条件挂上 data_sample。

**容器挂载经过属性 setter**。`data_sample.gt_instances_3d = gt_instances_3d` 这类赋值会触发 [mmdet3d/structures/det3d_data_sample.py:L137-L147](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/det3d_data_sample.py#L137-L147) 的 setter，内部 `set_field(..., dtype=InstanceData)` 做类型断言——u2-l4 讲过的「运行时类型防线」正是在打包这一刻生效的。

#### 4.3.4 代码实践

**实践目标**：亲手验证路由规则——同一批数据，用不同 `keys` 打包，观察字段去向。

**操作步骤**：

```python
# 示例代码：keys 路由实验
import numpy as np
from mmdet3d.datasets.transforms.formating import Pack3DDetInputs
from mmdet3d.testing import create_data_info_after_loading

results = create_data_info_after_loading()

# 实验一：标准 LiDAR 训练 keys
pack = Pack3DDetInputs(keys=['points', 'gt_bboxes_3d', 'gt_labels_3d'])
p1 = pack(results)
print('inputs:', list(p1['inputs'].keys()))
print('gt_instances_3d 键:', p1['data_samples'].gt_instances_3d.keys())
print('gt_instances 键(应为空):', p1['data_samples'].gt_instances.keys())
print('gt_pts_seg 键(应为空):', p1['data_samples'].gt_pts_seg.keys())

# 实验二：测试 keys（模拟 test_pipeline）
pack_test = Pack3DDetInputs(keys=['points'])
p2 = pack_test(results)
print('测试 inputs:', list(p2['inputs'].keys()))
print('gt_instances_3d 长度(应为0):', len(p2['data_samples'].gt_instances_3d))

# 实验三：未登记键
try:
    Pack3DDetInputs(keys=['plane'])(results)
except NotImplementedError as e:
    print('捕获预期异常:', e)
```

**需要观察的现象**：实验一中 `gt_labels_3d` 进容器后变成 `labels_3d`（前缀被剥）；实验二中 gt 容器存在但长度为 0；实验三抛 `NotImplementedError`。

**预期结果**：三个实验分别对应白名单、测试打包与 fail-fast 三条规则；具体打印待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：想让自定义的新监督键 `gt_velocities` 进入 `gt_instances_3d`，需要改哪里？

**答案**：两步——把 `'gt_velocities'` 追加到 `INSTANCEDATA_3D_KEYS`（或子类化后覆盖该类属性并注册新 transform），再在配置的 `keys` 里加上它。只改配置不改表会得到 `NotImplementedError`；只改表不写配置则键留在 results 里被丢弃。这是 u8-l3 自定义模型/数据的固定套路。

**练习 2**：为什么 `proposals` 出现在 4.2 节的张量化列表里，却不能写进 `keys`？

**答案**：张量化循环（L173-L183）与路由表（L50-L62）是两个独立集合，前者是后者的超集。`proposals`、`gt_bboxes_ignore`、`gt_labels` 会被预先转成 Tensor（为扩展预留），但当前四张表没有登记它们，写入 `keys` 会触发 `NotImplementedError`。读源码时要区分「被准备的键」与「被路由的键」。

**练习 3**：ScanNet 训练配置比 KITTI 多打包两个 mask 键，它们最终进入哪个容器、被哪类模型消费？

**答案**：`pts_semantic_mask`、`pts_instance_mask` 属于 `SEG_KEYS`，去前缀后进入 `data_samples.gt_pts_seg`，长度恒等于点数（u3-l4 的不变量）。VoteNet 类室内检测器的 head 用 `pts_semantic_mask` 做前景种子点监督；语义分割模型（单元 8 的 segmentors）则整体消费 `gt_pts_seg`。

### 4.4 meta_keys 三级查找与 eval_ann_info 特例

#### 4.4.1 概念说明

`meta_keys` 收集的是**元信息**：不是模型的前向输入、也不是监督信号，而是「描述这个样本的上下文」——路径、标定矩阵、增强参数、坐标系类型、样本编号。它们与数据字段的本质区别是：数据字段随任务变（检测/分割打包不同键），元信息则全流程共享（可视化要 `lidar2img` 画投影、评估器要 `sample_idx` 对答案、多模态融合层要 `transformation_3d_flow` 逆变换增强，见 u3-l6）。

收集规则是**三级查找**，对默认清单里的每个键依次尝试：

1. **根级**：`key in results` 直接取——管线各 transform 写到根级的键（如 `LoadAnnotations3D` 之后数据集类提升的 `lidar_path`）在此命中；
2. **images 树**：v2 格式的 info 把相机信息按 `results['images'][cam_id][...]` 组织（u3-l2）。单相机时直接取；多相机时**聚合成 list**（按 cam_id 顺序）；
3. **lidar_points 树**：`results['lidar_points'][key]`，存放点云路径、特征维数等。

三级都找不到就静默跳过。注意源码用的是 `elif` 链：**一旦 results 里存在 `images` 键，lidar_points 分支永远不会被检查**——这是阅读时容易忽略的细节，也解释了为什么数据集基类要把关键 meta 提升到根级（见下）。

#### 4.4.2 核心流程

```text
for key in meta_keys (默认 36 个):
    if key in results:            → data_metas[key] = results[key]      # 一级
    elif 'images' in results:
        单相机 → data_metas[key] = results['images'][cam][key]          # 二级·单视角
        多相机 → data_metas[key] = [各相机中该键的值, …]（有则收录）    # 二级·多视角
    elif 'lidar_points' in results:
        → data_metas[key] = results['lidar_points'][key]                # 三级
data_sample.set_metainfo(data_metas)   # 一次性写入 metainfo 分区
```

写入走 `set_metainfo`，与 data fields 严格分区（u2-l4 讲过 `Det3DDataSample` 的 metainfo/data 两分区与 `to_dict`/序列化行为）。

#### 4.4.3 源码精读

**三级查找循环**。[mmdet3d/datasets/transforms/formating.py:L199-L221](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L199-L221) 是完整实现：L200-L202 根级命中；L203-L217 images 分支，注意 L204 判断 `len(results['images'].keys()) == 1` 区分单/多视角，多视角时 L211-L217 逐相机收集成 `img_metas` list，**只在非空时写入**；L218-L220 是被 `elif` 短路的 lidar_points 分支。最后 L221 `set_metainfo` 一次写入。

**为什么根级优先是可靠的**。[mmdet3d/datasets/det3d_dataset.py:L278-L285](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L278-L285) 中 `parse_data_info` 在 use_lidar 时把 `lidar_path`、`num_pts_feats` 从 `info['lidar_points']` **提升复制到根级**（L284-L285）。正是这次提升，让这两个关键 meta 在一级查找就被命中，不必依赖被 `images` 短路的第三级。读 u3-l1 时看到的「拼绝对路径 + 提升字段」，到这里才显出完整用意。

**eval_ann_info：唯一不走路由的裸 dict**。[mmdet3d/datasets/transforms/formating.py:L246-L249](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L246-L249) 在路由循环之外单独处理 `eval_ann_info`：results 里有就挂上、没有就置 `None`。它是 `Det3DDataSample` 上唯一允许裸 dict 的字段（u2-l4 讲过它的 setter 不做 InstanceData 断言）。消费方是室内评估：[mmdet3d/evaluation/metrics/indoor_metric.py:L49-L58](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/indoor_metric.py#L49-L58) 的 `process` 把每个 data_sample 的 `eval_ann_info` 与 `pred_instances_3d` 配对存进 `self.results`，供 `compute_metrics` 算 mAP@0.25/0.5。室外指标（KittiMetric/NuScenesMetric）则自带 ann_file 读真值，不消费这个字段——所以室外样本打包时它常常是 `None`（数据集类只在 test_mode 下生成它）。

**meta 的消费预览**。[mmdet3d/visualization/local_visualizer.py:L1004](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L1004) 一行就能看到 `data_sample.metainfo` 被整体传给绘制逻辑——`lidar2img` 投影画框、`box_type_3d` 决定框类，全部来自本节收集的元信息。

#### 4.4.4 代码实践

**实践目标**：观察三级查找与多视角聚合的真实行为，理解「images 存在时 lidar_points 被短路」。

**操作步骤**：

```python
# 示例代码：meta 收集实验
from mmdet3d.datasets.transforms.formating import Pack3DDetInputs
from mmdet3d.testing import create_data_info_after_loading

results = create_data_info_after_loading()
print('images 里的相机键:', list(results['images'].keys()))
print('lidar_points 里的键:', list(results['lidar_points'].keys()))

pack = Pack3DDetInputs(keys=['points'])
packed = pack(results)
meta = packed['data_samples'].metainfo
print('收集到的 meta 键:', sorted(meta.keys()))
for k in sorted(meta.keys()):
    v = meta[k]
    print(k, '->', (f'list(len={len(v)})' if isinstance(v, list) else v))
```

**需要观察的现象**：假数据的 `images` 含 CAM0~CAM3 与 R0_rect 共 5 个键，走多视角聚合分支——`img_path` 收集成只含 CAM2 一项的 list（只有 CAM2 有 `img_path`），`cam2img` 收集成 4 项 list；同时 `lidar_points` 里明明有 `num_pts_feats`、`lidar_path`，meta 里却**没有**它们——因为 `elif` 链在 images 分支短路了。

**预期结果**：`metainfo` 里能看到 `img_path`、`cam2img` 等少数键，`data fields` 只有空的 gt 容器；上述短路现象是判断「为什么我的 meta 键没被打包」这类问题的第一嫌疑。具体打印待本地验证。（真实 KITTI 数据集不受此影响，因为 `parse_data_info` 已把这两个键提升到根级。）

#### 4.4.5 小练习与答案

**练习 1**：多视角 nuScenes 样本打包后 `data_samples.metainfo['cam2img']` 是什么形态？为什么？

**答案**：是按相机顺序排列的 list（每个元素是一个内参矩阵），由二级查找的多视角聚合分支生成（L211-L217）。因为多视角模型的 head 需要逐相机的内参做 3D→2D 投影，list 与图像堆叠的相机维一一对应。

**练习 2**：`eval_ann_info` 为什么不像其他 gt 键一样走 `INSTANCEDATA_3D_KEYS` 路由？

**答案**：它是给评估器用的**原始标注副本**（裸 dict，含 difficulty、group_id 等不定字段），不是定长的实例级张量数据，装不进要求「各字段第一维同长」的 `InstanceData`；而且它只在 test_mode 下存在、与损失无关，所以走独立通道直接挂到 data_sample 上。

**练习 3**：默认 `meta_keys` 里有一批 `pcd_*` 开头的键，它们是谁写的、谁读？

**答案**：由 u3-l6 的几何增强（`RandomFlip3D`/`GlobalRotScaleTrans`/`ObjectNoise`）连同 `transformation_3d_flow` 审计列表一起写进 results；打包后被多模态融合层读取，按倒序逆变换把图像标注还原到增强后的坐标系（`apply_3d_transformation`）。这批键是「增强与多模态之间的一根线」。

### 4.5 出口之后：collate、预处理器与字段消费者地图

#### 4.5.1 概念说明

打包产物离开 DataLoader worker 后还要过两道工序才能进模型：**collate**（MMEngine 默认 `pseudo_collate`，把 batch 个 `dict(inputs, data_samples)` 原样拼成 `list`，不堆叠张量——每帧点数不同，本来就堆不成一个矩形张量）和**数据预处理器**（`Det3DDataPreprocessor`，模型的第一个「隐形层」：把图像 list 堆叠成 `(B, C, H, W)`、可选做体素化、把数据搬上 GPU，并向每个 data_sample **追加** `batch_input_shape` 等 metainfo）。之后 `Base3DDetector.forward` 按 mode 三分发到 `loss`/`predict`/`_forward`。

本节把整条链上的「字段 → 消费者」整理成一张总表——这张表正是本讲综合实践要产出的东西，也是读懂单元 4/5 模型代码的钥匙。

#### 4.5.2 核心流程

```text
DataLoader（pseudo_collate）
  data = {'inputs': [{'points': T, …}, …], 'data_samples': [Det3DDataSample, …]}
        │
        ▼  Det3DDataPreprocessor.simple_process
  inputs['img']  → 归一化/填充/堆叠 → inputs['imgs'] (B,C,H,W)
  inputs['points'] → 保留 list；voxel=True 时 → inputs['voxels']（voxel_dict）
  data_samples   → 追加 batch_input_shape / pad_shape metainfo
        │
        ▼  Base3DDetector.forward(inputs, data_samples, mode)
  mode='loss'     → head 读 gt_instances_3d / gt_pts_seg 构造监督
  mode='predict'  → head 出结果 → add_pred_to_datasample 写 pred_instances_3d
        │
        ▼
  Evaluator.process（读 pred_instances_3d + sample_idx / eval_ann_info）
  Visualizer（读 pred_instances_3d + metainfo 标定）
```

字段消费者总表：

| 打包产物字段 | 生产者 | 消费者 |
| --- | --- | --- |
| `inputs['points']` | 4.2 剥壳的张量 | 预处理器（可选体素化，u4-l3）→ `extract_feat` → 骨干网络 |
| `inputs['img']` | 4.2 转置的张量 | 预处理器堆叠成 `imgs` → 图像骨干（单目/多模态） |
| `gt_instances_3d.bboxes_3d` / `labels_3d` | 4.3 路由 | 检测头 `loss` 与 `get_targets`：assigner/coder 用框对象方法（单元 5） |
| `gt_instances.bboxes` / `labels` | 4.3 路由 | 单目/多模态的 2D 分支损失 |
| `gt_pts_seg.pts_*_mask` | 4.3 路由 | VoteNet 前景种子监督、分割模型损失（单元 8） |
| `metainfo['sample_idx']` | 4.4 收集 | 评估器对答案、可视化命名 |
| `metainfo['lidar2img']` 等 | 4.4 收集 | 可视化投影画框 |
| `metainfo['box_type_3d']` | 4.4 收集 | 预测框包装、指标计算 |
| `metainfo['pcd_*' / 'transformation_3d_flow']` | 4.4 收集 | 多模态融合层逆变换增强 |
| `eval_ann_info` | 4.4 特例 | `IndoorMetric` 一族在线评估 |
| （后续追加）`batch_input_shape` | 预处理器 | 图像 head 构造 mask 等 |

#### 4.5.3 源码精读

**预处理器的 simple_process**。[mmdet3d/models/data_preprocessors/data_preprocessor.py:L154-L208](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L154-L208) 是打包产物的第一站：L167-L171 先算图像填充尺寸再 `collate_data`；L174-L179 处理点云——`batch_inputs['points']` 保持 list 原样，`voxel=True` 时调用 `voxelize` 生成 `voxels` 字典（体素化细节是 u4-l3 的主题）；L181-L206 处理图像堆叠；L188-L194 向每个 data_sample **追加** `batch_input_shape` 与 `pad_shape` metainfo——这证明了 metainfo 分区在打包之后仍可继续写入，契约是「活的」。`collate_data`（[L227-L300](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L227-L300)）里 L244 的注释明确写着处理的是 `pseudo_collate` 的产物（Tensor 的 list）。

**forward 三模式分发**。[mmdet3d/models/detectors/base.py:L32-L91](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/base.py#L32-L91) 中 L74-L91 按 mode 分发：`loss` 把 inputs 与 data_samples 一起交给子类；`predict` 先看 `data_samples[0]` 是不是 list（augtest 标志，呼应 4.1）；`tensor` 只前向不读写 data_samples。

**loss 如何消费 gt**。[mmdet3d/models/detectors/single_stage.py:L56-L76](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/single_stage.py#L56-L76) 的 `loss` 签名正是 `(batch_inputs_dict, batch_data_samples)`：docstring 写明 inputs 含 `points`/`imgs` 键，data_samples 含 gt——本讲打包的两个出口在这里精确对接。L74-L75 先 `extract_feat` 再把 data_samples 整体递给 `bbox_head.loss`，由 head 内部拆 `gt_instances_3d`（单元 5 展开）。

**推理路径的同一形态**。[mmdet3d/apis/inference.py:L178](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inference.py#L178) 中 u1-l3 讲过的 `inference_detector` 也是用 `pseudo_collate(data)` 后交给 `model.test_step`——API 推理与训练走的是**同一份契约**，这就是 mmdet3d 里「换个入口不用换数据格式」的根源。

#### 4.5.4 代码实践

**实践目标**：把一份打包产物送进 `pseudo_collate` + `Det3DDataPreprocessor`，观察 inputs 形态从「单样本」变「批量」。

**操作步骤**：

```python
# 示例代码：批量视角观察（LiDAR-only 配置，不做体素化）
import torch
from mmengine.dataset import pseudo_collate
from mmdet3d.models import Det3DDataPreprocessor
from mmdet3d.datasets.transforms.formating import Pack3DDetInputs
from mmdet3d.testing import create_data_info_after_loading

results = create_data_info_after_loading()
packed = Pack3DDetInputs(
    keys=['points', 'gt_bboxes_3d', 'gt_labels_3d'])(results)
batch = pseudo_collate([packed, packed])          # 模拟 batch_size=2
print('collate 后 inputs 键:', batch['inputs'].keys())
print('points 是 list 吗:', isinstance(batch['inputs']['points'], list))

pre = Det3DDataPreprocessor()                     # voxel=False
out = pre(batch, training=True)
print('预处理后 inputs 键:', out['inputs'].keys())
print('data_samples 数量:', len(out['data_samples']))
print('新增 metainfo:',
      set(out['data_samples'][0].metainfo) - set(packed['data_samples'].metainfo))
```

**需要观察的现象**：collate 后 `inputs['points']` 是长度 2 的 Tensor list；预处理后 inputs 仍有 `points` 键（list 原样）、data_samples 数量为 2；纯点云样本没有图像，所以不会出现 `imgs` 键，也不会追加 `batch_input_shape`。

**预期结果**：与上表一致；若把预处理器换成 `Det3DDataPreprocessor(voxel=True, voxel_layer=...)` 则会多出 `inputs['voxels']`——这属于 u4-l3 的内容，这里只需确认「打包产物是预处理器的合法输入」。运行输出待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么 mmdet3d 默认用 `pseudo_collate` 而不是把点云堆叠成 `(B, N, C)` 的 `default_collate`？

**答案**：每帧点数不同（且增强后更不同），堆成矩形要么截断要么填充；体素类模型的处理单位本来就是「每帧各自的点列表」，预处理器/体素化层会自行在 coors 里编码 batch 索引（u4-l3）。所以 collate 只搬运、不整形。

**练习 2**：`batch_input_shape` 这个 metainfo 是谁写入的？为什么不在 `Pack3DDetInputs` 里就写好？

**答案**：由 `Det3DDataPreprocessor.simple_process` 写入（data_preprocessor.py L188-L194）。因为填充后的批内统一尺寸要到「看到了整个 batch 的图像」才能确定，单样本视角的管线阶段无从得知——这体现了「meta 分区在数据流的每一站都可以增量补充」的设计。

**练习 3**：模型 `predict` 模式输出的还是 `list[Det3DDataSample]`，这些 data_sample 与本讲打包的是同一批对象吗？

**答案**：是同一批（引用传递）：`predict` 在传入的 `batch_data_samples` 上调用 `add_pred_to_datasample` 写入 `pred_instances_3d`（base.py L149-L152）。所以一个 data_sample 的完整一生是：**打包生产 gt 与 metainfo → 预处理器追加 meta → 模型追加 pred → 评估器/可视化读取**——本讲生产的对象就是贯穿全流程的那个「信封」。

## 5. 综合实践

**任务：为一份真实管线的打包产物编写「字段档案 + 消费者地图」。**

本任务把本讲四个模块串起来：构造数据 → 完整打包 → 逐字段登记 → 消费者归档。

**步骤**：

1. **构造一条「经过增强」的样本**（不依赖下载数据集，复用官方假数据 + 手工模拟增强写入的键）：

```python
# 示例代码：综合实践主脚本 pack_audit.py
import numpy as np
from mmdet3d.datasets.transforms.formating import Pack3DDetInputs
from mmdet3d.testing import create_data_info_after_loading

results = create_data_info_after_loading()
# 模拟 u3-l6 几何增强会写入的 meta 键（值仅作占位）
results['pcd_scale_factor'] = 1.02
results['pcd_rotation_angle'] = 0.35
results['transformation_3d_flow'] = ['HF', 'R', 'S']

pack = Pack3DDetInputs(
    keys=['points', 'gt_bboxes_3d', 'gt_labels_3d',
          'pts_semantic_mask', 'pts_instance_mask'])
packed = pack(results)

ds = packed['data_samples']
print('== inputs ==')
for k, v in packed['inputs'].items():
    print(f'  {k}: type={type(v).__name__}, shape={getattr(v, "shape", None)}')
print('== data_samples.metainfo ==')
for k, v in ds.metainfo.items():
    print(f'  {k}: {type(v).__name__}')
print('== gt_instances_3d ==');  print(' ', ds.gt_instances_3d.keys())
print('== gt_pts_seg ==');       print(' ', ds.gt_pts_seg.keys())
print('== eval_ann_info ==');    print(' ', ds.eval_ann_info)
```

   （注意：假数据里没有点级 mask，这两个键会被白名单静默跳过，`gt_pts_seg` 为空——这本身就是观察点。）

2. **为每个打印出的字段填一张消费者表**，格式照抄 4.5.2 节总表，但要求写出「证据」：消费者是哪个类的哪个方法（提示：用 u1-l4 教过的 grep class 反查法，例如 `grep -rn "gt_instances_3d" mmdet3d/models/`）。

3. **对照官方测试收尾**：运行 `python -m pytest tests/test_datasets/test_transforms/test_formating.py -v`，把你脚本里 `inputs['points']` 的元素和、`labels_3d` 的值与测试断言（13062.6436 / `tensor([1])`）对比——一致说明你的打包链路理解无误。

**验收标准**：能不查讲义地说出——`inputs` 最多两个键、三个 gt 容器无条件存在、meta 来自三级查找、`eval_ann_info` 走特例通道；消费者表至少覆盖 points、bboxes_3d、labels_3d、sample_idx、box_type_3d 五个字段。测试运行结果待本地验证。

## 6. 本讲小结

- `Pack3DDetInputs` 是数据管线与模型世界之间的**海关**：把几十个平铺键翻译成 `dict(inputs, data_samples)` 两键契约，必须放在管线最后。
- **keys 是白名单、四张表是路由表**：`INPUTS_KEYS` 进 inputs，3D/2D 实例键进对应 `InstanceData`，分割键进 `PointData`；`gt_` 前缀被剥掉（`gt_bboxes_labels`→`labels` 特例），未登记键 fail-fast 报 `NotImplementedError`。
- **meta_keys 走三级查找**（根级 → images 单/多视角 → lidar_points），`elif` 链意味着 images 存在时第三级被短路，数据集基类提前把 `lidar_path`/`num_pts_feats` 提升到根级正是为了绕开这一点。
- **张量化在打包时完成**：points 剥掉 `BasePoints` 壳、img HWC→CHW、int 标签包成 LongTensor、float64 统一降为 float32；3D 框对象**原样存活**进损失函数。
- **训练与测试的差别全在 keys**：测试只打包 `INPUTS_KEYS`，训练按任务加监督键；测试产物仍是完整 `Det3DDataSample`（gt 容器为空）。
- 打包之后：`pseudo_collate` 只搬运不整形 → `Det3DDataPreprocessor` 堆叠图像/可选体素化/追加 metainfo → `forward` 三模式读写同一批 data_sample 对象，评估与可视化最后收尾。

## 7. 下一步学习建议

本讲之后，单元 3（数据侧）全部完成，进入单元 4（模型体系与检测器基类）：

- **u4-l1 注册器机制**：本讲反复出现的 `@TRANSFORMS.register_module()` 与 `dict(type='Pack3DDetInputs')` 的路由原理将正式展开；
- **u4-l2 检测器分层体系**：`Base3DDetector.forward` 三模式、`loss`/`predict` 如何拆解本讲打包出的 data_samples；
- **u4-l3 数据预处理器与体素化**：4.5 节一笔带过的 `voxelize`（hard/dynamic 两种模式、coors 里的 batch 编码）是下一讲的主角，建议先重跑 4.5.4 的脚本并尝试打开 `voxel=True`。

复习建议：把 4.5.2 的消费者总表贴在手边——单元 5、6 讲每个 head 的 `loss_by_feat` 时，都要回到这张表找「它的监督信号从打包的哪个字段来」。
