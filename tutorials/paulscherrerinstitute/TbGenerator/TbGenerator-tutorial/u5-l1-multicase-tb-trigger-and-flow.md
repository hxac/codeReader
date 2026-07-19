# 多用例 TB 的触发与生成流程

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `TESTCASES` 这个文件级标签是如何被 `TbInfo.__init__` 翻译成布尔「模式开关」`isMultiCaseTb` 的，并解释「它是模式开关而非数量判断」这句话的含义。
- 默写出多用例模式下 `Generate` 的产物清单：1 个主 TB + 1 个 TB 包 + 每用例 1 个 case 包（共 \( 2 + N \) 个文件，\( N \) 为用例数），并指出主 TB 在库声明区多出的两段 `use` 语句来自哪里。
- 画出 `NextCase` / `ProcessDone` / `AllProcessesDone_c` 这三个信号在 `p_tb_control` 与各个 `p_<process>` 之间的「握手时序」：谁递增 `NextCase`、谁等待、谁置完成位、仿真如何收尾。
- 对照生成的 `*_tb.vhd`，逐行解释 `_Processes` 多用例分支里那个嵌套 `for` 循环（外层遍历进程、内层遍历用例）产出的 VHDL，并说清每个用例为何调用 `work.<tb>_case_<case>.<proc>(...)` 而不是直接写测试代码。
- 区分多用例模式与单用例模式在 `_Processes`、`_TbControl`、`_GenericConstants` 三处代码分支上的差异。

本讲是 u5「多文件多用例 testbench」单元的第一讲，**只讲「触发与主流程」**：什么标签触发多用例、`Generate` 在多用例下额外生成哪些文件、主 TB 里的进程如何按 `NextCase` 调度各用例。至于 TB 包里 `Generics_t` 记录怎么定义、case 包里 `procedure` 签名怎么推断方向，这些「包内部实现」留到 u5-l2。

## 2. 前置知识

本讲直接承接 u4-l3。进入本讲前，请确认你已掌握：

- **单用例 `_Processes` 分支**（u4-l3）：每个 `p_<process>` 的结构是「等复位释放 → 用户占位 `assert` → 置 `ProcessDone(TbProcNr_<p>_c) <= '1'` → `wait;`」。本讲要做的，就是把其中「用户占位」那一整段替换成「遍历用例、调用 case 包过程」。
- **`_TbControl` 单用例分支**（u4-l3）：`p_tb_control` 等所有复位释放后，执行一句 `wait until ProcessDone = AllProcessesDone_c;`，再把 `TbRunning <= false` 结束仿真。
- **脚手架信号**（u4-l3 / u4-l2 的 `_TbControlSignals`）：`TbRunning`（布尔，仿真继续的开关）、`NextCase`（整数，初值 `-1`）、`ProcessDone`（每进程一比特的向量）、`AllProcessesDone_c`（全 1 常量）、`TbProcNr_<p>_c`（每进程在向量里的下标）。
- **`isMultiCaseTb` 与 `testCases`**（u2-l3）：`isMultiCaseTb = Tags.TESTCASES in info.fileScopeTags`（只判键是否存在），`testCases` 是把标签值归一成 list 后的用例名列表。
- **`Generate` 的写作顺序**（u4-l2）：主 TB 内部按「文件头 → 库声明 → 实体 → 架构声明区 → begin → 并发语句（实例化、控制、时钟、复位、进程）→ end;」誊抄。

一个贯穿全讲的直觉：**多用例不是「另一种生成器」，而是同一个 `Generate` 在若干关键位置多了一个 `if self.tbInfo.isMultiCaseTb:` 分支**。掌握了这个布尔值在哪里分流，你就掌握了多用例与单用例的全部差异。换句话说，本讲本质上是「把 u4-l3 里被 `if ... else:` 折叠起来的那个 `if` 分支单独展开讲」。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `TbGen.py` | 引擎核心，定义 `TbGenerator` 类 | `Generate` 里多用例的两处分流（库声明区的包引用、末尾的包文件生成）、`_Processes` 与 `_TbControl` 的多用例分支、`_GenericConstants` 多用例分支里的 `Generics_c` 常量 |
| `TbInfo.py` | TB 数据模型 | `isMultiCaseTb` / `testCases` 的由来，`TbPkgDeclaration` 与 `TbCaseDeclaration` 两个新增的库声明方法 |
| `MultiFileTb.py` | 多文件 TB 的包生成器 | `WriteTbPkg`（生成 TB 包）、`WriteCasePkg`（每个用例一个 case 包）——本讲只看它们「被谁调用、产出什么文件」，内部细节见 u5-l2 |
| `example/multiCaseTb/psi_common_async_fifo.vhd` | 多用例示例 DUT | 在 simpleTb 基础上多了 `$$ TESTCASES=Full,Empty $$`，是本讲的对照样本 |
| `example/multiCaseTb/run.bat` | 示例启动脚本 | 一行 `py TbGen.py -src ... -dst .\tb -clear -force`，即本讲实践的运行入口 |

> 说明：`FileWriter` 来自外部依赖 `PsiPyUtils`（不在本仓库内），本讲沿用 u4-l2 / u4-l3 的推断——它是一个可链式调用、自管理缩进、能回改上一行的「写作器」。

## 4. 核心概念与源码讲解

### 4.1 isMultiCaseTb：TESTCASES 标签如何成为「模式开关」

#### 4.1.1 概念说明

回顾 u2-l3：文件级标签 `$$ TESTCASES=Full,Empty $$` 写在 VHDL 的独立注释行里，描述「这个 testbench 要跑哪几个用例」。它被 `DutInfo._ParseTags` 收集进 `fileScopeTags` 字典，键统一小写为 `"testcases"`。

`TbInfo.__init__` 拿到这个字典后，做的第一件事就是判定模式：

```python
self.isMultiCaseTb = Tags.TESTCASES in info.fileScopeTags
```

这句话的关键在于 **`in` 判断的是「键是否存在」，而不是「值里有几个用例」**。所以：

- 写 `$$ TESTCASES=Full $$`（哪怕只有一个用例）→ `isMultiCaseTb = True` → 走多用例分支，照样生成 TB 包与 case 包。
- 完全不写 `TESTCASES` → `isMultiCaseTb = False` → 走单用例分支，只生成一个主 TB。

这就是为什么 u2-l3 反复强调「`TESTCASES` 是模式开关，不是数量判断」。它决定了 `Generate` 是否生成额外文件、进程是否按用例调度，与「到底有几个用例」是两回事——一旦开关打开，哪怕只有一个用例，整套多用例机制都会启动。

`isMultiCaseTb` 一旦为真，紧接着就把用例名取出来并归一成 list：

```python
if self.isMultiCaseTb:
    self.testCases = info.fileScopeTags[Tags.TESTCASES]
    if type(self.testCases) is str:
        self.testCases = [self.testCases]
```

这里的 `if type(...) is str:` 是一个防御性归一：`_ParseTags` 对「单值」返回字符串、对「逗号分隔」返回 list（见 u2-l1）。归一之后，下游代码（`_Processes`、`_TbControl` 的 `for i, c in enumerate(self.tbInfo.testCases)`）就可以无差别地遍历，不必关心是单值还是多值。

#### 4.1.2 核心流程

```
TbInfo.__init__(dutInfo):
  isMultiCaseTb = "testcases" 是否存在于 dutInfo.fileScopeTags
  if isMultiCaseTb:
      testCases = fileScopeTags["testcases"]
      若 testCases 是 str，则包成 [testCases]   # 归一成 list
  else:
      testCases = None
  ...
```

#### 4.1.3 源码精读

[TbInfo.py:14-22](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L14-L22)：`TbInfo.__init__` 开头判定 `isMultiCaseTb` 并提取 `testCases`。第 15 行的 `in` 判断是「模式开关」的唯一定义点；第 19-20 行是单值→list 的归一。

对应的输入来自示例 DUT 的两行独立注释：

[example/multiCaseTb/psi_common_async_fifo.vhd:24-25](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/multiCaseTb/psi_common_async_fifo.vhd#L24-L25)：`$$ PROCESSES=Input,Output $$` 与 `$$ TESTCASES=Full,Empty $$`。后者正是触发多用例模式的唯一来源，解析后 `fileScopeTags = {"processes": ["Input","Output"], "testcases": ["Full","Empty"]}`，于是 `isMultiCaseTb=True`、`testCases=["Full","Empty"]`、`tbProcesses=["Input","Output"]`。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证「`TESTCASES` 是模式开关而非数量判断」。
2. **操作步骤**：
   - 复制 `example/multiCaseTb/psi_common_async_fifo.vhd` 为一个临时文件（如 `_scratch.vhd`），不要改原示例。
   - 用 Python 直接驱动引擎，绕开文件生成：

     ```python
     # 示例代码：在仓库根目录运行
     from TbGen import TbGenerator
     tbGen = TbGenerator()
     tbGen.ReadHdl("example/multiCaseTb/psi_common_async_fifo.vhd")
     print("isMultiCaseTb =", tbGen.tbInfo.isMultiCaseTb)
     print("testCases     =", tbGen.tbInfo.testCases)
     print("tbProcesses   =", tbGen.tbInfo.tbProcesses)
     ```
   - 再把临时文件里的 `$$ TESTCASES=Full,Empty $$` 改成 `$$ TESTCASES=OnlyOne $$`（只剩一个用例），重新 `ReadHdl` 那个临时文件，打印同样的三项。
   - 最后把临时文件里的 `TESTCASES` 那一整行删掉，再 `ReadHdl` 一次。
3. **需要观察的现象**：
   - 原始示例：`isMultiCaseTb = True`，`testCases = ['Full', 'Empty']`。
   - 单用例 `OnlyOne`：`isMultiCaseTb` **仍然是 `True`**，`testCases = ['OnlyOne']`（注意是单元素 list，验证了 str→list 归一生效）。
   - 删除整行：`isMultiCaseTb = False`，`testCases = None`。
4. **预期结果**：三种情况下 `isMultiCaseTb` 分别为 `True / True / False`，证明开关只取决于「键是否存在」，与用例个数无关。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `$$ TESTCASES= $$`（等号后为空）写进注释，`isMultiCaseTb` 会是 `True` 还是 `False`？

**参考答案**：`True`。因为 `Tags.TESTCASES in info.fileScopeTags` 只判键是否存在。至于空值会带来什么后续问题（例如 `testCases` 可能是空字符串、归一成 `['']`，导致生成一个名为 `_case_` 的怪文件），属于边界缺陷，本讲不展开——但这是个很好的「模式开关与数据质量解耦」的观察点。

**练习 2**：`tbProcesses`（`PROCESSES` 标签）和 `testCases`（`TESTCASES` 标签）在 `TbInfo.__init__` 里的处理方式有何相同与不同？

**参考答案**：相同——都做 `str → [str]` 的归一，都从 `fileScopeTags` 取值。不同——`tbProcesses` 有缺省值 `["Stimuli"]`（见 [TbInfo.py:26-30](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L26-L30)），且**不影响模式判定**；`testCases` 没有缺省值，且它的存在性直接决定 `isMultiCaseTb`。换言之：`PROCESSES` 决定「TB 有几个测试进程」，`TESTCASES` 决定「TB 是不是多用例模式」。

---

### 4.2 Generate 在多用例模式下的「额外产物」

#### 4.2.1 概念说明

单用例模式下，`Generate` 只产出一个文件：`{tbName}{extension}`（如 `psi_common_async_fifo_tb.vhd`）。多用例模式下，产物一下子变成「1 + 1 + N」三类：

| 产物 | 文件名（本例） | 数量 | 由谁生成 |
| --- | --- | --- | --- |
| 主 TB | `psi_common_async_fifo_tb.vhd` | 1 | `Generate` 主体（与单用例同一个 `FileWriter`） |
| TB 包 | `psi_common_async_fifo_tb_pkg.vhd` | 1 | `WriteTbPkg(...)` |
| case 包 | `psi_common_async_fifo_tb_case_Full.vhd`、`..._case_Empty.vhd` | \( N \)（=用例数） | 对每个用例调一次 `WriteCasePkg(...)` |

总数为 \( 2 + N \)。本例 \( N=2 \)，共 4 个文件。

为什么要拆出这些包？核心动机是**把「可复用的测试框架」与「每个用例的具体激励」分离**：

- 主 TB（以及 TB 包）是**机器生成、不允许手改**的调度框架（注意各段标题里的 `!DO NOT EDIT!`）。
- 每个 case 包里是一组 `procedure` 的**空实现**（带 `assert ... severity warning` 占位），用户只在这些 procedure 里填测试代码。换用例、加用例时，主 TB 与 TB 包可以重新生成而不冲掉用户写在 case 包里的代码。

> 这种「生成的骨架 + 用户填的 case 包」的分工，正是多用例 TB 区别于单用例 TB（后者把占位 `assert` 直接写进主 TB 进程）的根本价值。u5-l2 会逐行打开 case 包里的 `procedure` 签名。

#### 4.2.2 核心流程

```
Generate(tbPath, extension, overwrite):
  写主 TB 文件（与单用例相同的 FileWriter）：
      ... 文件头、库声明、用户包 ...
      if isMultiCaseTb:                      # ← 分流点 A：主 TB 多引用两个包
          TbPkgDeclaration(f)                #   use work.<tb>_pkg.all;
          TbCaseDeclaration(f)               #   use work.<tb>_case_<case>.all;  逐用例
      ... 实体、架构、并发语句 ...
  if isMultiCaseTb:                          # ← 分流点 B：多生成包文件
      WriteTbPkg(tbPath, ...)                #   生成 1 个 TB 包
      for case in testCases:                 #   逐用例
          WriteCasePkg(tbPath, ..., case,...)#     生成 1 个 case 包
```

两个分流点都在 `Generate` 里：**分流点 A** 让主 TB 在库声明区「看见」这些包（否则主 TB 里的 `work.<tb>_case_<case>.<proc>(...)` 调用无法编译）；**分流点 B** 才真正把这些包文件写出来。两者必须同时存在，缺一不可。

#### 4.2.3 源码精读

**分流点 A：主 TB 多引用 TB 包与各 case 包**

[TbGen.py:233-236](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L233-L236)：在主 TB 的库声明段（`UserPkgDelcaration` 之后、实体声明之前），多用例模式额外调用两个方法：

[TbInfo.py:57-60](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L57-L60)：`TbPkgDeclaration` 写出 `library work;` 与 `use work.<tbName>_pkg.all;`，让主 TB 能引用 TB 包里定义的 `Generics_t` 记录类型与未导出常量。

[TbInfo.py:62-66](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L62-L66)：`TbCaseDeclaration` 对 `testCases` 里每个用例写一行 `use work.<tbName>_case_<case>.all;`，让主 TB 能调用各 case 包里的 `procedure`。注意它返回 `f`（链式），而 `TbPkgDeclaration` 没有显式 `return`（返回 `None`）——但因为它是 `Generate` 里链式调用的「最后一棒」之一（后面紧跟的是 `_EntityDeclaration(f)` 重新起头），这个不一致在本场景下不会出错。这是一个值得注意的**代码异味**：若将来有人在 `TbPkgDeclaration(f).Xxx()` 之后链式调用就会触发 `AttributeError`。

**分流点 B：生成包文件**

[TbGen.py:255-260](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L255-L260)：主 TB 的 `with FileWriter(...)` 块结束后，`Generate` 检查 `isMultiCaseTb`，若是则调 `WriteTbPkg(...)` 生成 TB 包，再 `for case in self.tbInfo.testCases:` 循环对每个用例调 `WriteCasePkg(...)`。注意这两步用的是**新的 `FileWriter`**（各自在 `WriteTbPkg` / `WriteCasePkg` 内部 `with` 打开），与主 TB 的 `FileWriter` 完全独立。

这两个函数的「外壳」一眼即明（内部细节见 u5-l2）：

[MultiFileTb.py:13-15](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/MultiFileTb.py#L13-L15)：`WriteTbPkg` 算出包名 `<tbName>_pkg`，用 `FileWriter` 打开 `<path>/<tbName>_pkg<extension>`，写版权头、库声明、用户包，然后写包头（`Generics_t` 记录、未导出常量）与空包体。

[MultiFileTb.py:59-61](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/MultiFileTb.py#L59-L61)：`WriteCasePkg` 算出包名 `<tbName>_case_<case>`，打开对应文件，为每个 `tbProcesses` 里的过程写一个 `procedure` 声明（包头）与空实现（包体）。

#### 4.2.4 代码实践

1. **实践目标**：用对照实验看清「多用例比单用例多出哪些文件」。
2. **操作步骤**：
   - 先跑单用例示例：`py TbGen.py -src example/simpleTb/psi_common_async_fifo.vhd -dst example/simpleTb/tb -clear -force`（路径按本机调整），用 `ls example/simpleTb/tb` 列出生成文件。
   - 再跑多用例示例：`cd example/multiCaseTb` 后执行 `run.bat`（等价于 `py ..\..\TbGen.py -src .\psi_common_async_fifo.vhd -dst .\tb -clear -force`），用 `ls tb` 列出生成文件。
   - 把两次的文件清单并排对比。
3. **需要观察的现象**：
   - simpleTb：只有 1 个 `psi_common_async_fifo_tb.vhd`。
   - multiCaseTb：4 个文件——`psi_common_async_fifo_tb.vhd`、`psi_common_async_fifo_tb_pkg.vhd`、`psi_common_async_fifo_tb_case_Full.vhd`、`psi_common_async_fifo_tb_case_Empty.vhd`。
4. **预期结果**：多用例多出的 3 个文件，分别对应「1 个 TB 包 + 2 个 case 包」，与 \( 2+N \) 公式吻合。
5. 若本机无 `py` 启动器或无 `PsiPyUtils`，可用 4.1.4 的 Python 片段打印 `tbInfo.tbName` 与 `tbInfo.testCases`，手动推算应生成 `2 + len(testCases)` 个文件——**待本地验证**实际文件名。

#### 4.2.5 小练习与答案

**练习 1**：如果 `TESTCASES=Full,Empty,Overflow`（三个用例），会生成几个文件？分别叫什么？

**参考答案**：\( 2 + 3 = 5 \) 个。主 TB `psi_common_async_fifo_tb.vhd`、TB 包 `psi_common_async_fifo_tb_pkg.vhd`、case 包 `..._case_Full.vhd`、`..._case_Empty.vhd`、`..._case_Overflow.vhd`。

**练习 2**：为什么主 TB 必须在库声明区 `use work.<tb>_case_<case>.all;`，而不能只在 `Generate` 末尾生成 case 包文件就够了？

**参考答案**：因为主 TB 的 `_Processes` 多用例分支会写出 `work.<tb>_case_<case>.<proc>(...)` 这样的过程调用（见 4.3）。VHDL 要求被调用的 `procedure` 所在的包必须先 `use` 可见，否则主 TB 无法编译。所以「分流点 A（主 TB 引用包）」与「分流点 B（生成包文件）」缺一不可——前者解决可见性，后者提供定义。

---

### 4.3 _Processes 多用例分支：用 NextCase 调度各用例

#### 4.3.1 概念说明

单用例 `_Processes` 里，每个 `p_<process>` 直接把「用户占位 `assert`」写在进程体内（u4-l3）。多用例模式下，这段占位被替换成一个**嵌套循环**：外层 `for p in tbProcesses` 仍然是「为每个测试过程写一个 `process`」，但进程体内部**再嵌一层 `for i, c in enumerate(testCases)`**，让同一个进程顺序跑完所有用例。

每个用例在进程体内的「四步小流程」是：

1. `wait until NextCase = i;`——**等调度器点名**：等到 `p_tb_control` 把 `NextCase` 置成自己的编号 `i`。
2. `ProcessDone(TbProcNr_<p>_c) <= '0';`——**先把自己的完成位拉低**，表示「我开始干活了」，避免上一用例残留的全 1 状态被误判。
3. `work.<tb>_case_<case>.<proc>(<args>, Generics_c);`——**调用 case 包里的过程**，把真正的测试代码执行权交出去（用户在 case 包里填实现）。
4. `wait for 1 ps;` 然后 `ProcessDone(TbProcNr_<p>_c) <= '1';`——**等一个 delta 后置完成位**，告诉调度器「我这个进程的本用例跑完了」。

所有进程在用例 `i` 上都跑完（即 `ProcessDone` 全 1）后，调度器才会把 `NextCase` 推进到 `i+1`，于是所有进程的 `wait until NextCase = i+1` 同时放行，进入下一用例。这是一种典型的**屏障同步（barrier）**：每个用例是一道栅栏，所有进程都到达（置位）后，调度器才放行下一道。

#### 4.3.2 核心流程

```
_Processes(f):
  标题: 多用例 → "Processes !DO NOT EDIT!"   # 提醒整段机器生成
  for p in tbProcesses:                       # 外层：每个测试过程一个 process
      写 "p_<p> : process begin"
      if isMultiCaseTb:
          for i, c in enumerate(testCases):   # 内层：同一进程顺序跑各用例
              写 "-- <c>"
              写 "wait until NextCase = <i>;"
              写 "ProcessDone(TbProcNr_<p>_c) <= '0';"
              args = 用 GetPortsForProcess(p) 取出 PROC=<p> 的端口，拼成逗号串
              写 "work.<tb>_case_<c>.<p>(<args>, Generics_c);"
              写 "wait for 1 ps;"
              写 "ProcessDone(TbProcNr_<p>_c) <= '1';"
      else:
          ... 单用例占位分支（u4-l3）...
      写 "wait; end process;"
```

进程参数列表 `args` 的来源是 `GetPortsForProcess`，它本质就是一次 `FilterForTag`：

[TbInfo.py:47-48](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L47-L48)：`GetPortsForProcess(process)` 返回所有 `PROC=<process>` 的端口（大小写不敏感）。本例对 `Input` 进程，会筛出 `InClk`、`InData`、`InVld`、`InRdy`、`OutRdy`（标了 `PROC=Output,Input`）、`OutFull`（标了 `PROC=Input,Output`）等端口。

#### 4.3.3 源码精读

[TbGen.py:86-104](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L86-L104)：`_Processes` 全方法。注意三个层次：

- 第 87-90 行：多用例时标题加 `!DO NOT EDIT!`，因为整段进程体都是机器生成的调度逻辑，用户不应改主 TB，而应去 case 包里写代码。
- 第 92 行：外层 `for p in self.tbInfo.tbProcesses`，为每个测试过程写一个 `process`。
- 第 96-104 行：**多用例内层循环**，即上面「四步小流程」的来源。第 101 行用生成器表达式把 `GetPortsForProcess(p)` 的端口名拼成 `args`；第 102 行把 `tb`/`case`/`proc`/`args` 套进 `work.<tb>_case_<case>.<proc>(<args>, Generics_c);` 模板。

以本例的 `Input` 进程为例，第 96-104 行产出的 VHDL 大致是（缩进风格跟随 `FileWriter`，下面是逻辑等价形式）：

```vhdl
-- Input
p_Input : process
begin
  -- Full
  wait until NextCase = 0;
  ProcessDone(TbProcNr_Input_c) <= '0';
  work.psi_common_async_fifo_tb_case_Full.Input(InClk, InData, InVld, InRdy, OutRdy, OutFull, Generics_c);
  wait for 1 ps;
  ProcessDone(TbProcNr_Input_c) <= '1';
  -- Empty
  wait until NextCase = 1;
  ProcessDone(TbProcNr_Input_c) <= '0';
  work.psi_common_async_fifo_tb_case_Empty.Input(InClk, InData, InVld, InRdy, OutRdy, OutFull, Generics_c);
  wait for 1 ps;
  ProcessDone(TbProcNr_Input_c) <= '1';
  wait;
end process;
```

对比单用例分支 [TbGen.py:105-116](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L105-L116)：那里是「等复位 → `assert False report "Insert your code here!"` → 置完成位」，整段是写死在主 TB 里的占位。多用例把这段占位**整体替换**成「遍历用例、调用 case 包过程」，主 TB 因此变得「纯调度、无业务」，这正是多用例模式的设计意图。

#### 4.3.4 代码实践

1. **实践目标**：把「进程参数列表」与「VHDL 过程调用」对应起来，验证 `GetPortsForProcess` 的筛选结果。
2. **操作步骤**：
   - 运行 4.1.4 的 Python 片段（`ReadHdl` 多用例示例），再加两行：

     ```python
     for p in tbGen.tbInfo.tbProcesses:
         ports = tbGen.tbInfo.GetPortsForProcess(p)
         print(p, "->", [port.name for port in ports])
     ```
   - 打开生成的 `tb/psi_common_async_fifo_tb.vhd`，定位 `p_Input : process` 段，找到 `work.psi_common_async_fifo_tb_case_Full.Input(...)` 这一行的参数。
3. **需要观察的现象**：
   - `Input` 进程的参数列表应包含所有标了 `PROC=Input`（含 `PROC=Output,Input`）的端口：`InClk, InData, InVld, InRdy, OutRdy, OutFull`。
   - 生成的 VHDL 调用行的参数顺序与 `GetPortsForProcess` 返回的端口顺序一致，末尾固定追加 `Generics_c`。
4. **预期结果**：Python 打印的端口名列表 = VHDL 调用括号里的实参列表（去掉末尾 `Generics_c`）。这说明 `_Processes` 完全靠 `GetPortsForProcess`（即 `FilterForTag`）决定过程实参，端口标签是唯一真相源。
5. **待本地验证**：端口在列表里的**确切顺序**取决于 `self.dutInfo.ports` 的原始顺序（即 VHDL 里 port 声明顺序），不同版本的解析器若改变端口排序，生成的参数顺序也会变——请以本机实际生成为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么每个用例开头要写一句 `ProcessDone(TbProcNr_<p>_c) <= '0';`，能不能省掉？

**参考答案**：不能省。上一个用例结束时该进程把自己的完成位置了 1。若进入下一用例时不先清零，`ProcessDone` 向量可能在调度器检测前仍保持全 1，导致调度器误以为新用例「所有进程已完成」而立刻推进 `NextCase`，跳过本用例。先清零、跑完再置位，才能让「全 1」可靠地表示「本用例大家都跑完了」。

**练习 2**：进程调用 `wait for 1 ps;` 后才置完成位，这个 1 ps 的作用是什么？

**参考答案**：给被调用的 case 过程产生的信号变化一个 delta 周期去传播、稳定。过程内部可能驱动了 DUT 的输入信号，DUT 的输出需要经过若干 delta 才更新。`wait for 1 ps` 引入一个微小时延，确保置完成位之前信号已经收敛，避免采样到过渡中的毛刺值。这是仿真层面的一个「让尘埃落定」的惯用法。

---

### 4.4 _TbControl 多用例分支：递增 NextCase 驱动用例序列

#### 4.4.1 概念说明

`p_tb_control` 是整个 testbench 的「总调度」。单用例模式下，它只要做两件事：等复位释放、等所有进程完成（`wait until ProcessDone = AllProcessesDone_c`），然后结束仿真。多用例模式下，它在两者之间多了一个**驱动用例序列的循环**：

```
等复位释放
for i, c in enumerate(testCases):
    NextCase <= i            # 点名：现在跑第 i 个用例
    wait until ProcessDone = AllProcessesDone_c   # 等所有进程跑完本用例
TbRunning <= false           # 用例全跑完，结束仿真
```

`p_tb_control` 与各 `p_<process>` 通过两个信号完成握手：

- `NextCase`（整数）：由 `p_tb_control` **写**、各 `p_<process>` **读**（用 `wait until NextCase = i` 等待点名）。
- `ProcessDone`（向量）：由各 `p_<process>` **写**各自那一比特、`p_tb_control` **读**整个向量（用 `wait until ProcessDone = AllProcessesDone_c` 汇合）。

完整的时序（以 `testCases=["Full","Empty"]`、两进程 `Input`/`Output` 为例）：

```
p_tb_control            p_Input                p_Output
-----------             -------                --------
等复位释放               等复位释放              等复位释放
NextCase <= 0    ──→    wait until NextCase=0  wait until NextCase=0
                        放行: 清位→调 Full.Input→置位   清位→调 Full.Output→置位
wait until ProcessDone=AllProcessesDone_c   (屏障：两进程都置位)
NextCase <= 1    ──→    wait until NextCase=1  wait until NextCase=1
                        放行: 清位→调 Empty.Input→置位  清位→调 Empty.Output→置位
wait until ProcessDone=AllProcessesDone_c   (屏障)
TbRunning <= false      wait;                  wait;
                        (时钟进程跳出 while TbRunning loop，仿真结束)
```

这是一把**双向握手的屏障调度**：调度器推进 `NextCase` 后必须等所有进程确认（全 1）；进程跑完一个用例后必须等调度器推进 `NextCase` 才进下一个。任何一方都不会「跑过头」。

#### 4.4.2 核心流程

```
_TbControl(f):
  写 "p_tb_control : process begin"
  if 存在复位端口:
      写 "wait until <所有复位都无效>;"      # 与单用例相同
  if isMultiCaseTb:
      for i, c in enumerate(testCases):
          写 "-- <c>"
          写 "NextCase <= <i>;"
          写 "wait until ProcessDone = AllProcessesDone_c;"
  else:
      写 "wait until ProcessDone = AllProcessesDone_c;"
  写 "TbRunning <= false;"
  写 "wait; end process;"
```

注意：多用例与单用例在「等复位」和「结束仿真」两端完全一致，**唯一差异是中间这段循环**——单用例只等一次全完成，多用例每用例「点名一次 + 等一次全完成」。

#### 4.4.3 源码精读

[TbGen.py:122-141](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L122-L141)：`_TbControl` 全方法。

- 第 126-129 行：等所有复位释放。这段对单/多用例都执行，逻辑来自 `FilterForTag(ports, TYPE, "rst")` 取复位端口、用 `GetPortValue(r, False)` 拼出「复位无效」条件。本例有 `InRst`、`OutRst` 两个复位，所以会生成 `wait until InRst = '0' and OutRst = '0';`（具体无效值由 `LOWACTIVE` 决定，本例高有效故为 `'0'`）。
- 第 130-134 行：**多用例调度循环**。`NextCase <= i` 点名，`wait until ProcessDone = AllProcessesDone_c` 屏障等待。
- 第 135-136 行：单用例分支，只等一次。
- 第 138 行：`TbRunning <= false;`——用例（单用例下是整个测试）跑完后，把仿真继续开关关掉。

`NextCase`、`ProcessDone`、`AllProcessesDone_c` 这三个信号本身的声明在 `_TbControlSignals`：

[TbGen.py:166-174](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L166-L174)：第 169 行 `signal NextCase : integer := -1;`（初值 `-1`，确保仿真开始时没有任何进程误以为「点名了用例 0」而提前放行——必须等 `p_tb_control` 真正写入 0）；第 170 行 `ProcessDone` 向量宽度由 `len(tbProcesses)-1` 决定（本例两进程 → `0 to 1`）；第 171 行 `AllProcessesDone_c` 是同宽全 1 常量；第 172-173 行为每个进程定义下标常量 `TbProcNr_<p>_c`。这套脚手架单/多用例**完全共用**，`NextCase` 在单用例下虽然声明了但无人写也无人读，只是闲置。

收尾链路（与 u4-l3 一致）：`TbRunning <= false` 后，各时钟进程的 `while TbRunning loop` 条件不再成立，跳出循环、执行 `wait;`，仿真随之结束。

#### 4.4.4 代码实践

1. **实践目标**：在生成的 `*_tb.vhd` 里完整还原「NextCase 递增 ↔ ProcessDone 汇合」的握手时序。
2. **操作步骤**：
   - 运行 `example/multiCaseTb/run.bat`（或等价命令）生成 TB。
   - 打开 `tb/psi_common_async_fifo_tb.vhd`，定位 `p_tb_control : process`。
   - 在 `_TbControl` 段里数 `NextCase <=` 出现的次数与取值；数 `wait until ProcessDone = AllProcessesDone_c` 出现的次数。
   - 再定位 `p_Input : process` 与 `p_Output : process`，数各自的 `wait until NextCase =` 次数与取值。
3. **需要观察的现象**：
   - `p_tb_control` 里有两条 `NextCase <= 0;` 与 `NextCase <= 1;`，紧跟两条 `wait until ProcessDone = AllProcessesDone_c;`。
   - `p_Input` 与 `p_Output` 各有两条 `wait until NextCase = 0;` 与 `wait until NextCase = 1;`。
   - 三个进程的「用例数」一致（都等于 `len(testCases)=2`），形成两两配对的屏障。
4. **预期结果**：调度器与每个测试进程都遍历了同样的用例序列；调度器写 `NextCase`、各进程读 `NextCase`；各进程写自己的 `ProcessDone` 比特、调度器读整个 `ProcessDone` 向量。这就是 4.4.1 时序表在源码里的落地。
5. **待本地验证**：若在 VHDL 仿真器里跑这个 TB，由于 case 包里全是空实现（只 `assert ... warning`），仿真会迅速跑完两个用例并结束——可在波形里观察 `NextCase` 从 `-1 → 0 → 1` 的跳变与 `ProcessDone` 向量的逐位置位。

#### 4.4.5 小练习与答案

**练习 1**：`NextCase` 的初值为什么是 `-1` 而不是 `0`？

**参考答案**：若初值为 0，仿真零时刻各测试进程的 `wait until NextCase = 0` 可能立即成立（与复位等待的先后顺序产生竞争），导致进程在 `p_tb_control` 尚未真正「点名」时就放行。初值 `-1` 保证 `NextCase` 不等于任何合法用例编号（用例编号从 0 开始），所有进程必然先卡在 `wait until NextCase = i`，直到 `p_tb_control` 在复位释放后显式写入 0 才放行。这是一个用「非法初值」充当「未启动哨兵」的常见技巧。

**练习 2**：多用例模式下，`p_tb_control` 末尾的 `TbRunning <= false;` 会在什么时候执行？它与各 `p_<process>` 里的 `wait;` 谁先谁后？

**参考答案**：`TbRunning <= false` 在**最后一个用例**的 `wait until ProcessDone = AllProcessesDone_c` 之后执行，即所有进程跑完最后一个用例、全部置位之时。之后各测试进程的 `for` 循环结束，执行各自的 `wait;` 挂起。两者几乎同时发生（同一仿真时刻的 delta 内）：`p_tb_control` 关 `TbRunning`，测试进程随后 `wait;`，时钟进程则在下一个循环判断时发现 `TbRunning` 为假而跳出 `while TbRunning loop`。仿真在所有进程都 `wait;` 后因无事件而自然结束。

---

### 4.5（补充）_GenericConstants 多用例分支：打包导出 generic 为 Generics_c

> 本节是对「主 TB 在多用例下还多出什么」的补充，帮助你把 4.2 里提到的 `Generics_c` 与 `_Processes` 调用里的 `Generics_c` 实参对上号。它不属于本讲的四个核心模块，但读懂它能避免一个常见疑惑：「`Generics_c` 这个常量是哪来的？」

单用例模式下，导出的 generic（`EXPORT=true`）只进 TB 实体的 `generic` 子句与 `generic map`（见 u2-l2 / u4-l2）。多用例模式下，因为每个 case 包的过程都需要拿到**同一组**导出 generic 的值，工具把它们**打包成一个记录常量 `Generics_c : Generics_t`**，作为过程调用的最后一个实参统一传递。

[TbGen.py:154-163](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L154-L163)：`_GenericConstants` 末尾的多用例分支，在「Fixed Generics」「Not Assigned Generics」两段之后，新增「Exported Generics」段，写出 `constant Generics_c : Generics_t := ( ... );`，聚合体里逐个列出 `EXPORT=true` 的 generic（本例 `Width_g`、`Depth_g`）。若没有任何导出 generic，则写一个 `Dummy => true` 占位（VHDL 不允许空记录）。

这里的 `Generics_t` 记录类型本身定义在 TB 包里（u5-l2 会展开 [MultiFileTb.py:24-30](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/MultiFileTb.py#L24-L30)）。于是数据流闭环：**TB 包定义 `Generics_t` 类型 → 主 TB 用它声明 `Generics_c` 常量并填值 → 主 TB 进程把 `Generics_c` 作为实参传给 case 包里的过程 → case 包过程签名最后一个形参也是 `constant Generics_c : Generics_t`**。这条链解释了为什么主 TB 必须 `use work.<tb>_pkg.all;`（4.2 的分流点 A）——否则 `Generics_t` 这个类型名在主 TB 里不可见。

## 5. 综合实践

把本讲的触发、产物、调度三件事串起来，做一个「改用例、读时序」的端到端练习：

1. **改造输入**：把 `example/multiCaseTb/psi_common_async_fifo.vhd` 复制一份到临时目录，将第 25 行改成 `$$ TESTCASES=Full,Empty,Overflow $$`（新增 `Overflow` 用例）。
2. **预测**：在不运行的情况下，先在纸上写出：(a) 会生成几个文件、各自文件名；(b) 主 TB 里 `p_tb_control` 会有几条 `NextCase <=`；(c) 每个 `p_<process>` 会有几条 `wait until NextCase =`；(d) 会生成几个 case 包文件。
3. **运行验证**：用 `py TbGen.py -src <临时文件> -dst <临时目录>/tb -clear -force` 生成，`ls` 列出文件，与预测 (a)(d) 对照。
4. **读时序**：打开主 TB，数 `NextCase <=` 与 `wait until ProcessDone = AllProcessesDone_c` 的次数，与预测 (b) 对照；数 `p_Input` 里 `wait until NextCase =` 的次数与取值（应为 0、1、2），与预测 (c) 对照。
5. **填代码**：打开新生成的 `..._case_Overflow.vhd`，找到 `Input` 过程的空实现（那条 `assert false report "Case OVERFLOW Procedure INPUT: No Content added yet!"` 占位），把它替换成一行真实激励（例如 `InVld <= '1'; InData <= (others => '1'); wait until ...`，具体视 DUT 行为而定）。重新生成主 TB（注意：**只重生成主 TB 与 TB 包会覆盖 case 包**——实际工程里需谨慎，本练习仅用于理解 case 包是「用户可编辑区」）。
6. **小结**：写一句话回答——「多用例模式相比单用例，到底多解决了什么问题？」（参考答案：把机器生成的调度骨架与用户编写的用例激励分离到不同文件，使加用例、换用例时不破坏已有测试代码。）

> 若本机无法运行（缺 `PsiPyUtils` 或 `py` 启动器），步骤 2-4 可降级为「源码阅读型实践」：直接读 `TbGen.py` 的 `_Processes` / `_TbControl` 与 `Generate` 末尾循环，**手推**出 3 个用例下的文件清单与 `NextCase` 取值序列（0、1、2），并标注「待本地验证」。

## 6. 本讲小结

- `TESTCASES` 文件级标签是**多用例模式的唯一开关**：`isMultiCaseTb = "testcases" in fileScopeTags` 只判键是否存在，与用例个数无关——哪怕一个用例也会触发整套多用例机制。
- 多用例产物为 \( 2 + N \) 个文件：1 主 TB + 1 TB 包 + \( N \) 个 case 包（\( N \)=用例数）。主 TB 在库声明区多 `use` 了 TB 包与各 case 包（`TbPkgDeclaration` / `TbCaseDeclaration`），这是「分流点 A」；`Generate` 末尾调 `WriteTbPkg` 与循环 `WriteCasePkg` 生成包文件，这是「分流点 B」。
- `_Processes` 多用例分支把单用例的「用户占位」替换成**内层遍历用例的循环**：每个用例「等 `NextCase=i` → 清完成位 → 调 `work.<tb>_case_<case>.<proc>(...)` → 置完成位」，过程实参来自 `GetPortsForProcess(p)`，末尾固定追加 `Generics_c`。
- `_TbControl` 多用例分支是**总调度**：等复位释放后，循环 `NextCase <= i` + `wait until ProcessDone = AllProcessesDone_c` 推进用例，最后 `TbRunning <= false` 结束仿真。与单用例的唯一差异是这段循环。
- `NextCase`（调度器写、进程读）与 `ProcessDone`（进程写各自比特、调度器读全向量）构成**双向握手的屏障同步**：每个用例是一道栅栏，所有进程置位后调度器才放行下一用例。`NextCase` 初值 `-1` 是「未启动哨兵」。
- 导出 generic 在多用例下被打包成记录常量 `Generics_c : Generics_t`（`_GenericConstants` 多用例分支），统一传给所有 case 过程；`Generics_t` 类型定义在 TB 包里，这正是主 TB 必须 `use work.<tb>_pkg.all;` 的原因。

## 7. 下一步学习建议

本讲只讲了「多用例的触发与主 TB 调度」，刻意把 case 包内部留白。下一讲 **u5-l2《WriteTbPkg / WriteCasePkg 与过程方向》** 正好补上这块：

- 精读 [MultiFileTb.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/MultiFileTb.py)：`WriteTbPkg` 如何定义 `Generics_t` 记录与未导出常量、`WriteCasePkg` 如何为每个过程生成声明与空实现。
- 重点理解 `PortDirectionForProcedure`：它如何根据端口的 `PROC` 标签与 `TYPE` 标签，推断过程参数是 `in` 还是 `inout`（解释 `OutRdy` 为何在 `Input` 过程里是 `inout`）。
- 建议先复习 u2-l2 关于 `PROC` 标签「端口级、单数」的论述，以及 u3-l2 关于 `VhdlPortDeclaration.direction` 的解析，再读 u5-l2 会更顺畅。

读完 u5 整个单元后，你可以进入 u6《用户接口、输出与二次开发》，从 GUI/CLI 外壳与扩展实践的角度，把整套生成器当作可二次开发的平台来使用。
