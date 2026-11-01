# u4-l3 Det3DDataPreprocessor 与体素化预处理

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `Det3DDataPreprocessor` 在整个训练/推理链路中的位置：它挂在模型上、在 `forward` 之前，把 DataLoader 送来的一个 batch 整理成模型可直接消费的 `dict(inputs, data_samples)`。
2. 解释图像路径（pad → 归一化 → stack 成 `(B, 3, H, W)`）与点云路径（保持 `List[Tensor]` → 可选体素化）为什么处理方式完全不同。
3. 读懂 `voxel_size`、`max_num_points`、`max_voxels`、`grid_shape`、`point_cloud_range` 这一组体素化配置的含义与换算关系。
4. 理解 batch 维如何被编码进 `coors` 张量的第 0 列，以及下游 `VoxelNet.extract_feat` 如何仅凭 `coors[-1, 0] + 1` 恢复 batch_size。
5. 手工调用 `VoxelGenerator`（numpy/numba 参考实现）和 `VoxelizationByGridShape`（mmcv CUDA 算子封装），打印 `coors`、`voxels`、`num_points` 并解释每个数字。

## 2. 前置知识

本讲建立在前面几讲已建立的认知之上，先用两段话把它们串起来。

**数据从哪来**：u3-l8 讲过，数据管线的最后一个 transform `Pack3DDetInputs` 会把平铺的 results dict 打包成 `dict(inputs, data_samples)`；再经 `pseudo_collate`，一个 batch 变成 `{'inputs': {'points': [帧1张量, 帧2张量, ...], 'img': [...]}, 'data_samples': [...]}`。注意 `pseudo_collate` 只做「装进 list」而不做任何堆叠——点云各帧点数不同、图像各帧尺寸可能不同，此刻还没法变成规整张量。

**数据到哪去**：u4-l2 讲过，检测器的 `loss`/`predict` 都从 `data_preprocessor` 处理后的 batch 开始。`Det3DDataPreprocessor` 正是这一步的实现者：图像必须被 pad 到统一尺寸再 stack，点云则可以「不堆叠」，因为体素化之后 batch 信息会以另一种方式（coors 第 0 列）编码进规整的整数张量里。

另外需要两个朴素的概念：

- **体素（voxel）**：把三维空间用一个长方体范围（`point_cloud_range`）罩住，再按 `voxel_size` 切成小格子，每个格子就是一个体素。点云是「一串无序点」，体素网格是「有行列编号的格子」，后者才能被卷积网络处理。
- **柱体（pillar）**：如果 `voxel_size` 的 z 方向尺寸覆盖了整个高度范围，z 方向就只有 1 层格子，体素退化成一根根竖直的「柱子」——这就是 PointPillars 里的 pillar。

格坐标的数学定义很简单。设第 \( j \) 维的范围下界为 \( r_j^{\min} \)、体素边长为 \( v_j \)，则点 \( p_j \) 落入的格编号为：

\[ c_j = \left\lfloor \frac{p_j - r_j^{\min}}{v_j} \right\rfloor \]

若 \( c_j < 0 \) 或 \( c_j \geq G_j \)（该维格数），该点在范围之外，直接丢弃。网格形状为：

\[ G = \mathrm{round}\left(\frac{r^{\max} - r^{\min}}{v}\right) \]

本讲会反复用到这两个式子。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mmdet3d/models/data_preprocessors/data_preprocessor.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py) | `Det3DDataPreprocessor`：batch 的第一站，图像归一化/pad/堆叠 + 点云体素化调度，支持 hard/dynamic/cylindrical/minkunet 四种体素化 |
| [mmdet3d/models/data_preprocessors/voxelize.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/voxelize.py) | `VoxelizationByGridShape`（模型前向用的体素化层，包装 mmcv CUDA 算子）与 `DynamicScatter3D` |
| [mmdet3d/models/task_modules/voxel/voxel_generator.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/voxel/voxel_generator.py) | `VoxelGenerator`：numpy + numba 的 CPU 参考实现，用于数据管线离线采样 |
| [configs/_base_/models/pointpillars_hv_secfpn_kitti.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/pointpillars_hv_secfpn_kitti.py) | PointPillars KITTI 的 `data_preprocessor` 配置，是本讲的配置对照组 |
| [mmdet3d/models/detectors/voxelnet.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/voxelnet.py) | `VoxelNet.extract_feat`：体素字典的下游消费者 |
| [mmdet3d/models/voxel_encoders/pillar_encoder.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/voxel_encoders/pillar_encoder.py) | `PillarFeatureNet.forward`：接收 `(voxels, num_points, coors)` 三件套 |
| [tests/test_models/test_task_modules/test_voxel/test_voxel_generator.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_task_modules/test_voxel/test_voxel_generator.py) | `VoxelGenerator` 的确定性单测，是本讲实践的官方依据 |
| [tests/test_models/test_data_preprocessors/test_data_preprocessor.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_data_preprocessors/test_data_preprocessor.py) | `Det3DDataPreprocessor` 的单测，展示如何构造伪 batch |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1 `Det3DDataPreprocessor`：batch 的第一站**——图像与点云两条分岔路径。
2. **4.2 `VoxelizationByGridShape`：模型前向的体素化层**——hard 与 dynamic 两种模式、`voxel_size` 与 `grid_shape` 的互推。
3. **4.3 `voxelize()` 方法：batch 维如何编进 `coors`**——四种 `voxel_type` 与 batch 编码、`voxel_centers` 反算。
4. **4.4 `VoxelGenerator`：numpy/numba 参考实现**——CPU 版逐点落格算法，及它在数据管线中的真实用途。

### 4.1 Det3DDataPreprocessor：batch 的第一站

#### 4.1.1 概念说明

`Det3DDataPreprocessor` 是 mmdet3d 全部检测模型的默认数据预处理器（写在每个 model config 的 `data_preprocessor` 字段里）。它继承自 MMDetection 的 `DetDataPreprocessor`，做两件互补的事：

- **图像路径**：把 `inputs['img']` 里尺寸各异的图像 pad 到统一尺寸（可再对齐到 `pad_size_divisor` 的倍数）、做通道转换（BGR↔RGB）与归一化，最后 stack 成 `(B, 3, H, W)` 的规整张量，键名从 `img` 改为 `imgs`。
- **点云路径**：`inputs['points']` **原样保留为 `List[Tensor]`**（不做 pad、不做 stack），若配置了 `voxel=True`，则额外产出一个 `voxels` 字典挂到 `batch_inputs['voxels']` 上。

为什么点云不堆叠？因为各帧点数不同，`torch.stack` 要求每一维长度一致；更重要的是，体素化之后 batch 信息会被编码进 `coors` 的第 0 列（4.3 节），无需靠张量的第 0 维表达。

#### 4.1.2 核心流程

```text
DataLoader 产出 data = {'inputs': {'points': [T1, T2...], 'img': [I1, I2...]},
                        'data_samples': [S1, S2...]}
        │
        ▼
forward(data)                    # TTA 时 data 是 list，逐增强分支处理
        ▼
simple_process(data)
        ├─ 若 inputs 里有 'img'：先算 batch_pad_shape（pad 后的逐帧尺寸）
        ├─ collate_data(data)
        │     ├─ cast_data：把张量搬到目标设备
        │     ├─ 图像：preprocess_img（通道转换 + 归一化）
        │     │        → stack_batch / multiview_img_stack_batch（pad + 堆叠）
        │     │        → 键名 img → imgs
        │     └─ 点云：原样保留 List[Tensor]
        ├─ 若 inputs 里有 'points'：
        │     batch_inputs['points'] = inputs['points']
        │     若 self.voxel：batch_inputs['voxels'] = self.voxelize(points, data_samples)
        ├─ 若 inputs 里有 'imgs'：
        │     写入 batch_input_shape / pad_shape 到每个 data_sample 的 metainfo
        │     samplelist_boxtype2tensor(data_samples)   # 3D 框对象 → 张量
        └─ return {'inputs': batch_inputs, 'data_samples': data_samples}
```

输出里 `batch_inputs` 可能含三个键：`points`（原始点列表）、`voxels`（体素字典，体素化开启时）、`imgs`（图像张量）。这正是 u4-l2 讲过的检测器 `loss`/`predict` 所读的 `batch_inputs_dict`。

#### 4.1.3 源码精读

类声明与继承关系——注意它注册在 `MODELS` 节点、父类来自 mmdet：

- [mmdet3d/models/data_preprocessors/data_preprocessor.py:L22-L23](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L22-L23) —— `@MODELS.register_module()` 装饰的 `Det3DDataPreprocessor(DetDataPreprocessor)`，配置里 `type='Det3DDataPreprocessor'` 就路由到这里。

构造函数：图像相关参数全部交给父类，点云相关的只有五个新参数，其中 `voxel_layer` 字典在 `voxel=True` 时被展开成 `VoxelizationByGridShape` 实例：

- [mmdet3d/models/data_preprocessors/data_preprocessor.py:L119-L124](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L119-L124) —— 保存 `voxel/voxel_type/batch_first/max_voxels`，并用 `VoxelizationByGridShape(**voxel_layer)` 构建体素化层。这就是 config 里 `voxel_layer=dict(...)` 的每个键都对应该类构造参数的原因。

`forward` 的测试时增强分支——u7-l4 的 TTA 会把多个增强视角组成 list 传入，这里逐个走 `simple_process`：

- [mmdet3d/models/data_preprocessors/data_preprocessor.py:L142-L152](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L142-L152) —— `data` 是 list 时按增强分支循环处理再原样返回 list。

`simple_process` 是主调度，两条路径在这里分岔：

- [mmdet3d/models/data_preprocessors/data_preprocessor.py:L167-L171](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L167-L171) —— 先在 pad 之前记下原始图像形状用于计算 `batch_pad_shape`，再调用 `collate_data` 完成搬运与图像整理。
- [mmdet3d/models/data_preprocessors/data_preprocessor.py:L174-L179](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L174-L179) —— **点云路径的全部三行**：把 `inputs['points']` 直接放入 `batch_inputs['points']`；若开启体素化，调用 `self.voxelize(...)` 把返回的字典挂到 `batch_inputs['voxels']`。
- [mmdet3d/models/data_preprocessors/data_preprocessor.py:L184-L197](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L184-L197) —— 图像路径收尾：把 `batch_input_shape`（堆叠后的统一尺寸）和 `pad_shape`（该帧 pad 到的尺寸）写进每个 `data_sample` 的 metainfo（DETR 类模型的 mask 构造需要它），再调 `samplelist_boxtype2tensor` 把 data_samples 里的框对象转成张量。
- [mmdet3d/models/data_preprocessors/data_preprocessor.py:L203-L208](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L203-L208) —— 训练期可选的 batch 级增强，最后返回 `{'inputs': batch_inputs, 'data_samples': data_samples}`。

`collate_data` 里图像如何变成 `imgs`：

- [mmdet3d/models/data_preprocessors/data_preprocessor.py:L240-L266](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L240-L266) —— `cast_data` 搬运设备后，对 `pseudo_collate` 产出的图像列表逐帧 `preprocess_img`（3 维单视角）或逐视角处理再 stack（4 维多视角），最后用 mmengine 的 `stack_batch` 按 `pad_size_divisor` 对齐并堆叠。
- [mmdet3d/models/data_preprocessors/data_preprocessor.py:L296](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L296) —— 整理完成后写回 `data['inputs']['imgs']`：键名由 `img`（单帧）变成 `imgs`（batch 张量）。

#### 4.1.4 代码实践

官方单测 `test_forward` 展示了最小可跑用法（CPU 即可，不需要 GPU）：

1. **实践目标**：亲手跑通 `Det3DDataPreprocessor` 的图像+点云 batch，确认输出形态。
2. **操作步骤**：在仓库根目录新建临时脚本（不要写进仓库），内容改编自官方测试：

   ```python
   # 示例代码：改编自 tests/test_models/test_data_preprocessors/test_data_preprocessor.py
   import torch
   from mmdet3d.models.data_preprocessors import Det3DDataPreprocessor
   from mmdet3d.structures import Det3DDataSample

   processor = Det3DDataPreprocessor(mean=[0., 0., 0.], std=[1., 1., 1.])
   points = torch.randn((5000, 3))                      # 一帧 5000 点
   image = torch.randint(0, 256, (3, 11, 10)).float()   # 一帧 3x11x10 图像
   data = {'inputs': dict(points=[points], img=[image]),
           'data_samples': [Det3DDataSample()]}
   out = processor(data)
   print(out['inputs'].keys())             # 期望含 points 与 imgs
   print(out['inputs']['imgs'].shape)      # 期望 (1, 3, 11, 10)
   print(len(out['inputs']['points']))     # 期望 1，仍是 list
   ```

3. **需要观察的现象**：`points` 仍是长度为 1 的 list 且未被 pad；`imgs` 已是 4 维张量；`data_samples` 长度不变。
4. **预期结果**：与官方断言一致（`imgs.shape == (1, 3, 11, 10)`）。可以把两帧图像改成不同宽（如 `(3,10,11)` 与 `(3,9,14)`）再跑一次，观察 `imgs` 自动 pad 到 `(2, 3, 10, 14)`——这正是单测 L59-L75 验证的行为。
5. 体素化分支（`voxel=True`）依赖 mmcv 的编译算子，本节先不跑，4.3 节实践再涉及；若本机 mmcv 未编译对应扩展，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么点云不能像图像那样直接 `torch.stack` 成 `(B, N, C)`？

**答案**：`torch.stack` 要求各张量形状完全一致，而各帧点数几乎必然不同；即使强行 pad，大量占位点也会污染体素统计。因此点路径保留 `List[Tensor]`，batch 信息改由体素化后的 `coors` 第 0 列编码（见 4.3）。

**练习 2**：inputs 里的 `'img'` 键是在哪一步、为什么变成 `'imgs'` 的？

**答案**：在 `collate_data` 完成归一化、pad、堆叠之后（[data_preprocessor.py:L296](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L296)）。改名是为了区分「逐帧图像列表」与「已堆叠的 batch 图像张量」两种语义，`simple_process` 后半段（L181）据此用 `'imgs' in inputs` 判断。

**练习 3**：`batch_input_shape` 与 `pad_shape` 两个 metainfo 有何区别？

**答案**：`batch_input_shape` 是整个 batch 堆叠后的统一 `(H, W)`（取第一帧的堆叠尺寸）；`pad_shape` 是每个样本各自被 pad 到的尺寸。前者构造 batch 级 mask（如 DETR 的 transformer mask），后者还原该帧原始有效区域。

### 4.2 VoxelizationByGridShape：模型前向的体素化层

#### 4.2.1 概念说明

`VoxelizationByGridShape`（位于 `voxelize.py`）是真正干活的体素化层，它包装 mmcv 编译好的 CUDA/CPU 算子 `hard_voxelize_forward` 与 `dynamic_voxelize_forward`。它的名字里 "ByGridShape" 指一个贴心功能：你可以不给 `voxel_size`，改为给 `grid_shape`（每维格数），它会按范围自动反推体素尺寸。

它支持两种体素化模式（由 `max_points`/`max_voxels` 是否为 -1 决定）：

- **hard（硬体素化）**：每个体素最多装 `max_num_points` 个点，输出定长张量 `(M, max_points, C)`，超出的点**静默丢弃**；同时限制体素总数不超过 `max_voxels`。SECOND、PointPillars 用这种。
- **dynamic（动态体素化）**：不丢点、不定长，只返回每个点所在格子的坐标 `coors (N, 3)`（每点一行），「体素特征」就是原始点本身。点→体素的聚合交给下游（如 `DynamicScatter3D`）再做。

#### 4.2.2 核心流程

构造时的两条互斥推算路径（注意公式不对称）：

- 给 `voxel_size`：\( G = \mathrm{round}\!\big(\frac{r^{\max}-r^{\min}}{v}\big) \)（格数由尺寸除出来）
- 给 `grid_shape`：\( v = \frac{r^{\max}-r^{\min}}{G - 1} \)（尺寸由格数减一除出来）

forward 时按训练/测试选择 `max_voxels`（构造时被 `_pair` 规整成二元组）：

```text
forward(points)
   ├─ training ? max_voxels = self.max_voxels[0] : self.max_voxels[1]
   └─ voxelization(points, voxel_size, point_cloud_range,
                   max_num_points, max_voxels, deterministic)
        ├─ max_points==-1 或 max_voxels==-1 → dynamic：
        │     仅返回 coors (N, 3)   # 每点一行
        └─ 否则 → hard：
              预分配 voxels (max_voxels, max_points, C)
              预分配 coors   (max_voxels, 3)
              预分配 num_points (max_voxels,)
              CUDA/CPU kernel 逐点落格填充
              截取 [:voxel_num] 返回有效部分
```

#### 4.2.3 源码精读

算子加载——体素化的热点循环在 mmcv 的编译扩展里，本文件只是桥：

- [mmdet3d/models/data_preprocessors/voxelize.py:L11-L14](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/voxelize.py#L11-L14) —— 从 mmcv `_ext` 加载 `dynamic_voxelize_forward`、`hard_voxelize_forward` 与 `dynamic_point_to_voxel_{forward,backward}` 四个算子。

`_Voxelization.forward` 的双模式分支：

- [mmdet3d/models/data_preprocessors/voxelize.py:L62-L70](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/voxelize.py#L62-L70) —— dynamic 分支：只分配 `(N, 3)` 的 coors 并调用 `dynamic_voxelize_forward`，不产生定长 voxels。
- [mmdet3d/models/data_preprocessors/voxelize.py:L71-L94](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/voxelize.py#L71-L94) —— hard 分支：先按 `max_voxels × max_points × C` 预分配三个缓冲区，kernel 填完后用 `[:voxel_num]` 切出有效段，返回 `(voxels, coors, num_points)` 三元组。docstring 里特别提醒：**`max_voxels` 可能丢点，调用前应先洗牌点云**——这正是数据管线末尾放 `PointShuffle` 的原因之一（u3-l6）。
- [mmdet3d/models/data_preprocessors/voxelize.py:L43-L52](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/voxelize.py#L43-L52) —— `deterministic` 参数说明：非确定性实现明显更快但结果不可复现，细节见注释里链接的 issue 894 / PR 904。

`VoxelizationByGridShape` 的构造与互推：

- [mmdet3d/models/data_preprocessors/voxelize.py:L136-L137](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/voxelize.py#L136-L137) —— `voxel_size` 与 `grid_shape` 互斥，同时给直接报错。
- [mmdet3d/models/data_preprocessors/voxelize.py:L140-L143](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/voxelize.py#L140-L143) —— `max_voxels` 若是单个 int 会被 `_pair` 变成 `(int, int)`，即「(训练上限, 测试上限)」。
- [mmdet3d/models/data_preprocessors/voxelize.py:L146-L162](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/voxelize.py#L146-L162) —— 两条互推路径的实现：给 `voxel_size` 时 `grid_shape = (range[3:] - range[:3]) / voxel_size` 后取整；给 `grid_shape` 时 `voxel_size = (range[3:] - range[:3]) / (grid_shape - 1)`，注意分母是 `grid_shape - 1`。
- [mmdet3d/models/data_preprocessors/voxelize.py:L164-L172](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/voxelize.py#L164-L172) —— forward 按 `self.training` 取 `max_voxels[0]` 或 `[1]` 再调用底层 `voxelization`。

配置对照（PointPillars KITTI）：

- [configs/_base_/models/pointpillars_hv_secfpn_kitti.py:L1-L12](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/pointpillars_hv_secfpn_kitti.py#L1-L12) —— `voxel_size = [0.16, 0.16, 4]`，`point_cloud_range = [0, -39.68, -3, 69.12, 39.68, 1]`，`max_num_points=32`，`max_voxels=(16000, 40000)`。套用 \( G = \mathrm{round}\big(\frac{r^{\max}-r^{\min}}{v}\big) \)：x 方向 \( (69.12-0)/0.16 = 432 \)，y 方向 \( (39.68-(-39.68))/0.16 = 496 \)，z 方向 \( (1-(-3))/4 = 1 \)。z 方向只有 1 层——每个「体素」都是贯穿全高的柱子，这就是 pillar 的由来。`middle_encoder` 的 `output_shape=[496, 432]` 正是 (y格数, x格数)。

#### 4.2.4 代码实践

1. **实践目标**：用 Python 交互式验证「配置 → 网格形状」的换算，不依赖任何权重。
2. **操作步骤**（示例代码，可直接在仓库根目录的 Python 里执行；需要已安装 mmcv 编译版，若 import 报错则待本地验证）：

   ```python
   # 示例代码
   import torch
   from mmdet3d.models.data_preprocessors.voxelize import VoxelizationByGridShape

   layer = VoxelizationByGridShape(
       point_cloud_range=[0, -39.68, -3, 69.12, 39.68, 1],
       max_num_points=32,
       voxel_size=[0.16, 0.16, 4],
       max_voxels=(16000, 40000))
   print(layer.grid_shape)    # 期望 [432, 496, 1]
   print(layer.voxel_size)    # 期望 [0.16, 0.16, 4]

   # 反向：只给 grid_shape，让它反推 voxel_size
   layer2 = VoxelizationByGridShape(
       point_cloud_range=[0, -180, -4, 50, 180, 2],
       max_num_points=-1,
       grid_shape=[480, 360, 32],
       max_voxels=-1)
   print(layer2.voxel_size)   # 观察 (r_max-r_min)/(G-1) 的反推结果

   # hard 体素化一帧伪点云（10000 点，第 4 列模拟反射率）
   pts = torch.rand(10000, 4)
   pts[:, 0] = pts[:, 0] * 69.12          # x ∈ [0, 69.12)
   pts[:, 1] = pts[:, 1] * 79.36 - 39.68  # y ∈ [-39.68, 39.68)
   pts[:, 2] = pts[:, 2] * 4 - 3          # z ∈ [-3, 1)
   layer.eval()                            # 用测试档 max_voxels=40000
   voxels, coors, num_points = layer(pts)
   print(voxels.shape, coors.shape, num_points.shape)
   ```

3. **需要观察的现象**：`grid_shape` 恰为 `[432, 496, 1]`；`coors` 的形状是 `(M, 3)` 且 `M <= 40000`；`voxels` 是 `(M, 32, 4)`；`num_points` 每个值 `<= 32` 且总和远小于 10000（大量点被 `max_points=32` 截断丢弃）。
4. **预期结果**：上述形状关系成立。若把 `pts` 乘一个系数移到范围外，`M` 会明显变小（范围外的点被丢弃）。
5. 本实践依赖 mmcv 的 `_ext` 算子；在只装了 CPU 版或未编译扩展的环境会抛异常，此情形下请改做 4.4 节实践（纯 numpy），并在此标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`max_voxels=(16000, 40000)` 中两个数各在什么时候生效？为什么测试时的上限通常更大？

**答案**：构造时被 `_pair` 成二元组，forward 里训练用 `[0]`、测试用 `[1]`（[voxelize.py:L164-L168](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/voxelize.py#L164-L168)）。训练受显存与吞吐约束更紧，测试更看重不漏检，故上限更宽松。

**练习 2**：hard 模式下哪些点会被丢弃？

**答案**：三类：① 落在 `point_cloud_range` 之外的点；② 某体素内排在前 `max_num_points` 之后的多余点；③ 体素总数达到 `max_voxels` 后新出现的格子里的点。docstring 因此建议调用前洗牌，让丢弃随机化。

**练习 3**：`grid_shape=[480, 360, 32]` 反推 `voxel_size` 时为什么分母是 `G-1` 而不是 `G`？

**答案**：两个方向的约定不同（[voxelize.py:L146-L162](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/voxelize.py#L146-L162)）：`voxel_size → grid_shape` 把网格当作「范围按体素边长切分」的区间数，直接相除；`grid_shape → voxel_size` 则按「G 个格中心均匀覆盖范围」来解，两端格中心落在范围边界上，故间距是范围长除以 `G-1`。两种约定并存是历史接口兼容的结果，使用时只需记住：给定其中一个，另一个由构造函数自动补齐。

### 4.3 voxelize() 方法：batch 维如何编进 coors

#### 4.3.1 概念说明

`Det3DDataPreprocessor.voxelize()` 是「逐帧体素化 + batch 维编码」的组装车间。核心问题：逐帧调用体素化层后得到的是各帧独立的 `(M_i, ...)` 张量，把它们 `torch.cat` 到一起后，下游怎么知道每个体素属于哪一帧？

答案是：**在 `coors` 最左侧补一列帧编号**。`F.pad(res_coors, (1, 0), value=i)` 把第 `i` 帧的所有体素坐标变成 4 列 `(batch_idx, c_z, c_y, c_x)`。这样整个 batch 的体素合成了一个规整的整数张量，sparse conv / scatter 只需按第 0 列分组即可还原 batch。

另一个隐藏知识点是 **coors 的后三列是 `(z, y, x)` 倒序**而非 `(x, y, z)`。源码里有两处铁证：一是 `voxel_centers` 反算时用 `res_coors[:, [2, 1, 0]]` 把列倒回 xyz；二是 numpy 版 `VoxelGenerator` 的 reverse kernel 用 `coor[ndim_minus_1 - j] = c` 写入倒序坐标（4.4 节）。这与后续 `PointPillarsScatter` 期望的 BEV 行列顺序一致。

#### 4.3.2 核心流程

```text
voxelize(points: List[Tensor], data_samples)
  voxel_type == 'hard'（检测主线，PointPillars/SECOND/CenterPoint）:
      for i, 每帧 res in points:
          voxels_i, coors_i, num_points_i = self.voxel_layer(res)   # 单帧体素化
          voxel_centers_i = (coors_i[:, [2,1,0]] + 0.5) * voxel_size + range[:3]
          coors_i = F.pad(coors_i, (1,0), value=i)                  # ← batch 编码
      cat 所有帧 → voxel_dict = {voxels, coors, num_points, voxel_centers}
  voxel_type == 'dynamic':
      每帧只取 coors (N,3)，pad batch 维；voxels = torch.cat(points)（点即特征）
  voxel_type == 'cylindrical'（Cylinder3D 分割）:
      笛卡尔 → 极坐标 (rho, phi, z) → clamp → floor 除 voxel_size → coors
      并用 get_voxel_seg 生成体素级分割标签与 point2voxel_map
  voxel_type == 'minkunet'（MinkUNet 分割）:
      round(p / voxel_size) 量化 → sparse_quantize 去重（ravel_hash + np.unique）
      训练时按 max_voxels 随机采样体素；point2voxel_map 写回 data_sample
```

`voxel_centers` 的反算公式（把整数格坐标变回米制中心）：

\[ \mathbf{x}_{center} = \big([z, y, x] + 0.5\big) \odot v + (r_x^{\min}, r_y^{\min}, r_z^{\min}) \]

其中 \(\odot\) 表示逐元素相乘，`[z,y,x]` 换回 `[x,y,z]` 顺序后每维加半个格长。

#### 4.3.3 源码精读

hard 分支（本讲主线）：

- [data_preprocessor.py:L364-L381](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L364-L381) —— 逐帧调用 `self.voxel_layer(res)` 得到三件套；L369-L371 反算 `voxel_centers`（注意 `[:, [2, 1, 0]]` 的列重排，这是 coors 为 zyx 倒序的直接证据）；L372 用 `F.pad(..., (1, 0), value=i)` 在最左列写入帧号；最后四组张量各自 `torch.cat`。
- [data_preprocessor.py:L383-L384](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L383-L384) —— hard 模式额外把 `num_points` 与 `voxel_centers` 放进 voxel_dict（dynamic 模式没有这两个键）。
- [data_preprocessor.py:L465-L466](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L465-L466) —— 所有模式共同的出口：`voxel_dict['voxels']` 与 `voxel_dict['coors']`。

dynamic 分支：

- [data_preprocessor.py:L385-L393](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L385-L393) —— 每帧只拿 coors、pad batch 维；`voxels = torch.cat(points)`：动态体素化下「体素特征」就是原始点本身，聚合延迟到网络的 `DynamicScatter3D` 完成（[voxelize.py:L244-L318](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/voxelize.py#L244-L318)，其 forward 检测到 coors 是 4 列带 batch 维时逐帧散射再拼回）。

cylindrical 与 minkunet 分支（分割模型用，导览即可）：

- [data_preprocessor.py:L394-L426](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L394-L426) —— cylindrical：先转极坐标 `(rho, phi, z)`，clamp 到范围后 `floor` 除以 `voxel_size` 得 coors；`get_voxel_seg`（[L470-L492](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L470-L492)）借 `dynamic_scatter_3d` 对 one-hot 语义标签做 mean 再 argmax，生成体素级标签。
- [data_preprocessor.py:L427-L460](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L427-L460) —— minkunet：`round(p / voxel_size)` 量化后用 `sparse_quantize`（ravel_hash + `np.unique`，[L494-L542](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L494-L542)）去重；训练时体素数超过 `max_voxels` 则随机采样。注意 L448-L455：`batch_first=True` 时帧号 pad 在第 0 列、否则 pad 在最后一列——这正是构造函数里 `batch_first` 参数的含义。

下游消费者（体素字典的去向）：

- [mmdet3d/models/detectors/voxelnet.py:L36-L48](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/voxelnet.py#L36-L48) —— `VoxelNet.extract_feat` 取出 `batch_inputs_dict['voxels']`，把 `(voxels, num_points, coors)` 三件套喂给 `voxel_encoder`（如 `PillarFeatureNet`，签名见 [pillar_encoder.py:L95-L107](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/voxel_encoders/pillar_encoder.py#L95-L107)）；**L42 仅凭 `coors[-1, 0].item() + 1` 恢复 batch_size**——因为各帧按顺序 cat 且每帧 pad 了固定帧号，最后一行必然属于最后一帧。
- 随后 `middle_encoder`（`PointPillarsScatter`）用 `voxel_features + coors + batch_size` 把柱特征 scatter 回 `(B, C, 496, 432)` 的伪图像，进入 2D backbone（u5-l1 的主题）。

#### 4.3.4 代码实践

1. **实践目标**：亲手验证「逐帧 pad 帧号 → cat → 第 0 列即 batch 维」这一编码，并复现 `VoxelNet` 的 batch_size 推断技巧。
2. **操作步骤**（示例代码，复现 hard 分支的 pad/cat 逻辑，无需 GPU）：

   ```python
   # 示例代码：手工模拟 voxelize() 的 batch 编码（只用 coors，不跑 CUDA）
   import torch
   import torch.nn.functional as F

   coors_frame0 = torch.tensor([[0, 0, 431], [0, 1, 100], [1, 10, 200]])
   coors_frame1 = torch.tensor([[0, 0, 431], [2, 5, 50]])
   all_coors = torch.cat([
       F.pad(coors_frame0, (1, 0), mode='constant', value=0),
       F.pad(coors_frame1, (1, 0), mode='constant', value=1),
   ], dim=0)
   print(all_coors)
   print('batch_size =', all_coors[-1, 0].item() + 1)
   ```

3. **需要观察的现象**：拼接结果每行 4 列，前三行（frame0）第 0 列为 0，后两行（frame1）第 0 列为 1；即使两帧存在相同的 `(z,y,x)`（如 `(0,0,431)`），第 0 列仍能把它们区分开。
4. **预期结果**：`batch_size = 2`，与 `VoxelNet.extract_feat` 的推断方式一致。
5. 进阶（需 GPU + mmcv 编译版，否则待本地验证）：仿照官方测试的 cylindrical 用例（[test_data_preprocessor.py:L100-L128](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_data_preprocessors/test_data_preprocessor.py#L100-L128)），把 `voxel=True` 打开对两帧点云跑 `processor(inputs)`，检查 `out['inputs']['voxels']['coors'].shape == (N, 4)` 且第 0 列只有 0/1 两种取值。

#### 4.3.5 小练习与答案

**练习 1**：`coors` 的四列分别是什么？给出源码证据。

**答案**：`(batch_idx, z, y, x)`。证据一：`voxel_centers` 反算用 `res_coors[:, [2, 1, 0]]` 重排后才当 xyz 用（[data_preprocessor.py:L368-L371](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L368-L371)）；证据二：numpy 版 kernel 以 `coor[ndim-1-j] = c` 写入倒序坐标（4.4 节）。

**练习 2**：dynamic 模式的 `voxel_dict` 里有哪些键？和 hard 比缺了什么？

**答案**：只有 `voxels` 与 `coors`（`voxels` 即 cat 后的原始点）。缺 `num_points`（不定长无从统计每格点数上限）与 `voxel_centers`（聚合推迟到网络内）。

**练习 3**：为什么 `VoxelNet` 敢用 `coors[-1, 0] + 1` 推 batch_size？这个写法依赖什么前提？

**答案**：依赖两点：① `batch_first=True`（默认），帧号在第 0 列；② `voxelize` 按帧序循环 cat，最后一行必属最后一帧，其帧号 = batch_size - 1。若 `batch_first=False`（帧号在末列），该写法需改为 `coors[-1, -1]`。

### 4.4 VoxelGenerator：numpy/numba 参考实现

#### 4.4.1 概念说明

`VoxelGenerator`（位于 `mmdet3d/models/task_modules/voxel/`）是同一算法的 **numpy + numba 单线程实现**。它注册在 `MODELS` 节点却几乎不参与模型前向——它的真实用户是**数据管线**：`VoxelBasedPointSampler`（[transforms_3d.py:L1481-L1509](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L1481-L1509)）在 CPU 上用它对多 sweep 点云做「每体素最多留 N 点」的降采样。

两个实现的分工：

| | `VoxelGenerator` | `VoxelizationByGridShape` |
| --- | --- | --- |
| 实现 | numpy 数组 + numba jit 循环 | mmcv 编译算子（CUDA/CPU） |
| 输入 | `np.ndarray` | `torch.Tensor` |
| 使用位置 | dataset transforms（离线、CPU） | `Det3DDataPreprocessor`（模型前向） |
| batch 维 | 不管（单帧调用） | 由上层 `voxelize()` pad |

对学习者来说它是最好的阅读材料：算法逻辑全部摊开在 kernel 里，没有隐藏在编译扩展背后。

#### 4.4.2 核心流程

```text
points_to_voxel(points, voxel_size, coors_range, max_points, reverse_index=True, max_voxels)
  1. voxelmap_shape = round((range[3:] - range[:3]) / voxel_size)
     reverse_index=True 时把形状反转成 (D, H, W)   # zyx
  2. 预分配：
     voxels      (max_voxels, max_points, C)  全 0
     coors       (max_voxels, 3)              全 0
     num_points  (max_voxels,)                全 0
     coor_to_voxelidx  voxelmap_shape 形状的查找表，初值全 -1
  3. numba kernel 逐点：
     for 每个点 i:
        c_j = floor((p_ij - range_j) / voxel_size_j)
        若任一维出界 → 跳过
        reverse_index: coor[2-j] = c_j        # 存成 (z, y, x)
        idx = coor_to_voxelidx[z, y, x]
        若 idx == -1（新格子）：
            若 voxel_num >= max_voxels → 跳过
            登记新体素，coors[voxel_num] = coor
        若 num_points[idx] < max_points：
            voxels[idx, num] = points[i]；计数 +1
        否则：该点被静默丢弃
  4. 截取 [:voxel_num] 返回 (voxels, coors, num_points)
```

关键数据结构是 `coor_to_voxelidx` 查找表：一个形状等于完整体素网格的 int32 数组，值为 -1 表示该格还没被任何点占用，否则存该格在输出 `voxels` 里的行号。它把「这个格子是第几个体素」的查询做成 O(1)。

#### 4.4.3 源码精读

- [voxel_generator.py:L10-L11](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/voxel/voxel_generator.py#L10-L11) —— `@MODELS.register_module()` 的 `VoxelGenerator`（注意它注册在 MODELS 而非 TASK_UTILS，虽然放在 task_modules 目录下）。
- [voxel_generator.py:L22-L39](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/voxel/voxel_generator.py#L22-L39) —— 构造函数算 `grid_size = (range[3:] - range[:3]) / voxel_size` 后 round，与 4.2 的 \( G \) 公式一致。
- [voxel_generator.py:L41-L45](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/voxel/voxel_generator.py#L41-L45) —— `generate` 只是对 `points_to_voxel` 的薄封装，固定 `reverse_index=True`（输出 zyx 坐标）。
- [voxel_generator.py:L115-L124](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/voxel/voxel_generator.py#L115-L124) —— 计算 `voxelmap_shape` 并反转、预分配四个数组；查找表 `coor_to_voxelidx` 形状即完整体素网格。
- [voxel_generator.py:L136-L138](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/voxel/voxel_generator.py#L136-L138) —— 用 kernel 返回的真实体素数 `voxel_num` 截掉预分配的空段。
- [voxel_generator.py:L196-L215](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/voxel/voxel_generator.py#L196-L215) —— reverse kernel 的核心三步：`floor` 算格坐标并查界、`coor[ndim_minus_1 - j] = c` 写成 zyx 倒序、经查找表判断新格登记或旧格追加；`voxel_num >= max_voxels` 时 `continue` 跳过整个新体素；`max_points` 截断：格子已满则点不入 `voxels`、计数不增——丢弃是静默的。
- 真实调用方：[transforms_3d.py:L1499-L1506](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L1499-L1506) —— `VoxelBasedPointSampler` 用两个 `VoxelGenerator`（当前 sweep 与历史 sweep 各一个）做体素降采样。
- 官方单测：[tests/test_models/test_task_modules/test_voxel/test_voxel_generator.py:L7-L20](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_task_modules/test_voxel/test_voxel_generator.py#L7-L20) —— 固定随机种子的 20 个点、`voxel_size=[5,5,1]`，期望产生恰好 4 个体素、coors 为 `[[2,0,0],[3,0,0],[0,0,0],[1,0,0]]`、每格点数 `[5,5,5,3]`。

#### 4.4.4 代码实践

1. **实践目标**：用伪点云 + 0.16m 柱体尺寸手工调用 `VoxelGenerator`，对照官方单测理解 `coors`、`voxels`、`num_points` 三件套；再模拟两帧拼接，观察 batch 维编码。纯 CPU 可跑（依赖 numba，属 runtime 依赖）。
2. **操作步骤**（示例代码）：

   ```python
   # 示例代码：先复现官方单测，再做 0.16m 柱体实验
   import numpy as np
   from mmdet3d.models.task_modules.voxel import VoxelGenerator

   # ---- 实验 A：复现官方单测（结果可精确对照）----
   np.random.seed(0)
   vg = VoxelGenerator(voxel_size=[5, 5, 1],
                       point_cloud_range=[0, 0, 0, 20, 40, 4],
                       max_num_points=5)
   pts = np.random.uniform(0, 4, (20, 3))
   voxels, coors, num_points = vg.generate(pts)
   print(voxels.shape)      # 期望 (4, 5, 3)
   print(coors)             # 期望 [[2 0 0] [3 0 0] [0 0 0] [1 0 0]]
   print(num_points)        # 期望 [5 5 5 3]

   # ---- 实验 B：PointPillars 同款 0.16m 柱体 + 模拟 batch 维 ----
   vg2 = VoxelGenerator(voxel_size=[0.16, 0.16, 4],
                        point_cloud_range=[0, -39.68, -3, 69.12, 39.68, 1],
                        max_num_points=32,
                        max_voxels=16000)
   frame0 = np.random.uniform([0, -39.68, -3], [69.12, 39.68, 1],
                              size=(20000, 4)).astype(np.float32)
   frame1 = np.random.uniform([0, -39.68, -3], [69.12, 39.68, 1],
                              size=(15000, 4)).astype(np.float32)
   v0, c0, n0 = vg2.generate(frame0)
   v1, c1, n1 = vg2.generate(frame1)
   print('grid_size =', vg2.grid_size)          # 期望 [432. 496.   1.]，z 维 1 层=柱体
   print('frame0:', v0.shape, 'frame1:', v1.shape)

   # 模拟 Det3DDataPreprocessor.voxelize 的 batch 编码（numpy 版）
   batched_coors = np.concatenate(
       [np.pad(c0, ((0, 0), (1, 0)), constant_values=0),
        np.pad(c1, ((0, 0), (1, 0)), constant_values=1)], axis=0)
   print('batched_coors 形状:', batched_coors.shape)   # (M0+M1, 4)
   print('batch 维取值:', np.unique(batched_coors[:, 0]))  # 期望 [0 1]
   print('batch_size =', batched_coors[-1, 0] + 1)     # 期望 2
   print('体素内点数分布:', np.bincount(np.concatenate([n0, n1])))
   ```

3. **需要观察的现象**：
   - 实验 A 输出与官方断言逐位一致（`voxels.shape == (4, 5, 3)` 等）；
   - 实验 B 中 `grid_size` 的 z 维为 1（柱体）；两帧的 `coors` 都是 `(M, 3)` 且第 1 列（z）恒为 0；拼接后 `batched_coors` 第 0 列恰好只有 `{0, 1}`；`num_points` 的取值大量落在 0~32 之间且 32 处可能有截断堆积。
4. **预期结果**：如上。若把 `max_num_points` 改成 1 再跑实验 B，会看到「每柱只留 1 点」的极端降采样——这正是 `VoxelBasedPointSampler` 在数据管线里用它做采样时的用法。
5. 上述脚本未在本环境执行，具体数值「待本地验证」（实验 A 的期望值直接来自官方单测断言，可信度最高）。

#### 4.4.5 小练习与答案

**练习 1**：`VoxelGenerator` 注册在哪个注册节点？它的真实调用方在模型里还是数据管线里？

**答案**：注册在 `MODELS`。真实调用方是数据管线的 `VoxelBasedPointSampler`（[transforms_3d.py:L1499](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L1499)），模型前向用的体素化是 `VoxelizationByGridShape`。

**练习 2**：`coor_to_voxelidx` 查找表的作用是什么？没有它会怎样？

**答案**：它以完整体素网格为形状，把「格子 (z,y,x) → 输出体素行号」的映射做成 O(1) 随机访问，同时用 -1 标记「格子尚未出现」。没有它，判断新格子需要线性扫描已有 coors，复杂度从 O(N) 退化为 O(N·M)。

**练习 3**：官方单测里 20 个点只产生 4 个体素，且 `expected_coors` 的前两列全为 0，为什么？

**答案**：点从 `uniform(0, 4)` 采样，x、y、z 都落在 `[0, 4)`；按 `voxel_size=[5,5,1]`，y 方向 \( \lfloor y/5 \rfloor = 0 \) 必为 0。z 方向格宽恰为 1，理论上 z 列可取 0~3，但 `np.random.seed(0)` 采出的点在 z 上分布集中，断言中 z、y 两列为 0、只有 x 列非零（`[2,3,0,1]`，对应 \( \lfloor x/5 \rfloor \in \{0,1,2,3\} \)）——这正是单测必须固定种子的原因。

## 5. 综合实践

**任务：不借助任何数据集，把「点云 batch → 体素字典 → batch_size 恢复」整条链路在 CPU 上跑通并画出来。**

1. 用 4.4 节实验 B 的代码生成两帧不同点数（如 20000 与 15000）的伪点云（`float32`，4 列 = x,y,z,反射率），PointPillars 同款 `voxel_size=[0.16,0.16,4]` 与 `point_cloud_range=[0,-39.68,-3,69.12,39.68,1]`。
2. 逐帧调用 `VoxelGenerator.generate`，手工 pad 帧号后 cat，复现 `Det3DDataPreprocessor.voxelize` 的 hard 分支（[data_preprocessor.py:L364-L381](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/data_preprocessors/data_preprocessor.py#L364-L381)），得到 `voxels/coors/num_points` 三件套与 batched `coors`。
3. 用 `coors[:, [2,1,0]]`（z,y,x → x,y,z）反算 `voxel_centers`（加 0.5、乘 voxel_size、加 range 下界），随机抽 3 个体素，验证其中心坐标确实落在 `point_cloud_range` 内、且相邻格中心的 x 间距是 0.16。
4. 用 `batched_coors[-1, 0] + 1` 恢复 batch_size，与真实帧数对照。
5. 画一张数据流图：`List[Tensor]` →（逐帧 voxel_layer）→（pad 帧号）→（cat）→ `{voxels, coors, num_points, voxel_centers}` →（`PillarFeatureNet`）→（`PointPillarsScatter` + batch_size）→ 伪图像，并把每一步的张量形状标在图上。

预期结果：所有形状断言通过；第 3 步抽到的中心坐标满足 \((c + 0.5) \times 0.16 \in [0, 69.12]\)（x 维）。全程 CPU 可跑；若想再走一步 mmcv 版 `VoxelizationByGridShape`（4.2 实践），需要编译版 mmcv，无 GPU 环境标注「待本地验证」。

## 6. 本讲小结

- `Det3DDataPreprocessor` 是挂在模型上的 batch 第一站：图像路径做通道转换→归一化→pad→stack 成 `(B,3,H,W)`，键名 `img → imgs`；点云路径**不堆叠**，保留 `List[Tensor]` 并可选体素化。
- `VoxelizationByGridShape` 包装 mmcv 编译算子，hard 模式输出定长 `(M, max_points, C)` 三件套并受 `max_voxels=(训练, 测试)` 双上限约束（超过即静默丢点，故调用前应洗牌）；dynamic 模式只给每点 coors，聚合推迟到网络内。
- `voxel_size` 与 `grid_shape` 互斥且可互推，但两个方向公式不对称（除以 `G` vs 除以 `G-1`）；PointPillars 的 `voxel_size=[0.16,0.16,4]` 使 z 维只有 1 层格，体素退化为柱体（pillar），`grid_shape=[432,496,1]`。
- batch 信息编码在 `coors` 第 0 列（`F.pad(..., value=i)` + cat），后三列是 `(z,y,x)` 倒序；`VoxelNet.extract_feat` 用 `coors[-1,0]+1` 一行恢复 batch_size。
- voxel_dict 有四种风味：hard（检测主线，多 `num_points`/`voxel_centers` 两键）、dynamic、cylindrical（极坐标 + 体素分割标签）、minkunet（round 量化 + 哈希去重 + point2voxel_map）。
- `VoxelGenerator` 是同一算法的 numpy/numba CPU 版，真实用户是数据管线的 `VoxelBasedPointSampler`；其 `coor_to_voxelidx` 查找表把新格判定做成 O(1)，是理解体素化算法的最佳阅读入口。

## 7. 下一步学习建议

本讲结束于 `batch_inputs_dict['voxels']` 被送进检测器。下一讲 **u5-l1（VoxelNet 主流程与 PointPillars 全链路）** 正好从对面接住它：`PillarFeatureNet` 如何把 `(M, 32, 4)` 的原始点变成 `(M, 64)` 的柱特征、`PointPillarsScatter` 如何按 `coors` 把柱特征撒回 `(B, 64, 496, 432)` 伪图像。建议先读 [mmdet3d/models/voxel_encoders/pillar_encoder.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/voxel_encoders/pillar_encoder.py) 与 [mmdet3d/models/middle_encoders/pillar_scatter.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/middle_encoders/pillar_scatter.py)，重点看后者如何用 `coors[:, 0]` 分帧。若对分割分支感兴趣，可提前浏览 cylindrical/minkunet 两分支对应的 **u8-l1（Cylinder3D 与 MinkUNet）**。
