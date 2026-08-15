# 卷积通路：Img2col、SetFmatrix 与 Conv2d Forward

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 **im2col（image to column）** 技巧如何把二维卷积等价变换成矩阵乘，以及变换后 M、K 两个维度各自对应什么。
2. 掌握 PTO 中承载卷积配置的特殊 tile 类型 **ConvTile**，以及展开指令 **TIMG2COL** 的参数、约束与 `posM/posK` 滑窗语义。
3. 理解 **SETFMATRIX / SET_IMG2COL_RPT / SET_IMG2COL_PADDING** 三条配置指令各自写哪个硬件寄存器，以及 `SetFmatrixMode` 四种模式（A/B × AUTO/MANUAL）的分工。
4. 读懂 `kernels/manual/a2a3/conv2d_forward` 这个完整算子：多核切分、L1 caching、double buffer 与「TLOAD → TIMG2COL/TEXTRACT → TMATMUL → TSTORE」四级流水。
5. 能手算一层卷积的输出尺寸，并与 kernel、host 侧、golden 生成脚本三处代码交叉验证。

## 2. 前置知识

### 2.1 二维卷积与输出尺寸公式

卷积用一个小的权重窗口（卷积核，本讲例子中是 \(h_k \times w_k = 3 \times 3\)）在输入特征图上滑动，每个窗口位置做一次逐元素乘加。四个超参决定滑动方式：

- **stride（步长）**：窗口每次移动几个像素；
- **dilation（膨胀）**：核内采样点之间的间隔（间隔取样，等效放大感受野）；
- **padding（补边）**：在输入四周补几圈 0，用于控制输出尺寸、保留边界信息。

输出特征图尺寸由下面这个公式决定（本讲会在仓库里看到它在三个文件中同时出现）：

\[
h_{out} = \frac{h_{in} + pad_{top} + pad_{bottom} - dilation_h \cdot (h_k - 1) - 1}{stride_h} + 1
\]

\[
w_{out} = \frac{w_{in} + pad_{left} + pad_{right} - dilation_w \cdot (w_k - 1) - 1}{stride_w} + 1
\]

### 2.2 im2col：卷积 → 矩阵乘

直接滑窗做卷积难以复用第五讲（u5-l1）学过的 Cube 矩阵乘单元。经典做法是 **im2col**：把每个输出位置对应的输入感受野「拉直成一行/一列」，拼成一个大矩阵，卷积就变成了普通 GEMM：

- 矩阵的 **M 维** = 输出位置数 \(N_{batch} \times H_{out} \times W_{out}\)（每个输出像素是一行）；
- 矩阵的 **K 维** = 输入通道 × 核空间 \(C_{in} \times h_k \times w_k\)（感受野内所有输入元素是一行内的列）；
- 权重同样重排成 \([N_{out},\ C_{in} \cdot h_k \cdot w_k]\)，一次 TMATMUL 即得一层卷积。

代价是数据被「展开」后体积膨胀、有重复搬运，所以 PTO 把展开指令放在 **L1→L0A** 这一段（见 4.2），让膨胀只发生在片上。

### 2.3 两种五维布局

本讲的输入输出都用昇腾卷积惯用的 **NC1HWC0** 五维布局：把通道维 \(C\) 按 \(C_0 = 16\)（半精度下 32 字节对齐的最小分形）切块，变成 \([N, C_1, H, W, C_0]\)，其中 \(C = C_1 \times C_0\)。权重则用 **FRACTAL_Z** 分形布局（回顾 u2-l2：NZ 类分形是为 Cube 单元「边搬边摆」准备的摆放方式）。

### 2.4 承接前讲

本讲默认你已掌握：Tile 的位置类型（`Mat`→L1、`TileLeft/TileRight`→L0A/L0B、`TileAcc`→累加器，见 u5-l1）；TMATMUL/TMATMUL_ACC 的累加协议；四级流水（TLOAD→MTE1 切片→M→FIXPIPE 写回）与 `(srcPipe, dstPipe, eventId)` 事件配对（u2-l3、u5-l2/l3）。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [include/pto/common/pto_tile.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp) | `ConvTileShape`（最多 6 维的特征图形状）与 `ConvTile`（携带卷积超参的配置+数据 tile）定义 |
| [include/pto/common/pto_instr.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp) | TIMG2COL / SETFMATRIX / SET_IMG2COL_RPT / SET_IMG2COL_PADDING 的公共 API 薄壳 |
| [include/pto/common/type.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp) | `SetFmatrixMode` 枚举（A/B × AUTO/MANUAL 四种模式） |
| [include/pto/npu/a2a3/TImg2col.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TImg2col.hpp) | A2/A3 真机实现：约束检查、参数下发、`img2colv2_cbuf_to_ca` intrinsic |
| [include/pto/npu/a2a3/SetFmatrix.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/SetFmatrix.hpp) | SETFMATRIX 真机实现：把 fmap 尺寸与 pad 打包写入 FMATRIX 寄存器 |
| [include/pto/npu/a2a3/SetImg2colRpt.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/SetImg2colRpt.hpp) / [SetImg2colPadding.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/SetImg2colPadding.hpp) | 写 `l3d_rpt`（repeat 配置）与 `padding`（补边值）寄存器 |
| [include/pto/cpu/TImg2col.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TImg2col.hpp) | CPU 仿真实现：逐元素重算 im2col，是理解语义的最佳参考 |
| [kernels/manual/a2a3/conv2d_forward/](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward) | 完整 Conv2d 前向算子：kernel、host 入口、造数脚本、运行脚本 |
| [docs/isa/TIMG2COL.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TIMG2COL.md)、[docs/isa/SETFMATRIX.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/SETFMATRIX.md) | ISA 语义文档（约束与汇编形式） |

## 4. 核心概念与源码讲解

### 4.1 im2col 的行列映射与 ConvTile 配置载体

#### 4.1.1 概念说明

PTO 没有为卷积提供「一步到位」的指令，而是把它拆成「展开 + 矩阵乘」两步，展开这一步就是 TIMG2COL。要做展开，指令必须知道全部卷积超参（fmap 尺寸、stride、dilation、kernel 尺寸、四边 padding、补边值……）。PTO 的设计是：**把这些超参全部挂在源 tile 自己身上**——这就是 `ConvTile`。它既是数据（L1 上的一段缓冲），又是配置（一组 getter/setter 存的卷积参数），指令执行时直接从 tile 上读取，不必带一长串函数参数。

#### 4.1.2 核心流程

TIMG2COL 展开矩阵中第 \(m\) 行、第 \(k\) 列的取值规则（CPU 实现即按此公式逐元素计算）：

1. **行 → 输出像素**：把 \(m\) 按 \(N_{batch} \to D \to H_{out} \to W_{out}\) 的顺序逐层取模分解，得到输出坐标 \((n, h_{out}, w_{out})\)；
2. **列 → 感受野元素**：把 \(k\) 按 \(C_1 \to (h_k, w_k) \to C_0\) 的顺序分解，得到「哪个 C1 块、核内哪个位置、块内哪个通道」；
3. **回址取数**：输入地址 \(h_{in} = h_{out} \cdot stride_h + h_k^{off} \cdot dilation_h - pad_{top}\)，\(w_{in}\) 同理；若 \((h_{in}, w_{in})\) 落在特征图外，取 `padValue`，否则读源 tile。

即整条通路是「输出坐标 + 感受野偏移 → 输入坐标」的纯函数映射，输出尺寸公式自然嵌入其中。

#### 4.1.3 源码精读

**ConvTileShape：最多 6 维的特征图形状模板。** 与普通 `Tile` 的 `Rows × Cols` 不同，ConvTile 的形状直接就是特征图的逻辑形状，`DYNAMIC`（-1）占位的维度在构造时填入运行期值：

- [include/pto/common/pto_tile.hpp:1128-1151](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1128-L1151) — `ConvTileShape` 支持最多 6 维（NDC1HWC0 场景），静态维进类型、动态维进 `shape[]` 数组；这与 u2-l1 学过的 `Shape<DIM_0..DIM_4>` 是同一套混合静态/动态设计。
- [include/pto/common/pto_tile.hpp:1226-1246](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1226-L1246) — `ConvTile` 模板：`Loc`（TileType）、元素类型、`BufferSize`（字节数容量）、`Layout`、`Shape_`，外加编译期 `staticShape[]` 与运行期 `shape[]` 双轨形状。

**ConvTile 上的卷积超参存取。** 全部以普通成员变量 + inline getter/setter 实现：

- [include/pto/common/pto_tile.hpp:1314-1354](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1314-L1354) — 依次是 `fmapH/fmapW`（特征图高宽）、`padList_[4]`（四边补边）、`filterH/filterW`（核尺寸）、`dilationH/W`、`strideH/W`、`padValue`（补边填充值）、`channelSize`（本次参与展开的通道数）、`repeatStride/repeatTime/repeatMode`（硬件 repeat 配置）、`transpose`。
- [include/pto/common/pto_tile.hpp:1362-1369](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1362-L1369) — 私有成员定义处，`padList_[4]` 默认全 0。

**数学参考：golden 脚本里的纯 numpy im2col。** 造数脚本中的 `img2col_nhwc` 就是 4.1.2 行列映射的可执行版：

- [kernels/manual/a2a3/conv2d_forward/scripts/gen_data.py:52-88](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/scripts/gen_data.py#L52-L88) — 先 `np.pad` 补边，再对每个输出位置 \((n, h_{out}, w_{out})\) 收集感受野列，拼出 `col_matrix`，形状注释写明为 `[C_in*H_k*W_k, N*H_out*W_out]`——即 K×M。第 63-64 行就是输出尺寸公式。

#### 4.1.4 代码实践

**实践目标**：不依赖任何硬件，用 golden 脚本亲眼确认「卷积 → 两个矩阵」的形状映射。

1. **操作步骤**：进入 `kernels/manual/a2a3/conv2d_forward/scripts/`，先直接运行 `python3 gen_data.py`（生成 input/golden 二进制）；然后在该目录用 `python3 -c` 交互式导入 `img2col_nhwc`，对一个 \(1 \times 8 \times 8 \times 16\)（NHWC）输入、\(3 \times 3\) 核、padding 全 0 调用它，打印 `col_matrix.shape`。
2. **需要观察的现象**：col_matrix 形状应为 \((16 \times 3 \times 3,\ 1 \times 6 \times 6) = (144,\ 36)\)；把 padding 改为 \((1,1,1,1)\) 后变为 \((144,\ 64)\)（输出变 \(8 \times 8\)）。
3. **预期结果**：K = \(C_{in} \cdot h_k \cdot w_k\)、M = \(N \cdot H_{out} \cdot W_{out}\) 与 4.1.2 的分解完全一致；输入体积 \(8 \times 8 \times 16 = 1024\) 元素被展开成 \(144 \times 36 = 5184\)，直观体现 im2col 的膨胀代价。
4. 若你的 numpy 环境异常无法运行，标注「待本地验证」，改为通读 `img2col_nhwc` 第 75-87 行的双重循环逐行核对。

#### 4.1.5 小练习与答案

**练习 1**：默认用例（`hin=16, win=96, hk=wk=3, stride=1, dilation=1, pad=1`）下，展开矩阵的 M 和 K 各是多少？
**答案**：\(h_{out} = (16+1+1-2-1)/1+1 = 16\)，\(w_{out} = (96+1+1-2-1)/1+1 = 96\)；M = \(4 \times 16 \times 96 = 6144\)，K = \(32 \times 16 \times 3 \times 3 = 4608\)。这与 kernel 模板参数 `m=6144, k=4608`（4.4 节）严丝合缝。

**练习 2**：为什么 `ConvTile` 要把卷积超参挂在自己身上，而不是像 TMATMUL 那样全走函数参数？
**答案**：卷积超参多达十余个（fmap 尺寸、四边 pad、stride、dilation、核尺寸、padValue、repeat 配置……），塞进函数签名会让每条调用点冗长且易错；挂在 tile 上后，「这块 L1 数据按什么卷积参数解释」与数据本身绑定，TIMG2COL 只需 `(dst, src, posM, posK)` 四个参数，同一 tile 也可在多次展开间复用/微调（如 conv2d_forward 每个 m 块只改 `padList` 的上下边）。

### 4.2 TImg2col 指令：一条指令完成「展开 + L1→L0A 搬运」

#### 4.2.1 概念说明

朴素 im2col 要先把展开矩阵在内存里物化出来再喂给 Cube，多一次完整搬运。TIMG2COL 把两步合成一步：**源是 L1 上的 ConvTile（原始特征图），目的直接是 L0A 上的 `TileLeft`（展开后的矩阵）**，展开在搬运途中由硬件完成。它等价于「TEXTRACT + im2col」——对比 4.4 节 kernel 里权重走 `TEXTRACT`（纯切片）而特征图走 `TIMG2COL`（切片 + 展开），两条指令挂在同一条 MTE1 流水线上。

`posM/posK` 是滑窗坐标：L1 里缓存的特征图只能展开出 im2col 大矩阵的一个局部，`(posM, posK)` 指明本次要取「全局展开矩阵」中从第 posM 行、第 posK 列开始的子块。

#### 4.2.2 核心流程

公共 API 仍是熟悉的三段式薄壳：

```text
TIMG2COL(dst, src, posM, posK, events...)
  ├─ TSYNC(events...)          # 等待传入的依赖事件（u2-l3）
  ├─ TIMG2COL_IMPL(dst, src, posM, posK)
  │    ├─ static_assert 编译期契约（类型/布局/位置）
  │    ├─ [仅 AUTO 模式] 自动 SETFMATRIX/SET_IMG2COL_RPT/SET_IMG2COL_PADDING
  │    ├─ stepM = dst.GetValidRow()
  │    ├─ stepK = CeilAlignment(dst.GetValidCol(), c0Size)   # 列数向上对齐到 C0
  │    └─ img2colv2_cbuf_to_ca(dst, src, stepK, stepM, posK, posM, ...)  # 硬件 intrinsic
  └─ return RecordEvent
```

注意 NPU 实现里 `stepM/stepK` 传给 intrinsic 时顺序是 `(stepK, stepM)`，且 stepK 按 `c0Size`（32 字节块内的元素数）向上对齐——展开矩阵的列必须凑满整个 \(C_0\) 分形，不满处由硬件补齐。

#### 4.2.3 源码精读

**公共 API 薄壳。**

- [include/pto/common/pto_instr.hpp:907-916](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L907-L916) — `TIMG2COL(dst, src, posM=0, posK=0, events...)`：模板参数 `FmatrixMode` 默认 `FMATRIX_A_MANUAL`，函数体就是 TSYNC + `TIMG2COL_IMPL` 转发，返回 `RecordEvent`。与 TMATMUL 等指令完全同构（u2-l4 的三层结构）。

**A2/A3 真机实现。**

- [include/pto/npu/a2a3/TImg2col.hpp:91-108](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TImg2col.hpp#L91-L108) — `TIMG2COL_IMPL` 的编译期契约：源必须是 `TileType::Mat`（L1）且布局 `NC1HWC0/NDC1HWC0`；目的必须是 `TileLeft`（L0A）且 SLayout/BLayout 行主序；源目元素类型必须相同；dtype 白名单为 `int8_t/half/bfloat16_t/float`。违反任何一条直接编译失败——回顾 u3-l4 的结论：CPU 仿真检查较松，真机契约在 `*_IMPL` 层拦截。
- [include/pto/npu/a2a3/TImg2col.hpp:109-113](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TImg2col.hpp#L109-L113) — **AUTO 模式的核心差异**：若 `FmatrixMode` 为 `FMATRIX_A_AUTO/B_AUTO`，这里自动从 ConvTile 读参并依次调用 `SetFmatrix/SetRepeat/SetPadding` 写硬件寄存器；MANUAL 模式则什么都不做——寄存器由用户自己提前用 4.3 节的配置指令写好。
- [include/pto/npu/a2a3/TImg2col.hpp:114-120](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TImg2col.hpp#L114-L120) — `stepM` 取目的 tile 有效行、`stepK` 对齐到 `c0Size` 后，连同 ConvTile 上的 stride/dilation/filter/transpose/channelSize 一起传入底层 `TImg2col` 函数。
- [include/pto/npu/a2a3/TImg2col.hpp:67-89](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TImg2col.hpp#L67-L89) — 最内层：取 `__cbuf__`（L1）源指针与 `__ca__`（L0A）目的指针，处理 filterW/H 超过 255 时的高低位拆分，最终落到 `img2colv2_cbuf_to_ca` intrinsic。命名直译就是「cbuf(L1) → ca(L0A) 的 img2col」，印证 4.2.1 的通路判断。

**CPU 仿真实现（语义的金标准）。**

- [include/pto/cpu/TImg2col.hpp:110-145](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TImg2col.hpp#L110-L145) — CPU 版 `TIMG2COL_IMPL`：对目的有效区逐元素双重循环，行分解 \(mIndex = posM + r\) → \((n, d, outRow, outCol)\)，列分解 \(kIndex = posK + c\) → \((c1, kernelH, kernelW, c0)\)，再调 `CalculateValue` 回址取数。它不写任何寄存器（`(void)FmatrixMode` 直接丢弃模式参数），因为参数就存在 ConvTile 字段里，直接读即可。
- [include/pto/cpu/TImg2col.hpp:147-166](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TImg2col.hpp#L147-L166) — `CalculateValue`：默认取 `padValue`；按 \(h_{in} = outRow \cdot stride_h + kernelH \cdot dilation_h - pad_{top}\) 回址，越界（`inputH/inputW` 不在 `[0, fmapH/fmapW)` 内）则保持补边值，否则经 `GetInputOffset` 读源数据。
- [include/pto/cpu/TImg2col.hpp:78-108](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TImg2col.hpp#L78-L108) — `ExtractImg2ColParams`：从 ConvTile 抽出全部超参并在第 100-105 行重新实现了一遍输出尺寸公式——**这是公式在仓库里的第 4 处出现**（kernel、main、gen_data、cpu 仿真各一处），四处一致本身就是很好的交叉验证素材。

#### 4.2.4 代码实践

**实践目标**：追踪单个元素的映射，确认你真的读懂了行列分解。

1. **操作步骤**：对照 [include/pto/cpu/TImg2col.hpp:122-144](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TImg2col.hpp#L122-L144)，设 `posM=96, posK=16`，`dst(r=0, c=0)`；手算：`mIndex=96`，在 \(H_{out}=16, W_{out}=96\)（练习 1 的默认用例）下 `mIndex % (16*96) = 96` → `outRow = 96/96 = 1, outCol = 0`；`kIndex=16`，在 \(C_0=16,\ h_k=w_k=3\) 下 `kernelOffset = (16 % 144)/16 = 0, c0Index = 0` → 核内左上角、第 0 通道。
2. **需要观察的现象**：该元素回址 \(h_{in} = 1 \cdot 1 + 0 \cdot 1 - 1 = 0\)，\(w_{in} = 0\)，即读输入 `(h=0, w=0, ch=0)`——输出第 1 行第 0 列像素的感受野左上角，正是卷积定义。
3. **预期结果**：换 `posK=32` 再算一次，应得到 `kernelOffset = 1`（核内 (0,1) 位置），\(w_{in} = 1\)。两个手算都对，说明分解顺序 \(C_1 \to (h_k,w_k) \to C_0\) 已被你掌握。
4. 本实践为纯源码阅读 + 手算，无需运行环境。

#### 4.2.5 小练习与答案

**练习 1**：为什么 TIMG2COL 的目的 tile 必须是 `TileLeft`（L0A），而不能是 `TileType::Vec`（UB）？
**答案**：TIMG2COL 的产物是 im2col 矩阵，唯一消费者是 Cube 矩阵乘的 A 操作数；而 TMATMUL 要求 A 在 L0A（u5-l1）。让展开直达 L0A，避免了「L1→UB→L0A」的二次搬运，也让 MTE1 流水线（它和 TEXTRACT 一样挂 MTE1）与 Cube 计算可以按事件重叠。

**练习 2**：`posM/posK` 在 4.4 节 kernel 中的实际取值是什么含义？
**答案**：kernel 中调用为 `TIMG2COL(aTile[flag], fmapMat[idx], woutStart, kModStepKa * baseK)`——`posM = woutStart` 是当前 m 块起始输出像素在整行内的列偏移，`posK = kModStepKa * baseK` 是当前 k 块在 L1 缓存所覆盖的 K 范围内的列偏移；两者合起来把「全局 im2col 矩阵的 [baseM, baseK] 子块」定位出来（见 4.4.3）。

### 4.3 SetFmatrix/SetImg2col 系列：三个寄存器与四种模式

#### 4.3.1 概念说明

A2/A3 硬件执行 img2col 时，卷积超参并不都走指令操作数，有一部分要预先写进三个硬件配置寄存器：

| 寄存器 | 内容 | 写入指令 |
| :--- | :--- | :--- |
| FMATRIX（`set_fmatrix` / `set_fmatrix_b`） | 特征图宽 W(16bit)、高 H(16bit)、四边 padList(4×8bit) | SETFMATRIX |
| L3D_RPT（`set_l3d_rpt`） | repeatStride(16bit)、repeatTime(8bit)、repeatMode(8bit) | SET_IMG2COL_RPT |
| PADDING（`set_padding`） | 补边填充值（按数据位宽复制/直通打包） | SET_IMG2COL_PADDING |

`SetFmatrixMode` 的四值枚举决定**谁来写这些寄存器**：

- `FMATRIX_A_MANUAL`（默认）：用户在 TIMG2COL 前自己调 SETFMATRIX 等三条指令；
- `FMATRIX_A_AUTO`：TIMG2COL 内部自动从 ConvTile 读参写寄存器，三条 SET 指令变成空操作；
- `FMATRIX_B_*`：同上两档，但写 `set_fmatrix_b`（B 路寄存器），供需要第二组特征图配置的场景使用，具体场景为后端实现定义。

配置指令的数据来源统一是 ConvTile 的字段——「SET 指令 = 把 tile 上的配置投影到寄存器」。

#### 4.3.2 核心流程

以 SETFMATRIX 为例，寄存器打包是纯位域拼接：

```text
regFmatrix[63:0]
  = fmapW[15:0] | fmapH[31:16] | padList[0][39:32] | padList[1][47:40]
  | padList[2][55:48] | padList[3][63:56]
        ↓
  set_fmatrix(regFmatrix)   // 或 set_fmatrix_b
```

L3D_RPT 同理：`repeatStride[15:0] | repeatTime[23:16] | repeatMode[31:24]`。PADDING 按 `sizeof(DType)` 分三档：1 字节时把同一字节复制成高低两份（适配硬件按 16bit 通道单元取值），2/4 字节时直接整型直通。

#### 4.3.3 源码精读

**模式枚举。**

- [include/pto/common/type.hpp:339-344](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp#L339-L344) — `enum class SetFmatrixMode` 四个值：`FMATRIX_A_AUTO / FMATRIX_B_AUTO / FMATRIX_A_MANUAL / FMATRIX_B_MANUAL`。

**SETFMATRIX 真机实现：位域打包。**

- [include/pto/npu/a2a3/SetFmatrix.hpp:15-38](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/SetFmatrix.hpp#L15-L38) — 仅在 MANUAL 两档生效；`fmapW` 占低 16 位、`fmapH` 左移 16、`padList[0..3]` 从第 32 位起每项 8 位；A_MANUAL 走 `set_fmatrix`，B_MANUAL 走 `set_fmatrix_b`。
- [include/pto/npu/a2a3/SetImg2colRpt.hpp:15-24](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/SetImg2colRpt.hpp#L15-L24) — SET_IMG2COL_RPT 实现：`repeatStride | repeatTime<<16 | repeatMode<<24` 打包写 `set_l3d_rpt`，同样只在 MANUAL 档生效。
- [include/pto/npu/a2a3/SetImg2colPadding.hpp:15-33](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/SetImg2colPadding.hpp#L15-L33) — SET_IMG2COL_PADDING 实现：按 `sizeof(DataType)` 1/2/4 字节三档打包 `padValue` 写 `set_padding`；int8 场景把单字节复制到高低字节。

**公共 API 与架构分档。**

- [include/pto/common/pto_instr.hpp:918-924](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L918-L924) — `SETFMATRIX(src, events...)` 薄壳，转发 `SETFMATRIX_IMPL`。
- [include/pto/common/pto_instr.hpp:942-975](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L942-L975) — `SET_IMG2COL_RPT/PADDING` 按架构用两段 `#if` 各定义一次：A2A3+KirinX90 一套、A5+Kirin9030+`__CPU_SIM` 一套，签名相同。回顾 u2-l4 的「架构 × 后端」互斥编译分层。

**CPU 仿真端：SET 指令退化为断言。**

- [include/pto/cpu/SetFmatrix.hpp:14-19](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/SetFmatrix.hpp#L14-L19) — CPU 版 `SETFMATRIX_IMPL` 只做 `PTO_CPU_ASSERT(fmapH>0 && fmapW>0)`，不写任何寄存器：CPU 的 TIMG2COL 直接读 ConvTile 字段（4.2.3），寄存器机制纯属真机细节。
- [include/pto/npu/a2a3/TImg2col.hpp:40-65](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TImg2col.hpp#L40-L65) — `SetRepeat/SetPadding` 内部函数：AUTO 模式下由 TIMG2COL_IMPL 第 109-113 行调用的正是这两个函数加上文件首部的 `SetFmatrix`（[第 16-38 行](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TImg2col.hpp#L16-L38)），与 4.3.1 表格中三条 SET 指令的实现体一一对应——AUTO/MANUAL 的差别只是「调用时机在 TIMG2COL 内部还是外部」。

#### 4.3.4 代码实践

**实践目标**：搞清楚选不同 `FmatrixMode` 时你需要多写/少写哪些指令。

1. **操作步骤**：阅读 4.4.3 节将看到的 kernel 写法——`SETFMATRIX(fmapMat[0])` 显式调用一次（MANUAL 风格）。假设把它删掉并把 `TIMG2COL` 的模板实参改成 `FMATRIX_A_AUTO`，列出指令序列的变化。
2. **需要观察的现象**：MANUAL 档下 SETFMATRIX/SET_IMG2COL_RPT/SET_IMG2COL_PADDING 各自生效写寄存器；AUTO 档下这三条 SET 变空操作（其 IMPL 里的 `if constexpr` 分支不命中），改由 TIMG2COL_IMPL 内部自动完成同样三件事（[TImg2col.hpp:109-113](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TImg2col.hpp#L109-L113)）。
3. **预期结果**：结论是两档最终写入的寄存器内容相同；工程上 MANUAL 档可以在「参数不变的多轮循环外只写一次寄存器」来省指令（kernel 正是这么做的，每 m 块 SETFMATRIX 一次、循环内 kIter 多轮复用），AUTO 档胜在不易漏配。CPU 仿真下两种写法结果完全一致（SET 均为空操作/断言），差异只在真机指令数——「待本地验证」于真机。
4. 本实践为源码阅读型，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`regFmatrix` 是 64 位，`fmapH/fmapW` 各占 16 位、`padList` 4 项各 8 位，刚好占满。由此推断 fmapW 的上限是多少？
**答案**：16 位无符号，上限 65535；这也解释了 [TImg2col.hpp:81-84](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TImg2col.hpp#L81-L84) 为什么对 `filterW/H > 255` 要做高低位拆分——filter 尺寸参数在 intrinsic 里只有 8 位低位通道，超限部分走额外参数传递。

**练习 2**：为什么 CPU 仿真端保留 SETFMATRIX 的调用形式，却只做断言？
**答案**：保持 kernel 源码「一份代码、多后端编译」（u2-l4 的核心承诺）。若 CPU 端删掉这个符号，同一份 kernel 在 `__CPU_SIM` 下就编译不过；保留接口、掏空实现，既维持了 API 一致性，又顺手在仿真期拦截 `fmapH/fmapW` 忘配置（=0）这类低级错误。

### 4.4 conv2d_forward 完整算子：四级流水、L1 caching 与 double buffer

#### 4.4.1 概念说明

`kernels/manual/a2a3/conv2d_forward` 把本讲三条指令组装成一个生产级卷积前向。它的总骨架与 u5-l3 的 gemm_performance 同源——**卷积在这里就是一次布局特殊的 GEMM**——但 A 矩阵（特征图展开）的 L1→L0 段从 `TEXTRACT` 换成了 `TIMG2COL`。README 总结的四个优化手段：多核切分（24 核 4×6 划 M/N）、base block 选择（`[128, 256, 48]`）、L1 caching（`stepKa=stepKb=3`，一次搬 3 个 k 块）、double buffer（L1/L0A/L0B 三级乒乓）。

#### 4.4.2 核心流程

单个核内一次 m 块的计算（`mLoop × nLoop × kLoop` 三重循环）：

```text
for mIter:                                  # 沿 M(=batch*hout*wout) 切 baseM=128
    计算本块覆盖的输出行范围 → 反推输入行窗口 [hinStart, hinEnd]
    为 fmapMat 填 ConvTile 参数(fmapH/W、filter、四边 pad、channelSize)
    SETFMATRIX(fmapMat[0])                  # MANUAL 档：寄存器只写一次
    for nIter:                              # 沿 N 切 baseN=256
        for kIter:                          # 沿 K(=cin*hk*wk) 切 baseK=48
            每 stepKa=3 轮才 TLOAD 一次     # MTE2: GM→L1, 一次搬 3 个 k 块
            TIMG2COL(aTile, fmapMat, woutStart, kModStepKa*baseK)  # MTE1: 展开+切片→L0A
            TEXTRACT (bTile, weightMat, ...)                         # MTE1: 纯切片→L0B
            TMATMUL / TMATMUL_ACC          # M: 首轮初始化、后续累加
        TSTORE                              # FIX: 写回 GM
```

核间按 `blockIdx` 做 4×6 划分（M 方向 4 组、N 方向 6 组），每核输出互不重叠、零核间同步——与 u5-l2 基线 GEMM 相同的多核哲学。L1 caching 的收益在于：同一片输入特征图被连续 3 个 k 块共用，`TIMG2COL` 用不同的 `posK` 从同一块 L1 数据里三次取数，GM 搬运量摊薄为 1/3。

#### 4.4.3 源码精读

**tile 类型与缓冲摆放。**

- [kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp:257-266](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp#L257-L266) — `channelSize = Ceil(stepKa*baseK, hk*wk)`（一次 L1 缓存覆盖的通道数），据此定义 `TileMatAData = ConvTile<Mat, U, bufferSizeA, NC1HWC0, ConvTileShape<1, channelSize/c0, -1, win, c0>>`（H 维动态）与 `TileMatBData = ConvTile<Mat, U, bufferSizeB, FRACTAL_Z, ...>`（权重，纯数据用途）。
- [kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp:270-279](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp#L270-L279) — `TileLeft/TileRight/TileAccCompact` 分别是 L0A/L0B/累加器 tile；`TASSIGN` 把 L0A/L0B 两个乒乓槽摆在 `0x0` 与 `0x0 + 32KiB`（`L0_PINGPONG_BYTES`，与 u5-l3 相同的 L0 半区约束）。回顾 u3-l2：Manual 模式下摆放地址是开发者的责任。

**ConvTile 参数填充与 SETFMATRIX（4.3 的实战现场）。**

- [kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp:215-231](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp#L215-L231) — 对每个乒乓槽的 fmapMat：`SetFmapH(hinCount)/SetFmapW(win)/SetChannelSize/SetFilterH(3)/SetFilterW(3)`；四边 pad 中**左右边硬编码 1**（`SetPadList(0,1)/(1,1)`），**上下边按本 m 块与图像边界的关系动态计算**（第 225-226 行：块首行之前的补边、块尾行之后超出图像的补边）——im2col 的 pad 语义被拆到「整图边界」上，块内不重复补。随后 `SETFMATRIX(fmapMat[0])` 一次写寄存器，循环内所有 TIMG2COL 复用。
- [kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp:204-213](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp#L204-L213) — m 块 → 输出像素行范围（`mStart/wout → houtStart/houtEnd`）→ 反推输入行窗口 `hinStart/hinEnd` 的换算，`hinStart = Max(0, houtStart*strideH - padTop)` 正是 4.2 回址公式的逆过程。

**主循环：TIMG2COL 与 TEXTRACT 并肩工作。**

- [kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp:146-179](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp#L146-L179) — `ProcessKIteration`：每 `stepKa=3` 轮 kIter 才构造一次 fmap/weight 的 GlobalTensor 视图并 `TLOAD` 进 L1 乒乓槽（`SetFlag<PIPE_MTE2, PIPE_MTE1>` 用 0/1 两个编号分别标记 A/B 两条搬运完成）；否则直接进入矩阵乘阶段。
- [kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp:114-145](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp#L114-L145) — `MacroMatmul`：先 `WaitFlag<PIPE_M, PIPE_MTE1>` 等 Cube 用完 L0 槽；第 129 行 `TIMG2COL(aTile[mte1DBFlag], fmapMat[currMte2Idx], woutStart, kModStepKa*baseK)` 完成「L1 特征图 → L0A 展开子块」，第 132 行 `TEXTRACT` 同期把权重切片进 L0B；之后 `SetFlag/WaitFlag<PIPE_MTE1, PIPE_M>` 握手交棒给 `TMATMUL/TMATMUL_ACC`（`MatmulAcc` 见[第 37-45 行](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp#L37-L45)，首轮清零、后续累加，即 u5-l1 的 split-K 累加协议）。三条流水（MTE2/MTE1/M）靠事件编号 + 乒乓槽位交替重叠。
- [kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp:97-113](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp#L97-L113) — `InitSyncFlags/WaitSyncFlags`：循环首尾补发/补等反向同步事件，保证「最后一次反向等待」有牌可等（u5-l3 同款技巧）。

**写回与 host 侧。**

- [kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp:79-95](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp#L79-L95) — `StoreResult`：输出按 NC1HWC0 的 5 维 shape/stride 构造 GlobalTensor 视图后 `TSTORE`，`PIPE_M→PIPE_FIX` 事件对保护「累加器未写完不搬运」。
- [kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp:305-345](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp#L305-L345) — `launchConv2dForward`：全部 tiling 参数以 `constexpr` 固化（`m=6144, k=4608, n=6144`，与练习 1 手算一致）；第 339-340 行用输出尺寸公式算出 `hout/wout` 再传入 kernel 模板。**注意**：模板实参列表在第 299 行传到 `wout` 为止，`stride/pad` 系列形参走模板默认值 1——想改 padding 生效，只改这里的 constexpr 变量是不够的（见 4.4.4）。
- [kernels/manual/a2a3/conv2d_forward/main.cpp:28-31](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/main.cpp#L28-L31) 与 [main.cpp:88-104](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/main.cpp#L88-L104) — host 侧用同一公式分配输出内存；golden 比对在第 75-85 行（容差 0.001）。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：修改 padding 参数并手算一层输出尺寸，与程序输出对比。

**第一层：手算 + 三处代码交叉验证（零依赖，必做）**

1. **操作步骤**：
   - 用第 2.1 节公式手算两组输出尺寸：
     - 默认：\(h_{in}=16, w_{in}=96\)，pad 全 1，\(h_k=w_k=3\)，stride/dilation=1 → \(h_{out}=16, w_{out}=96\)；
     - 改 pad 全 0 → \(h_{out} = (16-2-1)+1 = 14\)，\(w_{out} = (96-2-1)+1 = 94\)。
   - 在仓库中定位同一公式的四处实现并核对取参：[conv2d_forward_kernel.cpp:339-340](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp#L339-L340)、[main.cpp:29-30](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/main.cpp#L29-L30)、[gen_data.py:63-64](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/scripts/gen_data.py#L63-L64)、[cpu/TImg2col.hpp:100-105](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TImg2col.hpp#L100-L105)。
2. **需要观察的现象**：默认参数下程序各处应一致得到 \(16 \times 96\)；同时确认 \(m = batch \cdot h_{out} \cdot w_{out} = 4 \times 16 \times 96 = 6144\) 与 tiling 表里的 `m=6144` 对应。
3. **预期结果**：手算、kernel 模板参数、host 内存分配、golden 造数四者闭合；若 pad 改 0，则 \(m\) 应变为 \(4 \times 14 \times 94 = 5264\)——这是第二层改动的检查锚点。

**第二层：本机运行 golden 脚本（有 numpy 即可）**

1. **操作步骤**：把 [gen_data.py:156-164](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/scripts/gen_data.py#L156-L164) 中 case 的 `padding=(1,1,1,1)` 改为 `(0,0,0,0)`，运行 `python3 gen_data.py`；用 `ls -l output/golden.bin` 观察文件大小。
2. **需要观察的现象**：golden 字节数从 \(4 \cdot 16 \cdot 96 \cdot 6144 \cdot 2\)（fp16，\(N_{out}=6144\)）按 \(h_{out} \times w_{out}\) 的缩减比例 \(14 \times 94 / (16 \times 96)\) 相应变小。
3. **预期结果**：文件大小比例印证手算的 \(h_{out}=14, w_{out}=94\)。（改完后建议还原 `(1,1,1,1)`，避免影响后续真机运行。）

**第三层：真机/sim 运行 kernel（需 CANN 环境，选做）**

1. **操作步骤**：按 [README.md:109-134](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/README.md#L109-L134)：`source ${ASCEND_INSTALL_PATH}/bin/setenv.bash` → `python3 scripts/gen_data.py` → `bash run.sh -r npu -v Ascend910B1`（`run.sh` 亦接受 `-r sim` 走 CANN 仿真器，见 [run.sh:40-55](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/run.sh#L40-L55)）。若要真正改 padding，需同步修改：① `launchConv2dForward` 的 constexpr pad 值并显式传入 kernel 模板（或改模板默认值）；② `Compute` 内 [SetPadList(0/1) 的硬编码 1](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp#L223-L224)（左右边）；③ main.cpp 与 gen_data.py 的默认参数；④ `m` 等衍生 tiling 量。
2. **需要观察的现象**：成功时输出 `test success`（golden 比对通过）；漏改上述任何一处，典型症状是边界行/列数值错或维度不匹配导致比对失败。
3. **预期结果**：默认参数下 `test success`；padding=0 全链改动正确后同样 `test success`，且输出尺寸为 \(4 \times 384 \times 14 \times 94 \times 16\)。本层结果**待本地验证**（需要昇腾硬件或 C simulator，本次未运行）。

#### 4.4.5 小练习与答案

**练习 1**：README 性能表里 TLOAD 占比高达 75%~81%，TSTORE 只有 1.7%~5.6%，说明什么？
**答案**：与 u5-l3 的判读方法一致——输入侧（特征图 + 权重）是内存瓶颈的主要来源，输出只写一次且被 \(h_k \cdot w_k \cdot C_{in}\) 倍计算摊薄；TMATMUL 占比 86%~91% 说明整体已接近 Cube Bound。TLOAD 高的另一个原因是 im2col 展开使特征图存在感受野重叠（相邻输出行共享输入行），L1 caching（stepKa=3）正是为了摊薄这部分重复搬运。

**练习 2**：为什么特征图走 TIMG2COL 而权重走 TEXTRACT？
**答案**：权重本来就是 \([C_{in} \cdot h_k \cdot w_k,\ N_{out}]\) 的「矩阵」（FRACTAL_Z 布局），K 迭代只需按行切片——TEXTRACT 足够；特征图是 5-D 图像，必须先做 im2col 展开才成为矩阵，TIMG2COL 把「展开 + 切片 + L1→L0A」合成一条指令，省去物化中间矩阵。

**练习 3**：`Compute` 中 `SetPadList(2, Max(0, padTop - houtStart*strideH))` 为什么可能为 0？
**答案**：padList 的 top 值描述「本块 L1 缓存的特征图上边界相对整图补边」。当 m 块的起始输出行不在图像第一行（`houtStart*strideH ≥ padTop`）时，L1 窗口上边界落在真实图像内部，无需再补边，故取 0；整图级的 pad 只在覆盖图像边界的块上出现。这体现了「分块后 pad 语义要按窗口重新折算」的实现细节。

## 5. 综合实践

**任务：给 conv2d_forward 写一份「卷积 → GEMM」映射说明书，并用 stride=2 验证你的理解。**

1. **画数据流图**：以默认用例为对象，画出从 GM 的 `x1_gm.bin`（NC1HWC0）到最终 `output_z.bin` 的完整数据流，标出每一级存储（GM→L1→L0A/L0B→Acc→GM）、每条边对应的指令（TLOAD/TIMG2COL/TEXTRACT/TMATMUL(_ACC)/TSTORE）及其流水线归属（MTE2/MTE1/M/FIX），并在 L1→L0A 那条边上标注 `posM/posK` 的含义。
2. **算维度**：把 stride 改为 \(2 \times 2\)（仅手算，不要求改代码），推出新的 \(h_{out}, w_{out}, m\)，以及 M 维分解中 `mIndex → (n, outRow, outCol)` 每步的除数/模数变化。
3. **验证**：用第 4.1.4 节的方法跑一次 `img2col_nhwc`（stride=(2,2)），确认你推出的 \(H_{out} \times W_{out}\) 与 numpy 输出的列数一致；再对照 [cpu/TImg2col.hpp:122-144](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TImg2col.hpp#L122-L144) 的分解代码核对第 2 步的除数链。
4. **思考题（选做）**：若把 `stepKa` 从 3 改为 1，`channelSize`、`bufferSizeA`、TLOAD 次数各怎么变？L1 caching 的收益还在吗？（提示：回到 [conv2d_forward_kernel.cpp:257-266](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp#L257-L266) 的公式，并对照 README 的 TLOAD 占比数据。）

## 6. 本讲小结

- **im2col 把卷积变成 GEMM**：M = \(N_{batch} \cdot H_{out} \cdot W_{out}\)（输出像素），K = \(C_{in} \cdot h_k \cdot w_k\)（感受野元素）；输出尺寸公式在 kernel、host、golden 脚本、CPU 仿真四处出现且一致。
- **ConvTile 是「数据 + 卷积配置」二合一的 tile**：形状是特征图逻辑形状（最多 6 维、支持 DYNAMIC），fmap 尺寸、四边 pad、stride、dilation、核尺寸、padValue、repeat 配置全部挂在 tile 字段上。
- **TIMG2COL = im2col 展开 + L1→L0A 搬运**：源是 L1 的 `ConvTile<Mat, NC1HWC0>`，目的必须是 `TileLeft`；`posM/posK` 从 L1 缓存展开出的局部矩阵中定位子块；CPU 实现按「行分解输出坐标、列分解感受野、回址取数」逐元素重算，是语义金标准。
- **三条 SET 配置指令写三个硬件寄存器**：SETFMATRIX（fmapH/W + padList 打包 64 位写 `set_fmatrix(_b)`）、SET_IMG2COL_RPT（repeat 三件套写 `l3d_rpt`）、SET_IMG2COL_PADDING（补边值写 `padding`）；`SetFmatrixMode` 的 AUTO 档让 TIMG2COL 内部自动写寄存器，MANUAL 档由用户在循环外写一次复用，CPU 仿真下全部退化为空操作/断言。
- **conv2d_forward = 布局特殊的 GEMM**：多核 4×6 切 M/N、base block [128,256,48]、stepKa/stepKb=3 的 L1 caching、三级 double buffer 与事件编排，整体与 gemm_performance 同构，唯一结构性差异是 A 通路用 TIMG2COL 替代 TEXTRACT。

## 7. 下一步学习建议

- **u5-l5（MX 混合精度矩阵乘）**：继续 Cube 家族的最后一块拼图，看 TMATMUL_MX 如何在 A5 上把缩放因子引入矩阵乘。
- **通读 [docs/coding/tutorials/](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/tutorials) 与 [docs/isa/SET_IMG2COL_RPT.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/SET_IMG2COL_RPT.md)、[docs/isa/SET_IMG2COL_PADDING.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/SET_IMG2COL_PADDING.md)**：补齐本讲两条 SET 指令的 ISA 文档细节。
- **对比阅读 A5 实现**：`include/pto/npu/a5/` 下的 TImg2col（若存在对应实现，见 include/README.md 的逐指令支持表）——A5 放宽了 dtype 白名单（TIMG2COL 文档 Constraint 一节），正好用 u2-l4 学到的 arch_capability 视角审视代际差异；这也是 u11-l2 架构适配的前菜。
- **性能侧延伸**：带着本讲「TLOAD 占比高源于感受野重叠」的结论进入 u6-l3 性能优化方法论，练习用利用率表判定 Bound 并设计 tiling。
