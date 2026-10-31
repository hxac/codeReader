# 迁移基准：ScanRefer 与 Reason3D 的微调评估

## 1. 本讲目标

前面六个单元里，我们一直在 Ground3D 自建数据集（247 万 QA 对）的世界里打转。本讲换一个视角：**把 Ground3D-LMM 迁移到两个外部基准（ScanRefer、Reason3D）上，做逐数据集微调后的评估**。这是论文 Table 5 / Table 6 的来源，也是检验「这套架构是不是只在自家数据上有效」的关键实验。

学完本讲，你应该能够：

1. 独立说出 ScanRefer / Reason3D 标注到 `*_infos_{train,val}.pkl` 的完整转换链，并指出它与 Ground3D 原生数据管线的两个本质差异（**整场景 vs 帧级部分场景**、**外部人工标注 vs 自产 QA**）。
2. 解释 `tools/build_scene_images.py` 如何用确定性的 `linspace` 采样为每个场景挑出 20 张 RGB 视图，以及为什么「确定性」对可复现评估至关重要。
3. 区分 2×2 四套迁移评估配置（数据集 × 3D/3D+2D）的差异点，记住四个 mIoU 基线数字，并知道复现时的三个注意事项（方差、NCCL 超时、最优 epoch）。
4. 避开四个真实的坑：`'scanrefer'` 不是合法的转换 key、`reason3d_file` 参数加载后从未使用、迁移配置的 LoRA rank 与 Ground3D 内部配置规则不同、四路输出目录必须隔离。

## 2. 前置知识

### 2.1 什么是「迁移评估」

Ground3D-LMM 在自家数据集上训练后，作者又在 ScanRefer 和 Reason3D 上**分别微调再评估**（per-dataset fine-tuning，不是 zero-shot）。这是 3D 社区的常规做法：外部基准有公开排行榜，横向对比才有说服力。所以本讲的四套配置里都保留了 `train_dataloader` / `optim_wrapper` / `epoch` 等训练字段——它们就是微调时用的配置，评估时只是借用同一条数据管线。

### 2.2 两个基准是什么

| | ScanRefer | Reason3D |
|---|---|---|
| 任务性质 | 3D 指称分割（referring segmentation） | 3D 推理分割（reasoning segmentation） |
| 输入 | 一句自然语言描述（如 "the brown wooden table next to the sofa"） | 一个推理问题（需要常识/空间推理才能确定目标） |
| 目标粒度 | **实例级**：描述唯一指向一个物体实例 | **语义级**：目标是某一类物体（全部实例） |
| 答案形式 | 把描述原句回显并包裹接地标记 | 说出目标物体的类别名 |
| 构建于 | ScanNet | ScanNet |

这个「实例级 vs 语义级」的粒度差异，会直接落到代码里两行不同的 GT 索引上（见 4.2.3）。

### 2.3 整场景 vs 帧级：世界观转变

u2 单元里 Ground3D 数据的帧 ID 是 `场景_frame_序号`（单视角部分点云）；本讲两个基准都回到**完整场景**（`scene0011_00` 这种裸场景 ID，整间房的点云）。这解释了后面很多代码差异：

- 转换器不扫 `vert.npy` 定帧，而是**以标注 JSON 的场景键为权威**；
- 评估解码器不再按 `_frame_` 切场景 ID 去找单帧图像，而是要么完全不给图（3D），要么把整个图像目录喂进去（3D+2D）。

### 2.4 前置讲义衔接

| 前置讲义 | 本讲复用的概念 |
|---|---|
| u2-l2 | `create_data.py` 薄入口 + `update_pkl_infos` v2 规范化的转换链模式 |
| u2-l3 | 裸文件名契约、`ScanNetUnifiedSegDataset`、`LoadAnnotations3D_` 按路径键加载 |
| u2-l4 | 超点级 GT 的 scatter-mean 多数投票、NaN 占位契约 |
| u4-l5 | 推理两阶段（generate + 二次前向取隐状态）、`save_pred_qa_dir` 写盘 |
| u4-l6 | Joint 与 3D-only 变体、`image_dir` 语义 |
| u6-l1 | `Part3DGlamm_SegMetric` 的 mIoU/Acc@0.25/Acc@0.50 |
| u6-l2 | `--cfg-options` 覆盖、LoRA rank 失配被静默吞掉的隐患 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [docs/DATA_SCANREFER_REASON3D.md](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/DATA_SCANREFER_REASON3D.md) | 官方复现手册：数据获取、四行评估表、目录布局、坑 |
| [tools/create_data_scanrefer.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/create_data_scanrefer.py) | ScanRefer infos pkl 转换入口（薄脚本） |
| [tools/create_data_reason3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/create_data_reason3d.py) | Reason3D infos pkl 转换入口（薄脚本） |
| [tools/part3d_glamm_data_utils.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py) | 转换器实现：`_ScanNetSceneGroundingData` 共享基类 + 两个子类 |
| [tools/update_infos_to_v2.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/update_infos_to_v2.py) | v2 规范化：`update_reason3d_infos` 保留 `qa_data`/`super_pts_path` |
| [tools/build_scene_images.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/build_scene_images.py) | 为每个场景确定性抽取 20 张 RGB 帧 |
| [configs/ground3dlmm_eval_scanrefer.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_scanrefer.py) | ScanRefer 3D 评估 + 微调配置 |
| [configs/ground3dlmm_eval_scanrefer_image.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_scanrefer_image.py) | ScanRefer 3D+2D 配置 |
| [configs/ground3dlmm_eval_reason3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_reason3d.py) | Reason3D 3D 配置 |
| [configs/ground3dlmm_eval_reason3d_image.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_reason3d_image.py) | Reason3D 3D+2D 配置 |
| [uniseg3d/transforms_3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py) | 四个迁移专用 transform（两张 GT 表 + 两个 QA 生成器） |
| [uniseg3d/query_decoder.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py) | `Grounded_Decoder_Eval_Text` / `Grounded_Decoder_Eval_MultiImg` 两个评估解码器 |
| [uniseg3d/scannet_dataset.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/scannet_dataset.py) | `ScanNetUnifiedSegDataset`：四个迁移配置共用的数据集类 |
| [uniseg3d/loading.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/loading.py) | `LoadAnnotations3D_`：按路径键存在与否加载四类 mask |
| [uniseg3d/prompt_template.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py) | 两个系统指令模板（3D 版 / 带图版） |
| [uniseg3d/formatting.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/formatting.py) | `Pack3DDetInputs_Reason3D` 打包路由 |

## 4. 核心概念与源码讲解

### 4.1 模块一：两个基准的数据准备脚本

#### 4.1.1 概念说明

迁移的第一步是把外部基准的标注变成 mmengine 数据管线能消费的 `*_infos_{train,val}.pkl`。这一步的输入是「三件套」：

1. **ScanNet 基座**：`scannet_instance_data/`（标准 oneformer3d 式预处理，含 `vert.npy` 点云、`sp_label.npy` 超点、`ins_label.npy` 实例标签、对齐框等）——两个基准都构建在 ScanNet 上，所以基座共用；
2. **逐场景标注 JSON**：官方发布的 ScanRefer/Reason3D 标注被整理成「一个 JSON 按 `scene_id` 键控、值是该场景的 QA 列表」的形式；
3. **`<scene>_reason.pth` 张量**：Reason3D 官方 `prepare_data_reason.py` 产出的逐点五元组（坐标、颜色、超点、ScanNet-200 语义标签、实例标签）。

为什么和 u2-l2 的 `Part3dGlammData` 长得像但不是同一个类？因为两个本质差异：Ground3D 数据是帧级（部分点云），这里是整场景；Ground3D 的 QA 是自产的、转换时要展平补 `<SEG>`，这里的 QA 是外部人工标注、**整段原样搬运进 pkl**（`qa_data` 键），格式适配推迟到运行时的 transform 里做（4.2）。

#### 4.1.2 核心流程

```
scannet_instance_data/<scene>_vert.npy ─┐
scannet_instance_data/<scene>_sp_label.npy ─┤├─> _ScanNetSceneGroundingData.get_infos()
<qa_data_dir>/<dataset>_<split>_by_scene.json ─┤│      每个场景:
<reason_pth_dir>/<split>/<scene>_reason.pth ─┘│        1. 复制点云 → points/<scene>.bin
                                              │        2. 复制超点 → super_points/<scene>.bin
                                              │        3. 实例 mask ← 子类钩子 _instance_mask()
                                              │        4. 语义 mask ← reason.pth[3]（ScanNet-200）
                                              │        5. qa_data ← 标注 JSON 的该场景列表（原样）
                                              │        6. annos ← 对齐框 + 轴对齐矩阵
                                              └─> {prefix}_infos_{split}.pkl
                                                      │
                                        update_pkl_infos('reason3d', ...)
                                                      │  v2 规范化: 路径削成裸文件名、
                                                      │  保留 super_pts_path 与 qa_data、
                                                      │  annos 转成 instances 列表
                                                      ▼
                                        data/scannet/oneformer3d_{scanrefer,reason3d}/
                                        （points/ instance_mask/ semantic_mask/ super_points/ + pkl）
```

#### 4.1.3 源码精读

**（a）两个薄入口几乎逐字相同。** [tools/create_data_scanrefer.py:18-49](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/create_data_scanrefer.py#L18-L49) 定义 `scanrefer_data_prep`：对 `train`/`val` 两个 split 各跑一遍「构建 infos → dump pkl → 立刻做 v2 更新」。注意第 49 行调用的是 `update_pkl_infos('reason3d', ...)`——**ScanRefer 借用了 Reason3D 的整场景更新例程**（脚本内注释也写明这一点）。[tools/create_data_reason3d.py:18-49](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/create_data_reason3d.py#L18-L49) 结构完全一致，只是导入的转换器类换成 `Reason3dData`。

这里有个必须知道的坑：查 [tools/update_infos_to_v2.py:577-589](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/update_infos_to_v2.py#L577-L589) 的分发逻辑，合法的 dataset key 只有 `scannet` / `scannet200` / `part3d_glamm` / `reason3d` / `instruct3d`，**传 `'scanrefer'` 会直接 `NotImplementedError`**。两个入口脚本之所以能工作，正是因为它们硬编码传了 `'reason3d'`。

**（b）共享基类：模板方法模式。** [tools/part3d_glamm_data_utils.py:244-287](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L244-L287) 是 `_ScanNetSceneGroundingData.__init__`：

- 子类只需声明 `qa_json_template`（如 `'ScanRefer_filtered_{split}_by_scene.json'`），基类据此拼路径加载标注 JSON；
- 第 285-287 行把样本清单过滤为「JSON 键中以 `scene` 开头的场景」——**场景清单以标注 JSON 为权威**，而不是 u2-l2 里 Part3dGlammData 的 txt split 清单；
- 类头注释（第 226-233 行）明确说明了「整场景 vs 帧级」的定位差异。

[tools/part3d_glamm_data_utils.py:314-317](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L314-L317) 声明抽象钩子 `_instance_mask`——**这是两个子类唯一的实质差异点**。

**（c）逐场景处理主体。** [tools/part3d_glamm_data_utils.py:319-407](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L319-L407) 的 `get_infos` 内嵌 `process_single_scene`：

- 第 327-342 行：把 `scannet_instance_data` 里的 `vert.npy` 与 `sp_label.npy` 原样转存为裸 `.bin`（复用基座，不重算）；
- 第 344-355 行：实例 mask 调子类钩子 `_instance_mask(...)` 后强制 `int64` 落盘（延续 u2-l2 的 dtype 契约）；
- 第 357-369 行：语义 mask 取 `reason_data[3]`（`_reason.pth` 五元组里的 ScanNet-200 语义标签，见 [tools/part3d_glamm_data_utils.py:305-312](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L305-L312) 的加载器与注释）；
- 第 371 行：`info['qa_data'] = self.qa_files[sample_idx]`——**整段 QA 原样进 pkl**，不做任何展平或 `<SEG>` 补齐（对比 u2-l2 的 `_flatten_qa_data`）；
- 第 373-399 行：从 `scannet_instance_data` 的对齐框文件组装 `annos`，类别查表用 [tools/part3d_glamm_data_utils.py:237-241](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L237-L241) 定义的 19 个 ScanNet-18 检测类。

**（d）两个子类只差三行。**

- [tools/part3d_glamm_data_utils.py:410-425](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L410-L425)：`ScanReferData` 的实例 mask 取 `reason_data[4]`（`_reason.pth` 的实例标签字段）；
- [tools/part3d_glamm_data_utils.py:428-442](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L428-L442)：`Reason3dData` 的实例 mask 取基座里的 `<scene>_ins_label.npy`。

**（e）v2 更新保留两个「编外」键。** [tools/update_infos_to_v2.py:472-552](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/update_infos_to_v2.py#L472-L552) 的 `update_reason3d_infos` 是整场景版本的规范化：路径削成裸文件名（目录交给配置的 `data_prefix` 运行时拼接，即 u2-l3 的裸文件名契约），其中第 501-505 行显式保留 `super_pts_path` 与 `qa_data`——这两个键不在 mmengine 标准 info schema 里，漏掉任何一个下游都会崩。

#### 4.1.4 代码实践

**实践目标**：不下载任何数据，仅凭源码推导出转换脚本的完整命令行接口，并验证一份现成 pkl 的关键字段。

**操作步骤**：

1. 阅读两个入口脚本的 `parse_args`（[tools/create_data_scanrefer.py:52-82](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/create_data_scanrefer.py#L52-L82)），把五个参数（`--root-path` / `--qa-data-dir` / `--reason-pth-dir` / `--out-dir` / `--info-prefix`，外加可选 `--workers`）的默认值与是否必填抄成一张表。
2. 对照 [docs/DATA_SCANREFER_REASON3D.md:47-71](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/DATA_SCANREFER_REASON3D.md#L47-L71) 的两条官方命令，核对每个参数指向的目录内容。
3. （可选，需要先按手册跑完转换）用下面的检查脚本读取生成的 val pkl：

```python
# 示例代码：检查 scanrefer_infos_val.pkl 的关键字段（非项目原有代码）
import mmengine
d = mmengine.load('data/scannet/oneformer3d_scanrefer/scanrefer_infos_val.pkl')
item = d['data_list'][0]
print('lidar_path     :', item['lidar_points']['lidar_path'])   # 应为裸文件名 xxx.bin
print('super_pts_path :', item['super_pts_path'])               # 应为裸文件名 xxx.bin
print('qa_data 类型    :', type(item['qa_data']))               # 应为 list（该场景的 QA 条目）
print('qa_data[0] 键  :', sorted(item['qa_data'][0].keys()))    # ScanRefer 应含 description/object_id
print('metainfo       :', d['metainfo']['dataset'], d['metainfo']['info_version'])
```

**需要观察的现象**：`lidar_path`/`super_pts_path` 是裸文件名而非绝对路径；`qa_data` 是列表且首条含 `description`、`object_id`（ScanRefer）或 `question`、`object_name`、`object_id`（Reason3D）。

**预期结果**：步骤 1 的参数表与 docs 命令一一对应；步骤 3 的五个打印符合上述描述。步骤 3 在本地数据就绪前**待本地验证**；步骤 1、2 纯源码阅读，现在即可完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `create_data_scanrefer.py` 第 49 行传给 `update_pkl_infos` 的第一个参数是 `'reason3d'` 而不是 `'scanrefer'`？

**答案**：`update_pkl_infos` 的分发表（update_infos_to_v2.py:577-589）只认 `scannet`/`scannet200`/`part3d_glamm`/`reason3d`/`instruct3d` 五个 key，传 `'scanrefer'` 会触发 `NotImplementedError`。ScanRefer 与 Reason3D 同为「整场景 + qa_data」结构，`update_reason3d_infos` 的规范化逻辑（保留 super_pts_path/qa_data、annos 转 instances）对两者通用，所以直接借用。

**练习 2**：`_ScanNetSceneGroundingData` 用什么决定「处理哪些场景」？这和 `Part3dGlammData`（u2-l2）有何不同？

**答案**：以标注 JSON（`*_by_scene.json`）的顶层键为准，并过滤出 `scene` 开头的键（part3d_glamm_data_utils.py:282-287）；Part3dGlammData 则以 split txt 清单为唯一权威、再扫 `vert.npy` 定帧。前者是整场景、外部标注驱动；后者是帧级、自产清单驱动。

**练习 3**：ScanRefer 和 Reason3D 的转换器子类在 `_instance_mask` 钩子里的取数来源分别是什么？

**答案**：ScanReferData 取 `_reason.pth` 五元组的第 5 个元素 `reason_data[4]`（实例标签）；Reason3dData 取基座 `scannet_instance_data/<scene>_ins_label.npy`。

### 4.2 模块二：QA 模板与超点 GT 表生成

#### 4.2.1 概念说明

pkl 里的 `qa_data` 是「生」的外部标注（ScanRefer 的 `description`、Reason3D 的 `question`），模型不能直接吃。运行时管线里有一对 transform 把它变成训练/推理所需的三样东西：

1. **问题与回答模板**：把原始描述/问题包上触发词和接地标记，做成模型熟悉的 `<p>…</p><SEG>` 形式；
2. **逐 QA 的超点级 GT mask**（`sp_gt_seg`）：这是 mIoU 的监督/对齐目标；
3. **三级归档键**（`qa_keys`/`qa_level`/`qa_task_type`）：迁移基准里三者统一填基准名，所以预测 JSON 的归档层级是平的（对比 Ground3D 的 9 类任务树）。

粒度差异在这里落地：**ScanRefer 的 GT 行取自实例表 `gt_sp_masks_ins`（object_id = 实例编号），Reason3D 的 GT 行取自语义表 `gt_sp_masks_sem`（object_id = 语义类别编号）**。

两个基准还各自带一个「触发词」约定（与 4.4.3 的指令模板呼应）：

- ScanRefer：问题结尾加 `<sentence>` → 模型应「返回整句描述并包进 `<p>句子</p><SEG>`」；
- Reason3D：问题结尾加 `<SEG><name>` → 模型应「只回答物体名」。

#### 4.2.2 核心流程

```
LoadAnnotations3D_ 按路径键加载四类 mask（pts_instance_mask / pts_semantic_mask / sp_pts_mask …）
        │
        ▼
AddSuperPointAnnotations_{ScanRefer,Reason3D}
        逐点 one-hot → scatter_mean 到超点 → >0.5 二值化
        产出 gt_sp_masks_ins（实例×超点）与 gt_sp_masks_sem（语义×超点）
        │
        ▼
QA_Generation_{ScanRefer,Reason3D}[_Test]
        对 qa_data 每条: 套模板(question/answer) + 打归档键
        GT = 对应表第 object_id 行 (1, num_superpoints)
        行全 0 → NaN 整行占位（不删条目）
        产出 selected_qa_data + sp_gt_seg（逐 QA 逐索引对应）
        │
        ▼
Pack3DDetInputs_Reason3D: selected_qa_data/scene_name 走 inputs 通道,
                          sp_gt_seg 走 data_samples 通道（承接 u2-l5 双通道图景）
```

#### 4.2.3 源码精读

**（a）ScanRefer 版 GT 表：故意不重编号。** [uniseg3d/transforms_3d.py:1437-1574](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1437-L1574) 的 `AddSuperPointAnnotations_ScanRefer`：第 1475-1486 行把「stuff 类置 -1 + 实例重编号」的整段逻辑**注释掉了**——因为 ScanRefer 的 `object_id` 就是 ScanNet 原始实例编号，one-hot 之后表的行号恰好等于原始实例 id（第 1497-1501 行处理 `-1` 点：置为 max+1 再 one-hot 后砍掉最后一列），下游才能用 `gt_sp_masks_ins[object_id]` 直接对位。第 1503-1511 行是熟悉的 one-hot → `scatter_mean` → `>0.5` 多数投票（u2-l4 同款），第 1557-1563 行同时产出 `gt_sp_masks_ins` 与 `gt_sp_masks_sem` 两张表。

**（b）Reason3D 版 GT 表：正常重编号，但 QA 用的是语义表。** [uniseg3d/transforms_3d.py:692-821](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L692-L821) 的 `AddSuperPointAnnotations_Reason3D`：第 730-741 行把 stuff 类（墙/地板）与忽略点置 -1 并重编号连续实例 id；第 745-761 行同样 one-hot + scatter-mean 产出实例表；第 774-794 行用逐点语义标签同样手法产出语义表（最后一行兜底「无任何类的超点」）。第 804-810 行把两张表都写进 results。**关键在 QA 侧取哪张**：[uniseg3d/transforms_3d.py:827-873](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L827-L873) 的 `QA_Generation_Reason3D_Test` 第 842 行取 `gt_sp_masks_sem`、第 862 行 `gt_sp_sem_masks[object_id]` 直接按语义类别号取行——这就是「Reason3D 目标是语义级」在代码里的落点。

**（c）QA 模板与 NaN 兜底。** ScanRefer 训练版 [uniseg3d/transforms_3d.py:1581-1645](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1581-L1645)（评估版 [1653-1712](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1653-L1712) 逻辑相同、无采样）：

```python
qa['question'] = qa['description'] + '<sentence>'
qa['answer']   = '<p>' + qa['description'] + '</p><SEG>'   # 回答=描述原句回显
i_qa_seg = gt_sp_instance_masks[object_id].unsqueeze(0)    # 实例表第 object_id 行
```

（摘自 [transforms_3d.py:1617-1630](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1617-L1630)。）Reason3D 版（[transforms_3d.py:853-862](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L853-L862)）则是 `question + '<SEG><name>'`、`answer = '<p>' + object_name + '</p><SEG>'`（回答=类别名）。两边的 GT 行若全 0（目标不在场景里），都用 NaN 整行占位（[transforms_3d.py:1633-1637](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1633-L1637)、[863-865](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L863-L865)），延续 u2-l4「永不交付错位 GT」的契约——u6-l1 的 `get_iou` 靠「行和>0」自动剔除 NaN 行，所以这些条目不计入 mIoU。

**（d）三个值得记录的怪癖。**

1. **`reason3d_file` 加载后从未使用。** 三个 QA transform 的 `__init__` 都执行 `self.reason3d_data = json.load(open(reason3d_file))`（[transforms_3d.py:1584](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1584)、[830](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L830)、[1656](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1656)），但全文件检索可知 `self.reason3d_data` 只出现于这三处赋值——真正喂给模型的是 pkl 里的 `input_dict['qa_data']`。这个参数的实际作用只剩「文件必须存在，否则 `open(None)` 直接抛错」。
2. **Reason3D 的 train_pipeline 用的是 `_Test` 变体**（[configs/ground3dlmm_eval_reason3d.py:135-136](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_reason3d.py#L135-L136)，指向 train 的 by_scene JSON）：训练时不做 `num_qa` 采样，场景内全部 QA 参与训练；ScanRefer 训练则用带采样的 `QA_Generation_ScanRefer`（[configs/ground3dlmm_eval_scanrefer.py:135-137](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_scanrefer.py#L135-L137)，`num_qa=10`）。
3. **`with_seg_3d=False` 不代表语义 mask 不加载。** 训练管线的 `LoadAnnotations3D_` 关着 `with_seg_3d`（如 [configs/ground3dlmm_eval_scanrefer.py:115-120](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_scanrefer.py#L115-L120)），但覆写版 `_load_masks_3d` 的加载完全由「results 里是否存在对应 `*_path` 键」驱动（[uniseg3d/loading.py:70-90](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/loading.py#L70-L90)）——`pts_semantic_mask_path` 在 pkl 里有，语义 mask 就会被加载，供 `AddSuperPointAnnotations_*` 使用（[transforms_3d.py:1472-1473](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1472-L1473) 需要它）。

**（e）数据集与打包。** 四份配置的 `dataset_type` 都是 `ScanNetUnifiedSegDataset`：[uniseg3d/scannet_dataset.py:18-35](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/scannet_dataset.py#L18-L35) 在原始 info 上拼 `super_pts_path` 绝对路径、调父类后再从 `lidar_path` 提取 `scene_name`（整场景 ID，如 `scene0011_00`）。`qa_data` 键随 info 条目透传进 results（u2-l3 讲过同一机制）。最后 [uniseg3d/formatting.py:328-348](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/formatting.py#L328-L348) 的 `Pack3DDetInputs_Reason3D` 路由：`selected_qa_data`、`scene_name` 在 `INPUTS_KEYS`（前向通道），`sp_gt_seg` 在 `SEG_KEYS`（监督通道）——与 u2-l5 的双通道图景一致。

#### 4.2.4 代码实践

**实践目标**：不依赖 GPU 和项目环境，手工复现「逐点 one-hot → scatter-mean → >0.5」的超点 GT 表生成，理解多数投票的边界行为。

**操作步骤**：运行下面的独立脚本（示例代码，仅需 numpy；也可以纸笔推演）：

```python
# 示例代码：最小 scatter-mean 多数投票实验（非项目原有代码）
import numpy as np

num_pts, num_sp = 5, 2
sp_pts_mask   = np.array([0, 0, 1, 1, 1])       # 点 → 超点
instance_mask = np.array([1, 1, 0, 1, 1])       # 点 → 实例 id（原始编号，不重排）

one_hot = np.eye(instance_mask.max() + 1)[instance_mask].T   # (2 实例, 5 点)
print('逐点 one-hot 表:\n', one_hot)

# 逐超点求均值（等价于 torch_scatter.scatter_mean(insts.float(), sp_pts_mask, dim=-1)）
frac = np.stack([one_hot[:, sp_pts_mask == s].mean(axis=1) for s in range(num_sp)])  # (超点, 实例)
print('超点内实例占比:\n', frac.T)
print('阈值 0.5 二值化后的 gt_sp_masks_ins:\n', (frac.T > 0.5))
```

**需要观察的现象**：实例 1 在超点 0 中占比 2/2=1.0、在超点 1 中占比 3/5=0.6，均过阈值；实例 0 在超点 1 中占比 2/5=0.4，**不过**阈值。

**预期结果**（可手工推导）：

```
gt_sp_masks_ins =
[[False, False],   # 实例 0：两个超点内占比 0 与 0.4，均 ≤ 0.5
 [True,  True ]]   # 实例 1：占比 1.0 与 0.6，均 > 0.5
```

把 `instance_mask` 改成 `[1, 0, 0, 1, 1]`（超点 0 内 1:1 平局）再跑一次：平局占比恰好 0.5，`> 0.5` 判 False——**多数必须是严格多数**。这也解释了为什么小实例可能整行全 0、进而触发 4.2.3（c）的 NaN 兜底。

#### 4.2.5 小练习与答案

**练习 1**：ScanRefer 的 GT 为什么取实例表、Reason3D 的 GT 为什么取语义表？

**答案**：由两个基准的目标粒度决定。ScanRefer 是实例级指称（一条描述唯一指向一个实例，`object_id` 是实例编号），所以 `QA_Generation_ScanRefer*` 用 `gt_sp_masks_ins[object_id]`（transforms_3d.py:1601/1630）；Reason3D 是语义级推理分割（目标是一类物体，`object_id` 是语义类别编号），所以 `QA_Generation_Reason3D_Test` 用 `gt_sp_masks_sem[object_id]`（transforms_3d.py:842/862）。

**练习 2**：`AddSuperPointAnnotations_ScanRefer` 里被注释掉的重编号代码，如果恢复会发生什么？

**答案**：重编号会把实例 id 压缩成 0..K-1 连续编号，one-hot 表的行号随之改变，而 ScanRefer 标注里的 `object_id` 仍是原始实例编号，`gt_sp_masks_ins[object_id]` 会取到错误的行（GT 张冠李戴）。Reason3D 版可以重编号，是因为它的 QA 不查实例表（查语义表，语义表由语义标签直接 one-hot，不受实例重编号影响）。

**练习 3**：预测 JSON 里 ScanRefer 条目的归档层级长什么样？和 Ground3D 自家评估的预测 JSON 有何区别？

**答案**：三个归档键（`qa_keys`/`qa_level`/`qa_task_type`）全部填 `'scanrefer'`（或 `'reason3d'`），所以 JSON 是 `{'scanrefer': {'scanrefer': {'scanrefer': [条目...]}}}` 的扁平结构；Ground3D 自家评估的三级键是 task_level/task_type/sub_task 的 9 类任务树（u1-l4），层级有实际语义。

### 4.3 模块三：20 视图图像构建

#### 4.3.1 概念说明

3D+2D 行给模型的不是单帧图像，而是**每个场景 20 张均匀分布的 RGB 视图**（把「房间从 20 个角度看」作为点云几何之外的 2D 上下文）。`tools/build_scene_images.py` 负责从 ScanNet 官方 SensReader 解码出的原始帧序列（`scannet/scans/<scene>/color/<i>.jpg`，一个连续的 0..N-1 帧号区间）中抽帧。

它的设计要点是**确定性**：抽哪些帧只是「该场景帧数 N」这一个整数的纯函数，不含任何随机性或环境依赖——这样每个用户本地构建出的 20 帧都完全一致，评估结果才可比较。这在学术界是「不可再分发数据、人人本地重建」约束下的必要工程纪律（见 [docs/DATA_SCANREFER_REASON3D.md:15-17](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/DATA_SCANREFER_REASON3D.md#L15-L17) 关于 ScanNet ToU 不可再分发的说明）。

#### 4.3.2 核心流程

```
场景清单来源（二选一，互斥必填）:
  --pkl    : 从 infos pkl 的 lidar_points.lidar_path 剥出场景 ID（兼容 <scene>_frame_<n> 与裸 <scene>）
  --scenes : 文件 / glob / 空格逗号列表（正则 scene\d{4}_\d{02} 提取）
        │
        ▼  逐场景
list_color_indices(raw_root, scene): 列出 color/ 下全部数字命名的 .jpg 帧号（跳过 ._ 苹果双击文件），升序
        │           无 color/ 目录或为空 → [skip] 并计数，不中断
        ▼
sample_indices(frame_indices): N ≥ 20 → linspace(0, N-1, 20) 截断取整后映射回真实帧号
                               N < 20 → 全取（不复制不填充）
        │
        ▼
<out>/<scene>/<frame_index>.jpg   ← 已存在的跳过（可断点续跑），缺失的从原始帧复制
```

#### 4.3.3 源码精读

- **常量与两个输入解析器**：[tools/build_scene_images.py:69](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/build_scene_images.py#L69) 定义 `NUM_FRAMES = 20`；[第 109-122 行](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/build_scene_images.py#L109-L122) `scenes_from_pkl` 从 pkl 的样本路径剥场景 ID——先去扩展名，再按 `_frame_` 切（有则取前半，无则整名即场景），迁移 pkl 的裸场景 ID 走后者；[第 125-143 行](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/build_scene_images.py#L125-L143) `scenes_from_arg` 支持文件/glob/列表三种来源并去重。
- **帧号列举**：[tools/build_scene_images.py:73-89](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/build_scene_images.py#L73-L89) 的 `list_color_indices`：目录不存在返回 `None`（区别于空列表），过滤 `._` 前缀与非 `.jpg`，数字词干转 int 后排序。
- **核心采样**：[tools/build_scene_images.py:92-106](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/build_scene_images.py#L92-L106) 的 `sample_indices`：

```python
positions = np.linspace(0, n - 1, NUM_FRAMES).astype(int)
return [frame_indices[p] for p in positions]
```

`.astype(int)` 是**向零截断**（不是四舍五入）：第 k 个位置取 \( \left\lfloor \tfrac{k\,(N-1)}{19} \right\rfloor \)。文件头注释（[第 18-35 行](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/build_scene_images.py#L18-L35)）专门强调了这一点并给出实例：`linspace(0, 5577, 20)` → `0, 293, 587, 880, ...`。由于帧号区间连续（0..N-1），「按个数 N」与「按 max_index+1」两种读法结果逐位相同。
- **主循环**：[tools/build_scene_images.py:146-193](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/build_scene_images.py#L146-L193)：互斥必填的 `--pkl`/`--scenes`（第 149-151 行）；目标文件已存在则跳过写入（第 181-183 行，幂等、可断点续跑），缺失则调 `ef.extract_scannet`（复用 `tools/extract_frame.py` 的 SensReader 配方复制该帧，调用点注释「copies color/<idx>.jpg」）；结尾打印「N=帧数 → 写入帧数 + 抽中帧号首尾」与跳过统计（第 187-193 行）。

产物的目录树 `<out>/<scene>/<idx>.jpg` 正是解码器 `model.decoder.image_dir` 期望的布局（文件头注释第 4-16 行说明，与 4.4.3 的 `MultiImg` 解码器读法互为契约）。官方用法见 [docs/DATA_SCANREFER_REASON3D.md:75-91](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/DATA_SCANREFER_REASON3D.md#L75-L91)。

#### 4.3.4 代码实践

**实践目标**：用最小数值实验验证 `linspace` 截断采样，理解「确定性纯函数」的含义。

**操作步骤**：运行（任何有 numpy 的环境均可）：

```python
# 示例代码：复现 sample_indices 的抽帧位置（非项目原有代码）
import numpy as np

def sample_positions(n, num_frames=20):
    if n <= num_frames:
        return list(range(n))
    return np.linspace(0, n - 1, num_frames).astype(int).tolist()

print('N=40 :', sample_positions(40))
print('N=15 :', sample_positions(15))     # 不足 20 帧：全取
print('N=5578 前4个:', sample_positions(5578)[:4])   # 文档实例：应得 0,293,587,880
```

**需要观察的现象**：N=40 时抽到的几乎全是偶数帧且最后一帧是 39；N=15 时返回 0..14；N=5578 的前四个值与源码注释给出的 `0, 293, 587, 880` 完全一致。

**预期结果**（可手工推导，文档注释可交叉验证）：

- `N=40` → `[0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 34, 36, 39]`（步长 39/19≈2.05，逐项截断后恰好隔 2 取 1，末项恒为 N-1）；
- `N=15` → `[0, 1, ..., 14]`；
- `N=5578` 前 4 项 → `[0, 293, 587, 880]`。

**追问**（纸笔）：把 `.astype(int)` 换成 `.astype(int) + 1`，抽出的帧会怎么变？——整体右移一格且末帧越界（N-1+1=N），说明「截断」与「末项=N-1」是这套采样能自洽的两个细节。

#### 4.3.5 小练习与答案

**练习 1**：`build_scene_images.py` 如何保证不同用户构建的 20 帧完全一致？

**答案**：抽帧位置是 `numpy.linspace(0, N-1, 20).astype(int)`（build_scene_images.py:105），是帧数 N 的纯函数——无随机数、无时间戳、无环境差异；`.astype(int)` 的向零截断行为也是确定的（numpy 语义），且帧号区间连续保证「按个数」与「按 max+1」等价（文件头注释第 27-32 行）。

**练习 2**：某场景只解码出 13 帧，脚本怎么处理？某场景忘了跑 SensReader，又怎么处理？

**答案**：13 < 20 时 `sample_indices` 返回全部 13 帧，不复制不填充（第 100-104 行）；没有 `color/` 目录时 `list_color_indices` 返回 `None`，主循环打 `[skip]` 提示先解码 `.sens`、计入 `total_missing` 后继续（第 164-169 行），不会中断整个批任务。

**练习 3**：为什么这个工具要支持 `--pkl` 和 `--scenes` 两种互斥输入？

**答案**：`--pkl` 让场景清单与评估用的数据集严格一致（从 infos pkl 的 lidar_path 提取，第 109-122 行）——评估哪些场景就准备哪些图像；`--scenes`（文件/glob/列表）则覆盖没有 pkl 的场景（例如只想先为一个场景搭可视化），正则 `scene\d{4}_\d{2}` 容忍路径前后缀（第 70、125-143 行）。

### 4.4 模块四：四套迁移评估配置与 mIoU 基线

#### 4.4.1 概念说明

四套配置构成 2×2 矩阵：{ScanRefer, Reason3D} × {3D（纯点云）, 3D+2D（点云 + 20 视图）}。论文基线（[README.md:331-341](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L331-L341)，与 [docs/DATA_SCANREFER_REASON3D.md:8-13](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/DATA_SCANREFER_REASON3D.md#L8-L13) 一致）：

| 行 | 配置 | HF checkpoint | mIoU（论文） |
|---|---|---|---|
| ScanRefer 3D | `configs/ground3dlmm_eval_scanrefer.py` | `Ground3D-LMM-ScanRefer-4B-3D` | 38.72 |
| ScanRefer 3D+2D | `configs/ground3dlmm_eval_scanrefer_image.py` | `Ground3D-LMM-ScanRefer-4B-Joint` | 41.30 |
| Reason3D 3D | `configs/ground3dlmm_eval_reason3d.py` | `Ground3D-LMM-Reason3D-4B-3D` | 36.35 |
| Reason3D 3D+2D | `configs/ground3dlmm_eval_reason3d_image.py` | `Ground3D-LMM-Reason3D-4B-Joint` | 41.29 |

两个可量化的观察：**加 20 视图 RGB 对两个基准都涨点**（ScanRefer +2.58、Reason3D +4.94）；指标打印三件套是 `QA Seg mIoU`（=mIoU）、`QA Seg Precision_quarter`（=Acc@0.25）、`QA Seg Precision_half`（=Acc@0.50）（[docs:136-137](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/DATA_SCANREFER_REASON3D.md#L136-L137)，即 u6-l1 的 `Part3DGlamm_SegMetric`）。

与 Ground3D 内部评估（u4-l6/u6-l2）的一个**机制差异**要特别强调：内部 joint/_3d 两个变体共用同一个 `Grounded_Decoder_Eval`，靠 `image_dir=None` 在运行时过滤 image 条目；迁移基准则**拆成两个解码器子类**——因为内部数据的场景 ID 含 `_frame_`（单帧查找），迁移数据是裸场景 ID + 一个图像目录，父类的「按帧号找一张图」逻辑两头都不适用，于是派生出一对子类各自重写 `_process_single_turn_qa` 与 `infer_reason_eval`。

#### 4.4.2 核心流程

一次迁移评估的调用链（承接 u1-l4/u4-l5/u6-l2 已拆过的部分，只列本讲新差异）：

```
bash tools/dist_test.sh configs/ground3dlmm_eval_scanrefer_image.py <ckpt> 4
   └─ tools/test.py → Runner → TestLoop（配置驱动，无迁移特有逻辑）
        └─ ScanNetUnifiedSegDataset + 4.2 的管线 → selected_qa_data + sp_gt_seg + scene_name
        └─ Grounded_LMM_main.predict()（u3-l1）
             ├─ SpConvUNet + 超点池化（不变）
             ├─ 解码器: Eval_Text（3D 行）或 Eval_MultiImg（3D+2D 行）
             │    每条 QA: 组消息 → generate → 二次前向取隐状态 → <SEG> 查询
             │    场景级写 <save_pred_qa_dir>/<scene>.json
             └─ qa_predict_by_feat + Part3DGlamm_SegMetric → mIoU / Acc@0.25 / Acc@0.50
```

#### 4.4.3 源码精读

**（a）配置差异，用真实 diff 看。** 以下输出是本仓库两份配置文件的逐字 diff（`diff -u`，可在仓库根目录复现）。

3D → 3D+2D（以 ScanRefer 为例，Reason3D 同构）只有四处：

```diff
--- configs/ground3dlmm_eval_scanrefer.py
+++ configs/ground3dlmm_eval_scanrefer_image.py
     decoder=dict(
-        type='Grounded_Decoder_Eval_Text',
+        type='Grounded_Decoder_Eval_MultiImg',
...
-        max_point_tokens=3000,
-        save_pred_qa_dir='pred_qa_scanrefer'),
+        max_point_tokens=2000,
+        save_pred_qa_dir='pred_qa_scanrefer_image',
+        image_dir='data/scannet/scannet_20_img'),
...
-         num_qa=10),
+         num_qa=5),
...
-load_from = 'work_dirs/ground3d_scanrefer_3d/pytorch_model.pth'  # released HF checkpoint; ...
```

即：**换解码器子类、点 token 预算 3000→2000（给 20 张图的视觉 token 腾位置）、输出目录换名、删 `load_from`（checkpoint 必须走命令行位置参数）**，外加训练侧 `num_qa` 减半。源码位置：[configs/ground3dlmm_eval_scanrefer.py:44-73](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_scanrefer.py#L44-L73) vs [configs/ground3dlmm_eval_scanrefer_image.py:44-74](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_scanrefer_image.py#L44-L74)、[L252](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_scanrefer.py#L252)（load_from）。

ScanRefer → Reason3D（同为 3D 行）则换掉数据侧一串（真实 diff 摘要）：`AddSuperPointAnnotations_Reason3D`、`QA_Generation_Reason3D_Test`、`data_root='data/scannet/oneformer3d_reason3d'`、`ann_file='reason3d_infos_{train,val}.pkl'`、`save_pred_qa_dir='pred_qa_reason3d'`、`load_from='work_dirs/ground3d_reason3d_3d/…'`，训练长度 `epoch = 10 → 20`。源码：[configs/ground3dlmm_eval_reason3d.py:130-136](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_reason3d.py#L130-L136)、[L193](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_reason3d.py#L193)、[L239](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_reason3d.py#L239)、[L250](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_reason3d.py#L250)。

**（b）两个解码器子类。**

- [uniseg3d/query_decoder.py:3273-3283](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L3273-L3283) `Grounded_Decoder_Eval_Text`（3D 行）：类 docstring 说明动机——裸场景 ID、无图像，改用纯文本提示。第 3300-3303 行组装消息：`PART_GLAMM_INSTRUCTION` 模板 + 点 token + 问题，`content` 是**纯字符串**（零 image 条目）。一个迁移专属的兜底在第 3342-3347 行：生成的回答若混入 `<|point|>`（模型幻觉出的点 token），查询直接置为零向量，保证分割头仍拿到合法张量（承接 u4-l4「无效查询」的处理家族）。逐场景写预测 JSON 在 [第 3378-3380 行](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L3378-L3380)。
- [uniseg3d/query_decoder.py:3152-3162](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L3152-L3162) `Grounded_Decoder_Eval_MultiImg`（3D+2D 行）：docstring 写明「ScanRefer/Reason3D 场景 ID 是裸的、且带一个多视图帧目录，于是加载 `image_dir/<scene>/` 下全部 `*.jpg`」。关键三段：[第 3239-3244 行](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L3239-L3244) 列目录并**按文件名中的数字排序**（过滤 `._`）；[第 3246-3250 行](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L3246-L3250) 把每张图做成 `{'type':'image', 'image':路径, 'max_pixels':448*448, 'min_pixels':224*224}` 的 image 条目——`max/min_pixels` 智能缩放把每图视觉 token 预算钳在固定区间（u4-l6 同机制，这里是 20 图所以钳得更紧）；[第 3181-3183 行](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L3181-L3183) 把 image 条目列表放在文本条目**之前**组成 user 消息，用 `PART_GLAMM_INSTRUCTION_IMG` 模板。其余（generate → 二次前向取隐状态 → 查询 → 写 JSON，[第 3190-3223 行](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L3190-L3223)）与父类/u4-l5 相同。

两个模板的差异见 [uniseg3d/prompt_template.py:36-52](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py#L36-L52)：3D 版多了 `<sentence>` 触发词的解释与「只依据点云作答」的约束；带图版改为「点云 + 给定图像」。注意 ScanRefer 的问题模板恒带 `<sentence>` 后缀（4.2.3c），而带图版指令里没有这句解释——这是模板与数据侧的历史错位，复现时照抄即可，改动前值得想清楚。

**（c）LoRA rank：不要照搬 u6-l2 的直觉。** u6-l2 讲过 Ground3D 内部评估「joint=16、3D=32」且 `eval_ground3d.sh` 会探测 rank 自动选配置。**迁移四配置全是 `lora_r=32, lora_alpha=64`**（如 [configs/ground3dlmm_eval_scanrefer.py:66-71](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_scanrefer.py#L66-L71)、[configs/ground3dlmm_eval_reason3d_image.py:66-71](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_reason3d_image.py#L66-L71)），且这些配置**不经** `eval_ground3d.sh` 跑（用 `dist_test.sh` 直接跑）。但 u6-l2 的核心警告依然适用：配置 rank 与 checkpoint 不匹配时 `strict=False` 会静默吞掉 adapter、模型无声退回基座——所以务必按表格配对 config 与 checkpoint。

**（d）四路输出互不覆盖。** `save_pred_qa_dir` 四个值（`pred_qa_scanrefer` / `pred_qa_scanrefer_image` / `pred_qa_reason3d` / `pred_qa_reason3d_image`）是刻意的：同一数据集的 3D 与 3D+2D 行共用同一份 pkl 与标注，只能靠配置区分，若输出目录同名会互相覆盖。[docs/DATA_SCANREFER_REASON3D.md:114-120](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/DATA_SCANREFER_REASON3D.md#L114-L120) 明确写了「share inputs, never clobber outputs」。

**（e）复现注意事项**（[docs/DATA_SCANREFER_REASON3D.md:141-150](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/DATA_SCANREFER_REASON3D.md#L141-L150)）：

- **跑分方差约 ±1~1.5 点**：解码虽是贪心，但点云池化/稀疏卷积用了非确定的 GPU 原子操作，固定种子下 mIoU 仍会晃动——落在表格 ±1.5 点内即算复现成功；
- **多卡评估超时**：3D+2D 行每场景 20 图、约慢 2×，跨卡指标聚合可能超过 NCCL 默认 10 分钟超时而中止，配置里设了 `env_cfg.dist_cfg.timeout=7200`（如 [configs/ground3dlmm_eval_scanrefer.py:267-268](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_scanrefer.py#L267-L268)）——**不要删**，或改单卡；
- **发布 checkpoint 是各行最优 val epoch**，结合方差，别指望复刻到小数点。

#### 4.4.4 代码实践

**实践目标**：不下载 checkpoint、不跑模型，用两个只读 diff 把 2×2 配置矩阵的差异完整提取出来。

**操作步骤**：

1. 在仓库根目录执行：

```bash
# 维度一：3D vs 3D+2D（数据集不变）
diff -u configs/ground3dlmm_eval_scanrefer.py  configs/ground3dlmm_eval_scanrefer_image.py
diff -u configs/ground3dlmm_eval_reason3d.py   configs/ground3dlmm_eval_reason3d_image.py
# 维度二：ScanRefer vs Reason3D（模态不变）
diff -u configs/ground3dlmm_eval_scanrefer.py  configs/ground3dlmm_eval_reason3d.py
```

2. 为每个 diff 输出建一张「改动行 → 作用」的表，标出哪些改动是**机制性**的（解码器子类、transform 类型）、哪些是**命名性**的（data_root、save_pred_qa_dir、load_from 路径）。
3. 核对四个 `save_pred_qa_dir` 两两不同；核对 Reason3D 训练管线用的是 `QA_Generation_Reason3D_Test`（train 的 by_scene JSON）。

**需要观察的现象**：维度一的 diff 只有 4 处左右（type / max_point_tokens+save_dir+image_dir / num_qa / load_from）；维度二的 diff 集中在数据管线与路径名；两份 image 配置的 decoder 段逐字相同。

**预期结果**：ScanRefer 3D vs 3D+2D 的真实 diff 为——`type: Eval_Text → Eval_MultiImg`、`max_point_tokens: 3000 → 2000`、`save_pred_qa_dir: pred_qa_scanrefer(_image)`、新增 `image_dir='data/scannet/scannet_20_img'`、训练 `num_qa: 10 → 5`、删除 `load_from`。本讲 4.4.3（a）内嵌的 diff 输出即真实运行结果，可直接对照（本实践在仓库内已实际执行验证）。

**延伸**（可选，需 u1-l2 环境，零 GPU 零数据）：

```bash
# 示例代码：零 GPU 干跑，核对配置解析后的关键字段（非项目原有命令）
PYTHONPATH=. python -c "
from mmengine.config import Config
for f in ['configs/ground3dlmm_eval_scanrefer.py',
          'configs/ground3dlmm_eval_scanrefer_image.py']:
    cfg = Config.fromfile(f)
    d = cfg.model.decoder
    print(f.split('/')[-1], d.type, d.lora_r, d.max_point_tokens, d.save_pred_qa_dir)
"
```

预期打印两行：`..._scanrefer.py Grounded_Decoder_Eval_Text 32 3000 pred_qa_scanrefer` 与 `..._scanrefer_image.py Grounded_Decoder_Eval_MultiImg 32 2000 pred_qa_scanrefer_image`。**待本地验证**（字段值本身可直接从配置文件源码读出核对）。

#### 4.4.5 小练习与答案

**练习 1**：为什么迁移评估要拆 `Eval_Text` / `Eval_MultiImg` 两个解码器子类，而 Ground3D 内部评估一个 `Grounded_Decoder_Eval` 就够？

**答案**：内部数据的样本 ID 是 `场景_frame_序号`，父类按 `_frame_` 切出帧号查单帧图像，joint/3d 差异只是「有没有这张图」（`image_dir=None` 时 image 条目被过滤，u4-l6）。迁移数据是裸场景 ID：3D 行根本没有帧号可查，3D+2D 行则要吃整个图像目录——两种情况父类逻辑都不适用，只能各自重写消息组装（`_process_single_turn_qa`）与场景循环（`infer_reason_eval`）。

**练习 2**：3D+2D 行为什么把 `max_point_tokens` 从 3000 降到 2000？

**答案**：每场景多了 20 个 image 条目，经 `max_pixels=448*448` 钳制后仍占用可观的视觉 token 预算；降低点 token 上限是为控制单条 QA 的总序列长度（推理时每条 QA 前缀都要完整过一遍 4B 语言塔，长度直接乘以 QA 数就是显存与时间）。

**练习 3**：如果你把 ScanRefer 3D 的 checkpoint 用到 `ground3dlmm_eval_reason3d.py` 上跑，最可能发生什么？

**答案**：不是显式的报错，而是静默的错配：`load_from` 非严格加载（u5-l3/u6-l2），Reason3D 配置的 pkl/标注/transform 都按 Reason3D 组织，而 checkpoint 权重是在 ScanRefer 上微调的——评估能跑完但数字无意义；若 rank/参数名失配，adapter 还会被 `strict=False` 静默丢弃、模型退回基座 Qwen。正确做法是按 README 表格把 config 与 checkpoint 一一配对。

## 5. 综合实践

**任务：为「ScanRefer 3D+2D」这一行做一次完整的迁移就绪检查（dry run），产出一份可勾选的 checklist。** 这个任务贯穿本讲全部四个模块：转换链（4.1）→ GT 与模板（4.2）→ 图像构建（4.3）→ 配置与启动（4.4）。

按顺序完成并勾选（前三步纯只读/离线，现在就能做；后两步需数据与 u1-l2 环境）：

1. **转换链核对**（对应 4.1）：对照 [docs/DATA_SCANREFER_REASON3D.md:95-112](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/DATA_SCANREFER_REASON3D.md#L95-L112) 的目录树，列出 `oneformer3d_scanrefer/` 下必须存在的 4 个 bin 目录、2 个 pkl、2 个 by_scene JSON；确认 `scanrefer_infos_val.pkl` 的 `data_list[0]` 含裸文件名 `lidar_path`/`super_pts_path` 与 `qa_data` 列表（用 4.1.4 的检查脚本）。
2. **QA 模板核对**（对应 4.2）：确认 `configs/ground3dlmm_eval_scanrefer_image.py` 的 test_pipeline（[L178-184](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_scanrefer_image.py#L178-L184)）用的是 val 的 by_scene JSON、`AddSuperPointAnnotations_ScanRefer` 在 `QA_Generation_ScanRefer_Test` **之前**（顺序错了 `gt_sp_masks_ins` 不存在，管线直接崩）。
3. **配置 diff 核对**（对应 4.4）：跑 4.4.4 的三个 diff，确认 image 行的四个机制/命名改动齐全，且 `save_pred_qa_dir='pred_qa_scanrefer_image'` 不与 3D 行同名。
4. **图像构建**（对应 4.3，需 ScanNet 原始数据）：解码 `.sens` 后运行 `python tools/build_scene_images.py --pkl data/scannet/oneformer3d_scanrefer/scanrefer_infos_val.pkl --raw_root $SCANNET_RAW --out data/scannet/scannet_20_img`，观察每个场景打印的 `N=帧数 -> 20 frame(s)` 与跳过数；抽一个场景数一下 jpg 是否恰为 20 张、文件名与 4.3.4 推导的帧号一致。
5. **启动评估**（需 checkpoint，按 [docs:124-137](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/docs/DATA_SCANREFER_REASON3D.md#L124-L137)）：`bash tools/dist_test.sh configs/ground3dlmm_eval_scanrefer_image.py <Ground3D-LMM-ScanRefer-4B-Joint>/pytorch_model.pth 4`，关注三件套指标 `QA Seg mIoU` / `Precision_quarter` / `Precision_half`，并核对 `env_cfg.dist_cfg.timeout=7200` 未被删。

**验收标准**：checklist 五节全部可勾选；第 5 步的 mIoU 落在 41.30 ± 1.5 内即视为复现成功（方差说明见 docs）。第 1-3 步的预期结果在对应小节均已给出；第 4-5 步**待本地验证**。

## 6. 本讲小结

- **转换链**：两个迁移基准共用 `_ScanNetSceneGroundingData` 模板基类——以 by_scene 标注 JSON 的场景键为权威，复用 `scannet_instance_data` 基座，`qa_data` 整段原样进 pkl；两个子类只差 `_instance_mask` 钩子（reason.pth[4] vs ins_label.npy）；v2 更新借用 `'reason3d'` 例程并保留 `super_pts_path`/`qa_data` 两个编外键。
- **粒度落到索引上**：ScanRefer 是实例级（GT 行取 `gt_sp_masks_ins[object_id]`、答案回显描述、`<sentence>` 触发），Reason3D 是语义级（GT 行取 `gt_sp_masks_sem[object_id]`、答案说类别名、`<name>` 触发）；GT 缺失一律 NaN 整行占位，由指标侧行和判据自动剔除。
- **20 视图是确定性纯函数**：`linspace(0, N-1, 20).astype(int)` 向零截断、末项恒为 N-1、不足全取——不可再分发的数据因此可以被任何人逐位一致地重建。
- **2×2 配置矩阵**：3D→3D+2D 只有四处实质改动（解码器子类 `Eval_Text`→`Eval_MultiImg`、max_point_tokens 3000→2000、save_pred_qa_dir 换名、删 load_from）；基线 38.72 / 41.30 / 36.35 / 41.29，2D 视图稳定涨点。
- **避坑清单**：`'scanrefer'` 不是合法转换 key；`reason3d_file` 参数加载后从未使用（真数据来自 pkl 的 qa_data）；迁移配置 LoRA 全是 r=32，u6-l2 的 rank 探测规则不适用；四路输出目录必须隔离；复现容忍 ±1~1.5 点方差，别删 NCCL timeout=7200。

## 7. 下一步学习建议

本讲是「拿现成基准复现」；下一讲 **u7-l2《二次开发：新增子任务、数据集与替换底座》** 把本讲反过来看——**迁移本身就是一个完整的「接入新数据集」案例**：converter（`_ScanNetSceneGroundingData`）、Dataset（`ScanNetUnifiedSegDataset`）、transform（四个迁移专用类）正是新增数据集三件套的现成模板。建议：

1. 先读 u7-l2，把本讲 4.1/4.2 的组件映射到「新增一个数据集要写哪几个类」的清单上；
2. 想继续读源码的话，重点对比 `Grounded_Decoder_Eval`（父类，query_decoder.py:1982 起）与两个迁移子类的 `_process_single_turn_qa`，体会「继承点选在哪里」——这直接决定你新增模态/输入形式时要动多少代码；
3. 最后用 u7-l3《架构取舍与总结》收束全套手册，回看「超点 token 化、`<SEG>` 查询复用、LoRA 微调」在迁移场景下是否依然成立（本讲的 2×2 基线表就是证据之一）。
