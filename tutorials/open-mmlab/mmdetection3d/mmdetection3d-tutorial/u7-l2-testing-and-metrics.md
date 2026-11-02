# u7-l2 test.py 与评估指标体系

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `tools/test.py` 从解析命令行到启动 `runner.test()` 的完整步骤，理解它与 `tools/train.py` 一样是「薄入口」。
2. 描述 MMEngine `BaseMetric` 的两阶段契约：`process` 逐批收集结果、`compute_metrics` 末期统一计算，以及 Evaluator 在测试循环中的位置。
3. 解释 NuScenesMetric 的 mAP、NDS 与 mATE/mASE/mAOE/mAVE/mAAE 五项真阳误差是怎么算出来的——以及为什么说它本质是「格式转换器 + 官方评测器代理」。
4. 理解 KittiMetric 对 easy/moderate/hard 三档难度的判定标准，以及 bbox（2D）/BEV/3D 三种 IoU 下的 AP11 与 AP40 计算。
5. 分清两个容易混淆的 score 阈值：`tools/test.py --score-thr`（只影响可视化）与 `model.test_cfg.score_thr`（真正影响指标）。

## 2. 前置知识

- **精确率与召回率（P/R）**：对某一类别，把所有预测按置信度从高到低排序，逐个判定为 TP（匹配上真值）或 FP（没匹配上）。精确率 \( P = \dfrac{TP}{TP+FP} \)，召回率 \( R = \dfrac{TP}{TP+FN} \)，其中 FN 是漏检的真值数。AP（Average Precision）就是 P-R 曲线下的面积，mAP 是对所有类别取平均。
- **mAP11 与 mAP40**：KITTI 官方把召回轴均匀切成 41 个采样点（0, 0.025, ..., 1.0）。旧版指标 AP11 只取其中 11 个点（间隔 0.1）求平均，2019 年后的新指标 AP40 取后 40 个点求平均。AP40 更接近真实的曲线下面积，数值通常比 AP11 低。
- **BaseMetric 契约**：MMEngine 中所有评估指标的基类。子类实现两个方法：`process(data_batch, data_samples)` 在每个 batch 推理结束后被调用、把结果攒进 `self.results`；`compute_metrics(results)` 在整个数据集跑完后被调用、返回一个 `Dict[str, float]`。这个「先攒后算」的设计是为了支持分布式收集（`collect_device`）与一次性全局排序。
- **score 阈值的两层含义**：模型推理出的每个 3D 框带一个置信度分数。模型侧的 `test_cfg.score_thr` 在 NMS 前过滤低分框，会改变进入评估器的结果集合；而 `tools/test.py` 的 `--score-thr` 参数只传给可视化 Hook，决定画哪些框，**不影响任何指标**。这是新手最常见的误区。
- **难例（DontCare）**：KITTI 标注中的 DontCare 区域表示「这里有个东西但不参与评分」。落在 DontCare 里的预测既不算 TP 也不算 FP，被豁免统计。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tools/test.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py) | 测试命令行入口：解析参数、改写配置、构建 Runner、启动 `runner.test()` |
| [mmdet3d/evaluation/metrics/nuscenes_metric.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py) | nuScenes 协议指标：结果转官方 json、调 nuscenes-devkit 评测、转写 mAP/NDS |
| [mmdet3d/evaluation/metrics/kitti_metric.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/kitti_metric.py) | KITTI 协议指标：结果转 KITTI 格式、GT 转 kitti_annos、调 kitti_eval |
| [mmdet3d/evaluation/functional/kitti_utils/eval.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/functional/kitti_utils/eval.py) | KITTI AP 的原生实现（numba 加速）：难度过滤、TP/FP 统计、AP11/AP40 |
| [mmdet3d/evaluation/metrics/indoor_metric.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/indoor_metric.py) | 室内指标：直接消费 `eval_ann_info`，调 indoor_eval 算 mAP@0.25/0.5 |
| [mmdet3d/evaluation/metrics/waymo_metric.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/waymo_metric.py) | Waymo 指标：转官方 bin 格式、调 waymo-devkit 评测（MAPH/LET 等指标） |
| [configs/_base_/datasets/kitti-3d-3class.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py) | KITTI 数据基座：`val_evaluator`/`test_evaluator` 如何挂接 KittiMetric |
| [configs/_base_/models/pointpillars_hv_secfpn_kitti.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/pointpillars_hv_secfpn_kitti.py) | PointPillars 模型基座：`test_cfg.score_thr` 等推理参数所在位置 |

## 4. 核心概念与源码讲解

### 4.1 tools/test.py：测试的薄入口

#### 4.1.1 概念说明

`tools/test.py` 与上一讲的 `tools/train.py` 一样不含任何算法逻辑，它只做三件事：把命令行参数「翻译」进配置、用配置构建 Runner、调用 `runner.test()`。训练与测试共用同一套 `Runner.from_cfg` 组装机制，区别只在最后调用的是 `train()` 还是 `test()`。理解这一点后你会发现：**测试的一切行为（用哪个数据集、走什么管线、算什么指标）都由配置文件描述**，`test.py` 只负责临时改写几项。

#### 4.1.2 核心流程

```text
main()
 ├─ parse_args()                 # 解析 config / checkpoint / --cfg-options 等
 ├─ cfg = Config.fromfile(...)   # 执行配置文件，得到完整实验描述
 ├─ cfg.launcher = args.launcher # 分布式启动方式
 ├─ cfg.merge_from_dict(...)     # --cfg-options 深度合并进配置
 ├─ 确定 work_dir                # 优先级：CLI > 配置文件 > 按配置文件名默认
 ├─ cfg.load_from = checkpoint   # 把权重路径写进配置，交给 Runner 加载
 ├─ (可选) trigger_visualization_hook  # --show/--show-dir 时打开可视化
 ├─ (可选) tta 配置替换           # 目前仅支持 3D 分割的 TTA
 ├─ Runner.from_cfg(cfg)         # 构建 model/test_dataloader/test_evaluator...
 └─ runner.test()                # 启动测试主循环
```

#### 4.1.3 源码精读

命令行参数定义集中在 [tools/test.py:L14-L67](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py#L14-L67)。注意 `--score-thr` 的 help 写的是 `bbox score threshold`，但它后续只被塞进可视化 Hook：

- [tools/test.py:L31-L32](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py#L31-L32) 定义 `--score-thr`（默认 0.1）。
- [tools/test.py:L44-L52](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py#L44-L52) 定义 `--cfg-options`，以 `key=value` 形式深度合并进配置——这是不改文件临时调参的正道。

`trigger_visualization_hook` 把可视化参数写进 `default_hooks.visualization`，可以看到 `score_thr` 与 `vis_task` 在这里被注入，只服务画图：

- [tools/test.py:L70-L96](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py#L70-L96) 打开 `draw=True`，写入 `vis_task` 与 `score_thr`；若配置里没有 visualization Hook 会直接报错。

`main` 的骨架：

- [tools/test.py:L103](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py#L103) `Config.fromfile` 加载并执行配置文件（回顾 u1-l5）。
- [tools/test.py:L110-L111](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py#L110-L111) 把 `--cfg-options` 深度合并进配置。
- [tools/test.py:L113-L120](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py#L113-L120) `work_dir` 三级优先级：CLI > 配置文件段 > 以配置文件名生成的默认路径，与 train.py 完全一致。
- [tools/test.py:L122](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py#L122) `cfg.load_from = args.checkpoint`——测试权重不是直接 load，而是写进配置交给 Runner 统一加载。
- [tools/test.py:L127-L133](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py#L127-L133) `--tta` 分支：目前只支持 3D 分割的测试时增强（检测的 TTA 是 TODO），会把 `tta_pipeline` 换进测试数据集、把 `tta_model` 包在原模型外。
- [tools/test.py:L135-L145](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py#L135-L145) 默认走 `Runner.from_cfg(cfg)`（配置里显式给了 `runner_type` 时改从 RUNNERS 注册器构建），最后 `runner.test()` 启动测试。

Runner 在 `test()` 阶段会根据配置中的 `test_dataloader` 与 `test_evaluator` 构建数据加载器和评估器（若未单独定义则回落到 `val_dataloader`/`val_evaluator`）。以 KITTI 基座为例：

- [configs/_base_/datasets/kitti-3d-3class.py:L141-L163](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L141-L163) 定义了 `test_dataloader`（batch_size=1、不 shuffle、test_mode=True）与 `val_evaluator = dict(type='KittiMetric', ann_file=..., metric='bbox')`，且 `test_evaluator = val_evaluator`——**评估器完全由配置驱动**，换数据集只需换这块配置。

而真正影响指标的 score 阈值在模型基座里：

- [configs/_base_/models/pointpillars_hv_secfpn_kitti.py:L91-L98](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/pointpillars_hv_secfpn_kitti.py#L91-L98) `test_cfg` 中 `score_thr=0.1`、`nms_thr=0.01`、`max_num=50`，这些在 Anchor3DHead 的 `get_bboxes`（NMS 阶段）生效，决定进入评估器的框集合。

#### 4.1.4 代码实践

**实践目标**：确认 `--score-thr`（可视化）与 `model.test_cfg.score_thr`（模型）是两个互不相通的旋钮。

**操作步骤**（需要 u7-l1 训练出的 PointPillars checkpoint，或从 model zoo 下载的权重；数据集为 KITTI：

1. 基线测试，记下输出的 Car AP：

   ```bash
   python tools/test.py \
       configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py \
       work_dirs/pointpillars/epoch_80.pth
   ```

2. 换用命令行 `--score-thr 0.9` 再跑一次，对比终端打印的 AP 数字。
3. 改用 `--cfg-options model.test_cfg.score_thr=0.9` 再跑一次，再对比。
4. 加 `--show-dir tmp/` 与 `--task lidar_det` 观察第 2、3 步可视化框数量的差异。

**需要观察的现象**：第 2 步（`--score-thr 0.9`）的 mAP 与基线完全一致，但 `--show-dir` 里只画高分框；第 3 步（`model.test_cfg.score_thr=0.9`）画的框和指标同时变化。

**预期结果**：`--score-thr` 只进入可视化 Hook（tools/test.py:L89），评估器拿到的 `pred_instances_3d` 不变；`model.test_cfg.score_thr` 在 NMS 前裁剪低分框，直接改变送评结果。具体 AP 数值变化幅度「待本地验证」（AP40 对适度提高阈值并不敏感，原因见 4.4）。

#### 4.1.5 小练习与答案

**练习 1**：为什么测试时 `work_dir` 默认是 `./work_dirs/<配置文件名>` 而不是训练目录？

**答案**：[tools/test.py:L113-L120](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py#L113-L120) 的三级优先级里，若 CLI 未给 `--work-dir` 且配置文件中没有 `work_dir` 键，就用配置文件名生成默认目录。测试产物（指标 json、可视化）写在这里；训练目录是配置里的 work_dir，两者可同可不同，想写进训练目录可显式传 `--work-dir`。

**练习 2**：`--tta` 对 PointPillars 有效吗？

**答案**：无效。tools/test.py:L127-L129 注释明确写着「目前只支持 3D 分割的 TTA，检测的 TTA 是 TODO」，且断言配置中必须存在 `tta_model` 与 `tta_pipeline`，PointPillars 的配置没有这两个键，直接传 `--tta` 会断言失败。

---

### 4.2 BaseMetric 契约：process 与 compute_metrics 的两阶段分工

#### 4.2.1 概念说明

mmdet3d 的所有指标类都继承 MMEngine 的 `BaseMetric`，注册在 `METRICS` 节点下。这个契约把评估拆成两阶段：**process（在线、逐批）**只做轻量的搬运——把 GPU 张量搬到 CPU、挑出必要字段塞进 `self.results`；**compute_metrics（离线、末期）**才做重计算——排序、匹配、算 AP。这样设计有三个好处：评估逻辑与模型解耦（换指标不动模型）、支持分布式（各 rank 的 results 先按 `collect_device` 汇聚再算）、指标可以拿到全数据集的全局视图（AP 必须全局排序，单批内算不了）。

调用时机由 Runner 的测试循环驱动（MMEngine 侧行为，非 mmdet3d 源码）：每个 batch 经 `model.test_step` 前向得到一批 `Det3DDataSample`，测试循环随即调用 `Evaluator.process(data_batch, data_samples)`；全部 batch 结束后调用 `Evaluator.compute_metrics(results)`，返回的 `Dict[str, float]` 被日志打印并写入 `work_dir/<时间戳>/` 下的 scalars 数据。

#### 4.2.2 核心流程

```text
runner.test()
 └─ for batch in test_dataloader:
      data_samples = model.test_step(data)      # 预处理 + 前向 + add_pred_to_datasample
      evaluator.process(data_batch, data_samples)   # 逐批攒 results
 └─ metrics = evaluator.compute_metrics(all_results) # 全局排序算 AP
 └─ logger 输出 + MessageHub 记录
```

#### 4.2.3 源码精读

三个指标类的 `process` 几乎逐字相同，以 KittiMetric 为例：

- [mmdet3d/evaluation/metrics/kitti_metric.py:L145-L168](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/kitti_metric.py#L145-L168) 遍历 batch 内每个 `data_sample`，把 `pred_instances_3d` 与 `pred_instances` 的每个张量 `.to('cpu')`，连同 `sample_idx` 一起打包 append 进 `self.results`。**注意它搬运的是整个 `pred_instances_3d`（含 bboxes_3d 框对象），真值标注并不在这一步进来**——KITTI/nuScenes 的 GT 由 metric 自己的 `ann_file` 提供，只有室内指标例外（见 4.5）。

`compute_metrics` 的统一骨架（以 KittiMetric 为例）：

- [mmdet3d/evaluation/metrics/kitti_metric.py:L170-L216](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/kitti_metric.py#L170-L216) 四步走：① 从 `self.dataset_meta` 取 `classes`（dataset_meta 由 Evaluator 在启动时从数据集注入）；② 读自己的 `ann_file` 并把 v2 格式 GT 转成 KITTI 风格；③ `format_results` 把框内结果转成 KITTI 格式；④ 逐个 metric 调 `kitti_evaluate` 得到 `Dict[str, float]`。`format_only=True` 时只落盘不评估（用于向官方测试服务器提交）。

nuScenes 侧同构：

- [mmdet3d/evaluation/metrics/nuscenes_metric.py:L125-L147](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L125-L147) `process`：同样搬运 `pred_instances_3d` / `pred_instances` / `sample_idx` 到 CPU。
- [mmdet3d/evaluation/metrics/nuscenes_metric.py:L149-L184](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L149-L184) `compute_metrics`：读 `ann_file` 的 `data_list`、`format_results` 转官方 json、`nus_evaluate` 调官方评测、汇总 `metric_dict`。

#### 4.2.4 代码实践

**实践目标**：不经模型与数据集，直接手工驱动一个指标类的两阶段契约。

**操作步骤**：

1. 写一个 20 行脚本（示例代码，不是仓库文件）：手工构造一个 `data_sample` 字典，包含 `pred_instances_3d`（`bboxes_3d` 用 `LiDARInstance3DBoxes`、`scores_3d`、`labels_3d` 三个 tensor）、`pred_instances`（空 InstanceData）与 `sample_idx`；实例化 `KittiMetric(ann_file=<kitti_infos_val.pkl>)` 并手动设置 `metric.dataset_meta = dict(classes=['Pedestrian', 'Cyclist', 'Car'])`；先调 `metric.process(None, [data_sample])`，打印 `len(metric.results)`；再调 `metric.compute_metrics(metric.results)`（注意：只有一条样本且无对应 GT 匹配时 AP 为 0，属正常）。
2. 观察 `process` 前后张量的 `device` 变化。

**需要观察的现象**：`self.results` 每个元素恰好包含 `pred_instances_3d`、`pred_instances`、`sample_idx` 三个键；张量已全部在 CPU。

**预期结果**：单样本下 compute_metrics 能跑通但 AP 全为 0（正样本不足）；这一步主要验证你理解「process 攒、compute_metrics 算」的分工。跑通完整流程需要真实数据集，「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `process` 里要把张量 `.to('cpu')`？

**答案**：`self.results` 会一直驻留内存直到整个数据集跑完，若保留 GPU 张量，几千帧的框会撑爆显存；同时分布式汇聚（`collect_device`）默认在 CPU 间通信。

**练习 2**：评估用的真值从哪里来？和模型输入里的 `gt_instances_3d` 是同一份吗？

**答案**：KITTI/nuScenes/Waymo 的 GT 由 metric 自己的 `ann_file` 参数提供（如 kitti_metric.py:L67、nuscenes_metric.py:L164-L165 各自 `load(self.ann_file)`），与测试管线是否加载真值无关；室内指标则复用随 `Det3DDataSample` 流转的 `eval_ann_info`（见 4.5）。两份数据同源（都出自 info pkl），但消费路径不同。

---

### 4.3 NuScenesMetric：mAP、NDS 与五项真阳误差

#### 4.3.1 概念说明

nuScenes 官方评测不用 IoU 匹配，而用**基于中心点距离的匹配**：预测框与真值框只要 BEV 中心距离小于阈值（按类别 0.5m、1m、2m、4m 四档）即算命中，且一帧内每个真值只被得分最高的预测匹配一次。四个距离阈值下各算一个 AP（官方用 500 点积分，与 KITTI 的 41 点不同），再平均得 mAP。

除 mAP 外，官方对每类**匹配成功的真阳（TP）**统计五项误差：mATE（平移）、mASE（尺度）、mAOE（朝向）、mAVE（速度）、mAAE（属性）。NDS（nuScenes Detection Score）把它们与 mAP 揉成一个数：

\[
\text{NDS} = \frac{1}{10}\left( 5 \cdot \text{mAP} + \sum_{i=1}^{5} \left( 1 - \min(\text{err}_i, 1)\right) \right)
\]

即 mAP 贡献一半权重，五项误差（各自截断到 1）贡献另一半。误差越小 NDS 越高。

关键架构事实：**mmdet3d 不自己实现这套评测**。NuScenesMetric 的职责是三段式代理——把框内结果转换成官方 submission json（坐标搬到全局系、尺寸/朝向换到官方约定）、调用 nuscenes-devkit 的 `NuScenesEval`、再把官方产出的 `metrics_summary.json` 转写成 `{前缀}/mAP`、`{前缀}/NDS` 等键。

#### 4.3.2 核心流程

```text
compute_metrics(results)
 ├─ 读 ann_file['data_list']                    # 取每帧 token 与标定
 ├─ format_results(results, classes, prefix)
 │    └─ _format_lidar_bbox / _format_camera_bbox
 │         ├─ output_to_nusc_box: pred_instances_3d → List[NuScenesBox]
 │         │    （dims 重排 [1,0,2]、yaw→四元数、拼速度）
 │         ├─ lidar_nusc_box_to_global: lidar→ego→global，按类别量程过滤
 │         └─ 写 results_nusc.json（sample_token → 框列表）
 ├─ nus_evaluate → _evaluate_single
 │    ├─ NuScenesEval(...).main()               # 官方 devkit 算 mAP/NDS/TP 误差
 │    └─ 读 metrics_summary.json 转写成 metric_dict
 └─ return metric_dict
```

#### 4.3.3 源码精读

类与三个关键映射表：

- [mmdet3d/evaluation/metrics/nuscenes_metric.py:L23-L24](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L23-L24) `@METRICS.register_module()` 注册 `NuScenesMetric`，配置里 `dict(type='NuScenesMetric', ...)` 即路由到它。
- [mmdet3d/evaluation/metrics/nuscenes_metric.py:L52-L67](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L52-L67) `NameMapping`：nuScenes 的 23 个细分类别（如 `human.pedestrian.adult`）折叠到评测用的 10 类（`pedestrian`）。
- [mmdet3d/evaluation/metrics/nuscenes_metric.py:L68-L79](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L68-L79) `DefaultAttribute`：每类的默认属性（如 car → `vehicle.parked`），mAAE 指标要求预测带属性，没有属性分支时用它兜底。
- [mmdet3d/evaluation/metrics/nuscenes_metric.py:L81-L87](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L81-L87) `ErrNameMapping`：devkit 输出的误差名（`trans_err` 等）到展示名（`mATE` 等）的翻译表——这就是五项真阳误差进入最终 metric_dict 的入口。

格式转换链（以 LiDAR 框为例）：

- [mmdet3d/evaluation/metrics/nuscenes_metric.py:L267-L313](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L267-L313) `format_results`：对每个名字含 `pred` 与 `3d` 的结果键（如 `pred_instances_3d`），按 `bboxes_3d` 的实际类型分派到 LiDAR 或相机格式化函数。
- [mmdet3d/evaluation/metrics/nuscenes_metric.py:L560-L630](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L560-L630) `output_to_nusc_box`：把 `pred_instances_3d` 转成官方 `NuScenesBox` 列表。LiDAR 分支（L588-L605）做三件事：尺寸列重排 `box_dims[:, [1, 0, 2]]`（mmdet3d 的 (l,w,h) → 官方 (w,l,h) 约定）、yaw 转绕 z 轴四元数、tensor 第 7:9 列拼成平面速度；相机分支（L606-L624）额外取负 yaw 并叠加一个绕 x 轴 90° 的旋转，把相机系朝向折到地面系。
- [mmdet3d/evaluation/metrics/nuscenes_metric.py:L633-L668](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L633-L668) `lidar_nusc_box_to_global`：先乘 `lidar2ego` 再乘 `ego2global` 把框搬到全局系（官方评测在全局系做跨帧匹配），并用 `eval_configs.class_range` 按类别量程（如 car 50m）过滤超距预测。
- [mmdet3d/evaluation/metrics/nuscenes_metric.py:L485-L557](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L485-L557) `_format_lidar_bbox`：组装 `sample_token → [anno]` 字典并 dump 成 `results_nusc.json`。其中 L517-L536 是一个启发式属性推断：速度模长 > 0.2 的车辆记 `vehicle.moving`、静止行人记 `pedestrian.standing`，否则回落 `DefaultAttribute`——LiDAR 模型没有属性分支，mAAE 全靠这段兜底。

评测与转写：

- [mmdet3d/evaluation/metrics/nuscenes_metric.py:L186-L210](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L186-L210) `nus_evaluate`：对 result_dict 中每个 3D 结果键调 `_evaluate_single` 并合并。
- [mmdet3d/evaluation/metrics/nuscenes_metric.py:L212-L265](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L212-L265) `_evaluate_single` 是官方评测器的调用点：L239-L246 构造 `NuScenesEval`（`eval_set_map` 把 v1.0-mini/v1.0-trainval 映到 mini_val/val 子集）并执行 `main(render_curves=False)`；L249 读回 `metrics_summary.json`；L252-L264 把逐类 `label_aps`、逐类 `label_tp_errors`、全局 `tp_errors`（经 ErrNameMapping 变成 mATE 等）、`nd_score`（NDS）与 `mean_ap`（mAP）逐项转写进 `detail` 字典，键名带 `pred_instances_3d_NuScenes/` 前缀。
- `compute_metrics` 中对 `_evaluate_single` 的调用链在 [mmdet3d/evaluation/metrics/nuscenes_metric.py:L176-L180](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L176-L180)（`nus_evaluate` 内部 L207-L208 真正调用 `_evaluate_single`）——这就是实践任务要求阅读的调用点。

#### 4.3.4 代码实践

**实践目标**：肉眼验证 `format_results` 产出的 submission json 结构。

**操作步骤**：

1. 在安装了 nuscenes-devkit 的环境里，用 4.2.4 的脚本方式构造 `NuScenesMetric(data_root=..., ann_file=..., jsonfile_prefix='tmp/nusc')`，`process` 若干条含 `LiDARInstance3DBoxes` 的假样本后调用 `compute_metrics`（无 devkit 数据时会失败，属预期）；或者更简单——在真实测试时传 `jsonfile_prefix`：
   `--cfg-options val_evaluator.jsonfile_prefix=tmp/nusc_fmt val_evaluator.format_only=True`
2. 打开 `tmp/nusc_fmt/pred_instances_3d/results_nusc.json`，检查 `meta` 与 `results` 两个键；抽一帧看 anno 的字段（translation/size/rotation/velocity/detection_name/detection_score/attribute_name）。

**需要观察的现象**：json 顶层是 `{'meta': ..., 'results': {sample_token: [anno, ...]}}`；`translation` 是全局系坐标（数值可能远大于雷达系量程，因为 ego 原点随时间移动）。

**预期结果**：字段结构与 L538-L547 组装 `nusc_anno` 的键一一对应；`format_only=True` 时不再要求本机有完整 nuScenes 数据。完整数值验证「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：LiDAR 模型没有属性预测分支，mAAE 是怎么来的？

**答案**：`_format_lidar_bbox` 在 [nuscenes_metric.py:L517-L536](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L517-L536) 用速度模长猜属性（>0.2 记 moving），猜不出的类别回落 `DefaultAttribute`（L68-L79）。因此 LiDAR 模型的 mAAE 只反映启发式的命中率，不代表模型真的学了属性。

**练习 2**：为什么 nuScenes 用中心距离而不用 IoU 匹配？

**答案**：官方动机是 BEV 旋转 IoU 对小目标和细长目标（行人、锥桶）过于苛刻——几厘米的定位差就能让小框 IoU 跌破阈值；中心距离直接度量定位质量且与目标尺寸解耦，与 mATE 等 TP 误差的自然定义一致。（这是 nuScenes 论文的设计动机，mmdet3d 只是遵循官方协议。）

---

### 4.4 KittiMetric 与 kitti_eval：难度分级、三种 IoU 与 AP11/AP40

#### 4.4.1 概念说明

KITTI 官方评测与 nuScenes 相反：**IoU 匹配、原生实现**。三层含义：

1. **三种 IoU 口径**：bbox（图像 2D 框 IoU）、bev（BEV 旋转框 IoU）、3d（旋转 IoU × 高度交集）。官方榜单以 IoU 0.7（Car）/0.5（Pedestrian、Cyclist）为主门槛，mmdet3d 同时算 0.7 与 0.5 两档（`min_overlaps`）。
2. **难度分级**：每类按真值的「像素高度、遮挡等级、截断程度」分 easy/moderate/hard 三档分别算 AP，门槛为高度 ≥ 40/25/25 像素、遮挡 ≤ 0/1/2 级、截断 ≤ 0.15/0.3/0.5。
3. **AP11 与 AP40**：召回轴 41 点上取 11 点或 40 点平均，且 precision 曲线先做了单调化（每个点取右侧最大值）。

与 NuScenesMetric 的「代理」角色不同，KittiMetric 自己完成全部计算：`mmdet3d/evaluation/functional/kitti_utils/eval.py` 是从官方 kitti-object-eval-python 移植的 numba 实现。

还有一个容易忽略的点：**KITTI 官方评测在相机坐标系下进行**。因此 KittiMetric 的格式化阶段必须把（可能是雷达系的）预测框经 `lidar2cam` 投到相机系，还要把 3D 框的 8 个角点投到图像平面得到 2D 框——2D AP 用的就是这个投影框，而不是模型直接输出的 2D 框。

#### 4.4.2 核心流程

```text
compute_metrics(results)
 ├─ convert_annos_to_kitti_annos(pkl_infos)   # v2 info → kitti_annos（name/bbox/...）
 ├─ format_results → bbox2result_kitti        # 逐帧：
 │    ├─ convert_valid_bboxes                 #   limit_yaw、lidar→cam、角点投影出 2D 框、
 │    │                                       #   图内外 + 点云范围双重过滤
 │    └─ 组装 name/dimensions/location/rotation_y/alpha/score
 ├─ gt_annos ← data_infos[sample_idx]['kitti_annos']
 └─ kitti_evaluate → kitti_eval(gt, dt, classes, eval_types)
      ├─ clean_data            # 按 difficulty 标记 ignored_gt / ignored_dt / DontCare
      ├─ calculate_iou_partly  # 分块算 metric∈{0:bbox,1:bev,2:3d} 的重叠矩阵
      ├─ compute_statistics_jit# 逐阈值统计 TP/FP/FN
      ├─ get_thresholds        # 从 TP 分数里自适应选 41 个召回采样阈值
      └─ get_mAP11 / get_mAP40 # 对 precision 取 11 点 / 40 点平均 ×100
```

#### 4.4.3 源码精读

构造与入口：

- [mmdet3d/evaluation/metrics/kitti_metric.py:L52-L85](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/kitti_metric.py#L52-L85) `__init__`：`pcd_limit_range` 默认 `[0, -40, -3, 70.4, 40, 0]` 用于过滤框中心出界的预测；`default_cam_key` 默认 `'CAM2'`（KITTI 的主相机，Waymo 继承时换成 `CAM_FRONT`）；L80 的 `allowed_metrics = ['bbox', 'img_bbox', 'mAP', 'LET_mAP']` 划定合法 metric 取值。
- [mmdet3d/evaluation/metrics/kitti_metric.py:L87-L143](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/kitti_metric.py#L87-L143) `convert_annos_to_kitti_annos`：把 v2 info 的 `instances`（`bbox_label`、`bbox_3d`、`truncated`、`occluded`、`alpha`、`bbox`）逐字段搬进 KITTI 风格的 `kitti_annos` 字典，类别经 `label2cat` 还原成字符串名。评测的 GT 侧输入即由它准备。
- [mmdet3d/evaluation/metrics/kitti_metric.py:L218-L251](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/kitti_metric.py#L218-L251) `kitti_evaluate`：对 `results_dict` 里每个结果键决定 `eval_types`——`pred_instances`（2D）或 metric 为 `img_bbox` 时只算 `['bbox']`，否则 3D 结果算 `['bbox', 'bev', '3d']` 三种。**注意配置里 `metric='bbox'` 并不表示只算 2D**，它只是走默认分支，3D 结果仍然三种都算；真正切换到纯图像评测要写 `metric='img_bbox'`。随后调 `kitti_eval` 并把结果键格式化为 `{结果名}/{指标类型}`。

预测侧格式化（重点）：

- [mmdet3d/evaluation/metrics/kitti_metric.py:L555-L650](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/kitti_metric.py#L555-L650) `convert_valid_bboxes`：先 `limit_yaw` 把 yaw 归一到 [-π, π)（L583）；取 `default_cam_key` 的 `lidar2cam` 与 `cam2img`（L595-L599）；LiDAR 框经 `convert_to(Box3DMode.CAM, lidar2cam)` 转相机系、相机框反向转雷达系（L604-L610）；相机系框取 `corners` 经 `points_cam2img` 投影出 8 个角点、min/max 包络得 2D 框（L612-L617）；最后做双重有效性过滤——2D 框与图像有交（L621-L623）且（LiDAR 框时）中心在 `pcd_limit_range` 内（L625-L629）。
- [mmdet3d/evaluation/metrics/kitti_metric.py:L313-L438](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/kitti_metric.py#L313-L438) `bbox2result_kitti`：把有效框组装成 KITTI 格式 anno。L379-L384 计算 `alpha`（观测角）：相机框用 \(-\arctan2(x, z) + \text{yaw}\)，LiDAR 框等价地在雷达系取 \(-\arctan2(-y, x) + \text{yaw}\)。L405-L423 可选写出官方 submission txt（每行 `name -1 -1 alpha bbox hwl xyz ry score`）。
- [mmdet3d/evaluation/metrics/kitti_metric.py:L199-L202](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/kitti_metric.py#L199-L202) GT 与预测按 `sample_idx` 对齐——这解释了为什么 dataloader 必须 `shuffle=False` 之外、metric 还要自己按索引取 GT。

AP 计算核心（functional 层）：

- [mmdet3d/evaluation/functional/kitti_utils/eval.py:L30-L82](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/functional/kitti_utils/eval.py#L30-L82) `clean_data`：难度三要素定义在 L31-L34——`MIN_HEIGHT=[40,25,25]`、`MAX_OCCLUSION=[0,1,2]`、`MAX_TRUNCATION=[0.15,0.3,0.5]`。对每个 GT：同类且不超任一门槛为有效（标 0）；同名易混类（Car↔Van、Pedestrian↔Person_sitting）或超门槛为忽略（标 1）；其余类为无关（标 -1）。检测框同理按高度过滤。L67-L68 收集 DontCare 框。
- [mmdet3d/evaluation/functional/kitti_utils/eval.py:L163-L281](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/functional/kitti_utils/eval.py#L163-L281) `compute_statistics_jit`：贪心匹配——每个 GT 在未分配的检测里挑重叠超 `min_overlap` 且分数最高者（compute_fp 模式下改为挑重叠最大者），统计 TP/FP/FN；`compute_aos` 时累计 \( (1+\cos(\Delta\alpha))/2 \) 的朝向相似度。L249-L268 把与 DontCare 框重叠超阈值的未匹配检测从 FP 中豁免（`nstuff` 扣减）。
- [mmdet3d/evaluation/functional/kitti_utils/eval.py:L10-L27](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/functional/kitti_utils/eval.py#L10-L27) `get_thresholds`：把全部 TP 分数降序排列，按 \( \text{recall} = i / \text{num\_gt} \) 自适应挑出约 41 个等距召回点对应的分数阈值——**这就是 AP 对 score_thr 不敏感的根源**：阈值不是固定的 0~1 均匀网格，而是从实际命中分数里选的，只要低分框还没被模型侧阈值砍掉，它们仍参与排序与选点。
- [mmdet3d/evaluation/functional/kitti_utils/eval.py:L451-L570](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/functional/kitti_utils/eval.py#L451-L570) `eval_class`：主循环按「类别 × 难度 × IoU 档」三重枚举；L483 `N_SAMPLE_PTS = 41`；L545-L558 由累计 TP/FP/FN 算每点的 \( R = \frac{tp}{tp+fn} \)、\( P = \frac{tp}{tp+fp} \)，随后对 precision/recall 做**后缀最大单调化**（每点取其右所有点的最大值），这是 KITTI 官方对 P-R 曲线的平滑方式。
- [mmdet3d/evaluation/functional/kitti_utils/eval.py:L573-L584](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/functional/kitti_utils/eval.py#L573-L584) `get_mAP11`（下标 0,4,8,... 共 11 个点）与 `get_mAP40`（下标 1..40 共 40 个点）求平均再乘 100。
- [mmdet3d/evaluation/functional/kitti_utils/eval.py:L662-L727](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/functional/kitti_utils/eval.py#L662-L727) `kitti_eval`：L681-L687 构造 `min_overlaps`——两档 IoU（0.7/0.5）× 三种 metric × 五类（Car/Pedestrian/Cyclist/Van/Person_sitting）的匹配阈值表；L708-L722 检测双方 alpha 有效时自动追加 AOS（朝向相似度）评测；之后调 `do_eval`（L596-L639，按 eval_types 分别对 metric 0/1/2 调 `eval_class`）并组装 AP11/AP40 结果字符串。

#### 4.4.4 代码实践

**实践目标**：验证难度分级如何改变同一模型的 AP，并对比 AP11 与 AP40。

**操作步骤**（无需改源码，全部用命令行）：

1. 跑一次基线测试（同 4.1.4 第 1 步），在终端输出里找到 `AP11 Results` 与 `AP40 Results` 两个区块。
2. 对照 `Car AP11@0.70, 0.70, 0.70` 一行的三个数，分别对应 easy/moderate/hard；再找 `Car AP40@0.50` 一行对比 IoU 档位差异。
3. 阅读输出中每行之前的表头，回溯到 [eval.py:L733-L748](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/functional/kitti_utils/eval.py#L733-L748) 的字符串拼接处，确认 `{:.2f}, {:.2f}, {:.2f}` 三个格式位就是 min_overlap 的 bbox/bev/3d 三列（同一 metric 下三列相同）。

**需要观察的现象**：easy > moderate > hard 单调递减；同一难度下 AP40 明显低于 AP11；IoU 0.5 档的 3D AP 远高于 0.7 档。

**预期结果**：三个现象都应稳定复现——难度门槛（高度/遮挡/截断）收紧使有效真值减少但同时也更难检；AP40 的 40 点积分比 AP11 的稀疏 11 点更忠实于曲线；IoU 0.5 匹配更宽松。具体数值「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 LiDAR 检测的结果要投到相机坐标系再评？

**答案**：KITTI 官方协议以相机系为准（标注即相机系，2D/BEV/3D AP 与 alpha 都在相机系定义）。`convert_valid_bboxes`（kitti_metric.py:L604-L610）用 info 中的 `lidar2cam` 把雷达系预测转回相机系，2D 框则由相机系 3D 框的 8 角点投影包络而来。这也意味着训练时的坐标系（LiDAR 系）与评测坐标系（CAM 系）不同，靠 Box3DMode 转换衔接（回顾 u2-l1）。

**练习 2**：把 `model.test_cfg.score_thr` 从 0.1 提到 0.5，AP40 大概率几乎不变，为什么提到 0.95 就会崩？

**答案**：`get_thresholds`（eval.py:L10-L27）从实际 TP 分数中自适应选 41 个召回点，低分框存在时只是参与排序，不改变命中集合，AP 几乎不变；但阈值过高会把本可命中的 TP 框在 NMS 前砍掉，真值漏检（FN 上升），召回上不去，AP 随之坍塌。

**练习 3**：`anno['alpha']` 与 `anno['rotation_y']` 有什么区别？

**答案**：`rotation_y` 是目标在相机坐标系的全局朝向角；`alpha` 是观测角——目标相对观测者的朝向（\(-\arctan2(x,z)+\text{yaw}\)，kitti_metric.py:L379-L384），同一辆车转弯时 rotation_y 变、alpha 只在车辆朝向改变时才明显变化。AOS 指标基于 alpha 差计算。

---

### 4.5 一览其他指标：IndoorMetric 与 WaymoMetric

#### 4.5.1 概念说明

- **IndoorMetric**（ScanNet/SUN RGB-D）：室内检测用**体积 IoU**（轴对齐框直接算 3D 交并比），在 IoU 0.25 与 0.5 两档各算一个 mAP——0.25 档源自 ScanNet 论文的宽松标准（室内目标姿态多变，0.25 已算可用）。它与其他指标最大的不同：**真值不用自己读 ann_file，而是消费随 `Det3DDataSample` 流转的 `eval_ann_info` 字段**（回顾 u2-l4：`eval_ann_info` 是唯一裸 dict 字段，在 parse_data_info 阶段生成、经 Pack3DDetInputs 特例通道打包）。
- **WaymoMetric**：又是「转格式 + 调官方 devkit」的代理模式——把预测转成官方 bin 序列文件，调 waymo-open-dataset 的评测器，产出 mAP/MAILH（含 heading 的 AP）与 LET（长时误差）等指标。其 `process` 需要处理纯视觉模型输出的相机系框：投影出 2D 框做图内过滤后，经 `lidar2cam` 逆变换回雷达系；多视角图像模式（`mv_image_based`）下还要先合并 5 个相机的重叠预测。

#### 4.5.2 核心流程

```text
IndoorMetric:  process 攒 (eval_ann_info, pred_instances_3d)
               → compute_metrics → indoor_eval（体积 IoU + mmdet 的 eval_map，多 IoU 档）
WaymoMetric:   process 攒框（相机系框先过滤再转雷达系）
               → compute_metrics → （mv 模式先合并多视角）→ 转官方 bin → waymo devkit 评测
```

#### 4.5.3 源码精读

- [mmdet3d/evaluation/metrics/indoor_metric.py:L39-L58](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/indoor_metric.py#L39-L58) `IndoorMetric.process`：每个样本攒一个二元组 `(eval_ann_info, cpu_pred_3d)`——注意 GT 来自 data_sample 而非独立 ann_file，这是室内链路的独特契约。
- [mmdet3d/evaluation/metrics/indoor_metric.py:L60-L90](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/indoor_metric.py#L60-L90) `compute_metrics`：`box_type_3d` 缺省时回落 `'depth'`（兼容旧 checkpoint），调 `indoor_eval` 并传入 `self.iou_thr`（默认 `[0.25, 0.5]`）。
- [mmdet3d/evaluation/functional/indoor_eval.py:L120-L159](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/functional/indoor_eval.py#L120-L159) 室内版 TP/FP 判定：每帧内用 `pred_cur.overlaps(pred_cur, gt_cur)` 算体积 IoU，逐预测找最大 IoU 的 GT，超过阈值且未被占用记 TP、否则 FP——熟悉的 VOC 式贪心，逐 IoU 档并行统计后由 `average_precision` 得 AP。
- [mmdet3d/evaluation/metrics/waymo_metric.py:L73-L126](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/waymo_metric.py#L73-L126) `WaymoMetric.process`：先 `limit_yaw` 归一角度；相机系框用角点投影 `points_cam2img` 求 2D 包络、与 `img_shape` 判交后过滤（L91-L119），再经 `inv(lidar2cam)` 转回雷达系；额外攒下 `context_name` 与 `timestamp`（Waymo 的 sample 定位键）。注意它攒的是裸 numpy 数组而非框对象。
- [mmdet3d/evaluation/metrics/waymo_metric.py:L128-L150](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/waymo_metric.py#L128-L150) `compute_metrics` 开头：`mv_image_based` 模式下断言结果数是帧数的 5 倍，先 `merge_multi_view_boxes` 把 5 相机结果合并成帧级结果再进入格式化。

#### 4.5.4 代码实践

**实践目标**：对比三族指标的 GT 来源差异。

**操作步骤**：

1. 用 Grep 在 `mmdet3d/evaluation/metrics/` 目录搜索 `eval_ann_info`，统计出现在哪些文件。
2. 再搜索 `self.ann_file` 与 `load(`，观察 KittiMetric/NuScenesMetric/LyftMetric 与 IndoorMetric 的分工。
3. 打开 `configs/votenet/votenet_8xb8_scannet-3d.py` 及其继承的数据基座 `configs/_base_/datasets/scannet-3d.py`，确认 `val_evaluator` 用的是 `IndoorMetric` 且没有 `ann_file` 参数。

**需要观察的现象**：`eval_ann_info` 只在 indoor_metric.py 出现；KittiMetric/NuScenesMetric 的构造参数里都有 `ann_file`。

**预期结果**：室外三件套（KITTI/nuScenes/Waymo）GT 独立成档（可对 test 服务器盲测），室内指标 GT 随样本流转（只能本地评）。这是「待本地验证」级别的结构性结论，grep 结果可直接确认前两条。

#### 4.5.5 小练习与答案

**练习 1**：为什么室内 IoU 阈值用 0.25/0.5，室外用 0.7/0.5？

**答案**：室内框是轴对齐（with_yaw=False，回顾 u2-l2）、目标在房间内姿态与遮挡差异大，ScanNet 官方把 0.25 体积 IoU 定为可用线、0.5 为高质量线；室外车辆/行人框有朝向且 BEV 投影后重叠计算更稳定，KITTI 官方沿用 0.7（Car）/0.5（行人等）的 2D/BEV 传统门槛。

**练习 2**：WaymoMetric 的 `process` 为什么要存 `context_name` 和 `timestamp`？

**答案**：Waymo 官方 bin 格式按场景名 + 时间戳定位每一帧，转换时要把每条预测挂回正确的帧上下文；KITTI/nuScenes 只需一个整型 `sample_idx`/token，Waymo 的定位键是复合的。

---

## 5. 综合实践

**任务**：以一次完整的「阈值扫描」实验串联本讲内容，弄清 score 阈值在「模型 NMS → 结果集 → AP」链路上的真实作用。

前置：u7-l1 训练好的 PointPillars KITTI 模型（或 model zoo 权重）与 KITTI 数据。

1. **基线**：`python tools/test.py <config> <ckpt>`，从输出的 AP40 区块抄下 `Car/Pedestrian/Cyclist` 三类的 3D AP（easy/moderate/hard 九个数）。
2. **假旋钮对照**：加 `--score-thr 0.5` 再跑，确认指标与基线逐位一致——解释它只进了 [tools/test.py:L89](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py#L89) 的可视化 Hook。
3. **真旋钮扫描**：分别用 `--cfg-options model.test_cfg.score_thr=0.3 / 0.5 / 0.8 / 0.95` 跑四次，把九个数填进一张表。
4. **解释**：结合 [eval.py:L10-L27](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/functional/kitti_utils/eval.py#L10-L27) 的 `get_thresholds` 自适应选点机制，说明为什么中等阈值下 AP 几乎不动、过高阈值下召回崩塌；再用 `--cfg-options model.test_cfg.nms_thr=0.5`（收紧 NMS）做一次对照，观察高重叠目标类的 AP 变化方向。
5. **源码追踪**：从 [nuscenes_metric.py:L176-L180](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L176-L180) 的 `nus_evaluate` 调用点出发，画出 NuScenesMetric 的 `compute_metrics → format_results → _evaluate_single → metrics_summary.json → metric_dict` 调用链，标注每一步的输入输出类型。

**产出**：一张阈值-AP 表、一条 5 步调用链图。数据集不可用时，第 3、4 步数值「待本地验证」，但第 2 步的结论（假旋钮不影响指标）可直接从源码推得。

## 6. 本讲小结

- `tools/test.py` 是薄入口：参数翻译进配置（`--cfg-options` 深度合并、`work_dir` 三级优先级、checkpoint 写入 `load_from`），一切由 `Runner.from_cfg(cfg)` 组装、`runner.test()` 驱动。
- `--score-thr` 只进可视化 Hook；真正影响指标的是 `model.test_cfg.score_thr`（NMS 前的分数过滤）。
- 所有指标遵守 BaseMetric 两阶段契约：`process` 逐批把 CPU 化的 `pred_instances_3d` 攒进 `self.results`，`compute_metrics` 末期全局排序计算。
- NuScenesMetric 是「格式转换器 + 官方评测代理」：结果转全局系 submission json → 调 nuscenes-devkit 的 `NuScenesEval` → 从 `metrics_summary.json` 转写 mAP/NDS 与 mATE 等；NDS = mAP 与五项 TP 误差的对半加权。
- KittiMetric 原生计算：GT 转 kitti_annos、预测经 lidar2cam 转相机系并投影 2D 框、双重范围过滤；`kitti_eval` 按「类别 × 难度（高度/遮挡/截断三门槛）× IoU 档（0.7/0.5）」枚举，bbox/BEV/3D 三种 IoU 各算 AP11 与 AP40，阈值由 TP 分数自适应选点。
- IndoorMetric 消费 `eval_ann_info`（GT 随样本流转）、体积 IoU @0.25/0.5；WaymoMetric 复合帧键 + 官方 bin 代理评测。

## 7. 下一步学习建议

- **u7-l3 可视化系统**：本讲反复出现的 `--show-dir`、`vis_task`、可视化 Hook `score_thr` 下一讲正式拆解——`Det3DLocalVisualizer` 如何画 3D 框、`Vis3DHook` 何时触发。
- **u7-l4 高阶推理**：Inferencer 体系内部同样走 `model.test_step` + 评估契约，学完本讲再看 Inferencer 会非常轻松。
- **源码延伸阅读**：`mmdet3d/evaluation/functional/kitti_utils/rotate_iou.py`（BEV 旋转 IoU 的 CUDA 实现，本讲的黑盒）、`mmdet3d/evaluation/functional/lyft_eval.py`（与 nuScenes 同族的距离匹配协议，可对照加深理解）。
