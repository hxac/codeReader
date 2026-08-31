# u4-l1 render() 渲染主流程

## 1. 本讲目标

学完本讲，你应该能够：

1. 按顺序说出 `render()` 从输入到返回的完整流水线（8 个步骤）。
2. 逐一说出 `GaussianRasterizationSettings` 的 18 个字段各来自 Camera、GaussianModel、`pipe` 还是 `render()` 参数。
3. 解释返回 dict 中 `viewspace_points`（即 means2D）、`visibility_filter`、`radii` 三个张量在训练循环中的具体用途。
4. 说清「有效不透明度」的构成：\(\sigma(\_opacity)\)、衰减因子与 `marginal_t` 三者在默认 CUDA 路径与 Python 回退路径中分别在哪里相乘。

本讲是单元 4 的第一讲：只讲**主路径**。Python 调试回退（`compute_cov3D_python` / `convert_SHs_python` 的完整细节）与 `env_map` 背景留到 u4-l3，CUDA 绑定与 `markVisible` 留到 u4-l2，opacity decay 的模式族留到单元 6。

## 2. 前置知识

本讲直接建立在以下已建立的概念之上（不重复推导，只取结论）：

- **Camera 的四个渲染量**（u2-l3）：`world_view_transform`（W2C 视图矩阵，因 CUDA 按列主序解释而转置存储）、`full_proj_transform`（视图矩阵 × 投影矩阵）、`camera_center`（= −R·T，相机光心）、`timestamp`（帧号归一化到 `time_duration` 域）；以及 `FoVx`/`FoVy` 与 `image_width`/`image_height`。
- **裸值存储 + 读时激活**（u3-l1 / u3-l2）：`_opacity` 经 sigmoid、`_scaling`/`_scaling_t` 经 exp、`_rotation`/`_rotation_r` 经归一化后才成为渲染输入；`_xyz` 与 `_t` 直存直读。4D 高斯在 3D 六属性外多了 `_t`(N,1)、`_scaling_t`(N,1)、`_rotation_r`(N,4，仅 `rot_4d=True`)。
- **时间边缘化**（u3-l3）：渲染时刻 \(\tau\) 下，4D 高斯切片为 3D 椭球，时间衰减因子
  \[ \text{marginal\_t}(i,\tau) = \exp\left(-\frac{(t_i-\tau)^2}{2\sigma_{t,i}}\right) \]
  会乘进不透明度；`> 0.05` 的剔除阈值对应时间半宽约 \(2.45\sqrt{\sigma_t}\)。
- **`torch.autograd.Function`**：PyTorch 自定义算子的标准写法——`forward(ctx, ...)` 里 `ctx.save_for_backward(...)` 暂存反向所需张量，`backward(ctx, ...)` 返回与输入一一对应的梯度元组。

两个新术语：

- **光栅化（rasterization）**：把连续几何（这里是数百万个高斯椭球）投影到离散像素网格、逐像素做 alpha blending 合成图像的过程。4C4D 中它由一个可微 CUDA 核完成。
- **视锥剔除（frustum culling）**：把投影后落在相机视锥之外（或屏幕上覆盖半径为 0）的高斯直接跳过，不参与渲染与梯度更新。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [gaussian_renderer/\_\_init\_\_.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py) | `render()` 的唯一实现，train.py 与 render.py 两个入口共用 |
| [gaussian_renderer/diff_gaussian_rasterization.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/diff_gaussian_rasterization.py) | 仓库内的 JIT 编译版光栅化包装：`GaussianRasterizationSettings` / `GaussianRasterizer` / `_RasterizeGaussians` 定义（未被任何代码 import，见 4.2 的澄清） |
| [diff-gaussian-rasterization/diff_gaussian_rasterization/\_\_init\_\_.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py) | 实际被 `render()` import 的安装版包装，与上一行文件同构（类定义在 L206/L226/L231/L242） |
| [scene/gaussian_model.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py) | `render()` 消费的 `get_*` property、`get_marginal_t`、`opacity_decay` |
| [scene/cameras.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/cameras.py) | settings 中相机侧字段（矩阵、FoV、尺寸、timestamp）的定义处 |
| [train.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py) | `render()` 返回值的消费现场（本讲引用行号说明用途） |
| [diff-gaussian-rasterization/cuda_rasterizer/forward.cu](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu) | CUDA 侧在核内完成 `opacity × marginal_t` 的证据（仅引用，不精读，u8-l1 详讲） |

## 4. 核心概念与源码讲解

### 4.1 render()：从模型属性到渲染 dict 的全景流水线

#### 4.1.1 概念说明

`render()` 是整个项目**唯一的渲染入口**：输入「一台相机 + 一个高斯模型 + 一组开关」，输出「渲染图 + 训练所需的辅助张量」。它本体几乎不做数值计算，而是一个**参数组装器**——把 Camera 的几何量、GaussianModel 激活后的属性、`pipe` 的调试开关打包成一份 `GaussianRasterizationSettings` 加一组输入张量，交给 CUDA 光栅化器，再把结果整理成 dict 交还训练循环。

理解本函数就理解了「一次前向渲染需要什么」：这是阅读 CUDA 侧代码（u8-l1）和调试任何渲染问题（黑图、形状不匹配、显存爆炸）的先修地图。

#### 4.1.2 核心流程

```text
render(viewpoint_camera, pc, pipe, bg_color, ...):
 1. screenspace_points = zeros_like(get_xyz, requires_grad=True)   # means2D 梯度容器
 2. raster_settings = GaussianRasterizationSettings(相机几何 + 模型开关 + pipe.debug)
 3. rasterizer = GaussianRasterizer(raster_settings)
 4. means3D / opacity ← property 读取
    若 args.opacity_decay 且 iteration > decay_from_iter:
        opacity ← pc.opacity_decay(f_min, f_max, mask=可见性)      # 单元 6 详讲
 5. 几何输入二选一：
    默认路径：scales / rotations，4D 再加 scales_t / ts，rot_4d 再加 rotations_r
    回退路径(compute_cov3D_python)：cov3D_precomp + delta_mean，且 opacity *= marginal_t
 6. 颜色输入二选一：
    默认路径：shs = get_features（SH→RGB 由 CUDA 完成）
    回退路径(convert_SHs_python)：Python 算 4D SH → colors_precomp
 7. rendered_image, radii, depth, alpha, flow, covs_com = rasterizer(全部输入)
 8. visibility_filter = radii > 0；返回 6 键 dict
```

关键点：第 5、6 步各有一个「默认 / 回退」二选一开关（都来自 `pipe`，默认全 False），因此**主路径上传给 CUDA 的是裸属性（尺度、旋转、SH 系数），协方差与颜色的具体求值都发生在 CUDA 核内**；Python 端的两个回退开关是逐行同构的调试镜像（u4-l3 详讲）。

#### 4.1.3 源码精读

**(a) 函数签名与 means2D 梯度容器**

[gaussian_renderer/\_\_init\_\_.py:L19-L30](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L19-L30) 定义了 7 个参数：`viewpoint_camera`（渲染视角）、`pc`（GaussianModel）、`pipe`（PipelineParams）、`bg_color`（背景色，必须在 GPU 上）、`scaling_modifier`（全局尺度缩放，默认 1.0）、`override_color`（跳过 SH 直接给定颜色）、`args` 与 `iteration`（这两个**只为 opacity decay 服务**，推理时 `render.py` 不传，`iteration` 取默认 −1 从而永远不触发衰减分支）。

第 26–30 行创建 `screenspace_points` 并 `retain_grad()`：这是一个形状同 `get_xyz` 的全零张量，却带 `requires_grad=True`。它是 3DGS 一脉相承的**梯度容器技巧**——自定义 `autograd.Function` 的中间输出不会自动保留 `.grad`，而把它作为**输入**传入光栅化器后，`backward` 算出的 `grad_means2D`（每个高斯投影中心的屏幕空间梯度）就会累积到它的 `.grad` 上。训练循环正是用它驱动致密化：

[train.py:L148-L150](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L148-L150) 在 `loss.backward()` 之后取 `torch.norm(viewspace_point_tensor.grad[:,:2], dim=-1)`——只取前两列（x/y 屏幕 coords），即「这个高斯在屏幕上挪动能多大程度降低损失」，这是 clone/split 的判定信号（u5-l4）。

**(b) settings 组装与光栅化器实例化**

[gaussian_renderer/\_\_init\_\_.py:L33-L57](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L33-L57) 先由 FoV 算 `tanfovx`/`tanfovy`（视锥锥角的半角正切），再打包 `raster_settings`（字段级拆解见 4.2.3），最后 `GaussianRasterizer(raster_settings=raster_settings)` 包一层 `nn.Module`。注意 `nn.Module` 在这里**不含任何可学习参数**，纯粹为了借用 `__call__` → `forward` 的模块化调用形式。

**(c) opacity decay 接入点（预览）**

[gaussian_renderer/\_\_init\_\_.py:L64-L75](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L64-L75)：当 `args.opacity_decay` 开启且 `iteration > decay_from_iter`（默认 500，延迟启用）时，`time_aware` 模式下用 `rasterizer.markVisible(means3D)`（空间可见）与 `pc.get_marginal_t(...) > 0.05`（时间可见）取交集作为 `visibility`，再调 `pc.opacity_decay(f_min, f_max, mask=visibility)` 得到衰减后的不透明度并**替换**局部变量 `opacity`。本讲只需记住调用点与替换关系，模式族与原位写回细节在 u6-l2 / u6-l3 展开。

**(d) 几何输入：默认走「裸属性」**

[gaussian_renderer/\_\_init\_\_.py:L79-L101](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L79-L101) 先把所有几何输入置 `None`，然后二选一：

- 默认（`pipe.compute_cov3D_python == False`，第 94–101 行）：`scales = pc.get_scaling`、`rotations = pc.get_rotation`；`gaussian_dim == 4` 时再补 `scales_t = pc.get_scaling_t`、`ts = pc.get_t`；`rot_4d` 时再补 `rotations_r = pc.get_rotation_r`。协方差矩阵**不在 Python 端算**，CUDA 侧收到尺度/旋转后自行组装（时间边缘化也在核内完成，见 4.3.3）。
- 回退（第 85–93 行）：Python 端调 `get_current_covariance_and_mean_offset` 得到条件协方差与均值偏移，`means3D = means3D + delta_mean`，并且第 91–93 行**显式**做 `opacity = opacity * marginal_t`——这正是默认路径交给 CUDA 做的那一步乘法。

**(e) 颜色输入与 ts 的兜底**

[gaussian_renderer/\_\_init\_\_.py:L105-L127](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L105-L127)：默认路径 `shs = pc.get_features`（形如 (N, SH 通道数, 3)），SH→RGB 由 CUDA 的 `computeColorFromSH_4D` 完成（u3-l4）；第 124–125 行的 `if pc.gaussian_dim == 4 and ts is None: ts = pc.get_t` 是**兜底**——若走了 `cov3D_precomp` 回退，第 79–101 行不会填 `ts`，但 4D 球谐求值（无论在 CUDA 还是 Python）都需要每个高斯的时间中心，故在此补上。第 129 行 `flow_2d = torch.zeros_like(pc.get_xyz[:,:2])` 是 (N,2) 的全零占位：光流是预留输出端，输入恒为零。

**(f) Python 预筛选（仅回退路径）**

[gaussian_renderer/\_\_init\_\_.py:L132-L157](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L132-L157)：只在 `compute_cov3D_python and gaussian_dim == 4` 时，用 `mask = marginal_t[:,0] > 0.05` 把 10 类张量（means2D/means3D/ts/shs/colors_precomp/opacity/scales/scales_t/rotations/rotations_r/cov3D_precomp/flow_2d）**同步**过滤——漏掉任何一个都会导致行错位。u4-l3 详讲。

**(g) 光栅化调用与返回整理**

[gaussian_renderer/\_\_init\_\_.py:L160-L172](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L160-L172) 一次性解包六个输出：`rendered_image, radii, depth, alpha, flow, covs_com`。随后 [L189-L195](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L189-L195)：若走过预筛选，`radii` 只剩被筛后的短张量，需要 `radii_all = radii.new_zeros(mask.shape); radii_all[mask] = radii` 散回全长度；最终 `visibility_filter = radii_all > 0`——被视锥剔除或屏幕半径为 0 的高斯在此标记为不可见。[L199-L205](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L199-L205) 返回 dict。

返回 dict 各键的形状与含义：

| 键 | 形状 | 含义与消费现场 |
| --- | --- | --- |
| `render` | (3, H, W) | RGB 渲染图；train.py:139 解包后与 gt 算 L1/SSIM |
| `viewspace_points` | (N, 3) | means2D 梯度容器（全零占位）；backward 后 `.grad[:, :2]` 是屏幕空间梯度，train.py:150 取范数做致密化信号 |
| `visibility_filter` | (N,) bool | `radii > 0`；train.py:237 只对可见高斯更新 `max_radii2D`，train.py:241/244 的致密化统计同样只在可见高斯上进行 |
| `radii` | (N,) | 每个高斯在屏幕上的包围半径（像素）；train.py:172–174 在 batch>1 时跨视角取 max |
| `depth` | — | 深度图输出；train.py:140 的解包语句**被注释掉**，训练未消费（形状待确认） |
| `alpha` | — | 每像素累积不透明度（`_RasterizeGaussians.forward` 返回 `1-T`）；在 [L174-L187](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L174-L187) 的 env_map 分支内部用于 `(1-alpha)×背景` 合成 |
| `flow` | — | 光流输出（输入 `flow_2d` 恒为零）；当前训练/推理入口均未消费 |

`means2D` / `visibility_filter` / `radii` 三者的训练用途是本讲学习目标之二，汇总如下（引用均为真实消费行）：

- **means2D**：唯一目的就是「让 PyTorch 把屏幕空间梯度存下来」。没有它，`loss.backward()` 之后无法得知每个高斯在屏幕上应该如何移动，致密化的 clone/split 判据就消失了。
- **visibility_filter**：保证被剔除的高斯不参与统计与更新。[train.py:L236-L238](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L236-L238) 用它更新 `max_radii2D`（剪枝的屏幕尺寸阈值，u5-l4），[train.py:L241-L244](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L241-L244) 的 `add_densification_stats` 也以它为下标。
- **radii**：既是可见性的来源（`> 0`），又在 batch>1 时跨视角取最大值（[train.py:L172-L174](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L172-L174)），供剪枝时用「出现过的最大屏幕尺寸」做阈值。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「总高斯数 ≫ 可见高斯数」以及渲染图的形状。

**操作步骤**（在自己的本地副本上做，观察完还原；本仓库规则下 worker 不改源码，读者实验后建议 `git checkout -- gaussian_renderer/__init__.py` 还原）：

1. 打开 `gaussian_renderer/__init__.py`，在第 198 行（`return` 之前）插入：

```python
    # 示例代码：渲染统计 hook
    if iteration % 1000 == 0:
        print(f"[render] iter={iteration} total={pc.get_xyz.shape[0]} "
              f"visible={int(visibility_filter.sum())} "
              f"image={tuple(rendered_image.shape)}")
```

2. 按 README 的训练命令（如 flame_steek 配置）启动训练，观察每 1000 次迭代打印的一行。
3. 与 TensorBoard 的 `total_points` 曲线对照。

**需要观察的现象**：

- `total` 随训练推进持续增长（致密化在加高斯）；
- `visible` 通常远小于 `total`（视锥剔除 + 时间剔除同时生效：4 台相机一次只看 1 个视角，且每个高斯只在自己的时间半宽内可见）;
- `image` 形如 `(3, H, W)`，与 gt 图一致（例如 `--res 1` 下 (3, 580, 1014)，具体数值取决于数据集与 resolution，待本地验证）。

**预期结果**：可见比例一般在百分之几到几十之间浮动；如果 `visible == total`，说明剔除失效（例如 `gaussian_dim` 被误设为 3 或时间尺度爆炸），应当警惕。GPU 环境缺失时的替代实践见第 5 节综合实践 B。

#### 4.1.5 小练习与答案

**练习 1**：为什么 means2D 要传入一个 `requires_grad=True` 的全零张量，而不是直接传 `None`？

**答案**：`autograd.Function` 只对「作为输入传入的张量」回传梯度。传 `None` 时 `GaussianRasterizer.forward` 会把它归一化成空张量（[gaussian_renderer/diff_gaussian_rasterization.py:L286-L291](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/diff_gaussian_rasterization.py#L286-L291) 附近把 None 换成 `torch.Tensor([])`），`backward` 算出的 `grad_means2D` 没有落点，训练循环将拿不到屏幕空间梯度，致密化失去判据。全零占位张量本身数值无害——它只充当梯度的「收件地址」。

**练习 2**：`visibility_filter` 为什么直接用 `radii > 0`，而不是在 Python 里重新做一遍视锥判断？

**答案**：`radii` 是光栅化过程的真实副产品——高斯投影后若覆盖不到任何像素（在视锥外、或投影退化），CUDA 侧记 0。用它做可见性既精确（与渲染实际发生的事情一致）又零额外开销；Python 端重算则要重复投影逻辑，还可能与 CUDA 的剔除口径不一致。

**练习 3**：`batch_size > 1` 时，多个视角各自的 `visibility_filter`/`radii` 如何合并成一个？

**答案**：[train.py:L172-L174](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L172-L174)：可见性按「任一视角可见即可见」（`torch.stack(...).sum(1) > 0`），`radii` 取各视角最大值（`torch.stack(...).max(1)[0]`）。梯度归一化 `× batch_size / visibility_count` 的推导在 u5-l3。

### 4.2 GaussianRasterizationSettings：18 个字段从哪里来

#### 4.2.1 概念说明

`GaussianRasterizationSettings` 是一个 `NamedTuple`——**不可变**的字段记录。用它而非 dict 有两个原因：字段集合固定、拼写错误在构造时立刻报错；它作为非张量参数整体传入 `autograd.Function`，backward 对它无需求梯度（`_RasterizeGaussians.backward` 返回的梯度元组最后一项就是 `None`，见 [gaussian_renderer/diff_gaussian_rasterization.py:L201-L215](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/diff_gaussian_rasterization.py#L201-L215)）。

**一个必须先澄清的事实（遗留代码识别，延续 u1-l3 / u2-l3 的习惯）**：`render()` 顶部的 `from diff_gaussian_rasterization import GaussianRasterizationSettings, GaussianRasterizer`（[gaussian_renderer/\_\_init\_\_.py:L15](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L15)）解析到的是 **pip 安装的扩展包**（来自 `diff-gaussian-rasterization/` 子包，`from . import _C` 加载编译好的 .so）；而仓库内的 [gaussian_renderer/diff_gaussian_rasterization.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/diff_gaussian_rasterization.py) 是一份用 `torch.utils.cpp_extension.load` 现场 JIT 编译的同构变体（[L19-L28](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/diff_gaussian_rasterization.py#L19-L28)），全仓库 grep 不到任何 import 它的代码——它不被执行，但类定义与安装版一致（安装版的 `GaussianRasterizationSettings` 在 [diff-gaussian-rasterization/diff_gaussian_rasterization/\_\_init\_\_.py:L206](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py#L206)、`markVisible` 在 L231）。本讲按规格引用仓库内文件的行号，两者结论通用。

#### 4.2.2 核心流程

18 个字段按来源分四类，组装顺序即 [gaussian_renderer/\_\_init\_\_.py:L36-L55](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L36-L55) 的书写顺序：

```text
相机侧(8):  image_height, image_width, tanfovx, tanfovy,
            viewmatrix, projmatrix, campos, timestamp
模型侧(6):  sh_degree, sh_degree_t, time_duration,
            rot_4d, gaussian_dim, force_sh_3d
pipe 侧(2): debug, bg(受 env_map_res 影响时退化为 zeros(3))
render 参数(2): scale_modifier, bg(默认取 bg_color)
```

#### 4.2.3 源码精读

定义本体在 [gaussian_renderer/diff_gaussian_rasterization.py:L219-L237](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/diff_gaussian_rasterization.py#L219-L237)。逐字段来源表：

| 字段 | 来源（组装处 L36–L55） | 定义处 | 用途 |
| --- | --- | --- | --- |
| `image_height` | `int(viewpoint_camera.image_height)` | cameras.py:52 | 输出图尺寸、CUDA 分块划分 |
| `image_width` | `int(viewpoint_camera.image_width)` | cameras.py:51 | 同上 |
| `tanfovx` | `math.tan(viewpoint_camera.FoVx * 0.5)` | cameras.py:31 | 视锥剔除的锥角参数 |
| `tanfovy` | `math.tan(viewpoint_camera.FoVy * 0.5)` | cameras.py:32 | 同上（y 方向） |
| `bg` | `bg_color` 参数；`pipe.env_map_res` 非零时强制 `torch.zeros(3)` | render() 调用方 | 背景色（见练习 1） |
| `scale_modifier` | `scaling_modifier`（默认 1.0） | render() 参数 | 全局缩放高斯尺度，3DGS 遗留，一般不动 |
| `viewmatrix` | `viewpoint_camera.world_view_transform` | cameras.py:66 | W2C 视图矩阵（转置存储） |
| `projmatrix` | `viewpoint_camera.full_proj_transform` | cameras.py:71 | 投影矩阵（视图×投影之积） |
| `sh_degree` | `pc.active_sh_degree` | gaussian_model.py | 当前激活的**空间**球谐阶数（渐进进阶，u3-l4） |
| `sh_degree_t` | `pc.active_sh_degree_t` | gaussian_model.py | 当前激活的**时间**球谐阶数 |
| `campos` | `viewpoint_camera.camera_center` | cameras.py:72 | 相机光心，算视线方向（SH 视角相关颜色） |
| `timestamp` | `viewpoint_camera.timestamp` | cameras.py:74 | 渲染时刻 τ（4D 核心的「第四维坐标」） |
| `time_duration` | `pc.time_duration[1] - pc.time_duration[0]` | 模型参数 | 时间归一化域长度，兼作 SH 时间基周期 T |
| `rot_4d` | `pc.rot_4d` | 模型开关 | 是否用双四元数 4D 旋转 |
| `gaussian_dim` | `pc.gaussian_dim` | 模型开关 | 3 或 4，决定 CUDA 内部分支 |
| `force_sh_3d` | `pc.force_sh_3d` | 模型开关 | 4D 模型强制用 3D SH（u3-l4） |
| `prefiltered` | 恒 `False` | render() 硬编码 | 声明输入未做 Python 预筛 |
| `debug` | `pipe.debug` | arguments/__init__.py:73 | CUDA 失败时 dump `snapshot_fw.dump` |

值得注意的三个设计细节：

1. **相机字段占了大半**——渲染是「以相机为中心」的操作，同一模型对不同相机产生不同 settings，这也是为什么 settings 在 `render()` 内部每次调用都重建，而 `GaussianRasterizer` 只是个薄包装（[gaussian_renderer/diff_gaussian_rasterization.py:L239-L242](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/diff_gaussian_rasterization.py#L239-L242)）。
2. **`sh_degree`/`sh_degree_t` 进 settings 而不是进张量**：激活阶数是「本轮用 SH 的前多少个通道」的标量开关，CUDA 据此决定 `computeColorFromSH_4D` 消耗到第几个频率块（u3-l4）。
3. **`time_duration` 传标量差**：CUDA 签名要 float，周期只关心长度；`[0,10]` 配置下就是 10.0。

`GaussianRasterizer.forward` 在调用 CUDA 前做**三重互斥校验**（[gaussian_renderer/diff_gaussian_rasterization.py:L262-L271](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/diff_gaussian_rasterization.py#L262-L271)）：

- `shs` 与 `colors_precomp` **恰给其一**（第 262–263 行）；
- `scales`+`rotations` 与 `cov3D_precomp` **恰给其一**（第 265–266 行）；
- `rot_4d` 且未给 `cov3D_precomp` 时，`rotations_r`、`scales_t`、`ts` **必须齐给**（第 268–271 行）。

这解释了 `render()` 里那些 `None` 的语义：None 表示「这一路输入交由另一路负责」。随后 [L273-L291](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/diff_gaussian_rasterization.py#L273-L291) 把所有 None 归一化为空张量（C++ 侧以空张量区分「未提供」），[L294-L308](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/diff_gaussian_rasterization.py#L294-L308) 经 `rasterize_gaussians` 转入 `_RasterizeGaussians.apply`——从此进入 autograd 世界，细节是 u4-l2 的主题。

#### 4.2.4 代码实践

**实践目标**：把「字段 → 来源」的映射内化成肌肉记忆。

**操作步骤**：

1. 打印（或在纸上抄下）4.2.3 的表格并遮住「来源」列。
2. 逐字段在源码里找赋值行：先看 [gaussian_renderer/\_\_init\_\_.py:L36-L55](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L36-L55) 的组装处，再跳到对应定义处（如 `timestamp` 跳 cameras.py:74）。
3. 自查三问：哪些字段一次训练内**从不变化**（如 `gaussian_dim`、`time_duration`）？哪些**每个视角都变**（相机侧 8 个）？哪些**随迭代变化**（`sh_degree`/`sh_degree_t` 每 1000 步进阶一档，直到 5000 步冻结——u3-l4）？

**需要观察的现象 / 预期结果**：能不假思索地说出「18 个字段里 8 个来自相机、6 个来自模型、其余来自 pipe 与 render 参数」；能解释为什么 settings 必须每次调用重建（相机字段在变）。

（可选的 GPU 实验，待本地验证：在本地副本把第 48 行改成 `timestamp=5.0` 固定时刻，对同一视角的不同帧各渲染一次——预期动态场景的两帧渲染结果几乎相同，因为时间维被冻结；还原代码。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `pipe.env_map_res` 非零时 `bg` 被强制设为 `torch.zeros(3)`（[gaussian_renderer/\_\_init\_\_.py:L41](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L41)）？

**答案**：启用环境贴图时背景由球面贴图提供，渲染端在第 187 行做 `rendered_image + (1 - alpha) * bg_color_from_envmap` 合成。若 `bg` 非零，CUDA 会先把纯色背景混进 `rendered_image`/`alpha`，再叠 env_map 就等于背景被计了两次；置零让 (1−alpha) 通道干净地留给 env_map。

**练习 2**：`prefiltered` 字段恒为 `False` 说明了什么？

**答案**：主路径完全不做 Python 侧预筛，剔除（视锥 + 时间）全部交给 CUDA 核内完成；只有 `compute_cov3D_python` 调试路径才在 Python 里按 `marginal_t > 0.05` 筛（且筛后 `radii` 要散回全长度，见 4.1.3 (g)）。

**练习 3**：`sh_degree` 与 `sh_degree_t` 为什么必须进 settings？

**答案**：CUDA 侧 `computeColorFromSH_4D` 需要知道当前激活到第几阶，才能只消费 SH 系数张量的前若干频率块（低阶先学、高阶后学）。这是 3DGS「渐进球谐」策略在 4D 上的双维扩展（u3-l4 的 `oneupSHdegree`）。

### 4.3 GaussianModel property 与 opacity × marginal_t

#### 4.3.1 概念说明

`render()` 不直接碰 `_opacity`、`_scaling` 这些裸值，一切输入都经 property 读时激活获得——这是 u3-l1「裸值存储 + 读时激活」模式在渲染端的总应用。本模块解决本讲第三个学习目标：**有效不透明度**的构成。渲染时刻 \(\tau\) 下，高斯 \(i\) 实际参与 blending 的不透明度为

\[
\alpha_{\text{eff}}(i, \tau) \;=\; \sigma(\text{\_opacity}_i)\;\times\;\underbrace{f_i}_{\text{decay 因子（若启用）}}\;\times\;\underbrace{\exp\!\left(-\frac{(t_i-\tau)^2}{2\sigma_{t,i}}\right)}_{\text{marginal\_t}}
\]

两个「乘法发生的位置」：

- **默认 CUDA 路径**：Python 只传 \(\sigma(\_opacity)\)（可能已含 decay）＋ `ts` ＋ `scales_t`（+`rotations_r`），marginal_t 的计算与相乘**全部在 CUDA 核内**；
- **Python 回退路径**：`render()` 第 91–93 行**显式**乘 `marginal_t`，并在第 132–133 行用同一公式做预筛。

#### 4.3.2 核心流程

property 激活一览（消费视角）：

| property | 激活 | 形状 | render() 中的去向 |
| --- | --- | --- | --- |
| `get_xyz` | 无（直存） | (N,3) | `means3D`，也是 `screenspace_points` 的形状模板 |
| `get_t` | 无（直存） | (N,1) | `ts`（回退路径由 L124–125 兜底补上） |
| `get_opacity` | sigmoid | (N,1) | `opacity`（可能被 decay 结果替换） |
| `get_scaling` | exp | (N,3) | `scales` |
| `get_scaling_t` | exp | (N,1) | `scales_t` |
| `get_rotation` | 归一化 | (N,4) | `rotations` |
| `get_rotation_r` | 归一化 | (N,4) | `rotations_r`（仅 rot_4d） |
| `get_features` | cat(dc, rest) | (N, C, 3) | `shs` |

时间方差 \(\sigma_t\) 的来源有两条分支（[scene/gaussian_model.py:L244-L250](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L244-L250)）：

```text
get_cov_t():
  rot_4d=True : L = build_scaling_rotation_4d(s_xyzt, q_left, q_right)
                cov_t = (L Lᵀ)[3,3]        # 4D 协方差右下角块 = (时间尺度)²
  rot_4d=False: cov_t = get_scaling_t      # 直接把激活后的时间尺度当方差
marginal_t = exp( -0.5 (t - τ)² / cov_t )
```

这就是 u3-l3 指出的陷阱在渲染端的落点：**同一个 `_scaling_t` 数值在两种模式下 \(\sigma_t\) 相差一个平方**。

#### 4.3.3 源码精读

**(a) property 区**

[scene/gaussian_model.py:L193-L233](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L193-L233)：`get_scaling`(L194)、`get_scaling_t`(L198)、`get_rotation`(L206)、`get_rotation_r`(L210)、`get_xyz`(L214)、`get_t`(L218)、`get_features`(L226)、`get_opacity`(L232)。注意 `get_features` 是拼接 `_features_dc` 与 `_features_rest` 的 (N, C, 3) 张量——C 由 `get_max_sh_channels`(L236–242) 的三分支逻辑决定（u3-l4）。

**(b) marginal_t 的 Python 定义**

[scene/gaussian_model.py:L252-L254](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L252-L254)：`get_marginal_t(timestamp)` 返回 \(\exp(-0.5(t-\tau)^2/\sigma_t)\)。注释掉了归一化系数 \(1/\sqrt{2\pi\sigma_t}\)——渲染只需要相对权重，不影响 blending 结果。它在本讲范围内有两个消费点：opacity decay 的 `time_visibility` 掩码（render L67）与 Python 回退路径的显式相乘（L92–93）。

**(c) CUDA 侧的同构证据**

[diff-gaussian-rasterization/cuda_rasterizer/forward.cu:L331-L348](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L331-L348) 的 `computeCov3D_conditional`：`cov_t = Sigma[3][3]`（L332）、`marginal_t = __expf(-0.5*dt*dt/cov_t)`（L333）、`mask = marginal_t > 0.05`（L334）、**`opacity *= marginal_t`**（L336）、条件协方差 `cov11 - outerProduct(cov12,cov12)/cov_t`（L339）、均值偏移 `cov12/cov_t*dt`（L348）——与 u3-l3 推导的 Schur 补公式逐行对应。预处理循环 [forward.cu:L416-L434](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L416-L434) 中，`gaussian_dim==4` 时先经此函数拿 `time_mask`，`if (!time_mask) return;` 直接剔除；非 `rot_4d` 的 4D 分支（L430–434）用 `sigma` 同式计算并同样 `opacity *= marginal_t`。**结论：默认路径下 Python 无需也不应再乘 marginal_t，乘法在核内恰好发生一次。**

**(d) render() 侧的两个乘点对照**

- 默认路径：[gaussian_renderer/\_\_init\_\_.py:L94-L101](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L94-L101) 只传 `opacity`（L61 的 `get_opacity`，或 L75 的 decay 结果）与 `ts`/`scales_t`，**不相乘**；
- 回退路径：[gaussian_renderer/\_\_init\_\_.py:L91-L93](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L91-L93) `opacity = opacity * marginal_t` 显式相乘，随后 [L132-L133](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L132-L133) 用同一 `marginal_t > 0.05` 掩码预筛。

**(e) opacity_decay 的调用点（预览）**

[scene/gaussian_model.py:L584-L620](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L584-L620)：`opacity_decay(f_min, mode, p, f_max, mask)` 以 `old_opacity = self.get_opacity` 为基数乘上衰减因子，`mask` 非 None 时经 `torch.where` 只衰减可见高斯（L616–617），最后把结果经 `inverse_opacity_activation`（logit）原位写回 `_opacity.data`（L619）并返回激活值。render() 在 [L74-L75](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L74-L75) 拿返回值直接当渲染用的 `opacity`。模式族与梯度语义留给单元 6。

#### 4.3.4 代码实践

**实践目标**：用手算验证 `get_marginal_t` 的公式与阈值掩码。

**操作步骤**（示例代码，可在项目根目录新建临时脚本运行，只需 PyTorch/CPU）：

```python
# 示例代码：验证 get_marginal_t
import torch
from scene.gaussian_model import GaussianModel
from arguments import ModelParams, PipelineParams  # 仅用于构造 args

# 构造一个最小模型（gaussian_dim=4, rot_4d=False，协方差分支走 get_scaling_t）
import argparse
parser = argparse.ArgumentParser()
mp = ModelParams(parser, sentinel=False)   # 3D/4D 开关在脚本参数里
args = mp.extract(parser.parse_args(["--model_path", "/tmp/empty",
                                     "--gaussian_dim", "4"]))
args.rot_4d = False
pc = GaussianModel(sh_degree=0, args=args)

pc._t = torch.tensor([[0.0], [1.0], [5.0], [9.0]])          # 时间中心
pc._scaling_t = torch.log(torch.tensor([[1.0], [1.0], [1.0], [1.0]]))  # σ_t=1

tau = 5.0                                                    # 渲染时刻
m = pc.get_marginal_t(tau)                                   # (4,1)
print(m)                                                     # 手算对照 exp(-0.5*(t-5)^2)
print((m[:, 0] > 0.05))                                      # 阈值掩码
```

**需要观察的现象**：`m` 依次约为 \(\exp(-12.5), \exp(-8), 1, \exp(-8)\)（≈ 3.7e-6, 3.4e-4, 1.0, 3.4e-4）；掩码只有第 3 个高斯为 True——即 \(|t-\tau| \le 2.45\sqrt{\sigma_t}\) 才可见。

**预期结果**：手算与 `get_marginal_t` 输出一致；掩码与公式 \(2.45\sqrt{\sigma_t}\) 时间半宽吻合。把 `_scaling_t` 的 log 值改为 `log(4)`（σ_t=4），半宽应变为 4.9，第 2、4 个高斯（|Δt|=4）也应进入掩码。若 `args` 构造在本地版本报错（参数名随版本变动，待本地验证），可退而直接引用公式在纸面完成对照。

#### 4.3.5 小练习与答案

**练习 1**：`rot_4d` 开关如何改变 \(\sigma_t\)（即 `get_cov_t`）的来源？

**答案**：[scene/gaussian_model.py:L244-L250](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L244-L250)：`rot_4d=True` 时用 `build_scaling_rotation_4d` 组装 \(L\) 并取 \((LL^\top)_{44}\)——由于 \(\Sigma=LL^\top\)，这一项等于时间尺度的**平方**；`False` 时直接返回激活后的 `get_scaling_t` 本身。因此同一 `_scaling_t` 数值在两种模式下 \(\sigma_t\) 差一个平方（u3-l3 陷阱），切换开关必须重训。

**练习 2**：默认 CUDA 路径下，decay 后的 opacity 为什么不需要在 Python 再乘一次 `marginal_t`？

**答案**：乘法在核内恰好发生一次——`forward.cu` 的 `computeCov3D_conditional`（rot_4d 分支）或 L430–434（非 rot_4d 分支）都会执行 `opacity *= marginal_t`。Python 若再乘一次会把时间衰减计两次，运动区域会系统性变透明。Python 回退路径因为绕过了这段核内代码，才需要在 L91–93 显式补上。

**练习 3**：`opacity_decay` 的 `mask=None` 与 `mask=visibility` 行为有何差别？

**答案**：[scene/gaussian_model.py:L616-L617](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L616-L617)：`mask=None` 时对全体高斯统一衰减；给了 mask 时 `torch.where(mask, curr_opacity, old_opacity)` 只衰减当前视角「空间且时间可见」的高斯，其余保持原值——这是 `time_aware` 策略（u6-l3）的核心。

## 5. 综合实践

**任务 A（有 GPU）：渲染统计 hook + 曲线记录**

1. 完成 4.1.4 的打印 hook，并把打印改成追加写 CSV（列：iteration, total, visible, 可见比例）。
2. 跑一次 5000 次迭代的短训练（把配置里 `iterations` 调小，注意 u1-l4 讲过的 `save_iterations` 不同步问题）。
3. 画三条曲线：`total`、`visible`、`visible/total`，回答：(a) 致密化让 total 增长多快？(b) 可见比例随训练上升还是下降？为什么（提示：时间尺度 `_scaling_t` 会被优化变小，时间可见的高斯比例随之变化）？

**任务 B（无 GPU）：渲染输入形状标注 + 调用链图**

1. 在 [gaussian_renderer/\_\_init\_\_.py:L160-L172](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L160-L172) 的光栅化调用处，为每个关键字参数写一行形状注释（N 为高斯总数）：

```text
means3D          (N,3)  ← get_xyz（回退路径 +delta_mean）
means2D          (N,3)  ← screenspace_points（全零占位，梯度容器）
shs              (N,C,3)← get_features，C=get_max_sh_channels；回退路径为 None
colors_precomp   (N,3)  ← 仅 convert_SHs_python/override_color 路径
flow_2d          (N,2)  ← 全零占位
opacities        (N,1)  ← get_opacity（可能含 decay；回退路径已乘 marginal_t）
ts               (N,1)  ← get_t（cov3D 回退路径由 L124-125 兜底）
scales           (N,3)  ← get_scaling；cov3D 回退路径为 None
scales_t         (N,1)  ← get_scaling_t（仅 4D）
rotations        (N,4)  ← get_rotation
rotations_r      (N,4)  ← get_rotation_r（仅 rot_4d）
cov3D_precomp    (N,6)  ← 仅 compute_cov3D_python 路径
```

2. 画出从 `train.py` 到 CUDA 的完整调用链：
   `train.py:138 render(...)` → `gaussian_renderer/__init__.py render()` → `GaussianRasterizer.forward`（三重互斥校验）→ `rasterize_gaussians` → `_RasterizeGaussians.apply` → `_C.rasterize_gaussians`（进入 C++/CUDA）。
3. 在调用链的每一级旁标注「这一级做了什么校验/整理」。

**预期结果**：任务 A 得到三条曲线与一段分析；任务 B 得到一张带形状的调用链图。两者都可直接作为后续 u4-l2（autograd 绑定）与 u5-l4（致密化）的学习底稿。

## 6. 本讲小结

- `render()` 是参数组装器：把 Camera 几何量、模型激活属性、`pipe` 开关打包成 `GaussianRasterizationSettings`（18 字段：相机侧 8、模型侧 6、pipe/render 参数 4），连同 12 个输入张量交给 CUDA 光栅化器，返回 6 键 dict。
- `means2D` 是 `requires_grad=True` 的全零占位张量，唯一作用是接收 backward 的屏幕空间梯度，作为致密化信号；`visibility_filter = radii > 0` 与 `radii` 共同保证被剔除的高斯不参与 `max_radii2D` 更新与致密化统计。
- 几何与颜色各有「默认 CUDA / Python 回退」二选一：默认路径传裸属性（scales/rotations/shs/ts/…），协方差、SH 颜色与 `opacity × marginal_t` 全在核内完成；回退路径在 Python 逐行同构。
- 有效不透明度 = sigmoid(_opacity) × decay 因子（若启用）× marginal_t；rot_4d 开关会改变 \(\sigma_t\) 的来源（平方关系差异）。
- 仓库内 `gaussian_renderer/diff_gaussian_rasterization.py` 是未被 import 的 JIT 变体，实际生效的是 pip 安装的 `diff_gaussian_rasterization` 包——又一处需要识别的遗留代码。

## 7. 下一步学习建议

- **u4-l2（光栅化的 Python 绑定与 markVisible）**：沿本讲的调用链往下，精读 `_RasterizeGaussians` 的 `forward`/`backward` 如何组织 28 个参数、`ctx.save_for_backward` 暂存哪些张量，以及 `markVisible` 如何只靠 viewmatrix/projmatrix 做视锥判断。
- **u4-l3（Python 回退路径与环境贴图背景）**：本讲两次绕过的 `compute_cov3D_python` / `convert_SHs_python` 完整展开——预筛选的 10 类张量同步过滤与 `env_map` 的 `(1-alpha)` 混合。
- 复习锚点：字段来源记不清时回看 [gaussian_renderer/\_\_init\_\_.py:L36-L55](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L36-L55)；marginal_t 公式记不清时回看 [scene/gaussian_model.py:L252-L254](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L252-L254) 与 [forward.cu:L331-L336](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L331-L336) 的对照。
