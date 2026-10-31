# 数据转换：create_data.py 生成 infos.pkl

## 1. 本讲目标

上一讲（u2-l1）我们看懂了 HuggingFace 上 Ground3D 数据集的**原始形态**：每帧四个 `.npy` 加三层 QA JSON。但训练/评估配置真正消费的却是另外两种东西——扁平的 `.bin` 字节流和 `part3d_glamm_infos_{train,val}.pkl`。本讲就讲这条转换链路。读完本讲，你应该能够：

1. 独立运行 `tools/create_data.py`，生成 `points/`、`super_points/`、`part_mask/`、`object_mask/` 四个 bin 目录与两个 infos pkl；
2. 解释 `Part3dGlammData` 如何确定「哪些帧进入 infos、哪些帧被丢弃」；
3. 手推 `_flatten_qa_data` 的输入输出结构，并说明 `<SEG>` 标记在转换阶段被补齐的两条不同规则（单轮 vs 多轮）；
4. 说清 `update_pkl_infos` 做的「v1 → v2」规范化到底改了什么、为什么它会**原地覆盖**输入 pkl；
5. 掌握写盘/读盘两侧的 dtype 契约（int64）——这是自制数据集时最容易踩的坑。

## 2. 前置知识

### 2.1 `.npy` 与裸 `.bin` 的区别

`.npy` 是 NumPy 的序列化格式，文件头自带 dtype 和 shape 元数据，`np.load` 可以「无脑」还原。而转换脚本用的是 `ndarray.tofile()`，它写的是**纯字节流（C 顺序）**，不带任何元数据。读回时必须自己知道 dtype：

- `points/<frame>.bin`：每点 6 个 float32（XYZ+RGB），\( N \times 6 \times 4 = 24N \) 字节；
- `*_mask/<frame>.bin`：每点 1 个 int64，\( N \times 8 \) 字节。

点数 \( N \) 本身不存储在任何地方——它由点数组的长度隐式决定。这也是为什么标签数组和点云必须**逐点对齐**（u2-l1 讲过：四类 npy 共享同一长度 N）。

### 2.2 pkl 与 mmengine.dump

`.pkl` 是 Python pickle 序列化文件。本项目统一用 `mmengine.dump(obj, path, 'pkl')` 写、`mmengine.load(path)` 读（内部仍是 pickle），好处是读写两侧风格一致。

### 2.3 OpenMMLab infos 的 v1 与 v2 格式

mmdet3d 系数据集历史上用过两种「每帧元信息」结构：

- **v1**：自由格式 dict，字段如 `point_cloud`、`pts_path`、`annos`，各数据集自定义；
- **v2**：统一模板 `lidar_points` / `instances` / `pts_semantic_mask_path` 等标准键，外加一个 `metainfo` 头（类别表、数据集名、版本号）。

本项目的 `Part3dGlammData.get_infos()` 先产出 v1，再由 `update_infos_to_v2.py` 翻译成 v2，交给 `Part3DGlamm_Dataset`（下一讲 u2-l3 的主角）。

### 2.4 承接上一讲

帧 ID 的形式是 `<场景>_frame_<序号>`（如 `scene0000_00_frame_12`）；`vert.npy` 是帧存在的标志；QA 分 `part_qa` / `object_qa` / `multiturn_qa` 三层；任务名共 9 类（`ALL_TASKS`）。这些结论本讲直接使用。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [tools/create_data.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/create_data.py) | 命令行入口 | 参数解析、按 `--dataset` 分发、「转换 + v2 更新」两段式流程 |
| [tools/part3d_glamm_data_utils.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py) | 转换器主体 | `Part3dGlammData` 类、两个 `_flatten_*` 函数、`ALL_TASKS` 常量 |
| [tools/update_infos_to_v2.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/update_infos_to_v2.py) | v2 规范化 | `update_pkl_infos` 分发表、`update_part3d_glamm_infos` 字段搬运 |
| [configs/ground3dlmm_train.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py) | 消费方 | `ann_file` / `data_prefix` 如何指向转换产物 |
| [uniseg3d/loading.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/loading.py) | 读回对端 | bin 以什么 dtype 被读回（写读契约） |
| [README.md](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md) | 官方文档 | 转换命令与产物布局 |

## 4. 核心概念与源码讲解

先给一张全局数据流图，四个小节分别对应图中的四段：

```text
HF 原始数据（u2-l1 讲过的形态）
  processed_data/<scene>/frame_<n>_{vert,sp_label,part_label,object_label}.npy
  refined_qa_data/{part_qa,object_qa}/<scene>/final_json_outputs/<n>_{part,object}_mapping.json
  refined_qa_data/multiturn_qa/<scene>/frame_<n>_multi_conv.json
  part_ground3d_{train,val}.txt
        │
        │ ① 入口分发（4.1）：create_data.py --dataset part3d_glamm
        ▼
  ② Part3dGlammData（4.2）：split 清单 ∩ vert.npy 扫描 → 逐帧处理
        │   ├─ _process_qa（4.3）：QA 展平 + <SEG> 补齐，三路全空则丢帧
        │   ├─ vert.npy      → points/<frame>.bin
        │   ├─ sp_label.npy  → super_points/<frame>.bin
        │   ├─ object_label  → object_mask/<frame>.bin（int64，非 test）
        │   └─ part_label    → part_mask/<frame>.bin（int64，非 test）
        │   产出 v1 infos 列表 → mmengine.dump → <prefix>_infos_<split>.pkl
        ▼
  ④ update_pkl_infos（4.4）：v1 → v2 规范化，原地覆盖同一个 pkl
        │
        ▼
  part3d_glamm_infos_{train,val}.pkl（v2）
        └─ 被 configs 的 ann_file + data_prefix 消费（u2-l3）
```

### 4.1 create_data.py 入口分发

#### 4.1.1 概念说明

`create_data.py` 是从 mmdet3d 官方工具改来的**薄入口**：它只做三件事——解析命令行、按 `--dataset` 选择数据集准备函数、串联「生成 v1 infos」与「v2 更新」两步。所有真正的工作都在被调用的模块里。这种「入口 + 工具函数库」的拆分让同一份入口可以服务 ScanNet、part3d_glamm（以及 u7-l1 将讲的 ScanRefer/Reason3D 专用入口）等多种数据集。

#### 4.1.2 核心流程

1. `argparse` 解析参数（默认值都按 `data/scannet` 布局给出）；
2. `register_all_modules()` 注册 mmdet3d 全部模块（保险起见，转换本身并不依赖注册表）；
3. `--dataset part3d_glamm` → `part3d_glamm_data_prep()`：
   - 对 `train`、`val` 两个 split 各构造一个 `Part3dGlammData`；
   - `get_infos()` 逐帧生成 v1 info 列表；
   - `mmengine.dump` 写 `<prefix>_infos_<split>.pkl`；
   - 立刻调 `update_pkl_infos()` 把这个 pkl 翻译成 v2（见 4.4）。

#### 4.1.3 源码精读

参数定义——注意 `--extra-tag` 就是输出 pkl 的文件名前缀：

[tools/create_data.py:81-105](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/create_data.py#L81-L105)

这段代码定义了 CLI 参数：`--dataset`（默认 `part3d_glamm`）、`--root-path`（数据源根目录）、`--out-dir`（产物写到哪里）、`--qa_data_dir`（QA JSON 根目录，默认 `data/scannet/refined_qa_data`）、`--extra-tag`（pkl 文件名前缀，默认 `part3d_glamm`）和 `--workers`。

主入口的数据集分发：

[tools/create_data.py:107-125](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/create_data.py#L107-L125)

`scannet`/`scannet200` 走 `scannet_data_prep`（整场景 ScanNet 检测数据，本讲不展开）；`part3d_glamm` 走我们要讲的 `part3d_glamm_data_prep`；其他名字直接抛 `NotImplementedError`。

`part3d_glamm_data_prep` 的两段式流程：

[tools/create_data.py:49-69](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/create_data.py#L49-L69)

每个 split 依次执行：构造 `Part3dGlammData` → `get_infos()` 拿 v1 列表 → `mmengine.dump` 写 pkl → `update_pkl_infos` 原地升级为 v2。test split 的代码被注释掉了，所以实际只产出 train/val 两个 pkl。

三个值得注意的细节（都直接来自代码）：

1. **`--extra-tag` 决定 pkl 文件名**：`f'{info_prefix}_infos_train.pkl'`。训练配置里 ScanNet++ 数据源期望的名字是 `part3d_glamm_scannetpp_infos_train.pkl`（[configs/ground3dlmm_train.py:181](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L181)），而 README 的 ScanNet++ 示例命令（[README.md:179-183](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L179-L183)）只换了路径没换 tag——照抄会生成 `part3d_glamm_infos_train.pkl`，与配置不匹配。转换 ScanNet++ 时应加 `--extra-tag part3d_glamm_scannetpp`。
2. **`--workers` 实际不生效**：它被传进 `get_infos(num_workers=workers)`，但 `Part3dGlammData.get_infos` 内部是串行 for 循环（见 4.2.3），并没有并行。这是从 mmdet3d 原版（用线程池）简化时留下的接口。
3. **scannet 分支需要额外文件**：入口顶部把 `data/` 目录插进 `sys.path` 以导入 `indoor_converter`（[tools/create_data.py:6-10](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/create_data.py#L6-L10)）；而 `part3d_glamm` 分支用的 `Part3dGlammData` 就在 `tools/` 旁边，无需该路径。

#### 4.1.4 代码实践：干跑入口，核对参数

**实践目标**：不碰任何数据，只通过 `--help` 和一次注定失败的干跑核对 CLI 参数与分发逻辑。

**操作步骤**：

1. 在仓库根目录运行（本讲实践均未实际运行过，结果标注「待本地验证」）：

   ```bash
   python tools/create_data.py --help
   ```

2. 对照 [tools/create_data.py:81-105](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/create_data.py#L81-L105) 逐项确认参数与默认值。
3. 再运行一次**注定失败**的干跑验证分发逻辑：

   ```bash
   python tools/create_data.py --dataset not_a_dataset --root-path /tmp/none
   ```

**需要观察的现象**：`--help` 打印出 `--dataset`、`--root-path`、`--out-dir`、`--qa_data_dir`、`--extra-tag`、`--workers` 六个参数及默认值；第二个命令在 `register_all_modules()` 之后抛出 `NotImplementedError: Don't support not_a_dataset dataset.`。

**预期结果**：参数清单与 4.1.3 一致；异常来自 [tools/create_data.py:124-125](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/create_data.py#L124-L125)。若 `import mmdet3d` 就报错，说明环境未按 u1-l2 完成，先回去补环境。（待本地验证）

#### 4.1.5 小练习与答案

**练习 1**：README 说 ScanNet++「布局完全相同，换个路径即可」，为什么实际还得加 `--extra-tag part3d_glamm_scannetpp`？
**答案**：布局确实相同（转换器按 `processed_data/`、`refined_qa_data/` 等相对路径找文件，与数据源无关）；但 pkl 文件名由 `--extra-tag` 决定（[tools/create_data.py:49-50](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/create_data.py#L49-L50)），而训练配置的第二个数据源期望 `part3d_glamm_scannetpp_infos_train.pkl`（[configs/ground3dlmm_train.py:181](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L181)）。名字不匹配时数据集初始化会因找不到 ann_file 而报错。

**练习 2**：`--out-dir` 和 `--root-path` 指向不同目录会发生什么？
**答案**：读取（npy/json/txt）始终按 `root_path` 解析（`Part3dGlammData` 里只用 `self.root_dir` 拼源路径），而 bin 与 pkl 写到 `save_path`（`out_dir` 为 None 时回落到 `root_path`，见 [tools/create_data.py:45](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/create_data.py#L45)）。也就是说可以「读 A 盘、写 B 盘」，但写出的 pkl 里的相对路径（`points/xxx.bin`）要在 B 盘下成立，后续 `data_prefix` 得指向 B 盘。

**练习 3**：`part3d_glamm_data_prep` 里 `import mmengine` 写在函数体中间（第 57 行），为什么也能工作？
**答案**：Python 的 import 语句执行到才触发且模块全局缓存；这只是代码风格问题（疑似调试残留），功能上与顶部导入无差别。

### 4.2 Part3dGlammData 转换流程：从扫描帧到 v1 infos

#### 4.2.1 概念说明

`Part3dGlammData` 是转换器的核心类。它回答三个问题：**哪些帧要转**（split 清单 ∩ 磁盘扫描）、**每帧转什么**（4 类 npy → 4 类 bin + QA 字典）、**什么时候丢帧**（无 QA 或无点云）。它的产出是 v1 格式的 info 列表——每帧一个 dict，只记相对路径和 QA，不存点数据本身。

#### 4.2.2 核心流程

以 split=train 为例：

```伪代码
init:
    读 part_ground3d_train.txt 得到场景集合 S          # 清单是唯一权威
    扫 processed_data/ 下属于 S 的场景里的 *_vert.npy   # 得到帧 ID 列表并排序
    若清单文件不存在 → 直接 FileNotFoundError

对每个帧 ID:
    qa = _process_qa(frame)             # 三路读 QA（4.3）
    若 qa 全空 → 丢弃该帧
    若 vert.npy 不存在 → 打印警告并丢弃
    vert → points/<frame>.bin           # 原样 dtype
    sp_label → super_points/<frame>.bin（若存在）# 原样 dtype
    非 test 模式:
        object_label → object_mask/<frame>.bin   # 强制 int64
        part_label   → part_mask/<frame>.bin     # 强制 int64
    填一个空 annos（gt_num=0 + 单位阵 axis_align_matrix）
返回 v1 info 列表
```

#### 4.2.3 源码精读

**构造函数：split 清单是唯一权威**。

[tools/part3d_glamm_data_utils.py:103-124](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L103-L124)

这段代码读取 `part_ground3d_{split}.txt` 作为场景集合；文件不存在时抛出带恢复提示的 `FileNotFoundError`。源码注释特意解释了为什么不能「磁盘上有什么就转什么」：`processed_data/` 里可能包含没有 QA 的场景，回退到「扫盘即真相」会静默构造出与发布版本不同的 split（这也呼应 u2-l1 的「划分只认 txt 清单」）。

**帧扫描与路径拼接**。

[tools/part3d_glamm_data_utils.py:127-149](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L127-L149)

`_frame_npy` 把帧 ID `<scene>_frame_<n>` 拆开拼出 `processed_data/<scene>/frame_<n>_<suffix>.npy`；`_scan_frame_ids` 用「文件名以 `frame_` 开头、以 `_vert.npy` 结尾」这一约定找出所有真实存在的帧，最后 `sorted` 保证顺序确定（对可复现的 split 很重要）。注意帧列表受两层过滤：场景必须在 split 清单里，且目录下必须有 `vert.npy`。

**逐帧处理与四类 bin 的写盘**。

[tools/part3d_glamm_data_utils.py:176-214](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L176-L214)

这段代码是每帧的完整处理：先取 QA（`_process_qa` 返回 None 就整帧丢弃——**QA 是帧的准入条件**）；再把 `vert.npy` 原样 `tofile` 成 `points/<frame>.bin` 并记录相对路径；超点标签写到 `super_points/`（仅当文件存在）；非 test 模式下把 object/part 标签 `.astype(np.int64)` 后写到 `object_mask/`、`part_mask/`；最后塞一个 `gt_num=0`、`axis_align_matrix` 为 4×4 单位阵的空 `annos`，好让 v2 更新器有结构可填（源码注释 [tools/part3d_glamm_data_utils.py:211-213](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L211-L213) 自己说明了这一点）。

三个 dtype 层面的观察（自制数据集时必须记住）：

| 文件 | 写盘 dtype | 读回 dtype | 结论 |
|---|---|---|---|
| `points/*.bin` | npy 原样（HF 为 float32） | mmdet3d `LoadPointsFromFile` 默认 float32（[configs/ground3dlmm_train.py:106-109](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L106-L109)） | 两侧约定 float32 |
| `super_points/*.bin` | npy **原样**（无 astype） | [uniseg3d/loading.py:124-129](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/loading.py#L124-L129) 按 int64 | **契约：源 npy 必须本来就是 int64**，否则读回错乱 |
| `object_mask/`、`part_mask/*.bin` | 显式 `.astype(np.int64)` | [uniseg3d/loading.py:52-56](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/loading.py#L52-L56) 按 int64 | 转换器替你保证 |

也就是说：object/part 的 dtype 是转换器强制的，而超点标签是「裸搬运」——如果自制数据的 `sp_label.npy` 存成 int32，写盘不报错，读回时两个 int32 会被拼成一个 int64，**长度腰斩、数值错乱**，且报错位置远在训练期。

**get_infos 的串行循环**。

[tools/part3d_glamm_data_utils.py:216-223](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L216-L223)

这段代码对帧 ID 列表串行调用 `_process_frame`，返回 None（被丢弃的帧）不进列表。`num_workers` 参数收下但没用——`--workers 8` 不会加速，全量转换耗时主要由 npy 读写决定。

顺带一提，同一文件里还有 `_ScanNetSceneGroundingData` / `ScanReferData` / `Reason3dData` 三个**整场景**转换类（[tools/part3d_glamm_data_utils.py:244-443](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L244-L443)），它们服务 u7-l1 的迁移基准，本讲只需知道「同文件、不同数据形态、别混淆」。

#### 4.2.4 代码实践：自造迷你场景跑通转换链

**实践目标**：不下载完整数据集（约 150 GB），用 3 个自造帧验证转换器的准入规则与产物结构。

**操作步骤**：

1. 把下面的造数据脚本保存到仓库**外**的任意位置（如 `/tmp/make_mini.py`）。**这是示例代码，不是项目原有代码**：

   ```python
   # /tmp/make_mini.py —— 构造一个 2 场景、3 帧的迷你 Ground3D 布局
   import os
   import json
   import numpy as np

   root = '/tmp/mini_ground3d'
   N = 100  # 每帧点数

   def frame(scene, n, with_qa=True):
       d = f'{root}/processed_data/{scene}'
       os.makedirs(d, exist_ok=True)
       rng = np.random.default_rng(n)
       np.save(f'{d}/frame_{n}_vert.npy', rng.rand(N, 6).astype(np.float32))
       np.save(f'{d}/frame_{n}_sp_label.npy', rng.integers(0, 5, N).astype(np.int64))
       np.save(f'{d}/frame_{n}_object_label.npy', rng.integers(0, 3, N).astype(np.int64))
       np.save(f'{d}/frame_{n}_part_label.npy', rng.integers(0, 6, N).astype(np.int64))
       if with_qa:
           qa = {'0': {'functional_part_grounding':
                       [{'question': 'Point to the seat.', 'answer': 'The seat.<p>3</p>'}]}}
           od = f'{root}/refined_qa_data/part_qa/{scene}/final_json_outputs'
           os.makedirs(od, exist_ok=True)
           json.dump(qa, open(f'{od}/{n}_part_mapping.json', 'w'))

   frame('scene0000_00', 0)   # 正常帧
   frame('scene0000_00', 1, with_qa=False)   # 无 QA 帧 → 应被丢弃
   frame('scene0001_00', 0)   # 另一场景的正常帧

   open(f'{root}/part_ground3d_train.txt', 'w').write('scene0000_00\nscene0001_00\n')
   open(f'{root}/part_ground3d_val.txt', 'w').write('scene0001_00\n')
   ```

2. 运行 `python /tmp/make_mini.py`，然后在仓库根目录执行转换（与 [README.md:169-173](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L169-L173) 的官方命令同构，只换路径）：

   ```bash
   python tools/create_data.py --dataset part3d_glamm \
       --root-path   /tmp/mini_ground3d \
       --out-dir     /tmp/mini_ground3d \
       --qa_data_dir /tmp/mini_ground3d/refined_qa_data
   ```

3. 检查产物：`ls /tmp/mini_ground3d/{points,super_points,part_mask,object_mask}` 与 `ls /tmp/mini_ground3d/*.pkl`。

**需要观察的现象**：终端先打印 `train: 2 frames`（无 QA 的帧 1 在 `_process_frame` 里被丢）；随后 tqdm 进度条走完；v2 更新阶段打印 `Warning, you may overwriting the original data ...` 并暂停 5 秒（见 4.4）。

**预期结果**：`points/` 下只有 2 个 bin（`scene0000_00_frame_0.bin`、`scene0001_00_frame_0.bin`）；`part3d_glamm_infos_train.pkl` / `part3d_glamm_infos_val.pkl` 各一个；每个 points bin 大小应为 \( 100 \times 6 \times 4 = 2400 \) 字节，每个 mask bin 应为 \( 100 \times 8 = 800 \) 字节。（待本地验证）

#### 4.2.5 小练习与答案

**练习 1**：一个帧所在场景在 split 清单里、有 `vert.npy`，但 `sp_label.npy` 缺失，会发生什么？
**答案**：帧不丢。`if os.path.exists(sp_file)` 分支直接跳过，info 里没有 `super_pts_path` 键（[tools/part3d_glamm_data_utils.py:191-195](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L191-L195)）。但下一讲的 `LoadAnnotations3D_` 需要按 `super_pts_path` 读超点（[uniseg3d/loading.py:118](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/loading.py#L118)），缺键会在训练期爆 `KeyError`——典型的「转换期静默、训练期爆炸」隐患。

**练习 2**：为什么 `_process_frame` 里 QA 检查放在点云读写**之前**？
**答案**：语义上「无 QA 的帧对本项目无用」（模型的训练样本是问答对），先判 QA 把无效帧挡在最前面；效率上避免先写出 bin 再丢弃造成的无谓磁盘写入。

**练习 3**：`info['annos']` 为什么要填 `axis_align_matrix = np.eye(4)`？
**答案**：帧级部分场景没有 ScanNet 整场景那种轴对齐变换，但 v2 更新器会从 `annos` 里取这个矩阵（[tools/update_infos_to_v2.py:425-427](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/update_infos_to_v2.py#L425-L427)）。填单位阵表示「恒等变换」，让下游拿到一个合法的 4×4 矩阵而不需要特判。

### 4.3 QA 展平与 `<SEG>` 补齐

#### 4.3.1 概念说明

u2-l1 讲过单轮 QA JSON 的嵌套结构是 `{条目id: {任务名: [{question, answer}, ...]}}`——按「条目」组织。但训练时的采样单元是「某任务下的一条 QA」，与条目 id 无关。`_flatten_qa_data` 就是把这层嵌套压成按任务索引的字典，顺便做三件清洗：跳过统计键、跳过未知任务、给缺标记的问题补 `<SEG>`。

`<SEG>` 是模型专用的「接地占位符」：问题末尾的 `<SEG>` 相当于对模型说「请把你指的东西用掩码给我」；回答里的 `<p>id</p><SEG>` 则把指代落成具体实例编号（u1-l1、u1-l4 已建立这个概念；u4-l4 会讲它的隐状态如何被抽成分割查询）。**在转换期统一补齐**后，训练管线（u2-l4 的 `QA_Generation_v2`）就不必再处理「问题有没有标记」的分支。

#### 4.3.2 核心流程

单轮展平的变换：

```text
输入  {id_1: {task_a: [qa, qa], task_b: [qa]},
       id_2: {task_a: [qa]},
       'scene_level_metrics': <非 dict，跳过>}
输出  {'qa_data': {task_a: [qa, qa, qa], task_b: [qa]}}
```

多轮展平不按任务分组，输出「会话列表的列表」：每个元素是一整段多轮对话（`[{question, answer}, ...]`）。

`<SEG>` 补齐的两条规则对比：

| QA 层级 | 补齐条件 | 代码依据 |
|---|---|---|
| 单轮（part/object） | 问题缺 `<SEG>` 就**无条件**补，不看答案 | [tools/part3d_glamm_data_utils.py:59-60](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L59-L60) |
| 多轮 | 仅当**该轮答案含 `<SEG>`** 且问题缺时才补 | [tools/part3d_glamm_data_utils.py:83-84](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L83-L84) |

多轮更保守的原因：多轮对话里大量轮次是纯文本追问（如「那它多宽？」），答案不含 `<SEG>`，这类轮次不需要接地标记；而单轮任务按设计每条都是接地/测量类问题，统一补更省事。

#### 4.3.3 源码精读

**单轮展平函数**。

[tools/part3d_glamm_data_utils.py:42-65](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L42-L65)

这段代码逐条完成：文件不存在返回 `None`；顶层值不是 dict 的（如统计块）跳过；任务名不在 `ALL_TASKS` 或值不是 list 的跳过；对每条 QA 先 `copy.deepcopy`（防止补 `<SEG>` 的原地修改污染共享对象），再在问题缺 `<SEG>` 时追加；最后用字典推导 `{k: v for k, v in ... if v}` 删掉空任务键，返回 `{'qa_data': {task: [items]}}`。这个「空 dict 视为空」的语义决定了 4.2 中 `_process_qa` 的丢帧判断。

`ALL_TASKS` 常量（9 个任务名，与 README 的 9 类子任务一致）：

[tools/part3d_glamm_data_utils.py:34-39](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L34-L39)

这段列表是任务名的**唯一合法集合**：不在名单里的任务名在展平时被静默丢弃。它同时也是评估旋钮 `task_level` 的取值域（u1-l4）。

**多轮展平函数**。

[tools/part3d_glamm_data_utils.py:68-88](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L68-L88)

这段代码遍历顶层条目，把每个 list 型值作为一段对话原样收进 `out`；对每轮做「答案含 `<SEG>` 且问题不含则补」的条件补齐；整体为空时返回 `None`。注意与单轮版不同，这里**没有 deepcopy**，是直接改写读进来的 dict——对本脚本无碍（进程内一次性使用），但复用这段代码时要小心。

**三路 QA 拼装与丢帧判定**。

[tools/part3d_glamm_data_utils.py:155-173](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L155-L173)

这段代码按 u2-l1 讲过的三层路径分别读 part/object/multi 的 JSON：`part_qa/<scene>/final_json_outputs/<n>_part_mapping.json`、`object_qa/.../<n>_object_mapping.json`、`multiturn_qa/<scene>/frame_<n>_multi_conv.json`。三个返回值都可能是 `None`；「文件存在但展平后没有任何任务」也被归一化成 `None`（第 160-166 行的 len 判断）。三者全 `None` 才丢帧，否则返回 `{'part_qa_data': ..., 'object_qa_data': ..., 'multi_turn_qa_data': ...}`——这三个键名就是配置里 `task_level` 的三个取值（u1-l4）。

#### 4.3.4 代码实践：单测式验证 `_flatten_qa_data`

**实践目标**：用一个刁钻的输入验证展平与补齐规则，不需要完整数据集。

**操作步骤**：

1. 在仓库根目录执行（**示例代码**）：

   ```python
   import json, sys
   sys.path.insert(0, 'tools')
   from part3d_glamm_data_utils import _flatten_qa_data

   tricky = {
       'scene_level_metrics': {'iou_mean': 0.73},          # 非 dict 顶层值 → 跳过
       'item_1': {
           'functional_part_grounding': [
               {'question': 'Point to the handle.', 'answer': 'Here.<p>2</p>'},
               {'question': 'Where is the door?<SEG>', 'answer': 'There.<p>1</p>'},
           ],
           'made_up_task': [{'question': '?', 'answer': '!'}],   # 未知任务 → 跳过
       },
       'item_2': {
           'existence_verification': [
               {'question': 'Is there a lamp?', 'answer': 'Yes.'},  # 无 <p>，问题也会被补 <SEG>
           ],
       },
   }
   json.dump(tricky, open('/tmp/tricky.json', 'w'))
   out = _flatten_qa_data('/tmp/tricky.json')
   print(json.dumps(out, indent=2))
   ```

2. 仔细看打印出的两个问题字符串的结尾。

**需要观察的现象**：输出只含 `functional_part_grounding` 与 `existence_verification` 两个键；第一条问题的结尾多了 `<SEG>`，第二条保持原样（本来就带）；`made_up_task` 消失；顶层没有 `scene_level_metrics`。

**预期结果**：

```json
{"qa_data": {
  "functional_part_grounding": [
    {"question": "Point to the handle.<SEG>", "answer": "Here.<p>2</p>"},
    {"question": "Where is the door?<SEG>",   "answer": "There.<p>1</p>"}],
  "existence_verification": [
    {"question": "Is there a lamp?<SEG>", "answer": "Yes."}]}}
```

注意 `existence_verification` 的答案不含 `<p>` 也没有接地目标，但问题照样被补了 `<SEG>`——印证「单轮无条件补齐」。（待本地验证）

#### 4.3.5 小练习与答案

**练习 1**：如果未来数据集新增第 10 个任务 `color_reasoning`，只改 QA JSON 不改代码会怎样？
**答案**：该任务的所有 QA 在展平时被静默丢弃（不在 `ALL_TASKS`，[tools/part3d_glamm_data_utils.py:54](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L54)），没有任何警告。新增任务必须同步把名字加进 `ALL_TASKS`（u7-l2 的扩展链）。

**练习 2**：`_flatten_qa_data` 里的 `try/except Exception: continue` 包住了什么风险？
**答案**：包住「qa_item 不是 dict 或缺 `question` 键」的情况——此时 `item['question']` 抛 `KeyError`/`TypeError`，被吞掉后跳过该条。好处是一条脏数据不会毁掉整帧；坏处是脏数据完全不可见（没有日志）。排查「某帧 QA 数量比预期少」时要想到这里。

**练习 3**：为什么单轮用 `deepcopy` 而多轮不用？
**答案**：单轮版把 item 复制进新的聚合结构并会修改 `item['question']`，不 deepcopy 会改写 `json.load` 出来的原对象；多轮版直接把整个 `qa_list` 放进输出、原地改 `turn['question']`，没有「复制到新结构」这一步。两者是历史写法差异，语义上都正确（一次性脚本内）。

### 4.4 infos v2 更新：update_pkl_infos 与 v2 标准结构

#### 4.4.1 概念说明

`update_infos_to_v2.py` 改编自 mmdet3d 官方脚本，职责是把 v1 info 列表翻译成 OpenMMLab v2 标准结构。对 Ground3D 而言它做的其实是「**格式整形 + 原地覆盖**」：把 `pts_path` 挪进 `lidar_points.lidar_path` 并只留文件名、把 mask 相对路径挪到标准键名下、原样搬运 `qa_data`、给空 `annos` 生成空 `instances`，最后包上 `metainfo` 头重写同一个文件。

#### 4.4.2 核心流程

```伪代码
update_part3d_glamm_infos(pkl_path, out_dir):
    若 out_dir 出现在 pkl_path 里 → 打印覆盖警告并 sleep 5 秒
    data_list = mmengine.load(pkl_path)                 # v1 列表
    对每条 ori_info:
        temp = 空标准模板                                # lidar_points/radar_points/instances/...
        temp.lidar_points.num_pts_feats = 6
        temp.lidar_points.lidar_path  = 文件名(ori.pts_path)     # 剥掉目录！
        可选键: pts_part_mask_path / pts_object_mask_path /
                super_pts_path / qa_data                # 原样搬运
        temp.axis_align_matrix = ori.annos.axis_align_matrix.tolist()
        gt_num == 0 → instances = []
        clear_data_info_unused_keys(temp)               # 递归删除空/None 字段
    写 {metainfo, dataList} 到 out_dir/同名.pkl         # 原地覆盖！
```

#### 4.4.3 源码精读

**分发表**。

[tools/update_infos_to_v2.py:577-589](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/update_infos_to_v2.py#L577-L589)

`part3d_glamm` 与 `part3d_glamm_scannetpp` 共用 `update_part3d_glamm_infos`（两个数据源结构相同）；`reason3d`/`instruct3d` 共用另一套。这就是 `create_data.py` 里 `update_pkl_infos(info_prefix, ...)` 直接传前缀也能正确分发的原因——前缀恰好就是数据集名。

**空标准模板**。

[tools/update_infos_to_v2.py:124-150](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/update_infos_to_v2.py#L124-L150)

这段代码构造的空 dict 是 v2 的「全字段模板」：`lidar_points`、`radar_points`、`image_sweeps`、`instances` 等一应俱全（继承自自动驾驶数据集的模板）。对纯 3D 室内数据，多数字段最终会被清空删除。

**字段搬运与 qa_data 保留**。

[tools/update_infos_to_v2.py:403-419](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/update_infos_to_v2.py#L403-L419)

这段代码是 part3d_glamm 分支的核心搬运逻辑：`num_pts_feats=6` 进标准位置；四个路径键用 `Path(...).name` 把 `points/xxx.bin` 削成只剩 `xxx.bin`（目录部分交给配置里的 `data_prefix` 运行时重新拼接，下一讲详解）；`qa_data` 整块原样通过——**4.3 展平补齐后的结构就是训练直接消费的结构**，v2 更新不碰它。

**annos 处理与清空**。

[tools/update_infos_to_v2.py:423-448](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/update_infos_to_v2.py#L423-L448)

因为 4.2 里填的 `gt_num=0`，这里 `instances` 直接为空列表；`axis_align_matrix`（单位阵）被 `.tolist()` 成普通嵌套 list（pkl 里存 list 比 numpy 数组更通用）。随后 `clear_data_info_unused_keys` 递归删除所有 None/空字段（[tools/update_infos_to_v2.py:161-186](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/update_infos_to_v2.py#L161-L186)），模板里自动驾驶相关的空壳被清干净。

**原地覆盖写回与 metainfo 头**。

[tools/update_infos_to_v2.py:450-466](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/update_infos_to_v2.py#L450-L466)

`out_path = osp.join(out_dir, pkl_name)`——由于 `create_data.py` 传的 `out_dir` 就是写 v1 的目录，**输出文件名与输入相同**：v1 pkl 被 v2 pkl 原地覆盖。脚本用「打印覆盖警告 + sleep 5 秒」提醒（[tools/update_infos_to_v2.py:387-390](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/update_infos_to_v2.py#L387-L390)）。最终结构是 `{metainfo: {...}, dataList: [...]}`，metainfo 含 18 个 ScanNet 类别表、`dataset='part3d_glamm'`、`info_version='1.1'`。这个类别表对部件级任务并无实际作用（模型不用 bbox 检测头，`instances` 恒为空），只是 v2 规范要求有 `categories` 字段。

转换结束后，最终 pkl 中每帧的条目形如（示意，键序无关）：

```python
{
  'lidar_points': {'num_pts_feats': 6, 'lidar_path': 'scene0000_00_frame_0.bin'},
  'super_pts_path': 'scene0000_00_frame_0.bin',
  'pts_part_mask_path': 'scene0000_00_frame_0.bin',
  'pts_object_mask_path': 'scene0000_00_frame_0.bin',
  'axis_align_matrix': [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]],
  'instances': [],
  'qa_data': {'part_qa_data': {'qa_data': {...}},   # 4.3 的产物原样嵌入
              'object_qa_data': None,
              'multi_turn_qa_data': None},
}
```

消费端衔接——训练配置用 `data_prefix` 把裸文件名拼回完整路径：

[configs/ground3dlmm_train.py:100-104](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L100-L104)

这段配置把 `points`/`part_mask`/`object_mask`/`super_points` 四个子目录名绑到 `pts`/`pts_part_mask`/`pts_object_mask`/`sp_pts_mask` 四个逻辑前缀上，与 `ann_file='part3d_glamm_infos_train.pkl'`（[configs/ground3dlmm_train.py:171](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L171)）配合，正好对应转换器写出的四个 bin 目录与 pkl——这也是 README「产物布局」一节（[README.md:185-193](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L185-L193)）的来源。

#### 4.4.4 代码实践：体检生成的 pkl

**实践目标**：验证 4.2.4 产出的 pkl 确实是 v2 结构，且路径与 QA 被正确搬运。

**操作步骤**：

1. 接着 4.2.4 的迷你数据集，运行检查脚本（**示例代码**）：

   ```python
   import mmengine, numpy as np
   d = mmengine.load('/tmp/mini_ground3d/part3d_glamm_infos_train.pkl')
   print('顶层键:', list(d.keys()))            # 期望 ['metainfo', 'dataList']
   print('metainfo.dataset:', d['metainfo']['dataset'])
   print('帧数:', len(d['dataList']))           # 期望 2（无 QA 帧已丢）
   info = d['dataList'][0]
   print('单帧键:', sorted(info.keys()))
   print('lidar_path:', info['lidar_points']['lidar_path'])
   print('qa_data 键:', list(info['qa_data'].keys()))
   q = info['qa_data']['part_qa_data']['qa_data']['functional_part_grounding'][0]
   print('补齐后的问题:', q['question'])        # 期望结尾有 <SEG>

   # bin 字节数对账
   n6 = np.fromfile('/tmp/mini_ground3d/points/' + info['lidar_points']['lidar_path'],
                    dtype=np.float32)
   sp = np.fromfile('/tmp/mini_ground3d/super_points/' + info['super_pts_path'],
                    dtype=np.int64)
   print('点数:', len(n6) // 6, '超点标签数:', len(sp))   # 期望 100 100
   ```

2. 对照打印结果与本节的「最终条目示意」。

**需要观察的现象**：顶层是 `metainfo` + `dataList` 两键；`lidar_path` 是**裸文件名**（没有 `points/` 前缀）；QA 问题结尾带 `<SEG>`；点数与超点标签数一致。

**预期结果**：与注释里的期望值一致；`instances` 为空列表；`axis_align_matrix` 是 4×4 单位阵的嵌套 list。（待本地验证）

#### 4.4.5 小练习与答案

**练习 1**：为什么 v2 更新后路径只剩文件名？这样不会丢信息吗？
**答案**：不会，这是刻意的解耦：pkl 只记录「这个样本用哪个文件」，而「文件在哪个目录」由配置的 `data_prefix` 在运行时拼接（下一讲 `parse_data_info` 会看到拼接代码）。好处是同一份 pkl 可以配合不同的磁盘布局（换盘、换挂载点）而无需重新转换。

**练习 2**：如果转换中途 Ctrl+C，磁盘上会留下什么状态？
**答案**：可能留下「部分 bin + v1 版 pkl」的组合（v2 覆盖发生在每个 split 的最后一步 `update_pkl_infos`）。v1 pkl 的顶层是 list 而不是 `{metainfo, dataList}`，用 4.4.4 的体检脚本一验便知；重跑一次转换即可修复（转换是幂等的：bin 原样重写，pkl 重新生成再覆盖）。

**练习 3**：`metainfo['categories']` 里的 18 个类别在本项目里有什么用？
**答案**：基本没有实际用处。Ground3D-LMM 不做 bbox 检测，`instances` 恒为空；这个表是 v2 规范的字段要求，也是从官方 ScanNet 转换器复制来的残留，不影响训练与评估。

## 5. 综合实践

把本讲四个模块串成一个完整的「数据管线验收」任务：

**任务**：为自制（或下载的）Ground3D 格式数据做一次端到端转换，并产出一份验收清单。

1. **造数据**：按 4.2.4 的脚本造迷你布局，但这次加入三处故意埋的坑——
   - 一个没有任何 QA 的帧（验证准入规则）；
   - 一条任务名为 `made_up_task` 的 QA（验证 `ALL_TASKS` 过滤）；
   - 把某个 `sp_label.npy` 存成 int32（验证 4.2.3 表格里的 dtype 契约——转换不报错，但读回长度会从 100 变 50）。
2. **转换**：运行 `tools/create_data.py`（参数同 4.2.4），记录终端上的帧数打印、警告与 5 秒停顿。
3. **验收**：运行 4.4.4 的体检脚本，逐项核对：顶层两键、帧数 = 造出的正常帧数、`<SEG>` 已补、`made_up_task` 不在 `qa_data` 里。
4. **对账**：用 `np.fromfile` 分别按 float32/int64 读 points 与某个 mask，验证字节数满足 \( 24N \) 与 \( 8N \)；再故意按 int64 读那个 int32 的 super_points bin，观察长度减半的现象。
5. **反思**：写下你会给「下一个想接入自制数据集的人」的三条忠告（提示：split 清单、QA 准入、dtype 契约）。

预期：全部现象都能用本讲引用的源码行解释。（待本地验证）

## 6. 本讲小结

- `tools/create_data.py` 是薄入口：按 `--dataset` 分发，`--extra-tag` 决定 pkl 文件名（ScanNet++ 需 `part3d_glamm_scannetpp` 才能对上训练配置）；`--workers` 在当前实现里不产生并行效果。
- `Part3dGlammData` 以 `part_ground3d_{split}.txt` 为唯一 split 权威，扫 `vert.npy` 定帧；**无 QA 的帧整帧丢弃**，缺 sp/obj/part 标签则静默跳过对应键。
- 四类 npy 被写成裸 `.bin`：object/part 强制 int64，super_points 原样搬运——读端统一按 int64（`uniseg3d/loading.py`），因此源 npy 的 dtype 是隐含契约。
- `_flatten_qa_data` 把 `{id:{task:[...]}}` 压成 `{'qa_data':{task:[...]}}`，过滤统计键与未知任务；单轮 QA 无条件补 `<SEG>`，多轮只在答案含 `<SEG>` 时补。
- `update_pkl_infos` 做 v1→v2 整形：路径削成裸文件名（目录交给 `data_prefix`）、`qa_data` 原样搬运、空 annos 变空 instances，最后**原地覆盖**同一个 pkl 文件。

## 7. 下一步学习建议

转换产物已经躺在 `part3d_glamm_infos_{train,val}.pkl` 里了。下一讲 **u2-l3《数据集类与点云/标注加载》** 将站到消费端：读 `uniseg3d/scannet_dataset.py` 的 `Part3DGlamm_Dataset.parse_data_info`，看它如何用 `data_prefix` 把本讲的裸文件名拼回完整路径；再读 `uniseg3d/loading.py` 的 `LoadAnnotations3D_`，看它如何按 int64 契约把四个 bin 读成 `sp_pts_mask` / `pts_part_mask` / `pts_object_mask`。建议先自己重读一遍 [uniseg3d/loading.py:23-138](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/loading.py#L23-L138) 打底。之后 u2-l4 会讲 `QA_Generation_v2` 如何消费本讲展平出的 `qa_data`。
