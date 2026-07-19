# VHDL 仿真流程与 testbench

## 1. 本讲目标

上一讲（[u2-l1](u2-l1-python-package-tests.md)）我们走进了 Python 实现的工程骨架，看到了「`unittest` + `assertEqual`」这一套测试基础设施。本讲我们横向跳到 **VHDL** 这一边，看清楚同一套位真语义在硬件描述语言里是如何被**编译、运行、判定成败**的。

VHDL 没有 Python 那样现成的 `unittest` 框架，于是这个库用一个非常朴素但有效的办法来组织测试：写一个 testbench 实体，里面用一个进程 `p_control` 顺序地调用一堆**自定义的 `Check*` 过程**做断言；再用一个 Tcl 脚本 `sim.tcl` 驱动 Modelsim 编译并运行它，最后**扫描仿真输出的文本日志**来判定成功还是失败。

学完本讲你应当能够：

- 说清 [sim/sim.tcl](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/sim/sim.tcl) 的完整工作流：清理 transcript → `vcom -2008` 编译包与 testbench → `vsim` + `run -all` 运行 → 用 `regexp` 扫描 transcript 判定成败。
- 理解 testbench 里 `CheckStdlv` / `CheckInt` / `CheckReal` / `CheckStdl` / `CheckBoolean` 这一族**断言式校验过程**，以及它们统一用 `###ERROR###` 前缀 + `severity error` 的报告约定。
- 解释为什么判定成败的关键是 `###ERROR###` 与 `Fatal:` 这两个关键字，以及它们分别由谁产生。
- 看懂 `p_control` 进程「`print` 分节标题 → 一组 `Check*` 调用 → 下一个分节」的组织方式，并能仿照它新增一条断言。
- 理解 testbench 顶部 `use work.en_cl_fix_pkg.all;` 如何把被测包引入作用域，以及那个本地 `to_string` 函数为何存在。

本讲同样**不深入某个定点函数的算法**（那是 Unit 3、Unit 4 的事），只关心「VHDL 测试是如何被驱动和判定的」这一**测试基础设施**。

## 2. 前置知识

阅读本讲前，你需要具备：

- **VHDL 最少概念**：知道一个 `entity` 描述对外接口、一个 `architecture` 描述行为；知道 `process` 是一段顺序执行的代码；知道 `signal` / `variable` 的区别（本讲以 `variable` 为主，但其实 testbench 里几乎不用信号）；知道 `assert ... report ... severity ...` 是 VHDL 的断言语句。
- **VHDL record（记录类型）的最少概念**：知道 `FixFormat_t` 是一个有 `Signed` / `IntBits` / `FracBits` 三个字段的记录，`(true, 3, 0)` 这种写法是**按位置**给三个字段赋值的「聚合（aggregate）」。这一点承接 [u1-l2](u1-l2-fixformat-type.md)。
- **Tcl 最少概念**：知道 Tcl 用 `set var value` 赋值、用 `[...]` 执行命令并取结果、用 `{...}` 或 `"..."` 表示字符串。本讲会顺带解释 `sim.tcl` 里出现的每一条 Tcl 命令，不要求你精通。
- 本手册 **u1-l3** 引入的 `cl_fix_width` 位宽公式 \( W = S + I + F \)，以及 **u1-l4 / u1-l5** 的 `Trunc_s` / `Round_s` / `Sat_s` / `None_s` 等常量（testbench 里会直接用到它们）。

一个贯穿全讲的核心理念来自 [u1-l1](u1-l1-overview.md)：**位真（bit-true）**。要做到三语言位真一致，VHDL 这一边必须有一套自动化测试，能把 `cl_fix_width`、`cl_fix_resize` 等函数的真实输出与期望值逐条比对。本讲讲的就是这套自动化测试的「外壳」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [sim/sim.tcl](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/sim/sim.tcl) | 仿真驱动脚本（28 行）。编译包与 testbench、运行仿真、扫描 transcript 判定成败。本讲的「导演」。 |
| [vhdl/tb/en_cl_fix_pkg_tb.vhd](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd) | VHDL testbench。内含 `to_string` 兼容函数、`Check*` 断言过程族、`print` 过程，以及唯一一个测试驱动进程 `p_control`。本讲的「演员」。 |
| [vhdl/src/en_cl_fix_pkg.vhd](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd) | 被测对象：定点运算包。testbench 通过 `use work.en_cl_fix_pkg.all;` 引用它。本讲只在「它如何被引入作用域」与「`cl_fix_width` 的真实签名」两处触及它。 |

三者的调用关系非常清晰：

```
sim.tcl  ──vcom──▶  en_cl_fix_pkg.vhd   (先编译进 work 库)
                          ▲
                          │ use work.en_cl_fix_pkg.all
sim.tcl  ──vcom──▶  en_cl_fix_pkg_tb.vhd (后编译，引用上面的包)
sim.tcl  ──vsim──▶ en_cl_fix_pkg_tb      (运行)
sim.tcl  ──regexp──▶ Transcript.transcript (扫描日志判定成败)
```

## 4. 核心概念与源码讲解

### 4.1 sim.tcl：编译、运行与 Transcript 关键字判定

#### 4.1.1 概念说明

Modelsim（及 Questa）这类 VHDL 仿真器自带一个 Tcl 命令行，可以用脚本来**批量驱动**「编译 → 仿真 → 退出」的整套流程。`sim.tcl` 就是这样一个驱动脚本。它最巧妙（也是最朴素）的地方在于：**它不靠仿真器的返回码来判定成败，而是把仿真过程中所有打印输出记录到一个文本文件 `Transcript.transcript` 里，再用正则表达式去这个文本里找两个关键字**——找到任何一个就算失败。

为什么要这样设计？因为 testbench 里的断言（`assert`）即便失败，也只会向 transcript 打印一条信息，**不会让脚本层面的命令返回非零退出码**。如果靠返回码判定，一条断言失败可能被完全忽略。所以作者用一个所有人都能看懂、所有仿真器都会写的「文本日志」作为唯一的真相来源（single source of truth），用关键字扫描来做判定。这是一种非常跨工具、跨平台、几乎不会失效的做法。

#### 4.1.2 核心流程

`sim.tcl` 的 28 行可以划成四段：

1. **清理 transcript（重置日志文件）**——保证本次判定读到的是「本次仿真」的输出，而不是上一次残留的日志。
2. **编译**——用 `vcom -2008` 把被测包和 testbench 按顺序编译进 `work` 库（包必须先于 testbench）。
3. **运行**——用 `vsim` 加载 testbench 顶层实体，`run -all` 跑完全部测试，`quit -sim` 退出仿真。
4. **判定**——打开 `Transcript.transcript`，读全文，用两个 `regexp` 找 `###ERROR###` 与 `Fatal:`，任一命中即失败。

判定逻辑用伪代码表示就是：

```
found       = 文本里是否出现 "###ERROR###"  (大小写不敏感)
foundFatal  = 文本里是否出现 "Fatal:"       (大小写不敏感)
if found 或 foundFatal:
    打印 "!!! ERRORS OCCURED IN SIMULATIONS !!!"   # 失败
else:
    打印 "SIMULATIONS COMPLETED SUCCESSFULLY"      # 成功
```

注意：**只有「两个关键字都没出现」才算成功**。这套判定对 testbench 内部的报告约定（下一节）提出了一个硬性要求——所有失败的断言都必须打印带 `###ERROR###` 前缀的文本。

#### 4.1.3 源码精读

第一段，清理并重置 transcript：

[sim.tcl:1-5](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/sim/sim.tcl#L1-L5) —— 先把 transcript 重定向到一个临时 `Dummy.transcript`（丢弃旧内容），删除可能残留的 `Transcript.transcript`，再把 transcript 重定向到 `./Transcript.transcript` 并用 `transcript on` 开启记录。这一段确保后续判定扫描的是「干净、仅属于本次仿真」的日志。

第二段，编译：

[sim.tcl:7-9](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/sim/sim.tcl#L7-L9) —— `vcom -quiet -work work -2008` 依次编译包与 testbench。三个标志的含义：`-quiet` 只输出必要信息；`-work work` 编译进名为 `work` 的库；`-2008` 使用 **VHDL-2008** 标准（testbench 与包都用到了 VHDL-2008 特性，例如 `cl_fix_width((true,3,0))` 这种内联 record 聚合在某些老标准下需要更繁琐的写法）。**顺序很关键**：包必须在 testbench 之前编译，否则 testbench 里的 `use work.en_cl_fix_pkg.all;` 会找不到目标。

第三段，运行：

[sim.tcl:11-14](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/sim/sim.tcl#L11-L14) —— `vsim -quiet work.en_cl_fix_pkg_tb` 加载 testbench 顶层，`run -all` 让仿真一直跑到没有更多事件（testbench 末尾的 `wait;` 会让它停下，见 4.4），`quit -sim` 结束本次仿真、把日志落盘。

第四段，判定（本讲最核心的一段）：

[sim.tcl:16-23](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/sim/sim.tcl#L16-L23) —— 用 Tcl 的 `open` / `read` 把整个 `Transcript.transcript` 读进变量 `transcriptContent`，然后用两次 `regexp -nocase` 分别检测 `###ERROR###` 与 `Fatal:`。`-nocase` 表示大小写不敏感；`{Fatal:}` 外层用花括号是为了让其中的冒号不被 Tcl 当作特殊字符。`echo $found; echo $foundFatal` 把两个 0/1 结果打印出来，便于调试。

[sim.tcl:24-28](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/sim/sim.tcl#L24-L28) —— 最终判定：只要 `found` 或 `foundFatal` 任一为 1，就报「ERRORS OCCURED」；否则报「COMPLETED SUCCESSFULLY」。

#### 4.1.4 代码实践

**实践目标**：亲手把 `sim.tcl` 的判定逻辑复述成一份「判定规则表」，确认你真的读懂了它。

**操作步骤**（纯阅读，不需要仿真器）：

1. 打开 [sim.tcl:16-28](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/sim/sim.tcl#L16-L28)，对照上面 4.1.2 的伪代码，把下面这张「判定规则表」填满：

   | transcript 中是否出现 `###ERROR###` | 是否出现 `Fatal:` | `sim.tcl` 最终结论 |
   |---|---|---|
   | 否 | 否 | （填写） |
   | 是 | 否 | （填写） |
   | 否 | 是 | （填写） |
   | 是 | 是 | （填写） |

2. 思考：假如某条 `vcom` 编译失败，transcript 里会出现什么？根据这张表，`sim.tcl` 会判成功还是失败？（提示：编译错误本身不一定带 `###ERROR###` 或 `Fatal:`——这正是这套纯关键字判定的一个**已知局限**，作者默认编译能通过才谈测试。）

**预期结果**：四种组合里只有第一种（两个关键字都不出现）判「COMPLETED SUCCESSFULLY」，其余三种都判「ERRORS OCCURED」。

**待本地验证**：若你手头有 Modelsim/Questa，可在 `sim/` 目录执行 `vsim -do sim.tcl`（或按你工具的脚本执行方式）观察末尾两行 `echo` 的 0/1 与最终结论；若没有仿真器，本实践作为源码阅读型练习即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `sim.tcl` 要在判定**之前**先 `file delete ./Transcript.transcript`？
**答案**：为了删除上一次仿真残留的日志。否则本次判定会读到旧日志里的 `###ERROR###`/`Fatal:`，把上次失败误当成本次失败（假阴性——把一次实际通过的测试判成失败）。

**练习 2**：如果把第 21 行的 `{Fatal:}` 改成 `Fatal`（去掉冒号），判定结果会更宽松还是更严格？
**答案**：更**宽松地匹配**（更容易命中失败）。`Fatal:` 要求冒号，只有仿真器真正发出的致命错误（形如 `# ** Fatal: ...`）才会匹配；去掉冒号后，任何包含 `fatal` 子串的文本（哪怕是某条注释或变量名）都会被判为失败，增加**误报**风险。

---

### 4.2 Check* 断言过程族：统一 ###ERROR### 报告约定

#### 4.2.1 概念说明

`sim.tcl` 用 `###ERROR###` 关键字来判定失败，这就倒逼 testbench 内部必须遵守一个约定：**每当发现一个「期望值 ≠ 实际值」的测试失败，就必须向 transcript 打印一行带 `###ERROR###` 前缀的文本**。

为了不让每个测试用例都手写一遍 `assert ... report "###ERROR### ..."`，testbench 在 architecture 的声明区里定义了**一族 `Check*` 过程**，按被比较的数据类型分门别类：比较 `std_logic_vector` 用 `CheckStdlv`，比较整数用 `CheckInt`，比较浮点用 `CheckReal`，比较单比特 `std_logic` 用 `CheckStdl`，比较布尔用 `CheckBoolean`。每个过程都接受「期望值、实际值、描述消息」三个参数，内部做相等性判断，不等就报告 `###ERROR###`。

这族过程之于 VHDL testbench，就好比 `self.assertEqual(期望, 实际)` 之于 Python `unittest`（见 [u2-l1](u2-l1-python-package-tests.md)）——只是这里没有框架，靠几个手写过程实现。

#### 4.2.2 核心流程

每个 `Check*` 过程的逻辑都长一个样：

```
procedure CheckXxx(expected, actual, msg) is
begin
    assert expected 与 actual 满足「相等」条件
        report "###ERROR### " & msg & " [expected: ..., got: ...]"
        severity error;
end procedure;
```

两个关键设计决策：

1. **统一前缀 `###ERROR###`**：这正是 `sim.tcl` 要找的关键字。所有逻辑失败最终都汇聚成 transcript 里的一行 `###ERROR### ...`，把「断言层」和「判定层」用一根字符串线索串了起来。
2. **统一用 `severity error`（而不是 `failure`）**：在 VHDL 四级严重度 NOTE/WARNING/ERROR/FAILURE 里，`error` 只报告、**不中止**仿真。于是即便某条断言失败，仿真仍会继续跑到 `run -all`，**所有**测试用例都能执行完，一次运行就能收集到全部失败点。如果改用 `severity failure`，第一条失败就会让仿真停住，后面的检查全被跳过（详见 4.2.5 练习）。

唯一的例外是 `CheckReal`：浮点数不能直接用 `=` 比较（累积舍入会让两个「本应相等」的实数差一个 ULP），所以它用一个极小容差 \( 10^{-12} \) 来判定「近似相等」：

\[
\text{通过} \iff \text{expected} \in (\text{actual} - 10^{-12},\ \text{actual} + 10^{-12})
\]

#### 4.2.3 源码精读

先看 testbench 顶部如何把被测包和标准库引入作用域，这是后续 `Check*` 能调用 `cl_fix_*`、能用到 `std_logic_vector` 的前提：

[en_cl_fix_pkg_tb.vhd:10-17](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L10-L17) —— 引入 `ieee.std_logic_1164`（提供 `std_logic`/`std_logic_vector`）、`ieee.numeric_std`（提供有/无符号算术）、`std.textio`（文件/行 IO，`print` 过程要用），最后 `use work.en_cl_fix_pkg.all;` 把**被测包**整体引入——从此 `cl_fix_width`、`FixFormat_t`、`Trunc_s`/`Round_s`/`Sat_s`/`None_s` 等名字在 testbench 里直接可见。

> 注意 `work.en_cl_fix_pkg` 之所以能 `use`，是因为 `sim.tcl` 已经先用 `vcom` 把 `en_cl_fix_pkg.vhd` 编译进了 `work` 库（见 4.1.3 第二段）。**编译顺序 = 依赖顺序**。

接着看五个 `Check*` 过程。比较位串的 `CheckStdlv`：

[en_cl_fix_pkg_tb.vhd:37-44](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L37-L44) —— `assert expected = actual` 比较两个 `std_logic_vector`；不等时 `report` 一段以 `###ERROR###` 开头的消息，用 `to_string` 把两个位串转成可读文本（`to_string` 见 4.3），`severity error` 只报告不中止。

比较整数的 `CheckInt`：

[en_cl_fix_pkg_tb.vhd:47-54](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L47-L54) —— 结构与 `CheckStdlv` 完全一致，只是参数类型是 `integer`，文本化用 `integer'image`。`cl_fix_width` 返回 `positive`（整数的子类型），所以它的测试都用 `CheckInt`（见 4.4）。

比较浮点的 `CheckReal`（本族里唯一「不严格相等」的成员）：

[en_cl_fix_pkg_tb.vhd:56-63](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L56-L63) —— 判定条件是 `expected < actual + 1.0e-12 and expected > actual - 1.0e-12`，即上面那个容差公式；文本化用 `real'image`。`cl_fix_to_real` 一类返回 `real` 的函数用它来比对。

再看两个同族的小兄弟，模式完全相同，只是类型不同：

[en_cl_fix_pkg_tb.vhd:65-72](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L65-L72) —— `CheckStdl` 比较单个 `std_logic`（如 `cl_fix_sign`、`cl_fix_get_msb` 的返回值），用 `std_logic'image` 文本化。

[en_cl_fix_pkg_tb.vhd:74-81](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L74-L81) —— `CheckBoolean` 比较 `boolean`（如 `cl_fix_in_range`、`cl_fix_compare` 的返回值），用 `boolean'image` 文本化。

最后是辅助的 `print` 过程：

[en_cl_fix_pkg_tb.vhd:83-88](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L83-L88) —— 用 `std.textio` 的 `write`/`writeline` 把一段文本写到 `output`（即 transcript）。`p_control` 用它打印 `*** cl_fix_width ***` 这样的分节标题，让 transcript 可读、便于定位失败发生在哪一段。

#### 4.2.4 代码实践

**实践目标**：把 `Check*` 一族的「类型 → 过程 → 文本化函数」对应关系整理成表，确认你能在写新断言时选对过程。

**操作步骤**：

1. 对照 4.2.3 的四个代码链接，填写下表：

   | 被测函数返回类型 | 应使用的 Check 过程 | 文本化用的属性 |
   |---|---|---|
   | `std_logic_vector` | `CheckStdlv` | `to_string(...)`（本地定义） |
   | `integer` / `positive` | （填写） | （填写） |
   | `real` | （填写） | （填写） |
   | `std_logic` | （填写） | （填写） |
   | `boolean` | （填写） | （填写） |

2. 在 testbench 里搜索 `cl_fix_compare` 的调用（[en_cl_fix_pkg_tb.vhd:644](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L644) 附近），确认它用的是哪个 `Check*` 过程，并解释为什么选它。

**预期结果**：`integer` → `CheckInt` / `integer'image`；`real` → `CheckReal` / `real'image`；`std_logic` → `CheckStdl` / `std_logic'image`；`boolean` → `CheckBoolean` / `boolean'image`。`cl_fix_compare` 返回 `boolean`，故用 `CheckBoolean`。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `CheckInt` 里的 `severity error` 改成 `severity failure`，整套测试的行为会发生什么变化？
**答案**：`failure` 会让 Modelsim **立即中止**仿真。于是**第一条**失败的断言之后的所有 `Check*` 都不会再执行，你只能看到第一个失败、看不到其余；同时 Modelsim 会打印一条 `Fatal:` 信息，被 `sim.tcl` 的 `foundFatal` 捕获并判失败。也就是说，从「一次跑完收集所有失败」退化成「遇到第一个失败就停」——这正是作者坚持用 `severity error` 的原因。

**练习 2**：`CheckReal` 为什么不用 `assert expected = actual`？
**答案**：`real` 是浮点数，`cl_fix_to_real` 内部的乘除会引入微小的舍入误差，两个「数学上相等」的实数在二进制层面可能差一个 ULP，直接 `=` 几乎一定判不等。用 \( 10^{-12} \) 容差比较可以吸收这点误差，同时这个容差又远小于任何定点 LSB（最小也是 \( 2^{-52} \approx 2.2\times10^{-16} \) 量级，但实际用例的 LSB 远大于此），不会把真正的错误漏判成通过。

---

### 4.3 to_string 兼容函数：跨仿真器的字符串化

#### 4.3.1 概念说明

`CheckStdlv` 在报告失败时要把 `std_logic_vector` 转成可读字符串（比如把位串 `"0011"` 打印成文本 `0011`），这个转换由 testbench 自己定义的 `to_string` 函数完成。

你可能会问：VHDL-2008 标准不是已经为 `std_logic_vector` 提供了内建的 `to_string` 吗？没错——但**不是所有仿真器都完整支持 VHDL-2008**。作者在函数正上方的注释里明确写道：这个本地实现是「为不支持 VHDL-2008 的工具（例如 Vivado 自带的 xsim）准备的等价物」。换句话说，这是一份**可移植性兜底**：让这个 testbench 即便拿到 VHDL-2008 支持不完整的工具上也能编译通过。

#### 4.3.2 核心流程

把一个 `std_logic_vector` 转成字符串，思路是**逐位转换再拼接**：

```
function to_string(a : std_logic_vector) return string is
    建一个与 a 等长、初值全为 NUL 的字符串 b
    for 每一位 a(i):
        取 std_logic'image(a(i)) 的第 2 个字符   # 例如 image('1') = "'1'"，第 2 个字符就是 '1'
        追加到 b
    return b
```

关键在于 VHDL 的 `T'attribute`——`std_logic'image(x)` 返回字符 `x` 的**词法表示**，固定是 3 个字符：单引号、字符本身、单引号。例如 `std_logic'image('1')` 返回字符串 `'1'`（三个字符：`'`、`1`、`'`），取第 2 个字符就得到干净的 `1`。对 `0`/`1`/`X`/`U`/`Z` 等所有 std_logic 值都成立。

#### 4.3.3 源码精读

[en_cl_fix_pkg_tb.vhd:26-35](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L26-L35) —— 注释直白说明用途：「为不支持 VHDL-2008 的工具（如 vivado simulator）定义 VHDL-2008 的等价物」。函数体：声明一个长度为 `a'length`、初值全 `NUL` 的字符串 `b`，用一个游标 `stri` 从 1 开始，遍历 `a` 的每一位，把 `std_logic'image(a(i))(2)`（即 image 串的第 2 个字符）写入 `b(stri)` 并递增游标，最后返回 `b`。`a'range` / `a'length` 是 VHDL 的属性，表示数组的索引范围与长度——即便 `a` 的索引不是从 0 开始也能正确遍历。

> 这正好解释了 `sim.tcl` 里 `vcom ... -2008` 与这个本地 `to_string`「看似矛盾」的关系：`sim.tcl` 这条路径走的是 Modelsim + `-2008`，理论上能直接用内建 `to_string`；但 testbench 仍然保留这个本地函数，是为了让同一份测试代码也能拿到 xsim 等工具上跑。**本地定义在就近作用域里生效**，不影响 Modelsim 下的编译。这是一份「写一次、到处编译」的工程取舍。

#### 4.3.4 代码实践

**实践目标**：手算一次 `to_string`，确认你理解 `image` 属性的返回形态。

**操作步骤**：

1. 设 `a = "0101"`（一个 4 位的 `std_logic_vector`）。
2. 依次写出循环每一步 `std_logic'image(a(i))` 的**完整返回值**，以及取 `(2)` 后得到的字符。
3. 拼出最终返回的字符串。

**预期结果**：

| i | a(i) | `std_logic'image(a(i))` 完整返回 | `(2)` 字符 |
|---|---|---|---|
| 第 1 位 | `'0'` | `'0'`（3 字符） | `0` |
| 第 2 位 | `'1'` | `'1'`（3 字符） | `1` |
| 第 3 位 | `'0'` | `'0'` | `0` |
| 第 4 位 | `'1'` | `'1'` | `1` |

最终返回字符串 `"0101"`——与输入位串的「逻辑值序列」一致。**待本地验证**：若想实测，可在 testbench 里临时加一行 `report to_string("0101");` 用 Modelsim 跑一次观察 transcript。

#### 4.3.5 小练习与答案

**练习 1**：如果某一位是 `'U'`（未初始化），`to_string` 会把它转成什么字符？
**答案**：`std_logic'image('U')` 返回 `'U'`（3 字符：`'`、`U`、`'`），取 `(2)` 得 `U`。所以最终字符串里对应位置会出现字母 `U`——这其实是个有用的副作用：testbench 里若意外出现 `U`，会在失败报告里一眼可见。

**练习 2**：为什么用 `b : string (1 to a'length)` 且初值 `(others => NUL)`，而不是直接逐步拼接？
**答案**：VHDL 的 `string` 长度在创建时就固定、之后不能改变，没有 Python 那种「边 append 边增长」的能力。所以必须**预先**按目标长度分配好定长字符串，再用游标 `stri` 逐位填入；`NUL` 初值只是占位，填完之后每一位都被覆盖，初值实际不影响结果。

---

### 4.4 p_control 进程：分节测试与包引用

#### 4.4.1 概念说明

前面三节我们准备好了「导演 `sim.tcl`」「断言机制 `Check*`」「文本化工具 `to_string`」三样零件。本节看它们如何被组装进一个真正的测试驱动进程 `p_control`。

`p_control` 是 testbench 里**唯一**的进程，它没有任何时钟、没有任何信号激励——本质上就是一段「跑一次就完事」的顺序代码：按被测函数**逐个分节**，每节先 `print("*** 函数名 ***")` 打个标题，再紧跟一组针对该函数的 `Check*` 调用。整段进程在最后用一句 `wait;` 把自己永久挂起，让 `sim.tcl` 的 `run -all` 能够自然结束。

这种「一个进程、顺序分节、断言校验」的结构，是 VHDL 纯组合/纯函数库测试的典型写法：被测对象都是函数（不是时序电路），不需要时钟，只需要把各种输入喂进去、比对输出。

#### 4.4.2 核心流程

`p_control` 的骨架是：

```
p_control : process
begin
    print("*** cl_fix_width ***");          -- 第 1 节标题
    CheckInt(期望, cl_fix_width(...), "说明");
    CheckInt(...);                           -- 该节的若干断言

    print("*** cl_fix_from_real ***");       -- 第 2 节标题
    CheckStdlv(...);
    ...

    -- ……依次覆盖 to_real、resize、add、sub、mult、abs、neg、
    --      shift、max/min_value、in_range、compare、sign/int/frac、
    --      combine、get/set_msb/lsb、sabs/sneg、addsub/saddsub ……

    wait;                                    -- 永久挂起，run -all 到此结束
end process;
```

每节的「期望值」都来自对函数语义的**人工推导**（多数情况下承接前面几讲的公式）。例如 `cl_fix_width` 节里，期望值就是套用 \( W = S + I + F \) 算出来的整数。一旦实现与公式不一致，`CheckInt` 就会向 transcript 打一行 `###ERROR###`，被 `sim.tcl` 抓住判失败——于是「位真一致性」就被这条链路自动守护了。

#### 4.4.3 源码精读

进程的开头与第一节（`cl_fix_width`），是本讲的实践落点：

[en_cl_fix_pkg_tb.vhd:95-105](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L95-L105) —— `p_control : process` 起头；`print("*** cl_fix_width ***");` 打印分节标题；接着是 7 条 `CheckInt`，覆盖 `cl_fix_width` 在「无符号/有符号 × 纯整数/纯小数/整数+小数/负整数位/负小数位」各种格式下的位宽。每条的期望值都符合 \( W = S + I + F \)：例如 `cl_fix_width((true,3,3))` 期望 `7`（\(1+3+3\)）、`cl_fix_width((true,-2,3))` 期望 `2`（\(1+(-2)+3\)），承接 [u1-l3](u1-l3-format-helpers.md)。

注意 `(false, 3, 0)` 这种写法：它是 VHDL **按位置的 record 聚合**，等价于 `FixFormat_t'(Signed => false, IntBits => 3, FracBits => 0)`。之所以能这么省略类型限定，是因为 `cl_fix_width` 的形参类型已经是 `FixFormat_t`，编译器能从上下文推断出这个聚合的类型。这正是 4.1.3 提到的 VHDL-2008 便利写法之一。

对照看一下被测函数 `cl_fix_width` 在包里的真实定义，确认 testbench 调用的是同一个东西：

[en_cl_fix_pkg.vhd:223-224](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L223-L224) —— 包头里 `cl_fix_width` 的声明：形参 `fmt : FixFormat_t`，返回 `positive`。返回类型 `positive` 是 `integer` 的子类型，所以 testbench 用 `CheckInt`（形参为 `integer`）来接它完全合法。

[en_cl_fix_pkg.vhd:1321-1329](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1321-L1329) —— 包体里 `cl_fix_width` 的实现：先用 `assert (fmt.IntBits+fmt.FracBits) > 0 ... severity failure` 守住「IntBits+FracBits ≥ 1」的硬约束（承接 [u1-l3](u1-l3-format-helpers.md) 讲过的跨语言约束差异——VHDL 在这里用 `failure`，**会**中止仿真并打印 `Fatal:`，被 `sim.tcl` 的 `foundFatal` 抓住）；然后返回 `toInteger(fmt.Signed)+fmt.IntBits+fmt.FracBits`，即 \( W = S + I + F \)。注意这里 `severity failure` 与 `Check*` 里的 `severity error` 形成对照：**被测代码内部**的不变量违反用 `failure`（宁可停也要暴露），**测试断言**的失败用 `error`（要收集全部失败）。

进程的结尾：

[en_cl_fix_pkg_tb.vhd:825-826](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L825-L826) —— 一句不带条件的 `wait;` 把进程永久挂起。`p_control` 没有时钟、没有敏感信号，跑到这一行后再也不会被唤醒，于是仿真再也没有新事件，`sim.tcl` 的 `run -all` 在此处自然结束，进入 transcript 判定阶段。这一句是「跑一次就完事」式 testbench 的标准收尾。

#### 4.4.4 代码实践（本讲主线实践）

**实践目标**：仿照 `cl_fix_width` 节的格式，给 `cl_fix_width` 新增一条针对 `(true, 4, 4)` 的断言；并说明这条断言失败时 transcript 会出现什么关键字、`sim.tcl` 会作何判定。

**操作步骤**：

1. 先**手算期望值**：`cl_fix_width((true, 4, 4))` 即 \( W = 1 + 4 + 4 = 9 \)。
2. 在 [en_cl_fix_pkg_tb.vhd:97-105](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L97-L105) 这一段的**末尾**（第 105 行那条 `CheckInt(... (true, 3, -2) ...)` 之后、第 107 行 `print("*** cl_fix_from_real ***")` 之前）仿照格式新增一行：

   ```vhdl
   CheckInt(9, cl_fix_width((true, 4, 4)), "cl_fix_width Wrong: Signed, Integer and Fractional Bits");
   ```

3. （可选，需要 Modelsim）把改动后的 testbench 重新跑一遍 `sim.tcl`，观察 transcript。

**需要观察的现象**：

- **正常情况**（实现正确）：这条新断言通过，**不**向 transcript 写任何 `###ERROR###`；`sim.tcl` 末尾 `echo $found` 与 `echo $foundFatal` 都是 `0`，最终打印 `SIMULATIONS COMPLETED SUCCESSFULLY`。
- **故意制造失败**（把期望值改成错的，例如写成 `8`）：`CheckInt` 里的 `assert 8 = 9` 不成立，于是 report 一行：

  ```
  ###ERROR### cl_fix_width Wrong: Signed, Integer and Fractional Bits [expected: 8, got: 9]
  ```

  这行里的 `###ERROR###` 会被 `sim.tcl` 第 20 行的 `regexp` 命中（`found = 1`），最终打印 `!!! ERRORS OCCURED IN SIMULATIONS !!!`。

**预期结果**：新增断言的期望值为 `9`；失败时 transcript 出现的关键字是 `###ERROR###`（由 `CheckInt` 产生），`sim.tcl` 据此判失败。

**待本地验证**：本实践的真实运行需要 Modelsim/Questa。若没有仿真器，可只做「手算期望值 + 写出断言行 + 推演失败时 transcript 文本」的源码阅读型练习，这同样能验证你对整条链路的理解。

#### 4.4.5 小练习与答案

**练习 1**：`p_control` 末尾的 `wait;` 如果漏写，会发生什么？
**答案**：进程没有 `wait` 就会无限循环——VHDL 规定一个进程必须在初始化阶段至少挂起一次，否则在 0 仿真时刻就会陷入无限循环、仿真无法推进（多数仿真器会报错或在 0 时刻反复执行该进程）。`run -all` 要么报错、要么永不结束，`sim.tcl` 的判定也就无从谈起。所以 `wait;` 不仅是「结束标志」，更是进程语法/语义上的必需。

**练习 2**：`cl_fix_width` 包体里那个 `assert ... severity failure`（[en_cl_fix_pkg.vhd:1324-1326](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1324-L1326)）若被触发，会同时被 `sim.tcl` 的哪一个 regexp 抓到？为什么？
**答案**：会被 `foundFatal`（`{Fatal:}`）抓到。因为 `severity failure` 会让 Modelsim 打印一条形如 `# ** Fatal: ...` 的信息并中止仿真，其中包含 `Fatal:`，正好被第 21 行的 `regexp -nocase {Fatal:}` 命中。这也说明 `sim.tcl` 的两个关键字是**分工**的：`###ERROR###` 抓「测试逻辑失败」（`Check*` 产生），`Fatal:` 抓「被测代码不变量违反或仿真器致命错误」（`severity failure` 或运行时错误产生）。

**练习 3**：为什么 testbench 把所有测试都放进**一个**进程 `p_control`，而不是每个被测函数开一个进程？
**答案**：因为这些测试之间是**顺序、可读、无需并发**的——一个进程里 `print` 标题 + 一组 `Check*` 的分节写法，让 transcript 的输出顺序完全可控、便于人眼定位失败。多进程并发反而会让打印交错、失败难追溯。这种「单进程顺序分节」是纯函数库 testbench 的惯用、也是够用的写法。

---

## 5. 综合实践

把本讲四个最小模块串成一条**端到端**的链路实践。目标：亲眼看清楚「一个测试失败」是如何从 VHDL 断言一路传导到 `sim.tcl` 的最终结论的。

**任务**：

1. **制造一个失败**：在 `p_control` 的 `cl_fix_width` 节里（[en_cl_fix_pkg_tb.vhd:97-105](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L97-L105)），新增一条**故意写错期望值**的断言，例如：

   ```vhdl
   CheckInt(99, cl_fix_width((true, 4, 4)), "DELIBERATE FAILURE");
   ```

   （正确期望应是 `9`，这里故意写 `99`。）

2. **画出数据流**：在纸上画出这条失败从产生到被判定的完整路径，标注每一步发生在哪个文件、哪一行：

   ```
   CheckInt(99, ...) 断言 99≠9 不成立            [en_cl_fix_pkg_tb.vhd:47-54]
        │  report "###ERROR### DELIBERATE FAILURE [expected: 99, got: 9]"
        │  severity error  → 不中止，仿真继续
        ▼
   transcript 里多一行 "###ERROR### ..."          [写入 ./Transcript.transcript]
        │
        ▼  run -all 结束后
   sim.tcl: regexp -nocase "###ERROR###" 命中      [sim.tcl:20]
        │  found = 1
        ▼
   sim.tcl: if (found==1||foundFatal==1) 成立      [sim.tcl:24-28]
        │
        ▼
   打印 "!!! ERRORS OCCURED IN SIMULATIONS !!!"
   ```

3. **回答两个问题**：
   - 为什么这条断言失败后，**它后面**的 `cl_fix_from_real`、`cl_fix_resize` 等所有分节仍然会全部执行完？（提示：`severity error` 不中止。）
   - 如果同时把这条断言的 `severity` 改成 `failure`，上述数据流会在哪一步**提前中断**？`sim.tcl` 最终又会判成功还是失败？（提示：`failure` 中止仿真 + 触发 `Fatal:`。）

4. **（可选，需要 Modelsim）**：把改动跑一遍 `vsim -do sim.tcl`，对照你画的数据流，逐一核对 transcript 里的 `###ERROR### DELIBERATE FAILURE` 行、`echo $found`/`$foundFatal` 的值、以及最终结论行。

**验收**：你能不看讲义，用自己的话把「一条 `Check*` 失败 → transcript 出现 `###ERROR###` → `sim.tcl` 的 regexp 命中 → 最终打印 ERRORS」这条链路完整复述一遍，并解释 `severity error` 与 `severity failure` 在这条链路上的不同后果。真实运行结果待本地验证（无仿真器则作为源码阅读型综合练习）。

## 6. 本讲小结

- VHDL 这一边没有 `unittest` 框架，测试由一个 28 行的 Tcl 脚本 [sim/sim.tcl](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/sim/sim.tcl) 驱动：清理 transcript → `vcom -2008` 先编译包、再编译 testbench → `vsim` + `run -all` 运行 → 退出仿真。
- 成败判定**不靠返回码，而靠扫描 transcript 文本**：用 `regexp -nocase` 找两个关键字 `###ERROR###` 与 `Fatal:`，**任一命中即失败，全不命中才算成功**（[sim.tcl:16-28](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/sim/sim.tcl#L16-L28)）。
- testbench 用一族 `Check*` 过程做断言：`CheckStdlv`/`CheckInt`/`CheckReal`/`CheckStdl`/`CheckBoolean` 分别对应位串、整数、实数、单比特、布尔；它们**统一**用 `###ERROR###` 前缀 + `severity error` 报告，把「断言层」与「判定层」用一根字符串线索串起来（[en_cl_fix_pkg_tb.vhd:37-81](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L37-L81)）。
- `severity error` 是关键设计：它**不中止**仿真，所以一次 `run -all` 能收集到**全部**失败点；`CheckReal` 另用 \( 10^{-12} \) 容差比较浮点，避开浮点严格相等的脆弱性。
- 两个关键字分工明确：`###ERROR###` 由 `Check*` 产生（测试逻辑失败），`Fatal:` 由仿真器产生（如被测代码里 `cl_fix_width` 守约束的 `severity failure`、或运行时致命错误）。
- 本地 `to_string`（[en_cl_fix_pkg_tb.vhd:26-35](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L26-L35)）是为不支持 VHDL-2008 的仿真器（如 xsim）准备的可移植兜底，靠 `std_logic'image(...)(2)` 逐位提取字符。
- 唯一的测试进程 `p_control` 采用「`print` 分节标题 → 一组 `Check*`」的顺序结构，覆盖库内几乎所有函数，末尾用一句 `wait;` 永久挂起，让 `run -all` 自然结束。

## 7. 下一步学习建议

本讲把 VHDL 测试的**外壳**讲透了，但里面 `Check*` 比对的真实函数（`cl_fix_resize`、`cl_fix_add`、`cl_fix_mult` 等）我们只把它们当「被调用的名字」，还没打开它们的算法。建议下一步：

- **横向补齐三语言测试对照**：读 [u2-l3 MATLAB 实现模型与使用方式](u2-l3-matlab-model.md)。你会看到 MATLAB 既没有 `sim.tcl` 也没有 `unittest`，目前**没有任何自动化测试**——这正好凸显本讲 VHDL 这套「脚本 + 断言 + 关键字扫描」的自动化价值，也解释了为什么跨语言位真一致主要靠 VHDL 与 Python 两边的测试来守护。
- **纵向打开核心函数**：进入 Unit 3，从 [u3-l1 数值与字符串转换函数](u3-l1-conversion-functions.md) 开始。届时你会发现，本讲 testbench 里 `cl_fix_from_real`、`cl_fix_to_real`、`cl_fix_resize` 那一长串 `CheckStdlv` 用例（[en_cl_fix_pkg_tb.vhd:107-285](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L107-L285)）正是这些函数行为的「黄金样本」，可以一边读实现一边回来对照用例。
- **想动手扩展测试**：本讲的「新增一条 `CheckInt`」实践只是入门。等你学完 Unit 3/Unit 4 的运算函数，可以回到这个 testbench，仿照 `cl_fix_add`/`cl_fix_mult` 的分节，为你自己设计的定点运算场景补充边界用例（最负值、饱和、回绕），把「位真一致性」的网织得更密。
