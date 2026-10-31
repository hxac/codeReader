# Ground3D 数据集结构：从 npy 到 QA JSON

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 HuggingFace 上 `amolharsh/Ground3D_Dataset` 的两大目录（`processed_data` 与 `refined_qa_data`）分别存放什么、如何组织。
2. 列出每个场景帧的四类 `.npy` 文件（`vert` / `sp_label` / `part_label` / `object_label`）各自的含义、维度约定与对齐关系。
3. 描述三种 QA 层级（`part_qa` / `object_qa` / `multiturn_qa`）的 JSON 嵌套结构、9 类子任务名，以及转换代码在结构上做的第一道「清洗」。
4. 能够亲手下载标注子集、读懂一个场景帧的全部原始文件——为下一讲 `create_data.py` 的格式转换打下基础。

## 2. 前置知识

本讲是数据侧的第一讲，只需要入门层建立过的几个概念，这里先快速回顾：

- **点云（point cloud）**：一个 3D 场景用一大堆点表示，每个点有坐标 (X, Y, Z)。Ground3D 的点还带颜色 (R, G, B)，所以每点是 6 个数。
- **超点（superpoint）**：把空间上相邻、颜色相近的点聚成一小块，一块就是一个超点。一个场景有几万到几十万个点，但通常只有几百到几千个超点。模型真正「读」的是超点，不是每个点（这是 u1-l1 讲过的效率关键）。
- **接地标记**：训练数据里，回答文本用 `<p>某个物体/部件</p><SEG>` 这种写法标注「这一段文字对应哪块区域」；`<SEG>` 是特殊 token，它位置的隐状态会被复用为分割查询（u1-l1 讲过的 `<SEG>` 机制）。
- **帧（frame）与场景（scene）**：Ground3D 的训练单元是「单视角部分场景帧」，一个场景（如 `scene0000_00`）下有多帧（如 `frame_0`、`frame_1`……），帧的全局 ID 是 `<scene>_frame_<n>`。
- **task_level / task_type / sub_task 三层旋钮**：u1-l4 里我们用它们过滤评估任务，本讲你会看到这些名字在**原始数据**里是如何组织的——评估时的过滤字段并非凭空发明，而是沿着数据本身的层级设计的。

另外，本讲会频繁用到 `numpy` 读 `.npy` 文件、用 `json` 读标注。不需要安装整个项目环境也能完成本讲大部分实践（只需 `numpy` 与 `huggingface_hub`）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `README.md` | 数据集规模统计表、下载方式、HF 原始目录布局图、9 个子任务名列表 |
| `tools/part3d_glamm_data_utils.py` | **本讲主战场**：模块文档字符串完整描述了 HF 布局；`Part3dGlammData` 类的 `_scan_frame_ids` / `_frame_npy` / `_process_qa` 是「官方如何理解这套目录」的最权威证据 |
| `uniseg3d/transforms_3d.py` | 只做旁证：QA 条目里哪些字段来自原始 JSON、哪些是下游补加的 |
| `uniseg3d/loading.py` | 只做旁证：`.bin` 回读时的 dtype/维度约定，反推 npy 的形状 |

> 本讲聚焦「数据长什么样」；`create_data.py` 如何把 npy 转成 `.bin` + `infos.pkl` 是下一讲（u2-l2）的主题，这里只借 `_process_frame` 理解文件对应关系。

## 4. 核心概念与源码讲解

### 4.1 HF 数据集目录布局：三大块

#### 4.1.1 概念说明

Ground3D 数据集发布在 HuggingFace（`amolharsh/Ground3D_Dataset`），完整下载约 150 GB。它每个数据源（`scannet` / `scannetpp`）下分三大块：

1. **`processed_data/<场景>/`** —— 几何与标注数据：每帧四个 `.npy` 数组（点、超点、部件标签、物体标签）。这是体积的大头。
2. **`refined_qa_data/`** —— 问答标注：按 `part_qa` / `object_qa` / `multiturn_qa` 三个层级组织的 JSON 文件。
3. **`part_ground3d_{train,val}.txt`** —— 场景级的训练/验证划分清单。

一个关键事实：**标注（~3 GB）和几何数据（~150 GB）是可分离下载的**。README 的快速上手给出了只拉标注子集的方式，非常适合本讲的学习实践：

```python
# 示例代码（来自 README 快速上手）
from huggingface_hub import snapshot_download

# Annotations only (~3 GB) for exploration
data_root = snapshot_download(
    repo_id="amolharsh/Ground3D_Dataset",
    repo_type="dataset",
    allow_patterns=["*/refined_qa_data/*", "*/part_ground3d_*.txt", "README.md"],
)
```

参见 [README.md:63-75](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L63-L75)，这段代码演示了用 `allow_patterns` 只下载 `refined_qa_data` 与划分清单。

#### 4.1.2 核心流程

整套目录可以画成这样（对照 README 的布局图）：

```
data/scannet/                                  ← 每个数据源一个根目录
├── processed_data/<scene>/
│   ├── frame_<n>_vert.npy                     # 点云：XYZ+RGB，load_dim=6
│   ├── frame_<n>_sp_label.npy                 # 超点 id（每点一个整数）
│   ├── frame_<n>_part_label.npy               # 部件标签（每点一个整数）
│   └── frame_<n>_object_label.npy             # 物体标签（每点一个整数）
├── refined_qa_data/
│   ├── part_qa/<scene>/final_json_outputs/<n>_part_mapping.json
│   ├── object_qa/<scene>/final_json_outputs/<n>_object_mapping.json
│   └── multiturn_qa/<scene>/frame_<n>_multi_conv.json
└── part_ground3d_train.txt / part_ground3d_val.txt   # 场景划分清单
```

数据消费方理解这套布局的两条规则：

- **帧的发现规则**：一个「帧」当且仅当 `processed_data/<scene>/` 下存在 `frame_<n>_vert.npy`。以 `vert.npy` 为帧的存在标记，帧 ID 拼成 `<scene>_frame_<n>`。
- **划分的权威规则**：哪些场景属于 train / val，**只由 txt 清单说了算**，不由「磁盘上有什么」说了算。原因在源码注释里写得很清楚：`processed_data/` 里可能合法地存在没有任何 QA 的场景点云，如果按磁盘内容兜底，就会悄悄构建出和发布不同的划分。

#### 4.1.3 源码精读

转换器模块的文档字符串是官方对 HF 布局最完整的描述：

> [tools/part3d_glamm_data_utils.py:9-18](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L9-L18) —— 逐行列出四类 npy 与三类 QA JSON 的确切相对路径。注意两个易错点：`part_qa`/`object_qa` 的 JSON 文件名是 `<n>_part_mapping.json`（**没有** `frame_` 前缀），而 `multiturn_qa` 是 `frame_<n>_multi_conv.json`（**有**前缀）；且前两者多一层 `final_json_outputs/` 目录。

README 中的对应布局图（内容一致，面向用户）：

> [README.md:150-163](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L150-L163) —— 「Step 1 — download」一节的目录树，并注明 `vert.npy` 是 `load_dim=6`。

划分清单的权威性体现在构造函数里：

> [tools/part3d_glamm_data_utils.py:111-124](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L111-L124) —— `Part3dGlammData.__init__` 拼出 `part_ground3d_{split}.txt` 路径；文件不存在直接抛 `FileNotFoundError`（而不是扫目录兜底），并用 `mmengine.list_from_file` 读入场景集合，最后打印该划分的帧数。

帧 ID 与 npy 路径的互转规则：

> [tools/part3d_glamm_data_utils.py:127-131](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L127-L131) —— `_frame_npy`：把 `<scene>_frame_<n>` 按 `'_frame_'` 切开，映射回 `processed_data/<scene>/frame_<n>_<suffix>.npy`。

> [tools/part3d_glamm_data_utils.py:133-149](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L133-L149) —— `_scan_frame_ids`：遍历 `processed_data/<scene>/` 下所有以 `frame_` 开头、以 `_vert.npy` 结尾的文件，拼出帧 ID 并排序。只有出现在划分清单里的场景会被扫描（`scene_id not in scene_id_set` 的直接跳过）。

#### 4.1.4 代码实践

**实践目标**：不下载 150 GB 全量数据，只拉 ~3 GB 标注子集，亲眼确认目录布局与划分清单。

**操作步骤**：

```bash
# 示例代码
pip install -U huggingface_hub
python -c "
from huggingface_hub import snapshot_download
root = snapshot_download(
    repo_id='amolharsh/Ground3D_Dataset',
    repo_type='dataset',
    allow_patterns=['*/refined_qa_data/*', '*/part_ground3d_*.txt', 'README.md'],
)
print(root)
"
```

然后查看目录结构：

```bash
# 示例代码：找到缓存根目录后（把 <root> 换成上一步打印的路径）
find "<root>" -maxdepth 3 -type d | head -30      # 目录骨架
wc -l "<root>"/scannet/part_ground3d_train.txt     # ScanNet 训练场景数
head -3  "<root>"/scannet/part_ground3d_train.txt  # 场景 ID 长什么样
```

**需要观察的现象**：

1. 目录里只有 `refined_qa_data/` 和两个 txt，**没有** `processed_data/`（因为 `allow_patterns` 没匹配它）。
2. `scannet` 与 `scannetpp` 两个源的目录结构完全同构。

**预期结果**：`scannet/part_ground3d_train.txt` 有 1,433 行、`val` 有 80 行；`scannetpp` 为 857 / 48 行——与 README 的统计表（[README.md:126-130](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L126-L130)）一致。若网络受限无法下载，此实践「待本地验证」，可改为直接阅读 README 的布局图完成认知。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Part3dGlammData.__init__` 在划分清单缺失时抛异常，而不是退化为「扫描 `processed_data/` 里有什么就用什么」？

**答案**：因为 `processed_data/` 中可能包含没有任何 QA 的场景点云（README 也提到部分 ScanNet++ 场景缺文件或在 VLM 校验阶段丢了 QA）。磁盘兜底会悄悄构建出与发布不同的划分，导致训练/评估数据与论文不可比；显式报错把问题暴露在准备阶段。见 [tools/part3d_glamm_data_utils.py:111-121](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L111-L121) 的注释。

**练习 2**：帧 `scene0000_00_frame_12` 的 `sp_label` 文件的完整相对路径是什么？它的 `part_qa` JSON 路径又是什么？

**答案**：超点标签在 `<root>/processed_data/scene0000_00/frame_12_sp_label.npy`（由 `_frame_npy` 规则拼接，[tools/part3d_glamm_data_utils.py:127-131](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L127-L131)）；part QA 在 `<root>/refined_qa_data/part_qa/scene0000_00/final_json_outputs/12_part_mapping.json`（由 `_process_qa` 拼接，[tools/part3d_glamm_data_utils.py:157-159](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L157-L159)）。注意后者文件名没有 `frame_` 前缀。

**练习 3**：`_scan_frame_ids` 为什么只认 `*_vert.npy`，而不是「目录里有任意 npy 就算一帧」？

**答案**：`vert.npy` 是唯一必需的几何文件——没有点云，其余标签无从对齐，QA 也无法回答。拿它当帧的存在标记可以天然跳过下载不完整、只有标签没有点的残缺帧。见 [tools/part3d_glamm_data_utils.py:145-148](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L145-L148)。

### 4.2 每帧四类 npy：点、超点、部件、物体

#### 4.2.1 概念说明

每个场景帧目录下的四个 `.npy` 文件描述**同一批点**的不同属性：

| 文件后缀 | 内容 | 形状 | dtype 语义 |
|---|---|---|---|
| `frame_<n>_vert.npy` | 点本身：XYZ 坐标 + RGB 颜色 | \( (N, 6) \) | 浮点 |
| `frame_<n>_sp_label.npy` | 每个点属于哪个超点 | \( (N,) \) | 整数（超点 id） |
| `frame_<n>_part_label.npy` | 每个点的部件实例标签 | \( (N,) \) | 整数（部件实例 id） |
| `frame_<n>_object_label.npy` | 每个点的物体实例标签 | \( (N,) \) | 整数（物体实例 id） |

四者的第一维都是点数 \(N\)，逐点对齐：第 \(i\) 行描述的是同一个点。超点数 \(M = |\text{unique}(sp\_label)|\)，通常 \(M \ll N\)。

两处容易误解的地方：

- **part_label / object_label 是「实例」标签，不是「类别」标签**。同一个场景里两把椅子是两个不同的 object id。QA 里的 `object_id` / `part_id` 指的就是这些实例编号（4.3 节会看到证据）。
- **超点标签与部件/物体标签是独立的两套分组**。超点是几何聚类（给模型压缩序列用），部件/物体是语义实例（给 QA 提供答案区域用）。一个部件通常覆盖若干个完整超点，所以超点级 GT 可以由「部件 mask ∩ 超点」投影得到——这正是后续 `AddSuperPointAnnotations_Part3DGlamm`（u2-l4）做的事。

#### 4.2.2 核心流程

转换阶段四类 npy 的消费优先级不同：

```
读一帧 (frame_id = <scene>_frame_<n>)
│
├─ vert.npy       必需：缺失 → 警告并丢弃该帧；转成 points/<frame_id>.bin
├─ sp_label.npy   可选：存在才写 super_points/<frame_id>.bin
├─ object_label.npy ┐
└─ part_label.npy   ├─ 仅 train/val 写出（test 划分跳过）；统一转 int64 后写
                    └─ object_mask/ part_mask/<frame_id>.bin
```

注意 `.bin` 是 `tofile()` 写出的**裸字节流，不含形状信息**，所以回读方必须自己知道 dtype 和长度——这就是转换时强制 `astype(np.int64)` 的意义：写读双方约定死 8 字节有符号整数。

#### 4.2.3 源码精读

文档字符串对四类文件的一句话定义：

> [tools/part3d_glamm_data_utils.py:11-14](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L11-L14) —— `vert.npy` 注明 `load_dim=6`（XYZ+RGB）；`sp_label` 是 superpoint ids；`part_label` / `object_label` 是 per-point 标签。

`_process_frame` 中四类文件的写出逻辑（这里只看文件对应关系，转换全流程留给 u2-l2）：

> [tools/part3d_glamm_data_utils.py:183-195](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L183-L195) —— `vert.npy` 缺失时打印警告并返回 `None`（整帧被丢弃）；存在则 `np.load(...).tofile()` 写入 `points/<frame_id>.bin` 并记录 `info['pts_path']`；`sp_label.npy` 用 `os.path.exists` 判断，存在才写 `super_points/` 目录并记录 `info['super_pts_path']`。

> [tools/part3d_glamm_data_utils.py:197-209](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L197-L209) —— `object_label` 与 `part_label` 仅在**非 test 模式**下处理：`np.load(...).astype(np.int64).tofile(...)` 强转 64 位整数后分别写入 `object_mask/`、`part_mask/`。也就是说验证集评估时模型拿不到这两类 GT 标签文件路径（防止「偷看答案」）。

回读方的 dtype 约定（旁证形状）：

> [uniseg3d/loading.py:128-131](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/loading.py#L128-L131) —— 超点标签以 `np.fromfile(sp_pts_mask_path, dtype=np.int64)` 读回，得到与点数等长的 1-D 数组 `sp_pts_mask`。`part_mask` 同样按 `int64` 1-D 读回（[uniseg3d/loading.py:55-56](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/loading.py#L55-L56)）。这从消费端证实：三类标签都是 \( (N,) \) 的逐点整数数组，与 \( (N,6) \) 的点云逐行对齐。

#### 4.2.4 代码实践

**实践目标**：亲手读取一个场景帧的四类 npy，验证「逐点对齐」与维度约定。

**操作步骤**：标注子集里没有 `processed_data`，需要定向下载单个场景（避免全量 150 GB）：

```python
# 示例代码：只下载 scannet 下一个场景的 processed_data（按需调整场景名）
from huggingface_hub import snapshot_download
root = snapshot_download(
    repo_id="amolharsh/Ground3D_Dataset",
    repo_type="dataset",
    allow_patterns=["scannet/processed_data/scene0000_00/*"],  # 换成任一存在于 train.txt 的场景
)
```

然后检查（`<root>` 换成返回路径，帧号按实际存在的换）：

```python
# 示例代码
import numpy as np, os
d = os.path.join("<root>", "scannet", "processed_data", "scene0000_00")
n = 0  # 换成该目录下实际存在的帧号，如 "0"
vert  = np.load(os.path.join(d, f"frame_{n}_vert.npy"))
sp    = np.load(os.path.join(d, f"frame_{n}_sp_label.npy"))
part  = np.load(os.path.join(d, f"frame_{n}_part_label.npy"))
obj   = np.load(os.path.join(d, f"frame_{n}_object_label.npy"))
print("vert ", vert.shape, vert.dtype, "坐标范围:", vert[:, :3].min(0), vert[:, :3].max(0))
print("sp   ", sp.shape,   sp.dtype,   "超点数 M =", len(np.unique(sp)))
print("part ", part.shape, part.dtype, "部件实例数 =", len(np.unique(part)))
print("obj  ", obj.shape,  obj.dtype,  "物体实例数 =", len(np.unique(obj)))
assert vert.shape[0] == sp.shape[0] == part.shape[0] == obj.shape[0], "逐点对齐失败!"
```

**需要观察的现象**：

1. 四个数组第一维完全相等（同一批点）。
2. `vert` 形状为 `(N, 6)`，前 3 列是米制坐标（量级 ~0-5），后 3 列是 0-255 的颜色。
3. 超点数 \(M\) 远小于点数 \(N\)。

**预期结果**：以上断言通过；具体 \(N\)、\(M\) 数值因帧而异，「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_process_frame` 对 `vert.npy` 缺失和 `sp_label.npy` 缺失采取不同策略？

**答案**：`vert.npy` 是帧的根本——没有点云其他一切无从谈起，所以缺失即丢帧（返回 `None`，[tools/part3d_glamm_data_utils.py:184-186](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L184-L186)）；`sp_label.npy` 缺失只跳过超点路径记录，帧仍保留（[tools/part3d_glamm_data_utils.py:191-195](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L191-L195)），下游对空超点有兜底路径。

**练习 2**：`.bin` 文件为什么不存形状？回读时如何知道该读多少个元素？

**答案**：`tofile()` 只写裸字节。回读方（`loading.py`）用 `np.fromfile(path, dtype=np.int64)` 一次读完整个文件，长度自然等于文件字节数除以 8——对逐点标签而言长度就是点数 \(N\)，无需额外存储。这也是转换端必须统一 `astype(np.int64)` 的原因：dtype 约定不一致会读出乱码。

**练习 3**：给定 `sp_label`（\( (N,) \)）和某个部件的逐点 0/1 掩码 `pm`（\( (N,) \)，1 表示属于该部件），如何得到**超点级**的该部件掩码？

**答案**：对每个超点 \(s\)，统计它内部属于该部件的点占比并阈值化，例如 \(\text{mask}_{sp}[s] = \mathbb{1}[\frac{1}{|s|}\sum_{i \in s} pm_i > 0.5]\)。用 numpy 可写作 `np.bincount(sp[pm==1], minlength=M) > 0`（保守版：只要含一个正点就算）。这正是下游把 \( (N,) \) 标签投影成 \( (M,) \) 超点 GT 的思路，具体实现在 u2-l4 的 `AddSuperPointAnnotations_Part3DGlamm` 中展开。

### 4.3 QA JSON 的三层层级与 9 类任务名

#### 4.3.1 概念说明

`refined_qa_data` 下按提问粒度分三个层级：

| 层级目录 | 每帧文件 | 粒度 | 转换后的键名 |
|---|---|---|---|
| `part_qa/` | `final_json_outputs/<n>_part_mapping.json` | 单轮问答，答案区域是**部件** | `part_qa_data` |
| `object_qa/` | `final_json_outputs/<n>_object_mapping.json` | 单轮问答，答案区域是**物体** | `object_qa_data` |
| `multiturn_qa/` | `frame_<n>_multi_conv.json` | **多轮**对话，每轮可引用物体或部件 | `multi_turn_qa_data` |

（路径拼接见 [tools/part3d_glamm_data_utils.py:155-173](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L155-L173) 的 `_process_qa`。）

单轮 QA JSON 的原始结构是嵌套字典：

```
{ <id>: { <task_name>: [ {question, answer, object_id?, part_id?, ...}, ... ], ... }, ... }
     └ 条目 id      └ 9 类任务名之一        └ 该任务下的问答列表
```

多轮 QA 则是「对话列表的列表」：每个 `<id>` 下是若干场对话，每场对话是一列 `{question, answer, ...}` 轮次。

9 类任务名（转换代码中的权威清单 `ALL_TASKS`）：

`functional_part_grounding` · `functional_object_grounding` · `scale_comparison_size` · `distance_estimation` · `relative_position_forward_reasoning` · `relative_depth_forward` · `existence_verification` · `scale_estimation` · `grounded_dimension_reasoning`

> ⚠️ **一个需要注意的不一致**：README 正文开头写「8 sub-tasks」（[README.md:18](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L18)），但 README 的子任务列表（[README.md:137-146](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L137-L146)）和代码里的 `ALL_TASKS` 都是 **9 个**名字。以代码为准（过滤逻辑认的是这 9 个字符串）；论文中 8/9 的口径差异「待确认」，学习时记住「数据里有 9 类任务名」即可。

数据集的整体规模（README 统计表）：

| 来源 | 训练场景 | 验证场景 | Object QA | Part QA | Multi-turn | 总计 |
|---|---:|---:|---:|---:|---:|---:|
| ScanNet | 1,433 | 80 | 655,760 | 909,905 | 62,064 | 1,627,729 |
| ScanNet++ | 857 | 48 | 339,396 | 498,399 | 9,783 | 847,578 |
| **合计** | **2,290** | **128** | **995,156** | **1,408,304** | **71,847** | **2,475,307** |

（见 [README.md:122-130](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L122-L130)。）

#### 4.3.2 核心流程

`_flatten_qa_data` 把单轮 QA 的两层嵌套压平成按任务分组的字典：

```
输入:  { id_1: { task_a: [qa, qa], task_b: [qa] },
         id_2: { task_a: [qa] },
         "scene_level_metrics": <非字典的统计项, 跳过> }
                    │
                    ▼  逐 id → 逐 task → 逐 qa_item
中间:  深拷贝每条 qa；若 question 不含 '<SEG>' 则在末尾追加 '<SEG>'
                    │
                    ▼  丢弃空任务
输出:  { 'qa_data': { task_a: [qa, qa, qa], task_b: [qa] } }
```

多轮的 `_flatten_multi_turn_qa` 更简单：收集所有对话（轮次列表），并对**答案里含 `<SEG>` 而问题里没有**的轮次给问题补上 `<SEG>`。注意方向差异：单轮是无条件给问题补 `<SEG>`，多轮是看答案里有没有 `<SEG>` 才补。

关于 `<SEG>` 补齐的动机：模型的回答模板以 `<SEG>` 结尾表达「此处接地」，训练时需要问题与回答在 token 层面成对出现这种标记，发布数据里部分问题缺失，转换阶段统一补上，避免训练管线里再打补丁。

#### 4.3.3 源码精读

任务名权威清单：

> [tools/part3d_glamm_data_utils.py:34-39](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L34-L39) —— `ALL_TASKS` 列出全部 9 个任务名。后续所有按任务过滤（包括 u1-l4 用过的 `sub_task` 旋钮的取值空间）都以这组字符串为准，拼错即静默匹配不到任何 QA。

单轮展平函数：

> [tools/part3d_glamm_data_utils.py:42-49](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L42-L49) —— `_flatten_qa_data` 的文档字符串写明输入输出结构；文件不存在返回 `None`；初始化时给 9 个任务各建一个空列表。从这里可以直接读出原始 JSON 的形状：`{id: {task: [{question, answer}, ...]}}`。

> [tools/part3d_glamm_data_utils.py:50-61](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L50-L61) —— 循环体：`origin[_id]` 不是字典的直接跳过（注释点名 `scene_level_metrics` 之类统计项——说明原始 JSON 里混有非 QA 的元数据键）；任务名不在 `ALL_TASKS` 或值不是列表的跳过；每条 qa 深拷贝后，若 `question` 不含 `<SEG>` 则追加。单条解析失败静默跳过（`except Exception: continue`）。

> [tools/part3d_glamm_data_utils.py:64-65](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L64-L65) —— 最后把完全没有 QA 的任务从字典里删掉，输出紧凑的 `{'qa_data': {task: [items]}}`。

多轮展平函数：

> [tools/part3d_glamm_data_utils.py:68-88](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L68-L88) —— `_flatten_multi_turn_qa`：输出是「对话列表」，每场对话是一个轮次列表；[L83-L84](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L83-L84) 是补 `<SEG>` 的条件判断——**当且仅当该轮 answer 含 `<SEG>` 而 question 不含时**才给 question 补。空结果返回 `None`。

每帧三类 QA 的读取与组装：

> [tools/part3d_glamm_data_utils.py:155-173](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L155-L173) —— `_process_qa`：按 4.1 节的路径模板分别读 part / object / multiturn 三个 JSON；展平后为空的置 `None`；三者皆 `None` 则该帧无 QA、整帧丢弃；否则返回 `{'part_qa_data': ..., 'object_qa_data': ..., 'multi_turn_qa_data': ...}`。这三个键名就是 u1-l4 里 `task_level` 旋钮的取值（`part_qa_data` / `object_qa_data` / `multi_turn_qa_data`）——**评估配置的过滤层级直接沿用了转换产物的键结构**。

QA 条目里的 id 字段是原始数据自带的（下游证据）：

> [uniseg3d/transforms_3d.py:279-301](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L279-L301) —— `get_seg_from_single_qa` 直接访问 `single_qa['object_id']` / `single_qa['part_id']`，并用它们去索引部件/物体掩码来构造 GT。而 `qa_keys` / `qa_type` 这两个字段是下游 `flatten_qa_data` 补加的（[uniseg3d/transforms_3d.py:446-447](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L446-L447)），原始 JSON 里没有。区分「数据自带」与「管线补加」是读数据格式时的重要习惯。

#### 4.3.4 代码实践

**实践目标**：对下载好的标注子集，统计一个场景里各任务的 QA 分布，并观察 `<SEG>` 标记的分布。

**操作步骤**：

```python
# 示例代码：复用项目自己的展平函数，保证行为与转换一致
import sys, os, json
sys.path.insert(0, "tools")                     # 仓库根目录下运行
from part3d_glamm_data_utils import _flatten_qa_data, ALL_TASKS

root = "<标注子集根目录>/scannet/refined_qa_data"
scene = "scene0000_00"                          # 换成 train.txt 里任一场景
qa_dir = os.path.join(root, "part_qa", scene, "final_json_outputs")
for fn in sorted(os.listdir(qa_dir))[:3]:       # 只看前 3 帧的 part QA
    flat = _flatten_qa_data(os.path.join(qa_dir, fn))
    if flat is None:
        print(fn, "-> 文件不存在"); continue
    n_total = sum(len(v) for v in flat["qa_data"].values())
    n_seg = sum(q["question"].endswith("<SEG>") for v in flat["qa_data"].values() for q in v)
    print(f"{fn}: 共 {n_total} 条 QA, 分布 {[ (t, len(flat['qa_data'].get(t, []))) for t in flat['qa_data'] ]}")
    print(f"        其中 {n_seg} 条问题以 <SEG> 结尾")
    sample = next(iter(flat["qa_data"].values()))[0]
    print("        首条 QA 的键:", list(sample.keys()))
```

**需要观察的现象**：

1. 每帧 part QA 按任务分组，任务名都是 `ALL_TASKS` 的子集。
2. 每条 QA 的键至少有 `question` / `answer`，接地型任务通常还有 `object_id`（或类似 id 字段）。
3. 展平后所有问题都以 `<SEG>` 结尾——包括那些原始数据里本来没有、被 `_flatten_qa_data` 补上的。

**预期结果**：脚本打印出每帧的任务分布计数与首条 QA 的键名列表；不同帧、不同场景的任务分布差异较大，具体数字「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`task_level`（u1-l4 的评估旋钮）的三个合法取值是什么？它们和 `refined_qa_data` 的三个子目录是什么关系？

**答案**：`part_qa_data` / `object_qa_data` / `multi_turn_qa_data`（见 [uniseg3d/transforms_3d.py:421](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L421) 的注释）。它们是 `_process_qa`（[tools/part3d_glamm_data_utils.py:172-173](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L172-L173)）产出的三个键，分别对应原始目录 `part_qa` / `object_qa` / `multiturn_qa`——目录名的连字符变体加了 `_data` 后缀。

**练习 2**：单轮 QA 和多轮 QA 的 `<SEG>` 补齐条件有何不同？为什么多轮要加条件？

**答案**：单轮无条件给不含 `<SEG>` 的问题追加（[tools/part3d_glamm_data_utils.py:59-60](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L59-L60)）；多轮只在**该轮答案含 `<SEG>`** 时才补（[tools/part3d_glamm_data_utils.py:83-84](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L83-L84)）。多轮对话里存在纯闲聊/追问轮（如「谢谢」「还有什么？」），其答案不含接地标记，若也给问题补 `<SEG>`，会造出「问题承诺接地、回答却不接地」的脏样本。

**练习 3**：如果想统计整个 ScanNet 训练集 `scale_estimation` 任务有多少条 QA，用本讲知识怎么写？（只需思路）

**答案**：遍历 `part_ground3d_train.txt` 中的场景 → 对每个场景遍历 `refined_qa_data/{part_qa,object_qa}/<scene>/final_json_outputs/*.json` → 逐个调用 `_flatten_qa_data` → 累加 `flat['qa_data'].get('scale_estimation', [])` 的长度。多轮任务不计入（`scale_estimation` 只出现在单轮层级）。所得总数应与 README 表中该任务的规模量级相符（精确到任务的分拆数字 README 未给出，故只做量级核对，「待本地验证」）。

## 5. 综合实践

**任务：为 Ground3D 的一个场景帧制作「数据卡片」。**

把本讲三个模块串起来——目录导航、四类 npy 对齐检查、QA 展平统计——产出一份单帧档案：

1. **选定帧**：从 `scannet/part_ground3d_train.txt` 任取一个场景，下载该场景的 `processed_data`（4.2.4 的定向 `allow_patterns`）并确认标注子集（4.1.4）已就绪；从 `processed_data/<scene>/` 里挑一个帧号 \(n\)，帧 ID 记为 `<scene>_frame_<n>`。
2. **几何侧**：按 4.2.4 的脚本读四类 npy，记录：\(N\)（点数）、\(M=|\text{unique}(sp)|\)（超点数）、部件/物体实例数、坐标范围，并断言四者第一维一致。
3. **标注侧**：按 4.3.4 的脚本展平该帧的 part / object QA，统计各任务条数；另读 `multiturn_qa/<scene>/frame_<n>_multi_conv.json`（若存在），记录对话场数与平均轮数。
4. **交叉验证**：取一条 `functional_part_grounding` 的 QA，用其 id 字段在 `part_label` / `object_label` 的 unique 值中查找，确认该 id 确实存在（验证「QA 引用的实例 = npy 标签里的实例编号」）。
5. **产出**：把以上数字填进一张 Markdown 卡片（帧 ID、\(N\)、\(M\)、实例数、各任务 QA 数、多轮对话数），存入你自己的学习笔记。

预期结果：一张能独立说明「这个帧里有什么」的卡片；所有断言通过。完整数据未下载时，第 2、4 步「待本地验证」，可先只完成标注侧。

## 6. 本讲小结

- Ground3D 每个数据源三大块：`processed_data/<scene>/`（四类逐点 npy）、`refined_qa_data/{part_qa,object_qa,multiturn_qa}`（三层 QA JSON）、`part_ground3d_{train,val}.txt`（划分权威清单）。
- 帧的唯一标记是 `vert.npy`，帧 ID 格式 `<scene>_frame_<n>`；划分只认 txt 清单，磁盘内容不兜底——缺失清单直接报错。
- 四类 npy 逐点对齐：`vert` 是 \( (N,6) \) 的 XYZ+RGB，`sp_label` / `part_label` / `object_label` 都是 \( (N,) \) 整数标签；转换时标签统一 `astype(np.int64)` 写裸 `.bin`，回读靠 dtype 约定还原。
- 单轮 QA 原始结构是 `{id: {task: [{question, answer, ...}]}}`，任务名共 9 个（`ALL_TASKS`；README 标题写 8 个子任务，与列表/代码的 9 个名字存在口径差异，以代码为准）。
- 转换层的第一道清洗：跳过非字典条目（如 `scene_level_metrics`）、跳过未知任务、给问题补 `<SEG>`（单轮无条件、多轮看答案）、丢弃空任务；`object_id` / `part_id` 是原始数据自带，`qa_keys` / `qa_type` 是下游管线补加。
- 评估配置的 `task_level` 三取值（`part_qa_data` / `object_qa_data` / `multi_turn_qa_data`）直接来自 `_process_qa` 产物的三个键——数据层级与评估旋钮一一对应。

## 7. 下一步学习建议

下一讲 **u2-l2《数据转换：create_data.py 生成 infos.pkl》** 将沿着本讲的原始数据继续向下走：`tools/create_data.py` 如何按 `--dataset part3d_glamm` 分发到 `Part3dGlammData`，`_process_frame` 写出的四类 `.bin` 与 `info` 字典如何被 `tools/update_infos_to_v2.py` 汇总成 `part3d_glamm_infos_{train,val}.pkl`。建议先浏览 [tools/create_data.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/create_data.py) 的参数定义，再对照本讲 4.2.3 的 `_process_frame` 片段预习。之后 u2-l3 会讲配置如何用 `ann_file` + `data_prefix` 消费这些 pkl 与 bin。
