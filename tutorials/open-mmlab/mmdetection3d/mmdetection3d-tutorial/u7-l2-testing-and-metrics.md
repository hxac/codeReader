# test.py 与评估指标体系

## 1. 本讲目标

上一讲我们跟完了 `tools/train.py` 与 Runner 的训练主循环。本讲沿着对称的另一条腿前进——**测试与评估**。学完本讲，你应该能够：

1. 说出 `tools/test.py` 如何从「一个 config + 一个 checkpoint」出发，构建出模型、测试数据加载器与评估器，并启动 `runner.test()`。
2. 辨析两个容易混淆的 `score_thr`：命令行 `--score-thr` 只影响可视化，而 `model.test_cfg.score_thr` 才真正影响 mAP。
3. 解释 MMEngine `BaseMetric` 的两段式契约（`process` 逐批收集、`compute_metrics` 一次性结算），以及它在 `NuScenesMetric` 与 `KittiMetric` 中的落地。
4. 说出 nuScenes 指标体系（mAP、NDS、mATE/mASE/mAOE/mAVE/mAAE）的含义与计算路径，以及 KITTI 的难度分档（easy/moderate/hard）、strict/loose 双阈值与 AP11/AP40 的来源。
5. 对训练出的 PointPillars 模型运行 `tools/test.py`，并通过 `--cfg-options` 扫描不同 `score_thr`，观察并解释 mAP 变化。

## 2. 前置知识

### 2.1 PR 曲线与 AP（温习）

检测器输出的每个框都带一个置信度分数。把分数阈值从高到低滑动，每个阈值下都能统计出：

- **查准率** \( precision = \frac{TP}{TP + FP} \)，检出的框里有多少是对的；
- **查全率** \( recall = \frac{TP}{TP + FN} \)，真值里有多少被检出来了。

以 recall 为横轴、precision 为纵轴画出曲线，曲线下面积就是 **AP**（Average Precision，平均精度）。实践中并不连续积分，而是在若干个 recall 采样点上取「该点及之后的最大精度」（先做单调化再求平均），KITTI 用 41 个采样点。

### 2.2 「匹配」的定义因数据集而异

算 TP 之前必须先回答「预测框和真值框怎样算匹配上」：

- **KITTI**：按 IoU 匹配。2D 框用图像 IoU，BEV/3D 框用旋转框 IoU，且 Car 与行人/骑行者用不同阈值（后文详述）。
- **nuScenes**：按 **BEV 中心点欧氏距离** 匹配（0.5/1/2/4 米四档）。理由是远处框的 IoU 对小的定位误差极其敏感，距离度量更平滑。这部分协议由官方 nuscenes-devkit 实现，mmdet3d 直接调用。
- **室内（ScanNet/SUN RGB-D）**：按 3D IoU（0.25/0.5 两档），继承 VoteNet 论文约定。

这就是为什么 mmdet3d 不做一个「通用 metric」，而是每个数据集族一个 metric 类——**评估协议本身是数据集官方定义的一部分**，复现论文数字的前提是用官方协议。

### 2.3 BaseMetric 与 Evaluator（通俗版）

MMEngine 把「评估」抽象成两个角色：

- **Evaluator**：挂在 Runner 上，测试循环每迭代一次，就把模型的批量输出喂给手下的 metric；循环结束后统一调用结算。
- **BaseMetric**：所有指标的基类，规定了两段式契约：
  - `process(data_batch, data_samples)`：每批调用一次，把需要的字段（预测框、分数、标签、样本号）攒进 `self.results` 列表；
  - `compute_metrics(results)`：全量数据跑完后调用一次，拿 `self.results` 算出最终指标字典。

这个「先攒后算」的设计不是偶然：AP 需要**全局**排序所有预测分数才能画 PR 曲线，单批内根本算不出来。

### 2.4 回顾：预测结果长什么样

上一单元讲过，模型 `predict` 的输出经 `add_pred_to_datasample` 写进 `Det3DDataSample`，测试循环传给 metric 的每个 `data_sample` 里就有：

- `pred_instances_3d`：含 `bboxes_3d`（框对象）、`scores_3d`、`labels_3d`；
- `pred_instances`：多模态/单目模型附带的 2D 预测；
- `sample_idx`：样本索引，用来对齐真值。

本讲的所有 metric 都从这三个字段出发。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tools/test.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py) | 测试薄入口：解析参数、改写配置、交给 `Runner.test()` |
| [mmdet3d/evaluation/metrics/\_\_init\_\_.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/__init__.py) | 8 个 metric 类的总出口（Kitti/NuScenes/Indoor/Waymo/Lyft/Seg/InstanceSeg/PanopticSeg） |
| [mmdet3d/evaluation/metrics/nuscenes_metric.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py) | nuScenes 协议评估：格式化成官方 json → 调官方 NuScenesEval → 汇总 NDS/mAP/五类误差 |
| [mmdet3d/evaluation/metrics/kitti_metric.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/kitti_metric.py) | KITTI 协议评估：转 KITTI 原生标注格式 → 调函数式 `kitti_eval` |
| [mmdet3d/evaluation/functional/kitti_utils/eval.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/functional/kitti_utils/eval.py) | KITTI AP 的 numba 实现：难度过滤、IoU、41 点 PR、AP11/AP40 |
| [mmdet3d/evaluation/metrics/waymo_metric.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/waymo_metric.py) | Waymo 协议：格式化 bin → subprocess 调官方指标二进制 |
| [mmdet3d/evaluation/metrics/indoor_metric.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/indoor_metric.py) | 室内指标：mAP@0.25/0.5（复用 `indoor_eval` 函数） |
| [mmdet3d/models/dense_heads/base_3d_dense_head.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/base_3d_dense_head.py) | `_predict_by_feat_single`：`test_cfg.score_thr` 真正生效的位置 |
| [mmdet3d/engine/hooks/visualization_hook.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/engine/hooks/visualization_hook.py) | `--score-thr` 的实际去向：只控制画图时过滤低分框 |
| [configs/_base_/datasets/kitti-3d-3class.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py) | `val_evaluator = dict(type='KittiMetric', ...)`：metric 的挂接方式 |

调用链总览（自顶向下）：

```
tools/test.py main()
  └─ Runner.from_cfg(cfg)          # 按 config 构建 model/test_dataloader/val_evaluator
      └─ runner.test()             # MMEngine 测试循环
          ├─ 每次 iter：model.test_step → Det3DDataSample(pred_instances_3d)
          │            └─ Evaluator → Metric.process()   # 攒结果
          └─ 循环结束：Evaluator.evaluate → Metric.compute_metrics()  # 结算
                ├─ KittiMetric    → functional/kitti_utils/eval.py（自研 numba 实现）
                ├─ NuScenesMetric → 官方 nuscenes-devkit NuScenesEval
                └─ WaymoMetric    → 官方 compute_detection_metrics_main 二进制
```

## 4. 核心概念与源码讲解

### 4.1 tools/test.py：薄测试入口

#### 4.1.1 概念说明

和 `train.py` 一样，`test.py` 是**薄入口**：它自己不含任何测试逻辑，只做三件事——读配置、按命令行参数改配置、把配置整个交给 `Runner` 并调用 `runner.test()`。所有真正的工作（构建模型、加载权重、跑测试循环、驱动评估器）都由 MMEngine Runner 按 config 完成。

理解它的意义在于：**测试行为的一切旋钮都在 config 里**（`test_dataloader`、`val_evaluator`/`test_evaluator`、`model.test_cfg`），命令行参数只是改配置的快捷方式。

#### 4.1.2 核心流程

```text
main()
 ├─ parse_args()                 # config、checkpoint 两个位置参数 + 可选项
 ├─ cfg = Config.fromfile(...)   # 执行配置文件
 ├─ cfg.launcher / merge_from_dict(cfg_options)   # 分布式方式、命令行覆盖
 ├─ 确定 work_dir                # 优先级：CLI > 配置文件 > 配置文件名默认
 ├─ cfg.load_from = checkpoint   # 权重路径塞进配置，由 Runner 负责加载
 ├─ (可选) trigger_visualization_hook   # --show/--show-dir 时打开可视化
 ├─ (可选) TTA                    # 用 tta_model 包一层、换 tta_pipeline
 ├─ Runner.from_cfg(cfg)         # 组装一切
 └─ runner.test()                # 启动测试主循环
```

#### 4.1.3 源码精读

参数解析中与后续辨析最相关的是 `--score-thr`，默认 0.1：

[tools/test.py:L31-L40](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py#L31-L40) 定义 `--score-thr`（bbox 分数阈值）与 `--task`（可视化任务类型，五选一）。

主流程的骨架：

[tools/test.py:L109-L122](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py#L109-L122) 依次完成：写入 launcher、用 `--cfg-options` 深度合并覆盖配置、按「CLI > 配置段 > 文件名」三级优先级定 `work_dir`，最后把命令行传入的 checkpoint 赋给 `cfg.load_from`——注意权重加载也不发生在 test.py 里，它只是改配置。

[tools/test.py:L135-L145](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py#L135-L145) 收尾两步：默认走 `Runner.from_cfg(cfg)`；若配置里写了 `runner_type` 则改从 RUNNERS 注册节点构建自定义 Runner；最后 `runner.test()` 启动测试。测试与评估的全部逻辑从此刻起都在 MMEngine 与各 metric 类中。

**关键辨析：`--score-thr` 不影响 mAP。** 它的唯一去向是可视化 hook：

[tools/test.py:L70-L96](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py#L70-L96) `trigger_visualization_hook` 把 `draw=True`、`vis_task`、`score_thr` 写进 `default_hooks.visualization`——这只是告诉可视化器「低于该分数的框不要画」。

[mmdet3d/engine/hooks/visualization_hook.py:L146-L158](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/engine/hooks/visualization_hook.py#L146-L158) hook 在 `after_val_iter` 中把 `self.score_thr` 作为 `pred_score_thr` 传给 `add_datasample`，纯粹用于画图过滤。

真正影响评估结果的阈值藏在模型配置里：

[configs/_base_/models/pointpillars_hv_secfpn_kitti.py:L91-L98](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/pointpillars_hv_secfpn_kitti.py#L91-L98) PointPillars 的 `test_cfg`：`score_thr=0.1`、`nms_thr=0.01`、`nms_pre=100`、`max_num=50`。

[mmdet3d/models/dense_heads/base_3d_dense_head.py:L355-L358](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/base_3d_dense_head.py#L355-L358) `_predict_by_feat_single` 从 `test_cfg` 取出 `score_thr`，连同 `max_num` 一起传给 `box3d_multiclass_nms`——**在这里被过滤掉的框根本不会进入 metric**，所以才会改变 mAP。要改它，用 `--cfg-options model.test_cfg.score_thr=0.3`。

另外注意 TTA 分支：

[tools/test.py:L127-L133](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py#L127-L133) 目前只支持 3D 分割的 TTA（注释明言 detection 的 TTA 是 TODO），做法是把 `test_dataloader` 的 pipeline 换成 `tta_pipeline`，并用 `tta_model` 包装原模型。

#### 4.1.4 代码实践

1. **实践目标**：确认「test.py 是薄入口」以及两个 `score_thr` 的不同命运。
2. **操作步骤**：
   - 在仓库根目录执行 `python tools/test.py --help`，浏览所有参数；
   - 打开上一讲训练产出的 work_dir（或官方 PointPillars KITTI checkpoint），跑一次基准测试（**待本地验证**，需要已生成 `data/kitti/kitti_infos_val.pkl`）：

     ```bash
     python tools/test.py \
         configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py \
         work_dirs/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class/epoch_80.pth
     ```

   - 再加 `--score-thr 0.9` 跑一次，比较两次日志里的 `KITTI/Overall_3D_AP40_easy` 等指标。
3. **需要观察的现象**：两次评估指标**完全一致**；只有加 `--show-dir` 时才能看到低分框从可视化中消失。
4. **预期结果**：`--score-thr` 只改可视化（tools/test.py:L89 只写进 visualization hook）。若想真正改变 mAP，应使用 `--cfg-options model.test_cfg.score_thr=0.9` 重跑，指标会变化（见 4.4.4 的扫描实践）。
5. 本实践依赖 KITTI 数据与训练权重，环境不具备时阅读源码走完调用链即可，命令行为标注为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `test.py` 不需要 `train.py` 里的 `--amp`、`--resume` 处理？

**答案**：AMP 混合精度与断点续训都是**训练期**概念；测试只做一次前向、不反传不更新参数，`--amp` 无意义，断点由 `cfg.load_from = args.checkpoint` 一次载入完成（tools/test.py:L122），不需要在多个 checkpoint 间挑选恢复点。

**练习 2**：`--cfg-options model.test_cfg.nms_pre=1000` 会改变什么？

**答案**：`nms_pre` 在 `_predict_by_feat_single` 中用于每层特征图 NMS 前的 topk 预筛（base_3d_dense_head.py:L327-L337），调大它意味着更多候选框能参与 NMS 与分数排序，可能提高 recall 上限，但也会增加后处理耗时；它同样必须经 `model.test_cfg` 生效，而不是任何命令行开关。

### 4.2 评估契约：BaseMetric 的 process / compute_metrics 两段式

#### 4.2.1 概念说明

mmdet3d 的 8 个 metric 类全部继承 MMEngine 的 `BaseMetric`，全部注册在 `METRICS` 节点，全部遵守同一契约：

- `process`：测试循环每批调用，做**轻量**的搬运（搬到 CPU、抽出关心的字段），追加进 `self.results`；
- `compute_metrics`：数据集遍历完后调用一次，做**重**的计算（格式转换、调评估协议、汇总指标字典）。

把重活推迟到 `compute_metrics` 有两个好处：一是 AP 需要全数据集的预测分数统一排序，天然只能最后算；二是 `format_only` 模式（只转格式不计分）可以在同一份代码上实现，方便向官方测试服务器提交结果。

#### 4.2.2 核心流程

```text
Runner 测试循环
  └─ 每次 iter → Evaluator.process(data_batch, outputs)
        └─ 对 batch 中每个 data_sample：
             Metric.process(...) → self.results.append(精简后的 dict)

  循环结束 → Evaluator.evaluate(dataset_length)
        └─（分布式时先跨卡收集 results）
        └─ Metric.compute_metrics(self.results)
             ├─ 读 ann_file 拿真值
             ├─ format_results：预测 → 数据集官方格式
             └─ 调评估协议 → 返回 {'前缀/指标名': 数值} 字典
```

#### 4.2.3 源码精读

先看注册与总出口：

[mmdet3d/evaluation/metrics/\_\_init\_\_.py:L2-L14](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/__init__.py#L2-L14) 8 个 metric 类从这里导出；配置里 `val_evaluator = dict(type='KittiMetric', ...)` 的 `type` 字符串经 `METRICS` 注册节点路由到类（机制见 u4-l1）。

两个主力 metric 的 `process` 几乎逐字相同：

[mmdet3d/evaluation/metrics/nuscenes_metric.py:L125-L147](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L125-L147) 对每个样本把 `pred_instances_3d`、`pred_instances` 里的张量逐个 `.to('cpu')`，连同 `sample_idx` 打包 append 进 `self.results`。[mmdet3d/evaluation/metrics/kitti_metric.py:L145-L168](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/kitti_metric.py#L145-L168) 是同构实现。注意这里**不做任何分数过滤**——低分框也要留给 AP 全局排序。

metric 从哪里知道类别名等元信息？`compute_metrics` 里的 `self.dataset_meta`：

[mmdet3d/evaluation/metrics/kitti_metric.py:L170-L190](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/kitti_metric.py#L170-L190) `compute_metrics` 开头取 `self.dataset_meta['classes']`（由 Evaluator 从数据集 `metainfo` 自动同步给 metric），然后 `load(self.ann_file)` 读真值、`format_results` 转格式。**真值不来自 `results` 里的 data_samples**，而来自 metric 自己持有的 `ann_file`——这印证了 u3-l3 的结论：评估的真值口径由 metric/evaluator 侧独立掌控。

还有 `format_only` 逃生门（两个 metric 的 `__init__` 中均有断言）：

[mmdet3d/evaluation/metrics/nuscenes_metric.py:L111-L115](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L111-L115) `format_only=True` 时必须给 `jsonfile_prefix`，此时只把预测转成官方提交格式、不做评估——用于向 nuScenes 测试服务器（无真值标注）提交。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证 metric 在配置中如何被挂接、`dataset_meta` 从哪来。
2. **操作步骤**：
   - 阅读 [configs/_base_/datasets/kitti-3d-3class.py:L158-L163](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L158-L163)：`val_evaluator = dict(type='KittiMetric', ann_file=data_root + 'kitti_infos_val.pkl', metric='bbox')`，且 `test_evaluator = val_evaluator`；
   - 用 `python tools/misc/print_config.py configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py` 展开配置，在输出中定位 `val_evaluator` 与 `val_dataloader.dataset.metainfo.classes`。
3. **需要观察的现象**：展开后的 `val_evaluator.ann_file` 是完整路径；`metainfo.classes` 为 `['Pedestrian', 'Cyclist', 'Car']`。
4. **预期结果**：确认 Evaluator 构建自 `val_evaluator` 配置块，metric 的 `dataset_meta['classes']` 正来自数据集侧 `metainfo`（若两者类别不一致，评估结果将系统性错位——这是自定义数据集时的高发 bug 点）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `process` 里不直接算每帧的 AP，最后求平均？

**答案**：AP 不是按帧平均的量。PR 曲线要在**整个数据集**上对所有预测分数排序后统计 TP/FP，单帧内无法定义全局 recall；两段式契约正是为此设计。

**练习 2**：`process` 把张量搬到 CPU 再存，为什么？

**答案**：`self.results` 会一直累积到测试结束，若是 GPU 张量，数百上千帧的预测会把显存占满；分布式场景下 results 还要跨卡收集（`collect_device`），CPU 张量才能安全序列化传输。

### 4.3 NuScenesMetric：从预测到 NDS 与 mATE

#### 4.3.1 概念说明

nuScenes 官方评估协议（`detection_cvpr_2019`）的核心思想：**用 BEV 中心距离替代 IoU 做匹配**，再额外统计五类真阳性（TP）误差。最终报告：

- **mAP**：在 0.5/1/2/4 米四个中心距离阈值下 AP 的平均——即「在给定的距离容差内，能以多高精度召回物体中心」。
- **五类 TP 误差**：mATE（平均平移误差，米）、mASE（尺度误差）、mAOE（朝向误差，弧度）、mAVE（速度误差，m/s）、mAAE（属性误差，如 moving/parked 判错的比例）。
- **NDS**（nuScenes Detection Score）：把检测质量与定位质量融成单值：

\[ NDS = \frac{1}{10}\left( 5 \cdot mAP + \sum_{i=1}^{5}\left(1 - \min(TP_i,\, 1)\right) \right) \]

其中 \( TP_i \) 是第 \( i \) 类误差的均值。NDS 由官方 devkit 按上式计算并写入结果文件的 `nd_score` 字段。

mmdet3d 的策略是**不重复实现协议**：把预测格式化成官方 json，直接调用 nuscenes-devkit 的 `NuScenesEval`，再读回结果汇总成日志指标。

#### 4.3.2 核心流程

```text
compute_metrics(results)
 ├─ load(ann_file)['data_list']                # 真值 info（拿 token、标定）
 ├─ format_results(results, classes, jsonfile_prefix)
 │    ├─ 遍历每帧：output_to_nusc_box(预测 → NuScenesBox 对象)
 │    ├─ lidar_nusc_box_to_global(雷达系 → ego 系 → 全局系，途中按类别量程过滤)
 │    ├─ 用速度幅值兜底推断 attribute（vehicle.moving 等）
 │    └─ dump 成 {meta, results} json
 ├─ nus_evaluate → _evaluate_single
 │    ├─ NuScenes(version, dataroot)           # 加载官方数据库
 │    ├─ NuScenesEval(...).main()              # 官方协议评估
 │    └─ mmengine.load(metrics_summary.json)   # 读回结果
 └─ 汇总成 {pred_instances_3d_NuScenes/mAP, .../NDS, .../mATE, 每类 AP...}
```

#### 4.3.3 源码精读

三个类级常量先认识一下：

[mmdet3d/evaluation/metrics/nuscenes_metric.py:L52-L87](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L52-L87) `NameMapping` 把 nuScenes 细粒度类别（`vehicle.bus.bendy`）折叠成评估用的 10 大类（`bus`）；`DefaultAttribute` 给每类一个默认属性（模型不预测属性时的兜底）；`ErrNameMapping` 把 devkit 的误差名（`trans_err`）翻译成日志里的 `mATE`。

构造时锁定评估协议版本：

[mmdet3d/evaluation/metrics/nuscenes_metric.py:L120-L123](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L120-L123) `config_factory(self.eval_version)`（默认 `detection_cvpr_2019`）返回官方 `DetectionConfig`，里面带着各类别的评估量程（class_range，如 car 50 米）等参数，后续量程过滤会用到。

`compute_metrics` 的调度中枢（本讲实践要求走读的调用点）：

[mmdet3d/evaluation/metrics/nuscenes_metric.py:L149-L184](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L149-L184) 依次：从 `dataset_meta` 取 `classes` 与 `version`（v1.0-mini / v1.0-trainval，决定评估切分）；`load(self.ann_file)['data_list']` 读真值 info；`format_results` 产出官方格式 json；随后按 `self.metrics` 列表循环调 `nus_evaluate`。`format_only=True` 时到格式化即止。

格式化阶段做坐标系的「出口转换」：

[mmdet3d/evaluation/metrics/nuscenes_metric.py:L560-L630](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L560-L630) `output_to_nusc_box` 把 `bboxes_3d`/`scores_3d`/`labels_3d` 转成官方 `NuScenesBox`：取 `gravity_center` 作为中心、`dims[:, [1, 0, 2]]` 交换宽长以对齐官方尺寸约定、yaw 构造绕 z 轴四元数、`tensor[i, 7:9]` 两个速度分量直接搬过去——这是 u3-l3 讲过的「nuScenes 9 维框（7+2 速度）」的出口。

[mmdet3d/evaluation/metrics/nuscenes_metric.py:L633-L668](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L633-L668) `lidar_nusc_box_to_global` 用 info 里的 `lidar2ego`、`ego2global` 两级刚体变换把框搬到全局坐标系，途中按 `eval_configs.class_range` 把超出类别量程的预测丢掉。**为什么必须转全局？** nuScenes 官方评估在全局坐标系做匹配，且 ego 车自身位置随时间变化，只有全局系下预测与真值才可比。

属性（attribute）的推断兜底在 LiDAR 分支里：

[mmdet3d/evaluation/metrics/nuscenes_metric.py:L515-L536](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L515-L536) LiDAR 模型通常不预测属性，于是用**速度幅值**启发式判断：速度大于 0.2 m/s 的车辆记 `vehicle.moving`，静止的行人记 `pedestrian.standing`……这些属性参与 mAAE 指标。

最后是「调官方、读结果」：

[mmdet3d/evaluation/metrics/nuscenes_metric.py:L212-L265](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L212-L265) `_evaluate_single` 中 L229-L230 延迟导入官方 `NuScenesEval`（来自 nuscenes.eval.detection.evaluate），L233-L246 构造官方评估器并 `main(render_curves=False)` 跑完协议，L249 读回官方写出的 `metrics_summary.json`，L252-L264 把三类信息铺平进指标字典：每类的 `label_aps`（按 0.5/1/2/4 米距离阈值展开成 `类名_AP_dist_阈值`）、每类的 TP 误差、全局 `tp_errors`（经 `ErrNameMapping` 变成 `mATE` 等）以及 `NDS`/`mAP` 总分。键名统一带 `pred_instances_3d_NuScenes/` 前缀。

#### 4.3.4 代码实践

1. **实践目标**：走读 `compute_metrics` 在 NuScenesMetric 中的调用点与数据流。
2. **操作步骤**：
   - 静态走读：从 [nuscenes_metric.py:L176-L178](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L176-L178)（`nus_evaluate` 调用）进入 `_evaluate_single`，列出从 `self.results` 到 `metrics_summary.json` 的完整字段旅程；
   - 打开任一 nuScenes 配置（如 `configs/centerpoint/centerpoint_pillar02_second_secfpn_8xb4-cyclic-20e_nus-3d.py`），找到 `val_evaluator` 块，确认 `ann_file`、`data_root`、`modality` 三个参数；
   - （可选，需本地有 v1.0-mini 数据）用 mini 数据跑一次 `tools/test.py`，在日志里搜 `mATE`、`NDS`（**待本地验证**）。
3. **需要观察的现象**：日志指标形如 `pred_instances_3d_NuScenes/mAP`、`pred_instances_3d_NuScenes/NDS`、`pred_instances_3d_NuScenes/car_AP_dist_2.0`；评估前控制台会打印 `Formating bboxes of pred_instances_3d`。
4. **预期结果**：能画出「process 攒的 results → format_results 的 json → 官方 NuScenesEval → metrics_summary.json → 指标字典」五步流水线图，并能指出每步的输入输出文件。

#### 4.3.5 小练习与答案

**练习 1**：为什么 NuScenesMetric 要把 `use_camera`/`use_lidar`（`modality`）写进结果 json 的 `meta` 字段？

**答案**：nuScenes 官方排行榜按传感器配置分组（LiDAR / camera / multi-modal），提交结果必须声明用了什么传感器；`format_results` 里 `nusc_submissions = {'meta': self.modality, ...}`（nuscenes_metric.py:L549-L552）就是为提交服务器准备的。

**练习 2**：LiDAR 分支的 mAAE 指标由谁决定？

**答案**：不由模型决定，由 [nuscenes_metric.py:L517-L536](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L517-L536) 的速度启发式决定——速度大于 0.2 m/s 记 moving 类属性。所以纯 LiDAR 模型的 mAAE 基本反映「速度估计对属性的间接推断质量」。

### 4.4 KittiMetric：难度分档与 2D/BEV/3D AP

#### 4.4.1 概念说明

KITTI 官方协议的特点有三：

1. **三档难度**：easy / moderate / hard。按真值框的**图像高度**（远近代理）、**遮挡等级**、**截断比例**三指标过滤真值——小、被遮挡、被截断的目标在 easy 档不计入评估。
2. **双 IoU 阈值**（strict/loose）：Car 用 0.7（loose 0.5），Pedestrian/Cyclist 用 0.5（loose 0.25）。
3. **三种框维度**：bbox（图像 2D）、bev（鸟瞰）、3d（立体），各自报一列 AP；早期论文报 AP11（11 点采样），2019 年后官方改 AP40（40 点采样），本仓库两者都算。

与 nuScenes 不同，KITTI 的 AP 是 mmdet3d **自研实现**（numba 加速），在 `functional/kitti_utils/eval.py`。一个重要细节：**即使是纯 LiDAR 检测器，KITTI 评估也要算 2D 投影框**——难度分档用的是图像高度，2D bbox AP 也是标准输出，所以 KittiMetric 必须把 3D 框投影回相机图像。

#### 4.4.2 核心流程

```text
compute_metrics(results)
 ├─ convert_annos_to_kitti_annos(load(ann_file))   # v2 info → KITTI 原生 anno 九字段
 ├─ format_results → bbox2result_kitti             # 预测同样转 KITTI 原生格式
 │    └─ convert_valid_bboxes：limit_yaw → LiDAR↔CAM 双份框
 │         → 8 角点投影出 2D 框 → 图像内 & 点云范围内双重过滤
 ├─ 按 sample_idx 对齐 gt_annos 与 dt_annos
 └─ kitti_evaluate → functional kitti_eval
      ├─ clean_data：按难度给每个 gt/dt 打 0（计）/1（忽略）/-1（无关）
      ├─ calculate_iou_partly：分块算 dt×gt 重叠（2D/BEV/3D）
      ├─ compute_statistics_jit：每个分数阈值下数 TP/FP
      ├─ get_thresholds：41 个 recall 点对应的分数阈值
      ├─ PR 单调化（后缀最大值）
      └─ get_mAP11 / get_mAP40 → 按 类×难度×严格度 铺平
```

AP 的数学表述（先单调化再平均）：

\[ AP = \frac{1}{|R|}\sum_{r_i \in R} \max_{\tilde{r} \ge r_i} p(\tilde{r}) \]

其中 AP11 取 \( R \) 为 41 个采样点中下标 \( \{0, 4, 8, \dots, 40\} \) 共 11 个；AP40 取下标 \( \{1, 2, \dots, 40\} \) 共 40 个（跳过第 0 点）。

#### 4.4.3 源码精读

`__init__` 里的合法 metric 白名单：

[mmdet3d/evaluation/metrics/kitti_metric.py:L80-L85](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/kitti_metric.py#L80-L85) 允许 `'bbox'`（默认，3D 输入评 bbox/bev/3d）、`'img_bbox'`（纯 2D 评估）、`'mAP'`、`'LET_mAP'`（Waymo 图像侧协议）。

真值的「格式回退」——把 v2 标准 info 翻译回 KITTI 原生标注：

[mmdet3d/evaluation/metrics/kitti_metric.py:L87-L143](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/kitti_metric.py#L87-L143) `convert_annos_to_kitti_annos` 把每条 info 的 `instances` 拆成 `name/truncated/occluded/alpha/bbox/dimensions/location/rotation_y/score` 九字段。这是 u3-l2 讲的「converter 把 KITTI 原始标注升级为 v2 格式」的逆过程——评估器要喂给官方协议实现，而该实现吃的是原生九字段。

预测侧的对应转换在 `convert_valid_bboxes`：

[mmdet3d/evaluation/metrics/kitti_metric.py:L595-L617](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/kitti_metric.py#L595-L617) 无论模型输出 LiDAR 框还是相机框，都同时准备两份：`box3d_camera`（经 `lidar2cam` 转到 CAM 系）与 `box3d_lidar`；再用 `box_preds_camera.corners` 的 8 个角点经 `points_cam2img`（内参 P2 投影）在图像上取 min/max 得到 **2D 投影框**——纯点云模型的 2D bbox AP 就这么来的。

[mmdet3d/evaluation/metrics/kitti_metric.py:L620-L631](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/kitti_metric.py#L620-L631) 双重有效性过滤：2D 投影框必须与图像相交；LiDAR 框中心还须落在 `pcd_limit_range`（默认 `[0, -40, -3, 70.4, 40, 0]`，即 KITTI 点云范围，见 L55）内。被过滤的预测**不进入评估**——这意味着把框后处理量程设得比 `pcd_limit_range` 大没有意义。

`compute_metrics` 的调度与 `kitti_evaluate` 的维度选择：

[mmdet3d/evaluation/metrics/kitti_metric.py:L199-L212](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/kitti_metric.py#L199-L212) 用 `result['sample_idx']` 从 `data_infos` 里逐帧取出 `kitti_annos` 作为 gt，再循环调 `kitti_evaluate`。

[mmdet3d/evaluation/metrics/kitti_metric.py:L239-L249](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/kitti_metric.py#L239-L249) 维度选择规则：若结果是 2D 的 `pred_instances` 或 metric 为 `img_bbox`，只评 `['bbox']`；3D 结果评 `['bbox', 'bev', '3d']`。随后调函数式 `kitti_eval`，返回键加 `{name}/` 前缀（如 `pred_instances_3d/KITTI/Car_3D_AP40_easy`）。

难度分档的权威定义在 functional 层：

[mmdet3d/evaluation/functional/kitti_utils/eval.py:L30-L65](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/functional/kitti_utils/eval.py#L30-L65) `clean_data` 用三组常数给真值打标：`MIN_HEIGHT=[40, 25, 25]` 像素、`MAX_OCCLUSION=[0, 1, 2]`、`MAX_TRUNCATION=[0.15, 0.3, 0.5]`（下标即难度 0/1/2）。每个真值得到三态：`0` 有效（计入分母）、`1` 忽略（匹配上既不算 TP 也不算 FP，相当于 DontCare）、`-1` 无关类。注意评估 `Pedestrian` 时 `Person_sitting` 打 `0`（近邻类特殊处理），评估 `Car` 时 `Van` 同理——这是 KITTI 官方对易混类的约定。

41 点 PR 与 AP 的计算主干：

[mmdet3d/evaluation/functional/kitti_utils/eval.py:L451-L497](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/functional/kitti_utils/eval.py#L451-L497) `eval_class` 按「类 × 难度 × IoU 阈值」三重循环组织：先 `calculate_iou_partly` 分块算完全部重叠矩阵，再对每个组合用 `compute_statistics_jit` 统计各分数阈值下的 TP/FP（numba 加速），`N_SAMPLE_PTS = 41` 定死采样点数。

[mmdet3d/evaluation/functional/kitti_utils/eval.py:L545-L558](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/functional/kitti_utils/eval.py#L545-L558) 由累计计数算出每个采样点的 precision/recall，然后做**后缀最大值**单调化——这正是公式里 \( \max_{\tilde r \ge r_i} p(\tilde r) \) 的代码形态。

[mmdet3d/evaluation/functional/kitti_utils/eval.py:L573-L584](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/functional/kitti_utils/eval.py#L573-L584) `get_mAP11` 每 4 个采样点取 1 个（0,4,…,40 共 11 个）求平均；`get_mAP40` 取下标 1–40 求平均。两者都乘 100 转百分数。

双阈值与结果键名：

[mmdet3d/evaluation/functional/kitti_utils/eval.py:L681-L687](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/functional/kitti_utils/eval.py#L681-L687) `overlap_0_7` / `overlap_0_5` 两个 \( 3 \times 5 \) 矩阵（行是 bbox/bev/3d 三种 metric 维度，列是 Car/Pedestrian/Cyclist/Van/Person_sitting 五类），堆叠成 `[2, 3, 5]` 的 `min_overlaps`——第一维就是 strict/loose 两档。

[mmdet3d/evaluation/functional/kitti_utils/eval.py:L756-L770](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/functional/kitti_utils/eval.py#L756-L770) 结果键形如 `KITTI/Car_3D_AP11_easy_strict`（strict 档）与 `..._loose`；AP40 段（L805-L876）同构。论文里汇报的 `mAP` 通常是 moderate 难度、strict 阈值、AP40 的 3D 指标。

#### 4.4.4 代码实践

1. **实践目标**：验证 `model.test_cfg.score_thr`（而非 `--score-thr`）对 mAP 的真实影响，理解低分框截断与 PR 曲线的关系。
2. **操作步骤**（接 4.1.4 的基准测试，**待本地验证**）：

   ```bash
   CFG=configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py
   CKPT=work_dirs/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class/epoch_80.pth

   for thr in 0.05 0.1 0.3 0.5; do
     python tools/test.py $CFG $CKPT \
       --work-dir work_dirs/score_thr_$thr \
       --cfg-options model.test_cfg.score_thr=$thr 2>&1 | tee log_score_thr_$thr.txt
   done
   ```

3. **需要观察的现象**：从每份日志中摘出 `KITTI/Overall_3D_AP40_easy/moderate/hard` 与 `KITTI/Overall_2D_AP40_*`，按 thr 列表。
4. **预期结果**：score_thr 升高 → AP 单调不升（通常明显下降）。原因：`base_3d_dense_head.py:L355-L358` 在 `box3d_multiclass_nms` 之前就把低于阈值的框丢掉，PR 曲线在高 recall 端被截断，曲线下面积缩小；score_thr 降到 0.05 以下时 AP 趋于稳定（受 `nms_pre=100` 与 `max_num=50` 上限约束）。若发现某档指标纹丝不动，检查是否误用了 `--score-thr`（只影响可视化）。
5. 补充观察：日志里同时有 AP11 与 AP40 两段、strict 与 loose 两档，对比同一次运行中 `Car_3D_AP40_moderate_strict` 与 `..._loose`（IoU 0.7 vs 0.5）的差距，感受 IoU 阈值对严格度的敏感度。

#### 4.4.5 小练习与答案

**练习 1**：一个远处真值框图像高度只有 30 像素、完全遮挡，它在 easy 和 hard 档各算什么？

**答案**：easy 档 `MIN_HEIGHT=40` 且 `MAX_OCCLUSION=0`，该框高度 30 < 40、遮挡等级 2 > 0，被打成「忽略」（`ignored_gt=1`）——预测命中它既不算 TP 也不算 FP；hard 档（25 像素 / 遮挡 ≤2）下它是有效真值（`ignored_gt=0`），漏检计入 FN。见 [eval.py:L55-L65](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/functional/kitti_utils/eval.py#L55-L65)。

**练习 2**：为什么纯 LiDAR 模型（PointPillars）的评估结果里也有 2D bbox AP？

**答案**：`convert_valid_bboxes` 把预测 3D 框转到相机系后，用 8 角点投影到图像取外接矩形得到 2D 框（kitti_metric.py:L612-L617），所以 2D AP 是 3D 预测的「投影副产品」；同时难度分档依赖图像高度，没有投影就无法分档。

**练习 3**：`KittiMetric` 的 `pcd_limit_range` 设成比模型 `point_cloud_range` 更大，会发生什么？

**答案**：不会带来额外收益也不会报错——模型侧 `PointsRangeFilter`/`ObjectRangeFilter` 早已把范围外的目标和点滤掉（见 u3-l6），预测中根本不会有范围外的框，`pcd_limit_range` 的过滤条件恒为真；反之设得更小则会误杀边缘预测、压低 AP。

### 4.5 导览：WaymoMetric、IndoorMetric 与指标家族全景

#### 4.5.1 概念说明

前两节代表了两条实现路线：KittiMetric「自研 numba 协议实现」、NuScenesMetric「调官方 Python devkit」。其余 metric 沿这两条路线分布：

- **WaymoMetric**：官方只提供 C++ 指标二进制，于是「格式化 bin + subprocess 调外部可执行文件」是第三条路线。
- **IndoorMetric**：复用 mmdet3d 自带的 `indoor_eval` 函数（基于 mmdet 的 `eval_map`），按 3D IoU@0.25/0.5 报 mAP，是「函数式自研」的轻量版。
- **SegMetric / InstanceSegMetric / PanopticSegMetric**：点云语义/实例/全景分割的 mIoU/mAP 类指标。
- **LyftMetric**：与 nuScenes 同源（Kaggle 协议），走 devkit 路线。

#### 4.5.2 核心流程（以 WaymoMetric 为例）

```text
compute_metrics(results)
 ├─ (多视角图像模式) 先合并同一帧 5 个视角的框
 ├─ format_results → 写成官方 bin（pred_box/pred_score/pred_class/...）
 └─ waymo_evaluate
      └─ subprocess 调 compute_detection_metrics_main <bin> <gt_bin>
           └─ 解析官方输出文本 → 指标字典（mAP@0.5/0.7、mAPH 按难度 LEVEL_1/2）
```

#### 4.5.3 源码精读

[mmdet3d/evaluation/metrics/waymo_metric.py:L128-L174](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/waymo_metric.py#L128-L174) `compute_metrics` 前半段处理 `mv_image_based` 载入模式（每帧 5 个视角的结果先合并），然后格式化到临时目录，循环 `waymo_evaluate`。

[mmdet3d/evaluation/metrics/waymo_metric.py:L193-L199](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/waymo_metric.py#L193-L199) `metric == 'mAP'` 时拼出命令行字符串，指向仓库内编译的 C++ 二进制 `compute_detection_metrics_main`，用 `subprocess` 执行——评估本身发生在 mmdet3d 进程之外。

[mmdet3d/evaluation/metrics/indoor_metric.py:L39-L58](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/indoor_metric.py#L39-L58) `IndoorMetric.process` 与检测 metric 的差异：真值走 `data_sample['eval_ann_info']`（u2-l4 讲过的唯一裸 dict 字段），预测只要 `pred_instances_3d`——室内评估的真值直接随 Det3DDataSample 流转，不需要独立 ann_file。

[mmdet3d/evaluation/metrics/indoor_metric.py:L60-L90](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/indoor_metric.py#L60-L90) `compute_metrics` 把 (eval_ann, pred) 对列交给 `indoor_eval`，按 `iou_thr=[0.25, 0.5]`（L32 默认值）各算一遍 mAP；`box_type_3d` 缺失时回退 `depth`（兼容旧 checkpoint，L79-L80）。

#### 4.5.4 代码实践

1. **实践目标**：建立「数据集 → metric 类 → 协议实现路线」的对照表。
2. **操作步骤**：
   - 在 `mmdet3d/evaluation/metrics/` 目录下浏览 8 个 metric 文件的头注释与 `__init__` 参数；
   - 对每个文件用一条 grep 确认其协议归属，例如 `grep -n "nuscenes.eval" mmdet3d/evaluation/metrics/nuscenes_metric.py`（官方 devkit）、`grep -n "subprocess" mmdet3d/evaluation/metrics/waymo_metric.py`（外部二进制）、`grep -n "kitti_eval" mmdet3d/evaluation/metrics/kitti_metric.py`（自研函数）。
3. **需要观察的现象**：三类实现路线的 import 特征清晰可辨——官方 Python 包 import、subprocess 外部命令、本地 functional 模块 import。
4. **预期结果**：得到一张多行对照表（KITTI/nuScenes/Waymo/Lyft/室内/分割 × 匹配准则 × 实现路线 × 关键参数），作为日后接入自定义数据集时选择或仿写 metric 的决策依据。

#### 4.5.5 小练习与答案

**练习 1**：WaymoMetric 为什么不像 KittiMetric 那样在 Python 里实现协议？

**答案**：Waymo 官方以 C++ 二进制（`compute_detection_metrics_main`）发布指标计算与真值 bin 生成，Python 侧没有等价 devkit；为了保证与排行榜完全一致，mmdet3d 选择格式化后 subprocess 调官方二进制（waymo_metric.py:L193-L199），代价是必须编译/放置该二进制（`mmdet3d/evaluation/functional/waymo_utils/` 下）。

**练习 2**：IndoorMetric 为什么默认 IoU 阈值是 0.25/0.5 而不是像 KITTI 那样 0.7？

**答案**：室内点云只扫描到物体表面，框的内部是空的，体素 IoU 天然偏低；VoteNet 等论文沿用 0.25/0.5 两档作为室内检测的标准报告口径，IndoorMetric 的默认 `iou_thr=[0.25, 0.5]`（indoor_metric.py:L32）即是对该约定的承接。

## 5. 综合实践

**任务：score_thr 扫描实验 + 双指标体系对照报告。**

1. 准备：完成 u7-l1 的 PointPillars 训练（或下载官方 checkpoint），并已生成 KITTI info pkl（u3-l2）。
2. 基准评估：跑一次默认配置的 `tools/test.py`，从日志抄下 AP11/AP40 两段中 `Overall` 行的 2D/BEV/3D 六个数字。
3. 扫描：按 4.4.4 的脚本对 `model.test_cfg.score_thr ∈ {0.05, 0.1, 0.3, 0.5}` 各跑一次，记录 `KITTI/Overall_3D_AP40_moderate_strict`。
4. 分析：
   - 画出 score_thr–AP 折线，解释为何单调不升；
   - 对比同一运行内 easy/moderate/hard 三档差距，结合 `clean_data` 的三组常数解释「hard 档有效真值更多、recall 要求更严」的原因；
   - 对比 strict 与 loose 档差距，说明 IoU 阈值敏感度。
5. 延伸（可选，需 nuScenes mini 数据）：对 CenterPoint 配置跑一次 `tools/test.py`，在日志中找出 `mAP`、`NDS`、`mATE`，并用 2.1 节的 NDS 公式手工验证 `NDS ≈ (5·mAP + Σ(1−min(TP,1)))/10`（数据来自官方输出的 `metrics_summary.json`，可给 metric 配 `jsonfile_prefix` 参数保留中间文件）。
6. 产出：一份 Markdown 报告，含扫描表格、折线图、三问的因果解释，以及「`--score-thr` 与 `model.test_cfg.score_thr` 辨析」一段（引用本讲两处源码行号）。

本综合实践串起了本讲的全部最小模块：test.py 入口与配置覆盖（4.1）、两段式评估契约（4.2）、KITTI 难度与 AP 计算（4.4），延伸部分覆盖 nuScenes 指标体系（4.3）与其他 metric 路线（4.5）。

## 6. 本讲小结

- `tools/test.py` 是薄入口：改配置（`load_from`、`work_dir`、可视化、TTA）后交给 `Runner.from_cfg` + `runner.test()`，评估由 `val_evaluator`/`test_evaluator` 配置块驱动。
- 命令行 `--score-thr` 只进可视化 hook；`model.test_cfg.score_thr` 在 `box3d_multiclass_nms` 前生效，才是影响 mAP 的阈值——改它用 `--cfg-options model.test_cfg.score_thr=...`。
- 所有 metric 继承 `BaseMetric`，遵守 `process`（逐批攒 CPU 结果）与 `compute_metrics`（全量结算）两段式契约；AP 必须全数据集统一排序，天然只能最后算。
- NuScenesMetric 走「格式化官方 json → 调 nuscenes-devkit NuScenesEval → 读 metrics_summary.json」路线，输出 mAP（中心距离四档平均）、mATE/mASE/mAOE/mAVE/mAAE 与 NDS；LiDAR 模型的属性由速度启发式兜底。
- KittiMetric 走自研 numba 路线：预测与真值都转回 KITTI 原生九字段，`clean_data` 按「图像高度/遮挡/截断」三常数分 easy/moderate/hard 三档并打有效/忽略/无关三态标，41 点 PR 单调化后取 AP11（每 4 点取 1）与 AP40（取 1–40），IoU 双阈值分 strict/loose。
- 其余指标按实现路线归类：Waymo 调官方 C++ 二进制、Lyft 走 devkit、室内与分割复用本地 functional 函数；接入自定义数据集时应对照本讲的多行对照表选择仿写对象。

## 7. 下一步学习建议

- **下一讲 u7-l3（可视化系统）**：本讲多次出现的 `--show-dir`、`Vis3DHook`、`Det3DLocalVisualizer` 将在下一讲展开——你将看到 `pred_score_thr` 在绘图管线里的最终消费方式，以及 BEV 与多模态投影两种渲染。
- **源码延伸阅读**：
  - [mmdet3d/evaluation/functional/kitti_utils/eval.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/functional/kitti_utils/eval.py) 的 `compute_statistics_jit`（L163 起）——TP/FP 计数与 DontCare 抵消的细节；
  - [mmdet3d/evaluation/functional/indoor_eval.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/functional/indoor_eval.py)——室内 mAP 的函数式实现，是自定义 metric 的最小仿写模板；
  - MMEngine 的 `BaseMetric`/`Evaluator` 源码（site-packages 内），确认 `dataset_meta` 注入与分布式结果收集的实现。
- **向后看**：u7-l4 将讲 Inferencer 与 TTA——`tools/test.py:L127-L133` 里那句「detection TTA 是 TODO」的另一半故事在 `mmdet3d/models/test_time_augs/merge_augs.py` 的 `merge_aug_bboxes_3d` 中。
