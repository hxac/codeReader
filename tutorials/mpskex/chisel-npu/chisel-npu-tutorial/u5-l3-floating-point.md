# FP32/BF16/BF8 浮点运算

## 1. 本讲目标

本讲是 VALU 浮点能力的专门讲义。学完后你应当能够：

1. 读懂 `fp.scala` 中 `IEEE754` 对象提供的 FP32 组合逻辑（加、乘、FMA、转换），并说清它的 **Tier-2 设计取舍**：RNE 默认、Flush-To-Zero（FTZ）、无 NaN、溢出饱和。
2. 手算任意一个普通 FP32 数的位模式（符号 / 指数 / 尾数），并解释 FTZ 对极小输入的影响。
3. 区分 **BF8 的两种变体 E4M3 与 E5M2**，说出各自的位宽、偏置、典型用途（激活 vs 权重/梯度），以及 `funct7[6]` 如何在 VALU 里选变体。
4. 理解 `fp.scala` 全部是**纯组合逻辑**，VALU 在外围加 1 拍输出寄存器；并理解 `FpRef` Scala 参考模型如何支撑 bit-exact（逐位精确）测试。

---

## 2. 前置知识

本讲假设你已经读过 **u5-l1（VALU 多宽度数据通路）**，知道：

- VALU 是 K 通道、三宽度（VX/VE/VR）的向量协处理器，FP32 走 **VR 通路**（每 lane 32 位）。
- VALU 用「通路复用」：所有 op 的候选结果都在同一个 per-lane 循环里组合算出，再用 `MuxLookup` 按 `op` 选一个；输出经 `RegNext` 寄存，带来 1 拍延迟。

此外需要一点 IEEE 754 二进制浮点的基础直觉：

- 一个浮点数表示为 \( \text{value} = (-1)^s \times 1.m \times 2^{e} \)，其中 \(s\) 是符号位，\(1.m\) 是「隐含前导 1 + 小数部分 \(m\)」，\(e\) 是真实（无偏）指数。
- 为了用无符号位段存有正有负的指数，存的是**偏置指数** \(E = e + \text{bias}\)。FP32 的 bias = 127。

如果你对「为什么 NPU 还需要浮点」有疑问：矩阵乘的累加结果是 INT32（在 VR 里），但**再量化**（rescale）时需要乘一个浮点 scale、再做激活（exp/erf）。这些都需要 FP32。NPU 不追求 IEEE 全精度，而是追求**面积小、时序短、够用**——这就是 Tier-2 取舍的来源。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|:---|:---|
| `src/main/scala/alu/vec/fp.scala` | **核心**。`IEEE754` 对象提供 FP32/BF16/BF8 全部组合逻辑；`FpRef` 对象提供测试用 Scala 参考模型。 |
| `src/main/scala/alu/vec/vec.scala` | VALU 主体。在 per-lane 循环里调用 `IEEE754.*` 得到各 FP 候选，再用 `MuxLookup` 选入 `out_vr`。 |
| `docs/implementations/VectorALU.md` | VALU 设计文档。含 Tier-2 约束表、FP 指令表、BF8 编码表。 |
| `src/test/scala/alu/vec/VALUFP32Spec.scala` | FP32 仿真测试，用 `java.lang.Float` 做参考，带 ULP（最低位单位）容差。 |

> 永久链接 base：`https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/`

---

## 4. 核心概念与源码讲解

### 4.1 FP32 字段编码与 Tier-2 设计取舍

#### 4.1.1 概念说明

FP32（IEEE 754 binary32）一个字 32 位，分三段：

```
 31 | 30 ........ 23 | 22 .................... 0
  s |    E (8 bit)    |        M (23 bit)        |
 符号 |    偏置指数     |       尾数小数部分        |
```

- 真实数值 \( = (-1)^s \times 1.M \times 2^{(E-127)} \)（\(E\in[1,254]\) 时为规格化数）。
- \(E=0\)：零或**非规格化数（subnormal）**；\(E=255\)：特殊值（±Inf / NaN）。

`IEEE754` 是 chisel-npu 里所有 FP32 运算的「积木箱」。它把上面三个字段的位置写成具名常量，再提供取字段、消毒（sanitize）、打包等基础原语，`fadd32`/`fmul32` 等运算都建立其上。

**Tier-2 设计取舍**（NPU 特有的简化，见文件头注释）：

| 属性 | 标准 IEEE 754 | chisel-npu Tier-2 | 动机 |
|:---|:---|:---|:---|
| 舍入 | 多种可选 | **RNE（最近偶）默认** | 简化硬件 |
| NaN | 会传播 | **当 0 处理，输出永不 NaN** | NPU 无需 NaN 语义 |
| ±Inf | 会传播 | **当 0 处理，溢出饱和到最大有限正/负数** | 避免无穷蔓延 |
| Subnormal | 支持 | **输入/输出都 FTZ（冲零）** | 省掉昂贵的非规格化处理逻辑 |
| 组合/寄存 | — | **纯组合**，VALU 外围加 1 拍寄存器 | 时序可控 |

> 直觉：标准浮点里最贵的就是「非规格化数处理」和「NaN/Inf 传播」。NPU 的输入是量化后的定点/整数，几乎不会出现这些病态值，所以 Tier-2 直接把它们冲零/饱和，换来了更小的面积和更短的组合路径。

#### 4.1.2 核心流程：取字段 → 消毒 → 打包

1. **取字段**：`sign`/`exp`/`man` 三个函数用固定的位边界切出符号、偏置指数、尾数小数。
2. **判零/判特殊**：`isZero` 只看 `exp === 0`（含 subnormal）；`isSpecial` 看 `exp === 0xFF`（Inf/NaN）。
3. **消毒 sanitize**：凡 `isSpecial || isZero` 的输入，一律替换成「保留符号位 + 其余 31 位清零」，即把它们变成带符号的 ±0。这就是 FTZ + NaN-as-0 的总入口。
4. **打包 pack**：把 `s/e/m` 拼回 32 位，**不做溢出检查**（溢出由各运算自行处理）。

#### 4.1.3 源码精读

字段常量（位边界与偏置）：

[fp.scala:L32-L42](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/fp.scala#L32-L42) 定义 `SIGN_BIT=31`、指数位段 `[30:23]`、尾数位段 `[22:0]`、`EXP_BIAS=127`，以及两个饱和目标 `MAX_FP32=0x7F7FFFFF`（最大有限正数）、`MIN_FP32=0xFF7FFFFF`（最大有限负数）。

取字段与判零/判特殊：

[fp.scala:L44-L57](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/fp.scala#L44-L57) —— `isZero(f)` 只判 `exp(f) === 0.U`，因此**非规格化数（exp==0 且 man!=0）也被当成零**，这正是 FTZ 在输入侧的实现。

消毒函数（FTZ + 特殊值冲零的总闸）：

[fp.scala:L60-L64](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/fp.scala#L60-L64) —— `sanitize` 把 Inf/NaN/subnormal/zero 统统变成 ±0；`pack` 不检查溢出。

VALU 如何把整段 VR lane 当 FP32 喂进来：

[vec.scala:L269-L282](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L269-L282) —— `fpA = aVR(31,0)`，把 4N 位的 VR lane 的低 32 位直接当 FP32 位模式，再调用 `IEEE754.fadd32(fpA, fpB)` 等，得到一堆 FP 候选结果。

设计文档中的 Tier-2 约束表：

[VectorALU.md:L280-L286](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/VectorALU.md#L280-L286) 列出 RNE/NaN/Inf/subnormal/overflow 五条 Tier-2 行为。

#### 4.1.4 代码实践：手算 FP32 值 1.0 的位模式

**实践目标**：把抽象的「符号/指数/尾数」落到一个具体的 32 位十六进制字，并验证它和 `FpRef` 的 `java.lang.Float` 参考一致。

**操作步骤**：

1. 写出 \(1.0\) 的规格化表示：\(1.0 = 1.0 \times 2^{0}\)。
2. 符号 \(s = 0\)（正数）。
3. 无偏指数 \(e = 0\)，偏置指数 \(E = e + 127 = 127 = \texttt{0b0111\_1111} = \texttt{0x7F}\)。
4. 尾数小数部分：\(1.M\) 中的 \(M = 0\)，故 23 位尾数全 0。
5. 拼接：\(0\_\texttt{01111111}\_\texttt{000...0}\) = `0x3F800000`。

**需要观察的现象 / 预期结果**：

- 用 Scala REPL 验证（**示例代码**，非项目原生命令）：
  ```scala
  scala> java.lang.Float.floatToRawIntBits(1.0f).toHexString
  res0: String = 3f800000
  ```
  与手算一致。
- 反过来：`Integer.parseUnsignedInt("3f800000", 16)` 再 `intBitsToFloat` 应得到 `1.0`。

**待本地验证**：在 `sbt console` 中 `import alu.vec.FpRef._` 后 `f32Bits(1.0f).toHexString` 是否也是 `3f800000`。

#### 4.1.5 小练习与答案

**练习 1**：写出 FP32 值 \(-2.5\) 的十六进制位模式。
**答案**：\(-2.5 = -1.25 \times 2^{1}\)，\(s=1\)，\(e=1\)，\(E=128=\texttt{0x80}\)，\(M=0.25\to 0.01_b\)，尾数 \(= \texttt{0x200000}\)。结果 `0xC0200000`。

**练习 2**：FP32 最小规格化正数是多少？它的指数字段值是多少？
**答案**：最小规格化正数 \(= 2^{-126} \approx 1.18\times10^{-38}\)，对应 \(E=1\)（\(e=1-127=-126\)）。注意 \(E=0\) 的是零或非规格化，Tier-2 下被冲零。

---

### 4.2 fadd32 与 fmul32：组合逻辑如何做浮点运算

#### 4.2.1 概念说明

浮点加法比定点加法复杂，因为它要先「对齐指数」再「加减尾数」：

\[
a \pm b = (-1)^{s_r} \times 1.M_r \times 2^{E_r}
\]

加法的核心难点是**对阶**（把小指数的操作数右移，使两个尾数对齐到同一指数）与**规格化**（加减后结果可能需要左移或右移，以重新满足「隐含前导 1」的形式）。乘法相对简单：指数相加、尾数相乘，再做一次规格化。

`fadd32`/`fmul32` 把这些步骤全用组合逻辑（`WireDefault` + `when`）实现，**没有寄存器**——VALU 在外围用 `RegNext` 给它们加 1 拍延迟。

#### 4.2.2 核心流程

**fadd32**（[fp.scala:L71-L165](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/fp.scala#L71-L165)）步骤：

1. **消毒**：`sanitize(a)`、`sanitize(b)`（FTZ + 特殊值冲零）。
2. **取件**：抽出符号、指数（pad 到 10 位**有符号**避免溢出）、尾数并补上隐含前导 1（24 位）。
3. **保证 |a| ≥ |b|**：比较指数（指数相同再比尾数），必要时交换，使 `hi` 是绝对值大者。
4. **对阶**：`shift = hiExp - loExp`，把小尾数右移 `shift` 位（封顶 25，超过即视为 0），得到 `loAligned`。
5. **加减**：同号相加；异号相减（大减小；若不够减则交换并翻转结果符号）。结果扩展到 25 位以捕获进位/借位。
6. **规格化**：用 `PriorityEncoder(Reverse(raw))` 找到 25 位结果 `raw` 的最高置位位 `hbit`：
   - 若 `hbit == 24`（加法进位）：尾数右移 1，指数 +1；
   - 若 `hbit ≥ 23`：右移 `hbit-23`；
   - 若 `hbit < 23`：左移 `23-hbit`；
   - 指数相应调整为 \(E_r = E_{hi} + (\text{hbit} - 23)\)。
7. **饱和**：`overflow`（\(E_r \ge 255\)）→ 输出 `MAX/MIN_FP32`；`underflow`（\(E_r \le 0\) 或 `raw==0`）→ 输出 ±0（输出侧 FTZ）。

> 关于 `PriorityEncoder(Reverse(raw))` 的直觉：把 25 位数倒过来后，「最低置位位」就是「从最高位往下数的第一个 1」，即前导零个数 `lzFromTop`；于是真正的前导 1 位置 `hbit = 24 − lzFromTop`。

**fmul32**（[fp.scala:L170-L212](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/fp.scala#L170-L212)）更短：

1. 消毒；结果符号 \(s_r = s_a \oplus s_b\)（异号得负）。
2. 指数相加减偏置：\(E_r = E_a + E_b - 127\)（减一次 bias，因为两个偏置指数各加了一次）。
3. 尾数相乘得 48 位积 `prod = aMan * bMan`（`aMan/bMan` 都是补了前导 1 的 24 位）。
4. 规格化：积的最高位在 bit 47 或 46，据此二选一截取 23 位尾数、调整指数。
5. 同样的 overflow/underflow 饱和（任一输入为零 → 结果 0）。

**FMA 家族**（[fp.scala:L219-L222](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/fp.scala#L219-L222)）：`fma32(a,b,c) = fadd32(fmul32(a,b), c)`，即**两次运算的串接**，并非真正单步融合（注释明确说明它只为「中间结果有限且规格化」的量化场景服务，保证约 1-ULP 精度）。`fms/nfma/nfms` 只是符号变体。

#### 4.2.3 源码精读

fadd32 的对阶与加减核心：

[fp.scala:L89-L111](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/fp.scala#L89-L111) —— 注意 `shiftCap` 把移位封顶 25，保证移过头的小操作数贡献为 0；异号且 `hiExt < loExt` 时翻转符号。

fadd32 的规格化（前导 1 检测）：

[fp.scala:L123-L149](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/fp.scala#L123-L149) —— `rawTop = raw(24)` 单独处理加法进位；其余情况用 `hbit` 与 23 的关系决定左移还是右移。

fadd32 的饱和收尾（输出侧 FTZ）：

[fp.scala:L152-L164](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/fp.scala#L152-L164) —— `underflow = (rExp <= 0.S) || (raw === 0.U)`，所以任何会落到 subnormal 区的结果都直接冲零。

fmul32 的尾数相乘与二选一规格化：

[fp.scala:L181-L198](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/fp.scala#L181-L198) —— `prod(47)` 置位时取 `prod(46,24)`、否则取 `prod(45,23)`。

VALU 端把 FP 候选选入 VR 输出：

[vec.scala:L393-L405](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L393-L405) —— `VecOp.vfadd/vfmul/vfma/...` 各自映射到对应的 `fpAdd/fpMul/fpFma`，经 `.pad(N4)` 送入 `selVR`，再由宽度门控写入 `out_vr`。

#### 4.2.4 代码实践：参考 VALUFP32Spec 设计一个 fadd 用例，并解释 FTZ 影响

**实践目标**：亲手照着项目测试的写法设计一条 `vfadd` 用例，验证硬件与 `java.lang.Float` 参考在 1-ULP 内一致；并构造一个会被 FTZ 吞掉的极小输入，观察它如何变成 0。

**操作步骤**：

1. 阅读 [VALUFP32Spec.scala:L61-L74](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUFP32Spec.scala#L61-L74)（`runVfadd`）和 [L34-L45](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUFP32Spec.scala#L34-L45)（`pokeFPLanes`）。
2. 仿照它，构造 K=8 的两路 `aArr/bArr`，用 `f32Bits(...)` 把 `Float` 转成位模式 `poke` 进 `in_a_vr/in_b_vr`。
3. `pokeCtrlFP(dut, VecOp.vfadd)` 把控制设成 FP32（注意它把 `regCls` 设为 2=VR，`dtype` 设为 `FP32C1`，见 [L21-L29](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUFP32Spec.scala#L21-L29)）。
4. `clock.step()` 一拍，`readVR` 读出 8 个 lane，用 `withinUlp`（[L51-L57](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUFP32Spec.scala#L51-L57)）与参考比对。

**关于 FTZ 影响的说明（必答）**：

把 `aArr(i)` 设成一个 subnormal 值，例如 `1e-40f`（其 FP32 指数字段 \(E=0\)、尾数非零）。在 `fadd32` 里，第一行 `sanitize` 会因 `isZero`（只看 `exp===0`）把它判为真，从而替换成 ±0。因此「`1.0f + 1e-40f`」在 Tier-2 硬件里得到的是 `1.0f`，而不是略大于 1 的某个值——**subnormal 贡献被 FTZ 丢弃了**。同理 `fmul32` 里两个极小但非零的数相乘，若结果指数落到 ≤0（underflow 分支），也会被冲成 ±0。

**预期结果 / 待本地验证**：正常 lane 在 1-ULP 内匹配参考；含 subnormal 的 lane 结果为干净的零或对侧操作数，而非参考模型给出的微小增量。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `fadd32` 把指数 pad 到 10 位**有符号**再参与运算？
**答案**：两数相加/减后指数可能进位（如 `hbit==24` 时 `E_r = E_hi + 1`），也可能因左移规格化而 `E_r = E_hi - shiftL` 出现负值。用 10 位有符号既容纳 ±127 的偏置指数范围，又留出进/借位余量，最后再用 `overflow/underflow` 比较（`rExp >= 255` / `<= 0`）统一饱和。

**练习 2**：`fmul32` 为什么 `rExpRaw = aExp + bExp - EXP_BIAS`，要减一次偏置？
**答案**：`aExp/bExp` 都已经是「真实指数 + 127」的偏置值，相加后偏置被加了两次（254），所以减去一次 127 还原成正确的偏置指数。

---

### 4.3 BF8 的两种变体 E4M3 / E5M2 与转换

#### 4.3.1 概念说明

**BF8** 是 8 位浮点（Brain-Float 8），为低精度训练/推理设计。一个 BF8 字 8 位，分符号/指数/尾数三段，但指数和尾数各占几位有两种主流分配：

| 变体 | 结构（位） | 指数偏置 | 典型用途 | 最大值（文档） |
|:---|:---|:---:|:---|:---:|
| **E4M3** | 1 符号 + 4 指数 + 3 尾数 | 7 | **激活**（动态范围适中、精度更高） | ≈ 448 |
| **E5M2** | 1 符号 + 5 指数 + 2 尾数 | 15 | **权重 / 梯度**（动态范围大、精度低） | ≈ 57 344 |

> 直觉：E4M3 尾数多一位 → 表示更精细；E5M2 指数多一位 → 动态范围更大。训练时梯度可能跨很多数量级，所以用 E5M2；激活数值相对集中，用 E4M3。在 chisel-npu 里，`funct7[6]` 这一位在 CVT 指令里选变体：`0`=E4M3，`1`=E5M2。

**BF16**（[fp.scala:L320-L327](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/fp.scala#L320-L327)）更简单：它就是 FP32 的高 16 位，符号与指数完全一致、尾数截断。FP32→BF16 做一次 RNE（加 `0x8000` 后截断），BF16→FP32 把低 16 位补零，几乎无损。

#### 4.3.2 核心流程：FP32 ↔ BF8

**FP32 → BF8 E4M3**（[fp.scala:L336-L355](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/fp.scala#L336-L355)）：

1. 消毒、取符号、算无偏指数 `fExp = exp - 127`、取尾数。
2. **重新偏置**到 E4M3 的 bias：`eRaw = fExp + 7`。
3. 若 `eRaw >= 15`：**溢出饱和**到最大正常值（`Cat(fSgn, 0b01111111)`）。
4. 若 `eRaw > 0`：打包 `Cat(符号, eRaw(3,0), FP32尾数高3位)` —— 直接截取 FP32 尾数的最高 3 位作为 BF8 尾数（截断，不四舍五入）。
5. 若 `eRaw <= 0`（含 subnormal）：**下溢冲零**。

**BF8 E4M3 → FP32**（[fp.scala:L358-L366](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/fp.scala#L358-L366)）：把 BF8 的指数减 7 再加回 FP32 的 127、尾数放高位低 20 位补零，零值特判。

E5M2 流程完全对称（[fp.scala:L369-L396](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/fp.scala#L369-L396)），区别只在位段：指数 5 位取 `fMan(22,21)`（高 2 位尾数）、偏置 15、溢出阈值 31。

**分派**（[fp.scala:L399-L402](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/fp.scala#L399-L402)）：`f32ToBf8(f, e5m2)` / `bf8ToF32(b, e5m2)` 用一个 `Bool` 选变体，省去两套 API。

#### 4.3.3 源码精读

VALU 端如何用 dtype 选 E5M2：

[vec.scala:L293-L295](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L293-L295) —— `isBf8E5M2 = (io.ctrl.dtype === VecDType.BF8E5M2)`，把这个 `Bool` 传给 `IEEE754.f32ToBf8/bf8ToF32` 做变体分派。这正是「`funct7[6]` 选变体」在硬件里的落点。

文档里的 BF8 变体表与选择位：

[VectorALU.md:L342-L349](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/VectorALU.md#L342-L349) —— 给出 E4M3/E5M2 的 S/Exp/Man/Bias/Max 表，并说明 `funct7[6]` 选择规则。

VALU CVT 指令总表（输入/输出端口、拍数）：

[VectorALU.md:L232-L245](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/VectorALU.md#L232-L245) —— `vcvt_f32_bf8`（BF8→FP32，VX→VR）与 `vcvt_bf8_f32`（FP32→BF8，VR→VX）的端口映射。

#### 4.3.4 代码实践：手算一个 FP32→BF8 E4M3 编码

**实践目标**：把「重新偏置 + 截尾数 + 饱和」这套规则手算一遍，建立对 BF8 动态范围的直觉。

**操作步骤**：

1. 取输入 FP32 = \(1.0\)（`0x3F800000`）：\(E=127\)，无偏指数 \(e = 0\)，尾数全 0。
2. 重新偏置：`eRaw = 0 + 7 = 7`，落在 \( (0, 15) \) 区间 → 正常打包。
3. 取 FP32 尾数高 3 位：`man(22,20) = 000`。
4. 打包：`Cat(0, 0111, 000)` = `0b0_0111_000` = `0x38`。

**需要观察的现象 / 预期结果**：

- BF8 E4M3 的 `0x38` 解码回去：\(E_{bf8}=7\)，\(e=7-7=0\)，\(1.M = 1.000\)，即 \(1.0 \times 2^0 = 1.0\)，**往返无损**。
- 用 `FpRef.f32ToBf8E4M3`（[fp.scala:L447-L459](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/fp.scala#L447-L459)）核对：输入 `0x3F800000` 应输出 `0x38`。
- 取一个超出 E4M3 范围的大数，例如 FP32 的 `1e4f`，观察它会撞到 `eRaw >= 15` 分支而被**饱和到最大正常值**。

**待本地验证**：在 `sbt console` 调 `FpRef.f32ToBf8E4M3(java.lang.Float.floatToRawIntBits(1.0f))` 是否得 `0x38`。

#### 4.3.5 小练习与答案

**练习 1**：同样是 8 位，E5M2 比 E4M3 多了什么、少了什么？
**答案**：E5M2 指数多 1 位（动态范围更大，偏置 15），但尾数少 1 位（精度更低）。这就是「权重大动态范围用 E5M2、激活要精度用 E4M3」的来源。

**练习 2**：`f32ToBf8E4M3` 里若 `eRaw <= 0` 直接输出 0，对应原值的什么情况？
**答案**：对应无偏指数 \(e \le -7\) 的 FP32 值（即 \(|x| < 2^{-7}\)），它们已超出 E4M3 的最小规格化范围，Tier-2 不支持 BF8 的非规格化数，直接冲零（下溢 FTZ）。

---

### 4.4 FpRef：Scala 参考模型与 bit-exact 测试

#### 4.4.1 概念说明

硬件 FP 运算好不好，需要一个「黄金答案」来比对。最权威、最省事的黄金答案就是 JVM 自带的 `java.lang.Float`（或 `Math.fma`）——它实现的是完整 IEEE 754。`FpRef` 对象把这些 JVM 调用薄封装成与 `IEEE754` 同名的函数（`fadd/fmul/fma`、`s32ToF32/f32ToS8`、`f32ToBf8E4M3` 等），供测试 spec 调用。

关键点：

- `FpRef` 是**纯 Scala**，不会被综合成硬件；它只活在 `src/main/scala` 是因为 Chisel 允许普通 Scala 对象与硬件对象共处一个源文件。
- 硬件 Tier-2 是 IEEE 子集（FTZ、无 NaN、溢出饱和），与完整 IEEE **不可能逐位相等**，所以测试用 **ULP 容差**而非严格相等。

#### 4.4.2 核心流程：位模式 ↔ Float 的互转

`FpRef` 的全部工作建立在两个 JVM 原语上（[fp.scala:L424-L425](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/fp.scala#L424-L425)）：

- `f32Bits(f)`：`Float → 32 位 int 位模式`（`floatToRawIntBits`）。
- `bitsF32(i)`：`32 位 int → Float`（`intBitsToFloat`）。

于是 `fadd(aBits, bBits) = f32Bits(bitsF32(aBits) + bitsF32(bBits))`——先把位模式还原成 Float，用 JVM 的 `+` 算，再转回位模式。`fma` 用 `Math.fma`（真正融合、单步舍入）。

测试侧（[VALUFP32Spec.scala:L51-L57](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUFP32Spec.scala#L51-L57)）定义 `withinUlp(hw, ref, ulp)`：把两边的位模式还原成 Float，比较差值是否 ≤ `ulp * Math.ulp(ref)`（`Math.ulp` 是该 Float 处的「最后一位单位」大小）。加/乘默认 2 ULP，FMA 因是两次运算放宽到 4 ULP（[L101](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUFP32Spec.scala#L101)）。

#### 4.4.3 源码精读

FpRef 对象（参考模型）：

[fp.scala:L421-L475](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/fp.scala#L421-L475) —— 注意 `f32ToS32` 直接用 Java 的 `(int)float` 强转（RTZ），与硬件 `f32ToS32RTZ` 语义对齐；`f32ToBf8E4M3`/`f32ToBf8E5M2` 用 `Math.getExponent` + `Math.round` 给出 BF8 编码的参考。

VALUFP32Spec 的溢出饱和用例：

[VALUFP32Spec.scala:L106-L120](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUFP32Spec.scala#L106-L120) —— `Float.MAX_VALUE * 2` 在 JVM 里是 `+Infinity`，但断言硬件输出 `!hw.isInfinite`，证明 Tier-2 把溢出**饱和到最大有限值**而非 Inf。这正是「参考模型会给出 Inf、硬件给饱和值」、二者在病态区间**故意不一致**的体现。

FP 指令映射表（funct3 → 操作）：

[VectorALU.md:L288-L296](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/VectorALU.md#L288-L296) —— `fadd/fsub/fmul/fneg/fabs/fmax/fmin` 均为 1 拍，全在 VR 上操作。

#### 4.4.4 代码实践：跟踪一个 vfadd 测试的「硬件 vs 参考」比对链

**实践目标**：把 spec 里从「构造输入 → poke → step → peek → withinUlp」的完整链路走一遍，理解 bit-exact 测试如何容纳 Tier-2 与 IEEE 的差异。

**操作步骤**：

1. 在 `runVfadd`（[L61-L74](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUFP32Spec.scala#L61-L74)）里，`expected = f32Bits(aArr(i) + bArr(i))` 是参考答案（用 Scala `Float` 加法）。
2. `pokeFPLanes`（[L34-L45](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUFP32Spec.scala#L34-L45)）把每个 `Float` 经 `f32Bits` 转成位模式 `poke` 进 `in_a_vr(i)/in_b_vr(i)`，并把不相关的 VX/VE 端口清零。
3. `clock.step()` 一拍后 `readVR` 读硬件 `out_vr(i)` 的位模式。
4. `withinUlp(result(i), expected)` 把两边都还原成 Float 比较。

**需要观察的现象 / 预期结果**：

- 普通数值（如 `1.0+2.0=3.0`）硬件与参考**逐位相等**（0 ULP 差）。
- 接近舍入边界的值可能差 1 ULP（RNE 实现细节差异），仍在容差内通过。
- 把某 lane 改成「极大数 ×2」会看到硬件饱和而参考给出 Inf——此时 `withinUlp` 的 `isInfinite` 分支**不会**让它们都通过（因为硬件不是 Inf），但 `runOverflowSat` 是单独断言 `!hw.isInfinite`，**主动承认**这种不一致。

**待本地验证**：运行 `tool/test-specific-spec.sh alu.vec.VALUFP32Spec`（单测，参见 u9-l2），观察 8 个 lane 的通过情况与单测耗时。

#### 4.4.5 小练习与答案

**练习 1**：为什么测试用 `withinUlp` 而不是 `hw == ref` 严格相等？
**答案**：硬件是 IEEE 子集（FTZ、非真正融合的 FMA、自定义 RNE 截断），在病态输入和舍入边界上不可能与完整 IEEE 逐位相等；用 ULP 容差承认「误差最多几个最低位」，既保护了 Tier-2 的合法差异，又能抓住真正的功能 bug。

**练习 2**：`FpRef.fma` 用 `Math.fma`（单步融合），但硬件 `fma32` 是两次运算。这对测试容差意味着什么？
**答案**：硬件多一次中间舍入，最多多 1 ULP 误差，所以 `runVfma` 把容差从默认的 2 ULP 放宽到 4 ULP（[L101](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/alu/vec/VALUFP32Spec.scala#L101)）。

---

## 5. 综合实践

**任务**：把本讲四个模块串成一条「INT8 → FP32 → BF8 → FP32」的量化-反量化-低精度存储微链，画出每步落在哪类寄存器、调哪个 `IEEE754` 函数。

要求：

1. 起点是 MMALU 累加器写在 VR 里的一个 INT32 值（比如 `+100`）。
2. 第 1 步用 `vcvt_s32_f32`（`IEEE754.s32ToF32`）把它转成 FP32，结果仍在 VR。
3. 第 2 步用 `vcvt_bf8_f32`（`IEEE754.f32ToBf8`）压成 BF8 E4M3，结果落到 VX（N 位）。
4. 第 3 步用 `vcvt_f32_bf8`（`IEEE754.bf8ToF32`）还原回 FP32，结果落回 VR。
5. 对照 [vec.scala:L285-L297](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L285-L297) 的 CVT 候选与 [VectorALU.md:L232-L245](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/VectorALU.md#L232-L245) 的端口表，说明每步的输入端口、输出端口与拍数。
6. 用 `FpRef` 手算：`100 → f32 → bf8(E4M3) → f32` 的位模式分别是什么？BF8 往返是否无损？把 `100` 换成 `450`（接近 E4M3 上限）再试一次，观察是否饱和。

**预期产出**：一张三步转换表（指令 / 调用函数 / 输入寄存器类 / 输出寄存器类 / 拍数）+ 一段对「BF8 往返误差与饱和」的简短结论。这是 u7-l1（量化流水线）的预热。

---

## 6. 本讲小结

- `fp.scala` 的 `IEEE754` 对象是 VALU 全部浮点能力的组合逻辑积木箱，**无寄存器**；VALU 在外围用 `RegNext` 加 1 拍延迟，故 FP 算术指令为 1 拍、FMA（两次运算串接）按 2 拍处理。
- **Tier-2 取舍**：RNE 默认、FTZ（输入/输出都冲零）、无 NaN（当 0 处理）、溢出饱和到 `±0x7F7FFFFF`。`sanitize` 是 FTZ+NaN-as-0 的总闸，`isZero` 只看 `exp===0`，故 subnormal 也被当零。
- `fadd32` 走「消毒→保证|a|≥|b|→对阶→加减→前导 1 规格化→饱和」六步；`fmul32` 走「符号异或→指数相加减偏置→尾数相乘→二选一规格化→饱和」。
- **BF8 两变体**：E4M3（1+4+3，bias 7，激活）与 E5M2（1+5+2，bias 15，权重/梯度），由 CVT 指令的 `funct7[6]` 选，硬件里落到 `isBf8E5M2 = dtype===BF8E5M2`；FP32→BF8 用「重新偏置 + 截高位尾数 + 上溢饱和/下溢冲零」。
- **BF16** 是 FP32 高 16 位别名，转换近似无损。
- `FpRef` 用 `java.lang.Float`/`Math.fma` 作黄金参考；测试用 **ULP 容差**（add/mul 2 ULP、fma 4 ULP）容纳 Tier-2 与完整 IEEE 的合法差异，对「溢出饱和 vs Inf」这类**故意的不一致**单独断言。

---

## 7. 下一步学习建议

- **u5-l4（水平归约与广播）**：`vsum`/`vrmax` 把多通道压成 VR 宽度广播，是 softmax 分母的来源，与本讲的 FP 通路互补。
- **u7-l1（后量化流水线）**：本讲的 `vcvt_f32_s32` / `vfma` / `vcvt_s8_f32` 正是 INT8 再量化的三步链；那里的 `NCoreBackendQuantSpec` 会用本讲的 `FpRef` 思路做端到端 bit-exact 校验。
- 继续阅读源码：`src/main/scala/alu/vec/vec.scala` 第 267–297 行的 FP/CVT 候选区，与 `VALUFP32Spec.scala` 的全部子用例，把「组合逻辑 → 1 拍寄存 → 写回 VR」这条链彻底走通。
