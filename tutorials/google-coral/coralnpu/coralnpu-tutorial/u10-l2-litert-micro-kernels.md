# litert-micro 算子内核

## 1. 本讲目标

本讲深入 CoralNPU 的**软件算子库** `sw/opt/litert-micro/`，理解它如何把 TensorFlow Lite for MicroControllers（TFLM）的标准算子（卷积、深度卷积、全连接、池化）**重新实现为针对 CoralNPU 向量后端的 RVV（RISC-V 向量扩展）内核**。

学完后你应当能够：

- 说清 `sw/opt/litert-micro/` 这一软件层在整条「标量核驱动 → 向量/MAC 后端执行」链路中的位置，并区分它与 u7 章节讲的 SystemVerilog MAC 引擎（硬件）的边界。
- 看懂 TFLM 的 `init / prepare / invoke` 注册机制，理解 `Register_CONV_2D()` 等函数如何用一个优化内核**替换**参考内核，以及为何类型不匹配时仍会回退到参考实现。
- 读懂累加器后处理头 `accumulator_util.h`：`PrepareShiftParams` 如何拆分有符号移位、`PostprocessAcc` 如何完成「加偏置 → 左移 → 乘缩放因子 → 右移 → 加偏移 → 收窄 → 钳位」的整数量化流水线。
- 用 `fully_connected.cc` 这一最简算子掌握 RVV 的核心乘累加模式：`vsetvli` 设向量长度 → `vwmacc.vv` 宽化乘累加 → `vredsum` 跨道归约。
- 在 `conv.cc` 中追踪多策略分发（`ConvPerChannel` 的 if/else 链）、`CONV_MAC` 内联汇编如何把 4×4 卷积核钉在向量寄存器里、以及「权重当 wide 广播、激活当 narrow 标量」与「输入通道分块向量化」两种不同的数据组织思路。
- 理解算子如何被验证：`conv_test.cc` 在同一份输入上同时跑参考核与优化核，比对结果、记录周期数。

## 2. 前置知识

在进入本讲前，建议你已建立以下认知（均来自本手册前序讲义）：

- **RVV 编程模型**（u10-l1）：`vsetvli` 写入 `vtype`（SEW/LMUL）并算出本次实际处理元素数 `vl`；`VLMAX = VLEN × LMUL / SEW`；软件 stripmining 用 `while(n>0){ vl=vsetvli; 处理 vl 个; n-=vl }`。CoralNPU 的 `VLEN = 128`，向量扩展为 `Zve32x`，元素支持 8/16/32 位。
- **MAC 外积引擎**（u7-l4）：硬件的「wide × narrow」外积结构，`VDOT` 把 4×8bit 乘法归约进 32bit 累加器，每周期 256 MACs。**本讲的代码是 C++ 软件**，它发出的 `vwmacc.vv` 等向量指令最终由这套后端执行——所以本讲的「wide/narrow」是数据组织层面的概念，对应到硬件的外积结构。
- **整数量化基础**：TFLM 的 `int8` 量化把实数 \(r\) 映射为 \(q = \mathrm{round}(r/S) + Z\)，其中 \(S\) 是 scale、\(Z\) 是零点。两个量化张量相乘时，会出现 `input_offset`、`output_offset`、`output_multiplier`、`output_shift` 这些「量化参数」。本讲代码的很大一部分就是在高效地处理这些参数。
- **TFLM 算子接口**：每个 TFLM 算子是一个 `TFLMRegistration`，含 `init`（分配持久状态）、`prepare`（推断形状、申请 scratch buffer）、`invoke`（真正计算）三个回调。`TfLiteContext` 是 TFLM 传给算子的「服务总线」，提供 `AllocatePersistentBuffer`、`RequestScratchBufferInArena`、`GetScratchBuffer` 等能力。

> 术语提示：下文「参考核（reference kernel）」指 TFLM 自带的可移植 C++ 实现（`tflite::reference_integer_ops::*`）；「优化核（optimized kernel）」指本讲这套用 RVV 重写的 CoralNPU 专用实现。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `sw/opt/litert-micro/accumulator_util.h` | ~125 | 累加器后处理：`PrepareShiftParams` 拆分有符号移位、`PostprocessAcc` 完成整数量化并收窄到 int8/int16。被所有需要「量化收尾」的算子复用。 |
| `sw/opt/litert-micro/fully_connected.cc` | ~137 | 全连接算子：最简的 `vwmacc` + `vredsum` 向量乘累加范式，并暴露 `Register_FULLY_CONNECTED()`。 |
| `sw/opt/litert-micro/conv.cc` | ~1490 | 卷积算子（本讲最复杂）：`ConvPerChannel` 多策略分发、`CONV_MAC` 内联汇编宏、4×4 各种快速路径、权重重排、`ConvEval/ConvInit/ConvPrepare`。 |
| `sw/opt/litert-micro/depthwise_conv.cc` | ~560 | 深度可分离卷积：分块（patch）策略 + 3×3 的输入列复用（含手工内联汇编）。 |
| `sw/opt/litert-micro/pooling.cc` | ~138 | 最大池化：纯向量的逐通道 `vmax`，最简单的「按通道向量化」算子范例。 |
| `sw/opt/litert-micro/memory_util.h` | ~63 | 定义 `OpDataConvCustom`（在 TFLM `OpDataConv` 上追加 repacked 权重、weight_sums、scratch 索引等字段）与对齐分配工具。 |
| `sw/opt/litert-micro/test/conv_test.cc` | ~184 | 算子验证主程序：用同一输入跑参考核与优化核，比对结果、记录周期数。 |
| `sw/opt/rvv_opt.h` | ~59 | 向量化的 `Memcpy`/`Memset`，作为算子间共用的内存搬运工具。 |

构建关系（`sw/opt/litert-micro/BUILD`）：每个算子是一个 `cc_library`，`target_compatible_with = ["//platforms/cpu:coralnpu_v2"]`，即只对 CoralNPU 平台生效；它们依赖 TFLM 的 `op_resolvers` 与内部 `common`/`reference_base`，`conv`/`depthwise_conv` 还依赖 `//sw/opt:rvv_opt`。测试目录 `test/BUILD` 用自定义宏 `coralnpu_v2_binary`（见 u1-l3）把算子编成可在 Verilator 仿真器上运行的 `.elf`，再用 `py_test`（依赖 `coralnpu_v2_sim_utils`，见 u10-l3）驱动仿真、回收结果。

## 4. 核心概念与源码讲解

### 4.1 算子注册机制：如何替换 TFLM 参考实现

#### 4.1.1 概念说明

`sw/opt/litert-micro/` 的根本目标不是「新写一套算子」，而是**为已有 TFLM 模型提供跑得更快的同名算子**。TFLM 的运行时通过一张「算子注册表」（op resolver）把模型里的 `Conv2D`、`FullyConnected` 等 opcode 绑定到具体的 `TFLMRegistration`。本仓库的做法是：拿到 TFLM 默认的 registration，**只替换其中的 `invoke`（必要时也替换 `init`/`prepare`）**，其余字段原样保留。这样模型不用改、序列化格式不用动，只是同名算子在 CoralNPU 上换成了 RVV 实现。

这套替换有两个安全阀：

1. **类型闸门**：优化内核只处理 `int8 × int8`；遇到其它类型（如 int16/float）就在 `invoke` 里**回退到参考核**，保证正确性。
2. **回退到参考核**：当算子的形状落在本优化内核不支持的范围（如非 4×4 卷积核），`ConvPerChannel` 会打印 `Fallback kernel` 并调用 `tflite::reference_integer_ops::ConvPerChannel`。

这些 `Register_*` 函数最终被 `tests/npusim_examples/run_full_mobilenet_v1.cc` 这类端到端程序装进 op resolver，从而跑通整个 MobileNet（见 u10-l3）。

#### 4.1.2 核心流程

一个典型算子的注册流程（以全连接为例）：

```text
Register_FULLY_CONNECTED()
  ├── registration = tflite::Register_FULLY_CONNECTED()   # 拿 TFLM 默认注册
  ├── registration.invoke = FullyConnectedEval            # 只替换 invoke
  └── return registration                                 # 其余字段（init/prepare）不变

FullyConnectedEval(context, node)                         # TFLM 调用入口
  ├── 从 node 取 input/filter/bias/output 张量
  ├── if (input->type == int8 && filter->type == int8):
  │     FullyConnected(...)    ← 真正的 RVV 优化内核
  │     return kTfLiteOk
  └── else:
        return tflite::Register_FULLY_CONNECTED().invoke(...)  # 回退参考核
```

#### 4.1.3 源码精读

`fully_connected.cc` 的注册与回退逻辑非常简洁，是理解所有算子的样板：

- 拿到默认 registration 并替换 `invoke`，最后返回：[sw/opt/litert-micro/fully_connected.cc:130-134](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/fully_connected.cc#L130-L134) —— 注意它只改 `registration.invoke`，`init`/`prepare` 沿用 TFLM 默认实现。
- `FullyConnectedEval` 的类型闸门与回退：[sw/opt/litert-micro/fully_connected.cc:113-127](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/fully_connected.cc#L113-L127) —— `int8×int8` 走优化内核，否则委托参考核。

卷积算子替换得更彻底，连 `init`/`prepare` 也换了，因为它需要自定义的 scratch buffer 索引：

- 注册时同时覆盖三个回调：[sw/opt/litert-micro/depthwise_conv.cc:551-557](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/depthwise_conv.cc#L551-L557)（`Register_DEPTHWISE_CONV_2D` 同时设 `init/prepare/invoke`）。
- `ConvEval` 同样有「int8×int8 否则回退」的双层 `switch`：[sw/opt/litert-micro/conv.cc:1350-1370](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/conv.cc#L1350-L1370)。

> 自定义结构：卷积/深度卷积把 TFLM 的 `OpDataConv` 扩展成 `OpDataConvCustom`，新增 `repacked_weights`、`weight_sums`、各 `*_buffer_index` 字段——见 [sw/opt/litert-micro/memory_util.h:26-33](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/memory_util.h#L26-L33)。`ConvInit` 用它分配并清零：[sw/opt/litert-micro/conv.cc:1374-1389](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/conv.cc#L1374-L1389)。

#### 4.1.4 代码实践

1. **实践目标**：确认「替换 invoke + 类型回退」这一模式在四个算子里一致。
2. **操作步骤**：
   - 打开 `fully_connected.cc`、`pooling.cc`、`conv.cc`、`depthwise_conv.cc`，分别定位各自的 `Register_*` 与 `*Eval` 函数。
   - 在每个 `*Eval` 里找到「`if (input->type == kTfLiteInt8)`」与「`return tflite::Register_*().invoke(context, node)`」两行。
3. **观察现象**：四个算子的回退目标不同——全连接/池化回退到默认 registration 的 `invoke`，而卷积的 `ConvEval` 在不支持分支里直接调用参考核 `tflite::reference_integer_ops`/`Register_CONV_2D`。
4. **预期结果**：你能用一句话概括「优化内核只接管 int8 路径，其余交给参考核」这一安全网。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `Register_FULLY_CONNECTED` 只替换 `invoke`，而 `Register_DEPTHWISE_CONV_2D` 连 `init`/`prepare` 也要替换？
  - **答案**：全连接的优化内核不需要额外的 scratch buffer，TFLM 默认的 `init/prepare` 足够；而深度卷积需要一块「整张量的 int32 累加器缓冲」用于延后量化，必须在 `prepare` 里申请并把索引存进 `OpDataConvCustom`（见 `DepthwiseConvPrepare` 申请 `accs_buffer_index`），所以连 `init`/`prepare` 一并替换。
- **练习 2**：如果一个模型里出现了 `int16` 的卷积，会走哪条路径？
  - **答案**：`ConvEval` 的 `switch(input->type)` 在 `kTfLiteInt8` 分支之外直接 `return tflite::Register_CONV_2D().invoke(context, node)`，即回退到 TFLM 参考核，**结果正确但较慢**。

### 4.2 累加器与量化后处理（accumulator_util.h）

#### 4.2.1 概念说明

所有 int8 算子的真正计算都产生 **int32 累加器（accumulator, 简称 acc）**，但模型需要的是 **int8 输出**。把 acc 变成 int8 的过程叫「**量化后处理（requantization）**」，它由 TFLM 的每通道量化参数驱动：

\[ q_{out} = \mathrm{clamp}\left( \mathrm{round}\left(\frac{\mathrm{acc} + \mathrm{bias} - Z_{in}\cdot\sum W}{2^{\,n}}\right) \cdot M \cdot 2^{s} + Z_{out},\; a_{min},\; a_{max}\right) \]

其中 \(M\) 是 `output_multiplier`（定点缩放因子），\(s, n\) 来自 `output_shift`，\(Z\) 是零点。`accumulator_util.h` 把这条公式拆成两个函数：

- `PrepareShiftParams`：TFLM 给的 `output_shift` 是**有符号**整数（正左移、负右移），但 RVV 的移位指令需要「左移量」和「右移量」两个**无符号**操作数。这个函数把一个有符号 shift 拆成 `left[]`（正的部分）与 `right[]`（负的部分取绝对值）两张表。
- `PostprocessAcc`：对一批 int32 acc 跑完整的「加偏置 → 左移 → 乘 \(M\) → 右移 → 加 \(Z_{out}\) → 收窄到 int16 → 收窄到 int8 → 钳位」流水，用 `e32m8`（最大 LMUL）一次处理尽可能多的输出通道。

#### 4.2.2 核心流程

`PostprocessAcc` 的逐拍（每条 RVV intrinsics 一拍）数据流：

```text
acc(int32) ──+ bias ──> <<lshift ──> vsmul(×M, 舍入) ──> >>rshift(舍入)
            ──> +output_offset ──> vnclip→int16 ──> vnclip→int8
            ──> clamp[out_min,out_max] ──> store int8
```

两个关键点：

1. **左移在前、乘 \(M\) 在后**：因为 int8 量化的 acc 较小，先左移放大再做乘法，能保住精度；这与 int16 路径（`PostprocessAcc16`）的处理顺序不同。
2. **延后量化、批量收尾**：卷积/深度卷积先把**整张输出**的 int32 acc 写进一块 scratch buffer（`accs_buf`），等所有乘累加做完，再一次性调用 `PostprocessAcc`。这样向量化的收尾代价被摊薄到整张图，而非每个像素都做一遍。

`PrepareShiftParams` 用 RVV 的「窄化」指令链 `i32 → i16 → i8` 把 32 位 shift 收成 8 位，再用 `vmax(_, 0)` 同时取出正半轴（`shl`）与负半轴的绝对值（`shr`）——这是用一条 `vmax` 同时算两个结果的经典技巧。

#### 4.2.3 源码精读

- `PrepareShiftParams` 的拆分：[sw/opt/litert-micro/accumulator_util.h:31-51](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/accumulator_util.h#L31-L51)。第 38-39 行 `vncvt` 把 shift 从 32 位窄化到 8 位；第 40 行 `vneg` 取相反数；第 42-45 行用 `vmax(_, 0)` 分别取出左移量与右移量。

```c
const vint8m2_t neg = __riscv_vneg_v_i8m2(shift8, vl);
// 正值左移、负值右移（取绝对值）
const vuint8m2_t shl = ...__riscv_vmax_vx_i8m2(shift8, 0, vl)...;
const vuint8m2_t shr = ...__riscv_vmax_vx_i8m2(neg, 0, vl)...;
```

- `PostprocessAcc` 的完整量化流水：[sw/opt/litert-micro/accumulator_util.h:54-96](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/accumulator_util.h#L54-L96)。各步骤对应：
  - 加偏置（`bias_data` 可空）：第 65-67、76 行；
  - 左移：第 78 行 `vsll_vv`；
  - 乘缩放因子（`vxrm=0` 即 round-to-nearest-up）：第 59、79 行 `vsmul_vv`；
  - 右移（舍入）：第 81 行 `vssra_vv`；
  - 加输出零点：第 83 行；
  - 两次 `vnclip` 收窄 int32→int16→int8：第 85-86 行；
  - 钳位：第 88-89 行 `vmax/vmin`。

- 注意 int16 路径走的是**标量**实现以保证与 TFLM 位级精确一致（注释明说「correctness over performance」）：[sw/opt/litert-micro/accumulator_util.h:98-121](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/accumulator_util.h#L98-L121)。

#### 4.2.4 代码实践

1. **实践目标**：把 `output_shift` 的正负值喂给 `PrepareShiftParams`，验证拆分结果。
2. **操作步骤**：在脑中（或写一段主机侧 C）构造 `shift_in = {+3, -2, 0, +1}`，`out_d=4`，跟踪 `PrepareShiftParams` 的执行。
3. **观察现象**：`left` 应为 `{3,0,0,1}`、`right` 应为 `{0,2,0,0}`（右移量取了负值的绝对值）。
4. **预期结果**：理解「一条有符号 shift → 两个无符号移位量」的拆分，后续算子才能用 `vsll` + `vsra` 两拍实现原本的一拍有符号移位。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `PostprocessAcc` 要先 `vsll`（左移）再 `vsmul`（乘 \(M\)），而不是反过来？
  - **答案**：int8 量化下 acc 的有效位数偏少，先左移放大再做缩放乘法可减少中间舍入误差；顺序反了会损失精度，导致与 TFLM 参考核对不齐。
- **练习 2**：`PostprocessAcc16` 为什么退化成标量循环？
  - **答案**：注释写明用户要求「correctness over performance」——为了保证与 TFLM 位级精确一致，作者放弃了向量化带来的微小舍入差异，改用标量 `MultiplyByQuantizedMultiplier`。

### 4.3 全连接算子（fully_connected.cc）

#### 4.3.1 概念说明

全连接（`FullyConnected`，即 \(y = Wx + b\)）是本讲**最简单**的算子，也是理解 RVV 乘累加模式的最佳入口。其数学核心是一个**点积**：对每个输出通道 \(c\)，

\[ \mathrm{acc}_c = \sum_{d=0}^{D-1} (x_d - Z_{in})(W_{c,d} - Z_w) \]

注意 TFLM 的 int8 全连接里，**权重通常不带零点**（\(Z_w = 0\)），但**输入带零点** \(Z_{in}\)，所以每个 \(x_d\) 要先加 `input_offset`。优化内核的做法是：把 \(D\) 维的点积分块（stripmine），每块用一条 `vwmacc.vv`（**宽化乘累加**：两个 16 位向量相乘、累加到 32 位向量），最后用 `vredsum` 把这条向量**跨道归约**成单个标量。

#### 4.3.2 核心流程

```text
对每个 batch b、输出通道 out_c：
  acc_v = 0  (vint32m4，宽通道留余量)            # 第 56 行 vmv.x 0
  d = 0
  while d_rem > 0:                                 # 软件 stripmine（u10-l1）
    vl = vsetvl_e8m1(d_rem)                        # 本块处理 vl 个元素
    in_v8   = vle8(&input[b*D + d],      vl)       # 取输入块
    weight  = vle8(&filter[out_c*D + d], vl)       # 取权重块
    in_v16   = vsext(in_v8)  + input_offset        # int8→int16 并加零点
    weight16 = vsext(weight) + filter_offset        # int8→int16 并加零点
    acc_v = vwmacc(acc_v, in_v16, weight16, vl)    # 宽化乘累加：32bit ← 16bit×16bit
    d += vl; d_rem -= vl
  sum = vredsum(acc_v) → 标量                       # 跨道归约成单个 acc
  acc += bias[out_c]
  acc = MultiplyByQuantizedMultiplier(acc, mult, shift) + output_offset
  output = clamp(acc, min, max)                     # 钳位为 int8
```

这里 `vwmacc` 的关键：操作数是 `i16m2`（LMUL=2 的 16 位），累加器是 `i32m4`（LMUL=4 的 32 位），正好是「结果宽度 = 2 × 操作数宽度」的宽化关系。最终结果要的是**一个标量**（单输出通道），所以必须 `vredsum` 把整条向量折成 1 个数。

#### 4.3.3 源码精读

- 用 `m4` 累加器留余量、`m1` 操作数 stripmine 的主循环：[sw/opt/litert-micro/fully_connected.cc:50-77](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/fully_connected.cc#L50-L77)。第 56 行特意用 `vsetvlmax_e32m4` 把整条 `m4` 寄存器清零，避免残留。
- 核心 `vwmacc.vv`：[sw/opt/litert-micro/fully_connected.cc:73](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/fully_connected.cc#L73)。
- 跨道归约成标量：[sw/opt/litert-micro/fully_connected.cc:80-83](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/fully_connected.cc#L80-L83)（`vredsum` 后用 `vmv_x_s` 取出标量）。
- 加偏置 + 量化缩放 + 钳位：[sw/opt/litert-micro/fully_connected.cc:85-94](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/fully_connected.cc#L85-L94)。注意这里**没有用 `PostprocessAcc`**——因为全连接一次只产出一个标量 acc，直接标量调用 `tflite::MultiplyByQuantizedMultiplier` 即可，不值得向量化。

> 对比：全连接是「逐输出通道向量化、最后归约成标量」；而卷积/深度卷积是「把整张 int32 acc 缓冲攒满，再批量 `PostprocessAcc`」。两种思路的选择取决于「产出多少个 acc」。

#### 4.3.4 代码实践

1. **实践目标**：读出 `vwmacc` 操作数与累加器的 LMUL 关系，验证宽化语义。
2. **操作步骤**：
   - 在 [fully_connected.cc:59-73](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/fully_connected.cc#L59-L73) 旁标注每个变量的类型后缀：`in_v8` 是 `i8m1`、`in_v16`/`weight_v16` 是 `i16m2`、`acc_v` 是 `i32m4`。
   - 在脑中代入 `VLEN=128`：`e8m1` 的 `vl` 上限是 \(128/8 = 16\)，所以一次 `vwmacc` 最多算 16 个乘积。
3. **观察现象**：`m1`(8bit) → `m2`(16bit) → `m4`(32bit)，LMUL 每步翻倍，正好对应位宽翻倍。
4. **预期结果**：你能解释「为何累加器必须比操作数宽一倍」——乘积会从 8bit×8bit 变成需要 16bit，再累加需要 32bit 才不溢出。
5. 运行结果待本地验证（需 CoralNPU 工具链与 Verilator，参见 u2-l3）。

#### 4.3.5 小练习与答案

- **练习 1**：`acc_v` 为什么选 `i32m4` 而不是 `i32m1`？
  - **答案**：注释（第 55 行）写明「allow headroom」。累加 \(D\) 个 16bit 乘积会增长位数，用更宽的 `m4` 寄存器组提供累加余量，避免溢出；同时与 `e8m1` 操作数在元素数上对齐（`m4` 的 32bit 容量 = `m1` 的 8bit 容量，都是 16 个 `VLEN=128` 元素）。
- **练习 2**：为何全连接不调用 `PostprocessAcc`？
  - **答案**：它每个输出通道只算出一个标量 acc，`PostprocessAcc` 面向「一批 int32 acc」的向量化收尾，对单标量没有收益，故直接标量调用 `MultiplyByQuantizedMultiplier`。

### 4.4 卷积算子（conv.cc）：多策略分发与 MAC 内核

#### 4.4.1 概念说明

卷积是本讲最重头的算子，也是 CoralNPU 作为「ML 加速器」最该跑得快的算子。它的计算量远大于全连接（要遍历空间维度 `out_y × out_x` 和卷积核 `4×4`），所以 `conv.cc` 没有单一实现，而是按**形状**走不同快速路径。`ConvPerChannel` 是一个分发器：它根据 `filter_height/filter_width`、`input_depth`、是否有 repacked 权重、stride 等，挑一条最优内核；都不满足时回退参考核。

CoralNPU 卷积优化的两条核心思路，正好对应 overview.md（u7-l4）的「wide / narrow」外积结构：

1. **权重当 wide（向量）、激活当 narrow（标量广播）**：在 `Conv2D_4x4` 的 repacked 路径里，把同一输入通道、连续若干输出通道的权重**重排成连续内存**，一条 `vle8` 一次取出 `vl` 个输出通道的权重当向量，而激活是单标量，用 `vwmacc_vx`（标量×向量）广播。这样「一个输入元素 × 一组权重」一条指令搞定多个输出通道。
2. **输入通道分块向量化**：在 `Conv_4_4_16_Stride1` / `Conv_4_4_48_Stride1` 里，把 4×4 卷积核的 16 个抽头（每个抽头是一条覆盖输入通道块的向量）**钉在向量寄存器** `v1`–`v16`，用 `vwmacc.vv`（向量×向量）算出部分积向量，最后 `vredsum` 归约成单输出。相邻输出像素可复用输入列（`CONV_MAC_2X`）。

此外有一个跨两条思路都用的优化：**把 `input_offset` 折进偏置**。逐元素加 `input_offset` 太贵，于是预处理时算出每个输出通道的权重和 `weight_sums[oc]`，把 `input_offset × weight_sums[oc]` 一次性并入 `bias`，这样内循环里就不用反复加零点了。

#### 4.4.2 核心流程

`ConvPerChannel` 的分发决策树（简化）：

```text
ConvPerChannel(params, data, ...):
  copy filter/bias 到对齐 DTCM 缓冲
  PrepareShiftParams(...)                       # 拆分有符号 shift
  if 4×4 且 input_depth%4==0 且 repacked_weights:
      → Conv_4x4_OCVectorized   # wide=权重(输出通道向量), input_offset 折进 bias
  elif 4×4 且 repacked_weights_generic 且 od>=32:
      → Conv2D_4x4              # 权重连续重排 + 标量激活广播
  elif 4×4 且 input_depth<=16:
      → Conv_4_4_16             # 4×4×16 抽头钉寄存器 + CONV_MAC
  elif 4×4 且 input_depth<=48 且 stride==1:
      → Conv_4_4_48_Stride1     # 48 拆成 3 个 16 块, 2× 输出像素复用
  elif 4×4 且 repacked_weights_generic:
      → Conv2D_4x4
  else:
      → tflite::reference_integer_ops::ConvPerChannel   # 回退参考核
```

而 `CONV_MAC` 宏（用内联汇编写）每拍做：取 8bit 输入块 → 符号扩展到 16bit → 加 `input_offset` → 与 16bit 权重向量 `vwmacc.vv` 累加。整个 4×4 核循环调用 16 次 `CONV_MAC`（每个空间抽头一次），再 `vredsum` 归约。

#### 4.4.3 源码精读

**A. 分发器 `ConvPerChannel`**：[sw/opt/litert-micro/conv.cc:1240-1332](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/conv.cc#L1240-L1332)
- 把 filter/bias 拷进对齐缓冲、调 `PrepareShiftParams`：第 1273-1292 行；
- if/else 分发链：第 1294-1331 行；
- 兜底回退参考核并打印 `Fallback kernel`：第 1324-1331 行。

**B. `CONV_MAC` 内联汇编宏**：[sw/opt/litert-micro/conv.cc:36-46](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/conv.cc#L36-L46)。它把「取输入→加偏移→宽化乘累加」压成一段固定指令序列，并固定使用 `v18/v30` 等临时寄存器，把 `v1`–`v16` 留给 4×4 权重抽头。其 2× 版本（一次输入同时累加到两个相邻输出像素的 acc）见 [conv.cc:48-61](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/conv.cc#L48-L61)。

**C. 4×4×48 stride1 内核**：[sw/opt/litert-micro/conv.cc:669-929](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/conv.cc#L669-L929)
- 注释说明「48 通道拆 3 个 16 块、外层 batch/output_channel、每步算 2 个输出像素」的策略：第 660-668 行；
- 把 4×4×16 权重抽头钉到 `v1`–`v16`（`register ... __asm__("vN")`）：第 712-756 行；
- 快速路径用 `CONV_MAC`/`CONV_MAC_2X` 遍历 4×4 抽头：第 796-818 行；
- `vredsum` 把部分积向量归约成标量并写入 `accs_buf`：第 899-918 行；
- 全图算完后一次 `PostprocessAcc` 收尾：第 925-928 行。

**D. 「权重当 wide」的 `Conv2D_4x4` repacked 路径**：[sw/opt/litert-micro/conv.cc:234-308](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/conv.cc#L234-L308)。关键三行：

```c
vint8m1_t w = __riscv_vle8_v_i8m1(packed_ptr + kc * output_depth, vl); // 取 vl 个输出通道的权重
vint16m2_t w16 = __riscv_vwadd_vx_i16m2(w, 0, vl);                      // int8→int16
acc0 = __riscv_vwmacc_vx_i32m4(acc0, (int16_t)(pad_ptr[..] + input_offset), w16, vl); // 标量激活 × 向量权重
```

这里 `vwmacc_vx` 的「vx」表示**标量×向量**——单个激活标量广播到整条权重向量，正好对应硬件「wide 权重 × narrow 激活」的外积。注释（第 248-256 行）还解释了「用 padding 缓冲后可无脑访问越界地址，因为 padding 填的是 `-input_offset`，使 `(val + input_offset) = 0`」这一去分支技巧。

**E. 权重重排与 weight_sums**：[sw/opt/litert-micro/conv.cc:947-974](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/conv.cc#L947-L974)（`RepackWeightsD48` 把权重重排成「输出通道连续」并累加 `weight_sums[oc]`），随后在 `Conv_4x4_OCVectorized` 里把 `input_offset × weight_sums` 折进偏置：[conv.cc:1027-1030](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/conv.cc#L1027-L1030)。

> 边界处理：`Conv2D_4x4` 有「整块在内」的快速路径与「越界」的带分支慢速路径之分（见第 793-818 vs 819-895）；`PadInput`/`TiledPadInput`（[conv.cc:84-113](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/conv.cc#L84-L113)、[976-1000](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/conv.cc#L976-L1000)）用 padding 把慢速路径也消除掉。

#### 4.4.4 代码实践

1. **实践目标**：在 `conv.cc` 里标注两条「wide/narrow」数据组织思路分别出现在哪。
2. **操作步骤**：
   - 打开 [conv.cc:234-308](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/conv.cc#L234-L308)（`Conv2D_4x4` repacked 路径），在 `vwmacc_vx` 旁批注「wide=权重向量(输出通道)、narrow=激活标量」。
   - 打开 [conv.cc:796-818](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/conv.cc#L796-L818)（`Conv_4_4_48_Stride1` 快速路径），在 `CONV_MAC` 调用旁批注「向量轴=输入通道块、4×4 抽头钉寄存器、CONV_MAC_2X 复用相邻列」。
   - 跟踪 `input_offset` 是如何被折进 bias 的：从 [RepackWeightsD48:965](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/conv.cc#L965) 的 `weight_sums[oc] += val` 追到 [adjusted_bias:1027-1030](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/conv.cc#L1027-L1030)。
3. **观察现象**：两条路径的 `vwmacc` 后缀不同——一个是 `_vx`（标量×向量），一个是 `_vv`（向量×向量）。
4. **预期结果**：你能向同伴解释「同样是 4×4 卷积，为何 input_depth 小（≤16/48）时走寄存器钉住的 `vv` 路径，而 output_depth 大且有权重重排时走 `vx` 广播路径」。
5. 运行结果待本地验证。

#### 4.4.5 小练习与答案

- **练习 1**：`CONV_MAC` 为什么用内联汇编而不是直接写 intrinsics？
  - **答案**：为了**钉住寄存器**。作者要把 16 个 4×4 权重抽头常驻 `v1`–`v16`，并把临时量固定到 `v18/v30`，避免编译器寄存器分配抖动导致反复 spill。内联汇编的 `"vr"` 约束 + `__asm__("vN")` 寄存器变量能精确控制这点（见第 712-727 行的 `register vint8m1_t fil00 __asm__("v1")`）。
- **练习 2**：`Conv_4_4_48_Stride1` 为什么把 48 个输入通道拆成 3 个 16 块，而不是一次性向量化 48 个？
  - **答案**：受限于向量寄存器资源。`VLEN=128` 下 `e8m1` 一个块最多 16 元素，而 4×4 抽头已占 16 个寄存器；分块复用同一组寄存器既控制寄存器压力，又便于跨块累加进 `accs_buf`。
- **练习 3**：把 `input_offset` 折进 bias 省掉了内循环里的什么操作？
  - **答案**：省掉了「对每个输入元素都加一次 `input_offset`」。因为 \(\sum (x_d + Z_{in}) W_{c,d} = \sum x_d W_{c,d} + Z_{in}\sum W_{c,d}\)，后半项 \(Z_{in}\cdot\mathrm{weight\_sums}[c]\) 与输入无关，可预算进 bias，内循环只算 \(\sum x_d W_{c,d}\)。

### 4.5 深度卷积、池化与算子验证

#### 4.5.1 概念说明

- **深度可分离卷积（depthwise）**：每个输入通道独立卷积，不做通道间混合。它和标准卷积的关键差别是「filter 的 `out_d = in_d × depth_multiplier`」，访存模式不同，所以单独优化。`depthwise_conv.cc` 用**分块（patch）**策略——把输出图按是否触及边界切成「上边/中左/中心/中右/下边」若干 patch，中心 patch 走无边界检查的快速路径；3×3 核还有专门的「**输入列复用**」版本（相邻输出像素复用上一次加载的输入列），含手工内联汇编。
- **池化（pooling）**：`pooling.cc` 是最简单的向量算子——最大池化只需在通道维上反复 `vmax`，无需乘累加、无需量化（int8 in→int8 out，只做钳位）。
- **算子验证（conv_test.cc）**：本仓库验证算子的标准范式是「**同输入、双跑、比结果**」——先用 TFLM 参考核算出 `output_data_ref`，再用优化核算出 `output_data`，二者应逐字节相等；同时用 `mcycle_read()` 记录两边的周期数 `ref_cycles` / `opt_cycles`，量化加速比。

#### 4.5.2 核心流程

深度卷积分块调度（`DepthwiseConvPerChannel`）：

```text
把输出图切成 5 个 patch（按 pad/stride 算边界）：
  Top        (y ∈ [0, out_y_top))       ← DepthwiseConvPerChannelPatch (带边界检查)
  Middle-L   (x ∈ [0, out_x_left))      ← Patch
  Center     (3×3 且 stride==dilation)  ← PatchCenter3x3Reuse6 (列复用, 内联汇编)
  Middle-R   (x ∈ [out_x_right, out_w)) ← Patch
  Bottom     (y ∈ [out_y_bottom, out_h))← Patch
所有 patch 的 int32 acc 汇入 accs_buf → PostprocessAcc 收尾
```

最大池化的向量循环极简：对每个输出像素，把 `depth` 个通道分块，每块用 `vmax` 在 `filter_h × filter_w` 个抽头上取最大，最后钳位。

#### 4.5.3 源码精读

- 深度卷积主路径 `DepthwiseConvPerChannelPatch` 的乘累加：[sw/opt/litert-micro/depthwise_conv.cc:101-139](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/depthwise_conv.cc#L101-L139)。第 118-130 行用 `vlse8`（**带步长**的向量加载，步长 = `depth_multiplier`）从交错布局的 filter 里取同一通道的权重，`vwmacc.vv` 累加；第 130 行是核心乘累加；最后 `vsse32`（带步长存储）把 acc 写回 `accs_buf`，并立即 `PostprocessAcc`（第 142-146 行）。
- 3×3 中心 patch 的输入列复用（手工内联汇编）：[sw/opt/litert-micro/depthwise_conv.cc:262-358](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/depthwise_conv.cc#L262-L358)。它对 3 行各算一次「取新列 → sext → vadd offset → vwmacc」，并用 `vmv1r.v` 把上一列挪给下一轮复用（注释里多次写「Moved」标注复用）。
- 分块调度：[sw/opt/litert-micro/depthwise_conv.cc:433-471](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/depthwise_conv.cc#L433-L471)（Top/MiddleL/Center/MiddleR/Bottom 五段）。
- 最大池化：[sw/opt/litert-micro/pooling.cc:60-90](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/pooling.cc#L60-L90)。第 64 行用 `-128` 初始化最大值向量，第 76 行 `vmax_vv` 在抽头上取最大，第 81-82 行钳位后 `vse8` 写出。
- 算子验证 `conv_test.cc`：所有张量都用 `__attribute__((section(".data")))` 钉进 DTCM（见 u2-l2），用 `extern "C"` 暴露给仿真主机：[sw/opt/litert-micro/test/conv_test.cc:13-67](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/test/conv_test.cc#L13-L67)。
- 参考核 `run_ref`（用 `mcycle_read` 计时）：[sw/opt/litert-micro/test/conv_test.cc:69-92](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/test/conv_test.cc#L69-L92)。
- 优化核 `run_optimized`（构造 mock `TfLiteContext` 提供 scratch 分配、按形状 repack 权重、再调 `ConvPerChannel` 并计时）：[sw/opt/litert-micro/test/conv_test.cc:94-170](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/test/conv_test.cc#L94-L170)。第 132-141 行针对 `4×4×48×48` 形状调 `RepackWeightsD48`，正好触发 4.4 节的快速路径。
- `run_verify` 先 ref 后 opt、`impl` 函数指针默认指向它：[sw/opt/litert-micro/test/conv_test.cc:172-177](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/test/conv_test.cc#L172-L177)。

#### 4.5.4 代码实践

1. **实践目标**：用 `conv_test.cc` 把一个 4×4×48×48 卷积在仿真器上跑通，回收 `output_data` 与 `output_data_ref`、`opt_cycles` 与 `ref_cycles`。
2. **操作步骤**：
   - 按 u2-l3 构建 Verilator 仿真器：`bazel build //tests/verilator_sim:core_mini_axi_sim`。
   - 构建测试二进制：`bazel build //sw/opt/litert-micro/test:conv_test`（产出 `.elf`）。
   - 用 cocotb 测试台（u2-l4）`load_elf` 加载该 `.elf`，`lookup_symbol` 找到 `input_data`/`filter_data`/`output_data`/`output_data_ref`/`opt_cycles`/`ref_cycles` 等符号地址。
   - `write` 注入随机 int8 输入与权重，`execute_from` 启动，`wait_for_halted` 后 `read` 回两组输出与周期数。
3. **观察现象**：`output_data` 与 `output_data_ref` 应逐字节相等（int8 路径要求位级精确）；`opt_cycles` 应显著小于 `ref_cycles`。
4. **预期结果**：算子正确性通过（两份输出一致），并能给出一个加速比数字。
5. 运行结果待本地验证；若无法本地仿真，可改为「源码阅读型实践」——对照 `conv_test.cc:94-170` 解释 `run_optimized` 为何要 mock 出 `GetScratchBuffer`/`AllocatePersistentBuffer`（因为裸机无 TFLM 运行时，这些回调必须自己供给）。

#### 4.5.5 小练习与答案

- **练习 1**：深度卷积为何用 `vlse8`（带步长加载）取 filter？
  - **答案**：depthwise filter 在内存里按 `[out_d][kh][kw]` 排布，而 `out_d = in_d × depth_multiplier`。同一空间抽头、同一输入通道对应的 `depth_multiplier` 个权重在内存里**步长为 `depth_multiplier`** 地交错排列，`vlse8` 的步长参数正好把它们聚拢成一条向量。
- **练习 2**：最大池化为何不需要 `PostprocessAcc`？
  - **答案**：池化只做 `max`，没有乘累加、不产生 int32 acc、也不含量化缩放，int8 输入直接 int8 输出，只需 `vmax`/`vmin` 钳位即可。
- **练习 3**：`conv_test.cc` 里 `impl` 是一个函数指针并默认指向 `run_verify`，这样设计有什么好处？
  - **答案**：仿真主机只需改写 `impl` 指向（如指向单独的 `run_ref` 或 `run_optimized`），就能在不重编内核的前提下选择「只跑参考核」「只跑优化核」或「双跑比对」三种模式，便于分阶段调试与精确测时。

## 5. 综合实践

**任务：为 `conv_test.cc` 选定一个 4×4×16×16 的形状，画出从「输入注入」到「输出回收」的完整数据通路，并定位它会命中 `ConvPerChannel` 的哪条快速路径。**

建议步骤：

1. 在 [conv.cc:1294-1331](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/litert-micro/conv.cc#L1294-L1331) 的分发链里代入 `filter_height=4, filter_width=4, input_depth=16, output_depth=16`，确认它命中 `Conv_4_4_16`（因 `input_depth <= 16`），进而走 `Conv_4_4_16_StrideN`。
2. 跟踪 `Conv_4_4_16_StrideN`：标注它如何把 4×4 抽头钉到寄存器、如何用 `CONV_MAC` 累加、如何在 chunk=0 时初始化 `accs_buf`、其它 chunk 时累加，最后 `PostprocessAcc` 收尾。
3. 用一张图把数据流串起来：cocotb 主机 `write input_data/filter_data`（DTCM）→ `execute_from`（内核跑 `run_optimized`）→ `ConvPerChannel` 分发 → `Conv_4_4_16_StrideN` 向量乘累加 → `accs_buf`（scratch）→ `PostprocessAcc` → `output_data`（DTCM）→ 主机 `read output_data`。
4. 进阶：把形状换成 `4×4×48`、`stride=1`，确认命中 `Conv_4_4_48_Stride1`，并对照 4.4 节解释它为何改用「输入通道分块 + 2× 像素复用」策略。

> 这一实践把本讲的「注册机制 → 量化后处理 → 向量乘累加 → 分发策略 → 测试验证」全部串起来，是过渡到 u10-l3（MobileNet 端到端仿真）的最后一块拼图。

## 6. 本讲小结

- `sw/opt/litert-micro/` 是**软件算子层**：用 RVV（`<riscv_vector.h>` + 内联汇编）把 TFLM 的 int8 算子重写为 CoralNPU 向量后端友好的实现，通过 `Register_*` **只替换 `invoke`（必要时连 `init/prepare`）**接入模型，类型不匹配或形状不支持时回退到 TFLM 参考核。
- 核心计算原语是 `vwmacc`（宽化乘累加，16bit×16bit→32bit）与 `vredsum`（跨道归约）；全连接是其最简范式——`vsetvli` 分块 → `vwmacc.vv` → `vredsum` → 标量量化。
- 量化后处理集中在 `accumulator_util.h`：`PrepareShiftParams` 把有符号 shift 拆成左/右两个无符号移位量；`PostprocessAcc` 跑「加偏置→左移→`vsmul`→右移→加零点→`vnclip` 收窄→钳位」。卷积/深度卷积把整张 int32 acc 攒进 `accs_buf` 后批量收尾，摊薄量化代价。
- 卷积 `ConvPerChannel` 是多策略分发器，按形状选 4×4 的多条快速路径；数据组织有两种「wide/narrow」思路：`Conv2D_4x4` 把权重当向量、激活当标量广播（`vwmacc_vx`），`Conv_4_4_16/48` 把输入通道块向量化、4×4 抽头钉寄存器（`vwmacc_vv`）。`input_offset × weight_sums` 被预算进 bias 以省掉内循环加零点。
- 深度卷积用 patch 分块消除边界分支、3×3 中心走输入列复用的内联汇编；池化是最简的通道向 `vmax`。
- 算子验证遵循「同输入双跑比对」：`conv_test.cc` 同时跑参考核与优化核、用 `mcycle_read` 记录周期数，由 cocotb/npusim 驱动 Verilator 仿真回收结果（对接 u2-l3/u2-l4/u10-l3）。

## 7. 下一步学习建议

- **向上一层（端到端）**：阅读 u10-l3「npusim 与 MobileNet 端到端」，看本讲的 `Register_CONV_2D`/`Register_DEPTHWISE_CONV_2D` 如何被 `tests/npusim_examples/run_full_mobilenet_v1.cc` 装进 op resolver、把整个 MobileNet 在 `coralnpu_v2_sim` 上跑通。
- **向下一层（硬件映射）**：回到 u7-l4「MAC 外积乘累加引擎」，对照 `rvv_backend_mulmac.sv`，理解本讲发出的 `vwmacc.vv` 指令在硬件上如何被 stripmining（u7-l6）展开成 4 次发射、并落到 wide×narrow 外积的 VDOT 上——把「软件写的向量」与「硬件算的 MAC」对应起来。
- **横向补充**：本讲未细讲的 `logistic.cc`（sigmoid 的查表+插值实现）是另一类「非线性激活」算子，建议作为练习自行阅读，体会「非乘累加型算子」在 RVV 上的写法。
- **动手方向**：仿照 `conv_test.cc`，为 `fully_connected.cc` 写一个最小 `*_test.cc`（参考核 `tflite::reference_integer_ops::FullyConnected` vs 优化核），构建并仿真，验证 int8 位级一致并测出加速比。
