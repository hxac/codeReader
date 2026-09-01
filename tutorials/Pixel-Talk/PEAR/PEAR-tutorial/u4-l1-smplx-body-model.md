# SMPL-X：参数化人体模型的加载与模板

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 SMPL-X 参数化人体模型的「模板 + 形状基底 + 姿态基底 + 关节回归器 + 蒙皮权重」五件套各自的作用与张量形状。
2. 走读 `models/modules/smplx/SMPLX.py` 的 `__init__`，说清它从 `assets/SMPLX` 下读入了哪些文件、哪些是 git 里有的、哪两个必须手动下载。
3. 区分仓库里两个同名的 `SMPLX` 类（`models/modules/smplx/SMPLX.py` 与 `models/smplx/SMPLXV2.py`），并知道 PEAR 的 `EHM_v2` 实际用的是哪一个、用到什么程度（只用张量，不调 forward）。
4. 独立使用 `blend_shapes` 与 `vertices2joints` 两个函数：给一组随机 betas，把模板变成「成形身体」，再回归出 55 个关节并检查坐标范围。
5. 理解 `smplx2flame_ind`、`uvmap_f_idx`、`query_lbs` 这类索引缓冲的用途，为下一讲的 FLAME 头替换和 EHM 融合打底。

## 2. 前置知识

**参数化人体模型（parametric human body model）**。把它想象成一台「参数 → 网格」的机器：输入一组低维参数（体型、姿态、表情），输出一个由几万个顶点、几万个三角面组成的人体网格。SMPL-X 是 MPI 发布的统一模型，同时覆盖身体、手和脸。它内部由五块常量数据构成：

- **模板（template）** \( T \in \mathbb{R}^{10475 \times 3} \)：一个中性姿态、中性体型的「平均人体」顶点云；
- **形状基底（shapedirs）** \( S \in \mathbb{R}^{10475 \times 3 \times B_s} \)：每列是一个「体型变化方向」（高矮胖瘦），系数就是常说的 betas；
- **姿态基底（posedirs）**：关节旋转带来的肌肉形变方向；
- **关节回归器（J_regressor）** \( R \in \mathbb{R}^{55 \times 10475} \)：从顶点线性回归出 55 个关节的三维坐标；
- **蒙皮权重（lbs_weights）** \( W \in \mathbb{R}^{10475 \times 55} \)：每个顶点受哪些关节旋转的影响、影响多大（线性混合蒙皮，LBS，详见 u4-l3）。

**register_buffer 与 nn.Parameter 的区别**。`nn.Parameter` 是可训练参数，会被 optimizer 更新；`register_buffer` 注册的是常量张量，不参与训练，但会跟着 `.to(device)` 一起搬家、会进 `state_dict`（除非 `persistent=False`）。SMPL-X 的资产全部是常量，所以这个类里几乎全是 buffer。

**Structured 资产文件**。`.npz` 是 numpy 的打包格式（一组命名数组），`.pkl` 是 Python pickle。代码里用一个 5 行的 `Struct` 类把它们变成可以用点号访问的对象（见 4.1.3）。

**承接上一讲**：u2-l5 已经拆过「图像 patch → 参数字典」这段；本讲开始拆「参数字典 → 10475 顶点网格」这段。你要记住的衔接点是：网络的 `body_param` 里的 `shape`/`body_pose`/`left_hand_pose` 等键，最终都会喂给本讲的这些张量。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `models/modules/smplx/SMPLX.py` | **本讲主角**。PEAR 实际使用的 SMPL-X 容器类：加载全部资产、注册全部 buffer，还带一套 UV/牙齿扩展工具函数 |
| `models/smplx/SMPLXV2.py` | 另一个同名 `SMPLX` 类，自带投影与关键点输出的「旧版自包含实现」；当前仓库没有真正的调用方 |
| `models/modules/ehm/EHM_v2.py` | SMPLX 类的唯一实际消费者（作为张量容器），本讲会反复引用它的构造与 forward 片段 |
| `models/modules/flame/lbs.py` | `blend_shapes` / `vertices2joints` / `lbs_wobeta` 等函数的真实定义处（EHM_v2 从这里 import） |
| `models/modules/smplx/lbs.py` | `Struct` / `to_tensor` / `to_np` 工具与另一份同名 LBS 实现 |
| `assets/SMPLX/` | 14 个 git 内资产 + 2 个需手动下载的模型文件（见 4.1） |

## 4. 核心概念与源码讲解

### 4.1 SMPLX 初始化与资产加载

#### 4.1.1 概念说明

先建立一个关键认知：**在 PEAR 里，`SMPLX` 这个类不是网络，而是一个「资产容器」**。

它继承 `nn.Module`，但没有任何可学习参数。它的工作是：构造时把 `assets/SMPLX` 目录下十几个二进制文件一次性读进内存，转换成 float32/int64 张量并注册成 buffer。`EHM_v2` 构造它之后，只「借」它的张量（`self.smplx.v_template`、`self.smplx.shapedirs`……），在 EHM_v2 自己的 forward 里调用外部函数做计算——**EHM_v2 从头到尾没有调用过 `self.smplx(...)` 这个 forward**（用 grep 可以验证，`self.smplx.` 后面跟的全是属性访问）。这也解释了 u1-l3 提过的现象：EHM_v2 不进 checkpoint，因为它没有可学习参数。

第二个关键认知：**仓库里有两个叫 `SMPLX` 的类**，这是初学者最容易踩的坑：

| | `models/modules/smplx/SMPLX.py` | `models/smplx/SMPLXV2.py` |
| --- | --- | --- |
| 被 EHM_v2 引用方式 | `from ..smplx import SMPLX`（EHM_v2.py 第 5 行） | `from models.smplx.SMPLXV2 import SMPLX as SMPLX_v2`（第 6 行） |
| 是否被真正使用 | ✅ 作为张量容器 | ❌ import 之后全仓库再无引用，属「未使用 import」 |
| forward 内容 | 参数 → 顶点/关节（带 eyelid、head_scale 等扩展） | 参数 → 顶点/关节 **+ 弱透视投影 + 关键点输出**，自带三套投影函数 |
| 独有内容 | UV 工具、add_teeth、smplx2flame 索引、拉普拉斯矩阵 | `batch_orth_proj` / `batch_persp_proj` 等投影函数 |

记住方向：`models/modules/smplx`（官方资产层）≠ `models/smplx`（可学习解码头所在包，u3-l3 讲的 `smplx_head.py` 也在这个包里）。

#### 4.1.2 核心流程

`SMPLX.__init__` 的执行流程（自上而下）：

1. 读 `SMPLX_NEUTRAL_2020.npz` → `Struct` 对象 `smplx_model`（模板、基底、回归器等全在里面）；
2. 读 `flame_generic_model.pkl` → `Struct` 对象 `flame_model`（只为了取 FLAME 的面片 `f`，供头部关键点插值用）；
3. 把 `smplx_model` 的各字段转成张量、注册 buffer（faces / v_template / shapedirs / posedirs / J_regressor / parents / lbs_weights / landmark 索引）；
4. 注册一组「默认参数」buffer（全零 shape、单位旋转姿态），供 forward 里缺参时兜底；
5. 加载 J14 关节回归器、FLAME/MANO 顶点对应索引、eyelid 偏移、mediapipe/203 关键点嵌入；
6. 计算头部与双手的中心点（head_center 等，用于后续 head/hand 缩放）；
7. 加载 UV 相关：uv_mask、逐像素 LBS 权重、UV 展开网格 obj、UV 图上的面片索引与重心坐标；
8. 计算 V×V 拉普拉斯矩阵（正则化用）；
9. 若 `add_teeth=True`，追加 120 个牙齿顶点（EHM_v2 默认开启）；
10. `get_head_idx_from_pos()` 按 y 坐标阈值标记头部顶点。

#### 4.1.3 源码精读

**构造签名与两个必下载文件**：

[models/modules/smplx/SMPLX.py:L127-L141](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L127-L141) —— 构造函数签名（`n_shape=200, n_exp=50, add_teeth=False, uv_size=512` 是类默认值），随后用 `np.load` 读 `SMPLX_NEUTRAL_2020.npz`、用 `pickle.load(..., encoding='latin1')` 读 `flame_generic_model.pkl`，各自包成 `Struct`。

注意：这两个文件**不在 git 仓库里**（`git ls-files assets/SMPLX/` 可以验证，u1-l2 也讲过），因为它们受 SMPL-X / FLAME 许可证保护，必须手动下载后放进 `assets/SMPLX/`。仓库自带的其余 14 个文件（`SMPL-X__FLAME_vertex_ids.npy`、`lbs_map_smplx_512.npy`、`smplx_uv.obj` 等）都会被 `__init__` 直接用到。

`Struct` 与类型转换工具定义在：

[models/modules/smplx/lbs.py:L447-L459](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/lbs.py#L447-L459) —— `Struct` 只是把 dict 变成可用点号访问的对象；`to_np` 里专门处理了 `scipy.sparse`（npz 里的 `J_regressor` 原始形态是稀疏矩阵，`todense()` 后再转 `np.array`）。

**模型主体张量的注册**：

[models/modules/smplx/SMPLX.py:L144-L160](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L144-L160) —— 这 17 行注册了 LBS 需要的全部核心张量：`faces_tensor`（三角面）、`v_template`（模板）、`shapedirs`（形状基底）、`posedirs`（姿态基底，注意被 reshape 成了 `[J*9, V*3]` 的二维布局）、`J_regressor`、`parents`（第 158 行把根关节的父索引改成 -1）、`lbs_weights`。

其中 shapedirs 的拼接值得细看：

[models/modules/smplx/SMPLX.py:L150-L152](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L150-L152) —— SMPL-X 2020 版 npz 里的 `shapedirs` 有 400 列：前 300 列是体型基底、后 100 列是表情基底（与 FLAME 表情空间兼容，第 149 行注释点明了这一点）。这里取「前 `n_shape` 列 + 第 300 到 `300+n_exp` 列」拼起来。**类默认 `n_shape=200`，但 EHM_v2 传进来的是 300**（EHM_v2 自己的默认值），所以 PEAR 实际使用时 `shapedirs` 是 `[10475, 3, 350]`（300 体型 + 50 表情）。

**默认参数兜底**：

[models/modules/smplx/SMPLX.py:L171-L181](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L171-L181) —— 一组零值 shape/expression 和单位旋转姿态（`torch.eye(3)` 重复若干次）。写法上有点绕：`nn.Parameter(..., requires_grad=False)` 再套一层 `register_buffer`，效果等价于注册了一个常量。forward 里若某个参数键缺失就用它们补齐。

**跨模型索引的加载**：

[models/modules/smplx/SMPLX.py:L189-L218](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L189-L218) —— 依次加载：`SMPLX_to_J14.pkl`（14 个常用关节的回归器，用于覆盖回归出的关节位置，L195-L205 还根据 `SMPLX_names` 名字表算出 source/target 索引）、`SMPL-X__FLAME_vertex_ids.npy`（**SMPL-X 头部区域顶点 ↔ FLAME 顶点的对应关系，本讲最重要的索引，4.2 详述**）、左右眼睑偏移、mediapipe 关键点嵌入、`MANO_SMPLX_vertex_ids.pkl`（SMPL-X ↔ MANO 手部顶点对应）。

注意 L208：`self.smplx2flame_ind = np.load(...)` 是**普通 numpy 数组属性，不是 buffer**——所以它不会跟着 `.to(device)` 移动，但用 numpy 整数数组给 torch 张量做花式索引是合法的（EHM_v2.py 第 154 行就是这么用的）。

**EHM_v2 如何构造它**：

[models/modules/ehm/EHM_v2.py:L14-L19](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L14-L19) —— `EHM_v2(flame_assets_dir, smplx_assets_dir)` 内部用默认 `n_shape=300, n_exp=50, add_teeth=True` 调 `SMPLX(...)`，同时构造一个 FLAME。三个推理入口的调用方式完全一致：`EHM_v2("assets/FLAME", "assets/SMPLX")`（如 [inference_wo_detect.py:L66](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L66)）。这也再次说明：`assets/SMPLX` 下必须同时有 `flame_generic_model.pkl`（FLAME 模型要存两份，u1-l2 讲过原因）。

**旧版 SMPLXV2 长什么样（对照阅读）**：

[models/smplx/SMPLXV2.py:L283-L304](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/SMPLXV2.py#L283-L304) —— 它的 forward 签名带 `pose_type` / `proj_type`，说明它是一个「参数 → 网格 → 投影 → 2D/3D 关键点」的全流程封装；[models/smplx/SMPLXV2.py:L216-L281](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/SMPLXV2.py#L216-L281) 是它的三套投影函数（正交 / 弱透视转透视 / 透视修正）。在 PEAR 现架构里，投影职责已经移交给 u3-l4 讲过的 `GS_Camera` 体系，这个类便闲置了。读它的价值在于对照理解「旧版把相机放进人体模型、新版把相机拆出来」的架构演化。

#### 4.1.4 代码实践：亲眼看到资产里有什么

**实践目标**：确认资产就位、观察构造耗时，并打印 npz 里的原始键名，建立「文件 → Struct 字段 → buffer」的映射感。

**操作步骤**（示例代码，保存为仓库根目录下的 `inspect_smplx_assets.py` 并运行；不要改动任何源码文件）：

```python
# 示例代码：python inspect_smplx_assets.py
import time
import numpy as np
from models.modules.smplx import SMPLX

# 1) 先看 npz 里到底装了什么（此步只依赖 SMPLX_NEUTRAL_2020.npz）
ss = np.load('assets/SMPLX/SMPLX_NEUTRAL_2020.npz', allow_pickle=True)
print('npz keys:', sorted(ss.files))

# 2) 构造（add_teeth=False 先避开 FLAME 依赖，聚焦 SMPL-X 本体）
t0 = time.time()
smplx = SMPLX('assets/SMPLX', n_shape=300, n_exp=50, add_teeth=False)
print(f'构造耗时 {time.time() - t0:.1f} s')

# 3) 确认它没有任何可学习参数
print('可训练参数量:', sum(p.numel() for p in smplx.parameters()))
```

**需要观察的现象**：

- npz 键列表里应出现 `v_template`、`shapedirs`、`posedirs`、`J_regressor`、`kintree_table`、`weights`、`f` 等（完整列表以本地输出为准，待本地验证）；
- 构造耗时明显偏慢（十几秒到几十秒量级），而且期间内存占用会明显上升——原因见 4.3 的 `generate_position_map` 纯 Python 双循环和 V×V 拉普拉斯矩阵；
- 可训练参数量为 0。

**预期结果**：如果 `SMPLX_NEUTRAL_2020.npz` 或 `flame_generic_model.pkl` 没放好，第 2 步会直接抛 `FileNotFoundError`，报错路径会告诉你缺哪个文件；两个文件就位后，第 3 步输出 `0`。

#### 4.1.5 小练习与答案

**练习 1**：为什么这个类里几乎全用 `register_buffer` 而不是 `nn.Parameter`？

**答案**：SMPL-X 的模板、基底、回归器、蒙皮权重都是「资产常量」，不参与训练、不需要梯度；`register_buffer` 让它们随 `.to(device)` 移动并进入 `state_dict`，但不会被 optimizer 意外更新。这也与「EHM_v2 不进 checkpoint」的结论自洽——整个对象里没有可学习参数。

**练习 2**：`assets/SMPLX` 下哪两个文件不在 git 里？为什么？

**答案**：`SMPLX_NEUTRAL_2020.npz` 和 `flame_generic_model.pkl`。前者是 SMPL-X 官方模型（受 MPG 许可证保护），后者是 FLAME 官方模型（同样受许可证保护），都不能随仓库分发，需要按 README（u1-l2）指引手动下载。其余 14 个辅助文件（索引、UV、obj）都在 git 里。

**练习 3**：`EHM_v2.py` 第 6 行 import 了 `SMPLX_v2`，这个类在 PEAR 里被用到了吗？

**答案**：没有。`EHM_v2.py` 第 6 行 `from models.smplx.SMPLXV2 import SMPLX as SMPLX_v2` 之后，全文件（乃至全仓库）再无 `SMPLX_v2` 的引用，是一次「未使用 import」。真正被使用的是第 5 行 `from ..smplx import SMPLX`，即 `models/modules/smplx/SMPLX.py`。

### 4.2 核心 buffer 形状

#### 4.2.1 概念说明

这一节回答一个具体问题：**「一个 SMPL-X」在显存里到底由哪些张量、什么形状构成？** 把这张表记牢，后面读 EHM_v2 的 forward 时，所有注释里的形状就都能对上号了。

先给两个基础公式（本讲只用到它们，完整 LBS 留到 u4-l3）：

形状混合（blend shapes）：

\[ V_{shaped} = T + \sum_{l=1}^{B_s} \beta_l \, S_{:, :, l} \]

即：模板加上「每个体型方向 × 对应系数」的线性组合。50 个表情系数走的是同一条公式——它们只是拼在 betas 后面的第 301~350 维，共享同一组 shapedirs。

关节回归（joints regression）：

\[ J = R \, V_{shaped}, \quad R \in \mathbb{R}^{55 \times 10475} \]

即：每个关节坐标是全部顶点坐标的加权和（回归器的每一行是一组权重）。骨盆大约在原点附近，头在最上方——这就是「模板坐标系」。

#### 4.2.2 核心流程

以 PEAR 实际构造参数（`n_shape=300, n_exp=50, add_teeth=True`）为准的 buffer 清单：

| buffer / 属性 | 形状 | dtype | 用途 |
| --- | --- | --- | --- |
| `v_template` | `[10475, 3]`（+120 牙 → `[10595, 3]`） | float32 | 中性人体模板 |
| `faces_tensor` | `[20908, 3]`（+168 牙面 → `[21076, 3]`） | int64 | 三角面片 |
| `shapedirs` | `[10475, 3, 350]` | float32 | 300 体型 + 50 表情基底（+120 行零） |
| `posedirs` | `[486, 10475*3]` | float32 | 姿态基底，`(55-1)*9=486` 行 |
| `J_regressor` | `[55, 10475]` | float32 | 顶点 → 55 关节 |
| `parents` | `[55]` | int64 | 运动树父索引，`parents[0] = -1` |
| `lbs_weights` | `[10475, 55]` | float32 | 逐顶点蒙皮权重（+120 牙行） |
| `smplx2flame_ind` | `[5023]`（+120 → `[5143]`） | int64（numpy） | SMPL-X 头部顶点 ↔ FLAME 顶点对应 |
| `face_l_eyelid` / `face_r_eyelid` | `[1, 5023, 3]` | float32 | 眨眼方向的逐顶点偏移 |
| `smplx2mano_ind` | dict（`left_hand` / `right_hand`） | — | SMPL-X ↔ MANO 手部顶点对应 |
| `extra_joint_regressor` | `[14, 10475]` | float32 | J14 常用关节回归器 |
| `head_center` / `left_hand_center` / `right_hand_center` | `[3]` | float32 | 头/手中心，供缩放用 |
| `laplacian_matrix`（及 negate_diag 变体） | `[V, V]` | float32 | 网格拉普拉斯，正则化用，`persistent=False` |
| `uvmap_f_idx` / `uvmap_f_bary` / `uvmap_mask` | `[512,512]` / `[512,512,3]` / `[512,512]` | int32 / float32 / bool | UV 图上每像素所属面片及重心坐标 |

几个可以用文件字节数自行验证的数字（`.npy` 文件头按 128 字节估算）：

- `smplx_faces.npy` 大小 251,024 B：(251024 − 128) / 4 / 3 = **20908** 个面片（int32 存储）；
- `SMPL-X__FLAME_vertex_ids.npy` 大小 40,312 B：(40312 − 128) / 8 = **5023** 个索引（int64），恰好等于 FLAME 模板顶点数（EHM_v2.py 第 154 行注释 `[B,5023,3]` 可佐证）；
- `lbs_map_smplx_512.npy` 大小 57,671,808 B：(57671808 − 128) / 4 = 512 × 512 × 55，即每个 UV 像素 55 维蒙皮权重。

#### 4.2.3 源码精读

**核心张量注册**（与 4.1.3 同一段，此处看形状细节）：

[models/modules/smplx/SMPLX.py:L144-L160](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L144-L160) —— `posedirs` 在第 154-156 行被 reshape 成 `[-1, num_pose_basis].T`，从 npz 里的 `[10475, 3, 486]` 变成 `[486, 10475*3]`，这是为了 forward 里一次矩阵乘就能算出全部顶点的姿态偏移。`parents` 取自 `kintree_table[0]` 并把根关节置 -1。

**smplx2flame_ind 与三个中心点**：

[models/modules/smplx/SMPLX.py:L231-L236](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L231-L236) —— 用 `smplx2flame_ind` 从模板里取出头部顶点求均值得到 `head_center`，用 `smplx2mano_ind` 取双手顶点均值得到左右手中心。这三个中心点是为「头/手局部缩放」准备的：缩放公式 `v * s + (1-s) * center` 保证缩放以该部位中心为不动点（modules 版 SMPLX 的 forward 第 370-381 行正是这么用的）。

**blend_shapes 与 vertices2joints 的定义**（EHM_v2 实际 import 的版本）：

[models/modules/flame/lbs.py:L360-L381](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L360-L381) —— `blend_shapes` 用一个 einsum 实现 4.2.1 的公式：`'bl,mkl->bmk'`，betas 与 shapedirs 最后一份量相乘求和。

[models/modules/flame/lbs.py:L340-L357](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L340-L357) —— `vertices2joints` 用 einsum `'bik,ji->bjk'` 实现关节回归，一行完成 \( J = R V \)。

**EHM_v2 里的实际调用（本讲公式的落点）**：

[models/modules/ehm/EHM_v2.py:L123-L140](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L123-L140) —— 第 123-126 行：网络输出的 body shape 只有 200 维，这里补零到 300（注释「后面 100 维度不重要」指的是 300 维体型空间里网络只学前 200 维）；第 128 行拼上 50 维表情成 350 维 `shape_components`；第 139 行 `template_vertices + blend_shapes(...)` 得到成形身体 `new_template_vertices`；第 140 行 `vertices2joints` 回归出 T-pose 关节 `tbody_joints`。随后第 154-158 行就用 `smplx2flame_ind` 把头部顶点换成 FLAME 头（下一讲展开）。

**add_teeth 对形状的影响**：

[models/modules/smplx/SMPLX.py:L509-L521](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L509-L521) —— 在上下唇中点附近手工摆出 120 个牙齿顶点（8 组 × 15 个），追加到 `v_template` 末尾：10475 → 10595。EHM_v2.py 第 24 行的注释 `# [10595,3]` 佐证了这一点。

[models/modules/smplx/SMPLX.py:L547-L570](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L547-L570) —— 与新顶点配套，所有逐顶点张量都要扩行：`shapedirs` 补零行（但第 548-556 行让牙齿的体型基底取上下唇基底的均值，使牙齿随嘴唇体型走）、`posedirs`/`J_regressor` 补零、`lbs_weights` 补零后，第 569-570 行给上牙绑定关节 12（neck）、下牙绑定关节 22（jaw）——张嘴时下牙才会跟着下颌动。最后第 571-744 行手工写出 168 个牙面三角形（84 上 + 84 下），第 744 行拼进 `faces_tensor`。

**一个「注释漂移」实例**：EHM_v2.py 第 146-147 行注释写着 `shapedirs[10475, 3, 20]`——`20` 是老 SMPL 的 betas 数，与实际的 350 列不符（u3-l3 讲过这类注释漂移，读代码时要以下文表格中的真实形状为准）。

#### 4.2.4 代码实践：给模板「穿体型」并回归关节

**实践目标**：亲手完成「betas → 成形身体 → 55 关节」这条最小链路，并把 4.2.2 的表格逐行验证一遍。

**操作步骤**（示例代码，保存为 `inspect_smplx_buffers.py`，在仓库根目录运行）：

```python
# 示例代码：python inspect_smplx_buffers.py
import torch
from models.modules.smplx import SMPLX
from models.modules.flame.lbs import blend_shapes, vertices2joints  # 与 EHM_v2 同源

smplx = SMPLX('assets/SMPLX', n_shape=300, n_exp=50, add_teeth=False)

# 1) 逐项打印核心 buffer 的形状与 dtype
for name in ['v_template', 'faces_tensor', 'shapedirs', 'posedirs',
             'J_regressor', 'parents', 'lbs_weights', 'flame_faces_tensor',
             'extra_joint_regressor', 'head_center']:
    t = getattr(smplx, name)
    print(f'{name:24s} {str(tuple(t.shape)):20s} {t.dtype}')

# smplx2flame_ind 是 numpy 属性而不是 buffer，单独看
ind = smplx.smplx2flame_ind
print(f'smplx2flame_ind           {ind.shape}            {ind.dtype} {type(ind).__name__}')

# 2) 随机 betas 施加体型，再回归关节
torch.manual_seed(0)
betas = torch.randn(1, 350) * 0.5           # 300 shape + 50 exp，最后一维须等于 shapedirs 的列数
v_shaped = smplx.v_template[None] + blend_shapes(betas, smplx.shapedirs)
joints = vertices2joints(smplx.J_regressor, v_shaped)

print('v_shaped:', tuple(v_shaped.shape))    # 期望 (1, 10475, 3)
print('joints  :', tuple(joints.shape))      # 期望 (1, 55, 3)
for i, axis in enumerate('xyz'):
    print(f'{axis}: [{joints[0, :, i].min():+.3f}, {joints[0, :, i].max():+.3f}]')

# 3) 零 betas 对照组：应当几乎还原模板本身
v_zero = smplx.v_template[None] + blend_shapes(torch.zeros(1, 350), smplx.shapedirs)
print('零 betas 与模板的最大误差:', (v_zero - smplx.v_template[None]).abs().max().item())
```

**需要观察的现象**：

- 第 1 步打印出的形状应与 4.2.2 表格逐行一致（`add_teeth=False` 时是未扩牙的版本：`v_template (10475, 3)`、`shapedirs (10475, 3, 350)`、`posedirs (486, 31425)`、`J_regressor (55, 10475)`、`lbs_weights (10475, 55)`）；
- 关节坐标三个轴的量级在「米」这个数量级内，且头部关节的 y 值明显为正、脚部明显为负（具体数值待本地验证）；
- 第 3 步的最大误差应为 0（零系数的线性组合就是模板本身）。

**预期结果**：`v_shaped` 与 `joints` 形状如注释所示；把 `betas` 的尺度从 0.5 改成 2.0 重跑，关节坐标范围会明显变大（体型变化更剧烈），这能直观感受 betas 的语义。

#### 4.2.5 小练习与答案

**练习 1**：`add_teeth=True` 之后，`v_template`、`lbs_weights`、`J_regressor`、`smplx2flame_ind` 各变成什么形状？

**答案**：`v_template` 10475→10595（+120 牙）；`lbs_weights` `[10475,55]`→`[10595,55]`（牙行先补零，再把上牙权重给关节 12、下牙给关节 22）；`J_regressor` `[55,10475]`→`[55,10595]`（补零列，即牙齿不参与关节回归）；`smplx2flame_ind` 5023→5143（追加 120 个牙顶点索引，使 FLAME 侧的牙也能写回 SMPL-X 网格）。

**练习 2**：为什么牙齿的 `shapedirs` 不补零，而是取上下唇基底的均值？

**答案**：牙齿长在嘴唇后面，脸型（比如嘴部厚薄、下颌长短）变化时牙齿应随之移动。补零意味着牙齿永远停留在模板位置，张嘴或胖瘦变化时会穿模；取上下唇体型基底的均值是一种廉价的近似绑定（见 SMPLX.py 第 548-556 行）。

**练习 3**：网络输出的 body shape 是 200 维，`shapedirs` 却有 300 个体型列，这 100 维差距是怎么处理的？为什么不直接用 200 列的 shapedirs？

**答案**：EHM_v2.forward 第 123-126 行把 200 维补零到 300 维再参与混合。保留 300 列是因为 FLAME 侧（下一讲）按 300 维体型空间建模，EHM_v2 让身体与头部共用同一套 300/50 的维度约定，代码里只需一处补齐。

### 4.3 UV 与索引工具

#### 4.3.1 概念说明

除了 LBS 五件套，SMPL-X 类在构造时还准备了一整套**「UV 空间」数据**。UV 是把三维网格表面展开到二维方形图上的坐标（纹理贴图就靠它）。PEAR 把人体网格摊到一张 512×512 的 UV 图上后，就可以把「逐顶点的操作」变成「逐像素的操作」——例如给每个 UV 像素缓存一份蒙皮权重（`query_lbs`），或按 UV 像素所在的 y 坐标挑出头部区域（`get_head_idx_from_pos`）。

需要说明的是：这一组数据（`query_lbs`、`position_map`、`uv_coord_map` 等）在本仓库的推理与训练主链路里**没有被后续代码读取**（grep 可验证），属于为逐像素/高斯类扩展准备的能力，目前「备而未用」。但它们解释了两件读者必然遇到的事：为什么 SMPLX 构造慢、为什么构造期间内存峰值高。读懂它们也是读懂 PEAR 论文标题里 "Pixel-aligned" 工程基础的一环。

#### 4.3.2 核心流程

UV 数据的装配流程：

1. 读 `uv_masks/uv_mask512_with_faceid_smplx.npy`：512×512 整数图，每个像素记录它属于哪个面片（-1 为背景）；
2. 读 `smplx_faces.npy`，把「像素 → 面片 id」翻译成「像素 → 三个顶点索引」（`flist_uv`）；
3. 读 `lbs_map_smplx_512.npy`：每个 UV 像素的 55 维蒙皮权重，只保留有效像素（`query_lbs`）；
4. 解析 `smplx_uv.obj`：得到 UV 展开后的纹理坐标 `texcoords` 与「面片 → 纹理坐标」索引 `faces_uv_idx`；
5. 用 OpenCV 把每个 UV 三角形光栅化进 512×512 索引图（`uvmap_f_idx`），再算每个像素在所属三角形内的重心坐标（`uvmap_f_bary`）与有效掩码（`uvmap_mask`）；
6. 逐像素填一张「位置图」`position_map`（该像素对应面片的中心坐标）；
7. `get_head_idx_from_pos`：把模板中 y > 0.15 的顶点、以及 UV 图上面片中心 y > 0.15 的像素分别标成头部索引。

#### 4.3.3 源码精读

**UV 掩码与逐像素面片表**：

[models/modules/smplx/SMPLX.py:L782-L795](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L782-L795) —— `load_masks` 读入 `uv_mask512_with_faceid_smplx.npy`（每像素面片 id）与 `smplx_faces.npy`，`get_face_per_pixel`（第 764-780 行）把背景像素的 -1 归零后做花式索引，得到 `flist_uv`：每个有效像素对应三角形的三个顶点索引；同时返回有效像素布尔掩码 `points_idx_from_posmap`。

**逐像素蒙皮权重缓存**：

[models/modules/smplx/SMPLX.py:L238-L245](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L238-L245) —— 第 239-241 行加载 57MB 的 `lbs_map_smplx_512.npy`，reshape 成 `[512*512, 55]` 后只保留有效像素并扩成 `[1, N_valid, 55]` 的 `query_lbs`：这是「UV 图上每个像素的 LBS 权重」，即 4.2 表格里 `lbs_weights` 的像素版。第 245 行调用 `generate_position_map` 生成位置图。

**构造慢的元凶**：

[models/modules/smplx/SMPLX.py:L797-L830](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L797-L830) —— `generate_position_map` 是一个 512×512 的纯 Python 双重 for 循环，逐像素取三角形三顶点求均值。这就是 4.1.4 实践里构造耗时的主要来源之一。同类的还有 [L883-L908](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L883-L908) 的 `get_uvmap_faces_barycoord`（逐像素叉积算重心坐标）。

**UV 图面片索引与重心坐标**：

[models/modules/smplx/SMPLX.py:L873-L881](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L873-L881) —— `get_uvmap_faces_index` 用 `cv2.drawContours` 把每个 UV 三角形以「面片编号」为灰度值画进 512×512 图（`-1` 填充初始化背景），得到 `uvmap_f_idx`；配合 L883 的重心坐标 `uvmap_f_bary`，任意 UV 像素都能以 `(面片 id, 重心坐标)` 二元组唯一定位网格上的一个点——这正是「像素对齐」的基础原语。

**UV obj 解析**：

[models/modules/smplx/SMPLX.py:L916-L945](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L916-L945) —— `OBJLoader` 逐行解析 `smplx_uv.obj` 的 `v` / `vt` / `f` 三类语句（obj 的索引从 1 开始，第 942 行减 1 转成 0 基）。第 247-252 行把 `texcoords`（UV 纹理坐标）与 `faces_uv_idx`（面片 → 纹理坐标索引）注册为 buffer，第 250 行 `texcoords[:,1]=1-texcoords[:,1]` 翻转 v 轴，对齐图像坐标系的 y 向下约定。

**按几何位置挑头部**：

[models/modules/smplx/SMPLX.py:L421-L434](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/smplx/SMPLX.py#L421-L434) —— `get_head_idx_from_pos` 用一个朴素阈值 `y > 0.15` 在模板空间（`head_idxs_temp`）与 UV 像素空间（`head_idxs_uv_flat`，借助 `uvmap_f_idx`/`uvmap_f_bary` 先算每个像素的面片中心）各标出一套头部索引。EHM_v2 构造时（[EHM_v2.py:L281-L294](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L281-L294)）在替换过 FLAME 头的模板上又跑了一遍同名方法——阈值法简单粗暴，但依赖「模板站姿、头朝上」这个先验。

#### 4.3.4 代码实践：数一数 UV 图上的有效像素

**实践目标**：验证 UV 数据的形状与「有效像素」概念，直观感受网格到 UV 图的覆盖密度。

**操作步骤**（示例代码，接在 4.2.4 的脚本后面，或单独运行）：

```python
# 示例代码（续前一个脚本，smplx 已构造）
for name in ['uvmap_f_idx', 'uvmap_f_bary', 'uvmap_mask', 'texcoords', 'faces_uv_idx']:
    t = getattr(smplx, name)
    print(f'{name:14s} {str(tuple(t.shape)):18s} {t.dtype}')

valid = smplx.uvmap_mask.sum().item()
print(f'有效 UV 像素: {valid} / {512*512} = {valid / (512*512):.1%}')
print('query_lbs:', tuple(smplx.query_lbs.shape))          # (1, N_valid, 55)
print('position_map:', tuple(smplx.position_map.shape))    # (512, 512, 3)
```

**需要观察的现象**：`uvmap_mask` 为 True 的像素比例（即人体网格在 512×512 UV 图上的覆盖率）；`query_lbs` 的第二维应恰好等于有效像素数（因为它就是按 `valid_idx` 筛过的）；`position_map` 中无效像素是全零。

**预期结果**：五个张量的形状与 dtype 与 4.2.2 表格一致；覆盖率是一个小于 100% 的合理比例（UV 展开必有空隙，具体数值待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`uvmap_f_idx` 里的 `-1` 是什么意思？它是怎么来的？

**答案**：表示该 UV 像素不属于任何面片（背景）。`get_uvmap_faces_index` 先用 `-1` 把整张 512×512 图填充，再用 `cv2.drawContours` 把每个三角形画成自己的面片编号，没被任何三角形覆盖的像素保持 -1；对应的布尔掩码就是 `uvmap_mask`。

**练习 2**：为什么说 `query_lbs` 是 4.2 表格里 `lbs_weights` 的「像素版」？

**答案**：`lbs_weights` 是 `[10475, 55]`，给每个**顶点**一份对 55 个关节的蒙皮权重；`query_lbs` 是 `[1, N_valid, 55]`，给 UV 图上每个**有效像素**一份同样的权重（来自 `lbs_map_smplx_512.npy`）。有了它，任何附着在 UV 图上的逐像素量（颜色、高斯）都能不走顶点插值直接做 LBS。

**练习 3**：`get_head_idx_from_pos` 为什么可以直接用 `y > 0.15` 这种阈值挑头部？什么情况下会失效？

**答案**：因为模板固定是标准站姿、骨盆在原点附近、头在最高处，模板坐标系里头部顶点的 y 值天然落在某个区间之上，阈值 0.15 是经验值。它只在「模板空间」成立——对任意姿态下的预测网格（人弯腰、倒立）就失效了，所以它只在构造期对模板用一次，不用于运行期。

## 5. 综合实践

**任务：写一个「SMPL-X 体检脚本」`smplx_checkup.py`，把本讲三块内容串起来。**

要求脚本依次完成：

1. **资产体检**：构造 `SMPLX('assets/SMPLX', n_shape=300, n_exp=50, add_teeth=False)`，打印构造耗时与可训练参数量（应为 0）；
2. **形状体检**：把 4.2.2 表格里的每个张量打印成「名称 / 形状 / dtype」清单，与表格逐行比对；
3. **功能体检**：随机 betas（尺度 0.5）经 `blend_shapes` 得到 `v_shaped`，再用 `vertices2joints` 回归 55 个关节，打印三轴坐标范围；再用零 betas 跑一遍，断言 `v_shaped` 与 `v_template` 完全一致（最大误差为 0）；
4. **导出验证**：把 `v_template` 与 `faces_tensor` 写成一个 `neutral.obj`（obj 索引从 1 开始，写 face 行时顶点编号要 +1），文件大小应约在 1~2 MB 量级；用任意网格查看器（如 Windows 3D 查看器、Blender、`trimesh`）打开，应看到一个中性站姿人体；
5. **对照实验**：把 `add_teeth` 改为 `True` 重新构造（此时需要 `assets/FLAME` 也就位），重复第 2 步，观察 `v_template`、`faces_tensor`、`smplx2flame_ind` 三者的形状变化是否等于 4.2.5 练习 1 的答案。

完成标志：形状清单全部对上、零 betas 断言通过、`neutral.obj` 能被打开渲染、`add_teeth` 两版形状差恰为 +120 / +168 / +120。第 4、5 步的具体数值与渲染效果待本地验证。

## 6. 本讲小结

- PEAR 实际使用的 SMPL-X 类是 `models/modules/smplx/SMPLX.py`；`models/smplx/SMPLXV2.py` 是自带投影的旧版封装，当前仓库没有真正调用它（EHM_v2 里的 import 未使用）。
- SMPL-X 在显存里就是一张「常量张量表」：模板 `[10475,3]`、面片 `[20908,3]`、形状基底 `[10475,3,350]`（300 体型 + 50 表情）、关节回归器 `[55,10475]`、蒙皮权重 `[10475,55]`，全部是 buffer、零可学习参数，因此不进 checkpoint。
- \( V_{shaped} = T + \sum_l \beta_l S_l \) 与 \( J = R V_{shaped} \) 两个线性公式就是 EHM_v2.forward 第 139-140 行的全部数学；EHM_v2 只借 SMPLX 的张量、不调它的 forward。
- `smplx2flame_ind`（5023 个索引）是身体与头部两个世界的桥：EHM_v2 用它定位 SMPL-X 头部顶点，下一讲 FLAME 头就是沿它「换头」的；`smplx2mano_ind` 同理服务双手。
- `add_teeth=True`（EHM_v2 默认）会把模板扩成 10595 顶点、21076 面片，并给牙齿手工分配蒙皮权重（上牙随 neck、下牙随 jaw）。
- 构造期慢、内存高，主要来自 512×512 纯 Python 双循环的 `position_map`、逐像素重心坐标，以及两张 V×V 的拉普拉斯矩阵；`query_lbs` / `position_map` 等逐像素数据在当前主链路中备而未用。

## 7. 下一步学习建议

下一讲（u4-l2）转向另一个参数化模型 **FLAME**：`models/modules/flame/FLAME.py` 如何加载 `assets/FLAME`、shape/expression/jaw/eye/eyelid 参数各控制什么、`add_teeth` 在 FLAME 侧怎么生成牙齿模板。读完它你就能完整理解 EHM_v2 构造函数第 21-25 行那段「换头」代码。之后再进入 u4-l3 的 LBS 蒙皮数学（`lbs_wobeta`、`batch_rigid_transform`），并最终在 u4-l4 把身体与头在 EHM_v2.forward 里会师。建议预习时先自己读一遍 [EHM_v2.py:L14-L32](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L14-L32)，带着「每个张量从哪来」的问题进入下一讲。
