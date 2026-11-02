# u7-l3 可视化系统：Det3DLocalVisualizer 与 Hook

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `Det3DLocalVisualizer` 的三层继承关系（mmengine → mmdet → mmdet3d），以及 `vis_backends` 多后端（Local / Tensorboard）如何把同一张可视化图写往不同目的地。
2. 追踪 `add_datasample` 的完整执行路径：它如何按 `vis_task` 分流到点云通道与图像通道，如何用 `palette` 给 3D 框按类别上色。
3. 理解三种绘制原语的数学：Open3D 里的 3D 框线框、BEV 旋转矩形四角点计算、3D 框 8 角点经投影矩阵落到图像平面。
4. 解释 `Det3DVisualizationHook` 的触发链路：`test.py --show/--show-dir` 如何一路改写配置、Hook 又如何按 `interval` 决定画哪一帧、把图存到哪里。

本讲承接 u7-l2 的一个关键结论：命令行 `--score-thr` 只进可视化链路，不影响 mAP——本讲我们将看到它最终落在 `Det3DVisualizationHook.score_thr` 上。

## 2. 前置知识

**Visualizer（可视化器）**。mmengine 提供的通用绘图组件，既能在屏幕上开窗口画图，也能把图写入文件、TensorBoard 等「后端」。它是一个**单例**：全进程只有一个当前实例，任何组件（Hook、Inferencer）都可以通过 `Visualizer.get_current_instance()` 拿到它。

**vis_backends（可视化后端）**。绘图本身与存储解耦：`LocalVisBackend` 把图写进本地 `vis_data` 目录，`TensorboardVisBackend` 写成事件文件供 TensorBoard 读取。一个 Visualizer 可同时挂多个后端，画一次、处处存。

**Open3D**。一个 3D 数据可视化库。点云是「没画面的抽象数字」，必须借助 Open3D 这类渲染器才能交互式查看。本讲大量代码用 `try: import open3d except ImportError` 包裹——没有装 Open3D 也能画图像通道，只有点云通道需要它。

**matplotlib**。图像通道的画布。`Det3DLocalVisualizer` 内部维护一个 matplotlib 坐标轴 `self.ax_save`，所有画在图上的元素（多边形、散点）都是 matplotlib 的 artist。

**Hook 触发点**。回顾 u7-l1：Runner 的 val/test 循环每处理完一个 batch 会回调 `after_val_iter` / `after_test_iter`。可视化 Hook 就挂在这两个点上。

还需要 u2 系列的两块地基：

- **Det3DDataSample 数据契约**（u2-l4）：预测结果装在 `pred_instances_3d`（含 `bboxes_3d`、`scores_3d`、`labels_3d`），真值装在 `gt_instances_3d`。
- **三大坐标系**（u2-l1/u2-l2）：LiDAR（x 前 z 上）、CAM（z 前 y 下）、DEPTH（y 前 z 上）；7 自由度框 = 中心 + 尺寸 + yaw；`corners` 属性把框展开成 8 个角点，`bev` 属性压成 XYWHR 五元组。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [mmdet3d/visualization/local_visualizer.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py) | 主角：`Det3DLocalVisualizer`，全部 3D 绘制接口与总入口 `add_datasample` |
| [mmdet3d/visualization/vis_utils.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/vis_utils.py) | 投影数学：三种坐标系 3D 框到图像平面的投影函数 |
| [mmdet3d/engine/hooks/visualization_hook.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/engine/hooks/visualization_hook.py) | `Det3DVisualizationHook`：验证/测试期自动触发绘制 |
| [mmdet3d/structures/bbox_3d/utils.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/utils.py) | `points_cam2img`：齐次投影的底层实现 |
| [mmdet3d/structures/bbox_3d/base_box3d.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py) | `bev` 属性：BEV 绘制的数据来源 |
| [configs/_base_/datasets/kitti-3d-3class.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py) | 数据集基座配置里 `visualizer` 块的标准写法 |
| [tools/test.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py) | `trigger_visualization_hook`：CLI 参数如何打开 Hook |
| [demo/pcd_demo.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/demo/pcd_demo.py) | u1-l3 跑通的推理 demo，其可视化最终也落到本讲的组件 |
| [docs/en/user_guides/visualization.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/visualization.md) | 官方可视化文档，本讲综合实践的脚本蓝本 |

## 4. 核心概念与源码讲解

### 4.1 Visualizer 三层家族与多后端适配

#### 4.1.1 概念说明

3D 检测的可视化面临一个特殊难题：**点云和图像是两种完全不同的介质**。画 2D 框用 matplotlib 就够了；画点云里的 3D 框需要 Open3D 的 3D 渲染。MMDetection3D 没有另起炉灶，而是继承 MMDetection 的 2D 可视化器、在其上叠加 3D 能力，形成三层家族：

```
mmengine.visualization.Visualizer        # 通用骨架：后端管理、单例、画布
        ↑ 继承
mmdet.visualization.DetLocalVisualizer   # 2D 检测：draw_bboxes、_draw_instances
        ↑ 继承
mmdet3d.Det3DLocalVisualizer             # 3D：draw_bboxes_3d、draw_bev_bboxes、
                                        #     draw_proj_bboxes_3d、add_datasample
```

这样设计的好处：2D 检测结果（多模态任务里图像上的 2D 框）直接复用父类的 `_draw_instances`，mmdet3d 只需实现 3D 特有的部分。

「多后端适配」指第二个维度：**画在哪**。`vis_backends` 参数接收一个后端配置列表，同一张画好的图会依次交给每个后端存储。这是策略模式的典型应用——绘制逻辑与存储介质彻底解耦。

#### 4.1.2 核心流程

配置文件中的声明 → Runner 构建 → 单例注册 → 各组件取用：

```text
configs/_base_/datasets/kitti-3d-3class.py:
    vis_backends = [dict(type='LocalVisBackend')]
    visualizer = dict(type='Det3DLocalVisualizer',
                      vis_backends=vis_backends, name='visualizer')
        │
        ▼ Runner 初始化（按注册器构建，save_dir 缺省时指向 work_dir/时间戳）
Det3DLocalVisualizer 单例
        │
        ├── Det3DVisualizationHook.__init__ 里 Visualizer.get_current_instance()
        └── Inferencer._init_visualizer 里注入 dataset_meta
```

注意 `name='visualizer'` 不是随便起的：单例按名字索引，Hook 取当前实例时依赖这个名字与 Runner 中的约定一致。

#### 4.1.3 源码精读

**注册与构造函数**。[mmdet3d/visualization/local_visualizer.py:41-42](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L41-L42) 把类挂到 `VISUALIZERS` 注册节点；构造函数见 [mmdet3d/visualization/local_visualizer.py:113-147](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L113-L147)：先把 `name / vis_backends / save_dir / bbox_color / text_color / line_width / alpha` 等参数上交给父类（这些是 2D 家族就有的），再保留自己特有的三个状态——`multi_imgs_col`（多视角图像拼图的列数）、Open3D 交互用的三个布尔标志（暂停/下一帧/退出）。

**配置挂接的标准写法**。[configs/_base_/datasets/kitti-3d-3class.py:165-167](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L165-L167)：每个数据集基座配置末尾都声明了 `visualizer` 块，默认只挂 `LocalVisBackend`。想上 TensorBoard，改成：

```python
vis_backends = [dict(type='LocalVisBackend'),
                dict(type='TensorboardVisBackend')]
```

**Open3D 的可选导入**。[mmdet3d/visualization/local_visualizer.py:33-38](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L33-L38) 用 try/except 把 Open3D 导成 `None`，只有真正走到点云通道时才会在 [mmdet3d/visualization/local_visualizer.py:169-171](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L169-L171) 报 ImportError 并提示安装。

**单例的取用**。[mmdet3d/engine/hooks/visualization_hook.py:68](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/engine/hooks/visualization_hook.py#L68)：Hook 构造时一行 `Visualizer.get_current_instance()` 拿到 Runner 建好的那个 visualizer，不经过任何注册器查找——这就是单例的威力。

**dataset_meta 的注入**。Visualizer 要按类别配色、写类别名，就得知道数据集的 `classes / palette / ignore_index`。Inferencer 侧的注入点在 [mmdet3d/apis/inferencers/base_3d_inferencer.py:142-145](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/base_3d_inferencer.py#L142-L145)：构建后直接把 `self.model.dataset_meta` 塞给 visualizer。训练/测试侧则由 mmengine 的 Runner 从 dataloader 的 dataset 上自动同步（这就是为什么 config 里从不用手写 classes 给 visualizer）。

**主进程保护**。所有公开绘制方法都带 `@master_only` 装饰器（如 [mmdet3d/visualization/local_visualizer.py:185](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L185)、[L377](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L377)、[L929](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L929)）：分布式多卡时只在 rank 0 绘制，避免多进程同时写同一个文件互相覆盖。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到多后端适配——同一张图被写到两个后端。
2. **操作步骤**：
   - 选一个 KITTI 基座配置（如 `configs/_base_/datasets/kitti-3d-3class.py`），复制其 `vis_backends` 定义；
   - 在自己的实验配置里追加 `vis_backends = [dict(type='LocalVisBackend'), dict(type='TensorboardVisBackend')]` 并覆盖 `visualizer` 块；
   - 用 u7-l2 的方式跑一次 `tools/test.py`（带 `--show-dir`）。
3. **需要观察的现象**：`work_dir/{时间戳}/` 下除 `vis_data` 图片目录外，还多出一个 `vis_data` 里的 TensorBoard 事件文件；`tensorboard --logdir work_dir` 能看到图像面板。
4. **预期结果**：两个后端各存一份同一内容的图。**待本地验证**（本讲义环境未安装 TensorBoard，命令行为需本地确认）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Det3DLocalVisualizer` 继承 `DetLocalVisualizer` 而不是直接继承 mmengine 的 `Visualizer`？

**答案**：多模态任务（如 KITTI 上同时出 2D 与 3D 结果）需要在同一张图像上画 2D 框与投影 3D 框。2D 绘制能力（`_draw_instances`、类别名标注等）已经在 mmdet 的 `DetLocalVisualizer` 里实现好了，继承过来即可复用；直接继承 mmengine 版则要自己重写整套 2D 检测绘制。这是跨库组件复用（u1-l4 讲过的 mmdet 借用模式）在非模型组件上的体现。

**练习 2**：Hook 为什么不在配置里显式传 `visualizer` 参数，而是 `get_current_instance()`？

**答案**：mmengine 的 Visualizer 是按名字索引的全局单例，Runner 初始化时已按配置建好唯一实例；Hook 若再按配置建一个，就会出现两个 visualizer 各存一份图。取单例保证「配置只声明一次、所有消费者共享同一个」。

### 4.2 add_datasample：统一绘制入口与 GT/Pred 双通道

#### 4.2.1 概念说明

`add_datasample` 是整个可视化系统的**总入口**：上层的 Hook（训练/测试期）和 Inferencer（推理期）都只调这一个方法。它的职责是把一份 `Det3DDataSample`（可能同时含 GT 和预测）翻译成屏幕/文件上的图。

两个关键设计：

1. **vis_task 五选一**：`mono_det`（单目）、`multi-view_det`（多视角）、`lidar_det`（LiDAR）、`lidar_seg`（LiDAR 分割）、`multi-modality_det`（多模态）。任务决定走哪条绘制通道——这本质上是把「输入介质」（图像 or 点云 or 两者）显式化了，因为光看 data dict 无法可靠区分。
2. **GT 与预测并排画**：两者都画时，图像通道会水平拼接成「左 GT 右 Pred」一张大图，方便人眼比对；点云通道则把 GT 框和 Pred 框画进同一个 Open3D 场景。

而**颜色的来源**正是本讲实践要回答的问题：`dataset_meta['palette']`（数据集 `METAINFO` 里每类一色）经 `get_palette` 扩展成色板，再按 `labels_3d` 逐框查表——即**3D 框颜色由类别标签决定，与分数无关**。

#### 4.2.2 核心流程

```text
add_datasample(name, data_input, data_sample, vis_task, ...)
│
├─ 1. 校验 vis_task；从 dataset_meta 取 classes / palette / ignore_index
│
├─ 2. draw_gt 且 data_sample 里有 gt_instances_3d
│     └─ _draw_instances_3d(...)          # 真值通道
│
├─ 3. draw_pred 且 data_sample 里有 pred_instances_3d
│     ├─ pred_instances_3d = pred[sred.scores_3d > pred_score_thr].to('cpu')
│     └─ _draw_instances_3d(...)          # 预测通道（同一函数复用）
│
├─ 4. _draw_instances_3d 内部按 vis_task 分流：
│     ├─ lidar_det / multi-modality_det：
│     │    points+框 → to_depth_mode → set_points + draw_bboxes_3d  （Open3D）
│     └─ mono_det / multi-modality_det：
│          img → set_image + draw_proj_bboxes_3d（matplotlib）
│          （多视角时逐相机画小图再拼网格）
│
├─ 5. show=True → self.show(...)          # 开窗口 / 截屏保存
└─ 6. out_file 给定 → mmcv.imwrite(...)
       否则 → self.add_image(...)          # 交给所有 vis_backends
```

#### 4.2.3 源码精读

**入口校验与元信息读取**。[mmdet3d/visualization/local_visualizer.py:977-987](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L977-L987)：`vis_task` 必须落在五个合法值内；`classes`、`palette`、`ignore_index` 全部从 `self.dataset_meta` 读取——分割任务还要用 `ignore_index` 把不该画的点过滤掉（`keep_index`）。

**预测分数过滤的细节**。[mmdet3d/visualization/local_visualizer.py:1024-1030](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L1024-L1030)：先用 `scores_3d > pred_score_thr` 布尔索引筛掉低分框，再用 `.to('cpu')` 搬家。源码注释特别说明不能用 `.cpu()`——因为 `BaseInstance3DBoxes` 没有实现 `.cpu()` 方法（它是 Tensor 的包装类而非 Tensor 子类）。这是 u2-l2「框是包装类」的直接推论。

**点云通道（lidar_det）**。[mmdet3d/visualization/local_visualizer.py:639-669](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L639-L669)：

- 点和框若不是深度系，经 `to_depth_mode` 统一转到 DEPTH 坐标系（下一节解释为什么）；
- `input_meta` 里有 `axis_align_matrix` 时（ScanNet 等室内数据，见 u3-l4），点云还要按该矩阵旋转平移对齐；
- **上色逻辑**（[L658-662](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L658-L662)）：

```python
bbox_color = palette if self.bbox_color is None else self.bbox_color
bbox_palette = get_palette(bbox_color, max_label + 1)
colors = [bbox_palette[label] for label in labels_3d]
```

  优先用构造参数 `bbox_color`；没传则用 `dataset_meta['palette']`。`get_palette`（从 mmdet 跨库导入）负责把色板补齐到 `max_label+1` 个颜色。随后每个框按自己的 `labels_3d` 查表得到专属颜色。

**图像通道（mono_det / multi-modality_det）**。[mmdet3d/visualization/local_visualizer.py:671-743](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L671-L743)：若 `img` 是 list 或 4 维张量则走**多视角网格**分支——逐张 `set_image`、逐张 `draw_proj_bboxes_3d`，且 [L691-698](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L691-L698) 有一段精巧的元信息分发：`input_meta` 里凡是长度等于视角数的序列（如 `lidar2img` 列表）都按第 i 个视角切分，标量则原样共享，保证每个视角配对标定矩阵。单视角则直接 `set_image` + 画投影框。注意 `mono_det` 额外画 `centers_2d`（u6-l5 讲过的 2D 中心点）。

**GT/Pred 拼接**。[mmdet3d/visualization/local_visualizer.py:1058-1069](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L1058-L1069)：两个通道都画时 `np.concatenate(..., axis=1)` 水平拼成左 GT 右 Pred。

**出口分流**。[mmdet3d/visualization/local_visualizer.py:1090-1100](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L1090-L1100)：给了 `out_file` 就直接 `mmcv.imwrite` 落盘（注意 `[..., ::-1]` 把 RGB 翻回 BGR，因为 mmcv 写盘约定 BGR）；否则 `add_image` 把图广播给所有 vis_backends。2D 检测结果另存 `_2d` 后缀文件。

**Inferencer 侧的调用样例**。[mmdet3d/apis/inferencers/lidar_det3d_inferencer.py:190-201](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/lidar_det3d_inferencer.py#L190-L201)：u1-l3 跑的 `pcd_demo.py` 最终就是这几行——`draw_gt=False`（推理没有真值）、`vis_task='lidar_det'`、`o3d_save_path` 指向 `vis_lidar/xxx.png`。

#### 4.2.4 代码实践

1. **实践目标**：不跑模型，直接手工构造 data sample 验证 `add_datasample` 的 GT 通道与 out_file 出口。
2. **操作步骤**（示例代码，改写自类 docstring [mmdet3d/visualization/local_visualizer.py:79-111](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L79-L111)）：

```python
# 示例代码：手工调用 add_datasample（无需权重、无需数据集）
import numpy as np, torch
from mmengine.structures import InstanceData
from mmdet3d.structures import DepthInstance3DBoxes, Det3DDataSample
from mmdet3d.visualization import Det3DLocalVisualizer

visualizer = Det3DLocalVisualizer(save_dir='outputs')
visualizer.dataset_meta = {'classes': ('cabinet', 'bed', 'chair'),
                           'palette': [(106, 0, 228), (119, 11, 32), (0, 0, 192)]}

points = np.random.rand(1000, 3).astype(np.float32)
gt = InstanceData()
gt.bboxes_3d = DepthInstance3DBoxes(torch.rand((5, 7)))
gt.labels_3d = torch.randint(0, 3, (5,))
sample = Det3DDataSample()
sample.gt_instances_3d = gt

visualizer.add_datasample('scene', dict(points=points), sample,
                          vis_task='lidar_det', out_file='outputs/gt_vis')
```

3. **需要观察的现象**：`outputs/gt_vis.png` 生成（注意 4.1 节：走 Open3D 通道且无显示设备时，`out_file` 只在图像通道有产物；本例 lidar_det 的图像通道为空，主要产物是 `save_dir` 后端里的记录）。若本机装了 Open3D 且有显示设备，可加 `show=True` 看交互窗口。
4. **预期结果**：理解「`add_datasample` 只是把绘制翻译成通道调用，实际落盘介质由 out_file/后端/show 三者决定」。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `pred_instances_3d` 过滤后要用 `.to('cpu')` 而不是 `.cpu()`？

**答案**：`InstanceData` 的 `.cpu()` 会对其所有字段调用同名方法，而 `bboxes_3d` 字段是 `BaseInstance3DBoxes`——一个持有 Tensor 的包装类，没有实现 `.cpu()`，调用会抛 AttributeError；`.to('cpu')` 则是包装类支持的方法。源码注释（L1027-1028）明确记录了这个坑。

**练习 2**：GT 与 Pred 的 3D 框画在同一个 Open3D 场景里，怎么区分谁是谁？

**答案**：按当前实现主要靠 `bbox_color`/构造参数区分（源码 [L261-263](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L261-L263) 的 TODO 注释承认尚未按 pred/GT 自动分色，颜色只按类别标签查表）。因此严格比对 GT/Pred 通常更推荐图像通道的左右拼接图，或分别只开 `draw_gt` / `draw_pred` 画两张。

### 4.3 绘制原语：Open3D 3D 框、BEV 框与投影框的数学

#### 4.3.1 概念说明

三个绘制原语对应人看 3D 检测结果的三种视角：

| 原语 | 介质 | 回答的问题 |
|---|---|---|
| `draw_bboxes_3d` | Open3D 三维场景 | 框在三维空间中的真实位置与姿态 |
| `draw_bev_bboxes` | matplotlib 二维画布 | 鸟瞰图（BEV）下框的俯视轮廓与朝向 |
| `draw_proj_bboxes_3d` / `draw_points_on_image` | 原始图像 | 框/点投影到相机图像上是否贴合目标 |

为什么点云通道统一转 **DEPTH 坐标系**？Open3D 的默认视角约定是「y 轴向前、z 轴向上」——恰好等于 mmdet3d 的 DEPTH 约定（u2-l1）。统一转到深度系后，Open3D 里看到的画面方向才符合直觉。

投影原语的数学核心只有一个：**针孔相机模型的齐次坐标投影**。3D 点 \(p=(x,y,z)\) 经投影矩阵 \(M\)（4×4 的 lidar2img / 3×3 的 cam2img）映射后必须除以深度：

\[
\begin{bmatrix} u\cdot d \\ v\cdot d \\ d \end{bmatrix} = M \begin{bmatrix} x \\ y \\ z \\ 1 \end{bmatrix}, \qquad (u, v) = \left( \frac{u\cdot d}{d},\ \frac{v\cdot d}{d} \right)
\]

不除以 \(d\)，得到的只是「像素坐标乘深度」，毫无意义。

#### 4.3.2 核心流程

**draw_bboxes_3d（Open3D）**：

```text
逐框：
  center, dim, yaw ← 框张量 7 元组
  底面中心 → 几何中心：center[重力轴] ± dim[重力轴]/2
  OrientedBoundingBox(center, rot_mat(yaw), dim)
  LineSet（12 条边线框）→ paint(bbox_color[i]) → add_geometry
  （可选）框内点索引 → points_colors[indices] = 框色   # 点被"染"成框的颜色
```

**draw_bev_bboxes（matplotlib）**：

```text
bev ← bboxes.bev            # (N,5)：xc, yc, w, h, theta
bev[:, :4] *= scale         # 米 → 像素（默认 15 像素/米）
vec1 = (w/2·cosθ, w/2·sinθ)         # 沿朝向的半长向量
vec2 = (−h/2·sinθ, h/2·cosθ)        # 垂直朝向的半宽向量
四角点 = 中心 ± vec1 ± vec2
poly[:, :, 0] += width/2     # 把传感器原点挪到画布中心
draw_polygons(...)           # matplotlib 多边形
```

**draw_proj_bboxes_3d（matplotlib）**：

```text
按框类型选投影函数（LiDAR/Camera/Depth 三选一）
corners_2d ← proj 函数（8 角点 × 透视除法）
（可选）过滤一半以上角点越界的框
12 条边索引连线 → PathPatch 描边
front_polys（前表面 4 点）→ 半透明填充
```

#### 4.3.3 源码精读

**Open3D 框的几何构造**。[mmdet3d/visualization/local_visualizer.py:292-330](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L292-L330)：先 `convert_to(Box3DMode.DEPTH)` 统一坐标系、`tensor2ndarray` 转numpy；每个框用 `get_rotation_matrix_from_xyz` 把 yaw 变旋转矩阵。注意 [L307-312](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L307-L312) 的锚点换算——u2-l2 讲过 mmdet3d 框默认锚点是**底面中心**，而 `OrientedBoundingBox` 要几何中心，所以 LiDAR/DEPTH（重力轴 z 向上）要 `+= dim/2`，CAM（y 向下）则 `-= dim/2`。[L321-325](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L321-L325) 是很实用的交互细节：用 `get_point_indices_within_bounding_box` 找出框内点，把它们染成框的颜色——肉眼判断「框是否套住了目标」变得非常直接。

**BEV 画布**。[mmdet3d/visualization/local_visualizer.py:332-374](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L332-L374)：`set_bev_image` 默认造一张 900×900 黑底画布，`origin='lower'` 让 y 轴朝上（与 LiDAR 前向一致）；三条灰色虚线画出**相机视锥**（±45°）、红色 `+` 标出传感器原点。这些装饰让人一眼知道朝向。

**BEV 框的数据来源**。[mmdet3d/structures/bbox_3d/base_box3d.py:155-159](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L155-L159)：`bev` 属性就是 `tensor[:, [0, 1, 3, 4, 6]]`——中心 xy + 尺寸 wh + yaw 的 (N,5)。相机系框在 [mmdet3d/structures/bbox_3d/cam_box3d.py:173-182](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/cam_box3d.py#L173-L182) 重写了该属性：取 x、z 列且 **yaw 取负**（相机系 y 向下的坐标约定传导到 BEV，u2-l1 的结论再次出现）。

**BEV 四角点计算**。[mmdet3d/visualization/local_visualizer.py:419-440](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L419-L440)：`np.split` 把 bev 拆成 ctr、w、h、theta；两个正交半向量 vec1/vec2 相加减得四个角点——这就是把「带旋转的矩形」参数化为「中心 + 两组半边向量」的标准做法。最后 [L432](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L432) 的 `poly[:, :, 0] += self.width / 2` 把 LiDAR 坐标（x 前向、原点在传感器）平移到画布中央。

**三个投影函数的分派**。[mmdet3d/visualization/local_visualizer.py:522-529](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L522-L529) 用 `isinstance` 三分支把 `DepthInstance3DBoxes / LiDARInstance3DBoxes / CameraInstance3DBoxes` 分别路由到 `proj_depth_bbox3d_to_img / proj_lidar_bbox3d_to_img / proj_camera_bbox3d_to_img`——框类型自带坐标系信息（u2-l2 的子类体系），所以不需要额外参数声明坐标系。

**底层投影数学**。[mmdet3d/structures/bbox_3d/utils.py:217-225](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/utils.py#L217-L225) 是 `points_cam2img` 的核心三行：补齐次维、乘投影矩阵、前两列除以第三列（深度）。[mmdet3d/visualization/vis_utils.py:100-113](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/vis_utils.py#L100-L113) 的 LiDAR 版本多了两个防御细节：深度 clip 到 `[1e-5, 1e5]` 防除零（L110-112）；支持 4×4 的 `lidar2img`（LiDAR 系直接一步到像素）。相机版在 [mmdet3d/visualization/vis_utils.py:161-175](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/vis_utils.py#L161-L175)，深度版（[L119-146](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/vis_utils.py#L119-L146)）最特别：投影前先调 `apply_3d_transformation(..., reverse=True)` **逆放数据增强**（u3-l6 讲过的 `transformation_3d_flow` 审计线索在这里被消费），因为室内 GT 框是在增强后的点云坐标系里标注/预测的，要还原到原始相机姿态才能与原图对齐。

**12 条边的线框**。[mmdet3d/visualization/local_visualizer.py:551-569](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L551-L569)：`lines_verts_idx = [0,1,2,3,7,6,5,4,0,3,7,4,5,1,2,6]` 这串索引按「后四点一圈 → 前四点一圈 → 四条连接边」的顺序串起 8 个角点画出 12 条边；随后 [L572-579](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L572-L579) 再把前表面 4 点（索引 4:）画成半透明多边形，暗示框的朝向。多视角任务还有 [L534-549](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L534-L549) 的越界过滤：8 个角点中至少 4 个落在图像内才画，避免长投影线拉花拼图。

**点云投影上色**。[mmdet3d/visualization/local_visualizer.py:442-476](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L442-L476)：`draw_points_on_image` 复用 `points_cam2img(..., with_depth=True)` 拿到每个点的深度，按 `深度/max_depth` 归一化后过 jet colormap——近处红、远处蓝，距离信息编码进了颜色。

#### 4.3.4 代码实践

1. **实践目标**：跑通 `draw_bev_bboxes` 与 `draw_points_on_image` 两个原语，理解「BEV 画布」与「图像投影画布」是两套独立的画布状态。
2. **操作步骤**（示例代码，改写自 [docs/en/user_guides/visualization.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/visualization.md)）：

```python
# 示例代码：BEV 框 + 点云投影
import mmcv, numpy as np, torch
from mmengine import load
from mmdet3d.visualization import Det3DLocalVisualizer
from mmdet3d.structures import CameraInstance3DBoxes

info = load('demo/data/kitti/000008.pkl')['data_list'][0]
img = mmcv.imread('demo/data/kitti/000008.png')
img = mmcv.imconvert(img, 'bgr', 'rgb')

# ---- 第一张图：BEV ----
gt_bboxes = CameraInstance3DBoxes(
    np.array([ins['bbox_3d'] for ins in info['instances']], dtype=np.float32))
vis = Det3DLocalVisualizer()
vis.set_bev_image()                        # 900x900 黑底 + 相机视锥
vis.draw_bev_bboxes(gt_bboxes, edge_colors='orange')
vis.show(save_path='outputs/bev.png')      # 无显示设备时保存

# ---- 第二张图：点云投影 ----
points = np.fromfile('demo/data/kitti/000008.bin', dtype=np.float32).reshape(-1, 4)
lidar2img = np.array(info['images']['CAM2']['lidar2img'], dtype=np.float32)
vis2 = Det3DLocalVisualizer()
vis2.set_image(img)
vis2.draw_points_on_image(points[:, :3], lidar2img)
mmcv.imwrite(vis2.get_image()[..., ::-1], 'outputs/pts_on_img.png')
```

3. **需要观察的现象**：`bev.png` 上橙色旋转矩形按真实朝向摆放、画布中心有红 `+`；`pts_on_img.png` 上点云按距离呈现红→蓝渐变，且道路/车辆的点贴合图像中的对应区域。
4. **预期结果**：两张图成功生成。注意 BEV 用的是**相机系 GT 框**（pkl 里 `bbox_3d` 就是相机系 7 参数，见 u3-l3），`bev` 属性自动取 x-z 平面并翻转 yaw。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`draw_bev_bboxes` 的 `scale=15` 是什么含义？为什么需要它？

**答案**：每米 15 像素的缩放系数。KITTI 点云范围是几十米量级，而画布是 900 像素——不乘 scale 框会挤在几个像素内根本看不见。BEV 是示意图而非度量图，允许这种任意缩放（[L421](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L421) 只缩放 `[:, :4]` 即中心与宽高，yaw 不能缩放）。

**练习 2**：`proj_depth_bbox3d_to_img` 为什么要先 `apply_3d_transformation(..., reverse=True)` 而 LiDAR/Camera 版本不需要？

**答案**：室内（ScanNet/SUN RGB-D）训练管线包含 `RandomFlip3D`、`GlobalRotScaleTrans` 等增强，这些增强被记入 `transformation_3d_flow`（u3-l6）。可视化拿到的是**原始图像**，框却在**增强后坐标系**里，必须逆放增强才能对上。而室外 LiDAR 框通常由模型在当前帧预测、`lidar2img` 又是原始标定，投影路径上没有需要撤销的增强。（Camera 版针对单目检测，其测试管线一般不做几何增强。）

**练习 3**：`points_cam2img` 为什么最后必须除以第三列？

**答案**：齐次投影 \(M p\) 的输出是 \((u\cdot d, v\cdot d, d)\)，前两维都乘了深度 \(d\)；除以 \(d\) 才得到真实的像素坐标 \((u, v)\)。这既是针孔模型的数学，也解释了 LiDAR 版为何先把深度 clip 到 `1e-5` 以上——点恰好在相机平面上时防除零。

### 4.4 Det3DVisualizationHook：训练与测试期的自动触发

#### 4.4.1 概念说明

前两节的接口都是「手动调用」；Hook 把它们接入 Runner 生命周期，让验证/测试过程**自动**产出可视化。`Det3DVisualizationHook` 有两个行为截然不同的触发点：

- **`after_val_iter`**：验证循环每 batch 回调。验证通常跑完整验证集，逐帧画太慢，所以按 `interval` 抽帧、且每 batch 只画第一条样本。
- **`after_test_iter`**：测试循环每 batch 回调。这里**每条样本都画**（`for data_sample in outputs`），因为 test 常用于批量出结果图。

一个容易忽略的要点：Hook 拿到的 `data_batch` 里的图像/点云是**管线加工后的张量**（归一化、pad 过），直接画会失真。所以 Hook 不用 `data_batch`，而是从 `data_sample.img_path / lidar_path` 元信息**重新读原始文件**。

#### 4.4.2 核心流程

```text
tools/test.py --show / --show-dir xxx --task lidar_det
        │
        ▼ trigger_visualization_hook（tools/test.py:70-96）
default_hooks.visualization.draw = True
default_hooks.visualization.show / test_out_dir / vis_task / score_thr ← CLI
        │
        ▼ Runner.test() 循环
after_test_iter(runner, batch_idx, data_batch, outputs)
  ├─ draw=False → 直接返回
  ├─ test_out_dir → 实际落点 = work_dir/{timestamp}/{test_out_dir}（首次拼接）
  └─ 逐条 outputs：
       ├─ 按 vis_task 从 img_path / lidar_path 重读原始数据 → data_input
       ├─ out_file（图像）/ o3d_save_path（点云） ← 数据文件名派生
       └─ visualizer.add_datasample('test sample', data_input, data_sample, ...)
```

#### 4.4.3 源码精读

**默认注册与默认关闭**。[configs/_base_/default_runtime.py:9](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/default_runtime.py#L9) 在 `default_hooks` 里注册了 `visualization=dict(type='Det3DVisualizationHook')`；而构造函数默认 `draw=False`（[mmdet3d/engine/hooks/visualization_hook.py:56-58](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/engine/hooks/visualization_hook.py#L56-L58)）——**默认不画**，画图有渲染开销，不该拖累每次训练。

**CLI 到 Hook 的桥**。[tools/test.py:70-96](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py#L70-L96)：`trigger_visualization_hook` 直接改写 `cfg.default_hooks.visualization` 字典——`draw=True` 无条件打开，`--show` 追加 `show/wait_time`，`--show-dir` 追加 `test_out_dir`，`--task` 必须显式给出（`assert` 五个合法值），`--score-thr` 填入 `score_thr`。这正是 u7-l2 结论的源码落点：**CLI 的 score-thr（默认 0.1）只影响画哪些框，与评估无关**。

**show 与后端互斥**。[mmdet3d/engine/hooks/visualization_hook.py:72-78](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/engine/hooks/visualization_hook.py#L72-L78)：`show=True` 时直接清空 `self._visualizer._vis_backends = {}` 并发 warning——在线展示时再往磁盘/TensorBoard 存一份是浪费。二选一的取舍写在类 docstring（[L24-35](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/engine/hooks/visualization_hook.py#L24-L35)）里。

**验证触发的抽帧逻辑**。[mmdet3d/engine/hooks/visualization_hook.py:108-158](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/engine/hooks/visualization_hook.py#L108-L158)：

- [L113](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/engine/hooks/visualization_hook.py#L113)：`total_curr_iter = runner.iter + batch_idx`——验证时 `runner.iter` 是当前训练迭代数，`batch_idx` 是验证循环内序号，两者相加保证不同 epoch 的验证不会画完全相同的帧（docstring 明言「不保证每次评估画同一批」）。
- [L118-144](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/engine/hooks/visualization_hook.py#L118-L144)：按 `vis_task` 组装 `data_input`——图像用 `mmcv.imfrombytes(..., channel_order='rgb')` 读，`img_path` 为 list（多视角）则逐张读；点云用 `np.frombuffer(..., dtype=np.float32).reshape(-1, num_pts_feats)` 读，`num_pts_feats` 正是 u3-l5 讲过的 `load_dim` 同款信息。
- [L146](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/engine/hooks/visualization_hook.py#L146)：`total_curr_iter % self.interval == 0` 才真正调 `add_datasample`，默认 `interval=50`。

**测试触发的逐条落盘**。[mmdet3d/engine/hooks/visualization_hook.py:160-241](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/engine/hooks/visualization_hook.py#L160-L241)：

- [L174-177](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/engine/hooks/visualization_hook.py#L174-L177)：`test_out_dir` 首次触发时拼接成 `osp.join(runner.work_dir, runner.timestamp, self.test_out_dir)`——所以 `--show-dir vis` 的实际落点是 `work_dir/{时间戳}/vis/`。
- [L186-226](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/engine/hooks/visualization_hook.py#L186-L226)：每条样本派生两个输出名——图像通道 `out_file` 直接用原图文件名（保证与源数据一一对应），点云通道 `o3d_save_path` 取 `.bin` 主名换 `.png`。
- [L228-241](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/engine/hooks/visualization_hook.py#L228-L241)：把 `out_file` 与 `o3d_save_path` 一并传给 `add_datasample`，后者（4.2 节）负责 `mmcv.imwrite` 与 Open3D 截屏（[local_visualizer.py:892-896](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L892-L896)）。

**单测佐证**。[tests/test_engine/test_hooks/test_visualization_hook.py:62-87](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_engine/test_hooks/test_visualization_hook.py#L62-L87) 用 Mock 的 runner 驱动两个触发点：`after_test_iter` 后断言 `hook._test_index` 累加；`draw=False` 时断言输出目录**不存在**、`draw=True` 时断言存在——正好覆盖「默认不画」与「指定目录才落盘」两条行为。

#### 4.4.4 代码实践

1. **实践目标**：不改任何源码，用命令行打开 Hook 并核对落盘路径。
2. **操作步骤**：
   - 准备好 u7-l1 训练出的 PointPillars checkpoint（或任一下载权重）；
   - 运行：

```bash
python tools/test.py configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py \
    work_dirs/pointpillars/epoch_80.pth \
    --task lidar_det --score-thr 0.3 --show-dir vis_pred
```

3. **需要观察的现象**：终端先打出 Hook 相关配置；结束后 `work_dir/{时间戳}/vis_pred/` 下出现一组以点云文件名命名的 `.png`（Open3D 截屏）；若把 `--task` 改成不匹配数据的 `mono_det`，会因 `data_sample` 缺 `img_path` 断言失败。
4. **预期结果**：每条测试样本一张图，图中 3D 框颜色按 KITTI 类别色板区分（下一节综合实践解释来源）。远程服务器无 `DISPLAY` 时不能加 `--show`（会因无显示设备失败，demo 侧有对应降级逻辑，见 [demo/pcd_demo.py:65-70](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/demo/pcd_demo.py#L65-L70)）。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`after_val_iter` 里为什么用 `runner.iter + batch_idx` 而不只是 `batch_idx` 做间隔判断？

**答案**：验证会在训练过程中多次触发，`batch_idx` 每次验证都从 0 重新计数；叠加 `runner.iter`（当前训练迭代）后，不同 epoch 的验证落在不同的取模相位上，避免每次验证都画完全相同的前几帧（docstring：`There is no guarantee that the same batch of images is visualized for each evaluation`）。

**练习 2**：Hook 为什么从 `img_path`/`lidar_path` 重读文件，而不用 `data_batch` 里现成的数据？

**答案**：`data_batch` 的数据已过完整测试管线——图像被归一化、pad、可能堆叠成 batch 张量，点云也可能被裁剪/变换；画在图上要么一片噪声要么尺寸错乱。`data_input` 需要的是人眼可读的原始介质，元信息里保留的路径（u2-l4 讲过 `img_path`/`lidar_path` 属于 metainfo）就是重读入口。

**练习 3**：`--show-dir` 指定的目录最终落在哪三级路径之下？

**答案**：`work_dir/{runner.timestamp}/{test_out_dir}`（[visualization_hook.py:174-177](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/engine/hooks/visualization_hook.py#L174-L177)），时间戳一级保证多次测试互不覆盖。

## 5. 综合实践

**任务**：对 KITTI demo 数据分别产出 BEV 与点云投影两张可视化图，并解释 3D 框颜色的来源链路。

**背景**：demo/data/kitti/ 下自带一对样本——`000008.bin`（LiDAR 点云）、`000008.png`（相机图像）、`000008.pkl`（v2 格式 info，含 GT 框与标定）。官方文档 [docs/en/user_guides/visualization.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/visualization.md) 的四个示例正好覆盖全部原语，我们取其中两个组合成完整脚本（示例代码）：

```python
# bev_and_proj.py —— 示例代码
import mmcv
import numpy as np
from mmengine import load
from matplotlib import pyplot as plt

from mmdet3d.structures import CameraInstance3DBoxes
from mmdet3d.visualization import Det3DLocalVisualizer

info = load('demo/data/kitti/000008.pkl')['data_list'][0]

# ========== 图 1：BEV 俯视 ==========
gt_bboxes = CameraInstance3DBoxes(
    np.array([ins['bbox_3d'] for ins in info['instances']], np.float32))
vis = Det3DLocalVisualizer()
vis.set_bev_image()                                  # 900x900 黑底 + 视锥
vis.draw_bev_bboxes(gt_bboxes, edge_colors='orange') # 手动指定橙色
vis.show()
plt.savefig('outputs/bev_gt.png')                    # 无显示设备时的兜底

# ========== 图 2：点云投影到图像 ==========
img = mmcv.imread('demo/data/kitti/000008.png')
img = mmcv.imconvert(img, 'bgr', 'rgb')
points = np.fromfile('demo/data/kitti/000008.bin', np.float32).reshape(-1, 4)
lidar2img = np.array(info['images']['CAM2']['lidar2img'], np.float32)

vis2 = Det3DLocalVisualizer()
vis2.set_image(img)
vis2.draw_points_on_image(points[:, :3], lidar2img)  # 深度染色散点
mmcv.imwrite(vis2.get_image()[..., ::-1], 'outputs/pts_proj.png')
```

**验收清单**：

1. `bev_gt.png`：橙色旋转矩形数量等于 pkl 中 instance 数；画布中心红 `+` 是 LiDAR 原点、虚线是 ±45° 视锥（[local_visualizer.py:352-374](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L352-L374)）。
2. `pts_proj.png`：点按近红远蓝着色，且点簇与图像里的车辆/道路对齐（投影数学见 4.3.3）。
3. **颜色来源解释**：本脚本 BEV 用了手动 `edge_colors='orange'`——因为**直接调绘制原语不会自动按类别上色**；自动上色只发生在 `add_datasample` 路径，链路是：
   - 数据集类 `METAINFO` 定义 `palette`，如 [mmdet3d/datasets/kitti_dataset.py:55-60](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/kitti_dataset.py#L55-L60) 为 KITTI 8 类各配一色（`Car` 是 `(0, 0, 142)` 深蓝）；
   - Runner/Inferencer 把 `dataset_meta`（含 palette 与 classes）注入 visualizer；
   - `_draw_instances_3d` 里 `get_palette(palette, max_label+1)` 扩展色板后按 `labels_3d` 逐框查表（[local_visualizer.py:658-662](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/visualization/local_visualizer.py#L658-L662)）；
   - 所以「同类别同颜色、颜色由数据集元信息决定、与置信度无关」。
4. **进阶**（有显示设备且装了 Open3D 时）：用 u1-l3 的 `demo/pcd_demo.py` 跑 PointPillars 推理加 `--show`，把交互窗口里看到的预测框颜色与 KITTI 色板对照。

若本机无 Open3D/显示设备，图 1 的 `plt.savefig` 兜底仍可产出（matplotlib 不依赖 DISPLAY）；图 2 完全不依赖 Open3D。整体**待本地验证**。

## 6. 本讲小结

- `Det3DLocalVisualizer` 走三层继承（mmengine → mmdet.DetLocalVisualizer → mmdet3d），2D 能力跨库复用、3D 能力自己实现；绘制与存储解耦，`vis_backends`（Local/Tensorboard）让同一张图多目的地落盘，`@master_only` 保证分布式只在主进程画。
- `add_datasample` 是唯一总入口：按 `vis_task` 五选一分流点云通道（统一转 DEPTH 系后交给 Open3D）与图像通道（matplotlib 投影框、多视角网格拼接）；预测先按 `scores_3d` 过滤，且必须用 `.to('cpu')` 而非 `.cpu()`（框是包装类）。
- 三种绘制原语各有数学：Open3D 框需底面中心→几何中心换算；BEV 框由 `bev` 属性（XYWHR）加两组正交半向量算四角点、乘 scale 换像素、平移画布中心；图像投影统一走齐次坐标除深度的 `points_cam2img`，深度版投影前还要逆放数据增强。
- 3D 框颜色的唯一权威来源是数据集 `METAINFO['palette']` 经 `get_palette` 扩展、按 `labels_3d` 查表；直接调绘制原语不会自动上色。
- `Det3DVisualizationHook` 默认 `draw=False`；`tools/test.py --show/--show-dir` 经 `trigger_visualization_hook` 改写 `default_hooks.visualization` 打开它。验证按 `interval` 抽帧（`runner.iter + batch_idx` 取模），测试逐条落盘到 `work_dir/{timestamp}/{show-dir}`，数据一律从 `img_path/lidar_path` 重读原始文件。
- u7-l2 的结论在本讲落地：CLI `--score-thr` 填的是 `Det3DVisualizationHook.score_thr`，只决定画哪些框，不影响 mAP。

## 7. 下一步学习建议

下一讲 **u7-l4 高阶推理：Inferencer 体系与 TTA** 将补全推理侧的全景：`Base3DInferencer` 如何构建 visualizer（本讲 4.1.3 的 `_init_visualizer` 正是入口）、`--show` 在无显示设备时的降级、以及 `MultiViewWrapper` + `merge_augs` 的测试时增强。如果你更关心训练侧，可先回看 u7-l1 的 hook 体系再读本讲 Hook 会更顺。

源码延伸阅读建议：`mmdet3d/visualization/__init__.py`（本讲全部导出只有 7 个名字，半天可读完）；mmengine 的 `Visualizer`/`BaseVisBackend` 源码（理解 `add_image` 如何广播到后端）；以及 `tools/misc/browse_dataset.py`——它复用同一套 visualizer 在**没有模型**的情况下浏览数据集与增强效果（u7-l5 会把它纳入工具箱）。
