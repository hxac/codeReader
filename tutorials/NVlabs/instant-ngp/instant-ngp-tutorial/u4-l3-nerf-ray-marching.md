# NeRF 光线步进与体渲染

## 1. 本讲目标

上一篇（u4-l2）我们拆开了 `NerfNetwork` 的双头结构：给它「一个空间点 + 一个观察方向」，它吐出 4 维 `[R,G,B,σ]`。但**单看一个点**是出不了画的——要渲染一张图，得对每个像素沿一条视线**连续采样很多个点**，再把它们的颜色按「谁离相机近、谁更不透明」加权叠起来。这就是**体渲染（volume rendering）**。

本讲解决三件事：

1. 一根光线是如何从相机发射出来、怎样沿光线一步步向前「步进（ray marching）」并对 `NerfNetwork` 取样的——由 `NerfTracer` 和融合内核 `render_nerf` 负责。
2. 一串采样点的颜色如何被合成为最终像素颜色——即 **alpha 合成 / 透射率（transmittance）累积**，以及 `render_min_transmittance` 如何提前终止光线省算力。
3. 步距为何「越远越大」（圆锥步距 `cone_angle_constant`），以及大场景为何需要**多级联（cascade）**来覆盖。

学完后你应当能：画出从相机到像素 RGBA 的完整管线；解释体渲染积分的离散形式与提前终止；说清 `cone_angle_constant` 和 `max_cascade` 各自的作用。

## 2. 前置知识

- **体数据 / 不透明度**：NeRF 把场景表达成一个连续的「颜色云 + 密度云」。密度 σ 越大，表示这里越「实」、越挡光。光穿过时一路被吸收、一路染色，最终到你眼睛的颜色，就是沿途所有点颜色的加权平均。
- **透射率 transmittance \(T\)**：光线走到某处「还剩多少光没被挡住」。起点 \(T=1\)（全透），每经过一小段就被吸收掉一部分，\(T\) 单调下降到 0（全挡死）。
- **光线步进 ray marching**：因为密度场是神经网络，没法解析积分，只能沿光线**离散采样**：取一段算一段，累加。
- **前置讲义**：本讲依赖 u4-l2（`NerfNetwork` 双头）与 u3（哈希编码）。 NerfNetwork 的输入 `NerfCoordinate`（位置 + 方向 + 步长 `dt`）正是本讲光线步进每一步要喂进去的东西。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `include/neural-graphics-primitives/fused_kernels/render_nerf.cuh` | **融合渲染内核**：一个 CUDA 设备函数，每个像素一个线程，端到端完成「发射光线 → 步进 → 查网络 → alpha 合成」。这是标准 `Shade` 模式下的快速路径。 |
| `src/testbed_nerf.cu` | NeRF 渲染的**主机端编排**：`Testbed::render_nerf()` 决定走融合内核还是 `NerfTracer`；`NerfTracer::init_rays_from_camera()` 生成光线，`NerfTracer::trace()` 跑步进循环。 |
| `include/neural-graphics-primitives/testbed.h` | `NerfTracer` 类声明、`render_nerf` 方法声明，以及 `m_nerf` 中的关键参数 `cone_angle_constant` / `max_cascade` / `render_min_transmittance`。 |
| `include/neural-graphics-primitives/nerf_device.cuh` | 步进几何的**纯函数工具集**：`calc_cone_angle` / `calc_dt` / `advance_n_steps` / `if_unoccupied_advance_to_next_occupied_voxel`，以及密度网格位域查询 `density_grid_occupied_at`。 |

## 4. 核心概念与源码讲解

### 4.1 体渲染原理与 alpha 合成

#### 4.1.1 概念说明

把一根光线沿参数 \(t\)（距相机的距离）离散成 \(N\) 个采样点，第 \(i\) 个点处网络给出颜色 \(c_i\) 和密度 \(\sigma_i\)，该段长度为 \(\delta_i\)（代码里的 `dt`）。经典 NeRF 体渲染方程的离散形式为：

\[
C=\sum_{i=1}^{N} T_i\,\alpha_i\,c_i,
\qquad
T_i=\exp\!\left(-\sum_{j=1}^{i-1}\sigma_j\,\delta_j\right),
\qquad
\alpha_i = 1-\exp(-\sigma_i\,\delta_i)
\]

直观上：

- \(\alpha_i\) 是「这一小段有多不透明」，由密度 × 步长决定，密度越大、步长越长，\(\alpha\) 越接近 1。
- \(T_i\) 是「光线走到第 \(i\) 段还剩多少光」，等于前面所有段的不透明度连乘取反，即 \(T_i=\prod_{j<i}(1-\alpha_j)\)。
- 最终颜色 = 每段颜色 ×「这段的不透明度」×「到这段时还剩的光」，全部相加。

代码里不显式维护 \(T_i\)，而是维护**累积不透明度** `color.a`（记作 \(a\)），它与透射率互补：\(T_i = 1 - a_{i-1}\)。这样递推写起来最省事。

#### 4.1.2 核心流程

把上面的离散积分翻成递推伪代码：

```
color = (0,0,0,0)          # rgb 是预乘 alpha 的累积色, a 是累积不透明度
for 每个采样点 i:
    σ  = network_to_density(nerf_out.w)   # 第 4 维 → 密度
    α  = 1 - exp(-σ * dt)                 # 这一段的 alpha
    T  = 1 - color.a                      # 剩余透射率
    w  = α * T                            # 这一段对像素的实际贡献权重
    color.rgb += c_i * w
    color.a   += w                        # 累积不透明度
    if color.a > 1 - τ:                   # 剩余透射率 < τ, 提前终止
        normalize(color); break
```

这里的关键常数 \(\tau =\) `render_min_transmittance`（默认 `0.01`）。它的物理含义是：**当剩余透射率低于 1% 时，再往后的采样点最多只能给像素贡献 1% 的颜色，不值得再算了，于是提前结束这根光线。** 这是 instant-ngp 在渲染时最重要的一处省算力优化。

#### 4.1.3 源码精读

融合内核里的合成就三行，把上面的递推逐字翻译了一遍：

[include/neural-graphics-primitives/fused_kernels/render_nerf.cuh:146-150](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/render_nerf.cuh#L146-L150) — `alpha = 1 - exp(-密度*dt)`，`weight = alpha * (1-color.a)`，`color += (rgb*weight, weight)`。这就是体渲染积分的一次累加。

提前终止的判断在循环末尾：

[include/neural-graphics-primitives/fused_kernels/render_nerf.cuh:163-166](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/render_nerf.cuh#L163-L166) — 当 `color.a > 1 - min_transmittance` 时，把颜色除以 `color.a` 归一化、标记光线死亡 `alive=false`，跳出步进循环。`min_transmittance` 实参由 [include/neural-graphics-primitives/testbed.h:890](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L890) 的 `render_min_transmittance` 提供（默认 `0.01`）。

非融合路径 `NerfTracer` 里同样的合成逻辑写在主机端编排的 `composite_kernel_nerf` 中：

[src/testbed_nerf.cu:631-680](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L631-L680) — `T = 1 - local_rgba.a`、`alpha = 1-exp(-密度*dt)`、`weight = alpha*T`、`local_rgba += (rgb*weight, weight)`，以及同样的 `if (local_rgba.a > 1 - min_transmittance)` 提前 `break`。两条路径数学完全一致，只是组织方式不同。

> 小贴士：循环正常结束（光线穿出场景）时颜色是**预乘 alpha** 的（rgb 已被透射率加权），所以内核末尾会把背景环境贴图按 `1-color.a` 的剩余透射率补上：[render_nerf.cuh:179-181](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/render_nerf.cuh#L179-L181)。

#### 4.1.4 代码实践

**实践目标**：验证「密度越大、提前终止越早」这件事，并量化 `render_min_transmittance` 的省算力效果。

1. 打开 [render_nerf.cuh](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/render_nerf.cuh)，定位 L147（算 `alpha`）、L148（算 `weight`）、L163（提前终止）。
2. **阅读型跟踪**：假设一根光线前几段密度极大（\(\sigma\to\infty\)），则第一段 \(\alpha\approx1\)、`color.a≈1`，立刻触发 L163 终止——这解释了为什么 solid 物体内部的光线几乎一步就停。反过来，若整条光线都在低密度区，`color.a` 涨不上来，光线会一直步进到穿出包围盒（`t >= MAX_DEPTH()`）。
3. **改参数观察**：`render_min_transmittance` 通过 `m_nerf.render_min_transmittance` 控制（testbed.h:890）。把它从 `0.01` 调大到 `0.1`（剩余透射率 10% 就停），渲染会更快但更朦胧（背景透出来更多）；调小到 `0.001` 则更精确但更慢。**待本地验证**：该字段是否直接暴露给 pyngp 请见 u7 的 pybind11 绑定（`src/python_api.cu`）；若未直接暴露，可通过改用更大 `aabb_scale`（间接改变光线步数）或对照 `cone_angle_constant` 来观察提前终止对渲染耗时的影响。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `weight = alpha * (1 - color.a)` 而不是直接 `weight = alpha`？
**答**：因为远处的不透明段会被近处已经累积的不透明度「挡住」。`(1-color.a)` 正是当前剩余透射率 \(T\)，乘上去才符合体渲染方程 \(T_i\alpha_i c_i\) 的物理意义；直接用 `alpha` 会把被遮挡的远段也算成满贡献。

**练习 2**：`render_min_transmittance` 设成 `0` 会怎样？
**答**：理论上 `1 - color.a > 0` 恒成立（除非 \(\alpha\) 严格等于 1），光线永远不会因不透明度终止，会一直步进到穿出包围盒，计算量大幅增加、画面却几乎不变。

---

### 4.2 NerfTracer：光线的生成、批步进与压缩

#### 4.2.1 概念说明

`render_nerf.cuh` 的融合内核是「一像素一线程」端到端跑完一根光线。但有些渲染模式（法线 `Normals`、深度 `Depth`、位置 `Positions`、AO、编码可视化，或关掉 JIT 融合时）**不能**用这个融合内核——因为它们要额外调用网络的梯度或可视化接口。这时 `Testbed::render_nerf()` 会改走 `NerfTracer` 这条**两段式**路径：

1. **生成光线**：`init_rays_from_camera()` 为每个像素算出一根 `Ray{origin, dir}`，并用密度网格跳到第一个非空体素。
2. **迭代步进**：`trace()` 循环——把「还活着」的光线**压缩（compaction）**到紧凑数组，再批量喂给网络做推理，最后合成。

为什么非要用压缩？因为步进过程中不同光线「死亡」的时刻不同（不透明物体一步就停，空旷区域要走很远）。如果不压缩，已经死掉的光线仍占着线程空转，浪费算力。`NerfTracer` 每隔几步把活光线紧凑重排一次，让 GPU 只对活光线做昂贵的 MLP 推理。

#### 4.2.2 核心流程

`NerfTracer::trace()` 的主循环（伪代码）：

```
n_alive = 像素总数
i = 1
while i < MARCH_ITER(=10000):
    # 1) 压缩：把活光线紧凑搬到 rays_current，死光线搬到 rays_hit
    n_alive = compact_kernel_nerf(...)
    if n_alive == 0: break

    # 2) 估算步数：每批要跑几步，让总查询数 ≈ 2M 以打满 GPU
    n_steps = clamp(2M / n_alive, min, max)

    # 3) 生成这 n_steps 步的网络输入（沿光线往前跳，跳过空体素）
    generate_next_nerf_network_inputs(...)   # 每条光线产出 n_steps 个 NerfCoordinate

    # 4) 批量 MLP 推理（一次算 n_alive * n_steps 个点）
    network->inference_mixed_precision(...)

    # 5) 合成：把这 n_steps 步的颜色累加进 rgba，命中阈值则标记死亡
    composite_kernel_nerf(...)

    i += n_steps
```

注意第 2 步：`n_steps_between_compaction` 会随活光线数 `n_alive` 动态调整。活光线多时少跑几步就压缩一次（防止压缩太频繁）；活光线少（大部分已死）时一次多跑几步（攒够 ~200 万个查询再算，让 GPU 吃饱）。

#### 4.2.3 源码精读

**主机端总入口**，决定走哪条路径：

[src/testbed_nerf.cu:1894-1908](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L1894-L1908) — `Testbed::render_nerf()` 的签名，参数里带了两套相机矩阵（`camera_matrix0/1`，用于滚动快门插值）、密度网格位域 `density_grid_bitfield`、以及 `visualized_dimension`（决定是否走非融合路径）。

路径选择在 [src/testbed_nerf.cu:1928-2003](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L1928-L2003)：只有当 `m_jit_fusion && render_mode==Shade && visualized_dimension==-1 && show_accel==-1` 四个条件**同时**满足时，才用 4.3 节的融合内核；否则落到 [src/testbed_nerf.cu:2005-2066](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2005-L2066) 的 `tracer.init_rays_from_camera` + `tracer.trace` 两段式路径。

**生成光线**：

[src/testbed_nerf.cu:1591-1675](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L1591-L1675) — `NerfTracer::init_rays_from_camera()`：先 `enlarge` 预留显存，再用 `init_rays_with_payload_kernel_nerf` 内核（L1628）每个像素算 `uv→Ray` 并和包围盒求交，得到每根光线的初始 `t`；清零 rgba/depth；最后调 `advance_pos_nerf_kernel`（L1659）把每根光线推进到第一个**非空**体素，避免一上来就在空气里白跑。

**步进 + 压缩主循环**：

[src/testbed_nerf.cu:1677-1814](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L1677-L1814) — `NerfTracer::trace()`。压缩块在 L1715-1737（`compact_kernel_nerf` + 双缓冲 `m_rays[0]/m_rays[1]` 乒乓）；动态步数 `n_steps_between_compaction = clamp(target/n_alive, MIN, MAX)` 在 L1744-1746；生成下一步输入在 L1750-1768（`generate_next_nerf_network_inputs`）；批量推理在 L1772（`network->inference_mixed_precision`）；合成在 L1780-1805（`composite_kernel_nerf`）。外层 `while (i < MARCH_ITER)` 的硬上限 `MARCH_ITER=10000` 见 [src/testbed_nerf.cu:50](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L50)。

**沿光线批量生成采样点**（步进的核心）：

[src/testbed_nerf.cu:528-577](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L528-L577) — `generate_next_nerf_network_inputs`：每条活光线在 `for j in n_steps` 循环里反复调 `if_unoccupied_advance_to_next_occupied_voxel`（跳过空体素）、`calc_dt`（算这步步长）、把 `{warp_position, warp_direction, warp_dt}` 写进网络输入缓冲。注意它一次写 `n_alive * n_steps` 个 `NerfCoordinate`，供下一步整批推理。

`NerfTracer` 类本身与光线数据布局 `RaysNerfSoa`（双缓冲 + 命中缓冲 + 网络 input/output + 计数器）声明在 [include/neural-graphics-primitives/testbed.h:163-236](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L163-L236)；每根光线的状态 `NerfPayload{origin, dir, t, max_weight, idx, n_steps, alive}` 定义在 [include/neural-graphics-primitives/nerf_device.cuh:145-153](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L145-L153)。

#### 4.2.4 代码实践

**实践目标**：搞懂「压缩」为何能省算力。

1. 读 [src/testbed_nerf.cu:1744-1746](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L1744-L1746)，写出 `n_steps_between_compaction` 的公式：`clamp(2*1024*1024 / n_alive, MIN_STEPS_INBETWEEN_COMPACTION, MAX_STEPS_INBETWEEN_COMPACTION)`。
2. **跟踪一个推理**：假设初始 `n_alive = 100万` 像素，则每批约跑 `2M/1M = 2` 步就压缩一次；随着光线陆续死亡，`n_alive` 降到 1 万时，每批会跑 `2M/1万 = 200` 步（受 `MAX_STEPS_INBETWEEN_COMPACTION` 封顶）才压缩——这样总查询数始终维持在 ~200 万，让 MLP 推理的批大小稳定打满 GPU。
3. **思考题（待本地验证）**：如果不做压缩、固定让全部像素都跑到底，渲染一张 1080p 图大约要浪费多少次空转的 MLP 推理？（提示：被物体挡住或飞出场景的光线本该早停。）

#### 4.2.5 小练习与答案

**练习 1**：`trace()` 为什么用两个缓冲 `m_rays[0]` 和 `m_rays[1]` 乒乓切换？
**答**：为了在不分配新显存的前提下做原地压缩。压缩内核从「上一轮的活光线缓冲」读、把活光线写到「本轮缓冲」、把死光线写到 `m_rays_hit`；下一轮交换读写角色，避免读写同一缓冲产生数据竞争。

**练习 2**：为什么标准 `Shade` 模式默认不走 `NerfTracer` 而走 4.3 的融合内核？
**答**：`NerfTracer` 把「步进、推理、合成」拆成多个独立内核，每次都要把中间结果（百万级采样点）写回显存再读出，访存开销大。融合内核把三步拼进单个内核、用寄存器传递中间值，省掉这些显存往返，所以标准渲染走融合路径更快；`NerfTracer` 留给需要网络梯度/可视化的特殊渲染模式。

---

### 4.3 render_nerf 融合内核：一像素一线程端到端渲染

#### 4.3.1 概念说明

[render_nerf.cuh](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/render_nerf.cuh) 里的 `render_nerf` 设备函数是 instant-ngp 渲染的主力。它的设计哲学是：**一个 CUDA 线程负责一个像素，从头到尾把那根光线跑完**——发射光线、和包围盒求交、跳空体素、查网络、alpha 合成、提前终止、写帧缓冲，全在一个内核里用寄存器/共享内存完成，中间结果不落盘。

它之所以能「端到端」，靠的是 u8-l2 会讲的 **JIT 融合**：运行时把 `NerfNetwork` 的前向（位置编码 + 密度 MLP + 方向编码 + 颜色 MLP）编译成一段设备函数 `eval_nerf`，再 `#include` 进 `render_nerf.cuh`，于是内核里直接 `eval_nerf(nerf_in, params)` 就能在寄存器里算出 `[R,G,B,σ]`，无需调用 tiny-cuda-nn 的通用推理接口。

#### 4.3.2 核心流程

一根光线在融合内核里的完整生命：

```
1. 由像素坐标 (x,y) 算 uv，考虑亚像素抖动 sample_index
2. uv → 相机矩阵（含滚动快门插值）→ Ray{origin, dir}
3. Ray 与 render_aabb 求交，得到进入包围盒的 t
4. t += advance_n_steps(...)  （随机抖动一下，避免摩尔纹）
5. while True:
     a. if_unoccupied_advance_to_next_occupied_voxel  # 跳到下一个非空体素
     b. dt = calc_dt(t, cone_angle)
     c. nerf_in = {位置, 方向, dt}  (+ extra_dims)
     d. nerf_out = eval_nerf(nerf_in, params)         # 网络前向
     e. alpha 合成（见 4.1）
     f. 若 color.a > 1-min_transmittance → 归一化、跳出
6. 写 depth_buffer、按剩余透射率混入 envmap、写 frame_buffer
```

一个关键细节：步骤 d 的 `eval_nerf` 必须**整个 warp 一起执行**（GPU 上同 warp 的 32 个线程必须走同一段代码路径，否则结果错乱）。所以代码先用 `__all_sync(0xFFFFFFFF, !alive)` 判断「是不是全 warp 都死了」——只有全死才 `break`；只要还有一条光线活着，整个 warp 都得跑 `eval_nerf`，死光线之后用 `if (!alive) continue;` 跳过合成。

#### 4.3.3 源码精读

内核签名（参数极多，分四组：相机/镜头、包围盒、密度网格与步进、网络与输出）：

[include/neural-graphics-primitives/fused_kernels/render_nerf.cuh:22-58](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/render_nerf.cuh#L22-L58) — 注意 `__launch_bounds__(128, 4)`（每线程块 128 线程，每 SM 最多 4 块），以及 `density_grid`（空区域跳过）、`cone_angle_constant`（步距）、`min_transmittance`（提前终止）这几个本讲主角参数。

**像素 → 光线 → 进包围盒**：

[include/neural-graphics-primitives/fused_kernels/render_nerf.cuh:65-92](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/render_nerf.cuh#L65-L92) — `ld_random_pixel_offset` 给亚像素抖动；`uv_to_ray` 把像素映射成 `Ray`；`render_aabb.ray_intersect` 求出光线进入包围盒的 `t`，并校验起点在盒内。

**warp 一致性 + 步进 + 推理**：

[include/neural-graphics-primitives/fused_kernels/render_nerf.cuh:120-142](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/render_nerf.cuh#L120-L142) — `dt = calc_dt(t, cone_angle)`、组装 `NerfCoordinate`、`__all_sync` 守卫、`eval_nerf(nerf_in, params)`、`if (!alive) continue`。这段是「为什么融合内核快」的核心：网络推理和步进紧挨在一起，中间值留在寄存器。

**主机端如何把这段设备函数编译并启动**：

[src/testbed_nerf.cu:1928-2002](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L1928-L2002) — 用 `CudaRtcKernel` 在运行时把 `nerf_network->generate_device_function("eval_nerf")` 生成的前向代码 + `#include "render_nerf.cuh"` 拼成一段 PTX/CUBIN 缓存到 `device.fused_render_kernel()`，然后 `launch` 启动，把 `render_min_transmittance`（L1995）等参数传进去。`visualized_dimension > -1` 时会强制改走 `NerfTracer`（见 L1916）。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：完整描述一根光线从进入包围盒到写出 RGBA 的全过程，并解释 `render_min_transmittance` 的提前终止。

**操作步骤**：

1. 打开 [render_nerf.cuh](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/render_nerf.cuh)，按行号顺序阅读一遍内核体（L59-184）。
2. 用一张表把光线经历的主要步骤、对应行号、做了什么填出来（见下方预期结果）。
3. 重点解释 L163 的提前终止：写出触发条件 `color.a > 1 - min_transmittance`，并说明 `min_transmittance` 实参来自 `m_nerf.render_min_transmittance`（testbed.h:890，默认 `0.01`）。

**需要观察的现象**：在「实心物体内部」的光线，因首段密度大、\(\alpha\approx1\)，`color.a` 立刻逼近 1，几乎一步就触发 L163 终止；在「空旷/烟雾」区域的光线则要步进很多段，`color.a` 缓慢爬升，直到穿出包围盒（`t>=MAX_DEPTH`）才停。

**预期结果**（光线生命周期表）：

| 步骤 | 行号 | 做什么 |
|------|------|--------|
| 亚像素抖动 + uv | L65-66 | `ld_random_pixel_offset` 防摩尔纹 |
| uv → 相机 → Ray | L67-85 | 含滚动快门、镜头畸变、光圈 |
| 与包围盒求交得初始 t | L90-92 | `render_aabb.ray_intersect` |
| 算 cone_angle、抖动 t | L101-102 | `calc_cone_angle` + `advance_n_steps` |
| 步进循环 | L106-167 | 跳空体素 → 算 dt → eval_nerf → 合成 → 终止判断 |
| 写深度 | L177 | 命中权重最大的点作为深度 |
| 混入环境贴图 | L179-181 | 用剩余透射率补背景 |
| 写帧缓冲 | L183 | 输出 RGBA |

**提前终止说明**：`render_min_transmittance=0.01` 意味着当光线剩余透射率低于 1%（即累积不透明度 > 99%）时，后续采样最多再贡献 1% 颜色，于是立即归一化并 `alive=false` 跳出循环。对几乎不透明的物体，这能把每根光线的网络查询次数从几百次压到个位数，是渲染提速的关键。

#### 4.3.5 小练习与答案

**练习 1**：为什么内核里要用 `__all_sync(0xFFFFFFFF, !alive)` 而不是直接 `if (!alive) break;`？
**答**：CUDA 的 warp 要求 32 个线程执行相同的指令流。若某条光线 `alive=false` 就直接 `break`，warp 内其他活光线会被迫一起跳出，导致它们永远渲染不完。`__all_sync` 保证只有「全 warp 都死」才整体退出；否则所有线程（含死的）都执行 `eval_nerf`，死线程随后用 `if (!alive) continue;` 跳过合成，从而维持 warp 一致性。

**练习 2**：融合内核和 `NerfTracer` 谁负责写 `frame_buffer`？
**答**：两者都写。融合内核在 L183 直接写 `frame_buffer[idx]`；`NerfTracer` 路径则由 `composite_kernel_nerf` 把颜色累加进 `rays_current.rgba`，再在主循环结束后拷回 `render_buffer.frame_buffer`。最终都汇入 u6-l1 讲的 `CudaRenderBuffer`。

---

### 4.4 圆锥步距与多级联：cone_angle_constant 与 max_cascade

#### 4.4.1 概念说明

**圆锥步距（cone stepping）**。一根光线对应屏幕上一个像素，而像素在 3D 空间里的「足迹」是一个随距离增大的圆锥——离相机越远，一个像素覆盖的体积越大。如果到处用同样小的步长，远处就会过采样（浪费）；用同样大的步长，近处就会欠采样（糊）。instant-ngp 的做法是：**让步长 `dt` 随距离 `t` 线性增大**，近似匹配圆锥足迹。

这个「增大速率」由 `cone_angle_constant` 控制：默认 `1/256`，即步长大致按 \(\delta \approx t \cdot c\) 增长。对 `aabb_scale <= 1` 的单位立方体场景（如原始 NeRF 的合成数据），改用 `cone_angle_constant = 0` 的**固定步长**模式（等价于原始 NeRF 的均匀采样）。

**多级联（cascade）**。原始 NeRF 的场景都被归一化进 \([0,1]^3\) 单位立方体。instant-ngp 要渲染真实尺度的大场景（如 `aabb_scale=128` 意味着包围盒是单位立方体的 128 倍边长），密度网格和哈希编码都用一套「级联」机制：第 `mip` 级联是 128³ 的网格，覆盖的空间范围随 `mip` 翻倍增长，远处用粗级联（大范围、低分辨率）、近处用细级联。`max_cascade` 由 `aabb_scale` 决定，`NERF_CASCADES()=8` 是硬上限。

#### 4.4.2 核心流程与数学

**步距的「步进空间」映射**。代码不直接写 `dt = c*t`，而是先把距离 `t` 映射到一个「步进空间」坐标 `n`，再 `n+1` 映射回来：

\[
n = \mathrm{to\_stepping\_space}(t),\qquad
\mathrm{advance\_n\_steps}(t, c, 1) = \mathrm{from\_stepping\_space}(n+1, c)
\]

在主要的对数区间里，\(\mathrm{to\_stepping\_space}(t) = \log(t)/\log(1+c)\)，于是 `n` 每加 1，\(t\) 就乘以 \((1+c)\)，步长：

\[
\delta = \mathrm{calc\_dt}(t,c) = \mathrm{advance\_n\_steps}(t,c,1)-t \;\approx\; t\cdot c
\]

当 \(c \le 10^{-5}\) 时退化为线性映射 \(n=t/\text{MIN\_CONE\_STEPSIZE}\)，步长恒为 `MIN_CONE_STEPSIZE = SQRT3/NERF_STEPS = 1.732/1024 ≈ 0.00169`（固定步长）。`NERF_STEPS()=1024` 即「单位长度内最细 1024 步」。

**级联选择**：`mip_from_pos(pos)` 根据 `pos` 到中心 `0.5` 的最大坐标偏离，用 `frexpf` 取浮点指数，把空间分成 \([0,1]\)、\([1,2]\)、\([2,4]\)… 的嵌套立方体，偏离越大级联越高。渲染时 `if_unoccupied_advance_to_next_occupied_voxel` 用当前级联的密度位域决定是否跳过。

#### 4.4.3 源码精读

步进几何工具集全在 [include/neural-graphics-primitives/nerf_device.cuh](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh)：

- 基本常数：[nerf_device.cuh:28-36](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L28-L36) — `NERF_STEPS()=1024`、`NERF_CASCADES()=8`、`STEPSIZE()`、`MIN_CONE_STEPSIZE()`、`MAX_CONE_STEPSIZE()`。
- `calc_cone_angle`：[nerf_device.cuh:370-377](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L370-L377) — 注意它**直接返回常数 `cone_angle_constant`**（注释里那段按像素大小算的版本被注释掉了），所以「圆锥角」其实是个全局常数，不随像素位置变。
- 步进空间映射：[nerf_device.cuh:379-421](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L379-L421) — `to_stepping_space` / `from_stepping_space` 的三段分段函数（近端线性、中段对数、远端线性）。
- `advance_n_steps` / `calc_dt`：[nerf_device.cuh:423-429](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L423-L429)。
- `mip_from_pos`：[nerf_device.cuh:443-448](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L443-L448)。
- 空体素跳跃：[nerf_device.cuh:462-495](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L462-L495) — `if_unoccupied_advance_to_next_occupied_voxel`：当前体素空就沿光线跳到下一个体素边界，且会**尽量挑最大的空体素**（`while (mip < max_mip && !occupied) ++mip;`）一次跳一大步。
- 密度位域查询：[nerf_device.cuh:335-341](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L335-L341) — `density_grid_occupied_at`，按 Morton 码查一个 bit。

**两个关键参数在数据集加载时的初始化**：

[src/testbed_nerf.cu:2433-2440](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2433-L2440) —
```
max_cascade = 0;
while ((1 << max_cascade) < aabb_scale) ++max_cascade;        // ≈ log2(aabb_scale)
cone_angle_constant = (aabb_scale <= 1) ? 0.0f : 1.0f/256.0f;  // 大场景圆锥步进，单位立方体固定步长
```
字段声明与默认值见 [include/neural-graphics-primitives/testbed.h:866](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L866)（`max_cascade`）与 [testbed.h:883](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L883)（`cone_angle_constant = 1/256`）。密度网格位域 `density_grid_bitfield` 见 [testbed.h:861](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L861)，其更新机制在下一讲 u4-l5 详讲。

渲染调用链把这些参数一路传到内核：`Testbed::render_nerf`（[testbed_nerf.cu:1983-1984](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L1983-L1984) 传 `max_cascade`、`cone_angle_constant`）→ 融合内核签名（[render_nerf.cuh:41-43](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/render_nerf.cuh#L41-L43) 的 `min_mip/max_mip/cone_angle_constant`）。

#### 4.4.4 代码实践

**实践目标**：手算 `cone_angle_constant` 决定的步长，并理解级联如何覆盖大场景。

1. 由 [nerf_device.cuh:33-34](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L33-L34) 算 `MIN_CONE_STEPSIZE = SQRT3/1024 ≈ 0.00169`。
2. 取 `cone_angle_constant = 1/256 ≈ 0.0039`。在对数区间，`calc_dt(t, c) ≈ t·c`。故 `t=0.1`（近处）时步长约 `0.00039`（比 `MIN_CONE_STEPSIZE` 还小，落在近端线性段）；`t=2.0`（远处）时步长约 `0.0078`，是近处的 ~20 倍——远处采样变稀疏，匹配更大的像素足迹。**待本地验证**：可在 [render_nerf.cuh:101](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/render_nerf.cuh#L101) 处加一行 `printf` 打印 `t` 与 `dt`，观察二者比例。
3. **级联覆盖**：设 `aabb_scale=128`，由 [testbed_nerf.cu:2433-2436](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2433-L2436) 得 `max_cascade=7`，即级联 0..7 共 8 层（恰等于 `NERF_CASCADES()`）。第 0 级联覆盖单位立方体核心区，第 7 级联覆盖到 \(2^7=128\) 倍范围——正好把整个 `aabb_scale` 包进来。

#### 4.4.5 小练习与答案

**练习 1**：`cone_angle_constant` 设成 0 会怎样？为什么单位立方体场景这么做？
**答**：`calc_cone_angle` 返回 0，`to_stepping_space` 退化为线性 \(n=t/\text{MIN\_CONE\_STEPSIZE}\)，步长恒为 `MIN_CONE_STEPSIZE`，即原始 NeRF 的均匀采样。单位立方体场景里像素足迹随距离变化不大，固定步长足够、且实现简单，故取 0。

**练习 2**：为什么空体素跳跃要「尽量挑最大的空体素」（`while mip<max_mip && !occupied: ++mip`）？
**答**：这样能一次性跨过一大片连续空区域，而不是一个一个小体素地挪，大幅减少循环次数和密度位域查询次数；这是光线步进能在空旷大场景里保持实时帧率的关键技巧之一（配合 u4-l5 的密度网格）。

---

## 5. 综合实践

**任务：用 pyngp 体验「提前终止 + 圆锥步距 + 多级联」三者的协作。**

> 依赖 u7 的 pyngp 绑定（见 [src/python_api.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu)）。若尚未编译 pyngp，按 u1-l3 用 `cmake -DNGP_BUILD_WITH_PYTHON=ON ...` 编译。

1. 用 pyngp 加载 `data/nerf/fox` 训练到收敛（参考 run.py 写法）。`fox` 的 `aabb_scale=1`，因此 [testbed_nerf.cu:2440](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2440) 会把 `cone_angle_constant` 设成 0（固定步长），`max_cascade=0`（单级联）——这是最简单的渲染配置。
2. 换一个 `aabb_scale>1` 的大场景（例如自己用 colmap2nerf 采的 360° 数据，设 `aabb_scale=32`）。重新加载后 `cone_angle_constant=1/256`、`max_cascade=5`，渲染管线自动启用圆锥步距 + 多级联空区域跳过。
3. **对照观察**（待本地验证）：用 pyngp 的 `render` 出图并计时，比较两个场景的单帧渲染耗时与大场景「远处」区域的清晰度。预期大场景虽步距更稀疏，但因密度网格跳空 + 多级联覆盖，仍能实时且不糊。
4. **源码侧串读**：把本讲的三个层次连起来——`Testbed::render_nerf`（[testbed_nerf.cu:1894](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L1894)）选路径 → 融合内核 `render_nerf`（[render_nerf.cuh:22](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/render_nerf.cuh#L22)）里用 `cone_angle_constant` 步进（L101）、用密度网格 `density_grid` 跳空（L111）、用 `min_transmittance` 提前终止（L163）。整条链路在三个参数上闭环。

## 6. 本讲小结

- 体渲染把每根光线的颜色表示为沿途采样点的加权积分：\(C=\sum_i T_i\alpha_i c_i\)，代码用累积不透明度 `color.a` 递推，\(T_i = 1-\)`color.a`。
- `render_min_transmittance`（默认 0.01）在剩余透射率低于 1% 时提前终止光线，是渲染省算力的关键。
- 渲染有两条路径：标准 `Shade` 走 [render_nerf.cuh](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/render_nerf.cuh) 的**融合内核**（一像素一线程、寄存器内端到端）；法线/深度/可视化等模式走 `NerfTracer` 的**两段式 + 光线压缩**路径。
- `NerfTracer::trace()` 用双缓冲压缩活光线，并按 `n_alive` 动态调整每批步数，让 MLP 批查询稳定在 ~200 万以打满 GPU。
- 圆锥步距 `cone_angle_constant`（默认 1/256，单位立方体场景取 0）让步长随距离线性增大，匹配像素足迹；多级联 `max_cascade ≈ log2(aabb_scale)`（上限 `NERF_CASCADES()=8`）让密度网格与渲染覆盖任意大场景。
- 融合内核必须用 `__all_sync(0xFFFFFFFF, !alive)` 维持 warp 一致性，死光线也得陪跑 `eval_nerf` 后再 `continue`。

## 7. 下一步学习建议

- **u4-l4（NeRF 训练循环）**：本讲讲的是「渲染」用到的光线步进；训练用的步进几乎相同，但要在步进后算损失并反传，且引入误差图重要性采样和光线压缩统计 `NerfCounters`，建议紧接着读。
- **u4-l5（密度网格与空区域跳过）**：本讲反复用到 `density_grid_bitfield` 来跳空，但没讲它怎么来的——下一篇详解密度网格的 EMA 更新与位域压缩，是空区域跳过的数据来源。
- **u8-l2（JIT 融合与全融合内核）**：想深入 `eval_nerf` 这段设备函数是怎么由 `NerfNetwork::generate_device_function` 运行时拼出来的，以及 JIT 融合的缓存机制，留到专家层。
- 自学线索：把 [render_nerf.cuh](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/render_nerf.cuh) 与 [train_nerf.cuh](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/train_nerf.cuh) 并排对照阅读，能一次看清「渲染」与「训练」两条步进管线的异同。
