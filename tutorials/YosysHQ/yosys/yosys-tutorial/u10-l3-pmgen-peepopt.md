# pmgen 模式匹配与 peepopt 窥孔优化

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 **pmgen 是什么、解决什么问题**：它是一个「模式匹配生成器」，把一份声明式的 `.pmg` 文件编译成一个头文件级 C++ 子图匹配器，让优化 pass 用几十行描述就能写出原本要几百行手写回溯搜索的「子图查找 + 替换」逻辑。
- 读懂 **`.pmg` 文件格式**：`pattern` / `state` / `udata` / `match…endmatch` / `code…endcode` / `subpattern`，以及 `select`、`index`、`filter` 三档由快到慢的匹配条件。
- 跟踪 **peepopt 如何消费生成的匹配器**：`PeepoptPass` 用 `pm.setup()` 建索引、用 `pm.run_muldiv()` 等跑各条规则、靠 `did_something` 循环到不动点；并能指着 `peepopt_muldiv.pmg` 讲清「`(A*B)/B → A`」这条规则是怎么被描述出来的。
- 准确说出 **pmgen 在 Yosys 里的真实用户**（peepopt 全套规则 + 各厂商 DSP/SRL/carry 推断 pass），并纠正一个常见误解：`alumacc` 并不使用 pmgen。

本讲依赖 [u6-l3 opt：网表优化大流程](u6-l3-opt.md)（你需要先知道 `opt` 是一条编排型 pass、网表由 `$` 单元构成、`did_something` 不动点循环是怎么回事），并承接 [u3-l4 内部单元库](u3-l4-internal-cell-library.md)（`$mul` / `$div` / `$shift` 等单元的端口约定）。本讲与同单元的 [u10-l2 functional IR 与 AIG](u10-l2-functional-ir-aig.md) 并列：二者都是 RTLIL 之上的「派生工具链」，只是一个面向形式化窄化、一个面向优化改写。

---

## 2. 前置知识

### 2.1 什么是「窥孔优化」（peephole optimization）

「窥孔」比喻 Optimization pass 透过一个**小窗口**看局部网表，识别出「能被更优等价结构替换」的固定子图，然后就地改写。它的特点是：

- **局部**：只看窗口内几个相邻单元，不做全局分析。
- **模式驱动**：每条规则写成「左边一个子图模式 → 右边一个替换」。例如 `(A*B)/B` 在整数除法可除尽时等于 `A`，可以删掉一对乘除。
- **可叠加**：一个 pass 可以内置几十条这样的规则，逐条尝试。

难点不在「替换」本身（改 RTLIL 的 `connect` / `remove` 在 [u3-l1](u3-l1-module-cell-wire-deep.md) 已讲过），而在「**查找**」：在一个有成千上万个单元的网表里，高效地定位「某个 `$mul` 的输出恰好驱动某个 `$div` 的被除数」这种结构。手写这种带回溯的子图搜索既繁琐又易错——这正是 pmgen 要消除的样板代码。

### 2.2 子图同构与回溯搜索

子图匹配本质上是「子图同构」问题，是 NP 的。pmgen 采用最朴素也最通用的策略：**带回溯的递归搜索**。README 里直接说明了这一点：

> The algorithm used in the generated pattern matcher is a simple recursive search with backtracking.（[passes/pmgen/README.md:8-12](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/README.md#L8-L12)）

效率靠两件事保证：第一，`.pmg` 作者负责给出一个**好的单元匹配顺序**（先匹配约束最强的单元，便于尽早剪枝）；第二，pmgen 会把能预先算的东西在 `setup()` 阶段一次性建索引（用 hashlib 的 `dict`/`pool`），匹配时只做 O(1) 查表。这就是 `select`/`index`（快）与 `filter`（慢）三档区分的意义。

### 2.3 关键术语

- **`.pmg` 文件**：Pattern Matcher Generator 的输入，行导向的声明式描述。
- **状态变量（state）**：匹配过程中携带的中间结果（如「当前匹配到的 `$mul` 单元指针」「某段信号」），由生成器自动保存/恢复以支持回溯。
- **`port()` / `param()` / `nusers()`**：`.pmg` 里对 RTLIL 的便捷封装——`port(c, \Y)` 等价于 `sigmap(c->getPort(\Y))`（取端口并经 SigMap 归一化），`param(c, \A_SIGNED)` 取参数，`nusers(sig)` 返回驱动某信号的「不同 cell 数（端口/IO 各计一次）」。
- **`accept` / `reject` / `branch`**：`.pmg` 代码块里控制回溯的三个原语——`accept` 接受当前匹配（触发回调）、`reject` 回溯换一组候选、`branch` 探索另一条分支。

> 前置讲义回顾：`SigMap` 把同一信号在网表中的多种等价写法（改名、切片、拼接）归并到唯一「规范代表位」（见 [u3-l2](u3-l2-sigspec-sigtools.md)）。pmgen 生成的匹配器对每个 `port()` 都套了一层 `sigmap`，因此匹配是「按规范信号」而非「按字面 wire 指针」——这一点至关重要，否则切片/重命名后的网表会匹配失败。

---

## 3. 本讲源码地图

| 文件 | 职责 |
|---|---|
| [passes/pmgen/pmgen.py](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/pmgen.py) | **生成器本体**：读 `.pmg`、做词法重写 `rewrite_cpp`、用 `process_pmgfile` 解析成 block 列表、最后 `print(...)` 拼出 `*_pm.h`（含 `setup`/`run_*`/`block_*`） |
| [passes/pmgen/README.md](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/README.md) | `.pmg` 文件格式的**权威说明书**（pattern/state/match/code/subpattern/generate） |
| [passes/opt/peepopt.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/peepopt.cc) | **消费者**：`PeepoptPass`，`#include` 生成的 `peepopt_pm.h`，循环调用各 `run_*` 到不动点 |
| [passes/opt/peepopt_muldiv.pmg](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/peepopt_muldiv.pmg) | 最简单的一条规则：`(A*B)/B → A`，是读懂 `.pmg` 的最佳入口 |

配套文件（构建集成与示例）：

- [passes/opt/CMakeLists.txt](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/CMakeLists.txt)：用 `pmgen_command(peepopt …)` 声明「把 7 个 `.pmg` 合并生成 `peepopt_pm.h`」。
- [cmake/PmgenCommand.cmake](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/cmake/PmgenCommand.cmake)：`pmgen_command` 的 CMake 定义，本质是一个 `add_custom_command` 调 `python3 pmgen.py`。
- [passes/pmgen/test_pmgen.pmg](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/test_pmgen.pmg)：pmgen 自带的「特性展示」文件，演示了 `subpattern`/`slice`/`choice`/`generate` 等高级用法，配合 `test_pmgen.cc` 做自测。
- 同目录其它 6 条 peepopt 规则：`peepopt_muldiv_c.pmg`、`peepopt_shiftadd.pmg`、`peepopt_shiftmul_right.pmg`、`peepopt_shiftmul_left.pmg`、`peepopt_shiftpow2.pmg`、`peepopt_formal_clockgateff.pmg`。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**① pmgen 生成器**（`.pmg` 怎么变成 C++）、**② `.pmg` 模式语言**（怎么写一条规则）、**③ peepopt 应用**（生成的匹配器怎么被一个真实 pass 用起来）。

### 4.1 pmgen 生成器：从 `.pmg` 到 C++ 头文件

#### 4.1.1 概念说明

pmgen 的定位是一台「**专用的源到源翻译器**」：输入是一种小型 DSL（`.pmg`），输出是一份 header-only 的 C++ 头文件 `foobar_pm.h`，里面定义一个类 `foobar_pm`。这份头文件被需要做子图匹配的 pass 直接 `#include`，就像它是手写的一样。

这样做的好处是「**数据驱动**」：新增一条优化规则，不需要写 C++、不需要重新理解回溯算法、不需要碰 pass 的注册代码——只要写一段几十行的 `.pmg` 描述，构建系统会自动重新生成头文件。这与 techmap 用 Verilog 模板描述映射规则（见 [u6-l5](u6-l5-techmap-simplemap.md)）是同一种设计哲学：**把易变的知识从 C++ 代码里剥离成数据**。

README 一句话点明了输入输出：

> The program `pmgen.py` reads a `.pmg` (Pattern Matcher Generator) file and writes a header-only C++ library that implements that pattern matcher.（[passes/pmgen/README.md:4-6](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/README.md#L4-L6)）

#### 4.1.2 核心流程

pmgen.py 是一个 **~800 行的单文件 Python 脚本**，只用标准库（`re`/`sys`/`pprint`/`getopt`，见 [passes/pmgen/pmgen.py:3-6](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/pmgen.py#L3-L6)），不依赖 Yosys 本体。它的工作分三步：

```text
  ┌─────────────┐   process_pmgfile      ┌──────────────┐   print(...)       ┌──────────────┐
  │  foo.pmg    │ ───────────────────▶  │  blocks 列表  │ ────────────────▶  │  foo_pm.h    │
  │ (行导向 DSL) │   （解析 + rewrite_cpp）│  (dict 组成的) │   （逐行拼 C++）   │ (header-only)│
  └─────────────┘                        └──────────────┘                     └──────────────┘
                                            │
                                            └─ 每个块带 type: pattern / state / match / code / subpattern / final
```

1. **解析（`process_pmgfile`）**：逐行读 `.pmg`，按首关键字（`pattern`/`state`/`udata`/`match`/`code`/`subpattern`/`arg`/`fallthrough`）分派，把每一段压成一个 `dict`，组成一个有序的 `blocks` 列表（[passes/pmgen/pmgen.py:110-331](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/pmgen.py#L110-L331)）。
2. **词法重写（`rewrite_cpp`）**：嵌入在 `.pmg` 里的 C++ 代码段不能原样吐出，要做两类改写（见 4.1.3）。
3. **代码生成**：遍历 `blocks`，对每种 `type` 拼出对应的 C++：`pattern` 变成 `struct state_*_t`/`run_*`，`match` 变成索引声明 + `setup()` 建表 + `block_*` 查表，`code` 变成 `block_*` 里的一段带 `reject/accept/branch` 宏的代码（[passes/pmgen/pmgen.py:349-798](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/pmgen.py#L349-L798)）。

一个关键细节：**多文件合并**。当传给 pmgen 多个 `.pmg` 文件时，必须用 `-p <prefix>` 指定一个公共前缀；所有文件里的 `pattern` 会被合并进**同一个**类 `<prefix>_pm`。这正是 peepopt 的用法——7 条规则分别写在 7 个 `.pmg` 里，却生成同一个 `peepopt_pm` 类、共用一份 `setup()` 索引（[passes/pmgen/pmgen.py:31-38](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/pmgen.py#L31-L38)）。若只传单个文件、未给前缀，则以前缀取文件名（去 `.pmg`、去目录），生成的类名与文件同名。

#### 4.1.3 源码精读：`rewrite_cpp` 让 `.pmg` 里的 C++ 更好写

`.pmg` 的 `match`/`code`/`generate` 段里写的是「近似 C++」，pmgen 用 `rewrite_cpp` 把它改写成「合法 C++」再吐出。最关键的一处改写是**标识符内部化**：`.pmg` 里直接写 `$mul`、`\Y`、`\A_SIGNED` 这种 RTLIL 风格的 IdString，`rewrite_cpp` 会把它们扫描出来，转成 `id_d_mul`、`id_b_Y` 这样的成员变量名，并在类里声明对应的 `IdString` 成员（构造时初始化）：

```python
# passes/pmgen/pmgen.py:64-99  （节选）
if s[i] in ('$', '\\') and i + 1 < len(s):
    ...
    n = s[i:j+1]            # 例如 "$mul" 或 "\\Y"
    if n[0] == '$':
        v = "id_d_" + n[1:]  # → id_d_mul
    else:
        v = "id_b_" + n[1:]  # → id_b_Y
    ...
```

这样 `.pmg` 里写 `mul->type == $mul`，生成的 C++ 里就变成 `mul->type == id_d_mul`，而 `id_d_mul` 是匹配器类的一个 `IdString` 成员，构造时被初始化为 `"$mul"`。这一层让你在描述规则时可以用最自然的 RTLIL 字面量，而不必关心 `ID::` 宏或 `IdString` 构造语法（那是 [u3-l3/u3-l4](u3-l3-idstring-const-hashlib.md) 的话题）。

#### 4.1.4 源码精读：`setup()` 把 `select`/`index` 预算成索引

匹配器的效率核心在 `setup()`。pmgen 为每个 `match` 块生成一张索引表：

```python
# passes/pmgen/pmgen.py:380-396  为每个 match 块声明 dict 索引
for index in range(len(blocks)):
    block = blocks[index]
    if block["type"] == "match":
        index_types = [entry[0] for entry in block["index"]]
        ...
        print("  typedef std::tuple<{}> index_{}_key_type;".format(...), file=f)
        print("  dict<index_{}_key_type, vector<index_{}_value_type>> index_{};".format(...), file=f)
```

`setup(cells)` 遍历传入的全部 cell，对每个 cell **一次性**地：跑所有 `select`（不通过则跳过该 cell）、展开 `slice`/`choice`/`define`、用 `index` 左表达式计算键、把「cell + 展开变量」塞进对应键的 `vector`（[passes/pmgen/pmgen.py:496-549](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/pmgen.py#L496-L549)）。`setup()` 还顺带建了一张 `dict<SigBit, pool<Cell*>> sigusers`，供 `nusers()` 查询信号的用户数。

匹配时（`block_*` 里），对每个 `index` 用**右表达式**（是已匹配上游单元的函数）算出键，直接 `index_<n>.find(key)` 取候选 cell 列表——O(1) 查表，再在候选上跑较慢的 `filter`。所以 README 才强调：`select`/`index` 是快操作，应当优先用；`filter` 留给无法预先索引的条件（[passes/pmgen/README.md:159-171](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/README.md#L159-L171)）。

#### 4.1.5 源码精读：`run_*` 与 `block_*` 的回溯骨架

每个 `pattern` 生成一族 `run_<name>(callback)`，内部初始化状态变量、把 `accept_cnt` 清零，然后调用入口块 `block_<pattern起始下标>(1)`（[passes/pmgen/pmgen.py:558-581](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/pmgen.py#L558-L581)）。`block_<n>` 之间按 `blocks` 顺序链式互调：一个 `code` 块结束后隐式调用 `block_<n+1>`，一个 `match` 块对每个候选 cell 递归调用 `block_<n+1>`。

回溯靠宏 + `goto`。pmgen 在每个 `code` 块开头临时定义这几个宏（[passes/pmgen/pmgen.py:662-668](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/pmgen.py#L662-L668)）：

```python
print("#define reject   do { goto rollback_label; } while(0)", file=f)
print("#define accept   do { accept_cnt++; on_accept(); if (rollback) goto rollback_label; } while(0)", file=f)
print("#define branch   do {{ block_{}(recursion+1); if (rollback) goto rollback_label; }} while(0)".format(index+1), file=f)
```

- `reject`：丢弃当前这条半成品匹配，跳到块尾的 `rollback_label`，在那里**恢复状态变量**（pmgen 自动为被修改的 state 生成 backup/restore），然后返回上层——上层 `match` 块会尝试下一个候选 cell。
- `branch`：显式地「再往下走一格」（等价于隐式的块尾继续），但允许在同一个 `code` 块里多次 `branch` 来枚举多组赋值（典型用法见 muldiv 里「先原序、再 swap」）。
- `accept`：认定当前匹配成立，调用用户回调（即 `run_<name>(...)` 传进来的 lambda，里面做真正的网表改写），计一次命中。

`blacklist(cell)` / `autoremove(cell)` 是另一条回溯触发途径：它们把「这个 cell 已失效」记入 `blacklist_cells`，并通过 `rollback` 整数让递归回退到「该 cell 当初被匹配的那一层」去尝试别的候选（[passes/pmgen/pmgen.py:433-449](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/pmgen.py#L433-L449)，匹配块侧的处理在 [passes/pmgen/pmgen.py:749-769](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/pmgen.py#L749-L769)）。这保证一条规则在回调里删掉一个 cell 后，同一轮里不会再用它匹配出互相冲突的结果。

> 小结：pmgen 生成的匹配器 = 一份 `setup()` 预算的索引 + 一组 `block_*` 递归函数 + 用宏模拟的 `reject/accept/branch`。你只管在 `.pmg` 里描述「要什么样的子图」，回溯与索引的样板代码全自动生成。

#### 4.1.6 代码实践：亲手跑一次 pmgen

pmgen.py 只依赖标准库，**不需要先编译 Yosys**，可以直接运行。

1. **实践目标**：亲眼看到 `.pmg` 是如何变成 C++ 头文件的。
2. **操作步骤**（在仓库根目录）：

   ```bash
   python3 passes/pmgen/pmgen.py \
       -p peepopt_muldiv \
       -o /tmp/peepopt_muldiv_pm.h \
       passes/opt/peepopt_muldiv.pmg
   ```

3. **观察现象**：打开 `/tmp/peepopt_muldiv_pm.h`，你应该能看到（实际产物以本地运行为准，**待本地验证**具体行号）：
   - 顶部注释 `// Generated by pmgen.py from passes/opt/peepopt_muldiv.pmg`；
   - `struct peepopt_muldiv_pm { ... };` 一个类；
   - 成员 `IdString id_d_mul;` / `IdString id_d_div;` / `IdString id_b_Y;` …（来自 `rewrite_cpp` 对 `$mul`/`\Y` 的重写）；
   - `void setup(const vector<Cell*> &cells)` 里为 `div` 这个 match 块建的 `dict<...> index_2;`；
   - `int run_muldiv(std::function<void()> on_accept_f)` 与若干 `void block_<n>(int recursion)`；
   - `#define reject do { goto rollback_label; } while(0)` 等宏。
4. **预期结果**：头文件里的结构与你刚读过的 pmgen.py 代码生成段一一对应。把 `muldiv` 这个 pattern 的 `state`/`match`/`code` 与生成的 `block_*` 对照，能复现「`code` 块里的 `branch;` 变成了 `block_<n+1>(recursion+1)`，`accept;` 变成了 `accept_cnt++; on_accept();`」。
5. 若加 `-d`（debug）参数，pmgen 会先把解析出的 `blocks` 列表 `pprint` 出来，便于理解数据结构。

### 4.2 `.pmg` 模式语言：写一条子图规则

#### 4.2.1 概念说明

`.pmg` 是一种**行导向**的声明式语言：大部分行是空白分隔的 token，`//` 起注释（[passes/pmgen/README.md:54-59](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/README.md#L54-L59)）。一个 `.pmg` 文件含一个或多个 `pattern`，每个 `pattern` 是「`match` 块」与「`code` 块」交错排列的序列，从上往下就是搜索顺序。

设计上，`.pmg` 把一条规则拆成两半：

- **`match…endmatch`**：声明「我要找什么样的 cell」。pmgen 据此建索引、做候选枚举。
- **`code…endcode`**：声明「匹配到之后（或匹配过程中）要算什么、要不要接受、要不要改写」。里面是普通 C++，外加 `accept/reject/branch` 等控制原语。

这种「声明式查找 + 命令式动作」的切分，正是窥孔优化最自然的表达。

#### 4.2.2 核心流程：一条规则的骨架

一条典型规则长这样（以 muldiv 为蓝本）：

```text
pattern <名>                      ← 声明一个模式，生成 run_<名>()
state <类型> <变量>...            ← 匹配过程携带的中间状态（自动回溯保存）
udata <类型> <变量>...            ← 用户数据（不参与回溯，常作只读配置）

match <变量>                      ← 找一个 cell，绑定到该变量（隐式 Cell*）
    select <expr>                 ← 【快】初始化时对每个 cell 算一次，必须为 true
    index <类型> lhs === rhs      ← 【快】lhs 入索引键，rhs 匹配时算，相等才命中
    filter <expr>                 ← 【慢】逐候选求值，无法索引的条件放这
    optional | semioptional       ← 允许该变量取 nullptr 的情形
endmatch

code <可改的 state 变量>...       ← 命令式代码
    ...                            ← 普通 C++（可用 port/param/nusers/branch/...）
    accept;                        ← 接受这一组匹配（触发 run 传入的回调）
endcode
```

关键约定（来自 README）：

- `match <statevar>` 隐式声明一个 `Cell*` 类型的同名 state 变量（[passes/pmgen/README.md:141-146](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/README.md#L141-L146)）。
- `select` 只能用「本 match 的那个变量」（因为初始化时其它 state 还没赋值）；`index` 的**左**表达式也受同样限制，**右**表达式则可用前面 match/code 已绑定的 state（[passes/pmgen/README.md:153-164](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/README.md#L153-L164)）。
- `code` 块开头要列出「本块会修改哪些 state 变量」，pmgen 据此决定哪些需要 backup/restore（支持回溯）、哪些视为常量引用（[passes/pmgen/README.md:222-236](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/README.md#L222-L236)）。

#### 4.2.3 源码精读：`peepopt_muldiv.pmg` 逐行讲

这是全仓库最短、最适合入门的一条规则——它实现「`(A*B)/B → A`」（整数除法抵消乘法）。完整文件只有 39 行：

```verilog
// passes/opt/peepopt_muldiv.pmg:1-9   声明模式 + 找到乘法器
pattern muldiv

state <SigSpec> t x y
state <bool> is_signed

match mul
    select mul->type == $mul
    select GetSize(port(mul, \A)) + GetSize(port(mul, \B)) <= GetSize(port(mul, \Y))
endmatch
```

- `pattern muldiv` → 生成 `run_muldiv()`。
- 三个 `SigSpec` state（`t/x/y`）用来在「乘法」与「除法」之间传递信号；`is_signed` 记符号性。
- `match mul` 找一个 `$mul` 单元；两条 `select` 在 `setup()` 时过滤候选：类型必须是 `$mul`，且 `A 位宽 + B 位宽 ≤ Y 位宽`（保证乘积不溢出，是后面「可抵消」的必要条件）。注意 `\A` 经 `rewrite_cpp` 变成 `id_b_A`。

接着是一段 `code`，用 `branch` 枚举两种乘数顺序：

```verilog
// passes/opt/peepopt_muldiv.pmg:11-18   绑定 t/x/y，并枚举 A、B 的两种顺序
code t x y is_signed
    t = port(mul, \Y);          // 乘法输出（除法的被除数候选）
    x = port(mul, \A);          // 先假设 x = A
    y = port(mul, \B);
    is_signed = param(mul, \A_SIGNED).as_bool();
    branch;                     // ① 用 (x=A, y=B) 往下搜
    std::swap(x, y);            // 块尾隐式 branch ② 用 (x=B, y=A) 再搜一遍
endcode
```

`.pmg` 规则：「每个 code 块末尾隐含一次 `branch`」（[passes/pmgen/README.md:255-275](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/README.md#L255-L275)）。所以这段代码会探索两个分支：先 `(x=A, y=B)`，再 swap 成 `(x=B, y=A)`。这样无论 `B` 接在乘法器的 `A` 端还是 `B` 端，都能被 `div` 那条 `index` 匹配上——这是用 `branch` 处理「对称性 / 多种枚举」的典型手法。

然后找除法器：

```verilog
// passes/opt/peepopt_muldiv.pmg:20-25   找到“正好抵消”的除法器
match div
    select div->type.in($div)
    index <SigSpec> port(div, \A) === t     // 除法的被除数 = 乘法输出
    index <SigSpec> port(div, \B) === x     // 除法的除数   = 某个乘数
    filter param(div, \A_SIGNED).as_bool() == is_signed
endmatch
```

- `select div->type.in($div)` 限定 `$div`。
- 两条 `index` 是规则的核心：除法器的 `A` 端（被除数）必须等于乘法输出 `t`，`B` 端（除数）必须等于某个乘数 `x`。这两条由 `setup()` 建成索引，匹配时用「乘法这一侧算出的 `t`、`x`」去 O(1) 查候选除法器。
- `filter` 检查符号性一致（无法在初始化时索引，因为它依赖前面 `code` 算出的 `is_signed`，所以放慢路径）。

匹配成立后改写：

```verilog
// passes/opt/peepopt_muldiv.pmg:27-39   用另一个乘数 y 替换除法输出，删掉除法器
code
    SigSpec div_y = port(div, \Y);
    SigSpec val_y = y;
    if (GetSize(div_y) != GetSize(val_y))
        val_y.extend_u0(GetSize(div_y), param(div, \A_SIGNED).as_bool());
    did_something = true;
    log("muldiv pattern in %s: mul=%s, div=%s\n", module, mul, div);
    module->connect(div_y, val_y);   // div_Y := y（即 A*B/B = A 的“A”）
    autoremove(div);                  // 标记 div 待删（pm 析构时统一 remove）
    accept;
endcode
```

逻辑就是 `(A*B)/B = A`：把除法器的输出直接连到「另一个乘数 `y`」上（宽度不一致则零扩展），然后 `autoremove(div)` 让匹配器在析构时删掉这个已经无用的 `$div`，并 `accept` 计一次命中。`did_something` 是 peepopt.cc 里的全局布尔，用来驱动外层不动点循环（见 4.3）。

> 注意 muldiv **没有删 `mul`**：因为 `mul` 的输出 `t` 可能还被别的 cell 用着（`index` 只要求 `div.A===t`，不要求 `t` 只有 `div` 一个用户）。peepopt 的后续 `opt_clean`（在 [u6-l3](u6-l3-opt.md) 讲过）会负责清掉真正悬空的 `$mul`。这种「改写 + 留给通用清理 pass 收尾」的分工是 Yosys 优化 pass 的常见套路。

#### 4.2.4 进阶语法速览（选读）

muldiv 只用到最基础的语法，README 与 `test_pmgen.pmg` 还演示了更高级的能力，这里列要点供你按需查阅：

| 语法 | 作用 | 出处 |
|---|---|---|
| `optional` / `semioptional` | 允许该 match 取 `nullptr`（semioptional = 有就匹配、没有才置空） | [README:173-179](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/README.md#L173-L179) |
| `slice <var> <n>` | 把一个 cell 切成 n 段（如 `$pmux` 的每个选择位），枚举每段 | [README:182-199](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/README.md#L182-L199) |
| `choice <类型> <var> {…}` | 在一组选项里枚举（如尝试 `\A` 或 `\B` 端口） | [README:201-218](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/README.md#L201-L218) |
| `define <类型> <var> (expr)` | 定义派生局部变量 | 同上 |
| `code…finally…endcode` | `finally` 段在**回溯**时执行，适合维护栈/打印调试 | [README:280-294](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/README.md#L280-L294) |
| `subpattern <名>` + `arg` | 可复用、可递归的子模式，用 `subpattern(名);` 调用 | [README:296-355](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/README.md#L296-L355) |
| `generate a b` | 自动测试用例生成（按概率 a/b 随机造出匹配的子图） | [README:361-385](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/README.md#L361-L385) |

`passes/pmgen/test_pmgen.pmg` 把上述特性几乎全用了一遍（如 `reduce` 用 `subpattern`+`finally` 递归找最长与/或/异或链、`eqpmux` 用 `choice`+`slice` 匹配 `$eq` 驱动的 `$pmux`），是进阶阅读的最佳样本。

#### 4.2.5 小练习与答案

**练习 1**：在 `peepopt_muldiv.pmg` 里，如果把第二条 `index`（`port(div, \B) === x`）改成 `filter`，会对性能与正确性有什么影响？

> **答案**：正确性不变——`filter` 与 `index` 都是「必须为 true 才命中」的谓词。但性能会变差：`index` 让 pmgen 在 `setup()` 时按 `port(div,\B)` 建索引，匹配时 O(1) 查表；改成 `filter` 后，匹配器要**遍历所有 `$div` 候选**逐个比较 `B` 端，回退到线性扫描。这正是 README 强调「能 `index` 就别 `filter`」的原因。

**练习 2**：muldiv 的 `code` 用 `branch`+`swap` 枚举了乘数两种顺序。如果删掉 `std::swap(x, y);`，会漏掉什么情况？

> **答案**：会漏掉「除数 `B` 接在乘法器 `\B` 端、而被除数来自 `\A` 端」之外的那一半布局。具体说，`x` 固定为 `\A`、`y` 固定为 `\B` 时，只有当除法的除数恰好等于乘法器 `A` 输入才能匹配；乘数在 `B` 输入的情况不会被抵消识别。用 `branch` 探索两序正是为了覆盖乘法器两个输入端对称的情形。

### 4.3 peepopt 应用：一个真实的 pmgen 消费者

#### 4.3.1 概念说明

`peepopt`（peephole optimizers）是 pmgen 在 Yosys 主仓库里**最核心的使用者**：它把「一堆窥孔规则」打包成一条 pass。peepopt 自己几乎不含算法——它的 `execute` 只做三件事：读配置、建匹配器、反复跑 `run_*` 直到不动点。真正「找子图 + 改写」的逻辑全在 7 个 `.pmg` 文件里，经 pmgen 编译进 `peepopt_pm.h`。

这种「**瘦宿主 + 数据驱动规则**」的结构好处明显：加一条新优化规则 = 加一个 `.pmg` + 在 `peepopt.cc` 里加一行 `pm.run_xxx()`，无需改动匹配算法。peepopt 默认在 `opt` 的 fine 阶段被调用（见 [u6-l3](u6-l3-opt.md) 的 `opt` 编排）。

#### 4.3.2 核心流程

peepopt 的执行流（[passes/opt/peepopt.cc:84-130](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/peepopt.cc#L84-L130)）是一个经典的不动点循环：

```text
for 每个 selected module:
    did_something = true
    while did_something:                ← 不动点循环
        did_something = false
        peepopt_pm pm(module)           ← 构造匹配器（含 SigMap）
        pm.setup(module->selected_cells())   ← 一次性建索引
        if formalclk:
            pm.run_formal_clockgateff()
        else:
            pm.run_shiftadd()
            pm.run_shiftmul_right()
            pm.run_shiftmul_left()
            pm.run_shiftpow2()
            pm.run_muldiv()             ← 4.2 讲的那条规则
            pm.run_muldiv_c()
```

要点：

1. **不动点**：每条规则在改写网表后会把全局 `did_something` 置 true（muldiv 里那行 `did_something = true;` 就是干这个）。外层 `while` 据此反复重建匹配器、重跑全部规则，直到一轮里没有任何规则命中。这与 `opt` 的不动点循环（[u6-l3](u6-l3-opt.md)）是同一思想：一次替换可能暴露出新的可优化机会。
2. **每轮重建匹配器**：注意 `peepopt_pm pm(module)` 与 `pm.setup(...)` 在 `while` 体内。因为 `setup()` 的索引是网表快照，规则改写后快照失效，必须重建。这也是 README 提醒「匹配器用完后其 `setup()` 索引即过期」的体现。
3. **`-formalclk` 分支**：传入 `-formalclk` 时不跑算术/移位规则，只跑 `run_formal_clockgateff()`（把基于锁存的时钟门控改成基于触发器的形态，配合 `clk2fflogic` 避免组合反馈，见 help 文本 [passes/opt/peepopt.cc:77-82](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/peepopt.cc#L77-L82)）。

#### 4.3.3 源码精读：构建集成——`.pmg` 如何进入编译

peepopt 的 `.pmg` 不是手动编译的，而是在 CMake 构建时由 `pmgen_command` 自动生成。声明在 `passes/opt/CMakeLists.txt`：

```cmake
# passes/opt/CMakeLists.txt:83-92
pmgen_command(peepopt
    peepopt_shiftmul_right.pmg
    peepopt_shiftmul_left.pmg
    peepopt_shiftadd.pmg
    peepopt_shiftpow2.pmg
    peepopt_muldiv.pmg
    peepopt_muldiv_c.pmg
    peepopt_formal_clockgateff.pmg
    PREFIX
        peepopt
)
yosys_pass(peepopt
    peepopt.cc
    ${PMGEN_peepopt_OUTPUT}
)
```

- `pmgen_command(peepopt … PREFIX peepopt)`：把 7 个 `.pmg` 用公共前缀 `peepopt` 合并，生成构建目录下的 `peepopt_pm.h`，并通过变量 `PMGEN_peepopt_OUTPUT` 暴露其路径。
- `yosys_pass(peepopt peepopt.cc ${PMGEN_peepopt_OUTPUT})`：把生成的头文件与 `peepopt.cc` 一起编译成 `peepopt` pass。于是 `peepopt.cc` 里的 `#include "passes/opt/peepopt_pm.h"`（[passes/opt/peepopt.cc:40](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/peepopt.cc#L40)）在构建时就能找到。

`pmgen_command` 本身只是个 `add_custom_command`（[cmake/PmgenCommand.cmake:35-60](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/cmake/PmgenCommand.cmake#L35-L60)）——它声明「以这些 `.pmg` 和 `pmgen.py` 为依赖，跑一次 `python3 pmgen.py … -o peepopt_pm.h`」，.pmg 一改就自动重生成头文件。这种「源文件 → 构建期代码生成 → 正常编译」的模式，与 Verilog 前端用 flex/bison 在构建期生成词法/语法器（见 [u5-l1](u5-l1-verilog-lexer-parser.md)）完全同构。

#### 4.3.4 源码精读：scratchpad 调参

peepopt 还示范了 pass 如何从 `design->scratchpad` 读配置（Design 全局状态，见 [u2-l2](u2-l2-design-module.md)）：

```cpp
// passes/opt/peepopt.cc:101-104   读“shiftadd 允许的填充倍数”上限
// limit the padding from shiftadd to a multiple of the input data
shiftadd_max_ratio = design->scratchpad_get_int(
    "peepopt.shiftadd.max_data_multiple", 2);
```

`shiftadd_max_ratio` 是 `peepopt.cc` 顶部声明的全局变量（[passes/opt/peepopt.cc:30](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/peepopt.cc#L30)），而 `peepopt_shiftadd.pmg` 的 `code` 段里直接读它（`if(shiftadd_max_ratio>0 && ...)`，见 [passes/opt/peepopt_shiftadd.pmg:106-111](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/peepopt_shiftadd.pmg#L106-L111)）——因为 `.pmg` 里的 C++ 最终就内联进 `peepopt_pm.h`、与 `peepopt.cc` 同属一个翻译单元，全局变量天然可见。这说明 `.pmg` 的 code 段并非孤立，它能访问宿主 pass 的全局状态与 RTLIL API（`module->connect`、`log` 等）。

#### 4.3.5 pmgen 的其它用户（与一个常见误解）

理解了 peepopt，就能举一反三看懂仓库里所有 pmgen 用户。除 peepopt 外，pmgen 主要被**各厂商的 DSP / SRL / 进位链推断 pass** 使用——它们要在网表里识别「乘加/移位寄存器/进位链」这类特定子图，正好是 pmgen 的拿手好戏。例如：

- [techlibs/xilinx/xilinx_dsp.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/xilinx_dsp.cc) 配 `xilinx_dsp48a.pmg` / `xilinx_dsp_CREG.pmg`：把 `$mul`+`$add`+流水线寄存器打包成 DSP48；
- [techlibs/xilinx/xilinx_srl.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/xilinx_srl.cc)：识别移位寄存器链（SRL）；
- [techlibs/ice40/ice40_dsp.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/ice40/ice40_dsp.cc)、[techlibs/quicklogic/ql_dsp_macc.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/quicklogic/ql_dsp_macc.cc)、[techlibs/microchip/microchip_dsp.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/microchip/microchip_dsp.cc)、[techlibs/lattice/lattice_dsp_nexus.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/lattice/lattice_dsp_nexus.cc)：各厂商 DSP 推断；
- [passes/pmgen/test_pmgen.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/test_pmgen.cc)：pmgen 自带的特性测试（同时 `#include` 了 ice40/xilinx 生成的头文件做端到端验证，见其 CMake `REQUIRES`）。

> **重要更正（避免被误导）**：学习路线里把 `alumacc` 与 `shiftadd` 并列为「基于 pmgen 的优化」，这只对了一半。`shiftadd` 确实是 peepopt 的一条 pmgen 规则（`peepopt_shiftadd.pmg`）；但 **`alumacc` 并不使用 pmgen**——它是 `passes/techmap/alumacc.cc` 里手写的 C++ pass（把算术单元拆成 `$macc` 形态，见 [u6-l5](u6-l5-techmap-simplemap.md) 里关于 `alumacc` 的讨论），其源码与 CMake 里都没有任何 `.pmg` / `_pm.h`。pmgen 的真实用户是上面列出的 peepopt 与各厂商 DSP/SRL/carry 推断 pass。

#### 4.3.6 代码实践：让 peepopt 真的命中 muldiv

这是一次端到端验证，需要先按 [u1-l2](u1-l2-build-and-run.md) 构建出 `yosys`。

1. **实践目标**：构造一个含 `(A*B)/B` 的设计，验证 `peepopt` 能识别并消除乘除对，并对照 `peepopt_muldiv.pmg` 解释日志。
2. **操作步骤**：

   写一个最小 Verilog（示例代码，非项目原有文件）`muldiv_test.v`：

   ```verilog
   module muldiv_test(input [7:0] a, input [7:0] b, output [7:0] y);
       assign y = (a * b) / b;
   endmodule
   ```

   在 `yosys>` 交互 shell 里执行：

   ```yosys
   read_verilog muldiv_test.v
   proc
   opt -full 		    ; # 先做常规清理，把表达式综合成 $mul/$div
   write_rtlil /tmp/before.rtlil  ; # 记录改写前：应能看到 $mul 与 $div
   peepopt 		    ; # 跑窥孔优化
   write_rtlil /tmp/after.rtlil   ; # 记录改写后
   ```

3. **需要观察的现象**：
   - `peepopt` 运行时应打印形如 `muldiv pattern in ...: mul=..., div=...` 的日志（正是 [peepopt_muldiv.pmg:35](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/peepopt_muldiv.pmg#L35) 那行 `log`）。
   - 对比 `before.rtlil` 与 `after.rtlil`：`$div` 单元应被移除（其输出被 `connect` 到乘法器的另一个输入），`$mul` 若因此悬空，再跑一次 `opt_clean` 会一并删掉。
4. **预期结果**：最终网表里乘除对被消除，`y` 直接由 `a`（经位宽调整）驱动，功能等价但少了一个乘法器和一个除法器。
5. 若 muldiv 没触发，常见原因是网表里信号经 SigMap 归一化后端口连接与 `index` 不完全一致，或符号性 `filter` 不满足；可用 `dump` 配合 `select` 检查 `$mul`/`$div` 的端口连接。具体行为**待本地验证**。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「**读懂一条规则 → 预测匹配器结构 → 实测验证**」的小研究。

**任务**：选取 peepopt 的另一条规则 `peepopt_muldiv_c.pmg`（实现「`(A*B)/C → A*(B/C)`，当 C 是能整除 B 的常数」，见 help 文本 [peepopt.cc:56](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/peepopt.cc#L56)），完成以下三步：

1. **读 `.pmg`**：打开 [passes/opt/peepopt_muldiv_c.pmg](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/peepopt_muldiv_c.pmg)，指出它的 `match` 块分别匹配了哪些单元、用了哪些 `select`/`index`/`filter`、`code` 块如何判断「C 能整除 B」并改写。把它和 `peepopt_muldiv.pmg` 对比，说明二者 `match`/`index` 结构的异同。
2. **预测生成产物**：参照 4.1.6 的方法，用 `python3 passes/pmgen/pmgen.py -p muldiv_c -o /tmp/muldiv_c_pm.h passes/opt/peepopt_muldiv_c.pmg` 单独生成（或直接在已构建的 `build/passes/opt/peepopt_pm.h` 里看合并版）。先**手写预测**：这个 pattern 会有几个 `match` 块、分别建几张索引表、`run_muldiv_c` 会调用哪些 `block_*`，再打开头文件核对。
3. **实测**：写一个 `assign y = (a * 8) / 4;`（C=4、B=8 的常数情形），按 4.3.6 的流程跑 `peepopt`，确认 `muldiv_c` 日志是否出现、`$div` 是否被常数除法替换。

> 这一步把「读 DSL → 理解代码生成 → 验证优化效果」三件事打通，是掌握 pmgen 这类「代码生成器」最有效的练习方式。

---

## 6. 本讲小结

- **pmgen 是源到源翻译器**：读声明式 `.pmg`，吐 header-only 的 `*_pm.h`，内含一个带 `setup()`/`run_*()`/`block_*()` 的匹配器类；算法是带回溯的递归搜索，靠 `select`/`index` 预建索引保证效率。
- **`.pmg` 把规则拆成「声明式查找 + 命令式动作」**：`match` 用 `select`（初始化过滤）/`index`（O(1) 查表）/`filter`（逐候选慢路径）描述要找的子图；`code` 用普通 C++ 加 `accept`/`reject`/`branch`/`subpattern` 控制回溯与改写。
- **`rewrite_cpp` 是 DSL 舒适层**：让 `.pmg` 里能直接写 `$mul`/`\Y` 这种 RTLIL 字面量与 `port()`/`param()`/`nusers()` 便捷函数，由生成器翻译成合法 C++ 与 `IdString` 成员。
- **peepopt 是「瘦宿主 + 数据驱动规则」**：自身只有不动点循环，7 条规则全在 `.pmg` 里，经 CMake 的 `pmgen_command` 在构建期生成 `peepopt_pm.h` 并 `#include`——加规则只需加 `.pmg` 与一行 `pm.run_*()`。
- **muldiv 是最佳入门样本**：39 行实现 `(A*B)/B → A`，展示了 state 传递、`branch` 枚举乘数顺序、`index` 锁定上下游连接、`autoremove`+`accept` 改写网表的完整套路。
- **真实用户与一个误解**：pmgen 的实际用户是 peepopt 全套规则 + 各厂商 DSP/SRL/进位链推断 pass；`alumacc` 并不使用 pmgen（手写 C++），需注意区分。

---

## 7. 下一步学习建议

- **横向对照另一种「数据驱动改写」**：阅读 [u6-l5 techmap 与 simplemap](u6-l5-techmap-simplemap.md)，对比 techmap（用 Verilog 模板做单元替换）与 pmgen（用 `.pmg` 做子图替换）——两者都把易变知识从 C++ 剥离成数据，但 techmap 替换单个 cell、pmgen 匹配多 cell 子图，适用场景不同。
- **深入一个厂商 DSP 推断 pass**：选 [techlibs/xilinx/xilinx_dsp48a.pmg](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/xilinx_dsp48a.pmg) 配合 [u8-l2 目标平台综合流程](u8-l2-vendor-synth-flows.md)，看 pmgen 如何识别「乘法 + 加法 + 流水线寄存器」并打包成 DSP48，体会 pmgen 在真实综合流程里的工程价值。
- **进阶 DSL 特性**：精读 [passes/pmgen/test_pmgen.pmg](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/test_pmgen.pmg) 与 [passes/pmgen/README.md](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/pmgen/README.md)，掌握 `subpattern` 递归、`slice`/`choice` 枚举、`generate` 自动测试用例生成——这些是把 pmgen 用于复杂子图（如长加法树、级联链）的关键。
- **自己写一条规则**：参考 [u9-l1 编写自定义 Pass](u9-l1-write-custom-pass.md) 的 pass 骨架，结合本讲的 `pmgen_command` 用法，尝试为自己的优化思路写一个 `.pmg` 并接入一个最小 pass，完成从「用 pmgen」到「扩展 pmgen」的闭环。
