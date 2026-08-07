# 项目总览与定位

## 1. 本讲目标

本讲是整本学习手册的第一篇,目标是让你在还没有读任何一行 Chisel 代码之前,先建立对 `chisel-npu` 这个项目的**全局认识**。读完本讲,你应当能够:

- 用一句话说清 `chisel-npu` 是什么、解决什么问题;
- 说出 **MMALU、VALU、多宽度寄存器堆** 三大核心组件各自的作用;
- 识别项目的主要技术栈(Chisel / Scala / sbt / firtool / Verilator / SystemC / Vivado);
- 准确复述贯穿全代码的三个符号 **N(bits)、L、K** 的含义;
- 区分 `docs/`(文档)与 `src/`(源码)两个阅读入口,并画出一张数据流框图。

本讲不要求你懂 Chisel 语法,也不要求你能跑仿真,只需要会读 README 和文档表格。

---

## 2. 前置知识

本讲为零基础读者设计,但有几个名词先建立一个**直觉上的印象**即可,后面遇到再展开:

- **NPU(Neural Processing Unit,神经网络处理器)**:一种专门加速神经网络推理(尤其是矩阵乘法和激活函数)的硬件加速器,可以理解为「把 CPU 里几十条指令才能完成的矩阵运算,压缩成硬件里一拍/几拍就完成的电路」。
- **RTL(Register Transfer Level,寄存器传输级)**:描述硬件电路的代码风格,Chisel / Verilog / SystemVerilog 都是 RTL 语言。`chisel-npu` 用 Chisel 写 RTL,再「翻译(elaborate)」成 SystemVerilog 给仿真器和 FPGA 工具用。
- **FPGA(Field Programmable Gate Array,现场可编程门阵列)**:一种可以反复「重连线」的芯片,常用来在流片前验证 RTL 设计。本项目用 Xilinx Kintex-7 FPGA 做验证平台。
- **ISA(Instruction Set Architecture,指令集架构)**:软件与硬件之间的「合同」,规定了一条指令的二进制编码和它该做什么。本项目采用 RISC-V 风格的 32 位 ISA。

如果上面某个词你完全没听过也不必担心,本讲只用得上最表层的含义。

---

## 3. 本讲源码地图

本讲只读「项目门面」级别的文件,不进入任何 Chisel 模块内部:

| 文件 | 作用 | 本讲用它来 |
|:---|:---|:---|
| [README.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/README.md) | 面向用户的项目总览:亮点、用法、目录结构 | 理解项目定位与技术栈 |
| [docs/index.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/index.md) | ReadTheDocs 文档首页,含**权威记号表** | 记住 N/L/K 三参数与寄存器类别 |
| [AGENTS.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/AGENTS.md) | 给 AI 助手/开发者的「避坑指南」,信息密度极高 | 交叉验证技术栈与源码布局 |
| [src/main/scala/top/top.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/top/top.scala) | 顶层入口,把 Chisel 设计 elaborate 成 `top.sv` | 看懂「Chisel → SystemVerilog」的产物链路 |
| [docs/implementations/NeuralCore.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/NeuralCore.md) | 神经核心的实现说明,含组件数据流图 | 画出本讲的实践框图 |

> 阅读建议:README 是「故事版」,docs/index.md 是「参数字典」,AGENTS.md 是「避坑清单」。三者交叉读,信息最准。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块:**① 项目定位与技术栈(README Highlights)**、**② 全局参数与记号表(docs/index.md)**、**③ NCoreBackend 组件数据流图**。

### 4.1 项目定位与技术栈(README Highlights)

#### 4.1.1 概念说明

`chisel-npu` 是一个**用 Chisel 6 实现的开源 NPU RTL 设计**,目标是面向低功耗、边缘(edge)场景的 SoC 集成。换句话说,它不是一块可以买到的芯片,而是一套「设计图纸(RTL)」加上「验证平台(FPGA)」加上「驱动软件(Linux/Python)」的完整开源工程,你可以拿来学习、二次开发,甚至流片。

它的核心设计哲学来自 [docs/implementations/NeuralCore.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/NeuralCore.md):**像一个轻量级超流水线处理器**——当脉动阵列(MMALU)在跑几十拍的矩阵乘时,向量 ALU(VALU)和访存单元可以与之重叠执行,从而提升吞吐。

#### 4.1.2 核心流程

从「写代码」到「上 FPGA」的整条链路可以概括为:

1. 用 **Chisel(Scala)** 写 RTL,放在 `src/main/scala/`。
2. 用 **sbt** 调用 **firtool**(CIRCT 的 FIRRTL 编译器)把 Chisel「elaborate」成 **SystemVerilog**(`top.sv`)。
3. 用 **Verilator / SystemC** 做软件仿真与测试(`src/test/scala/`)。
4. 用 **Vivado** 把 `top.sv` 综合、布局布线,烧进 **Kintex-7 FPGA** 做实测。

这条链路上的每一环都由 Docker 镜像统一封装,因此你不必在裸机上折腾工具链。

#### 4.1.3 源码精读

README 的 Highlights 段落直接列出了项目的「六大卖点」,这是理解项目最省力的入口:

- [README.md:L9-L30](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/README.md#L9-L30) —— 这段用 bullet 列出了 RISC-V 风格 32 位 ISA、K×K 脉动 MMALU、K 通道 VALU、多宽度寄存器堆、端到端量化流水线、FPGA 参考平台六大亮点。本讲的「三大核心组件」就来自这里。

技术栈的**权威出处**在 AGENTS.md,它明确写出 Chisel / Scala / sbt 版本和所需工具:

- [AGENTS.md:L6-L9](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/AGENTS.md#L6-L9) —— 这段说明:Chisel 6.7.0、Scala 2.13.12、sbt 1.9.7,需要 firtool 1.62.1、verilator v5.036、SystemC 3.0.1,且这些工具都由 `fangruil/chisel-dev` Docker 镜像提供。关键提醒:**裸机上的 sbt 通常跑不起来**,优先用 Docker。

这些版本号也能在 `build.sbt` 里交叉确认:

- [build.sbt:L3-L13](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/build.sbt#L3-L13) —— `scalaVersion := "2.13.12"`、`val chiselVersion = "6.7.0"`,以及 `chisel-plugin` 编译器插件依赖。

README 的 Usage 段给出了**最常用的几条命令**:

- [README.md:L32-L49](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/README.md#L32-L49) —— `make image` 建镜像、`make container` 进容器、`make test` 跑测试、`make build` 生成 Verilog、`make build-sc` 生成 SystemC、`make docs` 起文档。本讲你只需要记住前四条。

#### 4.1.4 代码实践(源码阅读型)

> 实践目标:不看正文,仅凭 README 自己复述出项目的「六大亮点」与「五条核心命令」。

操作步骤:

1. 打开 [README.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/README.md),只读 `## Highlights` 和 `## Usage` 两节。
2. 合上文件,用中文口头(或在笔记里)说出:项目有哪些亮点?跑测试用哪条命令?
3. 再打开 [AGENTS.md 的 Toolchain 段](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/AGENTS.md#L6-L9),核对你记的 Chisel / Scala / sbt 版本是否正确。

需要观察的现象 / 预期结果:你能不查文件说出 Chisel 6.7.0、Scala 2.13.12、`make test` 跑 `sbt test`、优先用 Docker 这四点,就说明定位已经建立。命令的**实际运行**见下一讲(u1-l2),本讲不要求执行。

#### 4.1.5 小练习与答案

**练习 1**:README 说项目「优先用 Docker」而非裸机 sbt,原因是什么?
> **参考答案**:因为 Chisel 的 elaboration 依赖 firtool 编译器,且需要正确版本的 Chisel 6.7.0 被 `publishLocal`,裸机环境很难凑齐。`fangruil/chisel-dev` 镜像把 firtool / verilator / SystemC 一次性装好,所以 AGENTS.md 明确建议「优先用 Docker 跑每一条命令」。

**练习 2**:下面哪个**不是** `chisel-npu` 技术栈的一部分:Scala、sbt、firtool、Vivado、CUDA?
> **参考答案**:CUDA。前三者是 RTL 构建链,Vivado 是 FPGA 工具;CUDA 是 GPU 生态,与本 NPU 的 RTL 设计无关。

---

### 4.2 全局参数与记号表(docs/index.md)

#### 4.2.1 概念说明

`chisel-npu` 全代码库(ISA、VALU、寄存器堆、backend、测试)反复出现三个符号:**N(bits)、L、K**。AGENTS.md 里有一句话点明了它们的重要程度:**「混淆这三个符号是本代码库最主要的一类错误来源」**。所以本讲必须先把它钉死。

除了三个参数,还有一个关键概念:**VX / VE / VR 三类寄存器共享同一块物理存储**,只是「看这块存储的位宽视角」不同。这是后面寄存器堆模块的根基,本讲先建立直觉。

#### 4.2.2 核心流程

三个参数的含义(以 docs/index.md 的记号表为准):

| 符号 | 含义 | 测试默认值 | Top(K=64) |
|:---:|:---|:---:|:---:|
| **N**(写作 **N(bits)**) | 基础通道位宽,等于 MMALU 的 `nbits` | 8 | 8 |
| **L** | 基础 VX 寄存器的数量,必须被 4 整除 | 32 | 32 |
| **K** | 每个寄存器的 SIMD 通道数;在 backend 边界 `K == MMALU.n`(阵列边长) | 8 | 64 |

三类寄存器对同一块物理字节数组的别名关系:

| 类别 | 数量 | 通道位宽 | 别名关系 |
|:---|:---:|:---:|:---|
| VX[0..L-1] | 32 | N bits | 原生视角 |
| VE[0..L/2-1] | 16 | 2N bits | VE[i] = VX[2i] ∥ VX[2i+1] |
| VR[0..L/4-1] | 8 | 4N bits | VR[i] = VX[4i..4i+3] |

物理存储总字节数:

\[
\text{TotalBytes} = L \times K \times \frac{N}{8}
\]

代入测试默认值(N=8, L=32, K=8):

\[
32 \times 8 \times \frac{8}{8} = 256 \text{ 字节}
\]

> 直觉:**「一块 256 字节的存储,既可以用 32 个 8 位 VX 寄存器看,也可以用 8 个 32 位 VR 寄存器看」**——后者正好用来存放 MMALU 的 32 位累加结果。一个银行(Bank)同时容纳 INT8 输入和 INT32/FP32 累加器,这就是 README 里「INT8 inputs, INT32/FP32 accumulators in one bank」的含义。

为什么 L 必须被 4 整除?因为 VR 的别名需要把每 4 个 VX 行(VX[4i..4i+3])合并成 1 个 VR 行,如果 L 不是 4 的倍数,VR 视角就无法整除,别名机制就崩了。这个约束在 `MultiWidthRegisterBlock` 里用 `require` 强制。

#### 4.2.3 源码精读

记号表的**权威出处**是 docs/index.md 的 `## Notation` 段:

- [docs/index.md:L14-L31](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/index.md#L14-L31) —— 这里用一张 info 块表格列出了 N/L/K 的定义、测试默认值与 Top 默认值,以及 VX/VE/VR 的别名表。本讲的 4.2.2 表格就是它的中文复述。

AGENTS.md 给出了**等价但更强调「坑」**的版本,并补了一条关键区分:

- [AGENTS.md:L29-L48](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/AGENTS.md#L29-L48) —— 同样的记号表,额外强调:**MMALU 的 `n` 是「脉动阵列边长」,不是通道数;`NCoreBackend` 在实例化时用 `require` 强制 `K == mmalu.n`**。不要把 `K`(VALU 通道数)和 `n`(阵列边长)当成两回事。

#### 4.2.4 代码实践(纸笔型)

> 实践目标:用具体数字验证记号表,亲手算出物理存储大小和别名下标。

操作步骤:

1. 假设 N=8、L=32、K=8(测试默认)。
2. 用公式 \( L \times K \times N/8 \) 算出物理存储总字节数。
3. 写出 VE[3] 和 VR[2] 各自对应的 VX 别名下标。
4. 把你的答案与 [docs/index.md:L24-L31](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/index.md#L24-L31) 的别名表对照。

需要观察的现象 / 预期结果:

- 总字节 = \( 32 \times 8 \times 1 = 256 \) 字节;
- VE[3] = VX[6] ∥ VX[7];
- VR[2] = VX[8..11]。

这是纯纸笔计算,无需运行任何命令。

#### 4.2.5 小练习与答案

**练习 1**:如果 N=8、L=32、K=8,VE 类寄存器一共有多少个?每个 VE 的通道位宽是多少?
> **参考答案**:VE 数量 = L/2 = 16 个;每个 VE 的通道位宽 = 2N = 16 bit。

**练习 2**:为什么 K 在 backend 边界必须等于 MMALU 的 `n`?
> **参考答案**:因为 MMALU 的 `n` 是脉动阵列的边长(n×n 个 PE),而 K 是 VALU 与寄存器堆的 SIMD 通道数。要让 MMALU 一次吃进/吐出的 K 个通道与 VALU、寄存器堆的 K 通道对齐,就必须 `K == mmalu.n`,这个约束由 `NCoreBackend` 的 `require` 强制。

**练习 3**:L 为什么必须被 4 整除?
> **参考答案**:VR 视角需要把每 4 个 VX 行合成 1 个 VR 行(VR[i] = VX[4i..4i+3]),L 不能被 4 整除时这个划分无法整除,别名机制失效。

---

### 4.3 NCoreBackend 组件数据流图

#### 4.3.1 概念说明

前面两个模块讲的是「项目是什么」和「参数怎么读」。本模块把它们组装起来,让你看到 `chisel-npu` 的**数据是怎么流的**——这也是本讲综合实践的产出物。

神经核心(`NCoreBackend`)是整个 NPU 的中央执行单元,它把四个部件连成一条流水线:

1. **InstrDecoder(指令译码器)**:组合逻辑,把 32 位指令字翻译成 `DecodedMicroOp`。
2. **MultiWidthRegisterBlock(多宽度寄存器堆)**:就是 4.2 讲的那块 256 字节存储,提供 VX/VE/VR 读写端口。
3. **MMALU(矩阵乘法引擎)**:K×K 脉动阵列,执行矩阵乘与流式累加。
4. **VALU(向量 ALU)**:K 通道,做算术 / 逻辑 / 浮点 / LUT 激活 / 归约。

> 一句话直觉:**指令进来 → 译码器拆成控制信号和数据地址 → 寄存器堆喂操作数给 MMALU 和 VALU → 算完再写回寄存器堆**。

#### 4.3.2 核心流程

用伪流程描述一拍指令的生命周期(节选自 NeuralCore.md 的执行流水线):

```
Cycle 0  取指/发射:  Frontend 把 32 位指令字送给 InstrDecoder
Cycle 0  译码:        InstrDecoder 输出 DecodedMicroOp
                     → 寄存器堆按 rd/rs1/rs2 异步读出操作数
                     → NCoreVALUBundle 送给 VALU
                     → NCoreMMALUCtrlBundle 送给 MMALU
Cycle 1  计算 + 锁存:  VALU 用 RegNext 锁存输出;MMALU 的 PE 在累加
Cycle 2  写回(VALU):  out_vx/ve/vr 写回寄存器堆对应写端口
Cycle 3K-2 MMALU 收尾: MMALU 的 INT32 结果写回 VR 写端口 1(不截断)
```

两个值得注意的时序要点(后面讲义会反复用到):

- **VALU 写回需要保持 2 拍**:因为 VALU 的输出寄存器多加了 1 拍延迟,译码出的向量操作必须保持有效 2 个时钟周期,写回才能在第 2 拍触发。
- **MMALU 与 VALU 可重叠**:MMALU 流水线长达 `3K-2` 拍,而 VALU 只有 1~2 拍,调度器可以在脉动阵列「排空」期间发射向量指令,把量化开销隐藏掉。

#### 4.3.3 源码精读

NeuralCore.md 用一张 mermaid 图把四个组件的连线画得清清楚楚,这是本讲最重要的「参考图」:

- [docs/implementations/NeuralCore.md:L21-L48](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/NeuralCore.md#L21-L48) —— 这张 `graph TB` 图展示了 `Frontend → InstrDecoder → {RegisterFile, MMALU, VALU}` 的完整拓扑,并标注了每条连线上传递的 Bundle 名称(如 `NCoreVALUBundle`、`NCoreMMALUCtrlBundle`)和写回端口(out_vx → VX 写口 0、MMALU out → VR 写口 1)。

组件职责的文字说明在同文件:

- [docs/implementations/NeuralCore.md:L58-L78](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/NeuralCore.md#L58-L78) —— 逐个说明四个组件:寄存器堆是 `L×K×N/8` 字节、异步读同步写;MMALU 是 K×K PE、延迟 `3K-2` 拍、INT32 结果直写 VR 不截断;VALU 是 K 通道、FP32/BF16/BF8、除 vfma 外都是 1 拍输出。

参数约束(把 4.2 的参数和 4.3 的组件焊在一起):

- [docs/implementations/NeuralCore.md:L127-L132](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/NeuralCore.md#L127-L132) —— 列出四条 `require` 约束:`K == mmalu.n`、`L % 4 == 0`、`N == mmalu.nbits`、`4N == mmalu.accum_nbits`。这正是参数表与组件图的「接口契约」。

最后看一眼顶层入口,理解「Chisel → SystemVerilog」是怎么发生的:

- [src/main/scala/top/top.scala:L11-L19](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/top/top.scala#L11-L19) —— `object Main` 调用 `ChiselStage.emitSystemVerilog(...)`,把一个 Chisel 模块 elaborate 成 SystemVerilog 字符串,再 `Files.write` 到 `top.sv`。

> ⚠️ 诚实说明:当前 `top.scala` 的 `Main` 实际 elaborate 的是 `new MMALU(new MMPE(), 32, 8, 32)`(即一个 n=32、nbits=8、accum=32 的 MMALU,对应 README 里的「K=32 MMALU」FPGA 版本),**并不是完整的 NCoreBackend**。也就是说,仓库根目录的 `top.sv` 目前只含矩阵引擎。完整的 `NCoreBackend` 定义在 `backend/SimpleBackend.scala`,会在 u6 单元详细讲解。本讲你只需理解 elaborate 这条链路本身。

#### 4.3.4 代码实践(画图型)

> 实践目标:亲手画出 `Frontend → InstrDecoder → (RegisterFile, MMALU, VALU)` 的数据流框图,并在图上标注 N(bits)/L/K 出现的位置。

操作步骤:

1. 仔细阅读 [docs/implementations/NeuralCore.md 的组件图](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/NeuralCore.md#L21-L48) 和 [4.2 的记号表](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/index.md#L14-L31)。
2. 在纸上(或任意画图工具)画出 5 个方框:Frontend、InstrDecoder、RegisterFile、MMALU、VALU。
3. 照着 mermaid 图连线,并在连线上写出传递的内容(如 `DecodedMicroOp`、`VX 读口 0,3`、`out_vx → VX 写口 0`)。
4. **标注参数位置**:
   - 寄存器堆方框上标 `L × K × N/8 字节`、`VX/VE/VR`;
   - MMALU 方框上标 `n = K`、`nbits = N`、`accum = 4N`、延迟 `3K-2`;
   - VALU 方框上标 `K 通道 × {N, 2N, 4N}`。

需要观察的现象 / 预期结果:你应当得到一张与 NeuralCore.md mermaid 图拓扑一致、且在每个计算部件上都标了 N/L/K 的框图。如果你画出的图里 MMALU 的 INT32 输出**没有**指向 VR 写口 1,或 VALU 输出**没有**分别指向 VX/VE/VR 写口 0,说明漏了关键连线,回头核对 [L21-L48](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/NeuralCore.md#L21-L48)。

#### 4.3.5 小练习与答案

**练习 1**:在数据流图里,MMALU 的结果写回寄存器堆的**哪个**写端口?为什么强调「不截断」?
> **参考答案**:写回 VR 写端口 1。「不截断」是指 MMALU 输出 `Vec(K, SInt(4N.W))`(32 位)直接写入 VR(4N 位),中间不做任何位宽裁剪,保证累加精度不丢。

**练习 2**:InstrDecoder 是组合逻辑还是时序逻辑?它的输入输出分别是什么?
> **参考答案**:纯组合逻辑(一拍完成,无寄存器)。输入是 32 位指令字,输出是 `DecodedMicroOp`(含 family、op、regCls、rd/rs1/rs2/rs3、imm、mma 控制等字段),并附带一个 `io.illegal` 非法指令标志。

**练习 3**:为什么说「MMALU 与 VALU 可重叠执行」对 NPU 性能很重要?
> **参考答案**:MMALU 一次矩阵乘要跑 `3K-2` 拍,而量化所需的 vcvt/vfma 等 VALU 指令只要 1~2 拍。如果能在这 `3K-2` 拍的「排空窗口」里穿插发射 VALU 指令,就能把量化开销隐藏在矩阵乘时间里,大幅提升吞吐——这正是「轻量级超流水线处理器」设计哲学的体现。

---

## 5. 综合实践

把本讲三个最小模块串起来,完成下面这个**贯穿性小任务**:

> **任务:为 `chisel-npu` 写一张「一页纸项目速览卡」(Project One-Pager)。**

要求这张速览卡包含四个区域:

1. **一句话定位**:用你自己的话(不要抄 README 原文)说明 `chisel-npu` 是什么。
2. **技术栈**:列出 Chisel / Scala / sbt / firtool / Verilator / SystemC / Vivado 各自扮演的角色,并注明版本(参考 [AGENTS.md:L6-L9](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/AGENTS.md#L6-L9))。
3. **参数字典**:抄录 N/L/K 三参数的定义、默认值,以及 VX/VE/VR 别名表(参考 [docs/index.md:L14-L31](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/index.md#L14-L31)),并亲手算出物理存储字节数。
4. **数据流框图**:画出 4.3.4 的 `Frontend → InstrDecoder → (RegisterFile, MMALU, VALU)` 框图,并在每个计算部件上标注 N/L/K。

验收标准:把这张速览卡给一个完全没接触过本项目的同学看,他能在 5 分钟内说清「这项目用什么写的、有哪三大组件、数据怎么流」。完成它,你就达成了本讲的全部学习目标。

---

## 6. 本讲小结

- `chisel-npu` 是一个用 **Chisel 6.7.0(Scala 2.13.12)** 实现的**开源 NPU RTL 设计**,面向低功耗边缘 SoC,配套 FPGA 验证平台与 Linux/Python 驱动。
- 三大核心组件:**MMALU**(K×K 脉动矩阵引擎)、**VALU**(K 通道向量 ALU)、**多宽度寄存器堆**(VX/VE/VR 共享一块物理存储)。
- 三个全局参数必须钉死:**N(bits)=8** 基础通道位宽、**L=32** VX 寄存器数(被 4 整除)、**K=8(test)/64(top)** 通道数且 `K == mmalu.n`。
- 物理存储 \( L \times K \times N/8 \) 字节(默认 256B),VX/VE/VR 是同一块存储的三种位宽视角。
- 数据流是 `Frontend → InstrDecoder → (RegisterFile, MMALU, VALU)`,MMALU 结果直写 VR 不截断,VALU 按宽度写回。
- 优先用 Docker 跑命令,两条最常用:`make test`(跑测试)、`make build`(生成 `top.sv`)。

---

## 7. 下一步学习建议

本讲只读了「门面」,还没碰任何 Chisel 代码。建议按下面顺序继续:

1. **下一讲 u1-l2《开发环境与构建运行方式》**:亲手跑通 `make test` / `make build`,理解 Makefile、`build.sbt` 与 Docker 镜像如何协作,把本讲「纸面认识」变成「能跑起来」。
2. **之后 u1-l3《源码目录结构与顶层入口》**:进入 `src/main/scala/`,理解各子目录职责,并把 `top.scala` 的 elaborate 链路跑一遍。
3. **再之后 u1-l4《全局参数 N/L/K 与寄存器类概念》**:深入到 `isa/instrFormat.scala`,把本讲 4.2 的记号表和真实源码里的枚举(`VecWidth`)对应起来。

> 推荐先读源码:`src/main/scala/top/top.scala`(最短,20 行)→ `docs/index.md` 的 Notation 表 → `docs/implementations/NeuralCore.md` 的组件图。这三处读通,你就有了阅读后续所有讲义的「地图」。
