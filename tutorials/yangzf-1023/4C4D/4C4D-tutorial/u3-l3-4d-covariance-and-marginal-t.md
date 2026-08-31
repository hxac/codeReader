# 讲义 u3-l3：4D 协方差与时间边缘化

## 1. 本讲目标

上一讲（u3-l2）我们给高斯补上了第四维：时间中心 `_t`、时间尺度 `_scaling_t`、时间右四元数 `_rotation_r`。但屏幕是二维的、世界是三维的——**渲染某一帧时，一个「活在四维里的椭球」如何变成一个「这一时刻的三维椭球」？** 这正是本讲要回答的问题。

学完本讲你应该能够：

1. **推导**（而不只是记住）条件高斯公式：给定 4D 协方差 \(\Sigma\)，在固定时刻 \(\tau\) 处的 3D 条件协方差是 \(\Sigma_{xx}-\Sigma_{xt}\Sigma_{tx}/\sigma_t^2\)，均值偏移是 \(\Sigma_{xt}/\sigma_t^2\cdot(\tau-\mu_t)\)。
2. 读懂 `build_scaling_rotation_4d` 如何用**双四元数**组装 4×4 矩阵 \(L\)，以及 \(\Sigma = LL^{\mathsf T}\) 的由来。
3. 解释 `get_marginal_t` 的时间高斯衰减 \( \exp\left(-\frac{(\tau-\mu_t)^2}{2\sigma_t^2}\right) \) 的物理含义，以及代码里那个魔数 `0.05` 从哪来。
4. 说出 `mean_offset` 为什么必须加到投影中心上，漏掉它会出现什么错误。
5. 注意到 `rot_4d` 开与关时 `_scaling_t` 的**语义并不一致**这一陷阱。

## 2. 前置知识

### 2.1 条件高斯分布（本讲唯一的数学前置）

把一个四维随机变量切成两块：空间部分 \(x\in\mathbb R^3\) 和时间部分 \(t\in\mathbb R\)。它的协方差矩阵也切成四块：

\[
\Sigma=\begin{pmatrix}\Sigma_{xx}&\Sigma_{xt}\\ \Sigma_{tx}&\sigma_t^2\end{pmatrix},\qquad
\mu=\begin{pmatrix}\mu_x\\ \mu_t\end{pmatrix}
\]

其中 \(\Sigma_{xx}\) 是 3×3、\(\Sigma_{xt}\) 是 3×1、\(\sigma_t^2\) 是标量（时间方差）。「我已知时刻 \(t=\tau\)，问空间部分长什么样」就是**条件分布** \(p(x\mid t=\tau)\)。对联合高斯做配平方（或用分块矩阵求逆），结果是仍是高斯：

\[
p(x\mid t=\tau)=\mathcal N\!\left(x;\ \mu_x+\frac{\Sigma_{xt}}{\sigma_t^2}(\tau-\mu_t),\ \ \Sigma_{xx}-\frac{\Sigma_{xt}\Sigma_{tx}}{\sigma_t^2}\right)
\]

两个式子各有名字：

- 均值修正项 \(\frac{\Sigma_{xt}}{\sigma_t^2}(\tau-\mu_t)\)：**如果椭球在时空里是斜的**（空间和时间相关，\(\Sigma_{xt}\neq 0\)），在 \(\tau\) 时刻切开它，切面中心不再位于 \(\mu_x\)，而是沿着时空相关方向平移。
- 协方差收缩项 \(\Sigma_{xx}-\frac{\Sigma_{xt}\Sigma_{tx}}{\sigma_t^2}\)：这是 \(\Sigma\) 的 **Schur 补**。斜的椭球被「压扁」到超平面上，切面在相关方向上会比原 \(\Sigma_{xx}\) 更窄。

直觉图像：想象一根斜放在四维里的雪茄，\(x\) 与 \(t\) 强相关（\(t\) 变大时 \(x\) 也变大）。固定 \(t=\tau\) 切一刀，切面是个椭圆，椭圆中心相对 \(\mu_x\) 挪了一段距离，且在最斜的方向上比雪茄自身的横截面更瘦。**这两件事在代码里分别叫 `mean_offset` 和 `current_covariance`。**

### 2.2 为什么要「条件」而不是「拍扁」

另一种把 4D 变 3D 的办法是把时间积掉（边缘化 \(p(x)=\int p(x,t)\,dt\)）。对高斯来说这只需丢掉 \(\Sigma\) 的时间行列，均值和 \(\Sigma_{xx}\) 都不变——但它会把「先在 A 处、后到 B 处」的运动模糊成「同时在 A 和 B 处」。4DGS 的选择是：**渲染 \(\tau\) 时刻就取条件分布 \(p(x\mid \tau)\)，再乘上时间衰减因子**，等价于直接在超平面 \(t=\tau\) 上求值联合密度 \(p(x,\tau)\)（见 4.3.1 的推导）。这样每个高斯在每一帧都是一个清晰的三维椭球，位置和大小随 \(\tau\) 平滑变化。

### 2.3 复习：3D 高斯的协方差是怎么来的

u3-l1 讲过 `get_covariance`：3D 路径里 \(L=D R\)（\(D=\mathrm{diag}(s)\) 缩放、\(R\) 旋转），\(\Sigma = L^{\mathsf T}L\)。本讲的 4D 版本结构完全一样，只是 \(R\) 变成 4×4、四元数从一个变两个，且乘法顺序换成 \(\Sigma = LL^{\mathsf T}\)（原因见 4.1.3 的说明）。

## 3. 本讲源码地图

| 文件 | 本讲涉及的内容 |
| --- | --- |
| [scene/gaussian_model.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py) | `setup_functions` 里定义的两个 `build_covariance_*` 闭包（本讲主角）、`get_cov_t`、`get_marginal_t`、`get_current_covariance_and_mean_offset` |
| [utils/general_utils.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py) | `build_rotation_4d`（双四元数→4×4 旋转）、`build_scaling_rotation_4d`（组装 \(L\)）、`strip_symmetric`（对称矩阵只留 6 个数） |
| [gaussian_renderer/__init__.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py) | 消费端：`compute_cov3D_python` 调试回退里如何用条件协方差与 `mean_offset` |
| [diff-gaussian-rasterization/cuda_rasterizer/forward.cu](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu) | CUDA 侧逐行镜像：`computeCov3D_conditional` 用同一套 Schur 公式 |
| [diff-gaussian-rasterization/cuda_rasterizer/backward.cu](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu) | 反向传播中 `marginal_t` 的梯度如何回传到 `cov_t` |

一句提醒：本讲出现的 Python 工具函数（`build_scaling_rotation_4d`、`strip_symmetric`）**把张量硬编码创建在 `device="cuda"` 上**，所以纯 CPU 环境无法直接调用它们；本讲的实践任务会同时给出「CPU 可跑的手工验证」与「GPU 上调用真函数对比」两条路。

## 4. 核心概念与源码讲解

本讲按数据流拆成四个模块：**组装 \(L\)（4.1）→ Schur 补切出条件协方差（4.2）→ 时间衰减因子（4.3）→ 渲染侧消费与 CUDA 镜像（4.4）**。

### 4.1 `build_scaling_rotation_4d`：用双四元数组装 4×4 矩阵 L

#### 4.1.1 概念说明

3D 旋转用 1 个单位四元数就够了。4D 旋转（SO(4)）不行——它需要**两个**单位四元数 \((l, r)\)，对应映射 \(x \mapsto l\,x\,r\)（或 \(l\,x\,\bar r\)，取决于约定）。这就是 u3-l2 里 `_rotation`（左四元数）与 `_rotation_r`（右四元数）成对出现的原因：**双四元数 = 4D 旋转的极简参数化**，8 个数（去冗余后 6 个自由度）正好覆盖 4D 旋转的自由度，且天然可微、易于优化。

`build_scaling_rotation_4d` 的职责一句话：**给每个高斯造一个 4×4 矩阵 \(L = R\,\mathrm{diag}(s)\)，使 \(\Sigma = LL^{\mathsf T}\) 就是该高斯的 4D 协方差**。这里 \(s\) 是激活后的 4 维尺度（3 空间 + 1 时间，即 `get_scaling_xyzt`），\(R\) 是双四元数给出的 4D 旋转。

#### 4.1.2 核心流程

```
输入: s (N,4) 已激活的 xyzt 尺度, l (N,4) 左四元数, r (N,4) 右四元数
  1. 归一化 l, r 得单位四元数 q_l, q_r          # build_rotation_4d 内部完成
  2. 用 q_l 的 (a,b,c,d) 堆出 4×4 矩阵 M_l      # 四元数左乘的实矩阵表示
  3. 用 q_r 的 (p,q,r,s) 堆出 4×4 矩阵 M_r      # 右乘矩阵的一个变体
  4. A = M_l @ M_r, 再 A = A.flip(1,2)          # 得到 4D 旋转矩阵 R
  5. L = zeros(N,4,4); L[i,i] = s[i]            # 对角缩放矩阵 D
  6. 返回 L = R @ D                              # 注意顺序：旋转在左
```

#### 4.1.3 源码精读

**双四元数 → 4×4 旋转矩阵**，[utils/general_utils.py:113-133](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py#L113-L133)：

```python
def build_rotation_4d(l, r):
    l_norm = torch.norm(l, dim=-1, keepdim=True)
    r_norm = torch.norm(r, dim=-1, keepdim=True)

    q_l = l / l_norm
    q_r = r / r_norm

    a, b, c, d = q_l.unbind(-1)
    p, q, r, s = q_r.unbind(-1)

    M_l = torch.stack([a,-b,-c,-d,
                       b, a,-d, c,
                       c, d, a,-b,
                       d,-c, b, a]).view(4,4,-1).permute(2,0,1)
    M_r = torch.stack([ p, q, r, s,
                       -q, p,-s, r,
                       -r, s, p,-q,
                       -s,-r, q, p]).view(4,4,-1).permute(2,0,1)
    A = M_l @ M_r
    A = A.flip(1,2)
    return A
```

这段代码做了什么：第 114-118 行先把左右四元数归一化（所以调用方传入**未激活的裸参数** `_rotation`/`_rotation_r` 也没关系，归一化在这里补）；第 123-130 行用四元数四个分量按固定排布堆出两个 4×4 实矩阵——`M_l` 正是「用 \(q_l\) 做四元数左乘」的实矩阵表示（对比 [diff-gaussian-rasterization/cuda_rasterizer/forward.cu:301-306](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L301-L306) 被注释掉的那份布局，逐字相同）；第 131-132 行相乘后做 `flip(1,2)`（沿两个维度同时翻转）。

两个诚实的提醒，免得读者在这 20 行里迷路：

- `M_r` 的排布**不是**教科书里标准的四元数右乘矩阵，而是它的一种变体，再配合 `A.flip(1,2)` 一起构成一套自洽的约定。这套约定是作者与 CUDA 侧对齐调出来的（`forward.cu:301-327` 保留了三份被注释/在用的布局可供对照）。**读代码时请把 `build_rotation_4d(l, r)` 当作一个整体——「双四元数参数化的 4D 正交旋转」——而不必手工推导每个元素。**
- 不论约定细节如何，产物 \(R\) 必须满足正交性 \(R^{\mathsf T}R=I\)、\(\det R=+1\)，这是 4.1.4 实践要验证的性质。

**缩放与旋转拼装**，[utils/general_utils.py:135-145](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py#L135-L145)：

```python
def build_scaling_rotation_4d(s, l, r):
    L = torch.zeros((s.shape[0], 4, 4), dtype=torch.float, device="cuda")
    R = build_rotation_4d(l, r)

    L[:,0,0] = s[:,0]
    L[:,1,1] = s[:,1]
    L[:,2,2] = s[:,2]
    L[:,3,3] = s[:,3]

    L = R @ L
    return L
```

这段代码做了什么：先把 4 个尺度放进对角矩阵 \(D\)，再左乘 4D 旋转，返回 \(L = R D\)，形状 `(N, 4, 4)`。注意它**硬编码 `device="cuda"`**（第 136 行），这就是纯 CPU 环境调不动它的原因。

与 3D 版本对比一个有意思的不对称，[utils/general_utils.py:102-111](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py#L102-L111) 中 3D 是 `L = L @ R`（即 \(D R\)，缩放在左），而 4D 是 \(R D\)（缩放在右）。相应地，[scene/gaussian_model.py:31](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L31) 的 3D 协方差用 \(\Sigma = L^{\mathsf T}L\)，而 [scene/gaussian_model.py:37](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L37) 的 4D 协方差用 \(\Sigma = LL^{\mathsf T}\)。两种写法得到的都是「旋转后的各向异性椭球」，只是 \(R\) 与 \(R^{\mathsf T}\) 的约定差一次转置，两套各自与 CUDA 侧对应实现保持一致。读代码时不要假设两边公式逐字相同。

#### 4.1.4 代码实践

**实践目标**：验证 `build_rotation_4d` 的输出确实是正交旋转矩阵，且恒等四元数给出单位阵。

**操作步骤**：仓库版本硬编码 CUDA，所以在 CPU 上先用 numpy 逐行复刻它（正好检验你是否读懂了排布），GPU 机器上再调真函数对照。把下面脚本存为 `4C4D-tutorial/practice_u3l3_rot4d.py`（示例代码，本讲义新增，不属于原项目）：

```python
import numpy as np

def build_rotation_4d_np(l, r):
    """numpy 复刻 utils/general_utils.py:113-133，device 无关版"""
    q_l = l / np.linalg.norm(l, axis=-1, keepdims=True)
    q_r = r / np.linalg.norm(r, axis=-1, keepdims=True)
    a, b, c, d = q_l
    p, q, r_, s_ = q_r                       # 尾下划线避免与入参 r 撞名
    M_l = np.array([[a,-b,-c,-d],
                    [b, a,-d, c],
                    [c, d, a,-b],
                    [d,-c, b, a]])
    M_r = np.array([[ p, q, r_, s_],
                    [-q, p,-s_, r_],
                    [-r_, s_, p,-q],
                    [-s_,-r_, q, p]])
    return (M_l @ M_r)[::-1, ::-1]           # flip(1,2) == 两维同时反序

rng = np.random.default_rng(0)
l = rng.normal(size=4); r = rng.normal(size=4)
R = build_rotation_4d_np(l, r)
print("R^T R ≈ I ?", np.allclose(R.T @ R, np.eye(4), atol=1e-6))
print("det(R) =", np.linalg.det(R))
R_id = build_rotation_4d_np([1,0,0,0], [1,0,0,0])
print("恒等四元数 -> 单位阵 ?", np.allclose(R_id, np.eye(4)))
```

**需要观察的现象**：`R^T R ≈ I` 为 True、`det(R) ≈ 1.0`、恒等四元数确实得到单位阵。

**预期结果**：正交性与行列式都通过。若你把 `[::-1, ::-1]`（对应 `flip`）删掉再跑，`det` 可能变成 ±1 之间的另一个值或 `R^T R` 不再是单位阵——这能直观感受 flip 在这套约定里不是装饰。以上结论**待本地验证**（本讲义未实际运行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 3D 旋转 1 个四元数、4D 旋转要 2 个？
**答案**：3D 旋转群 SO(3) 由单位四元数（SU(2)）双重覆盖，1 个四元数足够；4D 旋转群 SO(4) 同构于 \(SU(2)\times SU(2)\) 的商，需要一对单位四元数 \((l,r)\)，映射形如 \(x\mapsto lxr\)。对应到模型就是 `_rotation`（左）与 `_rotation_r`（右）成对存在，且 `_rotation_r` 只在 `rot_4d=True` 时创建（见 [scene/gaussian_model.py:85](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L85)）。

**练习 2**：`build_scaling_rotation_4d` 里若不先归一化四元数会怎样？
**答案**：得到的 \(R\) 不再正交，\(\Sigma=LL^{\mathsf T}\) 的特征值会被非单位范数缩放，高斯的实际大小随四元数模长漂移，梯度也会被污染。归一化在 [utils/general_utils.py:114-118](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py#L114-L118) 内完成，因此 `get_current_covariance_and_mean_offset` 传入的是未激活的 `_rotation`/`_rotation_r` 裸参数（[scene/gaussian_model.py:259-263](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L259-L263)）。

---

### 4.2 `build_covariance_from_scaling_rotation_4d`：Schur 补切出条件协方差（本讲核心）

#### 4.2.1 概念说明

这是 `setup_functions` 里定义的闭包，也是整条「4D → 3D」变换的心脏。它解决的问题是：**渲染 \(\tau\) 时刻的帧时，把每个高斯的 4D 协方差 \(\Sigma\) 压成该时刻的 3D 条件协方差，同时算出切面中心的平移量。** 它只在 `rot_4d=True` 时被选为 `covariance_activation`（见 [scene/gaussian_model.py:53-56](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L53-L56)）。输出两样东西：

1. `symm`：条件协方差 \(\Sigma_{x|x}\) 压成 6 维向量（对称矩阵只存上三角）；
2. `mean_offset`：均值平移 \(\frac{\Sigma_{xt}}{\sigma_t^2}dt\)，形状 `(N, 3)`，调用方要把它加到 `_xyz` 上。

#### 4.2.2 核心流程

先把 2.1 的结论对齐到代码记号。代码把 4×4 的 \(\Sigma\) 分块为：

\[
\Sigma=\begin{pmatrix}
\underbrace{\Sigma_{00..22}}_{\texttt{cov\_11}\ (3\times3)} & \underbrace{\Sigma_{0..2,\,3}}_{\texttt{cov\_12}\ (3\times1)}\\[2pt]
\underbrace{\Sigma_{3,\,0..2}}_{\texttt{cov\_12}^{\mathsf T}} & \underbrace{\Sigma_{33}}_{\texttt{cov\_t}\ (1\times1)}
\end{pmatrix},\qquad dt=\tau-\mu_t
\]

于是条件分布的两条公式逐字对应：

\[
\texttt{current\_covariance}=\texttt{cov\_11}-\frac{\texttt{cov\_12}\,\texttt{cov\_12}^{\mathsf T}}{\texttt{cov\_t}},
\qquad
\texttt{mean\_offset}=\frac{\texttt{cov\_12}}{\texttt{cov\_t}}\cdot dt
\]

**这两条公式为什么成立（配平方推导梗概）**。联合密度指数项里的二次型用分块求逆展开，\(\Sigma^{-1}\) 的左上块恰是 Schur 补的逆：

\[
\left(\Sigma^{-1}\right)_{xx}=\left(\Sigma_{xx}-\frac{\Sigma_{xt}\Sigma_{tx}}{\sigma_t^2}\right)^{-1}
\]

把它代回 \((z-\mu)^{\mathsf T}\Sigma^{-1}(z-\mu)\)（\(z=(x,\tau)\)）并按 \(x\) 配平方，\(x\) 的二次项系数给出条件协方差，一次项系数给出条件均值 \(\mu_x+\frac{\Sigma_{xt}}{\sigma_t^2}(\tau-\mu_t)\)。也就是说：**代码做的事 = 在超平面 \(t=\tau\) 上对 4D 高斯做切片**。

还有一个关键细节：`cov_t` **不是** \((\text{时间尺度})^2\) 这么简单。因为 \(\Sigma=LL^{\mathsf T}\)、\(L=R\,\mathrm{diag}(s)\)、\(R\) 正交，所以

\[
\Sigma=R\,\mathrm{diag}(s)^2 R^{\mathsf T}
\quad\Longrightarrow\quad
\texttt{cov\_t}=\Sigma_{33}=\sum_{k=0}^{3}R_{3k}^{\,2}\,s_k^{\,2}
\]

当 4D 旋转把时间轴和空间轴耦合起来时，**有效时间方差是四个平方尺度的加权混合**——这正是 4D 旋转的表达力所在：一个高斯可以「沿时间方向斜着伸长」，此时它在某个时刻的切面中心会随 \(\tau\) 移动（运动！）、切面宽度也随之变化（形变！）。两个高斯若只有 `_xyz` 不同而没有时空耦合，就只是「静止在不同位置」；有了 \(R\) 的耦合，才有「移动的椭球」。

执行流程伪代码：

```
输入: scaling (N,4)=get_scaling_xyzt, scaling_modifier, rotation_l, rotation_r, dt (N,1)=timestamp-_t
  1. L = build_scaling_rotation_4d(modifier*scaling, rotation_l, rotation_r)   # R·D
  2. Σ = L @ L^T                                                              # (N,4,4)
  3. cov_11 = Σ[:, :3, :3];  cov_12 = Σ[:, 0:3, 3:4];  cov_t = Σ[:, 3:4, 3:4]
  4. 条件协方差 = cov_11 - cov_12 @ cov_12^T / cov_t
  5. symm = strip_symmetric(条件协方差)          # (N,6) 上三角
  6. mean_offset = (cov_12 / cov_t) * dt          # (N,3)
  7. 返回 (symm, mean_offset)
```

#### 4.2.3 源码精读

**闭包本体**，[scene/gaussian_model.py:35-48](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L35-L48)：

```python
def build_covariance_from_scaling_rotation_4d(scaling, scaling_modifier, rotation_l, rotation_r, dt=0.0):
    L = build_scaling_rotation_4d(scaling_modifier * scaling, rotation_l, rotation_r)
    actual_covariance = L @ L.transpose(1, 2)
    cov_11 = actual_covariance[:,:3,:3]
    cov_12 = actual_covariance[:,0:3,3:4]
    cov_t = actual_covariance[:,3:4,3:4]
    current_covariance = cov_11 - cov_12 @ cov_12.transpose(1, 2) / cov_t
    symm = strip_symmetric(current_covariance)
    if dt.shape[1] > 1:
        mean_offset = (cov_12.squeeze(-1) / cov_t.squeeze(-1))[:, None, :] * dt[..., None]
        mean_offset = mean_offset[..., None]  # [num_pts, num_time, 3, 1]
    else:
        mean_offset = cov_12.squeeze(-1) / cov_t.squeeze(-1) * dt
    return symm, mean_offset.squeeze(-1)
```

逐段说明：

- 第 36-37 行：组装 \(L=R D\) 并得 \(\Sigma=LL^{\mathsf T}\)，形状 `(N,4,4)`，与 4.1 的推导一致。
- 第 38-40 行：切出三个分块。注意 `cov_12` 保留成 `(N,3,1)` 而不是 `(N,3)`，这样第 41 行的矩阵乘除法形状天然对齐，无需广播技巧。
- 第 41 行：**Schur 补一行写完**。由于 `cov_t` 是 `(N,1,1)`，`/ cov_t` 是逐高斯的标量除法。
- 第 42 行：`strip_symmetric` 把对称 3×3 压成 6 维（[utils/general_utils.py:65-77](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py#L65-L77)，顺序为 \(\Sigma_{00},\Sigma_{01},\Sigma_{02},\Sigma_{11},\Sigma_{12},\Sigma_{22}\)），这是光栅化器要求的输入格式——3DGS 系列一贯只传上三角。
- 第 43-47 行：`mean_offset` 的两个分支。`dt = timestamp - self.get_t`（见下一条代码点）。当前两个入口都传**标量** `viewpoint_camera.timestamp`，广播后 `dt` 形状为 `(N,1)`，走 `else` 分支返回 `(N,3)`；`dt.shape[1] > 1` 的分支是为「一批时间戳同时求值」保留的（`timestamp` 为长度大于 1 的张量时触发，输出 `(N, num_time, 3)`），在现有主路径上不会触发（调用点见 [gaussian_renderer/__init__.py:87](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L87) 与 [gaussian_renderer/__init__.py:113](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L113)，两处均传标量）。

**入口封装**，[scene/gaussian_model.py:259-263](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L259-L263)：

```python
def get_current_covariance_and_mean_offset(self, scaling_modifier = 1, timestamp = 0.0):
    return self.covariance_activation(self.get_scaling_xyzt, scaling_modifier, 
                                                              self._rotation, 
                                                              self._rotation_r,
                                                              dt = timestamp - self.get_t)
```

这段代码做了什么：把激活后的 4 维尺度 `get_scaling_xyzt`（[scene/gaussian_model.py:201-203](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L201-L203)，即 `exp(cat([_scaling, _scaling_t]))`）、两个裸四元数和 \(dt=\tau-\_t\) 喂给闭包。**注意 `covariance_activation` 是按 `rot_4d` 而不是按 `gaussian_dim` 选择的**（[scene/gaussian_model.py:53-56](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L53-L56)）：`rot_4d=False` 时它绑定的是只有 3 个位置参数的 3D 版本，此时调用本方法会因多传参数直接 `TypeError`。渲染端因此只在 `rot_4d` 时才调用它（[gaussian_renderer/__init__.py:86-88](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L86-L88)）——这是一个容易被忽略的隐式契约。

**CUDA 侧逐行镜像**，[diff-gaussian-rasterization/cuda_rasterizer/forward.cu:331-351](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L331-L351)：

```cpp
glm::mat4 Sigma = glm::transpose(M) * M;
float cov_t = Sigma[3][3];
float marginal_t = __expf(-0.5*dt*dt/cov_t);
mask = marginal_t > 0.05;
if (!mask) return;
opacity*=marginal_t;;
glm::mat3 cov11 = glm::mat3(Sigma);
glm::vec3 cov12 = glm::vec3(Sigma[0][3],Sigma[1][3],Sigma[2][3]);
glm::mat3 cov3D_condition = cov11 - glm::outerProduct(cov12, cov12) / cov_t;
...
glm::vec3 delta_mean = cov12 / cov_t * dt;
p_orig.x+=delta_mean.x; ...
```

这段代码做了什么：这是 `preprocessCUDA` 中每个高斯在 GPU 上执行的 `computeCov3D_conditional`。可以逐行对上 Python 版：`cov11 - outerProduct(cov12,cov12)/cov_t` 就是第 41 行的 Schur 补，`delta_mean = cov12/cov_t*dt` 就是第 47 行的 `mean_offset`，并且**直接把 `delta_mean` 加进投影中心 `p_orig`**（第 348-351 行）——这就是 4.2.4 实践里 Python 回退路径要手工复刻的那一步。中间夹着的 `marginal_t` 计算属于 4.3 模块。真正训练时走的是这份 CUDA 代码；Python 闭包主要服务于 `compute_cov3D_python=True` 的调试回退路径，两者公式必须保持一致（反向传播在 [backward.cu:750-772](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L750-L772) 手工对 Schur 补求导，`dL_dcov12`、`dL_dcovt` 两段就是上面对 \( \Sigma_{xt} \) 与 \( \sigma_t^2 \) 的偏导数展开）。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：手工实现条件协方差与均值偏移公式，验证与 `build_covariance_from_scaling_rotation_4d` 的输出完全一致。

**操作步骤**：

1. 选定一组参数：尺度 `s = [0.5, 1.0, 0.3, s_t]`（取 `s_t = 1.2`）、非平凡左右四元数 `l = [1, 0.3, -0.2, 0.1]`、`r = [0.7, 0.2, 0.5, -0.4]`、渲染时刻 `\tau = 2.0`、时间中心 `\mu_t = 0.5`。
2. 在 CPU 上用 numpy 复刻 `build_rotation_4d`（4.1.4 已给出）得到 \(R\)，组装 \(\Sigma = R\,\mathrm{diag}(s)^2 R^{\mathsf T}\)。
3. **手工**算 Schur：`cov11 = Σ[:3,:3]`，`cov12 = Σ[:3,3:4]`，`cov_t = Σ[3,3]`，然后 `cond = cov11 - cov12 @ cov12.T / cov_t`，`offset = cov12 / cov_t * (τ - μ_t)`。
4. 在 GPU 机器上通过模型封装调用**真函数**对比（示例代码，本讲义新增；`GaussianModel` 构造参数含义见 u3-l1/u3-l2）：

```python
import torch
from scene import GaussianModel

pc = GaussianModel(sh_degree=3, gaussian_dim=4,
                   time_duration=[0., 10.], rot_4d=True)
# 直接填充属性，避免走 create_from_pcd（那会随机采样 _t，不便于对照）
pc._xyz = torch.zeros(1, 3, device="cuda")
pc._t   = torch.tensor([[0.5]], device="cuda")                       # mu_t
pc._scaling   = torch.log(torch.tensor([[0.5, 1.0, 0.3]], device="cuda"))
pc._scaling_t = torch.log(torch.tensor([[1.2]], device="cuda"))
pc._rotation   = torch.tensor([[1., 0.3, -0.2, 0.1]], device="cuda")  # 左四元数 l
pc._rotation_r = torch.tensor([[0.7, 0.2, 0.5, -0.4]], device="cuda") # 右四元数 r

symm, offset = pc.get_current_covariance_and_mean_offset(1.0, timestamp=2.0)
print("条件协方差(6维):", symm)        # 期望与第 3 步手工值一致
print("均值偏移(3维):", offset)        # 期望 = cov12/cov_t*(2.0-0.5)
```

第 3 步手工值里的 \(R\) 用 4.1.4 的 numpy 复刻计算（同一组 `l`、`r`），保证两边输入完全相同。

**需要观察的现象**：`symm` 与手工 `cond` 的上三角 6 元素逐项相等（误差在 float32 精度内，约 1e-6）；`offset` 等于手工 `cov12/cov_t*1.5`。再做一个退化检查：把 `l = r = [1,0,0,0]`（恒等旋转），此时 \(\Sigma_{xt}=0\)，应观察到 `offset` 全零、`symm` 恰为 `diag(0.5², 1.0², 0.3²)` 的上三角。

**预期结果**：两路完全一致；恒等旋转下偏移为零。以上数值**待本地验证**（本讲义编写环境无 GPU、也无法运行 python，未实际执行）。

#### 4.2.5 小练习与答案

**练习 1**：如果渲染端忘了 `means3D = means3D + delta_mean`（[gaussian_renderer/__init__.py:88](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L88)），图像会出什么问题？
**答案**：每个高斯的协方差被「斜切」收缩了，但中心仍停在 \(\mu_x\)。对静止高斯（\(\Sigma_{xt}=0\)）毫无影响——`mean_offset` 恒为零；但对做运动的高斯，切面中心本应随 \(\tau\) 沿运动方向前移，漏加后椭球位置滞后，运动区域会出现重影/拖尾，且越「斜」（时空相关越强）的高斯错位越大。这也解释了为什么 CUDA 版把 `delta_mean` 直接写进 `p_orig`（[forward.cu:348-351](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L348-L351)）并把它写回 `out_means3D` 供后续使用。

**练习 2**：`cov_t` 什么时候恰好等于 `s_t^2`？
**答案**：当 4D 旋转不把时间轴与空间轴耦合、即 \(R\) 的第 3 行只有 \(R_{33}\neq0\) 时（例如左右四元数都取恒等，\(R=I\)）。一般情形 \(\texttt{cov\_t}=\sum_k R_{3k}^2 s_k^2\)，是四个平方尺度的加权混合——这正是 4D 旋转能让高斯「沿时间斜伸」的机制。

**练习 3**：为什么条件协方差一定「不大于」原 \(\Sigma_{xx}\)？
**答案**：Schur 补减去的是半正定项 \(\Sigma_{xt}\Sigma_{tx}/\sigma_t^2\)（\(\sigma_t^2>0\)），所以对任意向量 \(v\) 有 \(v^{\mathsf T}\Sigma_{x|x}v \le v^{\mathsf T}\Sigma_{xx}v\)。几何上：斜的椭球被超平面切开，切面在任何方向上的宽度都不会超过原椭球沿该方向的投影宽度。

---

### 4.3 `get_cov_t` 与 `get_marginal_t`：时间边缘化衰减

#### 4.3.1 概念说明

条件协方差回答「这一刻椭球长什么样」，还差一个问题：**这一刻它有多亮？** 一个时间中心在 \(\mu_t\)、时间方差 \(\sigma_t^2\) 的高斯，在远离 \(\mu_t\) 的时刻应当几乎不可见。`get_marginal_t` 给出的就是这个可见度：

\[
\texttt{marginal\_t}=\exp\!\left(-\frac{(\tau-\mu_t)^2}{2\,\texttt{cov\_t}}\right)
\]

它在 \(\tau=\mu_t\) 处取 1，随时间距离做高斯衰减。渲染时把它**乘进 opacity**（[gaussian_renderer/__init__.py:91-93](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L91-L93) Python 回退路径，[forward.cu:336](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L336) CUDA 路径）。

**为什么「条件高斯 × marginal_t」是正确的切片渲染？** 把联合密度拆开：

\[
p(x,\tau)=p(x\mid \tau)\cdot p(\tau)
\propto \mathcal N_3\!\left(x;\ \mu_x+\frac{\Sigma_{xt}}{\sigma_t^2}dt,\ \Sigma_{x|x}\right)\cdot
\exp\!\left(-\frac{dt^2}{2\sigma_t^2}\right)
\]

其中 \(dt=\tau-\mu_t\)，比例系数是常数 \((2\pi\sigma_t^2)^{-1/2}\)。也就是说：**渲染器画的正是 4D 高斯在超平面 \(t=\tau\) 上的密度值**——位置用条件均值（含 `mean_offset`），形状用条件协方差（Schur 补），亮度用 `marginal_t`。代码里那个被注释掉的归一化因子恰好就是这个常数：[scene/gaussian_model.py:254](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L254) 末尾的 `# / torch.sqrt(2*torch.pi*sigma)`。它被省略是合理的——每个高斯的整体亮度由可学习的 `opacity` 承担，常数因子可以被 opacity 吸收。

#### 4.3.2 核心流程

```
get_cov_t(modifier):                       # 时间方差从哪来
  if rot_4d:   Σ = (R·D)(R·D)^T;  cov_t = Σ[:,3,3]     # 旋转混合后的方差（4D 旋转路径）
  else:        cov_t = exp(_scaling_t) * modifier      # 轴对齐路径：直接把激活值当方差
get_marginal_t(timestamp, modifier):
  sigma = get_cov_t(modifier)                          # (N,1)
  return exp(-0.5 * (_t - timestamp)^2 / sigma)        # (N,1)
```

阈值关系（解释魔数 `0.05`）：

\[
\texttt{marginal\_t}>0.05
\iff \exp\!\left(-\frac{dt^2}{2\,\texttt{cov\_t}}\right)>0.05
\iff |dt|<\sqrt{2\ln 20}\cdot\sqrt{\texttt{cov\_t}}\approx 2.448\sqrt{\texttt{cov\_t}}
\]

即每个高斯的有效时间支撑半宽约为 \(2.45\sqrt{\texttt{cov\_t}}\)，超出即被剔除（`marginal_t <= 0.05` 时 CUDA 直接 `return`，见 [forward.cu:333-335](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L333-L335) 与 [forward.cu:432-433](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L432-L433)）。

#### 4.3.3 源码精读

**`get_cov_t`：两条路径**，[scene/gaussian_model.py:244-250](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L244-L250)：

```python
def get_cov_t(self, scaling_modifier = 1):
    if self.rot_4d:
        L = build_scaling_rotation_4d(scaling_modifier * self.get_scaling_xyzt, self._rotation, self._rotation_r)
        actual_covariance = L @ L.transpose(1, 2)
        return actual_covariance[:,3,3].unsqueeze(1)
    else:
        return self.get_scaling_t * scaling_modifier
```

这段代码做了什么：`rot_4d=True` 时完整重建 4D 协方差并取右下角元素（就是 4.2 里 `cov_t` 的同一计算，重复了一遍而非复用）；`rot_4d=False` 时没有 4D 旋转、时空天然解耦，直接返回激活后的时间尺度。**注意两条路径对 `_scaling_t` 的语义不一致**（详见 4.4.3 的对照表）。

**`get_marginal_t`：一维高斯衰减**，[scene/gaussian_model.py:252-254](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L252-L254)：

```python
def get_marginal_t(self, timestamp, scaling_modifier = 1): # Standard
    sigma = self.get_cov_t(scaling_modifier)
    return torch.exp(-0.5*(self.get_t-timestamp)**2/sigma) # / torch.sqrt(2*torch.pi*sigma)
```

这段代码做了什么：输入渲染时刻 `timestamp`，输出 `(N,1)` 的时间衰减因子；被注释掉的正是 4.3.1 推导出的常数归一化。`# Standard` 注释表明它对应论文里标准的时间高斯形式。

**`_scaling_t` 的初始值从哪来**（连接 u3-l2），[scene/gaussian_model.py:428-429](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L428-L429)：

```python
            dist_t = torch.zeros_like(fused_times, device="cuda") + (self.time_duration[1] - self.time_duration[0]) / 5
            scales_t = torch.log(torch.sqrt(dist_t))
```

这段代码做了什么：初始化时把 `dist_t` 设为时长/5（`time_duration=[0,10]` 时即 2.0），再取 `log(sqrt(·))` 存入 `_scaling_t`。于是 `get_scaling_t = exp(_scaling_t) = sqrt(时长/5) ≈ 1.414`。上一行被注释的 `distCUDA2` 写法（第 427 行）说明原 4DGS 是用「时间维最近邻距离平方」初始化的，4C4D 改成了与点云无关的常数。`dist_t` 沿用了 3D 的「平方量」命名习惯，但这正是 4.4.3 陷阱的来源。

#### 4.3.4 代码实践

**实践目标**：画出 `marginal_t` 关于时间距离的衰减曲线，验证 `0.05` 阈值对应的有效支撑半宽公式。

**操作步骤**（纯 numpy/CPU 可跑，示例代码）：

```python
import numpy as np

def marginal_t(dt, cov_t):
    return np.exp(-0.5 * dt**2 / cov_t)      # scene/gaussian_model.py:254

cov_t = 2.0                                   # 对应 dist_t = duration/5（rot_4d 路径、恒等旋转）
dt = np.linspace(0, 8, 400)
m = marginal_t(dt, cov_t)

half_width = np.sqrt(2 * np.log(20) * cov_t)  # 解析解
print("有效支撑半宽 |dt| <=", round(half_width, 4))     # 预期 ≈ 5.4286
print("数值验证 marginal_t(half_width) =", marginal_t(half_width, cov_t))  # 预期 ≈ 0.05
```

**需要观察的现象**：曲线在 `dt=0` 处为 1，单调下降；`marginal_t(5.4286) ≈ 0.05`，此后低于阈值会被 CUDA 剔除。

**预期结果**：`half_width = sqrt(2·ln20·2.0) = 2·sqrt(ln20) ≈ 5.4286`，`marginal_t(5.4286) = 0.05` 精确成立（可解析验证，数值运行**待本地验证**）。若把 `cov_t` 换成 `1.414`（`rot_4d=False` 路径的取值），半宽缩到约 4.56——同样的 `_scaling_t`，两条路径给出的「高斯存活时长」差了约 \(\sqrt{2}\) 倍。

#### 4.3.5 小练习与答案

**练习 1**：为什么剔除阈值是 0.05 而不是 0.5 或 0.001？
**答案**：这是个工程折中，在「视觉上可忽略」与「剔除收益」之间取点。0.05 意味着衰减到 5% 亮度以下的高斯不再参与渲染，对应 \(2.45\sqrt{\texttt{cov\_t}}\) 的时间半宽；取 0.5 会把还很亮（一半亮度）的高斯也剔掉造成时序跳变，取 0.001 则半宽扩到 \(3.72\sqrt{\texttt{cov\_t}}\)，剔除几乎不省计算。该常数在 Python 侧（[gaussian_renderer/__init__.py:67](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L67)、[:133](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L133)）与 CUDA 侧（[forward.cu:334](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L334)、[:433](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L433)）各写了一遍，改动时必须同步。

**练习 2**：`marginal_t` 是乘在 opacity 上的，那 opacity 的梯度会怎样受影响？
**答案**：渲染值对 opacity 的偏导被 `marginal_t` 缩放（[backward.cu:768-769](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L768-L769)：`dL_dopacity[idx] *= marginal_t`）。远离当前时刻的高斯即使空间上可见，其 opacity 也几乎收不到梯度——这正是「时间可见性」的梯度意义。同时 [backward.cu:770-772](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L770-L772) 还把这条链的梯度汇入 `dL_dcovt`，所以 `_scaling_t` 也会被联合优化。u6-l3 的 opacity decay 掩码正是复用了 `> 0.05` 这一判据。

**练习 3**：`get_marginal_t` 的注释里被划掉的归一化因子，为什么可以省略？
**答案**：见 4.3.1 的分解 \(p(x,\tau)\propto p(x\mid\tau)\cdot\exp(-dt^2/2\sigma_t^2)\)，省略的是与 \(x\) 无关的常数 \((2\pi\sigma_t^2)^{-1/2}\)。渲染上每个高斯的总亮度本就由可学习的 `_opacity` 控制，常数可被其吸收；若保留反而会让 `_scaling_t` 的变化通过常数项干扰亮度，与 opacity 的职责重叠。

---

### 4.4 渲染侧消费与 `rot_4d` 的语义陷阱

#### 4.4.1 概念说明

本模块把前三个模块串进真实调用链，并指出一个源码级的坑：**`rot_4d` 开与关时，`_scaling_t` 在 `get_cov_t` 里的含义不同**。读代码、复现实验、切换 `rot_4d` 对比时必须知道这一点。

#### 4.4.2 核心流程

```
render()（gaussian_renderer/__init__.py）
  ├─ pipe.compute_cov3D_python == False（默认，快路径）
  │     只把 scales / scales_t / rotations / rotations_r / ts 传给 CUDA，
  │     Schur 补 + marginal_t 全部在 forward.cu 的 computeCov3D_conditional 里做
  └─ pipe.compute_cov3D_python == True（调试回退）
        rot_4d:  cov3D_precomp, delta_mean = pc.get_current_covariance_and_mean_offset(...)
                 means3D += delta_mean            # mean_offset 必须手工加上
        gaussian_dim==4: opacity = opacity * marginal_t
        预筛选: mask = marginal_t[:,0] > 0.05，对所有输入张量同步过滤
```

#### 4.4.3 源码精读

**Python 回退路径**，[gaussian_renderer/__init__.py:85-93](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L85-L93)：

```python
    if pipe.compute_cov3D_python:
        if pc.rot_4d:
            cov3D_precomp, delta_mean = pc.get_current_covariance_and_mean_offset(scaling_modifier, viewpoint_camera.timestamp)
            means3D = means3D + delta_mean
        else:
            cov3D_precomp = pc.get_covariance(scaling_modifier)
        if pc.gaussian_dim == 4:
            marginal_t = pc.get_marginal_t(viewpoint_camera.timestamp)
            opacity = opacity * marginal_t
```

这段代码做了什么：这是 4.2/4.3 两个公式的消费现场。`rot_4d` 时把条件协方差交给光栅化器、把 `mean_offset` 加进 `means3D`；`gaussian_dim==4` 时（无论 `rot_4d`）把 `marginal_t` 乘进 opacity。注意 `+ delta_mean` 只出现在这条 Python 回退里——CUDA 快路径里同样的事由 `forward.cu:348-351` 完成。此外 [gaussian_renderer/__init__.py:113-114](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L113-L114) 在 `convert_SHs_python` 分支里再次调用了同一函数，只为拿到 `delta_mean` 来修正球谐的方向向量 `dir_pp`（视线方向也必须从「修正后的中心」出发，否则视角相关颜色会算错位置）。

**CUDA 无旋路径**，[diff-gaussian-rasterization/cuda_rasterizer/forward.cu:429-435](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L429-L435)：

```cpp
		if (gaussian_dim == 4){  // no rot_4d
            float dt = ts[idx]-timestamp;
            float sigma = scales_t[idx] * scale_modifier;
            float marginal_t = __expf(-0.5*dt*dt/sigma);
            if (marginal_t <= 0.05) return;
            opacity *= marginal_t;
		}
```

这段代码做了什么：`rot_4d=False` 且 `gaussian_dim==4` 时，空间协方差走普通 3D `computeCov3D`（无 Schur 补、无 mean_offset，因为时空解耦时两者分别为 \(\Sigma_{xx}\) 和 0），时间衰减用 `sigma = scales_t[idx] * scale_modifier`——**直接用激活后的时间尺度当方差**，与 Python `get_cov_t` 的 else 分支（[scene/gaussian_model.py:250](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L250)）完全一致。

**`_scaling_t` 语义对照表（陷阱）**。把 [scene/gaussian_model.py:244-250](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L244-L250) 两条分支并排看：

| 路径 | `cov_t` 的取值 | `get_scaling_t` 的角色 | CUDA 对应 |
| --- | --- | --- | --- |
| `rot_4d=True` | \(\Sigma_{33}=\sum_k R_{3k}^2 s_k^2\)，恒等旋转时 \(= s_t^2\) | 是**尺度**，进入 \(\Sigma\) 前先平方 | [forward.cu:331-332](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L331-L332) |
| `rot_4d=False` | \(= s_t\)（未平方） | 被直接当作**方差**使用 | [forward.cu:431](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L431) |

结论：Python 与 CUDA 在**每条路径内部**是一致的，但**跨路径**时同一个 `_scaling_t` 数值产生的时间方差差一个平方。代入初始化值（`dist_t = 时长/5 = 2`，`s_t = sqrt(2)`）：`rot_4d=True` 时 `cov_t = 2`，`rot_4d=False` 时 `cov_t ≈ 1.414`，对应的有效时间半宽分别为约 5.43 与 4.56。这是从 4DGS 继承下来的约定差异（源码注释未说明动机），**切换 `rot_4d` 做对比实验时务必意识到两者的时间支撑宽度并不可比**。

#### 4.4.4 代码实践

**实践目标**：确认 Python 回退路径与 CUDA 快路径产出一致，并亲手体会 `mean_offset` 与预筛选掩码的必要性。

**操作步骤**：

1. 在 yaml 中加 `compute_cov3D_python: true`（该键属于 `PipelineParams`，命令行同样可用 `--compute_cov3D_python`），用一个小数据集（或 1000 次迭代即停）各跑一次训练。
2. 对照两次运行的 TensorBoard `l1_loss` 曲线：前几百个迭代的差异应在数值噪声量级；若显著偏离，优先排查 `mean_offset` 与掩码同步。
3. 阅读型检查（无需 GPU）：列出 [gaussian_renderer/__init__.py:132-157](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L132-L157) 中所有被 `mask` 过滤的张量，数一数一共几个，并回答：若漏掉对 `cov3D_precomp` 的过滤会发生什么？

**需要观察的现象**：步骤 2 两条损失曲线基本重合；步骤 3 应数出 `means2D/means3D/ts/shs/colors_precomp/opacity/scales/scales_t/rotations/rotations_r/cov3D_precomp/flow_2d` 等逐项过滤。

**预期结果**：两条路径数值一致；漏过滤 `cov3D_precomp` 会导致第 \(i\) 个高斯拿到别人的协方差——张量行数与 `opacity`、`means3D` 不再对齐，轻则画面错乱、重则形状不匹配直接报错。本实践依赖 GPU 与数据，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`gaussian_dim=4` 且 `rot_4d=False` 时，`get_current_covariance_and_mean_offset` 还能调用吗？
**答案**：不能。`covariance_activation` 按 `rot_4d` 绑定（[scene/gaussian_model.py:53-56](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L53-L56)），此时它指向 3 参数的 `build_covariance_from_scaling_rotation`，而该方法传了 5 个参数，会抛 `TypeError`。渲染端用 `if pc.rot_4d` 守卫（[gaussian_renderer/__init__.py:86](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L86)）规避。这也是为什么 `covariance_activation` 的选择依据是 `rot_4d` 而非 `gaussian_dim`：无 4D 旋转时根本不需要 Schur 补。

**练习 2**：`get_cov_t` 在 `rot_4d=True` 分支里重复计算了整个 4×4 协方差，却只用其中一个元素。这样写有什么影响？
**答案**：功能上等价（结果就是 \(\Sigma_{33}\)），代价是 `get_marginal_t` 每次调用都要做一遍 `build_scaling_rotation_4d` 的 4×4 矩阵乘。它主要被渲染端低频调用（每视角一次、N 个高斯批量算），相对整个光栅化开销可以接受；但如果要把它用进训练内循环（如 u6-l3 的 `time_visibility` 掩码），这段重复计算是首要的优化候选。判断依据见 [scene/gaussian_model.py:245-248](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L245-L248)。

---

## 5. 综合实践

**任务：写一个 100 行以内的「纯 numpy 时间切片渲染器」，把本讲四个模块串起来。**

要求实现并依次调用：

1. `rot4d(l, r)`：复刻 `build_rotation_4d`（4.1.4 已给出 numpy 版）。
2. `cov4d(s, l, r)`：返回 \(\Sigma = R\,\mathrm{diag}(s)^2 R^{\mathsf T}\)。
3. `slice_at(Sigma, mu, tau)`：返回 `(cond_cov, mean_offset, marginal_t)`，即 Schur 补、均值偏移、时间衰减三件套（公式见 4.2.2、4.3.2）。
4. 主程序：取一个刻意「斜」的高斯——`s = [0.3, 0.3, 0.3, 1.0]`，左右四元数取会使时空耦合的值（提示：可随机采样后检查 \(\Sigma_{xt}\) 非零），\(\mu=(0,0,0,\mu_t)\)。在 \(\tau \in \{-3,-1,0,1,3\}\) 五个时刻，把条件高斯投影到 x-y 平面画热力图（直接对网格求值 \( \texttt{marginal\_t}\cdot\mathcal N_2 \) 即可），拼成一行五张子图。

**检查点**（自己回答）：

- 子图系列的亮斑中心是否随 \(\tau\) 平移？平移方向是否与 \(\Sigma_{xt}\) 的符号一致？（对应 `mean_offset`）
- 亮斑整体亮度是否在 \(\tau=\mu_t\) 处最大、向两侧按高斯衰减？（对应 `marginal_t`）
- 亮斑形状是否随 \(\tau\) 变化（在斜的方向上被压扁）？（对应 Schur 补）
- 把 `rot` 换成恒等四元数重跑，前两条现象是否消失、只剩「静止等亮」？

**预期结果**：能看到一个随时间平移、变亮变暗、轻微形变的椭圆——这就是 4D 高斯被逐帧切片的微观图景，也正是 `forward.cu` 里每个高斯每个线程做的事。运行结果**待本地验证**。

## 6. 本讲小结

- 4D 高斯渲染到某一帧 = **在超平面 \(t=\tau\) 上对联合密度求值**，可分解为「条件 3D 高斯（位置 + 形状）」乘「时间衰减 `marginal_t`」；被省略的常数归一化由可学习的 opacity 吸收。
- `build_scaling_rotation_4d` 用**双四元数**组装 4×4 旋转（SO(4) 需要一对四元数），返回 \(L=R\,\mathrm{diag}(s)\)，协方差 \(\Sigma=LL^{\mathsf T}\)。
- 条件协方差是 **Schur 补** \(\Sigma_{xx}-\Sigma_{xt}\Sigma_{tx}/\sigma_t^2\)（[gaussian_model.py:41](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L41)），均值偏移 \(\Sigma_{xt}/\sigma_t^2\cdot dt\)（第 47 行）——**漏加 `mean_offset` 运动区域会拖尾**；`cov_t` 在有 4D 旋转时是四个平方尺度的旋转混合，正是「斜椭球」产生运动的机制。
- `marginal_t = exp(-dt²/2·cov_t)`，阈值 `0.05` 对应有效时间半宽 \(\sqrt{2\ln20}\sqrt{\texttt{cov\_t}}\approx2.45\sqrt{\texttt{cov\_t}}\)；该判据同时是 u6-l3 opacity decay 时间可见性掩码的来源。
- Python 闭包与 CUDA `computeCov3D_conditional` 逐行同构，真训练走 CUDA；`covariance_activation` 按 `rot_4d`（而非 `gaussian_dim`）绑定，`get_current_covariance_and_mean_offset` 仅在 `rot_4d=True` 时可调用。
- **陷阱**：`get_cov_t` 两条路径中 `_scaling_t` 语义不一致（`rot_4d` 时先平方进 \(\Sigma\)，否则直接当方差），切换 `rot_4d` 对比时时间支撑宽度不可比。

## 7. 下一步学习建议

本讲解决了「一个 4D 高斯在某一帧长什么样」。接下来两条线：

- **u3-l4（4D 球谐）**：补上另一半——椭球的**颜色**如何随视角与时间变化（`eval_shfs_4d`、`sh_degree_t`）。学完后你就集齐了 `render()` 需要的全部模型侧输入。
- **u4-l1（render() 渲染主流程）**：把本讲的条件协方差、`marginal_t` 放进 `GaussianRasterizationSettings` 的组装现场，看它们如何与视锥剔除、分块光栅化衔接；u4-l3 会详细拆 `compute_cov3D_python` 回退路径里的张量同步掩码。

延伸阅读建议：`diff-gaussian-rasterization/cuda_rasterizer/backward.cu:750-772` 中对 Schur 补与 `marginal_t` 的手工求导，是把本讲公式反向走一遍的最好练习；EWA splatting（3DGS 把 3D 协方差投影成 2D 屏幕协方差的算法）则是本讲公式在投影阶段的下一站。
