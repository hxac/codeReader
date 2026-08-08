# 可编程查找表 LUT 与激活函数

## 1. 本讲目标

本讲聚焦 VALU 九大家族中一个最「特殊」的家族——`VALU_LUT`(opcode `0x13`)可编程查找表。学完本讲后,你应当能够:

- 说出为什么 NPU 用查找表(LUT)而不是多项式计算来实现 `exp`/`tanh`/`erf`/`recip` 等非线性激活函数。
- 读懂 `Qfmt` 对象用 SQ1.6 / UQ0.8 定点格式预生成四张参考表的算法,并明白它只是 Scala 工具、**不会**被综合成硬件 ROM。
- 理解 VALU 内部那两块 256 项、每项 N 位的 bank 寄存器(`lutBankA`/`lutBankB`)如何支持「双缓冲」:一块服务当前 `vlut`,另一块用 `vsetlut` 预装下一张表。
- 跟踪 `vsetlut`(分段写表)与 `vlut`(逐通道查表)两条数据通路,理解 bank 选择位如何复用 `ctrl.round[0]`。
- 解释 `vsetlut` 为什么是**唯一**一条不写回寄存器堆的 VALU 指令,以及后端为何必须显式抑制它的所有 RF 写端口。

---

## 2. 前置知识

本讲建立在 [u5-l1 VALU 多宽度数据通路](u5-l1-valu-datapath.md) 之上。在继续之前,请确认你已经掌握:

- **VALU 的整体结构**:K 通道、三宽度(VX/VE/VR)、按 `op` 用 `MuxLookup` 在每个 lane 内选结果、输出经 `RegNext` 延迟一拍。
- **`ctrl.regCls` 是宽度总开关**:0=VX(N 位)、1=VE(2N 位)、2=VR(4N 位),它同时决定 lane 内有效候选与后端打开的写回端口。
- **全局参数**:N(bits)=8(基础通道位宽)、K=8(测试态每寄存器通道数)、L=32(VX 寄存器数);VE/VR 是同一物理存储的别名视图。
- **「越级特例」概念**:u5-l1 提到 CVT 与水平归约(vsum 等)输出宽度可能与 `regCls` 不符,需在宽度门控与写回守卫里特殊处理。本讲的 `vsetlut`/`vlut` 是另一类特例——它们读写 VALU 内部隐藏状态,而非(或不仅仅)寄存器堆。

此外需要一点**定点数(Q-format)**的直觉:

- 定点数就是「把小数点位置固定死的整数」。例如 UQ0.8 表示「0 位整数 + 8 位小数」,一个 8 位无符号数 \(v\) 代表的真实值是 \(v/2^8 = v/256\);SQ1.6 表示「1 位符号 + 6 位小数」,一个 8 位有符号数 \(s\) 代表的真实值是 \(s/2^6 = s/64\)。
- 用定点的好处是:查表只需整数下标、无需浮点硬件;坏处是动态范围和精度都有限。对激活函数这种「输入范围已知、输出单调」的场景,定点 + LUF 完全够用。

> 术语提示:**激活函数(activation function)**是神经网络里夹在矩阵乘之后的非线性函数,如 `exp`(softmax 用)、`tanh`、`erf`(GELU 用)。NPU 之所以要支持它们,是因为没有非线性的网络等价于一层线性变换。

---

## 3. 本讲源码地图

本讲涉及的关键文件及其作用:

| 文件 | 作用 |
|:---|:---|
| [src/main/scala/alu/vec/vec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala) | VALU 主体。包含 `Qfmt` 定点参考表对象、两块 LUT bank 寄存器、`vsetlut` 写表逻辑与 `vlut` 查表逻辑。 |
| [src/main/scala/isa/micro_op/VALUMicroCode.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/VALUMicroCode.scala) | 定义 `VecOp` 枚举(`vlut`/`vsetlut` 两个内部操作码)与 `NCoreVALUBundle` 控制包(含被复用的 `round` 与 `imm` 字段)。 |
| [src/main/scala/isa/instSetArch.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instSetArch.scala) | 定义 `VALU_LUT` 家族(opcode `0x13`)与 `Funct3Lut` 子操作编码。 |
| [src/main/scala/isa/instrDecoder.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala) | 组合译码器:把 `funct3` 翻译成 `vlut`/`vsetlut`,把 bank 选择位送到 `round[0]`,并强制 `vsetlut` 的宽度为 VR。 |
| [src/main/scala/isa/NpuAssembler.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala) | Scala 汇编器:`vlut`/`vsetlut` 两个命名助手,封装 R/I 型指令字的拼接。 |
| [src/main/scala/backend/SimpleBackend.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala) | 后端:包含 `isSetLut` 守卫,负责抑制 `vsetlut` 对寄存器堆的所有写端口。 |
| [src/test/scala/alu/vec/VALUProgrammableLutSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUProgrammableLutSpec.scala) | LUT 单元测试:`loadBank` 分段装表、`sweepLut` 遍历 256 项验证查表 bit-exact。 |
| [src/test/scala/alu/vec/VALUActivationSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUActivationSpec.scala) | 激活函数组合测试:用 `vlut` 拼出 softmax、GELU。 |
| [docs/implementations/VectorALU.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/VectorALU.md) | VALU 设计文档:`VALU_LUT` 指令参考、segment 容量表、softmax/GELU 流程图。 |

---

## 4. 核心概念与源码讲解

本讲按数据流方向拆成四个最小模块:先看「表数据从哪来」(`Qfmt`),再看「表存在哪、为什么是两块」(bank 寄存器),然后看「怎么把表装进去」(`vsetlut`),最后看「怎么查表、查完怎么处理」(`vlut` + 写回抑制)。

### 4.1 Qfmt 定点参考表对象

#### 4.1.1 概念说明

神经网络激活函数(`exp`、`tanh`、`erf`、`recip`)都是**非线性**函数。在硬件上实现非线性有两条路:

1. **多项式/迭代逼近**:用乘法器 + 加法器算泰勒展开或 Cordic。优点是表小,缺点是要占用 VALU 的算术通路、多拍延迟、且精度难控。
2. **查找表(LUT)**:把「输入 → 输出」的映射预先算好存成一张表,运行时按输入当地址直接读出结果。优点是**单拍、纯组合读、零算力开销**,缺点是要存表。

chisel-npu 选择了 LUT 方案,因为激活函数的输入(量化后的 INT8)只有 256 种取值,一张 256 项的表就能覆盖整个定义域,查表只需一个时钟周期。这正好把昂贵的乘法器留给矩阵乘和 `vfma`。

但表里的数值怎么来?NPU 不能在硬件里跑 `math.exp`。答案是:**在编译期/测试期用 Scala 算好,再通过 `vsetlut` 灌进硬件**。这个「Scala 侧算表」的工具就是 `Qfmt` 对象。关键认知——

> `Qfmt` 是**纯 Scala** 工具,它生成的表只是数据,**不会被综合成硬件 ROM**。硬件里只有两块空的、可写的 bank 寄存器,表内容必须运行时用 `vsetlut` 装入。

#### 4.1.2 核心流程

`Qfmt` 用两种定点格式表示表的内容:

- **输入侧 SQ1.6**:1 位符号 + 6 位小数,存于 8 位有符号数。真实值 \(= s/64\),范围约 \([-2.0,\ 1.984]\)。下标 `raw`(0..255)先按二补码解释成有符号数 \(s\),再除以 64 得到真实输入 \(x\)。
- **exp 输出侧 UQ0.8**:0 位整数 + 8 位小数,存于 8 位无符号数。真实值 \(= u/256\),范围 \([0,\ 0.996]\)。因为 \(e^x\) 恒正,UQ0.8 正好够用。

四张表的输入/输出格式:

| 表 | 输入格式 | 输出格式 | 用途 | 特殊处理 |
|:---|:---:|:---:|:---|:---|
| `lutExp` | SQ1.6 | UQ0.8 | softmax 的 \(e^x\) | 输出按 `SInt(8.W)` 二补码存储;值封顶在 \(255/256\) |
| `lutRecip` | SQ1.6 | SQ1.6 | \(1/x\)(softmax 归一化) | \(x=0\) 时返回哨兵值 127(避免除零) |
| `lutTanh` | SQ1.6 | SQ1.6 | tanh 激活 | 单调非减 |
| `lutErf` | SQ1.6 | SQ1.6 | GELU 的 erf | 用 Abramowitz-Stegun 多项式逼近 |

生成一张表的伪代码是:

```
对 raw = 0..255:
    x = 把 raw 解释成 8 位有符号数 / 64        # SQ1.6 解码
    y = 数学函数(x)                            # exp / 1/x / tanh / erf
    entry = 把 y 量化成 8 位定点               # SQ1.6 或 UQ0.8 编码
    table[raw] = entry
```

#### 4.1.3 源码精读

`Qfmt` 的核心常量与格式转换函数([vec.scala:L37-L56](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L37-L56)):

```scala
object Qfmt {
  val FRAC_BITS = 6
  val IN_SCALE  = 1 << FRAC_BITS  // 64
  val EXP_SCALE = 256              // UQ0.8 for vexp output
```

- `IN_SCALE = 64` 是 SQ1.6 的小数缩放;\(x = s / 64\)。
- `EXP_SCALE = 256` 是 UQ0.8 的小数缩放;\(u = y \times 256\)。

`sq16ToDouble` 把 8 位下标解释成有符号 SQ1.6 真实值:

```scala
def sq16ToDouble(raw: Int): Double = {
  val signed = if (raw >= 128) raw - 256 else raw   // 8 位二补码 → 有符号
  signed.toDouble / IN_SCALE                        // /64
}
```

例如 `raw = 0` → \(x = 0\);`raw = 255` → \(signed = -1\),\(x = -1/64 \approx -0.016\)。

`lutExp` 的生成([vec.scala:L58-L64](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L58-L64)),注意它把输出存成「有符号 8 位二补码」:

```scala
// vexp: SQ1.6 → UQ0.8 stored as SInt(8.W) two's-complement
val lutExp: Seq[Int] = Seq.tabulate(256) { raw =>
  val x = sq16ToDouble(raw)
  val e = math.exp(x)
  val u = doubleToUq08(math.min(e, 255.0 / EXP_SCALE))  // 封顶,避免溢出 255
  if (u > 127) u - 256 else u                            // 128..255 存成负数二补码
}
```

最后这行 `if (u > 127) u - 256 else u` 是关键:UQ0.8 的值 128..255 在 8 位容器里与负数二补码共享同一比特模式(例如 255 ↔ −1)。文档专门提醒([VectorALU.md:L457](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/VectorALU.md#L457)):`out_vx` 是 `UInt(N.W)`,所以 0..255 是无符号值,255(\(\approx\) exp(0)≈1.0)完全可表示——只有当调用方把它当 `SInt` 解释时才会看到「负数」。

`lutRecip` 对 \(x=0\) 做了哨兵处理([vec.scala:L66-L69](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L66-L69)):

```scala
val lutRecip: Seq[Int] = Seq.tabulate(256) { raw =>
  val x = sq16ToDouble(raw)
  if (x == 0.0) 127 else doubleToSq16(1.0 / x)   // 除零 → 最大正 SQ1.6 值 127
}
```

#### 4.1.4 代码实践

**实践目标**:手算 `Qfmt.lutExp(0)`,验证它确实是 \(e^0 = 1\) 的 UQ0.8 定点表示。

**操作步骤**:

1. 打开 [vec.scala:L58-L64](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L58-L64),按 `raw = 0` 代入,逐步算:
   - `x = sq16ToDouble(0) = 0/64 = 0.0`
   - `e = math.exp(0.0) = 1.0`
   - `math.min(1.0, 255.0/256) = min(1.0, 0.99609375) = 0.99609375`(注意 exp 输出被封顶到 \(255/256\))
   - `u = doubleToUq08(0.99609375) = round(0.99609375 × 256) = 255`,再 clamp 到 \([0,255]\) 仍为 255
   - 因 `255 > 127`,返回 `255 - 256 = -1`
2. 所以 `Qfmt.lutExp(0) == -1`(Scala `Int`)。
3. 把这个结果当成 8 位无符号字节看:`-1` 的二补码是 `0xFF = 255`。
4. 按 UQ0.8 还原:\(255 / 256 = 0.99609375 \approx 1.0 = e^0\)。✓

**需要观察的现象**:exp 表在 \(x=0\) 处不是恰好 1.0,而是 0.996——这是 UQ0.8 的封顶造成的最大量化误差约 \(0.4\%\)。

**预期结果**:`Qfmt.lutExp(0)` 在 Scala 里等于 `-1`,但作为无符号 8 位 LUT 项等于 `255`,代表 \(0.996 \approx e^0\)。这也印证了文档 L457 的提醒:只有当谁把 `out_vx` 当 `SInt` 才会看到「−1」。

> 如果无法本地运行 sbt,这一步是纯算术推导,可在纸上完成;若能在 `sbt console` 里执行 `alu.vec.Qfmt.lutExp(0)` 可直接核对等于 `-1`。

#### 4.1.5 小练习与答案

**练习 1**:`Qfmt.lutRecip(128)` 对应的真实输入和输出各是多少?为什么它不是哨兵值?
**答案**:`raw=128` → `signed = 128-256 = -1` → \(x = -1/64 = -0.015625\)(非 0,所以不走 `x==0` 的哨兵分支,哨兵只出现在 `raw=0` 即 \(x=0\) 处);\(1/x = -64\),`doubleToSq16(-64.0)` 内部 `round(-64×64) = -4096`,被 `math.max(-128, math.min(127, -4096))` clamp 到 \(-128\),即返回 \(-128\)(注意 `lutRecip` 没有像 `lutExp` 那样的 `u-256` 符号转换步骤)。作为存储字节是 `0x80`,SQ1.6 下还原为 \(-128/64 = -2.0\)。这说明对接近 0 的负输入,recip 会饱和到 SQ1.6 下限。

**练习 2**:为什么 `lutExp` 用 UQ0.8 输出,而 `lutTanh` 用 SQ1.6 输出?
**答案**:`exp(x)` 恒为正,UQ0.8(无符号)能充分利用 0..255 的全部码点表示 \([0,1)\);`tanh(x)` 取值在 \((-1,1)\),可正可负,必须用有符号的 SQ1.6 才能表示负值。

---

### 4.2 双 bank 可编程 LUT 存储寄存器

#### 4.2.1 概念说明

LUF 要查的表存在哪?chisel-npu 没有用只读 ROM(那样表就写死了,换激活函数要重新综合硬件),而是在 VALU 内部放了两块**可写的寄存器阵列**(bank),每块 256 项、每项 N 位(N=8 时即 256 字节)。这就是「可编程(programmable)」的含义——表内容在运行时由软件决定。

为什么是**两块**(bank A、bank B)而不是一块?为了**双缓冲(double buffering)**:

- 当 bank A 正在服务一条 `vlut`(比如查 exp 表)时,软件可以同时用 `vsetlut` 往 bank B 里预装下一张表(比如 recip 表)。
- 等切换到 recip 查表时,直接选 bank B 即可,**无需先把 A 的内容腾空、再装载 B**——没有停顿(stall)。

这一点在 softmax 流水里尤其关键:softmax 要先用 exp、紧跟着用 recip,两块 bank 让两次查表无缝衔接(见 4.4 与综合实践)。

#### 4.2.2 核心流程

两块 bank 的读写分工:

| 操作 | 方向 | 涉及 bank | 触发指令 |
|:---|:---:|:---:|:---|
| 写表 | 软件写入 bank 寄存器 | A 或 B(由选择位决定) | `vsetlut` |
| 查表 | bank 寄存器读出 | A 或 B(由选择位决定) | `vlut` |

bank 选择位的来源是个巧妙的**复用**:`NCoreVALUBundle` 里本用来表示舍入模式的 `round` 字段,在 LUT 家族被重新解释为 bank 选择位——`round[0] = 0` 选 bank A,`round[0] = 1` 选 bank B。译码器会从 `funct3[0]` 抽出这一位送到 `round[0]`。这样既不增加控制包字段,又让两条指令能各自独立选 bank。

bank 内容的**持久性**:写表逻辑用 `when (op === VecOp.vsetlut)` 严格门控,意味着**只有 `vsetlut` 那一拍才会改写 bank**;其他所有指令(ALU 运算、查表等)都不会动 bank,bank 内容会一直保持到下一次 `vsetlut`。

#### 4.2.3 源码精读

两块 bank 的声明([vec.scala:L117-L125](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L117-L125)),注意它们是 `RegInit`(可写寄存器,复位为 0),且注释明确说 Qfmt 表不再综合成 ROM:

```scala
val lutBankA = RegInit(VecInit(Seq.fill(256)(0.U(N.W))))
val lutBankB = RegInit(VecInit(Seq.fill(256)(0.U(N.W))))
```

bank 选择位从 `ctrl.round[0]` 取得([vec.scala:L160-L162](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L160-L162)):

```scala
val lutSegBits = math.max(1, log2Ceil(math.max(2, 256 / (K * 4))))
val lutSeg     = io.ctrl.imm(lutSegBits - 1, 0).asUInt
val lutBankSel = io.ctrl.round(0)
```

- `lutBankSel = io.ctrl.round(0)`:`round` 字段最低位就是 bank 选择(0=A, 1=B)。
- `lutSegBits` 是 segment 下标的位宽,与 `vsetlut` 分段写表有关(见 4.3),K=8 时为 3 位(0..7)。

译码器侧如何产生这个 bank 选择位([instrDecoder.scala:L288-L297](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L288-L297)):

```scala
// For LUT ops, round[0] carries the bank select (taken from funct3[0]):
//   vlut.A (f3=0) → round=0, vlut.B (f3=1) → round=1
//   vsetlut.A (f3=4) → round=0, vsetlut.B (f3=5) → round=1
io.decoded.valu.round := Mux(
  family === OpFamily.VALU_FP_FMA, rndS,
  Mux(family === OpFamily.VALU_CVT, f7CvtRnd,
    Mux(family === OpFamily.VALU_LUT, Cat(0.U(1.W), f3(0)),   // ← LUT:用 funct3[0]
      f7Round)))
```

即:对 LUT 家族,`round = Cat(0, funct3[0])`,把 `funct3` 最低位塞进 `round[0]`,高位补 0。

#### 4.2.4 代码实践

**实践目标**:确认两块 bank 的物理规模,并理解 bank 选择位为何复用 `round`。

**操作步骤**:

1. 读 [vec.scala:L124-L125](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L124-L125):每块是 `VecInit(Seq.fill(256)(0.U(N.W)))`,即 256 项 × N 位。N=8 时每块 256 字节,两块共 512 字节。
2. 读 `NCoreVALUBundle` 定义([VALUMicroCode.scala:L138-L146](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/VALUMicroCode.scala#L138-L146)),确认里面**没有**专门的 `lutBank` 字段——bank 选择是搭便车用 `round`。
3. 思考:如果改用独立字段会怎样?会多 1 位控制位、需要改 Bundle 与译码器,而 `round` 在 LUT 家族本就用不到(查表不涉及舍入),复用它零成本。

**需要观察的现象**:两块 bank 是独立寄存器,互不影响;写 A 不改变 B,反之亦然(这被 [VALUProgrammableLutSpec.scala:L118-L123](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUProgrammableLutSpec.scala#L118-L123) 的「两 bank 同时装不同表」用例验证)。

**预期结果**:能在源码里指认 bank 存储、bank 选择位来源,并说清「复用 `round` 是因为 LUT 家族不需要舍入模式」。

#### 4.2.5 小练习与答案

**练习 1**:如果要把每块 bank 从 256 项扩到 512 项,需要同步改动哪些地方?
**答案**:至少要改 `lutBankA/B` 的 `Seq.fill(256)` 为 512、`lutSegBits` 的计算分母(每段仍是 K×4 字节,所以段数翻倍)、`vlut` 的下标宽度,以及 `Qfmt` 表的长度。还要重新评估 8 位输入下标能否寻址 512 项(需要 9 位下标,`in_a_vx` 只有 8 位,需扩输入宽度或分段)。

**练习 2**:为什么 bank 用 `RegInit(...0...)` 而不是 `Mem`?
**答案**:bank 容量小(256 字节),用寄存器阵列可实现纯组合读(任意项一拍可读,适合 `vlut` 的并行多 lane 同时查不同地址);`Mem` 多端口随机读会昂贵或受限。代价是寄存器比 SRAM 面积大,但 256 字节可接受。

---

### 4.3 vsetlut:分段写表路径

#### 4.3.1 概念说明

`vsetlut` 解决「怎么把一张 256 字节的表装进 bank」的问题。难点在于:一条 VR 寄存器只有 K×4N 位 = K×4 字节(N=8 时 K=8 → 32 字节),**装不下**整张 256 字节的表。所以 `vsetlut` 采用**分段(segment)装载**:每次写一个 K×4 字节的段,调用若干次填满整块 bank。

`vsetlut` 是一条 **I 型指令**,它的「副作用」只作用于 VALU 内部的 bank 寄存器,**不写回寄存器堆**——这是它与其他所有 VALU 指令的根本区别,也是 4.4 要重点处理的「写回抑制」问题的来源。

#### 4.3.2 核心流程

`vsetlut` 的指令字布局(I 型,opcode `0x13`):

| 字段 | 含义 |
|:---|:---|
| opcode = `0x13` | VALU_LUT 家族 |
| funct3 = `4`(bank A)或 `5`(bank B) | 选 bank,译码后进 `round[0]` |
| rs1 | VR 源寄存器(里面装着 K×4 字节的表数据) |
| imm | segment 下标 `s`(段号) |
| rd | 无(不写回) |

装载一段的伪代码:

```
对 lane k = 0..K-1:
    对字节 b = 0..3:
        idx  = s * (K*4) + (k*4 + b)        # 这一字节在 256 项表里的全局下标
        byte = VR[rs1].lane[k] 的第 [8b+7 : 8b] 字节   # 小端取字节
        bank[idx] = byte                    # 写进 A 或 B(由 round[0] 决定)
```

**段容量**——填满一块 256 字节 bank 需要几次 `vsetlut`,取决于 K:

| K | 每个 VR 携带字节(K×4) | 填满 256 字节 bank 的 `vsetlut` 次数 |
|:---:|:---:|:---:|
| 8 | 32 | 8 |
| 64 | 256 | 1 |

(见 [VectorALU.md:L210-L215](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/VectorALU.md#L210-L215)。)

#### 4.3.3 源码精读

`vsetlut` 的编码助手([NpuAssembler.scala:L139-L153](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L139-L153)):

```scala
/** 写一段 K×4 字节进选定 bank。segment=段号;bank=0→A,1→B。I 型,rd=0。 */
def vsetlut(rs1: Int, segment: Int, bank: Int = 0): Int =
  encI(0x13, 4 + (bank & 1), 0, rs1, segment)
```

- `encI(opcode=0x13, funct3=4+bank, rd=0, rs1, imm=segment)`:bank A 时 funct3=4,bank B 时 funct3=5;`rd=0` 暗示无寄存器目的地。

VALU 内的写表逻辑([vec.scala:L151-L173](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L151-L173)):

```scala
when (op === VecOp.vsetlut) {
  for (k <- 0 until K) {
    for (b <- 0 until 4) {
      val idx  = lutSeg * (K * 4).U + (k * 4 + b).U
      val byte = io.in_a_vr(k)((b + 1) * 8 - 1, b * 8)
      when (!lutBankSel) { lutBankA(idx) := byte }
      .otherwise         { lutBankB(idx) := byte }
    }
  }
}
```

- 外层 `when (op === VecOp.vsetlut)` 保证只在 `vsetlut` 那拍写 bank。
- `idx = lutSeg*(K*4) + (k*4 + b)`:把段号 `lutSeg`(来自 `imm` 低位)与 lane 内字节偏移拼成全局下标。
- `io.in_a_vr(k)((b+1)*8-1, b*8)`:从第 k 个 VR lane(32 位)里按小端取出第 b 个字节。
- `lutBankSel`(即 `round[0]`)决定写 A 还是 B。

注意这套循环是 Chisel 的 `for`,在 elaborate 时**展开成 K×4 个并发的寄存器写**,而非运行时循环——所以一条 `vsetlut` 一拍写完一整段 32 字节(在 K=8 时)。

#### 4.3.4 代码实践

**实践目标**:跟踪 K=8 时一次 `vsetlut` 到底写了哪些表项。

**操作步骤**:

1. 设 K=8,段号 `s=2`,bank A。计算这条 `vsetlut` 写入的全局下标范围:
   - `idx = 2*(8*4) + (k*4 + b) = 64 + k*4 + b`,其中 `k=0..7, b=0..3`。
   - 当 `k=0`:idx = 64,65,66,67;`k=1`:68,69,70,71;…;`k=7`:92,93,94,95。
   - 所以段 `s=2` 覆盖表项 `[64..95]`,共 32 项。
2. 对照测试里的 `loadBank`([VALUProgrammableLutSpec.scala:L66-L81](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUProgrammableLutSpec.scala#L66-L81)),它正是循环 `seg = 0..7`,每次把 4 个表字节小端拼进一个 `word`,再 poke 进 `in_a_vr(k)`:
   ```scala
   for (seg <- 0 until segs) {        // segs = 256/(K*4) = 8
     ...
     for (k <- 0 until K) {
       var word = 0L
       for (b <- 0 until 4) {
         val entry = table(seg * K * 4 + k * 4 + b) & 0xFF
         word |= entry.toLong << (8 * b)      // 小端拼 4 字节
       }
       dut.io.in_a_vr(k).poke(word.U)
     }
     pokeCtrl(dut, VecOp.vsetlut, regCls = 2 /* VR */, bank = bank, imm = seg)
     dut.clock.step()
   }
   ```
3. 观察:`regCls` 被 poke 成 `2`(VR),这样后端才会把 VR 读端口的数据送到 `in_a_vr`(见 4.4 对译码器强制 VR 宽度的说明)。

**需要观察的现象**:8 次 `vsetlut`(段 0..7)正好覆盖 256 个表项,无重无漏。

**预期结果**:能口算出「段 `s` 覆盖表项 \([s \times 32,\ s \times 32 + 31]\)」,并理解 K=8 需 8 次、K=64 需 1 次 `vsetlut` 填满整块 bank。

#### 4.3.5 小练习与答案

**练习 1**:`vsetlut` 的 `imm` 在 K=8 与 K=64 时各需要几位?
**答案**:K=8 时段数=8,需 \(\lceil\log_2 8\rceil = 3\) 位(`lutSegBits = log2Ceil(max(2, 256/32)) = 3`);K=64 时段数=1,`max(2,1)=2`,`log2Ceil(2)=1` 位。源码用 `math.max(1, ...)` 保证至少 1 位。

**练习 2**:如果连续两次 `vsetlut` 写同一 bank 的同一段,会发生什么?
**答案**:第二次会覆盖第一次写入的 32 字节,bank 只保留最后一次的值。这正是「可编程」的体现,也被 [VALUProgrammableLutSpec.scala:L126-L132](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUProgrammableLutSpec.scala#L126-L132) 的「覆盖 bank A」用例验证(先装 exp,再装 recip,最后只剩 recip)。

---

### 4.4 vlut:逐通道查表与写回抑制

#### 4.4.1 概念说明

`vlut` 是查表指令,做的是:把每个 VX lane 的值当 0..255 的下标,到选定的 bank 里读出一个字节,结果送到 `out_vx`。它是一条 **R 型指令**,1 拍出结果(经输出寄存器)。由于每个 lane 独立查自己的下标,一次 `vlut` 就完成 K 个通道的并行非线性映射——这正是 LUT 方案「单拍、零算力」的收益所在。

但本模块真正的重点是 `vsetlut` 在后端引发的**写回抑制**问题。回顾背景:译码器为了让后端把 VR 源数据送到 VALU 的 `in_a_vr`,强制把 `vsetlut` 的 `regCls` 设成 VR。可这一设,会「连累」后端的 VR 写回守卫——因为守卫的判据之一就是 `regCls === VR`。如果不专门处理,`vsetlut` 就会误把(全 0 的)`out_vr` 写进 VR 寄存器,破坏寄存器堆。所以后端必须用 `isSetLut` 守卫**显式抑制** `vsetlut` 的所有 RF 写端口,让它成为唯一一条「只动 VALU 内部状态、不碰寄存器堆」的 VALU 指令。

#### 4.4.2 核心流程

`vlut` 的查表流程(每个 lane 并行):

```
lutIdx = in_a_vx[lane]          # 0..255 的无符号下标
result = bank[lutIdx]           # bank = round[0] ? lutBankB : lutBankA
out_vx[lane] = result           # 经 RegNext 延迟一拍
```

后端写回使能的判定(对 VALU 家族):

```
若 isSetLut(op):                  # vsetlut 特判
    vx_w_en(0) := false            # 抑制所有写端口
    vr_w_en(0) := false
否则:
    vx_w_en(0) := (regCls==VX 或 窄CVT) 且 非归约 且 非setLut
    ve_w_en(0) := (regCls==VE)
    vr_w_en(0) := (regCls==VR 或 宽CVT 或 归约) 且 非setLut
```

`vlut` 自身:汇编器设其宽度为 VX,所以 `regCls=VX`,`vx_w_en(0)` 自然成立,查表结果写回 VX 目的寄存器。

#### 4.4.3 源码精读

`vlut` 的查表逻辑([vec.scala:L228-L230](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L228-L230)):

```scala
// LUT lookup (always VX): raw byte index → byte result from the selected bank
val lutIdx = aU
val vxLut  = Mux(lutBankSel, lutBankB(lutIdx), lutBankA(lutIdx))
```

- `aU = io.in_a_vx(lane)`:原始无符号字节作下标。
- `Mux(lutBankSel, lutBankB, lutBankA)`:`round[0]` 选 bank。
- 结果 `vxLut` 进入 VX 结果多路选择器([vec.scala:L333-L334](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L333-L334)):`VecOp.vlut.asUInt -> vxLut`,再经 `RegNext` 从 `out_vx` 输出([vec.scala:L454](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L454))。

`vlut` 的编码([NpuAssembler.scala:L143-L144](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L143-L144)):宽度恒为 VX,`rs2` 不用:

```scala
def vlut(rd: Int, rs1: Int, bank: Int = 0): Int =
  encR(0x13, bank & 1, f7(VX), rd, rs1, 0)
```

现在看后端写回抑制。译码器先把 `vsetlut` 强制成 VR 宽度([instrDecoder.scala:L220-L225](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L220-L225)),以便后端把 VR 读数据送到 `in_a_vr`:

```scala
// vsetlut (I-format): reads from a VR source register → force VR width so
// the backend routes in_a_vr correctly.
when (family === OpFamily.VALU_LUT &&
      (f3 === Funct3Lut.VSETLUT_A || f3 === Funct3Lut.VSETLUT_B)) {
  width := 2.U  // VR
}
```

这个「好意」却会触雷:后端的 VR 写回守卫 `regCls === VR` 会因此为真。后端用 `isSetLut` 守卫堵住([SimpleBackend.scala:L214-L240](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L214-L240)):

```scala
// isSetLut ops (vsetlut) write only to VALU-internal bank registers;
// all RF write ports must be suppressed.
rf.io.vx_w_en(0)   := ((dec.valu.regCls === W.VX) || isNarrowCvtOut(dec.valu.op)) &&
                       !isReduceToVR(dec.valu.op) && !isSetLut(dec.valu.op)
...
// vsetlut has regCls=VR but must NOT write the RF — suppress it.
rf.io.vr_w_en(0)   := ((dec.valu.regCls === W.VR) || isWideCvtOut(dec.valu.op) ||
                        isReduceToVR(dec.valu.op)) && !isSetLut(dec.valu.op)
```

守卫本身只是一行([SimpleBackend.scala:L264-L268](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L264-L268)):

```scala
/** vsetlut writes only to VALU-internal LUT bank registers.
 *  All register-file write ports must be suppressed for this op. */
def isSetLut(op: VecOp.Type): Bool = op === VecOp.vsetlut
```

为什么 `ve_w_en` 不需要这个守卫?因为 `vsetlut` 的 `regCls=VR`,`ve_w_en := (regCls === W.VE)` 本来就是假,天然安全。而 `vr_w_en` 的判据含 `regCls === W.VR`,会被 `vsetlut` 触发为真,所以**必须**靠 `!isSetLut` 关掉——这正是「为何必须抑制 VR 写端口」的直接原因。

#### 4.4.4 代码实践

**实践目标**:回答 spec 的核心问题——在 backend 中为何 `vsetlut` 必须抑制所有 RF 写端口?

**操作步骤**:

1. 读 [instrDecoder.scala:L220-L225](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L220-L225):确认 `vsetlut` 被强制 `width := 2`(VR)。
2. 读 [SimpleBackend.scala:L235-L237](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L235-L237):VR 写使能判据 `(regCls === W.VR) || ...`,对 `vsetlut` 为真。
3. 读 [vec.scala:L164](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L164) 与宽度门控 [vec.scala:L433-L450](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L433-L450):`vsetlut` 不在任何输出门控里断言,`rawVR` 默认 0,故 `out_vr` 在 `vsetlut` 那拍是 0。
4. 推理:若没有 `!isSetLut` 守卫,`vsetlut` 会把 `out_vr = 0` 写进 VR[rd](汇编器里 `rd=0`),**把 VR 寄存器 0 的内容清零**——这是严重的隐式副作用。
5. 结论:`isSetLut` 守卫把 `vsetlut` 隔离成「纯副作用指令」:它只改 VALU 内部的 bank 寄存器,对寄存器堆完全透明。

**需要观察的现象**:`vsetlut` 执行后,bank 内容改变,但任何 VX/VE/VR 寄存器都不变。

**预期结果**:能用自己的话讲清这条因果链——「译码器为路由 `in_a_vr` 而强制 VR 宽度 → 连累 VR 写回守卫为真 → 必须用 `isSetLut` 显式抑制 → 否则 `out_vr=0` 会污染寄存器堆」。

> 这一步是源码阅读型实践,无需运行仿真;若想验证,可参考 [VALUProgrammableLutSpec.scala:L86-L101](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUProgrammableLutSpec.scala#L86-L101) 的 `sweepLut`,在 `loadBank` 前后 peek 某个 VR 寄存器,确认其未变(待本地验证)。

#### 4.4.5 小练习与答案

**练习 1**:`vlut` 的输入和输出分别走哪个宽度端口?为什么 `rs2` 没用?
**答案**:输入用 `in_a_vx`(VX,N 位)当下标,输出走 `out_vx`(VX)。因为查表是字节进、字节出,都在 VX 宽度即可;`rs2` 在 R 型 `vlut` 里被汇编器填 0,硬件不读它。

**练习 2**:假设删掉 `vr_w_en` 判据里的 `&& !isSetLut(dec.valu.op)`,softmax 流水会出什么错?
**答案**:每次 `vsetlut` 装 exp/recip 表时,都会顺手把 VR[0](或当前 `vr_out_addr` 指向的寄存器)清零。softmax 恰好要用 VR 存放归约中间结果(`vsum` 的输出在 VR),所以归约结果会被紧接着的 `vsetlut` 冲掉,导致后续除法出错。

---

## 5. 综合实践

把本讲四个模块串起来,完成一次「双 bank 装表 + 查表」的端到端跟踪,模拟 softmax 里 exp/recip 的查表环节(完整 softmax 还需归约与除法,见 [u5-l4](u5-l4-reduce-and-broadcast.md) 与 [u7-l2](u7-l2-gemm-softmax-tutorial.md))。

**任务**:对照 [VALUProgrammableLutSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUProgrammableLutSpec.scala) 与 [VALUActivationSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUActivationSpec.scala),在纸上(或 sbt console 里)走完下面这条序列,标注每步涉及的 bank、宽度与寄存器类:

1. **装 exp 表进 bank A**:用 `Qfmt.lutExp` 作为 256 字节表,调用 `vsetlut(rs1, segment=0..7, bank=0)` 共 8 次。
   - 思考:每次 `vsetlut` 的 `regCls` 是什么?后端为何不会误写 RF?(答:VR;因 `isSetLut` 守卫。)
2. **装 recip 表进 bank B**:同样 8 次 `vsetlut(..., bank=1)`,期间 bank A 不受影响(双缓冲)。
3. **查 exp**:对输入向量 `x`(SQ1.6 的 8 位有符号值)执行 `vlut(rd, rs1=x, bank=0)`,得到 \(e^x\) 的 UQ0.8 结果在 VX。
   - 验证 `x=0` 的 lane 输出应为 255(\(\approx e^0\)),对应 [VectorALU.md:L429](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/VectorALU.md#L429) 的 softmax 流程图第 3 步。
4. **查 recip**:对某个标量(如 `vsum` 的结果)执行 `vlut(..., bank=1)`,得到 \(1/\Sigma\) 在 VX。
5. **画一张时序图**:横轴是时钟拍,标出 `vsetlut`×8(A)、`vsetlut`×8(B)、`vlut`(A)、`vlut`(B)各发生在哪拍,以及 bank A/B 内容何时就绪。

**预期结果**:

- 能说清 bank A 装 exp、bank B 装 recip 是**并发可进行**的(双缓冲价值)。
- 能解释 `vlut(A)` 与 `vlut(B)` 各 1 拍出结果(经 `RegNext`)。
- 能指出整条序列里**没有任何一条 `vsetlut` 写回寄存器堆**,只有两条 `vlut` 写回 VX。

> 若在容器内,可运行 `tool/test-specific-spec.sh VALUProgrammableLutSpec`(见 [u9-l2](u9-l2-test-suite-and-ci.md))观察这 6 个子用例(exp/recip/双 bank/覆盖/哨兵/identity)全绿;运行 `VALUActivationSpec` 可看 softmax、GELU 的完整组合。运行命令的精确输出待本地验证。

---

## 6. 本讲小结

- VALU 用**可编程 LUT**(而非多项式或固定 ROM)实现 `exp`/`tanh`/`erf`/`recip` 等非线性激活:单拍、纯组合读、零算力开销,8 位输入正好覆盖 256 项表。
- `Qfmt` 是**纯 Scala** 工具,用 SQ1.6(输入,`/64`)和 UQ0.8(exp 输出,`/256`)定点格式预生成四张参考表;它**不**综合成硬件 ROM,表数据要运行时用 `vsetlut` 灌进硬件。
- 硬件里有两块 **256×N 位的 bank 寄存器**(`lutBankA`/`lutBankB`),支持**双缓冲**:一块服务当前 `vlut`,另一块用 `vsetlut` 预装下一张表,切换无停顿。
- bank 选择位**复用** `ctrl.round[0]`(译码器从 `funct3[0]` 抽出),因为 LUT 家族用不到舍入模式,搭便车零成本。
- `vsetlut`(I 型,分段写表)按 K×4 字节一段装载 bank,K=8 需 8 次、K=64 需 1 次填满;它是**唯一**不写回寄存器堆的 VALU 指令。
- 后端用 `isSetLut` 守卫**显式抑制** `vsetlut` 的所有 RF 写端口——因为译码器为路由 `in_a_vr` 强制其 `regCls=VR`,否则 VR 写回守卫会把 `out_vr=0` 误写进寄存器堆。

---

## 7. 下一步学习建议

- **水平归约与广播**([u5-l4](u5-l4-reduce-and-broadcast.md)):本讲综合实践里 softmax 的 `vsum` 步骤依赖水平归约,下一讲讲清 `vsum`/`vrmax` 如何把 K 通道压缩成 VR 宽度广播结果,以及 `isReduceToVR` 这个与 `isSetLut` 并列的写回守卫。
- **浮点运算**([u5-l3](u5-l3-floating-point.md)):`vlut` 输出的 UQ0.8 是定点,而量化流水线后半段会切到 FP32;了解 FP32/BF16/BF8 后能看懂为何 softmax 最终要用 `vfma` 做缩放。
- **端到端量化与 softmax**([u7-l1](u7-l1-quantization-pipeline.md)、[u7-l2](u7-l2-gemm-softmax-tutorial.md)):把本讲的 `vlut`(exp/recip)、u5-l4 的归约、u5-l3 的浮点拼成完整 `GEMM→vlut(exp)→vsum→vfma` 注意力流水线。
- **继续读源码**:精读 [VALUActivationSpec.scala:L61-L119](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUActivationSpec.scala#L61-L119) 的 `runSoftmax`,它是把本讲 LUT 与归约、算术组合成真实激活函数的最佳示例。
