# 形式验证后端：smt2 / aiger / btor

## 1. 本讲目标

Yosys 的绝大多数后端（`write_verilog`、`write_json`、`write_spice` …）服务于「综合后把网表交给下游工具」。本讲聚焦的是另一类后端——**形式验证（formal verification）后端**，它们的目标不是物理实现，而是把一个 RTLIL 设计翻译成「数学求解器能吃下去」的形式化描述，从而让机器去**证明**关于这个设计的命题（例如「输出永远不会从非零跳到零」「计数器永远不会到 15」）。

学完本讲你应该能够：

- 说清 `write_smt2`、`write_aiger`、`write_btor` 三种后端各自的输出格式、抽象层次与典型下游工具；
- 看懂 `write_smt2` 生成的 SMT-LIB2 片段，能解释一个 `$dff`/`$and` 是怎样被表达成 SMT 公式的；
- 理解「状态 + 转移关系」这一所有形式化后端的共同骨架，以及 SMT、AIG、BTOR 三种格式在表达这副骨架时的取舍；
- 会用 `prep` + `write_smt2` 配合 `yosys-smtbmc` 做一次有界模型检查（BMC）。

## 2. 前置知识

### 2.1 形式验证在干什么

仿真（Verilator / iverilog）是「跑几个测试向量，看看有没有错」。形式验证是「用约束求解器，**穷举所有可能**，证明某个性质**永不**被违反」。前者只能发现 bug，后者（在可解范围内）能证明没有 bug。

Yosys 里形式验证最常见的两种证明手段是：

- **有界模型检查（Bounded Model Checking, BMC）**：展开设计的时序 \( k \) 步，问求解器「前 \( k \) 步内是否存在一条让性质被违反的执行路径」。如果求解器回答 UNSAT，说明前 \( k \) 步内性质一定成立。
- **\( k \)-归纳（k-induction）**：先证「前 \( k \) 步性质成立」（base case），再证「若某连续 \( k \) 步性质都成立，则第 \( k+1 \) 步也成立」（inductive step）。两步都成立则性质对所有步数成立。

这两种手段都离不开一个前提：**把硬件描述成一个状态机**——有一组状态变量，有一组描述「从当前状态到下一状态」的约束，还有一组描述「要证明的性质」的公式。本讲三个后端，本质上都在做同一件事：把 RTLIL 翻译成这种「状态 + 转移 + 性质」的形式，只是输出的「语言」不同。

### 2.2 必要的 RTLIL 回顾

- `$dff`（或门级 `$_DFF_P_`）是寄存器：有时钟、数据输入 `D`、输出 `Q`，代表一个状态位。
- `$and`（或门级 `$_AND_`）是组合逻辑。
- `$assert` / `$assume` / `$cover` 是形式验证专用单元：分别表示「必须成立的断言」「可以假设的前提」「希望被覆盖的状态」。它们只在 `read_verilog -formal` 下从 `assert/assume/cover` 语句生成。
- `prep`（见 u4-l2）是面向形式验证的综合脚本：它保守地保留字级单元（不 techmap、不 abc），并保留 `$mem` 不展开，正适合喂给形式化后端。

### 2.3 三个格式速览

| 后端命令 | 输出格式 | 抽象层次 | 典型下游 |
| --- | --- | --- | --- |
| `write_smt2` | SMT-LIBv2 文本 | 字级（BitVec/Array） | `yosys-smtbmc`、z3、cvc5、yices |
| `write_aiger` | AIGER（二进制/ASCII） | 位级（与门+反相器图） | abc、AIGER 工具链、AVY |
| `write_btor` | BTOR2 文本 | 字级（位向量） | BTOR2 工具链、Boolector、nuXmv |

一句话记忆：**SMT2 和 BTOR 都是「字级」的（一个 8 位加法器仍是一个加法节点），AIGER 是「位级」的（一个 8 位加法器会被拆成一堆二输入与门）；SMT2 把状态机编码成「自由状态变量 + 转移约束公式」，AIGER/BTOR 把状态机编码成格式内置的「寄存器 + next」结构。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [backends/smt2/smt2.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/smt2/smt2.cc) | SMT-LIBv2 后端：`Smt2Worker` 把每个模块翻成 SMT 公式，`Smt2Backend` 注册 `write_smt2` 命令 |
| [backends/aiger/aiger.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/aiger/aiger.cc) | AIGER 后端：`AigerWriter` 把网表归约为 AND/NOT/FF 三件套再序列化，`AigerBackend` 注册 `write_aiger` |
| [backends/btor/btor.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/btor/btor.cc) | BTOR2 后端：`BtorWorker` 把每个单元翻成带编号的 BTOR 行，`BtorBackend` 注册 `write_btor` |
| [backends/smt2/smtbmc.py](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/smt2/smtbmc.py) | 配套求解驱动脚本 `yosys-smtbmc`：读 `write_smt2` 的产物，驱动 SMT 求解器做 BMC / 归纳 |
| [examples/smtbmc/](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/smtbmc) | 一整套可运行的 smtbmc 示例（demo1–demo9），含生成 `.smt2` 与调用 `yosys-smtbmc` 的 `Makefile` |

> 说明：本讲只讲这三个**后端**（RTLIL → 格式）。等价检查（`equiv_*`）和 SAT 编码（`satgen`）属于 u10 的高级内部机制，本讲不展开。

## 4. 核心概念与源码讲解

### 4.1 三个后端的共性：把 RTLIL 翻译成「状态机证明问题」

在进入每个后端之前，先抓住它们的共同骨架。任何时序电路的形式化都回答三个问题：

1. **状态是什么？** —— 哪些信号是寄存器（需要跨周期保持）。
2. **合法状态 / 转移是什么？** —— 在当前状态下，下一拍这些寄存器会变成什么；初始状态又是什么。
3. **要证明的性质是什么？** —— 哪些 `$assert`/`$assume`/`$cover` 必须满足。

三个后端的差别，就在于用什么「语言」表达这三件事：

- **SMT2** 把状态做成一个**不解释的排序** `mod_s`，再用一串 SMT 函数（`mod_t` 描述转移、`mod_i` 描述初值、`mod_a` 描述断言）去约束它。状态之间没有内置「时间」概念，时间是由求解脚本（smtbmc）把若干个状态变量 `s0..sk` 用 `mod_t` 串起来手动构造的。
- **AIGER / BTOR** 是**带时序语义**的格式：寄存器（latch / state）是格式的一等公民，格式本身就知道「寄存器的当前值」和「下一拍值（next）」的关系。求解器直接在这种结构上做展开。

此外，三个后端都继承自统一的 `Backend` 基类（见 u7-l1、u4-l1）：命令名由 `Backend("smt2")` 自动拼成 `write_smt2`，`extra_args` 统一处理文件打开与 `-`/`.gz`，子类只需实现面向输出流的 `execute(f, ...)`。三者还都**在写出前先调用若干预处理 pass** 把设计里的「形式化后端处理不了」的单元换掉——这是理解输出内容的关键，下面分别讲。

---

### 4.2 smt2 后端：把设计编码成 SMT-LIB2 公式

#### 4.2.1 概念说明

`write_smt2` 把每个模块翻译成一段 SMT-LIBv2 文本。它的核心思想是：**用一个抽象排序 `<mod>_s` 表示「该模块的一个完整状态」**，然后围绕这个排序定义一组函数，分别描述「状态的某个字段取什么值」「两个状态是否构成一次合法转移」「初始状态是什么」「断言是否成立」。

这套约定在 `Smt2Backend::help()` 里有完整文档，是理解 smt2 输出的「词汇表」：

- `(declare-sort |<mod>_s| 0)` —— 模块状态排序。
- `|<mod>_n <wire>|` —— 状态字段访问函数：给定一个状态，返回某根线/寄存器/端口的值（单根线返回 `Bool`，多位线返回 `(_ BitVec n)`）。
- `|<mod>_t|` —— 转移函数：`(state, next_state) → Bool`，为真表示这两个状态构成一次合法转移。
- `|<mod>_i|` —— 初始状态谓词：为真表示该状态是合法的初始状态。
- `|<mod>_a|` / `|<mod>_u|` / `|<mod>_c|` —— 所有断言 / 假设 / 覆盖的合取。

> 反直觉点：SMT2 里**没有内置「时钟」或「下一拍」概念**。「下一拍」是用第二个状态变量 `next_state` 配合 `mod_t` 函数表达的（见 4.2.2）。`prep`/`async2sync` 等会把异步逻辑同步化，使每个寄存器都共用一个全局时钟。

#### 4.2.2 核心流程

整个翻译由 `Smt2Worker` 完成，主入口是 `run()`，整体流程：

```text
对模块里每个 wire（端口/寄存器/带 keep 的线）
    生成访问函数 |<mod>_n <wire>|  —— 组合逻辑由 get_bool/get_bv 递归展开
对每个 $assert/$assume/$cover
    收集到 _a / _u / _c 谓词
对每个寄存器 ($dff / $_DFF_)
    在转移向量 trans 里追加：(= D(当前状态)  Q(next_state))
write():
    声明排序 |<mod>_s|
    输出所有字段访问函数与中间 define-fun
    输出转移函数 |<mod>_t| = (and trans...)
    （初值/断言函数类似）
```

最关键的一步是**转移关系的编码**。对一个寄存器，它在「当前状态」下的 `D` 输入（由组合逻辑算出）必须等于它在「下一状态」里的 `Q` 输出：

\[
\text{trans} \;\equiv\; \bigwedge_{\text{每个寄存器 } r} \bigl(\, D_r(\text{state}) = Q_r(\text{next\_state}) \,\bigr)
\]

也就是说，「下一拍寄存器的新值 = 这拍算出来的 D」。求解脚本只需声明若干状态变量 `s0, s1, ..., sk`，逐个 `(assert (mod_t s_i s_{i+1}))`，就把时序展开了。

#### 4.2.3 源码精读

**(1) 状态容器与字段声明。** `Smt2Worker` 持有翻译所需的全部中间结构，最重要的是三个字符串向量：`decls`（声明/中间函数）、`trans`（转移约束）、`hier`（层次约束）：

[backends/smt2/smt2.cc:33-48](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/smt2/smt2.cc#L33-L48) —— `Smt2Worker` 结构体，`decls/trans/hier` 三个向量分别积累三类 SMT 文本。

每个状态字段由 `makebits()` 声明。它会根据模式（默认不解释排序 / `-stbv` 位向量 / `-stdt` 数据类型）生成不同形式的 SMT 声明：

[backends/smt2/smt2.cc:78-116](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/smt2/smt2.cc#L78-L116) —— `makebits()`：默认模式下输出 `(declare-fun |name| (|<mod>_s|) (_ BitVec n))`，即「给定状态返回一个位向量」的访问函数。

**(2) 信号 → SMT 表达式的递归展开。** `get_bool(bit)` / `get_bv(sig)` 把一根信号翻译成 SMT 表达式。如果这根信号由某个 cell 驱动，就递归调用 `export_cell(cell)` 先把这个 cell 翻译成 SMT，再把它的输出替换进去：

[backends/smt2/smt2.cc:329-372](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/smt2/smt2.cc#L329-L372) —— `get_bool` / `get_bv`：常数位直接返回 SMT 常量，被驱动的位先 `export_cell(bit_driver[bit])` 再引用其编号。这就是「组合逻辑在引用时才递归展开、按需求值」的实现。

**(3) 每个 cell 怎么翻成 SMT。** `export_cell()` 是一个按 `cell->type` 分发的大函数，分两类处理：

- **门级单元**（`$_AND_` 等，单 bit）用 `export_gate()` 翻成布尔表达式：
  [backends/smt2/smt2.cc:577-592](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/smt2/smt2.cc#L577-L592) —— `$_AND_ → (and A B)`、`$_NOT_ → (not A)`、`$_MUX_ → (ite S B A)` 等。一行 Verilog 门直接对应一个 SMT 布尔算子。
- **字级单元**（`$and` 等，多位）在 BitVec 模式下用 `export_bvop()` 翻成位向量算子：
  [backends/smt2/smt2.cc:647-649](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/smt2/smt2.cc#L647-L649) —— `$and → (bvand A B)`、`$or → (bvor A B)`、`$xor → (bvxor A B)`。注意 `export_bvop` 内部会做位宽对齐与符号扩展。
  [backends/smt2/smt2.cc:459-498](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/smt2/smt2.cc#L459-L498) —— `export_bvop()`：把模板字符串里的 `A`/`B`/`S` 占位符替换成对应端口的 `get_bv`，需要时用 `((_ extract ...))` 截断到输出位宽。

**(4) 寄存器的转移约束。** 寄存器（`$dff`/`$_DFF_`）被记录到 `registers` 集合，它本身不输出组合表达式，而是在「导出寄存器逻辑」阶段往 `trans` 向量里追加约束：

[backends/smt2/smt2.cc:1203-1217](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/smt2/smt2.cc#L1203-L1217) —— 对每个寄存器，把 `(= D(当前) Q(next_state))` 追加进 `trans`。`get_bool(..., "next_state")` 把 `Q` 解释成「下一状态」里的字段——这正是 4.2.2 里那条转移公式的代码化身。

**(5) 组装输出。** `write()` 把积累好的向量拼成最终文本：先声明状态排序，再吐出所有中间 `define-fun`，最后用 `(and ...)` 把 `trans` 向量包成转移函数 `|<mod>_t|`：

[backends/smt2/smt2.cc:1466-1509](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/smt2/smt2.cc#L1466-L1509) —— `write()`：L1480 声明 `(declare-sort |<mod>_s| 0)`；L1497 定义转移函数 `(define-fun |<mod>_t| ((state ...) (next_state ...)) Bool (and ...))`。注释里那些 `; yosys-smt2-module`、`; yosys-smt2-input` 等就是供 `smtbmc.py` 解析的元数据。

**(6) Backend 注册与预处理。** 命令注册与 u7-l1 完全一致；注意 `execute()` 开头会先跑两个 pass 把形式化后端不直接支持的多路器拆掉：

[backends/smt2/smt2.cc:1597-1598](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/smt2/smt2.cc#L1597-L1598) —— `Smt2Backend() : Backend("smt2", ...)` 注册 `write_smt2`。
[backends/smt2/smt2.cc:1769-1771](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/smt2/smt2.cc#L1769-L1771) —— `execute()` 开头先 `bmuxmap` + `demuxmap`，把 `$bmux`/`$demux` 等拆成 smt2 能直接处理的更基础单元。模块按拓扑序输出，顶层用 `; yosys-smt2-topmod` 标注（见 [smt2.cc:1911-1916](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/smt2/smt2.cc#L1911-L1916)）。

#### 4.2.4 代码实践

**实践目标**：亲手生成一段 SMT-LIB2，看懂 `$dff` 与 `$and` 的翻译结果，并用 `yosys-smtbmc` 跑一次 BMC。

仓库里已经有一个现成的小例子（一个会回环的 4 位计数器，带断言 `counter != 15`）：

[backends/smt2/example.v](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/smt2/example.v) —— 待证明的设计：`counter` 在 0..10 之间循环，断言它永远不等于 15。
[backends/smt2/example.ys](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/smt2/example.ys) —— 配套脚本：`read_verilog -formal` → `prep`-style 流水线（`memory -nordff` 保留寄存器不并入读端口）→ `write_smt2 -wires`。

**操作步骤**（在仓库根目录，假设已构建出 `./yosys`）：

```bash
# 1) 生成 SMT-LIB2
cd backends/smt2
../../yosys -s example.ys        # 产出 example.smt2

# 2) 看里面的关键片段
grep -nE 'declare-sort|define-fun \|(main_t|main_n|main_i|main_a)|bvand|\(and' example.smt2 | head -40
```

**需要观察的现象**：

- 开头应有 `; yosys-smt2-module main` 与 `(declare-sort |main_s| 0)`。
- 转移函数 `(define-fun |main_t| ((state |main_s|) (next_state |main_s|)) Bool (and ... ))`：其内部每一项形如 `(= (|main#..| state) (|main_n counter| next_state))`，即「这拍算出的 next 值 = 下一拍的 counter」——这就是 4.2.2 的转移公式。
- 若计数逻辑里出现了按位与（综合后可能有 `bvand`），对照源码 [smt2.cc:647](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/smt2/smt2.cc#L647) 确认 `$and → (bvand A B)`。

**进阶（可选，需本地有 SMT 求解器）**：用 smtbmc 做有界模型检查。`examples/smtbmc/Makefile` 给出了标准用法：

```bash
cd examples/smtbmc
make demo1      # 内部: yosys-smtbmc --dump-vcd demo1.vcd demo1.smt2  (BMC)
make demo5      # 内部: yosys-smtbmc -g -t 50 ...                     (展开 50 步)
```

> 参见 [examples/smtbmc/Makefile:5-19](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/smtbmc/Makefile#L5-L19) 中 `yosys-smtbmc` 的各标志：`-i` 归纳、`-t N` 展开 N 步、`-s z3` 指定求解器、`-g` 反例模型生成、`--dump-vcd` 导出反例波形。

**预期结果**：计数器在 0..10 循环，断言 `counter != 15` 成立，故 BMC 在有限步内应返回 UNSAT（无可行反例）；若你把 `example.v` 的断言改成 `counter != 5`（一个会被违反的性质），则 smtbmc 会返回 SAT 并给出一条到达 5 的反例波形。**若本地没有 z3/cvc5，以上 smtbmc 运行结果为「待本地验证」。**

#### 4.2.5 小练习与答案

**练习 1**：为什么 `write_smt2` 默认把状态做成一个「不解释的排序」 `|<mod>_s|`，而不是直接用一串 `Bool` 变量？

> **参考答案**：把状态抽象成一个排序，可以让「同一模块的多个状态实例」共用同一套字段访问函数（`|<mod>_n wire|`），只需传入不同的状态变量（`s0, s1, ...`）。这正是 smtbmc 展开时序、做 BMC/归纳的基础。若改成裸 `Bool`，每个时间步都得重新声明一组名字不同的变量，模块也无法被层次化复用。`-stbv` 选项则会把它改成单个大位向量（见 [smt2.cc:1470-1472](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/smt2/smt2.cc#L1470-L1472)）。

**练习 2**：在 `export_cell` 中，`$dff` 和 `$_AND_` 的处理路径有何本质区别？

> **参考答案**：`$_AND_`（组合）在 [smt2.cc:579](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/smt2/smt2.cc#L579) 直接通过 `export_gate` 输出一个 `define-fun` 表达式（同状态内求值）；而 `$dff`（时序）在 [smt2.cc:598-611](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/smt2/smt2.cc#L598-L611) 只是登记为寄存器并声明状态字段，真正表达其语义的「`= D(state) Q(next_state)`」是在稍后 [smt2.cc:1211-1217](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/smt2/smt2.cc#L1211-L1217) 追加到转移向量里的——它跨越「当前/下一」两个状态，这是组合与时序在 SMT2 编码里的根本不同。

---

### 4.3 aiger 后端：把设计压成与门/反相器图

#### 4.3.1 概念说明

AIGER（And-Inverter Graph）是一种**位级**格式：整个设计只能用三种基本元件表达——**输入、二输入与门（AND）、锁存器（latch）**，外加取反通过「文字（literal）的最低位」表达。一个 8 位加法器在这里不是「一个 add」，而是几十个 AND 门的网络。

AIGER 的优势是**极度精简、和 SAT 求解器亲缘最近**：AIG 本身就可以线性时间转成 CNF，因此大量模型检查器（AVY、abc 的 `&` 命令等）直接吃 AIGER。代价是**必须先把设计降到门级**。

Yosys 的 `write_aiger` 接受的设计已经被要求是「展平且只含 `$_AND_`/`$_NOT_`/简单 FF/`$assert`/`$assume`/`$initstate`」——这一点写死在它的帮助文本里：

[backends/aiger/aiger.cc:900-905](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/aiger/aiger.cc#L900-L905) —— 帮助文本明确：设计必须已展平，只允许 `$_AND_`、`$_NOT_`、简单 FF、`$assert`、`$assume`、`$initstate`。`$assert`/`$assume` 会被转成 AIGER 的 bad-state property 与 invariant constraint。

AIGER 文字的编码规则：每个节点有一个变量号 `var ≥ 1`，文字 `lit = 2·var + s`，其中 \(s\in\{0,1\}\) 表示是否取反：

\[
\text{lit} = 2\cdot \text{var} + \text{sign},\qquad \text{sign}=0\text{（正）}/1\text{（反）}
\]

变量号按「输入 → 锁存器 → AND 门」的顺序连续编号，文字 0/1 分别表示常量假/真。

#### 4.3.2 核心流程

`AigerWriter` 的翻译分两阶段：

```text
阶段一（构造函数）：把 RTLIL 单元「归约」成几张映射表
    $_NOT_        → not_map[Y] = A
    $_AND_        → and_map[Y] = (A, B)
    $_DFF_/$_FF_  → ff_map[Q] = D
    $assert       → asserts.push((A, EN))
    $assume       → assumes.push((A, EN))
    （其它单元一律 log_error 不支持）

阶段二（write_aiger）：把映射表编号并序列化
    给输入/锁存器/AND 分配连续变量号
    对每个输出/断言用 bit2aig() 递归求出其 AIG 文字
    写 AIGER 头 + 锁存器表 + 输出表 + AND 门表（二进制用增量编码）
```

关键在于 `bit2aig()`：它把一根任意 RTLIL 信号递归地「追到底」，沿途查 `not_map`/`and_map`/`alias_map`，把整条组合逻辑链压平成 AIG 文字，并 memo 到 `aig_map` 里。

#### 4.3.3 源码精读

**(1) 二进制增量编码。** AIGER 二进制格式用一个手写的变长整数编码器压缩 AND 门表：

[backends/aiger/aiger.cc:29-39](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/aiger/aiger.cc#L29-L39) —— `aiger_encode()`：7 位一组、最高位置 1 表示「还有后续」的经典变长编码，用来紧凑存储 AND 门两条输入文字之间的差值。

**(2) 把 cell 归约成映射表。** 构造函数遍历模块内所有 cell，把每个支持的单元类型登记到对应 map：

[backends/aiger/aiger.cc:222-300](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/aiger/aiger.cc#L222-L300) —— `$_NOT_`→`not_map`、`$_DFF_*`/`$_FF_`→`ff_map`、`$_AND_`→`and_map`、`$initstate`→`initstate_bits`、`$assert`→`asserts`、`$assume`→`assumes`。任何不在白名单里的单元都落到 [aiger.cc:343](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/aiger/aiger.cc#L343) 的 `log_error("Unsupported cell type")`。

**(3) 递归求 AIG 文字。** `bit2aig()` 是核心，用 memoization + 栈做环检测：

[backends/aiger/aiger.cc:80-133](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/aiger/aiger.cc#L80-L133) —— `bit2aig()`：先查 `aig_map` 命中则返回；否则按 `not_map`/`and_map`/`alias_map` 递归。`and_map` 命中时调 `mkgate()` 生成一个新 AND 变量（[aiger.cc:73-78](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/aiger/aiger.cc#L73-L78)），把两条输入递归求出并按「大在前」排序后压栈。`bit2aig_stack` + `next_loop_check` 周期性地检查组合环路（[aiger.cc:88-102](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/aiger/aiger.cc#L88-L102)），有环就报错。

**(4) 锁存器编号与初值。** 锁存器按变量号连续分配，初值有三档：0（复位为 0）、1（复位为 1）、2（任意/未定义）：

[backends/aiger/aiger.cc:396-410](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/aiger/aiger.cc#L396-L410) —— 给每个 FF 分配变量号，`init_map` 里没有的就记 `aig_latchinit=2`（任意），有则记 0/1。`-zinit` 模式会给未初始化 FF 额外加输入 pin（见 [aiger.cc:380-388](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/aiger/aiger.cc#L380-L388)）。

**(5) 序列化头与门表。** `write_aiger()` 输出 AIGER 文件：

[backends/aiger/aiger.cc:543-548](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/aiger/aiger.cc#L543-L548) —— 头行 `aig M I L O A`（二进制）或 `aag ...`（ASCII，`-ascii`）。`M` 最大变量号、`I` 输入数、`L` 锁存器数、`O` 输出数、`A` AND 门数，后四个 `B C J F` 是 bad/constraint/justice/fairness（用于 `$assert`/`$assume`/`$live`/`$fair`）。
[backends/aiger/aiger.cc:603-611](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/aiger/aiger.cc#L603-L611) —— 二进制模式下，AND 门只写两个增量 `delta0 = lhs - rhs0`、`delta1 = rhs0 - rhs1`，并用 `aiger_encode` 压缩，比 ASCII 紧凑得多。

**(6) Backend 注册与前置检查。**

[backends/aiger/aiger.cc:892-893](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/aiger/aiger.cc#L892-L893) —— `AigerBackend() : Backend("aiger", ...)` 注册 `write_aiger`。
[backends/aiger/aiger.cc:1032-1038](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/aiger/aiger.cc#L1032-L1038) —— `execute()` 检查：有顶层、顶层被整选、无残留 process、无残留 memory，才创建 `AigerWriter` 并写出。这说明调用前必须先 `flatten`+`proc`+`techmap` 到门级。

#### 4.3.4 代码实践

**实践目标**：把一个设计降到门级后写出 AIGER，对比 ASCII 与二进制格式，体会「位级归约」。

**操作步骤**：

```bash
# 准备一个最小设计 (与门 + 触发器)
cat > /tmp/dff_and.v <<'EOF'
module top(input clk, input a, input b, output reg q);
  wire y; assign y = a & b;
  always @(posedge clk) q <= y;
endmodule
EOF

# 降到 AIGER 后端能吃的门级，然后分别输出 ASCII / 二进制
./yosys -p "read_verilog /tmp/dff_and.v; hierarchy -top top; proc; opt; techmap; abc -g AND; opt -fast" \
        -p "write_aiger -ascii -map /tmp/dff_and.aig.map /tmp/dff_and.aag" \
        -p "write_aiger -map /tmp/dff_and.bin.map /tmp/dff_and.aig"

head -8 /tmp/dff_and.aag       # 看 ASCII 头: aag M I L O A
xxd /tmp/dff_and.aig | head -3 # 看二进制头与增量编码的字节
```

> 说明：`abc -g AND` 把逻辑映射成只含 AND（+取反）的形式；若 abc 未启用，可改用 `simplemap` 后再 `opt`。具体能否一步到位「待本地验证」，必要时用 `abc`/`aigmap` 等 pass 配合，目标是让网表只含 `$_AND_`/`$_NOT_`/`$_DFF_*`。

**需要观察的现象**：

- ASCII 头 `aag M I L O A` 中 `L=1`（一个 q 触发器）、`A=1`（一个与门 `y=a&b`）、`I=2`（a、b；clk 在同步化后通常由全局时钟替代或体现为边沿）。
- 在 `.aag` 里能看到形如 `7 4 6 1` 的行——`7` 是 AND 门变量号(lit)、`4`/`6` 是两条输入文字（最低位 0/1 区分是否取反）。
- 二进制 `.aig` 文件明显比 ASCII 小，且 AND 门表是一串 `aiger_encode` 字节。

**预期结果**：成功生成 `.aag`（人类可读）与 `.aig`（紧凑二进制），二者描述同一张 AIG。若 `write_aiger` 报 `Unsupported cell type` 或 `unmapped processes`，说明预处理没到位，需要继续 `proc`/`techmap`/`abc`。

#### 4.3.5 小练习与答案

**练习 1**：AIGER 用「文字最低位」表示取反，而不是引入独立的 NOT 节点。这样做有什么好处？

> **参考答案**：取反变成文字的一个比特，是「免费」的——不增加节点数，AIG 规模只由 AND 数决定。这让 AIG 紧凑、归一（同一个函数的多种 AND/NOT 实现可被 `mkgate` 排序去重，见 [aiger.cc:75-77](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/aiger/aiger.cc#L75-L77) 把两条输入按大小排序），也使 SAT 求解器的 CNF 转换非常直接。代价是表达「带复位/使能的复杂触发器」时，必须先用 `techmap` 把它们拆成裸 `$_DFF_` + 逻辑门。

**练习 2**：`write_aiger` 为什么不允许设计里有 `$dff`（只允许 `$_DFF_*`）？

> **参考答案**：`$dff` 是字级、参数化的抽象单元；AIGER 是位级格式，一个 \(n\) 位寄存器必须是 \(n\) 个独立的 latch。`bit2aig`/`ff_map` 只识别单位宽的 `$_DFF_*`/`$_FF_`（[aiger.cc:234-247](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/aiger/aiger.cc#L234-L247)），所以必须先 `techmap`（见 u6-l5）把 `$dff` 拆成 `$_DFF_*` 门。同理 `$anyinit` 也被特殊处理为「单位宽多 latch」（[aiger.cc:249-258](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/aiger/aiger.cc#L249-L258)）。

---

### 4.4 btor 后端：字级位向量格式

#### 4.4.1 概念说明

BTOR2 是一种**字级（word-level）位向量**格式，介于 SMT2 和 AIGER 之间：和 SMT2 一样保留多位运算（一个 8 位加法仍是一个 `add` 节点），但语法比 SMT 简洁得多——每一行就是 `编号 操作 排序 参数...`，且**把时序内建进格式**（`state` + `next`），不像 SMT2 要靠脚本手动串状态。

BTOR2 由 Biere 等人提出（论文引用见源码顶部）：

[backends/btor/btor.cc:20-23](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/btor/btor.cc#L20-L23) —— `[[CITE]]` 标注 BTOR2 论文出处，是理解格式语义的权威参考。

BTOR2 的核心概念：

- **sort**：`<nid> sort bitvec <width>` 声明一个位向量排序，`sort array <addr_sid> <data_sid>` 声明数组（给存储器用）。
- **节点**：每行一个，编号递增。运算节点如 `<nid> and <sid> <a> <b>`、`<nid> add <sid> <a> <b>`。
- **state**：`<nid> state <sid>` 声明一个状态变量（寄存器/存储器）；可用 `<nid> init <sid> <state> <val>` 给初值，用 `<nid> next <sid> <state> <nextval>` 给下一拍值。
- **output / bad / constraint**：分别标记输出、bad-state property（断言取反）、约束（假设）。

#### 4.4.2 核心流程

`BtorWorker` 在构造时一次性把整个模块翻成 BTOR 行。它的设计是「按需展开 + 两遍处理寄存器」：

```text
1. 遍历 wire，建立信号到节点号(bit_nid/sig_nid)的映射
2. 对每个输出端口: 发 output 行
3. 对每个 cell (export_cell): 按类型发对应 BTOR 行
     组合单元 ($add/$and/$eq/...): <nid> <op> <sid> <a> <b>
     $dff/$_DFF_: <nid> state <sid>  + 记入 ff_todo
     $anyconst:   <nid> state <sid>  + <nid> next <sid> <s> <s>  (自指=自由常数)
4. 遍历 ff_todo: 发 <nid> next <sid> <state> <D>  (寄存器下一拍 = D)
5. $assert: 发 not A → and en → bad   ；$assume: 发 constraint
```

与 SMT2 不同，**时序在这里是格式原生语义**：`state` 节点本身就是寄存器，`next` 行直接告诉求解器「下一拍值」，无需额外构造 `(state, next_state)` 谓词。

#### 4.4.3 源码精读

**(1) 排序声明。** 位向量排序按宽度去重缓存，数组排序按 (地址宽,数据宽) 去重：

[backends/btor/btor.cc:206-227](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/btor/btor.cc#L206-L227) —— `get_bv_sid()` 发 `<nid> sort bitvec <width>`；`get_mem_sid()` 发 `<nid> sort array <addr_sid> <data_sid>`。`sorts_bv` / `sorts_mem` 保证同一宽度只声明一次。

**(2) 组合单元翻译。** `export_cell()` 是按类型分发的大函数，二元算术/逻辑运算映射到同名 BTOR 算子：

[backends/btor/btor.cc:253-271](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/btor/btor.cc#L253-L271) —— `$add→add`、`$sub→sub`、`$mul→mul`、`$and→and`、`$or→or`、`$xor→xor`、`$concat→concat`、`$shr→srl`、`$sshr→sra` ……一个字级单元直接对应一行 BTOR。随后的代码用 `get_sig_nid()` 递归求出两个操作数的节点号，再发 `<nid> <op> <sid> <nid_a> <nid_b>`（见 [btor.cc:288-340](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/btor/btor.cc#L288-L340)）。

**(3) 寄存器 → state 节点。** `$dff` 翻成一个 `state` 节点，可选附 `init` 行，并把「待发的 next」记入 `ff_todo`：

[backends/btor/btor.cc:676-746](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/btor/btor.cc#L676-L746) —— 发 `<nid> state <sid>`（[L727-729](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/btor/btor.cc#L727-L729)）；若有 `init` 属性再发 `<nid> init <sid> <state> <val>`（[L736-741](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/btor/btor.cc#L736-L741)）；最后 `ff_todo.push_back((nid, cell))`，把「下拍值=D」留到第二遍发。

[backends/btor/btor.cc:1362-1380](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/btor/btor.cc#L1362-L1380) —— `ff_todo` 循环：对每个寄存器发 `<nid> next <sid> <state> <nid_D>`，即「state 的下一拍 = 它的 D 输入」。这条 `next` 行与 SMT2 里 `trans` 向量里的 `(= D Q_next)` 在语义上完全等价，只是 BTOR 把它做成了一等语法。

**(4) 自由常数。** `$anyconst`（形式验证里的「任意常数」）翻成指向自己的 `state`：

[backends/btor/btor.cc:748-766](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/btor/btor.cc#L748-L766) —— `$anyconst` 发 `<nid> next <sid> <s> <s>`（next 指向自己），表示「初值任意、之后保持不变」的自由常数；`$anyseq` 则只是个 `state`（每拍任意新值）。

**(5) 断言与假设。** `$assert` 取反后作为 bad property，`$assume` 作为 constraint：

[backends/btor/btor.cc:1289-1313](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/btor/btor.cc#L1289-L1313) —— `$assert`：发 `not A`、再 `and en (not A)`，最后 `<nid> bad <...>`。`bad` 节点为真 = 断言被违反 = 求解器要找的反例。`-s` 模式会把所有 bad 折成一棵 OR 树（见 [btor.cc:1442-1458](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/btor/btor.cc#L1442-L1458)）。
[backends/btor/btor.cc:1269-1287](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/btor/btor.cc#L1269-L1287) —— `$assume`：发 `or A (not en)` 后 `<nid> constraint <...>`，约束求解器只在满足假设的状态里搜索。

**(6) 输出与 Backend 注册。**

[backends/btor/btor.cc:1254-1265](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/btor/btor.cc#L1254-L1265) —— 对每个输出端口发 `<nid> output <nid_sig>`。
[backends/btor/btor.cc:1552-1553](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/btor/btor.cc#L1552-L1553) —— `BtorBackend() : Backend("btor", ...)` 注册 `write_btor`。
[backends/btor/btor.cc:1589-1593](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/btor/btor.cc#L1589-L1593) —— 与 smt2 类似，`execute()` 开头先 `bmuxmap`+`demuxmap`+`bwmuxmap` 把 BTOR 不直接支持的多路器拆掉。

#### 4.4.4 代码实践

**实践目标**：生成一份 BTOR2，对照源码确认 `$dff` 变成 `state`+`next`、`$and` 变成 `and` 行。

**操作步骤**：

```bash
# 用一个字级设计：8位寄存器 = a & b
cat > /tmp/btor_demo.v <<'EOF'
module top(input clk, input [7:0] a, input [7:0] b, output reg [7:0] q);
  wire [7:0] y; assign y = a & b;
  always @(posedge clk) q <= y;
endmodule
EOF

./yosys -p "read_verilog /tmp/btor_demo.v; hierarchy -top top; proc; opt; write_btor -v /tmp/btor_demo.btor"
grep -nE 'sort bitvec| state | next | and | output| bad| constraint' /tmp/btor_demo.btor | head -40
```

**需要观察的现象**：

- 多行 `N sort bitvec 8` / `N sort bitvec 1`：8 位数据排序与 1 位（时钟/布尔）排序。
- `N and <sid8> <a> <b>`：`$and` 的字级翻译（对照 [btor.cc:264](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/btor/btor.cc#L264)）。
- `N state <sid8> q`：`q` 寄存器（对照 [btor.cc:727](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/btor/btor.cc#L727)）。
- `M next <sid8> N D`：寄存器下一拍 = D（对照 [btor.cc:1377](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/btor/btor.cc#L1377)）。`-v` 会加上缩进与注释，便于人读。

**预期结果**：得到一段几十行的 BTOR2，能清晰地看到「8 位 and 是一行」而非 AIGER 那样「8 个独立与门」。若想验证语义，可把 `.btor` 喂给 BTOR2 工具（如 `btormc`/Boolector），具体结果「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：同样的 8 位与门，在 AIGER 里和在 BTOR 里分别是什么规模？说明了什么？

> **参考答案**：BTOR 里是一个 `and` 行 + 一个 8 位 sort（约 2 行）；AIGER 里是 8 个独立 AND 门 + 8 个 latch（如带寄存）。说明 BTOR/SMT2 这类**字级**格式在「宽位运算 + 大存储器」上远比 AIGER 紧凑，而 AIGER 的优势在于和 SAT/CNF 的零摩擦衔接。选哪个取决于下游求解器更擅长字级（SMT/BTOR 求解器）还是位级（SAT 求解器）。

**练习 2**：BTOR 用 `next` 行表达「下拍值」，SMT2 用 `mod_t` 谓词表达「合法转移」。这两种表达在能力上等价吗？

> **参考答案**：在这个后端的用法下等价——`next` 行 `<n> next <sid> <state> <D>` 表达的就是「state 的下拍值由函数 D 给定」，等价于 SMT2 里 `mod_t` 中的 `(= D(state) Q(next_state))`。差别是表达风格：BTOR 把时序做成格式的原生结构（求解器直接按状态机语义展开），SMT2 把它编码成普通的一阶公式（由 smtbmc 脚本负责声明多个状态变量并用 `mod_t` 串联）。

---

## 5. 综合实践

把三个后端串起来做一次对比实验，体会「同一个设计、三种编码」的差异。

**任务**：对同一个 4 位计数器（用 [backends/smt2/example.v](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/smt2/example.v)），分别用 `prep`+`write_smt2`、`flatten`+门级+`write_aiger`、`prep`+`write_btor` 产出三种格式，然后回答：

1. 在 `write_smt2` 输出里，找到 `|main_t|` 转移函数，数一下它由几条 `(= ... ...)` 组成；它们分别对应哪些寄存器？
2. 在 `write_btor` 输出里，找到所有 `state` 行与对应 `next` 行，确认「state 数 == next 数 == 寄存器数」。
3. 在 `write_aiger`（ASCII）输出里，读 `aag` 头行的 `I/L/A` 三个数字，解释它们为什么远大于 BTOR 里的「一个 state」（提示：AIGER 是位级，4 位计数器 = 4 个 latch + 若干 AND）。
4. 把 `example.v` 的断言从 `counter != 15` 改成 `counter != 5`，重新跑 smtbmc（若本地有求解器），观察是否从 UNSAT 变成 SAT 并给出反例。

**参考脚本骨架**：

```bash
# SMT2
yosys -p 'read_verilog -formal backends/smt2/example.v; prep -top main -nordff; write_smt2 -wires /tmp/c.smt2'
# BTOR
yosys -p 'read_verilog -formal backends/smt2/example.v; prep -top main -nordff; write_btor -v /tmp/c.btor'
# AIGER (需先降到 $_AND_/$_NOT_/$_DFF_)
yosys -p 'read_verilog backends/smt2/example.v; hierarchy -top main; proc; opt; techmap; opt -fast; write_aiger -ascii /tmp/c.aag'
```

**预期收获**：你会直观看到——同一份 RTLIL，字级格式（SMT2/BTOR）里寄存器是「整体」，位级格式（AIGER）里被拆成单 bit 的 latch；而「转移关系」在 SMT2 是一个布尔谓词 `mod_t`、在 BTOR 是 `next` 行、在 AIGER 是 latch 的输入连到组合逻辑。这正是三种形式化后端「同骨架、不同语言」的全部精髓。

## 6. 本讲小结

- 三个后端都把 RTLIL 编码成「**状态 + 转移关系 + 性质**」的状态机证明问题，差别只在表达语言与抽象层次。
- **`write_smt2`** 用一个不解释排序 `|<mod>_s|` 表示状态，组合逻辑递归展开成 SMT 表达式（`$_AND_→(and A B)`、`$and→(bvand A B)`），时序由转移谓词 `|<mod>_t|` 中的 `(= D(state) Q(next_state))` 表达；时间需由 `yosys-smtbmc` 手动展开。
- **`write_aiger`** 把设计归约成 AND/NOT/Latch 三件套（`not_map`/`and_map`/`ff_map`），用 `bit2aig()` 递归求 AIG 文字；位级、与 SAT 亲缘最近，但要求设计先降到 `$_AND_`/`$_NOT_`/`$_DFF_*` 门级。
- **`write_btor`** 输出字级 BTOR2，每个单元一行（`$add→add`、`$and→and`），寄存器是原生 `state` 节点 + `next` 行，`$assert→bad`、`$assume→constraint`，时序是格式内建语义。
- 三者都继承统一的 `Backend` 基类、用 `extra_args` 处理文件，且都在写出前跑 `bmuxmap`/`demuxmap`（BTOR 还多一个 `bwmuxmap`）等预处理 pass。
- 选型经验：字级、宽位运算/存储器多用 SMT2 或 BTOR；要与 SAT 求解器/AIGER 工具链衔接用 AIGER；SMT2 生态最广（z3/cvc5/yices），BTOR2 与 Boolector/nuXmv 配合好。

## 7. 下一步学习建议

- **等价检查**：阅读 `passes/equiv/`（`equiv_make`/`equiv_simple`/`equiv_status`），它用一个 SAT 求解器比对两份网表，与本章的形式化输出共享底层 SAT 能力——这会引出 u10-l1 的 `satgen`。
- **SAT 编码底层**：进入 `kernel/satgen.cc`，看一个 `$and`/`$dff` 是如何被编码成 CNF 子句的；理解了它，再回头看 `write_aiger` 的 AIG→CNF 就很自然（见 u10-l1）。
- **functional IR / smtlib**：`backends/functional/` 提供了另一条 SMT 生成路径（`write_functional_smt2`），它走函数式 IR 而非直接遍历 RTLIL，适合对比两种代码生成思路（见 u10-l2）。
- **实践深化**：把 `examples/smtbmc/` 的 demo1–demo9 全跑一遍，对照各自的 `.v`、生成的 `.smt2` 与 smtbmc 的不同标志（`-i` 归纳、`-t N` 步数、`-s solver`），建立对 BMC 与 \(k\)-归纳的实操直觉。
