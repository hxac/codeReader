# 形式验证：PSL 断言与覆盖率报告

## 1. 本讲目标

学完本讲，你应当能够：

- 读懂 `DPRAM_CONT.v` 里三条以注释形式写出的 **PSL 断言**，并说出它们各自守护的环形缓冲不变式。
- 理解 Questa 仿真流程中**代码覆盖率（code coverage）**与**断言覆盖率（assertion coverage）**是如何分别被「编译期插桩」与「运行期收集」的。
- 逐行解读 `report.txt`，把里面的 `Bins/Hits/Misses/Coverage` 数字对应回具体的 RTL 语句，判断哪些分支/条件/断言真正被执行过。
- 学会为一个新的模块（`SPROM_CONT` 的奇偶地址切换）起草一条 PSL 断言，并意识到「复位/初值例外」是写断言时必须处理的坑。

本讲是 **u1-l3（仿真流程）** 的形式化延伸，也是 **u3-l2（DPRAM_CONT 环形缓冲控制器）** 的形式化收口：u3-l2 用自然语言描述的「读地址领先写地址 1」「REN 正常时恒为 1」等直觉，在这里被写成机器可检验的断言并被仿真证明。

## 2. 前置知识

### 2.1 什么是断言（Assertion）

写 RTL 时，我们心里总有一些「任何时候都必须成立」的规则，例如「写地址与读地址相等的那一拍，写使能必须为 0」。**断言**就是把这种规则写成一行机器可读、每个时钟边沿自动检查的语句。一旦某拍违反，仿真器立刻报错并指出时间和位置。

### 2.2 什么是 PSL

**PSL（Property Specification Language，IEEE 1850）** 是一种专门描述「时序性质」的形式化语言。它和 Verilog 的区别在于：Verilog 描述「电路怎么算」，PSL 描述「电路永远不能怎样」。本讲会用到几个最小语法元素：

| PSL 关键字 / 符号 | 含义 |
| --- | --- |
| `psl` | 行首指令标记。Questa 会扫描源码里以 `// psl` 开头的注释行，把它当作真正的断言编译进来（inline PSL）。 |
| `assert` | 断言指令。性质被违反时报严重错误。 |
| `always (P)` | 时序算子：性质 `P` 在**每一个**采样点都必须成立。 |
| `@ (posedge CLK)` | 采样事件：在 `CLK` 上升沿对性质求值。 |
| `->` | 逻辑蕴含：「如果 `A` 则 `B`」，等价于 `(!A) || B`。注意这是 PSL 的蕴含，不是 Verilog 的连线。 |
| `==` | 相等（沿用 Verilog 语义）。 |

### 2.3 什么是覆盖率（Coverage）

覆盖率回答的问题是「我的仿真到底把设计**跑过**了多少」。它分两大类：

- **代码覆盖率（code coverage）**：从 RTL 结构出发，统计分支、条件、语句等是否被执行过。它衡量「代码有没有被走过」，但不保证「走得对」。
- **断言覆盖率（assertion coverage）**：统计每条断言是否被求值过（attempted）、是否曾失败。它衡量「我写的性质有没有真的被检验」。

两者互补：代码覆盖率高只代表「没死代码」，断言通过才代表「性质被满足」。本讲的 `report.txt` 同时呈现这两类。

> 名词速查：**Bin（箱）** 是覆盖点的一个取值分支；**Hit（命中）** 表示该箱在仿真中被触发过；**Miss（遗漏）** 表示定义了却没被触发；**Coverage = Hits / Bins**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [01_DPRAM_CONT/DPRAM_CONT.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT.v) | 环形缓冲地址控制器，本讲的**断言主角**：三条 PSL 断言以注释形式写在输出赋值旁边。 |
| [07_FIR_x2/Questa/FIR_x2.bat](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/FIR_x2.bat) | 编译与仿真批处理：决定哪些文件插桩代码覆盖率（`+cover=bcs`）。 |
| [07_FIR_x2/Questa/run.do](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/run.do) | 仿真 do 文件：末尾的 `coverage report` 把覆盖数据汇总成 `report.txt`。 |
| [07_FIR_x2/Questa/report.txt](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/report.txt) | 最终覆盖率报告：本讲的**解读对象**。 |
| [07_FIR_x2/FIR_x2.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v) | 顶层模块，提供代码覆盖率的来源（唯一的饱和 `always` 块）。 |
| [03_SPROM_CONT/SPROM_CONT.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT.v) | 系数地址控制器：综合实践里**新断言的草拟对象**。 |

---

## 4. 核心概念与源码讲解

### 4.1 PSL 断言语义

#### 4.1.1 概念说明

PSL 断言的本质是：**在每个时钟沿，把一句「应当永远成立」的布尔/时序表达式求值一次，结果为假就报错**。它不改变电路功能，只是在仿真时挂上一层「检查器」。写得好的断言等于把设计的**不变式（invariant）**固化进代码：日后任何人改 RTL 只要破坏了不变式，回归仿真立刻炸出来。

`DPRAM_CONT` 选择了 inline PSL 的写法——把断言藏在 `// psl ...` 注释里，紧跟在它所守护的那行 `assign` 上方。好处是断言与被检查逻辑零距离，读代码时一眼能看到「这根线承诺什么」。

#### 4.1.2 核心流程

一条 inline PSL 断言从写下到生效，经历三步：

1. **编译期**：Questa 的 `vlog` 扫描源码，识别 `// psl` 行，把它编译成一个挂在指定时钟上的检查器对象（不需要额外命令行开关）。
2. **运行期**：`vsim -coverage` 启动仿真后，每个 `@ (posedge MCLK_I)` 采样点都对性质求值。
3. **报告期**：若任一采样点性质为假，仿真器立即打印断言失败（严重级别）；若全程为真，则在 `coverage report -assert` 里记为「命中」。

一句话流程图：

```
// psl assert always (P) @ (posedge CLK);
        │
        ▼  vlog 扫描识别
   挂在 CLK 上的检查器
        │
        ▼  vsim 每个 posedge 求值 P
   P==1 ?  ──否──▶ 报断言失败（断点）
        │是
        ▼
   coverage 记一次「命中」
```

#### 4.1.3 源码精读

`DPRAM_CONT` 把三条断言贴在三段输出赋值旁，注释里的 `#0/#1/#2` 编号即 `report.txt` 中 `Assertions 3` 的来源。

**断言 #0：读使能永远有效**

[01_DPRAM_CONT/DPRAM_CONT.v:102-104](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT.v#L102-L104)

```verilog
// Assertion #0: REN_O must be 1'b1
// psl assert always (REN_O == 1'b1) @ (posedge MCLK_I);
assign REN_O = ~(WEN_REG & (WADDR_REG == RADDR_REG));
```

它声明「`REN_O` 在任意 `MCLK` 上升沿都等于 1」。结合 u3-l2 的公式 `REN_O = ~(WEN_REG & (WADDR_REG==RADDR_REG))`，这条断言等价于断定「碰撞项 `WEN_REG & (WADDR_REG==RADDR_REG)` 永不为真」。**这条断言比 u3-l2 的自然语言直觉更严格**：它证明即使进入复位预填充阶段，`REN_O` 也从不掉 0——因为复位时 `RADDR_REG` 每拍比 `WADDR_REG` 领先 1，二者永不相等。

**断言 #1：地址撞址时禁止写**

[01_DPRAM_CONT/DPRAM_CONT.v:106-108](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT.v#L106-L108)

```verilog
// Assertion #1: WEN_O must be 1'b0 when WADDR_O equals to RADDR_O.
// psl assert always ((WADDR_O == RADDR_O) -> (WEN_O == 1'b0)) @ (posedge MCLK_I);
assign WEN_O = WEN_REG;
```

用 PSL 蕴含 `->` 表达：「一旦写地址等于读地址，写使能必须为 0」。这是读写冲突的护栏：万一某拍两根指针撞上，控制器宁可放弃写也不能覆盖正在被读的单元。正常情况下 `WADDR` 与 `RADDR` 由断言 #2 保证相差 1，撞址本不该发生；断言 #1 是兜底。

**断言 #2：写发生时，读指针恰好领先写指针 1**

[01_DPRAM_CONT/DPRAM_CONT.v:110-113](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT.v#L110-L113)

```verilog
// Assertion #2: RADDR_O must be equals to (WADDR_O + 1'b1) if WEN_REG_P is 1'b1
// psl assert always ((WEN_O == 1'b1) -> (RADDR_O == WADDR_O + 1'b1)) @ (posedge MCLK_I);
assign WADDR_O = WADDR_REG;
assign RADDR_O = RADDR_REG;
```

这是 u3-l2 「读地址领先写地址 1」不变式的形式化。它只在写使能拉高的那一拍检查（用 `WEN_O==1 -> ...` 限定），正好对应新样点刚写入、读指针开始扫描历史的那一刻。

> 三条断言合起来构成环形缓冲的**完整形式化契约**：#0 读永不停止、#1 撞址不写、#2 写时读领先 1。

#### 4.1.4 代码实践

**实践目标**：把每条断言对应到一个具体的 RTL 行为。

1. 打开 `01_DPRAM_CONT/DPRAM_CONT.v`，定位 102–113 行的三条 `// psl` 注释。
2. 对照 u3-l2 的 RTL，分别回答：断言 #0 在「复位预填充」阶段为何仍成立？断言 #2 在「LRCK 上升沿那一拍」`WADDR/RADDR` 各取何值？
3. 需要观察的现象：在脑中（或仿真波形里）追踪一个 LRCK 周期内 `WEN_REG`、`WADDR_REG`、`RADDR_REG` 的取值，验证三条性质在每一拍都为真。
4. 预期结果：三条断言对应的行为在全程均成立——这正是 `report.txt` 给出 `Assertions 3/3 = 100%` 的原因。
5. 若你无法在脑中确认某条，标注「待本地验证」并用 Questa 波形核对。

#### 4.1.5 小练习与答案

**练习 1**：把断言 #1 改成不带 `->` 的纯布尔形式（提示：用德摩根律）。

**答案**：`always ((WADDR_O != RADDR_O) || (WEN_O == 1'b0))`。因为 `(A -> B) == (!A || B)`。

**练习 2**：如果有人把 `REN_O` 的公式改成 `assign REN_O = WEN_REG & (WADDR_REG == RADDR_REG);`，断言 #0 会怎样？

**答案**：碰撞项被直接输出，正常运行时 `REN_O` 几乎恒为 0，断言 #0 会在第一个采样点立即失败并报严重错误——这正是断言的防御价值。

---

### 4.2 覆盖率收集与报告

#### 4.2.1 概念说明

覆盖率不会自己产生，它需要两个前提：**编译时插桩**（告诉工具「请统计这段代码的分支/条件/语句」）和**运行时收集**（仿真时把「是否走过」记下来）。本项目的关键设计取舍是——**只有顶层 `FIR_x2.v` 被插桩了代码覆盖率，而 `DPRAM_CONT.v` 没有**。这解释了为什么 `report.txt` 里两个设计单元的指标「长短不一」。

需要区分四种覆盖率指标的来源：

| 指标 | 来源 | 本项目是否收集 |
| --- | --- | --- |
| Branches / Conditions / Statements | RTL 结构，需 `+cover=bcs` 编译期插桩 | 仅 `FIR_x2.v`（见 `FIR_x2.bat` 第 5 行） |
| Assertions | PSL/SVA 断言对象，`vsim -coverage` 自动收集 | `DPRAM_CONT`（3 条 inline PSL） |
| Covergroup（功能覆盖）| `covergroup` 构造，需 RTL 显式定义 | 无（项目未定义，`-cvg` 空跑） |
| Directive（指令覆盖）| PSL `cover` 指令 | 无（项目未定义，`-directive` 空跑） |

#### 4.2.2 核心流程

```
vlog +cover=bcs  FIR_x2.v          ┐ 仅顶层插桩 b/c/s
vlog              DPRAM_CONT.v ... ┘ 其余模块含 PSL 但不插桩代码覆盖
        │
        ▼
vsim -coverage  work.FIR_x2_TB     ← 启用运行期收集（断言+已插桩代码）
        │
        ▼  do run.do
run -all
coverage report -output report.txt -du=* -assert -directive -cvg -codeAll
```

注意「**断言覆盖率不需要 `+cover=bcs`**」：PSL 断言一经 `vlog` 识别就成为检查器对象，`vsim -coverage` 自动统计它。所以 `DPRAM_CONT.v` 即便用裸 `vlog` 编译，它的 3 条断言照样出现在报告里——这是初学者最容易看漏的一点。

#### 4.2.3 源码精读

**编译期：只有顶层插桩**

[07_FIR_x2/Questa/FIR_x2.bat:5-13](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/FIR_x2.bat#L5-L13)

```bat
vlog +cover=bcs ../FIR_x2.v          ← 仅此文件插桩 branch/condition/statement
vlog ../../01_DPRAM_CONT/DPRAM_CONT.v   ← 裸编译：无代码覆盖插桩，但 PSL 仍生效
...
```

`+cover=bcs` 的三个字母分别对应 **b**ranch、**c**ondition、**s**tatement（不含 **t**oggle）。第 6–13 行的子模块都用裸 `vlog`，所以它们在报告里**不会出现 Branches/Conditions/Statements 行**——`DPRAM_CONT` 只靠 PSL 断言贡献了一行 Assertions。

**运行期：开启收集**

[07_FIR_x2/Questa/FIR_x2.bat:16](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/FIR_x2.bat#L16)

```bat
vsim -debugdb=+acc work.FIR_x2_TB -voptargs=+acc -coverage -do "do run.do"
```

`-coverage` 是开关：仿真时把「断言是否被求值」「插桩代码是否被走过」逐拍累计下来。

**报告期：一行命令汇总一切**

[07_FIR_x2/Questa/run.do:17](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/run.do#L17)

```tcl
coverage report -output report.txt -du=* -assert -directive -cvg -codeAll
```

各选项含义：

| 选项 | 作用 |
| --- | --- |
| `-output report.txt` | 把报告写入文件。 |
| `-du=*` | 报告所有设计单元（`Design Unit` 通配）。 |
| `-assert` | 汇总**断言覆盖**（产出 `DPRAM_CONT` 的 Assertions 行）。 |
| `-directive` | 汇总 PSL/SVA **指令覆盖**（本项目无 `cover` 指令，故空）。 |
| `-cvg` | 汇总 **covergroup 功能覆盖**（本项目无，故空）。 |
| `-codeAll` | 汇总**所有代码覆盖类型**（branch/condition/statement/…，产出 `FIR_x2` 的 B/C/S 行）。 |

#### 4.2.4 代码实践

**实践目标**：解释报告里两个设计单元指标不对称的根因。

1. 对比 `report.txt` 中 `work.DPRAM_CONT` 与 `work.FIR_x2` 两段，注意前者只有 `Assertions`，后者只有 `Branches/Conditions/Statements`。
2. 回到 `FIR_x2.bat` 第 5 行与第 6 行，指出二者编译开关的差异。
3. 需要观察的现象：若你把第 6 行也改成 `vlog +cover=bcs ../../01_DPRAM_CONT/DPRAM_CONT.v`，重跑后 `DPRAM_CONT` 段会多出 Branches/Conditions/Statements 行。
4. 预期结果：指标不对称完全由「编译期插桩与否」决定，而非模块本身有无分支。
5. 这是一个「改一行编译开关、重跑、对比报告」的源码阅读型实践；本地若无 Questa 可标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `DPRAM_CONT` 没有给 `+cover=bcs`，断言却仍被统计？

**答案**：因为断言覆盖率来自 PSL 检查器对象，`vlog` 识别 `// psl` 行即生成检查器，`vsim -coverage` 自动统计，与代码覆盖率插桩（`+cover=bcs`）是两套独立机制。

**练习 2**：`+cover=bcs` 里为何没有 `t`？

**答案**：`t` 是 toggle（翻转）覆盖，统计每根信号的 0→1/1→0。本项目关心控制逻辑的结构覆盖（分支/条件/语句），不关心逐信号翻转，故未启用。若加上 `+cover=bcst`，报告会多出 Toggles 行。

---

### 4.3 时序不变式验证

#### 4.3.1 概念说明

「时序不变式」是指在**每个时钟沿**都应成立的性质。前两节已经把工具链备齐：4.1 用断言把不变式写出来，4.2 用覆盖率把「不变式被检验到了」量化出来。本节把两者落到一份具体的报告上——`report.txt`，看它如何证明「设计不仅跑过，而且性质被满足」。

解读覆盖率报告有一个固定心法：**先看 Bins（定义了多少），再看 Misses（漏了多少），最后看 Coverage（百分比）**。`Misses != 0` 意味着有定义但仿真没覆盖到的角落；对于断言，`Misses` 还可能伴随「失败」，而失败会以独立报错出现。

#### 4.3.2 核心流程

```
打开 report.txt
   │
   ├── work.DPRAM_CONT 段：看 Assertions 行
   │       3 Bins / 3 Hits / 0 Misses / 100%
   │       ⇒ 3 条断言全部被求值且无失败
   │
   ├── work.FIR_x2 段：看 Branches/Conditions/Statements 行
   │       2/2、1/1、7/7 全 100%
   │       ⇒ 顶层饱和分支两种走向都被走过（非死代码）
   │
   └── 末尾 TOTAL：断言总覆盖 100%，按 DU 汇总 100%
```

#### 4.3.3 源码精读

**`DPRAM_CONT` 段：断言全覆盖**

[07_FIR_x2/Questa/report.txt:4-8](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/report.txt#L4-L8)

```
=== Design Unit: work.DPRAM_CONT ===
    Enabled Coverage     Bins   Hits   Misses  Coverage
    Assertions              3      3        0   100.00%
```

`Bins=3` 对应 4.1.3 的三条断言（#0/#1/#2），`Hits=3 / Misses=0` 表示三条都在仿真中被求值过且全程未违反。注意：这里**没有** Branches/Conditions/Statements 行——这正是 4.2.4 解释的「未插桩代码覆盖」的结果。

**`FIR_x2` 段：代码全覆盖**

[07_FIR_x2/Questa/report.txt:11-17](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/report.txt#L11-L17)

```
=== Design Unit: work.FIR_x2 ===
    Enabled Coverage     Bins   Hits   Misses  Coverage
    Branches               2      2        0   100.00%
    Conditions             1      1        0   100.00%
    Statements             7      7        0   100.00%
```

这三个数字都能在 `FIR_x2.v` 里逐一找到出处：

- **Statements = 7**：顶层共有 7 条可统计语句——`always` 块内 3 条非阻塞赋值（[FIR_x2.v:169-171](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L169-L171)）加上 `DUMMY_NRST` 与 3 条输出 `assign`（[FIR_x2.v:92](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L92)、[175-177](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L175-L177)），合计 7。

- **Branches = 2**：唯一的运行时分支来自饱和三元表达式（[FIR_x2.v:171](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L171)）：

  ```verilog
  DATAO_REG <= (ADD_DATA[MULT_WIDTH-2] == ADD_DATA[MULT_WIDTH-3]) ? <截位> : <饱和>;
  ```

  它产生「饱和 / 不饱和」两个分支。第 175 行 `(WADDR_WIDTH >= 7) ? ... : MCLK_I` 是**常量**三元（参数在编译期求值），不产生运行时分支，故不计数。

- **Conditions = 1**：上述饱和判定的条件 `ADD_DATA[MULT_WIDTH-2] == ADD_DATA[MULT_WIDTH-3]`，其真/假两种取值在仿真中都被走过。

- **关键结论**：`Branches 2/2` 证明饱和分支（u5-l3 的钳位路径）**真的被触发过**，不是死代码；这正是 u5-l3 「饱和与不饱和上界连续无跳变」结论的实证。

**末尾汇总**

[07_FIR_x2/Questa/report.txt:20-22](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/report.txt#L20-L22)

```
TOTAL ASSERTION COVERAGE: 100.00%  ASSERTIONS: 3
Total Coverage By Design Unit (filtered view): 100.00%
```

#### 4.3.4 代码实践

**实践目标**：确认全覆盖，并为 `SPROM_CONT` 起草一条新断言。

**第一部分——确认 100%（结论可直接从 `report.txt` 读出）**

1. 打开 `report.txt`，确认 `work.DPRAM_CONT` 的 `Assertions` 为 3/3=100%。
2. 确认 `work.FIR_x2` 的 `Branches/Conditions/Statements` 均为 100%。
3. 这一步无需运行仿真，报告本身就是结论。

**第二部分——为 SPROM_CONT 起草一条 PSL 断言（草案，需验证）**

回顾 u4-l2：`SPROM_CONT` 的奇偶地址切换由 [SPROM_CONT.v:82-88](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT.v#L82-L88) 实现——正常模式下

```verilog
CADDR_REG <= {(CADDR_REG[ROM_ADDR_WIDTH-1:1] + 1'b1), LRCK_I};
```

即地址最低位 `CADDR_REG[0]` 直接跟随 `LRCK_I`（高电平扫奇地址、低电平扫偶地址）。最自然的「奇偶切换」不变式就是这条 LSB 跟随关系。模仿 `DPRAM_CONT` 的注释风格，草案如下：

```verilog
// Assertion (draft): The LSB of CADDR_REG tracks LRCK_I (odd/even phase select).
// psl assert always (CADDR_REG[0] == $past(LRCK_I, 1)) @ (posedge MCLK_I);
```

1. 操作步骤：在 `SPROM_CONT.v` 的 `always` 块后插入上面两行注释，重跑 `FIR_x2.bat`+`run.do`。
2. 需要观察的现象：注意 [SPROM_CONT.v:84](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT.v#L84) 的初始化分支把 `CADDR_REG` 强置为 `{…, 1'b1}`，与当前 `LRCK_I` 无关——这与 `DPRAM_CONT` 不同，所以草案**很可能在 LRCK 上升沿那一拍失败**。
3. 预期结果：要么用 `disable iff` 或前件把初始化/复位拍排除，要么改选一个对初值不敏感的不变式（见小练习）。
4. 因涉及本地仿真，断言是否一次通过标注「待本地验证」——这正是起草断言的真实工作流：先写、再跑、再按失败信息加例外。

> **更稳妥的备选草案**：由 [SPROM_CONT.v:91](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT.v#L91) 的 `CADDRO_REG <= {ROM_ADDR_WIDTH{1'b1}} - CADDR_REG`（即按位取反）可写
> `// psl assert always (CADDRO_REG == (~CADDR_REG)) @ (posedge MCLK_I);`
> 它不依赖 `LRCK_I` 的时序，但同样要在第一拍（`CADDRO_REG` 与 `CADDR_REG` 尚未同步）处理初值例外。

#### 4.3.5 小练习与答案

**练习 1**：`report.txt` 里 `work.FIR_x2` 没有 `Assertions` 行，是否说明顶层没有需要验证的性质？

**答案**：不是。这只说明顶层没有写 PSL/SVA 断言。顶层的关键性质（如饱和前提）目前由 `DPRAM_CONT` 的断言 + u6-l1 的 `MAX_TOTAL` 抽头和检查 + u5-l3 的结构共同保证，而非顶层断言。

**练习 2**：若一次仿真后 `report.txt` 显示某断言 `Bins=1, Hits=0, Misses=1`，意味着什么？该如何处置？

**答案**：意味着该断言在整个仿真中**一次都没被求值**（例如其时钟从未翻转，或使能条件从未成立）。处置办法：扩充测试激励让对应采样点出现；否则这条断言形同虚设——「没失败」不等于「被验证」。

**练习 3**：为什么说 `Branches 2/2 = 100%` 是 u5-l3 饱和结论的关键证据？

**答案**：饱和分支是「正常不会走、一旦溢出才走」的罕见路径。`2/2` 证明仿真里确实构造出了触发饱和的输入，使钳位路径被执行过；若它是 `1/2`（Misses=1），就说明饱和代码从未被走过，u5-l3 关于「饱和与不饱和上界连续」的论断将缺少仿真支撑。

---

## 5. 综合实践

把本讲三块知识串起来，完成下面这条贯穿任务（即本讲规格指定的实践）：

1. **读报告**：打开 [07_FIR_x2/Questa/report.txt](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/report.txt)，逐段确认——`work.DPRAM_CONT` 的 `Assertions` 为 3/3=100%，`work.FIR_x2` 的 `Branches/Conditions/Statements` 均为 100%。
2. **回溯证据**：在 [DPRAM_CONT.v:102-113](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT.v#L102-L113) 找到对应 3 条断言的源码；在 [FIR_x2.v:171](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L171) 找到产生 `Branches=2` 的三元表达式，说明两条分支分别是「饱和」与「不饱和」。
3. **解释不对称**：用 [FIR_x2.bat:5-6](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/FIR_x2.bat#L5-L6) 解释为什么两个单元的指标列不同。
4. **起草断言**：为 [SPROM_CONT.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT.v) 的奇偶地址切换写一条 PSL 断言草案（见 4.3.4），写出你预期的「可能失败的拍」以及打算如何加例外。
5. 产出：一张三列表格——「断言/分支 → 对应源码行 → 报告里的数字」，把报告里的每个 `100%` 都钉到一行真实代码上。

> 全程不需要修改任何源码逻辑（断言草案只是新增注释行）。若本地无 Questa，断言草案的有效性标注「待本地验证」，但前 3 步的读报告与回溯证据可完全基于本仓库已提供的 `report.txt` 完成。

## 6. 本讲小结

- `DPRAM_CONT` 的三条 inline PSL 断言把环形缓冲的直觉写成了机器可检验的契约：#0 读使能永不拉低、#1 撞址时禁止写、#2 写发生时读指针恰好领先写指针 1。
- inline PSL 以 `// psl assert always (P) @ (posedge CLK);` 形式藏在注释里，Questa 的 `vlog` 自动识别，无需额外开关。
- 覆盖率分两套独立机制：**代码覆盖**（branch/condition/statement）需编译期 `+cover=bcs` 插桩；**断言覆盖**由 `vsim -coverage` 自动收集，与代码插桩无关——这解释了报告里 `DPRAM_CONT` 只有 Assertions、`FIR_x2` 只有 B/C/S 的不对称。
- `report.txt` 的 `Bins/Hits/Misses/Coverage` 心法：先看定义数、再看遗漏数、最后看百分比；`Assertions 3/3` 与 `Branches 2/2` 共同证明「性质被满足且饱和分支非死代码」。
- 起草新断言（如 `SPROM_CONT` 的奇偶 LSB 跟随）必须处理复位/初值例外——`$past`、初始化强置位都是常见失败源，这是「先写、再跑、再加例外」真实工作流的起点。

## 7. 下一步学习建议

- **横向扩展断言**：把 4.3.4 的方法应用到 `MULT`/`ADD`，为「数据延迟＝时钟延迟＝2 拍」的对齐契约（u5-l1、u5-l2）各起草一条 PSL 断言，体会「不变式越精确，越能锁住流水线对齐」。
- **补全代码覆盖**：尝试把 `FIR_x2.bat` 中子模块也加上 `+cover=bcs` 重跑，观察 `DPRAM_CONT` 等单元的 Branches/Conditions 是否仍为 100%，找出哪些分支依赖特定的输入信号才会被走到。
- **结合 u6-l1 的源头保证**：把本讲的饱和分支覆盖与 u6-l1 的 `MAX_TOTAL` 抽头和检查对照阅读，理解「Python 端预防 + Verilog 端饱和 + 仿真端断言/覆盖」三层防线如何共同保证滤波器线性。
- **若要深入形式化**：PSL（IEEE 1850）与 SVA（SystemVerilog Assertions）的完整时序算子（`until`、`before`、`next`、`within`）可让你表达比 `always (A -> B)` 更复杂的时间关系，是迈向形式验证（formal verification）的下一级台阶。
