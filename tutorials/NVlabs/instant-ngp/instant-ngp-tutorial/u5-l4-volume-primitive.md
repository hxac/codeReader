# 体素原语与 NanoVDB

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 instant-ngp 的「体素原语（Volume）」与 NeRF 的根本区别：真值密度来自一个真实的稀疏体素网格（NanoVDB），而不是一组照片。
- 读懂 `.nvdb` 文件从磁盘到 GPU 的完整加载过程，包括包围盒归一化、`world2index` 坐标映射、`bitgrid` 空区域位图、以及 `global_majorant` 的由来。
- 理解参与介质的「delta tracking（空碰撞跟踪）」光线步进原理，以及它如何用一张位图跳过空白体素。
- 掌握 `albedo`、`scattering`、`inv_distance_scale` 三个散射渲染参数各自的物理含义与代码作用点。
- 看懂 MLP 在 Volume 模式下「学什么、渲染时怎么用」：训练时从 NanoVDB 蒙特卡洛生成监督样本，渲染时用前向 alpha 合成逼近体积外观。

## 2. 前置知识

### 2.1 什么是参与介质（participating media）

烟、云、雾、毛玻璃这类物体不像硬表面那样有明确的边界，光在其中穿行时会被**吸收（absorb）**和**散射（scatter）**。描述它的核心量是**消光系数（extinction coefficient）**σ：它越大，光越容易被挡住。沿一条光线累加 σ 就得到光学厚度，厚度越大透过率越低。这正是 NeRF 体渲染背后的同一套数学（见 [u4-l3](u4-l3-nerf-ray-march.md)），区别只在密度从哪儿来。

### 2.2 什么是 NanoVDB

[NanoVDB](https://www.openvdb.org/documentation/doxygen/NanoVDBOverview.html) 是 OpenVDB 的轻量、GPU 友好分支，用**稀疏层级结构**存储体素网格——只有真正有数据的区域才占显存，所以能装下整朵云。它的文件格式是 `.nvdb`。本仓库没有自己实现 NanoVDB，而是把 `dependencies/` 里的 nanovdb 头文件当库用，自己只负责解析文件头并把整块网格字节拷到显存。

### 2.3 Delta tracking（空碰撞跟踪）

当我们想在空间中「随机走」一根穿过介质的光线时，直接按固定步长采样很浪费——大部分空气是空的。Delta tracking（Woodcock tracking）的思路是：取一个**全局上界** `majorant`（≥ 介质中任意点的真实 σ），用它采样「自由飞行距离」；走到碰撞点后再掷骰子决定这次碰撞是「真碰撞」还是「假（null）碰撞」。真碰撞里再细分是散射还是吸收。这样无论介质多稀疏都能正确采样，且天然支持跳过空区域。本讲的 `global_majorant` 正是这个上界。

> 与 NeRF 的对比直觉：NeRF 沿光线按「圆锥步距」确定性采样一堆点，用网络给每个点估密度；Volume 则按物理自由飞行距离随机蹦跳，密度真值来自 NanoVDB，网络只是事后学着模仿这种外观。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/testbed_volume.cu` | Volume 模式的全部实现：加载、训练、渲染内核。本讲主战场。 |
| `include/neural-graphics-primitives/testbed.h` | `m_volume`（含 `VolPayload`）状态结构体定义；`training_prep_volume` 空实现。 |
| `configs/volume/base.json` | Volume 模式的默认网络配置（HashGrid + FullyFusedMLP）。 |
| `src/common_host.cu` | `mode_from_scene`：`.nvdb` 后缀→`ETestbedMode::Volume` 的判定。 |
| `src/testbed.cu` | 在 `load_training_data` 里分发到 `load_volume`；GUI 暴露三个散射参数。 |

## 4. 核心概念与源码讲解

### 4.1 NanoVDB 加载：从 .nvdb 到 GPU

#### 4.1.1 概念说明

加载阶段要回答四个问题：

1. 这真的是个 NanoVDB 文件吗？（魔数校验）
2. 网格数据多大、有没有被压缩？（决定能不能直接拷贝）
3. 网格在「体素索引空间」里占多大范围？如何把它居中放进 instant-ngp 统一的 `[0,1]³` 归一化空间？（几何归一化）
4. 哪些体素是「非空」的？最大密度是多少？（供光线步进与 delta tracking 使用）

这四个答案分别对应文件头校验、字节拷贝、`world2index` 映射、`bitgrid` 与 `global_majorant`。

#### 4.1.2 核心流程

```
load_volume(data_path)
 ├─ 读 NanoVDBFileHeader (16B)：magic / version / gridCount / codec
 ├─ 读 NanoVDBMetaData (176B)：gridSize / voxelCount / indexBBox / voxelSize ...
 ├─ 校验：magic=="NanoVDB0"、gridCount>0、codec==0（不支持压缩）
 ├─ 读 grid 名（nameSize 字节）
 ├─ 把 gridSize 字节原始网格数据拷到 m_volume.nanovdb_grid（GPU）
 ├─ 用 indexBBox 计算 aabb，把网格最长边归一化到 [0,1]，居中
 ├─ 算 world2index_scale / world2index_offset（世界↔索引坐标互转）
 ├─ 遍历所有体素：density>0.001 的，按 Morton3D 置 bitgrid 对应比特
 └─ 记 global_majorant = 最大密度
```

#### 4.1.3 源码精读

入口是 `Testbed::load_volume`，它先按 NanoVDB 的二进制布局逐字段读取。文件头魔数定义为 `"NanoVDB0"` 的小端 64 位整数：

[src/testbed_volume.cu:584-607](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L584-L607) —— `NanoVDBFileHeader` 与 `NanoVDBMetaData` 两个 POD 结构体，配两条 `static_assert` 保证内存布局与官方一致（header 16B、metadata 176B）。

读取后做三道硬校验，任一失败即抛异常：

[src/testbed_volume.cu:620-631](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L620-L631) —— 校验魔数、至少一个网格、以及 `codec == 0`（**不支持压缩 nvdb**，源码注释明说 `cannot use compressed nvdb files`）。这意味着实践中要用 `nanovdb` 工具把网格转成未压缩格式。

接着把整块网格字节当不透明缓冲拷进显存，并用 `indexBBox`（体素索引空间的包围盒）做几何归一化：

[src/testbed_volume.cu:642-668](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L642-L668) —— 关键四步：①`cpugrid` 读 gridSize 字节并 `copy_from_host` 到 `m_volume.nanovdb_grid`；②取三轴最大跨度 `maxsize`，令 `scale = 1/maxsize`，把 aabb 居中到 `[0,1]³`；③`world2index_scale = maxsize`（世界坐标乘它回到体素索引尺度）；④`world2index_offset` 把世界中心对齐到体素索引中心。

> **坐标映射的直觉**：渲染时网络与 aabb 都在归一化的 `[0,1]³` 世界里工作；但查 NanoVDB 密度要用体素整数索引。二者靠 `nanovdbpos = pos * world2index_scale + world2index_offset` 这一个式子互转（见 4.2.3）。它和 NeRF 里 `nerf_position_to_ngp` 的「先 scale+offset」是同一类坐标工序。

最后遍历全部体素，构建 `bitgrid`（一张 128³ 的 1-bit 占用图）并记录最大密度：

[src/testbed_volume.cu:670-700](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L670-L700) —— 对每个体素查 `acc.getValue`，密度 `> 0.001` 视为「占用」，把它的世界坐标量化到 128³ 后用 `morton3D` 算位索引，置 `bitgrid[bitidx/8]` 的对应比特；同时跟踪 `mn/mx`，最终 `m_volume.global_majorant = mx`。

`m_volume` 自身的字段定义在 `testbed.h`：

[include/neural-graphics-primitives/testbed.h:979-999](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L979-L999) —— `Volume` 结构体集中了所有状态：物理参数（`albedo/scattering/inv_distance_scale`）、数据来源（`nanovdb_grid/bitgrid`）、坐标映射（`world2index_*`）、delta tracking 上界（`global_majorant`）、训练样本缓冲（`training.positions/targets`）、以及双缓冲光线追踪态（`pos[2]/payload[2]/hit_counter/radiance_and_density`）。

路由到 `load_volume` 的判定在 `mode_from_scene`：

[src/common_host.cu:144-160](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/common_host.cu#L144-L160) —— `.nvdb` 后缀短路返回 `ETestbedMode::Volume`，再由 `load_training_data` 经 `case Volume: load_volume(path)` 调用（见 [src/testbed.cu:175](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L175)）。

#### 4.1.4 代码实践

1. **目标**：亲手验证 NanoVDB 的文件头布局与 `global_majorant` 的取值来源。
2. **步骤**：
   - 在仓库根目录找示例数据 `data/volume/cloud.nvdb`（官方四示例之一，对应 Volume 模式）。
   - 用任意十六进制查看器看文件前 16 字节，确认前 8 字节是 ASCII `NanoVDB0`。
   - 运行 `./instant-ngp data/volume/cloud.nvdb`，观察启动日志里 `tlog::info` 打印的 `gridSize / voxelCount / indexBBox / nanovdb extrema: mn mx`。
3. **观察**：日志会输出 `nanovdb extrema: <mn> <mx>`。
4. **预期**：其中 `mx`（最大密度）正是随后用于 delta tracking 的 `global_majorant`（见 [src/testbed_volume.cu:697-699](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L697-L699)）。
5. 若手头没有 `cloud.nvdb` 或运行环境，则把第 4.1.3 节两处源码行号对一遍即可，相关数值标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `load_volume` 看到 `metadata.codec != 0` 就直接抛异常，而不是去解压？
**答案**：本仓库把网格当不透明字节块整体拷进显存、再交给 nanovdb 的 GPU 访问器直接读，没有实现任何解压逻辑（注释 `cannot use compressed nvdb files`）。所以必须用未压缩（codec==0）的 nvdb。

**练习 2**：`world2index_scale` 为什么被设成 `maxsize`（三轴最大跨度）而不是各轴独立的尺寸？
**答案**：因为 aabb 归一化时用的是统一的 `scale = 1/maxsize`（保持各轴等比缩放，不产生形变）。世界坐标乘 `world2index_scale = maxsize` 正好逆运算回体素索引尺度，再加 `world2index_offset` 对齐中心。

---

### 4.2 体积光线步进：delta tracking 与空区域跳过

#### 4.2.1 概念说明

Volume 模式的光线步进用的是 delta tracking（空碰撞跟踪）。它有两个关键设计：

- **自由飞行距离**：用全局上界 `global_majorant` 采样光线走到下一次「碰撞」的距离，公式为

  \[ \Delta t = -\ln(1-\zeta_1)\cdot\frac{\text{distance\_scale}}{\text{global\_majorant}} \]

  其中 ζ₁ 是均匀随机数。`distance_scale/global_majorant` 共同决定平均自由程。
- **空区域跳过**：每次算出的碰撞点若落在密度为 0 的空气里（`bitgrid` 该比特为 0），就继续往前蹦，直到踩进「占用」体素为止——这是性能关键，避免对空空气反复采样。

#### 4.2.2 核心流程

```
walk_to_next_event(rng, aabb, pos, dir, bitgrid, scale):
  loop:
    ζ1 = random()
    Δt = -ln(1-ζ1) * scale          # 采样自由飞行距离
    pos += dir * Δt
    if pos 不在 aabb 内: return false   # 光线逃出体积
    bitidx = morton3D(量化 pos 到 128³)
    if bitgrid[bitidx] 标记占用: return true   # 找到一个真实候选点
    # 否则：在空气里，继续 loop 再蹦一步
```

撞到占用体素后，查询该点 NanoVDB 密度 `d`，按 delta tracking 掷骰子：

\[
p_{\text{ext}} = \frac{d}{\text{global\_majorant}},\quad
p_{\text{scat}} = p_{\text{ext}}\cdot\text{albedo}
\]

- ζ₂ ≥ p_ext：**null 碰撞**，假装没碰到，继续往前；
- ζ₂ < p_scat：**散射**，方向变成 `dir*scattering + 随机方向` 后归一化；
- 否则：**吸收**，光线终止。

#### 4.2.3 源码精读

`walk_to_next_event` 是整个 Volume 光线步进的基石，训练与渲染都复用它：

[src/testbed_volume.cu:70-88](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L70-L88) —— 注意它把 `pos` 量化到 128³ 网格、用 `morton3D` 取位、查 `bitgrid`。注释也坦白了局限：`for spatially varying majorant, we must check dt against the range...`——即当前实现假设 majorant 全局常数，未做空间自适应。

delta tracking 的「掷骰子」逻辑出现在训练数据生成内核里（注释 `ye olde delta tracker`）：

[src/testbed_volume.cu:131-158](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L131-L158) —— 在最多 128 次迭代里：先 `walk_to_next_event` 跳过空气；命中后用 `acc.getValue(pos*world2index_scale + world2index_offset + 抖动)` 查 NanoVDB 真值密度；再算 `extinction_prob = density/global_majorant`、`scatter_prob = extinction_prob*albedo`，按 ζ₂ 分流到 null/散射/吸收。

#### 4.2.4 代码实践

1. **目标**：理解 `global_majorant` 在步进中的双重作用。
2. **步骤**：打开 [src/testbed_volume.cu:70-88](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L70-L88) 与 [src/testbed_volume.cu:131-158](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L131-L158)，把所有用到 `global_majorant` 的地方画出来。
3. **观察**：它出现在两处——①作为自由飞行距离的分母（`scale = distance_scale/global_majorant`，步长反比于它）；②作为密度归一化的分母（`extinction_prob = density/global_majorant`，决定真碰撞概率）。
4. **预期**：你会看到 `global_majorant` 越大，每步步长越短（更密→蹦得近）、且单点真碰撞概率 `density/global_majorant` 越低（更多 null 碰撞）。这正是 delta tracking 自洽的体现。
5. 结论性的运行观察「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果 `global_majorant` 设得比真实最大密度还小，delta tracking 会出什么问题？
**答案**：`extinction_prob = density/global_majorant` 会大于 1，物理上不再是无偏偏采样——概率被截断、消光被低估，渲染会偏亮/偏透。这就是为什么源码严格取 `global_majorant = mx`（网格真实最大值）。

**练习 2**：`bitgrid` 在步进里扮演什么角色？如果删掉它（让 `walk_to_next_event` 不查位图直接返回）会怎样？
**答案**：它是空区域跳过的加速结构，让光线在空气里「大步蹦」而不是逐体素检查。删掉后光线会在每个自由飞行点都去查 NanoVDB 密度（多为 0），大量浪费访存与计算——与 NeRF 用 `density_grid_bitfield` 跳空是完全相同的设计哲学（见 [u4-l5](u4-l5-density-grid.md)）。

---

### 4.3 散射渲染参数：albedo / scattering / distance_scale

#### 4.3.1 概念说明

Volume 模式把参与介质的物理参数直接做成可调滑条，三个参数各有物理含义：

| 参数 | 字段 | 物理含义 | 代码作用 |
|------|------|----------|----------|
| Albedo | `m_volume.albedo` | 单次散射反照率：散射占消光的比例。越大介质越「亮」。 | `scatter_prob = extinction_prob * albedo` |
| Scattering | `m_volume.scattering` | 相位函数各向异性 g。>0 前向、<0 后向、≈0 各向同性。 | `dir = normalize(dir*scattering + random_dir)` |
| Distance scale | `m_volume.inv_distance_scale` | 平均自由程的倒数（密度全局倍率）。越大越「密」。 | `distance_scale = 1/inv_distance_scale`，进 `scale` |

#### 4.3.2 核心流程

每帧渲染前由 `render_volume` 把用户调的参数算成内核要用的派生量：

```
distance_scale = 1 / max(inv_distance_scale, 0.01)   # 防 0
scale = distance_scale / global_majorant             # 进 delta tracking
```

GUI 改这三个滑条会触发 `accum_reset`（重新累积采样），因为它们改变了介质的物理外观。

#### 4.3.3 源码精读

GUI 暴露三个滑条（注意 `inv_distance_scale` 用对数刻度，因为它的有效范围跨好几个数量级）：

[src/testbed.cu:1267-1273](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1267-L1273) —— `Albedo`(0~1)、`Scattering`(-2~2)、`Distance scale`(`inv_distance_scale` 1~100，对数滑条)。三者任一改变都置 `accum_reset`。

派生量在 `render_volume` 与 `train_volume` 入口都各算一次：

[src/testbed_volume.cu:451-453](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L451-L453) —— `distance_scale = 1.f / std::max(m_volume.inv_distance_scale, 0.01f)`，再传进 `init_rays_volume` 作为 `scale`（见 [src/testbed_volume.cu:288](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L288)）。训练侧同样的式子在 [src/testbed_volume.cu:184](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L184)。

三个参数如何渗进 delta tracking，可对照 [src/testbed_volume.cu:146-153](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L146-L153)：`albedo` 决定散射概率、`scattering` 决定散射后的新方向、`inv_distance_scale`（经 `scale`）决定自由飞行距离。

#### 4.3.4 代码实践

1. **目标**：用 GUI 滑条直观感受三个物理参数对云外观的影响。
2. **步骤**：加载 `data/volume/cloud.nvdb`，等画面稳定后打开 `Volume training options` 面板（对应 [src/testbed.cu:1267-1273](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1267-L1273) 这段）。
3. **操作与观察**：
   - 把 **Albedo** 从 0.95 降到 0.2：云应该明显变暗（吸收增加、散射减少）。
   - 把 **Scattering** 拉到 +2：云呈现强前向散射（迎光面亮、背光面暗的对比加剧）。
   - 把 **Distance scale** 调大（`inv_distance_scale`↑）：云变密、更不透明。
4. **预期**：每次改动后画面会重新累积（`accum_reset`），说明这些参数直接进了物理采样。
5. 无 GUI/无数据时，按 4.3.1 的表格与源码行号对照理解即可，数值「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 GUI 里 `Distance scale` 滑条绑的是 `inv_distance_scale`（倒数）而不是 `distance_scale` 本身？
**答案**：用户心智里「滑条往右=云更密」更直觉，而「更密」对应平均自由程更小，即 `distance_scale` 更小、`inv_distance_scale` 更大。绑倒数正好让滑条方向符合直觉，再叠对数刻度适应大范围。

**练习 2**：`Scattering` 滑条范围是 -2~2，但相位函数各向异性 g 物理上通常在 [-1,1]。代码里这个值怎么用？
**答案**：它并不是直接当 Henyey-Greenstein 的 g 参数用，而是作为线性混合系数 `dir = normalize(dir*scattering + random_dir)`——当 `scattering=0` 时方向完全随机（各向同性），`>0` 偏向原方向（前向），`<0` 因 `dir*负值` 偏向反方向。范围放宽到 ±2 是为了允许更强的方向保持。

---

### 4.4 训练与渲染：MLP 如何逼近体积外观

#### 4.4.1 概念说明

到这里你可能会问：既然 NanoVDB 已经存了真值密度，为什么还要训一个 MLP？答案是——**MLP 学的不是密度本身，而是「体积的散射外观」**。

- NanoVDB 只给静态的密度场 σ(x)，它不含光照、不含散射方向。
- 训练时，程序用 delta tracking 在 NanoVDB 里蒙卡模拟光线传播（散射/吸收），在每个真实碰撞点记录「这里的入射辐照颜色 + 这里的密度」作为监督目标。
- 渲染时用两条路：
  - **真值路**（`m_render_ground_truth`）：直接拿 NanoVDB 跑 delta tracking，不经过 MLP。
  - **MLP 路**：用网络在每个采样点预测 `(RGB, density)`，再做前向 alpha 合成。

这恰好是本讲练习要对比的关键：**NeRF 的密度来自网络、从照片学；Volume 的密度真值来自 NanoVDB，网络只是学着复现介质外观。**

#### 4.4.2 核心流程

```
train_volume(target_batch_size):
  ├─ distance_scale = 1/inv_distance_scale
  ├─ 启动 volume_generate_training_data_kernel：
  │     每根光线蒙卡走 NanoVDB，记前 4 个真实碰撞点的 (pos, density, 目标色)
  │     目标色 = proc_envmap(出射方向) * throughput
  ├─ 把 positions/targets 拼成 GPUMatrix
  └─ m_trainer->training_step(...)  # 网络学 (pos → RGB+density)

render_volume(...):
  ├─ init_rays_volume：每像素一根光线，蹦到第一个占用点
  ├─ if m_render_ground_truth: volume_render_kernel_gt   # 纯 NanoVDB delta tracking
  └─ else: 循环最多 64 次：
        m_network->inference(pos → RGB+density)
        volume_render_kernel_step：alpha 合成 + 蹦下一步或写像素
```

注意 alpha 合成的形式（与 NeRF 的体渲染方程同构）：

\[
\alpha_i = p_{\text{ext},i}\cdot T_i,\quad T_i = 1 - \text{col}.a
\]
\[
\text{col}.\text{rgb} \mathrel{+}= \text{RGB}_i\cdot\alpha_i,\qquad \text{col}.a \mathrel{+}= \alpha_i
\]

#### 4.4.3 源码精读

先看网络形状——它和 NeRF 一样是 **3 进 4 出（RGB+密度）**：

[src/testbed_volume.cu:36-42](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L36-L42) —— `network_dims_volume` 返回 `n_input=3, n_output=4`。这是 Volume 能复用 NeRF 体渲染式子的前提。

训练数据生成内核是「蒙卡教师」：

[src/testbed_volume.cu:93-169](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L93-L169) —— 每个线程负责若干光线，蒙卡走 NanoVDB，把前 `MAX_TRAIN_VERTICES=4` 个真实碰撞点的位置与密度记进 `outpos/outdensity`，最终目标色取自 `proc_envmap(dir,...)*throughput`（[src/testbed_volume.cu:159-167](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L159-L167)）——即「从这里朝出射方向看到的天空色」，第 4 维写密度。

`train_volume` 把这些样本喂给 `m_trainer`：

[src/testbed_volume.cu:171-220](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L171-L220) —— 启动生成内核、把 `positions/targets` 拼成 `GPUMatrix`、调 `m_trainer->training_step`。注意 Volume 的 `training_prep_volume` 是空实现（[include/neural-graphics-primitives/testbed.h:327](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L327)）——因为它的样本在 `train_volume` 内部现生成，不像 NeRF 需要预建密度网格/误差图。

渲染分两条路。真值路（`m_render_ground_truth`）完全绕开网络：

[src/testbed_volume.cu:301-374](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L301-L374) —— `volume_render_kernel_gt` 每个活像素跑 128 次 delta tracking，散射则查 `proc_envmap`、吸收则输出黑、逃逸则查背景，直接写 `frame_buffer`。

MLP 路是迭代式 alpha 合成：

[src/testbed_volume.cu:376-438](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L376-L438) —— `volume_render_kernel_step` 取网络输出 `local_output`（RGB 在 xyz、密度在 w），算 `extinction_prob = density/global_majorant`（>1 则截断）、`alpha = extinction_prob*T`，做前向合成；当累积不透明度 `>0.99`、光线逃出 aabb、或到达最大迭代时，补上背景透射并写像素，否则把光线推进到下一缓冲继续。

调度这两条路的主函数：

[src/testbed_volume.cu:440-582](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L440-L582) —— `render_volume` 先 `init_rays_volume` 发光线（[src/testbed_volume.cu:467-495](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L467-L495)），同步拿到活光线数 `n`；`m_render_ground_truth` 为真走 `volume_render_kernel_gt`，否则进 MLP 迭代循环（[src/testbed_volume.cu:529-580](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L529-L580)）。该循环用 `pos[2]/payload[2]` 双缓冲在迭代间搬运活光线，每 4 次迭代把剩余光线数同步回 CPU 决定是否提前结束。

默认网络配置（与 SDF/Image 同构的单头 `NetworkWithInputEncoding`，而非 NeRF 的双头）：

[configs/volume/base.json:23-36](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/volume/base.json#L23-L36) —— `HashGrid`(16 层 ×2 特征) 编码 + `FullyFusedMLP`(64 神经元 ×2 隐层)，输出激活 ReLU。

#### 4.4.4 代码实践（本讲核心对比任务）

1. **目标**：对比 NeRF 与 Volume 两种体积渲染的数据来源，并说清 `global_majorant` 的作用。
2. **步骤**：
   - 在 [include/neural-graphics-primitives/testbed.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h) 中定位 `m_nerf`（NeRF 状态，密度由 `NerfNetwork` 从照片学）与 `m_volume`（[testbed.h:979-999](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L979-L999)，密度真值来自 `nanovdb_grid`）。
   - 在 [src/testbed_volume.cu:288](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L288) 与 [src/testbed_volume.cu:349](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L349) 找到 `global_majorant` 的两处使用。
3. **要回答的问题**：
   - `m_nerf` 与 `m_volume` 各自的「密度数据来源」是什么？→ NeRF 是网络（从照片监督），Volume 是 NanoVDB 稀疏网格（真值），MLP 只是学着复现外观。
   - `global_majorant` 在体积步进中起什么作用？→ 它是 delta tracking 的全局消光上界，既决定自由飞行步长（`scale=distance_scale/global_majorant`，反比），又决定单点真碰撞概率（`density/global_majorant`）。
4. **预期结果**：你能写出「NeRF = 照片→网络密度；Volume = NanoVDB 真值密度 + 网络学外观」这条对照，并解释为何 `global_majorant` 必须取网格真实最大密度。
5. 运行层面（加载 cloud.nvdb 并切换 `m_render_ground_truth`）的视觉对比「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：Volume 模式的 `training_prep_volume` 为什么是空函数？
**答案**：因为它的训练样本是每步在 `train_volume` 内部用蒙卡 delta tracking 现生成（直接读 NanoVDB），不需要像 NeRF 那样预建密度网格、误差图或按批采样光线，所以没有「训练准备」阶段（见 [testbed.h:327](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L327)）。

**练习 2**：MLP 渲染路（`volume_render_kernel_step`）里的 alpha 合成，与 NeRF 的体渲染方程有什么相同和不同？
**答案**：相同——都是前向 alpha 合成 `col.rgb += RGB_i·α_i`、`α_i = extinction_i·T_i`、`T_i = 1-col.a`，且都设了不透明度阈值（这里 `>0.99`，NeRF 用 `render_min_transmittance`）提前终止。不同——NeRF 沿圆锥步距确定性采样、密度来自网络；Volume 沿蒙卡自由飞行距离随机蹦跳、采样位置由 delta tracking 决定，密度来自网络但物理参数（albedo/scattering）直接进合成。

**练习 3**：默认配置下 Volume 用的是哪种网络包装器？和 NeRF 一样吗？
**答案**：不一样。Volume 走单头 `NetworkWithInputEncoding`（一套 HashGrid 编码 + 一套 MLP，3→4），见 [configs/volume/base.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/volume/base.json)；NeRF 走双头 `NerfNetwork`（位置头 + 方向头，见 [u4-l2](u4-l2-nerf-network-architecture.md)）。

## 5. 综合实践

**任务：完整走通「加载→训练→对比真值与 MLP 渲染」全流程，并量化 `global_majorant` 的影响。**

1. **加载与初始化**：`./instant-ngp data/volume/cloud.nvdb`。在启动日志里记下 `nanovdb extrema` 的 `mx`，确认它等于运行时 `m_volume.global_majorant`（[src/testbed_volume.cu:699](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L699)）。
2. **观察真值渲染**：开启训练前（或按渲染模式键），确认 `m_render_ground_truth` 路径——这时画面是纯 NanoVDB delta tracking（`volume_render_kernel_gt`），不依赖网络。
3. **训练 MLP**：按 `T` 开训练，观察 loss 下降。MLP 正在学着从 NanoVDB 蒙卡生成的 `(pos→RGB+density)` 样本里复现体积外观（`train_volume` + `volume_generate_training_data_kernel`）。
4. **对比两条渲染路**：训练若干秒后，切换 `m_render_ground_truth`（对应 [src/testbed.cu:2326](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2326) 的快捷键），对比 MLP 合成结果与 NanoVDB 真值结果。
5. **量化 `global_majorant`**：在源码里把 `m_volume.global_majorant` 人为改小（**仅为阅读实验，不提交**），重新跑——预期画面会偏亮偏透（见 4.2.5 练习 1），从而验证它是 delta tracking 的物理上界。
6. **若无法本地运行**：用 4.4.4 的源码对照法完成「NeRF vs Volume 数据来源对比」并写一段说明，视觉部分标注「待本地验证」。

> 本任务串起了本讲全部最小模块：NanoVDB 加载（4.1）→ delta tracking 步进（4.2）→ 三参数调节（4.3）→ 训练/渲染两条路（4.4）。

## 6. 本讲小结

- Volume 原语与 NeRF 都是体渲染，但 **真值密度来自 NanoVDB 稀疏体素网格**，而非照片；MLP 学的是「散射外观」`(pos→RGB+密度)`，不是密度本身。
- `load_volume` 做四件事：校验未压缩 nvdb 头 → 拷网格到显存 → 把网格归一化居中进 `[0,1]³` 并算 `world2index` 映射 → 建 `bitgrid` 占用位图并取 `global_majorant=最大密度`。
- 光线步进用 **delta tracking（空碰撞跟踪）**：自由飞行距离 `Δt=-ln(1-ζ)·distance_scale/global_majorant`，再用 `density/global_majorant` 与 `albedo` 决定 null/散射/吸收。
- `bitgrid`（128³ Morton 位图）让光线在空气里大步跳过，等价于 NeRF 的 `density_grid_bitfield` 空区域跳过。
- 三个散射参数：`albedo`（散射占比）、`scattering`（相位各向异性/方向混合）、`inv_distance_scale`（密度倍率的倒数），GUI 对数滑条直接驱动物理采样。
- 渲染分两条路：`m_render_ground_truth` 走纯 NanoVDB delta tracking；否则走 MLP 迭代 alpha 合成（`volume_render_kernel_step`），与 NeRF 体渲染方程同构。

## 7. 下一步学习建议

- **回到 NeRF 体积渲染**：把本讲的 delta tracking 步进与 [u4-l3](u4-l3-nerf-ray-march.md) 的圆锥步距 + alpha 合成对照阅读，理解「确定性采样 vs 蒙卡采样」两种体渲染策略。
- **密度网格对照**：本讲的 `bitgrid` 与 [u4-l5](u4-l5-density-grid.md) 的 `density_grid_bitfield` 是同一种「位图跳空」思想，可对比二者如何构建与查询。
- **渲染缓冲与产物**：Volume 的 `frame_buffer` 写入后如何上屏，见 [u6-l1 渲染缓冲区与 CUDA-GL 互操作](u6-l1-render-buffer.md)。
- **想扩展介质模型**：若要改散射/吸收的物理模型或相位函数，入口都在 `src/testbed_volume.cu` 的 `walk_to_next_event` 与三个 `*_kernel`；想换编码/网络则改 `configs/volume/base.json`（参见 [u8-l5 扩展 instant-ngp](u8-l5-extending-instant-ngp.md)）。
