# 天线电磁仿真

## 1. 本讲目标

AERIS-10 雷达能看多远、能分辨多准，最终都落在那块天线阵列上。本讲带你读懂仓库里两套互补的天线仿真资产——基于 openEMS 的全波 FDTD 仿真（算单个缝隙波导/贴片单元的 S 参数与增益），以及基于 Matlab Antenna Toolbox 与解析阵列因子的方向图综合（算 16×32 阵列怎么扫描、副瓣多高）。

学完后你应该能够：

- 说清楚「为什么要仿真天线」以及 FDTD（时域有限差分）的基本直觉。
- 读懂 `openems_quartz_slotted_wg_10p5GHz.py` 的 `sanity / balanced / full` 三档配置，并解释网格分辨率、空气盒尺寸、频率点数如何左右「精度 ↔ 时间」的权衡。
- 看懂缝隙波导（slotted waveguide）与贴片阵列（patch array）两种辐射单元的几何与馈电方式。
- 解释阵列因子（Array Factor）与波束扫描、副瓣加权（Kaiser 锥削）的关系，把本讲和 u2-l2 里的相控阵公式串起来。

## 2. 前置知识

在进入源码前，先用三段大白话把电磁仿真的几个关键词讲清楚。

**S 参数（S-parameters）。** 把天线当成一个二端口网络，从端口 1 喂进去多少功率、反射回来多少（S11）、传到端口 2 多少（S21）。天线要辐射，就意味着能量「漏」出去了，所以一个好天线在中心频率上 S11 很低（比如 −10 dB 以下，意味着反射不到 1/10）。本讲的 openEMS 脚本一边扫频一边画 S11/S21，就是为了看天线是否在 10.5 GHz「调准了」。

**远场与增益（Far-field / Gain）。** 天线近处的电磁场很复杂，但离远了之后，方向图会稳定成一个只和角度（θ, φ）有关的形状。FDTD 仿真先把近场算出来，再用「近场→远场变换」（NF2FF）外推得到远场方向图。增益（dBi）就是这个方向图主瓣最高点相对于「理想全向辐射器」的倍数（取 10·log10）。

**FDTD（Finite-Difference Time-Domain）。** 把空间切成很多小立方体（网格/细胞，cell），每个细胞记住 6 个分量（Ex,Ey,Ez,Hx,Hy,Hz）；然后在时间上用麦克斯韦方程的差分形式一步步「推」下去——电场更新磁场、磁场更新电场，交替前进。它最大的好处是一次仿真就能得到整个频带的响应（因为激励是高斯脉冲，含所有频率）；代价是网格越细、空气盒越大，细胞数立方级增长，时间与内存暴涨。这正是本讲「三档配置」要解决的核心矛盾。

> 关于相控阵、波束扫描、相位差公式等概念，本讲承接 u2-l2《雷达信号处理流水线》中已建立的整体认知，不重复展开。

## 3. 本讲源码地图

| 文件 | 作用 | 语言/工具 |
| --- | --- | --- |
| [5_Simulations/Antenna/openems_quartz_slotted_wg_10p5GHz.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/Antenna/openems_quartz_slotted_wg_10p5GHz.py) | 石英填充缝隙波导的 openEMS 全波仿真，含 `sanity/balanced/full` 三档配置，输出 S 参数、阻抗与 3D 远场增益 | Python + openEMS/CSXCAD |
| [5_Simulations/Antenna/Quartz_Waveguide.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/Antenna/Quartz_Waveguide.py) | 同一缝隙波导的早期单档版本（固定 32 缝、`mesh_res=min(0.5, λ0/30)`），适合对照阅读「不含 profile 抽象」的原始写法 | Python + openEMS/CSXCAD |
| [5_Simulations/Matlab/Antenna16_8.m](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/Matlab/Antenna16_8.m) | 用 Antenna Toolbox 搭建 16×8 贴片阵列（RO4350B 基板），增量计算并保存方向图 | Matlab |
| [5_Simulations/array_pattern_Kaiser25dB_like.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/array_pattern_Kaiser25dB_like.py) | 32×16 阵列的解析阵列因子综合，用 Kaiser 锥削压副瓣，画 E/H 面与热图 | Python（纯解析，不依赖 openEMS） |
| [5_Simulations/Slotted_DielectricFilled_Waveguide.m](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/Slotted_DielectricFilled_Waveguide.m) | 氧化铝（alumina, εr=9.8）填充缝隙波导的几何设计脚本，给出缝间距、缝长等设计规则 | Matlab |
| [docs/AERIS_Antenna_Report.pdf](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/AERIS_Antenna_Report.pdf) | 正式天线设计报告（约 1.6 MB PDF），汇总仿真与实测结论 | 文档 |

补充说明：仓库 `5_Simulations/` 下还有 `slot_layout_taper32.csv`（32 缝的锥削权重表）、`Matlab/Antenna_array.m` 与 `antenna16_antenna8.m` 等同族脚本，以及若干已生成 PNG（`E_plane_cut.png`、`Heatmap_Kaiser25dB_like.png` 等）。本讲聚焦上面 6 个核心文件，其余作为延伸阅读。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**① openEMS FDTD 全波仿真**、**② 缝隙波导/贴片辐射单元**、**③ 阵列方向图综合与扫描**。三者构成一条「单元 → 阵列」的设计链：先仿真单个辐射单元确认它能在 10.5 GHz 正常辐射（模块①②），再用解析阵列因子快速评估上百个单元组成阵列后的方向图与扫描性能（模块③）。

### 4.1 openEMS FDTD 全波仿真：三档配置的艺术

#### 4.1.1 概念说明

openEMS 是一个开源的三维电磁 FDTD 仿真器。本讲的主角 `openems_quartz_slotted_wg_10p5GHz.py` 用它来仿真一段「石英填充的矩形缝隙波导」——也就是 AERIS-10X（Extended 版）天线阵列里的一根辐射列。

这个脚本最值得学习的设计，不是某条电磁公式，而是它把仿真拆成了 **`sanity / balanced / full` 三档配置**。FDTD 仿真的「精度 ↔ 时间」权衡非常残酷：网格减半，细胞数变 8 倍，时间也接近 8 倍。如果一上来就用最细网格跑 32 条缝，可能跑几个小时后才发现几何画错了。所以作者用三档配置让你「先用便宜的错误检测、再用贵的精确计算」：

- **sanity（健全检查）**：12 条缝、粗网格（0.8 mm），几分钟跑完，只用来确认「结构能辐射、S11 在 10.5 GHz 附近有凹陷」。
- **balanced（平衡）**：24 条缝、0.6 mm 网格，质量与时间的折中。
- **full（完整）**：32 条缝、0.5 mm 网格、最大空气盒、最多频率点，用于最终交付数据。

#### 4.1.2 核心流程

脚本的执行流程可以概括为七步：

1. **选档**：读取 `PROFILE`，从 `profiles` 字典取出该档的所有参数。
2. **算导波波长**：由 εr、波导宽壁 a 算 TE10 截止频率 fc10，再算导波波长 λg，进而定出缝间距 `λg/2`、缝长 `0.47λg`、边距 `0.25λg`。
3. **建网格**：把所有几何关键坐标（金属壁、缝边）显式塞进网格线集合，再做一次 `SmoothMeshLines` 平滑，限制最大网格尺寸。
4. **建几何与材料**：石英介质块 + PEC（理想导体）管壁 + 在顶壁上「挖」出空气缝（用优先级覆盖金属）。
5. **加端口与近远场盒子**：两端加矩形波导 TE10 端口（端口 1 激励、端口 2 接收），中间包一个 NF2FF 盒子用于近场→远场变换。
6. **跑 FDTD**：高斯脉冲激励，跑足时间步直到能量收敛到 `EndCriteria=1e-5`。
7. **后处理**：扫频算 S11/S21/输入阻抗；算 3D 远场方向图与最大实现增益 `Gmax = Dmax·(1−|S11|²)`。

FDTD 数值稳定性的核心是 **CFL 条件**：时间步长 Δt 必须满足

\[
\Delta t \le \frac{1}{c\sqrt{\frac{1}{\Delta x^2}+\frac{1}{\Delta y^2}+\frac{1}{\Delta z^2}}}
\]

也就是说网格越细，Δt 必须越小，同样的物理仿真时长就需要更多时间步——这就是「网格变细 ⇒ 时间暴涨」的物理根源（既是细胞数变多，又是步数变多）。脚本里 `SetTimeStepFactor(0.95)` 就是把 Δt 设在 CFL 上限的 95%，留一点安全裕度。

#### 4.1.3 源码精读

**三档配置的定义**是全脚本的中枢：

[5_Simulations/Antenna/openems_quartz_slotted_wg_10p5GHz.py:32-55](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/Antenna/openems_quartz_slotted_wg_10p5GHz.py#L32-L55) —— 这段定义了 `PROFILE` 开关与三档参数表（缝数 Nslots、网格分辨率 mesh_res、空气盒尺寸 air_x/y/z、远场角采样 n_theta/n_phi、频率点数 freq_pts、PML 层数 pml），最后 `cfg = profiles[PROFILE]` 取出当前档。注意三档的「旋钮」是同一组，区别只在数值大小。

**导波波长与缝尺寸**的计算：

[5_Simulations/Antenna/openems_quartz_slotted_wg_10p5GHz.py:73-82](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/Antenna/openems_quartz_slotted_wg_10p5GHz.py#L73-L82) —— 先算 TE10 截止频率 `fc10 = C0/(2·√εr·a)`，再算介质内波长 λd、导波波长 λg，最后缝间距 `slot_s=0.5λg`、缝长 `slot_L=0.47λg`、边距 `margin=0.25λg`。这是缝隙波导设计的经典规则（见模块 4.2）。

**FDTD 引擎与激励设置**：

[5_Simulations/Antenna/openems_quartz_slotted_wg_10p5GHz.py:87-99](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/Antenna/openems_quartz_slotted_wg_10p5GHz.py#L87-L99) —— `openEMS(NrTS=6e5, EndCriteria=1e-5)` 最多跑 60 万时间步、能量降到 1e-5 即停；`SetGaussExcite` 用覆盖 9.5–11.5 GHz 的高斯脉冲（一次激励得到整个频带）；`SetBoundaryCond([PML_n]*6)` 六面都加完美匹配层吸收边界；`SetTimeStepFactor(0.95)` 是 CFL 安全系数。

**复杂度与内存估算**——这是三档配置存在的根本理由：

[5_Simulations/Antenna/openems_quartz_slotted_wg_10p5GHz.py:134-143](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/Antenna/openems_quartz_slotted_wg_10p5GHz.py#L134-L143) —— 脚本主动打印细胞数 `Ncells = Nx·Ny·Nz` 与粗略内存 `mem_fields_bytes = Ncells·6·8`（每个细胞存 6 个场分量、每个 8 字节 double）。注释明说这是「to help stay inside 16 GB」——也就是说作者明确把内存预算当成硬约束，所以才要先跑 sanity 看一眼 `[mesh] cells` 行。

**缝「挖洞」覆盖金属的优先级技巧**：

[5_Simulations/Antenna/openems_quartz_slotted_wg_10p5GHz.py:164-169](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/Antenna/openems_quartz_slotted_wg_10p5GHz.py#L164-L169) —— 顶壁是 PEC 金属，每条缝用一个 AIR 材质的盒子去「盖」在顶壁上，并 `SetPriority(10)` 提高优先级，确保空气盒在网格里真的把金属「切」掉，形成辐射缝隙。

**实现增益的计算**：

[5_Simulations/Antenna/openems_quartz_slotted_wg_10p5GHz.py:276-280](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/Antenna/openems_quartz_slotted_wg_10p5GHz.py#L276-L280) —— `Gmax = Dmax·(1−|S11|²)`：最大方向性 Dmax（来自 NF2FF）乘以失配因子（反射越小、辐射出去的越多）。这正是「S11 调得好 ⇔ 增益高」在代码里的直接体现。

对照阅读：早期版本 `Quartz_Waveguide.py` 没有三档抽象，参数直接写死——

[5_Simulations/Antenna/Quartz_Waveguide.py:48-58](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/Antenna/Quartz_Waveguide.py#L48-L58) —— 固定 `Nslots=32`、`mesh_res=min(0.5, λ0/30)`（约 0.95 mm，但封顶 0.5 mm）。把它和三档版对照，就能看出「为什么需要 profile」：单档脚本要么太慢、要么太粗，没有回旋余地。

#### 4.1.4 代码实践

**实践目标：** 把 `PROFILE` 从 `sanity` 切到 `balanced`，定量解释每个参数的变化，并估算细胞数与运行时间的增长倍数。

**操作步骤：**

1. 打开 `5_Simulations/Antenna/openems_quartz_slotted_wg_10p5GHz.py`，把第 32 行改成 `PROFILE = "balanced"`（如果你没装 openEMS，只读不改也可以完成下面的分析部分）。
2. 对照第 38–54 行的两档参数表，逐项记录差异：

   | 参数 | sanity | balanced | 变化 |
   | --- | --- | --- | --- |
   | Nslots | 12 | 24 | ×2（缝多了一倍，波导更长） |
   | mesh_res | 0.8 mm | 0.6 mm | 每个线维度网格密度 ×(0.8/0.6)≈1.33 |
   | air_x | 6.0 | 8.0 | 空气盒变宽 |
   | air_y | 20.0 | 30.0 | 空气盒变高 |
   | air_z | 10.0 | 12.0 | 空气盒变深 |
   | freq_pts | 201 | 301 | 扫频点更多 |
   | pml | 6 | 8 | 吸收层更厚 |

3. **估算细胞数增长**（解析估算，非实测）：FDTD 细胞数大致正比于「仿真体积 ÷ 单个网格体积」。
   - 体积维度变化（用脚本第 108–115 行的几何公式估算波导长度 `guide_length = 2·margin + (Nslots−1)·slot_s`，其中 `slot_s = 0.5·λg`，石英 εr=3.8、a=13.28 mm 时 λg≈17.6 mm，故 slot_s≈8.8 mm）：
     - sanity 波导长约 105 mm，balanced 约 211 mm（z 方向 ×2）。
     - 加上空气盒后，x 方向约 ×1.5、y 方向约 ×1.9、z 方向约 ×2.0，总体积约 **×5.8**。
   - 网格细化：mesh_res 0.8→0.6，每个线维度密度 ×1.33，三维合计 **×2.37**。
   - 合计细胞数约 **×13.8**（5.8 × 2.37）。
4. **估算运行时间增长**：单步耗时正比于细胞数；又因 CFL 条件，最小网格变小会让 Δt 变小、步数变多（这里主要由固定的细几何特征如 0.6 mm 缝宽决定，mesh_res 本身影响较小）。保守估计时间增长 **≈ 一个数量级（×10–×15）**。频率点与远场角的增加还会额外加价后处理时间。

**需要观察的现象：** 运行后留意终端的 `[mesh] cells:` 输出（脚本第 134–137 行算的 `Ncells` 与内存估算）。sanity 应在几十万量级、内存几百 MB 以内；balanced 会明显上一个台阶。

**预期结果：** balanced 的 S11 曲线在 10.5 GHz 附近应比 sanity 的凹陷更深更尖（缝更多、分辨率更高 → 谐振更准）；3D 方向图主瓣更窄、增益数字更高。**精确的细胞数与运行时长取决于本机硬件与 openEMS 版本——待本地验证。**

> 即便不安装 openEMS，上面的参数对照表与解析估算也能完整完成，这是「源码阅读型实践」。

#### 4.1.5 小练习与答案

**练习 1：** 为什么脚本要在 sanity 阶段主动打印 `Ncells` 和 `mem_fields_bytes`，而不是直接跑 full？

**参考答案：** 因为 FDTD 的内存与时间随网格立方级增长，full 档可能直接撑爆 16 GB 内存或跑几小时。先在 sanity（12 缝、0.8 mm）跑一遍看 `[mesh] cells` 行，可以在几分钟内确认几何/网格是否合理、细胞数是否在预算内，避免在 full 档浪费几小时后才发现画错。

**练习 2：** `SetTimeStepFactor(0.95)` 设成 0.99 会怎样？设成 1.5 呢？

**参考答案：** 0.99 会让 Δt 更逼近 CFL 上限，仿真略快但数值稳定性裕度变小，边界/色散误差更大，可能发散；1.5 直接违反 CFL 条件，仿真会数值爆炸（场迅速发散到 inf），openEMS 通常会报错终止。所以 0.95 是「在稳与快之间留 5% 裕度」的常规取值。

---

### 4.2 缝隙波导与贴片阵列：两种辐射单元

#### 4.2.1 概念说明

AERIS-10 的两个变体用了两种不同的天线单元（README 明确列出）：

- **AERIS-10N（Nexus，3 km）**：8×16 **贴片阵列**（patch array）。每个贴片是一块小金属片，蚀刻在介质基板上，像微带天线一样辐射。
- **AERIS-10X（Extended，20 km）**：32×16 **介质填充缝隙波导**（dielectric-filled slotted waveguide）。一段矩形金属管，内部填介质，顶壁上开一排缝隙，波导里的能量从缝隙漏出去辐射。

为什么 Extended 版用缝隙波导？因为它能承受 GaN 功放送来的高功率（金属波导散热好、耐功率远优于贴片），且介质填充能让波导在 10.5 GHz 做得更小、更适合排成 32 列的紧凑阵列。仓库里两种单元都有仿真脚本：贴片阵列在 `Matlab/Antenna16_8.m`，缝隙波导在 openEMS 脚本与 `Slotted_DielectricFilled_Waveguide.m`。

> 关于「为什么 10.5 GHz」「波束扫描 ±45°」的系统级定位，见 u1-l1 与 u2-l2。

#### 4.2.2 核心流程

**缝隙波导设计规则**（来自 `Slotted_DielectricFilled_Waveguide.m` 与 openEMS 脚本共用的公式）：

矩形波导 TE10 模的截止频率为

\[
f_{c10} = \frac{c}{2a\sqrt{\varepsilon_r}}
\]

其中 a 是波导宽壁（内部尺寸），εr 是填充介质的相对介电常数。介质中的导波波长为

\[
\lambda_g = \frac{\lambda_0/\sqrt{\varepsilon_r}}{\sqrt{1 - (f_{c10}/f_0)^2}}
\]

缝隙波导的关键设计规则全部从 λg 推出来：

- **缝间距 = λg/2**：相邻缝相位差 180°，配合缝在中心线两侧交替偏置（offset ±），让辐射同相叠加（这是「谐振阵」的标准做法）。
- **缝长 ≈ 0.47·λg**：每个缝近似一个半波谐振缝隙。
- **边距 ≈ 0.25·λg**：首尾缝到波导端面的距离。

> 注意仓库里出现了两种介质：openEMS 脚本用 **石英 εr=3.8**（宽壁 a=13.28 mm），而 `Slotted_DielectricFilled_Waveguide.m` 用 **氧化铝 alumina εr=9.8**（a=8.5 mm）。εr 越大，波导可以做越窄，但损耗与加工难度也变高。这两份脚本代表对填充介质的两种选型考量，最终选哪个以硬件设计文件与天线报告为准。

**贴片阵列设计规则**：贴片尺寸由基板介电常数决定，谐振长度约 `λ/(2·√εr)`；阵列里相邻贴片间距通常取自由空间半波长 `λ0/2`，以防栅瓣（grating lobes）。

#### 4.2.3 源码精读

**openEMS 里的缝隙几何**（石英版，承接 4.1）：

[5_Simulations/Antenna/openems_quartz_slotted_wg_10p5GHz.py:117-123](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/Antenna/openems_quartz_slotted_wg_10p5GHz.py#L117-L123) —— 缝中心 z 坐标等间距排布（`margin + i·slot_s`），x 坐标在中心线两侧交替偏置 ±0.90 mm（`+delta0 if i%2==0 else -delta0`）。正是这个「交替偏置」让 λg/2 间距的缝能同相辐射。

[5_Simulations/Antenna/openems_quartz_slotted_wg_10p5GHz.py:153-162](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/Antenna/openems_quartz_slotted_wg_10p5GHz.py#L153-L162) —— 石英介质块填满波导内部 `[0,0,0]–[a,b,L]`，PEC 管壁四面合围（左/右/底/顶），顶壁后续被缝「挖洞」。

**氧化铝版的设计规则**（Matlab）：

[5_Simulations/Slotted_DielectricFilled_Waveguide.m:14-33](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/Slotted_DielectricFilled_Waveguide.m#L14-L33) —— 这段把上面三条设计规则原原本本写成 Matlab：`fc10 = c0/(2·a·√εr)`、`lamg = lam0/√(1−(fc10/f0)²)`、`slotSpacing=lamg/2`、`slotLen=0.47·lamg`、`slotWid=0.02·lam0`。注释里直接给出氧化铝（εr=9.8、a=8.5 mm）下的数值：λg≈33.83 mm、缝间距≈16.915 mm、缝长≈15.9 mm。这是 openEMS 脚本之外、用 Antenna Toolbox `waveguideSlotted` 对象搭几何的另一种实现路径。

**贴片阵列的几何搭建**（16×8）：

[5_Simulations/Matlab/Antenna16_8.m:1-29](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/Matlab/Antenna16_8.m#L1-L29) —— 贴片长 `Length_patch=8.76 mm`、宽 `Width_patch=9.545 mm`、间距 `dist_patch=λ0/2`（10.5 GHz 下约 14.27 mm），基板 RO4350B（εr=3.48、厚 0.578 mm）。第 24–29 行用双重循环把 16×8 个贴片 + 馈电线累加成一个 `AntennaPlane`，体现「阵列 = 单元 × 规则排布」。

[5_Simulations/Matlab/Antenna16_8.m:36-40](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/Matlab/Antenna16_8.m#L36-L40) —— 构造 `pcbStack`（三层：天线面 / 介质 / 地平面），并在第 40 行设 16 个馈电点 `FeedLocations`（沿一列分布），用同相馈电。这就是贴片阵列的物理模型。

#### 4.2.4 代码实践

**实践目标：** 用 `Slotted_DielectricFilled_Waveguide.m` 的公式，手算氧化铝与石英两种填充下的 λg 与缝间距，理解「换介质 = 换尺寸」。

**操作步骤：**

1. 取 c≈3×10⁸ m/s，f0=10.5 GHz，故 λ0≈28.57 mm。
2. 氧化铝（εr=9.8，a=8.5 mm）：
   - fc10 = c/(2·a·√εr) = 3e8 /(2·0.0085·3.130) ≈ 5.61 GHz。
   - λd = λ0/√εr = 28.57/3.130 ≈ 9.13 mm。
   - λg = λd/√(1−(fc10/f0)²) = 9.13/√(1−(5.61/10.5)²) = 9.13/√(1−0.2856) = 9.13/0.8452 ≈ 10.80 mm。

   等等——这与脚本注释「λg=33.83 mm」对不上。重读 `Slotted_DielectricFilled_Waveguide.m` 第 16 行：`fc10 = c0/(2*a*sqrt(eps_r))`，但脚本里 a=8.5 mm 时算出的 fc10 其实 ≈ 5.61 GHz；而注释说 λg≈33.83 mm、缝间距 16.915 mm。代入反推：若 λg=33.83，则 λd/√(1−(fc10/f0)²)=33.83，而 λd=9.13，需 √(...)=0.27，即 fc10/f0≈0.96，fc10≈10.1 GHz——这意味着该脚本注释里的数值与 a=8.5 mm 并不自洽，**注释可能引用了不同 a 值的结果**。
3. 石英（εr=3.8，a=13.28 mm）：fc10≈5.79 GHz、λd≈14.66 mm、λg≈17.6 mm、缝间距≈8.8 mm（与 4.1.4 的估算一致）。

**需要观察的现象：** 同样是「缝隙波导」，换介质后宽壁 a、λg、缝间距全都变；且氧化铝脚本的注释数值与公式参数存在不自洽。

**预期结果：** 你会发现「读代码不读注释」的纪律在仿真脚本里同样成立——公式（第 16、22 行）是真值，注释里的具体毫米数（第 26 行）可能是早期迭代的残留。**把这种不一致记录下来，并以公式为准——待本地用 Matlab 实跑确认。**

> 这是「源码阅读型实践」：重点是练就用公式反推、用代码校核注释的能力，而不是盲信注释里的数字。

#### 4.2.5 小练习与答案

**练习 1：** 缝隙为什么要在中心线两侧「交替偏置」（±offset），而不是全部偏在同侧？

**参考答案：** 相邻缝间距是 λg/2，对应波导内 TE10 模相位差 180°。如果所有缝都偏在同侧，相邻缝辐射会反相相消；把缝交替偏置到中心线两侧，相当于再引入 180° 的相位反转，两次 180° 抵消，所有缝辐射同相叠加，形成强主瓣。这就是「谐振式缝隙阵」的核心。

**练习 2：** 为什么 Extended 版用缝隙波导而不是继续用贴片？

**参考答案：** 缝隙波导是全金属结构，耐高功率（Extended 版有 10 W 级 GaN 功放）、散热好、损耗低；介质填充还能在 10.5 GHz 把波导做小，便于排成 32 列紧凑阵列。贴片阵列在这些方面都不如缝隙波导，所以 Nexus（低功率、3 km）够用、Extended（高功率、20 km）必须升级。

---

### 4.3 阵列方向图综合：从单元到扫描波束

#### 4.3.1 概念说明

模块 4.1/4.2 仿真的是「单个辐射单元/单根波导列」。但 AERIS-10 是 **相控阵**——16 列（或 8 列）× 32 行的单元一起辐射，靠 ADAR1000 相移器给每个单元加不同的相位，让波束在空间里电子扫描（±45°）。

全波仿真（openEMS）算 16×32=512 个单元的相互耦合太贵了。工程上的做法是 **方向图综合**：用解析的「阵列因子」公式快速算方向图，单元本身的特性用一个「单元因子」近似。本讲的 `array_pattern_Kaiser25dB_like.py` 就是干这件事——它不调用 openEMS，纯 numpy 几毫秒就能画出整个阵列的方向图，包括副瓣锥削。

#### 4.3.2 核心流程

**方向图 = 阵列因子 × 单元因子**：

\[
\text{Pattern}(\theta,\phi) = |AF(\theta,\phi)| \cdot |EF(\theta,\phi)|
\]

对 M×N 的平面阵（y 方向 M 个、z 方向 N 个，间距 dy、dz），阵列因子是两个方向的可分离乘积：

\[
AF(\theta,\phi) = \left(\sum_{m=0}^{M-1} w_m\, e^{j\,k_0\,y_m(\sin\theta\sin\phi - \sin\theta_0\sin\phi_0)}\right)
                 \left(\sum_{n=0}^{N-1} w_n\, e^{j\,k_0\,z_n(\sin\theta\cos\phi - \sin\theta_0\cos\phi_0)}\right)
\]

其中 (θ₀,φ₀) 是想让主瓣指向的方向（扫描角），wₘ/wₙ 是各单元的幅度加权（锥削），k₀=2π/λ₀。改变 (θ₀,φ₀) 就是电子扫描——这正是 ADAR1000 给每个单元加相位差在做的事（与 u2-l2 的相控阵公式 Δφ=2π/λ·d·sinθ 一致）。

**为什么用 Kaiser 锥削（wz = np.kaiser(N, beta)）？** 等幅加权（所有 w=1）的阵列副瓣很高（第一副瓣约 −13 dB），会把弱目标盖住。给边缘单元降幅度（锥削/窗）可以压低副瓣（脚本标题写「~−25 dB」），代价是主瓣变宽、增益略降。这是雷达方向图设计的经典权衡。

**计算流程**：脚本先算 y 和 z 两个方向的阵列因子 AFy、AFz，相乘得总 AF；再乘单元因子（这里用 |cos θ| 近似）；归一化后画 E 面（φ=0）、H 面（φ=90°）方向图与二维热图。

#### 4.3.3 源码精读

**阵列规模与间距**（对应 32×16 物理阵列）：

[5_Simulations/array_pattern_Kaiser25dB_like.py:10-13](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/array_pattern_Kaiser25dB_like.py#L10-L13) —— `M=16`（y 方向，列）、`N=32`（z 方向，每列缝数），`dy=14.276 mm ≈ λ0/2`（自由空间半波长，防栅瓣），`dz=16.915 mm`（注意它正好等于氧化铝缝隙波导的缝间距，呼应模块 4.2）。这与 README「32×16 阵列」一致。

**锥削加权**：

[5_Simulations/array_pattern_Kaiser25dB_like.py:20-23](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/array_pattern_Kaiser25dB_like.py#L20-L23) —— y 方向等幅（`wy=ones`），z 方向用 `np.kaiser(32, beta=1.65)` 锥削并归一化。beta=1.65 对应接近汉宁窗的轻度锥削，目标副瓣约 −25 dB。

**阵列因子核心公式**（直接对应上面的数学）：

[5_Simulations/array_pattern_Kaiser25dB_like.py:33-41](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/array_pattern_Kaiser25dB_like.py#L33-L41) —— `ky/kz` 是观察方向的波数分量，`ky0/kz0` 是扫描方向的波数分量；`Ay`、`Az` 分别是 y、z 方向的复数求和（带锥削权重），两者相乘即总 AF。改 `theta0/phi0` 就能让主瓣指向别处（扫描）。

**半功率波束宽度（HPBW）计算**：

[5_Simulations/array_pattern_Kaiser25dB_like.py:52-59](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/array_pattern_Kaiser25dB_like.py#L52-L59) —— `hpbw_deg` 找到主瓣峰值、再量出比峰值低 3 dB 的两点角度差，即波束宽度。这是评估「雷达角分辨率」的关键指标（波束越窄、分辨越准）。

**单元因子**：

[5_Simulations/array_pattern_Kaiser25dB_like.py:30-31](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/array_pattern_Kaiser25dB_like.py#L30-L31) —— 单元因子用 `|cos θ|` 近似（典型缝隙/贴片单元的上半空间方向图）。

#### 4.3.4 代码实践

**实践目标：** 不改硬件、只改两行参数，观察「扫描」与「锥削」对方向图的影响。这个脚本只依赖 numpy + matplotlib，无需 openEMS/Matlab，最容易本地实跑。

**操作步骤：**

1. 装好 `numpy`、`matplotlib`（`uv sync --group dev` 已含）。
2. **实验 A（扫描）**：把第 16 行 `phi0_deg = 0.0` 改成 `phi0_deg = 30.0`（或改 `theta0_deg`），重跑。
3. **实验 B（锥削）**：把第 20 行 `beta = 1.65` 改成 `0.0`（等幅加权），重跑。
4. 观察输出的三张图：`E_plane_Kaiser25dB_like.png`、`H_plane_Kaiser25dB_like.png`、`Heatmap_Kaiser25dB_like.png`。

**需要观察的现象：**

- 实验 A：主瓣应明显偏离 0°，指向你设的扫描角方向；同时副瓣电平随扫描角变化（扫描越大、波束越宽——这是相控阵的「波束展宽」现象）。
- 实验 B：beta=0（等幅）时，第一副瓣会从约 −25 dB 升到约 −13 dB（更接近理论的 −13.2 dB），但主瓣变窄。

**预期结果：** E 面与 H 面的 −3 dB 波束宽度会被打印在图标题里（脚本第 68、80 行的 `hpbw_deg`）。16×32 阵列在 λ0/2 间距下，理论半功率波束宽度约

\[
\text{HPBW} \approx 0.886\frac{\lambda}{N\cdot d}
\]

对 N=32、d=λ0/2（即 N·d=16·λ0）约 0.886/16 rad ≈ 3.18°。**实跑数值以本地输出为准。**

#### 4.3.5 小练习与答案

**练习 1：** 把 dy 从 λ0/2 加大到 λ0，方向图会发生什么？

**参考答案：** 间距超过 λ0/2 后，空间采样定理被破坏，方向图里会出现 **栅瓣**（grating lobes）——即除了主瓣外，在别的角度出现与主瓣一样高的波束。这在雷达里是灾难（无法判断目标到底在哪个方向），所以阵列间距几乎总是取 λ0/2 或略小。

**练习 2：** 为什么脚本只在 z 方向（32 单元）做 Kaiser 锥削，而 y 方向（16 单元）等幅？

**参考答案：** 这是一个工程取舍。z 方向 32 单元多，副瓣贡献大，锥削收益高；y 方向只有 16 单元，再锥削会进一步减少有效单元数、显著加宽波束，得不偿失。实际工程里会在两个方向都做锥削并整体优化，本脚本是简化示例，展示「锥削能压副瓣」这一核心手段。

---

## 5. 综合实践

把三个模块串起来，做一次「单元 → 阵列」的设计推演：

**任务：** 假设你要把 Extended 版的天线从石英填充（εr=3.8）换成氧化铝填充（εr=9.8），评估它对系统的影响。

1. **单元层（模块 4.1/4.2）：** 用 4.2.4 的公式，算出氧化铝下波导宽壁 a 需要多小才能让 fc10 远低于 10.5 GHz（保证单模传输）。指出这会让波导变窄、整块阵列变紧凑，但氧化铝更脆、更难加工。
2. **仿真层（模块 4.1）：** 决定先跑 `PROFILE="sanity"` 还是 `full`？为什么？（答：先 sanity 看细胞数与内存，再上 full——这正是三档配置的意义。）
3. **阵列层（模块 4.3）：** 介质改变后，缝间距 `λg/2` 变了（氧化铝的 λg 更短），但阵列的 **列间距 dy** 应该保持 `λ0/2`（自由空间半波长）不变——为什么？（答：栅瓣由自由空间波长 λ0 决定，与波导内填充介质无关。）

**交付物：** 一页纸的「换介质影响清单」，分单元/仿真/阵列三栏，每栏写一句话结论 + 一个要重新跑的脚本名。这能帮你建立「改一个参数，三层都要复核」的系统级工程直觉。

## 6. 本讲小结

- AERIS-10 用两套互补的仿真：openEMS 做 **单元级全波 FDTD**（贵但精确，含 S 参数与远场增益），解析阵列因子做 **阵列级方向图综合**（便宜快速，用于扫描与副瓣评估）。
- `openems_quartz_slotted_wg_10p5GHz.py` 的核心是 **`sanity/balanced/full` 三档配置**：它把网格分辨率、缝数、空气盒、频率点、PML 层打包成可切换的档位，让你先用便宜的 sanity 查错、再上贵的 full 精算；脚本主动打印细胞数与内存估算来守住 16 GB 预算。
- FDTD 的「网格变细 ⇒ 时间暴涨」有物理根源：细胞数立方级增长 + CFL 条件让时间步变小、步数变多。
- 缝隙波导的设计规则全部从导波波长 λg 推导：缝间距 λg/2 + 交替偏置实现同相辐射、缝长 0.47λg。仓库里有石英（εr=3.8）与氧化铝（εr=9.8）两种填充选型。
- 阵列方向图 = 阵列因子 × 单元因子；Kaiser 锥削压副瓣（−13 dB → −25 dB），代价是主瓣变宽；间距超过 λ0/2 会出栅瓣。
- 读仿真脚本要「以公式与代码为准」：`Slotted_DielectricFilled_Waveguide.m` 注释里的毫米数与公式参数存在不自洽，应以公式为真值并本地复算确认。

## 7. 下一步学习建议

- **横向**：阅读 `5_Simulations/AAF_openEMS/aaf_simulation.py`（另一份 openEMS 仿真）、`Matlab/Antenna_array.m` 与 `antenna16_antenna8.m`（同族贴片阵列脚本），对比它们与本讲主脚本的异同。
- **向 RF 链路**：进入 u12-l2《RF 链路与滤波器仿真》，看 IF 带通滤波器（LTspice）、DAC 重建滤波与 QPA2962 GaN 功放仿真——它们与天线共同决定了整条射频链路的性能。
- **向硬件实物**：结合 u13-l1《硬件设计文件导览》里的天线板原理图与 Gerber，对照本讲的几何参数（缝间距、贴片尺寸）落到真实 PCB 上是什么样。
- **向系统验证**：若想看天线方向图如何影响后续信号处理，可回看 u2-l2 的波束扫描与 u4 系列的接收信号处理链，理解「天线主瓣宽度 ↔ 角分辨率」「副瓣电平 ↔ 杂波/虚警」的系统级联系。
