# 仓库结构与入口文件

## 1. 本讲目标

通过本讲，你应当能够：

- 看懂 TbGenerator 仓库的整体文件布局，知道每个 Python 模块大概负责什么。
- 区分 **CLI 入口** `TbGen.py` 与 **GUI 入口** `TbGenGui.pyw` 这两条不同的启动路径，理解它们各自如何被调用。
- 在源码中精确定位核心引擎 `TbGenerator` 类，并知道它对外暴露的两个关键方法 `ReadHdl()` 和 `Generate()`。
- 亲手运行一次 `python TbGen.py -h`，看到真实的 CLI 参数帮助，或者至少理解它需要哪些前置依赖。

本讲只做「地图绘制」——告诉你有哪些房间、哪个门是正门。具体的房间内部（标签系统、VHDL 解析、生成流程）会在后续讲义中逐个展开。

## 2. 前置知识

在学习本讲前，你应该已经读过 [u1-l1 项目定位、依赖与许可](u1-l1-project-overview.md)，并掌握下面这些概念：

- **DUT（Design Under Test，被测设计）**：一段用 VHDL 写成的硬件功能模块，是我们要测试的对象。
- **Testbench（测试台）**：用来给 DUT 施加激励、观察输出的 VHDL 代码。TbGenerator 的工作就是从 DUT 自动生成测试台的「骨架」。
- **VHDL**：一种硬件描述语言，DUT 与生成的 testbench 都是 VHDL 文本文件（`.vhd`）。
- **依赖**：本项目依赖三个外部 Python 库——`PsiPyUtils`（PSI 内部工具库，提供文件写入等能力）、`PyQt5`（GUI 框架）、`pyparsing`（文本/文法解析库）。

此外，理解本讲需要一点点 Python 基础常识：

- `if __name__ == '__main__':` 是 Python 脚本的「直接运行入口」——只有当文件被 `python xxx.py` 直接执行时，该代码块才会运行；被 `import` 时不会运行。
- `argparse` 是 Python 标准库里用来解析命令行参数的模块。
- 「类（class）」是一种把数据和操作打包在一起的结构，本讲的 `TbGenerator` 就是一个类。

> 名词小贴士：本仓库里同时存在 `TbGen.py`（文件名）和 `TbGenerator`（类名），它们不是一回事。`TbGen.py` 是入口文件，`TbGenerator` 是文件里定义的核心类。下文会反复出现这两个名字，请注意区分。

## 3. 本讲源码地图

本讲主要涉及下面两个入口文件，其余模块只做一句话介绍：

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `TbGen.py` | CLI 入口 + 核心引擎 | `TbGenerator` 类、`__main__` 里的 argparse |
| `TbGenGui.pyw` | GUI 入口 | `TbGenGui(QDialog)` 类、信号槽、复用 `TbGenerator` |

仓库里还有几个本讲只会「点名」、后续讲义再深入的模块：

- `DutInfo.py`：封装 DUT 的信息（端口、generic、库声明、标签）。
- `TbInfo.py`：根据 DUT 信息建模要生成的 testbench（名称、过程、用例）。
- `VhdlParse.py`：基于 `pyparsing` 的 VHDL 文法解析器。
- `MultiFileTb.py`：多用例 testbench 的 package 文件生成。
- `UtilFunc.py`：输出格式化辅助函数（标题、版权声明）。
- `PsiPyUtils`（外部库）：提供 `FileWriter` 等文件写入基础设施。

## 4. 核心概念与源码讲解

### 4.1 仓库整体布局：扁平结构

#### 4.1.1 概念说明

打开仓库根目录你会发现，几乎所有 Python 源文件都「平铺」在根目录下，没有 `src/`、`lib/` 这样的子目录。这种布局叫**扁平布局（flat layout）**。

扁平布局的好处是：模块之间互相 `import` 时路径很短（直接 `from DutInfo import DutInfo` 即可），适合本项目这种「规模不大、模块不多」的工具型项目。

仓库根目录的结构可以这样分类理解：

```
TbGenerator/
├── TbGen.py            ← CLI 入口 + TbGenerator 核心
├── TbGenGui.pyw        ← GUI 入口（注意扩展名是 .pyw）
├── DutInfo.py          ← DUT 数据模型
├── TbInfo.py           ← Testbench 数据模型
├── VhdlParse.py        ← VHDL 解析
├── MultiFileTb.py      ← 多文件 TB 生成
├── UtilFunc.py         ← 格式化辅助
├── README.md / Changelog.md / License.txt / LGPL2_1.txt   ← 文档与许可
├── doc/                ← PDF/Word 文档
└── example/            ← 可运行示例（simpleTb、multiCaseTb）
```

#### 4.1.2 两条调用路径

用户使用这个工具有两种方式，对应两个入口文件：

1. **命令行方式（CLI）**：在终端执行 `python TbGen.py -src xxx.vhd -dst ./tb ...`，适合脚本化、批处理、CI 场景。
2. **图形界面方式（GUI）**：双击或执行 `python TbGenGui.pyw`，弹出一个窗口手动选文件，适合偶尔手动生成一次的场景。

关键点：**无论走哪条路，最终都调用同一个核心类 `TbGenerator`**。也就是说，CLI 和 GUI 只是两件不同的「外壳」，里面是同一台引擎。这一点是理解整个仓库的钥匙。

```
用户 ─┬─ python TbGen.py（CLI）────┐
      │                            ├─→ TbGenerator.ReadHdl() → Generate()
      └─ python TbGenGui.pyw（GUI）─┘
```

#### 4.1.3 源码精读：import 与路径自举

`TbGen.py` 顶部有一段看起来「奇怪」的代码，值得专门说明：

```python
import os
import sys
if __name__ == "__main__":
    myPath = os.path.realpath(os.path.dirname(__file__))
    sys.path.append(myPath + "/..")
```

这段代码在「直接运行本文件」时，把**上一级目录**加入 Python 的模块搜索路径 `sys.path`。这是为了满足 README 中要求的「目录结构」——按官方约定，`TbGenerator` 应放在 `Python/TbGenerator/` 这样的子目录里，而它依赖的 `PsiPyUtils` 放在 `Python/PsiPyUtils/`。把上一级目录（`Python/`）加入搜索路径后，`from PsiPyUtils import FileWriter` 才能找到兄弟目录里的 `PsiPyUtils`。

参见永久链接：

- [TbGen.py:7-11](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L7-L11)：`__main__` 守卫内的路径自举逻辑（把上一级目录加入 `sys.path`）。

`TbGenGui.pyw` 顶部有完全相同的写法，原因一致：

- [TbGenGui.pyw:12-14](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L12-L14)：GUI 入口同样做路径自举。

> 小提示：如果你把所有仓库都 clone 到同一个父目录下（如 README 推荐的 `psi_fpga_all`），这段自举逻辑就能正常工作。否则你可能需要手动设置 `PYTHONPATH`。

#### 4.1.4 代码实践

**实践目标**：建立对仓库文件的全局印象，并能用一句话说出每个模块的职责。

**操作步骤**：

1. 在仓库根目录列出所有 `.py` / `.pyw` 文件。
2. 对每个文件，打开它只看**最顶部 5~15 行**（版权注释 + import + 类/函数定义的第一行），据此推断它的职责。
3. 把结果填进一张「文件 → 职责」表格。

**需要观察的现象**：你应该发现每个文件顶部都有一个 `from Xxx import Yyy` 的 import 链，这些 import 暴露了模块之间的依赖关系。例如 `TbGen.py` 同时 import 了 `DutInfo`、`TbInfo`、`MultiFileTb`、`UtilFunc`，说明它是「总装配」角色。

**预期结果**（参考答案，本仓库的职责表）：

| 文件 | 一句话职责 |
| --- | --- |
| `TbGen.py` | CLI 入口，定义核心引擎 `TbGenerator` 类，把读取与生成串起来 |
| `TbGenGui.pyw` | GUI 入口，用 PyQt5 弹窗收集输入后复用 `TbGenerator` |
| `DutInfo.py` | 封装 DUT：解析 VhdlFile、归类库、提供标签工具方法 |
| `TbInfo.py` | 根据 DUT 信息建模要生成的 testbench（名称、过程、用例） |
| `VhdlParse.py` | 基于 `pyparsing` 解析 VHDL 的 entity/generic/port 等 |
| `MultiFileTb.py` | 多用例 testbench 的 TB 包与 case 包生成 |
| `UtilFunc.py` | 输出格式化辅助：标题分隔线、版权声明 |

### 4.2 核心引擎：TbGenerator 类

#### 4.2.1 概念说明

`TbGenerator` 是整个工具的核心。它的工作非常聚焦，可以概括为「两步走」：

1. **读取（ReadHdl）**：读入一个 VHDL DUT 文件，把它解析成结构化的数据（`DutInfo`），并据此推导出 testbench 的信息（`TbInfo`）。
2. **生成（Generate）**：把这套数据按固定顺序「翻译」成 VHDL testbench 文本，写到目标目录。

之所以把它设计成一个有状态的类（而不是一个函数），是因为「读」和「生成」是分离的两次操作，中间需要保存解析结果（`self.dutInfo`、`self.tbInfo`）。这种「先读后写、状态挂在 self 上」的设计在后续讲义中会反复出现。

#### 4.2.2 核心流程

`TbGenerator` 的对外接口极其简洁：

```text
实例化 TbGenerator()
   │
   ├─ ReadHdl(filePath)      # 第一步：读取并解析 VHDL
   │      ├─ self.dutInfo = DutInfo(filePath)   # 解析 DUT
   │      └─ self.tbInfo  = TbInfo(self.dutInfo) # 推导 TB 信息
   │
   └─ Generate(tbPath, extension, overwrite=False)   # 第二步：写出 TB
          └─ 按 Header→库声明→实体→架构→DUT 实例化→控制→时钟→复位→进程 的顺序写文件
```

需要特别强调：**必须先调用 `ReadHdl()` 再调用 `Generate()`**。`Generate()` 开头有一处显式检查——如果还没读入任何 VHDL 文件就直接生成，会抛出异常。这是「有状态对象」常见的防御式写法。

#### 4.2.3 源码精读

类的定义、构造函数和读取方法非常短，先看这三处：

- [TbGen.py:23-31](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L23-L31)：定义 `class TbGenerator`，`__init__` 把 `dutInfo`/`tbInfo` 初始化为 `None`；`ReadHdl` 接收一个文件路径，构造 `DutInfo` 与 `TbInfo`。

```python
class TbGenerator:
    def __init__(self):
        self.dutInfo = None
        self.tbInfo = None

    def ReadHdl(self, filePath : str):
        self.dutInfo = DutInfo(filePath)
        self.tbInfo = TbInfo(self.dutInfo)
```

再看生成方法的入口（内部各 `_Xxx()` 子方法的细节留到 u4 讲义）：

- [TbGen.py:221-228](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L221-L228)：`Generate()` 的签名与「未读取就生成」的防御检查，以及目标目录不存在则 `os.mkdir` 创建。

```python
def Generate(self, tbPath : str, extension : str, overwrite : bool = False):
    if self.dutInfo is None:
        raise Exception("No VHDL File parsed yet, call ReadHdl() first!")
    if not os.path.exists(tbPath):
        os.mkdir(tbPath)
```

`Generate()` 主体是一个 `with FileWriter(...) as f:` 块，按固定顺序调用一串 `_Xxx()` 方法，依次写出 testbench 的各个段落。本讲你只需要记住「调用顺序」，不需要理解每个方法内部：

- [TbGen.py:228-253](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L228-L253)：用 `FileWriter` 打开目标文件，依次调用 `_Header → LibraryDeclarations → UserPkgDelcaration → (TbPkg/TbCase) → _EntityDeclaration → _GenericConstants → _TbControlSignals → _DutSignals → _DutInstantiation → _TbControl → _Clocks → _Resets → _Processes`。

这段调用的「顺序」就是生成出来的 VHDL 文件从上到下的段落顺序，这一点在 [u4-l2 Generate 主流程](u4-l2-generate-flow-and-single-tb.md) 会详细对照。

> 注意：上面提到的 `DutInfo`、`TbInfo`、`FileWriter` 等都来自别的模块。本讲你只要知道「`TbGenerator` 是总指挥，它调用别人」即可，不必现在去读它们。

#### 4.2.4 代码实践

**实践目标**：用最少的代码亲手驱动一次 `TbGenerator`，验证「先读后写」的两步模型。

**操作步骤**：

1. 确认依赖已安装（`PsiPyUtils`、`pyparsing`；它们必须可被 import，否则连入口都跑不起来）。
2. 在仓库根目录写一个临时脚本（示例代码，非项目原有代码）：

   ```python
   # 示例代码：临时脚本，验证 TbGenerator 的两步调用
   import sys
   sys.path.append(".")  # 让脚本能找到本目录下的模块
   from TbGen import TbGenerator

   tbGen = TbGenerator()
   tbGen.ReadHdl("example/simpleTb/psi_common_async_fifo.vhd")
   tbGen.Generate("./my_tb_out", ".vhd")
   print("done")
   ```

3. 运行它，然后查看 `./my_tb_out/` 目录。

**需要观察的现象**：目录下会出现一个 `*_tb.vhd` 文件，这就是生成的 testbench 骨架。

**预期结果**：成功生成一个 `.vhd` 文件；如果跳过 `ReadHdl()` 直接调 `Generate()`，会看到 `Exception: No VHDL File parsed yet, call ReadHdl() first!`。

**待本地验证**：实际能否跑通取决于 `PsiPyUtils` 是否按 README 要求安装到位。若 import 阶段就报 `ModuleNotFoundError: No module named 'PsiPyUtils'`，请先解决依赖再重试。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `TbGenerator` 设计成「有状态的类」，而不是一个一次性调用的函数 `generate_tb(src, dst)`？

> **参考答案**：因为「读取解析」和「生成写出」是两个独立的阶段，中间需要保存解析得到的数据（`dutInfo`、`tbInfo`）。用类把这些状态挂在 `self` 上，可以让两步分别调用，也方便后续在两步之间插入检查或修改（例如先 `ReadHdl` 看一眼解析结果，再决定是否 `Generate`）。

**练习 2**：如果把 `tbGen.Generate(...)` 那行删掉、只保留 `tbGen.ReadHdl(...)`，会发生什么？这说明 `ReadHdl` 的副作用体现在哪里？

> **参考答案**：不会有任何文件被写出。`ReadHdl` 的副作用是「填充 `self.dutInfo` 和 `self.tbInfo`」，它只解析、不落盘；真正写文件的是 `Generate`。这也解释了为什么 `Generate` 开头要检查 `self.dutInfo is None`。

### 4.3 CLI 入口：argparse 命令行

#### 4.3.1 概念说明

CLI（Command-Line Interface，命令行界面）入口指的是 `python TbGen.py ...` 这种在终端里通过参数驱动程序的方式。本项目用 Python 标准库的 `argparse` 来解析命令行参数。

`argparse` 的基本套路是：

1. 创建一个 `ArgumentParser`。
2. 用 `add_argument` 逐个声明程序接受哪些参数（`-src`、`-dst` 等）。
3. 调用 `parse_args()`，`argparse` 自动把命令行字符串解析成 Python 对象。

这套声明全部写在 `if __name__ == '__main__':` 守卫里——也就是说，**只有直接运行 `TbGen.py` 时才会启动 CLI 逻辑；当 `TbGenGui.pyw` 通过 `from TbGen import TbGenerator` 引入本文件时，这段 CLI 代码不会执行**。这正是 `__main__` 守卫的意义：让同一个文件既能作为「可运行脚本」，又能作为「可被导入的模块」。

#### 4.3.2 核心流程

CLI 入口的执行顺序如下：

```text
python TbGen.py -src a.vhd -dst ./tb -clear -force
   │
   ├─ 1. 顶部 import（含 PsiPyUtils 等依赖）          ← 依赖缺失会在这里崩
   ├─ 2. if __name__ == '__main__': 进入守卫
   ├─ 3. ArgumentParser 声明 -src/-dst/-clear/-mrg/-force
   ├─ 4. parse_args() 得到 args 对象
   ├─ 5. 校验 -src 是文件；按 -clear/-force 清理目标目录
   ├─ 6. TbGenerator().ReadHdl(args.src)
   └─ 7. TbGenerator().Generate(args.dst, extension, overwrite=args.mrg)
```

本讲的 5 个 CLI 参数含义如下（精确含义在 [u6-l2 CLI 参数详解](u6-l2-cli-args-clear-merge.md) 展开）：

| 参数 | 含义 | 是否必填 |
| --- | --- | --- |
| `-src` | VHDL 源文件（DUT）路径 | 必填 |
| `-dst` | testbench 输出目录 | 必填 |
| `-clear` | 生成前清空目标目录 | 可选，默认 False |
| `-mrg` | 生成 `.mrg` 合并文件而非 `.vhd` | 可选，默认 False |
| `-force` | 配合 `-clear` 使用，跳过用户确认直接清空 | 可选，默认 False |

#### 4.3.3 源码精读

参数声明集中在下面这一段：

- [TbGen.py:263-270](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L263-L270)：`__main__` 守卫 + 5 个 `add_argument` + `parse_args()`。

```python
if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument("-src", dest="src", help="VHDL source file", required=True)
    parser.add_argument("-dst", dest="dst", help="TB destination directory", required=True)
    parser.add_argument("-clear", dest="clear", help="Clear destination directory before generating TB",
                        required=False, default=False, action="store_true")
    ...
    args = parser.parse_args()
```

注意 `-clear`、`-mrg`、`-force` 都用了 `action="store_true"`——这意味着它们是「开关型」参数：在命令行里写了这个标志就为 `True`，不写就是默认的 `False`，不需要再跟一个值。

参数声明之后，是参数校验与目录清理逻辑（含 `-clear`/`-force` 的人机交互确认），最后才是真正调用引擎：

- [TbGen.py:273-299](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4e9f4/TbGen.py#L273-L299)：校验 `-src` 是否为文件；`-clear` 时若目标目录存在且未加 `-force`，则用 `input()` 弹出 Y/N 确认，确认后才删除目录内文件。
- [TbGen.py:301-314](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L301-L314)：实例化 `TbGenerator`，调用 `ReadHdl`，根据 `-mrg` 决定扩展名，最后 `Generate`。

最关键的「桥接」就这两行——CLI 外壳把命令行参数喂给核心引擎：

```python
tbGen = TbGenerator()
tbGen.ReadHdl(args.src)
...
tbGen.Generate(args.dst, extension, overwrite=args.mrg)
```

仓库自带的真实示例可以佐证这些参数的用法：

- [example/simpleTb/run.bat:1](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/run.bat#L1)：`py ..\..\TbGen.py -src .\psi_common_async_fifo.vhd -dst .\tb -clear -force`，一次用到了 `-src`/`-dst`/`-clear`/`-force` 四个参数。

#### 4.3.4 代码实践

**实践目标**：亲手运行 CLI 的帮助命令，看到真实的参数列表；并理解为什么 `argparse` 的帮助也可能跑不出来。

**操作步骤**：

1. 在仓库根目录执行：
   ```bash
   python TbGen.py -h
   ```
2. 仔细阅读打印出来的帮助文本，把每个参数的 `help` 文案与上表的中文含义对照。
3. 故意只给一个参数，例如 `python TbGen.py -src a.vhd`，观察 `argparse` 的报错。

**需要观察的现象**：

- 正常情况下，`-h` 会打印出一段 usage 说明和 5 个参数的 help。
- 只给 `-src` 不给 `-dst` 时，`argparse` 会报 `the following arguments are required: -dst` 并以非零码退出。

**预期结果**：你能看到 5 个参数（`-src`/`-dst`/`-clear`/`-mrg`/`-force`）的帮助。

**待本地验证（重要）**：因为 `TbGen.py` 顶部的 import（[TbGen.py:13-21](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L13-L21)）会先于 `argparse` 执行，**如果 `PsiPyUtils`、`pyparsing` 等依赖没装好，连 `-h` 都会以 `ModuleNotFoundError` 失败**，根本走不到参数解析。所以请先确认依赖齐全；若依赖缺失，本实践改为「源码阅读型」：直接对照 [TbGen.py:265-269](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L265-L269) 的 `add_argument` 把参数表抄写一遍即可。

> 为什么 `argparse` 之前先崩？因为 Python 执行文件时是从上到下先跑完所有顶层 `import` 语句，再进入 `if __name__ == '__main__':` 块的。`argparse` 活在那个块里，依赖 import 失败时它还没机会运行。

#### 4.3.5 小练习与答案

**练习 1**：`-clear` 和 `-force` 是什么关系？为什么 `-force` 单独使用没有意义？

> **参考答案**：`-clear` 表示「生成前清空目标目录」。当目标目录已存在时，若没有 `-force`，程序会用 `input()` 弹出 Y/N 让用户二次确认；加上 `-force` 则跳过确认直接清空。因此 `-force` 是 `-clear` 的「免确认加速器」，单独写 `-force` 而不写 `-clear` 不会触发任何清理逻辑，没有意义。

**练习 2**：为什么所有 `add_argument` 都包在 `if __name__ == '__main__':` 里，而不是放在模块顶层？

> **参考答案**：`TbGen.py` 既是一个可运行脚本，又是一个被 `TbGenGui.pyw` 用 `from TbGen import TbGenerator` 导入的模块。把 CLI 相关代码放进 `__main__` 守卫，可以保证「导入时不会触发命令行解析」，只有「直接运行时」才走 CLI 流程。

### 4.4 GUI 入口：TbGenGui(QDialog)

#### 4.4.1 概念说明

GUI（Graphical User Interface，图形用户界面）入口是 `TbGenGui.pyw`。它用 **PyQt5** 这个 Python GUI 框架画出一个对话框窗口，让用户用鼠标选文件，而不是敲命令行。

几个 PyQt5 的基础概念（初学者可能陌生，先解释）：

- **QApplication**：每个 PyQt5 程序有且只有一个「应用对象」，它管理 GUI 的事件循环。
- **QDialog**：一个「对话框」窗口基类。本项目的 `TbGenGui` 继承自它，所以主窗口就是一个对话框。
- **控件（Widget）**：窗口里的可见元素，如 `QLabel`（文字标签）、`QLineEdit`（单行输入框）、`QPushButton`（按钮）、`QCheckBox`（复选框）。
- **布局（Layout）**：决定控件在窗口里如何排列，如 `QVBoxLayout`（纵向排列）、`QHBoxLayout`（横向排列）。
- **信号与槽（Signal & Slot）**：PyQt5 的事件机制。「信号」是控件发出的事件（如按钮被点击 `clicked`），「槽」是你绑定的处理函数（如 `self.LoadSrc`）。用 `btn.clicked.connect(self.LoadSrc)` 把两者连起来。

> 为什么扩展名是 `.pyw` 而不是 `.py`？在 Windows 上，`.pyw` 用 `pythonw.exe` 运行，**不会弹出黑色控制台窗口**，适合纯 GUI 程序。这是 GUI 入口区别于 CLI 入口的一个外在标志。

#### 4.4.2 核心流程

GUI 程序的启动套路是固定的：

```text
python TbGenGui.pyw
   │
   ├─ 1. app = QApplication(sys.argv)        # 创建应用对象
   ├─ 2. dlg = TbGenGui()                     # 创建并 show() 主对话框
   │        └─ 在 __init__ 里：搭布局、建控件、连信号槽
   └─ 3. exit(app.exec_())                    # 进入事件循环，等用户操作
```

进入事件循环后，程序就「挂着」等用户点击按钮。当用户点击 **Generate TB** 按钮时，触发 `TbGenGui.Generate` 槽函数，它内部依然实例化一个 `TbGenerator` 并调用 `ReadHdl` + `Generate`——和 CLI 走的是同一台引擎。

#### 4.4.3 源码精读

类的定义与窗口搭建在 `__init__` 里完成，这里能清楚看到「控件 + 布局 + 信号槽」三件套：

- [TbGenGui.pyw:17-52](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L17-L52)：`class TbGenGui(QDialog)` 的 `__init__`，搭建源文件选择、目标目录选择、Generate 按钮、Clear/Merge 复选框，并 `show()` 显示窗口。

关键的三处信号槽连接（这是 GUI 行为的核心）：

```python
self.srcBtn.clicked.connect(self.LoadSrc)   # 选源文件按钮
self.dstBtn.clicked.connect(self.LoadDst)   # 选目标目录按钮
self.genBtn.clicked.connect(self.Generate)  # 生成按钮
```

- [TbGenGui.pyw:29](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L29)：源文件选择按钮 → `LoadSrc`。
- [TbGenGui.pyw:36](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L36)：目标目录选择按钮 → `LoadDst`。
- [TbGenGui.pyw:40](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L40)：生成按钮 → `Generate`。

两个文件/目录选择槽函数用 PyQt5 的标准对话框，并维护一个 `self.lastDirectory` 来「记住」上次打开的位置（还特意 `+ "/.."` 跳到上一级，因为 TB 和源文件通常分开放）：

- [TbGenGui.pyw:55-65](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L55-L65)：`LoadSrc` 用 `QFileDialog.getOpenFileName`（过滤 `*.vhd`）；`LoadDst` 用 `QFileDialog.getExistingDirectory`。两者都更新 `self.lastDirectory`。

最关键的「生成」槽函数，复用了 CLI 同一个引擎，并用 `QErrorMessage` 把异常显示给用户：

- [TbGenGui.pyw:67-95](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L67-L95)：`Generate` 槽函数——校验路径、按 Clear 复选框清理、按 Merge 复选框决定扩展名与 `overwrite`，最后 `TbGenerator().ReadHdl(src)` + `Generate(dst, ext, overwrite=overwrite)`；异常用 `QErrorMessage(parent=self).showMessage(str(e))` 弹窗。

注意 GUI 里「Merge 复选框」与 CLI 里 `-mrg` 的对应关系：勾选 Merge 时 `ext=".mrg"` 且 `overwrite=True`，这与 CLI 中 `overwrite=args.mrg` 的逻辑一致——**两个外壳对同一个引擎参数采用了相同的语义**。

最后是 GUI 的启动样板代码：

- [TbGenGui.pyw:99-102](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L99-L102)：`if __name__ == '__main__':` 内创建 `QApplication`、实例化 `TbGenGui`、`exit(app.exec_())` 进入事件循环。

```python
if __name__ == '__main__':
    app = QApplication(sys.argv)
    dlg = TbGenGui()
    exit(app.exec_())
```

#### 4.4.4 代码实践

**实践目标**：跑通一次 GUI 生成，亲身验证「GUI 与 CLI 共用 `TbGenerator`」。

**操作步骤**：

1. 确认 `PyQt5` 已安装（GUI 独有的依赖）。
2. 运行：
   ```bash
   python TbGenGui.pyw
   ```
3. 在弹出的窗口里：点 **Select Source** 选 `example/simpleTb/psi_common_async_fifo.vhd`；点 **Select Destination** 选一个空目录；点 **Generate TB**。
4. 观察输出目录。

**需要观察的现象**：窗口正常弹出；点 Generate 后，目标目录出现一个 `*_tb.vhd` 文件，和 CLI 跑出来的内容一致。

**预期结果**：生成成功。若源文件路径不存在，会弹出一个 `QErrorMessage` 错误对话框（这正是 [TbGenGui.pyw:95](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L95) 的效果）。

**待本地验证**：GUI 需要图形显示环境。如果你在无显示器的纯命令行服务器（如 SSH 终端）上运行，可能因缺少 display 而无法弹出窗口，这种情况下请改用 4.3 节的 CLI 方式验证。

#### 4.4.5 小练习与答案

**练习 1**：在 `TbGenGui.Generate` 里搜索，它是否真的调用了 `TbGenerator`？这说明了 CLI 与 GUI 的什么关系？

> **参考答案**：是的，[TbGenGui.pyw:85-93](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L85-L93) 实例化了 `TbGenerator()` 并调用 `ReadHdl` 与 `Generate`。这说明 CLI 和 GUI 只是对同一核心引擎的两种「外壳」，真正的业务逻辑只有一份，避免了重复实现。

**练习 2**：GUI 是如何把「Merge 复选框」和「Clear 复选框」映射到引擎参数的？与 CLI 对应关系如何？

> **参考答案**：Clear 复选框勾选时，在生成前遍历目标目录删除文件（对应 CLI 的 `-clear`，但 GUI 没有 `-force` 的二次确认，复选框本身就是确认）；Merge 复选框勾选时，扩展名用 `.mrg` 并令 `overwrite=True`（对应 CLI 的 `-mrg` 与 `overwrite=args.mrg`）。两者语义一致。

## 5. 综合实践

把本讲的三条线索（模块布局、CLI、GUI、核心引擎）串起来，完成下面的「入口追踪」小任务：

1. **画依赖图**：在纸上画出本仓库 7 个 Python 模块（`TbGen.py`、`TbGenGui.pyw`、`DutInfo.py`、`TbInfo.py`、`VhdlParse.py`、`MultiFileTb.py`、`UtilFunc.py`）的 `import` 依赖关系图（谁 import 谁）。提示：从每个文件顶部前 15 行的 `from X import Y` 就能读出来。
2. **两条路径汇合点**：在你的依赖图上，用笔标出「CLI 路径」与「GUI 路径」最终汇合到 `TbGenerator` 的那两个方法（答案应是 `ReadHdl` 与 `Generate`）。
3. **运行对比**：分别用 CLI 和 GUI 对同一个示例文件 `example/simpleTb/psi_common_async_fifo.vhd` 生成 testbench（输出到不同目录），然后用 `diff` 比较两个生成的 `*_tb.vhd`。预期它们应该**完全相同**（因为走的是同一个引擎），如果不同，思考可能是哪些参数（如 `-mrg`/`overwrite`）导致的差异。

这个任务能帮你建立「外壳与引擎分离」的架构直觉，这是读懂后续所有讲义的基础。

## 6. 本讲小结

- 仓库采用**扁平布局**，7 个 Python 模块平铺在根目录，互相 `import` 路径很短。
- 工具有**两个入口**：CLI 入口 `TbGen.py` 与 GUI 入口 `TbGenGui.pyw`，但两者最终都调用同一个核心类 `TbGenerator`。
- `TbGenerator` 对外只有两步：`ReadHdl(filePath)` 读取解析、`Generate(tbPath, extension, overwrite)` 写出 testbench；必须先读后写。
- CLI 用 `argparse` 声明 5 个参数（`-src`/`-dst`/`-clear`/`-mrg`/`-force`），全部包在 `if __name__ == '__main__':` 守卫里，保证文件被 import 时不触发命令行逻辑。
- GUI 用 PyQt5 的 `QDialog` + 信号槽机制，把「Merge/Clear 复选框」映射到与 CLI 一致的引擎参数。
- 入口文件顶部都有一段 `sys.path` 自举逻辑，用于在官方推荐的目录结构下找到兄弟库 `PsiPyUtils`。

## 7. 下一步学习建议

本讲你已经拿到了「整张地图」并知道两扇门（CLI/GUI）通往同一个引擎。接下来建议：

1. 先动手跑通一次真实生成——这正是下一讲 [u1-l3 首次运行：生成第一个 testbench](u1-l3-first-run-generate-tb.md) 的主题，会用 `example/simpleTb` 实际产出 `.vhd` 并逐段解读。
2. 跑通之后，如果你想理解「DUT 文件里的 `$$ ... $$` 注解是怎么被工具识别的」，进入 [u2-l1 标签语法与解析原理](u2-l1-tag-syntax-and-parsing.md)。
3. 如果你想直接深入 `TbGenerator.Generate` 内部那一长串 `_Xxx()` 方法，可以跳到 [u4-l2 Generate 主流程与单文件 TB 骨架](u4-l2-generate-flow-and-single-tb.md)，但建议先完成 u1-l3。
