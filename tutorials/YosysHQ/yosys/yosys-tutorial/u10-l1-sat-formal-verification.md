# SAT 与形式验证机制

## 1. 本讲目标

本讲是「高级内部机制」的第一讲，带你进入 Yosys 的形式化验证内核。

前面 u7-l3 讲过三类**形式验证后端**（`write_smt2`/`write_aiger`/`write_btor`）：它们把 RTLIL 写成求解器吃的格式，交给外部工具（如 `yosys-smtbmc`）做模型检查。本讲要回答一个更底层的问题：**Yosys 自己是如何把一个门级网表翻译成 SAT 问题的？又是如何用这套能力做电路等价检查的？**

学完后你应当掌握：

1. 什么是 SAT 问题、CNF，以及为什么时序电路要「按时间帧展开（unrolling）」。
2. `SatGen` 如何把一个 RTLIL 单元（如 `$and`、`$dff`）逐个编码为布尔约束。
3. `sat` 命令（`SatHelper`）如何驱动 `SatGen`、用「反证法」证明电路性质，以及如何做 k-归纳（temporal induction）。
4. 等价检查链 `equiv_make → equiv_simple/equiv_induct → equiv_status`，以及 `$equiv` 单元的作用。
5. 能动手对同一设计的两个版本做一次等价检查，并解释结果。

## 2. 前置知识

本讲假设你已经学过 **u3-l4（内部单元库）**，知道 `$and`/`$or`/`$mux`/`$dff` 这类以 `$` 开头的高层单元的端口约定（二元运算 `A/B→Y`、触发器 `CLK/D→Q`）。同时建议回顾 **u3-l2（SigSpec/SigMap）** 和 **u7-l3（形式验证后端）**。

下面把本讲会用到的几个概念用通俗语言过一遍。

### 2.1 什么是 SAT 问题

**SAT（Boolean Satisfiability，布尔可满足性）** 问题是这样的：给定一个由布尔变量和与/或/非组成的公式，问是否存在一组变量的 `0/1` 赋值，使整个公式为真。

- 如果存在这样的赋值，称为 **SAT（可满足）**，求解器会给出一个「反例」。
- 如果不存在，称为 **UNSAT（不可满足）**，即「无论怎么赋值公式都不可能为真」。

这一点非常关键：形式验证里大量使用 **反证法**——「我想证明性质 P 永远成立」等价于「把 NOT(P) 丢给 SAT 求解器，若 UNSAT 则 P 成立」。

### 2.2 CNF 与 ezSAT

现代 SAT 求解器（如 MiniSAT）只认 **CNF（合取范式）**：若干个子句（clause，变量或其否定的「或」）再整体做「与」。

Yosys 不要求你手写 CNF，而是提供一个名为 **ezSAT** 的辅助库（位于 `libs/ezsat/`）。你用 `vec_and(a,b)`、`vec_or(a,b)`、`vec_eq(x,y)`、`assume(c)` 这类高层 API 构造表达式，ezSAT 内部用 Tseitin 变换自动翻译成等价的 CNF，并交给底层求解器。

\[ \text{RTLIL 网表} \xrightarrow{\text{SatGen}} \text{ezSAT 表达式} \xrightarrow{\text{Tseitin}} \text{CNF} \xrightarrow{\text{MiniSAT}} \text{SAT / UNSAT} \]

### 2.3 时序电路为什么要「展开」

一个组合逻辑门（如 `$and`）的输入输出关系在同一时刻成立，直接编码即可。但触发器（`$dff`）表示「**下一个时钟沿**把 D 端的值搬到 Q 端」，这是一种**跨时刻**的关系。

SAT 本身没有「时间」概念，所以 Yosys 用 **时间帧展开（time-frame expansion / unrolling）** 的经典技巧：为每个时间步 `t` 复制一份电路变量。于是「Q 在 t 时刻的值 = D 在 t-1 时刻的值」就变成了一条**同一组 SAT 变量之间**的约束。展开 N 帧，就把「未来 N 步内会不会出错」变成了一个纯组合的 SAT 问题。

### 2.4 BMC 与 k-归纳

形式验证有两大常用手段（u7-l3 已提及，这里强化）：

- **BMC（Bounded Model Checking，有界模型检查）**：展开 N 帧，问「这 N 步内是否存在违反性质的路径」。SAT → 找到反例；UNSAT → 这 N 步内安全（但不能保证永远安全）。
- **k-归纳（k-induction）**：分两步证明「永远安全」。①基例（base case）：从初始状态出发，前 k 步性质成立；②归纳步（induction step）：**假设**任意连续 k 步性质都成立，证明第 k+1 步也成立。两者都 UNSAT，则性质对所有时刻成立。

记住这两个词，本讲的 `sat -tempinduct`、`equiv_induct` 都建立在它们之上。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [kernel/satgen.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/satgen.h) | `SatGen` 类声明：信号导入、单元编码的接口 |
| [kernel/satgen.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/satgen.cc) | `SatGen::importCell`：把每种 RTLIL 单元翻译成 ezSAT 约束 |
| [kernel/register.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc) | 默认 SAT 求解器（MiniSAT）的注册 |
| [passes/sat/sat.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/sat/sat.cc) | `sat`/`eval` 命令：`SatHelper` 驱动 `SatGen` 做约束求解与性质证明 |
| [passes/equiv/equiv.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv.h) | 等价检查的公共基类 `EquivWorker`（复用 `SatGen`） |
| [passes/equiv/equiv_make.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_make.cc) | 构造 miter：把 gold/gate 两版设计合并并插入 `$equiv` 单元 |
| [passes/equiv/equiv_simple.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_simple.cc) | 用 SAT（BMC 风格）逐个证明 `$equiv` 单元 |
| [passes/equiv/equiv_induct.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_induct.cc) | 用 k-归纳证明 `$equiv` 单元 |
| [passes/equiv/equiv_status.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_status.cc) | 统计已证明/未证明的 `$equiv` 单元数量 |
| [passes/equiv/equiv_opt.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_opt.cc) | `equiv_opt`：把上述命令编排成「证明优化不改行为」的脚本 |

一句话总览：`SatGen` 是**唯一的编码器**（RTLIL→SAT），被 `sat` 命令和 `equiv_*` 系列共用；`equiv_*` 只是「构造问题 + 调求解器 + 报结果」的不同外壳。

## 4. 核心概念与源码讲解

### 4.1 satgen 编码：把门级网表翻译成 SAT 约束

#### 4.1.1 概念说明

`SatGen` 的职责非常纯粹：**给定一个 RTLIL 单元，向一个 ezSAT 实例里添加若干布尔约束，使这组约束在语义上等价于该单元的功能。**

举个最朴素的例子，一个二输入与门 `y = a & b`，其 SAT 编码就是一条约束：

\[ (a \land b) \iff y \]

也就是说，任何对 `a`、`b`、`y` 的赋值，只有满足「当且仅当 a、b 同时为 1 时 y 为 1」才被求解器接受。把网表里所有单元都这样编码后，求解器找到的合法赋值，就对应电路的一种合法行为。

`SatGen` 还要解决两个额外问题：

1. **信号 → SAT 变量的映射**：网表里的每一根线（的每一位）要变成一个 SAT 变量；常数位直接变成 `CONST_TRUE`/`CONST_FALSE`。
2. **时间**：通过给变量名加时间步前缀（如 `@1:`、`@2:`），让同一根线在不同时刻对应不同 SAT 变量，从而支持时序电路展开。

#### 4.1.2 核心流程

`SatGen` 编码一个模块的基本流程：

1. **导入信号**：对要约束的每个 `SigSpec`，逐位映射为 SAT 字面量（literal）。
   - 线上的位 → 一个新的 SAT 变量（带前缀和时间步）。
   - 常数位 → `CONST_TRUE`/`CONST_FALSE`。
2. **导入单元**：对每个单元调用 `importCell`，按 `cell->type` 分发到对应的编码分支，向 ezSAT 添加约束。
3. **(可选) 建模 x 不定值**：开启 `model_undef` 时，为每一位额外维护一个「是否为 x」的变量，做四值逻辑的 x 传播。

编码的关键直觉可以用伪代码概括：

```
importSigSpec(sig, timestep):
    对 sig 的每一位 bit:
        若 bit 是常数: 返回 CONST_TRUE 或 CONST_FALSE
        否则: 以 "<prefix>@<t>:<wire>[offset>" 为名新建/复用一个 SAT 变量

importCell($and):          ez.assume( vec_eq( vec_and(A, B), Y ) )
importCell($dff, t):       若 t==1: Q 视为自由初值
                           否则:     ez.assume( vec_eq( D[t-1], Q[t] ) )
importCell($assert):       记录到 asserts_a，供 importAsserts 汇总
```

#### 4.1.3 源码精读

**入口与求解器抽象。** `SatGen` 持有一个 `ezSAT *ez` 指针和一个 `SigMap`。`ezSatPtr` 是它的工厂，默认指向全局求解器：

[kernel/satgen.h:60-63](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/satgen.h#L60-L63) 定义 `ezSatPtr`，它的构造函数调用 `yosys_satsolver->create()` 生成一个 `ezSAT` 实例。这里的 `yosys_satsolver` 是一个全局指针，指向当前选用的求解器；`SatSolver` 是一个注册用的抽象基类（[kernel/satgen.h:36-58](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/satgen.h#L36-L58)），采用与 Pass 类似的链表式去中心化注册。默认求解器在 [kernel/register.cc:1191-1198](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L1191-L1198) 注册：`MinisatSatSolver` 在构造时把 `yosys_satsolver` 指向自己，`create()` 返回一个 `ezMiniSAT`。所以 **Yosys 默认就是用 MiniSAT 求解**。

**信号导入与时间步前缀。** 编码的原子操作是 `importSigSpec`：

[kernel/satgen.h:114-119](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/satgen.h#L114-L119) 中，时间步 `timestep` 被拼成前缀 `prefix + "@<t>:"`（`timestep==-1` 表示无时间维度的组合问题）。真正干活的是 [kernel/satgen.h:90-112](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/satgen.h#L90-L112) 的 `importSigSpecWorker`：对每一位，若 `bit.wire == NULL`（常数位）则返回 `CONST_TRUE`/`CONST_FALSE`，否则用 `ez->frozen_literal(name)` 新建一个**具名** SAT 变量并记入 `imported_signals` 表（保证同名同位只创建一次）。这就是「时间帧展开」的落点：同一个 `SigBit` 在 `@1:` 和 `@2:` 下会得到两个不同的 SAT 变量。

> 小贴士：`frozen_literal` 表示「这个名字的变量在本次求解里固定不变」，便于多帧复用同一变量。

**组合门编码：以 `$and` 为例。** 真正的翻译逻辑在 [kernel/satgen.cc:62-91](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/satgen.cc#L62-L91)。这段把 `$_AND_/$_NAND_/$_OR_/.../$and/$or/$xor/$add/$sub` 一族二元单元统一处理：先导入 A、B、Y 三个端口，做位宽对齐（`extendSignalWidth`），再按类型发约束。关键一行：

[kernel/satgen.cc:72-73](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/satgen.cc#L72-L73) 对 `$and`/`$_AND_` 执行 `ez->assume(ez->vec_eq(ez->vec_and(a, b), yy));`，这正是 \((a \land b) \iff y\) 的逐位向量版。`vec_and` 是逐位与，`vec_eq` 是逐位等价（即「两向量每位都相等」），`assume` 把这个约束加进 CNF。`$or`/`$xor` 等只是把 `vec_and` 换成 `vec_or`/`vec_xor`（[kernel/satgen.cc:76-83](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/satgen.cc#L76-L83)）。

**时序单元编码：以 `$dff` 为例。** 触发器在 [kernel/satgen.cc:1206-1284](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/satgen.cc#L1206-L1284) 处理。注意它的**前置条件** `timestep > 0`——时序单元只有在「有时间维度」的问题里才有意义；同时若该 FF 带异步置位/复位（`has_aload/has_arst/has_sr`）则 `return false`，提示先用 `async2sync`/`clk2fflogic` 转成同步形式（这呼应 u7-l3 中形式验证要求同步化的说法）。

- [kernel/satgen.cc:1214-1221](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/satgen.cc#L1214-L1221)：当 `timestep == 1` 时，只把 Q 端记入 `initial_state` 集合而不加任何约束——**初值是自由的**（由调用方决定是全 0、全 x 还是任意）。这正是「寄存器上电状态未知」的建模。
- [kernel/satgen.cc:1224](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/satgen.cc#L1224)：`d = importDefSigSpec(cell->getPort(ID::D), timestep-1)`，即「D 在**上一时刻**的值」。
- [kernel/satgen.cc:1273](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/satgen.cc#L1273)：`ez->assume(ez->vec_eq(d, qq));`，这是寄存器传递的核心约束 \(\text{D}[t-1] = \text{Q}[t]\)。中间若有使能（CE）或同步复位（SRST），则通过 `mux` 把 d 替换成「使能有效时保持旧值 / 复位有效时取复位值」（[kernel/satgen.cc:1242-1269](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/satgen.cc#L1242-L1269)）。

**断言的收集。** 形式验证里的 `$assert` 单元（来自 `read_verilog -formal`）在 [kernel/satgen.cc:1366-1372](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/satgen.cc#L1366-L1372) 并不立即生成约束，而是把它的 A（断言条件）和 EN（使能）累积到 `asserts_a[pf]`/`asserts_en[pf]` 表里，再由 `importAsserts`（[kernel/satgen.h:177-189](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/satgen.h#L177-L189)）在需要时汇总成一个大的「所有断言都成立」的合取式。

**无法编码时降级。** `importCell` 遇到不支持的单元（如 `$fsm`、`$mem*`、异步 FF）会 `return false`（[kernel/satgen.cc:1387-1389](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/satgen.cc#L1387-L1389)），由调用方经 [kernel/satgen.cc:1394-1407](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/satgen.cc#L1394-L1407) 的 `report_missing_model` 报错或告警。这就是为什么做 SAT 形式验证前，设计通常要先 `prep`/`memory_map`/`async2sync`，把高层单元降成 `SatGen` 认识的门级形式。

#### 4.1.4 代码实践

**目标**：亲手验证「`$and` 在 SAT 里就是 `(A&B)==Y`」这句话，建立对编码的直观认识。

**步骤**：

1. 准备一个极小 Verilog 文件 `gate.v`：
   ```verilog
   module gate(input a, b, output y);
     assign y = a & b;
   endmodule
   ```
2. 在 yosys 里读入并查看综合出的单元：
   ```bash
   yosys -p "read_verilog gate.v; prep; write_rtlil"
   ```
   你会在 RTLIL 文本里看到一个 `$and` 单元，端口为 `\A`、`\B`、`\Y`。
3. （源码阅读型）打开 [kernel/satgen.cc:62-91](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/satgen.cc#L62-L91)，确认 `$and` 的编码就是第 73 行那句 `assume(vec_eq(vec_and(a,b), yy))`。
4. 用 `sat` 命令问一个问题：「能否让 `y=1` 而 `a=0`？」（若 UNSAT，说明编码正确反映了与门语义）：
   ```
   yosys -p "read_verilog gate.v; prep; sat -set y=1 -set a=0"
   ```

**需要观察的现象**：第 4 步应输出 `SAT solving failed - found contradiction`（即 UNSAT），因为约束 `y=1` 与「`y = a&b` 且 `a=0`」矛盾——这正是「编码等价于与门功能」的证据。

**预期结果**：`y=1, a=0` 无解；改为 `-set a=1 -set b=1` 则可解且 `y=1`。若你的 yosys 构建未含 MiniSAT，命令会报错——这一点「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`SatGen` 为什么对 `$dff` 要求 `timestep > 0`？若在 `timestep == -1`（纯组合问题）里遇到触发器会怎样？

**答案**：触发器表达的是跨时刻关系，没有时间帧（`timestep == -1`）就无法表达「D[t-1] = Q[t]」。此时该分支不会被命中，`importCell` 会继续往下走，最终因找不到匹配的编码分支而 `return false`，调用方报「No SAT model」。所以时序电路必须用带 `-seq`（时间步）的求解方式。

**练习 2**：`importSigSpecWorker` 为什么要把已导入的位记入 `imported_signals[pf][bit]` 表？

**答案**：保证「同一根线在同一个前缀/时间步下只创建一个 SAT 变量」。否则同一信号会被实例化成两个独立变量，它们之间没有任何约束，等于「断线」，编码就错了。这正是 2.3 节「时间帧展开」中变量复用的关键。

**练习 3**：`-enable_undef`（`model_undef = true`）打开后，`$and` 的编码会比原来多出什么？

**答案**：会额外为 A、B、Y 各维护一组「是否为 x」的变量，并加上 x 传播规则（如「A、B 都确定不为 0 时，若任一为 x 则 Y 也为 x」），见 [kernel/satgen.cc:93-133](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/satgen.cc#L93-L133)。这套「undef gating」让求解器能区分 0/1/x，是 `-prove-x`、`equiv_induct -undef` 等的基础。

---

### 4.2 passes/sat：用反证法证明电路性质

#### 4.2.1 概念说明

`SatGen` 只负责「翻译」，不负责「提问」。`passes/sat/sat.cc` 里的 `sat` 命令（以及它的好兄弟 `eval`）就是**提问者**：它把用户在命令行里写的约束（`-set`、`-prove`）翻译成 SAT 问题，然后调用求解器。

它的核心思想是 **反证法证明（proof by contradiction）**：

- `-prove <信号> <值>`：我想证明「该信号永远等于该值」。
- 实现：`assume( NOT( 信号 == 值 ) )`，再求解。
  - UNSAT → 找不到反例 → **性质成立**。
  - SAT → 求解器给出一个反例赋值 → **性质不成立**。

对于时序电路，`sat` 还支持 `-seq <N>`（展开 N 个时间步做 BMC）和 `-tempinduct`（做 k-归纳），把第 2.4 节的两大手段直接暴露给用户。

#### 4.2.2 核心流程

`sat` 命令的主体是 `SatHelper` 结构体，它把 `ezSAT` 求解器、`SatGen` 编码器、用户约束三者粘合在一起。整体流程：

```
SatHelper:
  ├─ setup(timestep):     应用 -set/-set-at 约束，遍历 module->cells() 调 satgen.importCell
  ├─ setup_proof():       把 -prove 条件汇总成一个布尔表达式（性质 P）
  └─ 主循环:
       ez->assume( NOT(P) )          # 反证法：假设性质不成立
       ez->solve():
           UNSAT  → 性质成立 (SUCCESS)
           SAT    → 有反例，打印波形 (FAILED)
```

k-归纳模式（`-tempinduct`）则把单一求解拆成两个 `SatHelper`：

```
basecase:    从初值出发，展开 seq_len 帧，证明前 seq_len 步性质成立
inductstep:  假设性质在一段窗口内成立（把性质作为前提 assume），证明再走一步仍成立
两者都 UNSAT  → 归纳成功
```

#### 4.2.3 源码精读

**SatHelper 持有的两件套。** [passes/sat/sat.cc:58-59](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/sat/sat.cc#L58-L59) 中，`ezSatPtr ez`（求解器）和 `SatGen satgen`（编码器）并列——这就是「提问者」的全部内核。其余字段（`sets`/`prove`/`prove_x`/`sets_at` 等，[passes/sat/sat.cc:62-76](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/sat/sat.cc#L62-L76)）都是「用户约束的存放处」。

**setup()：导入整张网表。** [passes/sat/sat.cc:244-252](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/sat/sat.cc#L244-L252) 是核心循环：对模块里**每一个被选中的单元**调用 `satgen.importCell(cell, timestep)`；若返回 `false`（无模型）则交给 `report_missing_model`。也就是说，`setup(t)` 就是「把整个模块在时间步 t 上的行为，编码进 SAT 数据库」。`setup` 还会把用户的 `-set x=v` 翻成 `assume(signals_eq(x, v))`（[passes/sat/sat.cc:174-176](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/sat/sat.cc#L174-L176)），把 `init` 属性翻译成初值约束（[passes/sat/sat.cc:273-302](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/sat/sat.cc#L273-L302)）。

**反证法的落点。** [passes/sat/sat.cc:1654-1665](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/sat/sat.cc#L1654-L1665) 是普通（非归纳）证明模式的关键：`sathelper.ez->assume(sathelper.ez->NOT(sathelper.setup_proof()))`。`setup_proof()` 把所有 `-prove` 条件合取成一个表达式 P（性质），这里对它取反再 assume——「请找一个让性质不成立的赋值」。随后的 `solve` 若失败（UNSAT）即证明成立，若成功（SAT）则打印出反例波形。

**时间帧与 BMC。** [passes/sat/sat.cc:1658-1665](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/sat/sat.cc#L1658-L1665) 用 `for (int timestep = 1; timestep <= seq_len; timestep++)` 循环展开多个时间步，并对每步调 `setup_proof(timestep)` 收集性质位，最终要求「所有步的性质都不能被同时违反」。这就是 BMC 的实现。

**k-归纳。** [passes/sat/sat.cc:1438-1439](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/sat/sat.cc#L1438-L1439) 创建 `basecase` 与 `inductstep` 两个独立的 `SatHelper`；归纳步的关键在 [passes/sat/sat.cc:1482](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/sat/sat.cc#L1482)：`inductstep.ez->assume(inductstep.setup_proof(1))`——把「性质在窗口内成立」**作为前提**，再去证明下一步仍成立。两边都 UNSAT，归纳成功。

#### 4.2.4 代码实践

**目标**：用 `sat -prove` 证明一个简单组合电路的性质，体会「反证法 = 取反再求 SAT」。

**步骤**：

1. 写一个 2 选 1 多路器 `mux2.v`：
   ```verilog
   module mux2(input s, a, b, output y);
     assign y = s ? b : a;
   endmodule
   ```
2. 读入、prep，然后证明性质「当 `s=0` 时 `y==a`」：
   ```bash
   yosys -p "read_verilog mux2.v; prep; sat -set s=0 -prove y a"
   ```
   这里 `-prove y a` 表示「证明 y 恒等于 a」。
3. 再故意给一个**错误**的性质看反例：
   ```bash
   yosys -p "read_verilog mux2.v; prep; sat -set s=0 -prove y b"
   ```
   （`s=0` 时 y 应等于 a，而非 b，故应给出反例。）

**需要观察的现象**：第 2 步应输出 `Proof succeeded!`；第 3 步应输出 `SAT proof failed - model found:` 并给出一组反例赋值（如 `a=0, b=1, s=0, y=0`）。

**预期结果**：见上。若求解器未编入则报错，标「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`-prove y a` 与 `-set y a` 在 SAT 层面有什么本质区别？

**答案**：`-set` 是把 `signals_eq(y,a)` 作为**硬约束 assume**（「我规定它们相等，请在满足此前提下找解」）；`-prove` 则是把 `signals_eq(y,a)` 作为**待证性质 P**，实际 assume 的是 `NOT(P)`，求 UNSAT 来证明。前者会改变可行解空间，后者只是提问、不改变电路语义。

**练习 2**：为什么 k-归纳要分 base case 和 induction step 两次求解？只做归纳步行不行？

**答案**：归纳步只证明了「**若**前 k 步成立，则第 k+1 步也成立」这一蕴含关系，但它不保证电路**从初始状态出发**真的进入这个「成立窗口」。缺少 base case，一个永远违反性质但有「自洽」转移的电路也能通过归纳步。两者合起来才构成完整的数学归纳：base case 提供起点，induction step 提供递推。

**练习 3**：`sat` 命令默认求解器是什么？在哪里设定的？

**答案**：默认 MiniSAT，由 [kernel/register.cc:1191-1198](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L1191-L1198) 的全局静态对象 `MinisatSatSolver` 在启动时把 `yosys_satsolver` 指向自己；`SatGen`/`SatHelper` 通过 `ezSatPtr` 调 `yosys_satsolver->create()` 得到 `ezMiniSAT` 实例。

---

### 4.3 passes/equiv：等价检查链

#### 4.3.1 概念说明

**等价检查（equivalence checking）** 回答的问题是：两份电路（通常一份是「优化前/gold」，一份是「优化后/gate」）在相同输入下，输出是否永远相同？这是验证「我的优化/综合没改逻辑」的标准手段。

Yosys 的等价检查不是一条命令，而是一条**链**，核心围绕一个特殊单元 `$equiv`：

| 命令 | 作用 |
|------|------|
| `equiv_make gold gate equiv` | 构造 miter：把两版设计合并进 `equiv` 模块，在每个对应信号上插一个 `$equiv` 单元（断言「gold 这一位 == gate 这一位」） |
| `equiv_simple [-seq N]` | 用 SAT（BMC 风格，逐位、向后扫输入锥）证明每个 `$equiv` |
| `equiv_induct [-seq N]` | 用 k-归纳证明 `$equiv`，擅长复杂时序 |
| `equiv_status [-assert]` | 统计还有多少 `$equiv` 未被证明；`-assert` 时未证明则报错 |

`$equiv` 单元有三个端口 A（gold 侧）、B（gate 侧）、Y。它「已证明」的判据极其简单：**A 与 B 被短路成同一信号**（即 `A == B`）。证明器（`equiv_simple`/`equiv_induct`）成功后就把 B 改成 A；`equiv_status` 据此统计。

#### 4.3.2 核心流程

整条链的数据流：

```
gold 模块  ┐
           ├─ equiv_make ─→  equiv 模块（含若干 $equiv 单元，gold/gate 共享主输入）
gate 模块  ┘                        │
                                    ▼
                          equiv_simple / equiv_induct
                          （用 SatGen 把 equiv 模块编码进 SAT，
                           对每个 $equiv 求 XOR(A,B) 是否可满足）
                                    │
                                    ▼
                          equiv_status
                          （数还剩多少 A≠B 的 $equiv）
```

`equiv_simple` 证明单个 `$equiv` 的算法是经典的 **BMC + 输入锥（input cone）** 思路：

```
对每个待证 $equiv(A, B):
    assume( A XOR B )                      # 假设两者不同
    从 A、B 向后扫它们的「输入锥」（驱动它们的逻辑）
    只把这些锥里的单元 importCell 进 SAT    # 懒加载，保持问题规模小
    solve():
        UNSAT → 不可能不同 → 证明成功，短路 B:=A
        SAT   → 该序列长度下有反例，向上一时间步继续展开（直到 -seq 上限）
```

注意「只导入相关锥」这个设计：它不把整张网表塞进求解器，而是按需扩展，让 SAT 问题尽量小。这与 `sat` 命令「导入整个模块」的策略不同。

#### 4.3.3 源码精读

**公共基类复用 SatGen。** `equiv_simple` 与 `equiv_induct` 共享基类 `EquivWorker`（[passes/equiv/equiv.h:54-65](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv.h#L54-L65)），它和 `SatHelper` 一样持有 `ezSatPtr ez` + `SatGen satgen`。这印证了本讲的主线：**`SatGen` 是共享编码器，等价检查只是换了一种「提问」方式**。公共配置 `EquivBasicConfig`（[passes/equiv/equiv.h:12-52](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv.h#L12-L52)）统一了 `-undef`、`-seq <N>`（默认 1）、`-set-assumes` 等选项。

**equiv_make 构造 miter。** `equiv_make gold gate equiv` 的工作在 [passes/equiv/equiv_make.cc:418-425](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_make.cc#L418-L425) 的 `run()` 里一目了然：`copy_to_equiv` → `find_same_wires` → `find_same_cells`。

- [passes/equiv/equiv_make.cc:103-136](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_make.cc#L103-L136) 的 `copy_to_equiv`：把 gold 和 gate 各克隆一份，把线/单元名加上 `_gold`/`_gate` 后缀，再一起 `cloneInto` 进同一个 `equiv_mod`。两份逻辑就此同居一模块。
- [passes/equiv/equiv_make.cc:258](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_make.cc#L258)（输出端口对应位）和 [passes/equiv/equiv_make.cc:295](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_make.cc#L295)（内部网对应位）：对每一对「应等价」的线，调 `addEquiv(NEW_ID, gold_bit, gate_bit, wire)` 生成一个 `$equiv` 单元。两版设计的**主输入被接到同一根线上**（[passes/equiv/equiv_make.cc:265-274](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_make.cc#L265-L274)），从而保证「相同输入」这个前提。

**equiv_simple 的反证法。** [passes/equiv/equiv_simple.cc:252-269](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_simple.cc#L252-L269) 的 `prepare_ezsat` 把 `$equiv` 的两个端口 A、B 导入后，`ez->assume(ez->XOR(ez_a, ez_b))`——「请找一个让 gold 与 gate **不同**的赋值」。随后在 `prove_equiv_cell` 里，[passes/equiv/equiv_simple.cc:337-343](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_simple.cc#L337-L343) 只把「输入锥」里的单元 `importCell`，[passes/equiv/equiv_simple.cc:347-352](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_simple.cc#L347-L352) 调 `ez->solve(ez_context)`：UNSAT 即「不可能不同」，于是 `cell->setPort(ID::B, cell->getPort(ID::A))` 把 B 短路成 A（标记为已证明）。SAT 则向更早的时间步继续展开（[passes/equiv/equiv_simple.cc:358-393](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_simple.cc#L358-L393)）。

**equiv_induct 的 k-归纳。** [passes/equiv/equiv_induct.cc:146-159](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_induct.cc#L146-L159) 同样 `cond = ez->XOR(ez_a, ez_b)`，但它在更外层（默认 `-seq 4`，见 [passes/equiv/equiv_induct.cc:177](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_induct.cc#L177)）把「前面若干步已等价」作为归纳前提。其 help（[passes/equiv/equiv_induct.cc:179-189](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_induct.cc#L179-L189)）诚实地说明这是一种「弱等价」：它证明两电路在连续 N 周期输出一致后**不会发散**，常配合仿真「前 N 周期已同步」来获得强保证。

**equiv_status 的判据。** [passes/equiv/equiv_status.cc:61-67](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_status.cc#L61-L67) 遍历 `$equiv` 单元，仅凭 `cell->getPort(ID::A) != cell->getPort(ID::B)` 判定「未证明」——因为证明器成功时已把 B 改成 A。[passes/equiv/equiv_status.cc:86-89](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_status.cc#L86-L89) 在 `-assert` 且有未证明项时 `log_error`，这正是脚本里把等价检查接进 CI 的常用开关。

**equiv_opt：一键编排。** [passes/equiv/equiv_opt.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_opt.cc) 是一个 `ScriptPass`（回顾 u4-l2），把上述链打包成「证明某条优化 pass 没改行为」。它的脚本 [passes/equiv/equiv_opt.cc:170-225](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_opt.cc#L170-L225) 分四段：①`run_pass` 存优化前快照 `preopt`、跑用户命令、存优化后快照 `postopt`（[passes/equiv/equiv_opt.cc:170-182](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_opt.cc#L170-L182)）；②`prepare` 把两份快照复制成 `gold`/`gate`；③`prove` 依次 `equiv_make` → `equiv_induct` → `equiv_status`；④`restore` 恢复 `preopt`。注意它默认用的是 **`equiv_induct`**（[passes/equiv/equiv_opt.cc:218](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_opt.cc#L218)），因为时序电路归纳证明更强。

#### 4.3.4 代码实践

**目标**：对同一设计的两个版本（一份原样、一份多做一次 `opt`）用 `equiv_make + equiv_simple + equiv_status` 做等价检查，验证优化没改逻辑。

**步骤**：

1. 准备 `eq.v`（一个含寄存器的小计数器）：
   ```verilog
   module eq(input clk, rst, input [3:0] d, output reg [3:0] q);
     always @(posedge clk) if (rst) q <= 4'b0; else q <= d + q;
   endmodule
   ```
2. 用一条 yosys 脚本构造 gold/gate 两版并做等价检查（保存为 `eq.ys`）：
   ```
   # 读入并准备 gold（保守综合，保留行为）
   read_verilog eq.v
   prep -flatten         # proc + 基础清理，得到门级但保留 $dff

   design -save gold     # 存为 gold
   opt                   # 再做一次优化得到 gate
   design -save gate

   # 合并并构造 miter
   equiv_make gold gate equiv
   select -module equiv

   # 证明（组合电路用 equiv_simple 即可；时序可加 -seq）
   equiv_simple
   equiv_status          # 看还剩多少未证明（想失败即报错可加 -assert）
   ```
3. 运行：`yosys eq.ys`。

**需要观察的现象**：日志会打印 `equiv_make` 插入了多少 `$equiv`，`equiv_simple` 逐个打印 `success!`，最后 `equiv_status` 汇总「Of those cells N are proven and 0 are unproven. Equivalence successfully proven!」。

**对照编码（回到 4.1）**：`equiv_simple` 之所以能证明等价，正是因为它复用 `SatGen` 把 `equiv` 模块里的每个 `$and`（编码成 `(A&B)==Y`）、每个 `$dff`（编码成 `D[t-1]==Q[t]`）翻译成 CNF，然后问「gold 与 gate 在某位上能否不同」。若你给 `d` 端故意加一个取反（制造不等价），`equiv_status` 会报告未证明项——可作为反向实验。

**预期结果**：正常情况下 `Equivalence successfully proven!`，证明 `opt` 保持了行为。若求解器/`prep` 行为有差异，标「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`$equiv` 单元「已被证明」在数据结构上是怎么体现的？`equiv_status` 据何判断？

**答案**：证明器成功后执行 `cell->setPort(ID::B, cell->getPort(ID::A))`，使 A、B 指向同一信号。`equiv_status` 因此只需比较 `getPort(A) != getPort(B)`：相等即已证明，不等即未证明（[passes/equiv/equiv_status.cc:63](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_status.cc#L63)）。这是一种把「证明状态」编码进网表本身的巧妙设计。

**练习 2**：`equiv_simple` 为什么只把「输入锥」里的单元导入 SAT，而不是像 `sat` 命令那样导入整个模块？

**答案**：等价检查关心的是「gold 某位与 gate 某位能否不同」，只有直接或间接驱动这两位的逻辑才与答案相关，其他无关逻辑只会无谓增大 CNF、拖慢求解。按需扩展输入锥（[passes/equiv/equiv_simple.cc:318-343](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_simple.cc#L318-L343)）能在保证正确性的前提下把问题规模压到最小。

**练习 3**：`equiv_opt` 默认用 `equiv_induct` 而非 `equiv_simple`，为什么？

**答案**：`equiv_opt` 要证明「优化前后**时序**电路行为一致」，纯组合式的 `equiv_simple`（默认 `-seq 1`）对带状态机的电路往往证不动；`equiv_induct` 用 k-归纳，能处理「状态需要若干周期才传播到输出」的情况（见 [passes/equiv/equiv_induct.cc:179-181](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_induct.cc#L179-L181) 的说明）。两者并非二选一，复杂设计里常先 `equiv_simple` 清掉简单位、再 `equiv_induct` 收尾。

---

## 5. 综合实践

把本讲三块知识串起来：**编码 → 提问 → 等价检查**。

**任务**：构造一个有意引入「优化前后行为差异」的反例，观察 SAT 等价检查如何抓出它，并解释底层发生了什么。

1. 准备一个 8 位加法器 `add.v`：
   ```verilog
   module add(input [7:0] a, b, output [7:0] s);
     assign s = a + b;
   endmodule
   ```
2. **正确版**等价检查（验证 `opt` 不改行为）：
   ```
   read_verilog add.v
   prep -flatten
   design -save gold
   opt
   design -save gate
   equiv_make gold gate equiv
   select -module equiv
   equiv_simple
   equiv_status -assert
   ```
   预期：`Equivalence successfully proven!`。
3. **故意破坏**：把 `gate` 版本的输出接一个按位取反，再跑一次等价检查：
   ```
   read_verilog add.v
   prep -flatten
   design -save gold
   opt
   select -module add
   %co                # 这一步只是示意；实际可改用 add -not 接到输出
   ...                # （更稳妥：手写第二个模块 add_broken，把 s 改成 ~(a+b)）
   ```
   更简洁的做法是直接写两个模块 `add_gold` 和 `add_broken`，分别 `equiv_make add_gold add_broken equiv` 后检查。
4. **对照源码解释**：当第 3 步 `equiv_status` 报告「unproven」时，请对照 [kernel/satgen.cc:72-73](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/satgen.cc#L72-L73)（`$and` 的 `(A&B)==Y`）和 [passes/equiv/equiv_simple.cc:347](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/equiv/equiv_simple.cc#L347)（`solve(XOR(A,B))`）说明：求解器找到了一组让 gold 与 gate 输出不同的输入（即 SAT），所以这个 `$equiv` 无法被短路，状态保持「unproven」。

**预期成果**：你能用一句话讲清「等价检查 = 用 SAT 找 gold/gate 是否存在分歧，UNSAT 即等价」，并能指出每个 `$and`/`$dff` 在这条链里分别被哪段代码编码、被哪段代码提问。若部分现象「待本地验证」，请如实标注。

## 6. 本讲小结

- **SAT 反证法**是贯穿本讲的核心范式：证明性质 P ⟺ 求 `NOT(P)` 为 UNSAT。`sat -prove`、`equiv_simple`、`equiv_induct` 全部如此。
- **`SatGen`（kernel/satgen.cc）是唯一的编码器**：它把每个 RTLIL 单元翻译成 ezSAT 约束——`$and` 变成 `(A&B)==Y`，`$dff` 变成 `D[t-1]==Q[t]`，靠 `@N:` 前缀实现时间帧展开。
- **时序靠展开，初值是自由的**：触发器在 `timestep==1` 时 Q 不加约束（上电未知），其余时刻服从寄存器传递；异步 FF 需先 `async2sync`/`clk2fflogic`。
- **`sat` 命令（SatHelper）= 编码 + 提问**：它导入整模块、把 `-set` 当约束、把 `-prove` 当待证性质取反后求 UNSAT，`-tempinduct` 提供 k-归纳。
- **等价检查是一条链**：`equiv_make` 构造 miter（gold/gate 共享输入、插 `$equiv`），`equiv_simple`（BMC + 输入锥）或 `equiv_induct`（k-归纳）证明，`equiv_status` 数结果；`$equiv` 已证明 ⟺ A 被短路成 B。
- **分层复用**：`SatGen` 被 `sat`/`eval`、`equiv_*`、以及 smtbmc 等形式 pass 共用——一套编码器支撑了 Yosys 几乎所有内置的 SAT 形式验证能力。

## 7. 下一步学习建议

- **接 u10-l2（functional IR 与 AIG）**：学习 `kernel/cellaigs` 如何把网表转成与门/反相器图（AIG），它与 SAT 编码、abc9 都紧密相关；你会看到另一种「把网表喂给求解器」的表示。
- **接 u7-l3（形式验证后端）**：如果你还没动手，建议实际跑一次 `prep; write_smt2` 配合 `yosys-smtbmc`，体会「SAT 编码（本讲）」与「SMT2 后端 + 外部求解器」两条路线的分工。
- **深入阅读**：对照 [passes/sat/sat.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/sat/sat.cc) 的 `setup_proof` 与时间帧循环，理解 BMC/归纳的实现细节；阅读 `libs/ezsat/ezsat.h` 了解表达式→CNF 的 Tseitin 变换是如何把 `vec_and/vec_eq/assume` 落地为子句的。
- **动手扩展**：基于 u9-l1 的自定义 Pass 知识，试着写一个小 pass，用 `SatGen` 直接编码一个模块并调用 `ez->solve` 求解一个自定义性质，把本讲的「编码 + 提问」亲手串一遍。
