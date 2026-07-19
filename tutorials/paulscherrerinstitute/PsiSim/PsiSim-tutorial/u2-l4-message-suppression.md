# 消息抑制机制

## 1. 本讲目标

本讲聚焦 PsiSim 的「消息抑制（Message Suppression）」机制。读完本讲，你应当能够：

- 说清楚 `compile_suppress` 与 `run_suppress` 两条命令解决了什么问题、把数据存进了哪个状态变量。
- 逐步追踪一次 `compile_suppress 135,1236` 调用，准确写出 `CompileSuppress` 变量在调用前后的字符串值变化。
- 解释去重用的 `regexp -nocase` 为什么加 `-nocase`，并指出这种「把消息号当正则模式做子串匹配」的去重方式存在的边界情况。
- 说清楚抑制字符串最终如何衔接到 SAL（模拟器抽象层），以及为什么它**只对 Modelsim 生效**，对 GHDL/Vivado 无效。

本讲依赖 u2-l1（命名空间、状态变量与 `init`），并会再次回到那 10 个命名空间状态变量中的两个：`CompileSuppress` 与 `RunSuppress`。

## 2. 前置知识

在进入源码前，先用通俗语言建立两个直觉。

**什么是「消息抑制」？**
仿真器在编译和仿真时会往控制台打印大量告警/提示信息，例如「信号未被驱动」「数值被截断」等。这些告警在大型回归测试里往往是**已知且无害**的噪音，会把真正的错误淹没。仿真器（如 Modelsim）通常允许你告诉它「**这几个编号的消息别再打印了**」，这就是「消息抑制」。

PsiSim 把这件事抽象成了两条对称的命令：

| 命令 | 作用阶段 | 写入的状态变量 |
| --- | --- | --- |
| `compile_suppress <msgNos>` | 编译阶段（`vcom`/`vlog`） | `CompileSuppress` |
| `run_suppress <msgNos>` | 仿真阶段（`vsim`） | `RunSuppress` |

**TCL 字符串与列表的边界**
PsiSim 把这两个变量当作**普通字符串**用（不是 TCL list），靠「每个消息号后跟一个逗号」来分隔。理解这一点非常关键，因为它决定了后面 `split` 与 `regexp` 的行为。

**TCL `regexp` 的返回值**
`regexp ?选项? 模式 字符串`：若「模式」在「字符串」中匹配到，返回 `1`，否则返回 `0`。`-nocase` 表示大小写不敏感。

## 3. 本讲源码地图

本讲只涉及两个文件，集中在 PsiSim.tcl 的几个小区域：

| 文件 | 作用 |
| --- | --- |
| `PsiSim.tcl` | 全部实现。本讲关注：状态变量声明（第 22–23 行）、`init` 重置（第 371–372 行）、`compile_suppress`（第 394–405 行）、`run_suppress`（第 410–421 行）、消费 `CompileSuppress` 的 `sal_compile_file`（第 162–170 行）、消费 `RunSuppress` 的 `sal_run_tb`（第 224–232 行）、以及两处「桥梁」`compile`（第 585 行）与 `run_tb`（第 787、826 行）。 |
| `CommandRef.md` | 命令文档。`compile_suppress`（第 319–341 行）与 `run_suppress`（第 343–346 行）。 |

数据流全景（一句话）：用户调用 `compile_suppress`/`run_suppress` → 字符串被拼接进 `CompileSuppress`/`RunSuppress` → 编译/运行命令再把它读出来，交给 Modelsim 的 `-suppress` / `+nowarn` 开关。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 CompileSuppress/RunSuppress 的用途** —— 它们是什么、为什么需要。
- **4.2 消息编号的拼接与去重** —— 字符串是怎么一点点拼起来的，去重怎么实现（本讲核心）。
- **4.3 与 SAL 的衔接** —— 拼好的字符串最终如何被编译/运行流程消费。

### 4.1 CompileSuppress/RunSuppress 的用途

#### 4.1.1 概念说明

在 u2-l1 里我们已经知道，PsiSim 用 10 个命名空间状态变量充当「内存登记表」。其中两个专门记录「要抑制哪些消息」：

- `CompileSuppress`：编译期要抑制的消息号集合。
- `RunSuppress`：仿真期要抑制的消息号集合。

它们和 `Sources`、`TbRuns` 一样，遵循「**配置阶段写入、运行阶段消费**」的统一节奏——只不过 `Sources`/`TbRuns` 描述的是「编译/仿真什么」，而这两个变量描述的是「编译/仿真时要安静到什么程度」。

为什么要在 PsiSim 层面做这件事，而不是直接写在 Modelsim 工程里？因为 PsiSim 的全部价值就是「用纯文本脚本描述仿真」并对版本控制友好（见 u1-l1）。把要抑制的消息号写进 `config.tcl`，意味着团队成员能**在 Git diff 里看到**某次新增/删除了哪条抑制规则，而不必去比对二进制工程文件。

#### 4.1.2 核心流程

这两个变量的生命周期非常简单：

```
init            →  把两者都置为空串 ""
compile_suppress →  往 CompileSuppress 追加（带去重）
run_suppress    →  往 RunSuppress 追加（带去重）
compile_files   →  sal_compile_file 读 CompileSuppress，交给 vcom/vlog
run_tb          →  sal_run_tb 读 RunSuppress，交给 vsim
```

注意一个重要事实：**两条命令本身只负责「写字符串」，完全不碰仿真器**。真正把字符串翻译成仿真器开关的工作，全部推迟到 SAL 层（见 4.3）。

#### 4.1.3 源码精读

先看声明与重置。两个变量在命名空间层只声明、不赋值（u2-l1 已讲过这条规律）：

[PsiSim.tcl:L22-L23](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L22-L23) —— `CompileSuppress` 与 `RunSuppress` 的命名空间级声明（仅声明，由 `init` 赋初值）。

真正赋初值发生在 `init` 里，统一置为空串：

[PsiSim.tcl:L371-L372](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L371-L372) —— `init` 把两个变量都清成 `""`，这是「去重判断的起点」。

再看文档对这两条命令的描述，确认它们的职责划分：

[CommandRef.md:L319-L341](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L319-L341) —— `compile_suppress` 文档：抑制编译期告警，多个编号「用逗号分隔」。

[CommandRef.md:L343-L346](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L343-L346) —— `run_suppress` 文档：与 `compile_suppress` 完全等价，只是作用在仿真阶段。

> ⚠️ 注意：文档说「多个编号用逗号分隔（delimited using a comma）」，但源码里的分隔逻辑其实不是按逗号切分的（见 4.2.3 的逐行追踪）。这是一个文档与实现存在细微出入的真实例子，读源码时要留意。

#### 4.1.4 代码实践

**实践目标**：在源码层面确认「这两条命令只写字符串、不碰仿真器」。

**操作步骤**：

1. 打开 PsiSim.tcl，定位 `compile_suppress`（第 394 行）与 `run_suppress`（第 410 行）两个 proc。
2. 通读这两个 proc 的全部语句，找它们是否调用了任何 `vcom`/`vlog`/`vsim`/`exec` 之类会触发仿真器动作的命令。

**需要观察的现象**：这两个 proc 的函数体里**只有** `variable`、`split`、`foreach`、`regexp`、字符串拼接这几类纯 TCL 操作，**没有任何对外部仿真器的调用**。

**预期结果**：能用自己的话总结——「`compile_suppress`/`run_suppress` 是纯字符串累加器，副作用仅限于修改一个命名空间变量」。如果你看到了任何 `vcom`/`vsim` 调用，说明你看错位置了。

#### 4.1.5 小练习与答案

**练习 1**：为什么 PsiSim 要把「编译期抑制」和「运行期抑制」拆成两个变量，而不是合并成一个？

**参考答案**：因为两个阶段调用的是 Modelsim 的不同命令——编译期是 `vcom`/`vlog`（用 `-suppress`），运行期是 `vsim`（用 `+nowarn`），两者接受的告警编号体系不同、传递开关也不同。拆开存放，才能在各自阶段拼出正确的命令行。

**练习 2**：在 PsiSim.tcl 中，`CompileSuppress` 这个名字一共出现在哪几行？分别属于「声明 / 重置 / 写入 / 读取」中的哪一类？

**参考答案**：第 22 行（声明）、第 371 行（`init` 重置）、第 395/399/401 行（`compile_suppress` 写入）、第 164/167 行（`sal_compile_file` 读取）、第 585 行（`compile` proc 里又读了一次——详见 4.3.3，这一处其实是「未使用的读取」）。

---

### 4.2 消息编号的拼接与去重

#### 4.2.1 概念说明

这是本讲的核心。`compile_suppress`/`run_suppress` 用的是一种**极简的字符串累加 + 子串去重**方案，没有用 TCL 的 list 或 dict。理解它需要抓住三点：

1. **累加格式**：每加入一个消息号，就在后面补一个逗号，整体形如 `135,1236,`（注意**末尾有一个拖尾逗号**）。
2. **切分方式**：用 `split $msgNos`（**未传第二个参数**）按**空白字符**切分输入，而不是按逗号。
3. **去重判定**：用 `regexp -nocase $msg $CompileSuppress` 判断「这个消息号是否已经在字符串里出现过」。

#### 4.2.2 核心流程

`compile_suppress` 与 `run_suppress` 的实现**完全对称**（一个动 `CompileSuppress`，一个动 `RunSuppress`），伪代码如下：

```
proc compile_suppress(msgNos):
    msgList = split(msgNos)          # 按空白切分，不是按逗号！
    for msg in msgList:
        exists = regexp -nocase msg CompileSuppress   # 子串匹配去重
        if exists == 0:              # 没出现过才追加
            CompileSuppress = CompileSuppress + msg + ","
```

关键性质：

- 输入 `compile_suppress 135,1236` 时，因为字符串里**没有空白**，`split` 只切出一个元素 `"135,1236"`，整段被当作一个消息号追加 → 结果是 `"135,1236,"`。
- 输入 `compile_suppress "135 1236"`（空格分隔）时，`split` 切出两个元素 `"135"`、`"1236"`，分别追加 → 结果也是 `"135,1236,"`。
- 也就是说：**因为每段都补了拖尾逗号，无论用户用逗号还是空格分隔，最终都能拼出 Modelsim 能识别的逗号列表**。这是一种「容错的巧合设计」，而非显式分支。

#### 4.2.3 源码精读

逐行精读 `compile_suppress`（`run_suppress` 完全同构，不再重复）：

[PsiSim.tcl:L394-L405](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L394-L405) —— `compile_suppress` 全文：切分、去重、追加。

关键四行拆解：

- 第 396 行 `set msgList [split $msgNos]`：`split` 未传分隔符参数，按任意空白字符（空格/制表/换行等）切分。因此 `"135,1236"` 不会被逗号切开。
- 第 399 行 `set exists [regexp -nocase $msg $CompileSuppress]`：把 `$msg` 当作**正则模式**，在 `CompileSuppress` 字符串里做子串匹配；`-nocase` 表示大小写不敏感；匹配到返回 `1`，否则 `0`。
- 第 400–402 行 `if {$exists == 0} { variable CompileSuppress $CompileSuppress$msg, }`：仅当**没匹配到**时，才把 `msg` 和一个逗号拼到末尾。注意 `variable CompileSuppress $CompileSuppress$msg,` 这一行的求值顺序——先在右侧读到旧值，再拼上 `$msg` 和字面量 `,`，最后写回命名空间变量。

`run_suppress` 与之一一对应，只是把变量名换成 `RunSuppress`：

[PsiSim.tcl:L410-L421](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L410-L421) —— `run_suppress` 全文，结构与 `compile_suppress` 完全对称。

> **边界情况（源码阅读型观察，未实际运行，待本地验证）**：由于 `regexp` 做的是「子串匹配」而非「等值匹配」，会出现一种微妙的假去重。若 `CompileSuppress` 已是 `"1236,"`，再调用 `compile_suppress 123`，则 `regexp "123" "1236,"` 会因为 `"1236"` 里含有子串 `"123"` 而返回 `1`，于是 `123` 被**误判为已存在而静默丢弃**。读源码时应意识到这条隐患；它对「一次调用、用逗号连写多个编号」的典型用法没有影响，但在「分批追加、编号互为前缀」时会暴露。

#### 4.2.4 代码实践（本讲指定实践）

**实践目标**：跟踪一次 `compile_suppress 135,1236` 调用，写出 `CompileSuppress` 在调用前后的字符串值变化，并解释去重 `regexp` 为什么加 `-nocase`。

**操作步骤**：

1. 假设脚本刚执行过 `init`，此时由 [PsiSim.tcl:L371](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L371) 知 `CompileSuppress = ""`（空串）。这正是 README 示例（`compile_suppress 135,1236`）执行前的状态。
2. 执行 `compile_suppress 135,1236`，按下表逐步求值（**纯纸面推演，无需运行仿真器**）：

| 步骤 | 表达式 | 结果 |
| --- | --- | --- |
| 入参 | `msgNos` | `"135,1236"` |
| 第 396 行 | `msgList = [split "135,1236"]`（按空白切） | `{135,1236}`（**单元素**，因无空白） |
| 第 397 行循环 | `msg` 取唯一元素 | `"135,1236"` |
| 第 399 行 | `exists = [regexp -nocase "135,1236" ""]` | `0`（空串里找不到） |
| 第 401 行 | `CompileSuppress = "" . "135,1236" . ","` | `"135,1236,"` |

**需要观察的现象**：因为 `split` 按空白而非逗号切分，`135,1236` 整段被当作**一个**消息号进入循环；又因初值是空串，去重判定为「不存在」，于是整段连同拖尾逗号一起被追加。

**预期结果**：

- 调用前：`CompileSuppress = ""`
- 调用后：`CompileSuppress = "135,1236,"`

这个值随后会被 [PsiSim.tcl:L167](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L167) 拼成 Modelsim 的 `-suppress 135,1236,`，Modelsim 按逗号解析出 `135` 和 `1236` 两条抑制规则——拖尾逗号对 Modelsim 无害，所以最终行为正确。

> 若你想验证「空格分隔」分支，把调用改成 `compile_suppress "135 1236"`：`split` 会切出 `{135 1236}`，循环跑两次，最终 `CompileSuppress` 同样是 `"135,1236,"`。这两种写法等价，是 4.2.2 提到的「容错巧合」。**待本地验证**：可在装有 Modelsim 的环境里跑一遍，观察 `vcom` 是否真的少打印了 135、1236 两条告警。

**解释 `-nocase`**：Modelsim 的消息号通常是纯数字（如 135、1236、8684），**纯数字没有大小写之分**，所以对典型用法而言，`-nocase` 在功能上**没有任何影响**。它存在的原因是一种**防御性/鲁棒性**考虑——一旦将来出现含字母的告警标识（例如形如 `Num_Opt`、`FXDC_1` 这类字母数字混合 ID），`-nocase` 能保证 `ABC` 与 `abc` 被视作同一条消息而不会重复登记。对数字零成本、对字母数字更健壮，因此作者统一加了 `-nocase`。（源码未留注释，此为依据语义的合理推断。）

#### 4.2.5 小练习与答案

**练习 1**：连续调用两次 `compile_suppress 135,1236`，`CompileSuppress` 最终是什么？为什么？

**参考答案**：仍是 `"135,1236,"`。第二次调用时，`regexp -nocase "135,1236" "135,1236,"` 返回 `1`（整段已存在），命中 `if {$exists == 0}` 的否定分支，不再追加。这就是去重在起作用。

**练习 2**：先调用 `compile_suppress 1236`，再调用 `compile_suppress 123`，`CompileSuppress` 会变成什么？这符不符合预期？

**参考答案**：第一次后 `CompileSuppress = "1236,"`；第二次 `regexp "123" "1236,"` 因子串匹配命中（`"1236"` 含 `"123"`）返回 `1`，于是 `123` **被误判已存在而丢弃**，最终仍是 `"1236,"`。这**不符合**「二者应并存」的预期，是 4.2.3 指出的子串去重隐患的真实例子（待本地验证）。

**练习 3**：为什么作者用 `split $msgNos`（不传分隔符）而不是 `split $msgNos ","`？

**参考答案**：因为消息号本身就用「追加拖尾逗号」的方式拼接进变量，若再用逗号切分输入，反而会和变量内部的分隔符语义混淆；按空白切分配合「每段补逗号」，使得「逗号连写」和「空格连写」两种输入习惯都能得到正确的逗号列表，是一种容错设计。

---

### 4.3 与 SAL 的衔接

#### 4.3.1 概念说明

拼接好的 `CompileSuppress`/`RunSuppress` 只是字符串，要真正生效，必须被翻译成具体仿真器的命令行开关。这件翻译工作是 **SAL（Simulator Abstraction Layer，模拟器抽象层）** 做的——u1-l2 已经介绍过 SAL 是屏蔽 Modelsim/GHDL/Vivado 差异的内部层。

本模块要回答三个问题：

1. 谁来读这两个变量？
2. 读出来后拼成什么命令？
3. **三种仿真器都生效吗？**（答案：否，只对 Modelsim 生效。）

#### 4.3.2 核心流程

两条变量的消费路径（注意 push/pull 的不对称）：

```
【编译期，pull 式】
compile_files → compile → sal_compile_file(...)
                               └─ 内部自己 variable CompileSuppress（直接读命名空间变量）
                                      └─ Modelsim: -suppress $CompileSuppress
                                      └─ GHDL/Vivado: 忽略

【仿真期，push 式】
run_tb → sal_run_tb(lib, tbName, tbArgs, timeLimit, RunSuppress, ...)
                                            └─ 作为参数传入
                                                   └─ Modelsim: +nowarn$RunSuppress
                                                   └─ GHDL/Vivado: 忽略
```

一个重要的不对称：`RunSuppress` 是**作为参数**显式传进 `sal_run_tb` 的；而 `CompileSuppress` 是 `sal_compile_file` **自己伸手去读**命名空间变量的（参数列表里根本没有它）。这种不一致会直接体现在 4.3.3 的源码里。

#### 4.3.3 源码精读

**编译期：sal_compile_file 自己读 `CompileSuppress`**

[PsiSim.tcl:L162-L170](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L162-L170) —— `sal_compile_file` 在 Modelsim 分支把 `CompileSuppress` 拼进 `vcom`/`vlog` 的 `-suppress` 开关。

逐行看：

- 第 164 行 `variable CompileSuppress`：在 proc 内链接命名空间变量（pull 式读取）。
- 第 167 行 `set args "-work $lib $vFlags -suppress $CompileSuppress $fileOptions -quiet $path"`：把整个 `CompileSuppress` 字符串（如 `"135,1236,"`）直接塞进 `-suppress` 后面，形成 `-suppress 135,1236,`。
- 第 168–174 行：VHDL 走 `vcom`、Verilog 走 `vlog`，都用 `{*}$args` 展开。
- 第 175–205 行的 GHDL 与 Vivado 分支：**完全没有引用 `CompileSuppress`**，直接用各自的 `ghdl -a` / `xvhdl` 命令编译。这意味着 `compile_suppress` 对 GHDL/Vivado **没有任何效果**。

**一处「未使用的读取」（架构观察）**：

[PsiSim.tcl:L585](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L585) —— `compile` proc 里也写了一行 `variable CompileSuppress`，但紧接着的 `sal_compile_file` 调用（第 605 行）**并没有把它当参数传出去**。

也就是说，`compile` proc 把变量链接进来了却从未使用——它是**事实上的死代码**。真正消费 `CompileSuppress` 的是 `sal_compile_file` 内部那一行 `variable CompileSuppress`（第 164 行）。读源码时若不细看，很容易误以为是 `compile` 把抑制串「喂」给了 SAL，实则 SAL 是「自己取」的。

**仿真期：sal_run_tb 接收 `RunSuppress` 作为参数**

[PsiSim.tcl:L224-L232](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L224-L232) —— `sal_run_tb` 的形参 `suppressMsgNo` 接收 `RunSuppress`，并在 Modelsim 分支拼成 `+nowarn`。

逐行看：

- 第 224 行形参表 `{lib tbName tbArgs timeLimit suppressMsgNo {wave ""}}`：`suppressMsgNo` 是第 5 个位置参数，由调用方 `run_tb` 传入 `RunSuppress`（见下方第 826 行）。
- 第 227–230 行：仅当 `suppressMsgNo` 非空时，构造 `+nowarn$suppressMsgNo`（如 `+nowarn8684,3479,...,`）。
- 第 231 行：把 `$supp` 拼进 `vsim` 命令行。
- 第 241–306 行的 GHDL 与 Vivado 分支：同样**完全忽略** `suppressMsgNo`。所以 `run_suppress` 也只对 Modelsim 生效。

**push 式调用的证据**：

[PsiSim.tcl:L826](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L826) —— `run_tb` 在循环里把 `RunSuppress` 作为第 5 个实参传给 `sal_run_tb`（对照第 787 行 `variable RunSuppress` 先读出变量）。

对比 4.2 中的 `CompileSuppress` 路径：`RunSuppress` 是「**调用方读出 → 作参数传入**」，而 `CompileSuppress` 是「**SAL 内部自己读**」。同属一套机制，却用了两种耦合风格，这是阅读 PsiSim 时值得记下的架构细节。

> `launch_tb`（交互调试）走的是同一条 `RunSuppress` 通路：[PsiSim.tcl:L943](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L943) 与 [PsiSim.tcl:L952](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L952) 分别把 `RunSuppress` 传给 `sal_launch_tb` / `sal_run_tb`，最终同样落到 Modelsim 的 `+nowarn`（见 [PsiSim.tcl:L309-L316](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L309-L316)）。

#### 4.3.4 代码实践

**实践目标**：在源码层面验证「抑制机制只对 Modelsim 生效」。

**操作步骤**：

1. 打开 `sal_compile_file`（第 162 行起），分别检查 Modelsim / GHDL / Vivado 三个 `if` 分支，搜索 `CompileSuppress` 出现在哪些分支。
2. 打开 `sal_run_tb`（第 224 行起），同样检查三分支里 `suppressMsgNo` 出现在哪。
3. 列一张表，记录三种仿真器各自是否使用抑制变量。

**需要观察的现象**：`CompileSuppress` 只在 Modelsim 分支（第 164、167 行）出现；`suppressMsgNo` 只在 Modelsim 分支（第 228–229 行）被用来构造 `+nowarn`。GHDL 与 Vivado 分支里**搜不到**这两个名字。

**预期结果**：

| 仿真器 | `compile_suppress` 是否生效 | `run_suppress` 是否生效 |
| --- | --- | --- |
| Modelsim | ✅（`-suppress`） | ✅（`+nowarn`） |
| GHDL | ❌（分支内未引用） | ❌（分支内未引用） |
| Vivado | ❌（分支内未引用） | ❌（分支内未引用） |

**结论**：如果你用 `init -ghdl` 或 `init -vivado` 跑回归，写再多 `compile_suppress`/`run_suppress` 也不会改变输出——它们只在 Modelsim 下被消费。这是源码阅读型实践，无需运行即可得出（待本地验证：在 GHDL 下故意写一条 `compile_suppress` 并打印 `CompileSuppress` 的值，会看到变量确实被写入了，但 GHDL 编译命令里并不含它）。

#### 4.3.5 小练习与答案

**练习 1**：`compile` proc 第 585 行的 `variable CompileSuppress` 到底有没有作用？删掉它会影响功能吗？

**参考答案**：没有作用，是死代码。真正消费 `CompileSuppress` 的是 `sal_compile_file` 内部第 164 行的 `variable CompileSuppress`。删掉第 585 行不会影响编译或抑制行为（待本地验证），它只是一个历史遗留的、未使用的变量链接。

**练习 2**：为什么 `RunSuppress` 要作为参数传给 `sal_run_tb`，而 `CompileSuppress` 却让 `sal_compile_file` 自己读？

**参考答案**：没有功能上的必然理由，更像是两段代码由不同时机/习惯写成所留下的**风格不一致**。push（传参）耦合更松、更显式；pull（内部 `variable`）耦合更紧、更隐式。认识到这种不一致，有助于后续在 u4-l3（扩展新仿真器）时决定统一成哪种风格。

**练习 3**：在 GHDL 模式下调用 `compile_suppress 135,1236` 后，`CompileSuppress` 变量本身有没有被修改？它有没有效果？

**参考答案**：变量**会被修改**（因为 `compile_suppress` 只写字符串、与仿真器无关，`CompileSuppress` 会变成 `"135,1236,"`）；但**没有任何效果**，因为 GHDL 分支的 `sal_compile_file` 根本不读这个变量。这是个典型陷阱：命令「成功执行」却不等于「生效」。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个端到端的「纸上追踪」任务。

**场景**：一个 `config.tcl` 里写了 README 风格的抑制规则：

```tcl
#suppress messages
compile_suppress 135,1236
run_suppress 8684,3479,3813,8009,3812
```

随后 `run.tcl` 在 Modelsim 下依次执行 `init` → `source config.tcl` → `compile_files -all` → `run_tb -all`。

**请你完成**：

1. 画出从「用户调用」到「Modelsim 命令行」的完整数据流图，标出每一步变量值的变化。
2. 写出 `compile_files -all` 编译某个 VHDL 文件时，最终交给 Modelsim 的 `vcom` 命令里 `-suppress` 后面的字符串。
3. 写出 `run_tb -all` 仿真某个测试台时，最终交给 Modelsim 的 `vsim` 命令里 `+nowarn` 后面的字符串。
4. 如果把 `init` 换成 `init -ghdl`，重新评估第 2、3 步的命令行会发生什么变化，并解释原因。

**参考答案要点**：

1. 数据流：`init`（`CompileSuppress=""`, `RunSuppress=""`）→ `compile_suppress 135,1236`（`CompileSuppress="135,1236,"`）→ `run_suppress 8684,...`（`RunSuppress="8684,3479,3813,8009,3812,"`）→ `compile_files` 调 `sal_compile_file`，内部 pull 读 `CompileSuppress` → `vcom ... -suppress 135,1236, ...`；`run_tb` 把 `RunSuppress` 作参数 push 给 `sal_run_tb` → `vsim ... +nowarn8684,3479,3813,8009,3812, ...`。
2. `-suppress` 后是 `135,1236,`（带拖尾逗号，Modelsim 可正常解析）。
3. `+nowarn` 后是 `8684,3479,3813,8009,3812,`。
4. `init -ghdl` 下，`CompileSuppress`/`RunSuppress` 变量**仍会被写**成同样的值，但 GHDL 分支的 `sal_compile_file`（`ghdl -a`）与 `sal_run_tb`（`ghdl --elab-run`）都**不读**这两个变量，所以编译/仿真命令里**不会出现**任何抑制开关——抑制完全失效。这印证了 4.3 的结论：消息抑制目前是 Modelsim 专属能力。

## 6. 本讲小结

- `compile_suppress`/`run_suppress` 是两个**纯字符串累加器**，分别写 `CompileSuppress`/`RunSuppress`，本身不碰任何仿真器。
- 累加格式是「每段消息号 + 拖尾逗号」，形如 `135,1236,`；`split` 按**空白**切分（非逗号），配合拖尾逗号使逗号/空格两种输入都能拼出合法列表。
- 去重靠 `regexp -nocase $msg $var` 做**子串匹配**——数字场景下 `-nocase` 无实际作用，是面向字母数字 ID 的防御性写法；但子串匹配会带来「编号互为前缀时假去重」的隐患。
- 两个变量都由 `init` 清零为 `""`，是去重判定的起点。
- 衔接 SAL 时存在**风格不对称**：`CompileSuppress` 被 `sal_compile_file` pull 式自读（`compile` 里的同名链接是死代码），`RunSuppress` 被 `run_tb` push 式作参数传入 `sal_run_tb`。
- 两种抑制都**只对 Modelsim 生效**（`-suppress` / `+nowarn`）；GHDL 与 Vivado 分支完全忽略这两个变量——「命令执行成功」不等于「生效」。

## 7. 下一步学习建议

- 下一篇 **u2-l5 编译流程与过滤** 会展开 `compile_files` → `compile` → `sal_compile_file` 的完整调用链，届时你会再次看到本讲的 `CompileSuppress` 是如何被裹进每一次 `vcom`/`vlog` 调用的，建议把本讲的 pull 式读取记在脑子里带过去对照。
- 之后 **u2-l6 仿真运行与脚本钩子（run_tb）** 会详细讲 `run_tb` 如何遍历 `TbRuns` 并把 `RunSuppress` 传给 `sal_run_tb`，与本讲的 push 式通路直接衔接。
- 若你对 SAL 的 dispatch 模式（`if {$Simulator == "Modelsim"} ... elseif ...`）感兴趣，可以直接跳到单元 3 的 **u3-l1 SAL 设计与 dispatch 模式**，从架构层面理解「为什么 GHDL/Vivado 分支天然就没有抑制能力」。
- 想动手验证时，建议在装有 Modelsim 的环境里复现本讲的「调用前后字符串值变化」与「子串假去重」两个观察，把它们从「待本地验证」变成「已确认」。
