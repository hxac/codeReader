# u5-l5 材质、背景与 PatchRenderer

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `no-material`（无材质）的工作方式：它为什么把「出颜色」的责任完全交给几何网络的特征头，以及 DreamCraft3D 四个阶段为什么全都选它。
2. 对比 `diffuse-with-point-light-material`（点光漫反射材质）的着色逻辑，说清楚 texture 阶段弃用它与 BSD 自举得分蒸馏目标之间的一致性关系。
3. 区分 `solid-color-background` 与 `textured-background` 两种背景的实现差异，并追踪背景颜色如何进入渲染合成与参考图损失（loss）。
4. 读懂 `patch-renderer` 的「全局低分辨率 + 局部高分辨率 patch」分块渲染思路，并说明 DreamCraft3D 的 texture 阶段为什么没有启用它、而是直接渲染 1024×1024。

## 2. 前置知识

### 2.1 材质与背景在一条光线里各管一段

一个可微三维渲染器输出的每个像素，本质上都遵循同一个「over 合成」公式：

\[ C_{\text{pixel}} = C_{\text{fg}} + (1 - \alpha) \cdot C_{\text{bg}} \]

- \( C_{\text{fg}} \)：前景颜色——光线打到物体表面（体渲染中则是沿途累积）得到的颜色，由**材质（material）**负责；
- \( \alpha \)：不透明度——物体遮住这条光线的程度（体渲染里是透射率加权累积，光栅化里是可见性 mask 的抗锯齿版本）；
- \( C_{\text{bg}} \)：背景颜色——光线没被挡住的部分露出的底色，由**背景（background）**负责。

在 threestudio 的插件化架构里，材质和背景是两个独立的注册组件（`material_type` / `background_type`），由渲染器在 `forward` 里分别调用再合成。渲染器通过一个 namedtuple 持有三者引用而不重复挂载进模块树（见 [threestudio/models/renderers/base.py:L22-L48](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/base.py#L22-L48)，`SubModules` 定义在 L28-L35）。

### 2.2 albedo（反照率）与带光照的颜色

- **albedo**：材质「本身的颜色」，与光照无关。导出三维资产时贴在网格上的纹理就是 albedo。
- **shaded color（带阴影的颜色）**：albedo 乘上光照项（环境光 + 漫反射等）后的结果，同一表面在不同光照下不同。
- **BSD 蒸馏对像素敏感**：texture 阶段的 `stable-diffusion-bsd-guidance` 拿**最终合成渲染图**去训练扩散模型、再反传梯度（system 在 [threestudio/systems/dreamcraft3d.py:L196-L203](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L196-L203) 把 `out["comp_rgb"]` 喂给 guidance）。如果渲染颜色里混入了随机变化的光照，等于给扩散模型的训练目标注入了噪声——这是本讲反复出现的主线。

### 2.3 承接上一讲

上一讲（u5-l4）我们读完了 DMTet 几何与 nvdiff-rasterizer 光栅化器，知道渲染器只在**可见表面点**查询外观网络。本讲补齐渲染管线的最后两块拼图：表面点查到的「特征」如何变成颜色（材质），以及没被表面挡住的像素填什么（背景）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [threestudio/models/materials/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/materials/base.py) | `BaseMaterial` 基类：定义材质契约（`forward` + `export`）与 `requires_normal` / `requires_tangent` 能力开关 |
| [threestudio/models/materials/no_material.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/materials/no_material.py) | `no-material`：特征直接过激活函数变 RGB，DreamCraft3D 四阶段全用它 |
| [threestudio/models/materials/diffuse_with_point_light_material.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/materials/diffuse_with_point_light_material.py) | `diffuse-with-point-light-material`：带环境光/点光源着色的对照组，本仓库未被任何配置使用 |
| [threestudio/models/background/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/background/base.py) | `BaseBackground` 基类：输入射线方向，输出背景颜色 |
| [threestudio/models/background/solid_color_background.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/background/solid_color_background.py) | `solid-color-background`：单一颜色背景（可学习/可随机增强） |
| [threestudio/models/background/textured_background.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/background/textured_background.py) | `textured-background`：可学习球面环境贴图背景 |
| [threestudio/models/renderers/patch_renderer.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/patch_renderer.py) | `patch-renderer`：包装任意体渲染器的分块高分辨率渲染器（上游 threestudio 通用组件，四份 DreamCraft3D 配置均未启用） |
| threestudio/models/renderers/nvdiff_rasterizer.py、nerf_volume_renderer.py | 两个「消费者」：展示材质/背景被调用的现场 |
| threestudio/systems/dreamcraft3d.py | 参考 RGB loss 中背景颜色的合成现场 |
| configs/dreamcraft3d-*.yaml | 四个阶段的 material/background/renderer 选择 |

## 4. 核心概念与源码讲解

### 4.1 no-material：让几何特征直接变成颜色

#### 4.1.1 概念说明

`no-material` 的名字容易误导——它不是「没有材质」，而是「材质层不做任何着色计算」。回顾 u5-l1 讲过的 `implicit-volume` 双头结构：密度头管形状，特征头管外观。`no-material` 做的全部事情，就是把几何网络输出的特征（最后一维恰为 3）过一个激活函数（默认 `sigmoid`，压到 (0,1) 区间）当作 RGB。

为什么要这样设计？

1. **省掉一份网络**：颜色直接由几何特征头产生，材质层零参数（在 DreamCraft3D 的配置下），外观学习全部集中在几何模块的 `feature_network`。
2. **输出是 albedo 型颜色**：不含任何光照项，同一表面颜色恒定——这正是「渲染图 ↔ 参考图」逐像素监督（`lambda_rgb`）想要的；带光照反而会让不同步的同视角颜色不一致。
3. **与导出对齐**：`export()` 返回的 `albedo` 就是同一个前向输出（clamp 到 [0,1]），训练时被监督的像素和最终贴到网格上的纹理是同一份量，不存在「训练优化 A、导出给你 B」的错位。

#### 4.1.2 核心流程

`NoMaterial` 有两种模式，由配置决定：

```text
configure():
    若 input_feature_dims 与 mlp_network_config 都给出:
        建 MLP（use_network = True）   # 特征维 ≠ 3 时需要升维/映射
    否则:
        use_network = False           # 纯激活函数直通

forward(features, **kwargs):
    use_network == False:
        断言 features 最后一维 == n_output_dims（=3）
        color = sigmoid(features)
    use_network == True:
        color = MLP(features) 再过激活
    return color
```

DreamCraft3D 四份配置的 `material` 段要么为空、要么只写 `n_output_dims: 3` / `requires_normal: true`，从不给 `mlp_network_config`，所以**始终走直通分支**。这能成立的前提是几何端的特征维度恰好是 3：DMTet 几何的默认 `n_feature_dims: int = 3`（[threestudio/models/geometry/tetrahedra_sdf_grid.py:L35](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L35)）。

另一个容易被忽略的细节是 `requires_normal` 开关。基类 `BaseMaterial` 用两个类属性声明材质「需要什么输入」（[threestudio/models/materials/base.py:L19-L20](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/materials/base.py#L19-L20)）。`no-material` 本身不用法向，但它的 `requires_normal` 配置项被渲染器「借用」：nerf 渲染器据此决定是否让几何网络计算法向（供法向相关 loss 消费）：

- coarse-nerf 配置写 `material: requires_normal: true`（[configs/dreamcraft3d-coarse-nerf.yaml:L75-L77](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L75-L77)），coarse-neus 同（[configs/dreamcraft3d-coarse-neus.yaml:L62-L64](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L62-L64)）——因为粗阶段有 `lambda_normal_smooth`、几何阶段有法向渲染等需求；
- geometry/texture 配置只写 `n_output_dims: 3`，`requires_normal` 取默认 False（[configs/dreamcraft3d-texture.yaml:L61-L63](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L61-L63)、[configs/dreamcraft3d-geometry.yaml:L52-L54](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L52-L54)）——此时网格法向由三角面叉积给出（u5-l4），无需隐式场再算一次。

#### 4.1.3 源码精读

注册与配置：注册名 `no-material`，`color_activation` 默认 `sigmoid`，可选挂一个 MLP：

- [threestudio/models/materials/no_material.py:L15-L23](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/materials/no_material.py#L15-L23) — `@threestudio.register("no-material")` 登记；Config 定义 `n_output_dims=3`、`color_activation="sigmoid"`、可选 `input_feature_dims` / `mlp_network_config`。

configure 按需建网络，并透传 `requires_normal`：

- [threestudio/models/materials/no_material.py:L27-L39](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/materials/no_material.py#L27-L39) — 只有两个可选配置同时存在才调用 `get_mlp` 建 `self.network`；否则 `use_network=False`，材质零参数。`self.requires_normal = self.cfg.requires_normal` 把能力开关暴露给渲染器。

forward 的两条分支：

- [threestudio/models/materials/no_material.py:L41-L54](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/materials/no_material.py#L41-L54) — 直通分支先断言特征维度等于 3（配置写错维度会在构造/首帧立即报错而非默默广播），再 `get_activation(self.cfg.color_activation)(features)`；注意签名是 `forward(self, features, **kwargs)`——渲染器传入的 `viewdirs`、`positions`、`light_positions`、`shading_normal` 全被吞掉，这正是「着色无关」的代码体现。

export 直接复用 forward：

- [threestudio/models/materials/no_material.py:L56-L63](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/materials/no_material.py#L56-L63) — `color = self(features).clamp(0, 1)`，返回 `{"albedo": color[..., :3]}`。u2-l4 讲过的 mesh-exporter 烘焙纹理时查询的就是这个接口，所以「训练看到的颜色」与「导出贴图的颜色」同源。

两个消费现场（呼应 u5-l2 / u5-l4）：

- 体渲染路径：nerf 渲染器对**每个采样点**调用材质，背景用射线方向查询——[threestudio/models/renderers/nerf_volume_renderer.py:L281-L292](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L281-L292)，其中 `output_normal=self.material.requires_normal` 就是上面说的借用；随后 over 合成 `comp_rgb = comp_rgb_fg + bg_color * (1.0 - opacity)`（[L343-L356](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L343-L356)），并把 `comp_rgb_bg` 一并放进输出字典（[L358-L365](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L358-L365)）。
- 光栅化路径：nvdiff 渲染器只在 `selector = mask[..., 0]` 选出的**可见表面点**上查询几何与材质，再与背景 lerp——[threestudio/models/renderers/nvdiff_rasterizer.py:L148-L186](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nvdiff_rasterizer.py#L148-L186)：L159-L160 取可见点坐标查几何得 `geo_out`（含 `features`）；L163-L164 若 `material.requires_normal` 则附上插值出的 `shading_normal`；L172-L178 以 `**geo_out` 与额外信息调用 `self.material(...)`；L182 `gb_rgb_bg = self.background(dirs=gb_viewdirs)`；L183 `torch.lerp(gb_rgb_bg, gb_rgb_fg, mask.float())` 完成 over 合成。这条「只在表面点查 MLP」的路径正是 texture 阶段能直接渲染 1024 的底气（4.4 节再展开）。

#### 4.1.4 代码实践

实践目标：脱离完整训练，直接实例化两种材质并对比前向输出，直观看到「直通 albedo」与「着色后颜色」的差别。

操作步骤（示例代码，保存为仓库外任意脚本如 `mat_probe.py`，需在已装好 threestudio 依赖、能 `import threestudio` 的环境中运行；计算本身很轻，有无 GPU 均可）：

```python
# 示例代码：材质前向对比
import torch, threestudio

torch.manual_seed(0)
N = 4  # 模拟 4 个表面采样点

# 1) no-material：DreamCraft3D 的选择（空 dict = 全默认配置）
mat_a = threestudio.find("no-material")({})
feats = torch.rand(N, 3) * 4 - 2          # 模拟几何特征头的原始输出（未经激活）
print("no-material:", mat_a(feats))        # sigmoid(feats)，逐点独立、与视角/光照无关

# 2) diffuse-with-point-light-material：对照组
mat_b = threestudio.find("diffuse-with-point-light-material")({})
positions      = torch.tensor([[0., 0., 0.]] * N)   # 表面点
shading_normal = torch.tensor([[0., 0., 1.]] * N)   # 法向朝 +z
light_pos      = torch.tensor([[0.2, 0., 2.]])      # 点光源在斜上方
for mode in ["albedo", "textureless", "diffuse"]:
    out = mat_b(feats, positions=positions, shading_normal=shading_normal,
                light_positions=light_pos, shading=mode)
    print(f"diffuse[{mode}]:", out)
```

需要观察的现象：

1. `no-material` 输出全部落在 (0,1)，且 `sigmoid(x)` 单调对应输入——同一点的颜色只由特征决定。
2. `diffuse` 模式的输出 = albedo × (环境光 + 漫反射项)，同样输入下数值整体被压暗/调亮；`textureless` 模式与 albedo 无关（只剩光照项）；`albedo` 模式则退化为与 no-material 类似的量。
3. 把 `light_pos` 改到 `[-2, 0, 0.2]`（光源在背面）再跑 `diffuse`：漫反射项 clamp 到 0，输出只剩环境光分量 `0.1 * albedo`——颜色随光源剧烈变化。

预期结果：三种 shading 模式输出差异明显，而 no-material 对光源位置毫无反应。若报 `Expected 3 output dims` 断言错误，说明你把特征维度传错（直通分支要求最后一维恰为 3）。实际数值随环境版本可能略有不同，具体打印值「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `implicit-volume` 的 `n_feature_dims` 改成 6（例如想给外观更多容量），保持 `no-material` 直通分支会发生什么？
**答案**：forward 里的断言 `features.shape[-1] == n_output_dims` 失败（6 ≠ 3），直接抛异常。解决办法是给 material 配置同时提供 `input_feature_dims: 6` 与一个 `mlp_network_config`，切换到 `use_network=True` 分支，由 MLP 把 6 维映射回 3 维 RGB。

**练习 2**：`no-material` 的 `export` 和 `forward` 是什么关系？为什么说这是它的优点？
**答案**：`export` 直接调用 `self(features)` 后 clamp，返回 `{"albedo": ...}`。训练损失监督的与导出贴图烘焙的是同一个函数的输出，不存在「训练优化的量和导出量不一致」的偏差，纹理保真目标天然对齐。

**练习 3**：coarse-nerf 配置里 `material.requires_normal: true`，但 no-material 的 forward 根本不用法向，这个配置项给谁看？
**答案**：给渲染器看。nerf-volume-renderer 用 `self.material.requires_normal` 决定调用几何时是否 `output_normal=True`（[threestudio/models/renderers/nerf_volume_renderer.py:L281-L284](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L281-L284)），供 coarse 阶段的法向平滑等损失消费；nvdiff 路径则据此决定是否传 `shading_normal`（[nvdiff_rasterizer.py:L163-L164](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nvdiff_rasterizer.py#L163-L164)）。

### 4.2 对照组：diffuse-with-point-light-material 为什么没被选中

#### 4.2.1 概念说明

`diffuse-with-point-light-material` 是 threestudio 上游的经典材质：albedo ×（环境光 + 漫反射）。它展示了「材质层真正做着色」时该有的样子，也解释了 DreamCraft3D 为什么在**所有**阶段（不只是 texture 阶段）都绕开它。核心动机有三条：

1. **与 BSD 蒸馏目标保持一致**（题目提示的重点）。BSD 的做法（u1-l1、u7-l5 详述）是拿当前渲染图去 DreamBooth 式微调一个专属扩散模型、再用它蒸馏回场景，渲染图是扩散模型的**训练数据**。若渲染图携带点光源着色，微调出的个性化先验就会把这套光照「背」进模型，再蒸馏回来时把光照烙进纹理——而导出的资产里并没有这盏灯，纹理被污染。
2. **参考图逐像素监督要求同视角颜色稳定**。该材质在训练时按概率随机切换 albedo / textureless / diffuse 三种 shading（同一表面、同一步内颜色都可能不同），与参考图的 RGB 回归目标冲突。
3. **法向依赖引入耦合**。它强制 `requires_normal=True`，着色质量绑死在法向精度上；粗阶段法向本来就噪声大。

#### 4.2.2 核心流程

着色公式（Lambert 漫反射 + 环境光）：

\[ C = \text{albedo} \cdot \big( \underbrace{c_{\text{amb}}}_{\text{环境光}} + \underbrace{\max(0,\ \mathbf{n} \cdot \mathbf{l}) \cdot c_{\text{diff}}}_{\text{漫反射}} \big) \]

其中 \(\mathbf{n}\) 是着色法向，\(\mathbf{l}\) 是指向点光源的单位向量。训练期它还做两种数据增强：

```text
每一步（shading 参数未显式指定时）:
    若 global_step < ambient_only_steps: 强制 shading = "albedo"   # 先只学反照率
    否则按概率:
        P(1 - diffuse_prob)            → "albedo"       # 纯反照率
        P(diffuse_prob × textureless_prob) → "textureless"  # 纯光照（无纹理）
        其余                              → "diffuse"      # 完整着色
```

#### 4.2.3 源码精读

- [threestudio/models/materials/diffuse_with_point_light_material.py:L14-L24](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/materials/diffuse_with_point_light_material.py#L14-L24) — 注册与 Config：环境光/漫反射光颜色、`ambient_only_steps`（前期只学 albedo 的步数）、`diffuse_prob` / `textureless_prob`（增强概率）、`soft_shading`（随机环境比例）。
- [threestudio/models/materials/diffuse_with_point_light_material.py:L28-L41](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/materials/diffuse_with_point_light_material.py#L28-L41) — `configure` 把两种光色注册为 buffer（不参与训练），并声明 `requires_normal = True`（写死，法向是必需输入）。
- [threestudio/models/materials/diffuse_with_point_light_material.py:L43-L82](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/materials/diffuse_with_point_light_material.py#L43-L82) — forward 前半段：L53 `albedo = sigmoid(features[..., :3])`（注意取前 3 维，特征可大于 3）；L55-L72 决定环境/漫反射光强（显式 `ambient_ratio` > `soft_shading` 随机 > 固定值三档）；L74-L82 按上式算 `textureless_color` 并乘上 clamp 后的 albedo。
- [threestudio/models/materials/diffuse_with_point_light_material.py:L84-L108](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/materials/diffuse_with_point_light_material.py#L84-L108) — shading 模式选择：训练时以 `random()` 按概率切换三种模式（且整个 batch 用同一种），非训练默认 diffuse；L101-L106 三种返回分支里都有 `+ xxx * 0` 这类写法——注释标明是为了让 DDP 不把「本步未用到的参数」判成冗余。
- [threestudio/models/materials/diffuse_with_point_light_material.py:L110-L114](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/materials/diffuse_with_point_light_material.py#L110-L114) — `update_step`：前 `ambient_only_steps` 步 `ambient_only=True`，训练强制走 albedo——这是 u3-l2 讲过的 Updateable 钩子的典型应用。
- [threestudio/models/materials/diffuse_with_point_light_material.py:L116-L120](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/materials/diffuse_with_point_light_material.py#L116-L120) — `export` 只导出 albedo：印证「着色是训练期的临时手段，资产最终只要反照率」。

全仓库检索可确认：`diffuse-with-point-light-material` 没有出现在 `configs/` 的任何一份 yaml 中，是上游 threestudio 留下的可用组件。

#### 4.2.4 代码实践

实践目标：用 4.1.4 的脚本体验「随机着色增强」的不稳定性，理解它对逐像素监督的干扰。

操作步骤（示例代码，接续 4.1.4 的脚本）：

```python
# 示例代码：随机 shading 增强 vs no-material 的稳定性
mat_b.eval()  # 先验证 eval 模式固定输出 diffuse
f = torch.rand(1, 3)
torch.manual_seed(42)
eval_out = mat_b(f, positions=positions[:1], shading_normal=shading_normal[:1],
                 light_positions=light_pos)          # eval 默认 shading="diffuse"
mat_b.train()
torch.manual_seed(42)
outs = [mat_b(f, positions=positions[:1], shading_normal=shading_normal[:1],
              light_positions=light_pos).item() for _ in range(12)]
print("eval 一次:", eval_out.item())
print("train 连续 12 次同一输入:", [round(o, 3) for o in outs])
```

需要观察的现象：eval 模式输出恒定；train 模式下**同一输入**的 12 次前向输出在若干组明显不同的数值之间跳变（对应 albedo / textureless / diffuse 三种模式被随机抽中）。

预期结果：train 模式输出至少出现两簇差异明显的值。再对比 4.1.4 里 no-material 的输出——同输入永远同输出。结论：任何「以渲染图为监督/训练数据」的损失（参考图 RGB 回归、BSD 微调）都更偏爱后者。具体数值「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么该材质的 `export` 只导出 albedo，而不是导出带光照的渲染颜色？
**答案**：光照是训练期的合成条件（light_positions 是渲染器传入的虚拟光源），不属于资产本身；导出的 obj 要在任意用户光照环境下使用，只有反照率是资产固有属性。

**练习 2**：`ambient_only_steps` 的设计动机是什么？
**答案**：训练早期法向噪声大，漫反射项 \(\max(0, \mathbf{n}\cdot\mathbf{l})\) 会把法向误差放大并污染 albedo 学习；先强制若干步只学 albedo（纯环境光），等几何稳定后再引入着色增强，是一种课程式策略。

**练习 3**：如果把 texture 阶段的 material 换成 diffuse-with-point-light-material（假设配置可跑），BSD 流程会出什么问题？
**答案**：BSD 用渲染图微调 LoRA（`guidance.train_unet_lora`），着色随光源与 shading 抽样变化会让同一视角的「训练样本」目标不稳定，LoRA 先验把虚拟点光的光影烙进纹理；蒸馏回来后再经参考图 loss 拉扯，纹理会出现 baked shading 与漂移。no-material 保证蒸馏目标（渲染像素）与资产纹理（albedo）同源且稳定。

### 4.3 背景族：solid-color 与 textured 的差异及其在 loss 中的分工

#### 4.3.1 概念说明

背景回答的问题是：「这条光线没被物体挡住时，像素是什么颜色？」两种实现代表两个极端：

- **solid-color-background**：单一颜色填满全图，可选可学习、可选随机颜色增强。默认白色、不可学习。
- **textured-background**：一张可学习的 64×64 球面环境贴图，按射线方向采样，不同方向不同颜色，可表达「天有蓝天、地有地面」的环境感。

DreamCraft3D 四份配置**全部使用 solid-color-background 且不传任何参数**（无 `background:` 段），即固定白色背景。原因与材质一脉相承：参考图经过 carvekit 抠图（u2-l1），背景本来就是「无信息」的；蒸馏先验（DeepFloyd/SD）也不需要花容量去学一个背景。背景在这里的唯一职责是给合成图像一个确定的底色。

背景与材质在 loss 中的分工（本讲目标 3）：

- **材质决定前景颜色**，被 `loss_ref_rgb` 逐像素监督（在物体 mask 内）；
- **背景只出现在合成式里**，并通过一个关键技巧被「无损」地接入参考监督：GT 图的背景像素不拿真实值，而是拿**渲染出的背景**来填（见 4.3.3 第三段），从而保证 RGB loss 从不在背景像素上惩罚模型——背景换成什么颜色、可不可学习，都不破坏参考图监督。

#### 4.3.2 核心流程

solid-color 的 forward：

```text
color = env_color 广播到 (B, H, W, 3)
训练期若 random_aug 且以 random_aug_prob 概率命中:
    color ← 随机颜色 (每 batch 一个)
return color
```

textured 的 forward（球面方向 → UV → 双线性采样）：

```text
dirs (…, 3) 单位方向向量
u = atan2( sqrt(x²+y²), z ) / π          ∈ [0, 1]   # 极角（z 轴为极轴）
v = atan2( y, x ) / (2π) + 0.5           ∈ [0, 1]   # 方位角
uv 归一化到 [-1, 1] → F.grid_sample 从 64×64 贴图双线性采样 → sigmoid
```

两个背景的输出都会被渲染器放进 `comp_rgb_bg`，并参与 over 合成（体渲染 [nerf_volume_renderer.py:L356](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L356)；光栅化 [nvdiff_rasterizer.py:L182-L183](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nvdiff_rasterizer.py#L182-L183)）。

#### 4.3.3 源码精读

基类契约：背景的输入是射线方向、输出是逐方向颜色——[threestudio/models/background/base.py:L13-L24](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/background/base.py#L13-L24)。

solid-color：

- [threestudio/models/background/solid_color_background.py:L13-L21](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/background/solid_color_background.py#L13-L21) — Config：`color=(1,1,1)`、`learned=False`、`random_aug=False`、`random_aug_prob=0.5`。
- [threestudio/models/background/solid_color_background.py:L25-L34](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/background/solid_color_background.py#L25-L34) — `learned=True` 时 `env_color` 是 `nn.Parameter`（可训练），否则注册为 buffer（冻结）。DreamCraft3D 走 buffer 路径，白色恒定。
- [threestudio/models/background/solid_color_background.py:L36-L51](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/background/solid_color_background.py#L36-L51) — `torch.ones(...) * env_color` 广播成图；随机增强分支里 `color * 0 +` 的写法同样是防 DDP 冗余参数检查（与 4.2 同款技巧）。

textured：

- [threestudio/models/background/textured_background.py:L13-L27](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/background/textured_background.py#L13-L27) — Config 含贴图高宽（64×64）；`configure` 把贴图本身设为 `nn.Parameter`——一个背景就是 64×64×3 个可训练参数。
- [threestudio/models/background/textured_background.py:L29-L35](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/background/textured_background.py#L29-L35) — `spherical_xyz_to_uv`：方向 → 球面 UV 的手写映射（极角 + 方位角）。
- [threestudio/models/background/textured_background.py:L37-L54](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/background/textured_background.py#L37-L54) — UV 缩放到 [-1,1] 后 `F.grid_sample` 双线性采样（reflection padding），再 sigmoid。整个背景随训练逐步长出图案。

参考 loss 中背景的「合成技巧」（分工的现场）：

- [threestudio/systems/dreamcraft3d.py:L127-L143](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L127-L143) — `loss_ref_rgb`。L131-L133：`gt_rgb = gt_rgb * gt_mask + out["comp_rgb_bg"] * (1 - gt_mask)`——**GT 背景像素被替换成渲染背景**，于是 \(\text{gt} - \text{pred}\) 在纯背景像素上恒等于 0（两侧同为渲染背景），RGB loss 只真正约束物体；L135-L136 coarse/geometry 阶段用 MSE；L138-L141 texture 阶段改 L1 且再套一层 `grow_mask`（把 mask 腐蚀一圈，只在物体「内核」像素上算 loss，容忍边缘误差）。
- [threestudio/systems/dreamcraft3d.py:L146-L147](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L146-L147) — mask loss 用 `out["opacity"]` 与 `gt_mask` 比较，完全不经过背景：形状由 mask loss 管，颜色由（背景免疫的）rgb loss 管，职责清晰。
- 一个关键联动（承接 u3-l3 的 parse_optimizer 结论）：texture 配置的 `optimizer.params` 只点名 `geometry.encoding`、`geometry.feature_network`、`guidance.train_unet`、`guidance.train_unet_lora`（[configs/dreamcraft3d-texture.yaml:L139-L152](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L139-L152)），未点名的模块不会被优化——所以即便换成 textured-background，它的贴图参数在该配置下也不会被训练，除非显式加入 optimizer 键。

#### 4.3.4 代码实践

实践目标：验证「GT 背景用渲染背景回填」的合成逻辑对背景组件是透明的——换背景不破坏参考图监督结构，但会改变背景像素的填充内容。

操作步骤：

1. 在 4.1.4 脚本基础上补一段（示例代码）：

```python
# 示例代码：两种背景对同一组方向的输出
dirs = torch.tensor([[[[0., 0., 1.], [1., 0., 0.]]]])   # (1,1,2,3) 两个方向
bg_solid  = threestudio.find("solid-color-background")({})
bg_text   = threestudio.find("textured-background")({})
print("solid :", bg_solid(dirs))     # 白色 (1,1,1)
print("textured:", bg_text(dirs))    # 随机初始化贴图采样值，逐方向不同

# 手工复现 ref loss 的背景回填（纯 torch，验证逻辑）
gt_rgb   = torch.rand(1, 4, 4, 3)          # 假想参考图
gt_mask  = (torch.rand(1, 4, 4, 1) > 0.5).float()
pred_rgb, comp_rgb_bg = torch.rand(1, 4, 4, 3), torch.rand(1, 4, 4, 3)
gt_filled = gt_rgb * gt_mask + comp_rgb_bg * (1 - gt_mask)
diff_bg = (gt_filled - pred_rgb).square() * (1 - gt_mask)
print("背景像素残差:", diff_bg.sum().item())   # 恒为 0：两侧同为 comp_rgb_bg
```

2. 进阶（需 GPU 与完整训练环境，「待本地验证」）：在 texture 阶段把背景换成可学习环境贴图并短训数百步：

```bash
python launch.py --config configs/dreamcraft3d-texture.yaml --train \
    --gui false --gradio false \
    system.prompt_processor.prompt="a delicious hamburger" \
    system.geometry_convert_from <geometry阶段trial路径> \
    system.background_type=textured-background \
    trainer.max_steps=200
```

需要观察的现象：

1. 脚本第 1 步：solid 输出与方向无关的白色；textured 输出逐方向不同（初始为随机贴图过 sigmoid 的值）。
2. 脚本第 2 步（纯 torch）：`diff_bg` 恒为 0，证明背景像素不产生任何 loss——`gt_rgb*mask + comp_rgb_bg*(1-mask)` 的合成对背景组件完全透明。
3. 进阶训练：渲染图背景区域出现非纯白底色；`train/loss_ref_rgb` 曲线与白底版本量级相当（背景未被监督），但需注意 texture 阶段 rgb loss 使用 grow_mask 腐蚀后的 mask（[dreamcraft3d.py:L138-L141](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L138-L141)），背景影响进一步被排除。

预期结果：mask 合成逻辑在 textured-background 下依然成立（结构上无任何分支依赖背景类型）；区别只在 `comp_rgb_bg` 的内容从常量白色变成视角相关的贴图值，以及——若你把它加进 optimizer——它会开始吸收部分监督信号。训练现象「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `gt_rgb` 的背景像素要用 `out["comp_rgb_bg"]` 回填，而不是直接在 mask 外忽略这些像素（比如把 loss 写成 `(gt_rgb - pred_rgb).square() * gt_mask` 的均值）？
**答案**：功能上两者都屏蔽了背景。回填写法让 loss 张量保持完整图像形状（实现简单、与全图 MSE/L1 接口一致），并且天然把「物体边缘的半透明/抗锯齿像素」纳入监督——mask 取 [0,1] 连续值时，回填式按比例混合 GT 与渲染背景，而硬乘 mask 的写法在这些像素上会引入 GT 背景的未知值。此外 grow_mask 版本仍在 mask 内腐蚀，两种思路可以组合。

**练习 2**：solid-color-background 的 `random_aug` 有什么用途？DreamCraft3D 为什么不开？
**答案**：训练时以一定概率把背景换成随机颜色，可防止外观网络把「白背景」烙进纹理（数据增强，让模型对背景不变）。DreamCraft3D 的参考图是抠图后的白底且 BSD 用同一渲染管线自举，保持背景恒定更利于蒸馏目标稳定，因此四份配置都用默认白色、不开增强。

**练习 3**：换 textured-background 后，若希望它真的被训练起来，除了 `background_type` 还要改什么？
**答案**：在 `system.optimizer.params` 里补一个指向背景参数的键（如 `background.texture: lr: 0.01`），因为 parse_optimizer 只为被点名的模块路径建参数组；不点名则贴图参数无梯度更新（详见 u3-l3）。

### 4.4 PatchRenderer：分块高分辨率渲染思路

#### 4.4.1 概念说明

`patch-renderer` 回答一个通用问题：当目标渲染分辨率很高（如 1024）而显存装不下整图训练时怎么办？它的答案不是「整体降分辨率」，而是：

- 每步渲染一张**全局低分辨率图**（默认下采样 4 倍）——覆盖全部像素，保证全图都有梯度信号、风格全局一致；
- 再随机裁一个**全分辨率 patch**（默认 128×128）渲染——把宝贵的显存集中投放到一小块区域，提供高频细节的学习信号；
- 把 patch 的结果「贴回」上采样后的全局图对应位置，得到一张训练用输出。

必须诚实说明：**`patch-renderer` 没有被 DreamCraft3D 的四份配置使用**（`configs/` 中所有 `renderer_type` 都是 nerf-volume-renderer / neus-volume-renderer / nvdiff-rasterizer）。它是上游 threestudio 的通用组件，本讲把它作为「高分辨率策略」的对照方案精读。DreamCraft3D 的 texture 阶段选择的是另一条路：直接 1024×1024 渲染（[configs/dreamcraft3d-texture.yaml:L8-L10](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L8-L10) 与 [L18-L20](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L18-L20)）。这之所以可行，是因为 texture 阶段的 nvdiff-rasterizer 只在可见表面点查询外观 MLP（u5-l4、4.1.3 节），显存开销与「表面像素数」而非「场景体积采样点数」挂钩；同时 batch_size 降到 1、precision 提到 32。而体渲染路径（nerf/neus）每个像素要采样数百个点，1024 整图训练不现实——patch-renderer 正是为这类渲染器准备的，因此它继承自 `VolumeRenderer` 而非 `Rasterizer`，forward 签名也是 `rays_o/rays_d`。

#### 4.4.2 核心流程

```text
forward(rays_o, rays_d, ...):
    if 训练模式:
        # ① 全局分支
        对 rays_o/rays_d 双线性下采样 (H/4, W/4)
        out_global = base_renderer(下采样后的整图)

        # ② patch 分支
        随机取左上角 (patch_x, patch_y)，裁 128×128 的射线块
        out = base_renderer(patch 射线)

        # ③ 融合
        找出 out 中与 comp_rgb 同形状的"图像级"键
        对每个这样的键:
            out_global[key] 上采样回 (H, W)      # 可选 detach（global_detach）
            把 patch 区域覆盖写回 out_global[key]
        return out_global
    else:
        return base_renderer(完整 rays)          # 推理不分块
```

注意 `global_detach=True` 的含义：全局分支只提供「背景板」，不回传梯度，梯度全部来自 patch——适合显存极紧的场景；默认 False 时全局与 patch 都带梯度。

#### 4.4.3 源码精读

- [threestudio/models/renderers/patch_renderer.py:L14-L22](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/patch_renderer.py#L14-L22) — 注册与 Config：`patch_size=128`、`global_downsample=4`、`global_detach=False`，以及 `base_renderer_type` + `base_renderer`（被包装渲染器的类型与参数）——这是「装饰器式组合」：patch-renderer 本身不做渲染，只做调度。
- [threestudio/models/renderers/patch_renderer.py:L26-L37](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/patch_renderer.py#L26-L37) — `configure` 用 `threestudio.find(self.cfg.base_renderer_type)` 二次查注册表构建真正的渲染器，并把 system 传下来的 geometry/material/background 原样转交——注册机制（u3-l1）的嵌套消费。
- [threestudio/models/renderers/patch_renderer.py:L49-L63](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/patch_renderer.py#L49-L63) — 全局分支：`F.interpolate` 对 `rays_o`/`rays_d`（先 permute 成 NCHW）做双线性下采样后整图渲染。
- [threestudio/models/renderers/patch_renderer.py:L65-L72](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/patch_renderer.py#L65-L72) — patch 分支：`torch.randint` 随机取裁剪原点，切出 128×128 的射线子块渲染。
- [threestudio/models/renderers/patch_renderer.py:L74-L88](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/patch_renderer.py#L74-L88) — 融合逻辑的关键：L74-L79 通过「与 `comp_rgb` 同 ndim 且逐像素形状相同」筛选**图像级**键（如 `comp_rgb`、`comp_normal`、`opacity`），跳过点级键（如 `weights`、`t_dirs`——它们的形状是采样点数，patch 与全局无法直接覆盖）；L81-L83 全局结果上采样回原分辨率；L84-L85 可选 detach；L86-L88 把 patch 像素覆盖写回。这意味着点级正则（orient/sparsity 等）实际来自 patch 分支的输出语义——这是一处需要小心对待的实现细节。
- [threestudio/models/renderers/patch_renderer.py:L97-L106](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/patch_renderer.py#L97-L106) — `update_step` / `train` / `eval` 全部委托给 base_renderer：包装器必须保持被包装者的全部生命周期行为（u3-l2 的钩子协议），否则占用网格更新、训练/推理开关会失效。

#### 4.4.4 代码实践

实践目标：用纯 PyTorch 复现 patch-renderer 的「下采样全局 + 原分辨率 patch + 覆盖回填」融合逻辑，不需要 threestudio 环境即可运行。

操作步骤（示例代码）：

```python
# 示例代码：模拟 patch_renderer 的融合逻辑
import torch, torch.nn.functional as F

H = W = 512; PS, down = 128, 4
torch.manual_seed(0)
out_global = torch.rand(1, H // down, W // down, 3)   # 假想全局低清渲染
out_patch  = torch.rand(1, PS, PS, 3)                 # 假想全分辨率 patch

patch_y, patch_x = 192, 320                            # 随机裁剪原点
merged = F.interpolate(out_global.permute(0, 3, 1, 2), (H, W),
                       mode="bilinear").permute(0, 2, 3, 1)
merged[:, patch_y:patch_y + PS, patch_x:patch_x + PS] = out_patch
print(merged.shape, merged[0, :5, 0, 0].tolist())      # patch 外=上采样值
```

需要观察的现象：`merged` 形状为 `(1, 512, 512, 3)`；patch 区域 (行 192–319、列 320–447) 是 `out_patch` 的原值，patch 之外是低清图上采样后的平滑值——一张「局部清晰、全局模糊」的训练目标图。

预期结果：与 [patch_renderer.py:L80-L88](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/patch_renderer.py#L80-L88) 的行为一致。可再改 `PS`、`down` 感受两个配置对「细节覆盖面积 vs 显存」的权衡。运行结果可直接在本机验证。

进阶（选做，需完整环境）：思考题式验证——若把 patch-renderer 套在 nerf-volume-renderer 外层（`renderer_type: patch-renderer`、`base_renderer_type: nerf-volume-renderer`），coarse 配置的 `loss_orient`/`loss_sparsity` 用的点级键会来自哪个分支？阅读 [patch_renderer.py:L74-L79](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/patch_renderer.py#L74-L79) 与 [dreamcraft3d.py:L252-L274](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L252-L274) 后回答：返回字典里的点级键（`weights`、`normal`、`t_dirs`）来自 patch 分支的输出（`out` 里未被融合逻辑处理的键原样保留在返回值中），正则只在 patch 覆盖的采样点上计算。

#### 4.4.5 小练习与答案

**练习 1**：patch-renderer 为什么继承 `VolumeRenderer` 而不是 `Rasterizer`？它的 forward 签名说明了什么？
**答案**：它的 forward 收发 `rays_o/rays_d`（射线束），这是体渲染器的接口；分块渲染解决的是「体渲染每像素采样数百点、整图训练显存爆炸」的问题。而 nvdiff 光栅化的开销主要在可见表面点，DreamCraft3D 的 texture 阶段直接 1024 渲染即可，无需分块。

**练习 2**：融合时为什么要筛选「与 comp_rgb 同形状的键」？如果对 `weights`（点级键）也做覆盖会发生什么？
**答案**：全局图与 patch 的**图像级**键形状同为 `(B,H,W,C)`，才能上采样后按像素覆盖；而 `weights` 等点级键的第一维是采样点总数，patch 与全局的点数、点序都不同，既无法上采样也无法对位覆盖，强行处理会形状报错或语义错乱。

**练习 3**：`global_detach=True` 与 `False` 各适合什么场景？
**答案**：`False`（默认）时全局分支也回传梯度，全图都有（低频）学习信号，训练更稳；`True` 时梯度只来自 patch，显存更省，适合显存极紧或只想精修局部细节的情况，代价是 patch 之外的区域每步只作为「无梯度的背景板」存在。

## 5. 综合实践

**任务：一张「材质 × 背景」组合决策表，配一份可视化探针脚本。**

1. **写探针脚本**（合并 4.1.4 / 4.2.4 / 4.3.4 的片段为一个 `probe_material_background.py`）：构造 `no-material` 与 `diffuse-with-point-light-material` 两种材质、`solid-color-background` 与 `textured-background` 两种背景（均用 `threestudio.find(注册名)({})`），对同一份随机特征与同一组射线方向各输出一组颜色；用 matplotlib 把 4 组结果按「材质 × 背景」画成 2×2 的对比图（前景色块 + 背景色块拼接即可），并叠加显示 diffuse 材质在 train 模式下连续 8 次前向的输出抖动。
2. **回答三个决策问题**（写成脚本输出后的 Markdown 备注）：
   - texture 阶段为什么选 no-material？（从「渲染图是 BSD 的训练数据」与「参考图逐像素回归」两个角度各给一条理由）
   - 背景如何做到「参与合成但不干扰参考图监督」？（引用 `gt_rgb * gt_mask + comp_rgb_bg * (1 - gt_mask)` 的回填逻辑）
   - 如果显存不足以直接渲染 1024，DreamCraft3D 有哪两条路？（降低 `data.height/width`（README Tips，见 u2-l4/u8-l3）或启用 patch-renderer——并说明后者为何更适合体渲染路径）
3. **可选 GPU 实战**（「待本地验证」）：在 texture 配置上以 `system.background_type=textured-background` 短训 200 步，对比 `train/loss_ref_rgb` 曲线与导出 obj 的纹理是否受背景影响（预期：loss 量级相当、导出纹理不变——背景不进入 mesh-exporter 的烘焙流程，见 u2-l4）。

## 6. 本讲小结

- `no-material` 是「零参数直通材质」：几何特征头输出 3 维特征，`sigmoid` 后即 RGB；DreamCraft3D 四阶段全用它，因为输出是稳定的 albedo 型颜色，且 `export` 与 `forward` 同源，训练监督与导出纹理天然一致。
- `diffuse-with-point-light-material` 展示了真正的着色（albedo ×（环境光 + 点光漫反射））以及随机 shading 增强、`ambient_only_steps` 课程；DreamCraft3D 不用它，根本原因是渲染图要作为 BSD 蒸馏/参考回归的**稳定目标**，不能携带随机光照。
- 背景的两个实现是「常量」与「可学习球面贴图」两个极端；渲染器以 over 合成把背景接进 `comp_rgb`，而参考 RGB loss 用 `comp_rgb_bg` 回填 GT 背景，使背景**参与合成但免疫监督**——换背景不破坏 loss 结构。
- 渲染器经 namedtuple 持有 geometry/material/background 引用（不重复注册进模块树），材质的 `requires_normal` 是渲染器决定「是否让几何算法向」的能力开关。
- `patch-renderer` 用「全局低清 + 随机全分辨率 patch + 覆盖回填」实现分块高分辨率训练，包装任意体渲染器并完整委托其生命周期钩子；DreamCraft3D 未启用它——texture 阶段靠 nvdiff 光栅化「只在可见表面点查询外观」的特性直接渲染 1024×1024。

## 7. 下一步学习建议

本讲补齐了渲染管线的最后两个插件（材质、背景），单元五（3D 表示与可微渲染）到此完整。下一讲进入单元六「训练系统与损失函数」：

- **u6-l1（dreamcraft3d-system 的 configure 与双引导组装）**：看 geometry/material/background/renderer 四件套如何被 `BaseLift3DSystem.configure` 总装成 `dreamcraft3d-system`，以及 guidance/guidance_3d 的挂载——本讲的材质与背景正是在那里被实例化并交给渲染器的。
- 之后再读 **u6-l3 / u6-l4（training_substep 的损失体系）**，本讲 4.3 节的背景回填技巧将在那里展开为完整的 ref 损失族（RGB/mask/depth/normal）。
- 若想深究 BSD 与蒸馏目标一致性的完整推导，跳到 **u7-l4 / u7-l5（stable-diffusion-bsd-guidance）**。
