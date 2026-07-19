# 测试运行定义（TbRuns 数据模型）

## 1. 本讲目标

上一篇（u2-l2）我们看清了 `add_library`/`add_sources` 如何把「源文件」登记进 `Sources` 这个 dict 列表。本讲把镜头移到回归测试的另一半——**测试台到底「怎么跑」**：

- 同一个测试台要不要用不同的 generics 多跑几遍？
- 跑之前要不要先执行一个外部脚本（比如生成测试向量）？跑完要不要再执行一个（比如比对结果）？
- 这个测试台在某个仿真器上会崩溃，能不能只跳过它？

PsiSim 用一组命令把上面这些信息收集起来，最终存进另一个与 `Sources` 平行的数据模型：`TbRuns`。学完本讲你应当能够：

1. 说出 `ThisTbRun` 这个 dict 的全部字段及默认值，并能解释它和 `TbRuns` 的关系。
2. 掌握 `create_tb_run → （可选）配置命令 → add_tb_run` 的「两段式编程模型」，知道为什么配置命令必须夹在中间。
3. 会用多组 generics、前后脚本（pre/post script）和 `tb_run_skip` 灵活定义测试运行，并能推算一次 `run_tb` 究竟会触发几次仿真。

## 2. 前置知识

在读源码前，先补两个本讲会用到的 TCL 概念。

**dict（字典）。** TCL 的 dict 是一个「键→值」映射，在底层就是一个长度为偶数的扁平列表：`{k1 v1 k2 v2 ...}`。常用操作：

- `dict create k1 v1 k2 v2` —— 新建一个 dict。
- `dict set varName key value` —— 把 `varName` 里那个 dict 的某个键改为 `value`（键不存在则新增）。
- `dict get $d key` —— 取出某键的值。

dict 和 list 一样是**按值复制**的，这是理解本讲一个关键行为的基础（见 4.1）。

**过程参数 `args`。** 如果一个 `proc` 的**最后一个**形参名字叫 `args`，TCL 会把所有多余的实际参数打包成一个 list 交给它。本讲的 `tb_run_add_arguments` 就靠它接收「任意多组 generics」。要注意：只有**末位**的 `args` 才有这个魔法，夹在中间的同名参数只是普通参数（见 4.3 中 `tb_run_add_pre_script` 的坑）。

承接前面几讲：PsiSim 所有实现都在 `psi::sim` 命名空间里；配置阶段的命令只**改状态变量**，真正的执行（编译/仿真/检查）由运行阶段的命令去**消费**这些状态变量。本讲的 `create_tb_run` 一族命令属于「配置命令」，它们写出的 `TbRuns` 会在下一篇（u2-l6 `run_tb`）被消费。

## 3. 本讲源码地图

本讲几乎只围绕一个文件 `PsiSim.tcl` 展开，文档 `CommandRef.md` 提供参数说明，`README.md` 给出一个真实示例。

| 文件 | 本讲关注的内容 |
| --- | --- |
| `PsiSim.tcl` | `ThisTbRun`/`TbRuns` 状态变量声明；`create_tb_run`、五个配置命令、`add_tb_run` 的实现；以及 `run_tb` 如何消费 `TbRuns`。 |
| `CommandRef.md` | `create_tb_run`、`add_tb_run`、`tb_run_add_arguments`、`tb_run_add_pre_script`、`tb_run_add_post_script`、`tb_run_add_time_limit`、`tb_run_skip` 的参数表与「必须夹在中间」的说明。 |
| `README.md` | 第 105–135 行的 `#TB Runs` 示例，是本讲最贴近真实用法的参考。 |

数据流一图速览：

```
create_tb_run        : 在 ThisTbRun 里新建一个带默认值的 dict（半成品）
   │
   ├─ tb_run_add_arguments   ─┐
   ├─ tb_run_add_pre_script   │  只改 ThisTbRun 的某个字段（可选、可任意次序）
   ├─ tb_run_add_post_script  │
   ├─ tb_run_add_time_limit   │
   └─ tb_run_skip             ─┘
   │
add_tb_run           : 把 ThisTbRun 按值复制，追加进 TbRuns 列表（定稿）
                        ↓
run_tb (u2-l6)       : 遍历 TbRuns，逐个跑仿真
```

## 4. 核心概念与源码讲解

### 4.1 ThisTbRun 与 TbRuns 数据模型

#### 4.1.1 概念说明

`Sources` 描述「要编译什么」，`TbRuns` 描述「要仿真什么、怎么仿真」。两者结构同源：都是一个 list，每个元素是一个 dict。区别在于 dict 的字段不同：

- `Sources` 的元素描述一个**源文件**（上一篇讲过：`PATH`/`LIBRARY`/`TAG`/`LANGUAGE`/`VERSION`/`OPTIONS`）。
- `TbRuns` 的元素描述一次**测试运行**（一个测试台 + 它的 generics 组合 + 前后脚本 + 时间限制 + 跳过策略）。

这里有一个关键设计：PsiSim 用**两个**变量来管理测试运行：

- `ThisTbRun` —— **半成品/草稿**变量，存放「正在编辑中的那一个」测试运行。
- `TbRuns` —— **登记表**变量，存放「已经定稿、等待运行」的全部测试运行。

为什么要分两个？因为定义一个测试运行是**分多步**完成的（先建、再加 generics、再加脚本……），在它定稿前不能污染登记表。所以 `ThisTbRun` 充当「编辑缓冲区」，定稿时再快照进 `TbRuns`。

#### 4.1.2 核心流程

一个测试运行从无到有进入 `TbRuns` 的流程：

1. `create_tb_run` 把 `ThisTbRun` **整体重建**为一个带全套默认值的 dict。
2. 配置命令用 `dict set` 修改 `ThisTbRun` 的个别字段。
3. `add_tb_run` 执行 `lappend TbRuns $ThisTbRun`。

第 3 步依赖 TCL 的**按值复制**语义：`$ThisTbRun` 在那一刻被求值成一个字符串副本追加进列表。此后即便下一个 `create_tb_run` 把 `ThisTbRun` 重建，也不会影响已经进表的运行——它们是独立的副本。这就是「定稿」二字的实现原理。

一个推论：单个 run 触发的仿真次数等于它参数集合的大小：

\[
\text{仿真次数} = \text{len}(\text{TB\_ARGS})
\]

默认 `TB_ARGS` 是只含一个空串的 list，所以 \(\text{len}=1\)，即默认只跑一次。

#### 4.1.3 源码精读

先看两个状态变量本身——它们都只是「声明」，不赋值，初值由 `init` 或后续命令写入：

[PsiSim.tcl:20-21](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L20-L21) —— `ThisTbRun`（草稿）与 `TbRuns`（登记表）的命名空间声明。

再看 `init` 重置了哪些变量：

[PsiSim.tcl:368-373](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L368-L373) —— `init` 把 `Libraries`/`Sources`/`TbRuns`/`CompileSuppress`/`RunSuppress`/`CurrentLib` 都清零。

注意一个反直觉的点：**`init` 重置了 `TbRuns`，却没有重置 `ThisTbRun`。** 这不是遗漏，而是有意为之——`ThisTbRun` 是「一次性草稿」，它只在 `create_tb_run` 到 `add_tb_run` 之间有意义，每次 `create_tb_run` 都会把它整体重建，所以不需要 `init` 来清。

`create_tb_run` 把这个草稿用 `dict create` 一次性铺满默认值：

[PsiSim.tcl:631-642](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L631-L642) —— 用 `dict set` 逐字段写入默认值，得到下表所示的「空 run」。

`ThisTbRun` 的全部 11 个字段及默认值：

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `TB_NAME` | （你传入的测试台名） | 测试台实体名 |
| `TB_LIB` | `CurrentLib`（或显式传入的库） | 测试台所在的库 |
| `TB_ARGS` | `[list ""]`（含一个空串的 list） | 传给仿真器的参数字符串集合，每组跑一次 |
| `PRESCRIPT_CMD` | `""` | 前置脚本命令，空串表示不执行 |
| `PRESCRIPT_PATH` | `"."` | 前置脚本的工作目录 |
| `PRESCRIPT_ARGS` | `""` | 前置脚本的参数 |
| `POSTSCRIPT_CMD` | `""` | 后置脚本命令，空串表示不执行 |
| `POSTSCRIPT_PATH` | `"."` | 后置脚本的工作目录 |
| `POSTSCRIPT_ARGS` | `""` | 后置脚本的参数 |
| `TIME_LIMIT` | `"None"` | 仿真时间上限，`"None"` 表示跑到底 |
| `SKIP` | `"None"` | 对哪些仿真器跳过本 run |

最后看「定稿」动作：

[PsiSim.tcl:706-710](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L706-L710) —— `add_tb_run` 把当前 `ThisTbRun` 按值追加进 `TbRuns`。注意它只读 `$ThisTbRun`、不读任何参数，所以它「提交」的永远是「最近一次 `create_tb_run` 之后编辑出的那个 run」。

#### 4.1.4 代码实践

**目标：** 不依赖任何仿真器，直接在普通 TCL 解释器里把 `ThisTbRun`/`TbRuns` 的演化「看」出来，验证「按值复制」与「`create_tb_run` 整体重建」这两个论断。

**操作步骤：** PsiSim.tcl 顶层只定义命名空间与 proc，source 时不会执行任何仿真器命令；而 `create_tb_run`/`tb_run_add_arguments`/`add_tb_run` 只操作 dict，同样不碰仿真器。所以可以用普通 `tclsh` 直接自省（不要调用 `init`，因为 `init` 会触发 `sal_init_simulator` 去跑 `vcom`）。

```tcl
# 在项目根目录执行（示例代码，需本地有 tclsh）
tclsh
% source PsiSim.tcl
% namespace import psi::sim::*
% create_tb_run "demo_tb"
% set psi::sim::ThisTbRun        ;# 看到 11 个字段的默认值
% add_tb_run
% set psi::sim::TbRuns           ;# 看到列表里多了刚才那个 run
% create_tb_run "demo_tb"        ;# 再次 create：ThisTbRun 被整体重建
% set psi::sim::ThisTbRun        ;# TB_ARGS 等字段又回到默认
% set psi::sim::TbRuns           ;# 关键观察：第一个 run 仍然在，没被影响
```

**需要观察的现象：**

1. 第一次 `set psi::sim::ThisTbRun` 输出一个含 11 个键值对的 dict，`TB_ARGS` 是单个空串。
2. 第二个 `create_tb_run` 之后，`ThisTbRun` 重新变成默认值（证明它是被整体覆盖的草稿）。
3. 最后的 `TbRuns` 里**第一个 run 不受影响**（证明 `add_tb_run` 是按值复制的快照）。

**预期结果：** `TbRuns` 在第二次 `create_tb_run` 后仍然只含第一个 run 的完整副本。`ThisTbRun` 的字符串形式是一串扁平的 `键 值 键 值 …`。

> 本实践未实际运行，输出形式以源码逻辑推断为准，**待本地验证**（尤其 dict 各值的精确字符串排版，不同 TCL 版本可能略有差异）。

#### 4.1.5 小练习与答案

**练习 1.** 为什么 `init` 重置了 `TbRuns` 却不重置 `ThisTbRun`？

> **答：** `TbRuns` 是跨整个项目的登记表，每次新回归必须清空；`ThisTbRun` 是一次性草稿，每次 `create_tb_run` 都会整体重建它，所以不需要 `init` 清。

**练习 2.** 如果 `TB_ARGS` 的默认值设计成空 list `[]` 而不是 `[list ""]`（含一个空串），`run_tb` 默认会少跑还是多跑？

> **答：** 会**一次都不跑**。因为 `run_tb` 用 `foreach tbArgs $allArgLists` 遍历 `TB_ARGS`，空 list 会让循环体执行 0 次。设计成 `[list ""]` 正是为了保证「不调 `tb_run_add_arguments` 也至少跑一次」。

---

### 4.2 两段式编程模型：create → 配置 → add

#### 4.2.1 概念说明

PsiSim 定义一个测试运行用的是**两段式**（create/configure/add）写法，而不是一条带很多参数的大命令：

```tcl
create_tb_run "my_tb"           ;# 第一段：开一个新 run，写入默认值
tb_run_add_arguments "-gA=1"    ;# 中间段：按需改字段（可选）
add_tb_run                      ;# 第二段：定稿，提交进 TbRuns
```

这种写法的好处是**可组合、可省略**：你不需要的配置（比如 pre/post script）干脆不写即可，因为 `create_tb_run` 已经把所有字段都给了合理默认值。代价是必须遵守一个契约——**配置命令只能夹在 `create_tb_run` 与 `add_tb_run` 之间**。`CommandRef.md` 里每条配置命令都重复了这句话，例如：

> The command must be called between the `create_tb_run` and the `add_tb_run` commands.

#### 4.2.2 核心流程

两段式的执行逻辑：

1. **开 run** —— `create_tb_run <tb> [<lib>]` 重建 `ThisTbRun`，默认 `TB_LIB` 取 `CurrentLib`（即最近一次 `add_library` 建的库），这和 `add_sources` 缺省 `-lib` 的逻辑完全一致。
2. **改字段** —— 任意条配置命令，每条只 `dict set ThisTbRun <字段> <值>`，彼此独立、次序不限。
3. **定稿** —— `add_tb_run` 把 `ThisTbRun` 快照进 `TbRuns`。

契约的「强制力」其实来自数据流而非显式校验：

- 配置命令体内都是 `variable ThisTbRun; dict set ThisTbRun …`。如果在没有 `create_tb_run` 的情况下调用，`$ThisTbRun` 还没被赋值，TCL 会抛 `can't read "ThisTbRun"`——这是隐式拦截。
- 如果在 `add_tb_run` **之后**再调配置命令，它会改到「上一个还没被覆盖的草稿」，直到下一次 `create_tb_run` 才被冲掉；这不会报错，但改动不会进表——属于静默错误，要靠纪律避免。

#### 4.2.3 源码精读

先看第一段 `create_tb_run` 的库选择 + 重建逻辑：

[PsiSim.tcl:622-643](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L622-L643) —— `library` 形参默认 `"None"`；为 `None` 时回落到 `CurrentLib`；随后 `dict create` 重建草稿。

再看第二段 `add_tb_run`：

[PsiSim.tcl:706-710](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L706-L710) —— 一行 `lappend TbRuns $ThisTbRun; list`（结尾的 `list` 是 TCL 里常见的「吞掉返回值」技巧）。

中间段的命令长什么样？以 `tb_run_add_time_limit` 为例，它最短，最能体现「只改一个字段」的模式：

[PsiSim.tcl:689-692](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L689-L692) —— 把 `TIME_LIMIT` 字段设为传入的 `limit`。

真实用例可对照 `README.md` 的 TB Runs 段，那里用 `create_tb_run` / `tb_run_add_arguments` / `add_tb_run` 三连定义了五个测试运行：

[README.md:105-135](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L105-L135) —— 注意最后那个 `psi_common_logic_pkg_tb`：它只用了 `create_tb_run` + `add_tb_run` 两行，中间什么配置都没加——这正是两段式「按需省略」的典型写法。

#### 4.2.4 代码实践

**目标：** 通过手写一段最简配置并预测出错场景，内化「夹在中间」契约。

**操作步骤：** 阅读下面三段示例代码，回答问题，然后（可选）用 4.1.4 的 tclsh 方法验证你的判断。

```tcl
# 示例代码：两段式定义两个 run
create_tb_run "tb_a"
add_tb_run                      ;# run #1：全部默认值

create_tb_run "tb_b"
tb_run_add_arguments "-gX=1" "-gX=2"
add_tb_run                      ;# run #2：带两组 generics
```

**需要观察/回答的现象：**

1. 如果把第一段的 `add_tb_run` 删掉，直接进入第二个 `create_tb_run "tb_b"`，`TbRuns` 里最终有几个 run？为什么？
2. 如果在脚本最末尾（两个 `add_tb_run` 之后）再写一行 `tb_run_add_arguments "-gY=9"`，会改到哪个 run？这个改动会进 `TbRuns` 吗？

**预期结果：**

1. 删掉第一个 `add_tb_run` 后，`TbRuns` 最终**只有 1 个 run**（即 `tb_b`）。因为第一个 `create_tb_run "tb_a"` 写入的草稿从未被 `add_tb_run` 提交，紧接着的 `create_tb_run "tb_b"` 把草稿整体覆盖，`tb_a` 的信息就丢了——而且不会报错。
2. 末尾那行会改到「当前草稿」（最后一次 `create_tb_run "tb_b"` 留下的那份），但因为后面没有再 `add_tb_run`，**这个改动不会进入 `TbRuns`**，属于静默无效操作。

> 结论：两段式契约靠纪律维护；漏写 `add_tb_run` 或在 `add_tb_run` 之后再改字段，都会静默丢失配置。**待本地验证**：可用 tclsh 打印 `set psi::sim::TbRuns` 确认 run 个数与字段。

#### 4.2.5 小练习与答案

**练习 1.** `create_tb_run "tb_x"` 不传第二个参数时，`TB_LIB` 会被设成什么？

> **答：** 设成 `CurrentLib`，即最近一次 `add_library` 创建的库。如果从头到尾没调过 `add_library`，`CurrentLib` 是 `init` 设的哨兵值 `"NoCurrentLibrary"`。

**练习 2.** 能不能给同一个测试台定义两个 run（比如一个带 pre_script、一个带 post_script）？

> **答：** 能。`CommandRef.md` 的 `add_tb_run` 一节明确写道：「it is possible to add multiple runs for the same testbench」。只要重复「`create_tb_run` → 配置 → `add_tb_run`」即可，每次 `add_tb_run` 都往 `TbRuns` 里追加一个独立副本。

---

### 4.3 多组 generics、脚本钩子与 skip

#### 4.3.1 概念说明

这是 `TbRuns` 模型最实用的三种能力，分别对应三个字段：

- **多组 generics** —— `TB_ARGS`。一个测试台常常需要用多组参数各跑一次（不同时钟比例、不同 FIFO 深度……）。PsiSim 让你把每组参数写成一条字符串，丢给 `tb_run_add_arguments`，它会自动「每组跑一次」。
- **脚本钩子** —— `PRESCRIPT_*` / `POSTSCRIPT_*`。在所有参数组跑之前/之后各执行一次外部命令（如生成测试数据、清理临时文件、比对日志）。
- **跳过** —— `SKIP`。对全部或指定仿真器跳过本 run，用来规避某个仿真器的已知 bug。

#### 4.3.2 核心流程

三者最终都由 `run_tb`（下一篇 u2-l6 详讲）消费，但本讲先点清它们如何被读出来：

1. `tb_run_add_arguments` 收集任意多组参数 → 写入 `TB_ARGS`（一个 list）。
2. `run_tb` 遍历 `TbRuns`，对每个 run：
   - 先看 `SKIP`：命中当前仿真器或 `"all"` 就 `continue`，整个 run 跳过。
   - 若 `PRESCRIPT_CMD` 非空，**执行一次**前置脚本（不论有几组参数）。
   - 对 `TB_ARGS` 里**每一组**参数各调一次 `sal_run_tb`。
   - 若 `POSTSCRIPT_CMD` 非空，**执行一次**后置脚本。

关键时序：**前后脚本把所有参数组「包」在一起，只各跑一次**，而不是每组参数都跑一遍脚本。这正对应 `CommandRef.md` 里 `create_tb_run` 的说明：「the scripts are ran only once before/after all simulations with different arguments」。

#### 4.3.3 源码精读

先看 `tb_run_add_arguments` 如何用末位 `args` 收集任意多组参数：

[PsiSim.tcl:679-682](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L679-L682) —— `args` 把所有实际参数打包成 list，整体赋给 `TB_ARGS`。

再看 `run_tb` 如何消费 `TB_ARGS`——这是「每组跑一次」的落点：

[PsiSim.tcl:822-827](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L822-L827) —— `foreach tbArgs $allArgLists { sal_run_tb … }`。默认 `allArgLists=[list ""]` 时循环 1 次，符合 4.1 的推算。

前后脚本的执行位置（注意它们在 `tbArgs` 循环的**外面**）：

[PsiSim.tcl:812-834](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L812-L834) —— pre-script 在循环前执行一次，post-script 在循环后执行一次，中间夹着「按参数组重复仿真」。

跳过逻辑有两处。配置端：

[PsiSim.tcl:698-701](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L698-L701) —— `tb_run_skip` 把传入的 `simulator` 写进 `SKIP`，**缺省值是 `"all"`**（即「不传参 = 对所有仿真器都跳过」，这是个容易踩的坑）。

消费端：

[PsiSim.tcl:807-810](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L807-L810) —— `if {([lsearch $skip $Simulator] != -1) || ($skip == "all")} { … continue }`。

这里有个值得细看的技巧与一个**大小写陷阱**：

- `lsearch $skip $Simulator` 把 `SKIP` 字符串当作一个 list 来搜索当前 `Simulator`。于是单值 `"Vivado"` 能命中，多值字符串 `"Vivado GHDL"` 也能分别命中（因为字符串被当成了两元素 list）。这就是 `CommandRef.md` 说「skip 多个仿真器用 `tb_run_skip "Vivado GHDL"`」的实现依据。
- 但 `lsearch` **默认大小写敏感**，而 `Simulator` 的内部取值是 `"Modelsim"`/`"GHDL"`/`"Vivado"`（全大写的 GHDL）。`CommandRef.md` 第 303 行的示例 `tb_run_skip "Vivado Ghdl"` 里写的是 `Ghdl`（首字母大写），与代码里的 `GHDL` 不一致，**待本地验证**：在该写法下，GHDL 很可能因为大小写不匹配而**不会被跳过**。为安全起见，请一律使用 `"Modelsim"`、`"GHDL"`、`"Vivado"`、`"all"` 这些精确大小写。

附带一提 `tb_run_add_pre_script` 的形参签名：

[PsiSim.tcl:651-658](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L651-L658) —— 签名是 `{cmd args path}`（均带默认值）。这里的 `args` 处在**中间位置**，不是末位，所以它**没有**「收集剩余参数」的魔法，只是一个普通参数（用来放脚本参数字符串）。`post_script` 与之完全对称。所以调用时要把脚本参数作为**单个字符串**传入，例如 `tb_run_add_pre_script make "clean all" ./build`。

#### 4.3.4 代码实践

**目标：** 给定一个具体的 `TbRuns`，推算 `run_tb -all` 在 Modelsim 下会调用几次 `sal_run_tb`、各 run 的脚本各跑几次。这是下一篇 u2-l6 的热身。

**操作步骤：** 阅读下面示例代码（假设 `Simulator="Modelsim"`），填出表格。

```tcl
# 示例代码
create_tb_run "tb_a"
tb_run_add_arguments "-gR=1" "-gR=2" "-gR=3"
tb_run_add_pre_script make "gen_vectors"
add_tb_run                      ;# run A：3 组参数 + pre_script

create_tb_run "tb_b"
tb_run_skip "GHDL"
add_tb_run                      ;# run B：默认 1 组参数，跳过 GHDL

create_tb_run "tb_c"
tb_run_skip                     ;# 不传参 → SKIP="all"
add_tb_run                      ;# run C：全部跳过

create_tb_run "tb_d"
tb_run_add_arguments "-gD=1" "-gD=2"
tb_run_add_post_script python "compare.py"
add_tb_run                      ;# run D：2 组参数 + post_script
```

**需要观察/填写：**

| run | 是否被跳过 (Modelsim) | `sal_run_tb` 次数 | pre_script 次数 | post_script 次数 |
| --- | --- | --- | --- | --- |
| A | ? | ? | ? | 0 |
| B | ? | ? | 0 | 0 |
| C | ? | ? | 0 | 0 |
| D | ? | ? | 0 | ? |

**预期结果：**

| run | 是否被跳过 | `sal_run_tb` 次数 | pre | post |
| --- | --- | --- | --- | --- |
| A | 否（`SKIP="None"`） | **3**（3 组参数） | **1** | 0 |
| B | 否（`SKIP="GHDL"`，当前是 Modelsim，不命中） | **1**（默认 1 组） | 0 | 0 |
| C | **是**（`SKIP="all"`） | **0** | 0 | 0 |
| D | 否 | **2**（2 组参数） | 0 | **1** |

合计 `sal_run_tb` 调用 \(3+1+0+2=6\) 次。关键点：run A 的 pre_script 只跑 **1** 次而非 3 次；run C 因为 `SKIP="all"` 被整体跳过、连脚本都不跑。

> 结论与上述推算一致；若改在 GHDL 下跑，run B 会被跳过（`SKIP="GHDL"` 命中），合计变为 5 次。**待本地验证**：可在 tclsh 里手工构造这些 dict 并模拟 `run_tb` 的判断逻辑，或直接在仿真器里加日志确认。

#### 4.3.5 小练习与答案

**练习 1.** `tb_run_skip`（不带任何参数）的效果是什么？为什么这是个「危险」的默认值？

> **答：** 形参 `simulator` 默认 `"all"`，所以不带参数调用会把本 run 对**所有**仿真器都跳过——等于「永远不跑」。如果只是想暂时禁用某个 run，用 `tb_run_skip all` 是可以的；但如果本意是跳过某个特定仿真器却忘了写参数，就会静默地把这个 run 完全关掉。

**练习 2.** 一个 run 有 4 组 generics，并配了 pre_script 和 post_script。pre/post script 一共跑几次？

> **答：** 一共 **2 次**——pre_script 1 次、post_script 1 次。脚本包在所有参数组的外面，与参数组数量无关；4 组 generics 只决定 `sal_run_tb` 跑 4 次。

**练习 3.** 想跳过 Vivado 和 GHDL、但保留 Modelsim，该怎么写？要注意什么？

> **答：** 写 `tb_run_skip "Vivado GHDL"`（一个含两个单词的字符串，`lsearch` 会把它当两元素 list 分别匹配）。**注意大小写**：必须用 `"GHDL"` 而不是 `"Ghdl"`，因为 `lsearch` 默认大小写敏感，而内部 `Simulator` 取值是全大写的 `"GHDL"`。

---

## 5. 综合实践

把本讲三块内容串起来，完成下面这个贴近真实工程的配置任务（属于「源码阅读 + 脚本编写型」实践，不需要真正跑仿真器）。

**任务背景：** 你有一个测试台 `my_fifo_tb`，需要：

- **run #1**：跑前先用 `make gen_stim` 生成激励（工作目录 `./stim`），不传额外 generics（用默认值）。
- **run #2**：用 `tb_run_add_arguments` 传入三组不同的 generics，观察 FIFO 在不同深度下的行为。
- **run #3**：这个 run 在 Vivado 仿真器上会崩溃，需要用 `tb_run_skip` 跳过 Vivado（其他仿真器照常跑）。

**操作步骤：**

1. 假设测试台所在库 `work` 已由 `add_library work` 建立（`CurrentLib` 已指向 `work`）。
2. 在 `config.tcl` 里写出这三个 run 的定义（参照 `README.md` 第 105–135 行的三段式风格）。
3. 为每个 run 在脑中（或用 4.1.4 的 tclsh 自省法）画出 `ThisTbRun` 定稿时的关键字段值。
4. 推算：在 Modelsim 下 `run_tb -all` 这三个 run 合计会触发几次 `sal_run_tb`？在 Vivado 下呢？

**参考写法（示例代码）：**

```tcl
# run #1：带 pre_script，使用默认 generics
create_tb_run "my_fifo_tb"
tb_run_add_pre_script make "gen_stim" ./stim
add_tb_run

# run #2：三组不同 generics
create_tb_run "my_fifo_tb"
tb_run_add_arguments \
    "-gDepth_g=32"  \
    "-gDepth_g=128" \
    "-gDepth_g=512"
add_tb_run

# run #3：跳过 Vivado，其他仿真器照跑
create_tb_run "my_fifo_tb"
tb_run_skip "Vivado"
add_tb_run
```

**预期结果与自检：**

- run #1：`PRESCRIPT_CMD="make"`、`PRESCRIPT_ARGS="gen_stim"`、`PRESCRIPT_PATH=…/stim`（被 `file normalize` 规范化）、`TB_ARGS` 为默认单元素 list。
- run #2：`TB_ARGS = {"-gDepth_g=32" "-gDepth_g=128" "-gDepth_g=512"}`，无脚本。
- run #3：`SKIP="Vivado"`，其他字段默认。
- 仿真次数：Modelsim 下 run#1 跑 1 次、run#2 跑 3 次、run#3 跑 1 次（`Vivado` 不命中 Modelsim，不跳过）→ 合计 **5 次**；Vivado 下 run#3 被跳过 → 合计 **4 次**。

> 写完后，建议用 4.1.4 的 tclsh 方法 `source PsiSim.tcl`、`namespace import psi::sim::*`、依次执行上面三段，再 `set psi::sim::TbRuns` 核对字段是否符合预期。**待本地验证**（精确字符串排版以本地 TCL 输出为准）。

## 6. 本讲小结

- `TbRuns` 与 `Sources` 同构：都是一个 list，每个元素是一个 dict；`TbRuns` 描述「测试运行」，字段有 `TB_NAME`/`TB_LIB`/`TB_ARGS`/`PRESCRIPT_*`/`POSTSCRIPT_*`/`TIME_LIMIT`/`SKIP` 共 11 个。
- `ThisTbRun` 是「草稿」变量，`TbRuns` 是「登记表」；`add_tb_run` 靠 TCL 的按值复制把草稿快照进登记表，故后续 `create_tb_run` 不会影响已入库的 run。
- 定义一个 run 用两段式：`create_tb_run`（重建草稿、写满默认值）→ 可选配置命令 → `add_tb_run`（定稿）。配置命令必须夹在中间，这条契约靠数据流隐式维护、不靠显式校验。
- `TB_ARGS` 默认是 `[list ""]`，所以不调 `tb_run_add_arguments` 也至少跑一次；调用后「每组参数跑一次」，仿真次数 \(=\text{len}(\text{TB\_ARGS})\)。
- 前后脚本把所有参数组包在一起，**只各跑一次**；`tb_run_skip` 用 `lsearch` 匹配当前 `Simulator`，缺省值是 `"all"`（小心静默全跳过），且大小写敏感——一律用 `"GHDL"`/`"Vivado"`/`"Modelsim"`/`"all"`。

## 7. 下一步学习建议

本讲只讲了 `TbRuns` 是怎么**填**出来的，还没讲它是怎么**被花掉**的。下一篇 **u2-l6《仿真运行与脚本钩子（run_tb）》** 会逐行拆解 `run_tb` 的过滤（`-all`/`-lib`/`-name`/`-contains`）、`SKIP` 判断、pre/post 脚本执行时机和对每组 `tb_args` 调用 `sal_run_tb` 的循环——正好把本讲 4.3 的推算在源码里坐实。

之后可以横向对比另一条消费 `TbRuns` 的路径——单元 3 的 **u3-l5《交互调试（launch_tb / 波形）》**：它也读 `TbRuns`，但只取 `-argidx` 指定的那一组参数、且不跑前后脚本，专用于交互调试。

阅读源码时建议带着本讲的字段表去对照 `run_tb`（PsiSim.tcl 第 789–836 行）里每一处 `dict get $run <字段>`，你会看到 11 个字段如何各司其职。
