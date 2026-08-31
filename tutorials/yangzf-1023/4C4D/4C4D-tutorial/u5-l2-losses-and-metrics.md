# u5-l2 损失函数与评估指标

## 1. 本讲目标

上一讲（u5-l1）我们建立了训练主循环的五阶段骨架：render → loss → backward → densify → step。本讲把放大镜对准其中的 **loss 阶段**，读完本讲你应该能够：

1. 写出 4C4D 的总损失公式 \(\mathcal{L}=(1-\lambda)\,L_1+\lambda\,(1-\mathrm{SSIM})\)，并解释 \(\lambda\)（`lambda_dssim`）如何权衡「逐像素保真」与「结构相似」。
2. 逐行读懂 `l1_loss`、纯 PyTorch 版 `ssim`、CUDA 版 `fused_ssim` 三段真实源码，说清楚为什么训练用 CUDA 版而不用纯 PyTorch 版。
3. 手工推导并核对 `psnr` 的计算（含 `psnr` 函数按通道 reshape 的形状细节），分清训练进度条 PSNR 与测试评估 PSNR 两个不同口径。
4. 看懂 `training_report` 写了哪些 TensorBoard 曲线、在哪些相机上评估、以及它为什么是 best checkpoint 的唯一判据。

## 2. 前置知识

### 2.1 逐像素距离：L1 与 L2

比较两张同尺寸图像最直接的办法是逐像素求差。设渲染图 \(p\) 与真值图 \(g\) 各有 \(N\) 个像素（4C4D 中形状为 `(3, H, W)`，\(N=3HW\)）：

\[ L_1 = \frac{1}{N}\sum_{i=1}^{N}|p_i - g_i|, \qquad L_2 = \frac{1}{N}\sum_{i=1}^{N}(p_i - g_i)^2 \]

L2 对离群像素（差值大的点）按平方放大惩罚，L1 只线性增长，更稳健。3DGS/4DGS 一脉的传统是 **L1 + SSIM** 的组合，而不是 L2。

### 2.2 结构相似性：SSIM 的直觉

PSNR/L1 把图像当作一堆独立数字，而人眼对「结构」（边缘、纹理、明暗分布）比对绝对像素值更敏感。SSIM（Structural Similarity）在每个局部窗口内比较三件事：

- **亮度**：窗口均值 \(\mu_x, \mu_y\) 是否接近；
- **对比度**：窗口方差 \(\sigma_x^2, \sigma_y^2\) 是否接近；
- **结构**：协方差 \(\sigma_{xy}\) 衡量两者的相关程度（像素共同涨落）。

标准 SSIM 在窗口 \(x,y\) 上的定义为（\(C_1=0.01^2,\ C_2=0.03^2\) 是防止分母为零的稳定常数，动态范围取 1）：

\[ \mathrm{SSIM}(x,y)=\frac{(2\mu_x\mu_y+C_1)(2\sigma_{xy}+C_2)}{(\mu_x^2+\mu_y^2+C_1)(\sigma_x^2+\sigma_y^2+C_2)} \]

SSIM 取值范围约在 \([-1,1]\)，两张一样的图得 1。实践中用 11×11、\(\sigma=1.5\) 的高斯窗口在整张图上滑动，对每个窗口算一个 SSIM 再取平均。

### 2.3 PSNR：对数尺度的误差

PSNR（Peak Signal-to-Noise Ratio）定义为：

\[ \mathrm{PSNR} = 20\log_{10}\frac{I_{\max}}{\sqrt{\mathrm{MSE}}} \]

4C4D 的图像归一化到 \([0,1]\)，峰值 \(I_{\max}=1\)，于是 \(\mathrm{PSNR}=-10\log_{10}\mathrm{MSE}\)，单位是分贝（dB）。MSE 每缩小 10 倍，PSNR 提高 10 dB。新视角合成论文里报告的 30 dB、35 dB 指的就是它。

### 2.4 autograd.Function：自定义前向/反向

u4-l2 已经见过 `_RasterizeGaussians` 这个 `torch.autograd.Function` 子类：它不做数值计算，只负责组织参数、缓存中间量、前向调 CUDA、反向回传梯度。本讲的 `FusedSSIMMap` 是同一模式的又一个例子，规模小得多，正适合当作「最小可读的 autograd.Function」。

### 2.5 EMA 与 TensorBoard

- **EMA**（指数移动平均）用平滑系数 \(\alpha\) 衰减历史：\(\bar{x}_t=\alpha x_t+(1-\alpha)\bar{x}_{t-1}\)，让抖动的逐迭代指标变成可读的曲线。4C4D 进度条取 \(\alpha=0.4\)。
- **TensorBoard** 的 `SummaryWriter.add_scalar(tag, value, step)` 把标量写到事件文件，训练完成后 `tensorboard --logdir <model_path>` 即可看曲线。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
| --- | --- |
| [train.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py) | 损失的组合与调用现场：`Ll1`/`Lssim`/`loss` 三行（L143–L148）、进度条 EMA（L188–L216）、`training_report` 的定义（L305–L367）与调用（L222–L232） |
| [utils/loss_utils.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/loss_utils.py) | `l1_loss`/`l2_loss` 与 3DGS 原版纯 PyTorch `ssim`（后者训练循环已弃用，但评估代码仍在用） |
| [utils/image_utils.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/image_utils.py) | `mse`/`psnr` 两个指标函数 |
| [fused-ssim-main/fused_ssim/__init__.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/fused-ssim-main/fused_ssim/__init__.py) | CUDA 版 SSIM 的 Python 包装：`FusedSSIMMap`（autograd.Function）与入口 `fused_ssim` |
| [fused-ssim-main/ext.cpp](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/fused-ssim-main/ext.cpp) | pybind11 登记表：把 `fusedssim`/`fusedssim_backward` 两个 C++ 函数注册给 Python |
| [arguments/__init__.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py) | `lambda_dssim = 0.2` 默认值（L95） |

一条阅读主线：**train.py 的三行损失 → 三个被 import 的函数 → 各自的实现文件**。train.py 顶部的三行 import 就是本讲的地图索引：

- [train.py:L18](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L18) 引入 `l1_loss`，行尾注释 `#, ssim, msssim` 是 3DGS 遗留痕迹——原版纯 PyTorch SSIM 在这里被注释掉了；
- [train.py:L25](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L25) 引入 `psnr`；
- [train.py:L36](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L36) 引入 CUDA 版 SSIM 并改名 `fast_ssim`。

## 4. 核心概念与源码讲解

### 4.1 l1_loss：逐像素平均绝对误差

#### 4.1.1 概念说明

`l1_loss` 是总损失中的「像素保真」项：它不关心结构，只要求渲染图在每个像素、每个通道上都贴近真值。它是三项损失中最便宜、梯度最稳定的一项（处处有梯度 ±1/N），承担着把整体颜色和亮度拉到正确范围的作用；SSIM 项则补充它对边缘和纹理不敏感的短板。两者的分工在 4.3 节合并讨论。

#### 4.1.2 核心流程

```text
输入: image (3,H,W) 渲染图（render 返回的 "render" 张量）
      gt_image (3,H,W) 真值图（DataLoader 读入、已在 [0,1]）
1. 逐元素相减 → (3,H,W)
2. 逐元素取绝对值
3. 对所有元素取均值 → 标量
```

#### 4.1.3 源码精读

[l1_loss 与 l2_loss 的全部实现](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/loss_utils.py#L21-L25)只有两行，一个负责绝对值、一个负责平方：

```python
def l1_loss(network_output, gt):
    return torch.abs((network_output - gt)).mean()

def l2_loss(network_output, gt):
    return ((network_output - gt) ** 2).mean()
```

注意两点：

- 这里**没有**用 `torch.nn.L1Loss`，而是手写张量表达式。数值上等价（`mean` 对所有元素取平均，即 \(\frac{1}{3HW}\)），且少一层模块包装。
- `l2_loss` 在 4C4D 中没有任何调用点，属于 3DGS 模板带来的备用函数。

调用现场在训练循环内：[train.py:L143](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L143) `Ll1 = l1_loss(image, gt_image)`，其中 `image` 是 [train.py:L139](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L139) 从 `render_pkg` 解包出的 `(3,H,W)` 张量。

L1 与 L2 的敏感度差异可以用一个思想实验记住：若一张 \(N\) 像素的图中只有 1 个像素差 \(\delta=0.5\)，则 \(L_1=0.5/N\)、\(L_2=0.25/N\)；把差值放大到 \(\delta=1\)，\(L_1\) 翻倍、\(L_2\) 翻四倍。L2 会被少量离群像素（例如高斯边缘的过曝尖峰）牵着走，L1 不会。

#### 4.1.4 代码实践

**实践目标**：验证 `l1_loss` 的数值口径，并直观感受 L1 对离群像素的稳健性。

**操作步骤**（示例代码，可在任意有 PyTorch 的环境运行，CPU 即可）：

```python
import torch
from utils.loss_utils import l1_loss, l2_loss

torch.manual_seed(0)
gt = torch.rand(3, 64, 64)          # 模拟真值图，取值 [0,1]
img = gt + 0.05                      # 每个像素整体偏移 0.05
print("L1 =", l1_loss(img, gt).item())   # 期望 0.05
print("L2 =", l2_loss(img, gt).item())   # 期望 0.0025

# 制造 1 个离群像素
img2 = gt.clone(); img2[0, 0, 0] += 0.5
print("L1(outlier) =", l1_loss(img2, gt).item())   # ≈ 0.5/N
print("L2(outlier) =", l2_loss(img2, gt).item())   # ≈ 0.25/N
```

**需要观察的现象**：整体偏移 0.05 时 L1 恰为 0.05；单个离群像素只让 L1 增加 \(0.5/12288\approx4\times10^{-5}\)，而 L2 增加 \(0.25/12288\)。

**预期结果**：L1 精确等于 0.05（所有像素差相同）；离群实验中 L2 的增量是 L1 增量的 100 倍（0.25/N 对 0.5/N 的比值随 \(\delta\) 平方放大）。本环境无 GPU，但此实践只依赖 CPU 版 PyTorch，逻辑上可复现；运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`l1_loss(image, gt_image)` 的结果与 `torch.nn.L1Loss()(image, gt_image)` 是否相等？为什么？

**答案**：相等。`nn.L1Loss` 默认 `reduction='mean'`，对全部元素取绝对值后平均，与手写 `torch.abs(x - y).mean()` 在 `(3,H,W)` 输入上逐元素一致。

**练习 2**：若把 `image` 与 `gt_image` 的形状改成 `(1,3,H,W)`（带 batch 维），`l1_loss` 的返回值会变吗？

**答案**：不变。`mean()` 对所有元素求平均，元素总数 \(3HW\) 相同，batch 维不改变均值。

**练习 3**：渲染图偶尔有像素超出 \([0,1]\)（球谐求值加 0.5 后未 clamp），`l1_loss` 对这些超范围像素如何处理？

**答案**：照常参与计算——`l1_loss` 不做任何裁剪，差值直接取绝对值。这也是为什么训练损失可以惩罚「过白/过黑」的像素，而测试评估（4.4 节）会先 `torch.clamp(image, 0, 1)` 再算指标，两者口径不同。

### 4.2 fused_ssim：CUDA 版结构相似性

#### 4.2.1 概念说明

SSIM 项负责「结构」：它在一个 11×11 的高斯窗口内比较均值、方差与协方差，让渲染图在边缘锐度、纹理分布上贴近真值。`loss_utils.py` 里保留着 3DGS 原版的纯 PyTorch 实现（5 次 `F.conv2d`），但 4C4D 的训练循环弃用了它，改用 fused-ssim 子包的 CUDA 实现——即 u1-l2 介绍过的四个 CUDA 扩展之一。原因很直接：**SSIM 处在训练热路径上**，每次迭代、每个 batch 视角都要算一次，3 万次迭代累计调用数万次；而纯 PyTorch 版每次调用都要为 5 次卷积构建 autograd 图、生成多个全尺寸中间张量，反向还要重放这些卷积。

#### 4.2.2 核心流程

纯 PyTorch 路径（3DGS 原版，`loss_utils.ssim`）：

```text
1. gaussian(11, 1.5) 造 1D 高斯核，外积成 11×11 2D 窗口
2. 5 次分组卷积: mu1, mu2, E[x²], E[y²], E[xy]
3. 由卷积结果组装 σ1², σ2², σ12（每个都是全尺寸中间张量）
4. 代入 SSIM 公式得 ssim_map，再 .mean()
反向: autograd 沿 5 条卷积链自动回放
```

CUDA fused 路径（4C4D 训练实际使用）：

```text
train.py: fast_ssim(img.unsqueeze(0), gt.unsqueeze(0))     # 包装层，(1,3,H,W)
  └─ FusedSSIMMap.apply(C1, C2, img1, img2, padding, train)  # autograd.Function
       ├─ forward:  fusedssim(C1,C2,img1,img2,train)          # 单个 CUDA kernel
       │            返回 ssim_map + 3 个预计算偏导 dm_dmu1/dm_dsigma1_sq/dm_dsigma12
       └─ backward: fusedssim_backward(..., dL_dmap, 3 个偏导) # 单个 CUDA kernel
  └─ map.mean()                                              # 返回标量
```

#### 4.2.3 源码精读

**（a）3DGS 原版纯 PyTorch SSIM**（训练已弃用，用于对照）。[create_window](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/loss_utils.py#L27-L35) 用 \(\sigma=1.5\) 的 1D 高斯核外积出 11×11 窗口；[_ssim 的统计量计算](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/loss_utils.py#L47-L62) 是 2.2 节公式的直译：

```python
mu1 = F.conv2d(img1, window, padding=window_size // 2, groups=channel)
...
sigma1_sq = F.conv2d(img1 * img1, window, ...) - mu1_sq
sigma12   = F.conv2d(img1 * img2, window, ...) - mu1_mu2
C1, C2 = 0.01 ** 2, 0.03 ** 2
ssim_map = ((2*mu1_mu2 + C1)*(2*sigma12 + C2)) / ((mu1_sq+mu2_sq+C1)*(sigma1_sq+sigma2_sq+C2))
```

注意 `img1 * img1`、`img2 * img2`、`img1 * img2` 三个全尺寸临时张量与 5 次卷积全部留在 autograd 图里——这正是 fused 版要消灭的开销。这一版并未被删除：[utils/mesh_utils.py:L20](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/mesh_utils.py#L20) 仍 `from utils.loss_utils import ssim`，供 render.py 推理路线的 `GaussianExtractor` 评估使用（详见 u7-l3）；同一份代码库里「训练用 CUDA 版、离线评估用纯 PyTorch 版」并存。

**（b）fused-ssim 的 Python 包装层**。[按硬件后端三选一加载](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/fused-ssim-main/fused_ssim/__init__.py#L5-L10)：CUDA → `fused_ssim_cuda`，Apple MPS → `fused_ssim_mps`，Intel XPU → `fused_ssim_xpu`。若三者皆无（纯 CPU 环境），`fusedssim` 名字根本不会被定义，`from fused_ssim import fused_ssim` 虽能成功（模块本身可导入），但一调用就报 `NameError`——这与 diff-gaussian-rasterization 一样是「必须有 GPU 扩展」的子包。

[FusedSSIMMap.forward](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/fused-ssim-main/fused_ssim/__init__.py#L15-L28) 的关键设计是在前向就**预计算反向所需偏导**：

```python
ssim_map, dm_dmu1, dm_dsigma1_sq, dm_dsigma12 = fusedssim(C1, C2, img1, img2, train)
ctx.save_for_backward(img1.detach(), img2, dm_dmu1, dm_dsigma1_sq, dm_dsigma12)
```

`dm_dmu1` 等三个张量是 SSIM map 对 \(\mu_1\)、\(\sigma_1^2\)、\(\sigma_{12}\) 的偏导数。于是 [backward](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/fused-ssim-main/fused_ssim/__init__.py#L30-L39) 不需要重建任何卷积图，直接把链式法则的两步（上游梯度 × 局部偏导 × 窗口平滑）在一个 kernel 里做完：

```python
grad = fusedssim_backward(C1, C2, img1, img2, dL_dmap, dm_dmu1, dm_dsigma1_sq, dm_dsigma12)
return None, None, grad, None, None, None
```

返回的 6 个值对应 forward 的 6 个参数，只有第 3 位（`img1`，渲染图）有梯度——与子包 README 写明的约束一致：「只允许第一个图像可微」。真值图不需要梯度，连中间量都不必为它保留。

[入口函数 fused_ssim](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/fused-ssim-main/fused_ssim/__init__.py#L41-L49) 只做三件事：硬编码 \(C_1=0.01^2, C_2=0.03^2\)、校验 `padding` 合法并调用 `apply`、对 ssim_map 取均值返回标量。`padding="valid"` 会把 ssim_map 的边界各裁掉 5 像素（11×11 窗口的半径），对应 pytorch-msssim 的口径；`train=False` 跳过偏导计算，推理更快。

**（c）绑定层**。[ext.cpp 的全部登记](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/fused-ssim-main/ext.cpp#L4-L6) 只有 3 行 `m.def`——与 u4-l2 的光栅化绑定层完全同构，只是这里只注册 `fusedssim` 和 `fusedssim_backward` 两个入口。真正的数学在 `ssim.cu` 的 CUDA kernel 里，本讲不展开（方法同 u8-l1 的 CUDA 阅读法）。

**（d）为什么 fused 版快**。[fused-ssim README](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/fused-ssim-main/README.md#L3-L8) 列了五点结构性优化，并在 [Performance 一节](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/fused-ssim-main/README.md#L205-L206) 声称比此前最快的可微实现 pytorch-msssim 快 5–8×：

1. SSIM 的卷积是空间局部的，可以完全融合（fully-fused），中间量不落全局内存；
2. 高斯卷积的反向传播本身又是一次高斯卷积——不需要 autograd 重放前向图；
3. 高斯卷积可分离（先横后竖），计算量从 \(k^2\) 降到 \(2k\)；
4. 高斯核对称，乘法次数减半；
5. 多个统计量（\(\mu_1,\mu_2,E[x^2],E[y^2],E[xy]\)）共用一次卷积遍历。

加上权重硬编码进 kernel（省掉每次读取窗口张量），这套优化正好消掉（a）中列举的全部 Python 侧开销。对 4C4D 而言：3 万迭代 × 每迭代 batch_size 个视角 ×（1 次 forward + 1 次 backward），SSIM 项从「训练瓶颈之一」降到几乎免费。

**（e）训练侧的调用现场**。[train.py:L144](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L144)：

```python
Lssim = 1.0 - fast_ssim(image.unsqueeze(0), gt_image.unsqueeze(0))
```

`render` 返回 `(3,H,W)`，而 fused-ssim 约定输入是 `(BS, CH, H, W)` 的 4D 张量，所以两侧都要 `unsqueeze(0)` 补出 batch 维。同时注意 README 的使用约束：图像必须归一化到 \([0,1]\)（DataLoader 读入的真值满足；渲染图理论上可能略微越界，靠 \(C_1,C_2\) 稳定分母）、只支持 2D 图像、只有 11×11 标准窗口。

#### 4.2.4 代码实践

**实践目标**：对拍 CUDA 版与纯 PyTorch 版 SSIM 的数值一致性，并实测加速比。

**操作步骤**（需要 GPU；无 GPU 时改为只做步骤 4 的源码对照，标注「待本地验证」）：

```python
import time, torch
from utils.loss_utils import ssim                    # 纯 PyTorch 版
from fused_ssim import fused_ssim as fast_ssim      # CUDA 版

torch.manual_seed(0)
gt  = torch.rand(1, 3, 512, 512).cuda()
img = (gt + 0.05).clamp(0, 1).requires_grad_(True)

# 1. 数值对拍：两边都是 11x11 高斯窗(σ=1.5)、C1=0.01^2、C2=0.03^2、same padding
v_ref  = ssim(img, gt).item()
v_fused = fast_ssim(img, gt).item()
print(f"pytorch={v_ref:.6f}  fused={v_fused:.6f}  diff={abs(v_ref-v_fused):.2e}")

# 2. 反向可用性：只允许第一个张量可微
fast_ssim(img, gt).backward()
print("grad norm:", img.grad.norm().item())   # 应为有限值

# 3. 计时（各自预热 10 次后测 100 次平均）
for name, fn in [("pytorch", ssim), ("fused", fast_ssim)]:
    for _ in range(10): fn(img if name=="fused" else img, gt)
    torch.cuda.synchronize(); t0 = time.time()
    for _ in range(100):
        out = fn(img, gt); out.backward() if name=="fused" else None
    torch.cuda.synchronize()
    print(name, f"{(time.time()-t0)/100*1000:.2f} ms/次")
```

**需要观察的现象**：步骤 1 两个数值在小数点后 3~4 位内一致；步骤 3 fused 明显快于纯 PyTorch。

**预期结果**：数值差应在 \(10^{-3}\) 量级以内（边界像素的处理方式略有差别：纯 PyTorch 版在 padding 处用零填充，fused 版按 `same` 语义处理，两者不完全逐位一致属正常）；加速比预期在数倍量级，具体数字与显卡有关（README 基准是 5–8×，相对 pytorch-msssim）。**待本地验证**（本环境无 GPU）。若差异明显偏大，优先检查：窗口是否都是 11×11 且 σ=1.5、`padding` 是否取 `"same"`、两图是否都在 \([0,1]\)。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `FusedSSIMMap.backward` 返回 6 个值里只有第 3 个非 `None`？

**答案**：autograd 要求 backward 的返回个数与 forward 参数个数一一对应。6 个参数是 `(C1, C2, img1, img2, padding, train)`，其中 C1/C2 是 Python 浮点数、padding/train 是字符串（不可微），img2 是真值图（不需要梯度），只有 img1（渲染图）需要梯度，所以返回 `(None, None, grad, None, None, None)`。

**练习 2**：`train=False` 时 `fusedssim` 不返回偏导张量。如果此时仍调用 backward 会发生什么？

**答案**：前向只保存了 `ssim_map`，`ctx.save_for_backward` 中缺少 `dm_dmu1` 等偏导，backward 无法计算（在 CUDA 侧拿到未定义/空的张量，行为未定义或报错）。所以 `train=False` 只能用于纯推理，README 也建议配合 `torch.no_grad()` 使用。纯推理的评估代码（如 training_report 的测试循环）沿用了默认 `train=True`，因为测试循环本身在 `torch.no_grad()` 下运行，`ctx` 不会被用到。

**练习 3**：把 `Lssim = 1.0 - fast_ssim(...)` 写成 `fast_ssim` 直接最大化 SSIM 有什么区别？

**答案**：等价。最大化 SSIM 就是最小化 `1 - SSIM`，乘 -1 只改变符号。写成 `1 - SSIM` 是为了让 Ll1 与 Lssim 都是「越小越好」的损失，才能用同一套加权求和与 backward。

### 4.3 总损失：lambda_dssim 的加权与 batch 梯度累积

#### 4.3.1 概念说明

两个部件单独看各有盲区：L1 逐像素拉均值，会偏向「糊」的解（一张模糊图的平均像素误差往往不大）；SSIM 关注局部结构，但对全局色偏不敏感。3DGS 一脉把它们线性组合：

\[ \mathcal{L} = (1-\lambda)\,L_1 + \lambda\,(1-\mathrm{SSIM}), \qquad \lambda = \texttt{lambda\_dssim} = 0.2 \]

\(\lambda\) 是「结构项的权重」：0.2 意味着 80% 力气花在像素保真、20% 花在结构相似。这不是可微调的超参数摆设——它直接塑造渲染风格：\(\lambda\) 偏大，边缘更锐利但可能引入高频伪影；偏小，整体更平滑但细节糊。

#### 4.3.2 核心流程

```text
for batch_idx in range(batch_size):          # train.py L133
    render → image                            # L138-L139
    Ll1   = l1_loss(image, gt_image)          # L143
    Lssim = 1.0 - fast_ssim(...)              # L144
    loss  = (1-λ)·Ll1 + λ·Lssim              # L145
    loss  = loss / batch_size                 # L147  ← 梯度累积的关键
    loss.backward()                           # L148  ← 累积进 .grad，不覆盖
```

注意 `backward()` 被放在 for 循环**内**：batch 内每个视角各自反传一次，PyTorch 把梯度**累加**进各参数的 `.grad`；`loss / batch_size` 保证累加完的梯度等价于「batch 平均损失」的一次反传。每个视角的 `loss` 数值只是该视角的（除以了 batch_size），日志里看到的却是另一回事——见下面的口径陷阱。

#### 4.3.3 源码精读

[损失组合的三行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L142-L148)：

```python
# Loss
Ll1 = l1_loss(image, gt_image)
Lssim = 1.0 - fast_ssim(image.unsqueeze(0), gt_image.unsqueeze(0))
loss = (1.0 - opt.lambda_dssim) * Ll1 + opt.lambda_dssim * Lssim

loss = loss / batch_size
loss.backward()
```

权重默认值注册在 OptimizationParams：[arguments/__init__.py:L95](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L95) `self.lambda_dssim = 0.2`。按 u1-l4 的参数体系，它既可以用 `--lambda_dssim` 命令行覆盖，也可以写进 yaml 的对应键（yaml 合并优先级高于命令行默认值）。

**一个真实的口径陷阱**：`Ll1`、`Lssim` 是 for 循环体内反复赋值的变量，循环结束后它们保存的是 **batch 中最后一个视角**的损失，而非 batch 平均。随后 [train.py:L190](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L190) 把它们打包进 `loss_dict`，再传给 `training_report` 写入 TensorBoard 的 `train_loss_patches/l1_loss` 等曲线。也就是说，**TensorBoard 上的训练损失曲线是「batch 末视角」的抽样**，不是均值——这是从 3DGS 单视角循环继承下来的日志口径，看图时心里要有数（曲线的波动里有一部分来自视角切换）。

另外注意 [train.py:L84](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L84) 的 `lambda_all`：它收集 `opt` 里所有 `lambda` 开头的键并**显式排除 `lambda_dssim`**，因为 `Ldssim` 已经手工写进损失了；其余 `lambda_*` 是可选附加损失项的开关，属于 u8-l4 的扩展话题。

#### 4.3.4 代码实践

**实践目标**：确认 \(\lambda\) 对损失的定量影响，并验证 `loss / batch_size` 的梯度等价性。

**操作步骤**（示例代码，CPU 可跑）：

```python
import torch
from utils.loss_utils import l1_loss

# 1. lambda 的定量影响
gt = torch.rand(3, 32, 32)
img = gt + 0.05
Ll1, Lssim = l1_loss(img, gt).item(), 0.95        # 假设 SSIM=0.95
for lam in (0.0, 0.2, 0.5, 1.0):
    print(f"λ={lam}: loss={(1-lam)*Ll1 + lam*(1-Lssim):.4f}")

# 2. 梯度累积等价性：p 是可训练参数，"img = f(p)" 的最小模拟
p_mean = torch.zeros(3, 32, 32, requires_grad=True)
g = torch.ones(3, 32, 32)
for _ in range(4):                       # 模拟 batch_size=4，四次反传
    loss = l1_loss(p_mean, g) / 4
    loss.backward()
print("累积梯度 =", p_mean.grad.unique())   # 期望全为 0.25（每次 +0.25/4/…）

p2 = torch.zeros(3, 32, 32, requires_grad=True)
l1_loss(p2, g).backward()                # 一次反传的“batch 平均”对照
print("一次反传 =", p2.grad.unique())      # 期望 1.0
```

**需要观察的现象**：第 2 部分 4 次累积后梯度为 0.25，是单次反传（1.0）的 1/4——即「逐视角 loss/batch_size 再累积」与「平均损失一次反传」对 batch 内各视角梯度相同时完全等价；视角不同时等价性变为「各视角梯度贡献的平均」，这正是 u5-l3 要展开的 `visibility_count` 归一化的出发点。

**预期结果**：如上所述。梯度数值可手算验证：\(\partial L_1/\partial p_i = \mathrm{sign}(p_i-g_i)/N\)，全 1 时每个元素梯度 \(1/N\)，再除 batch_size、乘 batch 次得 \(1/(4N)\cdot N\)… 建议直接跑出来核对 `0.25`。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`lambda_dssim=1.0` 时 L1 项完全消失，训练会怎样？

**答案**：损失只剩 \(1-\mathrm{SSIM}\)。SSIM 是局部窗口内的归一化比值，对全局亮度/色偏不敏感（两个窗口只要结构一致、均值方差成比例仍可得高分），渲染可能整体偏色或偏亮而损失不动。极端权重下还容易鼓励高频伪影。

**练习 2**：为什么 `loss / batch_size` 写在循环内、`backward()` 也写在循环内，而不是把 batch 的 loss 加起来最后一次 backward？

**答案**：数学上等价（梯度可加），但每次 backward 后该视角的计算图即被释放，显存只需容纳一个视角的图；若把各视角 loss 相加后持有，多个视角的图会同时驻留显存。逐视角反传是省显存的标准做法。

**练习 3**：想在 yaml 里把 \(\lambda\) 改成 0.4，同时保证命令行 `--lambda_dssim 0.1` 不生效，能做到吗？

**答案**：能。按 u1-l4 确立的优先级「yaml > 命令行 > 代码默认值」，只要 yaml 中写 `lambda_dssim: 0.4`，`recursive_merge` 会在命令行解析之后用 yaml 值无条件 `setattr` 覆盖，命令行的 0.1 被覆盖掉。

### 4.4 psnr 与 training_report：指标口径与日志

#### 4.4.1 概念说明

PSNR 不参与训练（没有 `psnr(...).backward()`），它是**评估口径**：进度条上每 10 次迭代刷新一次的 PSNR 反映当前 batch 的渲染质量，`training_report` 在测试迭代点算出的 test PSNR 则决定 best checkpoint。理解这两条口径的差异（是否 clamp、在哪些相机上、多少频率），是读懂训练日志、复现论文数字的前提。

#### 4.4.2 核心流程

```text
每 10 次迭代:  psnr_for_log = psnr(image, gt_image).mean()     # 训练视角、未 clamp
每 100 次迭代: training_report(...)
                 ├─ 写 TensorBoard: l1_loss/ssim_loss/total_loss/iter_time/total_points/opacity_histogram
                 └─ 若 iteration ∈ testing_iterations:
                      在 train+test 两套 held-out 相机上
                      渲染 → clamp(0,1) → 累计 l1/psnr/ssim → 求平均
                      test 组的平均 PSNR 作为返回值 → best checkpoint 判据
```

#### 4.4.3 源码精读

**（a）psnr 函数**。[mse 与 psnr 的实现](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/image_utils.py#L14-L19)：

```python
def mse(img1, img2):
    return (((img1 - img2)) ** 2).view(img1.shape[0], -1).mean(1, keepdim=True)

def psnr(img1, img2):
    mse = (((img1 - img2)) ** 2).view(img1.shape[0], -1).mean(1, keepdim=True)
    return 20 * torch.log10(1.0 / torch.sqrt(mse))
```

三个值得抠的细节：

1. **峰值硬编码为 1.0**：分子是 `1.0`，即默认输入已归一化到 \([0,1]\)。给 8 位图（峰值 255）用这个函数会得到错误的 dB 值。
2. **`view(img1.shape[0], -1)` 的形状语义**：输入 `(3,H,W)` 时按**通道**分组，返回 `(3,1)`——每通道一个 PSNR；调用处再用 `.mean()` 把三通道平均。由于三通道像素数相同，\((\mathrm{MSE}_r+\mathrm{MSE}_g+\mathrm{MSE}_b)/3\) 恰等于全体像素的 MSE，所以最终标量与「全像素一次算」严格相等。但若直接传入 `(B,3,H,W)` 且 B>1，分组会发生在 batch 维而非通道维，返回 `(B,1)`——形状约定隐式依赖调用方传 `(3,H,W)`，是个脆弱点。
3. **MSE=0 时返回 inf**：两张全同的图会得到 `inf`，日志里看到 inf 说明某次渲染与真值完全一致（通常只在退化情形出现）。

**（b）进度条 PSNR**。[train.py:L193-L198](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L193-L198) 每 10 次迭代在 `torch.no_grad()` 下计算：

```python
psnr_for_log = psnr(image, gt_image).mean().double()
ema_loss_for_log = 0.4 * loss.item() + 0.6 * ema_loss_for_log
ema_l1loss_for_log = 0.4 * Ll1.item() + 0.6 * ema_l1loss_for_log
ema_ssimloss_for_log = 0.4 * Lssim.item() + 0.6 * ema_ssimloss_for_log
```

注意这里的 `image` **没有 clamp**（对比（d）中的测试评估），且同样取自 batch 末视角。EMA 系数 0.4/0.6 让进度条数字平滑；随后 [postfix 组装](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L206-L208) 把 `Loss/PSNR/gs_num` 显示在进度条上。

**（c）training_report 的日志职责**。[train.py:L305-L312](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L305-L312) 每 100 次迭代写 6 条 TensorBoard 记录：

```python
tb_writer.add_scalar('train_loss_patches/l1_loss', Ll1.item(), iteration)
tb_writer.add_scalar('train_loss_patches/ssim_loss', Lssim.item(), iteration)
tb_writer.add_scalar('train_loss_patches/total_loss', loss.item(), iteration)
tb_writer.add_scalar('iter_time', elapsed, iteration)
tb_writer.add_scalar('total_points', scene.gaussians.get_xyz.shape[0], iteration)
tb_writer.add_histogram("scene/opacity_histogram", scene.gaussians.get_opacity, iteration)
```

其中 `total_points` 与 `opacity_histogram` 是 u5-l4、u6-l4 分析致密化与衰减效果时要盯的两条曲线。调用点在 [train.py:L222-L232](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L222-L232)：`iteration % 100 == 0 or iteration in testing_iterations`。

**（d）training_report 的评估职责**。[train.py:L316-L337](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L316-L337)：当迭代数落在 `testing_iterations` 里，对 `train`/`test` 两套相机（来自 `scene.getValidationCameras`，test 组即 u2-l2 讲过的 held-out 相机）逐个渲染并累计三项指标：

```python
image = torch.clamp(render_pkg["render"], 0.0, 1.0)     # ← 先 clamp！
l1_test += l1_loss(image, gt_image).mean().double()
psnr_test += psnr(image, gt_image).mean().double()
ssim_test += fast_ssim(image.unsqueeze(0), gt_image.unsqueeze(0)).mean().double()
```

[求平均并写曲线](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L355-L364) 后，`test` 组的平均 PSNR 被返回（`psnr_test_iter`）。注意评估 SSIM 用的是 `fast_ssim`（与训练同一实现），而 L1 用的是 `l1_loss`——**训练与评估共用同一套损失函数**，只是评估在 `no_grad`、且图像先 clamp。

**（e）两个 PSNR 口径的差异**。进度条 PSNR（（b））未 clamp、batch 末视角、训练视角；测试 PSNR（（d））clamp 后、全部 held-out 相机平均。前者普遍**略低或略不稳定**——渲染图未 clamp 时超范围像素会拉高 MSE。做实验对照时应只认 `test/loss_viewpoint - psnr` 曲线。

**（f）返回值的去向：best checkpoint**。[train.py:L273-L274](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L273-L274)：

```python
if test_psnr >= best_psnr:
    best_psnr = test_psnr
```

测试 PSNR 一旦刷新历史最优，就触发 best checkpoint 的保存（保存细节属于 u5-l5）。所以 `training_report` 的返回值不是纯日志——它直接决定哪个迭代的权重被标记为最优。

**（g）评估点从哪来**。`--test_iterations` 默认 `[7000, 30000]`（[train.py:L385](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L385)）；开启 `--exhaust_test` 后会按 `test_per_iter` 步长把评估点加密到全程（[train.py:L445-L446](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L445-L446)）。函数末尾的 `torch.cuda.empty_cache()`（[train.py:L366](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L366)）在整场相机遍历后释放缓存。

#### 4.4.4 代码实践

**实践目标**：不依赖训练，手工核对 `psnr` 的数值与形状行为。

**操作步骤**（示例代码，CPU 可跑）：

```python
import torch
from utils.image_utils import psnr, mse

# 1. 已知 MSE 的构造：每像素偏移 0.1 → MSE=0.01 → PSNR=20 dB
gt  = torch.rand(3, 64, 64)
img = gt + 0.1
out = psnr(img, gt)
print("形状:", out.shape, " 值:", out)          # 期望 (3,1)，三个 20.0000
print("标量:", out.mean().item())              # 期望 20.0

# 2. 偏移减半 → MSE=0.0025 → PSNR=26.02 dB（+6.02 dB ≈ MSE/4）
img2 = gt + 0.05
print(psnr(img2, gt).mean().item())            # 期望 26.0206

# 3. 全同图像 → inf
print(psnr(gt.clone(), gt).mean().item())      # 期望 inf

# 4. 形状陷阱：传 (B,3,H,W) 会按 batch 分组
img4 = (gt + 0.1).unsqueeze(0).repeat(2, 1, 1, 1)
gt4  = gt.unsqueeze(0).repeat(2, 1, 1, 1)
print(psnr(img4, gt4).shape)                   # 期望 (2,1)，而不是 (3,1)
```

**需要观察的现象**：构造的偏移量与 dB 值严格对应；`(3,H,W)` 输入返回 `(3,1)`；4D 输入的分组维度发生变化。

**预期结果**：`20·log10(1/0.1)=20.0000`；`20·log10(1/0.05)=26.0206`（MSE 缩小 4 倍恰加 6.02 dB）；全同图得 `inf`；4D 输入得 `(2,1)`。手算即可复核，运行**待本地验证**（CPU 即可）。

#### 4.4.5 小练习与答案

**练习 1**：训练日志的 PSNR 曲线与测试评估的 PSNR 曲线为何系统性地略有差异？列出至少两个原因。

**答案**：（1）测试评估先把渲染图 `torch.clamp(0,1)` 再算指标，进度条 PSNR 用原始输出，超范围像素拉低数值；（2）进度条 PSNR 只取 batch 末视角、训练视角，测试 PSNR 是全部 held-out 相机平均；（3）频率不同（每 10 次对每 100/评估点）。

**练习 2**：`psnr` 里 `view(img1.shape[0], -1)` 在 `(3,H,W)` 输入下按通道分组、再 `.mean()`，与直接对全部像素求 MSE 是否等价？

**答案**：等价。三通道像素数相同，\(\mathrm{MSE}_{all}=(\mathrm{MSE}_r+\mathrm{MSE}_g+\mathrm{MSE}_b)/3\)，而 `20·log10(1/sqrt(·))` 是非线性函数但作用在合并后的标量上，先平均 MSE 再取对数与合并后计算一致。

**练习 3**：论文对比时该引用哪条 TensorBoard 曲线作为「测试 PSNR」？

**答案**：`test/loss_viewpoint - psnr`（由 [train.py:L361-L362](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L361-L362) 写出），它对应 held-out 相机、clamp 后的全程平均；best checkpoint 也是由这条口径的返回值选出的。

## 5. 综合实践

把本讲三个最小模块串成一个「指标对拍台」：同一对随机图像上，同时计算 L1、两种 SSIM、PSNR，并比较纯 PyTorch SSIM 近似与两种仓库实现的数量级。

**实践目标**：验证 (1) `l1_loss`/`psnr` 的数值口径；(2) CUDA 版与纯 PyTorch 版 SSIM 的一致性与速度差；(3) 自写的简版 SSIM 与标准实现的偏差来源。

**操作步骤**（示例代码，保存为 `metric_lab.py` 在仓库根目录运行；步骤 2 需 GPU）：

```python
import time, torch
import torch.nn.functional as F
from utils.loss_utils import l1_loss, ssim, create_window
from utils.image_utils import psnr
try:
    from fused_ssim import fused_ssim as fast_ssim
    HAS_FUSED = torch.cuda.is_available()
except Exception:
    HAS_FUSED = False

torch.manual_seed(0)
gt  = torch.rand(3, 256, 256)          # 模拟 DataLoader 给出的 [0,1] 真值
img = (gt + 0.08 * torch.randn_like(gt)).clamp(0, 1)   # 模拟带噪声的渲染图

# ---- 1. L1 与 PSNR（CPU 即可）----
print("L1   =", l1_loss(img, gt).item())
print("PSNR =", psnr(img, gt).mean().item())

# ---- 2. 两种“官方”SSIM 对拍（需 GPU）----
if HAS_FUSED:
    img_c, gt_c = img.cuda().unsqueeze(0), gt.cuda().unsqueeze(0)
    v_ref = ssim(img_c, gt_c).item()
    v_fast = fast_ssim(img_c, gt_c.requires_grad_(False)).item()
    print(f"SSIM pytorch={v_ref:.6f}  fused={v_fast:.6f}  diff={abs(v_ref-v_fast):.2e}")
    for name, fn in [("pytorch", ssim), ("fused", fast_ssim)]:
        f = (lambda a, b: fn(a, b)) if name == "pytorch" else fn
        for _ in range(5): f(img_c, gt_c)
        torch.cuda.synchronize(); t0 = time.time()
        for _ in range(50): f(img_c, gt_c)
        torch.cuda.synchronize()
        print(f"{name}: {(time.time()-t0)/50*1e3:.2f} ms/次")
else:
    print("无 GPU：跳过 fast_ssim 对拍（待本地验证）")

# ---- 3. 自写“朴素 SSIM”：均匀窗口 avg_pool 近似 ----
def naive_ssim(a, b, k=11):
    def blur(x):                       # 均匀窗代替 11x11 高斯窗
        x = x.unsqueeze(0)
        return F.avg_pool2d(x, k, stride=1, padding=k//2).squeeze(0)
    mu1, mu2 = blur(a), blur(b)
    s1 = blur(a*a) - mu1**2; s2 = blur(b*b) - mu2**2; s12 = blur(a*b) - mu1*mu2
    C1, C2 = 0.01**2, 0.03**2
    m = ((2*mu1*mu2 + C1)*(2*s12 + C2)) / ((mu1**2 + mu2**2 + C1)*(s1 + s2 + C2))
    return m.mean().item()

print("SSIM naive =", naive_ssim(img, gt))
print("SSIM pytorch(官方, CPU) =", ssim(img, gt).item())
```

**需要观察的现象**：

1. L1 约在 0.06 附近（噪声标准差 0.08 的期望绝对值 \(0.08\sqrt{2/\pi}\approx0.064\)）；PSNR 约在 19~21 dB（噪声方差 0.0064 → \(-10\log_{10}0.0064\approx21.9\) dB，clamp 会略拉低）。
2. 两种官方 SSIM 数值差在 \(10^{-3}\) 量级内、fused 版明显更快。
3. 朴素版与官方版数值同量级但有可测差异（预期差 \(10^{-3}\sim10^{-2}\)）。

**预期结果与差异来源分析**（第 3 步的核心）：朴素版把高斯窗换成均匀窗——标准 SSIM 的窗口中心权重大、边缘权重小，均匀窗会让远端像素等权参与局部统计，窗口内若有强边缘，\(\mu,\sigma\) 的估计被拉平，SSIM 偏差随图像高频含量增大；此外 avg_pool 的 padding 处理与 `conv2d(padding=5)` 的零填充在边界行为上一致，但两版窗口形状不同仍会造成系统性偏移。这正是「SSIM 实现细节（窗口类型/σ/padding）不同，数字不能直接跨论文比较」的具体原因。运行数值**待本地验证**。

## 6. 本讲小结

- 总损失是 \(\mathcal{L}=(1-\lambda)L_1+\lambda(1-\mathrm{SSIM})\)，\(\lambda=\)`lambda_dssim` 默认 0.2：L1 管逐像素保真、SSIM 管局部结构，加权组合在 [train.py:L143-L145](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L143-L145) 三行完成。
- `l1_loss` 手写自 `torch.abs(diff).mean()`，对离群像素线性响应、比 L2 稳健；`l2_loss` 无人调用。
- 训练用 CUDA 版 `fused_ssim`（`FusedSSIMMap` 前向预计算三个偏导、反向单 kernel 完成），纯 PyTorch 版 `ssim` 保留给 `GaussianExtractor` 的离线评估；README 给出的可分离/对称/融合等优化是其快 5–8× 的来源。
- `psnr` 峰值硬编码 1.0、按 `shape[0]` 分组求通道 MSE，依赖调用方传 `(3,H,W)`；进度条 PSNR（未 clamp、batch 末视角）与测试 PSNR（clamp、held-out 相机平均）是两个口径。
- `training_report` 每 100 迭代写 6 条 TensorBoard 记录，在 `testing_iterations` 上评估 train/test 两组相机，返回的 test PSNR 是 best checkpoint 的唯一判据。
- TensorBoard 上的 `train_loss_patches/*` 曲线是 batch 末视角的抽样而非 batch 平均——读图时的必要心理准备。

## 7. 下一步学习建议

- **u5-l3 多视角 batch 训练与梯度合并**：本讲 4.3 只说到 `loss / batch_size` 后逐视角 backward 梯度累加；下一讲补上 `visibility_count` 归一化（乘 `batch_size/visibility_count`）如何让「累加」等价于「可见视角的平均」，以及 `_t` 梯度为何同样处理。
- **u5-l4 自适应致密化与剪枝**：本讲提到 `training_report` 写出的 `total_points`、`opacity_histogram` 两条曲线，正是观察致密化节奏与衰减效果的窗口，下一讲讲它们背后的算法。
- **u5-l5 检查点与日志**：本讲的 `test_psnr >= best_psnr` 判据如何落到磁盘上的 `chkpntN.pth` 与 `point_cloud.ply`。
- 若想继续读 SSIM 的 CUDA 内核，可用 u8-l1 的方法进入 `fused-ssim-main/ssim.cu`，重点找 `fusedssim` 中一次遍历同时累计五个统计量的循环。
