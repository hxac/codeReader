# 首次运行：生成第一个 testbench

## 1. 本讲目标

在前两讲里，我们已经知道 TbGenerator 是「把带 `$$ ... $$` 注解的 VHDL DUT 文件，自动变成 testbench 骨架」的工具，也理清了仓库结构与入口文件。本讲要做一件最实在的事：**真正跑通一次生成，并看懂产出的 testbench 文件长什么样**。

学完本讲，你应当能够：

- 用 `-src` / `-dst` / `-clear` / `-force` 这套命令行参数，独立运行一次 TbGenerator；
- 说清楚一次生成内部经历了哪两大步骤（先 `ReadHdl` 读，再 `Generate` 写）；
- 打开生成的 `*_tb.vhd` 文件后，能迅速定位 **DUT 实例化（`i_dut`）**、**时钟进程（`p_clock_*`）** 以及 **测试进程（如 `p_Input` / `p_Output`）** 这几个关键段落，并知道它们分别由哪段源码产生。

本讲只聚焦「跑通 + 看懂骨架」，标签语法的细节、VHDL 解析的内部原理会在后续单元（u2、u3）展开。

## 2. 前置知识

在开始之前，请确认你理解下面几个概念（前两讲已建立）：

- **DUT（Design Under Test，被测设计）**：我们要测试的那个 VHDL 模块，本讲的例子是一个异步 FIFO（`psi_common_async_fifo`）。
- **Testbench（测试台 / TB）**：一段专门用来给 DUT 喂输入、观察输出的 VHDL 代码。它本身一般不可综合，只在仿真器里跑。
- **`$$ ... $$` 注解标签**：写在 VHDL 注释里的特殊标记，TbGenerator 靠它们知道「哪个端口是时钟」「时钟频率多少」「端口归到哪个测试过程」等信息。本讲你只需要会用，不需要懂它的解析原理。
- **CLI（命令行接口）**：通过命令行参数（如 `-src`、`-dst`）来驱动程序的方式。TbGenerator 的 CLI 入口是 `TbGen.py`。

另外，请确保本机已安装依赖（见 u1-l1）：Python 库 `PsiPyUtils`（≥3.0.0）以及 pip 包 `pyparsing`。本讲的运行实践依赖它们；若未安装，可先按「源码阅读型实践」完成理解，再补装后实跑。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [`example/simpleTb/run.bat`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/run.bat#L1) | 一个 Windows 批处理脚本，封装了一条调用 `TbGen.py` 的完整命令，是最省事的运行方式。 |
| [`example/simpleTb/psi_common_async_fifo.vhd`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd) | 示例 DUT 文件：一个异步 FIFO。里面用 `$$ ... $$` 标注了时钟、复位、进程归属等信息，是本讲的「输入」。 |
| [`TbGen.py`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py) | 核心引擎。既定义了 `TbGenerator` 类（`ReadHdl` + `Generate`），又在文件末尾用 `if __name__ == '__main__':` 提供了 CLI。本讲的三个最小模块全部来自这里。 |
| [`DutInfo.py`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py) | DUT 的数据模型。`ReadHdl` 内部会构造它。本讲只用到它的几个属性（`name`、`generics`、`ports`、`dutLibrary`）。 |
| [`TbInfo.py`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py) | testbench 的数据模型。`ReadHdl` 内部会构造它，决定生成文件名、进程名等。 |

> 提示：`DutInfo.py` 与 `TbInfo.py` 的内部细节是 u2、u4 的内容，本讲只取我们「跑通一次」所必需的那一点点。

## 4. 核心概念与源码讲解

### 4.1 运行方式：CLI 参数与示例输入

#### 4.1.1 概念说明

TbGenerator 的引擎是一个有状态的类 `TbGenerator`（详见 u1-l2），它对外只暴露两步：先「读」、再「写」。但作为使用者，你通常不会自己写 Python 去调它，而是直接用现成的命令行入口 `TbGen.py`。这个文件末尾有一段 `if __name__ == '__main__':` 守卫代码，负责把命令行参数翻译成对引擎的调用。

最省事的运行方式，是用示例目录里已经写好的 `run.bat`。我们先看它到底执行了什么命令。

#### 4.1.2 核心流程

一条完整的 CLI 调用，内部按下面顺序执行：

```text
run.bat
  └─> py TbGen.py -src <DUT文件> -dst <输出目录> [-clear] [-mrg] [-force]
        └─> argparse 解析 5 个参数
              └─> 校验 -src 是真实文件
                    └─> 若 -clear：清空输出目录（无 -force 则交互确认）
                          └─> 若输出目录不存在：创建它
                                └─> new TbGenerator() → ReadHdl(src) → Generate(dst, ext, overwrite)
```

注意最后一步：**CLI 只是把参数拼好，真正的活儿仍然交给 `TbGenerator` 的 `ReadHdl` 与 `Generate`**（这两个方法是 4.2、4.3 的主角）。这印证了 u1-l2 的结论——外壳（CLI）与引擎分离，业务逻辑只有一份。

#### 4.1.3 源码精读

先看示例的 `run.bat`，它只有一行：

```bat
py ..\..\TbGen.py -src .\psi_common_async_fifo.vhd -dst .\tb -clear -force
```

这一行：以 `example/simpleTb/` 为当前目录，向上两级找到仓库根的 `TbGen.py`；把当前目录的 `psi_common_async_fifo.vhd` 作为 DUT；把结果输出到当前目录的 `tb/` 子目录；并带上 `-clear -force`（清空目标目录且无需交互确认）。详见 [example/simpleTb/run.bat:1](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/run.bat#L1)。

这 5 个参数在 `TbGen.py` 末尾用 `argparse` 声明：

```python
parser.add_argument("-src", dest="src", help="VHDL source file", required=True)
parser.add_argument("-dst", dest="dst", help="TB destination directory", required=True)
parser.add_argument("-clear", dest="clear", ..., default=False, action="store_true")
parser.add_argument("-mrg",  dest="mrg",  ..., default=False, action="store_true")
parser.add_argument("-force", dest="force", ..., default=False, action="store_true")
```

> 见 [TbGen.py:264-269](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L264-L269)。

参数含义一览：

| 参数 | 是否必需 | 作用 |
| --- | --- | --- |
| `-src` | 必需 | VHDL 源文件（DUT）路径。 |
| `-dst` | 必需 | testbench 的输出目录。 |
| `-clear` | 可选（flag） | 生成前清空 `-dst` 目录。 |
| `-force` | 可选（flag） | 与 `-clear` 配合：跳过「是否清空」的交互确认，直接清。单独使用没有清除效果。 |
| `-mrg` | 可选（flag） | 生成 `.mrg` 合并文件而非 `.vhd`，且允许覆盖既有文件（高级用法，本讲不用，详见 u6-l2）。 |

`-clear` 与 `-force` 的协作值得注意：只有当 `-clear` 被指定、且目标目录已存在时，程序才会进入清除流程；此时若没有 `-force`，会用 `input(...)` 弹出 `Y/N` 确认，输错即中止。这段逻辑在 [TbGen.py:277-294](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L277-L294)，关键片段：

```python
if args.clear:
    if os.path.exists(args.dst):
        if not args.force:
            i = input("Path '{}' exists, do you really want to clear it (Y/N)".format(args.dst))
            if i not in ["Y", "y"]:
                print("Aborted by user"); exit(0)
        for file in os.listdir(args.dst):       # 仅删文件，不递归删子目录
            ... os.remove(fp)
```

清完（或无需清）之后，若 `-dst` 目录不存在则创建它（[TbGen.py:296-299](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L296-L299)），随后才进入真正的生成：

```python
tbGen = TbGenerator()
tbGen.ReadHdl(args.src)
extension = ".vhd"
if args.mrg:
    extension = ".mrg"
tbGen.Generate(args.dst, extension, overwrite=args.mrg)
```

> 见 [TbGen.py:302-314](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L302-L314)。注意 `overwrite` 只在 `-mrg` 时为 `True`——也就是说普通 `.vhd` 生成默认**不覆盖**同名文件。

最后顺便认识一下「输入」长什么样。示例 DUT 的开头几行（[example/simpleTb/psi_common_async_fifo.vhd:23](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L23) 和 [:39](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L39)）：

```vhdl
-- $$ PROCESSES=Input,Output $$          -- 文件级标签：声明两个测试过程

entity psi_common_async_fifo is
    ...
    InClk : in std_logic; -- $$ TYPE=CLK; FREQ=100e6; PROC=Input $$   -- 端口标签
    ...
```

现在你只需记住三件事，它们会在 4.3 的输出里一一对应：

1. 文件级有 `PROCESSES=Input,Output`（所以测试进程叫 `Input`、`Output`，**不是** `Stimuli`）；
2. `InClk` / `OutClk` 是两个时钟，频率分别是 `100e6`、`125e6`；
3. 实体名是 `psi_common_async_fifo`。

#### 4.1.4 代码实践

**实践目标**：在不依赖 Windows `py` 启动器的前提下，手动拼出等价的运行命令并理解每一段。

**操作步骤**：

1. 进入 `example/simpleTb/` 目录。
2. 把 `run.bat` 里的命令「翻译」成你本机的等价命令。例如在 Linux/macOS 或没有 `py` 启动器时：

   ```bash
   python /path/to/TbGen.py -src ./psi_common_async_fifo.vhd -dst ./tb -clear -force
   ```

   （把 `/path/to/` 换成仓库根的真实路径，即 `run.bat` 里 `..\..\` 所指向的位置。）
3. 若依赖已安装，运行后会看到类似 `Read HDL` → `Generate TB` → `Done` 的打印（来自 [TbGen.py:303-311](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L303-L311)）。
4. 用 `python TbGen.py -h` 查看 argparse 自动生成的帮助，对照上表核对每个参数。

**需要观察的现象**：

- 不带 `-force` 而输出目录已存在时，程序会停在 `Y/N` 确认；输入 `n` 会打印 `Aborted by user` 并退出（[TbGen.py:282-284](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L282-L284)）。
- `-src` 指向一个不存在的文件时，会打印 `ERROR: -src path ... is not a file` 并 `exit(-1)`（[TbGen.py:273-275](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L273-L275)）。

**预期结果**：`example/simpleTb/tb/` 目录被（重新）创建，其中多出一个 `psi_common_async_fifo_tb.vhd` 文件。文件名如何得来，见 4.2 与 4.3。

> 若本机未安装 `PsiPyUtils` / `pyparsing`，运行会报 `ModuleNotFoundError`，这一步的实际执行结果**待本地验证**；可先继续后面的源码阅读型实践。

#### 4.1.5 小练习与答案

**练习 1**：如果只写 `-clear` 不写 `-force`，而 `tb/` 目录已经存在并含有旧文件，会发生什么？

> **答案**：程序会进入 [TbGen.py:280-284](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L280-L284) 的交互分支，打印提示并等待用户输入；只有输入 `Y` 或 `y` 才会继续清空，否则中止退出。

**练习 2**：`-mrg` 参数除了改变文件后缀，还会影响 `Generate` 的哪个入参？

> **答案**：它会把 `extension` 从 `.vhd` 改成 `.mrg`，并把 `overwrite` 置为 `True`（[TbGen.py:307-310](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L307-L310)）。换言之，`.mrg` 模式允许覆盖既有同名文件，而普通 `.vhd` 模式不允许。

---

### 4.2 `TbGenerator.ReadHdl`：从 VHDL 到数据模型

#### 4.2.1 概念说明

一次生成被明确拆成「读」和「写」两步，`ReadHdl` 就是「读」这一步。它接收一个 VHDL 文件路径，把它解析成内存里的数据模型，存到 `TbGenerator` 的两个实例属性上：`self.dutInfo`（描述 DUT 本身）和 `self.tbInfo`（描述要生成的 testbench）。`Generate` 后续完全依赖这两个对象，不再读磁盘上的 VHDL。

#### 4.2.2 核心流程

```text
ReadHdl(filePath)
  ├─> self.dutInfo = DutInfo(filePath)
  │      └─> VhdlFile(filePath) 解析 entity/use 语句/注释
  │      └─> self.name   = entity 名（如 "psi_common_async_fifo"）
  │      └─> libraries   = 按 library 分组的 use 语句
  │      └─> fileScopeTags = 文件级 $$ ... $$ 标签（如 PROCESSES）
  │
  └─> self.tbInfo = TbInfo(self.dutInfo)
         └─> tbName       = name + "_tb"
         └─> tbProcesses  = PROCESSES 标签值；缺失时默认 ["Stimuli"]
         └─> isMultiCaseTb= 是否存在 TESTCASES 标签
```

#### 4.2.3 源码精读

`ReadHdl` 本身极其简短——它只做「装配」，真正的解析被委托给 `DutInfo` 和 `TbInfo`：

```python
def ReadHdl(self, filePath : str):
    self.dutInfo = DutInfo(filePath)
    self.tbInfo = TbInfo(self.dutInfo)
```

> 见 [TbGen.py:29-31](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L29-L31)。

`DutInfo.__init__` 里我们只关心三个产物（[DutInfo.py:36-51](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L36-L51)）：

- `self.name = self.parseInfo.entity.name` → 实体名（[:38](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L38)）。
- `self.libraries` → 按 library 名分组的 use 语句字典。
- `self.fileScopeTags` → 文件级标签字典（来自注释行，[:48-51](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L48-L51)）。

另外，`generics`、`ports`、`dutLibrary` 都是 `DutInfo` 上的属性。其中 `dutLibrary` 在没有 `DUTLIB` 标签时默认为 `"work"`：

```python
@property
def dutLibrary(self):
    if Tags.DUTLIB in self.fileScopeTags:
        return self.fileScopeTags[Tags.DUTLIB]
    else:
        return "work"
```

> 见 [DutInfo.py:61-66](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L61-L66)。本例没有 `DUTLIB` 标签，所以 `dutLibrary = "work"`——这会直接出现在后面 DUT 实例化的 `entity work.psi_common_async_fifo` 里。

再看 `TbInfo.__init__`，本讲用到三个字段（[TbInfo.py:14-30](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L14-L30)）：

```python
self.isMultiCaseTb = Tags.TESTCASES in info.fileScopeTags        # 本例：无 TESTCASES → False
...
self.tbName = info.name + "_tb"                                  # → "psi_common_async_fifo_tb"
self.tbProcesses = ["Stimuli"]                                   # 默认值
if Tags.PROCESSES in info.fileScopeTags:                         # 本例：有 PROCESSES=Input,Output
    self.tbProcesses = info.fileScopeTags[Tags.PROCESSES]
    if type(self.tbProcesses) is str:
        self.tbProcesses = [self.tbProcesses]
```

把这几点套到 `simpleTb` 示例上，`ReadHdl` 跑完后 `self.tbInfo` 的状态是：

| 字段 | 取值 | 出处 |
| --- | --- | --- |
| `tbName` | `"psi_common_async_fifo_tb"` | 实体名 + `_tb` |
| `tbProcesses` | `["Input", "Output"]` | 来自 `$$ PROCESSES=Input,Output $$` |
| `isMultiCaseTb` | `False` | 没有 `TESTCASES` 标签 |

> ⚠️ **一个容易踩的坑**：很多文档会把默认测试进程称作「Stimuli」。但本示例**带了** `PROCESSES=Input,Output`，所以生成的进程是 `p_Input` / `p_Output`，**没有** `p_Stimuli`。只有当 DUT 文件完全没有 `PROCESSES` 标签时，才会用默认值 `["Stimuli"]` 而生成 `p_Stimuli`。这一点会在 4.3 的输出里得到印证。

#### 4.2.4 代码实践

**实践目标**：不运行程序，仅凭源码与示例 VHDL，预测 `ReadHdl` 跑完后的关键状态。

**操作步骤**（源码阅读型实践）：

1. 打开 [example/simpleTb/psi_common_async_fifo.vhd](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd)。
2. 找到 `entity ... is`（[:28](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L28)），读出实体名。
3. 找到文件级标签 `$$ PROCESSES=Input,Output $$`（[:23](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L23)）。
4. 确认整个文件**没有** `$$ TESTCASES=... $$` 标签。
5. 对照上面的「字段取值表」，写下你预测的 `tbName`、`tbProcesses`、`isMultiCaseTb`。

**需要观察的现象 / 预期结果**：你的预测应与上表完全一致。如果之后真的跑了一次生成，可以用下面的 Python 片段（**示例代码**，非项目原有代码）来验证——它会绕开 CLI，直接复用引擎的「读」步骤：

```python
# 示例代码：直接调用 ReadHdl，打印数据模型关键字段
from TbGen import TbGenerator
tg = TbGenerator()
tg.ReadHdl("example/simpleTb/psi_common_async_fifo.vhd")
print(tg.tbInfo.tbName)            # 期望: psi_common_async_fifo_tb
print(tg.tbInfo.tbProcesses)       # 期望: ['Input', 'Output']
print(tg.tbInfo.isMultiCaseTb)     # 期望: False
print(tg.dutInfo.dutLibrary)       # 期望: work
```

> 该片段的实际运行结果**待本地验证**（依赖 `PsiPyUtils` / `pyparsing` 已安装）。

#### 4.2.5 小练习与答案

**练习 1**：假如把示例 VHDL 里的 `$$ PROCESSES=Input,Output $$` 这一行注释删掉，`tbProcesses` 会变成什么？生成的进程名会怎么变？

> **答案**：`TbInfo` 会走默认分支，`tbProcesses = ["Stimuli"]`（[TbInfo.py:26](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L26)），于是生成的进程名变成 `p_Stimuli`（单个）。

**练习 2**：`tbName` 是怎么决定的？它和输出文件名有什么关系？

> **答案**：`tbName = info.name + "_tb"`（[TbInfo.py:24](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L24)）。`Generate` 会把输出文件命名为 `{dst}/{tbName}{extension}`，所以本例输出文件就是 `tb/psi_common_async_fifo_tb.vhd`（见 4.3）。

---

### 4.3 `TbGenerator.Generate`：按固定顺序写出 testbench

#### 4.3.1 概念说明

`Generate` 是「写」这一步。它打开一个输出文件，然后**按一个写死的顺序**，把 testbench 的各个段落依次吐进去。这个顺序就是一份单文件 testbench 的标准骨架：版权头 → 库声明 → 实体声明 → 架构（常量与信号）→ DUT 实例化 → TB 控制 → 时钟 → 复位 → 测试进程。理解了这个顺序，你就能在任意一份生成的 TB 里「按图索骥」。

#### 4.3.2 核心流程

`Generate` 的写作顺序（单文件模式，对应 [TbGen.py:228-253](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L228-L253)）：

```text
打开文件 {dst}/{tbName}{extension}（FileWriter，受 overwrite 控制）
  _Header            版权头 + "Testbench generated by TbGen.py"
  LibraryDeclarations library / use 语句（来自 DutInfo）
  UserPkgDelcaration  用户额外包（本例无）
  [多用例时额外声明 TB 包与各 case 包 —— 本例不是多用例，跳过]
  _EntityDeclaration  entity <tbName> is ...（导出的 generic 才进实体）
  architecture sim of <tbName> is
    _GenericConstants   固定常量 / 默认值 / 导出 generic 记录
    _TbControlSignals   TbRunning、NextCase、ProcessDone 等控制信号
    _DutSignals         与 DUT 端口一一对应的 signal 声明（带初值）
  begin
    _DutInstantiation   i_dut : entity <lib>.<name> [generic map] [port map]
    _TbControl          p_tb_control：等所有 ProcessDone 后令 TbRunning=false 结束仿真
    _Clocks             每个时钟一个 p_clock_* 进程
    _Resets             每个复位一个 p_rst_* 进程
    _Processes          每个测试过程一个 p_<name> 进程（本例：p_Input、p_Output）
  end;
```

段落标题之所以能整整齐齐，是因为它们都走同一个工具函数 `VhdlTitle`（[UtilFunc.py:10-19](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/UtilFunc.py#L10-L19)），一级标题会画一条由 60 个 `-` 组成的分隔线。

#### 4.3.3 源码精读

先看 `Generate` 如何打开文件、如何把各段串起来：

```python
def Generate(self, tbPath : str, extension : str, overwrite : bool = False):
    if self.dutInfo is None:
        raise Exception("No VHDL File parsed yet, call ReadHdl() first!")
    if not os.path.exists(tbPath):
        os.mkdir(tbPath)
    with FileWriter(tbPath + "/" + self.tbInfo.tbName + extension, overwrite=overwrite) as f:
        self._Header(f).WriteLn()
        self.dutInfo.LibraryDeclarations(f)
        self.tbInfo.UserPkgDelcaration(f)
        ...
        self._EntityDeclaration(f)
        ...
        self._DutInstantiation(f).WriteLn()
        self._TbControl(f).WriteLn()
        self._Clocks(f).WriteLn()
        self._Resets(f).WriteLn()
        self._Processes(f).WriteLn()
        f.DecIndent().WriteLn("end;")
```

> 见 [TbGen.py:221-253](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L221-L253)。注意开头的断言：**必须先 `ReadHdl` 再 `Generate`**，否则抛异常（[:222-223](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L222-L223)）。输出文件名 `tbPath + "/" + tbName + extension`，本例即 `tb/psi_common_async_fifo_tb.vhd`（[:228](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L228)）。

接着看本讲实践要求定位的三个段落。

**(a) DUT 实例化 `_DutInstantiation`** —— 产出 `i_dut`：

```python
f.WriteLn("i_dut : entity {}.{}".format(self.dutInfo.dutLibrary, self.dutInfo.name)).IncIndent()
...
f.WriteLn("port map (").IncIndent()
for p in self.dutInfo.ports:
    f.WriteLn("{} => {},".format(p.name, p.name))
```

> 见 [TbGen.py:33-49](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L33-L49)（`i_dut` 在 [:35](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L35)）。本例 `dutLibrary="work"`、`name="psi_common_async_fifo"`，所以生成 `i_dut : entity work.psi_common_async_fifo`，随后是 `generic map`（仅含 `EXPORT=true` 或带 `CONSTANT` 的 generic）和把**所有端口**同名连接的 `port map`。

**(b) 时钟进程 `_Clocks`** —— 产出 `p_clock_*`：

```python
for clk in DutInfo.FilterForTag(self.dutInfo.ports, Tags.TYPE, "clk"):
    if not DutInfo.HasTag(clk, Tags.FREQ):
        raise Exception("Clock {} has not FREQ tag!".format(clk.name))
    f.WriteLn("p_clock_{} : process".format(clk.name)).IncIndent()
    f.WriteLn("constant Frequency_c : real := real({});".format(DutInfo.GetTag(clk, Tags.FREQ))).DecIndent()
    f.WriteLn("begin").IncIndent()
    f.WriteLn("while TbRunning loop").IncIndent()
    f.WriteLn("wait for 0.5*(1 sec)/Frequency_c;")
    f.WriteLn("{name} <= not {name};".format(name=clk.name))
    ...
```

> 见 [TbGen.py:51-66](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L51-L66)。它先用 `FilterForTag` 筛出所有 `TYPE=clk` 的端口，对每个端口生成一个进程。本例有两个时钟端口 `InClk`、`OutClk`，于是产出 `p_clock_InClk`、`p_clock_OutClk` 两个进程。半周期来自 `wait for 0.5*(1 sec)/Frequency_c;`，即

\[ T_{\text{half}} = \frac{0.5}{f}\ \text{秒} \]

把 `InClk` 的 `FREQ=100e6` 代入：\( T_{\text{half}} = 0.5/10^{8}\,\text{s} = 5\,\text{ns} \)；`OutClk` 的 `125e6` 则给出 \( 4\,\text{ns} \)。

**(c) 测试进程 `_Processes`** —— 产出 `p_Input` / `p_Output`（单文件分支）：

```python
for p in self.tbInfo.tbProcesses:          # ["Input", "Output"]
    ...
    f.WriteLn("p_{} : process".format(p))  # → p_Input、p_Output
    f.WriteLn("begin").IncIndent()
    ...                                     # 单文件分支：等复位无效后留 "-- User Code"
    f.WriteLn("assert False report \"Insert your code here!\" severity note;")
    f.WriteLn("ProcessDone(TbProcNr_{}_c) <= '1';".format(p))
    f.WriteLn("wait;")
    f.DecIndent().WriteLn("end process;")
```

> 见 [TbGen.py:86-120](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L86-L120)。进程名直接来自 4.2 得到的 `tbProcesses`，所以本例是 `p_Input`、`p_Output`，而**不是** `p_Stimuli`。每个进程在 `-- User Code` 处留了一句 `assert ... report "Insert your code here!"`，这正是「骨架」二字的具体含义——激励代码要你自己往里填。

把以上三段拼起来，本例生成的 `tb/psi_common_async_fifo_tb.vhd` 大致长这样（**基于源码推断的预期输出，待本地运行验证**；省略号代表未展示的段落）：

```vhdl
------------------------------------------------------------
-- Copyright (c) <今年> by Paul Scherrer Institute, Switzerland
-- ...
------------------------------------------------------------

-- Testbench generated by TbGen.py
-- see Library/Python/TbGenerator
-- ... Libraries / Entity / Architecture 头 ...

-- *** DUT Signals ***    （_DutSignals 产出，与端口一一对应）
signal InClk : std_logic := '0';
signal InRst : std_logic := '0';
...

begin

------------------------------------------------------------
-- DUT Instantiation              ← _DutInstantiation
------------------------------------------------------------
i_dut : entity work.psi_common_async_fifo
generic map (
    Width_g => Width_g,
    Depth_g => Depth_g,
    AlmFullLevel_g => AlmFullLevel_g
)
port map (
    InClk => InClk,
    ...
);

------------------------------------------------------------
-- Clocks !DO NOT EDIT!           ← _Clocks
------------------------------------------------------------
p_clock_InClk : process
    constant Frequency_c : real := real(100e6);
begin
    while TbRunning loop
        wait for 0.5*(1 sec)/Frequency_c;
        InClk <= not InClk;
    end loop;
    wait;
end process;

p_clock_OutClk : process
    constant Frequency_c : real := real(125e6);
begin
    ...
end process;

------------------------------------------------------------
-- Resets                         ← _Resets
------------------------------------------------------------
p_rst_InRst : process ...        -- 等到 InClk 出现两个上升沿后释放
p_rst_OutRst : process ...

------------------------------------------------------------
-- Processes                      ← _Processes（单文件分支）
------------------------------------------------------------
-- *** Input ***
p_Input : process
begin
    -- start of process !DO NOT EDIT
    wait until InRst = '0' and OutRst = '0';
    -- User Code
    assert False report "Insert your code here!" severity note;
    -- end of process !DO NOT EDIT!
    ProcessDone(TbProcNr_Input_c) <= '1';
    wait;
end process;

-- *** Output ***
p_Output : process ... ProcessDone(TbProcNr_Output_c) <= '1'; ...

------------------------------------------------------------
-- Testbench Control !DO NOT EDIT!  ← _TbControl
------------------------------------------------------------
p_tb_control : process
begin
    wait until InRst = '0' and OutRst = '0';
    wait until ProcessDone = AllProcessesDone_c;
    TbRunning <= false;            -- 所有进程完成后结束仿真
    wait;
end process;

end;
```

把这张「预期输出」和上面 `_Xxx()` 方法清单对照，你就能在真实文件里迅速找到本讲要求的三个定位点：`i_dut`（来自 `_DutInstantiation`）、`p_clock_*`（来自 `_Clocks`）、测试进程 `p_Input` / `p_Output`（来自 `_Processes`）。

#### 4.3.4 代码实践

**实践目标**：跑通一次生成（或在源码层面走通），并在产物里按 `Generate` 的调用顺序定位每个段落。

**操作步骤**：

1. 按 4.1 的命令运行一次，得到 `example/simpleTb/tb/psi_common_async_fifo_tb.vhd`。
2. 用编辑器打开该文件，自上而下找到下列段落，并在每段旁边标注它由哪个方法生成：

   | 段落标题 | 生成方法 |
   | --- | --- |
   | `-- Testbench generated by TbGen.py` | `_Header` |
   | `Libraries` | `DutInfo.LibraryDeclarations` |
   | `Entity Declaration` 下的 `entity ... is` | `_EntityDeclaration` |
   | `DUT Instantiation` 下的 `i_dut : entity ...` | `_DutInstantiation` |
   | `Testbench Control !DO NOT EDIT!` 下的 `p_tb_control` | `_TbControl` |
   | `Clocks !DO NOT EDIT!` 下的 `p_clock_InClk`、`p_clock_OutClk` | `_Clocks` |
   | `Resets` 下的 `p_rst_InRst`、`p_rst_OutRst` | `_Resets` |
   | `Processes` 下的 `p_Input`、`p_Output` | `_Processes` |

3. **重点定位**本讲要求的三处：搜索 `i_dut`（DUT 实例化）、搜索 `p_clock_`（应能找到 `p_clock_InClk` 与 `p_clock_OutClk` 两个）、搜索 `p_Input` / `p_Output`（测试进程）。**确认这里没有 `p_Stimuli`**——并用 4.2 的结论解释为什么。

**需要观察的现象 / 预期结果**：

- 文件中确有两个时钟进程，`Frequency_c` 分别是 `real(100e6)` 与 `real(125e6)`。
- 测试进程只有 `p_Input`、`p_Output` 两个，每个都含 `assert False report "Insert your code here!" severity note;` 这句占位。
- `p_tb_control` 在 `wait until ProcessDone = AllProcessesDone_c;` 之后执行 `TbRunning <= false;`——这就是仿真收尾的机制：所有测试进程把自己的 `ProcessDone` 比特置 1，当它们全部为 1（等于 `AllProcessesDone_c`）时，控制进程关闭 `TbRunning`，时钟循环随之退出，仿真结束。

> 若本机暂未装好依赖，可改为「源码阅读型实践」：直接对照 [TbGen.py:228-253](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L228-L253) 的调用顺序，手绘上面这张「段落 → 方法」对照表，实际运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么本例的测试进程是 `p_Input` / `p_Output`，而不是 `p_Stimuli`？请用源码说明。

> **答案**：`_Processes` 遍历的是 `self.tbInfo.tbProcesses`（[TbGen.py:92](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L92)），而该值在 `TbInfo` 里被示例的 `$$ PROCESSES=Input,Output $$` 覆盖成了 `["Input", "Output"]`（[TbInfo.py:27-30](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L27-L30)）。`"Stimuli"` 只是缺失该标签时的默认值。

**练习 2**：如果某个 `TYPE=CLK` 的端口**没有**写 `FREQ` 标签，生成时会怎样？

> **答案**：`_Clocks` 会在生成该进程前抛出 `Exception("Clock <名字> has not FREQ tag!")`（[TbGen.py:54-55](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L54-L55)），随后被 CLI 的 `try/except` 捕获并打印 `ERROR: ...`（[TbGen.py:312-314](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L312-L314)），生成失败。这说明 `FREQ` 是时钟端口的必填标签。

**练习 3**：仿真在什么时候、由谁结束？

> **答案**：由 `p_tb_control`（`_TbControl`）结束。它等到 `ProcessDone = AllProcessesDone_c`（即所有测试进程都把自己那位置 1）后，执行 `TbRunning <= false;`（[TbGen.py:136-138](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L136-L138)）；时钟进程里的 `while TbRunning loop` 随即退出并 `wait;`，仿真停止。

## 5. 综合实践

把本讲三块内容串起来，做一个「**改动标签 → 重新生成 → 解释差异**」的小任务：

1. **复制示例**：把 `example/simpleTb/` 复制一份到你自己的工作目录（不要改原示例）。
2. **做一处改动**：在副本的 VHDL 里，把文件级标签 `$$ PROCESSES=Input,Output $$` 改成 `$$ PROCESSES=Stimuli $$`（即只保留一个名为 `Stimuli` 的过程）。
3. **重新生成**：用 4.1 的等价命令运行，输出到新目录（例如 `-dst ./tb2 -clear -force`）。
4. **diff 对比**：把新生成的 `psi_common_async_fifo_tb.vhd` 与原 `tb/` 里的版本做文本对比。
5. **解释差异**：用本讲学到的链路说明——为什么新版本里只剩一个 `p_Stimuli` 进程？`ProcessDone` 信号（`std_logic_vector`）的位宽为什么也跟着变了？（提示：见 `_TbControlSignals` 里对 `len(tbProcesses)-1` 的使用，[TbGen.py:170-171](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L170-L171)）。

这个任务同时用到了「CLI 运行（4.1）」「`ReadHdl` 如何把标签变成 `tbProcesses`（4.2）」「`Generate` 如何把 `tbProcesses` 变成进程与控制信号（4.3）」三部分知识，能帮你确认自己真的把整条链路走通了。

> 实际 diff 结果**待本地验证**；若暂无条件运行，可改为在源码层面预测差异：进程数从 2 变 1、`ProcessDone` 由 `0 to 1` 变为 `0 to 0`、`AllProcessesDone_c` 同步变窄，其余段落不变。

## 6. 本讲小结

- 一条完整的 CLI 调用形如 `python TbGen.py -src <DUT> -dst <目录> [-clear] [-force] [-mrg]`，参数在 [TbGen.py:264-269](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L264-L269) 声明；`run.bat` 只是对它的一行封装。
- 一次生成严格分成两步：`ReadHdl`（读 VHDL → `dutInfo`/`tbInfo`）与 `Generate`（按固定顺序写文件），且必须先读后写（[TbGen.py:29-31](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L29-L31)、[:221-223](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L221-L223)）。
- 输出文件名为 `{dst}/{tbName}{extension}`，本例即 `tb/psi_common_async_fifo_tb.vhd`，`tbName` 由实体名加 `_tb` 得到。
- 生成的 TB 是一份「按段拼接」的骨架：`i_dut` 实例化来自 `_DutInstantiation`，`p_clock_*` 来自 `_Clocks`，测试进程来自 `_Processes`。
- 本例因为带 `$$ PROCESSES=Input,Output $$`，测试进程是 `p_Input` / `p_Output`，**没有** `p_Stimuli`；后者只是无 `PROCESSES` 标签时的默认。
- 仿真收尾靠 `p_tb_control` 在所有进程完成（`ProcessDone = AllProcessesDone_c`）后置 `TbRunning <= false`。

## 7. 下一步学习建议

到这里，你已经能跑通一次生成、并看懂骨架。接下来建议：

- **进入 u2（VHDL 注解标签系统）**：本讲你只是「会用」`$$ TYPE=CLK; FREQ=...; PROC=... $$` 这类标签。u2-l1 会讲清它们的语法与 `_ParseTags` 的 pyparsing 文法，u2-l2 会逐一讲解 `TYPE/FREQ/CLK/PROC/EXPORT/CONSTANT` 等标签如何左右生成结果。学完后，你可以回头解释本讲综合实践里观察到的所有差异。
- **对照阅读**：在进入 u2 之前，可以先把 `simpleTb` 与 `multiCaseTb` 两个示例的 VHDL 文件 diff 一下，留意 `multiCaseTb` 多出的 `TESTCASES` 标签——它正是 u5「多用例 TB」的入口，也对应本讲里被跳过的 `isMultiCaseTb=True` 分支。
- **源码入口**：想提前感受解析层，可以浏览 [`DutInfo.py`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py) 与 [`TbInfo.py`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py)，它们是 u4「数据模型」的主角。
