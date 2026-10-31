# 打包与预处理：从 pipeline 到 batch_inputs_dict

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `Pack3DDetInputs_Part_Glamm` 打包的每一个键，以及每个键最终落在 `inputs`、`gt_pts_seg`、`gt_instances_3d` 中的哪一条通道。
2. 完整追踪 `selected_qa_data` 从 `QA_Generation_v2` 产出，到 `Pack3DDetInputs_Part_Glamm` 打包，再到 `Det3DDataPreprocessor_` 组装，最终进入解码器 `qa_data` 参数的传递路径。
3. 解释 `Det3DDataPreprocessor_` 相对 mmdet3d 基类多做了什么、为什么必须重写。
4. 描述模型 `loss()` 收到的 `batch_inputs_dict` 的确切结构（键、类型、形状、按样本成列表的语义）。

本讲是单元 2（数据侧）的最后一讲：前四讲解决了「数据长什么样、怎么转换、怎么加载、怎么采样 QA」，本讲解决「**这一堆键如何变成模型能吃的两个参数**」。

## 2. 前置知识

### 2.1 OpenMMLab 数据流的「两条通道」

在 mmengine/mmdet3d 的世界里，一次前向的输入被拆成两条物理隔离的通道：

- **`inputs`（输入通道）**：模型前向真正要「消费」的数据——点云、图像、问题文本、场景名等。它最终成为 `loss(batch_inputs_dict, ...)` 的第一个参数。
- **`data_samples`（标注通道）**：监督信号与元信息，装在一个 `Det3DDataSample` 对象里，内部再细分为 `gt_pts_seg`（点级分割标注，`PointData` 类型）、`gt_instances_3d`（3D 实例标注）等子结构与 `metainfo`。

**输入走 inputs、监督走 data_samples**——这是贯穿本讲的主线口诀。同一个 transform 产出的两个键，可能就此分道扬镳：`selected_qa_data`（问题文本，模型要读）走 inputs，`sp_gt_seg`（分割真值，损失要读）走 data_samples。

### 2.2 从 results 字典到 batch：三段旅程

一个样本在进入模型前经历三段组装：

```text
transform 流水线（results: dict）
        │  ① Pack3DDetInputs_Part_Glamm.transform
        ▼
单样本打包 {'inputs': {...}, 'data_samples': Det3DDataSample}
        │  ② dataloader 的 pseudo_collate（mmengine 默认组批方式）
        ▼
{'inputs': {键: [样本0的值, 样本1的值, ...]}, 'data_samples': [样本0, 样本1, ...]}
        │  ③ Det3DDataPreprocessor_.simple_process
        ▼
{'inputs': batch_inputs_dict, 'data_samples': [Det3DDataSample, ...]}
        │  ④ Base3DDetector.forward 分发
        ▼
model.loss(batch_inputs_dict, batch_data_samples) / model.predict(...)
```

`pseudo_collate` 是 mmengine 默认的批组装函数，语义是「**按位装箱、不做堆叠**」：每个样本的同名值被收进一个 Python 列表，而不是拼成一个大张量。这对点云至关重要——不同场景点数、超点数都不一样，根本无法堆叠成规整张量。所以你会看到 `batch_inputs_dict['points']` 是一个「每场景一个张量」的列表。

### 2.3 data_preprocessor 挂在哪里

`model = dict(type='Grounded_LMM_main', data_preprocessor=dict(type='Det3DDataPreprocessor_'))` 中的 `data_preprocessor` 是模型的内置组件（见 [configs/ground3dlmm_train.py:L28-L30](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L28-L30)）。mmengine 的 `BaseModel.forward` 会先调用 `self.data_preprocessor(data, training)`，再把产物分发给 `loss()` / `predict()`。也就是说，**你写不到 forward 里的批处理逻辑，就写在 data_preprocessor 里**——这正是本项目需要一个 `Det3DDataPreprocessor_` 的原因。

### 2.4 术语表

| 术语 | 含义 |
|---|---|
| `Det3DDataSample` | mmdet3d 的单样本标注容器，含 `gt_pts_seg`、`gt_instances_3d`、`metainfo` 等字段 |
| `PointData` | 装点级分割标注的容器（`gt_pts_seg` 的类型） |
| `to_tensor` | mmdet3d 的工具函数，把 `np.ndarray` 转成 `torch.Tensor`（不拷贝数据） |
| `pseudo_collate` | 「假组批」：同名值进列表，不堆叠张量 |
| 白名单 | 配置里 `Pack3DDetInputs_Part_Glamm(keys=[...])` 的 `keys` 列表，只有同时出现在列表与 results 中的键才会被打包 |

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [uniseg3d/formatting.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/formatting.py) | 打包 transform（3 个变体类） | `Pack3DDetInputs_Part_Glamm` 的键分组与打包循环 |
| [uniseg3d/data_preprocessor.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/data_preprocessor.py) | 批预处理组件 | `simple_process` 的逐键透传 |
| [configs/ground3dlmm_train.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py) | 训练配置 | pack 的 `keys` 白名单、data_preprocessor 挂载、batch_size=1 |
| [uniseg3d/transforms_3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py) | 数据变换 | `QA_Generation_v2` 产出 `selected_qa_data` / `sp_gt_seg`；`ElasticTransfrom` 产出 `elastic_coords` |
| [uniseg3d/uniseg3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py) | 模型主体 | `Grounded_LMM_main.loss()` 如何消费 `batch_inputs_dict` |
| [uniseg3d/scannet_dataset.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/scannet_dataset.py) | 数据集类 | `scene_name` 的来源 |
| [uniseg3d/query_decoder.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py) | 查询解码器 | `Grounded_Decoder_Joint.forward` 接收 `qa_data`（传递路径终点） |

## 4. 核心概念与源码讲解

### 4.1 Pack3DDetInputs_Part_Glamm：键收集与去向

#### 4.1.1 概念说明

`Pack3DDetInputs_Part_Glamm` 是整条 transform 流水线的**最后一站**。它面前是一个被十几个 transform 反复加工过的 `results` 字典：点云、四类 mask、超点 GT 表、采样好的 QA、弹性坐标……模型没法直接吃一个杂乱的大字典，`Pack` 的职责就是「**分拣装箱**」：

1. 把 numpy 数组转成张量（`to_tensor`），字符串、字典、字典列表原样保留；
2. 按「四类键组」把每个键分拣到正确通道：模型输入 → `inputs`，点级分割标注 → `gt_pts_seg`，3D 实例标注 → `gt_instances_3d`，元信息 → `metainfo`；
3. 组装成 `{'inputs': dict, 'data_samples': Det3DDataSample}` 交给 dataloader。

它继承自 mmdet3d 的 `Pack3DDetInputs`（尾下划线命名规则见 u1-l3：`_Part_Glamm` 后缀表示为 Ground3D 数据专门改写的版本）。改写的动机有两个：**新增键组**（`selected_qa_data`、`sp_gt_seg` 等 Ground3D 专属键必须登记进某个键组，否则打包循环直接抛 `NotImplementedError`），以及**放宽类型转换**（QA 数据是「字典的列表」，不能一刀切转张量）。

#### 4.1.2 核心流程

```text
pack_single_results(results):
    1. points 若是 BasePoints 对象 → 取 .tensor
    2. img（若存在）→ HWC 转 CHW 张量
    3. 类型规范化循环（对约 30 个已知键）：
       - 值是 list：若元素是 ndarray/Tensor → 逐个 to_tensor；
         否则（元素是 dict 等）→ 原样保留
       - 值是 str / dict → 原样保留
       - 值是 ndarray / Tensor → to_tensor
       - 其他 → 原样保留
    4. 新建 Det3DDataSample；把 meta_keys 里的元信息 set_metainfo
    5. 分拣循环（只遍历配置 keys 白名单 ∩ results 现有键）：
       key ∈ INPUTS_KEYS       → inputs[key]
       key ∈ INSTANCEDATA_3D_KEYS → gt_instances_3d[去 gt_ 前缀]
       key ∈ INSTANCEDATA_2D_KEYS → gt_instances[去 gt_ 前缀]（本配置不涉及）
       key ∈ SEG_KEYS          → gt_pts_seg[去 gt_ 前缀]（值为 None 时跳过）
       都不在                    → raise NotImplementedError
    6. data_sample 挂上 gt_instances_3d / gt_instances / gt_pts_seg / eval_ann_info
    7. 返回 {'inputs': inputs, 'data_samples': data_sample}
```

注意第 5 步的「白名单 ∩ 现有键」语义：配置 `keys` 里写了 `gt_labels_3d`、`is_novel`、`pts_instance_objextId_shuffle`、`point_prompt_distance_norms`，但本管线并没有任何 transform 产出它们，于是 `if key in results` 检查不通过，直接跳过——**配置白名单是超集，实际打包以 results 里真实存在的键为准**。

#### 4.1.3 源码精读

**键组定义**——先看三个键组如何划分（这是本类的「路由表」）：

[uniseg3d/formatting.py:L159-L182](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/formatting.py#L159-L182) 注册了 `Pack3DDetInputs_Part_Glamm` 并定义三组键：

- `INPUTS_KEYS` 末尾新增了 `selected_qa_data`（L167）——QA 文本是**模型输入**，必须走 inputs 通道直达解码器；
- `SEG_KEYS` 新增了 `sp_gt_seg`（L176）——分割真值是**监督信号**，走 `gt_pts_seg` 通道；
- `INSTANCEDATA_3D_KEYS` 含 `gt_sp_masks`、`is_novel`——超点级实例表归 3D 实例标注容器。

L177 有一行注释 `# 'sp_gt_seg'  # moved to INPUTS_KEYS, no longer used as SEG_KEY`，但代码里 `sp_gt_seg` 实际**仍在** `SEG_KEYS` 中——注释与代码不一致，以代码为准（后面 4.2 节会看到这个不一致带来的一个「死分支」）。

**类型规范化循环**——Ground3D 数据形态多样，基类「一律转张量」的循环被改成了按类型分发：

[uniseg3d/formatting.py:L245-L262](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/formatting.py#L245-L262)：值是列表时先看首元素类型——是 `np.ndarray`/`torch.Tensor` 才逐个 `to_tensor`（`sp_gt_seg` 这种「每条 QA 一个张量」的列表走这里）；字符串、字典（`selected_qa_data` 的元素是问答 dict）则原样保留。这一段是相对基类最重要的修改：没有它，`selected_qa_data` 会在类型转换阶段被破坏。

**分拣循环**：

[uniseg3d/formatting.py:L285-L307](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/formatting.py#L285-L307)：遍历 `self.keys`（即配置传入的白名单），按四类键组分拣。相对基类新增了 SEG 分支的 None 保护（L297-L302）：`PointData` 不接受 `None`，而 `pts_part_mask`/`pts_object_mask` 在路径缺失时会被 `LoadAnnotations3D_` 置为 `None`（见 u2-l3），这里静默跳过而不是崩溃。任何不在四组里的键直接 `raise NotImplementedError`（L303-L307）——这是逼着你「新键必须登记」的守门员。

**组装收尾**：

[uniseg3d/formatting.py:L274-L283](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/formatting.py#L274-L283) 与 [uniseg3d/formatting.py:L309-L321](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/formatting.py#L309-L321)：创建 `Det3DDataSample`、写入元信息（`meta_keys` 未在配置中显式给出，使用基类 `__init__` 的默认值——一组路径/形状/翻转类元信息键，具体清单可在本地用 `pack.meta_keys` 打印确认，它是实例属性）、挂上三个标注容器与 `eval_ann_info`，最后返回 `{'inputs': ..., 'data_samples': ...}`。

**`_remove_prefix` 的效果**：键写入容器时会去掉 `gt_` 前缀（基类方法）。所以 `gt_sp_masks` 存进 `gt_instances_3d` 后叫 `sp_masks`；而 `sp_pts_mask`、`sp_gt_seg` 本来就没有 `gt_` 前缀，名字不变——这正好和模型侧的读取代码对上（`gt_pts_seg.sp_pts_mask`、`gt_pts_seg.sp_gt_seg`，见 4.3.3）。

**配置中的白名单**：

[configs/ground3dlmm_train.py:L150-L158](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L150-L158)：`Pack3DDetInputs_Part_Glamm` 是 train_pipeline 的最后一个 transform，`keys` 白名单共列 14 个键（含 4 个实际不会出现的键，另有 3 个 point/text prompt 键被注释掉）。评估配置使用完全相同的键列表（见 [configs/ground3dlmm_eval_ground3d_scannet.py:L202-L210](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L202-L210)），保证训练与推理吃到同构的数据。

据此可以画出**训练配置下每个真实存在键的去向表**：

| 键 | 产出者 | 类型 / 形状 | 打包去向 |
|---|---|---|---|
| `points` | LoadPointsFromFile + 几何增强 + NormalizePointsColor_ | Tensor (N, 6)，RGB∈[-1,1] | `inputs['points']` |
| `elastic_coords` | ElasticTransfrom | Tensor (N, 3)（体素尺度坐标副本） | `inputs['elastic_coords']` |
| `selected_qa_data` | QA_Generation_v2 | list[dict] 或 list[list[dict]]（每条含 question/answer/qa_keys/qa_type） | `inputs['selected_qa_data']` |
| `scene_name` | Part3DGlamm_Dataset.parse_data_info | str | `inputs['scene_name']` |
| `sp_pts_mask` | LoadAnnotations3D_（with_sp_mask_3d=True） | int64 Tensor (N,) | `gt_pts_seg.sp_pts_mask` |
| `pts_part_mask` / `pts_object_mask` | LoadAnnotations3D_（路径存在时） | int64 Tensor (N,)，缺失为 None | `gt_pts_seg.*`（None 则跳过） |
| `sp_gt_seg` | QA_Generation_v2 | list[Tensor]，第 i 个形状 (该条 QA 的 `<p>` 数, 超点数 S)，与 `selected_qa_data` 逐索引对应 | `gt_pts_seg.sp_gt_seg` |
| `gt_sp_masks` | AddSuperPointAnnotations_Part3DGlamm | Tensor (实例数, S) 超点级实例表 | `gt_instances_3d.sp_masks` |
| `gt_labels_3d` / `is_novel` / `pts_instance_objextId_shuffle` / `point_prompt_distance_norms` | （本管线未产出） | — | 白名单命中但 results 缺失 → 跳过 |

其中 `selected_qa_data` 每个元素的四个字段来自 [uniseg3d/transforms_3d.py:L444-L448](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L444-L448)（`qa_keys`、`qa_type` 在展平时打上），`sp_gt_seg` 的构建在 [uniseg3d/transforms_3d.py:L499-L541](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L499-L541)（u2-l4 已详述）。`elastic_coords` 由 [uniseg3d/transforms_3d.py:L1079-L1083](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1079-L1083) 写入——注意概率 `p` 只控制**是否加噪**，坐标副本本身总会生成，所以这个键在训练时恒存在。

#### 4.1.4 代码实践

**实践目标**：不下载任何数据、不启动训练，用一份合成 `results` 字典离线驱动 `Pack3DDetInputs_Part_Glamm`，亲眼验证「同一个 transform 产出的键被分拣到两条通道」。

**操作步骤**（示例代码，需在 u1-l2 搭好的环境中运行）：

```python
# practice_pack.py —— 示例代码（非项目原有文件）
import torch
from uniseg3d.formatting import Pack3DDetInputs_Part_Glamm

S = 5      # 假设本帧有 5 个超点
N = 20     # 假设本帧有 20 个点

pack = Pack3DDetInputs_Part_Glamm(keys=[          # 与训练配置相同的白名单
    'points', 'gt_labels_3d', 'pts_part_mask', 'pts_object_mask', 'selected_qa_data',
    'sp_pts_mask', 'gt_sp_masks', 'elastic_coords', 'is_novel', 'sp_gt_seg',
    'pts_instance_objextId_shuffle', 'point_prompt_distance_norms', 'scene_name'])

results = {
    'points': torch.randn(N, 6),                                   # 模拟归一化后的 XYZ+RGB
    'elastic_coords': torch.randn(N, 3).numpy(),                   # ndarray，验证 to_tensor
    'sp_pts_mask': torch.randint(0, S, (N,), dtype=torch.int64),
    'pts_part_mask': None,                                         # 模拟路径缺失
    'sp_gt_seg': [torch.ones(1, S)],                               # 1 个 <p> 标签 → (1, S)
    'selected_qa_data': [{'question': 'Where is the handle? <|point|>',
                          'answer': '<p>handle</p><SEG>',
                          'qa_keys': 'part',
                          'qa_type': 'part_qa_data_xxx'}],
    'scene_name': 'scene0000_00_frame_0',
}

packed = pack(results)
print('inputs 键        :', sorted(packed['inputs'].keys()))
ds = packed['data_samples']
print('gt_pts_seg 键    :', sorted(ds.gt_pts_seg.keys()))
print('gt_instances_3d 键:', ds.gt_instances_3d.keys())
print('selected_qa_data 原样性:', packed['inputs']['selected_qa_data'][0]['answer'])
print('sp_gt_seg 形状    :', ds.gt_pts_seg.sp_gt_seg[0].shape)
print('meta_keys 默认值 :', pack.meta_keys)   # 实例属性，来自基类 __init__ 默认值
```

**需要观察的现象**：

1. `inputs` 只含 4 个键：`points`、`elastic_coords`、`selected_qa_data`、`scene_name`；
2. `gt_pts_seg` 含 `sp_pts_mask`、`sp_gt_seg`，但**不含** `pts_part_mask`（None 被跳过）；
3. `elastic_coords` 从 ndarray 变成了 Tensor，而 `selected_qa_data` 仍是 dict 列表（未被破坏）；
4. `gt_instances_3d` 为空容器（`gt_sp_masks` 未在合成数据里给出；真实管线中会含 `sp_masks`）。

**预期结果**：上述 4 条全部符合即通过。若把一个未登记的新键（如 `'my_key'`）同时加进 `keys` 与 `results`，应观察到 `NotImplementedError`——这就是分拣循环的守门行为。

**待本地验证**：本脚本依赖完整环境（`import uniseg3d` 会连带加载 spconv/MinkowskiEngine/transformers），打印的具体键顺序可能因 mmdet3d 版本略有差异，以实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `selected_qa_data` 从 `INPUTS_KEYS` 移到 `SEG_KEYS`，会发生什么？

**参考答案**：它会走 `gt_pts_seg['selected_qa_data']` 通道，`inputs` 里不再有这个键。连锁反应有两处：一是 `Det3DDataPreprocessor_.simple_process` 中 `if 'selected_qa_data' in inputs` 永远为假，`batch_inputs['selected_qa_data']` 消失；二是 `Grounded_LMM_main.loss()` 里 `batch_inputs_dict['selected_qa_data']` 会直接 `KeyError`。这印证了「问题文本是模型输入，必须走 inputs 通道」的设计。

**练习 2**：配置 `keys` 白名单里的 `gt_labels_3d` 为什么不会导致报错，也不会出现在打包结果里？

**参考答案**：分拣循环对每个键先做 `if key in results` 检查（[uniseg3d/formatting.py:L286-L287](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/formatting.py#L286-L287)）。本管线的 `LoadAnnotations3D_` 配置了 `with_label_3d=False`，不产出 `gt_labels_3d`，该键被静默跳过。白名单是「允许打包的键」，不是「必须打包的键」。

**练习 3**：`sp_gt_seg` 是「张量的列表」，为什么它在类型规范化循环里没有整体变成一个大张量？

**参考答案**：循环的 list 分支只做「逐元素 `to_tensor`」（[uniseg3d/formatting.py:L245-L248](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/formatting.py#L245-L248)），不做拼接。因为不同 QA 的 `<p>` 标签数不同，各元素形状 (p_i, S) 不一致，无法堆叠；保持列表形态正是为了在 `loss()` 里与 `selected_qa_data` 逐索引对应，再按场景 `torch.cat`（见 4.3.3）。

### 4.2 Det3DDataPreprocessor_：从样本列表到 batch_inputs_dict

#### 4.2.1 概念说明

`Det3DDataPreprocessor_` 解决的问题是：**批组装后的自定义键如何进入模型输入**。mmdet3d 基类 `Det3DDataPreprocessor` 的 `simple_process` 只负责三类输入——`points`（可选取体素化）、`imgs`（归一化/padding/格式转换），以及它们的组合；凡是基类没登记的键，都进不了它返回的 `inputs` 字典。而 Ground3D-LMM 需要往前传 `elastic_coords`、`scene_name`、`selected_qa_data` 等十多个新键，所以必须重写。

重写的方式非常「朴素」：保留基类全部逻辑，再**逐键透传**新字段。子类源码中 `# new fields added by Part3d Glamm` 的注释（[uniseg3d/data_preprocessor.py:L106-L111](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/data_preprocessor.py#L106-L111)）直接表明这些分支是为新键专门添加的。

顺带一提，它还有一个跨版本兼容的小改动：基类中直接访问 `self.boxtype2tensor` 的代码，在子类里被包上了 `hasattr` 判断（L54-L61），以兼容没有这些属性的 mmdet3d 版本——读源码时遇到这种「防御式包裹」，通常是作者在多版本环境下踩过坑。

#### 4.2.2 核心流程

```text
simple_process(data, training):
    1. 若 inputs 里有 img → 计算批内 padding 形状
    2. collate_data(data)   # 继承自基类：整理成 {'inputs': ..., 'data_samples': 列表}
    3. batch_inputs = dict()
    4. points  → batch_inputs['points']（本配置未启用 voxel，跳过体素化）
       elastic_coords → batch_inputs['elastic_coords']
       imgs     → 归一化/padding/批增强后 → batch_inputs['imgs']（本配置无图，跳过）
    5. 逐键透传 13 个自定义键（存在才透传）：
       point_prompts / point_prompt_instance_ids / point_prompt_sp_ids /
       emb_text / label_text / gt_text_prompt / text_token / text_object_id /
       pts_instance_objextId_shuffle / point_prompt_distance_norms /
       scene_name / selected_qa_data / sp_gt_seg
    6. 返回 {'inputs': batch_inputs, 'data_samples': data_samples}
```

训练配置（[configs/ground3dlmm_train.py:L28-L30](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L28-L30)）没有传任何参数给 `Det3DDataPreprocessor_`，因此 voxel/图像归一化等开关全是默认关闭/空转——本项目中它**几乎纯粹是一个透传组装器**。

#### 4.2.3 源码精读

**入口与组装骨架**：

[uniseg3d/data_preprocessor.py:L9-L37](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/data_preprocessor.py#L9-L37)：先 `collate_data`（继承自基类，把 dataloader 喂进来的数据整理成 `inputs` / `data_samples` 两部分），再新建空的 `batch_inputs` 字典。`points` 分支保留基类行为（L29-L34），`voxel` 开关默认关闭；紧接着的 `elastic_coords` 透传（L36-L37）是第一个新键分支。

**图像分支**：

[uniseg3d/data_preprocessor.py:L39-L71](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/data_preprocessor.py#L39-L71)：整段处理 `imgs`（归一化、padding 元信息写回 data_samples、批增强）。注意它检查的是 `'imgs' in inputs`（带 s），而 Pack 阶段的键叫 `img`（不带 s）——图像是在这里才被堆叠成批张量的。3D-only 训练配置的 pipeline 不加载图像，此分支整体不触发；Joint 变体的图像如何进入消息列表属于 u4-l6 的内容，此处只需知道「图不从这里走模型输入」。

**自定义键透传**：

[uniseg3d/data_preprocessor.py:L73-L111](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/data_preprocessor.py#L73-L111)：13 个 `if 'xxx' in inputs: batch_inputs['xxx'] = inputs['xxx']`。对照 4.1.3 的去向表可知，训练配置下真正会触发的只有 4 个：`elastic_coords`、`scene_name`、`selected_qa_data`（L107-L108），加上骨架里的 `points`。其余 9 个分支服务于文件中其他模型变体（point/text prompt 系列）或数据集。

**一个值得注意的「死分支」**：L110-L111 的 `sp_gt_seg` 透传分支**在本管线下永远不触发**——因为 `Pack3DDetInputs_Part_Glamm` 把 `sp_gt_seg` 登记在 `SEG_KEYS`（formatting.py L176），它去了 `gt_pts_seg`，`inputs` 里根本不会有这个键（L177 那行「moved to INPUTS_KEYS」的注释描述的是一次没有最终完成的迁移）。GT 的真实通道是 data_samples，见 4.3.3。

**返回契约**：

[uniseg3d/data_preprocessor.py:L113](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/data_preprocessor.py#L113)：返回 `{'inputs': batch_inputs, 'data_samples': data_samples}`。mmengine 的 `BaseModel.forward` 拆开这两个字段，分别作为 `loss(batch_inputs_dict, batch_data_samples)` 的两个参数——`batch_inputs_dict` 这个名字就是从这里来的。

#### 4.2.4 代码实践

**实践目标**：通过「配置 × 代码」交叉对照，推导出训练时 `simple_process` 会透传哪些键；再用脚本模拟一次批组装验证推导（可选）。

**操作步骤**：

1. 打开 [uniseg3d/data_preprocessor.py:L29-L111](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/data_preprocessor.py#L29-L111)，列出所有写入 `batch_inputs` 的键（共 15 个：points、voxels、imgs、elastic_coords + 12 个透传键）。
2. 对照 4.1.3 的去向表，勾出「训练配置下 inputs 里真实存在的键」。
3. （可选，示例代码）接 4.1.4 的脚本继续：

```python
# practice_preproc.py —— 示例代码（非项目原有文件）
from mmengine.dataset import pseudo_collate     # 模拟 dataloader 的默认组批
from uniseg3d.data_preprocessor import Det3DDataPreprocessor_

prep = Det3DDataPreprocessor_()
batch = pseudo_collate([packed])                # packed 来自 4.1.4
out = prep.simple_process(batch, training=True)
print('batch_inputs_dict 键:', sorted(out['inputs'].keys()))
print('points 是列表吗    :', isinstance(out['inputs']['points'], list))
print('QA 仍在吗          :', out['inputs']['selected_qa_data'][0][0]['qa_type'])
```

**需要观察的现象**：

1. 推导结果：训练配置下 `batch_inputs_dict` 只含 `points`、`elastic_coords`、`scene_name`、`selected_qa_data` 四个键；
2. `points` 是长度为 1 的列表（batch_size=1），元素是 (N, 6) 张量——印证 pseudo_collate 的「按位装箱」语义；
3. `selected_qa_data[0]` 对应样本 0 的整条 QA 列表，结构未被破坏。

**预期结果**：推导与脚本输出一致即通过；同时确认 `sp_gt_seg` **不在** `batch_inputs_dict` 中。

**待本地验证**：第 3 步依赖本地 mmengine/mmdet3d 版本的 `collate_data` 实现，个别版本对 dict 型 inputs 的处理细节略有差异；若脚本报错，可改为在真实训练第一个 iteration 处打印 `batch_inputs_dict.keys()` 观察。

#### 4.2.5 小练习与答案

**练习 1**：为什么本项目必须重写 `simple_process`，而不是把新键留在 `data_samples` 里让模型自己取？

**参考答案**：两条路线其实都可行，且项目两条都在用——**监督信号**（`sp_gt_seg`、`sp_pts_mask`）确实走 data_samples 由 `loss()` 自取；但**前向输入**（问题文本 `selected_qa_data`、场景名、弹性坐标）在语义上属于 inputs 通道，且 `loss()`/`predict()` 的签名以 `batch_inputs_dict` 统一接收（见 4.3.3），让模型代码用同一种方式拿到所有输入。若全塞进 data_samples，模型侧就得在每个变体里重复「从 sample 里抠输入」的代码。拆成两条通道是 OpenMMLab 的既定设计，重写 preprocessor 是让新键合规进入 inputs 通道的最小改动。

**练习 2**：`simple_process` 里 `if 'img' in data['inputs']` 与 `if 'imgs' in inputs` 一个在 collate 前、一个在 collate 后，且拼写不同，这是 bug 吗？

**参考答案**：不是。前者是 Pack 阶段的键名 `img`（单样本单图），用于提前计算批内 padding 形状；后者是批组装后的键名 `imgs`（mmdet3d 基类在 collate/堆叠后使用复数键）。这是从 mmdet3d 基类沿袭的命名约定。本项目 3D-only 配置两者都不触发，读代码时不必纠结。

**练习 3**：如果把 batch_size 从 1 调到 4（配置 [configs/ground3dlmm_train.py:L163](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L163)），`batch_inputs_dict['selected_qa_data']` 会变成什么形状？

**参考答案**：变成长度 4 的列表，第 i 个元素是第 i 个场景自己的 QA 列表（其长度仍是该场景采样的 `num_qa=10` 条左右）。pseudo_collate 只加一层「按样本」的列表，不会拍平内层结构。`loss()` 里的解码器调用也正是按样本逐个处理这层列表（`for i, scene_qa in ...` 的消费方式见 u4-l3）。

### 4.3 batch_inputs_dict 的最终结构：模型 loss() 如何逐键消费

#### 4.3.1 概念说明

前两个模块讲的是「怎么装」，本模块讲「怎么拆」。`Grounded_LMM_main.loss()` 是整条数据链路的终点验收方：它拿到 `batch_inputs_dict`（输入通道）与 `batch_data_samples`（标注通道）后，一边把 `points`/`elastic_coords` 送进稀疏卷积主干提取超点特征，一边把 `selected_qa_data`/`scene_name` 送给语言解码器，最后把 `gt_pts_seg` 里的真值对齐成损失函数要的形状。理解这段「验收代码」，前面所有的打包规则就都有了为什么。

#### 4.3.2 核心流程

```text
loss(batch_inputs_dict, batch_data_samples):
    A. GT 对齐（标注通道）：
       遍历 batch 内每个 sample：
         - 读 gt_pts_seg.sp_pts_mask，累加 superpoint_bias 得全局超点偏移 batch_offsets
         - 把该场景的 sp_gt_seg（多条 QA 的 (p_i, S_i)）沿 dim=0 拼接 → sp_gt_masks[i]
    B. 特征提取（输入通道）：
       - collate(points 列表, elastic_coords 列表) → 稀疏坐标/特征/inverse_mapping
       - SpConvUNet 前向 + scatter_mean 超点池化（u3 系列详述）
    C. 语言分支（输入通道）：
       - raw_positions = 各场景 points[:, :3] 竖向拼接 → scatter_mean 得超点质心
       - decoder(x, scene_names, qa_data=selected_qa_data, point_positions) → language_losses
    D. 汇合：
       criterion(解码输出, sp_gt_masks, language_losses) → 总损失
```

#### 4.3.3 源码精读

**GT 对齐——batch_offsets 与 sp_gt_masks**：

[uniseg3d/uniseg3d.py:L1111-L1125](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1111-L1125)：因为 pseudo_collate 不堆叠张量，batch 内每个场景的超点编号都从 0 开始。模型用 `superpoint_bias` 累加：场景 0 的超点编号不变，场景 1 的整体加上「场景 0 的超点数 + 1」，依此类推，得到全局 `batch_offsets`。同时每个场景 `gt_pts_seg.sp_gt_seg`（张量列表）被 `torch.cat` 成一条——**这一步就是 4.1 练习 3 里「保持列表形态」的最终消费点**。注意这里读的全是 `batch_data_samples[i].gt_pts_seg`，印证 GT 走标注通道。

**特征提取的输入**：

[uniseg3d/uniseg3d.py:L1127-L1135](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1127-L1135)：`self.collate(batch_inputs_dict['points'], batch_inputs_dict.get('elastic_coords', None))`——`points` 被当作列表遍历，`.get` 的默认值 None 说明 `elastic_coords` 缺失时模型会退回原始坐标量化。`collate`/`extract_feat` 的内部机制属于 u3-l2/u3-l3，本讲只需记住：**稀疏主干的输入完全来自 inputs 通道**。

**语言分支的输入——selected_qa_data 的终点**：

[uniseg3d/uniseg3d.py:L1165-L1172](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1165-L1172)：先用 `torch.vstack([p[:, :3] for p in batch_inputs_dict['points']])` 把各场景坐标拼成全批点集（又一次证明 `points` 是列表），scatter_mean 得到每个超点的质心 `point_positions`；随后取出 `scene_names` 与 `selected_qa_data`，连同超点特征一起传入解码器。接收端签名见 [uniseg3d/query_decoder.py:L1962-L1978](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1962-L1978)：`forward(x, scene_names=None, qa_data=None, point_positions=None)`——`qa_data` 就是 `selected_qa_data` 换了个名字，解码器如何把它变成多轮对话与 `<SEG>` 查询是 u4-l3/u4-l4 的主题。

**汇合点**：

[uniseg3d/uniseg3d.py:L1184](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1184)：`self.criterion(x, sp_gt_masks, language_losses, original_queries=x['original_queries'])`——输入通道的产物（语言损失）与标注通道的产物（分割真值）在这里合流。`predict()` 分支（[uniseg3d/uniseg3d.py:L1251](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1251) 起）以同样方式消费 `scene_name`/`selected_qa_data`（L1301-L1303），区别只是不再需要 GT 对齐。

**完整结构清单**（训练配置、batch_size=1）：

```text
batch_inputs_dict = {
  'points':           [Tensor(N, 6)],        # 每场景一个张量；前 3 列 XYZ、后 3 列归一化 RGB
  'elastic_coords':   [Tensor(N, 3)],        # 体素尺度坐标（可能加过弹性噪声）
  'scene_name':       ['scene0000_00_frame_0'],   # str，来自 parse_data_info
  'selected_qa_data': [[{question, answer, qa_keys, qa_type}, ...]],  # 每场景其 QA 列表
}
batch_data_samples = [Det3DDataSample(
  gt_pts_seg = PointData{ sp_pts_mask: Tensor(N,), sp_gt_seg: [Tensor(p, S)], ...},
  gt_instances_3d = InstanceData_{ sp_masks: Tensor(实例数, S)},
  metainfo = {…基类默认元信息…},
  eval_ann_info = {…评估用标注…})]
```

#### 4.3.4 代码实践

**实践目标**：用「只读不改」的方式，在真实训练启动的第一个 iteration 观察一次 `batch_inputs_dict`，把本讲推导的结构与运行时对质。

**操作步骤**：

1. 确认已完成 u1-l2 环境与 u2-l2 数据转换（需要 `part3d_glamm_infos_train.pkl` 及对应 bin 数据）。
2. 阅读训练入口 [tools/train.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/train.py)（u5-l3 会详解），确认它只是标准 Runner 启动器。
3. 选择以下任一观察手段（均为**本地临时实验**，观察后请还原，不要提交改动）：
   - 在 [uniseg3d/uniseg3d.py:L1109](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1109) 附近临时加一行日志：`print({k: type(v[0]).__name__ if isinstance(v, list) else type(v).__name__ for k, v in batch_inputs_dict.items()})`；
   - 或用调试器在 `loss()` 入口下断点，展开 `batch_inputs_dict` 与 `batch_data_samples[0].gt_pts_seg`。
4. 发起一次短训练（1 个 iteration 即可中断），例如参考 [train.sh](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/train.sh) 的启动方式（完整训练流程属 u5-l3）。

**需要观察的现象**：

1. `batch_inputs_dict` 的键集 = {points, elastic_coords, scene_name, selected_qa_data}；
2. `points[0]` 形状为 (N, 6)，N 为该场景点数（十万量级）；`elastic_coords[0]` 为 (N, 3)；
3. `gt_pts_seg.sp_gt_seg` 是长度约为 `num_qa=10` 的张量列表，且 `len(selected_qa_data[0]) == len(gt_pts_seg.sp_gt_seg)`（逐索引对应契约）；
4. 多数 grounding 类 QA 的 `sp_gt_seg[i]` 形状为 (1, S) 或 (2, S)——`<p>` 标签数封顶 2（u2-l4）。

**预期结果**：四条现象全部吻合即通过；若第 3 条对不上，说明数据管线上某个 transform 被改动过，需回查 u2-l4 的采样逻辑。

**待本地验证**：本实践依赖真实数据与 GPU 训练环境；无法启动训练时，可退回 4.1.4/4.2.4 的离线合成脚本完成同类观察。

#### 4.3.5 小练习与答案

**练习 1**：`loss()` 里为什么需要 `superpoint_bias` 这套偏移算术？pseudo_collate 不做这件事吗？

**参考答案**：pseudo_collate 只负责「按位装箱」，不理解超点编号的语义——每个场景的 `sp_pts_mask` 都独立从 0 编号。而后续 `extract_feat` 会把全批超点特征拼成一个大张量统一处理，必须先有全局唯一编号：`batch_offsets = [0, S_0+1, S_0+1+S_1+1, ...]` 记录每个场景在全批超点序列中的起点，`superpoint_bias` 累加即完成重编号（[uniseg3d/uniseg3d.py:L1111-L1122](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1111-L1122)）。这是「组批不堆叠」设计在模型侧的必然代价。

**练习 2**：`batch_inputs_dict['scene_name']` 是在数据链路的哪一站产生的？模型拿它做什么？

**参考答案**：最早产生于数据集解析阶段 [uniseg3d/scannet_dataset.py:L32-L33](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/scannet_dataset.py#L32-L33)（从 `lidar_path` 文件名提取）；随后作为普通 str 键随 results 一路流过 Pack（INPUTS_KEYS）与 preprocessor（透传），最终在 `loss()`/`predict()` 中交给解码器——解码器用场景名定位该场景的 RGB 帧图像（Joint 变体，见 u4-l6）以及构造预测 JSON 的文件名（u1-l4 的 `save_pred_qa_dir`）。

**练习 3**：训练配置 batch_size=1（[configs/ground3dlmm_train.py:L162-L164](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L162-L164)），是框架限制一次只能训一个场景吗？

**参考答案**：不是。`loss()` 的偏移算术明确支持任意 batch size（u3-l1 详述），batch_size=1 是显存约束下的工程选择：单场景点数可达十万级、超点数千级，加上 4B 参数语言模型的前向，单样本已接近显存上限。数据结构上（列表 + 偏移）留足了扩批余地。

## 5. 综合实践

**任务：给一条 QA 做「全链路护照」——把 `selected_qa_data` 的五个关卡走一遍并画出路径图。**

1. **构造**：从本地 Ground3D 数据集任取一个场景的 `refined_qa_data` JSON（u2-l1），或直接手写一条含 `<p>...</p><SEG>` 的问答 dict。
2. **第一关（采样）**：对照 [uniseg3d/transforms_3d.py:L499-L541](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L499-L541)，说明这条 QA 如何与一个 `(p, S)` 形状的张量配成一对。
3. **第二关（打包）**：用 4.1.4 的合成脚本把这条 QA 塞进 `results['selected_qa_data']`，确认它落在 `inputs` 通道且字段无损。
4. **第三关（组批）**：用 4.2.4 的脚本模拟 pseudo_collate + simple_process，确认它出现在 `batch_inputs_dict['selected_qa_data'][0]`。
5. **第四关（验收）**：在 [uniseg3d/uniseg3d.py:L1169-L1172](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1169-L1172) 找到它被取出的代码行，再进入 [uniseg3d/query_decoder.py:L1962](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1962) 找到 `qa_data` 参数。
6. **产出**：画一张五个关卡 的路径图，并在每个关卡旁标注「所在通道（inputs/data_samples）与关键代码行号」。把与它配对的 `sp_gt_seg` 也画进图里——它会在第二关分岔去 data_samples 通道，在第四关的 `criterion` 处与 QA 的预测重逢。

**验收标准**：路径图能清楚回答「为什么一条 QA 的文本和它的分割真值会走两条不同的路，又在哪里会合」。脚本部分若环境不可用，允许以纯源码行号追踪完成，但需标注「待本地验证」。

## 6. 本讲小结

- **打包即分拣**：`Pack3DDetInputs_Part_Glamm` 用四张「键组路由表」（INPUTS/SEG/INSTANCEDATA_3D/INSTANCEDATA_2D）把 results 字典拆成 `inputs`（模型输入）与 `data_samples`（监督信号）两条通道；配置 `keys` 是白名单超集，实际打包 = 白名单 ∩ 现有键，未登记的新键直接抛 `NotImplementedError`。
- **类型规范化是关键改动**：相对基类，本类对「list[dict]、str、dict」做了原样保留分支，使 `selected_qa_data`（问答 dict 列表）和 `scene_name`（str）不被 `to_tensor` 破坏；`sp_gt_seg`（张量列表）则逐元素转张量、不拼接，保持与 QA 逐索引对应。
- **预处理即透传**：`Det3DDataPreprocessor_` 在保留基类 points/imgs 处理的基础上，为 13 个自定义键添加 `if in inputs: 透传` 分支；训练配置下真正生效的只有 `elastic_coords`、`scene_name`、`selected_qa_data`（外加骨架里的 points）。`sp_gt_seg` 分支因 Pack 把它路由去了 SEG_KEYS 而成为死分支——注释与代码不一致时以代码为准。
- **batch_inputs_dict 的形状语义**：所有值都是「按样本的列表」（pseudo_collate 按位装箱、不堆叠），因此模型侧需要 `batch_offsets`/`superpoint_bias` 偏移算术把各场景超点编号全局化。
- **两条通道在 loss() 汇合**：`points`+`elastic_coords` → 稀疏主干；`scene_name`+`selected_qa_data`+超点质心 → 解码器产生 `language_losses`；`gt_pts_seg.sp_pts_mask`/`sp_gt_seg` → 损失对齐；最终 `criterion(x, sp_gt_masks, language_losses)` 合流。
- 至此，单元 2 的数据链路（npy → infos.pkl → Dataset/Transform → Pack → batch_inputs_dict → loss()）全部打通，下一单元进入 3D 侧模型内部。

## 7. 下一步学习建议

- **下一讲 u3-l1「模型主入口 Grounded_LMM_main：init/loss/predict」**：本讲我们把 `loss()` 当作「验收方」只看了消费侧，下一讲完整拆解它的组件构建与 `collate`/`extract_feat` 内部。
- **提前阅读**：[uniseg3d/uniseg3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py) 的 `Grounded_LMM_main` 类（L906 起），重点找 `collate` 与 `extract_feat` 两个方法的定义位置。
- **回头看**：若对 `to_tensor`/`PointData`/`Det3DDataSample` 等 mmdet3d 结构仍不熟，可回到 u1-l3 的注册机制讲义，配合本地 Python 里 `import mmdet3d` 后用 `inspect.getsource` 查看基类实现（`Pack3DDetInputs`、`Det3DDataPreprocessor`、`BaseDataPreprocessor.collate_data`），把「本项目改了什么」与「基类提供了什么」彻底分开。
