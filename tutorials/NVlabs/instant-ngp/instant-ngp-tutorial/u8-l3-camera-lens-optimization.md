# 相机位姿与镜头优化

## 1. 本讲目标

NeRF 训练依赖一组带相机位姿和内参的照片。但现实中，无论是 COLMAP 反推出来的位姿，还是手机记录的位姿，都难免有误差——相机摆歪了几度、焦距估偏了一点、不同照片曝光不一致、镜头本身有畸变。这些误差会让模型「被迫」用一个错误视角去解释一张正确的图，结果就是浮点物（floater）、模糊和色彩漂移。

instant-ngp 给出了一组**可训练的相机参数**，让这些误差在训练中自动修正。本讲学完后，读者应该能够：

1. 理解「相机自标定（camera self-calibration）」的思想：把每张训练图的位姿、焦距、曝光都当成可优化参数，与 NeRF 网络联合训练。
2. 区分并掌握三类 CPU 侧的逐图优化器：`AdamOptimizer<T>`、`RotationAdamOptimizer`、`VarAdamOptimizer`。
3. 看懂两个 GPU 侧的可训练缓冲：畸变图 `m_distortion` 与环境贴图 `m_envmap`。
4. 认识每图潜码 `extra_dims` 与固定光照方向 `light_dir` 的用途。
5. 读懂训练循环里「每 N 步更新一次相机」的节流逻辑（`n_steps_between_cam_updates`）。

---

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前面讲义）：

- **NeRF 数据集结构**（u4-l1）：每张训练图对应一个 4×4 的 camera-to-world 矩阵，存放在 `transforms.json` 的 `frames[]` 里；外加焦距等内参。
- **NerfNetwork 双头架构**（u4-l2）：网络输入位置和方向，输出 RGB 与密度；其中「方向」就是相机看场景的视线方向，由相机位姿决定。
- **NeRF 训练循环**（u4-l4）：每步从训练图里采光线，沿光线采样、计算损失、反向传播。本讲的相机梯度正是在这条反向链路上产生的。
- **Adam 优化器**：一种自适应学习率的一阶梯度方法，维护梯度的一阶矩（动量）与二阶矩（梯度平方的滑动平均）。本讲会反复用到它的公式。

一个关键直觉：**相机参数和网络参数是两类不同的「可学习量」**。网络参数（哈希表 + MLP 权重）量级巨大、在 GPU 上，由 tiny-cuda-nn 的 `Trainer` 统一更新；相机参数（位姿、焦距、曝光）数量很少（每张图几个浮点）、在 CPU 上维护，由 instant-ngp 自己写的轻量 Adam 优化器更新。本讲的主角就是这第二类。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [include/neural-graphics-primitives/testbed.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h) | 声明 `m_nerf.training` 子结构里的所有 `optimize_*` 开关、`cam_*_offset` 优化器、畸变图 `m_distortion` 与环境贴图 `m_envmap`、每图潜码 `extra_dims` 与 `light_dir`。 |
| [include/neural-graphics-primitives/adam_optimizer.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/adam_optimizer.h) | 三个 CPU 侧优化器的完整实现：`VarAdamOptimizer`（变长向量）、`AdamOptimizer<T>`（固定类型，如 `vec2`/`vec3`）、`RotationAdamOptimizer`（旋转专用，保证更新后仍是合法旋转）。 |
| [src/testbed_nerf.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu) | NeRF 训练主文件：初始化各 `cam_*_offset`（含学习率）、`update_transforms()` 把偏移叠加到位姿上、训练循环里每 N 步取回 GPU 梯度并调用优化器更新相机参数。 |
| [include/neural-graphics-primitives/envmap.cuh](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/envmap.cuh) | 环境贴图设备函数：`read_envmap` 按视线方向查询环境光、`deposit_envmap_gradient` 把梯度按双线性权重回灌进贴图。 |

---

## 4. 核心概念与源码讲解

### 4.1 相机自标定

#### 4.1.1 概念说明

「相机自标定」指的是：**不把相机位姿和内参当作固定的真值，而是当作可在训练中修正的参数**。

为什么需要它？NeRF 的核心方程是「从一个相机位姿发射光线、采样场景、渲染出像素」。如果位姿错了，相当于从错误的视角去解释这张图——网络为了把误差降下来，会扭曲场景去迁就错误位姿，产生伪影。常见的不准来源包括：

- **外参误差（extrinsics）**：COLMAP 重建出的相机位置/朝向有偏差。
- **内参误差（intrinsics）**：焦距估计不准（focal length）。
- **曝光误差（exposure）**：不同照片亮度不一致（自动白平衡、自动曝光）。
- **镜头畸变（distortion）**：广角/鱼眼镜头的光线折射偏离针孔模型。

instant-ngp 的对策是给每张训练图（甚至全局）附加一组**偏移量（offset）**，这些偏移量在训练中与网络一起被梯度下降修正。修正后的「真实」相机 = 数据集原始相机 + 学到的偏移。

#### 4.1.2 核心流程

一次相机优化的完整数据流可以画成：

```
GPU 训练内核
   │  (反向传播时，把损失对相机参数的偏导累加进 *_gradient_gpu)
   ▼
cam_pos_gradient_gpu / cam_rot_gradient_gpu / cam_exposure_gradient_gpu / cam_focal_length_gradient_gpu
   │  (每 n_steps_between_cam_updates 步：拷回 CPU)
   ▼
CPU 侧 Adam 优化器
   │  cam_pos_offset[i].step(gradient)   等等
   ▼
update_transforms()  —— 把 cam_pos_offset / cam_rot_offset 叠加到 dataset.xforms
   ▼
下一次 GPU 训练用更新后的位姿发射光线
```

这里有三个值得注意的设计：

1. **不是每步都更新**。相机梯度积累 `n_steps_between_cam_updates`（默认 16）步后才更新一次，避免相机和网络互相「追逐」导致震荡。
2. **位姿修正用偏移量、不改原始数据**。原始 `dataset.xforms` 始终保留，偏移单独存在 `cam_*_offset` 里，由 `update_transforms()` 在每次更新后叠加。这样可随时 `reset_camera_extrinsics()` 回到初始位姿。
3. **只有显式开启才生效**。所有 `optimize_*` 开关默认为 `false`（见下文），默认行为是「相机固定，只训练网络」。

#### 4.1.3 源码精读

先看开关与优化器的声明。在 `testbed.h` 的 `m_nerf.training` 结构里：

[include/neural-graphics-primitives/testbed.h:800-L808](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L800-L808) —— 五个 `optimize_*` 开关全部默认 `false`，以及节流参数 `n_steps_between_cam_updates = 16`。含义：畸变、外参、每图潜码、焦距、曝光，缺省都不优化。

对应的优化器实例（每张训练图一个，故用 `std::vector`）：

[include/neural-graphics-primitives/testbed.h:774-L777](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L774-L777) —— `cam_exposure`（曝光，`vec3`）、`cam_pos_offset`（位置，`vec3`）、`cam_rot_offset`（旋转，用 `RotationAdamOptimizer`）逐图一个；`cam_focal_length_offset` 是全局唯一的 `vec2`（所有图共享一个焦距偏移）。

它们的初始学习率在加载数据时设定：

[src/testbed_nerf.cu:2363-L2366](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2363-L2366) —— 曝光学习率 `1e-3`，位置与旋转都是 `1e-4`，焦距更小 `1e-5`。学习率越小，说明这个参数对结果越敏感、越要小心挪动。

再看「偏移如何叠加到位姿上」——`update_transforms()`：

[src/testbed_nerf.cu:2329-L2337](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2329-L2337) —— 先把旋转偏移构造成旋转矩阵 `rot = rotmat(cam_rot_offset[i].variable())`，左乘到原位姿的旋转部分（`rot * mat3(xform.start)`）；再把位置偏移加到平移列（`xform.start[3] += cam_pos_offset[i].variable()`）。注意旋转修正作用在原始位姿「之上」（左乘），位置修正是简单平移加法。

#### 4.1.4 代码实践

**实践目标**：理解相机优化开关的默认行为，并设计一组「打开外参优化」的配置。

**操作步骤**：

1. 打开 [testbed.h 第 800-808 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L800-L808)，确认五个 `optimize_*` 默认都是 `false`。
2. 思考：若你用 COLMAP 生成的数据集训练 fox 出现明显 floater，你会优先打开哪个开关？参考答案见 4.1.5。
3. （可选运行）用 pyngp 写脚本，加载一个 NeRF 场景后，把 `testbed.nerf.training.optimize_extrinsics = True` 置位，训练若干步，观察 `export_camera_extrinsics` 导出的位姿是否相对初始值发生了偏移。

**需要观察的现象**：开启 `optimize_extrinsics` 后，相机的位置/旋转会在训练中缓慢漂移，以补偿位姿误差；关闭时位姿始终与 `dataset.xforms` 一致。

**预期结果**：对位姿不准的数据集，开启外参优化通常能降低最终损失并减少 floater。若数据集位姿本来就准，过强的相机优化反而可能让相机「偷懒」去拟合网络本应学的细节，需配合 L2 正则（见 4.2.3）。

> 待本地验证：具体损失下降幅度取决于数据集，建议在本机用同一数据集开关对比。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cam_focal_length_offset` 是全局一个（`AdamOptimizer<vec2>`），而 `cam_pos_offset` 是每张图一个（`vector<AdamOptimizer<vec3>>`）？

**参考答案**：同一台相机拍的所有照片焦距相同，所以焦距偏移全局共享；但每张照片的拍摄位置/朝向各不相同，各自的位姿误差也相互独立，故位置与旋转偏移必须逐图维护。

**练习 2**：fox 数据出现 floater，优先打开哪个开关？

**参考答案**：优先 `optimize_extrinsics`，因为 floater 多半源于位姿误差导致网络迁就错误视角；若 floater 仍存在，再考虑 `optimize_exposure`（曝光不一致会让部分区域颜色学不准）和 `optimize_distortion`（镜头畸变）。

---

### 4.2 Adam 系列优化器

相机参数数量少、在 CPU 上更新，instant-ngp 没有复用 tiny-cuda-nn 的 GPU 优化器，而是自己写了三个轻量 Adam 类。它们都位于 [adam_optimizer.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/adam_optimizer.h)。

#### 4.2.1 概念说明

Adam 优化器维护每个参数的两份「记忆」：

- **一阶矩** \(m\)（动量）：梯度的指数滑动平均，方向更稳。
- **二阶矩** \(v\)：梯度平方的指数滑动平均，自动给每个参数分配自适应学习率（梯度一直很大的参数，步长自动变小）。

一次更新为：

\[
m_t = \beta_1 m_{t-1} + (1-\beta_1) g_t
\]

\[
v_t = \beta_2 v_{t-1} + (1-\beta_2) g_t^2
\]

\[
\hat{m}_t = \frac{m_t}{1-\beta_1^t},\quad \hat{v}_t = \frac{v_t}{1-\beta_2^t}
\]

\[
\theta_t = \theta_{t-1} - \eta \cdot \frac{\hat{m}_t}{\sqrt{\hat{v}_t}+\varepsilon}
\]

其中 \(\eta\) 是基础学习率，\(\beta_1,\beta_2\) 默认 0.9 与 0.99，\(\varepsilon=10^{-8}\) 防止除零。代码里把偏差校正（\(\hat{m},\hat{v}\)）合并进了 `actual_learning_rate`：

\[ \text{actual\_lr} = \eta \cdot \frac{\sqrt{1-\beta_2^t}}{1-\beta_1^t} \]

三个类的区别在于「被优化的量是什么类型、如何更新」：

| 类 | 被优化量 | 用途 | 关键差异 |
|----|---------|------|---------|
| `AdamOptimizer<T>` | 固定类型 `T`（如 `vec2`/`vec3`） | 焦距、曝光、位置 | 标准亚当更新，逐元素运算 |
| `VarAdamOptimizer` | 变长 `std::vector<float>` | 每图潜码 `extra_dims` | 用 `vector` 支持任意维度 |
| `RotationAdamOptimizer` | `vec3`（轴角表示） | 相机旋转偏移 | 更新后左乘一个小旋转矩阵，保证结果仍是合法旋转 |

第三类是最特别的——旋转不能简单做向量加减（那样会脱离旋转群 SO(3)、破坏正交性）。

#### 4.2.2 核心流程

`AdamOptimizer<T>::step(g)` 的标准流程：

```
1. iter += 1
2. actual_lr = learning_rate * sqrt(1 - beta2^iter) / (1 - beta1^iter)   // 偏差校正
3. first_moment  = beta1 * first_moment  + (1-beta1) * g                  // 动量
4. second_moment = beta2 * second_moment + (1-beta2) * g*g                // 自适应分母
5. variable -= actual_lr * first_moment / (sqrt(second_moment) + epsilon) // 更新
```

`RotationAdamOptimizer` 的流程相同，但第 5 步替换为「在 SO(3) 上做更新」：

```
5'. rot = actual_lr * first_moment / (sqrt(second_moment) + epsilon)   // 一个小的轴角修正
6'. variable = rotvec( rotmat(-rot) * rotmat(variable) )              // 复合：旧旋转 ∘ 修正
```

即把当前旋转 `variable` 先转成矩阵，左乘一个由梯度算出的小旋转 `rotmat(-rot)`，再转回轴角。这样无论更新多少次，`variable` 始终是合法的旋转向量。

#### 4.2.3 源码精读

`AdamOptimizer<T>::step` 的核心几行：

[include/neural-graphics-primitives/adam_optimizer.h:145-L152](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/adam_optimizer.h#L145-L152) —— 第 148 行算 `actual_learning_rate`（已含偏差校正），149-150 行更新一阶/二阶矩，151 行 `variable -=` 完成更新。注意除法是逐元素的（`T` 是 `vec2`/`vec3` 时重载了运算符）。

`RotationAdamOptimizer` 的特殊更新：

[include/neural-graphics-primitives/adam_optimizer.h:238-L247](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/adam_optimizer.h#L238-L247) —— 第 244 行算出小修正 `rot`，246 行 `rotvec(rotmat(-rot) * rotmat(variable()))` 把修正作用到当前旋转上。这是「旋转在李代数 so(3) 上做梯度下降、再映射回 SO(3)」的工程实现。

再回到训练循环看外参更新。注意三个细节：**梯度尺度还原**、**L2 正则**、**学习率衰减**：

[src/testbed_nerf.cu:2887-L2932](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2887-L2932) ——

- 第 2907-2908 行：`pos_gradient = cam_pos_gradient[i] * per_camera_loss_scale`，其中 `per_camera_loss_scale = n_images / LOSS_SCALE / n_steps_between_cam_updates`（[第 2884-2885 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2884-L2885)）。这把 GPU 上累加（含 `LOSS_SCALE` 放大、含 N 步累积）的梯度还原成「每张图每步」的有效梯度。
- 第 2910-2912 行：加上 L2 正则 `gradient += offset.variable() * extrinsic_l2_reg`（默认 `1e-4`，见 [testbed.h:785](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L785)），防止相机偏移无限漂大。
- 第 2914-2925 行：学习率随步数指数衰减 `extrinsic_learning_rate * 0.33^(step/128)`，下限是主优化器学习率的千分之一。这保证训练后期相机基本「锁死」，把舞台交给网络。

最后调用 `cam_pos_offset[i].step(pos_gradient)` 与 `cam_rot_offset[i].step(rot_gradient)`（第 2927-2928 行）真正更新，再 `update_transforms()`（第 2931 行）把新偏移写回位姿。

#### 4.2.4 代码实践

**实践目标**：用纸笔验算一次 `AdamOptimizer<vec3>` 的更新，确认能对上源码。

**操作步骤**：

1. 假设某个相机位置偏移当前为 `variable = (0,0,0)`，一阶矩、二阶矩均为 0，`iter = 0`。
2. 第 1 次更新收到梯度 `g = (0.1, 0, 0)`，`learning_rate = 1e-4`，`beta1 = 0.9`，`beta2 = 0.99`，`epsilon = 1e-8`。
3. 手算：`iter = 1`；`first_moment = 0.1*0.1 = 0.01`（`(1-beta1)*g = 0.1*0.1`）；`second_moment = 0.01*0.1^2 = 0.0001*...` —— 实际 `second_moment = (1-0.99)*0.1^2 = 0.01*0.01 = 1e-4`；`actual_lr = 1e-4 * sqrt(1-0.99^1)/(1-0.9^1) = 1e-4 * sqrt(0.01)/0.1 = 1e-4 * 0.1/0.1 = 1e-4`。
4. 更新量 = `1e-4 * 0.01 / (sqrt(1e-4) + 1e-8) = 1e-4 * 0.01 / (0.01 + 1e-8) ≈ 1e-4`，方向为 `(−1,0,0)`。
5. 新 `variable ≈ (−1e-4, 0, 0)`。

**需要观察的现象**：第一次更新后，位置偏移约为「负学习率」量级（约 `−1e-4`），与代码逻辑一致。

**预期结果**：手算 `variable ≈ (-1e-4, 0, 0)`。这是 Adam 在冷启动时「实际步长≈基础学习率」的典型特征（偏差校正使首步不被零矩压成 0）。

#### 4.2.5 小练习与答案

**练习 1**：为什么相机优化要在 CPU 上做，而不是像网络那样在 GPU 上？

**参考答案**：相机参数极少（每图几个浮点），GPU 启动内核与显存搬运的开销远超计算本身；且位姿更新后还要 `update_transforms()` 做矩阵叠加并拷回 GPU，CPU 侧直接操作更简单。网络参数量巨大，必须在 GPU 上做向量化更新。

**练习 2**：`RotationAdamOptimizer` 为什么不直接 `variable -= actual_lr * m / (sqrt(v)+eps)`？

**参考答案**：旋转向量做普通加减后会脱离 SO(3)，破坏正交性（旋转矩阵的转置不再等于逆）。正确做法是在李代数 so(3) 上算出一个小的轴角修正，再通过矩阵复合（`rotmat(-rot) * rotmat(variable)`）映射回合法旋转。

---

### 4.3 畸变图与环境贴图（GPU 侧可训练缓冲）

`cam_*_offset` 是 CPU 侧逐图的小参数。但有两类相机相关量是**空间分布的**（每个像素/每个方向一个值），无法用几个浮点表达，于是 instant-ngp 把它们建成 GPU 上的可训练网格缓冲：畸变图 `m_distortion` 与环境贴图 `m_envmap`。

#### 4.3.1 概念说明

**畸变图（distortion map）**：真实镜头（尤其广角、鱼眼）会让直线变弯，偏离针孔模型。instant-ngp 不用解析畸变公式（如 OpenCV 的 k1/k2/p1/p2），而是学一张二维偏移网格——对每个归一化像平面坐标，查表得到一个 2D 修正量 \((\Delta u, \Delta v)\)，把「畸变后的像素坐标」纠正回「无畸变坐标」。这张网格本身可训练，于是镜头畸变能在训练中自动拟合。

**环境贴图（envmap）**：NeRF 只能学到被相机拍到过的区域，没拍到的背景（天空、远处）是未知的。环境贴图是一张球面展开的 RGBA 图：给定一条视线方向，从中查一个颜色作为该方向的背景。它也是可训练的，于是模型能同时学会「场景」与「它所处的环境光照」。光照方向 `light_dir`（[testbed.h:871](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L871)）是一个固定的 `vec3`，可作为着色时的光源方向（用于产生方向相关的反射效果）。

两者的共同点：都是 `TrainableBuffer`（tiny-cuda-nn 提供的可训练张量缓冲），各有独立的 `Trainer` 和 `Optimizer`，因此它们的参数也参与梯度下降，但**不**用本讲的 CPU 侧 Adam 类，而走 GPU 优化器（`trainer->optimizer_step`）。

#### 4.3.2 核心流程

环境贴图在渲染中的双向数据流：

```
渲染某像素：
   光线方向 dir  ──read_envmap(envmap, dir)──►  背景颜色 background_color
                                                       (与 NeRF 体渲染合成)
反向传播：
   损失对背景颜色的偏导  ──deposit_envmap_gradient──►  envmap_gradient (双线性回灌)
                                                       │
                                          train_envmap=true 时
                                                       ▼
                                       m_envmap.trainer->optimizer_step  更新贴图
```

畸变图的流程类似：训练时用畸变图把像素坐标「纠正」后再发射光线，反向时把梯度回灌进 `m_distortion.map->gradients()`，`optimize_distortion` 开启时调用 `m_distortion.trainer->optimizer_step` 更新。

#### 4.3.3 源码精读

两个缓冲的结构声明（内嵌于 `Testbed`）：

[include/neural-graphics-primitives/testbed.h:1242-L1265](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L1242-L1265) —— `TrainableEnvmap` 持有 `TrainableBuffer<4, 2, float>`（2D、每格 4 通道 RGBA）、独立的 `optimizer` 与 `trainer`、分辨率 `resolution` 与 `loss_type`。

[include/neural-graphics-primitives/testbed.h:1267-L1288](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L1267-L1288) —— `TrainableDistortionMap` 持有 `TrainableBuffer<2, 2, float>`（2D、每格 2 通道偏移），同样自带 `optimizer`/`trainer`。

环境贴图的查询与梯度回灌在 `envmap.cuh`：

[include/neural-graphics-primitives/envmap.cuh:24-L50](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/envmap.cuh#L24-L50) —— `read_envmap` 先把视线方向 `dir` 转成球面坐标 `dir_to_spherical_unorm`，再在贴图上做双线性插值（4 个 texel 加权）。注意 x 方向做了环绕（第 33-37 行 `pos.x ± resolution.x`），因为球面经度是周期的；y 方向做了截断（第 38 行 clamp），因为纬度不周期。

[include/neural-graphics-primitives/envmap.cuh:52-L87](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/envmap.cuh#L52-L87) —— `deposit_envmap_gradient` 是 `read_envmap` 的「反向」：用同样的双线性权重，通过 `atomicAdd` 把梯度累加到对应 texel（第 83-86 行的 4 次 `deposit_val`）。

环境贴图的优化触发点在训练循环：

[src/testbed_nerf.cu:2762-L2775](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2762-L2775) —— `train_envmap` 为真时取 `m_envmap.envmap->gradients()`，清零后参与反向，最后 `m_envmap.trainer->optimizer_step` 更新贴图。

畸变图的更新（在外参更新的同一节流块内）：

[src/testbed_nerf.cu:2934-L2939](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2934-L2939) —— 先用 `safe_divide` 把梯度按 `gradient_weights` 归一化（每个网格点被采样次数不同，需加权），再 `m_distortion.trainer->optimizer_step` 更新。注意它和相机外参一样受 `n_steps_between_cam_updates` 节流。

#### 4.3.4 代码实践

**实践目标**：理解 `read_envmap` 与 `deposit_envmap_gradient` 的「前向查询/反向回灌」对称关系。

**操作步骤**：

1. 打开 [envmap.cuh](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/envmap.cuh)，对照第 42-47 行（前向：4 个 texel 双线性加权求和）与第 83-86 行（反向：同样的 4 组权重分别回灌）。
2. 验证：前向对某 texel 的权重是 `(1-wx)*(1-wy)`，反向 `deposit_val` 第一个调用的权重也正好是 `(1-wx)*(1-wy)`——两者严格对称，这是「可微插值」的必要条件。
3. 思考：为什么 x 方向要环绕（经度周期）而 y 方向要截断（纬度不周期）？参考答案见 4.3.5。

**需要观察的现象**：前向与反向用完全相同的 4 组双线性权重、处理相同的 4 个 texel；x 越界靠 `± resolution.x` 回绕，y 越界靠 clamp。

**预期结果**：确认 `read_envmap` 是 `deposit_envmap_gradient` 的精确转置，从而保证环境贴图的梯度计算正确。

> 待本地验证：若要实际观察环境贴图被学出来的样子，需在 pyngp 里设置 `train_envmap=True` 并渲染 `ERenderMode` 中查看背景的视角。

#### 4.3.5 小练习与答案

**练习 1**：为什么环境贴图的 x（经度）方向环绕、y（纬度）方向截断？

**参考答案**：球面上经度 0° 与 360° 是同一条经线，故 x 方向周期连续，越界需回绕到对侧；纬度只有 [0, π]（北极到南极），到极点就到头，不周期，故 y 方向用 clamp 截断。

**练习 2**：畸变图用 `TrainableBuffer<2, 2, float>`，其中两个 `2` 分别代表什么？

**参考答案**：第一个 `2` 是每个网格点存储 2 个 float（即 2D 偏移 \((\Delta u, \Delta v)\)）；第二个 `2` 是缓冲的秩（rank）为 2，即二维网格（对应像平面的两个坐标轴）。

---

### 4.4 每图潜码 extra_dims 与光照方向

除了位姿/焦距/曝光/畸变，还有一类「逐图」的可学习量：`extra_dims`（每图潜码）。

#### 4.4.1 概念说明

`extra_dims` 是**每张训练图附带的一段可学习潜码（latent code）**。它的动机是：同一个场景在不同照片里可能有不同的「状态」——不同的光照、不同的反射、甚至轻微的形变——这些差异无法用单一的、静态的 NeRF 表达。于是给每张图配一段潜码，在网络前向时把这段潜码与位置/方向一起送进网络，让模型学会「这张图特有的外观」。

与之相对的 `light_dir`（[testbed.h:871](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L871)）是一个**全局固定**的 `vec3` 光源方向，用于产生方向相关的着色（如高光、阴影）。它不是默认可训练的，更像是渲染时的一个外部输入参数。

> 注意：`extra_dims` 是否启用取决于网络配置——只有当 NerfNetwork 声明了 `n_extra_dims > 0`（见 [testbed_nerf.cu:1863](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L1863) 的 `dataset.n_extra_dims() == 0` 判断）时，每图潜码才有意义。

#### 4.4.2 核心流程

```
每张图 i 有一段长度为 n_extra_dims 的潜码 extra_dims_gpu[i*n_extra_dims : (i+1)*n_extra_dims]
   │
   │ 训练第 i 张图的光线时，把这段潜码拼到 NerfCoordinate 后面一起送进网络
   ▼
反向传播产生 extra_dims_gradient_gpu
   │ (每 n 步拷回 CPU)
   ▼
extra_dims_opt[i].step(gradient)   // VarAdamOptimizer，因为是变长向量
   │
   ▼
update_extra_dims()  把新潜码拷回 GPU
```

潜码用 `VarAdamOptimizer`（变长向量版 Adam，[adam_optimizer.h:25](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/adam_optimizer.h#L25)）逐图优化，因为潜码维度由配置决定、运行时可变。

#### 4.4.3 源码精读

潜码与优化器声明：

[include/neural-graphics-primitives/testbed.h:779-L781](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L779-L781) —— `extra_dims_gpu`（潜码）、`extra_dims_gradient_gpu`（梯度）、`extra_dims_opt`（每图一个 `VarAdamOptimizer`）。

潜码在内核里按图索引取用：

[src/testbed_nerf.cu:744](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L744) —— `const float* extra_dims = extra_dims_gpu + img * n_extra_dims;`，即第 `img` 张图的潜码起始地址。

潜码的优化步骤：

[src/testbed_nerf.cu:2860-L2879](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2860-L2879) —— 把 GPU 梯度拷回 CPU，逐图除以 `LOSS_SCALE` 还原，用 `extra_dims_opt[i].step(gradient)` 更新（第 2875 行），再 `update_extra_dims()` 拷回 GPU。

渲染时用哪张图的潜码由 `rendering_extra_dims_from_training_view`（[testbed.h:873](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L873)）决定，默认取第 0 张训练图的潜码。

#### 4.4.4 代码实践

**实践目标**：确认 `extra_dims` 在「无潜码配置」下是被跳过的。

**操作步骤**：

1. 阅读 [testbed_nerf.cu:1863](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L1863) 附近的 `if (dataset.n_extra_dims() == 0)` 分支，理解当网络不需要潜码时的提前返回。
2. 阅读 [testbed_nerf.cu:2860-2879](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2860-L2879)，确认潜码优化受 `train_extra_dims`（即 `optimize_extra_dimensions`）开关控制。

**需要观察的现象**：默认 `configs/nerf/base.json` 没有声明 extra dims，`n_extra_dims() == 0`，因此潜码相关代码在标准 NeRF 训练中根本不执行；只有自定义网络配置声明了潜码维度，`extra_dims` 链路才被激活。

**预期结果**：`extra_dims` 是「可选的逐图潜码」机制，标准配置下休眠，需要时才由 `optimize_extra_dimensions` 与网络配置共同启用。

#### 4.4.5 小练习与答案

**练习 1**：`extra_dims_opt` 为什么用 `VarAdamOptimizer` 而不是 `AdamOptimizer<vec3>`？

**参考答案**：潜码维度 `n_extra_dims` 由网络配置决定，可能不是 2 或 3，运行时才确定。`VarAdamOptimizer` 用 `std::vector<float>` 支持任意维度；`AdamOptimizer<T>` 要求编译期固定类型（如 `vec2`/`vec3`）。

**练习 2**：`extra_dims`（逐图潜码）与 `light_dir`（固定光源方向）有何本质区别？

**参考答案**：`extra_dims` 每张图一段、可训练，捕捉每张图特有的外观差异；`light_dir` 是单个全局 `vec3`、默认不可训练，作为着色时的统一光源方向输入。前者随图片变化、后者全场统一。

---

## 5. 综合实践

设计一个「相机自标定对照实验」，把本讲内容串起来：

1. **准备**：选一个 NeRF 数据集（如 fox 或你自己的 COLMAP 数据）。先用默认配置（所有 `optimize_*` 关闭）训练若干步，记录最终损失与渲染质量。
2. **打开外参优化**：用 pyngp 设置 `testbed.nerf.training.optimize_extrinsics = True`，重新训练同样步数，对比损失与 floater 情况。
3. **叠加曝光优化**：再设 `optimize_exposure = True`，观察色彩一致性是否改善。注意源码里曝光优化会做「去均值」规整（[testbed_nerf.cu:2988-2992](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2988-L2992)），解释为什么需要这一步——因为「所有曝光同时加一个常数」是不可观测的（gauge freedom），减去均值是为了固定这个自由度。
4. **调节节流**：把 `n_steps_between_cam_updates` 从 16 改成 4 或 64，观察相机更新频率对训练稳定性的影响，并联系 4.1.2 的「相机与网络互相追逐」论述。
5. **导出对比**：用 `export_camera_extrinsics` 在两组实验里分别导出位姿，对比 `optimize_extrinsics` 开启前后相机位姿的偏移量。

> 待本地验证：以上对比实验需在有 CUDA GPU 的本机执行；具体数值结论取决于数据集质量。

---

## 6. 本讲小结

- instant-ngp 把每张训练图的外参（位姿）、焦距、曝光当作**可优化偏移量**（`cam_pos_offset`/`cam_rot_offset`/`cam_focal_length_offset`/`cam_exposure`），与网络联合训练，即「相机自标定」，用于修正 COLMAP 等来源的位姿/内参/曝光误差。
- 相机参数在 **CPU 侧**用三个自研轻量 Adam 类更新：`AdamOptimizer<T>`（焦距/曝光/位置）、`VarAdamOptimizer`（变长潜码）、`RotationAdamOptimizer`（旋转，通过矩阵复合保证更新后仍是合法旋转）。
- 相机更新**不是每步都做**，而是受 `n_steps_between_cam_updates`（默认 16）节流；更新时还原梯度尺度（`per_camera_loss_scale`）、加 L2 正则（`extrinsic_l2_reg`）、且学习率随步数指数衰减以在训练后期锁死相机。
- 空间分布的相机相关量用 **GPU 可训练缓冲**：畸变图 `m_distortion`（`TrainableBuffer<2,2,float>`）学镜头畸变、环境贴图 `m_envmap`（`TrainableBuffer<4,2,float>`）学背景光照，二者各带独立 `Trainer`，前向查询/反向回灌严格对称（`read_envmap` ↔ `deposit_envmap_gradient`）。
- `extra_dims` 是**可选的逐图可学习潜码**（`VarAdamOptimizer`，受 `optimize_extra_dimensions` 与网络 `n_extra_dims` 共同控制），`light_dir` 是全局固定光源方向，二者分别捕捉「每图特有外观」与「统一着色方向」。

---

## 7. 下一步学习建议

- 若想看这些优化如何被 Python 暴露与脚本化，阅读 **u7-l1（pyngp 绑定架构）** 与 **u7-l2（run.py）**，理解 `optimize_extrinsics` 等开关在 Python 侧如何设置、`export_camera_extrinsics` 如何调用。
- 若想理解相机优化后位姿如何用于「相机路径」和视频渲染，继续 **u6-l2（相机路径与视频渲染）**，那里会用到本讲的 `set_camera_extrinsics` 与 `update_transforms`。
- 若对 `Trainer`/`TrainableBuffer`/`optimizer_step` 这些 GPU 侧优化基础设施感兴趣，建议跳到外部依赖 **tiny-cuda-nn** 的源码（本仓库的 `dependencies/tiny-cuda-nn/`），那里是 `m_envmap.trainer`、`m_distortion.trainer` 以及主网络 `m_trainer` 的真正实现。
- 若想了解滚动快门（rolling shutter）下相机位姿如何用「起点+终点」两个矩阵表达，回头细读 [testbed.h:844-846](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L844-L846) 的 `set_camera_extrinsics_rolling_shutter`，它是本讲位姿模型在运动模糊场景下的推广。
