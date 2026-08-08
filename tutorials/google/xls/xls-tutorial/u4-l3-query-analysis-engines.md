# 查询与分析引擎

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 XLS 里「分析」与「优化」是如何解耦的：优化 Pass 不自己去推导信息，而是向一个统一的 `QueryEngine` 接口提问（「这个 Node 的某些位是不是恒为 0？」「这个 Node 的取值范围是什么？」）。
- 理解 **BDD 查询引擎（`BddQueryEngine`）** 如何用二元决策图精确推断每个位的状态与位之间的蕴含关系，以及它为什么会因为算术/比较运算而指数爆炸。
- 理解 **区间集合分析（`IntervalSet` + `RangeQueryEngine`）** 如何用一个不相交区间的集合来近似每个 Node 的取值范围，恰好弥补 BDD 不擅长算术的短板。
- 理解 **前向查询引擎（`ForwardingQueryEngine`）** 这类「适配器」基类的作用，以及多个引擎如何通过 `UnionQueryEngine` 组合成一个「取并集」的综合引擎。
- 能够读懂一个 Pass 是怎么消费查询引擎的，并能用一个含掩码（mask）比较与位拼接的 IR，亲手说明位级信息是如何触发化简的。

本讲只讲「分析」这一侧的数据结构与原理，不展开任何具体的图变换（那是 u4-l2 的事）。

## 2. 前置知识

在进入正文前，先用通俗语言建立几个概念。

**静态分析 vs. 运行期求值。** 运行期求值是「给一个具体输入，算出一个具体输出」；静态分析则是「不跑程序，只看图的结构，推断出对所有可能输入都成立的事实」。例如「`y = and(x, 0b00001111)` 之后，`y` 的高 4 位一定是 0」就是一个静态可证明的事实，与 `x` 具体取什么无关。查询引擎就是 XLS 的静态分析设施。

**三值逻辑（ternary）。** 静态分析里，一个位有三种状态：确定是 0（`kKnownZero`）、确定是 1（`kKnownOne`）、不确定（`kUnknown`）。我们用 `0/1/X` 来记，比如 `0b1XX0` 表示「最高位是 1、最低位是 0、中间两位不确定」。三值逻辑是位级分析的最基本抽象，它的按位与/或/非都有标准的真值表（见后文 `ternary.h`）。

**抽象解释（abstract interpretation）。** 这是本讲反复出现的核心思想。设想我们不去算「具体的 0/1」，而是在某个「抽象域」里算：

- 在**三值域**里，每个位是 `0/1/X`，`and(1, X)=X`、`and(0, X)=0`。
- 在**BDD 域**里，每个位是一个布尔函数（关于输入位的真值函数）。
- 在**区间域**里，每个值是一个范围集合，比如 `y ∈ {0..15}`。

关键洞见是：**只要给每个域定义清楚最基础的「一位的与/或/非/选择」怎么做，加减法、比较、移位这些复杂运算都可以用同一套模板「翻译」过去。** XLS 用一个 CRTP 模板 `AbstractEvaluator` 把这件事固化下来，BDD、三值等域都是它的实例。这是本讲理解 BDD 引擎的钥匙。

**二元决策图（BDD）。** BDD 是一种紧凑表示布尔函数的有向无环图：每个内部节点是一个变量（某一位），根据该位取 0 或 1 分成两条出边，叶节点是 `0` 或 `1`。两个布尔函数是否等价、一个是否蕴含另一个，都可以在 BDD 上精确判断。BDD 的代价是：某些运算（尤其算术、乘法）会让图指数膨胀。

**承接关系。** u4-l1 讲了 Pass 框架，u4-l2 讲了五个代表性 Pass，并提到它们统一用 `Run()` 返回「是否改动」、用查询引擎拿信息。本讲就回答：**那些 Pass 用的查询引擎到底是什么、内部怎么工作。** 本讲也依赖 u3-l1（IR 的 `Node`/`Function` 概念）与 u3-l2（运算符 `Op`）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `xls/passes/query_engine.h` | 所有查询引擎的抽象基类 `QueryEngine`，定义了 Pass 能问的全部问题（接口）。 |
| `xls/passes/lazy_query_engine.h` | `LazyQueryEngine` 模板基类，提供「按需计算 + 增量失效缓存」，`BddQueryEngine` 等建立在它之上。 |
| `xls/passes/bdd_query_engine.h` / `.cc` | **BDD 查询引擎**：用 BDD 做精确位级分析。 |
| `xls/passes/bdd_evaluator.h` | `SaturatingBddEvaluator`：把 BDD 包成抽象求值器，带路径数饱和。 |
| `xls/ir/abstract_evaluator.h` | `AbstractEvaluator` CRTP 模板：「一套原语 → 任意运算」的抽象解释骨架。 |
| `xls/ir/ternary.h` | 三值逻辑定义与按位运算真值表。 |
| `xls/ir/interval_set.h` | **区间集合** `IntervalSet`：不相交区间的集合及其运算。 |
| `xls/passes/range_query_engine.h` | **区间查询引擎** `RangeQueryEngine`：跟踪每个 Node 的取值区间集合。 |
| `xls/passes/forwarding_query_engine.h` | **前向查询引擎** `ForwardingQueryEngine`：适配器基类，把所有接口调用转发给「真实」引擎。 |
| `xls/passes/union_query_engine.h` | `UnionQueryEngine`：把多个引擎的结果并起来，组成综合引擎。 |
| `xls/passes/arith_simplification_pass.cc` | 消费方示例：算术化简 Pass 如何向查询引擎提问。 |

## 4. 核心概念与源码讲解

### 4.1 优化与分析的解耦：`QueryEngine` 接口

#### 4.1.1 概念说明

一个优化 Pass 要改图，必须先有依据。比如「把 `x + 0` 化成 `x`」需要知道加数恒为 0；「删掉 `if (false)` 的死分支」需要知道条件恒为假。这些「恒为某值」的事实从哪来？

XLS 的答案是：**Pass 不自己推导，而是向一个统一的「查询引擎」提问。** 引擎负责静态分析，Pass 负责图变换，两者用一个纯接口解耦。这样：

- 同一套分析（位级已知位、取值范围、位间蕴含）可以被所有 Pass 复用。
- 换一个更聪明（或更省时）的分析引擎，不用动任何一个 Pass。
- Pass 的逻辑只关心「图怎么改」，不必关心「事实怎么来」。

#### 4.1.2 核心流程

`QueryEngine` 是一个纯抽象基类，它定义了 Pass 能问的所有问题，大致分四类：

1. **位值是否已知**：`GetTernary(node)` 返回一个三值向量（每位的 `0/1/X`）；`IsFullyKnown`、`KnownValue`、`IsAllZeros`、`IsAllOnes` 等是便捷封装。
2. **取值范围**：`GetIntervals(node)` 返回该 Node 可能落入的区间集合。
3. **位之间的关系**：`Implies(a, b)`（a 为真是否蕴含 b 为真）、`AtMostOneTrue(bits)`（这些位里是否最多一个为真）、`KnownEquals` / `KnownNotEquals`（两位是否恒等/恒反）。
4. **条件信息**：`ImpliedNodeValue` / `ImpliedNodeTernary`（「假设某些位取特定值时，这个 Node 会是什么」），支撑 `if` 分支内的特化分析。

关键约定（务必记住）：

> **返回 `true` 表示「已证明为真」；返回 `false` 表示「分析无法确定」，而**不是**「已证明为假」。**

也就是说，`KnownEquals(a,b)` 返回 `false` 并不代表 `a` 与 `b` 一定不相等，只代表「引擎没把握」。这是一种**保守的过近似（over-approximation）**：宁可漏报（少优化），绝不误报（错优化）。误报会导致优化后功能改变，是编译器的致命错误。

#### 4.1.3 源码精读

基类定义与用途说明在 [xls/passes/query_engine.h:L104-L116](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/query_engine.h#L104-L116)：注释明确写出「这是一个回答函数中位值与位间关系问题的抽象基类」。

返回值约定就在类注释里 [xls/passes/query_engine.h:L109-L115](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/query_engine.h#L109-L115)（即上面那段「true=已证明、false=不确定」的契约）。

几个核心接口签名：

- 位级已知值：[xls/passes/query_engine.h:L132-L133](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/query_engine.h#L132-L133) `GetTernary(Node*)`。
- 取值区间：[xls/passes/query_engine.h:L154](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/query_engine.h#L154) `GetIntervals(Node*)`。
- 蕴含关系：[xls/passes/query_engine.h:L163-L164](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/query_engine.h#L163-L164) `Implies(a, b)`。
- 恒等/恒反：[xls/passes/query_engine.h:L181-L186](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/query_engine.h#L181-L186) `KnownEquals` / `KnownNotEquals`。
- 是否全部已知：[xls/passes/query_engine.h:L216](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/query_engine.h#L216) `IsFullyKnown`。

**消费方示例**——算术化简 Pass（u4-l2 讲过）怎么用引擎。它把引擎当作只读字典，到处提问：

- 判断「乘法会不会溢出」时，用 `GetTernary` 拿到操作数的三值，再算它至少需要多少位：[xls/passes/arith_simplification_pass.cc:L335-L340](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/arith_simplification_pass.cc#L335-L340)。如果已知位宽够大、不可能溢出，才敢做后续改写。
- 除法化简里，先确认除数是编译期常量：[xls/passes/arith_simplification_pass.cc:L586-L587](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/arith_simplification_pass.cc#L586-L587) `IsFullyKnown` + `KnownValueAsBits`。
- 判断「除数会不会是 0」：[xls/passes/arith_simplification_pass.cc:L1461](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/arith_simplification_pass.cc#L1461) `query_engine.Covers(divisor, UBits(0, ...))`——只有当引擎能证明除数**不**覆盖 0 时，才能安全地省去除零处理。
- 已知前导零个数与最大值：[xls/passes/arith_simplification_pass.cc:L1441-L1444](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/arith_simplification_pass.cc#L1441-L1444) `KnownLeadingZeros` / `MaxUnsignedValue`。

可以看到，Pass 完全不知道引擎内部是 BDD 还是区间分析——它只调接口。这就是解耦的全部价值。

#### 4.1.4 代码实践

**实践目标：** 用肉眼追踪「提问—回答」的链路，体会 Pass 对引擎接口的依赖。

**操作步骤：**

1. 打开 [xls/passes/arith_simplification_pass.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/arith_simplification_pass.cc)，搜索 `query_engine.`，统计这个文件里调用了 `QueryEngine` 的哪几个方法。
2. 对每个调用，对照 [query_engine.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/query_engine.h) 里的接口注释，确认它属于四类问题中的哪一类（位值 / 区间 / 位间关系 / 条件）。
3. 特别看 L1461 的 `Covers(divisor, 0)`：如果引擎返回 `true`（除数可能为 0），Pass 会怎么做？如果返回 `false`（除数绝不可能是 0）呢？

**需要观察的现象：** 你会发现 Pass 的几乎所有判断都以 `if (query_engine.某方法(...))` 开头——引擎说「是」才动手改，说「不确定」就跳过。

**预期结果：** 你会列出一个方法清单（如 `KnownValueAsBits`、`IsFullyKnown`、`GetTernary`、`Covers`、`MaxUnsignedValue`、`KnownLeadingZeros`），并理解「Pass 的保守性来自引擎的过近似」。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `KnownEquals(a, b)` 返回 `false` 时，不能据此把 `a` 和 `b` 当成「不相等」来优化？

**参考答案：** 因为 `false` 的语义是「分析无法确定」，不是「已证明不等」。引擎是过近似，可能只是能力不够。若误当作「不等」去优化，可能把功能上其实相等的两条路径错合并，导致结果错误。

**练习 2：** 假设你要新写一个 Pass，需要知道「Node `n` 的最低位是不是恒为 0」。你应该调哪个接口？

**参考答案：** 调 `GetTernary(n)` 拿到三值向量，检查最低位是否为 `kKnownZero`；或用更直接的 `KnownValue(TreeBitLocation(n, 0))` 看是否返回 `false`（即该位恒为 0）。

---

### 4.2 BDD 查询引擎：位级精确分析

#### 4.2.1 概念说明

`BddQueryEngine` 是 XLS 里**最精确的位级分析引擎**。它的思路是：把函数里每个「位」表示成一个关于输入位的布尔函数，并用 BDD 来存这些函数。一旦某个位的 BDD 恰好是常数 `0` 或常数 `1`，就说明这个位在所有输入下都确定是 0 或 1——这正是 `GetTernary` 想要的「已知位」。

相比朴素的三值传播（按位与/或/非一遇到 `X` 就退化），BDD 能保留**位与位之间的关系**。例如三值分析看 `x == x` 未必能得到「恒真」（因为 `x` 的每位都是 `X`），而 BDD 能精确证明它恒真，因为 `x` 的每一位都和自身比较。

代价是：BDD 对算术、乘法、宽位移这类运算会**指数膨胀**。所以 XLS 的策略是——这类运算干脆不放进 BDD，让它们退化成「未知变量」，把算术范围的推断交给区间引擎（4.3 节）。

#### 4.2.2 核心流程

BDD 引擎对每个 Node 计算的信息，是「该 Node 每一位对应的 BDD 节点」（一个 `LeafTypeTree<vector<BddNode>>`）。流程是经典的**抽象解释**：

1. **入参/未知节点**：每个无法分析的位，在 BDD 里建一个全新的「变量节点」代表它（意思是「这个位可以是任意值」）。
2. **可分析节点**：用抽象求值器 `SaturatingBddEvaluator`，按 IR 的运算语义，把操作数位的 BDD 组合成结果位的 BDD。例如 `and(a, b)` 的某一位 = `BDD的And(a 的该位, b 的该位)`；`not(a)` = `Bdd的Not(...)`；`select(s, t, f)` = `IfThenElse(s, t, f)`。
3. **饱和保护**：如果某个组合出来的 BDD 函数路径数（从节点到叶 0/1 的路径数）超过上限，就用 `TooManyPaths` 哨兵替换它，并退化为一个新变量——以精度换不爆炸。
4. **读出结论**：查询 `GetTernary` 时，遍历每一位，若该位的 BDD 节点等于常数 `0` → `kKnownZero`；等于常数 `1` → `kKnownOne`；否则 `kUnknown`。

而步骤 2 里「把任意 IR 运算翻译成 BDD 运算」之所以不用为每个 `Op` 写一份，是因为有 `AbstractEvaluator` 这个模板：它已经用最基本的 `And/Or/Not/If` 原语实现好了加法、比较、移位、位切片等所有复杂运算。BDD 域只要提供这几个原语，就「白拿」了对全部运算的解释能力。

可以用一段伪代码概括「读出已知位」的逻辑：

```text
for 每一位 bit_index of node:
    bdd_node = GetBddNode(node, bit_index)
    if bdd_node == bdd.zero():   ternary[bit] = KnownZero
    elif bdd_node == bdd.one():  ternary[bit] = KnownOne
    else:                        ternary[bit] = Unknown
```

至于「位间关系」，比如 `Implies(a, b)`，靠的就是布尔恒等式：

\[ a \Rightarrow b \quad\equiv\quad \neg(a \wedge \neg b) \]

只要在 BDD 上算出 `Not(And(a_bdd, Not(b_bdd)))` 是否等于常数 `1`，就能精确判定蕴含是否成立。

#### 4.2.3 源码精读

类的定位与权衡写在注释里 [xls/passes/bdd_query_engine.h:L44-L50](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/bdd_query_engine.h#L44-L50)：BDD 比 ternary 更锐利，缺点是对算术/比较会变慢甚至指数慢，因此这些运算一般被排除。类声明 [xls/passes/bdd_query_engine.h:L51-L52](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/bdd_query_engine.h#L51-L52) 显示它继承自 `LazyQueryEngine<vector<SaturatingBddNodeIndex>>`——也就是说「每个节点存的 Info = 它每一位的（饱和）BDD 节点」，并且是惰性计算的。

**饱和机制。** BDD 元素是一个求和类型：要么是正常的 BDD 节点，要么是 `TooManyPaths` 哨兵：[xls/passes/bdd_evaluator.h:L39-L40](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/bdd_evaluator.h#L39-L40)。`SaturatingBddEvaluator` 继承自 `AbstractEvaluator`，[xls/passes/bdd_evaluator.h:L72-L73](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/bdd_evaluator.h#L72-L73)，为 BDD 域实现了带饱和的 `Not/And/Or/If`：[xls/passes/bdd_evaluator.h:L82-L137](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/bdd_evaluator.h#L82-L137)。每个运算算完都会检查路径数是否超限，超了就返回 `TooManyPaths`，任何与 `TooManyPaths` 运算的结果也是 `TooManyPaths`——这是「以精度换不爆炸」的精确定义。

**抽象求值器模板（理解 BDD 引擎的钥匙）。** [xls/ir/abstract_evaluator.h:L52-L53](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/abstract_evaluator.h#L52-L53) 是一个 CRTP 模板。它把 `One/Zero` 转交给派生类（[xls/ir/abstract_evaluator.h:L71-L72](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/abstract_evaluator.h#L71-L72)），然后用这几个原语组合出全部高层运算，例如位切片（[xls/ir/abstract_evaluator.h:L188](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/abstract_evaluator.h#L188)）、无符号小于（[xls/ir/abstract_evaluator.h:L235](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/abstract_evaluator.h#L235)）、加法（[xls/ir/abstract_evaluator.h:L449](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/abstract_evaluator.h#L449)）。这正是「一套原语 → 任意运算」的抽象解释骨架，BDD 域、三值域都复用它。

**哪些运算不进 BDD。** `ShouldEvaluate` 决定一个节点要不要算 BDD：[xls/passes/bdd_query_engine.cc:L132-L146](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/bdd_query_engine.cc#L132-L146)。它把移位/动态切片（宽度超过 128 位时，[L72](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/bdd_query_engine.cc#L72)）、`kNonAnalyzableOps`（[L86-L118](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/bdd_query_engine.cc#L86-L118)，含数组、tuple、gate、proc 通信等）和乘除模 `kMultiplicationOps`（[L120-L129](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/bdd_query_engine.cc#L120-L129)）排除。被排除的节点会退化成全新变量（信息未知）。

**逐节点求值。** `BddNodeEvaluator` 是一个访问者：[xls/passes/bdd_query_engine.cc:L372-L391](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/bdd_query_engine.cc#L372-L391)。对认识的运算，它用 `AbstractNodeEvaluator` 按语义组合操作数 BDD；对不认识的运算（`DefaultHandler`），直接用一组新变量代替。`ComputeInfo` 把这些串起来：[xls/passes/bdd_query_engine.cc:L410-L435](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/bdd_query_engine.cc#L410-L435)，先 `ShouldEvaluate` 过滤、再注入操作数信息、最后 `node->VisitSingleNode(&node_evaluator)` 触发求值。

**读出已知位。** `GetTernary` 的核心映射：[xls/passes/bdd_query_engine.cc:L813-L847](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/bdd_query_engine.cc#L813-L847)——逐位取 BDD 节点，等于 `bdd().zero()` 记 `kKnownZero`（[L838-L839](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/bdd_query_engine.cc#L838-L839)）、等于 `bdd().one()` 记 `kKnownOne`（[L840-L841](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/bdd_query_engine.cc#L840-L841)），否则 `kUnknown`。单位的 `KnownValue` 也是同样判定：[xls/passes/bdd_query_engine.cc:L395-L408](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/bdd_query_engine.cc#L395-L408)。默认路径上限 `kDefaultPathLimit = 1024`：[xls/passes/bdd_query_engine.h:L66](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/bdd_query_engine.h#L66)。

**三值逻辑基础。** `TernaryValue` 三态枚举：[xls/ir/ternary.h:L43-L47](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ternary.h#L43-L47)。按位与的真值表（`0 & X = 0`、`1 & X = X`）：[xls/ir/ternary.h:L186-L201](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ternary.h#L186-L201)。这套三值运算就是朴素位级传播用的；BDD 引擎之所以更强，正是因为它能超越这种「位独立」的近似。

#### 4.2.4 代码实践

**实践目标：** 用一个含掩码（mask）按位与的 IR，亲手推出 BDD/区间分析会得到哪些「恒为 0」的位，并定位消费这类信息的化简代码。

**操作步骤：**

1. 把下面这段 IR 存成 `/tmp/mask_example.ir`（这是**示例 IR**，并非项目自带文件）：

   ```ir
   package mask_example

   fn mask_example(x: bits[8]) -> bits[1] {
     // 15 == 0b00001111：低 4 位掩码
     masked: bits[8] = and(x, bits[8]:15)
     // masked 与 15 比较：当且仅当低 4 位全为 1
     is_full: bits[1] = eq(masked, bits[8]:15)
     ret is_full
   }
   ```

2. **人工分析**（BDD/区间视角）：
   - `and(x, 0b00001111)` 的第 4~7 位（高 4 位）：`0 & X = 0`，所以这 4 位在 BDD 里是常数 `0`，即 `masked` 的三值是 `0b0000XXXX`，取值范围 `masked ∈ {0..15}`。
   - `eq(masked, 15)`：因为 `masked` 高 4 位恒 0，比较的真相只取决于低 4 位是否全 1——这正是一个可化简的比较。

3. **定位消费方**：在算术化简 Pass 里，有一段专门处理「与常数掩码的无符号比较」的代码，它依赖引擎给出常量值：[xls/passes/arith_simplification_pass.cc:L1988-L2020](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/arith_simplification_pass.cc#L1988-L2020)。注意它用 `query_engine.KnownValueAsBits`（[L2002](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/arith_simplification_pass.cc#L2002)）拿到比较常量，再判断它是不是形如 `0...01...1` 的掩码（`LsbConstantMask`，[L1198](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/arith_simplification_pass.cc#L1198)），最后把比较改写成对高低位的 reduce 运算。

4. （可选，需本地构建）运行 `bazel-bin/xls/tools/opt_main /tmp/mask_example.ir` 观察优化前后 IR 是否变化。

**需要观察的现象：** 你应当能解释「为什么 `masked` 的高 4 位是已知 0」这件事可以被静态证明（不依赖 `x` 的值），以及这一事实如何让 `eq(masked, 15)` 变得可化简。

**预期结果 / 待本地验证：** 人工分析上，`masked ∈ {0..15}`、高 4 位恒 0 是确定的。`opt_main` 是否会把这段具体 IR 里的 `eq` 改写、改写成什么形状，取决于当前优化等级与管线里掩码化简的触发条件，**待本地验证**。重点是理解「分析得出已知位 → 化简 Pass 据此改写」这条链路，而不是某个固定输出。

> 真实工程佐证：最近一次相关提交 `7e91bb0a2 arith_simplification: extend unsigned mask comparisons to include off-by-one consts`（见 `git log`）正是扩展了 [L1988-L2020](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/arith_simplification_pass.cc#L1988-L2020) 这段掩码比较化简，说明这类「分析驱动」的化简是活跃开发的方向。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 BDD 引擎把乘法 `kSMul/kUMul` 和除法 `kSDiv/kUDiv` 排除在分析之外？

**参考答案：** 因为乘法的 BDD 表示会随位宽指数膨胀（两个 N 位乘积需刻画 2N 位的关系），会导致引擎爆炸性变慢。XLS 选择放弃对乘除的位级精确分析，把它们退化为未知变量，把「取值范围」交给区间引擎来近似。

**练习 2：** 给定 `y: bits[4] = and(x, bits[4]:0b1010)`，写出 `y` 的三值表示和取值集合。

**参考答案：** 三值：`0bX0X0`（第 1、3 位恒 0，第 0、2 位等于 `x` 对应位，未知）。取值集合是 `{0, 2, 8, 10}`，即区间表示 `{0..0, 2..2, 8..8, 10..10}`。

**练习 3：** 用布尔恒等式解释 BDD 上 `Implies(a, b)` 是怎么判定的。

**参考答案：** \(a \Rightarrow b \equiv \neg(a \wedge \neg b)\)。在 BDD 上构造 `Not(And(a_bdd, Not(b_bdd)))`，若它等于常数 `1`（恒真），则蕴含成立。

---

### 4.3 区间集合分析：`IntervalSet` 与 `RangeQueryEngine`

#### 4.3.1 概念说明

BDD 精确但不擅长算术。区间分析正好反过来：**用一个不相交区间的集合来近似一个值能取哪些数。** 例如「`y ∈ {0..15}`」用一个区间 `[0, 15]` 就表达了；「`y` 要么是 0，要么是 200」用 `{[0,0], [200,200]}` 两个区间表达。

区间分析对加减乘除等算术运算非常友好（两个区间相加，结果区间端点就是端点之和的上下界），所以它恰好补上 BDD 留下的算术空白。XLS 里 `IntervalSet` 是数据结构，`RangeQueryEngine` 是用它来跟踪全函数每个 Node 取值范围的查询引擎。

#### 4.3.2 核心流程

`IntervalSet` 的核心不变量叫**归一化（Normalize）**。一个归一化的区间集合满足：

- 没有非法（空）区间；
- 任意两个区间不相交也不相邻（否则合并成一个）；
- 区间按无符号大小排序；
- 在所有「表示同一组点」的区间集合里，区间数最少。

很多方法（`Intervals()`、`Covers`、`IsPrecise` 等）都要求集合先归一化。集合运算（并、交、补）都会返回归一化结果。

`RangeQueryEngine` 的运行流程：

1. `Populate(f)` 时，用一个 DFS 访问者按拓扑序遍历函数（[range_query_engine.h:L114-L117](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/range_query_engine.h#L114-L117)，默认用 `NoGivensProvider`，即除图本身外无额外已知信息）。
2. 对每个 Node，依据其 `Op` 语义，把操作数的 `IntervalSet` 组合成结果的 `IntervalSet`（加法=区间端点相加，比较=结果落在 `{0..0}` 或 `{1..1}`，等等）。
3. 同时从区间里抽出「已知位」：若某区间集合里所有数的某一位都相同，该位就是已知位，填进 `known_bits`。
4. 查询 `GetTernary` 直接由 `known_bits` 构造三值向量；查询 `GetIntervals` 返回该 Node 的区间树。

由此，`RangeQueryEngine` 同时提供了「区间」和「位级已知位」两类信息，且对算术友好。

#### 4.3.3 源码精读

`IntervalSet` 数据结构：[xls/ir/interval_set.h:L37](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/interval_set.h#L37)。几个常用静态工厂：覆盖全空间的 `Maximal`（[L62](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/interval_set.h#L62)）、精确单点 `Precise`（[L69](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/interval_set.h#L69)）、掏空一个点的 `Punctured`（[L73](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/interval_set.h#L73)）。

**归一化的定义**（务必理解）：[xls/ir/interval_set.h:L269-L283](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/interval_set.h#L269-L283)，方法在 [L283](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/interval_set.h#L283) `Normalize()`。核心查询：是否覆盖某点 `Covers`（[L351](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/interval_set.h#L351)）、是否只含一个点 `IsPrecise`（[L363](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/interval_set.h#L363)）、是否覆盖全空间 `IsMaximal`（[L372](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/interval_set.h#L372)）、最小外包区间 `ConvexHull`（[L287](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/interval_set.h#L287)）。集合运算：并 `Combine`（[L296](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/interval_set.h#L296)）、交 `Intersect`（[L300](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/interval_set.h#L300)）、补 `Complement`（[L304](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/interval_set.h#L304)）——三者都返回归一化结果。

**`RangeQueryEngine`。** 类声明 [xls/passes/range_query_engine.h:L103](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/range_query_engine.h#L103)。它给每个 Node 存一份 `RangeData`（含 `IntervalSetTree` 与可选三值）：[xls/passes/range_query_engine.h:L49-L54](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/range_query_engine.h#L49-L54)。`Populate` 走无先验的默认提供者：[xls/passes/range_query_engine.h:L114-L117](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/range_query_engine.h#L114-L117)。`GetTernary` 由 `known_bits` 构造三值：[xls/passes/range_query_engine.h:L151-L161](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/range_query_engine.h#L151-L161)；`GetIntervals` 直接返回区间树：[xls/passes/range_query_engine.h:L163-L165](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/range_query_engine.h#L163-L165)。

一个值得注意的设计点：注释 [range_query_engine.h:L134-L143](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/range_query_engine.h#L134-L143) 说明 `HasKnownIntervals` 与 `IsTracked` 不完全等同——为了与 BDD 引擎兼容，`IsTracked` 实际看的是「有没有已知三值位」（[L125](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/range_query_engine.h#L125)），而元组/数组类型可能「有区间但无位级三值」。这是两类引擎并存留下的历史细节。

#### 4.3.4 代码实践

**实践目标：** 用区间运算手工推出一段含算术与掩码的 IR 的取值范围。

**操作步骤：**

1. 考虑下面这段**示例 IR**：

   ```ir
   package range_example

   fn range_example(x: bits[8]) -> bits[8] {
     masked: bits[8] = and(x, bits[8]:15)   // x & 0b00001111
     plus: bits[8] = add(masked, bits[8]:3) // (x & 0xF) + 3
     ret plus
   }
   ```

2. 逐步推区间（区间运算规则：加法的区间端点 = 操作数端点之和的上下界）：
   - `masked`：`x` 未知 = 全空间 `[0, 255]`；与 `15` 后，区间变成 `[0, 15]`。
   - `plus = masked + 3`：`[0+3, 15+3] = [3, 18]`。
   - 所以 `RangeQueryEngine.GetIntervals(plus)` 会得到一个落在 `[3, 18]` 的区间集合（可能被进一步精确化为若干子区间，但一定 ⊆ `[3,18]`）。

3. 对照 [range_query_engine.h:L163-L165](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/range_query_engine.h#L163-L165) 确认 `GetIntervals` 就是返回这份区间树。

**需要观察的现象：** 注意 BDD 引擎能算出 `masked` 的「高 4 位恒 0」，但**不一定**算得出 `plus` 的范围（因为 `add` 在 BDD 里也可能因路径数超限而饱和退化）；而区间引擎能轻松给出 `[3, 18]`，却给不出「某一位恒为某值」这样精细的位级关系（除非所有取值在某位上恰好相同）。

**预期结果：** `plus ∈ [3, 18]` 是区间分析给出的过近似结论。这正好说明 BDD 与区间两类引擎是**互补**的——这也是下一节要把它们组合起来的原因。

#### 4.3.5 小练习与答案

**练习 1：** `IntervalSet` 为什么要 `Normalize`？不归一化会怎样？

**参考答案：** 归一化保证区间不相交、不相邻、最少、有序。不归一化时，同一组点可能被很多重叠/相邻区间表示，导致集合运算（交并补）结果膨胀、`Covers`/`IsPrecise` 等查询出错或变慢。多数方法都 `CHECK(is_normalized_)` 强制要求归一化。

**练习 2：** 给定区间集合 `{[0,0], [200,200]}`（bits[8]），它的 `ConvexHull` 是什么？这是否丢失了信息？

**参考答案：** `ConvexHull = [0, 200]`。是的，它把中间 1~199 这些原本不可能的值也包了进来，丢失了「只能是 0 或 200」的精确性。这是区间分析「过近似」的典型代价。

---

### 4.4 前向查询引擎与组合：`ForwardingQueryEngine`、`UnionQueryEngine`

#### 4.4.1 概念说明

到目前为止我们有三类引擎（BDD、区间、以及 u4-l2 提到的只看局部上下文的 `StatelessQueryEngine`），它们各有所长。实际优化时，Pass 想「问到什么就能答什么」，不想关心背后是哪个引擎。这就需要两类「胶水」：

- **前向查询引擎（`ForwardingQueryEngine`）**：一个适配器基类。它自己不实现任何分析，而是把 `QueryEngine` 的**每一个**虚函数原样转发给一个「真实」引擎（由子类通过 `real()` 提供）。它的价值是让子类可以在转发前后插逻辑（比如「加一个假设条件」），或在不暴露真实引擎所有权的前提下当 `QueryEngine` 用。
- **并集引擎（`UnionQueryEngine`）**：把多个引擎的结果合并——对每个问题，只要其中**任意一个**引擎能证明为真，就返回真。生产环境里优化管线实际用的「综合查询引擎」通常就是 BDD + 区间（+ 其他）的并集。

#### 4.4.2 核心流程

**前向引擎的转发模式。** `ForwardingQueryEngine` 继承 `QueryEngine`，对每个虚函数都写成 `return real().同名方法(...)`。子类只需实现两个纯虚方法 `real()`（返回真实引擎的引用），即可获得全部接口的默认转发行为。如果想改写某个查询，只需 override 那一个方法即可。

**并集引擎的合并模式。** `UnionQueryEngine` 持有一组引擎指针，`Populate` 时依次 populate 每个子引擎；查询时遍历子引擎，把结果**取并**（如 `GetTernary` 取各位已知信息的并集，`IsAllZeros` 等布尔查询取「任一为真即真」）。

这两者组合起来，就实现了「分析与优化的彻底解耦 + 多分析的优势互补」：

```text
   Pass ──(只认 QueryEngine 接口)──▶ UnionQueryEngine
                                        ├─ BddQueryEngine       (位级精确，算术弱)
                                        ├─ RangeQueryEngine     (算术强，位级粗)
                                        └─ ...其他引擎
```

#### 4.4.3 源码精读

**`ForwardingQueryEngine`**：[xls/passes/forwarding_query_engine.h:L41-L42](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/forwarding_query_engine.h#L41-L42)。它的所有方法都是一行转发，例如 `GetTernary`（[L49-L52](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/forwarding_query_engine.h#L49-L52)）、`GetIntervals`（[L68-L70](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/forwarding_query_engine.h#L68-L70)）、`IsFullyKnown`（[L130](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/forwarding_query_engine.h#L130)）。真实引擎由子类经纯虚 `real()` 提供：[xls/passes/forwarding_query_engine.h:L148-L150](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/forwarding_query_engine.h#L148-L150)。

> 注意一个工程约定：基类 `QueryEngine` 的注释里写着 `// LINT.IfChanged - All virtual functions must be forwarded by ForwardingQueryEngine.`（[query_engine.h:L120-L121](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/query_engine.h#L120-L121)），结尾有 `// LINT.ThenChange(//xls/passes/forwarding_query_engine.h)`（[query_engine.h:L237](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/query_engine.h#L237)）。这是个静态检查标记：一旦给 `QueryEngine` 增删虚函数，就必须同步更新 `ForwardingQueryEngine` 的转发，否则会有 lint 报错。这说明「全量转发」是被工具守护的硬约束。

**前向引擎的一个真实子类：`BddQueryEngine::AssumingQueryEngine`。** 它是一个「带额外假设（assumption）」的特化引擎：[xls/passes/bdd_query_engine.cc:L150](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/bdd_query_engine.cc#L150)。它对每个查询都把「假设条件」揉进去后再转发给底层 BDD 引擎，例如 `GetTernary` 在假设成立的前提下重新判定每位：[xls/passes/bdd_query_engine.cc:L206-L237](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/bdd_query_engine.cc#L206-L237)（若 `assumption` 蕴含该位为 1，则记 `kKnownOne`，[L224-L225](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/bdd_query_engine.cc#L224-L225)）。这正是「前向 + 插逻辑」模式的范例——`SpecializeGivenPredicate`/`SpecializeGiven` 产出的条件化引擎大多长这样。

**`UnionQueryEngine`**：把多个（不拥有的）引擎合并：[xls/passes/union_query_engine.h:L49](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/union_query_engine.h#L49) `UnownedUnionQueryEngine`，注释 [L44-L47](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/union_query_engine.h#L44-L47) 说明它合并多个引擎的结果。拥有子引擎所有权的版本是 `UnionQueryEngine`（[L142](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/union_query_engine.h#L142)），并提供编译期可变参工厂 `Of(...)`（[L167](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/union_query_engine.h#L167)）。它把三值/区间等查询逐一转发给各子引擎并合并——这是「多分析互补」在代码里的落点。

#### 4.4.4 代码实践

**实践目标：** 通过阅读代码，验证「前向 = 全量转发」「并集 = 取并」两个模式。

**操作步骤：**

1. 打开 [forwarding_query_engine.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/forwarding_query_engine.h)，随机挑 5 个方法，确认它们体都是 `return real().xxx(...)`。
2. 在 [union_query_engine.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/union_query_engine.cc)（若已生成）或头文件注释里，看 `GetTernary` / `IsAllZeros` 这类方法是如何遍历 `engines_` 取并的。
3. 思考：若一个 Pass 同时需要「位级精确」和「算术范围」，为什么不直接用 BDD，而要用并集引擎？

**需要观察的现象：** 前向引擎的方法体千篇一律；并集引擎的方法体则是对 `engines_` 的循环。

**预期结果：** 你能用自己的话解释「`ForwardingQueryEngine` 解决的是接口适配/条件化，`UnionQueryEngine` 解决的是能力互补」。

#### 4.4.5 小练习与答案

**练习 1：** `ForwardingQueryEngine` 为什么要用纯虚 `real()` 而不是直接存一个 `QueryEngine*` 成员？

**参考答案：** 因为子类可能并不「拥有」一个现成的引擎，而是「每次查询时动态构造/特化」一个视图（如 `AssumingQueryEngine` 把假设揉进 BDD 查询）。用纯虚 `real()` 把「如何拿到真实引擎」开放给子类，既支持持有指针，也支持这种动态特化，更灵活。

**练习 2：** `UnionQueryEngine` 的 `IsAllZeros(node)` 在什么条件下返回 `true`？

**参考答案：** 只要**任意一个**子引擎能证明该 Node 全为 0，就返回 `true`（取各引擎能力的并集）。若所有子引擎都不能证明，才返回 `false`（即「无法确定」）。

---

## 5. 综合实践

把本讲三块内容串起来，做一个完整的「分析 → 化简」追踪。

**任务：** 用一段同时含「位拼接（concat）+ 掩码按位与 + 算术」的 IR，分别从 BDD 和区间两个视角推断信息，再定位可能消费这些信息的化简点。

**示例 IR（存为 `/tmp/composite.ir`）：**

```ir
package composite

fn composite(x: bits[8]) -> bits[4] {
  // 高 4 位补零：concat(0b0000, x[0:4])
  low: bits[4] = bit_slice(x, 4, 4)        // 取 x 的低 4 位
  pad: bits[4] = literal(bits[4]:0)
  wide: bits[8] = concat(pad, low)         // wide = 0b0000_low  => 高 4 位恒 0
  plus: bits[8] = add(wide, bits[8]:1)     // wide + 1
  top: bits[4] = bit_slice(plus, 4, 4)     // 取 plus 高 4 位
  ret top
}
```

**请你完成：**

1. **BDD 视角**：`wide` 的哪些位恒为 0？`plus = wide + 1` 之后，`plus` 的高 4 位在 BDD 里还能保持已知吗？（提示：`add` 在 BDD 中可能因路径数超限而饱和退化——这正是 4.2 讲的算术短板。）
2. **区间视角**：`wide ∈ [0, ?]`、`plus ∈ [?, ?]`、`top` 的范围是什么？（提示：`wide ∈ [0,15]`，`plus ∈ [1,16]`，`top` 是 `plus` 的高 4 位。）
3. **消费追踪**：用 [arith_simplification_pass.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/arith_simplification_pass.cc) 里的接口调用（如 `KnownLeadingZeros`、`MaxUnsignedValue`、`GetTernary`、`Covers`），说明哪类信息能让 Pass 对这段代码做化简（例如已知 `plus` 不会进位到第 5 位以外，或 `top` 恒为某值）。
4. **（可选，待本地验证）** 用 `bazel-bin/xls/tools/opt_main /tmp/composite.ir` 跑一遍，对照优化前后 IR，看你的推断是否被化简体现出来。

**参考要点（分析层面）：**

- `wide` 高 4 位恒 0（BDD 可证），`wide ∈ [0,15]`（区间可证）。
- `plus ∈ [1,16]`，所以 `plus` 的最高有效位（第 4 位）只有在 `plus == 16` 时才为 1，`top` 大概率恒为 0（除 `plus == 16` 时 `top` 的低 4 位为 1——需精确分析，**待本地验证** `top` 是否被证明恒 0）。
- 若分析能证明 `top` 恒 0，则 `ret top` 可被替换为常量 0——这是「分析驱动」化简的典型收益。

## 6. 本讲小结

- **分析与优化解耦**：优化 Pass 不自己推导信息，只向统一的 `QueryEngine` 接口提问；引擎给过近似的保守结论（`true` = 已证明，`false` = 无法确定），保证优化绝不误报。
- **`QueryEngine` 接口**分四类问题：位级已知值、取值区间、位间关系、条件化信息，是所有引擎的统一出口。
- **BDD 引擎**用二元决策图做位级精确分析，能抓到位间关系（蕴含、互斥），但通过 `ShouldEvaluate` 主动排除乘除模和过宽移位，并用 `TooManyPaths` 饱和防止指数爆炸；其底层是 `AbstractEvaluator`「一套原语 → 任意运算」的抽象解释骨架。
- **区间集合分析**用归一化的不相交区间集合 `IntervalSet` 近似取值范围，对算术友好，`RangeQueryEngine` 同时产出区间与已知位，正好补 BDD 的算术短板。
- **前向引擎** `ForwardingQueryEngine` 是全量转发的适配器基类（lint 守护「每个虚函数都要转发」），支撑条件化特化（如 `AssumingQueryEngine`）；**并集引擎** `UnionQueryEngine` 把多个引擎的能力取并，组成实际管线用的综合引擎。
- **BDD 与区间互补**：位级精确 vs 算术友好，生产中通常以并集方式同时使用。

## 7. 下一步学习建议

- **往下游看消费方**：选一个重度依赖查询引擎的 Pass 深读，例如 `xls/passes/narrowing_pass.cc`（它有 `SpecializedQueryEngines`，专门在 `select` 分支内做条件化分析）或 `xls/passes/arith_simplification_pass.cc` 全文，体会「分析结论如何逐条变成图改写」。
- **往数据结构深处看**：若想理解 BDD 的内部表示与等价/蕴含判定如何实现，读 `xls/data_structures/binary_decision_diagram.h`；区间运算的细节读 `xls/ir/interval.h` 与 `xls/ir/interval_set.cc`。
- **更多引擎**：读 `xls/passes/stateless_query_engine.h`（u4-l2 提到的只看局部上下文的弱引擎）、`xls/passes/context_sensitive_range_query_engine.h`（带上下文敏感的区间分析）、`xls/passes/bit_count_query_engine.h`（专门统计前导同号位），理解「不同精度/成本的引擎各司其职」。
- **衔接调度**：本讲的区间分析（如 `MaxUnsignedValue`、`KnownLeadingZeros`）也常作为第四单元后续「延迟模型与流水线调度」的输入之一，可在学 u4-l4/u4-l5 时回头对照。
