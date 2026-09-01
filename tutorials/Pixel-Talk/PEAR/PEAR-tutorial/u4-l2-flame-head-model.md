# u4-l2 FLAME：参数化头部模型与眼睑/下颌控制

## 1. 本讲目标

上一讲（u4-l1）我们拆开了 SMPL-X 这个「资产容器」，并埋下了一个伏笔：`smplx2flame_ind` 是通向「FLAME 换头」的桥。本讲就走到桥的另一端，专门拆解 FLAME 头部模型。学完本讲，你应该能够：

1. 说出 FLAME 参数字典中每个分量的维度与含义（shape 300 / expression 50 / global 3 / neck 3 / jaw 3 / eye 6 / eyelid 2）。
2. 说出 FLAME 模板顶点数（5023，加牙齿后 5143）与 5 个关节（root、neck、jaw、双眼）的运动学结构。
3. 解释 PEAR 为什么把 FLAME 的 global pose 和 neck pose 强行置零，只让下颌、眼球、眼睑动。
4. 理解 eyelid（眼睑）这种「线性位移基底 × 标量系数」的加法式细节控制，以及 add_teeth 如何手工造出 120 个牙齿顶点。
5. 独立写出一段脚本：零 shape/expression 下得到中性头，再分别只改 jaw_params 和 eyelid_params 观察形变。

## 2. 前置知识

### 2.1 FLAME 是什么

FLAME（Faces Learned with an Articulated Model and Environments）是马克斯·普朗克研究所提出的**参数化三维头部模型**，可以理解为「头部版的 SMPL」：给定一组低维系数，它输出一个完整的头部三角网格。它和 SMPL/SMPL-X 一样受许可证保护，模型文件需要手动下载（u1-l2 已讲）。

FLAME 的核心公式和 SMPL-X 同源，分三步：

1. **形状混合（shape blend）**：在模板顶点上加形状基底的线性组合

\[
v_{shaped} = v_{template} + \sum_{i=1}^{300}\beta_i S_i + \sum_{i=1}^{50}\psi_i E_i
\]

其中 \(\beta\) 是形状系数、\(\psi\) 是表情系数。FLAME 把「表情」也当作一种形状基底来加——这是它和 SMPL-X 的一个小差异。

2. **姿态混合（pose blend）**：关节旋转带来的 corrective 位移。

3. **线性混合蒙皮（LBS）**：按每个关节的蒙皮权重把顶点绑到运动学链上。这些步骤的完整推导在 u4-l3，本讲只需记住「lbs 输入参数、输出顶点和关节」。

### 2.2 为什么 PEAR 需要 FLAME

SMPL-X 自带的头部只有 10475 顶点中的一部分，且头部表情自由度很少（只有下颌 3 维 + 眼球 6 维，shape 里隐含一点头部形状）。想还原眯眼、张嘴、皱眉这些**面部细节**，就需要 FLAME 这种专门的头部模型。PEAR 的做法（u4-l4 会完整拆解）是：用 FLAME 生成一个精细的头，按 `smplx2flame_ind` 索引替换掉 SMPL-X 的头，再整体做一次 LBS。

### 2.3 承接 u4-l1 的两个结论

- `models/modules/` 下的模型类（SMPLX、FLAME）都是**纯资产容器**：张量注册为 buffer、无可学习参数、不进 checkpoint。
- EHM_v2 构造时 `SMPLX` 和 `FLAME` 各实例化一次，`SMPLX.add_teeth` 内部还会再实例化一次 FLAME——所以构造 EHM_v2 时 FLAME 的初始化代码实际跑了**两遍**，这也是构造慢的原因之一。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [models/modules/flame/FLAME.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py) | FLAME 模型类：资产加载、forward、add_teeth、FlameMask 区域掩码 |
| [models/modules/ehm/EHM_v2.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py) | PEAR 实际使用的 FLAME 前向分支（注意：它不调用 `FLAME.forward`，而是直接调 `lbs`） |
| [models/modules/flame/lbs.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py) | 蒙皮函数 `lbs`（返回 5 个值，本讲只看签名与调用） |
| [models/smplx/smplx_head.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py) | 网络侧：FLAME 参数由哪些解码器输出（衔接 u3-l3） |
| [models/modules/smplx/SMPLX.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py) | `smplx2flame_ind` 的加载与牙齿扩展（衔接 u4-l1） |
| [assets/FLAME/](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/assets/FLAME) | FLAME 资产目录（仓库自带掩码/眼睑/模板，只缺 generic_model.pkl） |

## 4. 核心概念与源码讲解

### 4.1 FLAME 资产加载

#### 4.1.1 概念说明

FLAME 类的 `__init__` 做的事情只有一件：把 `assets/FLAME` 目录下的一组文件读进来，变成 PyTorch buffer。它不做任何计算。要读懂它，关键是搞清楚**每个文件对应哪种数据**：

| 资产文件 | 内容 | 是否随仓库分发 |
| --- | --- | --- |
| `FLAME2020/generic_model.pkl` | 主模型：模板、shape/pose 基底、关节回归器、蒙皮权重 | 否，需手动下载 |
| `FLAME_masks/FLAME_masks.pkl` | 部位顶点掩码（face/neck/scalple/boundary/eyeball…） | 是 |
| `l_eyelid.npy` / `r_eyelid.npy` | 左右眼睑的位移基底（每个顶点一个 3D 向量） | 是 |
| `landmark_embedding.npy` | 静态/动态/全量 68 关键点的重心坐标嵌入 | 是 |
| `mediapipe_landmark_embedding.npz` | MediaPipe 风格关键点嵌入 | 是 |
| `203_landmark_embeding.npz` | 203 关键点嵌入（存在则启用） | 是 |
| `head_template.obj` | 头部拓扑与 UV（供渲染/掩码） | 是 |
| `selected_lowerhead.npy` | 下头部顶点索引（从 head_index 中剔除） | 是 |

#### 4.1.2 核心流程

```
读 generic_model.pkl（pickle → Struct 属性访问）
    ├─ faces / v_template / shapedirs / posedirs
    ├─ J_regressor / kintree_table(parents) / weights
    ├─ shapedirs 取前 300 列(shape) + 300~350 列(expression) 拼接
    └─ 注册为 buffer
读 FLAME_masks.pkl → non_head_index = neck ∪ boundary
读 l/r_eyelid.npy → 眼睑位移基底 buffer
eye_pose / neck_pose 固定为零的 Parameter（不可学习）
读 landmark 系列嵌入 → 各关键点 buffer
读 head_template.obj → UV、FlameMask
（可选）add_teeth()：模板从 5023 → 5143 顶点
```

#### 4.1.3 源码精读

**入口与环境补丁。** 文件顶部先把 `np.bool`、`np.int` 等 numpy 旧别名补回去——这是为了用新版 numpy 反序列化老版本 pickle（内含 chumpy 对象）时不报错，属于 u1-l2 讲过的环境兼容手段：

- [models/modules/flame/FLAME.py:19-25](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L19-L25)：numpy 兼容性 monkey-patch。

**构造函数签名。** PEAR 调用时只用前两个参数 + `add_teeth`：

- [models/modules/flame/FLAME.py:79](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L79)：`def __init__(self, flame_assets_dir, n_shape=300, n_exp=50, with_texture=False, add_teeth=False, ...)`。EHM_v2 以 `n_shape=300, n_exp=50, add_teeth=True` 调用（见 [models/modules/ehm/EHM_v2.py:19](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L19)）。

**主模型 pickle。** 用 `Struct(**ss)` 把 pickle 字典变成属性访问（`flame_model.v_template` 这种写法）：

- [models/modules/flame/FLAME.py:82-86](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L82-L86)：打开 `FLAME2020/generic_model.pkl` 并包成 `Struct`。这个文件是唯一需要手动下载的（[README.md:79](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L79) 要求把它同时放到 `assets/FLAME/FLAME2020/` 和 `assets/SMPLX/` 两处）。

**non_head_index。** 把 neck 与 boundary 两片顶点的索引拼起来，注册为 `non_head_index`。u4-l4 会看到：换头时这些顶点**不**用 FLAME 的结果，而是保留 SMPL-X 原顶点，用于缝合颈部接缝：

- [models/modules/flame/FLAME.py:88-91](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L88-L91)：加载 `FLAME_masks.pkl`，拼接 `neck + boundary` 得到 `non_head_index`。

**核心五件套。** 与 u4-l1 的 SMPL-X 完全同构，只是规模小得多：

- [models/modules/flame/FLAME.py:96-100](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L96-L100)：`faces_tensor`（[9976,3]）与 `v_template`（[5023,3]，打印语句确认用 generic 模型）。`n_ori_verts` 记下 5023。
- [models/modules/flame/FLAME.py:103-105](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L103-L105)：**shapedirs 的取列技巧**。原始 FLAME 的 shapedirs 是 [5023,3,400]：前 300 列是 identity shape 基底，第 300~350 列是表情基底。这里切出 `[:,:,:300]` 和 `[:,:,300:350]` 拼成 [5023,3,350]——「表情也是 betas 的一部分」在代码上就体现为这一行拼接。
- [models/modules/flame/FLAME.py:107-114](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L107-L114)：posedirs 重排为 [P, V*3]、`J_regressor`（[5,5023]，**只有 5 个关节**）、`parents`（`kintree_table[0]`，根节点置 -1）、`lbs_weights`（[5023,5]）。

**眼睑基底。** 两个 [1,5023,3] 的 buffer，每个分量是一个 3D 位移向量，乘一个标量系数就得到眼睑闭合一帧的位移（细节在 4.3 展开）：

- [models/modules/flame/FLAME.py:116-117](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L116-L117)：注册 `l_eyelid` / `r_eyelid`。

**眼球与颈部旋转的「钉死」。** eye_pose、neck_pose 被注册成 `requires_grad=False` 的零 Parameter——注释写着 "Fixing Eyeball and neck rotation"。注意这并不是 PEAR 的行为（PEAR 的眼球旋转是网络预测的、会传进 forward），而是从 DECA 原版继承的默认值机制；它们的实际作用是充当 forward 里缺失参数时的**默认零值来源**：

- [models/modules/flame/FLAME.py:120-125](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L120-L125)：`eye_pose` [1,6] 与 `neck_pose` [1,3] 固定为零。

**关键点嵌入与拓扑。** FLAME 内置了四套关键点嵌入（68 静态 + 动态轮廓、全量 68、MediaPipe、可选 203），以及从 obj 读入的 UV。本讲的实践不直接用它们，但 EHM_v2 输出的 landmark 用的则是 SMPL-X 侧的嵌入（见 u4-l4）：

- [models/modules/flame/FLAME.py:128-159](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L128-L159)：landmark 嵌入加载；仓库里 `203_landmark_embeding.npz` 存在，故 `using_lmk203=True`。
- [models/modules/flame/FLAME.py:175-187](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L175-L187)：`head_index` 默认全部顶点、再用 `selected_lowerhead.npy` 剔除下头部；随后 `load_obj` 读 `head_template.obj` 拿 UV，构造 `FlameMask`，最后按需调用 `add_teeth()`。

#### 4.1.4 代码实践

**实践目标**：亲手构造 FLAME，把 4.1.3 里提到的每个 buffer 形状打印出来，验证「资产容器」的说法。

**操作步骤**（示例代码，需在仓库根目录、u1-l2 建好的 pear 环境下运行）：

```python
# flame_inspect.py —— 示例代码
import torch
from models.modules.flame.FLAME import FLAME

flame = FLAME("assets/FLAME", n_shape=300, n_exp=50, add_teeth=True)

for name in ["v_template", "faces_tensor", "shapedirs", "posedirs",
             "J_regressor", "parents", "lbs_weights",
             "l_eyelid", "r_eyelid", "non_head_index"]:
    buf = getattr(flame, name)
    print(f"{name:15s} {tuple(buf.shape)}")

print("n_ori_verts =", flame.n_ori_verts)
print("head_index  :", flame.head_index.shape)
```

**需要观察的现象**：`v_template` 为 `(5143, 3)`（5023 + 120 牙齿）、`shapedirs` 为 `(5143, 3, 350)`、`J_regressor` 为 `(5, 5143)`、`lbs_weights` 为 `(5143, 5)`、`l_eyelid`/`r_eyelid` 为 `(1, 5143, 3)`、`non_head_index` 是一维索引向量。

**预期结果**：所有张量都是 buffer（`flame.v_template.requires_grad` 为 False），`sum(p.numel() for p in flame.parameters())` 应远大于 0 但全部来自 `eye_pose`/`neck_pose` 两个被钉死的 Parameter，且不可训练。若改用 `add_teeth=False` 构造，`v_template` 应回到 `(5023, 3)`。（待本地验证）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `shapedirs` 的最后一维是 350 而网络解码器 `flame_shape_decoder` 输出 300、`flame_expression_decoder` 输出 50？

**答案**：因为 [FLAME.py:103-105](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L103-L105) 把原始 400 列切成「300 shape + 50 expression」拼接，使用时 `betas = cat([shape, expression])` 恰好 350 维，与 shapedirs 的列数对齐。300 和 50 分别是 `n_shape`、`n_exp` 的默认值。

**练习 2**：`non_head_index` 由哪两片区域拼成？它会在哪个模块里被消费？

**答案**：neck 与 boundary 两片掩码（[FLAME.py:88-91](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L88-L91)）；在 EHM_v2.forward 换头时用于恢复 SMPL-X 原始的颈部/边界顶点（[EHM_v2.py:157](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L157)，u4-l4 精读）。

### 4.2 full_pose 拼接与 FLAME 前向

#### 4.2.1 概念说明

FLAME 的「姿态」由 5 个关节的轴角旋转组成，共 15 维，按固定顺序拼接：

| 分量 | 维度 | 关节 | PEAR 中的状态 |
| --- | --- | --- | --- |
| `pose_params`（global） | 3 | root（关节 0） | **被 EHM_v2 置零** |
| `neck_pose_params` | 3 | neck（关节 1） | **被 EHM_v2 置零** |
| `jaw_params` | 3 | jaw（关节 2） | 网络预测，生效 |
| `eye_pose_params` | 6 | 双眼球（关节 3、4） | 网络预测，生效 |

这 15 维全部来自网络解码头的一个 14 维线性层加上拆分（衔接 u3-l3）：

- [models/smplx/smplx_head.py:270-278](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/smplx_head.py#L270-L278)：`flame_poses_decoder = nn.Linear(dim, 14)`，切成 eye(6) + global(3) + jaw(3) + eyelid(2)。也就是说**网络确实预测了头部全局旋转**，但 EHM_v2 一律丢弃。

为什么丢弃？直觉是：换头之后，头部朝向由 **SMPL-X 身体姿态**驱动——替换进 SMPL-X 模板的头顶点会随后续的 `lbs_wobeta`（用 SMPL-X 的蒙皮权重）一起转动。如果 FLAME 侧再自带一个全局/颈部旋转，就会和身体侧的运动「转两次」，产生矛盾。所以 PEAR 只保留 FLAME 负责**脸部局部细节**的自由度：下颌、眼球、眼睑、表情。

#### 4.2.2 核心流程

以 PEAR 真正使用的 EHM_v2 FLAME 分支为准（不是 `FLAME.forward`，见 4.2.3 的警告）：

```
输入 flame_param_dict（6 键）
    ├─ shape_params 补零到 300 维（网络可输出不足 300 时）
    ├─ （可选）zero_expression / zero_jaw / zero_shape 开关
    ├─ neck_pose ← flame.neck_pose（零）扩展
    ├─ global_pose ← 全零          ★ 头部朝向交给身体侧
    ├─ betas      = cat([shape(300), expression(50)])  → [B,350]
    ├─ full_pose  = cat([global(3), neck(3), jaw(3), eye(6)]) → [B,15]
    ├─ template   = flame.v_template 扩展到 batch
    ├─ (verts, joints, J, T, A) = lbs(betas, full_pose, template, ...)
    ├─ verts += r_eyelid * eyelid[:,1] + l_eyelid * eyelid[:,0]   ★ LBS 之后
    ├─ ori_head_vertices = verts.clone()      # 缩放前快照
    └─ verts = verts * head_scale[:,None]     # 逐轴缩放（head_scale 为 [B,3]）
```

#### 4.2.3 源码精读

**参数解包。** EHM_v2.forward 的 FLAME 分支从 `flame_param_dict` 取 6 个键：

- [models/modules/ehm/EHM_v2.py:38-45](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L38-L45)：取出 `eye_pose_params`、`shape_params`、`expression_params`、`pose_params`（全局）、`jaw_params`、`eyelid_params`，并从 body 侧取 `head_scale`。

**补零与置零。** shape 不足 300 维则补零；三个 zero 开关供可视化/消融用；最关键的是下面两行强制置零：

- [models/modules/ehm/EHM_v2.py:51-59](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L51-L59)：shape 补零与 `zero_expression / zero_jaw / zero_shape` 开关。
- [models/modules/ehm/EHM_v2.py:62-65](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L62-L65)：`global_pose_params = torch.zeros_like(...)`、`neck_pose_params = torch.zeros_like(...)`——网络预测的头部全局旋转在这里被整体丢弃。

**拼接与前向。**

- [models/modules/ehm/EHM_v2.py:68-71](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L68-L71)：`betas = cat([shape, exp])` 得 [B,350]；`full_pose = cat([global, neck, jaw, eye])` 得 [B,15]（注释写 [1,15]）；模板扩展到 batch（注释写 [1,5023,3]，实际 add_teeth=True 下是 5143——注释漂移，以代码为准）。
- [models/modules/ehm/EHM_v2.py:73-76](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L73-L76)：调用 `lbs` 并解包 **5 个返回值** `head_vertices, head_joints, J, T, A`。`lbs` 的完整定义在 [models/modules/flame/lbs.py:142-230](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L142-L230)（`v_shaped` → 关节回归 → pose 位移 → 刚体变换 → 蒙皮，最后 `return verts, J_transformed, J, T, A`，详见 u4-l3）。

**眼睑与缩放。**

- [models/modules/ehm/EHM_v2.py:77-83](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L77-L83)：LBS **之后**把眼睑位移加上（细节见 4.3）；`ori_head_vertices` 保存缩放前快照（最终也会作为 `prediction['ori_head_vertices']` 输出）；`head_scale` 来自 body 侧的 6 维 scale 解码器后 3 维（[smplx_head.py:292-294](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L292-L294)），以 [B,1,3] 广播做**逐轴**缩放，用于吸收 FLAME 头与 SMPL-X 头的大小差异。

**⚠️ 重要警告：`FLAME.forward` 是一段「僵尸代码」。** FLAME 类自带一个看起来完整的 forward：

- [models/modules/flame/FLAME.py:271-312](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L271-L312)：`FLAME.forward` 做参数默认值补全、zero 开关、拼接 betas/full_pose、把眼睑位移加在**模板上（LBS 之前）**，然后在 [FLAME.py:309-312](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L309-L312) 以 `vertices, joints = lbs(...)` 只解包 **2 个**返回值。

而 `models/modules/flame/lbs.py` 的 `lbs` 返回 **5 个**值（[lbs.py:230](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L230)）。也就是说直接调用 `FLAME.forward` 大概率会抛出 `too many values to unpack`（待本地验证）。全仓库 grep 也找不到任何 `.flame(` 形式的调用——PEAR 从不使用它，而是像 EHM_v2 这样**inline 重写**了前向。这带来两个学习要点：

1. 读研究代码时，「类里有 forward」不等于「forward 被使用」；要以调用关系为准（u1-l3 的 import 可达性分析方法在这里同样适用）。
2. 两份实现的**眼睑施加时机不同**：`FLAME.forward` 是 LBS 前加在模板上（[FLAME.py:305-307](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L305-L307)，其下方 [FLAME.py:313-315](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L313-L315) 还留着被注释掉的 LBS 后版本），EHM_v2 是 LBS 后加——PEAR 的实际行为以后者为准。

#### 4.2.4 代码实践

**实践目标**：用零参数跑通 EHM_v2 风格的 FLAME 前向，得到「中性头」，并确认置零逻辑的效果。

**操作步骤**（示例代码）：

```python
# flame_neutral.py —— 示例代码：参考 EHM_v2.py:62-76 的分支
import torch
from models.modules.flame.FLAME import FLAME
from models.modules.flame.lbs import lbs

flame = FLAME("assets/FLAME", n_shape=300, n_exp=50, add_teeth=True)
B = 1
shape = torch.zeros(B, 300)
exp   = torch.zeros(B, 50)
betas = torch.cat([shape, exp], dim=1)                       # [1,350]
full_pose = torch.cat([
    torch.zeros(B, 3),    # global：EHM_v2.py:64 置零
    torch.zeros(B, 3),    # neck：  EHM_v2.py:65 置零
    torch.zeros(B, 3),    # jaw
    torch.zeros(B, 6),    # eye
], dim=1)                                                    # [1,15]
template = flame.v_template.unsqueeze(0).expand(B, -1, -1)

verts, joints, J, T, A = lbs(betas, full_pose, template,
                             flame.shapedirs, flame.posedirs,
                             flame.J_regressor, flame.parents,
                             flame.lbs_weights, dtype=flame.dtype)
print("verts ", tuple(verts.shape))    # 期望 (1, 5143, 3)
print("joints", tuple(joints.shape))   # 期望 (1, 5, 3)
print("head_joints:\n", joints[0])     # 关节 0=root, 1=neck, 2=jaw, 3/4=双眼
```

**需要观察的现象**：`verts` 形状 `(1, 5143, 3)`；由于 betas 和 full_pose 全零，`verts` 应与 `flame.v_template` 完全一致（可断言 `torch.allclose(verts[0], flame.v_template)`）；`joints` 只有 5 个。

**预期结果**：中性头 == 模板本身。这验证了「blend shapes 与 LBS 在零参数下是恒等变换」，也是后面综合实践的对照基准。（待本地验证）

#### 4.2.5 小练习与答案

**练习 1**：网络预测的 `pose_params`（头部全局旋转，3 维）最终去了哪里？

**答案**：被读出后在 [EHM_v2.py:64](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L64) 用 `torch.zeros_like` 整体覆盖，从未参与任何计算。头部朝向由 SMPL-X 身体姿态经后续 `lbs_wobeta` 蒙皮驱动（u4-l4）。

**练习 2**：`flame_param_dict` 一共 6 个键，其中 eyelid_params 是从哪个解码器的哪几维切出来的？

**答案**：从 14 维的 `flame_poses_decoder` 输出中切出第 12~14 维（[smplx_head.py:272-276](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L272-L276)）：eye 前 6 维、global 6:9、jaw 9:12、eyelid 12:14。

### 4.3 eyelid 与 teeth 细节

#### 4.3.1 概念说明

这一模块讲 FLAME 里两个「不走 LBS 主流程」的细节机制。

**眼睑（eyelid）——加法式细节控制。** 眨眼这种形变很难用关节旋转表达（眼睑没有关节），FLAME 的做法非常直接：预先离线算好「完全闭合」时每个顶点的位移向量 \(e_l, e_r \in \mathbb{R}^{V \times 3}\)，运行时乘一个标量系数加回去：

\[
v' = v + c_l \cdot e_l + c_r \cdot e_r,\quad c_l, c_r \in \mathbb{R}
\]

`eyelid_params` 是 [B,2]，第 0 维控左眼、第 1 维控右眼。它的优点是线性、稳定、好回归；代价是只能表达「闭合程度」这一种自由度。

**牙齿（teeth）——手工拓扑扩展。** 张嘴时嘴里空荡荡会穿帮，PEAR 给 FLAME 加了一副「程序化生成」的牙：不来自官方资产，而是**在初始化时从嘴唇参考点量出来的 120 个盒状顶点**，连同手写的三角面、UV、蒙皮权重一起拼进模板。上牙随颈关节动、下牙随下颌关节动，于是张嘴时牙会跟着开合。

#### 4.3.2 核心流程

```
add_teeth()（构造期，一次性）：
    从 FlameMask 取上/下唇外圈顶点索引
        ├─ 量唇间平均距离 mean_dist
        ├─ 构造牙列中面/上缘/下缘/牙根/背面共 8 组 × 15 = 120 个顶点
        ├─ v_template ← cat([v_template(5023), v_teeth(120)])  → 5143
        ├─ head_index 追加牙齿索引
        ├─ shapedirs：牙齿行 ← 上下唇 shapedirs 均值（有形状跟随）
        ├─ posedirs / eyelid / J_regressor：牙齿部分置零（不参与）
        ├─ lbs_weights：全零后 上牙+=关节1(neck)、下牙+=关节2(jaw)
        └─ faces：手写 112+ 个三角形 + linspace 生成的 UV 网格
```

对应地，SMPL-X 侧的 `add_teeth`（u4-l1 已讲）会把 SMPL-X 模板 10475 → 10595，并把 `smplx2flame_ind` 从 5023 → 5143（[SMPLX.py:535](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L535)），两侧的牙齿追加顺序一致，索引一一对应。

#### 4.3.3 源码精读

**眼睑在 PEAR 中的生效位置（LBS 之后）**：

- [models/modules/ehm/EHM_v2.py:77-79](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L77-L79)：`head_vertices += r_eyelid * eyelid_params[:, 1:2, None]`，再 `+= l_eyelid * eyelid_params[:, 0:1, None]`。`[:, None]` 把标量系数变成 [B,1,1] 以便广播到 [B,V,3]。注释里残留的 `[:, :self.flame.n_ori_verts]` 说明作者曾考虑过「加牙后眼睑基底多了 120 行要不要裁掉」的问题——最终按 add_teeth 里把牙齿行的 eyelid 置零处理（见下）。
- [models/modules/flame/FLAME.py:305-307](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L305-L307)：对照——`FLAME.forward` 中同样的加法发生在 LBS 之前的模板上；[FLAME.py:313-315](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L313-L315) 是被注释掉的 LBS 后版本。

**牙齿的几何构造——以嘴唇为标尺**：

- [models/modules/flame/FLAME.py:372-384](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L372-L384)：`add_teeth` 开头通过 `self.mask.get_vid_by_region` 取上下唇外圈顶点，算唇间平均距离 `mean_dist`，把牙列中面放在双唇中点、向内（z 方向）退 `mean_dist * 1.5`——注释明确写着 "how far the teeth are from the lips"。牙的高度、厚度也都是 `mean_dist` 的倍数。
- [models/modules/flame/FLAME.py:386-423](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L386-L423)：用 8 组（上/下 × 缘/根 × 前/背面）各 15 个顶点拼出 `v_teeth` [120,3]，拼进 `v_template` 并更新 `n_ori_verts`。每组 15 个顶点的注释（如 `num_verts_orig + 0-14`）就是后续索引的说明书。

**牙齿的蒙皮权重——谁跟谁动**：

- [models/modules/flame/FLAME.py:491-494](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L491-L494)：`lbs_weights` 先补 120 行零，然后 `vid_teeth_upper` 行的关节 1（neck）加 1、`vid_teeth_lower` 行的关节 2（jaw）加 1。效果：**上牙绑定颈部、下牙绑定下颌**，张嘴（jaw 旋转）时下牙随下颌转、上牙不动，正好模拟开合。

**牙齿对其它基底的处理**：

- [models/modules/flame/FLAME.py:467-477](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L467-L477)：牙齿行的 shapedirs 用上下唇 shapedirs 的均值填充——让牙齿「继承」嘴唇的形状形变，嘴的大小变化时牙跟着变。
- [models/modules/flame/FLAME.py:479-489](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L479-L489)：posedirs、eyelid 基底、J_regressor 的牙齿部分一律补零——牙齿不产生姿态校正位移、不参与眼睑闭合、不回归新关节。
- [models/modules/flame/FLAME.py:456-465](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L456-L465)：牙齿 UV 用 `torch.linspace` 生成 15×7 网格拼到 `verts_uvs` 后面；[FLAME.py:497-582](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L497-L582) 起是手写的上下牙三角面索引（本讲不逐行读）。

**FlameMask——按部位查顶点。** `get_vid_by_region(['lip_outside_ring_upper'])` 这类调用的后台：

- [models/modules/flame/FLAME.py:697-724](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L697-L724)：`FlameMask` 把 `FLAME_masks.pkl` 里的部位掩码（face/neck/scalp/boundary/eyeball/ear/forehead/lips…，见 [FLAME.py:742-756](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L742-L756)）逐个注册进一个 `BufferContainer`，并提供按区域取顶点/面片的方法。牙齿加完后还会注册 `teeth_upper`/`teeth_lower`/`teeth` 三个新掩码（[FLAME.py:441-443](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L441-L443)）。

#### 4.3.4 代码实践

**实践目标**：不改代码，仅通过两个「探针」验证 eyelid 与 teeth 机制的真实效果。

**操作步骤**（示例代码）：

```python
# flame_probe.py —— 示例代码
import torch
from models.modules.flame.FLAME import FLAME

flame = FLAME("assets/FLAME", add_teeth=True)

# 探针 1：眼睑基底到底动了哪些顶点？
disp = (flame.l_eyelid[0] + flame.r_eyelid[0]).norm(dim=-1)   # [5143]
moved = (disp > 1e-6).sum()
print(f"eyelid 基底影响的顶点数: {moved.item()} / {disp.numel()}")
print(f"位移最大值: {disp.max():.4f}")

# 探针 2：牙齿的蒙皮权重
w = flame.lbs_weights[-120:]            # 最后 120 行 = 牙齿
print("上牙示例 lbs_weights:", w[0].tolist())   # 期望 neck 位为 1
print("下牙示例 lbs_weights:", w[20].tolist())  # 期望 jaw 位为 1
print("普通头皮顶点权重:", flame.lbs_weights[0].tolist())
```

**需要观察的现象**：eyelid 基底只影响眼周一小片顶点（数量远小于 5143），其余行为 0；牙齿 120 行的蒙皮权重是 one-hot（上牙指向 neck、下牙指向 jaw）。

**预期结果**：`moved` 为几百到一两千量级（眼周区域）；牙齿权重向量的非零位置分别是索引 1 和索引 2。（待本地验证）

#### 4.3.5 小练习与答案

**练习 1**：eyelid 基底是 [1,5143,3]，加了牙齿之后多出的 120 行是怎么处理的？如果置零，加法 `verts + eyelid * c` 会不会把牙齿顶点弄坏？

**答案**：[FLAME.py:484-486](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/FLAME.py#L484-L486) 给 eyelid 基底补了 120 行零，所以牙齿行的位移恒为 0，加法不会影响牙齿。

**练习 2**：为什么上牙绑 neck（关节 1）而不是 root（关节 0）？绑 root 行不行？

**答案**：功能上绑 root 也能让上牙随头整体动（root 是链根，所有关节都继承它的变换）。但 neck 是离头部最近的「稳定参照」，且 FLAME 的 pose corrective（posedirs）按「非根关节」的旋转差计算；更重要的是下牙必须绑 jaw 才能开合，上牙绑 neck 使上下牙在「头转动」时同步、在「张嘴」时分离——绑 root 时如果 root 有旋转同样同步，两种选法在 PEAR 里等价（global/neck 都被置零，头部朝向实际由 SMPL-X 侧蒙皮决定）。这是个体会「权重设计意图」的练习，不必死记结论。

## 5. 综合实践

**任务**：把 4.2.4 的中性头脚本扩展成一个「下颌 vs 眼睑」对照实验，产出三张对比图。这正是本讲规格里指定的实践任务。

**操作步骤**（示例代码，保存为 `flame_ablation.py`，在仓库根目录运行）：

```python
# flame_ablation.py —— 示例代码：参考 EHM_v2.py:38-83 的 FLAME 分支
import torch, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from models.modules.flame.FLAME import FLAME
from models.modules.flame.lbs import lbs

flame = FLAME("assets/FLAME", n_shape=300, n_exp=50, add_teeth=True)
B = 1

def head(jaw=None, eyelid=None):
    """零 shape/expression，只允许 jaw / eyelid 非零。"""
    betas = torch.zeros(B, 350)                      # shape300+exp50 全零
    full_pose = torch.cat([
        torch.zeros(B, 3),                           # global（EHM_v2 置零）
        torch.zeros(B, 3),                           # neck（EHM_v2 置零）
        jaw    if jaw    is not None else torch.zeros(B, 3),
        torch.zeros(B, 6),                           # eyeball
    ], dim=1)
    tpl = flame.v_template.unsqueeze(0)
    verts, joints, *_ = lbs(betas, full_pose, tpl,
                            flame.shapedirs, flame.posedirs,
                            flame.J_regressor, flame.parents,
                            flame.lbs_weights, dtype=flame.dtype)
    if eyelid is not None:                           # EHM_v2.py:77-79 的顺序
        verts = verts + flame.r_eyelid * eyelid[:, 1:2, None]
        verts = verts + flame.l_eyelid * eyelid[:, 0:1, None]
    return verts[0]

neutral = head()
jaw_open = head(jaw=torch.tensor([[0.6, 0.0, 0.0]]))     # 分量/正负可实验
blink    = head(eyelid=torch.tensor([[1.0, 1.0]]))

def plot(v, title, path):
    fig = plt.figure(figsize=(5, 5)); ax = fig.add_subplot(projection="3d")
    ax.scatter(v[:, 0], v[:, 1], v[:, 2], s=0.3, c=v[:, 1],
               cmap="viridis")                    # 按高度着色，便于观察
    ax.set_title(title); ax.set_axis_off()
    fig.savefig(path, dpi=200, bbox_inches="tight"); plt.close()

plot(neutral,  "neutral",           "flame_neutral.png")
plot(jaw_open, "jaw open",          "flame_jaw.png")
plot(blink,    "eyelids closed",    "flame_blink.png")

# 量化差异：哪些顶点动得最多？
for name, v in [("jaw", jaw_open), ("blink", blink)]:
    d = (v - neutral).norm(dim=-1)
    idx = d.argmax()
    print(f"{name}: 最大位移 {d.max():.4f} @ 顶点 {idx.item()}, "
          f"位移>1e-4 的顶点数 {(d > 1e-4).sum().item()}")
```

**需要观察的现象**：

1. 三张散点图整体轮廓几乎一致，只有局部区域不同。
2. `jaw` 组的量化输出应集中在**下颌/下唇/下牙**顶点（含最后 120 个牙齿顶点中的下牙部分），位移量与 `0.6` 这个轴角大小成正比。
3. `blink` 组的位移应集中在**眼周**顶点，与 4.3.4 探针 1 打印的眼睑影响范围一致；由于眼睑是纯加法位移，幅度通常比下颌旋转小。
4. 若把 `jaw` 的 `0.6` 换到第 2、3 个分量，或改符号，张嘴方向会不同——轴角三个分量分别对应绕 x/y/z 的旋转，具体哪个分量是「张嘴」请以本地观察为准（待本地验证）。

**预期结果**：你能直观看到「下颌控制的是刚性旋转 + corrective 位移（作用在下巴一片顶点上）」与「眼睑控制的是加法位移（作用在眼皮一片顶点上）」这两种机制的差别——这正是 4.3.1 的结论。

**思考延伸**：把 `jaw_open` 与 `blink` 同时设非零，验证两种形变可叠加（线性近似）；再试着给 `betas` 的前几维加非零值，观察「头型」整体变化如何区别于局部形变。

## 6. 本讲小结

- FLAME 是「头部版 SMPL-X」：`generic_model.pkl` 的五件套（模板/shape 基底/pose 基底/关节回归器/蒙皮权重）+ 眼睑基底 + 部位掩码，全部注册为 buffer，零可学习参数；模板 5023 顶点、5 关节（root/neck/jaw/双眼），`add_teeth=True` 后 5143 顶点。
- PEAR 的 FLAME 前向以 EHM_v2 的 inline 分支为准：`betas = cat([shape(300), exp(50)])`、`full_pose = cat([global(3), neck(3), jaw(3), eye(6)])`，其中 **global 与 neck 被强制置零**——头部朝向交给 SMPL-X 身体侧蒙皮，FLAME 只负责脸部局部细节。
- `FLAME.forward` 是僵尸代码：它对 `lbs` 做 2 值解包而 `lbs` 返回 5 个值，且全仓库无人调用；它的眼睑施加时机（LBS 前）也与 EHM_v2（LBS 后）不同。
- 眼睑 = 加法式细节控制：`verts += c_l·l_eyelid + c_r·r_eyelid`，系数来自 14 维 flame_poses 解码器的最后 2 维。
- 牙齿 = 手工拓扑扩展：以唇间距离为标尺程序化生成 120 顶点，上牙绑 neck、下牙绑 jaw 实现开合，shapedirs 继承嘴唇、其余基底置零；SMPL-X 侧同步扩展使 `smplx2flame_ind` 达到 5143。
- 读研究代码要警惕「注释漂移」（EHM_v2 里 [1,5023,3]、[1,15] 等注释与 add_teeth 后的真实形状不符）和「僵尸 forward」——一切以调用关系和实际形状为准。

## 7. 下一步学习建议

本讲我们多次把 `lbs` 当黑盒调用（输入 betas/full_pose、输出 verts/joints/J/T/A）。下一讲 **u4-l3（LBS 蒙皮：lbs / lbs_wobeta / lbs_get_transform）** 会打开这个黑盒：`blend_shapes` 的线性公式、`batch_rodrigues` 轴角转旋转矩阵、`batch_rigid_transform` 的前向运动学推导，以及 PEAR 定制的 `lbs_wobeta`（先做 shape blend 再蒙皮，正是换头流程需要的形式）与 `lbs_get_transform`（输出逐顶点变换矩阵）。读完 u4-l3 再进入 u4-l4（EHM_v2 换头融合），你会看到本讲的 `head_vertices` 如何被平移对齐（FLAME 双眼中点 → SMPL-X 双眼中点）后嵌回身体模板。

建议同步阅读：[models/modules/flame/lbs.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py) 中 `lbs` 与 `lbs_wobeta` 的并排对比，以及 FLAME 官方论文中关于 pose blend shapes 的章节（仓库 README 参考文献列表第 [1](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L153) 条）。
