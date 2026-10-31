# u2-l2 数据集注册与准备

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 detectron2 的 `DatasetCatalog`（数据在哪、怎么读）与 `MetadataCatalog`（数据是什么、有多少类）这两个全局注册表的分工，以及「import 即注册」的触发链条。
2. 精读 `register_ade20k_panoptic.py`，理解一个 `register_*` 函数做了哪两件事、`_PREDEFINED_SPLITS` 预定义了什么、`get_metadata()` 如何构建类别表与 id 映射。
3. 读懂 `datasets/prepare_ade20k_pan_seg.py`：它如何把官方的「语义标注 + 实例标注」两张 PNG 合成为 COCO 全景格式的 PNG + JSON，以及它最终在磁盘上生成哪些文件。
4. 记住各数据集的 `evaluator_type` 元数据取值，并知道它在哪里被消费（`train_net.py` 的 `build_evaluator`）。
5. 在**不下载完整数据集**的情况下，用 dry-run 脚本验证注册逻辑、从 `MetadataCatalog` 打印 ADE20K 全景数据集的类别数量。

## 2. 前置知识

- **DatasetCatalog（数据集目录）**：一个全局字典，键是数据集名字符串（如 `"ade20k_panoptic_train"`），值是一个**加载函数**。调用这个函数才会真正去读磁盘，返回 detectron2 标准格式的 `list[dict]`（每个 dict 描述一张图：图片路径、标注路径、标注内容）。它是**惰性**的——注册时只存函数，不碰任何文件。
- **MetadataCatalog（元数据目录）**：同样是全局字典，但存的是**静态描述信息**：类别名 `thing_classes` / `stuff_classes`、颜色表、`evaluator_type`、忽略标签 `ignore_label` 等。设置元数据**不需要数据集真的在磁盘上**——这正是本讲 dry-run 实践的原理。
- **thing / stuff**：全景分割把类别分成两类——有清晰个体边界、可数的叫 **thing**（人、车、床）；铺满区域、不可数的叫 **stuff**（天空、墙面、道路）。ADE20K 的 150 类中有 100 个 thing 类、50 个 stuff 类（本讲实践会验证）。
- **COCO 全景格式**：每张图配一张 PNG（每个分割段涂一种唯一颜色）和一个 JSON（记录每个段的 `id`、`category_id`、`area`、`bbox`）。段 id 按 panopticapi 的约定编码为 \(\text{id} = \text{category\_id} \times 1000 + k\)（\(k\) 为该类别内的递增计数），这也是注册时 `label_divisor=1000` 元数据的由来。
- **DETECTRON2_DATASETS 环境变量**：所有内置数据集的根目录。未设置时默认是当前工作目录下的 `./datasets`（见 [datasets/README.md:L10-L22](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/datasets/README.md#L10-L22)，说明根目录约定与四个数据集子目录）。
- 承接 [u1-l3](u1-l3-repo-structure.md)：本仓库是 detectron2 插件项目，「import 即注册」的副作用机制是理解本讲的前提；承接 [u2-l1](u2-l1-training-entry-train-net.md)：`Trainer.build_evaluator` 与 `build_train_loader` 正是本讲注册结果的消费方。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mask2former/data/datasets/__init__.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/__init__.py#L1-L10) | import 全部 7 个注册模块，是注册的「总开关」 |
| [mask2former/data/datasets/register_ade20k_panoptic.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L1-L390) | 本讲主角：ADE20K 全景数据集的类别表、加载函数与注册 |
| [datasets/prepare_ade20k_pan_seg.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/datasets/prepare_ade20k_pan_seg.py#L1-L500) | 预处理脚本：官方语义 + 实例标注 → COCO 全景格式 |
| [datasets/README.md](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/datasets/README.md#L89-L120) | 各数据集的期望目录结构与准备步骤说明 |
| [train_net.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L67-L105) | `build_evaluator`：`evaluator_type` 的消费方 |
| [mask2former/data/datasets/register_coco_panoptic_annos_semseg.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_coco_panoptic_annos_semseg.py#L129-L181) | 对照参考：COCO 的注册脚本，展示「嫁接在 detectron2 内置注册之上」的另一种模式 |

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：双 Catalog 机制、`register_ade20k_panoptic` 注册函数、`load_ade20k_panoptic_json` 惰性加载、`prepare_ade20k_pan_seg.py` 预处理脚本、`evaluator_type` 全景。

### 4.1 双 Catalog 机制与「import 即注册」链条

#### 4.1.1 概念说明

detectron2 用两个**进程级全局单例**管理数据集：

- `DatasetCatalog` 管「**数据**」：名字 → 加载函数。训练 / 评估时数据加载器按名字取出函数、调用它拿到 `list[dict]`。
- `MetadataCatalog` 管「**描述**」：名字 → 属性集。可视化要颜色表、模型要类别数、`build_evaluator` 要 `evaluator_type`，都从这里查。

这套设计的关键好处是**解耦与惰性**：注册一个数据集只是往两个字典里塞条目，完全不需要数据在磁盘上；只有真正取数（`DatasetCatalog[name]`）时才会读 JSON、检查文件是否存在。因此「注册」可以在任何机器上完成（dry-run 友好），「加载」才依赖真实数据。

#### 4.1.2 核心流程

注册的触发链条（承接 u1-l3 的「import 即注册」）：

```text
import mask2former                        # 入口脚本 train_net.py 第一行相关 import
  └─ mask2former/__init__.py:  from . import data
       └─ mask2former/data/__init__.py:  from . import datasets
            └─ mask2former/data/datasets/__init__.py:  import 7 个 register_* 模块
                 └─ 每个模块底部:  _root = os.getenv("DETECTRON2_DATASETS", "datasets")
                                register_all_xxx(_root)     # 模块顶层语句，import 时立即执行
```

也就是说：**只要 import 了 mask2former 包，7 个数据集注册模块的顶层代码就会执行**，几十个数据集名字（含各 train/val split）就被登记进两个 Catalog。反过来说，漏掉这个 import，配置里写的数据集名就会报「Dataset xxx is not registered」。

#### 4.1.3 源码精读

链条第一环——[mask2former/__init__.py:L2](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/__init__.py#L2)（`from . import data  # register all new datasets`，注释直接点明这一步的目的就是注册数据集）：

[mask2former/data/__init__.py:L2](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/__init__.py#L2) 把 `datasets` 子包引入：

[mask2former/data/datasets/__init__.py:L2-L10](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/__init__.py#L2-L10) 依次 import 7 个注册模块：`register_ade20k_full`、`register_ade20k_panoptic`、`register_coco_stuff_10k`、`register_mapillary_vistas`、`register_coco_panoptic_annos_semseg`、`register_ade20k_instance`、`register_mapillary_vistas_panoptic`。这一步执行后，所有注册模块的顶层代码都跑完了。

而每个注册模块的最后一行都是同样的模式，以 ADE20K 为例（[register_ade20k_panoptic.py:L389-L390](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L389-L390)）：读取 `DETECTRON2_DATASETS` 环境变量（默认 `"datasets"`），然后立即调用 `register_all_ade20k_panoptic(_root)` 把该数据集的全部 split 注册进 Catalog。**这行代码在 import 时执行，不在任何函数里**——这就是「import 即注册」的落点。

#### 4.1.4 代码实践

**实践目标**：不下载任何数据，验证「import 即注册」确实发生了。

**操作步骤**：在仓库根目录新建 `inspect_catalog.py`（示例代码，本讲所有脚本都是示例代码，仓库中不存在）：

```python
# 示例代码：inspect_catalog.py
import mask2former  # 触发注册；CPU 环境即可，无需编译 CUDA 算子
from detectron2.data import DatasetCatalog, MetadataCatalog

names = DatasetCatalog.list()
ade = sorted(n for n in names if "ade20k" in n)
print(f"注册的数据集总数: {len(names)}")
print("含 ade20k 的名字:")
for n in ade:
    print("  ", n)
```

**需要观察的现象**：import 之后没有任何读盘、下载动作；打印出的 `ade20k` 相关名字中应包含 `ade20k_panoptic_train`、`ade20k_panoptic_val`、`ade20k_sem_seg_train` 等。

**预期结果**：数据集名被登记，但此时磁盘上并没有 `ADEChallengeData2016/` 目录——证明注册与数据下载是两回事。具体数字取决于 detectron2 版本内置的数据集，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `train_net.py` 里 `import mask2former` 相关语句全部删掉，配置里 `DATASETS.TRAIN: ("ade20k_panoptic_train",)` 会发生什么？
**答案**：注册链条断裂，`DatasetCatalog` 里没有 `ade20k_panoptic_train` 这个名字，构建训练数据加载器时会抛出「dataset not registered」类错误；模型注册（`META_ARCHITECTURE_REGISTRY`）同样会失败（u1-l3 讲过的联动效应）。

**练习 2**：`DatasetCatalog` 和 `MetadataCatalog` 哪一个可以在没有数据集文件的情况下完整查询？为什么？
**答案**：`MetadataCatalog`。因为注册元数据只是往属性集里写字符串 / 列表，不碰磁盘；而 `DatasetCatalog` 里存的是加载函数，调用（即 `DatasetCatalog[name]` 取数）时才会读 JSON 并断言文件存在。

---

### 4.2 `register_ade20k_panoptic`：预定义 split 与元数据构建

#### 4.2.1 概念说明

一个 `register_*` 函数的标准动作只有两步：

1. `DatasetCatalog.register(name, loader)`——把加载函数以数据集名登记（惰性）；
2. `MetadataCatalog.get(name).set(...)`——写入元数据：各类根目录路径、`evaluator_type`、`ignore_label`、`label_divisor`，以及展开的 `**metadata`（类别名、颜色、id 映射）。

而「注册哪些名字、每个名字对应磁盘上哪些路径」由模块级的 `_PREDEFINED_SPLITS_*` 字典预先写死。这是 detectron2 生态里内置数据集的标准写法：**数据集名 → 相对路径**的映射表 + 一个循环注册全部条目。

#### 4.2.2 核心流程

```text
_PREDEFINED_SPLITS_ADE20K_PANOPTIC（名字 → 5 元组相对路径）
        │  对每个条目
        ▼
register_ade20k_panoptic(name, metadata, image_root, panoptic_root,
                         semantic_root, panoptic_json, instances_json)
        ├── DatasetCatalog.register(name, lambda: load_ade20k_panoptic_json(...))
        └── MetadataCatalog.get(name).set(
                panoptic_root, image_root, panoptic_json, json_file,
                evaluator_type="ade20k_panoptic_seg",
                ignore_label=255, label_divisor=1000,
                **metadata)   # thing/stuff 类别表 + id 映射
```

其中 `metadata` 由 `get_metadata()` 一次性构建，被所有 split 共享。

#### 4.2.3 源码精读

**（1）预定义 split 表**。[register_ade20k_panoptic.py:L308-L323](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L308-L323)（`_PREDEFINED_SPLITS_ADE20K_PANOPTIC`：每个数据集名对应一个 5 元组——图片目录、全景 PNG 目录、全景 JSON、**detectron2 格式语义标注目录**、实例 JSON，全部是相对 `DETECTRON2_DATASETS` 根目录的路径）。注意第 4 项 `annotations_detectron2/...` 不是官方发布的内容，而是由另一个脚本 `prepare_ade20k_sem_seg.py` 生成的语义标注（见 [datasets/README.md:L97-L106](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/datasets/README.md#L97-L106)）。

**（2）注册函数本体**。[register_ade20k_panoptic.py:L271-L305](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L271-L305)（先 `DatasetCatalog.register` 登记惰性加载函数，再 `MetadataCatalog.get(...).set(...)` 写元数据）。两处关键：

- [L290-L295](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L290-L295)：用 `lambda` 把 `load_ade20k_panoptic_json` 和它的 5 个参数打包注册——**此刻函数不会执行**。
- [L296-L305](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L296-L305)：写入 `evaluator_type="ade20k_panoptic_seg"`、`ignore_label=255`（训练时 255 像素不参与损失）、`label_divisor=1000`（全景段 id 按 \(\text{category\_id} \times 1000 + k\) 编码的基数约定），最后 `**metadata` 展开塞入类别表与两张 id 映射表。

**（3）元数据构建 `get_metadata()`**。[register_ade20k_panoptic.py:L326-L367](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L326-L367)。三件事：

- [L334-L342](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L334-L342)：从 `ADE20K_150_CATEGORIES`（模块顶部 L8-L204 的 150 条类别表，每条含 `name/id/isthing/color`）抽取 `thing_classes`（只取 `isthing == 1` 的 **100** 个）与 `stuff_classes`（**全部 150** 个——语义分割要预测所有类，所以 stuff 表包含 thing 类，代码注释解释了这种「双份命名」是为了复用 detectron2 的可视化函数）。
- [L352-L365](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L352-L365)：构建**两张 id 映射表**。遍历 150 个类别（`i` 为在 150 类列表中的下标）：

  \[
    \text{thing\_map}[\text{cat.id}] = i \;(\text{仅 isthing}), \qquad \text{stuff\_map}[\text{cat.id}] = i \;(\forall\, \text{cat})
  \]

  「原始 dataset id → 训练用连续 id」的转换是集合预测模型训练的必要步骤——模型的分类头需要 `[0, num_classes)` 的连续标签。注意一个细节：ADE20K 的 thing 连续 id **没有**压缩成 `[0,100)`，而是保留其在 150 类表中的原位下标（与 stuff 表对齐），这样同一套类别空间可以同时服务语义 / 全景两个任务（注释 L361 说明这是为了复用 sem_seg evaluator）。

**（4）批量注册与根目录**。[register_ade20k_panoptic.py:L370-L386](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L370-L386)（`register_all_ade20k_panoptic`：构建一次 `metadata`，循环把每个 split 的相对路径拼上 `root` 后注册）。

**（5）对照：COCO 的另一种注册模式**。[register_coco_panoptic_annos_semseg.py:L129-L157](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_coco_panoptic_annos_semseg.py#L129-L157) 展示了嫁接模式：它**先 `delattr` 删掉 detectron2 内置 COCO 条目的 `thing_classes`/`thing_colors` 再覆盖成自己的**（L133-L139），并把加载函数注册到新名字 `name + "_with_sem_seg"` 下（L142-L146）；[L165-L167](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_coco_panoptic_annos_semseg.py#L165-L167) 直接从内置条目「偷」`image_root` 和 `json_file`。效果见 [Base-COCO-PanopticSegmentation.yaml:L17-L18](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml#L17-L18)：训练用 `coco_2017_train_panoptic`（内置名 + 本仓库替换的元数据），测试用 `coco_2017_val_panoptic_with_sem_seg`（本仓库新注册的带语义标注变体）。**ADE20K 则完全自包含**，不依赖 detectron2 的内置 ADE20K 条目——这是两种注册风格的典型差异。

#### 4.2.4 代码实践

**实践目标**：dry-run 打印 ADE20K 全景数据集的 `thing_classes` / `stuff_classes` 数量与 `evaluator_type`（本讲主实践之一）。

**操作步骤**：在仓库根目录新建 `inspect_ade20k_meta.py`（示例代码）：

```python
# 示例代码：inspect_ade20k_meta.py
import mask2former  # 触发注册
from detectron2.data import MetadataCatalog

for name in ["ade20k_panoptic_train", "ade20k_panoptic_val"]:
    meta = MetadataCatalog.get(name)
    print(f"== {name} ==")
    print("  thing_classes 数量:", len(meta.thing_classes))
    print("  stuff_classes 数量:", len(meta.stuff_classes))
    print("  evaluator_type   :", meta.evaluator_type)
    print("  ignore_label     :", meta.ignore_label)
    print("  label_divisor    :", meta.label_divisor)
    print("  panoptic_json    :", meta.panoptic_json)

meta = MetadataCatalog.get("ade20k_panoptic_train")
print("thing 前 3 类:", meta.thing_classes[:3])
print("stuff 前 3 类:", meta.stuff_classes[:3])
print("thing id 映射（前 3 项）:",
      list(meta.thing_dataset_id_to_contiguous_id.items())[:3])
```

**需要观察的现象**：脚本秒级完成，无任何 IO；`panoptic_json` 等路径打印出来指向 `datasets/ADEChallengeData2016/...`（或 `DETECTRON2_DATASETS` 指向的根目录），即使该目录不存在也不报错。

**预期结果**：`thing_classes` 为 **100**、`stuff_classes` 为 **150**、`evaluator_type` 为 `ade20k_panoptic_seg`、`ignore_label` 为 `255`。thing id 映射的值应能看到「跳跃」（不是 0,1,2…连续，而是 150 类表中的原位下标）。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `stuff_classes` 有 150 项而 `thing_classes` 只有 100 项？stuff 表里包含 thing 类会不会有问题？
**答案**：语义分割要给全部 150 类各预测一张概率图，所以 stuff 表取全部类别；thing 表只服务于「可数个体」的实例 / 全景分支。不冲突——两个表喂给模型的不同部分，代码注释（L329-L333）说明双份命名是为了兼容 detectron2 现有可视化函数按 thing/stuff 分别处理的逻辑。

**练习 2**：`register_ade20k_panoptic` 的 `instances_json` 参数被存进了哪个元数据键？后续谁会用它？
**答案**：存为 `json_file`（L300）。它是 COCO 格式实例标注（`ade20k_instance_train.json`，由 `prepare_ade20k_ins_seg.py` 生成），供实例分割评估器（如 `InstanceSegEvaluator` / COCO api）使用。

---

### 4.3 `load_ade20k_panoptic_json`：惰性加载与标准 dict 格式

#### 4.3.1 概念说明

加载函数是 `DatasetCatalog` 里那个「 deferred 的承诺」。它被调用时读全景 JSON，把每张图转成 detectron2 **标准 dict 格式**——后续 `DatasetMapper`（下一讲 u2-l3 的主角）吃的就是这个格式。对全景分割，每条记录比普通检测任务多两个字段：`pan_seg_file_name`（全景 PNG 路径）和 `sem_seg_file_name`（语义 PNG 路径），外加 `segments_info`（该图所有段的类别与连续 id）。

#### 4.3.2 核心流程

```text
DatasetCatalog["ade20k_panoptic_train"] 被首次访问
  └─ load_ade20k_panoptic_json(json, image_root, panoptic_root, semseg_root, meta)
       ├─ 读 JSON 的 "annotations" 数组
       ├─ 逐条: 拼 image/panoptic/sem_seg 三个文件路径
       ├─ 逐段: _convert_category_id —— 原始 category_id → 连续 id，并打上 isthing 布尔
       └─ 断言: 记录非空；首条记录的三个文件都真实存在
  ⇒ 返回 list[dict]
```

#### 4.3.3 源码精读

**（1）id 转换**。[register_ade20k_panoptic.py:L228-L239](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L228-L239)（`_convert_category_id`：若段的 `category_id` 命中 thing 映射表则换成 thing 连续 id 并置 `isthing=True`，否则换 stuff 连续 id 并置 `isthing=False`）。JSON 里存的是 dataset 原始 id，训练用的连续 id 在加载期完成翻译。

**（2）主函数**。[register_ade20k_panoptic.py:L241-L268](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L241-L268)：

- [L251-L253](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L251-L253)：三条路径拼接——图片把扩展名强制换成 `.jpg`（L247-L250 的 TODO 注释承认这是对 COCO 的硬编码假设），全景与语义 PNG 直接用 annotation 里的 `file_name`。
- [L255-L263](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L255-L263)：组装标准 dict：`file_name` / `image_id` / `pan_seg_file_name` / `sem_seg_file_name` / `segments_info` 五个键。
- [L264-L267](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L264-L267)：三连断言——记录非空、**首条记录**的三个文件都存在。注意只抽查第一条：这是「尽早失败」的廉价设计，数据中途缺文件会在更晚的 mapper 阶段才爆。

#### 4.3.4 代码实践

**实践目标**：亲眼验证「惰性」——注册成功 ≠ 能加载；缺数据时加载在断言处失败。

**操作步骤**：把下面几行追加到 4.2.4 的脚本末尾（示例代码）：

```python
from detectron2.data import DatasetCatalog

print("已注册?", "ade20k_panoptic_val" in DatasetCatalog.list())  # True
try:
    dicts = DatasetCatalog["ade20k_panoptic_val"]  # 这里才真正触发读盘
    print("加载成功，样本数:", len(dicts))
    print("首条记录:", dicts[0])
except (AssertionError, FileNotFoundError) as e:
    print("加载失败（预期，若无数据）:", type(e).__name__, str(e)[:120])
```

**需要观察的现象**：第一行打印 `True`；如果本地没有 ADE20K 数据，取数这行抛出 `AssertionError`（来自 L265-L267 的断言）或 JSON 不存在的 `FileNotFoundError`；如果有数据，则打印样本数（官方 val 为 2000 张）与首条记录的五个键。

**预期结果**：注册与加载的边界清晰可辨。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么注册时不顺便把 JSON 读进来，而要拖到取数时？
**答案**：（a）进程启动速度——注册几十个数据集但通常只用一两个；（b）训练中每个 worker 各自惰性取数，天然适配分布式；（c）让「改代码 / dry-run 配置」不依赖几百 GB 的数据下载。

**练习 2**：`_convert_category_id` 为什么要把 `isthing` 信息写回 `segments_info`？
**答案**：下游的 `COCOPanopticNewBaselineDatasetMapper` 生成训练 GT 时需要区分 thing 段（每个实例一个 mask、参与匹配）与 stuff 段（一类一个 mask），这个布尔标记免得 mapper 再查一遍类别表。

---

### 4.4 `prepare_ade20k_pan_seg.py`：从语义 + 实例标注合成全景标注

#### 4.4.1 概念说明

ADE20K 官方发布的是**语义标注**（每像素一个类别 id）和**实例标注**（另需单独下载，PNG 两个通道分别存类别与实例编号）——但全景分割训练需要 COCO 全景格式（一图一 PNG + 一 JSON，每个段有独立 id / area / bbox）。这个脚本做的就是「1 + 1 = 全景」的格式转换：

- **stuff 段**来自语义标注：每个 stuff 类的全部像素连成一个段；
- **thing 段**来自实例标注：每个实例一个段，类别通过映射文件换算成语义类别 id。

`isthing` 的判定也在这里完成：某语义类别若出现在「实例类别 → 语义类别」映射的目标集合里，它就是 thing 类。

#### 4.4.2 核心流程

```text
对 train / val 两个 split：
  遍历 images/{training,validation}/*.jpg（排序后）
    ├─ 读语义 PNG、实例 PNG → 两张 id 矩阵（像素值减 1 对齐 0 起始类别 id）
    ├─ 生成 stuff 段：语义标注中每个非 thing 类别的连通区域
    │    · IdGenerator 分配 (segment_id, color)，颜色写入 pan_seg 画布
    │    · 断言 stuff 段之间无重叠；计算 area 与 bbox
    ├─ 生成 thing 段：实例标注中每个非零实例编号
    │    · 类别 = map_id[实例类别]，覆盖写入 pan_seg（thing 优先于 stuff）
    ├─ 保存 panoptic PNG 到 ade20k_panoptic_{train,val}/
    └─ 累积 images / annotations / categories
  写出 ade20k_panoptic_{train,val}.json
```

两个关键源码级细节：**先贴 stuff 再贴 thing**（官方两类标注存在重叠，后写入的 thing 像素覆盖 stuff）；**类别映射表来自仓库自带文件**，id 统一减 1（官方 1 起始 → 程序内 0 起始）。

#### 4.4.3 源码精读

**（1）入口与目录**。[prepare_ade20k_pan_seg.py:L321-L338](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/datasets/prepare_ade20k_pan_seg.py#L321-L338)（`__main__` 入口：从 `DETECTRON2_DATASETS` 取根目录，为 `train/val` 两 split 分别定位 `images/`、`annotations/`（语义）、`annotations_instance/`（实例）三个输入目录，并创建输出目录 `ade20k_panoptic_{name}/`）。输出 JSON 路径在 [L334](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/datasets/prepare_ade20k_pan_seg.py#L334)。

**（2）两个仓库自带的配置文件**。[prepare_ade20k_pan_seg.py:L341-L355](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/datasets/prepare_ade20k_pan_seg.py#L341-L355)：读取 `datasets/ade20k_instance_imgCatIds.json`（其实只声明了用途）与 `datasets/ade20k_instance_catid_mapping.txt`（实例类别 id → 语义类别 id 的逐行映射，首行是表头要跳过），并做 \(\text{map}(x) = x - 1\) 的平移（代码注释：让 id 从 0 开始，255 作为 ignore label）。注意这两个是**相对路径**，脚本必须从仓库根目录运行，否则 `open` 直接失败。

**（3）类别表与 isthing 判定**。[prepare_ade20k_pan_seg.py:L357-L367](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/datasets/prepare_ade20k_pan_seg.py#L357-L367)（用脚本顶部的 150 个类别名与调色板构建 `ADE20K_150_CATEGORIES`，其中 `"isthing": int(cat_id in map_id.values())`——类别 id 出现在实例→语义映射的目标集合中即为 thing 类）。这份表与注册脚本里的表内容一致，只是两处各自维护了一份。

**（4）读入两种标注**。[prepare_ade20k_pan_seg.py:L383-L405](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/datasets/prepare_ade20k_pan_seg.py#L383-L405)：打开原图取宽高；建 RGB 画布 `pan_seg`；`IdGenerator`（来自 panopticapi）负责发号；语义 PNG 减 1 得 `semantic_cat_ids`，实例 PNG 第 0 通道减 1 得类别、第 1 通道直接是实例编号（[L403-L405](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/datasets/prepare_ade20k_pan_seg.py#L403-L405) 的注释说明：实例 id 从 1 开始，0 保留为 VOID）。

**（5）先 stuff 后 thing**。[L409-L423](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/datasets/prepare_ade20k_pan_seg.py#L409-L423)（注释点明语义与实例标注有重叠，因此先贴 stuff；对语义标注里每个非 thing 类别生成 mask，`assert pan_seg[mask].sum() == 0` 保证 stuff 之间互不重叠，随后 `get_id_and_color` 发号并涂色）。[L425-L445](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/datasets/prepare_ade20k_pan_seg.py#L425-L445) 用行列求和的方式算 bbox（先按列投影取 x 范围、再按行投影取 y 范围），并记录 `id/category_id/area/bbox/iscrowd`。thing 段循环在 [L447-L480](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/datasets/prepare_ade20k_pan_seg.py#L447-L480)：断言每个实例内类别唯一（L453），经 `map_id` 换算成语义类别后覆盖写入画布——**这就是 thing 像素覆盖重叠 stuff 像素的位置**。

**（6）产出落盘**。[prepare_ade20k_pan_seg.py:L482-L500](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/datasets/prepare_ade20k_pan_seg.py#L482-L500)（每图保存 `ade20k_panoptic_{split}/{image_id}.png`；split 处理完后用 panopticapi 的 `save_json` 写出包含 `images/annotations/categories` 三个数组的 JSON——正是 4.3 节加载函数要读的那个文件）。

#### 4.4.4 代码实践

**实践目标**：写出 `prepare_ade20k_pan_seg.py` 运行后 `ADEChallengeData2016/` 的目录结构清单，标注每项的来源（官方下载 / 哪个脚本生成）——本讲主实践之二。

**操作步骤**：

1. 阅读 [datasets/README.md:L89-L120](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/datasets/README.md#L89-L120) 与脚本 L321-L334，交叉比对；
2. 对照 `_PREDEFINED_SPLITS_ADE20K_PANOPTIC`（L308-L323）确认注册方需要哪些路径真实存在；
3. 写出自己的清单，格式如下（这就是参考答案，读者应先自己写再对照）：

```text
ADEChallengeData2016/
  images/                            # 官方下载：training/ validation/ 两 split 的 jpg
  annotations/                       # 官方下载：语义分割 PNG
  objectInfo150.txt                  # 官方下载：150 类类别信息
  annotations_instance/              # 官方单独下载（README L113-L115 的 wget）
  annotations_detectron2/            # 由 prepare_ade20k_sem_seg.py 生成（README L106）
  ade20k_panoptic_train.json         # ★ 本脚本生成：全景标注 JSON
  ade20k_panoptic_train/             # ★ 本脚本生成：全景 PNG
  ade20k_panoptic_val.json           # ★ 本脚本生成
  ade20k_panoptic_val/               # ★ 本脚本生成
  ade20k_instance_{train,val}.json   # 由 prepare_ade20k_ins_seg.py 生成（README L120）
```

4. 若本地已下载 ADE20K：安装 `pip install git+https://github.com/cocodataset/panopticapi.git` 后在**仓库根目录**执行 `python datasets/prepare_ade20k_pan_seg.py`，观察 tqdm 进度与生成文件。

**需要观察的现象**：生成的 JSON 中 `categories` 数组有 150 条、含 `isthing` 字段；随便打开一张 `ade20k_panoptic_train/*.png`，像素是 RGB 颜色而非类别 id。

**预期结果**：目录清单与上面一致；注册脚本期望的 5 个路径（`images/training`、`ade20k_panoptic_train/`、`ade20k_panoptic_train.json`、`annotations_detectron2/training`、`ade20k_instance_train.json`）全部能对上号。未下载数据时只完成第 1-3 步即可。

#### 4.4.5 小练习与答案

**练习 1**：为什么 stuff 段之间可以 `assert pan_seg[mask].sum() == 0`，而 thing 段不行？
**答案**：语义标注里每个像素只有一个类别 id，不同 stuff 类的 mask 天然互斥；但官方语义标注与实例标注之间存在重叠（同一条注释 L409-L410），实例 mask 可能落在 stuff 区域上，所以 thing 必须后贴、允许覆盖。

**练习 2**：把 `thing` 和 `stuff` 的处理顺序对调会发生什么？
**答案**：重叠区域的归属反转——stuff 会覆盖 thing 像素，实例 mask 被侵蚀，`segments_info` 里 thing 段的 `area/bbox` 与画布内容不一致，训练 GT 质量下降。顺序是一个有语义的决策，不是随手写的。

**练习 3**：脚本为什么依赖两个仓库内文件（`ade20k_instance_imgCatIds.json`、`ade20k_instance_catid_mapping.txt`）而不是硬编码映射？
**答案**：实例标注的类别体系与语义 150 类体系不同，二者关系由官方映射文件描述；用数据文件而非硬编码便于跟随官方更新，也让「isthing = 出现在映射目标集合」这一定义有据可查。

---

### 4.5 `evaluator_type` 与数据集全景

#### 4.5.1 概念说明

`evaluator_type` 是挂在每个数据集元数据上的一枚「任务标签」。`train_net.py` 的 `build_evaluator`（u2-l1 讲过它是 `Trainer` 覆写的四个方法之一）拿到数据集名后第一件事就是查这枚标签，再配合 `MODEL.MASK_FORMER.TEST` 的三个开关（`SEMANTIC_ON` / `INSTANCE_ON` / `PANOPTIC_ON`，u6 会细讲）组合出评估器列表。**注册时写对这枚标签，评估期就能自动分发**——这就是元数据驱动的设计。

#### 4.5.2 核心流程

```text
build_evaluator(cfg, dataset_name)
  └─ evaluator_type = MetadataCatalog.get(dataset_name).evaluator_type
       ├─ "sem_seg" / "ade20k_panoptic_seg"      → SemSegEvaluator（当对应开关开）
       ├─ "coco_panoptic_seg"                     → COCOPanopticEvaluator / COCOEvaluator / SemSegEvaluator
       ├─ "mapillary_vistas_panoptic_seg"         → InstanceSegEvaluator / SemSegEvaluator
       ├─ "cityscapes_*"                          → Cityscapes 系列
       └─ "lvis"                                  → LVISEvaluator
```

#### 4.5.3 源码精读

**（1）消费点**。[train_net.py:L79](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L79)（`evaluator_type = MetadataCatalog.get(dataset_name).evaluator_type`——本讲所有注册工作的最终买单处）。分发逻辑在 [train_net.py:L81-L105](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L81-L105)：注意 `ade20k_panoptic_seg` 与 `sem_seg` 共用语义评估入口（L81-L87），全景评估对四种 `*_panoptic_seg` 统一放行（L93-L100）；ADE20K 专属的实例评估在 [L134-L135](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L134-L135)。

**（2）本仓库写下的标签一览**（由 grep 确认）：

| evaluator_type | 写入位置 | 数据集 |
| --- | --- | --- |
| `ade20k_panoptic_seg` | [register_ade20k_panoptic.py:L301](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L301) | ADE20K 全景 |
| `coco_panoptic_seg` | [register_coco_panoptic_annos_semseg.py:L153](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_coco_panoptic_annos_semseg.py#L153) | COCO 全景 |
| `mapillary_vistas_panoptic_seg` | [register_mapillary_vistas_panoptic.py:L422](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_mapillary_vistas_panoptic.py#L422) | Mapillary 全景 |
| `sem_seg` | [register_ade20k_full.py:L958](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_full.py#L958)、[register_mapillary_vistas.py:L500](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_mapillary_vistas.py#L500)、[register_coco_stuff_10k.py:L216](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_coco_stuff_10k.py#L216) | 各纯语义数据集 |
| `cityscapes_instance` / `cityscapes_sem_seg` / `cityscapes_panoptic_seg` | detectron2 内置注册（非本仓库文件） | Cityscapes |

另有一个模块顶层的「补丁」值得注意：[register_ade20k_panoptic.py:L206-L214](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L206-L214) 在 import 时给 `ade20k_sem_seg_train/val` 两个（detectron2 内置的）语义数据集条目补上 `stuff_colors`——元数据不仅可以注册时写，还可以事后增量 `.set()`。

#### 4.5.4 代码实践

**实践目标**：推演 `ade20k_panoptic_val` 在默认 ADE20K 配置下会得到哪几个评估器。

**操作步骤**：

1. 打开 ADE20K 全景基线配置 `configs/ade20k/panoptic-segmentation/maskformer2_R50_bs16_160k.yaml`，顺着 `_BASE_` 找到 `MODEL.MASK_FORMER.TEST` 的三个开关取值；
2. 手动沿 [train_net.py:L79-L147](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L79-L147) 走一遍：`evaluator_type == "ade20k_panoptic_seg"` 会命中哪些 `if`；
3. 用 4.2.4 的脚本加一行确认标签：`print(MetadataCatalog.get("ade20k_panoptic_val").evaluator_type)`。

**需要观察的现象**：`ade20k_panoptic_seg` 同时命中 L81（SemSegEvaluator）与 L93-L100 的全景分支（PANOPTIC_ON 时加 COCOPanopticEvaluator），INSTANCE_ON 时再经 L134 加 InstanceSegEvaluator。

**预期结果**：三个开关全开时得到 3 个评估器组成的 `DatasetEvaluators`。具体取值待本地验证（取决于 Base yaml 的默认开关）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 ADE20K 的实例评估不直接用 detectron2 的 `COCOEvaluator`，而用本仓库的 `InstanceSegEvaluator`（u7-l2 主题）？
**答案**：Mask2Former 的实例输出没有真实边界框（只有占位 `pred_boxes`），`InstanceSegEvaluator` 继承 `COCOEvaluator` 并把输入约定改成 mask-first，避免按框评估失真。这是模型范式（mask classification）向评估侧的传导。

**练习 2**：如果你的数据集 `evaluator_type` 拼错了，会在什么时候、以什么方式失败？
**答案**：训练照常，评估构建时 `build_evaluator` 里 `evaluator_list` 为空，落到 [train_net.py:L139-L144](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L139-L144) 的 `NotImplementedError`（"no Evaluator for the dataset ... with the type ..."）。注册期不校验标签值——又一个「惰性失败」的例子。

---

## 5. 综合实践

**任务**：写一个一站式 dry-run 脚本 `u2l2_dry_run.py`，把本讲四个环节串起来——验证注册链、打印元数据、探测惰性加载边界、输出目录清单核对。全程**不需要**下载 ADE20K（约数 GB）。

```python
# 示例代码：u2l2_dry_run.py —— 在仓库根目录运行: python u2l2_dry_run.py
import os
# 故意指向一个空目录，证明注册不依赖真实数据
os.environ["DETECTRON2_DATASETS"] = os.path.abspath("./_empty_datasets_root")
os.makedirs(os.environ["DETECTRON2_DATASETS"], exist_ok=True)

import mask2former  # 第 1 环：import 即注册
from detectron2.data import DatasetCatalog, MetadataCatalog

# 第 2 环：元数据查询
meta = MetadataCatalog.get("ade20k_panoptic_train")
report = {
    "thing_classes": len(meta.thing_classes),
    "stuff_classes": len(meta.stuff_classes),
    "evaluator_type": meta.evaluator_type,
    "ignore_label": meta.ignore_label,
    "label_divisor": meta.label_divisor,
    "image_root": meta.image_root,
    "panoptic_json": meta.panoptic_json,
    "sem_seg_root（注册参数 semantic_root）": meta.panoptic_root,  # 见下方思考题 2
}
for k, v in report.items():
    print(f"{k:44s} = {v}")

# 第 3 环：惰性加载边界
try:
    DatasetCatalog["ade20k_panoptic_train"]
    print("加载成功（不应发生，目录是空的）")
except Exception as e:
    print(f"[预期失败] {type(e).__name__}: {str(e)[:100]}")

# 第 4 环：核对注册方期望的磁盘路径 vs 预处理脚本生成的路径
expected = meta.image_root, meta.panoptic_root, meta.panoptic_json
print("注册方期望的三个路径（都应不存在）:", expected)
```

**预期结果**（待本地验证）：

- 第 2 环打印 `thing_classes=100`、`stuff_classes=150`、`evaluator_type=ade20k_panoptic_seg`、`ignore_label=255`、`label_divisor=1000`；
- 第 3 环抛 `FileNotFoundError`（空目录下找不到 `ade20k_panoptic_train.json`）或 `AssertionError`；
- 第 4 环的路径与 4.4.4 目录清单中带 ★ 的条目一一对应。

**思考题（附答案）**：

1. 把 `DETECTRON2_DATASETS` 指向空目录后脚本仍能完成第 2 环，说明什么？
   **答**：`MetadataCatalog.set()` 纯内存操作；路径只是字符串，不在注册期校验。
2. `report` 里 `sem_seg_root` 一栏实际打印的是 `panoptic_root`——去 [register_ade20k_panoptic.py:L296-L305](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L296-L305) 确认：注册函数并没有把 `semantic_root`（即 `annotations_detectron2/...`）写进元数据，它只作为参数被闭包进加载函数。真正的语义 PNG 路径出现在加载结果的 `sem_seg_file_name` 字段里。
3. 若现在把环境变量指回真实数据目录再跑第 3 环，还需要哪些前置脚本已经跑过？
   **答**：`prepare_ade20k_sem_seg.py`（生成 `annotations_detectron2/`）、`prepare_ade20k_pan_seg.py`（生成两个 ★ 条目）、`prepare_ade20k_ins_seg.py`（生成 `ade20k_instance_*.json`）。

## 6. 本讲小结

- detectron2 用 **双 Catalog** 管数据集：`DatasetCatalog` 存「名字 → 惰性加载函数」，`MetadataCatalog` 存「名字 → 静态描述」；注册不碰磁盘，取数才读文件。
- **import 即注册**链条：`import mask2former` → `data` → `datasets` → 7 个注册模块顶层执行 `register_all_*(root)`，根目录来自 `DETECTRON2_DATASETS`（默认 `./datasets`）。
- `register_ade20k_panoptic` 的两步标准动作：登记惰性 loader + 写元数据（`evaluator_type="ade20k_panoptic_seg"`、`ignore_label=255`、`label_divisor=1000`、100 个 thing 类 / 150 个 stuff 类、原始 id → 150 类表原位下标的连续 id 映射）。
- `prepare_ade20k_pan_seg.py` 把官方语义 PNG + 单独下载的实例 PNG 合成为 COCO 全景格式：**先贴 stuff（互斥有断言）后贴 thing（可覆盖）**，依赖仓库内两个映射文件，输出 `ade20k_panoptic_{train,val}/` PNG 与同名 JSON。
- `evaluator_type` 是任务标签，被 `train_net.py:79` 的 `build_evaluator` 消费，配合 `TEST` 三开关组合出评估器列表——注册侧写对一个字符串，评估侧全自动分发。
- COCO 注册脚本展示了与 ADE20K 不同的「嫁接」风格：删改 detectron2 内置条目元数据并注册 `*_with_sem_seg` 变体。

## 7. 下一步学习建议

注册与预处理只解决了「数据在哪、长什么样」；下一讲 **u2-l3 DatasetMapper** 将顺着本讲的标准 dict 格式往下走：`build_train_loader` 按 `INPUT.DATASET_MAPPER_NAME` 选出 5 种 mapper 之一，把 `file_name` / `pan_seg_file_name` / `segments_info` 变成模型真正吃的 `image tensor + Instances`。建议先精读 [mask2former/data/dataset_mappers/coco_panoptic_new_baseline_dataset_mapper.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/coco_panoptic_new_baseline_dataset_mapper.py#L1-L50) 的 `__call__`，留意它如何消费本讲 `segments_info` 里的 `isthing` 标记；若想先横向扩展数据集知识，可对照阅读 `register_mapillary_vistas_panoptic.py` 体会换一套数据集时注册脚本的共性与差异。
