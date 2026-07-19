# 嵌套配置与多库项目实践

## 1. 本讲目标

本讲是单元 4（扩展、实践与架构取舍）的第一篇，把前面单元 2 学到的「数据模型」和「过滤逻辑」组装成一个**可维护的真实工程结构**。读完本讲，你应当能够：

- 用 `config.tcl` / `run.tcl` 的两文件拆分实现**库级**与**项目级**的嵌套回归——让一个库既能独立跑自己的回归，又能被上层项目「汇总」进来。
- 用多次 `add_library` 构建**多库工程**，并理解 `-tag` 这一与库正交的分组维度如何支撑「选择性编译」。
- 用 `-contains` 做开发期的**局部迭代**——只重编译、只重跑改动相关的那一部分，把全量回归的等待时间压缩到最小。

本讲不再重复 `Sources` / `TbRuns` 字典的字段定义（见 u2-l2、u2-l3），而是聚焦「这些数据如何在多个文件之间累积、如何被 `-lib` / `-tag` / `-contains` 三个过滤维度切片消费」。

## 2. 前置知识

本讲默认你已经掌握以下概念（若陌生，请先读对应讲义）：

- **两文件工作流与黄金七步**（u1-l3）：`source PsiSim.tcl → namespace import → init → source config.tcl → compile_files → run_tb → run_check_errors`。关键铁律：`config.tcl` 不调 `init`，`init` 在每份被执行的 `run.tcl` 里只出现一次。
- **Sources 数据模型**（u2-l2）：`Sources` 是 list-of-dict，每个 dict 含 `PATH/LIBRARY/TAG/LANGUAGE/VERSION/OPTIONS`；`add_library` 既追加库名到 `Libraries`，又把「当前默认库」游标 `CurrentLib` 指过去；`init` 给 `CurrentLib` 设哨兵初值 `"NoCurrentLibrary"`。
- **TbRuns 数据模型**（u2-l3）：`TbRuns` 是 list-of-dict；`ThisTbRun` 是「草稿」，`add_tb_run` 把草稿快照进登记表；`TB_LIB` 字段缺省取 `CurrentLib`。
- **compile 的哨兵过滤**（u2-l5）：`compile` 用 `"All-Libraries"/"All-Tags"/"All-regex"` 三个哨兵表示「该维度不过滤」，三个 `continue` 串成「库 ∧ 标签 ∧ 路径包含」的 AND 过滤。
- **run_tb 的过滤**（u2-l6）：`run_tb` 的 `-lib/-name/-contains` 三维度同样用哨兵 + `continue`；`-all` 同时重置库与名字两维。

一个需要提前点破的核心事实：PsiSim 的全部状态（`Libraries`/`Sources`/`TbRuns`/`CurrentLib`/两个 `Suppress` 串）都是 **namespace 变量**，它们的生命周期跨越任意多次 `source` 调用。嵌套、多库、局部迭代之所以可行，根因就是这个「跨文件持久化的可变状态表」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `PsiSim.tcl` | 全部实现。本讲聚焦 `init`（状态清零）、`add_library`（多库游标）、`add_sources`（`-tag` 写入字典）、`compile`（三维权切片）、`compile_files`（包装）、`run_tb`（运行期过滤）这几个 proc。 |
| `README.md` | 「Usage」一节用文字定义了嵌套模型；附带的 `config.tcl` / `run.tcl` 示例是本讲实践的直接蓝本。 |
| `CommandRef.md` | 给出 `compile_files` / `run_tb` / `clean_libraries` 各开关的官方语义，含一条关于 `-clean` 的重要告警。 |

## 4. 核心概念与源码讲解

### 4.1 库级与项目级嵌套

#### 4.1.1 概念说明

「嵌套」要解决的问题很朴素：一个 FPGA 工程通常由多个库组成（例如公共库 `psi_common`、测试辅助库 `psi_tb`、项目业务库）。你希望：

1. **单独开发某个库时**，只跑这个库自己的回归，秒级反馈。
2. **集成到项目时**，把所有库的测试一次性汇总跑一遍，确保没有互相破坏。

PsiSim 的解法是把「描述」和「执行」拆成两个文件，让描述文件（`config.tcl`）成为**可被复用的纯数据声明**：

- 每个库有自己的 `config.tcl`（登记自己的源文件与测试运行）和 `run.tcl`（`init` + `source` 自己的 `config.tcl` + 编译 + 跑）。
- 项目层的 `run.tcl` **不再重复声明**各库的文件，而是 `init` 一次后，依次 `source` 各库的 `config.tcl`，让它们的声明**累积进同一张状态表**，最后统一编译、统一跑。

README 的「Usage」一节正是这样描述的：

[README.md:42-45](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L42-L45) —— 官方对嵌套的文字定义：库有独立的 `config.tcl`/`run.tcl` 可单跑；项目 `run.tcl` 通过调用库的 `config.tcl` 把库的全部测试纳入项目回归。

这个模式能成立，靠的是两条性质：**状态跨 `source` 持久**，且 **`config.tcl` 只改状态、不含 `init` 与执行命令**（u1-l3 已建立）。

#### 4.1.2 核心流程

库级 `run.tcl`（以 `psi_common` 为例）走完整的黄金七步：

```
source <path>/PsiSim.tcl
namespace import psi::sim::*
init                          ;# 选仿真器 + 状态清零（仅此一次）
source ./config.tcl           ;# 登记本库的库/源/测试
compile_files -all -clean
run_tb -all
run_check_errors "###ERROR###"
```

项目级 `run.tcl` 把「`init`」保留一次，把「`source` 本库 config」换成「依次 `source` 各库 config」：

```
source <path>/PsiSim.tcl
namespace import psi::sim::*
init                                    ;# 全工程仅此一次清零
source ../psi_common/config.tcl         ;# 累积 psi_common 的声明
source ../psi_tb/config.tcl             ;# 继续累积 psi_tb 的声明
compile_files -all -clean               ;# 编译全部累积的 Sources
run_tb -all                             ;# 跑全部累积的 TbRuns
run_check_errors "###ERROR###"
```

状态累积过程（`init` 之后）：

| 时机 | `Libraries` | `CurrentLib` | `Sources` | `TbRuns` |
|------|-------------|--------------|-----------|----------|
| `init` 之后 | `{}` | `"NoCurrentLibrary"` | `{}` | `{}` |
| `source` psi_common/config 之后 | `{psi_common}` | `psi_common` | psi_common 的文件 | psi_common 的 run |
| 再 `source` psi_tb/config 之后 | `{psi_common psi_tb}` | `psi_tb` | 两库文件之和 | 两库 run 之和 |

最终 `compile_files -all` 与 `run_tb -all` 消费的是**合并后**的整张表。整工程触发的仿真总次数为：

\[
N_{\text{sim}} \;=\; \sum_{r \,\in\, \text{TbRuns}} \bigl|\,\text{TB\_ARGS}(r)\,\bigr|
\]

即「所有被 `source` 进来的库的 run、各自泛型组数」之总和。

#### 4.1.3 源码精读

**为什么 `init` 必须在 `source` 任何 `config.tcl` 之前、且全工程只一次？** 因为 `init` 负责把状态表清零：

[PsiSim.tcl:368-377](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L368-L377) —— `init` 显式重置 `Libraries/Sources/TbRuns/CompileSuppress/RunSuppress/CurrentLib` 六个数据模型变量。注意 `ThisTbRun` 不在此列（它是草稿，由 `create_tb_run` 整体重建，见 u2-l3）。

若项目 `run.tcl` 忘了 `init`，上一轮回归残留的 `Sources`/`TbRuns` 会和新声明叠在一起，导致重复编译、重复跑甚至跑出已删除的旧测试。若每个库的 `config.tcl` 自己调 `init`，则后 `source` 的库会**清掉**前面库的声明，嵌套直接失效。这就是「`config.tcl` 不调 `init`、`init` 每份被执行的 `run.tcl` 恰好一次」铁律的根源。

**累积是如何发生的？** `add_library` 是纯追加：

[PsiSim.tcl:384-388](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L384-L388) —— `lappend Libraries $lib` 把新库追加到列表末尾，同时 `variable CurrentLib $lib` 把游标拨过去。因为 `Libraries` 是 namespace 变量，连续两次 `source` 各自调 `add_library`，结果就是两个库都在列表里。

同理，`add_sources` 把每个文件做成 dict 后 `lappend Sources $ThisSrc`（u2-l2 已精读），`add_tb_run` 把草稿 `lappend TbRuns`（u2-l3 已精读）。三者都是「只追加、不覆盖」，所以多次 `source` 天然叠加。

**README 示例的另一面：依赖「内联」而非「source」。** 顺带留意 README 自带的 `config.tcl` 示例其实用的是另一种模式：

[README.md:71-80](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L71-L80) —— 一个 `psi_common` 项目的 `config.tcl`，只 `add_library psi_common` 一次，却把 `psi_tb/hdl/` 下的依赖文件**直接加进 psi_common 库**并打上 `-tag lib`。

这是「把依赖库的源文件内联编译进当前库」的写法，区别于「`source` 依赖库的 `config.tcl`」。两者都合法：前者简单、依赖少；后者真正复用依赖库自己的测试声明，是本讲主推的可扩展结构。理解这一区别，你才能在「单库小工程」与「多库大工程」之间做取舍。

#### 4.1.4 代码实践

**实践目标**：亲手验证「多次 `source` 会让状态累积」，而不是覆盖。

**操作步骤**（源码阅读型实践，无需仿真器）：

1. 在 `PsiSim.tcl` 中找到 `init`（L351）、`add_library`（L384）、`add_sources`（L435）、`create_tb_run`/`add_tb_run`（L622/L706）这五个 proc。
2. 假设有两份极简 `config.tcl`：
   - `a_config.tcl`：`add_library libA`；`add_sources "../a" "a.vhd" -tag src`；`create_tb_run "a_tb"`；`add_tb_run`。
   - `b_config.tcl`：`add_library libB`；`add_sources "../b" "b.vhd" -tag src`；`create_tb_run "b_tb"`；`add_tb_run`。
3. 在脑中（或用任意 TCL 解释器）执行：`init` → `source a_config.tcl` → `source b_config.tcl`。
4. 逐步填出上文 4.1.2 的状态表。

**需要观察的现象**：`CurrentLib` 在第二次 `source` 后变成了 `libB`；`Libraries` 长度为 2；`Sources` 含两个文件；`TbRuns` 含两个 run。

**预期结果**：`run_tb -all` 会依次跑 `libA.a_tb` 与 `libB.b_tb`；`compile_files -all` 会编译两个文件。若把 `init` 错放在两个 `source` 之间，则 `libA` 的全部声明丢失——这正是铁律的现实代价。

> 说明：本实践为「源码阅读 + 心智推演」型，未假定你已安装 Modelsim/GHDL。若你有独立 TCL 解释器，可去掉 `sal_init_simulator` 依赖后实际跑通；否则标注「待本地验证」即可。

#### 4.1.5 小练习与答案

**练习 1**：如果 `b_config.tcl` 忘了写 `add_library libB`，直接 `add_sources "../b" "b.vhd"`，会发生什么？

**参考答案**：`add_sources` 缺省 `-lib` 时取 `CurrentLib`（u2-l2）。由于上一步 `a_config.tcl` 把 `CurrentLib` 拨到了 `libA`，所以 `b.vhd` 会被**静默地编译进 libA**，而不是单独成库。这是嵌套中最常见的「状态串味」陷阱，根因是 `CurrentLib` 这个隐式游标。

**练习 2**：为什么不能让每个库的 `config.tcl` 自己调 `init`，从而「各自独立」？

**参考答案**：`init` 会清零 `Libraries/Sources/TbRuns`（L368-377）。若 `b_config.tcl` 调 `init`，它会把 `a_config.tcl` 已累积的声明全部抹掉，项目层就只能看到最后一个被 `source` 的库。所以 `init` 必须上移到唯一的 `run.tcl`、且只调一次；`config.tcl` 保持「纯声明」。

---

### 4.2 多库与 `-tag` 分组

#### 4.2.1 概念说明

多库工程里有两个**正交**的分组维度，初学者常把它们混淆：

- **`-lib`（库）**：物理维度。一个库对应仿真器里的一个编译目标（Modelsim 的 `work` 之类、GHDL 的一个 `--work=` 名字）。文件必须归属到某个库，跨库引用靠库可见性。
- **`-tag`（标签）**：逻辑维度。一个任意字符串，贴在 `Sources` 字典的 `TAG` 字段上，纯粹用来「选择性编译」。同一个库里可以有多种 tag，同一种 tag 也可以跨多个库。

两者的关系是「**逻辑或**地选择要不要打标签，**与**地过滤要不要编译」。换言之，`-lib` 圈定一个物理集合，`-tag` 在（可选地）进一步圈定一个逻辑子集。Tag 不影响文件被编译进哪个库，只影响 `compile -tag X` 时它会不会被选中。

为什么要 tag？在多库、多模块的工程里，你常常只想重编译「与某个特性相关的文件」——比如所有公共包（`-tag lib`）、所有业务源（`-tag src`）、所有测试台（`-tag tb`）。README 示例正是这样分组的：

[README.md:82-103](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L82-L103) —— 同一个库里用 `-tag src`（业务源）与 `-tag tb`（测试台）把文件分成两组。

#### 4.2.2 核心流程

多库 + tag 的典型声明顺序（注意「先建库再加文件」）：

```
add_library psi_common                 ;# Libraries={psi_common}, CurrentLib=psi_common
add_sources "../hdl" "a.vhd b.vhd" -tag src
add_sources "../testbench" "a_tb.vhd" -tag tb

add_library psi_tb                     ;# Libraries={psi_common,psi_tb}, CurrentLib=psi_tb
add_sources "../tb/hdl" "util.vhd" -tag lib
```

随后可用不同切片编译：

| 命令 | 选中文件 |
|------|----------|
| `compile_files -all` | 全部（库、tag 都不过滤） |
| `compile_files -lib psi_common` | psi_common 库里的全部文件（不论 tag） |
| `compile_files -tag tb` | 任意库里 tag==tb 的文件（此处即 `a_tb.vhd`） |
| `compile_files -lib psi_common -tag tb` | 同时满足：psi_common 库 **且** tag==tb |
| `run_tb -lib psi_common` | 只跑 `TB_LIB==psi_common` 的 run |
| `run_tb -all` | 跑全部 run |

注意 `run_tb` 没有 `-tag`（tag 只贴在源文件上，测试运行只有库与名字两个维度）。

#### 4.2.3 源码精读

**`-tag` 如何进入字典**：`add_sources` 解析 `-tag` 开关，写入 `TAG` 字段：

[PsiSim.tcl:451-454](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L451-L454) —— 解析 `-tag <name>`；若不传，`tag` 保持初值空串 `""`。

[PsiSim.tcl:479-485](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L479-L485) —— 每个文件构造 dict，`dict set ThisSrc TAG $tag` 把标签固化进数据模型（详见 u2-l2）。

**`-lib` 与 `-tag` 如何联合过滤**：`compile` 用三个哨兵表达「该维度不过滤」，再用三个 `continue` 串成 AND：

[PsiSim.tcl:550-554](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L550-L554) —— 哨兵初值：`Library="All-Libraries"`、`Tag="All-Tags"`、`contains="All-regex"`。

[PsiSim.tcl:594-602](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L594-L602) —— 三个 `continue`：库不匹配则跳过、tag 不匹配则跳过、路径不含子串则跳过。形式化地，一个文件 \(f\) 被选中当且仅当：

\[
\text{selected}(f) = (L=\star \lor L=f_L) \;\land\; (T=\star \lor T=f_T) \;\land\; (C=\star \lor C \sqsubseteq f_P)
\]

其中 \(\star\) 代表哨兵值（「不过滤」），\(f_L/f_T/f_P\) 是该文件字典的 `LIBRARY/TAG/PATH`，\(C \sqsubseteq f_P\) 表示「\(C\) 是 \(f_P\) 的子串」。三个子句任一为假就 `continue`，故是 AND。

**`compile_files` 只是包装**：

[PsiSim.tcl:609-613](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L609-L613) —— `compile_files` 用 `join`+`eval` 把参数原样转发给内部 `compile`。两名字并存只为规避 Modelsim 自带 `compile` 命令的命名冲突（`compile` 不 `namespace export`，详见 u2-l5）。对外请始终用 `compile_files`。

**运行期按库过滤**：`run_tb` 的 `-lib` 命中 `TB_LIB` 字段：

[PsiSim.tcl:794-796](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L794-L796) —— `runLib != Library` 且 `Library != "All-Libraries"` 则 `continue`。`TB_LIB` 在 `create_tb_run` 时缺省取 `CurrentLib`（见 u2-l3 的 `create_tb_run`），所以「先 `add_library` 再 `create_tb_run`」的顺序决定了测试运行归到哪个库。

#### 4.2.4 代码实践

**实践目标**：用一张表预测不同 `compile_files` 切片会编译哪些文件，验证你对「库 ∧ tag」AND 过滤的理解。

**操作步骤**：

1. 假设 `Sources` 经多库声明后含如下 5 个文件（`LIBRARY/TAG`）：
   - `psi_common_math_pkg.vhd` → `psi_common / src`
   - `psi_common_sync_fifo.vhd` → `psi_common / src`
   - `psi_common_sync_fifo_tb.vhd` → `psi_common / tb`
   - `psi_tb_txt_util.vhd` → `psi_tb / lib`
   - `psi_tb_compare_pkg.vhd` → `psi_tb / lib`
2. 对下述每条命令，用上面的 AND 公式预测命中集合：
   - `compile_files -all`
   - `compile_files -lib psi_common`
   - `compile_files -tag lib`
   - `compile_files -lib psi_common -tag tb`
   - `compile_files -tag src -lib psi_tb`（注意这是个「陷阱」项）

**需要观察的现象**：最后一条应命中**空集**——因为没有任何文件同时满足「在 psi_tb 库」且「tag==src」。这正是 AND 语义的体现：两个维度是「同时满足」而非「任一满足」。

**预期结果**（命中文件数）：`-all`→5；`-lib psi_common`→3；`-tag lib`→2；`-lib psi_common -tag tb`→1；`-tag src -lib psi_tb`→0。可在本地用一段 Tcl 把这 5 个 dict 塞进 `Sources` 后调用 `compile`（mock 掉 `sal_compile_file`）验证，或直接对照 L594-602 的三个 `continue` 推演。

#### 4.2.5 小练习与答案

**练习 1**：`-lib` 和 `-tag` 有什么本质区别？能否用 `-tag` 完全替代 `-lib`？

**参考答案**：`-lib` 是物理维度，决定文件编译进仿真器的哪个库（影响跨文件可见性与 `vsim lib.tb` 的引用路径）；`-tag` 是纯逻辑维度，只影响 `compile` 的选中与否，不改变文件归属的库。不能用 tag 替代 lib：即使两个文件 tag 相同，若分属不同库，它们在仿真器里仍是不同的编译实体，测试台引用的是「库名.实体名」。

**练习 2**：在嵌套场景下，为什么推荐每个库的 `config.tcl` 都用一致的 tag 词汇（如统一用 `src/tb/lib`）？

**参考答案**：因为 `compile_files -tag X` 是**跨库**生效的（`-lib` 缺省即 `All-Libraries`）。统一词汇让你在项目层能用一句 `compile_files -tag tb` 一次性重编译所有库的测试台，而不必逐库指定。词汇不一致会让 tag 切片碎片化，失去「跨库逻辑分组」的价值。

---

### 4.3 `-contains` 局部迭代

#### 4.3.1 概念说明

开发期最高频的动作是：**改一个文件，只想重编译它、重跑它的测试**，而不是每次都全量 `compile_files -all -clean` + `run_tb -all`。PsiSim 为此提供 `-contains`——一个**子串过滤器**。

但这里有一个**容易踩坑的不对称**，必须先讲清楚：

- `compile_files -contains X` 匹配的是源文件的**绝对路径** `PATH`（L600）。
- `run_tb -contains X` 匹配的是测试运行的**名字** `TB_NAME`（L800）。

两者匹配的对象不同！所以为了让「重编译」与「重跑」对齐到同一批东西，你需要选一个**同时出现在文件路径和测试台名字里**的子串。这天然要求一种命名约定：例如把 fifo 相关的测试台放在 `.../fifo_tb/fifo_tb.vhd`、实体名也叫 `fifo_tb`，那么子串 `fifo` 既能命中该文件路径，也能命中该测试台名字。

`-contains` 与 `-clean` 的搭配是另一个关键取舍：

- **不带 `-clean`** 的 `compile_files -contains X`：只重编译路径含 `X` 的文件，**保留**其他文件已编译的实体。最快，但如果被改动的文件有下游依赖，下游不会自动重编译（PsiSim 不跟踪 HDL 依赖），可能跑出过期结果。
- **带 `-clean`**：会先清库。但注意 `compile -clean` 调用的是 `clean_libraries -lib $Library`，而 `$Library` 缺省是哨兵 `"All-Libraries"`——也就是说，`compile_files -tag X -clean` 或 `compile_files -contains X -clean` 会**清掉所有库**，然后只重编译匹配的那一小撮，留下大量「未编译」的文件。CommandRef 明确告警：

[CommandRef.md:422-425](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L422-L425) —— `-clean` 选项官方说明：应仅与 `-all` 联用。

所以局部迭代的**安全姿势**是：首次全量 `compile_files -all -clean` 建好基线，之后迭代只用 `-contains`（不带 `-clean`），改完跑 `run_tb -contains X` 验证。

#### 4.3.2 核心流程

一个完整的「改 fifo 测试台 → 验证」迭代循环：

```
# 一次性基线（首次或依赖结构变化时）
compile_files -all -clean
run_tb -all
run_check_errors "###ERROR###"

# —— 之后改了 fifo_tb.vhd ——
# 步骤1：只重编译 fifo 相关源文件（路径含 "fifo"）
compile_files -contains fifo
# 步骤2：只重跑 fifo 相关测试（名字含 "fifo"）
run_tb -contains fifo
# 步骤3：判错（run_check_errors 读的是 ./Transcript.transcript，每次 run_tb 开头会自动 clean_transcript）
run_check_errors "###ERROR###"
```

注意 `run_tb` 开头会自动 `clean_transcript`（u2-l7 已讲），所以局部跑之前不必手动清日志。但若你跳过 `run_tb` 直接读旧 transcript，就会拿到上一次的内容——这是另一个易错点。

#### 4.3.3 源码精读

**编译期 `-contains`：匹配 PATH**

[PsiSim.tcl:570-573](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L570-L573) —— 解析 `-contains <str>`，存入 `contains`（缺省哨兵 `"All-regex"`，名字里带 regex 但实现并非正则）。

[PsiSim.tcl:600-602](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L600-L602) —— `[string first $contains $thisFilePath] == -1` 则 `continue`。这是对**绝对路径** `thisFilePath`（即字典里的 `PATH`，经 `file normalize`，u2-l2）做子串匹配，不是正则。所以 `compile_files -contains fifo` 会命中路径里任何位置含 `fifo` 的文件（如 `.../fifo_tb/fifo_tb.vhd`、`.../sync_fifo.vhd`）。

**运行期 `-contains`：匹配 TB_NAME**

[PsiSim.tcl:773-776](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L773-L776) —— 解析 `-contains <str>`。

[PsiSim.tcl:800-802](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L800-L802) —— `[string first $contains $runName] == -1` 则 `continue`。这里 `runName` 是 `TB_NAME`（字典里的 `TB_NAME`，由 `create_tb_run` 第一个参数设定，u2-l3）。同样是 `string first` 子串匹配，但作用对象是测试台**名字**，不是路径。

两者机制相同（`string first` 子串），但**数据源不同**——这就是 4.3.1 所说「不对称」的源码根因。

**`-clean` 的全局副作用**：

[PsiSim.tcl:581-583](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L581-L583) —— `if {$clean} { clean_libraries -lib $Library }`。注意传给 `clean_libraries` 的是 `$Library`，而 `$Library` 在只给 `-tag`/`-contains` 时仍是哨兵 `"All-Libraries"`。

[PsiSim.tcl:530-536](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L530-L536) —— `clean_libraries` 的循环：`($Library == "All-Libraries") || ($Library == $lib)` 即清库。`Library=="All-Libraries"` 时**所有库都被清**。于是 `compile_files -contains fifo -clean` 会清空全部库、却只重编译 fifo 相关文件——其余文件实体消失，后续 `run_tb` 会因找不到实体而失败。

#### 4.3.4 代码实践

**实践目标**：为一个「修改了 fifo 测试台」的场景，设计一条最小代价的验证命令序列，并解释每条命令的命中集合。

**操作步骤**：

1. 假设工程含：`.../sync_fifo.vhd`（业务源）、`.../fifo_tb/fifo_tb.vhd`（测试台，实体名 `fifo_tb`）、以及一堆与 fifo 无关的文件。
2. 你刚改了 `fifo_tb.vhd` 的某个断言。写出迭代命令。
3. 分别写出 `compile_files -contains fifo` 与 `run_tb -contains fifo` 的命中对象（路径 vs 名字）。
4. 思考：如果你把测试台实体命名为 `sf_tb`、但文件路径仍是 `.../fifo_tb/sf_tb.vhd`，`-contains fifo` 还能同时命中两者吗？

**需要观察的现象**：编译命令命中**路径**含 `fifo` 的文件（`sync_fifo.vhd` 与 `fifo_tb.vhd` 都会被重编译，因为路径里都有 `fifo`）；运行命令命中**名字**含 `fifo` 的 run（只有 `fifo_tb` 这个测试台）。在第 4 步的命名下，编译仍命中（路径有 fifo），但 `run_tb -contains fifo` **不再命中**（名字是 `sf_tb`，不含 fifo）——迭代循环断裂。

**预期结果**：安全序列为 `compile_files -contains fifo` → `run_tb -contains fifo` → `run_check_errors "###ERROR###"`。结论：**稳定的命名约定（文件路径与实体名共享同一子串）是 `-contains` 迭代循环可靠的前提**。若无法保证命名一致，应改用 `-name`（运行期精确名）或退回 `-all`。

> 说明：命中集合可对照 L600-602 与 L800-802 的 `string first` 推演；实际运行需仿真器，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `compile -contains` 匹配 PATH、`run_tb -contains` 匹配 TB_NAME，这种「不对称」是设计缺陷还是合理取舍？

**参考答案**：合理取舍。编译期手里只有源文件，最自然的判据就是「这个文件是不是我想重编的」，路径子串最直观；运行期手里只有测试运行登记，最自然的判据是「这个测试是不是我想重跑的」，名字子串最直观。代价是要求使用者用一致的命名约定把两者对齐。这是一种「把责任交给约定」的典型权衡（PsiSim 多处如此，如 `###ERROR###` 的独特性责任也在使用者）。

**练习 2**：`compile_files -tag tb -clean` 会出现什么问题？为什么？

**参考答案**：`-clean` 触发 `clean_libraries -lib $Library`，而此处 `$Library` 仍是哨兵 `"All-Libraries"`（因为没给 `-lib`），于是**清掉所有库**；随后只重编译 tag==tb 的文件，导致 `src`/`lib` 等其他 tag 的文件实体丢失，`run_tb` 时仿真器找不到被测实体而失败。正确做法：要么 `compile_files -all -clean`（全量重建），要么局部迭代时**不带** `-clean`（依赖既有编译产物）。

## 5. 综合实践

**任务**：设计一个含两个子库 `psi_common`、`psi_tb` 的项目结构。每个子库有自己的 `config.tcl` 和 `run.tcl`，可独立跑自己的回归；顶层 `run.tcl` 通过 `source` 两个子库的 `config.tcl` 把回归汇总。写出关键文件内容草图，并用 `-tag`、`-contains` 说明日常迭代姿势。

**目标目录结构**（示例草图）：

```
project/
├── TCL/PsiSim/PsiSim.tcl          # 框架本体（被各 run.tcl source）
├── psi_common/
│   ├── config.tcl                 # 声明 psi_common 的库/源/测试
│   ├── run.tcl                    # 独立跑 psi_common 回归
│   ├── hdl/*.vhd                  # 业务源（-tag src）
│   └── testbench/*_tb/*.vhd       # 测试台（-tag tb）
├── psi_tb/
│   ├── config.tcl                 # 声明 psi_tb 的库/源/测试
│   ├── run.tcl                    # 独立跑 psi_tb 回归
│   └── hdl/*.vhd                  # 测试辅助库（-tag lib）
└── run.tcl                        # 项目顶层：汇总两库回归
```

**`psi_common/config.tcl`（草图）**：

```tcl
# 示例代码（非项目原有文件，为本实践设计的草图）
namespace import psi::sim::*
add_library psi_common
compile_suppress 135,1236
run_suppress 8684,3479
add_sources "../hdl" {
    psi_common_math_pkg.vhd \
    psi_common_sync_fifo.vhd \
} -tag src
add_sources "../testbench" {
    psi_common_sync_fifo_tb/psi_common_sync_fifo_tb.vhd \
} -tag tb
create_tb_run "psi_common_sync_fifo_tb"
tb_run_add_arguments "-gDepth_g=32" "-gDepth_g=128"
add_tb_run
```

**`psi_common/run.tcl`（草图）**：

```tcl
# 示例代码
source ../../TCL/PsiSim/PsiSim.tcl
namespace import psi::sim::*
init                            ;# 独立跑：本库自己 init 一次
source ./config.tcl
compile_files -all -clean
run_tb -all
run_check_errors "###ERROR###"
```

**`psi_tb/config.tcl` / `psi_tb/run.tcl`**：结构完全对称，只是 `add_library psi_tb`、文件指向 `psi_tb/hdl`、tag 用 `lib`。

**项目顶层 `project/run.tcl`（草图，本实践的核心）**：

```tcl
# 示例代码
source ./TCL/PsiSim/PsiSim.tcl
namespace import psi::sim::*
init                                          ;# 全工程唯一一次 init
source ../psi_common/config.tcl               ;# 累积 psi_common 声明
source ../psi_tb/config.tcl                   ;# 继续累积 psi_tb 声明
compile_files -all -clean                     ;# 编译两库全部 Sources
run_tb -all                                   ;# 跑两库全部 TbRuns
run_check_errors "###ERROR###"
```

**验收要点**（对照本讲所学逐一自检）：

1. **嵌套正确性**：顶层只 `init` 一次；两个 `config.tcl` 都不含 `init`；`source` 顺序使 `Libraries={psi_common psi_tb}`。
2. **游标安全**：每个 `config.tcl` 都以 `add_library` 开头，避免 4.1.5 练习 1 的「状态串味」（否则 psi_tb 的文件会被错编进 psi_common）。
3. **tag 切片**：项目层可用 `compile_files -tag tb` 一次性重编译两库的全部测试台；用 `run_tb -lib psi_common` 只跑 psi_common 的回归。
4. **局部迭代**：改了 `psi_common_sync_fifo_tb.vhd` 后，用 `compile_files -contains fifo` → `run_tb -contains fifo` → `run_check_errors "###ERROR###"` 做最小代价验证（前提是命名约定让 `fifo` 同时出现在路径与实体名里）。
5. **`-clean` 纪律**：局部迭代不带 `-clean`；只有全量重建才用 `compile_files -all -clean`。

> 说明：以上 `config.tcl`/`run.tcl` 均为「示例代码」草图，需结合你实际的 VHDL 文件名与目录调整后才能运行；实际跑通需 Modelsim/GHDL/Vivado 之一，结果待本地验证。

## 6. 本讲小结

- **嵌套的本质**是「状态表跨 `source` 持久 + `config.tcl` 是纯声明」：库级 `run.tcl` 各自 `init`+`source` 自己的 config；项目级 `run.tcl` 只 `init` 一次，再依次 `source` 各库 config，让 `Libraries/Sources/TbRuns` 累积成一张大表。
- **`init` 是分水岭**：它清零数据模型（L368-377），必须在 `source` 任何 config 之前、且全工程恰好一次；放进 config 会抹掉前面的声明。
- **多库靠 `add_library` 追加 + `CurrentLib` 游标**：`add_sources` 缺省 `-lib` 时取 `CurrentLib`，故「先建库再加文件」的顺序决定归属，错序会静默串味。
- **`-lib`（物理）与 `-tag`（逻辑）正交**：`compile` 用三个哨兵 + 三个 `continue` 做库 ∧ tag ∧ 路径包含的 AND 过滤（L594-602）；统一 tag 词汇让项目层能跨库切片。
- **`-contains` 是开发期局部利器**：编译期匹配 `PATH`、运行期匹配 `TB_NAME`（L600-602 vs L800-802），机制相同但数据源不同，需用一致的命名约定把两者对齐。
- **`-clean` 有全局副作用**：`compile -clean` 传的是 `$Library`，缺省即 `All-Libraries` 会清掉所有库（L581-583/L530-536），故局部迭代不应带 `-clean`，CommandRef 也告警其仅与 `-all` 联用。

## 7. 下一步学习建议

- **u4-l2（GHDL/GTKWave 工作流深度实践）**：把本讲设计的多库工程换成 `init -ghdl` 跑通，体会 GHDL 下多库、双版本编译与库产物子目录的差异。
- **u4-l3（扩展新模拟器与架构取舍）**：本讲反复出现的「全局可变状态 + 隐式游标 `CurrentLib` + 命名约定责任」正是 PsiSim 的架构取舍，下一篇会系统讨论其风险与改进方向。
- **回看 u2-l5 / u2-l6**：若你对 `-lib/-tag/-contains` 的哨兵过滤细节仍有疑虑，重读这两篇的源码精读可与本讲互相印证。
- **延伸阅读**：对照 `CommandRef.md` 的 `compile_files`/`run_tb`/`clean_libraries` 三节，把本讲的语义结论与官方文档逐条核对，建立「源码 ↔ 文档」的双向校验习惯。
