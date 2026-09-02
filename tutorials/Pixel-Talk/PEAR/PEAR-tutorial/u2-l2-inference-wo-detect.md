# 单人推理全链路：inference_wo_detect.py 逐行走读

## 1. 本讲目标

读完本讲，你应该能够：

1. 从第 1 行到最后一行走读 [inference_wo_detect.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py)，说清每一段在做什么。
2. 解释图像如何从磁盘上的一张原图，变成形状为 `(1, 3, 256, 256)` 的模型输入张量（`pad_and_resize` + `to_tensor`）。
3. 说明 `ehm(outputs['body_param'], outputs['flame_param'])` 这一行如何把两个参数字典变成 10475 个顶点的网格。
4. 解释 `GS_Camera` 的构造参数（`focal_length=24`、`image_size=1024`）从哪里来、为什么渲染器和相机必须用同一套数值。
5. 解释 `mesh_*.jpg` 与 `result_*.jpg` 两张输出图分别是怎么写出来的，以及 `alpha=0.5` 混合的数学含义。

本讲是「走读型」讲义：不新增任何概念框架，而是把 [u1-l4](u1-l4-first-inference-run.md) 里概括的「读配置 → 建渲染器 → 下权重 → 建模型 → 前向 → 渲染 → 落盘」套路在**一个最短的脚本里逐行展开**。理解了这个最短链路，后面多人版和 Gradio 版只是在外围加壳。

## 2. 前置知识

本讲默认你已读过：

- [u1-l1](u1-l1-project-overview.md)：PEAR 的输入是 256×256 人体 patch，输出是 `body_param` / `flame_param` / `pd_cam` 三个字段。
- [u1-l4](u1-l4-first-inference-run.md)：三个推理入口共享同一套组装套路。
- [u2-l1](u2-l1-config-system.md)：`ConfigDict` + `add_extra_cfgs` 如何把 `configs/infer.yaml` 读成配置对象。

再用三段话补齐本讲会用到的术语：

**张量维度约定。** PyTorch 图像张量通常写成 `(B, C, H, W)`：批大小、通道、高、宽。而 OpenCV 读进来的 `numpy` 图像是 `(H, W, C)` 且通道顺序是 **BGR**（蓝绿红）。从一种表示变到另一种需要「维度重排 + 数值缩放到 0~1」，这正是预处理做的事。

**letterbox（信箱式）等比缩放。** 神经网络需要固定尺寸输入，但原图宽高比千差万别。直接拉伸会把人压扁变形；等比缩放后再用黑边补齐到正方形，就叫 letterbox——像老电影在宽屏电视上下加黑边。

**alpha 混合（alpha blending）。** 把两张同尺寸图像 \( M \)（前景）和 \( B \)（背景）按权重线性叠加：

\[
\text{result}(x) = \alpha \cdot M(x) + (1-\alpha) \cdot B(x), \quad \alpha \in [0, 1]
\]

\( \alpha=0.5 \) 即各占一半；\( \alpha \) 越大网格越「实」，越小越「透」。对应 `cv2.addWeighted(M, alpha, B, 1-alpha, 0)`。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [inference_wo_detect.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py) | 主角：无检测单人推理入口，仅 133 行 | 全部行 |
| [models/pipeline/ehm_pipeline.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py) | 可学习网络：ViT 骨干 + 解码头 | `forward` 里的归一化与裁剪 |
| [models/modules/ehm/EHM_v2.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py) | 参数 → 网格的统一人体模型 | `forward` 的整体数据流（细节留到单元四） |
| [utils/pipeline_utils.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/pipeline_utils.py) | 通用工具 | `to_tensor` |
| [utils/graphics_utils.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py) | 相机与渲染基础设施 | `GS_Camera`、`GS_BaseMeshRenderer.render_mesh` |
| [models/modules/renderer/body_renderer.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/renderer/body_renderer.py) | `Renderer2` 渲染器 | 拓扑加载与转发 |
| [models/smplx/smplx_head.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py) | 解码头（单元三精读） | 只看输出字典与 `pd_cam` 的构造 |

注意路径陷阱（[u1-l3](u1-l3-repo-structure-map.md) 已强调）：`models/smplx/` 是**可学习的解码头**，`models/modules/smplx/` 才是**参数转网格的官方模型层**，两回事。

---

## 4. 核心概念与源码讲解

先给一张全景图。`inference()` 函数从 [inference_wo_detect.py:46](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L46) 开始，整体分两大块：

```text
┌─ 一次性初始化（循环外）───────────────────────────┐
│ ① ConfigDict 读 infer.yaml + add_extra_cfgs      │ L49-L53
│ ② Renderer2 渲染器（assets/SMPLX, 1024, f=24）    │ L54
│ ③ hf_hub_download 下载 pear_model.pt             │ L56-L58
│ ④ Ehm_Pipeline + 加载 backbone/head 权重 + cuda   │ L59-L63
│ ⑤ EHM_v2（assets/FLAME, assets/SMPLX）+ 灯光     │ L66-L68
└──────────────────────────────────────────────────┘
┌─ 逐图主循环 ──────────────────────────────────────┐
│ ⑥ 读图 → pad_and_resize(256) → to_tensor          │ L77-L83   【模块一】
│ ⑦ ehm_model(img_patch) → outputs 参数字典         │ L85       【模块二】
│ ⑧ ehm(body_param, flame_param) → 顶点/关节        │ L86       【模块二】
│ ⑨ pad_and_resize(1024) 备好背景图                 │ L88       【模块三】
│ ⑩ GS_Camera + render_mesh → 网格图                │ L91-L94   【模块三】
│ ⑪ 保存 mesh_*.jpg；提亮背景并 alpha 混合          │ L96-L111  【模块三】
└──────────────────────────────────────────────────┘
```

下面按三个最小模块展开。

### 4.1 模块一：图像预处理 —— pad_and_resize 与 to_tensor

#### 4.1.1 概念说明

网络输入必须是固定尺寸，且 PEAR 训练时用的是「单人居中」的 256×256 人体 patch（见 [u1-l1](u1-l1-project-overview.md)）。本脚本名中的 `wo_detect`（without detection）意味着：**不做人体检测，直接假设画面里只有一个大致居中的人**。因此预处理策略非常简单——把整张图 letterbox 到 256×256：

- **等比缩放**：取 `min(target/h, target/w)` 作为缩放系数，保证人体不变形；
- **居中填充**：短边方向用黑色（`np.zeros`）补齐到正方形。

这里有两点值得注意：

1. **256×256 进、256×192 用**。`pad_and_resize` 产出的是正方形，但 `Ehm_Pipeline.forward` 里会再裁掉左右各 32 列（见 4.2.3），实际入网的是 256×192——即 8:6 的长宽比，与训练 patch 一致。letterbox 产生的左右黑边恰好常落在被裁掉的区域里。
2. **通道顺序没有转 RGB**。`cv2.imread` 读入的是 BGR，脚本在喂给网络前**没有**做 BGR→RGB 转换（对比 [app.py:240](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L240) 读帧后做了 `cv2.COLOR_BGR2RGB`）。两个入口行为不一致，是一处值得留意的历史痕迹；对结果的实际影响程度**待本地验证**。

#### 4.1.2 核心流程

`pad_and_resize(img, target_size)` 的执行过程（原图高宽为 `h, w`）：

```text
scale = min(target/h, target/w)          # 等比缩放系数，取较小者
new_w, new_h = int(w*scale), int(h*scale)
resized = cv2.resize(img, (new_w, new_h))  # 双线性插值
padded = 全零 (target, target, 3) 画布      # 黑底
x_offset = (target - new_w) // 2           # 水平居中
y_offset = (target - new_h) // 2           # 垂直居中
padded[y:y+new_h, x:x+new_w] = resized     # 贴入画布中央
```

随后的张量化（[inference_wo_detect.py:81-82](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L81-L82)）：

```text
resized (256,256,3) uint8 numpy, 值域 [0,255], BGR
   │ to_tensor(x, 'cuda:0')        → cuda 上的 torch 张量（形状不变）
   │ img_patch / 255               → float, 值域 [0,1]
   │ permute(2,0,1)                → (3,256,256)
   │ unsqueeze(0)                  → (1,3,256,256)
   ▼
img_patch: (B=1, C=3, H=256, W=256)，模型输入
```

注意 `/255` 是在 permute 的同一行里顺带做的；真正的均值方差归一化（ImageNet 统计量）发生在 `Ehm_Pipeline.forward` 内部，不在脚本里。

#### 4.1.3 源码精读

**letterbox 实现**：[inference_wo_detect.py:25-34](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L25-L34) —— 计算等比缩放系数、双线性缩放、建黑底画布、按居中偏移贴入。默认形参是 `target_size=512`，但主循环里两处调用分别显式传了 256（模型输入，L80）和 1024（渲染背景，L88）。

**类型统一工具**：[utils/pipeline_utils.py:62-88](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/pipeline_utils.py#L62-L88) —— `to_tensor` 把 `torch.Tensor` / `numpy.ndarray` / `List` 统一搬到指定 device。对 `numpy` 走 `torch.from_numpy(x).to(device)`。它**不做**维度重排、**不做**归一化，只管类型和设备。

**主循环里的预处理三行**：[inference_wo_detect.py:79-83](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L79-L83) —— `cv2.imread` 读图（BGR）；`pad_and_resize(img, target_size=256)` 得到 letterbox 图；`to_tensor` 上 GPU 后除以 255、重排维度、加 batch 维。注释 `# (B, C, H, W)` 标出了最终形状。

**图像清单收集**：[inference_wo_detect.py:70-75](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L70-L75) —— 按扩展名过滤输入目录下的图片并 `sorted` 排序，保证按文件名顺序逐张处理。

#### 4.1.4 代码实践

**实践目标**：亲眼确认 letterbox 的黑边位置与宽高比保持，为后面理解「256×256 进、256×192 用」打基础。

**操作步骤**（以下为**示例代码**，非仓库原有文件，可在仓库根目录新建临时脚本运行，用完删除，不要提交）：

```python
# demo_letterbox.py（示例代码）
import numpy as np, cv2
from inference_wo_detect import pad_and_resize

img = np.full((400, 200, 3), 200, dtype=np.uint8)   # 一张 400 高 × 200 宽的灰图
out = pad_and_resize(img, target_size=256)
print(out.shape)                                    # (256, 256, 3)

# 找出黑边列的范围：列全零即 padding
col_black = np.all(out == 0, axis=(0, 2))
print("黑边列数 =", col_black.sum())                # 预期 256 - int(200*256/400) = 256-128 = 128
```

**需要观察的现象**：输出恒为 `(256, 256, 3)`；竖长图（h>w）黑边出现在**左右**两侧、总宽 128 列。

**预期结果**：`scale = min(256/400, 256/200) = 0.64`，`new_w = 128`，`new_h = 256`，`x_offset = (256-128)//2 = 64`——即左右各 64 列黑边。而 `forward` 里 `x[:, :, :, 32:-32]` 每侧只裁 32 列，**裁不完黑边**；也就是说对竖长图，网络会看到部分黑边。这一点对推理质量的实际影响**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：一张 1920×1080 的横图经 `pad_and_resize(img, 256)` 后，黑边出现在哪、有多宽？
**答案**：`scale = min(256/1080, 256/1920) = 256/1920 ≈ 0.1333`，缩放后为 256×144，上下共补 `256-144=112` 行黑边（上下各 56 行）。横图黑边在上下，竖图黑边在左右。

**练习 2**：如果把 `pad_and_resize` 里的 `min` 改成 `max`，会发生什么？
**答案**：缩放系数变大，较长边会被截断——`new_w` 或 `new_h` 会超过 `target_size`，随后 `padded_img[y_offset:y_offset+new_h, x_offset:x_offset+new_w] = resized` 的切片赋值只会保留画布内的部分，等于把人体边缘裁掉而非补黑边，且宽高比信息被破坏。

**练习 3**：为什么脚本里 `/255` 之后还要在 `forward` 里再做一次 ImageNet 归一化？两次分别解决什么？
**答案**：`/255` 把像素值从 uint8 值域 `[0,255]` 线性压到 float 的 `[0,1]`，是数值表示层面的规整；ImageNet 归一化 `(x-mean)/std` 是把分布对齐到预训练骨干（ViTPose 系）见过的数据分布。前者是「单位换算」，后者是「分布对齐」，缺一不可。

---

### 4.2 模块二：模型与权重加载 —— Ehm_Pipeline、pear_model.pt 与 EHM_v2

#### 4.2.1 概念说明

这个脚本要装配**三件套**，职责各不相同：

| 部件 | 是什么 | 可学习参数 | 数据来源 |
| --- | --- | --- | --- |
| `Ehm_Pipeline`（ehm_model） | ViT 骨干 + SMPLX 解码头 | 有 | `pear_model.pt`（HuggingFace 自动下载） |
| `EHM_v2`（ehm） | 参数 → 网格的统一人体模型 | 无（只注册 buffer） | `assets/FLAME`、`assets/SMPLX` 资产文件 |
| `Renderer2`（body_renderer） | 网格 → 图像的渲染器 | 无 | `assets/SMPLX/smplx_tex.obj` 拓扑 |

三个关键设计：

1. **权重自动下载**。`hf_hub_download` 从 HuggingFace 仓库 `BestWJH/PEAR_models` 拉 `pear_model.pt`，本地有缓存则直接复用——用户无需手动下权重。
2. **`strict=False` 加载**。checkpoint 里只有 `backbone` 和 `head` 两段 state dict（见 [inference_wo_detect.py:61-62](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L61-L62)），且骨干沿用 ViTPose 预训练结构，键未必一一对应；`strict=False` 允许"能对上的就载入"，代价是**载入不完全也不会报错**——研究代码里方便但需自担风险的写法。
3. **EHM_v2 不进 checkpoint**。它由本地资产文件构造，模板、蒙皮权重等都是 buffer，所以 `pear_model.pt` 里没有它。

#### 4.2.2 核心流程

前向一圈的数据流（形状以单人 `B=1` 为例）：

```text
img_patch (1,3,256,256) 值域[0,1]
  │ Ehm_Pipeline.forward:
  │   ① ImageNet normalize
  │   ② x[:,:,:,32:-32] → (1,3,256,192)        # 裁掉左右各32列
  │   ③ backbone(x) → 特征图                     # ViT, cfg.BACKBONE
  │   ④ head(feats) → outputs dict
  ▼
outputs = {
  'body_param' : {shape(1,200), exp(1,50), global_pose(1,1,3,3),
                  body_pose(1,21,3,3), left/right_hand_pose(1,15,3,3),
                  hand_scale(1,3), head_scale(1,3), ...}
  'flame_param': {shape_params(1,300), expression_params(1,50),
                  eye_pose_params(1,6), pose_params(1,3),
                  jaw_params(1,3), eyelid_params(1,2)}
  'pd_cam'     : (1,4,4)  外参 RT 矩阵
}
  │ ehm(outputs['body_param'], outputs['flame_param'])
  ▼
pd_smplx_dict = {
  'vertices': (1,10475,3), 'joints': (1,145,3),
  'ver_transform_mat': 逐顶点变换矩阵, 'ori_head_vertices': (1,5023,3)
}
```

#### 4.2.3 源码精读

**配置与渲染器**：[inference_wo_detect.py:49-54](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L49-L54) —— `ConfigDict(model_config_path='configs/infer.yaml')` 读配置（[u2-l1](u2-l1-config-system.md) 已拆解），`add_extra_cfgs` 补写附加段；`BodyRenderer` 是 `Renderer2` 的别名（L7 的 `from ... import Renderer2 as BodyRenderer`），构造参数 `("assets/SMPLX", 1024, focal_length=24.0)` 分别指定拓扑资产目录、渲染画布边长、焦距。**这里的 1024 和 24.0 两个数后面还会再出现两次，必须处处一致**（见模块三）。

**权重下载与加载**：[inference_wo_detect.py:56-63](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L56-L63) —— 从 `BestWJH/PEAR_models` 下载 `pear_model.pt`；`torch.load(..., map_location='cpu', weights_only=True)` 安全反序列化到 CPU；随后 `backbone` 与 `head` 各自 `load_state_dict(..., strict=False)`；最后整体 `.cuda()`。

**EHM_v2 与灯光**：[inference_wo_detect.py:66-68](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L66-L68) —— 用 `assets/FLAME`、`assets/SMPLX` 两个目录构造（资产放置要求见 [u1-l2](u1-l2-environment-and-assets.md)）；`PointLights` 在相机前方 `(0,-1,-10)` 处放一盏灯供 Phong 着色使用。

**网络内部的归一化与裁剪**：[models/pipeline/ehm_pipeline.py:46-50](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py#L46-L50) —— `forward` 三步：`self.normalize(x)`（ImageNet 均值方差，构造于 [ehm_pipeline.py:26](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py#L26)）；`self.backbone(x[:, :, :, 32:-32])` 把 256×256 裁成 256×192 再提特征；`self.head(feats)` 输出参数字典。骨干与解码头在 [ehm_pipeline.py:24-25](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py#L24-L25) 由 `cfg.BACKBONE` / `cfg.HEAD` 解包构造。

> **走读观察点：docstring 已过时。** [ehm_pipeline.py:29-44](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py#L29-L44) 的注释说返回 `'pd_cam': shape (B,3)`、`'pd_params'`，但解码头实际返回的是 `body_param` / `flame_param` 两个字典和 `(B,4,4)` 的 `pd_cam`（见 [models/smplx/smplx_head.py:315-319](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L315-L319)）。**注释与代码冲突时，以代码为准**——这是读研究代码的基本功。

**参数字典的真实内容**：[models/smplx/smplx_head.py:271-300](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L271-L300) —— `flame_param` 六个键（眼/颈/下颌/眼睑/表情/形状）与 `body_param` 各键（全局姿态、身体姿态、双手姿态均为旋转矩阵，另有 shape/exp/hand_scale/head_scale）都在这里切片组装，注释标了每个维度。

**EHM_v2.forward 的四段结构**（细节留到单元四精读，这里只认地图）：

1. FLAME 分支：[EHM_v2.py:37-83](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L37-L83) —— 用 `flame_param` 跑 LBS 得到头部网格（L73-L76），加眼睑偏移（L77-L79），乘 `head_scale`（L82-L83）。
2. 身体分支：[EHM_v2.py:88-140](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L88-L140) —— 取 `body_param` 各键，把 shape(200) 补零到 300 后与 exp 拼接、做 shape 混合得到「已变形模板」（L137-L140）。
3. 头替换：[EHM_v2.py:153-158](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L153-L158) —— 把 FLAME 头平移对齐颈部关节后，按 `smplx2flame_ind` 索引写回 SMPL-X 模板对应位置，同时保留颈部边界顶点（L157）。
4. 蒙皮与输出：[EHM_v2.py:160-163](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L160-L163) 的 `lbs_wobeta` 做姿态蒙皮得顶点；[EHM_v2.py:204-213](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L204-L213) 组装返回字典：`vertices (1,10475,3)`、`joints (1,145,3)`（55 关节 + 68 landmark + 22 额外关节，见 L183-L189）、逐顶点变换矩阵、原始 FLAME 头顶点。

> **走读观察点：`pose_type='aa'` 形参没被用上。** 脚本在 [inference_wo_detect.py:86](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L86) 传了 `pose_type='aa'`（axis-angle），但 `EHM_v2.forward` 签名里的 `pose_type`（[EHM_v2.py:34-35](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L34-L35)）在函数体内**从未被引用**——真正决定「输入是轴角还是旋转矩阵」的是 `global_pose` 的维度判断（[EHM_v2.py:114-121](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L114-L121)：4 维走旋转矩阵路径 `pose2rot=False`）。解码头输出的姿态本就是旋转矩阵，所以走的是后者。读代码时别被形参名带偏。

#### 4.2.4 代码实践

**实践目标**：验证 checkpoint 的结构假设——它确实只含 `backbone` 与 `head` 两段。

**操作步骤**（**示例代码**；前提是本机跑过一次推理，`pear_model.pt` 已在 HuggingFace 缓存中，或允许脚本在线下载）：

```python
# demo_ckpt.py（示例代码）
import torch
from huggingface_hub import hf_hub_download

path = hf_hub_download(repo_id="BestWJH/PEAR_models",
                       filename="pear_model.pt", repo_type="model")
state = torch.load(path, map_location="cpu", weights_only=True)

print("顶层键:", list(state.keys()))
for k, v in state.items():
    print(k, "子模块数:", len(v))
    first = next(iter(v.items()))
    print("  首个参数名:", first[0], "形状:", tuple(first[1].shape))
```

**需要观察的现象**：顶层只有 `backbone`、`head` 两个键；backbone 的参数名里能看到大量 `blocks.N.` 前缀（32 层 Transformer Block）。

**预期结果**：与 [inference_wo_detect.py:61-62](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L61-L62) 的两行 `load_state_dict` 一一对应；`EHM_v2` 不在 checkpoint 内。若网络受限无法下载，此实践改为源码阅读型：直接从 L60-L62 两行代码推导出相同结论。

#### 4.2.5 小练习与答案

**练习 1**：为什么加载权重要用 `map_location='cpu'` 再 `.cuda()`，而不是直接 load 到 GPU？
**答案**：先载 CPU 再整体搬移，可以避免「保存时的设备编号与当前机器 GPU 编号不一致」导致的加载失败，也让 `strict=False` 的检查在 CPU 上完成。这是跨机器分发权重的通用习惯。

**练习 2**：`Ehm_Pipeline.forward` 为什么在归一化之后才做 `x[:, :, :, 32:-32]` 裁剪？顺序反过来行不行？
**答案**：`normalize` 是逐通道的仿射变换（每个通道减均值除方差），与空间位置无关，所以「先归一化再裁剪」和「先裁剪再归一化」数值上等价。顺序无所谓，行不行都正确——但**不裁不行**：256×256 与训练分布 256×192 不一致。真正的约束是必须裁，而不是裁的先后。

**练习 3**：`ehm`（EHM_v2）既然无可学习参数，为什么还要 `.cuda()`？
**答案**：它内部注册了大量 buffer（模板顶点、蒙皮权重、关节回归器等）并参与前向计算；这些张量必须与输入参数在同一设备上，否则 CPU 张量与 CUDA 张量相乘会直接报设备不匹配错误。

---

### 4.3 模块三：渲染与叠加输出 —— GS_Camera、render_mesh 与 alpha 混合

#### 4.3.1 概念说明

渲染要回答一个问题：**3D 顶点怎么变成 2D 图像上的像素？** 这需要一套相机模型，分两部分：

- **外参（R、T）**：世界坐标到相机坐标的旋转与平移——人来决定「人在相机的哪里」；
- **内参（焦距 f、主点、画幅）**：相机自身的成像属性——决定「怎么投到像素平面」。

PEAR 的特殊之处在于外参由网络预测（`pd_cam`），而**内参是全仓库写死的一组常数**：焦距 `f=24`、主点 `(0,0)`、画幅 `1024×1024`。你在本讲会看到这三个数出现在**三个地方**，它们必须一致：

| 出现位置 | 代码 | 作用 |
| --- | --- | --- |
| `BodyRenderer("assets/SMPLX", 1024, focal_length=24.0)` | [inference_wo_detect.py:54](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L54) | 渲染器画布与默认焦距 |
| `build_cameras_kwargs(1, 24)` | [inference_wo_detect.py:91](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L91) | 本次渲染用的相机内参 |
| `pd_cam[:, 2:] = 24 / (pd_cam[:, 2:] + 1e-9)` | [models/smplx/smplx_head.py:306](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L306) | 网络预测时按同一焦距换算深度 |

第三个出现位置是最有意思的：网络第三个相机输出本是弱透视的尺度 \( s \)，训练/推理时用焦距把它换算成透视深度：

\[
z = \frac{f}{s}, \quad f = 24
\]

这就是「弱透视 → 透视」的经典转换：小物体近轴成像时，投影尺度 \( s \) 与距离 \( z \) 成反比，比例系数正是焦距。分母加 `1e-9` 是防零。

#### 4.3.2 核心流程

渲染与落盘的完整流程：

```text
① pd_cam 的诞生（解码头内）
   cam_decoder(token_out) → 3 维 (tx, ty, s)
   + bias [0, 0, 1.5]                     smplx_head.py:303-305
   第三维换算 z = 24/s                     smplx_head.py:306
   get_full_proj: R=diag(-1,-1,1), T=(tx,ty,z)
   → Tmat (B,4,4) 即 outputs['pd_cam']    smplx_head.py:205-231

② 脚本组装相机
   batch_images = pad_and_resize(img, 1024)      # 背景图
   GS_Camera(principal_point=0, focal_length=24,
             image_size=[1024,1024], device='cuda',
             R=pd_cam[:3,:3], T=pd_cam[:3,3])    # 拆 4×4 为 R/T

③ 渲染
   render_mesh(vertices[None,0], pd_camera, lights)
   → GS_MeshRasterizer 光栅化 + SoftPhongShader 着色
   → RGBA (B,4,1024,1024)，背景像素置白         graphics_utils.py:843-847

④ 后处理落盘
   RGBA → 取 RGB → cpu → clip(0,255) → uint8 → transpose(1,2,0)
   → RGB2BGR → imwrite mesh_{name}.jpg
   背景提亮 ×1.5 → addWeighted(网格图, 0.5, 亮背景, 0.5)
   → imwrite result_{name}.jpg
```

关于「f=24 到底对应多大的视场角」：这套 GS 风格投影里 `tan(θ/2) = 1/f`（[utils/graphics_utils.py:240-256](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L240-L256) 中 `get_proj_matrix(1/invtanfov, ...)`，`invtanfov=24`）。折算到 1024 画幅上的等效像素焦距：

\[
f_{px} = \frac{S/2}{\tan(\theta/2)} = 512 \times 24 = 12288 \ \text{px}, \quad \theta = 2\arctan\frac{1}{24} \approx 4.8^\circ
\]

这是一个极长的焦距（近似正交的窄视场），恰好呼应了弱透视假设——人只占画面一小块时，透视退化为准正交投影。

#### 4.3.3 源码精读

**弱相机参数 → 4×4 外参**：[models/smplx/smplx_head.py:303-306](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L303-L306) —— 回归 3 维、加偏置 `[0,0,1.5]`、第三维按 \( z=24/s \) 换算深度。随后 [smplx_head.py:205-231](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L205-L231) 的 `get_full_proj` 用**固定的对角旋转** \( R=\mathrm{diag}(-1,-1,1) \)（x、y 轴取负，把坐标系翻到 pytorch3d 的约定）与平移 \( T=(t_x,t_y,z) \) 拼出 4×4 矩阵——这就是 `outputs['pd_cam']` 的 (B,4,4)。

**相机内参关键字典**：[inference_wo_detect.py:36-43](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L36-L43) —— `build_cameras_kwargs` 造出 `principal_point=0`、`focal_length`、`image_size=[1024,1024]`、`device='cuda'`。

**拆 4×4 组装相机**：[inference_wo_detect.py:88-91](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L88-L91) —— 先把原图 letterbox 到 1024 备作背景；再把 `outputs['pd_cam']` 按切片拆成 `R=[0:1,:3,:3]`、`T=[0:1,:3,3]` 传入 `GS_Camera`。`GS_Camera` 继承自 pytorch3d 的 `CamerasBase`（[utils/graphics_utils.py:172-208](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L172-L208)），把单个焦距标量扩展成 (N,2)，适配 GS 风格投影；其 `get_projection_transform`（[graphics_utils.py:240-256](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L240-L256)）注释写明「中心点为 00，focal 为 24」，底层 `get_proj_matrix`（[graphics_utils.py:57-76](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L57-L76)）按 \( 1/\tan(\theta/2)=f \) 填标准透视投影矩阵。

**调用渲染并转图像**：[inference_wo_detect.py:93-94](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L93-L94) —— `render_mesh(vertices[None, 0, ...], pd_camera, lights=lights)` 单人单次渲染；返回 (B,4,H,W)，取前 3 通道（RGB）、`detach().cpu().numpy()`、`clip(0,255)`、`uint8`、`transpose(1,2,0)` 变回 (H,W,C)。`Renderer2` 本体（[models/modules/renderer/body_renderer.py:105-130](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/renderer/body_renderer.py#L105-L130)）只负责从 `smplx_tex.obj` 加载网格拓扑与 UV，渲染逻辑在父类。

**渲染内部**：[utils/graphics_utils.py:780-849](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L780-L849) —— `GS_BaseMeshRenderer.render_mesh` 给每个顶点统一肤色 `skin_color=[252,224,203]`（L800-L806），组 `Meshes`，用 `GS_MeshRasterizer` + `SoftPhongShader` 渲染（L835-L838）；随后 [graphics_utils.py:843-847](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L843-L847) 把**背景像素显式置白**（注释原文 "White background instead of black"）并追加 alpha 通道，最后乘 255。光栅化参数（画幅、`blur_radius=0`、`faces_per_pixel=1`）与灯光方位在 [graphics_utils.py:703-728](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L703-L728) 初始化。

**保存网格图**：[inference_wo_detect.py:96-97](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L96-L97) —— pytorch3d 输出是 RGB，`cv2.imwrite` 期望 BGR，所以先 `cv2.COLOR_RGB2BGR` 再写 `mesh_{img_name}.jpg`。这张图是**白底纯网格**（背景被置白）。

**叠加结果图**：[inference_wo_detect.py:99-111](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L99-L111) —— 三步：

1. L99-L100 计算 `foreground_mask`/`background_mask`（判断像素是否非黑）。**但这两个变量后面从未被使用**——它们是黑色背景旧版留下的死代码；如今背景是白色，这个「非黑即前景」的判断即使启用也会把整张白底判成前景。
2. L102-L104 把背景图乘 1.5 提亮（float 中间过程防溢出，再 `np.clip(0,255)` 转回 uint8）；L104 的 `result = bright_bg.copy()` 随即被 L109 覆盖，同样是无效行。
3. L107-L111 `alpha = 0.5`（注释明确写着 "you can change the alpha to control the transparency of the mesh"），`cv2.addWeighted(pd_mesh_img, alpha, bright_bg, 1-alpha, 0)` 做**全图均匀**的 \( \alpha \) 混合，写出 `result_{img_name}.jpg`。

**入口与命令行**：[inference_wo_detect.py:116-128](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L116-L128) —— `--config_name/-c` 默认 `infer`；`--input_path` 与 `--output_path` 默认同为 `data_input/test_source_images`（即输出直接落在输入目录旁边，产物前缀 `mesh_`/`result_` 避免覆盖原图）；`--debug` 被解析但未使用；`torch.set_float32_matmul_precision('high')` 允许矩阵乘使用 TF32 以提速。文件头部还有若干未使用的 import（`lightning`、`glob`、`time`、`rtqdm`、`device_parser`），是从训练脚本复制入口时留下的痕迹。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：把硬编码的 `alpha` 混合系数和 `pad_and_resize` 的 `target_size` 提升为 argparse 命令行参数，并用 `0.3/0.7`、`320` 三组值做对照实验，直观理解两个旋钮各自控制什么。

**操作步骤**：

1. 复制一份脚本再改（**不要改动仓库源码用于提交**，本地实验用副本即可）：`cp inference_wo_detect.py my_inference.py`。
2. 在 [inference_wo_detect.py:118-124](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L118-L124) 的 `argparse` 段新增三个参数（**示例代码**）：

   ```python
   parser.add_argument('--alpha', type=float, default=0.5,
                       help='mesh transparency, 0~1')
   parser.add_argument('--render_size', type=int, default=1024,
                       help='canvas size for background & rendering')
   parser.add_argument('--model_input_size', type=int, default=256,
                       help='letterbox size fed to the network')
   ```

3. 把参数一路传进 `inference()`（改函数签名为 `inference(config_name, input_path, output_path, alpha, render_size, model_input_size)`），然后替换三处硬编码：
   - [L80](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L80) `pad_and_resize(img, target_size=model_input_size)`；
   - [L88](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L88) `pad_and_resize(img, target_size=render_size)`，同时把 [L54](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L54) 的 `BodyRenderer(..., 1024, ...)` 与 [L91](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L91) 的 `build_cameras_kwargs(1, 24)`（以及 [L37](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L37) 里写死的 `1024`）同步换成 `render_size`——**画布三处必须一致**，否则网格图与背景图尺寸不齐，`addWeighted` 直接报错；
   - [L107](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L107) `alpha = args.alpha`。
4. 准备输入：`mkdir -p data_input/test_source_images`，放入 [u1-l4](u1-l4-first-inference-run.md) 用过的 `example/images` 下几张单人图。
5. 跑三组对照（均**待本地验证**，需按 [u1-l2](u1-l2-environment-and-assets.md) 备好环境与资产）：

   ```bash
   python my_inference.py --alpha 0.3 --output_path out_a03
   python my_inference.py --alpha 0.7 --output_path out_a07
   python my_inference.py --render_size 320 --output_path out_r320
   ```

**需要观察的现象**：

- `out_a03` vs `out_a07`：同一个 `result_*.jpg` 里网格相对背景的「浓淡」变化——0.3 更透（背景人影更清楚），0.7 更实（网格占主导）。`mesh_*.jpg` 两组完全相同，因为 `alpha` 不参与渲染本身。
- `out_r320`：`mesh_*.jpg` 与 `result_*.jpg` 都变成 320×320，网格在画幅中的**相对占比**与 1024 版基本一致（渲染分辨率变了，投影几何没变）。
- 额外实验 `--model_input_size 320`（选做）：输入变 320×320 后 `forward` 裁成 320×256，token 网格从 16×12 变为 20×16。骨干里存在位置编码的双三次插值逻辑（[models/backbones/vit.py:31-37](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L31-L37)），因此大概率**能跑通但预测质量漂移**——训练分布外分辨率的实际效果待本地验证。

**预期结果**：`alpha` 只影响叠加权重，`render_size` 只影响输出画布；两者都不触碰网络。`model_input_size` 是唯一会改变网络输入分布的旋钮，风险自负。

#### 4.3.5 小练习与答案

**练习 1**：如果把 [L91](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L91) 的 `build_cameras_kwargs(1, 24)` 改成 `build_cameras_kwargs(1, 48)`，渲染会怎样？
**答案**：相机焦距变成 48，投影矩阵按 \( \tan(\theta/2)=1/48 \) 构造，等效像素焦距翻倍；而 `pd_cam` 里的深度仍是按 \( z=24/s \) 换算的（[smplx_head.py:306](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L306)）。内外参不再配套，网格投影尺度与位置会和原图人体对不齐（整体放大/偏移）。渲染器构造处的 `focal_length=24.0` 只是 `render_mesh` 在未显式传相机时的默认值，本脚本每帧都显式传了 `pd_camera`，所以真正生效的是 L91 这个 24。

**练习 2**：`foreground_mask`（L99）为什么是死代码？如何判断一段代码「死了」？
**答案**：它在 L99-L100 计算后，后续任何一行都没有引用 `foreground_mask` 或 `background_mask`；L109 的混合是无条件的全图 `addWeighted`。判断方法是在文件内搜索该标识符的所有出现位置——只出现在赋值侧、从未出现在读取侧，即死代码。另外它的语义（非黑即前景）与当前白底渲染也不兼容，说明它属于黑色背景的旧版本。

**练习 3**：`bright_bg` 为什么要乘 1.5 提亮，而不是直接用原图混合？
**答案**：`mesh_*.jpg` 背景是纯白（255），`alpha=0.5` 混合后白色会把整张结果图「冲淡」；预先把背景乘 1.5（并截断）是为了补偿这部分亮度损失，让人体区域仍可辨识。这是针对「白底网格图 + 均匀混合」这组特定选择的补偿手段——若把渲染背景改回黑色，这行就应一并调整。

---

## 5. 综合实践

**任务：写一个「链路体检」脚本 `probe_pipeline.py`，一次性打印全链路每一步的张量形状与数值范围**，把本讲三个模块串起来。（**示例代码**思路，自己动手实现。）

要求覆盖以下检查点，每个检查点对应本讲一处源码：

1. **预处理段**：读一张图，打印 `img.shape` → `pad_and_resize(img, 256).shape` → `to_tensor` + permute 后的形状与 `img_patch.min()/max()`（预期 `0.0/1.0`）。
2. **网络段**：`outputs = ehm_model(img_patch)` 后打印三个键：`body_param` 每个键的 shape（对照 4.2.2 的表）、`flame_param` 每个键的 shape、`pd_cam` 的 shape（预期 `(1,4,4)`）与内容（注意 `R` 是否为 `diag(-1,-1,1)` 附近、`T` 第三维是否为正的深度值）。
3. **人体模型段**：`pd_smplx_dict = ehm(outputs['body_param'], outputs['flame_param'], pose_type='aa')` 后打印 `vertices`（预期 `(1,10475,3)`）、`joints`（预期 `(1,145,3)`）与 `vertices` 的三轴数值范围。
4. **渲染段**：按 [inference_wo_detect.py:91-94](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L91-L94) 的方式构造 `GS_Camera` 并渲染，打印渲染输出形状（预期 `(1,4,1024,1024)`）。
5. **一致性断言**：用 `assert` 检查 `body_renderer.focal_length == 24` 与相机 `focal_length` 一致、渲染画布 == 背景图边长。

**预期结果**：脚本最后输出一张汇总表，形状与 4.2.2 / 4.3.2 的流程图逐行对得上。任何一行对不上，说明你对链路的理解和代码有出入——回头精读对应小节。运行依赖环境与资产（[u1-l2](u1-l2-environment-and-assets.md)），数值结果**待本地验证**。

## 6. 本讲小结

- `inference_wo_detect.py` 是 PEAR **最短的完整推理链路**：`ConfigDict` 配置 → `Renderer2` 渲染器 → `hf_hub_download` 权重 → `Ehm_Pipeline`（backbone+head，`strict=False` 加载）→ `EHM_v2`（资产构造、不进 checkpoint）。
- 预处理是「letterbox 等比缩放到 256×256 + `/255` + 维度重排」，ImageNet 归一化与 256×192 裁剪（`x[:,:,:,32:-32]`）在 `Ehm_Pipeline.forward` 内部完成；脚本读图后**未做 BGR→RGB 转换**，是一处值得留意的不一致。
- `ehm(body_param, flame_param)` 的四段结构：FLAME 分支出头 → 身体分支做 shape 混合 → 按 `smplx2flame_ind` 把 FLAME 头替换进 SMPL-X 模板 → `lbs_wobeta` 蒙皮，产出 `(1,10475,3)` 顶点与 `(1,145,3)` 关节；`pose_type='aa'` 形参实际未被使用，分支由张量维度决定。
- 相机内参是全仓库常数（`f=24`、主点 0、画布 1024），且出现在**三处必须一致**的位置；`pd_cam` 第三维由弱透视尺度按 \( z=f/s \) 换算成透视深度，`R` 为固定 `diag(-1,-1,1)`。
- 输出两张图：`mesh_*.jpg` 是白底纯网格（`render_mesh` 显式置白背景）；`result_*.jpg` 是它与「×1.5 提亮背景」的全图均匀 `alpha=0.5` 混合；`foreground_mask` 等几行是黑底旧版遗留的死代码。
- 读研究代码的两个习惯：**注释与代码冲突时以代码为准**（如 `forward` 的过时 docstring），**形参名不代表行为**（如未使用的 `pose_type`）。

## 7. 下一步学习建议

- 下一讲 [u2-l3](u2-l3-multi-person-detection.md) 进入多人场景：YOLO 检测、`process_bbox` 扩框、`generate_patch_image` 仿射裁剪与 `warpAffine` 回贴——你会看到「无检测版」的 `pad_and_resize` 被「检测裁剪版」替代后链路的其余部分几乎不变。
- 若想先把网络内部讲清楚，可跳读 [u2-l5](u2-l5-ehm-pipeline-forward.md)（`Ehm_Pipeline.forward` 精读）与单元三（ViT 骨干、TransformerDecoder、参数解码器组）。
- 本讲反复出现的 `pd_cam` 与投影矩阵的完整推导放在 [u3-l4](u3-l4-camera-model.md)；`EHM_v2` 的头替换细节放在 [u4-l4](u4-l4-ehm-v2-fusion.md)。
- 建议现在就做第 5 节的综合实践：一张「形状对得上」的体检表，是后面所有讲义里随时可以回来核对的地基。
