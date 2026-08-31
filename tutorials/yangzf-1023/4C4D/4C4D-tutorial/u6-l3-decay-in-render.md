# 衰减如何接入渲染：time_aware 可见性掩码

## 1. 本讲目标

前两讲（u6-l1、u6-l2）我们已经知道：Neural Decaying Function 是一个两层 MLP（`Coefficient` 网络），它给每个高斯输出一个落在窄带 \([f_{\min}, f_{\max}]=[0.996, 0.998]\) 内的衰减因子，乘到该高斯的不透明度上，逐迭代连乘形成「复利式软删除」。

但一个关键问题还没有回答：**这个乘法到底发生在哪里？对谁生效？什么时候生效？**

本讲沿渲染调用链精读 `render()` 中的 opacity decay 接入段，学完后你应当能够：

1. 解释 `decay_from_iter` 延迟启用的作用，并说出衰减在整条训练管线里的**唯一激活点**。
2. 追踪 `markVisible` 从 Python 到 CUDA 的三层调用链，理解「空间可见性」的判据。
3. 推导 `get_marginal_t > 0.05` 这一「时间可见性」阈值背后的数学（\(|\Delta t| < 2.45\sqrt{\sigma_t}\)）。
4. 说明「空间可见 ∧ 时间可见」的掩码交集如何保证只衰减当前视角**真正可见**的高斯，以及衰减发生在渲染前对后续梯度通路的影响（包括 `mask=None` 时衰减完全失效这一反直觉陷阱）。

## 2. 前置知识

本讲默认你已读过 u4-l1（render 主流程）、u4-l2（光栅化绑定与 markVisible）、u3-l3（4D 协方差与时间边缘化）、u6-l1/u6-l2（Coefficient 网络与 opacity_decay 模式族）。用三句话唤起记忆：

- **读时激活**：`GaussianModel` 把裸值存进 `_opacity` 等参数，读取时经 `get_opacity = sigmoid(_opacity)` 激活（[scene/gaussian_model.py:L231-L233](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L231-L233)）。
- **4D 高斯**：每个高斯除 xyz 外还有时间中心 `_t` 与时间尺度，渲染某一时刻 \(\tau\) 时它以高斯形式在时间轴上「弥散」，离 \(\tau\) 越远贡献越小。
- **opacity_decay 的双通路**：返回值携带梯度进渲染器（这是衰减网络获得梯度的唯一通路），同时用 `_opacity.data = ...` 原位写回做状态持久化、绕过 autograd。

再补两个本讲要用的基础概念：

- **视锥（frustum）与近平面**：相机只能看到金字塔形视锥内的物体。光栅化前会做粗筛，把明显在相机背后（视图空间深度 \(z \le 0.2\)）的点直接判为不可见，这就是视锥剔除（frustum culling）。
- **`torch.where(mask, A, B)` 的梯度语义**：输出在 `mask` 为 True 处取 A、False 处取 B，反向传播时梯度**只流经被选中的那个分支**——本讲第 4.4 节的「梯度只来自可见高斯」正是由这个语义保证的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [gaussian_renderer/__init__.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py) | 全仓库唯一渲染入口 `render()`，本讲主角：L63-L75 的 opacity decay 接入段 |
| [scene/gaussian_model.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py) | `get_marginal_t`（时间可见性的数学源头）与 `opacity_decay`（掩码消费方） |
| [diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py) | 光栅化 Python 包装层，`GaussianRasterizer.markVisible` 在此 |
| [diff-gaussian-rasterization/rasterize_points.cu](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/rasterize_points.cu) | C++ 薄壳：分配输出掩码并转发给 CUDA |
| [diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu) | CUDA kernel：逐点写 `present[idx]` |
| [diff-gaussian-rasterization/cuda_rasterizer/auxiliary.h](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/auxiliary.h) | `in_frustum` 判据的最终实现（近平面检查） |
| [module/__init__.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/module/__init__.py) | `Coefficient.forward`，梯度通路的终点 |
| [train.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py) | 衰减相关参数注册、唯一传 `args` 的 render 调用点、训练策略联动 |

## 4. 核心概念与源码讲解

### 4.1 render() 的 opacity decay 分支：延迟启用与唯一激活点

#### 4.1.1 概念说明

衰减不是独立于渲染的预处理步骤，而是**内嵌在 `render()` 里**的一段条件逻辑。它受三个条件门控：

1. `args is not None` —— 调用方必须把训练参数传进来；
2. `args.opacity_decay` —— 总开关（默认 True）；
3. `iteration > args.decay_from_iter` —— 必须晚于起始迭代（默认 500，严格大于，即第 501 次迭代起生效）。

为什么要延迟？第 1~500 次迭代是「冷启动期」：高斯刚从稀疏点云初始化，位置、尺度、不透明度都还很粗糙，球谐阶数也在逐档爬升。此时如果就开始衰减，等于在模型还没有任何重建能力时惩罚它——网络分不清「这个高斯没用」和「这个高斯还没学好」。把起点对齐到 `densify_from_iter=500`（[arguments/__init__.py:L96-L100](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L96-L100)），让「致密化（加法）」与「衰减（减法）」几乎同时上线、从同一起跑线开始竞争。

#### 4.1.2 核心流程

```text
render(viewpoint_camera, pc, pipe, bg_color, args, iteration)
  └─ opacity = pc.get_opacity                     # 读时激活：sigmoid(_opacity)
  └─ if args 存在 且 opacity_decay 且 iteration > decay_from_iter:
       ├─ time_aware=True:
       │    space_visibility = rasterizer.markVisible(means3D)      # (N,) bool
       │    time_visibility  = pc.get_marginal_t(τ)[:,0] > 0.05     # (N,) bool
       │    visibility = space_visibility & time_visibility          # 交集
       │    visibility = visibility.view(-1, 1)                      # 对齐 (N,1)
       ├─ time_aware=False: visibility = None
       └─ opacity = pc.opacity_decay(f_min, f_max, mask=visibility)  # 衰减+写回
  └─ （后续照常：准备 scales/rotations/ts，送入 CUDA 光栅化）
```

#### 4.1.3 源码精读

衰减接入段全文只有十几行，位于准备渲染输入的阶段：

[gaussian_renderer/__init__.py:L59-L75](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L59-L75)（下方为关键摘录）

```python
means3D = pc.get_xyz
means2D = screenspace_points
opacity = pc.get_opacity          # 先取「正常」的不透明度

# opacity decay
if (args is not None) and args.opacity_decay and (iteration > args.decay_from_iter):
    if args.time_aware:
        space_visibility = rasterizer.markVisible(means3D)
        time_visibility = pc.get_marginal_t(viewpoint_camera.timestamp)[:,0] > 0.05 
        visibility = space_visibility & time_visibility
        visibility = visibility.view(-1, 1)
    else:
        visibility = None
    
    # Apply opacity decay to Gaussians that are visible in current view
    visible_opacities = pc.opacity_decay(f_min=args.f_min, f_max=args.f_max, mask=visibility)        
    opacity = visible_opacities
```

这段代码做了三件事：先把未衰减的 `opacity` 备好（L61），满足三重门控后构造可见性掩码（L65-L69），最后调用 `pc.opacity_decay` 得到衰减后的不透明度并**替换**局部变量 `opacity`（L74-L75）。注意衰减发生在组装 `GaussianRasterizationSettings` **之后**、调用光栅化 **之前**——传给 CUDA 的就是衰减后的值。

相关参数注册在 train.py，默认值如下：

- [train.py:L411-L419](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L411-L419)：注册 `--opacity_decay`（默认 True）、`--f_max`（0.998）、`--f_min`（0.996）、`--decay_from_iter`（500）。
- [train.py:L427](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L427)：注册 `--time_aware`（默认 True）。

**衰减的唯一激活点**是训练循环里的 render 调用。[train.py:L138](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L138) 是全仓库唯一同时传 `args=args, iteration=iteration` 的调用：

```python
render_pkg = render(viewpoint_cam, gaussians, pipe, background, args=args, iteration=iteration)
```

而 `training_report` 里的评估渲染（[train.py:L332](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L332)）只传 `(pipe, background)`，`args` 保持默认 `None`：

```python
render_pkg = renderFunc(viewpoint, scene.gaussians, *renderArgs)   # args=None → 不衰减
```

`utils/mesh_utils.py` 中推理用的 render 调用同样不传 `args`。由此得到一个重要结论：**衰减只在训练的正向渲染中发生；测试评估与推理渲染读到的都是 `_opacity` 里已累积的衰减状态，但自身不再继续衰减**。

#### 4.1.4 代码实践

**实践目标**：用静态检索证实「衰减只在训练渲染中激活」，并定位所有相关参数。

**操作步骤**：

1. 在仓库根目录执行（只读检索，不改动任何源码）：

```bash
grep -rn "args=args" --include="*.py" .
grep -rn "renderFunc(" train.py
grep -rn "decay_from_iter\|time_aware" --include="*.py" . | grep -v tutorial
```

2. 打开 [train.py:L305-L332](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L305-L332)，确认 `training_report` 的 `renderArgs` 是 `(pipe, background)` 二元组（见 [train.py:L228-L232](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L228-L232) 的调用处），不含 `args`。
3. 推理侧检查：`grep -n "self.render(" utils/mesh_utils.py`，确认这些调用没有 `args`/`iteration`。

**需要观察的现象**：`args=args` 全仓库只命中 train.py:138 一处训练渲染调用；`decay_from_iter` 只在 train.py（注册）与 gaussian_renderer/\_\_init\_\_.py（判断）两处出现。

**预期结果**：你会得到一张「衰减生效边界图」——训练循环内逐视角渲染（衰减），`training_report` 的 train/test 评估渲染（不衰减），`render.py`/`GaussianExtractor` 推理渲染（不衰减）。这也解释了为什么 TensorBoard 里的 test PSNR 评估的是「衰减累积后的存量状态」而非「再衰减一次的瞬时状态」。

#### 4.1.5 小练习与答案

**练习 1**：`iteration > args.decay_from_iter` 用的是严格大于。若 `decay_from_iter=500`，衰减从第几次迭代开始？致密化从第几次开始？

**答案**：训练迭代号从 1 起（train.py 中 `first_iter=0` 后先 `+=1` 再进循环），所以衰减从第 501 次迭代开始。致密化条件是 `iteration > densify_from_iter(500)` 且 `iteration % 100 == 0`（[train.py:L246](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L246)），501~599 之间没有 100 的倍数，第一次实际致密化发生在第 600 次迭代——衰减比致密化早 99 次迭代「先行一步」。

**练习 2**：`--opacity_decay` 与 `--time_aware` 都是 `action="store_true", default=True`。这意味着能通过命令行把它们关掉吗？正确的关闭方式是什么？

**答案**：不能。`store_true` 只能把标志置 True，配上 `default=True` 后无论是否传参该值恒为 True。正确方式是在 yaml 配置里写 `opacity_decay: false` 或 `time_aware: false`——train.py 的 `recursive_merge` 对叶子键无条件 `setattr`（[train.py:L434-L443](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L434-L443)），yaml 值会覆盖命令行结果。这也是本讲综合实践做消融的参数注入通道。

**练习 3**：如果把衰减逻辑搬到训练循环里、在 `render()` 之外调用，会破坏什么？

**答案**：会破坏「衰减因子参与当前视角可微渲染」这一设计。衰减后的 `opacity` 必须作为光栅化的输入张量进入计算图，反向传播时 `grad_opacities` 才能经链式法则回流到 `Coefficient` 网络参数；在渲染外做衰减则衰减网络拿不到任何梯度，联合优化退化为单向手工调度。

### 4.2 空间可见性：markVisible 三层调用链

#### 4.2.1 概念说明

「空间可见性」回答的问题是：**这个高斯的中心在当前相机的视锥内吗？** 它由光栅化扩展暴露的 `markVisible` 接口给出。它与渲染结果里的 `visibility_filter`（`radii > 0`，渲染后得到）不同：

- `markVisible` 是**渲染前**的保守粗筛，只看高斯中心点的位置，不关心椭球大小与遮挡，输出必是最终可见性的超集；
- `visibility_filter` 是**渲染后**的精确判定（屏幕半径大于 0），但要付出完整光栅化的代价。

衰减需要的是渲染前的判断（因为衰减本身就是渲染前对输入的修改），所以选用前者。整条调用链穿过四层代码（与 u4-l2 的五层架构一致）。

#### 4.2.2 核心流程

```text
render() 业务层            rasterizer.markVisible(means3D)
    ↓
Python 包装层（diff_gaussian_rasterization/__init__.py）
    no_grad 下调 _C.mark_visible(positions, viewmatrix, projmatrix)
    ↓
C++ 薄壳（rasterize_points.cu）
    分配 (P,) 的 torch.bool 张量 present，初始化为 False
    ↓
CUDA kernel（rasterizer_impl.cu 的 checkVisible）
    每线程处理一个高斯：present[idx] = in_frustum(...)
    ↓
判据（auxiliary.h 的 in_frustum）
    p_view = 视图空间坐标；p_view.z <= 0.2 → 不可见（近平面剔除）
```

#### 4.2.3 源码精读

Python 包装层在 `no_grad` 下调用 CUDA，返回 `(P,)` 的布尔掩码：

[diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py:L231-L240](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py#L231-L240)（关键摘录）

```python
def markVisible(self, positions):
    # Mark visible points (based on frustum culling for camera) with a boolean 
    with torch.no_grad():
        raster_settings = self.raster_settings
        visible = _C.mark_visible(
            positions,
            raster_settings.viewmatrix,
            raster_settings.projmatrix)
        
    return visible
```

注意两点：`torch.no_grad()` 表明这只是个无梯度的查询；它复用了 `render()` 刚组装好的 `raster_settings` 里的视图/投影矩阵——所以 `markVisible` 必须在 `GaussianRasterizer` 构造之后调用（源码中正是如此，[gaussian_renderer/__init__.py:L57](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L57) 先建 rasterizer，L66 才调 markVisible）。

C++ 薄壳分配输出张量并转发：

[diff-gaussian-rasterization/rasterize_points.cu:L268-L287](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/rasterize_points.cu#L268-L287)（关键摘录）

```cpp
torch::Tensor markVisible(
        torch::Tensor& means3D,
        torch::Tensor& viewmatrix,
        torch::Tensor& projmatrix)
{ 
  const int P = means3D.size(0);
  
  torch::Tensor present = torch::full({P}, false, means3D.options().dtype(at::kBool));
 
  if(P != 0)
  {
    CudaRasterizer::Rasterizer::markVisible(P,
        means3D.contiguous().data<float>(),
        viewmatrix.contiguous().data<float>(),
        projmatrix.contiguous().data<float>(),
        present.contiguous().data<bool>());
  }
  
  return present;
}
```

CUDA kernel 逐点写掩码：

[diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu:L55-L67](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterizer/cuda_rasterizer/rasterizer_impl.cu#L55-L67)（关键摘录）

```cpp
auto idx = cg::this_grid::thread_rank();
if (idx >= P)
    return;

float3 p_view;
float3 orig_point={orig_points[3*idx],orig_points[3*idx+1],orig_points[3*idx+2]};
present[idx] = in_frustum(orig_point, viewmatrix, projmatrix, false, p_view);
```

最终判据在 `in_frustum`：

[diff-gaussian-rasterization/cuda_rasterizer/auxiliary.h:L140-L163](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/auxiliary.h#L140-L163)（关键摘录）

```cpp
// Bring points to screen space
float4 p_hom = transformPoint4x4(p_orig, projmatrix);
float p_w = 1.0f / (p_hom.w + 0.0000001f);
float3 p_proj = { p_hom.x * p_w, p_hom.y * p_w, p_hom.z * p_w };
p_view = transformPoint4x3(p_orig, viewmatrix);

if (p_view.z <= 0.2f) // || ((p_proj.x < -1.3 || p_proj.x > 1.3 || p_proj.y < -1.3 || p_proj.y > 1.3)))
{
    ...
    return false;
}
return true;
```

注意 **x/y 方向的 NDC 边界检查被注释掉了**，实际生效的只有近平面深度判据 \(p_{\text{view}}.z \le 0.2\)。也就是说：相机正后方和紧贴近平面的高斯会被剔除，但水平/垂直方向出界的点仍算「可见」。这正体现了 markVisible 的「保守超集」定位——宁可多留，不可漏杀。对衰减而言这是合理的：出界但接近边界的高斯其椭球仍可能投进画面。

顺带一个矩阵约定提醒（承接 u2-l3）：`viewmatrix` 是 `world_view_transform`，在 [scene/cameras.py:L66](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/cameras.py#L66) 存储前做了 `transpose(0,1)`（CUDA 按列主序消费）；CUDA 侧 `transformPoint4x3`（[auxiliary.h:L59-L67](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/auxiliary.h#L59-L67)）用跨步索引 `matrix[0]/matrix[4]/matrix[8]/matrix[12]` 取数，恰好等价于用原始行主序 W2C 矩阵左乘齐次坐标。

#### 4.2.4 代码实践

**实践目标**：不依赖 CUDA 扩展，用 numpy 复刻 `in_frustum` 的近平面判据，直观理解「什么样的点被判为空间不可见」。

**操作步骤**：新建临时脚本（放在仓库外或 `/tmp`，勿写入源码目录），运行以下**示例代码**：

```python
# 示例代码：纯 numpy 复刻 in_frustum 的近平面判据（CPU 可运行）
import numpy as np

np.random.seed(0)
N = 8
xyz = np.random.randn(N, 3) * 3.0     # 假想的高斯中心

# 构造一个位于原点、朝 +z 看的相机：W2C 为单位阵（行主序）
W2C = np.eye(4)
stored = W2C.T                        # 复刻 cameras.py 的列主序存储（transpose(0,1)）

# transformPoint4x3 的等价形式：p_view = xyz @ stored[:3,:3] + stored[3,:3]
p_view = xyz @ stored[:3, :3] + stored[3, :3]
visible = p_view[:, 2] > 0.2          # 近平面判据，与 auxiliary.h:153 一致

for x, z, v in zip(xyz[:, 2], p_view[:, 2], visible):
    print(f"world_z={x:+.3f}  view_z={z:+.3f}  visible={v}")
```

**需要观察的现象**：`view_z` 与 `world_z` 数值一致（单位阵相机下视图空间即世界空间）；所有 `view_z <= 0.2` 的点 `visible` 均为 False，其余为 True。

**预期结果**：约一半随机点落在相机后方（\(z<0\)）被剔除；\(0 < z \le 0.2\) 的点虽然在前方但贴近平面也被剔除。若把相机改为朝 \(-z\) 看（例如 `W2C[:3,:3] = np.diag([1,-1,-1])`），剔除集合会整体翻转——验证判据完全由视图空间深度决定。（真实数据上的 GPU 调用结果待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：`markVisible` 的输出与渲染返回的 `visibility_filter`（`radii>0`）有何区别？为什么衰减用的是前者？

**答案**：`markVisible` 是渲染前的几何粗筛（只判高斯中心是否过近平面，x/y 出界不剔除，是超集），`visibility_filter` 是渲染后的精确判定（含投影半径、实际覆盖像素）。衰减必须发生在渲染前（它修改的正是渲染输入 `opacity`），此时 `radii` 尚未产生，只能用 markVisible。

**练习 2**：`markVisible` 为什么包在 `torch.no_grad()` 里？如果去掉会发生什么？

**答案**：可见性判断是离散的二值决策，本身不可微，也不需要梯度。去掉 `no_grad` 后该调用会进入 autograd 图，但因为输出是 `torch.bool` 张量、且从不参与损失回传，功能上仍能运行，只是白白记录计算图、徒增开销——作者用 `no_grad` 明示「这是一次纯查询」。

**练习 3**：一个高斯中心在视锥外 1 米处，但椭球半径 2 米、部分投影进画面。`markVisible` 和最终渲染分别怎么处理它？

**答案**：`markVisible` 只要中心过了近平面就返回 True（x/y 出界被注释掉），所以它会被计入「空间可见」、参与衰减；最终渲染按完整投影计算其屏幕半径，`radii>0`，也可见。两者此时一致。反过来若中心在近平面之后，markVisible 判 False，而 CUDA 前向渲染的正式剔除路径（forward.cu 中同样调用 `in_frustum`）也会跳过它——两条路径共用同一判据。

### 4.3 时间可见性：get_marginal_t 与 0.05 阈值

#### 4.3.1 概念说明

4D 高斯在时间轴上是一条以 \(_t\) 为中心、以 \(\sqrt{\sigma_t}\) 为尺度的一维高斯。渲染时刻为 \(\tau\)（即 `viewpoint_camera.timestamp`）时，它对当前帧的贡献按时间边缘分布衰减：

\[
\text{marginal\_t} = \exp\!\left(-\frac{(t-\tau)^2}{2\sigma_t}\right)
\]

「时间可见性」定义为 \(\text{marginal\_t} > 0.05\)：高斯的时间中心离当前渲染时刻足够近，才认为它「正在参与当前帧的重建」。直觉上，一个时间上远离 \(\tau\) 的高斯在本次渲染中几乎不贡献像素，也就几乎收不到重建梯度——如果对它衰减，它既无法被梯度「救回」，也不该被此刻的衰减惩罚（它可能在别的时刻有用）。这就是时间掩码的意义。

#### 4.3.2 核心流程

阈值 0.05 对应的时间距离可以闭式解出。令衰减等于阈值：

\[
\exp\!\left(-\frac{\Delta t^2}{2\sigma_t}\right) > 0.05
\;\Longleftrightarrow\;
\Delta t^2 < 2\sigma_t \ln 20 \approx 5.99\,\sigma_t
\;\Longleftrightarrow\;
|\Delta t| < 2.45\sqrt{\sigma_t}
\]

即掩码保留的是「时间中心落在渲染时刻 \(\pm 2.45\) 个时间标准差之内」的高斯。这个 2.45 与 u4-l3 讲过的 Python 回退路径预筛阈值（[gaussian_renderer/__init__.py:L132-L133](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L132-L133)）完全同源——同一把尺子既用于调试路径的预筛选，也用于衰减的时间掩码。

执行流程：

```text
get_marginal_t(timestamp τ)
  ├─ sigma = get_cov_t()                # 时间方差 σ_t
  │    ├─ rot_4d=True:  取 4D 协方差 Σ 的 (3,3) 元素（时间方差）
  │    └─ rot_4d=False: 直接用 get_scaling_t = exp(_scaling_t)
  └─ return exp(-0.5 * (get_t - τ)² / sigma)    # (N,1)
       → [:,0] > 0.05 得 (N,) bool 时间掩码
```

#### 4.3.3 源码精读

[scene/gaussian_model.py:L252-L254](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L252-L254)

```python
def get_marginal_t(self, timestamp, scaling_modifier = 1): # Standard
    sigma = self.get_cov_t(scaling_modifier)
    return torch.exp(-0.5*(self.get_t-timestamp)**2/sigma) # / torch.sqrt(2*torch.pi*sigma)
```

这行就是上一节公式的一比一实现：分子是时间中心与渲染时刻的平方差，分母是 \(2\sigma_t\)。被注释掉的归一化因子 \(\sqrt{2\pi\sigma_t}\) 说明作者明确不关心概率密度口径，只要相对衰减幅度。

\(\sigma_t\) 的来源分两条路：

[scene/gaussian_model.py:L244-L250](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L244-L250)

```python
def get_cov_t(self, scaling_modifier = 1):
    if self.rot_4d:
        L = build_scaling_rotation_4d(scaling_modifier * self.get_scaling_xyzt, self._rotation, self._rotation_r)
        actual_covariance = L @ L.transpose(1, 2)
        return actual_covariance[:,3,3].unsqueeze(1)
    else:
        return self.get_scaling_t * scaling_modifier
```

承接 u3-l3 的提醒：`_scaling_t` 的语义在 rot_4d 开关下**差一个平方**——rot_4d 时走完整 4D 协方差取时间方差（元素含尺度平方），非 rot_4d 时 `exp(_scaling_t)` 直接充当方差。做数值实验时务必先确认自己处在哪条分支。

在 render() 的衰减分支里，`get_marginal_t` 的调用与切片：

[gaussian_renderer/__init__.py:L67](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L67)

```python
time_visibility = pc.get_marginal_t(viewpoint_camera.timestamp)[:,0] > 0.05 
```

`get_marginal_t` 返回 `(N,1)`，`[:,0]` 压成 `(N,)` 再与同为 `(N,)` 的 `space_visibility` 按位与。另外注意：默认渲染路径（`compute_cov3D_python=False`）下 CUDA 核内部会自行完成等价的时间边缘化并乘进不透明度（u4-l1 讲过），这里的 Python 调用**只为构造掩码**，是一次独立的只读评估，两处使用互不影响。

#### 4.3.4 代码实践

**实践目标**：用数值实验验证 \( |\Delta t| < 2.45\sqrt{\sigma_t} \) 这一边界，并观察阈值对掩码保留率的影响。

**操作步骤**：运行以下**示例代码**（纯 CPU）：

```python
# 示例代码：验证 get_marginal_t 的 0.05 阈值边界（CPU 可运行）
import torch

torch.manual_seed(0)
N, tau, sigma_t = 100000, 5.0, 0.4     # 渲染时刻 τ=5，时间方差 σ_t=0.4
t = torch.empty(N).uniform_(0, 10)     # 时间中心均匀铺满 [0,10]（time_duration）

marginal = torch.exp(-0.5 * (t - tau) ** 2 / sigma_t)   # 复刻 gaussian_model.py:254
mask = marginal > 0.05

# 1) 边界验证：掩码保留的点应全部满足 |Δt| < sqrt(2*ln(20)*σ_t)
bound = (2 * torch.log(torch.tensor(20.0)) * sigma_t) ** 0.5
print(f"理论边界 = {bound:.4f} (= 2.45*sqrt(0.4))")
print(f"掩码内最大 |Δt| = {(t[mask] - tau).abs().max():.4f}")
print(f"恰好在边界上的 marginal = {torch.exp(-0.5 * bound**2 / sigma_t):.4f}")

# 2) 保留率：时间中心均匀分布时，掩码留下多大比例的高斯
print(f"掩码保留率 = {mask.float().mean():.2%}")
```

**需要观察的现象**：`掩码内最大 |Δt|` 约等于 `理论边界`（浮点误差内）；边界处 `marginal` 恰为 0.05；保留率约为 \(2 \times 2.45\sqrt{0.4} / 10 \approx 31\%\)。

**预期结果**：输出依次约为 1.5481、略小于 1.5481、0.0500、31%。可再改 `sigma_t`（如 0.1、1.6）重复运行：\(\sigma_t\) 越大（时间弥散越宽）保留率越高——说明时间掩码的松紧完全由各高斯自己的时间尺度决定，长寿命高斯几乎时刻「时间可见」，短寿命高斯只在邻近时刻被衰减。

#### 4.3.5 小练习与答案

**练习 1**：默认配置 `rot_4d=True`、`time_duration=[0,10]` 时，初始化令 `dist_t = duration/5 = 2` 并存 `_scaling_t = log(√dist_t)`（[scene/gaussian_model.py:L428-L429](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L428-L429)）。此刻一个新初始化的高斯对给定渲染时刻的「时间可见窗口」有多宽？

**答案**：rot_4d 分支下 \(\sigma_t\) 取 4D 协方差的 (3,3) 元素，即 \( (\text{get\_scaling\_t})^2 = \text{dist\_t} = 2\)，故 \(\sqrt{\sigma_t} \approx 1.41\)，窗口半宽 \(2.45 \times 1.41 \approx 3.46\)，在 \([0,10]\) 定义域内覆盖约 69%——初始高斯大多能通过时间掩码，只有训练把 \(\sigma_t\) 收紧之后时间掩码才真正开始筛选。（若 `rot_4d=False`，则 \(\sigma_t = \text{get\_scaling\_t} = \sqrt{2}\)，窗口半宽约 2.91——这正是 4.3.3 节所说「差一个平方」的具体体现。）

**练习 2**：为什么时间掩码阈值取 0.05 而不是更小（如 1e-4）或更大（如 0.5）？

**答案**：更小的阈值让掩码接近「全通过」，时间维筛选失效，远时刻高斯也会被衰减，可能误杀在别的帧有用的高斯；更大的阈值过度收紧，大量近时刻高斯逃过衰减，软删除变慢。0.05 对应 \(\pm 2.45\) 个时间标准差，与正态分布 98%~99% 覆盖带的工程惯例一致，也与 Python 回退路径的预筛阈值统一，是「有效贡献范围」的合理代理。

**练习 3**：`get_marginal_t` 在 render() 里被调用时传的 `scaling_modifier` 是多少？这意味着什么？

**答案**：默认值 1（[gaussian_renderer/__init__.py:L67](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L67) 调用未传该参数）。`scaling_modifier` 本是渲染放大因子的钩子（`render()` 的形参，默认也是 1.0），掩码评估与实际渲染使用同一默认值，保证「掩码判可见」与「核内实际贡献」口径一致。

### 4.4 掩码交集、渲染前衰减与梯度双通路

#### 4.4.1 概念说明

三个模块合流：`visibility = space_visibility & time_visibility` 把「相机看得见」与「此刻在场」取交集，只有**当前视角真正可见**的高斯才被衰减。这个设计的核心动机是**公平竞争**：

- 被掩码选中的高斯正在参与本次渲染、正在接收重建梯度。若它对成像有益，梯度会推高其有效不透明度（以及衰减因子），形成「救援」；若它无用，梯度不救，衰减因子逐迭代连乘把它压向剪枝线。
- 掩码外的高斯本次既不贡献也不被罚，等它真正出现在某个视角/时刻时再接受检验。

于是衰减不再是盲目地全局压缩，而是一场「每个高斯在自己被看见的每一次，都要证明自己有用」的持续考试。这正是论文所说「把梯度聚焦到几何学习」的机制落地。

连乘的数学：设每次衰减乘以因子 \(f\in[f_{\min}, f_{\max}]\)，\(k\) 次后有效不透明度为

\[
o_k = o_0 \cdot f^k
\]

从 \(o_0=0.1\) 跌破剪枝线 \(0.005\)（`thresh_opa_prune`，[arguments/__init__.py:L96](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L96)）所需次数为

\[
k > \frac{\ln(0.005/0.1)}{\ln f} \approx
\begin{cases}
747, & f = 0.996\\
1496, & f = 0.998
\end{cases}
\]

再叠加 batch 视角维度：`batch_size=4` 时一个迭代内 `render()` 被逐视角调用 4 次，每次都带该视角的掩码做一次衰减——高斯的衰减速度与它「被看见的频率」成正比，越显眼的高斯越早面临抉择。

#### 4.4.2 核心流程

```text
掩码构造（render 内）
  visibility (N,1) ──────────────┐
                                 ▼
opacity_decay(f_min, f_max, mask)
  old = get_opacity                          # 带梯度
  factor = f_min + (f_max-f_min) * Coefficient(old, xyzt, scaling_xyzt)
  curr = old * factor
  opacity = torch.where(mask, curr, old)     # 梯度只流经被选分支
  _opacity.data = inverse_sigmoid(opacity)   # 状态持久化，绕过 autograd
  return opacity                             # 携带梯度，交给光栅化
                                 │
                                 ▼
CUDA 光栅化（以衰减后的 opacity 做 alpha 合成）
  backward: grad_opacities ──链式法则──▶ Coefficient 参数 & _opacity
                                 │
                                 ▼
train.py: coef_optimizer.step() / optimizer.step()   # 双优化器交替前进
```

#### 4.4.3 源码精读

掩码交集与形状对齐在渲染端完成：

[gaussian_renderer/__init__.py:L64-L75](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L64-L75)（关键摘录）

```python
if (args is not None) and args.opacity_decay and (iteration > args.decay_from_iter):
    if args.time_aware:
        space_visibility = rasterizer.markVisible(means3D)
        time_visibility = pc.get_marginal_t(viewpoint_camera.timestamp)[:,0] > 0.05 
        visibility = space_visibility & time_visibility
        visibility = visibility.view(-1, 1)      # (N,) → (N,1)，与 opacity 对齐
    else:
        visibility = None
    
    visible_opacities = pc.opacity_decay(f_min=args.f_min, f_max=args.f_max, mask=visibility)        
    opacity = visible_opacities
```

`view(-1, 1)` 不可省略：`torch.where` 要求掩码与两个值张量形状可广播，`(N,1)` 的 `opacity` 配 `(N,1)` 的掩码正好逐高斯对位。注意 `render()` 默认走 `mode='net'`——调用方没传 mode，`opacity_decay` 的签名默认值就是 `mode='net'`（其余六种模式是消融工具箱，见 u6-l2）。

掩码的消费与双通路写在 `opacity_decay` 尾部：

[scene/gaussian_model.py:L602-L620](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L602-L620)（关键摘录）

```python
elif mode == 'net': # [f_min, f_max]
    if self.coefficient is None:
        raise ValueError("Coefficient is not defined")
    curr_opacity = old_opacity * (f_min + (f_max - f_min) * self.coefficient(old_opacity, self.get_xyzt, self.get_scaling_xyzt))
...
opacity = old_opacity
if mask is not None: 
    opacity = torch.where(mask, curr_opacity, old_opacity)
    
self._opacity.data = self.inverse_opacity_activation(opacity)
return opacity
```

四个要点逐条对应学习目标「衰减发生在渲染前对梯度的影响」：

1. **`mask=None` 陷阱**：若 `time_aware=False`，`opacity` 保持 `old_opacity`——不仅状态写回毫无变化，返回值也不含衰减因子，**衰减彻底失效**（连 Coefficient 的梯度通路都被切断）。管线里衰减真正生效完全依赖 `time_aware` 默认 True。
2. **梯度只来自可见高斯**：`torch.where` 反向时梯度只流经选中分支，`Coefficient` 网络只在「本次被看见的高斯」上获得梯度，与掩码的公平性设计严格对应。
3. **双通路解耦**：返回值 `opacity` 携带梯度（经 `curr_opacity = old_opacity * factor`，`old_opacity = sigmoid(_opacity)` 本身也带梯度，所以 `_opacity` 与 Coefficient 同时收到梯度）；`_opacity.data = ...` 写回是纯状态持久化，不进计算图、不破坏 `Parameter` 身份与优化器。
4. **渲染前生效**：替换后的 `opacity` 在 L160-L172 被送进光栅化器（[gaussian_renderer/__init__.py:L160-L172](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L160-L172)），CUDA 按 alpha 合成消费它；反向返回的 `grad_opacities`（十三元梯度组中的第 6 项，见 u4-l2）经链式法则同时回流到 `_opacity` 与 Coefficient。

梯度终点与双优化器在训练循环收尾：

- [module/__init__.py:L33-L48](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/module/__init__.py#L33-L48)：`Coefficient.forward` 完成输入拼接与归一化，是梯度的最终落点。
- [train.py:L263-L265](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L263-L265)：高斯优化器 step 之后 `coef_optimizer` 紧接着 step，两个网络逐迭代交替更新。

最后是衰减开关带来的三处训练策略联动（本讲只指路，细节在 u6-l4）：

- [train.py:L473-L474](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L473-L474)：`opacity_decay` 开启时 `densify_until_iter = iterations`，致密化窗口拉满全程——因为衰减持续制造「待填补的空洞」，需要致密化长期在线对冲。
- [train.py:L246-L249](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L246-L249)：开启衰减时 `size_threshold` 强制为 None，停用「屏幕尺寸过大即剪枝」条件。
- [train.py:L254-L256](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L254-L256)：周期性 `reset_opacity` 被 `not args.opacity_decay` 条件屏蔽——3DGS 靠定期重置不透明度逼出冗余，4C4D 改用衰减做「温和版重置」，两者互斥。

#### 4.4.4 代码实践

**实践目标**：在纯 CPU 上复现「掩码门控的复利衰减 + 梯度只来自可见高斯」两个核心机制（不依赖 CUDA 扩展与数据集）。

**操作步骤**：在仓库根目录用 `python -c` 或临时脚本运行以下**示例代码**（`module` 包无 CUDA 依赖，可直接 import）：

```python
# 示例代码：CPU 复现掩码门控衰减与梯度选择性（在仓库根目录运行）
import torch
from module import Coefficient

torch.manual_seed(0)
N = 2000
coef = Coefficient(hidden_dim=32, use_4d_features=True, dropout_rate=0.1)
f_min, f_max = 0.996, 0.998

opacity = torch.full((N, 1), 0.1)        # 模拟激活后的 get_opacity
xyzt = torch.randn(N, 4) * 2.0           # 模拟 get_xyzt
scaling_xyzt = torch.rand(N, 4) + 0.1    # 模拟 get_scaling_xyzt
opt = torch.optim.Adam(coef.parameters(), lr=1e-5, weight_decay=1e-4)

# ── 实验 A：梯度只来自可见高斯 ──────────────────────────────
def grad_with_mask(mask):
    factor = f_min + (f_max - f_min) * coef(opacity, xyzt, scaling_xyzt)
    loss = (opacity * factor * mask).sum() / mask.sum()   # 只有可见高斯"参与渲染"
    opt.zero_grad(); loss.backward()
    return coef.net[0].weight.grad.clone()

maskA = (torch.rand(N) > 0.5).view(-1, 1)
maskB = (torch.rand(N) > 0.5).view(-1, 1)
gA, gB = grad_with_mask(maskA), grad_with_mask(maskB)
print("不同掩码 → 梯度不同:", not torch.allclose(gA, gB))
print("梯度非全零:", gA.abs().sum().item() > 0)

# ── 实验 B：复利式软删除的速率 ──────────────────────────────
for it in range(1, 1201):
    if it <= 500:                        # 复刻 decay_from_iter = 500
        continue
    mask = (torch.rand(N) > 0.4).view(-1, 1)          # 模拟交集掩码：约 60% 可见
    factor = f_min + (f_max - f_min) * coef(opacity, xyzt, scaling_xyzt)
    decayed = opacity * factor
    # 状态持久化：只对可见高斯写入衰减（对应 _opacity.data 写回）
    opacity = torch.where(mask, decayed.detach(), opacity)

print(f"700 次衰减后 mean={opacity.mean():.4f}  min={opacity.min():.4f}")
print(f"跌破 0.005 剪枝线的比例: {(opacity < 0.005).float().mean():.2%}")
```

**需要观察的现象**：实验 A 输出两个 True——掩码变了梯度就变，证明 Coefficient 的梯度完全由「哪些高斯可见」决定；实验 B 中不透明度均值从 0.1 单调下滑，部分高斯跌破 0.005。

**预期结果**：约 700 次衰减、每次保留 60% 高斯（期望每个高斯被衰减约 420 次），\(0.1 \times 0.997^{420} \approx 0.029\)——均值降到 0.03 上下但一般还不到 0.005，跌破剪枝线的比例应接近 0%；把循环次数加倍后跌破比例会显著上升。这与 4.4.1 节的理论估算（约 750~1500 次衰减跌穿）自洽。若想精确复现训练中的频率，把每迭代掩码改为 4 份（对应 batch_size=4 的逐视角衰减）即可。

#### 4.4.5 小练习与答案

**练习 1**：`space_visibility` 与 `time_visibility` 为什么取交集（`&`）而不是并集或只用其中一个？

**答案**：只用空间掩码会衰减「在视锥内但此刻不在场」的高斯——它们本次渲染几乎无贡献、拿不到救援梯度，会被无声误杀；只用时间掩码则会衰减「在场但背对相机」的高斯，同样无梯度救援。交集保证被衰减者恰好是「正在接收重建梯度」的那批，衰减与梯度形成公平竞争。

**练习 2**：一个高斯在 batch_size=4 的迭代中 4 个视角全部可见，另一个只在前 3 个视角可见。一个迭代内各被衰减几次？这说明什么？

**答案**：分别被衰减 4 次和 3 次——`render()` 逐视角调用，每次都用该视角的掩码做一次 `opacity_decay` 并写回状态。说明衰减速率与可见频率成正比：越常被看见的高斯越快积累衰减压力，也越有机会频繁获得梯度救援，两者天然同频。

**练习 3**：阅读 [train.py:L254-L256](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L254-L256)：为什么 `opacity_decay` 开启后要禁用周期性 `reset_opacity`？

**答案**：`reset_opacity` 把全部高斯不透明度粗暴重置为低值再让有用的长回来，是 3DGS 清除冗余的手段；而衰减是它的「连续温和版」——每迭代乘 0.996~0.998 并由梯度选择性救援。两者同时开启会互相打架：重置会清空衰减精心维持的梯度竞争格局，衰减又会立刻侵蚀重置刚设的基线，故代码用 `not args.opacity_decay` 使两者互斥。

## 5. 综合实践

设计并执行一组**衰减强度小规模消融**，把本讲三个模块（延迟启用、空间×时间掩码、渲染前衰减）串成一条可量化的实验线。

**实践目标**：量化 `decay_from_iter`（启用时机）与 `f_min/f_max`（衰减强度）对最终测试 PSNR 与高斯数量的影响，验证「衰减强度与几何质量」的权衡关系。

**准备：参数注入通道**。`--opacity_decay`/`--time_aware` 是 `store_true` 且默认 True，命令行关不掉；正确做法是复制配置：

```bash
cp configs/dynerf/flame_steak.yaml /tmp/ablation_base.yaml
```

然后在副本顶层增改这些键（`recursive_merge` 会无条件覆盖同名参数）：

```yaml
opacity_decay: True      # 消融对照组设为 false
time_aware: True
decay_from_iter: 500     # 试 0 / 500 / 2000
f_min: 0.996             # 强衰减档试 0.99
f_max: 0.998             # 强衰减档试 0.995
exhaust_test: True       # 每 test_per_iter=1500 迭代输出一次测试 PSNR
```

为控制成本，可在 `OptimizationParams` 段把 `iterations` 调小（如 `7_000`）、`resolution` 调大（如 4，更粗）。注意 u1-l4 讲过的坑：`save_iterations` 在 yaml 合并前已追加，改 `iterations` 后需确认 `[7000, 30000]` 默认保存点仍覆盖你的终点，且 `model_path` 已存在会直接报错——每次实验换一个输出目录。

**执行矩阵**（核心 5 组，算力充裕可扩到 3×3=9 组）：

| 组 | opacity_decay | decay_from_iter | (f_min, f_max) | 命令 |
| --- | --- | --- | --- | --- |
| A 基线 | false | — | — | `python train.py --config /tmp/ablation_A.yaml` |
| B 默认 | true | 500 | (0.996, 0.998) | `python train.py --config /tmp/ablation_B.yaml` |
| C 提前 | true | 0 | (0.996, 0.998) | `python train.py --config /tmp/ablation_C.yaml` |
| D 延后 | true | 2000 | (0.996, 0.998) | `python train.py --config /tmp/ablation_D.yaml` |
| E 强衰减 | true | 500 | (0.99, 0.995) | `python train.py --config /tmp/ablation_E.yaml` |

**记录与观察**：

1. 最终测试 PSNR：解析 stdout 中 `[ITER N] Evaluating test: L1 ... PSNR ...` 行，或读 TensorBoard 的 `test/loss_viewpoint - psnr` 标量。
2. 高斯数量曲线：TensorBoard 的 `total_points`（[train.py:L311](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L311) 每 100 迭代记录一次），同时关注 `opacity_histogram`（[train.py:L312](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L312)）看低不透明度堆积是否被衰减清走。
3. 建议每张曲线在 `decay_from_iter` 附近放大观察——B 组应在 500 附近出现不透明度分布左移的拐点。

**结果表模板**（待本地验证后填写）：

| 组 | 最终测试 PSNR ↑ | 最终高斯数 | PSNR/百万点 | 现象描述 |
| --- | --- | --- | --- | --- |
| A | | | | |
| B | | | | |
| C | | | | |
| D | | | | |
| E | | | | |

**分析框架**（先写预测再对答案）：

- C（提前启用）vs B：预测 C 的高斯数更少——早期高斯尚未学好就被衰减，部分被误杀；PSNR 持平或略降。
- D（延后启用）vs B：预测 D 的高斯数更多——前 2000 迭代无衰减压力、致密化自由生长；PSNR 变化小但「每百万点的 PSNR」效率下降。
- E（强衰减）vs B：\(f_{\min}=0.99\) 使跌穿剪枝线所需次数从约 750 降到约 300，预测高斯数显著下降；若过度剪枝则 PSNR 受损，否则得到更精简的模型——这正是窄带 \([0.996,0.998]\) 被选为默认的原因。
- A（关闭）vs B：同时注意 A 组会恢复 `reset_opacity` 周期重置与 15_000 的致密化截止（[train.py:L473-L474](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L473-L474) 的联动消失），对照时须区分「衰减本身的贡献」与「联动策略的贡献」。

**无 GPU 时的替代交付**：不执行训练，提交一份实验设计文档，包含上表的预测列及依据（引用本讲 4.4.1 的连乘公式估算各组跌穿剪枝线的迭代数）、参数注入的 yaml 片段、以及计划采集的指标清单，并明确标注所有数值「待本地验证」。

## 6. 本讲小结

- 衰减内嵌于 `render()`，受 `args is not None`、`opacity_decay`、`iteration > decay_from_iter`（默认 500，严格大于）三重门控；`train.py:138` 是全仓库唯一传 `args` 的渲染调用，**评估与推理渲染都不衰减**。
- 空间可见性由 `markVisible` 给出：Python 包装层 `no_grad` 调用 → C++ 薄壳分配 `(P,)` bool → CUDA kernel 逐点调 `in_frustum`，实际判据只有近平面 \(p_{\text{view}}.z \le 0.2\)（x/y 边界检查被注释），是渲染前保守超集。
- 时间可见性由 `get_marginal_t(\tau) > 0.05` 给出，等价于时间中心落在渲染时刻 \(\pm 2.45\sqrt{\sigma_t}\) 内；\(\sigma_t\) 在 rot_4d 开关下取自不同分支（4D 协方差 (3,3) 元素 vs `exp(_scaling_t)`），语义差一个平方。
- 掩码取交集后经 `view(-1,1)` 对齐，由 `torch.where` 消费：梯度只流经可见分支，衰减与重建梯度在每个高斯「被看见的每一次」公平竞争；`_opacity.data` 写回做状态持久化、返回值携梯度进光栅化，双通路解耦。
- 复利数学：\(o_k = o_0 f^k\)，默认窄带下约 750~1500 次衰减跌穿 0.005 剪枝线，且衰减速率与 batch 内可见视角数成正比。
- 两个反直觉细节：`time_aware=False`（即 `mask=None`）时衰减**完全失效**；`--opacity_decay`/`--time_aware` 因 `store_true` 默认 True 而无法从命令行关闭，消融必须走 yaml 的 `recursive_merge` 通道。

## 7. 下一步学习建议

本讲讲清了衰减「在哪、何时、对谁」生效；下一讲 **u6-l4 联合优化：双优化器与训练策略联动** 将补齐最后一环：`coef_optimizer` 的构建细节（`coefficient_lr`、`weight_decay` 对小网络的正则作用）、两个优化器逐迭代交替 step 的动力学，以及 `densify_until_iter=iterations`、禁用 `reset_opacity` 与 `size_threshold` 三处联动如何共同实现「梯度聚焦几何学习」。建议先消化本讲综合实践的消融结果再进入 u6-l4——你会对「衰减—致密化对抗」的平衡有第一手体感。源码预习重点：[train.py:L57-L67](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L57-L67)（Coefficient 的构造与挂载）与 [scene/gaussian_model.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py) 中 `training_setup` 的优化器参数组。
