# 相机模型：pd_cam、get_full_proj 与投影矩阵

## 1. 本讲目标

上一讲（u3-l3）我们数完了 9 个参数解码器，唯独把 `cam_decoder` 留到了本讲。本讲就把这块最后 的拼图补上：网络吐出的 **3 个数**，是怎么变成下游一直在用的 **4×4 矩阵** `pd_cam` 的。

学完本讲你应该能：

1. 说清 `pd_cam` 的三步构造：`cam_decoder` 线性输出 → 加偏置 `[0, 0, 1.5]` → 第三维换成 \(24/s\) 深度。
2. 解释为什么旋转部分固定为 \(\mathrm{diag}(-1,-1,1)\)、平移部分只有两维——这是"裁剪对齐 patch"带来的强先验。
3. 手工推导 `get_proj_matrix` 中 \(P_{00} = 1/\tan(\text{fov}/2) = 24\)，并算出这只相机 的视场角约 \(4.77^\circ\)。
4. 推导深度换算 \(z = f/s\) 的自洽性：它恰好让"尺度参数 \(s\)"等价于"人体半高在画面中的占比"。
5. 会用 `GS_Camera.perspective_projection` 把 EHM 输出的 3D 关节投到 1024×1024 图像平面，并映射回原图检查对齐。

## 2. 前置知识

### 2.1 针孔相机与内参、外参

把相机想成一个带小孔的盒子，3D 点 \(p=(x,y,z)\) 沿直线穿过小孔打到成像平面上：

\[
u = f\frac{x}{z}, \qquad v = f\frac{y}{z}
\]

- \(f\) 是**焦距**（以像素为单位的叫内参 \(K\)，以"归一化平面"为单位的叫 NDC 焦距）；
- "除以 \(z\)"就是**透视除法**——离得越远成像越小；
- 把 3D 点从世界坐标系搬进相机坐标系需要 **RT 矩阵**（外参）：\(p_{cam} = R\,p + T\)，\(R\) 是 3×3 旋转，\(T\) 是 3 维平移，拼成 4×4 齐次矩阵就是本讲反复出现的 RT。

### 2.2 NDC 与屏幕坐标

渲染管线不喜欢"像素"，喜欢先把一切映射到 \([-1,1]^3\) 的标准立方体，叫 **NDC**（Normalized Device Coordinates）。NDC 再线性拉伸回像素就是**屏幕坐标**。PEAR 用的约定：

\[
x_{ndc} = 24\frac{x_c}{z_c}, \qquad x_{screen} = (1 - x_{ndc})\cdot \tfrac{1024}{2} \in [0, 1024]
\]

注意两处非常规的符号：\(P_{33}=1\)（即 \(w = +z\)，相机看向 \(+z\)，这是 pytorch3d / Gaussian Splatting 约定，而 OpenGL 传统是 \(w=-z\)）；以及屏幕映射里那个 `1 -`（pytorch3d 的 NDC \(+x\) 朝左，翻一次才回到"x 朝右"的图像坐标）。

### 2.3 弱透视 vs 透视

- **透视相机**：每个点各自除以自己的 \(z\)，近大远小。
- **弱透视相机**：假设人体各部分深度差不多，整体除以一个平均深度 \(z_0\)，于是"人体在画面里多大"只用一个尺度 \(s\) 就能描述。

从单张 256×256 的裁剪 patch 里恢复真实深度几乎不可能，所以 HMR 系工作（包括 PEAR）都让网络预测**弱相机参数**（尺度 + 2 维平移），再在渲染/投影阶段把它"翻译"回一个合法的透视相机。本讲 4.2 会证明这个翻译是自洽的。

### 2.4 坐标系约定

SMPL-X / EHM 的网格坐标系是 pytorch3d 风格：\(y\) 朝上（头顶为 \(+y\)）、\(z\) 朝观察者。渲染要把 \(y\) 翻成"朝下"（图像 \(v\) 方向），这一步由固定的 \(R=\mathrm{diag}(-1,-1,1)\) 完成——它绕 \(z\) 轴旋转 180°，行列式为 \(+1\)，仍是合法旋转。

## 3. 本讲源码地图

| 文件 | 本讲关注的内容 |
| --- | --- |
| [models/smplx/smplx_head.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py) | `cam_decoder` 输出、bias 与 \(24/s\) 换算、`get_full_proj` 组装 RT、head 内部的 `get_proj_matrix` |
| [utils/graphics_utils.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py) | 参数化版 `get_proj_matrix`、`GS_Camera` 全家（投影矩阵、NDC、屏幕、`perspective_projection`） |
| [inference_wo_detect.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py) | 推理侧 `GS_Camera` 的构造样板（`build_cameras_kwargs` + R/T 切片） |
| [models/pipeline/pipeline.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py) | 训练侧对 `perspective_projection` 的消费点（2D 关键点损失的投影算子） |

提醒一个 u1-l3 就强调过的辨析：`models/smplx/` 是**可学习的解码头**，`models/modules/smplx/` 是**参数转网格的官方层**，两者不是一回事。本讲的相机代码全部在前者（`smplx_head.py`）和 `utils/graphics_utils.py` 里。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **pd_cam 的构造**——3 维弱相机参数如何变成 4×4 RT；
2. **get_proj_matrix / get_full_proj**——透视投影内参与 \(\tan(\text{fov})\) 的关系；
3. **GS_Camera 与透视投影**——两条等价的投影路径与它们的消费场景。

### 4.1 pd_cam 的构造：从 3 个数到 4×4 RT

#### 4.1.1 概念说明

u3-l3 里 `cam_decoder = nn.Linear(1024, 3)` 是 9 个解码器中最小的一个，只输出 3 个数。这 3 个数的语义是：

| 分量 | 含义 | 取值来源 |
| --- | --- | --- |
| 第 0、1 维 | 平移 \(t_x, t_y\)（NDC 尺度） | 网络原始输出，bias 为 0 |
| 第 2 维 | 尺度 \(s\) → 深度 \(z = 24/s\) | 网络原始输出 + 1.5，再取倒数换算 |

也就是说，**网络实际上只需要回答两个问题**："人在画面里偏了多少"（2 维平移）和"人在画面里多大"（1 维尺度）。旋转不用预测——因为输入永远是裁剪正对的人体 patch，相机永远"正对"人，这是数据预处理替相机做的强先验。u2-l3 讲过的 `process_bbox` + `generate_patch_image` 仿射裁剪，正是在制造这个先验。

#### 4.1.2 核心流程

```text
token_out (B,1024)
    │  cam_decoder: Linear(1024→3)
    ▼
raw (B,3)                          # 无约束的原始输出
    │  += bias [0, 0, 1.5]         # 平移不动，尺度加 1.5
    ▼
pd_cam (B,3) = (tx, ty, s)
    │  pd_cam[:,2:] = 24/(s+1e-9)  # 第三维换成深度 z = f/s
    ▼
pd_cam (B,3) = (tx, ty, z)
    │  get_full_proj: R=diag(-1,-1,1) 固定, T=pd_cam
    ▼
RT (B,4,4)  ──────────────►  all_out['pd_cam']   # 下游拿到的是 4×4！
    │
    └─ full_project = P @ RT      # 算了但没输出（见 4.1.3 末尾）
```

#### 4.1.3 源码精读

**第一步：3 维输出加偏置、换算深度。**

[models/smplx/smplx_head.py:303-306](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L303-L306) —— 网络输出加 `[0, 0, 1.5]` 偏置，第三维立即换成 \(f/s\) 深度：

```python
bias = torch.tensor([ 0, 0, 1.5], device=token_out.device)
pd_cam = self.cam_decoder(token_out)
pd_cam += bias
pd_cam[:, 2:] =  24 /  (pd_cam[:, 2:] +  1e-9) # f / s
```

三个细节：

- 平移的 bias 是 0：以画面中心为基准，符合"裁剪把人放中间"的预处理；
- 尺度的 bias 是 1.5：呼应 u3-l3 讲过的**残差式预测**——如果网络输出接近 0，初始深度就是 \(24/1.5 = 16\)，一个温和的中间值，训练不会从"深度爆炸/为负"起步；
- `1e-9` 防 s→0 时除零。

**第二步：拼出 4×4 RT。**

[models/smplx/smplx_head.py:205-231](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L205-L231) —— `get_full_proj` 用固定 R 和上一步的 T 组装 RT，再左乘投影矩阵：

```python
def get_full_proj(self, pd_cam):
    B = pd_cam.shape[0]
    T = pd_cam # [B,1,3]
    R = torch.tensor([
            [-1.0,  0.0,  0.0],
            [ 0.0, -1.0,  0.0],
            [ 0.0,  0.0,  1.0]
        ], device=pd_cam.device, dtype=pd_cam.dtype).unsqueeze(0).expand(B, -1, -1)

    Tmat=torch.eye(4,device=R.device)[None].repeat(R.shape[0],1,1)
    Tmat[:,:3,:3] = R.clone()
    Tmat[:,:3,3] = T.clone()
    ...
    full_mat = torch.bmm(proj_mat, Tmat)
    return full_mat, Tmat
```

- \(R=\mathrm{diag}(-1,-1,1)\)：绕 \(z\) 轴转 180°，把"y 朝上"翻成"y 朝下"，完成网格坐标系 → 相机坐标系的转换；
- `Tmat[:,:3,3] = T`：把 \((t_x, t_y, z)\) 直接塞进平移列——注意第三维此刻已经是**深度**而不是尺度；
- 注释 `# [B,1,3]` 与实际的 `(B,3)` 不符，又一处注释漂移（本手册已多次遇到，以代码为准）。

**第三步：输出的是 RT，不是 3 维参数。**

[models/smplx/smplx_head.py:309-317](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L309-L317) —— 组装输出字典：

```python
full_project, RT= self.get_full_proj(pd_cam) # [B,9]

all_out =  {}
all_out['pd_cam'] = RT  # [4,4]
all_out['body_param'] = body_param_dict
all_out['flame_param'] = flame_param_dict
```

两个必读要点：

- **命名陷阱**：局部变量 `pd_cam`（3 维）和 `all_out['pd_cam']`（4×4）同名不同物。u2-l5 说"`pd_cam` 是 (B,4,4) RT 矩阵"，指的就是后者——本讲终于补上了它出生的完整过程。
- **下游生效审计**：`full_project`（内参×外参的乘积）算出来后**没有进入输出字典**，函数内也没有其他使用。真正被下游消费的只有 `RT`，内参是在渲染/投影阶段由消费方（`GS_Camera`）按同样的 \(f=24\) 约定现场重建的。`full_project` 属于"备而未用"，与 u2-l1 的 `with_smplx_gaussian`、u3-l3 的 `smplx_joint_decoder` 同一待遇。

#### 4.1.4 代码实践

**实践目标**：不跑网络，只用纯数学在 CPU 上复现"3 维参数 → 4×4 RT"的全过程，验证你对构造规则的理解。

**操作步骤**（示例代码，可直接在仓库根目录用 `python` 交互式运行）：

```python
# 示例代码：手工复现 pd_cam 的构造（不依赖 GPU 和模型权重）
import torch

B = 1
raw = torch.tensor([[0.1, -0.2, 0.3]])          # 假装的 cam_decoder 原始输出

# 第一步：bias 与 24/s 换算（对照 smplx_head.py:303-306）
pd_cam = raw + torch.tensor([0, 0, 1.5])
pd_cam[:, 2:] = 24 / (pd_cam[:, 2:] + 1e-9)
print(pd_cam)                                   # [[ 0.1, -0.2, 13.3333]]

# 第二步：拼 RT（对照 smplx_head.py:209-218）
R = torch.tensor([[-1., 0, 0], [0, -1., 0], [0, 0, 1.]])
Tmat = torch.eye(4)[None].repeat(B, 1, 1)
Tmat[:, :3, :3] = R
Tmat[:, :3, 3] = pd_cam
print(Tmat[0])
```

**需要观察的现象**：

- 第三维从 `0.3 + 1.5 = 1.8` 变成了 `24/1.8 ≈ 13.33`——尺度被翻译成了"网格放在离相机约 13 个单位远处"；
- RT 的旋转块是干净的 `diag(-1,-1,1)`，平移列是 `(0.1, -0.2, 13.33)`。

**预期结果**：`raw` 的尺度分量越大（人在画面里越大），换算出的深度越小（人离相机越近）——这正是"近大远小"的逆运算。此实践为纯张量运算，预期结果可直接在 CPU 上验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 bias 是 `[0, 0, 1.5]` 而不是 `[0, 0, 0]`？

**答案**：第三维要再做 \(24/s\) 的倒数换算。若 s 从 0 附近起步，\(24/s\) 会爆炸甚至变号。加 1.5 让网络"什么都不预测"时深度落在 \(24/1.5=16\) 的温和区间，与 u3-l3 的 `set_smpl_init` 均值初始化同属残差式预测思路。

**练习 2**：为什么 R 固定为 \(\mathrm{diag}(-1,-1,1)\) 而不让网络预测旋转？

**答案**：输入是经过 `sanitize_bbox` → `process_bbox` → `generate_patch_image` 仿射裁剪出的正对人体 patch，相机与人体的相对朝向被预处理固定为"正对"，剩下的自由度只有平移（2 维）和尺度（1 维）。让网络预测旋转反而引入了不可辨识性（人体姿态和相机会互相补偿）。这是 HMR 系工作的标准做法。

**练习 3**：`all_out['pd_cam']` 里存的是 3 维弱相机参数还是 4×4 矩阵？

**答案**：4×4 RT。3 维参数是 `forward` 内的局部变量 `pd_cam`，在 `get_full_proj` 里被消化掉了。下游如 `inference_wo_detect.py` 直接对 `outputs['pd_cam']` 做 `[:3,:3]`、`[:3,3]` 切片，说明拿到的就是矩阵。

### 4.2 get_proj_matrix：透视投影内参与 tanfov 的关系

#### 4.2.1 概念说明

RT 只负责"搬家"，把点从网格坐标系搬到相机坐标系；真正决定"成像多大"的是**投影矩阵 P**（内参）。PEAR 没有存一张内参矩阵到 checkpoint，而是每次用 `get_proj_matrix(tanfov, ...)` 现场生成——因为整只相机只有一个自由度：焦距 \(f=24\)，主点在画面中心，\(z_{near}=0.01\)、\(z_{far}=100\)。

这一节要回答三个问题：这个矩阵每一项是怎么来的？焦距 24 对应多大的视场角？为什么深度取 \(z=f/s\) 就能自洽？

#### 4.2.2 核心流程

`get_proj_matrix` 的输入是**半视场角的正切** `tanfov`，视锥体四壁由它和 \(z_{near}\) 决定：

```text
tanfov = 1/focal_length = 1/24
    │  视锥体: right = tanfov·z_near, top = tanfov·z_near, 左右/上下对称
    ▼
P = diag(1/tanfov, 1/tanfov, ~1.0001)  +  [2,3]=-0.01, [3,2]=1
    │  即 P[0,0]=P[1,1]=24, w=z
    ▼
full = P @ RT        # 先外参后内参，一次矩阵乘法完成整个投影
```

推导：视锥体 \([-r,r]\times[-t,t]\times[n,f]\) 映射到 NDC 立方体的标准透视矩阵，在左右、上下均对称（主点居中）时化简为

\[
P=\begin{bmatrix}
\frac{1}{\tan(\text{fov}/2)} & 0 & 0 & 0\\[4pt]
0 & \frac{1}{\tan(\text{fov}/2)} & 0 & 0\\[4pt]
0 & 0 & \frac{z_{far}}{z_{far}-z_{near}} & -\frac{z_{far}z_{near}}{z_{far}-z_{near}}\\[4pt]
0 & 0 & 1 & 0
\end{bmatrix}
\]

最后一行 \(w = z\)（而非 OpenGL 的 \(-z\)）是因为 pytorch3d / Gaussian Splatting 约定相机看向 \(+z\)。代入 PEAR 的数值 \(\tan(\text{fov}/2)=1/24\)、\(z_{near}=0.01\)、\(z_{far}=100\)：

- \(P_{00}=P_{11}=24\) —— 这就是全仓到处出现的"焦距 24"的真身；
- \(P_{22}=100/99.99\approx 1.0001\)，\(P_{23}\approx -0.0100\) —— 深度缓冲映射，对 xy 投影无影响；
- NDC 坐标因此是 \(x_{ndc}=24\,x_c/z_c\)，\(y_{ndc}=24\,y_c/z_c\)。

**视场角**：由 \(\tan(\text{fov}/2)=1/24\) 得

\[
\text{fov} = 2\arctan\!\left(\tfrac{1}{24}\right) \approx 2\times 2.386^\circ \approx 4.77^\circ
\]

这是一只**极窄视野的"长焦"相机**——不奇怪，网格被摆在 \(z=24/s\approx 16\) 个单位外，物理身高约 2 个单位，角尺寸本来就只有几度。

**深度换算 \(z=f/s\) 的自洽性**（本讲最值得记住的一个推导）。把 \(z=f/s\) 代回投影公式，设人体半高 \(h\approx 1\)（SMPL-X 站姿头顶 \(y\approx+1\)、脚 \(y\approx-1\)）：

\[
y_{ndc} = \frac{f\,y}{z} = \frac{f\,y}{f/s} = s\cdot y
\quad\Longrightarrow\quad
y_{ndc}^{\text{头顶}} \approx s\cdot 1 = s
\]

也就是说：**尺度参数 \(s\) 的几何意义就是"人体半高占 NDC 半高（即画面半高）的比例"**。\(s=1\) 时头顶恰好顶到画面边缘，\(s=0.5\) 时人占画面一半。弱透视的"尺度"与透视的"深度"通过 \(z=f/s\) 严丝合缝地对上了——这就是"弱相机参数翻译成透视相机"的数学根据。

#### 4.2.3 源码精读

**head 内部这份：`get_proj_matrix`。**

[models/smplx/smplx_head.py:62-81](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L62-L81) —— 由 tanfov 定义视锥体，逐项填写对称透视矩阵：

```python
def get_proj_matrix( tanfov,device, z_near=0.01, z_far=100, z_sign=1.0,):
    tanHalfFovY = tanfov
    tanHalfFovX = tanfov

    top = tanHalfFovY * z_near
    bottom = -top
    right = tanHalfFovX * z_near
    left = -right
    z_sign = 1.0

    proj_matrix = torch.zeros(4, 4).float().to("cuda")
    proj_matrix[0, 0] = 2.0 * z_near / (right - left)
    proj_matrix[1, 1] = 2.0 * z_near / (top - bottom)
    ...
```

逐项核对：`2*z_near/(right-left) = 2n/(2·tanfov·n) = 1/tanfov`，即上一节的 \(P_{00}\)。注意 [models/smplx/smplx_head.py:73](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L73) 写死了 `.to("cuda")`——这就是 u2-l5 说过"前向必须有 GPU"的直接原因：`forward` 每次都会经 `get_full_proj` → `get_projection_transform` 走到这里。

**head 的缓存与焦距来源。**

[models/smplx/smplx_head.py:187-203](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L187-L203) —— `get_projection_transform` 把 `focal_length` 转成 `tanfov=1/focal` 并缓存：

```python
if  torch.unique(self.focal_length).numel()==1: # True
    invtanfov=self.focal_length[0,0]  #
    proj_mat=get_proj_matrix(1/invtanfov,device,z_near=self.z_near,z_far=self.z_far)  # 内参？
    proj_mats=proj_mat[None].repeat(self.focal_length.shape[0],1,1)
```

`self.focal_length` 来自 [models/smplx/smplx_head.py:155-161](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L155-L161)：`torch.tensor([24])` 再按 `batch_size` expand 成 `(B,2)`——这也解释了 u2-l5 的疑问"为什么构造 head 需要 `TRAIN.batch_size`"：焦距张量要预先扩到批大小。首次调用后结果缓存在 `self.proj_mats`，后续直接复用。

**utils 里那份参数化的孪生兄弟。**

[utils/graphics_utils.py:57-76](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L57-L76) —— 与 head 内版本几乎逐行相同，唯一实质差异是 [utils/graphics_utils.py:68](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L68) 写的是 `.to(device)`：

```python
proj_matrix = torch.zeros(4, 4).float().to(device)
```

所以做 CPU 上的数值实验要 import 这一份（4.1.4 与 4.2.4 都靠它）。此外 utils 里还有面向"外部给定的 w2c 相机"的封装 [utils/graphics_utils.py:78-84](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L78-L84) `get_full_proj_matrix`，它被训练数据管线使用（[dataset/webdata_loader.py:293](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L293)），推理链路不经过它。

**u2-l2 的"三处一致"在源码上的落点**：head 内 \(f=24\)（L155）、`GS_Camera` 构造传 `focal_length=24`（[inference_wo_detect.py:91](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L91)）、渲染器 `BodyRenderer("assets/SMPLX", 1024, focal_length=24.0)`（[inference_wo_detect.py:54](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L54)）。三处任改一处，网格就会"错位/错大小"。

#### 4.2.4 代码实践

**实践目标**：在 CPU 上数值验证投影矩阵的每一项，并亲手算出视场角和深度自洽性。

**操作步骤**（示例代码）：

```python
# 示例代码：验证 get_proj_matrix 的数值（用 utils 版，可在 CPU 运行）
import torch
from utils.graphics_utils import get_proj_matrix

P = get_proj_matrix(1/24, device="cpu", z_near=0.01, z_far=100)
print(P)
# 核对: P[0,0]==P[1,1]==24, P[0,2]==P[1,2]==0, P[3,2]==1

# 视场角
fov = 2 * torch.atan(torch.tensor(1/24)) * 180 / torch.pi
print(f"FOV = {fov:.2f} deg")            # ≈ 4.77°

# 深度自洽: z=f/s 时, 半高≈1 的人体投影到 NDC 的位置
for s in [0.5, 1.0, 1.5]:
    z = 24 / s
    y_ndc_head = 24 * 1.0 / z            # 头顶 y≈+1
    print(f"s={s}: z={z:.1f}, 头顶 NDC y≈{y_ndc_head:.2f}")
```

**需要观察的现象**：

- `P[0,0]` 与 `P[1,1]` 精确等于 24（不是 23.99——因为 `1/tanfov` 里 tanfov 本来就是 `1/24` 传进去的）；
- `s=0.5/1.0/1.5` 时头顶的 NDC 坐标分别约 `0.5/1.0/1.5`——验证 \(y_{ndc}=s\cdot y\)。

**预期结果**：打印值与 4.2.2 的推导逐一吻合。此实践为纯 CPU 数值实验，预期结果可直接验证。

#### 4.2.5 小练习与答案

**练习 1**：不看书推导一遍：`tanfov=1/24` 时 \(P_{00}\) 等于多少？

**答案**：\(P_{00}=2z_{near}/(right-left)=2n/(2\cdot\tan(\text{fov}/2)\cdot n)=1/\tan(\text{fov}/2)=24\)。分子分母里的 \(z_{near}\) 互相抵消——NDC 焦距与近平面距离无关。

**练习 2**：这只相机的视场角多大？为什么这么设计？

**答案**：约 \(4.77^\circ\)。因为网格放在 \(z=24/s\)（约十几个单位）远处做透视除法，配合 \(f=24\) 的 NDC 焦距，人体的角尺寸自然落在这个量级；等价地说 \(f=24\) 把"归一化平面"放大了 24 倍，画面容纳的角度就缩小到 \(2\arctan(1/24)\)。

**练习 3**：若把 `pd_cam[:, 2:] = 24 / (...)` 里的 24 改成 48（其余不动），渲染结果会怎样？

**答案**：深度翻倍、投影矩阵的 \(P_{00}\) 仍是 24，于是 \(y_{ndc}=24y/(48/s)=0.5\,s\,y\)——人会渲染成原来的一半大。因为深度换算里的 \(f\) 与投影矩阵里的 \(f\) 必须是同一个 24（4.2.3 末尾"三处一致"的第四处：换算式本身），改了一处没改另一处就破坏了 \(z=f/s\) 的自洽性。

### 4.3 GS_Camera 与透视投影：把 3D 点投到屏幕

#### 4.3.1 概念说明

前面两节造好了 RT 和 P，真正"扣扳机"的是 `utils/graphics_utils.py` 里的 `GS_Camera`。它继承 pytorch3d 的 `CamerasBase`，但换掉了投影方法（类注释写着 "adapting to gaussian splatting's projection method"，这也是 GS 前缀的来源），提供两条**数学上等价、实现路径不同**的投影通道：

| 通道 | 入口 | 实现方式 | 消费者 |
| --- | --- | --- | --- |
| 矩阵通道 | `transform_points_screen` | 组装 `full = P @ RT`，齐次乘 + 除 \(w\)，再按 `self.image_size` 映射屏幕 | 渲染器（`GS_BaseMeshRenderer.forward` 等） |
| 显式通道 | `perspective_projection` | 显式写死 K（\(f=24\)、主点 0）和画布 1024，逐步做"旋转→平移→除深度→乘 K" | 训练循环的 2D 关键点投影 |

为什么要留两条？矩阵通道贴合 pytorch3d 渲染器（`GS_MeshRasterizer` 在内部调 `transform_points_to_view` / `transform_points_view_to_ndc`）；显式通道来自 4D-Humans 的经典实现（源码注释里附了链接），简单直接，适合训练时对批量关节做投影求损失。

#### 4.3.2 核心流程

**构造**（推理侧样板，`inference_wo_detect.py:91`）：

```text
outputs['pd_cam'] (1,4,4)
    ├── R ← [:, :3, :3]   = diag(-1,-1,1)
    └── T ← [:, :3, 3]    = (tx, ty, z)
GS_Camera(principal_point=0, focal_length=24, image_size=[1024,1024], device='cuda', R, T)
```

**矩阵通道**（`transform_points_screen`）：

```text
p (B,N,3) → 齐次化 → full = P @ RT → p·full → 除 w 得 NDC
          → screen = (1 - ndc) · image_size/2   # 两次翻转后落在 [0,1024]
```

**显式通道**（`perspective_projection`）：

```text
p_cam = R·p + T
u = 24·p_cam.x / p_cam.z        # K 的作用
screen = (1 - u) · 1024/2       # 硬编码 1024
```

两条通道的等价性可一行验证：矩阵通道的 \(x_{ndc}=P_{00}\,x_c/w = 24\,x_c/z_c\)（\(P_{02}=0\)，主点居中），屏幕映射同为 \((1-x_{ndc})\cdot 512\)；显式通道一模一样。**前提是** focal=24、image_size=1024、主点为 0——恰好是全仓的固定约定。

#### 4.3.3 源码精读

**构造函数。**

[utils/graphics_utils.py:172-208](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L172-L208) —— `GS_Camera.__init__`：主点写死 `(0,0)`，标量焦距扩成 `(N,2)`，`proj_mats` 置空待缓存：

```python
def __init__(self, focal_length=1.0, R=..., T=..., principal_point=((0.0, 0.0),),
             device="cpu", in_ndc=True, image_size=None):
    ...
    if self.focal_length.ndim == 1:  # (N,)
        self.focal_length = self.focal_length[:, None]  # (N, 1)
    self.focal_length = self.focal_length.expand(-1, 2)  # (N, 2)
    self.proj_mats=None
```

推理侧的构造样板在 [inference_wo_detect.py:36-43](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L36-L43)（`build_cameras_kwargs`：主点 0、focal 24、画布 1024、device cuda）与 [inference_wo_detect.py:91](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L91)（从 `outputs['pd_cam']` 切出 R/T）：

```python
pd_camera = GS_Camera(**build_cameras_kwargs(1,24), R = outputs['pd_cam'][0:0+1,:3,:3], T = outputs['pd_cam'][0:0+1,:3,3])
```

训练侧同一套约定见 [models/pipeline/pipeline.py:85-86](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L85-L86)：`self.cameras = GS_Camera(**cameras_kwargs)`，其中 `cameras_kwargs` 由同文件 L207 起的 `build_cameras_kwargs(batch_size, focal_length=24)` 生成——**训练与推理共用同一只相机**。

**投影矩阵的重建与缓存。**

[utils/graphics_utils.py:240-256](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L240-L256) —— `GS_Camera.get_projection_transform`：与 head 内版本（4.2.3）逻辑相同，把 focal 转 `tanfov=1/focal` 调 `get_proj_matrix`，区别是 device 参数化、走本文件那份实现：

```python
if  torch.unique(self.focal_length).numel()==1:
    invtanfov=self.focal_length[0,0]
    proj_mat=get_proj_matrix(1/invtanfov,device)
    proj_mats=proj_mat[None].repeat(self.focal_length.shape[0],1,1)
```

这印证了 4.1.3 的审计结论：head 不往外传 `full_project` 没关系——`GS_Camera` 会用同样的 \(f=24\) 把 P 现场再造一遍。

**矩阵通道：齐次变换与屏幕映射。**

[utils/graphics_utils.py:258-290](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L258-L290) —— `transform_points_to_ndc` 用 `full = P @ RT` 一次矩阵乘完成外参+内参，再除齐次分量 \(w\)：

```python
full_mat=torch.bmm(proj_mat[:Tmat.shape[0]],Tmat)  # 防止最后一个batch
points_ndc=torch.einsum('bij,bnj->bni',full_mat,points_h)
points_ndc_xyz=points_ndc[:,:,:3]/(points_ndc[:,:,3:]+1e-7)
```

[utils/graphics_utils.py:306-330](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L306-L330) —— `transform_points_to_screen` 把 NDC 线性映射到像素，`with_xyflip` 完成 pytorch3d 手性到图像坐标的翻转：

```python
points_screen[...,:2]= (points_ndc[...,:2] - 1)* image_size/2   # x,y  in [-1024,0]
if with_xyflip:  # true
    points_screen[...,:2]=points_screen[:,:,:2]*-1  # 转化到 [0,1024]
```

**显式通道：`perspective_projection`。**

[utils/graphics_utils.py:336-382](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L336-L382) —— 来自 4D-Humans 的实现（注释附原始链接）：R 默认取同样的 \(\mathrm{diag}(-1,-1,1)\)，K 的焦距与画布**硬编码**：

```python
K = torch.zeros([B, 3, 3], device=points.device, dtype=points.dtype)
K[:,   0,  0] = 24
K[:,   1,  1] = 24
K[:,   2,  2] = 1.
K[:, :-1, -1] = camera_center
...
points = torch.einsum('bij, bkj -> bki', rotation, points)
points = points + translation.unsqueeze(1)
projected_points = points / points[:, :, -1].unsqueeze(-1)   # 透视除法
projected_points = torch.einsum('bij, bkj -> bki', K, projected_points)
...
points_screen[...,:2]= (projected_points[...,:2] - 1) * 1024 / 2
if with_xyflip:
    points_screen[...,:2] = points_screen[:,:,:2]*-1
```

注意一个使用陷阱：这段代码**不读** `self.focal_length` 和 `self.image_size`——24 和 1024 是写死的字面量。如果你构造 `GS_Camera` 时传了别的 image_size，矩阵通道会跟着变，显式通道仍按 1024 输出。在这个"全仓常数"的项目里两者恰好一致，但二次开发改画布时这里是个暗雷。

**谁在消费它（下游生效审计，正面案例）。** 这个方法不是摆设——训练循环正是靠它把预测的 3D 关节投成 2D 来算关键点损失：

[models/pipeline/pipeline.py:287](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L287) —— 投影 EHM 输出的 145 个关节（55 关节 + 68 landmark + 22 附加点，见 u4 计划）：

```python
pred_smpl_2d   = self.cameras.perspective_projection(pred_smpl_3d, R = outputs['pd_cam'][:,:3,:3], T= outputs['pd_cam'][:,:3,3]) # [1,145,3]
```

同类的调用还有 [models/pipeline/pipeline.py:434](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L434)（验证阶段的 `pd_proj_joints`）。也就是说，**2D 关键点监督信号在数学上就是本讲的投影链**——网络学到的 `pd_cam` 之所以能对齐像素，是因为损失直接经过这条链反传。u5-l3 讲损失时还会回到这里。

#### 4.3.4 代码实践

**实践目标**：不依赖网络权重，验证两条投影通道在 PEAR 约定下（focal=24、画布 1024、主点 0）数值等价。

**操作步骤**（示例代码，CPU 可跑；`GS_Camera` 的渲染相关 import 需要 pytorch3d，参照 u1-l2 安装）：

```python
# 示例代码：对照两条投影通道
import torch
from utils.graphics_utils import GS_Camera

# 用 4.1.4 造出的 RT（这里重造一遍）
pd_cam3 = torch.tensor([[0.1, -0.2, 24/1.8]])
R = torch.tensor([[[-1., 0, 0], [0, -1., 0], [0, 0, 1.]]])
T = pd_cam3
screen = torch.tensor([1024., 1024.])[None]
cam = GS_Camera(principal_point=torch.zeros(1, 2), focal_length=24,
                image_size=screen, device="cpu", R=R, T=T)

pts = torch.randn(1, 50, 3) * 0.5               # 假装是关节点（米级尺度）
a = cam.transform_points_screen(pts)[0, :, :2]  # 矩阵通道
b = cam.perspective_projection(pts)[0, :, :2]   # 显式通道
print((a - b).abs().max())                      # 期望接近 0
```

**需要观察的现象**：两条通道输出的最大差异应在 \(10^{-3}\) 像素量级以内（差异来自 `1e-7` 除法保护项与浮点顺序）。

**预期结果**：差异近似为 0，印证 4.3.2 的等价性推导。若把 `image_size` 改成 512 再跑，矩阵通道输出减半而显式通道不变——肉眼可见两条通道分道扬镳。此实践为纯 CPU 数值实验，预期结果可直接验证。

#### 4.3.5 小练习与答案

**练习 1**：`transform_points_screen` 与 `perspective_projection` 何时等价？

**答案**：当 focal_length=24、image_size=1024、主点为 0 时（即 PEAR 的固定约定）。因为矩阵通道的 \(x_{ndc}=24x_c/z_c\)、屏幕映射 \((1-x_{ndc})\cdot 512\) 与显式通道逐字相同。任一约定被单独修改（尤其 image_size，显式通道硬编码 1024 不读 `self.image_size`）等价性即破坏。

**练习 2**：构造 `GS_Camera` 时把 `image_size` 传成 512，`perspective_projection` 的输出画布是多大？

**答案**：仍是 1024×1024——`1024/2` 写死在函数体里。要改显式通道的画布只能改源码，改构造参数只影响矩阵通道（`transform_points_screen` 读 `self.image_size`）。

**练习 3**：训练循环里 2D 关键点损失用的投影算子是哪一个？它和推理渲染用的相机是同一套约定吗？

**答案**：`self.cameras.perspective_projection`（[models/pipeline/pipeline.py:287](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L287)）。是同一套：`self.cameras` 在 pipeline.py:85-86 以 `focal_length=24`、`image_size=1024` 构造，R/T 直接取自 `outputs['pd_cam']` 的切片，与 `inference_wo_detect.py:91` 的构造方式完全一致。

## 5. 综合实践

**任务**：取一张真实推理的 `pd_cam`，把 EHM 输出的 145 个 3D 关节投影回原图，用 `cv2.circle` 画点，肉眼检查与人体轮廓是否对齐——把本讲的"构造 RT → 重建投影 → 屏幕映射 → letterbox 逆变换"整条链走通。

**操作步骤**（示例代码，保存为仓库根目录下的 `check_pd_cam_proj.py`，需 GPU 与 u1-l2 备齐的资产）：

```python
# 示例代码：pd_cam 投影对齐检查（在仓库根目录运行）
import cv2, torch, numpy as np
from models.modules.ehm import EHM_v2
from models.pipeline.ehm_pipeline import Ehm_Pipeline
from utils.pipeline_utils import to_tensor
from utils.graphics_utils import GS_Camera
from utils.general_utils import ConfigDict, add_extra_cfgs
from huggingface_hub import hf_hub_download

# 1. 装配模型（与 inference_wo_detect.py 完全一致）
meta_cfg = add_extra_cfgs(ConfigDict(model_config_path='configs/infer.yaml'))
ehm_model = Ehm_Pipeline(meta_cfg)
_state = torch.load(hf_hub_download('BestWJH/PEAR_models', 'pear_model.pt',
                                    repo_type='model'), map_location='cpu',
                    weights_only=True)
ehm_model.backbone.load_state_dict(_state['backbone'], strict=False)
ehm_model.head.load_state_dict(_state['head'], strict=False)
ehm_model = ehm_model.cuda()
ehm = EHM_v2("assets/FLAME", "assets/SMPLX").cuda()

def pad_and_resize(img, target_size):
    h, w = img.shape[:2]
    scale = min(target_size / h, target_size / w)
    new_w, new_h = int(w * scale), int(h * scale)
    r = cv2.resize(img, (new_w, new_h))
    c = np.zeros((target_size, target_size, 3), np.uint8)
    c[(target_size-new_h)//2:(target_size-new_h)//2+new_h,
      (target_size-new_w)//2:(target_size-new_w)//2+new_w] = r
    return c

img = cv2.imread('data_input/test_source_images/demo.jpg')   # 换成你的图
img_patch = torch.permute(to_tensor(pad_and_resize(img, 256), 'cuda:0') / 255,
                          (2, 0, 1)).unsqueeze(0)

# 2. 前向：参数 + 网格 + 关节
outputs = ehm_model(img_patch)
pd_smplx_dict = ehm(outputs['body_param'], outputs['flame_param'], pose_type='aa')

# 3. 按 inference_wo_detect.py:91 的方式构造 GS_Camera
pd_cam = outputs['pd_cam']                                    # (1,4,4)
screen = torch.tensor([1024., 1024.])[None]
pd_camera = GS_Camera(principal_point=torch.zeros(1, 2).float(), focal_length=24,
                      image_size=screen, device='cuda',
                      R=pd_cam[0:1, :3, :3], T=pd_cam[0:1, :3, 3])

# 4. 投影关节到 1024 画布（joints 本身就是 (B,145,3)，不要再 unsqueeze）
kp2d = pd_camera.perspective_projection(pd_smplx_dict['joints'])   # (1,145,3)
kp2d = kp2d[0, :, :2].detach().cpu().numpy()

# 5. letterbox 逆变换：1024 画布坐标 → 原图像素坐标
h, w = img.shape[:2]
scale = min(1024 / h, 1024 / w)
new_w, new_h = int(w * scale), int(h * scale)
x_off, y_off = (1024 - new_w) // 2, (1024 - new_h) // 2
vis = img.copy()
for x, y in kp2d:
    u = (x - x_off) / scale        # 忽略整数化误差的连续逆映射
    v = (y - y_off) / scale
    if 0 <= u < w and 0 <= v < h:
        cv2.circle(vis, (int(u), int(v)), 4, (0, 0, 255), -1)
cv2.imwrite('proj_check.jpg', vis)
```

**需要观察的现象**：

1. `proj_check.jpg` 上 145 个红点应勾勒出人体骨架与脸部轮廓（前 55 个是 SMPL-X 关节，后 68 个是脸部 landmark——脸上会有一圈密集红点）；
2. 点的位置应贴合原图人体的头顶、肩、肘、膝等部位；
3. 前两维 `pd_cam` 平移越大，点整体越偏向一侧；尺度 \(s\) 越大点越撑满画面。

**预期结果**：若环境与权重就绪，红点与人体轮廓对齐（误差在几个像素内）——这验证了 4.1 的 RT 构造、4.2 的投影约定、4.3 的屏幕映射以及 letterbox 逆变换整条链的自洽。若点整体偏移半幅画面，优先检查两处：是否忘了 letterbox 逆变换里的 `x_off/y_off`，以及第 5 步的 scale 是否误用了 256 而非 1024。本实践依赖 GPU、模型权重与 FLAME/SMPLX 资产，具体渲染效果**待本地验证**。

**延伸思考**（不必上机）：这个脚本其实就是训练循环里 2D 关键点损失的"推理版复刻"——`pipeline.py:287` 做的是同样的事，只不过它拿 GT 关节来监督。想通这一点，u5-l3 的损失设计就只剩加权策略了。

## 6. 本讲小结

- `all_out['pd_cam']` 是 **4×4 RT**，不是 3 维参数：`cam_decoder` 输出 3 维 → 加偏置 `[0,0,1.5]` → 第三维换算 \(z=24/s\) → 与固定 \(R=\mathrm{diag}(-1,-1,1)\) 拼成 Tmat；局部变量与输出字典同名不同物，是最容易踩的命名陷阱。
- 旋转固定、平移两维、尺度一维——"裁剪正对 patch"的预处理先验替网络消掉了旋转自由度；bias=1.5 是残差式预测在相机上的体现。
- `get_proj_matrix` 的核心恒等式：\(P_{00}=1/\tan(\text{fov}/2)=24\)，对应约 \(4.77^\circ\) 的窄视场"长焦"相机；`z=f/s` 的换算让 \(y_{ndc}=s\cdot y\)，即**尺度 s 的几何意义是人体半高占画面半高的比例**——弱透视到透视的翻译因此自洽。
- head 里算出的 `full_project`（P@RT）没有进入输出字典，属"备而未用"；内参由 `GS_Camera` 用同样的 \(f=24\) 现场重建，因此"焦距 24 / 画布 1024"必须三处（head、GS_Camera、Renderer）一致。
- `GS_Camera` 有两条等价投影通道：矩阵通道 `transform_points_screen`（渲染用，读 `self.image_size`）与显式通道 `perspective_projection`（训练损失用，K 与 1024 硬编码不读成员变量）——后者是 2D 关键点监督的投影算子（`pipeline.py:287`），也是二次开发改画布时的暗雷。
- head 内 `get_proj_matrix` 写死 `.to("cuda")`（`smplx_head.py:73`），utils 版参数化 device——CPU 数值实验请用 utils 版，这也是"前向必须有 GPU"的根源。

## 7. 下一步学习建议

本讲补完了 u3 网络侧最后一块：**参数 → 相机**。相机拿到 RT 之后，下游只剩"网格从哪来"。下一讲 **u4-l1（SMPL-X：参数化人体模型的加载与模板）** 开始单元四，进入 `models/modules/smplx/SMPLX.py`，看 `body_param['shape']` 里的 200 维 betas 如何通过 `blend_shapes`、`vertices2joints` 变成 10475 个顶点——`pd_cam` 与这些顶点在 u4-l4（EHM_v2）合流，再在 u4-l5（Renderer2）合成本讲反复出现的 1024×1024 渲染图。

如果想先巩固本讲，建议两件事：一是把 4.3.4 的等价性脚本跑一遍并故意改 `image_size` 观察两条通道分离；二是带着"投影损失如何反传到 `cam_decoder`"这个问题去预读 [models/pipeline/pipeline.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py) 中 L279-L300 附近的损失段，为 u5-l3 做铺垫。
