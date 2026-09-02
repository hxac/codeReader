# 网格渲染：Renderer2、GS_BaseMeshRenderer 与光照

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `Renderer2` 在初始化时从 `assets/SMPLX/smplx_tex.obj` 里加载了什么（拓扑 faces 与 UV），以及这些 buffer 在当前推理路径中哪些真正被用到。
2. 走读 `GS_BaseMeshRenderer.render_mesh` 的完整流程：`Meshes` 组装 → `GS_MeshRasterizer` 光栅化 → `SoftPhongShader` Phong 着色 → 白底与 alpha 通道合成，并能解释它与 pytorch3d 原生 `MeshRasterizer`/`MeshRenderer` 的关系。
3. 理解 `PointLights` 的位置与颜色（ambient/diffuse/specular）如何影响渲染结果，特别是"为什么 PEAR 的灯放在 (0, −1, −10) 这种负 z 位置"背后的相机几何。
4. 掌握把 `pd_smplx_dict['vertices']` 与 `renderer.faces` 导出为 obj 文件（trimesh）的方法，并理解渲染拓扑与 EHM 顶点数之间"牙齿顶点不被渲染"的细节。

本讲是单元四的收尾：u4-l4 产出了 10595 个顶点的统一网格，u3-l4 讲清了 `pd_cam` 相机矩阵，本讲回答最后一问——**这些顶点是怎么变成你看到的 1024×1024 图像的**。

## 2. 前置知识

### 2.1 光栅化与着色：渲染流水线的两步

把三角网格变成图像，经典上分两步：

- **光栅化（rasterization）**：对每个三角形，找出它覆盖了屏幕上哪些像素，并记录该像素属于哪个面片（`pix_to_face`）、深度（`zbuf`）、以及像素在三角形内的重心坐标（`bary_coords`，三个和为 1 的权重）。
- **着色（shading）**：用重心坐标插值出每个像素的属性（颜色、法线），再按光照模型算出最终颜色。

pytorch3d 把这两步做成可插拔组件：`MeshRenderer = MeshRasterizer + Shader`。PEAR 没有直接用原生组合，而是**替换了光栅化里的坐标变换部分**（自定义 `GS_MeshRasterizer`，"GS" 指 Gaussian Splatting 风格的投影），这是本讲的重点之一。

### 2.2 Phong 光照模型

`SoftPhongShader` 用的是经典 Phong 反射模型，每个像素的颜色由三项组成：

\[
C_{\text{out}} = k_a \otimes I_a + k_d \otimes I_d \cdot \max(0,\ \mathbf{n}\cdot\mathbf{l}) + k_s \otimes I_s \cdot \max(0,\ \mathbf{r}\cdot\mathbf{v})^{\alpha}
\]

- **环境光（ambient）** \(I_a\)：与方向无关的常数底光，保证背光面不是纯黑。
- **漫反射（diffuse）** \(I_d\)：正比于表面法线 \(\mathbf{n}\) 与指向光源方向 \(\mathbf{l}\) 的夹角余弦，正面被照亮的墙面效应。
- **高光（specular）** \(I_s\)：反射方向 \(\mathbf{r}\) 与视线方向 \(\mathbf{v}\) 的夹角越小越亮，指数 \(\alpha\) 控制高光斑大小。

\(k_a/k_d\) 在这里就是顶点颜色（PEAR 用单一肤色填充所有顶点），\(I_a/I_d/I_s\) 由光源对象给出。pytorch3d 的 `PointLights` 只传 `location` 时，三个颜色分量用库的默认值（白光；构造后可通过 `lights.ambient_color` 等属性查看，具体数值待本地验证）。

### 2.3 回顾两件前置事实

- **u3-l4**：网络输出的 `pd_cam` 是一个 4×4 RT 矩阵，旋转固定为 \(\mathrm{diag}(-1,-1,1)\)，平移 \(T=(t_x, t_y, f/s)\)，其中 \(s\) 是弱透视尺度（加 1.5 偏置后为正）、焦距 \(f=24\)。渲染画布恒为 1024×1024。
- **u4-l4**：`EHM_v2` 输出 `pd_smplx_dict['vertices']`，形状 (B, 10595, 3)——前 10475 个是 SMPL-X 拓扑顶点（头部已被 FLAME 替换），末尾 120 个是程序化生成的牙齿顶点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [models/modules/renderer/body_renderer.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/renderer/body_renderer.py) | `Renderer2`：推理链路实际使用的渲染门面，负责加载 obj 拓扑与 UV。文件里还有一个未被任何入口使用的旧类 `Renderer` |
| [utils/graphics_utils.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py) | 真正的渲染原语：`GS_BaseMeshRenderer`（含 `render_mesh`）、`GS_MeshRasterizer`、`GS_Camera`（u3-l4 已精读） |
| [models/modules/renderer/base_renderer.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/renderer/base_renderer.py) | 旧渲染家族（`BaseMeshRenderer`/`PointRenderer`/`TextureRenderer`），用原生 `PerspectiveCameras`，仅被孤儿模块引用，本讲作为对照组 |
| [utils/helper.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/helper.py) | `face_vertices`：按面片索引 gather 顶点，Renderer2 用它展开 UV |
| [inference_wo_detect.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py) | 调用方样本：构造 Renderer2、lights、GS_Camera 并落盘 |
| [models/smplx/smplx_head.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py) | `pd_cam` 的构造处（u3-l4 已精读，本讲只用它的结论推光照几何） |
| `assets/SMPLX/smplx_tex.obj` | 渲染拓扑资产：10475 个顶点、20908 个三角面，附 UV 坐标（无贴图文件） |

一个命名陷阱先记下：仓库里有**三个**"BaseMeshRenderer / Renderer"家族——[base_renderer.py:L24](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/renderer/base_renderer.py#L24) 的 `BaseMeshRenderer`（旧，配 `PerspectiveCameras`）、[graphics_utils.py:L502](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L502) 的 `BaseMeshRenderer`（LightningModule 版，配 `GS_Camera`）、以及本讲主角 [graphics_utils.py:L701](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L701) 的 `GS_BaseMeshRenderer`（纯 `torch.nn.Module` 版）。推理链路只用最后一个。

## 4. 核心概念与源码讲解

### 4.1 Renderer2 初始化：从 smplx_tex.obj 加载拓扑与 UV

#### 4.1.1 概念说明

渲染一个网格需要两样东西：**顶点位置**（每帧由 EHM_v2 现算）和**拓扑**（哪些顶点连成三角形，固定不变）。`Renderer2` 的职责就是在构造时把固定拓扑从 obj 文件读进来，注册成 buffer，让后续每帧只喂顶点就能渲染。

三个入口脚本里的构造方式完全一致（以 [inference_wo_detect.py:L54](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L54) 为例）：

```python
body_renderer = BodyRenderer("assets/SMPLX", 1024, focal_length=24.0).cuda()
```

注意 `image_size=1024`、`focal_length=24` 与 u3-l4 讲过的 GS_Camera 构造参数（`build_cameras_kwargs(1, 24)`、1024 画布）必须一致，否则投影和光栅化的画布对不上。

#### 4.1.2 核心流程

```
Renderer2.__init__(assets_dir, image_size, focal_length=24)
  ├─ GS_BaseMeshRenderer.__init__(image_size, focal_length, inverse_light=True)   # 灯光/光栅化设置，见 4.2
  ├─ load_obj(assets_dir/smplx_tex.obj)
  │    ├─ faces.verts_idx   → (F,3)   面片→顶点索引        ★ 渲染真正用的拓扑
  │    ├─ faces.textures_idx→ (F,3)   面片→UV 顶点索引
  │    └─ aux.verts_uvs     → (Vt,2)  UV 顶点坐标
  ├─ register_buffer('faces', faces[None])          # (1, 20908, 3)
  ├─ register_buffer('raw_uvcoords', uvcoords)     # (1, Vt, 2)
  ├─ UV 规范化：补齐次 1 → ×2−1 映射到 [−1,1] → 翻转 v
  ├─ face_vertices(uvcoords, uvfaces)              # 展开成逐面片 UV (1, F, 3, 3)
  └─ register_buffer('uvcoords'/'uvfaces'/'face_uvcoords')
```

关键审计结论：**当前推理路径（`render_mesh`）只消费 `faces` buffer，用纯色顶点纹理渲染；四个 UV 相关 buffer 注册后无人读取**（详见 4.1.3 最后一段）。

#### 4.1.3 源码精读

**构造与 obj 加载**（[models/modules/renderer/body_renderer.py:L105-L125](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/renderer/body_renderer.py#L105-L125)）：

```python
class Renderer2(GS_BaseMeshRenderer):
    def __init__(self, assets_dir, image_size=1024, device='cuda', focal_length=24):
        super().__init__( image_size, focal_length=focal_length,inverse_light=True)
        topology_path = osp.join(assets_dir, 'smplx_tex.obj')
        self.focal_length=focal_length
        verts, faces, aux = load_obj(topology_path)
        uvcoords = aux.verts_uvs[None, ...]      # (N, V, 2)
        uvfaces = faces.textures_idx[None, ...]  # (N, F, 3)
        faces = faces.verts_idx[None, ...]
        self.register_buffer('faces', faces)
        self.register_buffer('raw_uvcoords', uvcoords)
```

这段做三件事：调用父类构造（`inverse_light=True` 决定灯光方位，见 4.3）；用 pytorch3d 的 `load_obj` 读 obj；把面片顶点索引 `faces.verts_idx` 注册为 buffer。`load_obj` 返回三元组 `(verts, faces, aux)`——注意 PEAR **丢弃了 `verts`**（obj 里的模板顶点坐标），只留拓扑索引；顶点位置每帧由 EHM_v2 提供。另外仓库里只有 `smplx_tex.obj` 而没有它引用的 `smplx_tex.mtl` 贴图文件，`load_obj` 对缺失材质只告警不报错（待本地验证具体警告文案）。

**UV 规范化**（[models/modules/renderer/body_renderer.py:L118-L125](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/renderer/body_renderer.py#L118-L125)）：

```python
uvcoords = torch.cat([uvcoords, uvcoords[:, :, 0:1]*0.+1.], -1)  # 补齐次坐标 1 → (1,Vt,3)
uvcoords = uvcoords*2 - 1          # [0,1] → [−1,1]
uvcoords[..., 1] = -uvcoords[..., 1]  # 翻转 v（obj 与 graphics 上下行约定相反）
face_uvcoords = face_vertices(uvcoords, uvfaces)   # gather 成 (1, F, 3, 3)
```

`face_vertices`（[utils/helper.py:L260-L278](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/helper.py#L260-L278)）先把每个 batch 的面片索引加上偏移 `batch_idx × nv`，再从展平的顶点数组里 gather，得到"每个面片的三个顶点坐标"。它既用于 UV 也用于一切"逐面片属性"。

**牙齿顶点不在拓扑里**——这是我们用仓库资产直接验证过的事实：`smplx_tex.obj` 含 10475 个 `v` 顶点、20908 个 `f` 面，且所有面片索引都不超过 10475（1-based）。而 u4-l4 讲过 EHM_v2（`add_teeth=True`）输出 10595 个顶点。pytorch3d 的 `Meshes` 只校验"面片索引 < 顶点数"，所以多出来的 120 个牙齿顶点被静默接受但**不被任何面片引用、不参与光栅化**——即 `mesh_*.jpg` 里看不到牙齿。旁证是 [app.py:L472](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L472) 被注释掉的 `vertices_list.append(pd_smplx_dict['vertices'][0, :-120]...)`，作者自己收集顶点时也把末尾 120 个切掉了。

**forward 只是个壳**（[models/modules/renderer/body_renderer.py:L127-L130](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/renderer/body_renderer.py#L127-L130)）：

```python
def forward(self, vertices, faces=None, ...):
    if faces is None:
        faces = self.faces.squeeze(0)
    return super().forward(vertices, faces, ...)
```

三个入口**都不走 `forward`**，而是直接调 `render_mesh`（见 [inference_wo_detect.py:L93](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L93)）。另注意：`GS_BaseMeshRenderer` 里还有一个 `render_textured_mesh`（[utils/graphics_utils.py:L663-L698](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L663-L698)）想用 `self.faces_uvd`/`self.verts_uvd` 做 UV 贴图渲染，但 GS 版构造函数从未设置这两个属性（相关语句被注释，[utils/graphics_utils.py:L725-L726](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L725-L726)）——对 `Renderer2` 调它会直接 `AttributeError`。UV 缓冲属"备而未用"。

#### 4.1.4 代码实践：盘点 obj 拓扑

1. **实践目标**：亲手确认渲染拓扑的规模，验证"牙齿顶点不在拓扑里"。
2. **操作步骤**：在仓库根目录跑下面脚本（示例代码，需 pear 环境）：

```python
# check_topology.py（示例代码）
import torch
from pytorch3d.io import load_obj

verts, faces, aux = load_obj("assets/SMPLX/smplx_tex.obj")
f_idx = faces.verts_idx            # (F,3) long
print("obj 顶点数:", verts.shape[0])          # 模板顶点，渲染时被丢弃
print("面片数:", f_idx.shape[0])
print("面片最大顶点索引:", f_idx.max().item())  # 0-based，应 < 10475
print("UV 顶点数:", aux.verts_uvs.shape[0])

# 对照 EHM 输出：10595 顶点中，索引 >= 10475 的 120 个是牙齿，不被任何面片引用
teeth = torch.arange(10475, 10595)
covered = torch.zeros(10595, dtype=torch.bool)
covered[f_idx.reshape(-1)] = True
print("被面片引用的牙齿顶点数:", covered[teeth].sum().item())   # 预期 0
```

3. **需要观察的现象**：顶点数 10475、面片数 20908、最大索引 10474、牙齿引用数为 0。
4. **预期结果**：与你从 `grep -c "^v " assets/SMPLX/smplx_tex.obj` 得到的文本统计一致；从而解释为什么渲染图里看不到牙齿。运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Renderer2` 加载 obj 后把 `verts`（模板顶点）丢掉，而 `EHM_v2` 却要保留自己的模板？
**答案**：渲染只需要**拓扑**（谁连谁），顶点位置每帧由 EHM_v2 用网络预测参数实时计算并传入 `render_mesh`；obj 里的模板坐标是静止的平均人体，对渲染无用。EHM_v2 的模板（10595 顶点）则是 LBS/换头的计算基底，二者职责不同。

**练习 2**：如果把 `Renderer2("assets/SMPLX", 1024, focal_length=24.0)` 里的 `image_size` 改成 512，渲染会发生什么？
**答案**：光栅化画布变成 512×512（`raster_settings.image_size`），但调用方构造的 `GS_Camera` 仍是 1024 画布、且入口脚本把结果当 1024 用（如 u2-l3 里"1024 渲染图先缩回 256"的回贴逻辑），于是网格在图中的相对大小错位。u3-l4 强调过：focal 24、画布 1024 必须多处一致。

### 4.2 render_mesh：光栅化 + Phong 着色

#### 4.2.1 概念说明

`render_mesh` 是三个入口实际调用的渲染函数。它与 `forward` 的区别：`forward` 面向"从 4×4 RT 矩阵自建相机"的用法；`render_mesh` 接受调用方**已经构造好的 `GS_Camera`**（来自 `pd_cam`），只管把顶点变成图像。核心仍然是 pytorch3d 的 `MeshRenderer`（rasterizer + shader）组合，但光栅化器换成了定制的 `GS_MeshRasterizer`，以适配 u3-l4 讲过的 GS 投影约定（\(w = z_{\text{view}}\)，即可见半空间是 \(z_{\text{view}}>0\)，与 OpenGL 的 \(w=-z\) 相反）。

#### 4.2.2 核心流程

```
render_mesh(vertices, cameras=pd_camera, lights=lights)
  ├─ faces 缺省 → self.faces（obj 拓扑）
  ├─ lights 覆盖 self.lights（入口传入 PointLights(0,−1,10)）
  ├─ 顶点着色：肤色 [252,224,203]/255 填满 (B,V,3) → TexturesVertex
  │    （可选：smplx2flame_ind 指定的头顶点改涂 head_color [236,248,254]）
  ├─ Meshes(verts, faces, textures)
  ├─ MeshRenderer(
  │     rasterizer = GS_MeshRasterizer(cameras, raster_settings)
  │       └─ transform: world → view → ndc（z 用视图深度覆写）→ rasterize_meshes
  │     shader = SoftPhongShader(cameras, lights, blend_params)
  │   )
  ├─ 输出 (B,H,W,4) → permute 成 (B,4,H,W)
  └─ alpha<0.5 的背景像素置白；拼接第 4 通道（网格=1/背景=0）；×255
```

#### 4.2.3 源码精读

**入口侧的调用**（[inference_wo_detect.py:L91-L97](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L91-L97)）：

```python
pd_camera = GS_Camera(**build_cameras_kwargs(1,24), R=outputs['pd_cam'][0:1,:3,:3], T=outputs['pd_cam'][0:1,:3,3])
pd_mesh_img = body_renderer.render_mesh(pd_smplx_dict['vertices'][None, 0,...], pd_camera, lights=lights)
pd_mesh_img = (pd_mesh_img[:,:3].detach().cpu().numpy()).clip(0, 255).astype(np.uint8)[0].transpose(1,2,0)
pd_mesh_img = cv2.cvtColor(pd_mesh_img.copy(), cv2.COLOR_RGB2BGR)
cv2.imwrite(os.path.join(output_path,f"mesh_{img_name}.jpg"), pd_mesh_img )
```

`GS_Camera` 直接吃 `pd_cam` 的 R/T（u3-l4 的产物）；渲染结果取前 3 通道（RGB）转 numpy，经 `cv2.cvtColor` RGB→BGR 后用 OpenCV 落盘。app.py 里同样的转换被注释掉（[app.py:L469](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L469)），因为 imageio 写 mp4 期望 RGB——这解释了两个入口的色彩约定差异。

**render_mesh 主体**（[utils/graphics_utils.py:L780-L817](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L780-L817)）：

```python
def render_mesh(self, vertices,cameras=None,transform_matrix=None, faces=None,lights=None,skin_color=None,smplx2flame_ind=None):
    ...
    if faces is None:
        faces = self.faces
    if cameras is None:
        transform_matrix=transform_matrix.clone()
        cameras = self._build_cameras(transform_matrix, self.focal_length)
    if lights is None:
        self.lights = self.lights      # 保持默认（构造时的 inverse 灯）
    else:
        self.lights=lights             # 用入口传入的灯覆盖（副作用：改自身状态）
    ...
    verts_rgb = torch.from_numpy(skin_color/255).to(vertices.device).float()[None, None, :].repeat(B, V, 1)
    if smplx2flame_ind is not None:
        head_rgb = torch.from_numpy(self.head_color/255).to(vertices.device).float()[None, None, :].repeat(B, V, 1)
        verts_rgb[:,smplx2flame_ind] = head_rgb[:,smplx2flame_ind]
    textures = TexturesVertex(verts_features=verts_rgb)
    mesh = Meshes(verts=vertices.to(device), faces=t_faces.to(device), textures=textures)
```

几个值得注意的细节：

- **着色方式是"纯色顶点纹理"**：`TexturesVertex` 给每个顶点同一肤色 (0.988, 0.878, 0.796)，光栅化后按重心坐标插值仍是常数，明暗完全由 Phong 光照贡献。
- **`smplx2flame_ind` 是"头顶点高亮"开关**：传入 `ehm.smplx.smplx2flame_ind` 可把 FLAME 替换的头部涂成淡蓝白 [236,248,254]，但两个入口都默认不传（[inference_images.py:L343](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L343) 里这行被注释）。
- **`self.lights=lights` 有副作用**：它会永久替换渲染器自身的默认灯，反复用不同 lights 调用时行为依赖调用顺序。

**渲染器装配与白底合成**（[utils/graphics_utils.py:L835-L849](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L835-L849)）：

```python
renderer = MeshRenderer(
    rasterizer=GS_MeshRasterizer(cameras=cameras, raster_settings=self.raster_settings),
    shader=SoftPhongShader(cameras=cameras, lights=self.lights.to(device), device=device, blend_params=self.blend)
)
render_results = renderer(mesh).permute(0, 3, 1, 2)   # (B,H,W,4) → (B,4,H,W)
images = render_results[:, :3]
alpha_images = render_results[:, 3:]
alpha = alpha_images.expand(-1, 3, -1, -1) < 0.5       # True = 背景
images[alpha] = 1.0                                    # 背景置白
images = torch.cat([images, 1 - alpha[:, :1] * 1.0], dim=1)  # RGBA：网格处 alpha=1
images = images * 255
```

pytorch3d 的软渲染器天然输出第 4 通道 alpha（网格像素 ≈1、背景 ≈0）。PEAR 借它做两件事：把背景漂白成 `mesh_*.jpg` 的白底，并把 alpha 编码进第 4 通道（网格=1、背景=0）供后续合成用。`blend_params` 的背景色设为黑（构造时 `bg_color=[0,0,0]`），但随后被这段手工置白覆盖。

**光栅化设置**在构造函数里（[utils/graphics_utils.py:L712](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L712)）：`RasterizationSettings(image_size=1024, blur_radius=0.0, faces_per_pixel=1)`——每个像素只取最近 1 个面片、无模糊，即"硬边"渲染，没有软阴影。

**GS_MeshRasterizer 的定制点**（[utils/graphics_utils.py:L416-L440](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L416-L440)）：

```python
verts_world = meshes_world.verts_padded()
verts_view = cameras.transform_points_to_view(verts_world, eps=eps,**kwargs)
verts_ndc =  cameras.transform_points_view_to_ndc(verts_view, eps=eps,**kwargs)
verts_ndc[..., 2] = verts_view[..., 2]     # 用视图深度覆写 NDC 的 z
meshes_ndc = meshes_world.update_padded(new_verts_padded=verts_ndc)
```

pytorch3d 原生 `MeshRasterizer.transform` 一步调 `cameras.transform_points_ndc`；GS 版拆成 view→NDC 两步，因为 `GS_Camera` 的投影矩阵 \(w = z_{\text{view}}\)（[utils/graphics_utils.py:L73](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L73) 的 `proj_matrix[3, 2] = z_sign`）会把 NDC 的 z 映射成非标准区间，所以这里**直接用视图深度覆写 z**，保证 `rasterize_meshes` 的遮挡判断（zbuf）仍在正常的深度量纲上。随后 [utils/graphics_utils.py:L480-L499](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L480-L499) 调底层 `rasterize_meshes` 返回 `Fragments`（`pix_to_face`/`zbuf`/`bary_coords`/`dists`，中文注释齐全）。注意其中行内注释写"# 512"是旧参数残留——实际传入的是 1024，又一处"注释漂移"。

#### 4.2.4 代码实践：给渲染加"灯光开关"

1. **实践目标**：体会在 `faces_per_pixel=1`、纯色顶点纹理下，画面明暗完全由 `PointLights` 决定。
2. **操作步骤**：复制 `inference_wo_detect.py` 为 `inference_light_lab.py`（不要改原文件），把 [inference_wo_detect.py:L68](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L68) 的灯改成三组，循环推理同一张图（示例代码）：

```python
light_presets = {
    "default":  dict(location=[[0.0, -1.0, -10.0]]),
    "dim_amb":  dict(location=[[0.0, -1.0, -10.0]],
                    ambient_color=[[0.1, 0.1, 0.1]], diffuse_color=[[0.9, 0.9, 0.9]]),
    "warm_cool":dict(location=[[3.0, -1.0, -10.0]],
                    ambient_color=[[0.6, 0.5, 0.4]], diffuse_color=[[0.8, 0.7, 0.6]]),
}
for name, kw in light_presets.items():
    lights = PointLights(device='cuda:0', **kw)
    ...  # 原推理与渲染流程，输出文件名加上 f"_{name}"
```

   运行 `python inference_light_lab.py --input_path example/images`。
3. **需要观察的现象**：`dim_amb` 背光区域明显变暗、受光面对比增强；`warm_cool` 出现偏暖色调且光源方位偏移带来的左右明暗不对称。
4. **预期结果**：三张 `mesh_*.jpg` 拓扑与姿态完全一致，仅明暗/色温不同——证明着色阶段独立于几何阶段。具体成像待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`render_mesh` 里为什么要 `images[alpha] = 1.0` 而不是保留 pytorch3d 的黑色背景？
**答案**：`blend_params` 默认背景是黑；置白后 `mesh_*.jpg` 是白底图，方便人眼查看，也让 u1-l4 讲过的 `foreground_mask = np.any(pd_mesh_img != [0,0,0])` 这类按"非黑"找前景的逻辑（在旧黑底版本中）语义清晰。注意在白底下该 mask 实际恒为真，入口里它并未影响 `addWeighted` 的全图混合——这是遗留代码，读代码时以实际数据流为准。

**练习 2**：`GS_MeshRasterizer` 与原生 `MeshRasterizer` 的差别只有 `transform` 一个方法，为什么必须换？
**答案**：原生 transform 假设标准 pytorch3d 相机（\(w=-z\) 语义下 NDC z 单调）；`GS_Camera` 的投影按 3DGS 约定 \(w=+z\)，直接一步投影得到的 NDC z 不适合做深度缓存。GS 版拆成 view/NDC 两步并覆写 `verts_ndc[..., 2] = verts_view[..., 2]`，遮挡判断回到物理深度。这与 u3-l4 的"GS_Camera 两条投影通道"是同一件事在渲染侧的体现。

**练习 3**：想让网格边缘抗锯齿，改哪里？
**答案**：`RasterizationSettings` 的 `blur_radius`（[utils/graphics_utils.py:L712](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L712)）从 0 调为小正数（如 1e-4 弧度量级），并适当增大 `faces_per_pixel`。代价是渲染变慢且 alpha 边界变软，回贴 mask 的"硬边界"效果也会改变。

### 4.3 光照控制：PointLights 的位置与颜色

#### 4.3.1 概念说明

`PointLights` 是点光源：位置 `location` 决定光从哪来，`ambient_color`/`diffuse_color`/`specular_color` 三组 RGB 分别缩放 Phong 三项。PEAR 有个乍看反直觉的设计——灯放在 \((0, -1, -10)\)，z 是**负**的；而且 `Renderer2` 构造时专门传了 `inverse_light=True`。要理解它，需要把 u3-l4 的相机结论推一步：**算出相机在世界系的物理位置**。

#### 4.3.2 核心流程与推导

`GS_BaseMeshRenderer.__init__` 的灯光分支（[utils/graphics_utils.py:L712-L723](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L712-L723)）：

```python
if inverse_light:
    self.lights = PointLights( location=[[0.0, -1.0, -10.0]])
else:
    self.lights = PointLights( location=[[0.0, 1.0, 10.0]])
self.manual_lights = PointLights(
    location=((0.0, 0.0, 5.0), ),
    ambient_color=((0.5, 0.5, 0.5), ),
    diffuse_color=((0.5, 0.5, 0.5), ),
    specular_color=((0.01, 0.01, 0.01), )
)
```

`Renderer2` 传 `inverse_light=True` 取负 z 灯；`manual_lights`（一套带完整三色参数的"手工灯"）在推理链路无人使用，属备而未用的调参入口。

为什么负 z 才是对的？u3-l4 已确立：`pd_cam` 的旋转 \(R=\mathrm{diag}(-1,-1,1)\)、平移 \(T=(t_x, t_y, f/s)\)，其中 \(s>0\)（[models/smplx/smplx_head.py:L303-L306](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L303-L306)：尺度加 1.5 偏置后取 \(24/s\)）。相机中心 \(C\) 满足 \(RC+T=0\)：

\[
C = -R^{-1}T = -R^{\mathsf T}T = (t_x,\ t_y,\ -f/s)
\]

因为 \(s>0\)，所以 \(C_z = -f/s < 0\)——**相机位于世界系 z 为负的一侧，沿 +z 方向看向原点附近的人体**（这与 GS 投影 \(w=+z\)、可见半空间 \(z_{\text{view}}>0\) 自洽）。于是：

- 灯在 \((0,-1,-10)\)：\(z=-10<0\)，与相机同侧，在人体**正面**打光 → 可见表面被照亮。
- 非 inverse 的 \((0,1,+10)\)：\(z>0\)，在人体**背面**，正面只剩环境光，渲染发暗。

"inverse"反的不是相机，而是 pytorch3d 示例里常见的 (0,1,10) 摆灯习惯。入口脚本没有依赖构造时的默认灯，而是各自显式传了同款负 z 灯（[inference_wo_detect.py:L68](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L68)），传给 `render_mesh` 后覆盖 `self.lights`。

#### 4.3.3 源码精读（光照在管线中的注入点）

灯只在着色阶段进入管线：`SoftPhongShader(cameras=cameras, lights=self.lights...)`（[utils/graphics_utils.py:L835-L838](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L835-L838)）。`SoftPhongShader` 内部用 `cameras` 把顶点位置与法线变换到相机系，再与光源位置求方向做 Phong 三项叠加（pytorch3d 内部还涉及 NDC 空间换算，本讲不展开，读者可通过移动 `location` 观察效应来建立直觉）。光栅化阶段完全不感知灯光。

#### 4.3.4 代码实践：翻转灯的 z 朝向

1. **实践目标**：用实验验证 4.3.2 的推导——负 z 灯亮、正 z 灯暗。
2. **操作步骤**：在 4.2.4 的实验脚本里加一组 `"flip_z": dict(location=[[0.0, 1.0, 10.0]])`，其余不变。
3. **需要观察的现象**：`flip_z` 一组整体显著变暗（只剩环境光与边缘漏光），身体正面的明暗渐变几乎消失。
4. **预期结果**：与推导一致；若不一致，回头检查你实验用的 `pd_cam` 旋转是否仍为 \(\mathrm{diag}(-1,-1,1)\)。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：把灯 `location` 的 y 从 −1 改成 +3，预期脸上还是腿上变亮？
**答案**：y 分量控制光的"高低"。注意 SMPL 世界系 y 向上、且相机 R 翻转了 x/y 轴向，但光源位置在世界系中描述，改 y 就是把灯抬高——头顶、肩部朝上的面变亮。具体视觉差异待本地验证。

**练习 2**：只想让网格变成"哑光塑料感"（去高光），改 `PointLights` 哪个参数？
**答案**：把 `specular_color` 调到接近 0（参考 `manual_lights` 的 0.01）。反过来把 `ambient_color` 压低、`diffuse_color` 拉高会得到更强的立体感。

### 4.4 网格导出：从 vertices + faces 到 obj

#### 4.4.1 概念说明

渲染是"给人看"的，导出 obj 是"给下游工具用"的（MeshLab/Blender/trimesh/采集管线）。导出只需要两样东西：一帧顶点坐标 `(V,3)` 和面片索引 `(F,3)`——前者来自 `pd_smplx_dict['vertices']`，后者就是 `renderer.faces`（obj 拓扑 buffer）。trimesh 已是环境内依赖（[utils/graphics_utils.py:L19](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L19) 顶部 `import trimesh`；它未出现在 requirements.txt，属传递依赖，若报 ImportError 手动 `pip install trimesh`）。

#### 4.4.2 核心流程

```
vertices = pd_smplx_dict['vertices'][0]        # (10595,3) torch, cuda
faces    = body_renderer.faces[0]              # (20908,3) long
   ↓ 转 numpy / cpu
trimesh.Trimesh(vertices=verts[:, :3], faces=faces, process=False)
   ↓ .export('result.obj')
```

两个注意点：一是**牙齿顶点**——faces 只引用前 10475 个顶点，导出全部 10595 个也合法（trimesh 容忍未引用顶点），但若下游做"顶点数=面片覆盖"的假设，建议切片 `verts[:10475]` 或用 `mesh.remove_unreferenced_vertices()`（或在导出前用 app.py:472 同款的 `[:-120]`）；二是 `faces` buffer 的 dtype 是 long、形状 `(1,F,3)`，记得 `[0]` 去掉 batch 维。

#### 4.4.3 源码精读

导出的原料在渲染函数开头就被准备好了：`render_mesh` 里 `if faces is None: faces = self.faces`（[utils/graphics_utils.py:L784-L786](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L784-L786)），`t_faces = faces.repeat(B, 1, 1)`（[utils/graphics_utils.py:L799](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L799)）复制到 batch。`self.faces` 正是 4.1 里 `register_buffer('faces', faces.verts_idx[None, ...])` 的那个 buffer，随 `.cuda()` 一起搬到 GPU。也就是说**渲染用哪个拓扑，导出就用哪个拓扑**，天然一致。trimesh 侧（示例代码）：

```python
import numpy as np, trimesh
verts = pd_smplx_dict['vertices'][0].detach().cpu().numpy()   # (10595,3)
faces = body_renderer.faces[0].detach().cpu().numpy()          # (20908,3)
mesh = trimesh.Trimesh(vertices=verts[:10475], faces=faces, process=False)
mesh.export('result.obj')          # 可用 MeshLab / Blender / trimesh 查看
```

`process=False` 很重要：trimesh 默认会合并"重复"顶点、重排面片，可能破坏 SMPL-X 顶点顺序（而 PEAR 的大量索引资产——`smplx2flame_ind` 等——都按原始顺序定义）。

#### 4.4.4 代码实践：导出并检查 result.obj

1. **实践目标**：把一帧推理网格落成自包含的 obj，验证网格完整性。
2. **操作步骤**：
   1. 在 4.2.4 的实验脚本里，紧跟 `render_mesh` 之后加上 4.4.3 的导出片段；
   2. 运行推理，得到 `result.obj`；
   3. 检查：`mesh.vertices.shape == (10475,3)`、`mesh.faces.shape == (20908,3)`、`mesh.is_watertight` 或 `mesh.euler_number` 是否合理；
   4. 用 MeshLab/Blender 或 `trimesh` 的 `mesh.show()` 打开查看。
3. **需要观察的现象**：obj 在查看器里是完整人体（含 FLAME 头），姿态与同帧 `mesh_*.jpg` 一致；`mesh.volume` 为有限值而非 0（说明面片封闭性基本正常）。
4. **预期结果**：顶点/面片数与 4.1.4 的统计吻合。注意 obj 是**世界系**坐标（SMPL 规范空间），不包含相机——想在图上复现渲染效果还需保存 `pd_cam`（见综合实践）。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：导出的 obj 在 Blender 里"躺着/镜像"，为什么？
**答案**：obj 存的是 SMPL 世界系坐标（米制、y 向上），而渲染图经过 \(R=\mathrm{diag}(-1,-1,1)\) + 透视投影翻转了 x/y；查看器只加载几何不做该变换。这不是数据错误，是坐标系约定差异（u3-l4 讨论过 R 固定的原因）。

**练习 2**：如何只导出"头部"子网格？
**答案**：用 `ehm.smplx.smplx2flame_ind` 索引顶点子集，同时用 `trimesh.Trimesh(...).submesh([face_ids], append=True)` 或按"面片三个顶点都在子集内"过滤 faces；直接只切 vertices 不切 faces 会得到破面。

## 5. 综合实践

**任务：给 `inference_wo_detect.py` 做一个"渲染审计"副本。** 复制为 `render_audit.py`（不要改原文件），对同一张输入图完成：

1. **灯光对比**：按 4.2.4 的三组预设 + 4.3.4 的 `flip_z` 共四组灯光各渲染一张，文件名带预设名拼成 2×2 网格图（`cv2.hconcat/vconcat`），直观对比明暗与色温。
2. **头部高亮**：给 `render_mesh` 传入 `smplx2flame_ind=ehm.smplx.smplx2flame_ind` 再渲染一张，确认 FLAME 替换的头部区域变成淡蓝白（对照 [utils/graphics_utils.py:L808-L810](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L808-L810) 的 `head_color`）。注意入口脚本里这一参数目前是被注释掉的（[inference_images.py:L343](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L343)）。
3. **资产导出**：按 4.4 导出 `result.obj`，同时把 `outputs['pd_cam']`、`body_param`、`flame_param` 存进 `result.npz`（`np.savez`，tensor 先 `.cpu().numpy()`）。
4. **自检**：写 3 个断言——`renderer.faces[0].max() < 10475`；导出 mesh 的 `bounds`（各维极差）与 `pd_smplx_dict['vertices'][:10475]` 的极差一致；`result.npz` 里的 `pd_cam` 形状为 (1,4,4)。

预期产物：一张 2×2 灯光对比图、一张头部高亮图、`result.obj`、`result.npz`。这个任务把本讲三个模块（拓扑加载、光栅化着色、导出）与 u3-l4（相机）、u4-l4（EHM 输出）串成一条可复现的链路；运行结果待本地验证。

## 6. 本讲小结

- `Renderer2` 是薄门面：真正内容是构造时从 `assets/SMPLX/smplx_tex.obj` 读入的拓扑 buffer `faces`（20908 面片、只覆盖前 10475 顶点，**EHM 的 120 个牙齿顶点不被渲染**）与一组当前无人消费的 UV buffer。
- 推理入口不走 `forward` 而直调 `GS_BaseMeshRenderer.render_mesh`：纯色 `TexturesVertex` → `Meshes` → `GS_MeshRasterizer`（view/NDC 两步变换、z 用视图深度覆写，适配 \(w=+z\) 的 GS 投影）+ `SoftPhongShader`（Phong 三项光照）→ 背景置白、拼 RGBA、×255。
- 灯光在着色阶段注入：`PointLights` 的 `location` 与三色参数分别控制光位与 ambient/diffuse/specular 强弱；`inverse_light=True` 把灯放到 \((0,-1,-10)\)，由 \(C=(t_x,t_y,-f/s)\) 的相机中心推导可知这在**相机同侧**（人体正面），非 inverse 的正 z 灯会照在背面。
- 网格导出只需顶点 + faces 两块原料，用 trimesh `process=False` 导出 obj 以保住 SMPL-X 原始顶点顺序；obj 是世界系几何，不含相机信息。
- 值得记的审计点：`manual_lights`、UV buffer、`render_textured_mesh`（对 `Renderer2` 调用会因缺 `faces_uvd` 属性报错）均为备而未用；`self.lights=lights` 是有状态的覆盖；行内注释"512"与实际 1024 不符。

## 7. 下一步学习建议

- 进入单元五：先读 [models/pipeline/pipeline.py:L347](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L347) 与 [models/pipeline/pipeline.py:L405](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L405)，看训练循环如何复用同一个 `render_mesh` 做**可视化与验证**（预测/GT 双网格并排渲染）——你会看到本讲的渲染在训练侧的第二个消费者。
- u5-l4 会讲 app.py 的时序平滑与 mp4 编码，其中逐帧渲染调用的正是本讲的 [app.py:L467](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L467)。
- 若想深挖渲染本身：对照阅读 [models/modules/renderer/base_renderer.py:L206-L322](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/renderer/base_renderer.py#L206-L322) 的 `TextureRenderer`（球谐光照 `add_SHlight` + `TexturesUV` 贴图渲染），它是"如果 PEAR 要做真实贴图渲染"的旧尝试，虽然不在推理链路上，但能帮你看懂 UV buffer 当初为何被加载。
