# 随机相机：RandomCameraIterableDataset 与射线几何

## 1. 本讲目标

DreamCraft3D 没有＂数据集照片＂——它只有一个三维场景（NeRF/NeuS/网格）和一台不断换位置的虚拟相机。每个训练步，系统都要现采一批相机位姿，渲染出图，再拿去让扩散先验打分。本讲拆开这台＂虚拟相机采样器＂，学完后你应当能：

1. 读懂 `RandomCameraDataModuleConfig` 里每一个采样旋钮（仰角/方位角/距离/视场角/扰动）的含义与 DreamCraft3D 各阶段的取值策略。
2. 讲清一条完整链路：球面参数采样 → 球坐标转直角坐标 → 构造 `c2w` 相机矩阵 → 生成 `rays_o/rays_d` 射线 → 构造 `mvp_mtx` 投影矩阵。
3. 理解 `get_ray_directions`、`get_rays`、`get_projection_matrix`、`get_mvp_matrix` 这四个投影几何工具函数的数学含义。
4. 解释 `progressive_until` 渐进视角（从参考视角逐渐张开到全球面）与 `batch_uniform_azimuth`（批内方位角分桶覆盖）两个策略的动机。
5. 独立实例化 `RandomCameraIterableDataset`，不启动任何训练就把相机采样分布画出来。

本讲是纯几何与采样逻辑，**不需要 GPU、不需要预训练权重**，是全书最适合＂轻装实验＂的一讲。

## 2. 前置知识

- **相机外参与内参**：三维渲染里一台相机由两组参数决定。外参（`c2w`，camera-to-world 矩阵）回答＂相机摆在哪、朝哪看＂；内参（焦距 `focal`、主点、视场角 `fovy`）回答＂底片有多广角＂。
- **坐标系约定**：threestudio 采用右手坐标系，**x 朝后、y 朝右、z 朝上**；相机自身遵循 OpenGL 约定——看向自身坐标系的 \(-z\) 方向，\(x\) 朝右、\(y\) 朝上。方位角（azimuth）从 \(+x\) 转向 \(+y\)，范围 \((-180, 180)\)；仰角（elevation）即与水平面的夹角，范围 \((-90, 90)\)。
- **球坐标**：相机位置用＂方位角 \(\theta\) + 仰角 \(\varphi\) + 距离 \(d\)＂描述，转成直角坐标：
  \[ x = d\cos\varphi\cos\theta,\quad y = d\cos\varphi\sin\theta,\quad z = d\sin\varphi \]
- **mvp 矩阵**：光栅化渲染器（nvdiff）不消费射线，而是消费 `mvp = proj @ w2c`（投影矩阵 × 世界到相机矩阵），把顶点直接投到屏幕像素。所以数据模块要同时产出两套 ＂相机描述＂：给体渲染器的 `rays_o/rays_d`，和给光栅化渲染器的 `mvp_mtx`。
- **update_step 钩子**（第 u3-l2 讲）：所有组件实现 `Updateable.update_step(epoch, global_step)`，每个训练批次开始前被 `BaseSystem` 递归调用。本讲的分辨率爬坡与渐进视角正是靠它驱动。
- **Janus 问题**（第 u1-l1 讲）：纯文本先验会让模型生成＂多面体＂。DreamCraft3D 用 Zero123 视图相关先验对抗它——这也解释了为什么本讲的相机参数被钉得那么死（见 4.1）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [threestudio/data/uncond.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py) | 随机相机数据集全家桶：`RandomCameraDataModuleConfig`（配置）、`RandomCameraIterableDataset`（训练用无限采样流）、`RandomCameraDataset`（验证/测试用确定性视角环）、`RandomCameraDataModule`（注册名 `random-camera-datamodule`） |
| [threestudio/utils/ops.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/ops.py) | 投影几何工具箱：`get_ray_directions`（像素→相机系射线）、`get_rays`（相机系→世界系射线）、`get_projection_matrix` / `get_mvp_matrix`（光栅化投影） |
| configs/dreamcraft3d-coarse-nerf.yaml | 真实取值样本：`data.random_camera` 段展示 DreamCraft3D 如何拧这些旋钮 |
| threestudio/systems/dreamcraft3d.py（延伸） | 消费现场：`batch = batch["random_camera"]` 取出本讲产出的 batch |
| threestudio/models/guidance/stable_zero123_guidance.py（延伸） | 消费现场：用 batch 里的 `elevation/azimuth` 构造 Zero123 的相对位姿条件 |

## 4. 核心概念与源码讲解

### 4.1 配置面板：RandomCameraDataModuleConfig

#### 4.1.1 概念说明

随机相机的全部行为由一个 dataclass 描述。把它当成一台＂虚拟云台＂的控制面板：分辨率决定底片大小，四个 range 决定云台在球面上的活动范围，三个 perturb 决定云台的抖动，light 系列决定补光灯怎么摆。

#### 4.1.2 核心流程

配置字段按功能分五组：

| 分组 | 字段 | 默认值 | 含义 |
| --- | --- | --- | --- |
| 底片 | `height` / `width` / `batch_size` | 64 / 64 / 1 | 可为 `int` 或 `List[int]`，配合 `resolution_milestones` 做多档分辨率爬坡 |
| 底片 | `resolution_milestones` | `[]` | 换挡步数列表，长度须等于分辨率档数减一 |
| 云台范围 | `elevation_range` | (-10, 90) | 仰角范围（度） |
| 云台范围 | `azimuth_range` | (-180, 180) | 方位角范围（度） |
| 云台范围 | `camera_distance_range` | (1, 1.5) | 相机到原点距离范围 |
| 云台范围 | `fovy_range` | (40, 70) | 垂直视场角（度），沿图像高度方向 |
| 抖动 | `camera_perturb` / `center_perturb` / `up_perturb` | 0.1 / 0.2 / 0.02 | 相机位置、注视中心、上方向的随机扰动幅度 |
| 灯光 | `light_position_perturb` / `light_distance_range` / `light_sample_strategy` | 1.0 / (0.8, 1.5) / "dreamfusion" | 灯光方向扰动、距离范围、采样策略 |
| 评估 | `eval_elevation_deg` / `eval_camera_distance` / `eval_fovy_deg` / `eval_height` / `eval_width` / `n_val_views` / `n_test_views` | 15 / 1.5 / 70 / 512 / 512 / 1 / 120 | 验证/测试视角环的固定参数 |
| 策略 | `batch_uniform_azimuth` / `progressive_until` / `rays_d_normalize` | true / 0 / true | 批内方位角分桶、渐进视角步数、射线是否单位化 |

对照 DreamCraft3D 粗阶段真实配置（`configs/dreamcraft3d-coarse-nerf.yaml` 的 `data.random_camera` 段）：

| 字段 | coarse-nerf 取值 | 与默认的差异及原因 |
| --- | --- | --- |
| `elevation_range` | [-10, 45] | 收窄仰角，符合 Zero123 训练数据的视角分布 |
| `camera_distance_range` | [3.8, 3.8] | **钉死距离**：渲染相机必须与 Zero123 条件相机严格对应 |
| `fovy_range` | [20.0, 20.0] | **钉死视场角**，配置注释写明 ＂Zero123 has fixed fovy＂ |
| `camera_perturb` 等 | 全 0.0 | 关闭抖动——扰动会破坏与 Zero123 条件位姿的对应关系 |
| `progressive_until` | 200 | 开启渐进视角（默认 0 即关闭） |
| `batch_uniform_azimuth` | false | 训练 batch_size 为 1，开关退化为等价（见 4.3） |
| `height/width` | [128, 384] + milestones [3000] | 第 3000 步从 128 升到 384 |

#### 4.1.3 源码精读

配置定义在 [threestudio/data/uncond.py:L27-L59](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L27-L59)：dataclass 逐字段给出上述默认值。注意 L29-L33 的注释——OmegaConf 不支持容器类型的 Union，所以 `height/width/batch_size` 声明为 `Any`，由数据集构造函数自己判断是 `int` 还是 `List[int]`。

真实取值见 [configs/dreamcraft3d-coarse-nerf.yaml:L18-L39](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L18-L39)：`random_camera` 作为 `data` 的子段存在——DreamCraft3D 实际用 `single-image-datamodule`（第 u4-l2 讲），它内部再创建本讲的随机相机数据集。

`Rays_d_normalize` 默认打开的原因写在 [threestudio/data/uncond.py:L319](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L319) 的注释里：`the returned rays_d MUST be normalized!`——体渲染器（nerfacc）把沿射线的参数 \(t\) 当作距离度量，若射线不单位化，\(t\) 就不再等于欧氏距离，采样区间全部失真。

#### 4.1.4 代码实践

**目标**：用命令行验证你能精准控制这台云台。

1. 打开 [configs/dreamcraft3d-coarse-nerf.yaml:L26-L33](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L26-L33)，逐字段抄下 `random_camera` 的取值。
2. 用第 u2-l2 讲的点号语法，在命令行把仰角范围临时改为 `[-30, 60]`：
   ```bash
   python launch.py --config configs/dreamcraft3d-coarse-nerf.yaml --train \
       data.random_camera.elevation_range="[-30,60]" \
       system.prompt_processor.prompt="a hamburger" \
       --gpu 0
   ```
3. 训练启动后打开 `outputs/.../configs/parsed.yaml`，确认覆盖已生效。

**观察现象**：`parsed.yaml` 中 `elevation_range` 变为 `[-30, 60]`，其余不变。
**预期结果**：验证＂配置即旋钮＂。若只做静态阅读不启动训练，本步标注为**待本地验证**（该命令需要完整环境与权重）。

#### 4.1.5 小练习与答案

1. **问**：为什么 coarse 阶段把 `camera_distance_range` 和 `fovy_range` 钉成单点，而上游 threestudio 默认让它们随机？
   **答**：Zero123 是视图条件先验，它假设＂渲染视角相对参考视角的偏移＂与给它的条件一致；距离或视场角漂移会让渲染画面与条件预期的取景不一致，梯度方向随之错乱。钉死二者（并关闭 perturb）保证采样相机与条件相机严格对应。
2. **问**：想训练分辨率 64→128 两档、第 1000 步切换，配置怎么写？
   **答**：`height: [64, 128]`、`width: [64, 128]`、`resolution_milestones: [1000]`，三列表长度须相等，且 milestones 长度 = 档数 − 1。
3. **问**：把 `rays_d_normalize` 改成 `false` 会破坏什么？
   **答**：`rays_d` 的模长会随焦距缩放（各像素不等），nerfacc 中沿射线积分的 \(t\) 不再等于欧氏距离，近远采样平面、密度场尺度约定全部失效。

### 4.2 无限数据集：IterableDataset、分辨率爬坡与 num_workers=0

#### 4.2.1 概念说明

常规 `Dataset` 有固定长度（一张照片一个样本）。随机相机没有＂样本＂概念——它是一个**无限随机流**：每个训练步现采一批新位姿，永不重复。PyTorch 对这类场景的答案就是 `IterableDataset`。它的三个设计要点都服务于＂运行时自我修改＂：

1. `__iter__` 永远产出空字典，真正的采样发生在 `collate`；
2. 分辨率/渐进视角靠 `update_step` 在**每个批次开始前**刷新自身属性；
3. DataLoader 强制 `num_workers=0`，保证属性修改对采样可见。

#### 4.2.2 核心流程

```
每个训练批次开始
→ BaseSystem 分发 update_step（第 u3-l2 讲）
→ RandomCameraIterableDataset.update_step(epoch, global_step)
   ├─ bisect 在 [-1, 3000] 中换挡 → 选出当前 height/width/batch_size/directions_unit_focal
   └─ progressive_view(global_step) → 收缩或放开 elevation/azimuth 采样范围
→ DataLoader 向迭代器要一个 batch（得到 {}）
→ collate([{}]) 忽略输入，现场采样一批新相机位姿
→ 产出 dict（rays_o/rays_d/mvp_mtx/c2w/...）
```

#### 4.2.3 源码精读

**构造与换挡准备**（[threestudio/data/uncond.py:L62-L104](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L62-L104)）：`__init__` 把 `int` 规整成单元素列表（L66-L77），构造 `resolution_milestones = [-1] + 配置值`（L91），并**预计算每一档分辨率下 focal=1.0 的射线方向网格**存进 `self.directions_unit_focals`（L93-L96）——之后每个 batch 只需一次除法就能适配任意焦距，不必重建 meshgrid。

**update_step**（[threestudio/data/uncond.py:L106-L116](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L106-L116)）：`bisect_right(milestones, global_step) - 1` 选出当前档位。coarse 配置下 milestones 为 `[-1, 3000]`：第 2999 步 `bisect_right` 返回 1 → 档 0（128）；第 3000 步返回 2 → 档 1（384）。随后调用 `progressive_view`（见 4.5）。

**无限迭代**（[threestudio/data/uncond.py:L118-L120](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L118-L120)）：`while True: yield {}`——迭代器永不枯竭，Lightning 靠 `max_steps` 决定何时停。

**num_workers=0 的原因**（[threestudio/data/uncond.py:L489-L497](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L489-L497)）：L492-L494 的注释直言——若想在运行时修改 `self.width/self.height`，**必须**关掉多进程。多 worker 会 fork 出属性快照，主进程的换挡与渐进视角对采样进程不可见。`train_dataloader`（L499-L502）还传 `batch_size=None`：batch 完全由 `collate_fn` 自己组装，DataLoader 不再打包。

**注册入口**（[threestudio/data/uncond.py:L470-L476](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L470-L476)）：`@register("random-camera-datamodule")` 把 `RandomCameraDataModule` 挂进注册表（第 u3-l1 讲），`__init__` 里 `parse_structured` 完成严格配置校验。

#### 4.2.4 代码实践

**目标**：单靠 bisect 模拟换挡，不实例化任何 torch 对象。

1. 在 Python 里执行：
   ```python
   import bisect
   milestones, heights = [-1, 3000], [128, 384]
   for step in [0, 1500, 2999, 3000, 4999]:
       idx = bisect.bisect_right(milestones, step) - 1
       print(step, "->", heights[idx])
   ```
2. 对照 [threestudio/data/uncond.py:L107-L110](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L107-L110) 逐行核对你的实现与源码是否一致。

**观察现象**：输出 `0 -> 128, 1500 -> 128, 2999 -> 128, 3000 -> 384, 4999 -> 384`。
**预期结果**：第 3000 步**恰好**切换到高分辨率档（bisect_right 对相等值取右侧）。

#### 4.2.5 小练习与答案

1. **问**：为什么 DataLoader 必须 `num_workers=0`？
   **答**：数据集在主进程中被 `update_step` 修改 `self.height/width/directions_unit_focal`；多 worker 持有 fork 出的旧副本，分辨率爬坡与渐进视角对采样全部失效。
2. **问**：`__iter__` yield 空字典、采样全在 collate，这样的设计换来什么？
   **答**：采样时机被推迟到 collate，此刻 `update_step` 刷新过的最新属性（分辨率、视角范围）必然生效；同时 batch 内容与迭代器解耦，batch_size 可随档位变化。
3. **问**：`RandomCameraIterableDataset` 与 `RandomCameraDataset`（4.5）为何分成两个类？
   **答**：训练要＂无限随机流＂（IterableDataset），验证/测试要＂可复现的固定视角环＂（带 `__len__`/`__getitem__` 的普通 Dataset），语义不同故拆开。

### 4.3 collate 采样核心：从球面参数到 c2w 矩阵

#### 4.3.1 概念说明

`collate` 是整台采样器的心脏，约 200 行做完六件事：采仰角 → 采方位角 → 采距离与视场角 → 球坐标转直角坐标 → 加扰动 → 组装 `c2w`。两个采样策略值得单独记：

- **仰角双策略**：每个 batch 抛一次硬币——一半批次在仰角度数上均匀采样（球面上天然偏向两极），一半批次用**反变换采样**在球面面积上均匀。
- **`batch_uniform_azimuth` 批内分桶**：把方位角全程等分 `B` 个桶，每个样本落在自己的桶内随机位置，保证一个 batch 合起来必然覆盖 360°。

#### 4.3.2 核心流程

```
collate(batch):                      # 输入被忽略
1. 抛硬币选仰角策略:
   A) 仰角均匀:  φ ~ U(φmin, φmax)            # 极点偏置
   B) 球面均匀:  u ~ U(0,1); φ = asin(u·(sinφmax − sinφmin) + sinφmin)
2. 方位角:
   batch_uniform_azimuth=True:  θ_k = (u_k + k)/B · Δθ + θmin, k=0..B-1, u_k~U(0,1)
   否则:                        θ ~ U(θmin, θmax)
3. 距离 d ~ U(dmin, dmax);  fovy ~ U(fmin, fmax)
4. 球坐标 → 直角坐标: p = (d·cosφ·cosθ, d·cosφ·sinθ, d·sinφ)
5. 扰动: p += U(-cp, cp);  center += N(0, ctr_p);  up = (0,0,1) + N(0, up_p)
6. 组装正交基:
   lookat = normalize(center − p);  right = normalize(lookat × up);  up = normalize(right × lookat)
   c2w = [ right | up | −lookat | p ]（列向量），末行 (0,0,0,1)
7. 灯光采样（dreamfusion / magic3d 两种策略）
8. 交给 ops 工具产出 rays_o/rays_d 与 proj/mvp（见 4.4）
```

其中步骤 1B 的数学依据：球面上纬度环周长 \(\propto \cos\varphi\)，欲在球面**面积**上均匀，仰角密度须 \(p(\varphi) \propto \cos\varphi\)，其累积分布为

\[ F(\varphi) = \frac{\sin\varphi - \sin\varphi_{\min}}{\sin\varphi_{\max} - \sin\varphi_{\min}} \]

对 \(u \sim U(0,1)\) 取反函数即得 \(\varphi = \arcsin\big(u\,(\sin\varphi_{\max} - \sin\varphi_{\min}) + \sin\varphi_{\min}\big)\)——与源码逐字对应。混合两种策略的净效果是让采样略偏两极：对 Zero123 而言极向视角的相对位姿条件更＂稳＂，多给一些样本。

#### 4.3.3 源码精读

**仰角双策略**（[threestudio/data/uncond.py:L143-L172](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L143-L172)）：L147 `random.random() < 0.5` 是**批级**硬币（Python 随机数，每批抛一次）；分支 A（L149-L154）仰角均匀采样，注释标明 ＂biased towards poles＂；分支 B（L156-L172）用 `torch.asin` 做 4.3.2 中的反变换采样，实现球面面积均匀。

**方位角分桶**（[threestudio/data/uncond.py:L174-L192](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L174-L192)）：

```python
azimuth_deg = (
    torch.rand(self.batch_size) + torch.arange(self.batch_size)
) / self.batch_size * (az_max - az_min) + az_min
```

第 \(k\) 个样本落在 \(\big[\theta_{\min} + \frac{k}{B}\Delta\theta,\ \theta_{\min} + \frac{k+1}{B}\Delta\theta\big)\) 内，批内合起来覆盖全程——这是 ＂ensures sampled azimuth angles in a batch cover the whole range＂（L177）的含义。注意 coarse 配置 `batch_size=[1,1]`：\(B=1\) 时两个分支公式退化为相同，所以配置里写 `false` 毫无影响。

**球坐标转换**（[threestudio/data/uncond.py:L201-L211](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L201-L211)）：注释写明坐标系约定 ＂x back, y right, z up; azimuth from +x to +y＂，与 4.3.2 公式一致。

**三路扰动**（[threestudio/data/uncond.py:L220-L235](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L220-L235)）：相机位置加**均匀**噪声 \([-c_p, c_p]\)，注视中心与上方向加**高斯**噪声。coarse 配置三者全为 0，此段直接跳过。

**c2w 组装**（[threestudio/data/uncond.py:L298-L308](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L298-L308)）：

```python
lookat = F.normalize(center - camera_positions, dim=-1)
right  = F.normalize(torch.cross(lookat, up), dim=-1)
up     = F.normalize(torch.cross(right, lookat), dim=-1)
c2w3x4 = torch.cat([torch.stack([right, up, -lookat], dim=-1),
                    camera_positions[:, :, None]], dim=-1)
```

先由 ＂看向哪里＂（lookat）与 ＂头顶朝哪＂（up）正交化出右手相机系；旋转矩阵三列取 `[right, up, -lookat]`——第三列是 \(-\)lookat，因为 OpenGL 相机看向自身 \(-z\)，视线方向对应相机系的负 z 轴。平移列就是相机位置。val/test 版本同一套构造见 [threestudio/data/uncond.py:L399-L409](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L399-L409)。

**灯光采样**（[threestudio/data/uncond.py:L244-L296](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L244-L296)）：`dreamfusion` 策略（L251-L261）让灯光方向 ＝ 相机方向加高斯抖动后归一化，再乘以随机距离——＂随身打光＂；`magic3d` 策略（L262-L292）在相机邻域构造局部坐标系，把灯光限制在与视线成 30°~90° 的锥形区域内。这些位置仅供着色用，与几何采样无关。

**产出 dict**（[threestudio/data/uncond.py:L330-L344](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L330-L344)）：batch 同时携带两套相机描述——给体渲染的 `rays_o/rays_d` 与给光栅化的 `mvp_mtx`；还有一个易踩坑的细节：**返回的 `elevation` 与 `azimuth` 是度数**（L337-L338 存的是 `elevation_deg/azimuth_deg`）。消费现场在 [threestudio/models/guidance/stable_zero123_guidance.py:L212-L224](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py#L212-L224)：Zero123 引导把 ＂渲染相机 − 参考相机＂ 的相对仰角/方位角编码成条件向量（内部才 `deg2rad`），这就是采样相机与先验条件挂钩的位置；系统侧的取用点在 [threestudio/systems/dreamcraft3d.py:L104-L109](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L104-L109)（`batch = batch["random_camera"]`）。

#### 4.3.4 代码实践（本讲主实践 · 上半部分）

**目标**：不启动训练，直接实例化数据集，取 8 个 batch，把 64 个相机位置画在球面上。

1. 在仓库根目录新建 `vis_camera.py`（示例代码，仓库中不存在）：
   ```python
   import torch, matplotlib.pyplot as plt
   from torch.utils.data import DataLoader
   from threestudio.data.uncond import (
       RandomCameraIterableDataset, RandomCameraDataModuleConfig)

   torch.manual_seed(0)
   cfg = RandomCameraDataModuleConfig(          # 直接构造 dataclass，绕过 OmegaConf
       height=64, width=64, batch_size=8,
       elevation_range=(-10, 45), azimuth_range=(-180, 180),
       camera_distance_range=(3.8, 3.8), fovy_range=(20.0, 20.0),
       camera_perturb=0.0, center_perturb=0.0, up_perturb=0.0,
       eval_elevation_deg=0.0, progressive_until=0, batch_uniform_azimuth=True,
   )
   dataset = RandomCameraIterableDataset(cfg)
   loader = DataLoader(dataset, num_workers=0, batch_size=None,
                        collate_fn=dataset.collate)   # 与 train_dataloader 完全一致

   batches = []
   for i, batch in enumerate(loader):
       if i >= 8: break                              # 迭代器无限，手动截断
       batches.append(batch)

   pos = torch.cat([b["c2w"][:, :3, 3] for b in batches])   # 取 c2w 平移分量
   az  = torch.cat([b["azimuth"] for b in batches])          # 度数，用作颜色

   fig = plt.figure(figsize=(6, 6))
   ax = fig.add_subplot(projection="3d")
   ax.scatter(pos[:, 0], pos[:, 1], pos[:, 2], c=az, cmap="hsv", s=25)
   ax.set_xlabel("x (back)"); ax.set_ylabel("y (right)"); ax.set_zlabel("z (up)")
   plt.savefig("camera_dist.png", dpi=150)
   print("z range:", pos[:, 2].min().item(), pos[:, 2].max().item())
   print("c2w translation == camera_positions:",
         torch.allclose(pos, torch.cat([b["camera_positions"] for b in batches])))
   ```
2. 运行 `python vis_camera.py`（需 u1-l2 的完整环境；纯 CPU 即可，`import threestudio` 会连带导入整个包）。

**观察现象**：
- 64 个点构成一条球面带：距离恒为 3.8，\(z\) 落在 \([3.8\sin(-10°),\ 3.8\sin 45°] \approx [-0.66,\ 2.69]\)；
- 按方位角着色时颜色绕环正好走完一圈 HSV 色环（`batch_uniform_azimuth=True` 的功劳）；
- `torch.allclose` 打印 `True`——`c2w` 平移列与 `camera_positions` 按构造相等。

**预期结果**：得到 `camera_dist.png`，分布如上。若环境不含 matplotlib/torch，本实践标注为**待本地验证**。

#### 4.3.5 小练习与答案

1. **问**：两种仰角采样策略各自的球面分布是什么？
   **答**：分支 A 在仰角度数上均匀，纬度环越靠近极点越小，等量样本挤在极区 → 极点偏置；分支 B 经 asin 反变换实现球面面积均匀。
2. **问**：`batch_size=1` 时 `batch_uniform_azimuth` 有区别吗？
   **答**：没有。\(B=1\) 时 \((u + 0)/1 = u\)，两个分支公式退化为同一形式；coarse 配置 batch_size 为 1，故写 `false` 无实际影响。
3. **问**：`c2w` 旋转部分第三列为什么是 \(-\)lookat 而不是 lookat？
   **答**：OpenGL 约定相机看向自身 \(-z\)；lookat 是 ＂相机→目标＂ 的视线方向，恰为相机系的 \(-z\) 轴，故 z 轴列取 \(-\)lookat，保证右手系。

### 4.4 ops.py 射线与投影工具四件套

#### 4.4.1 概念说明

`collate` 把几何计算委托给 `threestudio/utils/ops.py` 的四个函数：`get_ray_directions` 生成相机坐标系下每像素的射线方向，`get_rays` 把它旋转平移到世界系得到 `rays_o/rays_d`（给体渲染器），`get_projection_matrix` + `get_mvp_matrix` 产出屏幕投影矩阵（给 nvdiff 光栅化器）。理解它们等于理解 ＂一条射线如何从一个像素诞生＂。

#### 4.4.2 核心流程

```
像素网格 (i, j)                    ← get_ray_directions（focal=1 预存，逐 batch 除以 f）
    ↓ direction_cam = ((i−cx)/fx, −(j−cy)/fy, −1)
旋转到世界系                        ← get_rays: rays_d = R·dir, rays_o = t（广播到每像素）
    ↓ 归一化 rays_d
投影矩阵                            ← get_projection_matrix(fovy, aspect, near, far)
    ↓ mvp = proj @ w2c              ← get_mvp_matrix（w2c 由 c2w 转置导出）
供 nvdiff 光栅化 / 导出器使用
```

其中焦距由垂直视场角换算：

\[ f = \frac{H/2}{\tan(\mathrm{fovy}/2)} \]

（fovy 沿图像高度方向定义，故只与 \(H\) 相关。）

#### 4.4.3 源码精读

**get_ray_directions**（[threestudio/utils/ops.py:L180-L217](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/ops.py#L180-L217)）：L207-L211 用 `indexing="xy"` 的 meshgrid 生成像素网格（i 沿宽、j 沿高），加 0.5 取像素中心；L213-L215 堆出方向

```python
directions = torch.stack([(i - cx) / fx, -(j - cy) / fy, -torch.ones_like(i)], -1)
```

两个符号都重要：\(y\) 分量取负，因为图像行号 \(j\) 向下增长而相机 \(y\) 轴向上；\(z\) 恒为 \(-1\)，再次体现 OpenGL ＂看 \(-z\)＂ 约定。principal 默认取图像中心 \((W/2, H/2)\)。

**unit-focal 复用**（[threestudio/data/uncond.py:L310-L317](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L310-L317)）：L311 按上式算出每样本焦距，L312-L317 把预存的 `directions_unit_focal` 复制后仅对前两维除以焦距——网格与分辨率绑定、按档缓存，焦距每步随机也只需一次逐元素除法。

**get_rays**（[threestudio/utils/ops.py:L220-L266](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/ops.py#L220-L266)）：以训练走的 4D 分支（L248-L253）为例——`directions[:, :, :, None, :] * c2w[:, None, None, :3, :3]` 后 `sum(-1)` 即矩阵乘 \(R\cdot d\)，得到世界系方向；`rays_o` 直接取 `c2w[:, :3, 3]` 广播到每像素。L261-L262 的 `F.normalize` 响应 ＂MUST be normalized＂ 约定；`keepdim=True`（[threestudio/data/uncond.py:L320-L322](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L320-L322) 调用处）保留 `(B, H, W, 3)` 形状以便与渲染图对齐。L257-L259 还有一个默认关闭的 `noise_scale` 技巧（对 rays_o/rays_d 加噪声消除网格状伪影）。

**get_projection_matrix**（[threestudio/utils/ops.py:L269-L281](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/ops.py#L269-L281)）：构造 OpenGL 风格透视矩阵

\[ P = \begin{bmatrix} \dfrac{1}{a\tan(\mathrm{fovy}/2)} & 0 & 0 & 0\\[4pt] 0 & -\dfrac{1}{\tan(\mathrm{fovy}/2)} & 0 & 0\\[4pt] 0 & 0 & -\dfrac{f+n}{f-n} & -\dfrac{2fn}{f-n}\\[4pt] 0 & 0 & -1 & 0 \end{bmatrix} \]

（\(a\) 为宽高比，\(n/f\) 为近远平面。）L275-L277 的注释点明 \([1,1]\) 取负是**特意为 nvdiffrast 翻转 y 轴**。调用处 [threestudio/data/uncond.py:L324-L326](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L324-L326) 把 near/far 硬编码为 0.1/1000.0，源码留有 `FIXME` 标记。

**get_mvp_matrix**（[threestudio/utils/ops.py:L284-L295](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/ops.py#L284-L295)）：先由 c2w 解析求逆得 w2c——旋转取转置 \(R^\top\)、平移取 \(-R^\top t\)（对刚体变换等价于求逆，省一次矩阵求逆），再 `mvp = proj @ w2c`。同文件 L298-L301 的 `get_full_projection_matrix`（`c2w @ proj`）供需要 full projection 的渲染路径使用，`uncond.py` 顶部（L17-L23）把这三件套一并导入。

#### 4.4.4 代码实践

**目标**：用数值检验焦距公式与射线单位化约定。

1. 在 4.3.4 脚本的末尾追加：
   ```python
   import math
   f = 0.5 * 64 / math.tan(math.radians(20) / 2)     # H=64, fovy=20°
   print("expected focal:", f)                        # ≈ 181.6
   rd = torch.cat([b["rays_d"] for b in batches])     # (64, 64, 64, 3)
   print("rays_d norm mean:", rd.norm(dim=-1).mean().item())   # 应为 1.0
   ```
2. 把 4.3.4 的取图分支改成直接读 batch：`batch["proj_mtx"][:, 0, 0]` 与 `1/(math.tan(math.radians(20)/2))`（H=W 时 \(a=1\)）对比。

**观察现象**：焦距 ≈ 181.6（64/2 ÷ tan10°）；`rays_d` 范数均值 ≈ 1.0；`proj_mtx[0,0]` ≈ 5.67 与 \(1/\tan 10°\) 一致。
**预期结果**：公式、源码、batch 三方数值互洽。若未跑通，标注**待本地验证**。

#### 4.4.5 小练习与答案

1. **问**：`get_ray_directions` 里 \(y\) 分量为何取负、\(z\) 分量为何恒为 \(-1\)？
   **答**：图像行号 \(j\) 向下、相机 \(y\) 向上，方向需翻转；OpenGL 相机看向 \(-z\)，故前方像素的 z 分量为负。
2. **问**：为什么预存 `focal=1.0` 的方向网格、用的时候再除以焦距？
   **答**：网格只取决于分辨率，按档缓存后每个 batch 只做一次除法即可适配任意随机 fovy，避免逐步重建 meshgrid。
3. **问**：`proj_mtx[1,1]` 为什么要加负号？
   **答**：nvdiffrast 输出的 y 轴方向与 OpenGL 约定相反，预先在投影矩阵里翻转，光栅化结果才与相机系约定对齐（源码注释 L276-L277）。

### 4.5 progressive_view 渐进视角与 val/test 确定性视角

#### 4.5.1 概念说明

`progressive_until` 实现 ＂先把参考视角学扎实，再逐步环视全球面＂：训练初期把采样范围收缩到参考图相机附近的一个点，随步数线性张开到配置的完整范围。DreamCraft3D 粗阶段设 `progressive_until=200`——前 200 步相机从参考视角（仰角 0°、方位角 0°）逐渐散开。这样做的动机：参考图只提供一个视角的可靠监督，Zero123 的相对位姿条件也是 ＂离参考视角越近越准＂，先近后远的课程式调度能避免训练一开始就在完全未见的视角上产生互相矛盾的梯度。

与之配套，`RandomCameraDataset` 为验证/测试生成**确定性的视角环**：固定仰角/距离/fovy，方位角均匀绕一圈，保证每个 epoch 的验证画面可横向对比。

#### 4.5.2 核心流程

```
progressive_view(global_step):
    r = min(1, global_step / (progressive_until + 1))     # 进度 0→1
    elevation_range ← (1−r)·[eval_el, eval_el] + r·配置范围   # 从参考仰角张开
    azimuth_range   ← (1−r)·[0, 0]          + r·配置范围      # 从方位角 0 张开
    # camera_distance_range 与 fovy_range 的插值被注释停用（L132-L141）

step=0: r=0        → 范围退化为单点（参考视角）
step=until: r≈1    → 张开到完整配置范围
step>until: r=1    → 保持完整范围（progressive_until=0 时第 1 步即全开）
```

#### 4.5.3 源码精读

**progressive_view**（[threestudio/data/uncond.py:L122-L141](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L122-L141)）：`r = min(1.0, global_step / (progressive_until + 1))`；仰角从 `eval_elevation_deg`（coarse 配置经插值取 `data.default_elevation_deg = 0.0`，即参考图相机仰角）插值到配置范围两端（L124-L127），方位角从 0 插值到配置范围（L128-L131）。距离与 fovy 的对应插值代码被注释停用（L132-L141）——与 ＂钉死 distance/fovy＂ 的策略一致。该方法只被 [threestudio/data/uncond.py:L116](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L116) 的 `update_step` 调用，即随 u3-l2 的钩子机制每批刷新。

**RandomCameraDataset 视角环**（[threestudio/data/uncond.py:L347-L441](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py#L347-L441)）：L358-L363 生成方位角——`val` 分支取 `linspace(0, 360, n+1)[:n]`（注释：＂make sure the first and last view are not the same＂，去掉与首视角重复的端点），`test` 分支取 `linspace(0, 360, n)`（如 n_test_views=120）；L364-L396 把仰角、距离、fovy 固定为 `eval_elevation_deg/eval_camera_distance/eval_fovy_deg`，灯光位置直接等于相机位置（L397）。随后与训练路径共用同一套 c2w/射线/投影构造（L399-L431），`__getitem__`（L446-L462）按下标返回单视角 dict——结构训练 batch 完全一致，下游渲染器无需感知差异。

#### 4.5.4 代码实践（本讲主实践 · 下半部分）

**目标**：直观看到 progressive_until 如何控制采样范围的张开过程，并对比不同 `elevation_range` 的分布差异。

1. 在 4.3.4 脚本基础上追加（示例代码）：
   ```python
   fig = plt.figure(figsize=(15, 4.5))
   for k, step in enumerate([0, 100, 250]):
       dataset.update_step(epoch=0, global_step=step)   # 手动模拟训练推进
       b = dataset.collate([{}])                         # collate 忽略输入
       p = b["c2w"][:, :3, 3]
       ax = fig.add_subplot(1, 3, k + 1, projection="3d")
       ax.scatter(p[:, 0], p[:, 1], p[:, 2], c=b["azimuth"], cmap="hsv", s=25)
       ax.set_title(f"step={step}, elev_range={dataset.elevation_range}")
   plt.savefig("progressive.png", dpi=150)

   from dataclasses import replace
   cfg2 = replace(cfg, elevation_range=(-10, 90))        # 对照组：放大仰角
   ds2 = RandomCameraIterableDataset(cfg2); ds2.update_step(0, 250)
   p2 = ds2.collate([{}])["c2w"][:, :3, 3]
   print("wide-elev z range:", p2[:, 2].min().item(), p2[:, 2].max().item())
   ```
2. 运行并打开 `progressive.png`。

**观察现象**：
- `step=0`（r=0）：仰角/方位角范围都是单点，扰动又为 0，8 个点**完全重合**在 \((3.8, 0, 0)\)——图上只剩一个点；
- `step=100`（r≈0.498）：仰角范围约 \([-4.98, 22.4]\)，点云呈窄带；
- `step=250`（r=1）：恢复完整 \([-10, 45]\) 球面带；
- 对照组 `elevation_range=(-10, 90)`：\(z\) 上限升到 \(3.8\sin 90° = 3.8\)，出现＂俯视到顶＂的样本。
- 把 `progressive_until` 改回 0 再跑 step=0：`r = 0/1 = 0` 仍收缩——真正全开发生在 step≥1，所以配置写 0 等于＂几乎不渐进＂。

**预期结果**：三联图呈现 ＂一点 → 窄带 → 完整球带＂的张开过程；对照实验验证 `elevation_range` 直接决定 \(z\) 的取值区间。若环境不可用，标注**待本地验证**。

#### 4.5.5 小练习与答案

1. **问**：`progressive_until=200`、`eval_elevation_deg=0`、配置范围 \([-10, 45]\) 时，第 100 步的实际仰角范围是多少？
   **答**：\(r = 100/201 \approx 0.4975\)，范围 \(= [0.4975\times(-10),\ 0.4975\times 45] \approx [-4.98,\ 22.4]\)。
2. **问**：val 与 test 的视角环差别在哪？
   **答**：val 用 `linspace(0, 360, n+1)[:n]` 剔除与首视角重合的端点；test 用 `linspace(0, 360, n)` 全点。两者仰角/距离/fovy 固定为 eval 值，灯光跟随相机。
3. **问**：为什么 distance 与 fovy 的渐进插值被注释停用，而仰角/方位角保留？
   **答**：与 4.1 一致——Zero123 条件相机要求 distance/fovy 恒定；＂课程式张开＂只需要作用在视角（看哪里），不应作用在取景（多近、多广）。

## 5. 综合实践

**任务：制作一张 ＂随机相机采样器行为对照图＂，用一张图讲清三个旋钮。**

把 4.3.4 与 4.5.4 合并成一个脚本 `vis_camera_suite.py`（示例代码），输出一张 2×2 的图：

1. **面板 A（基准）**：coarse 配置原样参数（`elevation_range=(-10,45)`、`distance=3.8` 固定、`fovy=20` 固定、perturb 全 0、`progressive_until=0`），8 个 batch 共 64 点，按方位角着色。
2. **面板 B（放大仰角）**：`elevation_range=(-10, 90)`，其余同 A——观察点云向上＂封顶＂到 \(z=3.8\)。
3. **面板 C（渐进过程）**：`progressive_until=200`，分别取 step 0/100/250 三个小图叠放或取 250 一幅并注明前两态——观察 ＂单点 → 窄带 → 全开＂。
4. **面板 D（打开扰动）**：`camera_perturb=0.1, center_perturb=0.2, up_perturb=0.02`（即上游 threestudio 默认值）——观察点云从 ＂光滑球带＂ 变成带厚度的 ＂毛球＂，并思考：这种厚度正是 DreamCraft3D 要关掉它的原因（采样位姿与 Zero123 条件位姿必须一致）。

同时在终端打印自检三项：

```python
assert torch.allclose(batch["c2w"][:, :3, 3], batch["camera_positions"])   # c2w 平移=相机位置
assert torch.allclose(batch["rays_d"].norm(dim=-1), torch.ones_like(...), atol=1e-5)  # 射线单位化
assert abs(0.5 * H / math.tan(fovy_rad / 2) - f_from_proj) < 1e-3          # fovy↔焦距互洽
```

**验收标准**：能指着图说出每个面板对应哪个配置字段；三个 assert 全部通过（`rays_d` 归一化的 atol 若因 float32 精度略超 1e-5，可放宽到 1e-4 并说明原因）。本实践全程 CPU 可跑，无需下载任何权重。

## 6. 本讲小结

- 随机相机由 [threestudio/data/uncond.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/uncond.py) 的三个类构成：训练用无限采样流 `RandomCameraIterableDataset`、验证/测试用确定性视角环 `RandomCameraDataset`、注册入口 `RandomCameraDataModule`（`random-camera-datamodule`）。
- 采样链路：仰角（双策略）/方位角（可选批内分桶）/距离/fovy 各自独立采样 → 球坐标转直角坐标 → 正交化出 `right/up/−lookat` 组装 `c2w` → 灯光采样 → 产出同时含 `rays_o/rays_d`（体渲染）与 `mvp_mtx`（光栅化）的 batch。
- DreamCraft3D 粗阶段把 distance、fovy 钉死并把三类扰动关零，都是为了满足 Zero123 ＂渲染相机 ≡ 条件相机＂ 的前提；batch 里返回的 `elevation/azimuth` 是**度数**，Zero123 用相对位姿 `deg2rad` 后编码成条件。
- [threestudio/utils/ops.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/ops.py) 四件套：`get_ray_directions`（像素→相机系，y 翻转、z=−1）、`get_rays`（旋转平移到世界系并归一化）、`get_projection_matrix`（OpenGL 透视 + nvdiffrast y 翻转）、`get_mvp_matrix`（proj @ w2c）。
- `progressive_until` 让视角范围从参考相机（仰角=eval_elevation_deg、方位角=0）线性张开到全配置范围，是 ＂先学参考视角、再环视全球面＂ 的课程式调度；分辨率爬坡与它共用 `update_step` 钩子，且 DataLoader 必须 `num_workers=0` 才能生效。

## 7. 下一步学习建议

- **下一讲（u4-l2）**：`single-image-datamodule`（[threestudio/data/image.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py)）如何把本讲的随机相机嵌入参考图 batch（`batch["random_camera"]` 的来源），以及参考视角的 `mvp_mtx_ref/c2w_ref` 如何参与深度/法向监督。
- **向前衔接（u5-l2）**：本讲产出的 `rays_o/rays_d` 如何被 `nerf-volume-renderer` 消费做 ray marching；`mvp_mtx` 则等 u5-l4 的 nvdiff 光栅化器和 u2-l4 的网格导出器回收。
- **建议动手**：把综合实践的脚本留好，u4-l2 学完后把参考视角相机与随机相机画进同一张图，验证 ＂progressive 起点 = 参考视角＂ 这一设计。
