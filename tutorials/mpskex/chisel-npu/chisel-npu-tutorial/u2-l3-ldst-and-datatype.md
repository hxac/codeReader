# 装载存储指令与数据类型

## 1. 本讲目标

在 u2-l1、u2-l2 里，我们拆解了 32 位指令字的三层编码(opcode→funct3→funct7)，重点都在「计算型」指令上。本讲把目光转向另一类同样关键、但常常被初学者忽略的指令家族:**装载(LD)与存储(ST)**——也就是「数据怎么从外部存储搬进寄存器堆、算完又怎么搬出去」。

学完本讲，你应当能够:

- 说出 LD/ST 家族的 opcode 值，并解释它的 `funct3` 为何被用来表示**传输宽度**(而不是像 VALU 家族那样表示子操作)。
- 在 `dataType.scala` 里识别 `U8C4`、`S8C4`、`U16C2`、`S16C2` 等打包格式的命名规律，并画出它们在 32 位传输字里的字节布局。
- 读懂 `memMicroCode.scala` 里的 `MemLayout`、`MemChannel`、`MMUCtrlBundle` 三个访存微操作字段，理解它们为「矩阵 tile 搬运」预留的接口形态。
- 说出 NPU 为什么要把 4 个 INT8「打包」成一个 32 位传输单元——这是本讲的核心直觉。
- **清楚地区分**「已经在 RTL 里跑起来的部分」和「只定义了类型/接口、尚未接线的脚手架部分」，避免把设计意图当成已实现行为。

> ⚠️ 本讲会反复强调一个事实:`dataType.scala` 与 `memMicroCode.scala` 里的类型在当前 HEAD **尚未接入**译码器与后端，它们是「为未来内存子系统预留」的定义。本讲的价值正在于让你提前理解这些类型的设计意图，等它们被接通时不至于陌生。

## 2. 前置知识

本讲默认你已经掌握(u2-l1、u2-l2):

- **32 位指令字的三层编码**:opcode[6:0] 选家族、funct3[14:12] 选子操作、funct7[31:25] 带属性;`rd/rs1/rs2` 各 5 位。
- **三个全局参数**(u1-l4):\(N(\text{bits})=8\) 是基础通道位宽(等于一个 VX lane 的位宽)、\(L=32\) 是 VX 寄存器数量、\(K\) 是每寄存器的 SIMD 通道数(测试态 8、上板 64)。
- **三类寄存器 VX/VE/VR**(u1-l4):它们是同一块物理存储的三种「视图」，分别按 \(N\)、\(2N\)、\(4N\) 位宽解释 lane。
- **MMALU 是 K×K 脉动阵列**(u1-l1):矩阵乘的输入输出天然是「成块的 tile」，而不是单个标量。

此外，有两个朴素概念需要先建立直觉:

- **传输宽度(transfer width)**:访存指令一次读写多少位的「数据单元」。CPU 里 `lb/lw` 分别传 1 字节、4 字节;NPU 里同样需要告诉硬件「这次搬的是 8 位的 lane，还是一整条 K-lane 的向量」。
- **打包(packing)**:总线/存储器通常按固定宽度的「字(word)」工作。chisel-npu 的存储字宽是 32 位，但一次矩阵乘要用到大量 8 位数据(量化神经网络的权重和激活常见 INT8)。把多个小数据塞进一个 32 位字里一起搬，能成倍提升带宽利用率——这就是「打包」。

## 3. 本讲源码地图

| 文件 | 角色 | 当前实现状态 |
|:-----|:-----|:-----|
| [src/main/scala/isa/instSetArch.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instSetArch.scala) | 定义 `OpFamily.LD/ST` 与 `Funct3Mem`(传输宽度编码) | ✅ 已定义，译码器会读取 |
| [src/main/scala/isa/instrDecoder.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala) | 把 LD/ST 的 `funct3` 抽到 `DecodedMicroOp.mem_width` | ✅ 部分实现(仅抽出宽度字段) |
| [src/main/scala/isa/dataType.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/dataType.scala) | 打包数据类型 Bundle:`U8C4Input`、`S8C4Input`、`U16C2Input` 等 | 🟡 已定义，**尚未接线** |
| [src/main/scala/isa/micro_op/memMicroCode.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/memMicroCode.scala) | 访存微操作字段:`MemLayout`、`MemChannel`、`MMUCtrlBundle` | 🟡 已定义，**尚未接线** |
| [docs/designs/02.memory.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/designs/02.memory.md) | 内存模型设计设想(地址空间布局) | 🟡 标注 `[WIP]`，设计进行中 |

记住这张表里 🟡 的三行——它们是本讲的「主角」，但你要时刻清楚它们**还没有变成会跑的硬件**。

## 4. 核心概念与源码讲解

### 4.1 LD/ST 指令家族与 funct3 传输宽度编码

#### 4.1.1 概念说明

在 VALU 家族里，`funct3` 选的是「子操作」(ADD / SUB / MUL …)。但 LD/ST 家族不一样:它的「操作」本质上只有两个——读(LD)和写(ST)，已经由 opcode 区分了;于是 `funct3` 这 3 个比特被**重新利用**为「传输宽度」选择器，告诉硬件这一次访存搬的数据有多「宽」。

这是 RISC-V 的常见手法(`lb/lh/lw` 用 funct3 区分字节/半字/字)，chisel-npu 借用了同样的思路，但扩展出了 NPU 特有的「整向量传输」选项。

#### 4.1.2 核心流程

LD/ST 指令的译码流程:

1. 译码器看 `opcode`，识别为 `LD`(0x01)或 `ST`(0x02)家族。
2. 不再走 VALU 的 `(family, funct3) → VecOp` 大表，而是把 `funct3` 原样抽出，作为传输宽度。
3. 这个宽度值进入 `DecodedMicroOp.mem_width`，留给(未来的)内存子系统使用。

传输宽度编码共 6 种，分两组:

| `funct3` 值 | 名称 | 含义 | 在 N=8 时的物理宽度 |
|---:|:-----|:-----|:-----|
| 0 | `BYTE`  | 单个 N 位 lane(字节) | 8 位 |
| 1 | `HALF`  | 2N 位(半字) | 16 位 |
| 2 | `WORD`  | 4N 位(字) | 32 位 |
| 3 | `VX_VEC`| 整条 K-lane 的 VX 向量 | K×N 位(测试态 64 位) |
| 4 | `VE_VEC`| 整条 K-lane 的 VE 向量 | K×2N 位 |
| 5 | `VR_VEC`| 整条 K-lane 的 VR 向量 | K×4N 位 |
| 6,7 | (保留) | — | 命中即非法 |

注意一个关键区分:`BYTE/HALF/WORD` 描述的是**单个元素的位宽**，而 `VX_VEC/VE_VEC/VR_VEC` 描述的是**一整条向量寄存器**的成块搬运。前者像 CPU 的标量访存，后者是 NPU 特有的「向量级」批量访存——一次搬完整的一条 VX/VE/VR。

#### 4.1.3 源码精读

先看 opcode 家族的分配，LD/ST 各占一个值:

[src/main/scala/isa/instSetArch.scala:L32-L46](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instSetArch.scala#L32-L46) —— `OpFamily` 枚举，`LD=0x01`、`ST=0x02`，紧挨着 `MMA=0x03`，可见「访存」与「矩阵乘」在家族空间里被刻意排在相邻位置(它们在数据流上本就紧密耦合)。

再看 `funct3` 的传输宽度编码:

[src/main/scala/isa/instSetArch.scala:L168-L177](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instSetArch.scala#L168-L177) —— `Funct3Mem` 对象。注释里点明了 `BYTE` 是「N-bit lane」、`HALF` 是「2N-bit」、`WORD` 是「4N-bit」，与全局参数 N(bits) 直接挂钩;6、7 保留。

然后看译码器如何处理 LD/ST:

[src/main/scala/isa/instrDecoder.scala:L199](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L199) —— 注释 `// MMA, LD, ST, NOP: vecOp stays at default (not used)`。这是本讲最重要的诚实结论之一:**LD/ST 不会产生任何 VecOp**，译码器对它们的处理仅限于抽出宽度。

[src/main/scala/isa/instrDecoder.scala:L34](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L34) —— `DecodedMicroOp` 里有 `mem_width = UInt(3.W)` 字段，专门承载 LD/ST 的 funct3。

[src/main/scala/isa/instrDecoder.scala:L276](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L276) —— `io.decoded.mem_width := f3`，把 funct3 原样赋给 mem_width。注意此处**没有**对 funct3 做合法性检查(LD/ST 的保留值 6、7 当前不会被标为非法)，这也印证了 LD/ST 路径还在「预留」阶段。

> 顺带一提:`InstrDecoderSpec`(u2-l5)里没有任何针对 LD/ST 的测试用例，从侧面印证了这条路径尚未被功能验证覆盖。

#### 4.1.4 代码实践

**实践目标**:亲手把 LD/ST 的 funct3 译码逻辑「补画」出来，建立「funct3 当宽度用」的肌肉记忆。

**操作步骤**:

1. 打开 `src/main/scala/isa/instSetArch.scala`，找到 `Funct3Mem`，抄下 6 个有效值与注释里的位宽。
2. 打开 `src/main/scala/isa/instrDecoder.scala`，定位 `io.decoded.mem_width := f3` 这一行。
3. 假设 N=8、K=8，填一张表:把 `BYTE/HALF/WORD/VX_VEC/VE_VEC/VR_VEC` 各自对应的「物理位宽」算出来(提示:`VX_VEC` = K×N = 8×8 = 64 位)。

**需要观察的现象**:你会看到从 `BYTE`(8 位)到 `VR_VEC`(256 位)，单次访存的数据量跨了 32 倍。这正是 NPU 访存指令相对于 CPU 的「粗粒度」特征。

**预期结果**:BYTE=8、HALF=16、WORD=32、VX_VEC=64、VE_VEC=128、VR_VEC=256(单位:位)。若你的计算与之一致，说明你已理解 funct3 当宽度用的机制。

> 待本地验证:若你想在仿真里确认 LD/ST 不会被译码器判为非法，可参考 `InstrDecoderSpec.check` 的写法，构造一条 opcode=0x01 的指令 poke 进 `InstrDecoder`，断言 `io.illegal` 为 false、`io.decoded.mem_width` 等于你填入的 funct3。

#### 4.1.5 小练习与答案

**练习 1**:为什么 LD/ST 用 `funct3` 表示宽度，而不是像 VALU 那样表示子操作?

> **答案**:LD/ST 的「操作种类」已经被 opcode 区分(LD=读、ST=写)，只有两种，不需要 funct3 再选子操作;于是把 funct3 这 3 个比特省下来表示传输宽度，性价比更高。

**练习 2**:`Funct3Mem` 里有 6 个有效值(0..5)，值 6、7 是保留的。如果硬件收到一条 LD 指令、funct3=6，当前译码器会怎么处理?

> **答案**:当前 `instrDecoder.scala` 对 LD/ST 的 funct3 **不做**合法性检查(只把 `f3` 赋给 `mem_width`)，所以 funct3=6 既不会被标 illegal，也不会被特殊处理——它会原样传到 `mem_width`。这正是「LD/ST 尚未完整实现」的体现;真正接通内存子系统后，这两个保留值应当被判为非法。

**练习 3**:在 N=8、K=8 下，`VX_VEC` 一次搬运 64 位、等于 8 个 INT8。这和接下来要讲的 `U8C4Input`(32 位里装 4 个 INT8)是什么关系?

> **答案**:`VX_VEC` 是「整条向量」级别的搬运(64 位 = 一整条 VX)，而 `U8C4Input` 描述的是「单个 32 位存储字」内部如何解释(装 4 个 INT8)。前者是访存指令的粒度，后者是存储字内的数据格式——两者是不同层面的概念，下一节会讲清楚。

---

### 4.2 打包数据类型 Bundle(dataType.scala)

#### 4.2.1 概念说明

上一节我们看到，`WORD` 一次传 32 位。但「32 位里装的是什么」并没有指定——它可能是 4 个 INT8，也可能是 1 个 FP32，还可能是 2 个 BF16。`dataType.scala` 就是用来回答这个问题的:它定义了一组 Chisel `Bundle`，每种 Bundle 描述「一个 32 位存储字如何被切成若干 lane」。

这种「把多个小数据塞进一个字」的做法叫**打包(packing)**，是 NPU 提升带宽利用率的根本手段。命名规律是:

\[ \underbrace{\text{U/S/FP/BF}}_{\text{数据类型}}\;\underbrace{\text{8}}_{\text{每元素位数 W}}\;\underbrace{\text{C4}}_{\text{元素个数 C}} \quad\text{且}\quad W \times C = 32 \]

即每个 Bundle 的所有字段加起来**恰好 32 位**，与存储字宽对齐。

#### 4.2.2 核心流程

一个 32 位存储字可以被解释为不同打包格式，打包密度(每个字装几个元素)为:

\[
C = \frac{32}{W}
\]

其中 W 是单个元素的位数。常见组合:

| 类型 | W | C | 32 位字内的布局 | 典型用途 |
|:-----|---:|---:|:-----|:-----|
| `U8C4` / `S8C4` | 8 | 4 | `[c3 c2 c1 c0]` 各 8 位 | INT8 量化权重/激活 |
| `U16C2` / `S16C2` | 16 | 2 | `[c1 c0]` 各 16 位 | INT16 中间结果 |
| `FP16C2` / `BF16C2` | 16 | 2 | `[c1 c0]` 各 16 位 | 半精度/脑浮点 |
| `U32C1` / `S32C1` / `FP32C1` | 32 | 1 | `[c0]` 32 位 | INT32 累加结果 / FP32 |

> 注意:在 Chisel 的 `Bundle` 里，**先声明的字段位于低位**。所以 `U8C4Input` 里先声明的 `u8c0` 是最低 8 位，`u8c3` 是最高 8 位。这一点在画字节布局时尤其重要。

#### 4.2.3 源码精读

整份文件只有类型定义，没有任何逻辑——典型的「数据格式声明」文件:

[src/main/scala/isa/dataType.scala:L3](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/dataType.scala#L3) —— 注意包名是 `package isa.dtype`(独立子包)，使用时需 `import isa.dtype._`。

INT8 打包格式(本节主角，对应「为什么打包 4 个 INT8」):

[src/main/scala/isa/dataType.scala:L7-L12](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/dataType.scala#L7-L12) —— `U8C4Input`:4 个 `UInt(8.W)` 字段，无符号 INT8。

[src/main/scala/isa/dataType.scala:L14-L19](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/dataType.scala#L14-L19) —— `S8C4Input`:4 个 `SInt(8.W)` 字段，有符号 INT8。

INT16 打包格式:

[src/main/scala/isa/dataType.scala:L21-L24](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/dataType.scala#L21-L24) —— `U16C2Input`:2 个 `UInt(16.W)`。

[src/main/scala/isa/dataType.scala:L26-L29](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/dataType.scala#L26-L29) —— `S16C2Input`:2 个 `UInt(16.W)`。

> ⚠️ **源码阅读提醒**:`S16C2Input` 的两个字段被声明为 `UInt(16.W)` 而非 `SInt(16.W)`，与类名里的 `S`(signed)不一致;同样地，`S32C1Input` 的字段名叫 `u32c0` 却是 `SInt(32.W)`。结合这些类型尚未接线的现状，这更像是**占位/未完成**的实现痕迹，而非最终设计。阅读时以「设计意图(类名)」理解语义，但别忘了「当前代码(字段类型)」与之有出入。

16 位浮点与 32 位格式:

[src/main/scala/isa/dataType.scala:L31-L39](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/dataType.scala#L31-L39) —— `FP16C2Input` 与 `BF16C2Input`:各 2 个 `UInt(16.W)`(浮点位模式当无符号比特搬运)。

[src/main/scala/isa/dataType.scala:L41-L51](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/dataType.scala#L41-L51) —— `U32C1Input`/`S32C1Input`/`FP32C1Input`:单个 32 位字段，即整个字就是一个元素。注意这三个的字段名都叫 `u32c0`(包括 S32 与 FP32)，再次印证文件处于「脚手架」阶段。

最后强调一句:**当前全仓库没有任何地方 `import isa.dtype`**(可在仓库内搜索 `U8C4Input` 验证)，这些 Bundle 还没被任何模块实例化使用。

#### 4.2.4 代码实践

**实践目标**:画出 `U8C4Input` 与 `S16C2Input` 的字节布局图，并量化「打包 4 个 INT8」带来的带宽收益。

**操作步骤**:

1. 打开 `dataType.scala`，对着 `U8C4Input` 的字段声明画出 32 位字的布局。记住 Chisel 规则:**先声明 = 低位**。
2. 同样画出 `S16C2Input` 的布局(注意它是两个 16 位字段)。
3. 做一个简单计算:假设存储带宽是每拍 32 位，要搬 64 个 INT8 数据。不打包(每次搬 1 个 INT8)需要多少拍?打包成 `U8C4`(每次搬 4 个)需要多少拍?

**需要观察的现象 / 预期结果**:

`U8C4Input` 的字节布局(位 31 → 位 0):

```
  bit31..24   bit23..16   bit15..8    bit7..0
 ┌──────────┬──────────┬──────────┬──────────┐
 │   u8c3   │   u8c2   │   u8c1   │   u8c0   │
 └──────────┴──────────┴──────────┴──────────┘
```

`S16C2Input` 的字节布局:

```
       bit31..16            bit15..0
 ┌───────────────┬───────────────┐
 │    s16c1      │     s16c0     │   (注意:源码里是 UInt(16.W))
 └───────────────┴───────────────┘
```

带宽计算:搬 64 个 INT8，不打包需 64 拍，打包成 `U8C4` 只需 \(64 / 4 = 16\) 拍——**4 倍加速**。这就是打包的根本动机。

> 待本地验证(可选):进 `sbt console` 执行 `import chisel3._; import isa.dtype._; println((new U8C4Input).getWidth)`，应打印 `32`，确认所有字段加起来确实是 32 位。若环境里没有这些类(取决于源码是否纳入默认编译)，则标注为待本地验证。

#### 4.2.5 小练习与答案

**练习 1**:命名 `U8C4` 里的 `U`、`8`、`C4` 分别代表什么?一个 `U8C4Input` 占多少位?

> **答案**:`U`=Unsigned(无符号)、`8`=每个元素 8 位、`C4`=4 个元素(Count 4)。共 \(8 \times 4 = 32\) 位。

**练习 2**:如果要新增一个「4 个有符号 8 位」的类型，应该参照哪个已有类?为什么 `S8C4Input` 的字段用 `SInt` 而 `S16C2Input` 用 `UInt`?

> **答案**:应参照 `S8C4Input`(它正是 4 个有符号 8 位)。`S8C4Input` 用 `SInt(8.W)` 是正确的有符号声明;`S16C2Input` 用 `UInt(16.W)` 与其类名的 signed 语义不符，属于前文提到的脚手架不一致——以 `S8C4Input` 为「正确范本」。

**练习 3**:为什么 chisel-npu 的存储字宽恰好选 32 位，而不是 64 位或 128 位?

> **答案**:32 位是一个平衡点:既能让 4 个 INT8 恰好打包成一字(与量化网络最常见的 INT8 数据对齐)，又与 32 位累加结果(MMALU 的 INT32 输出)、FP32 单精度对齐。更宽的字(64/128)会成倍增加寄存器堆/总线位宽，在边缘 NPU 的面积功耗约束下不划算。更宽的并行度由「向量级」(VX_VEC 等)和「K 通道」来提供，而不是靠加宽单个字。

---

### 4.3 访存微操作字段(memMicroCode.scala)

#### 4.3.1 概念说明

`dataType.scala` 回答了「字内数据怎么解释」，`memMicroCode.scala` 则回答「访存控制信号长什么样」。它定义了三样东西:

- **`MemLayout`**:传输粒度(8/16/32 位)的枚举——是上一节打包宽度的「硬件侧」对应物。
- **`MemChannel`**:在一个 32 位字内，「子通道」选择——决定访问字内的哪一段。
- **`MMUCtrlBundle`**:**内存管理单元(MMU)控制 Bundle**，最关键的一项，为「矩阵 tile 搬运」预留了一组地址向量。

#### 4.3.2 核心流程

**MemLayout ↔ 子通道**:`MemChannel` 的注释揭示了一套「字内字节寻址」机制。一个 32 位字有 4 个字节位置(ch0..ch3)。按传输粒度不同，可用的子通道也不同:

\[
\text{可用子通道数} = \frac{32}{\text{传输粒度}}
\]

| `MemLayout` | 粒度 | 可用 `MemChannel` | 含义 |
|:-----|---:|:-----|:-----|
| `bit8`  | 8 位  | ch0, ch1, ch2, ch3 | 字内的 4 个字节都可单独访问 |
| `bit16` | 16 位 | ch0, ch2          | 两个半字位置(ch1/ch3 不存在) |
| `bit32` | 32 位 | ch0               | 整字，只有 ch0 |

这正是源码注释「16/32 bits will have no ch1」「32 bits will have no ch2」「16/32 bits will have no ch3」的含义。

**MMUCtrlBundle 的矩阵地址**:矩阵乘需要把 A、B 两个矩阵的 tile 搬进阵列，再把 C 矩阵的 tile 搬出去。对一个 n×n 阵列，输入和输出各需要 \(n^2\) 个地址。所以 `MMUCtrlBundle` 用 `Vec(n*n, UInt(...))` 同时携带 \(n^2\) 个 `in_addr` 和 \(n^2\) 个 `out_addr`——一次控制信号就能调度整批 tile 的搬运。

#### 4.3.3 源码精读

传输粒度枚举:

[src/main/scala/isa/micro_op/memMicroCode.scala:L7-L11](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/memMicroCode.scala#L7-L11) —— `MemLayout`:`bit8/bit16/bit32`，对应 8/16/32 位三种粒度。可看作 `Funct3Mem` 的 BYTE/HALF/WORD 在硬件控制侧的镜像。

子通道枚举(关键是注释):

[src/main/scala/isa/micro_op/memMicroCode.scala:L13-L21](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/memMicroCode.scala#L13-L21) —— `MemChannel`:ch0..ch3。每一行的注释写明了哪些粒度下「没有」该通道，构成上一节那张子通道表。

矩阵 tile 搬运控制 Bundle:

[src/main/scala/isa/micro_op/memMicroCode.scala:L23-L28](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/memMicroCode.scala#L23-L28) —— `MMUCtrlBundle(n=8, size=4096)`:

- `offset_keep: Bool`、`h_only: Bool`:两个控制位(从命名推测与「保留偏移」「仅半字」相关，尚未有文档明确语义，理解为待确认)。
- `in_addr: Vec(n*n, UInt(log2Ceil(size).W))`:\(n^2\) 个输入地址。默认 \(n=8\) → 64 个地址;`size=4096` → 每地址 \( \lceil \log_2 4096 \rceil = 12\) 位，共 \(64 \times 12 = 768\) 位。
- `out_addr: Vec(n*n, ...)`:同上，\(n^2\) 个输出地址。

把这组地址数(64 个)与 MMALU 的 n×n PE 数(n=8 时 64 个)对照，就能体会设计意图:一个 `MMUCtrlBundle` 把「整批矩阵 tile 的来源/去处地址」一气呵成地交给未来的 DMA/SPM(便签存储器)子系统。这正是 u1-l1 提到的「NPU 访存以 tile 为单位」在控制接口上的具象化。

> 同样要诚实指出:`MMUCtrlBundle` 当前也未被任何模块实例化(全仓库除本文件外无引用)，它是为 u8-l2/u8-l3 将要讲的「XDMA/Python 驱动 stage→kick→wait→collect」那套 tile 搬运流程预留的 RTL 侧接口。

#### 4.3.4 代码实践

**实践目标**:量化 `MMUCtrlBundle` 的规模，体会「矩阵 tile 搬运」控制信号有多宽。

**操作步骤**:

1. 读 `MMUCtrlBundle` 的参数默认值 `n=8, size=4096`。
2. 计算:`log2Ceil(4096)` 等于多少?`n*n` 等于多少?`in_addr` 这一个字段占多少位?加上 `out_addr` 后，两个地址向量共多少位?
3. 把算出来的总位宽，和一条 32 位指令字对比，说说为什么这种「批量地址」不能塞进普通指令里、而必须走单独的控制 Bundle。

**需要观察的现象 / 预期结果**:

- `log2Ceil(4096) = 12`(因为 \(2^{12} = 4096\))。
- `n*n = 8*8 = 64`。
- 单个 `in_addr`: \(64 \times 12 = 768\) 位;`in_addr + out_addr` 合计 \(768 \times 2 = 1536\) 位。

**结论**:1536 位的控制信号远远超过 32 位指令字，根本不可能编码进普通 LD/ST 指令。所以矩阵 tile 的搬运必须由专门的(配置型)控制结构 `MMUCtrlBundle` 来承载，LD/ST 指令本身只负责「发起一次传输」这种轻量语义。这种「瘦指令 + 胖控制结构」的分工，是 NPU 区别于 CPU 的典型架构特征。

> 待本地验证:若想确认位宽推算，可在 `sbt console` 里 `import chisel3._; import isa.micro_op._; println((new MMUCtrlBundle(n=8, size=4096)).getWidth)`，期望打印 `1538`(1536 位地址 + `offset_keep` 1 位 + `h_only` 1 位)。

#### 4.3.5 小练习与答案

**练习 1**:`MemLayout` 与 `Funct3Mem` 都描述了「8/16/32 位」三种宽度，它们是重复的吗?

> **答案**:不是简单的重复，而是同一概念在两个层面的镜像。`Funct3Mem` 在**指令编码层**用 funct3 的值(BYTE/HALF/WORD)告诉译码器宽度;`MemLayout` 在**硬件控制层**用枚举值(bit8/bit16/bit32)供 MMU 控制逻辑使用。前者面向程序员/汇编器，后者面向 RTL 内部连线。

**练习 2**:`MMUCtrlBundle` 为什么用 `Vec(n*n, ...)` 而不是单个地址?把 n 从 8 改成 32，地址位宽会变吗?

> **答案**:因为矩阵乘一次要调度 \(n \times n\) 个 tile 的输入和输出，每个 tile 一个地址，所以是 `Vec(n*n, ...)`。把 n 从 8 改到 32:地址**个数**从 64 变成 1024(`n*n` 变化)，但**每个地址的位宽** `log2Ceil(size)` 只取决于 `size`(4096→12 位)，与 n 无关——只要 `size` 不变，单地址位宽不变。

**练习 3**:`MemChannel` 的 ch0..ch3 在 `bit32` 粒度下只有 ch0 可用。这是否意味着 32 位传输「浪费」了 ch1/ch2/ch3?

> **答案**:不是浪费，而是语义不同。`bit32` 一次搬整个 32 位字，字内不再细分，所以只有 ch0(整字)一个位置;ch1..ch3 是为更细粒度(`bit8`/`bit16`)的字内寻址准备的。粒度越细，可寻址的子通道越多;粒度越粗，子通道越少但单次数据量越大。

---

### 4.4 内存模型设计设想(docs/designs/02.memory.md)

#### 4.4.1 概念说明

前三个模块讲的都是「指令与类型」，本节上升到一个尚未定型的设计文档。`02.memory.md` 标题就标了 `[WIP]`(Work In Progress)，它描述的是 chisel-npu **打算**怎么组织地址空间——目前还停留在设想阶段，很多地址都是 `TBD`(待定)。

读这份文档的目的不是背诵它的结论(它还没有结论)，而是理解 NPU 内存设计的**出发点**:为什么 NPU 的存储层次和 CPU 不一样。

#### 4.4.2 核心流程

文档提出了两个核心设想:

1. **共享 L2 + 大容量便签存储器(SPM)**:NPU 与 CPU 共享 L2 缓存(加速 DDR 的 DMA 访问)，同时 NPU 自己带一块大的 SPM 用来存中间结果。这与 GPU 的「全局内存 + 共享内存」思路一脉相承(文档明确引用了 NVIDIA PTX ISA)。
2. **哈佛式(Harvard-ish)的从设备架构**:NPU 作为 CPU 的从设备(slave)，指令(.code)与数据(.data)分离，每个 Processing Core 有自己的寄存器/代码/数据空间。

地址空间被划分为若干命名段:

| 段名 | 含义 | 访问范围 |
|:-----|:-----|:-----|
| `.reg` | 每个 Core 的寄存器 | 内部 R/W、外部 R/W |
| `.sreg` | 所有 Core 共享的寄存器(如 flag、PC) | 内部 RO、外部 RO |
| `.code` | 每个 Core 的代码 | R/W |
| `.data` | 每个 Core 的数据 | R/W |
| `.sdata` | 全局共享数据 | R/W |

(以上地址均标注 `TBD`，尚未固化。)

#### 4.4.3 源码精读

文档开宗明义标注进度:

[docs/designs/02.memory.md:L1](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/designs/02.memory.md#L1) —— 标题 `# [WIP] Memory`，提示读者这是进行中的设计。

设计动机:

[docs/designs/02.memory.md:L3-L5](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/designs/02.memory.md#L3-L5) —— 点明 NPU 的「memory wall」难题:SIMD/SIMT 架构在处理单元与存储之间有高内存墙;作者的方案是集成式 SoC——共享 L2(加速 DMA)+ 大 SPM(存中间结果)，并参考 NVIDIA PTX 的状态空间设计。

地址空间布局表:

[docs/designs/02.memory.md:L9-L19](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/designs/02.memory.md#L9-L19) —— Memory Address Layout 表。注意 `Address` 列全是 `TBD`，说明具体地址尚未分配;`Internal/External Access` 列区分了「Core 自己能不能访问」与「CPU/外部能不能访问」，这是从设备架构的关键。

寄存器设想:

[docs/designs/02.memory.md:L20-L31](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/designs/02.memory.md#L20-L31) —— 设想了 `.reg.ax/bx/cx/dx`(每 Core 的 32 位向量寄存器)与 `.sreg.fl/pc`(共享的 flag 与程序计数器)。注意这里出现了 `.reg.ax` 这种命名，与已实现的 VX/VE/VR 三类寄存器(u1-l4、u3-l2)还**没有对上号**——文档设想与当前 RTL 之间存在落差，再次印证内存子系统尚在演进。

#### 4.4.4 代码实践

**实践目标**:在 `02.memory.md` 里定位 LD/ST 传输宽度的「设想语境」，并据此写一段「为什么 NPU 要把 4 个 INT8 打包成一个传输单元」的说明。

**操作步骤**:

1. 打开 `docs/designs/02.memory.md`，通读全文(很短)。
2. 找到「memory wall」「SPM」「L2」「Harvard-ish」这几个关键词，理解 NPU 存储层次的动机。
3. 结合本讲 4.2 节的打包密度公式 \(C = 32/W\)，写一段 3–5 句的说明，解释「4 个 INT8 打包」如何缓解 memory wall。

**需要观察的现象 / 预期结果**:

阅读后你会发现，文档的核心痛点是「处理单元与存储之间的内存墙」。你的说明应当包含以下要点(参考答案):

> NPU 的计算单元(MMALU/VALU)每拍能消耗大量数据，但存储带宽受限于 DDR 与总线。量化神经网络最常见的数据是 INT8，若每次只搬 1 个 INT8(8 位)而总线/字宽是 32 位，就有 24 位被浪费，带宽利用率仅 25%。把 4 个 INT8 打包成一个 32 位字(`U8C4`)，一次搬运的数据量提升 4 倍，带宽利用率回到 100%。这样在同样的总线宽度下，能喂饱计算单元、缓解 memory wall——这正是 `dataType.scala` 定义 `U8C4Input`、`Funct3Mem` 提供 `VX_VEC` 等成块搬运选项的根本动机。

> 待确认:`02.memory.md` 并未直接写明「传输宽度=funct3」的最终编码约定(那是 `Funct3Mem` 的职责);文档只给出地址空间设想。若你期待在文档里找到 LD/ST 宽度的明文规定，请以 `instSetArch.scala` 的 `Funct3Mem` 为准。

#### 4.4.5 小练习与答案

**练习 1**:`02.memory.md` 提到 NPU 与 CPU「共享 L2」并自带「大 SPM」。为什么 NPU 需要自己的 SPM，而不能全靠 L2 缓存?

> **答案**:NPU 的中间结果(矩阵乘的部分和、激活前的累加值)访问模式高度规则、可预测，且数据量巨大。L2 缓存是为 CPU 不规则访问优化的(带替换策略、一致性协议)，用它装 NPU 中间结果会污染缓存、效率低。SPM(便签存储器)由软件显式管理、无缓存开销，专门放这些规则的大块中间数据，能提供稳定的高带宽。

**练习 2**:文档设想的 `.reg.ax` 与当前 RTL 里的 VX/VE/VR 是什么关系?

> **答案**:它们是同一概念(每 Core 的向量寄存器)在不同成熟度的两种表达。`.reg.ax/bx/...` 是文档早期的命名设想;VX/VE/VR 是已经落地的「三类宽度寄存器」实现(u1-l4、u3-l2)。两者尚未对齐，说明寄存器命名/编址仍在演进——这正是 `[WIP]` 文档的常态。

**练习 3**:文档把 NPU 描述为 CPU 的「slave device(从设备)」，并采用「Harvard-ish」结构。这对 LD/ST 指令的设计有什么影响?

> **答案**:作为从设备，NPU 的指令(.code)和数据(.data)分离(Harvard 式)，所以 LD/ST 只搬数据、不会与取指争用同一端口;同时外部(CPU/DMA)也能访问 `.reg/.data` 等空间(表里 External R/W)，这意味着 LD/ST 的对象既可能是 NPU 自己发起的内部搬运，也可能是 CPU/DMA 发起的外部读写——这正是 u8 单元要讲的 XDMA/Python 驱动直接读写寄存器/SPM 的底层依据。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「从指令到数据格式」的端到端阅读任务:

**任务**:假设你要为 chisel-npu 设计一条 LD 指令，把一段 INT8 量化权重从 SPM 搬进寄存器堆，准备喂给 MMALU 做矩阵乘。请完成下列子任务:

1. **选指令家族与宽度**:在 `Funct3Mem` 的 6 个值里，你会选哪个 `funct3` 来表示「一次搬一整条 K-lane 的 VX 向量」?为什么不用 `BYTE`?
2. **选数据格式**:搬进来的 32 位存储字，应该用 `dataType.scala` 里的哪个 Bundle 解释?画出它的字节布局。
3. **算搬运规模**:在 K=8、n=8(K==n)的测试配置下，搬满一组 MMALU 的 A 矩阵 tile(n×n=64 个 INT8)需要多少个 32 位字?用你选的 `funct3` 需要多少次传输?
4. **读控制接口**:参考 `MMUCtrlBundle`，说明这 64 个 tile 的「来源地址」由哪个字段携带、占多少位。
5. **诚实标注**:在本任务的结论里，明确标出哪些步骤「已经有 RTL/编码支撑」、哪些「只是设计意图/脚手架」。

**参考答案要点**:

1. 选 `VX_VEC`(funct3=3):它一次搬整条 K-lane 的 VX 向量(K×N 位)。不用 `BYTE`(单 lane)是因为它一次只搬 8 位，搬 64 个数据要 64 次，而 `VX_VEC` 一次搬一条 VX(K=8 时 64 位 = 8 个 INT8)，效率高得多。
2. 用 `U8C4Input`(无符号 INT8 量化权重)或 `S8C4Input`(有符号，更常见于量化权重)。布局见 4.2.4 的字节图。
3. 64 个 INT8 = \(64 \times 8 = 512\) 位 = 16 个 32 位字(`U8C4` 每字 4 个 INT8，\(64/4=16\))。用 `VX_VEC`(每条 VX=64 位=8 个 INT8)需 \(64/8 = 8\) 次传输。
4. `MMUCtrlBundle.in_addr: Vec(n*n, UInt(log2Ceil(size).W))`，n=8 时 64 个地址，每个 12 位(size=4096)，共 768 位。
5. **已有支撑**:`Funct3Mem` 编码、译码器抽 `mem_width`、`dataType`/`MMUCtrlBundle` 的类型定义。**只是脚手架**:`dataType` 与 `MMUCtrlBundle` **尚未接线**到任何模块、`02.memory.md` 地址空间仍是 `[WIP]`/`TBD`、LD/ST 译码器**不做** funct3 合法性检查且无功能测试。结论:当前你可以「写出」这条 LD 指令的编码并让译码器抽出宽度，但它还**不会真正搬运数据**——完整的内存子系统是后续工作。

## 6. 本讲小结

- **LD/ST 用 funct3 表示传输宽度**:opcode 区分读写，funct3 被省下来表示 `BYTE/HALF/WORD/VX_VEC/VE_VEC/VR_VEC` 六种粒度，这是 RISC-V 思路在 NPU 上的扩展。
- **打包是 NPU 提升带宽的根本手段**:`dataType.scala` 的 `U8C4/S8C4/...` 把多个小数据塞进 32 位字，打包密度 \(C=32/W\);4 个 INT8 打包(`U8C4`)相对逐字节搬运有 4 倍带宽收益。
- **访存控制是「瘦指令 + 胖控制结构」**:LD/ST 指令本身只发起传输，矩阵 tile 的批量地址由 `MMUCtrlBundle` 的 `Vec(n*n, ...)` 承载(n=8 时仅地址就 1536 位)。
- **`MemLayout`/`MemChannel` 提供字内子通道寻址**:`bit8` 有 ch0..ch3、`bit16` 有 ch0/ch2、`bit32` 只有 ch0，可用通道数 \(=32/\text{粒度}\)。
- **内存模型仍是进行中**:`02.memory.md` 标 `[WIP]`，提出共享 L2 + 大 SPM + Harvard 式从设备架构，地址全是 `TBD`。
- **关键诚实结论**:`dataType.scala`、`memMicroCode.scala` 的类型**当前未接入**译码器/后端，LD/ST 路径只有「编码 + 抽 mem_width」已实现、无功能测试;本讲讲的是**设计意图与预留接口**，而非已跑通的硬件行为。

## 7. 下一步学习建议

本讲讲的是「数据怎么进/出」，但还没讲「数据进来后由谁执行」。建议按以下顺序继续:

1. **u2-l4 Scala 汇编器**:学会用 `encR/encI/encS` 真正构造一条 LD/ST 指令字，把本讲的 funct3 宽度编码落实到 32 位十六进制值。
2. **u2-l5 组合译码器**:深入 `InstrDecoder`，对照本讲看到的 `mem_width := f3`，理解 LD/ST 与 VALU/MMA 家族在译码大表里的位置差异。
3. **u3-2 多宽度寄存器堆**:`LD/ST` 搬进来的数据最终落到 VX/VE/VR 寄存器堆;学完寄存器堆的别名机制，你就能完整理解「一个 `U8C4` 字写进 VR[i] 如何原子更新底层 4 行 VX」。
4. **u8-2 / u8-3 FPGA 驱动**:当你想看「矩阵 tile 搬运」真正跑起来是什么样，跳到 u8 单元的 XDMA C 工具与 Python 驱动，那里的 `stage→kick→wait→collect` 四步正是 `MMUCtrlBundle` 在软件侧的对应物。

> 想要更扎实地理解本讲，建议回头再读一遍 `dataType.scala` 与 `memMicroCode.scala` 全文(都很短)，并在仓库里搜索 `U8C4Input`、`MMUCtrlBundle` 确认它们确实尚未被引用——亲手验证「脚手架」状态，比任何讲解都更有说服力。
