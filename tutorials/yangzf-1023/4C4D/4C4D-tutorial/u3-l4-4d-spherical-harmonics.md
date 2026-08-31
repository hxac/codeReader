# 4D 球谐：eval_shfs_4d 与时间视角相关颜色

## 1. 本讲目标

学完本讲,你应该能够:

1. 解释 4C4D 如何用「空间球谐 × 时间余弦基」的张量积,让每个 4D 高斯的颜色同时依赖于**观察方向**和**渲染时刻**。
2. 读懂 `utils/sh_utils.py` 中 `eval_sh` 与 `eval_shfs_4d` 的差异,说出 4D 球谐系数的通道布局(索引 0–15 / 16–31 / 32–47 各代表什么)。
3. 对任意 `(gaussian_dim, force_sh_3d, max_sh_degree, max_sh_degree_t)` 组合,手工算出 `get_max_sh_channels` 的返回值,并理解 `sh_channels_4d = [1, 6, 16, 33]` 这张硬编码表的适用范围。
4. 描述 `oneupSHdegree` 的两段进阶策略:先升空间阶数、空间满后再升时间阶数,并推演出训练过程中每 1000 次迭代的激活时间线。

## 2. 前置知识

### 2.1 球谐函数:球面上的「标准频率成分」

一段一维信号可以分解成 sin/cos 的加权和(傅里叶级数);类似地,定义在**球面**上的函数可以分解成一组标准基函数的加权和,这组基就是**球谐函数**(Spherical Harmonics, SH),记作 \(Y_l^m\)。

- 阶数 \(l\) 越高,基函数在球面上「抖动」得越厉害,能表达的细节越锐利。
- \(l\) 阶有 \(2l+1\) 个基,从 0 阶累加到 \(L\) 阶共 \((L+1)^2\) 个基。例如 \(L=3\) 时是 \(1+3+5+7=16\) 个。

### 2.2 3DGS 中的视角相关颜色

3DGS 给每个高斯存一组 SH 系数,把颜色写成观察方向 \(\mathbf{d}\) 的函数:

\[ c(\mathbf{d}) = \sum_{l,m} c_{lm} Y_l^m(\mathbf{d}) \]

其中 0 阶(直流/DC)通道是常数基,代表「平均颜色」;高阶通道编码高光、镜面反射等**随视角变化**的外观。这正是 u3-l1 讲过的 `_features_dc`(1 个通道)与 `_features_rest`(其余通道)的来源。

### 2.3 时间作为第二个自变量

4D 场景中,同一点的颜色不仅随视角变,还随**时间**变。4C4D(继承自 4DGS)的做法是给时间方向配一组**余弦傅里叶基** \(\cos(2\pi k \Delta t / T)\),并与空间球谐做**张量积**:

\[ c(\mathbf{d}, \Delta t) = \sum_{l,m} c_{lm} Y_l^m(\mathbf{d}) + \cos\!\Big(\tfrac{2\pi \Delta t}{T}\Big)\sum_{l,m} c^{(1)}_{lm} Y_l^m(\mathbf{d}) + \cos\!\Big(\tfrac{4\pi \Delta t}{T}\Big)\sum_{l,m} c^{(2)}_{lm} Y_l^m(\mathbf{d}) \]

其中 \(\Delta t = t_g - \tau\) 是「高斯时间中心减渲染时刻」,\(T\) 是 `time_duration` 的长度(入口默认 10,见 u3-l2)。直觉:第一项是与时间无关的基础颜色;第二项让基础颜色随时间做「一个周期的呼吸」;第三项做「两个周期的呼吸」,能表达更快的颜色变化(如闪烁的火苗)。

**为什么用余弦而不是别的时间基?** 余弦基光滑、周期恰好为 \(T\),与 `time_duration` 时间域(承接 u2-l2 的 timestamp 归一化)首尾对齐;实现上也只需一次 `cos` 调用,适合在 CUDA 里逐高斯求值。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [utils/sh_utils.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/sh_utils.py) | 球谐常数为 \(C_0\sim C_4\);`sh_channels_4d` 通道表;`eval_sh`(3D)、`eval_shfs_4d`(4D)两个求值函数;`RGB2SH`/`SH2RGB` 颜色换算 |
| [scene/gaussian_model.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py) | `get_max_sh_channels` 通道数判定、`oneupSHdegree` 渐进进阶、`get_features` 拼接、`create_from_pcd` 的系数张量分配 |
| [train.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py) | 由 `pipe.eval_shfs_4d` 决定 `sh_degree_t`;每 `sh_increase_interval` 步调用一次 `oneupSHdegree` |
| [gaussian_renderer/__init__.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py) | 组装 `GaussianRasterizationSettings`(把激活的阶数传给 CUDA);`convert_SHs_python` 调试分支里调用 `eval_shfs_4d` |
| [diff-gaussian-rasterization/cuda_rasterizer/forward.cu](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu) | CUDA 内置的 `computeColorFromSH_4D`,与 Python 版 `eval_shfs_4d` 逐行同构,是**默认渲染路径** |
| [configs/dynerf/flame_steak.yaml](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml) | 官方配置:`sh_degree: 3`、`convert_SHs_python: False`、`eval_shfs_4d: True` |
| [arguments/__init__.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py) | `PipelineParams.eval_shfs_4d` 开关、`OptimizationParams.sh_increase_interval` 间隔 |

## 4. 核心概念与源码讲解

### 4.1 eval_sh 与 eval_shfs_4d:给颜色加上时间自变量

#### 4.1.1 概念说明

`eval_sh` 是 3DGS 的标准球谐求值:输入阶数 `deg`、系数 `sh`(形状 `[..., C, (deg+1)^2]`)和单位方向 `dirs`(形状 `[..., 3]`),输出每个颜色通道的标量值(`[..., C]`)。它只认**空间方向**这一个自变量。

`eval_shfs_4d` 是它的 4D 扩展:多两个参数 `deg_t`(时间阶数)与 `dirs_t`(时间差 \(\Delta t\)),输出仍是 `[..., C]`。它解决的问题是:**动态场景中,高斯的视角相关外观本身也在随时间演化**——比如火焰既会随观察角度变亮,又会随时间忽明忽暗。把时间基与空间基做张量积,一组系数就能同时表达这两种变化。

两个函数的关系可以概括为:

| | `eval_sh` | `eval_shfs_4d` |
| --- | --- | --- |
| 自变量 | 空间方向 \(\mathbf{d}\) | 空间方向 \(\mathbf{d}\) + 时间差 \(\Delta t\) |
| 通道数 | \((deg+1)^2\) | 由 `get_max_sh_channels` 三分支决定(deg_t>0 时为 \((deg+1)^2(deg_t+1)\)) |
| 最多支持阶数 | 空间 0–4 | 空间 0–3、时间 0–2(实现写死) |
| 在本仓库中的角色 | `force_sh_3d` / 3D 模式与 Python 回退路径 | `convert_SHs_python=True` 时的 Python 回退路径;默认路径是 CUDA 侧同构实现 |

#### 4.1.2 核心流程

`eval_shfs_4d` 的系数通道按「频率块」组织,每一块复用同一套 16 个空间基:

```
输入: deg(空间阶数), deg_t(时间阶数), sh[..., C, K], dirs[..., 3], dirs_t(Δt), l(时间周期 T)

1. 直流项:      result = C0 * sh[..., 0]                        # 基础颜色(常数基)
2. 空间部分:    按 deg 逐阶累加 l1*/l2*/l3* 基 × sh[..., 1..15]   # 与时间无关的视角相关项
3. 若 deg_t>0:  t1 = cos(2π·Δt/T)
                result += t1 × (全部 16 个空间基) × sh[..., 16..31]  # 频率 1 的时间调制
4. 若 deg_t>1:  t2 = cos(2π·2·Δt/T)
                result += t2 × (全部 16 个空间基) × sh[..., 32..47]  # 频率 2 的时间调制
返回 result[..., C]
```

写成公式:

\[ \text{result} = \sum_{k=0}^{deg_t} \cos\!\Big(\frac{2\pi k\, \Delta t}{T}\Big) \cdot \Big(\sum_{l,m} c^{(k)}_{lm} Y_l^m(\mathbf{d})\Big), \qquad k=0 \text{ 时余弦项恒为 } 1 \]

三个值得注意的性质:

- **余弦是偶函数**,所以 \(\cos(2\pi k\Delta t/T)\) 对 \(\Delta t\) 的正负不敏感——「高斯出现在时刻 \(\tau\) 之前」和「之后」产生同样的颜色调制,颜色不区分时间的先后方向,只关心距离。
- **周期恰为 \(T/k\)**,当 \(T\) 等于 `time_duration` 长度时,时间轴首尾颜色平滑衔接。
- **实现写死了 16 个空间基的时间块**:`deg_t>0` 分支无条件引用 `sh[..., 16..31]` 的全部 16 个系数(包含 3 阶基 `l3m3...l3p3`),而这些基变量只在 `deg > 2` 时才被定义。因此 **`deg_t > 0` 隐式要求空间 `deg = 3`**;若 `deg < 3` 且 `deg_t > 0`,Python 版会直接抛 `NameError`。官方配置里 `sh_degree: 3` 正好满足这一约束。

#### 4.1.3 源码精读

**(a) 常数与通道表**——实数球谐基的系数(继承自 PlenOctree),以及本讲的通道表:

```python
C0 = 0.28209479177387814
C1 = 0.4886025119029199
...
sh_channels_4d = [1, 6, 16, 33]
```

这段定义了各阶基函数的归一化常数,`sh_channels_4d` 是 4DGS 遗留的硬编码通道表,稍后在 4.2 详述:[utils/sh_utils.py:L26-L56](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/sh_utils.py#L26-L56)。

**(b) `eval_sh` 的 3D 求值**——按 `deg` 逐阶累加,方向分量 \(x,y,z\) 直接嵌入多项式:

```python
result = C0 * sh[..., 0]
if deg > 0:
    x, y, z = dirs[..., 0:1], dirs[..., 1:2], dirs[..., 2:3]
    result = (result - C1 * y * sh[..., 1] + C1 * z * sh[..., 2] - C1 * x * sh[..., 3])
    ...
```

这就是 3DGS 的标准 SH 求值:0 阶取 DC 系数,1 阶用方向的线性组合乘系数,依此类推:[utils/sh_utils.py:L75-L101](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/sh_utils.py#L75-L101)。

**(c) `eval_shfs_4d` 的空间部分**——与 `eval_sh` 数值等价,但把每个基改写成命名变量(为的是时间块复用它们):

```python
def eval_shfs_4d(deg, deg_t, sh, dirs, dirs_t, l = torch.pi):
    ...
    l0m0 = C0
    result = l0m0 * sh[..., 0]
    if deg > 0:
        l1m1 = -1 * C1 * y;  l1m0 = C1 * z;  l1p1 = -1 * C1 * x
        result = result + l1m1 * sh[..., 1] + l1m0 * sh[..., 2] + l1p1 * sh[..., 3]
    ...
```

注意签名中 `l` 是时间周期参数,默认 `torch.pi`,但训练/渲染管线调用时**总是显式传入** `time_duration` 的长度:[utils/sh_utils.py:L115-L161](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/sh_utils.py#L115-L161)。

**(d) 时间频率块**——`deg_t>0` 时用 `t1 = cos(2π·Δt/T)` 调制全部 16 个空间基,占索引 16–31;`deg_t>1` 时 `t2 = cos(4π·Δt/T)` 占 32–47:

```python
if deg_t > 0:
    t1 = torch.cos(2 * torch.pi * dirs_t / l)
    result = (result +
        t1 * l0m0 * sh[..., 16] + t1 * l1m1 * sh[..., 17] + ... + t1 * l3p3 * sh[..., 31])
    if deg_t > 1:
        t2 = torch.cos(2 * torch.pi * 2 * dirs_t / l)
        result = (result +
            t2 * l0m0 * sh[..., 32] + ... + t2 * l3p3 * sh[..., 47])
```

这段是 4D SH 的核心:同一套空间基被三个时间频率(0、1、2)分别加权一遍:[utils/sh_utils.py:L181-L221](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/sh_utils.py#L181-L221)。

**(e) 渲染端如何调用它**——`render()` 中 `convert_SHs_python=True` 的调试分支:

```python
shs_view = pc.get_features.transpose(1, 2).view(-1, 3, pc.get_max_sh_channels)
...
if pc.gaussian_dim == 3 or pc.force_sh_3d:
    sh2rgb = eval_sh(pc.active_sh_degree, shs_view, dir_pp_normalized)
elif pc.gaussian_dim == 4:
    dir_t = (pc.get_t - viewpoint_camera.timestamp).detach()
    sh2rgb = eval_shfs_4d(pc.active_sh_degree, pc.active_sh_degree_t, shs_view,
                          dir_pp_normalized, dir_t, pc.time_duration[1] - pc.time_duration[0])
colors_precomp = torch.clamp_min(sh2rgb + 0.5, 0.0)
```

三个要点:`get_features` 形状是 `(N, K, 3)` 而 SH 函数期望 `(..., C=3, K)`,所以先 `transpose(1,2)`;`dir_t` 就是 \(\Delta t = t_g - \tau\);`l` 参数传的是时间域长度。最后 `+0.5` 把 SH 空间颜色平移回 RGB 色域并截断负值:[gaussian_renderer/__init__.py:L109-L121](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L109-L121)。这个 `+0.5` 与初始化时的 `RGB2SH(rgb) = (rgb - 0.5)/C0`([utils/sh_utils.py:L225-L226](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/sh_utils.py#L225-L226))互为逆操作——DC 系数取 `RGB2SH(初始颜色)` 后,`C0·sh[0] + 0.5 ≈ 初始颜色`。

**(f) 默认路径其实在 CUDA 里**——官方配置 `convert_SHs_python: False`,此时 `shs = pc.get_features` 直接交给光栅化器,由 CUDA 内置函数求值:

```cpp
__device__ glm::vec3 computeColorFromSH_4D(int idx, int deg, int deg_t, int max_coeffs,
        const glm::vec3* means, glm::vec3 campos, const float* shs, bool* clamped,
        const float* ts, const float timestamp, const float time_duration)
{
    ...
    const float dir_t = ts[idx]-timestamp;      // Δt = 高斯时间中心 - 渲染时刻
    ...
    if (deg_t > 0){
        float t1 = cos(2 * MY_PI * dir_t / time_duration);
        result += t1 * (l0m0 * sh[16] + ... + l3p3 * sh[31]);
        if (deg_t > 1){
            float t2 = cos(2 * MY_PI * dir_t * 2 / time_duration);
            result += t2 * (l0m0 * sh[32] + ... + l3p3 * sh[47]);
        }
    }
    result += 0.5f;
```

它与 Python 版 `eval_shfs_4d` **逐行同构**(同样的索引布局、同样的余弦基),差别只是逐高斯在 GPU 上执行,且 `deg_t` 分支嵌套在 `deg > 2` 内部——同样体现「时间阶数依赖空间 3 阶」的约束:[diff-gaussian-rasterization/cuda_rasterizer/forward.cu:L73-L90](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L73-L90)、[diff-gaussian-rasterization/cuda_rasterizer/forward.cu:L142-L187](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L142-L187)。渲染预处理按 `gaussian_dim`/`force_sh_3d` 分派到 3D 或 4D 版本:[diff-gaussian-rasterization/cuda_rasterizer/forward.cu:L474-L485](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L474-L485)。

**顺带一提**:激活的阶数经由 `GaussianRasterizationSettings` 传给 CUDA——`sh_degree=pc.active_sh_degree, sh_degree_t=pc.active_sh_degree_t, timestamp=..., time_duration=...`([gaussian_renderer/__init__.py:L45-L49](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L45-L49))。也就是说,渐进进阶(4.3)是通过「每次渲染传入当前激活阶数」生效的,系数张量本身始终按最大通道数分配。

#### 4.1.4 代码实践

**实践目标**:用一组假 SH 系数验证 `eval_shfs_4d` 的输入输出形状与时间调制行为。本实践只依赖 PyTorch 与 `utils/sh_utils.py`,**不需要 GPU 和 CUDA 扩展**。

**操作步骤**(以下为示例代码,在仓库根目录保存为 `/tmp/sh4d_check.py` 后执行 `python /tmp/sh4d_check.py`):

```python
# 示例代码
import torch
from utils.sh_utils import eval_shfs_4d, sh_channels_4d

torch.manual_seed(0)
N, C, T = 7, 3, 10.0                      # 7 个高斯、RGB 三通道、时间周期 10

# 1) 通道数对照(见 4.2)
deg = 3
print("deg_t=0 查表分支 :", sh_channels_4d[deg])          # 33
print("deg_t=2 公式分支 :", (deg + 1) ** 2 * (2 + 1))      # 48

# 2) 假系数:只让「频率 1 时间块」携带非零系数
sh = torch.zeros(N, C, 48)
sh[:, :, 0] = 1.0                          # DC 通道:基础色
sh[:, :, 16] = 0.5                          # cos(2πΔt/T) × 常数基 的时间调制

dirs = torch.nn.functional.normalize(torch.randn(N, 3), dim=1)   # 单位方向 (N,3)
dirs_t = torch.linspace(0.0, T, N).unsqueeze(1)                  # Δt 从 0 扫到 T,(N,1)

out = eval_shfs_4d(3, 2, sh, dirs, dirs_t, T)
print("输出形状:", out.shape)              # 期望 (N, C) = (7, 3)
print("红色通道:", out[:, 0])              # 观察 cos 调制:Δt=0 时最大,Δt=T/2 时最小
```

**需要观察的现象**:

1. 输出形状为 `(7, 3)`——`[..., C, K]` 进、`[..., C]` 出。
2. 红色通道数值随 `dirs_t` 呈**一个完整余弦周期**:起点与终点(Δt=0 与 Δt=T)数值相同,中点(Δt=T/2)最小。

**预期结果**(待本地验证):输出形状 `torch.Size([7, 3])`;红色通道在 Δt=0 与 Δt=T 处相等(余弦周期性),在 Δt=T/2 处取到最小值 `C0*(1.0-0.5)≈0.141`。

#### 4.1.5 小练习与答案

**练习 1**:如果把上面实验中的 `deg` 从 3 改成 2(其余不变),会发生什么?为什么?

**答案**:抛出 `NameError`,`l3m3` 未定义。因为 `deg_t > 0` 分支写死引用 16 个空间基(含 3 阶的 `l3m3...l3p3`),而这些变量只在 `deg > 2` 块内定义([utils/sh_utils.py:L163-L200](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/sh_utils.py#L163-L200))。所以时间阶数隐式要求空间阶数为 3。

**练习 2**:时间基为什么用 \(\cos(2\pi k\Delta t/T)\) 而不是 \(\sin\)?这带来什么限制?

**答案**:余弦是偶函数,对 \(\Delta t\) 对称,实现简单且与时间域首尾周期对齐。限制是颜色调制**不区分时间方向**(过去/未来对称),无法表达「先变亮后变暗」这类有方向性的相位行为——这类行为只能靠 \(\Delta t\) 本身的符号信息由别处(如时间高斯的 `get_marginal_t`,见 u3-l3)承担。

**练习 3**:`eval_shfs_4d` 的参数 `l` 如果调用时忘传(用默认 `torch.pi`),而 `time_duration` 长度是 10,会出什么问题?

**答案**:时间基周期变成 \(\pi\) 而数据时间域是 10,余弦的相位与帧号完全错位,时间块系数学到的是「错误频率」的颜色变化,渲染时颜色闪烁异常。管线中 `gaussian_renderer/__init__.py:L120` 显式传 `pc.time_duration[1] - pc.time_duration[0]` 就是为了避免这一点;自己单独调用该函数时必须同样显式传 `T`。

### 4.2 sh_channels_4d 通道表与 get_max_sh_channels

#### 4.2.1 概念说明

每个高斯的 SH 系数张量要按「最大通道数 \(K\)」分配内存(每通道 3 个 float,对应 RGB)。\(K\) 由四个量共同决定:`gaussian_dim`、`force_sh_3d`、`max_sh_degree`、`max_sh_degree_t`。`get_max_sh_channels` 就是这个判定逻辑的唯一权威实现,它同时服务于:

- `create_from_pcd` 分配系数张量;
- `render()` 里 Python 回退路径的 `view(-1, 3, pc.get_max_sh_channels)` 重排;
- 训练日志与张量形状自检。

`sh_channels_4d = [1, 6, 16, 33]` 是继承自 4DGS 的**硬编码表**,只在 `gaussian_dim=4` 且 `max_sh_degree_t=0`(即未开启 `eval_shfs_4d`)时用于查表。它对应的通道数(如 deg=3 时 33)**不是** \((deg+1)^2(deg_t+1)\) 乘积公式算出来的——这是两套不同的 4D SH 参数化约定并存留下的差异,是阅读代码时最容易踩的坑(见下面的「陷阱」)。

#### 4.2.2 核心流程

`get_max_sh_channels` 的三分支判定:

```
若 gaussian_dim == 3 或 force_sh_3d:        K = (max_sh_degree + 1)^2          # 纯 3D SH
否则若 gaussian_dim == 4 且 deg_t == 0:      K = sh_channels_4d[max_sh_degree]   # 4DGS 遗留查表
否则(gaussian_dim == 4 且 deg_t > 0):      K = (max_sh_degree+1)^2 * (deg_t+1) # 张量积公式
```

对照表(每高斯的 SH 系数个数 = K × 3):

| gaussian_dim | force_sh_3d | max_sh_degree | max_sh_degree_t | K | 判定来源 |
| --- | --- | --- | --- | --- | --- |
| 3 | — | 3 | —(忽略) | 16 | \((3+1)^2\) |
| 4 | False | 3 | 0 | **33** | `sh_channels_4d[3]` 查表 |
| 4 | False | 3 | 1 | 32 | \(16\times2\) |
| 4 | False | 3 | 2 | **48** | \(16\times3\) |
| 4 | True | 3 | 任意 | 16 | `force_sh_3d` 走 3D 公式 |

参数量视角:deg=3、deg_t=2 时每个高斯 144 个颜色系数,是 3DGS(48 个)的三倍;致密化到百万级高斯后,SH 系数成为显存大户之一。这也是 4.3 渐进进阶策略存在的现实原因之一。

**陷阱:33 与 16 的不一致**。当 `gaussian_dim=4、deg_t=0` 时,系数张量按 33 通道分配,但渲染求值(CUDA 的 `computeColorFromSH_4D` 与 Python 的 `eval_shfs_4d` 在 `deg_t=0` 时)只消费前 16 个通道(索引 0–15),索引 16–32 的系数**被分配但当前渲染实现不消费、拿不到梯度**。这是 4DGS 原生 4D SH 参数化(通道表 33)与 4C4D 实际渲染实现(张量积)两套约定并存的产物。实践建议:4D 训练要么开 `eval_shfs_4d`(走 48 通道、全部被消费),要么设 `force_sh_3d`(退回纯 3D 的 16 通道);官方 dynerf 配置选择了前者。

#### 4.2.3 源码精读

**(a) 通道数判定**——三个分支覆盖所有合法组合:

```python
@property
def get_max_sh_channels(self):
    if self.gaussian_dim == 3 or self.force_sh_3d:
        return (self.max_sh_degree+1)**2
    elif self.gaussian_dim == 4 and self.max_sh_degree_t == 0:
        return sh_channels_4d[self.max_sh_degree]
    elif self.gaussian_dim == 4 and self.max_sh_degree_t > 0:
        return (self.max_sh_degree+1)**2 * (self.max_sh_degree_t + 1)
```

注意它是 `@property`,引用时不加括号;`force_sh_3d` 优先级最高,把 4D 高斯的颜色强行降回 3D SH:[scene/gaussian_model.py:L235-L242](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L235-L242)。

**(b) 阶数登记**——`__init__` 里同时记录「当前激活」与「上限」两套阶数:

```python
self.active_sh_degree = 0
self.max_sh_degree = sh_degree
...
self.active_sh_degree_t = 0
self.max_sh_degree_t = sh_degree_t
```

激活值从 0 起步,由 4.3 的 `oneupSHdegree` 逐步抬升;上限来自构造参数:[scene/gaussian_model.py:L63-L65](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L63-L65)、[scene/gaussian_model.py:L92-L93](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L92-L93)。

**(c) 上限的来源**——`sh_degree_t` 由 `pipe.eval_shfs_4d` 决定;官方 yaml 里六个 dynerf 场景全部设 `eval_shfs_4d: True`,所以默认 `sh_degree_t=2`:

```python
gaussians = GaussianModel(dataset.sh_degree, gaussian_dim=gaussian_dim, time_duration=time_duration,
                          rot_4d=rot_4d, force_sh_3d=force_sh_3d,
                          sh_degree_t=2 if pipe.eval_shfs_4d else 0, coefficient=coefficient)
```

这是开关名(`eval_shfs_4d`)与参数名(`sh_degree_t`)不对应的隐蔽接线,读代码时容易漏掉:[train.py:L62-L63](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L62-L63);开关默认 False,定义在 [arguments/__init__.py:L69-L78](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L69-L78);yaml 值见 [configs/dynerf/flame_steak.yaml:L11-L32](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml#L11-L32)。

**(d) 通道数如何决定张量形状**——`create_from_pcd` 按它分配系数,DC 通道放初始颜色,其余清零:

```python
features = torch.zeros((fused_color.shape[0], 3, self.get_max_sh_channels)).float().cuda()
features[:, :3, 0 ] = fused_color
features[:, 3:, 1:] = 0.0
...
self._features_dc = nn.Parameter(features[:,:,0:1].transpose(1, 2).contiguous().requires_grad_(True))
self._features_rest = nn.Parameter(features[:,:,1:].transpose(1, 2).contiguous().requires_grad_(True))
```

最终 `_features_dc` 形状 `(N, 1, 3)`、`_features_rest` 形状 `(N, K-1, 3)`,`get_features` 沿通道维 cat 回 `(N, K, 3)`——呼应 u3-l1 的「裸值存储」与 4.1.3(e) 的 `transpose(1,2)`:[scene/gaussian_model.py:L406-L438](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L406-L438)、[scene/gaussian_model.py:L225-L229](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L225-L229)。另注意 `_features_rest` 的学习率是 `feature_lr / 20`([scene/gaussian_model.py:L485-L487](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L485-L487)):高频系数学得比 DC 慢 20 倍,与渐进进阶互为补充。

**(e) 激活阶数参与持久化**——`capture`/`restore` 元组把两个激活阶数一并存入 checkpoint(4D 分支中 `active_sh_degree` 在首位、`active_sh_degree_t` 在末尾),保证续训/推理时渐进进阶进度不丢:[scene/gaussian_model.py:L116-L139](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L116-L139)、[scene/gaussian_model.py:L157-L178](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L157-L178)。同时 `save_ply` 只导出「已激活」的 `f_rest` 通道数([scene/gaussian_model.py:L362-L367](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L362-L367)),所以早停模型的 ply 文件 f_rest 字段数会比最大通道数少——加载时需与 `load_ply` 的断言匹配(详见 u3-l5)。

#### 4.2.4 代码实践

**实践目标**:按规格完成通道数对照计算——`max_sh_degree=3` 且 `max_sh_degree_t=0/2` 时 `get_max_sh_channels` 的返回值,并与 `sh_channels_4d` 表和乘积公式互相印证。

**操作步骤**:

1. 纯查表验证(无需任何扩展,只需 Python):

```python
# 示例代码:手工复现 get_max_sh_channels 三分支
sh_channels_4d = [1, 6, 16, 33]

def max_channels(gaussian_dim, force_sh_3d, deg, deg_t):
    if gaussian_dim == 3 or force_sh_3d:
        return (deg + 1) ** 2
    elif gaussian_dim == 4 and deg_t == 0:
        return sh_channels_4d[deg]
    elif gaussian_dim == 4 and deg_t > 0:
        return (deg + 1) ** 2 * (deg_t + 1)

print(max_channels(4, False, 3, 0))   # 33:deg_t=0 走查表分支
print(max_channels(4, False, 3, 2))   # 48:deg_t=2 走公式分支,16*3
print(max_channels(4, True,  3, 2))   # 16:force_sh_3d 压回 3D 公式
print(max_channels(3, False, 3, 5))   # 16:3D 模式忽略 deg_t
```

2. (可选,需已编译 simple-knn 与 diff-gaussian-rasterization 扩展;无需 GPU 数据)直接实例化模型对拍:

```python
# 示例代码:与真实实现对拍(需要环境,待本地验证)
from scene.gaussian_model import GaussianModel
m = GaussianModel(sh_degree=3, gaussian_dim=4, time_duration=[0, 10],
                  rot_4d=True, force_sh_3d=False, sh_degree_t=2)
print(m.get_max_sh_channels)          # 期望 48
```

注意 `GaussianModel.__init__` 末尾会调用 `setup_functions`,其引用的 `distCUDA2` 等来自 CUDA 扩展,所以这一步要求 u1-l2 的环境已就绪;查表本身是纯 Python。

**需要观察的现象**:查表分支返回 33(不是 \(16\times1=16\)),公式分支返回 48;`force_sh_3d` 一票否决回到 16。

**预期结果**(待本地验证):`33 / 48 / 16 / 16`。若第 2 步可运行,`m.get_max_sh_channels` 输出 `48`。

#### 4.2.5 小练习与答案

**练习 1**:`sh_channels_4d = [1, 6, 16, 33]` 中,deg=1 对应 6,而 \((1+1)^2 = 4\)。多出来的 2 个通道在 `eval_shfs_4d` 的求值路径中会被消费吗?

**答案**:不会。`eval_shfs_4d`(及其 CUDA 同构版本)在 `deg_t=0` 时只消费索引 \(0 \sim (deg+1)^2-1\);这张表是 4DGS 原生参数化的遗留约定,只在 `deg_t=0` 的分配分支使用。超出 \((deg+1)^2\) 的通道被分配但不参与求值(见 4.2.2 的陷阱)。

**练习 2**:为什么 `force_sh_3d` 要放在判定最前面、优先于 `gaussian_dim`?

**答案**:`force_sh_3d` 的语义就是「虽然是 4D 高斯(有 `_t`、`_scaling_t`),但颜色退化为纯 3D SH」——几何仍是 4D 的,外观不随时间变。它让「运动」与「变色」可以解耦:某些场景想用 4D 几何但不需要时间相关颜色时,可省下 3 倍颜色系数并避免时间外观过拟合([scene/gaussian_model.py:L236-L238](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L236-L238))。

**练习 3**:配置里 `sh_degree: 3`、`eval_shfs_4d: True`,训练中每个高斯颜色参数占多少 float?比 3DGS 多几倍?

**答案**:\(K \times 3 = 48 \times 3 = 144\) 个;3DGS 是 \(16 \times 3 = 48\) 个,恰为 3 倍。

### 4.3 oneupSHdegree:两段进阶策略

#### 4.3.1 概念说明

高阶 SH 系数表达高频细节,但**过早放开**会让优化在几何尚未成形时,就用高频颜色「糊弄」重建误差(过拟合视角相关噪声),这是 3DGS 就有的经典问题。4C4D 沿用「渐进进阶」:训练从 0 阶(只有 DC,纯平均色)开始,每隔固定步数把激活阶数抬一档。

4D 场景多了一层:**阶数有两个维度**(空间 `active_sh_degree` 与时间 `active_sh_degree_t`)。`oneupSHdegree` 采用「先空间、后时间」的两段策略——先把空间方向的外观学满,再放开时间频率。直觉是:空间上的视角相关外观(高光等)是「静态底色」的一部分,应先于「随时间闪烁」的高频成分被学好;同时这也与 4.1 的实现约束一致——时间块引用全部 16 个空间基,只有空间 3 阶全部激活后,时间块的梯度才有完整意义。

#### 4.3.2 核心流程

```
每 sh_increase_interval(默认 1000)次迭代调用一次 oneupSHdegree():

if active_sh_degree < max_sh_degree:        # 第一段:空间阶数未满
    active_sh_degree += 1                    #   优先升空间
elif max_sh_degree_t and active_sh_degree_t < max_sh_degree_t:   # 第二段:空间已满且时间上限非 0
    active_sh_degree_t += 1                  #   才升时间
```

两个细节:

- `elif` 保证同一次调用**只升一档**,且时间阶数永不在空间未满时上升。
- `self.max_sh_degree_t and ...` 利用了 Python 的短路:若时间上限为 0(未开 `eval_shfs_4d`),整个第二段直接跳过。

以官方默认(`sh_degree: 3`、`eval_shfs_4d: True` → `sh_degree_t=2`、`sh_increase_interval: 1000`)为例的激活时间线:

| 迭代号(触发时) | active_sh_degree | active_sh_degree_t | 本次渲染实际消费通道 \((d+1)^2(d_t+1)\) |
| --- | --- | --- | --- |
| 1–999 | 0 | 0 | 1(仅 DC) |
| 1000 | 1 | 0 | 4 |
| 2000 | 2 | 0 | 9 |
| 3000 | 3 | 0 | 16(空间学满) |
| 4000 | 3 | 1 | 32(开始学时间频率 1) |
| 5000 | 3 | 2 | 48(全部放开) |
| ≥6000 | 3 | 2 | 48(不再变化) |

注意「实际消费通道」在 3000 步前等于 \((d+1)^2\),与 `deg_t=0` 的查表通道 33 并不相同——渐进进阶看的是**激活阶数**,不是分配上限;分配始终按 `get_max_sh_channels`(48)一次性完成。

#### 4.3.3 源码精读

**(a) 进阶本体**——四行实现两段策略:

```python
def oneupSHdegree(self):
    if self.active_sh_degree < self.max_sh_degree:
        self.active_sh_degree += 1
    elif self.max_sh_degree_t and self.active_sh_degree_t < self.max_sh_degree_t:
        self.active_sh_degree_t += 1
```

没有 else:两个上限都到达后调用是无操作,所以训练循环不必额外判断:[scene/gaussian_model.py:L400-L404](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L400-L404)。

**(b) 训练循环的触发点**——每隔 `sh_increase_interval` 步调用一次,发生在每次迭代的 `update_learning_rate` 之后、渲染之前:

```python
gaussians.update_learning_rate(iteration)

# Every 1000 its we increase the levels of SH up to a maximum degree
if iteration % opt.sh_increase_interval == 0:
    gaussians.oneupSHdegree()
```

`sh_increase_interval` 是 `OptimizationParams` 的成员,默认 1000,可被 yaml 或命令行覆盖(承接 u1-l4 的合并优先级):[train.py:L117-L123](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L117-L123)、[arguments/__init__.py:L105](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L105)。

**(c) 激活阶数如何生效**——每次渲染,激活值被写进光栅化配置传给 CUDA:

```python
raster_settings = GaussianRasterizationSettings(
    ...
    sh_degree=pc.active_sh_degree,
    sh_degree_t=pc.active_sh_degree_t,
    timestamp=viewpoint_camera.timestamp,
    time_duration=pc.time_duration[1]-pc.time_duration[0],
    ...
)
```

CUDA 的 `computeColorFromSH_4D` 收到的 `deg/deg_t` 就是这两个激活值,未激活的高阶系数在求值中被完全跳过(不产生前向贡献,也就没有反向梯度):[gaussian_renderer/__init__.py:L36-L55](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L36-L55);Python 回退路径同理直接传 `pc.active_sh_degree, pc.active_sh_degree_t`([gaussian_renderer/__init__.py:L117-L120](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L117-L120))。

**(d) 与致密化的交互**——clone/split 复制高斯时,`_features_rest` 整块按 `get_max_sh_channels-1` 个通道复制,与激活进度无关;新升高档后,新旧高斯的高阶系数即刻一起参与优化([scene/gaussian_model.py:L689](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L689)、[scene/gaussian_model.py:L732](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L732))。

#### 4.3.4 代码实践

**实践目标**:推演默认配置下 30000 次迭代中激活阶数的时间线,并验证 `oneupSHdegree` 在两个上限都到达后退化为无操作。

**操作步骤**:

1. 阅读型推演(纸面完成):对照 4.3.2 的表格,写出 iteration = 1000, 3000, 5000, 7000, 30000 时的 `(active_sh_degree, active_sh_degree_t)`。
2. 代码验证(无需 GPU,只需 PyTorch;若 `scene.gaussian_model` 因 CUDA 扩展缺失无法 import,则用下面的等价复现):

```python
# 示例代码:复现两段进阶逻辑并模拟 30000 步
class SH:
    def __init__(self, max_deg, max_deg_t):
        self.active_sh_degree, self.max_sh_degree = 0, max_deg
        self.active_sh_degree_t, self.max_sh_degree_t = 0, max_deg_t
    def oneup(self):   # 逐行对照 gaussian_model.py:L400-L404
        if self.active_sh_degree < self.max_sh_degree:
            self.active_sh_degree += 1
        elif self.max_sh_degree_t and self.active_sh_degree_t < self.max_sh_degree_t:
            self.active_sh_degree_t += 1

sh = SH(max_deg=3, max_deg_t=2)             # 官方 dynerf 配置
marks = {}
for it in range(1, 30001):
    if it % 1000 == 0:
        sh.oneup()
        marks[it] = (sh.active_sh_degree, sh.active_sh_degree_t)
print(marks[1000], marks[3000], marks[5000], marks[7000], marks[30000])
```

3. (可选)在真实训练里观察:开启 TensorBoard,对照 `train.py` 每 1000 步的进阶点,观察 loss 曲线在 3000 与 4000 步附近是否出现小幅抬升(新放开的高频系数从 0 开始学习)。

**需要观察的现象**:第 1000 步 `(1,0)`,第 3000 步 `(3,0)`,第 5000 步 `(3,2)`,第 7000 步与第 30000 步均为 `(3,2)`——进阶在第 5000 步后完全停止。

**预期结果**(待本地验证):`(1, 0) (3, 0) (3, 2) (3, 2) (3, 2)`。若第 3 步可执行,loss 曲线在进阶点附近的微小波动属于正常现象(新系数冷启动)。

#### 4.3.5 小练习与答案

**练习 1**:把 `sh_increase_interval` 从 1000 改成 500,`sh_degree=3`、`sh_degree_t=2` 时全部通道在第几步放开?若 iterations 只有 6000,有什么风险?

**答案**:需要 5 次进阶(空间 3 次 + 时间 2 次),即第 2500 步全部放开。若总迭代只有 6000,高频时间系数只训练约 3500 步,可能欠拟合;反之间隔太大又会导致后期才放开、高频细节来不及学。间隔应与总迭代数匹配。

**练习 2**:`max_sh_degree_t=0` 时第 5000 步调用 `oneupSHdegree` 会发生什么?

**答案**:什么也不变。`elif self.max_sh_degree_t and ...` 中 `max_sh_degree_t=0` 为假值直接短路,空间阶数又已满,函数无操作——这正是「未开 `eval_shfs_4d` 就没有时间进阶」的机制来源。

**练习 3**:为什么不把「升空间」和「升时间」放在同一次调用里一起做(比如 3000 步同时升到 `(3,1)`)?

**答案**:一次只放开一个「频率维度」是渐进优化的稳健性考量:每步新增的待学参数少,优化目标变化平缓;同时实现上 `elif` 保证了调用语义简单(无状态机)。此外时间块复用全部 16 个空间基,空间系数还在大幅变动时同步学时间调制,容易把空间误差错吸收进时间系数。

## 5. 综合实践

**任务:写一个「4D SH 通道手册」小脚本,把三个模块串起来。**

在仓库根目录写一个只依赖 PyTorch 的脚本(示例代码),完成三件事并输出一份对照报告:

1. **通道矩阵**:用 4.2.4 的 `max_channels` 复现函数,枚举 `gaussian_dim ∈ {3,4}` × `force_sh_3d ∈ {False,True}` × `deg ∈ {0..3}` × `deg_t ∈ {0..2}` 的全部合法组合,打印 `K` 与每高斯颜色参数量 `3K`(共约 32 行;`gaussian_dim=3` 时跳过 `deg_t>0` 的重复行)。
2. **时间调制曲线**:取 `deg=3, deg_t=2, T=10`,固定一个单位方向,令 `sh[:, :, 16]` 与 `sh[:, :, 32]` 非零,在 `Δt ∈ [0, 10]` 上均匀采样 100 点调用 `eval_shfs_4d`,分别画出只开频率 1、只开频率 2、两者同开的输出曲线(可用 matplotlib,或直接打印每 10 个采样点的数值表),验证周期 \(T\) 与 \(T/2\)。
3. **进阶时间线**:用 4.3.4 的 `SH` 类模拟 30000 步、间隔 1000,输出「迭代 → 激活阶数 → 本次实际消费通道数 \((d+1)^2(d_t+1)\)」的完整表格,并标出全部通道放开的迭代号。

**验收标准**:第 1 部分能指出 `(4, False, 3, 0) → 33` 与 `(4, False, 3, 2) → 48` 两个关键值及前者「分配 33、消费 16」的陷阱;第 2 部分曲线在 \(\Delta t=0\) 与 \(\Delta t=T\) 处相等;第 3 部分在 5000 步后激活阶数冻结为 `(3, 2)`。全流程无需 GPU;涉及运行结果的数值标注「待本地验证」。

## 6. 本讲小结

- 4D 球谐 = **空间球谐 × 时间余弦傅里叶基**的张量积:`eval_shfs_4d` 把通道按频率块组织,索引 0–15 是与时间无关的空间 SH,16–31 是 \(\cos(2\pi\Delta t/T)\) 调制的时间频率 1 块,32–47 是 \(\cos(4\pi\Delta t/T)\) 的频率 2 块。
- 默认渲染路径是 CUDA 内置的 `computeColorFromSH_4D`(与 Python 版逐行同构);`eval_shfs_4d` 是 `convert_SHs_python=True` 时的调试/回退实现,官方配置默认关闭。
- `get_max_sh_channels` 三分支决定系数张量宽度:3D 或 `force_sh_3d` 走 \((d+1)^2\);4D 且 `deg_t=0` 走 4DGS 遗留表 `sh_channels_4d=[1,6,16,33]`(33 通道中渲染只消费前 16);4D 且 `deg_t>0` 走 \((d+1)^2(d_t+1)\),官方配置(3,2)为 48。
- `sh_degree_t` 由 `pipe.eval_shfs_4d` 开关决定(`True → 2`),这是开关名与参数名不一致的隐蔽接线;时间块引用全部 16 个空间基,因此 **`deg_t>0` 隐式要求空间 `deg=3`**。
- `oneupSHdegree` 的两段进阶:先升空间阶数(每 `sh_increase_interval` 步一档),空间满后再升时间阶数;激活值经 `GaussianRasterizationSettings` 传入 CUDA,未激活通道不参与求值、无梯度,激活进度随 `capture`/`restore` 持久化。
- 颜色初始化与求值通过 `RGB2SH`/`+0.5` 互逆衔接,`_features_rest` 学习率是 `feature_lr/20`,高频系数天然学得更慢。

## 7. 下一步学习建议

- **下一讲 u3-l5(初始化与持久化)**:看 `create_from_pcd` 如何用本讲的 `get_max_sh_channels` 分配系数张量,以及 `save_ply`/`load_ply` 中 f_rest 字段数与激活阶数的对应关系。
- **单元 4(u4-l1、u4-l3)**:本讲的 `render()` 调用点将在渲染管线里展开——重点看 `GaussianRasterizationSettings` 的完整字段与 `convert_SHs_python`/`compute_cov3D_python` 两条 Python 回退路径如何配合 `eval_shfs_4d` 与时间边缘化。
- **延伸阅读**:对照 [diff-gaussian-rasterization/cuda_rasterizer/forward.cu:L73-L195](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L73-L195) 与 [backward.cu](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu) 中反向版本的同名函数,理解 `clamped` 标志如何在反向传播中处理被截断的负颜色。
