# 源码目录结构与顶层入口

## 1. 本讲目标

上一讲（u1-l2）我们跑通了 `make test` / `make build`,知道 `sbt run` 会调用 `top.Main` 并产出 `top.sv`。本讲把镜头拉近,回答四个问题:

1. `src/main/scala` 下到底有哪些目录?每个目录分别负责什么?
2. 「顶层入口」`top.Main` 在哪个文件里?它到底做了什么?
3. 一段 Chisel/Scala 代码,是怎么一步步变成 SystemVerilog 的(`elaborate` 链路)?
4. `top.Main` 当前 elaborate 的对象是 `MMALU`,它的规模如何随参数 `n` 变化?

学完后你应该能:画出 `src/` 的目录树并标注职责;找到并读懂 `top.scala`;说清楚 `ChiselStage.emitSystemVerilog` 的输入、输出与产物文件;理解「改一个参数 → 生成的 Verilog 规模随之改变」这件事的因果。

## 2. 前置知识

- **Chisel 不是又一种硬件描述语言,而是 Scala 的库。** 你写的 `.scala` 文件首先是一段合法的 Scala 程序;程序运行时,Chisel 会「搭建」一棵硬件图(FIRRTL IR),再由 CIRCT 的 `firtool` 把这棵 IR 翻译成 SystemVerilog。所以「生成 RTL」本质上是「运行一段 Scala 程序」。
- **elaborate(精细化/展开)**:运行上述 Scala 程序、把参数化的 Chisel 模板「实例化」成具体硬件的过程。例如 `new MMALU(..., n = 32, ...)` 在 elaborate 时才会确定「阵列里到底摆多少个 PE」。
- **`sbt run`** 会扫描项目里的 `object ... extends App`,自动发现并运行主入口。本项目的入口是 `top.Main`。
- 本讲继续沿用上一讲钉死的记号:`N(bits)`=基础通道位宽、`L`=VX 寄存器个数、`K`=SIMD 通道数(在 backend 边界 `K == MMALU.n`)。详见 u1-l1。

> 提醒:本讲只看「目录 + 入口 + elaborate 链路」,不展开 ISA、寄存器堆、计算通路的内部细节——那些分别在 U2、U3、U4/U5 讲。本讲的目标是让你「拿到地图,认得大门」。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
|:---|:---|
| `src/main/scala/top/top.scala` | **顶层入口** `object Main`,运行后 elaborate `MMALU` 并写出 `top.sv` |
| `src/main/scala/alu/` | 计算单元:`mma/`(矩阵乘引擎)、`pe/`(处理单元)、`vec/`(向量 ALU) |
| `src/main/scala/backend/SimpleBackend.scala` | `NCoreBackend`:把译码器+寄存器堆+MMALU+VALU 连成后端流水线(完整后端,但**当前 `top.Main` 暂未 elaborate 它**) |
| `src/main/scala/isa/` | 指令集:编码格式、opcode 家族、汇编器 `NpuAssembler`、译码器 `InstrDecoder`、数据类型、微操作 Bundle |
| `src/main/scala/sram/` | 存储:`multiWidthRegister.scala`(VX/VE/VR 别名寄存器堆)、`register.scala`(旧版) |
| `src/main/scala/utils/gates.scala` | 通用门电路工具(如 `ORGate`) |
| `src/test/scala/` | 仿真测试(ScalaTest + Chisel 原生仿真器),目录结构与 `main` 镜像 |
| `build.sbt` | Scala/Chisel 版本与依赖(Chisel 6.7.0、Scala 2.13.12) |
| `Makefile` | `build`/`test` 等目标,本质是对 `sbt run`/`sbt test` 的 Docker 薄封装 |
| `ip/vivado/` | 打包好的 Vivado 工程(纯 Verilog + TCL,**不参与 sbt 构建**) |

> 完整的目录职责速查表见仓库的 [AGENTS.md#L103-L115](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/AGENTS.md#L103-L115),本讲只挑与「入口 + elaborate」相关的部分精读。

## 4. 核心概念与源码讲解

### 4.1 源码目录结构与职责

#### 4.1.1 概念说明

Chisel 项目遵循 Scala/sbt 的标准约定:

- `src/main/scala/` 放**产品代码**——这些代码 elaborate 后会变成真正的 RTL,最终上 FPGA/ASIC。
- `src/test/scala/` 放**测试代码**——它们是 ScalaTest 规范,在仿真器里 `poke`/`peek` 信号,验证产品代码的行为,**不会**被 elaborate 进 RTL。
- 两套目录通常**镜像**排布:`main/scala/alu/vec/vec.scala` 对应 `test/scala/alu/vec/VALUArithSpec.scala`,方便「找实现→找测试」。

chisel-npu 的 `main` 源码按「数据流方向」自下而上组织:最底层的处理单元(`alu/pe`)→ 矩阵乘引擎(`alu/mma`)与向量 ALU(`alu/vec`)→ 由后端(`backend`)连线 → 由 ISA(`isa`)提供指令接口 → 由 `top` 作为入口把某个模块 elaborate 出去。

#### 4.1.2 核心流程:目录到数据流的对应

```text
isa/                  指令接口层(给后端「下命令」)
  └─ instrFormat / instSetArch / NpuAssembler / instrDecoder
       │
backend/              集成层(把命令翻译成对各单元的控制)
  └─ SimpleBackend.scala = NCoreBackend
       │   ├── InstrDecoder     (译码)
       │   ├── MultiWidthRegisterBlock (寄存器堆, sram/)
       │   ├── MMALU            (矩阵乘, alu/mma/)
       │   └── VALU             (向量运算, alu/vec/)
       │
alu/                  计算层(真正做算术)
  ├─ pe/   处理单元(BasePE / MMPE)
  ├─ mma/  矩阵乘引擎(SystolicArray + ControlUnit + DataFeeder/Collector)
  └─ vec/  向量 ALU(算术/逻辑/LUT/浮点)
sram/                 存储层(寄存器堆)
top/                  入口层(本讲主角)
utils/                杂项工具门电路
```

注意一个「认知陷阱」:**`top.Main` 当前 elaborate 的只是 `MMALU`,而不是完整的 `NCoreBackend`。** 完整后端 `NCoreBackend` 定义在 [backend/SimpleBackend.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala) 里,且被各 `NCoreBackend*Spec` 测试覆盖,但仓库根目录的 `top.sv` 暂时只反映 `MMALU`。这一点 u1-l1 已提过,本讲在 4.4 用源码坐实它。

#### 4.1.3 源码精读:main 与 test 的镜像

产品源码清单(共 22 个 `.scala` 文件,按目录分组):

```text
src/main/scala/
├── alu/
│   ├── mma/  mma.scala, cu/controlUnit.scala, sa/{systolicArray,dataFeeder,dataCollector}.scala
│   ├── pe/   basePE.scala, procElem.scala
│   └── vec/  vec.scala, fp.scala
├── backend/  SimpleBackend.scala
├── isa/      instrFormat, instSetArch, NpuAssembler, instrDecoder, dataType
│   └── micro_op/  MMALUMicroCode, VALUMicroCode, memMicroCode
├── sram/     multiWidthRegister.scala, register.scala
├── top/      top.scala
└── utils/    gates.scala
```

测试源码在 `src/test/scala/` 下镜像同样的 `alu/isa/sram/backend` 结构,另多出 `utils/`(共享测试工具包 `testUtil`,如 `printHelper`、`widthHelper`)。

#### 4.1.4 代码实践:目录速读

1. **目标**:建立「找实现 / 找测试」的肌肉记忆。
2. **操作步骤**:
   - 在仓库根目录执行 `ls src/main/scala/alu/vec/`,确认 `vec.scala`(VALU 实现)存在。
   - 再执行 `ls src/test/scala/alu/vec/`,找到对应的 `VALUArithSpec.scala`。
   - 打开 `src/main/scala/backend/SimpleBackend.scala`,只看类名声明,确认 `NCoreBackend` 在这里。
3. **需要观察的现象**:`main` 与 `test` 的子目录名几乎一一对应。
4. **预期结果**:你能凭目录名直接定位「某模块的实现文件」与「它的测试文件」。
5. 命令行清单(只读)属「待本地验证」——具体文件名以上文树状图为准。

#### 4.1.5 小练习与答案

- **练习 1**:`isa/micro_op/` 下的三个 Bundle 文件分别服务于哪个计算单元?
  - **答**:`MMALUMicroCode.scala` 服务于 MMALU,`VALUMicroCode.scala` 服务于 VALU,`memMicroCode.scala` 服务于(未来的)访存指令。
- **练习 2**:为什么 `ip/vivado/` 下的 `.v` 文件不出现在 `src/main/scala` 树里?
  - **答**:它们是打包好的 Vivado 工程(平台胶水逻辑,如 `npu_ctrl_lite.v`、`npu_dma_master.v`),属于 FPGA 平台层而非 NPU 计算核 RTL,不参与 sbt/Chisel elaborate,所以独立存放。详见 U8。

---

### 4.2 顶层入口 top.Main

#### 4.2.1 概念说明

「顶层入口」就是那个被 `sbt run` 自动发现、运行后会「生成 RTL」的 Scala `object`。在 chisel-npu 里,它是 `top` 包里的 `object Main`。它本身**不含任何硬件逻辑**——它的职责只有一个:实例化一个目标模块,把它交给 Chisel 去 elaborate,然后把结果字符串写到磁盘文件 `top.sv`。

可以把 `top.Main` 理解成一条「生产线开关」:按下它(`sbt run`),流水线就启动:Scala 程序跑起来 → 生成 FIRRTL → `firtool` 翻译 → 得到 SystemVerilog 文本 → 落盘。

#### 4.2.2 核心流程:从 sbt run 到 top.Main

```text
make build  ──► docker run ... sbt run
                        │
                        ▼
              sbt 扫描所有 object ... extends App
                        │
                        ▼
              发现 top.Main 并执行其主体
                        │
                        ▼
         ChiselStage.emitSystemVerilog(new MMALU(...))
                        │
                        ▼
              返回 SystemVerilog 文本字符串 hdl
                        │
                        ▼
         Files.write(Paths.get("top.sv"), hdl.getBytes(...))
                        │
                        ▼
                   仓库根目录出现 top.sv
```

关键点:`sbt run` 之所以能「自动找到」`top.Main`,是因为它满足两个条件——位于 `src/main/scala` 下(在 sbt 的编译范围里)、且是 `object ... extends App`(带标准 Scala 入口)。项目里目前只有这一个 `App` 对象,所以无需额外指明。

#### 4.2.3 源码精读

整个入口只有 9 行有效代码,见 [src/main/scala/top/top.scala#L11-L19](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/top/top.scala#L11-L19):

```scala
object Main extends App {
  val hdl = ChiselStage.emitSystemVerilog(
    new MMALU(new MMPE(), 32, 8, 32),
    firtoolOpts = Array("-disable-all-randomization", "-strip-debug-info")
  )
  Files.write(Paths.get("top.sv"), hdl.getBytes(StandardCharsets.UTF_8))
}
```

逐行解读:

- `object Main extends App`:`App` trait 让 `Main` 的类体直接成为 `main` 方法,`sbt run` 会执行它。
- `import alu.mma._` / `import alu.pe._`(文件顶部):把 `MMALU` 与 `MMPE` 引入作用域。
- `ChiselStage.emitSystemVerilog(new MMALU(...), firtoolOpts = ...)`:核心一行(详见 4.3)。`new MMALU(...)` 是被 elaborate 的目标;返回值 `hdl` 是一整个 SystemVerilog 文件的**字符串**。
- `new MMALU(new MMPE(), 32, 8, 32)`:实例化一个 `MMALU`,参数依次是 `pe_gen / n / nbits / accum_nbits`,即「用 `MMPE` 当处理单元、阵列边长 32、数据位宽 8、累加位宽 32」(详见 4.4)。
- `firtoolOpts`:`-disable-all-randomization` 关掉未初始化寄存器的随机初值(便于确定性仿真),`-strip-debug-info` 去掉调试信息(缩小产物体积)。
- `Files.write(Paths.get("top.sv"), ...)`:用 JDK 的 NIO 把字符串按 UTF-8 写到仓库根目录的 `top.sv`。

> 对比 Makefile:见 [Makefile#L30-L33](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/Makefile#L30-L33)。`build` 依赖伪文件目标 `top.v`,而 `top.v` 目标里实际跑的是 `sbt run`;`sbt run` 又写出 `top.sv`。这就是 u1-l2 提到的「目标名 `top.v` 与产物 `top.sv` 不一致」的根因——它都在这 4 行 Scala 里。

#### 4.2.4 代码实践:定位入口

1. **目标**:确认 `sbt run` 到底运行了什么。
2. **操作步骤**:
   - 打开 [src/main/scala/top/top.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/top/top.scala)。
   - 在整个 `src/main/scala` 下搜索 `extends App`(用编辑器或 `Grep`)。
3. **需要观察的现象**:全仓库只有 `top.Main` 一个 `extends App` 对象。
4. **预期结果**:这解释了为何 `sbt run` 无需任何参数就能找到入口——它是唯一的 `App`。
5. 不需要运行任何修改性命令,属纯阅读型实践。

#### 4.2.5 小练习与答案

- **练习 1**:如果想让 `sbt run` 改为 elaborate `NCoreBackend`,最小改动是什么?
  - **答**:把 `new MMALU(new MMPE(), 32, 8, 32)` 换成 `new NCoreBackend(...)`(并 `import backend._`),`Files.write` 那一行基本不变。注意 `NCoreBackend` 的构造参数与 `MMALU` 不同,需对照 [SimpleBackend.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala) 的类签名填写。
- **练习 2**:`firtoolOpts` 里的 `-disable-all-randomization` 去掉会怎样?
  - **答**:未初始化的寄存器(如各 `RegInit` 之外裸 `Reg`)在仿真初始化时会带随机值,仿真行为可能不确定;去掉后产物体积略增、仿真复现性下降。

---

### 4.3 ChiselStage.emitSystemVerilog 产物链路

#### 4.3.1 概念说明

`ChiselStage.emitSystemVerilog` 是 CIRCT/Chisel 提供的「一键 elaborate」API。它接收两样东西:

1. 一个「待展开的模块」(一段 `=> Module` 的表达式,这里是 `new MMALU(...)`);
2. 一组传给底层 `firtool` 的选项(`firtoolOpts`)。

它返回一段 SystemVerilog **字符串**。整条链路是:

```text
Chisel(Scala) ──elaborate──► FIRRTL IR ──firtool(CIRCT)──► SystemVerilog 文本
```

- **第一步 elaborate**:运行 Scala,把 `new MMALU(new MMPE(), 32, 8, 32)` 这棵「参数化模板」展开成具体的、无参数的硬件图(FIRRTL)。此时 `n=32` 被代入,阵列里的 PE 个数、位宽全部固化为常量。
- **第二步 firtool 翻译**:CIRCT 的 `firtool` 把 FIRRTL IR 优化、 lowered,输出标准 SystemVerilog。

之所以强调「返回字符串」,是为了理解上一行 `Files.write(...)`:`top.sv` 只是把这个字符串原样落盘而已,没有任何额外魔法。

#### 4.3.2 核心流程:三段链路与依赖

```text
build.sbt 依赖:                      链路里每一步靠谁:
  org.chipsalliance % chisel 6.7.0   ──►  ChiselStage.emitSystemVerilog
  chisel-plugin(编译器插件)          ──►  让 Chisel 宏/注解生效
  firtool 1.62.1(镜像内置)          ──►  FIRRTL → SystemVerilog
```

版本由 [build.sbt#L7-L14](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/build.sbt#L7-L14) 钉死:

```scala
val chiselVersion = "6.7.0"
...
"org.chipsalliance" %% "chisel" % chiselVersion,
```

并需要编译器插件 [build.sbt#L23](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/build.sbt#L23):

```scala
addCompilerPlugin("org.chipsalliance" % "chisel-plugin" % chiselVersion cross CrossVersion.full)
```

`firtool` 不在 `build.sbt` 里——它是个**外部可执行文件**,由 `fangruil/chisel-dev` Docker 镜像提供(版本 1.62.1)。这正是 AGENTS.md 反复强调「优先用 Docker、裸机 sbt 多半跑不通」的原因:缺了 `firtool`,第二步就断了。

#### 4.3.3 源码精读:elaborate 与落盘

核心两行见 [src/main/scala/top/top.scala#L14-L18](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/top/top.scala#L14-L18):

```scala
val hdl = ChiselStage.emitSystemVerilog(
  new MMALU(new MMPE(), 32, 8, 32),
  firtoolOpts = Array("-disable-all-randomization", "-strip-debug-info")
)
Files.write(Paths.get("top.sv"), hdl.getBytes(StandardCharsets.UTF_8))
```

- `emitSystemVerilog` 的第一个参数是「名 call」`new MMALU(...)`——它被作为「按名参数」传入,函数内部才真正执行实例化,从而能完整收集硬件图。
- 返回的 `hdl: String` 就是整个 `.sv` 文件的内容;`Files.write` 把它写到相对路径 `top.sv`(相对当前工作目录,即仓库根)。
- `firtoolOpts` 是透传给 `firtool` 的命令行选项数组。

> 产物追踪:本仓库里 `top.sv` / `top.v` **均未被 git 跟踪**(可用 `git ls-files '*.sv' '*.v'` 验证,目前仅 `ip/vivado/...` 下的 3 个 `.v` 被跟踪)。也就是说 `top.sv` 是纯构建产物,需要先跑一次 `make build` 才会出现。

#### 4.3.4 代码实践:观察 elaborate 产物

1. **目标**:亲眼看到「Scala 字符串 → .sv 文件」的落盘。
2. **操作步骤**:
   - 先确认根目录没有 `top.sv`(`ls top.sv`,预期不存在)。
   - 执行 `make build`(等价于 `sbt run`,在 Docker 里)。
   - 再次 `ls top.sv`,并 `head -n 40 top.sv` 看文件头。
3. **需要观察的现象**:`top.sv` 出现;文件头应是 `module MMALU(...)`(因为 elaborate 的是 `MMALU`),而非 `NCoreBackend`。
4. **预期结果**:首模块名为 `MMALU`,印证「当前入口只 elaborate MMALU」。
5. 完整构建需 Docker 与网络拉依赖,首次较慢——若环境无 Docker,此项标注「待本地验证」。

#### 4.3.5 小练习与答案

- **练习 1**:`emitSystemVerilog` 返回的是字符串而不是直接写文件,这种设计有什么好处?
  - **答**:把「生成」与「落盘」解耦。调用者可以先把字符串打印到 stdout 调试、或写进任意路径、或做后处理(如本仓库另起 `build-sc` 走 verilator)。`top.Main` 只是选择了「写到 `top.sv`」这一种用法。
- **练习 2**:为什么 `firtoolOpts` 要显式 `-strip-debug-info`?
  - **答**:去掉与源码行号绑定但综合无关的注释/属性,缩小 `top.sv` 体积、降低下游工具(Verilator/Vivado)解析负担;对功能仿真不影响。

---

### 4.4 MMALU 实例化与参数化 elaborate

#### 4.4.1 概念说明

`MMALU` 是 NPU 的矩阵乘引擎,也是 `top.Main` 当前 elaborate 的唯一对象。它最关键的特性是:**规模随参数 `n`(脉动阵列边长)的平方增长**。这一节我们就用源码看清「参数如何决定硬件规模」,并完成本讲的主打实践——改 `n` 看 PE 数量变化。

回顾构造签名([src/main/scala/alu/mma/mma.scala#L23](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L23)):

```scala
class MMALU[T <: BasePE](pe_gen: => BasePE, val n: Int = 8, val nbits: Int = 8, val accum_nbits: Int = 32)
```

对照 `top.scala` 里的调用 `new MMALU(new MMPE(), 32, 8, 32)`:

| 位置 | 形参 | 实参 | 含义 |
|:---:|:---|:---:|:---|
| 1 | `pe_gen` | `new MMPE()` | 处理单元生成器(每个 PE 用 `MMPE`) |
| 2 | `n` | `32` | 脉动阵列边长(\(n\times n\) 个 PE) |
| 3 | `nbits` | `8` | 数据位宽(即 `N(bits)`) |
| 4 | `accum_nbits` | `32` | 累加器位宽 |

> 文档不一致提醒:`docs/index.md` 的记号表写着「Top (K=64)」,`AGENTS.md` 的参数表也写「Default (top): K=64」,但 `top.scala` 实际传入的是 `n=32`。以代码为准——当前 elaborate 的阵列边长是 **32**(这也与本讲实践任务的「从 32 改为 8」一致)。

#### 4.4.2 核心流程:n 如何决定规模

`MMALU` 用一行 `Seq.fill` 摆放下全部 PE,见 [src/main/scala/alu/mma/mma.scala#L34](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L34):

```scala
val pe_io = VecInit(Seq.fill(n * n) { Module(pe_gen).io })
```

- `Seq.fill(n * n) { Module(pe_gen).io }`:在 elaborate 时**循环 `n*n` 次**,每次实例化一个 `MMPE`。这是「参数化展开」的典型写法——`n` 在运行 Scala 时是普通 `Int`,循环结束后,这些 PE 就成了硬件图里固定的 `n*n` 个实例。
- 因此 PE 总数为:

\[
\text{PE 数量} = n^{2}
\]

代入两个 `n` 值:

\[
n=32 \Rightarrow 32^{2}=1024 \text{ 个 PE},\qquad n=8 \Rightarrow 8^{2}=64 \text{ 个 PE}
\]

从 32 改到 8,PE 数量降为原来的 \(1/16\)。同时,矩阵乘的流水线延迟(见 `mma.scala` 文件头注释)从 \(3n-1\) 变为更小:

\[
\text{延迟} = 3n-1 \quad(\text{插入流水寄存器后})
\]

\[n=32 \Rightarrow 95\text{ 拍},\qquad n=8 \Rightarrow 23\text{ 拍}\]

这就是「改一个参数,Verilog 规模与延迟同时变」的因果:`n` 在 elaborate 阶段被代入 `Seq.fill` 与各 `Vec(n, ...)` 端口,硬件随之放大或缩小。

PE 本体:`MMPE` 继承自 `BasePE`,见 [src/main/scala/alu/pe/procElem.scala#L12-L19](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/pe/procElem.scala#L12-L19) 与 [src/main/scala/alu/pe/basePE.scala#L12-L31](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/pe/basePE.scala#L12-L31)。`MMPE` 执行的是 `in_a * in_b` 并按 `keep` 决定累加或覆盖(PE 内部细节留待 U4,本讲只需知道「每个 `Module(pe_gen)` 就是一个 PE 实例」)。

#### 4.4.3 源码精读:实例化与端口规模

回到 [src/main/scala/alu/mma/mma.scala#L23-L34](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L23-L34):

```scala
class MMALU[...](pe_gen: => BasePE, val n: Int = 8, val nbits: Int = 8, val accum_nbits: Int = 32) extends Module {
  val io = IO(new Bundle {
    val in_a     = Input(Vec(n, SInt(nbits.W)))       // 端口宽度也随 n 变
    val in_b     = Input(Vec(n, SInt(nbits.W)))
    val in_accum = Input(Vec(n, SInt(accum_nbits.W)))
    ...
    val out      = Output(Vec(n, SInt(accum_nbits.W)))
    ...
  })
  val pe_io = VecInit(Seq.fill(n * n) { Module(pe_gen).io })  // n*n 个 PE
  ...
}
```

注意 `Vec(n, ...)` 出现在端口里——这意味着不只是 PE 个数,连 `MMALU` 对外的 `in_a/in_b/out` 端口宽度也随 `n` 线性变化。所以改 `n` 会同时改变:① 顶层端口位宽;② PE 实例数(\(n^2\));③ 各内部 `Vec` 与流水寄存器(如 `pipe_a = Seq.fill(n*n)`)的规模。

#### 4.4.4 代码实践:改 n 看 PE 数量(本讲主打)

1. **目标**:用实验验证「PE 数量 = \(n^2\)」,直观感受 elaborate 的规模随参数变化。
2. **操作步骤**:
   - 先按 4.3.4 跑一次 `make build`,生成基线 `top.sv`(`n=32`)。统计 `MMPE` 实例数,例如统计 `MMPE` 在 `top.sv` 中出现的次数(定义 1 处 + 实例化若干处)。
   - 打开 [src/main/scala/top/top.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/top/top.scala),把第 15 行的 `new MMALU(new MMPE(), 32, 8, 32)` 改成 `new MMALU(new MMPE(), 8, 8, 32)`(只把 `n` 从 32 改为 8)。
   - 再次 `make build` 重新生成 `top.sv`,再次统计 `MMPE` 实例数。
   - **实验结束后务必把 `n` 改回 32**(本仓库 `top.sv` 虽未被跟踪,但源码 `top.scala` 是被跟踪的,不要留下改动)。
3. **需要观察的现象**:实例数从约 \(32^2=1024\) 个降到约 \(8^2=64\) 个(差 16 倍);`top.sv` 文件体积也大幅缩小。
4. **预期结果**:PE 实例数与 \(n^2\) 吻合,印证 `Seq.fill(n*n)` 的展开行为。
5. 由于此项依赖 Docker 构建环境,实际计数值「待本地验证」;但 \(n^2\) 这一关系可直接由 [mma.scala#L34](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/mma/mma.scala#L34) 的 `Seq.fill(n * n)` 源码推出,无需运行也能确定。

> 说明:本实践要求**读者**在自己的工作副本上临时修改 `top.scala`。作为讲义生成方,我们**不会**改动仓库源码;读者改完后请自行还原。

#### 4.4.5 小练习与答案

- **练习 1**:如果把 `n` 从 32 改成 16,PE 数量变为多少?相对 32 减少了百分之几?
  - **答**:\(16^2=256\) 个 PE。相对 \(32^2=1024\),减少了 \((1024-256)/1024=75\%\)。注意 PE 数随 `n` 平方缩放,所以「边长减半」等于「面积变 1/4」。
- **练习 2**:`MMALU` 的第一个参数为什么写成 `=> BasePE`(按名参数)而不是 `BasePE`?
  - **答**:按名参数意味着「每次使用 `pe_gen` 时才重新求值」。`Seq.fill(n*n){ Module(pe_gen).io }` 需要创建 `n*n` 个**互相独立**的 `MMPE` 实例;若传一个已构造好的 `BasePE` 实例,所有 `Module(...)` 会指向同一个对象,无法生成多个独立 PE。按名参数保证每次 `Module(pe_gen)` 都新建一个硬件模块。

---

## 5. 综合实践

把本讲的三件事(读目录、读入口、看 elaborate 规模)串起来:

1. **画一张「源码到 Verilog」的全景图**:从 `src/main/scala/top/top.scala` 出发,标注 `object Main` → `ChiselStage.emitSystemVerilog(new MMALU(...))` → FIRRTL → `firtool` → `top.sv` 的完整链路;在 `MMALU` 节点上注明 `n=32, nbits=8, accum_nbits=32`,并标出 PE 数 \(n^2=1024\)。
2. **在图上用虚线圈出「当前未被 elaborate 的部分」**:`backend/SimpleBackend.scala` 里的 `NCoreBackend`(以及 `isa/`、`sram/`、`alu/vec/` 等只在 `NCoreBackend` 内部才被连起来的模块)。写一句话说明:为什么它们存在于源码、却不出现在当前 `top.sv` 里(答:elaborate 是「按需实例化」,`top.Main` 只 new 了 `MMALU`,没被 `new` 出来的类不会进入硬件图)。
3. **对照验证**:打开 [docs/index.md 的 Quick Start](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/index.md#L64-L80),确认 `make build` 这一步与你画的链路一致。

完成这张图后,你应该能用一句话向别人解释:「chisel-npu 的 RTL 是运行一段 Scala 程序生成的,当前这段程序只生成了 32×32 的 MMALU。」

## 6. 本讲小结

- `src/main/scala` 按「ISA → backend → alu → sram → top」自下而上组织;`src/test/scala` 与之镜像,且不进入 RTL。
- 顶层入口是 [top.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/top/top.scala) 里的 `object Main extends App`,`sbt run` 自动发现并执行它。
- `ChiselStage.emitSystemVerilog(new MMALU(...))` 完成「Scala → FIRRTL → SystemVerilog 字符串」,再由 `Files.write` 落盘成 `top.sv`。
- 当前 elaborate 的目标是 `MMALU(new MMPE(), n=32, nbits=8, accum_nbits=32)`,**不是**完整的 `NCoreBackend`。
- PE 数量 = \(n^2\);改 `n` 会平方级改变生成的 Verilog 规模,延迟则按 \(3n-1\) 线性变化。
- `top.sv`/`top.v` 是未被 git 跟踪的构建产物;`top.v`(Makefile 目标名)与 `top.sv`(实际产物)的不一致根因就在 `top.scala` 的落盘文件名。

## 7. 下一步学习建议

- 顺着「入口 elaborate 了什么」,自然进入 **U2 指令集架构(ISA)**:先看 [instrFormat.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala) 的 32 位指令编码,理解 `NCoreBackend` 将来要译码的对象。
- 想先看「当前 `top.sv` 里那个 `MMALU` 的内部」?可跳到 **U4 矩阵乘法引擎 MMALU**,从 [pe/procElem.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/pe/procElem.scala) 的 PE 开始读。
- 对「为什么完整后端没被 elaborate」感兴趣?阅读 [backend/SimpleBackend.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala) 的类签名,那将是 **U6 NCoreBackend 集成**的主题。
