# SDF 原语与球面追踪

## 1. 本讲目标

本讲是「其他原语」单元的第一篇，聚焦 instant-ngp 的 **SDF（Signed Distance Field，有向距离场）原语**。读者在 u2 已认识 `Testbed` 的模式分发，在 u3 已认识 `reset_network` 如何构造网络。本讲要回答一个新问题：**当输入是一个三角网格（如 `data/sdf/armadillo.obj`）时，instant-ngp 如何用一个 MLP 学会「空间中任意一点到这个网格表面的有向距离」，又如何把这个距离场渲染成一张图？**

学完后你应当掌握：

1. 理解 SDF 表示的是「空间到表面的有向距离场」，以及 instant-ngp 里「内为负、表面为零、外为正」的符号约定。
2. 看懂 `SphereTracer` 如何沿每条相机光线，用网络预测的距离一步步「安全推进」，直到命中表面——这就是**球面追踪（sphere tracing）**。
3. 理解 `train_sdf` / `generate_training_samples_sdf` 如何「在线」生成训练样本：在表面精确采样、在表面附近扰动采样、在整个空间均匀采样三类点。
4. 认识渲染所需的法线（有限差分近似）、软阴影（阴影光线）、以及最终着色（Disney BRDF）是怎么接上的。
5. 理解 `distance_scale` / `zero_offset` 两个关键参数如何影响追踪的步长与收敛到的等值面位置。

---

## 2. 前置知识

### 2.1 什么是 SDF（有向距离场）

一个三维场景的 **SDF** 是一个函数 \(d(\mathbf{p})\)，输入空间一点 \(\mathbf{p}\)，输出该点到最近表面的**有向距离**：

- 表面之外：\(d>0\)，数值等于到表面的最近距离；
- 表面之上：\(d=0\)；
- 表面之内：\(d<0\)，绝对值等于到表面的最近距离。

instant-ngp 用的就是这个约定（见后文 `compare_signs_kernel` 中 `inside = distance <= 0`）。SDF 的核心好处是它有一个**关键的几何保证**：在任意一点 \(\mathbf{p}\)，以 \(|d(\mathbf{p})|\) 为半径的球体内**绝对不会碰到表面**。这个保证是球面追踪能够「大胆迈步」的数学基础。

### 2.2 球面追踪（Sphere Tracing）

传统光线追踪要逐一求交，而 SDF 渲染用一种更聪明的办法——**球面追踪**：

1. 从光线起点出发；
2. 查询当前点的 SDF 值 \(d\)；
3. 因为半径 \(d\) 内没有表面，光线可以**安全地沿方向前进 \(d\)**；
4. 重复，直到 \(d\) 小于某个很小的阈值 \(\varepsilon\)——此时认为光线「命中」了表面。

直觉上，这就像蒙着眼睛往前走，每一步先问「前面多远之内肯定没墙」，然后恰好走那么远。靠近墙时步子自然变小，最终贴到墙上。

### 2.3 法线、软阴影与着色

- **法线**：SDF 在表面附近的梯度方向就是表面法线。可以用**有限差分（finite difference, FD）**近似梯度。
- **软阴影**：朝光源再发射一条「阴影光线」做球面追踪，途中离障碍物越近、可见度越低，从而得到柔和的阴影边界。
- **着色**：有了位置、法线、光照，就能用 BRDF 模型（本项目用 Disney BRDF）算出像素颜色。

### 2.4 与 NeRF 的对照

NeRF（u4）渲染的是**体密度**，沿光线密集采样并做 alpha 合成；SDF 渲染的是**硬表面**，沿光线用距离自适应地大步跳跃直到命中。两者的「网络」也不同：SDF 网络输入 3 维坐标、输出 1 维标量（距离）。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/neural-graphics-primitives/testbed.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L899-L945) | `Sdf` 状态结构体（含 `distance_scale`、`zero_offset`、`maximum_distance` 等参数）、`SphereTracer` 与 `FiniteDifferenceNormalsApproximator` 内嵌类、`distance_fun_t`/`normals_fun_t` 类型别名 |
| [src/testbed_sdf.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1108-L1326) | SDF 模式的全部实现：`render_sdf` 渲染编排、`SphereTracer::trace` 球面追踪主循环、`generate_training_samples_sdf` 样本生成、`train_sdf` 训练、`calculate_iou` 评测、各类 CUDA kernel |
| [include/neural-graphics-primitives/sdf.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/sdf.h#L24-L54) | `RaysSdfSoa`：球面追踪光线的数据布局（位置、法线、距离、阴影可见度等结构体数组） |
| [include/neural-graphics-primitives/sdf_device.cuh](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/sdf_device.cuh#L23-L40) | `SdfPayload`（每条光线的方向、步数、是否存活）、`BRDFParams`（着色参数） |
| [include/neural-graphics-primitives/fused_kernels/trace_sdf.cuh](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/trace_sdf.cuh#L23-L121) | JIT 融合的球面追踪内核：一个线程负责一条光线，内部 `while` 循环反复查 `eval_sdf` 并推进，可同时累计阴影可见度 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：**距离场渲染（SDF 原语的网络与坐标空间）**、**SphereTracer（球面追踪主循环）**、**train_sdf（在线采样与训练）**，并补充一个延伸模块讲法线、阴影与着色。

### 4.1 距离场渲染：SDF 原语的网络与坐标空间

#### 4.1.1 概念说明

SDF 原语要做的事，是让一个 MLP 拟合 \(d(\mathbf{p})\)：输入 3 维坐标，输出 1 维有向距离。在 instant-ngp 里，这个 MLP 由 `reset_network` 用 `configs/sdf/base.json` 构造成 `NetworkWithInputEncoding`（坐标先经 HashGrid 编码，再过 FullyFusedMLP，详见 u3-l3）。

关键在于：**距离的「真值」从哪来？** 训练时，真值来自加载的三角网格——通过 `TriangleBvh` 的有向距离查询得到（这一块在 u5-l2 详讲，本讲只需知道它给出 \(d_{\text{ref}}\)）。渲染时，则完全信任网络输出 \(d_{\text{model}}\)，用球面追踪把它变成图像。

还有一个**坐标归一化**的约定：`load_mesh` 会把网格顶点缩放到 \([0,1]^3\) 立方体内（除以 `mesh_scale` 再平移），这样所有距离常数都不必再带包围盒系数。SDF 的 AABB 也就基本是单位立方体。

#### 4.1.2 核心流程

SDF 原语的数据流：

```
三角网格(.obj/.stl)
   │  load_mesh: 归一化到 [0,1]^3，建 TriangleBvh / TriangleOctree
   ▼
训练阶段: generate_training_samples_sdf
   │  在表面 / 近表面 / 全空间采样点，用 BVH 查真值距离
   ▼
train_sdf: MLP(坐标) → 预测距离，与真值做损失(MAPE)，反传更新
   │
渲染阶段: render_sdf
   │  从相机发射光线 → SphereTracer 用网络距离球面追踪 → 命中点
   ▼
shade_kernel_sdf: 用法线 + 阴影 + BRDF 算颜色 → 帧缓冲
```

#### 4.1.3 源码精读

SDF 网络的输入输出维度只有 3 进 1 出：

[src/testbed_sdf.cu:45-51](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L45-L51) —— `network_dims_sdf` 声明 `n_input=3, n_output=1, n_pos=3`。这决定了 `reset_network` 构造的 MLP 是「3→1」的标量回归器。

加载网格时的坐标归一化（关键 4 行）：

[src/testbed_sdf.cu:1396-1398](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1396-L1398) —— 把每个顶点变换到 \([0,1]^3\)：先减去包围盒中心、除以 `mesh_scale`，再加 0.5。

SDF 的网络配置（默认用 HashGrid 编码 + FullyFusedMLP）：

[configs/sdf/base.json:23-36](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/sdf/base.json#L23-L36) —— 注意 `loss` 用 `MAPE`（平均绝对百分比误差），这对距离这种跨多个数量级的量更稳健。

#### 4.1.4 代码实践

1. **实践目标**：确认 SDF 网络是「3 进 1 出」的标量回归器，并理解它的损失函数。
2. **操作步骤**：
   - 打开 [configs/sdf/base.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/sdf/base.json#L1-L37)，记录 `encoding`（HashGrid, 16 层）、`network`（FullyFusedMLP, 64 神经元, 2 隐藏层）、`loss`（MAPE）。
   - 对照 [network_dims_sdf](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L45-L51)。
3. **需要观察的现象**：网络输出只有 1 维，与 NeRF 的 4 维（RGB+密度）形成对比。
4. **预期结果**：你能用一句话说出「SDF 网络把 3D 坐标映射成 1 个距离标量」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 SDF 的损失用 `MAPE` 而不是普通的 L2？
**答案**：距离值跨越很多数量级（表面附近接近 0，远处可能很大）。MAPE 对相对误差敏感，能避免远处大距离点的绝对误差淹没表面附近的小距离点；L2 会被大值主导，导致表面（\(d\approx 0\)，最关键的区域）拟合不准。

**练习 2**：SDF 网络的符号约定是「内为负」吗？依据是哪段代码？
**答案**：是。[compare_signs_kernel](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L553-L554) 中 `bool inside1 = distances_ref[i] <= 0.f;`，即距离 ≤ 0 视为「在内部」。

---

### 4.2 SphereTracer：球面追踪主循环（核心模块）

#### 4.2.1 概念说明

`SphereTracer` 是 `Testbed` 的内嵌类（[testbed.h:95-161](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L95-L161)），负责把「一束相机光线」推进到「命中表面」。它解决的核心问题是：**不同光线命中的快慢差别极大**——有的光线几步就撞上近处表面，有的要走过大半个场景才命中或逃出。如果让所有光线都走最大步数，会浪费大量算力。

instant-ngp 的解法是 **「步进 + 压缩（compaction）」**：定期把已经终止（命中或逃出）的光线从活跃列表里剔除，挪到一个「命中缓冲」，让后续只对仍存活的光线做查询。这就是 GPU 上的「光线紧凑化」，类似 u4 里 NeRF 的光线压缩。

#### 4.2.2 核心流程

球面追踪主循环（伪代码）：

```
trace(distance_function, network, zero_offset, distance_scale, maximum_distance, aabb, ...):
    n_alive = 初始光线数
    i = 1
    while i < MARCH_ITER(=10000):
        step_size = min(i, 4)              # 早期压缩更频繁
        # —— 推进一步（两种实现之一）——
        if 启用融合内核:
            trace_sdf.cuh: 每线程一线程内 while 循环，
                           反复 (eval_sdf - zero_offset)*distance_scale 推进
        # （非融合路径：distance_function 查距离 + advance_pos_kernel_sdf 推进，循环 step_size 次）
        # —— 压缩 ——
        alive 光线  -> 缓冲前部（继续）
        终止光线    -> m_rays_hit（命中/逃出）
        n_alive = 剩余存活数
        if n_alive == 0: break
        i += step_size
    return 命中数 n_hit
```

每条光线在每一步的关键计算：

\[ d_{\text{step}} = \big(\,d_{\text{model}}(\mathbf{p}) - \text{zero\_offset}\,\big) \times \text{distance\_scale} \]

然后光线原点前进 \(d_{\text{step}}\)。**存活条件**（见源码）要求预测距离仍大于 `maximum_distance`（默认 `0.00005`），否则视为命中。

#### 4.2.3 源码精读

**距离函数与法线函数的类型别名**——它们都是「输入位置数组、输出距离/法线数组」的可调用对象：

[testbed.h:92-93](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L92-L93) —— `distance_fun_t` / `normals_fun_t`。在渲染时，`distance_function` 要么是「网络推理」、要么是「BVH 真值」（后者用于 ground truth 渲染），见 [testbed.cu:4945-4955](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4945-L4955)。

**`trace()` 的主循环**——压缩频率随步数增长：

[src/testbed_sdf.cu:816-825](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L816-L825) —— `MARCH_ITER=10000` 是单次追踪的步数上限（安全阀），`STEPS_INBETWEEN_COMPACTION=4` 是两次压缩之间最多走的步数；`step_size=min(i,4)` 让前几步压缩得更勤，因为初期光线分歧最大、能尽快剔除已命中的。

**融合内核推进（单线程负责一条光线）**：

[trace_sdf.cuh:60-104](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/trace_sdf.cuh#L60-L104) —— 这是 JIT 生成的内核（`eval_sdf` 由网络在运行时拼接，详见 u8-l2），其 `while(true)` 内部第一步就是查询距离：

[trace_sdf.cuh:61](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/trace_sdf.cuh#L61) —— `dist = ((float)eval_sdf(ray.o, params)[0] - zero_offset) * distance_scale;` 这一行是整个球面追踪的核心：取网络输出、减偏移、乘缩放，得到本步安全步长。

[trace_sdf.cuh:103](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/trace_sdf.cuh#L103) —— 存活条件 `alive &= dist > maximum_distance && fabsf(dist/2) > 3*maximum_distance && aabb.contains(ray.o);` 一旦预测距离跌到阈值以下，或光线走出包围盒，光线就终止（命中或逃出）。

> 注意 `__all_sync(0xFFFFFFFF, !alive)`（[第 64 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/trace_sdf.cuh#L64)）：同一 warp 内**所有线程都必须一起执行 `eval_sdf`**（否则 MLP 的 warp 级内核会失配），已终止的线程只是 `continue` 跳过推进逻辑，但 MLP 查询照跑。这与 NeRF 融合内核的 `__all_sync` 约束同源（u4-l3）。

**非融合路径的推进 kernel**——逻辑与融合内核完全一致，只是拆成两步：

[src/testbed_sdf.cu:183-189](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L183-L189) —— 读 `distances[i]`（已被 `distance_function` 填成网络原始输出），减 `zero_offset`、乘 `distance_scale`，再 `pos += distance * dir`。

[src/testbed_sdf.cu:218-227](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L218-L227) —— 同样的存活判定与「走出 aabb 则终止」。

**压缩 kernel**——把存活光线紧凑排到缓冲前部、命中光线收集到 `m_rays_hit`：

[src/testbed_sdf.cu:456-490](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L456-L490) —— `compact_kernel_sdf` 用 `atomicAdd` 计数器把存活光线写入 `dst_*`、命中的光线写入 `dst_final_*`。命中光线的 `distance` 被设成 `1.0f`（注释写明 `HACK: Distances encode shadowing factor when shading`——着色阶段用这个槽位存阴影因子）。

**光线的负载结构**：

[sdf_device.cuh:23-28](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/sdf_device.cuh#L23-L28) —— `SdfPayload` 含 `dir`（方向）、`idx`（对应像素）、`n_steps`（累计步数，用于 AO 与 Cost 渲染）、`alive`（是否存活）。`n_steps` 在着色时被用来做环境遮蔽（AO）：步数越多说明光线绕过的空腔越深，越暗。

**`SphereTracer` 类的成员**：

[testbed.h:148-160](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L148-L160) —— 双缓冲 `m_rays[2]`（压缩时在两块间倒）、命中缓冲 `m_rays_hit`、`m_alive_counter`/`m_hit_counter` 两个原子计数器、可选的 `m_fused_trace_kernel`。

#### 4.2.4 代码实践（本讲主实践）

1. **实践目标**：读懂球面追踪每一步如何用网络距离推进光线，并理解 `distance_scale` / `zero_offset` 如何影响收敛。
2. **操作步骤**：
   - 打开 [trace_sdf.cuh:60-104](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/trace_sdf.cuh#L60-L104)，按顺序标出每轮 `while` 做了什么：① 第 61 行算 `dist`；② 第 64 行 warp 同步；③ 第 74 行 `ray.o = ray(dist)` 推进；④ 第 103 行判定是否存活。
   - 对照非融合版本 [advance_pos_kernel_sdf:183-227](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L183-L227)，确认两者用的是同一个公式 `dist = (raw - zero_offset) * distance_scale`。
   - 在 GUI 中加载 `./instant-ngp data/sdf/armadillo.obj`，打开 Rendering 面板找到 SDF 专有的 `distance scale` 与 `zero offset` 滑杆（对应 `m_sdf.distance_scale` / `m_sdf.zero_offset`）。
3. **需要观察的现象**：
   - 把 `distance_scale` 从 0.95 调大到接近 1（甚至 >1）：渲染**可能更快**（步数变少），但表面可能出现「穿透」或噪点——因为学习到的 SDF 只是近似，不严格满足「半径内无表面」的保证，步子太大就会跨过薄壁。
   - 把 `distance_scale` 调小（如 0.5）：渲染**变慢**（步数增多，可切到 `Cost` 渲染模式看每像素步数变亮），但更稳健。
   - 调 `zero_offset`：表面会**膨胀或收缩**，因为命中的等值面从 \(d=0\) 变成了 \(d=\text{zero\_offset}\)。
4. **预期结果**：你能解释「`distance_scale` 控制步长（越小越慢但越安全）；`zero_offset` 控制提取哪一层等值面（默认 0 即训练的表面）」。
5. **若无法本地运行**：待本地验证。可改为纯源码阅读——在 [trace_sdf.cuh:61](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/trace_sdf.cuh#L61) 与 [advance_pos_kernel_sdf:183-185](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L183-L185) 两处确认公式一致，并说明为何 `distance_scale<1` 能降低「跨过表面」的风险。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `step_size = min(i, 4)` 要让前几步压缩更频繁？
**答案**：球面追踪初期，不同光线命中的快慢差异最大——正对近处表面的光线一两步就终止，掠射远处的光线要走很久。早期频繁压缩能尽快把这些「快光线」剔除出活跃列表，让后续只处理真正需要长距离推进的光线，避免对已终止光线做空查询。

**练习 2**：`MARCH_ITER = 10000` 这个常量起什么作用？设得太大或太小有什么问题？
**答案**：它是单条光线在单次 `trace()` 调用内的**步数上限**，是一个安全阀，防止某条光线因网络误差陷入「永远到不了表面也走不出 AABB」的死循环。设太小，远处/掠射光线会被提前截断、渲染出现空洞；设太大，异常情况下会浪费算力。源码见 [testbed_sdf.cu:43](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L43)。

**练习 3**：融合内核里为什么必须用 `__all_sync` 让整个 warp 一起执行 `eval_sdf`？
**答案**：底层 FullyFusedMLP 是按 warp（32 线程）协同执行的内联内核，要求 warp 内所有线程同步参与。若已终止的线程提前退出、不参与 MLP 查询，会导致 warp 内执行路径不一致、寄存器/共享内存布局错乱。所以用 `__all_sync(0xFFFFFFFF, !alive)` 在 warp 全部终止时才整体跳出，否则即使本线程已终止也要陪着跑 MLP（见 [trace_sdf.cuh:64-72](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/trace_sdf.cuh#L64-L72)）。

---

### 4.3 train_sdf：在线采样与训练

#### 4.3.1 概念说明

SDF 训练有一个独特之处：**训练样本是「在线」生成的**。不像图像/NeRF 有固定的数据集，SDF 的真值距离可以随时随地用网格 BVH 查询，所以每一步训练前都可以现采一批新点。这由 `training.generate_sdf_data_online`（默认 `true`）控制。

样本不是均匀撒在整个空间——那样绝大多数点会落在「离表面很远」的地方，对学习表面细节没帮助。instant-ngp 把一批样本分成**三类**，重点覆盖表面附近。

#### 4.3.2 核心流程

`generate_training_samples_sdf` 把 `n_to_generate` 个样本按 1/8 为单位切成三类（见源码 1450-1455 行）：

| 类别 | 占比 | 生成方式 | 真值距离 |
| --- | --- | --- | --- |
| 表面精确点（surface_exact） | 4/8 | 按三角形面积加权，在网格表面均匀采点 | 恒为 0（在表面上） |
| 表面扰动点（surface_offset） | 3/8 | 在表面点上加一个 logistic 随机扰动 | BVH 查询 |
| 全空间均匀点（uniform） | 1/8 | 在 AABB（或八叉树叶子）内均匀采样 | BVH 查询 |

训练循环 `train_sdf` 则很标准：打乱样本顺序去相关，把位置矩阵喂给 `Trainer::training_step`，用 MAPE 损失反传。

\[ \text{loss} = \text{MAPE}\big(d_{\text{model}}(\mathbf{p}),\ d_{\text{ref}}\big) \]

#### 4.3.3 源码精读

**三类样本的配比**——以 1/8 为基本单位：

[src/testbed_sdf.cu:1450-1455](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1450-L1455) —— `n_to_generate_base = n/8`，`surface_exact = 4*base`、`surface_offset = 3*base`、`uniform = 1*base`。

**表面精确点的距离直接置零**（无需查 BVH）：

[src/testbed_sdf.cu:1472](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1472) —— `cudaMemsetAsync(distances, 0, ...)`，因为这些点本就在表面上，\(d=0\)。

**表面扰动点**——先采样再叠加扰动，距离用扰动长度做上界加速 BVH 查询：

[src/testbed_sdf.cu:232-245](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L232-L245) —— `perturb_sdf_samples` 把扰动加到位置上，并把距离设为 `length(perturbation)*1.001`（略大于真实扰动，作为 BVH 查询的上界以加速）。扰动幅度由 `stddev = bounding_radius/1024 * surface_offset_scale` 控制（[第 1477 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1477)）。

**真值距离由 BVH 查询**：

[src/testbed_sdf.cu:1524-1532](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1524-L1532) —— `triangle_bvh->signed_distance_gpu(...)` 用 `mesh_sdf_mode`（Watertight/Raystab/PathEscape，详见 u5-l2）计算到网格的有向距离。

**训练准备与训练本体**：

[src/testbed_sdf.cu:1621-1631](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1621-L1631) —— `training_prep_sdf`：当开启在线生成时，每步调用 `generate_training_samples_sdf` 产新数据。

[src/testbed_sdf.cu:1578-1619](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1578-L1619) —— `train_sdf`：打乱（`shuffle`）后构造位置/目标矩阵，调 `m_trainer->training_step`，累加损失标量。注意 SDF 训练**不需要像 NeRF 那样沿光线采样**——它就是普通的「坐标→标量」回归。

#### 4.3.4 代码实践

1. **实践目标**：理解三类样本的配比，以及「在线生成」开关的效果。
2. **操作步骤**：
   - 读 [generate_training_samples_sdf:1449-1535](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1449-L1535)，按代码算出：当 `target_batch_size = 1<<20`（约 100 万）时，三类各多少点？（答：表面精确 52.4 万、表面扰动 39.3 万、均匀 13.1 万。）
   - 在 GUI 的 Rendering/SDF 面板里找到「online」生成开关（对应 `generate_sdf_data_online`），关闭后再训练，观察现象。
3. **需要观察的现象**：关闭在线生成后，训练数据不再刷新（`training.size` 固定），模型只在一批固定点上拟合，损失很快停在一个不理想的水平，远处表面会出现明显错误。
4. **预期结果**：你能解释「在线采样让模型每步都见到新鲜点，避免对固定点过拟合、更好覆盖整个空间」。
5. **若无法本地运行**：待本地验证。可改为阅读 `calculate_iou`（[第 1636-1680 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1636-L1680)），说明它如何用同一套采样 + `compare_signs_kernel` 统计内外符号一致率来算 IoU。

#### 4.3.5 小练习与答案

**练习 1**：为什么不把所有训练点都均匀撒在空间里？
**答案**：均匀撒点会让绝大多数点远离表面，而 SDF 最关键、最难学的是表面附近的高频细节（曲面走向）。按 7:1 的比例把大部分点（表面精确 + 表面扰动）集中在表面附近，能让有限的训练预算花在刀刃上；只留 1/8 均匀点保证全局内外符号正确。

**练习 2**：`surface_offset_scale` 这个参数调大会有什么效果？
**答案**：它放大表面扰动点的扰动标准差 `stddev`（[第 1477 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1477)）。调大后，扰动点会分布到离表面更远的「壳层」，训练目标覆盖更厚的表面邻域，有助于学习离表面稍远处的距离梯度；但过大可能稀释对精确表面的拟合。

---

### 4.4 法线、阴影与着色（延伸模块）

球面追踪只给出「命中点的位置」，要变成有光照的图像还需要法线、阴影和着色。

#### 4.4.1 概念说明

- **法线**：SDF 的梯度 \(\nabla d\) 即表面法线。本项目支持两种法线——解析法线（直接对网络求输入梯度 `m_network->input_gradient`，见 [testbed.cu:4961-4965](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4961-L4965)）和**有限差分（FD）法线**（默认，用 6 次网络查询做中心差分）。
- **软阴影**：朝太阳方向再发一条阴影光线做球面追踪，途中用 iquilezles 的软阴影公式累计最小可见度。
- **着色**：用 Disney BRDF（`evaluate_shading`）结合法线、光照、AO（来自主光线的 `n_steps`）算颜色。

FD 法线的中心差分公式（归一化后不需要除以 \(2\varepsilon\)）：

\[ \mathbf{n} \;\propto\; \big(d(\mathbf{p}+\varepsilon\mathbf{e}_x)-d(\mathbf{p}-\varepsilon\mathbf{e}_x),\;\; d(\mathbf{p}+\varepsilon\mathbf{e}_y)-d(\mathbf{p}-\varepsilon\mathbf{e}_y),\;\; d(\mathbf{p}+\varepsilon\mathbf{e}_z)-d(\mathbf{p}-\varepsilon\mathbf{e}_z)\big) \]

#### 4.4.2 核心流程

`render_sdf` 的整体编排：

```
1. 初始化 SphereTracer，从相机发射光线
2. trace() 球面追踪 → 得到命中点
3. 算法线（解析 or FD）
4. 若 Shade 模式：再发阴影光线 trace() → 得到阴影可见度
5. shade_kernel_sdf：法线 + 阴影 + BRDF + AO → 像素颜色
```

#### 4.4.3 源码精读

**FD 法线**——沿三轴各正负偏移 ε 查询，做差：

[src/testbed_sdf.cu:1066-1106](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1066-L1106) —— `FiniteDifferenceNormalsApproximator::normal`。第 1103 行把六个查询结果组装成法线向量（未归一化，着色时再 `normalize`）。偏移量 `fd_normals_epsilon` 默认 `0.0005`（[testbed.h:902](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L902)）。

**软阴影**——在主追踪命中后，朝太阳方向再发一批阴影光线：

[src/testbed_sdf.cu:93-101](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/trace_sdf.cuh#L93-L101) —— 阴影光线的可见度公式（iquilezles 软阴影）：`min_vis = min(min_vis, k*d/max(0, total_dist-y))`，其中 `k = shadow_sharpness`（默认 2048，[testbed.h:900](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L900)）控制阴影边界的锐利程度。

[src/testbed_sdf.cu:1240-1283](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1240-L1283) —— `render_sdf` 中阴影光线的发射与结果回收（`prepare_shadow_rays` → 第二次 `trace()` → `write_shadow_ray_result`）。

**着色**——多种渲染模式由 `ERenderMode` 切换：

[src/testbed_sdf.cu:324-403](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L324-L403) —— `shade_kernel_sdf`。`Shade` 模式调 Disney BRDF（`evaluate_shading`），并把 `distances[i]` 当作阴影因子（注释 `Distance encodes shadow occlusion. 0=occluded, 1=no shadow`）；`AO` 模式用 `pow(0.92, n_steps)` 模拟环境遮蔽；`Cost` 模式按 `n_steps` 染色（步数=开销）；`Normals` 直接可视化法线。

#### 4.4.4 代码实践

1. **实践目标**：通过切换渲染模式，直观看到法线、AO、阴影、开销。
2. **操作步骤**：加载 `data/sdf/armadillo.obj`，训练到收敛，依次按数字键切换渲染模式（键 1=AO，键 2=Shade，键 5=Normals，键 7=Cost；详见 u1-l5 的键盘表，对应 [ERenderMode 枚举](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common.h#L68-L80)）。
3. **需要观察的现象**：
   - `Cost` 模式下，正对相机的平坦区域偏暗（步数少），掠射边缘和高频细节处偏亮（步数多）。切到 `Cost` 时控制台还会打印 `Total steps per hit = ...`（见 [render_sdf 第 1313-1325 行](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1313-L1325)）。
   - `Shade` 模式下能看到软阴影与地板棋盘格。
4. **预期结果**：你能把每个渲染模式对应到 `shade_kernel_sdf` 里的一个 `case`。
5. **若无法本地运行**：待本地验证。可改为阅读 `shade_kernel_sdf` 的 switch，列出每种 `ERenderMode` 各算什么颜色。

#### 4.4.5 小练习与答案

**练习 1**：FD 法线为什么要查 6 次网络而不是 3 次？
**答案**：中心差分需要每个轴的正负两侧各一次查询，三轴共 \(3\times2=6\) 次。若只用前向差分（3 次）会引入一阶偏差，中心差分更对称、精度更高。见 [FD normal 的六次查询](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1078-L1091)。

**练习 2**：`shadow_sharpness`（k）调大，阴影会变怎样？
**答案**：`k` 越大，软阴影公式 `k*d/(total-y)` 对「光线擦过障碍物」越敏感，阴影边界越锐利、越硬；`k` 越小阴影越柔和、越模糊。

---

## 5. 综合实践

**任务**：把球面追踪的「步进—终止—着色」整条链路串起来，并用 `Cost` 模式定量观察 `distance_scale` 对收敛步数的影响。

**步骤**：

1. 编译并运行 `./instant-ngp data/sdf/armadillo.obj`（构建方法见 u1-l3）。
2. 让它训练到损失趋于稳定（GUI 顶部的 loss 曲线变平）。
3. 切到 `Cost` 渲染模式（数字键 7），记录控制台打印的 `Total steps per hit` 平均值（[源码](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1313-L1325)）。
4. 在 Rendering/SDF 面板把 `distance_scale` 从默认 `0.95` 调到 `0.5`，再读一次 `Total steps per hit`；然后调到 `0.99`，再读一次。
5. 切回 `Shade` 模式（键 2），在 `distance_scale=0.99` 下观察犰狳的薄壁（如耳朵边缘）是否出现穿透或噪点。

**要回答的问题**：

- `distance_scale` 减半时，平均步数大约翻了几倍？这与「步长 ∝ distance_scale」的预期是否一致？
- `distance_scale` 接近 1 时，为什么薄壁处更容易出错？（提示：学习的 SDF 是近似值，不严格满足「半径内无表面」。）
- 把 `zero_offset` 从 0 调到一个小正值，表面是膨胀还是收缩？为什么？（提示：命中条件变成网络输出 = `zero_offset`，而网络在表面外输出正值。）

**预期结论**：`distance_scale` 是「速度 vs 鲁棒性」的旋钮——越小越慢越安全，越大越快但越易穿透薄壁；`zero_offset` 则平移了被提取的等值面。两者共同决定了球面追踪「在哪、以多大步子」收敛。

> 若本地无 GPU 或无法编译：改为纯源码阅读实践。通读 [render_sdf](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1108-L1326)，画一张时序图，标出「初始化光线 → trace 命中 → 法线 → 阴影光线 → 着色」五个阶段各自调用的 kernel，并标注 `distance_scale`/`zero_offset` 在哪两个 kernel 里被消费。

---

## 6. 本讲小结

- **SDF 原语**用一个 3 进 1 出的 MLP 拟合「空间到表面的有向距离」，符号约定为内负外正、表面为零；网络由 `configs/sdf/base.json` 构造，损失用对相对误差敏感的 MAPE。
- **球面追踪**是 SDF 的渲染算法：沿光线反复用网络距离「安全推进」，靠近表面时步长自然变小，直到距离小于 `maximum_distance` 即命中。
- **`SphereTracer::trace`** 用「步进 + 双缓冲压缩」应对光线命中快慢的巨大差异：定期把已终止光线剔到 `m_rays_hit`，只对存活光线继续查询；`MARCH_ITER` 是步数安全阀。
- 推进的核心公式是 `dist = (网络输出 - zero_offset) * distance_scale`：`distance_scale` 控制步长（越小越慢越安全），`zero_offset` 控制提取哪一层等值面。
- **`train_sdf` 在线生成样本**，按 4:3:1 分配「表面精确 / 表面扰动 / 全空间均匀」三类点，把训练预算集中在表面附近；真值距离由 `TriangleBvh` 查询。
- 渲染所需的**法线**（解析或有限差分）、**软阴影**（iquilezles 阴影光线）、**着色**（Disney BRDF + 来自 `n_steps` 的 AO）在 `render_sdf` 里依次接上，由 `ERenderMode` 切换可视化。

---

## 7. 下一步学习建议

- **u5-l2 网格 BVH、八叉树与 OptiX**：本讲的「真值距离」全部来自 `TriangleBvh::signed_distance_gpu`，下一讲深入讲解三种 `EMeshSdfMode`（Watertight/Raystab/PathEscape）的差别，以及 `TriangleOctree` 如何既提供 Takikawa 编码、又用于 SDF 的空间加速。
- **u8-l2 JIT 融合与全融合内核**：本讲反复出现的 `trace_sdf.cuh` 是 JIT 生成的融合内核，`eval_sdf` 由 `generate_device_function` 在运行时拼接。若想彻底理解「为什么融合内核要把编码+MLP 拼成一个设备函数」，请读 u8-l2。
- **对照 u4 NeRF**：建议回看 u4-l3 的体渲染，对比「体密度 alpha 合成」与「SDF 球面追踪」两种隐式场渲染范式的根本差异——前者软、密采样；后者硬、自适应大步跳跃。
- **源码延伸**：若对 SDF 的几何应用感兴趣，可继续阅读 [get_sdf_gt_on_grid](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1550-L1576)（在 3D 网格上采真值距离）与 u6-l4 的 Marching Cubes，看 SDF 如何被提取成可导出的三角网格。
