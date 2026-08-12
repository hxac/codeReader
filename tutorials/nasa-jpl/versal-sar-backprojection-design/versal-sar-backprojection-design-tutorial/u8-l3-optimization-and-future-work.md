# 优化机会与未来工作

## 1. 本讲目标

本讲是「仿真、度量与优化」单元的收尾，目光从「当前设计跑得多快、耗多少电」转向「下一步还能把它推到多快、多大、多完整」。读完本讲你应当能够：

- 说清当前反投影实现里**真正拖慢 AIE 计算**的两个算法瓶颈——插值阶段的标量动态索引、以及对全部 224 个重建内核的**全量 RC 广播**；
- 解释 `pl_stride` 分支为何被锁死在 `AIE_SWITCHES = 1`，根因是 `system.cfg` 无法表达数组化 AXI4-Stream 端口，并掌握文档给出的三条候选解法；
- 认识把反投影扩展成「完整雷达信号链」的四个方向：DSP 引擎做脉冲压缩（FFT/iFFT）、全 360° 孔径聚束、stripmap 模式，以及与 AMD XD100 参考设计的横向对比。

本讲的特殊性在于：它**几乎不引入新源码**，而是把前 27 讲已经读过的代码当作「体检对象」，用 `doc/sections/future_work.tex` 这份「待办清单」逐条对照源码，说清每个优化点卡在哪、改了会影响谁。

## 2. 前置知识

本讲是高级讲义，默认你已完成以下认知铺垫（对应依赖讲义）：

- **u5-l5（图像重建三讲收尾）**：你已经知道 `img_reconstruct_kern` 的插值段是全内核唯一的标量瓶颈，`m_img` 是跨 602 脉冲持久累加的成员缓冲，由 RTP `rtp_dump_img_in` 控制末脉冲才 dump。本讲会把这条「标量插值」直接对接到 future_work 的 *Dynamic Buffer Indexing* 条目。
- **u5-l1（Data Broadcast 内核）**：你已知道 `data_broadcast_kern` 把一整条 RC 距离压缩线（512 个 cfloat）扇出广播给全部 224 个重建内核。本讲会用它来量化「全量 RC 广播」的带宽开销。
- **u7-l2（三个分支对比）**：你已知道 `main` / `host_stride` / `pl_stride` 三者只在「输入侧预排序由谁做」上不同，而 `pl_stride` 当前受 `AIE_SWITCHES = 1` 限制。本讲会讲清这个限制的**工具链根因**和候选解法。
- **u8-l2（性能与功耗度量）**：你已知道上板度量的瓶颈是「Populating data buffers」的串行 CSV 解析（约 35 分钟），而 AIE 反投影本身只需数百毫秒。本讲讨论的算法优化针对的是那「数百毫秒」里的 AIE 算力短板，二者是不同层面的瓶颈，不要混淆。

此外需要两个基础术语：

- **ILP（Instruction-Level Parallelism，指令级并行）**：靠 VLIW 架构在单个时钟周期里发射多条指令，本质是「时间重叠」——让上一轮迭代还在算时，下一轮的加载已经开始。
- **SIMD（Single Instruction Multiple Data）**：靠向量寄存器用一条指令同时算多个元素，本质是「数据并行」。
- **DSP 引擎**：Versal 里独立于 AIE 阵列的、专门做规则算术（乘加、FFT）的硬核，适合把高度规则的变换从 AIE 卸载出来。

## 3. 本讲源码地图

本讲的核心「规格源码」是文档，对照阅读的「被优化对象」是 AIE 内核源码：

| 文件 | 作用 | 本讲用它做什么 |
|------|------|----------------|
| [doc/sections/future_work.tex](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex) | 「未来工作」清单，7 个小节 | 本讲的主线骨架，逐条对照源码 |
| [doc/sections/implementation.tex](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex) | 实现细节，含三分支与各内核步骤 | 取「插值 NOTE」「三分支」「后处理恒在 PL」等定论 |
| [doc/sections/versal_overview.tex](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/versal_overview.tex) | AIE 三级并行（ILP/SIMD/多核） | 说明 ILP 为何是「被低估的第三级」 |
| [design/aie/backprojection.cc](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc) | 三个 AIE 内核实现 | 定位插值标量循环、RC 广播、`chess_prepare_for_pipelining` |
| [design/aie/custom_kernels.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h) | `ImgReconstruct` 类与 `m_img` 声明 | 看 `m_img` 的大小与持久性 |
| [design/common.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h) | 三域共享的规模宏与物理常数 | 量化带宽、像素数、SAMPLES |

## 4. 核心概念与源码讲解

本讲按 future_work 的内在逻辑拆成四个最小模块：

- **4.1** 三级并行中的短板：ILP 优化空间（对应 *ILP Optimizations*）
- **4.2** 图像重建内核的三大算法优化机会（对应 *AI Engine Algorithm Optimizations*，本讲重心，承接实践任务）
- **4.3** `pl_stride` 多流限制与候选解法（对应 *DMA Stride Controller PL Kernel Improvements*）
- **4.4** 面向完整雷达系统的扩展：DSP、全孔径、stripmap、XD100（对应其余四节）

---

### 4.1 三级并行中的短板：ILP 优化空间

#### 4.1.1 概念说明

Versal 的 AI Engine 阵列同时提供**三级并行**，文档在 overview 里列得很清楚：

- **ILP**：靠 VLIW 架构，单周期发射多条指令（[versal_overview.tex:L25-L35](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/versal_overview.tex#L25-L35) 旁注：ILP 通过 VLIW 在单周期执行多条操作）。
- **SIMD**：靠向量寄存器，一条指令算多个元素。
- **Multicore**：靠 tile 阵列，最多 400 个 tile 并行。

本设计**重度依赖 SIMD 与多核，ILP 贡献较小**。implementation.tex 明确写道：当前实现「heavily emphasizes SIMD and multi-core parallelism」，ILP 只做了一部分，更多改动留作 future work（[implementation.tex:L41-L49](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex#L41-L49)）。换句话说：数据并行和空间并行已经被吃透，**时间维度的并行（流水）还有富余**。

#### 4.1.2 核心流程

ILP 优化的落地手段是给选定的 `for` 循环加 `chess_prepare_for_pipelining` 指令，告诉编译器「请把相邻迭代在时间上重叠起来」——上一轮的乘法还在执行时，下一轮的向量加载就已经发出。这正是 SIMD 与 ILP 的关键区别：

- **SIMD**：16 个像素的差分距离用 `aie::sub/mul_square/sqrt` 一组向量指令同时算完（数据并行）。
- **ILP**：即使每轮做的事已经向量化了，相邻两轮之间仍可以通过流水隐藏加载延迟（时间并行）。

future_work 还指出 PL 侧有对称手段：HLS 的 `#pragma HLS PIPELINE`（降低发起间隔 II）、`UNROLL`、`DATAFLOW`（[future_work.tex:L46-L63](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L46-L63)）。文档建议用 *Vitis Analyzer* 这类 profiling 工具去量化这些 ILP 优化对整体性能的实际影响，并找出更多机会——也就是说，**当前 ILP 的收益还没被严格度量过**。

#### 4.1.3 源码精读

`chess_prepare_for_pipelining` 在 `backprojection.cc` 中共出现 5 处，覆盖了几个热点循环：

```cpp
// data_broadcast_kern：RC 块拷贝
for(unsigned i=0; i < RC_SAMPLES/16; i++) chess_prepare_for_pipelining {
    *rc_out_iter++ = *rc_in_iter++;
}
```
见 [backprojection.cc:L53-L55](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L53-L55)——RC 广播的搬运循环已被流水化。

其余四处分别在：像素读入 [backprojection.cc:L79](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L79)、主重建循环 [backprojection.cc:L111](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L111)、插值标量循环 [backprojection.cc:L178](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L178)、图像 dump 循环 [backprojection.cc:L206](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L206)。这说明开发者已经「点名」了关键循环做流水，但 future_work 的观点是：**点的够不够、II 降到位没有，还缺一次系统性的 profiling 来回答**。

#### 4.1.4 代码实践

**实践目标**：在源码里建立「ILP 已覆盖面」的清单，为后续 profiling 圈定候选循环。

**操作步骤**：

1. 在 `design/aie/backprojection.cc` 中检索 `chess_prepare_for_pipelining`，列出全部 5 处的行号与所在函数。
2. 对每处循环，判断它体内是「向量运算」（如 4.1.3 的 RC 块拷贝、L111 主循环里的 `aie::sub/mul_square`）还是「标量运算」（如 L178 插值循环）。
3. 在 future_work 的 *ILP Optimizations* 段（[future_work.tex:L46-L63](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L46-L63)）旁注：哪几个循环最值得送进 Vitis Analyzer 看 II（initiation interval）。

**需要观察的现象**：标量循环（L178）即使加了流水指令，其 II 也可能受限于「串行依赖」而无法降到 1；向量循环（L111）则更可能受益。

**预期结果**：得到一张「行号 → 循环类型 → 是否已流水 → 是否值得进一步 profiling」的表格。**待本地验证**：具体 II 数值需在 Vitis Analyzer 中打开编译产物才能看到，本仓库源码本身不携带该数值。

#### 4.1.5 小练习与答案

**练习 1**：SIMD 和 ILP 都能让程序变快，本设计为什么说 ILP 是「被低估的第三级」？
> **答案**：因为前两级（SIMD 的数据并行、Multicore 的空间并行）已经被设计吃透——224 个内核各做 16 路向量运算；而第三级 ILP（时间流水）只在「部分」`for` 循环上用 `chess_prepare_for_pipelining` 点到，且收益未被 profiling 严格度量，故有富余。

**练习 2**：`chess_prepare_for_pipelining` 是写给 AIE 编译器的；PL 侧的等价手段是什么？
> **答案**：HLS 的 `#pragma HLS PIPELINE`（降发起间隔）、`UNROLL`（展开）、`DATAFLOW`（任务级并发），见 [future_work.tex:L59-L63](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L59-L63)。

---

### 4.2 图像重建内核的三大算法优化机会

> 对应 [future_work.tex:L161-L215](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L161-L215)（*AI Engine Algorithm Optimizations*）。这是本讲的重心，也是综合实践的素材来源。三个子项互相牵连，需要一起看。

#### 4.2.1 概念说明

future_work 在「AIE 算法优化」标题下点了三件事，本质上都是在问「**数据该怎么喂、怎么取，才能让 224 个重建内核少等、少搬、少卡**」：

1. **Dynamic Buffer Indexing for Interpolation（动态缓冲索引）**——当前最大算力瓶颈。插值要从 RC 缓冲里按「运行时才算出来」的下标取样本，下标不连续、无法在编译期确定，导致 AIE API 无法用向量 gather，只能退化成标量逐元素取。详见 [future_work.tex:L203-L215](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L203-L215)。
2. **Selective RC Sample Distribution（选择性 RC 分发）**——当前最大带宽浪费。一整条 512 样本的 RC 线被**全量广播**给每个重建内核，哪怕某核只用到其中一小段。详见 [future_work.tex:L185-L201](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L185-L201)。
3. **Internal Target Pixel Generation via RTP（用 RTP 内部生成目标像素）**——当前有一条「DDR→GMIO→包流→重建内核」的目标像素搬运通路；若改用 RTP 让内核自己生成像素，可省掉这条流。详见 [future_work.tex:L173-L183](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L173-L183)。

文档特别提醒这三项**互相牵制**：如果先做了第 3 项（像素在内核内部生成），主机就不再知道每个内核需要哪些 RC 段，会让第 2 项（选择性 RC）的协调变得更复杂。这正是「优化路线选择」要权衡的地方。

#### 4.2.2 核心流程

把三个子项放到一次内核调用的执行流程里看，最清楚：

```text
每脉冲每核的一次 img_reconstruct_kern 调用：
  ① 读包流，还原目标像素 X/Y/Z            ← 第3项想砍掉这条流（改 RTP 内部生成）
  ② SIMD：差分距离 → 索引 → 相位校正        ← 已向量化，不是瓶颈
  ③ 标量插值：按动态下标从 rc_in 取样本      ← 第1项：算力瓶颈，拖慢整段(含 m_img 累加)
  ④ 标量累加：m_img[...] += img            ← 与③同处一个标量循环，被③「传染」
  ⑤ 末脉冲 RTP=1 时 dump m_img
```

关键认知：**第 1 项（动态索引）和 `m_img` 累加循环是同一个循环**。下面 4.2.3 会用源码证实。所以「动态索引对 m_img 累加循环的影响」= 它迫使整个循环（取 RC + 插值 + 累加）全部标量化，本可 16 路并行的累加被拖成 16 次串行。

带宽侧（第 2 项）的核心流程则发生在**另一个内核** `data_broadcast_kern`：每个脉冲，它把当前 RC 线（512 cfloat）从局部存储以 AXI4-Stream burst 复制成 224 份，灌给每个重建内核的 `rc_in` 缓冲。每个内核都拿到**完整**的 512 样本，但插值时只触碰其中一小段下标窗口。

#### 4.2.3 源码精读

**（1）动态索引瓶颈——同一段循环里的插值与累加**

[backprojection.cc:L178-L185](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L178-L185) 是全内核唯一的标量循环：

```cpp
for(int px_idx=0; px_idx<16; px_idx++) chess_prepare_for_pipelining {
    // 动态下标 gather：high/low 索引运行时才算出，不连续
    rc_delta = rc_in_iter[high_idx_int_vec.get(px_idx)]
             - rc_in_iter[low_idx_int_vec.get(px_idx)];
    auto px_rc_delta = rc_delta*(float)(px_delta_idx_vec.get(px_idx));
    auto interp = px_rc_delta + rc_in_iter[low_idx_int_vec.get(px_idx)];

    auto img = interp * ph_corr_vec.get(px_idx);
    m_img[(px_seg_idx*16) + px_idx] += img;   // ← 累加也被迫标量化
}
```

读码要点：

- `rc_in_iter[high_idx_int_vec.get(px_idx)]` 是**逐元素、按下标随机访问** `rc_in` 缓冲（gather）。`high_idx_int_vec` / `low_idx_int_vec` 来自 [backprojection.cc:L145-L147](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L145-L147)，由差分距离除以距离分辨率得到，**每个像素不同、运行时才确定**。
- AIE 的向量 load 要求地址连续或编译期可定的步长，**不支持向量 gather**，所以这里只能 `.get(px_idx)` 一次取一个标量，循环 16 次。
- `m_img[(px_seg_idx*16) + px_idx] += img;` 的下标其实是线性可推的，**它本身并不阻碍向量化**；它之所以标量化，仅仅因为它和上面的 gather 同处一个循环，被「传染」了。这正是综合实践要评估的「动态索引对 m_img 累加循环的影响」。
- 这段循环嵌在外层 [backprojection.cc:L111](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L111) 的主循环里，每核每次调用执行 `SAMPLES/16` 轮，每轮 16 次。implementation.tex 用粗体 NOTE 点名了这个问题（[implementation.tex:L557-L569](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex#L557-L569)），原文称其为「a large performance hit」。

`m_img` 本身是 `ImgReconstruct` 类的成员，跨 602 脉冲持久存在，大小由整除约束保证（[custom_kernels.h:L41](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L41)）：

```cpp
alignas(aie::vector_decl_align) cfloat m_img[(PULSES*RC_SAMPLES)/IMG_SOLVERS];
```

**（2）全量 RC 广播——带宽侧的浪费**

[backprojection.cc:L40-L56](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L40-L56) 是广播内核。它从主机各收一份 slowtime 与 RC，重新发出后由 AIE 流交换网络组播给 224 个重建内核：

```cpp
void data_broadcast_kern(...) {
    ...
    writeincr(slowtime_out, readincr_v<4>(slowtime_in));   // slowtime 流式直通
    for(unsigned i=0; i < RC_SAMPLES/16; i++) chess_prepare_for_pipelining {
        *rc_out_iter++ = *rc_in_iter++;                    // RC 整条扇出广播
    }
}
```

每个重建内核因此每脉冲都收到**完整 512 个 cfloat** 的 RC 线，但插值（4.2.3-(1)）只用到 `low_idx`/`high_idx` 落到的那一小段。文档在 *Selective RC Sample Distribution*（[future_work.tex:L185-L201](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L185-L201)）里指出：插值所用 RC 下标「depend on the target pixels and slow-time data」，因此主机其实**可以预先算出**每个内核到底需要哪些 RC 段，只把需要的段分发下去。

**（3）选择性 RC 与内部 RTP 像素的张力**

future_work 给出两种选择性 RC 的实现思路：让 ARM 预算后改走 **Pixel Demux 内核**（而非 Data Broadcast）分发，或用**专门的 AIE 内核**做 RC 段分发（[future_work.tex:L193-L201](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L193-L201)）。但它紧接着警告：若先做了 *Internal Target Pixel Generation via RTP*（像素在内核内生成，[future_work.tex:L173-L183](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L173-L183)），主机就「不再知道每个内核需要哪些 RC 段」，协调复杂度上升。即第 2 项与第 3 项存在耦合，**不能无脑同时上**。

#### 4.2.4 代码实践

> 本节给出综合实践所需的「量化评估」骨架；完整的优先级路线写在第 5 节综合实践里。

**实践目标**：用源码里的常量，量化「动态索引」与「选择性 RC」分别影响哪一段、影响多大。

**操作步骤**：

1. 从 [common.h:L17-L38](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L17-L38) 取 `PULSES=602`、`RC_SAMPLES=512`、`IMG_SOLVERS=224`，算出每核像素数 `SAMPLES = 602×512/224 = 1376`。
2. **评估动态索引对 `m_img` 累加循环的影响**：确认 [backprojection.cc:L178-L185](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L178-L185) 里「gather RC」与「`m_img += img`」同处一个 16 次标量循环；结论是：理想情况下该循环本可向量化为 1 组向量运算（16 路），现状却被 gather 拖成 16 次串行，潜在加速比约 16×（仅此段）。
3. **评估选择性 RC 对 Data Broadcast 带宽的影响**：现状每脉冲广播 `224 × 512 cfloat × 8 B = 896 KiB`；若每个内核只需宽 `W` 个 RC 样本的窗口（`W` 取决于像素到内核的空间局部性），则降为 `224 × W × 8 B`。代入两个边界：`W=512`（像素跨满距离，无收益）与 `W=64`（局部聚集，约省 87.5%）。

**需要观察的现象**：步骤 2 里「16×」是这一段的渐近上限，实际还要扣掉地址计算与寄存器压力；步骤 3 里 `W` 的真实取值取决于 Pixel Demux 的像素分配是否空间聚集。

**预期结果**：得到两组数字——「插值段潜在加速比」与「RC 广播带宽节省比」，并意识到前者是确定的算法收益，后者依赖像素分配策略。**待本地验证**：精确的 `W` 分布需要统计每个内核实际触碰的 RC 下标范围，本仓库无现成脚本输出该统计。

#### 4.2.5 小练习与答案

**练习 1**：`m_img[(px_seg_idx*16) + px_idx] += img;` 的写地址是线性可推的，为什么这一句也变成了标量？
> **答案**：因为它和上面的 `rc_in_iter[high_idx_int_vec.get(px_idx)]` gather 同处 [backprojection.cc:L178-L185](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L178-L185) 这一个循环；AIE 不支持向量 gather，整个循环只能标量化，累加被「传染」。

**练习 2**：future_work 说选择性 RC 与「内部 RTP 生成像素」互相牵制，根因是什么？
> **答案**：选择性 RC 要主机预计算「每核需要哪些 RC 段」（[future_work.tex:L193-L201](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L193-L201)）；而内部 RTP 生成像素（[future_work.tex:L173-L183](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L173-L183)）会让像素归属在内核内才确定，主机就无从预知所需 RC 段，协调变复杂。

**练习 3**：为什么 AIE 的向量 load 无法直接服务于插值的 gather？
> **答案**：AIE 向量 load 要求地址连续或编译期可定的步长；而插值的 `high_idx`/`low_idx` 由运行时的目标像素与 slowtime 决定，逐像素不同且不连续，属于 gather 模式，当前 AIE API 不支持。

---

### 4.3 `pl_stride` 多流限制与候选解法

> 对应 [future_work.tex:L66-L130](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L66-L130)（*DMA Stride Controller PL Kernel Improvements*）。承接 u7-l2 的「三分支」结论。

#### 4.3.1 概念说明

回顾 u7-l2：`pl_stride` 分支多了一片 PL 硬件——DMA Stride Controller，用步进式取址（stride）在 DDR 里预排序目标像素，再把数据流式送进 AIE，从而让各重建内核能更早并行启动。但这个分支目前被**锁死在 `AIE_SWITCHES = 1`**，即只能驱动 1 个 bpCluster。

这条限制**不是算法问题，也不是仿真问题**——future_work 明确说仿真和 testbench 都确认 `AIE_SWITCHES > 1` 在功能上能跑（[future_work.tex:L100-L102](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L100-L102)）。真正的拦路虎是 **Vitis 工具链**：`system.cfg` 无法表达「数组化的 AXI4-Stream 端口」。

对照之下，`main` 分支的 PL 包路由器 `dma_pkt_router` 之所以能跑 `AIE_SWITCHES = 7`，是因为它给每个 PL 实例用**独立的命名端口**（`pl_stream_in`，每实例一个），不依赖数组化端口——这就是 u7-l2 所说「包路由器无此约束」的根因。

#### 4.3.2 核心流程

DMA Stride Controller 的顶层签名里，输出是一个**数组端口**（[future_work.tex:L88-L97](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L88-L97)）：

```cpp
int dma_stride_controller(
    ap_uint<64>* ddr_mem,
    hls::stream<ap_axiu<128, 0, 0, 0>> pl_stream_out[AIE_SWITCHES]  // ← 数组化端口
) {
    #pragma HLS INTERFACE axis port=pl_stream_out
    ...
}
```

问题链条：

```text
想支持 AIE_SWITCHES=N
  → 需要 pl_stream_out[N] 这样一个数组流端口
  → system.cfg 要描述这 N 条 PL→AIE 连接
  → 但 system.cfg 只认「显式、唯一命名」的流接口
  → 无法引用 pl_stream_out[AIE_SWITCHES] 这种参数化数组
  → 结果：pl_stride 只能 N=1
```

其后果是「只有 1 个 bpCluster 能被驱动」，AIE 并行度被压到 1/7，`pl_stride` 的潜力发挥不出来。

future_work 给出三条候选解法（[future_work.tex:L113-L123](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L113-L123)）：

1. **Kernel Prototyping（内核原型化）**：为目标 `AIE_SWITCHES` 的每个取值各写一个内核原型，把 `pl_stream_out_N` 端口**显式展开**成一个个命名端口。
2. **Kernel Replication（内核复制）**：把 Stride Controller 改成只输出单簇流，然后**实例化多份**内核，每簇一份——这正是 `dma_pkt_router` 已经在用的策略。
3. **Toolchain Integration（工具链集成）**：等更新的 Vitis 版本原生支持 `system.cfg` 里的数组化 AXI4-Stream 端口。

#### 4.3.3 源码精读

本节主要读文档与签名，因为 `pl_stride` 分支的 Stride Controller 内核**不在当前 `main` 分支仓库内**（u7-l2 已说明），其行为以 implementation.tex 的 [PL DMA Stride Controller Kernel](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex#L674-L767) 一节为准。

定论性陈述来自 implementation.tex：三分支只在「输入侧预排序由谁做」不同，而「**后处理恒在 PL**」是系统不变量（[implementation.tex:L145-L152](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex#L145-L152)）——无论输入侧谁排，输出侧都由 PL 包路由器按 `instance_id` 把 `pktmerge` 打乱的图像包重排回连续 DDR。这条不变量解释了为什么「Stride Controller 受限」只影响输入侧并行度，而不影响输出侧正确性。

对照 `main` 分支的包路由器（u6-l1 已读）：它用 `m_axi` + `axis` 单命名端口，7 个实例各写同一块 DDR 的不重叠区段，故能 `AIE_SWITCHES = 7`。这正是「Kernel Replication」思路的现成范例，也是 4.3.2 第 2 条解法最直接的依据。

#### 4.3.4 代码实践

**实践目标**：把 Stride Controller 的「数组端口」问题与包路由器的「命名端口」策略对照清楚，验证「Kernel Replication」解法的可行性来源。

**操作步骤**：

1. 读 [future_work.tex:L88-L97](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L88-L97)，记下 `pl_stream_out[AIE_SWITCHES]` 这个数组端口。
2. 回顾 u6-l1 / u7-l1 里 `dma_pkt_router` 的接口：它没有数组端口，而是「一个命名 `axis` 端口 + 多个实例」，并由 `system.cfg` 的 `nk=` 声明多个实例、`stream_connect=` 逐条命名连接。
3. 在纸上把「Kernel Replication」解法画出来：把 Stride Controller 改成单簇输出 `pl_stream_out`，再按 `AIE_SWITCHES` 实例化 N 份，每份一个命名端口，仿照包路由器的 `system.cfg` 写法。

**需要观察的现象**：两种策略在 `system.cfg` 里的表达差异——数组端口无法写，命名端口可以逐条 `stream_connect`。

**预期结果**：能用自己的话说明「为什么包路由器能跑 7 路、Stride Controller 只能跑 1 路」，并指出「Kernel Replication」是把前者已验证的策略搬到后者。本仓库不含 Stride Controller 源码，故代码级验证**待本地在 `pl_stride` 分支进行**。

#### 4.3.5 小练习与答案

**练习 1**：`pl_stride` 被锁在 `AIE_SWITCHES = 1`，这是算法限制还是工具链限制？
> **答案**：工具链限制。仿真已确认 `AIE_SWITCHES > 1` 功能正常（[future_work.tex:L100-L102](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L100-L102)），是 `system.cfg` 无法表达数组化 AXI4-Stream 端口所致。

**练习 2**：三条候选解法里，哪一条最贴近 `main` 分支包路由器的现有做法？
> **答案**：Kernel Replication（[future_work.tex:L117-L119](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L117-L119)）。包路由器正是「单命名端口 + 多实例」的策略，已在 `AIE_SWITCHES = 7` 下验证可行。

---

### 4.4 面向完整雷达系统的扩展：DSP、全孔径、stripmap、XD100

> 对应 future_work 的四个收尾小节。它们不再是「优化现有反投影」，而是「把反投影变成一个更完整的在轨雷达处理链」。

#### 4.4.1 概念说明

四个方向按「改哪、改多大」排序：

- **DSP 引擎做 FFT/iFFT 脉冲压缩**（[future_work.tex:L133-L158](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L133-L158)）：FFT/iFFT 高度规则、算术密集，适合塞进 PL 或 DSP 专用块，把 AIE 腾出来做反投影这种「数据相关」的复杂计算。早期开发时 FFT/iFFT 曾在 AIE 里实现并验证过，后来为了让核心反投影先跑通被移除。本设计的输入其实是 MATLAB 离线做完 iFFT（频域零填充调分辨率）后的时域相位历史数据；而在真实雷达链里，脉冲压缩（FFT+iFFT）必须上板、在实时数据通路上完成。
- **全 360° 孔径聚束**（[future_work.tex:L7-L26](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L7-L26)）：当前只处理约 5° 方位覆盖（约 602 脉冲，1°≈117 脉冲），是「聚焦演示」而非完整重建。扩到 360° 主要是 **ARM 侧**改动——分段顺序喂数据、处理中间同步；硬件架构预计能支撑连续孔径，但分段投递与同步机制尚未实现。
- **stripmap 模式**（[future_work.tex:L29-L43](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L29-L43)）：聚束模式是波束持续指向同一区域、分辨率高但幅面小；stripmap 则是天线指向固定、方位波束恒定，沿航迹生成连续条带图像、幅面宽。实现 stripmap 需要适配「动态场景几何下的连续目标像素输入」，当前架构可行，但**需要新的 stripmap 几何数据集**。
- **与 AMD XD100 参考设计对比**（[future_work.tex:L218-L240](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L218-L240)）：2025 年 8 月 AMD 发布 XD100 参考设计教程，内含一节 *Back-Projection for SAR on AI Engines*，用的是**同一个 GOTCHA 数据集**。它展示了多速率调度、动态范围管理、以及用 Vitis DSP Library 向量化的 `sin()/cos()/sqrt()`。两种设计思路不同，值得在吞吐、tile 利用率、能效上做横向对比——但因时间经费所限未做。

#### 4.4.2 核心流程

把这四项映射到「触哪个域、改什么边界」：

| 扩展方向 | 主要触及域 | 关键改动 / 前置条件 |
|----------|-----------|---------------------|
| DSP 脉冲压缩 | DSP / PL | 把 FFT/iFFT 从离线 MATLAB 搬上板，加入实时数据通路（[future_work.tex:L154-L158](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L154-L158)） |
| 全 360° 聚束 | ARM | 分段顺序投递多段孔径 + 中间同步（[future_work.tex:L19-L26](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L19-L26)） |
| stripmap | AIE / ARM | 适配动态几何的连续像素输入；需新数据集（[future_work.tex:L38-L43](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L38-L43)） |
| XD100 对比 | （评估，非改码） | 同算法同数据集，对比吞吐/tile 利用率/能效（[future_work.tex:L233-L240](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L233-L240)） |

注意 XD100 一项里的一个**技术彩蛋**：XD100 用 Vitis DSP Library 做了向量化的 `sin/cos/sqrt`。这恰好呼应本设计在 u5-l4 用 `sincos_complex` + 2π 折叠手写的相位校正、以及 u5-l3 的 `aie::sqrt` 差分距离——两套设计在这些「数学内函数」上的取舍，正是值得横向对比的细节之一。

#### 4.4.3 源码精读

本节几乎不读新代码，而是把 future_work 的定论与已读源码挂钩：

- **DSP 卸载的动机**与 `ph_corr_coef`、`sincos_complex` 的位置一致：相位校正的三角函数发生在 [backprojection.cc:L90](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L90)（`ph_corr_coef = (4*PI*MIN_FREQ)/C`）与 [backprojection.cc:L168](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L168)（`sincos_complex`）。future_work 说 FFT/iFFT 早期在 AIE 实现过、后被移除（[future_work.tex:L137-L145](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L137-L145)），因此当前 `main` 仓库里看不到它们——这是「移除」而非「从未有过」。
- **全孔径的 ARM 侧属性**：和 u3-l5 读过的 `bp()` 投递循环一致——逐脉冲经 GMIO 投递、RTP 末脉冲置 1。扩到 360° 意味着把「602 脉冲一组」扩展为「多组分段投递 + 中间同步」，落点仍在 ARM 的编排逻辑，而非 AIE 内核。
- **stripmap 的数据集前提**：本设计输入依赖 GOTCHA 的聚束几何（slowtime 的天线方位角序列，见 u3-l4），stripmap 需要不同的航迹几何，因此卡在「缺数据集」而非「架构不行」（[future_work.tex:L40-L43](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L40-L43)）。

#### 4.4.4 代码实践

**实践目标**：把四个扩展方向逐个「定位」到本课程已读的源码/讲义，验证自己理解了「改哪里」。

**操作步骤**：

1. 对「DSP 脉冲压缩」，在 [backprojection.cc](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc) 里标出当前所有「三角/平方根」数学调用（`ph_corr_coef` L90、`sincos_complex` L168、`aie::sqrt` L133），说明这些是 XD100 用 DSP Library 向量化的同类操作。
2. 对「全 360° 聚束」，回到 u3-l1/u3-l5 的主机五阶段流程，指出「分段投递 + 中间同步」会插在哪一阶段（运行图 / 反投影 `bp()`）。
3. 对「stripmap」，回到 u3-l4 的 `genTargetPixels()` 与方位角解卷绕，指出 stripmap 的「固定指向 + 恒定波束」会如何改变 `unwrap()` 的输入序列。
4. 对「XD100 对比」，列出至少 3 个可对比维度：`sin/cos/sqrt` 的实现、tile 利用率、多速率调度。

**需要观察的现象**：四个方向触达的域各不相同（DSP/PL、ARM、AIE+ARM、评估），印证本设计的异构分工让「扩展」天然分域。

**预期结果**：一张「扩展方向 → 触及域 → 对应已读源码/讲义」的映射表。本节为源码阅读型实践，无运行步骤，**无需本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么把 FFT/iFFT 卸载到 DSP 引擎「更准确地代表实际雷达处理」？
> **答案**：真实雷达链里脉冲压缩（FFT+iFFT）必须在板上实时数据通路完成（[future_work.tex:L154-L158](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L154-L158)）；本设计靠 MATLAB 离线做 iFFT，是评估期的简化。卸载到 DSP 既还原真实流程，又平衡了 AIE/DSP 负载。

**练习 2**：扩到全 360° 聚束，主要是改 AIE 内核还是改 ARM？
> **答案**：主要改 ARM。future_work 说硬件架构预计支持连续孔径，缺的是「分段顺序投递 + 中间同步」机制（[future_work.tex:L19-L26](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L19-L26)），属主机编排逻辑。

**练习 3**：实现 stripmap 模式的拦路问题是架构不行，还是缺数据集？
> **答案**：缺数据集。文档说当前架构对 stripmap 可行，但需要提供合适输入几何与采集参数的新数据集（[future_work.tex:L40-L43](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L40-L43)）。

---

## 5. 综合实践

> 这是本讲的核心实践，完成规格里要求的那条任务：从 future_work 中挑出「**动态 buffer 索引**」与「**选择性 RC 分发**」两项，分别评估它们对 **`m_img` 累加循环**和 **Data Broadcast 带宽**的影响，并写出一条你认为最值得优先做的改进路线。

### 5.1 实践目标

把 4.2 的量化骨架发展成一份「优化决策建议」，能用数字和源码证据回答：「先做哪一项、为什么、收益与风险各是什么」。

### 5.2 操作步骤

**第一步：评估「动态 buffer 索引」对 `m_img` 累加循环的影响**

1. 打开 [backprojection.cc:L178-L185](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L178-L185)。
2. 确认事实：`rc_in_iter[high_idx_int_vec.get(px_idx)]` 这一行 gather，与 `m_img[(px_seg_idx*16) + px_idx] += img;` 这一行累加，**在同一个 16 次标量循环里**。
3. 写出结论：因为 AIE 不支持向量 gather，整个循环被强制标量化；`m_img` 累加本可与插值一起向量化（16 路并行），现状却被拖成 16 次串行。该段潜在加速比上界约为：

\[
\text{加速比上界} \approx \frac{16\ \text{路（理想向量）}}{1\ \text{路（当前标量）}} = 16\times
\]

   且这段循环每核每次调用执行 `SAMPLES/16 = 1376/16 = 86` 轮、每轮 16 次，每脉冲都要跑一遍，是名副其实的「每脉冲每核都要付的税」。把结论与 future_work 的原话（「a large performance hit」「for every pulse」，[future_work.tex:L203-L215](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L203-L215)）对齐。

**第二步：评估「选择性 RC 分发」对 Data Broadcast 带宽的影响**

1. 打开 [backprojection.cc:L40-L56](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L40-L56)，确认现状：每个脉冲，512 个 cfloat 的 RC 线被广播给全部 224 个重建内核。
2. 算现状带宽（每脉冲）：

\[
B_{\text{现状}} = 224 \times 512\ \text{cfloat} \times 8\ \text{B} = 917\,504\ \text{B} \approx 896\ \text{KiB}
\]

3. 设每个内核实际只需宽 `W` 个 RC 样本的窗口（`W` 取决于像素到内核的空间局部性），则选择性分发后：

\[
B_{\text{选择}} = 224 \times W \times 8\ \text{B},\qquad
\text{节省比} = 1 - \frac{W}{512}
\]

4. 代入两个边界说明敏感性：`W = 512`（像素跨满距离维，节省 0%，最差）；`W = 64`（局部聚集，节省约 87.5%，最好）。把结论与 future_work 的描述（[future_work.tex:L185-L201](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L185-L201)）对齐。

**第三步：写出优先级路线**

综合两点，给出你的建议并论证。下面给出一个**参考路线**（你可以同意或反驳，但要用源码与数字支撑）：

> **优先做「选择性 RC 分发」里的「主机预计算 + 经 Pixel Demux 分发」子方案，且暂缓「内部 RTP 生成像素」。**
>
> 理由：
> 1. **收益确定性与实现成本**：动态索引的理想 16× 受限于地址计算与寄存器压力，且需要等「AIE API 未来支持向量 gather」（[future_work.tex:L212-L215](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L212-L215)），不完全由本团队掌控；选择性 RC 在 `W` 较小时即时省带宽，且实现路径落在团队可控的主机 + Pixel Demux 一侧。
> 2. **附带红利**：选择性 RC 只下发需要的 RC 段，每个内核的 `rc_in` 缓冲占用变小，局部存储腾出来，可做更密的 tile 布局（[future_work.tex:L188-L191](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L188-L191)）。
> 3. **耦合控制**：刻意**暂缓**内部 RTP 生成像素，正是为了让主机继续「知道每核需要哪些 RC 段」，避免 [future_work.tex:L197-L201](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/future_work.tex#L197-L201) 警告的协调复杂度。
> 4. **后续**：等选择性 RC 稳定后，再用 Vitis Analyzer 度量插值段是否仍是瓶颈；若是，再考虑动态索引的重构或跟进 AIE API 升级。

### 5.3 需要观察的现象

- 第一步的「16×」是渐近上界，实际收益受寄存器/地址计算侵蚀；
- 第二步的节省比高度依赖 `W`，而 `W` 取决于 Pixel Demux 的像素分配是否空间聚集——若像素到内核是「跨距离维散布」，`W` 接近 512，节省很小。

### 5.4 预期结果

产出一份一页纸的「优化决策建议」，含：两张量化表（插值段加速上界、RC 带宽节省曲线）、一条带论证的优先级路线。精确的 `W` 分布与插值段真实 II **待本地验证**（前者需统计每核 RC 下标范围，后者需 Vitis Analyzer）。

---

## 6. 本讲小结

- 本设计的算力短板集中在 **AIE 插值段**：[backprojection.cc:L178-L185](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L178-L185) 因动态 gather 退化为标量，把本可 16 路并行的 `m_img` 累加也一并「传染」成串行（对应 *Dynamic Buffer Indexing*）。
- 本设计的带宽短板集中在 **Data Broadcast 全量 RC 广播**：每个脉冲把 512 cfloat 扇出给 224 核，约 896 KiB/脉冲，多数核只用一小段（对应 *Selective RC Sample Distribution*）。
- future_work 三项 AIE 算法优化**互相耦合**：内部 RTP 生成像素会让选择性 RC 的主机预计算失去依据，故需排序取舍。
- `pl_stride` 的 `AIE_SWITCHES = 1` 限制是**工具链根因**（`system.cfg` 不认数组化 AXI4-Stream 端口），不是算法/仿真问题；包路由器因「单命名端口 + 多实例」而无此约束，恰好示范了「Kernel Replication」解法。
- 三级并行里 **ILP 是被低估的第三级**：SIMD 与多核已吃透，`chess_prepare_for_pipelining` 只点了部分循环，收益尚待 Vitis Analyzer 系统度量。
- 扩展方向分域清晰：DSP 卸载 FFT/iFFT（DSP/PL）、全 360° 聚束（ARM 编排）、stripmap（需新数据集）、XD100 横向对比（同算法同数据集，可借鉴其 DSP Library 向量化 `sin/cos/sqrt`）。

## 7. 下一步学习建议

本讲是项目学习手册的最后一篇，没有「下一讲」。建议你把本讲的「优化清单」当作二次开发的入口，按下面的顺序动手：

1. **先做一次系统 profiling**：按 u8-l1/u8-l2 的方法，用 `aiesim_profile`（`--profile --dump-vcd`）与 Vitis Analyzer 打开编译产物，**用数字**回答本讲留下的「待本地验证」——插值段的真实 II、每核 RC 下标范围 `W` 的分布。有了这两组数据，4.2 的优化路线选择才有实证依据。
2. **挑一条最小改动先落地**：综合实践给出的「选择性 RC 经 Pixel Demux 分发」是一个主机 + 单个 AIE 内核改动可控的切入点；可在缩小规模（`AIE_SWITCHES=1`、`PULSES` 取小值）下先验证 RC 段选取的正确性，对照 aiesim 的 `output_img.csv` 与 MATLAB 参考。
3. **横向对照 XD100**：用同一个 GOTCHA 数据集跑 AMD XD100 的反投影教程，记录其 tile 利用率与 `sin/cos/sqrt` 实现，作为本设计优化的参照系。
4. **回到源码**：若要追动态索引这条线，重读 [backprojection.cc:L62-L214](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L62-L214) 整个 `img_reconstruct_kern`，并跟踪 AMD 后续 AIE API 是否加入了向量 gather 支持——这是解开插值标量瓶颈的关键外部依赖。

至此，你已从「SAR 反投影是什么」一路读到「它还能走到哪里」，具备了对这个三域异构设计做二次开发与性能权衡的完整地图。
