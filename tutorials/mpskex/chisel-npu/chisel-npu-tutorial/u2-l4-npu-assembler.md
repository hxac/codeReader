# Scala 汇编器 NpuAssembler

## 1. 本讲目标

前两讲（u2-l1、u2-l2）我们学会了「读」一条 32 位指令：知道 opcode 选家族、funct3 选子操作、funct7 带属性，也知道 funct7 里 width/round/sat/dtype 各占哪几个比特。但写测试、写示例时，我们不可能每次都手动算「VE 宽度、RTZ 舍入、饱和、FP 这四个属性拼成的 funct7 到底是几」。

本讲解决的是「**写**」的问题：如何用 Scala 程序生成一条合法的 32 位指令字。学完后你应当能够：

- 说清 `encR / encI / encS` 三个原语各自拼哪些字段、为什么都用 `Long` 中间量。
- 用 `f7 / f7Cvt` 把两类 funct7 属性打包成 7 位整数。
- 解释为什么所有助手都返回 Scala `Int`，而当 bit31 为 1 时它会是负数、必须用 `toLong & 0xFFFFFFFFL` 还原成无符号 32 位再 `.U`。
- 直接调用 `vadd(...)`、`vcvt_s8_f32(...)` 等命名助手生成指令字。

一句话定位：`NpuAssembler` 是 chisel-npu 仿真与端到端测试的「汇编器」，把人类可读的 `vadd(rd=1, rs1=2, rs2=3, width=VX)` 翻译成可以 `poke` 进硬件的 32 位字。

## 2. 前置知识

本讲承接 u2-l1（R/I/S 三种 32 位格式与 `InstrBits` 位段）和 u2-l2（13 个 opcode 家族、funct3/funct7 三层译码）。开始前请确认你已理解：

- **三种格式共享低位字段**：`opcode[6:0]`、`rd[11:7]`、`funct3[14:12]`、`rs1[19:15]` 在 R/I/S 三种格式里位置完全一致，区别只在高位 `[31:20]`。
- **funct7 是属性包**：普通 R 型用 width/round/sat/dtype 四个属性；CVT（类型转换）家族换用 src/sat/round/bf8 布局。两者位布局不同，不能混用。
- **三类寄存器 VX/VE/VR**：一条指令通过 funct7 的 width 字段选择操作哪类寄存器。
- **Scala `Int` 是 32 位有符号整数**：最高位（bit31）是符号位。这条常识是本讲「符号陷阱」一节的根因。

如果你对上述任何一点模糊，请先回到 u2-l1、u2-l2 复习。本讲不重复它们的细节，只讲「如何用 Scala 把这些位段拼起来」。

## 3. 本讲源码地图

| 文件 | 作用 |
|:---|:---|
| [src/main/scala/isa/NpuAssembler.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala) | **本讲主角**。一个 Scala `object`，提供常量、`encR/encI/encS`、`f7/f7Cvt` 和大量命名助手。 |
| [src/main/scala/isa/instrFormat.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala) | 位段常量 `InstrBits`、`VecWidth`/`VecRound` 枚举、`Funct7Attrs`/`CvtFunct7` 两个 case class。汇编器里的 `f7/f7Cvt` 就是它们 `encode` 方法的薄封装。 |
| [src/main/scala/isa/instSetArch.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instSetArch.scala) | `OpFamily` 枚举与每家族的 funct3 表。命名助手里的 `0x10`、`0x14` 等 opcode 数字就来自这里。 |
| [src/test/scala/isa/InstrDecoderSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala) | 汇编器的「真实用法范本」：用 `NpuAssembler` 构造指令，再 `poke` 进译码器验证字段。本讲的实践大量参照它的写法。 |

## 4. 核心概念与源码讲解

### 4.1 NpuAssembler 常量：把 funct7 属性「命名化」

#### 4.1.1 概念说明

u2-l2 讲过 funct7 是属性包，但要让 `f7(width=VX)` 这样的写法成立，必须先把 `VX` 定义成一个 Scala 名字。`NpuAssembler` 第一步就是给 funct7 的每个子段值取一个可读的名字：

- **width 选择**：`VX=0 / VE=1 / VR=2`，对应 funct7[1:0]。
- **舍入模式**：`RNE/RTZ/FLOOR/CEIL`，对应 funct7[3:2]。
- **数据类型大类**：`INT/FP/BF`，对应 funct7[6:5]。
- **CVT 格式码**：`S8/S16/S32/F32/BF16/BF8`，是 3 位 src/dst 格式选择（见 u2-l2 的 `FmtCode`）。

注意：这些是**普通的 Scala `Int` 常量**，不是 Chisel 硬件类型。它们只活在「生成指令字」的 Scala 世界里，elaborate 时不会变成硬件。

#### 4.1.2 核心流程

四个常量组的对应关系：

| 常量组 | 取值 | funct7 位段 | 含义 |
|:---|:---|:---:|:---|
| `VX/VE/VR` | 0 / 1 / 2 | [1:0] | 操作的寄存器类（width） |
| `RNE/RTZ/FLOOR/CEIL` | 0 / 1 / 2 / 3 | [3:2] | 舍入模式 |
| `INT/FP/BF` | 0 / 1 / 2 | [6:5] | 数据类型大类（dtype） |
| `S8/S16/S32/F32/BF16/BF8` | 0 / 1 / 2 / 3 / 4 / 5 | CVT 的 src/dst | 格式码 |

前三组服务于普通 R 型（`f7`）；第四组服务于 CVT 家族（`f7Cvt`）。

#### 4.1.3 源码精读

常量集中定义在文件开头的注释块之后：

[.src/main/scala/isa/NpuAssembler.scala:25-47](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L25-L47) 定义了上面四组常量，注释里直接标明了它们落在 funct7 的哪一段（如「Width selectors (funct7[1:0])」）。

这些数字并非凭空捏造，而是和硬件侧 `instrFormat.scala` 的枚举严格对齐。例如：

[.src/main/scala/isa/instrFormat.scala:72-77](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L72-L77) 把 `VecWidth` 定义为 `VX=Value(0) / VE=Value(1) / VR=Value(2)`，与汇编器的 `VX=0/VE=1/VR=2` 完全一致。汇编器只是把硬件枚举的值「镜像」成 Scala `Int`，便于在测试里直接当参数传。

#### 4.1.4 代码实践

1. **实践目标**：确认汇编器常量的值，并与 `instrFormat.scala` 的枚举对齐。
2. **操作步骤**：进入开发容器后启动 Scala REPL（详见 u1-l2 的构建方式），导入汇编器：

   ```scala
   // 在容器内执行：make container，然后 sbt console
   import isa.NpuAssembler._
   println(VX, VE, VR)        // (0, 1, 2)
   println(RNE, RTZ, FLOOR, CEIL)  // (0, 1, 2, 3)
   println(INT, FP, BF)       // (0, 1, 2)
   println(S8, S16, S32, F32, BF16, BF8)  // (0, 1, 2, 3, 4, 5)
   ```

3. **观察现象**：打印出的数字应与上表一致。
4. **预期结果**：`(0,1,2)`、`(0,1,2,3)`、`(0,1,2)`、`(0,1,2,3,4,5)`。
5. 上述值由源码常量直接给出，可在本地用 `sbt console` 验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `VX/VE/VR` 只取 0/1/2 而把 3 留空？

**参考答案**：3 是 funct7[1:0] 的保留值（u2-l2 的 `VecWidth.VW_RSV=3`）。译码器遇到 width=3 会判定为非法指令。汇编器不提供 `3` 这个具名常量，正是为了从源头避免生成非法 width。

**练习 2**：`BF8` 常量值为 5，但 funct7 里 BF8 还有 E4M3/E5M2 两种变体，这个变体由谁携带？

**参考答案**：由 CVT 家族的 funct7[6]（`bf8E5M2`）携带。`BF8=5` 只选定了「BF8 这种格式」，至于具体是哪种变体，要看 `f7Cvt` 的 `bf8E5M2` 参数（见 4.3）。

---

### 4.2 encR / encI / encS：三个位拼接原语

#### 4.2.1 概念说明

三种指令格式对应三个「位拼接原语」，它们做的事完全一样：把各字段按 RISC-V 位段左移到正确位置，再按位或（`|`）拼成一个 32 位字。区别只在于各自接收哪些字段：

- `encR`：R 型，接收 opcode/funct3/funct7/rd/rs1/rs2。
- `encI`：I 型，把 funct7+rs2 这 12 位替换成 12 位立即数 `imm`。
- `encS`：S 型（仅融合乘加 FMA 用），把 funct7 拆成 `rs3[31:27]` 和舍入位 `rnd[26:25]`。

#### 4.2.2 核心流程

三种格式的位段布局（承接 u2-l1）：

```
R-type  [funct7(7) | rs2(5) | rs1(5) | funct3(3) | rd(5) | opcode(7)]
I-type  [    imm[11:0](12) | rs1(5) | funct3(3) | rd(5) | opcode(7)]
S-type  [rs3(5)|rnd(2)| rs2(5) | rs1(5) | funct3(3) | rd(5) | opcode(7)]
```

三个原语的拼接伪代码（以 `encR` 为例）：

```
word = (opcode & 0x7F)            // [6:0]
     | ((rd     & 0x1F) << 7)     // [11:7]
     | ((funct3 & 0x7)  << 12)    // [14:12]
     | ((rs1    & 0x1F) << 15)    // [19:15]
     | ((rs2    & 0x1F) << 20)    // [24:20]
     | ((funct7 & 0x7F) << 25)    // [31:25]
```

`& 0x7F` / `& 0x1F` 这类掩码是「防呆」：即使你不小心传了超出位宽的值，也只截取低位，保证字段不越界污染相邻字段。

**为什么用 `Long` 中间量？** 这是本讲最重要的一个细节。Scala 的 `Int` 是 32 位有符号数，当 funct7 落在 bit[31:25] 且其最高位（bit6，即字的 bit31）为 1 时，整个 `word` 的 bit31 就是 1，`Int` 会把它解释成负数。如果全程用 `Int` 计算，左移到 bit25 以上时符号位会干扰结果。三个原语统一先转 `toLong`、在 64 位空间里或运算，最后再 `(w & 0xFFFFFFFFL).toInt` 截成 32 位返回。这样无论 bit31 是 0 还是 1，拼出来的「位模式」都正确。

#### 4.2.3 源码精读

`encR` 把六个字段拼成 R 型字，注意每行都 `.toLong`：

[.src/main/scala/isa/NpuAssembler.scala:59-68](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L59-L68) 这段代码做了三件事：每个字段先 `toLong` 再左移、按位或；用掩码限定每个字段的位宽；最后 `(w & 0xFFFFFFFFL).toInt` 把 64 位结果截成 32 位、以（可能是负的）`Int` 返回。注释里写明了「returns Long to avoid signed-int overflow at bit 31」的设计意图。

`encI` 用 12 位立即数替换 funct7+rs2 的高位：

[.src/main/scala/isa/NpuAssembler.scala:70-79](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L70-L79) 关键是 `imm12 = imm.toLong & 0xFFF`：只取立即数低 12 位，再放到 `[31:20]`。负立即数（如 `imm = -1`）会变成 `0xFFF`，从而把字的 bit31 置 1——这正是 4.4 节符号陷阱的来源。

`encS` 处理 FMA 的双高位字段（rs3 与 rnd）：

[.src/main/scala/isa/NpuAssembler.scala:81-91](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L81-L91) 与 `encR` 相比，funct7 的 7 位被拆成 `round`（2 位，[26:25]）和 `rs3`（5 位，[31:27]）。因为 S 型需要第四个操作数寄存器 rs3 来表达 `rd = rs1*rs2 ± rs3`。

#### 4.2.4 代码实践

1. **实践目标**：手算一条 `vadd` 的 R 型字，验证你对位段的理解。
2. **操作步骤**：`vadd(rd=0, rs1=1, rs2=2, width=VX)` 等价于 `encR(0x10, funct3=0, funct7=f7(VX), rd=0, rs1=1, rs2=2)`，其中 `f7(VX)=0`（见 4.3）。手动把每个字段填进位段表：

   ```
   [31:25] funct7 = 0000000   (0)
   [24:20] rs2    = 00010     (2)
   [19:15] rs1    = 00001     (1)
   [14:12] funct3 = 000
   [11:7]  rd     = 00000     (0)
   [6:0]   opcode = 0010000   (0x10)
   ```
3. **观察现象**：拼成 32 位 `0000 0000 0010 0000 1000 0000 0001 0000`。
4. **预期结果**：十六进制为 `0x00208010`。这个值由位段手填得出，可在 `sbt console` 里 `println(f"%08x".format(vadd(0,1,2,VX)))` 验证是否一致。
5. 待本地验证：实际 console 输出应打印 `00208010`。

#### 4.2.5 小练习与答案

**练习 1**：`encR` 里为什么每个字段都先 `.toLong`？如果全程用 `Int` 会出什么错？

**参考答案**：因为 funct7 落在字的高位 [31:25]。当 funct7 的 bit6 为 1 时，字的 bit31 为 1，`Int` 会把整个字当成负数，左移到高位时符号位会污染计算。先转 `Long`（64 位有符号，bit31 不是符号位）在更大空间里或运算，再 `& 0xFFFFFFFFL` 截断，能保证位模式正确。

**练习 2**：`encI` 为什么对 imm 做 `& 0xFFF`？

**参考答案**：I 型立即数只有 12 位（[31:20]）。`& 0xFFF` 只保留低 12 位，既支持负数（如 `-1` → `0xFFF`，符号扩展进 12 位全 1），又防止调用者传入超范围值污染高位字段。

**练习 3**：`encS` 与 `encR` 的参数列表差在哪？为什么？

**参考答案**：`encS` 多了 `rs3`，并把 `funct7` 换成了 `round`（2 位）。因为 FMA 需要 `rd = rs1*rs2 ± rs3` 这第四个寄存器操作数，必须占用 funct7 的高 5 位 [31:27] 给 rs3，剩下的 [26:25] 只够放 2 位舍入模式。

---

### 4.3 f7 / f7Cvt：打包两种 funct7 布局

#### 4.3.1 概念说明

u2-l2 强调过：普通 R 型和 CVT 家族的 funct7 **位布局不同**。`f7` 与 `f7Cvt` 就是分别打包这两种布局的两个小函数。它们各自就是 `instrFormat.scala` 里 `Funct7Attrs.encode` 和 `CvtFunct7.encode` 的薄封装——汇编器只是把 case class 调用改写成了带默认参数的函数，用起来更轻。

两种布局对比：

| 比特 | 普通 R 型（`f7`） | CVT（`f7Cvt`） |
|:---:|:---|:---|
| [1:0] | width（VX/VE/VR） | — |
| [2:0] | — | src 格式码（S8/S16/…/BF8） |
| [3:2] | round | — |
| [3] | — | sat（饱和） |
| [4] | sat | — |
| [5:4] | — | round |
| [6:5] | dtype（INT/FP/BF） | — |
| [6] | — | bf8 变体（0=E4M3, 1=E5M2） |

#### 4.3.2 核心流程

`f7` 的打包公式（对应 funct7[1:0]/[3:2]/[4]/[6:5]）：

\[ \text{funct7} = (\text{width}\,\&\,3) \;\big|\; ((\text{round}\,\&\,3)\ll 2) \;\big|\; (\text{sat}\ll 4) \;\big|\; ((\text{dtype}\,\&\,3)\ll 5) \]

`f7Cvt` 的打包公式（对应 funct7[2:0]/[3]/[5:4]/[6]）：

\[ \text{funct7}_{cvt} = (\text{src}\,\&\,7) \;\big|\; (\text{sat}\ll 3) \;\big|\; ((\text{round}\,\&\,3)\ll 4) \;\big|\; (\text{bf8E5M2}\ll 6) \]

可以看到同一个「sat」「round」语义在两种布局里位置完全不同，所以绝不能用 `f7` 去拼 CVT 指令、反之亦然。

#### 4.3.3 源码精读

`f7` 用默认参数让 width/round/sat/dtype 都可省略：

[.src/main/scala/isa/NpuAssembler.scala:51-53](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L51-L53) 这三行就是上面第一个公式的直译。`if (sat) 1 else 0` 把 Scala `Boolean` 转成 0/1 再左移到 bit4。

`f7Cvt` 用 CVT 专属的位段：

[.src/main/scala/isa/NpuAssembler.scala:55-57](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L55-L57) 注意 src 在 [2:0]（3 位，能表示 8 种格式）、sat 在 [3]、round 在 [5:4]、bf8 变体在 [6]。

它们与硬件侧 case class 的对应关系——`Funct7Attrs.encode` 与 `f7` 逐位相同：

[.src/main/scala/isa/instrFormat.scala:117-125](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L117-L125) `Funct7Attrs` 的 `encode` 方法与汇编器 `f7` 的表达式完全一致，只是 `f7` 把它包装成带默认参数的函数。`CvtFunct7.encode`（[同文件:127-135](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L127-L135)）同理对应 `f7Cvt`。

#### 4.3.4 代码实践

1. **实践目标**：手算一个带满属性的 funct7，体会属性如何落位。
2. **操作步骤**：在 `sbt console` 里执行：

   ```scala
   import isa.NpuAssembler._
   // 「VE 宽度、RTZ 舍入、饱和、FP」的 R 型 funct7
   println(f"%02x".format(f7(width=VE, round=RTZ, sat=true, dtype=FP)))
   // 「src=S32、饱和、RNE、E4M3」的 CVT funct7
   println(f"%02x".format(f7Cvt(srcFmt=S32, sat=true, round=RNE, bf8E5M2=false)))
   ```
3. **观察现象**：手算第一个：`f7(VE=1, RTZ=1, sat=true, FP=1) = 1 | (1<<2) | (1<<4) | (1<<5) = 1|4|16|32 = 53 = 0x35`。
4. **预期结果**：第一行打印 `35`。第二行 `f7Cvt(S32=2, sat=true, RNE=0, E4M3=false) = 2 | (1<<3) | 0 | 0 = 2|8 = 10 = 0x0a`，打印 `0a`。
5. 待本地验证：实际 console 输出应分别是 `35` 与 `0a`。

#### 4.3.5 小练习与答案

**练习 1**：如果误把 `f7(dtype=BF)` 用到 CVT 指令上，会发生什么？

**参考答案**：`f7` 把 dtype 放在 [6:5]，而 CVT 期望 [6:5] 是 round、[6] 是 bf8 变体。混用会让译码器把 dtype 的比特解释成完全不同的语义，导致 src 格式、舍入模式、BF8 变体全部错位，通常直接被判非法或算出错误结果。这正是需要两个独立函数 `f7/f7Cvt` 的原因。

**练习 2**：`f7Cvt` 里 `srcFmt & 7` 为什么用 `& 7` 而 `f7` 里 width 用 `& 3`？

**参考答案**：CVT 的 src 格式码占 3 位（[2:0]，8 种格式 S8…BF8+保留），所以掩码是 `& 7`；普通 width 只占 2 位（[1:0]，4 种 VX/VE/VR/保留），所以是 `& 3`。掩码宽度等于字段位宽。

---

### 4.4 命名助手与 Int → UInt 的符号陷阱

#### 4.4.1 概念说明

有了常量、原语和 funct7 打包器，最后一步是把它们组合成「一条指令」级别的命名助手，比如 `vadd(rd, rs1, rs2, width, sat)`。每个助手做的事情很简单：**填好该家族的 opcode 和 funct3，再调对应的原语**。例如 `vadd` 就是 `encR(0x10, funct3=0, f7(width, sat=sat), rd, rs1, rs2)`——它替你记住了「算术家族 opcode=0x10、加法 funct3=0」。

所有助手都返回 Scala `Int`（一个 32 位位模式）。这里有一个贯穿全测试套件的**符号陷阱**：当拼出的字 bit31 为 1 时（例如带负立即数的 I 型、或 dtype/imm 让高位为 1 的字），Scala `Int` 把它当成负数。直接 `.U` 进 Chisel 时，Chisel 会把负数字面量当成错误。因此 poke 前必须先用 `(instr.toLong & 0xFFFFFFFFL).U` 把它还原成无符号 32 位。这也是 AGENTS.md 反复强调的 gotcha。

#### 4.4.2 核心流程

命名助手与原语的关系（以 VALU_ARITH 家族为例）：

```
vadd(rd,rs1,rs2,width,sat)  ──► encR(opcode=0x10, funct3=0, f7(width,sat), rd,rs1,rs2)
vsub(...)                   ──► encR(opcode=0x10, funct3=1, ...)
...
vfma(rd,rs1,rs2,rs3,round)  ──► encS(opcode=0x17, funct3=0, rd,rs1,rs2,rs3,round)   // S 型
vcvt(rd,rs1,dstFmt,srcFmt)  ──► encR(opcode=0x14, dstFmt, f7Cvt(srcFmt,...), rd,rs1,0)
```

把 `Int` 安全喂进 Chisel 的两条等价路径：

```
路径 A（测试套件惯用）：  dut.io.instr.poke((instr.toLong & 0xFFFFFFFFL).U)
路径 B（汇编器自带的隐式类）：instr.asUInt   // 展开为 (instr.toLong & 0xFFFFFFFFL).U(32.W)
```

`NpuAssembler` 在文件末尾提供了一个 `IntToUInt` 隐式类来支持路径 B；但项目现有测试（如 `InstrDecoderSpec`）一律采用路径 A 的显式写法，本讲实践也遵循这一约定。

#### 4.4.3 源码精读

`vadd` 等算术助手都是 `encR` 的一行封装：

[.src/main/scala/isa/NpuAssembler.scala:99-100](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L99-L100) 这一行替你固定了 opcode=`0x10`（VALU_ARITH）和 funct3=`0`（ADD），只暴露语义参数 `rd/rs1/rs2/width/sat`。同家族的 `vsub…vrsub` 只是 funct3 从 0 递增到 7（[文件:99-114](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L99-L114)）。

CVT 助手用 `f7Cvt` 而非 `f7`，且 funct3 装的是 dst 格式码：

[.src/main/scala/isa/NpuAssembler.scala:158-162](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L158-L162) 注意 `vcvt` 把 `dstFmt` 放进 funct3、`srcFmt` 放进 `f7Cvt`。配套的便捷别名 `vcvt_s8_f32`（[文件:170](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L170)）命名规则是 `vcvt_<dst>_<src>`，即 `vcvt_s8_f32` 表示 **INT8→FP32**（窄输入、宽输出，结果落 VR）。

FMA 助手走 S 型原语 `encS`：

[.src/main/scala/isa/NpuAssembler.scala:207-208](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L207-L208) `vfma` 是少数用 `encS` 的助手，因为它要表达 `rd = rs1*rs2 + rs3`，需要 rs3 这个第四操作数。

符号陷阱的「官方解法」与隐式类：

[.src/main/scala/isa/NpuAssembler.scala:245-248](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L245-L248) `asUInt` 把 `Int` 经 `toLong & 0xFFFFFFFFL` 还原成无符号 32 位再 `.U(32.W)`。

测试套件实际怎么用（路径 A 的真实范例）：

[.src/test/scala/isa/InstrDecoderSpec.scala:25](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala#L25) `check` 助手里 `dut.io.instr.poke((instr.toLong & 0xFFFFFFFFL).U)`——这就是全项目喂指令字的标准姿势。`InstrDecoderSpec` 用 `vadd(rd=1, rs1=2, rs2=3, width=VX)` 构造指令、再校验译码字段（[文件:44-50](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala#L44-L50)）。

#### 4.4.4 代码实践（符号陷阱演示）

1. **实践目标**：亲眼看到 bit31=1 时 `Int` 变成负数，并验证 `toLong & 0xFFFFFFFFL` 能还原。
2. **操作步骤**：在 `sbt console` 里构造一条带负立即数的 `vmovi`：

   ```scala
   import isa.NpuAssembler._
   val w = vmovi(rd=0, imm=-1)        // imm=-1 → imm12=0xFFF → bit31=1
   println(w)                          // 一个负数
   println(f"%08x".format(w.toLong & 0xFFFFFFFFL))  // 还原成无符号 32 位
   ```
3. **观察现象**：`vmovi(0,-1)` = `encI(0x18, 1, 0, 0, -1)`，其中 `imm12 = (-1).toLong & 0xFFF = 0xFFF`，于是字的高 12 位 `[31:20]` 全 1，bit31=1。
4. **预期结果**：第一行打印一个**负整数**（bit31=1 所致）；第二行打印 `fff01018`（opcode `0x18` 与 funct3 `1<<12=0x1000` 都在低位）。把 `w` 直接 `.U` 会触发 Chisel 负数字面量错误，而 `(w.toLong & 0xFFFFFFFFL).U` 正常。
5. 待本地验证：实际 console 第一行的负数数值请本地确认；第二行的十六进制应为 `fff01018`。

#### 4.4.5 小练习与答案

**练习 1**：`vadd` 和 `vfadd` 都叫「加法」，它们走的原语和 funct7 有什么不同？

**参考答案**：`vadd` 走 `encR`、opcode `0x10`（VALU_ARITH）、`f7(width, …)` 带 width 属性，操作 VX/VE/VR 整数；`vfadd` 也走 `encR` 但 opcode `0x16`（VALU_FP）、`f7(VR, dtype=FP)`，width 固定 VR、dtype 固定 FP，做的是 FP32 浮点加法。

**练习 2**：为什么所有命名助手都返回 `Int` 而不是直接返回 Chisel `UInt`？

**参考答案**：汇编器是纯 Scala 工具，运行在 elaborate 之外的「测试/脚本」世界，那时还没有 Chisel 硬件上下文。返回 `Int`（位模式）最通用：既能在 `sbt console` 里直接打印，也能在测试里经 `toLong & 0xFFFFFFFFL` 转成 `UInt` poke 进硬件。返回 `UInt` 反而会绑定到 Chisel 上下文、失去纯计算的灵活性。

**练习 3**：`vcvt_s8_f32` 和 `vcvt_f32_s8` 哪个是「窄输出」？窄输出结果落到哪类寄存器？

**参考答案**：命名规则是 `vcvt_<dst>_<src>`。`vcvt_f32_s8` 是 FP32←INT8，dst 是 FP32（宽），这是「宽输出」落 VR；`vcvt_s8_f32` 是 INT8←FP32，dst 是 INT8（窄），是「窄输出」落 VX。（写回时序与 backend 的 `isNarrowCvtOut` 修正在 u6-l2 详讲。）

---

## 5. 综合实践

把本讲四个模块串起来，完成规格里要求的核心任务：**用命名助手 `vadd` 构造一条指令，再用 `encR + f7` 手动拼接验证两者一致**，并把指令喂进译码器。

**步骤 1 — 进入环境**。按 u1-l2 的方式进入开发容器并启动 REPL：

```bash
make container        # 进入 fangruil/chisel-dev 镜像，仓库挂在 /workspace
sbt console           # Scala REPL，main 源码已在 classpath 上
```

**步骤 2 — 用命名助手构造指令**。在 REPL 中：

```scala
import isa.NpuAssembler._
val a = vadd(rd=0, rs1=1, rs2=2, width=VX, sat=false)   // 「VX 宽度、不饱和」加法
println(f"%08x".format(a.toLong & 0xFFFFFFFFL))         // 打印 32 位十六进制
```

由 4.2.4 的位段手算，`vadd(0,1,2,VX)` 的 funct7=`f7(VX)=0`，整字应为 `0x00208010`。

**步骤 3 — 用 `encR + f7` 手动拼接同一条指令**：

```scala
val b = encR(opcode=0x10, funct3=0, funct7=f7(width=VX, sat=false), rd=0, rs1=1, rs2=2)
println(a == b)        // 期望 true
println(f"%08x".format(b.toLong & 0xFFFFFFFFL))   // 也应打印 00208010
```

**步骤 4 — （进阶）喂进译码器，参照真实测试**。退出 REPL，参考 [InstrDecoderSpec:44-50](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala#L44-L50) 的写法，确认这条字能被合法译码：

```scala
// 在一个 spec 里
import isa.NpuAssembler._
simulate(new InstrDecoder) { dut =>
  dut.io.instr.poke((vadd(rd=0, rs1=1, rs2=2, width=VX).toLong & 0xFFFFFFFFL).U)
  dut.clock.step(0)
  assert(!dut.io.illegal.peek().litToBoolean)   // 合法
  dut.io.decoded.rs1.expect(1.U)
  dut.io.decoded.rs2.expect(2.U)
}
```

可以用 `tool/test-specific-spec.sh isa.InInstrDecoderSpec`（u9-l2 会讲）单跑该 spec。

**预期结果**：步骤 2 和步骤 3 打印的都是 `00208010`，且 `a == b` 为 `true`。这同时验证了三件事：命名助手 `vadd` 确实只是 `encR` 的封装；你对手工位段的理解正确；`(x.toLong & 0xFFFFFFFFL)` 的符号还原对 bit31=0 的字同样无害。

> 待本地验证：以上十六进制结果由位段手算得出；实际 `sbt console` 与仿真输出请在本机容器内运行确认。

## 6. 本讲小结

- `NpuAssembler` 是纯 Scala 汇编器，把 `vadd(rd,rs1,rs2,width,sat)` 这类可读调用翻译成 32 位指令字，供仿真 `poke` 与端到端测试使用。
- 三个原语 `encR / encI / encS` 分别拼 R/I/S 三种格式；它们都用 `Long` 中间量并 `& 0xFFFFFFFFL` 截断，以规避 Scala `Int` 在 bit31 的符号溢出。
- `f7` 与 `f7Cvt` 打包两种**互不兼容**的 funct7 布局（普通 R 型 vs CVT），分别是 `Funct7Attrs.encode` / `CvtFunct7.encode` 的薄封装。
- 命名助手（`vadd`、`vcvt_s8_f32`、`vfma` 等）只是把家族的 opcode/funct3 固定好、再调对应原语；命名规则如 `vcvt_<dst>_<src>`。
- **关键 gotcha**：助手返回的 `Int` 在 bit31=1 时是负数，poke 前必须 `(instr.toLong & 0xFFFFFFFFL).U` 还原成无符号 32 位；项目测试一律采用这一显式写法。

## 7. 下一步学习建议

本讲让你掌握了「**生成**」指令字的能力。下一讲 **u2-l5（组合译码器 InstrDecoder）** 讲「**解析**」指令字——它正是本讲 `InstrDecoderSpec` 里那个把 32 位字变回 `DecodedMicroOp` 的模块。建议：

1. 直接进入 u2-l5，学习 `InstrDecoder` 如何反向切字段、如何判定 `illegal`（保留 opcode、非法 funct3、保留 width=3、CVT src==dst）。
2. 阅读 [src/test/scala/isa/InstrDecoderSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala) 全文，它既是本讲的练习底稿，也是 u2-l5 的最佳预习材料。
3. 进阶可继续到 u3-l1，看 `DecodedMicroOp` 如何拆成 `NCoreVALUBundle` / `NCoreMMALUCtrlBundle` 喂给计算单元——那时你会真正看到本讲生成的指令字如何驱动硬件。
