# Det3DDataSample：贯穿训练推理的数据契约

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `Det3DDataSample` 的全部字段（`gt_instances_3d`、`pred_instances_3d`、`gt_pts_seg`、`pts_pred_instances_3d` 等），并区分「实例级 3D 数据」「实例级 2D 数据」和「点级数据」三类容器。
2. 画出一条数据样本的完整生命周期：数据管线出口（`Pack3DDetInputs`）生产 → `data_preprocessor` 补充 metainfo → 模型 `loss`/`predict` 读写 → 评估器 `process` 消费。
3. 会用属性赋值、`set_data`、`set_metainfo`、`get`、`keys()` 这些读写接口手工构造和拆解一个 `Det3DDataSample`，并能解释 setter 背后的类型断言为什么存在。

## 2. 前置知识

**什么是「数据契约」。** 一个训练框架里有多种角色：DataLoader 负责喂数据、模型负责算损失和预测、评估器负责算指标、可视化器负责画图。如果每个角色都自定义一套数据格式，代码会迅速失控。「契约」就是所有角色共同遵守的一份字段约定——谁生产什么字段、谁消费什么字段，全部写死在一个类里。MMDetection3D 中这份契约就是 `Det3DDataSample`。

**BaseDataElement 与 InstanceData（来自 MMEngine）。** MMEngine 的 `structures` 模块提供了数据容器的基座：

- `BaseDataElement`：最通用容器，内部把字段分成两组——**metainfo（元信息）**与 **data fields（数据字段）**。它提供 `set_metainfo`、`set_data`、`get`、`keys()`、`values()`、`items()` 等读写接口，以及 `print` 时分层打印的友好 repr。
- `InstanceData`：继承自 `BaseDataElement`，专门存「一帧里 N 个实例」的数据（如 N 个框、N 个标签、N 个分数），约束是所有字段的第一个维度必须同为 N，因此支持用下标、切片、布尔掩码「按实例」筛选。

**本讲与前面两讲的衔接。** 前两讲我们已经掌握了契约里装的两个「重型货物」：

- `BaseInstance3DBoxes` 家族（u2-l2）：7 自由度 3D 框的张量包装类，`gt_instances_3d.bboxes_3d` 装的就是它；
- `BasePoints` 家族（u2-l3）：点云容器，但它只住在 `inputs['points']` 里，进不了 `data_samples`（后面会看到原因）。

本讲要回答的问题是：这些货物如何被打包、贴标签、在整条流水线上流转。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `mmdet3d/structures/det3d_data_sample.py` | **本讲主角**。定义 `Det3DDataSample` 及其全部字段的属性读写器 |
| `mmdet3d/structures/point_data.py` | 点级数据容器 `PointData`，承载点云分割的标注与预测 |
| `mmdet3d/structures/__init__.py` | structures 包的总出口，`Det3DDataSample`/`PointData` 从这里对外导出 |
| `mmdet3d/datasets/transforms/formating.py` | `Pack3DDetInputs`：数据管线的最后一个 transform，**生产** Det3DDataSample |
| `mmdet3d/models/detectors/base.py` | `Base3DDetector.forward` 三模式入口 + `add_pred_to_datasample`：把预测**写回** data_sample |
| `mmdet3d/models/detectors/single_stage.py` | 单阶段检测器的 `loss`/`predict`，展示 data_samples 的**消费**方式 |
| `mmdet3d/models/segmentors/base.py` | 分割模型后处理，展示 `set_data` 批量写入 `pred_pts_seg` |
| `mmdet3d/models/data_preprocessors/data_preprocessor.py` | 预处理器向 data_sample **追加** metainfo |
| `mmdet3d/evaluation/metrics/kitti_metric.py` | 评估器**读取** data_sample 的字段 |
| `mmdet3d/structures/ops/transforms.py` | `bbox3d2result`：检测头统一预测键名的工具函数 |
| `mmdet3d/testing/model_utils.py` | 官方测试用的「假 data_sample」构造器，是最好的模仿范本 |
| `tests/test_structures/test_det3d_data_sample.py` | Det3DDataSample 的单元测试，实践时直接运行 |

## 4. 核心概念与源码讲解

### 4.1 Det3DDataSample：3D 检测的数据契约本体

#### 4.1.1 概念说明

`Det3DDataSample` 是一个纯粹的「字段声明」类：它本身几乎没有逻辑，只做两件事——声明 3D 生态需要哪些字段、为每个字段强制类型检查。它继承自 MMDetection 的 `DetDataSample`（2D 检测的契约），因此天然拥有 `proposals`、`ignored_instances`、`gt_instances`、`pred_instances` 这四个 2D 字段，再在之上**新增**了 3D 与点级字段。这个继承关系正是 u1-l1 讲过的「复用 MMDetection 组件」思想在数据结构上的体现。

为什么需要它？因为 3D 检测的输入输出形态比 2D 复杂得多：同一个模型可能既输出点云 3D 框又输出图像 2D 框（多模态），分割模型还要输出逐点标签。把这些字段全部标准化，下游的评估器和可视化器才能做到「一套代码适配所有模型」。

#### 4.1.2 核心流程

一条样本在框架中的完整旅程（本讲的主线图，后面各模块都会回到它）：

```text
数据集 pipeline 产出的 results dict（点、框、标签、各种 meta）
        │
        │  Pack3DDetInputs.pack_single_results   ← 生产：分流打包
        ▼
dict(inputs={'points', 'img'}, data_samples=Det3DDataSample)
        │
        │  DataLoader 组 batch → Det3DDataPreprocessor
        │  （向每个 data_sample 追加 batch_input_shape / pad_shape 等 metainfo）
        ▼
Base3DDetector.forward(inputs, data_samples, mode=...)
        ├── mode='loss'    → head.loss 读取 gt_instances_3d / gt_pts_seg 算损失
        ├── mode='predict' → head 产出 results_list，
        │                    add_pred_to_datasample 写入 pred_instances_3d / pred_instances
        └── mode='tensor'  → 只返回张量，不读写 data_samples
        │（predict 模式的输出：list[Det3DDataSample]）
        ▼
Evaluator.process（KittiMetric 读 pred_instances_3d + sample_idx）
Visualizer（读 pred_instances_3d + metainfo 画框）
```

字段分类总表（建议牢记，读任何 mmdet3d 代码都会反复遇到）：

| 字段 | 容器类型 | 谁生产 | 谁消费 |
| --- | --- | --- | --- |
| `gt_instances_3d` | `InstanceData` | `Pack3DDetInputs`（数据管线） | 检测头 `loss`/`get_targets`（单元 5） |
| `gt_instances` | `InstanceData`（继承自 mmdet） | `Pack3DDetInputs` | 2D 分支损失、多模态评估 |
| `pred_instances_3d` | `InstanceData` | `add_pred_to_datasample` | 评估器、可视化器 |
| `pred_instances` | `InstanceData`（继承自 mmdet） | `add_pred_to_datasample` | 多模态 2D 评估 |
| `pts_pred_instances_3d` / `img_pred_instances_3d` | `InstanceData` | 多模态检测器 | 区分点云/图像来源的 3D 预测 |
| `gt_pts_seg` / `pred_pts_seg` | `PointData` | `Pack3DDetInputs` / 分割模型后处理 | 分割损失与指标 |
| `eval_ann_info` | `dict` 或 `None` | 数据集类 | 在线评估 |
| metainfo（`sample_idx`、`box_type_3d`、`lidar2img`…） | 任意 | `set_metainfo` | 全流程共享 |

#### 4.1.3 源码精读

**类定义与字段总览**。[mmdet3d/structures/det3d_data_sample.py:L11-L53](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/det3d_data_sample.py#L11-L53) 声明类并继承 `DetDataSample`；这段 docstring 就是契约正文，逐条列出每个字段的用途。特别注意 L26-L48 对 `pred_instances_3d` 的说明：**纯点云任务**（`use_lidar=True, use_camera=False`）与**纯视觉任务**（`use_camera=True`）的 3D 预测都存进 `pred_instances_3d`；只有**多模态任务**才把基于点云的 3D 预测存进 `pts_pred_instances_3d`、基于图像的存进 `img_pred_instances_3d`，以示区分。

**官方使用范例写在 docstring 里**。[mmdet3d/structures/det3d_data_sample.py:L54-L107](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/det3d_data_sample.py#L54-L107) 这段可以直接执行的 doctest 展示了三种构造方式：先建 `InstanceData` 再整体赋值（L61-L71）、用关键字参数一步构造（L98、L106）、用 dict 解包构造 `InstanceData` 后再赋值（L109-L116）。L72-L94 还展示了 `print(data_sample)` 的输出格式——分成 `META INFORMATION` 与 `DATA FIELDS` 两层，嵌套字段会缩进展开，这正是 4.1.4 实践要观察的「序列化结果」。

**文件末尾的类型别名**。[mmdet3d/structures/det3d_data_sample.py:L210-L213](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/det3d_data_sample.py#L210-L213) 定义了 `SampleList = List[Det3DDataSample]`、`OptSampleList`、`ForwardResults` 三个别名。全库所有检测器的 `loss`/`predict` 签名用的都是它们——契约一旦如此命名，函数签名就再也不会出现裸 `list`。

**包出口**。[mmdet3d/structures/__init__.py:L26-L27](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/__init__.py#L26-L27) 把 `Det3DDataSample`、`PointData` 与 `BasePoints` 家族一起从 `mmdet3d.structures` 导出；u1-l4 讲过 structures 是六大子模块中最底层的一个，本讲的两个类正位于这层地基之上。

#### 4.1.4 代码实践

**实践目标**：不借助任何数据集和模型，手工构造一个「训练阶段视角」的 `Det3DDataSample`，并打印它的分层 repr，直观理解契约里装了什么。

**操作步骤**（以下为示例代码，可直接存成 `explore_data_sample.py` 在仓库根目录运行）：

```python
# 示例代码：手工构造 Det3DDataSample
import torch
from mmengine.structures import InstanceData

from mmdet3d.structures import (Det3DDataSample, LiDARInstance3DBoxes,
                                PointData)

# 1) 元信息：描述「这一帧」的背景，而非某个具体实例
metainfo = dict(
    sample_idx='000008',
    box_type_3d=LiDARInstance3DBoxes,   # 告诉下游 bboxes_3d 是哪个框类
    lidar_path='data/kitti/training/velodyne/000008.bin')

data_sample = Det3DDataSample(metainfo=metainfo)

# 2) 真值：3 个 KITTI 尺寸的车辆框（x, y, z, dx, dy, dz, yaw）
gt_bboxes_3d = LiDARInstance3DBoxes(torch.tensor([
    [10.0, -2.0, -1.20, 3.9, 1.6, 1.5, 0.50],
    [15.5,  1.0, -1.10, 4.2, 1.7, 1.5, -0.3],
    [20.0,  0.5, -1.05, 1.2, 0.9, 1.1, 1.20],   # 一个行人
]))
gt_instances_3d = InstanceData(
    bboxes_3d=gt_bboxes_3d,
    labels_3d=torch.tensor([0, 0, 2]),           # 0=Car, 2=Pedestrian
)
data_sample.gt_instances_3d = gt_instances_3d

# 3) 假装模型已经预测完：2 个带分数的预测
pred_instances_3d = InstanceData(
    bboxes_3d=LiDARInstance3DBoxes(gt_bboxes_3d.tensor[:2] + 0.1),  # 加偏移模拟误差
    scores_3d=torch.tensor([0.92, 0.61]),
    labels_3d=torch.tensor([0, 0]),
)
data_sample.pred_instances_3d = pred_instances_3d

# 4) 点级真值：3 个点上的分割掩码（演示 SEG 字段，长度=点数）
data_sample.gt_pts_seg = PointData(
    pts_semantic_mask=torch.tensor([1, 1, 0]),
    pts_instance_mask=torch.tensor([0, 0, -1]),
)

# 5) 观察契约内容
print(data_sample)                       # 分层 repr（序列化结果）
print('字段列表:', data_sample.keys())    # data fields 的键
print('元信息键:', data_sample.meta_keys())
print('框数量:', len(data_sample.gt_instances_3d))
print('sample_idx:', data_sample.get('sample_idx'))
```

**需要观察的现象**：

1. `print(data_sample)` 会输出 `<Det3DDataSample(META INFORMATION ... DATA FIELD ...)>` 的嵌套结构，`gt_instances_3d`、`pred_instances_3d`、`gt_pts_seg` 各占一段——与源码 docstring L72-L94 展示的格式完全一致。
2. `keys()` 只包含 data fields（如 `gt_instances_3d`），而 `sample_idx` 要用 `meta_keys()`/`get('sample_idx')` 才能拿到——metainfo 与 data fields 是两个独立分区。
3. `len(data_sample.gt_instances_3d)` 返回 3：`InstanceData` 的长度即实例数。

**预期结果**：脚本能无报错运行，`pred_instances_3d.scores_3d` 与 `labels_3d` 长度一致（`InstanceData` 的同长约束）。具体打印数值属「示例代码」输出，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么点云本身（`BasePoints` 对象）不作为 `Det3DDataSample` 的字段，而是放在 `inputs['points']` 里？

**答案**：`Det3DDataSample` 描述的是「标注与预测」这类随批次变化的监督/输出信息；点云是模型的直接输入张量，`Pack3DDetInputs` 会把它降维成裸 tensor 放进 `inputs`（见 [formating.py:L144-L146](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L144-L146)：`results['points'].tensor`）。只有「点级标注/预测」（掩码）才进 data_sample 的 `gt_pts_seg`/`pred_pts_seg`。

**练习 2**：`pts_pred_instances_3d` 与 `pred_instances_3d` 什么时候用前者？

**答案**：多模态任务（`use_lidar=True, use_camera=True`）。此时点云分支和图像分支各产出一套 3D 预测，分别写入 `pts_pred_instances_3d` 与 `img_pred_instances_3d` 以便区分；单一模态任务统一写 `pred_instances_3d`（依据 docstring L34-L48）。

**练习 3**：`Det3DDataSample` 里哪个字段不是 `InstanceData`/`PointData`，类型是什么？

**答案**：`eval_ann_info`，类型是 `dict` 或 `None`（docstring L51-L52），存数据集原始标注，供在线评估使用。

### 4.2 读写机制：property 三件套与 set_data / get

#### 4.2.1 概念说明

`Det3DDataSample` 没有普通的 `__init__` 参数列表，所有字段都通过 **property（属性）三件套**——getter、setter、deleter——来读写。这样设计有两个目的：

1. **类型防线**：setter 内部调用 `set_field(value, '_xxx', dtype=InstanceData)`，赋值时立刻断言类型，把「塞错容器」的错误从「训练到一半才崩」提前到「赋值那一行」。
2. **下划线隔离**：真实数据存在 `_gt_instances_3d` 这类私有字段里，`gt_instances_3d` 只是访问入口，避免用户绕过检查直接改内部状态。

更底层的通用读写由 MMEngine 的 `BaseDataElement` 提供：`set_data(dict)` 批量写、`get(key, default)` 安全读、`set_metainfo(dict)` 写元信息区。mmdet3d 自己的代码两种风格都在用。

#### 4.2.2 核心流程

```text
写入一条 3D 真值：
data_sample.gt_instances_3d = InstanceData(...)
    → property setter
    → self.set_field(value, '_gt_instances_3d', dtype=InstanceData)
    → 断言 value 是 InstanceData，登记进 _data_fields

批量写入（分割后处理风格）：
data_sample.set_data({'pred_pts_seg': PointData(...), ...})
    → 逐键走 setattr，落到对应分区

读取：
data_sample.gt_instances_3d      # 属性风格
data_sample['gt_instances_3d']   # 下标风格（KittiMetric 就这么用）
data_sample.get('sample_idx')    # 带默认值的读

删除：
del data_sample.gt_instances_3d  → deleter 删私有字段并注销登记
```

#### 4.2.3 源码精读

**gt_instances_3d 三件套**。[mmdet3d/structures/det3d_data_sample.py:L137-L147](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/det3d_data_sample.py#L137-L147)：getter 直接返回 `self._gt_instances_3d`；setter 调 `set_field(..., dtype=InstanceData)` 完成类型检查；deleter 删除私有字段。紧随其后的 [L149-L159](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/det3d_data_sample.py#L149-L159) 是结构完全相同的 `pred_instances_3d`，再往后 [L161-L183](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/det3d_data_sample.py#L161-L183) 是多模态专用的 `pts_pred_instances_3d`/`img_pred_instances_3d`，[L185-L207](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/det3d_data_sample.py#L185-L207) 是点级的 `gt_pts_seg`/`pred_pts_seg`——注意后两者的 `dtype` 换成了 `PointData`。整个类 200 行代码就是这组三件套的重复声明，「声明式契约」的风格一览无余。

**类型断言真的会被测到**。[tests/test_structures/test_det3d_data_sample.py:L112-L117](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_structures/test_det3d_data_sample.py#L112-L117) 用 `pytest.raises(AssertionError)` 验证：给 `pred_instances_3d` 赋裸 tensor、给 `pred_pts_seg` 赋 tensor 都会抛断言错误——这就是 setter 的价值。同文件 [L119-L154](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_structures/test_det3d_data_sample.py#L119-L154) 逐一验证每个字段的 `del` 都会让 `'xxx' not in data_sample` 成立；[L21-L30](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_structures/test_det3d_data_sample.py#L21-L30) 则验证 `get` 接口与成员运算符 `in` 的行为。

**set_data 的真实用法（分割侧）**。[mmdet3d/models/segmentors/base.py:L156-L164](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/base.py#L156-L164) 在分割后处理里一次写入两个字段：`batch_data_samples[i].set_data({'pts_seg_logits': PointData(...), 'pred_pts_seg': PointData(...)})`。与「逐个 property 赋值」等价，但适合成批写入。

**set_metainfo 的真实用法（预处理侧）**。[mmdet3d/models/data_preprocessors/data_preprocessor.py:L188-L194](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L188-L194) 在组完 batch 后向每个 data_sample 追加 `batch_input_shape` 与 `pad_shape`——契约的 metainfo 区在流水线上是「越走越丰富」的：管线写入 `sample_idx`，预处理器再写入 padding 信息，模型 rescale、可视化、评估各取所需。

#### 4.2.4 代码实践

**实践目标**：亲手触发一次类型断言，验证「契约的防线」确实存在，并跑通官方单测。

**操作步骤**：

1. 在 4.1.4 脚本末尾追加两行「故意写错」的代码：

```python
# 示例代码：故意违反契约
try:
    data_sample.pred_instances_3d = torch.rand(2, 4)   # 裸 tensor，非法
except AssertionError as e:
    print('类型断言触发:', e)
```

2. 运行官方单元测试（在仓库根目录）：

```bash
pytest tests/test_structures/test_det3d_data_sample.py -v
```

**需要观察的现象**：

1. 第一段代码打印出「类型断言触发」，且 `data_sample.pred_instances_3d` 仍保持原值（赋值被拒绝）。
2. pytest 输出中 `test_init`、`test_setter`、`test_deleter` 三个用例全部 PASSED。

**预期结果**：断言如约触发、单测全绿。具体报错文案取决于 MMEngine 版本，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `gt_pts_seg` 的 setter 里 `dtype` 是 `PointData` 而 `gt_instances_3d` 是 `InstanceData`？

**答案**：两者数据形态不同——实例级数据（N 个框/标签）要求同长为 N，适合 `InstanceData`；点级数据（每个点一个标签）要求长度等于点数且全部一维，适合 `PointData`。setter 的 `dtype` 就是把这种区分固化成运行时检查。

**练习 2**：`data_sample.gt_instances_3d = xxx` 与 `data_sample.set_data({'gt_instances_3d': xxx})` 效果一样吗？

**答案**：一样。`set_data` 内部逐键走 setattr，最终仍会命中 property setter 的类型断言；分割后处理代码（segmentors/base.py:159）与属性赋值（formating.py:243）是同一机制的两种写法。

**练习 3**：如果想让一个自定义字段（比如 `my_heatmap`）进 data_sample，直接 `data_sample.my_heatmap = tensor` 可以吗？

**答案**：可以。`Det3DDataSample` 继承的 `BaseDataElement` 允许动态添加任意 data field；只有 docstring 里声明的那些字段才有强类型断言。但更稳妥的做法是放进 `InstanceData`/`PointData` 内部（如 `pred_pts_seg.pts_semantic_mask`），保持「一层容器一层含义」的结构。

### 4.3 PointData：点级标注与预测的容器

#### 4.3.1 概念说明

`InstanceData` 的世界是「一个实例一行」；`PointData` 的世界是「一个点一行」。3D 语义分割任务里，一帧 10 万个点各有各的语义标签和实例编号，这些一维、等长的数组需要一个专门容器——这就是 `PointData`。它在 `Det3DDataSample` 中占据 `gt_pts_seg` 与 `pred_pts_seg` 两个字段。

`PointData` 的两条铁律（docstring 明确写出）：所有 data field 必须**都是一维的**、且**长度相同**（都等于点数）。这使它能像张量的第一维那样被整体索引。

#### 4.3.2 核心流程

```text
写入：point_data.pts_semantic_mask = tensor(N,)   → __setattr__ 校验有 __len__
索引：point_data[bool 掩码 / LongTensor / 切片 / int]
    → __getitem__ 对每个字段并行取同一批索引，返回新 PointData
长度：len(point_data) = 第一个 data field 的长度
```

`__getitem__` 是 `PointData` 最有意思的部分：无论传入布尔掩码、索引张量、切片还是整数，它都会**对容器内所有字段施加同一次筛选**，返回一个携带相同 metainfo 的新 `PointData`。这个能力和 `InstanceData` 的按实例筛选同源——数据增强或后处理中「只保留前景点」时，一条索引语句就能同步裁剪语义掩码与实例掩码。

#### 4.3.3 源码精读

**类定义与官方范例**。[mmdet3d/structures/point_data.py:L14-L46](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/point_data.py#L14-L46)：docstring 给出最小示例——100 个点的 `PointData`，`point_data[10:60]` 切片后长度变为 50，随后补写 `pts_semantic_mask`/`pts_instance_mask` 两个 `(100,)` 张量。

**写入校验**。[mmdet3d/structures/point_data.py:L48-L67](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/point_data.py#L48-L67)：`__setattr__` 先保护 `_metainfo_fields`/`_data_fields` 两个内部名单不可被覆盖，再断言新值必须实现 `__len__`（L62-L63 的 `isinstance(value, Sized)`），否则拒绝写入；L67 让 `point_data['key'] = value` 与属性赋值等价。

**索引分发**。[mmdet3d/structures/point_data.py:L69-L154](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/point_data.py#L69-L154)：`__getitem__` 先把 list/ndarray 索引统一转成 `torch.LongTensor`（L79-L87，含 Windows int32→int64 的兼容处理）；字符串索引退化为 `getattr`（L92-L93）；整数索引被改写为保持维度的切片（L95-L100）；张量索引分支（L103-L149）遍历所有字段，tensor 直接 `v[item]`，ndarray 转 numpy 索引，而 str/list/tuple 这类「没有索引语义」的值则借 `cat` 逐个拼接；切片分支（L151-L153）最简单，逐字段 `v[item]`。

**长度定义**。[mmdet3d/structures/point_data.py:L156-L161](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/point_data.py#L156-L161)：`__len__` 返回第一个 data field 的长度，空容器返回 0。

#### 4.3.4 代码实践

**实践目标**：体验 `PointData` 的「并行筛选」能力——用一个布尔掩码同时裁剪两个字段。

**操作步骤**（示例代码）：

```python
# 示例代码：PointData 的并行筛选
import torch
from mmdet3d.structures import PointData

n = 10
pd = PointData(
    pts_semantic_mask=torch.randint(0, 3, (n,)),
    pts_instance_mask=torch.randint(-1, 3, (n,)))

keep = pd.pts_semantic_mask == 2        # 只保留第 2 类点
pd_kept = pd[keep]                       # 一条语句同时裁剪两个字段

print('原始点数:', len(pd))              # 10
print('保留点数:', len(pd_kept))          # 随机，<= 10
print('掩码自检:', (pd_kept.pts_semantic_mask == 2).all().item())

# 违反「必须有 __len__」的写入会被拒绝：
try:
    pd.pts_semantic_mask = 3            # int 没有 __len__
except AssertionError as e:
    print('写入被拒绝:', e)
```

**需要观察的现象**：`len(pd_kept)` 等于掩码中 True 的个数；`pts_instance_mask` 与 `pts_semantic_mask` 被同步裁到同一长度；给字段赋 `3` 这类标量会触发 `AssertionError`。

**预期结果**：以上行为与源码 L48-L67、L156-L161 的实现一致；具体保留点数随机，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`PointData` 为什么把 `pts_instance_mask` 和 `pts_semantic_mask` 设计成同长？

**答案**：两者都按「点」对齐——第 i 个元素的语义类别与实例编号描述的是同一个点。同长约束让 `pd[keep]` 这类一次索引就能保持两个字段的对应关系不乱。

**练习 2**：docstring 里说「未来可能把 instance 级信息移到 `gt_instances_3d`」，这句话暗示 `PointData` 当前还临时承担了什么？

**答案**：它还临时存放 `instances_labels`、`instances_scores` 这类实例级字段（见 [point_data.py:L23-L26](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/point_data.py#L23-L26)），按数据形态这些本应属于 `InstanceData`，官方注释承认这是历史包袱。

**练习 3**：`pd[3]`（整数索引）返回什么？

**答案**：返回只包含第 3 个点的新 `PointData`——L95-L100 把整数 `3` 改写为 `slice(3, None, len(pd))`（步长取容器长度，因此实际只命中索引 3 一个元素），字段仍是一维张量而不是降维成标量，源码注释称这是「keep the dimension」。

### 4.4 data_sample 的完整生命周期：生产、消费、评估

#### 4.4.1 概念说明

单看字段定义还不足以理解契约，必须看它在流水线三个环节里被谁写、被谁读：

- **生产**：`Pack3DDetInputs`（数据管线最后一个 transform，u3-l8 会专门精读）把增强后的 `results` dict 拆成 `inputs` 与 `data_samples`；
- **读写**：模型在 `loss` 模式读真值、在 `predict` 模式写预测；
- **消费**：评估器把 `pred_instances_3d` 与 `sample_idx` 收进结果列表。

理解这条链路后，你读任何检测器/评估器代码都能立刻对上「它在碰契约的哪个字段」。

#### 4.4.2 核心流程

```text
pack_single_results(results) 的分流规则（formating.py）：
  INPUTS_KEYS       ('points','img')        → inputs
  INSTANCEDATA_3D_KEYS (gt_bboxes_3d 等)    → gt_instances_3d（去掉 gt_ 前缀）
  INSTANCEDATA_2D_KEYS (gt_bboxes 等)       → gt_instances
  SEG_KEYS          (pts_*_mask 等)         → gt_pts_seg
  meta_keys         (sample_idx 等 38 项)   → data_sample.set_metainfo

预测写入（detectors/base.py）：
  head 产出 results_list（每个元素键名 bboxes_3d/scores_3d/labels_3d，
  由 bbox3d2result 统一组装）
  → add_pred_to_datasample 逐样本写入
    data_sample.pred_instances_3d / data_sample.pred_instances

评估消费（kitti_metric.py）：
  for data_sample in data_samples:
      读 data_sample['pred_instances_3d'] → 逐字段 .to('cpu')
      读 data_sample['sample_idx']（来自 metainfo）
      存入 self.results，最终由 compute_metrics 换算 AP
```

#### 4.4.3 源码精读

**字段分流的「路由表」**。[mmdet3d/datasets/transforms/formating.py:L50-L62](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L50-L62) 定义四组键名单：`INPUTS_KEYS`、`INSTANCEDATA_3D_KEYS`、`INSTANCEDATA_2D_KEYS`、`SEG_KEYS`——4.4.2 流程图里的分流规则就是查这四张表。[L64-L80](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L64-L80) 的 `meta_keys` 默认值列出约 38 个元信息键（`lidar2img`、`box_type_3d`、`sample_idx`、`pcd_scale_factor`……），相当于契约 metainfo 区的官方清单。

**生产现场**。[mmdet3d/datasets/transforms/formating.py:L194-L221](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L194-L221)：先 `new` 出 `Det3DDataSample()` 与三个空容器（`InstanceData`×2 + `PointData`），再按 `meta_keys` 从 results（含 `images`/`lidar_points` 嵌套 dict 的兼容分支）收集元信息调用 `set_metainfo`。[L223-L245](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L223-L245)：遍历 `self.keys` 查路由表分发，注意 `gt_bboxes_3d` 进来时若不是框容器会被 `to_tensor` 转成裸张量（L184-L186），`gt_` 前缀被 `_remove_prefix` 剥掉（L84-L87）——所以契约里的键名是 `bboxes_3d` 而管线里的键名是 `gt_bboxes_3d`。最后 [L246-L255](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L246-L255) 写入 `eval_ann_info`（无则 `None`），返回 `dict(data_samples=..., inputs=...)`——这就是 DataLoader 拿到的最终形态。

**三模式入口**。[mmdet3d/models/detectors/base.py:L74-L91](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/base.py#L74-L91)：`forward` 按 `mode` 分发到 `loss`/`predict`/`_forward`；`predict` 分支还识别 `data_samples[0]` 是 list 的「测试时增强」情形（u7-l4 的伏笔）。

**训练消费**。[mmdet3d/models/detectors/single_stage.py:L56-L76](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/single_stage.py#L56-L76)：`loss(batch_inputs_dict, batch_data_samples)` 原封不动把整批 data_samples 传给 `bbox_head.loss`——真值的拆包发生在检测头内部（单元 5 精读 `get_targets` 时会看到它逐样本读 `gt_instances_3d`）。

**预测写入**。[mmdet3d/models/detectors/single_stage.py:L109-L113](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/single_stage.py#L109-L113) 把 head 的 `results_list` 交给 `add_pred_to_datasample`；真正写契约的是 [mmdet3d/models/detectors/base.py:L149-L152](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/base.py#L149-L152)：循环里两行赋值 `data_sample.pred_instances_3d = ...` 与 `data_sample.pred_instances = ...`，缺哪一侧就用空 `InstanceData()` 补齐（L140-L147）。预测结果的统一键名来自 [mmdet3d/structures/ops/transforms.py:L51-L77](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/transforms.py#L51-L77) 的 `bbox3d2result`：返回 `dict(bboxes_3d=..., scores_3d=..., labels_3d=...)`（可选 `attr_labels`），全部先 `.to('cpu')`——评估器因此拿到的天然是 CPU 张量前的统一格式。

**评估消费**。[mmdet3d/evaluation/metrics/kitti_metric.py:L145-L168](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/kitti_metric.py#L145-L168)：`process` 逐样本读 `data_sample['pred_instances_3d']`（下标写法等价于属性访问）、逐字段 `.to('cpu')`，再从 metainfo 读 `data_sample['sample_idx']` 作为对齐真值的钥匙，最终append 进 `self.results`，供 `compute_metrics`（L170 起）换算 AP。

**官方「范本数据」**。[mmdet3d/testing/model_utils.py:L124-L148](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/testing/model_utils.py#L124-L148)：mmdet3d 给单测造假数据的函数就是按契约组装——`InstanceData` 装 `bboxes_3d`+`labels_3d` 赋给 `gt_instances_3d`，`PointData` 装 `pts_instance_mask`/`pts_semantic_mask` 赋给 `gt_pts_seg`，metainfo 里放 `box_type_3d`。自己写测试或调试脚本时照抄这个结构即可。

#### 4.4.4 代码实践

**实践目标**：不装任何数据集，直接调用 `Pack3DDetInputs` 把一个手工 results dict 打包成 `{'inputs', 'data_samples'}`，亲眼看到契约被「生产」出来。

**操作步骤**（示例代码）：

```python
# 示例代码：绕过数据集，直接驱动 Pack3DDetInputs
import torch
from mmdet3d.datasets.transforms.formating import Pack3DDetInputs
from mmdet3d.structures import LiDARInstance3DBoxes, LiDARPoints

results = dict(
    points=LiDARPoints(torch.rand(100, 4)),          # 100 点，4 通道
    gt_bboxes_3d=LiDARInstance3DBoxes(torch.rand(3, 7)),
    gt_labels_3d=torch.tensor([0, 0, 2]),
    sample_idx='000008',
    box_type_3d=LiDARInstance3DBoxes,
    lidar_path='data/kitti/training/velodyne/000008.bin',
)

pack = Pack3DDetInputs(
    keys=('points', 'gt_bboxes_3d', 'gt_labels_3d'),
    meta_keys=('sample_idx', 'box_type_3d', 'lidar_path'))
packed = pack.transform(results)

print('顶层键:', packed.keys())                        # dict_keys(['data_samples', 'inputs'])
print('inputs 键:', packed['inputs'].keys())           # ['points']，且已是裸 tensor
ds = packed['data_samples']
print(type(ds).__name__)                               # Det3DDataSample
print('真值字段:', ds.gt_instances_3d.keys())          # ['bboxes_3d', 'labels_3d']（前缀已剥）
print('sample_idx:', ds.get('sample_idx'))
print('points 形状:', packed['inputs']['points'].shape)  # torch.Size([100, 4])
```

**需要观察的现象**：

1. `packed` 只有两个顶层键：`data_samples` 与 `inputs`。
2. `inputs['points']` 已经从 `LiDARPoints` 对象变成了裸 `torch.Tensor`（formating.py:L144-L146 的降维）。
3. `gt_instances_3d` 内部的键是 `bboxes_3d`/`labels_3d`——`gt_` 前缀在打包时被剥掉。
4. `sample_idx` 不在 `keys()` 里而在 metainfo 区，`ds.get('sample_idx')` 才能取到。

**预期结果**：以上 4 点均与 formating.py 的实现一一对应；具体张量数值随机，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：管线里键名是 `gt_bboxes_3d`，契约里却是 `bboxes_3d`，这层改名发生在哪里、为什么？

**答案**：发生在 [formating.py:L84-L87](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L84-L87) 的 `_remove_prefix`，在写入 `gt_instances_3d[...]` 前剥掉 `gt_`。因为容器名 `gt_instances_3d` 已经表达了「这是真值」，内部字段再带 `gt_` 就冗余了；而 `pred_instances_3d` 复用同样的字段名 `bboxes_3d`，读写两侧因此对称。

**练习 2**：`add_pred_to_datasample` 为什么要为缺失的一侧填空 `InstanceData()`（base.py:L140-L147）？

**答案**：为了保证契约的完整性——每个输出 data_sample 都同时具有 `pred_instances_3d` 与 `pred_instances` 两个字段。纯点云模型没有 2D 预测，评估器（如 KittiMetric.process 同时读两者，kitti_metric.py:L158-L165）就不用判空，下游代码得以简化。

**练习 3**：`KittiMetric.process` 用 `data_sample['sample_idx']` 对齐真值。这个键最初是谁写进 data_sample 的？

**答案**：`Pack3DDetInputs` 在收集 metainfo 时写入（`meta_keys` 默认清单含 `sample_idx`，formating.py:L73；写入点在 L199-L221 的 `set_metainfo`）。数据集类的 `parse_data_info` 先在 results dict 里提供它（单元 3 展开）。

## 5. 综合实践

**任务：手工扮演一次「最小训练-推理-评估」回合，全程只用契约，不碰真实数据与模型。**

把下面三段拼成一个脚本（示例代码），复现 4.1.2 主线图的每个环节：

```python
# 示例代码：契约视角的迷你回合
import torch
from mmengine.structures import InstanceData

from mmdet3d.datasets.transforms.formating import Pack3DDetInputs
from mmdet3d.evaluation.metrics.kitti_metric import KittiMetric
from mmdet3d.structures import Det3DDataSample, LiDARInstance3DBoxes, LiDARPoints

# ── 环节 1：模拟数据管线出口 ──────────────────────────────
results = dict(
    points=LiDARPoints(torch.rand(100, 4)),
    gt_bboxes_3d=LiDARInstance3DBoxes(torch.rand(3, 7)),
    gt_labels_3d=torch.tensor([0, 0, 2]),
    sample_idx='000001', box_type_3d=LiDARInstance3DBoxes)
packed = Pack3DDetInputs(
    keys=('points', 'gt_bboxes_3d', 'gt_labels_3d'),
    meta_keys=('sample_idx', 'box_type_3d')).transform(results)
data_sample, inputs = packed['data_samples'], packed['inputs']

# ── 环节 2：模拟模型 predict 写入预测（照抄 add_pred_to_datasample 的两行）──
fake_head_result = InstanceData(
    bboxes_3d=data_sample.gt_instances_3d.bboxes_3d[:2],
    scores_3d=torch.tensor([0.9, 0.8]),
    labels_3d=torch.tensor([0, 2]))
data_sample.pred_instances_3d = fake_head_result
data_sample.pred_instances = InstanceData()          # 2D 侧补空，保持契约完整

# ── 环节 3：模拟评估器消费（复刻 KittiMetric.process 的读取方式）──
pred_3d = data_sample['pred_instances_3d']           # 下标读法与 KittiMetric 相同
pred_3d = {k: v.to('cpu') for k, v in pred_3d.items()}
record = dict(pred_instances_3d=pred_3d,
              sample_idx=data_sample['sample_idx'])
print('送入评估器的记录:', record)

# ── 环节 4：模拟置信度过滤（评估/可视化常见操作）──
kept = fake_head_result[fake_head_result.scores_3d > 0.85]
print('过滤后实例数:', len(kept))                      # 预期 1（0.9 > 0.85）
```

**验收标准**：脚本能跑通；环节 3 打出的 `record` 的键与 `KittiMetric.process`（kitti_metric.py:L156-L168）收集的键一致；环节 4 的过滤结果符合预期。张量数值与 `record` 的具体内容**待本地验证**。完成后回答：如果环节 2 少写 `pred_instances = InstanceData()`，环节 3 的代码会在哪一行以什么方式失败？（提示：`KittiMetric.process` 对 `pred_2d` 逐字段 `.to('cpu')`。）

## 6. 本讲小结

- `Det3DDataSample` 继承 mmdet 的 `DetDataSample`，用 property 三件套声明 `gt_instances_3d`/`pred_instances_3d`/`pts_pred_instances_3d`/`gt_pts_seg` 等字段，setter 的 `dtype` 断言就是契约的运行时防线。
- 实例级数据装 `InstanceData`（N 实例同长），点级数据装 `PointData`（N 点同长、全一维），`eval_ann_info` 是唯一的裸 dict 字段。
- metainfo 与 data fields 是两个独立分区：`sample_idx`、`box_type_3d`、`lidar2img` 等元信息由管线写入、由预处理器增补（`batch_input_shape`/`pad_shape`），全流程共享。
- 生命周期三步：`Pack3DDetInputs.pack_single_results` 按 four 张键名路由表生产 `dict(inputs, data_samples)`；模型 `loss` 模式读真值、`predict` 模式经 `add_pred_to_datasample` 写预测；评估器用 `data_sample['pred_instances_3d']` 与 `sample_idx` 消费结果。
- `gt_` 前缀在打包时被剥掉，因此真值容器与预测容器内部字段同名（`bboxes_3d`/`labels_3d`），读写对称；`bbox3d2result` 保证所有检测头的预测键名统一。
- 读写 API 三套等价通道：属性赋值（触发类型断言）、`set_data`（批量）、`set_metainfo`（元信息区）；`get`/`keys()`/`in` 用于安全读取。

## 7. 下一步学习建议

本讲完成了「数据结构」单元的最后一块拼图。下一步进入单元 3（数据集与数据管线）：

1. **u3-l1 Det3DDataset 基类**：看 `parse_data_info` 如何为 `Pack3DDetInputs` 准备 results dict 里的每一个键——本讲 4.4 环节的上游。
2. **u3-l8 Pack3DDetInputs 专讲**：本讲只精读了它的分流骨架，那一讲会完整覆盖 `meta_keys` 全表、训练/测试两套打包差异。
3. 想先验证环境的读者，可以运行 `pytest tests/test_structures/ -v`，把本讲的三个结构（框、点、DataSample）的单测一起跑绿。
4. 阅读 [mmdet3d/testing/model_utils.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/testing/model_utils.py) 的完整 `create_data_sample` 函数——它是官方认可的「标准 data_sample 长什么样」的参考答案。
