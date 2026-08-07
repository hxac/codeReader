# 处理单元 PE:累加与 keep 控制

> 本讲属于 **U4 矩阵乘法引擎 MMALU** 的第一讲,也是整个矩阵引擎最底层的一块积木。
> 在读完本讲后,你将理解一个 PE(Processing Element,处理单元)内部到底发生了什么,
> 以及它为什么是 NPU 里「最核心的计算单元」。

---

## 1. 本讲目标

学完本讲后,你应当能够:

1. 说清楚 **PE 是什么**:它是执行 `in_a * in_b + 累加器` 的乘累加(Multiply–Accumulate, MAC)硬件单元,是脉动阵列里重复出现的基本细胞。
2. 准确描述 **keep 控制语义**:`keep=true` 时把新乘积累加进寄存器,`keep=false` 时直接覆盖寄存器。
3. 理解 **BasePE 与 MMPE 的关系**:`BasePE` 是可被替换/扩展的「基座」,`MMPE` 是一个具体的整型 MAC 实现;`MMALU` 通过类型参数 `[T <: BasePE]` 与「按名参数」`pe_gen` 注入任意 PE 子类。
4. 从数学上解释 **为什么累加位宽 `accum_nbits`(32)远大于数据位宽 `nbits`(8)**:为了在累加一长串乘积时不溢出。
5. 参考现有 `PESpec`,亲手写一个仿真测试,验证 keep 的累加/覆盖行为与时序。

---

## 2. 前置知识

本讲默认你已经掌握 [u1-l4 全局参数 N/L/K 与寄存器类概念](u1-l4-params-nlk-and-regclass.md) 中的内容,重点回忆以下几点:

- **N(bits)** 是基础通道位宽(默认 8),也就是本讲里 PE 的 `nbits`。在 `NCoreBackend` 边界上 `N == mmalu.nbits`,由实例化代码强制。
- **K** 是 SIMD 通道数 / 脉动阵列边长,在 backend 边界 `K == mmalu.n`。
- 阵列里每个 PE 处理的就是 N 位的整型数据,累加结果会以更宽的位宽(默认 32 位,对应 **VR** 寄存器位宽 `4N`)写回。

此外需要一点点 Chisel 基础概念(不熟悉的术语下面都会解释):

| 术语 | 含义 |
| --- | --- |
| `Module` | Chisel 里一个硬件模块,对应 Verilog 的 `module`。 |
| `RegInit(x)` | 一个带初值 `x` 的寄存器,跨时钟沿保存状态。 |
| `SInt(w.W)` | 位宽为 `w` 的**有符号**定点整数(补码)。 |
| `when / .otherwise` | Chisel 的条件赋值,对应 Verilog 的 `always` 里 `if/else`。 |
| 「最后连接胜出」(last-connect-wins) | 同一个信号被多次赋值时,**最后一条**赋值生效,这是 Chisel 的核心语义之一。 |

> 一个直觉:NPU 算的主要是「矩阵乘」。矩阵乘的本质是大量「**两个数相乘,再把乘积累加起来**」——也就是点积。PE 就是把这一句「乘 + 累加」做成硬件的最小单元,成百上千个 PE 拼成的阵列就是脉动阵列(下一讲 u4-l2 专题)。

---

## 3. 本讲源码地图

本讲只涉及 PE 本身,源码非常短,但它是后面整个 MMALU 的地基。

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [src/main/scala/alu/pe/basePE.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/pe/basePE.scala) | PE 的**基座类** `BasePE`:定义 IO 接口、累加器寄存器 `res`、默认 MAC 行为 | 接口、位宽、可替换性 |
| [src/main/scala/alu/pe/procElem.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/pe/procElem.scala) | PE 的**具体类** `MMPE`:整型乘累加单元,NPU 的核心计算单元 | 具体实现、与 BasePE 的继承关系 |
| [src/main/scala/isa/micro_op/MMALUMicroCode.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/MMALUMicroCode.scala) | `NCoreMMALUCtrlBundle`:PE 唯一真正读取的控制位 `keep` 就来自这里 | keep 字段的定义 |
| [src/main/scala/alu/mma/mma.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala) | `MMALU` 顶层:用 `pe_gen` 参数把 PE **注入**到 n×n 阵列 | 注入机制(可换 PE 的关键) |
| [docs/implementations/ProcessingElement.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/ProcessingElement.md) | PE 的设计文档(含波形示意图) | keep 与「流式多 GEMM」的设计意图 |
| [src/test/scala/alu/pe/PESpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/pe/PESpec.scala) | PE 的仿真测试 | 本讲代码实践的范本 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开:**BasePE(基座与接口)→ MMPE(具体单元)→ keep 控制逻辑(累加/覆盖与流式)**。

### 4.1 BasePE:PE 的接口与累加器基座

#### 4.1.1 概念说明

PE 的全部行为可以浓缩成一句话:

> 每个时钟沿,把「当前输入的两个数相乘」的结果,根据 `keep` 决定是**加到**寄存器里还是**覆盖**寄存器,然后把寄存器的值送到输出。

这里有两件东西需要先固定下来:**接口**(PE 对外暴露哪些信号)和**那个保存累加结果的寄存器**(它的位宽是本讲一个关键设计点)。`BasePE` 就是把这两件事固化下来的「基座类」——它定义了所有 PE 共用的 IO 形状和一个带初值的累加器寄存器 `res`,并且给出了一套默认的 MAC 行为。

之所以把它单独做成一个类(而不是直接把所有逻辑写死在一个 `MMPE` 里),是为了**可替换性**:`MMALU` 在实例化时通过一个「PE 生成器」参数注入 PE(见 4.2)。如果你将来想换一个浮点 PE、或者一个低精度 PE,只要新写一个继承 `BasePE` 的子类、改一行实例化代码即可,阵列里其余的数据通路完全不用动。

#### 4.1.2 核心流程

`BasePE` 在 elaborate(精细化,即把参数代入生成具体硬件)时建立的数据通路如下:

```
        ┌──────────── NCoreMMALUCtrlBundle ────────────┐
        │  keep (本讲唯一真正使用的位)                  │
        └────────────────────┬──────────────────────────┘
                             │
 in_a (SInt nbits) ──┐       │
                     ├──► [乘法 in_a*in_b] ──► [选择: keep?]
 in_b (SInt nbits) ──┘                                │
                                                      ▼
                              ┌──────────────────────────────┐
                              │  res : Reg(SInt accum_nbits) │  ← 累加器寄存器
                              │  keep=true : res <= res+prod │
                              │  keep=false: res <= prod     │
                              └──────────────┬───────────────┘
                                             │
                                             ▼
                                  out (SInt accum_nbits)
```

要点:

1. `in_a`、`in_b` 是 **N 位有符号**输入(默认 8 位)。
2. 中间的乘积 `in_a * in_b` 在 Chisel 里会自动展宽到 `2*nbits` 位(下面 4.1.3 解释为什么)。
3. 累加器 `res` 是一个 **`accum_nbits` 位**(默认 32 位)的寄存器,远宽于输入。
4. 输出 `out` 直接把寄存器 `res` 的当前值送出,因此 `out` 是**寄存后的值**:本拍看到的 `out` 反映的是上一拍写入 `res` 的结果。

#### 4.1.3 源码精读

先看 `BasePE` 的类签名与 IO 定义:

[basePE.scala:12-22](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/pe/basePE.scala#L12-L22) —— 定义 `BasePE` 的构造参数与 IO 接口(累加器位宽的注释也在这一段):

```scala
class BasePE(val nbits: Int = 8, val accum_nbits: Int = 32) extends Module {
  val io = IO(
    new Bundle {
      val ctrl        = Input(new NCoreMMALUCtrlBundle())
      val in_a        = Input(SInt(nbits.W))
      val in_b        = Input(SInt(nbits.W))
      //  The register bandwith is optimized for large transformer
      //  The lower bound of max cap matrix size is:
      //    2^12 x 2^12 = (4096 x 4096)
      val out         = Output(SInt(accum_nbits.W))
  })
```

几个要点逐条对应:

- **两个构造参数都有默认值**:`nbits = 8`、`accum_nbits = 32`。这正好对应全局参数 N=8 与 VR 宽度 4N=32。
- `in_a` / `in_b` 都是 `SInt(nbits.W)`,即 N 位**有符号**整数。注意是有符号——矩阵乘里权重和激活都可能是负数,补码运算必须用 `SInt` 而不是 `UInt`。
- `ctrl` 是 `NCoreMMALUCtrlBundle`,里面有 `keep / use_accum / busy` 三个位,但 **PE 内部只读 `keep`**(`use_accum`、`busy` 是给 MMALU 里 ControlUnit / DataCollector 用的,PE 不关心)。
- `out` 是 `SInt(accum_nbits.W)`,宽度等于累加器宽度,而非输入宽度。

接着是累加器寄存器本身:

[basePE.scala:24](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/pe/basePE.scala#L24) —— 声明累加器寄存器,初值为 0:

```scala
val res = RegInit(0.S(accum_nbits.W))
```

`RegInit(0.S(accum_nbits.W))` 表示:一个 `accum_nbits` 位的有符号寄存器,复位/初值为 0。这就是 PE 唯一的状态——整个 PE 除了这个寄存器,没有任何其它时序元件,所以 PE 的「记忆」只有「当前累加和」。

**为什么 `accum_nbits`(32) 要远大于 `nbits`(8)?** 关键就在源码里那条注释「max cap matrix size 2^12 × 2^12」。我们来算一笔账:

- 单次乘积:两个 8 位有符号数相乘,极端值是 \((-128)\times(-128) = 16384 = 2^{14}\)。要表示 \([-2^{14}, 2^{14}]\) 需要 15 个幅度位 + 1 个符号位 = 16 位。Chisel 的有符号乘法恰好把结果展宽到 `nbits + nbits = 16` 位,正好够装下。
- 累加一串乘积:矩阵乘的一个点积,要把「归约维 R」上的 R 个乘积全加起来。注释设定 R 最多到 \(2^{12} = 4096\)。最坏情况下累加和的幅度为:

\[
R \times 2^{14} = 2^{12} \times 2^{14} = 2^{26}
\]

表示 \([-2^{26}, 2^{26}]\) 需要 27 位(26 幅度位 + 1 符号位)。

- `accum_nbits = 32 > 27`,还留了约 5 位的余量。**结论:32 位累加器足以在 4096 长度的归约维下不溢出**,这就是 `accum_nbits` 远大于 `nbits` 的根本原因,也是矩阵乘结果能「不截断直写 VR」(见 u6)的前提。

> 💡 这一节真正要带走的不只是「32 比 8 大」,而是「**累加位宽必须够装下『最长点积』的所有乘积之和**」这条设计准则。换一批参数(比如 nbits=16 或更长的归约维)时,要重新算 `accum_nbits` 是否还够。

#### 4.1.4 代码实践(源码阅读型)

**实践目标**:确认「PE 是按需被实例化、且实例化个数随阵列边长 n 平方增长」这件事,为下一讲 u4-l2 的脉动阵列做铺垫。

**操作步骤**:

1. 打开 [src/main/scala/alu/mma/mma.scala:23](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L23),观察 `MMALU` 的类签名:

   ```scala
   class MMALU[T <: BasePE](pe_gen: => BasePE, val n: Int = 8, ...) extends Module
   ```

   - `[T <: BasePE]`:类型参数,约束「注入的 PE 必须是 `BasePE` 的子类」。
   - `pe_gen: => BasePE`:**按名参数**(by-name),每次用到 `pe_gen` 时才会重新求值一次 `new MMPE(...)`。

2. 打开 [mma.scala:34](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L34),看 PE 如何被铺成阵列:

   ```scala
   val pe_io = VecInit(Seq.fill(n * n) {Module(pe_gen).io})
   ```

   这里 `Seq.fill(n * n)` 会调用 `Module(pe_gen)` 共 \(n^2\) 次,即阵列里摆 \(n \times n\) 个 PE。

3. 对照 [top.scala:15](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/top/top.scala#L15) 的 `new MMALU(new MMPE(), 32, 8, 32)`,以及 [SimpleBackend.scala:158](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L158) 的 `Module(new MMALU(new MMPE(N), K, N, N4))`。

**需要观察的现象 / 预期结果**:

- 默认 `n=32` 时,`Seq.fill(n*n)` 生成 \(32 \times 32 = 1024\) 个 `MMPE` 实例。如果你照 [u1-l3](u1-l3-source-layout-and-top.md) 的实践把 `n` 改成 8,这个数量会变成 64。
- 「按名参数」意味着 `pe_gen` 不是「预先 new 好一个 PE 传进去」,而是「一个会反复 new 的配方」——这正是能注入自定义 PE 的关键:把 `new MMPE()` 换成 `new MyFloatPE()` 即可全阵列替换。

> 待本地验证:在容器内 `make build`(n=32)后,在生成的 `top.sv` 里统计 `MMPE` 模块实例个数,应与 \(n^2\) 一致。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `in_a` / `in_b` 用 `SInt` 而不是 `UInt`?如果误用 `UInt`,矩阵乘在哪里会出错?

> **参考答案**:权重和激活都可能为负,补码运算必须用有符号 `SInt`。若用 `UInt`,负数会被当成大正数,乘积符号错误,整个点积结果错误。Chisel 里 `SInt * SInt` 才会做正确的符号扩展与有符号乘。

**练习 2**:假设把 `nbits` 改成 16、归约维仍是 \(2^{12}\),用本节的公式估算 `accum_nbits` 至少要多大才不溢出?

> **参考答案**:单次乘积极值 \(2^{15-1}\times 2^{15-1}\) 的量级——更直接地,两个 16 位有符号数乘积最多需要 32 位;再累加 \(2^{12}\) 个,幅度上界 \(\approx 2^{31+12}=2^{43}\),加上符号需约 44 位。所以 `accum_nbits` 至少要 44 位左右才安全(实际设计会向上取整到 48 或 64)。

---

### 4.2 MMPE:乘累加核心单元

#### 4.2.1 概念说明

`BasePE` 只是「基座」;真正被实例化、被阵列使用的具体 PE 是 `MMPE`(MM = Matrix Multiplication)。它的注释写得很直白:「processing element unit in npu design. **This is the core compute unit.**」——它是 NPU 最核心的计算单元。

`MMPE` 继承自 `BasePE`,做的运算就是整型乘累加(MAC):

\[
\text{res}_{\text{next}} = \begin{cases} \text{res} + a\cdot b, & \text{keep} = 1 \\ a\cdot b, & \text{keep} = 0 \end{cases}
\]

理解 `MMPE` 有一个容易困惑的点:你会发现它的代码和 `BasePE` 几乎**一模一样**(都是那两行 `when/otherwise`)。这不是笔误,而是 Chisel 继承的一种用法——`BasePE` 里已经写了一套默认 MAC,`MMPE` 继承后再把同样的逻辑「重写」一遍。由于 Chisel 的「最后连接胜出」语义,**子类 `MMPE` 的赋值会在父类之后执行并生效**。当前 `MMPE` 重写的内容与默认值相同,所以行为不变;但这种结构把「可以在这里改写算法」的扩展点显式留了出来:想换一种 MAC(比如带舍入、带饱和的版本),在子类里改 `res :=` 即可,接口和寄存器都复用 `BasePE`。

> 诚实说明:`BasePE` 在代码里是一个**具体类**(`class`,非 Scala `trait`、也非 `abstract`),它本身就能实例化、本身就含完整 MAC。本讲把它称为「基座/抽象基类」,指的是它在**设计意图**上扮演「可被继承改写的公共基座」这个角色,而不是说它在 Scala 语法上是 abstract。

#### 4.2.2 核心流程

`MMPE` 的执行流程(从输入到输出,逐拍):

1. 组合求乘积:`prod = in_a * in_b`,Chisel 自动得到 `2*nbits` 位有符号结果。
2. 看 `keep`:
   - `keep=true`:把 `prod` **加到** `res` 的当前值上,作为下一拍 `res` 的新值。
   - `keep=false`:用 `prod` **覆盖** `res`,作为下一拍 `res` 的新值。
3. 下一个时钟沿,`res` 更新为新值。
4. `out` 始终等于 `res` 的**当前**值——因此 `out` 比输入晚 1 拍(寄存器输出)。

伪代码(用 Verilog 风格描述这拍的行为):

```
// 时钟沿触发的寄存器更新
always @(posedge clk) begin
  if (reset) res <= 0;
  else if (keep) res <= res + ($signed(in_a) * $signed(in_b));
  else           res <=       ($signed(in_a) * $signed(in_b));
end
assign out = res;   // 组合输出当前寄存器值
```

#### 4.2.3 源码精读

`MMPE` 的全部代码只有 8 行:

[procElem.scala:12-19](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/pe/procElem.scala#L12-L19) —— `MMPE` 继承 `BasePE` 并实现整型乘累加:

```scala
class MMPE(nbits: Int = 8, accum_nbits: Int = 32) extends BasePE(nbits, accum_nbits) {
  when (io.ctrl.keep) {
    res := res + (io.in_a * io.in_b)
  } .otherwise {
    res := (io.in_a * io.in_b)
  }
  io.out := res
}
```

逐行拆解:

- `extends BasePE(nbits, accum_nbits)`:把构造参数透传给父类,因此 `io`、`res` 都是从 `BasePE` 继承来的,`MMPE` 自己不再重复声明。
- `io.in_a * io.in_b`:两个 `SInt(nbits.W)` 相乘,Chisel 产出 `SInt((2*nbits).W)` 的有符号乘积——这正是 4.1.3 里说的「自动展宽到 16 位」。
- `res := res + (乘积)`:把乘积加到累加器。加法会再做一次位宽推导,结果放进 `accum_nbits` 位的 `res`(有足够余量,不会丢精度)。
- `res := (乘积)`:覆盖。注意是「直接赋乘积」,而不是「先清零再累加」——覆盖路径完全不读旧 `res`,所以上一拍的累加和被丢弃。
- `io.out := res`:把寄存器当前值送到输出。

再看一眼 `keep` 这个控制位从哪里来:

[MMALUMicroCode.scala:7-11](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/MMALUMicroCode.scala#L7-L11) —— PE 唯一读取的控制位 `keep` 定义在此:

```scala
class NCoreMMALUCtrlBundle () extends Bundle {
    val keep = Bool()
    val use_accum = Bool()
    val busy = Bool()
}
```

承接 [u3-l1](u3-l1-microop-and-ctrl-bundles.md) 的结论:`keep` 是**唯一**由译码器真正产生的 MMA 控制位(它复用了指令字 funct7 的 sat 位),`use_accum` / `busy` 由后端派生或占位。PE 只关心 `keep`,所以本讲也只讲 `keep`。

#### 4.2.4 代码实践(阅读 + 手算型)

**实践目标**:在不跑仿真的前提下,通过「手算 + 读源码」确认 `MMPE` 的位宽推导和单拍行为,为下一节的仿真实践做准备。

**操作步骤**:

1. 读 [procElem.scala:14](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/pe/procElem.scala#L14) 与 [procElem.scala:16](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/pe/procElem.scala#L16)。注意 `keep` 分支用的是 `res + (in_a*in_b)`,而 `otherwise` 分支**只有** `in_a*in_b`。
2. 手算下面这个序列(默认 `nbits=8, accum_nbits=32`,初值 `res=0`),逐拍写下 `res` 的新值与 `out` 的值:

   | 拍 | in_a | in_b | keep | 旧 res | 新 res(下一拍) | 本拍 out(=旧 res) |
   | --- | --- | --- | --- | --- | --- | --- |
   | 0 | 2 | 3 | false | 0 | ? | ? |
   | 1 | 4 | 5 | true  | ? | ? | ? |
   | 2 | 4 | 5 | false | ? | ? | ? |

**预期结果**(填空答案):

- 拍0:新 res = \(2\times3 = 6\)(覆盖,旧 res=0 被忽略);本拍 out = 0(还是初值)。
- 拍1:新 res = \(6 + 4\times5 = 26\)(累加);本拍 out = 6。
- 拍2:新 res = \(4\times5 = 20\)(覆盖,26 被丢弃);本拍 out = 26。

**需要观察的现象**:「本拍 out」永远等于「上一拍写入的 res」,正好滞后一拍——这是寄存器输出的固有性质,也是 4.3 节仿真里 `poke → step → expect` 顺序的依据。

> 待本地验证:本表是手算结果;真正跑仿真请用 4.3.4 的 `PESpec` 范本验证。

#### 4.2.5 小练习与答案

**练习 1**:`MMPE` 的 `otherwise` 分支写的是 `res := (io.in_a * io.in_b)`,而不是 `res := 0`。这两种写法在「覆盖」语义下结果相同吗?为什么作者选择前者?

> **参考答案**:不同。`res := 0` 要到**再下一拍**喂入乘积才会算出结果(因为先清零、再加,需要两拍);而 `res := in_a*in_b` 在**当拍**就把乘积写进去,一拍完成。作者选前者,是为了让「覆盖」也是单拍生效,保持 PE 每拍都能产出有效 MAC 的流水节奏。

**练习 2**:既然 `MMPE` 的逻辑和 `BasePE` 完全相同,为什么不直接用 `BasePE`,还要再写一个 `MMPE`?

> **参考答案**:这是为扩展留的「具名扩展点」。`MMALU` 的类型参数是 `[T <: BasePE]`、注入的是 `new MMPE()`,语义上「阵列用的是 MMPE 这一种 PE」。把具体类单独命名,便于将来新增 `FloatPE`、`LowPrecPE` 等并列子类时,只改注入处一行即可切换,而不必改动 `BasePE` 或阵列连线。

---

### 4.3 keep 控制逻辑与流式累加

#### 4.3.1 概念说明

`keep` 虽然只有 1 个比特,却是整个矩阵引擎「能高效工作」的核心开关。它的语义非常简单:

- **`keep = true`(累加)**:新乘积**加进** `res`。连续多拍 `keep=true`,就等于在 `res` 里累加一个**点积**(一长串乘积之和)。
- **`keep = false`(覆盖)**:新乘积**替换** `res`。相当于「清掉旧结果,从这一拍重新开始算一个新的点积」。

这条机制之所以重要,是因为它直接服务于「**流式(streaming)多 GEMM**」:NPU 不会把一整个矩阵乘算完再算下一个,而是让数据像流水一样源源不断流过阵列。用 `keep=true` 累加当前这次矩阵乘的部分积,到了边界用一拍 `keep=false`「收尾并开启下一次矩阵乘」——无需停顿、无需清零指令,阵列始终满载。这一点 [ProcessingElement.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/ProcessingElement.md) 原文写得很明确:

> PE component will only accumulate the result if the `ACCUM` is high. **This is efficient for pipelining multiple GEMM in stream.**

承接 [u4-l5](u4-l5-mma-top-and-streaming.md)(后续讲义)会讲到的 M×K 流式归约:一条 `keep=true` 的指令就能让 PE 自动累加 K 拍、每 K 拍吐出一个累积和,根源就是本节这套 `keep` 语义。

#### 4.3.2 核心流程

把 keep 的行为画成时序(纵轴是时钟拍,`res` 在每拍沿更新,`out` 跟随 `res`):

```
拍号:     0     1     2     3     4
in_a:     a1    a2    a3    a4    a5
in_b:     b1    b2    b3    b4    b5
keep:     1     1     0     1     1
res下一拍:0+a1b1  +a2b2  a3b3   +a4b4 +a5b5
          (本次GEMM点积累加)  (覆盖→开启新点积)(新点积累加)
out(本拍):0     a1b1  a1b1+a2b2  a3b3  a3b3+a4b4
```

读法:

- 拍0~拍1 `keep=1`:PE 在累加「第一个点积」\(a_1b_1 + a_2b_2\)。
- 拍2 `keep=0`:这一拍的乘积 \(a_3b_3\) **覆盖**掉之前的累加和,等价于「第一个点积结束、第二个点积从 \(a_3b_3\) 重新开始」。
- 拍3~拍4 `keep=1`:继续累加第二个点积 \(a_3b_3 + a_4b_4 + a_5b_5\)。

这正是「无需专门清零,靠一拍 `keep=0` 就能切换到下一次累加」的流式工作方式。

> 设计文档 [ProcessingElement.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/ProcessingElement.md) 用一段 wavedrom 波形表达了同样的故事:输出依次为 `prod1=a_1*b_1`、`prod_1+a_2*b_2`、`prod_3=a_3*b_3`、`prod_3+a_4*b_4`——即「累加 → 累加 → 覆盖(新点积) → 累加」,中间 `accu` 拉低一拍就完成了切换。

#### 4.3.3 源码精读

keep 的判定逻辑同时出现在 `BasePE` 和 `MMPE` 里,内容一致。看 `BasePE` 这一份:

[basePE.scala:24-31](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/pe/basePE.scala#L24-L31) —— 累加器寄存器与 keep 控制的核心:

```scala
val res = RegInit(0.S(accum_nbits.W))

when (io.ctrl.keep) {
  res := res + (io.in_a * io.in_b)
} .otherwise {
  res := (io.in_a * io.in_b)
}
io.out := res
```

对应关系:

- `RegInit(0.S(accum_nbits.W))`:累加器,复位为 0。**这是 PE 能「记住」累加和的唯一原因**——没有寄存器,组合逻辑算完就丢,无法跨拍累加。
- `when (io.ctrl.keep) { res := res + ... }`:累加分支。
- `.otherwise { res := ... }`:覆盖分支,只写新乘积、不读旧 `res`。
- `io.out := res`:`out` 是 `res` 的**当前值**,组合输出。

再确认一次 `keep` 的来源链:指令字 funct7 的 sat 位 → 译码器 `DecodedMicroOp.mma_keep` → `NCoreMMALUCtrlBundle.keep` → PE 的 `io.ctrl.keep`(详见 [u3-l1](u3-l1-microop-and-ctrl-bundles.md) 与 [u2-l2](u2-l2-opcode-families-attrs.md))。也就是说,**一条矩阵乘指令是否累加,最终就体现在这一个比特上**。

#### 4.3.4 代码实践(仿真型,本讲主实践)

**实践目标**:参考 [PESpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/pe/PESpec.scala),亲手写一个最小测试,验证 keep 的「累加」与「覆盖」两种语义,并体会 `poke → step → expect` 的时序。

**操作步骤**:

1. 先读现有测试 [PESpec.scala:12-51](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/pe/PESpec.scala#L12-L51),注意它的三段:128 拍随机 `keep=true` 累加(行 18-28)、一拍 `keep=false` 覆盖(行 30-39)、再一拍 `keep=true` 累加(行 41-49)。
2. 在 `src/test/scala/alu/pe/` 下新建一个测试(示例代码,改编自 PESpec),聚焦本讲的具体数值:

   ```scala
   // 示例代码:PeKeepDemoSpec.scala(改编自 PESpec,用于本讲练习)
   package alu.pe
   import chisel3._
   import chisel3.simulator.EphemeralSimulator._
   import org.scalatest.flatspec.AnyFlatSpec
   import chisel3.experimental.BundleLiterals._

   class PeKeepDemoSpec extends AnyFlatSpec {
     "MMPE keep demo" should "accumulate then overwrite" in {
       simulate(new MMPE(8, 32)) { dut =>
         // —— 第一拍:(2,3), keep=false,从 0 开始 ——
         dut.io.in_a.poke(2.S)
         dut.io.in_b.poke(3.S)
         dut.io.ctrl.keep.poke(false.B)
         dut.clock.step()                      // 沿后 res = 2*3 = 6
         dut.io.out.expect(6)                  // out 现在反映 res=6

         // —— 第二拍:(4,5), keep=true,累加 ——
         dut.io.in_a.poke(4.S)
         dut.io.in_b.poke(5.S)
         dut.io.ctrl.keep.poke(true.B)
         dut.clock.step()                      // 沿后 res = 6 + 4*5 = 26
         dut.io.out.expect(26)                 // 2*3 + 4*5 = 26 ✓

         // —— 第三拍:再喂(4,5), keep=false,覆盖 ——
         dut.io.in_a.poke(4.S)
         dut.io.in_b.poke(5.S)
         dut.io.ctrl.keep.poke(false.B)
         dut.clock.step()                      // 沿后 res = 4*5 = 20
         dut.io.out.expect(20)                 // 26 被覆盖为 4*5 = 20 ✓
       }
     }
   }
   ```

3. 运行单测。按 [u1-l2](u1-l2-build-and-run.md) 与 [u9-l2](u9-l2-test-suite-and-ci.md) 的方式,在容器内用项目自带的快捷脚本只跑这一个 spec:

   ```bash
   ./tool/test-specific-spec.sh alu.pe.PeKeepDemoSpec
   ```
   (若脚本参数风格不同,也可 `sbt "testOnly alu.pe.PeKeepDemoSpec"`。)

**需要观察的现象**:

- 第二拍 `keep=true` 后,`out` 为 `26 = 2*3 + 4*5`,证明「累加」生效。
- 第三拍 `keep=false` 后,`out` 为 `20 = 4*5`,证明旧的 `26` 被「覆盖」。
- 把任意一拍的 `poke` 与 `step` 之间加注释观察:`expect` 必须在 `step` **之后**调用,因为 `out` 是寄存器输出。

**预期结果**:三个 `expect`(6、26、20)全部通过。若把第二拍的 `keep` 误设为 `false`,`expect(26)` 会失败(实际得到 `20`);若把第三拍的 `keep` 误设为 `true`,`expect(20)` 会失败(实际得到 `26+20=46`)——这正好反过来验证了 keep 的语义。

> 待本地验证:上述数值与命令需在你本地的 Docker 容器(`fangruil/chisel-dev`)中实际运行确认;如果你不方便新建文件,直接读懂现有 `PESpec` 并对照 4.2.4 的手算表也算完成本实践。

#### 4.3.5 小练习与答案

**练习 1**:如果想要 PE 计算一个长度为 4 的点积 \(a_1b_1+a_2b_2+a_3b_3+a_4b_4\),这 4 拍的 `keep` 应该怎么设置?

> **参考答案**:第一拍 `keep` 可以为 `true` 也可以为 `false`(因为 `res` 初值是 0,`0 + a1b1` 与 `a1b1` 相同);第 2~4 拍必须 `keep=true`,才能把后续乘积累加进去。最简洁的写法是 4 拍全部 `keep=true`。

**练习 2**:`out` 为什么比输入「晚一拍」?如果想让 `out` 当拍就反映本拍输入的乘积,需要改什么?代价是什么?

> **参考答案**:因为 `out := res`,而 `res` 是寄存器,本拍看到的是上一拍写入的值。要当拍就出结果,得把 `res` 改成组合线(wire)直接算 `in_a*in_b`,但那样就**丢失了累加能力**(组合逻辑无法跨拍保存和),也就不成其为 MAC 了。寄存器带来的「1 拍延迟」是 PE 能累加的代价。

**练习 3**:用一句话解释「为什么一拍 `keep=false` 就能等价于『结束旧点积、开始新点积』,而不需要一条专门的清零指令」。

> **参考答案**:因为 `keep=false` 分支直接把新乘积写进 `res`,既丢弃了旧累加和,又写入了新点积的第一项,清零与重启在同一个时钟沿一次完成。

---

## 5. 综合实践

把本讲三个模块串起来,完成下面这个「迷你点积机」任务:

**任务**:用单个 `MMPE(8, 32)`,在仿真里连续计算两个点积,中间不停顿、不专门清零,验证流式工作方式。

1. **第一个点积** \(D_1 = 2\cdot3 + 4\cdot5 + (-1)\cdot7\):
   - 连续 3 拍喂入 \((2,3)\)、\((4,5)\)、\((-1,7)\),全部 `keep=true`(第 1 拍 `keep` 取 true 或 false 均可)。
   - 第 3 拍 `step` 后,`out` 应为 \(6+20-7 = 19\)。
2. **切换到第二个点积** \(D_2 = 10\cdot10 + 1\cdot1\):
   - 紧接着喂 \((10,10)\) 且 `keep=false`(这一拍既结束 \(D_1\)、又开启 \(D_2\))。
   - 再喂 \((1,1)\) 且 `keep=true`。
   - 切换拍 `step` 后 `out` 应为 \(100\)(=`10*10`);\((1,1)\) 拍 `step` 后 `out` 应为 \(100+1 = 101\)。
3. **观察**:在整个过程中,你没有写过任何「清零」操作——两次点积的边界完全由一拍 `keep=false` 搞定。这正是 4.3 说的「流式多 GEMM」在单个 PE 上的缩影。
4. **进阶**:把上述 5 拍的 `in_a/in_b/keep` 与期望 `out` 整理成一张表(参考 4.3.2 的格式),并解释为什么 `out` 列整体比 `res` 列晚一拍。

> 这是「源码阅读 + 仿真」混合实践:数值可手算预判,最终以容器内 `sbt "testOnly ..."` 的 `expect` 通过为准(待本地验证)。

---

## 6. 本讲小结

- **PE 是 NPU 最核心的计算单元**,本质上是一个带累加器寄存器的乘累加(MAC)模块:每拍算 `in_a * in_b`,根据 `keep` 决定累加或覆盖。
- **`BasePE` 是基座**:定义了 IO 接口(`ctrl/in_a/in_b/out`)、累加器寄存器 `res`、以及累加位宽 `accum_nbits`;它把「可被继承改写、可被注入替换」的扩展点留了出来。
- **`MMPE` 是具体实现**:继承 `BasePE`,做整型 MAC,是实际被阵列使用的 PE;`MMALU` 通过 `[T <: BasePE]` + 按名参数 `pe_gen` 把它注入 n×n 阵列(共 \(n^2\) 个)。
- **`keep` 是核心开关**:`keep=true` 累加、`keep=false` 覆盖;一拍 `keep=false` 就能「结束旧点积 + 开启新点积」,无需清零指令,支撑流式多 GEMM。
- **累加位宽远大于数据位宽**:`accum_nbits=32` vs `nbits=8`,是为了在归约维长达 \(2^{12}\) 时仍不溢出(\(2^{12}\times2^{14}=2^{26}\),需 27 位,32 位足够)。
- **时序**:`out` 是寄存器输出,比输入晚 1 拍;仿真里必须 `poke → step → expect` 的顺序。

---

## 7. 下一步学习建议

PE 只是阵列里的「一个细胞」。下一讲进入「把很多 PE 拼起来」:

- **u4-l2 二维脉动阵列 SystolicArray2D**:看 `reg_h`/`reg_v` 两组移位寄存器如何把输入向量错拍馈送到 n×n 个 PE,以及为什么对角线 PE 在同一拍拿到同一组数据。建议先吃透本讲的 keep 语义,因为阵列正是靠 keep 控制每个 PE 的累加窗口。
- **u4-l3 数据馈送器与收集器**:看寄存器堆的数据如何经 `DataFeeder` 进入阵列、PE 的累加结果又如何被 `DataCollector` 收回。
- **u4-l5 MMALU 顶层与流式归约**:回到「M×K 流式归约只需一条 `keep=true`」的全貌,你会发那条结论的根基就是本讲的 keep 控制。

如果你还想从「指令怎么产生 keep」的角度补全链路,可以回看 [u2-l2 opcode 家族与 funct3/funct7 属性](u2-l2-opcode-families-attrs.md)(keep 复用 funct7 的 sat 位)和 [u3-l1 微操作与控制 Bundle](u3-l1-microop-and-ctrl-bundles.md)(keep 是 `NCoreMMALUCtrlBundle` 里唯一译码来的位)。
