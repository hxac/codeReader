# u6-l4 接地可视化：导出彩色 PLY 点云

## 1. 本讲目标

学完本讲，你应该能够：

1. 打开 mask dump 的两个开关（环境变量 `MASK_DUMP_DIR` 与配置项 `model.decoder.save_viz_dir`），说清两者的优先级与各自适用的场景。
2. 用 `tools/export_grounding_ply.py` 把 dump 出来的逐点掩码染成彩色 PLY 点云，并区分 `combine` 与 `per_qa` 两种输出模式、掌握 `--group` 分组染色技巧。
3. 精确解释「每个 `<SEG>` → 一种颜色」的映射规则：区域编号如何产生、调色板如何循环、退化掩码如何被过滤、重叠区域先到先得。
4. 读懂 `uniseg3d/grounding_viz.py` 的写入细节：行并集、`max_frac` 退化过滤、背景调暗、二进制 PLY 的顶点结构。

本讲是单元 6 的收官：u6-l1 讲过 `Part3DGlamm_SegMetric` 只保留每场景的 IoU 数字、把掩码本身丢掉；u6-l2 讲过 `eval_ground3d.sh` 有一个 `--mask-dump` 开关。本讲就把这条「被指标丢掉的原始掩码」通道完整接通，最终产出一眼能看懂的彩色点云。

## 2. 前置知识

### 2.1 为什么需要一条独立的可视化通道

mIoU 是一个数字：它告诉你「平均接地对了多少」，但回答不了「错在哪」。是整个物体都错、还是只漏了一个扶手？是把相邻两把椅子并成了一个区域、还是把地板也接了进来？这些都必须**看图**。而推理主链路（u3-l1 的 `predict()`）在指标消费完 `pred_pts_seg` 后就把掩码留在内存里等待回收，因此项目在 `predict()` 出口处加了一段可选的落盘逻辑——mask dump。

### 2.2 掩码从哪来：回顾上采样与对齐

在 u3-l1 与 u6-l1 已经讲过，逐 QA 的预测掩码是超点级 logits 经 `sigmoid → > sp_score_thr(0.4) → mask[:, superpoints]` 上采样成的**点级布尔矩阵**，形状为 `(行数, N_points)`，行数是该条回答里 `<SEG>` 指称的个数（与 GT 对齐时不足补零行、超出截断）。本讲消费的就是这些点级矩阵。

### 2.3 两个文件格式

- **object 数组 `.npy`**：不同 QA 的行数不同（0/1/2 行）、不同场景的点数也不同，无法堆成规则张量，所以 dump 用 `dtype=object` 的 NumPy 数组（内部靠 pickle）存「变长列表的列表」。读回时必须 `np.load(..., allow_pickle=True)`。
- **PLY**：一种极简的通用点云/网格格式。本讲写的是**二进制** PLY（`text=False`），每个顶点 6 个字段：`x/y/z`（float32 坐标）+ `red/green/blue`（uint8 颜色）。用 MeshLab、CloudComparison 或 Open3D 都能直接打开。项目依赖里已经装了 `plyfile` 读写库（见 [docs/INSTALL.md:L74](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/INSTALL.md#L74)），不需要再引 Open3D。

### 2.4 一条关键提醒（承接 u4-l4/u4-l5）

推理时若某条 QA 的回答里没有 `<p>...</p>` 指称，`get_query_tokens_eval` 会退回一个**零向量查询**（[uniseg3d/query_decoder.py:L2423-L2425](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2423-L2425)），解码器把它的掩码 logits 置成 `1e-4` 哨兵（[uniseg3d/query_decoder.py:L1932](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1932)）。注意 `sigmoid(1e-4) ≈ 0.50002 > 0.4`，过阈值后这个哨兵会变成**整场景全 True** 的掩码。本讲的 `max_frac` 退化过滤正是为了把它（以及「把整个房间都接进来」的失败样本）挡在可视化之外——这是理解 4.3 节的钥匙。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [uniseg3d/uniseg3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1310-L1325) | `Grounded_LMM_main.predict()` 末尾的 mask dump 开关与写盘逻辑（本讲数据源头） |
| [uniseg3d/query_decoder.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1997-L2011) | `Grounded_Decoder_Eval.__init__` 里的 `save_viz_dir` 配置入口 |
| [tools/export_grounding_ply.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/export_grounding_ply.py) | 离线命令行工具：dump 目录 + 点云 bin → 彩色 PLY（combine/per_qa 两种模式） |
| [uniseg3d/grounding_viz.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/grounding_viz.py) | 核心写入器：掩码 → 逐点标签 → 调色板染色 → 二进制 PLY |
| [eval_ground3d.sh](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L136-L142) | `--mask-dump` 参数：把 dump 目录镜像成与预测输出同构的目录树 |
| [README.md](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L347-L366) | 官方两步可视化说明 |
| [configs/ground3dlmm_eval_ground3d_scannet.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L92-L110) | 评估配置：`sp_score_thr=0.4` 阈值与 `data_prefix` 的 `points` 目录（导出工具的 `--points_dir`） |

## 4. 核心概念与源码讲解

### 4.1 mask dump 开关：MASK_DUMP_DIR 与 save_viz_dir

#### 4.1.1 概念说明

mask dump 是 `predict()` 出口处的一段**旁路**逻辑：不影响推理结果、不参与指标计算，只在开关打开时把每个场景的逐 QA 预测/GT 掩码写成两个 `.npy`。它有两个等价的开关：

1. **环境变量 `MASK_DUMP_DIR`**：进程级开关，改环境不改配置，适合临时 dump 一次。
2. **配置项 `model.decoder.save_viz_dir`**：写在解码器 `__init__` 里的参数，经 `--cfg-options` 注入，适合固化在脚本里。

两者任一非空即开启，环境变量优先（见 4.1.3 的 `or` 短路）。

#### 4.1.2 核心流程

```text
predict() 正常跑完，results_list[i] = PointData(qa_pred_masks, qa_gt_masks)
  ↓
读开关：dump_dir = os.environ.get('MASK_DUMP_DIR') or self.decoder.save_viz_dir
  ↓ 都为 None → 直接返回，零开销
dump_dir 非空 → makedirs(dump_dir, exist_ok=True)
  ↓ 逐场景 i：
    {dump_dir}/{scene_name}_qa_pred_masks.npy   ← object 数组，元素是 (行数, N) 的 bool ndarray
    {dump_dir}/{scene_name}_qa_gt_masks.npy     ← object 数组，元素是同形状的 GT（torch → numpy）
```

文件名里的 `scene_name` 来自 `batch_inputs_dict['scene_name']`，也就是 u2-l3 讲过的「场景_frame_序号」帧 ID——它与 `data/scannet/points/` 下的 `<scene>.bin` 同名，这正是导出工具能把掩码与点云重新配对的**纯字符串契约**。

#### 4.1.3 源码精读

**① 掩码的最终形态（dump 的内容是什么）**：

- [uniseg3d/uniseg3d.py:L1218-L1222](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1218-L1222)：`qa_predict_by_feat` 里，末层掩码 logits 先 `sigmoid`、再与 `test_cfg.sp_score_thr`（配置中为 0.4，[configs/ground3dlmm_eval_ground3d_scannet.py:L98](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L98)）比较得超点级布尔，最后 `mask[:, superpoints]` 列索引广播成点级。
- [uniseg3d/uniseg3d.py:L1223-L1235](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1223-L1235)：与 GT 行对齐（有 GT 才对齐：GT 行多则预测补零行、行少则截断），随后 `.cpu().numpy()` 落成 ndarray。注释明确说：**没有 GT 的预测行也原样保留，正是为了可视化**。

**② 开关与写盘**：

- [uniseg3d/uniseg3d.py:L1310-L1313](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1310-L1313)：注释写明两种开关；`_os.environ.get('MASK_DUMP_DIR') or getattr(self.decoder, 'save_viz_dir', None)`——`or` 短路保证**环境变量优先**；`getattr` 兜底是防止 decoder 没有该属性（如训练版解码器）时报错。
- [uniseg3d/uniseg3d.py:L1314-L1325](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1314-L1325)：`makedirs(..., exist_ok=True)` 幂等建目录；预测侧直接 `np.array(_pred, dtype=object)`（本来就是 ndarray 列表）；GT 侧是 torch 张量列表，用 `m.cpu().numpy() if hasattr(m, 'cpu') else ...` 逐个搬运。两个文件都用 `allow_pickle=True`。

**③ 配置入口 `save_viz_dir`**：

- [uniseg3d/query_decoder.py:L1997-L2011](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1997-L2011)：`Grounded_Decoder_Eval.__init__` 声明 `save_viz_dir=None` 形参并保存到 `self.save_viz_dir`；注释直说这是给 `tools/export_grounding_ply.py` 喂数据的开关。注意它在 `__init__` 阶段就 `makedirs`（构造即建目录），而 `MASK_DUMP_DIR` 是在 `predict()` 里每次调用时建——前者的目录在配置解析成功那一刻就存在。

**④ 批量评估时的目录镜像（承接 u6-l2）**：

- [eval_ground3d.sh:L46](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L46)：`--mask-dump <dir>` 参数。
- [eval_ground3d.sh:L136-L142](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L136-L142)：关键一行 `dump_env="MASK_DUMP_DIR=$MASK_DUMP/${save_dir#"$OUT_ROOT/"}`——把预测输出目录树的相对路径**原样镜像**到 dump 根目录下。原因：dump 文件名只有 `{scene}_qa_pred_masks.npy`，而同一个场景会出现在多个子任务的评估运行里（场景级数据集、QA 在 transform 里按子任务过滤，见 u6-l2），若全部写进同一个平铺目录，后跑的子任务会**静默覆盖**先跑的结果。镜像目录树后每个子任务各写各的分支。u6-l3 的 GM-δ 同样靠这棵镜像树把文本预测与掩码一一配对。

#### 4.1.4 代码实践：开关优先级与产物观察（零 GPU）

1. **实践目标**：不跑模型，只验证「开关的读取规则」与「dump 文件名契约」，为后续真跑推理扫清认知障碍。
2. **操作步骤**：

   **第 1 步**，通读官方说明与 dump 段源码（各 1 分钟）：

   ```bash
   sed -n '347,366p' README.md          # 官方两步可视化说明
   sed -n '1310,1326p' uniseg3d/uniseg3d.py   # dump 开关与写盘
   ```

   **第 2 步**，干跑配置验证 `save_viz_dir` 能被 `--cfg-options` 注入（不需要 GPU、不下模型）：

   ```bash
   PYTHONPATH=. python -c "
   from mmengine.config import Config
   cfg = Config.fromfile('configs/ground3dlmm_eval_ground3d_scannet.py')
   cfg.merge_from_dict({'model': {'decoder': {'save_viz_dir': 'viz_dumps'}}})
   print('save_viz_dir =', cfg.model.decoder.save_viz_dir)
   print('sp_score_thr =', cfg.model.test_cfg.sp_score_thr)
   print('pts prefix   =', cfg.data_prefix.pts)
   "
   ```

   预期打印 `save_viz_dir = viz_dumps`、`sp_score_thr = 0.4`、`pts prefix = points`——即最后一个正是导出工具 `--points_dir data/scannet/points` 的来源。

3. **需要观察的现象**：`merge_from_dict` 按点路径落位成功；`data_prefix.pts='points'` 与 [configs/ground3dlmm_eval_ground3d_scannet.py:L106-L110](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L106-L110) 一致。
4. **预期结果**：确认配置通道可用、文件名契约（`{scene}` ↔ `points/<scene>.bin`）成立。
5. 以上命令我未在本机执行，输出为源码推演，**待本地验证**。真跑推理的完整命令（在 u1-l4 环境就绪后）为：

   ```bash
   PYTHONPATH=. python tools/test.py configs/ground3dlmm_eval_ground3d_scannet.py "$CKPT" \
       --cfg-options model.decoder.save_viz_dir=viz_dumps \
           test_dataloader.dataset.pipeline.2.transforms.2.task_level=object_qa_data \
           test_dataloader.dataset.pipeline.2.transforms.2.task_type=qa_data \
           test_dataloader.dataset.pipeline.2.transforms.2.sub_task=functional_object_grounding \
       test_dataloader.batch_size=1
   ```

   或沿用 u6-l2 的批量入口并打开镜像目录树：`bash eval_ground3d.sh --mask-dump viz_dumps ...`（**待本地验证**，需 checkpoint 与数据）。

#### 4.1.5 小练习与答案

**练习 1**：同时设置 `MASK_DUMP_DIR=a` 和 `model.decoder.save_viz_dir=b`，掩码写到哪？

**答案**：写到 `a`。[uniseg3d/uniseg3d.py:L1313](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1313) 的 `_os.environ.get('MASK_DUMP_DIR') or getattr(...)` 中 `or` 短路：环境变量非空即取环境变量，配置值根本不会被读取。这也解释了 `eval_ground3d.sh` 为什么敢放心地用 `MASK_DUMP_DIR=...` 前缀注入——它能压过任何配置里的 `save_viz_dir`。

**练习 2**：为什么 dump 必须用 `dtype=object` 的 npy，而不是普通 ndarray？

**答案**：普通 ndarray 要求规则形状，但每条 QA 的行数随回答中 `<p>` 标签个数变化（0/1/2 行，且与 GT 对齐时可能补零行），不同场景点数也不同，无法堆叠。object 数组按 pickle 存「变长列表」，读回需 `allow_pickle=True`。代价是文件不可跨 Python 环境随意移植（pickle），但换来了零拷贝的灵活性。

**练习 3**：用裸 `MASK_DUMP_DIR=flat_dir bash eval_ground3d.sh --levels part ...` 连跑 8 个子任务，会发生什么？

**答案**：8 次运行都写进同一个平铺目录。同一个场景会参与多个子任务的评估（QA 在 `QA_Generation_Test` 里按 `sub_task` 过滤，见 u6-l2），而 dump 文件名只含场景名，所以后跑的子任务会**静默覆盖**先跑的同名文件，最后目录里只剩最后一批。正确做法是 `--mask-dump viz_dumps`，让 [eval_ground3d.sh:L137](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L137) 按预测目录树镜像出 `viz_dumps/<dataset>/pred_qa_data_val/part_qa_data/qa_data/<sub_task>/...`。

### 4.2 export_grounding_ply 的 combine/per_qa 模式

#### 4.2.1 概念说明

`tools/export_grounding_ply.py` 是一个**纯离线**工具：不加载模型、不需要 GPU、不 import torch/MinkowskiEngine，只要 numpy + plyfile 就能跑。它做三件事：

1. 从 `--masks_dir` 收集所有 `*_qa_pred_masks.npy`（注意 glob 模式只匹配**预测**文件，GT dump 不在此列）；
2. 从 `--points_dir` 按 `{scene}.bin` 读回 N×6 的 XYZ+RGB 点云（即 `create_data.py` 产出的 `points/` 目录，见 u2-l2）;
3. 按两种模式把掩码染成 PLY 写到 `--out`：
   - **`combine`（默认）**：一个场景一个 `.ply`，场景内第 i 个接地区域染调色板第 i 色（红、绿、蓝、橙、紫…）。
   - **`per_qa`**：一条 QA 一个 `.ply`（放在 `<out>/<scene>/qa00.ply` 子目录里），唯一区域恒为红色。

两个调节旋钮：`--group N`（combine 下每 N 个连续掩码分进一个 ply，颜色在每个 ply 内重新从红开始）与 `--max_frac 0.85`（覆盖率超过该比例的掩码视为退化接地而跳过，透传给写入器）。

#### 4.2.2 核心流程

```text
sorted(glob(masks_dir/*_qa_pred_masks.npy))     # 逐 dump 文件
  scene = 文件名去掉 _qa_pred_masks.npy 后缀
  xyz, rgb = 读 points_dir/{scene}.bin           # bin 缺失 → 打印 skip、继续下一场景
  qa_masks = list(np.load(pf, allow_pickle=True)) # 每元素 = 一条 QA 的 (行数, N) bool 矩阵
  ├─ mode == per_qa:  每个 m → <out>/<scene>/qa{i:02d}.ply（单掩码 → 红）
  │                    仅当 labels.max() > 0 才计入 n_ply
  └─ mode == combine: group<=0 → 全部掩码一个 ply：{scene}.ply
                       group=N  → 每 N 个连续掩码一个 ply：{scene}_grp{g:02d}.ply
                       每个 ply 打印 "{scene}: {n} colored region(s) -> 路径"
打印 Done. Wrote {n_ply} .ply file(s)
```

`--group` 的典型用法：多轮对话（`task_level=multi_turn_qa_data`）时**每一轮各占一个 dump 条目**（u4-l5 讲过推理路径逐 QA/逐轮独立跑解码器），跨物体对话「第一轮指 A、第二轮指 B」对应的两个条目用 `--group 2` 分进同一个 ply，就得到 README 里那张「一红一绿」的对话图。

#### 4.2.3 源码精读

**① 离线加载技巧**：

- [tools/export_grounding_ply.py:L38-L45](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/export_grounding_ply.py#L38-L45)：用 `importlib.util.spec_from_file_location` **按文件路径**直接加载 `uniseg3d/grounding_viz.py`，绕过 `import uniseg3d` 包本身。原因写在注释里：包的 `__init__` 会连带拉起 torch/MinkowskiEngine，那样「离线工具」就名存实亡。这是插件式仓库里「想复用一个模块又不想背整个包」的惯用手法。

**② 点云读回**：

- [tools/export_grounding_ply.py:L48-L53](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/export_grounding_ply.py#L48-L53)：`np.fromfile(bin, float32).reshape(-1, 6)`——与 `LoadPointsFromFile` 的 `load_dim=6` 读的是同一个文件；`pc[:, :3]` 为坐标，`pc[:, 3:6].clip(0, 255).astype(uint8)` 为颜色。注意 bin 里存的是**原始 0–255 RGB**（`NormalizePointsColor_` 归一化只发生在管线内存中，不落盘，见 u2-l3），所以这里直接 clip 成 uint8 即可。

**③ 命令行参数**：

- [tools/export_grounding_ply.py:L59-L72](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/export_grounding_ply.py#L59-L72)：三个必填（`--masks_dir/--points_dir/--out`）加三个可选——`--mode`（L64-L66）、`--max_frac` 默认 0.85（L67-L68）、`--group` 默认 0 即不分組（L69-L71，help 里给了 `2 → 红绿配对` 的例子）。

**④ 主循环与两种模式**：

- [tools/export_grounding_ply.py:L74-L77](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/export_grounding_ply.py#L74-L77)：glob 结果排序后逐文件处理；一个都找不到直接报错退出并提示先跑 `MASK_DUMP_DIR` 推理。
- [tools/export_grounding_ply.py:L80-L88](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/export_grounding_ply.py#L80-L88)：剥后缀得 `scene`；读 bin（缺失则 `skip` 后 `continue`，批量导出不会因个别场景断掉）；`np.load(..., allow_pickle=True)` 还原逐 QA 掩码列表；空列表跳过。
- [tools/export_grounding_ply.py:L90-L95](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/export_grounding_ply.py#L90-L95)：**per_qa** 模式——每条掩码单独包成 `[m]` 列表写入 `<out>/<scene>/qa{qi:02d}.ply`；`lab.max() > 0` 才计数（被退化过滤掉的全背景 ply 仍会写盘，但不计入末尾统计——一个容易观察到的细节差异）。
- [tools/export_grounding_ply.py:L96-L104](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/export_grounding_ply.py#L96-L104)：**combine** 模式——`group<=0` 时整个 `qa_masks` 列表进一个 ply；否则切片 `[i:i+group]`。每组独立调用 `write_grounding_ply`，所以**颜色编号在每个 ply 内从 1 重新开始**（这正是「组内红绿、跨组重新红绿」的实现原理）。每 ply 打印实际染出的区域数。

**⑤ GT 文件不在消费之列**：

- glob 模式 `'*_qa_pred_masks.npy'`（[tools/export_grounding_ply.py:L74](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/export_grounding_ply.py#L74)）只匹配预测文件；`{scene}_qa_gt_masks.npy` 与预测文件格式完全同构（见 4.1.3 ②），想可视化 GT 只需把它复制/改名成 `_qa_pred_masks.npy` 后缀（综合实践里会用这招做对比图）。

#### 4.2.4 代码实践：三种模式各跑一遍（离线可跑）

1. **实践目标**：用一个手造的迷你场景，一次看清 `combine`、`combine --group`、`per_qa` 三种输出差异，以及退化掩码被过滤后的表现。

2. **操作步骤**：

   **第 1 步**，造数据（示例代码，保存为任意路径如 `make_viz_fixture.py` 运行；只需 numpy）：

   ```python
   # 示例代码：生成 200 点的假场景 + 一个含 3 条 QA 的 dump
   import os, numpy as np
   os.makedirs('viz_practice/points', exist_ok=True)
   os.makedirs('viz_practice/dumps', exist_ok=True)

   N = 200
   xyz = np.random.RandomState(0).rand(N, 3).astype(np.float32) * 2.0   # 米制坐标
   rgb = np.tile(np.linspace(150, 230, N, dtype=np.float32)[:, None], (1, 3))  # 灰底色
   xyzrgb = np.concatenate([xyz, rgb], axis=1).astype(np.float32)
   xyzrgb.tofile('viz_practice/points/scene0000_00_frame_0.bin')   # N×6 float32 裸 bin

   m0 = np.zeros((1, N), bool); m0[0, 0:40]    = True   # QA0：单 <SEG>，40 点
   m1 = np.zeros((2, N), bool); m1[0, 40:80]   = True; m1[1, 80:120] = True  # QA1：双 <SEG> 两行
   m2 = np.ones((1, N), bool)                              # QA2：全 True（模拟 1e-4 哨兵/失败接地）
   np.save('viz_practice/dumps/scene0000_00_frame_0_qa_pred_masks.npy',
           np.array([m0, m1, m2], dtype=object), allow_pickle=True)
   print('fixture ready')
   ```

   **第 2 步**，三种模式各跑一次：

   ```bash
   python tools/export_grounding_ply.py --masks_dir viz_practice/dumps \
       --points_dir viz_practice/points --out viz_practice/ply_combine
   python tools/export_grounding_ply.py --masks_dir viz_practice/dumps \
       --points_dir viz_practice/points --out viz_practice/ply_group --group 1
   python tools/export_grounding_ply.py --masks_dir viz_practice/dumps \
       --points_dir viz_practice/points --out viz_practice/ply_perqa --mode per_qa
   ```

   **第 3 步**，用 plyfile 检查产物（示例代码）：

   ```python
   # 示例代码：读取并统计颜色分布
   import numpy as np
   from plyfile import PlyData
   v = PlyData.read('viz_practice/ply_combine/scene0000_00_frame_0.ply')['vertex'].data
   rgb = np.stack([v['red'], v['green'], v['blue']], 1).astype(int)
   print('红色点数  :', int(((rgb[:, 0] > 200) & (rgb[:, 1] < 100)).sum()))   # 期望 40（QA0）
   print('绿色点数  :', int(((rgb[:, 1] > 150) & (rgb[:, 0] < 100)).sum()))   # 期望 80（QA1 两行并集）
   print('背景点数  :', int((rgb.max(1) <= 230 * 0.55 + 1).sum()))           # 期望 80×0.55 调暗
   ```

3. **需要观察的现象**：
   - `ply_combine`：终端打印 `scene0000_00_frame_0: 2 colored region(s)`——QA2 全 True 覆盖率 1.0 > 0.85 被丢弃，只剩 QA0（红）与 QA1（绿）；
   - `ply_group`（group=1）：产出 `scene0000_00_frame_0_grp00.ply`（1 区红）、`grp01.ply`（1 区红——QA1 的两行被并集成**一个**区域）、`grp02.ply`（打印 `0 colored region(s)`，文件仍写出但全背景）；
   - `ply_perqa`：`<out>/<scene>/qa00.ply、qa01.ply` 有色，`qa02.ply` 全背景；末尾统计 `Wrote 2 .ply file(s)`（per_qa 只计 `lab.max()>0` 的文件）。
4. **预期结果**：三个目录的 ply 均可用 MeshLab/CloudCompare 打开；颜色计数符合第 3 步脚本注释里的期望值。
5. 本实践的输出均由源码推演（`masks_to_labels` 的并集与过滤逻辑见 4.3），我未实际执行，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：combine 模式下一个场景有 7 个有效区域，第 6、7 个区域分别是什么颜色？

**答案**：调色板只有 5 色，写入器按 `(i-1) % len(palette)` 取模循环（[uniseg3d/grounding_viz.py:L86](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/grounding_viz.py#L86)）。第 6 区域 `(6-1)%5=0` 又回到红，第 7 区域是绿。颜色只保证「同 ply 内前 5 个区域互不相同」，不保证全局唯一。

**练习 2**：4 条 QA 的 dump，`--group 0` 与 `--group 2` 各产出什么？

**答案**：`--group 0`：1 个 `{scene}.ply`，4 个区域依次红/绿/蓝/橙。`--group 2`：2 个 ply——`grp00` 含 QA0(红)+QA1(绿)，`grp01` 含 QA2(红)+QA3(绿)，颜色在组内**重新从红开始**，因为每组是独立调用 `write_grounding_ply`（[tools/export_grounding_ply.py:L96-L104](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/export_grounding_ply.py#L96-L104)）。多轮跨物体对话想要「一红一绿」就必须用 group（或多轮每轮一条目的天然顺序 + group=轮数）。

**练习 3**：`--points_dir` 给错目录会发生什么？

**答案**：不报错。`_load_points` 返回 `(None, None)`，主循环打印 `skip {scene}: {scene}.bin not in ...` 后 `continue`（[tools/export_grounding_ply.py:L82-L85](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/export_grounding_ply.py#L82-L85)），最后打印 `Wrote 0 .ply file(s)`。工具的容错是「跳过而非失败」，所以**看到 0 个产物时要先检查终端里的 skip 行**。

### 4.3 grounding_viz 写 PLY：标签化、调色板与退化过滤

#### 4.3.1 概念说明

`uniseg3d/grounding_viz.py` 是唯一真正「染颜色、写文件」的模块，导出工具只是它的批处理外壳。它把「掩码列表 → 带色点云」拆成三步：

1. **归一化**：每条掩码（可能是 `(行数, N)`、`(N,)` 或 object 嵌套）统一成长度为 N 的一维布尔向量，多行取**并集**；
2. **标签化**：按顺序给每条**被接受**的掩码分配递增编号，写入 `labels` 数组（0=背景，i=第 i 个区域），退化掩码（空或覆盖率超限）直接丢弃，重叠处**先到先得**；
3. **染色写盘**：背景 RGB 乘 `dim_bg=0.55` 调暗，区域 i 涂调色板色，写成二进制 PLY。

「每个 `<SEG>` → 一种颜色」的**精确表述**是：每个 **dump 条目**（一条 QA；多轮对话的每一轮各算一条）经行并集后对应**至多一个**区域；区域按其在 masks 列表中的顺序（跳过被丢弃者后）依次取调色板颜色。单条 QA 回答里写两个 `<p>...</p><SEG>` 时，两行会被**并集成同一个区域同一种颜色**——想分色只能靠拆条目（多轮）加 `--group`。这是初学最容易误解的一点，README 的「2 → red + green」指的是两条目（多轮两轮）的情形。

#### 4.3.2 核心流程

```text
write_grounding_ply(out_path, xyz, base_rgb, masks, max_frac=0.85, dim_bg=0.55)
  │
  ├─ labels = masks_to_labels(masks, N, max_frac)
  │     labels = zeros(N, int32)；next_id = 0
  │     for mask in masks:
  │        m = _to_bool_1d(mask, N)        # 行并集 any(0)；长度不匹配 → None
  │        frac = m.mean()                  # 覆盖率
  │        if frac == 0 or frac > max_frac: continue      # 退化过滤
  │        next_id += 1
  │        labels[m & (labels == 0)] = next_id            # 先到先得
  │
  ├─ rgb = (base_rgb * dim_bg).clip(0,255).astype(uint8)  # 背景调暗
  ├─ for i in 1..labels.max(): rgb[labels == i] = palette[(i-1) % 5]
  └─ 写二进制 PLY（vertex: x/y/z f4 + red/green/blue u1），返回 labels
```

覆盖率过滤的阈值语义：\( \text{frac} = \frac{1}{N}\sum_{j=1}^{N} m_j \)，接受条件是 \( 0 < \text{frac} \le \text{max\_frac} \)。默认 `max_frac=0.85` 意味着「模型把超过 85% 的场景点都接进来」被视为无信息量的失败接地（含 2.4 节的 1e-4 哨兵全 True 情形），不给颜色。

#### 4.3.3 源码精读

**① 固定调色板**：

- [uniseg3d/grounding_viz.py:L20-L26](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/grounding_viz.py#L20-L26)：`DEFAULT_PALETTE` 共 5 色——红 (220,30,30)、绿 (45,175,70)、蓝 (40,90,230)、橙 (230,160,30)、紫 (160,60,200)，饱和度都拉得很高，就是为了在调暗的背景上「跳出来」。

**② 归一化 `_to_bool_1d`**：

- [uniseg3d/grounding_viz.py:L28-L41](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/grounding_viz.py#L28-L41)：先拆 object 嵌套（L36-L37）；二维则 `m.any(0)` **沿行取并集**（L38-L39）——这就是「单 QA 多 `<SEG>` 并成一个区域」的发生地；最后展平并**校验长度**（L41），与 N 不匹配返回 None，调用方静默跳过。这个长度守卫默默吸收了「dump 掩码与 bin 点数不一致」的脏数据（正常情况下两者同源同长，评估管线不做点采样，`MultiScaleFlipAug3D` 的 `flip=False、pts_scale_ratio=1` 也不改几何）。

**③ 标签化 `masks_to_labels`**：

- [uniseg3d/grounding_viz.py:L44-L62](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/grounding_viz.py#L44-L62)：`labels` 全零起步（L51）；退化判据 `frac == 0.0 or frac > max_frac`（L57-L59）；`labels[m & (labels == 0)] = next_id`（L61）——掩码只染**尚未被占领**的点，因此两条 QA 接地重叠时**先出现的条目赢**，后者只在空白处补色；`next_id` 只对被接受的掩码递增，所以颜色编号连续、不会因跳过某个退化掩码而「跳色」。

**④ 染色与写盘 `write_grounding_ply`**：

- [uniseg3d/grounding_viz.py:L81-L86](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/grounding_viz.py#L81-L86)：先整幅 `base_rgb * dim_bg`（默认 0.55，L66 的形参）把背景压暗一半不到，再按标签盖上调色板色——顺序保证区域色不被调暗。被过滤掉的区域与背景同待遇，保持暗色。
- [uniseg3d/grounding_viz.py:L88-L94](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/grounding_viz.py#L88-L94)：用 NumPy 结构化数组直接描述 PLY 顶点（`x/y/z` 为 `'f4'`、`red/green/blue` 为 `'u1'`），`PlyData(..., text=False)` 写**二进制** PLY（比 ASCII 小且读写快）；`os.makedirs(dirname, exist_ok=True)` 让调用方无须预建输出目录；返回 `labels` 供上层统计区域数。

**⑤ 模块级设计**：

- 文件头（[uniseg3d/grounding_viz.py:L1-L14](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/grounding_viz.py#L1-L14)）明确本模块「import-light」：只依赖 `os/numpy/plyfile`，既可被离线工具按文件路径加载（4.2.3 ①），也可以在评估循环里直接调用。染色的语义（1 红、2 红绿、N 调色板）与 docstring 的图例说明一一对应。

#### 4.3.4 代码实践：读源码改参数，预测再验证

1. **实践目标**：通过修改 `--max_frac` 与 `dim_bg` 的行为预测，验证你确实理解了 `masks_to_labels` 的过滤与调暗逻辑，而不是只会跑命令。

2. **操作步骤**（沿用 4.2.4 的 `viz_practice` 数据）：

   **第 1 步**，先把 QA2 的掩码从「全 True」改造成「85% 覆盖」的边界样本（示例代码）：

   ```python
   # 示例代码：把 QA2 改成覆盖 170/200 = 0.85 的边界掩码
   import numpy as np
   p = 'viz_practice/dumps/scene0000_00_frame_0_qa_pred_masks.npy'
   m0, m1, _ = list(np.load(p, allow_pickle=True))
   m2 = np.zeros((1, 200), bool); m2[0, :170] = True      # frac = 0.85
   np.save(p, np.array([m0, m1, m2], dtype=object), allow_pickle=True)
   ```

   **第 2 步**，重跑 combine 并观察：

   ```bash
   python tools/export_grounding_ply.py --masks_dir viz_practice/dumps \
       --points_dir viz_practice/points --out viz_practice/ply_edge
   ```

   **第 3 步**，回答并验证：这次打印几个 colored region？（对照判据 `frac > max_frac` 是**严格大于**。）

3. **需要观察的现象**：`frac = 0.85` 不大于 0.85，因此 QA2 这次**被接受**，打印 `3 colored region(s)`，QA2 染蓝色（第三色）。再把它改成 171 点（frac=0.855）重跑，则回到 `2 colored region(s)`。
4. **预期结果**：两次运行的区域数随覆盖率跨过 0.85 边界而翻转，与 [uniseg3d/grounding_viz.py:L57-L59](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/grounding_viz.py#L57-L59) 的严格大于语义一致。
5. 以上为源码推演结果，**待本地验证**。若想同时观察 `dim_bg`，可在 Python 里直接调 `write_grounding_ply(..., dim_bg=0.2)` 对比背景明暗（该形参没有 CLI 开关，只能编程调用——见练习 3）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `max_frac` 过滤不可或缺？不加会发生什么？

**答案**：两类掩码会污染图面：其一，无 `<p>` 指称的 QA（如 existence_verification）经零向量查询 + `1e-4` 哨兵，`sigmoid(1e-4)≈0.5 > 0.4` 阈值后变成全 True 掩码；其二，真正的失败接地（模型「糊」地选中大半个房间）。不加过滤时这些掩码会覆盖整幅点云、把后续区域全部挤掉（先到先得），一张图全红。`masks_to_labels` 的 `frac==0 or frac>max_frac` 双判据（空掩码也一并挡住）就是图面质量的守门员，docstring 称之为 mirroring the figure pipeline（论文图管线）。

**练习 2**：一条 QA 的回答含两个 `<p>...<SEG>`，dump 条目形状 `(2, N)`。它在 combine ply 里占几个区域、什么颜色？想让两个目标分色怎么办？

**答案**：占**一个**区域——`_to_bool_1d` 对二维掩码 `m.any(0)` 行并集（[uniseg3d/grounding_viz.py:L38-L39](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/grounding_viz.py#L38-L39)），颜色按条目顺序分配。想分色：若两目标来自多轮对话的两轮（各占一个 dump 条目），用 `--group 2` 即得一红一绿；若是单轮双目标，现有写入器无能为力——需要改 `_to_bool_1d`/`masks_to_labels` 让行不并集、逐行分配标签（一个合理的二次开发小改动）。

**练习 3**：想把背景完全压黑（`dim_bg=0`）或者换一套配色，CLI 做得到吗？

**答案**：做不到。`export_grounding_ply.py` 的 argparse 只暴露 `--mode/--max_frac/--group`（[tools/export_grounding_ply.py:L59-L72](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/export_grounding_ply.py#L59-L72)），`dim_bg` 与 `palette` 是 `write_grounding_ply` 的关键字形参（[uniseg3d/grounding_viz.py:L65-L66](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/grounding_viz.py#L65-L66)），只能编程调用。得益于模块 import-light，四行脚本即可：`import grounding_viz`（或按 4.2.3 ① 的路径加载法）后自行读 bin、调 `write_grounding_ply(..., palette=[...], dim_bg=0.0)`。

## 5. 综合实践

**任务：从真实（或手造）dump 产出一套「预测 vs GT」对比图集，并写一张观察记录表。**

在完成 u1-l4 环境与数据准备的前提下：

1. **开 dump 跑一个子任务**（真实路径，待本地验证）：

   ```bash
   bash eval_ground3d.sh --checkpoint <CKPT> --levels part \
       --sub_tasks functional_object_grounding --mask-dump viz_dumps
   # dump 落在 viz_dumps/scannet/pred_qa_data_val/part_qa_data/qa_data/functional_object_grounding/
   ```

   若暂无 GPU/checkpoint，则用 4.2.4 的手造 fixture 代替，并补造一份 GT dump（同结构、掩码换成「理想预测」）。

2. **导出预测侧**：对 dump 分支目录跑 combine 与 `--group 1` 两种模式：

   ```bash
   python tools/export_grounding_ply.py \
       --masks_dir viz_dumps/scannet/pred_qa_data_val/part_qa_data/qa_data/functional_object_grounding \
       --points_dir data/scannet/points --out viz_ply/pred
   ```

3. **导出 GT 侧**：把同目录下的 `*_qa_gt_masks.npy` 复制到新目录并改名为 `*_qa_pred_masks.npy`（格式同构，见 4.2.3 ⑤），用同样命令导出到 `viz_ply/gt`。
4. **观察记录表**：挑 5 个场景，逐个打开 `viz_ply/pred/{scene}.ply` 与 `viz_ply/gt/{scene}.ply`，记录：场景名、预测区域数 vs GT 区域数、颜色对应情况（红对红？绿对绿？）、有无「整片染色」（对应漏检的补零行或失败接地）、skip 行提示的缺失 bin。把表格与你随后跑出的 u6-l1 mIoU 数字并排——找出一例「mIoU 高但图面明显偏差」或反之的案例。
5. **预期收获**：亲手验证「数字指标」与「图面质量」的互补性，并对 `max_frac`、行并集、先到先得三个规则形成肌肉记忆。

## 6. 本讲小结

- mask dump 是 `Grounded_LMM_main.predict()` 出口的旁路：环境变量 `MASK_DUMP_DIR` **优先于** `decoder.save_viz_dir`（`or` 短路），逐场景写出 `{scene}_qa_pred_masks.npy` 与 `{scene}_qa_gt_masks.npy` 两个 object 数组，与 `points/{scene}.bin` 靠同名字符串契约配对。
- 批量评估必须用 `eval_ground3d.sh --mask-dump` 让 dump 目录**镜像**预测目录树，否则同一场景在多个子任务运行中会互相静默覆盖。
- `export_grounding_ply.py` 是纯离线工具（按文件路径加载 `grounding_viz`，不 import torch），`combine` 一个场景一个 ply、按条目序取调色板 5 色循环，`per_qa` 一条 QA 一个恒红 ply，`--group N` 让颜色在每组内重新从红开始（多轮对话「一红一绿」的正确打开方式）。
- 「每个 `<SEG>` → 一种颜色」的精确语义：**每条 dump 条目（一条 QA / 多轮的一轮）经行并集后至多一个区域**；单条 QA 内多个 `<SEG>` 会被并集成同色区域；重叠处先到先得；颜色编号跳过被过滤的退化掩码后仍连续。
- `masks_to_labels` 的 `frac==0 or frac>max_frac(0.85)` 双判据挡住空掩码与「接地一切」：其中全 True 掩码的重要来源是无指称 QA 的零查询 + `1e-4` 哨兵经 `sigmoid≈0.5 > 0.4` 阈值的产物。
- 写盘细节：背景 RGB 乘 `dim_bg=0.55` 调暗以突出高饱和区域色；PLY 为二进制、顶点 6 字段（`x/y/z` f4 + `red/green/blue` u1）；`dim_bg/palette` 无 CLI 开关，只能编程调用。

## 7. 下一步学习建议

- 下一讲进入单元 7：**u7-l1 迁移基准 ScanRefer 与 Reason3D**——mask dump + PLY 导出同样是检视迁移效果的利器，可先想好如何把本讲流程套到那两个基准的评估配置上。
- 回顾 **u6-l3 文本指标**：`--mask-dump` 产出的同一棵目录树也是 GM-δ（IoU+δ 联合判定）里掩码通道的数据源，两条消费链共享一个 dump。
- 想做可视化增强（单 QA 多 `<SEG>` 分色、逐区域半透明、网页端查看器），从改 `masks_to_labels` 的行级标签化入手，是一个改动面小、验证闭环快的二次开发练习（衔接 u7-l2）。
