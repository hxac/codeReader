# PyQt5 GUI 实现

## 1. 本讲目标

通过本讲，你应当能够：

- 看懂 `TbGenGui.pyw` 这不到 100 行的 GUI 源码，理解它如何用 **PyQt5** 从零搭出一个对话框窗口。
- 说清「**控件（Widget）+ 布局（Layout）+ 信号与槽（Signal & Slot）**」这三件套在源码里分别对应哪些行。
- 解释 `LoadSrc` / `LoadDst` 两个槽函数里的「**目录记忆**」（`self.lastDirectory`）与「**上级目录跳转**」（`+ "/.."`）逻辑，并理解作者为什么这么设计。
- 解释 `Generate` 槽函数如何**复用同一个核心引擎 `TbGenerator`**，并用 `QErrorMessage` 把任何异常以弹窗形式反馈给用户。
- 亲手给 GUI **新增一个复选框**（例如「强制覆盖」），并把它正确地接到 `TbGenerator.Generate` 的 `overwrite` 参数上。

本讲是《TbGenerator 学习手册》**高级（advanced）**阶段的第一讲。在前面的 [u1-l2 仓库结构与入口文件](u1-l2-repo-structure-and-entry-points.md) 里，我们对 GUI 做过一次「高空俯瞰」（见该讲 4.4 节），知道了「GUI 与 CLI 共用同一台引擎」。本讲不再重复那张地图，而是**降落到地面**，逐行拆开 GUI 的四个最小模块：`TbGenGui(QDialog)` 类骨架、`LoadSrc`/`LoadDst`、`Generate` 槽、`QErrorMessage`。

## 2. 前置知识

学习本讲前，建议你已经读过下面两讲：

- [u1-l2 仓库结构与入口文件](u1-l2-repo-structure-and-entry-points.md)：本讲默认你已经知道「`TbGenGui.pyw` 是 GUI 入口，它 `from TbGen import TbGenerator` 复用核心引擎」。这些内容本讲不再赘述。
- [u4-l2 Generate 主流程与单文件 TB 骨架](u4-l2-generate-flow-and-single-tb.md)：本讲默认你已经知道 `TbGenerator.Generate(tbPath, extension, overwrite=False)` 这个签名，以及它「先 `ReadHdl` 后 `Generate`、必须先读后写」的两步模型。GUI 只是把这两步包进一个按钮回调里。

此外，你需要一点 **PyQt5** 的入门概念。如果你从没接触过它，下面这些名词先建立直觉即可，本讲会结合源码再讲一遍：

- **QApplication**：每个 PyQt5 程序有且只有一个「应用对象」，它掌管整个 GUI 的**事件循环（event loop）**——可以理解成一个「永远在转、等用户点击」的死循环。
- **QDialog**：一个「对话框」窗口基类。本项目的 `TbGenGui` 继承自它，所以主窗口本身就是一个对话框。
- **控件（Widget）**：窗口里看得见、点得到的元素，如 `QLabel`（文字标签）、`QLineEdit`（单行输入框）、`QPushButton`（按钮）、`QCheckBox`（复选框）。
- **布局（Layout）**：决定控件在窗口里如何排列的「容器」，如 `QVBoxLayout`（纵向自上而下排列）、`QHBoxLayout`（横向左右排列）。
- **信号与槽（Signal & Slot）**：PyQt5 的事件机制。**信号**是控件发出的「事件」（如按钮被点击的 `clicked`），**槽**是你绑定的「处理函数」（如 `self.LoadSrc`）。用 `btn.clicked.connect(self.LoadSrc)` 把两者连起来，于是「点按钮」就触发「函数」。
- **try/except**：Python 的异常捕获语法。GUI 里用它把任何抛出的异常「兜住」，避免程序直接崩溃，再转成弹窗提示。

> 名词小贴士：文件名 `TbGenGui.pyw` 与类名 `TbGenGui` 是两回事——前者是入口文件，后者是文件里定义的对话框类。下文会反复出现，请注意区分。另外，`.pyw` 这个扩展名在 Windows 上由 `pythonw.exe` 解释，**不会弹出黑色控制台窗口**，适合纯 GUI 程序；这一点 [u1-l2](u1-l2-repo-structure-and-entry-points.md) 已提过。

## 3. 本讲源码地图

本讲只深入两个文件，但二者地位差别很大：

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `TbGenGui.pyw` | GUI 入口（**本讲主角**） | 整个文件逐行讲解：类骨架、布局、信号槽、文件对话框、`Generate` 槽、`QErrorMessage` |
| `TbGen.py` | 核心引擎 + CLI 入口 | 只关注两点：`TbGenerator` 类的对外接口（`ReadHdl`/`Generate`）与 CLI 的 `-mrg`/`overwrite` 语义，用来和 GUI 做对照 |

本讲**不**深入 `DutInfo`、`TbInfo`、`VhdlParse` 等模块——它们在前序讲义（[u4-l1](u4-l1-data-model-dutinfo-tbinfo.md)、[u3-l2](u3-l2-parse-entity-generic-port.md)）已讲透，GUI 也根本不直接调用它们。GUI 唯一接触的「业务对象」就是 `TbGenerator` 这一个类。

## 4. 核心概念与源码讲解

### 4.1 TbGenGui(QDialog)：PyQt5 类骨架、布局与信号槽

#### 4.1.1 概念说明

`TbGenGui` 是一个继承自 `QDialog` 的类。在 PyQt5 里，**自定义窗口类继承一个 Qt 窗口基类**是最常见的写法——继承之后，你的类就「天生是一个窗口」，可以直接用 `self` 调用所有窗口方法（如 `setWindowTitle`、`show`）。

一个 PyQt5 窗口类通常要在它的 `__init__` 里完成三件事，本讲称之为「**控件 + 布局 + 信号槽**三件套」：

1. **创建控件**：`new` 出各种 `QLabel`、`QLineEdit`、`QPushButton`、`QCheckBox`。
2. **摆进布局**：把控件按一定顺序 `addWidget` 到 `QVBoxLayout` / `QHBoxLayout` 里，最后 `setLayout`。
3. **连接信号槽**：把按钮的 `clicked` 信号 `connect` 到对应的处理函数。

`TbGenGui.__init__` 就是这三件事的完整示范。在逐行读它之前，先把整个 GUI 的「**控件树 + 布局树**」画出来，让你先有全局画面：

```text
TbGenGui (QDialog)
└── layout : QVBoxLayout (纵向主布局)
    ├── QLabel("Source File")
    ├── srcLine : QLineEdit          ← 源文件路径输入框
    ├── srcBtn  : QPushButton        ← "Select Source"  → clicked → LoadSrc
    ├── QLabel("Destination Directory")
    ├── dstLine : QLineEdit          ← 目标目录输入框
    ├── dstBtn  : QPushButton        ← "Select Destination" → clicked → LoadDst
    ├── genBtn  : QPushButton        ← "Generate TB"   → clicked → Generate
    └── hLayout : QHBoxLayout (横向子布局)
        ├── clrCb : QCheckBox        ← "Clear Destination Dir"
        └── mrgCb : QCheckBox        ← "Create Merge Files"
```

注意一个细节：上面 9 个控件里，**前 7 个纵向排**，**最后 2 个复选框横向排成一行**。这正解释了为什么源码里同时出现了 `QVBoxLayout`（主）与 `QHBoxLayout`（子）两种布局——需要「换行排列」时，就把一个横向布局当作整体 `addLayout` 进纵向布局。这是 PyQt5 里非常典型的「**布局嵌套**」手法。

#### 4.1.2 核心流程

GUI 程序的生命周期分两个阶段，理解这一点能解释源码里很多看似「奇怪」的写法：

```text
【阶段 A：构造】只发生一次，在 __init__ 里完成
   QApplication.__init__ → 创建控件 → 摆布局 → 连信号槽 → show() 显示
            │
            ▼
【阶段 B：事件循环】进入 app.exec_()，程序「挂着」等用户操作
   用户点 srcBtn ─→ 触发 LoadSrc() ─→ 弹文件对话框 ─→ 填充 srcLine
   用户点 dstBtn ─→ 触发 LoadDst() ─→ 弹目录对话框 ─→ 填充 dstLine
   用户点 genBtn ─→ 触发 Generate() ─→ 调 TbGenerator 引擎 ─→ 写文件
            │
            ▼
   关闭窗口 ─→ app.exec_() 返回 ─→ exit() 结束进程
```

关键认知：`__init__` 里写的代码**只在窗口创建那一刻执行一次**；而 `LoadSrc`、`LoadDst`、`Generate` 这些槽函数是**每次点对应按钮才执行**。所以「把控件存到 `self.xxx`」就是为了——构造时建好的控件，能在之后的槽函数里继续读写它。这就是为什么 `srcLine`、`clrCb` 等都挂在 `self` 上。

#### 4.1.3 源码精读

先看文件顶部，确认依赖与「路径自举」逻辑：

- [TbGenGui.pyw:7-8](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L7-L8)：用 `from ... import *` 把 PyQt5 的 `QtGui` 与 `QtWidgets` 全量导入。本文件用到的 `QApplication`、`QDialog`、`QLabel`、`QLineEdit`、`QPushButton`、`QCheckBox`、`QVBoxLayout`、`QHBoxLayout`、`QFileDialog`、`QErrorMessage` **全部来自 `QtWidgets`**；`QtGui` 的星号导入在本文件里其实并未被直接使用，属于冗余但无害的写法。
- [TbGenGui.pyw:12-15](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L12-L15)：与 `TbGen.py` 完全相同的「路径自举」——直接运行时把上一级目录加入 `sys.path`，以便找到兄弟库 `PsiPyUtils`（详见 [u1-l2 4.1.3 节](u1-l2-repo-structure-and-entry-points.md)）；紧接着 `from TbGen import TbGenerator` 把核心引擎引入。

```python
from PyQt5.QtGui import *
from PyQt5.QtWidgets import *
import sys
import os

if __name__ == "__main__":
    myPath = os.path.realpath(os.path.dirname(__file__))
    sys.path.append(myPath + "/..")
from TbGen import TbGenerator
```

> 注意 `from TbGen import TbGenerator` 这一行在 `if __name__ == "__main__":` 块**之外**——这意味着**无论直接运行还是被导入，都会执行这次 import**，因为 GUI 类定义必须依赖 `TbGenerator`。只有「路径自举」那段被放进了 `__main__` 守卫（因为只有直接运行时才需要补路径）。

再看 `__init__` 的逐段拆解。第一段：建主布局、窗口标题、源文件区（标签 + 输入框 + 按钮 + 信号槽）：

- [TbGenGui.pyw:20-30](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L20-L30)：调用父类 `QDialog.__init__`、设置窗口标题、创建 `QVBoxLayout` 主布局，然后依次 `addWidget` 源文件的标签/输入框/按钮，并把 `srcBtn.clicked` 连到 `self.LoadSrc`。

```python
def __init__(self, parent = None):
    QDialog.__init__(self, parent=parent)
    self.setWindowTitle("Testbench Generator")
    layout = QVBoxLayout()

    layout.addWidget(QLabel("Source File", parent=self))
    self.srcLine = QLineEdit(parent=self)
    layout.addWidget(self.srcLine)
    self.srcBtn = QPushButton("Select Source", parent=self)
    self.srcBtn.clicked.connect(self.LoadSrc)
    layout.addWidget(self.srcBtn)
```

这里有三个 PyQt5 习惯值得记住：

1. **`QDialog.__init__(self, parent=parent)`**：显式调用父类构造函数。Python 3 里也可以写成 `super().__init__(parent)`，二者等价；作者用了旧式显式写法。
2. **`parent=self`**：创建控件时把它「挂」到当前窗口下。这在 Qt 里关系到**对象生命周期**——父窗口销毁时会自动回收子控件，避免内存泄漏。本讲后面会看到 `QErrorMessage(parent=self)` 也是同样道理。
3. **`btn.clicked.connect(self.LoadSrc)`**：信号槽连接的「标准句式」。注意传的是**函数对象本身**（`self.LoadSrc`，不带括号），不是函数调用结果。如果误写成 `self.LoadSrc()`，就会在构造阶段立刻执行一次，而不是「等点击」。

第二段：目标目录区，与源文件区结构完全对称（标签 + 输入框 + 按钮 + 信号槽），只是按钮和槽换成 `dstBtn` / `LoadDst`：

- [TbGenGui.pyw:32-37](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L32-L37)：目标目录的标签/输入框/按钮，`dstBtn.clicked.connect(self.LoadDst)`。

第三段：生成按钮 + 两个复选框（这里出现了布局嵌套）：

- [TbGenGui.pyw:39-50](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L39-L50)：`genBtn.clicked.connect(self.Generate)`；再建一个 `QHBoxLayout`，把 `clrCb`（Clear Destination Dir）和 `mrgCb`（Create Merge Files）两个复选框横向排进去，最后 `layout.addLayout(hLayout)` 把整行并回主纵向布局。

```python
    self.genBtn = QPushButton("Generate TB", parent=self)
    self.genBtn.clicked.connect(self.Generate)
    layout.addWidget(self.genBtn)

    hLayout = QHBoxLayout()
    self.clrCb = QCheckBox("Clear Destination Dir")
    hLayout.addWidget(self.clrCb)
    self.mrgCb = QCheckBox("Create Merge Files")
    hLayout.addWidget(self.mrgCb)
    layout.addLayout(hLayout)
```

注意「纵向布局 `addWidget` 放控件、`addLayout` 放子布局」的区别：把控件加进布局用 `addWidget`，把另一个布局整体加进来用 `addLayout`。混用是允许的，正是嵌套布局的关键。

最后收尾：把主布局真正交给窗口、显示窗口、初始化目录记忆变量：

- [TbGenGui.pyw:50-52](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L50-L52)：`self.setLayout(layout)` 让窗口采用主布局；`self.show()` 让窗口可见；`self.lastDirectory = "."` 初始化「目录记忆」为当前目录（这个变量是 4.2 节的主角）。

```python
    self.setLayout(layout)
    self.show()
    self.lastDirectory = "."
```

至此，「控件 + 布局 + 信号槽」三件套全部就位。`__init__` 跑完后，窗口已经画好、显示出来，三个按钮也都「上好了发条」，只等用户点击。

#### 4.1.4 代码实践

**实践目标**：不运行程序，仅凭源码画出 GUI 的「控件树 + 布局树」，并在源码里标注每个控件由哪几行创建。

**操作步骤**：

1. 打开 [TbGenGui.pyw:20-52](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L20-L52)。
2. 准备一张白纸，照本节 4.1.1 的树状图，**自己重画一遍**，标注每个节点对应的源码行号。
3. 特别留意：两个 `QCheckBox` 是怎么被「打包」进 `QHBoxLayout` 再整体进入主布局的——在图上把这一层嵌套画清楚。

**需要观察的现象**：你会发现整张图里 `self.xxx`（挂在实例上的控件）有 `srcLine`、`srcBtn`、`dstLine`、`dstBtn`、`genBtn`、`clrCb`、`mrgCb` 共 7 个；而两个 `QLabel("Source File")`、`QLabel("Destination Directory")` **没有**存到 `self`。

**预期结果**：能解释「为什么标签不存 `self`、而输入框和按钮要存」——标签是纯展示、后续不会被任何槽函数读写；输入框/按钮/复选框则要被槽函数读写（如 `Generate` 要读 `srcLine.text()`、`clrCb.isChecked()`），所以必须挂在 `self` 上才能跨方法访问。

**待本地验证**：无（纯源码阅读型实践）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `self.srcBtn.clicked.connect(self.LoadSrc)` 误写成 `self.srcBtn.clicked.connect(self.LoadSrc())`（多了括号），会发生什么？

> **参考答案**：`__init__` 执行到这一行时，会**立刻调用一次** `LoadSrc()`（因为带了括号），在窗口还没显示时就弹出文件选择对话框，并且把 `LoadSrc()` 的返回值（`None`）当作槽函数连接——之后点按钮**什么都不会发生**。这是 PyQt5 初学者最经典的坑：信号槽连接传的必须是**函数对象本身**。

**练习 2**：两个 `QLabel` 没有存到 `self`，会不会导致它们被 Python 垃圾回收、从而窗口里看不到文字？

> **参考答案**：不会。虽然 Python 侧没有变量持有它们，但 `layout.addWidget(QLabel(...))` 时，Qt 的**父子对象树（parent-child ownership）**已经接管了它们——布局与窗口会持有这些控件的 C++ 对象引用，确保它们在窗口存活期间不被销毁。所以「不存 `self`」对纯展示型控件是安全的。

### 4.2 LoadSrc / LoadDst：文件对话框、目录记忆与上级跳转

#### 4.2.1 概念说明

`LoadSrc` 和 `LoadDst` 是两个「按钮被点之后才执行」的槽函数。它们的职责是：弹出一个**系统标准的文件/目录选择对话框**，让用户用鼠标挑，挑完把路径回填到对应的输入框里。

PyQt5 提供了一组**静态**文件对话框，无需自己画：

- `QFileDialog.getOpenFileName(...)`：选「**文件**」，返回 `(路径, 过滤器)` 元组。
- `QFileDialog.getExistingDirectory(...)`：选「**目录**」，直接返回路径字符串。

这两个函数都有一个关键参数 `directory=...`，用来指定**对话框打开时停在哪个目录**。本讲的两个槽函数正是利用它，实现了一个小巧的用户体验优化：**记住用户上次挑文件的位置，下次直接停在那儿**。源码里把这个记忆存在 `self.lastDirectory` 里（4.1 节已见到它在 `__init__` 末尾被初始化为 `"."`）。

更妙的是，作者在更新 `lastDirectory` 时特意拼了一个 `+ "/.."`，让下一次对话框**跳到上一级目录**。这背后是一个领域经验：在 FPGA 工程里，**DUT 源文件和它生成的 testbench 通常放在不同的兄弟目录里**（比如 `rtl/xxx.vhd` 和 `tb/xxx_tb.vhd` 并列在某工程目录下）。从源文件目录「上一级」再展开，下一次选目标目录时就能一眼看到 `tb/`。

#### 4.2.2 核心流程

两个槽函数的逻辑高度对称，可以用同一张图描述：

```text
用户点击 srcBtn / dstBtn
   │
   ▼
弹出 QFileDialog（停在 self.lastDirectory）
   │
   ├─ 用户「取消」 → 返回 ""  → 直接返回，什么都不改
   │
   └─ 用户选了 file / dir（非 ""）
         │
         ├─ 把路径回填到 srcLine / dstLine（setText）
         └─ 更新 self.lastDirectory = <所选路径所在目录> + "/.."
                                              ↑ 下次对话框会跳到上一级
```

关键判断是 `if file != "":`（[TbGenGui.pyw:57](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L57)）——用户在对话框里点「取消」时，`getOpenFileName` 返回空字符串，此时**不能**把空串写进输入框，也不能更新目录记忆，否则会破坏已有内容。这是 GUI 编程里典型的「**先判空再处理**」防御。

#### 4.2.3 源码精读

先看 `LoadSrc`（选**文件**）：

- [TbGenGui.pyw:55-59](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L55-L59)：用 `getOpenFileName` 弹出文件对话框，`filter="*.vhd"` 只显示 VHDL 文件，`directory=self.lastDirectory` 控制起始位置；取元组第 0 项（路径）；非空则回填并更新 `lastDirectory`。

```python
def LoadSrc(self):
    file = QFileDialog.getOpenFileName(parent=self, caption="Select Source File",
                                       directory=self.lastDirectory, filter="*.vhd")[0]
    if file != "":
        self.srcLine.setText(file)
        self.lastDirectory = self.lastDirectory = os.path.dirname(file) + "/.." #Go one directory up ...
```

逐个参数解释 `getOpenFileName`：

| 参数 | 值 | 作用 |
| --- | --- | --- |
| `parent` | `self` | 对话框的父窗口（决定弹窗位置与生命周期） |
| `caption` | `"Select Source File"` | 对话框标题栏文字 |
| `directory` | `self.lastDirectory` | **对话框打开时停在哪个目录**（这是「目录记忆」的入口） |
| `filter` | `"*.vhd"` | 只列出 `.vhd` 文件（返回值第二项是用户实际选中的过滤器，本代码忽略） |

末尾 `[0]` 是因为该函数返回 `(路径, 过滤器)` 二元组，作者只取路径。

注意两处「源码原貌」细节（本讲忠于源码，原样指出）：

1. **第 59 行有一个重复赋值**：`self.lastDirectory = self.lastDirectory = ...`。这是源码里真实存在的「手滑」写法——Python 允许链式赋值 `a = b = expr`，所以 `self.lastDirectory = self.lastDirectory = expr` 在语法上等价于 `self.lastDirectory = expr`，多出来的左半截没有任何副作用，纯粹是冗余。
2. 行尾注释 `#Go one directory up because TB and SRT are usually stored in different folders`（`SRT` 大概是 `SRC` 的笔误）解释了 `+ "/.."` 的意图。

再看 `LoadDst`（选**目录**），结构完全对称，只是换成 `getExistingDirectory`：

- [TbGenGui.pyw:61-65](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L61-L65)：用 `getExistingDirectory` 弹目录对话框，非空则回填 `dstLine` 并更新 `lastDirectory`。

```python
def LoadDst(self):
    dir = QFileDialog.getExistingDirectory(parent=self, caption="Select Destination Directory",
                                           directory=self.lastDirectory)
    if dir != "":
        self.dstLine.setText(dir)
        self.lastDirectory = dir + "/.." #Go one directory up ...
```

注意两处差异：`LoadDst` **没有** `filter`（选目录不需要文件类型过滤），并且 `lastDirectory` 直接用 `dir + "/.."`（目录本身就是完整路径，无需再 `os.path.dirname`）；而 `LoadSrc` 用 `os.path.dirname(file) + "/.."`（先从文件路径取出它所在的目录，再上一级）。

> **关于 `+ "/.."` 的可靠性提示**：把字符串 `"/.."` 拼到路径末尾，依赖操作系统/Qt 在打开对话框时对 `..` 的解析。在多数桌面环境（Windows、带 GUI 的 Linux）下能正常工作，但属于「字符串拼接路径」而非 `os.path` 的规范用法。作为一个手边小工具，这种写法可以接受；若你要做更健壮的工具，应改用 `os.path.dirname(os.path.normpath(...))` 之类。本讲只解释现状，不改源码。

#### 4.2.4 代码实践

**实践目标**：亲手验证「目录记忆」与「上级跳转」这两条逻辑，理解 `self.lastDirectory` 是如何在多次点按钮之间累积变化的。

**操作步骤**（源码阅读 + 本地运行结合）：

1. **静态追踪**：假设 `self.lastDirectory` 初始为 `"."`，用户依次做了下面三步操作，在纸上写出每一步**之后** `self.lastDirectory` 的值：
   - 步骤 a：点 Select Source，选中 `/home/me/proj/rtl/fifo.vhd`。
   - 步骤 b：再点 Select Source，选中 `/home/me/proj/rtl/uart.vhd`。
   - 步骤 c：点 Select Destination，选中 `/home/me/proj/tb`。
2. **本地验证**（需图形环境 + 已装 PyQt5）：运行 `python TbGenGui.pyw`，故意先点 Select Source 选一个深层目录里的 `.vhd`，**取消**关闭；再点一次 Select Source，观察对话框这次停在哪个目录。

**需要观察的现象**：

- 静态追踪的答案应为：a 后 `"/home/me/proj/rtl/.."`；b 后仍为 `"/home/me/proj/rtl/.."`（因为两次都在同一目录选文件，`dirname` 相同）；c 后 `"/home/me/proj/tb/.."`。
- 本地运行时，「取消」不应清空输入框、也不应改写已有路径（因为空串被 `if file != "":` 挡住）。

**预期结果**：你能用一句话说清「目录记忆」是 `self` 上的一个**跨调用持久**变量，每次选完都会被改写，从而影响下一次对话框的起始位置。

**待本地验证**：步骤 2 需要图形显示环境。在无显示器的纯命令行服务器（如 SSH 终端）上，PyQt5 会因找不到 display 而无法弹窗，此时只做步骤 1 的静态追踪即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `LoadSrc` 用 `os.path.dirname(file) + "/.."`，而 `LoadDst` 只用 `dir + "/.."`？

> **参考答案**：`getOpenFileName` 返回的是**文件**完整路径（如 `.../rtl/fifo.vhd`），要得到「它所在的目录」必须先用 `os.path.dirname` 剥掉文件名；而 `getExistingDirectory` 返回的**本身就是目录**（如 `.../tb`），无需再剥一层。两者都需要再 `+ "/.."` 跳到上一级，于是写法上差了一个 `os.path.dirname`。

**练习 2**：如果删掉 `if file != "":` 这层判断，用户在对话框里点「取消」会发生什么不好的事？

> **参考答案**：取消时 `file` 为空串 `""`，若不拦截，`self.srcLine.setText("")` 会**清空**已经填好的源文件路径，并且 `os.path.dirname("") + "/.."` 会把 `lastDirectory` 改成无意义的 `"/.."`，破坏后续的目录记忆。所以这层判空是对「用户取消」这一常见操作的必要防御。

### 4.3 Generate 槽函数：复用 TbGenerator 引擎

#### 4.3.1 概念说明

`Generate` 是整个 GUI 的「心脏」——用户点 **Generate TB** 按钮时它被触发，真正去生成 testbench。本讲最重要的一个认知是：**它内部完全复用 CLI 那台同一个引擎 `TbGenerator`**，自己几乎不写任何业务逻辑。

回忆 [u1-l2](u1-l2-repo-structure-and-entry-points.md) 与 [u4-l2](u4-l2-generate-flow-and-single-tb.md) 已建立的模型：`TbGenerator` 的对外接口只有两步——

```python
tbGen = TbGenerator()
tbGen.ReadHdl(srcFilePath)                 # 第一步：读 VHDL，填 dutInfo/tbInfo
tbGen.Generate(dstDir, extension, overwrite)   # 第二步：写出 testbench 骨架
```

GUI 的 `Generate` 槽函数，本质就是把这两步**原样搬进按钮回调**，并加上「从 GUI 控件读参数」「把异常转弹窗」两件 GUI 专属的包装。可以把它理解成一个**适配层（adapter）**：把「输入框文本 + 复选框状态」翻译成「引擎方法调用」。

这个设计有一个巨大的好处：**业务逻辑只有一份**。无论用户走 CLI 还是 GUI，最终都经过同一份 `ReadHdl` + `Generate` 代码，因此两条路径产出的 testbench 必然一致（除参数差异外），也避免了「改了 CLI 忘了改 GUI」的双份维护负担。

#### 4.3.2 核心流程

`Generate` 槽函数的执行顺序如下（先忽略 `try/except`，4.4 节再讲它）：

```text
用户点 Generate TB 按钮
   │
   ▼
1. 从控件读参数：src = srcLine.text()，dst = dstLine.text()
   │
   ▼
2. 校验路径：src 必须是文件、dst 必须是目录（否则抛 FileNotFoundError）
   │
   ▼
3. 若 clrCb 勾选 → 遍历 dst，删除其中所有【文件】（不删子目录）
   │
   ▼
4. 实例化引擎：tbGen = TbGenerator()
   │
   ▼
5. 读 VHDL：tbGen.ReadHdl(src)
   │
   ▼
6. 由 mrgCb 决定扩展名与 overwrite：
      勾选 Merge → ext=".mrg", overwrite=True
      未勾选     → ext=".vhd",  overwrite=False
   │
   ▼
7. 写出：tbGen.Generate(dst, ext, overwrite=overwrite)
```

把这条链与 CLI 的 `__main__` 流程并排看，会发现**步骤 4-7 几乎逐行对应**。本讲把两套参数映射关系总结成下表，这是理解「CLI ↔ GUI 同构」的钥匙：

| CLI 参数 | GUI 对应 | 影响的引擎调用 |
| --- | --- | --- |
| `-src <file>` | `srcLine` 输入框内容 | `ReadHdl(src)` 的入参 |
| `-dst <dir>` | `dstLine` 输入框内容 | `Generate(dst, ...)` 的第一个入参 |
| `-clear` | `clrCb` 复选框 | 决定是否在生成前清空 dst |
| `-mrg` | `mrgCb` 复选框 | 决定 `extension`（`.mrg`/`.vhd`）与 `overwrite` |
| `-force` | **（GUI 无对应）** | CLI 里配合 `-clear` 跳过 Y/N 确认 |

最后一行很关键：**GUI 没有 `-force` 的对应物**。在 CLI 里，`-clear` 会先弹 `input()` 让你按 Y/N 确认，`-force` 是「免确认」（见 [TbGen.py:278-294](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L278-L294) 与 [u1-l2 4.3.5 节](u1-l2-repo-structure-and-entry-points.md)）；而在 GUI 里，勾选 Clear 复选框**本身就代表用户已经确认**，所以直接清空、无需二次弹窗。这是「复选框交互」与「命令行交互」在确认语义上的天然差别。

另一个值得专门记的映射：在 GUI 里，`overwrite` 参数**与 Merge 复选框绑定**——勾 Merge 才 `overwrite=True`，否则永远是 `False`。也就是说，**当前 GUI 没有任何办法让 `.vhd` 模式也强制覆盖**已存在的同名文件。这正是本讲综合实践（第 5 节）要你动手补齐的功能缺口。

#### 4.3.3 源码精读

逐段读 `Generate` 槽函数。第一段：从控件读参数并校验：

- [TbGenGui.pyw:67-75](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L67-L75)：整个函数体包在 `try` 里（`except` 见 4.4 节）；`src = self.srcLine.text()`、`dst = self.dstLine.text()`；用 `os.path.isfile` / `os.path.isdir` 校验，不满足则 `raise FileNotFoundError`。

```python
def Generate(self):
    try:
        src = self.srcLine.text()
        dst = self.dstLine.text()
        #Check files
        if not os.path.isfile(src):
            raise FileNotFoundError("File {} does not exist".format(src))
        if not os.path.isdir(dst):
            raise FileNotFoundError("Directory {} does not exist".format(src))
```

> **源码原貌提示（值得你留意）**：第 75 行的目标目录校验信息里，`format(src)` 用的是 `src` 而非 `dst`——也就是说，当目标目录不存在时，弹窗里显示的却是**源文件**路径。这是源码里真实存在的一个小瑕疵（复制粘贴时没改变量）。它不影响功能（异常照样会被捕获并弹窗），只是错误提示文案会误导用户。本讲只指出、不修改；你可以在综合实践里顺手修掉它。

第二段：按 Clear 复选框清理目标目录：

- [TbGenGui.pyw:77-82](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L77-L82)：若 `clrCb.isChecked()`，遍历 `os.listdir(dst)`，**只删文件、不删子目录**（`if os.path.isfile(fp)`），逐个 `os.remove`。

```python
        #Clear if required
        if self.clrCb.isChecked():
            for file in os.listdir(dst):
                fp = dst + "/" + file
                if os.path.isfile(fp):
                    os.remove(fp)
```

注意两点：其一，`clrCb.isChecked()` 返回布尔值，是「复选框状态 → Python 布尔」的标准读法；其二，清理逻辑只删**文件**，若 `dst` 里有子目录则原样保留——这比「无脑删整个目录再重建」更温和，避免误删用户在目录里建的子文件夹。

第三段（本节核心）：实例化引擎、读 VHDL、按 Merge 复选框决定扩展名与 `overwrite`、写出：

- [TbGenGui.pyw:84-93](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L84-L93)：`tbGen = TbGenerator()` → `ReadHdl(src)`；默认 `overwrite=False`；若 `mrgCb.isChecked()` 则 `ext=".mrg"` 且 `overwrite=True`，否则 `ext=".vhd"`；最后 `tbGen.Generate(dst, ext, overwrite=overwrite)`。

```python
        #Generate
        tbGen = TbGenerator()
        tbGen.ReadHdl(src)
        overwrite = False
        if self.mrgCb.isChecked():
            ext = ".mrg"
            overwrite = True
        else:
            ext = ".vhd"
        tbGen.Generate(dst, ext, overwrite=overwrite)
```

把这段与 CLI 的对应段（[TbGen.py:304-310](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L304-L310)）对照，几乎是一一映射：

```python
# CLI（TbGen.py 的 __main__ 内）
tbGen = TbGenerator()
tbGen.ReadHdl(args.src)
extension = ".vhd"
if args.mrg:
    extension = ".mrg"
tbGen.Generate(args.dst, extension, overwrite=args.mrg)
```

两边的 `overwrite` 都直接取自 Merge 信号（GUI 是 `mrgCb.isChecked()`，CLI 是 `args.mrg`），语义完全一致——勾选/传 `-mrg` 既改扩展名、又开覆盖。这就是「两个外壳、一套语义」的具体体现。

> 复习：`TbGenerator.Generate` 内部如何使用 `extension` 与 `overwrite`？它用 `extension` 拼出输出文件名 `{tbPath}/{tbName}{extension}`（[TbGen.py:228](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L228)），并把 `overwrite` 透传给 `FileWriter` 决定是否覆盖已存在文件。这两者的内部细节属于 [u4-l2](u4-l2-generate-flow-and-single-tb.md) 与 [u6-l2](u6-l2-cli-args-clear-merge.md) 的范围，本讲只关心 GUI 如何**喂参数**。

#### 4.3.4 代码实践

**实践目标**：跑通一次 GUI 生成，亲眼确认「GUI 与 CLI 产出一致」，并验证 Clear / Merge 两个复选框对输出的影响。

**操作步骤**：

1. 确认依赖齐全：`PyQt5`（GUI 独有）、`PsiPyUtils`、`pyparsing`。
2. 准备两个空目录作为输出，例如 `./out_gui` 与 `./out_gui_mrg`。
3. 运行 `python TbGenGui.pyw`，在弹窗里：
   - 点 **Select Source** 选 `example/simpleTb/psi_common_async_fifo.vhd`；
   - 点 **Select Destination** 选 `./out_gui`；
   - 勾上 **Clear Destination Dir**（确保目录干净）；
   - 点 **Generate TB**。
4. 观察是否在 `./out_gui` 下出现 `psi_common_async_fifo_tb.vhd`。
5. （可选对照）再用 CLI 跑一次同样输入到 `./out_cli`：
   ```bash
   python TbGen.py -src example/simpleTb/psi_common_async_fifo.vhd -dst ./out_cli -clear -force
   ```
   然后 `diff ./out_gui/psi_common_async_fifo_tb.vhd ./out_cli/psi_common_async_fifo_tb.vhd`。

**需要观察的现象**：

- 步骤 4：目标目录出现一个 `*_tb.vhd`，内容与 CLI 产物一致。
- 步骤 5（对照）：`diff` 应**无输出**（两文件相同），证明 GUI 与 CLI 走的是同一台引擎。
- 勾上 **Create Merge Files** 再生成一次到 `./out_gui_mrg`，应看到扩展名变成 `.mrg` 而非 `.vhd`。

**预期结果**：成功生成；勾 Merge 时扩展名为 `.mrg`，未勾时为 `.vhd`。

**待本地验证**：GUI 需要图形显示环境；纯命令行服务器无法弹窗时，请改用步骤 5 的 CLI 方式验证「同一引擎」这一结论。

#### 4.3.5 小练习与答案

**练习 1**：GUI 的 `Generate` 槽里，`overwrite` 参数在什么情况下会是 `True`？这带来什么限制？

> **参考答案**：只有当 **Create Merge Files**（`mrgCb`）勾选时 `overwrite=True`，同时扩展名变为 `.mrg`。这意味着「覆盖」与「.mrg 格式」被**捆绑**在一起，用户无法在 `.vhd` 模式下单独要求覆盖已存在的 `.vhd` 文件。若 `.vhd` 同名文件已存在且未勾 Merge，`overwrite=False` 会让 `FileWriter` 拒绝覆盖（具体行为见 `PsiPyUtils.FileWriter`）。这个限制正是综合实践要补的「强制覆盖」开关的动机。

**练习 2**：为什么说 GUI 的 `Generate` 是一个「适配层」，而不是「业务逻辑」？

> **参考答案**：因为它只做三件 GUI 专属的事——从控件读参数、把参数喂给 `TbGenerator.ReadHdl`/`Generate`、把异常转弹窗。真正的「解析 VHDL、按段落写出 testbench」全部由 `TbGenerator` 完成。把 GUI 换成 CLI 或网页前端，引擎这行代码都不用动——这正是「外壳与引擎分离」架构的价值。

### 4.4 QErrorMessage：异常捕获与错误弹窗

#### 4.4.1 概念说明

`Generate` 槽函数最外层裹着一个 `try ... except Exception as e:`，并在 `except` 里调用 `QErrorMessage(parent=self).showMessage(str(e))`。这看似不起眼的一行，其实体现了 GUI 编程的一条重要原则：**绝不能让异常把整个程序崩掉**。

对比 CLI 的处理方式：CLI 在 `__main__` 里也用 `try/except`，但它的 `except` 只是 `print("ERROR: " + str(e)); exit(-1)`（见 [TbGen.py:312-314](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L312-L314)）——打印到终端、然后**退出进程**。这对命令行程序完全合理：出错就报错退出，调用方看退出码。

但 GUI 程序**不能这么干**。用户正点着按钮，如果因为「源文件路径写错了」这种小错就让整个窗口闪退，体验极差。正确的做法是：**把异常「兜住」，翻译成一个友好的弹窗**，告诉用户哪里错了，然后**让窗口继续活着**，等用户修正后重试。`QErrorMessage` 就是 PyQt5 提供的「专门用来显示错误信息」的现成对话框。

关于 `QErrorMessage` 有两个概念值得先建立：

- 它是一个**对话框控件**，构造时 `QErrorMessage(parent=self)` 把它挂到主窗口下（同样是为了父子对象生命周期管理）。
- `showMessage(text)` 把一段文本塞进对话框并显示。注意 Qt 文档里 `QErrorMessage` 的 `showMessage` 通常是**非阻塞（modeless）**的——它弹出后，主窗口的事件循环仍在转；但 `Generate` 槽函数到 `except` 就返回了，不会继续执行后续（出错的）代码。

#### 4.4.2 核心流程

把 `try/except` 加进来后，`Generate` 的控制流变成：

```text
进入 Generate 槽
   │
   ▼
try:
   ├─ 读参数、校验、清目录、ReadHdl、Generate ...
   │        │
   │        └─ 任何一行抛异常（如源文件不存在、缺 FREQ 标签、未知 VHDL 类型）
   │
   └─ except Exception as e:
          └─ QErrorMessage(parent=self).showMessage(str(e))
                  │
                  └─ 弹出错误对话框，显示异常文本
                            │
                            ▼
                   槽函数返回，主窗口继续可用（不退出进程）
```

关键点：`except Exception as e` 捕获的是**所有继承自 `Exception` 的异常**（[TbGenerator](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py) 引擎里可能抛出的包括 `FileNotFoundError`、自定义 `UnknownVhdlType`、缺 `FREQ`/`CLK` 标签时的普通 `Exception` 等）。它们统统被翻译成同一形式：一段文本弹窗。这是一种**统一的错误出口（single error sink）**设计。

> 注意 `except Exception` **不会**捕获 `KeyboardInterrupt`、`SystemExit` 这类继承自 `BaseException` 而非 `Exception` 的特殊异常——这是 Python 的良好惯例，避免把「用户按 Ctrl+C」「程序主动退出」也吞掉。

#### 4.4.3 源码精读

`except` 块只有一行，但它承接了整个 `try` 体里的所有异常：

- [TbGenGui.pyw:94-95](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L94-L95)：`except Exception as e: QErrorMessage(parent=self).showMessage(str(e))`——把任意异常的字符串形式塞进错误对话框。

```python
    except Exception as e:
        QErrorMessage(parent=self).showMessage(str(e))
```

逐部分拆解这一行：

| 片段 | 含义 |
| --- | --- |
| `except Exception as e` | 捕获所有常规异常，绑定到变量 `e` |
| `QErrorMessage(parent=self)` | 现场新建一个错误对话框，父窗口是 `self`（主对话框） |
| `.showMessage(...)` | 显示消息（非阻塞） |
| `str(e)` | 把异常对象转成可读文本 |

这里有一个**用法细节**值得指出：代码**每次出错都新建一个 `QErrorMessage` 实例**，而不是复用一个。从效率看这略显浪费（每次都构造对话框），但好处是写法简单、且能避免「复用同一对话框时的状态残留」。对一个几乎只在出错时才触发的路径，这种取舍是合理的。

`str(e)` 的具体文本，取决于引擎抛的是哪种异常。举几个真实例子（这些异常都来自前面讲义讲过的源码）：

- 源文件路径不存在：`FileNotFoundError: File xxx does not exist`（[TbGenGui.pyw:73](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L73)，4.3.3 节已读过）。
- 时钟端口缺 `FREQ` 标签：`Exception: Clock xxx has not FREQ tag!`（[TbGen.py:55](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L55)，见 [u4-l3](u4-l3-clocks-resets-processes-signals.md)）。
- 复位端口缺 `CLK` 标签：`Exception: Reset xxx has not CLK tag!`（[TbGen.py:72](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L72)）。
- 端口遇到未知 VHDL 类型：`UnknownVhdlType`（[TbGen.py:187](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L187) 的 `_DutSignals` 捕获它并降级，但 `GetPortValue` 在别处仍可能抛出，见 [u2-l2](u2-l2-port-and-generic-tags.md)）。

无论哪一种，最终都会变成一行 `str(e)`，由 `QErrorMessage` 弹给用户。这种「引擎抛异常、外壳负责展示」的分工，正是 4.3 节「适配层」思想在错误处理上的延续。

#### 4.4.4 代码实践

**实践目标**：亲手触发一次错误弹窗，验证「异常不会让 GUI 崩溃」。

**操作步骤**：

1. 运行 `python TbGenGui.pyw`。
2. 在 **Source File** 输入框里**故意填一个不存在的路径**，例如 `/tmp/does_not_exist.vhd`（也可以留空——空串也不是合法文件）。
3. 选一个合法的目标目录（或随便填一个存在的目录）。
4. 点 **Generate TB**。

**需要观察的现象**：程序**不会崩溃**、窗口仍然活着；弹出一个 `QErrorMessage` 对话框，里面显示类似 `File /tmp/does_not_exist.vhd does not exist` 的文本。

**预期结果**：看到错误弹窗；关闭弹窗后，主窗口仍可继续操作（修正路径后能再次点 Generate）。

**待本地验证**：需要图形显示环境。若无图形环境，可改为命令行模拟：写一个小脚本直接 `from TbGenGui import TbGenGui` 之外的引擎调用，捕获 `TbGenerator().ReadHdl("不存在.vhd")` 抛出的异常并 `print(str(e))`，验证「同一异常文本」会出现在弹窗里。

#### 4.4.5 小练习与答案

**练习 1**：为什么 GUI 用 `QErrorMessage` 弹窗，而 CLI 用 `print` + `exit(-1)`？这种差别背后的原则是什么？

> **参考答案**：CLI 是「一次 invocation 跑完即退」的进程，出错打印到 stderr 并以非零码退出，方便脚本/CI 捕获失败；GUI 是「常驻窗口、等用户反复操作」的程序，出错应当**只中断本次操作、不杀掉整个进程**，否则用户每输错一次路径就要重开窗口，体验很差。背后原则是：**错误处理要匹配交互模型**——批处理重「退出码」，交互重「恢复能力」。

**练习 2**：如果删掉 `try/except`，让 `Generate` 槽里抛出的异常直接逃逸，会发生什么？

> **参考答案**：异常会冒泡出槽函数，进入 PyQt5 的事件循环。Qt 默认会把它打印到终端（stderr），但**槽函数就此中断**，本次生成失败；多数情况下窗口不会立刻消失（事件循环仍在转），但行为变得不可预测，且用户得不到任何可见的错误提示（因为没了 `QErrorMessage`）。所以 `try/except + QErrorMessage` 的真正价值是「**给用户一个可见、可理解的错误反馈**」，而不只是「防崩溃」。

## 5. 综合实践

把本讲四个最小模块（类骨架、`LoadSrc`/`LoadDst`、`Generate`、`QErrorMessage`）串起来，完成下面这个**端到端的小扩展任务**：给 GUI 新增一个「**Force Overwrite**」复选框，让 `.vhd` 模式也能强制覆盖已存在文件，并把它正确接到 `TbGenerator.Generate` 的 `overwrite` 参数上。

> 提醒：本任务要求你**修改 `TbGenGui.pyw` 源码**（这是学习性质的扩展练习）。如果你不想动仓库里的原始文件，请先复制一份再改，例如 `cp TbGenGui.pyw TbGenGui_mine.pyw`，然后在副本上操作。

**任务背景**：4.3 节已经指出，当前 GUI 把 `overwrite` 与 Merge 复选框**捆绑**——只有勾 Merge（`.mrg` 模式）才 `overwrite=True`。这意味着，若目标目录里已存在同名的 `xxx_tb.vhd` 且你不勾 Merge，`FileWriter(overwrite=False)` 会拒绝覆盖。我们要补一个独立开关，把「是否覆盖」从「是否 Merge」里解耦。

**操作步骤**：

1. **加控件 + 连布局**（对应 4.1 节）。在 `__init__` 的横向布局里，仿照 `clrCb`/`mrgCb` 再加一个复选框（示例代码，非项目原有代码）：

   ```python
   # 示例代码：在 hLayout 里追加一个复选框（紧挨在 self.mrgCb 之后）
   self.forceCb = QCheckBox("Force Overwrite")
   hLayout.addWidget(self.forceCb)
   ```

   对照 [TbGenGui.pyw:43-48](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L43-L48)，确认你把它放进了同一个 `hLayout`，于是它会与 Clear、Merge 并排显示。

2. **改 Generate 槽里的 overwrite 逻辑**（对应 4.3 节）。把原来的「`overwrite` 仅由 Merge 决定」改成「Force Overwrite 也能开启覆盖」（示例代码，非项目原有代码）：

   ```python
   # 示例代码：解耦 overwrite 与 Merge
   tbGen = TbGenerator()
   tbGen.ReadHdl(src)
   overwrite = self.forceCb.isChecked()    # ← 新增：Force 开关
   if self.mrgCb.isChecked():
       ext = ".mrg"
       overwrite = True                     # Merge 始终覆盖（保持原语义）
   else:
       ext = ".vhd"
   tbGen.Generate(dst, ext, overwrite=overwrite)
   ```

   对照原代码 [TbGenGui.pyw:84-93](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L84-L93)：你只是把 `overwrite = False` 这一行换成了 `overwrite = self.forceCb.isChecked()`，其余结构不变。注意把 Merge 分支里的 `overwrite = True` 保留——它表达「`.mrg` 模式天然要覆盖」的原有语义。

3. **（可选）顺手修掉文案瑕疵**。把 [TbGenGui.pyw:75](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGenGui.pyw#L75) 那行的 `format(src)` 改成 `format(dst)`，让「目标目录不存在」的错误提示显示正确的路径。

4. **验证**（对应 4.3、4.4 节）。运行修改后的 GUI：

   - 先正常生成一次到某目录，确认里面有了 `xxx_tb.vhd`。
   - **不勾** Force Overwrite、**不勾** Merge，再点一次 Generate，观察 `FileWriter` 是否因 `overwrite=False` 而拒绝覆盖（具体表现取决于 `PsiPyUtils.FileWriter`，可能是弹错误窗或静默不动）。
   - **勾上** Force Overwrite、**不勾** Merge，再点 Generate，观察同名 `.vhd` 是否被成功覆盖。
   - 故意填一个不存在的源文件路径点 Generate，确认 `QErrorMessage` 仍能正常弹出（即你的改动没有破坏错误处理）。

**需要观察的现象**：

- 新增的 Force Overwrite 复选框出现在 Clear、Merge 右边，三者并排。
- 勾 Force Overwrite 后，`.vhd` 模式也能覆盖已存在文件；不勾则保持原有的「不覆盖」行为。
- Merge 复选框的行为不变（勾它仍然走 `.mrg` 且覆盖）。
- 错误路径仍弹 `QErrorMessage`，窗口不崩溃。

**预期结果**：你成功把「是否覆盖」从「是否 Merge」解耦——这就是一次最小但完整的「**控件 → 信号槽 → 引擎参数**」三段式扩展，贯穿了本讲的全部四个最小模块。

**待本地验证**：`FileWriter(overwrite=...)` 在 `overwrite=False` 且文件已存在时的确切行为（报错 / 静默 / 抛异常）由 `PsiPyUtils` 决定，本讲无法在不读该外部库的前提下断言；若现象与预期不符，请到 [PsiPyUtils 仓库](https://github.com/paulscherrerinstitute/PsiPyUtils) 查 `FileWriter` 的实现确认。

## 6. 本讲小结

- `TbGenGui` 是继承自 `QDialog` 的窗口类，它的 `__init__` 一次性完成「**控件 + 布局 + 信号槽**」三件套：纵向主布局 `QVBoxLayout` 里嵌一个横向子布局 `QHBoxLayout`（放两个复选框），三个按钮分别 `connect` 到 `LoadSrc`/`LoadDst`/`Generate`。
- 控件是否存到 `self`，取决于「后续槽函数要不要读写它」：纯展示的 `QLabel` 不存，要被读写的 `srcLine`/`clrCb` 等都存。
- `LoadSrc`/`LoadDst` 用 `QFileDialog.getOpenFileName` / `getExistingDirectory` 弹标准对话框，并用 `self.lastDirectory` 实现**跨调用的目录记忆**，还特意 `+ "/.."` 跳到上一级（因为 DUT 源文件与 testbench 通常分放兄弟目录）；两者都靠 `if file/dir != "":` 挡住用户取消。
- `Generate` 槽是一个**适配层**：从控件读参数，喂给同一台引擎 `TbGenerator.ReadHdl(src)` + `Generate(dst, ext, overwrite)`，与 CLI 的 `__main__` 几乎逐行对应；GUI 把 Merge 复选框映射为 `ext=".mrg"` + `overwrite=True`，与 CLI 的 `-mrg` / `overwrite=args.mrg` 语义一致；GUI **没有** `-force` 的对应物（复选框本身就是确认）。
- `Generate` 整体裹在 `try/except Exception` 里，`except` 用 `QErrorMessage(parent=self).showMessage(str(e))` 把**任意**异常转成可见弹窗，保证「出错不杀进程、用户能看到反馈」——这是 GUI 错误处理区别于 CLI（`print` + `exit(-1)`）的根本原则。
- 综合实践中，你用一个「Force Overwrite」复选框把 `overwrite` 从 Merge 解耦，完成了一次贯穿「控件 → 布局 → 槽 → 引擎参数」的最小扩展。

## 7. 下一步学习建议

本讲你已把 GUI 的四个最小模块逐行读透，并验证了「外壳与引擎分离」。接下来建议：

1. 如果你想把 GUI 旁边的 CLI 外壳也彻底吃透（尤其是 `-clear`/`-force` 的清理与确认逻辑、`-mrg` 与 `overwrite` 的关系、以及 `FileWriter` 的缩进格式化），进入下一讲 [u6-l2 CLI 参数、目录清理与合并文件机制](u6-l2-cli-args-clear-merge.md)。它会与本讲形成「CLI ↔ GUI」的完整对照。
2. 如果你想做更深的二次开发——例如**新增一个标签**或**支持一个新 VHDL 类型**，让它从 `DutInfo` 贯穿到 `TbGen` 的输出（而不仅仅改 GUI 外壳），进入 [u6-l3 扩展实践：添加新标签与新 VHDL 类型](u6-l3-extension-new-tag-and-type.md)。
3. 若你对 GUI 用的外部库 `PsiPyUtils.FileWriter` 的 `overwrite`、缩进（`IncIndent`/`DecIndent`/`RemoveFromLastLine`）机制好奇，建议结合 [u4-l2 Generate 主流程](u4-l2-generate-flow-and-single-tb.md) 一并阅读 `PsiPyUtils` 源码，理解「GUI/CLI 喂参数 → 引擎调用 → FileWriter 落盘」这条完整链路。
