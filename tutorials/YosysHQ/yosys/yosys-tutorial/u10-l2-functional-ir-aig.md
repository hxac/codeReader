# functional IR 与 AIG 表示

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 **为什么 RTLIL 之外还要再有两种「更窄」的中间表示**：字级的 functional IR 与位级的 AIG。
- 读懂 `Functional::IR` 的数据结构，并能跟踪 `from_module` 把一个 RTLIL 模块翻译成「`(inputs, current_state) -> (outputs, next_state)` 纯函数」的过程。
- 读懂 `cellaigs`，能指出 `$and / $or / $not / $mux` 分别走哪条路径被压平成「与门 + 反相器」图。
- 准确说出这两种表示与 `abc`、`aiger`、`sat`/形式验证之间的衔接关系（以及哪些是直接消费、哪些只是「共同语言」）。

本讲依赖 [u3-l4 内部单元库](u3-l4-internal-cell-library.md)（你必须先认识 `$and / $mux / $dff` 与 `$_AND_` 这两套单元），并承接 [u10-l1 SAT 与形式验证机制](u10-l1-sat-formal-verification.md) 中关于「门级网表如何被编码」的讨论。

---

## 2. 前置知识

### 2.1 抽象层次：同一段逻辑的不同「分辨率」

一条 `assign y = a + b;` 在 Yosys 内部可以被表示成好几种形态，由粗到细大致是：

| 层次 | 表示 | 典型消费者 |
|---|---|---|
| 行为级 | RTLIL `Process` / `$add` | 前端、综合 pass |
| **字级函数式** | **functional IR（`add` 节点）** | SMT / Rosette / C++ 符号后端 |
| **位级门级** | **AIG（与门 + 反相器图）** | abc 优化映射、aiger、SAT |
| 布尔公式 | CNF 子句 | SAT 求解器 |

本讲的两位主角就是中间两层。它们都是「把 RTLIL 进一步窄化，方便特定下游工具」的产物：functional IR 把整个模块压成一个**纯函数**，AIG 把单个单元的布尔函数压成**只有两种原语**的图。

### 2.2 两个关键术语

- **SSA（Static Single Assignment，静态单赋值）**：每个变量只被赋值一次。好处是「变量名」与「它的定义」一一对应，做数据流分析、翻译成函数式语言都很省心。functional IR 就是 SSA 风格的。
- **计算图（compute graph）**：节点是运算，边是「谁是谁的输入」。functional IR 内部用 `ComputeGraph` 模板（见 [kernel/compute_graph.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/compute_graph.h)）存这张图，并自带**去重**：两个完全相同的节点会被合并成一个。

### 2.3 AIG 与「反相器折叠」

AIG = And-Inverter Graph（与门/反相器图）。它的核心思想来自一个布尔代数事实：**任何布尔函数都能只用「二输入与门」和「取反（非）」两种元件实现**。例如「或」可以用德摩根定律凑出来：

\[
a \lor b = \neg(\neg a \land \neg b)
\]

AIG 里「非」不是一个独立节点，而是挂在「边」上的一个布尔标记（`inverter` 位）。这叫**反相器折叠（inverter folding）**，它让图更小、更容易做结构哈希去重——两个「逻辑功能相同、只是某些边上反相次数不同」的子图更容易被识别为等价。

### 2.4 为什么不用 RTLIL 一统天下

RTLIL 是为「综合变换」设计的：它要支持多驱动、`process`、参数化、模块层次，结构非常丰富。但「丰富」对下游某些工具反而是负担：

- 写一个 SMT-LIB 后端时，你不想反复处理「位宽不够要补零扩展」「这个信号被多个 always 驱动」「这条线悬空」这类 RTLIL 特有问题——你只想把设计当成一个数学函数翻译过去。这正是 functional IR 的价值。
- 调 abc 做逻辑优化时，abc 只认 AIG。你必须先把 `$or / $xor / $mux / $aoi3` 这些五花八门的门统一压成 AND/NOT，abc 才能干活。这正是 AIG 的价值。

所以这三种表示是「分工」而非「替代」：RTLIL 是综合期主场，functional IR 与 AIG 是面向特定下游的「出口匝道」。

---

## 3. 本讲源码地图

| 文件 | 职责 |
|---|---|
| [kernel/functional.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/functional.h) | functional IR 的**类型定义**：`Fn` 枚举（指令集）、`Sort`（类型）、`IR`（整个设计）、`Node`（节点引用）、`Factory`（构造器，带不变量校验）、Visitor 基类 |
| [kernel/functional.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/functional.cc) | `from_module`（RTLIL→functional IR 的入口）、`handle()`（逐 cell 类型分发翻译）、拓扑排序、死代码删除 |
| [kernel/cellaigs.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/cellaigs.h) | `AigNode` / `Aig` 结构定义 |
| [kernel/cellaigs.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/cellaigs.cc) | `AigMaker`（逐门转 AIG 的原语）+ `Aig::Aig(Cell*)`（按 cell 类型分发）+ `optimize`（死节点消除） |

关联文件（消费者/示例）：

- [passes/techmap/aigmap.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/aigmap.cc)：`aigmap` pass，**`Aig` 最典型的消费者**，把任意组合单元降级为 `$_AND_` + `$_NOT_`。
- [docs/source/code_examples/functional/dummy.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/code_examples/functional/dummy.cc)：最小 functional 后端示例，演示如何遍历 IR。
- [backends/functional/smtlib.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/functional/smtlib.cc)：真实的 functional 后端（`write_functional_smt2`）。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**functional IR**、**AIG（cellaigs）**、**与 abc/sat 的衔接**。

### 4.1 functional IR：把模块当成一个纯函数

#### 4.1.1 概念说明

functional IR 把一个模块建模成一个数学上的纯函数：

\[
(\text{inputs},\ \text{current\_state}) \;\mapsto\; (\text{outputs},\ \text{next\_state})
\]

它有几个关键性质：

1. **SSA + 计算图**：每个中间值只赋值一次，整张图拓扑有序（定义先于使用）。这一点和 RTLIL 截然不同——RTLIL 的 wire 可以被反复驱动、process 可以嵌套。
2. **显式拆分**：RTLIL 里一个 `$add` cell「隐含」了「先把两路输入扩展到等宽再相加」这件事；functional IR 会把这件事**显式**拆成 `zero_extend / sign_extend` 节点 + `add` 节点。复杂操作被拆成多步，每步都是一个简单运算。
3. **去重**：底层 `ComputeGraph` 会对「函数 + 参数」完全相同的节点自动合并（`NodeData` 专门设计了 `hash_into` 与 `operator==`，见 [kernel/functional.h:228-235](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/functional.h#L228-L235)）。
4. **设计动机**：SMT-LIB、Rosette（Racket）、C++ 这些目标都是「函数式」语言，functional IR 与它们近乎同构，写后端时几乎是逐节点一对一翻译，不必反复处理 RTLIL 的位宽/多驱动等杂事。

#### 4.1.2 核心流程

把 RTLIL 模块变成 functional IR 的总入口是 `IR::from_module(module)`，其内部由 `FunctionalIRConstruction` 驱动，流程如下：

```
from_module(module):
    1. 建 IR 与 Factory
    2. FunctionalIRConstruction:
       a. driver_map.add(module)        # 用 DriverMap 把"端口连接"整理成"信号 -> 驱动表达式"
       b. 登记所有 input / output / state（寄存器、存储器各一对 current/next）
       c. 把 output.value 与 state.next_value 反向 enqueue（worklist）
    3. process_queue():                 # 工作队列
       取出一个待处理目标（一段被驱动的信号 或 一个 cell）
         - 若是信号：顺着 driver_map 找到驱动它的 cell/常数，递归 enqueue 驱动的输入
         - 若是 cell：调 handle() 把它翻译成一串 functional 节点
       Memoization：相同 DriveSpec 复用同一个 Node（SSA 去重）
    4. topological_sort()               # 拓扑排序，顺便检测组合环路、删除死代码
    5. forward_buf()                    # 坍缩占位用的 buf 节点
```

两点要点：

- **反向驱动 + worklist**：从「模块输出」和「next-state」出发，反向追溯「这个值由谁算出来」，按需把需要的子表达式加入队列。没被任何输出/状态用到的代码就是死代码，不会被生成（或在拓扑排序阶段被 `permute` 删掉）。
- **占位 buf 节点**：因为 SSA 要求「先用后定义」会很麻烦，构造期会先 `create_pending` 一个 `buf` 占位节点，等它的真正定义算好后用 `update_pending` 回填（见 [kernel/functional.h:545-552](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/functional.h#L545-L552)）；`forward_buf` 最后把这些占位节点坍缩掉。

#### 4.1.3 源码精读

**(1) `Fn` 枚举——functional IR 的「指令集」**

[kernel/functional.h:52-135](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/functional.h#L52-L135) 列出了所有节点函数，每条都带一段伪代码注释说明语义。摘几条：

```cpp
// add(a: bit[N], b: bit[N]): bit[N] = a + b
add,
// mux(a: bit[N], b: bit[N], s: bit[1]): bit[N] = s ? b : a
mux,
// memory_read(memory, addr): bit[data_width] = memory[addr]
memory_read,
```

注意它与 RTLIL 内部单元的对应但又更「纯」：这里没有 `A_SIGNED` 参数，有符号/无符号由拆出来的 `sign_extend`/`zero_extend` 节点表达。

**(2) `Sort`——节点的类型**

[kernel/functional.h:140-155](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/functional.h#L140-L155)：只有两种 sort——`bit[n]`（位向量）和 `memory[n,m]`（不可变数组），用 `std::variant<int, pair<int,int>>` 区分。

**(3) `Factory`——带不变量校验的构造器**

[kernel/functional.h:451-577](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/functional.h#L451-L577)。每个构造方法都会校验位宽不变量，例如二元运算要求两操作数同 sort：

```cpp
void check_basic_binary(Node const &a, Node const &b) { log_assert(a.sort().is_signal() && a.sort() == b.sort()); }
Node bitwise_and(Node a, Node b) { check_basic_binary(a, b); return add(Fn::bitwise_and, a.sort(), {a, b}); }
```

这意味着「位宽不对齐」在 functional IR 里是构造不出来的——RTLIL 那种「`$and` 两输入宽度不同、靠 `Y_WIDTH` 隐式扩展」的宽松必须在翻译时显式插入 `extend`。

**(4) `handle()`——RTLIL cell 如何被拆成多个节点**

这是「RTLIL → functional IR」的翻译核心。[kernel/functional.cc:256-275](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/functional.cc#L256-L275) 处理一组二元运算，最能体现「一个 cell 拆成多步」：

```cpp
if(cellType.in(ID($add), ID($sub), ID($and), ID($or), ID($xor), ID($xnor), ID($mul))){
    bool is_signed = a_signed && b_signed;
    Node a = factory.extend(inputs.at(ID(A)), y_width, is_signed);  // 显式扩展
    Node b = factory.extend(inputs.at(ID(B)), y_width, is_signed);
    if(cellType == ID($add))      return factory.add(a, b);
    ...
    else if(cellType == ID($and)) return factory.bitwise_and(a, b);
    ...
}
```

注意 `$add` 在 RTLIL 里只是「一个 cell」，到这里被显式拆成 `extend(A) + extend(B) + add`。这正是 functional IR「更接近数学函数」的体现。

**(5) `enqueue`——SSA 去重的关键**

[kernel/functional.cc:480-490](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/functional.cc#L480-L490)：同一段 `DriveSpec`（被驱动的信号描述）第二次出现时直接返回已存在的 Node，从而天然实现公共子表达式消除。

**(6) `from_module` 与排序**

[kernel/functional.cc:726-734](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/functional.cc#L726-L734) 是入口；[kernel/functional.cc:736-763](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/functional.cc#L736-L763) 的 `topological_sort` 用 SCC（强连通分量）算法排序，若发现 SCC 多于一个节点就判定为**组合环路**并报错（functional IR 不支持组合反馈）。

**(7) 前置条件**

[kernel/functional.cc:557-575](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/functional.cc#L557-L575) 的注释明确：使用 functional 后端前必须先跑 `async2sync` 或 `clk2fflogic`，使所有寄存器/存储器端口都变成「单一时钟的同步语义」——因为 functional IR 把时序统一表达成 `state` 的 current/next 对，无法表达异步/多时钟的原始形式。

#### 4.1.4 代码实践

**实践目标**：亲眼看到一个 RTLIL 模块被翻译成 functional IR 后长什么样。

**操作步骤**（运行型实践）：

1. 准备一个小设计 `tiny.v`：

```verilog
// 示例代码
module tiny(input [3:0] a, b, input s, output [3:0] y);
    assign y = s ? (a + b) : (a & b);
endmodule
```

2. 用 `prep`（保守综合）+ `clk2fflogic`（满足 functional 后端前置条件）+ `write_functional_smt2` 导出字级 IR：

```
# 示例脚本
read_verilog tiny.v
prep
write_functional_smt2 tiny.smt2
```

   > 命令名 `write_functional_smt2` 来自 [backends/functional/smtlib.cc:276](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/functional/smtlib.cc#L276)；该后端把 functional IR 翻译成 SMT-LIB。

3. 打开 `tiny.smt2`，定位到转移函数定义体。

**需要观察的现象**：

- 你应当能在输出里看到 `(add ...)`、`(bvand ...)`、选择（ite/mux 语义）这些字级构造，**而不是**逐位的 `$_AND_` 门——因为 functional IR 是字级的。
- `a + b` 不会「消失在某个 cell 里」，而是显式出现一个加法运算；如果位宽需要扩展，还会有对应的 zero/sign extend。

**预期结果**：转移函数体里出现与 `s ? (a+b) : (a&b)` 同构的字级表达式。若你的 yosys 构建未编入 functional 后端，可改为**源码阅读型实践**：对照 [dummy.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/code_examples/functional/dummy.cc) 说明 `for(auto node : ir)` 遍历顺序为何是拓扑序。具体能否运行 `write_functional_smt2` **待本地验证**（取决于构建选项）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 RTLIL 里一个 `$add` cell，到了 functional IR 会变成「`extend + extend + add`」三个节点？

> **答案**：RTLIL 允许 `$add` 的 A、B、Y 宽度不一致，靠参数隐式扩展；functional IR 不允许「构造时位宽不对齐」（`check_basic_binary` 会断言失败），因此必须把隐式扩展显式拆成 `extend` 节点，再接一个等宽的 `add`。这让 IR 更接近数学函数，也便于后端逐节点翻译。

**练习 2**：functional IR 为什么不能用拓扑排序表达「组合反馈」（比如一个 RS 锁存器）？

> **答案**：functional IR 是 SSA 计算图，要求节点可拓扑排序（定义先于使用）。组合反馈会形成强连通分量，`topological_sort` 检测到后直接 `log_error`（见 [functional.cc:761](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/functional.cc#L761)）。要把含反馈的设计送进 functional 后端，需先用 `scc -select; simplemap; select -clear` 等手段消解。

---

### 4.2 AIG（cellaigs）：与门/反相器图

#### 4.2.1 概念说明

`cellaigs` 解决的问题是：**把任意一个组合单元的布尔函数，压平成只用「二输入与门 + 反相器」两种元件的图**。

它的设计有三个关键点：

1. **反相器折叠**：「非」不是独立节点，而是每个节点上的一个 `inverter` 布尔位（见 `AigNode`）。取反一次就把这个位翻一下，不增加节点数。
2. **结构哈希去重**：建图时，`AigNode` 提供了 `operator==` 与 `hash_into`（[cellaigs.h:32-50](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/cellaigs.h#L32-L50)），配合 `idict<AigNode>` 实现「相同子图只存一份」。两个功能相同的 AND 子图不会重复出现。
3. **常数折叠**：建与门时即时化简，`a & a = a`、`a & ~a = 0`、`a & 1 = a`、`a & 0 = 0`，让图在构造过程中就保持最小。

注意：`Aig` 是**对单个 cell** 构造的（`Aig(Cell *cell)`），不是对整个模块。它把「这一个门的功能」展开成一个微型的 AIG。

#### 4.2.2 核心流程

```
Aig::Aig(cell):
    若 cell->type[0] != '$'：直接返回（不处理厂商原语/黑盒）
    建 AigMaker mk（提供原语：not_gate / and_gate / or_gate / xor_gate / mux_gate / adder ...）
    按 cell->type 分发：
      $not/$pos/$buf      -> not_gate 或直通
      $and/$or/$xor/...   -> 对应二元门（底层都靠 and_gate + not_gate 合成）
      $mux/$_MUX_/$_NMUX_ -> mux_gate
      $reduce_*            -> 一串 and/or/xor 归约
      $add/$sub            -> 逐位全加器 adder()
      $eq/$ne              -> 逐位异或后归约
      $lt/$gt/$le/$ge      -> 用减法的进位判断
      ...（AOI3/OAI3/AOI4/OAI4 等复合门）
    :optimize  从输出端口反向标记可达节点，删除不可达中间节点
```

最关键的一句：**所有「非与门」的运算（or/xor/nor/mux/...）最终都靠 `and_gate` + `not_gate` 两种原语合成**。这就是「统一表示」的来源。

#### 4.2.3 源码精读

**(1) `AigNode`——AIG 的唯一节点类型**

[kernel/cellaigs.h:27-38](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/cellaigs.h#L27-L38)：

```cpp
struct AigNode {
    IdString portname; int portbit;     // 引用某个输入端口的第几位（叶子）
    bool inverter;                       // 反相器标记（折叠在节点上）
    int left_parent, right_parent;       // 与门的两个输入节点下标（内部节点）
    vector<pair<IdString, int>> outports;// 这个节点驱动哪些输出位
    ...
};
```

一个节点同时承担三种角色：①叶子（`portbit >= 0`，引用输入位）；②常数（`portbit < 0` 且两个 parent 都 `< 0`，由 `inverter` 决定是 0 还是 1）；③与门（两个 parent 指向子节点）。`Aig` 就是 `vector<AigNode>` 加一个名字（[cellaigs.h:40-48](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/cellaigs.h#L40-L48)）。

**(2) `not_gate` 与 `and_gate`——仅有的两种原语**

`not_gate`（[cellaigs.cc:122-128](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/cellaigs.cc#L122-L128)）只是翻转 `inverter` 位，不新建节点。

`and_gate`（[cellaigs.cc:130-172](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/cellaigs.cc#L130-L172)）是整个 AIG 的基石，集中体现了常数折叠：

```cpp
int and_gate(int A, int B, bool inverter = false) {
    if (A == B)           return inverter ? not_gate(A) : A;     // a & a
    ...
    if (nA == nB_inv)     return bool_node(inverter);             // a & ~a => 0
    if (nA_bool && nB_bool) ...                                    // 常量 & 常量
    if (nA_bool) ...        // a & 1 / a & 0
    if (nB_bool) ...
    // 一般情况：新建一个与门节点
    AigNode node; node.inverter = inverter;
    node.left_parent = A; node.right_parent = B;
    return node2index(node);
}
```

**(3) `or_gate`——德摩根合成的范例**

[cellaigs.cc:179-184](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/cellaigs.cc#L179-L184)：

```cpp
int or_gate(int A, int B) {
    int not_a = not_gate(A);
    int not_b = not_gate(B);
    return nand_gate(not_a, not_b);   // = ~(~a & ~b) = a | b
}
```

`xor`、`nor`、`xnor`、`andnot`、`ornot` 同理，全部建立在 `and_gate`+`not_gate` 之上。

**(4) `mux_gate`——多路选择器如何变 AIG**

[cellaigs.cc:219-225](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/cellaigs.cc#L219-L225)：

```cpp
int mux_gate(int A, int B, int S) {
    int not_s = not_gate(S);
    int a_active = and_gate(A, not_s);   // a 生效：a & ~s
    int b_active = and_gate(B, S);       // b 生效：b & s
    return or_gate(a_active, b_active);  // 两者取或
}
```

即 \(\text{mux}(a,b,s) = (a \land \neg s) \lor (b \land s)\)。一个 `$mux` 在 AIG 里就是 1 个非门 + 2 个与门 + 1 个或门（而那个或门又展开成 3 个与非/与门）。

**(5) 分发表——`$and/$or/$not/$mux` 各走哪条路**

- `$not / $_NOT_` 走 [cellaigs.cc:305-313](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/cellaigs.cc#L305-L313)：逐位 `not_gate`。
- `$and / $or / $xor / $xnor / $_AND_ / $_OR_ ...` 走 [cellaigs.cc:315-331](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/cellaigs.cc#L315-L331)：逐位调对应门，由一个三目运算链选出门类型：
  ```cpp
  int Y = cell->type.in(ID($and), ID($_AND_))  ? mk.and_gate(A, B) :
          cell->type.in(ID($or),  ID($_OR_))    ? mk.or_gate(A, B)  :
          cell->type.in(ID($xor), ID($_XOR_))   ? mk.xor_gate(A, B) : ...;
  ```
- `$mux / $_MUX_ / $_NMUX_` 走 [cellaigs.cc:333-345](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/cellaigs.cc#L333-L345)：调 `mux_gate`，`$_NMUX_` 额外取反。

**(6) `node2index`——规范化去重**

[cellaigs.cc:71-85](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/cellaigs.cc#L71-L85)：建与门前先把 `left/right` 排成升序（利用与门交换律 `a&b == b&a`），再用 `idict` 查重，命中就复用既有下标。这就是结构哈希。

**(7) `optimize`——死节点消除**

[cellaigs.cc:500-527](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/cellaigs.cc#L500-L527)：从带 `outports` 的节点反向追溯 `left/right_parent`，标记所有可达节点，把没被任何输出用到的中间节点删掉，并重排下标。

#### 4.2.4 代码实践

**实践目标**（对应本讲核心任务）：在 `cellaigs.cc` 中找到 `$and/$or/$not/$mux` 转 AIG 的逻辑，说明 AIG 如何用统一的原语表示这些门。

**操作步骤**（源码阅读型，必做）：

1. 打开 [kernel/cellaigs.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/cellaigs.cc)。
2. 定位 `and_gate`（第 130 行）与 `not_gate`（第 122 行），确认它们是仅有的两种「真原语」。
3. 跟踪 `$or`：进入第 315-331 行的分发，看到 `$or` 调 `mk.or_gate`；再跳到 `or_gate`（第 179 行），看到它 = `nand_gate(not a, not b)`，而 `nand_gate` = `and_gate(..., true)`。于是 `$or` 的 AIG = `not→not→and(反相)`。
4. 跟踪 `$mux`：进入第 333-345 行，看到 `mux_gate`（第 219 行）= `or(and(A,~S), and(B,S))`，逐层展开回 `and_gate`/`not_gate`。
5. 画一张小表，列出每个门最终用了几个 `and_gate`、几个 `not_gate`。

**可选运行验证**（运行型实践，待本地验证）：

```
# 示例脚本：观察 aigmap 把高层门降级为 $_AND_ + $_NOT_
read_verilog tiny.v          # 含 a|b、s? 的逻辑
synth -flatten
aigmap
write_rtlil after_aigmap.il
```

**需要观察的现象**：执行 `aigmap` 后，原来的 `$or`/`$mux` 在 RTLIL 中应被替换为 `$_AND_` 与 `$_NOT_` 单元（可能还有 `$_NAND_`，取决于 `aigmap -nand_mode`），数量与你在步骤 5 画出的表一致。

**预期结果**：`aigmap` 后用 `select -list` 或 `stat` 看到组合逻辑只剩 `$_AND_`/`$_NOT_`（及可能的 `$_NAND_`）这一类与门/反相器原语，证明「统一表示」在 RTLIL 层面落地。具体替换细节**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `inverter` 用节点上的一个 `bool` 标记，而不是做成独立的「非门节点」？

> **答案**：把反相器「折叠」到节点/边上，能显著减小图的规模（一次取反不新增节点），并让结构哈希更有效——`a` 与 `~a` 共享同一节点、仅 `inverter` 位不同，功能等价的子图更容易被识别和去重。这也是 AIG 相对普通门级网表在 abc/SAT 中更高效的原因之一。

**练习 2**：`or_gate` 为什么不直接建一个「或门节点」，而要用德摩根定律凑出来？

> **答案**：因为 AIG 的定义就只有「与门 + 反相器」两种原语，没有「或门节点」这种东西。坚持只用 `and_gate`/`not_gate` 合成，才能保证整张图对只认 AIG 的下游工具（abc、aiger）是合法且统一的；同时也自动享受到 `and_gate` 内置的常数折叠与去重。

---

### 4.3 与 abc / sat 的衔接

#### 4.3.1 概念说明

两种表示面向不同的下游生态：

- **functional IR（字级）** → `write_functional_smt2` / `write_functional_rosette` / `write_functional_cxx` 等后端，产出 SMT-LIB、Racket/Rosette、C++ 等符号化目标代码，供 SMT 求解器或符号执行框架消费。
- **AIG（位级）** → `abc`（外部综合/优化工具 `yosys-abc`）与 `aiger` 格式，后者再喂给 SAT / BMC 求解器。

> **准确性说明（重要）**：本仓库里 `cellaigs::Aig` 这个 C++ 结构的直接 `#include` 消费者其实只有三处——`aigmap`、`timeest`、`json` 后端（可用 `grep '#include "kernel/cellaigs.h"'` 验证）。`abc9` 与 `satgen` **并不直接**使用 `cellaigs::Aig`：`abc9` 有自己的 AIG 切片/序列化机制，`satgen` 直接把 RTLIL 编码成 CNF（见 [u10-l1](u10-l1-sat-formal-verification.md)）；连较新的 `backends/aiger2` 都在注释里写了「TODO: decide how to unify this with cellaigs」（[backends/aiger2/aiger.cc:24](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/aiger2/aiger.cc#L24)）。
>
> 因此更准确的说法是：**「AIG」作为一种位级表示，是 abc 与 aiger/SAT 生态的共同语言**；`cellaigs::Aig` 是 Yosys 内部把任意组合单元压成 AIG 的一种实现，`aigmap` 是它的主消费者。不要误以为 `sat`/`equiv` 命令内部调用了 `cellaigs`。

#### 4.3.2 核心流程

```
RTLIL cell
   │
   ├──(cellaigs)──> Aig ──(aigmap 消费)──> RTLIL 里只剩 $_AND_ / $_NOT_ / $_NAND_
   │                                         │
   │                                         └──(可选)──> abc9 / aiger ──> SAT/BMC
   │
   └──(functional::from_module)──> Functional::IR ──> write_functional_smt2 / rosette / cxx
                                                       │
                                                       └──> SMT 求解器 / Rosette 符号执行

注：sat/equiv 命令走另一条路：RTLIL ──(satgen)──> CNF ──> SAT，不经过 cellaigs。
```

#### 4.3.3 源码精读

**(1) `aigmap` 如何消费 `Aig`**

[passes/techmap/aigmap.cc:78-117](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/aigmap.cc#L78-L117) 是「AIG 回写 RTLIL」的范本。对每个选中 cell 先 `Aig aig(cell)`，再遍历 `aig.nodes`：

```cpp
if (node.portbit >= 0) {
    bit = cell->getPort(node.portname)[node.portbit];   // 叶子：直接取输入位
} else if (node.left_parent < 0 && node.right_parent < 0) {
    bit = node.inverter ? State::S1 : State::S0;        // 常数 0/1
} else {
    SigBit A = sigs.at(node.left_parent);
    SigBit B = sigs.at(node.right_parent);
    ... // 建 $_AND_（或 nand_mode 下的 $_NAND_）
}
if (node.inverter) { ... 建 $_NOT_ ... }                 // 反相器折叠回写为独立非门
```

注意一个细节：AIG 里「折叠在节点上的 `inverter`」，回写到 RTLIL 时被还原成独立的 `$_NOT_` 单元——因为 RTLIL 的 `$_AND_` 没有内置反相位。

**(2) functional 后端如何消费 `Functional::IR`**

[dummy.cc:22-39](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/code_examples/functional/dummy.cc#L22-L39) 是最小范本：`from_module` 拿到 IR，`for(auto node : ir)` 按拓扑序遍历每个节点，用 `node.to_string()` 输出其函数与参数，再单独处理 outputs 与 states。真实的 `write_functional_smt2`（[backends/functional/smtlib.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/functional/smtlib.cc)）只是把「输出字符串」换成「输出 S-表达式」，骨架完全一致——这正是 functional IR「与函数式目标同构」带来的红利。

#### 4.3.4 代码实践

**实践目标**：对照 `aigmap.cc`，确认 AIG 节点的三个字段（`portbit` / `left_parent,right_parent` / `inverter`）分别对应 RTLIL 回写时的哪一类 `$_` 单元。

**操作步骤**（源码阅读型）：

1. 读 [aigmap.cc:97-117](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/aigmap.cc#L97-L117)。
2. 列出三栏对照表：AigNode 字段 → 判断条件 → 生成的 RTLIL 单元。

**预期结果**：

| AigNode 形态 | 判断条件 | 回写出的 RTLIL |
|---|---|---|
| 叶子 | `portbit >= 0` | 直接引用原 cell 的输入位（不建新单元） |
| 常数 | `portbit<0 && left/right<0` | `State::S0`/`S1`（常数位） |
| 与门 | `left/right >= 0` | `$_AND_`（或 `$_NAND_`） |
| 任意节点 + `inverter` | `node.inverter` | 额外加一个 `$_NOT_` |

#### 4.3.5 小练习与答案

**练习**：既然 functional IR 已经把设计表达成数学函数，为什么 `sat`/`equiv` 命令不直接基于 functional IR 来编码，而是用 `satgen` 直接编码 RTLIL？

> **答案**：两者目标不同。functional IR 是为「翻译成函数式目标语言」设计的，强调字级、SSA、与 SMT/Rosette 同构；而 `satgen` 需要的是位级、逐门的 CNF 编码，并能精细控制时间帧展开（`@N:` 前缀）、初始值、`$equiv` miter 等效检查语义。RTLIL 门级网表离 CNF 更近，`satgen` 直接遍历 cell 逐个编码更直接高效。两者并不冲突：functional IR 的字级 SMT 输出（`write_functional_smt2`）也可以交给 SMT 求解器，只是走的是「字级 SMT」而非「位级 SAT」这条路。

---

## 5. 综合实践

设计一个贯穿本讲的对比任务，把两种表示并排看清。

**任务**：用同一段逻辑分别落到 AIG 与 functional IR，对照它们的抽象层次。

**操作步骤**：

1. 写一个含「按位或、选择、加法」的小设计：

```verilog
// 示例代码
module demo(input [3:0] a, b, input s, output [3:0] y, output [3:0] z);
    assign y = s ? (a + b) : (a | b);   // 含 mux、add、or
    assign z = ~a;                        // 含 not
endmodule
```

2. **AIG 侧**：`synth` 后跑 `aigmap`，再 `write_rtlil`。观察 `y` 的逻辑被拆成了多少个 `$_AND_`/`$_NOT_`（注意：`$add` 会被综合成逐位全加器，每个全加器又是一堆与/非门）。
3. **functional IR 侧**：`prep` 后跑 `clk2fflogic`（如需要）再 `write_functional_smt2`。观察同一个 `y` 在 SMT 里是一个字级的 `ite`（mux）包着 `add` 和 `bvor`。

**需要观察的现象与预期结果**：

- AIG 输出是「位级爆炸」的：4 位加法 + 4 位 mux + 4 位 or 会展开成几十个 `$_AND_`/`$_NOT_`。
- functional IR 输出是「字级紧凑」的：`y = ite(s, a+b, a|b)` 基本一两行 S-表达式。
- 由此体会：**抽象层次越高（字级），表达越紧凑但离硬件越远；层次越低（AIG/CNF），越啰嗦但离 abc/SAT 越近。** Yosys 维护多种表示，就是为了让不同下游各取所需。

具体节点数与命令可运行性**待本地验证**。

---

## 6. 本讲小结

- **RTLIL 之外还有两种「更窄」的中间表示**：字级的 functional IR 与位级的 AIG，它们都是为特定下游「窄化」RTLIL 的产物，而非替代品。
- **functional IR** 把整个模块建模成纯函数 `(inputs, current_state) -> (outputs, next_state)`，采用 SSA 计算图、显式拆分位宽扩展、节点自动去重；入口是 `Functional::IR::from_module`（[functional.cc:726](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/functional.cc#L726)），翻译核心是 `handle()` 的逐 cell 分发，使用前需先 `async2sync`/`clk2fflogic`。
- **AIG（cellaigs）** 用「二输入与门 + 反相器」两种原语统一表示任意组合单元；`or/xor/mux` 全部经德摩根等定律由 `and_gate`+`not_gate` 合成；反相器折叠 + 结构哈希 + 常数折叠让图保持最小。
- **`$and/$or/$not/$mux` 转 AIG 的路径**：`$not`→`not_gate`；`$and`→`and_gate`；`$or`→`or_gate`（= `nand(not,not)`）；`$mux`→`mux_gate`（= `or(and(A,~S), and(B,S))`）。
- **衔接要分清**：functional IR 喂 SMT/Rosette/C++ 符号后端；`cellaigs::Aig` 的直接消费者是 `aigmap`/`timeest`/`json`，`abc9` 与 `satgen` 各有独立的 AIG/CNF 机制，不直接使用 `cellaigs`。AIG 作为「表示」才是 abc 与 aiger/SAT 生态的共同语言。
- 两种表示体现同一条设计哲学：**为不同的下游维护不同的抽象层次**，让综合（RTLIL）、符号验证（functional IR）、逻辑优化与 SAT（AIG）各得其所。

---

## 7. 下一步学习建议

- **顺接 abc9**：阅读 [passes/techmap/abc9.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/abc9.cc) 与 [backends/aiger2/aiger.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/aiger2/aiger.cc)，对照本讲的「AIG 是 abc/aiger 共同语言」理解 abc9 如何把模块切成 AIG 片段再调外部 `yosys-abc`。
- **顺接 SAT**：回到 [u10-l1](u10-l1-sat-formal-verification.md) 与 [kernel/satgen.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/satgen.cc)，对比「satgen 直接编码 RTLIL→CNF」与本讲「AIG→aiger→SAT」两条到达 SAT 的不同路径。
- **深入 functional 后端开发**：照着 [docs/source/yosys_internals/extending_yosys/functional_ir.rst](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/yosys_internals/extending_yosys/functional_ir.rst) 与 [dummy.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/code_examples/functional/dummy.cc)，动手写一个最小的 functional 后端（遍历 IR 打印每个节点），作为本讲的延伸实践。
- **底层机制**：若对 functional IR 的去重/拓扑排序底层感兴趣，读 [kernel/compute_graph.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/compute_graph.h) 与 [kernel/drivertools.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/drivertools.h)（`DriverMap` 是 `from_module` 把 RTLIL 连接关系整理成驱动树的关键）。
