# 第 1 讲:项目全景——PyPTO 是什么

> 本讲是 PyPTO 学习手册的第一讲,没有前置讲义。你只需要一台能读代码、能上网看文档的电脑。
> 本讲引用的所有源码均基于当前 HEAD `c7ba9fb0`,永久链接可直接在 GitHub 上打开核对。

## 1. 本讲目标

读完本讲,你应该能够:

1. 用自己的话说出 **PyPTO 的定位**:它是一个面向 AI 加速器的编程框架,核心工作是把 Python 写的算子/模型**编译**成芯片上高效执行的代码,而不是像普通 Python 那样解释执行。
2. 理解 **PTO(Parallel Tensor/Tile Operation)编程范式**与 **Tile 编程模型**的直觉:计算以「硬件感知的数据块(Tile)」为单位进行。
3. 区分三层抽象与三类使用者的对应关系:**算法开发者用 Tensor 级,性能专家用 Tile 级,系统开发者用 Block/ISA 级**。
4. 说出 PyPTO 生态五个仓库(`pypto`、`pypto-lib`、`PTOAS`、`pto-isa`、`simpler`)各自的职责,并能复述「一个 Python 函数 → 芯片上可执行代码」要经过哪几个阶段。

本讲刻意**不深入任何 C++ 实现细节**——那是第 4、5 单元的事。本讲只建立全景地图,让你在后续阅读源码时始终知道「我现在站在哪一层」。

## 2. 前置知识

本讲用到的概念都不难,逐一用大白话解释:

- **AI 加速器 / NPU**:专门为神经网络计算设计的芯片(如华为昇腾 Ascend)。它有很多并行计算核心和分层的片上存储,但「怎么把数据搬到核心旁边、怎么让成百上千个核心同时干活」需要专门的编程框架来打理。
- **Tensor(张量)**:多维数组,PyTorch 里的 `torch.Tensor` 就是它。在 PyPTO 里,Tensor 级数据通常指**放在设备全局内存(DDR)里的整块大数组**。
- **Tile(数据块)**:「硬件感知」的一小块数据——大小、摆放方式都贴合片上存储和计算单元的口味。计算发生在 Tile 上,才能吃满硬件的并行能力和内存层级。
- **编译器三件套**:前端(把源代码翻译成中间表示)、**IR(Intermediate Representation,中间表示)**——编译器内部对程序的数据结构描述、**Pass(编译遍)**——对 IR 做一次特定变换/优化的处理步骤、后端/CodeGen(把 IR 变成目标代码)。
- **Python 装饰器与类型注解**:`@pl.jit` 这样的写法是装饰器(在函数外面包一层逻辑);`a: pl.Tensor[[128, 128], pl.FP32]` 是类型注解,PyPTO 靠它读取形状与数据类型契约。
- **MPMD(Multiple Program Multiple Data,多程序多数据)**:与 SPMD(所有核心跑同一程序)相对,MPMD 指不同处理器上跑**不同的程序**:比如调度程序跑在 AICPU、计算内核跑在 AICore。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用法 |
| ---- | ---- | ---- |
| [README.md](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/README.md) | 项目定位、核心特性、目标用户、安装与示例入口 | 本讲主教材(英文,权威版本) |
| [README.zh-CN.md](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/README.zh-CN.md) | README 的中文翻译 | 对照阅读,帮助建立中文术语 |
| [docs/en/dev/00-ecosystem.md](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/00-ecosystem.md) | PTO 项目生态:五个仓库的分工、编译管线全景、仓库间接口 | 本讲第二主教材 |
| [docs/en/user/00-getting_started.md](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/00-getting_started.md) | 设备执行入门:常驻设备张量、显式派发、性能测量、分布式 | 远眺一眼「编译产物最终如何被派发到设备」 |
| `docs/en/user/02-quickstart.md`(辅助) | 用户快速上手:Tensor 级编程第一个算子 | 用真实代码佐证「分层抽象」 |
| `examples/beginner/01_hello_world.py`(辅助) | 最简单的 PyPTO 程序 | 用真实代码佐证「Tile 编程模型」 |

> 提示:项目文档约定**英文版 `docs/en/` 为权威版本**,中文版 `docs/zh/` 是镜像翻译(见 [README.zh-CN.md:L158](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/README.zh-CN.md#L158-L158))。学习时建议以英文为准、中文辅助理解。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块:**(1) PyPTO 的定位**、**(2) Tile 编程模型与 PTO 编程范式**、**(3) 分层抽象与三类开发者**、**(4) PTO 生态五仓库与编译管线**。

### 4.1 模块一:PyPTO 的定位——「Python 进,芯片指令出」的编译框架

#### 4.1.1 概念说明

很多人第一次看到「用 Python 写算子」会以为它像 NumPy 一样:每行 Python 立刻执行、立刻出结果。PyPTO 不是这样。

PyPTO 把你写的 Python 函数当作**待编译的源代码**:框架解析函数体,生成内部的中间表示(IR),再经过一串编译 Pass 逐步降级、优化,最后通过 CodeGen 生成底层的 **PTO 虚拟指令代码**,并进一步编译成目标平台上的可执行代码。运行时,这些可执行代码被装载到设备端,以 MPMD 方式调度到各个处理器核心上执行。

换句话说,PyPTO 之于 AI 加速器,大约相当于「一个带 Python 前端的编译器 + 运行时」,而不是「一个 Python 计算库」。

#### 4.1.2 核心流程

从高处看,一个 PyPTO 程序的生命周期是:

```text
你写的 Python DSL 程序(@pl.jit / @pl.program)
        │  ① 解析:Python 函数体 → 多级 IR
        ▼
IR(中间表示,Tensor/Tile/System 算子共存)
        │  ② Pass 流水线:内联、SSA、切分、降级、内存规划……
        ▼
降级后的 Tile 级 IR
        │  ③ CodeGen:生成 PTO 虚拟指令(.pto)与编排 C++
        │  ④ 汇编/编译:得到 AICore / AICPU 可执行产物
        ▼
设备执行(MPMD 调度,Host ↔ AICPU ↔ AICore 协同)
```

本讲只需要记住这条主线;每个环节的内部细节属于后续单元。

#### 4.1.3 源码精读

**定位句**:README 的 Overview 第一段一句话定义了整个项目——PyPTO 是面向 AI 加速器的高性能编程框架,采用 PTO 编程范式、以 Tile 编程模型为核心,通过多级 IR 把高层 Tensor 图逐步编译为硬件指令:

- [README.md:L7-L7](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/README.md#L7-L7) —— 项目定位总述:简化复杂融合算子与整网模型的开发,同时保持高性能;经多级 IR 从 Tensor 图逐步编译到硬件指令。
- [README.zh-CN.md:L7-L7](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/README.zh-CN.md#L7-L7) —— 同一句话的中文版,可作为中文术语的基准表述。

**七条核心特性**:README 用一个列表概括了框架能力,每一条都对应后续某个单元的主题:

- [README.md:L11-L17](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/README.md#L11-L17) —— 依次是:Tile 编程模型(L11)、多级计算图变换 **Tensor 图 → Tile 图 → Block 图 → 执行图**(L12)、自动代码生成(L13)、MPMD 执行调度(L14)、完整工具链支持(L15)、Python 友好 API(L16)、分层抽象设计(L17)。

注意 L12 这一条,它就是本讲综合实践里示意图的依据:分层变换的完整链条是 **Tensor Graph → Tile Graph → Block Graph → Execution Graph**,每一步都由一组 Pass 完成。

**文档地图**:README 末尾的表格告诉你四类文档分别讲什么,是后续学习的重要导航:

- [README.md:L153-L158](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/README.md#L153-L158) —— 用户手册(入门/语言/算子参考/调试)、PTO ISA 参考(集群架构、TPUSH/TPOP、buffer 管理)、开发者文档(IR、passes、代码生成、后端分派)、`simpler` 运行时文档。

#### 4.1.4 代码实践:跑通(或通读)Hello World

1. **实践目标**:亲手确认「PyPTO 程序长什么样、怎么运行」,把第 4.1.2 节的抽象流程落到一个真实文件上。
2. **操作步骤**:
   - 先完成安装(详见下一讲,这里只需知道入口):开发模式安装的推荐命令在 [README.md:L46-L58](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/README.md#L46-L58)(先装 CPU 版 torch 再 `pip install -e ".[dev]"`)。
   - 运行:`python examples/beginner/01_hello_world.py`,该入口同样列在 [README.md:L112-L116](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/README.md#L112-L116)。
   - 若暂时没有环境,直接通读 [examples/beginner/01_hello_world.py:L28-L47](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/01_hello_world.py#L28-L47) 亦可完成本实践(见下方观察点)。
3. **需要观察的现象**:
   - 程序文件顶部有一段「Concepts introduced」文档字符串,列出了 4 个概念(见 [examples/beginner/01_hello_world.py:L13-L17](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/01_hello_world.py#L13-L17)),与本讲 4.2、4.3 节一一对应。
   - `main` 块里用 `torch.allclose` 断言输出正确,成功则打印 `OK`(见 [examples/beginner/01_hello_world.py:L44-L47](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/01_hello_world.py#L44-L47))——**验证手段是拿 torch 结果做对照**,这是贯穿整本手册的基本功。
4. **预期结果**:终端输出 `OK`。首次运行会触发编译(耗时明显长于后续调用)。快速上手文档说明:入门级示例在普通 `pip install` 下即可运行,不需要 NPU,也不需要 ptoas 工具(见 [docs/en/user/02-quickstart.md:L6-L8](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/02-quickstart.md#L6-L8))。具体耗时与输出细节**待本地验证**(取决于是否已完成源码构建)。

#### 4.1.5 小练习与答案

**练习 1**:用一句话向同事解释 PyPTO 和 NumPy 的本质区别。
参考答案:NumPy 逐行解释执行 Python 代码;PyPTO 把 Python 函数体当作源代码**编译**成芯片指令再执行——Python 只是前端 DSL,不是执行引擎。

**练习 2**:README 列出的七条核心特性中,哪一条直接说明了「Tensor 图会经历多层图变换」?变换链条是什么?
参考答案:[README.md:L12](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/README.md#L12-L12) 的「Multi-level Computation Graph Transformation」;链条是 Tensor Graph → Tile Graph → Block Graph → Execution Graph,每步由一组 Pass 优化完成。

**练习 3**:本讲说「PyPTO 像一个带 Python 前端的编译器」。请从 README 的核心特性里找出两条支撑这个说法的证据。
参考答案:(a) L12-L13:通过编译 Pass 做多级图变换,CodeGen 生成底层 PTO 虚拟指令再编译为可执行代码;(b) L14:产物以 MPMD 方式调度到设备核心,说明执行发生在芯片上而非 Python 解释器里。

### 4.2 模块二:Tile 编程模型与 PTO 编程范式

#### 4.2.1 概念说明

**PTO 编程范式**的名字来自 Parallel Tensor/Tile Operation:并行地以 Tensor/Tile 为单位做运算。它落地为两个关键设计:

- **Tile 编程模型是核心**:所有计算基于 Tile——「硬件感知的数据块」,以此充分利用硬件并行计算能力和内存层级(见 [README.md:L11-L11](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/README.md#L11-L11))。直觉解释:芯片算得快、但数据搬运慢,Tile 的大小和摆放方式贴合片上存储,让数据「就在计算单元旁边」。
- **程序是「被解析」的,不是「被执行」的**:quickstart 开篇强调,PyPTO kernel 是 Python 源代码,`@pl.jit` 读取函数体并特化成 PyPTO IR,**在你编译并派发它之前什么都不会运行**(见 [docs/en/user/02-quickstart.md:L12-L14](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/02-quickstart.md#L12-L14))。

此外还有一个结构性约定:`@pl.jit` 标记的是**芯片级入口点**,属于控制面代码;真正的计算要放进 `with pl.at(level=pl.Level.CORE_GROUP):` 作用域——这个作用域声明「这段代码在芯片上执行」。去掉它会编译失败,报错 *"Misplaced tensor op ... should be inside InCore block"*(见 [docs/en/user/02-quickstart.md:L24-L28](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/02-quickstart.md#L24-L28) 与 [L89-L92](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/02-quickstart.md#L89-L92))。

#### 4.2.2 核心流程

一个显式操作 Tile 的计算作用域遵循经典的**三段式**:

```text
① load:  pl.load(全局内存 Tensor, 偏移, Tile 形状) → 把一块数据搬进片上 Tile
② compute: tile 级算子(t如 pl.add)在片上 Tile 上计算
③ store: pl.store(结果 Tile, 偏移, 目标 Tensor) → 写回全局内存
```

配合外层的两条机制:

- `@pl.jit` 装饰器:函数按输入 torch 张量的 **shape/dtype 特化 → 编译 → 缓存**(见 [examples/beginner/01_hello_world.py:L14-L14](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/01_hello_world.py#L14-L14))。
- `pl.Out[...]` 标注输出参数:输出通过**原地写回参数**交付,而不是返回新数组(见 [examples/beginner/01_hello_world.py:L16-L16](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/01_hello_world.py#L16-L16))。

#### 4.2.3 源码精读

Hello World 全文不到 50 行,是 Tile 编程模型的最小标本:

```python
@pl.jit
def tile_add(a: pl.Tensor, b: pl.Tensor, c: pl.Out[pl.Tensor]):
    with pl.at(level=pl.Level.CORE_GROUP):
        tile_a = pl.load(a, [0, 0], [128, 128])   # ① 从全局内存搬 128x128 块进片上
        tile_b = pl.load(b, [0, 0], [128, 128])
        tile_c = pl.add(tile_a, tile_b)           # ② 片上 Tile 计算
        pl.store(tile_c, [0, 0], c)               # ③ 写回输出 Tensor
    return c
```

- [examples/beginner/01_hello_world.py:L28-L35](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/01_hello_world.py#L28-L35) —— 上面这段代码的出处(注释为本讲义所加):`@pl.jit` 入口、`pl.at` 片上作用域、`pl.load/pl.add/pl.store` 三段式、`pl.Out` 输出参数,五行浓缩了 Tile 编程模型的全部要素。
- [examples/beginner/01_hello_world.py:L17-L17](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/01_hello_world.py#L17-L17) —— 官方注释直接点明类型语义:**Tensor 是全局内存数据,Tile 是片上寄存器数据**。这是初学者最容易混淆的一对概念。

同一份 Tensor→Tile 的世界观也出现在设备执行文档的示例里,可作为交叉印证:

- [docs/en/user/00-getting_started.md:L42-L48](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/00-getting_started.md#L42-L48) —— 另一个 `add_kernel`:同样的 `pl.at` + `pl.load/pl.store` 结构,说明这不是示例的偶然风格,而是语言层面的固定模式。

#### 4.2.4 代码实践

1. **实践目标**:在不运行代码的前提下,练出「一眼分清 Tensor 与 Tile」的能力,并理解 `@pl.jit` 的特化行为。
2. **操作步骤**:
   - 通读 [examples/beginner/01_hello_world.py:L28-L35](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/01_hello_world.py#L28-L35),给每个变量标注类型:`a/b/c` 是 Tensor(全局内存),`tile_a/tile_b/tile_c` 是 Tile(片上)。
   - 回答:第 31 行 `pl.load(a, [0, 0], [128, 128])` 的三个参数各是什么含义?(提示:结合三段式流程①。)
   - 再看 [examples/beginner/01_hello_world.py:L39-L42](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/01_hello_world.py#L39-L42):主程序准备了 `128x128` 的 torch 张量。思考:若改用 `(256, 256)` 的输入再次调用 `tile_add`,`@pl.jit` 会发生什么?
3. **需要观察的现象**:纯阅读实践,无运行现象;重点是自己能复述「load → compute → store 各自跨越了哪条内存边界」。
4. **预期结果**:三个参数依次是**源 Tensor、起始偏移 `[0, 0]`、Tile 形状 `[128, 128]`**;输入 shape 改变后,由于 `@pl.jit` 按 shape/dtype 特化并缓存,`(256, 256)` 会触发**一次新的编译**(缓存键不同)。此推断依据 [examples/beginner/01_hello_world.py:L14](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/01_hello_world.py#L14-L14) 对特化行为的官方注释,具体缓存细节在第 2 单元第 1 讲展开、**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**:「PyPTO kernel 是被解析的,不是被执行的」——这句话对刚接触 PyPTO 的 Python 用户意味着什么坑?
参考答案:函数体里的代码不是即时运行的 Python;你在函数体里写的 `print`、随意插入的 Python 副作用都不会按「解释执行」的直觉工作。应当把函数体当作 DSL 源码来写(依据 [docs/en/user/02-quickstart.md:L12-L14](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/02-quickstart.md#L12-L14))。

**练习 2**:为什么计算必须放在 `with pl.at(level=pl.Level.CORE_GROUP):` 里?这个作用域声明了什么?
参考答案:`@pl.jit` 入口是控制面(编排调度)代码,`pl.at` 作用域声明「这段计算在芯片执行单元上执行」,把计算移到执行面;缺了它编译会报 *"Misplaced tensor op ... should be inside InCore block"*(依据 [docs/en/user/02-quickstart.md:L24-L28](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/02-quickstart.md#L24-L28)、[L89-L92](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/02-quickstart.md#L89-L92))。

**练习 3**:输出为什么用 `pl.Out[...]` 参数交付而不是 `return` 一个新数组?
参考答案:`pl.Out` 声明参数方向为「写出」,告诉编译器/运行时这个缓冲区会被写入,从而决定调用前是否上传、调用后是否拷回;这也是设备端常见的原地写回模式(依据 [examples/beginner/01_hello_world.py:L16](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/01_hello_world.py#L16-L16) 及 [docs/en/user/02-quickstart.md:L84-L88](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/02-quickstart.md#L84-L88))。

### 4.3 模块三:分层抽象——同一件事的三种写法与三类开发者

#### 4.3.1 概念说明

PyPTO 的一个核心设计选择是:**同一套框架向不同人暴露不同抽象层级**(见 [README.md:L17-L17](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/README.md#L17-L17)),并据此划分目标用户(见 [README.md:L19-L23](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/README.md#L19-L23)):

| 抽象层级 | 面向谁 | 关心的东西 | 典型写法 |
| ---- | ---- | ---- | ---- |
| **Tensor 级**(全局内存整块数组) | 算法开发者 | 算法逻辑,快速实现与验证 | 对整个 Tensor 施加算子,不写数据搬运 |
| **Tile 级**(片上数据块) | 性能优化专家 | 分块大小、数据搬运、片上驻留 | 显式 `pl.load / pl.store` 三段式 |
| **Block 级 / PTO 虚拟指令集级** | 系统开发者 | 指令、调度、工具链、第三方框架集成 | 对接 pto-isa 指令集与运行时 API |

关键在于:**层级之间是编译器衔接的,不是人工翻译的**。Tensor 级代码会被编译器的 Pass 自动降级成 Tile 级代码——这既是「算法开发者写得省」的底气,也是「性能专家可深挖」的空间。

#### 4.3.2 核心流程

以「两个 128×128 张量相加」为例,两种层级的写法对照:

```text
Tensor 级(quickstart 版)                 Tile 级(hello world 版)
─────────────────────────                ─────────────────────────
with pl.at(level=CORE_GROUP):            with pl.at(level=CORE_GROUP):
    out[:] = pl.add(a, b)                    tile_a = pl.load(a, [0,0], [128,128])
    # 没有 load/store,                       tile_b = pl.load(b, [0,0], [128,128])
    # 没有偏移,没有形状                        tile_c = pl.add(tile_a, tile_b)
                                             pl.store(tile_c, [0,0], c)
编译器自动补全切分与搬运 ─────────────────►  数据搬运全部手工显式
(ConvertTensorToTileOps Pass)
```

两条路径最终殊途同归:Tensor 级版本经过 `ConvertTensorToTileOps` 这个 Pass 后,`pl.load/pl.store` 与切分逻辑会被自动插入(见 [docs/en/user/02-quickstart.md:L80-L82](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/02-quickstart.md#L80-L82)),效果可以在编译产物的 pass dump 目录 `compiled.output_dir/passes_dump/` 里亲眼看到。

#### 4.3.3 源码精读

**Tensor 级的最小算子**(quickstart):

- [docs/en/user/02-quickstart.md:L51-L67](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/02-quickstart.md#L51-L67) —— `add` 函数:注解里写明 `pl.Tensor[[128, 128], pl.FP32]` 的形状契约;函数体只有一行 `out[:] = pl.add(a, b)`——没有 tile 类型、没有 `pl.load/pl.store`、没有内存空间概念。
- [docs/en/user/02-quickstart.md:L70-L78](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/02-quickstart.md#L70-L78) —— 官方逐行注解表:解释了 `@pl.jit`、形状注解、`pl.Out` 方向、`pl.at` 作用域、`add.compile()` 各自行的作用,是初学者最好的「字对字」参考。
- [docs/en/user/02-quickstart.md:L80-L82](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/02-quickstart.md#L80-L82) —— 点破魔术的段落:Tensor 级 kernel 里「没有的东西」由 `ConvertTensorToTileOps` Pass 全部插入,产物可在 `passes_dump/` 目录查看。

**Tile 级的同一算子**(hello world):

- [examples/beginner/01_hello_world.py:L30-L35](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/01_hello_world.py#L30-L35) —— 同样是加法,但偏移 `[0, 0]`、Tile 形状 `[128, 128]`、load/store 时机全部由程序员掌控——这就是性能专家的工作面。

**系统开发者的视角**(指令集层):

- [docs/en/dev/00-ecosystem.md:L127-L136](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/00-ecosystem.md#L127-L136) —— `pto-isa` 仓库定义目标硬件的 tile 级指令集(C++ 头文件声明 load/store/计算/同步等硬件指令),供 PTOAS 与 simpler 消费——这是 Block/ISA 级开发者打交道的世界。

#### 4.3.4 代码实践

1. **实践目标**:通过并排对照两种层级的真实代码,体会「分层抽象不是口号,而是同一功能的两份可运行源码」。
2. **操作步骤**:
   - 左边打开 [docs/en/user/02-quickstart.md:L51-L67](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/02-quickstart.md#L51-L67)(Tensor 级),右边打开 [examples/beginner/01_hello_world.py:L28-L35](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/01_hello_world.py#L28-L35)(Tile 级)。
   - 自己动手列一张三列对照表:**「代码元素 | 只出现在 Tensor 级 | 只出现在 Tile 级」**,至少各填 3 行。
   - 思考题:两份代码的 `pl.at(level=pl.Level.CORE_GROUP)` 都能省吗?
3. **需要观察的现象**:纯阅读实践;重点是发现「两份代码的框架骨架(jit + at + Out)完全相同,差异集中在作用域内部」。
4. **预期结果**:对照表示例——Tensor 级独有:`out[:] = ...` 整体赋值、形状写在参数注解、无数据搬运语句;Tile 级独有:`pl.load/pl.store`、偏移量 `[0, 0]`、显式 Tile 形状、中间 Tile 变量。`pl.at` 两边都**不能**省(依据 [docs/en/user/02-quickstart.md:L89-L92](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/02-quickstart.md#L89-L92))。

#### 4.3.5 小练习与答案

**练习 1**:一位算法工程师只想快速验证一个融合算子的数值正确性,该选哪一层?为什么?
参考答案:Tensor 级。按 [README.md:L21-L21](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/README.md#L21-L21),算法开发者主要用 Tensor 级做快速实现与验证、专注算法逻辑;数据搬运与切分交给编译器(quickstart L80-L82)。

**练习 2**:Tensor 级代码里没有 `pl.load`,那数据搬运是谁做的?去哪里能「亲眼看到」?
参考答案:编译器的 `ConvertTensorToTileOps` Pass 自动插入;可在编译产物目录 `compiled.output_dir/passes_dump/` 下的逐 Pass 转储中看到(依据 [docs/en/user/02-quickstart.md:L80-L82](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/02-quickstart.md#L80-L82))。

**练习 3**:README 说分层抽象「向不同开发者暴露不同层级」。结合生态文档,系统开发者在 Block/ISA 级接触的具体是什么?
参考答案:PTO 虚拟指令集层面的东西——例如 `pto-isa` 定义的 tile 级硬件指令头文件(load/store/计算/同步),以及编排侧的 PTO2 运行时 API;用于与第三方框架集成或开发工具链(依据 [README.md:L23-L23](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/README.md#L23-L23)、[docs/en/dev/00-ecosystem.md:L127-L136](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/00-ecosystem.md#L127-L136))。

### 4.4 模块四:PTO 生态——五个仓库与一条编译管线

#### 4.4.1 概念说明

你现在读的 `pypto` 仓库只是 PTO 工具链的一环。PTO 是**多仓库**生态,从 Python 张量程序一路贯穿到硬件指令执行(见 [docs/en/dev/00-ecosystem.md:L1-L7](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/00-ecosystem.md#L1-L7)):

| 仓库 | 角色 | 一句话职责 |
| ---- | ---- | ---- |
| **pypto** | 编译框架 | Python DSL → IR → Pass → CodeGen(本仓库) |
| **pypto-lib** | 模型库 | 真实模型与基础张量函数,基于 pypto 编译 |
| **PTOAS** | 汇编器与优化器 | 把 `.pto` MLIR 变成调用 pto-isa 指令的 C++ |
| **pto-isa** | 指令集架构 | tile 级指令集的 C++ 头文件定义 |
| **simpler** | 任务运行时 | 在设备上执行编译产物,协调 Host ↔ AICPU ↔ AICore |

出处:[docs/en/dev/00-ecosystem.md:L13-L19](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/00-ecosystem.md#L13-L19)。

生态的关键设计是**仓库边界上都有明确定义的接口契约**:`.pto` 文件是 pypto 与 PTOAS 之间的契约(两边必须对 PTO MLIR 方言达成一致),PTO2 运行时 API 是 pypto 编排代码生成与 simpler 之间的契约(依据 [docs/en/dev/00-ecosystem.md:L169-L187](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/00-ecosystem.md#L169-L187))。

#### 4.4.2 核心流程

生态文档用一张 ASCII 图描绘了完整编译管线(原文见 [docs/en/dev/00-ecosystem.md:L23-L60](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/00-ecosystem.md#L23-L60))。下面是本讲义根据原文重绘的简化版:

```text
pypto-lib(真实模型,消费 pypto 框架)
    │  import 并通过 pypto 编译
    ▼
pypto:Python DSL → IR → Pass 流水线 → CodeGen
    │
    ├── InCore 函数(tile 级计算)──► .pto 文件(PTO-ISA MLIR)
    │        └► PTOAS 汇编优化 ──► 生成 C++(内含 pto-isa 指令)──► AICore 二进制
    │
    └── 编排函数(任务调度)──► 编排 C++(调用 PTO2 运行时 API)──► AICPU 二进制
                                  │
                                  ▼
                    simpler 运行时:任务依赖图执行,
                    Host ↔ AICPU ↔ AICore 三程序协同
```

要点三条:

1. **pypto 有两条 CodeGen 出口**(见 [docs/en/dev/00-ecosystem.md:L62-L65](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/00-ecosystem.md#L62-L65)):InCore 函数走 `.pto` → PTOAS → pto-isa → AICore;编排函数走 PTO2 运行时 API 的 C++ → AICPU。这正对应 4.3 节「执行面计算」与「控制面调度」的分离。
2. **IR 是多级的、共存的**:Tensor 算子、Tile 算子和系统算子共存于同一棵 IR;Pass 流水线(文档写作「20+ passes」,准确数量在后续单元结合 pass_manager 统计)逐步把 Tensor 级降到 Tile 级(见 [docs/en/dev/00-ecosystem.md:L82-L88](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/00-ecosystem.md#L82-L88))。
3. **`.pto` 里的指令长什么样**:PTO MLIR 方言包含 `pto.tload`、`pto.tmul`、`pto.alloc_tile` 这类操作(见 [docs/en/dev/00-ecosystem.md:L125-L125](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/00-ecosystem.md#L125-L125))——第 6 单元精读 CodeGen 时会与它们正面相遇。

#### 4.4.3 源码精读

**pypto 仓库自身的输入输出与内部结构**:

- [docs/en/dev/00-ecosystem.md:L73-L78](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/00-ecosystem.md#L73-L78) —— 输入是用 `pypto.language` DSL(`@pl.program`/`@pl.function`)写的 Python 程序;输出是 `.pto` 文件(每个 InCore 内核函数一份)与编排 C++(跑在 AICPU)。
- [docs/en/dev/00-ecosystem.md:L92-L98](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/00-ecosystem.md#L92-L98) —— 仓库关键目录表,预告了本手册后续单元的地图:`include/pypto/ir/`(C++ IR 节点定义,单元 4)、`src/ir/transforms/`(编译 Pass,单元 5)、`src/codegen/`(PTO 与编排代码生成器,单元 6)、`python/pypto/language/`(Python DSL 前端,单元 2)、`python/pypto/ir/`(Pass 管理器与编译 API,单元 3)。

**下游两个环节的契约**:

- [docs/en/dev/00-ecosystem.md:L111-L125](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/00-ecosystem.md#L111-L125) —— PTOAS:解析 `.pto` 的 PTO-ISA MLIR 方言,做 PTO 级优化(同步插入、内存规划),lower 成调用 pto-isa 指令的 C++。
- [docs/en/dev/00-ecosystem.md:L152-L167](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/00-ecosystem.md#L152-L167) —— simpler:在昇腾硬件上执行编译产物,管理 Host/AICPU 内核/AICore 内核**三程序执行模型**,构建任务依赖图;pypto 生成的编排 C++ 调用其实现的 PTO2 运行时 API(如 `rt_submit_task`、`make_tensor_external`)。

**运行时接口的一瞥**(连接本讲与真实 API):

- [docs/en/user/00-getting_started.md:L32-L37](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/00-getting_started.md#L32-L37) —— 用户侧运行时入口:`from pypto.runtime import ChipWorker, RunConfig`——编译产物最终通过 `ChipWorker` 被派发到设备;`ir.compile(...)` 则是编译入口(见 [L58-L58](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/00-getting_started.md#L58-L58))。这两个名字在第 3 单元会反复出现。

#### 4.4.4 代码实践

1. **实践目标**:把 4.4.2 的管线图内化成一张「阶段追踪表」,能对任意阶段说出「在哪个仓库、输入什么、输出什么」。
2. **操作步骤**:
   - 通读 [docs/en/dev/00-ecosystem.md:L21-L65](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/00-ecosystem.md#L21-L65)(管线图与两条 CodeGen 路径)。
   - 手绘或用表格填写 6 行:「①DSL 解析成 IR / ②Pass 流水线 / ③CodeGen(InCore)/ ④PTOAS 汇编 / ⑤编排 C++ 编译 / ⑥simpler 执行」,每行三列:所在仓库、输入、输出。
3. **需要观察的现象**:纯阅读实践;检验标准是不看原文也能填满表格。
4. **预期结果**(参考答案):

   | 阶段 | 所在仓库 | 输入 | 输出 |
   | ---- | ---- | ---- | ---- |
   | ①DSL → IR | pypto | `@pl.program`/`@pl.function` Python 程序 | 多级 IR(不可变树) |
   | ②Pass 流水线 | pypto | Tensor 级 IR | Tile 级(降级后)IR |
   | ③CodeGen(InCore) | pypto | Tile 级 IR | `.pto` 文件(PTO-ISA MLIR) |
   | ④汇编优化 | PTOAS | `.pto` 文件 | 调用 pto-isa 指令的 C++ → AICore 二进制 |
   | ⑤编排编译 | pypto(生成)+ 设备编译器 | 编排 IR | 使用 PTO2 运行时 API 的 C++ → AICPU 二进制 |
   | ⑥设备执行 | simpler | AICore/AICPU 二进制 | 任务图执行(Host↔AICPU↔AICore) |

   依据:[docs/en/dev/00-ecosystem.md:L62-L65](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/00-ecosystem.md#L62-L65)、[L73-L88](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/00-ecosystem.md#L73-L88)、[L111-L125](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/00-ecosystem.md#L111-L125)、[L152-L167](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/00-ecosystem.md#L152-L167)。

#### 4.4.5 小练习与答案

**练习 1**:`pypto-lib` 和 `pypto` 之间有没有专门的私有接口?
参考答案:没有。pypto-lib 是 pypto 框架的普通消费者:用同一套 `@pl.program`/`@pl.function` DSL 写程序、经同一条流水线编译,只是 import 了 `pypto.language` 并调用 `pypto.ir.compile`(依据 [docs/en/dev/00-ecosystem.md:L100-L109](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/00-ecosystem.md#L100-L109))。

**练习 2**:为什么说「`.pto` 文件是 pypto 与 PTOAS 之间的契约」?如果两边对方言定义不一致会怎样?
参考答案:pypto 的 PTO CodeGen 按 PTO MLIR 方言发射(如 `pto.tload`/`pto.tmul`/`pto.alloc_tile`),PTOAS 按同一方言解析;生态文档明确「两个仓库必须对 PTO MLIR 方言定义达成一致」(依据 [docs/en/dev/00-ecosystem.md:L125-L125](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/00-ecosystem.md#L125-L125)),不一致则 PTOAS 无法正确解析 `.pto`。

**练习 3**:「新增一条 tile 指令」要动哪几个仓库?这个事实说明了什么?
参考答案:pto-isa + PTOAS + pypto 三个仓库(ISA 头文件、PTO MLIR 方言、pypto 的算子与 codegen 都受影响),依据跨仓开发对照表 [docs/en/dev/00-ecosystem.md:L189-L199](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/00-ecosystem.md#L189-L199);说明生态通过清晰的接口契约分层协作,改动沿接口边界传播,而不是混在一起。

## 5. 综合实践

**任务**:阅读 README 与 ecosystem 文档后,用自己的话写一段约 200 字的总结——「PyPTO 把一个 Python 函数变成芯片上可执行代码要经过哪几个阶段」,并画出 Tensor Graph → Tile Graph → 执行代码的分层示意图。

1. **实践目标**:把本讲 4 个模块的信息压缩成一段自己能随时复述的话 + 一张图,作为后续所有讲义的「定位罗盘」。
2. **操作步骤**:
   - 重读 [README.md:L7-L17](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/README.md#L7-L17)(定位与核心特性,注意 L12 的图变换链条)与 [docs/en/dev/00-ecosystem.md:L21-L65](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/00-ecosystem.md#L21-L65)(编译管线与两条 CodeGen 路径)。
   - **先不看任何参考答案**,写下自己的 200 字总结(建议手写或记在笔记里),必须覆盖:解析成 IR、Pass 降级、两条 CodeGen 出口、运行时执行。
   - 画分层示意图:顶层标 Tensor Graph,中间标 Tile Graph(可加 Block Graph),底层标执行代码,并用箭头标注每层之间「谁(哪个仓库/哪类 Pass)负责变换」。
   - 用下面的自查清单核对。
3. **需要观察的现象**:写作过程中你会发现自己哪一环说不清楚——那正是需要回读对应小节(4.1 定位 / 4.2 Tile 模型 / 4.4 生态管线)的信号。
4. **预期结果**:自查清单(全部满足即通过)——
   - [ ] 提到「Python DSL 被**解析**(而非解释执行)成 IR」;
   - [ ] 提到 Pass 流水线把 Tensor 级**降级**到 Tile 级(切分、内存规划等);
   - [ ] 提到**两条** CodeGen 出口:InCore → `.pto`(走 PTOAS,AICore)与编排 → C++/PTO2 API(AICPU);
   - [ ] 提到最终由 simpler 运行时以 **MPMD/三程序模型**调度执行;
   - [ ] 图中 Tensor Graph 在最上、执行代码在最下,且箭头上有仓库/Pass 标注。

**参考范例**(写完自己的版本后再对照;约 200 字):

> PyPTO 把用 Python DSL 写的函数先**解析**成不可变的多级 IR,Tensor 算子、Tile 算子与系统算子共存于同一棵 IR 树。随后 Pass 流水线逐步把它从 Tensor 级**降级**到 Tile 级,完成内联、SSA、自动切分与内存规划。CodeGen 有两条出口:InCore 函数生成 `.pto`(PTO-ISA MLIR),经 PTOAS 汇编并配合 pto-isa 指令头文件得到 AICore 二进制;编排函数生成调用 PTO2 运行时 API 的 C++,编译到 AICPU。最终由 simpler 运行时按 MPMD 构建 Host-AICPU-AICore 三程序任务图,在芯片上调度执行。

参考示意图:

```text
┌─────────────────────────────────────────────────────────────┐
│ Tensor Graph   算法视角:@pl.jit / @pl.program + tensor 算子 │
└───────────────────────────┬─────────────────────────────────┘
                            │ pypto:DSL 解析 → 多级 IR;
                            │ Pass 流水线降级(切分/SSA/内存规划)
┌───────────────────────────▼─────────────────────────────────┐
│ Tile Graph     性能视角:pl.load / tile 计算 / pl.store 三段式│
└──────┬────────────────────────────────────┬─────────────────┘
       │ CodeGen(InCore)                    │ CodeGen(编排)
       ▼                                    ▼
  .pto(PTO-ISA MLIR)                 编排 C++(PTO2 运行时 API)
       │ PTOAS 汇编 + pto-isa 指令          │ 设备编译
       ▼                                    ▼
  AICore 二进制                          AICPU 二进制
       └───────────────┬────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────────┐
│ Execution Graph  simpler 运行时:MPMD 任务图,               │
│                   Host ↔ AICPU ↔ AICore 三程序协同执行       │
└─────────────────────────────────────────────────────────────┘
```

(图中各环节的依据:README L12 的 Tensor→Tile→Block→Execution 图变换链条;ecosystem L62-L65 的两条 CodeGen 路径;L152-L167 的三程序执行模型。)

## 6. 本讲小结

- **PyPTO 是编译框架,不是 Python 计算库**:Python DSL 被解析成多级 IR,经 Pass 流水线降级、CodeGen 生成 PTO 虚拟指令,最终在设备上以 MPMD 方式执行。
- **Tile 编程模型是核心**:Tensor 是全局内存的整块数组,Tile 是硬件感知的片上数据块;显式写法遵循 `pl.load → tile 计算 → pl.store` 三段式,计算必须放在 `pl.at(level=...)` 作用域内。
- **分层抽象对应三类开发者**:算法开发者(Tensor 级)写得省,性能专家(Tile 级)挖得深,系统开发者(Block/ISA 级)管指令与工具链;层级之间由编译 Pass(如 `ConvertTensorToTileOps`)自动衔接。
- **pypto 是五仓库生态的一环**:`pypto`(编译)→ `PTOAS`(汇编)→ `pto-isa`(指令集)→ `simpler`(运行时),外加消费方 `pypto-lib`;仓库边界靠 `.pto` 方言与 PTO2 运行时 API 两个契约衔接。
- **验证正确性的基本套路**:用 torch 生成输入与期望输出,`torch.allclose` 对照断言——从 hello world 开始就是如此。

## 7. 下一步学习建议

下一讲(**u1-l2 构建与环境搭建**)将解决「让这套框架在你机器上真正跑起来」的问题:scikit-build-core + CMake 的构建体系、开发模式安装、CPU 版 torch 依赖技巧,以及用 pytest 跑通单元测试。建议在进入下一讲前:

1. 通读 [examples/beginner/01_hello_world.py](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/01_hello_world.py) 全文(不到 50 行),确认 4.2 节的每个概念都能在代码里指出来。
2. 浏览 [docs/en/dev/00-ecosystem.md:L90-L98](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/00-ecosystem.md#L90-L98) 的目录表,提前眼熟 `include/pypto/ir/`、`src/ir/transforms/`、`src/codegen/`、`python/pypto/language/` 这几个目录名——它们是整本手册的常驻地名。
3. 有余力的读者可以扫一眼 [docs/en/user/02-quickstart.md](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/02-quickstart.md),它展示了比本讲更多的 Tensor 级写法细节,是第 2 单元(Python DSL 语言基础)的预习材料。
