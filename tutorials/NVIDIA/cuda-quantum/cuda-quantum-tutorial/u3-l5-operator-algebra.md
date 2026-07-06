# 算符代数：spin / fermion / boson / matrix

## 1. 本讲目标

本讲承接 u3-l3 的 `spin_op` 与 `observe`，把视野从「泡利算符」扩展到 CUDA-Q **统一的算符代数体系**。读完本讲你应当能够：

- 说清 `spin_op`、`fermion_op`、`boson_op`、`matrix_op` 这四类算符在类型层面是**同一个模板的不同实例化**，而不是四套互不相干的代码。
- 用 `cudaq::spin`/`cudaq::fermion`/`cudaq::boson`/`cudaq::operators` 命名空间下的工厂函数像写数学式一样构造算符，并对其进行加减乘、化简（`canonicalize`）。
- 理解算符如何「求值」为矩阵（`to_matrix`）、如何「序列化」为字符串（`to_string`/`dump`），以及如何用 `matrix_handler::define` 注册自定义算符。
- 掌握一个关键边界：**原生算符代数只提供「向通用 `matrix_op` 归一」的类型转换**，而真正的费米子↔量子比特映射（Jordan–Wigner 等）属于化学领域（`cudaq.chemistry`），不是核心代数的职责。

本讲不重复 u3-l3 已经讲过的 `observe`/期望值流程，只聚焦「算符本身如何构造、运算、化简与序列化」。

## 2. 前置知识

本讲需要一点量子物理与线性代数的直觉，下面用最少的篇幅补齐。

**产生与湮灭算符（二次量子化）。** 在多体物理里，我们用产生算符 \(a^\dagger_p\) 与湮灭算符 \(a_q\) 描述在某个模式（轨道/格点）\(p\) 上添加或移除一个粒子。它们的核心性质由对易关系决定：

- **费米子**（电子等，受泡利不相容限制）满足**反对易关系**：
  \[
  \{a_p, a^\dagger_q\} = \delta_{pq},\qquad \{a_p, a_q\} = 0
  \]
  其中 \(\{A,B\}=AB+BA\)。反对易意味着交换两个算符要多出一个负号，这正是 Jordan–Wigner 变换里那一串 \(Z\) 门的来源。
- **玻色子**（声子、光子等，可叠加）满足**对易关系**：
  \[
  [b_p, b^\dagger_q] = \delta_{pq},\qquad [b_p, b_q] = 0
  \]
  其中 \([A,B]=AB-BA\)。

**粒子数算符** \(n_p = a^\dagger_p a_p\)，本征值是该模式上的粒子数。一个典型的电子结构哈密顿量长这样：
\[
H = \sum_{pq} h_{pq}\, a^\dagger_p a_q + \frac12 \sum_{pqrs} h_{pqrs}\, a^\dagger_p a^\dagger_q a_r a_s
\]

**泡利算符。** 对每个量子比特，\(I, X, Y, Z\) 满足 \(XY=iZ\)、\(YZ=iX\)、\(ZX=iY\)，且 \(X^2=Y^2=Z^2=I\)（u3-l3 已详细介绍）。

**为什么要做 Jordan–Wigner 变换？** 量子计算机的native 操作是泡利门，而化学哈密顿量天然是费米子算符。Jordan–Wigner 把每个费米算符映射到一串泡利算符，例如：
\[
a_p \mapsto \frac12 (X_p + iY_p)\prod_{j<p} Z_j
\]
其中 \(\prod_{j<p} Z_j\) 这条「\(Z\) 串」就是用来兑现费米子反对易关系的。本讲末尾的实践会涉及这一步。

**张量积与维度。** 一个作用在 \(N\) 个 2 能级自由度上的算符，其矩阵是 \(2^N\times 2^N\)。本讲里反复出现的 `dimensions`（维度映射）就是在告诉算符「每个自由度有几个能级」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [runtime/cudaq/operators.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators.h) | 算符代数的「总头文件」：声明 `sum_op<>`、`product_op<>` 两个核心模板类、各类工厂函数，并在文件末尾给出 `spin_op`/`fermion_op`/`boson_op`/`matrix_op` 的类型别名。 |
| [runtime/cudaq/operators/operator_leafs.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/operator_leafs.h) | 算符代数的「叶子层」：`scalar_operator`（系数，可常量可回调）、`operator_handler`（所有原子算符的抽象基类）、`commutation_relations`（对易关系，区分费米/玻色）。 |
| [runtime/cudaq/operators/spin_op.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/spin_op.cpp) | `spin_handler` 的实现：泡利算符的位编码、按位乘法表、矩阵生成、`spin::` 工厂函数。 |
| [runtime/cudaq/operators/fermion_op.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/fermion_op.cpp) | `fermion_handler` 的实现：费米算符的四象限位编码、按位矩阵乘法、`fermion::` 工厂函数。 |
| [runtime/cudaq/operators/matrix_op.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/matrix_op.cpp) | `matrix_handler` 的实现：通用算符、**把任意其它 handler 转成 matrix handler 的转换构造函数**、自定义算符注册（`define`/`instantiate`）、预定义算符（number/parity/position/...）。 |
| [python/cudaq/operators/__init__.py](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/operators/__init__.py) | Python 端入口：导出 `spin`/`fermion`/`boson` 子模块、`SuperOperator`、自定义算符、算术运算与求值框架。 |

> 阅读建议：先看 `operators.h` 末尾的类型别名（最直观），再看 `operator_leafs.h` 的基类，最后挑 `spin_op.cpp` 与 `matrix_op.cpp` 精读。`fermion_op.cpp` 与 `boson_op.cpp` 结构高度对称，理解其一即可。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**算符类型体系**、**代数运算与化简**、**求值与序列化**。

### 4.1 算符类型体系

#### 4.1.1 概念说明

CUDA-Q 的算符代数建立在两个模板类之上：

- `product_op<HandlerTy>`：一个**单项**，是若干原子算符的乘积，再乘一个标量系数。对应数学里的 \(c \cdot O_1 O_2 \cdots O_k\)。
- `sum_op<HandlerTy>`：一个**多项和**，是若干 `product_op` 的线性组合。对应数学里的 \(\sum_i c_i\, P_i\)。

模板参数 `HandlerTy` 决定「这是哪一种粒子的算符」。CUDA-Q 内建四种 handler（原子算符类型），并在头文件末尾起好别名：

[operators.h:1888-1916](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators.h#L1888-L1916) —— 把 `sum_op<matrix_handler>` 等实例化分别起名为 `matrix_op`/`spin_op`/`boson_op`/`fermion_op`，并给出对应的 `*_op_term`（单项别名）。

```cpp
typedef sum_op<matrix_handler> matrix_op;       // 通用矩阵算符
typedef sum_op<spin_handler>   spin_op;         // 泡利/自旋算符（u3-l3 主角）
typedef sum_op<boson_handler>  boson_op;        // 玻色算符
typedef sum_op<fermion_handler> fermion_op;     // 费米算符
```

> 关键结论：**四类算符不是四套实现，而是同一套 `sum_op`/`product_op` 模板套上不同 `HandlerTy` 的产物**。所以它们的构造、运算、化简、序列化代码几乎完全共享；差异只在 handler 自身（编码、对易关系、矩阵定义）。

每个 handler 提供一组「工厂函数」用来生成最常用的原子算符，集中声明在：

[operators.h:700-761](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators.h#L700-L761) —— 按 handler 分组的静态工厂函数。

| Handler | 工厂函数（按 target） | 数学对应 |
|---------|----------------------|----------|
| `spin_handler` | `i / x / y / z`（单项），`plus / minus`（求和） | \(I,X,Y,Z,\;\sigma^\pm=\frac12(X\pm iY)\) |
| `fermion_handler` | `create / annihilate / number` | \(a^\dagger,\;a,\;n=a^\dagger a\) |
| `boson_handler` | `create / annihilate / number / position / momentum` | \(b^\dagger,b,n,\;x,p\) |
| `matrix_handler` | `number / parity / position / momentum / squeeze / displace` | 通用、可自定义的矩阵算符 |

注意 `HANDLER_SPECIFIC_TEMPLATE(...)` 宏（[operators.h:28-32](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators.h#L28-L32)）：它用 SFINAE 保证 `spin_op::create(...)` 这样的调用根本编译不过——只有对应 handler 实例化时工厂函数才存在。这是类型系统帮你「写错粒子种类就编不过」的守护。

#### 4.1.2 核心流程

构造一个算符的典型流程：

1. 用某命名空间下的工厂函数生成原子算符（`product_op`），例如 `fermion::create(0)`。
2. 用 `*`、`+`、`-` 与标量把它们组合成更复杂的表达式；运算符重载会自动把单项升格为和式（`product_op + product_op → sum_op`）。
3. 得到的 `sum_op`/`product_op` 是一棵**表达式树**，并不会立即算出矩阵。

handler 自身只存「最小的、可判等的信息」。例如：

- `spin_handler` 存 `{op_code, degree}`，`op_code` 取 \(0/1/2/3\) 对应 \(I/Z/X/Y\)（见 [spin_op.cpp:23-31](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/spin_op.cpp#L23-L31) 与构造函数 [spin_op.cpp:88-103](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/spin_op.cpp#L88-L103)）。
- `fermion_handler` 存 `{op_code, commutes, degree}`，并用一个 4 比特整数编码一个 \(2\times2\) 矩阵（见下一节的位编码）。

判等只比这些字段，例如 `fermion_handler::operator==` 只比 `degree` 与 `op_code`（[fermion_op.cpp:280-284](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/fermion_op.cpp#L280-L284)）。

#### 4.1.3 源码精读

**系数抽象 `scalar_operator`。** 每个 `product_op` 都带一个系数，它不只是个 `double`，而是一个「**常量或回调**」的 `variant`：

[operator_leafs.h:24-30](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/operator_leafs.h#L24-L30) —— `scalar_operator` 内部用 `std::variant<std::complex<double>, scalar_callback>` 存值，默认是 `1.`。

这意味着系数可以是**含参函数**（例如随时间变化的 Rabi 频率 \(\Omega(t)），求值时再带入参数字典。这一点在动力学（u7-l1）里会被反复利用。

**对易关系 `commutation_relations`。** 区分费米/玻色的关键：

[operator_leafs.h:441-446](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/operator_leafs.h#L441-L446) —— 默认（玻色）对易组 id 为 `-1`，费米对易组 id 为 `-2`，作为「编译期常量」挂在 handler 上。

[operator_leafs.h:308-342](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/operator_leafs.h#L308-L342) —— `commutation_relations` 内部有一张 `exchange_factors` 表：交换两个**作用在不同自由度上**的同组算符时要乘的因子（玻色为 \(+1\)、费米为 \(-1\)）。这正是反对易关系在代码里的落点。

**抽象基类 `operator_handler`。** 所有 handler 都实现这个接口：

[operator_leafs.h:354-484](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/operator_leafs.h#L354-L484) —— 定义了 `unique_id()`、`degrees()`、`to_matrix()`、`to_string()` 等纯虚函数，以及全局的 `canonical_order = std::less<std::size_t>()`（[operator_leafs.h:437](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/operator_leafs.h#L437)），规定自由度总是按升序排列。

#### 4.1.4 代码实践（源码阅读型）

**目标：** 通过阅读 `fermion_op.cpp` 的构造函数，弄清「`fermion::create(0)` 到底存了什么」。

**步骤：**

1. 打开 [fermion_op.cpp:118-132](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/fermion_op.cpp#L118-L132)，阅读 `fermion_handler(std::size_t target, int op_id)`。注意它把外部约定的 `op_id`（`1=create, 2=annihilate, 3=number`）翻译成内部 `op_code`（`4=Ad, 2=A, 8=N`）。
2. 对照 [fermion_op.cpp:37-60](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/fermion_op.cpp#L37-L60) 的 `op_code_to_string`，确认每个 `op_code` 的可读名（`Ad/A/N/(1-N)/I/0`）。
3. 打开 [fermion_op.cpp:300-310](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/fermion_op.cpp#L300-L310)，确认 `cudaq::fermion::create(target)` 只是把 `fermion_handler::create(target)` 包进一个 `product_op`。

**需要观察的现象：** 用户层的「`create/annihilate/number`」（语义命名）与内部存储的「`4/2/8`」（位编码）是两套名字；翻译发生在构造函数里。

**预期结果：** 你应当能回答：`fermion::annihilate(3)` 在内存里是一个 `product_op<fermion_handler>`，其 handler 的 `op_code=2`、`degree=3`、`commutes=false`。

#### 4.1.5 小练习与答案

**练习 1.** `spin_op`、`boson_op`、`fermion_op`、`matrix_op` 是四个独立的类吗？它们的关系是什么？

> **答：** 不是独立的类。它们都是同一个模板 `sum_op<HandlerTy>` 在不同 `HandlerTy` 下的 `typedef`（[operators.h:1896-1916](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators.h#L1896-L1916)）。对应的单项别名 `*_op_term` 则是 `product_op<HandlerTy>`。共享模板意味着构造、运算、化简、序列化的代码只写一遍。

**练习 2.** 为什么 `spin_op::create(0)` 会编译失败？

> **答：** 工厂函数用 `HANDLER_SPECIFIC_TEMPLATE(...)`（[operators.h:28-32](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators.h#L28-L32)）做了 SFINAE 约束：`create` 只在 `HandlerTy == fermion_handler`（或 `boson_handler`）时才存在。对 `spin_handler` 实例化时该重载被剔除，调用即编译错误。这是类型系统在编译期就阻止「把产生算符安到自旋上」这类语义错误。

---

### 4.2 代数运算与化简

#### 4.2.1 概念说明

算符代数的「代数」二字，指的是 `+`、`-`、`*`（以及一元负号、`+=`、`*=` 等）这一整套运算符重载，让你可以像写数学式一样写算符表达式，例如：

```cpp
// 一个最小两站点电子模型（紧致形式）
auto H = 0.5 * fermion::create(0) * fermion::annihilate(1)
       + 0.5 * fermion::create(1) * fermion::annihilate(0);
```

运算结果会**自动升格类型**：

- 单项 `*` 单项 → 单项（`product_op * product_op → product_op`）。
- 任何 `+`/`-` → 和式（结果至少是 `sum_op`）。
- 不同 `HandlerTy` 的算符做运算 → 一律归一到通用 `matrix_op`（详见 4.3）。

「化简」由两个函数承担：

- `canonicalize()`：把表达式整理成**规范形**——同一自由度上的原子算符按 `canonical_order` 排序、合并相邻同类项、**消去恒等算符 \(I\)**、必要时给缺失的自由度补上 \(I\)（让所有项作用在同样的自由度集合上）。
- `trim(tol)`：丢掉系数绝对值小于 `tol` 的项（数值清理）。

#### 4.2.2 核心流程

两个 handler 相乘时，对同一自由度上的算符要执行**真正的算符乘法**，由 handler 的 `inplace_mult` 完成。这是代数化简的引擎。

**泡利算符的乘法表**（[spin_op.cpp:48-61](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/spin_op.cpp#L48-L61)）：用 `op_code`（\(I=0,Z=1,X=2,Y=3\)）的 XOR 表示乘积算符，再用三条规则确定相位因子：

```
若 任一方为 I 或两者相同      → 因子 = +1
否则 若 op+1==other 或 op-2==other → 因子 = +i
否则                              → 因子 = -i
结果 op_code = this->op_code ^ other.op_code
```

读者可验证：\(X(2)\cdot Y(3)\)：`op+1==other` 成立 → 因子 \(+i\)，结果 `2^3=1=Z`，即 \(XY=iZ\) ✓；\(Y(3)\cdot X(2)\)：落入 else → 因子 \(-i\)，结果 `3^2=1=Z`，即 \(YX=-iZ\) ✓。

**费米算符的乘法**更精巧：handler 把一个 \(2\times2\) 矩阵压成一个 4 比特整数（每个比特对应矩阵的一个象限），于是矩阵乘法退化成「与（AND）当乘、异或（XOR）当加」：

[fermion_op.cpp:77-102](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/fermion_op.cpp#L77-L102) —— `inplace_mult` 用位运算实现 \(2\times2\) 矩阵乘法，注释明确写道「Multiplication becomes a bitwise and, addition becomes an exclusive or」。

位编码（见 `fermion_op.h` 顶部注释与 [fermion_op.cpp:26-32](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/fermion_op.cpp#L26-L32) 的合法 op_code 集合 `{0,1,2,4,8,9}`）：

| op_code | 位 | 含义 | 矩阵 |
|---------|----|------|------|
| `0` | `0000` | 零算符 | 全 0 |
| `1` | `0001` | \((1-N)\)，投影到空 | \(\mathrm{diag}(1,0)\) |
| `2` | `0010` | \(A\)（湮灭） | 右上为 1 |
| `4` | `0100` | \(A^\dagger\)（产生） | 左下为 1 |
| `8` | `1000` | \(N\)（粒子数） | \(\mathrm{diag}(0,1)\) |
| `9` | `1001` | \(I\) | 单位阵 |

> 这种「把矩阵压成位、乘法压成位运算」的设计，让算符表达式的整理几乎不分配内存、不调用线性代数库，是性能关键。

#### 4.2.3 源码精读

**运算符重载的总量。** `sum_op` 与 `product_op` 各自声明了几十个 `operator+/-/*`（含左值/右值、与标量/单项/和式的组合）。以 `sum_op` 为例，其右值/左值重载集中在 [operators.h:317-479](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators.h#L317-L479)，左侧算术（标量在左边）用 `friend` 模板实现（[operators.h:559-619](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators.h#L559-L619)）。重点不在记每个重载，而是理解**所有重载都最终落到 `insert` + `aggregate_terms` 上**：

[operators.h:66-73](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators.h#L66-L73) —— `sum_op` 的私有 `insert` 与 `aggregate_terms`，负责把新项并入和式、并在可能时与已有项合并。配合 `term_map`（项 id → 项下标）实现 O(1) 查重。

**`canonicalize` 的两个签名。**

[operators.h:779-787](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators.h#L779-L787) —— 无参版本（消去 \(I\)、整理顺序），以及带 `degrees` 集合的版本（把表达式扩展到作用于给定自由度集合，缺位补 \(I\)）。后者是 `observe` 在做术语合成前的常用预处理。

**`num_terms` 与遍历。**

[operators.h:156-161](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators.h#L156-L161) —— `operator[]` 按 index 取项；`sum_op` 还提供 `begin()/end()`（[operators.h:151-154](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators.h#L151-L154)），可写范围 for 逐项遍历。

#### 4.2.4 代码实践

**目标：** 用 `spin` 工厂构造一个表达式，化简后观察算符个数变化，验证 `inplace_mult` 的相位规则。

**步骤（C++，需链接 cudaq 运行时）：**

```cpp
// 示例代码：仅演示 API，不保证独立编译
#include <cudaq/spin_op.h>
using namespace cudaq;

int main() {
  // X0 * Y0  —— 同一比特上的两个泡利相乘，结果应为单项 i*Z0
  auto expr = spin::x(0) * spin::y(0);   // product_op<spin_handler>
  printf("ops before = %zu\n", expr.num_ops());   // 期望 2（X0, Y0）
  expr.canonicalize();                            // 合并同自由度泡利
  printf("ops after  = %zu\n", expr.num_ops());   // 期望 1（Z0）
  expr.dump();                                    // 观察系数应含相位 i
  return 0;
}
```

**手算复核（用 4.2.2 的乘法表）：** \(X(2)\cdot Y(3)\)：`op+1==other` 成立 → 相位 \(+i\)，结果 op_code \(2\oplus3=1\) 即 \(Z\)，故 \(X_0Y_0 = i\,Z_0\)。canonicalize 后只剩一个算符 \(Z_0\)，系数含相位 \(i\)。

> 注意别掉进陷阱：`x(0)*y(0)*z(0)` 会进一步变成 \((iZ)\cdot Z = iI\)，canonicalize 把恒等算符 \(I\) 也消去，最后只剩**纯标量** \(i\)（没有任何泡利剩余），`num_ops()` 会变为 0。这也正好演示了「canonicalize 消去 \(I\)」的行为。

**预期结果：** `x(0)*y(0)` 化简后 `num_ops()==1`，`dump()` 显示剩余算符为 `Z0`、系数含 \(i\)。若结果不符，请用 \(XY=iZ,\;XZ=-iY,\;YZ=iX\) 复核。

> 若本地暂无 C++ 构建环境，可改用 Python：`import cudaq; from cudaq import spin; e = spin.x(0)*spin.y(0); e.canonicalize(); e.dump()`，行为一致。该现象「待本地验证」。（`num_ops`/`num_terms` 是 `product_op`/`sum_op` 上的方法，见 [operators.h:1060](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators.h#L1060) 与 [operators.h:177](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators.h#L177)。）

#### 4.2.5 小练习与答案

**练习 1.** 为什么 `fermion::create(0) * fermion::annihilate(0)` 不会真的去调用任何矩阵库？

> **答：** 因为 `fermion_handler::inplace_mult`（[fermion_op.cpp:77-102](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/fermion_op.cpp#L77-L102)）用**位运算**完成了 \(2\times2\) 矩阵乘法：矩阵的每个象限被压成一个比特，乘法变成 AND、加法变成 XOR。只有真正调用 `to_matrix` 时才会构造稠密矩阵。

**练习 2.** `canonicalize()` 与 `trim(tol)` 的区别是什么？

> **答：** `canonicalize`（[operators.h:779-787](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators.h#L779-L787)）做的是**符号化整理**：排序、合并同类项、消去 \(I\)、补齐自由度，不改变数学含义。`trim`（[operators.h:773-776](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators.h#L773-L776)）做的是**数值清理**：丢弃系数绝对值小于容差的项，会（微小地）改变值，常用于浮点噪声抑制。

---

### 4.3 求值与序列化

#### 4.3.1 概念说明

算符表达式树最终要被「兑现」成两种有用的形式：

- **求值为矩阵**（`to_matrix`）：给定每个自由度的能级数（`dimensions`）和系数参数（`parameters`），算出整个算符的稠密（或稀疏）矩阵。这是连接到 `observe`/`evolve`/数值方法的桥梁。
- **序列化为字符串**（`to_string`/`dump`）：把算符打印成可读文本，便于调试与持久化。

此外，`matrix_handler` 还提供两条独特能力：

1. **自定义算符**：用 `matrix_handler::define(name, dims, callback)` 注册一个由回调生成矩阵的新算符，再用 `instantiate(name, degrees)` 实例化它。
2. **类型归一**：把任意其它 handler（spin/fermion/boson）转成通用 `matrix_handler`，使混合粒子类型的表达式得以统一处理。

> ⚠️ **重要边界：** 原生算符代数**只支持「向 `matrix_op` 归一」的类型转换**，**没有**内置的「费米子 ↔ 泡利（Jordan–Wigner / Bravyi–Kitaev）」映射。后者属于化学领域 `cudaq.chemistry`，底层调用 openfermion（见 [python/cudaq/domains/chemistry/__init__.py:68](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/domains/chemistry/__init__.py#L68) 的 `of.jordan_wigner(...)`）。本讲综合实践会分别演示这两条路径。

#### 4.3.2 核心流程

**求值流程。** `sum_op::to_matrix`（[operators.h:297-301](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators.h#L297-L301)）做的是：对每一项 `product_op`，先用 `canonical_form` 收集涉及到的自由度并校验/填充维度，再调 handler 的 `to_matrix` 得到该项的矩阵，最后把它们张量积起来再相加。每个 handler 的 `canonical_form` 同时承担「校验维度合法」的职责——例如费米/自旋算符强制每个自由度的维度必须是 2：

[fermion_op.cpp:66-75](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/fermion_op.cpp#L66-L75) —— 若该自由度已有维度且不等于 2，直接抛 `runtime_error("dimension for fermion operator must be 2")`。

**自定义算符流程。** `define` 把一个 `Definition`（名字 + 期望维度 + 矩阵回调 + 可选参数说明）插入全局表 `defined_ops`：

[matrix_op.cpp:51-62](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/matrix_op.cpp#L51-L62) —— 同名重复定义会抛错。预定义算符 `number/parity/position/momentum/squeeze/displace` 都是用同一个 `define` 注册的（见 [matrix_op.cpp:447-589](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/matrix_op.cpp#L447-L589)）。例如 `number` 的回调把 `diag(0,1,2,...,d-1)` 填进矩阵（[matrix_op.cpp:447-464](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/matrix_op.cpp#L447-L464)）。

`instantiate` 校验自由度顺序符合 `canonical_order`，然后返回一个 `product_op<matrix_handler>`：

[matrix_op.cpp:105-127](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/matrix_op.cpp#L105-L127)。

**类型归一流程。** `matrix_handler` 有一个**转换构造函数**，能吃下任何 handler 子类：

[matrix_op.cpp:256-303](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/matrix_op.cpp#L256-L303) —— 它把源 handler 的 `to_string(false)`（如费米湮灭算符的 `"A"`）拼成新 `matrix_handler` 的 `op_code`，并**注册一个矩阵回调**，回调里转回去调源 handler 的 `to_matrix`。也就是说：转换后算符的「身份」由字符串名保留，「行为」由回调按需重建。

因此当你写 `fermion::annihilate(0) + spin::x(1)` 这种**混合粒子类型**的表达式时，结果会被自动归一到 `matrix_op`。Python 端通过隐式转换实现：

[py_matrix_op.cpp:608-613](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/runtime/cudaq/operators/py_matrix_op.cpp#L608-L613) —— 注册了 `spin_op/boson_op/fermion_op → matrix_op` 的隐式转换，以及显式构造 `MatrixOperator(spin_op/fermion_op/boson_op)`（[py_matrix_op.cpp:150-152](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/runtime/cudaq/operators/py_matrix_op.cpp#L150-L152)）。

#### 4.3.3 源码精读

**`matrix_handler::to_matrix` 的「按需校验 + 回调生成」。**

[matrix_op.cpp:362-392](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/matrix_op.cpp#L362-L392) —— 遍历算符作用的自由度，逐个查 `defined_ops` 里的 `expected_dimensions`：`<=0` 表示「维度由用户提供」，缺失则抛错；`>0` 表示算符自带固定维度（如自旋恒为 2），冲突则抛错。最后调 `generate_matrix` 真正构造矩阵。

**type_prefix：为什么转换后名字没有奇怪前缀。**

[matrix_op.cpp:38-49](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/matrix_op.cpp#L38-L49) —— 对 `spin_handler`/`boson_handler`/`fermion_handler` 三种内建类型，`type_prefix` 特化为空串，所以转换后的 `op_code` 就是干净的可读名（`X`/`A`/`Ad`/`N`/...）。对自定义类型则带 `typeid(T).name()` 前缀以防重名。

**Python 端的工厂与求值入口。**

[python/cudaq/operators/__init__.py:9-16](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/operators/__init__.py#L9-L16) —— 导出 `boson/fermion/spin` 子模块、`SuperOperator`、自定义算符（`custom`）、`definitions`、算术框架 `OperatorArithmetics`，并显式 import `expressions`（否则 `evaluate` 不会被挂到算符类上）。

#### 4.3.4 代码实践

**目标：** 注册一个自定义矩阵算符 `MYX`（作用在 2 能级上，矩阵为 \(X\)），实例化并求值。

**步骤（Python）：**

```python
# 示例代码：演示 API
import cudaq
from cudaq import operators
import numpy as np

# 1. 定义一个自定义算符：期望维度 [2]，回调返回 2x2 矩阵
operators.define("MYX", [2], lambda dim: np.array([[0, 1], [1, 0]], dtype=complex))

# 2. 实例化并求值
op = operators.instantiate("MYX", [0])
mat = op.to_matrix({0: 2})          # 维度映射：自由度 0 有 2 能级
print(mat)                          # 期望打印 [[0,1],[1,0]]
```

**需要观察的现象：** 自定义算符的矩阵与你给定的回调一致；再次 `define("MYX", ...)` 会因重名抛错（[matrix_op.cpp:59-61](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/matrix_op.cpp#L59-L61)）。

**预期结果：** 打印出 \(2\times2\) 的 \(X\) 矩阵。该结果「待本地验证」（取决于 `operators.define/instantiate` 在当前安装版本的确切签名，可对照 [test_conversions.py:363-367](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/tests/operator/test_conversions.py#L363-L367) 的用法）。

#### 4.3.5 小练习与答案

**练习 1.** 把 `fermion_op` 显式转成 `matrix_op` 后，原费米算符的「反对易」信息还在吗？

> **答：** 部分在、部分不在。转换构造函数（[matrix_op.cpp:256-303](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/matrix_op.cpp#L256-L303)）会把源 handler 的 `commutation_group` 与 `commutes_across_degrees` 一并带进新的 `matrix_handler`（通过 `commutation_behavior`），所以「这是费米子类算符」的**组标记**保留；但具体算符已退化为「按名查表 + 回调生成矩阵」，跨自由度的反对易符号要在后续乘法里靠这个组标记重新演绎，而**不是**靠矩阵本身的有限尺寸。详见 [fermion_op.cpp:37-60](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/fermion_op.cpp#L37-L60) 注释里关于「有限尺寸效应」的说明。

**练习 2.** 为什么 CUDA-Q 核心代数不直接提供 Jordan–Wigner 变换？

> **答：** 因为 Jordan–Wigner 是一个**特定的物理映射**（费米子 → 量子比特泡利串），与化学积分、轨道基组等强耦合，属于「领域知识」而非「通用算符代数」。核心代数只提供通用的类型归一（→ `matrix_op`）与矩阵求值，把 JW 这类映射留给 `cudaq.chemistry`（经 openfermion，[chemistry/__init__.py:68](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/domains/chemistry/__init__.py#L68)）。这是一种有意的关注点分离。

---

## 5. 综合实践

**任务：** 用 `fermion_op` 构造一个简化版两站点电子哈密顿量，分别走「原生代数归一」与「Jordan–Wigner 到 spin_op」两条路径，对比化简后的项数。

**数学背景。** 取一个最小的紧束缚 + 在位能模型：
\[
H = \varepsilon_0\, n_0 + \varepsilon_1\, n_1 + t\,(a^\dagger_0 a_1 + a^\dagger_1 a_0)
\]
其中 \(n_p=a^\dagger_p a_p\)。这是一个作用在 2 个费米模式（4 个量子态）上的算符。

**路径 A —— 原生代数归一（无外部依赖，核心代数）。**

```python
# 示例代码
import cudaq
from cudaq import fermion, operators

eps0, eps1, t = 0.5, 0.5, 1.0
H = ( eps0 * fermion.number(0) + eps1 * fermion.number(1)
    + t   * fermion.create(0)  * fermion.annihilate(1)
    + t   * fermion.create(1)  * fermion.annihilate(0) )

print("fermion term_count =", H.term_count)   # 期望 4
H.canonicalize()
print("after canonicalize =", H.term_count)   # 通常仍为 4（无符号重复合并）

# 关键步骤：原生类型归一 fermion_op -> matrix_op
Hm = operators.MatrixOperator(H)              # 见 py_matrix_op.cpp:150-152
print("matrix_op type =", type(Hm).__name__)
mat = Hm.to_matrix({0: 2, 1: 2})              # 4x4 复矩阵
print("matrix shape =", len(mat), len(mat[0]) if mat else 0)
```

**观察要点：**
1. `fermion.number(0)` 等工厂函数生成 `product_op<fermion_handler>`；相加后得到 `fermion_op`（`sum_op<fermion_handler>`）。
2. `operators.MatrixOperator(H)` 触发 [matrix_op.cpp:256-303](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/matrix_op.cpp#L256-L303) 的转换构造函数，把每个费米算符包成带矩阵回调的 `matrix_handler`。
3. `to_matrix({0:2,1:2})` 给出 \(4\times4\) 矩阵；可手算验证其本征值为 \(\{0,\, \varepsilon_0,\varepsilon_1,\, \varepsilon_0+\varepsilon_1\}\) 与紧束缚项贡献的 \(\pm t\) 的组合。

**路径 B —— Jordan–Wigner 到 spin_op（需 openfermion，领域层）。**

CUDA-Q 核心代数不做 JW，但 `cudaq.chemistry` 经 openfermion 提供：

```python
# 示例代码：需要 openfermion + openfermionpyscf
import cudaq
# 这条路径通常用于真实分子；这里仅示意调用形态
from cudaq import chemistry
spin_op, molecule = chemistry.create_molecular_hamiltonian(
    geometry=[('H', (0., 0., 0.)), ('H', (0., 0., 0.7474))],
    basis='sto-3g', charge=0, multiplicity=1)
print("spin_op term_count =", spin_op.term_count)   # H2/STO-3G 通常为 15 项
```

该路径内部正是调用 `of.jordan_wigner(hamiltonian)`（[chemistry/__init__.py:68](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/domains/chemistry/__init__.py#L68)），把 `openfermion` 的 `FermionOperator` 映射成 `SpinOperator`。它**不会**经过本讲的 `fermion_op` 类型——这恰好印证了「JW 是领域层关注点」的边界。

**预期结果 / 待本地验证：**
- 路径 A：`fermion term_count = 4`，`matrix_op` 的 `to_matrix` 返回 \(4\times4\) 矩阵。本机无 openfermion 时只验证路径 A 即可。
- 路径 B：H₂/STO-3G 的 `spin_op.term_count` 通常为 15（4 自旋轨道下的标准结果），具体数值「待本地验证」。

**把知识串起来：** 路径 A 让你亲手用了本讲的三个最小模块——**类型体系**（`fermion::number/create/annihilate`）、**代数运算与化简**（`+`、`*`、`canonicalize`、`term_count`）、**求值与序列化**（`MatrixOperator` 转换、`to_matrix`）。路径 B 则让你看清核心代数与领域层（化学/JW）的边界在哪里。

## 6. 本讲小结

- **一套模板，四种算符：** `spin_op`/`fermion_op`/`boson_op`/`matrix_op` 都是 `sum_op<HandlerTy>` 的别名（[operators.h:1896-1916](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators.h#L1896-L1916)），共享构造、运算、化简、序列化代码；差异封装在各自的 handler 里。
- **系数可含参：** `product_op` 的系数是 `scalar_operator`，内部是「常量或回调」的 `variant`（[operator_leafs.h:24-30](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/operator_leafs.h#L24-L30)），支撑含时哈密顿量。
- **化简靠位运算：** 同自由度上的算符乘法由 handler 的 `inplace_mult` 完成——泡利用 XOR+相位规则（[spin_op.cpp:48-61](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/spin_op.cpp#L48-L61)），费米子把 \(2\times2\) 矩阵压成 4 比特做 AND/XOR 乘法（[fermion_op.cpp:77-102](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/fermion_op.cpp#L77-L102)）；`canonicalize` 负责排序、合并、消去 \(I\)。
- **求值即生成矩阵：** `to_matrix` 用每个 handler 的 `canonical_form` 校验/填充维度，再张量积相加；费米/自旋算符强制维度为 2。
- **通用算符 + 自定义：** `matrix_handler` 是通用容器，`define`/`instantiate` 让你注册任意矩阵算符（[matrix_op.cpp:51-127](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/matrix_op.cpp#L51-L127)）。
- **类型归一的边界：** 原生代数只把异类算符**归一到 `matrix_op`**（[matrix_op.cpp:256-303](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/matrix_op.cpp#L256-L303)）；**Jordan–Wigner 等费米↔泡利映射不在核心代数里**，而在 `cudaq.chemistry`（openfermion）。

## 7. 下一步学习建议

- **回到 u3-l3 / u3-l4：** 用本学的 `spin_op` 构造能力，去 `observe` 一个真正的化学哈密顿量，再用 `optimizer`+`gradient` 做 VQE。你会发现 `observe` 内部正是对 `spin_op` 的每一项求期望。
- **进入 u7-l1（动力学）：** `cudaq::evolve` 大量使用本讲的算符体系——含时系数（`scalar_operator` 回调）、玻色/费米/矩阵算符混合，以及 `super_op`（[operators.h:1765](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators.h#L1765)，作用在密度矩阵上的超算符）。
- **自定义扩展：** 想为新物理自由度建模时，优先用 `matrix_handler::define` 注册自定义算符（无需改源码）；只有需要全新对易关系时才考虑更深层的扩展。
- **阅读建议：** 通读 [operator_leafs.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/operator_leafs.h) 的 `operator_handler` 抽象与 `commutation_relations`，再对照 [sum_op.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/sum_op.cpp) 里 `aggregate_terms`/`canonicalize` 的实现，能形成完整的「表达式树如何被整理」的图景。
