# 初始化与持久化：create_from_pcd 与 PLY

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐行读懂 `create_from_pcd`：一批初始点云如何变成一组可训练的 `nn.Parameter`，位置、颜色来自哪里，空间尺度为什么由 `distCUDA2` 决定，时间中心 `_t` 在「有点云时间戳」与「没有时间戳」两种情况下分别怎么初始化。
2. 解释两个关键超参的数学含义：初始时间尺度取 `duration / 5` 意味着什么，`redundant_ratio` 如何把 `_t` 的采样区间向时间轴两端外扩，以及为什么静态点云需要这种「时间冗余」。
3. 画出 `save_ply` 写出的 68 列 PLY 字段布局，说出每列对应模型的哪个裸值属性，并指出这条持久化路径丢掉了什么（时间球谐块）。
4. 对比两条持久化路线：`chkpntN.pth`（`capture`/`restore`，无损、含优化器状态）与 `point_cloud.ply`（`save_ply`/`load_ply`，有损、仅供渲染），并理解 `load_ply` 前缀匹配逻辑里的三个坑。

本讲是单元 3 的收尾。u3-l1 建立了「裸值存储 + 读时激活」的心智模型，u3-l2 补上了第四维属性，u3-l3、u3-l4 讲清了这些属性如何被消费；本讲回答剩下的两个问题：**这些属性最初的数值从哪来**（初始化），以及**它们如何被写到磁盘、再读回来**（持久化）。

## 2. 前置知识

### 2.1 初始化：训练的「出生证明」

梯度下降只能修正已有数值，不能无中生有。训练开始前，每个高斯的每个属性都必须有一个初始值。初始化质量直接决定优化起点的好坏：

- **位置初始得太偏**，梯度要把大量迭代花在「把点挪对地方」上；
- **尺度初始得太大**，画面一片糊；**太小**，画面全是噪点；
- **时间中心初始得不对**（4D 特有问题），运动内容会一开始就「站错时间段」。

4C4D 的策略是分三类信息来源：**点云自带的**（位置、颜色、可能的时间戳）直接用；**几何统计量**（近邻距离 → 尺度）现算；**全局假设**（恒等旋转、不透明度 0.1、时间尺度 duration/5）拍一个经验值。本讲 4.1 会逐条对应到源码。

### 2.2 BasicPointCloud：初始化的输入容器

u2-l2 讲过，[scene/dataset_readers.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py) 产出、[utils/graphics_utils.py:17-21](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/graphics_utils.py#L17-L21) 定义的 `BasicPointCloud` 是一个四字段 NamedTuple：

```python
class BasicPointCloud(NamedTuple):
    points : np.array    # (N, 3) 空间坐标
    colors : np.array    # (N, 3) RGB，取值 [0,1]
    normals : np.array   # (N, 3) 法线（本项目中恒为全零，仅占位）
    time : np.array = None  # (N, 1) 可选时间戳，与 time_duration 同域
```

`time` 默认 `None`——这正是 `create_from_pcd` 里两条初始化分支的分叉点。注意 u2-l2 的结论会在此兑现：`num_pts_ratio > 1.001` 的增强分支会丢掉 `time`（[scene/dataset_readers.py:308-322](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L308-L322)），而 `num_pts` 下采样分支会保留它并按 `time_duration` 过滤（[scene/dataset_readers.py:324-344](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L324-L344)）。数据链路上游的一个选择，会直接决定本讲 4.1 走哪条分支。

### 2.3 两种「落盘」格式：.pth 与 .ply

- **`.pth`（torch.save 的 pickle）**：Python 对象的原生序列化，可以存任意嵌套结构——张量、字典、优化器状态。优点是无损，缺点是必须用 torch 读。
- **`.ply`（多边形文件格式）**：点云行业的通用文本/二进制格式，一张「表」：每行一个点，每列一个具名属性（`x`、`y`、`red`……）。任何点云软件都能打开，适合查看与交换，但表结构是扁平的，只能存每个高斯一份的数值。

4C4D 同时写两种：`chkpntN.pth` 给恢复训练用，`point_cloud.ply` 给渲染/查看用。4.3 与 4.4 会看到，这两条路线的「保真度」差异远大于名字暗示的差异。

### 2.4 术语回顾：裸值与激活

承接 u3-l1：`_scaling` 存的是 \(\log\sigma\)、`_opacity` 存的是 logit、`_rotation` 存未归一化四元数。**本讲的两个持久化函数存取的都是裸值**——记住这一点，看 PLY 字段表时才不会疑惑「为什么 scale 列是负数」。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [scene/gaussian_model.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py) | 主战场：`create_from_pcd`（L406-448）、`create_from_pth`（L450-477）、`save_ply`（L349-398）、`load_ply`（L281-347）、`capture`/`restore`（L98-191）、`get_cov_t`/`get_marginal_t`（L244-254） |
| [scene/dataset_readers.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py) | `fetchPly`/`storePly`（L145-175）：`points3D` 懒转换成 ply 并读回 `BasicPointCloud` 的桥 |
| [scene/__init__.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py) | 三个初始化分支的调度（L93-107，u2-l4 已讲）与 `Scene.save`（L109-112） |
| [train.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py) | 检查点的三个触发点：`--start_checkpoint` 恢复（L69-72）、best 检查点（L272-276）、常规保存（L278-281）；`redundant_ratio` 参数链（L66、L421） |
| [utils/sh_utils.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/sh_utils.py) | `RGB2SH`（L225-226）：初始颜色写进 DC 通道的换算 |
| [utils/general_utils.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py) | `inverse_sigmoid`（L19-20）：初始不透明度 0.1 的逆换算 |
| [simple-knn](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/simple-knn/setup.py) | `distCUDA2`：u1-l2 讲过的 CUDA 扩展，这里被用来算初始空间尺度 |

## 4. 核心概念与源码讲解

### 4.1 create_from_pcd：从点云到第一批 4D 高斯

#### 4.1.1 概念说明

`create_from_pcd` 是训练的起点（除非走 `loaded_pth`/`loaded_iter` 分支，见 u2-l4）：它把一份 `BasicPointCloud` 翻译成 `GaussianModel` 的九组 `nn.Parameter`。它要回答的其实是九个问题：每个高斯出生时的位置、颜色、高阶颜色、空间尺度、空间旋转、不透明度、时间中心、时间尺度、时间旋转分别是什么。

答案分三类：

| 信息类别 | 属性 | 来源 |
| --- | --- | --- |
| 点云自带 | `_xyz`、DC 颜色、（可选）`_t` | `pcd.points`、`pcd.colors`、`pcd.time` |
| 几何统计 | `_scaling` | `distCUDA2` 最近邻距离 |
| 全局假设 | `_rotation`、`_opacity`、`_scaling_t`、（无时间信息时的）`_t`、`_rotation_r` | 恒等四元数、0.1、duration/5、均匀采样、恒等四元数 |

「无时间信息时随机采样 `_t`」值得单独强调：4C4D 用 MASt3R/COLMAP 重建的静态点云（u2-l5）通常没有时间戳——**一个静态点代表的是「在所有时刻都存在的内容」**，但一个 4D 高斯必须有确定的时间中心，于是只能给每个点随机发一个时间，再靠训练去修正。`redundant_ratio` 就是为这条路径服务的。

#### 4.1.2 核心流程

```text
create_from_pcd(pcd, spatial_lr_scale, redundant_ratio)
│
├─ 1. 空间与颜色：points → cuda 张量；colors → RGB2SH → 填入 SH 的 DC 通道
│      features = zeros(N, 3, get_max_sh_channels)；features[:, :3, 0] = 颜色
│
├─ 2. 时间中心 _t（仅 gaussian_dim == 4）
│      ├─ pcd.time is None → 均匀采样，区间比 [t0, t1] 两端各外扩 rD/2（redundant_ratio）
│      └─ 否则 → 直接用点云时间戳
│
├─ 3. 空间尺度：dist2 = clamp_min(distCUDA2(points), 1e-7)
│      _scaling = log(sqrt(dist2))，复制 3 份（log 空间裸值）
│
├─ 4. 空间旋转：恒等四元数 (1,0,0,0)
│
├─ 5. 时间尺度（仅 4D）：dist_t = D/5（D = time_duration 长度，常数）
│      _scaling_t = log(sqrt(dist_t))；rot_4d 时另设 _rotation_r = 恒等四元数
│
├─ 6. 不透明度：inverse_sigmoid(0.1)，全点相同
│
└─ 7. 全部包装为 nn.Parameter(requires_grad=True)，max_radii2D 清零
```

涉及的两条数学换算：

初始不透明度走 `inverse_sigmoid`（即 logit 函数的逆）：

\[ \sigma^{-1}(x) = \log\frac{x}{1-x} \quad\Rightarrow\quad \sigma^{-1}(0.1) = \log\frac{0.1}{0.9} \approx -2.197 \]

存的是裸值 \(-2.197\)，读时 `get_opacity` 过 sigmoid 还原为 0.1（u3-l1 的模式）。

初始时间中心（无时间戳分支）的采样公式，设 \(D = t_1 - t_0\)（入口默认 10）、\(r\) 为 `redundant_ratio`、\(u \sim U[0,1)\)：

\[ t = \big(u\,(1+r) - \tfrac{r}{2}\big)\cdot D + t_0 \]

把 \(u\) 的两个端点代进去，得到采样区间：

\[ t \in \big[\,t_0 - \tfrac{rD}{2},\;\; t_1 + \tfrac{rD}{2}\,\big) \]

即**向时间轴两端对称外扩** \(rD/2\)。默认 `time_duration=[0,10]`、`r=0.2` 时即 \([-1, 11)\)。

#### 4.1.3 源码精读

**入口与点云装配**（[scene/gaussian_model.py:406-412](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L406-L412)）：先把 `spatial_lr_scale`（即 u2-l4 的 `cameras_extent`）记到自身，供 `training_setup` 缩放位置学习率；随后把点搬上 GPU、颜色经 `RGB2SH`（`(rgb-0.5)/C0`，[utils/sh_utils.py:225-226](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/sh_utils.py#L225-L226)）压进 SH 系数域，写入零初始化张量的 DC 通道。`features[:, 3:, 1:] = 0.0` 这行是继承自 3DGS 的**空切片**——`features` 第 1 维长度是 3，`3:` 切出来是空的，什么都不写；不影响正确性（张量本来就是零），但读源码时别被它骗。

**时间中心的两条分支**（[scene/gaussian_model.py:413-418](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L413-L418)）：`pcd.time is None` 时按 4.1.2 的公式均匀采样并打印提示；否则 `torch.from_numpy(pcd.time)` 直接采用点云时间戳——此时 `redundant_ratio` 完全不参与。注意形状约定：两条路径产出的都是 `(N, 1)`。

**空间尺度由近邻距离决定**（[scene/gaussian_model.py:422-425](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L422-L425)）：`distCUDA2`（u1-l2 讲过的 simple-knn 扩展）对每个点返回**到最近邻的平方距离**，下限钳到 `1e-7` 防止孤立点取 log 后爆掉；`torch.log(torch.sqrt(dist2))` 存成 log 空间裸值并复制 3 份给 xyz 三轴。直觉：**点密的地方高斯小、点疏的地方高斯大**——初始椭球大小自动匹配局部采样密度。旋转则统一取恒等四元数 \((w{=}1, x{=}0, y{=}0, z{=}0)\)。

**时间尺度取 duration/5**（[scene/gaussian_model.py:426-432](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L426-L432)）：`dist_t` 是一个与点无关的常数 \((t_1-t_0)/5\)，`scales_t = log(sqrt(dist_t))` 同样是 log 空间。激活后的时间尺度 \(s_t = \sqrt{D/5}\)（默认 \(D{=}10\) 时 \(s_t \approx 1.414\)）。它落到 u3-l3 讲过的 `get_cov_t`（[scene/gaussian_model.py:244-250](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L244-L250)）时的语义随 `rot_4d` 变化：

- `rot_4d=True`：时间方差 \(\mathrm{cov}_t = s_t^2 = D/5 = 2\)，即 \(\sigma_t \approx 1.41\)；
- `rot_4d=False`：`get_cov_t` 直接返回 \(s_t\) 本身，作为 `get_marginal_t`（[scene/gaussian_model.py:252-254](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L252-L254)）里 \(\exp(-\Delta t^2 / 2\sigma)\) 的 \(\sigma\) 参数。

这正是 u3-l3 提醒过的「`_scaling_t` 语义差一个平方」陷阱在初始化处的体现。数量级直觉：以 `rot_4d=True` 为例，初始高斯的时间衰减半高全宽约 \(2.355\,\sigma_t \approx 3.3\)，约占 10 长时间轴的 **三分之一**——出生时每个高斯在时间上「铺得比较长」，运动细节靠后续训练把它收窄。为什么是 `/5` 而不是别的？这是继承自 4DGS 的经验超参；[第 427 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L427)留有一行被注释掉的 `dist_t = ... distCUDA2(fused_times...)`，说明作者试过「按时间戳近邻距离现算」的数据驱动方案，最终弃用、回到常数。

**不透明度与参数包装**（[scene/gaussian_model.py:434-448](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L434-L448)）：`inverse_sigmoid(0.1)` 给所有点同一个初始不透明度（全 0 太「透明」会导致梯度消失，太大则糊成一片，0.1 是 3DGS 一脉相承的折中）。最后六个 3D 属性 + （4D 时）`_t`、`_scaling_t`、（rot_4d 时）`_rotation_r` 全部包成 `nn.Parameter`，`max_radii2D` 清零。注意 `_features_dc`/`_features_rest` 在包装时做了 `transpose(1, 2)`：内部存储是 `(N, C, 3)`（通道在前），与 4.3 的 PLY 布局（颜色通道在前）差一个转置。

**`redundant_ratio` 的参数链**：这个参数在三层各有默认值，且**互相覆盖**：

| 位置 | 默认值 | 说明 |
| --- | --- | --- |
| [train.py:421](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L421) | `0.0` | `--redundant_ratio` 命令行参数 |
| [scene/__init__.py:29-30](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L29-L30) | `0.2` | `Scene.__init__` 形参默认，但 train.py 总会显式传入 |
| [scene/gaussian_model.py:406](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L406) | `0.2` | `create_from_pcd` 形参默认，但 Scene 总会显式传入 |

经 train.py 入口时链条是 `args.redundant_ratio`（[train.py:66](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L66)）→ `Scene` → `create_from_pcd`，而官方 yaml（`configs/dynerf/*.yaml`）均未设置该键，所以**实际生效的默认值是 0.0**，采样区间恰好是 \([t_0, t_1)\)、不外扩。想在实验里启用时间冗余，必须在命令行或 yaml 里显式给值——这又是一个 u1-l4「优先级链条」主题的实例。

#### 4.1.4 代码实践

**实践目标**：亲手对比「带时间戳」与「不带时间戳」两种点云的 `_t` 初始化分布，并验证 `redundant_ratio` 的外扩公式。

**操作步骤**（示例代码，需 GPU 环境；本讲写作环境无 GPU，**待本地验证**）：

```python
# 示例代码：save as check_init_t.py，在仓库根目录运行
# 前置：已按 u1-l2 编译四个 CUDA 子包，GPU 可用
import numpy as np, torch
from scene.gaussian_model import GaussianModel
from utils.graphics_utils import BasicPointCloud

N = 20000
xyz   = np.random.randn(N, 3).astype(np.float32) * 0.5
color = np.random.rand(N, 3).astype(np.float32)

pcd_no_time = BasicPointCloud(points=xyz, colors=color,
                              normals=np.zeros((N, 3)), time=None)
pcd_with_time = BasicPointCloud(points=xyz, colors=color,
                                normals=np.zeros((N, 3)),
                                time=np.random.rand(N, 1).astype(np.float32) * 10)

for pcd, tag in ((pcd_no_time, "无 time"), (pcd_with_time, "有 time")):
    g = GaussianModel(sh_degree=3, gaussian_dim=4,
                      time_duration=[0, 10], rot_4d=True, sh_degree_t=2)
    g.create_from_pcd(pcd, spatial_lr_scale=1.0, redundant_ratio=0.2)
    t = g._t.detach()
    print(f"[{tag}] _t shape={tuple(t.shape)} "
          f"min={t.min():.3f} max={t.max():.3f} mean={t.mean():.3f}")
    print(f"[{tag}] _scaling_t 唯一值 = {g._scaling_t.detach().unique().item():.4f}"
          f"  (理论 log(sqrt(10/5)) = {np.log(np.sqrt(2)):.4f})")
```

**需要观察的现象与预期结果**：

1. 「无 time」分支：`_t` 均匀铺在 `[-1, 11)` 上（`r=0.2, D=10` 外扩 `rD/2 = 1`），终端打印 `No time information provided, using random time values with redundant ratio 0.2.`；把 `redundant_ratio` 改成 `0.0` 后区间收窄为 `[0, 10)`。
2. 「有 time」分支：`_t` 的分布与传入的 `pcd.time` 完全一致（可断言 `torch.equal`），不打印随机采样提示——`redundant_ratio` 对它无效。
3. `_scaling_t` 是全相同的常数 `log(sqrt(2)) ≈ 0.3466`，验证 duration/5 的初始化。
4. 两个模型的 `Number of points at initialisation` 都是 20000。

若把 `rot_4d=False` 再跑一遍，`_scaling_t` 数值不变，但按 4.1.3 的分析它消费时的语义差一个平方——可对照 u3-l3 的实践复核。

#### 4.1.5 小练习与答案

**练习 1**：`inverse_sigmoid(0.1)` 等于多少？为什么存这个值而不是直接存 0.1？

**答案**：\(\log(0.1/0.9) = \log(1/9) \approx -2.197\)。因为 `_opacity` 是裸值，`get_opacity` 读取时要过 `sigmoid`；裸值必须存 \(\sigma^{-1}(0.1)\) 才能还原出 0.1。直接存 0.1 的话读出来是 \(\sigma(0.1) \approx 0.525\)，初始画面会过亮过糊。

**练习 2**：默认 `time_duration=[0,10]`、`redundant_ratio=0.2` 时，无时间戳点云的 `_t` 采样区间是什么？如果走 train.py 入口且不改任何配置，实际区间又是什么？

**答案**：公式给 \([t_0 - rD/2,\, t_1 + rD/2) = [-1, 11)\)。但 train.py 的 `--redundant_ratio` 默认 0.0 且 yaml 不覆盖（4.1.3 的参数链表），所以实际是 \([0, 10)\)，无外扩。

**练习 3**：为什么「点云带时间戳」时不需要 `redundant_ratio`，而「不带」时需要？

**答案**：带时间戳时每个点的时间归属是已知的，直接采用即可。不带时只能随机撒时间：一个静态点代表全时段存在的内容，均匀撒满并略向外扩，可以保证时间轴两端（边界处 `get_marginal_t` 衰减最厉害的区域）也有足够的时间中心覆盖，训练再逐步特化——「冗余」指的是时间上的重叠覆盖。另注意 u2-l2 的结论：`num_pts_ratio>1.001` 增强分支会丢掉 `time`，让本来「有」时间戳的点云退回随机采样分支。

### 4.2 create_from_pth：从预训练 4DGS 权重热启动

#### 4.2.1 概念说明

除了从点云白手起家，`Scene` 还支持从一份**预训练好的 4DGS 权重字典**热启动（u2-l4 的三分支里优先级最高：`loaded_pth` > `loaded_iter` > `create_from_pcd`）。典型用途：拿一个已收敛的 4DGS 模型做 4C4D 的初始化，省去从随机时间起步的探索。

#### 4.2.2 核心流程

读入 `.pth` 里的字典 → 九组张量按各自的优化目标形态做最小变换（transpose、requires_grad）→ 包装为 `nn.Parameter`。与 `create_from_pcd` 最大的差别是：**没有任何随机性与启发式**——尺度、旋转、不透明度、时间参数全部来自预训练值，不做 `distCUDA2`、不撒随机时间、不设 0.1 不透明度。

#### 4.2.3 源码精读

[scene/gaussian_model.py:450-477](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L450-L477)：第一行 `assert self.gaussian_dim == 4 and self.rot_4d` 限定只接受「4D 且 rot_4d」的模型——因为函数体无条件加载 `rotation_r`/`scaling_t`（[第 462-463 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L462-L463)），一个 3D 模型没有对应属性可放。`_features_dc`/`_features_rest` 同样做 `(1,2)` 转置以匹配内部 `(N, C, 3)` 布局；`max_radii2D` 依旧清零（屏幕尺寸统计不迁移）。触发入口是 `ModelParams.loaded_pth`（[arguments/__init__.py:59](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L59)，默认空串）经 [scene/__init__.py:93-95](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L93-L95) 调用，`spatial_lr_scale` 仍取当前场景的 `cameras_extent`——**几何来自旧模型，学习率尺度来自新场景**。

#### 4.2.4 代码实践

**实践目标**：确认 `create_from_pth` 所需的字典键集合。

**操作步骤**：对照 [scene/gaussian_model.py:453-465](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L453-L465)，手工列出全部 9 个键：`xyz`、`features_dc`、`features_rest`、`t`、`scaling`、`rotation`、`scaling_t`、`rotation_r`、`opacity`。若手头有一份 4DGS 训练产物，先 `python -c "import torch; print(list(torch.load('xx.pth', map_location='cpu').keys()))"` 核对键名与形状是否齐备。

**预期结果**：键齐全且形状匹配（如 `features_*` 为 `(N, 3, C)`）即可热启动；缺任何一个键都会 `KeyError`。**待本地验证**（本讲环境无权重文件）。

#### 4.2.5 小练习与答案

**练习**：`create_from_pth` 里为什么 `max_radii2D` 清零而不是沿用预训练值？

**答案**：`max_radii2D` 记录的是每个高斯在**训练渲染中达到过的最大屏幕半径**（像素域），既依赖相机分辨率也依赖致密化/剪枝历史（`densification_postfix` 每次会把它整体清零，[scene/gaussian_model.py:674](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L674)）。新场景的相机集合已变，旧值没有意义，清零后由新训练的前几百次迭代重新统计，供 `densify_and_prune` 的 `big_points_vs` 判据使用。

### 4.3 save_ply / load_ply：表格化持久化与它的三个坑

#### 4.3.1 概念说明

`save_ply` 把模型导出成一张 PLY「表」：一行一个高斯，一列一个属性，全部 `f4`（float32）。它服务于**查看与渲染**（外部点云软件可直接打开），而不服务于恢复训练——PLY 里没有优化器状态、没有学习率尺度、没有激活球谐阶数。理解它的关键是把「列名 ↔ 模型裸值属性」的映射背下来，并清楚这条路径**丢掉了什么**。

#### 4.3.2 核心流程

```text
save_ply(path)
├─ 各属性 detach().cpu().numpy() 取裸值
├─ f_rest 截断：只保留 active_sh_degree 对应的前 (d+1)²-1 个通道/颜色
├─ np.concatenate 拼表：xyz | 全零 normals | f_dc | f_rest | opacity | scale | rot | t | scale_t | rot_r
└─ PlyElement.describe(..., 'vertex') 写文件

load_ply(path)（对称的读回）
├─ 按列名取 x/y/z、opacity、f_dc_0..2
├─ 按前缀过滤 + int 排序：f_rest_* / scale_* / rot* / t* / rot_r* / scale_t*
├─ f_rest 断言数量 == 3*(max_sh_degree+1)²-3，reshape 成 (N, 3, (d+1)²-1)
├─ gaussian_dim==4 时把 t / scale_t / rot_r 包成 _t / _scaling_t / _rotation_r
└─ active_sh_degree 直接置为 max_sh_degree
```

#### 4.3.3 源码精读

**写出的字段布局**（[scene/gaussian_model.py:369-390](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L369-L390)）。以训练结束时的常态 `active_sh_degree=3`、`gaussian_dim=4`、`rot_4d=True` 为例，共 **68 列**：

| 列名 | 列数 | 内容（注意：全是裸值） |
| --- | --- | --- |
| `x y z` | 3 | `_xyz` |
| `nx ny nz` | 3 | 恒为 0（占位，兼容 3DGS 查看器习惯） |
| `f_dc_0..2` | 3 | `_features_dc` 转置展平（SH 域颜色） |
| `f_rest_0..44` | 45 | `_features_rest` **前 15 通道** × 3 色 |
| `opacity` | 1 | `_opacity`（logit，负数） |
| `scale_0..2` | 3 | `_scaling`（log σ，负数） |
| `rot_0..3` | 4 | `_rotation`（未归一化四元数） |
| `t` | 1 | `_t` |
| `scale_t` | 1 | `_scaling_t` |
| `rot_r_0..3` | 4 | `_rotation_r` |

`save_ply` 写死追加 `t`/`scale_t`/`rot_r` 列（[第 358-360、386-389 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L358-L360)），因此它实际上只能服务 `gaussian_dim=4` 的模型——3D 模型的 `_t` 是空张量，拼表时形状对不上。

**坑一：f_rest 截断，时间球谐块丢失**（[scene/gaussian_model.py:362-367](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L362-L367)）：`sh_channels = (active_sh_degree+1)²`，`feature_rest = self._features_rest[:, :sh_channels-1, :]` 只取**前 15 个通道**。而 u3-l4 讲过，4D 且 `sh_degree_t=2` 时 `_features_rest` 有 47 个通道，其中第 16-47 通道是 \(\cos(2\pi\Delta t/T)\)、\(\cos(4\pi\Delta t/T)\) 调制的时间球谐块。**它们整块不落盘**——PLY 只保存与时间无关的基础外观。所以由 PLY 恢复的模型渲染动态颜色变化（如火焰的忽明忽暗）会退化。

**坑二：`rot` 前缀吞掉 `rot_r`**（[scene/gaussian_model.py:309-313](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L309-L313) vs [第 315-319 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L315-L319)）：读回时 `rot_names` 用 `p.name.startswith("rot")` 过滤——而 `"rot_r_0".startswith("rot")` 同样为真。于是 `rot_names` 会命中 **8 个**字段（`rot_0..3` + `rot_r_0..3`），稳定排序按尾号 int 排成 `[rot_0, rot_r_0, rot_1, rot_r_1, ...]`，`rots` 拼出 `(N, 8)` 的张量喂给 `_rotation`（应为 `(N, 4)`）。前缀 `"rot_r"` 的第二次过滤本意是把两类分开，但第一次过滤已经污染了。

**坑三：单字段 `t` / `scale_t` 的排序键会抛异常**（[scene/gaussian_model.py:320-321](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L320-L321)、[第 326-327 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L326-L327)）：`t_names` 过滤 `startswith("t")` 只会命中一列 `'t'`，随后 `sorted(t_names, key=lambda x: int(x.split('_')[-1]))` 要对 `'t'` 求 `int('t')`——Python 的 `sorted` 对单元素列表同样会调用 key 函数，这里抛 `ValueError: invalid literal for int() with base 10: 't'`。`scale_t` 同理（`'scale_t'.split('_')[-1]` 也是 `'t'`），只是执行顺序上 `t` 先炸。

> **诚实性说明**：坑二、坑三是本讲写作时对 [第 303-330 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L303-L330)逐行静态推演的结论（写作环境无法执行 Python/无 GPU，**待本地验证**）。推演含义是：**用本仓库 `save_ply` 写出的 4D PLY，无法被本仓库的 `load_ply` 读回**——`load_ply` 的这套前缀逻辑更像是为「时间列带数字后缀（如 `t_0`）」的上游 4DGS 布局写的。这也解释了为什么 4C4D 的推理入口 render.py 走的是 `.pth` 的 `restore`（u7-l1）而不是 `load_ply`；`load_ply` 的触发路径只剩 [scene/__init__.py:97-102](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L97-L102)（`load_iteration` 分支）。请用 4.3.4 的脚本实测确认。

**读回侧的其他细节**：`assert len(extra_f_names) == 3*(max_sh_degree+1)²-3`（[第 296 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L296)）与 reshape（[第 301 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L301)）配合，把 45 列折成 `(N, 3, 15)` 再转置回 `(N, 15, 3)`——所以 PLY 往返后 `_features_rest` 是 `(N, 15, 3)`，而 4D 训练时是 `(N, 47, 3)`，形状本身就印证了坑一。`active_sh_degree` 被直接置满（[第 347 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L347)），但 `active_sh_degree_t` 从不被恢复（保持构造时的 0）。

**顺带认识 `fetchPly`/`storePly`**（[scene/dataset_readers.py:145-175](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L145-L175)）：这组函数处理的是**输入侧**点云（`points3D` 懒转换，见 u2-l2），与 `save_ply`/`load_ply`（**模型侧**）是两套独立代码。注意 `fetchPly` 恰好演示了「正确」的按名读取：它直接按列名 `x/y/z`、`red/green/blue` 取值，`time` 用 `'time' in vertices` 判存在——没有前缀匹配的坑。输入侧的 time 列名是 `time`，模型侧写的是 `t`，两套命名并不通用。

#### 4.3.4 代码实践

**实践目标**：在**纯 CPU、不装 torch** 的环境下，用字段名列表模拟 `save_ply → load_ply` 的往返，实测 4.3.3 的坑二与坑三。

**操作步骤**（示例代码，任何有 python3 的机器可跑）：

```python
# 示例代码：save as ply_roundtrip_sim.py
# 1) 按 save_ply 的 construct_list_of_attributes 复刻字段顺序（active_sh_degree=3）
fields  = ['x', 'y', 'z', 'nx', 'ny', 'nz']
fields += ['f_dc_{}'.format(i) for i in range(3)]
fields += ['f_rest_{}'.format(i) for i in range(45)]      # 3*((3+1)^2-1)
fields += ['opacity'] + ['scale_{}'.format(i) for i in range(3)]
fields += ['rot_{}'.format(i) for i in range(4)]
fields += ['t', 'scale_t'] + ['rot_r_{}'.format(i) for i in range(4)]
print('save_ply 字段总数 =', len(fields), '（预期 68）')

# 2) 逐行复刻 load_ply 第 303-330 行的前缀过滤与排序
rot_names = [f for f in fields if f.startswith("rot")]
try:
    rot_names = sorted(rot_names, key=lambda x: int(x.split('_')[-1]))
    print("rots 列数 =", len(rot_names), "（_rotation 期望 4 列）->", rot_names)
except ValueError as e:
    print("rot 排序失败:", e)

t_names = [f for f in fields if f.startswith("t")]
print("t_names =", t_names)
try:
    t_names = sorted(t_names, key=lambda x: int(x.split('_')[-1]))
    print("t 排序 OK ->", t_names)
except ValueError as e:
    print("t 排序抛 ValueError ->", e)
```

**需要观察的现象**：第 2 步第一段打印 `rots 列数 = 8`，且 `rot` 与 `rot_r` 按尾号交错；第二段在 `t` 排序处抛 `ValueError`。

**预期结果**：与 4.3.3 的静态推演一致（`_rotation` 变 `(N, 8)`、`t` 排序崩溃），即本仓库的 4D PLY 无法自往返。修复方向（供对照）：把 L309 的过滤条件改成 `startswith("rot_")`、给 `t`/`scale_t` 的排序键加兜底（或直接按名取单列）。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：不看上文，写出 `save_ply` 字段表中「t、scale_t、rot_r_0..3」之前的三组字段名。

**答案**：`opacity`（1 列）、`scale_0..2`（3 列，log 空间）、`rot_0..3`（4 列，未归一化四元数）；再往前是 6 列坐标/法线与 48 列球谐（3 DC + 45 rest）。

**练习 2**：为什么 PLY 里 `scale` 列多为负数、`opacity` 列约为 -2.2 附近？

**答案**：存的是裸值。尺度列存 \(\log\sigma\)，初始 \(\sigma\approx\) 近邻距离的量级（通常小于 1），log 后为负；opacity 列存 \(\sigma^{-1}(0.1)\approx-2.197\)，训练中在附近漂移。读回后由 `get_scaling`/`get_opacity` 激活还原。

**练习 3**：假如要给 `save_ply` 补上时间球谐块的持久化，最小改动是什么？

**答案**：写侧把 [第 362-367 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L362-L367)的 `sh_channels` 从 `(active_sh_degree+1)²` 换成 `get_max_sh_channels`（4D 且 deg_t>0 时为 `(d+1)²(dt+1)`），让全部 47 通道落盘；读侧同步放宽 [第 296 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L296)的断言并按 `get_max_sh_channels` reshape。但改完仍是「有损时间元数据」（`active_sh_degree_t` 不落盘），这也是为什么恢复训练一律走 `.pth`。

### 4.4 capture / restore：无损检查点协议

#### 4.4.1 概念说明

`capture`/`restore` 是与 PLY 完全互补的持久化路线：它把模型**整个状态**（含优化器动量、梯度统计、网络权重）打包成一个顺序敏感的元组，经 `torch.save` 存为 `chkpntN.pth`。它解决 PLY 解决不了的两件事：**无损恢复**（所有属性、所有通道原样保留）与**断点续训**（Adam 的一阶/二阶动量也在里面）。

#### 4.4.2 核心流程

```text
capture()（gaussian_dim==4 分支，21 个元素按固定顺序）
  → torch.save((capture元组, iteration), model_path/chkpntN.pth)   # Scene.save，train.py 常规保存
  → torch.save((capture元组, iteration), model_path/chkpnt_best.pth) # train.py 按测试 PSNR 保存

restore(model_args, training_args)
  ├─ training_args is None   → 推理恢复：只解包装属性，不重建优化器
  └─ training_args is not None → 续训恢复：training_setup 重建优化器
                                 → load_state_dict 恢复 Adam 动量 / Coefficient 权重 / coef 优化器
```

#### 4.4.3 源码精读

**capture 的 21 元素元组**（[scene/gaussian_model.py:116-139](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L116-L139)）。顺序即协议，下表同时是「读 `.pth` 时的索引表」：

| 索引 | 元素 | 说明 |
| --- | --- | --- |
| 0 | `active_sh_degree` | 当前激活空间球谐阶数 |
| 1-7 | `_xyz, _features_dc, _features_rest, _scaling, _rotation, _opacity, max_radii2D` | 3DGS 七件套（裸值） |
| 8-10 | `xyz_gradient_accum, t_gradient_accum, denom` | 致密化梯度统计（u5-l3 详讲） |
| 11 | `optimizer.state_dict()` | Adam 全部动量 |
| 12 | `coef_optimizer.state_dict()` 或 None | Coefficient 网络的优化器 |
| 13 | `spatial_lr_scale` | 即 cameras_extent，学习率尺度 |
| 14-16 | `_t, _scaling_t, _rotation_r` | 第四维三件套 |
| 17 | `rot_4d` | 开关本身也被存档 |
| 18 | `env_map` | 球面背景图（未用时为空张量） |
| 19 | `active_sh_degree_t` | 当前激活时间球谐阶数 |
| 20 | `coefficient.state_dict()` 或 None | Neural Decaying Function 网络权重 |

对比 3D 分支（[第 99-115 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L99-L115)）只有 14 个元素——u3-l2 说过的「14 对 21」即由此而来。注意第 4-6 项存的是**整个 `_features_rest` 张量**，47 个通道一个不少：这就是 `.pth` 无损、`.ply` 有损的直接证据。

**restore 的两条路径**（[scene/gaussian_model.py:180-191](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L180-L191)）：`training_args is None` 时只解包属性（推理，render.py 走这条）；非 None 时先 `training_setup(training_args)` 重建优化器参数组，再 `load_state_dict` 恢复动量，最后恢复 Coefficient 权重与其优化器。三个细节值得圈出：

1. **恢复会覆盖 `rot_4d` 开关**（[第 175 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L175)）——检查点比当前配置说了算；但 `gaussian_dim`、`max_sh_degree`、`max_sh_degree_t` **不在**元组里，解包前必须用与训练一致的配置构造模型，否则 21 个值会按错误的分支语义对号入座。
2. **休眠 bug**：`if training_args is not None:` 块内无条件执行 `self.t_gradient_accum = t_gradient_accum`（[第 183 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L183)），而 3D 分支的解包（[第 143-156 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L143-L156)）根本没有这个名字——`gaussian_dim=3` 且带 `training_args` 恢复会 `NameError`。4C4D 只用 4D，所以休眠。
3. 元组协议**顺序敏感、无键名**：任何一方改了顺序，另一方静默错位——这是手工序列化的代价，也是 u3-l1 讲过的「capture/restore 是顺序敏感协议」的最终落点。

**train.py 的三个触发点**：`--start_checkpoint` 恢复（[train.py:69-72](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L69-L72)，`restore(model_params, opt)` 带 `training_args`，可续训）；常规保存（[train.py:278-281](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L278-L281)）在 `iteration in saving_iterations` 时调 `scene.save`；best 检查点（[train.py:272-276](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L272-L276)）只 `torch.save(capture())` 不写 PLY。`Scene.save`（[scene/__init__.py:109-112](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L109-L112)）则双写：`.pth` + `point_cloud/iteration_N/point_cloud.ply`。保存点默认 `[7000, 30000]`（[train.py:386](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L386)），且收尾迭代总会被追加（[train.py:432](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L432)）——但注意 u1-l4 的提醒：改 `iterations` 时 `save_iterations` 在合并前已定型，中途保存点不会跟随。

**两条持久化路线总对比**：

| 维度 | `chkpntN.pth`（capture/restore） | `point_cloud.ply`（save_ply/load_ply） |
| --- | --- | --- |
| 内容 | 21 元素全量：属性 + 梯度统计 + 双优化器 + Coefficient + env_map | 68 列裸值属性 |
| `f_rest` 时间块（16-47 通道） | 保留 | **丢失** |
| 优化器状态 / spatial_lr_scale | 保留 | 无 |
| 恢复后可否续训 | 可以 | 不可以 |
| 自往返（当前 HEAD） | 正常 | **load_ply 读不回 save_ply 的产物**（4.3.3 坑二/三） |
| 典型用途 | 续训、render.py 推理 | 查看器观察、外部工具交换 |

#### 4.4.4 代码实践

**实践目标**：把一个真实检查点按 4.4.3 的索引表逐项「验明正身」，确认哪一项存的是 Coefficient 网络。

**操作步骤**（示例代码；有 checkpoint 文件即可，CPU 机器用 `map_location` 也能跑，**待本地验证**）：

```python
# 示例代码：save as inspect_ckpt.py
import sys, torch
model_params, iteration = torch.load(sys.argv[1], map_location="cpu")
print("iteration =", iteration, "| 元组长度 =", len(model_params), "（4D 预期 21）")
for i, item in enumerate(model_params):
    if torch.is_tensor(item):
        desc = f"tensor {tuple(item.shape)} {item.dtype}"
    elif isinstance(item, dict):
        key0 = next(iter(item)) if item else "-"
        desc = f"dict({len(item)} 项)，首键 {key0}"
    else:
        desc = f"{type(item).__name__}: {item}"
    print(f"[{i:2d}] {desc}")
```

**需要观察的现象与预期结果**：

1. 长度为 21；`[0]` 与 `[19]` 是小整数（激活阶数），`[13]` 是 float（`spatial_lr_scale`），`[17]` 是 bool（`rot_4d`）。
2. `[11]`、`[12]` 是 dict——`state['exp_avg']` 之类即 Adam 动量；`[20]` 就是 **Coefficient 网络的 state_dict**（未启用 opacity_decay 训练时为 None，启用时键形如 `0.weight`/`2.weight` 的两层 Linear，对应 u6-l1）。
3. `[4]`（`_features_rest`）形状为 `(N, 47, 3)`——与 PLY 的 15 通道对比，直接印证 4.3.3 坑一。

#### 4.4.5 小练习与答案

**练习 1**：`chkpnt_best.pth` 与 `chkpnt30000.pth` 有何差别？

**答案**：内容格式相同（都是 `(capture元组, iteration)` 二元组），差别在触发条件与伴生产物：best 只在 `iteration in testing_iterations` 且测试 PSNR 创新高时写、**不写 PLY**；常规保存在 `saving_iterations` 触发、经 `Scene.save` 同时写出 `point_cloud/iteration_N/point_cloud.ply`。

**练习 2**：为什么 restore 之后 `get_opacity` 能立刻给出与保存时一致的值，而 PLY 路径不一定？

**答案**：`.pth` 存的也是裸值 `_opacity`，restore 原样写回属性，激活后必然一致；PLY 路径虽然 opacity 列也完整，但 `f_rest` 截断与（推演中的）前缀匹配问题使其他属性失真，整体渲染结果不可复现。

**练习 3**：把 capture 元组里的元素交换两个顺序，训练与恢复会发生什么？

**答案**：训练不受影响（capture 只是打包）；但用旧检查点恢复时，restore 按位置解包，两个元素会静默互换——若形状恰好兼容（如 `_scaling` 与 `_scaling_t` 都是列向量级别）不会报错，模型却已损坏。这就是「顺序敏感、无键名」协议的风险，也是练习用 4.4.4 索引表核对的意义。

## 5. 综合实践

**任务：给 4C4D 的两条持久化路线做一次「保真度审计」。**

用一个 20000 点的合成点云（4.1.4 的脚本可复用），完成一条完整链路并给出审计结论：

1. **初始化**：`GaussianModel(sh_degree=3, gaussian_dim=4, time_duration=[0,10], rot_4d=True, sh_degree_t=2)` + `create_from_pcd`（分别用带/不带 `time` 的点云，记录 `_t` 的 min/max/直方图；再对比 `redundant_ratio=0.0` 与 `0.2`）。
2. **构造优化器**：按 u3-l2 的方式给模型喂一份合理的 `training_args` 调 `training_setup`（capture 元组里 `optimizer.state_dict()` 需要 optimizer 已存在——不 setup 直接 capture 会在第 11 项上炸出 `AttributeError: 'NoneType'`，这本身就是一个值得记录的发现）。
3. **无损路线往返**：`capture()` → 逐项与原属性 `torch.equal` 对照 → 按相同配置新建模型 `restore(model_args, None)` → 再对照 `get_scaling_t`、`get_rotation_r`、`_features_rest` 形状。预期全部一致。
4. **有损路线审计**：`save_ply` → 用 4.3.4 的字段名模拟脚本核对 68 列布局 → 记录 `load_ply` 实际发生的报错（对照 4.3.3 坑二/坑三）→ 若按练习建议修好前缀匹配，再读回并对比：`_features_rest` 从 `(N,47,3)` 变 `(N,15,3)`、`_t` 分布是否保留。
5. **产出**：一张三列结论表（属性 | .pth 往返 | .ply 往返），并回答一句话问题——**如果只允许保留一种持久化格式，4C4D 应该保留哪个，为什么？**（提示：render.py 的选择已经给出了答案。）

GPU 环境缺位时，第 1、3 步无法执行的部分标注「待本地验证」，第 4 步的字段名模拟在纯 CPU 上即可完成。

## 6. 本讲小结

- `create_from_pcd` 的初始化分三类信息：点云自带（位置/颜色/可选时间戳）、几何统计（`distCUDA2` 最近邻距离 → log 空间空间尺度）、全局假设（恒等旋转、`inverse_sigmoid(0.1)`、时间尺度 `duration/5`）。
- 无时间戳时 `_t` 在 \([t_0 - \frac{rD}{2},\, t_1 + \frac{rD}{2})\) 均匀采样，`redundant_ratio` 给静态点云提供时间冗余覆盖；但经 train.py 的参数链实际默认是 0.0（三层默认值不一致，yaml 不覆盖）。
- 初始时间尺度 `duration/5` 是经验常数（被注释的 `distCUDA2` 方案说明作者试过数据驱动路线），它给每个新生的高斯在时间轴上约三分之一的覆盖，语义在 `rot_4d` 开关下差一个平方（u3-l3 陷阱）。
- `save_ply` 是 4D 专用的 68 列裸值表；它只持久化前 15 个 `f_rest` 通道，时间球谐块（16-47 通道）整块丢失。
- `load_ply` 的前缀匹配有硬伤：`startswith("rot")` 吞掉 `rot_r_*` 列、单字段 `t`/`scale_t` 的 int 排序键抛 `ValueError`——本仓库的 4D PLY 无法自往返（静态推演结论，待本地验证）；推理实际走 `.pth` 的 restore。
- `capture`/`restore` 是 21 元素的顺序敏感无损协议，含双优化器、Coefficient 权重与全部 47 个 `f_rest` 通道；`training_args` 是否为 None 区分续训与推理两种恢复。

## 7. 下一步学习建议

- **u5-l1（train.py 训练主循环全景）**：看本讲产出的九组参数如何被 `training_setup` 编进优化器参数组、`scene.save` 在循环中的调用时机，以及 `saving_iterations` 与 `iterations` 的联动坑（u1-l4）。
- **u5-l5（检查点、日志与输出目录）**：从训练循环视角再访 `capture`/`restore`，梳理输出目录里 `.pth`、`.ply`、`training_params.txt`、`cfg_args` 各自的用途与生命周期。
- **u6-l1/u6-l2（Neural Decaying Function）**：本讲 4.4.3 索引表里的第 20 号元素（Coefficient state_dict）与第 12 号元素（coef_optimizer）将在那里成为主角。
- **源码延伸阅读**：对照 [scene/dataset_readers.py:145-175](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L145-L175) 的 `fetchPly`/`storePly`，体会「按名读取」与「前缀匹配」两种 PLY 解析风格的可靠性差异——如果你想给 4C4D 提交第一个修复 PR，`load_ply` 的两处前缀问题是不错的候选。
