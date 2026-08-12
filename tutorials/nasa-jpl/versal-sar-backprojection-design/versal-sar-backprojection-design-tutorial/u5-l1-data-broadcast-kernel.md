# Data Broadcast 内核

## 1. 本讲目标

本讲聚焦反投影图里唯一一个不属于"解复用/重建"计算簇的内核——`data_broadcast_kern`（数据广播内核）。它是整张图的数据分发枢纽。学完后你应该能够：

- 说清 `data_broadcast_kern` 的四个端口（slowtime 的入/出流、RC 的入/出缓冲）各自搬什么数据，以及它的输出扇出到多少个重建内核。
- 区分 AIE 的两套数据搬运原语：流的 `readincr` / `writeincr` / `readincr_v<4>`，与缓冲的 `aie::begin_vector<16>` 迭代器，并能解释为什么 RC 用缓冲、slowtime 用流。
- 讲清"广播触发原理"：一次写出如何变成 224 份拷贝，以及为什么这个内核必须存在。

本讲只讲"数据怎么进、怎么搬出去"，不碰重建算法本身（相位校正、插值、累加留给 u5-l3 ~ u5-l5）。

## 2. 前置知识

本讲建立在前面几讲已建立的概念上，这里只做一句话提醒，不重复展开：

- **buffer 与 stream 端口（u2-l2）**：buffer 是 tile 局部存储里的一整块窗口（可随机寻址，框架默认双缓冲）；stream 是逐 beat 的连续流（只能顺序读写）。包流 pktstream 是带包头的流。
- **GMIO 投递（u2-l2、u3-l5）**：主机用 `gm2aie_nb` 把 DDR 里的数据经 NoC 非阻塞地推进 AIE 图的 GMIO 端口。
- **ADF 广播与 single_buffer（u4-l3）**：把一个输出端口 `connect` 到多个输入端口就会触发广播（数据复制）；广播连接上常加 `single_buffer` 关掉默认双缓冲以省局部存储。
- **Kahn 数据驱动执行（u2-l3）**：内核不是被时钟节拍调用，而是"所有输入就绪才 fire（点火执行）"。
- **关键规模宏（u1-l4）**：`PULSES=602`、`RC_SAMPLES=512`、`BC_ELEMENTS=4`、`AIE_SWITCHES=7`、`IMG_SOLVERS_PER_SWITCH=32`、`IMG_SOLVERS=224`。

一个需要先记住的事实：本设计共有 `IMG_SOLVERS = AIE_SWITCHES × IMG_SOLVERS_PER_SWITCH = 7 × 32 = 224` 个图像重建内核，它们**每个脉冲都需要同一份 slowtime 和同一份 RC**。这正是广播内核存在的根本理由。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `design/aie/backprojection.cc` | `data_broadcast_kern` 的实现（本讲主角），也含重建内核，本讲只引用它对 RC 的随机访问来佐证"RC 必须是 buffer"。 |
| `design/aie/custom_kernels.h` | `data_broadcast_kern` 的声明，可看到四个端口的精确类型签名。 |
| `design/aie/graph.h` | 顶层 `BackProjectionGraph` 里如何实例化 `data_bc_km`，以及把它的两个输出 `connect` 到全部 224 个重建内核的广播扇出。 |
| `design/aie/graph.cpp` | 仿真 `main` 里主机如何用 `gm2aie_nb` 把 slowtime / RC 推进广播内核的 GMIO 端口（驱动广播的源头）。 |
| `design/common.h` | 上述规模宏的定义来源。 |

## 4. 核心概念与源码讲解

### 4.1 data_broadcast_kern：单一广播源的职责与数据通路

#### 4.1.1 概念说明

设想没有广播内核：主机要让 224 个重建内核每个都拿到同一份 slowtime（天线 X/Y/Z 与参考距离）和同一份 RC（距离压缩样本），就得通过 GMIO 把这两份数据**分别发 224 次**。这既浪费 NoC 带宽，也远超 GMIO 通道数量的上限。

`data_broadcast_kern` 就是为此设计的"单一数据源 / 扇出点"：它从主机那里**只接收一次** slowtime 与 RC，然后在自己的输出端口上重新发出，由 ADF 框架 + AIE 流交换网络（stream switch）把这一次发出**复制**到全部 224 个重建内核。主机只需投递一次，224 个内核就都能拿到。

它有四个端口，两进两出，分别对应两类数据：

- `slowtime_in` / `slowtime_out`：slowtime，走 **stream**（流）。
- `rc_in` / `rc_out`：RC（距离压缩样本），走 **buffer**（缓冲窗口）。

#### 4.1.2 核心流程

每个脉冲，广播内核做两件事，然后由流交换网络把输出复制出去：

```
输入就绪（Kahn 触发）
   │
   ├─ slowtime_in (stream, 4 个 float) ──readincr_v<4>──► writeincr ──► slowtime_out (stream)
   │                                                        │
   │                                              流交换网络广播到 224 个重建内核的 in[0]
   │
   └─ rc_in (buffer, 512 个 cfloat) ──begin_vector<16> 逐块拷贝──► rc_out (buffer)
                                                                 │
                                                       缓冲扇出到 224 个重建内核的 in[1]
```

内核每被触发一次，处理**一个脉冲**的 slowtime（4 float）和一个脉冲的 RC（512 cfloat）。整个图跑 `PULSES=602` 次，所以广播内核一共 fire 602 次，每次都把当脉冲的 slowtime/RC 重新扇出给 224 个重建内核。

#### 4.1.3 源码精读

先看函数签名（四个端口的类型一目了然）：

[design/aie/custom_kernels.h:L17-L20](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L17-L20) —— 声明 `data_broadcast_kern`：slowtime 入/出都是 `stream<float>`，RC 入/出都是 `buffer<cfloat, extents<RC_SAMPLES>>`。

再看实现骨架：

[design/aie/backprojection.cc:L40-L56](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L40-L56) —— `data_broadcast_kern` 全部实现，只有十几行：先建两个 RC 缓冲的向量迭代器，再用 `writeincr` 把 slowtime 转出，最后循环拷贝 RC。

扇出关系在顶层图里。顶层图的广播扇出循环把 `data_bc_km` 的两个输出连到所有 224 个重建内核，并对每个输入端口加 `single_buffer`：

[design/aie/graph.h:L177-L184](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L177-L184) —— `out[0]`（slowtime）和 `out[1]`（RC）各 `connect` 到 `AIE_SWITCHES × IMG_SOLVERS_PER_SWITCH = 224` 个重建内核，共 `2 × 224 = 448` 条 connect；每条都配 `single_buffer`。

实例化语句：

[design/aie/graph.h:L160-L160](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L160-L160) —— `data_bc_km = kernel::create(data_broadcast_kern);`，整个顶层图只有这一个广播内核。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认两个输出端口的扇出数量。
2. **步骤**：打开 `design/aie/graph.h` 第 177–184 行的双层循环；外层 `j` 遍历 `AIE_SWITCHES=7`，内层 `i` 遍历 `IMG_SOLVERS_PER_SWITCH=32`。
3. **观察**：循环体里 `connect(data_bc_km.out[0], ...)` 与 `connect(data_bc_km.out[1], ...)` 各执行 `7×32=224` 次。
4. **预期结果**：`slowtime_out`（流）扇出到 224 个重建内核的 `in[0]`；`rc_out`（缓冲）扇出到 224 个重建内核的 `in[1]`。两个端口的扇出数相同，都是 224。
5. 待本地验证（可选）：若你能拿到编译后的图报告，可搜索 `data_bc_km` 的输出端口，确认其 fanout 为 224。

#### 4.1.5 小练习与答案

**练习 1**：如果把广播内核删掉、让主机直接喂数据给 224 个重建内核，最直接的代价是什么？

**答案**：主机需要 224 条 GMIO 通道并重复投递同一份数据 224 次，远超 GMIO 通道资源上限，且把 NoC 带宽浪费在复制上。广播内核把"复制"这件事下沉到 AIE 片内流交换网络完成。

**练习 2**：广播内核的输出扇出为什么不能改成"一个 pktstream 包流用 pkt_id 分发"？

**答案**：包流分发是给"每个目的地拿**不同**数据子集"用的（如目标像素的 1→32 解复用，见 u5-l2）。slowtime/RC 是"每个目的地拿**同一份**数据"，正是广播（buffer + connect 扇出 + single_buffer）的适用场景（u4-l2 的判据）。

---

### 4.2 流与缓冲的两套搬运原语：readincr_v<4> 与 begin_vector<16>

#### 4.2.1 概念说明

`data_broadcast_kern` 同时演示了 AIE 里两套截然不同的数据搬运原语：

- **流原语**：`readincr` / `readincr_v<N>`（从输入流读）、`writeincr`（向输出流写）。流是顺序的、逐 beat 的，读写必须按顺序，不能随机跳。
- **缓冲原语**：`aie::begin_vector<N>(buf)` 返回一个迭代器，每次解引用拿到一个 `N` 元素的向量，可对缓冲窗口做**向量化块拷贝**。缓冲在局部存储里，可随机寻址。

内核用流原语搬 slowtime，用缓冲原语搬 RC——这一选择不是随意的，而是由数据本身的大小和消费方式决定的（见 4.3）。

#### 4.2.2 核心流程

slowtime 路径（流）：

```
readincr_v<4>(slowtime_in)   // 一次向量读，取 4 个 float（= BC_ELEMENTS）
        │
        ▼
writeincr(slowtime_out, vec) // 一次向量写，把 4 个 float 推到输出流
```

RC 路径（缓冲）：

```
rc_in_iter  = aie::begin_vector<16>(rc_in)   // 输入缓冲的 16 元素向量迭代器
rc_out_iter = aie::begin_vector<16>(rc_out)  // 输出缓冲的 16 元素向量迭代器
for i in 0 .. RC_SAMPLES/16 - 1:             // 循环 512/16 = 32 次
    *rc_out_iter++ = *rc_in_iter++           // 每次拷贝 16 个 cfloat
```

为什么是 16？`aie::vector<cfloat, 16>` 是一个对 AIE 向量寄存器友好的宽度，能让单次拷贝走 SIMD/向量化通路，而不是逐元素标量搬。16 是 AIE 向量操作里常用的"一次搬一大组"的粒度。

为什么是 4？`readincr_v<4>` 一次取 4 个 float，正好等于 `BC_ELEMENTS=4`——一条 slowtime 记录的全部内容（天线 X、Y、Z 与参考距离 ref_range）。一次向量读就把整条记录拿完。

#### 4.2.3 源码精读

slowtime 的"读一条、写一条"：

[design/aie/backprojection.cc:L49-L50](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L49-L50) —— `writeincr(slowtime_out, readincr_v<4>(slowtime_in));`：用 `readincr_v<4>` 从输入流一次读 4 个 float，直接 `writeincr` 推到输出流。注意 slowtime 全程不落地到局部存储缓冲，纯流式直通。

RC 的向量化块拷贝：

[design/aie/backprojection.cc:L46-L47](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L46-L47) —— 建立输入/输出缓冲的 `aie::begin_vector<16>` 迭代器。

[design/aie/backprojection.cc:L52-L55](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L52-L55) —— `for(unsigned i=0; i < RC_SAMPLES/16; i++)` 循环，每次 `*rc_out_iter++ = *rc_in_iter++;` 拷贝 16 个 cfloat，并带 `chess_prepare_for_pipelining` 提示编译器把循环流水化。

RC 必须是 buffer 的铁证——重建内核对它做**随机索引**：

[design/aie/backprojection.cc:L178-L185](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L178-L185) —— `rc_in_iter[high_idx_int_vec.get(px_idx)]` 与 `rc_in_iter[low_idx_int_vec.get(px_idx)]`：下标随像素的差分距离动态变化，是随机访问。流（只能顺序读）根本做不到这件事，所以 RC 端到端必须是可随机寻址的 buffer。

#### 4.2.4 代码实践（源码阅读型 + 计算）

1. **目标**：解释 `RC_SAMPLES/16` 这个循环次数的由来。
2. **步骤**：
   - 查 `common.h` 得 `RC_SAMPLES=512`。
   - 在 `backprojection.cc:53` 看循环上界 `RC_SAMPLES/16`。
3. **计算**：

\[
\text{循环次数} = \frac{\texttt{RC\_SAMPLES}}{16} = \frac{512}{16} = 32
\]

   每次拷贝 16 个 cfloat，故一次内核调用搬的 RC 总量为：

\[
32 \times 16 = 512 = \texttt{RC\_SAMPLES}
\]

   正好搬完整条距离压缩线。
4. **预期结果**：循环次数 32 来源于"把 512 个 RC 样本按 16 个一组切分"，目的是让每次拷贝对齐 AIE 的向量宽度，走向量化通路。
5. **额外观察**：若把 `readincr_v<4>` 改成 `readincr_v<2>`，slowtime 一次只读 2 个 float，需要读两次才能拿完一条 4 元素记录；这会破坏"一条记录一次读"的简洁性，也降低流利用率。

#### 4.2.5 小练习与答案

**练习 1**：`readincr_v<4>` 里的 4 由哪个宏决定？为什么刚好是它？

**答案**：由 `BC_ELEMENTS=4` 决定。slowtime 一条记录就是 `BC_ELEMENTS` 个 float（天线 X/Y/Z + ref_range），`readincr_v<4>` 一次向量读恰好取完整条记录。

**练习 2**：把 RC 的搬运改成"逐元素 `for` 循环、每次拷 1 个 cfloat"会有什么后果？

**答案**：功能上等价，但性能下降：丧失向量化，每次只搬 1 个元素，无法填满 AIE 向量通路；`begin_vector<16>` 的意义就是把 16 个 cfloat 打成一组一次性搬走，配合 `chess_prepare_for_pipelining` 让循环流水起来。

---

### 4.3 广播触发原理：为什么 slowtime 要经 stream 转出

#### 4.3.1 概念说明

这是本讲的核心问题：为什么广播内核要把 slowtime 从 stream 转出（`writeincr(slowtime_out, ...)`），而不是像 RC 那样用 buffer？答案分两层。

**第一层——广播靠谁来完成？** 在 ADF 里，"一个输出端口 connect 到多个输入端口"就是广播。物理上，这次复制由 AIE 的**流交换网络（stream switch）**完成：源 tile 把数据送上流交换网络，网络在逐跳传递时把数据复制到所有目的 tile。所以"广播"本质上是流交换网络的一次组播。

**第二层——为什么 slowtime 选 stream、RC 选 buffer？** 这由数据在**重建内核一侧的消费方式**决定：

- **slowtime** 只有 4 个 float，且是**顺序**消费（重建内核一次性读完整条记录，见 4.1）。它又小又顺序，最适合用流。内核用 `writeincr` 把它推上输出流，流交换网络自然地把它组播到 224 个目的 tile。目的地一侧，ADF 自动插入一个"流→缓冲"的 DMA，把流里的 4 个 float 写进重建内核的 `BC_ELEMENTS` 窗口（即 `in[0]`，它被声明为 `input_buffer<float, BC_ELEMENTS>`）。
- **RC** 有 512 个 cfloat，且重建内核对它做**随机索引**（`rc_in_iter[high_idx]`，见 4.2.3 源码精读）。随机访问要求它落在可寻址的局部存储里，所以 RC 端到端必须是 buffer。广播内核仍把它广播出去——ADF 的 buffer→buffer 扇出同样由流交换网络把 RC 窗口复现到每个目的 tile 的局部存储。

所以"把 slowtime 经 stream 转出以利用广播"的确切含义是：**流输出是注入流交换网络、让一次写出被复制成 224 份的最自然方式；而 slowtime 又小又顺序，正好匹配流的特性**。RC 则因为需要随机访问，只能留在缓冲里走缓冲扇出。

#### 4.3.2 核心流程

把"触发"串起来，从主机到 224 个内核：

```
主机 gm2aie_nb(slowtime) ──► gmio_in_st ──► data_bc_km.in[0] (stream)
主机 gm2aie_nb(rc)       ──► gmio_in_rc ──► data_bc_km.in[1] (buffer)
                                            │
                                  两类输入都就绪 → 内核 fire（Kahn 触发）
                                            │
              ┌─────────────────────────────┴─────────────────────────────┐
              ▼                                                            ▼
   writeincr(slowtime_out, vec)                              begin_vector<16> 块拷到 rc_out
              │                                                            │
        out[0] 流                                                       out[1] 缓冲
              │                                                            │
              └──────► 流交换网络把单次写出复制成 224 份 ◄──────────────────┘
                                            │
                      224 个重建内核的 in[0]（slowtime 窗口）与 in[1]（RC 窗口）都被填好
```

关键点：内核 fire 的触发条件是两类输入都就绪（u2-l3 的 Kahn 数据驱动模型）；而"一次 fire → 224 份拷贝"靠的是流交换网络的组播能力。主机的 `gm2aie_nb` 投递是这条链的源头——投递一次，全图 224 个内核就都能拿到当脉冲的 slowtime/RC。

#### 4.3.3 源码精读

主机侧的源头投递（仿真 `main`，与真实主机 `bp()` 的投递模式一致，见 u3-l5）：

[design/aie/graph.cpp:L188-L193](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L188-L193) —— `gmio_in_st.gm2aie_nb(...)` 把整段 slowtime（`PULSES*BC_ELEMENTS` 个 float）一次性推进；`gmio_in_rc.gm2aie_nb(rc_array + pulse_idx*RC_SAMPLES, ...)` 则在脉冲循环里**逐脉冲**投递一条 RC 线。这正是驱动广播内核 fire 的输入来源。

GMIO → 广播内核的输入连接：

[design/aie/graph.h:L171-L172](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L171-L172) —— `connect(gmio_in_st.out[0], data_bc_km.in[0])`（slowtime，GMIO→流）、`connect(gmio_in_rc.out[0], data_bc_km.in[1])`（RC，GMIO→缓冲）。

广播内核把 slowtime 推上输出流（触发流交换网络组播的那一行）：

[design/aie/backprojection.cc:L49-L50](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L49-L50) —— `writeincr(slowtime_out, readincr_v<4>(slowtime_in));`：这一行 `writeincr` 就是"把 slowtime 经 stream 转出"的落点；它写到 `output_stream`，后续由流交换网络复制。

#### 4.3.4 代码实践（源码阅读型 + 推理）

1. **目标**：跟踪一次广播的完整触发链，并解释"为什么是 stream"。
2. **步骤**：
   - 从 `graph.cpp:188` 的 `gmio_in_st.gm2aie_nb` 出发，沿 `graph.h:171` 的 `connect` 进入 `data_bc_km.in[0]`（stream）。
   - 在 `backprojection.cc:50` 看到 `writeincr` 把 slowtime 写到 `out[0]`（stream）。
   - 沿 `graph.h:179` 的 `connect(data_bc_km.out[0], bpCluster[j].img_rec_km[i].in[0])` 到达重建内核的 `in[0]`，它是 `input_buffer<float, BC_ELEMENTS>`（见 `custom_kernels.h:26`）。
3. **观察与推理**：输出端是 `output_stream<float>`，输入端却是 `input_buffer<float, BC_ELEMENTS>`——这是一条**流→缓冲**连接。ADF 在目的 tile 自动插入 DMA，把流里的 4 个 float 写进局部存储窗口。这条连接能成立，正说明流是广播的载体，而缓冲只是目的地一侧的落地形态。
4. **预期结果**：你能讲清"主机投递一次 → 广播内核 fire → 流交换网络把 slowtime 复制成 224 份 → 每个重建内核的 slowtime 窗口都被填好"。
5. **对照 RC**：RC 在两端都是 buffer（`graph.h:180` 的 `connect(data_bc_km.out[1], ...in[1])`，两端皆 `buffer<cfloat, RC_SAMPLES>`），因为重建内核要随机索引它（`backprojection.cc:179`）。RC 的广播走缓冲扇出，同样由流交换网络复现到各目的 tile 的局部存储。
6. 待本地验证（可选）：在 aiesim 的 `--dump-vcd` 波形里，可观察到 `data_bc_km` 的输出流被多个 tile 同时读取（组播特征）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 slowtime 也改成 buffer 出（`output_buffer<float, BC_ELEMENTS>`），还能广播到 224 个内核吗？

**答案**：技术上 ADF 也能对 buffer 做扇出广播（RC 就是这么做的），所以"能不能"不是问题。但 slowtime 只有 4 个 float、又顺序消费，用 buffer 反而要为它分配并管理局部存储窗口，开销大于收益；用流直通（`writeincr`）是最轻量的注入广播网络的方式。所以这是"合不合适"而非"能不能"。

**练习 2**：广播内核的 fire 由什么触发？

**答案**：由 Kahn 数据驱动模型触发：`slowtime_in`（流）有数据且 `rc_in`（缓冲）填满时，两类输入都就绪，内核才 fire。源头是主机 `gmio_in_st` / `gmio_in_rc` 的 `gm2aie_nb` 投递（`graph.cpp:188-193`）。

**练习 3**：为什么 RC 不能像 slowtime 那样用流直通？

**答案**：因为重建内核对 RC 做随机索引（`rc_in_iter[high_idx]` / `[low_idx]`，`backprojection.cc:179`），流只能顺序读，无法随机跳到任意下标。RC 必须落在可随机寻址的局部存储缓冲里。

---

## 5. 综合实践

把本讲两个核心事实串起来，完成下面的综合任务。

**任务**：对照源码，回答两个问题并画出数据通路。

1. **扇出数量**：`data_broadcast_kern` 的两个输出端口——`slowtime_out`（流）和 `rc_out`（缓冲）——分别扇出到多少个重建内核？
   - 打开 [design/aie/graph.h:L177-L184](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L177-L184)。
   - 双层循环 `j ∈ [0, AIE_SWITCHES=7)`、`i ∈ [0, IMG_SOLVERS_PER_SWITCH=32)`。
   - **答案**：两个端口各扇出到 `7 × 32 = 224` 个重建内核（`out[0]`→`in[0]`，`out[1]`→`in[1]`），共 `2 × 224 = 448` 条 connect。两者扇出数相同。

2. **循环次数由来**：解释 `RC_SAMPLES/16` 这个循环次数。
   - 打开 [design/aie/backprojection.cc:L52-L55](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L52-L55)，配合 `common.h` 的 `RC_SAMPLES=512`。
   - **答案**：`512 / 16 = 32` 次。每次用 `begin_vector<16>` 迭代器拷贝 16 个 cfloat，32 次共 `32 × 16 = 512 = RC_SAMPLES`，正好搬完一条距离压缩线。16 是为了对齐 AIE 向量宽度、走向量化通路。

3. **画图**：画一张数据通路图，从主机 `gm2aie_nb` 开始，经 `gmio_in_st` / `gmio_in_rc` → `data_bc_km`（标注 slowtime 走流、RC 走缓冲）→ 流交换网络（标注"复制成 224 份"）→ 224 个重建内核的 `in[0]` / `in[1]`。在图上标出两处关键代码行：`writeincr`（流直通）与 `begin_vector<16>` 块拷贝。

**预期结果**：你能不查资料地说出"两个端口都扇出到 224 个内核"、"RC_SAMPLES/16=32 来源于把 512 样本按 16 个一组切分"，并能解释 slowtime 用流、RC 用缓冲的根本原因是消费方式（顺序 vs 随机访问）。

## 6. 本讲小结

- `data_broadcast_kern` 是整张反投影图的**数据分发枢纽**：从主机接收一次 slowtime 与 RC，重新发出后由 AIE 流交换网络广播给全部 224 个重建内核，免去主机重复投递 224 次。
- 它有四个端口：slowtime 入/出为 **stream**，RC 入/出为 **buffer**；两个输出端口各扇出到 224 个重建内核（共 448 条 connect，均配 `single_buffer`）。
- slowtime 用流原语搬运：`readincr_v<4>` 一次向量读 4 个 float（= `BC_ELEMENTS`），`writeincr` 推到输出流——流是注入流交换网络、让一次写出被复制成 224 份的最自然方式。
- RC 用缓冲原语搬运：`aie::begin_vector<16>` 迭代器每次拷 16 个 cfloat，循环 `RC_SAMPLES/16 = 512/16 = 32` 次搬完整条距离压缩线；之所以必须用缓冲，是因为重建内核要对 RC 做随机索引（`rc_in_iter[high_idx]`）。
- 广播的触发链：主机 `gm2aie_nb` 投递 → 两类输入就绪触发内核 fire（Kahn 数据驱动）→ 流交换网络把输出复制到 224 个目的地。
- 类型选择（流 vs 缓冲）由数据在重建内核一侧的**消费方式**决定：小且顺序的 slowtime 用流，大且需随机访问的 RC 用缓冲。

## 7. 下一步学习建议

广播内核只负责"把 slowtime 和 RC 喂给所有重建内核"。接下来按数据通路继续往下读：

- **u5-l2 Pixel Demux 内核**：讲目标像素流如何被 `px_demux_kern` 打包头、按 `pkt_id` 分发到 32 个重建内核（包流分发，与广播的"同一份数据给所有人"形成对照）。
- **u5-l3 图像重建（一）**：进入 `img_reconstruct_kern`，看它如何消费本讲广播来的 slowtime（提取天线位置 x_ant/y_ant/z_ant 与 r0）和 RC（做差分距离与索引），这是理解"为什么 RC 必须是 buffer"的算法侧佐证。
- 复习 **u4-l3**：本讲的扇出与 `single_buffer` 在那里已建立判据，可对照巩固"广播 vs 包交换"的区分。
