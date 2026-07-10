# u4-l5 密度网格与空区域跳过

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 NeRF 为什么需要一张「密度网格」、它缓存的是什么。
- 画出 `update_density_grid_nerf → update_density_grid_mean_and_bitfield` 的完整数据流，并解释网格值如何从「网络密度」一步步变成「一个比特」。
- 理解 `density_grid_decay`、均匀/非均匀采样在网格更新中的作用，并能指出源码里「EMA」这个名字与实际「衰减最大值」行为之间的差异。
- 看懂 `density_grid_bitfield` 的位域布局与级联 mip 金字塔，以及 `density_grid_occupied_at` 如何查询它。
- 解释渲染阶段 `if_unoccupied_advance_to_next_occupied_voxel` 如何利用位域跳过空体素，以及 `mark_density_grid_in_sphere_empty` 这类交互编辑能力。

## 2. 前置知识

本讲建立在 **u4-l3（NeRF 光线步进与体渲染）** 之上。回顾两个关键概念：

1. **体渲染靠沿光线采样**：每条像素光线都要沿其路径采样很多个点，每个点都要查询一次 `NerfNetwork` 得到 `[RGB, σ]`，再做 alpha 合成。
2. **绝大多数采样点落在空气里**：一个典型 NeRF 场景中，光线穿过的大部分体积都是空的，网络的密度输出接近 0。对这些空点做 MLP 查询是纯浪费——而且 NeRF 场景越大、空区域越多，浪费越严重。

本讲要解决的问题正是：**能否用一个廉价的占位查询，先判断「这一片是不是空的」，空的就直接跳过去，不再打扰网络？**

答案是肯定的，工具就是**密度网格（density grid）**。先建立直觉：

| 概念 | 直觉比喻 |
|------|----------|
| 密度网格 | 把整个场景体积分成 \(128\times128\times128\) 个小方块，每块记一个「这块有没有东西」的粗糙估计 |
| 位域（bitfield） | 把「有没有东西」进一步压成 1 个比特（0/1），省显存、查得快 |
| 空区域跳过 | 光线在空气里直接「瞬移」到下一个可能有东西的方块，省掉中间所有网络查询 |

只要理解这三点，后面的源码就是把它们落地。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/testbed_nerf.cu` | 密度网格的全部业务逻辑：更新、压缩、交互编辑都在这里 |
| `include/neural-graphics-primitives/testbed.h` | 声明 `Nerf` 结构体内的 `density_grid` 等成员，以及相关函数签名 |
| `include/neural-graphics-primitives/nerf_device.cuh` | 渲染侧的设备函数：位域查询、空区域跳过、级联 mip 都在这里（被光线步进内核调用） |

> 注意：本讲规格列出的源码是前两个文件，但渲染阶段的跳过逻辑实际落在 `nerf_device.cuh`。这个头文件是 GPU 设备函数库，被 `testbed_nerf.cu` 和融合内核共同包含，引用它是真实调用链的一部分。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 density_grid 的更新**（采样 → splat → 衰减最大值）
- **4.2 位域压缩与级联 mip**（float → bit → 金字塔）
- **4.3 空区域跳过与交互编辑**（查询位域、跳过空体素、挖洞）

数据流贯穿三者：网络密度 `→` 4.1 写入 `density_grid`(float) `→` 4.2 压成 `density_grid_bitfield`(bit) `→` 4.3 在光线步进时被查询跳过。

---

### 4.1 density_grid 的更新：跟踪网络输出的密度

#### 4.1.1 概念说明

`density_grid` 是一个粗粒度的「网络密度缓存」。它把单位立方体场景切成 \(128^3\) 个格子（`NERF_GRIDSIZE()=128`），每个格子存一个 float：网络在该格子附近输出的**光学厚度（optical thickness）**的某种平滑估计。有了它，渲染时就不用每个点都问网络，而是先问这个粗网格。

为什么需要「平滑/跟踪」而不是一次性算死？因为**网络本身在不断训练**，它对每个位置的密度预测一直在变。早期某个格子可能是空的，训练后变得有内容；反过来也可能。所以密度网格必须像一个低通滤波器，随训练逐步更新、跟踪网络的最新输出。

这里有个重要的命名陷阱：源码里这个量叫「EMA（指数移动平均）」，字段叫 `density_grid_decay`，注释也写「EMA smoothed densities」。但**真正的实现并不是 EMA，而是「衰减最大值（decayed max）」**——4.1.3 会用代码证明这一点，并解释为什么作者要这么做。

#### 4.1.2 核心流程

`update_density_grid_nerf` 每次被调用时做这些事（一个 GPU stream 内串成一条流水线）：

```
1. 把 density_grid resize 成 NERF_GRID_N_CELLS() * (max_cascade+1) 个 float
   （max_cascade 由场景大小决定，见 u4-l3）

2. 【仅首步/新增图像时】mark_untrained_density_grid:
   把「没有任何相机能看到」的格子标记为 -1.0（永久空，不参与训练/渲染）

3. 生成采样点 generate_grid_samples_nerf_nonuniform（跑两遍）:
   (a) 均匀采样: 在「未被标记为 untrained」的格子里随机选一批
   (b) 非均匀/重要性采样: 在「当前已经有密度」的格子里随机选一批

4. 分批调用 m_nerf_network->density(...)，在每个采样点查询网络密度

5. splat_grid_samples_nerf_max_nearest_neighbor:
   用 atomicMax 把每个采样点的光学厚度写回它所属格子的临时缓冲

6. ema_grid_samples_nerf:
   把临时缓冲合并进 density_grid（实际是衰减最大值，见下）

7. update_density_grid_mean_and_bitfield: 压成位域（4.2 节）
```

其中第 6 步的合并公式（记 \(\lambda\) 为 `density_grid_decay`，\(s_i\) 为本步 splat 到格子 \(i\) 的最大光学厚度）：

\[
d_i^{(t)} =
\begin{cases}
d_i^{(t-1)} & \text{若 } d_i^{(t-1)} < 0 \quad\text{(untrained 格子被锁定)}\\[4pt]
\max\!\big(\lambda\, d_i^{(t-1)},\; s_i^{(t)}\big) & \text{否则}
\end{cases}
\]

真正的 EMA 形式应是 \(\lambda d_{i}^{(t-1)} + (1-\lambda)s_i^{(t)}\)；而上式是「先把旧值衰减、再与新值取 max」——只要新值更显著就立刻把格子点亮，旧值只会慢慢衰减变暗。这种不对称正是为了捕捉细薄特征。

#### 4.1.3 源码精读

**数据结构**——四个成员，都在 `Nerf` 结构体内：

[include/neural-graphics-primitives/testbed.h:860-864](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L860-L864)：`density_grid`（float 网格本体）、`density_grid_bitfield`（压缩后的位域）、`density_grid_mean`（全网格平均密度，定阈值用）、`density_grid_ema_step`（更新步数计数器）。注释里的 `EMA smoothed` 就是前文提到的命名与实际行为不符之处。

[include/neural-graphics-primitives/testbed.h:818-819](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L818-L819)：衰减系数 `density_grid_decay = 0.95f` 与配套随机数发生器 `density_grid_rng`。`0.95` 控制旧密度以多快速度淡出。

**首步标记 untrained**——`mark_untrained_density_grid` 把相机看不见的格子打上 \(-1\)：

[src/testbed_nerf.cu:87-162](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L87-L162)：它对每个格子取 8 个角点，逐张训练图判断角点是否落在该图视锥内；只要至少 1 张图能看到（`min_count=1`），格子就是可训练的（写 `0.f`），否则写 `-1.f`。注释指出把 `min_count` 调到 2 能压制 floater（悬浮噪点），代价是引入重建瑕疵。

**生成采样点**——`generate_grid_samples_nerf_nonuniform` 用 `thresh` 参数区分两类采样：

[src/testbed_nerf.cu:216-257](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L216-L257)：随机选一个 cascade 层，再哈希式地挑格子、最多重试 10 次直到 `grid_in[idx] > thresh`。第 253 行把格子坐标加上一个随机偏移，得到格子内的随机采样位置。

**splat**——把网络输出写回临时网格：

[src/testbed_nerf.cu:259-284](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L259-L284)：先 `network_to_density` 把 MLP 原始值转成密度，再乘以最小步距得到光学厚度；然后用 `atomicMax((uint32_t*)&grid_out[local_idx], ...)` 写入。注释解释了为什么 `atomicMax` 合法：正浮点数的位模式按 uint 解释时仍单调，所以 uint 的 atomicMax 等价于 float 的 max。

**合并（EMA 的真相）**：

[src/testbed_nerf.cu:316-338](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L316-L338)：核心是第 335–337 行 `float val = (prev_val < 0.f) ? prev_val : fmaxf(prev_val * decay, importance);`。注释 332–333 明确写着「Maximum instead of EMA allows capture of very thin features」（用最大值代替 EMA，才能捕捉非常细薄的特征；只要格子里出现任何可见物，就立刻把它点亮）。被注释掉的 326–330 行才是真正的去偏 EMA，说明作者曾用过 EMA、后来改成了衰减最大值。

**两遍采样的参数**——`update_density_grid_nerf` 主体：

[src/testbed_nerf.cu:2527-2557](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2527-L2557)：第一遍 `thresh = -0.01f`（选「非 untrained」格子，即 \(-0.01\) 排除了 \(-1\) 的 untrained 格子），对应**均匀采样**参数 `n_uniform_density_grid_samples`；第二遍 `thresh = NERF_MIN_OPTICAL_THICKNESS()=0.01f`（只选当前已有密度的格子），对应**非均匀/重要性采样**参数 `n_nonuniform_density_grid_samples`。

**采样配比随训练变化**——`training_prep_nerf`：

[src/testbed_nerf.cu:3385-3398](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L3385-L3398)：训练前 256 步（`m_training_step < 256`）全部用均匀采样（`n_nonuniform=0`）来冷启动网格；256 步之后改成「一半均匀、一半重要性」，既保证覆盖又把算力集中到已经有内容的区域。

#### 4.1.4 代码实践

**实践目标**：确认「EMA」是名、衰减最大值是实，并理解 `density_grid_decay` 的作用。

**操作步骤（源码阅读型实践，无需 GPU）**：

1. 打开 `src/testbed_nerf.cu`，定位 `ema_grid_samples_nerf`（316 行起）。
2. 阅读被注释掉的 326–330 行（真 EMA）与生效的 335–337 行（衰减最大值），写下一句话说明两者区别。
3. 在 `testbed.h:818` 把 `density_grid_decay` 在脑中分别改成 `0.5f` 和 `0.99f`，预测：哪个会让网格更快「遗忘」已经空掉的旧格子？哪个会让早期瞬态特征更顽固地残留？
4. （可选，需编译环境）用 pyngp 加载 fox 训练若干秒后观察渲染：临时改大 `density_grid_decay` 接近 1，看悬浮噪点是否变多——印证注释里「max 能捕捉细薄特征，但也更容易点亮 floater」的权衡。

**需要观察的现象**：`decay` 越大（越接近 1），格子被点亮后越难熄灭，网格更「粘」；越小则网格响应越快但越易抖动。

**预期结果**：能用自己的话说出「这里名为 EMA、实为 `max(λ·旧, 新)`，目的是不漏掉细薄高密度结构」。

#### 4.1.5 小练习与答案

**Q1**：为什么 `splat_grid_samples_nerf_max_nearest_neighbor` 用 `atomicMax` 而不是 `atomicAdd`？

**答案**：一个格子可能被多个采样点命中，`atomicAdd` 会把密度累加、量纲被破坏（光学厚度不该简单相加）；而取 `max` 表示「这个格子里最显著的那次网络输出」，既保持了光学厚度的物理含义，又能在后续 `ema` 步骤里以衰减最大值的方式合并，语义自洽。注释 281–282 还指出正浮点的位模式单调，故 uint 的 atomicMax 可直接复用。

**Q2**：`density_grid_ema_step` 这个计数器是干什么用的？

**答案**：它记录密度网格已经被更新了多少步，主要作为随机数发生器的位移种子（`generate_grid_samples_nerf_nonuniform` 的 `step` 参数）和去偏参考（被注释掉的 EMA 去偏项用到）。每次 `update_density_grid_nerf` 末尾 `++m_nerf.density_grid_ema_step`，重置网络/首步时清零。

---

### 4.2 位域压缩与级联 mip：float 怎么变成 bit

#### 4.2.1 概念说明

`density_grid` 是 float 数组，对 \(128^3\) 格子、单个 cascade 就要 \(128^3\times 4\text{B}\approx 8\,\text{MiB}\)。渲染内循环里每条光线、每一步都要查它——读 float 既费带宽又费寄存器。

`density_grid_bitfield` 的思路：我们其实**只关心「这个格子有没有东西」，不需要确切数值**。于是把每个 float 压成 1 个 bit：超过阈值就是 1（占用），否则 0（空）。这是 \(32\times\) 压缩（32 bit → 1 bit），单 cascade 从 8 MiB 降到约 256 KiB，且逐位查询几乎零开销。

在此基础上还做了**级联 mip（cascade mip）**：把占用信息按越来越粗的分辨率组织成金字塔。渲染时远处/低分辨率的区域用粗 mip 一次覆盖更大空间，跳得更远。级联层数 `NERF_CASCADES()=8`，与 u4-l3 讲的多 cascade 大场景机制一致。

#### 4.2.2 核心流程

`update_density_grid_mean_and_bitfield` 三步：

```
1. reduce_sum 算出全网格的平均密度 density_grid_mean
   （先对每个值取 max(val,0) 再除以格子总数）

2. grid_to_bitfield: 每个 thread 处理连续 8 个 float 格子 → 输出 1 个 byte
   thresh = min(NERF_MIN_OPTICAL_THICKNESS=0.01, mean)
   第 j 个格子的密度 > thresh → byte 的第 j 位置 1

3. bitfield_max_pool: 对 level = 1..7
   把上一层(level-1)每 8 个 bit 用 OR 合成下一层(level)的 1 个 bit
   （只要子格子里任何一个占用，父格子就算占用 → 占用金字塔）
```

阈值公式：

\[
\text{thresh} = \min\big(\text{NERF\_MIN\_OPTICAL\_THICKNESS},\; \bar d\big)
\]

其中 \(\bar d\) 是平均密度。这样当场景整体很稀疏时阈值自动降低，避免「全都低于固定阈值而整片被判定为空」。

压缩比例示意（单 cascade，\(N=128^3=2{,}097{,}152\)）：

| 量 | 大小 |
|----|------|
| `density_grid` (float) | \(N \times 4\text{B} = 8\,\text{MiB}\) |
| `density_grid_bitfield` (bit) | \(N\,\text{bit} = 256\,\text{KiB}\) |
| 压缩比 | \(32\times\) |

#### 4.2.3 源码精读

**入口**——`update_density_grid_mean_and_bitfield`：

[src/testbed_nerf.cu:2594-2633](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2594-L2633)：先 `reduce_sum` 算 `density_grid_mean`（2602–2608，lambda 里 `fmaxf(val,0.f)/n_elements`），再调 `grid_to_bitfield`，最后循环 7 次调 `bitfield_max_pool` 建金字塔，最后 `set_all_devices_dirty()` 通知多 GPU 刷新（见 u8-l1）。

**float → byte**——`grid_to_bitfield`：

[src/testbed_nerf.cu:348-374](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L348-L374)：第 366 行 `thresh = std::min(NERF_MIN_OPTICAL_THICKNESS(), *mean_density_ptr);`，第 369–371 行循环 8 次，`bits |= grid[i*8+j] > thresh ? (1<<j) : 0;`。注意 359–362 行：超过 `n_nonzero_elements`（即 `max_cascade+1` 之外的 cascade）的字节直接置 0。

**占用金字塔**——`bitfield_max_pool`：

[src/testbed_nerf.cu:376-396](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L376-L396)：第 385–389 行把上一层 8 个 bit OR 成 1 个 bit（「任一子占用则父占用」），第 391–395 行用 Morton 码 + `NERF_GRIDSIZE()/8` 偏移定位到下一层对应位置，`|=` 累加写入。这是 max pooling 在二值占用图上的实现。

**位域的内存布局**——`grid_mip_offset` 与 `get_density_grid_bitfield_mip`：

[include/neural-graphics-primitives/nerf_device.cuh:331-333](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L331-L333)：`grid_mip_offset(mip) = NERF_GRID_N_CELLS() * mip`，注意单位是**比特**——每层占用 \(128^3\) bit（=256 KiB）。

[src/testbed_nerf.cu:3654](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L3654)：`get_density_grid_bitfield_mip(mip)` 返回 `density_grid_bitfield.data() + grid_mip_offset(mip)/8`，即第 `mip` 层的字节起始地址。8 层共占 \(128^3\times 8/8 = 2\,\text{MiB}\)。

**相关常量**：

[include/neural-graphics-primitives/nerf_device.cuh:25-26](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L25-L26)：`NERF_GRIDSIZE()=128`、`NERF_GRID_N_CELLS()=128^3`。

[include/neural-graphics-primitives/nerf_device.cuh:30](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L30) 与 [include/neural-graphics-primitives/nerf_device.cuh:43](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L43)：`NERF_CASCADES()=8`、`NERF_MIN_OPTICAL_THICKNESS()=0.01f`（占用阈值）。

#### 4.2.4 代码实践

**实践目标**：手算一个 float 格子变成 bitfield 里一个比特的全过程。

**操作步骤（纸笔实践）**：

1. 假设单 cascade，某个格子 `density_grid[i] = 0.03`（光学厚度 0.03），全网格平均 `mean_density_ptr` 算出来是 `0.005`。
2. 计算 `thresh = min(0.01, 0.005) = 0.005`。
3. 判断 `0.03 > 0.005` → 成立 → 该格子在它所在 byte 的对应 bit 位置 1。
4. 若该格子是该 byte 的第 3 个（`j=3`），则 byte 值贡献 `1<<3 = 0b00001000`。
5. 推算：8 个这样的 float（256 bit 信息）最终塌缩成 1 个 byte（8 bit），压缩 \(32\times\)。

**需要观察的现象**：阈值随 `mean_density` 自适应——场景越稀疏，阈值越低，越容易把微弱密度判为「占用」。

**预期结果**：能画出 `8 个 float → 1 个 byte` 的对应关系，并说出 `thresh` 为何取 `min(固定, 平均)`。

#### 4.2.5 小练习与答案

**Q1**：`grid_to_bitfield` 里 `thresh = min(NERF_MIN_OPTICAL_THICKNESS, mean)`，为什么用 `min` 而不是固定阈值？

**答案**：`NERF_MIN_OPTICAL_THICKNESS=0.01` 是绝对上限；当场景整体密度很低（平均 < 0.01）时，若仍用 0.01 会把几乎所有格子判空、网格失效。取 `min` 让阈值随场景稀疏度自适应下降，保证「相对显著」的格子被保留。代价是稀疏场景里阈值更敏感、更易受噪点影响。

**Q2**：`bitfield_max_pool` 用 OR 聚合而非 AND，对渲染跳过有什么影响？

**答案**：OR 意味着「只要子层有一个占用，父层就占用」——这是保守（偏松）的占用估计。好处是从不漏掉真实内容（不会把有东西的区域误判成空而跳过）；代价是可能把「仅边缘有一点点」的大格子在粗 mip 上整体标为占用，导致跳过不够激进、多算一些空查询。对正确性无害，只是少省一点算力。

---

### 4.3 空区域跳过与交互编辑：bit 怎么帮光线「瞬移」

#### 4.3.1 概念说明

前两节造好了 `density_grid_bitfield` 这张占用地图。本节讲它**在哪里、怎么被用**：光线步进内核在决定下一步采样位置前，先查位域，若当前所在体素是空的，就一次性跳到下一个占用体素，中间的网络查询全省掉。

这套逻辑由 `density_grid_occupied_at`（查询）和 `if_unoccupied_advance_to_next_occupied_voxel`（跳过）两个设备函数实现。后者还有个聪明之处：**遇到空体素时，它会尝试往更粗的 mip 攀升**——既然这一小格是空的，那包含它的更大格子是不是也空？若是，就按更大格子的步长一次跳得更远。

另外，密度网格不只是被动跟踪网络，还支持**交互式编辑**：`mark_density_grid_in_sphere_empty` 允许用户指定一个球，把球内的格子直接清成空（写 \(-1\)，永久锁定为空）。这是去除 floater、挖出裁剪区域的实用手段。

#### 4.3.2 核心流程

**查询一个点是否占用**（`density_grid_occupied_at`）：

```
1. 由 pos 与 mip 算出该点在 128^3 网格里的 Morton 索引 idx
   (cascaded_grid_idx_at: 先按 2^-mip 缩放定位 cascade，再 floor 到格子)
2. byte = bitfield[ idx/8 + grid_mip_offset(mip)/8 ]
3. bit = byte & (1 << (idx%8))   非零即占用
```

**沿光线跳过空区域**（`if_unoccupied_advance_to_next_occupied_voxel`）：

```
loop:
    pos = ray(t)
    若超出包围盒/最大深度 → 返回 MAX_DEPTH（光线终止）
    mip = 由 pos（或步距 dt）选合适的级联层
    若 density_grid_occupied_at(pos, mip) → 返回 t（命中占用区，停下来采样网络）
    否则:
        while mip < max_mip 且 mip+1 层也空: ++mip
            （找到能覆盖当前点的最大空体素）
        t = advance_to_next_voxel(t, ..., mip)
            （按该 mip 的格子分辨率做 DDA，跳到下一个格子边界）
        继续循环
```

`advance_to_next_voxel` 是 DDA 式的体素边界跨越：在所选 mip 的分辨率下，沿光线方向找到最近一个坐标分量取整边界，把 `t` 推到那里。mip 越粗，格子越大，一步跳得越远。

**交互挖洞**（`mark_density_grid_in_sphere_empty`）：

```
对每个格子:
    算格子中心位置 cell_pos 与格子等效半径 cell_radius（含 √3 对角线、按 cascade 层缩放）
    若 distance(球心, cell_pos) < radius + cell_radius:
        density_grid[i] = -1.0   （永久空，被 ema 步骤锁定，不参与训练/渲染）
调 update_density_grid_mean_and_bitfield 重建位域
```

#### 4.3.3 源码精读

**占用查询**——`density_grid_occupied_at`：

[include/neural-graphics-primitives/nerf_device.cuh:335-341](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L335-L341)：第 340 行 `return density_grid_bitfield[idx/8+grid_mip_offset(mip)/8] & (1<<(idx%8));`——这正是「byte = bitfield[idx/8+...]; bit = idx%8」的位运算。`idx==0xFFFFFFFF`（点在网格外）时第 337–338 行返回 false（视为空/不占用）。

**Morton 索引**——`cascaded_grid_idx_at`：

[include/neural-graphics-primitives/nerf_device.cuh:317-329](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L317-L329)：第 318–321 行用 `scalbnf(1,-mip)` 把坐标按 cascade 层缩放到 \([-0.5,0.5]\) 中心立方体，第 323 行乘以 `NERF_GRIDSIZE()` 取整得到 `ivec3`，第 328 行 `morton3D` 编码成一维索引。越界返回 `0xFFFFFFFF`。

**跳过内核**——`if_unoccupied_advance_to_next_occupied_voxel`：

[include/neural-graphics-primitives/nerf_device.cuh:462-495](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L462-L495)：第 482 行 `if (!density_grid || density_grid_occupied_at(pos, density_grid, mip)) return t;`——没有网格（其它原语）或当前格占用就直接采样。第 489–491 行 `while (mip < max_mip && !density_grid_occupied_at(pos, density_grid, mip+1)) ++mip;` 是「往粗 mip 攀升找最大空体素」的核心，第 493 行 `advance_to_next_voxel` 执行跳跃。

**体素跨越**——`advance_to_next_voxel` 与 `distance_to_next_voxel`：

[include/neural-graphics-primitives/nerf_device.cuh:431-441](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L431-L441) 配合 [include/neural-graphics-primitives/nerf_device.cuh:360-368](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L360-L368)：`distance_to_next_voxel` 求 `min(tx,ty,tz)`——三个轴到下一个整数边界的距离取最小，即 DDA。`res = scalbnf(NERF_GRIDSIZE(), -mip)` 让粗 mip 用更小的 `res`、覆盖更大空间。

**渲染中的调用点**——`advance_pos_nerf`：

[src/testbed_nerf.cu:420-423](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L420-L423)：光线推进时先 `advance_n_steps` 做常规步进，紧接着就调 `if_unoccupied_advance_to_next_occupied_voxel` 把 `t` 进一步推过空区域——这就是位域在光线步进主循环里的实际入口（融合渲染路径 `render_nerf.cuh` 内部也走同样的设备函数）。

**交互挖洞**——`mark_density_grid_in_sphere_empty`：

[src/testbed_nerf.cu:2658-2667](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2658-L2667)：调度 `mark_density_grid_in_sphere_empty_kernel` 后立即 `update_density_grid_mean_and_bitfield` 重建位域。内核 [src/testbed_nerf.cu:2635-2656](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2635-L2656) 第 2653 行用 `distance(pos, cell_pos) < radius + cell_radius`（`cell_radius` 用 `SQRT3` 包住整个格子的对角线，保守判定），命中即写 \(-1.0\)。因为 `ema_grid_samples_nerf` 对负值原样保留（4.1 的公式第一支），这些格子会被永久锁空。

#### 4.3.4 代码实践

**实践目标**：完整追踪「一个 3D 格子的网络密度 → bitfield 的一个比特 → 渲染时让光线跳过」的端到端链路（本讲规格指定的主实践任务）。

**操作步骤**：

1. **采样与评估**：在 `update_density_grid_nerf`（[src/testbed_nerf.cu:2476](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2476)）中，`generate_grid_samples_nerf_nonuniform` 选出格子并生成采样点；`m_nerf_network->density(...)`（2570 行）查询网络得到密度。
2. **写回格子**：`splat_grid_samples_nerf_max_nearest_neighbor`（2574 行）用 `atomicMax` 把光学厚度写进 `density_grid_tmp` 对应格子。
3. **合并**：`ema_grid_samples_nerf`（2585 行）以衰减最大值把 `density_grid_tmp` 并入 `density_grid`。**至此，某个 3D 格子获得了一个 float 密度值。**
4. **压成 bit**：`update_density_grid_mean_and_bitfield`（2591 行）→ `grid_to_bitfield`（2611 行）把该 float 与 `thresh` 比大小，写成它所在 byte 的某个 bit。**至此，该格子的密度塌缩成一个比特。**
5. **定位该 bit**：渲染时 `density_grid_occupied_at`（[nerf_device.cuh:335](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L335)）用 Morton 码算 `idx`，再 `bitfield[idx/8+grid_mip_offset(mip)/8] & (1<<(idx%8))` 取出该 bit。
6. **跳过决策**：`if_unoccupied_advance_to_next_occupied_voxel`（[nerf_device.cuh:462](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L462)）查到该 bit 为 0（空），就 `advance_to_next_voxel` 跳到下一个格子；为 1（占用）则停下，让 `NerfTracer`/融合内核在该点采样网络。

**需要观察的现象**：你能用一句话串起「密度值 > thresh ⇒ bit=1 ⇒ 光线在此停下查询网络；密度值 ≤ thresh ⇒ bit=0 ⇒ 光线跳过」。

**预期结果**：画出一张数据流图，标出 float `density_grid` 在哪一步变成 bit `density_grid_bitfield`，又在哪一步被光线步进读取。**若你无法在本地编译运行，明确标注「待本地验证」**——本实践以源码追踪为主，不需要 GPU。

#### 4.3.5 小练习与答案

**Q1**：`if_unoccupied_advance_to_next_occupied_voxel` 第 489 行为什么要 `while` 攀升 mip，而不是直接用当前位置算出的 mip？

**答案**：当前 mip 对应的格子是空的，但更大（更粗）的格子可能也空。若能用更粗的 mip，`advance_to_next_voxel` 的 `res` 更小、一步跨越的物理距离更大，能跳过整片空区域。攀升到「仍然空的最粗 mip」是在保证不跳过占用区的前提下，最大化单步跳跃距离。这就是级联 mip 金字塔在渲染侧的收益。

**Q2**：`mark_density_grid_in_sphere_empty` 把格子写成 \(-1.0\) 而不是 `0.0`，为什么？

**答案**：`ema_grid_samples_nerf` 对负值走第一分支原样保留（不会被新采样覆盖、也不会被衰减），所以 \(-1\) 是「永久空」标记。若只写 `0.0`，下一步密度采样一旦 splat 到正值就会把它重新点亮，挖洞效果立刻丢失。负值还让 `grid_to_bitfield` 中 `0 > thresh`（thresh 为正）为假，对应 bit 自然为 0。

---

## 5. 综合实践

**任务：用密度网格参数解释一次渲染卡顿/悬浮噪点的来源。**

1. 选一个你能在本地跑起来的小 NeRF 场景（如 `data/nerf/fox`）。加载训练几秒到基本收敛。
2. 打开 GUI 的 Rendering 面板与 NeRF 面板，找到与密度网格相关的可调项（如 `density_grid_decay`、可视化 `show_accel`）。源码里 `show_accel`（[testbed.h:879 附近](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L879)）可以把密度网格本身画出来。
3. 观察并记录：
   - 训练初期（前 256 步）`show_accel` 显示的占用分布是否更「满」？（对应 `training_prep_nerf` 此阶段全均匀采样冷启动。）
   - 把 `density_grid_decay` 调高（接近 1）后，渲染里是否出现更多悬浮噪点？（对应 4.1 讲的「衰减最大值更易点亮细薄/floater」。）
4. 用 `mark_density_grid_in_sphere_empty`（或 GUI 等价操作，如裁剪/挖洞工具）清掉一个明显悬浮的小球，观察它是否在后续训练中**不再**重新出现——印证 4.3 的「\(-1\) 永久锁空」。
5. 写一段话，把观察到的现象与本讲三处源码（衰减最大值、`thresh=min(...)`、攀升 mip 跳过）对应起来。

> 若无 GUI/编译环境，可改为**纯源码综合实践**：以 `data/nerf/fox` 为例，画出从「网络一次 `density()` 调用」到「光线在 `if_unoccupied_advance_to_next_occupied_voxel` 中跳过一个空体素」的完整调用链与数据流图，标注每一处涉及的文件与行号，并解释「为什么把 `NERF_MIN_OPTICAL_THICKNESS` 调大会让渲染更快但细节更糊」。

## 6. 本讲小结

- NeRF 场景绝大多数体积是空气；`density_grid` 是一个 \(128^3\) 的粗网格，缓存网络输出的密度，让光线步进能先廉价查表、跳过空区域，避免在空气里反复查询昂贵的 MLP。
- `density_grid` 名义上是「EMA」，实际用的是**衰减最大值** `max(λ·旧, 新)`（λ=`density_grid_decay`=0.95）：目的是不漏掉细薄高密度特征，只要格子里出现可见物就立刻点亮。
- 采样分均匀（`thresh=-0.01`，排除 untrained）与非均匀/重要性（`thresh=0.01`，聚焦已占用）两遍；前 256 步全均匀冷启动，之后各占一半。相机看不到的格子由 `mark_untrained_density_grid` 标记为 \(-1\) 永久空。
- 位域压缩把每个 float（32 bit）压成 1 个 bit（\(32\times\) 压缩），阈值 `thresh=min(0.01, 平均密度)` 自适应；再由 `bitfield_max_pool` 用 OR 把占用信息逐层聚合，建成 8 层级联 mip 金字塔。
- 渲染时 `density_grid_occupied_at` 用 Morton 码逐位查询；`if_unoccupied_advance_to_next_occupied_voxel` 遇空就往更粗 mip 攀升、用 DDA 一次跳到下一个占用体素，实现「瞬移」。
- `mark_density_grid_in_sphere_empty` 把球内格子写 \(-1\) 永久锁空，是去除 floater、交互挖洞的手段。

## 7. 下一步学习建议

- **横向对比 Volume 原语**（u5-l4）：体素模式同样做体积光线步进，但它的「占用」来自 NanoVDB 的 `bitgrid`/`global_majorant` 而非 EMA 网格，对照阅读能加深对「空区域跳过」这一通用加速思想的理解。
- **进入网格提取**（u6-l4 Marching Cubes）：`get_density_on_grid` 正是在一个 3D 网格上采样网络密度，与本章的密度网格思路相通，但目的是导出几何而非加速渲染。
- **深入融合内核**（u8-l2 JIT 融合）：`render_nerf.cuh`/`train_nerf.cuh` 内部复用本讲的 `if_unoccupied_advance_to_next_occupied_voxel` 与 `density_grid_occupied_at`，理解 JIT 融合后这些查询如何在寄存器内端到端完成。
- 若想验证参数对渲染质量/速度的影响，可结合 u7-l1（pyngp）写脚本，程序化调整 `density_grid_decay` 并批量截图对比。
