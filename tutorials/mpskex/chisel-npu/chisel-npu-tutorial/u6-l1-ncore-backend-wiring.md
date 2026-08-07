# NCoreBackend 总体连线

## 1. 本讲目标

本讲是「神经核心集成」单元（U6）的第一讲。前面几个单元我们分别学完了指令译码器（u2-l5）、控制 Bundle（u3-l1）、多宽度寄存器堆（u3-l2）、矩阵乘引擎 MMALU（u4 全单元）和向量 ALU VALU（u5-l1）。这些零件到目前为止都是**孤立的模块**——译码器只输出一个 `DecodedMicroOp`，寄存器堆只提供读写端口，MMALU 和 VALU 各自有 IO 却没人去驱动它们。

本讲要回答的核心问题是：**谁把这些零件连起来？数据从一条 32 位指令字出发，怎样流经译码器、寄存器堆，最终到达 MMALU 或 VALU 的输入，算完后又怎样写回寄存器堆？**

学完本讲你应该能够：

1. 说出 `NCoreBackend` 实例化的四个核心子模块，以及把 `K` 当作 `n` 传给 MMALU 这种「靠构造对齐参数」的做法。
2. 看懂并默写出寄存器堆的**端口分配表**——哪个 VX 读端口驱动 MMALU、哪个驱动 VALU、写端口又分给谁。
3. 理解 MMALU 的 INT32 结果如何**无截断直写 VR**，VALU 如何**按宽度写回**对应寄存器类。
4. 分清外部读写端口（`ext`）和 VR 结果读取端口的用途。
5. 养成一个源码阅读的关键习惯：**核对注释与代码是否一致**——本讲会实打实地指出几处注释落后于代码的地方。

> 说明：本讲只讲**连线（wiring）**——谁连到谁。至于「译码后按 family 分发、写回使能的各种守卫条件、VALU 两拍写回时序」等更细的分发逻辑，留给下一讲 u6-l2《指令分发与写回时序》。

## 2. 前置知识

本讲默认你已经掌握以下概念（若不熟，请先回看对应讲义）：

- **DecodedMicroOp（u2-l5 / u3-l1）**：译码器把 32 位指令字组合译码出的「包中包」控制结构，含 `family`、`rd/rs1/rs2`，并嵌套整块 `NCoreVALUBundle` 送 VALU。
- **多宽度寄存器堆 MultiWidthRegisterBlock（u3-l2）**：只有一块物理存储，VX/VE/VR 是它的三种别名视图；提供多组读写端口，写优先级 VR>VE>VX>ext。
- **MMALU（u4-l5）**：K×K 脉动阵列，输入是 VX 宽度（INT8）的 `in_a/in_b`，输出是 `Vec(K, SInt(4N.W))` 的 INT32 累加结果，流水延迟约 3K−1 拍。
- **VALU（u5-l1）**：K 通道向量 ALU，有三套宽度输入（VX/VE/VR）和三套寄存输出，由 `ctrl.regCls`（0/1/2=VX/VE/VR）总控。
- **全局参数 N/L/K（u1-l4）**：N=基础位宽(8)，L=VX 寄存器数(32，须被 4 整除)，K=每寄存器 SIMD 通道数(测试 8)。

一个贯穿全讲的关键 Chisel 语义：**对同一个输入信号多次用 `:=` 赋值时，「最后连接胜出」（last connection wins）**。本讲会用到这条规则来解释几处看似重复或被覆盖的连线。

## 3. 本讲源码地图

| 文件 | 作用 |
|:---|:---|
| [src/main/scala/backend/SimpleBackend.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala) | 本讲主角。定义 `NCoreBackend`，实例化并连线译码器、寄存器堆、MMALU、VALU。 |
| [docs/implementations/NeuralCore.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/NeuralCore.md) | 设计文档，给出整体框图与参数约束表（部分描述已落后于代码，本讲会指出）。 |
| [src/main/scala/sram/multiWidthRegister.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala) | 寄存器堆本体，端口定义与写优先级的物理来源。 |
| [src/main/scala/alu/mma/mma.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala) | MMALU 的 IO 定义，确认 `in_a/in_b` 是 VX 宽度、`out` 是 4N 宽度。 |
| [src/main/scala/isa/instrDecoder.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala) | `DecodedMicroOp` 输出 Bundle 的定义。 |
| [src/test/scala/backend/NCoreBackendQuantSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala) | 后端集成测试，演示如何从外部驱动这些 IO 端口。 |

## 4. 核心概念与源码讲解

### 4.1 NCoreBackend：四子模块总装与参数对齐

#### 4.1.1 概念说明

`NCoreBackend` 是 NPU 的「中央执行单元」。它的职责不是自己算什么，而是**当总装车间**：把译码器、寄存器堆、MMALU、VALU 这四个零件摆好，用连线（wire）把它们的 IO 对接起来，形成一条「取指 → 译码 → 读寄存器 → 执行 → 写回」的数据通路。

设计文档用一句话点明了它的超流水思想（可对照阅读）：

> The Neural Core (`NCoreBackend`) is the central execution unit of the NPU. It integrates an instruction decoder, a multi-width register file, the systolic-array matrix engine (MMALU), and the vector ALU (VALU) into a single pipelined backend.

这条通路之所以值得单独研究，是因为它要解决一个**端口争用**问题：寄存器堆的读写端口是稀缺资源（不是无限多个），而 MMALU 和 VALU 都要读源操作数、都要写结果。后端必须在有限的端口里做一个清晰的分配。

另一个关键概念是**参数对齐约束**：MMALU 的阵列边长 `n`、数据位宽 `nbits`、累加位宽 `accum_nbits`，必须分别等于 `K`、`N`、`4N`。本讲会看到，这个约束不是靠运行时检查，而是**靠把 K/N/4N 直接当作构造参数传进 MMALU 来保证**的。

#### 4.1.2 核心流程

整个后端的数据流可以这样用文字流程图描述：

```
io.instr (32-bit)
      │
      ▼
 ┌────────────┐  decoded (DecodedMicroOp)   ┌──────────────────────┐
 │ InstrDecoder│──────────────────────────▶│  family / rd / rs1.. │
 └────────────┘                              │  mma_keep            │
      │ 同一拍组合输出                        │  valu(NCoreVALUBundle)│
      │                                      └──────────────────────┘
      │ address ports (io.*_addr)                     │
      ▼                                              │ ctrl
 ┌──────────────────────┐  vx/ve/vr read data        ▼
 │ MultiWidthRegister   │──────────────────▶ ┌───────────┐  ┌────────┐
 │ Block  (RF)          │   VX port 0 ──────▶│  MMALU    │  │  VALU  │
 │                      │   VX port 3 (mux)─▶│  in_a/in_b│  │ in_*   │
 │                      │   VE/VR ports ────▶│           │  │        │
 │                      │◀───────────────────│  out(4N)  │  │ out_*  │
 │                      │  VR write 1 (MMALU)│           │  │        │
 │                      │◀───────────────────└───────────┘  └────────┘
 │                      │  VX/VE/VR write 0 (VALU, 按宽度)
 └──────────────────────┘
      │ ext_r_data / vr_r_data
      ▼
 io.ext_rd_data / io.vr_rd_data  (供外部/test 读取结果)
```

四个子模块各司其职：

- **InstrDecoder**：纯组合，1 拍把指令字翻成 `DecodedMicroOp`。
- **MultiWidthRegisterBlock**：存储与别名视图，提供多组读写端口。
- **MMALU**：慢通道，K×K 阵列，约 3K−1 拍出 INT32 结果。
- **VALU**：快通道，1 拍出结果（vfma 2 拍），三宽度。

#### 4.1.3 源码精读

先看类定义、参数与约束。[SimpleBackend.scala:52-66](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L52-L66)：

```scala
class NCoreBackend(
    val K: Int = 8,
    val N: Int = 8,
    val L: Int = 32,
) extends Module {
  require(L % 4 == 0, s"NCoreBackend: L=$L must be divisible by 4")
  require(K > 0 && N > 0)

  val N2 = 2 * N
  val N4 = 4 * N

  val VX_ADDR = log2Ceil(L)        // 默认 5
  val VE_ADDR = log2Ceil(L / 2)    // 默认 4
  val VR_ADDR = log2Ceil(L / 4)    // 默认 3
```

注意一个**诚实点**：设计文档 [NeuralCore.md 的参数约束表](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/NeuralCore.md#L127-L132) 写着 `K == mmalu.n`、`N == mmalu.nbits` 等「由 `require` 强制」。但代码里**并没有** `require(K == mmalu.n)`——这三条约束实际是靠下面这一行**构造期对齐**来保证的（[SimpleBackend.scala:158](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L158)）：

```scala
val mmalu = Module(new MMALU(new MMPE(N), K, N, N4))
```

对照 [MMALU 的构造签名](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L23)：`MMALU(pe_gen, n, nbits, accum_nbits)`。所以 `n=K`、`nbits=N`、`accum_nbits=N4=4N`——三者全部**因为实参就是 K/N/4N 而天然相等**，根本不可能不一致。这是比 `require` 更强的保证：`require` 是「运行到才发现错」，而构造对齐是「语法上就没机会写错」。代码里唯一的 `require` 只检查 `L%4==0`（VR 别名需要 VX 每 4 行一组）和 `K,N>0`。

再看宽度常量小工具 [SimpleBackend.scala:50](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L50)：

```scala
private object W { val VX = 0.U(2.W); val VE = 1.U(2.W); val VR = 2.U(2.W) }
```

它的存在是因为 `dec.valu.regCls` 是 `UInt(2.W)`（译码器刻意没用 `VecWidth` 这个 ChiselEnum，见 u3-l1 改名说明），后端要拿它和 0/1/2 比较来选写回端口，于是定义这个本地常量对象，免去导入枚举。

#### 4.1.4 代码实践

**目标**：把后端真正跑起来，确认这四个子模块能被 elaborate（精细化）成一个整体，并跑通一条最简单的端到端用例。

**操作步骤**：

1. 用 sbt 单跑后端集成测试（类名来自 `src/test/scala/backend/NCoreBackendQuantSpec.scala` 的 `package backend`）：
   ```bash
   sbt "testOnly backend.NCoreBackendQuantSpec"
   ```
   或用项目封装（u1-l2 介绍过）：`make test`（会跑全部测试）。
2. 观察输出中的 `execute vadd via backend` 这一条用例。

**需要观察的现象**：测试通过；`vadd` 用例先通过外部写端口把两个 INT8 向量写进 VX，再发一条 `vadd` 指令，最后从 VX 读回逐通道相加结果（参考 [NCoreBackendQuantSpec.scala:92-109](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala#L92-L109)）。这说明译码器 → 寄存器堆 → VALU → 写回 的链路是通的。

**预期结果**：测试全绿。若你的环境没有装好 sbt/firtool，可改用 Docker（u1-l2 的 `make container` 进容器后再 `sbt testOnly ...`）。若仍无法运行，请标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `NCoreBackend` 的 `L` 改成 30（不被 4 整除），elaborate 时会发生什么？
**答案**：elaborate 时 `require(L % 4 == 0)` 抛出 `IllegalArgumentException`，编译直接失败。这是 u3-l2 讲过的「VR 别名需要每 4 行一组」的物理要求。

**练习 2**：为什么代码用「把 K 当作 n 传进去」而不是写一句 `require(mmalu.n == K)`？
**答案**：构造对齐在语法层就消灭了不一致的可能，比运行期 `require` 更早暴露问题；且 MMALU 的 `n` 是 val 参数，在实例化那一刻就被 K 钉死，根本没有「先错再查」的窗口。

**练习 3**：`N4 = 4*N` 这个量在本讲里被用作谁的位宽？
**答案**：它同时是 MMALU 的累加位宽 `accum_nbits`（实参）、MMALU 输出 `out` 的元素宽度（`SInt(4N.W)`）、以及 VR 每个 lane 的宽度（4N 位，即 INT32/FP32）——这正是「MMALU 结果能无截断塞进 VR」的位宽基础。

---

### 4.2 多宽度寄存器堆的读写端口分配

#### 4.2.1 概念说明

寄存器堆 `MultiWidthRegisterBlock` 本身（u3-l2 已学）是「给一组读写端口，内部做别名与冲突裁决」。但**哪些消费者用哪个端口**，是 `NCoreBackend` 这一层决定的。这就是「端口分配」。

可以把寄存器堆想象成一栋大楼，读写端口是大楼的门。MMALU、VALU、外部 DMA 都要进出这栋楼，但门只有那么多，后端就是那个**分配门禁卡的门房**：MMALU A 走 0 号 VX 读门、VALU 的 VX 操作数走 1/2 号、外部读走 3 号（且和 MMALU B 分时复用）。

理解端口分配是读懂后端连线的钥匙：只要知道「读端口 X 驱动谁、写端口 Y 来自谁」，整张数据流图就活了。

#### 4.2.2 核心流程

寄存器堆的实例化（[SimpleBackend.scala:117-118](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L117-L118)）先规定了**端口数量**：

```scala
val rf = Module(new MultiWidthRegisterBlock(L, K, N,
  vx_rd = 4, vx_wr = 2, ve_rd = 2, ve_wr = 1, vr_rd = 2, vr_wr = 2))
```

即：VX 有 4 读 2 写、VE 有 2 读 1 写、VR 有 2 读 2 写，外加一组独立的 `ext`（外部）读写口。这些数字必须**够分**给所有消费者。

读端口分配的代码逻辑（见 4.2.3）做了三件事：①把每个 VX/VE/VR 读端口的地址接到对应的 `io.*_addr` 输入；②把读出的数据送到 MMALU 或 VALU 的输入；③**默认禁用所有写端口**，再在后面按 family 条件性地打开。

写端口分配则遵循「VALU 用 port 0，MMALU 用 VR 的 port 1」的约定，并在寄存器堆内部按 VR>VE>VX>ext 的优先级裁决（u3-l2 已学）。

#### 4.2.3 源码精读

先看源码顶部的**端口分配注释表**——这是作者写给读者的「设计意图版」分配方案，[SimpleBackend.scala:19-26](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L19-L26)：

```
//    VX read:  port 0 = MMALU in_a; port 1 = VALU in_a_vx
//    VX read:  port 2 = VALU in_b_vx; port 3 = external read
//    VE read:  port 0 = VALU in_a_ve; port 1 = VALU in_b_ve
//    VR read:  port 0 = VALU in_a_vr + MMALU in_b; port 1 = VALU in_b_vr + in_c_vr
//    VX write: port 0 = VALU narrow out; port 1 = external write
//    VE write: port 0 = VALU VE out
//    VR write: port 0 = VALU VR out; port 1 = MMALU accumulator direct (INT32, no truncation)
```

**但请注意：这张注释表有几处已经落后于真实代码。** 读源码最重要的习惯就是「以代码为准，注释只作参考」。下面我们逐条核对真实连线。

读端口的真实接线，[SimpleBackend.scala:120-137](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L120-L137)：

```scala
// ---- VX reads ----
rf.io.vx_r_addr(0) := io.mma_a_addr    // MMALU A
rf.io.vx_r_addr(1) := io.vx_a_addr     // VALU in_a_vx
rf.io.vx_r_addr(2) := io.vx_b_addr     // VALU in_b_vx
rf.io.vx_r_addr(3) := io.ext_rd_addr   // external      ← 注意：后面会被覆盖
io.ext_rd_data      := rf.io.vx_r_data(3)
...
rf.io.ext_r_addr := io.ext_rd_addr
```

把代码与注释逐条比对，可以整理出**实际读端口分配表**（以代码为准）：

| 寄存器类 | 读端口 | 地址来源 | 实际驱动谁 |
|:---|:---|:---|:---|
| VX | port 0 | `io.mma_a_addr` | **MMALU in_a** |
| VX | port 1 | `io.vx_a_addr` | VALU in_a_vx |
| VX | port 2 | `io.vx_b_addr` | VALU in_b_vx |
| VX | port 3 | `Mux(ext 活跃, ext_rd_addr, mma_b_addr)` | **外部读 ext_rd_data** 与 **MMALU in_b**（分时复用） |
| VE | port 0 | `io.ve_a_addr` | VALU in_a_ve |
| VE | port 1 | `io.ve_b_addr` | VALU in_b_ve |
| VR | port 0 | `io.vr_a_addr` | VALU in_a_vr，以及 `io.vr_rd_data` |
| VR | port 1 | `io.vr_b_addr` | VALU in_b_vr 与 in_c_vr（C 暂复用 B 口） |

这里有两处典型的「注释落后于代码」需要特别提醒：

**① VX 读端口 3 其实是与 MMALU in_b 分时复用，不是纯外部读。** 注释说「port 3 = external read」，但真实代码在 [SimpleBackend.scala:165](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L165) 用一个 Mux **覆盖**了上面的赋值（Chisel「最后连接胜出」）：

```scala
rf.io.vx_r_addr(3) := Mux(io.ext_wr_en || io.ext_rd_addr.orR, io.ext_rd_addr, io.mma_b_addr)
mmalu.io.in_b     := VecInit(rf.io.vx_r_data(3).map(_.asSInt))
```

也就是说：当外部写或外部读地址非零（外部正在活动）时，3 号口服务外部；否则 3 号口服务于 MMALU 的 in_b（由 `io.mma_b_addr` 指定）。这是一个**为了省一个读端口而做的时分复用折中**。

**② MMALU 的 in_b 来自 VX（INT8），不是注释里写的 VR。** 注释表写「VR read: port 0 = VALU in_a_vr + MMALU in_b」，但 MMALU 的 `in_b` 在 [mma.scala:25-26](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L25-L26) 是 `Vec(n, SInt(nbits.W))` 即 VX 宽度（INT8），物理上不可能来自 VR（4N=32 位）。代码实际把 in_b 接到了 VX port 3，注释那句「+ MMALU in_b」应理解为历史残留。

> 另一处细节：[SimpleBackend.scala:137](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L137) 还把 `rf.io.ext_r_addr := io.ext_rd_addr`。这是寄存器堆**独立**的外部读口（与 `vx_r_addr` 向量无关）。后端输出 `io.ext_rd_data` 取的是 `vx_r_data(3)` 而非 `ext_r_data`，但 `ext_r_addr` 仍必须驱动（否则 u3-l2 提到的 firtool「uninitialized sink」会报警）。所以这里看似冗余，实则是「必驱」。

再看**写端口**。代码先把所有写端口默认禁用，[SimpleBackend.scala:139-148](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L139-L148)：

```scala
rf.io.vx_w_en  := VecInit(Seq.fill(2)(false.B))   // VX 2 个写口默认关
rf.io.ve_w_en  := VecInit(Seq.fill(1)(false.B))   // VE 1 个写口默认关
rf.io.vr_w_en  := VecInit(Seq.fill(2)(false.B))   // VR 2 个写口默认关
// addr / data 同样填 0 ……
```

外部写则走**独立的 ext 口**而非 VX 写 port 1，[SimpleBackend.scala:150-153](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L150-L153)：

```scala
rf.io.ext_w_en   := io.ext_wr_en
rf.io.ext_w_addr := io.ext_wr_addr
rf.io.ext_w_data := io.ext_wr_data
```

于是真实**写端口分配表**为：

| 寄存器类 | 写端口 | 实际来源 | 备注 |
|:---|:---|:---|:---|
| VX | port 0 | VALU `out_vx` | 仅 isVALU 且 regCls=VX 等条件成立时打开（4.3 节、u6-l2 详述） |
| VX | port 1 | **未使用（恒禁用）** | 注释说「port 1 = external write」，但外部写实际走 ext 口 |
| VE | port 0 | VALU `out_ve` | |
| VR | port 0 | VALU `out_vr` | FP/INT32/宽转换/归约结果 |
| VR | port 1 | **MMALU `out`（INT32 无截断）** | 仅 family==MMA 时打开 |
| ext | — | `io.ext_wr_*` | 独立口，优先级最低 |

这里又有第三处注释偏差：注释说「VX write port 1 = external write」，但外部写走的是 `ext_w` 专用口，`vx_w_en(1)` 自始至终保持默认 `false`，**未被任何分支驱动**。

写优先级裁决在寄存器堆内部，[multiWidthRegister.scala:128-190](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/sram/multiWidthRegister.scala#L128-L190)：先放 ext（最低），再 VX，再 VE，最后 VR（最高），靠 Chisel「最后连接胜出」逐行覆盖，杜绝多驱动。

#### 4.2.4 代码实践

**目标**：亲手把「读端口分配」从源码里挖出来，画成一张表，并核对出注释与代码的全部出入。

**操作步骤**：

1. 打开 [SimpleBackend.scala 的 120-137 行](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L120-L137)，对每个 `rf.io.vx_r_addr(p) :=` 记下它的地址源。
2. 注意第 165 行对 `vx_r_addr(3)` 的二次赋值，判断它在「最后连接胜出」规则下的真实取值。
3. 全文搜索 `rf.io.vx_r_data(`、`rf.io.ve_r_data(`、`rf.io.vr_r_data(`，确认每个读端口的**数据**被送给谁（MMALU 还是 VALU 还是外部输出）。
4. 把结果填进一张「端口 → 地址源 → 消费者」三列表，与本讲 4.2.3 的表对照。

**需要观察的现象**：你会得到三处「注释与代码不符」——VX port 3 的复用、MMALU in_b 来自 VX 而非 VR、VX 写 port 1 实际未用。

**预期结果**：你画出的表与本讲 4.2.3 的「实际读/写端口分配表」一致。这正是源码阅读型实践的价值：注释会陈旧，代码不会。

#### 4.2.5 小练习与答案

**练习 1**：为什么 VX 要开 4 个读端口？分别属于谁？
**答案**：4 个读端口分别给 MMALU in_a（port 0）、VALU in_a_vx（port 1）、VALU in_b_vx（port 2），以及外部读 / MMALU in_b 分时复用（port 3）。读端口不能共享地址，所以需要这么多。

**练习 2**：`rf.io.ext_r_addr := io.ext_rd_addr`（第 137 行）看起来和 `vx_r_addr(3)` 重复，为什么还要写？
**答案**：`ext_r_addr` 是寄存器堆**独立**的外部读口，与 VX 读口向量无关；它即使不被后端用作输出，也必须被驱动到一个确定值，否则其异步读会触发 firtool 的 `uninitialized sink` 警告/错误（u3-l2 已说明）。

**练习 3**：如果两个写端口在同一拍往**同一行** VX 写不同数据，会怎样？
**答案**：寄存器堆内部按优先级 VR>VE>VX>ext 裁决（最后连接胜出），结果是确定性的——高优先级者的数据胜出，不会产生多驱动报错。但软件需要避免这种「同拍重叠写」以免丢数据。

---

### 4.3 MMALU 与 VALU 的实例化与连线

#### 4.3.1 概念说明

4.2 解决了「寄存器堆端口分给谁」，4.3 解决「MMALU 和 VALU 的输入怎么接、输出怎么写回」。

两个执行单元有截然不同的性格：

- **MMALU 是「慢而宽」**：吃 INT8（VX 宽度），吐 INT32（4N 宽度），算一次要约 3K−1 拍。它的结果天然是宽的，所以直接写 VR，且**不截断**——保留完整 32 位累加精度。
- **VALU 是「快而多宽」**：1 拍出结果，有三套宽度输入和三套宽度输出。它按当前指令的 `regCls` 决定写回哪类寄存器。

这一节的连线就是把 4.2 的读端口数据接到两个执行单元的输入，再把它们的输出接回 4.2 的写端口。

#### 4.3.2 核心流程

**MMALU 连线**：

```
rf VX port0 ──(asSInt)──▶ mmalu.in_a
rf VX port3 ──(asSInt)──▶ mmalu.in_b        (与外部读分时复用)
常量 0      ──────────────▶ mmalu.in_accum   (偏置当前固定为 0)
dec.mma_keep ─────────────▶ mmalu.ctrl.keep
false.B      ─────────────▶ mmalu.ctrl.use_accum
(family==MMA)─────────────▶ mmalu.ctrl.busy
mmalu.out (4N, 无截断) ───▶ rf VR write port 1   (仅 family==MMA 时)
```

**VALU 连线**：

```
rf vx_r_data(1) ─▶ valu.in_a_vx     rf ve_r_data(0) ─▶ valu.in_a_ve     rf vr_r_data(0) ─▶ valu.in_a_vr
rf vx_r_data(2) ─▶ valu.in_b_vx     rf ve_r_data(1) ─▶ valu.in_b_ve     rf vr_r_data(1) ─▶ valu.in_b_vr
                                                                     rf vr_r_data(1) ─▶ valu.in_c_vr (C 暂复用 B 口)
dec.valu ───────────────────▶ valu.ctrl        (整块 NCoreVALUBundle 直送)

valu.out_vx ─▶ rf VX write port 0   (regCls==VX 等条件)
valu.out_ve ─▶ rf VE write port 0   (regCls==VE)
valu.out_vr ─▶ rf VR write port 0   (regCls==VR 或宽转换/归约)
```

注意 `dec.valu` 是**整块**赋给 `valu.io.ctrl`（`:=`），因为译码器已经把所有 VALU 控制字段打包进 `NCoreVALUBundle`，后端无需逐字段搬运。

#### 4.3.3 源码精读

**MMALU 侧**。[SimpleBackend.scala:158-182](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L158-L182)：

```scala
val mmalu = Module(new MMALU(new MMPE(N), K, N, N4))
mmalu.io.in_a := VecInit(rf.io.vx_r_data(0).map(_.asSInt))   // VX port 0 → in_a
...
mmalu.io.in_b     := VecInit(rf.io.vx_r_data(3).map(_.asSInt)) // VX port 3 → in_b
mmalu.io.in_accum := VecInit(Seq.fill(K)(0.S(N4.W)))           // 偏置固定 0

mmalu.io.ctrl.keep      := dec.mma_keep
mmalu.io.ctrl.use_accum := false.B
mmalu.io.ctrl.busy      := (dec.family === OpFamily.MMA)

// MMALU 写回直接进 VR port 1 —— 不做精度截断
when (dec.family === OpFamily.MMA) {
  rf.io.vr_w_en(1)   := true.B
  rf.io.vr_w_addr(1) := io.mma_out_addr
  for (lane <- 0 until K) {
    rf.io.vr_w_data(1)(lane) := mmalu.io.out(lane).asUInt   // 4N 位原样写
  }
}
```

几个要点：

- `in_a`/`in_b` 用 `.map(_.asSInt)` 把 VX 的 `UInt(N.W)` 转成 MMALU 要的 `SInt(N.W)`，纯类型桥接，位模式不变。
- `in_accum`（偏置/初始累加值）当前**硬编码为全 0**，即 `Y = BᵀA + 0`，偏置路径在 backend 层尚未接通（u4-l5 提到这是待完成项）。
- **无截断直写**：`mmalu.io.out` 是 `Vec(K, SInt(4N.W))`，`asUInt` 后直接写进 VR port 1，而 VR lane 宽度正好是 4N，所以 32 位累加结果**一位不丢**地存进 VR。这是量化流水线（u7）能保持精度的物理基础。
- `mma_out_addr` 是 VR 地址（注意 `io.mma_out_addr` 在 io bundle 里是 `UInt(VR_ADDR.W)`，[SimpleBackend.scala:92](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L92)）。

**VALU 侧**。[SimpleBackend.scala:187-201](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L187-L201)：

```scala
val valu = Module(new VALU(K, N))
for (lane <- 0 until K) {
  valu.io.in_a_vx(lane) := rf.io.vx_r_data(1)(lane)
  valu.io.in_b_vx(lane) := rf.io.vx_r_data(2)(lane)
  valu.io.in_a_ve(lane) := rf.io.ve_r_data(0)(lane)
  valu.io.in_b_ve(lane) := rf.io.ve_r_data(1)(lane)
  valu.io.in_a_vr(lane) := rf.io.vr_r_data(0)(lane)
  valu.io.in_b_vr(lane) := rf.io.vr_r_data(1)(lane)
  valu.io.in_c_vr(lane) := rf.io.vr_r_data(1)(lane)  // C 暂复用 B 口
}
valu.io.ctrl := dec.valu          // 整块控制包直送
```

可以看到 VALU 的三套宽度输入被**同时**连上（不是按指令切换），每拍三个端口都有数据；真正「哪个有效」由 VALU 内部按 `ctrl.regCls` 选择（u5-l1 的「通路复用」）。`in_c_vr`（FMA 的第三操作数）目前复用 VR port 1，所以 `io.vr_c_addr` 这个输入虽在 io bundle 里声明了（[SimpleBackend.scala:86](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L86)），却**没有被使用**——又一个「预留接口」。

VALU 写回按宽度分流到 port 0，[SimpleBackend.scala:215-240](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L215-L240)（节选关键行）：

```scala
when (isVALU) {
  rf.io.vx_w_en(0)   := ((dec.valu.regCls === W.VX) || isNarrowCvtOut(...)) && !isReduceToVR(...) && !isSetLut(...)
  rf.io.vx_w_addr(0) := io.vx_out_addr
  ...
  rf.io.ve_w_en(0)   := dec.valu.regCls === W.VE
  ...
  rf.io.vr_w_en(0)   := ((dec.valu.regCls === W.VR) || isWideCvtOut(...) || isReduceToVR(...)) && !isSetLut(...)
  ...
}
```

**本讲只需把握主干**：VALU 的三类输出分别走 VX/VE/VR 的 port 0，每个写口的使能由 `regCls` 把关（`W.VX/W.VE/W.VR`）。至于 `isNarrowCvtOut`、`isWideCvtOut`、`isReduceToVR`、`isSetLut` 这些「例外修正」——它们处理 CVT 与归约指令输出宽度与 `regCls` 不一致的情况——细节正是下一讲 u6-l2 的主题。`isVALU` 本身是 9 个 VALU 家族的 OR（[SimpleBackend.scala:204-212](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L204-L212)）。

最后，把 MMALU 与 VALU 的写回放在一起看，就得到了完整的写回分工：**VR port 1 永远是 MMALU 的专属直写通道，VR port 0 及 VX/VE port 0 归 VALU**。两者在 `family` 上互斥（一条指令要么是 MMA，要么是 VALU），不会同拍抢同一写口。

#### 4.3.4 代码实践

**目标**：跟踪一条 `vadd`（VALU）和一条 `mma`（MMALU）的输入来源与输出去向，验证你对连线的理解。

**操作步骤**：

1. 读 [NCoreBackendQuantSpec.scala 的 runExecuteVadd](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala#L92-L109)：它先 `extWrite` 把 a、b 写进 VX[0]、VX[1]，再 `poke` `vx_a_addr=0`、`vx_b_addr=1`、`vx_out_addr=2`，然后 `issue(vadd)`。
2. 对着连线表回答：a 走 VX port 1 → `in_a_vx`，b 走 VX port 2 → `in_b_vx`，结果 `out_vx` → VX 写 port 0，写到 `vx_out_addr=2`。最后 `ext_rd_addr=2` 通过 VX port 3 读回。
3. 再看 [runFullQuantSequence](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala#L121-L138) 里的 `mma(rd=2, rs1=0, rs2=1, keep=true)`：思考 in_a 来自 `mma_a_addr`、in_b 来自 `mma_b_addr`（经 port 3），结果 INT32 直写 VR `mma_out_addr`。

**需要观察的现象**：VALU 用例 1 拍就能写回（`issue` 默认 2 拍，含 1 拍输出寄存器延迟）；MMALU 用例只是译码合法性检查，真正完整跑通 MMA 累加在更上层测试里。

**预期结果**：你能口头复述「vadd 的两个源操作数分别走 VX 读 port 1/2，结果走 VX 写 port 0；mma 的两个源走 VX 读 port 0/3，结果走 VR 写 port 1」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 MMALU 的 `in_accum` 写成全 0，而 VALU 的输入却是「同时连三套宽度」？
**答案**：MMALU 的偏置路径在 backend 层尚未接通（预留），故先填 0；VALU 三套输入同时连是因为它用「通路复用」——每拍都把三种宽度的数据摆好，由 `ctrl.regCls` 内部选一路，省去外部多路选择。

**练习 2**：`valu.io.ctrl := dec.valu` 是「整块赋值」，相对「逐字段赋值」有什么好处？
**答案**：译码器已把所有 VALU 控制信号打包成 `NCoreVALUBundle`，整块 `:=` 一次性搬运，后端无需逐字段列举，增删字段时这行不用改，减少维护成本和出错面。

**练习 3**：MMALU 结果 `asUInt` 后写进 VR，为什么不会丢精度？
**答案**：`out` 是 `SInt(4N.W)`（N=8 时 32 位），VR lane 宽度也是 4N 位，二者等宽，`asUInt` 只改类型不改位模式，32 位累加结果原样落盘，故无截断。

---

## 5. 综合实践

把本讲的三块知识串成一张完整的后端连线图。

**任务**：对照 [SimpleBackend.scala 顶部的端口分配注释（19-26 行）与真实代码](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L19-L26)，画一张**从 `io.instr` 到 MMALU/VALU 输出再写回寄存器堆**的完整连线图。要求：

1. 画出 `InstrDecoder → (RF 地址, dec.valu, dec.mma_keep)` 三条控制/地址流。
2. 在寄存器堆上标出 **VX 读端口 0..3 各自驱动谁**：
   - port 0 → MMALU in_a
   - port 1 → VALU in_a_vx
   - port 2 → VALU in_b_vx
   - port 3 → Mux(外部活跃 ? ext_rd_addr : mma_b_addr)，既喂外部读 `ext_rd_data`，又喂 MMALU in_b
3. 标出 VE/VR 读端口 → VALU 各宽度输入。
4. 标出写回：MMALU out → **VR 写 port 1（无截断）**；VALU out_vx/ve/vr → **VX/VE/VR 写 port 0（按 regCls）**。
5. 在图上用红笔（或备注）圈出**三处注释与代码不符**：VX port 3 复用、MMALU in_b 实为 VX 而非 VR、VX 写 port 1 实际未用。

**进阶验证**（可选，需本地环境）：仿照 `NCoreBackendQuantSpec` 的写法，在 sbt 测试里构造一条 `vadd`：先 `extWrite` 两个 VX 向量，再 `issue(vadd)`，再从 `ext_rd_data` 读回，断言逐通道相等。能跑通就证明你画的读端口 1/2、写端口 0 的连线是对的。

> 若无法在本地跑仿真，请把上述连线图与断言关系写成文字说明，并标注「待本地验证」。

## 6. 本讲小结

- `NCoreBackend` 是总装车间，实例化 `InstrDecoder`、`MultiWidthRegisterBlock`、`MMALU`、`VALU` 四个子模块并用连线把它们对接成一条数据通路。
- 参数对齐（`K==mmalu.n`、`N==nbits`、`4N==accum_nbits`）靠**构造期把 K/N/N4 当实参传入**保证，比 `require` 更强；唯一的 `require` 只查 `L%4==0` 和正数。
- 寄存器堆端口是稀缺资源，VX 开 4 读 2 写、VE 开 2 读 1 写、VR 开 2 读 2 写，外加独立 ext 口；后端用一张端口分配表把 MMALU/VALU/外部各安其位。
- 读端口里有「时分复用」：VX port 3 在外部活动时服务外部读，否则服务 MMALU in_b。
- MMALU 的 INT32 结果**无截断直写 VR port 1**；VALU 三类输出分别写 VX/VE/VR 的 port 0，由 `regCls` 把关。
- 源码注释会落后于代码：本讲实打实指出三处偏差，养成「以代码为准」的阅读习惯。

## 7. 下一步学习建议

本讲只解决了**谁连到谁**（wiring）。下一讲 **u6-l2《指令分发与写回时序》** 会深入到：

- `isVALU` 如何判定 9 个 VALU 家族、与 MMA 分支如何互斥分发；
- `isNarrowCvtOut` / `isWideCvtOut` / `isReduceToVR` / `isSetLut` 这些写回守卫的具体语义——为什么 CVT 和归约指令需要特殊修正，否则会写错寄存器类；
- VALU 的 1 拍输出寄存器如何导致「译码保持 2 拍」的写回时序。

建议阅读顺序：先回看本讲 4.3.3 的 VALU 写回守卫代码作为铺垫，再进 u6-l2。如果想看端到端效果，可以提前翻一眼 [NCoreBackendQuantSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala) 里 `MMA → vcvt → vfma → vcvt` 的量化指令序列，那是 U7《量化与端到端流水线》的预习。
