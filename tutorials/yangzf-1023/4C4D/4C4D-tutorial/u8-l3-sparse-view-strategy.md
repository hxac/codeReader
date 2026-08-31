# u8-l3 稀疏视角策略全景与消融设计

## 1. 本讲目标

学完本讲，你应该能够：

1. 把「视角数量」当作一个可控的实验变量，说清它如何沿着 `--training_view` → `Scene` → `readColmapSceneInfo` 这条链影响训练集大小、测试集构成、场景半径 `cameras_extent` 与初始点云。
2. 说清初始点云规模的三个控制旋钮（`initial_num_pts` 下采样、`num_pts_ratio` 增广、`redundant_ratio` 时间冗余）分别作用于哪段源码，以及 MASt3R 稠密初始化在「输入侧」补偿了什么。
3. 说清 Neural Decaying Function（opacity decay）在「优化侧」补偿了什么，并能列出它开启后训练策略的四条联动变化。
4. 独立设计并执行一张规范的消融实验表：视角数（2/4/6）× 初始化来源（COLMAP 稀疏点 vs MASt3R 稠密点）× opacity_decay 开关，为每格写出具体 yaml 与命令行，并选定对比指标。

本讲是「系统视角」的收束：不再深挖单个函数的实现细节（那已在 u2、u3、u6 各讲完成），而是把散落在 README、`train.py`、`scene/`、`gaussian_renderer/` 里的稀疏视角策略串成一张可以动手验证的实验地图。

## 2. 前置知识

阅读本讲前，你应当已经掌握（对应前置讲义）：

- **u2-l2 / u2-l5**：`readColmapSceneInfo` 如何按 `camXX_YYYY.png` 命名把「相机 × 帧」展开成训练集；`training_view` 按相机（而非帧）划分 train/test；MASt3R 经 MAtCha 以 `--sfm_config posed --sfm_only` 重建点云，且 `points3D` 只从训练视角重建以避免信息泄漏。
- **u3-l5**：`create_from_pcd` 的初始化约定——空间尺度取近邻距离、不透明度统一 0.1、无时间戳时 `_t` 随机采样并受 `redundant_ratio` 外扩、时间尺度取 `duration/5`。
- **u6-l3 / u6-l4**：opacity decay 的三重门控与「空间可见 ∧ 时间可见」掩码；`[f_min, f_max] = [0.996, 0.998]` 窄带因子逐迭代连乘的复利式软删除；衰减网络由独立 `coef_optimizer` 与高斯优化器交替 step。

本讲新引入的术语：

- **消融实验（ablation study）**：固定其余条件，只翻转一个组件的开关，用指标差异归因该组件的贡献。
- **混淆变量（confound）**：随实验条件一起变化、会污染归因的量。本讲最重要的混淆变量是「测试集随视角数变化」。
- **输入侧 vs 优化侧**：MASt3R 稠密初始化改善的是喂给优化的**起点**；opacity decay 改善的是优化过程中的**梯度分配**。二者正交，因此适合做因子化消融。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [README.md](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md) | 稀疏视角动机、MASt3R 数据准备流程、`training_view` 约定 |
| [train.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py) | 脚本级参数（`training_view`/`num_pts`/`opacity_decay` 等）、yaml 合并、衰减开启后的训练联动 |
| [scene/\_\_init\_\_.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py) | `Scene` 把 `training_view`、`num_pts`、`redundant_ratio` 递给数据读取与初始化 |
| [scene/dataset_readers.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py) | train/test 划分、`num_pts_ratio` 增广、`num_pts` 下采样（random/fps） |
| [scene/gaussian_model.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py) | `create_from_pcd` 初始化、`opacity_decay`、`training_setup` 中的 `coef_optimizer` |
| [gaussian_renderer/\_\_init\_\_.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py) | 衰减在渲染循环中的唯一接入点 |
| [configs/dynerf/flame_steak.yaml](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml) | 官方场景配置，消融实验的基准 yaml |

## 4. 核心概念与源码讲解

### 4.1 training_view 划分：把「视角数」变成可控实验变量

#### 4.1.1 概念说明

4C4D 的核心卖点是「4 台便携相机」。对源码而言，「几台相机」不是场景属性，而是一个命令行字符串：`--training_view "1,10,13,20"`。视角数量 \( V \) 之所以是稀疏重建的第一超参数，是因为多视角几何的两条基本约束都随 \( V \) 退化：

- **三角化覆盖**：一个三维点至少要被两台相机同时观测才可三角化。相机越少、重叠角越小，可重建的点越少、深度不确定性越大。
- **外观-几何约束比**：渲染损失对每个像素都在约束颜色（外观），而几何（位置、尺度）只通过投影一致性被间接约束。\( V \) 越小，几何可用的独立约束越少，而外观约束不变——这正是 README 中「稀疏设定下几何学习远难于外观建模」的物理解释。

README 在数据准备一节明确了泄漏边界：`points3D` 永远只从训练（稀疏）视角重建，`images/cameras` 若需评估则从全部视角生成，见 [README.md:L92-L94](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L92-L94)。官方预处理数据用的正是 4 视角 `1,10,13,20`，见 [README.md:L98](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L98)。

#### 4.1.2 核心流程

视角选择的完整传导链：

```text
命令行 --training_view "1,10,13,20"
   │  train.py 参数注册（store_true/str）
   ▼
yaml 合并（recursive_merge 无条件覆盖同名键）
   ▼
train.py 主块：拆分为 ['cam01','cam10','cam13','cam20']
   ▼
Scene(..., training_view=...) ──► sceneLoadTypeCallbacks["Colmap"](..., training_cam=...)
   ▼
readColmapSceneInfo：按 image_name 前缀 camXX 划分 train / test
   ▼
getNerfppNorm(train_cam_infos) ──► cameras_extent（仅由训练相机决定！）
```

注意两个下游后果：

1. **测试集是「其余全部相机」**：\( V \) 变化时测试集也变化——这是消融设计里必须处理的混淆变量（见 4.1.4 与第 5 节）。
2. **`cameras_extent` 只由训练相机计算**：\( V \) 越少，场景归一化半径越小，进而通过 `spatial_lr_scale` 缩放位置学习率、通过 `percent_dense × extent` 缩放致密化尺寸分界。改 \( V \) 不只是「少了几张图」，还隐式改了学习率与致密化几何尺度。

#### 4.1.3 源码精读

**（1）参数注册与解析**。[train.py:L403-L409](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L403-L409) 注册 `--training_view`（默认 `"1,10,13,20"`）与 `--testing_view`（默认空串）；[train.py:L467-L471](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L467-L471) 把逗号串拆成零填充的 `camXX` 列表。这一步发生在 yaml 合并之后，因此 yaml 里也可以写 `training_view: "1,13"`，且优先级高于命令行默认值（u1-l4 讲过的「yaml > 命令行」约定）。

**（2）Scene 转发**。[scene/\_\_init\_\_.py:L52-L53](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L52-L53) 把 `training_view` 作为 `training_cam` 传给 Colmap 读取回调；[scene/\_\_init\_\_.py:L84](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L84) 随后从 `nerf_normalization["radius"]` 取出 `cameras_extent`。

**（3）真正的划分逻辑**。[scene/dataset_readers.py:L280-L289](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L280-L289)：`eval=True` 时，train 集取 `image_name.split('_')[0] in training_cam` 的全部帧；若给了 `testing_cam` 则 test 集精确等于该列表，否则取「不在训练集」的全部相机。`--testing_view` 这个分支就是消融实验固定测试集的官方入口。

**（4）场景半径仅由训练相机决定**。[scene/dataset_readers.py:L291](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L291) 调用 `getNerfppNorm(train_cam_infos)`；其实现 [scene/dataset_readers.py:L67-L88](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L67-L88) 取训练相机光心到均值中心的最大距离再乘 1.1 作为半径。

#### 4.1.4 代码实践

**实践目标**：在不碰 GPU 的前提下，量化「视角数 → 训练/测试帧数」的映射，并亲眼确认测试集随 \( V \) 漂移这一混淆变量。

**操作步骤**：把下面的脚本存为独立文件运行（示例代码，纯 Python，无需安装项目依赖）：

```python
# 示例代码：模拟 readColmapSceneInfo 的视角划分约定
def split(frames, training_view, testing_view=""):
    training_cam = [f"cam{str(int(v)).zfill(2)}" for v in sorted(training_view.split(','))]
    if testing_view:
        testing_cam = [f"cam{str(int(v)).zfill(2)}" for v in sorted(testing_view.split(','))]
        test = [f for f in frames if f.split('_')[0] in testing_cam]
    else:
        test = [f for f in frames if f.split('_')[0] not in training_cam]
    train = [f for f in frames if f.split('_')[0] in training_cam]
    return train, test

CAMS, FRAMES = 21, 300   # N3V 单场景约 21 台相机、官方取前 300 帧
frames = [f"cam{c:02d}_{i:04d}.png" for c in range(CAMS) for i in range(FRAMES)]
for v in ["1,13", "1,10,13,20", "1,3,10,13,17,20"]:
    tr, te = split(frames, v)
    print(f"V={len(v.split(','))}: train={len(tr)}帧  test={len(te)}帧({len(te)//FRAMES}台相机)")
```

**需要观察的现象**：三行输出分别是 `train=600/test=5700(19台)`、`train=1200/test=5100(17台)`、`train=1800/test=4500(15台)`。

**预期结果**：测试集大小随 \( V \) 单调变化——若直接比较三组的 test PSNR，「视角变多」和「考题变简单」两个因素混在一起。补救方式：所有格子统一加 `--testing_view 0,5`（cam00、cam05 不出现在任何训练视角组合中），让 12 格共享同一份考卷。

**待本地验证**：真实场景相机总数（N3V 各场景为 19~21 台不等）与帧数请以 `images/` 目录实际清点为准；上面脚本只是划分逻辑的复刻。

#### 4.1.5 小练习与答案

**练习 1**：把 `--training_view` 误删后（使用默认值 `"1,10,13,20"`），但你的数据集只有 cam00、cam01 两台相机，训练会发生什么？
**答案**：`training_cam = ['cam01','cam10','cam13','cam20']`，`cam00` 的任何一帧都不在 train 集；test 集则包含全部 cam00/cam01 帧（若给了 `--testing_view` 则为其指定者）。更隐蔽的是 `getNerfppNorm` 只剩 cam01 一台相机参与计算，`cameras_extent` 严重偏小，位置学习率与致密化尺寸分界随之失真。划分本身不会抛错（`assert` 只校验 `images.bin` 覆盖与目录文件数一致，见 [scene/dataset_readers.py:L277-L278](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L277-L278)），问题会以「训练不收敛/测试崩坏」的方式静默出现。

**练习 2**：为什么 `points3D` 必须只用训练视角重建，而 `images.bin/cameras.bin` 反而建议用全部视角生成？
**答案**：`points3D` 是训练的初始化点云，若混入测试视角的三维信息，等于把测试视角的几何答案提前泄露给优化器，指标虚高；`images/cameras` 只提供位姿（测试视角渲染时需要），不参与点云生成，因此用全部视角生成不会泄漏，反而是评估的前提（[README.md:L92-L94](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L92-L94)）。同时 `readColmapSceneInfo` 要求 `images.bin` 的位姿覆盖 `images/` 目录全部 png（数量断言），位姿缺失会直接 `AssertionError`。

**练习 3**：`--training_view 1,2,10` 经解析后得到什么列表？这个顺序有影响吗？
**答案**：`sorted(['1','2','10'])` 是**字符串**排序，得到 `['1','10','2']` → `['cam01','cam10','cam02']`。顺序无影响：下游只做 `in training_cam` 的成员判断（[scene/dataset_readers.py:L282](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L282)），且相机列表随后还会按 `image_name` 重排。

### 4.2 create_from_pcd 初始化：稀疏点云的规模与时间冗余控制

#### 4.2.1 概念说明

MASt3R 稠密初始化是 4C4D 的**输入侧**策略。README 说得很直白：「视角很少时 COLMAP 只能产出极其稀疏的点云，因此我们改用基于 MASt3R 的重建」，见 [README.md:L47-L49](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L47-L49)。它补偿的是：稀疏视角下传统 SfM 三角化失败导致**优化起点几乎没有几何**。起点差，后续每个策略都要花更多迭代去「从错误几何里爬出来」。

但点云不是越多越好：显存、光栅化耗时、以及初始点云里的噪声都会随规模上涨。于是源码提供了三个正交的规模/分布旋钮：

| 旋钮 | 作用位置 | 语义 |
| --- | --- | --- |
| `--initial_num_pts`（写入 `num_pts`） | `dataset_readers` 下采样分支 | 点数**上限**：超过才触发，random（有放回）或 fps（均匀） |
| `num_pts_ratio` | `dataset_readers` 增广分支 | 点数**放大**：额外撒 `(ratio−1)·N` 个随机点 |
| `redundant_ratio` | `gaussian_model.create_from_pcd` | **时间冗余**：无时间戳时 `_t` 的采样区间向时间轴两端外扩 |

#### 4.2.2 核心流程

初始点云从磁盘到 4D 高斯的旅程：

```text
points3D.bin/txt ──(首次懒转换 storePly)──► points3D.ply ──fetchPly──► BasicPointCloud
   │                                                            （注意：storePly 不写 time 字段 → time=None）
   ├─ num_pts_ratio > 1.001 ？──是──► 追加随机点（且丢掉 time 字段）
   ├─ 点数 > num_pts 且 num_pts > 0 ？──是──► random / fps 下采样（+按 time_duration 过滤时间）
   ▼
Scene ──► gaussians.create_from_pcd(pcd, cameras_extent, redundant_ratio)
   ├─ 空间尺度 = log(√distCUDA2(近邻距离))
   ├─ 不透明度 = inverse_sigmoid(0.1)（全体一致）
   ├─ time=None ？──是──► _t 在 [t0 − r·L/2, t1 + r·L/2] 均匀随机采样（L = 时长）
   └─ 时间尺度 = log(√(L/5))
```

关键链条：**标准 MASt3R 路线下 `time` 恒为 `None`**（`storePly` 的 dtype 不含 time，见 [scene/dataset_readers.py:L160-L175](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L160-L175)；`fetchPly` 相应返回 `None`，见 [scene/dataset_readers.py:L154-L158](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L154-L158)），所以 `redundant_ratio` 分支在默认管线里是**活的**，不是死代码。

#### 4.2.3 源码精读

**（1）`num_pts_ratio` 增广分支**。[scene/dataset_readers.py:L308-L322](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L308-L322)：当 `num_pts_ratio > 1.001` 时，在点云均值附近一个不对称的盒子（y 方向上界加 2.0）里追加 `(ratio−1)·N` 个随机点，颜色由 `SH2RGB` 随机生成。注意 L322 重建 `BasicPointCloud` 时**没有传 `time`**——增广分支会丢弃时间戳。

**（2）`num_pts` 下采样分支**。[scene/dataset_readers.py:L324-L344](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L324-L344)：`random` 用 `np.random.randint` **有放回**抽样（索引可重复，保密度），`fps` 用 `utils.general_utils.fps` 做无放回均匀采样（反密度、补稀疏区，需 CUDA）；随后统一用同一个 `mask` 过滤 `xyz/rgb/normals/time`，并在点云自带时间时按 `time_duration` 再过滤一遍。

**（3）`num_pts` 的恒真守卫陷阱**。[train.py:L448-L449](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L448-L449)：

```python
if args.initial_num_pts is not None:   # default=-1，永远为真
    args.num_pts = args.initial_num_pts
```

这段代码在 yaml 合并**之后**执行，而 `--initial_num_pts` 默认 `-1`（[train.py:L392](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L392)），`-1 is not None` 恒真。结论：**官方 yaml 里的 `num_pts: 300_000`（[configs/dynerf/flame_steak.yaml:L3](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml#L3)）实际会被打回 `-1`，下采样分支整段不触发，全量 MASt3R 稠密点直接进训练**。想让下采样生效，必须在命令行显式给 `--initial_num_pts N`（或在 yaml 里写 `initial_num_pts` 这个键而不是 `num_pts`）。同理 [train.py:L454-L455](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L454-L455) 的 `--res` 恒真守卫会把 yaml 的 `resolution: 2` 打回 1。这是 u1-l4「三个坑」在消融设计里的直接后果。

**（4）`create_from_pcd` 的时间维初始化**。[scene/gaussian_model.py:L413-L418](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L413-L418)：`pcd.time is None` 时，`_t` 在时长 \( L \) 的区间上均匀采样，且区间两端各外扩 \( \frac{r}{2}L \)（\( r \) 即 `redundant_ratio`）——给运动到时间边界外的高斯留出生存空间。空间侧尺度由近邻距离决定（[scene/gaussian_model.py:L422-L423](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L422-L423)），时间侧尺度固定为 \( \sqrt{L/5} \) 的对数（[scene/gaussian_model.py:L428-L429](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L428-L429)），不透明度统一 0.1（[scene/gaussian_model.py:L434](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L434)）。

**（5）`redundant_ratio` 的默认值链**。函数签名默认 0.2（[scene/gaussian_model.py:L406](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L406)），`Scene` 签名默认 0.2（[scene/\_\_init\_\_.py:L29](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L29)），但 `train.py` 的 `--redundant_ratio` 默认 0.0（[train.py:L421](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L421)）并显式传入（[train.py:L64-L66](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L64-L66)）——**实际生效默认是 0**，两个 0.2 只是未被走到的形参默认。

#### 4.2.4 代码实践

**实践目标**：用数值实验理解 random 下采样的「有放回」性质，并亲手复现 `num_pts` 恒真守卫如何杀死 yaml 配置。

**操作步骤**：运行下面两段示例代码（纯 numpy，CPU 即可）：

```python
# 示例代码 1：random 下采样的重复率
import numpy as np
N, M = 100_000, 30_000                      # 10 万点的 MASt3R 云降到 3 万
mask = np.random.randint(0, N, M)           # 复刻 dataset_readers.py L326 的 random 分支
print(f"抽样 {M} 次，互异索引 {len(np.unique(mask))} 个，重复率 {1 - len(np.unique(mask))/M:.1%}")
```

```python
# 示例代码 2：复现 train.py L448-L449 的守卫
initial_num_pts, num_pts = -1, 300_000      # yaml 合并后的状态（yaml 只写了 num_pts）
if initial_num_pts is not None:             # -1 is not None → 恒真
    num_pts = initial_num_pts
print(f"最终 num_pts = {num_pts}")          # → -1，下采样分支不触发
```

**需要观察的现象**：示例 1 的互异索引约 2.59 万个（重复率约 14%），理论值 \( N(1-(1-1/N)^M) \approx 100000 \times (1-e^{-0.3}) \)；示例 2 输出 `-1`。

**预期结果**：由此得出消融表的硬性规则——**控制初始点数必须用 `--initial_num_pts`**；同时提醒：random 有放回意味着「降到 M 点」实际保留少于 M 个不同位置，密度高的区域被重复抽中的概率更大（这正是 u8-l2 结论「random 保密度、fps 补稀疏」的数值来源）。

**待本地验证**：若你有 GPU 和真实点云，可把 `--downsample_method` 在 `random`/`fps` 间切换（[train.py:L423](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L423)），对比初始 `Number of points at initialisation` 打印与最终 total_points 曲线。

#### 4.2.5 小练习与答案

**练习 1**：MASt3R 与 COLMAP 两条初始化路线，各自「补偿」稀疏视角的什么缺陷？
**答案**：COLMAP 依赖经典特征匹配 + 三角化，4 视角下两两重叠小、三角化基线差，产出点云极稀（甚至对 2 视角近乎失效）；MASt3R 是学习型双视图重建模型，对弱纹理与小重叠更鲁棒，能在位姿已知（`--sfm_config posed --sfm_only`）的前提下重建出稠密点云。一句话：**MASt3R 补偿输入侧的几何信息不足，把优化起点从「几乎无几何」抬到「有稠密但含噪的几何」**。

**练习 2**：`num_pts_ratio` 与 `--initial_num_pts` 都能改变初始点数，为什么消融表里应优先用后者？
**答案**：`num_pts_ratio` 只能放大不能缩小，追加的是均值附近的**随机均匀**点、颜色随机、且会**丢弃 time 字段**（L322），引入了「随机噪声点」这一额外变量；`--initial_num_pts` 走下采样分支，保留原点云的分布（random/fps 二选一），语义是干净的「规模上限」。因子化消融要求每次只动一个语义清晰的旋钮。

**练习 3**：`redundant_ratio=0.2`、`time_duration=[0,10]` 时，`_t` 的采样区间是什么？
**答案**：以 \( L=10 \)、\( r=0.2 \) 代入 [scene/gaussian_model.py:L415](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L415) 的公式：区间长 \( (1+r)L = 12 \)，起点 \( 0 - rL/2 = -1 \)，即 \( [-1, 11] \) 均匀采样——比相机 timestamp 的定义域 [0,10) 向两端各外扩 1。

### 4.3 opacity decay 联动：几何-外观再平衡的系统开关

#### 4.3.1 概念说明

Neural Decaying Function 是 4C4D 的**优化侧**策略，补偿的是稀疏视角下「几何约束不足但外观约束照常」造成的解空间失衡：大量高斯即使位置/尺度错误，也能靠把不透明度和颜色调对来压低渲染损失（「用外观作弊」）。衰减网络依据高斯的时空状态输出窄带因子 \( f \in [f_{\min}, f_{\max}] = [0.996, 0.998] \)，乘进有效不透明度；单次乘法微不足道，但**逐迭代连乘**构成复利：

\[ o_k = o_0 \cdot f^k \]

从 \( o_0 = 0.1 \) 跌破剪枝线 0.005 所需的可见迭代数为

\[ k = \left\lceil \frac{\ln(0.005/0.1)}{\ln f} \right\rceil \approx 748 \ (f=0.996) \ \text{或} \ 1497 \ \ (f=0.998) \]

于是「能持续从渲染梯度中获得不透明度救援的高斯」存活，「只会外观作弊的高斯」被软删除——梯度被导向几何学习。它与 MASt3R 正交（一个管起点、一个管过程），这正是消融表把二者做因子化的理论依据。

#### 4.3.2 核心流程

衰减不是孤立函数，开启 `--opacity_decay` 后训练系统发生**五处联动**：

```text
--opacity_decay（store_true，default=True，只能靠 yaml 关掉）
   ├── (A) train.py L57-60  ：构造 Coefficient 网络（关闭则为 None，不建 coef_optimizer）
   ├── (B) train.py L473-474：densify_until_iter = iterations    → 致密化窗口拉满全程
   ├── (C) train.py L247-249：size_threshold 强制 None          → 关闭屏幕尺寸剪枝
   ├── (D) train.py L254-255：not args.opacity_decay 短路        → 关闭周期性 reset_opacity
   ├── (E) train.py L263-265：coef_optimizer 与高斯优化器交替 step
   ▼
渲染循环内（唯一激活点）：gaussian_renderer/__init__.py L64-75
   门控：args 非空 ∧ opacity_decay ∧ iteration > decay_from_iter(500)
   掩码：markVisible(空间) ∧ get_marginal_t > 0.05(时间)
   执行：opacity = pc.opacity_decay(f_min, f_max, mask=visibility)
```

(B)(C)(D) 三条联动的共同逻辑：clone/split 尺寸分界、屏幕尺寸剪枝、周期性不透明度重置，都是 4DGS 时代与衰减**竞争**的「高斯去留判据」——衰减开启后，去留应统一交给「复利衰减 + 梯度救援」这一套机制，旧判据全部让位（u6-l4 已详述，此处只列代码位置）。

#### 4.3.3 源码精读

**（1）衰减本体**。[scene/gaussian_model.py:L584-L620](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L584-L620)：`opacity_decay` 的模式族里，管线实际走 `mode='net'`（L602-L605）——因子由 `Coefficient(old_opacity, self.get_xyzt, self.get_scaling_xyzt)` 给出，再线性映射到 \( [f_{\min}, f_{\max}] \)；L616-L617 用 `torch.where` 只对 `mask` 为真的高斯衰减；L619 把结果经 `inverse_opacity_activation` 写回 `_opacity.data`（绕过 autograd 做状态持久化），L620 返回携梯度的 `opacity` 供光栅化使用——双通路解耦是联合优化的关键（u6-l2 已详述）。

**（2）渲染端唯一接入点**。[gaussian_renderer/\_\_init\_\_.py:L64-L75](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L64-L75)：三重门控通过后，取 `markVisible` 的空间可见性与 `get_marginal_t > 0.05` 的时间可见性之交（L66-L69），调用衰减并用返回值替换本次渲染的 `opacity`（L74-L75）。注意只有**训练循环里传 `args` 的那次 render**（[train.py:L138](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L138)）会走到这里；评估与轨迹渲染不传 `args`，衰减门控永假——推理读到的已是累积进 `_opacity` 的结果。

**（3）双优化器**。[scene/gaussian_model.py:L501-L505](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L501-L505)：`coefficient is not None` 时为 353 参数的小网络单独建 Adam（`coefficient_lr=1e-5`、`coefficient_weight_decay=1e-4`，见 [configs/dynerf/flame_steak.yaml:L43-L44](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml#L43-L44)），与会被致密化增删的高斯参数组按生命周期分家；[train.py:L263-L265](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L263-L265) 中二者交替 step，一次 `backward` 同时喂饱两者。

**（4）衰减参数注册**。[train.py:L412-L419](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L412-L419)：`--opacity_decay` 是 `action="store_true", default=True`——**命令行无法把它置 False**，唯一关闭途径是 yaml 里写 `opacity_decay: False`（`recursive_merge` 无条件 `setattr`，见 [train.py:L434-L443](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L434-L443)）。`f_max/f_min/decay_from_iter` 也在这一段注册。

#### 4.3.4 代码实践

**实践目标**：用复利公式量化「衰减多久杀死一个作弊高斯」，并验证衰减开关在命令行上不可关闭。

**操作步骤**：

1. 运行下面示例代码，画出不同因子与不同初始不透明度下的存活曲线。
2. 执行 `python train.py --help | grep -A2 opacity_decay`，确认它是无参数的开关型选项。

```python
# 示例代码：衰减复利的存活迭代数
import math
for f in (0.996, 0.998):
    for o0 in (0.1, 0.5):
        k = math.ceil(math.log(0.005 / o0) / math.log(f))
        print(f"f={f}, o0={o0}: {k} 次可见衰减后跌破 0.005")
```

**需要观察的现象**：四行输出约为 `748 / 1497`（o0=0.1）与 `1122 / 2245`（o0=0.5）；`--help` 输出中 `--opacity_decay` 之后没有任何取值占位符（对比 `--f_min  F_MIN`）。

**预期结果**：训练共 30000 迭代、衰减自 500 起启用，即使 \( f \) 取上界 0.998，一个从 0.1 起、无梯度救援的高斯在约 1500 次可见迭代后即被剪除——衰减的时间尺度远小于训练时长，这就是「软删除」有效的原因。`--help` 的输出则印证：**消融表的「关衰减」格子必须准备一份 `opacity_decay: False` 的 yaml 变体**，命令行做不到。

**待本地验证**：曲线只描述无救援情形；真实训练中高斯会同时接收不透明度梯度，实际存活时间取决于「救援速率 vs 衰减速率」的竞争，需用 TensorBoard 的 `opacity_histogram` 观察分布随迭代的移动。

#### 4.3.5 小练习与答案

**练习 1**：衰减开启后为什么必须把 `densify_until_iter` 拉满到 `iterations`（[train.py:L473-L474](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L473-L474)）？
**答案**：衰减是一个慢过程（数百上千次迭代才见效），它持续把「作弊高斯」推向剪枝线，剪掉的部位需要 clone/split 补充真实几何。若致密化在 15000 迭代就停止（yaml 默认），后半程只有衰减在删除、没有重建在补充，场景会出现空洞。删除与补充必须同窗口运行。

**练习 2**：`time_aware` 掩码把「空间可见 ∧ 时间可见」作为衰减对象，为什么这比「全体衰减」更公平？
**答案**：衰减的对抗方是渲染梯度——只有当前视角真正看到的高斯才拿得到救援。若对全体衰减，不可见高斯（哪怕几何完全正确）也在纯亏不透明度，等于惩罚「恰好这一帧不在场」；取交集后，每个高斯的衰减次数正比于其可见频率，衰减与梯度在**同一批高斯**上公平竞争（[gaussian_renderer/\_\_init\_\_.py:L64-L74](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L64-L74)）。u6-l3 已指出反直觉细节：`time_aware=False` 时 `mask=None`，衰减实际完全失效。

**练习 3**：消融表的「关衰减」格子里，要不要顺手加 `--reset_opacity`？
**答案**：看目的。若想让该格严格对齐 4DGS 基线（周期性把全体不透明度压到 0.01 再重新学习，逼场景重建高斯分布），应加 `--reset_opacity`——重置条件 `(iteration % opacity_reset_interval == 0 and not args.opacity_decay) and args.reset_opacity`（[train.py:L254-L256](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L254-L256)）在关衰减时由该开关放行。若只想单独度量「衰减」这一个因子、保持其余与开启格一致，则不加。无论哪种选择，都必须在实验记录里写明——这是一个容易污染归因的自由度。

## 5. 综合实践

**任务**：设计并（在算力允许时）执行一张 \( 3 \times 2 \times 2 = 12 \) 格的因子化消融表，回答三个问题：视角数坍缩到什么程度时质量开始崩坏？MASt3R 稠密初始化在各视角数下各救回多少？衰减的增益是否在视角越少时越大？

### 5.1 实验设计

**因子与水平**

| 因子 | 水平 | 载体 |
| --- | --- | --- |
| 视角数 \( V \) | 2 / 4 / 6 | `--training_view 1,13` / `1,10,13,20` / `1,3,10,13,17,20`（索引示例，具体选哪几台应按场景相机布阵控制重叠度，待本地验证） |
| 初始化来源 | COLMAP 稀疏点 / MASt3R 稠密点 | `sparse/0/points3D.*` 换用不同重建产物 |
| opacity_decay | 开 / 关 | yaml 里 `opacity_decay: True/False` |

**控制变量（所有格子严格一致）**

- 同一份基准 yaml（由 [configs/dynerf/flame_steak.yaml](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml) 复制两份，唯一差异是 `opacity_decay` 布尔值）；
- 统一 `--testing_view 0,5`：固定同一份考卷，消除「测试集随 \( V \) 漂移」的混淆（4.1.4）；
- 统一 `--seed 42`、相同 `iterations=30_000`、相同 `--res`；
- 每格独立 `--output_dir`（目录已存在会直接报错，[train.py:L463-L464](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L463-L464)）。

**yaml 变体（`configs/ablation/flame_steak_nodecay.yaml` 相对基准的唯一 diff）**

```yaml
# 示例配置：仅新增/修改这一行，其余字段与 flame_steak.yaml 完全一致
opacity_decay: False
```

注意 `recursive_merge` 的键名白名单机制（`assert hasattr(args, key)`）保证 `opacity_decay` 是合法 yaml 键——它对应 [train.py:L412](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L412) 注册的参数。

**数据准备（每个 \( V \) 一套，两条初始化路线共用位姿）**

按 README 流程（[README.md:L116-L137](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L116-L137)）：

```bash
# 1) 为当前 V 建立仅含训练视角的 MASt3R 工作目录
python scripts/n3v2colmap.py data/N3V/$SCENE --training_view $VIEWS
# 2) 生成覆盖全部视角的位姿（供 held-out 评估用）
python scripts/n3v2colmap.py data/N3V/$SCENE
# 3) 在 mast3r_${N_SPARSE} 上跑 MASt3R（位姿已知、仅重建点云）
cd ../MAtCha && conda activate matcha
python train.py -s ../4C4D/data/N3V/$SCENE/mast3r_${N_SPARSE} \
  -o ../4C4D/data/N3V/$SCENE/mast3r_${N_SPARSE} --sfm_config posed --sfm_only
# 4) 把重建点云复制进 sparse/0/
cp -r data/N3V/$SCENE/mast3r_${N_DENSE}/sparse data/N3V/$SCENE/
cp data/N3V/$SCENE/mast3r_${N_DENSE}/mast3r_sfm/sparse/0/points3D.* data/N3V/$SCENE/sparse/0/
```

- **MASt3R 格**：如上，`points3D` 用第 3 步产物。
- **COLMAP 格**：在同一 `mast3r_${N_SPARSE}` 目录（仅训练视角、位姿已知）上改跑 COLMAP mapper 产出稀疏 `points3D.*`（或走 `n3v2blender.py` 路线，其内部调用 colmap 全流程，见 u2-l5），位姿侧 `images/cameras` 保持不变。**每个 \( V \) 都要重新生成**——点云必须与训练视角一致，否则跨格泄漏。
- 已知不一致（u2-l5 标注，待确认）：README 第 3 步运行目录写 `mast3r_${N_SPARSE}` 而第 4 步复制自 `mast3r_${N_DENSE}`，实操时以你实际生成点云的目录为准。

**训练命令模板（12 格逐格替换三个变量）**

```bash
python train.py \
  --config configs/ablation/flame_steak${DECAY_SUFFIX}.yaml \   # '' 或 '_nodecay'
  --training_view ${VIEWS} \
  --testing_view 0,5 \
  --initial_num_pts ${NPTS} \      # -1 表示不限制（走全量点云）；需控制规模时给正值
  --res 1 \
  --seed 42 \
  --output_dir abl_v${V}_${INIT}_${DECAY}
```

### 5.2 完整消融表（读者先填「预测」列再跑实验）

| 格号 | V | 初始化 | decay | --training_view | config | --output_dir | 预测（先填） | 实测（后填） |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 2 | COLMAP | off | `1,13` | nodecay | `abl_v2_colmap_off` | | |
| 2 | 2 | COLMAP | on | `1,13` | 基准 | `abl_v2_colmap_on` | | |
| 3 | 2 | MASt3R | off | `1,13` | nodecay | `abl_v2_mast3r_off` | | |
| 4 | 2 | MASt3R | on | `1,13` | 基准 | `abl_v2_mast3r_on` | | |
| 5 | 4 | COLMAP | off | `1,10,13,20` | nodecay | `abl_v4_colmap_off` | | |
| 6 | 4 | COLMAP | on | `1,10,13,20` | 基准 | `abl_v4_colmap_on` | | |
| 7 | 4 | MASt3R | off | `1,10,13,20` | nodecay | `abl_v4_mast3r_off` | | |
| 8 | 4 | MASt3R | on | `1,10,13,20` | 基准 | `abl_v4_mast3r_on` | | |
| 9 | 6 | COLMAP | off | `1,3,10,13,17,20` | nodecay | `abl_v6_colmap_off` | | |
| 10 | 6 | COLMAP | on | `1,3,10,13,17,20` | 基准 | `abl_v6_colmap_on` | | |
| 11 | 6 | MASt3R | off | `1,3,10,13,17,20` | nodecay | `abl_v6_mast3r_off` | | |
| 12 | 6 | MASt3R | on | `1,3,10,13,17,20` | 基准 | `abl_v6_mast3r_on` | | |

**参考预测**（用于校准直觉，非标准答案）：格 1 应最差（2 视角 + 极稀点云 + 无再平衡）；格 4 与格 12 争最好；MASt3R 的边际贡献随 \( V \) 增大而递减（视角多了 COLMAP 自己也能三角化）；衰减的边际贡献随 \( V \) 减小而增大（几何约束越缺，再平衡越关键）——这正是「两策略正交且互补」的可检验形式。

### 5.3 指标与读取方式

| 指标 | 来源 | 说明 |
| --- | --- | --- |
| test PSNR / SSIM / L1 曲线 | TensorBoard `test/loss_viewpoint - psnr` 等（[train.py:L359-L362](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L359-L362)） | 训练期内抽稀相机子集上的曲线，看收敛形态 |
| 最终 PSNR / LPIPS / D-SSIM | `python render.py --config <同训练yaml> --validate --training_view $VIEWS --testing_view 0,5 --start_checkpoint <dir>/chkpnt30000.pth` | 全局口径的最终对比。注意 README 写的 `--test` 在源码中不存在，真实开关是 `--validate`（[render.py:L149](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L149)，u7-l1 已标注）；且必须带与训练同一份 `--config`（render.py 默认 `gaussian_dim=3`） |
| 最终高斯数 | TensorBoard `total_points`（[train.py:L311](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L311)） | 衰减开/关的最大行为差异之一：on 格全程致密化、点数持续增长；off 格 15000 迭代后冻结 |
| 不透明度分布 | TensorBoard `scene/opacity_histogram`（[train.py:L312](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L312)） | on 格分布应持续向高不透明度集中（作弊者被清除） |
| 训练耗时 | TensorBoard `iter_time` | 报告效率代价 |
| 定性：跨时间一致性 | `python render.py ... --traj arc --start_checkpoint ...` | arc 轨迹只走锚点弧段 ±10%，稀疏视角下最稳妥（u7-l2）；重点看运动区域拖尾与漂浮物 |

**结果读取规则**：每个因子的主效应 = 沿该因子方向成对求差再对另外两因子平均；交互效应看「差值的差」（例如衰减增益是否随 \( V \) 递减）。所有对比都基于统一测试集 `--testing_view 0,5`。

**算力预算**：12 格 × 30000 迭代开销不小。建议先做 7000 迭代的 pilot（`--test_iterations`/`--save_iterations` 默认含 7000，[train.py:L385-L386](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L385-L386)），确认趋势方向后再对重点格跑满。本节所有训练/评估命令均**待本地验证**。

## 6. 本讲小结

- **视角数是第一超参数**：`--training_view` 经 `Scene` 传入 `readColmapSceneInfo` 决定 train/test 划分；`cameras_extent` 只由训练相机计算，因此改 \( V \) 还隐式改变位置学习率与致密化尺寸分界；`--testing_view` 是固定测试集、消除混淆的官方入口。
- **初始点云有三个正交旋钮**：`--initial_num_pts`（下采样上限，random 有放回 / fps 均匀）、`num_pts_ratio`（随机增广、会丢时间戳）、`redundant_ratio`（`_t` 采样区间外扩）。警惕恒真守卫：yaml 的 `num_pts` 与 `resolution` 分别被 `--initial_num_pts`、`--res` 的默认值打回，控制初始点数必须显式给 `--initial_num_pts`。
- **标准管线下点云无时间戳**：`storePly` 不写 time 字段，`create_from_pcd` 因此总走 `_t` 随机采样分支，`redundant_ratio` 是活参数（经 train.py 实际默认 0）。
- **两个策略正交且互补**：MASt3R 稠密初始化补偿输入侧（优化起点几乎无几何），opacity decay 补偿优化侧（外观作弊淹没几何梯度）；衰减靠 \( [0.996, 0.998] \) 窄带因子的复利效应（约 750~1500 次可见迭代跌破 0.005 剪枝线）实现软删除。
- **衰减是系统开关**：开启后五处联动——构造 Coefficient、`densify_until_iter` 拉满、`size_threshold` 置 None、禁用 `reset_opacity`、`coef_optimizer` 交替 step；且 `--opacity_decay` 是 `default=True` 的 store_true，**只能靠 yaml 关闭**，消融的「关」格必须准备 yaml 变体。
- **规范消融的三个纪律**：统一 `--testing_view` 固定考卷、逐格独立输出目录、显式记录 `--reset_opacity` 等自由度的取舍。

## 7. 下一步学习建议

- **u8-l4（二次开发与调试工具箱）**：本讲消融表用到的 `sceneLoadTypeCallbacks` 注册、`lambda_*` 损失日志约定、`debug_from`/`detect_anomaly` 调试手段将在那里系统展开；若你想给消融表加第四个因子（如自定义损失），那是你的下一站。
- **算力不足时**：先做「源码阅读型消融」——只用 4.3.4 的复利公式与 4.2.4 的守卫复现脚本推演各格行为，再对照官方下载的预处理数据（[README.md:L98-L100](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L98-L100)）训练一次 4 视角基线，验证你对输出目录结构（`training_params.txt`、TensorBoard 曲线、`chkpnt_best.pth`）的理解。
- **延伸阅读**：若消融结果显示衰减增益随视角数变化的形态有趣，可回到 u6-l2 的衰减模式族（`const`/`exp_*`/`power_*`/`mlp`）把「衰减曲线形状」作为更细的实验因子，`opacity_decay` 的 `mode` 参数就是现成的接入点。
