# u7-l5 实用工具集：分析、浏览与模型转换

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 `tools/analysis_tools/analyze_logs.py` 从训练日志（JSON-lines 格式）画出 loss 曲线与 mAP 曲线，并统计每 iter 平均训练耗时。
2. 用 `tools/analysis_tools/get_flops.py` 估算模型的 FLOPs 与参数量，同时清楚这个「实验性工具」的适用边界。
3. 用 `tools/analysis_tools/benchmark.py` 测量模型的真实 GPU 推理吞吐（fps），理解 warmup、同步计时与 fps 公式，甚至能从源码里读出它的两个小瑕疵。
4. 用 `tools/misc/browse_dataset.py` 逐帧浏览「经过数据管线之后」的训练数据，检查增强与标注是否正确。
5. 辨析一批辅助工具（`print_config.py`、`fuse_conv_bn.py`、`publish_model.py`、`visualize_results.py`）各自的角色，特别是能识别出 `visualize_results.py` 是面向旧版 0.x 配置的遗留脚本。

## 2. 前置知识

- **JSON-lines 日志**：MMEngine 训练时会在 `work_dir` 里写 `xxxx.log.json` 文件，它不是一个大 JSON，而是「一行一个 JSON 字典」，每行记录一次日志打印时刻的全部标量（loss、lr、time 等）。验证阶段的指标键名带斜杠前缀（如 `Kitti metric/Car_3D_moderate_strict_R40`）。
- **FLOPs 与 Params**：Params 是模型参数总量（与输入无关）；FLOPs（浮点运算次数）依赖输入尺寸——点云点数越多、图像分辨率越高，卷积层的运算量越大。二者常用于论文中对比模型复杂度。
- **GPU 计时与 warmup**：CUDA 运算是异步提交的，必须 `torch.cuda.synchronize()` 后再用 `time.perf_counter()` 才能测准；且前几次推理因 CUDA 上下文初始化、cudnn 自动调优而显著偏慢，测速前要先跑几个「热身」样本。
- **`test_step` 与 `data_preprocessor`**：回顾 u4-l2/u4-l3——模型的前向入口是 `loss/predict/_forward` 三模式，而 `model.test_step(data)` 会先经过 `Det3DDataPreprocessor`（图像归一化堆叠、点云体素化）再进 `forward`。本讲的 `benchmark.py` 正是通过 `test_step` 来测「端到端」推理速度的。
- **配置驱动**：这些工具几乎都遵循同一套路：`Config.fromfile` 读配置 →（可选 `--cfg-options` 覆盖）→ 用注册器 build 出数据集或模型 → 干活。这本身就是 u1-l5、u4-l1 所讲模式的一次复习。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tools/analysis_tools/analyze_logs.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/analyze_logs.py) | 解析 `.log.json`：`plot_curve` 画曲线、`cal_train_time` 统计训练耗时 |
| [tools/analysis_tools/get_flops.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/get_flops.py) | 调 mmcv 的 `get_model_complexity_info` 估算 FLOPs/Params |
| [tools/analysis_tools/benchmark.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/benchmark.py) | 加载真实测试数据与 checkpoint，测每秒推理帧数（fps） |
| [tools/misc/browse_dataset.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/browse_dataset.py) | 逐条可视化「管线加工后」的数据集样本 |
| [tools/misc/print_config.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/print_config.py) | 展开打印完整配置（u1-l5 已用过） |
| [tools/misc/fuse_conv_bn.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/fuse_conv_bn.py) | 推理加速：把 Conv+BN 融合成单个 Conv |
| [tools/misc/visualize_results.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/visualize_results.py) | 可视化测试输出的 pkl 结果（注意：0.x 遗留，见 4.5 节） |
| [tools/model_converters/publish_model.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/model_converters/publish_model.py) | 发布模型前瘦身：去优化器状态、转 CPU、文件名追加哈希 |
| [docs/en/user_guides/useful_tools.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/useful_tools.md) | 官方工具文档（部分示例参数名已与代码漂移，本讲会指出） |

## 4. 核心概念与源码讲解

本讲的五个最小模块：**日志分析**、**复杂度分析**、**推理测速**、**数据集浏览**、**辅助工具导览**。它们共同构成日常实验的「效率工具箱」。

### 4.1 模块一：analyze_logs.py——把训练日志变成曲线

#### 4.1.1 概念说明

训练跑完（或跑到一半），你最想问的两个问题是：「loss 降了吗？降得稳吗？」和「一个 iter 到底多慢？」。`analyze_logs.py` 就是对着 `work_dir` 里的 JSON-lines 日志做这两件事的离线工具，它有两个子命令：

- `plot_curve`：画任意键（loss、mAP 等）随 iter 或 epoch 的曲线；
- `cal_train_time`：统计每个 epoch 的平均 iter 耗时、最慢/最快 epoch 与标准差。

它依赖 `seaborn` 与 `matplotlib`（官方文档要求先 `pip install seaborn`，见 [docs/en/user_guides/useful_tools.md:L3-L11](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/useful_tools.md#L3-L11)）。

#### 4.1.2 核心流程

```text
读入一个或多个 .log.json
        │
        ▼
load_json_logs：逐行 json.loads，按 epoch 分桶
  - 只有一行的 dict 键数 ≤ 1 → 跳过（如只含 epoch 的行）
  - 键名含 '/'（验证指标）→ 取 '/' 后半段作为指标名，标记 val_flag
  - 同一行里出现 '/' 键后，其余键不再记录（避免把验证行当训练行）
  - 行内出现 'epoch' 键 → 更新当前 epoch
        │
        ▼
按子命令分发（main 里一句 eval(args.task)(log_dicts, args)）
  ├─ plot_curve：--eval 用 epoch 做 x 轴；否则用 'step'（iter）做 x 轴
  └─ cal_train_time：逐 epoch 求均值（默认丢每个 epoch 第一个 iter）
```

#### 4.1.3 源码精读

**（1）日志解析 `load_json_logs`**：[tools/analysis_tools/analyze_logs.py:L163-L193](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/analyze_logs.py#L163-L193)

- [L174-L176](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/analyze_logs.py#L174-L176)：跳过只含一个键的行（例如单独输出 `epoch` 的行），防止污染。
- [L181-L188](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/analyze_logs.py#L181-L188)：核心分拣逻辑——键名带 `/` 的是验证指标（如 `Kitti metric/xxx`），拆出后半段作键并置 `val_flag=True`；`elif val_flag: continue` 保证同一验证行里的其余键不再被记为训练曲线；普通键则追加进当前 epoch 的列表。
- [L190-L191](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/analyze_logs.py#L190-L191)：行里出现 `epoch` 就推进分桶编号。

**（2）画曲线 `plot_curve`**：[tools/analysis_tools/analyze_logs.py:L36-L97](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/analyze_logs.py#L36-L97)

- [L68-L76](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/analyze_logs.py#L68-L76)：`--eval` 分支——验证指标只在部分 epoch 出现，所以逐 epoch 收集「该 epoch 是否有值」，x 轴是 epoch，画带 `marker='o'` 的稀疏点。
- [L77-L88](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/analyze_logs.py#L77-L88)：训练指标分支——x 轴用日志行里的 `step`（全局 iter），多条 epoch 的 (x, y) 数组拼接后连线。
- [L92-L97](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/analyze_logs.py#L92-L97)：`--out` 给了就 `savefig`，否则弹窗 `plt.show()`（无显示环境时记得传 `--out` 与/或 `--backend Agg`）。

**（3）统计耗时 `cal_train_time`**：[tools/analysis_tools/analyze_logs.py:L11-L33](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/analyze_logs.py#L11-L33)

- [L16-L19](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/analyze_logs.py#L16-L19)：默认丢弃每个 epoch 的**第一个** iter 耗时（每个 epoch 开头 dataloader 冷启动最慢），加 `--include-outliers` 才计入。
- [L24-L33](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/analyze_logs.py#L24-L33)：先算每个 epoch 的均值，再输出最慢/最快 epoch、跨 epoch 标准差与总平均 s/iter。这正是 u7-l1 里「用日志判断数据瓶颈还是算力瓶颈」的直接工具。

**（4）子命令注册**：[tools/analysis_tools/analyze_logs.py:L153-L160](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/analyze_logs.py#L153-L160) 用 argparse 的 subparsers 定义 `plot_curve` 与 `cal_train_time`，`main` 末尾 [L205](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/analyze_logs.py#L205) 用 `eval(args.task)(log_dicts, args)` 按名分发——函数名即子命令名。

**（5）一个文档漂移的警示**：官方文档示例用的是 `--mode eval --interval 2`（[docs/en/user_guides/useful_tools.md:L33-L35](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/useful_tools.md#L33-L35)），但代码里真正的参数是 `--eval` 与 `--eval-interval`（[tools/analysis_tools/analyze_logs.py:L114-L122](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/analyze_logs.py#L114-L122)）。**工具用法永远以脚本 `--help` 输出为准**，这是读工具源码的第一个动机。

#### 4.1.4 代码实践

1. **实践目标**：不训练也能上手——用官方发布的 PointPillars 训练日志画出 loss 曲线与验证 mAP 曲线。
2. **操作步骤**：
   - 下载官方日志（该链接来自 [configs/pointpillars/README.md:L26](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/pointpillars/README.md#L26) 的 log 列）：

     ```bash
     wget https://download.openmmlab.com/mmdetection3d/v1.0.0_models/pointpillars/hv_pointpillars_secfpn_6x8_160e_kitti-3d-3class/hv_pointpillars_secfpn_6x8_160e_kitti-3d-3class_20220301_150306.log.json
     ```

   - 先看一眼日志里有哪些键：`tail -n 3 *.log.json`。你会看到训练行有 `loss`、`lr`、`time`、`step` 等键；验证行的键形如 `xxx/Car_3D_moderate_strict_R40`（带 `/` 前缀）。
   - 画训练 loss 曲线（iter 为 x 轴）：

     ```bash
     python tools/analysis_tools/analyze_logs.py plot_curve \
         hv_pointpillars_secfpn_6x8_160e_kitti-3d-3class_20220301_150306.log.json \
         --keys loss --legend loss --backend Agg --out loss_curve.png
     ```

   - 画验证 mAP 曲线（epoch 为 x 轴）。`--keys` 只填 `/` 后半段；该配置 `val_interval=2`（[configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py:L128](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py#L128)），所以传 `--eval-interval 2`：

     ```bash
     python tools/analysis_tools/analyze_logs.py plot_curve \
         hv_pointpillars_secfpn_6x8_160e_kitti-3d-3class_20220301_150306.log.json \
         --keys Car_3D_moderate_strict_R40 --eval --eval-interval 2 \
         --backend Agg --out map_curve.png
     ```

     （若提示键不存在，按 tail 看到的真实键名替换 `--keys`。）
   - 顺手统计训练速度：`python tools/analysis_tools/analyze_logs.py cal_train_time <同一日志>`。
3. **需要观察的现象**：loss 曲线是否平滑下降、有无周期性锯齿（与 cyclic 学习率联动）；mAP 曲线只在偶数 epoch 上有点（验证只在 `val_interval` 的倍数触发）。
4. **预期结果**：`loss_curve.png` 与 `map_curve.png` 生成；`cal_train_time` 打印形如 [docs/en/user_guides/useful_tools.md:L44-L52](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/useful_tools.md#L44-L52) 的四行统计。具体曲线形态与官方日志中的键名（版本间可能略有差异）——**待本地验证**。

#### 4.1.5 小练习与答案

1. **问**：`cal_train_time` 为什么默认丢弃每个 epoch 的第一个 iter？如何关掉这个行为？
   **答**：每个 epoch 的第一次迭代包含 dataloader 冷启动等一次性开销，会拉高均值；代码在 [L16-L19](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/analyze_logs.py#L16-L19) 用 `log_dict[epoch]['time'][1:]` 切掉首元素，加 `--include-outliers` 可保留。
2. **问**：`plot_curve` 画训练 loss 与画验证 mAP 时，x 轴分别取什么？对应哪段代码？
   **答**：训练指标用日志行里的 `step`（iter），[L77-L88](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/analyze_logs.py#L77-L88)；带 `--eval` 的验证指标用 epoch 编号，[L68-L76](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/analyze_logs.py#L68-L76)。
3. **问**：`load_json_logs` 遇到键名含 `/` 的行会做什么？为什么同一行后面的其他键被跳过？
   **答**：带 `/` 的键说明这行是验证阶段写下的指标，取 `/` 后半段作为指标名并置 `val_flag=True`（[L181-L184](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/analyze_logs.py#L181-L184)）；`elif val_flag: continue`（[L185-L186](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/analyze_logs.py#L185-L186)）防止同一行的训练类键（如 step）混进验证序列。

### 4.2 模块二：get_flops.py——估算模型复杂度

#### 4.2.1 概念说明

`get_flops.py` 回答「这个模型有多大、多贵」：Params 衡量权重规模，FLOPs 衡量一次前向的计算量。它本体只有 80 行，真正干活的是 mmcv 的 `get_model_complexity_info`——该函数会给模型的每个 `nn.Module` 挂上钩子，前向时按层统计乘加次数并累加：

\[
\text{FLOPs} = \sum_{l \in \text{layers}} \text{flops}(l), \qquad
\text{flops}(\text{conv}_l) \approx k_h k_w c_{in} c_{out} \cdot H_{out} W_{out}
\]

而 Params 与输入形状无关：\(\text{Params} = \sum_l \#\theta_l\)。这也是文档强调「FLOPs 与输入形状相关、params 无关」的原因（[docs/en/user_guides/useful_tools.md:L136-L138](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/useful_tools.md#L136-L138)）。

#### 4.2.2 核心流程

```text
parse_args：config + --shape（默认 40000 4）+ --modality（point/image/multi）
        │
        ▼
按 modality 把 --shape 整形成 input_shape
  point: (N, C)          —— 两个数
  image: 1 个数→(3,s,s) / 2 个数→(3,H,W)
  multi: 直接 NotImplementedError
        │
        ▼
Config.fromfile → 可选 --cfg-options 覆盖 → init_default_scope
        │
        ▼
MODELS.build(cfg.model)（只建模型，不载权重）→ cuda + eval
        │
        ▼
mmcv get_model_complexity_info(model, input_shape)
  —— 用 input_shape 造哑输入喂模型，逐层 hook 计数 → 打印 Flops / Params
```

#### 4.2.3 源码精读

**（1）参数与输入形状整形**：[tools/analysis_tools/get_flops.py:L16-L42](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/get_flops.py#L16-L42) 定义 `--shape` 默认 `[40000, 4]`（约 4 万个点、每点 4 维），`--modality` 三选一。[L48-L61](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/get_flops.py#L48-L61) 做整形：`point` 强制二元组；`image` 一个数补成 `(3, s, s)`、两个数拼成 `(3, H, W)`；`multi`（点云+图像）直接抛 `NotImplementedError`。

**（2）构建模型与计数**：[tools/analysis_tools/get_flops.py:L63-L73](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/get_flops.py#L63-L73)。流程是熟悉的 `Config.fromfile` → `merge_from_dict`（`--cfg-options`）→ `init_default_scope` → `MODELS.build(cfg.model)`，随后 `get_model_complexity_info(model, input_shape)`（[L11-L13](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/get_flops.py#L11-L13) 从 mmcv 导入，缺失时直接报 ImportError）。打印结果后，脚本**自己**追加了一句警告：结果用于论文前必须人工复核（[L77-L79](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/get_flops.py#L77-L79)）。

**（3）为什么它对 3D 检测器是「实验性」的**——这是本模块最重要的源码级理解。mmcv 的计数器按其契约会构造一个由 `input_shape` 决定的哑张量直接喂给 `model(...)`，这条路径**跳过了 `data_preprocessor`**。而 VoxelNet 的特征提取第一步就要取体素字典：

- [mmdet3d/models/detectors/voxelnet.py:L36-L44](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/voxelnet.py#L36-L44)：`extract_feat` 首行 `voxel_dict = batch_inputs_dict['voxels']`——这个 dict 在正常链路里由 `Det3DDataPreprocessor.voxelize()` 生产（u4-l3）。
- [mmdet3d/models/detectors/base.py:L87-L88](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/base.py#L87-L88)：哑张量会走 `mode='tensor'` 分支进入 `_forward`，最终同样落到 `extract_feat`。

也就是说，`--shape 40000 4` 喂进去的是「原始点张量」，而体素化检测器期待「体素化后的 dict」；文档也只承诺「单阶段、单模态」支持（[docs/en/user_guides/useful_tools.md:L139](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/useful_tools.md#L139)）。GN、自定义 CUDA 算子（如稀疏卷积、pillar scatter）不被计入 FLOPs（[L138](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/useful_tools.md#L138)），所以 3D 场景下这个数字**只适合粗略横向对比**。

#### 4.2.4 代码实践

1. **实践目标**：跑通（或解释失败原因）PointPillars 的 FLOPs 估算，理解工具边界。
2. **操作步骤**：在仓库根目录、已安装 mmcv 的环境中运行：

   ```bash
   python tools/analysis_tools/get_flops.py \
       configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py \
       --modality point --shape 40000 4
   ```

3. **需要观察的现象**：有两种可能，都值得记录——
   - **成功**：打印 `Input shape / Flops / Params` 三行（对照 [docs/en/user_guides/useful_tools.md:L124-L130](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/useful_tools.md#L124-L130) 的示例格式，但具体数值以你本地为准）；
   - **报错**（如 `KeyError`/`TypeError`）：结合 4.2.3（3）分析——哑张量绕过了 `data_preprocessor`，`extract_feat` 拿不到 `voxels` 字典。若出现，这本身就是「实验性工具」边界的实证。
   - 变化 `--shape 16000 4` 再跑一次，对比 FLOPs 与 Params 哪个变了、为什么。
4. **预期结果**：FLOPs 随点数近似成比例变化，Params 不变；体素类模型能否直接跑通**待本地验证**。若失败，可作为扩展练习：写 20 行脚本，自己用 `VoxelizationByGridShape`（u4-l3）把随机点云体素化成 `voxel_dict` 后手动喂给 `model.extract_feat`，绕开哑输入路径。

#### 4.2.5 小练习与答案

1. **问**：`--modality image --shape 800` 会构造出什么输入形状？
   **答**：`(3, 800, 800)`——一个数被补成方形，见 [tools/analysis_tools/get_flops.py:L51-L53](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/get_flops.py#L51-L53)。
2. **问**：为什么把 `--shape` 从 `40000 4` 改成 `16000 4`，Params 不变而 FLOPs 变？
   **答**：Params 是权重张量的元素个数总和，与前向输入无关；FLOPs 由逐层 hook 累加，卷积类层的运算量随输入空间尺寸（点数/分辨率）变化，公式见 4.2.1。
3. **问**：多模态模型（如 MVXNet）能用这个脚本吗？
   **答**：不能，`--modality multi` 直接 `NotImplementedError`（[tools/analysis_tools/get_flops.py:L58-L61](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/get_flops.py#L58-L61)），官方文档也只承诺单阶段单模态。

### 4.3 模块三：benchmark.py——测真实推理吞吐

#### 4.3.1 概念说明

FLOPs 是「纸面复杂度」，实际部署关心的是**每秒能处理几帧**（fps）。`benchmark.py` 与 `get_flops.py` 的本质区别：它不造哑输入，而是**构建配置里的真实 `test_dataloader`**、加载真实 checkpoint、对每个 batch 调 `model.test_step(data)`——完整覆盖数据预处理（含体素化）与前向，是端到端测速。

#### 4.3.2 核心流程

```text
读 config + checkpoint
  ├─ cudnn_benchmark 开关 → torch.backends.cudnn.benchmark
  ├─ Runner.build_dataloader(cfg.test_dataloader)   ← 真实数据
  ├─ MODELS.build(cfg.model) + load_checkpoint
  ├─ （可选）fuse_conv_bn 折叠 Conv+BN
  └─ model.to(device).eval()

逐 batch 循环（i 从 0 计）：
  torch.cuda.synchronize() → t0 = perf_counter()
  model.test_step(data)（可选 autocast 混合精度）
  synchronize() → elapsed = perf_counter() - t0
  i ≥ 5（warmup）后累加 pure_inf_time，每 log_interval 打印一次 fps
  达到 samples 打印 Overall fps 并 break
```

fps 的定义：

\[
\text{fps} = \frac{\text{有效样本数} - \text{warmup 数}}{\text{有效样本累计耗时}}
\]

#### 4.3.3 源码精读

**（1）装配真实环境**：[tools/analysis_tools/benchmark.py:L37-L54](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/benchmark.py#L37-L54)。`cudnn_benchmark`（L42-L43）开启后 cudnn 会为固定输入形状自动选最快卷积实现；`Runner.build_dataloader`（L46）复用 Runner 的工厂来建测试数据加载器；`load_checkpoint(model, args.checkpoint, map_location='cpu')`（L50）载权重；`--fuse-conv-bn` 时先折叠再上设备（L51-L53）。注意 [L12](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/benchmark.py#L12) `from tools.misc.fuse_conv_bn import fuse_module`——它按「包路径」跨工具目录导入，因此**必须在仓库根目录运行**；若报 `ModuleNotFoundError: No module named 'tools'`，先执行 `export PYTHONPATH=$PWD:$PYTHONPATH`。

**（2）同步计时与 warmup**：[tools/analysis_tools/benchmark.py:L56-L77](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/benchmark.py#L56-L77)。`num_warmup = 5`（L57）跳过前 5 个样本；每次前后 `torch.cuda.synchronize()`（L63、L69）确保 GPU 真正执行完再停表；`--amp` 时用 `autocast` 包住 `model.test_step(data)`（L66-L67）。

**（3）两个值得发现的源码瑕疵**（读工具源码的增值部分）：

- **参数缺 `type=int`**：[L19-L21](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/benchmark.py#L19-L21) 的 `--samples` 与 `--log-interval` 只给了 `default=2000/50` 而没有 `type=int`。默认值是 int，一切正常；但一旦你在命令行传 `--log-interval 50`，argparse 存进去的是**字符串** `'50'`，随后 [L74](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/benchmark.py#L74) 的 `(i + 1) % args.log_interval` 会抛 `TypeError`；而传 `--samples 100` 时 [L79](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/benchmark.py#L79) 的 `(i + 1) == args.samples` 恒为 False，脚本会一路跑完整个 dataloader。**实践建议：直接用默认值，别传这两个参数。**
- **最后一个样本被计时两次**：[L72-L73](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/benchmark.py#L72-L73) 已经把最后一个样本的 `elapsed` 加进 `pure_inf_time`，[L79-L81](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/benchmark.py#L79-L81) 又加了一次——分母多算一份，因此 `Overall fps` 会比最后一次周期打印的 fps **略低**。误差量级约 1/有效样本数，不影响量级判断，但值得知道。

#### 4.3.4 代码实践

1. **实践目标**：测 PointPillars 在你 GPU 上的端到端推理吞吐，并对比混合精度与 Conv+BN 融合的加速。
2. **操作步骤**：
   - 准备：按 u3-l2 生成 KITTI info pkl（`data/kitti/` 下有 `kitti_infos_val.pkl` 与 `training/velodyne_reduced/`）；下载官方 checkpoint（来自 [configs/pointpillars/README.md:L26](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/pointpillars/README.md#L26)）：

     ```bash
     wget https://download.openmmlab.com/mmdetection3d/v1.0.0_models/pointpillars/hv_pointpillars_secfpn_6x8_160e_kitti-3d-3class/hv_pointpillars_secfpn_6x8_160e_kitti-3d-3class_20220301_150306-37dc2420.pth
     ```

   - 在仓库根目录（必要时 `export PYTHONPATH=$PWD:$PYTHONPATH`）运行，**不要传 `--samples/--log-interval`**（理由见 4.3.3（3））：

     ```bash
     python tools/analysis_tools/benchmark.py \
         configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py \
         hv_pointpillars_secfpn_6x8_160e_kitti-3d-3class_20220301_150306-37dc2420.pth
     ```

   - 对照实验：分别加 `--amp`、`--fuse-conv-bn`、两者都加，各跑一遍并记录 fps。
3. **需要观察的现象**：前 5 个样本被跳过不打印；之后每 50 个样本打印一行 `fps: xx.x sample / s`；结束时打印 `Overall fps`（应与最后一行周期打印接近但略低，见 4.3.3（3）的第二点）。`--fuse-conv-bn` 后 fps 上升（2D backbone 里大量 Conv+BN 被折叠）。
4. **预期结果**：三种加速配置的 fps 均不低于基线；具体数值依赖 GPU 型号与 KITTI 数据是否就绪——**待本地验证**。若暂时没有 KITTI 数据，可先用 u7-l1 里缩小的数据集验证流程。

#### 4.3.5 小练习与答案

1. **问**：为什么要 `num_warmup = 5`？为什么要前后两次 `torch.cuda.synchronize()`？
   **答**：前几次推理含 CUDA 上下文初始化、cudnn 自动调优等一次性开销（[L56-L58](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/benchmark.py#L56-L58)），需剔除；CUDA 调用是异步的，只有 synchronize 之后 `perf_counter` 的差值才代表 GPU 真实耗时（[L63-L70](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/benchmark.py#L63-L70)）。
2. **问**：`benchmark.py` 测的速度为什么比 FLOPs 推出来的「理论速度」更有说服力？
   **答**：它走 `model.test_step(data)`，完整包含 `data_preprocessor`（图像归一化/堆叠、点云体素化）、真实数据加载与 NMS 后处理；FLOPs 只数被支持的算子，稀疏卷积、scatter、NMS 等都不计入。
3. **问**：`--log-interval 10` 会发生什么？为什么？
   **答**：会抛 `TypeError`。`--log-interval` 没声明 `type=int`，命令行传入的是字符串 `'10'`，[L74](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/analysis_tools/benchmark.py#L74) 里 `(i + 1) % args.log_interval` 对 int 和 str 取模会报错——这是给社区提 PR 的小好机会（补上 `type=int` 即可）。

### 4.4 模块四：browse_dataset.py——看见管线输出什么

#### 4.4.1 概念说明

u3 单元讲过：数据集类读 info pkl，pipeline 把原始数据加工成 `dict(inputs, data_samples)`。但「管线到底输出什么」是抽象的——`browse_dataset.py` 把每条样本**按管线跑一遍并画出来**，让你亲眼确认：GT 框位置对不对、增强有没有把目标转出界、点云是不是你期望的形状。它是排查「数据问题导致训练不收敛」的第一工具。

#### 4.4.2 核心流程

```text
build_data_cfg：
  读 config（可 --cfg-options 覆盖）
  ├─ 剥壳：RepeatDataset / CBGSDataset → 内层 dataset；ConcatDataset → 第一个
  ├─ --aug：pipeline 用 train_pipeline（看增强后的样子）
  └─ 默认：pipeline 用 test_pipeline，但把训练管线里的
        LoadAnnotations3D 与 Pack3DDetInputs（带 gt 键的版本）缝进去
        —— 没有标注就没法检查 GT
main：
  DATASETS.build（filter_empty_gt=False，不丢空帧）
  → VISUALIZERS.build + dataset_meta（类别、调色板）
  → 逐条遍历 dataset：add_datasample(...) 按 --task 选择画法
```

#### 4.4.3 源码精读

**（1）管线拼装 `build_data_cfg`**：[tools/misc/browse_dataset.py:L55-L91](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/browse_dataset.py#L55-L91)

- [L65-L71](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/browse_dataset.py#L65-L71)：剥掉 `RepeatDataset`/`CBGSDataset` 包装、`ConcatDataset` 只取第一个——否则浏览会把同一份数据看 N 遍。
- [L75-L87](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/browse_dataset.py#L75-L87)：默认用 `test_pipeline`（不含随机增强，帧与帧可比），但训练管线里的 `LoadAnnotations3D` 按原位置插回（L79-L81）、训练版 `Pack3DDetInputs`（带 `gt_bboxes_3d` 等键）替换末尾（L83-L87）——**没有这一步，画出来的只有点云没有框**。想看增强效果就加 `--aug` 直接用 `train_pipeline`。

**（2）构建与遍历**：[tools/misc/browse_dataset.py:L106-L148](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/browse_dataset.py#L106-L148)

- [L108-L113](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/browse_dataset.py#L108-L113)：`DATASETS.build(..., default_args=dict(filter_empty_gt=False))`——浏览时要看**所有**帧，不能像训练那样丢掉空标注帧；分割数据集没有该参数，靠 `except TypeError` 兜底重建。
- [L118-L119](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/browse_dataset.py#L118-L119)：构建配置里的 visualizer 并注入 `dataset_meta`——这正是 u7-l3 讲过的「框颜色按 labels_3d 查 METAINFO palette」机制的数据来源。
- [L123-L146](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/browse_dataset.py#L123-L146)：逐条 `add_datasample`，`--task` 五选一（[L27-L34](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/browse_dataset.py#L27-L34)）决定走 u7-l3 里的哪条可视化通道；`data_sample.numpy()`（L126）把 Det3DDataSample 里的张量转成 numpy 便于绘图；`--output-dir` 时每帧存成 `{i}.jpg`（无显示器的服务器用），LiDAR/多模态任务还另存 `pc_{i}.png` 的 Open3D 视图（L132-L136）。

#### 4.4.4 代码实践

1. **实践目标**：亲眼确认 KITTI 样本经过管线后的样子（GT 框 + 点云），并对比 `--aug` 前后差异。
2. **操作步骤**（前置：u3-l2 已生成 KITTI info pkl）：

   ```bash
   # 逐帧弹窗浏览（每 2 秒下一帧），Ctrl+C 退出
   python tools/misc/browse_dataset.py \
       configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py \
       --task lidar_det

   # 无显示器：落盘前 3769 张 val 样本（可 Ctrl+C 提前停）
   python tools/misc/browse_dataset.py \
       configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py \
       --task lidar_det --not-show --output-dir browse_out
   ```

   进阶：把配置换成 `configs/votenet/` 下的 ScanNet 配置、`--task lidar_det` 浏览室内数据；或加 `--aug` 对比同一帧的增强效果（GT 框应与点云同步旋转/翻转——u3-l6 的刚体一致性肉眼可验）。
3. **需要观察的现象**：BEV/3D 视图中每个目标有彩色框（颜色=类别）；`--aug` 下框始终「贴」着点云目标；进度条逐条推进。
4. **预期结果**：`browse_out/` 下出现 `0.jpg`、`1.jpg`…（以及 `pc_0.png` 等 Open3D 视图）；框-点错位、点云形状异常（如 use_dim 配错导致的奇怪通道）都会立刻显形。弹窗模式需要本地桌面环境，**待本地验证**。

#### 4.4.5 小练习与答案

1. **问**：为什么脚本要给 dataset 传 `filter_empty_gt=False`？为什么用 try/except 包住？
   **答**：训练时数据集会过滤无标注帧以加速收敛，但浏览数据的目的就是检查全部帧，空帧也要能看到（[L108-L111](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/browse_dataset.py#L108-L111)）；`Seg3DDataset` 等没有这个构造参数，`TypeError` 时退回普通 build（[L112-L113](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/browse_dataset.py#L112-L113)）。
2. **问**：默认（不带 `--aug`）时用的是 `test_pipeline`，为什么还要从 `train_pipeline` 里「借」两个 transform？
   **答**：`test_pipeline` 只打包 `points`（见 [configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py:L54-L77](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py#L54-L77)），没有 GT 就没法检查标注；脚本借回 `LoadAnnotations3D` 与带 gt 键的 `Pack3DDetInputs`（[L79-L87](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/browse_dataset.py#L79-L87)）。
3. **问**：`--task` 有哪五个取值？它最终传给了谁？
   **答**：`mono_det / multi-view_det / lidar_det / lidar_seg / multi-modality_det`（[L27-L34](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/browse_dataset.py#L27-L34)），透传给 `visualizer.add_datasample` 的 `vis_task` 参数，决定 u7-l3 中 `Det3DLocalVisualizer` 的五条分流通道。

### 4.5 模块五：辅助工具导览——print_config、fuse_conv_bn、publish_model 与一个「陷阱」

#### 4.5.1 概念说明

剩下四个小工具各管一段「实验收尾工作」：`print_config` 展开配置（调参前先看全貌）、`fuse_conv_bn` 做推理加速、`publish_model` 给要公开的 checkpoint 瘦身加哈希、`visualize_results` 名义上可视化测试输出——但最后这个是面向旧版配置的遗留脚本，需要特别辨析。

#### 4.5.2 核心流程

- **fuse_conv_bn 的数学**：BN 推理式为 \(y = \gamma\frac{x-\mu}{\sqrt{\sigma^2+\epsilon}} + \beta\)，代入卷积 \(x = W*a+b\) 后可合并成一个新卷积：

  \[
  W' = W \cdot \frac{\gamma}{\sqrt{\sigma^2+\epsilon}}, \qquad
  b' = (b-\mu)\cdot\frac{\gamma}{\sqrt{\sigma^2+\epsilon}} + \beta
  \]

  融合后前向少算一层，数值结果不变。
- **publish_model 的三步**：加载时 `map_location='cpu'` → 删除 `optimizer` 状态 → `sha256sum` 取前 8 位追加到文件名。

#### 4.5.3 源码精读

**（1）print_config.py**：[tools/misc/print_config.py:L17-L23](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/print_config.py#L17-L23)——`Config.fromfile` 后打印 `cfg.pretty_text`，即 u1-l5 中「把 `_base_` 继承链全部展开」的官方工具，还支持 `--options` 临时覆盖后再打印。

**（2）fuse_conv_bn.py**：逐层融合的核心在 [tools/misc/fuse_conv_bn.py:L27-L45](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/fuse_conv_bn.py#L27-L45)——递归遍历子模块，见到紧跟 Conv2d 的 BatchNorm 就地折叠，并把 BN 替换成 `nn.Identity()`（不删模块、只换身份，避免破坏命名）。单个融合的代数实现见 [L11-L24](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/fuse_conv_bn.py#L11-L24)，对应 4.5.2 的两条公式。命令行版入口 [L58-L64](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/fuse_conv_bn.py#L58-L64) 用 `init_model` 建模载权重、融合后 `save_checkpoint` 落盘。

**（3）publish_model.py**：[tools/model_converters/publish_model.py:L17-L27](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/model_converters/publish_model.py#L17-L27)——`torch.load(..., map_location='cpu')`（权重搬到 CPU）、`del checkpoint['optimizer']`（显著减小体积）、`sha256sum` 前 8 位十六进制拼进文件名（校验用），与官方文档描述的三件事一一对应（[docs/en/user_guides/useful_tools.md:L167-L172](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/useful_tools.md#L167-L172)）。

**（4）visualize_results.py——一个值得辨析的遗留脚本**：[tools/misc/visualize_results.py:L29-L33](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/visualize_results.py#L29-L33) 直接访问 `cfg.data.test.test_mode` 并 `DATASETS.build(cfg.data.test)`——`data.test` 是 **0.x 时代的配置结构**；而 1.x 配置（如 [configs/_base_/datasets/kitti-3d-3class.py:L141-L157](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L141-L157)）用的是 `test_dataloader.dataset`，顶层没有 `data` 键，MMEngine 的 `Config` 对缺失属性会抛 `AttributeError`。因此对当前 1.x 配置该脚本大概率直接报错；**1.x 的正确姿势**是 u7-l3 讲过的 `tools/test.py --show-dir`（由 `Det3DVisualizationHook` 驱动）。把它留在讲义里，正是想示范「读工具源码识破过期文档/脚本」的方法。

#### 4.5.4 代码实践

1. **实践目标**：走完「训练产物 → 可发布 checkpoint」的小闭环。
2. **操作步骤**（假设 u7-l1 的最小训练已在 `work_dirs/pointpillars_.../` 产出 `latest.pth`）：

   ```bash
   # 1) 看全量展开配置（复习 u1-l5）
   python tools/misc/print_config.py \
       configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py | less

   # 2) 融合 Conv+BN，产出推理加速版权重
   python tools/misc/fuse_conv_bn.py \
       configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py \
       work_dirs/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class/latest.pth \
       latest_fused.pth

   # 3) （可选）用 4.3 的 benchmark.py 分别测原权重与 fused 权重的 fps 对比

   # 4) 发布瘦身：去 optimizer、转 CPU、文件名追加 sha 前 8 位
   python tools/model_converters/publish_model.py latest_fused.pth pp_publish.pth
   ```

3. **需要观察的现象**：第 2 步后用 `python -c "import torch; sd=torch.load('latest_fused.pth',map_location='cpu')"` 应看到权重仍在、模型中 BN 已被折叠；第 4 步产物文件名变成 `pp_publish-<8位哈希>.pth`；对比融合前后两个 `.pth` 的文件大小（删除 optimizer 后明显变小）。
4. **预期结果**：`pp_publish-xxxxxxxx.pth` 生成且体积小于训练 checkpoint；融合权重 benchmark fps 不低于原权重——具体数值**待本地验证**。

#### 4.5.5 小练习与答案

1. **问**：`visualize_results.py` 为什么对 1.x 配置会失败？失败点在哪一行？
   **答**：它在 [tools/misc/visualize_results.py:L29-L30](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/visualize_results.py#L29-L30) 访问 `cfg.data.test`，这是 0.x 配置结构；1.x 配置顶层只有 `test_dataloader` 等键，`Config` 对不存在的属性抛 `AttributeError`。1.x 应改用 `tools/test.py --show-dir`。
2. **问**：`fuse_module` 为什么把 BN 换成 `nn.Identity()` 而不是直接删除子模块？
   **答**：保持模块命名与状态字典结构基本不变（[tools/misc/fuse_conv_bn.py:L36-L38](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/fuse_conv_bn.py#L36-L38) 注释写明「To reduce changes」），减少对checkpoint 兼容性与代码逻辑的影响。
3. **问**：`publish_model.py` 输出的文件名里的 8 位十六进制是什么、有什么用？
   **答**：checkpoint 文件的 sha256 前 8 位（[tools/model_converters/publish_model.py:L25-L26](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/model_converters/publish_model.py#L25-L26)），用于发布后校验下载文件是否完整无误——model zoo 里带哈希后缀的权重名就是这么来的。

## 5. 综合实践

**任务：给 PointPillars 建一张「复杂度—速度—质量」三联体检报告。**

1. **数据与产物**：KITTI info pkl（u3-l2）、官方 3class checkpoint 与官方训练日志（下载链接均在 [configs/pointpillars/README.md:L26](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/pointpillars/README.md#L26)）。
2. **第一步（复杂度）**：跑 4.2.4 的 `get_flops.py`，记录 FLOPs/Params；把 `--shape` 减半再跑一次，验证「FLOPs 随输入变、Params 不变」。
3. **第二步（速度）**：跑 4.3.4 的 `benchmark.py` 得基线 fps；再用 `fuse_conv_bn.py` 产出融合权重，重跑 `benchmark.py --fuse-conv-bn` 对比加速比。
4. **第三步（质量）**：用 4.1.4 的 `analyze_logs.py` 从官方日志画 `loss` 曲线与 `Car_3D_moderate_strict_R40`（或日志中实际存在的 mAP 键）曲线，标注出最慢 epoch（`cal_train_time` 的输出）。
5. **交付**：一张 markdown 表格（模型 / FLOPs / Params / fps / fused-fps / mAP / 平均 s/iter），外加三张 png 曲线。这张表就是论文或技术评审里「模型对比」页的雏形。

## 6. 本讲小结

- `analyze_logs.py` 解析 JSON-lines 日志：带 `/` 的键是验证指标、`--eval` 切 epoch 轴；`cal_train_time` 默认剔除每 epoch 首 iter 的离群耗时；文档示例参数（`--mode/--interval`）与代码（`--eval/--eval-interval`）已漂移，以 `--help` 为准。
- `get_flops.py` = `MODELS.build` + mmcv 哑输入计数器：Params 与输入无关、FLOPs 随输入变；哑输入绕过 `data_preprocessor`，而 VoxelNet 的 `extract_feat` 需要 `voxels` 字典，因此对体素类模型属实验性工具，只宜粗略对比。
- `benchmark.py` 走真实 `test_dataloader` + `model.test_step`，用 `synchronize` 前后夹逼计时并跳过 5 个 warmup 样本；源码里藏着两个可发现的小瑕疵（`--samples/--log-interval` 缺 `type=int`、最后一个样本耗时被重复累加）。
- `browse_dataset.py` 默认用 test_pipeline 但借回 `LoadAnnotations3D` 与带 gt 的 `Pack3DDetInputs`，并以 `filter_empty_gt=False` 看全量帧，是检查「管线输出」的眼睛；`--aug` 可验证点框刚体一致性。
- 收尾工具链：`print_config` 展开配置、`fuse_conv_bn` 按 \(W'=W\gamma/\sqrt{\sigma^2+\epsilon}\) 折叠卷积、`publish_model` 去 optimizer 加哈希；`visualize_results.py` 是 0.x 遗留（依赖 `cfg.data.test`），1.x 请用 `test.py --show-dir`。

## 7. 下一步学习建议

- 下一讲 u8-l1 进入 3D 语义分割：`Seg3DDataset` 与 Cylinder3D/MinkUNet，你会再次用到本讲的 `browse_dataset.py --task lidar_seg`。
- 建议精读的配套源码：`mmcv` 的 `get_model_complexity_info`（理解 hook 计数的白名单）；`mmdet3d/utils/`（`replace_ceph_backend` 等工具脚本的组织方式）。
- 若你在 4.3.3/4.5 里发现了那几个脚本瑕疵，不妨对照 `docs/en/notes/contribution_guides.md`（u8-l5 会讲）试着提一个补 `type=int` 的小 PR——这是参与开源项目最平滑的入口。
