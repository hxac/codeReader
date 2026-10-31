# u3-l2 加载、打包与批处理：三个改造版组件

## 1. 本讲目标

学完本讲，你应该能够：

1. 跟踪 `results` 字典在 pipeline 中一步步「长出」新键的过程：从数据集类给出的路径字符串，到内存里的 numpy 数组，再到 GPU 友好的 torch 张量。
2. 掌握 `Pack3DDetInputs_` 的三条「分拣流水线」：`INPUTS_KEYS` / `SEG_KEYS` / `INSTANCEDATA_3D_KEYS` 各自把哪些键送进 `inputs`、`gt_pts_seg`、`gt_instances_3d`，并能默写主配置 17 个训练键的完整去向。
3. 理解 `Det3DDataPreprocessor_` 在批组装阶段的「白名单透传」逻辑——为什么父类会丢掉提示相关键，而子类必须逐个把它们搬进 `batch_inputs`。
4. 独立写出「假场景 → 加载 → 归一化 → 打包 → 批处理」的最小复现脚本，整理出一张完整的键流转表。

## 2. 前置知识

**pipeline 与 `results` 字典**：u3-l1 讲过，数据集类只产出一份 `data_info`（主要是路径字符串）。真正读文件、做变换的是 pipeline——一串按顺序执行的 transform，每个 transform 的输入输出都是同一个 `results` 字典：拿到它、往里加几个键（或改几个键）、再原样传给下一个。可以把 `results` 想象成流水线上的托盘，本讲要做的就是记录托盘上每一步多了什么货。

**下划线命名约定**：u1-l3 提过，仓库里类名结尾的 `_`（如 `LoadAnnotations3D_`、`Pack3DDetInputs_`、`Det3DDataPreprocessor_`）表示「继承 mmdet3d 同名类、只改少量行为的改造版」。本讲的三个组件全部如此，读它们的诀窍是：先找 `super()` 调用或与父类的 diff，改动点就藏在那里。父类来自 README 第 58 行钉死 commit 安装的 mmdet3d（`fe25f7a5`），不在本仓库内，凡涉及父类内部行为，本讲只做功能性描述并标注。

**numpy 与 torch 张量的边界**：dataloader 的多进程 worker 里传 numpy 数组高效，但模型前向需要 `torch.Tensor`。`to_tensor`（mmdet3d 提供）负责 numpy → tensor 的转换。pipeline 中后段的「打包」步骤就是集中做这件事的地方。

**`np.frombuffer` 的只读陷阱**：`np.frombuffer(bytes)` 从字节流直接包装出数组，**零拷贝**但**只读**——之后任何原地写操作都会报 `ValueError: assignment destination is read-only`。这就是为什么 [loading.py:37-39](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py#L37-L39) 特意加了 `.copy()`。

**`Det3DDataSample`**：mmdet3d 的标准数据容器，一个样本的「装箱单」，内部再分三格：`gt_instances_3d`（实例级标注，装在 `InstanceData_` 里）、`gt_pts_seg`（点级分割标注，装在 `PointData` 里）、`metainfo`（场景号等元信息）。模型训练时，`inputs` 喂前向，`data_samples` 喂损失函数。

**伪批组装（pseudo collate）**：不同场景的点数不同，没法 `torch.stack` 成规整张量。dataloader 阶段只做最浅的合并——把 batch 内各样本的 `inputs`/`data_samples` 收集成列表；真正的「批处理」交给模型自带的 data_preprocessor 在 GPU 侧完成。本讲第三个组件就在这一步。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [uniseg3d/loading.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py#L1-L107) | 本讲主角一：`LoadAnnotations3D_`（额外加载超点掩码）与 `NormalizePointsColor_`（颜色归一化加 `color_std`） |
| [uniseg3d/formatting.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L1-L148) | 本讲主角二：`Pack3DDetInputs_`，三大键清单 + 分拣装箱 |
| [uniseg3d/data_preprocessor.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/data_preprocessor.py#L1-L103) | 本讲主角三：`Det3DDataPreprocessor_`，批组装时的白名单透传 |
| [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L146-L244) | 三个组件的「组装说明书」：train/test pipeline 逐项传参 |
| [uniseg3d/scannet_dataset.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/scannet_dataset.py#L28-L31) | 上游：`parse_data_info` 注入 `super_pts_path`，本讲被 `LoadAnnotations3D_` 消费 |
| [uniseg3d/structures.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/structures.py#L5-L25) | `InstanceData_`：放宽长度断言的实例数据容器，`gt_instances_3d` 的实际类型 |
| [uniseg3d/uniseg3d.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L419-L468) | 下游：`loss()` 如何按键名消费 `batch_inputs_dict` |
| [uniseg3d/instance_criterion.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L147-L172) | 下游佐证：损失侧用 `insts[i].sp_masks` 取「改名后」的 `gt_sp_masks` |
| [uniseg3d/unified_metric.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L62-L76) | 下游佐证：评估器消费 `data_sample['eval_ann_info']` |
| [uniseg3d/__init__.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L8-L11) | 第 8、9、11 行 import 触发三个组件注册进 `TRANSFORMS` / `MODELS` |

## 4. 核心概念与源码讲解

本讲覆盖的最小模块：

1. `LoadAnnotations3D_` 超点掩码加载与 `NormalizePointsColor_` 颜色归一化
2. `Pack3DDetInputs_` 的键分拣与 `Det3DDataSample` 组装
3. `Det3DDataPreprocessor_` 的键透传

先给一张总览图，把三个组件放进完整数据流里定位（`AddSuperPointAnnotations`、提示生成、`ElasticTransfrom` 属于 u3-l3～u3-l6，本讲只需知道它们在打包之前运行、负责往 `results` 里放更多键）：

```text
u3-l1 数据集类 parse_data_info
  └─ results = { pts_path, pts_instance_mask_path, pts_semantic_mask_path, super_pts_path, text_info, ... }   ← 只有字符串
      │
      ▼  ① LoadPointsFromFile        （mmdet3d 原生）读点云 bin → results['points'] = DepthPoints(N,6)
      ▼  ② LoadAnnotations3D_        ★本讲：读三个掩码 bin → pts_instance_mask / pts_semantic_mask / sp_pts_mask (N,) int64
      ▼  ③ PointSegClassMapping 等   （mmdet3d 原生，类别 id 映射）
      ▼  ④ GlobalRotScaleTrans       （mmdet3d 原生，旋转缩放增广，仅训练）
      ▼  ⑤ NormalizePointsColor_     ★本讲：RGB 归一化到约 [-1,1]
      ▼  ⑥ AddSuperPointAnnotations / TextPromptGeneration / PointPromptGeneration / ElasticTransfrom（u3-l3~l6）
      │     → gt_sp_masks / is_novel / text_token / point_prompts / elastic_coords ...
      ▼  ⑦ Pack3DDetInputs_          ★本讲：分拣装箱 → {'inputs': dict, 'data_samples': Det3DDataSample}
      │
      ▼  dataloader（多进程 worker + 伪批组装，只收集成列表）
      ▼  ⑧ Det3DDataPreprocessor_    ★本讲：白名单透传 → {'inputs': batch_inputs, 'data_samples': [...]}
      │
      ▼  UniSeg3D.loss(batch_inputs_dict, batch_data_samples)   （u4-l1 起）
```

### 4.1 `LoadAnnotations3D_` 超点掩码加载与 `NormalizePointsColor_`

#### 4.1.1 概念说明

u1-l4 建立了关键事实：ScanNet 数据准备会为每个场景产出四类「裸 bin」文件——`points`（N×6 的 XYZ+RGB）、`instance_mask`、`semantic_mask`、`super_points`（均为 (N,) int64）。前三个的加载逻辑 mmdet3d 原生就有；但第四个**超点标签**是本项目的私有数据（segmentator 过分割产物），原生 `LoadAnnotations3D` 完全不认识。

超点标签为什么必须在这里进 `results`？因为整个 UniSeg3D 是「超点即查询」：下游的 `AddSuperPointAnnotations`（u3-l3）要靠「点 → 超点」的归属关系把点级 GT 聚合成超点级 `gt_sp_masks`，模型 `extract_feat` 要靠它把体素特征 `scatter_mean` 到超点上。没有 `sp_pts_mask`，后面的一切都无从谈起。所以改造的第一站就设在标注加载这一步。

`NormalizePointsColor_` 解决的是另一个小问题：mmdet3d 原生的 `NormalizePointsColor` 只支持减均值，本仓库想要 SPFormer 风格的 `(color - mean) / std` 归一化，于是加了一个 `color_std` 参数。

#### 4.1.2 核心流程

`LoadAnnotations3D_.transform` 的执行过程：

```text
输入 results（含各类 *_path 字符串）
  1. super().transform(results)          ← 父类按 with_* 开关干活：
         with_bbox_3d=False  → 跳过 3D 框
         with_label_3d=False → 跳过 3D 类别标签
         with_mask_3d=True   → 读 instance_mask bin → results['pts_instance_mask'] (N,) int64
         with_seg_3d=True    → 读 semantic_mask bin → results['pts_semantic_mask'] (N,) int64
  2. 若 with_sp_mask_3d：
         读 super_points bin → results['sp_pts_mask'] (N,) int64
         若 results 里已有 'eval_ann_info'（测试阶段由父类链创建）：
             同时写入 eval_ann_info['sp_pts_mask'] 与 eval_ann_info['lidar_idx']（场景号）
输出 results
```

掩码文件读取的兜底策略是「两级降级」：优先用 mmengine 文件后端（支持远程存储，参数 `backend_args`），捕获 `ConnectionError` 后退回本地直读 `np.fromfile`，且 `check_file_exist` 会先给出明确的缺文件报错。

`NormalizePointsColor_.transform` 的数学过程非常简单，对每个点的 RGB 三通道：

\[ \text{color}' = \frac{\text{color} - \mu}{\sigma}, \quad \mu = [127.5, 127.5, 127.5],\ \sigma = 127.5 \]

RGB 取值 0～255，归一化后约落在 \([-1, 1]\)，与坐标量纲对齐，利于网络收敛。

#### 4.1.3 源码精读

先看类头与构造函数——`with_sp_mask_3d` 被声明为**必填**参数（没有默认值），强迫配置显式表态：

- [uniseg3d/loading.py:L11-L21](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py#L11-L21)：定义 `LoadAnnotations3D_`，继承 mmdet3d 的 `LoadAnnotations3D`；`__init__` 先存下 `self.with_sp_mask_3d`，其余参数原封不动通过 `**kwargs` 传给父类（父类因此照常拿到 `with_mask_3d=True, with_seg_3d=True` 等开关）。

再看超点加载的私有函数：

- [uniseg3d/loading.py:L32-L43](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py#L32-L43)：从 `results['super_pts_path']`（u3-l1 里 `parse_data_info` 拼好的绝对路径）读文件。先试 mmengine 文件后端 `get()`，失败则捕获 `ConnectionError`，用 `mmengine.check_file_exist` 显式报错并 `np.fromfile` 直读。第 37-39 行的注释和 `.copy()` 把 `np.frombuffer` 包装出的**只读**数组复制成可写数组——这就是前置知识里说的陷阱。

- [uniseg3d/loading.py:L45-L51](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py#L45-L51)：把 `sp_pts_mask` 写进 `results`；若 `results['eval_ann_info']` 已存在，则同步写入超点掩码和 `lidar_idx`。`sp_pts_mask_path.split("/")[-1][:-4]` 取文件名并掐掉最后 4 个字符（`.bin`），得到形如 `scene0000_00` 的场景号——评估阶段 `PromptSupportedUnifiedSegMetric` 正是从 `data_sample['eval_ann_info']` 取这份信息（[uniseg3d/unified_metric.py:L62](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L62)）。

主入口只有 4 行有效代码，是典型的「模板方法」式继承：

- [uniseg3d/loading.py:L54-L67](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py#L54-L67)：`transform` 先调 `super().transform(results)` 让父类完成实例/语义掩码加载，再按需追加超点掩码。**先 super 后自己**的顺序保证了所有掩码在进入后续 transform 前都已齐备。

配置里的实例化参数（两条 pipeline 完全相同）：

- [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L154-L160](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L154-L160)：`with_bbox_3d=False, with_label_3d=False, with_mask_3d=True, with_seg_3d=True, with_sp_mask_3d=True`——本任务是纯分割，不需要 3D 框和框级标签。

然后是颜色归一化组件：

- [uniseg3d/loading.py:L70-L82](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py#L70-L82)：`NormalizePointsColor_` 继承原生 `NormalizePointsColor`，构造函数整体重写（不走 `super().__init__`），新增 `color_std` 参数，默认 `127.5`（注释说明默认值来自 SPFormer 预处理）。注意它是**参数增强**而非流程增强——`transform` 逻辑也与父类不同步。

- [uniseg3d/loading.py:L84-L106](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py#L84-L106)：`transform` 先断言点对象带颜色属性（`attribute_dims` 含 `'color'`，由 `LoadPointsFromFile` 的 `use_color=True` 设置，颜色占第 3～5 列），然后就地做减均值、除标准差。`points.color` 是 mmdet3d `BasePoints` 提供的视图属性，读写都映射回 `.tensor` 的对应列。

- [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L168-L170](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L168-L170)：训练管线中的用法，`color_mean=[127.5, 127.5, 127.5]`，`color_std` 走默认值。测试管线在 [L223-L225](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L223-L225)（包在 `MultiScaleFlipAug3D` 里）传参一致。

#### 4.1.4 代码实践

**实践 1：亲手喂一次「加载 + 归一化」**

1. **实践目标**：不依赖真实 ScanNet 数据，用 100 个假点验证 `LoadAnnotations3D_` 与 `NormalizePointsColor_` 的行为，观察 `results` 新增的键与形状。
2. **操作步骤**：在 u1-l2 搭好的 conda 环境里运行以下脚本（**示例代码**，需在仓库根目录执行，`custom_imports` 的注册效果改由手动 `import uniseg3d` 触发）：

   ```python
   # practice_u3l2_step1.py —— 示例代码
   import os
   import numpy as np
   import uniseg3d  # 触发 @TRANSFORMS.register_module() 注册
   from mmdet3d.registry import TRANSFORMS

   # 1) 伪造一个最小场景：100 点、8 个超点、3 个实例、20 类语义
   N = 100
   root = 'fake_scannet'
   for sub in ['points', 'instance_mask', 'semantic_mask', 'super_points']:
       os.makedirs(os.path.join(root, sub), exist_ok=True)
   rng = np.random.RandomState(0)
   xyzrgb = np.concatenate([rng.rand(N, 3) * 4, rng.rand(N, 3) * 255], axis=1)
   xyzrgb.astype(np.float32).tofile(f'{root}/points/scene0000_00.bin')
   rng.randint(0, 3, N).astype(np.int64).tofile(f'{root}/instance_mask/scene0000_00.bin')
   rng.randint(0, 20, N).astype(np.int64).tofile(f'{root}/semantic_mask/scene0000_00.bin')
   rng.randint(0, 8, N).astype(np.int64).tofile(f'{root}/super_points/scene0000_00.bin')

   # 2) 模仿 u3-l1 parse_data_info 的产物（全是绝对路径字符串）
   data_info = dict(
       pts_path=f'{root}/points/scene0000_00.bin',
       pts_instance_mask_path=f'{root}/instance_mask/scene0000_00.bin',
       pts_semantic_mask_path=f'{root}/semantic_mask/scene0000_00.bin',
       super_pts_path=f'{root}/super_points/scene0000_00.bin',
   )

   # 3) 按 config 原样传参，手动实例化三个 transform
   load_pts = TRANSFORMS.build(dict(
       type='LoadPointsFromFile', coord_type='DEPTH', shift_height=False,
       use_color=True, load_dim=6, use_dim=[0, 1, 2, 3, 4, 5]))
   load_ann = TRANSFORMS.build(dict(
       type='LoadAnnotations3D_', with_bbox_3d=False, with_label_3d=False,
       with_mask_3d=True, with_seg_3d=True, with_sp_mask_3d=True))
   norm_color = TRANSFORMS.build(dict(
       type='NormalizePointsColor_', color_mean=[127.5, 127.5, 127.5]))

   # 4) 依次喂入，打印每步新增的键与形状
   results = load_pts(dict(data_info))          # 注意：transform 接收 dict
   print('after LoadPointsFromFile:', {k: type(v).__name__ for k, v in results.items()})
   print('points.shape =', results['points'].tensor.shape)
   results = load_ann(results)
   for k in ['pts_instance_mask', 'pts_semantic_mask', 'sp_pts_mask']:
       print(k, results[k].shape, results[k].dtype, 'unique=', np.unique(results[k]))
   before = results['points'].tensor.numpy()[:, 3:6].copy()
   results = norm_color(results)
   after = results['points'].tensor.numpy()[:, 3:6]
   print('color range before:', before.min(), before.max())
   print('color range after :', round(after.min(), 3), round(after.max(), 3))
   ```

3. **需要观察的现象**：
   - `LoadPointsFromFile` 之后 `results['points']` 是 `DepthPoints` 对象，`.tensor` 形状 `(100, 6)`；
   - `LoadAnnotations3D_` 之后新增 `pts_instance_mask` / `pts_semantic_mask` / `sp_pts_mask`，均为 `torch.Tensor` 或 `np.ndarray` 的 `(100,)`，dtype `int64`（u1-l4 的 bin 格式）；
   - `NormalizePointsColor_` 之后颜色列从 `[0, 255)` 变为约 `[-1, 1)`。
4. **预期结果**：掩码的 `unique` 值分别不超过 `{0,1,2}`、`{0..19}`、`{0..7}`；归一化后 `color ≈ (c - 127.5) / 127.5`。若父类对 `data_info` 还要求别的键（例如 `ann_info`），脚本会报 `KeyError`——按报错信息补上对应路径键即可，这本身也是熟悉父类契约的过程。**本脚本未在编写环境中运行（mmdet3d 未安装于阅读环境），待本地验证。**

#### 4.1.5 小练习与答案

**练习 1**：如果把 [loading.py:L37-L39](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py#L37-L39) 的 `.copy()` 去掉，最可能在后续哪一步出什么错？

**答案**：`np.frombuffer` 返回零拷贝的只读数组；下游一旦对它原地赋值（例如 `PointSegClassMapping` 重映射类别 id、或增广变换改掩码）就会抛 `ValueError: assignment destination is read-only`。`.copy()` 用一次小拷贝换掉这个隐患。（若下游恰好从不改写该数组，则不会立刻报错，但属于「埋雷」写法。）

**练习 2**：`LoadAnnotations3D_` 为什么要同时把 `sp_pts_mask` 和 `lidar_idx` 写进 `eval_ann_info`，而不是只在损失函数里读 `results['sp_pts_mask']`？

**答案**：训练侧确实只消费 `results['sp_pts_mask']`（被打包进 `data_samples`，见 4.2）。但**测试/评估**时评估器拿不到 pipeline 中间态，只能拿到最终 `data_sample`；把超点掩码与场景号塞进 `eval_ann_info`，再经 `Pack3DDetInputs_` 的 [L139-L142](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L139-L142) 挂到 `data_sample.eval_ann_info`，评估器（[unified_metric.py:L62](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L62)）就能取到 GT 超点掩码与场景标识来算指标。

**练习 3**：`NormalizePointsColor_` 与 `LoadAnnotations3D_` 都继承父类，但继承方式有何不同？

**答案**：`LoadAnnotations3D_` 走「模板方法」路线——`__init__` 用 `**kwargs` 透传给父类、`transform` 里调 `super().transform()` 复用父类流程再追加自己的步骤；`NormalizePointsColor_` 则是「整体重写」——`__init__` 不调 `super().__init__`，`transform` 也不调父类，只是借继承拿到注册名与类型身份。前者扩展流程，后者替换实现。

### 4.2 `Pack3DDetInputs_` 的键分拣与 `Det3DDataSample` 组装

#### 4.2.1 概念说明

pipeline 跑到最后，`results` 里混装着几十个键：点云、各种掩码、文本 token、点提示、弹性坐标……但模型的 `loss(batch_inputs_dict, batch_data_samples)` 只接受两个参数。**打包**就是把「一个大杂烩字典」整理成「模型入口结构」的分拣站。

分拣的规则写在三个**类属性**清单里，它们是理解本组件的唯一钥匙：

| 清单 | 去向 | 语义 |
| --- | --- | --- |
| `INPUTS_KEYS` | `inputs[key]` | 模型前向的**输入**：点云、提示、文本 token 等 |
| `INSTANCEDATA_3D_KEYS` | `gt_instances_3d[去前缀后的 key]` | 实例级 **GT 标注**：类别、超点掩码、novel 标记 |
| `SEG_KEYS` | `gt_pts_seg[去前缀后的 key]` | 点级**分割标注**：逐点掩码 |

原生 `Pack3DDetInputs` 的三张清单很短（`INPUTS_KEYS` 只有 `points`/`img` 等），完全不认识本项目的提示键；如果直接用原生版，配置 `keys` 里出现 `sp_pts_mask` 这类键会直接抛 `NotImplementedError`（[formatting.py:L130-L134](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L130-L134) 的兜底分支）。所以仓库的做法是：**扩充三张清单 + 复制父类打包逻辑**，让所有新键都有合法去处。

还有一个容易忽视的细节：`INSTANCEDATA_3D_KEYS` 的容器不是原生 `InstanceData`，而是仓库自己的 `InstanceData_`——后者删掉了「所有字段长度必须一致」的断言，允许 `sp_masks`（形状 `[n_sp, n_inst]`）与 `labels_3d`（长度 `n_inst`）这类**不同长度**的字段共存在一个容器里（[uniseg3d/structures.py:L5-L25](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/structures.py#L5-L25)）。

#### 4.2.2 核心流程

`pack_single_results` 的执行过程：

```text
输入 results（pipeline 终点的大字典）
  1. 若 results['points'] 是 BasePoints 对象 → 抽出裸 tensor（丢掉视图属性）
  2. 遍历一个「超集键列表」：把 numpy 值统一 to_tensor 转成 torch.Tensor
       （注意：这一步只是类型转换，还不决定去向；列表覆盖三张清单的所有键）
  3. 新建 Det3DDataSample + 三个空容器：gt_instances_3d(InstanceData_)、gt_instances、gt_pts_seg(PointData)
  4. 按 self.meta_keys 收集元信息 → data_sample.set_metainfo(img_metas)
  5. 逐个遍历 self.keys（config 里那一长串）：
       key ∈ INPUTS_KEYS        → inputs[key] = results[key]
       key ∈ INSTANCEDATA_3D_KEYS → gt_instances_3d[去 gt_ 前缀] = results[key]
       key ∈ INSTANCEDATA_2D_KEYS → gt_instances[去 gt_ 前缀] = results[key]（本项目基本用不到）
       key ∈ SEG_KEYS           → gt_pts_seg[去 gt_ 前缀] = results[key]
       都不在                    → raise NotImplementedError
  6. 挂上三个容器与 eval_ann_info
输出 {'inputs': inputs, 'data_samples': data_sample}
```

「去 `gt_` 前缀」沿用 mmdet/mmdet3d 父类 `_remove_prefix` 的约定：`gt_labels_3d → labels_3d`、`gt_sp_masks → sp_masks`；没有 `gt_` 前缀的键（如 `pts_semantic_mask`、`is_novel`）原样保留。这个改名在下游有实证：损失函数取的是 `insts[i].sp_masks`（[uniseg3d/instance_criterion.py:L147](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L147)）。

把主配置 train_pipeline 的 17 个键（[configs:L193-L199](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L193-L199)）完整查表，得到本讲最重要的「键流转表」：

| config 中的 key | 命中清单 | 打包后位置 | 说明 |
| --- | --- | --- | --- |
| `points` | INPUTS | `inputs['points']` | 点云 (N,6)，已归一化 |
| `elastic_coords` | INPUTS | `inputs['elastic_coords']` | 弹性形变后的坐标副本（u3-l6） |
| `point_prompts` | INPUTS | `inputs['point_prompts']` | 点提示坐标（u3-l4） |
| `point_prompt_instance_ids` | INPUTS | `inputs[...]` | 点提示对应的实例 id |
| `point_prompt_sp_ids` | INPUTS | `inputs[...]` | 点提示命中的超点 id |
| `label_text` | INPUTS | `inputs['label_text']` | 文本提示的类别标签 |
| `gt_text_prompt` | INPUTS | `inputs['gt_text_prompt']` | ⚠ 名字带 `gt_` 却走 inputs，见下 |
| `text_token` | INPUTS | `inputs['text_token']` | ScanRefer 文本 token（u3-l5） |
| `text_object_id` | INPUTS | `inputs[...]` | 文本提示指向的实例 id |
| `pts_instance_objextId_shuffle` | INPUTS | `inputs[...]` | 打乱后的实例 id 映射（u5-l6） |
| `point_prompt_distance_norms` | INPUTS | `inputs[...]` | 点提示距离归一化值（u5-l6） |
| `gt_labels_3d` | INSTANCEDATA_3D | `gt_instances_3d['labels_3d']` | 实例类别 |
| `gt_sp_masks` | INSTANCEDATA_3D | `gt_instances_3d['sp_masks']` | 超点级实例 GT（u3-l3 生成） |
| `is_novel` | INSTANCEDATA_3D | `gt_instances_3d['is_novel']` | base/novel 双路标记（u5-l3） |
| `pts_semantic_mask` | SEG | `gt_pts_seg['pts_semantic_mask']` | 逐点语义 GT |
| `pts_instance_mask` | SEG | `gt_pts_seg['pts_instance_mask']` | 逐点实例 GT |
| `sp_pts_mask` | SEG | `gt_pts_seg['sp_pts_mask']` | 点 → 超点归属 |

两点值得专门圈出来：

- **路由只看清单成员，不看名字前缀**。`gt_text_prompt` 带着 `gt_` 前缀却躺在 `INPUTS_KEYS` 里——因为指代分割的「文本提示 GT 掩码」是模型在 `add_text_prompt_queries` 里**自己**用来构造提示查询与提示 GT 的原料（[uniseg3d/uniseg3d.py:L655-L658](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L655-L658)），必须跟着 `batch_inputs` 走，而不是进 `data_samples`。
- **训练与测试的 keys 清单不同**。测试管线（[configs:L239-L243](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L239-L243)）不含 `pts_semantic_mask`/`pts_instance_mask`/`elastic_coords`/`is_novel`/`point_prompt_distance_norms`——测试不需要逐点 GT 进样本（评估用 `eval_ann_info`），也没有增广。

#### 4.2.3 源码精读

三张清单——本组件的全部「配置面」：

- [uniseg3d/formatting.py:L15-L18](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L15-L18)：`INPUTS_KEYS` 相比原生版（仅 `points`、`img`）扩充了 10 个键：`elastic_coords`、4 个点提示键、6 个文本提示键（`emb_text` 也在清单中，当前配置未打包它）。
- [uniseg3d/formatting.py:L19-L25](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L19-L25)：`SEG_KEYS` 在原生四项基础上加 `sp_pts_mask`。
- [uniseg3d/formatting.py:L26-L29](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L26-L29)：`INSTANCEDATA_3D_KEYS` 在原生项（`gt_bboxes_3d`、`gt_labels_3d`、`attr_labels`、`depths`、`centers_2d`）基础上加 `gt_sp_masks`、`is_novel`。

类型转换的「超集循环」：

- [uniseg3d/formatting.py:L80-L94](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L80-L94)：对 19 个可能存在的键做 `to_tensor`。注意这个列表是三张清单与 `self.keys` 的并集超集——**先统一转 tensor，再决定去向**；列表是 list 的键（测试多尺度增广场景）则逐元素转。第 4.1 节实践里看到的 `pts_instance_mask` 之所以从 numpy 变 tensor，就是这一步的功劳。
- [uniseg3d/formatting.py:L99-L103](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L99-L103)：两个特例——`gt_semantic_seg` 加一个 batch 维、`gt_seg_map` 同理（本项目配置未用，但保留父类行为）。

容器创建与元信息：

- [uniseg3d/formatting.py:L105-L108](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L105-L108)：`InstanceData_` 来自本仓库 `structures.py`（放宽长度断言），`PointData` 是 mmdet3d 原生点级容器。
- [uniseg3d/formatting.py:L110-L114](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L110-L114)：按 `self.meta_keys`（父类默认清单）从 `results` 收集元信息写入 `data_sample` 的 metainfo。

核心分拣循环：

- [uniseg3d/formatting.py:L116-L134](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L116-L134)：遍历 `self.keys`，按 `INPUTS_KEYS → INSTANCEDATA_3D_KEYS → INSTANCEDATA_2D_KEYS → SEG_KEYS` 的优先级链查表装箱；`key not in results` 的键直接跳过（这就是「pipeline 没生成某键也不会崩」的原因——所以实践中可以只喂部分键）；都不命中则抛 `NotImplementedError`，报错信息会提示你改 `Pack3DDetInputs` 加清单。
- [uniseg3d/formatting.py:L136-L142](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L136-L142)：挂载三个容器；`eval_ann_info` 存在则挂上，否则置 `None`——评估器因此总能源 `data_sample['eval_ann_info']`。
- [uniseg3d/formatting.py:L144-L148](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L144-L148)：返回 `{'data_samples': ..., 'inputs': ...}`——dataloader 里每个样本从此只扛这两个键。

#### 4.2.4 代码实践

**实践 2：验证分拣规则与「改名」效果**

1. **实践目标**：接实践 1 的 `results`，用手写的小 `keys` 清单跑 `Pack3DDetInputs_`，验证上表的路由与 `gt_` 前缀剥离。
2. **操作步骤**（**示例代码**，接 `practice_u3l2_step1.py` 末尾）：

   ```python
   # practice_u3l2_step2.py —— 示例代码
   from uniseg3d.formatting import Pack3DDetInputs_

   pack = Pack3DDetInputs_(keys=['points', 'pts_semantic_mask', 'pts_instance_mask',
                                 'sp_pts_mask'])          # 只喂已有的 4 个键
   packed = pack(results)

   print('inputs keys         :', list(packed['inputs'].keys()))
   print('points is tensor    :', type(packed['inputs']['points']).__name__,
         packed['inputs']['points'].shape)
   ds = packed['data_samples']
   print('gt_pts_seg fields   :', ds.gt_pts_seg.keys())
   for k in ds.gt_pts_seg.keys():
       print('  ', k, ds.gt_pts_seg[k].shape, ds.gt_pts_seg[k].dtype)
   print('gt_instances_3d fields:', list(ds.gt_instances_3d.keys()))   # 预期为空
   print('eval_ann_info       :', ds.eval_ann_info)

   # 再验证 NotImplementedError 兜底
   try:
       Pack3DDetInputs_(keys=['totally_unknown_key'])(results)
   except NotImplementedError as e:
       print('NotImplementedError ->', e)
   ```

3. **需要观察的现象**：`inputs` 只有 `points`（已是 tensor）；三个掩码进了 `gt_pts_seg` 且变成 tensor；`gt_instances_3d` 字段为空（因为 keys 没给 `gt_labels_3d`/`gt_sp_masks`，而循环只遍历 `self.keys`）；喂未知键触发 `NotImplementedError`。
4. **预期结果**：`gt_pts_seg` 打印出 `pts_semantic_mask (100,) int64` 等三项；错误信息里含 `Please modified Pack3DDetInputs to put totally_unknown_key ...`。**待本地验证。**

#### 4.2.5 小练习与答案

**练习 1**：想在配置里给 `keys` 追加一个新键 `my_aux_mask`（假设上游 transform 已生成它），只改配置够吗？

**答案**：不够。分拣循环要求键命中四张清单之一，否则抛 `NotImplementedError`。必须同时改 `formatting.py`：把 `my_aux_mask` 加进 `SEG_KEYS`（或 `INPUTS_KEYS`，取决于语义），并把它补进 [L80-L88](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L80-L88) 的 to_tensor 超集列表，否则它以 numpy 身份进容器，反传时可能出错。这也是 u6-l4 二次开发「四件套」的常见改点。

**练习 2**：`gt_text_prompt` 明明是 GT，为什么放在 `INPUTS_KEYS`？如果错放进 `INSTANCEDATA_3D_KEYS` 会发生什么？

**答案**：因为它的消费者是模型自身：`add_text_prompt_queries` 在**前向**阶段就要用它构造文本提示查询与提示 GT（[uniseg3d/uniseg3d.py:L655-L658](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L655-L658)），而 `batch_data_samples` 里的字段要到损失阶段才被 criterion 读取。错放的话，模型在 `batch_inputs_dict['gt_text_prompt']` 处会拿到 `None`/缺键，前向直接失败；且经 `_remove_prefix` 改名后，损失侧也未必按预期名字找到它。

**练习 3**：为什么 `gt_instances_3d` 要用仓库自制的 `InstanceData_` 而不是原生 `InstanceData`？

**答案**：原生 `InstanceData.__setattr__` 断言「新字段长度必须与已有字段一致」。本项目的 `gt_instances_3d` 要同时装 `labels_3d`（长度 = 实例数）、`sp_masks`（形状 `[n_sp, n_inst]`）、`is_novel`（长度 = 实例数）等长度互不相等的字段，断言必挂。`InstanceData_`（[structures.py:L8-L25](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/structures.py#L8-L25)）删掉这条断言，只保留「必须有 `__len__`」的弱要求。

### 4.3 `Det3DDataPreprocessor_` 的键透传

#### 4.3.1 概念说明

打包后的样本被 dataloader 收集成批。在 mmengine 体系里，「批组装」分两处完成：dataloader 侧只做浅合并（点数不同的各样本没法堆叠成规整张量，所以 `inputs` 里每个键的值是一个**按样本排列的列表**）；真正的批处理由 `model.data_preprocessor` 完成——它在配置的 `model` 五件套里排第一（u2-l2），前向时对整个批做归一化、体素化等收尾，产出模型 `loss()` 期望的 `batch_inputs`。

原生 `Det3DDataPreprocessor` 只认识三类输入：`points`（可选体素化）、`imgs`（图像归一化/填充）、`voxels`。它的 `simple_process` 会**新建一个空的 `batch_inputs` 字典，然后只往里放白名单键**——于是 `inputs` 里其余的一切（提示键、文本键、弹性坐标）都会被静默丢弃。UniSeg3D 的多任务提示输入全靠这些键，所以必须改造：把 `simple_process` 整体复制过来，再逐个补上 `if 'xxx' in inputs: batch_inputs['xxx'] = inputs['xxx']` 的搬运语句。

这就是「白名单透传」：`batch_inputs` 的内容 = 固定白名单 ∩ `inputs` 里实际存在的键。模型侧 `loss()` 的第一段就是在等这些键出现（[uniseg3d/uniseg3d.py:L465-L468](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L465-L468)：`if 'point_prompts' in batch_inputs_dict.keys(): ... add_point_prompt_queries(...)`）。

#### 4.3.2 核心流程

```text
输入 data（dataloader 产出：按样本组织的浅合并批）
  1. 若含图像 → 先算 pad 形状（本项目无图像分支，跳过）
  2. data = self.collate_data(data)          ← 父类继承方法：
       把「按样本排列的列表」聚成批级 inputs / data_samples 结构
       （点数不同 → points 保持按样本的列表；具体实现见 mmengine/mmdet3d 源码，行号待确认）
  3. batch_inputs ← 空字典，按白名单依次搬运：
       points（+可选 voxels） ／ elastic_coords ／ imgs
       point_prompts, point_prompt_instance_ids, point_prompt_sp_ids
       emb_text, label_text, gt_text_prompt, text_token, text_object_id
       pts_instance_objextId_shuffle, point_prompt_distance_norms
  4. 不在白名单的键 → 丢弃
输出 {'inputs': batch_inputs, 'data_samples': data_samples}
```

配置里它的实例化是「零参数」的：[configs:L22](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L22) `data_preprocessor=dict(type='Det3DDataPreprocessor_')`——父类默认 `voxel=False`（不做体素化，体素化由模型的 MinkowskiEngine 侧 `collate` 负责，u4-l1），也无需任何均值方差参数。

#### 4.3.3 源码精读

- [uniseg3d/data_preprocessor.py:L7-L9](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/data_preprocessor.py#L7-L9)：注册名 `Det3DDataPreprocessor_`，继承 mmdet3d `Det3DDataPreprocessor`，只重写 `simple_process` 一个方法。

- [uniseg3d/data_preprocessor.py:L22-L27](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/data_preprocessor.py#L22-L27)：先处理图像 pad 形状（本模型无图像输入，此分支不触发），随后调 `self.collate_data(data)`——这个方法**不在本文件**，沿继承链来自父类，职责是把 dataloader 的浅合并批聚成 `data['inputs']` / `data['data_samples']`。第 22 行 `if 'img' in data['inputs']` 的存在也说明：本方法整体是从图像检测版父类复制来的骨架。

- [uniseg3d/data_preprocessor.py:L29-L37](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/data_preprocessor.py#L29-L37)：第一段白名单——`points` 原样搬运（可选 `voxel` 体素化，本配置关闭）；第 36-37 行是**仓库新增的第一条透传**：`elastic_coords`（u3-l6 弹性增广产生的形变后坐标），模型要用它与原坐标配对计算体素映射。

- [uniseg3d/data_preprocessor.py:L39-L71](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/data_preprocessor.py#L39-L71)：图像分支整体继承自父类（pad、boxtype 转换、批增广），UniSeg3D 纯点云输入不会走进来，可跳读。

- [uniseg3d/data_preprocessor.py:L73-L101](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/data_preprocessor.py#L73-L101)：本组件的全部增量——**10 个 `if in: 搬运` 块**，把点提示三键（L73-L80）、文本提示五键（L82-L95，含 `emb_text`）与两个对比学习辅助键（L97-L101，`pts_instance_objextId_shuffle` 与 `point_prompt_distance_norms`，u5-l6 消费）逐一搬进 `batch_inputs`。每块结构完全相同，是机械但必要的「接口面清单」——将来新增提示类型（u6-l4）时，这里就是必须同步扩列的第三站（transform 生成键 → Pack 分拣键 → Preprocessor 透传键，三站名字必须一字不差）。

- [uniseg3d/data_preprocessor.py:L103](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/data_preprocessor.py#L103)：返回 `{'inputs': batch_inputs, 'data_samples': data_samples}`——这正是 `UniSeg3D.loss(batch_inputs_dict, batch_data_samples)` 的两个形参来源（[uniseg3d/uniseg3d.py:L419](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L419)）。

顺带一提：`batch_inputs['points']` 是**按样本的列表**（各样本点数不同），模型侧再用 MinkowskiEngine `TensorField` + `batch_offsets` 把它们拼成批稀疏张量——那是 u4-l1 的主题。

#### 4.3.4 代码实践

**实践 3：手工模拟批组装，观察白名单透传**

1. **实践目标**：把实践 2 打包出的样本复制成两个，模拟 dataloader 的浅合并批，调用 `simple_process`，观察 `batch_inputs` 里出现哪些键、值是什么形态。
2. **操作步骤**（**示例代码**，接 `practice_u3l2_step2.py` 末尾）：

   ```python
   # practice_u3l2_step3.py —— 示例代码
   from uniseg3d.data_preprocessor import Det3DDataPreprocessor_

   # 1) 模拟 dataloader：浅合并成 {'inputs': [...按样本...], 'data_samples': [...]}
   batch = {'inputs': [packed['inputs'], packed['inputs']],
            'data_samples': [packed['data_samples'], packed['data_samples']]}

   # 2) 直接调用被重写的 simple_process（绕过 forward 的版本差异）
   processor = Det3DDataPreprocessor_()
   out = processor.simple_process(batch, training=False)

   bi = out['inputs']
   print('batch_inputs keys:', list(bi.keys()))
   print('points type      :', type(bi['points']).__name__,
         'len =', len(bi['points']), 'shape[0] =', bi['points'][0].shape)
   print('data_samples len :', len(out['data_samples']))
   ```

3. **需要观察的现象**：`batch_inputs` 只含 `points`（本实践没有提示键）；`bi['points']` 是**长度 2 的列表**（每样本一个 `(100,6)` tensor），而非堆叠张量；`data_samples` 长度 2。
4. **预期结果**：若把实践 1 的假场景再补上提示键（u3-l4/u3-l5 之后即可），`batch_inputs` 将多出对应透传键。若当前 mmengine/mmdet3d 版本下 `simple_process` 对输入结构有额外要求而报错，可改调 `processor(batch)`（走 forward 入口），错误信息本身就是了解 `collate_data` 契约的线索。**待本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：删掉 [data_preprocessor.py:L73-L74](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/data_preprocessor.py#L73-L74) 这两行，训练还能跑吗？

**答案**：能启动，但交互式分割路径静默失效：`batch_inputs` 不再有 `point_prompts`，模型 [L465](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L465) 的 `if 'point_prompts' in batch_inputs_dict.keys()` 判定为假，直接跳过 `add_point_prompt_queries`，点提示相关的查询注入与损失全部不发生——不报错、但少学一个任务。这正是白名单透传的隐蔽性所在。

**练习 2**：为什么不在 `collate_data` 里一次性透传所有键，而要写 10 个 `if`？

**答案**：`collate_data` 是父类/mmengine 的公共方法，重写它要同时兼容图像、体素等所有父类行为，改动风险大；而 `simple_process` 里追加 `if in: copy` 是最小侵入——不动公共链路，只扩白名单。代价是新增键必须记得来这里补一行（u6-l4 扩展清单的第三站）。

**练习 3**：`Det3DDataPreprocessor_` 在配置中零参数实例化，说明 `voxel=False`。体素化既然是 3D 感知的常规操作，为什么这里关掉？

**答案**：本项目的「体素化」不是传统的 DenseVoxel，而是 MinkowskiEngine 稀疏张量化，发生在模型内部 `UniSeg3D.collate`/`extract_feat`（u4-l1）：需要同时用到坐标、`elastic_coords` 与超点归属等业务信息，且 `voxel_size=0.02`、`min_spatial_shape=128` 等参数是模型构造参数（[configs:L25-L27](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L25-L27)），放在 preprocessor 反而拿不到这些上下文。

## 5. 综合实践

**任务：绘制 u3-l2 的完整「键流转表」**

把三个分实践串成一个脚本，产出一张从 `data_info` 到 `batch_inputs` 的全链路键流转表，作为后续几讲（u3-l3～u3-l6 的每个新键、u4-l1 的模型输入）的速查底稿。

1. **准备**：跑通实践 1 的假场景脚本；准备一张 Markdown 表格模板，列为：
   `键名 | 产生它的 transform | 打包前类型/形状 | 命中清单 | 打包后位置 | batch_inputs 中是否存在`。
2. **步骤**：
   1. 在实践 1 的 `data_info` 基础上，再手写几个假键模拟 u3-l3～u3-l6 的产物（这一步**只造数据不验证语义**）：例如 `results['gt_sp_masks'] = torch.rand(8, 3) > 0.5`、`results['gt_labels_3d'] = torch.tensor([2, 3, 4])`、`results['is_novel'] = torch.tensor([False, False, True])`、`results['point_prompts'] = torch.rand(3, 3)`、`results['text_token'] = torch.zeros(2, 126, dtype=torch.long)`；
   2. 用配置里的**完整 17 键清单**实例化 `Pack3DDetInputs_`（直接抄 [configs:L193-L199](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L193-L199)），打包并核对 4.2.2 表格的每一行去向；
   3. 按实践 3 组批、运行 `simple_process`，把 `batch_inputs` 实际出现的键与 4.3 白名单比对；
   4. 把三个阶段的观察填进模板表格。
3. **需要观察的现象**：同一个键名的「三站一致性」——例如 `point_prompts` 必须同时出现在 config `keys`、`INPUTS_KEYS`、白名单 `if` 块里，缺任何一站都会断流（缺第一站被跳过、缺第二站抛 `NotImplementedError`、缺第三站静默丢弃）。
4. **预期结果**：一张 17 行的键流转表；能用它向别人解释「一个点提示从 transform 生成到被 `add_point_prompt_queries` 消费，中间经过哪三站」。所有运行结果**待本地验证**。

## 6. 本讲小结

- `LoadAnnotations3D_` = 父类掩码加载 + 一段超点 bin 读取（`.copy()` 防只读、降级读取兜底），并把 `sp_pts_mask`/`lidar_idx` 同步进 `eval_ann_info` 供评估器使用；`NormalizePointsColor_` 把 RGB 归一化到约 \([-1,1]\)。
- `Pack3DDetInputs_` 的行为完全由三张类属性清单决定：`INPUTS_KEYS` 进 `inputs`（前向原料），`INSTANCEDATA_3D_KEYS` 进 `gt_instances_3d`（实例 GT，`gt_` 前缀被剥掉），`SEG_KEYS` 进 `gt_pts_seg`（点级 GT）；路由看清单成员而非名字前缀，`gt_text_prompt` 是典型反例。
- 容器 `InstanceData_` 放宽了「字段等长」断言，让 `sp_masks` 与 `labels_3d` 这类异长字段共存；分拣循环对缺失键静默跳过、对无清单键抛 `NotImplementedError`。
- `Det3DDataPreprocessor_` 是「白名单透传」：`collate_data` 聚批后，只有 `points`/`elastic_coords`/`imgs` 与 10 个手写 `if` 块搬运的提示/文本键能进入 `batch_inputs`，其余键静默丢弃——新增提示类型时这里是第三站。
- 本讲三个组件构成「键的三站通道」：**transform 生成键 → Pack 分拣键 → Preprocessor 透传键**，三站键名必须一字不差，这是贯穿 u3 全单元和 u6-l4 二次开发的主线索。

## 7. 下一步学习建议

下一讲 **u3-l3 超点标注构造：AddSuperPointAnnotations** 接力本讲：你已经在键流转表里给 `gt_sp_masks`、`is_novel` 留了位置，下一讲就讲它们如何由 `pts_instance_mask` + `pts_semantic_mask` + `sp_pts_mask`（本讲加载的三件套）经 one-hot → `scatter_mean` → 0.5 阈值聚合出来，以及 `merge_ov` 如何合并 SAM3D 伪掩码。建议先复习 u1-l4 的「点级 → 超点级」关系，再带着键流转表去读 [uniseg3d/transforms_3d.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L1)。之后再按 u3-l4（点提示）、u3-l5（文本提示）、u3-l6（弹性变换）的顺序把键流转表的剩余空行逐一填满；到 u4-l1 时回头看本讲的 `batch_inputs`，你会看到它的每个键在模型前向中的确切落点。
