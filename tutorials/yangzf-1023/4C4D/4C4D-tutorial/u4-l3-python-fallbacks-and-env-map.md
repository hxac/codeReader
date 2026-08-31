# u4-l3 Python 回退路径与环境贴图背景

## 1. 本讲目标

上一讲（u4-l2）我们钻进了光栅化调用的最底层：pybind11 登记表、CUDA 薄壳与字节缓冲。本讲回到 `render()` 函数本身，专攻其中由 `pipe`（`PipelineParams`）控制的三个开关：

1. **`convert_SHs_python`**：把「球谐系数 → RGB 颜色」这步计算从 CUDA 核搬到 Python，用 `eval_shfs_4d` 手动求值；
2. **`compute_cov3D_python`**：把「4D 协方差 → 当前时刻 3D 条件协方差」这步计算搬到 Python，并顺带触发一套 `marginal_t > 0.05` 的时间预筛选机制；
3. **`env_map_res`**：启用一张**可优化的球面背景贴图**（environment map），用 `rendered_image + (1 - alpha) * bg` 与高斯前景做 over 合成。

学完本讲，你应该能够：

- 说出 `convert_SHs_python` 打开后，Python 侧颜色计算与 CUDA 内置 `computeColorFromSH_4D` **逐条等价**的条件（方向、时间差、+0.5、clamp、通道数）；
- 默写出 `mask = marginal_t[:, 0] > 0.05` 预筛选需要**同步过滤的全部张量列表**，并解释漏掉任何一个会发生什么形状/语义错误；
- 推导 `rendered_image + (1 - alpha) * bg_color_from_envmap` 与标准 alpha blending（over 操作符）的等价关系，以及为什么此时光栅化背景必须强制置零。

这三个知识点看似「调试旁路」，实际是理解 4C4D 渲染语义的最好放大镜：CUDA 核里一行融合代码，在回退路径里被展开成十几行显式 PyTorch，每一行都对应一个渲染语义决策。此外，`env_map` 并非摆设——官方配置 [configs/dynerf/coffee_martini.yaml:29-30](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/coffee_martini.yaml#L29-L30) 与 [flame_salmon.yaml:29-30](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_salmon.yaml#L29-L30) 都启用了 `env_map_res: 500`，这两个室内场景的背景就是靠它学出来的。

## 2. 前置知识

本讲假设你已读过 u4-l1（render 主流程）、u3-l3（4D 协方差与时间边缘化）、u3-l4（4D 球谐）。在此基础上补充三个通用概念：

**回退路径（fallback path）**。同一份计算在系统里有两份实现：一份是融合进 CUDA 核的高性能版本（默认路径），另一份是用纯 PyTorch 张量运算写的慢速版本（回退路径）。两者数学上等价，用途有三：① 调试时打印中间量；② 对拍（数值一致性验证）——两份实现互为测试基准；③ 教学——这正是我们读它的理由。代价是速度：CUDA 核一次 launch 处理全部高斯，Python 版则要经过完整的 PyTorch 调度开销。

**over 操作符（alpha compositing）**。图像合成中最基本的公式：把前景 \(F\)（不透明度 \(\alpha\)）盖在背景 \(B\) 上，结果为

\[ C = \alpha F + (1-\alpha) B \]

高斯泼溅的逐像素渲染就是多个高斯按深度排序反复做 over：第 \(i\) 个高斯贡献 \(c_i\alpha_i\prod_{j<i}(1-\alpha_j)\)，全部处理完后剩下的能量是透射率 \(T = \prod_i (1-\alpha_i)\)。若背景色为 \(B\)，最终像素 = 前景累计值 + \(T \cdot B\)。记住「\(1-\alpha\) 就是透射率」这句话，本讲第 4.4 节全靠它。

**grid_sample 的坐标约定**。`torch.nn.functional.grid_sample(input, grid)` 按归一化坐标 \([-1, 1]\) 从 `input` 中采样：\((-1,-1)\) 对应输入的左上角，\((1,1)\) 对应右下角。所以从「0 到 1 的 UV」转到 grid_sample 坐标要乘 2 减 1。它本身可微——梯度会流回被采样的图，这是 env_map 能被优化的前提。

**marginal_t 一句话回顾**（u3-l3 已详细推导）：4D 高斯在时间维上是一个一维高斯，渲染时刻 \(\tau\) 距其时间中心 \(t\) 的衰减为

\[ \text{marginal\_t} = \exp\left(-\frac{(t-\tau)^2}{2\sigma_t}\right) \]

它要乘进不透明度。默认 CUDA 路径里这一乘发生在核内部；打开 `compute_cov3D_python` 后必须由 Python 显式完成——这是本讲反复出现的一条主线。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [gaussian_renderer/__init__.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py) | 全部主角：`pipe` 分支、预筛选块、env_map 分支、radii 回填 |
| [utils/sh_utils.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/sh_utils.py) | `eval_sh`、`eval_shfs_4d`：Python 回退路径的颜色计算体 |
| [scene/gaussian_model.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py) | `get_current_covariance_and_mean_offset`、`get_marginal_t`、`get_cov_t`、`env_map` 属性的挂载与 capture |
| [scene/cameras.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/cameras.py) | `Camera.get_rays()`：env_map 分支的光线生成 |
| [arguments/__init__.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py) | `PipelineParams`：五个开关的注册处 |
| [train.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py) | env_map 参数的创建、优化器与 step |
| [diff-gaussian-rasterization/cuda_rasterizer/forward.cu](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu) | CUDA 侧 `computeColorFromSH_4D`：与 Python 回退对拍的基准 |
| [diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py) | 输入互斥校验、`1-T` 转 alpha |

## 4. 核心概念与源码讲解

### 4.1 render() 的 pipe 分支：两条回退路径与一个背景开关

#### 4.1.1 概念说明

`render(viewpoint_camera, pc, pipe, bg_color, ...)` 的第三个参数 `pipe` 是 [arguments/__init__.py:69-78](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L69-L78) 注册的 `PipelineParams` 组，包含七个键：

| 键 | 默认值 | 作用 |
| --- | --- | --- |
| `convert_SHs_python` | `False` | 颜色计算搬回 Python（4.3 节） |
| `compute_cov3D_python` | `False` | 协方差计算搬回 Python + 时间预筛选（4.2 节） |
| `debug` | `False` | 透传给光栅化设置，开启反向传播异常检测（u4-l2 已讲） |
| `env_map_res` | `0` | 大于 0 时启用分辨率为此值的可优化球面背景（4.4 节） |
| `env_optimize_until` | `1000000000` | env_map 优化到第几次迭代为止 |
| `env_optimize_from` | `0` | 已注册但主循环未消费（待确认，全仓库 grep 仅见于此处注册与 yaml） |
| `eval_shfs_4d` | `False` | 为 True 时 `sh_degree_t=2`，启用 48 通道 4D 球谐（u3-l4 已讲） |

「pipe 分支」指 `render()` 内所有以 `pipe.xxx` 为条件的代码。它们共同特点：**不改变渲染数学，只改变计算发生的位置（Python 还是 CUDA）与输入的组织方式**。所有官方 dynerf 配置里两个回退开关都是 `False`（如 [configs/dynerf/flame_steak.yaml:26-27](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml#L26-L27)），只有 `env_map_res` 在两个场景被打开。

#### 4.1.2 核心流程

`render()` 一次调用中 `pipe` 的全部消费点，按执行顺序：

```text
pipe.env_map_res ──→ ① raster_settings.bg 是否强制置零        (L41)
pipe.compute_cov3D_python ──→ ② 协方差分支：                     (L85-101)
      True:  cov3D_precomp = Python 算的条件协方差; means3D += 时间均值偏移;
             opacity *= marginal_t          ← CUDA 路径在核内做的事被显式化
      False: 传 scales / rotations / (scales_t, ts, rotations_r)，让 CUDA 自己算
pipe.convert_SHs_python ──→ ③ 颜色分支：                        (L108-125)
      True:  colors_precomp = eval_shfs_4d(...) + 0.5, clamp
      False: 传 shs 原始系数，CUDA 内置 computeColorFromSH_4D
pipe.compute_cov3D_python ──→ ④ 预筛选块：marginal_t > 0.05     (L131-157)
      对所有逐高斯张量做同步布尔索引
pipe.env_map_res ──→ ⑤ 背景合成：rendered + (1-alpha)·bg       (L174-187)
pipe.compute_cov3D_python ──→ ⑥ radii 回填到全长 N             (L189-193)
```

其中 ②③ 决定「传什么给光栅化器」，④⑥ 是 ② 的配套收尾，⑤ 与回退无关、是独立的背景机制。

注意互斥关系（在 [diff_gaussian_rasterization/__init__.py:249-258](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py#L249-L258) 强制校验）：

- `shs` 与 `colors_precomp` **必须二选一**（`Please provide excetly one of either SHs or precomputed colors!`）——所以 ③ 的两个分支互斥；
- `(scales, rotations)` 与 `cov3D_precomp` **必须二选一**——所以 ② 的两个分支互斥；
- `rot_4d` 且不传 `cov3D_precomp` 时，必须同时提供 `rotations_r / scales_t / ts`。

#### 4.1.3 源码精读

开关注册处——每个键都是「类属性即默认值」的 ParamGroup 模式（u1-l4）：

[arguments/__init__.py:69-78](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L69-L78) —— `PipelineParams` 把两个回退开关、debug、env_map 三个键与 `eval_shfs_4d` 注册进命令行参数组「Pipeline Parameters」；布尔键走 `action="store_true"`，所以命令行上写 `--convert_SHs_python` 即可打开，无需赋值。

`render()` 中 `pipe` 分支的骨架（省略中间细节，后续小节逐段展开）：

[gaussian_renderer/__init__.py:85-101](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L85-L101) —— 协方差二选一分支：`if pipe.compute_cov3D_python:` 走 Python 预计算（`cov3D_precomp`、`delta_mean`、`marginal_t` 三件事），`else:` 把原始属性 `scales/rotations/scales_t/ts/rotations_r` 交给 CUDA。

[gaussian_renderer/__init__.py:107-127](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L107-L127) —— 颜色三分支：`override_color`（外部直接给颜色）> `convert_SHs_python`（Python 算）> 默认（传 `shs` 给 CUDA）。

[gaussian_renderer/__init__.py:131-157](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L131-L157) —— 预筛选块：`mask = marginal_t[:,0] > 0.05` 后对 12 个候选张量逐一做布尔索引。

[gaussian_renderer/__init__.py:189-195](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L189-L195) —— radii 回填：被预筛掉的高斯半径记 0，因此 `visibility_filter = radii_all > 0` 长度恢复为全体高斯数 N，且时间不可见者自动标记为不可见。

#### 4.1.4 代码实践

**实践目标**：掌握打开/关闭回退开关的三种方式，并验证「yaml 优先级高于命令行」这一 u1-l4 结论在本开关上的表现。

**操作步骤**：

1. 命令行方式：在训练命令后追加 `--convert_SHs_python --compute_cov3D_python`（store_true 型参数，出现即 True）；
2. yaml 方式：把 [configs/dynerf/flame_steak.yaml:26-27](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml#L26-L27) 的两个键改为 `True`；
3. 阅读输出目录的 `training_params.txt`，确认 `convert_SHs_python` 与 `compute_cov3D_python` 的最终取值。

**需要观察的现象**：若 yaml 写 `False` 而命令行传了 `--convert_SHs_python`，最终生效值是 `False`——因为 train.py 的 `recursive_merge` 对 yaml 叶子键无条件 `setattr`，覆盖命令行（u1-l4 已推导该优先级链「派生覆盖 > yaml > 命令行 > 代码默认值」）。想让命令行生效，必须把键从 yaml 里删掉。

**预期结果**：`training_params.txt` 中记录的值与实际分支行为一致。GPU 环境下还可见训练显著变慢（待本地验证：变慢幅度取决于高斯数量）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `convert_SHs_python` 与默认 CUDA 路径传给光栅化器的张量一个是 `colors_precomp`、一个是 `shs`，而不能同时传？

**答案**：光栅化器包装层在 [diff_gaussian_rasterization/__init__.py:249-250](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py#L249-L250) 显式校验 `(shs is None and colors_precomp is None) or (shs is not None and colors_precomp is not None)` 时抛异常——两份颜色来源会让核内 `colors_precomp == nullptr` 的判别（[forward.cu:474](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L474)）失去意义，所以设计上强制二选一。

**练习 2**：`pipe.eval_shfs_4d` 与 `pipe.convert_SHs_python` 都和球谐有关，它们控制的分别是哪件事？

**答案**：`eval_shfs_4d` 控制**模型有几阶时间球谐**——train.py 在构造 GaussianModel 时把 `sh_degree_t=2 if pipe.eval_shfs_4d else 0` 传入，决定 `get_max_sh_channels` 是 16 还是 48（u3-l4）；`convert_SHs_python` 控制**这些系数在哪里被求值成 RGB**——Python 的 `eval_shfs_4d` 函数还是 CUDA 的 `computeColorFromSH_4D` 核。一个决定「学什么」，一个决定「在哪算」。

**练习 3**：两个回退开关各自独立打开时，另一个分支会发生什么？

**答案**：彼此独立。`convert_SHs_python` 单独打开时协方差仍走 CUDA（②走 else 分支），但注意此时代码仍会调用 `get_current_covariance_and_mean_offset` 求 `delta_mean` 用于方向计算（见 4.3 节的坑）；`compute_cov3D_python` 单独打开时颜色仍由 CUDA 从 `shs` 求值，且 `ts` 会在 [gaussian_renderer/__init__.py:124-125](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L124-L125) 被补上（CUDA 算 4D 球谐需要时间差）。

---

### 4.2 compute_cov3D_python：条件协方差回退与 marginal_t > 0.05 预筛选

#### 4.2.1 概念说明

默认路径下，光栅化器收到的是高斯的**原始参数**（空间尺度、四元数、时间尺度、时间中心），「4D 协方差在时刻 \(\tau\) 切片成 3D 条件协方差」这件事由 CUDA 的 `computeCov3D_conditional` 在核内完成（[forward.cu:417](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L417)）。打开 `compute_cov3D_python` 后，这步搬到 Python：

\[ \Sigma_{xx|\tau} = \Sigma_{11} - \frac{\Sigma_{xt}\Sigma_{tx}}{\sigma_t^2}, \qquad \Delta\mu = \frac{\Sigma_{xt}}{\sigma_t^2}(t - \tau) \]

即条件协方差取 Schur 补、均值偏移 \(\Delta\mu\) 加回空间中心（公式推导见 u3-l3，此处直接使用）。Python 版实现在 `build_covariance_from_scaling_rotation_4d`，CUDA 版与之逐行同构。

同时带来两个**连锁语义变化**，这是本模块的关键：

1. **marginal_t 必须显式乘进 opacity**。CUDA 路径里「有效不透明度 = sigmoid(_opacity) × marginal_t」发生在核内恰好一次（u4-l1）；Python 路径传给核的是已乘好的 `opacity`，核内不再有时间信息参与衰减。
2. **预筛选**。既然 Python 侧已经算出了每个高斯的 `marginal_t`，就可以顺手把时间上「几乎不可见」的高斯**整体扔掉**，不送进光栅化器——这是 CUDA 路径做不到的（核内剔除只按视锥和屏幕半径）。

为什么阈值是 0.05？解方程：

\[ \exp\left(-\frac{\Delta t^2}{2\sigma_t}\right) = 0.05 \;\Longleftrightarrow\; |\Delta t| = \sqrt{2\ln 20}\cdot\sqrt{\sigma_t} \approx 2.45\sqrt{\sigma_t} \]

即保留时间中心落在渲染时刻 ±2.45 个时间标准差内的高斯——正态分布 99.4% 能量都在 2.45σ 之内，扔掉的部分对渲染的影响可以忽略，但在上千帧的动态场景里能把送入光栅化的高斯数量砍掉一大截。

#### 4.2.2 核心流程

```text
输入: pc (GaussianModel), viewpoint_camera.timestamp τ, scaling_modifier

1. cov3D_precomp, delta_mean = pc.get_current_covariance_and_mean_offset(modifier, τ)
      └─ 调 build_covariance_from_scaling_rotation_4d:
           L = R_4d · diag(s_xyzt)          # 4×4
           Σ = L·Lᵀ                          # 4D 协方差
           cov3D = Σ[:3,:3] − Σ[:3,3:4]·Σ[3:4,:3]/Σ[3,3]   # Schur 补 → (N,6) 对称六元组
           delta_mean = Σ[:3,3:4]/Σ[3,3] · (τ − t)          # (N,3)
2. means3D += delta_mean                     # 时间均值偏移加回中心
3. marginal_t = pc.get_marginal_t(τ)         # (N,1) 时间高斯衰减
4. opacity = opacity * marginal_t            # 显式完成 CUDA 核内的乘法
5. mask = marginal_t[:,0] > 0.05             # (N,) 布尔掩码
6. 对 12 个候选张量同步做 tensor[mask]
7. 光栅化 → radii 长度变为 M(=mask.sum())
8. radii_all = 全零(N); radii_all[mask] = radii   # 回填，长度恢复 N
```

rot_4d 为 False 时走的是退化版：`cov3D_precomp = pc.get_covariance(scaling_modifier)` 只用空间尺度与空间旋转，**没有均值偏移、也没有 Schur 补**（时间不与空间耦合），但 marginal_t 乘 opacity 与预筛选照常执行。

#### 4.2.3 源码精读

[gaussian_renderer/__init__.py:85-93](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L85-L93) —— 回退分支主体：rot_4d 时调用 `get_current_covariance_and_mean_offset` 得到条件协方差与均值偏移，并把偏移加进 `means3D`；随后 `marginal_t = pc.get_marginal_t(...)`、`opacity = opacity * marginal_t` 把核内隐式的时间衰减显式化。

[gaussian_renderer/__init__.py:94-101](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L94-L101) —— 默认分支对照：只传原始属性 `scales / rotations / scales_t / ts / rotations_r`，条件协方差、均值偏移、marginal_t 全部留给 CUDA。

[scene/gaussian_model.py:259-263](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L259-L263) —— `get_current_covariance_and_mean_offset` 把 xyzt 尺度、左右四元数与 `dt = timestamp − get_t` 喂给 `covariance_activation`；该函数在 [scene/gaussian_model.py:53-56](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L53-L56) 按 `rot_4d` 绑定到 4D 或 3D 版本。

[scene/gaussian_model.py:35-48](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L35-L48) —— `build_covariance_from_scaling_rotation_4d`：组装 L、算 \(\Sigma = LL^T\)、取三个分块做 Schur 补、按 dt 形状分单时刻/多时刻两条均值偏移路径，最后 `strip_symmetric` 把 (N,3,3) 压成 (N,6)（[utils/general_utils.py:76-77](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py#L76-L77) 的 `strip_symmetric` 只取对角与上三角六个独立元素）。

[scene/gaussian_model.py:244-254](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L244-L254) —— `get_cov_t` 与 `get_marginal_t`：rot_4d 时 σt 取 4D 协方差的 (3,3) 元（方差语义），否则直接用 `get_scaling_t`（注意 u3-l3 提醒过：两种语义相差一个平方）；`get_marginal_t` 返回 exp 衰减，注释里被除掉的归一化因子说明它是有意省略的（渲染只关心相对权重）。

[gaussian_renderer/__init__.py:131-157](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L131-L157) —— 预筛选块全貌：`mask = marginal_t[:,0] > 0.05` 之后，12 个 `if xxx is not None: xxx = xxx[mask]` 逐一过滤。

[gaussian_renderer/__init__.py:189-195](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L189-L195) —— radii 回填：`radii_all = radii.new_zeros(mask.shape); radii_all[mask] = radii`，把 M 长度的核输出散射回 N 长度，被剔除者保持 0，`visibility_filter = radii_all > 0` 因此对全模型成立。

[train.py:150-152](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L150-L152) —— 下游消费证据：训练循环直接把 `viewspace_point_tensor / radii / visibility_filter` 当作长度 N 的全模型张量使用（如 [train.py:237-238](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L237-L238) 的 `gaussians.max_radii2D[visibility_filter]`），这是回填步骤存在的理由。

**预筛选的同步张量清单**（本模块核心表格）。`compute_cov3D_python=True` 且 `gaussian_dim=4` 时，凡长度为 N 的逐高斯输入都必须过同一个 `mask`：

| # | 张量 | 形状 | 该分支下是否非 None | 漏掉 mask 的后果 |
| --- | --- | --- | --- | --- |
| 1 | `means2D` | (N,3) | 是（screenspace_points） | 梯度容器与高斯数错位；布尔索引可微，不漏则梯度自动散射回原张量 |
| 2 | `means3D` | (N,3) | 是（已加 delta_mean） | 与其他张量长度不一致，核内按 P 遍历时读错/越界 |
| 3 | `ts` | (N,1) | 仅当 `convert_SHs_python=False`（L124-125 补设） | CUDA 求 4D 球谐时间差时长度错位 |
| 4 | `shs` | (N,K,3) | 仅当 `convert_SHs_python=False` | 颜色系数与高斯数不匹配 |
| 5 | `colors_precomp` | (N,3) | 仅当 `convert_SHs_python=True` | 同上 |
| 6 | `opacity` | (N,1) | 是 | 不透明度与高斯数错位，渲染结果错误 |
| 7 | `scales` | (N,3) | **否**（此分支恒 None，防御性保留） | — |
| 8 | `scales_t` | (N,1) | **否**（同上） | — |
| 9 | `rotations` | (N,4) | **否**（同上） | — |
| 10 | `rotations_r` | (N,4) | **否**（同上） | — |
| 11 | `cov3D_precomp` | (N,6) | 是 | 协方差与高斯数错位 |
| 12 | `flow_2d` | (N,2) | 是（全零占位，L129） | 光流占位张量长度错位 |

注意 7–10 四行：因为打开 `compute_cov3D_python` 后 scales/rotations 系列在 [L79-84](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L79-L84) 被初始化为 None 且只在 else 分支填充，它们在预筛选块里**永远不触发**——这四行是从 3DGS 上游沿袭的防御性代码。真正干活的是 1、2、3、4/5、6、11、12 共八类。

还有一个隐式约定：`mask` 之外，`marginal_t` 本身与 `delta_mean` 不需要过滤——前者已经完成了使命（生成 mask、乘进 opacity），后者已经加进 `means3D`。

#### 4.2.4 代码实践

**实践目标**：完成规格任务第一半——梳理「mask = marginal_t > 0.05 需要同步过滤的所有张量」，并用一个 CPU 可运行的小实验验证 0.05 阈值对应的时间半宽。

**操作步骤**：

1. **源码梳理**：对照上表，在 [gaussian_renderer/__init__.py:131-157](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L131-L157) 中为每一行 `if xxx is not None` 标注该张量在哪一行被赋值、何时为 None，自己重画一遍上表。
2. **漏一个试试（思想实验 + GPU 可选）**：假设把 L144-145 的 `opacity` 过滤注释掉，推导会发生什么：光栅化器包装层或 CUDA 侧会因 P（来自 means3D 的 M）与 opacity 的 N 不一致而报错，或产生错误渲染（具体报错形式取决于校验位置，**待本地验证**）。重点不是报错文本，而是「**任何长度为 N 的逐高斯张量都是同生共死的**」这一约束。
3. **CPU 小实验**（示例代码，只需 torch，无需 GPU 与 CUDA 扩展）验证时间半宽：

```python
# 示例代码：验证 marginal_t=0.05 对应 |dt| ≈ 2.45 * sqrt(sigma_t)
import torch

sigma_t = torch.tensor([0.4, 1.0, 2.0])      # 三种时间方差（rot_4d 语义）
dt = torch.linspace(0, 6, 1201).unsqueeze(-1)  # (1201, 1) 时间差
marginal = torch.exp(-0.5 * dt**2 / sigma_t)   # (1201, 3) 与 get_marginal_t 同式, 随 dt 单调递减
keep = marginal > 0.05                         # (1201, 3) 布尔掩码, 同 renderer L133
half_width = dt[keep.sum(dim=0) - 1].squeeze(-1)  # 每列最后一个仍 >0.05 的 dt (单调 ⇒ True 连续在前)
print(half_width / sigma_t.sqrt())             # 期望三列都 ≈ 2.4478 (=sqrt(2*ln20))
```

**需要观察的现象**：第 3 步输出三个数都约等于 2.45；`sigma_t` 越大（高斯时间上越「胖」），保留的绝对时间窗 \(|\Delta t|\) 越宽，但折算成标准差倍数不变。

**预期结果**：确认 0.05 不是魔法数，而是「±2.45 个时间标准差」的工程表达。若你把阈值改成 0.01 / 0.1，重算半宽应分别变为约 3.03 / 2.15 个标准差——这正是练习 3 的素材。

#### 4.2.5 小练习与答案

**练习 1**：`means2D = means2D[mask]` 之后，返回值里的 `viewspace_points` 为什么还是全长度 N 的 `screenspace_points`？梯度还能回来吗？

**答案**：`render()` 返回的是 [L200](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L200) 的 `screenspace_points`（原始 N 行张量），过滤产生的是它的一个可微视图。PyTorch 布尔索引（index_select）参与 autograd 图：反向时被选中行的梯度写回 `screenspace_points.grad` 对应行，被剔除行的梯度为 0——恰好等价于「时间不可见的高斯不产生屏幕空间梯度」，语义完全自洽。

**练习 2**：为什么预筛选只发生在 `compute_cov3D_python` 打开时，而默认 CUDA 路径不做？

**答案**：默认路径把 marginal_t 的计算留在核内，Python 侧拿不到这个量，也就无法据此构造布尔掩码；且 CUDA 核本身已有视锥剔除与屏幕半径判零（radii=0）两级淘汰。回退路径既然在 Python 里算出了 marginal_t（乘 opacity 时顺手就有），构造掩码零成本，剔除还能省掉光栅化大量时间上无关的高斯——这是「计算位置上移带来的额外优化机会」。

**练习 3**：把阈值从 0.05 改成 0.5，渲染质量和速度各会怎么变？

**答案**：半宽从 2.45σ 缩到约 1.18σ（\(\sqrt{2\ln 2}\approx1.177\)），时间边缘的高斯被成片剔除。速度上升（送入光栅化的高斯更少）；质量上，快速运动物体的「时间拖尾」能量被截断，可能出现运动区域的闪烁或变淡。由于 CUDA 路径核内的 marginal_t 衰减同样存在，0.05 对应丢弃的能量不足 0.5%，是质量-速度的合理折中（**待本地验证**具体视觉差异）。

---

### 4.3 convert_SHs_python：用 eval_shfs_4d 手动求颜色

#### 4.3.1 概念说明

「SH 系数 → RGB」同样有两份实现：默认路径把 `shs` 原样传给 CUDA，核内 `computeColorFromSH_4D` 逐高斯求值（[forward.cu:472-485](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L472-L485)）；回退路径在 Python 里调 `eval_shfs_4d` 得到 `colors_precomp`。

**等价条件**是本模块的学习重点。两份实现要给出相同颜色，必须同时满足以下五条（逐条可在源码里对上）：

| 条件 | Python 回退（renderer + sh_utils） | CUDA 默认（forward.cu） |
| --- | --- | --- |
| ① 视角方向 = 归一化(带时间偏移的中心 − 相机光心) | `dir_pp = (means3D + delta_mean) − camera_center`，L111/L114，`dir_pp_normalized` L115 | `dir = normalize(means[idx] − campos)`，L79-81，其中 means 是核内已做时间均值偏移的 `orig_points` |
| ② 时间差符号 = t − τ | `dir_t = pc.get_t − timestamp`，L119 | `dir_t = ts[idx] − timestamp`，L83 |
| ③ 时间基函数 | `cos(2π·dirs_t/l)`、`cos(4π·dirs_t/l)`，[sh_utils.py:182,203](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/sh_utils.py#L181-L203)，`l = time_duration` 长度由 L120 传入 | `cos(2π·dir_t/time_duration)`、`cos(2π·dir_t·2/time_duration)`，L143/L163 |
| ④ 偏置与截断 | `clamp_min(sh2rgb + 0.5, 0.0)`，L121 | `result += 0.5f` 后 `max(result, 0.0f)` 并记录 clamped 标志供反向，L187-194 |
| ⑤ 激活阶数一致 | 用 `pc.active_sh_degree / active_sh_degree_t` 决定消费多少通道 | raster_settings 里 `sh_degree/sh_degree_t = pc.active_*`（renderer L45-46） |

通道布局（u3-l4 结论在此复用）：0–15 时间无关空间球谐，16–31 一阶时间块乘 t1，32–47 二阶时间块乘 t2；时间块只在 `deg == 3` 时生效（两侧的嵌套 if 结构相同）；`eval_shfs_4d` 的 `sh` 形状为 (N, 3, K)。

还有一个**方向性差异**值得注意：回退路径里 `dir_pp` 与 `dir_t` 都 `.detach()` 了——梯度只流向 SH 系数（以及经由渲染结果流向 opacity/scales 等），不穿过「方向」这个中间量；CUDA 路径同理（方向在核内计算，不参与求导链）。所以两条路径的反向语义也一致。

#### 4.3.2 核心流程

```text
if pipe.convert_SHs_python（且 override_color is None）:
    shs_view = get_features.transpose(1,2).view(N, 3, K)     # (N,K,3) → (N,3,K)
    dir_pp:
        若 compute_cov3D_python: means3D 已含 delta_mean，直接减光心
        否则: 现场调 get_current_covariance_and_mean_offset 取 delta_mean 再加
    dir_pp_normalized = dir_pp / ||dir_pp||
    if gaussian_dim==3 or force_sh_3d:
        sh2rgb = eval_sh(active_deg, shs_view, dir_pp_normalized)      # 3D 球谐
    elif gaussian_dim==4:
        dir_t = (get_t − timestamp).detach()
        sh2rgb = eval_shfs_4d(active_deg, active_deg_t, shs_view,
                              dir_pp_normalized, dir_t, duration)
    colors_precomp = clamp_min(sh2rgb + 0.5, 0.0)
else:
    shs = get_features           # 原始系数交 CUDA
    若 4D 且 ts 尚为 None: ts = get_t   # CUDA 算时间差要用
```

**一个重要的坑**：`convert_SHs_python` 单独打开（不开 `compute_cov3D_python`）时，L113 仍会调用 `get_current_covariance_and_mean_offset`。该函数（[gaussian_model.py:259-263](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L259-L263)）向 `covariance_activation` 传 4 个位置参数加 dt；而 `rot_4d=False` 时绑定的是只有 3 个参数的 3D 版本（L53-56），从代码看会直接抛 `TypeError`。也就是说**这条回退路径隐含要求 rot_4d=True**（官方六个配置全部 `rot_4d: True`，所以平时不会踩到；**待本地验证**具体报错栈）。

#### 4.3.3 源码精读

[gaussian_renderer/__init__.py:108-109](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L108-L109) —— 取出全部球谐系数并转置成 (N, 3, K)：`get_features` 是 `_features_dc`(N,1,3) 与 `_features_rest`(N,K−1,3) 在维度 1 上的拼接（[gaussian_model.py:226-229](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L226-L229)），转置后恰好符合 `eval_shfs_4d` 对 `[..., C, coeffs]` 的要求；注意这里用的是 `get_max_sh_channels`（最大通道数）做 reshape，实际消费多少通道由 active 阶数决定。

[gaussian_renderer/__init__.py:110-115](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L110-L115) —— 视角方向的两种来路：协方差已预计算时 `means3D` 在 L88 已含偏移；否则现场补算 `delta_mean`——两路殊途同归，都保证「方向从**时间偏移后的**高斯中心指向相机」，与 CUDA 侧 `orig_points` 语义对齐。

[gaussian_renderer/__init__.py:116-121](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L116-L121) —— 3D/4D 分流与最终颜色：3D 或 `force_sh_3d` 走 `eval_sh`；4D 构造 `dir_t`（注意符号是 `get_t − timestamp`，与 CUDA 一致）后调 `eval_shfs_4d`，`l` 参数显式传 `time_duration[1] − time_duration[0]`（默认 10）；`+0.5` 是 SH→RGB 的直流偏置（与 RGB2SH 互逆，u3-l4），`clamp_min` 压掉负值。

[utils/sh_utils.py:115-133](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/sh_utils.py#L115-L133) —— `eval_shfs_4d` 的 DC 项：`result = C0 * sh[..., 0]`，与 `eval_sh`（[L58-113](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/sh_utils.py#L58-L113)）共享同一套常数 C0–C3；两者的 0–15 通道公式逐项相同（符号、系数完全一致）。

[utils/sh_utils.py:181-200](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/sh_utils.py#L181-L200) —— 一阶时间块：`t1 = cos(2π·dirs_t/l)` 一次性算出，乘到 16 个通道（sh[...,16] 到 sh[...,31]）上；[L202-221](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/sh_utils.py#L202-L221) 的二阶块用 `t2 = cos(4π·dirs_t/l)` 再乘 16 个通道。这两段与 [forward.cu:142-181](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L142-L181) 的 CUDA 版逐行对应——这是「两份实现互为对拍基准」的直观样本。

[forward.cu:187-194](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L187-L194) —— CUDA 侧的 +0.5 与 clamp：比 Python 版多做了 `clamped` 标志记录（反向时被 clamp 的通道不回传梯度），Python 版靠 `clamp_min` 的自动微分天然获得近似行为（输入被截断处梯度为 0）。

#### 4.3.4 代码实践

**实践目标**：用 CPU 可运行的对拍实验，验证 ① `eval_shfs_4d` 在 `deg_t=0` 时与 `eval_sh` 数学等价；② 时间块通道的「加与不加」差异。

**操作步骤**（`utils/sh_utils.py` 只依赖 torch，可在仓库根目录纯 CPU 运行）：

```python
# 示例代码：在仓库根目录执行 python -i 此脚本
import torch
from utils.sh_utils import eval_sh, eval_shfs_4d

torch.manual_seed(0)
N, K = 200, 48                       # 官方 4D 配置: 16(3,0)+16+16 = 48 通道
sh   = torch.randn(N, 3, K)          # 假 SH 系数
dirs = torch.nn.functional.normalize(torch.randn(N, 3), dim=-1)  # 单位方向
dirs_t = torch.rand(N, 1) * 10 - 5   # 时间差 ∈ [-5, 5]
l = 10.0                             # time_duration 长度

# 实验 1: deg_t=0 时, eval_shfs_4d 应只用前 16 通道且与 eval_sh 完全一致
a = eval_shfs_4d(3, 0, sh, dirs, dirs_t, l)
b = eval_sh(3, sh[..., :16], dirs)
print(torch.allclose(a, b, atol=1e-6))          # 预期 True

# 实验 2: 时间块贡献 = (deg_t=2 的结果) − (只用空间块的基函数 * 同系数)
c = eval_shfs_4d(3, 2, sh, dirs, dirs_t, l)
t1, t2 = torch.cos(2*torch.pi*dirs_t/l), torch.cos(4*torch.pi*dirs_t/l)
time_contrib = (t1[..., None]*0).sum()  # 占位: 请按下述提示手写
# 提示: time_contrib = Σ_{j=16..31} t1*sh[...,j]*基_j + Σ_{j=32..47} t2*sh[...,j]*基_j
# 若把 sh[:, :, 16:] 置零再求值, c 应退化为 a:
sh_space_only = sh.clone(); sh_space_only[:, :, 16:] = 0
c2 = eval_shfs_4d(3, 2, sh_space_only, dirs, dirs_t, l)
print(torch.allclose(c2, a, atol=1e-6))         # 预期 True
```

**需要观察的现象**：两个 `True`。第一个说明「`eval_shfs_4d(deg, 0, …)` ≡ `eval_sh(deg, sh[…:16], …)`」——4D 版本在不启用时间阶时严格退化为 3D 版本；第二个说明时间块只通过 16 号以后的系数起作用。

**预期结果**：全部通过即证明 Python 回退的通道语义与 u3-l4 总结的频率块布局一致。**与 CUDA 对拍（比较 `colors_precomp` 与核内 SH 求值）需要 GPU 环境，待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `dir_pp` 要加 `delta_mean` 再减相机光心，而不是直接用原始 `_xyz`？

**答案**：4D 高斯在时刻 τ 的有效空间中心是「均值 + 时间偏移」\(\mu + \Sigma_{xt}(t-\tau)/\sigma_t^2\)（u3-l3）。CUDA 路径里核内预处理已把偏移写进 `orig_points`，球谐方向就是从它出发的；Python 回退若不加偏移，运动物体上高斯的视角方向会系统性偏差，视角相关颜色与 CUDA 路径不再等价（等价条件①被破坏）。

**练习 2**：`shs_view` 的 reshape 用的是 `get_max_sh_channels`（最大 48），而求值用的是 `active_sh_degree`。如果一个高斯模型 active 阶数还是 0（训练刚开始），颜色由哪些系数决定？

**答案**：只由 `sh[..., 0]`（DC 通道）决定——`eval_shfs_4d` 里 `deg=0, deg_t=0` 时只有 `result = C0 * sh[..., 0]` 一项。`_features_rest` 初始化为 0（u3-l5），`oneupSHdegree` 每 1000 步升一阶（u3-l4），所以训练早期高斯是「无视角、无时间相关性」的纯直流颜色，随阶数上升才逐渐获得视角与时间相关的外观。

**练习 3**：`eval_shfs_4d` 的时间基为什么用 cos 而不用 sin（或复指数）？

**答案**：cos 是偶函数，保证时间差 ±Δt 产生相同调制——渲染「过去 0.3s」和「未来 0.3s」的视角方向差相同时颜色一致，符合「时间平移对称」的直觉，也把每阶基函数从复数 16 个压成实数 16 个。代价是无法表达时间的反对称分量（例如「变亮中」vs「变暗中」不可区分），这是 4DGS 一族表示能力的已知取舍（沿袭自 4DGS 原实现）。

---

### 4.4 env_map：球面背景贴图与 (1 − alpha) 混合

#### 4.4.1 概念说明

3DGS/4DGS 默认背景是**纯色**（黑或白，`bg_color`）。对背景复杂的真实场景（如 N3V 的咖啡厅），纯色背景逼着高斯去拟合背景纹理，浪费容量且新视角下背景不真实。4C4D 的做法是引入一张**可优化的等距圆柱投影（equirectangular）环境贴图** `env_map`，形状 (3, res, res)：

- **几何假设**：背景位于半径 \(R = 60\) 的球面上，场景完全包含在球内；
- **渲染语义**：高斯前景正常光栅化（但背景强制为黑），球面背景按每个像素光线的透射率 \(T = 1 - \alpha\) 透过来——即标准 over 合成 \(C = C_{fg} + T \cdot C_{bg}\)；
- **可优化性**：`env_map` 是 `nn.Parameter`，有独立 Adam 优化器，梯度经 `grid_sample` 反传，与高斯联合训练（到 `env_optimize_until` 为止）。

官方配置里 [coffee_martini.yaml:29-30](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/coffee_martini.yaml#L29-L30) 与 [flame_salmon.yaml:29-30](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_salmon.yaml#L29-L30) 设 `env_map_res: 500, env_optimize_until: 5000`，其余场景为 0——说明它是「按场景启用的可选背景建模」，室内带丰富背景的场景受益最大。

#### 4.4.2 核心流程

**混合公式的推导**（规格任务第二半）。设某像素沿光线排序的高斯为 \(i=1..n\)，各自贡献颜色 \(c_i\)、不透明度 \(\alpha_i\)：

\[ C_{\text{raster}} = \sum_{i=1}^{n} c_i \alpha_i \prod_{j<i}(1-\alpha_j) + T \cdot C_{\text{bg}}, \qquad T = \prod_{i=1}^{n}(1-\alpha_i) \]

这就是 over 操作符连乘的形式：每项 = 本高斯颜色 × 本高斯不透明度 × 前面所有人的透射率，最后剩下的透射率 \(T\) 分给背景。代码里：

1. 光栅化设置强制 `bg = torch.zeros(3)`（[L41](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L41)），于是 \(T \cdot C_{\text{bg}}\) 中核内贡献为 \(T \cdot 0 = 0\)，`rendered_image` 只含前景累计；
2. CUDA 返回透射率 \(T\)，Python 包装层立刻转成 `alpha = 1 − T` 返回（[diff_gaussian_rasterization/__init__.py:122](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py#L122)，u4-l2 已注意到这个转换「正好供 env_map 背景混合使用」）；
3. 环境贴图分支执行：

\[ \texttt{rendered\_image} \mathrel{+}= (1 - \texttt{alpha}) \cdot \texttt{bg\_color\_from\_envmap} \;\;\Longleftrightarrow\;\; C = C_{fg} + T \cdot C_{bg} \]

即 **`1 − alpha` 就是透射率 T**，公式与 over 操作符逐字对应。若不把核内 bg 置零，背景会被计两次（核内一次 \(T \cdot C_{\text{bg}}\)，分支再一次）——这就是条件 ① 为什么是硬前提。

**每个像素的背景色怎么来？** 三步：

1. **生成光线**：`Camera.get_rays()` 用内参 `cx/cy/fl_x/fl_y` 给每个像素造一条世界空间光线（原点 \(o\)、单位方向 \(d\)）；
2. **光线与球面求交**：解 \(|o + t d|^2 = R^2\)，即

\[ t^2 (d\cdot d) + 2t(o\cdot d) + (o\cdot o - R^2) = 0 \;\Longrightarrow\; t = -(o\cdot d) + \sqrt{(o\cdot d)^2 - (o\cdot o - R^2)} \]

（取「+√」根：从球内出发打到前方的球面）；`assert (delta > 0).all()` 保证每条光线都能命中球面——相机必须在半径 60 的球内，且 \(d\) 已归一化；
3. **交点转 UV 再采样**：经度 \(u = \mathrm{atan2}(y,x)/2\pi + 0.5 \in [0,1)\)、纬度 \(v = \mathrm{acos}(z/R)/\pi \in [0,1]\)（北极 0、南极 1），`×2 − 1` 转 grid_sample 坐标后从 `pc.env_map` 双线性采样。

#### 4.4.3 源码精读

[gaussian_renderer/__init__.py:41](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L41) —— 前置开关：`env_map_res > 0` 时光栅化背景强制为全零张量，不管训练循环传入的 `bg_color` 是黑是白——over 公式的「背景只算一次」前提。

[gaussian_renderer/__init__.py:174-181](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L174-L181) —— 求交段：先 `assert pc.env_map is not None`（`env_map_res` 开了但贴图没挂会立刻失败），`R = 60` 硬编码；`delta` 即判别式、`t_inter` 即交点距离（代码保留 `(rays_d**2).sum(-1)` 除项以兼容未归一化方向，`get_rays` 实际已归一化）；`xyz_inter` 为球面交点。

[gaussian_renderer/__init__.py:182-185](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L182-L185) —— UV 与采样：`tu` 经度、`tv` 纬度拼成 (H,W,2)，`*2−1` 后 `F.grid_sample(pc.env_map[None], texcoord[None])` 输出 (3,H,W) 的逐像素背景图；采样可微，梯度沿 grid_sample 流回 `env_map`。

[gaussian_renderer/__init__.py:187](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L187) —— 本模块一句话公式：`rendered_image = rendered_image + (1 - alpha) * bg_color_from_envmap`；上一行被注释掉的 `mask2` 实验代码暗示作者曾尝试只对部分半球使用背景。

[scene/cameras.py:76-83](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/cameras.py#L76-L83) —— `get_rays()`：像素中心网格 +0.5，按 `(i−cx)/fl_x, (j−cy)/fl_y, 1` 造相机系点，经 `inv(world_view_transform.T)`（注意存储时转置过，u2-l3）变到世界系，减光心、归一化得 `rays_d`；它**依赖真实内参**（`fl_x > 0`），这就是 u2-l3 说「env_map 分支只在带内参的数据上可用」的原因。

[train.py:91-98](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L91-L98) —— 贴图与优化器的诞生：`nn.Parameter(torch.zeros((3, res, res)))` 从**纯黑**起步，独立 Adam（学习率取 `feature_lr`），随后挂到 `gaussians.env_map`——借用 GaussianModel 的生命周期管理。

[train.py:267-269](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L267-L269) —— 三个优化器并列 step 的第三位：高斯 optimizer、Coefficient optimizer（u6-l4 展开）之后，`iteration < env_optimize_until` 时 env_map_optimizer 才 step——coffee_martini 配置下前 5000 步学背景，之后冻结、专心学前景。

[scene/gaussian_model.py:90](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L90) 与 [L136](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L136) —— `env_map` 作为模型属性占位初始化、并进入 4D 分支的 capture 元组第 19 项，因此随 checkpoint 一起保存/恢复（restore 在 [L176](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L176) 写回）——推理时（u7-l1）背景贴图能从 .pth 完整还原。

#### 4.4.4 代码实践

**实践目标**：完成规格任务第二半——推导混合公式与 alpha blending 的关系；并用 CPU 小实验验证球面 UV 映射的正确性。

**操作步骤**：

1. **公式推导（纸上完成）**：从 over 操作符出发写出 \(C_{\text{raster}}\) 的连加形式（见 4.4.2），指出 `rendered_image` 对应其中哪部分、`alpha = 1−T` 对应哪个量、`(1−alpha)·bg` 补上的又是哪一项；然后回答：若 [L41](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L41) 不强制 bg=0 而沿用训练传入的白色背景，最终图像会怎样？（答案：背景区域 = 核内 \(T\cdot\)白 + 分支 \(T\cdot\)envmap，背景被双重计入且发白，前景不受影响。）
2. **CPU 小实验**（示例代码，验证 UV 映射与合成公式）：

```python
# 示例代码：构造 4x4 小 env_map, 验证经纬度采样与 over 合成
import torch, math
import torch.nn.functional as F

res = 4
env = torch.arange(res*res, dtype=torch.float32).reshape(1, 1, res, res).repeat(1, 3, 1, 1) / (res*res)

# 球面点 → UV → grid_sample 坐标（复现 renderer L182-184）
R = 60.0
xyz = torch.tensor([[R, 0, 0], [0, R, 0], [0, 0, R], [-R, 0, 0]], dtype=torch.float32)  # (4,3)
tu = torch.atan2(xyz[:, 1:2], xyz[:, 0:1]) / (2*math.pi) + 0.5
tv = torch.acos(xyz[:, 2:3] / R) / math.pi
texcoord = torch.cat([tu, tv], dim=-1) * 2 - 1        # (4,2) ∈ [-1,1]
bg = F.grid_sample(env, texcoord[None, :, None, :])[0, :, :, 0]   # (3,4)
print(bg.shape)   # torch.Size([3, 4]): 每个球面点得到一个 RGB 背景

# over 合成: 前景(黑背景下渲染) + 透射率 * 背景
rendered = torch.zeros(3, 4)          # 假设该像素前景累计为 0（无高斯覆盖）
alpha   = torch.tensor([0.0, 0.5, 1.0, 0.8])  # 四像素累计不透明度
final = rendered + (1 - alpha)[None] * bg
print(final)          # alpha=1 的像素背景完全被遮挡, alpha=0 的像素纯背景
```

3. **阅读型步骤**：对照 [train.py:267-269](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L267-L269)，写出 env_map 的梯度来源链条：损失 → `rendered_image`（L187 的加法）→ `(1−alpha)·bg_color_from_envmap` → `grid_sample` → `env_map.grad`。注意 `alpha` 本身也依赖高斯不透明度，所以前景高斯「遮挡背景的程度」也会被背景监督信号间接训练。

**需要观察的现象**：实验 2 中 `alpha=1.0` 的第三列 `final` 为 0（背景完全被前景挡住），`alpha=0.0` 的第一列等于背景采样值。

**预期结果**：UV 四个特殊点（+x/+y/+z/−x 方向）采样值符合等距圆柱布局的直觉：北极点（+z）映射 `tv=0`（贴图顶端），赤道四点落在 `tv=0.5` 一行。真实场景渲染与 PSNR 提升需 GPU 训练，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `env_map` 初始化为全零（黑）而不是随机噪声或平均颜色？

**答案**：背景从黑开始有两个好处。其一，与 `bg=0` 的核内渲染一致——训练最开始 `alpha` 很小（高斯不透明度初值 0.1，u3-l5），像素几乎全靠 `(1−alpha)·env_map` 填充，黑色起点让背景先从「整体偏暗的均值」学起，等价于一种由粗到细的暖启动；其二，随机噪声会让早期损失剧烈震荡，干扰高斯几何学习（4C4D 的核心诉求恰是「先学好几何」，u1-l1）。另外它只是 Adam 优化下的普通参数，零初始化配合 `feature_lr` 步长足够收敛（**关于收敛行为的判断待本地验证**）。

**练习 2**：`assert (delta > 0).all()` 在什么情况下会失败？

**答案**：判别式 \((o\cdot d)^2 - |d|^2(|o|^2 - R^2) \le 0\) 意味着光线与半径 60 的球面无交——即相机在球外且视线方向背离球，或场景/相机的归一化尺度超出了半径 60 的假设。N3V 数据经 `n3v2colmap.py` 做过半径归一化（u2-l5），相机都在球内，每条向前发射的光线必命中球面，断言恒真；若你换了未归一化的自采数据并强行开 `env_map_res`，这里就是第一个报错点。

**练习 3**：`env_optimize_until: 5000` 之后 env_map 冻结，高斯还在继续训练。冻结背景对前景学习有什么影响？

**答案**：背景作为「稳定的回归目标」让高斯专心拟合前景动态物体——背景若一直变动，前景高斯需要不停追赶目标漂移。5000 步足以让一张 500×500 贴图收敛（它只依赖光线方向的分布，参数量远小于高斯模型）。同时冻结的背景参与计算图但不再更新，`rendered_image + (1−alpha)·bg` 仍会把「前景遮挡背景」的误差信号传给高斯的 opacity/位置。这是「先学背景、再学前景」的两阶段课程式（curriculum）策略。

---

## 5. 综合实践

**任务：给回退路径做一次「对拍」，并量化预筛选的剔除率。**

背景：回退路径的存在意义就是与 CUDA 路径互为基准。请设计并（在 GPU 环境下）执行以下实验；没有 GPU 时完成其中的 CPU 部分与实验设计文档。

**步骤**：

1. **准备**：用一个已训练好的输出目录（含 `chkpnt30000.pth`），写一个最小脚本：构造 `PipelineParams` 风格的 namespace、restore 高斯、取一个测试相机，分别以四种开关组合调用 `render()`（参照 [train.py:138](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L138) 的传参方式）：
   - A：两开关全关（默认 CUDA 路径，基准）；
   - B：仅 `convert_SHs_python=True`；
   - C：仅 `compute_cov3D_python=True`；
   - D：两开关全开。
2. **对拍**：对 B/C/D 分别与 A 计算 `render` 图像的逐像素最大绝对差与平均绝对差。预期三者都是小量（浮点顺序差异级别）；若 C 出现可感差异，重点检查你是否遗漏了「marginal_t 必须乘进 opacity」这一语义（本讲 4.2）。
3. **剔除率统计**：在组合 C/D 下打印 `mask.sum().item() / mask.numel()`——被时间预筛选扔掉的高斯比例。换 3 个不同 `timestamp` 的相机各测一次，观察剔除率如何随时刻变化。
4. **CPU 子任务**（无 GPU 必做）：完成 4.3.4 的 `eval_shfs_4d` 对拍实验与 4.2.4 的时间半宽实验。
5. **结论**：写一段 300 字总结——两条回退路径在哪些量上与 CUDA 严格一致、预筛选的剔除率与阈值的关系、以及你会不会在训练中长期打开它们（提示：速度 vs 可调试性）。

**预期结果**：对拍差异在 1e-5 量级以下；剔除率随时间分布不同在 30%–80% 之间波动（取决于高斯时间中心的分布，**具体数值待本地验证**）。

## 6. 本讲小结

- `pipe`（`PipelineParams`）的三个消费点决定了 `render()` 的三条支路：`compute_cov3D_python` 把**条件协方差**搬进 Python，`convert_SHs_python` 把**球谐求值**搬进 Python，`env_map_res` 启用**可优化球面背景**；两个回退开关在官方配置中默认关闭，`env_map_res: 500` 在 coffee_martini 与 flame_salmon 两个场景真实启用。
- `compute_cov3D_python=True` 带来两个连锁语义：`opacity` 必须显式乘 `marginal_t`（CUDA 路径在核内完成的那一乘），以及按 `marginal_t > 0.05`（≈ ±2.45 个时间标准差）对 **8 类实际非 None 的逐高斯张量**（means2D/means3D/ts/shs 或 colors_precomp/opacity/cov3D_precomp/flow_2d）做同步布尔过滤，随后 `radii_all` 回填把可见性掩码恢复到全长 N。
- `convert_SHs_python` 与 CUDA 内置 `computeColorFromSH_4D` 的等价依赖五条对齐：带时间偏移的方向、`dir_t = t − τ` 的符号、cos 时间基与 `time_duration` 归一、`+0.5` 与 clamp、active 阶数一致；该路径隐含要求 `rot_4d=True`（否则 `get_current_covariance_and_mean_offset` 会以 4D 参数调 3D 函数而报错）。
- env_map 分支的 `rendered_image + (1 - alpha) * bg` 就是 over 操作符：CUDA 返回透射率 T（包装层转成 `alpha = 1−T`），核内背景强制置零保证背景只计一次；逐像素背景来自光线与半径 60 球面的交点经等距圆柱 UV + `grid_sample` 采样，且梯度可回流使贴图可优化（`env_optimize_until` 后冻结）。

## 7. 下一步学习建议

本讲之后，渲染管线单元（u4）就完整了：你已经看过 `render()` 的主流程（u4-l1）、Python↔CUDA 绑定层（u4-l2）与 pipe 支路（本讲）。建议两条推进方向：

1. **进入训练主循环（u5-l1）**：带着本讲的两个具身问题去读 `train.py`——预筛选回填后的 `radii_all` 如何被 `max_radii2D` 与致密化统计消费；`rendered_image`（可能含 env_map 背景）如何进入 L1/SSIM 损失。随后 u5-l4（致密化与剪枝）会大量复用「可见性掩码」概念。
2. **预习 Neural Decaying Function 的接入点（u6-l3）**：opacity decay 的 `time_aware` 分支同样使用 `get_marginal_t(...) > 0.05` 构造时间可见性（[gaussian_renderer/__init__.py:65-69](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L65-L69)）并与 `markVisible` 的空间可见性取交集——你会看到本讲的 marginal_t 阈值在另一个语义（「只衰减当前视角真正可见的高斯」）下复用，两个 0.05 的设计一脉相承。

若想继续横向阅读源码：`utils/mesh_utils.py` 的 `GaussianExtractor`（u7-l3）会以 `override_color` 的方式复用本讲的第三个颜色分支，可作为渲染管线的收官阅读材料。
