# 卷积通路：Img2col、SetFmatrix 与 Conv2d Forward

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 im2col（image to column）为什么能把卷积变成矩阵乘，以及 PTO 的 `TIMG2COL` 指令与软件 im2col 的本质区别（硬件在 L1→L0A 搬运途中顺手完成展开）。
2. 掌握卷积通路的三条配置指令 `SETFMATRIX`、`SET_IMG2COL_RPT`、`SET_IMG2COL_PADDING` 的作用，以及 `SetFmatrixMode` 四种模式下「谁负责下发配置」的差异。
3. 读懂 `ConvTile` 这个"带卷积元数据的 Tile"，理解 `fmapH/fmapW/padList/filter/stride/dilation` 等字段的含义。
4. 通读 `kernels/manual/a2a3/conv2d_forward` 完整算子，把 u5-l1 的 `TMATMUL` 与 u5-l3 的多级双缓冲、L1 caching 拼成一条完整的卷积前向链路。
5. 能手算一层卷积的输出尺寸公式，并把它与 kernel 中的编译期常量对应起来。

## 2. 前置知识

- **卷积与 im2col**：2D 卷积是「卷积核在输入特征图上滑动做加权求和」。经典优化手段 im2col 把每个感受野的元素拷贝成矩阵的一列，卷积就退化成一次 GEMM：`Y = K_mat × X_col`。代价是软件要做大量重复拷贝。
- **NC1HWC0 / FRACTAL_Z 布局**：昇腾硬件偏好的 5 维特征图布局，C0 固定为 16（`c0=16`），C1 = ceil(C/16)，即通道按 16 个一组摆放；权重则预排成 `FRACTAL_Z` 分形格式，天然就是 Cube 单元想要的矩阵形态。这两个布局在 u2-l2「Tile 编程模型」中已介绍。
- **Cube 数据通路**（承接 u5-l1）：GM → L1（`TileType::Mat`）→ L0A/L0B（`TileLeft`/`TileRight`）→ 累加器（`TileAcc`）→ 写回。`TMATMUL` 定义在 L0 层。
- **事件同步与双缓冲**（承接 u2-l3、u5-l3）：`set_flag/wait_flag` 用 `(srcPipe, dstPipe, eventId)` 三元组配对；MTE2（搬入）/MTE1（片上搬移）/M（Cube）/FIX（写回）四条流水线靠事件编排重叠。
- **输出尺寸公式**（本讲反复使用）：

  \[ h_{out} = \left\lfloor \frac{h_{in} + pad_{top} + pad_{bottom} - dilation_H \cdot (h_k - 1) - 1}{stride_H} \right\rfloor + 1 \]

  \[ w_{out} = \left\lfloor \frac{w_{in} + pad_{left} + pad_{right} - dilation_W \cdot (w_k - 1) - 1}{stride_W} \right\rfloor + 1 \]

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [include/pto/npu/a2a3/TImg2col.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TImg2col.hpp) | A2/A3 上 `TIMG2COL` 的 NPU 实现，内部含 `SetFmatrix/SetRepeat/SetPadding` 三个 AUTO 模式辅助函数 |
| [include/pto/npu/a2a3/SetFmatrix.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/SetFmatrix.hpp) | `SETFMATRIX` 指令实现：把 fmap 尺寸与 pad 打包进 FMATRIX 寄存器 |
| [include/pto/npu/a2a3/SetImg2colRpt.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/SetImg2colRpt.hpp) | `SET_IMG2COL_RPT` 指令实现：写 repeat 配置寄存器 |
| [include/pto/npu/a2a3/SetImg2colPadding.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/SetImg2colPadding.hpp) | `SET_IMG2COL_PADDING` 指令实现：写填充值寄存器 |
| [include/pto/common/pto_tile.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp) | `ConvTile` 与 `ConvTileShape` 定义（卷积元数据载体） |
| [include/pto/common/type.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp) | `SetFmatrixMode` 枚举 |
| [include/pto/cpu/TImg2col.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TImg2col.hpp) | `TIMG2COL` 的 CPU 仿真实现（逐元素重排，语义参考） |
| [kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp) | 完整 Conv2d 前向算子 kernel（本讲主角） |
| [kernels/manual/a2a3/conv2d_forward/main.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/main.cpp) | host 侧入口：申请内存、下发 kernel、比对 golden |
| [kernels/manual/a2a3/conv2d_forward/scripts/gen_data.py](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/scripts/gen_data.py) | 造数脚本，内含软件 im2col 参考实现 |
| [kernels/manual/a2a3/conv2d_forward/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/README.md) | 算子说明、tiling 参数表与实测性能 |
| [tests/cpu/st/testcase/timg2col/main.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/timg2col/main.cpp) | `TIMG2COL` 的 CPU ST 用例（本讲实践载体） |

## 4. 核心概念与源码讲解

### 4.1 TIMG2COL：硬件版 im2col 展开指令

#### 4.1.1 概念说明

软件 im2col 需要先把特征图展开成一个大矩阵再喂给 GEMM，展开过程本身要额外读写一遍内存。昇腾的 Cube 单元把这一步做进了搬运通路：`TIMG2COL` 直接从 L1 的特征图 tile（`NC1HWC0` 布局的 `ConvTile`，位置 `Mat`）读取数据，一边搬运一边按卷积语义展开，写入 L0A 的 `TileLeft`。也就是说：

- **输入侧**：L1 上的一块特征图（不展开的原始 NC1HWC0 摆放）；
- **输出侧**：L0A 上已经展开好的 im2col 矩阵（行 = 输出像素 M，列 = \(C_{in} \times h_k \times w_k\) 的 K 维）；
- **代价**：零额外内存 pass——展开在 MTE1 流水线的搬运途中完成。

与之对照，权重因为已经离线排成 `FRACTAL_Z`（本身就是一个矩阵），不需要 im2col，直接用普通的 `TEXTRACT`（u4-l3 讲过的窗口搬移指令）从 L1 切片到 L0B 即可。这是 conv2d_forward kernel 里「A 路走 TIMG2COL、B 路走 TEXTRACT」不对称设计的根源。

#### 4.1.2 核心流程

`TIMG2COL(dst, src, posM, posK)` 的语义（以 CPU 仿真实现为规范参考）：

```text
对 dst 有效区内每个 (r, c)：
    mIndex = posM + r          # 展开矩阵的行：全局输出像素编号
    kIndex = posK + c          # 展开矩阵的列：C0 × hk × wk 中的某个通道×核位置

    由 mIndex 逆推出 (n, h_out, w_out)   # 第几个 batch、输出图上哪个像素
    由 kIndex 逆推出 (c1, c0, kernelH, kernelW)  # 哪个通道、卷积核哪个抽头

    inputH = h_out * strideH + kernelH * dilationH - padTop
    inputW = w_out * strideW + kernelW * dilationW - padLeft
    若 (inputH, inputW) 落在特征图内：
        dst[r][c] = src[n][c1][inputH][inputW][c0]
    否则：
        dst[r][c] = padValue     # padding 填充值
```

两个偏移参数解决「大矩阵分块」问题：一次 `TIMG2COL` 只生成 `[baseM, baseK]` 的一块，`posM/posK` 指明这块在完整 im2col 矩阵中的左上角坐标，循环中逐块生成。

#### 4.1.3 源码精读

先看 NPU（A2/A3）实现。`TIMG2COL_IMPL` 是指令入口，前半段是编译期契约检查：

[TImg2col.hpp:L91-L108](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TImg2col.hpp#L91-L108) —— 用 `static_assert` 强制五条硬约束：源必须是 `Mat` 位置的 `ConvTile`（L1）、目的必须是 `Left`（L0A）、源布局必须是 `NC1HWC0`（或 5 维版 `NDC1HWC0`）、源/目的 dtype 一致且属于 `int8_t/half/bfloat16_t/float` 白名单。

[TImg2col.hpp:L109-L121](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TImg2col.hpp#L109-L121) —— 关键分支：若 `FmatrixMode` 是 `*_AUTO`，则在本条指令内部先自动下发三组配置（`SetFmatrix/SetRepeat/SetPadding`），再计算 `stepM = dst 有效行数`、`stepK = 有效列按 c0 对齐`，最后落到真正的硬件原语。注意 `c0Size = 256B / sizeof(DType)`，即按 256 字节块对齐。

[TImg2col.hpp:L67-L89](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TImg2col.hpp#L67-L67) —— 最内层函数 `TImg2col` 把 tile 降级为裸指针（`__cbuf__` L1 指针 → `__ca__ L0A 指针），调用 CCE intrinsic `img2colv2_cbuf_to_ca`，把 stepM/stepK/posM/posK/stride/dilation/filter/transpose/channelSize 一次性传给硬件。`filterW/H > 255` 时拆成低 8 位 + 高位标志两个参数，规避 8 位寄存器位宽限制。

再看 CPU 仿真实现（它就是"指令语义说明书"）：

[cpu/TImg2col.hpp:L78-L108](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TImg2col.hpp#L78-L108) —— `ExtractImg2ColParams` 从 `ConvTile` 的元数据（shape 各维 + stride/dilation/filter/padList/channelSize）提取出全部卷积参数，并在 L100-L105 用与第 2 节完全相同的公式算出 `outH/outW`。注意 L95-L98 揭示了 **padList 四个槽位的顺序：`[0]=left, [1]=right, [2]=top, [3]=bottom`**。

[cpu/TImg2col.hpp:L110-L145](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TImg2col.hpp#L110-L145) —— CPU 版 `TIMG2COL_IMPL`：纯双层循环。外层把行号 `r` 逆映射回 `(n, d, outRow, outCol)`，内层把列号 `c` 逆映射回 `(c1, c0, kernelH, kernelW)`，然后调 `CalculateValue` 取数或填 padding，写入 dst 的分形偏移。`FmatrixMode` 在 CPU 后端被 `(void)` 忽略——寄存器写放在 CPU 上是空概念。

[cpu/TImg2col.hpp:L147-L166](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TImg2col.hpp#L147-L166) —— `CalculateValue`：计算 `inputH/inputW`，越界返回 `padValue`，命中则按 NC1HWC0 下标公式从 L1 数据取值。这 20 行就是 im2col 的全部数学。

> 提示：NPU 版与 CPU 版对 **dst 布局的 static_assert 措辞不同**（NPU 要求 RowMajor，CPU 要求 ColMajor，见 [TImg2col.hpp:L100-L101](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TImg2col.hpp#L100-L101) 与 [cpu/TImg2col.hpp:L47-L48](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TImg2col.hpp#L47-L48)），`TileLeft` 的默认布局在不同后端下解释有差异。写跨后端 kernel 时以所编译后端头文件的断言为准。

#### 4.1.4 代码实践：跑通并改造 timg2col ST 用例

1. **实践目标**：在 CPU 仿真下观察 `TIMG2COL` 的输出，验证你对 posM/posK 与 padding 语义的理解。
2. **操作步骤**：
   - 打开 [tests/cpu/st/testcase/timg2col/main.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/timg2col/main.cpp)，重点读第一个用例（L80-L125）：特征图 1×3×4、C0=8，2×2 卷积核，`padList={1,0,1,0}`（左 1、上 1），`padValue=-1.0f`，调用 `TIMG2COL(dst, src, 1, 8)` 从展开矩阵的 (posM=1, posK=8) 处取一块 16×16。
   - 用 `python3 tests/run_cpu.py` 构建 CPU ST 用例并用它运行 timg2col（可在 `tests/README.md` 确认过滤参数写法；若脚本参数不支持按名过滤，运行全部用例观察 `TImg2colCpuSimTest` 两组用例是否 PASSED）。
   - 手工推一个元素：展开矩阵第 1 行对应输出像素 (outRow=0, outCol=1)，第 8 列对应 kIndex=8+0 通道维的哪个抽头？对照 L131-L137 的逆映射公式算出 `dst[0][0]` 应取 `src` 的哪个下标，再与 `BuildExpected` 的参考值核对。
3. **需要观察的现象**：gtest 输出两组用例（`ManualMetadataPath...` 与 `AutoMetadataPath...`）均 PASSED；两者分别覆盖 MANUAL 与 AUTO 两种配置下发路径。
4. **预期结果**：CPU 仿真输出与 numpy 风格参考实现逐元素一致，误差为 0（`EXPECT_FLOAT_EQ`/`EXPECT_EQ`）。
5. 以上运行结果**待本地验证**（本讲义写作时未实际执行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 conv2d_forward 中权重走 `TEXTRACT` 而特征图走 `TIMG2COL`？

**答案**：权重已离线重排为 `FRACTAL_Z` 格式，本身就是 Cube 想要的矩阵形态，只需从 L1 按行窗口切片到 L0B；特征图是 NC1HWC0 摆放、卷积需要在滑动窗口中重复取数，必须做 im2col 展开——`TIMG2COL` 让展开随搬运免费完成。

**练习 2**：`posM=1, posK=8` 各自是什么含义？为什么 kernel 主循环里每轮 kIter 要传不同的 posK？

**答案**：`posM/posK` 是本次生成的块在完整 im2col 矩阵（M = N×H_out×W_out，K = C_in×hk×wk）中的行列起始偏移。K 维太长装不进 L0A，kernel 按每轮 `baseK` 切一段，第 i 轮传 `posK = i * baseK`（见后文 `kModStepKa * baseK`），M 维同理按输出像素块推进。

**练习 3**：若把 `padValue` 从 0 改成 -1，输出特征图哪些位置会变化？

**答案**：只有覆盖 padding 区的输出像素（即至少一个抽头落到特征图之外的滑窗位置，如图像第一行/列的输出）会变化，其求和项中越界抽头从 0 变为 -1×对应权重；完全落在图内的滑窗不受影响。

### 4.2 ConvTile 与 SetFmatrix/SetImg2col 配置指令族

#### 4.2.1 概念说明

`TIMG2COL` 的卷积参数（fmap 尺寸、pad、stride、dilation、filter、channelSize）不是作为指令参数逐个传入，而是挂在源 tile 上——这个 tile 类型就是 `ConvTile`：一个"普通数据 + 一包卷积元数据"的结构体。而硬件原语 `img2colv2_cbuf_to_ca` 真正消费的是三个**硬件配置寄存器**：

| 寄存器 | 写入指令 | 内容 |
| :--- | :--- | :--- |
| FMATRIX | `SETFMATRIX` | fmapW(16bit) \| fmapH(16bit) \| padList[4](4×8bit) 打包成 64bit |
| L3D_RPT | `SET_IMG2COL_RPT` | repeatStride(16bit) \| repeatTime(8bit) \| repeatMode(8bit) |
| PADDING | `SET_IMG2COL_PADDING` | 按数据位宽(1/2/4 字节)打包的填充值 |

`SetFmatrixMode`（[type.hpp:L339-L344](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp#L339-L344)）的四个枚举值决定**谁在什么时候写这些寄存器**：

- `FMATRIX_A_MANUAL / A_AUTO`：作用于 A 矩阵（特征图通路），MANUAL 需用户显式调 `SETFMATRIX` 等指令，AUTO 则在每条 `TIMG2COL` 内部自动下发；
- `FMATRIX_B_MANUAL / B_AUTO`：同上，但走 `set_fmatrix_b`（B 侧寄存器），供双矩阵卷积场景（如卷积反传对权重做 im2col）使用。

选择依据：配置不变时 MANUAL 只写一次、循环内省去重复写寄存器；fmap 参数随行块变化（如 padList 逐块不同）时用 AUTO 让每条指令自带配置更安全。

#### 4.2.2 核心流程

```text
MANUAL 模式（conv2d_forward 采用）：
    配置 ConvTile 元数据（SetFmapH/SetPadList/...）
    → SETFMATRIX(convTile) + SET_IMG2COL_PADDING + SET_IMG2COL_RPT   # 只写一次
    → 循环 { TIMG2COL(dst, src, posM, posK) }                          # 直接用寄存器现值

AUTO 模式：
    循环 { TIMG2COL(dst, src, posM, posK) }   # 每条指令内部先写三组寄存器再执行
```

#### 4.2.3 源码精读

**ConvTile 的元数据字段**：[pto_tile.hpp:L1314-L1354](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1314-L1354) 是一整组 getter/setter：`FmapH/FmapW`（特征图高宽）、`PadList[4]`、`FilterH/FilterW`、`DilationH/W`、`StrideH/W`、`PadValue`、`ChannelSize`、`RepeatStride/Time/Mode`、`Transpose`。对应的私有成员默认值见 [pto_tile.hpp:L1362-L1386](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1362-L1386)（stride/dilation 默认 1）。`ConvTileShape` 则与 `Shape` 同构：静态维进类型、`DYNAMIC(-1)` 维运行期填（[pto_tile.hpp:L1128-L1224](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1128-L1224)），最多支持 6 维（多出的 D 维用于 3D 卷积的 `NDC1HWC0`）。

**SETFMATRIX**：[SetFmatrix.hpp:L15-L38](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/SetFmatrix.hpp#L15-L38) —— 仅在 MANUAL 模式生效；把 `fmapW` 放 bit 0-15、`fmapH` 放 bit 16-31、`padList[0..3]` 各 8bit 放 bit 32-63，打包成一个 `uint64_t` 写入 `set_fmatrix`（或 B 侧 `set_fmatrix_b`）。这个 64bit 排布与 intrinsic 的硬件寄存器格式一一对应。

**SET_IMG2COL_RPT**：[SetImg2colRpt.hpp:L15-L24](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/SetImg2colRpt.hpp#L15-L24) —— 同样仅 MANUAL 模式生效；`repeatStride | repeatTime<<16 | repeatMode<<24` 打包写 `set_l3d_rpt`，控制硬件按 repeat 粒度自动重复搬运（同一行块内多个 16×16 子块的步进方式）。

**SET_IMG2COL_PADDING**：[SetImg2colPadding.hpp:L15-L33](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/SetImg2colPadding.hpp#L15-L33) —— 按数据位宽分支：1 字节类型把同一个字节复制两份（适配硬件一次至少搬 16bit），2/4 字节直接 reinterpret 成整数写 `set_padding`。

**AUTO 模式的对应实现**就在 TImg2col.hpp 内部：`SetFmatrix` 辅助函数 [TImg2col.hpp:L16-L38](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TImg2col.hpp#L16-L38) 与 `SetRepeat`（L40-L49）、`SetPadding`（L51-L65）做完全相同的打包，区别只在 `*_AUTO` 模式才编译进来，并在 `TIMG2COL_IMPL` 的 L109-L113 被自动调用。ST 用例 [timg2col/main.cpp:L116-L119](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/timg2col/main.cpp#L116-L119) 与 [timg2col/main.cpp:L161-L164](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/timg2col/main.cpp#L161-L164) 分别演示了 `FMATRIX_B_MANUAL`（显式三条 SET 指令 + 裸 TIMG2COL）与 `FMATRIX_B_AUTO`（SET 调用可省）两种等价写法。

#### 4.2.4 代码实践

1. **实践目标**：搞清 padList 四元组的真实顺序，避免「top/bottom 填反」这类静默错误。
2. **操作步骤**：
   - 在 timg2col ST 用例中，把 L99 的 `padList[] = {1, 0, 1, 0}` 改成 `{0, 0, 1, 0}`（只保留 top=1），重新运行该用例；
   - 对照 [cpu/TImg2col.hpp:L95-L98](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TImg2col.hpp#L95-L98) 的 `padLeft=GetPadList(0)...padBottom=GetPadList(3)` 确认改动语义。
3. **需要观察的现象**：用例仍然 PASSED——因为 golden 是由同一套 `BuildExpected`（读同一个 padList）生成的，改参数正确性不变，但**输出的数值矩阵变了**（第一列的填充分布改变）。如果想看到数值，可在 `TIMG2COL` 调用后临时打印 `dst.data()[i]`。
4. **预期结果**：padList 槽位 0/1 影响左右边界、2/3 影响上下边界；`padList={1,0,1,0}` 与 `{0,0,1,1}` 产生不同的 im2col 矩阵，尽管 pad 总量相同。
5. 数值对比部分**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`SETFMATRIX` 与 `TIMG2COL<FMATRIX_A_AUTO>` 都能写 FMATRIX 寄存器，conv2d_forward 为什么选 MANUAL？

**答案**：该算子中 fmap 的 `fmapH(hinCount)/fmapW` 与 padList 只随外层 mIter 变化，K 循环内几百条 `TIMG2COL` 共享同一配置。MANUAL 在每个 mIter 写一次寄存器（[conv2d_forward_kernel.cpp:L230](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp#L230)），循环内指令免带配置开销；AUTO 模式每条指令重复写同样的寄存器，浪费指令发射带宽。

**练习 2**：`ChannelSize` 字段是干什么的？不设会怎样？

**答案**：它告诉硬件本次 im2col 展开覆盖的输入通道数（K 维一段对应的通道区间）。CPU 参考实现里 `channelSize<=0` 时退回 `fmapC1*fmapC0`（[cpu/TImg2col.hpp:L99](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TImg2col.hpp#L99)）；NPU 上它参与 intrinsic 寻址，设错会导致取数错位。

**练习 3**：64bit FMATRIX 寄存器为什么要留 4 个 8bit 给 padList？

**答案**：卷积四边 padding 可以各不相同（非对称 pad，如 pooling 后的特征图），padList[0..3] 按左/右/上/下各占一个字节，硬件在滑窗寻址时按边分别判断越界。

### 4.3 conv2d_forward：完整卷积算子精读

#### 4.3.1 概念说明

`kernels/manual/a2a3/conv2d_forward` 把前面所有积木拼成一个生产级算子：输入 `X=[batch,cin,hin,win,c0]`（NC1HWC0）、权重 `K`（FRACTAL_Z）、输出 `Y=[batch,n/c0,hout,wout,c0]`。默认配置为 `X=[4,32,16,96,16]`、`K=[288,384,16,16]`、`Y=[4,384,16,96,16]`，stride/dilation=1、pad 四边=1，在 24 核 A3 上验证。

它综合运用四项优化（README「Optimization Details」）：多核切分（4×6 网格，`singleCoreM=1536/singleCoreK=4608/singleCoreN=1024`，K 不切避免核间规约——与 u5-l3 gemm_performance 同一策略）、base block `[128,256,48]`（对 fp16 有更高计算访存比且利于 512B 对齐）、L1 caching（`stepKa=stepKb=3`，一次搬 3 个 K 块）、L1/L0A/L0B 三级双缓冲。

注意 K 维的身份：`k = cin*c0*hk*wk = 512*9 = 4608`，即 im2col 矩阵的列数；`baseK=48` 恰好等于 16 通道 × 9 个抽头，故每个 L1 panel 加载 `channelSize = ceil(stepKa*baseK/(hk*wk)) = ceil(144/9) = 16` 个通道的行条带。

#### 4.3.2 核心流程

单核内（三级循环 mIter → nIter → kIter）的数据流：

```text
GM 特征图(NC1HWC0) ──TLOAD(MTE2)──> L1 fmapMat[2]      (ConvTile, Mat, 双缓冲)
GM 权重(FRACTAL_Z) ──TLOAD(MTE2)──> L1 weightMat[2]    (ConvTile, Mat, 双缓冲)

L1 fmapMat   ──TIMG2COL(MTE1)──> L0A aTile[2]   # 边搬边 im2col 展开
L1 weightMat ──TEXTRACT(MTE1)──> L0B bTile[2]   # 普通行切片
L0A + L0B    ──TMATMUL/TMATMUL_ACC(M)──> Acc outTile   # 首轮清零、后续累加
Acc outTile  ──TSTORE(FIX)──> GM 输出(NC1HWC0)
```

事件配对（沿用 u5-l3 的模式）：`MTE1→M` 保护「TEXTRACT 完成后才能 TMATMUL」；`M→MTE1` 保护「TMATMUL 用完 L0 半区后才能翻写另一个半区」；`MTE2→MTE1` 保护 L1 槽复用；循环首尾用 `InitSyncFlags/WaitSyncFlags` 补齐反向同步。

#### 4.3.3 源码精读

**核间切分**：[conv2d_forward_kernel.cpp:L59-L74](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp#L59-L74) —— `InitGMOffsets` 用 `get_block_idx()` 算出本核在 4×6 网格中的 (mCoreIdx, nCoreIdx)，把 GM 指针推到本核负责的 A panel、B panel 与 C tile 起点。各核输出互不相交，全程无需 SyncAll。

**ConvTile 配置（本讲核心知识的落点）**：[conv2d_forward_kernel.cpp:L215-L232](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp#L215-L232) —— 每个 mIter 先按输出行窗口推算需要的输入行区间 `[hinStart, hinEnd]`（L208-L213），再配置两个 fmapMat：`SetFmapH(hinCount)/SetFmapW(win)/SetFilterH(3)/SetFilterW(3)`；**padList 的四边是动态计算的**——`SetPadList(0/1, 1)` 是固定的左右 pad，而 `SetPadList(2, Max(0, padTop - houtStart*strideH))`、`SetPadList(3, ...)` 是「本行窗口相对整图的等效上下 pad」：因为 L1 里只装了特征图的一个行条带，窗口第一行距离条带顶部的越界量要重新折算。随后 L230 `SETFMATRIX(fmapMat[0])` 一次性下发配置（MANUAL 模式），L231-L232 用 `TASSIGN` 把两个 L1 panel 摆到不重叠的偏移。

**K 迭代主体**：[conv2d_forward_kernel.cpp:L150-L185](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp#L150-L185) —— `ProcessKIteration`：每 `stepKa` 轮做一次 TLOAD（L167-L179）：构造 5 维 `GlobalTensor` 视图描述 NC1HWC0 特征图（动态维填 `hinCount`）与 FRACTAL_Z 权重，先 `WaitFlag<PIPE_MTE1, PIPE_MTE2>` 等 L1 槽空闲，再分别 TLOAD 并各挂牌一个 `MTE2→MTE1` 事件（fmap 用 0 号、weight 用 1 号），最后翻转 mte2DBFlag。

**TIMG2COL 的调用点**：[conv2d_forward_kernel.cpp:L114-L145](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp#L114-L145) —— `MacroMatmul` 是一轮 K 的四段流水：① 等 TMATMUL 释放 L0 半区；② **`TIMG2COL(aTile[mte1DBFlag], fmapMat[currMte2Idx], woutStart, kModStepKa * baseK)`**（L129）——posM 传 `woutStart`（本 M 块起始输出像素在行内的列偏移）、posK 传 `kIter % stepKa * baseK`（本块在 L1 panel 内的 K 偏移）；同一条 MTE1 流水上，权重侧用 `TEXTRACT` 切片（L132）；③ 每 stepKa 轮末放行 L1 槽（L134-L137）；④ `TMATMUL`/`TMATMUL_ACC`（L140-L144，首轮清零、后续累加，与 u5-l2 gemm 基线同构，见 L37-L45 的 `MatmulAcc`）。

**写回**：[conv2d_forward_kernel.cpp:L76-L95](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp#L76-L95) —— `StoreResult` 用一个 5 维 NC1HWC0 的 `GlobalTensor` 视图描述输出，`TSTORE` 把 Acc tile 写回 GM，前后各一对 `M↔FIX` 事件。

**编译期尺寸推导**：[conv2d_forward_kernel.cpp:L339-L340](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp#L339-L340) —— `hout/wout` 直接用第 2 节的公式在编译期算出（默认 pad=1、3×3 核、stride=1 时 hout=16、wout=96），再据此推 `m = batch*hout*wout = 6144`。host 侧 [main.cpp:L28-L31](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/main.cpp#L28-L31) 用同一公式计算输出文件大小；golden 侧 [gen_data.py:L63-L64](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/scripts/gen_data.py#L63-L64) 的软件 im2col 也是同一公式——三处必须一致，这正是本讲综合实践的验证点。

#### 4.3.4 代码实践：修改 padding 并手算输出尺寸

1. **实践目标**：验证「改一个 pad，三处尺寸推导必须联动」这一工程事实。
2. **操作步骤**：
   - **手算**：默认 `hin=16, win=96, hk=wk=3, stride=1, dilation=1, pad 全 1` 时 `hout=16, wout=96`。现在把 `padTop` 从 1 改为 2，用公式算出 `hout = (16+2+1-2-1)/1+1 = 17`，`wout` 不变 = 96；新 `m = batch*hout*wout = 4*17*96 = 6528`（原 6144）。
   - **改代码**（三处联动）：① [gen_data.py:L162](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/scripts/gen_data.py#L162) 的 `padding=(1,1,1,1)` 改为 `(2,1,1,1)`；② kernel 侧模板默认值 `padTop = 1`（[conv2d_forward_kernel.cpp:L248](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/conv2d_forward_kernel.cpp#L248)）改为 2——`hout` 会在 L339 自动重算；③ `launchConv2dForward` 里的 `m=6144`（L309）与 `singleCoreM=1536`（L312）需按新 `m` 重推（`singleCoreM = m/4`，若不能整除还需调整核网格）。
   - **运行**（需 NPU 环境，CPU 仿真不覆盖本算子工程）：`source ${ASCEND_INSTALL_PATH}/bin/setenv.bash` → `python3 scripts/gen_data.py` → `bash run.sh -r npu -v Ascend910B1`。
3. **需要观察的现象**：golden 输出文件 `output/golden.bin` 大小变为 `4*384*17*96*16*2` 字节（NC1HWC0、fp16）；程序末尾打印 `test success`。
4. **预期结果**：kernel 输出与手算尺寸一致、数值与 golden 比对通过（容差 0.001，见 [main.cpp:L80](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/main.cpp#L80)）。
5. 无 NPU 环境时可做**源码阅读型验证**：只完成手算 + 对照三处公式，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：kernel 中 `SetPadList(2, Max(0, padTop - houtStart*strideH))` 为什么不能直接写 `padTop`？

**答案**：L1 里的 fmapMat 只装了特征图的一个行条带 `[hinStart, hinEnd]`，`TIMG2COL` 的越界判断以条带为坐标系。当本 M 块的输出行窗口从条带顶部就引入全局 padding 时（houtStart*strideH < padTop），条带内等效上 pad 是 `padTop - houtStart*strideH` 而非全局 padTop；反之为 0。下边同理。填错会导致条带边缘元素被误当成 padding。

**练习 2**：`baseK=48` 这个数字是怎么来的？换成 64 会发生什么？

**答案**：48 = c0(16) × hk(3) × wk(3) / wk... 更准确地说 48 = c0 × hk × wk / 3？不——48 = 16 通道 × 3×3 抽头 / 3？正解：`stepKa*baseK = 144 = channelSize(16) × hk*wk(9)`，即 baseK=48 恰是 16/3... 实际约束是 `channelSize = ceil(stepKa*baseK/(hk*wk))` 必须取整使 L1 行条带对齐通道组。**参考答案**：baseK 必须与 `c0×hk×wk`（=144 的因子结构）对齐，否则一个 K 块会横跨不完整的通道组，im2col 列映射错位；改成 64 会破坏该对齐，需同步重选 stepKa 使 `stepKa*baseK` 仍为 9 的倍数且通道组完整（待读者结合 L257-L258 的 `channelSize` 推导确认）。

**练习 3**：性能表（README「Measured Performance」）中 TEXTRACT 占比常在 60% 左右且随规模基本不降，瓶颈在哪个流水段？这对 A/B 两条通路分别意味着什么？

**答案**：MTE1（片上搬移）压力主要来自 B 侧 TEXTRACT 与 A 侧 TIMG2COL 共享同一流水线。改善方向是减少 MTE1 指令量：B 侧加大 baseN/stepKb 让每次 TEXTRACT 搬更多；A 侧 TIMG2COL 同理加大 baseK。TMATMUL 占比 90%+ 时接近 Cube 饱和，进一步优化应转向消除 MTE1 气泡。

## 5. 综合实践

**任务：给 conv2d_forward 画一张「参数 → 指令」对照表并做一次 stride 修改。**

1. 画出本算子的四级流水图（GM→L1→L0→Acc→GM），在每条边上标注指令（TLOAD/TIMG2COL/TEXTRACT/TMATMUL/TSTORE）、所属流水线（MTE2/MTE1/M/FIX）与保护它的事件对，重点标出 A 通路与 B 通路在 MTE1 上的分叉。
2. 整理一张「卷积参数 → PTO 落点」表：`pad* → ConvTile::SetPadList + SETFMATRIX 寄存器 bit32-63`、`stride* → ConvTile::SetStride*（进 intrinsic 参数）`、`hk/wk → SetFilter*（同时决定 k=C*9 的长度）`、`hout/wout → 编译期常量（kernel L339-L340、main.cpp L29-L31、gen_data.py L63-L64 三处联动）`。
3. 选做（需 NPU）：把 `strideH/W` 从 1 改成 2，重复 4.3.4 的三处联动流程（此时 `hout=(16+2-2*2-1)/2+1` 需重新手算），跑通并记录 `test success`。

预期产出：一张流水线图 + 一张对照表 + 一组手算尺寸（NPU 运行结果待本地验证）。

## 6. 本讲小结

- `TIMG2COL` 是硬件版 im2col：在 MTE1 流水线把 NC1HWC0 特征图从 L1 搬到 L0A 的途中完成展开，`posM/posK` 定位块在完整 im2col 矩阵中的偏移，padding 由硬件按 `padValue` 自动填充——软件 im2col 的整图重排开销被消掉。
- 卷积参数不进指令参数表，而是挂在 `ConvTile` 的元数据上，由 `SETFMATRIX`（fmap 尺寸+padList 打包 64bit）、`SET_IMG2COL_RPT`（repeat 配置）、`SET_IMG2COL_PADDING`（填充值）三条配置指令写入硬件寄存器；`SetFmatrixMode` 的 AUTO/MANUAL 之分在于配置由指令内部自动下发还是用户显式下发，配置稳定时 MANUAL 更省。
- padList 顺序是 `[left, right, top, bottom]`；对只装特征图行条带的 L1 panel，上下 pad 必须按窗口位置重算为「条带内等效 pad」，这是 conv2d_forward 中最容易踩的坑。
- 完整卷积链路 = TLOAD（GM→L1，stepKa/stepKb 批量 caching）→ TIMG2COL/TEXTRACT（L1→L0A/L0B，A 展开 B 切片）→ TMATMUL(_ACC)（首轮清零后续累加）→ TSTORE（NC1HWC0 视图写回），配三级双缓冲与 MTE2/MTE1/M/FIX 四流水线事件编排。
- 输出尺寸公式在 kernel 编译期、host 侧、golden 造数三处重复出现，修改 padding/stride 必须三处联动，否则连尺寸都对不上。

## 7. 下一步学习建议

- 下一讲 u5-l5 将进入 MX 混合精度矩阵乘（`TMATMUL_MX`、缩放因子布局与 A5 上的 mxfp4/mxfp8 实现），可对照本讲的 `TMATMUL` 数据通路理解「带 scale 的 Cube」。
- 想加深事件编排理解，可回读 u6-l2「流水线并行」并对照本讲 `MacroMatmul` 的四对 set/wait。
- 建议通读 [docs/isa/TIMG2COL.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TIMG2COL.md) 与 [docs/isa/SETFMATRIX.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/SETFMATRIX.md) 的 ISA 定义，以及 [kernels/manual/a2a3/conv2d_forward/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/conv2d_forward/README.md) 的性能表判读方法（承接 u5-l3 的利用率分析）。
