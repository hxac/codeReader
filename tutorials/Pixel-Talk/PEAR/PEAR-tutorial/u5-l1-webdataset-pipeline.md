# u5-l1 WebDataset 数据管线：tar 分片、RandomMix 与 example_formatter

## 1. 本讲目标

前四个单元我们一直在读「推理链路」：图像进去、参数出来、网格渲染。从本讲开始进入**训练侧**。训练与推理最大的差别不在网络，而在**数据**——PEAR 用 WebDataset 流式读取海量 tar 分片，把每条「原始帧 + 逐人标注」实时裁剪、增广、变换成一个训练样本。

学完本讲，你应该能：

1. 说出 `load_tars_as_wds` 如何把 tar 分片变成流式样本：`wds.WebDataset` → `decode("rgb8")` → `rename` → `example_formatter` 四级流水线，以及 `resampled=True`、`with_epoch(50_000)`、`shuffle(1000)` 各自的作用。
2. 说出 `build_web_tracked_data` 如何用 `MixedWebDataset + wds.RandomMix` 按权重混合多个数据集，权重如何归一化成采样概率。
3. 逐字段说清 `example_formatter` 的输出：一个样本从 `annotation.pyd`（pickle 标注）变成包含 `ehm_image`、`smplx_coeffs`、`flame_coeffs`、两套关键点、渲染相机等字段的训练字典。
4. 掌握 `get_example` 的数据增强：随机缩放/旋转/平移/翻转/极端裁剪/颜色抖动，以及**图像、2D/3D 关键点、SMPL 参数三类数据如何在同一增广下保持同步**。
5. 亲手从官方示例 `000000.tar` 取出一个样本，打印各字段形状，并标注哪些字段与推理输出（`body_param` / `flame_param` / `pd_cam`）一一对应。

## 2. 前置知识

### 2.1 什么是 WebDataset

传统 `Dataset + DataLoader` 要求每个样本是一个独立文件，海量小文件会让文件系统与对象存储崩溃。WebDataset 的做法是：

- 把上千个样本打成**一个大 tar 分片（shard）**，例如 `000000.tar`、`000001.tar`……
- **一个样本 = tar 内一组同前缀文件**。例如 `xxx.jpg`（整帧图像）和 `xxx.annotation.pyd`（该帧的逐人标注）同属一个样本，`xxx` 就是样本 key。
- 读取是**流式**的：顺序（或随机重采样）地读 tar，解一个样本、处理一个样本，不把整个数据集放内存，也不需要索引文件。
- webdataset 的默认解码器认识一批后缀：图像类解码成 numpy 数组，`.pyd` 按 pickle 反序列化成 Python 对象，`.json` 解析成 dict 等。文件名里**第一个点之后的全部内容**作为字典键，所以 `xxx.annotation.pyd` 在样本字典里的键是 `annotation.pyd`（两点后缀）。

### 2.2 配置如何驱动数据集（承接 u2-l1）

本讲大量使用 [configs/train.yaml](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/train.yaml) 的 `DATASET` 段。回顾 u2-l1：`ConfigDict(model_config_path='configs/train.yaml')` 读 YAML 成 dict 再包一层只读 OmegaConf，`meta_cfg.DATASET.datasets` 是一个**列表**，每个元素描述一个子数据集（`name` / `item.urls` / `item.epoch_size` / `weight`）。

### 2.3 两套关键点格式

- **SMPL 关键点（44 点）**：25 个身体点 + 19 个额外点（脚、头等），来自 SMPL 系拟合标注，`smpl_keypoints_2d/3d`。
- **DWPose 全身关键点（134 点）**：24 身体 + 68 面部 + 42 手部，来自 DWPose 检测器，`dwpose_keypoints_2d/3d`。

每条标注**只有其中一套是有效的**（由数据来源决定），另一套在样本里用全零占位——这是本讲要讲的一个关键机制 `smpl_kp` 开关。

### 2.4 RLE 人体掩码

部分标注带 `mask` 字段，是 pycocotools 的 RLE（run-length encoding）编码的二值人体掩码，用 `pycocotools.mask.decode` 还原成 `H×W` 的 0/1 数组。它把「人」从背景里抠出来，供渲染监督使用。

### 2.5 2×3 仿射变换

`cv2.warpAffine(img, trans, (W,H))` 用一个 2×3 矩阵 `trans` 做图像几何变换。2×3 共 6 个自由度，恰好由**三对不共线的对应点**唯一确定（`cv2.getAffineTransform`）。记住这一点，`gen_trans_from_patch_cv` 的「三对点求裁剪矩阵」就很好懂。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [dataset/webdata_loader.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py) | 数据管线主体：`load_tars_as_wds`（tar→流式样本）、`apply_example_formatter`（标注→训练样本）、`build_web_tracked_data`（多数据集混合装配） |
| [dataset/dataset_utils.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/dataset_utils.py) | 几何与增广工具箱：`get_example`、`do_augmentation`、`gen_trans_from_patch_cv`、`fliplr_params`、`rot_aa`、极端裁剪系列 |
| [configs/train.yaml](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/train.yaml) | `DATASET` 段声明子数据集、urls、权重；`TRAIN` 段声明 batch_size / train_iter |
| [train_ehms.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/train_ehms.py) | 训练入口：调用 `build_web_tracked_data` 构造 train/val 数据集并接 DataLoader |
| [models/pipeline/pipeline.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py) | **消费端证据**：`run_fit` 训练循环里逐字段使用本讲产出的样本（本讲只引用、精读留给 u5-l2） |
| [models/smplx/smplx_head.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py) | 推理侧输出字典的键名（用于与 GT 字段做一一对应） |

两个阅读提示（延续 u1-l3 的「孤儿模块」审计结论）：

- `dataset/webdata_loader.py` 里的 `pt_decoder`（L42-L46）、`decode_images`（L72-L80）、`DEFAULT_IMG_SIZE`（L51）以及 import 进来的 `expand_to_aspect_ratio` 在本文件中**均未被调用**——它们只被姊妹文件 `webdata_loader_render.py` 使用，属「复制式开发」的遗留。
- `dataset/__init__.py` 引用的 `data_loader.py / data_loader2.py / data_loader3.py` **不在仓库里**（只剩 `__pycache__` 里的 .pyc）。由于 Python 导入子模块必先执行包的 `__init__.py`，这会让 `from dataset.webdata_loader import ...` 直接报 `ModuleNotFoundError`——这是本讲实践中要跨过的第一个坑，详见 4.3.4。

## 4. 核心概念与源码讲解

### 4.1 load_tars_as_wds：从 tar 分片到流式样本

#### 4.1.1 概念说明

`load_tars_as_wds` 是数据管线的「取水口」。它解决三个问题：

1. **去哪取**：`urls` 支持 `{000000..000014}.tar` 这种花括号展开（`braceexpand`），也支持 `~` 与环境变量展开（`expand_urls`）。
2. **怎么取**：训练时用 `resampled=True` 让 shard 以「有放回随机重采样」方式无限流出——数据集没有「读完」的概念；测试时则顺序读且不打乱 shard 顺序。
3. **取出后是什么**：`decode("rgb8")` 把 `.jpg` 解成 uint8 RGB 数组、把 `.annotation.pyd` 解成 pickle 对象；`rename` 把 `jpg/jpeg/png` 中第一个出现的后缀统一改名为 `jpg`；最后挂上 `example_formatter`（4.2 节）把原始样本变成训练样本。

#### 4.1.2 核心流程

```text
urls（可含 {} 通配）
   │  expand_urls：braceexpand + 展开 ~ 与环境变量 → URL 列表
   ▼
wds.WebDataset(urls,
    nodesplitter = wds.split_by_node,   # DDP 多卡分片：不同卡取不同 shard
    shardshuffle = True,                # 训练时打乱 shard 顺序（test 为 False）
    resampled    = True,                # shard 无限有放回重采样（仅训练分支）
    cache_dir    = None)                # 不落盘缓存
   │
   ▼ .decode("rgb8")      # 图像→uint8 RGB；.pyd→pickle 对象；键 = 首个点后的完整后缀
   ▼ .rename(jpg="jpg;jpeg;png")   # 别名归一：三种图像后缀统一叫 'jpg'
   ▼ apply_example_formatter       # dataset.map(example_formatter)，见 4.2
   ▼
一个个「训练样本 dict」流出
```

随后在 `build_web_tracked_data` 里完成混合与定长：

```text
对 DATASET.datasets 里每个 ds_cfg:
    datasets.append(load_tars_as_wds(ds_cfg.item.urls, ds_cfg.item.epoch_size, split))
    weights.append(ds_cfg.weight)
weights ← weights / sum(weights)
train_dataset = MixedWebDataset();  train_dataset.append(wds.RandomMix(datasets, weights))
# train: train_dataset.with_epoch(50_000).shuffle(1000, initial=1000)
# valid: train_dataset.with_epoch(1_000).shuffle(1000, initial=1000)   （shuffle 可关）
```

要点：

- **RandomMix 的采样概率**就是归一化权重：\[ P(\text{第 } i \text{ 个数据集}) = \frac{w_i}{\sum_j w_j} \] 每取一个样本都独立掷一次骰子，所以「大权重数据集出现得更频繁」，而不是「先读完一个再读下一个」。
- **`with_epoch(N)`** 把「无限流」切成每「轮」N 个样本的伪 epoch——因为 `resampled=True` 下流永远不会枯竭，必须人为定义轮界，否则 DataLoader 的 epoch 语义（进度条、保存 checkpoint 的节奏）无从谈起。
- **`shuffle(1000, initial=1000)`** 是缓冲区打乱：维护 1000 个样本的滑窗，输出时随机取一个再补一个，对抗「tar 内样本顺序高度相关」（同一视频的相邻帧挤在一起）。
- `MixedWebDataset` 只是给 `RandomMix` 套了一层 `wds.WebDataset`（DataPipeline）外壳，让 `with_epoch` / `shuffle` 这些流水线组合子可用——它本身不含任何数据变换。

#### 4.1.3 源码精读

**入口：训练脚本如何装配数据集。** [train_ehms.py:L44-L56](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/train_ehms.py#L44-L56) 读配置后分别用 `split='train'` 与 `split='valid'` 构造两个数据集，再接 `torch.utils.data.DataLoader`：训练 batch_size 取自 `TRAIN.batch_size`（当前为 40），**不传 `shuffle=True`**——WebDataset 是 IterableDataset，打乱已由 shard 级（`shardshuffle`）+ 样本级（`shuffle(1000)`）自己完成，DataLoader 再开 shuffle 反而会报错。

**配置：数据集清单。** [configs/train.yaml:L125-L132](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/train.yaml#L125-L132) 当前只启用了一个名为 `Sample` 的演示数据集，urls 指向 `./ehm_datasets/000000.tar`。注意两点：其一，README（[README.md:L117-L127](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L117-L127)）把示例 tar 放在 `ehms_datasets/` 目录，而配置写的是 `ehm_datasets/`（少一个 s）——**以配置为准**，下载后放到 `ehm_datasets/000000.tar`；其二，被注释掉的正式数据集（如 [configs/train.yaml:L139](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/train.yaml#L139)）展示了真实用法：`urls: ./ehm_datasets/mpii_hmr2/{000000..000014}.tar` 一个数据集横跨 15 个分片。`epoch_size` 字段虽然被传进 `load_tars_as_wds`，但函数体内从未使用它（见下），是死配置。

**URL 展开。** [dataset/webdata_loader.py:L82-L90](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L82-L90) 先 `os.path.expanduser/expandvars` 展开 `~` 和环境变量，再用 `braceexpand` 把 `{000000..000014}` 展开成 15 个具体路径，最后返回 URL 列表。

**建流。** [dataset/webdata_loader.py:L321-L345](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L321-L345) 中，`split != 'test'` 走 [L333-L339](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L333-L339)：`shardshuffle=True` 打乱分片顺序、`resampled=True` 无限重采样、`cache_dir=None` 不缓存。注意签名里的 `resampled` 形参（接收 `ds_cfg.item.epoch_size`）在函数体内**没有任何引用**——真正的 `resampled=True` 是 L337 的字面量。随后三行 [L341-L343](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L341-L343) 依次 `decode("rgb8")` → `rename(jpg="jpg;jpeg;png")` → `apply_example_formatter(dataset)`（即 `dataset.map(example_formatter)`）。

**混合与定长。** [dataset/webdata_loader.py:L394-L437](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L394-L437) 是 `build_web_tracked_data` 全貌：L396-L399 按 `split` 选 `cfg_dataset.datasets`（train）或 `cfg_dataset.val_datasets`（valid/test）；L404-L415 循环建子数据集并收集权重；L417-L421 权重归一化后 `MixedWebDataset()` + `append(wds.RandomMix(datasets, weights))`；L429-L436 train 分支 `with_epoch(50_000).shuffle(1000, initial=1000)`，valid 分支 `with_epoch(1000)` 且 shuffle 可用参数关闭。

**MixedWebDataset 外壳。** [dataset/webdata_loader.py:L37-L39](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L37-L39) `super(wds.WebDataset, self).__init__()` 跳过 `WebDataset.__init__`、直接初始化其父类 `DataPipeline`，得到一个**空流水线**；随后 `append(wds.RandomMix(...))` 把 RandomMix 作为唯一 stage 挂上去。这样 `with_epoch/shuffle` 等 DataPipeline 组合子就能用了。

**翻转换表（4.3 节会用）。** [dataset/webdata_loader.py:L53-L55](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L53-L55) 定义 SMPL 44 点的左右互换表 `FLIP_KEYPOINT_PERMUTATION`（25 身体 + 19 额外）；[L59-L64](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L59-L64) 定义 DWPose 134 点的 `FLIP_KEYPOINT_PERMUTATION_DWPOSE`（24 身体 + 68 面部 + 42 手部，面部点左右眼等成对互换、手部左右手整体交换位置）。

#### 4.1.4 代码实践

**实践 A：观察 tar 分片的内部结构**（不写训练代码，先看清「样本 = 同前缀文件组」）。

1. 实践目标：确认 `000000.tar` 里每个样本的文件命名，验证 `annotation.pyd` 确实是逐帧标注、`jpg` 是整帧图像。
2. 操作步骤：按 [README.md:L120-L127](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L120-L127) 下载示例 tar，放到 `ehm_datasets/000000.tar`（注意目录名以配置为准），然后：

   ```bash
   tar -tf ehm_datasets/000000.tar | head -20
   ```

3. 需要观察的现象：文件是否成对出现（`<key>.jpg` 与 `<key>.annotation.pyd` 共享同一个 key 前缀）。
4. 预期结果：**待本地验证**——根据消费端代码反推，tar 内应是形如 `xxxx.jpg` + `xxxx.annotation.pyd` 的成对文件；若你的 tar 里还有其他后缀（如 `.json`、`.png`），把它们记录下来，与 `rename(jpg="jpg;jpeg;png")` 的别名表对照。

**实践 B：只用裸 webdataset 走到 decode 为止**，看清 formatter 挂上前样本长什么样。

```python
# peek_shard.py（示例代码，仓库根目录运行）
import webdataset as wds

ds = wds.WebDataset('./ehm_datasets/000000.tar', shardshuffle=False)
ds = ds.decode("rgb8").rename(jpg="jpg;jpeg;png")
sample = next(iter(ds))
print(type(sample), list(sample.keys()))
print('jpg dtype/shape:', sample['jpg'].dtype, sample['jpg'].shape)
ann = sample['annotation.pyd']
print('annotation type:', type(ann), 'len:', len(ann))
print('第一个人标注的键:', sorted(ann[0].keys()))
```

1. 实践目标：确认 decode 后 `jpg` 是 uint8 HWC 数组、`annotation.pyd` 是「逐人标注的 list」，并打印单条标注的键集合。
2. 操作步骤：`pip install webdataset` 后在仓库根目录运行（该脚本不 import 项目内模块，可绕开 `dataset/__init__.py` 的缺文件问题）。
3. 需要观察的现象：`sample['jpg'].shape` 是 `(H, W, 3)`；`annotation.pyd` 的长度 ≥ 1（一帧里有几个人就有几条标注）。
4. 预期结果：标注键应包含 `id_params`、`smplx_params`、`flame_params`、`dwpose_keypoints_2d/3d`、`scale`、`center`、`head_valid`、`hand_valid`、`pose_valid` 等（由 [webdata_loader.py:L123-L233](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L123-L233) 的访问代码反推）。**待本地验证**：完整键集与 `smpl_keypoints_2d/3d` 是否为 `None` 因数据而异。

#### 4.1.5 小练习与答案

**练习 1**：如果两个子数据集权重分别是 `0.1` 和 `0.3`，训练时每个样本来自第二个数据集的概率是多少？`with_epoch(50_000)` 在一个「轮」里第二个数据集大约贡献多少样本？

答案：归一化后 \(P_2 = 0.3/(0.1+0.3) = 0.75\)，即约 75%；一个 50000 样本的伪 epoch 里约 \(50000 \times 0.75 = 37500\) 个样本来自它（RandomMix 逐样本独立采样，实际数目有随机波动）。

**练习 2**：为什么 `build_web_tracked_data` 里 `with_epoch` 的 N（训练 50000）远大于示例数据集本身的样本数（epoch_size 1000）？

答案：训练分支 `resampled=True` 使 shard 流是**无限的有放回重采样**，数据会被反复使用；`with_epoch(N)` 只是定义「一轮迭代多长」来配合进度与 checkpoint 节奏，N 与物理样本数无关。

**练习 3**：`ds_cfg.item.epoch_size` 这个配置最终生效了吗？

答案：没有。它作为 `resampled` 形参传入 `load_tars_as_wds`（[train_ehms.py:L45](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/train_ehms.py#L45) → [webdata_loader.py:L321](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L321)），但函数体从未读取该参数，实际 `resampled=True` 写死在 L337。属「死配置」。

### 4.2 example_formatter：从 annotation.pyd 到训练样本

#### 4.2.1 概念说明

`example_formatter` 是管线的「翻译官」：webdataset 解出的原始样本（整帧图像 + 逐人 pickle 标注）不能直接喂网络，需要翻译成训练循环认的字典。它做五件事：

1. **选人**：一帧里有几个人就随机挑一个（`random.randint`）——训练永远是单人 patch，与推理「检测框裁单人 patch」对齐。
2. **拼 GT 参数**：把 `smplx_params / flame_params / id_params` 三摊标注合并成 `smplx_coeffs / flame_coeffs` 两个字典，补齐 shape 维度、加有效性标志（`has_flame / has_hand / has_body`）。
3. **组装 GT 相机**：把标注的 3×4 `camera_RT_params` 包成 4×4 的 `w2c_cam / c2w_cam`。
4. **裁剪增广**：中心/尺度来自标注，把 RGBA（图像 + 人体 mask）送进 `get_example`（4.3 节）做裁剪与增广，得到 256×256 patch 及同步变换后的关键点、SMPL 参数。
5. **组装多分辨率图像族与渲染相机**：同一 patch 派生 518 / 256 / 512 三种分辨率和 `render_cam_params`。

#### 4.2.2 核心流程

```text
sample = {'__key__':…, 'jpg': H×W×3 uint8, 'annotation.pyd': [标注1, 标注2, …]}
   │ ① random.randint 选一个 cur_annotation
   ▼
fet_tracking_info_from_raw(cur_annotation)
   ├─ smplx_shape 10 维 → 补零到 200 维；flame_shape 10 → 300
   ├─ sam3d bug：joints_offset 首元素为 1 → 整体清零
   ├─ smplx_coeffs = smplx_params + {shape, joints_offset, head_scale, hand_scale}
   ├─ flame_coeffs = flame_params + {shape_params}
   ├─ hmr_pose 与 body_pose 双向同步（取 8 个关节位）
   ├─ has_flame/has_hand/has_body ← head_valid/hand_valid/pose_valid（0/1 整数）
   ├─ data_to_tensor → squeeze_params（全部转 torch 并压掉 batch 维）
   └─ w2c_cam = diag(-1,-1,1,1) @ [RT; 001]，c2w_cam = inverse(w2c_cam)
   │ ② 关键点来源二选一：有 smpl_keypoints_2d → smpl_kp=True（44 点），否则 DWPose（134 点）
   │ ③ 组装传给 get_example 的 smpl_params（global/body/hands/betas/has_flame）
   │ ④ mask：标注有 RLE → 解码 + render_valid=1；否则全 1 掩码 + render_valid=0
   │    img_rgba = concat([原图, mask], axis=2)   # H×W×4
   │ ⑤ get_example(img_rgba, center, bbox_size, kp2d, kp3d, smpl_params, flip表, 256,256,
   │               mean=0, std=255, do_augment=True)  → 4.3 节
   │ ⑥ 变换后的参数写回 smplx_coeffs；has_flame/has_body 各 +0.1（软门控）
   │ ⑦ smpl_kp=True → smpl_kp2d/3d 填真值、dwpose_kp2d/3d 全零；反之亦然（dwpose 还把髋点 8/11 置零）
   │ ⑧ ehm_image = 256 patch；image/mask resize 518；target_image/mask resize 512
   │ ⑨ render_cam_params ← get_full_proj_matrix(w2c_cam, tanfovx=1/24)
   ▼
训练样本 dict（字段总表见 4.2.3）
```

两个值得先建立直觉的设计：

- **软门控 `+0.1`**：`has_flame` 原本是 0/1 整数，在 `get_example` 里当布尔用（有 FLAME 头就禁翻转）；写回样本时 `+= 0.1` 变成 0.1 / 1.1 两档浮点，下游损失（u5-l3 的 `HeadParameterLoss/BodyParameterLoss`）用阈值判断有效性，非零值保证无效样本也能进 batch 而不是被丢掉。
- **双关键点通道「一真一零」**：网络输出的 134 点 DWPose 关键点与 44 点 SMPL 关键点是两条监督线，但每条标注只有一套真值，另一套填零张量，配合 `smpl_kp` 布尔开关让损失端自行选路（[pipeline.py:L334-L341](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L334-L341) 可视化处就是按 `batch['smpl_kp']` 分流的）。

#### 4.2.3 源码精读

**① 随机选人。** [dataset/webdata_loader.py:L189-L192](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L189-L192) 在 `annotation.pyd` 列表里随机取一条。多人帧每次迭代可能抽出不同的人——同一条数据天然产生多种训练样本。

**② 标注清洗与参数合并。** [dataset/webdata_loader.py:L125-L145](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L125-L145)：若 `smplx_shape` 是 10 维（SMPL 体型系数），补零到 200 维（SMPL-X 全长）；`flame_shape` 同理补到 300（u4-l1 讲过「网络/模型侧 300 维 shape、数据侧只存 10 维」的约定，这里是补零点）。[L132-L133](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L132-L133) 针对 sam3d 数据源的 bug：`joints_offset` 首元素为 1 时整体清零。[L149-L153](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L149-L153) 的 `hmr_pose` 同步：若标注带 HMR 风格的身体姿态，就用它的第 `[0,1,3,4,6,7,9,10]` 号关节覆写 `body_pose` 对应关节；没有则令 `hmr_pose = body_pose`，保证该键恒存在。

**③ tensor 化与有效性标志。** [dataset/webdata_loader.py:L156-L166](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L156-L166) 先 `data_to_tensor`（递归把 np/list 转 float32 tensor，[L95-L106](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L95-L106)）再 `squeeze_params`（压掉多余 batch 维，[L108-L113](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L108-L113)），最后从 `head_valid / hand_valid / pose_valid` 三个标注标量生成 `has_flame / has_hand / has_body`。

**④ GT 相机组装。** [dataset/webdata_loader.py:L169-L183](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L169-L183)：把 3×4 的 `camera_RT_params` 嵌进 4×4 单位阵得 `RT_mat`，再左乘 `c2c_mat = diag(-1,-1,1,1)`（x、y 轴取反，即绕 z 转 180°，完成坐标系约定转换）得 `w2c_cam`，求逆得 `c2w_cam`。这两枚矩阵是**真值侧的 4×4 RT**，与推理输出的 `pd_cam`（u3-l4：预测的弱透视参数组装出的 4×4 RT）扮演同一角色。

**⑤ 关键点来源二选一。** [dataset/webdata_loader.py:L195-L204](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L195-L204)：`smpl_keypoints_2d` 非空则用 44 点 SMPL 真值（`smpl_kp=True`，翻转表用 `FLIP_KEYPOINT_PERMUTATION`），否则用 134 点 DWPose（`smpl_kp=False`，翻转表换成 DWPose 版）。

**⑥ 送进 get_example 的参数包。** [dataset/webdata_loader.py:L209-L246](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L209-L246) 组装 `smpl_params`（global_orient / body_pose / 双手 pose / betas / has_flame），取 `scale.max()` 与 `center` 作裁剪框，把图像与 mask 拼成 4 通道 `img_rgba`，然后调用 `get_example(..., 256, 256, DEFAULT_MEAN, DEFAULT_STD, do_augment=True, ...)`。注意 [L49-L50](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L49-L50) 定义的均值/标准差是 `mean=0、std=255`，效果就是简单地把像素除以 255 归一到 \([0,1]\)（ImageNet 归一化不在数据侧做，而在训练前向里做，见 [pipeline.py:L193](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L193) 的 `self.normalize`——与推理侧 `Ehm_Pipeline.forward` 完全一致）。

**⑦ 写回与软门控。** [dataset/webdata_loader.py:L249-L257](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L249-L257) 把增广后的（可能翻转/旋转过的）SMPL 参数写回 `smplx_coeffs`，并给 `has_flame`、`has_body` 各加 0.1。

**⑧ 双通道关键点与髋点置零。** [dataset/webdata_loader.py:L265-L277](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L265-L277) 按 `smpl_kp` 把真值填进一套通道、另一套填 `torch.zeros`（源码注释写的 `[44,4]` 已过时，DWPose 通道实际是 `(134,3)/(134,4)`）；DWPose 分支额外把第 8、11 号点（两侧髋部）整行乘 0 抹掉——放弃这两个点的 2D 监督（3D 通道未做同样处理，源码层面原因是只动了 `dwpose_kp2d`）。

**⑨ 多分辨率图像族与渲染相机。** [dataset/webdata_loader.py:L285-L310](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L285-L310)：256 patch 原样存为 `ehm_image`（网络输入），再 resize 出 518 的 `image/mask`（训练中实际只用于可视化，消费点在 [pipeline.py:L320](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L320)；行内注释「518 for ehm」已过时）与 512 的 `target_image/target_mask`（渲染监督分辨率）。[L293-L300](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L293-L300) 用 `get_full_proj_matrix(w2c_cam, 1/24)` 生成 `render_cam_params`——`tanfovx = 1/24` 正是全仓恒定焦距 24（u3-l4），键名 `camera_center` 里存的其实是相机世界坐标 `c2w_cam[:3,3]`（命名与语义有漂移，读码时注意）。

**输出字段总表**（形状由源码推得，标注 ⚠ 的依赖标注内容、**待本地验证**）：

| 字段 | 形状 / 类型 | 含义 | 推理侧对应物 |
| --- | --- | --- | --- |
| `ehm_image` | `(3,256,256)` float，[0,1] | 网络输入 patch（消费点 [pipeline.py:L271](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L271)） | `pad_and_resize` / `generate_patch_image` 产出的 256 patch（u2-l2 / u2-l3） |
| `image` / `mask` | `(3,518,518)` / `(1,518,518)` | 可视化用大图 | — |
| `target_image` / `target_mask` | `(3,512,512)` / `(1,512,512)` | 渲染监督图 | Renderer2 的 1024 画布（u4-l5，分辨率不同） |
| `image_name` | str | 样本 key（`__key__`） | — |
| `smplx_coeffs` | dict：`global_pose` ⚠`(3,)`、`body_pose` ⚠`(21,3)`、`left/right_hand_pose` ⚠`(15,3)`、`shape` `(200,)`、`joints_offset`、`head_scale`、`hand_scale`、`has_hand`、`has_body`、`hmr_pose`、`camera_RT_params` `(3,4)` | SMPL-X GT 参数（轴角表示） | `outputs['body_param']` 同名键（`global_pose/body_pose/left_hand_pose/right_hand_pose/hand_scale/head_scale/joints_offset/exp/shape`，见 [smplx_head.py:L283-L300](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L283-L300)；预测是旋转矩阵、GT 是轴角，损失里换算，属 u5-l3） |
| `flame_coeffs` | dict：`shape_params` `(300,)`、表情/下颌/眼睑等 ⚠、`has_flame`（0.1/1.1） | FLAME GT 参数 | `outputs['flame_param']`（`jaw_params/eyelid_params/expression_params/shape_params` 等，[smplx_head.py:L275-L278](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L275-L278)） |
| `smpl_kp` | bool | 本次标注用的是哪套关键点 | — |
| `smpl_kp2d` / `smpl_kp3d` | `(44,3)` / `(44,4)`，xy∈[0,1] | SMPL 44 点（无效时全零） | —（SMPLest 系的 2D/3D 监督通道） |
| `dwpose_kp2d` / `dwpose_kp3d` | `(134,3)` / `(134,4)` | DWPose 134 点（无效时全零；kp2d 髋点 8/11 已抹除） | —（训练时与 `pred_kps2d` 对齐计算 2D 损失，[pipeline.py:L282-L283](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L282-L283)） |
| `w2c_cam` / `c2w_cam` | `(4,4)` | GT 相机外参 | `outputs['pd_cam']`（预测的 4×4 RT） |
| `render_valid` | float（0/1） | 标注是否带真实渲染 mask | — |
| `render_cam_params` | dict（view/proj 矩阵、tanfovx=1/24、512 画布、camera_center） | 渲染视角参数 | `GS_Camera` 的构造参数（u3-l4） |

#### 4.2.4 代码实践（源码阅读型：字段消费审计）

1. 实践目标：为上表每一行找到训练循环里的**消费点**，证明「字段确实被用到」或发现「备而未用」。
2. 操作步骤：打开 [models/pipeline/pipeline.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py)，在 `run_fit`（L261 起）与验证函数中搜索 `batch['` 开头的表达式（如 L271、L282-L294、L320、L334-L341、L352-L353、L427-L472），把每个字段出现在哪一行、被谁使用（前向输入 / 损失真值 / 可视化）填进自己的表格。
3. 需要观察的现象：`ehm_image/smplx_coeffs/flame_coeffs/smpl_kp2d/smpl_kp3d/dwpose_kp2d/image/smpl_kp` 都有消费点；`target_image/target_mask/render_cam_params/render_valid/w2c_cam/c2w_cam` 在当前 `run_fit` 主损失里**没有**消费点（渲染监督属另一条实验线，见 `webdata_loader_render.py`）。
4. 预期结果：得到一张「字段 → 消费行号 → 用途」审计表。这会让你在 u5-l2 读训练循环时事半功倍。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `has_flame` 要在 `get_example` 之后再 `+= 0.1`，而不是直接保持 0/1？

答案：`has_flame` 在 `get_example` 内部当布尔用（[dataset_utils.py:L653-L654](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/dataset_utils.py#L653-L654)，有效即禁翻转），此后加 0.1 变成 0.1/1.1 两档浮点：既保留「无效也不丢样本」的非零语义，又让下游损失用阈值（如 >0.5）区分有效/无效做门控加权。

**练习 2**：`w2c_cam = diag(-1,-1,1,1) @ [RT;0,0,0,1]` 中左乘对角阵的几何意义是什么？

答案：把 x、y 坐标同时取反，等价于绕 z 轴旋转 180°，用于在「PyTorch3D 系」与「COLMAP/图像」两种坐标约定之间换系（源码 L162-L163 的注释所述），保证后续透视投影与图像坐标系一致。

**练习 3**：一帧里有 3 个人，同一个 epoch 内这一帧被采样 5 次，产出的人是否一定相同？

答案：不一定。`random.randint(0, len(annotation.pyd)-1)` 每次独立选人（[webdata_loader.py:L189-L190](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L189-L190)），同一帧多次采样大概率覆盖不同的人与不同增广。

### 4.3 get_example：裁剪、翻转与关键点同步变换

#### 4.3.1 概念说明

`get_example` 是从 PARE/EpipolarPose 一脉相承的经典 HMR 数据增强函数（文件头部注明出处）。它要解决的核心问题是：**图像做几何增广时，2D/3D 关键点与 SMPL 参数必须跟着做完全同步的变换，否则真值就废了**。

- 图像：用 `cv2.warpAffine` 裁出 256×256 patch（含缩放、旋转、平移、水平翻转）。
- 2D 关键点：先做镜像（\(x' = W - 1 - x\) 并按左右互换表重排），再用同一个仿射矩阵 `trans` 逐点变换，最后除以 patch 宽归一到 \([0,1]\)。
- 3D 关键点：镜像重排 + 绕 z 轴旋转（与图像旋转同角度、反方向约定）。
- SMPL 参数：镜像时交换左右关节并取反轴角 y/z 分量；旋转时用 Rodrigues 公式把全局朝向转过去；`betas`（体型）与平移无关、保持不变。

此外它还负责**极端裁剪**（extreme cropping，EFT 论文提出的策略）：以一定概率只保留身体的一部分（髋以上/肩以上/只剩头/只剩一条腿……），逼网络学会从局部恢复全身。

#### 4.3.2 核心流程

```text
get_example(img_rgba, center, bbox_size, kp2d, kp3d, smpl_params, flip表, 256, 256, mean=0, std=255, do_augment=True)
 ① 图像已是 np.ndarray（RGBA 4 通道），记录 img_size
 ② do_augmentation() 掷 8 个增广参数：
      scale∈N(1,0.3) 截断 | rot∈±2×30° 以 0.6 概率 | do_flip 以 0.5 概率
      extreme_crop 以 0.1 概率 | color_scale∈[0.8,1.2]³ | tx,ty∈±0.02
    ★ 若 smpl_params['has_flame'] 为真 → 强制 do_flip=False（FLAME 表情参数无法镜像）
 ③ extreme_crop：按可见关键点把框缩到局部身体（aggressive 版 9 选 1）
 ④ center += (tx,ty)×size  （随机平移）
 ⑤ kp3d ← keypoint_3d_processing（镜像重排 + 绕 z 旋转）
 ⑥ trans ← gen_trans_from_patch_cv(center, size×scale, 256, rot)（三对点求 2×3 仿射）
    img_patch ← warpAffine(img, trans, (256,256))；若 do_flip 先整图翻转并镜像 center_x
 ⑦ smpl_params ← smpl_param_processing（fliplr_params + rot_aa）
 ⑧ 前 3 通道乘 color_scale、截断 [0,255]，再做 (x-0)/255 → [0,1]；第 4 通道（mask）不做归一化
 ⑨ kp2d ←（若 flip：镜像 + 重排）→ trans_point2d 逐点仿射 → /256 归一
 ⑩ 返回 img_patch(CHW float), kp2d, kp3d, smpl_params, img_size, trans
```

第 ⑧ 步有个隐蔽细节：归一化循环只跑前 3 个通道（`range(min(img_channels, 3))`，[dataset_utils.py:L718-L721](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/dataset_utils.py#L718-L721)），第 4 通道 mask 仍是 0..255。下游 [webdata_loader.py:L262](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L262) 用 `clip(0,1)` 接收——任何非零值被压成 1，**mask 就这样被二值化了**。读码时不要误以为 mask 保留了软过渡。

#### 4.3.3 源码精读

**① 增广参数。** [dataset/dataset_utils.py:L73-L101](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/dataset_utils.py#L73-L101) 的 `do_augmentation` 一次掷出 8 个参数；概率与幅度常量集中写在 [L14-L23](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/dataset_utils.py#L14-L23)（`FLIP_AUG_RATE=0.5`、`ROT_AUG_RATE=0.6`、`ROT_FACTOR=30`、`SCALE_FACTOR=0.3`、`TRANS_FACTOR=0.02`、`EXTREME_CROP_AUG_RATE=0.1`、`COLOR_SCALE=0.2`），要调增广强度改这里即可。

**② 有 FLAME 就不翻转。** [dataset/dataset_utils.py:L651-L656](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/dataset_utils.py#L651-L656)：`smpl_params['has_flame']` 为真时 `do_flip=False`。原因是 `fliplr_params` 只会镜像 SMPL 参数（交换左右关节 + 轴角取反），FLAME 的表情/下颌/双眼参数没有对应的镜像操作，翻转图像会让这些真值失配，索性禁用。

**③ 极端裁剪。** [dataset/dataset_utils.py:L661-L680](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/dataset_utils.py#L661-L680) 以 `EXTREME_CROP_AUG_RATE=0.1` 触发，`EXTREME_CROP_AUG_LEVEL=1` 走 aggressive 版 [L1050-L1097](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/dataset_utils.py#L1050-L1097)：按人体可见关键点把框缩到「髋上/肩上/头部/躯干/单臂/双腿/单腿」等 9 种局部之一（每种裁法是一个独立函数，如 [L789-L813](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/dataset_utils.py#L789-L813) 的 `crop_to_head`），太小的框（<4 像素）自动放弃保持原框。

**④ 三对点求仿射。** [dataset/dataset_utils.py:L120-L167](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/dataset_utils.py#L120-L167) 的 `gen_trans_from_patch_cv`：源侧取「框中心、中心+半个（旋转后的）高、中心+半个（旋转后的）宽」三点，目标侧取 256×256 画布的对应三点，`cv2.getAffineTransform` 解出唯一 2×3 矩阵。它同时吸收了 scale（框变大变小）、rot（框旋转）、以及 patch 化的平移缩放——一个矩阵管全部几何。这与推理侧 [dataset_utils.py 同名函数被 inference_images.py 调用] 是同一个函数：**训练裁剪与推理裁剪共享同一套几何代码**，这是「训练-推理一致性」最直接的落点。

**⑤ 图像裁剪与 alpha 特判。** [dataset/dataset_utils.py:L379-L399](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/dataset_utils.py#L379-L399)：先按 `do_flip` 翻转整图并镜像 `c_x`（\(c_x \leftarrow W-1-c_x\)），`warpAffine` 输出 256×256；因为传进来的是 4 通道 RGBA，若调用方选了非 constant 边界模式，alpha 通道会被强制用 constant 模式单独重采样（[L393-L397](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/dataset_utils.py#L393-L397)），保证 patch 外的区域 mask=0。

**⑥ 2D 关键点同步。** [dataset/dataset_utils.py:L722-L728](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/dataset_utils.py#L722-L728)：翻转时 `fliplr_keypoints`（[L518-L532](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/dataset_utils.py#L518-L532)）做 \(x' = W-1-x\) 再按左右互换表重排行；随后 `trans_point2d`（[L170-L181](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/dataset_utils.py#L170-L181)）用与图像**同一个** `trans` 逐点变换；最后 `keypoints_2d[:,:-1] /= patch_width` 归一到 \([0,1]\)（置信度列不动）。

**⑦ 3D 关键点同步。** [dataset/dataset_utils.py:L534-L557](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/dataset_utils.py#L534-L557)：镜像重排后，用 `np.einsum('ij,kj->ki', rot_mat, keypoints_3d[:,:-1])` 把每个点的 xyz 绕 z 轴旋转 \(-\text{rot}\) 度——2D 靠 `trans`、3D 靠 `rot_mat`，两者角度同源（都来自 `do_augmentation` 掷出的 `rot`），于是 2D 与 3D 真值永远同步。

**⑧ SMPL 参数同步。** [dataset/dataset_utils.py:L579-L593](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/dataset_utils.py#L579-L593) 调 `fliplr_params` + `rot_aa`。`fliplr_params`（[L461-L515](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/dataset_utils.py#L461-L515)）做两件事：按置换表交换左右身体关节（左肩 ↔ 右肩等），并把每个关节轴角的第 1、2 分量（y、z）取反——镜像一个旋转等价于绕取反轴转取反角；`left_hand_pose/right_hand_pose` 整体互换再各自取反。[L559-L577](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/dataset_utils.py#L559-L577) 的 `rot_aa` 用 Rodrigues 公式 \(R = \mathcal{R}(\omega)\) 把全局朝向左乘图像平面内旋转 \(\mathcal{R}_z(-\text{rot})\) 再转回轴角：图像转了多少度，全局朝向就补偿多少度。

**⑨ 颜色抖动与归一化。** [dataset/dataset_utils.py:L708-L721](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/dataset_utils.py#L708-L721)：HWC→CHW（`convert_cvimg_to_tensor`），三通道各自乘 `color_scale∈[0.8,1.2]` 的随机亮度系数并截断 [0,255]，再按传入的 mean=0 / std=255 归一，即 \(x \mapsto x/255 \in [0,1]\)。

#### 4.3.4 代码实践（本讲主实践）

**目标**：从真实数据取一个训练样本，逐字段打印形状，并标注与推理输出的对应关系。

**步骤 0：跨过导入坑（一次性）。** `dataset/__init__.py` 引用的 `data_loader/data_loader2/data_loader3` 不在仓库（u1-l3 已审计），直接 `from dataset.webdata_loader import ...` 会先执行该 `__init__.py` 而报 `ModuleNotFoundError`。本地训练（`train_ehms.py` 同样会被卡）需要把 [dataset/__init__.py:L1-L3](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/__init__.py#L1-L3) 三行 import 注释掉——这是仓库发布状态的已知问题，改完不影响其他模块。

**步骤 1：补依赖。** 数据链路 import 了一批 `requirements.txt` 未列出的包（以实际报错为准）：`pip install webdataset braceexpand pycocotools yacs lmdb trimesh matplotlib`（`webdata_loader.py` 还会经 `utils.graphics_utils` 拉起 pytorch3d，u1-l2 已装）。

**步骤 2：下载数据。** 按 [README.md:L120-L127](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L120-L127) 下载示例 tar，放到 `ehm_datasets/000000.tar`（目录名以 [configs/train.yaml:L130](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/train.yaml#L130) 为准，不是 README 写的 `ehms_datasets/`）。

**步骤 3：运行体检脚本。**

```python
# inspect_sample.py（示例代码，仓库根目录运行）
import torch
from utils.general_utils import ConfigDict, add_extra_cfgs
from dataset.webdata_loader import build_web_tracked_data

meta_cfg = ConfigDict(model_config_path='configs/train.yaml')
meta_cfg = add_extra_cfgs(meta_cfg)

ds = build_web_tracked_data(cfg_dataset=meta_cfg.DATASET, split='train')
sample = next(iter(ds))          # with_epoch(50_000) 是无限流，取 1 个即可

def show(name, v):
    if isinstance(v, torch.Tensor):
        print(f'{name:22s} tensor {tuple(v.shape)}  range[{v.min():.3f},{v.max():.3f}]')
    elif isinstance(v, dict):
        print(f'{name:22s} dict   {list(v.keys())}')
        for k, vv in v.items(): show(f'  {name}.{k}', vv)
    else:
        print(f'{name:22s} {type(v).__name__}  {v}')

for k in ['image', 'mask', 'ehm_image', 'target_image', 'target_mask',
          'smpl_kp2d', 'smpl_kp3d', 'dwpose_kp2d', 'dwpose_kp3d',
          'smpl_kp', 'render_valid', 'image_name', 'w2c_cam', 'c2w_cam']:
    show(k, sample[k])
show('smplx_coeffs', sample['smplx_coeffs'])
show('flame_coeffs', sample['flame_coeffs'])
print('render_cam_params keys:', list(sample['render_cam_params'].keys()))
```

**需要观察的现象与预期结果**（形状为源码推得，**待本地验证**）：

- `ehm_image` 为 `(3,256,256)`、数值范围约 [0,1]——与推理入口喂给 `ehm_model` 的 patch 同构。
- `smpl_kp` 为 True 或 False；为 False 时 `smpl_kp2d` 是全零 `(44,3)`、`dwpose_kp2d` 是有值的 `(134,3)` 且 xy∈[0,1]、第 8/11 行为 0。
- `smplx_coeffs.shape` 为 `(200,)`（10 维补零的效果）、`flame_coeffs.shape_params` 为 `(300,)`；`has_flame`/`has_body` 取值 0.1 或 1.1。
- 同一脚本连按两次取样本（把 `next(iter(ds))` 复制两行）：由于随机选人 + 随机增广，两次的 `ehm_image` 与 pose 参数不同——这就是流式数据集的「无限增广」性质。
- **对应关系标注**（对照 4.2.3 总表最后一列）：`ehm_image` ↔ 推理输入 patch；`smplx_coeffs`（global_pose/body_pose/双手/shape 等）↔ `outputs['body_param']`；`flame_coeffs` ↔ `outputs['flame_param']`；`smplx_coeffs['camera_RT_params']` 与 `w2c_cam` ↔ `outputs['pd_cam']`（GT 4×4 RT ↔ 预测 4×4 RT）。

#### 4.3.5 小练习与答案

**练习 1**：图像翻转后，SMPL `body_pose` 需要做哪两步变换？为什么不直接把关节顺序保持不变？

答案：先按置换表交换左右关节（左肩 ↔ 右肩……），再把每个关节轴角的第 1、2 分量取反。保持顺序不变会导致「图像里的左臂」对应「参数里的右臂」，姿态真值左右错位。

**练习 2**：2D 关键点用 `trans` 仿射同步、3D 关键点用 `rot_mat` 旋转同步，两者为何不会失配？

答案：两者来自同一次 `do_augmentation()` 掷出的同一个 `rot`：`trans` 内含该旋转（`gen_trans_from_patch_cv` 的 downdir/rightdir 先被 `rotate_2d` 旋转），`rot_mat` 直接按 \(-\text{rot}\) 构造；尺度不影响 3D 坐标（关键点 3D 是相机系真值），平移只影响 2D。因此任意增广组合下 2D/3D 真值与图像保持一致。

**练习 3**：把 `smpl_kp2d` 的坐标归一化从「/256（patch 坐标）」改成「/1024（渲染画布）」需要动哪些地方？

答案：这其实是训练侧的历史包袱——[pipeline.py:L289](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L289) 里预测值是 `/1024` 后与 `batch['smpl_kp2d']`（[0,1] 域）相减，两套尺度并不显式一致。若真要改归一化域，除了 [dataset_utils.py:L728](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/dataset_utils.py#L728) 的除数，还必须同步检查所有消费端（L283、L289 两条损失）的尺度约定——单改数据侧必然悄悄破坏损失量纲。这个练习的意义在于体会「数据格式是数据侧与训练侧的契约」。

## 5. 综合实践

**数据管线体检报告**：把 4.3.4 的脚本扩展成一个小工具 `audit_pipeline.py`（示例代码）：

1. 迭代 20 个样本（`itertools.islice(iter(ds), 20)`），统计：`smpl_kp=True` 的比例、`has_flame>0.5` 的比例、`render_valid=1` 的比例，体会「一真一零」双通道与软门控在真实数据上的分布。
2. 任选 1 个样本，把 `ehm_image` 反归一化（`×255` 转 uint8）与 `mask[0]×255` 分别用 `cv2.imwrite` 存成 PNG，肉眼确认：patch 里居中是完整人体、mask 抠出的是人体前景。
3. 对同一样本连续取 5 次，把 5 张 `ehm_image` 拼成一张对比图，观察随机增广（缩放/旋转/翻转/平移/极端裁剪）的差异；若某次出现「只剩上半身」的 patch，那就是 extreme crop 生效了。
4. 在报告末尾贴上你的「字段 ↔ 推理输出」对应表（4.2.3 总表的验证版），形状与上表不符的项标注差异。

预期：全部观察项均可本地完成；第 2、3 步的图像结果是判断「数据管线是否健康」最直观的证据（人体是否居中、mask 是否对齐、增广是否合理）。若样本无法取出，按 4.3.4 的步骤 0/1 逐项排查导入与依赖。

## 6. 本讲小结

- 数据管线是四级流水线：`wds.WebDataset`（tar 分片流式读取，`resampled=True` 无限重采样）→ `decode("rgb8")`（jpg→uint8 数组、`.pyd`→pickle 标注）→ `rename`（图像后缀归一为 `jpg`）→ `map(example_formatter)`。
- `build_web_tracked_data` 用归一化权重驱动 `wds.RandomMix` 逐样本按 \(P_i = w_i/\sum_j w_j\) 混采多数据集，再用 `with_epoch(50_000)` 定义伪 epoch、`shuffle(1000)` 做样本级打乱；`epoch_size` 配置实际是死参数。
- `example_formatter` 每次从一帧随机选一个人，把标注拼成 `smplx_coeffs/flame_coeffs`（shape 补零 200/300、有效性标志 `+0.1` 软门控）、组装 GT 相机 `w2c/c2w`，并产出 `ehm_image`（256 网络输入）等多分辨率图像族。
- 关键点双通道「一真一零」：SMPL 44 点与 DWPose 134 点只有一套有真值，`smpl_kp` 布尔开关供损失端选路；DWPose 2D 的两侧髋点被刻意抹除。
- `get_example` 的灵魂是「同一增广、三处同步」：图像用 2×3 仿射 `trans`，2D 关键点用同一 `trans` 逐点变换，3D 关键点用同角度 `rot_mat` 旋转，SMPL 参数做左右置换 + 轴角取反 + `rot_aa` 全局补偿；有 FLAME 标注时禁用翻转。
- 与推理侧的契约：`ehm_image` ↔ 256 patch 输入，`smplx_coeffs/flame_coeffs` ↔ `body_param/flame_param`，`camera_RT_params/w2c_cam` ↔ `pd_cam`；训练与推理共享 `gen_trans_from_patch_cv` 等同一套几何代码。

## 7. 下一步学习建议

- **下一讲 u5-l2（训练主循环）**：带着本讲的「字段消费审计表」去读 [models/pipeline/pipeline.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py) 的 `OurPipeline.__init__` 与 `run_fit`，看 `ehm_image` 如何流过 normalize → 256×192 裁剪 → backbone → head（L193-L201 与推理侧 `Ehm_Pipeline.forward` 逐行对应），以及 Lightning Fabric DDP 如何包住 DataLoader。
- **u5-l3（损失设计）**：本讲刻意留下了两个钩子——`has_flame/has_body` 软门控如何被 `HeadParameterLoss/BodyParameterLoss` 消费、`smpl_kp` 开关如何切换 SMPL/DWPose 关键点监督——都将在损失一讲收口。
- **延伸阅读**：`dataset/webdata_loader_render.py` 是同一管线的「渲染监督」变体（`decode_images`、`pt_decoder`、`expand_to_aspect_ratio` 的真正消费者），对比两个文件能看清作者如何复制改造数据管线，也是规划自己数据格式时的参考模板。
