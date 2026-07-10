# NeRF 训练循环

## 1. 本讲目标

本讲承接 [u4-l2 NerfNetwork 双头架构](u4-l2-nerf-network-architecture.md) 与 [u4-l3 光线步进与体渲染](u4-l3-nerf-ray-marching.md)。前两讲告诉我们：`NerfNetwork` 能在「单个 3D 点」上输出 `[RGB, σ]`，体渲染能把「一根光线」变成「一个像素颜色」。本讲要回答的最后一个问题是——**每一步训练，到底拿什么去更新网络？**

读完本讲你应能：

1. 看懂 NeRF 训练的**批量采样机制**：每步不是喂整张图，而是从训练图像里随机采一批光线，每条光线再 marching 出若干采样点。
2. 理解 `NerfCounters` 如何用 `numsteps_counter` / `numsteps_counter_compacted` 统计样本数，并据此**动态调整每步的光线数**。
3. 弄清**光线压缩（compaction）**：为什么 marching 出来的样本要丢掉一部分，`measured_batch_size_before_compaction` 与 `measured_batch_size` 究竟差在哪里。
4. 掌握损失函数族 `ELossType`（L2/Huber/SMAPE…）如何在内核里被解析地求值，以及 `random_bg_color` 为什么能逼迫模型学会透明度。
5. 认识**误差图（error_map）重要性采样**：如何记录每张图、每个图块的累积误差，再按误差比例多采「难样本」。

---

## 2. 前置知识

- **NeRF 的体渲染公式（回顾 u4-l3）**。沿一根光线 \(r\) 在若干离散点 \((c_i,\sigma_i,\delta_i)\) 上做 alpha 合成：

  \[
  C(r)=\sum_{i=1}^{N} T_i\,\alpha_i\,c_i,\qquad
  \alpha_i = 1-\exp(-\sigma_i\,\delta_i),\qquad
  T_i = \exp\!\left(-\sum_{j<i}\sigma_j\delta_j\right)
  \]

  代码里把累积不透明度记作 `color.a`，于是透射率 \(T_i = 1-\)`color.a`。本讲训练内核用的就是这套递推。

- **混合精度与 `LOSS_SCALE`**。NeRF 用半精度（`network_precision_t`）训练，小梯度会下溢，所以要把损失（从而梯度）放大 `LOSS_SCALE()` 倍，在优化器里再除掉。定义见 [testbed.h:307-311](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L307-L311)。

- **密度网格位域（回顾 u4-l3 的空区域跳过）**。`density_grid_bitfield` 把空间打成 128³ 的位图，标记哪些格子「有内容」。marching 时只在这些占据格子里停步采点。相关设备函数在 `nerf_device.cuh`。

- **target_batch_size 的含义**。这是「**每步想要的采样点数量**」（不是光线数、不是像素数），默认 `1 << 18 = 262144`，可在 GUI 的 `Batch size` 滑块调节（[testbed.cu:1146](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1146)）。因为每条光线 marching 出的采样点数是**变量**，程序必须动态调整「采多少条光线」才能稳定命中这个目标。

> 关键直觉：NeRF 训练的「一个 batch」= 一堆 3D 采样点（`NerfCoordinate`），而不是一堆图片或一堆像素。理解了这一点，后面的 `NerfCounters` 与压缩就顺理成章。

---

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [src/testbed.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu) | `train()` 总调度：训练准备分发、每步 loss 上报节奏、把 `m_training_batch_size` 传给 `train_nerf`。 |
| [src/testbed_nerf.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu) | 本讲主战场：`training_prep_nerf`、`train_nerf`、`train_nerf_step`、`NerfCounters` 的两个成员函数，以及非融合路径的两个内核 `generate_training_samples_nerf` / `compute_loss_kernel_train_nerf`、误差图 CDF 重算。 |
| [include/neural-graphics-primitives/fused_kernels/train_nerf.cuh](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/train_nerf.cuh) | JIT 融合训练内核 `train_nerf`：一像素一线程，在寄存器里端到端完成 marching + 损失 + 反传。 |
| [include/neural-graphics-primitives/testbed.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h) | `NerfCounters` 结构体、`Nerf::Training` 里的 `error_map`、`loss_type`、`random_bg_color` 等训练态声明。 |
| [include/neural-graphics-primitives/nerf_device.cuh](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh) | 设备端工具：采样选图 `image_idx`、选像素 `nerf_random_image_pos_training`、误差图 CDF 采样 `sample_cdf_2d`、损失族 `loss_and_gradient`。 |
| [include/neural-graphics-primitives/common.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common.h) | 枚举 `ELossType`（7 种损失）与 `ETrainMode`（Nerf / Rfl / RflRelax）。 |
| [configs/nerf/base.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json) | 默认 NeRF 配置：`loss.otype = "Huber"`，编码/网络结构。 |

---

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：① 训练入口与批量机制；② 光线采样与样本生成；③ `NerfCounters` 与光线压缩（compaction）；④ 损失计算与 `loss_type`（含 `random_bg_color`）；⑤ 误差图重要性采样。

### 4.1 训练入口：从 frame() 到 train_nerf_step

#### 4.1.1 概念说明

NeRF 训练并非「喂一张完整训练图、算整张图的误差」。每一步训练，程序只从全部训练图像里**随机抽一小批光线**（每条光线对应一个像素），沿每条光线 marching 出若干采样点，把这些采样点的预测颜色与对应像素真值做损失，反传更新网络。这种「小批量随机」是 NeRF 能秒级收敛的关键之一。

整条调用链是：`frame()` → `train_and_render()` → `train()` → `train_nerf()` → `train_nerf_step()`。`train_nerf` 是「编排者」，负责训练准备、调度一步、做优化器步进、统计与误差图维护；`train_nerf_step` 才是真正发射 GPU 内核的那层。

#### 4.1.2 核心流程

`train()`（[testbed.cu:4561](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4561)）里与本讲相关的时序：

1. **兜底建网**：若 `m_trainer` 为空（刚切模式/刚加载数据），调用 `reload_network_from_file()` 临时建网——这是 u2-l2 讲过的「按需建网」。
2. **训练准备（隔步做）**：对 NeRF，`n_prep_to_skip = clamp(step/16, 1, 16)`，即训练越久、密度网格更新越稀疏；只有 `step % n_prep_to_skip == 0` 时才调用 `training_prep_nerf` 刷新密度网格。
3. **设置优化器超参**：沿 `optimizer.nested` 链找到叶子优化器，按 `m_train_network` / `m_train_encoding` 决定是否更新 MLP 权重 / 哈希表。
4. **loss 上报节奏**：`get_loss_scalar = (m_training_step % 16 == 0)`——只有每 16 步才算一次用于显示的标量损失，因为 `reduce_sum` 有开销。
5. **分发一步训练**：`switch(m_testbed_mode)` → `train_nerf(batch_size, get_loss_scalar, stream)`。

`train_nerf`（[testbed_nerf.cu:2704](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2704)）的骨架：

```
prepare_for_training_steps()        // 清零计数器与 loss 缓冲
[清零相机/曝光/畸变/焦距/extra_dims 的梯度缓冲]
[按需 resize 并清零 error_map.data]
train_nerf_step(...)                // 发射 marching + loss + 反传内核
m_trainer->optimizer_step(LOSS_SCALE)   // 用梯度更新参数
++m_training_step
update_after_training(...)          // 回读计数器、动态调 rays_per_batch、算 loss 标量
[若该重算误差图：构造 CDF → is_cdf_valid=true]
[把相机/曝光等梯度拷回 CPU，做各自的 Adam 步进]
```

#### 4.1.3 源码精读

`train_and_render` 是 frame 循环里「训练 + 渲染」的总入口（[testbed.cu:3172-3175](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3172-L3175)）：

```cpp
void Testbed::train_and_render(bool skip_rendering) {
    if (m_train) {
        train(m_training_batch_size);
    }
    ...
```

`m_training_batch_size` 默认 `1 << 18`（[testbed.h:1089](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L1089)），就是上面说的「每步目标采样点数」。它被强制对齐到 `BATCH_SIZE_GRANULARITY`（[testbed.cu:1151](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1151)）。

训练准备的「隔步跳过」与分发（[testbed.cu:4596-4611](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4596-L4611)）：

```cpp
uint32_t n_prep_to_skip = m_testbed_mode == ETestbedMode::Nerf ? clamp(m_training_step / 16u, 1u, 16u) : 1u;
if (m_training_step % n_prep_to_skip == 0) {
    switch (m_testbed_mode) {
        case ETestbedMode::Nerf: training_prep_nerf(batch_size, m_stream.get()); break;
        ...
```

`training_prep_nerf`（[testbed_nerf.cu:3385-3398](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L3385-L3398)）只做一件事——刷新密度网格：训练前 256 步均匀采样整张网格，之后改成「1/4 均匀 + 1/4 非均匀」以聚焦已发现的高密度区：

```cpp
if (m_training_step < 256) {
    update_density_grid_nerf(alpha, NERF_GRID_N_CELLS() * n_cascades, 0, stream);
} else {
    update_density_grid_nerf(alpha, NERF_GRID_N_CELLS() / 4 * n_cascades, NERF_GRID_N_CELLS() / 4 * n_cascades, stream);
}
```

> 这一步是 u4-l5「密度网格」的伏笔——它产出的位域会被本讲的 marching 直接用来跳过空区域。

最后一步训练的分发（[testbed.cu:4633-4634](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4633-L4634)）：

```cpp
case ETestbedMode::Nerf: train_nerf(batch_size, get_loss_scalar, m_stream.get()); break;
```

#### 4.1.4 代码实践

1. **目标**：在源码里画出「一帧 → 一步 NeRF 训练」的完整调用链。
2. **步骤**：从 `frame()`（u2-l2 已读）出发，依次定位 `train_and_render`、`train`、`training_prep_nerf`、`train_nerf`、`train_nerf_step`、`NerfCounters::prepare_for_training_steps` / `update_after_training`、`m_trainer->optimizer_step`。
3. **观察**：注意 `train()` 里训练准备与一步训练分别有自己的 `ScopeGuard` 计时（`m_training_prep_ms` / `m_training_ms`），这两个值会喂给 GUI 的帧时间条。
4. **预期**：训练前 256 步密度网格全量刷新（`training_prep` 较慢），之后变稀疏（变快）——这解释了为什么「刚加载场景那几秒」帧率会明显偏低。**待本地验证**：加载 fox 后观察前几秒 FPS 与稳定后 FPS 的差异。

#### 4.1.5 小练习与答案

**Q1**：为什么 `train()` 里训练准备要 `clamp(step/16, 1, 16)` 地隔步做，而不是每步都做？
**A**：密度网格用 EMA 平滑跟踪网络输出，每步都全量刷新既贵又不必要；随着训练推进、场景结构稳定，刷新频率可以单调下降（最多每 16 步一次），把 GPU 时间留给训练本身。

**Q2**：`get_loss_scalar` 为什么不是每步都算？
**A**：标量损失只用于 GUI 的 loss 曲线显示，不影响优化；而 `reduce_sum` 跨光线归约有开销，每 16 步算一次足够画出平滑曲线。

---

### 4.2 光线采样与样本生成（marching）

#### 4.2.1 概念说明

「采样一批光线」分两小步：先**选一张训练图**（`image_idx`），再**在这张图上选一个像素**（`nerf_random_image_pos_training`）；该像素就定义了一条相机光线。然后沿这条光线，借助密度网格位域**跳过空区域**，只在「占据体素」里按圆锥步距 marching，每停一次产生一个 `NerfCoordinate`（位置 + 方向 + 步距 dt）作为网络输入。

这里有两套实现路径：
- **非融合路径**（默认 `m_jit_fusion == false` 或 train_mode 为 `Nerf`）：`generate_training_samples_nerf` 只管 marching 记录坐标 → 调 `m_network->inference_mixed_precision` 做整批前向 → `compute_loss_kernel_train_nerf` 算损失与压缩。
- **融合路径**（`m_jit_fusion == true` 且 train_mode 为 Rfl/RflRelax）：单个 JIT 内核 `train_nerf`（`train_nerf.cuh`）一像素一线程，在寄存器里完成 marching + 前向 + 损失 + 反传。

两套的采样逻辑（选图、选像素、marching）完全一致，区别只在「是否把前向/反传折叠进同一个内核」。

#### 4.2.2 核心流程

一条训练光线的诞生（设备函数都在 [nerf_device.cuh](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh)）：

```
img   = image_idx(i, n_rays, n_rays_total, n_images, cdf_img?, &img_pdf)   // 选图（可按误差图加权）
uv    = nerf_random_image_pos_training(rng, resolution, ..., cdf_x_cond_y?, cdf_y?, img, &uv_pdf)  // 选像素
pix   = read_rgba(uv, ...)            // 读真值像素（.x<0 表示被遮罩，丢弃）
ray   = uv_to_ray(...) 或 metadata[img].rays[pix]   // 由内外参构造光线
t     = 起点（advance_n_steps 抖动，避免所有光线对齐）
while 仍在包围盒内 且 步数 < NERF_STEPS():
    if density_grid_occupied_at(pos, bitfield, mip):  ++j; t += dt   // 占据：记一个采样点
    else:                                            t = advance_to_next_voxel(...)  // 空：跳到下一格子
numsteps = j   // 这条光线贡献的采样点数
```

要点：
- **`NERF_STEPS() = 1024`**（[nerf_device.cuh:29](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L29)）是「每条光线最多停多少次」的上限，远大于典型值。
- **圆锥步距**：`dt = calc_dt(t, cone_angle)`（[nerf_device.cuh:427](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L427)），离相机越远步长越大（回顾 u4-l3）。
- **mip 级联**：`mip_from_dt` 根据距离选密度网格的 mip 层，远处用更粗的网格（回顾 u4-l3 的级联）。

#### 4.2.3 源码精读

选图设备函数 `image_idx`（[nerf_device.cuh:578-599](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L578-L599)）：传了 `cdf_img` 就按图像级 CDF 做重要性采样（4.5 讲）；否则让「同一 warp 内相邻线程处理同一张图」以提高访存局部性：

```cpp
// Neighboring threads in the warp process the same image. Increases locality.
return (((base_idx * n_training_images) / n_rays) % n_training_images;
```

选像素 `nerf_random_image_pos_training`（[nerf_device.cuh:553-576](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L553-L576)）：传了 `cdf_x_cond_y` 就走 `sample_cdf_2d` 按像素级误差采样，否则均匀随机；`snap_to_pixel_centers` 决定是否对齐到像素中心。

非融合内核 `generate_training_samples_nerf` 的核心是「两趟 marching」（[testbed_nerf.cu:691-849](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L691-L849)）。**第一趟只为数清楚这条光线要停几次**（[testbed_nerf.cu:798-811](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L798-L811)）：

```cpp
while (aabb.contains(pos = ray_unnormalized.o + t * ray_d_normalized) && j < NERF_STEPS()) {
    float dt = calc_dt(t, cone_angle);
    uint32_t mip = mip_from_dt(dt, pos, max_mip);
    if (density_grid_occupied_at(pos, density_grid, mip)) { ++j; t += dt; }
    else { t = advance_to_next_voxel(t, cone_angle, pos, ray_d_normalized, idir, mip); }
}
uint32_t numsteps = j;
```

然后**原子累加**到全局计数器，拿到自己这段坐标在缓冲里的起始偏移 `base`（[testbed_nerf.cu:812-815](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L812-L815)）：

```cpp
uint32_t base = atomicAdd(numsteps_counter, numsteps); // first entry in the array is a counter
if (base + numsteps > max_samples) { return; }         // 超过批量上界则丢弃这条光线
```

注意 `numsteps_counter` 的**第 0 个元素被复用成一个全局计数器**（注释明确写了），这是本讲反复出现的「就地计数器」技巧。**第二趟**（[testbed_nerf.cu:829-841](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L829-L841)）才真正把每个采样点的 `NerfCoordinate` 写进 `coords_out(base + j)`。

融合内核 `train_nerf`（[train_nerf.cuh:22](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/train_nerf.cuh#L22) 起）的选图选像素与非融合版完全一致（[train_nerf.cuh:86-92](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/train_nerf.cuh#L86-L92)），但它边 marching 边调 `eval_nerf`（JIT 生成的设备函数）算颜色，无需单独的前向 pass。

> 一个易混点：`generate_training_samples_nerf` 里 `max_level = random_val(rng) * 2.0f`（[testbed_nerf.cu:738](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L738)），注释说「乘 2 保证 50% 训练在最高 level」。这是哈希编码的「层级随机化」技巧——训练时随机只激活部分分辨率层，增强抗过拟合能力（对应 u3-l2 的多层级网格）。

#### 4.2.4 代码实践

1. **目标**：理解 `generate_training_samples_nerf` 为什么要跑「两趟 marching」。
2. **步骤**：阅读 [testbed_nerf.cu:793-848](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L793-L848)，对比第一趟（只数 `j`）与第二趟（写 `coords_out(j)`）的循环条件。
3. **思考**：能否只跑一趟？为什么不行？（提示：要在并发写入共享缓冲前，先用 `atomicAdd` 拿到这段坐标的起始偏移 `base`，而偏移依赖总停步数。）
4. **预期**：两趟循环的停步逻辑必须**完全一致**，否则第一趟数的 `numsteps` 与第二趟实际写入的点数对不上——这正是融合内核要把两趟合并、用 `__all_sync` 维持 warp 一致性的原因。**待本地验证**。

#### 4.2.5 小练习与答案

**Q1**：`density_grid_occupied_at` 返回 false 时，代码做什么？
**A**：调用 `advance_to_next_voxel` 把光线 `t` 直接推进到下一个密度网格格子的边界，跳过整个空体素——这就是「空区域跳过」，避免在空旷区域浪费网络前向。

**Q2**：为什么用 `atomicAdd(numsteps_counter, numsteps)` 而不是预分配每条光线固定点数？
**A**：每条光线穿过的占据体素数差异巨大（贴着物体表面的光线可能停几百次，擦边的光线只停几次），固定分配会造成大量浪费或不足；原子累加让所有光线共享一个紧凑缓冲，再用 4.3 的压缩把有效样本装进定长 batch。

---

### 4.3 NerfCounters：统计、动态批大小与光线压缩（compaction）

#### 4.3.1 概念说明

`NerfCounters` 是「每步训练的统计与缓冲管理器」。它解决两个问题：

1. **动态调整每步的光线数 `rays_per_batch`**。因为每条光线的采样点数是变量，固定光线数会导致实际 batch 大小忽大忽小。`NerfCounters` 量出上一一步真实产出了多少采样点，再反推这一步该采多少条光线，使实际 batch 稳定逼近 `target_batch_size`。
2. **光线压缩（compaction）**。一根光线 marching 出来的点里，往往有一段「光线已经不透明、后续点对颜色毫无贡献」的尾巴。把这些尾巴丢掉，既能省算力，又能把幸存的有效样本紧密打包进一个定长 batch 喂给 MLP。

它持有四个关键字段（[testbed.h:472-484](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L472-L484)）：

```cpp
struct NerfCounters {
    GPUMemory<uint32_t> numsteps_counter;           // 压缩前：所有光线 marching 出的总采样数
    GPUMemory<uint32_t> numsteps_counter_compacted; // 压缩后：丢掉不透明尾巴后的有效采样数
    GPUMemory<float> loss;                          // 每条光线的标量损失（用于求和上报）

    uint32_t rays_per_batch = 1 << 12;              // 每步采的光线数，动态自适应
    uint32_t n_rays_total = 0;
    uint32_t measured_batch_size = 0;               // = numsteps_counter_compacted（压缩后）
    uint32_t measured_batch_size_before_compaction = 0; // = numsteps_counter（压缩前）

    void prepare_for_training_steps(cudaStream_t stream);
    float update_after_training(uint32_t target_batch_size, bool get_loss_scalar, cudaStream_t stream);
};
```

#### 4.3.2 核心流程

`prepare_for_training_steps`（每步开头）：把两个计数器的第 0 个元素、以及 `loss` 缓冲清零。

内核运行期间：每条光线用 `atomicAdd(numsteps_counter, numsteps)` 累加自己 marching 的点数；压缩阶段每条光线再用 `atomicAdd(numsteps_counter_compacted, compacted_numsteps)` 累加幸存点数。

`update_after_training`（每步结尾）：把两个计数器拷回 CPU，填进 `measured_batch_size_before_compaction` / `measured_batch_size`，按比例缩放 `rays_per_batch`，并可选地归约损失：

\[
\text{rays\_per\_batch} \leftarrow \text{rays\_per\_batch} \cdot \frac{\text{target\_batch\_size}}{\text{measured\_batch\_size}}
\]

也就是「上一一步实际产出偏多就少采些光线，偏少就多采些」。结果再对齐到 `BATCH_SIZE_GRANULARITY` 并夹到上限 `1<<18`。

**为什么要压缩**：体渲染里，一旦累积透射率 \(T = 1 - \text{color.a} < 10^{-4}\)，光线之后的任何采样点 \(\alpha_i c_i\) 都会被 \(\prod(1-\alpha)\) 乘到几乎为 0，对颜色与梯度都没有贡献。继续为这些点做网络前向与反传纯属浪费。压缩把它们剔除，让固定大小（`target_batch_size`）的 batch 尽量装满「真正有梯度的样本」。

#### 4.3.3 源码精读

`prepare_for_training_steps`（[testbed_nerf.cu:2669-2676](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2669-L2676)）：

```cpp
void Testbed::NerfCounters::prepare_for_training_steps(cudaStream_t stream) {
    numsteps_counter.enlarge(1);
    numsteps_counter_compacted.enlarge(1);
    loss.enlarge(rays_per_batch);
    CUDA_CHECK_THROW(cudaMemsetAsync(numsteps_counter.data(), 0, sizeof(uint32_t), stream));          // 清第 0 个计数器
    CUDA_CHECK_THROW(cudaMemsetAsync(numsteps_counter_compacted.data(), 0, sizeof(uint32_t), stream));
    CUDA_CHECK_THROW(cudaMemsetAsync(loss.data(), 0, sizeof(float) * rays_per_batch, stream));
}
```

`update_after_training`（[testbed_nerf.cu:2678-2702](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2678-L2702)）——本讲 practice 的核心：

```cpp
numsteps_counter.copy_to_host(counter_cpu);
numsteps_counter_compacted.copy_to_host(compacted_counter_cpu);
measured_batch_size = 0;
measured_batch_size_before_compaction = 0;
if (counter_cpu[0] == 0 || compacted_counter_cpu[0] == 0) { return 0.f; }

measured_batch_size_before_compaction = counter_cpu[0];       // 压缩前总点数
measured_batch_size = compacted_counter_cpu[0];               // 压缩后有效点数

float loss_scalar = 0.0;
if (get_loss_scalar) {
    loss_scalar = reduce_sum(loss.data(), rays_per_batch, stream) * (float)measured_batch_size / (float)target_batch_size;
}

rays_per_batch = (uint32_t)((float)rays_per_batch * (float)target_batch_size / (float)measured_batch_size);
rays_per_batch = std::min(next_multiple(rays_per_batch, BATCH_SIZE_GRANULARITY), 1u << 18);
```

**压缩发生在哪？** 在非融合路径的 `compute_loss_kernel_train_nerf` 里：内核逐点累乘透射率 `T *= (1.f - alpha)`，一旦 `T < EPSILON(=1e-4)` 就 `break`，跳出后的 `compacted_numsteps` 才是幸存点数（[testbed_nerf.cu:916-948](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L916-L948)）：

```cpp
for (; compacted_numsteps < numsteps; ++compacted_numsteps) {
    if (T < EPSILON) { break; }
    ...
    T *= (1.f - alpha);
    network_output += padded_output_width;
    coords_in += 1;
}
...
uint32_t compacted_base = atomicAdd(numsteps_counter, compacted_numsteps); // 注意：写进的是 compacted 计数器
compacted_numsteps = min(max_samples_compacted - min(max_samples_compacted, compacted_base), compacted_numsteps);
```

（这里函数参数名叫 `numsteps_counter`，但调用方传进来的是 `counters.numsteps_counter_compacted.data()`，见 [testbed_nerf.cu:3264](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L3264)。）

在融合路径里，marching 与透射率累乘在同一趟完成，`numsteps` 本就已排除不透明尾巴（[train_nerf.cuh:226-228](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/train_nerf.cuh#L226-L228)），所以只需把 `numsteps_counter` 原样拷给 `numsteps_counter_compacted`（[testbed_nerf.cu:3186-3188](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L3186-L3188)）——此时两个 `measured_batch_size` 相等。

`train_nerf_step` 还会用 `measured_batch_size_before_compaction` 来决定**前向缓冲 `max_inference` 的大小**（[testbed_nerf.cu:3056-3060](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L3056-L3060)）——因为非融合路径必须先为「压缩前的全部点」分配前向输出缓冲，再在压缩后装进 `target_batch_size`：

```cpp
if (counters.measured_batch_size_before_compaction == 0) {
    counters.measured_batch_size_before_compaction = max_inference = max_samples;   // 首步用最坏上界
} else {
    max_inference = next_multiple(std::min(counters.measured_batch_size_before_compaction, max_samples), BATCH_SIZE_GRANULARITY);
}
```

其中 `max_samples = target_batch_size * 16`（[testbed_nerf.cu:3009](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L3009)，注释 `Somewhat of a worst case`）是前向缓冲的最坏上界。

> 还有一处「回滚防漏」：`fill_rollover` 内核（[testbed_nerf.cu:3298-3306](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L3298-L3306)）把 `target_batch_size` 之外空着的槽位用第 0 个样本回填，保证喂给 `Trainer` 的矩阵是「满的」——Trainer 不需要知道实际有多少有效样本。

#### 4.3.4 代码实践（本讲指定实践）

1. **目标**：回答——`measured_batch_size_before_compaction` 与 `measured_batch_size` 的区别是什么？为什么要做光线压缩（compaction）？
2. **步骤**：
   - 读 `update_after_training`（[testbed_nerf.cu:2678-2702](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2678-L2702)），看清二者分别来自 `numsteps_counter` 与 `numsteps_counter_compacted`。
   - 读压缩逻辑（[testbed_nerf.cu:916-948](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L916-L948)），找到 `if (T < EPSILON) break;`。
   - 读动态批大小公式（2698 行）。
3. **参考答案**：
   - **区别**：`measured_batch_size_before_compaction`（=`numsteps_counter`）是 marching 阶段产出的**原始采样点总数**，即「为这批光线做了多少次网络前向」；`measured_batch_size`（=`numsteps_counter_compacted`）是**压缩之后**真正参与梯度的有效点数——剔除了每根光线上「累积透射率 \(T < 10^{-4}\)」之后的死点，并夹到 `target_batch_size` 上界。前者 ≥ 后者。
   - **为什么压缩**：① **省算力**——不透明尾巴对颜色与梯度的贡献被 \(\prod(1-\alpha)\) 乘到 ~0，继续前向/反传是纯浪费；② **定长 batch**——Trainer 期望一个 `target_batch_size` 宽的矩阵，压缩把变长的每光线点数规整成紧凑、满载的有效 batch（再用 `fill_rollover` 补齐空槽）；③ **稳定动态批大小**——`update_after_training` 用「压缩后」的 `measured_batch_size` 来校准 `rays_per_batch`，让校准基于「真正有用的样本量」而非被死点稀释的总数。
4. **预期**：正常训练时 `measured_batch_size_before_compaction` 会比 `measured_batch_size` 大一些（差距取决于场景里有多少「厚重到快速不透明」的区域）；两者之比就是压缩率。**待本地验证**：可在 `update_after_training` 临时加一条日志打印这两个值观察比例。

#### 4.3.5 小练习与答案

**Q1**：如果某一步 `measured_batch_size == 0` 会怎样？
**A**：`train_nerf` 会把 `m_loss_scalar` 置 0、打印 `Nerf training generated 0 samples. Aborting training.` 并关掉 `m_train`（[testbed_nerf.cu:2779-2788](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2779-L2788)）。这通常意味着密度网格还没建立、所有光线都被判为空。

**Q2**：为什么 `rays_per_batch` 要按 `target/measured` 缩放，而不是固定 4096？
**A**：每条光线的平均采样点数会随训练变化（密度网格变密后光线停得更多）。若固定光线数，实际 batch 会飘离 `target_batch_size`，导致显存占用不稳或前向矩阵浪费；按比例缩放能把实际 batch 钉在目标附近。

---

### 4.4 损失计算与 loss_type

#### 4.4.1 概念说明

体渲染合成得到光线颜色 `rgb_ray` 后，与真值 `rgbtarget` 比较算损失。NeRF 的损失**不是**通过 tiny-cuda-nn 的 `m_loss` 对象算的，而是在训练内核里用设备函数 `loss_and_gradient` **解析地**同时返回损失值与对预测颜色的梯度——因为整个反传链路（alpha 合成、激活函数）都是手工写的，需要解析梯度而非自动微分。

`ELossType`（[common.h:99-107](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common.h#L99-L107)）共 7 种：`L2 / L1 / Mape / Smape / Huber / LogL1 / RelativeL2`。NeRF 默认 `loss_type` 来自 `configs/nerf/base.json` 的 `loss.otype`（= `"Huber"`），经 `string_to_loss_type` 解析（[testbed.cu:4209](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4209)）。

另一个关键开关是 `random_bg_color`：训练时每条光线随机生成一个背景色，迫使模型学会正确的透明度（密度）。

#### 4.4.2 核心流程

`loss_and_gradient`（[nerf_device.cuh:601-616](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L601-L616)）按 `loss_type` switch 到具体损失函数。每个损失函数都同时返回 `{loss, gradient}`，例如 L2：

\[
L_2 = \|\text{pred}-\text{target}\|^2,\qquad \frac{\partial L_2}{\partial \text{pred}} = 2(\text{pred}-\text{target})
\]

真值的合成（[testbed_nerf.cu:984-999](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L984-L999)）把曝光、背景、色彩空间都算进去：

\[
\text{rgbtarget} = \text{exposure\_scale} \cdot \text{texsamp}_{rgb} + (1 - \text{texsamp}_\alpha)\cdot \text{background}
\]

当 `random_bg_color` 开启，`background` 每条光线独立取随机值（[testbed_nerf.cu:965-967](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L965-L967)）。

**为什么随机背景能逼出透明度**：真值颜色 = 前景（训练像素）按 \(\alpha\) 混到随机背景上。若网络在某处预测的密度（从而 \(\alpha\)）不对，渲染色就会「漏」出那个随机背景——而背景每条光线都不同、与场景毫无相关性，唯一能让损失稳定下降的办法，就是让网络在该不透明处迅速累积出 \(\alpha\to1\)（正确遮挡随机背景），在该透明处保持 \(\alpha\to0\)（让随机背景透出来）。换言之，随机背景把「正确的占用/透明」变成了「能被监督信号区分」的东西。

#### 4.4.3 源码精读

`loss_and_gradient` 的 switch（[nerf_device.cuh:601-616](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L601-L616)）：

```cpp
case ELossType::RelativeL2:  return relative_l2_loss(target, prediction);
case ELossType::L1:          return l1_loss(target, prediction);
case ELossType::Mape:        return mape_loss(target, prediction);
case ELossType::Smape:       return smape_loss(target, prediction);
case ELossType::Huber:       return huber_loss(target, prediction, 0.1f) / 5.0f;  // 除以5让数值≈PSNR
case ELossType::LogL1:       return log_l1_loss(target, prediction);
default: case ELossType::L2: return l2_loss(target, prediction);
```

> 注意 Huber 那条注释（[nerf_device.cuh:607-612](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L607-L612)）：除以 5 是为了让 Huber 在零附近的 L2 区段与纯 L2 数值对齐，这样收敛后的损失读数近似 PSNR，方便和其他 NeRF 方法比 dB；Adam 这类自归一化优化器对常数因子不敏感，所以优化不受影响。

实际损失累加与梯度写入：`compute_loss_kernel_train_nerf` 先算总损失 `lg = loss_and_gradient(rgbtarget, rgb_ray, loss_type)`，再除以采样 pdf（[testbed_nerf.cu:1023-1024](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L1023-L1024)），并把每条光线的标量损失写进 `loss_output` 供 `update_after_training` 归约（[testbed_nerf.cu:1037-1040](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L1037-L1040)）：

```cpp
LossAndGradient lg = loss_and_gradient(rgbtarget, rgb_ray, loss_type);
lg.loss /= img_pdf * uv_pdf;
...
float mean_loss = mean(lg.loss);
if (loss_output) { loss_output[i] = mean_loss / (float)n_rays; }
```

有一条**容易被忽略但很重要**的注释（[testbed_nerf.cu:1031-1035](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L1031-L1035)）：梯度**故意不除** pdf。通常重要性采样里除 pdf 是为了「无偏」（只是降方差、不改变最优解）；但这里作者**想**让难样本的损失权重变高，从而真正改变优化目标，所以保留加权。这是 4.5 误差图采样的设计哲学。

`random_bg_color` 的声明与 GUI 开关（[testbed.h:793](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L793)、[testbed.cu:1198](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1198)），默认 `true`。损失类型的 GUI 下拉见 [testbed.cu:1203](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1203)。`base.json` 的 `loss.otype` 是 `Huber`（[configs/nerf/base.json:2-4](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json#L2-L4)）。

#### 4.4.4 代码实践

1. **目标**：对比不同 `loss_type` 对训练的影响。
2. **步骤**：加载 fox 训练，在 GUI 的 `NeRF training options → Loss` 下拉里依次切 `Huber / L2 / RelativeL2`，观察右下角 loss 曲线的**数值量级**变化（注意 Huber 被除了 5，读数偏小且近似 PSNR）。
3. **观察**：同时切 `Random bg color` 开/关，对比关闭后场景里「本应透明」的区域（如背景）是否会糊成一团固定颜色——这能直观看到随机背景对透明度学习的贡献。
4. **预期**：关掉 `random_bg_color` 后，空洞区域更容易被错误地填上密度。**待本地验证**。

#### 4.4.5 小练习与答案

**Q1**：为什么 NeRF 不直接用 tiny-cuda-nn 的 `Loss` 对象，而要在内核里手写 `loss_and_gradient`？
**A**：NeRF 的反传链路（体渲染 alpha 合成、密度/颜色激活）是手工实现的，需要的是「损失对网络输出 `[RGB,σ]` 的解析梯度」，再由链式法则手动传到每个采样点；`loss_and_gradient` 一次同时给出 loss 与梯度，正好嵌进这条手工反传。

**Q2**：`RelativeL2` 与普通 `L2` 有何不同？
**A**：`RelativeL2`（[nerf_device.cuh:83-90](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L83-L90)）把差值平方除以 `pred²+ε`，对暗部（小预测值）的误差更宽容、对亮部更敏感，常用于 HDR/环境贴图（`envmap.loss` 默认就是 `RelativeL2`）。

---

### 4.5 误差图（error_map）重要性采样

#### 4.5.1 概念说明

「重要性采样」的核心想法：**把训练资源多分给还没学好的地方**。instant-ngp 维护一张「误差图」——把每张训练图切成若干图块，累计每个图块上当前的损失；损失高的图块（难样本）下次被采到的概率更高。这能加速收敛、提升难视角的质量。

误差图分两级：
- **图像级**（`cdf_img`）：哪张训练图整体误差大，就多从那张图采光线（被 `image_idx` 使用）。
- **像素/图块级**（`cdf_x_cond_y` + `cdf_y`）：在选定的图里，哪个图块误差大，就多从那个图块采像素（被 `sample_cdf_2d` 使用）。

由两个开关分别控制（[testbed.h:810-811](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L810-L811)）：`sample_image_proportional_to_error` 与 `sample_focal_plane_proportional_to_error`。

注意：这套采样**不是无偏估计**——前面 4.4 讲过，梯度故意不除 pdf，等于「改变损失加权」而非「等价原问题的方差缩减」。这是作者有意为之的设计。

#### 4.5.2 核心流程

误差图的生命周期（全在 `train_nerf` 末尾，[testbed_nerf.cu:2790-2855](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2790-L2855)）：

```
每步训练内核里：把每条光线的 mean_loss 用双线性插值 deposit 到 error_map.data[img][tile]   // 累积
每 n_steps_between_error_map_updates 步：
    construct_cdf_2d  → 由 error_map.data 算每张图的 cdf_x_cond_y / cdf_y
    construct_cdf_1d  → 由 cdf_y 算图像级 cdf_img（GPU）
    CPU 上把 cdf_img 归一化，并混入 MIN_PMF=0.1 的均匀分量   // 防止某张图被饿死
    is_cdf_valid = true
    n_steps_between_error_map_updates *= 1.5              // 越往后重算越稀疏
```

下一次训练步里，`image_idx` / `sample_cdf_2d` 就拿这些 CDF 做反变换采样。

#### 4.5.3 源码精读

`ErrorMap` 结构体（[testbed.h:747-756](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L747-L756)）：

```cpp
struct ErrorMap {
    GPUMemory<float> data;          // 每张图每个图块的累积误差（原始 PMF）
    GPUMemory<float> cdf_x_cond_y;  // 条件 CDF：给定行，选列
    GPUMemory<float> cdf_y;         // 边缘 CDF：选行
    GPUMemory<float> cdf_img;       // 图像级 CDF：选图
    std::vector<float> pmf_img_cpu;
    ivec2 resolution = {16, 16};    // 误差图分辨率（图块数）
    ivec2 cdf_resolution = {16, 16};
    bool is_cdf_valid = false;
} error_map;
```

**累积**（融合内核 [train_nerf.cuh:276-305](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/train_nerf.cuh#L276-L305)，非融合 [testbed_nerf.cu:1042-1050](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L1042-L1050)）：把光线的 `mean_loss` 按双线性插值原子加到对应图块的 4 个相邻格子上。

**CDF 重算**（[testbed_nerf.cu:2813-2847](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2813-L2847)）：GPU 上 `construct_cdf_2d` / `construct_cdf_1d` 生成各级 CDF；图像级 CDF 再拷回 CPU 归一化并混入均匀分量（[testbed_nerf.cu:2842-2845](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2842-L2845)）：

```cpp
constexpr float MIN_PMF = 0.1f;
m_nerf.training.error_map.pmf_img_cpu[i] = (1.0f - MIN_PMF) * pmf * norm + MIN_PMF / n_images;
cdf_img_cpu[i] = (1.0f - MIN_PMF) * cdf * norm + MIN_PMF * (i + 1) / n_images;
```

`MIN_PMF = 0.1` 的作用：保证每张训练图**至少**有 10% 的均匀被采概率——否则一旦某张图初期误差低，就可能几乎永远采不到，陷入「误差图自我强化」的死循环。重算频率还会自衰减（`*= 1.5`，[testbed_nerf.cu:2854](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2854)）：训练后期场景趋于收敛，误差图变化变慢，没必要频繁重算。

**使用**：`sample_cdf_2d`（[nerf_device.cuh:499-528](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L499-L528)）做二维反变换采样（先按 `cdf_y` 选行、再按 `cdf_x_cond_y` 选列），且保留 `UNIFORM_SAMPLING_FRACTION = 0.5`（[nerf_device.cuh:497](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L497)）——即一半概率走均匀采样、一半走误差加权，同样是为了避免采样分布过度尖锐。`train_nerf_step` 里把这两个开关转成「传不传 CDF 指针」（[testbed_nerf.cu:3078-3080](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L3078-L3080)、[3152-3155](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L3152-L3155)）。

> 还有个可选的「锐度加成」：`include_sharpness_in_error` 开启时，会把训练图像的局部锐度也混进误差图（`sharpness_grid`，[train_nerf.cuh:287-299](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/fused_kernels/train_nerf.cuh#L287-L299)），让高频纹理区域被多采。

#### 4.5.4 代码实践

1. **目标**：观察误差图对采样分布的影响。
2. **步骤**：用 pyngp 加载一个带 test 视角的数据集（如 lego），分别设 `sample_image_proportional_to_error` 与 `sample_focal_plane_proportional_to_error` 为开/关，训练若干秒。
3. **观察**：开启时，GUI 里若有 `render_error_overlay`（[testbed.h:805-806](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L805-L806)）会可视化每张图的误差热力图——能看到误差高的图块被持续采样后逐渐变冷。
4. **预期**：开启重要性采样后，难视角的 PSNR提升更明显，但训练初期 loss 曲线可能反而偏高（因为更多采了难样本）。**待本地验证**。

#### 4.5.5 小练习与答案

**Q1**：为什么误差图采样要混入 `MIN_PMF=0.1` 的均匀分量和 `UNIFORM_SAMPLING_FRACTION=0.5`？
**A**：纯按误差采样会让初期误差低的图/图块几乎永不被采，误差图失去更新机会而自我强化；混入均匀分量保证所有区域都有最低采样概率，使误差图能持续被「重新发现」并修正。

**Q2**：误差图的更新频率为什么每轮 `*= 1.5`？
**A**：训练后期网络收敛、误差分布变化变慢，频繁重算 CDF 是浪费；指数衰减让早期频繁更新（快速锁定难区域）、后期稀疏更新（节省开销）。

---

## 5. 综合实践

**任务**：用 `pyngp` 写一个最小脚本，把本讲四个机制（批量采样、压缩、损失类型、误差图）串起来观察。

参考骨架（依赖 u7-l1 的 pyngp 绑定）：

```python
# 示例代码：非项目原有，仅作练习示范
import pyngp as ngp

testbed = ngp.Testbed(ngp.TestbedMode.Nerf)
testbed.load_training_data("data/nerf/fox")
testbed.reload_network_from_file("configs/nerf/base.json")

# 实验 1：切换损失类型，对比 loss 曲线
for loss in ["Huber", "L2", "RelativeL2"]:
    testbed.loss_type = loss  # 由 pyngp 暴露的属性（见 python_api.cu 绑定）
    testbed.train_mode = ngp.TrainMode.Nerf
    for _ in range(1000):
        testbed.frame()
    print(loss, "loss:", testbed.loss_scalar)

# 实验 2：开关随机背景色，观察空洞区域
testbed.random_bg_color = False
# 实验 3：开关误差图重要性采样
testbed.sample_image_proportional_to_error = True
testbed.sample_focal_plane_proportional_to_error = True
```

**要求**：
1. 记录三种 `loss_type` 下 1000 步后的 `loss_scalar`，验证 Huber 的数值是否确实比 L2 小约 5 倍（呼应 4.4 的「除以 5」）。
2. 关掉 `random_bg_color` 后，对 fox 这种有明确前景的场景，观察背景区域是否出现「错误填充」。
3. （进阶）尝试在 `update_after_training` 附近加日志（需改源码、自行编译），打印 `measured_batch_size_before_compaction` 与 `measured_batch_size`，实测压缩率。

> 本练习的 pyngp 属性名以 [src/python_api.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu) 的实际绑定为准；若某属性未暴露，改用 GUI 滑块手动对照实验。**待本地验证**。

---

## 6. 本讲小结

- NeRF 训练按「**小批量光线**」进行：每步随机采 `rays_per_batch` 条光线，每条光线 marching 出若干采样点，把采样点颜色与像素真值做损失反传；`target_batch_size`（默认 262144）控制的是**采样点数**而非光线数。
- 一条光线的诞生 = `image_idx` 选图 + `nerf_random_image_pos_training` 选像素；marching 借 `density_grid_bitfield` 跳过空体素，只在占据格子里停步，停一次产出一个 `NerfCoordinate`。
- `NerfCounters` 用 `numsteps_counter` / `numsteps_counter_compacted` 两个就地计数器分别统计「压缩前总点数」与「压缩后有效点数」，并据此动态缩放 `rays_per_batch` 稳定命中 `target_batch_size`。
- **光线压缩**剔除了每根光线上「累积透射率 \(T<10^{-4}\)」之后的死点——它们对颜色与梯度无贡献；压缩让定长 batch 装满真正有梯度的样本，是非融合与融合两条路径的关键差异点。
- 损失由内核里的 `loss_and_gradient` 解析地返回（7 种 `ELossType`，默认 Huber，除以 5 使读数近似 PSNR）；`random_bg_color` 用随机背景逼迫模型学会正确的占用/透明。
- **误差图重要性采样**把训练资源导向难样本（图像级 + 图块级两级 CDF），但故意不除 pdf，属于「改变损失加权」而非无偏偏方差缩减；`MIN_PMF` 与 `UNIFORM_SAMPLING_FRACTION` 防止采样分布过度尖锐。

---

## 7. 下一步学习建议

- 读完本讲，NeRF 的「数据 → 网络 → 渲染 → 训练」闭环已完整。建议接着读 [u4-l5 密度网格与空区域跳过](u4-l5-density-grid.md)，深入理解本讲反复用到的 `density_grid_bitfield` 是如何被 EMA 更新与位域压缩的——它直接决定了 marching 的效率与 `measured_batch_size` 的大小。
- 若对「密度网格采样如何反过来依赖网络输出」感兴趣，可看 `update_density_grid_nerf` 与 `NerfNetwork::density()` 捷径（u4-l2 讲过的密度专用前向）。
- 想动手做受控实验的读者，可跳到 [u7-l1 pyngp 绑定架构](u7-l1-pyngp-bindings.md) 与 [u7-l2 run.py](u7-l2-run-py-script.md)，把本讲的 loss_type / 误差图开关写成可复现的评测脚本。
- 对性能极致感兴趣者，后续 [u8-l2 JIT 融合与全融合内核](u8-l2-jit-fusion.md) 会讲清本讲「融合 vs 非融合」两条路径的内核生成机制（`generate_device_function`），解释为什么 Rfl/RflRelax 训练模式只能在 JIT 融合下使用。
