# u4-l3 LBS 蒙皮：lbs / lbs_wobeta / lbs_get_transform

## 1. 本讲目标

上一讲（u4-l2）我们把 FLAME 的 `lbs` 当黑盒调用：输入 betas 和 full_pose，输出 `head_vertices`。本讲打开这个黑盒。学完本讲你应该能：

1. **说清经典 LBS（Linear Blend Skinning，线性混合蒙皮）的五步流程**：shape blend → 关节回归 → pose blend → 前向运动学 → 蒙皮混合，并能写出每一步的数学公式。
2. **推导 `batch_rigid_transform` 的前向运动学**：给定静止关节位置和每个关节的旋转，如何沿 parents 树链式计算出摆好姿势的关节位置和每个关节的 4×4 变换矩阵。
3. **说明 `lbs_wobeta` 与经典 `lbs` 的输入差异与适用场景**：为什么 EHM_v2 换头之后必须用「先 shape 化、再蒙皮」的两段式接口。
4. **理解 `lbs_get_transform` 输出的逐顶点/逐关节变换矩阵（T 与 A）的几何语义与用途**。
5. 知道仓库里三份 `lbs.py` 的关系，以及同名函数返回值顺序不一致这个隐蔽的坑。

## 2. 前置知识

本讲是全手册数学密度最高的一讲，但所有概念都只需要线性代数基础。先用通俗语言把四个概念讲清楚：

**① 轴角表示（axis-angle）与旋转矩阵。** 描述 3D 旋转最直观的方式是「绕哪个轴转多少角度」：一个 3 维向量，方向是转轴、模长是角度（弧度）。它紧凑但不方便做矩阵运算，所以经常要转成 3×3 旋转矩阵 \(R\)。转换公式就是 Rodrigues 公式（见 4.1.3）。回忆 u3-l3：网络输出的 312 维姿态是 6D 表示，最终都要变成旋转矩阵才能进 LBS。

**② 齐次坐标与 4×4 变换矩阵。** 把 3D 点 \(v=(x,y,z)\) 写成 4 维 \(\tilde{v}=(x,y,z,1)\)，就能用一个 4×4 矩阵同时表达「旋转 + 平移」：

\[
\begin{bmatrix} v' \\ 1 \end{bmatrix}
=
\begin{bmatrix} R & t \\ 0 & 1 \end{bmatrix}
\begin{bmatrix} v \\ 1 \end{bmatrix}
= \begin{bmatrix} Rv + t \\ 1 \end{bmatrix}
\]

矩阵相乘天然对应「变换的复合」（先做 A 再做 B = B·A），这是前向运动学能用链乘实现的原因。

**③ 关节树（kinematic tree）与前向运动学（forward kinematics）。** 人体骨骼是一棵树：骨盆是根，脊柱→颈→头是一条链，肩→肘→腕→手指是另一条链。`parents` 数组就是这棵树的父母表：`parents[j]` 是关节 j 的父关节编号，根关节的 parents 为 -1。前向运动学指：已知每个关节**相对父关节**的旋转，沿树从根到叶逐级复合，算出每个关节在全局坐标系里的位置。「转肩关节」会带着整条手臂动，就是因为肘、腕、手指的变换都要先乘上肩的变换。

**④ 蒙皮权重（skin weights）。** 骨骼动了，皮肤怎么跟着动？每个顶点挂一组权重 \(w_{v,0},\dots,w_{v,J-1}\)（和为 1），表示它受各个关节影响的程度。肘部附近的顶点会同时给上臂、前臂各分一些权重，这样弯肘时肘部皮肤平滑过渡，而不是撕裂。LBS 的「线性混合」就是：顶点的新位置 = 各关节分别变换结果按权重加权平均。它的已知缺陷是弯 90° 以上时关节处会「瘪掉」（糖果纸效应），SMPL 系模型用 pose blend shapes 部分补偿这一点。

**本讲的上下文承接**：u4-l1 说过 SMPL-X 类是纯「资产容器」，五件套 buffer（`v_template`、`shapedirs`、`posedirs`、`J_regressor`、`lbs_weights` + `parents`）全部不可学习；EHM_v2 只借这些张量来算网格，从不调用 `SMPLX.forward`。所以**推理链路真正执行的 LBS 代码只有一个来源**：`models/modules/flame/lbs.py`（EHM_v2 在 [EHM_v2.py:L9](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L9) import 它）。这一点先记住，能帮你避开「读错文件」的弯路。

## 3. 本讲源码地图

| 文件 | 作用 | 在推理链路上吗 |
| --- | --- | --- |
| [models/modules/flame/lbs.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py) | **本讲主角**。EHM/EHM_v2 实际 import 的版本，含 `lbs`、`lbs_wobeta`、`lbs_get_transform`、`batch_rigid_transform`、`batch_rodrigues`、`blend_shapes`、`vertices2joints`、`rot_mat_to_euler` | 是 |
| [models/modules/smplx/lbs.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/lbs.py) | 近似副本，仅 `models/modules/smplx/SMPLX.py` 的 `forward`（僵尸代码，无人调用）使用；**返回值顺序与 flame 版不同** | 否 |
| [models/smplx/lbs.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/lbs.py) | 第三份拷贝，经典 SMPL-X 实现，`lbs` 只返回 2 个值；被 `SMPLXV2.py`、`smplx_layer.py` 引用，均不在推理链路 | 否 |
| [models/modules/ehm/EHM_v2.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py) | LBS 的三个真实调用点：FLAME 分支 `lbs`（L73）、身体分支 `blend_shapes` + 换头 + `lbs_wobeta`（L137-L163）、`get_transform_mat` 里的 `lbs_get_transform`（L264） | 是 |
| [models/modules/smplx/SMPLX.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py) | `parents`（55 关节，L157-L160）与 add_teeth 时对 LBS 输入张量的扩展约定（L547-L570） | 张量来源 |
| [models/modules/flame/FLAME.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py) | FLAME 侧 `parents`（5 关节，L111-L114） | 张量来源 |

一个帮助记忆的观察：`flame/lbs.py` 名字里带 flame，但它**不只服务 FLAME**——EHM_v2 的身体侧（SMPL-X，55 关节）也用它。它其实是「EHM 专用魔改版」恰好放在了 flame 目录下。

## 4. 核心概念与源码讲解

### 4.1 lbs 标准流程

#### 4.1.1 概念说明

SMPL / SMPL-X / FLAME 这一族参数化人体模型的核心思想是：**网格 = 模板 + 线性基的加权和**。LBS 函数就是把两 组参数（体型 β、姿态 θ）翻译成变形网格的「编译器」。它解决的问题：网络（u3-l3 的 9 个线性解码器）只能输出低维参数，而渲染需要 10475+ 个顶点的具体坐标——LBS 就是这个从参数到顶点的可微映射，且全部由矩阵乘法组成，天然支持反向传播，这正是训练端到端的关键。

#### 4.1.2 核心流程

经典 `lbs` 的五步流水线（`[B]`=批大小，`[V]`=顶点数，`[J]`=关节数）：

```text
输入: betas [B,NB], pose [B,(J+1)*3] 轴角, v_template [V,3],
      shapedirs [V,3,NB], posedirs [(J)*9, V*3],
      J_regressor [J,V], parents [J], lbs_weights [V,J]

① shape blend   : v_shaped = v_template + Σ β_i · B_i^shape          → [B,V,3]
② 关节回归      : J = J_regressor · v_shaped                          → [B,J,3]
③ pose blend    : v_posed = v_shaped + posedirs · (R_{1:J} - I)      → [B,V,3]
④ 前向运动学    : J_transformed, A = batch_rigid_transform(R, J, parents)
⑤ 蒙皮混合      : T = lbs_weights · A;  verts = T · [v_posed; 1]     → [B,V,3]

输出: verts [B,V,3], J_transformed [B,J,3], J [B,J,3], T [B,V,4,4], A [B,J,4,4]
```

对应的数学公式：

\[ v_{shaped} = v_{template} + \sum_{i=1}^{N_\beta} \beta_i B_i^{shape} \]

\[ v_{posed} = v_{shaped} + \Phi \cdot \mathrm{vec}\left(R_j - I\right)_{j=1..J} \]

\[ v' = \sum_{j=0}^{J-1} w_{v,j} \cdot A_j \begin{bmatrix} v_{posed} \\ 1 \end{bmatrix} \]

直觉版解释：① 捏体型（高矮胖瘦是把模板顶点沿「体型基」位移）；② 从捏好的身体表面按权重回归出关节该在哪（胖人肩膀位置不同）；③ 姿态 corrective（弯肘时肘部顶点预先「鼓」一点，补偿蒙皮的瘪陷）；④ 把骨架摆成姿势；⑤ 皮肤按权重跟着骨骼走。A_j 的几何含义见 4.2。

#### 4.1.3 源码精读

先看函数签名与三步前置（shape blend、关节回归）：

[models/modules/flame/lbs.py:L142-L143](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L142-L143) 定义 `lbs(betas, pose, v_template, shapedirs, posedirs, J_regressor, parents, lbs_weights, joints_offset=None, pose2rot=True, ...)`。注意两个可选参数：`joints_offset` 允许外部平移微调关节位置（EHM_v2 的 `body_param` 里就带这个键，见 forward 的 [EHM_v2.py:L97](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L97)）；`pose2rot` 决定 pose 是轴角（True，需转矩阵）还是已是旋转矩阵（False，u2-l5 讲过网络输出的是旋转矩阵，EHM_v2 会按张量维度选分支）。

[models/modules/flame/lbs.py:L185-L192](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L185-L192) 是①②步：`v_shaped = v_template + blend_shapes(betas, shapedirs)`，再用 `vertices2joints(J_regressor, v_shaped)` 回归关节。两个辅助函数都只有一行 einsum：

- [blend_shapes:L360-L381](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L360-L381)：`torch.einsum('bl,mkl->bmk', betas, shape_disps)`，即位移 \([B,V,3]\)。源码注释里给了逐分量形式 `Displacement[b,m,k] = Σ_l betas[b,l] * shape_disps[m,k,l]`。**关键性质：β 全零则位移全零**（源码中文注释「beta 为 0，则该项为 0」）。EHM_v2 在 [EHM_v2.py:L139](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L139) 手工调用它做身体侧 shape 化——这正是 4.3 要讲的「拆出 lbs_wobeta」的原因。
- [vertices2joints:L340-L357](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L340-L357)：`torch.einsum('bik,ji->bjk', vertices, J_regressor)`，\(J = V \cdot J_{reg}^{\top}\)。`J_regressor` 每行是一组作用于顶点的权重（行和为 1 的凸组合），把「表面顶点」线性组合出「骨架关节」。

第③步 pose blend 与轴角转换：

[models/modules/flame/lbs.py:L195-L211](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L195-L211)：先用 `batch_rodrigues` 把 pose 展平成 (B·J,3) 的轴角转成 (B,J,3,3) 旋转矩阵，然后构造 **pose feature = (R - I)**——注意切片 `rot_mats[:, 1:, :, :]` **丢掉了第 0 个关节（根关节）**。原因：posedirs 的列数是 (J-1)×9，官方 SMPL 数据只在「子关节」上定义了姿态形变基；根关节的旋转效果由第④步的整体刚体变换承担，不需要模板局部形变。最后 `pose_offsets = pose_feature @ posedirs` 得 (B,V,3)，加回 `v_shaped` 得 `v_posed`。

[batch_rodrigues:L384-L415](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L384-L415) 实现 Rodrigues 公式。对轴角向量 \(\omega\)，角度 \(\theta=\|\omega\|\)、单位轴 \(k=\omega/\theta\)，构造反对称矩阵：

\[
K = \begin{bmatrix} 0 & -k_z & k_y \\ k_z & 0 & -k_x \\ -k_y & k_x & 0 \end{bmatrix},
\qquad
R = I + \sin\theta \, K + (1-\cos\theta) K^2
\]

源码 [L399-L414](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L399-L414) 逐步对应：`angle = norm(rot_vecs + 1e-8)`（加 1e-8 防零向量除零）、`rot_dir` 即单位轴、`K` 用 `torch.cat(...).view(B,3,3)` 拼出（[L410-L411](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L410-L411) 的 9 个元素排列就是上面的 K）、最后一行 `ident + sin*K + (1-cos)*K@K`。零轴角输入时 sin=0、1-cos=0，恰好返回单位阵。

第④步只是一行调用（[L212-L213](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L212-L213)），详解放到 4.2。第⑤步蒙皮混合：

[models/modules/flame/lbs.py:L215-L230](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L215-L230)：把 A（B,J,4,4）reshape 成 (B,J,16)，与蒙皮权重 W（B,V,J）做矩阵乘 `T = W @ A` 得**逐顶点**混合变换 (B,V,4,4)；再把 `v_posed` 补齐次坐标 1，`v_homo = T @ v_posed_homo`，切片 `[:,:,:3,0]` 丢掉齐次分量得到最终顶点。返回五元组 `(verts, J_transformed, J, T, A)`——**注意 T 和 A 的顺序，4.3.3 会讲两个副本在这个顺序上不一致的坑**。

最后提一句规格里点名的 [rot_mat_to_euler:L27-L33](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L27-L33)：它只从旋转矩阵提取**绕 y 轴的角度**（`atan2(-R[2,0], sqrt(R[0,0]² + R[1,0]²))`），并不在 lbs 主链路里，唯一的调用方是 `find_dynamic_lmk_idx_and_bcoords`（[L36-L100](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L36-L100)）：用头部运动链的累计 y 角度查表，决定「动态面部 landmark」落在哪张三角面上（转头时 landmark 应换位置）。EHM_v2 在 [EHM_v2.py:L170-L176](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L170-L176) 用它算 68 个面部 landmark。

#### 4.1.4 代码实践

本实践不依赖任何受许可证保护的资产，只需要 torch，可以直接运行（示例代码）：

**目标**：验证 `batch_rodrigues` 的正确性，并亲手确认「零轴角 → 单位阵」「pose 全零 → 网格不动」两条性质中的第一条。

1. 实践目标：为 `batch_rodrigues` 写一个最小单测，与 pytorch3d 的参考实现交叉验证。
2. 操作步骤：在仓库根目录新建 `test_rodrigues.py`：

```python
# 示例代码
import torch
from models.modules.flame.lbs import batch_rodrigues
from pytorch3d.transforms import axis_angle_to_matrix

torch.manual_seed(0)
aa = torch.randn(1000, 3) * 2.0          # 随机轴角，角度可超过 pi

R = batch_rodrigues(aa)
R_ref = axis_angle_to_matrix(aa)

print("正交性 |R^T R - I| 的最大值:", (R.transpose(1, 2) @ R - torch.eye(3)).abs().max().item())
print("行列式最大偏差:", (torch.linalg.det(R) - 1).abs().max().item())
print("与 pytorch3d 的最大元素差:", (R - R_ref).abs().max().item())
print("零轴角的结果是单位阵:", torch.allclose(batch_rodrigues(torch.zeros(1, 3)), torch.eye(3)[None]))
```

3. 运行 `python test_rodrigues.py`（需要 u1-l2 搭好的 pear 环境，pytorch3d 已装）。
4. 需要观察的现象：前两项应在 1e-5 量级以内，第三项应在 1e-5 ~ 1e-6 量级（两实现的浮点顺序不同）；最后一项打印 True。
5. 预期结果：四条全部满足即通过。角度接近 π 时若第三项略增大，属正常现象（Rodrigues 公式在 θ≈π 处数值条件变差）。**待本地验证**（本讲义写作环境无法运行）。

#### 4.1.5 小练习与答案

**练习 1**：若 `betas` 与 `pose` 全零，`lbs` 的输出顶点等于什么？`J_transformed` 等于什么？

**答案**：`blend_shapes` 输出全零 → `v_shaped = v_template`；`R - I = 0` → `pose_offsets` 全零；`batch_rigid_transform` 中所有 R 为单位阵 → A 为纯平移且平移量为零（posed 关节 = rest 关节），蒙皮后顶点不变。所以 `verts = v_template`、`J_transformed = J`（rest 关节位置）。这个「全零恒等」是检验任何 LBS 实现的最低健全性测试。

**练习 2**：pose blend 为什么把根关节（第 0 个）排除在外（`rot_mats[:, 1:]`）？

**答案**：两层原因。数据层：官方 SMPL/SMPL-X/FLAME 资产的 `posedirs` 列数是 (J-1)×9，只在子关节上定义了姿态形变基，形状上就装不下根关节；语义层：根关节旋转是全身整体旋转，由第④步前向运动学的刚体变换整体承担，不应引起模板顶点相对骨架的局部形变。

**练习 3**：flame 版 `lbs` 在 [L182](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L182) 取 `batch_size = pose.shape[0]`，而 smplx 版在 [L187](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/lbs.py#L187) 取 `max(betas.shape[0], pose.shape[0])`。这个差异什么时候会暴露？

**答案**：只有当调用方传入的 betas 批大小与 pose 批大小不一致时才显现——flame 版静默以 pose 为准（betas 若更小会在广播或 einsum 处报错），smplx 版则取大者。正常调用（EHM_v2 中两者同批）永远不会触发，这是复制粘贴演化出的无害分歧，但读代码时要知道以哪份为准。

### 4.2 batch_rigid_transform：前向运动学推导

#### 4.2.1 概念说明

`batch_rigid_transform` 解决的问题是：**已知 rest 姿态（T-pose）下每个关节的位置 J、每个关节相对父关节的旋转 R、以及父母表 parents，求摆好姿势后的关节全局位置，以及供蒙皮使用的「把模板空间顶点搬到姿态空间」的逐关节 4×4 矩阵 A。** 它是 LBS 五步中最「骨骼」的一步，也是唯一有串行依赖（树形链式复合）的一步。

#### 4.2.2 核心流程

```text
输入: rot_mats [B,J,3,3], joints [B,J,3] (rest), parents [J]
① 求相对偏移  : rel_joints[j] = J[j] - J[parent(j)]            (root 除外)
② 拼局部矩阵  : M_j = [[R_j, rel_joints[j]], [0,1]]            → [B,J,4,4]
③ 沿树链乘    : G_j = G_{parent(j)} · M_j     (G_root = M_root)
④ 读平移列    : posed_joints[j] = G_j 的平移列
⑤ 减出蒙皮矩阵: A_j = G_j - pad(G_j · [J_j; 0])
输出: posed_joints [B,J,3], rel_transforms(A) [B,J,4,4]
```

第③步的递推就是前向运动学本身。展开第⑤步（注意源码 pad 的默认填充值是 **0** 而不是 1，见下文），\(G_j \cdot [J_j; 0]\) 的前三分量是 \(R_j^{global} J_j + t_j\)，于是：

\[
A_j = \begin{bmatrix} R_j^{g} & t_j - (R_j^{g} J_j + t_j) \\ 0 & 1 \end{bmatrix} = \begin{bmatrix} R_j^{g} & -R_j^{g} J_j \\ 0 & 1 \end{bmatrix}
\]

\[
A_j \begin{bmatrix} v \\ 1 \end{bmatrix} = \begin{bmatrix} R_j^{g}(v - J_j) + t_j \\ 1 \end{bmatrix}
\]

其中 \(t_j\) 恰是 posed_joints[j]。也就是说：**A_j 作用于顶点 v 的几何含义是「先把 v 平移到以关节 j 的 rest 位置为原点，应用关节 j 的全局旋转，再加回关节 j 的 posed 位置」**——这正是教科书上的蒙皮变换 \(G_j \cdot T(-J_j)\)。第⑤步用「矩阵减法」而不是「矩阵乘法」实现同一个东西，是一个初看很费解、推开后很巧妙的技巧。

用一个两关节的手算例子建立直觉（4.2.4 会用代码验证）：

- 关节 0 在原点 \(J_0=(0,0,0)\)，关节 1 在 \(J_1=(0,1,0)\)（沿 y 轴上方 1 米），`parents = [-1, 0]`。
- 关节 0 不转（\(R_0=I\)），关节 1 绕 z 轴转 90°（旋转矩阵 \(R_z\) 把 y 方向转向 -x 方向）。
- 则 \(M_1 = [[R_z, (0,1,0)], [0,1]]\)，\(G_1 = G_0 M_1 = M_1\)，**posed_joints_1 = 平移列 = (0,1,0)**——关节 1 自己的位置不动（它随父关节走，父没动）；动的是挂在它上面的东西。
- 若还有关节 2（parent=1，\(J_2=(0,2,0)\)），则 \(rel\_j_2 = (0,1,0)\)，\(G_2\) 的平移 = \(R_z \cdot (0,1,0) + (0,1,0) = (-1,1,0)\)——关节 2 被关节 1 的旋转带到了左上方。✓ 与直觉一致。

#### 4.2.3 源码精读

[models/modules/flame/lbs.py:L490-L547](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L490-L547) 是完整实现（smplx 版在 [L329-L383](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/lbs.py#L329-L383)，逻辑相同）。逐段看：

- [L514-L517](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L514-L517)：`rel_joints[:, 1:] -= joints[:, parents[1:]]` 一行完成①——用 parents 做高级索引取出每个关节父关节的 rest 位置，逐关节相减。root（下标 0）保持原值，因为 `parents[0] = -1` 不参与切片。这一行也解释了为什么 parents 必须是 long 张量（[SMPLX.py:L158](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L158) 从 `kintree_table[0]` 读出后强制 `.long()` 并把 `parents[0]` 置 -1）。
- [transform_mat:L477-L487](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L477-L487) 完成②：`F.pad(R, [0,0,0,1])` 给 3×3 旋转矩阵底行补 `[0,0,0,1]`，`F.pad(t, [0,0,0,1], value=1)` 给 3×1 平移补第 4 分量 1，横向拼接成 4×4。
- [L526-L534](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L526-L534) 完成③：`transform_chain` 是长度 J 的列表，第 i 项 = `transform_chain[parents[i]] @ transforms_mat[:, i]`。**这是一个沿树深的串行 for 循环**——G_i 依赖父节点的结果，无法完全并行。对 55 个关节这点串行开销可忽略，但值得知道瓶颈在哪。源码注释点明：「Subtract the joint location at the rest pose / No need for rotation, since it's identity when at rest」。
- [L536-L540](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L536-L540) 完成④：`posed_joints = transforms[:, :, :3, 3]`。注意这里 **同一行代码写了两遍**（L537-L540，第二遍是 L540 的重复赋值）——smplx 版里第二遍被注释掉了（[L375-L376](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/lbs.py#L375-L376)）。这是两份副本互相「打补丁」留下的痕迹，功能上无害，却是「这两个文件是手工同步的」的直接证据。
- [L542-L547](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L542-L547) 完成⑤。**最容易看错的一行**：

```python
joints_homogen = F.pad(joints, [0, 0, 0, 1])   # 变量名叫 homogen，实际补的是 0！
rel_transforms = transforms - F.pad(
    torch.matmul(transforms, joints_homogen), [3, 0, 0, 0, 0, 0, 0, 0])
```

`F.pad` 不指定 `value` 时默认填 **0**。所以 `joints_homogen` 是 \([J_j; 0]\) 而不是变量名暗示的 \([J_j; 1]\)。妙处在于：这个 0 恰好是必须的——按 4.2.2 的推导，\(G \cdot [J;0] = [R^g J;\,0]\)，相减后 A 的底行保持 \([0,0,0,1]\)（仍是仿射变换），平移列 \(t - R^g J\)，作用于齐次点正好给出 \(R^g(v-J) + t\)。若真的补成 1，A 的底行会变成 \([0,0,0,0]\) 且平移变成 \(-R^g J\)，蒙皮结果会整体丢掉关节的 posed 平移，网格塌向原点。读源码时请以这一行的**实际行为**为准，不要被变量名带偏。

最后看 parents 从哪来、长什么样：

- SMPL-X 侧 55 个关节：[SMPLX.py:L157-L160](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L157-L160) 从模型文件的 `kintree_table[0]` 读入。按 SMPL-X 官方关节顺序（以资产内 kintree_table 为准）：0 是骨盆（根），12 是颈、15 是头，22 是下颌、23/24 是左右眼，25–39 是左手链、40–54 是右手链。仓库内有两个现成证据：EHM_v2 用 `tbody_joints[:, 23:25]` 的均值当「SMPL-X 双眼中点」（[EHM_v2.py:L156](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L156)，u4-l4 换头对齐要用），add_teeth 给牙齿分配蒙皮权重时写死 `vid_teeth_upper → 12`（注释 `# move with neck`）、`vid_teeth_lower → 22`（注释 `# move with jaw`，[SMPLX.py:L568-L570](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L568-L570)）。
- FLAME 侧 5 个关节：[FLAME.py:L111-L114](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L111-L114) 同样读 `kintree_table[0]`：0 根、1 颈、2 下颌、3/4 双眼（EHM_v2 用 `head_joints[:, 3:5]` 均值当「FLAME 双眼中点」）。

顺带把 u4-l1 讲过的 add_teeth 与 LBS 张量联系起来：牙齿顶点是程序化生成后**拼接**到模板尾部的，所以它的 LBS 输入张量也要同步扩展——[SMPLX.py:L547-L560](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L547-L560) 给 `shapedirs`/`posedirs` 补零（牙齿不参与 shape/pose blend），[L564-L566](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L564-L566) 给 `J_regressor` 补零（牙齿不回归关节），[L567-L570](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L567-L570) 给 `lbs_weights` 补行并把上牙权重设给关节 12（颈）、下牙设给关节 22（下颌）——**于是牙齿只受蒙皮驱动：上牙随头颈动，下牙随张嘴动**。这是「蒙皮权重决定顶点归属」最生动的例子。

#### 4.2.4 代码实践

本实践同样不依赖资产，只需要 torch（示例代码）：

**目标**：用 4.2.2 的手算例子验证 `batch_rigid_transform` 的输出，确认你真的理解了 A 的语义。

1. 实践目标：验证「父转子不转时关节自身位置不变」「孙关节被带到位」「A_j 作用在关节自身 rest 位置上得到 posed 关节位置」三条性质。
2. 操作步骤：新建 `test_fk.py`：

```python
# 示例代码
import torch
from models.modules.flame.lbs import batch_rigid_transform

Rz = torch.tensor([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])   # 绕 z 轴 90°
rot_mats = torch.stack([torch.eye(3), Rz, torch.eye(3)])[None]   # [1,3,3,3]
joints = torch.tensor([[[0., 0., 0.], [0., 1., 0.], [0., 2., 0.]]])  # [1,3,3]
parents = torch.tensor([-1, 0, 1])

posed_joints, A = batch_rigid_transform(rot_mats, joints, parents)
print("posed_joints =", posed_joints[0])
print("A_1 作用在 J_1 上 =", (A[0, 1] @ torch.cat([joints[0, 1], torch.ones(1)]))[:3])
print("A_2 的旋转块 =", A[0, 2, :3, :3])
```

3. 需要观察的现象：`posed_joints` 第 0、1 行应为 (0,0,0) 与 (0,1,0)（关节 1 自身不动），第 2 行应为 (-1,1,0)（关节 2 被带到左上方）；`A_1 @ [J_1;1]` 应等于 posed_joints[1]，即权重全给关节 1 的顶点会精确落在 posed 关节上；`A_2` 的旋转块应等于 \(R_z\)（全局旋转 = 父链旋转之积）。
4. 预期结果：三条全部吻合，说明 4.2.2 的推导正确。**待本地验证**。
5. 思考题（不改代码）：如果把 parents 改成 `[-1, 0, 0]`（关节 2 直接挂在根上），posed_joints[2] 会变成什么？（答案：(0,2,0)，关节 2 不再受关节 1 旋转影响——parents 表就是骨骼拓扑。）

#### 4.2.5 小练习与答案

**练习 1**：`rel_joints[:, 1:] -= joints[:, parents[1:]]` 这一行为什么不会对 root 报错（parents[0] = -1 是负索引）？

**答案**：切片 `parents[1:]` 从下标 1 开始，root 的 -1 根本不参与索引；root 的 rel_joints 保持其绝对位置，作为整棵树的基准。

**练习 2**：`batch_rigid_transform` 的 for 循环为什么不能像矩阵乘法那样一步并行？如果一定要减少串行深度，可以怎么做？

**答案**：链式数据依赖——G_i 必须等 G_{parent(i)} 算完。但依赖图是树不是链：同一深度的兄弟关节（例如左右手各 15 个指关节）互相独立，可以按「树层级」分批并行（先算所有深度 1 的节点，再深度 2……），串行深度从 O(J) 降为 O(树高)。对本项目 55 个关节的规模，收益不值得引入复杂性。

**练习 3**：posed_joints 取的是 `transforms[:, :, :3, 3]`（G 的平移列）。请用两关节例子说明它为什么恰好等于「摆好姿势后的关节位置」。

**答案**：G_j 的构造是 \(G_j = G_{parent}\cdot[[R_j,\, rel_j],[0,1]]\)，归纳可得其平移列 \(t_j = t_{parent} + R^{g}_{parent}\cdot rel_j\)，即「父的 posed 位置 + 在父坐标系里转完的相对偏移」，这正是 posed 关节的递归定义（见 4.2.2 手算例）。

### 4.3 lbs_wobeta 与 lbs_get_transform：PEAR 的两个变体

#### 4.3.1 概念说明

`lbs_wobeta`（wobeta = **w**ith **o**ut **beta**，不吃 β）和 `lbs_get_transform` 都是对经典 `lbs` 的接口改造，动机来自 EHM_v2 的换头流程（u4-l4 的主菜，这里先按下不表，只看 LBS 视角）：

- 经典 `lbs` 的第一步是「模板 + shape blend」。但 EHM_v2 在身体侧的流程是：**先**做 shape 化（[EHM_v2.py:L139](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L139)），**然后**在这份 `new_template_vertices` 上做 FLAME 头替换（L153-L158），**最后**才蒙皮。蒙皮时如果再走一遍 `v_template + blend_shapes(...)`，不仅 shape 被加了两次，换头结果也会被模板直接覆盖。所以需要一个「接受任意已变形模板、只做第②③④⑤步」的版本——这就是 `lbs_wobeta`。
- `lbs_get_transform` 则是反向裁剪：只要第①②④步的 **A 与 posed joints**，不要 pose blend、不要蒙皮、不要顶点。它还多开一个 `joints=None` 入口，允许外部直接注入关节位置跳过关节回归。这类「只要变换矩阵」的接口服务于「把随身穿戴的附加物（顶点级的 UV 点、法线、外部资产）随身体姿态一起变换」的场景。

一句话区分三者：

| 函数 | 输入 | 做哪几步 | 输出 |
| --- | --- | --- | --- |
| `lbs` | betas + 原始模板 | ①②③④⑤ 全做 | verts, J_tr, J, T, A |
| `lbs_wobeta` | 已 shape 化/已改造的模板 | ②③④⑤（跳过①） | verts, J_tr, J, T, A |
| `lbs_get_transform` | betas + 模板，或外部 joints | ①②④（跳过③⑤） | A, J_transformed |

#### 4.3.2 核心流程

`lbs_wobeta` 与 `lbs` 在数学上**完全等价**，只是把「shape 化」从函数内挪到了调用方：

```text
路线 A（经典）: verts = lbs(betas, pose, v_template, ...)
路线 B（拆分）: v_shaped = v_template + blend_shapes(betas, shapedirs)
                verts = lbs_wobeta(pose, v_shaped, ...)
结论: 若 v_shaped 按同样公式计算，两条路线逐位一致（最大误差 0 或浮点舍入量级）
```

适用场景的差异才是重点：**只要你需要在「shape 化之后、蒙皮之前」对模板做额外修改（换头、加顶点、外部位移），就必须走路线 B**。EHM_v2 的身体分支是路线 B，FLAME 分支（模板没有被外部改造，见图 4.3.3 第一条）用路线 A——同一个 forward 里两个分支各取所需，这是很好的对照。

`lbs_get_transform` 的流程：

```text
joints 参数为 None → 内部做 shape 化 + 关节回归（同 lbs 的①②）
joints 参数非 None → 直接用外部关节位置
→ batch_rodrigues（若 pose2rot）→ batch_rigid_transform
→ 返回 (A, J_transformed)，不产出任何顶点
```

#### 4.3.3 源码精读

**EHM_v2 的三个调用点**（这是理解两个变体为什么存在的最好上下文）：

1. FLAME 分支走经典 `lbs`：[EHM_v2.py:L71-L76](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L71-L76)。betas 是 cat(shape 300, expression 50)，full_pose 15 维（global/neck/jaw/双眼各 3、6，其中 global 与 neck 已被 u4-l2 讲过的 `torch.zeros_like` 置零）。头模板没有外部改造，所以 shape blend 留在函数内做。解包顺序 `(head_vertices, head_joints, J, T, A)` 对应 flame 版 `lbs` 的返回序 `(verts, J_transformed, J, T, A)`（[L230](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L230)）。
2. 身体分支走两段式：[EHM_v2.py:L137-L140](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L137-L140) 手工 `blend_shapes` 得到 `new_template_vertices`（注释「已经做了 shape 变换（beta）」），[L153-L158](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L153-L158) 在它上面按 `smplx2flame_ind` 把 FLAME 头嵌进去，然后 [L160-L163](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L160-L163) 调 `lbs_wobeta` 完成蒙皮。注意 L145-L148 有一段被注释掉的「用 lbs 做shape 化」旧代码——**这就是 lbs_wobeta 诞生的化石证据**：作者先写了两段式，再把它固化成函数。
3. `lbs_get_transform` 只被 [EHM_v2.get_transform_mat 的 L264-L267](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L264-L267) 调用。该方法（L215-L268）还做了一堆左右手旋转矩阵转轴角的镜像处理（`left_hand_pose[:,1::3] *= -1` 等，为的是把 SMPL-X 旋转约定转成另一套模型约定）。全仓库搜索确认：`get_transform_mat` 自身没有任何调用方——它是**备而未用**的接口（EHM.py 里有同款），价值在于告诉你「PEAR 的作者预留了逐关节变换矩阵的出口」。

**`lbs_wobeta` 源码**：[models/modules/flame/lbs.py:L258-L338](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L258-L338)。逐行对比经典 `lbs`：签名少了 `betas` 与 `v_template`，多了 `v_shaped`（[L258-L259](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L258-L259)）；[L297-L299](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L297-L299) 直接从 `v_shaped` 回归关节（跳过 shape blend 这一步）；其余 pose blend（L300-L319）、前向运动学 + 蒙皮（L320-L336）与 `lbs` 逐行相同；返回五元组顺序也与 flame 版 `lbs` 一致（[L338](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L338)）。EHM_v2 拿到的 `ver_transform_mat`（逐顶点 T，形状 (B,V,4,4)）与 `joint_transform_mat`（逐关节 A，形状 (B,J,4,4)）随输出字典返回（[EHM_v2.py:L204-L209](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L204-L209)），供需要逐顶点空间变换的下游复用——比如把绑定在模板顶点上的任何附属量（UV 采样点、法线、外部贴附物）随姿态整体变换。

**`lbs_get_transform` 源码**：[models/modules/flame/lbs.py:L232-L253](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L232-L253)。三个要点：`joints is None` 才做 shape 化与关节回归（L236-L242），否则直接采用外部关节；不做 pose blend（没有 posedirs 相关代码）；L253 只返回 `(A, J_transformed)`。

**返回值顺序的坑**：flame 版 `lbs` 返回 `(verts, J_transformed, J, T, A)`（T 在前，[L230](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L230)）；而 smplx 版 `lbs` 返回 `(verts, J_transformed, J, A, T)`（**A 在前**，[models/modules/smplx/lbs.py:L235](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/lbs.py#L235)）。两个同名函数、同样五个返回值、顺序不同——如果你混用两个 import 路径（比如 `from models.modules.smplx.lbs import lbs` 又按 EHM_v2 的顺序解包），T 和 A 会**静默对调**，不报错但结果全错（T 是 (B,V,4,4)，A 是 (B,J,4,4)，V≠J 时形状会在下游某处爆炸，V=J 时更糟——直接算错）。SMPLX.forward 按 smplx 版顺序解包并命名 `joints_transform_mat=A, verts_transform_mat=T`（[SMPLX.py:L384-L387](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L384-L387)），EHM_v2 按 flame 版顺序解包，各自正确——但你自己写代码时必须查清楚 import 的是哪一份。另有两处小分歧：smplx 版 `batch_size` 取 `max(betas, pose)`（见练习 4.1-3）；smplx 版 `find_dynamic_lmk_idx_and_bcoords` 多了 `pose.detach()`、kin chain 参数名叫 `head_kin_chain`（flame 版叫 `neck_kin_chain`）。

#### 4.3.4 代码实践

本实践用**合成小模型**验证「两条路线数学等价」，不依赖 SMPL-X/FLAME 资产（示例代码）：

**目标**：亲手证明 `lbs_wobeta` 不是新算法，而是接口拆分；并体会在「shape 化之后要动手脚」的场景下它不可替代。

1. 实践目标：对比路线 A/B 输出顶点的最大误差范数；再在路线 B 的 `v_shaped` 上人为扰动 10 个顶点，观察 `lbs` 是否无法表达这种输入。
2. 操作步骤：新建 `test_wobeta.py`：

```python
# 示例代码
import torch
from models.modules.flame.lbs import lbs, lbs_wobeta, blend_shapes

torch.manual_seed(0)
B, V, J, NB = 2, 200, 6, 10
v_template = torch.randn(V, 3)
shapedirs  = torch.randn(V, 3, NB) * 0.01
posedirs   = torch.randn((J - 1) * 9, V * 3) * 0.001
J_regressor = torch.softmax(torch.randn(J, V), dim=1)   # 行和为 1：关节是顶点的凸组合
parents     = torch.tensor([-1, 0, 0, 1, 2, 3])
lbs_weights = torch.softmax(torch.randn(V, J), dim=1)   # 行和为 1：合法蒙皮权重
betas = torch.randn(B, NB)
pose  = torch.randn(B, J, 3) * 0.5

# 路线 A：经典 lbs（内部 shape 化）
verts_a, Jtr_a, J_a, T_a, A_a = lbs(betas, pose, v_template, shapedirs, posedirs,
                                    J_regressor, parents, lbs_weights)
# 路线 B：先手工 shape 化，再 lbs_wobeta
v_shaped = v_template + blend_shapes(betas, shapedirs)
verts_b, Jtr_b, J_b, T_b, A_b = lbs_wobeta(pose, v_shaped, posedirs,
                                           J_regressor, parents, lbs_weights)
print("路线 A/B 顶点最大误差范数:", (verts_a - verts_b).norm(dim=-1).max().item())

# 模拟「换头」：路线 B 允许在蒙皮前改模板
v_patched = v_shaped.clone()
v_patched[:, :10] += torch.tensor([0.0, 0.1, 0.0])   # 挪动 10 个顶点
verts_p, *_ = lbs_wobeta(pose, v_patched, posedirs, J_regressor, parents, lbs_weights)
print("被改动的 10 个顶点位移量:", (verts_p[:, :10] - verts_b[:, :10]).norm(dim=-1).mean().item())
```

3. 需要观察的现象：第一个打印应为 0（两条路线的浮点运算顺序完全相同）或 1e-7 量级；第二个打印应显著非零，且这 10 个顶点的邻居也会有位移（蒙皮是逐顶点变换，不扩散——但被挪动的顶点自身会带着新位置被 A 变换到姿态空间）。
4. 预期结果：确认等价性成立、扰动可表达。**待本地验证**。
5. 进一步思考：路线 A 有没有任何参数能表达「v_shaped 的前 10 个顶点被外部改过」？没有——`lbs` 的输入只有原始模板与 β，这正是 `lbs_wobeta` 存在的全部理由。

#### 4.3.5 小练习与答案

**练习 1**：EHM_v2 的 FLAME 分支（L73）为什么可以用经典 `lbs`，身体分支（L160）却必须用 `lbs_wobeta`？

**答案**：FLAME 分支的输入模板是 FLAME 原始模板、没有外部改造，shape blend 可以留在函数内做；身体分支的模板在蒙皮前经历了「shape 化 + 按 `smplx2flame_ind` 换头」两道外部修改，若再走经典 `lbs` 会把 β 加第二次、且用原始模板覆盖换头结果。

**练习 2**：`lbs_get_transform` 与 `lbs_wobeta` 的本质区别是什么？它当前在仓库里的处境如何？

**答案**：方向相反的裁剪——`lbs_wobeta` 是「砍掉输入端（shape blend）、保住输出端（顶点）」；`lbs_get_transform` 是「保住输入端、砍掉输出端（pose blend、蒙皮、顶点），只留 A 与 posed joints」，并支持外部注入 joints。它只被 `EHM_v2.get_transform_mat`（L215-L268）调用，而后者在全仓库没有任何调用方，属于备而未用的接口。

**练习 3**：写一行会「静默出错」的解包代码，并解释原因。

**答案**：`from models.modules.smplx.lbs import lbs` 之后写 `verts, Jtr, J, T, A = lbs(...)`。smplx 版实际返回序是 `(verts, J_transformed, J, A, T)`，于是变量 `T` 装的是 A、`A` 装的是 T。形状为 (B,V,4,4) 与 (B,J,4,4) 的张量被对调，下游若恰好按 4×4 矩阵用（如逐关节广播），错误不会立刻暴露。规避方法：解包前查该文件 `return` 行，或统一只从 `models/modules.flame.lbs` import。

## 5. 综合实践

现在做本讲的正式实践任务（对应大纲 practice_task）：**用真实 EHM_v2 资产对比 `lbs` 与 `lbs_wobeta` 的输出误差，并画出 55 关节的 parents 树**。前置条件：u1-l2 完成 assets/ 下 SMPL-X 与 FLAME 模型文件准备、pear 环境可用（EHM_v2 的 import 链需要 pytorch3d）。

**实践目标**：把 4.1-4.3 的三块知识串成一条线——在真实 55 关节、10595 顶点的 SMPL-X（add_teeth=True 默认开，10475 原始 + 120 牙齿，u4-l1）上验证等价性，再用 parents 树把「前向运动学发生在什么结构上」可视化。

**操作步骤**：在仓库根目录新建 `lbs_practice.py`（示例代码）：

```python
# 示例代码：python lbs_practice.py
import torch
from models.modules.ehm import EHM_v2                    # 与 inference_wo_detect.py 同款 import
from models.modules.flame.lbs import lbs, lbs_wobeta, blend_shapes

torch.manual_seed(0)
ehm = EHM_v2("assets/FLAME", "assets/SMPLX").eval()       # 默认 add_teeth=True
smplx = ehm.smplx
parents = smplx.parents
print("关节数:", len(parents), " 模板顶点数:", smplx.v_template.shape[0])

B = 1
betas = torch.randn(B, smplx.shapedirs.shape[-1]) * 0.5  # 350 = 300 shape + 50 exp
template = smplx.v_template.unsqueeze(0)

# 健全性检查：pose 全零 → 蒙皮前后顶点不变（练习 4.1-1 的实验版）
pose0 = torch.zeros(B, len(parents), 3)
v_shaped0 = template + blend_shapes(betas, smplx.shapedirs)
verts0, *_ = lbs_wobeta(pose0, v_shaped0, smplx.posedirs,
                        smplx.J_regressor, parents, smplx.lbs_weights)
print("pose 全零时的最大偏移:", (verts0 - v_shaped0).norm(dim=-1).max().item())

# 正式对比：同一组随机 pose，路线 A vs 路线 B
pose = torch.randn(B, len(parents), 3) * 0.3             # 轴角
verts_a, Jtr_a, J_a, T_a, A_a = lbs(betas, pose, template, smplx.shapedirs,
                                    smplx.posedirs, smplx.J_regressor,
                                    parents, smplx.lbs_weights)
v_shaped = template + blend_shapes(betas, smplx.shapedirs)
verts_b, Jtr_b, J_b, T_b, A_b = lbs_wobeta(pose, v_shaped, smplx.posedirs,
                                           smplx.J_regressor, parents,
                                           smplx.lbs_weights)
print("lbs vs lbs_wobeta 顶点最大误差范数:",
      (verts_a - verts_b).norm(dim=-1).max().item())
print("T 的形状:", tuple(T_a.shape), " A 的形状:", tuple(A_a.shape))

# parents 关节树：缩进文本画法（无需 graphviz）
children = {i: [] for i in range(len(parents))}
for i, p in enumerate(parents.tolist()):
    if p >= 0:
        children[p].append(i)
def show(i, depth=0):
    print("    " * depth + str(i))
    for c in sorted(children[i]):
        show(c, depth + 1)
show(0)
```

**需要观察的现象与预期结果**：

1. `关节数: 55`、`模板顶点数: 10595`（EHM_v2.py 内注释写的 10475 是加牙前的过时注释，u4-l1 已澄清）。
2. 「pose 全零时的最大偏移」应为 0 或 1e-7 量级——这是 LBS 实现的恒等性健全检查。
3. 「顶点最大误差范数」应为 0（两条路线的 `v_shaped` 计算完全同序）或浮点舍入量级（1e-6 以内）。若出现米级差异，说明你的两路线输入没有对齐（最常见：betas 维度写成 200 而不是 350）。
4. `T (1, 10595, 4, 4)`、`A (1, 55, 4, 4)`——逐顶点矩阵数量等于顶点数，逐关节矩阵数量等于关节数。
5. parents 树应呈现：根 0 分出双腿与脊柱链；沿脊柱向上到 12（颈）再 15（头），15 下挂 22（下颌）、23/24（双眼）；两只手臂链的末端（20/21 腕关节）各挂 15 个手指关节；树的最大深度出现在手指链上。把 12、15、22、23、24 这几个关键编号与 4.2.3 的证据互相印证。

第 1、2、5 项依赖 assets 与环境，**待本地验证**。

## 6. 本讲小结

- 经典 `lbs` 五步流水线：shape blend（`v_template + blend_shapes`）→ 关节回归（`vertices2joints`，J_regressor 是顶点到关节的凸组合）→ pose blend（posedirs 作用于 R−I，根关节不参与）→ 前向运动学（`batch_rigid_transform`）→ 蒙皮混合（W·A 逐顶点变换）。
- `batch_rigid_transform` 的核心是沿 parents 树链乘局部变换 \(G_j = G_{parent(j)}\cdot[[R_j, J_j-J_{parent(j)}],[0,1]]\)；posed 关节 = G 的平移列；蒙皮矩阵 A 由「减法技巧」\(A = G - \mathrm{pad}(G\cdot[J;0])\) 得到，几何含义是 \(R^g_j(v-J_j)+posed_j\)。`F.pad` 默认填 0 而非 1 是这个技巧成立的隐蔽前提。
- `lbs_wobeta` 与经典 `lbs` 数学等价，只是把 shape 化拆给调用方——为 EHM_v2「shape 化 → 换头 → 蒙皮」的两段式流程而生；EHM_v2.py L145-L148 被注释的旧代码是它演化的化石。
- `lbs_get_transform` 只产出 A 与 posed joints（可注入外部 joints、不做 pose blend），当前经由无人调用的 `get_transform_mat` 备而未用。
- 仓库有三份 `lbs.py`，推理链路只走 `models/modules/flame/lbs.py`；flame 版与 smplx 版同名 `lbs` 的返回值 T/A 顺序相反，混用 import 会静默出错。
- 牙齿是理解 LBS 张量语义的绝佳案例：shape/pose 基底与 J_regressor 补零、蒙皮权重手写挂到颈（12）与下颌（22），于是牙齿完全由蒙皮驱动实现开合。

## 7. 下一步学习建议

本讲补完了 u4-l2 留下的最后一块数学地基。下一讲 **u4-l4（EHM_v2：用 FLAME 头替换 SMPL-X 头的统一表达）** 会把本讲的 `lbs_wobeta` 调用放回完整上下文：`selected_head` 如何用「FLAME 双眼中点（`head_joints[:, 3:5]` 均值）→ SMPL-X 双眼中点（`tbody_joints[:, 23:25]` 均值）」平移对齐（你现在知道这两个索引为什么是 3:5 和 23:25 了——它们是两棵 parents 树上「眼睛」关节的编号）、`non_head_index` 如何保住颈部缝合边界、以及换头后的模板如何进入本讲的 `lbs_wobeta`。读 u4-l4 之前建议先跑完第 5 节综合实践——带着自己打印出来的 parents 树去读换头代码，关节索引会全部「活」起来。之后再进 u4-l5 的渲染链路，本讲的输出 `vertices (B,10595,3)` 正是渲染器的输入。
