# u3-l1 数据集类：ScanNetUnifiedSegDataset

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 mmengine / mmdet3d / uniseg3d 三层库组成的数据集类继承链，并说出每一层各自负责什么。
2. 说清 `ann_file`（即 `uniseg3d_infos_train.pkl` 里的 infos）在数据集类中如何被 `parse_data_info` 逐条加工成 `data_info`。
3. 掌握 `ScanNetUnifiedSegDataset` 仅有的两处改动：`parse_data_info` 注入 `super_pts_path`、`get_scene_idxs` 重写采样索引。
4. 独立推导 `data_root`、`data_prefix`、pkl 内相对路径三者的拼接规则，并能仿写一个自己的最小数据集子类。

## 2. 前置知识

**数据集类（Dataset class）**：在 mmengine 体系里，「数据集类」不是把点云数据本身装进内存的对象，而是一份**样本清单 + 取样流程**——它记录「有哪些场景、每个场景的文件在哪、标注是什么」，真正读文件的是后续 pipeline 里的 transform（下一讲 u3-l2 的主角）。这样设计的好处：数据集可以很轻（只存路径字符串），文件只在训练取样那一刻才被读取。

**infos（信息字典列表）**：u1-l4 讲过，`uniseg3d_infos_train.pkl` 是一个 pickle 文件，反序列化后是 `list[dict]`，每个 dict 描述一个场景：点云/掩码/超点文件的**相对路径**、ScanRefer 的 `text_info`（离线分好词的文本提示）、训练集独有的 `sam3d_pseudo_sp_masks`（SAM3D 伪掩码）。数据集类的核心工作就是把这份「原始 info」加工成 pipeline 能直接消费的 `data_info`。

**模板方法（Template Method）**：一种继承技巧——父类把流程骨架定死（「初始化时读 pkl → 对每条 info 调 `parse_data_info` → 存进 `data_list`」），但把其中一步留成**可重写的钩子**。子类只改钩子，不动骨架。`parse_data_info` 和 `get_scene_idxs` 就是 mmdet3d 留给我们的两个钩子。

**`osp.join`**：`os.path.join(a, b)` 把两段路径拼起来。注意两个特性：若 `b` 已经是绝对路径，`a` 会被直接丢弃；若 `a` 是空字符串 `''`，结果就是 `b` 原样。本讲会看到这两点都被代码利用了。

**`np.arange(n)`**：生成 `[0, 1, ..., n-1]` 的 numpy 数组。`np.arange(len(self))` 即「按顺序、每个样本取一次」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [uniseg3d/scannet_dataset.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/scannet_dataset.py#L1-L33) | 本讲主角，全仓库唯一的数据集类，仅 33 行 |
| [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L139-L144) | 数据集的「组装说明书」：`dataset_type`、`data_prefix`、dataloader 设置 |
| [uniseg3d/__init__.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L13) | 第 13 行 import 触发 `ScanNetUnifiedSegDataset` 注册进 `DATASETS` |
| [uniseg3d/loading.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py#L23-L52) | 下游消费方：`LoadAnnotations3D_` 读取 `results['super_pts_path']` 对应的 bin 文件 |
| [uniseg3d/transforms_3d.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L21-L47) | 下游消费方：`TextPromptGeneration` 等读取 pkl 透传下来的 `text_info`、`sam3d_pseudo_sp_masks` |
| [tools/update_infos_to_v2.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/update_infos_to_v2.py#L208-L218) | 佐证：官方 pkl 里 `super_pts_path` 只存文件名（basename） |
| [tools/scannet_data_utils.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/scannet_data_utils.py#L189-L197) | 佐证：info 生成时路径字段如何写入 |
| [README.md](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L111-L127) | 官方 pkl 的下载说明与内容构成 |

另外两个「只读不引用行号」的包内文件：父类 `mmdet3d/datasets/scannet_dataset.py`（mmdet3d 包内，README 第 58 行钉死的 commit `fe25f7a5` 安装，不在本仓库，行号待确认）与 mmengine 的 `BaseDataset`（框架基类）。本讲凡涉及父类行为，均以本仓库代码的运行证据为准，并明确标注。

## 4. 核心概念与源码讲解

本讲覆盖的最小模块：

1. 继承体系与两个重写点（`parse_data_info` / `get_scene_idxs`）
2. `data_prefix` 与 infos 的路径拼装规则

### 4.1 继承体系：三层库叠出来的数据集类

#### 4.1.1 概念说明

`ScanNetUnifiedSegDataset` 不是从零写的，它站在一条四层继承链的顶端：

```
mmengine.BaseDataset                ← 框架地基（读 pkl、管理 data_list、__getitem__ 跑 pipeline）
  └─ mmdet3d BaseSeg3DDataset       ← 3D 分割数据集公共层（包内）
      └─ mmdet3d ScanNetSegDataset  ← ScanNet 特化：parse_data_info / get_scene_idxs / ignore_index
          └─ uniseg3d ScanNetUnifiedSegDataset  ← 本仓库：只加超点路径 + 固定采样索引
```

下层两个类在 mmdet3d 包里（u1-l2 装的依赖），最上层才是我们仓库的代码。这个设计延续自文件头注释标明的 oneformer3d 项目——「能继承就不重写」，33 行解决战斗。

为什么要继承而不是自己写？因为 mmengine 的 `BaseDataset` 已经把所有脏活干完：打开 pkl、遍历每条原始 info、调钩子 `parse_data_info`、缓存结果、提供 `__getitem__(i)`（深拷贝第 i 条 `data_info` 并依次执行 pipeline transforms）。我们只需要告诉它「ScanNet 的 info 长什么样、路径怎么拼」。

#### 4.1.2 核心流程

从 pkl 到一条训练样本，数据集类这一侧的流程：

```text
ann_file = 'uniseg3d_infos_train.pkl'
        │  (mmengine BaseDataset 初始化时反序列化)
        ▼
list[dict]  原始 infos（每场景一个 dict）
        │  逐条调用 self.parse_data_info(info)   ← 钩子 1，本仓库重写
        ▼
data_list = [data_info, ...]  路径已拼好、字段已整理
        │  __getitem__(i)：深拷贝 data_info → 依次执行 pipeline transforms
        ▼
交给 LoadPointsFromFile / LoadAnnotations3D_ 等（u3-l2）真正读 bin 文件
```

两个关键认知：

- **`parse_data_info` 阶段不读任何点云文件**，它只做字符串加工。所以哪怕 bin 文件全缺，构建数据集对象本身也不会报错——报错推迟到第一次取样（u1-l2 实践里「数据缺失时记录报错位置」正是这个原理）。
- **采样由 `get_scene_idxs` 决定**（钩子 2）：它返回一个索引数组，框架据此决定每个 epoch 取哪些场景、是否重复。

#### 4.1.3 源码精读

先看整个文件的骨架：

[uniseg3d/scannet_dataset.py:L1-L12](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/scannet_dataset.py#L1-L12)：文件头注释声明改编自 oneformer3d；第 6 行从 mmdet3d 导入父类 `ScanNetSegDataset`，第 7 行导入 `DATASETS` 注册表，第 10-11 行用 `@DATASETS.register_module()` 把类注册进注册表、随后定义类——注册器机制 u2-l1 已详述，`custom_imports` 触发 [uniseg3d/__init__.py:L13](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L13) 的 `from .scannet_dataset import ScanNetUnifiedSegDataset`，import 的副作用就是第 10 行装饰器执行、类名进入 `DATASETS` 表。配置里 `type='ScanNetUnifiedSegDataset'` 字符串据此实例化。

[uniseg3d/scannet_dataset.py:L12](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/scannet_dataset.py#L12)：docstring 只有一句 "We just add super_pts_path."——这是全仓库改动量的诚实自白：相对父类，本类新增的全部功能就是超点路径。

类体只有两个方法，分别在 4.2 和 4.4 精读。还要看配置里数据集如何被组装：

[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L139-L144](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L139-L144)：`dataset_type = 'ScanNetUnifiedSegDataset'` 与 `data_prefix` 四元组——这四个「逻辑名 → 目录名」映射是理解 4.3 路径拼装的钥匙。

[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L247-L266](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L247-L266)：train/val/test 三个 dataloader 的 dataset 段。训练集传了 7 个参数：`type`（查注册表）、`ann_file='uniseg3d_infos_train.pkl'`、`data_prefix`、`pipeline`、`ignore_index=num_semantic_classes`（即 20，交给父类处理未标注类）、`scene_idxs=None`、`test_mode=False`；验证集只换 `ann_file` 与 `test_mode=True`，未传 `scene_idxs`（走父类默认值）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认继承链与注册链真实存在，不靠脑补。
2. **操作步骤**：
   - 在仓库根目录执行 `grep -n "register_module" uniseg3d/scannet_dataset.py`，确认装饰器行号；
   - 执行 `grep -rn "ScanNetUnifiedSegDataset" configs/`，确认两份主配置都通过 `dataset_type` 变量引用它；
   - 阅读父类：在已安装环境里执行 `python -c "from mmdet3d.datasets.scannet_dataset import ScanNetSegDataset; import inspect; print(inspect.getsourcefile(ScanNetSegDataset))"` 找到包内文件路径，打开看 `parse_data_info` 与 `get_scene_idxs` 的默认实现。
3. **需要观察的现象**：`ScanNetSegDataset` 的 `parse_data_info` docstring 与本仓库子类的 docstring 几乎逐字相同（"Has `ann_info` in training stage. And all path has been converted to absolute path."）——子类直接沿用了父类的契约描述。
4. **预期结果**：你能指出父类文件里与子类两个方法对应的原始版本。父类行号在包内、随安装版本浮动，故本讲不为其标注行号（待确认）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ScanNetUnifiedSegDataset` 里看不到 `__init__`、`__getitem__`、`__len__`？它们从哪来？

> **答案**：全部继承自 mmengine `BaseDataset`（经 mmdet3d 中间层）。子类只重写两个钩子方法，构造参数（`ann_file`、`data_prefix`、`pipeline` 等）经 `**kwargs` 一路上传给 `BaseDataset.__init__` 处理——这是模板方法模式：骨架在框架，变化点在子类。

**练习 2**：配置里 `type='ScanNetUnifiedSegDataset'` 这个字符串是怎么变成类的？

> **答案**：u2-l1 讲过的注册链：配置 `custom_imports`（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L5](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L5)）→ mmengine 代为 `import uniseg3d` → [uniseg3d/__init__.py:L13](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L13) import 子模块 → 类定义时 `@DATASETS.register_module()` 执行 → `DATASETS.build(cfg)` 按名字查表实例化。

**练习 3**：`parse_data_info` 阶段会读取 `points/xxxxx.bin` 文件吗？

> **答案**：不会。它只拼路径字符串、整理字段；读文件发生在 `__getitem__` 触发的 pipeline 第一步 `LoadPointsFromFile`（u3-l2 详讲）。推论：bin 文件缺失时，数据集构建成功、首次取样才报错。

### 4.2 重写点一：`parse_data_info` 注入 `super_pts_path`

#### 4.2.1 概念说明

父类 `ScanNetSegDataset.parse_data_info` 负责把一条原始 info 加工成 `data_info`：整理 `lidar_idx`、把 `pts_path` / `pts_instance_mask_path` / `pts_semantic_mask_path` 等标准路径拼成带 `data_root` 的完整路径、训练阶段组装 `ann_info`。但它是 mmdet3d 的通用代码，**根本不知道「超点」为何物**——pkl 里那个 `super_pts_path` 字段对它来说是个陌生键。

而 UniSeg3D 的整个「超点即查询」范式（u1-l1）都建立在超点掩码之上：下游 [uniseg3d/loading.py:L32](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py#L32) 的 `LoadAnnotations3D_._load_sp_pts_3d` 第一行就是 `sp_pts_mask_path = results['super_pts_path']`——它要求进入 pipeline 的 `data_info` 里必须有一个**已拼好的绝对路径键** `super_pts_path`。

子类要做的就是在父类加工前后，把这个键补上。这就是「注入」：往框架加工流程里塞一个自定义字段。

#### 4.2.2 核心流程

```python
# 伪代码：ScanNetUnifiedSegDataset.parse_data_info(info)
info['super_pts_path'] = osp.join(           # ① 先注入：把 pkl 里的短路径拼上前缀
    self.data_prefix.get('sp_pts_mask', ''),     #    前缀来自配置 data_prefix['sp_pts_mask']
    info['super_pts_path'])                      #    pkl 里存的相对路径/文件名
info = super().parse_data_info(info)         # ② 再交还父类：加工其余标准字段
return info                                   # ③ 原始 info 被原地扩充后返回
```

时序上有个值得注意的细节：注入发生在调用 `super()` **之前**，但父类并不认识这个键，所以先注后调与先调后注效果相同——作者选择先注，与 oneformer3d 的写法保持一致。更关键的问题是：**父类加工后这个陌生键还在吗？** 在。证据是运行层面的：若父类丢弃额外键，`LoadAnnotations3D_` 会在第一次取样时抛 `KeyError: 'super_pts_path'`，而项目能正常训练——这反推出父类的 `parse_data_info` 是在传入的 info dict 上**原地加工并保留额外键**的（其 docstring「all path has been converted to absolute path」描述的正是这种逐键拼接风格）。「用下游消费代码反推上游框架行为」是读这类仓库的常用技巧。

#### 4.2.3 源码精读

[uniseg3d/scannet_dataset.py:L18-L33](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/scannet_dataset.py#L18-L33)：`parse_data_info` 全文。第 28-29 行完成注入：

```python
info['super_pts_path'] = osp.join(
    self.data_prefix.get('sp_pts_mask', ''), info['super_pts_path'])
```

三个要点：

1. **`self.data_prefix`**：mmengine `BaseDataset` 初始化时已把 `data_root` 与配置里 `data_prefix` 的每一项拼好存入该属性（框架行为，见 4.3），所以此刻 `self.data_prefix['sp_pts_mask']` 已经是 `'data/scannet/super_points'` 这样的完整目录。
2. **`.get('sp_pts_mask', '')`**：用 `get` 而非 `[]` 取键，缺省空串。若配置没配 `sp_pts_mask`，前缀为 `''`，`osp.join('', x)` 返回 `x` 原样——路径原样透传而不是抛 `KeyError`，留了一层容错。
3. **原地修改**：第 31 行 `info = super().parse_data_info(info)` 把（已被我们扩充的）dict 交给父类继续加工，父类返回同一个加工后的 dict，第 33 行返回。

再看下游如何消费这个键，形成闭环：

[uniseg3d/loading.py:L23-L52](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py#L23-L52)：`_load_sp_pts_3d` 从 `results['super_pts_path']` 取路径，先尝试 `get()`（支持网络后端），失败则落到第 41-42 行 `mmengine.check_file_exist` + `np.fromfile` 读本地文件，第 45 行把读出的 `(N,)` int64 数组写进 `results['sp_pts_mask']`。**如果 4.2.3 的注入拼错了路径，报错就发生在这里**（`check_file_exist` 抛 `FileNotFoundError`）——记住这个报错位置，排查数据问题时能省半小时。

pkl 里 `super_pts_path` 原始值的形态也有据可查：[tools/update_infos_to_v2.py:L216-L218](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/update_infos_to_v2.py#L216-L218) 写明转换 v2 格式时只保留 `Path(...).name`——即 `scene0000_00.bin` 这样的纯文件名（同文件 L208-215 对 `lidar_path` 等标准键做同样处理）。这与 4.3 的拼装规则严格对应。

同理，pkl 里的 `text_info` 和 `sam3d_pseudo_sp_masks` 也是「父类不认识但原样透传」的额外键：[uniseg3d/transforms_3d.py:L29-L47](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L29-L47) 的 `TextPromptGeneration` 读 `input_dict['text_info']`，[uniseg3d/transforms_3d.py:L386](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L386) 读 `input_dict['sam3d_pseudo_sp_masks']`。它们不需要像 `super_pts_path` 这样被注入处理，因为值本身就是数据（数组/字典）而非需要拼前缀的路径。

#### 4.2.4 代码实践

**实践一（动手运行型）：在 `parse_data_info` 处打印一条完整 data_info**

1. **实践目标**：亲眼看到「原始 info → 处理后 data_info」的差别，确认 `super_pts_path` 注入生效、`text_info` 原样透传。
2. **操作步骤**：

   在仓库根目录新建 `inspect_data_info.py`（示例代码，不属于仓库）：

   ```python
   # 示例代码
   from uniseg3d.scannet_dataset import ScanNetUnifiedSegDataset

   class PrintInfoDataset(ScanNetUnifiedSegDataset):
       """在 parse_data_info 钩子处打印，其余行为不变。"""
       def parse_data_info(self, info: dict) -> dict:
           print('== 原始 info 的键 ==')
           print(sorted(info.keys()))
           out = super().parse_data_info(info)
           print('== 处理后 data_info 的键 ==')
           print(sorted(out.keys()))
           for k in sorted(out.keys()):
               v = out[k]
               if isinstance(v, str) and ('points' in k or 'mask' in k or 'pts' in k):
                   print(f'  {k:28s} -> {v}')
           ti = out.get('text_info')
           if ti:
               first_id = next(iter(ti))
               print(f'  text_info[{first_id}] 的键 -> {sorted(ti[first_id].keys())}')
           return out

   dataset = PrintInfoDataset(
       ann_file='data/scannet/uniseg3d_infos_train.pkl',
       data_root='data/scannet/',
       data_prefix=dict(pts='points', pts_instance_mask='instance_mask',
                        pts_semantic_mask='semantic_mask',
                        sp_pts_mask='super_points'),
       pipeline=[],            # 只看 data_info，不执行 transforms
       ignore_index=20,
       scene_idxs=None,
       test_mode=False)
   print('数据集样本数 len(dataset) =', len(dataset))
   ```

   运行：`python inspect_data_info.py`（需按 u1-l2 完成环境搭建、按 u1-l4 把官方 pkl 放到 `data/scannet/`；当前环境无数据时本步骤待本地验证）。
3. **需要观察的现象**：打印只发生在**第一条** info 上（如果框架实现按序处理；也可在打印后直接 `raise SystemExit` 截断）；处理后的 data_info 中，所有 `*_path` / `*_mask_path` 类键都带上了 `data/scannet/...` 前缀，其中 `super_pts_path` 形如 `data/scannet/super_points/scene0000_00.bin`。
4. **预期结果**：`text_info` 键完整出现在处理后的 data_info 里（证明父类透传额外键）；若某个路径前缀不对，构建阶段不报错，但把该路径传给 `mmengine.check_file_exist`（loading.py L41）会报 `FileNotFoundError`。
5. **注意**：标准路径键的准确键名取决于所装 mmdet3d 版本的 `parse_data_info` 风格（v1 风格 `pts_path` 或 v2 风格 `lidar_points` 嵌套），以你打印出的键清单为准（待本地验证）；`super_pts_path` 这个平铺键是我们子类亲手注入的，必然存在。

#### 4.2.5 小练习与答案

**练习 1**：如果把配置里 `data_prefix` 的 `sp_pts_mask='super_points'` 改成一个不存在的目录名，会在哪一步、以什么形式报错？

> **答案**：构建数据集与执行 `parse_data_info` 都不报错（只是字符串拼接）；错误推迟到 pipeline 中 `LoadAnnotations3D_._load_sp_pts_3d` 读文件时——本地路径分支里 [uniseg3d/loading.py:L41](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py#L41) 的 `mmengine.check_file_exist` 抛 `FileNotFoundError`（或网络后端分支抛连接错误）。

**练习 2**：为什么注入 `super_pts_path` 用 `self.data_prefix.get('sp_pts_mask', '')` 而不是 `self.data_prefix['sp_pts_mask']`？

> **答案**：`get` 带空串缺省——配置漏配该键时不抛 `KeyError`，路径保持 pkl 原值透传（`osp.join('', x) == x`）。这让数据集类对「pkl 里已自带目录前缀的相对路径」也能工作，是防御式写法。

**练习 3**：`text_info` 为什么不需要在 `parse_data_info` 里做任何注入？

> **答案**：`text_info` 的值是「实例 id → {text, text_token, label_id, ...}」的字典（[uniseg3d/transforms_3d.py:L29-L47](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L29-L47) 可见其结构），内容是数据本身、不是文件路径，无需拼前缀；父类原样透传即可。`super_pts_path` 特殊在它是**路径**，必须换成可打开的完整路径。

### 4.3 `data_prefix` 与 infos 的路径拼装规则

#### 4.3.1 概念说明

现在回答一个系统性问题：**同一个文件（如 `scene0000_00.bin`）的路径，分别在 pkl 里、配置里、内存里长什么样？** UniSeg3D 采用「三段式」约定，避免 pkl 与磁盘目录结构硬耦合：

| 层 | 存什么 | 例子 |
| --- | --- | --- |
| pkl（info 内） | 最短文件名（basename） | `scene0000_00.bin` |
| 配置 `data_prefix` | 逻辑名 → 子目录名 | `sp_pts_mask='super_points'` |
| 运行时（`data_root` 拼接后） | 完整相对仓库根的路径 | `data/scannet/super_points/scene0000_00.bin` |

好处：数据集整体搬家时只需改一处 `data_root`，pkl 不用重新生成；换一套目录布局只改 `data_prefix`。

#### 4.3.2 核心流程

拼装分两步、两个时机：

```text
【初始化时，mmengine BaseDataset 完成（框架行为，包内代码，待确认行号）】
data_root（来自 _base_ 配置链，'data/scannet/'）
    ⊕ data_prefix 的每一项
    ⇒ self.data_prefix = {pts: 'data/scannet/points',
                          pts_instance_mask: 'data/scannet/instance_mask',
                          pts_semantic_mask: 'data/scannet/semantic_mask',
                          sp_pts_mask: 'data/scannet/super_points'}

【逐条 parse_data_info 时（本仓库与父类各自完成）】
self.data_prefix['sp_pts_mask'] ⊕ info['super_pts_path']（basename）
    ⇒ 'data/scannet/super_points/scene0000_00.bin'
父类对 pts / instance_mask / semantic_mask 做同型拼接（框架契约）
```

两个易踩的坑：

- **`data_root` 从哪来？** 本配置自己没写 `data_root`，它沿 `_base_ = 'mmdet3d::_base_/datasets/scannet-seg.py'`（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L1-L4](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L1-L4)）继承自 mmdet3d 包内基座配置，值为 `'data/scannet/'`（u2-l2 讲过 `_base_` 递归深合并；包内文件行号待确认）。`ann_file='uniseg3d_infos_train.pkl'` 同样会被拼上 `data_root`，与 README 要求把 pkl 放进 `data/scannet`（[README.md:L111](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L111)）严格对应。
- **`osp.join` 的覆盖语义**：若 pkl 里的值不是 basename 而是带目录的相对路径（如旧式 `super_points/scene0000_00.bin`，见 [tools/scannet_data_utils.py:L197](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/scannet_data_utils.py#L197) 的 v1 风格写入），`osp.join` 会老实地串成 `data/scannet/super_points/super_points/scene0000_00.bin`——目录重复、路径失效。所以**必须使用官方 Release 的 pkl**（v2 风格 basename，[tools/update_infos_to_v2.py:L216-L218](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/update_infos_to_v2.py#L216-L218)），这与 u1-l4「pkl 必须下载、仓库内 dump 已被注释」的结论互为印证。

#### 4.3.3 源码精读

[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L140-L144](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L140-L144)：`data_prefix` 四元组，逻辑名与磁盘子目录一一对应：

| 逻辑名（配置键） | 目录值 | pkl 中的字段 | 拼接结果 |
| --- | --- | --- | --- |
| `pts` | `points` | 点云文件名（标准键） | `data/scannet/points/xxx.bin` |
| `pts_instance_mask` | `instance_mask` | 实例掩码文件名 | `data/scannet/instance_mask/xxx.bin` |
| `pts_semantic_mask` | `semantic_mask` | 语义掩码文件名 | `data/scannet/semantic_mask/xxx.bin` |
| `sp_pts_mask` | `super_points` | `super_pts_path`（本类注入） | `data/scannet/super_points/xxx.bin` |

前三行是 mmdet3d 父类在 `parse_data_info` 里消费的（框架契约），第四行是本仓库子类注入的——配置里加一行 `sp_pts_mask='super_points'`、代码里加一句 `osp.join`，二者缺一不可，这就是「配置即代码」在数据侧的体现。

[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L250-L257](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L250-L257)：dataset 七参数在 train_dataloader 中的落点。注意 `data_prefix=data_prefix` 传的是模块级变量（第 140 行定义的 dict），mmengine 递归 build 时把它作为关键字参数传给数据集 `__init__`。

[tools/scannet_data_utils.py:L189-L197](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/scannet_data_utils.py#L189-L197)：info 生成侧的对照——第 189 行 `info['pts_path'] = osp.join('points', f'{sample_idx}.bin')`、第 197 行 `info['super_pts_path'] = osp.join('super_points', f'{sample_idx}.bin')`，这是 v1 风格（带一层目录前缀）；官方发布的 pkl 走的是 [tools/update_infos_to_v2.py:L208-L218](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/update_infos_to_v2.py#L208-L218) 的转换，全部削成 basename。两份代码对照着读，「pkl 存最短路径」的设计意图一目了然。

#### 4.3.4 代码实践（源码阅读型 + 动手验证）

1. **实践目标**：亲手验证三段式拼装，并体会 `osp.join` 的语义陷阱。
2. **操作步骤**：

   ```python
   # 示例代码：可在任何有 python 的机器上跑，不需要数据
   from os import path as osp
   prefix = 'data/scannet/super_points'          # data_root ⊕ data_prefix 拼好后
   print(osp.join(prefix, 'scene0000_00.bin'))   # 情形 A：v2 风格 basename（官方 pkl）
   print(osp.join(prefix, 'super_points/scene0000_00.bin'))  # 情形 B：v1 风格带目录
   print(osp.join('', 'scene0000_00.bin'))       # 情形 C：data_prefix.get 缺省空串
   print(osp.join(prefix, '/abs/path.bin'))      # 情形 D：pkl 里存了绝对路径
   ```
3. **需要观察的现象**：四种情形分别输出什么。
4. **预期结果**：
   - A：`data/scannet/super_points/scene0000_00.bin` ✅（唯一正确形态）
   - B：`data/scannet/super_points/super_points/scene0000_00.bin` ❌（目录重复，读取必失败）
   - C：`scene0000_00.bin`（`.get` 缺省容错的效果，退化为相对当前工作目录）
   - D：`/abs/path.bin`（绝对路径覆盖前缀）
   
   此实践无环境依赖，可立即验证。

#### 4.3.5 小练习与答案

**练习 1**：想把整个数据集从 `data/scannet/` 挪到 `/mnt/nas/scannet/`，最少改几处？

> **答案**：一处——覆盖 `data_root`（在自有配置里写 `data_root = '/mnt/nas/scannet/'`，或命令行 `--cfg-options train_dataloader.dataset.data_root=...`）。pkl 与 `data_prefix` 均不用动，因为 pkl 只存 basename。

**练习 2**：自造一个 pkl，把 `super_pts_path` 写成绝对路径 `/data/xxx.bin`，注入后得到的路径是什么？

> **答案**：`osp.join` 遇到绝对路径的第二参数会丢弃前缀，结果仍是 `/data/xxx.bin`——这既可以是特性（pkl 直接指向任意绝对位置），也可以是坑（看似拼了前缀实际没生效）。

**练习 3**：`ann_file='uniseg3d_infos_train.pkl'` 没有 `data/` 前缀，框架为什么找得到它？

> **答案**：mmengine `BaseDataset` 初始化时同样用 `data_root` 拼接 `ann_file`，得到 `data/scannet/uniseg3d_infos_train.pkl`，与 [README.md:L111](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L111)「put it into `data/scannet`」的要求一致（框架行为，行号待确认）。

### 4.4 重写点二：`get_scene_idxs` 固定采样索引

#### 4.4.1 概念说明

`get_scene_idxs` 回答的问题是：**一个 epoch 里，哪些场景被取样、各取几次？** mmdet3d 父类的实现支持三种输入：`None`（全部场景各一次）、一个索引数组、或一个 `.npy` 文件路径（ScanNet 官方曾用这种权重文件让小场景被重复采样，以平衡类别）。返回的索引数组会被父类保存，并作为采样器（sampler）决定 epoch 长度与取样顺序的依据（框架契约，父类在包内，行号待确认）。

本仓库子类把它**锁死**为「全量、各一次」：

```python
def get_scene_idxs(self, *args, **kwargs):
    return np.arange(len(self)).astype(np.int32)
```

`*args, **kwargs` 吞掉一切入参——无论配置里 `scene_idxs` 传什么，都不再起作用。这是一次「去功能化」重写：放弃外置权重文件机制，换取行为完全确定、不依赖任何额外文件。

#### 4.4.2 核心流程

```text
配置: scene_idxs=None (train) / 未传 (val，走父类默认)
        │
        ▼
父类 __init__ 调 self.get_scene_idxs(scene_idxs)     ← 钩子 2，本仓库重写
        │
        ▼
子类忽略入参，返回 np.arange(len(self))               ← [0, 1, ..., N-1]，int32
        │
        ▼
父类保存该数组（self.scene_idxs，并派生 self.flag）   ← 框架契约，待确认
        │
        ▼
每个 epoch：N 个场景各被取样一次；shuffle 只改顺序、不增减次数
```

若某天子类返回 `[0,0,0,1,2]`，则场景 0 每 epoch 被取样 3 次——这就是「重复采样」机制;理解了这一点，你就掌握了控制数据分布的一个阀门。

#### 4.4.3 源码精读

[uniseg3d/scannet_dataset.py:L14-L16](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/scannet_dataset.py#L14-L16)：三行实现。`len(self)` 即 `BaseDataset.__len__` 返回的 `data_list` 长度（pkl 里场景数）；`.astype(np.int32)` 是与父类约定一致的 dtype（父类同样以 int32 返回，框架契约）。

[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L256](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L256)：训练集显式传 `scene_idxs=None`。注意验证集（L258-L265）压根没传这个键——即便如此，父类默认路径也会调 `get_scene_idxs`，而我们的重写让它恒等于全量索引。也就是说：**这个重写对 train/val 一视同仁，且让配置中的 `scene_idxs` 形同虚设**——这是有意为之的防御性设计（配置写错也不会改变采样行为）。

顺带一提 `test_mode=True`（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L265](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L265)）：`parse_data_info` docstring 里「Has `ann_info` in training stage」的语义由它控制——测试模式下保留 GT 标注供评估器对照（u6-l2 会用到）。

#### 4.4.4 代码实践（动手运行型）

**实践二：仿写一个「只取前 10 个场景」的子类并用小配置验证**

1. **实践目标**：证明 `get_scene_idxs` 的返回值直接决定 epoch 的取样范围，掌握「注册新数据集类 → 配置引用」的完整闭环。
2. **操作步骤**：

   第一步，仓库根目录新建 `my_dataset.py`（示例代码）：

   ```python
   # 示例代码
   import numpy as np
   from mmdet3d.registry import DATASETS
   from uniseg3d.scannet_dataset import ScanNetUnifiedSegDataset


   @DATASETS.register_module()
   class FirstTenScenesDataset(ScanNetUnifiedSegDataset):
       """只把前 10 个场景纳入采样索引。"""

       def get_scene_idxs(self, *args, **kwargs):
           return np.arange(min(10, len(self))).astype(np.int32)
   ```

   第二步，直接构造对比验证（不依赖配置，需 pkl 在位，待本地验证）：

   ```python
   # 示例代码
   import numpy as np
   from uniseg3d.scannet_dataset import ScanNetUnifiedSegDataset
   from my_dataset import FirstTenScenesDataset

   common = dict(
       ann_file='data/scannet/uniseg3d_infos_train.pkl',
       data_root='data/scannet/',
       data_prefix=dict(pts='points', pts_instance_mask='instance_mask',
                        pts_semantic_mask='semantic_mask',
                        sp_pts_mask='super_points'),
       pipeline=[], ignore_index=20, scene_idxs=None, test_mode=False)

   base = ScanNetUnifiedSegDataset(**common)
   sub = FirstTenScenesDataset(**common)

   print('len(base) =', len(base), ' len(sub) =', len(sub))   # 两者相同（数据清单没变）
   for d, name in [(base, 'base'), (sub, 'FirstTen')]:
       # scene_idxs / flag 的属性名来自 mmdet3d 框架契约，若名字不同请用
       # print([k for k in d.__dict__ if 'scene' in k or 'flag' in k]) 自查
       idx = getattr(d, 'scene_idxs', None)
       print(f'{name}: scene_idxs = {idx}')
   ```

   第三步，用小配置走注册链（示例代码，存为 `configs/small_first10.py`）：

   ```python
   # 示例代码
   _base_ = 'uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py'
   custom_imports = dict(imports=['my_dataset'])      # 让 mmengine import 我们的注册模块
   train_dataloader = dict(dataset=dict(type='FirstTenScenesDataset'))
   ```

   用 `python tools/train.py configs/small_first10.py --cfg-options ...`（或先按 u1-l2 的 dry-run 方式）观察训练循环每 epoch 的迭代数。
3. **需要观察的现象**：`len(base)` 与 `len(sub)` **完全相同**（数据清单不变）；但 `base` 的 `scene_idxs` 长度 = 场景总数（标准 ScanNet v2 训练集为 1201，待本地验证），`sub` 的 `scene_idxs = [0..9]` 长度 10；走小配置训练时每 epoch 迭代数按 `batch_size` 摊薄到 10 个场景对应的步数（1201 → 10 的骤减）。
4. **预期结果**：本实践验证了「`get_scene_idxs` 返回值长度 = 每 epoch 取样次数」这一机制；同时验证 `custom_imports` 指向自定义模块即可让新类即插即用——这正是 u6-l4 二次开发「新增数据集」的雏形。
5. **注意**：无数据环境可先做代码阅读部分（对照 4.4.3 读父类 `get_scene_idxs` 的默认实现），运行部分待本地验证；若直接构造时报意外关键字参数，请对照所装 mmdet3d 版本的 `ScanNetSegDataset.__init__` 签名微调参数（父类签名以包内为准，待确认）。

#### 4.4.5 小练习与答案

**练习 1**：配置里把 `scene_idxs` 改成一个权重文件路径 `scene_idxs.npy`，采样会变吗？

> **答案**：不会。[uniseg3d/scannet_dataset.py:L14-L16](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/scannet_dataset.py#L14-L16) 的重写用 `*args, **kwargs` 吞掉全部入参、恒返回全量 `arange`，外置权重机制被彻底关闭。想让权重文件生效，唯一办法是改这个方法。

**练习 2**：若想让「前 10 个场景各采 3 次、其余各 1 次」，`get_scene_idxs` 该返回什么？

> **答案**：返回 `np.concatenate([np.repeat(np.arange(10), 3), np.arange(10, len(self))]).astype(np.int32)`——索引数组中一个下标出现几次，该场景每 epoch 就被取样几次（框架契约）。同时注意 shuffle 只重排这个数组、不改变出现次数。

**练习 3**：为什么验证集配置不传 `scene_idxs` 也没问题？

> **答案**：父类对缺省入参有默认处理（`None` → 全量），且无论传什么都会被本仓库重写覆盖为全量 `arange`；评估要求每个验证场景恰好评估一次，全量索引正是期望行为。

## 5. 综合实践

**任务：给 UniSeg3D 接一个「子集调试数据集」，跑通从 pkl 到 data_info 的完整链路。**

做深度学习调试时，常需要「只用几十个场景快速过一遍训练/评估」。请把本讲两个钩子组合起来，实现一个 `DebugSubsetDataset`（示例代码）：

```python
# 示例代码：debug_subset_dataset.py（仓库根目录）
import numpy as np
from mmdet3d.registry import DATASETS
from uniseg3d.scannet_dataset import ScanNetUnifiedSegDataset


@DATASETS.register_module()
class DebugSubsetDataset(ScanNetUnifiedSegDataset):
    """只取前 num_scenes 个场景，用于快速调试。"""

    def __init__(self, num_scenes=50, **kwargs):
        self.num_scenes = num_scenes
        super().__init__(**kwargs)

    def get_scene_idxs(self, *args, **kwargs):
        return np.arange(min(self.num_scenes, len(self))).astype(np.int32)

    def parse_data_info(self, info: dict) -> dict:
        out = super().parse_data_info(info)
        if not hasattr(self, '_printed'):
            print('[DebugSubsetDataset] 示例 data_info 路径键：')
            for k, v in out.items():
                if isinstance(v, str) and ('mask' in k or 'pts' in k or 'path' in k):
                    print(f'   {k} -> {v}')
            self._printed = True
        return out
```

配套小配置（示例代码，`configs/debug_subset.py`）：

```python
_base_ = 'uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py'
custom_imports = dict(imports=['debug_subset_dataset'])
train_dataloader = dict(dataset=dict(type='DebugSubsetDataset', num_scenes=50))
val_dataloader = dict(dataset=dict(type='DebugSubsetDataset', num_scenes=10))
```

验收清单（对应本讲各模块）：

1. **继承与注册**（4.1）：`custom_imports` 能否让 `DATASETS.build` 找到新类？报 `KeyError` 说明 import 没触发。
2. **路径注入**（4.2、4.3）：打印出的 `super_pts_path` 是否为 `data/scannet/super_points/xxx.bin`？若出现重复目录，检查 pkl 是否为官方 Release 版本（v2 basename 风格）。
3. **采样控制**（4.4）：每 epoch 迭代数是否按 50/10 个场景缩水？观察训练日志的 `Epoch [x][y/z]` 中 z 值。
4. **下游联动**：用 `dataset[0]`（把 `pipeline` 换成完整 `train_pipeline` 构造）验证 `LoadAnnotations3D_` 能读到文件、`results['sp_pts_mask']` 形状为 `(N,)`。

无数据环境下，第 1、2 步的路径打印部分可以先读代码推演，其余待本地验证。

## 6. 本讲小结

- `ScanNetUnifiedSegDataset` 全文仅 33 行，站在 `mmengine.BaseDataset → mmdet3d.BaseSeg3DDataset → ScanNetSegDataset` 四层继承链顶端，只重写 `parse_data_info` 与 `get_scene_idxs` 两个模板方法钩子——「能继承就不重写」的最小改造风格。
- `parse_data_info` 的唯一实质改动是在调用父类前注入 `info['super_pts_path'] = osp.join(self.data_prefix.get('sp_pts_mask',''), ...)`，为「超点即查询」范式补上超点掩码的完整路径；父类对 `text_info`、`sam3d_pseudo_sp_masks` 等额外键原样透传（由下游 transforms 消费佐证）。
- 路径拼装是三段式：pkl 存 basename、`data_prefix` 给逻辑名→目录映射、`data_root`（来自 `_base_` 链）补根目录；`osp.join` 的绝对路径覆盖与空串透传语义都可能成为坑点。
- `get_scene_idxs` 被锁死为全量 `arange`，令配置中的 `scene_idxs` 失效、每个场景每 epoch 恰好取样一次——一个「去功能化」的防御性重写。
- 数据集构建阶段只拼字符串不读文件，路径错误的报错延迟到 pipeline 的 `LoadAnnotations3D_`（`mmengine.check_file_exist`）才出现——排查数据问题先看这个位置。
- 自定义数据集类的完整闭环是：写子类并 `@DATASETS.register_module()` → 用 `custom_imports` 指向所在模块 → 配置里改 `type`——这正是 u6-l4 接入新数据集的基本功。

## 7. 下一步学习建议

本讲产出的是「拼好路径、字段齐全的 `data_info` dict」，但它仍是一堆路径字符串和轻量标注。下一讲 **u3-l2《加载、打包与批处理：三个改造版组件》**将追踪 `data_info` 进入 pipeline 后的旅程：`LoadAnnotations3D_` 如何真正读出四个 bin 文件、`Pack3DDetInputs_` 如何把点/文本提示键分拣进 inputs 与 data_sample、`Det3DDataPreprocessor_` 如何在 collate 阶段组装 batch。建议先自行阅读 [uniseg3d/loading.py:L54-L67](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py#L54-L67) 的 `transform` 方法，思考 `super().transform(results)`（mmdet3d 原版）处理了哪些键、本仓库为何要追加 `_load_sp_pts_3d`。若想巩固本讲的继承主题，可对照阅读 mmdet3d 包内 `ScanNetSegDataset` 的 `parse_data_info` 默认实现（用 4.1.4 的 `inspect.getsourcefile` 定位），亲眼确认「额外键透传」这一框架行为。
