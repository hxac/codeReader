# 第 4 单元第 3 讲：复制（Replication）与偏移（Offset）

## 1. 本讲目标

学完本讲，你应该能够：

1. **区分两类布局扩展**：replication（复制）表示同一个逻辑元素出现在**多个物理位置**——一份数据、多处副本；offset（偏移）表示对基础坐标做**固定平移**——只挪位置、不产生副本。
2. 读懂并写出带副本的布局表达式：在基础布局 `S[(shape):(strides)]` 后面追加 `R[replica_shape : replica_stride]` 与偏移项，例如 `S[(32, …) : (1@TLane, …)] + R[4 : 32@TLane]`。
3. 说出**跨 warp 广播 scale factor** 这个硬件现场的完整链条：block-scaled MMA（MXFP8/NVFP4）→ SFA/SFB 打包进 32-lane 基础 tile → `.warpx4` 把它组播到 TMEM 的四个 32-lane 分区。
4. 在**多设备 GPU mesh** 中用同一套记号描述「按轴分片 + 跨设备复制 + 固定平移」三种摆放方式，并计算每个副本覆盖的 lane/column（或设备）区间。

本讲是「数据布局」单元的第 3 讲。u4-l1 建立了 Shape-Stride 模型 \( f_D(x)=\sum_k c_k s_k \)；u4-l2 把输出从「一个整数地址」推广为「一组带名字的物理坐标」。但到目前为止，**每个逻辑元素仍然只有唯一一个物理位置**。本讲打破这个限制——因为真实硬件（尤其是 Blackwell 的 block-scaled Tensor Core）要求同一份数据同时出现在好几个地方。

## 2. 前置知识

阅读本讲前，请确认你理解以下概念（不熟悉也没关系，下面用通俗语言快速补齐）：

- **命名轴布局（u4-l2）**：`S[(shape):(strides)]` 中每个 stride 都归属一根显式命名的物理轴，例如 `4@laneid`、`1@TLane`。求值时先按 shape 把扁平索引 \( x \) unflatten 成坐标，再按轴名累加，得到形如 `{"TLane": 5, "TCol": 3}` 的坐标字典。同一根轴被多个 iter 引用时贡献相加。
- **TMEM 的二维结构（u2-l2、u4-l2）**：Blackwell 的 Tensor Memory 是 128 个 Lane 行 × 最多 512 个 32-bit 列的二维阵列，用 `@TLane` 与 `@TCol` 描述。注意 TMEM 的 Lane 是**数据侧地址坐标**，不是线程的 laneid。
- **MMA 与 K 维累加（u2-l3）**：Tensor Core 做矩阵乘累加 \( D = C + A \times B \)，沿 K 维逐块推进。
- **低精度与 scale factor**：为了用更少的比特表示数，低精度格式把元素分成小组，每组共享一个缩放系数（scale factor）。例如 MXFP8 每 32 个元素共享一个 E8M0 字节，NVFP4 每 16 个 FP4 元素共享一个 E4M3 字节。实值 = 低精度值 × scale factor。
- **广播（broadcast）**：让一份数据同时可见于多个使用者。本讲的「跨 warp 广播」就是把 scale factor 复制成多份物理副本，让每个 warp 窗口都能就地读到。

一个直觉性铺垫：u4-l2 的布局函数像一张「一人一座」的座位表——每个观众（逻辑元素）对应唯一一个座位（物理坐标）。本讲要处理的情况是：**同一位观众需要同时在四个座位上留下复印件**（replication），或者**整场演出集体往后挪一排**（offset）。座位表的规则本身不用改，只需要在表格后面追加两栏说明。

## 3. 本讲源码地图

本讲的主战场是书章「Data Layout and Its Notation」的 Replication and Offset 一节，配合三个交互演示与三处后续章节的复用：

| 文件 | 作用 |
| --- | --- |
| [chapter_data_layout/index.md:L247-L353](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L247-L353) | 本讲第一现场：block-scaled MMA 的 scale factor 如何打包进 32-lane 基础 tile，又如何被 `.warpx4` 复制到 TMEM 四个分区 |
| [chapter_data_layout/index.md:L355-L381](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L355-L381) | `R[shape : strides]` 记号的定义：副本坐标 \( r \) 产生偏移 \( r \cdot s\ @\text{axis} \) |
| [chapter_data_layout/index.md:L383-L428](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L383-L428) | GPU mesh 一节：`R` 与 `O` 在多设备布局中的对比，并嵌入本讲核心演示 |
| [zh/chapter_data_layout/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_data_layout/index.md) | 同一章的中文镜像，结构与英文版一一对应，可对照阅读 |
| [_extra/demo/sf_tmem.html](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sf_tmem.html) | 交互演示：SFA 打包与 `.warpx4` 广播，点击任一 SFA 单元格查看它在四个分区中的位置 |
| [_extra/demo/tile_distributed.html](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tile_distributed.html) | 本讲核心演示：8×8 矩阵摆在 2×2 GPU mesh 上，可切换 fully sharded / shard+replica / shard+offset 三种布局 |
| [chapter_tirx_layout_api/index.md:L100-L180](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L100-L180) | TIRx `TileLayout` 的三段式写法 `S + R + offset` 与集合语义 \( L(x)=\{D(x)+r+O \mid r\in R\} \)（该章本身是 u10 的主题，此处只借用其记号定义） |
| [chapter_tirx_layout_api/index.md:L317-L346](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L317-L346) | scale factor 布局原子 `S[(32, sf_per_mma):(1@TLane, 1@TCol)] + R[4:32@TLane]` 的完整推导，含 8-bit 元素到硬件 32-bit 列的换算 |
| [chapter_tensor_cores/index.md:L226-L243](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L226-L243) | 本讲综合实践的依据：cta_group::2 场景下 SFA 沿 M 分片、SFB 在两个 CTA 各留完整副本 |
| [chapter_layout_generations/index.md:L271-L283](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L271-L283) | 澄清 `scale_vec` 的字内重复与 `R[4:32@TLane]` 的跨分区复制是两回事（该章是 u5-l3 的主题） |

## 4. 核心概念与源码讲解

### 4.1 跨 warp 广播：block-scaled MMA 的 scale factor 现场

#### 4.1.1 概念说明

在讲「记号怎么扩展」之前，先看一个**硬件真的需要多副本**的场景——这是书引入 replication 的动机。

**Block-scaled MMA** 不是某一种具体数据类型，而是一族使用「按块缩放」的低精度 MMA 操作，Blackwell 上常见的格式包括 MXFP8 与 NVFP4（见 [chapter_data_layout/index.md:L249-L254](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L249-L254)）。做法是把 A 和 B 沿 K 维切成一个个 scale block，每个 block 配一个 scale factor。若每个 block 沿 K 含 `K_blk` 个元素，则元素 `k` 所在的 block 是：

\[ \text{sfk} = k \ //\ K_\text{blk} \]

数学上它等价于先给 A、B 元素乘上各自的 scale factor 再做乘累加（见 [chapter_data_layout/index.md:L264-L271](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L264-L271)）：

\[
\begin{aligned}
A_\text{real}[m, k] &= A_\text{low}[m, k] \cdot \text{SFA}[m,\ k // K_\text{blk}]\\
B_\text{real}[k, n] &= B_\text{low}[k, n] \cdot \text{SFB}[n,\ k // K_\text{blk}]\\
D &= C + A_\text{real} \times B_\text{real}
\end{aligned}
\]

其中 `SFA[m, sfk]` 是 A 的第 `m` 行、第 `sfk` 个 K-scale-block 的系数；`SFB[n, sfk]` 是 B 的第 `n` 列对应的系数。

问题来了：这些 scale factor **放在 TMEM 的哪里**？书的 NVFP4 SFA 例子用 `M = 128`、`SF_K = 4`（每个 scale factor 占 1 字节），一步步推出摆放规则，最后发现同一份数据必须**同时出现在四个物理位置**——这正是 u4-l2 的布局函数做不到的事，于是引出本讲的 `R[...]` 记号。

#### 4.1.2 核心流程

整个推导分四步（数字均取自书中的 `M=128, SF_K=4` 例子）：

1. **算清字节数**：逻辑 SFA 是 `128 行 × 4 字节/行 = 512` 字节；搬运它的指令 `tcgen05.cp.32x128b.warpx4` 的基础 tile 是「32 个 lane × 每 lane 16 字节 = 512 字节」。两者恰好相等——这是摆放规则的约束来源。
2. **打包**：基础 tile 只有 32 个 lane 位置，装不下 128 行，于是把 `m` 拆开：

   \[ \text{local\_lane} = m \bmod 32, \qquad M_\text{group} = m \, //\, 32 \]

   `local_lane` 选 32 个 lane 之一；`Mgroup` 选该 lane 上的第几个 TCol。每行 4 个单字节 scale factor 恰好填满一个 32-bit TCol 单元，`sfk` 选单元内的字节。完整打包规则是：

   \[ \text{TCol} = M_\text{group}, \qquad \text{byte} = \text{sfk}, \qquad \text{byte\_offset} = \text{TCol}\cdot 4 + \text{byte} \]

3. **广播**：block-scaled `tcgen05.mma` 通过四个 32-lane 分区读 TMEM（分区 0 对应 TLane 0–31、分区 1 对应 32–63、分区 2 对应 64–95、分区 3 对应 96–127），并要求**每个分区**都在相同的 local-lane / TCol / byte 位置上提供完整的 scale-factor tile。PTX ISA 因此规定 SFA 与 SFB 都必须复制到全部四个分区（见 [chapter_data_layout/index.md:L324-L335](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L324-L335)）。
4. **物理坐标**：`.warpx4` 修饰符把打包好的基础 tile 组播进四个分区。设分区号 \( p = 0\ldots 3 \)，则：

   \[ \text{TLane} = \text{local\_lane} + 32p \]

   TCol 与 byte 坐标保持不变。例如 `SFA[64, 2]` 会同时出现在 `(TLane, TCol, byte)` = `(0,2,2)`、`(32,2,2)`、`(64,2,2)`、`(96,2,2)` 四个位置。

书中特别提醒（[chapter_data_layout/index.md:L347-L349](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L347-L349)）：这里出现了**两组互不相干的「四个」**——

- 四个 `Mgroup` 值把 128 个逻辑行**沿 TCol 打包**（这是分片，每行只出现在一个位置）；
- 四个分区是打包后 tile **沿 TLane 的物理副本**（这是复制，同一元素出现四次）。

「打包」与「复制」都是「数字 4」，但语义完全不同。区分这两者正是本讲的核心训练。

#### 4.1.3 源码精读

**① 尺寸吻合的巧合是规则的来源。**书在 [chapter_data_layout/index.md:L276-L289](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L276-L289) 写道：`128×4` 的 SFA 共 512 字节，而 `tcgen05.cp.32x128b.warpx4` 的 `.32x128b` 基础形状是 32 个本地 lane、每 lane 128 bit（16 字节），基础 tile 同样是 512 字节——大小严丝合缝，但基础 tile 只有 32 个 lane 位置，128 个 `m` 值不可能各占一个 lane，于是必须做 `m % 32` / `m // 32` 的拆分。

**② 打包规则与示例。**[chapter_data_layout/index.md:L298-L322](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L298-L322) 给出对固定本地 lane `l`，四组 SFA 行并排摆放的对照表：

```text
TCol 0: SFA[l,      0:4]
TCol 1: SFA[l + 32, 0:4]
TCol 2: SFA[l + 64, 0:4]
TCol 3: SFA[l + 96, 0:4]
```

这段代码说明：每 SFA 行的 4 个单字节 scale factor 恰好填满一个 32-bit TCol 单元，`sfk = 0…3` 选择单元内的字节。示例：`SFA[64, 2]` 有 `local_lane = 0`、`Mgroup = 2`，落在本地 lane 0 的 TCol 2 的字节 2 上；`SFA[0, 2]`、`SFA[32, 2]`、`SFA[64, 2]`、`SFA[96, 2]` 都用本地 lane 0，但分别占据 TCol 0、1、2、3——**它们不共享 TMEM 单元**（这是「打包」不是「复制」）。

**③ 广播与 `.warpx4`。**[chapter_data_layout/index.md:L337-L345](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L337-L345) 写道：`.warpx4` 修饰符把打包后的基础 tile 组播到四个分区，物理 Lane 坐标为 `TLane = local_lane + 32·p`，TCol 与 byte 坐标不变；因此 `SFA[64, 2]` 出现在 `(0,2,2)`、`(32,2,2)`、`(64,2,2)`、`(96,2,2)` 四处——这才是「复制」。

**④ 交互演示。**书在 [chapter_data_layout/index.md:L372-L381](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L372-L381) 嵌入 `sf_tmem.html`：点击任一 SFA 单元格，即可查看它在 32-lane 基础 tile 中的位置以及 `.warpx4` 之后在四个 TMEM 分区中的位置。演示副标题直接写明了本讲的结论式记号（[_extra/demo/sf_tmem.html:L36](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sf_tmem.html#L36)）：`.warpx4 broadcast: R[4 : 32@TLane]`；图例文字也标出 `TLane = m mod 32`（[L55](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sf_tmem.html#L55)）与 `.warpx4 broadcast → lanes { TLane, +32, +64, +96 }`（[L60](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sf_tmem.html#L60)）。

**⑤ SFB 同理。**[chapter_data_layout/index.md:L351-L353](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L351-L353) 指出 SFB 遵循同一硬件规则，只是把 A 的行号 `m` 换成 B 的列号 `n`：`N = 128`、`SF_K = 4` 时用 `local_lane = n % 32`、`TCol = n // 32`、`byte = sfk` 打包，再由 `.warpx4` 复制到四个分区。

#### 4.1.4 代码实践

**实践目标**：把 4.1.2 的打包 + 广播规则练到手，能对任意 `SFA[m, sfk]` 立即写出它的全部物理位置。

**操作步骤**（源码推演型实践，无需 GPU）：

1. 写一个几行的 Python 辅助函数（**示例代码**，非项目原有代码）：

   ```python
   def sfa_positions(m, sfk, M=128, SF_K=4):
       """返回 SFA[m, sfk] 经打包与 .warpx4 后的全部 (TLane, TCol, byte) 位置。"""
       local_lane = m % 32
       mgroup = m // 32          # 打包：沿 TCol 排开四组行
       tcol, byte = mgroup, sfk
       return [(local_lane + 32 * p, tcol, byte) for p in range(4)]  # 复制：四个分区
   ```

2. 用它验证书中给出的两个例子：
   - `SFA[64, 2]` → `[(0,2,2), (32,2,2), (64,2,2), (96,2,2)]`（书中原句）；
   - `SFA[0, 2]`、`SFA[32, 2]`、`SFA[96, 2]` 与 `SFA[64, 2]` 同用本地 lane 0，但 TCol 分别是 0、1、3——**TCol 不同，不是副本**。
3. 打开书站上的 `sf_tmem` 演示（本地构建后地址形如 `http://localhost:8000/_build/html/_extra/demo/sf_tmem.html`；构建方法见 u1-l2），点击几个 SFA 单元格，与脚本输出互相核对。

**需要观察的现象**：无论点击哪个 SFA 单元格，它**总是同时高亮在四个分区的相同相对位置**（TLane 相差 32 的倍数，TCol/byte 完全相同）；而同一列中 `m` 相差 32 的两个元素永远不在同一 TCol。

**预期结果**：脚本输出与演示高亮完全一致；你能口述「m 相差 32 → TCol 差 1（打包），p 相差 1 → TLane 差 32（复制）」。若无法在本地跑书站，可改为只做脚本推演并在纸上核对书中数值——此路径**待本地验证**的只有演示点击部分。

#### 4.1.5 小练习与答案

**练习 1**：`SFA[100, 1]`（M=128、SF_K=4）的 `local_lane`、`Mgroup`、`byte` 各是多少？它被 `.warpx4` 复制到哪四个 `(TLane, TCol, byte)` 位置？

**答案**：`local_lane = 100 % 32 = 4`；`Mgroup = 100 // 32 = 3`；`byte = sfk = 1`。复制到 `(4,3,1)`、`(36,3,1)`、`(68,3,1)`、`(100,3,1)`——TLane 依次为 `4 + 32p`。

**练习 2**：`SFA[64, 2]` 与 `SFA[96, 2]` 都落在字节 2 上。它们是彼此的副本吗？

**答案**：不是。`SFA[64, 2]` 的 `local_lane = 0`、`Mgroup = 2`；`SFA[96, 2]` 的 `local_lane = 0`、`Mgroup = 3`。两者 TCol 不同（2 与 3），是**沿 TCol 的打包**造成的两个不同物理单元，各自由 `.warpx4` 独立复制四份。判断副本的唯一标准：坐标只差在副本轴（这里是 TLane 相差 32 的倍数）上。

**练习 3**：SFB（N=128、SF_K=4）中 `SFB[n=20, sfk=3]` 的基础位置是什么？

**答案**：SFB 把 A 的行号 `m` 换成 B 的列号 `n`：`local_lane = 20 % 32 = 20`，`TCol = 20 // 32 = 0`，`byte = 3`；再复制到 `(20,0,3)`、`(52,0,3)`、`(84,0,3)`、`(116,0,3)`。

### 4.2 replication：`R[shape : strides]` 让一个元素拥有多个物理坐标

#### 4.2.1 概念说明

u4-l1/u4-l2 定义的 \( f_D(x) \) 对每个逻辑元素只返回**一个**位置，它无法表达 `.warpx4` 制造的额外副本。书的解法（[chapter_data_layout/index.md:L355-L360](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L355-L360)）是在基础布局后面**追加**一段 `R[shape : strides]`：

- `R[n : s@axis]` 引入一个**独立的副本坐标** \( r = 0\ldots n-1 \)，产生偏移 \( r \cdot s\ @\text{axis} \)；
- 它与逻辑索引**无关**——不管 \( x \) 是谁，副本都按同一组偏移平移；
- 副本不增加逻辑数据，只记录副本的物理位置（[L369-L370](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L369-L370) 原话："The replication term does not add new logical data; it records the physical locations of the copies."）。

于是布局函数的返回值从「一个坐标」升级为「**一组坐标**」。TIRx Layout API 章把它写成集合语义（[chapter_tirx_layout_api/index.md:L160-L168](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L160-L168)）：

\[ L(x) = \{\, D(x) + r + O \mid r \in R \,\} \]

其中 \( D(x) \) 是 shard（基础分片）产生的坐标，\( r \) 遍历副本 iter 产生的偏移，\( O \) 是固定偏移（4.3 节）。没有 `R` 时集合里只有一个坐标；有复制时每个副本一个坐标。

还有一个工程层面的要点（同上链接）：当前的 `layout.apply()` **只计算基础坐标** \( D(x)+O \)，不枚举 `R`；副本信息保留在 `layout.replica` 里，由**使用这个布局的 tile 操作**去处理。换句话说，布局只声明「副本应该在哪」，副本如何被生产（如 `.warpx4` 组播）或消费是操作的事。

#### 4.2.2 核心流程

带 replication 的求值过程用伪代码描述：

```text
输入: 逻辑坐标 x, shard S, 副本 R, 偏移 O
D = shard_eval(S, x)              # 与 u4-l2 完全相同：unflatten + 按轴累加
replicas = 笛卡尔积枚举 R 的全部副本坐标 r   # 例如 R[4:32@TLane] → {0,32,64,96}@TLane
L(x) = { D + r + O | r ∈ replicas }         # 每个副本平移一份基础坐标
```

对书中 TMEM 例子（[chapter_data_layout/index.md:L362-L370](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L362-L370)）：

```text
S[(32, …) : (1@TLane, …)] + R[4 : 32@TLane]
```

`R[4 : 32@TLane]` 中 `r` 取 `0、1、2、3`，产生 `TLane` 偏移 `0、32、64、96`。若某元素的 shard 坐标是 `TLane=5`，则它的完整位置集合是 `TLane ∈ {5, 37, 69, 101}`。

几个值得记住的性质：

- **副本总数** = 各副本 iter extent 的乘积（这里 \( 4 \)）；每个逻辑元素的物理位置数 = 副本总数。
- **u4-l2 的双射性检查要相应放宽**：物理位置数 = 逻辑元素数 × 副本数（同一逻辑元素占多处，不同逻辑元素仍不得撞在同一物理位置）。
- **`R` 的 stride 可以落在任何命名轴上**——沿 `@TLane` 复制（scale factor 广播）、沿 `@warpid` 复制（跨 warp）、沿设备轴复制（4.4 节的 mesh）都合法。

#### 4.2.3 源码精读

**① 记号定义。**[chapter_data_layout/index.md:L355-L370](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L355-L370) 一段完成定义：`f_D(x)` 只返回一个位置、无法表达 `.warpx4` 的副本，因此**追加** `R[shape : strides]`；`R[n : s@axis]` 引入独立副本坐标 `r = 0…n-1`、产生偏移 `r·s@axis`；TMEM 四副本即 `S[(32, …) : (1@TLane, …)] + R[4 : 32@TLane]`。

**② API 语义。**[chapter_tirx_layout_api/index.md:L132-L144](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L132-L144) 给出 API 视角的定义：副本 iter **不依赖逻辑索引**，只在物理空间里枚举额外偏移；例如 `R[2 : 4@warpid]` 沿 `warpid` 轴放两份副本、相隔 4 个 warp。并强调：布局只记录副本属于哪里，副本如何被生产/使用由消费该布局的 tile 操作决定。

**③ 三段式完整写法。**[chapter_tirx_layout_api/index.md:L170-L180](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L170-L180) 给出 `S[...] + R[...] + 偏移` 的完整例子：

```python
layout = TileLayout(
    S[(8, 2, 4, 2) : (4@laneid, 1@warpid, 1@laneid, 1)]
    + R[2 : 4@warpid]
    + 5@warpid
)
```

从左往右读：`S[...]` 摆放逻辑 tile，`R[2:4@warpid]` 在 4 个 warp 之外再加一份副本，`5@warpid` 把所有位置整体平移 5。

**④ scale factor 原子。**[chapter_tirx_layout_api/index.md:L317-L346](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L317-L346) 把 4.1 节的硬件规则收束成一个可复用的布局原子（`32xsf_per_mma`）：

```python
scale = TileLayout(
    S[(32, sf_per_mma) : (1@TLane, 1@TCol)]
    + R[4 : 32@TLane]
)
```

对逻辑坐标 `(r, s)`，shard 先给出 `TLane = r, TCol = s`；副本再生成 `TLane = r + 32q, q ∈ {0,1,2,3}`，于是这组 32 行出现在 lane `0-31`、`32-63`、`64-95`、`96-127`——**每个 warp 的 32-lane TMEM 窗口都能读到同样的 scale factor**。该节还交代了单位换算：8-bit scale-factor buffer 的 `TCol` 坐标仍以 buffer 元素为单位，4 个相邻元素位置打进一个 32-bit 硬件 Col，即硬件 Col = `s//4`、字节位置 = `s%4`。完整布局只需在此基础上再套 M 与 K-scale-block 的外层 iter。

**⑤ 交互演示预设。**仓库的 Layout API 交互演示（`static/tirx-layout-demo/`，由 [chapter_tirx_layout_api/index.md:L68-L94](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L68-L94) 嵌入正文，u10 详讲）预置了可直接编辑的副本表达式，见 [static/tirx-layout-demo/layout-demo.js:L856-L872](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/tirx-layout-demo/layout-demo.js#L856-L872)：`Shard + replica` 预设是 `S[(4,8):(8@laneid,1@laneid)] + R[2:1@warpid]`，`Tensor-core tile` 预设是 `S[(8,2,4,2):(4@laneid,1@warpid,1@laneid,1)] + R[2:4@warpid] + 5@warpid`——演示会**枚举出每个物理副本**供点击检查。

**⑥ 别和字内重复混淆。**[chapter_layout_generations/index.md:L271-L283](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L271-L283) 专门澄清：`scale_vec::1X/2X` 在一个 32-bit 字**内部**重复字节，与 `R[4:32@TLane]` 跨四个 TMEM lane 窗口复制是**两回事**；scale 在其 K-block 内的数学复用是第三个独立的概念。三层「复用」各管一段，不要混为一谈。

#### 4.2.4 代码实践

**实践目标**：实现集合语义 \( L(x)=\{D(x)+r+O\mid r\in R\} \) 的最小求值器，亲眼看到「一个逻辑元素 → 多个物理坐标」。

**操作步骤**（纯离线实践，无需 GPU 与 tvm）：

1. 新建 `repl_eval.py`（**示例代码**，非项目原有代码；放在讲义目录或任意临时目录均可，**不要写进仓库源码**）：

   ```python
   from itertools import product

   def unflatten(x, shape):
       coords = []
       for e in reversed(shape):
           coords.append(x % e)
           x //= e
       return list(reversed(coords))

   def shard_eval(x, shape, strides_axes):
       """strides_axes: [(stride, axis), ...]；返回按轴名累加的坐标字典。"""
       cs = unflatten(x, shape)
       out = {}
       for c, (s, ax) in zip(cs, strides_axes):
           out[ax] = out.get(ax, 0) + c * s
       return out

   def replica_offsets(replica_shape, replica_strides_axes):
       """枚举全部副本偏移（每个偏移也是一个坐标字典）。"""
       offs = [{}]
       for e, (s, ax) in zip(replica_shape, replica_strides_axes):
           offs = [dict(o, **{ax: o.get(ax, 0) + r * s}) for o in offs for r in range(e)]
       return offs

   def add_coords(a, b):
       return {k: a.get(k, 0) + b.get(k, 0) for k in set(a) | set(b)}

   def evaluate(x, shape, strides_axes, replica=(), offset=None):
       D = shard_eval(x, shape, strides_axes)
       if offset:
           D = add_coords(D, offset)
       return [add_coords(D, r) for r in replica_offsets(*replica)] if replica else [D]
   ```

2. 用它复现本讲的两个场景：

   ```python
   # 场景 A：TMEM scale factor 广播（元素单位）
   # S[(32, 4) : (1@TLane, 1@TCol)] + R[4 : 32@TLane]
   for x in [2 * 4 + 2]:        # 逻辑 (r=2, s=2) → SFA[64, 2] 的原子内坐标
       print(evaluate(x, (32, 4), [(1, "TLane"), (1, "TCol")],
                      replica=((4,), [(32, "TLane")])))
   # 期望: [{'TLane': 2, 'TCol': 2}, {'TLane': 34, ...}, {'TLane': 66, ...}, {'TLane': 98, ...}]

   # 场景 B：R[2 : 4@warpid] 的跨 warp 副本
   print(evaluate(5, (8,), [(1, "laneid")], replica=((2,), [(4, "warpid")])))
   # 期望: [{'laneid': 5, 'warpid': 0}, {'laneid': 5, 'warpid': 4}]
   ```

3. 运行 `python repl_eval.py`。

**需要观察的现象**：每次调用的输出都是**一个列表**——列表长度等于副本总数（场景 A 为 4，场景 B 为 2）；同一逻辑元素的所有坐标只差在副本轴的偏移上。

**预期结果**：输出与注释里的期望一致。场景 A 的四个 `TLane` 依次相差 32；这正是 `.warpx4` 广播后每个元素的落点。若把 `replica` 参数去掉，输出退化为单元素列表——就是 u4-l2 的世界。

#### 4.2.5 小练习与答案

**练习 1**：`R[2 : 4@warpid]` 产生哪些偏移？它和 `R[4 : 2@warpid]` 的区别是什么？

**答案**：`R[2 : 4@warpid]` 产生偏移 `0@warpid` 与 `4@warpid`，即两份副本、相隔 4 个 warp（与 [chapter_tirx_layout_api/index.md:L139-L142](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L139-L142) 一致）。`R[4 : 2@warpid]` 则产生 `0、2、4、6` 四个偏移——四份副本、相隔 2 个 warp。前者的 extent 是 2、stride 是 4；后者 extent 是 4、stride 是 2：**extent 决定份数，stride 决定间距**。

**练习 2**：布局 `S[(32, 4) : (1@TLane, 1@TCol)] + R[4 : 32@TLane]` 描述了多少个物理位置？它与同样 128 个逻辑元素的布局 `S[(128, 4) : (1@TLane, 1@TCol)]` 有何本质区别？

**答案**：前者逻辑元素 \( 32\times 4=128 \) 个，副本 4 份，共 \( 128\times 4=512 \) 个物理位置，每个元素出现在 4 处；后者 128 个元素各占一个位置，共 128 个物理位置，无副本。前者对应「每个 warp 窗口都能读到同一份 scale factor」的需求；后者要求 128 行**不同**数据各就各位（4.1 节的「打包」路线正是把 128 行压进 32 lane 之外的 TCol，两种方案物理上不等价）。

**练习 3**：为什么 `layout.apply()` 只返回基础坐标 \( D(x)+O \)，而不枚举副本？

**答案**：副本信息保存在 `layout.replica` 中，由消费该布局的 tile 操作处理（[chapter_tirx_layout_api/index.md:L168](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L168)）。布局是声明式描述：它说明「副本应当出现在哪里」；至于副本是靠 `.warpx4` 组播、靠多次拷贝还是只在读取时广播，是具体操作的实现细节，不属于布局求值本身。

### 4.3 offset：`O` 是固定平移而非复制

#### 4.3.1 概念说明

与 replication 相对的另一类扩展是 **offset（偏移）**：给基础坐标加一个**常数向量**。书用 `O` 记号表示，例如 `O[1@gpuid_x]` 表示沿 `@gpuid_x` 轴平移一步。

两者关键区别一句话：**`R` 制造副本，`O` 只挪位置**。在 GPU mesh 的例子里（详见 4.4 节），同一个基础布局：

- 加 `R[2 : 1@gpuid_x]` → 元素出现在**两台设备**上 `{(0,1), (1,1)}`；
- 加 `O[1@gpuid_x]` → 元素只出现在**一台设备** `(1,1)` 上，只是从 `gpuid_x=0` 平移到了 `gpuid_x=1`，没有产生任何副本（[chapter_data_layout/index.md:L410-L420](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L410-L420)）。

TIRx API 的表述（[chapter_tirx_layout_api/index.md:L146-L158](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L146-L158)）：offset **加到每一个被映射的坐标上**（包括每一个副本）；例如 `5@warpid` 把整个布局沿 `warpid` 轴平移 5 个位置。它的典型用途是**选定 tile 的起始坐标**，或把若干 tile 摆进同一硬件资源的**不同区域**——例如一个 block-scaled FP8 GEMM 会在 TMEM 里同时给两个累加器 stage 和 scale factor 划分区域（[chapter_tirx_layout_api/index.md:L309-L315](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L309-L315)），此时后放入的区域就要靠 offset 定位。

用集合语义看，`O` 与 `R` 的分工一目了然：

\[ L(x) = \{\, \underbrace{D(x)}_{\text{分片}} + \underbrace{r}_{\text{每个副本一份}} + \underbrace{O}_{\text{所有位置统一平移}} \mid r \in R \,\} \]

- \( O \) 在集合外壳之外——**每个**坐标都被它平移；
- \( r \) 在集合之内——**每个副本**一个取值；
- 物理位置数 = 逻辑元素数 × 副本数，**与 \( O \) 无关**。

#### 4.3.2 核心流程

应用 offset 后的求值流程只是在上节的伪代码里加一步：

```text
D = shard_eval(S, x)
D = D + O                     # 固定平移：对每个轴加常数
L(x) = { D + r | r ∈ replicas }   # 副本枚举照旧
```

对比表（把 `R` 与 `O` 放在一起）：

| 维度 | `R[shape : strides]`（复制） | `O[...]`（偏移） |
| --- | --- | --- |
| 语义 | 同一逻辑元素出现在多个物理位置 | 基础坐标整体平移 |
| 副本数量 | 各 extent 之积，≥ 2 | 恒为 1（不产生副本） |
| 是否依赖逻辑索引 | 否（独立副本坐标） | 否（常数） |
| 在 \( L(x) \) 中的位置 | 集合内，每副本一个取值 | 集合外壳外，统一加上 |
| 典型用途 | 广播 scale factor、跨设备冗余、跨 warp 可见性 | 选 tile 起点、在同一资源中摆放多个 tile |
| 硬件现场 | `.warpx4` 组播；SFB 组播到 CTA 对 | TMEM 分区定位；把 scale factor 摆到指定列区 |

#### 4.3.3 源码精读

**① 书中的对照实验。**[chapter_data_layout/index.md:L410-L420](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L410-L420) 是全书对二者最直接的一次对比：`base + R[2 : 1@gpuid_x]` 让元素 `(1,2,3)` 落在设备 `{(0, 1), (1, 1)}`、本地偏移 19；换成 `base + O[1@gpuid_x]` 后元素只落在设备 `(1, 1)`、本地偏移 19——offset 沿 `@gpuid_x` 把基础位置平移一步，**不创建副本**。

**② API 定义。**[chapter_tirx_layout_api/index.md:L146-L158](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L146-L158)：offset 加到每个被映射坐标上，记作 `O`；`5@warpid` 沿 `warpid` 轴整体平移 5；用途是选定 tile 起始坐标、或把多个 tile 摆进同一硬件资源的不同区域。

**③ 组合阅读顺序。**[chapter_tirx_layout_api/index.md:L180](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L180) 教你从左到右读 `S[...] + R[...] + 5@warpid`：`S` 摆放、`R` 加副本、offset 平移一切。注意 `5@warpid` 平移的是**包括副本在内的所有位置**——若 `R[2:4@warpid]` 的副本在 warpid 0 与 4，加 `5@warpid` 后就到 warpid 5 与 9。

**④ TMEM 划区的现实动机。**[chapter_tirx_layout_api/index.md:L309-L315](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L309-L315) 的例子说明为什么需要 offset：TMEM 列维可以不取 2 的幂（如 extent 112），两个 112 列的区域共 224 列；真实内核可能故意这么切，让一个 block-scaled FP8 GEMM 的 TMEM 同时容纳两个累加器 stage **和** scale factor，而不是让单个累加器 tile 独占 256 列。多个区域共处一址空间时，后放入者靠 offset 定位。

#### 4.3.4 代码实践

**实践目标**：用 4.2.4 的求值器做一组对照实验，量化「`R` 加设备、`O` 挪设备」的差别。

**操作步骤**：

1. 在 `repl_eval.py` 末尾追加（**示例代码**）：

   ```python
   # 8×8 矩阵按行分片到 2×2 mesh 的 @gpuid_y；行内是 4×8 行主序
   # base = S[(2, 4, 8) : (1@gpuid_y, 8@m, 1@m)]
   base_args = ((2, 4, 8), [(1, "gpuid_y"), (8, "m"), (1, "m")])

   def logical(y, row, col):        # 逻辑坐标 → 扁平索引
       return ((y * 4 + row) * 8) + col

   x = logical(1, 2, 3)             # 书中例子：元素 (y=1, row=2, col=3)

   print("base:      ", evaluate(x, *base_args))
   print("+ R[2:1@gpuid_x]:", evaluate(x, *base_args,
                                         replica=((2,), [(1, "gpuid_x")])))
   print("+ O[1@gpuid_x]:  ", evaluate(x, *base_args,
                                         offset={"gpuid_x": 1}))
   ```

2. 运行并对照输出。

**需要观察的现象**：三行输出的 `m` 分量都是 19（`2·8+3`）、`gpuid_y` 都是 1；差别只在 `gpuid_x`：base 没有 `gpuid_x` 分量（该轴未出现），加 `R` 后输出**两个**坐标（`gpuid_x=0` 与 `gpuid_x=1`），加 `O` 后输出**一个**坐标（`gpuid_x=1`）。

**预期结果**：与书中给出的结论逐字对应——`base + R[2:1@gpuid_x]` → `devices {(0,1), (1,1)}, local offset 19`；`base + O[1@gpuid_x]` → `device (1,1), local offset 19`（[chapter_data_layout/index.md:L402-L417](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L402-L417)）。

#### 4.3.5 小练习与答案

**练习 1**：布局 `S[(8,2,4,2):(4@laneid,1@warpid,1@laneid,1)] + R[2:4@warpid] + 5@warpid` 中，`5@warpid` 平移了哪些位置？

**答案**：**所有**位置——包括 `R` 产生的两个副本。设某元素 shard 后 `warpid = w`，则两个副本原本在 `warpid = w` 与 `w+4`，加 offset 后在 `w+5` 与 `w+9`（依据 [chapter_tirx_layout_api/index.md:L148](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L148)"The offset is added to every mapped coordinate" 与 [L165](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L165) 的集合式）。

**练习 2**：把 `R[2 : 1@gpuid_x]` 误写成 `O[1@gpuid_x]`（或反之），各会出什么错？

**答案**：该写 `R` 却写成 `O`：数据只在一台设备上，另一台设备读不到——对 SFB 这类「两个 CTA 都要完整副本」的场景，缺副本一侧的 MMA 会读到错误（或未初始化）的 scale factor。该写 `O` 却写成 `R`：数据被冗余写到两处，浪费容量与写入带宽，且若两侧副本更新不同步还会出现数据不一致。核心判断：需要**多个读取点同时可见** → `R`；只需要**换个起点** → `O`。

**练习 3**：TMEM 每个 lane 最多 512 个 32-bit 硬件列。设累加器 tile（32-bit 元素）占硬件列 0–223，8-bit scale factor 原子 `S[(32, sf):(1@TLane, 1@TCol)] + R[4:32@TLane]` 要放到从硬件列 224 开始的区域，偏移项应写什么？

**答案**：`@TCol` 的 stride 以 buffer 元素为单位，仅当元素宽 32-bit 时才与硬件列一一对应（[chapter_tirx_layout_api/index.md:L204](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L204)）；8-bit buffer 里 4 个元素位置才占一个硬件列。因此起点应换算为 `224·4 = 896` 个元素位置，偏移写 `+ 896@TCol`，即 `S[(32, sf) : (1@TLane, 1@TCol)] + R[4 : 32@TLane] + 896@TCol`。偏移只作用于 `@TCol`，`R` 仍沿 `@TLane` 复制；每个副本覆盖的元素列区间从 `[0, sf)` 平移为 `[896, 896+sf)`（硬件列 224 起），四个副本的 lane 区间 `0-31/32-63/64-95/96-127` 不受影响。（列规划数字为本练习设定，机制依据是 [chapter_tirx_layout_api/index.md:L158](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L158)"An offset can select a tile's starting coordinate"。）

### 4.4 GPU mesh 映射：shard + replica + offset 三种布局对比

#### 4.4.1 概念说明

同一套 `S + R + O` 记号可以从 SM 内部一路用到多设备。书用 **GPU mesh**（把多块 GPU 沿一根或多根逻辑设备轴排成的阵列）演示这一点（[chapter_data_layout/index.md:L383-L388](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L383-L388)）：一个 `2×2` mesh 有四块 GPU，每块用坐标 `(@gpuid_x, @gpuid_y)` 标识。设备轴（`@gpuid_x`、`@gpuid_y`）与 `@TLane`、`@laneid` 地位完全平等——都是命名轴，都能挂 stride。

这个例子把本讲三种机制放进同一张图：

1. **fully sharded（纯分片）**：行、列两个方向都切到设备轴上，每个元素只在一块 GPU 上；
2. **shard + replica（分片 + 复制）**：行方向分片，列方向整段复制到两块 GPU——每元素两份副本；
3. **shard + offset（分片 + 平移）**：行方向分片，整块数据平移到 `gpuid_x=1` 的 GPU 上——每元素仍只有一份。

对应到真实系统：张量并行里权重需要在多卡冗余（replica），流水线里某个 shard 只想放在特定 rank（offset 偏移后落位），数据并行里每卡各持一份完整副本再各自更新——布局记号把这些**摆放策略**写成一行可推理的式子。

#### 4.4.2 核心流程

书的具体设定（[chapter_data_layout/index.md:L389-L421](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L389-L421)）：8×8 矩阵，基础布局沿 `@gpuid_y` 分片：

```text
base = S[(2, 4, 8) : (1@gpuid_y, 8@m, 1@m)]
```

三个逻辑坐标 `(y, row, col)` 中，`y` 选两行 GPU，`row % 4` 与 `col` 决定本地 4×8 区域内的位置。逐步求值元素 `(1, 2, 3)`：

```text
gpuid_y = 1
m       = 2·8 + 3 = 19
```

在此基础上追加两项中的任意一个：

```text
base + R[2 : 1@gpuid_x]   →  元素 (1,2,3) 落在设备 {(0,1), (1,1)}，本地偏移 19
base + O[1@gpuid_x]       →  元素 (1,2,3) 落在设备 (1,1)，        本地偏移 19
```

三条判读规则：

- 看某个轴是否出现在**输出坐标**里：`@gpuid_x` 出现 ⇒ 该轴参与了放置；
- 看输出是**集合还是单点**：集合 ⇒ 有副本；单点 ⇒ 无副本；
- 看本地偏移 `m` 是否改变：`R` 与 `O` 都**不动** shard 部分——它们只在设备轴上做文章。

#### 4.4.3 源码精读

**① 书的正文推导。**[chapter_data_layout/index.md:L389-L421](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L389-L421) 完整给出 base 布局、元素 `(1,2,3)` 的求值、`+R` 与 `+O` 两种扩展的结果对照，并点明 offset「平移基础位置、不创建副本」。

**② 交互演示的三种模式。**书在 [chapter_data_layout/index.md:L423-L428](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L423-L428) 嵌入 `tile_distributed.html`：左侧是 8×8 逻辑矩阵，右侧是 2×2 GPU mesh，点击任一单元格即可看到「哪些设备持有该元素」。控制条提供三个按钮（[_extra/demo/tile_distributed.html:L42-L49](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tile_distributed.html#L42-L49)）：`Fully Sharded (S0S1)`、`Shard + Replica (S0R)`、`Shard + Offset (S0+O)`。

**③ 演示源码里的三组映射函数。**这个演示本身就是本讲语义的可执行注释：

- 分片模式（[_extra/demo/tile_distributed.html:L87-L106](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tile_distributed.html#L87-L106)）：注释直接标出布局 `S[(2, 4, 2, 4) : (1@gpuid_y, 4@m, 1@gpuid_x, 1@m)]`——行、列**都**切到设备轴，`shardGpus` 对每个 `(r, c)` 只返回**一块** GPU；
- 复制模式（[_extra/demo/tile_distributed.html:L108-L126](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tile_distributed.html#L108-L126)）：注释标出 `S[(2, 4, 8) : (1@gpuid_y, 8@m, 1@m)] + R[2 : 1@gpuid_x]`，`replicaGpus` 返回 `[gy*2, gy*2+1]`——**两块** GPU；
- 平移模式（[_extra/demo/tile_distributed.html:L140-L157](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tile_distributed.html#L140-L157)）：注释写明 `O[1@gpuid_x]: data placed at gpuid_x=1 (offset by 1, not replicated)`，`getGpus` 只返回 `gpuid_x=1` 的那块，且 `getGpuData` 对 `g % 2 === 0` 的 GPU 返回空——`gpuid_x=0` 的 GPU 上**没有数据**。

每块 GPU 的本地形状也印证语义（[L160-L165](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tile_distributed.html#L160-L165)）：分片模式本地是 4×4、地址 `lr*4+lc`；复制/平移模式本地是 4×8、地址 `lr*8+lc`。演示顶部的记号栏（[L186-L191](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tile_distributed.html#L186-L191)）随按钮切换三种布局表达式；点击单元格时公式栏展开逐项求值（[L325](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tile_distributed.html#L325)）：`addr = (row // 4) × 1@gpuid_y + (row % 4) × 8@m + col × 1@m + R[2 : 1@gpuid_x]`。

**④ Layout API 演示的 mesh 预设。**[static/tirx-layout-demo/layout-demo.js:L864-L865](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/tirx-layout-demo/layout-demo.js#L864-L865) 预置了 mesh 布局：fully sharded 用 `S[(2,2,2,2):(1@pid,2@m,2@pid,1@m)]`，mesh + replica 用 `S[(2,2,4):(1@pid,2@m,1@m)] + R[2:2@pid]`——注意它把分布式轴命名为 `@pid`（进程/设备编号），与书正文的 `@gpuid_x` 是同一角色。**轴名是记号问题，语义才是本质**。

#### 4.4.4 代码实践

**实践目标**：在交互演示中逐一核对三种模式的设备占用与本地地址，并计算「每个副本覆盖的区间」。

**操作步骤**：

1. 本地构建书站（见 u1-l2：`pip install -r requirements-docs.txt` 后 `sphinx-build -b html . _build/html`），浏览器打开 `_build/html/_extra/demo/tile_distributed.html`；或直接用文本编辑器阅读该 HTML 中的三组映射函数。
2. 依次切换三个按钮，**点击同一个单元格**（例如第 2 行第 3 列，即逻辑元素 `(r=2, c=3)`），记录：持有它的设备集合、本地地址、每块 GPU 的本地形状。整理成下表（示例答案先行给出，供核对）：

   | 模式 | 布局表达式 | 持有 `(r=2,c=3)` 的设备 | 本地地址 | GPU 本地形状 |
   | --- | --- | --- | --- | --- |
   | Fully Sharded | `S[(2,4,2,4):(1@gpuid_y,4@m,1@gpuid_x,1@m)]` | `{(y=0, x=0)}`（即 GPU 0） | `(r%4)·4 + c%4 = 2·4+3 = 11` | 4×4 |
   | Shard + Replica | `S[(2,4,8):(1@gpuid_y,8@m,1@m)] + R[2:1@gpuid_x]` | `{(y=0, x=0), (y=0, x=1)}` | `(r%4)·8 + c = 2·8+3 = 19`（两台相同） | 4×8 |
   | Shard + Offset | `S[(2,4,8):(1@gpuid_y,8@m,1@m)] + O[1@gpuid_x]` | `{(y=0, x=1)}`（仅 GPU 1） | `19` | 4×8 |

   计算依据：分片模式下 `gy = r//4`、`gx = c//4`、本地 `lr = r%4`、`lc = c%4`；复制/平移模式下 `gy = r//4`、本地 `lr = r%4`、`lc = c`（全列都在本地），与 [_extra/demo/tile_distributed.html:L87-L126](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tile_distributed.html#L87-L126) 的映射函数一致。

3. 用 4.2.4/4.3.4 的 `repl_eval.py` 脚本复核表中第 2、3 行（`evaluate` 的输出即设备集合）。

**需要观察的现象**：切到 Shard + Offset 后，右图中 `gpuid_x=0` 的两块 GPU **整体变灰**并标注 `no data (offset → gpuid_x=1)`（空数据 GPU 被降到 0.4 透明度，见 [_extra/demo/tile_distributed.html:L261-L272](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tile_distributed.html#L261-L272)）；切到 Shard + Replica 后，点击任意单元格都有**两块** GPU 同时高亮。

**预期结果**：表格三行与演示显示、脚本输出三方一致。特别注意 Replica 与 Offset 两行的本地地址完全相同（都是 19 对应的本地 `row%4`、`col` 结构）——`R` 与 `O` 都不触碰 shard 决定的本地摆法。若本地未构建书站，此步**待本地验证**，可先以演示源码的映射函数做纸面推演。

#### 4.4.5 小练习与答案

**练习 1**：在 `base = S[(2,4,8):(1@gpuid_y, 8@m, 1@m)]` 上，`R[2 : 1@gpuid_x]` 的每个「副本」各覆盖哪些设备？

**答案**：两个副本分别覆盖 `gpuid_x=0` 与 `gpuid_x=1` 的两列 GPU。对固定 `gpuid_y` 的一行（两块 GPU），副本 0 放在 `(0, gpuid_y)`，副本 1 放在 `(1, gpuid_y)`；每块 GPU 持有完整的 4×8 本地段（`replicaGpuData` 对每块 GPU 枚举 4×8 全部单元，见 [_extra/demo/tile_distributed.html:L118-L126](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tile_distributed.html#L118-L126)）。

**练习 2**：把基础布局改成同时沿两根设备轴分片（fully sharded 的 `S[(2,4,2,4):(1@gpuid_y,4@m,1@gpuid_x,1@m)]`），再叠加 `R[2 : 1@gpuid_y]`，矩阵元素 `(r=6, c=3)`（扁平索引 `x = 6·8+3 = 51`）会落到哪些 `gpuid_y` 上？这个结果说明什么？

**答案**：`unflatten(51; 2,4,2,4) = (51//32, (51//8)%4, (51//4)%2, 51%4) = (1, 2, 0, 3)`，即 shard 给出 `gpuid_y = 1`、`gpuid_x = 0`、本地 `m = 2·4+3 = 11`；副本偏移 `r·1@gpuid_y`（`r ∈ {0,1}`）把 `gpuid_y` 变为 `1` 与 `2`。若 mesh 只有 2 行，第二份副本落到界外。这说明两点：其一，**副本轴可以和分片轴同名**——同一根轴既承载分片 stride 又承载副本 stride，求值时分别贡献、按轴相加；其二，布局必须保证「shard 结果 + 副本偏移」仍落在资源范围内（把 mesh 扩成 4 行即可让两份副本都合法），这正是 u4-l1「shape 决定切分、strides 决定摆放」约束在副本维上的延续。

**练习 3**：为什么 Layout API 演示用 `@pid` 而书正文用 `@gpuid_x`？这会影响布局语义吗？

**答案**：不会。命名轴的名字只是标识（[chapter_tirx_layout_api/index.md:L202-L204](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L202-L204) 明确说「轴名是布局的一部分；不同轴名即使数值相同也代表不同物理位置」），`@pid` 与 `@gpuid_x` 都指「设备编号」这一物理坐标。真正不能混淆的是**不同空间**的同值坐标，如 `1@laneid` ≠ `1@TLane`。

## 5. 综合实践

综合实践把本讲全部机制（shard 打包、跨 warp 广播的 replication、跨 CTA 的 replication、以及 offset 定位）串成一个问题：**为 2-CTA（cta_group::2）block-scaled MMA 的 scale factor 写出完整的布局表达式，并计算每个副本覆盖的 lane/column 区间。**

**背景与硬件约束**（依据 [chapter_tensor_cores/index.md:L226-L243](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L226-L243)）：

- SFA **沿 M 分片**到 CTA 对：even CTA 持有 `SFA[0:128, :]`，odd CTA 持有 `SFA[128:256, :]`；
- 每个 CTA 只把自己那一半 B 的 N 列搬进本地 SMEM，但协作 MMA 消费**完整** B tile，因此 `SFB[0:N, :]` 必须对两个 CTA 都可用——常见实现是先把 SFB 组播进 CTA 对的两份 SMEM，再用 `tcgen05.cp.cta_group::2` 从各自 SMEM 拷进各自 TMEM；
- 在每个 CTA **内部**，SFA 与 SFB 都还要经 `tcgen05.cp.32x128b.warpx4` 把 32-lane 基础布局复制到全部四个 32-lane TMEM 分区（`TLane 0-31 / 32-63 / 64-95 / 96-127`）。

设定：`M = 256`（两 CTA 各 128）、`N = 128`、`SF_K = 4`、scale factor 占 1 字节；TMEM 的 `@TCol` 以 buffer 元素（8-bit）为单位，4 个元素位置打包进一个 32-bit 硬件 Col（硬件 Col = `s//4`、字节 = `s%4`，见 [chapter_tirx_layout_api/index.md:L335](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L335)）。

**任务 A：单 CTA 内的 SFA 原子（打包 + 跨 warp 广播）。**

逻辑 tile 是 `128 行 × 4 个 K-block`，自然扁平索引为 `x = m·4 + sfk`。按 4.1 节规则：`m` 拆成 `Mgroup = m//32`（每组占 4 个元素列，`4@TCol`）与 `local_lane = m%32`（`1@TLane`），`sfk` 选字节（`1@TCol`）。代回 `x = (Mgroup·32 + local_lane)·4 + sfk = Mgroup·128 + local_lane·4 + sfk`，可见 iter 顺序必须是 `(Mgroup, local_lane, sfk)`、extents `(4, 32, 4)`——**iter 顺序由逻辑索引的嵌套分解决定，写反了映射就完全不同**（u4-l1「shape 决定 x 怎么切」的再现）。表达式（本讲推导，机制与书中原子 [chapter_tirx_layout_api/index.md:L321-L326](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L321-L326) 一致，只是把外层维度也写了出来）：

```text
SFA_atom = S[(4, 32, 4) : (4@TCol, 1@TLane, 1@TCol)] + R[4 : 32@TLane]
```

每个副本覆盖的区间（元素单位）：

| 副本 q | TLane 区间 | TCol 区间（元素） | 硬件 Col 区间 |
| --- | --- | --- | --- |
| 0 | 0–31 | 0–16 | 0–4 |
| 1 | 32–63 | 0–16 | 0–4 |
| 2 | 64–95 | 0–16 | 0–4 |
| 3 | 96–127 | 0–16 | 0–4 |

四个副本 TCol 区间相同——**复制沿 TLane，不占额外列**。验证：`SFA[64, 2]` → `x = 64·4+2 = 258` → `unflatten(258; 4,32,4) = (258//128, (258//4)%32, 258%4) = (2, 0, 2)`，即 `Mgroup=2`、`local_lane=0`、`sfk=2` → shard 坐标 `TLane=0, TCol = 2·4+2 = 10`（硬件 Col 2、字节 2），四副本 `(0,10)/(32,10)/(64,10)/(96,10)`，与书中 `(0,2,2)/(32,2,2)/(64,2,2)/(96,2,2)` 逐点对应。

**任务 B：扩展到 CTA 对——SFA 分片、SFB 复制。**

- SFA 沿 M 分片到两 CTA：在最外层加一个 extent-2 的 iter，stride 落在 CTA 编号轴上。轴名书中未统一规定（Layout API 演示用 `@pid`），本实践记作 `@ctarank`（**示例记号**）。逻辑坐标扩展为 `(ctarank, m_local, sfk)`，扁平 `x = ctarank·512 + m_local·4 + sfk`，iter 顺序相应变为 `(ctarank, Mgroup, local_lane, sfk)`：

  ```text
  SFA_pair = S[(2, 4, 32, 4) : (1@ctarank, 4@TCol, 1@TLane, 1@TCol)] + R[4 : 32@TLane]
  ```

  每个 SFA 元素共 **4** 个物理位置（1 个 CTA × 4 个分区）：`ctarank = m // 128` 定 CTA，CTA 内再复制 4 份。

- SFB 需要在**两个 CTA 都有完整副本**（跨 CTA 复制），CTA 内还要 4 分区复制，于是 `R` 有**两项**（基础部分与任务 A 的原子相同，因为 SFB 的逻辑 `(n, sfk)` 与 SFA 的 `(m, sfk)` 结构一致）：

  ```text
  SFB_pair = S[(4, 32, 4) : (4@TCol, 1@TLane, 1@TCol)]
             + R[4 : 32@TLane]        # CTA 内 .warpx4 四分区
             + R[2 : 1@ctarank]       # CTA 对组播
  ```

  每个 SFB 元素共 **8** 个物理位置（2 CTA × 4 分区）；每个副本覆盖：`ctarank ∈ {0,1}` × `TLane` 四个 32-lane 窗口 × `TCol` 元素区间 `0–16`（本 CTA 内）。两个 `R` 项的偏移互相独立、笛卡尔组合——这正是集合语义 \( L(x)=\{D(x)+r_1+r_2+O\} \) 的直接应用。

**任务 C：用 offset 把 scale factor 摆进指定 TMEM 列区。**

设该 CTA 的 TMEM 还要放累加器：累加器（32-bit 元素）占硬件列 0–223，scale factor 区域从硬件列 224 开始（本任务设定的规划；机制依据「offset 选定 tile 起始坐标」，[chapter_tirx_layout_api/index.md:L158](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L158)；真实内核的列划分讨论可参考 [L309-L315](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tirx_layout_api/index.md#L309-L315)）。SFA 是 8-bit buffer，起点换算成元素单位是 `224·4 = 896`。给任务 A 的原子追加 `+ 896@TCol`：

```text
SFA_atom' = S[(4, 32, 4) : (4@TCol, 1@TLane, 1@TCol)] + R[4 : 32@TLane] + 896@TCol
```

四个副本的 TCol 元素区间全部变为 `[896, 912)`（硬件 Col 224–228），TLane 区间不变——offset 平移**所有**副本，但不改变副本数量与 lane 分布。

**验证方式**（离线可完成）：

1. 用 4.2.4 的 `repl_eval.py` 分别求值三个表达式在若干元素上的输出：
   - `SFA_pair` 上取全局 `m=64, sfk=2`，即 `x = (64//128)·512 + (64%128)·4 + 2 = 258`，确认得到 4 个坐标、`ctarank` 恒为 0、四坐标只差在 `TLane`；
   - `SFB_pair` 上任取一元素（如 `x = 258`，对应 `n=64, sfk=2`），确认输出 8 个坐标、`ctarank` 取遍 {0,1}；
   - `SFA_atom'` 上确认 `TCol` 整体 +896、副本数仍为 4。
2. 有 Blackwell GPU 的读者（可选，**待本地验证**）：到 tirx-kernels 参考内核仓库（见 u1-l3）中找到 block-scaled GEMM 的 scale factor 布局定义，与自己写的表达式对照维度与副本数；注意 API 中 `layout.apply()` 只返回基础坐标，副本数需从 `layout.replica` 读取。
3. 把三张「副本 × 覆盖区间」表（任务 A/B/C）画成 TMEM 128×512 网格的着色草图（TLane 纵轴、TCol 横轴），标出 SFA 四个分区带与 SFB 的两个 CTA × 四个分区带。

**预期结果**：你能不看讲义复述以下链条——「SFA 沿 M 打包进 32 lane 并分片到 CTA 对（无跨 CTA 副本）；SFB 完整复制到两个 CTA；两者在 CTA 内都被 `.warpx4` 复制成 4 个 lane 窗口；`R` 管副本数量、`O` 管整体落位」——并能对任意元素秒答它的全部物理位置。

## 6. 本讲小结

- **Replication（`R[shape : strides]`）**：引入独立于逻辑索引的副本坐标 \( r \)，产生偏移 \( r\cdot s\ @\text{axis} \)；它不增加逻辑数据，只记录副本的物理位置。每个逻辑元素的物理位置数 = 各副本 extent 之积。
- **Offset（`O`）**：加到**每一个**被映射坐标上的固定平移；只挪位置、不产生副本。用途是选定 tile 起始坐标、把多个 tile 摆进同一硬件资源的不同区域。
- **集合语义**：\( L(x) = \{D(x) + r + O \mid r \in R\} \)——shard 定基础坐标、`R` 每副本一份、`O` 统一平移；`layout.apply()` 只算 \( D(x)+O \)，副本留在 `layout.replica` 交给 tile 操作处理。
- **跨 warp 广播现场**：block-scaled MMA（MXFP8/NVFP4）的 SFA/SFB 打包进 32-lane 基础 tile（`local_lane = m%32`、`TCol = m//32`、`byte = sfk`），`.warpx4` 沿 `R[4:32@TLane]` 复制到四个 32-lane 分区（`TLane = local_lane + 32p`）。注意「四个 Mgroup 沿 TCol 打包」与「四个分区沿 TLane 复制」是两组不同的「4」。
- **GPU mesh 映射**：设备轴（`@gpuid_x`/`@gpuid_y`/`@pid`）与 `@TLane`、`@laneid` 同为命名轴；fully sharded / shard+replica / shard+offset 三种摆放统一写进 `S + R + O` 一行表达式。
- **单位换算**：8-bit 元素布局的 `@TCol` 以 buffer 元素为单位，4 个元素位置打包进一个 32-bit 硬件 Col（硬件 Col = `s//4`、字节 = `s%4`）。

## 7. 下一步学习建议

本讲补全了布局记号的最后两块拼图（多副本与平移），下一讲将进入**同章的 Swizzle Layout**（u4-l4）：如何在**不改变逻辑形状**的前提下重排 SMEM 地址以消除 bank conflict——那里会出现第三种「地址变换」（XOR 置换），与本讲的 `R`/`O` 一样是挂在布局记号上的扩展。建议按以下顺序继续：

1. **u4-l4 Swizzle 布局**：先读 [chapter_data_layout/index.md:L430-L489](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L430-L489)，配合 `swizzle_8x8.html` 与 `swizzle_128B.html` 两个演示，并运行 `img/scripts/gen_swizzle_conflict.py` 重生成 bank conflict 图。
2. **u10（TIRx Layout API）**：本讲已预览了 `TileLayout(S + R + offset)` 与 `layout.replica`；u10 将系统讲解 `TileLayout.apply()`、命名轴族与 `ComposeLayout`，并把本讲手写的求值器换成真正的 `tvm.tirx.layout` 实现。
3. **u5-l3（Blackwell 数据路径）与 u7-l2（cta_group 与 block-scaled MMA）**：本讲综合实践只写了 SFA/SFB 的布局表达式；这两讲跟进数据如何真正进 TMEM——`tcgen05.cp` 的 `.32x128b.warpx4` 形态、`scale_vec` 的字内重复（注意它与 `R[4:32@TLane]` 是两回事，见 [chapter_layout_generations/index.md:L271-L283](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L271-L283)），以及 cta_group::1/2 下不同 M 配置的 TMEM lane 映射。
4. **源码延伸阅读**：`_extra/demo/tile_distributed.html` 与 `static/tirx-layout-demo/layout-demo.js` 都是「布局语义的可执行注释」，通读它们的映射函数是检验自己是否真懂 `R`/`O` 的最快方式。
