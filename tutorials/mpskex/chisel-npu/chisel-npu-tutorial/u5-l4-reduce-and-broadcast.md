# 水平归约与广播

## 1. 本讲目标

本讲是 VALU 单元的第四篇，讲两个「打破逐通道对称性」的特殊操作：**水平归约**（horizontal reduction）与**广播**（broadcast）。学完后你应该能够：

- 理解 `vsum` / `vrmax` 这类水平归约如何用一棵跨 lane 的归约树，把 K 个通道压缩成一个标量。
- 解释为什么归约结果**永远是 VR 宽度**，并且被复制（广播）到全部 K 个 lane 的 `out_vr` 上。
- 掌握归约指令在 `funct7[1:0]` 里编码的是**输入宽度**而非输出宽度，并能说清这个设计带来的「输入/输出宽度错位」。
- 理解后端 `SimpleBackend` 中 `isReduceToVR` 修正函数存在的根本原因，以及没有它会写错哪类寄存器。
- 掌握 `vbcast` 的两种形态：`vbcast_reg`（取 lane 0）与 `vbcast_imm`（取立即数），以及它们如何把一个标量铺满 K 通道。

## 2. 前置知识

本讲默认你已经学完 [u5-l1 VALU 多宽度数据通路](u5-l1-valu-datapath.md)，已经建立了下面这些心智模型：

- **VX/VE/VR 是同一块物理存储的三种别名视图**：VX 是 K 个 N 位 lane（INT8），VE 拼相邻 2 行成 2N 位 lane（INT16），VR 拼 4 行成 4N 位 lane（INT32 / FP32）。
- **`ctrl.regCls` 是宽度总开关**：取值 0/1/2 = VX/VE/VR，它同时决定 lane 内的活动候选结果与后端打开的写回端口。
- **VALU 的输出结构**：`out_vx` / `out_ve` / `out_vr` 三套寄存输出并存，每拍只有一个端口携带有效数据，其余为 0；整体有 1 拍 `RegNext` 延迟。
- **「通路复用」**：所有 op 的候选结果在同一个 `for (lane <- 0 until K)` 循环里组合算出，再用 `MuxLookup` 按 `op` 选一个。

本讲要回答的新问题是：前面所有逐通道运算（`vadd`、`vmul`……）都是「K 进 K 出」，每个 lane 独立计算、互不相干。但神经网络里有两类需求打破了这个对称性：

1. **softmax 要把一行 K 个通道加起来**（求分母），再把和回填到每个通道做归一化——这是「K 进 1 出，再 1 进 K 出」。
2. **要把一个标量偏置（bias）或常数铺到 K 个通道**——这是「1 进 K 出」。

前者由**水平归约**解决，后者由**广播**解决。两者在硬件上都巧妙复用了 VALU 的逐通道输出结构。

> 术语提示：
> - **水平归约（horizontal reduction）**：沿 lane 方向（"水平"地横跨向量）做求和/求最大，把一个向量压成一个标量。与之相对的是 MMALU 那种沿矩阵维度的归约。
> - **广播（broadcast）**：把一个标量复制到向量的所有 lane。
> - **lane（通道）**：一个 SIMD 寄存器里的一个计算槽，K=8 表示 8 个 lane 并行。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开，再借两个 ISA 文件说明编码、一个后端文件说明写回修正。

| 文件 | 作用 |
| --- | --- |
| [src/main/scala/alu/vec/vec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala) | VALU 主体。归约树、广播变量、三套结果 mux、宽度门控输出都在这里。 |
| [src/main/scala/isa/micro_op/VALUMicroCode.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/VALUMicroCode.scala) | `VecOp` 枚举（`vsum`/`vrmax`/`vbcast_reg`/`vbcast_imm` 的内部操作码）与 `NCoreVALUBundle` 控制包。 |
| [src/main/scala/isa/instSetArch.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instSetArch.scala) | `OpFamily` 家族（`VALU_REDUCE`=0x12、`VALU_BCAST`=0x15）与各自的 `funct3` 子操作定义。 |
| [src/main/scala/isa/NpuAssembler.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala) | Scala 汇编器：`vsum`/`vrmax`/`vbcast`/`vbcastImm` 命名助手。 |
| [src/main/scala/backend/SimpleBackend.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala) | `NCoreBackend`：把译码结果分发给 VALU 并控制寄存器堆写回，含关键的 `isReduceToVR` 修正。 |
| [src/test/scala/alu/vec/VALUReduceSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUReduceSpec.scala) | 归约仿真测试：验证 `vsum`/`vrmax` 结果值与广播不变量。 |
| [src/test/scala/alu/vec/VALUCastSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUCastSpec.scala) | 广播仿真测试：验证 `vbcast_reg`/`vbcast_imm` 三宽度与广播不变量。 |

## 4. 核心概念与源码讲解

### 4.1 水平归约路径：从 K 通道压成一个标量

#### 4.1.1 概念说明

逐通道运算里，lane 之间是「老死不相往来」的：`out[i] = a[i] + b[i]`，第 i 个 lane 只看自己的 `a[i]`、`b[i]`。水平归约恰恰相反，它要**让所有 lane 一起参与同一个计算**：

- `vsum`：\( \text{out} = \sum_{i=0}^{K-1} a[i] \)
- `vrmax`：\( \text{out} = \max_{i=0}^{K-1} a[i] \)

结果是一个**标量**（一个数），不再是向量。这就是"水平"的含义——计算方向是横跨 lane 的，而不是沿 lane 内的比特。

为什么 NPU 需要它？最典型的场景是 softmax：算完一行 K 个通道的 exp 之后，需要把这 K 个值加起来当分母。再比如全局池化、reduce-sum 损失等。这些操作本质都是「把一个向量压成一个数」。

#### 4.1.2 核心流程

归约的实现是一棵**跨 lane 的归约树**，用 Chisel 的 `.reduce(_ + _)` / `.reduce { (a,b) => Mux(...) }` 描述。`.reduce` 会让综合器把 K 个 lane 组织成一棵二叉树（加法树或比较树），深度约 \( \lceil \log_2 K \rceil \)。

由于输入有 VX/VE/VR 三种可能的宽度，VALU **同时为三种宽度各算一棵归约树**，再用 `ctrl.regCls` 选出当前该用哪一棵：

```
            in_a_vx[K]  ──sign-extend──▶  sumVX / rmaxVX   (VX 归约树)
            in_a_ve[K]  ───────────────▶  sumVE / rmaxVE   (VE 归约树)
            in_a_vr[K]  ───────────────▶  sumVR / rmaxVR   (VR 归约树)
                                              │
                                   ctrl.regCls 选一棵
                                              │
                                         一个标量
                                              │
                                  复制到 out_vr 的全部 K 个 lane（见 4.2）
```

关键点：三棵树是**组合逻辑并行存在**的（即使本拍只用到一棵），这是 VALU「通路复用、控制极简」的一贯风格——组合冗余换控制简单。

#### 4.1.3 源码精读

归约树在 per-lane 循环**之外**预先算好，作为 lane 无关的标量：

[vec.scala:175-188](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L175-L188) —— 三宽度的归约树，这段代码定义了 sum/rmax 各三种。

逐行说明：

- `aVXsigned = VecInit(io.in_a_vx.map(_.asSInt))`：VX 输入是 `UInt(N.W)`，先统一转成有符号 `SInt`，因为求和/比较要按有符号语义（数据类型 S8C4）。
- `sumVX = aVXsigned.map(_.asTypeOf(SInt(N4.W))).reduce(_ + _)`：每个 lane 先符号扩展到 N4=32 位再相加。扩展到位宽后再 reduce 是为了求和过程绝不溢出。
- `rmaxVX = aVXsigned.reduce { (a,b) => Mux(a > b, a, b) }`：比较树求最大值，最后 `.asTypeOf(SInt(N4.W))` 提升到 32 位。
- `sumVE` / `sumVR` 的写法多了 `(N4-1, 0).asSInt`：每一步加法后截断到 32 位。对 K=8 的 16/32 位输入，求和远不会触及 32 位上界，所以截断是无损的。

注意一个诚实的局限：`vrmin`（以及 `vrand`/`vror`/`vrxor`）在枚举里有定义、能正确译码，但**归约计算尚未实现**——在 VX 结果 mux 里它被直接接成 `0.U`：

[vec.scala:329](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L329) —— `vrmin.asUInt -> 0.U, // TODO: add vrmin reduction`。本讲只讲已实现且被测试覆盖的 `vsum` 与 `vrmax`。

#### 4.1.4 代码实践

**目标**：阅读归约测试，理解「K 进 1 出」的期望值如何计算。

**操作步骤**：

1. 打开 [VALUReduceSpec.scala:32-48](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUReduceSpec.scala#L32-L48)（`runVsum`）。
2. 关注期望值的计算方式：`val expected = a.map(_.toLong).sum`，然后用 `(expected & 0xFFFFFFFFL).U` 与 `out_vr(i)` 比较。
3. 注意它对**每一个** lane `i` 都 `expect` 同一个 `expected`——这正是「广播」的断言。

**需要观察的现象 / 预期结果**：

- `vsum` 的结果出现在 `out_vr`（而非 `out_vx`），即使测试 `poke` 的是 `in_a_vx`、`regCls=0`(VX)。这印证了「归约结果永远是 VR 宽度」。
- 期望值用 `toLong` 求和再 `& 0xFFFFFFFFL`，是因为结果按 32 位补码解释（负数在 32 位下是大正数）。
- 待本地验证：在容器里执行 `bash tool/test-specific-spec.sh alu.vec.VALUReduceSpec`（脚本用法见 u9-l2），观察测试通过。

#### 4.1.5 小练习与答案

**练习 1**：K=8、N=8，输入 8 个 INT8 全是 `-128`，`vsum.vx` 的 32 位结果是多少？用 `& 0xFFFFFFFFL` 表示。

**参考答案**：和为 \( 8 \times (-128) = -1024 \)。`-1024` 的 32 位补码为 `0xFFFFFC00`，即 `0xFFFFFC00L`。它远在 32 位范围内，所以归约本身没有溢出问题。

**练习 2**：为什么 `sumVX` 要先把每个 lane 符号扩展到 N4=32 位再 `.reduce(_ + _)`，而不是直接在 N 位上 reduce？

**参考答案**：两个 N 位有符号数相加最多需要 N+1 位；K 个数累加最多需要 \( N + \lceil\log_2 K\rceil \) 位。若在 N 位上逐步累加，中间结果会溢出截断，得到错误的环绕值。先扩展到 32 位再求和，保证整棵加法树自始至终不溢出，结果精确。

---

### 4.2 out_vr 广播：一个标量如何铺满 K 个 lane

#### 4.2.1 概念说明

4.1 算出的归约结果是一个标量，但 VALU 的输出是 `Vec(K, ...)`——每个 lane 一个值。这两者怎么对接？

答案是 VALU 用了一个极其轻巧的技巧：**把同一个标量赋给 per-lane 循环里的每一个 `selVR(lane)`**。因为标量不依赖 lane，循环跑 K 次，`selVR(0)`、`selVR(1)`、…、`selVR(K-1)` 就全是同一个值。于是 `out_vr` 自然就成了「K 份相同的标量」——这就是**广播**，零额外硬件，只是数据的重复绑定。

这就是本讲第二个最小模块「out_vr 广播」。它和 4.3 的 `vbcast` 都是广播，但来源不同：

| 广播来源 | 机制 | 指令 |
| --- | --- | --- |
| 归约标量 | 跨 lane 归约树算出的一个数，重复绑定到每 lane | `vsum` / `vrmax` |
| lane 0 的值 | 只读输入的 lane 0，重复绑定到每 lane | `vbcast_reg` |
| 立即数 | `ctrl.imm` 符号扩展后，重复绑定到每 lane | `vbcast_imm` |

#### 4.2.2 核心流程

归约标量进入输出的路径有两个关键决策点，缺一不可：

1. **VR 结果 mux 按 `regCls` 选归约树**：因为 `regCls` 在归约指令里编码的是**输入宽度**，它既要选「对哪一种宽度的输入做归约」，又顺带决定了把哪棵树的结果送出去。
2. **宽度门控强制走 `out_vr`**：即使 `regCls=VX`（vsum.vx），输出也不走 `out_vx`，而强制走 `out_vr`。这是靠在 `rawVR` 的门控条件里**无条件**列出 `vsum`/`vrmax`/`vrmin` 实现的。

```
   regCls=VX ┐
             ├─▶ VR mux 选 sumVX ─▶ selVR(lane) [每 lane 同值]
   regCls=VE ┘                       │
   regCls=VR ───▶ VR mux 选 sumVR ───┘
                                     │
              宽度门控：op===vsum 强制 rawVR(lane):=selVR
                                     │
                        RegNext ─▶ out_vr (K 份相同标量)
```

#### 4.2.3 源码精读

**决策点 1——VR 结果 mux 按 `regCls` 选归约树**：

[vec.scala:386-392](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L386-L392) —— `vsum`/`vrmax` 在 VR mux 里的 `Mux(wid ...)` 选择。

```scala
VecOp.vsum.asUInt  -> Mux(wid === 1.U, sumVE(N4-1, 0).asUInt,
                        Mux(wid === 2.U, sumVR(N4-1, 0).asUInt,
                          sumVX.asTypeOf(SInt(N4.W)).asUInt)),
```

`wid` 就是 `ctrl.regCls`（见 [vec.scala:149](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L149) `val wid = io.ctrl.regCls`）。`wid=0`(VX) 选 `sumVX`，`wid=1`(VE) 选 `sumVE`，`wid=2`(VR) 选 `sumVR`。整段位于 `for (lane <- 0 until K)` 循环内，而 `sumVX/sumVE/sumVR` 是循环外算好的 lane 无关标量——所以每个 lane 拿到同一个值，广播就此完成。

**决策点 2——宽度门控强制走 `out_vr`**：

[vec.scala:433-450](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L433-L450) —— `rawVR` 的赋值门控。

关键行是 `wid === 2.U || op === VecOp.vsum || op === VecOp.vrmax || op === VecOp.vrmin || ...`。也就是说，哪怕 `regCls` 不是 VR，只要 `op` 是归约指令，`rawVR(lane)` 就被赋成 `selVR`，而 `rawVX(lane)` 此时（`wid !== 0` 或被归约特例排除）为 0。最终 [vec.scala:454-456](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L454-L456) 的 `io.out_vr := RegNext(rawVR)` 把这个广播值寄存一拍输出。

> 设计要点：归约是一个「越级特例」——输入宽度（由 `regCls` 决定）与输出宽度（恒为 VR）不一致。VALU 用两处特判（mux 里按 `wid` 选树、门控里按 `op` 强制 VR）吸收了这个不一致。这种不一致会一路传导到后端写回，就是 4.4 要解决的 `isReduceToVR` 问题。

#### 4.2.4 代码实践

**目标**：在仿真里亲眼看到「一个标量出现在全部 K 个 lane 的 `out_vr` 上」。

**操作步骤**：参考 [VALUReduceSpec.scala:43-46](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUReduceSpec.scala#L43-L46) 的「广播不变量」断言写法：

```scala
val lane0 = dut.io.out_vr(0).peek().litValue
for (i <- 1 until K) {
  assert(lane0 == dut.io.out_vr(i).peek().litValue, s"vsum broadcast invariant lane $i")
}
```

**需要观察的现象 / 预期结果**：`out_vr` 的 K 个 lane 的 `litValue` 两两相等。若你想加一条更激进的检查，可在 `vsum` 后断言 `out_vx` 的所有 lane 都是 0（因为归约不走 VX 输出端口）。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：假如把 [vec.scala:435](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L435) 里 `op === VecOp.vsum` 这一项删掉，`vsum.vx`（`regCls=0`）的输出会变成什么？

**参考答案**：`rawVR` 的门控只剩 `wid === 2.U`，而 `vsum.vx` 的 `wid=0`，于是 `rawVR(lane)` 落到默认的 `0.U`；同时 `rawVX(lane)` 因 `wid===0.U` 会取 `selVX`（VX mux 里 vsum 那一支是 `satOrTrunc(sumVX, sat, N)`，截断到 8 位）。结果：归约和会错误地出现在 `out_vx` 且被截断成 8 位，`out_vr` 全 0。这正好反向说明了门控特例的必要性。

**练习 2**：归约的「广播」与 `vbcast` 的「广播」在硬件实现上有什么共同点？

**参考答案**：两者都把一个 lane 无关的值（归约标量 / lane0 / imm）绑定到 per-lane 循环里每一个 `selX(lane)`，从而让 `out` 的全部 K 个 lane 取同一个值。区别只在那个标量从哪来：归约是算出来的，`vbcast` 是从 lane 0 或立即数读来的。

---

### 4.3 广播路径：vbcast 把一个标量铺满 K 通道

#### 4.3.1 概念说明

`vbcast` 解决的是反向问题：手上只有一个标量（一个 bias、一个常数、或某个通道的值），想让它出现在 K 个 lane 上，供后续逐通道运算使用。它有两种取标量的方式：

- `vbcast_reg`：取源寄存器 `rs1` 的 **lane 0**，铺到 `rd` 的全部 lane。
- `vbcast_imm`：取指令字里的 12 位**立即数**，符号扩展后铺到全部 lane。

和归约不同，`vbcast` 是**宽度一致**的——`regCls` 既决定输入宽度也决定输出宽度：`vbcast.vx` 读 VX 写 VX，`vbcast.vr` 读 VR 写 VR。所以它不需要 `isReduceToVR` 那种修正。

#### 4.3.2 核心流程

```
vbcast_reg:  in_a(0) ──────────────▶ a0VX/a0VE/a0VR ─▶ selX(lane)=a0  (每 lane 同值)
vbcast_imm:  ctrl.imm (SInt 12位) ─▶ sext 到目标宽度 ─▶ selX(lane)=immV (每 lane 同值)
                                                      │
                                  宽度门控：按 regCls 选 out_vx/ve/vr
```

注意一个 ISA 层的细节：`vbcast_imm` 是 **I 型**指令（立即数占据 `[31:20]`，正好吃掉了 R 型 `funct7` 的位置），所以它**没有 `funct7` 位段来编码宽度**。译码器对此做了硬编码兜底：`vbcast_imm` 一律按 VX 处理。而 `vbcast_reg` 是 R 型，有 `funct7[1:0]` 宽度位，可以正常选 VX/VE/VR。

#### 4.3.3 源码精读

**广播变量定义**（per-lane 循环内，但只读 lane 0）：

[vec.scala:299-304](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L299-L304) —— `a0VX = io.in_a_vx(0)` 等取 lane 0，`immV = io.ctrl.imm` 取立即数。

**VX 结果 mux 里的广播分支**：

[vec.scala:339-340](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L339-L340) —— `vbcast_reg -> a0VX`、`vbcast_imm -> immV(N-1, 0).asUInt`。

VE、VR mux 里也有对应分支（[vec.scala:369-370](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L369-L370)、[vec.scala:420-421](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L420-L421)），分别取 `a0VE`/`immV` 与 `a0VR`/`immV`。由于这些都绑在 per-lane 循环里的 `selVX/VE/VR(lane)`，且源值不依赖 `lane`，每个 lane 拿到同一个值——广播完成。

**立即数的符号扩展**：`immV` 是 `SInt(12.W)`。VX 分支取 `immV(N-1, 0)`（低 8 位），对负立即数等价于取其 8 位补码；VE/VR 分支用 `immV.asTypeOf(SInt(N2.W))` / `SInt(N4.W)` 做符号扩展。

**译码器对 `vbcast_imm` 宽度的硬编码**：

[instrDecoder.scala:217-219](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L217-L219) —— `when (family === OpFamily.VALU_BCAST && f3 === Funct3Bcast.IMM) { width := 0.U // VX }`。

**汇编器接口**：

[NpuAssembler.scala:181-186](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L181-L186) —— `vbcast`（R 型）与 `vbcastImm`（I 型）。

> 一个 API 小陷阱：`vbcastImm(rd, imm, width = VX)` 虽然带 `width` 参数，但函数体 `encI(0x15, 1, rd, 0, imm)` **完全没用 `width`**——因为 I 型指令没有 `funct7` 来放宽度位。所以 `vbcastImm` 实际只能产生 VX 广播；传 `width=VR` 不会有效果。这是「I 型牺牲 funct7 换立即数」的直接后果。

#### 4.3.4 代码实践

**目标**：用汇编器构造一条 `vbcast_imm`，验证负数立即数被正确符号扩展到 8 位。

**操作步骤**：参考 [VALUCastSpec.scala:97-107](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUCastSpec.scala#L97-L107)（`runBcastImmNeg`）。

1. 用 `import isa.NpuAssembler._` 调 `vbcastImm(rd=0, imm=-5)`，得到 32 位指令字。
2. 把它 poke 进 VALU（或经译码器），`clock.step()` 一拍。
3. 读 `out_vx` 的任一 lane。

**需要观察的现象 / 预期结果**：`-5` 在 12 位补码下是 `0xFFB`；取低 8 位得 `0xFB = 251`，正是 `(-5) & 0xFF`。所以 K 个 lane 的 `out_vx` 都应为 251。这同时验证了「符号扩展」与「广播」两件事。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`vbcast_reg` 读取的是 `rs1` 的 lane 0。如果软件想广播 lane 3 的值，该怎么做？

**参考答案**：硬件只固定读 lane 0，没有「广播任意 lane」的编码。软件需先用一条逐通道搬运/置换指令把目标 lane 的值搬到 lane 0（或直接把数据组织成 lane 0），再发 `vbcast_reg`。这是一种「硬件保持简单、把灵活性留给软件」的常见取舍。

**练习 2**：为什么 `vbcast_imm` 不像 `vbcast_reg` 那样支持 VE/VR 宽度？

**参考答案**：`vbcast_imm` 是 I 型，12 位立即数占用了 `[31:20]`，恰好是 R 型 `funct7` 的位置，没有比特留给宽度编码。译码器因此把 `vbcast_imm` 硬编码为 VX。若要广播一个 VE/VR 宽度的常数，需先 `vbcast_imm` 到一个 VX 寄存器，再用 CVT 指令扩展宽度。

---

### 4.4 后端修正 isReduceToVR：输入宽度与输出宽度的错位

#### 4.4.1 概念说明

归约指令有一个贯穿 ISA→VALU→后端的「错位」：`funct7[1:0]`（即 `regCls`）编码的是**输入宽度**，但输出**永远是 VR 宽度**。这在 VALU 内部已被 4.2 的两处特判消化。但后端 `SimpleBackend` 的写回逻辑是按 `regCls` 来决定打开哪类寄存器堆写端口的——如果照搬，`vsum.vx`（`regCls=VX`）会被误当成「写 VX」，把归约结果写进 VX 寄存器、覆盖错误的地址。

`isReduceToVR` 就是后端为修正这个错位而设的辅助函数：它识别出三类归约 op，**抑制**它们的 VX/VE 写、**强制**打开 VR 写。

#### 4.4.2 核心流程

后端对每类寄存器的写使能守卫（简化）：

```
vx_w_en(0) := (regCls===VX 或 是窄CVT) 且 非归约(reduce) 且 非setlut
ve_w_en(0) := regCls===VE
vr_w_en(0) := (regCls===VR 或 是宽CVT 或 是归约(reduce)) 且 非setlut
```

注意 `vx_w_en` 里多了 `!isReduceToVR`，`vr_w_en` 里多了 `|| isReduceToVR`——一正一反，把归约从「按 regCls 写」改写成「强制写 VR」。

#### 4.4.3 源码精读

**VX 写回守卫（含抑制归约）**：

[SimpleBackend.scala:222-226](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L222-L226)：

```scala
rf.io.vx_w_en(0) := ((dec.valu.regCls === W.VX) || isNarrowCvtOut(dec.valu.op)) &&
                    !isReduceToVR(dec.valu.op) &&
                    !isSetLut(dec.valu.op)
```

`vsum.vx` 的 `regCls===VX` 本会让 `vx_w_en(0)` 为真，`!isReduceToVR` 把它压回 false。

**VR 写回守卫（含强制归约）**：

[SimpleBackend.scala:235-239](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L235-L239)：

```scala
rf.io.vr_w_en(0) := ((dec.valu.regCls === W.VR) || isWideCvtOut(dec.valu.op) ||
                     isReduceToVR(dec.valu.op)) &&
                    !isSetLut(dec.valu.op)
```

`vsum.vx` 的 `regCls` 不是 VR，但 `isReduceToVR` 让 `vr_w_en(0)` 为真，结果正确写入 VR 端口（地址来自 `io.vr_out_addr`）。

**`isReduceToVR` 定义与根因注释**：

[SimpleBackend.scala:270-285](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L270-L285) —— 注释精确点明根因：「ISA 用*输入*寄存器类编码归约（如 `vsum.vx` 用 regCls=VX 选 VX 归约路径），但输出恒为 VR 宽度。后端的 `regCls===VR` 守卫因此在 VX/VE 输入时漏掉这些 op。」

#### 4.4.4 代码实践

**目标**：在脑中（或临时改后端验证）推演「没有 `isReduceToVR` 会写错哪里」。

**操作步骤**：

1. 假设把 [SimpleBackend.scala:222-226](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L222-L226) 的 `!isReduceToVR(...)` 删掉，同时把 [235-239](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L235-L239) 的 `|| isReduceToVR(...)` 删掉。
2. 发一条 `vsum.vx rd=vr0, rs1=vx1`（`regCls=VX`）。
3. 追踪两个写端口：`vx_w_en(0)` 会因 `regCls===VX` 而为真，把 `out_vx`（归约时为 0 或 8 位截断值）写到 `vx_out_addr`；`vr_w_en(0)` 会因 `regCls≠VR` 而为假，归约结果根本不写 VR。

**需要观察的现象 / 预期结果（错误行为）**：

- VR 目标寄存器 `vr0` **不会被更新**（VR 写关闭），软件读 `vr0` 拿到的是旧值。
- 某个 VX 寄存器（`vx_out_addr` 指向的）会被**误写**成归约和的低 8 位（甚至 0，因为归约时 `out_vx` 多为 0），破坏原本存放在那里的 INT8 数据。
- 这就是为什么 `isReduceToVR` 不可或缺：它把「输入宽度」与「输出宽度」在写回这一环重新对齐。**本步骤为源码阅读型推演，勿真正改后端源码**（本讲禁止改源码）；如需验证可在本地 worktree 实验。

#### 4.4.5 小练习与答案

**练习 1**：`vsum.ve`（对 VE 输入求和，`regCls=VE`）在没有 `isReduceToVR` 时会被写到哪里？

**参考答案**：`ve_w_en(0)` 因 `regCls===VE` 为真，会把归约结果（实际 `out_ve` 在归约时为 0）误写到 `ve_out_addr` 指向的 VE 寄存器；同时 `vr_w_en(0)` 为假，VR 拿不到正确结果。所以 VE 输入的归约同样需要 `isReduceToVR` 修正——它对所有三档输入宽度都生效。

**练习 2**：为什么 `vbcast` 不需要类似的 `isBcastToVR` 修正？

**参考答案**：因为 `vbcast` 的输入宽度与输出宽度**一致**：`regCls` 既选输入也选输出，写回端口的 `regCls===VX/VE/VR` 守卫天然正确。只有归约（和部分 CVT）存在输入/输出宽度错位，才需要特判。

---

## 5. 综合实践

把本讲三件事（归约、广播、写回修正）串起来，设计一个 softmax 分母计算的小序列（**源码阅读 + 伪代码型实践**，无需硬件）。

**背景**：softmax 一行需要 \( \text{denom} = \sum_i e^{x_i} \)，然后每个通道做 \( e^{x_i} / \text{denom} \)。这里要用到归约（求 denom）和广播（把 denom 铺回每个通道做除法/缩放）。

**任务**：

1. 用 `NpuAssembler` 写出求 denom 的指令：参考 [VALUReduceSpec](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUReduceSpec.scala) 的写法，先用一条 `vlut`（u5-l2 学过的 exp 激活）把 `vx0` 的 K 个 INT8 映射成 exp 值，再用一条 `vsum` 把它们归约。
2. 回答：这条 `vsum` 的 `rd` 应该指向哪类寄存器？`width` 参数（即 `regCls`）应填 `VX`/`VE`/`VR` 哪一个？为什么？
3. 接着用一条 `vbcast_reg` 把归约结果（已在某个 VR 寄存器的全部 lane 上）广播给后续除法用。说明这条 `vbcast` 应该用 `width=VR`，并指出它**不需要** `isReduceToVR` 修正的原因。
4. 最后追踪整条链路在 `SimpleBackend` 里的写回：`vlut` 写 VX、`vsum` 经 `isReduceToVR` 强制写 VR、`vbcast_reg.vr` 写 VR。画出每步结果落在哪类寄存器（VX/VE/VR）的表格。

**参考答案要点**：

- `vsum` 的 `rd` 指向 VR 寄存器（如 `vr0`），`width=VX`（因为是对 `vlut` 的 VX 输出求和）。这正是「输出 VR、输入宽度编码在 funct7」的典型用法。
- 归约结果经 [vec.scala:433-450](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L433-L450) 强制走 `out_vr`，再经后端 `isReduceToVR` 强制开 VR 写端口，落进 `vr0`。
- 后续 `vbcast_reg.vr` 读 `vr0` 的 lane 0、广播到另一个 VR 寄存器；它是宽度一致的广播，按 `regCls===VR` 正常写回，无需特判。
- 写回表：

| 步骤 | 指令 | 输入类 | 输出类 | 后端写回守卫 |
| --- | --- | --- | --- | --- |
| 1 | `vlut` | VX | VX | `regCls===VX` |
| 2 | `vsum.vx` | VX | **VR** | `isReduceToVR` 强制 VR |
| 3 | `vbcast_reg.vr` | VR | VR | `regCls===VR` |

（说明：完整的 softmax 还涉及 `vfma`/`vcvt` 做定点缩放与倒数，u7-l2 会专门讲整条量化注意力流水线。）

## 6. 本讲小结

- **水平归约**（`vsum`/`vrmax`）用一棵跨 lane 的归约树，把 K 个通道压成一个标量；为 VX/VE/VR 三种输入宽度各算一棵树，再用 `regCls` 选一棵。
- 归约结果**永远是 VR 宽度**，并被**广播**到 `out_vr` 的全部 K 个 lane——做法是把同一个 lane 无关标量绑定到 per-lane 循环的每个 `selVR(lane)`，零额外硬件。
- 归约指令在 `funct7[1:0]` 编码的是**输入宽度**而非输出宽度，造成输入/输出宽度错位；VALU 用「mux 按 `regCls` 选树 + 门控按 `op` 强制 `out_vr`」两处特判消化它。
- 后端 `SimpleBackend` 用 `isReduceToVR` 在写回环再次修正：抑制归约的 VX/VE 写、强制打开 VR 写，否则会把归约结果误写进 VX 寄存器、VR 目标拿不到值。
- `vbcast`（`vbcast_reg` 取 lane 0、`vbcast_imm` 取立即数）用同样的「标量绑定到每 lane」技巧实现广播，但它是**宽度一致**的，按 `regCls` 正常写回，无需特判。
- `vbcast_imm` 是 I 型，立即数占据了 `funct7` 位置，译码器把它硬编码为 VX；`vrmin` 等少数归约 op 虽能译码但**计算尚未实现**（接 0），属已知局限。

## 7. 下一步学习建议

- 本讲和 u5-l2（可编程 LUT）、u5-l3（浮点）共同构成 VALU 的全部特殊通路。建议接着读 [u5-l3 FP32/BF16/BF8 浮点运算](u5-l3-floating-point.md)，看 `vfma`（融合乘加）如何与归约、广播组合出完整的标量-向量运算。
- 归约与广播的真正用武之地在端到端流水线：直接跳到 [u7-l2 GEMM + Softmax 端到端教程](u7-l2-gemm-softmax-tutorial.md)，看 `vsum`（求 softmax 分母）、`vlut`（exp 激活）、`vbcast`（铺分母做归一化）如何拼成一行 transformer 注意力的完整计算。
- 想理解后端如何把本讲的归约/广播与 MMALU、CVT 一起调度的读者，可先读 [u6-l2 指令分发与写回时序](u6-l2-dispatch-and-writeback.md)，那里系统讲了 `isVALU` 分支与各类写回守卫（含本讲的 `isReduceToVR`、`isSetLut`、`isNarrowCvtOut` 等）的协作。
