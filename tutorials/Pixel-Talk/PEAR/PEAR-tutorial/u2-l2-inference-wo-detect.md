# 单人推理全链路：inference_wo_detect.py 逐行走读

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立走读 `inference_wo_detect.py` 的完整流程：读图 → `pad_and_resize` → `to_tensor` → `ehm_model` 前向 → `ehm` 参数转网格 → 渲染 → 叠加保存。
2. 说清楚 `ehm(body_param, flame_param)` 这一行是如何把两个参数字典变成 10475 个顶点的统一网格的。
3. 解释 `GS_Camera` 的构造参数（`focal_length=24`、`image_size=1024`）背后的投影几何，以及最后 `alpha` 混合渲染的数学含义。
4. 掌握一套「拿到一个推理脚本先看什么」的走读方法论：入口参数 → 预处理 → 模型装配 → 前向 → 后处理 → 落盘。

本讲是单元二第二讲。上一讲（u2-l1）我们已经知道 `ConfigDict` 如何把 YAML 变成配置对象；本讲就把这份配置真正「用起来」，沿一条真实推理链路把 PEAR 的五个核心模块串一遍。

## 2. 前置知识

### 2.1 回顾：本讲要用到的旧知识

- **u1-l1/u1-l4**：PEAR 输入是 256×256 的人体 patch，输出 `body_param` / `flame_param` / `pd_cam` 三个字段；`inference_wo_detect.py` 是「单人居中、无检测」的推理入口。
- **u2-l1**：`ConfigDict(model_config_path='configs/infer.yaml')` 读 YAML 成 dict 并包一层只读 OmegaConf；`add_extra_cfgs` 补充额外配置段。

### 2.2 本讲新术语

- **letterbox 填充（pad）**：把任意长宽比的图等比缩放后贴到正方形画布中央，空余部分填 0（黑边）。YOLO 等检测管线常用这个词，`pad_and_resize` 做的就是这件事。
- **CHW 与 HWC**：OpenCV 读图得到的是 `(H, W, C)`（高、宽、通道）；PyTorch 卷积网络期望 `(B, C, H, W)`。所以送网前要 `permute` 换轴再 `unsqueeze` 加 batch 维。
- **外参矩阵 RT（4×4）**：把世界坐标点搬到相机坐标的齐次矩阵，上三行是旋转 `R` 与平移 `T`，末行是 `[0,0,0,1]`。PEAR 里网络直接回归这个 4×4 矩阵（`pd_cam`）。
- **NDC（Normalized Device Coordinates）**：光栅化前的归一化坐标，\(x,y\in[-1,1]\)。pytorch3d 的渲染器吃 NDC 坐标，最后再映射到像素。
- **alpha 混合（图像融合）**：把两张图按权重逐像素加权求和：
  \[ \text{result} = \alpha \cdot I_{\text{mesh}} + (1-\alpha) \cdot I_{\text{bg}} \]
  其中 \(\alpha\) 控制网格的「不透明感」。

### 2.3 一条直觉：这条脚本为什么值得逐行走读

`inference_wo_detect.py` 只有约 130 行，却把 PEAR 的**全部五个核心模块**按依赖顺序各调用了一次：配置系统 → `Renderer2`（拓扑）→ `Ehm_Pipeline`（backbone + head）→ `EHM_v2`（参数转网格）→ `GS_Camera`（投影）→ 渲染器。它是全仓库「最小可读的完整链路」，读懂它，后面所有入口脚本（`inference_images.py`、`app.py`）都只是它的变体。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [inference_wo_detect.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py) | 本讲主角：无检测单人推理入口 | 全文走读 |
| [models/pipeline/ehm_pipeline.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py) | 网络管线（backbone + head） | `forward` 的归一化与裁剪 |
| [models/modules/ehm/EHM_v2.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py) | 参数 → 顶点的统一人体模型 | `forward` 的 FLAME/SMPL-X 融合 |
| [utils/pipeline_utils.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/pipeline_utils.py) | 通用工具 | `to_tensor` 的类型统一逻辑 |
| [utils/graphics_utils.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py) | 相机与渲染基础件 | `GS_Camera`、`get_proj_matrix`、`GS_BaseMeshRenderer.render_mesh` |
| [models/modules/renderer/body_renderer.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/renderer/body_renderer.py) | 渲染器封装 | `Renderer2` 如何加载网格拓扑 |
| [models/smplx/smplx_head.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py) | 解码头（u3-l3 精读） | 只看 `pd_cam` 4×4 矩阵的生成处 |
| [configs/infer.yaml](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/infer.yaml) | 推理配置 | `BACKBONE` / `HEAD` 两段 |

一个小观察：脚本顶部的 import 出现了两轮（第 1–9 行与第 11–23 行有重复），这是研究代码常见的「随手加 import」痕迹，不影响运行，读的时候以第二轮为准即可。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**4.1 图像预处理**、**4.2 模型组装与权重加载**、**4.3 前向与参数到网格**、**4.4 渲染与叠加输出**。它们正好对应主循环里的四段代码。

先用一张流程图记住全局（数字为张量形状）：

```text
cv2.imread (H,W,3) BGR
   │  pad_and_resize(target_size=256)
   ▼
(256,256,3) uint8 ── to_tensor('cuda:0') ──► /255 → permute(2,0,1) → unsqueeze(0)
   ▼
img_patch (1,3,256,256)
   │  Ehm_Pipeline.forward：normalize + 裁剪 [32:-32]
   ▼
(1,3,256,192) ── ViT 骨干 ──► 特征 ── SMPLXTransformerDecoderHead ──► outputs
   │      outputs['body_param']  (SMPL-X 参数字典)
   │      outputs['flame_param'] (FLAME 参数字典)
   │      outputs['pd_cam']      (1,4,4) RT 矩阵
   ▼
ehm(body_param, flame_param)  ── EHM_v2 ──► pd_smplx_dict['vertices'] (1,10475,3)
   │
   │  另一路：pad_and_resize(img, 1024) 作为背景 (1024,1024,3)
   ▼
GS_Camera(focal=24, image_size=1024, R/T ← pd_cam)
   │  body_renderer.render_mesh(vertices, camera, lights)
   ▼
pd_mesh_img (1,3,1024,1024) → HWC uint8 → mesh_*.jpg
   │  cv2.addWeighted(mesh, α, bright_bg, 1-α)
   ▼
result_*.jpg
```

### 4.1 图像预处理：pad_and_resize 与 to_tensor

#### 4.1.1 概念说明

网络是一个固定输入尺寸的 ViT（配置里 `img_size: [256, 192]`），而现实图片长宽比五花八门。**等比缩放 + 居中补零**是最保守的对齐方式：它不拉伸人体、不裁掉四肢，只要求「人在画面中央」。这正是本脚本「无检测、假设单人居中」的前提——它不像 `inference_images.py` 那样先用 YOLO 框出人再裁剪，而是赌输入图里的人本来就居中。

`to_tensor` 则是仓库里统一的「什么都变 torch.Tensor」工具，负责把 numpy 图像搬到 GPU。

#### 4.1.2 核心流程

`pad_and_resize(img, target_size)` 的算法：

1. 计算等比缩放系数 \( s = \min(T/h,\ T/w) \)，\(T\) 为目标边长。
2. 缩放：`new_w, new_h = int(w·s), int(h·s)`，用双线性插值。
3. 建一张全 0 的 \(T\times T\) 黑色画布。
4. 把缩放图贴到画布**正中央**（偏移量向下取整）。

随后：

5. `to_tensor(resized, 'cuda:0')`：numpy → torch，搬上 GPU，形状仍为 `(H,W,C)`。
6. `img_patch/255`：uint8 的 0–255 归一化到 0–1 浮点。
7. `permute(2,0,1)` 换轴成 `(C,H,W)`，`unsqueeze(0)` 加 batch 维 → `(1,3,256,256)`。

注意两点容易被忽略的事实：

- **通道顺序没有翻转**。`cv2.imread` 读进来的是 BGR，脚本没有做 `cvtColor(COLOR_BGR2RGB)` 就直接送网络——模型是按这个约定训练的。而后面渲染输出是 RGB，保存前反而做了一次 `COLOR_RGB2BGR`（第 96 行）。一进一出，方向相反。
- **归一化分两步**。0–1 的缩放在脚本里做，ImageNet 均值方差的 `normalize` 在 `Ehm_Pipeline.forward` 里做。分工不同，缺一不可。

#### 4.1.3 源码精读

`pad_and_resize` 定义在入口脚本自己身上（不在 utils 里）：

[inference_wo_detect.py:25-34](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L25-L34) —— 等比缩放取 `min(target_size/h, target_size/w)`，保证缩后图像**两边都不超过**目标边长；然后建黑色正方形画布，把缩放图贴到中央。对一个竖构图的人像（\(h>w\)），结果是左右留黑边。

`to_tensor` 的类型分派逻辑：

[utils/pipeline_utils.py:62-88](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/pipeline_utils.py#L62-L88) —— 对三种输入分别处理：已是 Tensor 就只搬设备；是 `np.ndarray` 就 `torch.from_numpy(...).to(device)`；是 List 就先转 numpy 再转 Tensor。`temporary=True` 时还会返回一个「还原函数」，本讲用不到，知道即可。

主循环里的调用链：

[inference_wo_detect.py:79-82](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L79-L82) —— `cv2.imread` 读图（BGR、`(H,W,3)`、uint8）；`pad_and_resize(img, target_size=256)` 得到 256×256；`to_tensor(...,'cuda:0')` 搬上 GPU；最后一行把数值除以 255、换轴成 CHW、加 batch 维，得到 `(1,3,256,256)` 的网络输入。注释里的 `(B, C, H, W)` 说明了作者对形状的预期。

#### 4.1.4 代码实践

**实践目标**：在不启动整个模型的情况下，单独验证 `pad_and_resize` 的行为与形状约定。

**操作步骤**（示例代码，可存为临时脚本在仓库根目录运行）：

```python
# 示例代码：验证 pad_and_resize
import cv2, numpy as np, sys
sys.path.insert(0, '.')
from inference_wo_detect import pad_and_resize

img = np.zeros((480, 360, 3), dtype=np.uint8)   # 竖构图 480x360
out = pad_and_resize(img, target_size=256)
print(out.shape)          # 期望 (256, 256, 3)
# 360/480 = 0.75 → new_w=192, new_h=256 → x_offset=(256-192)//2=32
print(out[:, :32].max(), out[:, -32:].max())    # 左右各 32 列应全为 0（黑边）
print(out[:, 32:-32].max())                     # 中间区域也是 0（因为原图本来就是黑的）
```

更严谨的做法是用 `example/images` 下的真实图片（若存在）替换 `np.zeros`，把黑边画出来看。

**需要观察的现象**：竖图左右出现对称黑边、宽各 32 像素；横图则是上下黑边。

**预期结果**：`out.shape == (256, 256, 3)`，黑边宽度符合 `int((256 - new_w)/2)` 的手算值。

**待本地验证**：以上结论来自对源码的直接推导，具体像素值需本地运行确认。

#### 4.1.5 小练习与答案

**练习 1**：一张 1920×1080 的横构图图片，`pad_and_resize(img, 256)` 后黑边出现在哪、多宽？

**答案**：\(s=\min(256/1080, 256/1920)=256/1920\approx0.1333\)，缩放后为 256×144（高 144），上下留黑边，各 `(256-144)//2 = 56` 像素。

**练习 2**：如果把 `int(w*scale)` 的 `int` 换成 `round`，程序会出什么问题？

**答案**：可能因四舍五入让 `new_w` 或 `new_h` 等于 257，超出了画布；赋值 `padded_img[y_off:y_off+257, ...] = resized_img` 在 numpy 里不会报错但会**静默截断**，导致内容丢失 1 像素。`int` 的向下取整是保守选择。

**练习 3**：为什么网络输入必须凑成正方形 256×256，而 `Ehm_Pipeline.forward` 内部又裁成 256×192？

**答案**：配置 `BACKBONE.img_size: [256, 192]` 说明 ViT 是按 256×192 的 patch 网格（12×16 个 patch）预训练/微调的；入口先补成正方形是为了保持「人在中央」的几何假设，管线内部再统一去掉左右各 32 列黑边（见 4.3.3），把输入对齐到骨干真正期望的宽高比。这也解释了为什么黑边留在左右时（竖图）裁掉的恰好是黑边而不是人体。

### 4.2 模型组装与权重加载

#### 4.2.1 概念说明

这一段回答三个问题：**用什么配置建网络**（YAML）、**权重从哪来**（HuggingFace Hub 自动下载）、**哪些模块需要权重**（只有 backbone 和 head，`EHM_v2` 是纯几何、无参数）。

关键认知（承接 u1-l3）：PEAR 的 checkpoint `pear_model.pt` 只包含 `backbone` 与 `head` 两段 state dict；`EHM_v2` 把 SMPL-X/FLAME 的模板、蒙皮权重等当作**常量缓冲**使用，不含任何可学习参数，所以不在 checkpoint 里。

#### 4.2.2 核心流程

组装顺序（全部发生在逐图循环之前，只做一次）：

1. `ConfigDict` 读 `configs/infer.yaml` → `add_extra_cfgs` 补充配置段。
2. `BodyRenderer("assets/SMPLX", 1024, focal_length=24.0)`：加载网格拓扑（faces）与 UV，固定渲染分辨率 1024、焦距 24。
3. `hf_hub_download` 从 `BestWJH/PEAR_models` 拉取 `pear_model.pt`（本地有缓存则直接复用）。
4. `Ehm_Pipeline(meta_cfg)` 按配置实例化 backbone + head。
5. `torch.load(..., weights_only=True)` 后分别给 `backbone`、`head` 灌权重（`strict=False`）。
6. `EHM_v2("assets/FLAME", "assets/SMPLX")` 构造参数→网格的几何层，`.cuda()`。
7. `PointLights(location=[[0,-1,-10]])` 准备一盏正面灯。

#### 4.2.3 源码精读

配置与渲染器：

[inference_wo_detect.py:49-54](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L49-L54) —— `ConfigDict` 读 `configs/{config_name}.yaml`（默认 `infer`），`add_extra_cfgs` 补充额外段后 `print` 出完整配置；接着用 `assets/SMPLX` 目录构造 `Renderer2`（脚本里 import 时起别名 `BodyRenderer`），渲染尺寸 1024、`focal_length=24.0`。这两个数后面会反复出现，务必记住。

权重下载与加载：

[inference_wo_detect.py:56-63](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L56-L63) —— `hf_hub_download(repo_id="BestWJH/PEAR_models", filename="pear_model.pt", repo_type="model")` 返回权重在本地的缓存路径（首次运行会真正联网下载）；`Ehm_Pipeline(meta_cfg)` 按配置建好模型；`torch.load(..., map_location='cpu', weights_only=True)` 安全反序列化；随后 `backbone` 与 `head` 各自 `load_state_dict(..., strict=False)`。`weights_only=True` 是 PyTorch 的安全开关，只允许加载张量，避免 pickle 任意代码执行。

为什么 `strict=False`：backbone 来自 ViTPose 预训练权重（见 [configs/infer.yaml:21](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/infer.yaml#L21) 的 `backbone_ckpt` 字段约定），键名可能与本仓库 ViT 类不完全一致，宽松加载可以让匹配得上的键生效。

无参数的几何层与灯光：

[inference_wo_detect.py:66-68](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L66-L68) —— `EHM_v2` 只需要 FLAME 与 SMPL-X 两个资产目录（资产放置约定见 u1-l2）；`PointLights` 放在 `(0,-1,-10)`，即相机前方（pytorch3d 相机看向 +Z），为 Phong 着色提供光源。注意这个位置与 `GS_BaseMeshRenderer` 里 `inverse_light=True` 分支的默认灯位一致（见 [utils/graphics_utils.py:713-716](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L713-L716)），而 `Renderer2.__init__` 正是以 `inverse_light=True` 调用父类的（[models/modules/renderer/body_renderer.py:106-107](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/renderer/body_renderer.py#L106-L107)）。

`Renderer2` 的构造：

[models/modules/renderer/body_renderer.py:105-125](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/renderer/body_renderer.py#L105-L125) —— `Renderer2` 继承 `GS_BaseMeshRenderer`，从 `assets/SMPLX/smplx_tex.obj` 用 pytorch3d 的 `load_obj` 读出顶点、面片与 UV 坐标，并把 faces 注册为 buffer。也就是说**渲染器自带一份 SMPL-X 拓扑**，后面渲染 `EHM_v2` 输出的 10475 顶点时用的就是这份 faces（顶点数一致，恰好对齐）。

#### 4.2.4 代码实践

**实践目标**：亲眼确认「checkpoint 只含 backbone/head 两段、`EHM_v2` 无可学习参数」这两件事。

**操作步骤**（示例代码，在仓库根目录、pear 环境下运行）：

```python
# 示例代码：检查 checkpoint 结构与 EHM_v2 的参数量
import torch
from huggingface_hub import hf_hub_download
from models.modules.ehm import EHM_v2

ckpt_path = hf_hub_download(repo_id="BestWJH/PEAR_models",
                            filename="pear_model.pt", repo_type="model")
state = torch.load(ckpt_path, map_location="cpu", weights_only=True)
print("顶层键:", list(state.keys()))
print("backbone 参数张量数:", len(state['backbone']))
print("head 参数张量数:", len(state['head']))

ehm = EHM_v2("assets/FLAME", "assets/SMPLX")
print("EHM_v2 可学习参数量:", sum(p.numel() for p in ehm.parameters()))
```

**需要观察的现象**：`state` 顶层只有 `backbone`、`head`（可能还有别的键，以实际输出为准）；`EHM_v2` 的可学习参数量为 0。

**预期结果**：与 u1-l3 的结论一致——EHM_v2 是纯几何层，权重全在 backbone/head。

**待本地验证**：`state` 的完整顶层键集合需本地打印确认。

#### 4.2.5 小练习与答案

**练习 1**：如果不传 `-c` 参数，`config_name` 是什么？配置从哪个文件读？

**答案**：默认 `"infer"`（[inference_wo_detect.py:119](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L119)），拼出路径 `configs/infer.yaml`。

**练习 2**：为什么 `EHM_v2` 不需要 `load_state_dict`？

**答案**：它没有 `nn.Parameter`。SMPL-X/FLAME 的模板顶点、`J_regressor`、蒙皮权重等全部是数据资产（以 buffer 或普通属性持有），推理时只做确定性的几何计算。

**练习 3**：`--debug` 参数（第 123 行）在本脚本里实际生效了吗？

**答案**：没有。它被 `argparse` 解析进 `args.debug`，但 `inference()` 只收到 `config_name / input_path / output_path` 三个参数，`debug` 从未被使用——又一个小体积研究代码的「留而未用」。

### 4.3 前向与参数到网格：ehm_model 与 ehm

#### 4.3.1 概念说明

这是整条链路的「大脑」部分，两行代码完成两步：

1. `outputs = ehm_model(img_patch)`：可学习的网络（ViT 骨干 + 解码头）从像素**回归参数**。
2. `pd_smplx_dict = ehm(outputs['body_param'], outputs['flame_param'], pose_type='aa')`：确定性的几何层把参数**展开成网格**。

第一步是「学习」，第二步是「解析」。PEAR 的设计哲学就在这里：网络只预测低维参数（几百维），把高维几何（10475×3 顶点）交给人体模型的先验知识去恢复。这也是它能实时（100 FPS）的根本原因之一。

#### 4.3.2 核心流程

**第一步：`Ehm_Pipeline.forward`**

```text
x (1,3,256,256)
  → normalize（ImageNet 均值/方差）
  → x[:, :, :, 32:-32]  裁掉左右各 32 列 → (1,3,256,192)
  → backbone(x)  → 特征（token 序列）
  → head(feats)  → outputs 字典
```

**第二步：`EHM_v2.forward`**

```text
flame_param（头） ──► FLAME lbs（global/neck 旋转被置零）──► head_vertices [B,5023,3]
                          │ + eyelid 偏移、× head_scale
body_param（身） ──► SMPL-X 模板 + blend_shapes(shape,exp) ──► new_template_vertices [B,10475,3]
                          │
头部替换：把 SMPL-X 头部区顶点(5023 个)对齐颈部关节后换成 FLAME 头
                          │
lbs_wobeta(full_pose, ...) ──► vertices [B,10475,3]（最终网格）
                          └──► joints [B,145,3]、ver_transform_mat、ori_head_vertices
```

数学上，FLAME 头嵌回身体的核心是对齐约束：设 FLAME 头部关节 3、4（脖子附近）的均值为 \(j_f\)，SMPL-X 关节 23、24（颈部）的均值为 \(j_b\)，则替换后的头顶点

\[ v_{\text{head}}' = s_{\text{head}} \odot v_{\text{flame}} - j_f + j_b \]

其中 \(s_{\text{head}}\) 是逐轴缩放（`head_scale`）。平移项 \(-j_f+j_b\) 保证「脖子接口」两边对齐。

#### 4.3.3 源码精读

管线前向的归一化与裁剪：

[models/pipeline/ehm_pipeline.py:29-52](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm_pipeline.py#L29-L52) —— `forward` 只做三件事：`normalize`（ImageNet 统计量，构造处在 [ehm_pipeline.py:26](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py#L26)）；`x[:, :, :, 32:-32]` 把 256×256 裁成 256×192（宽度方向去掉两侧各 32 像素——正是 4.1 里 letterbox 留下的黑边宽度量级）；backbone 出特征、head 出参数。注意 docstring 写的返回形状是历史版本注释，实际 `pd_cam` 已是 4×4 RT 矩阵（见 4.4.3）。

调用处：

[inference_wo_detect.py:85-86](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L85-L86) —— `outputs = ehm_model(img_patch)` 得到参数；`ehm(outputs['body_param'], outputs['flame_param'], pose_type='aa')` 展开成网格。一个细节：`pose_type='aa'` 这个关键字在 `EHM_v2.forward` 的函数体里**并没有被使用**——真正决定「输入是轴角还是旋转矩阵」的是 `global_pose` 的维度（4 维走矩阵分支），见下文。这是读源码时要小心的「形参陷阱」。

参数字典的内容（解码头侧）：

[models/smplx/smplx_head.py:283-317](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L283-L317) —— `body_param_dict` 装着 `global_pose/body_pose/left_hand_pose/right_hand_pose`（都是 6D 旋转转成的 3×3 矩阵）、`hand_scale/head_scale`、`exp/shape`；`flame_param_dict` 装着 `eye_pose_params/pose_params/jaw_params/eyelid_params/expression_params/shape_params`；最后 `all_out['pd_cam']` 存的是 `get_full_proj` 返回的 4×4 矩阵。

EHM_v2 的 FLAME 分支：

[models/modules/ehm/EHM_v2.py:38-83](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L38-L83) —— 解包 flame 参数后，第 64–65 行把 `global_pose_params` 与 `neck_pose_params` **强制置零**：头的全局朝向完全由身体的颈部关节带动，FLAME 只负责下巴、眼球、眼睑、表情这些「局部细节」。第 73–76 行调用 FLAME 的 `lbs` 得到 `head_vertices`；第 77–79 行把眼睑线性偏移加上去（眨眼就是两个标量乘一组固定位移向量）；第 83 行乘 `head_scale`。

身体分支与形状混合：

[models/modules/ehm/EHM_v2.py:88-139](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L88-L139) —— 解包身体参数；第 114–121 行按 `global_pose` 的维度数决定 `pose2rot`（本链路 head 输出 4 维旋转矩阵，走 `pose2rot=False` 分支，同时把 jaw/eye 用零矩阵占位）；第 123–126 行把 200 维 shape 补零到 300 维；第 139 行 `template + blend_shapes(shape_components, shapedirs)` 直接用形状基把模板「捏」成目标体型，第 140 行回归身体关节。

头部替换（PEAR 的核心一步）：

[models/modules/ehm/EHM_v2.py:150-158](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L150-L158) —— 取出 SMPL-X 头部区的 5023 个顶点，按 4.3.2 的公式平移对齐（`head_joints[:, 3:5]` 对 `tbody_joints[:, 23:25]`）；第 157 行把 `non_head_index`（颈部与边界）顶点恢复为 SMPL-X 原值，避免接缝处穿帮；第 158 行写回模板。

蒙皮与输出：

[models/modules/ehm/EHM_v2.py:160-163](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L160-L163) —— `lbs_wobeta` 在「已完成形状混合」的模板上做线性混合蒙皮（LBS 细节留到 u4-l3）。[EHM_v2.py:204-211](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L204-L211) —— 返回字典包含 `vertices [B,10475,3]`、`joints [B,145,3]`、逐顶点变换矩阵与原始 FLAME 头顶点。

#### 4.3.4 代码实践

**实践目标**：不看屏幕、只读源码，填出下面这张形状表（这是「源码阅读型实践」，无需 GPU）。

**操作步骤**：对照 [models/smplx/smplx_head.py:271-317](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L271-L317) 与 [models/modules/ehm/EHM_v2.py:34-211](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L34-L211)（设 batch \(B=1\)），先自己填，再对答案：

| 键 | 所属字典 | 形状 |
| --- | --- | --- |
| `global_pose` | body_param | ？ |
| `body_pose` | body_param | ？ |
| `left_hand_pose` | body_param | ？ |
| `shape` | body_param | ？ |
| `head_scale` | body_param | ？ |
| `jaw_params` | flame_param | ？ |
| `eyelid_params` | flame_param | ？ |
| `shape_params` | flame_param | ？ |
| `pd_cam` | outputs 顶层 | ？ |
| `vertices` | ehm 返回 | ？ |

**需要观察的现象 / 预期结果**（答案）：

| 键 | 形状 | 依据 |
| --- | --- | --- |
| `global_pose` | (1,1,3,3) | smplx_head.py:286，rot6d→rotmat |
| `body_pose` | (1,21,3,3) | smplx_head.py:287，21 个身体关节 |
| `left_hand_pose` | (1,15,3,3) | smplx_head.py:288，15 个手指关节 |
| `shape` | (1,200) | smplx_head.py:300 |
| `head_scale` | (1,3) | smplx_head.py:294，`smplx_scale[:,3:]` |
| `jaw_params` | (1,3) | smplx_head.py:275，`flame_pose[:,9:12]` |
| `eyelid_params` | (1,2) | smplx_head.py:276，`flame_pose[:,12:14]` |
| `shape_params` | (1,300) | smplx_head.py:278 |
| `pd_cam` | (1,4,4) | smplx_head.py:309–315，`get_full_proj` 返回的 Tmat |
| `vertices` | (1,10475,3) | EHM_v2.py:139/160/205，模板 10475 顶点 |

若想本地核实，可运行（示例代码）：

```python
# 示例代码：打印真实形状（需 GPU 与资产）
img = ...  # 4.1 的 (1,3,256,256) 张量
outputs = ehm_model(img)
for k, v in outputs['body_param'].items():
    print('body', k, tuple(v.shape))
for k, v in outputs['flame_param'].items():
    print('flame', k, tuple(v.shape))
```

**待本地验证**：上表由源码注释与切片下标推导，建议本地打印核对。

#### 4.3.5 小练习与答案

**练习 1**：`Ehm_Pipeline.forward` 里的 `x[:, :, :, 32:-32]` 裁掉的是哪 64 列？为什么恰好可以裁？

**答案**：裁掉宽度方向两侧各 32 列（256→192）。因为 letterbox 预处理下竖构图人的黑边正好在左右两侧，这 64 列对多数输入是无信息区域；同时骨干的 `img_size` 配置就是 `[256,192]`，这一裁剪把输入对齐到 patch 网格（12×16 个 16×16 patch）。

**练习 2**：为什么 FLAME 的 `global_pose_params` 被置零（EHM_v2.py:64）？

**答案**：头的全局朝向已由 SMPL-X 的颈部运动链决定（通过头部替换时的关节对齐传递）。若 FLAME 再自带一个全局旋转，头部就会与脖子「脱节」。FLAME 分支只保留下颌、眼球、眼睑、表情等局部自由度。

**练习 3**：`pose_type='aa'` 传了但没用，那实际走哪条分支？由什么决定？

**答案**：走 `pose2rot=False`（旋转矩阵）分支。由 `len(global_pose.shape)` 决定：head 输出的 `global_pose` 是 4 维张量 `(B,1,3,3)`，命中 EHM_v2.py:118–121 的 else 分支；若某调用方传入 `(B,1,3)` 的轴角，则会走 `pose2rot=True` 分支。

### 4.4 渲染与叠加输出：GS_Camera 与 alpha 混合

#### 4.4.1 概念说明

网格有了，还差两件事：**把它摆到正确位置投影出来**（相机），**画成图片并与原图叠加**（渲染与混合）。

PEAR 的相机设计很巧妙：网络不预测完整的相机内参外参，而是回归一个 3 维弱相机向量，再在解码头里「翻译」成一套固定的透视相机——焦距恒为 24（NDC 单位），主点恒在原点，旋转恒为 \(\mathrm{diag}(-1,-1,1)\)，唯一的自由度是平移 \(T=(t_x,t_y,t_z)\)。渲染分辨率恒为 1024×1024，所以背景图也要 `pad_and_resize` 到 1024。

最后一步是 alpha 混合：把渲染出的网格图与提亮过的原图加权叠加，让人一眼看出「网格贴得准不准」。

#### 4.4.2 核心流程

```text
outputs['pd_cam'] (1,4,4) ──拆──► R (1,3,3)＝diag(-1,-1,1)，T (1,3)
GS_Camera(principal_point=0, focal_length=24, image_size=[1024,1024], R, T)
    │
    ├─ get_projection_transform：tanfov = 1/24 → 4×4 投影矩阵
    └─ render_mesh(vertices, cameras, lights)
         ├─ TexturesVertex（皮肤色）+ Meshes
         ├─ GS_MeshRasterizer 光栅化
         ├─ SoftPhongShader 着色
         └─ 背景→白，附 mesh mask 通道，×255
    ▼
pd_mesh_img (1024,1024,3) RGB ──► RGB2BGR ──► mesh_*.jpg
    │
bright_bg = clip(原图1024 × 1.5)
result = α·mesh + (1-α)·bright_bg ──► result_*.jpg
```

涉及的投影几何只有三条公式（\(f=24\)，\(W=1024\)）：

\[ x_{\text{ndc}} = f \cdot \frac{X_{\text{view}}}{Z_{\text{view}}}, \qquad x_{\text{screen}} = \frac{W}{2}\,(1 - x_{\text{ndc}}) \]

\[ T_z = \frac{f}{s} \quad\Longrightarrow\quad x_{\text{ndc}} = f\cdot\frac{X}{f/s} = s\cdot X \]

第三条是「题眼」：解码头把预测的尺度 \(s\) 换算成深度 \(T_z=f/s\)（[smplx_head.py:306](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L306) 的 `pd_cam[:, 2:] = 24 / (pd_cam[:, 2:] + 1e-9)`），于是在人体尺度（\(Z\approx T_z\) 的小邻域）内，透视投影**退化成缩放 \(s\) 的弱透视**——这就是 HMR 类方法「弱相机嵌入透视相机」的标准手法，也是 u1-l1 说的「像素对齐」的数学来源。

#### 4.4.3 源码精读

背景图与相机装配：

[inference_wo_detect.py:88-91](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L88-L91) —— 第 88 行把原图再 pad 到 1024 作为叠加背景（渲染分辨率就是 1024，两边必须同尺寸）；第 91 行用 `build_cameras_kwargs(1, 24)` 的内参加上从 `pd_cam` 拆出的 `R`（前 3×3）与 `T`（第 4 列前 3 行）构造 `GS_Camera`。`build_cameras_kwargs` 定义在 [inference_wo_detect.py:36-43](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L36-L43)：主点全零、`focal_length` 传入 24、`image_size` 为 \([1024,1024]\)。

`pd_cam` 的 4×4 从哪来：

[models/smplx/smplx_head.py:304-317](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L304-L317) —— `cam_decoder` 输出 3 维向量，加偏置后第 3 维被换算成 \(24/s\)；`get_full_proj`（[smplx_head.py:205-231](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L205-L231)）把固定的 \(R=\mathrm{diag}(-1,-1,1)\) 与平移 \(T\) 拼成 4×4 齐次矩阵返回——它才是 `outputs['pd_cam']`。也就是说**旋转与内参在推理时是常量，网络只学平移**。

`GS_Camera` 的构造：

[utils/graphics_utils.py:172-208](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L172-L208) —— 继承 pytorch3d 的 `CamerasBase`；注意第 179 行标注 `principal_point` 「useless」——即使外面传了主点，第 189 行也会硬编码成 \((0,0)\)。焦距被扩展成 `(N,2)` 形状备用。

内参与焦距的关系：

[utils/graphics_utils.py:240-256](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L240-L256) —— `get_projection_transform` 把 `focal_length` 当作 `invtanfov`（半视场角正切的倒数）用，调 `get_proj_matrix(1/f)` 生成投影矩阵；而 [utils/graphics_utils.py:57-76](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L57-L76) 里 `proj_matrix[0,0] = 2·z_near/(right-left) = 1/\tan\theta = f`。所以半视场角 \(\theta=\arctan(1/24)\approx 2.38^\circ\)，全视场约 \(4.8^\circ\)——一个「窄而长」的针孔，人体恰好铺满画面。NDC 到像素的映射在 [utils/graphics_utils.py:306-330](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L306-L330) 的 `transform_points_to_screen`：先 `(ndc-1)·size/2` 再整体取反，把坐标搬进 \([0,1024]\) 的像素系。

渲染调用与颜色处理：

[inference_wo_detect.py:93-97](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L93-L97) —— `render_mesh(vertices[None, 0, ...], pd_camera, lights=lights)` 渲染单张；输出是 `(1,C,H,W)`，取 `[:,:3]`（丢弃第 4 通道的 mesh mask）、`clip(0,255)`、转 uint8、`transpose(1,2,0)` 变 HWC，最后 `COLOR_RGB2BGR` 后写盘 `mesh_{图片名}.jpg`。

渲染器内部：

[utils/graphics_utils.py:800-847](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L800-L847) —— `render_mesh` 给所有顶点铺同一肤色 `[252,224,203]`（第 801–806 行），构建 `Meshes`，用 `GS_MeshRasterizer` + `SoftPhongShader` 渲染（第 835–838 行）；第 843–847 行是关键后处理：alpha 小于 0.5 的**背景像素被显式置为白色**（注释原话 "White background instead of black"），再把 mesh mask 拼成第 4 通道，最后 ×255。所以你拿到的 `mesh_*.jpg` 是**白底**网格图，第 4 通道（真正的 mask）在第 94 行被 `[:,:3]` 丢掉了。

叠加与落盘：

[inference_wo_detect.py:99-111](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L99-L111) —— 第 102–103 行把背景图乘 1.5 提亮并截断；第 107 行写死 `alpha = 0.5`（注释提醒你可以改）；第 109 行 `cv2.addWeighted(pd_mesh_img, alpha, bright_bg, 1-alpha, 0)` 做加权融合；第 111 行写出 `result_{图片名}.jpg`。**一个细读才能发现的点**：第 99 行用「像素是否为纯黑」来判前景，但由于渲染背景是白色，这个 mask 恒为全前景、`background_mask` 恒为空——它是黑底时代的遗留代码，真正起作用的只有 alpha 加权。由此可推：默认参数下 `result_*.jpg` 的背景 ≈ \(0.5\times 255 + 0.5\times(1.5\times\text{原图})\)，会比原图明显偏亮（原图暗部会变成中灰）。

#### 4.4.4 代码实践

**实践目标**：用纯 CPU 验证「焦距 24 ↔ 视场角」的换算，加深对 `get_proj_matrix` 的理解。

**操作步骤**（示例代码，无需 GPU 与模型权重）：

```python
# 示例代码：复现 GS_Camera 的投影内参
import torch, math
from utils.graphics_utils import get_proj_matrix

P = get_proj_matrix(tanfov=1/24, device="cpu")
f_ndc = P[0, 0].item()          # = 1/tanfov = 24
half_fov = math.degrees(math.atan(1/24))
print(f"NDC 焦距 = {f_ndc}, 半视场角 = {half_fov:.3f}°, 全视场 ≈ {2*half_fov:.3f}°")

# 手算一个点：view 系下 (X=0.5, Z=2.0)，T_z = 2 意味着 s = 24/2 = 12
x_ndc = 24 * 0.5 / 2.0          # = 6 → 远超 1，落在视锥外
x_screen = (1 - x_ndc) * 1024 / 2
print(f"x_ndc={x_ndc}, x_screen={x_screen}")
```

**需要观察的现象**：`f_ndc` 恰为 24；半视场角约 2.38°；把 \(Z\) 换成 \(T_z=f/s\) 时 `x_ndc` 数值上等于 \(s\cdot X\)。

**预期结果**：与 4.4.2 的三条公式逐一吻合。`get_proj_matrix` 不依赖任何 GPU 资源，此实践可直接运行。

**待本地验证**：视锥外点的具体裁剪行为（光栅化层面）本实践不覆盖。

#### 4.4.5 小练习与答案

**练习 1**：`focal_length=24` 和 `image_size=1024` 分别出现在脚本的哪几处？它们为什么必须「处处一致」？

**答案**：`focal_length` 出现在 `BodyRenderer(..., focal_length=24.0)`（第 54 行）与 `build_cameras_kwargs(1, 24)`（第 91 行，`GS_Camera` 用它算投影矩阵）；`image_size=1024` 出现在 `BodyRenderer(..., 1024, ...)`（决定光栅化分辨率）与 `build_cameras_kwargs` 的 `screen_size`（决定 NDC→像素映射），同时背景图也 pad 到 1024（第 88 行）。不一致会导致网格与背景错位（尺寸或比例不匹配）。

**练习 2**：`pd_cam` 的旋转部分包含任何「视角」信息吗？

**答案**：不包含。\(R\) 恒为 \(\mathrm{diag}(-1,-1,1)\)（smplx_head.py:209–213），只做 X/Y 镜像；配合 screen 阶段的再次取反把坐标搬进像素系。所有「人在画面哪里、多大」的信息都在平移 \(T\) 里，尤其 \(T_z=f/s\)。

**练习 3**：把第 88 行背景的 `target_size` 从 1024 改成 512，直接运行会怎样？怎么修？

**答案**：`cv2.addWeighted` 要求两张图同尺寸，512 的背景与 1024 的渲染图混叠会抛 cv2 异常（待本地验证具体报错信息）。修法二选一：把背景再 `cv2.resize` 到 1024，或把渲染器与相机的 `image_size` 一并降到 512。这个练习说明 1024 是「渲染-相机-背景」三方共用的全局约定。

## 5. 综合实践

**任务**：把 `inference_wo_detect.py` 中的 alpha 混合系数与 `pad_and_resize` 的背景 `target_size` 提升为 argparse 命令行参数，做一组对照实验。

**背景说明**：脚本里有两处 `pad_and_resize`——第 80 行的 `target_size=256`（网络输入）与第 88 行的 `target_size=1024`（叠加背景）。**只有后者可以安全参数化**：前者被骨干的输入约定（`img_size: [256,192]`、`[32:-32]` 裁剪、位置编码）锁死，改成 320 会导致形状对不上而报错。所以本实践把 320 实验放在背景尺寸上，并顺带验证「为什么 256 不能动」。

**操作步骤**：

1. 复制脚本（建议保留原文件，在副本上改；本讲义自身不改动仓库源码）。
2. 在 argparse 段（[inference_wo_detect.py:118-124](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L118-L124)）增加两个参数：

```python
# 示例代码：新增的两个命令行参数
parser.add_argument('--alpha', type=float, default=0.5)
parser.add_argument('--bg_size', type=int, default=1024)
```

3. 把 `alpha`、`bg_size` 透传进 `inference()`，替换第 88 行的 `target_size=1024` 与第 107 行的 `alpha = 0.5`。
4. 当 `bg_size != 1024` 时，在 `cv2.addWeighted` 之前把背景 resize 到渲染尺寸（或反向 resize 渲染图），保证两边同尺寸。
5. 准备几张单人居中的图片放入某目录，依次运行：

```bash
python inference_wo_detect.py --input_path <你的图片目录> --output_path out_a03 --alpha 0.3
python inference_wo_detect.py --input_path <你的图片目录> --output_path out_a07 --alpha 0.7
python inference_wo_detect.py --input_path <你的图片目录> --output_path out_b320 --alpha 0.5 --bg_size 320
```

6. （探索分支）把第 80 行的 `256` 改成 `320` 运行一次，记录报错信息。

**需要观察的现象**：

- `--alpha 0.3`：网格颜色淡、原图细节清晰；`--alpha 0.7`：网格浓、几乎盖住原图。两者网格的**位置与形状完全一致**，只有浓淡不同。
- `--bg_size 320`：若未做第 4 步的 resize，会直接报尺寸错误；做了 resize 后，`result_*.jpg` 是低分辨率的合成图，而 `mesh_*.jpg` 不受影响（渲染分辨率仍由渲染器决定）。
- 探索分支：改网络输入尺寸会触发形状不匹配错误（位置编码/裁剪约定被破坏）。

**预期结果**：alpha 只影响混合权重、不影响几何；背景尺寸只影响合成图分辨率、不影响网格本身；网络输入 256 是硬约束。三组对照正好把「哪类参数能动、哪类不能动」摸清楚。

**待本地验证**：以上现象为源码推导的预期，具体报错文本与视觉效果需本地运行确认。

## 6. 本讲小结

- `pad_and_resize` 用「等比缩放 + 居中补零」把任意图片变成 256×256 输入，配合管线内部的 `[32:-32]` 裁剪对齐到骨干期望的 256×192；BGR 通道顺序全程保持，不额外翻转。
- 权重只存在于 `backbone` 与 `head` 两段（`hf_hub_download` 自动下载、`strict=False` 加载）；`EHM_v2` 是纯几何层，仅需 FLAME/SMPL-X 资产目录。
- 推理的核心是「网络回归参数、几何层展开网格」两步走：`ehm_model(img_patch)` 出三个字段，`ehm(body_param, flame_param)` 出 `(1,10475,3)` 顶点，其中 FLAME 头经关节对齐（\(-j_f+j_b\)）与 `head_scale` 缩放后嵌回 SMPL-X 身体。
- 相机是「半固定」的：内参焦距恒 24（NDC 单位，半视场角约 2.38°）、主点恒零、旋转恒 \(\mathrm{diag}(-1,-1,1)\)，网络只学平移，且 \(T_z=f/s\) 把尺度换算成深度，使透视相机在人体邻域内退化为弱透视——这是「像素对齐」的数学落点。
- 渲染链路 `Renderer2 → GS_BaseMeshRenderer.render_mesh` 输出**白底**网格图（背景被显式置白），脚本再与 1.5 倍提亮的背景按 `alpha=0.5` 加权叠加；基于「非黑即前景」的 mask 分支是黑底时代的遗留，实际不起作用。
- 1024 是渲染器、相机、背景图三方共用的全局尺寸约定，改一处必须同步改三处。

## 7. 下一步学习建议

- **下一讲（u2-l3）**：`inference_images.py` 的多人场景——YOLOv8 检测、`process_bbox` 扩框、`generate_patch_image` 仿射裁剪与 `warpAffine` 回贴。它是本讲的「多目标版」，重点看 patch 获取方式从 letterbox 变成仿射裁剪后，回贴是怎么逆向完成的。
- **下一讲（u2-l4）**：`app.py` 的 Gradio 视频演示与本讲的渲染链路相同，多了会话临时目录与参数平滑。
- **顺流而下（u2-l5、u3 系列）**：若想弄清 256×192 进骨干后发生了什么，接着读 `Ehm_Pipeline` 的细节与 `models/backbones/vit.py`、`models/smplx/pose_transformer.py`、`models/smplx/smplx_head.py`。
- **深挖几何（u4 系列）**：本讲一笔带过的 `blend_shapes`、`lbs_wobeta`、头部替换的索引缓冲（`smplx2flame_ind`、`non_head_index`）将在单元四逐个展开。
