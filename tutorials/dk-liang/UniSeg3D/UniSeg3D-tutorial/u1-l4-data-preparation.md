# 数据准备：从 ScanNet 原始数据到训练输入

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立跑通 ScanNet v2 的两级数据预处理：`data/scannet/batch_load_scannet_data.py`（解析原始场景）→ `tools/create_data.py`（生成训练用 bin 文件）。
2. 说清 `points/`、`instance_mask/`、`semantic_mask/`、`super_points/` 四类 `.bin` 文件各自的来源、dtype 和语义（谁生成的、装的是什么、被谁消费）。
3. 理解为什么 `uniseg3d_infos_train.pkl` / `uniseg3d_infos_val.pkl` 必须从官方 Release 下载而不能由本仓库脚本直接生成——它们额外包含 ScanRefer 文本、OpenCLIP 分词结果和 SAM3D 伪掩码。
4. 理解 `scannet_cls_embedding.pth` 类别嵌入文件的维度含义，以及它在模型侧（球面分类头）被如何加载。

本讲是第一单元的收官：u1-l1 认识了「超点」这个核心概念，u1-l2 装好了环境，u1-l3 画好了代码地图，本讲把「模型吃什么」彻底搞清楚。

## 2. 前置知识

### 2.1 ScanNet v2 场景里有哪些文件

ScanNet v2 是最常用的室内 3D 场景数据集，每个场景（scene）是 `data/scannet/scans/` 下的一个目录，训练 UniSeg3D 只用到其中 4 类文件：

| 文件（以 `scene0000_00` 为例） | 内容 |
| --- | --- |
| `scene0000_00_vh_clean_2.ply` | 网格（mesh）：顶点带 XYZ + RGB，是点云的来源 |
| `scene0000_00_vh_clean_2.0.010000.segs.json` | **过分割**结果：把每个顶点分到一个「小片段」（segment） |
| `scene0000_00.aggregation.json` | **聚合**标注：把若干小片段聚成一个物体实例，并给出类别名 |
| `scene0000_00.txt` | 元信息，其中 `axisAlignment` 行是 4×4 坐标对齐矩阵 |

理解 ScanNet 标注的「两段式」结构很关键：**segs.json 说「每个顶点属于哪个片段」，aggregation.json 说「每个片段属于哪个物体」**。把两者连起来，才能得到「每个顶点属于哪个物体实例、什么类别」——这正是逐点标注的来源。

ScanNet 需要在官网签署协议后才能下载，具体方式见 [ScanNet 官方仓库](https://github.com/ScanNet/ScanNet)。

### 2.2 `.bin` 裸数组与 `.pkl` 序列化

- 本讲出现的 `.bin` 文件**不是**标准格式（没有文件头），只是 `numpy.tofile()` 直接落盘的**裸字节数组**。读取时必须自己指定 dtype 和形状，例如 `np.fromfile(path, dtype=np.float32).reshape(-1, 6)`。
- `.pkl` 是 Python pickle 序列化文件。`uniseg3d_infos_train.pkl` 反序列化后是一个 `list[dict]`，每个 dict 描述一个场景（路径、标注、文本提示等），mmdet3d 称之为 **infos**。

### 2.3 超点、segmentator、SAM3D、ScanRefer（一句话回顾）

- **超点（superpoint）**：把点云过分割成数百上千个小块，每块内点属于同一物体表面的概率极高。u1-l1 讲过「超点即查询」，本讲讲超点标签文件是怎么来的。
- **segmentator**：u1-l2 安装的 C++ 扩展库，输入 mesh，输出每个顶点的超点编号，本讲用它生成 `super_points/*.bin`。
- **ScanRefer**：ScanNet 场景的「指代描述」数据集，每个描述句对应场景中一个物体实例，用于指代分割任务的文本提示。
- **SAM3D**：SegmentAnything3D，在 3D 点云上做类无关分割的模型；UniSeg3D 用它的输出作为**伪掩码**（pseudo mask）监督开放词汇能力。
- **OpenCLIP**：图文对齐的预训练模型，u1-l3 提到本仓库内嵌了 `open_clip/` 目录；这里用它把类别名/描述句变成「分词结果」（token）。

### 2.4 nyu40id 与类别映射

ScanNet 的原始类别名（`raw_category`，如 `wall`、`chair`）要映射成整数 id 才能训练。常用映射目标是 **nyu40id**（40 类体系，如 wall→1、floor→2、chair→5），映射表就是 `meta_data/scannetv2-labels.combined.tsv`（制表符分隔，表头含 `raw_category`、`nyu40id`、`id` 等列）。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `README.md` | 数据准备步骤与目录结构的权威说明（L96-L151） |
| `data/scannet/batch_load_scannet_data.py` | **第一级**：批量解析所有场景，产出 `scannet_instance_data/*.npy`（含超点） |
| `data/scannet/load_scannet_data.py` | 单场景解析核心：`export()` 函数把 4 个原始文件变成顶点/标签/框；也提供单场景 CLI |
| `data/scannet/scannet_utils.py` | 底层工具：读 tsv 标签映射、读 ply 顶点 |
| `tools/create_data.py` | **第二级**入口：命令行解析后调用 `indoor_converter` |
| `tools/indoor_converter.py` | 按 train/val/test 三个 split 构建 `ScanNetData` 并调用 `get_infos` |
| `tools/scannet_data_utils.py` | `ScanNetData.get_infos()`：把 npy 转成四个 `.bin` 目录，并在内存中组装 infos dict |
| `meta_data/`（数据文件，非代码） | split 清单（`scannet_train.txt` 1513 行、`scannetv2_train/val/test.txt` 1201/312/100 行）与标签映射 tsv |

消费端（本讲只看不精读，后续单元展开）：

| 文件 | 消费什么 |
| --- | --- |
| `uniseg3d/scannet_dataset.py` | 读 infos pkl，拼接 `super_pts_path` |
| `uniseg3d/loading.py` | 按 `int64` 读 `super_points/*.bin` |
| `uniseg3d/transforms_3d.py` | 读 `text_info`（ScanRefer + token）、`sam3d_pseudo_sp_masks`（SAM3D 伪掩码） |
| `uniseg3d/query_decoder.py` | `torch.load` 加载 `scannet_cls_embedding.pth` |

## 4. 核心概念与源码讲解

### 4.1 模块一：batch_load_scannet_data.py 主流程

#### 4.1.1 概念说明

这是数据准备的第一级：**遍历所有场景，把 ScanNet 原始的 4 类文件解析成统一的 npy 中间产物**，存进 `data/scannet/scannet_instance_data/`。它在 mmdet3d/votenet 经典脚本的基础上加了一个关键步骤——用 segmentator 对 mesh 做过分割，生成超点标签 `_sp_label.npy`。没有这一步，就没有 UniSeg3D 的「超点即查询」。

#### 4.1.2 核心流程

```text
main()
 ├─ batch_export(train: scans/ 目录, scannet_train.txt 1513 个场景, test_mode=False)
 │    对每个 scene：
 │      ├─ 已存在 {scene}_vert.npy ？ → 跳过（断点续跑）
 │      ├─ export_one_scan():
 │      │    ├─ 拼 4 个输入文件路径（ply / aggregation / segs / txt）
 │      │    ├─ 调 load_scannet_data.export() → 顶点/语义标签/实例标签/框/对齐矩阵
 │      │    ├─ 过滤 don't-care 类别的框
 │      │    ├─ （可选）按 max_num_point 随机下采样
 │      │    ├─ open3d 重读 mesh → segmentator.segment_mesh → 超点标签
 │      │    └─ np.save 保存 7 类 npy
 │      └─ 单个场景失败只打印，不中断整体
 └─ batch_export(test: scans_test/ 目录, test_mode=True)
      （test 集无标注，只保存顶点和超点；scans_test 不存在则直接返回）
```

#### 4.1.3 源码精读

**入口与默认参数**：所有路径都有合理默认值，所以 README 里直接 `python batch_load_scannet_data.py` 不带参数即可。[data/scannet/batch_load_scannet_data.py:135-167](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/data/scannet/batch_load_scannet_data.py#L135-L167) 定义了 `--max_num_point`（默认 None，即**不下采样**）、`--output_folder`（默认 `./scannet_instance_data`）、`--train_scannet_dir`/`--test_scannet_dir`（默认 `scans`/`scans_test`）、`--label_map_file`、`--train_scan_names_file`/`--test_scan_names_file`（默认 `meta_data/scannet_train.txt`/`meta_data/scannetv2_test.txt`）和 `--scannet200` 开关。[data/scannet/batch_load_scannet_data.py:168-183](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/data/scannet/batch_load_scannet_data.py#L168-L183) 中 `main()` 依次对 train（`test_mode=False`）和 test（`test_mode=True`）各调用一次 `batch_export`。

**断点续跑与容错**：[data/scannet/batch_load_scannet_data.py:117-131](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/data/scannet/batch_load_scannet_data.py#L117-L131) 逐行读取场景名清单；若 `{scene}_vert.npy` 已存在则跳过；`export_one_scan` 被 `try/except` 包住，单个场景失败只打印 `Failed export scan`。这意味着**中断后重跑是安全的**，但也意味着**要检查日志确认没有场景被静默跳过**。

**单场景导出**：[data/scannet/batch_load_scannet_data.py:35-52](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/data/scannet/batch_load_scannet_data.py#L35-L52) 按命名规则拼出 4 个输入文件路径，调用下一模块的 `export()` 拿到 7 个返回值。

**类别过滤**：[data/scannet/batch_load_scannet_data.py:23-31](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/data/scannet/batch_load_scannet_data.py#L23-L31) 定义了两组「关心的物体类别 id」：`SCANNET_OBJ_CLASS_IDS`（18 个，ScanNet20 的 things 类）和 `SCANNET200_OBJ_CLASS_IDS`（200 个）。[data/scannet/batch_load_scannet_data.py:67-71](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/data/scannet/batch_load_scannet_data.py#L67-L71) 用 `np.in1d` 只保留类别 id 在清单内的检测框（注意：**只过滤框，不过滤逐点标签**；`DONOTCARE_CLASS_IDS` 为空数组，所以 L55-L58 的点过滤实际不删任何点）。

**超点生成（本模块的灵魂）**：[data/scannet/batch_load_scannet_data.py:84-89](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/data/scannet/batch_load_scannet_data.py#L84-L89) 用 open3d **重新读一遍完整 mesh**（顶点 + 三角面片），转成 torch 张量后调用 `segmentator.segment_mesh(vertices, faces)`，得到每个**原始顶点**的超点编号，保存为 `{scene}_sp_label.npy`。

**保存清单**：[data/scannet/batch_load_scannet_data.py:89-100](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/data/scannet/batch_load_scannet_data.py#L89-L100) 保存的 npy 一览：

| npy | 内容 | shape / dtype |
| --- | --- | --- |
| `{scene}_vert.npy` | 顶点 XYZ+RGB | `(N, 6)` float32，RGB 为 0-255 |
| `{scene}_sp_label.npy` | 每顶点超点编号 | `(N,)` 整型 |
| `{scene}_sem_label.npy` | 每顶点语义标签（nyu40id） | `(N,)` uint32 |
| `{scene}_ins_label.npy` | 每顶点实例 id（0 = 未标注，1 起） | `(N,)` uint32 |
| `{scene}_unaligned_bbox.npy` / `_aligned_bbox.npy` | 每实例框：中心 3 + 尺寸 3 + 类别 1 | `(K, 7)` |
| `{scene}_axis_align_matrix.npy` | 4×4 坐标对齐矩阵 | `(4, 4)` |

注意 [data/scannet/batch_load_scannet_data.py:93](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/data/scannet/batch_load_scannet_data.py#L93) 的断言 `superpoints.shape == semantic_labels.shape`：超点是对**完整 mesh** 算的，若设置 `--max_num_point` 导致顶点被下采样（L74-L82 只采样 `mesh_vertices` 和标签，不采样超点），这个断言就会失败。所以本仓库的默认值 `None`（不下采样）是**唯一正确用法**。

#### 4.1.4 代码实践

**实践目标**：用 1-2 个场景跑通第一级解析，观察 `scannet_instance_data/` 的中间产物。

**操作步骤**（前置：已完成 u1-l2 的环境安装，含 segmentator；已按 ScanNet 官方流程下载至少 1 个场景到 `data/scannet/scans/scene0000_00/`）：

1. 在 `data/scannet/meta_data/` 下新建一个迷你清单文件 `scannet_train_mini.txt`，内容一行一个场景名，例如：
   ```text
   scene0000_00
   scene0000_01
   ```
2. 运行（`cd data/scannet` 后）：
   ```bash
   python batch_load_scannet_data.py \
       --train_scan_names_file meta_data/scannet_train_mini.txt
   ```
   不带 `scans_test` 也没关系：`batch_export` 在 test 模式下发现目录不存在会直接返回（[data/scannet/batch_load_scannet_data.py:110-112](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/data/scannet/batch_load_scannet_data.py#L110-L112)）。
3. 用 python 检查产物（**示例代码**）：
   ```python
   import numpy as np
   sid = 'scene0000_00'
   base = f'scannet_instance_data/{sid}'
   vert = np.load(f'{base}_vert.npy')
   sp   = np.load(f'{base}_sp_label.npy')
   sem  = np.load(f'{base}_sem_label.npy')
   ins  = np.load(f'{base}_ins_label.npy')
   print(vert.shape, vert.dtype)        # (N, 6) float32
   print(sp.shape, sp.max() + 1)        # 超点个数 = max+1
   print(np.unique(sem)[:10], np.unique(ins))
   ```

**需要观察的现象**：终端按场景打印 `Num of instances: xx` 与 `Num of care instances: xx`；四个逐点数组长度完全一致；`sem` 的取值落在 nyu40 id 集合（如 1,2,3,...,39）内；`ins` 含 0（未标注）且最小非零值为 1。

**预期结果**：`scannet_instance_data/` 下每个场景 7 个 npy（test 场景只有 2 个），形状满足上表。若 segmentator 报 `undefined symbol` 之类错误，回到 u1-l2 检查其符号链接安装是否完好。具体数值因场景而异，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `main()` 要调用两次 `batch_export`？两次调用的关键差异是什么？

> 答案：第一次处理 `scans/`（train+val，`test_mode=False`），会解析标注并保存全部 7 类 npy；第二次处理 `scans_test/`（`test_mode=True`），test 集没有标注，只保存 `_vert.npy` 和 `_sp_label.npy`，且若目录不存在直接返回。差异体现在 `export_one_scan` 的 `test_mode` 分支（L54、L92）和 `export()` 内部（L132-L158）。

**练习 2**：如果给 `--max_num_point 100000`，哪个断言会失败？为什么？

> 答案：[batch_load_scannet_data.py:93](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/data/scannet/batch_load_scannet_data.py#L93) 的 `assert superpoints.shape == semantic_labels.shape`。因为下采样只作用于 `mesh_vertices/semantic_labels/instance_labels`（L74-L82），而超点标签来自对**完整 mesh** 的 `segment_mesh`（L84-L87），长度仍是原始 N，两者不一致。

**练习 3**：`SCANNET_OBJ_CLASS_IDS` 过滤的是什么？为什么逐点语义标签不需要同样过滤？

> 答案：它过滤的是**检测框**（unaligned/aligned bboxes，L67-L70），把 wall/floor 等 stuff 类或其他不关心类的框删掉，供依赖框标注的任务使用。逐点语义标签保留全部类别信息，语义分割任务（含 wall/floor）和后续 `AddSuperPointAnnotations`（u3-l3）需要完整的逐点标注。

### 4.2 模块二：load_scannet_data.py 与 scannet_utils.py 单场景解析

#### 4.2.1 概念说明

`export()` 是整个数据准备的**原子操作**：输入一个场景的 4 个原始文件，输出「顶点 + 逐点语义 + 逐点实例 + 框 + 对齐矩阵」。它解决的正是 2.1 节所说的两段式标注拼接问题：**segs.json（顶点→片段）× aggregation.json（片段→实例/类别）⇒ 顶点级标注**。

#### 4.2.2 核心流程

```text
export(mesh, agg, segs, meta, label_map, ...):
 1. read_label_mapping(tsv)           # raw_category → nyu40id（或 id）
 2. read_mesh_vertices_rgb(ply)       # (N, 6) float32, RGB 0-255
 3. 从 meta txt 解析 axisAlignment     # 4×4 矩阵 A（无则单位阵）
 4. 顶点全局对齐: p_aligned = [p,1] @ Aᵀ
 5. read_aggregation(agg)             # object_id(+1) → 片段列表; label → 片段列表
 6. read_segmentation(segs)           # 片段 id → 顶点下标列表
 7. 对每个 (label, segs)：label_ids[verts] = nyu40id   # 逐点语义
 8. 对每个 (object_id, segs)：instance_ids[verts] = object_id  # 逐点实例
 9. extract_bbox() × 2                # 未对齐/已对齐两组框
10. 返回 7 元组
```

其中第 4 步的对齐用行向量形式实现（代码 L125-L127）：

\[ \mathbf{P}_{aligned} = \begin{bmatrix}\mathbf{p} & 1\end{bmatrix}_{N\times 4} \cdot \mathbf{A}^{\top} \]

即齐次坐标右乘转置矩阵，等价于对每个点做 \( \mathbf{A}\,[\mathbf{p},1]^{\top} \) 的列向量变换。

#### 4.2.3 源码精读

**读聚合标注**：[data/scannet/load_scannet_data.py:21-38](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/data/scannet/load_scannet_data.py#L21-L38) 遍历 `data['segGroups']`，建立两个映射：`object_id_to_segs`（实例→片段，注意 `objectId + 1`，**实例 id 从 1 开始**，0 留给未标注）和 `label_to_segs`（类别名字符串→片段）。

**读过分割**：[data/scannet/load_scannet_data.py:41-53](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/data/scannet/load_scannet_data.py#L41-L53) 读 `segIndices`，把顶点下标 `i` 归入片段 `seg_id`，返回 `seg_to_verts` 和总顶点数 `num_verts`。

**拼逐点标注**：[data/scannet/load_scannet_data.py:131-149](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/data/scannet/load_scannet_data.py#L131-L149) 先按 `label_to_segs` 填 `label_ids`（uint32，长度 N），再按 `object_id_to_segs` 填 `instance_ids`（uint32，0 = 未标注），并顺带记录 `object_id_to_label_id`（取该实例第一个顶点的语义标签）。

**抽框**：[data/scannet/load_scannet_data.py:56-71](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/data/scannet/load_scannet_data.py#L56-L71) 对每个实例取其点集的包围盒：

\[ \text{center} = \frac{x_{min}+x_{max}}{2},\quad \text{size} = x_{max}-x_{min} \]

拼成 `[center(3), size(3), label_id(1)]` 共 7 维，按 `obj_id - 1` 放行（注释提醒：这假设实例 id 恰好是连续的 1..K）。

**主函数 export()**：[data/scannet/load_scannet_data.py:74-107](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/data/scannet/load_scannet_data.py#L74-L107) 是流程图第 1-3 步：`--scannet200` 时映射目标是 `id` 列，否则 `nyu40id` 列（README 的标准命令不带该 flag，即 nyu40 体系，与 4.1 的类别过滤和配置文件里的 `sem_mapping` 一致）；[data/scannet/load_scannet_data.py:112-129](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/data/scannet/load_scannet_data.py#L112-L129) 逐行扫 meta txt 找 `axisAlignment`（test 集没有，默认单位阵），再做齐次对齐；[data/scannet/load_scannet_data.py:150-153](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/data/scannet/load_scannet_data.py#L150-L153) 对未对齐/已对齐两套顶点各抽一次框；[data/scannet/load_scannet_data.py:170-171](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/data/scannet/load_scannet_data.py#L170-L171) 返回 7 元组。

**底层工具**：[data/scannet/scannet_utils.py:33-44](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/data/scannet/scannet_utils.py#L33-L44) 的 `read_label_mapping` 用 `csv.DictReader` 按制表符读 tsv，建 `raw_category → int(nyu40id)` 字典；[data/scannet/scannet_utils.py:67-87](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/data/scannet/scannet_utils.py#L67-L87) 的 `read_mesh_vertices_rgb` 用 `plyfile` 读 mesh 顶点的 x/y/z/red/green/blue 六个属性，得到 `(N, 6)` float32（**RGB 保持 0-255**，归一化留到训练管线的 `NormalizePointsColor_` 做，见 u3-l2）。

**单场景 CLI**：[data/scannet/load_scannet_data.py:174-201](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/data/scannet/load_scannet_data.py#L174-L201) 让你可以独立对一个场景跑 `export` 并落盘 npy（不经过 batch 脚本、也不需要 segmentator）。

#### 4.2.4 代码实践

**实践目标**：绕开批量脚本，单场景直跑 `export()`，验证「两段式标注 → 逐点标注」的拼接结果。

**操作步骤**（`cd data/scannet` 后；需要 `pip install plyfile`，它是 `scannet_utils.py` 的直接依赖）：

```bash
python load_scannet_data.py \
    --scan_path scans/scene0000_00 \
    --output_file /tmp/scene0000_00 \
    --label_map_file meta_data/scannetv2-labels.combined.tsv
```

然后（**示例代码**）：

```python
import numpy as np
sem = np.load('/tmp/scene0000_00_sem_label.npy')
ins = np.load('/tmp/scene0000_00_ins_label.npy')
print(sem.shape, sem.dtype, sorted(np.unique(sem))[:8])
print(ins.shape, ins.dtype, sorted(np.unique(ins))[:8])
# 验证「实例 → 语义」一致性：每个实例内的点语义标签应唯一
for i in np.unique(ins)[1:6]:
    print(i, np.unique(sem[ins == i]))
```

**需要观察的现象**：`sem` 是 nyu40 id 的子集（wall=1、floor=2 等）；`ins` 从 0 开始、非零值连续；每个实例内部的语义标签基本唯一（个别噪声实例可能多于一个）。

**预期结果**：在 `/tmp/` 下得到 6 类 npy（单场景 CLI 不产生超点标签）。同一场景在 4.1.4 与本实践中产出的 `_vert/_sem/_ins` 应逐位一致（batch 脚本就是调同一个 `export`）。具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`instance_ids` 为什么要 `+1`？0 有什么含义？

> 答案：`read_aggregation` 里 `objectId + 1`（L30）。0 被保留为「未标注」（L142-L143 初始化全 0），非零实例 id 从 1 开始；下游 `AddSuperPointAnnotations` 等以「非零 = 有效实例」为前提。

**练习 2**：`unaligned_bbox` 和 `aligned_bbox` 差在哪一步？各有什么用？

> 答案：对齐版是用 `axis_align_matrix` 变换后的顶点（L125-L129）抽的框，未对齐版直接用原始顶点。两者的差体现了 ScanNet 各场景坐标系朝向不一致的问题；检测类任务通常用对齐框，UniSeg3D 的分割主线不依赖框，但 infos 里仍保留（4.3.3 的 annotations 部分）。

**练习 3**：`read_label_mapping` 不传 `--scannet200` 时映射到 `nyu40id`，这和配置文件有什么呼应？

> 答案：主配置的 `sem_mapping = [1, 2, 3, ..., 39]`（configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:281-283）正是 nyu40 id 列表：语义标签全集 20 类（含 wall/floor），实例类去掉前两个 stuff 类。若数据侧改用 `--scannet200` 的 `id` 映射，逐点标签值域就变成 1-200 区间的原始类别 id，与该配置不再匹配。

### 4.3 模块三：create_data.py / indoor_converter.py / scannet_data_utils.py 生成 bin 与 infos

#### 4.3.1 概念说明

第二级做两件事：**(a) 把 npy 中间产物转成训练直接消费的四个 `.bin` 目录；(b) 在内存里组装 infos（每个场景一个 dict，记录路径与标注）**。但有一个**极其重要的事实**：本仓库这版 `indoor_converter.py` 把所有 `mmengine.dump(...)` 都注释掉了——也就是说，**跑完 `create_data.py` 你只会得到四个 bin 目录，不会得到任何 pkl**。`uniseg3d_infos_train/val.pkl` 必须从官方 Release 下载（原因见 4.4）。

#### 4.3.2 核心流程

```text
python tools/create_data.py scannet --root-path ./data/scannet --out-dir ./data/scannet --extra-tag scannet
 └─ scannet_data_prep() → create_indoor_info_file('scannet')
     ├─ ScanNetData(split='train'/'val'/'test')   # 读 meta_data/scannetv2_{split}.txt
     └─ 每个 split 调 get_infos(workers)：
         对每个场景 process_single_scene()：
           ├─ load {id}_vert.npy      → tofile → points/{id}.bin       （副作用建目录）
           ├─ load {id}_sp_label.npy  → tofile → super_points/{id}.bin
           ├─ load {id}_ins_label.npy → int64 → tofile → instance_mask/{id}.bin
           ├─ load {id}_sem_label.npy → int64 → tofile → semantic_mask/{id}.bin
           ├─ 组装 annotations（框、类别名、axis_align_matrix）
           └─ 返回 info dict
     （mmengine.dump(infos, ...) 全部被注释 → 不落盘 pkl）
```

#### 4.3.3 源码精读

**薄入口**：[tools/create_data.py:9-25](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/create_data.py#L9-L25) 的 `scannet_data_prep` 只转调 `create_indoor_info_file`；注意 L20-L25 那段被注释的 `update_pkl_infos`——旧版 mmdet3d 流程里还有一步「升级 pkl 到 v2 格式」，这里一并省略。[tools/create_data.py:46-57](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/create_data.py#L46-L57) 是入口：`register_all_modules()` 注册 mmdet3d 全部模块，`dataset` 只接受 `scannet`/`scannet200`。

**split 构建**：[tools/indoor_converter.py:40-52](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/indoor_converter.py#L40-L52) 为 train/val/test 各建一个 `ScanNetData`（`--extra-tag scannet` 时 `scannet200=False`）。[tools/indoor_converter.py:54-67](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/indoor_converter.py#L54-L67) 依次调 `get_infos`（test 传 `has_label=False`），**每一处 `mmengine.dump` 和打印都被注释**——这就是「bin 会生成、pkl 不会生成」的直接证据。

**类别表与 split 文件**：[tools/scannet_data_utils.py:27-103](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/scannet_data_utils.py#L27-L103) 定义两套 `classes`/`cat_ids`（200 类与 18 类），[tools/scannet_data_utils.py:105-116](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/scannet_data_utils.py#L105-L116) 建 `cat2label`/`label2cat`/`cat_ids2class` 映射，并从 `meta_data/scannetv2_{split}.txt` 读场景清单（train 1201 / val 312 / test 100）。

**bin 生生的时刻（核心）**：[tools/scannet_data_utils.py:161-197](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/scannet_data_utils.py#L161-L197) 里 `process_single_scene` 先 `np.load` 顶点 npy，`mmengine.mkdir_or_exist` 建 `points/` 目录后 `points.tofile(...)` 落盘，并把相对路径写进 `info['pts_path']`；紧接着同样流程处理超点：`info['super_pts_path'] = 'super_points/{id}.bin'`。[tools/scannet_data_utils.py:213-241](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/scannet_data_utils.py#L213-L241) 处理两个标注 npy：**显式 `.astype(np.int64)`** 后 `tofile`，写入 `instance_mask/`、`semantic_mask/`，test 模式（`self.test_mode`）跳过。dtype 契约由此确定：

| bin 目录 | dtype | shape | 语义 |
| --- | --- | --- | --- |
| `points/{id}.bin` | float32 | `(N, 6)` | XYZ（米）+ RGB（0-255） |
| `super_points/{id}.bin` | int64 | `(N,)` | 每点超点编号，0 起，超点总数 = max+1 |
| `instance_mask/{id}.bin` | int64 | `(N,)` | 实例 id，0 = 未标注 |
| `semantic_mask/{id}.bin` | int64 | `(N,)` | nyu40 id |

其中 `super_points` 的 int64 契约由消费端 [uniseg3d/loading.py:32-45](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py#L32-L45) 反向锁定（`np.frombuffer(mask_bytes, dtype=np.int64)`）。

**annotations 组装**：[tools/scannet_data_utils.py:243-274](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/scannet_data_utils.py#L243-L274) 从 `_aligned_bbox.npy`/`_unaligned_bbox.npy` 读框，拆成 `location/dimensions/gt_boxes_upright_depth` 等字段，用 `cat_ids2class` 把 nyu40 id 转成连续 class 下标和类别名，外加 `axis_align_matrix`。

**并发**：[tools/scannet_data_utils.py:277-281](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/scannet_data_utils.py#L277-L281) 用 `ThreadPoolExecutor(num_workers)`（默认 4 线程）map 所有场景。注意异常会在 `list(infos)` 物化时才抛出——**任何一个场景缺 npy 都会让整体失败**。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：在**只有 1-2 个场景**的沙盒上完整跑通 `create_data.py`，验证四个 bin 目录的生成，而不是下载全量 1513 个场景。

**操作步骤**：

1. 建一个沙盒根目录 `data/scannet_mini/`，把 `meta_data/` 拷进去并改小三个清单：`scannetv2_train.txt`、`scannetv2_val.txt` 各留 1 个场景（如 `scene0000_00`、`scene0000_01`），`scannetv2_test.txt` 清空（test 为空列表时 `get_infos` 返回空表，不会报缺文件）。
2. 把 4.1.4 产出的 `scannet_instance_data/` 拷贝或软链到 `data/scannet_mini/` 下。
3. 从项目根目录运行：
   ```bash
   python tools/create_data.py scannet \
       --root-path ./data/scannet_mini \
       --out-dir ./data/scannet_mini \
       --extra-tag scannet
   ```
4. 检查 `data/scannet_mini/` 下是否出现 `points/ super_points/ instance_mask/ semantic_mask/` 四个目录，且每个只有 1-2 个 bin。

**需要观察的现象**：终端逐场景打印 `train sample_idx: scene0000_00` 等日志；命令正常退出且**没有生成任何 pkl 文件**（这验证了 4.3.1 的结论）。

**预期结果**：四个 bin 与 4.1.4 的 npy 一一对应；用 4.4.4 的读取脚本核对 dtype 与形状。若报 `FileNotFoundError: ..._aligned_bbox.npy`，说明该场景没经过 4.1 的 train 模式处理（val split 也需要带标注的 npy）。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`create_data.py` 跑完后 `data/scannet/` 里多了什么、少了什么？

> 答案：多了 `points/ super_points/ instance_mask/ semantic_mask/` 四个 bin 目录（由 `process_single_scene` 的 `tofile` 副作用产生）；**没有**多任何 pkl——`indoor_converter.py` L56-L67 的 `mmengine.dump` 全被注释，infos 只在内存里组装。

**练习 2**：为什么 `instance_mask` 和 `semantic_mask` 写 bin 前 `.astype(np.int64)`，而 `points` 不转？

> 答案：标签 npy 是 uint32（load_scannet_data.py:135/143），显式转成 int64 统一契约；顶点 npy 本来就是 float32（`read_mesh_vertices_rgb` 的产物），且训练管线按 float32 读取，无需转换。

**练习 3**：配置文件里 `data_prefix=dict(pts='points', pts_instance_mask='instance_mask', pts_semantic_mask='semantic_mask', sp_pts_mask='super_points')`（configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:140-144）和本模块的什么字符串对上了？

> 答案：和 `process_single_scene` 写入 info 的相对路径完全对齐：`info['pts_path']='points/{id}.bin'`、`info['pts_instance_mask_path']='instance_mask/{id}.bin'`、`info['pts_semantic_mask_path']='semantic_mask/{id}.bin'`、`info['super_pts_path']='super_points/{id}.bin'`。数据集类（u3-l1 的 `ScanNetUnifiedSegDataset.parse_data_info`）正是用 `data_prefix` 把这些相对路径拼成绝对路径。

### 4.4 模块四：官方发布文件的内容构成（两个 pkl + 类别嵌入）

#### 4.4.1 概念说明

README 要求下载三个文件：`uniseg3d_infos_train.pkl`、`uniseg3d_infos_val.pkl`、`scannet_cls_embedding.pth`（[README.md:111-126](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L111-L126)）。前两个替代了 4.3 中「被注释掉的 dump」：它们 = `create_data.py` 能生成的标准 ScanNet infos **+ 本仓库脚本生成不了的额外字段**（ScanRefer 文本、OpenCLIP 分词、train 集还有 SAM3D 伪掩码）。第三个是球面分类头的类别原型。

#### 4.4.2 核心流程

```text
uniseg3d_infos_train.pkl（每场景一个 dict）
 ├─ 标准字段：pts_path / super_pts_path / pts_instance_mask_path /
 │            pts_semantic_mask_path / annos / axis_align_matrix ...
 ├─ text_info: { "3": { text: [...描述句...],
 │                       text_token: [...OpenCLIP 分词...],
 │                       label_id: x, pts_id: ... }, ... }   ← ScanRefer
 └─ sam3d_pseudo_sp_masks: [超点级 bool 掩码, ...]            ← SAM3D（仅 train）

scannet_cls_embedding.pth
 └─ 三维张量 [C, P, D]（类别 × 每类原型 × 嵌入维度）
     模型侧追加 1 行 back_token → [C+1, P, D] → permute → [D, C+1, P]
```

#### 4.4.3 源码精读

**README 的内容声明**：[README.md:111-116](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L111-L116) 说明 train pkl 含四样东西（ScanNet 信息 / ScanRefer 文本 / OpenCLIP 分词 / SAM3D 伪掩码）；[README.md:118-122](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L118-L122) 说明 val pkl **不含**伪掩码（验证不需要开放词汇监督）；[README.md:124-126](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L124-L126) 说明类别嵌入用 OpenCLIP 处理得到。最终目录结构见 [README.md:128-151](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L128-L151)。

**text_info 的字段结构（从消费端反推）**：训练管线的 `TextPromptGeneration` 按 [uniseg3d/transforms_3d.py:29-49](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L29-L49) 消费它：`text_info` 是以**实例 id 字符串**（如 `'3'`）为 key 的 dict，每个值含 `text`（描述句列表）、`text_token`（与描述句对齐的分词结果列表）、`label_id`（类别下标）；测试版 `TextPromptTest` 还会用到 `pts_id`（[uniseg3d/transforms_3d.py:89-95](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L89-L95)）。分词在**数据准备阶段离线完成**并直接存进 pkl，训练时零开销——这就是「OpenCLIP 分词结果」字段的用途。

**SAM3D 伪掩码的用途**：`AddSuperPointAnnotations` 在 `merge_ov=True` 时读取 [uniseg3d/transforms_3d.py:385-390](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L385-L390) 的 `input_dict['sam3d_pseudo_sp_masks']`，把这些**超点级 bool 掩码**拼到 GT 实例掩码后面，并用 `is_novel` 向量标记哪些行是伪掩码（novel 类）——这正是 u5-l3 要精读的 base/novel 双路监督的数据来源。

**类别嵌入的加载与变形**：[uniseg3d/query_decoder.py:389-406](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L389-L406) 在 `sphere_cls=True` 时 `torch.load(vocabulary_cls_embedding_path)`：`size(1)` 记为 `_prototypes`（每类多个提示模板的原型数），`size(2)` 记为 `_dim`（嵌入维度）；随后在**第 0 维**拼接一个随机初始化、经分布式广播的 `back_token`（背景类原型），得到 `[C+1, P, D]`；最后 `permute(2, 0, 1)` 成 `[D, C+1, P]` 并注册为非持久化 buffer `cls_embed`。推理/训练时查询特征与这组原型做相似度即「球面分类」（细节在 u4-l4）。它的路径由配置 [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:66](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L66) 传入；两个 pkl 则分别作为 [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:252](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L252)（train）与 [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:261](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L261)（val/test）的 `ann_file`。

#### 4.4.4 代码实践

**实践目标**：亲手解剖三个官方文件，把 4.4.2 的结构图变成眼见为实。

**操作步骤**：按 README 链接下载三个文件放入 `data/scannet/`，然后（**示例代码**）：

```python
import pickle, torch

# 1) infos pkl
with open('data/scannet/uniseg3d_infos_train.pkl', 'rb') as f:
    infos = pickle.load(f)
print(type(infos), len(infos))            # list, 1201（训练集场景数）
info = infos[0]
print(sorted(info.keys()))                # 对照 4.4.2 的字段
print(list(info.get('text_info', {}))[:3])  # 实例 id 字符串
first = list(info['text_info'].values())[0]
print(first.keys(), first['text'][:1], first['text_token'][0].shape)
print(type(info.get('sam3d_pseudo_sp_masks', None)),
      info['sam3d_pseudo_sp_masks'][0].shape)  # 超点级掩码，bool

# 2) 类别嵌入
cls_embed = torch.load('data/scannet/scannet_cls_embedding.pth')
print(cls_embed.shape, cls_embed.dtype)   # [C, P, D]
```

**需要观察的现象**：train pkl 长度应为 1201（= `scannetv2_train.txt` 行数），val pkl 应为 312；`text_token` 是定长 token 序列；`sam3d_pseudo_sp_masks` 只在 train pkl 中出现；`cls_embed` 是三维张量，`D` 应与 OpenCLIP 文本塔输出维度一致（768 量级），`C`、`P` 的具体数值**待本地验证**。

**预期结果**：打印出的键集合覆盖 4.3.3 的标准字段 + `text_info`/`sam3d_pseudo_sp_masks`；这直接解释了「为什么 pkl 要官方发布」——`text_token` 与伪掩码的生成脚本不在本仓库内。

#### 4.4.5 小练习与答案

**练习 1**：为什么 val pkl 不含 `sam3d_pseudo_sp_masks`？

> 答案：伪掩码是**训练监督信号**（开放词汇的 novel 类监督，被 `merge_ov=True` 时的 `AddSuperPointAnnotations` 拼进 GT）；验证/测试走 `test_pipeline`，不做 GT 构造，伪掩码无用，所以 README L118-L122 声明 val pkl 只含 ScanNet 信息、ScanRefer 文本和分词。

**练习 2**：如果 `ann_file` 指向 4.3 自己生成的 pkl（假设你取消注释把 dump 打开），训练会在哪一步出问题？

> 答案：数据集加载本身能走（标准字段齐全），但一进入 train_pipeline 的 `TextPromptGeneration` 就会因 info 里没有 `text_info` 走空分支（transforms_3d.py:21-27 返回全空），指代任务监督消失；`merge_ov=True` 时 `AddSuperPointAnnotations` 读取 `sam3d_pseudo_sp_masks`（transforms_3d.py:386）会直接 `KeyError`。

**练习 3**：`scannet_cls_embedding.pth` 加载后为何要 append 一行 `back_token` 再 `permute(2,0,1)`？

> 答案：原始张量只覆盖 C 个前景类；分割还需要「背景/其他」类，`back_token` 补成 C+1 行（query_decoder.py:399-405）。`permute(2,0,1)` 把形状从 `[C+1, P, D]` 变为 `[D, C+1, P]`，方便后续按「特征维度 × 类别 × 原型」与查询特征做点积（u4-l4 的 `forward_logit`）。

## 5. 综合实践

**任务：在 1-2 个场景上走通「ScanNet 原始数据 → 四类 bin → 对照 README 核对」的完整链路。**

1. **准备**：完成 u1-l2 环境（重点验证 `import segmentator` 成功）；从 ScanNet 官方渠道下载 `scene0000_00`（有标注，train/val 用）放入 `data/scannet/scans/`。
2. **第一级**：按 4.1.4 用迷你清单跑 `batch_load_scannet_data.py`，确认 `scannet_instance_data/scene0000_00_*.npy` 齐全（7 个）。
3. **第二级**：按 4.3.4 的沙盒方案跑 `tools/create_data.py scannet --root-path ./data/scannet_mini --out-dir ./data/scannet_mini --extra-tag scannet`，得到四个 bin 目录。
4. **核验**（**示例代码**）：

   ```python
   import numpy as np
   root = 'data/scannet_mini'
   sid = 'scene0000_00'
   pts = np.fromfile(f'{root}/points/{sid}.bin', dtype=np.float32).reshape(-1, 6)
   sp  = np.fromfile(f'{root}/super_points/{sid}.bin', dtype=np.int64)
   ins = np.fromfile(f'{root}/instance_mask/{sid}.bin', dtype=np.int64)
   sem = np.fromfile(f'{root}/semantic_mask/{sid}.bin', dtype=np.int64)
   assert len(pts) == len(sp) == len(ins) == len(sem)
   print(f'N={len(pts)}, 超点数={sp.max()+1}, 实例数={len(np.unique(ins))-1}')
   print(f'XYZ 范围 {pts[:,:3].min(0)} ~ {pts[:,:3].max(0)}; '
         f'RGB 范围 {pts[:,3:].min()} ~ {pts[:,3:].max()}')
   print(f'语义 id {np.unique(sem)}; 实例 id {np.unique(ins)[:10]}')
   ```

5. **对照 README**：逐项核对 [README.md:128-151](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L128-L151) 的目录结构——`points/instance_mask/semantic_mask/super_points` 四个 bin 目录已就位；`uniseg3d_infos_train/val.pkl` 与 `scannet_cls_embedding.pth` 需从 Release 下载放入（全量版本覆盖 1201/312 个场景，你的迷你 bin 只是形状/类型验证）。
6. **记录**：把四个数组的 shape、dtype、取值范围整理成一张表，并注明「同一 N、超点数 = sp.max()+1、RGB ∈ [0,255]、语义 ∈ nyu40 id 集」这些不变量。这份表在 u3 系列读数据管线时会反复用到。

预期结果：所有断言通过；四个数组长度一致；XYZ 在米级范围（室内场景几米到十几米）；`sem` 不含 0 以外的异常值（0 = unlabeled）。若没有 ScanNet 下载权限，可先做 4.4.4 的 pkl 解剖作为替代实践。**待本地验证**。

## 6. 本讲小结

- 数据准备是**两级流水线**：`batch_load_scannet_data.py`（原始场景 → `scannet_instance_data/*.npy`，含 segmentator 超点）→ `create_data.py`（npy → `points/super_points/instance_mask/semantic_mask` 四类 bin + 内存中的 infos）。
- ScanNet 标注是两段式的：`segs.json`（顶点→片段）× `aggregation.json`（片段→实例/类别），`export()` 把它们拼成逐点语义/实例标签；实例 id 从 1 起，0 = 未标注。
- 四类 bin 的契约：points 是 `(N,6)` float32（XYZ 米 + RGB 0-255），super_points / instance_mask / semantic_mask 都是 `(N,)` int64；`super_points` 的读取契约由 `LoadAnnotations3D_` 的 `np.frombuffer(..., dtype=np.int64)` 锁定。
- **`create_data.py` 只产 bin 不产 pkl**（`indoor_converter.py` 的所有 `mmengine.dump` 被注释）；`uniseg3d_infos_train/val.pkl` 必须下载，因为它们额外携带 ScanRefer `text_info`（含离线 OpenCLIP 分词 `text_token`）和 SAM3D 伪掩码 `sam3d_pseudo_sp_masks`（仅 train）。
- `scannet_cls_embedding.pth` 是 `[C, P, D]` 的类别原型张量，模型侧 append 背景行后变形为 `[D, C+1, P]` 的 buffer，供球面分类使用。
- 两个易踩的坑：`--max_num_point` 会破坏「超点长度 = 顶点数」断言（不要用）；`create_data.py` 处理 val/test split 同样需要对应的 npy（沙盒实验时清空 test 清单即可绕过）。

## 7. 下一步学习建议

本讲结束后，磁盘上的训练输入已就绪，下一步进入**单元 3（数据流）**：

1. **u3-l1（数据集类）**：看 `ScanNetUnifiedSegDataset` 如何用 `ann_file='uniseg3d_infos_train.pkl'` 和 `data_prefix` 把本讲的产物变成 `data_info` dict——你会再次遇到 `super_pts_path`。
2. **u3-l2（加载与打包）**：`LoadAnnotations3D_` 按 int64 读 `super_points/*.bin` 的代码（本讲 loading.py:L32-L45）会在那里完整展开。
3. **u3-l3（超点标注构造）**：本讲埋下的 `sam3d_pseudo_sp_masks` / `is_novel` 将在 `AddSuperPointAnnotations` 中变成 base/novel 双路监督的 GT。
4. 若想加深对超点本身的理解，可对比阅读 segmentator 仓库（u1-l2 安装的那个）的 mesh 分割接口，理解 `segment_mesh(vertices, faces)` 输出编号的含义。
