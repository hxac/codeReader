# 单图数据管线：参考图监督与随机相机混合

## 1. 本讲目标

DreamCraft3D 的每一步训练都需要同时拿到两种「视角」：一张固定的**参考图视角**（提供 RGB、mask、深度、法向监督）和一批**随机采样视角**（提供给扩散引导模型做得分蒸馏）。本讲精读 `threestudio/data/image.py`，搞清楚这两者如何被装进同一个 batch。读完本讲你应该能：

1. 说出 `SingleImageDataModule` 的三个数据集类各自服务哪个阶段，以及训练 batch 的完整键列表；
2. 解释参考视角相机参数（`mvp_mtx`、`c2w4x4`、`rays_o/rays_d`）是如何从球坐标一步步构造出来并写入 batch 的；
3. 解释 `mvp_mtx_ref`/`c2w_ref` 这两个键的产生路径与消费现场——它们与 mask/深度监督的关系；
4. 掌握 `resolution_milestones` 分辨率爬坡机制：`bisect` 换挡、触发图片重载、与 `update_step` 钩子的联动。

本讲承接 u4-l1（随机相机采样器）与 u3-l2（`Updateable.update_step` 机制），不重复其中已讲过的采样分布与钩子遍历细节。

## 2. 前置知识

- **参考视角（reference view）**：用户给定 RGBA 参考图对应的那个相机位姿。它是常量——整轮训练中不变，由 `default_elevation_deg`、`default_azimuth_deg`、`default_camera_distance`、`default_fovy_deg` 四个配置唯一决定。
- **随机视角**：每步从球面上随机采样的相机（u4-l1 讲过的 `RandomCameraIterableDataset`），供扩散引导用。
- **c2w 矩阵**：camera-to-world，把相机坐标系下的点变换到世界坐标系的 \(4\times4\) 矩阵；前三列是相机右/上/后方向在世界的表示，第四列是相机中心位置。
- **mvp 矩阵**：model-view-projection，把世界坐标点直接投影到裁剪空间（可微光栅化器如 nvdiffrast 直接吃这个矩阵），由投影矩阵与 c2w 复合而来。
- **有状态数据集（stateful dataset）**：PyTorch 惯例是数据集存数据、DataLoader 每次取一条；而本讲的数据集把「当前相机、当前图片」存在 `self` 上，`__iter__` 只是无休止地 `yield {}`，真正的 batch 由 `collate_fn` 从 `self` 的属性现场拼出。这是理解本讲的钥匙。
- **BGRA 与 RGBA**：OpenCV `cv2.imread` 读图默认通道序是 BGR(A)，而深度学习约定是 RGB(A)，所以代码里要做一次颜色转换——注意本讲会指出一个例外。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [threestudio/data/image.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py) | 本讲主角：`SingleImageDataModuleConfig`、`SingleImageDataBase`、训练/验证两个数据集类与 `@register("single-image-datamodule")` |
| [threestudio/data/uncond.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py) | u4-l1 已精读；本讲只引用其 `collate` 返回的 dict 与 `RandomCameraDataModuleConfig` |
| [threestudio/systems/dreamcraft3d.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py) | batch 的消费现场：`training_substep` 如何拆包、切换 `random_camera`、注入 `mvp_mtx_ref`/`c2w_ref` |
| [threestudio/models/renderers/nvdiff_rasterizer.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nvdiff_rasterizer.py) | `mvp_mtx_ref` 的最终消费现场：参考视角可见性 mask 的计算 |
| [threestudio/utils/ops.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/ops.py) | `get_rays`（含 `noise_scale` 加噪）、`get_projection_matrix` 等投影工具 |
| [configs/dreamcraft3d-coarse-nerf.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml) | `single-image-datamodule` 在粗阶段的真实配置：双档分辨率、requires_depth/normal 开关 |
| [configs/dreamcraft3d-texture.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml) | texture 阶段单档 1024 分辨率、`use_mixed_camera_config` 开关 |

另外仓库自带的示例三件套在 `load/images/` 下（如 `groot_rgba.png`、`groot_depth.png`、`groot_normal.png`）。注意：README 与默认配置引用的 `hamburger_rgba.png` **并不在仓库里**，动手实践时请改用 groot 等自带数据。

## 4. 核心概念与源码讲解

### 4.1 配置与三类数据集：SingleImageDataModule 的骨架

#### 4.1.1 概念说明

`single-image-datamodule` 是四份阶段配置共同的 `data_type`（见 [configs/dreamcraft3d-coarse-nerf.yaml:6](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L6)）。它对外是一个 Lightning DataModule，对内拆成三个类：

- `SingleImageIterableDataset`：训练用，无限流；
- `SingleImageDataset`：验证/测试/导出用，有限索引数据集；
- 两者共享基类 `SingleImageDataBase`：存放参考相机、加载三件套、实现分辨率换挡——所有真正的逻辑都在这个基类里。

#### 4.1.2 核心流程

```text
SingleImageDataModule.setup(stage)
 ├─ stage 含 "fit"        → SingleImageIterableDataset(cfg, "train")
 │                            └─ 内部再创建 RandomCameraIterableDataset（随机相机流）
 └─ stage 含 "validate"/"test"/"predict" → SingleImageDataset(cfg, split)
                                           └─ 内部创建 RandomCameraDataset（确定性视角环，u4-l1 讲过）

DataLoader 取数（训练）:
  __iter__ 无限 yield {}  →  collate({}) 从 self 属性拼出完整 batch
                                          └─ 末尾再调 random_pose_generator.collate(None)
                                             塞进 batch["random_camera"]
```

#### 4.1.3 源码精读

配置项全部收在一个 dataclass 里，先读懂它，后面所有代码都只是消费这些字段：

[threestudio/data/image.py:L32-L51](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L32-L51)

```python
@dataclass
class SingleImageDataModuleConfig:
    # height and width should be Union[int, List[int]]
    # but OmegaConf does not support Union of containers
    height: Any = 96
    width: Any = 96
    resolution_milestones: List[int] = field(default_factory=lambda: [])
    default_elevation_deg: float = 0.0
    default_azimuth_deg: float = -180.0
    default_camera_distance: float = 1.2
    default_fovy_deg: float = 60.0
    image_path: str = ""
    use_random_camera: bool = True
    random_camera: dict = field(default_factory=dict)
    rays_noise_scale: float = 2e-3
    ...
    requires_depth: bool = False
    requires_normal: bool = False
    ...
    use_mixed_camera_config: bool = False
```

这段定义了数据侧的全部自由度：`height`/`width` 既可以是 `int` 也可以是 `List[int]`（注释解释了为什么类型是 `Any`——OmegaConf 不支持容器联合类型），成对出现时配合 `resolution_milestones` 构成分辨率爬坡表；`default_*` 四个参数决定参考相机；`random_camera` 是嵌套 dict，会被单独解析成 u4-l1 讲过的 `RandomCameraDataModuleConfig`。

注册与三个 loader 的组装：

[threestudio/data/image.py:L313-L327](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L313-L327)

```python
@register("single-image-datamodule")
class SingleImageDataModule(pl.LightningDataModule):
    ...
    def setup(self, stage=None) -> None:
        if stage in [None, "fit"]:
            self.train_dataset = SingleImageIterableDataset(self.cfg, "train")
        if stage in [None, "fit", "validate"]:
            self.val_dataset = SingleImageDataset(self.cfg, "val")
        if stage in [None, "test", "predict"]:
            self.test_dataset = SingleImageDataset(self.cfg, "test")
```

`@register("single-image-datamodule")` 是注册机制的消费端——yaml 里 `data_type: "single-image-datamodule"` 的值就是这个注册名（u3-l1 讲过；顺带一提，`threestudio/data/images.py` 里有个同名注册但不在导入链上，是死代码）。

[threestudio/data/image.py:L332-L345](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L332-L345)

```python
    def general_loader(self, dataset, batch_size, collate_fn=None) -> DataLoader:
        return DataLoader(
            dataset, num_workers=0, batch_size=batch_size, collate_fn=collate_fn
        )

    def train_dataloader(self) -> DataLoader:
        return self.general_loader(
            self.train_dataset,
            batch_size=self.cfg.batch_size,
            collate_fn=self.train_dataset.collate,
        )
```

两个细节值得停留：

1. `num_workers=0`——u4-l1 与 u3-l2 解释过原因：`update_step` 会在训练中途原地修改 `self.height`/`self.width` 等属性，多进程 worker 会各自持有旧副本，换挡永远传不到主进程。
2. 训练 loader 用 `batch_size=self.cfg.batch_size`（默认 1）且自定义 `collate_fn`。IterableDataset 的样本是空 dict `{}`，DataLoader 会把 `batch_size` 个空 dict 打成列表传给 `collate`——而 `collate` 干脆忽略入参，这一点在 4.4 节展开。

#### 4.1.4 代码实践

用三分钟验证「注册名 → 类」的对应关系（在仓库根目录、装好依赖的环境运行）：

1. **实践目标**：确认 yaml 里的 `data_type` 能直接映射到本讲的类。
2. **操作步骤**：在 Python 交互环境执行：

   ```python
   import threestudio
   cls = threestudio.find("single-image-datamodule")
   print(cls)          # 应打印 SingleImageDataModule
   print(cls.__module__)
   ```

3. **需要观察的现象**：打印出的类来自 `threestudio.data.image` 而不是 `threestudio.data.images`。
4. **预期结果**：`<class 'threestudio.data.image.SingleImageDataModule'>`。若环境缺少 `tinycudann` 等编译扩展导致 `import threestudio` 失败，此实践标注**待本地验证**（依赖安装见 u1-l2）。

#### 4.1.5 小练习与答案

**练习 1**：`height: Any = 96` 为什么不写成 `height: Union[int, List[int]] = 96`？

**答案**：源码注释直接回答——OmegaConf 的 structured 模式不支持容器的 Union 类型（`threestudio/data/image.py:34-35` 的注释）。所以类型放宽为 `Any`，合法性检查延后到 `setup` 里用 `isinstance(self.cfg.height, int)` 手工分流。

**练习 2**：`batch_size` 在 `SingleImageDataModuleConfig` 里是 `int = 1`，而这个 batch_size 控制的是参考图视角的 batch 还是随机相机的 batch？

**答案**：都不是真正控制「视角数量」的开关——参考视角永远只有 1 个（batch 顶层的 `rgb` 形状固定 `[1, H, W, 3]`）；随机相机的批量由 `random_camera.batch_size` 控制（如 coarse 配置的 `[1, 1]`）。顶层 `batch_size` 只决定 DataLoader 每次向 `collate` 传几个空 dict，而 `collate` 忽略入参，所以它实际是个「摆设」参数。

### 4.2 参考视角相机：从球坐标到 mvp_mtx

#### 4.2.1 概念说明

参考图是 2D 的，但深度/法向监督、mask 合成都需要知道「这张图是从哪个相机拍出来的」。`SingleImageDataBase.setup` 的前半段就是把这四个标量（仰角、方位角、距离、fovy）翻译成一套完整的相机张量。这个相机与 u4-l1 随机相机的构造**共用同一套几何约定**：右手系、x 朝后 y 朝右 z 朝上、方位角从 +x 转向 +y、相机看负 z（OpenGL 约定）。

#### 4.2.2 核心流程

球坐标转直角坐标：

\[
p = d\begin{bmatrix}\cos\phi\cos\theta \\ \cos\phi\sin\theta \\ \sin\phi\end{bmatrix},\quad
\phi=\text{elevation},\ \theta=\text{azimuth},\ d=\text{camera\_distance}
\]

有了相机位置 \(p\) 与注视点（原点）、世界上方向（+z），正交化出 right/up/lookat 三个轴，拼成 c2w：

```text
lookat = normalize(0 - p)            # 指向原点
right  = normalize(lookat × up_world)
up     = normalize(right × lookat)
c2w(3×4) = [right, up, -lookat | p]  # 第三列取 -lookat：相机看自己的 -z
c2w4x4   = 齐次补 [0,0,0,1]
```

焦距由 fovy 与图像高度换算：

\[
f = \frac{0.5\,H}{\tan(0.5\,\text{fovy})}
\]

注意这里 fovy 是**沿高度方向**的视场角，所以分母用 H 而不是 W。

#### 4.2.3 源码精读

参考相机的完整构造：

[threestudio/data/image.py:L81-L110](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L81-L110)

```python
elevation_deg = torch.FloatTensor([self.cfg.default_elevation_deg])
azimuth_deg = torch.FloatTensor([self.cfg.default_azimuth_deg])
camera_distance = torch.FloatTensor([self.cfg.default_camera_distance])

elevation = elevation_deg * math.pi / 180
azimuth = azimuth_deg * math.pi / 180
camera_position: Float[Tensor, "1 3"] = torch.stack(
    [
        camera_distance * torch.cos(elevation) * torch.cos(azimuth),
        camera_distance * torch.cos(elevation) * torch.sin(azimuth),
        camera_distance * torch.sin(elevation),
    ],
    dim=-1,
)
...
lookat: Float[Tensor, "1 3"] = F.normalize(center - camera_position, dim=-1)
right: Float[Tensor, "1 3"] = F.normalize(torch.cross(lookat, up), dim=-1)
up = F.normalize(torch.cross(right, lookat), dim=-1)
self.c2w: Float[Tensor, "1 3 4"] = torch.cat(
    [torch.stack([right, up, -lookat], dim=-1), camera_position[:, :, None]],
    dim=-1,
)
self.c2w4x4: Float[Tensor, "B 4 4"] = torch.cat(
    [self.c2w, torch.zeros_like(self.c2w[:, :1])], dim=1
)
self.c2w4x4[:, 3, 3] = 1.0
```

这段与 u4-l1 精读过的 [threestudio/data/uncond.py:L204-L308](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L204-L308) 中相机构造逐行同构——区别只是那边 batch 维是 B、参数来自随机采样，这边 batch 维恒为 1、参数来自配置。**这套约定与 Zero123 引导的相机条件编码严格对应**：coarse 配置里 `guidance_3d` 段的 `cond_elevation_deg`/`cond_azimuth_deg`/`cond_camera_distance` 就是插值自这四个 `default_*` 参数（见 [configs/dreamcraft3d-coarse-nerf.yaml:105-L108](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L105-L108)），保证「Zero123 眼里的条件相机」与「参考图实际相机」是同一台。

接着 `set_rays` 把 c2w 展开成体渲染用的射线和光栅化用的 mvp：

[threestudio/data/image.py:L152-L171](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L152-L171)

```python
def set_rays(self):
    # get directions by dividing directions_unit_focal by focal length
    directions: Float[Tensor, "1 H W 3"] = self.directions_unit_focal[None]
    directions[:, :, :, :2] = directions[:, :, :, :2] / self.focal_length

    rays_o, rays_d = get_rays(
        directions,
        self.c2w,
        keepdim=True,
        noise_scale=self.cfg.rays_noise_scale,
        normalize=self.cfg.rays_d_normalize,
    )

    proj_mtx: Float[Tensor, "4 4"] = get_projection_matrix(
        self.fovy, self.width / self.height, 0.01, 100.0
    )  # FIXME: hard-coded near and far
    mvp_mtx: Float[Tensor, "4 4"] = get_mvp_matrix(self.c2w, proj_mtx)

    self.rays_o, self.rays_d = rays_o, rays_d
    self.mvp_mtx = mvp_mtx
```

三个看点：

- `directions_unit_focal` 是「焦距归一」的射线方向模板（`get_ray_directions(H, W, focal=1.0)` 生成，见 [threestudio/data/image.py:L136-L139](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L136-L139)），除以真实焦距 `focal_length`（[L140-L142](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L140-L142) 按上式逐档预计算）后得到像素级射线方向。
- `noise_scale=self.cfg.rays_noise_scale`（默认 2e-3）是本模块**独有**的：u4-l1 随机相机的 `get_rays` 调用不传这个参数。它的作用在 [threestudio/utils/ops.py:L255-L259](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/ops.py#L255-L259)——给射线加一点随机抖动，避免固定整数像素网格在 NeRF 训练中烙下网格状伪影。
- near/far 硬编码为 `0.01, 100.0`（源码自带 FIXME 注释），与 uncond.py 里的 `0.1, 1000.0` 不同——读码时别把两处混为一谈。

#### 4.2.4 代码实践

1. **实践目标**：用配置里的默认参数手算参考相机位置，与代码产出对拍。
2. **操作步骤**：coarse 配置里 `default_elevation_deg: 0.0`、`default_azimuth_deg: 0.0`、`default_camera_distance: 3.8`（[configs/dreamcraft3d-coarse-nerf.yaml:L12-L15](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L12-L15)）。按本节公式手算：仰角 0、方位角 0 时 \(p = 3.8\cdot(1, 0, 0)\)；再在环境里执行

   ```python
   import math, torch
   d, phi, theta = 3.8, 0.0, 0.0
   p = torch.tensor([
       d * math.cos(phi) * math.cos(theta),
       d * math.cos(phi) * math.sin(theta),
       d * math.sin(phi),
   ])
   print(p)  # tensor([3.8000, 0.0000, 0.0000])
   ```

3. **需要观察的现象**：相机在 +x 轴上，即「x 朝后」约定的正后方；此时参考图视角就是 Zero123 的条件视角。
4. **预期结果**：手算与公式一致；随后可对照 4.4 节实践脚本打印的 `batch["camera_positions"]` 是否也是 `[3.8, 0, 0]`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 c2w 的旋转部分第三列是 `-lookat` 而不是 `lookat`？

**答案**：OpenGL/NVDiffRasterization 约定相机看向自身坐标系的 **-z**。`lookat` 是「相机指向原点」的世界方向，要让世界方向映射到相机 -z，旋转矩阵第三列（相机 +z 轴在世界的表示）必须取 `-lookat`。这与 `get_projection_matrix` 中 y 分量带负号（[threestudio/utils/ops.py:L275-L277](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/ops.py#L275-L277)）同为「适配 nvdiffrast 输出的翻转」。

**练习 2**：`c2w`（3×4）与 `c2w4x4` 各自服务谁？

**答案**：`c2w` 是 3×4 紧凑型，喂给 `get_rays`/`get_mvp_matrix` 这类内部工具；`c2w4x4` 是齐次 4×4，直接放进 batch（collate 里两个都放，见 4.4 节），供系统或渲染器做通用矩阵乘。

### 4.3 load_images：RGBA/深度/法向三件套的加载约定

#### 4.3.1 概念说明

u2-l1 讲过 `preprocess_image.py` 如何产出三件套；本节看训练侧如何按**文件名约定**把它们加载回来：`image_path` 指向 `*_rgba.png`，深度图与法向图通过字符串替换 `_rgba.png → _depth.png / _normal.png` 推导出来。加载行为受 `requires_depth`/`requires_normal` 两个开关控制——它们决定 `self.depth`/`self.normal` 是张量还是 `None`。

#### 4.3.2 核心流程

```text
load_images()
 ├─ 断言 image_path 存在
 ├─ cv2.imread(IMREAD_UNCHANGED) → BGRA
 ├─ cvtColor(BGRA2RGBA) → resize 到 (width, height) → /255
 ├─ rgb   = rgba[..., :3]              → self.rgb   [1,H,W,3] float
 ├─ mask  = rgba[..., 3:] > 0.5        → self.mask  [1,H,W,1] bool
 ├─ requires_depth  → 读 *_depth.png  resize → /255 → self.depth  [1,H,W,1] float（否则 None）
 └─ requires_normal → 读 *_normal.png resize → /255 → self.normal [1,H,W,3] float（否则 None）
```

三张图 resize 到**同一分辨率**且插值一致（`INTER_AREA`），保证像素级对齐——这是深度/法向监督成立的前提。

#### 4.3.3 源码精读

RGB 与 mask：

[threestudio/data/image.py:L173-L196](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L173-L196)

```python
rgba = cv2.cvtColor(
    cv2.imread(self.cfg.image_path, cv2.IMREAD_UNCHANGED), cv2.COLOR_BGRA2RGBA
)
rgba = (
    cv2.resize(rgba, (self.width, self.height), interpolation=cv2.INTER_AREA)
    .astype(np.float32)
    / 255.0
)
rgb = rgba[..., :3]
self.rgb: Float[Tensor, "1 H W 3"] = (
    torch.from_numpy(rgb).unsqueeze(0).contiguous().to(self.rank)
)
self.mask: Float[Tensor, "1 H W 1"] = (
    torch.from_numpy(rgba[..., 3:] > 0.5).unsqueeze(0).to(self.rank)
)
```

注意 `cv2.resize` 的尺寸参数顺序是 `(width, height)`——与直觉相反的 OpenCV 惯例。mask 直接用 `alpha > 0.5` 二值化，**dtype 是 bool**，消费端（如 `dreamcraft3d.py` 的 `gt_mask.float()`）再按需转浮点。

深度与法向：

[threestudio/data/image.py:L199-L234](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L199-L234)

```python
# load depth
if self.cfg.requires_depth:
    depth_path = self.cfg.image_path.replace("_rgba.png", "_depth.png")
    assert os.path.exists(depth_path)
    depth = cv2.imread(depth_path, cv2.IMREAD_UNCHANGED)
    depth = cv2.resize(depth, (self.width, self.height), interpolation=cv2.INTER_AREA)
    self.depth: Float[Tensor, "1 H W 1"] = (
        torch.from_numpy(depth.astype(np.float32) / 255.0).unsqueeze(0).to(self.rank)
    )
else:
    self.depth = None

# load normal
if self.cfg.requires_normal:
    normal_path = self.cfg.image_path.replace("_rgba.png", "_normal.png")
    ...
    self.normal: Float[Tensor, "1 H W 3"] = (...)
else:
    self.normal = None
```

两个读码观察（都不是我们改，而是理解行为时要心里有数）：

1. **normal 分支没有做 BGR→RGB 转换**（对比 rgba 分支的 `cvtColor`）。法向图的通道语义取决于预处理保存时的约定，消费端 `dreamcraft3d.py` 的法向损失里也有对应的轴翻转处理（`FIXME: reverse x axis`，见 [threestudio/systems/dreamcraft3d.py:L176-L185](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L176-L185)），两侧约定互相咬合。
2. **文件名约定是硬匹配**：`replace("_rgba.png", "_depth.png")` 要求输入路径必须以 `_rgba.png` 结尾，换个名字（比如 `ref.png`）训练时就会 `assert os.path.exists` 失败。这也是 u2-l1 强调「训练侧只靠文件名约定」的原因。

再看配置侧开关如何联动（coarse 阶段）：

[configs/dreamcraft3d-coarse-nerf.yaml:L16-L17](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L16-L17)

```yaml
  requires_depth: true
  requires_normal: ${cmaxgt0:${system.loss.lambda_normal}}
```

`requires_depth` 是硬写的 `true`——尽管 `lambda_depth: 0.0`，但 `lambda_depth_rel: 0.05`（相对深度损失，u6-l3 会精读）同样消费 `ref_depth`，所以深度图必须加载；`requires_normal` 则用 u2-l2 讲过的 `cmaxgt0` resolver 随 `lambda_normal` 的可达上界自动联动：`lambda_normal: 0.0` 时为 `False`，`self.normal` 为 `None`，法向图根本不读。

#### 4.3.4 代码实践

1. **实践目标**：直观验证 mask 二值化阈值与三件套的像素对齐。
2. **操作步骤**：用仓库自带的 groot 三件套（在仓库根目录运行）：

   ```python
   import cv2, numpy as np
   rgba = cv2.imread("load/images/groot_rgba.png", cv2.IMREAD_UNCHANGED)
   rgba = cv2.cvtColor(rgba, cv2.COLOR_BGRA2RGBA)
   depth = cv2.imread("load/images/groot_depth.png", cv2.IMREAD_UNCHANGED)
   alpha = rgba[..., 3]
   print("alpha>0.5 占比:", (alpha > 0.5).mean())
   # 检查深度图在 mask 外是否为 0（u2-l1：背景置零）
   fg = depth[(alpha > 127)]
   bg = depth[(alpha <= 127)]
   print("前景深度均值:", fg.mean(), "背景深度均值:", bg.mean())
   ```

3. **需要观察的现象**：mask 覆盖率是物体的合理占屏比；前景深度均值明显大于 0、背景深度均值接近 0。
4. **预期结果**：两行统计与 u2-l1 预处理脚本的输出约定（近亮远暗、背景置零）一致。若本机无环境，标注**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果把 coarse 配置的 `requires_depth` 改成 `false`，训练会发生什么？

**答案**：`self.depth = None`，collate 后 `batch["ref_depth"]` 为 `None`；而 `training_substep` 的相对深度损失无条件解包 `batch["ref_depth"]`（当 `lambda_depth_rel > 0` 时，见 [threestudio/systems/dreamcraft3d.py:L169](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L169)），对 `None` 做索引会直接抛 `TypeError`。这正是配置里把 `requires_depth` 硬写为 `true` 的原因。

**练习 2**：为什么三张图都用 `INTER_AREA` 插值而不是默认插值？

**答案**：`INTER_AREA` 是像素面积平均，适合**缩小**图像且抗混叠；三件套必须用同一插值核 resize，否则同一物体边缘在 RGB 与 depth/normal 上会错位 1-2 像素，边缘处的深度/法向监督就脏了。

### 4.4 collate：嵌套 batch 的组装与 mvp_mtx_ref/c2w_ref 的去向

#### 4.4.1 概念说明

这是本讲的核心模块。训练 batch 是一个**两层嵌套**结构：顶层是参考视角的全部信息（图片三件套 + 参考相机），`batch["random_camera"]` 键下藏着随机相机子 batch。系统侧（`dreamcraft3d-system`）在两种子步之间切换：参考监督子步用顶层 batch 渲染（等于「从参考相机渲染场景」），扩散引导子步切换到 `batch["random_camera"]` 渲染——但切换前会把参考相机的 `mvp_mtx`/`c2w4x4` 改名存为 `mvp_mtx_ref`/`c2w_ref` 随身携带。

#### 4.4.2 核心流程

```text
collate(任意入参，被忽略) → 返回 dict：
  顶层（参考视角，batch 维恒为 1）
    rays_o/rays_d      [1,H,W,3]   体渲染射线
    mvp_mtx            [4,4]       参考视角 mvp
    camera_positions   [1,3]  light_positions [1,3]
    elevation/azimuth  [1]（度）  camera_distances [1]
    rgb [1,H,W,3]  mask [1,H,W,1]  ref_depth [1,H,W,1]|None  ref_normal [1,H,W,3]|None
    height/width       cfg 原始值（可能是 list！）
    c2w [1,3,4]  c2w4x4 [1,4,4]（c2w4x4 = 齐次化 c2w）
  嵌套
    random_camera = RandomCameraIterableDataset.collate(None)
                    （u4-l1 的随机相机 batch：rays_o/rays_d/mvp_mtx/c2w/... batch 维=B）

系统侧 training_substep（消费现场）：
  ref 子步：直接用顶层 batch → 渲染视角 == 参考视角 → out["depth"] 与 ref_depth 像素对齐
  guidance 子步：batch = batch["random_camera"]，再把
                 mvp_mtx_ref = 顶层 mvp_mtx、c2w_ref = 顶层 c2w4x4 塞回 batch
```

#### 4.4.3 源码精读

训练数据集的 collate：

[threestudio/data/image.py:L259-L281](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L259-L281)

```python
def collate(self, batch) -> Dict[str, Any]:
    batch = {
        "rays_o": self.rays_o,
        "rays_d": self.rays_d,
        "mvp_mtx": self.mvp_mtx,
        "camera_positions": self.camera_position,
        "light_positions": self.light_position,
        "elevation": self.elevation_deg,
        "azimuth": self.azimuth_deg,
        "camera_distances": self.camera_distance,
        "rgb": self.rgb,
        "ref_depth": self.depth,
        "ref_normal": self.normal,
        "mask": self.mask,
        "height": self.cfg.height,
        "width": self.cfg.width,
        "c2w": self.c2w,
        "c2w4x4": self.c2w4x4,
    }
    if self.cfg.use_random_camera:
        batch["random_camera"] = self.random_pose_generator.collate(None)

    return batch
```

四个关键读点：

1. **入参 `batch` 被立即覆盖**——数据集是有状态的，每个训练步的「随机性」全部来自最后一行 `random_pose_generator.collate(None)`（随机相机采样发生在这里），参考视角部分则永远不变。
2. 深度/法向在 batch 里的键名带 `ref_` 前缀（`ref_depth`/`ref_normal`），与 `rgb`/`mask` 的裸键名形成语义区分：前者是「另一来源的监督信号」，后者是「参考视角的渲染目标」。
3. `"height": self.cfg.height` 放的是**原始配置值**——coarse 配置下这是一个 list `[128, 384]` 而非当前档位的 int（对比 `random_camera` 子 batch 里是换挡后的 `self.height`，见 [threestudio/data/uncond.py:L340-L341](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L340-L341)）。渲染器并不依赖它：体渲染器直接从 `rays_o.shape` 推 H/W（[threestudio/models/renderers/nerf_volume_renderer.py:L126](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L126)）。读码时不要被这个键误导。
4. `use_random_camera=False` 时不嵌套随机相机——但四份阶段配置都没有用到这个开关，纹理阶段也只是把 `guidance_3d` 留空（u2-l3），随机相机仍然存在（BSD 蒸馏仍需随机视角渲染）。

系统侧的拆包与切换：

[threestudio/systems/dreamcraft3d.py:L91-L109](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L91-L109)

```python
def training_substep(self, batch, batch_idx, guidance: str, render_type="rgb"):
    gt_mask = batch["mask"]
    gt_rgb = batch["rgb"]
    gt_depth = batch["ref_depth"]
    gt_normal = batch["ref_normal"]
    mvp_mtx_ref = batch["mvp_mtx"]
    c2w_ref = batch["c2w4x4"]

    if guidance == "guidance":
        batch = batch["random_camera"]

    # Support rendering visibility mask
    batch["mvp_mtx_ref"] = mvp_mtx_ref
    batch["c2w_ref"] = c2w_ref

    out = self(batch)
```

这十行是本讲的枢纽：先把参考视角的监督量与相机**在切换前**取出来；`guidance == "guidance"` 时整个 batch 换成随机相机子 batch（顶层那些 `rgb`/`mask`/`ref_depth` 从此不在 batch 里，这也是为什么 `gt_*` 必须提前解包）；最后无论如何都把参考相机的两个矩阵以 `*_ref` 之名注入。于是：

- **深度监督为什么不需要 `mvp_mtx_ref`**：ref 子步不切换 batch，渲染用的 `mvp_mtx` 本来就是参考视角的，`out["depth"]` 与 `ref_depth` 天然逐像素对齐（深度损失见 [threestudio/systems/dreamcraft3d.py:L155-L173](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L155-L173)）。
- **`mvp_mtx_ref` 的真正消费现场**在 texture 阶段的 nvdiff 光栅化器：

[threestudio/models/renderers/nvdiff_rasterizer.py:L57-L77](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nvdiff_rasterizer.py#L57-L77)

```python
if render_mask:
    # get front-view visibility mask
    with torch.no_grad():
        mvp_mtx_ref = kwargs["mvp_mtx_ref"] # FIXME
        v_pos_clip_front: Float[Tensor, "B Nv 4"] = self.ctx.vertex_transform(
            mesh.v_pos, mvp_mtx_ref
        )
        rast_front, _ = self.ctx.rasterize(v_pos_clip_front, mesh.t_pos_idx, (height, width))
        mask_front = rast_front[..., 3:]
        mask_front = mask_front[mask_front > 0] - 1.
        faces_vis = mesh.t_pos_idx[mask_front.long()]
        ...
        out.update({"mask": 1.0 - mask_vis.float()})
```

当从随机视角渲染时，网格的**背面**在参考图里根本看不见，若拿参考图全图去算损失会冤枉背面顶点。这段代码用参考视角的 mvp 把网格再光栅化一遍，找出参考视角下可见的三角面，再映回当前视角得到可见性 mask——它与参考 RGB 损失中的 `grow_mask`（边缘容差，[threestudio/systems/dreamcraft3d.py:L139-L141](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L139-L141)）共同构成 texture 阶段「只惩罚看得见的部分」的机制（u6-l3 再展开损失侧）。

验证/测试数据集则走另一条路产生同名键：

[threestudio/data/image.py:L297-L310](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L297-L310)

```python
def __len__(self):
    return len(self.random_pose_generator)

def __getitem__(self, index):
    batch = self.random_pose_generator[index]
    batch.update(
        {
        "height": self.random_pose_generator.cfg.eval_height,
        "width": self.random_pose_generator.cfg.eval_width,
        "mvp_mtx_ref": self.mvp_mtx[0],
        "c2w_ref": self.c2w4x4,
    }
    )
    return batch
```

`SingleImageDataset` 本身没有相机——它的每个样本直接取自确定性视角环（`random_pose_generator[index]`），再补上参考相机的 `mvp_mtx_ref`/`c2w_ref`。所以这两个键有**两条产生路径**：训练路径由系统在 `training_substep` 里注入，评估路径由数据集在 `__getitem__` 里注入，殊途同归于渲染器。

#### 4.4.4 代码实践（本讲主实践）

1. **实践目标**：实例化 `SingleImageDataModule` 取一个真实训练 batch，打印全部键与张量形状，并可视化三件套。
2. **操作步骤**：在仓库根目录新建 `inspect_batch.py`（示例代码，属于我们自己的脚本而非项目源码）：

   ```python
   import torch
   import threestudio  # noqa: F401  触发注册
   from threestudio.data.image import SingleImageDataModule

   cfg = {
       "image_path": "./load/images/groot_rgba.png",   # 仓库自带三件套
       "height": [128, 384],
       "width": [128, 384],
       "resolution_milestones": [3000],
       "default_elevation_deg": 0.0,
       "default_azimuth_deg": 0.0,
       "default_camera_distance": 3.8,
       "default_fovy_deg": 20.0,
       "requires_depth": True,
       "requires_normal": True,
       "use_random_camera": True,
       "random_camera": {
           "height": [128, 384], "width": [128, 384],
           "batch_size": [1, 1], "resolution_milestones": [3000],
           "eval_height": 512, "eval_width": 512,
           "elevation_range": [-10, 45], "azimuth_range": [-180, 180],
           "camera_distance_range": [3.8, 3.8], "fovy_range": [20.0, 20.0],
           "progressive_until": 200,
           "camera_perturb": 0.0, "center_perturb": 0.0, "up_perturb": 0.0,
       },
   }

   dm = SingleImageDataModule(cfg)
   dm.setup("fit")
   batch = next(iter(dm.train_dataloader()))

   def describe(d, prefix=""):
       for k, v in d.items():
           if isinstance(v, dict):
               describe(v, prefix + k + ".")
           elif torch.is_tensor(v):
               print(f"{prefix}{k}: Tensor {tuple(v.shape)} {v.dtype}")
           else:
               print(f"{prefix}{k}: {type(v).__name__} = {v}")

   describe(batch)

   import matplotlib.pyplot as plt
   fig, axes = plt.subplots(1, 4, figsize=(16, 4))
   axes[0].imshow(batch["rgb"][0]);          axes[0].set_title("rgb (原图, carvekit 抠图)")
   axes[1].imshow(batch["mask"][0, ..., 0], cmap="gray"); axes[1].set_title("mask (原图 alpha>0.5)")
   axes[2].imshow(batch["ref_depth"][0, ..., 0], cmap="gray"); axes[2].set_title("ref_depth (Omnidata)")
   axes[3].imshow(batch["ref_normal"][0]);   axes[3].set_title("ref_normal (Omnidata)")
   for ax in axes: ax.axis("off")
   plt.tight_layout(); plt.savefig("batch_inspect.png", dpi=120)
   ```

   运行 `python inspect_batch.py`。

3. **需要观察的现象**：
   - 顶层键与 4.4.2 的清单一致：`rgb` 为 `[1,128,128,3] float32`、`mask` 为 `[1,128,128,1] bool`、`ref_depth` 为 `[1,128,128,1]`、`ref_normal` 为 `[1,128,128,3]`、`mvp_mtx` 为 `[4,4]`、`c2w` 为 `[1,3,4]`、`c2w4x4` 为 `[1,4,4]`；`height`/`width` 打印为 `list = [128, 384]`（印证 4.4.3 读点 3）；
   - `random_camera.` 前缀下是 u4-l1 的键（`rays_o [1,128,128,3]`、`c2w [1,4,4]`、`fovy [1]` 等），且每次重跑 `elevation`/`azimuth` 数值都会变——随机性只发生在子 batch；
   - `batch_inspect.png` 中 rgb 与 mask 轮廓一致，depth 图前亮后暗、背景为 0，normal 图呈彩色。
4. **预期结果**：形状清单与上图现象全部吻合；若 matplotlib 未安装则 `pip install matplotlib`。本脚本未在当前环境执行过，运行结果**待本地验证**。注意两个坑：(a) 不要直接加载 yaml 的 `data` 段——`requires_normal: ${cmaxgt0:${system.loss.lambda_normal}}` 的插值引用了 `system` 段，脱离整份配置单独解析会失败，所以上面手写 dict；(b) 脚本必须在仓库根目录运行，`image_path` 是相对路径。

#### 4.4.5 小练习与答案

**练习 1**：`collate` 为什么可以完全忽略入参 `batch`？这种设计下「每个 epoch」还有意义吗？

**答案**：因为 `SingleImageIterableDataset` 是有状态数据集：`__iter__` 只是 `while True: yield {}`（[threestudio/data/image.py:L287-L289](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L287-L289)），空样本只是给 DataLoader 一个「再来一批」的节拍信号，真正的数据全部从 `self` 属性读。epoch 概念在此失效——训练步数由 `trainer.max_steps` 控制，数据永不枯竭。

**练习 2**：`training_substep` 里为什么必须在 `batch = batch["random_camera"]` **之前**取出 `mvp_mtx`/`c2w4x4`？

**答案**：切换后变量 `batch` 指向子 dict，而子 dict 里也有同名的 `mvp_mtx`/`c2w`（随机相机的）。若先切换再取，取到的是随机相机矩阵，`mvp_mtx_ref` 就名不副实，nvdiff 渲染器的参考视角可见性 mask 会算错。先取出、再切换、再改名注入，是这段代码的顺序关键。

**练习 3**：训练 batch 顶层的 `elevation`/`azimuth` 与 `random_camera.elevation`/`random_camera.azimuth` 各是什么？

**答案**：前者是参考相机的固定角度（来自 `default_elevation_deg`/`default_azimuth_deg`，形状 `[1]`，如 coarse 阶段的 `0.0`/`0.0`）；后者是本步随机采样的角度（形状 `[B]`，度数，供 Zero123 编码相对位姿条件，u4-l1 讲过）。同名不同义，读日志时别混。

### 4.5 update_step_：resolution_milestones 分辨率爬坡

#### 4.5.1 概念说明

coarse 阶段配置 `height: [128, 384]` + `resolution_milestones: [3000]` 的含义是：0～2999 步用 128² 训练，3000 步起切到 384²。难点在于「换挡」要同时改四样东西：当前 H/W、射线方向模板、焦距、以及**已经加载成张量的三件套图片**（必须按新分辨率重读重缩放）。这一切收拢在一个不到 15 行的 `update_step_` 里，靠 `bisect` 二分查找定位当前档位。

#### 4.5.2 核心流程

```text
setup 阶段（建档位表）:
  heights = [128, 384]           # 来自 cfg.height（int 则包成长度 1 的 list）
  milestones = [-1] + [3000]     # 前面垫一个 -1 作为第 0 档起点
  断言 len(heights) == len(milestones)      # 即 len(cfg.milestones) + 1
  预计算每档: directions_unit_focals[k], focal_lengths[k]

update_step_(epoch, global_step)（每批训练前被调用）:
  size_ind = bisect_right(milestones, global_step) - 1
  global_step=2999 → bisect_right([-1,3000], 2999)=1 → ind 0 → 128
  global_step=3000 → bisect_right([-1,3000], 3000)=2 → ind 1 → 384
  height 没变 → return（零开销）
  height 变了 → 换 width/方向模板/焦距 → set_rays() → load_images()（按新分辨率重读三件套）
```

垫 `-1` 的意义：`bisect_right([-1, m], 0) - 1 = 0`，保证第 0 步就落在第 0 档；而 `bisect_right` 取「右边界」意味着恰好在 milestone 步（如 3000）就已换挡，不是 3001。

#### 4.5.3 源码精读

建档位表（setup 的后半段）：

[threestudio/data/image.py:L118-L148](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L118-L148)

```python
self.heights: List[int] = (
    [self.cfg.height] if isinstance(self.cfg.height, int) else self.cfg.height
)
...
assert len(self.heights) == len(self.widths)
self.resolution_milestones: List[int]
if len(self.heights) == 1 and len(self.widths) == 1:
    if len(self.cfg.resolution_milestones) > 0:
        threestudio.warn(
            "Ignoring resolution_milestones since height and width are not changing"
        )
    self.resolution_milestones = [-1]
else:
    assert len(self.heights) == len(self.cfg.resolution_milestones) + 1
    self.resolution_milestones = [-1] + self.cfg.resolution_milestones

self.directions_unit_focals = [
    get_ray_directions(H=height, W=width, focal=1.0)
    for (height, width) in zip(self.heights, self.widths)
]
self.focal_lengths = [
    0.5 * height / torch.tan(0.5 * self.fovy) for height in self.heights
]
```

注意约束 `len(heights) == len(cfg.resolution_milestones) + 1`：N 档分辨率只需 N-1 个里程碑；写错个数会在 setup 当场断言失败（fail fast）。texture 阶段 `height: 1024` 是单档，milestones 直接被忽略为 `[-1]`（[configs/dreamcraft3d-texture.yaml:L9-L10](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L9-L10) 里干脆不写这个键）。

换挡逻辑：

[threestudio/data/image.py:L239-L251](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L239-L251)

```python
def update_step_(self, epoch: int, global_step: int, on_load_weights: bool = False):
    size_ind = bisect.bisect_right(self.resolution_milestones, global_step) - 1
    self.height = self.heights[size_ind]
    if self.height == self.prev_height:
        return

    self.prev_height = self.height
    self.width = self.widths[size_ind]
    self.directions_unit_focal = self.directions_unit_focals[size_ind]
    self.focal_length = self.focal_lengths[size_ind]
    threestudio.debug(f"Training height: {self.height}, width: {self.width}")
    self.set_rays()
    self.load_images()
```

`prev_height` 是免重启优化：绝大多数步落在同一档，比较一次高度就返回，`set_rays`/`load_images` 只在真正换挡时各执行一次。调用链则由训练子数据集双委托接通：

[threestudio/data/image.py:L283-L285](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L283-L285)

```python
def update_step(self, epoch: int, global_step: int, on_load_weights: bool = False):
    self.update_step_(epoch, global_step, on_load_weights)
    self.random_pose_generator.update_step(epoch, global_step, on_load_weights)
```

第一行换参考视角的档（含图片重载），第二行让随机相机流也同步换挡（u4-l1 讲过它还顺带做 `progressive_view` 渐进视角）。谁在调用它？u3-l3 精读过 `BaseSystem.on_train_batch_start` 里的 `update_if_possible(self.dataset, ...)`（[threestudio/systems/base.py:L176-L177](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L176-L177)）——每批训练开始前、`training_step` 之前，先刷新数据集。所以「分辨率爬坡」没有独立的调度器，完全是 u3-l2 的 `Updateable` 钩子机制的一次应用。

最后看本讲规格中提到的多相机混合配置——一个实验性分支：

[threestudio/data/image.py:L60-L75](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L60-L75)

```python
if self.cfg.use_random_camera:
    random_camera_cfg = parse_structured(
        RandomCameraDataModuleConfig, self.cfg.get("random_camera", {})
    )
    # FIXME: 
    if self.cfg.use_mixed_camera_config:
        if self.rank % 2 == 0:
            random_camera_cfg.camera_distance_range=[self.cfg.default_camera_distance, self.cfg.default_camera_distance]
            random_camera_cfg.fovy_range=[self.cfg.default_fovy_deg, self.cfg.default_fovy_deg]
            self.fixed_camera_intrinsic = True
        else:
            self.fixed_camera_intrinsic = False
```

`use_mixed_camera_config: true` 时**按进程 rank 分流**：偶数 rank 的随机相机内参（距离、fovy）被钉死成参考相机的值，奇数 rank 保持原配置范围——用于多卡训练时混合「与参考图同内参」和「自由内参」两种视角分布。geometry 与 texture 两份配置都带这个开关（[configs/dreamcraft3d-texture.yaml:L17](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L17) 与 [L43](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L43) 透传给 system），但默认 `false`，且源码带 FIXME 注释——属于未定稿的实验路径，了解即可，不建议在生产配置中开启。

#### 4.5.4 代码实践

1. **实践目标**：亲眼看到换挡时刻 batch 内容的变化，验证 `bisect` 边界。
2. **操作步骤**：在 4.4.4 脚本的基础上追加：

   ```python
   ds = dm.train_dataset
   print("换挡前:", ds.rgb.shape, ds.height)          # [1,128,128,3] 128
   ds.update_step(0, 2999)                            # 边界前一步
   print("step=2999 后:", ds.rgb.shape, ds.height)    # 仍 128
   ds.update_step(0, 3000)                            # 恰在 milestone
   print("step=3000 后:", ds.rgb.shape, ds.height)    # [1,384,384,3] 384
   batch2 = next(iter(dm.train_dataloader()))
   print("新 batch rgb:", batch2["rgb"].shape)        # [1,384,384,3]
   print("随机相机也换挡:", batch2["random_camera"]["height"])  # 384（int）
   ```

3. **需要观察的现象**：2999 步调用后一切不变（`prev_height` 短路）；3000 步调用后三件套被重读为 384²、顶层 `mvp_mtx` 因宽高比与焦距变化而更新；再取 batch，随机相机子 batch 的 `height` 也变成 384。
4. **预期结果**：如上四行注释。注意手动调用 `update_step` 只是模拟 Lightning 的每批刷新，真实训练中不需要（也不应该）手工调它。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：把配置改成 `height: [128, 256, 384]`，`resolution_milestones` 该写几个？若误写成 `[3000, 5000, 8000]` 会怎样？

**答案**：应写 2 个（如 `[3000, 5000]`），因为断言要求 `len(heights) == len(milestones) + 1`。误写 3 个会在 `setup` 阶段触发 `assert`，训练还没开始就报错——这是刻意的 fail-fast 设计。

**练习 2**：换挡时 `load_images` 被再次调用，会不会把 `requires_depth=False` 的行为改变？`self.prev_height` 初值是什么时候设的？

**答案**：不会——`load_images` 每次都完整重跑，`requires_depth` 为 `False` 时依然把 `self.depth` 置为 `None`，行为与首次一致。`prev_height` 在 `setup` 末尾初始化为第一档高度（[threestudio/data/image.py:L150](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L150)），因此第一次 `update_step_`（第 0 步）也会命中短路返回。

**练习 3**：为什么参考图视角的射线要加 `rays_noise_scale=2e-3` 噪声，而随机相机视角不加？

**答案**：参考视角三件套每步都以**完全相同的像素网格**重复监督 NeRF，固定的整数像素采样容易让密度场学出与网格对齐的伪影；加微扰等效于对采样位置做数据增广。随机视角每步相机都不同，采样天然多样，无需额外抖动。

## 5. 综合实践

把本讲四个模块串成一个「batch 体检报告」任务。在仓库根目录完成：

1. 复用 4.4.4 的脚本框架，取一个训练 batch，写一个 `report(batch)` 函数输出三段信息：
   - **顶层参考视角**：`rgb`/`mask`/`ref_depth`/`ref_normal` 的形状与 dtype，`camera_positions` 的数值（对照 4.2 手算的 `[3.8, 0, 0]`）；
   - **嵌套随机相机**：`random_camera` 下的 `c2w` 与顶层 `c2w4x4` 的差异（各是什么视角），`fovy` 与顶层 `default_fovy_deg` 的关系；
   - **监督链路标注**：用文本注释列出 `rgb`→原图（carvekit 抠图）、`mask`→原图 alpha、`ref_depth`/`ref_normal`→Omnidata 预测（u2-l1），并说明 `mvp_mtx_ref`/`c2w_ref` 在训练与验证两条路径上分别由谁产生（4.4.3）。
2. 追加 4.5.4 的换挡实验，把换挡前后的 `mvp_mtx` 也打印出来，解释矩阵为什么变了（提示：`get_projection_matrix` 的参数里有 `width/height`）。
3. 把 `batch_inspect.png` 与 `report` 的输出保存下来——下一讲（u5-l1 隐式体积几何）你会看到这些 `rays_o/rays_d` 如何被 `nerf-volume-renderer` 消费，届时可回头对照。

若本机暂无 GPU 环境，任务 1、2 中的张量检查部分**待本地验证**；任务 3 的注释梳理不依赖运行，可先完成。

## 6. 本讲小结

- `single-image-datamodule` 用三个类分工：`SingleImageDataBase` 承载全部状态与逻辑，`SingleImageIterableDataset`（训练、无限流）与 `SingleImageDataset`（验证/测试、确定性视角环）只是两种外壳；DataLoader 必须 `num_workers=0`，因为状态会被 `update_step` 原地修改。
- 训练 batch 是双层嵌套：顶层为**恒定的参考视角**（三件套 + 参考相机），`batch["random_camera"]` 下是**每步重新采样**的随机相机 batch；`collate` 完全忽略入参，数据全部来自数据集自身属性。
- `mvp_mtx_ref`/`c2w_ref` 有两条产生路径（训练由系统在切换 `random_camera` 前注入、评估由 `__getitem__` 注入），主要消费现场是 nvdiff 渲染器的参考视角可见性 mask；深度监督不需要它，因为 ref 子步的渲染矩阵本身就是参考视角。
- `resolution_milestones` 爬坡 = `bisect` 在 `[-1]+里程碑` 表上换挡 + 重设射线 + **按新分辨率重读三件套**，由 `Updateable.update_step` 钩子每批驱动，`prev_height` 短路保证零换挡开销。
- 配置侧有三个易踩的坑：`requires_depth` 硬写 `true` 是为了 `lambda_depth_rel`；直接加载 yaml 的 `data` 段会因 `${cmaxgt0:...}` 插值失败；`use_mixed_camera_config` 是带 FIXME 的按 rank 分流实验分支，默认关闭。

## 7. 下一步学习建议

数据侧到此闭环：你已经知道每一步训练拿到的 batch 长什么样。下一步进入**单元五（3D 表示与可微渲染）**：

- **u5-l1 隐式体积几何**：本讲顶层 batch 的 `rays_o`/`rays_d` 将被 `nerf-volume-renderer` 沿射线采样，喂给 `implicit-volume` 的密度 MLP——建议先读 [threestudio/models/geometry/implicit_volume.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_volume.py)，重点看它如何消费 `bounds` 与密度初始化。
- **u6-l3 参考图监督损失**：本讲反复出现的 `ref_depth`/`ref_normal`/`grow_mask` 的损失侧实现（最小二乘深度对齐、Pearson 相对深度、法向余弦）在 `training_substep` 的后半段，读完 u5 再看会非常顺畅。
- 想先松一口气的话，可以重跑 4.4.4 脚本并改成 `use_random_camera: False`，观察 batch 少了哪个键——这能反向加深你对嵌套结构的理解。
