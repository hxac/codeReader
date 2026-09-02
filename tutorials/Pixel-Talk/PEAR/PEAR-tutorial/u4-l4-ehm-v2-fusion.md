# EHM_v2：用 FLAME 头替换 SMPL-X 头的统一表达

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 EHM_v2 在**构造期**做的「模板头部替换」：用 FLAME 模板顶点按 `smplx2flame_ind` 覆盖 SMPL-X 头部顶点，并以「双眼关节中点」为锚点完成对齐。
2. 走读 EHM_v2.forward 中的 **FLAME→SMPLX 动态融合三步**：取出旧头 → 对齐覆盖新头 → 恢复颈部与边界顶点，并理解为什么蒙皮（LBS）要放在融合**之后**。
3. 说明头部平移对齐公式（`joints[3:5]` 均值）与 `head_scale` 各向异性缩放的几何含义。
4. 说出输出字典四个键的形状与语义，理解 145 个关节是如何由「55 回归关节 + 68 面部 landmark + 22 附加关节」拼出来的。
5. 理解 `laplacian_matrix` 等缓冲在正则化中的**潜在**用途，以及它当前在仓库中的真实消费状态（备而未用）。

## 2. 前置知识

本讲是单元四的收官，默认你已读过前三讲。这里补几个本讲会用到的概念：

- **T-pose（绑定姿势）**：人体双臂平举、没有任何关节旋转的标准姿势。参数化模型的模板顶点都定义在 T-pose 下。本讲所有「替换头部」的操作都发生在 T-pose 的模板顶点上，之后才做姿态蒙皮。
- **顶点索引替换（indexing replacement）**：SMPL-X 与 FLAME 是两个独立网格，PEAR 用一张预先算好的索引表 `smplx2flame_ind`（5023 个原生对应 + 120 个牙齿顶点，共 5143 项）说明「SMPL-X 的第 k 个头区顶点对应 FLAME 的第 k 个顶点」。于是换头可以写成一行张量赋值：`smplx_vertices[idx] = flame_vertices`。
- **锚点对齐（anchor alignment）**：两个网格的坐标系不完全重合，直接赋值会错位。做法是各选一个解剖位置相同的「锚点」（这里用左右眼球关节的中点，即头部中心），先减去源网格锚点、再加目标网格锚点，把新头「平移」到旧头的位置上。
- **重心坐标插值（barycentric interpolation）**：三角形面片内任一点的坐标可以用三个顶点的加权和表示，权重和为 1。面部 landmark 不是顶点，而是「某个面上、按某组重心系数」确定的点，所以要用这种方式从顶点算出来。
- **拉普拉斯矩阵（Laplacian matrix）**：图/网格上的差分算子 \( L = D - A \)（度矩阵减邻接矩阵）。对顶点坐标左乘 \( L \) 得到每个顶点与邻居均值的差，即「局部几何细节」。正则项 \( \|L V_{\text{new}} - L V_{\text{ref}}\|^2 \) 的直觉是：**允许网格整体变形，但每个顶点相对邻居的凹凸细节不要变**。
- **注释漂移**：u3-l3 引入的概念——代码注释描述的是旧版本行为。本讲会大量遇到：EHM_v2 默认 `add_teeth=True`，顶点数已是 10595（10475 原生 SMPL-X + 120 牙齿），但注释仍写 `[10475,3]`、`[5023,3]`。以代码为准。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [models/modules/ehm/EHM_v2.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py) | 本讲主角。推理链路唯一的「参数 → 网格」层：构造期做模板级换头，forward 做逐样本动态换头 + 蒙皮 + landmark/关节组装 |
| [models/modules/ehm/EHM.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM.py) | 旧版 EHM（v1）。多做 MANO 换手、替换策略不同，是理解 v2 设计取舍的最佳参照物 |
| [models/modules/smplx/SMPLX.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py) | SMPL-X 资产容器：`smplx2flame_ind` 的加载与牙齿扩充、`extra_joint_selector`/`use_joint_regressor` |
| [models/modules/flame/FLAME.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py) | FLAME 资产容器：`non_head_index`（颈+边界掩码）与 `head_index` 的定义 |
| [models/modules/flame/lbs.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py) | `lbs` / `lbs_wobeta` / `vertices2landmarks` / `find_dynamic_lmk_idx_and_bcoords` 的实现（u4-l3 已精读，本讲只引用） |
| [models/smplx/smplx_head.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py) | 网络解码头：本讲所有输入参数字典（`body_param`/`flame_param`）在这里组装 |
| [utils/loss_utils.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/loss_utils.py) | `laplacian_matrix` 缓冲的潜在消费者（逐帧优化阶段的拉普拉斯平滑正则） |

回顾一下调用位置（u2-l2 已走读）：三个推理入口都以 `ehm(outputs['body_param'], outputs['flame_param'], pose_type='aa')` 的方式调用本讲的主角，例如 [inference_wo_detect.py:86](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L86)。

## 4. 核心概念与源码讲解

### 4.1 模板头部替换初始化

#### 4.1.1 概念说明

EHM（Expressive Human Model）要解决的问题是：**SMPL-X 有身体和手但头部细节贫乏，FLAME 有精细的脸但没有身体**。传统 expressive 重建（如 SMPL-X 原生表情基底）脸部表达力不足；而分别输出两个网格又无法作为一个整体渲染、蒙皮。

PEAR 的方案是「**嫁接**」：以 SMPL-X 的 10475 个顶点为底座，把其中头部区域的顶点**按索引整体替换**成 FLAME 的顶点，替换后顶点数不变、网格拓扑不变（牙齿顶点两边都同步追加，见下），于是得到的统一网格既保留 SMPL-X 的身体与蒙皮权重，又拥有 FLAME 的脸部细节。这就是「EHM 统一表达」。

这一嫁接在代码里做了**两次**，这是理解本讲最容易混淆、也最关键的一点：

1. **构造期（静态）**：`__init__` 里用两个模型的**原始模板**（零参数 T-pose）做一次换头，得到一份「融合模板」`self.v_template` 并注册为缓冲。它是一次性快照，用于计算拉普拉斯矩阵等静态量。
2. **前向期（动态）**：`forward` 里每当来了新的 shape/表情/头尺度参数，都要在**当帧的 shape 化模板上重新做一次换头**——因为每一帧的身体体型、头部大小、头部位置都不同，锚点也要逐帧重算。

#### 4.1.2 核心流程

构造期流程（伪代码）：

```text
smplx = SMPLX(assets, add_teeth=True)     # 模板 10475+120 = 10595 顶点
flame = FLAME(assets, add_teeth=True)     # 模板 5023+120 = 5143 顶点

body_T  = 用 J_regressor 从 smplx 模板回归 55 个关节
head_T  = 用 J_regressor 从 flame 模板回归 5 个关节

a_flame = head_T 的 3、4 号关节（左右眼）均值   # FLAME 头部锚点
a_body  = body_T 的 23、24 号关节（左右眼）均值  # SMPL-X 头部锚点

v_fused = smplx.v_template.clone()
v_fused[smplx2flame_ind] = flame.v_template - a_flame + a_body   # 换头并对齐

注册缓冲：v_fused（融合模板）
        laplacian_matrix（基于 v_fused 与 smplx 面片）
```

对齐公式用数学写就是：对每个被替换的顶点 \( i \)

\[
v'_i \;=\; v^{\text{flame}}_i - \bar{J}^{\text{flame}}_{\text{eyes}} + \bar{J}^{\text{smplx}}_{\text{eyes}}
\]

其中 \( \bar{J}_{\text{eyes}} \) 是双眼关节坐标的均值。先减源锚点把 FLAME 头的「头心」搬到原点，再加目标锚点放到 SMPL-X 身体的头心处。只做平移、不做旋转——因为两个模板都是 T-pose 且脸朝同一方向，平移已足够。

两个锚点为什么选「双眼关节中点」而不是头顶或脖子？因为眼球中心接近头部几何中心，且 FLAME 与 SMPL-X 各自的关节回归器都能稳定回归出这个位置（FLAME 关节 3/4、SMPL-X 关节 23/24），无需额外标注。

#### 4.1.3 源码精读

先看构造函数如何实例化两个资产容器（u4-l1、u4-l2 已分别拆过它们的内部结构，这里只看接缝）：

[models/modules/ehm/EHM_v2.py:14-19](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L14-L19) —— 构造签名默认 `add_teeth=True`，SMPLX 与 FLAME 两个纯资产容器（零可学习参数）被挂在 EHM_v2 下：

```python
def __init__(self, flame_assets_dir, smplx_assets_dir,
              n_shape=300, n_exp=50, with_texture=False, add_teeth=True, ...):
    self.smplx = SMPLX(smplx_assets_dir, ..., add_teeth=add_teeth, ...)
    self.flame = FLAME(flame_assets_dir, n_shape=n_shape, n_exp=n_exp, ..., add_teeth=add_teeth)
```

接着是构造期换头的三行核心：

[models/modules/ehm/EHM_v2.py:21-25](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L21-L25) —— 模板替换 + 锚点对齐，一行完成：

```python
v_template, v_head_template = self.smplx.v_template.clone(), self.flame.v_template.clone()
tbody_joints = vertices2joints(self.smplx.J_regressor, v_template[None])   # [1,55,3]
flame_joints = vertices2joints(self.flame.J_regressor, v_head_template[None])  # [1,5,3]
v_template[self.smplx.smplx2flame_ind] = v_head_template \
    - flame_joints[0, 3:5].mean(dim=0, keepdim=True) \
    + tbody_joints[0, 23:25].mean(dim=0, keepdim=True)
self.register_buffer('v_template', v_template)
```

- `flame_joints[0, 3:5]`：FLAME 的 5 个关节是 root(0)、neck(1)、jaw(2)、left_eye(3)、right_eye(4)，切片取双眼。
- `tbody_joints[0, 23:25]`：SMPL-X 的 55 关节里 15 是 head、22 是 jaw、23/24 是双眼，切片同样取双眼。
- 最后一行的索引赋值正是 4.1.1 说的「一行张量赋值换头」。

`smplx2flame_ind` 这张索引表来自官方注册数据，加载于 [models/modules/smplx/SMPLX.py:208](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L208)（`SMPL-X__FLAME_vertex_ids.npy`），牙齿扩充在 [models/modules/smplx/SMPLX.py:535](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L535)：SMPL-X 侧追加 120 个牙齿顶点（模板从 10475 → 10595，见 [models/modules/smplx/SMPLX.py:521](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L521)），FLAME 侧同步把模板扩到 5143（[models/modules/flame/FLAME.py:422-423](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L422-L423)），两边一一对应，索引表才能继续工作。

融合模板注册后，紧接着构造拉普拉斯缓冲：

[models/modules/ehm/EHM_v2.py:27-31](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L27-L31) —— 用 pytorch3d 在融合网格上计算拉普拉斯矩阵：

```python
laplacian_matrix = Meshes(verts=[v_template], faces=[self.smplx.faces_tensor]).laplacian_packed().to_dense()
self.register_buffer("laplacian_matrix", laplacian_matrix, persistent=False)
D = torch.diag(laplacian_matrix)
laplacian_matrix_negate_diag = laplacian_matrix - torch.diag(D) * 2
self.register_buffer("laplacian_matrix_negate_diag", laplacian_matrix_negate_diag, persistent=False)
```

注意 `persistent=False`：这两个缓冲**不进 state_dict**（EHM_v2 本来也不进 checkpoint，双重保险）。它们的潜在消费者是 [utils/loss_utils.py:209-217](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/loss_utils.py#L209-L217) 的 `compute_laplacian_smoothing_loss`：

```python
L = self.laplacian_matrix[None, ...].detach()
basis_lap = L.bmm(verts[None]).detach()          # 参考网格的局部细节
offset_lap = L.expand(batch_size, -1, -1).bmm(offset_verts)
diff = (offset_lap - basis_lap) ** 2             # 细节保持约束
```

即「形变后网格的拉普拉斯（局部细节）要贴近基准网格」。但用 Grep 全仓检索可以确认：`Optimization_Loss`（[utils/loss_utils.py:110-131](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/loss_utils.py#L110-L131)，构造函数签名就接收 `laplacian_matrix, v_template, smplx2flame_ind` 三件套）**没有被任何入口导入**——它是为逐帧优化/高斯化阶段准备的模块，当前处于「备而未用」状态，与 u1-l3 里的孤儿模块同一待遇。学习时知道缓冲的用途即可，不要在主链路里找它的调用。

最后，构造函数以 [models/modules/ehm/EHM_v2.py:281-294](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L281-L294) 的 `get_head_idx_from_pos` 收尾：在融合模板上把 y 坐标大于 0.15 的顶点、以及 UV 展开图上面片中心 y 大于 0.15 的像素分别登记为 `head_idxs_temp` / `head_idxs_uv_flat` 两个缓冲（按几何位置粗划头部区域）。这两个缓冲同样没有主链路消费者，属于为下游 UV/高斯阶段准备的「头部区域掩码」。

**一个必须分清的细节**：构造期融合出的 `self.v_template` 与 forward 里实际使用的模板是**两份不同的张量**。forward 用的是 `self.smplx.v_template`（未换头的原始 SMPL-X 模板，见 4.2.3 第 4 步）再动态重做融合。`self.v_template` 只服务于拉普拉斯与头部索引这两个静态量。为什么不在 forward 里复用 `self.v_template`？因为 forward 的模板要先叠加**当帧的 shape/表情**（`blend_shapes`），而 shape 基底作用在换头前的 SMPL-X 拓扑上，融合必须在 shape 化之后逐帧重做。

#### 4.1.4 代码实践

**实践目标**：亲眼验证「构造期融合模板」确实把 FLAME 头嵌进了 SMPL-X 模板，并量化两者的锚点对齐。

**操作步骤**（示例代码，在仓库根目录新建脚本运行，需已按 u1-l2 备好资产）：

1. 构造 EHM_v2（构造较慢，因为要读 UV 数据，属正常现象，见 u4-l1）。
2. 比较融合模板与原始 SMPL-X 模板在 `smplx2flame_ind` 位置的差异。
3. 检查锚点：融合模板上按 SMPL-X 关节回归器回归出的双眼中点，应与原始模板的双眼中点几乎重合（换头不应移动头部位置）。

```python
# 示例代码：verify_template_fusion.py
import torch
from models.modules.ehm import EHM_v2

ehm = EHM_v2("assets/FLAME", "assets/SMPLX")          # CPU 即可，约需数十秒
ind = torch.as_tensor(ehm.smplx.smplx2flame_ind).long()

fused   = ehm.v_template                              # [10595,3] 融合模板
origin  = ehm.smplx.v_template                        # [10595,3] 未换头的 SMPL-X 模板

head_f, head_o = fused[ind], origin[ind]
diff = (head_f - head_o).norm(dim=-1)
print("模板顶点数:", fused.shape, " 头区顶点数:", head_f.shape)
print("头区差异  mean/max:", diff.mean().item(), diff.max().item())
print("头区之外差异(应全为 0):",
      (fused != origin).any(dim=-1).sum().item() - int((diff > 1e-6).sum().item()))

# 锚点检查：融合前后 SMPL-X 双眼中点应基本不动
from models.modules.flame.lbs import vertices2joints
J0 = vertices2joints(ehm.smplx.J_regressor, origin[None])
J1 = vertices2joints(ehm.smplx.J_regressor, fused[None])
print("双眼锚点偏移:",
      (J0[:, 23:25].mean(1) - J1[:, 23:25].mean(1)).norm(dim=-1).item())
```

**需要观察的现象**：头区（5143 个顶点）差异明显（FLAME 头与 SMPL-X 头本来形状就不同）；头区之外差异为 0（替换只发生在索引表覆盖的顶点上）；双眼锚点偏移接近 0 但不为 0（换头后关节回归器读到的顶点变了，锚点只是「近似不动」）。

**预期结果**：三行输出与上述描述一致。若报资产缺失错误，回到 u1-l2 检查 `assets/FLAME`、`assets/SMPLX` 目录。具体数值与耗时**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：构造期的对齐为什么只做平移、不做旋转和缩放？

**答案**：两个模板都是各自模型在 T-pose、零参数下的标准姿势，脸的朝向与世界坐标轴的约定一致（FLAME 官方与 SMPL-X 官方模板同源对齐），因此不存在相对旋转；而「每个人头的大小不同」这一缩放因素是逐样本的，由 forward 里的 `head_scale` 处理（4.2 节），构造期只处理「单位大小、标准位置」的模板，平移足够。

**练习 2**：`laplacian_matrix` 为什么要在**融合后**的模板上计算，而不是原始 SMPL-X 模板？

**答案**：拉普拉斯正则的目的是约束「最终输出网格」的局部细节，而 EHM_v2 的输出网格拓扑就是融合模板（头区来自 FLAME、其余来自 SMPL-X）。在融合网格上计算 \( L \)，邻居关系才与真实输出一致——头区的邻居是 FLAME 顶点而非已被替换掉的 SMPL-X 头顶点。

**练习 3**：`self.v_template` 用 `register_buffer` 注册且未传 `persistent=False`，它会出现在 EHM_v2 的 state_dict 里吗？这有影响吗？

**答案**：会（`persistent` 默认为 True）。但没有实际影响：u1-l3 已确认 `pear_model.pt` 只含 `backbone` 与 `head` 两段 state dict，EHM_v2 从不被 `load_state_dict`；即便保存也只是一个静态模板的冗余副本。

### 4.2 forward 中的 FLAME→SMPLX 融合

#### 4.2.1 概念说明

forward 是本讲的重头戏：给定网络解码头输出的两个参数字典（u3-l3 组装的 `body_param` 11 键与 `flame_param` 6 键），产出统一网格。整体分四段：

1. **FLAME 分支**：用 `flame_param` 跑 FLAME 侧 LBS，得到带表情、下颌、眼球、眼睑的精细头 `head_vertices`。
2. **身体分支**：用 `body_param` 中的 shape/exp 给 SMPL-X 模板做 shape 化，得到当帧的 T-pose 身体并回归 55 关节。
3. **融合三步**：取出旧头 → 锚点对齐覆盖新头 → 恢复颈部与边界顶点。
4. **蒙皮**：对融合后的 T-pose 网格执行 `lbs_wobeta`（u4-l3 讲过的「无 beta 版 LBS」），输出姿态化网格与 145 关节。

**为什么蒙皮必须放在融合之后？** 这是 EHM 设计的精髓：FLAME 头在融合时还处于「头部朝向与身体一致」的 T-pose；融合之后，头区顶点在 SMPL-X 的网格里，蒙皮时会被 SMPL-X 的颈部/头部关节旋转自然带动。也就是说，**头部的朝向完全由身体侧的 SMPL-X 姿态驱动，FLAME 只负责脸的局部细节（下颌、眼球、眼睑、表情）**。这也解释了 u4-l2 留下的伏笔：为什么 forward 里 FLAME 的 global/neck pose 被强制置零——如果 FLAME 自己再转一次头，就会和身体侧蒙皮「双重旋转」打架。

**为什么颈部和边界顶点要保留 SMPL-X 的？** FLAME 头是「一个头」，它的下边缘（脖子截口）与 SMPL-X 身体的脖子截口并不逐点相同。如果全部替换，脖子接缝处会出现台阶或裂缝。所以融合后要把 FLAME 里属于 `non_head_index`（颈部 + 边界掩码）的顶点恢复成 SMPL-X 原值，让接缝仍然落在 SMPL-X 自己的、与身体连续的几何上。

#### 4.2.2 核心流程

```text
输入: body_param(11 键) + flame_param(6 键)

① FLAME 分支
   betas = [shape(补零到300), exp(50)]            # 350 维
   full_pose = [global=0, neck=0, jaw(3), eyes(6)]  # 15 维，全局与颈部强制置零
   head_vertices, head_joints = lbs(flame 五件套)    # [B,5143,3], [B,5,3]
   head_vertices += 左右眼睑位移基底 * 系数          # 眨眼，LBS 之后加
   ori_head_vertices = head_vertices.clone()        # 留作输出（缩放前）
   head_vertices = head_vertices * head_scale       # 各向异性 3 维缩放

② 身体分支
   shape(200) 补零到 300, 与 exp(50) 拼成 350 维
   full_pose = [global, 21 身体, jaw=0, eyes=0, 左手15, 右手15]  # 55 关节
   new_template = smplx.v_template + blend_shapes(shape_components, shapedirs)
   tbody_joints = J_regressor @ new_template         # [B,55,3]

③ 融合三步（都在 T-pose 上）
   selected_head      = new_template[:, smplx2flame_ind]        # 取出旧头
   ori_selected_head  = 旧头.clone()
   selected_head      = head_vertices - FLAME眼中点 + SMPL-X眼中点  # 对齐覆盖
   selected_head[non_head_index] = ori_selected_head[non_head_index] # 恢复颈与边界
   new_template[:, smplx2flame_ind] = selected_head             # 写回

④ 蒙皮
   vertices, joints, ... = lbs_wobeta(full_pose, new_template, ...)  # 融合后一次蒙皮
```

融合对齐公式的逐帧版本：

\[
\tilde{v}_i = (v^{\text{flame}}_i \circ s) \;-\; \bar{J}^{\text{flame}}_{3:5} \;+\; \bar{J}^{\text{smplx}}_{23:25}, \qquad i \in \text{smplx2flame\_ind}
\]

其中 \( s \in \mathbb{R}^3 \) 是 `head_scale`（来自解码头 scale 解码器的后 3 维，见 [models/smplx/smplx_head.py:292-294](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L292-L294)），\( \circ \) 表示逐轴相乘（各向异性缩放）。注意运算顺序：**先绕 FLAME 坐标原点缩放，再减缩放前的眼心锚点**。严格来说，若 \( s \neq 1 \)，缩放后的头顶点与其「缩放前锚点」之差并不等于缩放后的相对位置，这里存在一点近似（作者在 notebook 副本 `EHM_v2-checkpoint.py` 中也留有「这样好像有点不 align」的疑问注释）；由于 \( s \) 接近 1 且锚点靠近坐标原点附近区域，实际误差很小。

#### 4.2.3 源码精读

**第 1 步：FLAME 分支。** 参数读取与清零策略：

[models/modules/ehm/EHM_v2.py:38-69](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L38-L69) —— 读 6 个 flame 参数与 `head_scale`，shape 不足 300 维补零，然后是本分支最重要的两行清零：

```python
global_pose_params = torch.zeros_like(global_pose_params).to(shape_params.device)
neck_pose_params = torch.zeros_like(neck_pose_params).to(shape_params.device)
```

网络确实预测了 `pose_params`（解码头输出 3 维头部全局旋转，见 [models/smplx/smplx_head.py:274](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L274)），但在这里被**直接丢弃**——头部朝向交给身体侧蒙皮，正如 4.2.1 所述。随后拼出 350 维 betas 与 15 维 full_pose（global 3 + neck 3 + jaw 3 + 双眼 6）。

[models/modules/ehm/EHM_v2.py:71-83](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L71-L83) —— FLAME 侧 LBS 与后处理（u4-l3 精读过 `lbs` 内部，这里看调用约定）：

```python
head_vertices, head_joints, J, T, A = lbs(betas, full_pose, template_vertices,
                                     self.flame.shapedirs, self.flame.posedirs,
                                     self.flame.J_regressor, self.flame.parents,
                                     self.flame.lbs_weights, dtype=self.flame.dtype)
if eyelid_params is not None:      # 眼睑：LBS 之后的加法式位移（u4-l2 讲过）
    head_vertices = head_vertices + self.flame.r_eyelid.expand(batch_size, -1, -1) * eyelid_params[:, 1:2, None]
    head_vertices = head_vertices + self.flame.l_eyelid.expand(batch_size, -1, -1) * eyelid_params[:, 0:1, None]
ori_head_vertices = head_vertices.clone()          # 缩放前快照，进输出字典
if head_scale is not None:
    head_vertices = head_vertices * head_scale[:, None]   # [B,1,3] 逐轴缩放
```

`ori_head_vertices` 就是本讲实践任务要拿来对比的「缩放前的 FLAME 原始头」。

**第 2 步：身体分支。** 参数规整：

[models/modules/ehm/EHM_v2.py:88-135](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L88-L135) —— 读 11 个 body 参数（`jaw_pose`/`eye_pose` 为 None、`joints_offset` 为 None，来自解码头 [models/smplx/smplx_head.py:296-298](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L296-L298)）。两处关键逻辑：

其一，**姿态格式自动判别**（[EHM_v2.py:114-121](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L114-L121)）：`global_pose` 是 3 维张量（轴角）则 `pose2rot=True`，是 3×3 矩阵（解码头经 `rot6d_to_rotmat` 输出的情况）则 `pose2rot=False`。**无论哪种，jaw 与 eye 都被置零**——下颌张合由 FLAME 分支的 `jaw_params` 负责，SMPL-X 侧再动下颌就重复了。

其二，**shape 维度对齐**（[EHM_v2.py:123-128](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L123-L128)）：网络只输出 200 维 shape，SMPL-X 基底有 300 维，后 100 维补零（注释「后面 100 维度不重要」）；再与 50 维 exp 拼成 350 维 `shape_components`。注意这里的 exp 是 `body_param['exp']`——SMPL-X 自己的表情基底，它会作用在模板头部顶点上，但那些顶点马上要被 FLAME 头替换，所以**脸部表情的实际来源是 FLAME 分支的 50 维 expression**，身体侧 exp 只影响未被替换的区域。

[models/modules/ehm/EHM_v2.py:137-143](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L137-L143) —— shape 化模板与关节回归（注意用的是 `self.smplx.v_template`，不是构造期融合的 `self.v_template`）：

```python
template_vertices = self.smplx.v_template.unsqueeze(0).expand(batch_size, -1, -1)
# 已经做了 shape 变换（beta）
new_template_vertices = template_vertices + blend_shapes(shape_components, self.smplx.shapedirs)
tbody_joints = vertices2joints(self.smplx.J_regressor, new_template_vertices)
```

注释 `[1,10475,3]` 又是注释漂移：`add_teeth=True` 下实际是 `[B,10595,3]`。被注释掉的旧实现（调用完整 `lbs` 取零姿态）说明这步曾经由 LBS 完成，如今显式拆成 `blend_shapes + vertices2joints` 两行，正好是 u4-l3 讲过的「shape 化外移」，为的是在蒙皮前插入换头。

**第 3 步：融合三步。** 全场核心：

[models/modules/ehm/EHM_v2.py:150-158](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L150-L158)：

```python
if not hasattr(self, 'head_index'):
    self.head_index = np.unique(self.flame.head_index)          # [4850]（v2 中未再使用）

if head_vertices is not None:  # 把 smplx 的人头顶点替换成 Flame 估计的头
    selected_head = new_template_vertices[:, self.smplx.smplx2flame_ind]
    ori_selected_head = new_template_vertices[:, self.smplx.smplx2flame_ind].clone()
    selected_head = head_vertices - head_joints[:, 3:5].mean(dim=1, keepdim=True) \
                                  + tbody_joints[:, 23:25].mean(dim=1, keepdim=True)
    selected_head[:, self.flame.non_head_index] = ori_selected_head[:, self.flame.non_head_index]
    new_template_vertices[:, self.smplx.smplx2flame_ind] = selected_head
```

五行逐行对应：

1. `selected_head`：按索引表从当帧 shape 化模板里**取出旧头**（含牙齿，5143 个顶点）。
2. `ori_selected_head`：旧头快照，下一步恢复颈与边界时要用。
3. 锚点对齐覆盖：`head_joints[:, 3:5]` 是 FLAME 侧当帧双眼关节（注意 FLAME 分支的 full_pose 里 global/neck 为零、jaw/eye 很小，锚点随表情轻微变化），`tbody_joints[:, 23:25]` 是当帧 SMPL-X 侧双眼关节（随体型变化）。两个均值一减一加，新头被平移到**当帧**身体的眼心处。
4. 颈与边界恢复：`non_head_index` 来自 FLAME 官方分区掩码（[models/modules/flame/FLAME.py:88-91](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L88-L91)，`neck` 与 `boundary` 两个分区拼接），这些位置的顶点**换回 SMPL-X 原值**，源码注释直言 "recover the neck and boundary vertices"。牙齿顶点不在该掩码里（掩码在加牙之前生成），所以牙齿保留 FLAME 侧几何。
5. 写回：融合完成的 T-pose 模板。

顺带一提第 150-151 行惰性计算的 `self.head_index`：在 v2 里**赋值后从未使用**——它是 v1 替换策略的遗物（v1 用「只写 head_index 位置」的方式换头，见 [models/modules/ehm/EHM.py:179-183](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM.py#L179-L183)），v2 改成「全写再恢复 non_head_index」后这行成了死代码，读源码时不要被它误导。

**第 4 步：蒙皮。**

[models/modules/ehm/EHM_v2.py:160-163](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L160-L163) —— 对融合后的模板一次蒙皮：

```python
vertices, joints, J, ver_transform_mat, joint_transform_mat = lbs_wobeta(
    full_pose, new_template_vertices,      # 融合后的 T-pose 模板
    self.smplx.posedirs,
    self.smplx.J_regressor, self.smplx.parents,
    self.smplx.lbs_weights, joints_offset=joints_offset,
    pose2rot=pose2rot, dtype=self.smplx.dtype)
```

传入的**全部是 SMPL-X 的蒙皮五件套**（posedirs、J_regressor、parents、lbs_weights）——头区顶点此刻已是网格的一部分，按 SMPL-X 的蒙皮权重随颈部/头部关节转动。返回值解包与 [models/modules/flame/lbs.py:338](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L338) 的 `return verts, J_transformed, J, T, A` 一一对应：`vertices` 姿态化网格、`joints` 姿态化关节（\( J_{\text{transformed}} \)）、`J` T-pose 关节、`ver_transform_mat` 逐顶点 4×4 混合变换（\( T \)）、`joint_transform_mat` 逐关节 4×4 变换（\( A \)）。注意 v1 的 [EHM.py:195](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM.py#L195) 把第二个返回值误命名为 `joints_transform`，以 v2 的命名为准（u4-l3 结尾警示过 lbs 三胞胎的返回顺序陷阱，这里是同一陷阱的变体）。

牙齿顶点如何跟着动？SMPL-X 侧加牙时手工设置了蒙皮权重：[models/modules/smplx/SMPLX.py:567-570](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L567-L570) —— `vid_teeth_upper` 权重全给 12 号关节（neck），`vid_teeth_lower` 全给 22 号关节（jaw）。于是在 EHM_v2 里张嘴时：FLAME 分支的 `jaw_params` 驱动下牙排几何，身体侧蒙皮又让下牙排随 SMPL-X 的 jaw 关节旋转——两套机制指向同一物理动作，u4-l2 讲过的「上牙绑 neck、下牙绑 jaw」在这里兑现。

**v1 与 v2 的融合策略对比**（理解设计演化的最短路径）：

| | EHM（v1） | EHM_v2 |
| --- | --- | --- |
| 换头方式 | 只把 `head_index` 位置写成 FLAME 头（[EHM.py:180-183](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM.py#L180-L183)） | 全部 `smplx2flame_ind` 写成 FLAME 头，再恢复 `non_head_index`（[EHM_v2.py:153-158](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L153-L158)） |
| 头部尺度 | 标量 `head_scale` + 平移 `head_pos_offset`（[EHM.py:65](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM.py#L65)） | 3 维各向异性 `head_scale`，无平移参数（[EHM_v2.py:82-83](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L82-L83)） |
| 换手 | 额外挂 MANO，替换左右手（[EHM.py:184-189](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM.py#L184-L189)） | 不做，手部直接用 SMPL-X 顶点 |
| landmark | 计算代码整段注释掉（[EHM.py:203-235](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM.py#L203-L235)） | 完整启用（见 4.3） |
| 输出 | 9 个键，含头/手子网格与参考关节（[EHM.py:239-254](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM.py#L239-L254)） | 4 个键（见 4.3.3） |

v2 的策略更简洁：不再需要 MANO（SMPL-X 手部精度已够），不再需要显式头部位移参数（锚点对齐自动定位），输出收窄到渲染与训练真正消费的字段。

#### 4.2.4 代码实践

**实践目标**：给头部替换加一个布尔开关，用同一组参数渲染「有 FLAME 头 / 无 FLAME 头」两张对比图，并量化 `ori_head_vertices` 与替换后头部顶点的差异——直观验证融合分支的作用。

**操作步骤**：

1. **加开关**（这是读者在本机checkout上做的实验性修改，做完可用 `git checkout -- models/modules/ehm/EHM_v2.py` 还原）。修改 [models/modules/ehm/EHM_v2.py:34](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L34) 的 forward 签名，追加一个参数：

   ```python
   # 示例代码：EHM_v2.forward 签名追加 use_flame_head=True
   def forward(self, body_param_dict: dict, flame_param_dict: dict = None, mano_param_dict: dict = None,
               zero_expression=False, zero_jaw=False, zero_shape=False,
               proj_type='persp', pose_type='rotmat', use_flame_head=True):
   ```

   再把 [EHM_v2.py:153](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L153) 的条件从 `if head_vertices is not None:` 改为 `if head_vertices is not None and use_flame_head:`。注意 FLAME 分支本身（38-86 行）不必跳过——`ori_head_vertices` 还要用于统计。

2. **跑对比**（示例代码，参考 [inference_wo_detect.py:55-97](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L55-L97) 的组装套路）：对同一张 `example/images` 下的图片分别调用两次：

   ```python
   # 示例代码：接在 inference_wo_detect.py 的 outputs = ehm_model(img_patch) 之后
   for flag in (True, False):
       pd = ehm(outputs['body_param'], outputs['flame_param'], pose_type='aa', use_flame_head=flag)
       pd_mesh_img = body_renderer.render_mesh(pd['vertices'][None, 0, ...], pd_camera, lights=lights)
       # ... 按 inference_wo_detect.py:93-97 的方式存为 mesh_cmp_{flag}.jpg
   ```

3. **打差异统计**（示例代码）：

   ```python
   # 示例代码：替换前后头部顶点差异
   ind = torch.as_tensor(ehm.smplx.smplx2flame_ind).long()
   fused_head = pd['vertices'][:, ind]                 # 蒙皮后的头区（含姿态）
   ori_head   = pd['ori_head_vertices']                # 缩放前的 FLAME 头（T-pose）
   d = (fused_head - ori_head).norm(dim=-1)
   print(f"头区顶点数: {ind.shape[0]}, 差异 mean={d.mean():.4f} max={d.max():.4f}")
   # 再排除蒙皮影响：对齐锚点后比较 ori(缩放前) 与 selected_head(缩放后)
   ```

**需要观察的现象**：

- `use_flame_head=False` 的渲染图头部是 SMPL-X 自带的低细节头，且因 jaw/eye 被置零而完全僵硬；`True` 的图脸部有表情、下颌可动、带牙齿。
- 差异统计里 `fused_head` 与 `ori_head` 的差包含三部分：姿态蒙皮、锚点平移、head_scale 缩放——数值应明显大于 0；若想做「纯替换差异」，应在两次 forward 之间比较同一帧的 `new_template_vertices[:, ind]`（可临时加一行打印，属实验性修改）。

**预期结果**：两张对比图差异集中在头部；统计值量级与身体尺度（米级，SMPL-X 模板坐标约 ±1）相当。渲染与统计的具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉第 157 行的 `selected_head[:, self.flame.non_head_index] = ori_selected_head[...]`（不恢复颈与边界），渲染结果最可能出现什么瑕疵？

**答案**：脖子接缝处出现错位、台阶或裂缝。FLAME 头的颈部截口顶点与 SMPL-X 身体脖子的截口顶点位置不一致，且 FLAME 的颈部顶点没有与身体连续的过渡；恢复 SMPL-X 原值后，接缝两侧仍是 SMPL-X 自身连续的几何，只在更靠上的脸部区域才切换为 FLAME 几何。

**练习 2**：身体侧把 `jaw_pose`、`eye_pose` 置零（EHM_v2.py:114-121），FLAME 侧把 `global_pose`、`neck_pose` 置零（EHM_v2.py:64-65）。请用一句话概括这种「对称置零」的分工。

**答案**：SMPL-X 只负责「头长在哪、朝哪偏」（通过身体姿态与蒙皮带动头部），FLAME 只负责「脸上发生了什么」（下颌、眼球、眼睑、表情）；各自把对方负责的自由度清零，避免同一部位被驱动两次。

**练习 3**：为什么融合操作（第 3 步）必须夹在 shape 化（第 2 步）与蒙皮（第 4 步）之间，而不能挪到蒙皮之后？

**答案**：三个原因。(a) `smplx2flame_ind` 索引表是按顶点序号定义的，蒙皮不改变顶点数量与顺序，这一点其实蒙皮后也成立；但 (b) FLAME 头是 T-pose 几何，若先蒙皮再换头，换上的头会保持 T-pose 朝向而身体已转动，头部将与身体姿态脱节，且无法再享受颈部关节的蒙皮带动；(c) 锚点对齐依赖 T-pose 下回归的关节（FLAME 眼心、身体眼心），蒙皮后的关节已含姿态，对齐公式不再自洽。所以「shape 化 → 换头 → 蒙皮」的顺序不可交换，这也正是 `lbs_wobeta`（把 shape 化从 LBS 里拆出来）存在的意义。

### 4.3 landmark 与关节输出组装

#### 4.3.1 概念说明

蒙皮得到 `vertices`（10595 顶点）之后，forward 的最后一段把它加工成训练与下游需要的**关节集合**。面部 landmark 是其中的关键：不同于身体关节有回归器，面部关键点定义在「面片 + 重心坐标」上——第 \( l \) 个 landmark 由第 \( f_l \) 个三角面的三个顶点 \( (v_{a}, v_{b}, v_{c}) \) 按重心系数 \( (w_a, w_b, w_c) \) 加权和得到：

\[
p_l = w_a v_a + w_b v_b + w_c v_c, \qquad w_a + w_b + w_c = 1
\]

其中一部分 landmark 是**静态的**（永远贴在固定的面上），另一部分是**动态的**——具体贴在哪个面取决于头部相对脖子的朝向（例如下巴处的点在低头时应换到更靠下的面），需要每帧根据姿态从候选面里挑选。

最终 145 个关节的构成是 \( 55 + 68 + 22 = 145 \)：

| 分组 | 数量 | 来源 |
| --- | --- | --- |
| 身体关节 | 55 | `lbs_wobeta` 回归的 SMPL-X 关节 |
| 面部 landmark | 68 = 51 静态 + 17 动态 | 重心插值 |
| 附加关节 | 22 | `extra_joint_selector`（从顶点挑选的关节，如指尖、眼角等） |

#### 4.3.2 核心流程

```text
vertices (10595) ──┬──> 静态 landmark 面/系数 (51)
                   ├──> 动态 landmark：按头部朝向从候选面挑选 (17)
                   │         └──> 两组合并 → vertices2landmarks → 68 个 landmark
                   ├──> extra_joint_selector(vertices, faces) → 22 个附加关节
                   └──> joints = cat([55, 68, 22]) → 145
                              │
                              └──> J14 回归器覆写其中 10 个身体关节（更稳）
```

#### 4.3.3 源码精读

[models/modules/ehm/EHM_v2.py:168-181](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L168-L181) —— 动态 landmark 挑选与重心插值：

```python
lmk_faces_idx = self.smplx.lmk_faces_idx.unsqueeze(dim=0).expand(batch_size, -1)      # 静态 51 个面
lmk_bary_coords = self.smplx.lmk_bary_coords.unsqueeze(dim=0).expand(batch_size, -1, -1)
dyn_lmk_faces_idx, dyn_lmk_bary_coords = find_dynamic_lmk_idx_and_bcoords(
        vertices, full_pose,
        self.smplx.dynamic_lmk_faces_idx, self.smplx.dynamic_lmk_bary_coords,
        self.smplx.head_kin_chain)          # 按头部运动链朝向挑选 17 个动态面
lmk_faces_idx = torch.cat([lmk_faces_idx, dyn_lmk_faces_idx], 1)
lmk_bary_coords = torch.cat([lmk_bary_coords, dyn_lmk_bary_coords], 1)
landmarks = vertices2landmarks(vertices, self.smplx.faces_tensor, lmk_faces_idx, lmk_bary_coords)
```

- `head_kin_chain = [15,12,9,6,3,0]`（[models/modules/smplx/SMPLX.py:93](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L93)）是 pelvis→…→head 的运动链，`find_dynamic_lmk_idx_and_bcoords`（实现在 [models/modules/flame/lbs.py:36](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L36)）沿它累计头部的复合旋转，据此从候选面里选出当前朝向最贴合的落点。
- `vertices2landmarks`（[models/modules/flame/lbs.py:103-139](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L103-L139)）就是重心插值的向量化实现，核心一行 `torch.einsum('blfi,blf->bli', lmk_vertices, lmk_bary_coords)`。
- 注意 landmark 贴的是 `self.smplx.faces_tensor`，即**融合网格**的面——头区的面片顶点已被换成 FLAME 几何，所以面部 landmark 实际落在 FLAME 头表面上，这正是训练时能用人脸关键点监督表情的几何基础。

[models/modules/ehm/EHM_v2.py:183-189](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L183-L189) —— 三组关节拼接成 145：

```python
final_joint_set = [joints, landmarks]                  # [B,55,3] [B,68,3]
if hasattr(self.smplx, 'extra_joint_selector'):
    extra_joints = self.smplx.extra_joint_selector(vertices, self.smplx.faces_tensor)
    final_joint_set.append(extra_joints)               # [B,22,3]
joints = torch.cat(final_joint_set, dim=1)             # [B,145,3]
```

`extra_joint_selector` 在 [models/modules/smplx/SMPLX.py:183-186](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L183-L186) 由 `smplx_extra_joints.yaml` 配置（`JointsFromVerticesSelector`，直接按顶点号取关节）。

[models/modules/ehm/EHM_v2.py:191-198](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L191-L198) —— 用 J14 回归器覆写 10 个身体关节：

```python
if self.smplx.use_joint_regressor:
    reg_joints = torch.einsum('ji,bik->bjk', self.smplx.extra_joint_regressor, vertices)
    replace_idxs = torch.tensor([2,3,6,7,8,9,10,11,12,13], device=joints.device).long()
    joints[:, self.smplx.source_idxs[replace_idxs].long()] = (
        joints[:, self.smplx.source_idxs[replace_idxs].long()].detach() * 0.0
        + reg_joints[:, self.smplx.target_idxs[replace_idxs].long()] * 1.0)
```

J14 是 SMPL-X 官方提供的「14 个主要关节」的顶点回归器（[models/modules/smplx/SMPLX.py:189-205](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L189-L205) 加载），比默认关节回归器在大关节上更稳，所以用它覆写髋、膝、踝、肩、肘、腕等 10 个位置。`detach()*0.0 + x*1.0` 是一个刻意的 autograd 技巧：数值上等于直接赋值为 `reg_joints`，但把梯度路径完全导向 J14 回归结果、切断对原 `joints` 的梯度——训练时损失只「教育」J14 通路。

最后是输出字典：

[models/modules/ehm/EHM_v2.py:204-211](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L204-L211) —— 四个键：

```python
prediction = {
    'vertices': vertices,                 # [B,10595,3] 姿态化统一网格（10475 SMPL-X + 120 牙齿）
    'joints': joints,                     # [B,145,3] 55+68+22
    'ver_transform_mat': ver_transform_mat,  # [B,10595,4,4] 逐顶点混合变换 T（u4-l3）
    'ori_head_vertices': ori_head_vertices,  # [B,5143,3] 缩放前的 FLAME 原始头（T-pose）
}
```

三个推理入口消费 `'vertices'` 送渲染器（[inference_wo_detect.py:93](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L93)）；训练循环额外消费 `'joints'` 做 2D/3D 关键点损失（u5-l3 会展开）。`ori_head_vertices` 为头部分析与正则保留原始 FLAME 几何。

两个顺带的源码阅读警示：(1) forward 签名里的 `proj_type`、`pose_type` 形参在函数体内**从未使用**（[EHM_v2.py:34-35](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L34-L35)），三个入口传的 `pose_type='aa'` 只是沿用 v1 接口的习惯，实际格式由 4.2.3 讲的形状判别决定——u2-l2 已提示过这一点。(2) [EHM_v2.py:101-102](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L101-L102) 的 `self.expression_params` 防御分支引用了 EHM_v2 上不存在的属性，若真走到会抛 `AttributeError`；它永远不会触发（解码头恒输出 exp），但不要模仿这种写法。

#### 4.3.4 代码实践

**实践目标**：验证 145 关节的三段构成与 J14 覆写行为。

**操作步骤**（示例代码，可在 4.2.4 的脚本里继续）：

```python
# 示例代码：接在一次 ehm(...) 调用之后
pd = ehm(outputs['body_param'], outputs['flame_param'], pose_type='aa')
print("vertices:", pd['vertices'].shape, " joints:", pd['joints'].shape)   # 期望 [1,10595,3] [1,145,3]

# 手工重算三段，验证拼接顺序
j55   = pd['joints'][:, :55]
lmk68 = pd['joints'][:, 55:123]
ext22 = pd['joints'][:, 123:]
print(j55.shape, lmk68.shape, ext22.shape)

# J14 覆写验证：临时用未覆写的 joints 对比（跳过 191-198 行需改源码，
# 更简单的办法：直接用 extra_joint_regressor 重算覆写值，与 joints 对照）
reg = torch.einsum('ji,bik->bjk', ehm.smplx.extra_joint_regressor, pd['vertices'])
src = ehm.smplx.source_idxs[torch.tensor([2,3,6,7,8,9,10,11,12,13]).long()].long()
tgt = ehm.smplx.target_idxs[torch.tensor([2,3,6,7,8,9,10,11,12,13]).long()].long()
print("J14 覆写位置最大误差:", (pd['joints'][:, src] - reg[:, tgt]).abs().max().item())  # 期望≈0
```

**需要观察的现象**：三段形状恰为 55/68/22；J14 覆写位置的最大误差为 0（说明这些关节确实被回归器接管）。

**预期结果**：输出与注释一致。`extra_joint_regressor` 需资产 `SMPLX_to_J14.pkl` 存在（u1-l2 已随 assets 准备）。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：68 个面部 landmark 为什么不直接做成「顶点号列表」，而要用「面 + 重心坐标」这种绕一圈的表示？

**答案**：因为关键点（如嘴角、眼角）在解剖上对应的是皮肤上的**位置**而不是网格的**顶点**——顶点位置随网格划分方式而定。用重心坐标把关键点表达为「某个面内的加权位置」，可以随网格连续变形而跟随，且不受顶点重排影响；动态 landmark 还能按头部朝向换面，表示「同一关键点在低头/抬头时贴在不同区域」。

**练习 2**：`use_joint_regressor` 覆写用的是 `detach()*0.0 + reg*1.0` 而不是直接赋值 `joints[:, src] = reg[:, tgt]`，两者数值相同，梯度上有什么区别？

**答案**：直接赋值对被赋值位置同样成立（autograd 对索引赋值会把梯度导向右侧），真正的差别在**显式切断左侧原值的梯度**：`detach()*0.0` 保证无论后续损失如何回传，都不会有梯度流回覆写前的 `joints`（即蒙皮回归通路），梯度只经由 J14 回归器（它以 buffer 形式存在，实际也不更新）。这是一种把意图写进代码的防御式写法。

**练习 3**：输出字典里为什么要同时给 `vertices` 和 `ori_head_vertices` 两份头部几何？

**答案**：`vertices` 里的头区是**已对齐、已缩放、已蒙皮**的最终几何，与身体融为一体，用于渲染与整体损失；`ori_head_vertices` 保留了**缩放前的 FLAME 原始头**，可用于头部分析（如表情幅度评估）、头部相关的正则或监督（训练侧损失可见 u5-l3），也为本讲这类「替换前后对比」实验提供了现成数据。

## 5. 综合实践

设计一个「**EHM 消融三联图**」任务，把本讲三个模块串起来：

1. **准备**：复制 `inference_wo_detect.py` 为 `ehm_ablation.py`（放仓库根目录，避免改原入口；也可放到 `data_input/` 外的自建目录，运行时从仓库根目录启动，理由见 u1-l2）。
2. **开关一（融合）**：按 4.2.4 给 EHM_v2.forward 加 `use_flame_head` 开关。
3. **开关二/三（表情与下颌）**：EHM_v2.forward 本来就有 `zero_expression`、`zero_jaw` 形参（[EHM_v2.py:34](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L34)），直接使用，无需改码。
4. **渲染三联图**：对同一张图各渲染一次——(a) 完整 EHM；(b) `zero_expression=True, zero_jaw=True`（有 FLAME 头但表情/下颌冻结）；(c) `use_flame_head=False`（完全退回 SMPL-X 头）。横向拼接保存。
5. **数据佐证**：对 (a)(c) 各打印一次 `pd['joints'][:, 55:123]`（68 面部 landmark）的坐标标准差——(a) 的 landmark 贴在 FLAME 表面、随表情分布，(c) 的贴在僵硬的 SMPL-X 头上；再打印 4.2.4 的头部差异统计。
6. **还原**：`git checkout -- models/modules/ehm/EHM_v2.py` 恢复源码，删除临时脚本，确认 `git status` 干净。

验收标准：三联图中 (a) 与 (c) 头部差异肉眼可见、(b) 介于两者之间（头型来自 FLAME 但表情中性）；能用自己的话说出每组差异对应 4.2.3 里的哪几行代码。渲染结果**待本地验证**。

## 6. 本讲小结

- EHM 的统一表达 = **索引替换 + 锚点对齐**：按 `smplx2flame_ind`（5143 项）把 FLAME 头顶点写进 SMPL-X 网格，锚点是两侧「双眼关节中点」（FLAME 关节 3:5 / SMPL-X 关节 23:25），构造期与 forward 各做一次，前者出静态融合模板，后者逐帧重做。
- forward 的顺序是「**FLAME 分支 → 身体 shape 化 → 换头 → 一次蒙皮**」：换头必须发生在 shape 化之后（体型当帧才知道）、蒙皮之前（T-pose 头才能被身体姿态带动），这正是 `lbs_wobeta` 存在的原因。
- 分工靠**对称置零**实现：FLAME 侧 global/neck 置零（头朝向交给身体蒙皮），SMPL-X 侧 jaw/eye 置零（下颌眼球交给 FLAME）；颈部与边界顶点替换后恢复 SMPL-X 原值（`non_head_index`）保证接缝连续；牙齿蒙皮权重手工设为上牙绑 neck、下牙绑 jaw。
- `head_scale` 是 3 维**各向异性**缩放，先绕 FLAME 原点缩放、再减缩放前眼心做平移，存在轻微近似。
- 输出四键：`vertices` [B,10595,3]（10475 SMPL-X + 120 牙齿，代码注释里的 10475 属注释漂移）、`joints` [B,145,3]（55 回归 + 68 面部 landmark + 22 附加，其中 10 个大关节再被 J14 回归器覆写并切断梯度）、`ver_transform_mat`、`ori_head_vertices`。
- `laplacian_matrix` / `laplacian_matrix_negate_diag` 与 `head_idxs_*` 缓冲服务于拉普拉斯平滑正则与头部区域掩码，其设计消费者 `utils/loss_utils.py` 的 `Optimization_Loss` 当前未被任何入口导入——**备而未用**，读主链路时不要去找它的调用。

## 7. 下一步学习建议

下一讲 **u4-l5（网格渲染：Renderer2、GS_BaseMeshRenderer 与光照）** 将消费本讲的 `'vertices'`：Renderer2 加载的 `smplx_tex.obj` 拓扑正是本讲 10595 顶点网格的面片结构（[models/modules/renderer/body_renderer.py:105-125](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/renderer/body_renderer.py#L105-L125)），配合 u3-l4 的相机矩阵把网格变成 1024×1024 图像。建议在进入下一讲前，先完成本讲 4.2.4 的实践——你会得到两张只有头部不同的渲染图，这正好是下一讲渲染链路的现成输入。若你更关心训练侧，可在完成单元四后直接跳到 u5-l2，届时本讲的 `joints`（145 维）与 `ori_head_vertices` 会在损失函数里再次出场。
