# u14-l5 条件 rescaling 与 writeback

## 1. 本讲目标

上一讲（u14-l4）结束时，PV MMA 已经能把 \( P\,V \) 累加进 TMEM 中的输出累加器 `O`，并且 `p_o_rescale` 屏障串联起了"P 前段就绪"与"O 槽可用"两重条件。本讲接过其中最模糊的一半——**WG2 到底对 O 做了什么、什么时候做**——并送 `O` 走完它在 FA4 中的最后一段路。具体回答三个问题：

1. **条件 rescaling 何时触发**：softmax 换指数参考时，旧 `O` 为什么、在什么阈值条件下必须先乘上 `acc_scale = exp2(delta)` 才能继续累加。
2. **校正循环怎么执行**：WG2 如何通过 SMEM mailbox 拿到 per-row `acc_scale`，如何用"阈值 + `any_sync`"两级过滤尽量跳过 TMEM→寄存器→TMEM 数据路径，又如何在跳过数据路径时**不**跳过同步协议；两个 Q stage 如何交替复用同一套 mailbox。
3. **O 如何离开芯片**：epilogue 的 `O / row_sum` → fp16 → SMEM → TMA store → GMEM 链路，以及 causal 与 non-causal 两条路径的分工差异。

学完后你应能独立写出校正循环与交替 stage 协议的伪代码，并准确说出一次 K/V 迭代中 `O` 的全部数据搬运次数——包括其中**零次**与 GMEM 交互这个容易答错的事实。

## 2. 前置知识

本讲是 FA4 单元第五讲，直接建立在以下已建立的认知上（只引用结论，不重复推导）：

- **在线 softmax 状态三元组（u14-l1 / u14-l3）**：每个 query 行跨 K/V 块保留 `row_max`（指数参考 \( r_i \)）、`row_sum`（\( \ell_i \)）、`O`（\( o_i \)）；换参考的判据是 \( \delta=(r_{\mathrm{old}}-r_c)\cdot\text{scale\_log2}\le 0 \)，换算因子 \( a_{\text{scale}}=2^\delta \)。u14-l3 已手算过数值例，本讲直接使用这些公式。
- **O 的 TMEM 落点（u14-l4）**：`O_region[SMEM_PIPE_DEPTH_Q + i_q, :, :]`，两个 Q stage 的累加器 O0/O1 分别独占物理列 `[256,384)` 与 `[384,512)`；`SMEM_PIPE_DEPTH_Q=2`。
- **p_o_rescale = 256 次到达（u14-l4）**：softmax 战组 128 次 + WG2 128 次，一次 wait 同时证明"P 前 `K_SPLIT` 列就绪"与"O 槽可初始化/累加"。
- **屏障类型学（u8-l1 / u14-l2）**：`MBarrier` 靠线程到达计数；`TCGen05Bar` 靠 `tcgen05.commit` 挂接的硬件完成通知；**硬件命名屏障（named barrier）没有显式 phase 参数**，靠参与者数量复用——本讲的 mailbox 就靠它。
- **tcgen05.ld/st 纪律（u7-l4）**：TMEM store 是异步的，写完必须 `tcgen05.wait::st` 才算落地。
- **TMEM lane 访问窗口（u7-l3）**：warpgroup 内编号 \( w \) 的 warp 只能访问 TLain \( \in [32w, 32w+31] \)——这解释了本讲"每个 WG2 warp 恰好负责 32 行"的分工。
- **FA4 角色（u14-l2）**：WG0/WG1 跑两个 Q stage 的 softmax，WG3 的三个 warp 分别发 TMA load、两类 MMA、TMA store，WG2 做校正与 non-causal epilogue，其每线程寄存器上限被 `setmaxnreg` 压到 64。

一个先写在这里的对照（本讲会反复出现）：`row_sum` 与 `O` 同为"旧状态"，但 **`row_sum` 活在 softmax 战组的寄存器里**，softmax 顺手就能乘 `acc_scale`；**`O` 活在 TMEM 里**，softmax 够不着它——这个不对称就是"校正路径"存在的全部原因，正文一句话点破：[chapter_flash_attention/index.md:716](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L716)。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲使用方式 |
|---|---|---|
| `chapter_flash_attention/index.md` | FA4 章正文（节选自 tirx-kernels 的 `flash_attention4.py`） | 精读 *Rescaling and Writeback*（L712–788，本讲主战场）、*Passing Per-Row State from Softmax to WG2*（L642–665，mailbox 协议）、算法推导（L54–102、L155、L169）、数据流与阶段表（L175–198）、角色表（L204–223）、读码约定与屏障总表（L272–311）、时间线（L667–710）、softmax 侧 `row_sum` 更新（L462–472） |
| `img/scripts/gen_flash_attention_barrier_flow.py` | 生成书中 FA4 屏障交接图的 matplotlib 脚本 | 其中 `gen_softmax_correction`（L161–232）画出 mailbox 的 named-ready/empty-return 生命周期，是本讲协议的图形化事实来源 |
| `img/scripts/gen_flash_attention_pipeline.py` | 生成书中 FA4 流水线时间线图 | WG2 一行的四个事件（pre-release / rescale O0 / rescale O1 / normalize，L139–147）是校正循环在时间轴上的投影 |

与 u14-l4 相同的提醒：本仓库只含教材正文与图表脚本，内核源码在 [tirx-kernels 仓库的 `flash_attention4.py`](https://github.com/mlc-ai/tirx-kernels/blob/5be39749e7dfd2c4bdae9b4d396f8ec35af07126/tirx_kernels/attention/flash_attention4.py)（见 [chapter_flash_attention/index.md:270](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L270)）。

## 4. 核心概念与源码讲解

### 4.1 条件 rescaling：旧 O 何时必须换尺度

#### 4.1.1 概念说明

跨 K/V 块保留的每行状态是三元组 \( (r_i,\ \ell_i,\ o_i) \)，正文称之为 `row_max` / `row_sum` / `O`：[chapter_flash_attention/index.md:92-96](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L92-L96)。基本在线 softmax 每遇到更大的行最大值就把参考顶上去，于是**每次**换参考都必须先把旧状态换算到新尺度：

\[ a_{\text{scale}} = e^{(r_{\mathrm{old}}-r_c)/\sqrt d} = 2^{\delta},\qquad \delta=(r_{\mathrm{old}}-r_c)\,\text{scale\_log2}\le 0 \]

（推导见 [chapter_flash_attention/index.md:78-90](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L78-L90)；u14-l3 已做数值例。）

FA4 的观察是：\( \ell_i \) 和 \( o_i \) 都是**未归一化**的中间量——最后会除以 `row_sum` 抵消掉任何公共尺度。所以参考不必每块都更新，只要尺度差不至于让指数溢出。书中阈值取 \( \tau=\log_2(256)=8 \)：当 \( -\delta=8 \) 时保留旧参考意味着当前块最大的未归一化权重可达 \( 2^8=256 \)，fp32 表示毫无压力；于是 \( \delta\ge-8 \) 就**不换参考**（`acc_scale=1`），只有 \( \delta<-8 \) 才换参考并触发 rescale：[chapter_flash_attention/index.md:76](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L76)。这带来三种情况（[chapter_flash_attention/index.md:98-102](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L98-L102)）：

| 情况 | 判据 | `new_ref` | `acc_scale` | 旧 O 需要更新吗 |
|---|---|---|---|---|
| 首 K/V 块 | `is_first` | `candidate_max` | 1 | 不存在旧 O，直接初始化 |
| 尺度差在阈值内 | `delta >= -8` | `row_max_old`（保留） | 1 | **否**（这就是"条件"） |
| 尺度差超阈值 | `delta < -8` | `candidate_max` | `exp2(delta)` | **是**（乘 `acc_scale`） |

两点必须强调：

- **这是性能优化，不是数学近似**。延迟 rescaling 不改变最终结果——每个累积权重都相对同一个 \( r_i \) 表示，归一化时公共因子消去（正文的 LSE 推导从同一性质出发，见 4.3.1）。
- **`acc_scale` 有两个消费者**。softmax 用它本地更新寄存器里的 `row_sum`；WG2 用它重缩放 TMEM 里的 `O`。读码约定表对此写得很明确：`acc_scale` 是"per-row scale computed by softmax and used in two places"——[chapter_flash_attention/index.md:282](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L282)。

#### 4.1.2 核心流程

softmax 侧的决策（u14-l3 已精读过代码）浓缩为：

```text
candidate_max = max(row_max_old, rowmax(S))
if is_first:      new_ref = candidate_max; acc_scale = 1
else:
    delta = (row_max_old - candidate_max) * scale_log2     # delta <= 0
    if delta >= -8: new_ref = row_max_old; acc_scale = 1    # 保留旧参考
    else:           new_ref = candidate_max; acc_scale = exp2(delta)
```

随后 `acc_scale` 兵分两路：

```text
acc_scale ──> softmax 自己: row_sum = row_sum * acc_scale + rowsum(P)   （寄存器内，零额外搬运）
        └──> 写入 SMEM mailbox ──> WG2: O = O * acc_scale               （TMEM→RF→TMEM，仅当需要）
```

章首伪代码用 `all(acc_scale == 1)` 表达"何时可以跳过对 O 的 rescaling"，并特别注明：**真实内核把这个测试拆到 WG2 的每个 warp 各自负责的 32 行上分别进行**——[chapter_flash_attention/index.md:155](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L155)。这句预告正是 4.2 的主题。

#### 4.1.3 源码精读

**softmax 侧的消费（`row_sum` 更新）**。伪代码中 `O = O * acc_scale[:,None] + block_O` 的"O 半边"发生在 WG2，而"row_sum 半边"留在 softmax 战组内、且时序上排在 mailbox 归还之后：[chapter_flash_attention/index.md:462-472](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L462-L472)

```python
softmax_corr.empty.wait(wg_id, phase_q)
phase_q ^= 1
if is_first:
    Tx.sum(row_sum, s_chunk_buf)
else:
    row_sum[0] = row_sum[0] * acc_scale
    Tx.sum(row_sum, s_chunk_buf, accum=True)
```

读法：`softmax_corr.empty.wait` 等 WG2 归还 mailbox（详见 4.2），`phase_q ^= 1` 是 u8-l2 相位翻转规则在这个 MBarrier 流水线上的实例；然后才 `row_sum * acc_scale` 再累加 `rowsum(P)`。fp32 的 `P` 值一直留在 `s_chunk_buf` 寄存器里等这一步用——softmax 不需要为 `row_sum` 做任何显式数据搬运。

**数据路径总述**。正文在算法一节的末尾提前给出了校正与回写的两条物理路径：[chapter_flash_attention/index.md:169](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L169)——"当指数参考变化时，旧 O 从 TMEM 读出、在寄存器中 rescale、写回 TMEM，然后下一个 PV MMA 才累加进去"；以及收尾路径（L183，见 4.3）。

**读码约定表中的三个名字**（本讲代码段的局部词汇表）：[chapter_flash_attention/index.md:279-282](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L279-L282)——`should_rescale`（per-row 标志：旧 O 是否必须先 rescale）、`rescale_threshold`（当前为 8.0）、`acc_scale`（两处消费）。

#### 4.1.4 代码实践

**实践目标**：用数值感受"两级判据"——阈值在**行**级裁剪，`any_sync` 在 **warp** 级裁剪（后者见 4.2，这里先做行级）。

**操作步骤**（数值模拟，纯 Python，无需 GPU）：

1. 写一个模拟脚本（示例代码，非项目原有代码）：随机生成若干行分数，按书中判据算每行的 `acc_scale`，统计为 1 的比例：

```python
import math, random

rescale_threshold = 8.0
scale_log2 = 1.0            # 任意合法值即可, 只影响 delta 的数值
random.seed(0)

skip = trig = 0
for trial in range(10000):
    row_max_old = random.uniform(-2, 2)
    m_block = row_max_old + random.uniform(-12, 12)   # 当前块行最大值
    r_c = max(row_max_old, m_block)
    delta = (row_max_old - r_c) * scale_log2           # delta <= 0
    if delta >= -rescale_threshold:
        acc_scale = 1.0                                # 保留旧参考
    else:
        acc_scale = 2.0 ** delta                       # exp2(delta) < 1/256
    skip += (acc_scale == 1.0); trig += (acc_scale < 1.0)

print(f"acc_scale==1 的行: {skip}, 触发 rescale 的行: {trig}")
```

2. 把 `m_block` 的扰动范围改成 `random.uniform(-4, 4)` 再跑一次，观察触发比例如何变化。

**需要观察的现象**：分数漂移越平缓（相邻 K/V 块的行最大值差距越小），触发 rescale 的行越少；极端情况下趋近 0。

**预期结果**：`uniform(-12,12)` 时相当一部分行触发（约一半，因为 \( r_c > r_{\text{old}}+8/\text{scale\_log2} \) 的概率不低）；`uniform(-4,4)` 时几乎为 0——此时阈值完全吸收了尺度漂移，WG2 的数据路径整段闲置。真实 attention 的分数块间漂移通常平缓，这正是该优化的收益来源。确定性算术 + 固定随机种子，可本地复现；具体比例数值以你本地输出为准（本讲未代跑）。

#### 4.1.5 小练习与答案

**练习 1**：`delta = -5` 与 `delta = -10` 时，`acc_scale` 分别是多少（`scale_log2` 已含在 delta 中）？各走哪个分支？

**答案**：\( \delta=-5 \)：\( \delta\ge-8 \)，走"保留旧参考"分支，`acc_scale` 精确置 1（不计算 exp2）；\( \delta=-10 \)：\( \delta<-8 \)，`acc_scale = exp2(-10) = 2^{-10} = 1/1024 \)，旧 `row_sum` 与 `O` 都要乘它。

**练习 2**：`acc_scale == 1` 时 softmax 仍然执行 `row_sum[0] = row_sum[0] * acc_scale`。这个乘法可以省略吗？

**答案**：数学上可以（乘 1 是恒等），工程上正文伪代码保留统一路径以简化控制流；对 `O` 而言这个"是否为 1"的判断是**性能开关**（决定是否走 TMEM 往返），对 `row_sum` 只是寄存器内一次乘法，省不省都无伤大雅——两处消费的代价完全不对称，这正是 4.2 两级过滤只对 O 侧做的原因。

**练习 3**：为什么 `row_sum` 的 rescale 和 `O` 的 rescale 发生在不同 warpgroup？

**答案**：数据所在地不同。`row_sum` 是 softmax 战组的寄存器私有状态，softmax 线程顺手即可更新；`O` 驻留 TMEM，PV MMA 的累加器必须保持在 MMA 可读的位置上，对它的读-改-写需要走 `tcgen05.ld`/TMEM store 通路并经 WG2 执行——正文 L716 对此有原句。

### 4.2 校正循环：WG2 的数据路径、两级过滤与 mailbox

#### 4.2.1 概念说明

校正（correction）= "对 TMEM 中旧 O 的 rescale"。执行者是 WG2 全体 128 线程（u14-l2 的角色表：WG2 负责两个 stage 的校正 + non-causal epilogue，[chapter_flash_attention/index.md:206-208](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L206-L208)）。它面临两个问题：

1. **acc_scale 怎么从 softmax 传到 WG2**？两个 warpgroup 不共享寄存器。FA4 论文的方案是用空闲 TMEM 传校正统计量；**当前 TIRx 实现改用 SMEM**：把 per-row `acc_scale` 写进 SMEM 缓冲 `sScale`，就绪信号用硬件命名屏障，归还信号用 `softmax_corr.empty`——正文明确说明这个 mailbox 是 TIRx 特有的 SMEM 路径而非论文的 TMEM 路径：[chapter_flash_attention/index.md:266](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L266)。每个 Q stage 一个可复用槽。
2. **怎么少搬数据**？答案是两级过滤器（正文原话："Conditional rescaling therefore acts as a two-level filter"，[chapter_flash_attention/index.md:752](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L752)）：
   - **第一级（行级，阈值测试）**：softmax 的 `delta >= -8` 判据已经让很多行的 `acc_scale` 恰为 1；
   - **第二级（warp 级，`any_sync`）**：WG2 每个 warp 检查自己 32 行的 `should_rescale` 标志，**全部为 0 才整段跳过** TMEM→寄存器→TMEM 数据路径；只要有任何一行需要，该 warp 就处理自己这 32 行的 stripe（scale 为 1 的行只是乘 1）。

关键的纪律是：**跳过数据路径不跳过同步协议**（[chapter_flash_attention/index.md:750](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L750)）——每 warp 照常向 `p_o_rescale` 与 `softmax_corr.empty` 贡献到达，否则 PV MMA 永远等不齐 256 次到达、mailbox 也永远回不到 softmax 手里。

#### 4.2.2 核心流程

**mailbox 六步协议**（named-ready / empty-return 的生产者-消费者对，正文 [chapter_flash_attention/index.md:648-655](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L648-L655)）：

```text
softmax 侧 (WG0 或 WG1, stage = wg_id)            WG2 侧
─────────────────────────────────────────────────────────────────
1. wait softmax_corr.empty[wg_id, phase_q]   ←──  (上一轮) arrive 归还槽
2. sScale[wg_id] = per-row acc_scale
3. ptx_bar_arrive(stage wg_id 的 named barrier)
                                              4. ptx_bar_sync 加入同名屏障, 读 sScale[wg_id]
                                                 5. 检查/更新 O (两级过滤), arrive softmax_corr.empty
6. 下一轮复用该槽
```

**校正循环的稳态时序**（端到端五步，正文 [chapter_flash_attention/index.md:762-768](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L762-L768)）：

```text
1. softmax 把 scale 值写入 SMEM
2. WG2 加入该 stage 的 statistics named barrier
3. 每个 WG2 warp 检查自己的 32 行, 只在需要时更新 TMEM 中的 O
4. WG2 完成对 p_o_rescale 与 softmax_corr.empty 的到达（无论数据路径是否运行）
5. WG3 的 PV MMA 现在可以消费 P 并累加进 rescale 后的 O
```

**首块特判（pre-release）**：主循环之前 TMEM 里没有旧 O，WG2 立即向两个 `p_o_rescale` 槽贡献到达，让首批 PV MMA 以 `accum=false` 直接初始化 O0/O1——时间线图最左侧的 `pre-release O0/O1` 事件，[chapter_flash_attention/index.md:708](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L708)。

**交替 stage 协议**：WG2 处理完 stage `i_q` 后，`softmax_corr.empty.arrive(1 - i_q)` 归还的是**另一个** stage 的 mailbox 槽，保持 WG0/WG1 固定的交替顺序（[chapter_flash_attention/index.md:661](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L661)）。

**分工辨析**（易混点，正文专门强调，[chapter_flash_attention/index.md:663](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L663)）：`softmax_corr.empty` 只推进 **mailbox 协议**；`p_o_rescale` 才向 PV MMA 证明"P 就绪且 O 可用"。图脚本的"What it does not prove"面板把这一点画得非常直白：named barrier + `softmax_corr.empty` **不能**证明 P 已写入 TMEM、O 已 rescale、或任一段 PV MMA 可以开始——[img/scripts/gen_flash_attention_barrier_flow.py:212-229](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_flash_attention_barrier_flow.py#L212-L229)。

#### 4.2.3 源码精读

**校正数据路径的核心代码**（正文 [chapter_flash_attention/index.md:718-731](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L718-L731)）：

```python
RESCALE_TILE = T.meta_var(16)
o_row = T.wg_reg_tile(RESCALE_TILE)
Tx.wg.copy_async(
    o_row,
    O_region[SMEM_PIPE_DEPTH_Q + i_q, :, d_start : d_start + RESCALE_TILE],
)
Tx.wg.mul(o_row, o_row, acc_scale)
Tx.wg.copy_async(
    O_region[SMEM_PIPE_DEPTH_Q + i_q, :, d_start : d_start + RESCALE_TILE],
    o_row,
)
T.ptx.tcgen05.wait.st()
```

逐行读法：

- `O_region[SMEM_PIPE_DEPTH_Q + i_q, :, d_start : d_start+16]`：当前 Q stage 的累加器，列方向取 `[d_start, d_start+16)` 一段（`RESCALE_TILE=16`）；`HEAD_DIM=128`，所以一段 16 列的读-乘-写要循环 8 次才覆盖整行（`d_start = 0, 16, …, 112`——正文代码只展示了带 `d_start` 参数的一段，循环结构为推导）。
- 三个 `Tx.wg.*` 构成完整的"读→乘→写"：`copy_async`（`tcgen05.ld`，TMEM→寄存器）、`mul`（寄存器乘 `acc_scale`）、`copy_async`（反向，TMEM store）。写后跟一句 `tcgen05.wait::st()`——u7-l4 的异步纪律：TMEM store 落地前不能让后续 MMA 读到半新半旧的 O。
- **为什么每个 warp 恰好管 32 行**？TMEM 访问窗口规则（u7-l3）：warpgroup 内 warp \( w \) 只能访问 lane \( [32w, 32w+31] \)。O 是 128 行，WG2 的 4 个 warp 各读自己的 32-lane 窗口，正好铺满且互不重叠——行级分工是硬件访问窗口的直接投影，正文 L733 的"32-row stripe"由此而来。

**两级过滤的控制流**（正文 [chapter_flash_attention/index.md:737-748](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L737-L748)）：

```python
should_rescale = T.Select(acc_scale < T.float32(1.0), 1, 0)
any_needs_rescale = T.ptx.any_sync(0xFFFFFFFF, should_rescale)

if any_needs_rescale != 0:
    # This warp: TMEM -> registers -> multiply -> TMEM
    ...

# The correction loop returns the other Q stage in its alternating protocol.
p_o_rescale.arrive(i_q)
softmax_corr.empty.arrive(1 - i_q)
```

读法：每个 lane 从自己那一行的 `acc_scale` 生成 0/1 标志（`acc_scale < 1.0` 等价于"这一行换过参考"，因为保留旧参考时 acc_scale 精确为 1）；`any_sync(0xFFFFFFFF, …)` 在全 warp 32 个 lane 间做规约（掩码全 1）；`if` 是 warp 一致分支（32 线程锁步，`any_needs_rescale` 对全 warp 相同，不会发散）。注意**两道 arrive 在 `if` 之外**——这是"跳过数据路径不跳过同步"的代码形态。

正文的 Tile primitive 四要素框给校正下了完整定义：[chapter_flash_attention/index.md:756-760](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L756-L760)——Scope: WG2，每 warp 独立检查并处理自己的 32 行；Layout: O in TMEM → 寄存器 → O in TMEM；Dispatch: `tcgen05.ld` 读、TMEM store 写、中间寄存器乘；Handoff: 加入 statistics named barrier，arrive `p_o_rescale`（→ PV MMA）与 `softmax_corr.empty`（→ softmax）。

**mailbox 的实现细节**三条（正文 [chapter_flash_attention/index.md:657-661](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L657-L661)）：

- `GQA_RATIO != 1` 时每个 Q stage 用一道 **256 线程**命名屏障（一个 softmax 战组 128 + WG2 128）配对；`GQA_RATIO == 1` 时改用四道 **64 线程**屏障配对相应的 32 线程 warp。命名屏障没有 phase 参数，靠参与者计数复用；`softmax_corr.empty` 则是有相位的 `MBarrier` 流水线。
- **首块仍然同步一次**：没有旧 O、不需要 `acc_scale`，但 softmax 与 WG2 照样走一遍 mailbox 交接并归还槽，让后续迭代的相位保持对齐（L659）。
- non-causal 的**最后一次**交接复用同一机制携带最终 `row_sum`（epilogue 用，见 4.3）；causal 路径的 epilogue 在 WG0/WG1 做，省掉这次往返（L221-223）。

**图形佐证**：mailbox 交接图 `img/flash_attention_softmax_correction.png` 由 `gen_softmax_correction` 生成，脚本里五个状态框（slot 空 → softmax 写 acc_scale/row_sum → named barrier arrive → bar.sync WG2 读 → softmax_corr.empty）与紫色回环箭头（empty 返回 softmax）一一对应协议六步：[img/scripts/gen_flash_attention_barrier_flow.py:179-191](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_flash_attention_barrier_flow.py#L179-L191)。时间线图中 WG2 一行的四个事件（pre-release O0/O1 → rescale O0 if needed → rescale O1 if needed → normalize O0/O1）则把校正循环钉在了时间轴上：[img/scripts/gen_flash_attention_pipeline.py:139-147](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_flash_attention_pipeline.py#L139-L147)。

#### 4.2.4 代码实践

**实践目标**：写出 WG2 校正循环的完整伪代码（含首块 pre-release 与两个 Q stage 的交替），这是本讲主实践的第 2 步，此处先独立完成。

**操作步骤**（源码推演，无需 GPU）：

1. 重读 [chapter_flash_attention/index.md:712-768](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L712-L768) 与 [chapter_flash_attention/index.md:642-665](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L642-L665)。
2. 合并成 WG2 视角的伪代码（示例代码，非项目原有代码；参考结构如下，补全所有 `…`）：

```python
# WG2, non-causal 路径 (每 warp 负责自己的 32 行; i_q 为当前 Q stage)

# 主循环前: pre-release —— 没有旧 O, 直接放行首批 PV MMA
for i_q in (0, 1):
    p_o_rescale.arrive(i_q)

for n_block in K/V 块序列:                    # K/V 倒序流入
    for i_q in (0, 1):                        # 两个 Q stage 交替
        ptx_bar_sync(i_q 的 statistics named barrier)   # 等 softmax 写好 mailbox
        acc_scale[0:32] = sScale[i_q][本 warp 的 32 行]  # 从 SMEM 读 per-row scale

        should = [s < 1.0 for s in acc_scale]
        if any_sync(0xFFFFFFFF, should) != 0:
            for d_start in range(0, 128, 16):           # RESCALE_TILE=16, 共 8 段
                o_row = tcgen05.ld(O[2+i_q, 本warp行, d_start:+16])
                o_row *= acc_scale
                tcgen05.st(O[2+i_q, 本warp行, d_start:+16], o_row)
            tcgen05.wait.st()

        p_o_rescale.arrive(i_q)              # 放行 PV MMA 第一段 (O 槽就绪)
        softmax_corr.empty.arrive(1 - i_q)   # 归还"另一个"stage 的 mailbox
```

3. 自查三处：(a) 两道 arrive 是否在 `if` 之外；(b) pre-release 是否发生在主循环前；(c) `softmax_corr.empty.arrive` 的参数是否为 `1 - i_q`。

**需要观察的现象**：无运行现象；检验标准是你的伪代码能否逐句在正文找到出处（六步协议 L648-655、五步时序 L762-768、控制流 L737-748、pre-release L708、交替 L661）。

**预期结果**：与上面参考结构等价；特别地，把 `arrive(1 - i_q)` 误写成 `arrive(i_q)` 是最常见的错误——它会让 mailbox 归还错对象，softmax 侧的 `softmax_corr.empty.wait(wg_id, …)` 永远等不到自己的槽（待本地推演验证：试画出两轮迭代的 arrive/wait 配对表，确认错位发生在第几轮）。

#### 4.2.5 小练习与答案

**练习 1**：`any_sync` 的掩码为什么是 `0xFFFFFFFF`？

**答案**：WG2 内做判定的是单个 warp 的 32 个 lane，掩码全 1 表示把 warp 内**全部** 32 行的 `should_rescale` 都纳入规约——漏掉任何一行都可能让"有行需要 rescale"被误判为"全不需要"，导致该 warp 跳过数据路径、O 留在旧尺度上，产生静默数值错误。

**练习 2**：某 warp 的 32 行中 31 行 `acc_scale=1`、仅 1 行需要 rescale。数据路径如何处理这 32 行？

**答案**：`any_sync` 返回非零，该 warp 走完整数据路径处理自己的 32 行 stripe——scale 为 1 的行只是乘 1（正文 L733 原句："rows whose scale is 1 are simply multiplied by 1"）。过滤粒度是 warp 而不是行：行级短路会在 32 行间造成分支发散，得不偿失。

**练习 3**：论文用空闲 TMEM 传校正统计量，当前实现为什么改用 SMEM（`sScale`）？

**答案**：这是实现选择而非算法差异（正文 L266 归入"paper 与当前 TIRx 内核的差异"）。SMEM mailbox 配硬件命名屏障即可完成交接，避免占用本已排满的 TMEM（u14-l4：512 列被 2×(S+O) 恰好占满，唯一的"空闲"是 S 被读走后的后半段——那里正被 P 时序复用）；代价是 mailbox 需要 ready/empty 两道信号管理 SMEM 槽的复用。

### 4.3 TMEM→GMEM 回写：epilogue 与 O 的完整路径

#### 4.3.1 概念说明

所有 K/V 块处理完后，TMEM 里的 O 已经是完整的未归一化加权和 \( o_i \)。epilogue 做三件事：除以 `row_sum`、转 fp16、送出芯片。正文数据流图给的两行就是本讲全貌（[chapter_flash_attention/index.md:182-183](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L182-L183)）：

```text
when needed: O in TMEM --tcgen05.ld--> registers --rescale/TMEM store--> O in TMEM
at the end:  O in TMEM --tcgen05.ld--> registers --normalize/cast--> O in SMEM --TMA store--> O in GMEM
```

第一行是 4.2 的校正，第二行是本模块的 epilogue。阶段表把它们并列为 FA4 独有的两行（GEMM 没有校正，epilogue 也不含归一化）：[chapter_flash_attention/index.md:197-198](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L197-L198)。

分工差异：**non-causal** 由 WG2 收尾（等最终 `row_sum`、`o_ready`、可复用的 `O_smem` stage，然后读最终 O、乘 `1/row_sum`、cast fp16、写 `O_smem`，`corr_epi.full` 交给 WG3 的 TMA-store warp）；**causal** 把最终 epilogue 挪回 WG0/WG1（softmax 做完顺手收尾），WG2 只保留校正、跳过最终 `row_sum` 的 mailbox 往返（[chapter_flash_attention/index.md:770](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L770) 与 [chapter_flash_attention/index.md:221-223](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L221-L223)）。

最后是 **LSE（log-sum-exp）**：训练前向通常要写出 LSE 供 backward 复用，否则 backward 必须重算。当前实现只写 O（[chapter_flash_attention/index.md:772](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L772)）。LSE 可以从已有状态直接恢复：

\[ \mathrm{LSE}_i = \log(\mathrm{row\_sum}_i) + r_i/\sqrt{d} \]

推导只要求 `row_sum` 与 \( r_i \) 用同一参考——\( r_i \) 不必是精确最大值（[chapter_flash_attention/index.md:774-788](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L774-L788)）。换句话说，**条件 rescaling 推迟的参考更新不影响 LSE 的正确性**——延迟 rescaling 不改数学结果这一性质在 LSE 侧再次兑现。无有效 key 的行 LSE 为 \( -\infty \)。

#### 4.3.2 核心流程

epilogue 的三重等待与交接环：

```text
WG2 (non-causal):
  等: 最终 row_sum   ← mailbox 的最后一次交接 (named barrier ready)
  等: o_ready(i_q)   ← TCGen05Bar: 最终 PV MMA 段完成, O 定稿
  等: corr_epi.empty ← 上一次 TMA store 排空, O_smem stage 可复用
  做: o = tcgen05.ld(O_region[2+i_q])     # TMEM → 寄存器
      o *= 1 / row_sum                     # 归一化 (逐行)
      O_smem[i_q] = fp16(o)                # 寄存器 → SMEM 暂存
  到达: corr_epi.full(i_q)                 # 128 次 → 放行 TMA store

WG3 warp 2:
  等: corr_epi.full → 发 TMA store (O_smem → GMEM)
  到达: corr_epi.empty (32 次)             # 归还 O_smem stage
```

**O 的搬运总账**（一个 Q stage、一个 K/V 迭代 vs 整个任务）：

| 时段 | 搬运 | 次数 |
|---|---|---|
| 稳态 K/V 迭代（本 warp 无 rescale） | 显式搬运 | **0**（PV MMA 累加走 Tensor Core 通路，不经寄存器） |
| 稳态 K/V 迭代（本 warp 有 rescale） | TMEM→RF、RF→TMEM | 2 次/迭代（8 段×16 列合计一次完整往返） |
| 任务收尾 | TMEM→RF、RF→SMEM、SMEM→GMEM | 各 1 次 |
| **O 与 GMEM 的交互（整个任务）** | 单向写出 | **1 次，且从不读回** |

这就是 FlashAttention 系列的核心收益在数据侧的体现：O 从首次初始化到最终落盘，**从不在 GMEM 中转**；稳态迭代中多数轮次连 TMEM 往返都没有——条件 rescaling 把"每遇到更大最大值就搬一次"压缩为"尺度差超 256 倍才搬一次"。

#### 4.3.3 源码精读

**epilogue 的正文定义**（[chapter_flash_attention/index.md:770](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L770)，一段话写尽三等待、四步操作与 causal 分工）：

> Once the non-causal K/V loop ends, WG2 switches from correction to epilogue. It waits for the final `row_sum`, `o_ready`, and a reusable `O_smem` stage. It then reads the final `O` from TMEM, multiplies by `1 / row_sum`, casts to fp16, and writes `O_smem`. `corr_epi.full` hands that tile to WG3, whose TMA store warp writes it to GMEM.

**屏障总表中的三道相关屏障**（[chapter_flash_attention/index.md:305-309](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L305-L309)）：

| 屏障 | 到达者 / 条件 | 完成后什么变得安全 |
|---|---|---|
| `o_ready` | 1 个 elected MMA 线程；Tensor Core 在最终 PV MMA 段完成时报告 | epilogue 可读最终 O 累加器 |
| `corr_epi.full` | WG2 的 128 线程报 128 次到达 | TMA-store warp 可读完成的 `O_smem` tile |
| `corr_epi.empty` | TMA-store warp 的 32 线程，等完 TMA store 后报 32 次到达 | epilogue 可复用该 `O_smem` stage |

注意 `corr_epi.full`/`empty` 又是一对 full/empty 双向交接（u8-l2 的资源圈模式）：full 交数据、empty 还缓冲，作用对象是 SMEM 的 `O_smem` stage；`o_ready` 则是 TCGen05Bar——类型由完成者决定（u8-l1 铁律：硬件完成的用 commit 挂接，线程完成的用到达计数）。

**TMA store 一侧的机制**（u12-l1 已学，这里只需对应）：SMEM→GMEM 的 store 用 commit group / wait group 判定源缓冲可复用，对应上表 `corr_epi.empty` 行的"after waiting for the TMA store"。时间线的收尾次序为：最终两段 PV MMA 完成 → WG2 归一化 O0/O1 → WG3 warp 2 按序发出两次 TMA store（[chapter_flash_attention/index.md:708](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L708) 末句）。

**为什么经 SMEM 中转而不是寄存器直写 GMEM**：TMA store 是以整块 tile 为单位的引擎搬运，源必须是引擎可寻址的 SMEM（u6-l1：单线程发起、描述符+坐标、引擎搬运）；寄存器是线程私有的，没有 TMA 意义上的地址。这与 GEMM epilogue 的 Dsmem 中转（u11-l2）完全同构——FA4 只是把"cast 后写 GMEM"换成了"归一化+cast 后写 GMEM"。

#### 4.3.4 代码实践

**实践目标**：跟踪一条 O 的 rescale + writeback 完整路径——本讲主实践的第 1、3 步在此完成。

**操作步骤**（源码阅读型，无需 GPU）：

1. 以 stage 0 的第 \( r \) 行（\( 0\le r<128 \)，属于 WG2 的 warp \( \lfloor r/32\rfloor \)）为跟踪对象，沿时间顺序填出下表（每行注明执行者、源/目的存储、tile 原语、硬件路径）：

| 步骤 | 事件 | 执行者 | 源 → 目的 | 原语 / 机制 |
|---|---|---|---|---|
| 0 | pre-release（首块） | ？ | — | ？ |
| 1 | 初始化 O | ？ | ？ | ？ |
| 2 | 稳态累加 | ？ | ？ | ？ |
| 3 | rescale（若触发） | ？ | ？ | ？ |
| 4 | 定稿信号 | ？ | — | ？ |
| 5 | 归一化 + cast | ？ | ？ | ？ |
| 6 | 落盘 GMEM | ？ | ？ | ？ |

2. 对照正文逐格核对：步骤 0/3 查 [chapter_flash_attention/index.md:708](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L708) 与 [chapter_flash_attention/index.md:718-731](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L718-L731)；步骤 1/2/4 查 PV MMA 一节（u14-l4，[chapter_flash_attention/index.md:478-532](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L478-L532)）与屏障表 L303/L305；步骤 5/6 查 [chapter_flash_attention/index.md:770](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L770) 与阶段表 L198。
3. 书面回答统计题：一次 K/V 迭代中 O 在 TMEM 与 **GMEM** 间往返几次？O 与**寄存器**间往返几次？

**需要观察的现象**：无运行现象；检验标准是表格每一格都能落到正文原句。

**预期结果**（对照核对）：0 = WG2 向两个 `p_o_rescale` 槽预放行；1 = WG3 warp 0 的第一段 PV MMA（`accum=false`，P/V→TMEM）；2 = 后续 PV MMA 两段（`accum=true`，Tensor Core 通路，不经寄存器）；3 = WG2 校正（TMEM→RF→TMEM，`tcgen05.ld`/store + `wait::st`，仅当 `delta < -8` 且 `any_sync` 非零）；4 = `o_ready`（TCGen05Bar，commit 挂接）；5 = WG2 读 TMEM、乘 `1/row_sum`、cast fp16、写 `O_smem`；6 = WG3 warp 2 的 TMA store（SMEM→GMEM）。统计题答案：**TMEM↔GMEM 为 0 次**——一次 K/V 迭代根本不碰 GMEM；TMEM↔RF 在该 warp 触发 rescale 时为 1 次往返（读+写各 1）、未触发时为 0；整个任务 O 与 GMEM 只有最后 1 次单向写出。

#### 4.3.5 小练习与答案

**练习 1**：non-causal epilogue 等待的三样东西各由谁证明？

**答案**：最终 `row_sum` 由 softmax 战组经 mailbox（statistics named barrier）证明；`o_ready` 由 Tensor Core 的完成通知（`tcgen05.commit` 挂接的 TCGen05Bar）证明；可复用的 `O_smem` stage 由 `corr_epi.empty`（TMA-store warp 32 次到达）证明。

**练习 2**：epilogue 为什么走"寄存器 → SMEM → GMEM"两跳，而不是寄存器直写 GMEM？

**答案**：写出 GMEM 用的是 TMA store——以整块 tile 为单位的引擎搬运，源必须是 TMA 可寻址的 SMEM；寄存器是线程私有空间，无法作为 TMA 源。归一化与 cast 这类逐元素计算则最适合在寄存器里做，于是形成两跳。这与 GEMM 的 Dsmem 中转同构（u11-l2）。

**练习 3**：若把这个内核扩展为训练前向，需要额外写出什么？延迟 rescaling 会不会破坏它？

**答案**：LSE。\( \mathrm{LSE}_i=\log(\mathrm{row\_sum}_i)+r_i/\sqrt d \)，且 `row_sum` 与 `row_max` 已在内核中维护，只差写出。不会破坏：推导只要求二者用同一参考，\( r_i \) 无需是精确最大值（正文 L788）——无有效 key 的行取 \( -\infty \)。

## 5. 综合实践

**任务**：完成规格指定的主实践——跟踪一条 O 的 rescale + writeback 完整路径、写出校正循环与两个 Q stage 交替使用的伪代码、统计一次 KV 迭代中 O 的搬运次数。全部可本地完成，无需 Blackwell GPU。

1. **路径跟踪表**：完成 4.3.4 的七行表格（以 stage 0 第 0 行为例），每格附正文行号出处；再把 stage 1（O1，物理列 `[384,512)`）的差异写一行备注（仅区域索引不同，协议相同）。
2. **校正循环伪代码**：完成 4.2.4 的 WG2 伪代码，补全所有 `…`；再写一份对应的 softmax 侧伪代码（`softmax_corr.empty.wait` → 写 mailbox → `ptx_bar_arrive` → 本地更新 `row_sum`，时序依据 [chapter_flash_attention/index.md:462-472](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L462-L472) 与 [chapter_flash_attention/index.md:648-655](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L648-L655)）。两侧合起来即是完整的交替 stage 协议。
3. **配对表推演**：画出前 4 轮迭代（2 个 K/V 块 × 2 个 stage）中每道 `softmax_corr.empty` 的 arrive/wait 配对与 `p_o_rescale` 的 256 次到达构成，标出 pre-release 的 2 次提前到达；检查交替规则 `arrive(1 - i_q)` 是否自洽。
4. **搬运统计**：按 4.3.2 的总账表给出你的数字，并明确回答两个"陷阱题"：一次 K/V 迭代中 O 与 GMEM 的交互次数（0）；O 从初始化到落盘与 GMEM 的总交互次数（1 次单向写出）。
5. **交叉验证（可选）**：在 `img/scripts` 目录运行 `python gen_flash_attention_barrier_flow.py`（依赖 matplotlib、numpy；输出写到 `../` 下的图片文件，会覆盖仓库图片——建议先复制到临时目录再运行），对照生成的 `flash_attention_softmax_correction.png` 检查你的六步协议次序；运行说明见 [img/scripts/README.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/README.md)。书中章末练习 2（四条路径跟踪，[chapter_flash_attention/index.md:911](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L911)）与本实践第 1 步高度重合，可互为核对。

**验收标准**：路径表每格有行号出处；两份伪代码的每道 arrive/wait 都能在正文找到对应句；配对表无悬空等待（softmax 的每个 wait 都有 WG2 的先导 arrive，含首块的那次"对齐用"同步）；两个统计题答案正确并能说出原因（"PV MMA 累加走 Tensor Core 通路"与"TMA store 源必须在 SMEM"）。

## 6. 本讲小结

- **条件 rescaling**：换参考的判据是 \( \delta=(r_{\mathrm{old}}-r_c)\cdot\text{scale\_log2} \)，\( \delta\ge-8 \) 保留旧参考（`acc_scale=1`、旧 O 不动），\( \delta<-8 \) 才换参考（`acc_scale=exp2(delta)`）；阈值容许至多 256 倍尺度差，是性能优化而非数学近似——`row_sum` 在 softmax 寄存器里顺手乘，`O` 在 TMEM 里必须走 WG2 的独立数据路径。
- **校正循环**：softmax 把 per-row `acc_scale` 写入 SMEM mailbox（`sScale`），named barrier 报就绪、`softmax_corr.empty` 归还槽；WG2 读 O（`tcgen05.ld`）→ 乘 `acc_scale` → 写回（TMEM store + `wait::st`），按 `RESCALE_TILE=16` 分段、每 warp 凭 32-lane 访问窗口负责 32 行。
- **两级过滤**：阈值测试（行级）+ `any_sync`（warp 级）联合决定是否跳过数据路径；**跳过数据路径不跳过同步协议**——`p_o_rescale.arrive(i_q)` 与 `softmax_corr.empty.arrive(1 - i_q)` 无条件执行，后者归还的是另一个 stage 的槽，维持 WG0/WG1 交替。
- **TMEM→GMEM 回写**：non-causal 由 WG2 在等齐最终 `row_sum`、`o_ready`、`corr_epi.empty` 后完成"读 TMEM → 乘 \( 1/\text{row\_sum} \) → cast fp16 → 写 `O_smem`"，`corr_epi.full` 放行 WG3 warp 2 的 TMA store；causal 把这段 epilogue 挪回 WG0/WG1。
- **搬运总账**：稳态 K/V 迭代中 O 的显式搬运为 0（未触发 rescale）或 1 次 TMEM↔RF 往返（触发）；整个任务 O 与 GMEM 只有 1 次单向写出、从不读回——FA4 用条件 rescaling 与片上驻留把 O 的流量压到理论最小。
- **LSE**：训练前向需要 \( \mathrm{LSE}_i=\log(\text{row\_sum}_i)+r_i/\sqrt d \)，可从现有状态直接恢复；推导只要求 `row_sum` 与 `row_max` 同参考，当前实现未写出。

## 7. 下一步学习建议

- **下一讲 u14-l6（causal 掩码、GQA 与 tile 调度）**：本讲两处"causal 特化"的伏笔（epilogue 移入 WG0/WG1、`K_SPLIT` 取 64）都将在下一讲展开——右下对齐的因果掩码如何划分有效/部分/跳过的 K/V 块、GQA 如何把多个 query head 打包进 128 行、以及 causal 与 non-causal 为何要用不同的 tile 调度器。
- **回看 u8-l2（相位与 stage 复用）**：本讲 mailbox 的 `softmax_corr.empty.wait(wg_id, phase_q)` + `phase_q ^= 1` 是 full/empty 资源圈的最小实例之一，值得与 Step 5/Step 7 的双缓冲环对照复习。
- **回看 u12-l1（TMA store 的 commit group）**：epilogue 里 `corr_epi.empty` 的"等完 TMA store 才归还"正是 u12-l1 store 完成机制在 FA4 中的复用。
- **延伸阅读**：对照 tirx-kernels 的 `flash_attention4.py` 原文验证 `sScale`、`RESCALE_TILE` 与 `should_rescale` 的真实定义；有余力可思考练习：若把 `rescale_threshold` 从 8 调到 4，触发频率与指数动态范围如何此消彼长（提示：最大未归一化权重从 \( 2^8 \) 降到 \( 2^4 \)，但 TMEM 往返次数上升）。
