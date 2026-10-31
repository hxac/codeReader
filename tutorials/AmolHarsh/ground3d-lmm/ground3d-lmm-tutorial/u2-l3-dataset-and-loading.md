# u2-l3 数据集类与点云/标注加载

## 1. 本讲目标

上一讲（u2-l2）我们跟着 `create_data.py` 走完了「HF 原始 npy/JSON → 裸 .bin + `part3d_glamm_infos_{train,val}.pkl`」的转换链，最后得到的是一个**只存裸文件名**的 infos pkl。本讲回答紧接着的问题：**训练/评估时，这些裸文件名是怎么变成磁盘上真实文件、又被读进内存的？**

学完本讲你应该能够：

1. 画出 `data_prefix` 四个键到 `data/scannet` 下四个目录的映射关系，并解释「裸文件名契约」为什么这样设计。
2. 逐行读懂 `Part3DGlamm_Dataset.parse_data_info`：它给父类 `ScanNetSegDataset` 补了哪三个路径键、`scene_name` 从哪来，以及它与原生数据集类的全部差异。
3. 说清 `LoadAnnotations3D_` 如何通过**方法重写 + 多态**替换父类的 mask 加载逻辑，加载哪四类 mask、写进 `results` 的哪些键、缺失时如何用 `None` 兜底。
4. 解释 `NormalizePointsColor_` 的归一化公式，以及它为什么比上游版本多一个 `color_std` 参数。

## 2. 前置知识

### 2.1 mmengine `BaseDataset` 的生命周期

本项目的数据集类不是从零写的，而是挂在 OpenMMLab 的数据框架上。一个 mmengine 数据集从配置到产出数据，大致经历：

1. `__init__`：接收 `ann_file`（就是那个 pkl）、`data_root`、`data_prefix`、`pipeline`、`metainfo` 等参数；mmengine 会把 `data_root` 与 `data_prefix` 的每个值拼成完整前缀（例如 `data/scannet` + `super_points` → `data/scannet/super_points`），存进 `self.data_prefix`。
2. `full_init()` → `load_data_list()`：读 pkl，对**每一条**原始 info 调用 `self.parse_data_info(info)`，把「原始 info」加工成「数据集内部 info」，得到 `self.data_list`。
3. `__getitem__(idx)`：取出 `data_list[idx]` 的副本作为 `results` 字典，依次交给 `pipeline` 里的每个 transform，最后返回。

也就是说：**`parse_data_info` 是 pkl 字段与 pipeline 字段之间的「翻译层」**，本讲的主角之一。

### 2.2 `results` 字典：pipeline 的通用货币

pipeline 里每个 transform 都接收并返回同一个 `results` 字典：读自己需要的键、写自己负责的键。本讲涉及的关键键名（也是下一讲的输入）：

| 键 | 写入者 | 含义 |
|---|---|---|
| `points` | `LoadPointsFromFile` | 点云对象（N×6，XYZ+RGB） |
| `sp_pts_mask` | `LoadAnnotations3D_` | 长度为 N 的**超点 id**（每个点属于哪个超点） |
| `pts_part_mask` / `pts_object_mask` | `LoadAnnotations3D_` | 长度为 N 的部件/物体**实例 id** |
| `pts_semantic_mask` / `pts_instance_mask` | `LoadAnnotations3D_` | 语义/实例标签（Ground3D 帧数据通常没有，置 `None`） |
| `scene_name` | `Part3DGlamm_Dataset.parse_data_info` | 帧名，如 `scene0000_00_frame_0` |

### 2.3 承接前两讲的两个事实

- **u2-l2 的 dtype 契约**：转换端把 part/object mask 强制 `int64` 写盘，超点标签原样搬运、**读端统一按 `int64` 读回**——本讲会在 `LoadAnnotations3D_` 里看到这个「读端」长什么样。
- **u1-l3 的尾下划线约定**：`LoadAnnotations3D_`、`NormalizePointsColor_` 这类带尾下划线的类名，表示「继承上游 mmdet3d 类的改版」；`Part3DGlamm_Dataset`（无下划线）则是本项目新命名的数据集类。

> 说明：本讲引用的 mmdet3d 父类（`ScanNetSegDataset`、`LoadAnnotations3D`、`NormalizePointsColor`，版本 1.4.0，见 u1-l2）源码不在本仓库内，相关行为只从**本仓库子类的调用方式**反推，不标注上游行号。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
|---|---|
| [uniseg3d/scannet_dataset.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/scannet_dataset.py) | 数据集类 `Part3DGlamm_Dataset`（及兄弟类 `ScanNetUnifiedSegDataset`），改写 `parse_data_info` |
| [uniseg3d/loading.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/loading.py) | `LoadAnnotations3D_`（mask/超点加载）与 `NormalizePointsColor_`（颜色归一化） |
| [configs/ground3dlmm_train.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py) | 训练配置：`data_prefix` 映射、pipeline 顺序、双数据源 `ConcatDataset` |
| [configs/ground3dlmm_eval_ground3d_scannet.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py) | 评估配置：同一套加载器在 `test_pipeline` 中的位置 |
| [tools/part3d_glamm_data_utils.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py) | （回顾）转换端写入 pkl 的字段名——`parse_data_info` 的输入契约 |
| [tools/update_infos_to_v2.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/update_infos_to_v2.py) | （回顾）v2 规范化：路径削成裸文件名的现场 |

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：**4.1 data_prefix 路径映射** → **4.2 parse_data_info** → **4.3 LoadAnnotations3D_** → **4.4 NormalizePointsColor_**，正好是「拼路径 → 解析 info → 读标注 → 归一化」这条真实执行链。

### 4.1 data_prefix 路径映射：裸文件名契约

#### 4.1.1 概念说明

u2-l2 里我们看到：v2 规范化（`update_part3d_glamm_infos`）把所有路径**削成裸文件名**（只留 `xxx.bin`），把「文件放在哪个目录」的决定权推迟到运行时。这样做的好处：

- 同一份数据挪位置（换机器、换 `data_root`）不用重新生成 pkl；
- ScanNet 与 ScanNet++ 两套数据共用完全相同的 pkl 结构，只改 `data_root` 即可（训练配置里正是这样拼的双数据源）。

运行时负责「目录 + 文件名 → 完整路径」的，就是配置里的 `data_prefix` 字典。它是 pkl 字段与磁盘目录之间的对照表。

#### 4.1.2 核心流程

```
配置: data_root = 'data/scannet'
      data_prefix = dict(pts='points', pts_part_mask='part_mask',
                         pts_object_mask='object_mask', sp_pts_mask='super_points')

mmengine 初始化数据集时:
    self.data_prefix[k] = join(data_root, data_prefix[k])        # 例如 'data/scannet/super_points'

parse_data_info 时:
    info['super_pts_path']      = join(self.data_prefix['sp_pts_mask'],      info['super_pts_path'])       # 裸名 → 完整路径
    info['pts_part_mask_path']  = join(self.data_prefix['pts_part_mask'],    info['pts_part_mask_path'])
    info['pts_object_mask_path']= join(self.data_prefix['pts_object_mask'],  info['pts_object_mask_path'])
```

pkl 字段 → 磁盘目录的完整对照（Ground3D 帧数据）：

| pkl 中的字段 | `data_prefix` 键 | 磁盘目录 | 文件内容 | 读端（下一模块） |
|---|---|---|---|---|
| `lidar_points.lidar_path` | `pts` | `points/` | N×6 点云（XYZ+RGB） | `LoadPointsFromFile`（原生） |
| `super_pts_path` | `sp_pts_mask` | `super_points/` | 长度 N 的超点 id | `LoadAnnotations3D_._load_sp_pts_3d`，按 `int64` 读 |
| `pts_part_mask_path` | `pts_part_mask` | `part_mask/` | 长度 N 的部件实例 id | `LoadAnnotations3D_._load_masks_3d`，按 `int64` 读 |
| `pts_object_mask_path` | `pts_object_mask` | `object_mask/` | 长度 N 的物体实例 id | 同上 |

#### 4.1.3 源码精读

配置侧的映射声明只有 8 行：

- [configs/ground3dlmm_train.py:L97-L104](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L97-L104) —— 声明 `dataset_type = 'Part3DGlamm_Dataset'`、`data_root = 'data/scannet'` 与四个前缀键。注意 `data_root` 是**相对工作目录**的路径，所以训练必须在仓库根目录（或把 `data/` 放到启动目录下）发起。
- 同一映射在评估配置里逐字重复：[configs/ground3dlmm_eval_ground3d_scannet.py:L104-L110](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L104-L110)。

「裸文件名」的出处（回顾 u2-l2，这里是证据链）：

- [tools/part3d_glamm_data_utils.py:L180-L195](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L180-L195) —— 转换端写入 `pts_path`、`super_pts_path`，此时还带 `points/`、`super_points/` 目录前缀。
- [tools/update_infos_to_v2.py:L404-L419](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/update_infos_to_v2.py#L404-L419) —— v2 更新器用 `Path(...).name` 把三个 mask 路径削成裸文件名，同时把 `pts_path` 挪进 `lidar_points.lidar_path`，`qa_data` 原样搬运。这就是 `parse_data_info` 看到的最终形态。

一个值得注意的变体：整场景数据（ScanRefer/Reason3D 迁移用）会额外写 `instance_mask/`、`semantic_mask/` 两个目录（[tools/part3d_glamm_data_utils.py:L344-L369](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L344-L369)），对应的评估配置也换了 `data_prefix` 键集合：[configs/ground3dlmm_eval_scanrefer.py:L100-L104](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_scanrefer.py#L100-L104)（`pts_instance_mask='instance_mask'`、`pts_semantic_mask='semantic_mask'`，没有 part/object）。**pkl 字段、`data_prefix` 键、磁盘目录三者必须两两对齐**，这是自制数据集最容易错位的地方。

#### 4.1.4 代码实践

**实践目标**：亲手验证「pkl 里存的是裸文件名、且能被 `data_prefix` 拼出真实路径」。

**操作步骤**（示例代码，非项目文件；需先完成 u2-l2 的转换得到 pkl）：

```python
# 示例代码：检查 infos pkl 的路径字段
import mmengine, os
infos = mmengine.load('data/scannet/part3d_glamm_infos_train.pkl')
data_list = infos['data_list'] if isinstance(infos, dict) else infos
first = data_list[0]
prefix = dict(pts='points', pts_part_mask='part_mask',
              pts_object_mask='object_mask', sp_pts_mask='super_points')
for pkl_key, pref_key in [('super_pts_path', 'sp_pts_mask'),
                          ('pts_part_mask_path', 'pts_part_mask'),
                          ('pts_object_mask_path', 'pts_object_mask')]:
    if pkl_key in first:
        full = os.path.join('data/scannet', prefix[pref_key], first[pkl_key])
        print(pkl_key, first[pkl_key], '->', full, '存在' if os.path.isfile(full) else '不存在!')
```

**需要观察的现象**：打印出的第一列都是 `xxx.bin` 裸文件名（不带目录）；拼出的完整路径在磁盘上真实存在。

**预期结果**：所有打印行都以「存在」结尾。若 pkl 是 v1 格式（路径带目录前缀），拼接会得到 `data/scannet/super_points/super_points/xxx.bin` 双重目录——这正是没跑 `update_infos_to_v2` 的症状。**待本地验证**（依赖真实数据集）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `data_root` 改成 `/data/scannet` 但忘了搬数据，会在哪一步报错——`parse_data_info` 还是 `LoadAnnotations3D_`？

**答案**：都不会立刻报错。`parse_data_info` 只做字符串拼接不查盘；`LoadAnnotations3D_` 里 `get()` 失败后退回 `mmengine.check_file_exist(...)`，在那里抛「文件不存在」。也就是说路径错误暴露在**取数据时**而非**建数据集时**。

**练习 2**：为什么 pkl 里存裸文件名，而不是转换时的完整路径？

**答案**：让数据位置可迁移。同一份 pkl 配不同 `data_root` 即可指向不同机器上的数据副本；且 ScanNet/ScanNet++ 共用 pkl 结构，只需改 `data_root`（训练配置的双数据源就是这么做的）。代价是 pkl 与目录结构强耦合，挪目录名必须同步改 `data_prefix`。

---

### 4.2 `Part3DGlamm_Dataset.parse_data_info`：子类只做增量

#### 4.2.1 概念说明

`Part3DGlamm_Dataset` 继承 mmdet3d 原生 `ScanNetSegDataset`（一个为 ScanNet 语义/实例分割设计的类，父类已会把 `lidar_points.lidar_path` 拼上前缀放进 `info['lidar_path']`，并按训练/测试模式整理 `ann_info`）。子类的哲学是**「只补父类不认识的键，其余全部复用」**——它要补的东西只有一个动机：本项目需要**超点**、**部件/物体两级实例 mask** 和 **QA 数据**，这三样原生 ScanNet 数据集都没有。

文件里还有一个兄弟类 `ScanNetUnifiedSegDataset`：只补 `super_pts_path`、没有 part/object mask，被 ScanRefer/Reason3D 迁移评估配置使用（见 4.1.3 末尾）。Ground3D 的训练与评估都用 `Part3DGlamm_Dataset`。

#### 4.2.2 核心流程

```
parse_data_info(info):                                        # info 来自 v2 pkl 的一条
    info['super_pts_path'] = join(data_prefix['sp_pts_mask'], info['super_pts_path'])
                                                               # 无条件直接下标访问 → 缺键即 KeyError
    if 'pts_part_mask_path'   in info:  join(data_prefix['pts_part_mask'],  ...)
    if 'pts_object_mask_path' in info:  join(data_prefix['pts_object_mask'],...)
                                                               # part/object 是可选的（训练集才有）
    info = 父类.parse_data_info(info)                          # lidar_path、ann_info、eval_ann_info 等
    info['scene_name'] = basename(info['lidar_path']) 去掉扩展名
    return info
```

注意这里的**不对称**：`super_pts_path` 用直接下标（必填），part/object 用 `in` 判断（可选）。这与模型强依赖超点、而某些帧可能没有部件/物体标注的语义一致——但转换端（u2-l2 的 L191-L195）只有在 `sp_label.npy` 缺失时不写该键，此时数据集初始化会直接 `KeyError`。换句话说：**这个模型里超点是硬性要求，缺超点的帧必须在转换阶段就被丢掉。**

#### 4.2.3 源码精读

- [uniseg3d/scannet_dataset.py:L40-L46](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/scannet_dataset.py#L40-L46) —— 类注册 `@DATASETS.register_module()` 与 `get_scene_idxs` 重写：忽略一切入参，返回 `np.arange(len(self))`，即**每个样本等权重采样**。原生 ScanNet 分割数据集的 `scene_idxs` 机制用于按场景做加权重采样，这里被整体屏蔽（配置里也传了 `scene_idxs=None`）。
- [uniseg3d/scannet_dataset.py:L58-L65](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/scannet_dataset.py#L58-L65) —— 三条路径的拼接。`self.data_prefix.get('sp_pts_mask', '')` 的 `''` 兜底意味着：若配置漏写该键，就退化为「裸文件名相对当前目录解析」，错误会被推迟到读文件时，属于典型的静默错位坑。
- [uniseg3d/scannet_dataset.py:L67-L69](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/scannet_dataset.py#L67-L69) —— 先调父类（`lidar_path` 在父类里变成完整路径），再从它提取 `scene_name`：`'data/scannet/points/scene0000_00_frame_0.bin'` → `'scene0000_00_frame_0'`。这个键最终被 `Pack3DDetInputs_Part_Glamm` 收进 data sample（[configs/ground3dlmm_train.py:L150-L158](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L150-L158) 的 `keys` 列表最后一项），在评估时用作逐场景预测 JSON 的文件名（承接 u1-l4 的 `save_pred_qa_dir`）。

与原生 `ScanNetSegDataset` 的差异汇总：

| 维度 | 原生 `ScanNetSegDataset` | `Part3DGlamm_Dataset` |
|---|---|---|
| 点云路径 | `lidar_points.lidar_path` + `pts` 前缀 | 同（复用父类） |
| 超点路径 | 无 | `super_pts_path` + `sp_pts_mask` 前缀（必填） |
| 部件/物体 mask | 无 | `pts_part_mask_path`/`pts_object_mask_path` + 各自前缀（可选） |
| QA 数据 | 无 | `qa_data` 键原样透传（父类不认识也不动它，留给 pipeline 用） |
| `scene_name` | 无 | 从 `lidar_path` 提取 |
| 场景采样 | `scene_idxs` 加权采样机制 | 恒等采样（全量索引） |

#### 4.2.4 代码实践

**实践目标**：搞清 `scene_name` 的提取规则，并验证一个隐藏的脆弱点。

**操作步骤**（示例代码，纯字符串操作，结果可静态推得）：

```python
# 示例代码：模拟 parse_data_info 的 scene_name 提取
cases = [
    'data/scannet/points/scene0000_00_frame_3.bin',
    'data/scannet/points/scene0000_00.bin',           # 整场景命名
    '/a/b/scene0000_00_frame_3.1.bin',                # 假设文件名里还有个点
]
for p in cases:
    print(p, '->', p.split('/')[-1].split('.')[0])
```

**需要观察的现象**：前两行分别得到 `scene0000_00_frame_3`、`scene0000_00`；第三行得到 `scene0000_00_frame_3`——`.1.bin` 被截掉了。

**预期结果**：确认 `split('.')[0]` 只对「文件名不含额外点号」的命名安全。Ground3D 的帧名（`场景_frame_序号`）满足该约定；自制数据集若命名带点号会在这里坏掉。这是阅读源码发现的隐含契约，无「待本地验证」必要（纯字符串切片）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `super_pts_path` 的拼接不用 `if 'super_pts_path' in info` 保护，而 part/object 要？

**答案**：超点是整个模型管线的硬依赖（超点池化、超点级 GT、`<|point|>` token 全靠它），缺了无法训练也无法评估，所以设计成「缺键就让数据集初始化尽早崩溃」；part/object 只被部分 QA 子任务用到（部件/物体接地），且转换端只在训练集写这些键，评估集可能没有，所以用 `in` 判断、缺了由加载器置 `None` 兜底（见 4.3）。

**练习 2**：`get_scene_idxs` 被重写为恒等采样，这对训练意味着什么？

**答案**：每个帧样本被采样到的概率相同，不再像原生 ScanNet 分割那样按场景大小/类别分布做加权重采样。Ground3D 的采样粒度本来就是「帧级 QA」，平衡工作交给了后续 `QA_Generation_v2` 的随机采样（下一讲），数据集层不再干预。

---

### 4.3 `LoadAnnotations3D_`：四类 mask 与超点标签的读取

#### 4.3.1 概念说明

`LoadAnnotations3D_`（尾下划线，继承上游 `LoadAnnotations3D`）解决的问题是：**原生加载器只认识语义/实例两类 mask，本项目还需要超点标签与部件/物体实例 mask**。它的做法非常「Python」：

1. `__init__` 增加一个必填开关 `with_sp_mask_3d`（注意：没有默认值，配置里忘了写会直接构造失败）；
2. **整体重写** `_load_masks_3d`（mask 加载的内部实现方法），把父类版本彻底替换；
3. `transform` 里先调 `super().transform()`，再追加超点加载。

关键的机制是**多态**：父类 `transform()` 依据 `with_mask_3d=True` 去调用 `self._load_masks_3d(...)` 时，`self` 是 `LoadAnnotations3D_` 实例，于是实际执行的是子类重写后的版本——父类的 mask 加载逻辑永远不会运行，但又完全不必改父类一行代码。这与 u1-l3 讲过的「插件式改写」一脉相承。

#### 4.3.2 核心流程

```
transform(results):
    results ← 父类 transform(results)
              └─ with_mask_3d=True → self._load_masks_3d(results)   # 多态：执行子类版本
                    part      ← 读 results['pts_part_mask_path']     （int64 裸数组，缺键则 None）
                    object    ← 读 results['pts_object_mask_path']   （同上）
                    semantic  ← 读 results['pts_semantic_mask_path'] （Ground3D 帧数据通常缺 → None）
                    instance  ← 读 results['pts_instance_mask_path'] （同上）
                    全部写回 results，并同步进 results['eval_ann_info']（若存在）
    if with_sp_mask_3d:
        sp_pts_mask ← 读 results['super_pts_path']（int64 + .copy()）
        写 results['sp_pts_mask']；eval_ann_info 里额外记录 lidar_idx（文件名去 .bin）
```

读文件的统一套路（四类 mask 相同）：

```python
try:
    mask_bytes = get(path, backend_args=self.backend_args)   # mmdet3d 的通用文件后端
    mask = np.frombuffer(mask_bytes, dtype=np.int64)
except ConnectionError:                                      # 无后端可用 → 本地文件直读
    mmengine.check_file_exist(path)                          # 顺带完成「存在性检查」
    mask = np.fromfile(path, dtype=np.int64)
```

这正是 u2-l2 说的「读端统一按 int64 读回」的现场：**写端 dtype 是隐含契约**——如果自制数据把 mask 存成了 `int32`，这里按 `int64` 解码会得到长度减半的乱码数组，且不会报错。

#### 4.3.3 源码精读

- [uniseg3d/loading.py:L10-L20](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/loading.py#L10-L20) —— 类定义与 `__init__`：新增必填开关 `with_sp_mask_3d`，其余参数（`with_bbox_3d/with_label_3d/with_mask_3d/with_seg_3d` 等）原样透传给父类。
- [uniseg3d/loading.py:L41-L56](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/loading.py#L41-L56) —— 四个 mask 变量先统一初始化为 `None`，随后「有路径键才读」。部件 mask 的读取是这套模板的典型（`np.frombuffer(..., dtype=np.int64)`）；物体/语义/实例三个分支结构完全相同（[L58-L90](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/loading.py#L58-L90)）。**缺失即 `None`** 的设计让下游只需 `is None` 判断，不必 try/except。
- [uniseg3d/loading.py:L92-L105](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/loading.py#L92-L105) —— 四类 mask 写回 `results`；若 `results` 里已有 `eval_ann_info`（测试模式下由数据集解析阶段创建），同步写入一份，供评估器取 GT。
- [uniseg3d/loading.py:L109-L138](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/loading.py#L109-L138) —— `_load_sp_pts_3d`：读超点标签。两处细节值得注意：其一，[L123-L125](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/loading.py#L123-L125) 的 `.copy()` 带着注释 "fix read-only bug"——`np.frombuffer` 返回的是只读视图，而下游（下一讲的 `AddSuperPointAnnotations_Part3DGlamm`）要对其做张量化与聚合，拷贝一份避免写报错；而 part/object 分支**没有**加 `.copy()`，说明下游对它们没有就地写操作，这是读代码时可以反推出的事实。其二，[L136-L137](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/loading.py#L136-L137) 从路径反解 `lidar_idx`（`[:-4]` 去掉 `.bin`），供评估时对齐场景。
- [uniseg3d/loading.py:L140-L153](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/loading.py#L140-L153) —— `transform`：先父类后超点。父类调用处理了 bbox/label/mask/seg 的全部分发（本配置只开了 `with_mask_3d`），随后按 `with_sp_mask_3d` 追加超点。
- 配置侧的开关：训练 pipeline [configs/ground3dlmm_train.py:L114-L120](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L114-L120) 与测试 pipeline [configs/ground3dlmm_eval_ground3d_scannet.py:L173-L179](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L173-L179) 完全一致：`with_mask_3d=True, with_sp_mask_3d=True`，其余全关——评估也要加载 GT mask，这正是 `Part3DGlamm_SegMetric` 能算 mIoU 的前提（承接 u6-l1）。

pipeline 顺序也值得看一眼（[configs/ground3dlmm_train.py:L106-L149](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L106-L149)）：`LoadPointsFromFile` → `LoadAnnotations3D_` → `GlobalRotScaleTrans` → `NormalizePointsColor_` → …。**mask 在几何增强之前加载**：mask 是逐点对齐的索引数组，只要增强变换不重排点序，点与 mask 的对应就始终成立（`GlobalRotScaleTrans` 只做整体旋转平移缩放，不重排；`ElasticTransfrom` 的弹性扰动则单独存 `elastic_coords`，留到 u3-l3 讲）。

#### 4.3.4 代码实践

**实践目标**：不依赖真实数据集，用三个伪造的 `.bin` 直接驱动 `LoadAnnotations3D_`，验证键名、dtype 与 `None` 兜底。

**操作步骤**（示例代码，需 u1-l2 环境装好 mmdet3d 后运行）：

```python
# 示例代码：用假数据驱动 LoadAnnotations3D_
import numpy as np, os
from uniseg3d.loading import LoadAnnotations3D_

for d in ['tmp_mini/super_points', 'tmp_mini/part_mask', 'tmp_mini/object_mask']:
    os.makedirs(d, exist_ok=True)
N = 8
np.array([0,0,1,1,2,2,3,3], dtype=np.int64).tofile('tmp_mini/super_points/f.bin')
np.array([0,0,1,1,1,2,2,2], dtype=np.int64).tofile('tmp_mini/part_mask/f.bin')
np.array([0,0,0,0,1,1,1,1], dtype=np.int64).tofile('tmp_mini/object_mask/f.bin')

t = LoadAnnotations3D_(with_sp_mask_3d=True, with_bbox_3d=False,
                       with_label_3d=False, with_mask_3d=True, with_seg_3d=False)
results = {'super_pts_path': 'tmp_mini/super_points/f.bin',
           'pts_part_mask_path': 'tmp_mini/part_mask/f.bin',
           'pts_object_mask_path': 'tmp_mini/object_mask/f.bin'}
out = t.transform(results)
for k in ['sp_pts_mask', 'pts_part_mask', 'pts_object_mask', 'pts_semantic_mask', 'pts_instance_mask']:
    v = out.get(k, '<缺键>')
    print(k, getattr(v, 'dtype', v), getattr(v, 'shape', ''))

# 再删掉 part 路径键，验证 None 兜底
del results['pts_part_mask_path']
out2 = t.transform(results)
print('part 缺失时 ->', out2['pts_part_mask'])
```

**需要观察的现象**：前三个键是 `int64 (8,)` 的数组；后两个键是 `None`；删键后 `pts_part_mask` 变为 `None` 而不是报错。

**预期结果**：与上述一致——这验证了 4.3.1 的「缺失即 None」设计和 int64 读端契约。**待本地验证**（本讲义写作环境未装 mmdet3d；若父类构造签名有出入，以 mmdet3d 1.4.0 的 `LoadAnnotations3D` 为准微调传参）。

#### 4.3.5 小练习与答案

**练习 1**：为什么子类要整体重写 `_load_masks_3d`，而不是在 `transform` 里追加 part/object 的加载？

**答案**：父类 `transform()` 内部按开关调用 `self._load_masks_3d`，其父类版本读的键与本项目 pkl 的键（`pts_part_mask_path` 等顶层路径键）对不上；重写同名方法既能让父类的分发逻辑原样工作（多态替换实现），又能统一处理四类 mask 的「缺键置 None」语义。如果改在 `transform` 里追加，就得复制父类的分发逻辑，代码更脆。

**练习 2**：若自制数据集把 `part_mask/*.bin` 存成了 `int32`，会在哪一步、以什么形式出错？

**答案**：加载不会报错，但 `np.frombuffer(bytes, dtype=np.int64)` 会把 8 字节当作一个 int64 解读，得到长度为原来一半的乱码数组；错误会推迟到下游（下一讲用 part mask 构造超点级 GT 时形状/数值异常）才暴露。这就是 u2-l2 强调「写端 dtype 是隐含契约」的原因。

**练习 3**：`sp_pts_mask` 读取时加了 `.copy()`，part/object 为什么可以不加？

**答案**：`np.frombuffer` 返回只读视图，谁会被就地修改谁就需要拷贝。超点标签在下游要参与 `torch.as_tensor` 之外的更多操作（含可能触发的内存共享写路径），作者遇到过 read-only 报错故显式修复；part/object 没加且能正常训练，说明下游对它们没有就地写——这是一个可以从代码差异反推出调用约束的例子。

---

### 4.4 `NormalizePointsColor_`：颜色归一化

#### 4.4.1 概念说明

点云的 RGB 通道取值是 0~255 的整数尺度，而模型（尤其是要送进 LLM 的特征）偏好小数值范围。`NormalizePointsColor_` 在加载后把颜色做标准化：

\[
c' = \frac{c - \mu}{\sigma}, \qquad \mu = 127.5,\ \sigma = 127.5
\]

由于 \( c \in [0, 255] \)，归一化后 \( c' \in [-1, 1] \)。取 127.5 是为了让 0 和 255 对称映射到 −1 和 +1（docstring 注明默认值沿用 SPFormer 的预处理约定）。

上游 `NormalizePointsColor` 只接受 `color_mean`、标准差固定；子类唯一的改动是把 `color_std` 参数化（「Just add color_std parameter」）。

#### 4.4.2 核心流程

```
transform(input_dict):
    points = input_dict['points']                  # 带 color 属性的点云对象
    断言 points.attribute_dims 含 'color'           # 无颜色直接报错，而不是静默跳过
    points.color = points.color − color_mean       # 逐通道减均值
    points.color = points.color / color_std        # 逐通道除标准差
    return input_dict
```

#### 4.4.3 源码精读

- [uniseg3d/loading.py:L156-L168](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/loading.py#L156-L168) —— 类定义与 `__init__`：`color_mean` 必填，`color_std` 默认 127.5。注意它**没有**调 `super().__init__`，是对父类的完全替换而非扩展。
- [uniseg3d/loading.py:L181-L191](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/loading.py#L181-L191) —— `transform` 主体：断言点云带 color 属性（`LoadPointsFromFile` 的 `use_color=True` + `use_dim=[0..5]` 保证了这一点），然后减均值、除标准差，就地更新 `points` 后返回。
- 配置侧：训练在几何增强之后调用（[configs/ground3dlmm_train.py:L128-L130](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L128-L130)，`color_mean=[127.5, 127.5, 127.5]`，`color_std` 用默认值）；评估时它被包在 `MultiScaleFlipAug3D` 的 `transforms` 里（[configs/ground3dlmm_eval_ground3d_scannet.py:L186-L189](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L186-L189)）——这正是 u1-l4 讲过的 `pipeline.2.transforms` 点路径的落点。

#### 4.4.4 代码实践

**实践目标**：验证归一化公式与值域。

**操作步骤**（示例代码）：

```python
# 示例代码：用 5 个随机点验证 NormalizePointsColor_
import torch
from mmdet3d.structures.points import LiDARPoints
from uniseg3d.loading import NormalizePointsColor_

pts = LiDARPoints(torch.rand(5, 6) * 255, points_dim=6, attribute_dims={'color': 3})
out = NormalizePointsColor_(color_mean=[127.5, 127.5, 127.5]).transform({'points': pts})
color = out['points'].tensor[:, 3:]
print(color.min().item(), color.max().item())   # 期望都落在 [-1, 1]
```

**需要观察的现象**：打印的最小/最大值都在 \([-1, 1]\) 内；XYZ 三列不受影响。

**预期结果**：颜色列从 \([0,255]\) 变为 \([-1,1]\)，几何列原样。**待本地验证**（同上，依赖 mmdet3d 环境）。

#### 4.4.5 小练习与答案

**练习 1**：为什么是 127.5 而不是 128？

**答案**：255 的中点是 127.5，这样 0 与 255 对称地映射到 −1 与 +1；若用 128，255 会映射到约 0.992，白点略偏向负侧，破坏对称性。对视觉特征而言这点差异很小，但保持与 SPFormer 等前人工作一致的预处理有利于复用预训练权重。

**练习 2**：这个变换为什么放在 `GlobalRotScaleTrans` 之后也没问题？

**答案**：`GlobalRotScaleTrans` 只作用于坐标（XYZ），不碰颜色通道；反之归一化只改颜色列、不碰坐标。两者作用域不相交，顺序无关紧要。真正对顺序敏感的是「mask/标注加载必须在任何可能重排点序的操作之前」。

---

## 5. 综合实践

**任务：给一个「迷你场景」手工跑通「拼路径 → 读标注 → 归一化」三段链，并填写 `results` 键演变表。**

这个任务把 4.1~4.4 串起来，不需要真实数据集，只需要 u1-l2 装好的环境：

1. **造数据**：任选一个临时目录，仿照 4.3.4 写出 8 个点的 `super_points/f.bin`、`part_mask/f.bin`、`object_mask/f.bin`，再用 `np.random.rand(8,6)` 造一个 `points/f.bin`（示例代码）。
2. **模拟 parse_data_info**：手写一个 `info` 字典（含 `lidar_points.lidar_path`、三个 mask 裸文件名、`qa_data={'part_qa_data': [], 'object_qa_data': [], 'multi_turn_qa_data': []}`），然后照抄 [uniseg3d/scannet_dataset.py:L48-L71](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/scannet_dataset.py#L48-L71) 的逻辑手工拼接路径（不要 import 数据集类，避免触发完整初始化），确认每条路径指向你造的文件。
3. **跑加载与归一化**：把拼好的路径放进 `results`，依次执行 `LoadPointsFromFile`（或手工构造带 color 的点对象）→ `LoadAnnotations3D_` → `NormalizePointsColor_`。
4. **填表**：对照下表记录每个阶段结束后 `results` 里「新增了哪些键、每个键的 dtype/形状」：

| 阶段 | 新增键 | dtype / 形状 |
|---|---|---|
| 初始 results | `lidar_path`、三个 mask 路径、`scene_name`、`qa_data` | str / dict |
| `LoadPointsFromFile` | `points` | （待填写） |
| `LoadAnnotations3D_` | `sp_pts_mask`、`pts_part_mask`、`pts_object_mask`、`pts_semantic_mask`、`pts_instance_mask` | （待填写） |
| `NormalizePointsColor_` | （无新键，`points` 颜色列被改写） | （待填写） |

5. **预期结果**：表格填完后，你会得到一张「一条数据进模型前的完整体检单」——它正是下一讲 `AddSuperPointAnnotations_Part3DGlamm` 与 `QA_Generation_v2` 的输入清单。**待本地验证**（依赖 mmdet3d 环境；`LoadPointsFromFile` 若不便构造，可跳过并用 `LiDARPoints` 手工建点，重点在标注链）。

## 6. 本讲小结

- pkl 里只存**裸文件名**，目录由 `data_root + data_prefix` 在运行时拼接；`data_prefix` 四键（`pts`/`sp_pts_mask`/`pts_part_mask`/`pts_object_mask`）与磁盘四目录一一对应，pkl 字段、前缀键、目录三者必须两两对齐。
- `Part3DGlamm_Dataset.parse_data_info` 对父类只做**增量**：补三个 mask 路径（超点必填、part/object 可选）、透传 `qa_data`、从 `lidar_path` 提取 `scene_name`，并把场景采样改成恒等采样。
- `LoadAnnotations3D_` 靠**重写 `_load_masks_3d` + 多态**替换父类 mask 逻辑：四类 mask 按「有路径键才读、缺失置 None」加载，统一按 `int64` 解码裸 bin（写端 dtype 是隐含契约）；`with_sp_mask_3d` 再追加超点标签（含修复 read-only 的 `.copy()`），并同步进 `eval_ann_info` 供评估用。
- `NormalizePointsColor_` 把 RGB 从 \([0,255]\) 归一化到 \([-1,1]\)（\((c-127.5)/127.5\)），相比上游多了一个可配置的 `color_std`。
- 加载顺序有讲究：mask 在几何增强**之前**加载，靠「逐点索引对齐 + 增强不重排点序」维持点与标签的对应。

## 7. 下一步学习建议

本讲结束时，`results` 里已经躺着 `sp_pts_mask`、`pts_part_mask`、`pts_object_mask` 和 `qa_data`，但它们还是「原始材料」：模型真正需要的超点级 GT 分割（`gt_sp_masks`/`sp_gt_seg`）与被选中的 QA（`selected_qa_data`）尚未产生。下一讲 **u2-l4「QA 采样与超点标注生成（QA_Generation_v2）」** 正是处理这两个 transform（`AddSuperPointAnnotations_Part3DGlamm` 与 `QA_Generation_v2`，见 [uniseg3d/transforms_3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py)）的关键环节，建议先带着一个问题去读：答案里的 `<p>...</p>` 标签数量是如何决定一张 GT mask 的形状的？
