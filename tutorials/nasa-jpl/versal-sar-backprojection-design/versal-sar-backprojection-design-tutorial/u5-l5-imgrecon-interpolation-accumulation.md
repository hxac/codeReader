# 图像重建(三)：插值、累加与 SIMD/流水

## 1. 本讲目标

本讲是「图像重建内核」三讲的收尾篇。u5-l3 算出了每个像素的差分距离和 RC 缓冲索引（`low_idx`/`high_idx`），u5-l4 把差分距离换算成复数相位校正向量 `ph_corr_vec`。本讲要回答最后三个问题：

1. 拿到索引后，怎么从 RC 缓冲里取出回波样本？为什么要做**线性插值**，而且只能**逐元素标量取**？
2. 取出的样本怎么累加成最终图像？为什么 `m_img` 要**跨 602 次内核调用持久累加**，又怎么靠 **RTP** 决定何时把图写出去？
3. 散落在 5 个循环上的 `chess_prepare_for_pipelining` 到底在做什么？它和 SIMD 是什么关系？

学完后，你应当能：
- 说清线性插值那段标量 `for` 循环为何**无法 SIMD 向量化**（动态索引的 gather 问题）；
- 解释 `m_img` 作为类成员如何跨调用累加、RTP 如何控制「最后一脉冲才 dump」；
- 区分 SIMD（数据并行）与流水/ILP（时间重叠），并看懂 `chess_prepare_for_pipelining` 的作用。

## 2. 前置知识

阅读本讲前，请确保已掌握：

- **u5-l3**：差分距离 `differ_range_vec` 的 SIMD 计算，以及把它换算成整数索引的 `low_idx_int_vec`/`high_idx_int_vec`、浮点索引 `px_idx_acc`。
- **u5-l4**：相位校正向量 `ph_corr_vec`（即 \(e^{j\theta}\)）的由来与 `sincos_complex` 域限制。
- **u2-l2 / u4-l3**：buffer 端口与 `pktstream` 包流的区别；`single_buffer` 的含义。
- **u3-l5**：主机用 RTP（`rtp_dump_img_in`）在末脉冲置 1 来通知内核 dump。
- **u1-l4**：`SAMPLES = (PULSES*RC_SAMPLES)/IMG_SOLVERS` 的整除约束。

一个直觉：反投影的「相干累加」就是把每个像素在所有脉冲上的相位校正后回波**同相叠加**，真实目标越叠越强、非目标相互抵消。本讲就是把这条累加公式落到 AIE 代码的最后一公里。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [design/aie/backprojection.cc](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc) | `img_reconstruct_kern` 的全部实现，本讲聚焦其后段（插值、累加、dump）与流水指令 |
| [design/aie/custom_kernels.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h) | `ImgReconstruct` 类声明，定义跨调用持久成员 `m_img` 与 `m_id` |
| [design/common.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h) | `PULSES`/`RC_SAMPLES`/`IMG_SOLVERS` 等规模宏与雷达常数 |
| [doc/sections/future_work.tex](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex) | 「Dynamic Buffer Indexing for Interpolation」等优化方向，本讲实践任务与之呼应 |

---

## 4. 核心概念与源码讲解

### 4.1 标量索引线性插值

#### 4.1.1 概念说明

u5-l3 把每个像素的差分距离换算成了一个**带小数的索引** `px_idx_acc`：它指向 RC 缓冲（距离压缩样本）里「最接近真实双程时延」的位置。但 RC 缓冲是离散采样——真实时延几乎不会正好落在某个整数采样点上，而是落在两个相邻样本之间。

线性插值就是用这两个相邻样本按小数权重做加权平均，得到一个更逼近真实值的「虚拟样本」：

\[
\text{interp} = \text{rc}[\text{low}] + t\cdot\bigl(\text{rc}[\text{high}] - \text{rc}[\text{low}]\bigr),\quad t\in[0,1)
\]

其中 `low`/`high = low+1` 是包围真实索引的两个整数邻居（u5-l3 已算出 `low_idx_int_vec`/`high_idx_int_vec`），\(t\) 是真实索引超出 `low` 的小数部分。

为什么要插值？因为直接取最近的整数样本会引入**量化误差**，使相位不对齐、图像变模糊。插值把亚采样级精度补回来，是反投影聚焦质量的关键。

#### 4.1.2 核心流程

这一段紧接 u5-l4 的相位校正计算之后，先补算插值权重 \(t\)，再进一个 16 次的标量循环逐像素取值：

1. **算小数权重**：\(t = \text{px\_idx\_acc} - \text{low\_idx}\)（把整数索引还原回 float 再相减）。
2. **逐像素（16 个）线性插值**：
   - 取相邻两样本之差 `rc_delta = rc[high] − rc[low]`；
   - 按权重放大 `px_rc_delta = rc_delta * t`；
   - 加回低样本 `interp = px_rc_delta + rc[low]`。
3. **乘相位校正**：`img = interp * ph_corr_vec[px_idx]`（复数乘，把相位转正）。
4. **累加进 `m_img`**（见 4.2）。

> 关键卡点：步骤 2 里 `rc[high]`、`rc[low]` 的下标是**每个像素都不一样、且由运行时数据决定**的。AIE 的向量加载指令需要一个基地址 + 固定步长（连续或固定 stride），无法一次发出 16 个互不相关的任意地址加载（即 SIMD gather）。于是这里**只能退化成标量循环，一个像素一次加载**。这正是文档里反复强调的性能瓶颈，也是本讲实践任务的核心。

#### 4.1.3 源码精读

先看插值权重的计算（紧跟 u5-l4 的相位校正之后）：

[design/aie/backprojection.cc:173-175](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L173-L175) —— 把整数索引 `low_idx_int_vec` 还原为 float，再用真实浮点索引 `px_idx_acc` 减去它，得到每个像素的小数权重 `px_delta_idx_vec`。

```cpp
// Fractional part for interpolation
low_idx_float_vec = aie::to_float(low_idx_int_vec);
auto px_delta_idx_vec = aie::sub(px_idx_acc, low_idx_float_vec).to_vector<float>(0);
```

接着是本讲最关键的标量循环：

[design/aie/backprojection.cc:178-185](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L178-L185) —— 对 16 个像素逐个做线性插值、乘相位校正、累加进 `m_img`。

```cpp
for(int px_idx=0; px_idx<16; px_idx++) chess_prepare_for_pipelining {
    rc_delta = rc_in_iter[high_idx_int_vec.get(px_idx)] - rc_in_iter[low_idx_int_vec.get(px_idx)];
    auto px_rc_delta = rc_delta*(float)(px_delta_idx_vec.get(px_idx));
    auto interp = px_rc_delta+rc_in_iter[low_idx_int_vec.get(px_idx)];

    auto img = interp*ph_corr_vec.get(px_idx);
    m_img[(px_seg_idx*16) + px_idx] += img;
}
```

逐行说明：

- `.get(px_idx)` 把 16 元素向量里第 `px_idx` 个**标量**取出来——一旦用了 `.get()`，就退出了向量世界，进入标量世界。
- `rc_in_iter[...]` 是对 `rc_in` 缓冲的随机访问（`rc_in_iter = aie::begin(rc_in)`，见 L104）。方括号里的下标随像素变化、且来自运行时算出的几何量，编译期无法预测，故无法向量化。
- 三行插值正是公式 \(\text{rc}[\text{low}]+t(\text{rc}[\text{high}]-\text{rc}[\text{low}])\) 的直译。
- `interp*ph_corr_vec.get(px_idx)`：`interp` 是 `cfloat`，`ph_corr_vec.get(px_idx)` 也是 `cfloat`，相乘即把样本旋转到正确相位（u5-l4 的成果在此用上）。

> 对比：同一函数里差分距离段（u5-l3，L121-135）和相位校正段（u5-l4，L150-168）全是 `aie::vector<float,16>` 的整体运算，16 路并行；唯独这里退回标量。这种「连续向量流被 gather 打断」正是 [doc/sections/future_work.tex:203-215](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L203-L215) 所说的「breaks the otherwise continuous flow of vectorized computation」。

#### 4.1.4 代码实践

**实践目标**：亲手定位插值标量循环，论证它为何无法 SIMD 向量化，并给出一个初步改进设想。

**操作步骤**：

1. 打开 [design/aie/backprojection.cc:178](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L178)，找到 `for(int px_idx=0; px_idx<16; ...)` 这段。
2. 在同函数里对比 L121-135 的差分距离计算（`aie::sub`/`aie::mul_square`/`aie::sqrt` 作用在整条 16 元素向量上）。
3. 回答：差分距离段为何能一次算 16 个像素、而插值段只能一次算 1 个？
4. 阅读文档 [doc/sections/future_work.tex:203-215](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L203-L215)（Dynamic Buffer Indexing for Interpolation）。

**需要观察的现象 / 预期结果**：

- 差分距离段：输入 `x_pxls_vec` 等是**整条向量**，`aie::sub(标量, 向量)` 返回向量——一次 16 路并行。
- 插值段：下标 `high_idx_int_vec.get(px_idx)` 是**逐个取出**的标量，`rc_in_iter[标量]` 是 16 个互不相同的随机地址加载。AIE 向量 load 指令做不到这件事，所以编译器只能发出 16 条标量 load。
- 结论：瓶颈不在算力，而在**数据相关的随机访存（gather）**。

**改进设想（初步）**：

- 思路 A——**预排序 RC**：若主机（ARM）或一个专用内核提前把每个像素所需的 RC 段聚集成**连续内存**，AIE 端就能用向量 load 一次取 16 个，把 gather 变成连续访问。代价是主机侧要预计算索引分布。
- 思路 B——**选择性 RC 分发**：对应 [doc/sections/future_work.tex:185-201](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L185-L201) 的「Selective RC Sample Distribution」——只把每个内核真正需要的 RC 段送进去，既省局部存储又便于连续布局。
- 思路 C——**等待工具链/库支持**：文档提到可关注未来 AMD AIE API 是否提供原生 gather 指令或 Vitis DSP 库支持。

> 待本地验证：以上设想均需在真实 aiesimulator 或硬件上度量插值段占总周期的比例后才有定论。文档明确把此项列为 future work，本仓库**尚未**实现。

#### 4.1.5 小练习与答案

**练习 1**：差分距离段（u5-l3）和插值段都在算几何量，为什么前者能 SIMD、后者不能？

> **答案**：差分距离段的输入（像素坐标 `x/y/z_pxls_vec`、天线位置）一旦加载成向量，所有运算（减、平方、加、开方）都是**逐元素整体**进行，地址模式固定（整条向量）。插值段要从 RC 缓冲取数，而每个像素的索引由其位置和天线位置**运行时**算出、彼此不同且不连续，属于数据相关的随机访存，向量 load 无法表达，故只能标量。

**练习 2**：插值权重 \(t\) 的取值范围是什么？若 \(t=0\) 或 \(t\to 1\) 分别意味着什么？

> **答案**：\(t = \text{px\_idx\_acc} - \text{low\_idx} \in [0,1)\)。\(t=0\) 表示真实索引恰为整数 `low`，`interp = rc[low]`（无需插值）；\(t\to 1\) 表示真实索引紧贴 `high` 一侧，结果趋近 `rc[high]`。

---

### 4.2 m_img 跨调用累加与 RTP dump

#### 4.2.1 概念说明

一次反投影成像要把 **602 个脉冲**（`PULSES`）的贡献叠到同一张图上。但 `img_reconstruct_kern` 每次只处理一个脉冲的数据（一组 slowtime + 一条 RC 距离线 + 一批目标像素）。要把 602 次的结果攒起来，内核需要一块**跨调用持久的累加缓冲**——这就是类成员 `m_img`。

这正是 u5-l3 提到的、为何重建内核必须是**类** `ImgReconstruct` 而非自由函数：自由函数的无状态模型无法在调用之间记住累加值。`m_img` 作为类成员，随对象在 tile 的局部存储里常驻，602 次调用持续往里 `+=`。

那什么时候把攒好的图写出去？显然只有**全部 602 脉冲累加完成**之后。源码用一个运行时参数 `rtp_dump_img_in`（RTP）当开关：前 601 次调用传 0（只累加、不输出），第 602 次（末脉冲）传 1（把 `m_img` 打包写出）。主机侧（u3-l5）正是这样逐脉冲 `graph.update(rtp_dump_img_in, ...)` 的。

#### 4.2.2 核心流程

**累加**（每个脉冲、每个像素都执行）：

\[
\text{m\_img}[p] \mathrel{+}= \text{interp}[p]\cdot e^{j\theta[p]}
\]

跨 602 脉冲累加完，得到像素 \(p\) 的最终聚焦值：

\[
\text{m\_img}[p] = \sum_{k=0}^{\texttt{PULSES}-1} \text{interp}_k[p]\cdot e^{j\theta_k[p]}
\]

这就是 u1-l1 所说的**相干累加**——真实目标同相相加越来越强，非目标相位错乱相互抵消。

**dump**（仅当 `rtp_dump_img_in == 1`）：

1. 建 `m_img` 迭代器，取首元素；
2. 写包头部：`writeHeader` + `m_id`（内核实例号）+ 2 个 32 位 padding，凑满一个 128 位 PLIO beat；
3. 循环把 `SAMPLES` 个 `cfloat`（每个拆 real/imag 两个 32 位字）写出；
4. 最后一个字置 `tlast=true`，标记包结束。

#### 4.2.3 源码精读

先看 `m_img` 的声明——它是 `ImgReconstruct` 的私有成员：

[design/aie/custom_kernels.h:38-42](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L38-L42) —— 类持有内核身份 `m_id` 与跨调用累加缓冲 `m_img`。

```cpp
private:
    uint32 m_id;
    // Used for focusing image over several iterations
    alignas(aie::vector_decl_align) cfloat m_img[(PULSES*RC_SAMPLES)/IMG_SOLVERS];
```

默认配置下 `m_img` 长度 = (602×512)/224 = **1376 个 cfloat**（约 10.75 KiB），常驻该内核所在 tile 的局部数据存储，跨 602 次调用持续累加。注释「Used for focusing image over several iterations」点明了它的职责。

累加发生在 4.1.3 那段标量循环的最后一行：

[design/aie/backprojection.cc:184](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L184) —— 把本脉冲的相位校正样本累加进持久缓冲。下标 `(px_seg_idx*16)+px_idx` 是**连续**的（从 0 走到 SAMPLES-1），所以这里写回 `m_img` 并不存在 gather 问题。

```cpp
m_img[(px_seg_idx*16) + px_idx] += img;
```

dump 段由 RTP 开关把门：

[design/aie/backprojection.cc:188-213](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L188-L213) —— 仅当 `rtp_dump_img_in` 为真（末脉冲）时，把累加好的 `m_img` 打包写出到 `img_out` 包流。

```cpp
if (rtp_dump_img_in) {
    auto img_iter = aie::begin(m_img);
    cfloat img_elem = *img_iter++;
    writeHeader(img_out, 0, getPacketid(img_out, 0));  // 包头
    writeincr(img_out, m_id, false);                   // 实例号（PL 路由器据此算 DDR 偏移）
    writeincr(img_out, 0, false);                      // padding
    writeincr(img_out, 0, false);                      // padding
    for(int i=0; i<SAMPLES-1; i++) chess_prepare_for_pipelining {
        writeincr(img_out, img_elem.real, false);
        writeincr(img_out, img_elem.imag, false);
        img_elem = *img_iter++;
    }
    writeincr(img_out, img_elem.real, false);
    writeincr(img_out, img_elem.imag, true);           // tlast：包结束
}
```

几个要点：

- **`rtp_dump_img_in` 是 RTP**：它在 [函数签名](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L62-L66) 里是普通 `int` 参数，但图里以 `connect<parameter>` 接到运行时参数端口（u2-l2/u3-l5），主机用 `graph.update` 在末脉冲置 1。
- **`m_id` 的用途**：每个重建内核的 `m_id` 全局唯一（u4-l2 的 `create_object<ImgReconstruct>(id)`），它被写进包头元数据，让 PL 包路由器能算出 `ddr_offset = m_id × 每核样本数`，把 224 个内核乱序到达的图像包拼回连续 DDR（详见 u6-l1）。
- **128 位对齐**：包头 = `writeHeader`(32b) + `m_id`(32b) + 2×padding(64b) = 128b，正好一个 PLIO beat；注释明说「Add two uint32 padding since bus width is 128 bit」。
- **`tlast`**：最后一个 `imag` 字置 `true`，是 AXI4-Stream 包结束标志，PL 路由器据此识别本内核图像包的边界。
- **bit 透传**：`img_elem.real`（float）被当作 32 位字写进 `pktstream`，PL 侧只按 `instance_id` 搬运、不解释内容，主机最终 `sync(FROM_DEVICE)` 回来再按 `cfloat` 读——与 u5-l3 的 `reinterpret_cast` 一脉相承，位模式全程不变。

#### 4.2.4 代码实践

**实践目标**：验证「RTP = 0 只累加、RTP = 1 才输出」的行为，并算清 `m_img` 的体积。

**操作步骤**：

1. 在 [custom_kernels.h:41](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L41) 确认 `m_img` 长度宏 `(PULSES*RC_SAMPLES)/IMG_SOLVERS`，代入默认值（602/512/224）算出 1376 个 cfloat。
2. 在 [backprojection.cc:184](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L184) 确认 `+=` 无条件执行（每个脉冲都累加）。
3. 在 [backprojection.cc:188](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L188) 确认 dump 被 `if (rtp_dump_img_in)` 把门。
4. 对照 [design/host/sar_backproject.cpp](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp)（u3-l5）里主机对 `rtp_dump_img_in` 的 update：前 601 脉冲置 0、末脉冲置 1。

**需要观察的现象 / 预期结果**：

- `m_img` 体积 = 1376 × 8 B ≈ **10.75 KiB**，可常驻单个 AIE tile 的 32 KiB 局部存储（u2-l1）。
- 若误把 RTP 一直置 1：每个脉冲都会 dump 一次半成品图，PL 路由器会收到 602 张不完整的图，主机最终只拿到末脉冲那份（图像严重欠聚焦）。
- 若误把 RTP 一直置 0：永远不输出，主机 `sync(FROM_DEVICE)` 拿到的是全零/旧数据。

> 待本地验证：可在 aiesimulator 里改 `rtp_dump_img_in` 的投递节奏，观察输出包数量与图像聚焦程度的变化。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 RTP 开关控制 dump，而不是每个脉冲都写出 `m_img`？

> **答案**：`m_img` 要累加满 602 个脉冲才是完整聚焦图。每脉冲都写会输出 602 张半成品、且 `m_img` 会被当作「成品」 prematurely 读走/污染后续累加。RTP 开关让内核在累加期静默、仅在末脉冲一次性 dump 完整图，既省带宽又保证正确性。

**练习 2**：dump 段里 `m_id` 写进包头有什么用？不写会怎样？

> **答案**：224 个内核的输出包经 `pktmerge` 汇到一条 PLIO 流后**到达顺序是乱的**。PL 包路由器靠包头里的 `m_id` 计算 `ddr_offset`，把每个内核的 1376 个像素写回 DDR 中属于它的不重叠区段。不写 `m_id`，路由器无法区分包来自哪个内核，图像会被错位拼接。

**练习 3**：`m_img` 的写回索引 `(px_seg_idx*16)+px_idx` 是连续的，而 RC 的读取索引是随机的。这说明什么？

> **答案**：累加写回**不存在** gather 问题，理论上可向量化；瓶颈只在 RC 的随机读取。这也暗示优化应聚焦在 RC 侧的访存模式，而非 `m_img` 侧。

---

### 4.3 chess_prepare_for_pipelining 流水

#### 4.3.1 概念说明

`chess_prepare_for_pipelining` 是写给 **Chess 编译器**（AIE 底层编译器）的循环流水提示。它的作用是让编译器对一个循环做**软件流水（software pipelining）**：把循环体的多条指令跨迭代重叠执行——当第 \(k\) 次迭代还在做累加时，第 \(k+1\) 次迭代的加载已经开始了。

这与本设计的三大并行手段直接对应（[doc/sections/future_work.tex:50-63](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L50-L63)）：

| 手段 | 含义 | 本设计体现 |
|------|------|-----------|
| **SIMD**（数据并行） | 一条指令同时处理多个数据 | `aie::vector<float,16>` 的 16 路运算（u5-l3/u5-l4） |
| **多核** | 多个内核同时跑 | 224 个重建内核分布在不同 tile（u4） |
| **ILP / 流水** | 同一内核内，跨迭代重叠指令 | `chess_prepare_for_pipelining`（本节） |

衡量流水的指标是**发起间隔 II（Initiation Interval）**：相邻两次迭代启动之间相隔的周期数。II=1 是理想——每周期启动一次新迭代。流水通过填满功能单元的「气泡」、隐藏访存与运算延迟来降低 II。

#### 4.3.2 核心流程

不写该指令时，编译器可能保守地**串行**展开循环（一次迭代彻底完成才开始下一次，存在等待气泡）。写了之后，编译器会：

1. 分析循环体里的数据依赖与资源占用；
2. 重排指令，把第 \(k+1\) 次迭代的「前期指令」（如加载、地址计算）插到第 \(k\) 次迭代的「后期指令」（如乘加）的空隙里；
3. 尝试达到尽可能小的 II。

对本内核至关重要的是：**即使插值段无法 SIMD，它仍能从流水中受益**。标量循环里每个像素要发两次随机加载（`rc[high]`、`rc[low]`），加载有延迟；流水让「第 \(k\) 个像素的累加」与「第 \(k+1\) 个像素的加载」重叠，把加载延迟隐藏掉。所以 4.1 那段标量循环也挂了 `chess_prepare_for_pipelining`——它虽救不了「一次只能处理 1 个像素」的根本吞吐，但能把每个像素的处理周期数压下来。

#### 4.3.3 源码精读

该指令在 `backprojection.cc` 中共出现 **5 次**，分别守护不同循环：

- [backprojection.cc:53](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L53) —— `data_broadcast_kern` 里 RC 数据的 16 元素块拷贝循环（u5-l1）。
- [backprojection.cc:79](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L79) —— 重建内核里把目标像素从包流读入 `x/y/z_trgt_pxls` 缓冲的循环。
- [backprojection.cc:111](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L111) —— 重建内核的**主计算循环**（`SAMPLES/16` 段，每段 16 像素的差分距离+相位校正，u5-l3/u5-l4）。
- [backprojection.cc:178](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L178) —— 4.1 讲的**插值标量循环**。
- [backprojection.cc:206](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L206) —— dump 段把 `m_img` 写出的循环。

典型写法（主计算循环）：

```cpp
for(int px_seg_idx=0; px_seg_idx < SAMPLES/16; px_seg_idx++) chess_prepare_for_pipelining {
    ...  // 一整段 16 路 SIMD 差分距离 + 相位校正
}
```

`chess_prepare_for_pipelining` 紧跟在 `for` 头之后、循环体之前，作用域是整个循环体。

> 注意区分：SIMD 是「**一条**指令处理 16 个数据」（空间并行，靠向量寄存器与向量功能单元）；流水是「**多条**指令在时间上重叠」（时间并行，靠重排调度）。主循环 L111 同时享受两者——循环体内部是 16 路 SIMD，循环之间是流水。而插值循环 L178 只有流水、没有 SIMD（被 gather 阻断）。

#### 4.3.4 代码实践

**实践目标**：理解流水指令的「有/无」差异，并学会用 Vitis Analyzer 度量 II。

**操作步骤**：

1. 在 [backprojection.cc](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc) 里数 `chess_prepare_for_pipelining` 出现的 5 处，确认它们分别属于哪个循环。
2. 对照 [doc/sections/future_work.tex:50-63](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L50-L63)（ILP Optimizations），文档建议用 **Vitis Analyzer** 分析这些循环的 II 与吞吐。
3. 若本地有 Vitis 工具链：用 Makefile 的 `aiesim_profile` 目标（u8-l1）跑出 profile 数据，在 Vitis Analyzer 里查看 L111 主循环与 L178 插值循环的 II，对比两者吞吐。

**需要观察的现象 / 预期结果**：

- 主循环（L111，含 SIMD）每个 II 处理 16 个像素；插值循环（L178，标量）每个 II 仅处理 1 个像素——插值段的 II 数约为 SIMD 段的 16 倍量级，这与文档所说「notable performance penalty」吻合。
- 去掉 L178 的 `chess_prepare_for_pipelining`（仅作思想实验，**不要真的改源码**），编译器大概率串行展开，II 变大、总周期进一步增加。

> 待本地验证：II 的精确数值需用 Vitis Analyzer 实测；本仓库未随附测量结果。

#### 4.3.5 小练习与答案

**练习 1**：SIMD 和流水（ILP）有什么本质区别？

> **答案**：SIMD 是**空间并行**——一条向量指令同时作用于多个数据，靠向量寄存器/功能单元，本设计里是 16 路。流水是**时间并行**——多条标量（或向量）指令跨迭代在时间上重叠执行，靠指令调度，指标是发起间隔 II。二者正交：L111 主循环两者都有，L178 插值循环只有流水。

**练习 2**：插值循环已经无法 SIMD，挂 `chess_prepare_for_pipelining` 还有意义吗？

> **答案**：有。虽然救不了「每周期只处理 1 个像素」的根本吞吐，但流水能把每个像素的两次随机加载延迟与累加运算重叠隐藏，降低每像素的周期数。在 gather 无法消除的前提下，这是尽量压榨性能的手段。

**练习 3**：为什么文档说本设计「ILP 贡献较小、主要靠 SIMD 与多核」？

> **答案**：SIMD（16 路）和多核（224 核）分别把单核吞吐和总并行度拉满，量级远大于单核内的指令重叠。但插值段的 gather 瓶颈恰恰卡住了 ILP 的发挥空间，所以 ILP 相对贡献较小；这也是 future work 想优化动态索引的原因。

---

## 5. 综合实践

**任务**：跟踪「一个目标像素」从索引到最终进入输出包的完整后段旅程，并在一张表里标注每一步用的是 SIMD、标量还是流水。

**操作步骤**：

1. 选定主循环（[backprojection.cc:111](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L111)）里的某一个像素 `px_idx`，它随 `px_seg_idx` 属于某 16 元素段。
2. 沿调用链填写下表（左列是阶段，右列标注并行手段）：

   | 阶段 | 代码位置 | 并行手段 |
   |------|----------|---------|
   | 算差分距离 | L121-135 | SIMD（16 路） |
   | 算索引 low/high | L139-147 | SIMD |
   | 算相位校正角 → `ph_corr_vec` | L150-171 | SIMD |
   | 算插值权重 `px_delta_idx_vec` | L173-175 | SIMD |
   | 取 RC、线性插值 | L178-182 | **标量**（gather，无法 SIMD）+ 流水 |
   | 乘相位校正、累加进 `m_img` | L183-184 | 标量 + 流水 |
   | 末脉冲 dump（写包） | L188-213 | 标量 + 流水（L206） |

3. 回答两个串联问题：
   - **为什么 4、5 两步之间出现了 SIMD → 标量的「断点」？**（答：第 5 步要按运行时算出的随机索引读 RC，向量 load 表达不了 gather。）
   - **`m_img` 要经历多少次 `+=` 才聚焦完成？**（答：每个像素被 602 个脉冲各加一次，即 602 次累加后才在末脉冲被 dump。）

4. 最后，结合 [future_work.tex:203-215](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L203-L215) 写一段话：若能把第 5 步的 gather 改成连续向量 load，上表的「并行手段」列会发生什么变化？整条主循环的 II 会如何变化？

**预期结果**：你能清楚指出**整条数据通路里唯一被 gather 拖慢的一段**就是插值取 RC（L178-182），而其余步骤要么是 SIMD、要么是连续访存的累加/dump。这把 u5-l3/u5-l4/u5-l5 三讲串成了一条完整的「像素→聚焦图」流水线，并准确定位了 future work 要优化的那块「短板」。

> 待本地验证：第 4 步关于 II 变化的结论，需在 Vitis Analyzer 里对比优化前后的实测周期才有定论。

---

## 6. 本讲小结

- **线性插值**用相邻两个 RC 样本按小数权重 \(t\) 做加权平均，补出亚采样级精度的回波值；公式 \(\text{interp}=\text{rc}[\text{low}]+t(\text{rc}[\text{high}]-\text{rc}[\text{low}])\)。
- 插值段是**全内核唯一的标量瓶颈**：RC 下标由运行时几何量决定、每个像素不同，向量 load 无法表达 gather，只能逐元素取（[backprojection.cc:178-185](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L178-L185)）。
- `m_img` 是 `ImgReconstruct` 的类成员（[custom_kernels.h:41](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L41)），跨 602 次调用持久累加，实现相干聚焦；其写回索引连续，无 gather 问题。
- **RTP `rtp_dump_img_in`** 当开关：前 601 脉冲置 0 只累加，末脉冲置 1 才把完整图打包写出（[backprojection.cc:188](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L188)）。
- dump 包结构 = 包头 + `m_id` + padding（凑 128 位）+ `SAMPLES` 个 cfloat（real/imag 拆写），末字 `tlast` 标包尾；`m_id` 供 PL 路由器算 DDR 偏移。
- `chess_prepare_for_pipelining` 是给 Chess 编译器的流水提示（ILP），与 SIMD（数据并行）、多核并列为三大优化手段；它在 5 个循环上出现，插值段虽不能 SIMD 仍能靠流水隐藏加载延迟。

## 7. 下一步学习建议

- **接 u6-l1（PL 包路由器内核）**：本讲 dump 出去的包带 `m_id`，下一讲就看 PL 侧如何解析包头、按 `instance_id` 把 224 份乱序图像重排进连续 DDR，形成闭环。
- **接 u8-l3（优化与未来工作）**：本讲反复提到的「Dynamic Buffer Indexing」「Selective RC Distribution」会在那里系统梳理，可作为动手改进的选题。
- **延伸阅读**：用 Vitis Analyzer（u8-l1/u8-l2）实测主循环与插值循环的 II，验证本讲关于「插值段是标量瓶颈」的论断；并与 AMD XD100 参考设计的向量化 `sin/cos/sqrt` 实现做对比（[future_work.tex:218-240](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L218-L240)）。
