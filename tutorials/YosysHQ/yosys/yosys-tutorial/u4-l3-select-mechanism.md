# 选择机制 select 与作用域

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 Yosys 里「选择（selection）」到底是什么数据、存在哪里、默认值是什么。
- 读懂 `RTLIL::Selection` 这个 C++ 结构的三种工作模式（全选 / 全选非黑盒 / 显式枚举），以及 Pass 是怎样查询「某个对象是否被选中」的。
- 理解「选择栈」`selection_stack` 在一次综合脚本运行期间的压栈/弹栈规则，以及为什么这是 Pass 之间互不干扰的关键。
- 会用 `select` 命令的栈式语法（模块/对象通配、`t:`/`w:`/`a:` 等前缀、`%u`/`%i`/`%d`/`%ci` 等操作符）精确框定后续命令的作用对象，并用 `cd` / `-module` 切换「模块作用域」。

本讲依赖 [u4-l1](u4-l1-pass-registration.md)（Pass 注册与 `Pass::call` 调度）和 [u2-l2](u2-l2-design-module.md)/[u2-l3](u2-l3-wire-cell-sigspec.md)（Design/Module/Wire/Cell 数据结构）。

## 2. 前置知识

在动手之前，先用大白话建立两个直觉。

**直觉一：综合脚本里的每条命令都「只对设计的一部分」起作用。**
比如 `delete` 会删东西，但你显然不想每次都把整个设计删光。Yosys 的做法是：每条命令执行前，先看 Design 上挂着的「当前选择（current selection）」——一个描述「这次该对哪些模块、哪些 wire、哪些 cell 生效」的集合。命令只在这个集合里干活。`select` 命令就是用来修改这个集合的。

**直觉二：选择既是「全局状态」，又能「临时覆盖」。**
默认情况下，当前选择是「整个设计」（全选），所以你平时写 `synth`、`opt` 时不用关心选择——它们作用于全设计。但你可以用 `select foo` 把范围缩小到模块 `foo`；也可以直接在命令尾巴上写 `stat t:$dff`，让 `stat` 临时只统计 `$dff` 单元，而不改动全局选择。这两种用法背后是同一套引擎。

> 名词辨析：本讲会出现两个「栈」，别混淆——
> - **选择栈 `selection_stack`**：挂在 `RTLIL::Design` 上，存放「当前选择」，是全局状态。本讲主角。
> - **工作栈 `work_stack`**：`select.cc` 内部的静态变量，只在解析一条选择表达式时临时用，用来算出最终结果。它是实现细节，4.3 节会用到。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `kernel/rtlil.h` | 声明 `RTLIL::Selection` 结构体，以及 `Design::selection_stack`、`push/pop_selection`、`selected_active_module` 等接口。 |
| `kernel/rtlil.cc` | 实现 `Selection` 的查询方法（`selected_module`/`selected_member`）、`optimize()`、`clear()`，以及 `Design` 的选择栈压弹与 `Module::selected_cells()` 等消费端辅助函数。 |
| `passes/cmds/select.cc` | `select` 命令本体（`SelectPass`）、`cd` 命令（`CdPass`），以及选择表达式的解析引擎 `select_stmt`、`handle_extra_select_args`、各种集合运算 `select_op_*`。 |
| `kernel/register.cc` | `Pass::call` 在执行每条命令前后对选择栈做深度保护；`Pass::extra_args` 把命令尾巴上的选择参数推入选择栈。 |
| `docs/source/using_yosys/more_scripting/selections.rst` | 官方对选择框架的用户文档，配有大量图例。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 `RTLIL::Selection` 数据结构**、**4.2 选择栈与作用域**、**4.3 `select` 命令语法**。

### 4.1 RTLIL::Selection：选择的数据结构

#### 4.1.1 概念说明

「选择」本质上是这样一个问题的回答：**设计里的某个对象（某个模块、某根 wire、某个 cell）属不属于当前要处理的范围？**

最朴素的表示法是「把所有被选中的对象都列出来」。但 Yosys 的默认情况是「全选」，如果把全设计成千上万个对象都列一遍，既慢又占内存。所以 `RTLIL::Selection` 采用了「三档 + 显式列表」的紧凑表示：

- 如果是**全选**，只需要一个布尔标志，不必枚举任何对象；
- 如果只选了**一部分**，才用两张表精确记下「哪些模块被整体选中」和「哪些模块里的哪些成员被选中」。

#### 4.1.2 核心流程

一个 `Selection` 对象由几个字段共同决定它「选了什么」。可以用下面的判定逻辑来理解（对单个模块 `M` 而言）：

```text
若 complete_selection(全选含黑盒)        → M 一定被选中
否则若 full_selection(全选但不含黑盒)    → M 若是黑盒则不选，否则选中
否则(显式枚举):
    若 M 在 selected_modules            → M 整体被选中(含其所有成员)
    否则若 M 在 selected_members        → 只有列出的成员被选中
    否则                                → M 未被选中
```

这里有个关键设计：**黑盒（blackbox）模块默认被排除**。因为综合脚本里 `read_verilog -lib` 读进来的库单元通常是黑盒，你一般不希望 `opt` 之类的命令去「优化」它们。`selects_boxes` 这个布尔位专门记录「本次选择是否把黑盒也纳入」。三种档位的对应关系是：

| 字段组合 | 含义 | 工厂方法 |
| --- | --- | --- |
| `complete_selection = true` | 全设计，**包含**黑盒 | `Selection::CompleteSelection()` |
| `full_selection = true`（且 `selects_boxes=false`） | 全设计，**不含**黑盒（最常见默认） | `Selection::FullSelection()` |
| 两者皆假，靠 `selected_modules`/`selected_members` | 显式枚举 | `Selection::EmptySelection()`（空集，再逐步加） |

#### 4.1.3 源码精读

先看结构体定义。`RTLIL::Selection` 用三个布尔位 + 两张表表达一切：

[passes/cmds/... 见 kernel/rtlil.h:1777-1798](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1777-L1798)

```cpp
struct RTLIL::Selection
{
    bool selects_boxes;          // 是否纳入黑盒模块
    bool complete_selection;     // 全选(含黑盒)
    bool full_selection;         // 全选(不含黑盒)
    pool<RTLIL::IdString> selected_modules;                  // 整体选中的模块名
    dict<RTLIL::IdString, pool<RTLIL::IdString>> selected_members; // 模块 -> 选中的成员名集合
    RTLIL::Design *current_design;
    Selection(bool full = true, bool boxes = false,
              RTLIL::Design *design = nullptr) :
        selects_boxes(boxes), complete_selection(full && boxes),
        full_selection(full && !boxes), current_design(design) { }
```

注意构造函数里 `complete_selection = full && boxes`、`full_selection = full && !boxes`：默认 `full=true, boxes=false`，所以**默认构造出来就是「全选但不含黑盒」**。三个工厂方法只是把这两位设成不同组合：

[kernel/rtlil.h:1860-1866](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1860-L1866)

Pass 在运行时并不直接读这些字段，而是调用三个查询方法。它们的判定逻辑正是 4.1.2 那张表：

[kernel/rtlil.cc:1049-1091](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L1049-L1091)

```cpp
bool RTLIL::Selection::selected_module(IdString mod_name) const {  // 模块是否被触及(整体或部分)
    if (complete_selection) return true;
    if (!selects_boxes && boxed_module(mod_name)) return false;
    if (full_selection) return true;
    if (selected_modules.count(mod_name) > 0) return true;
    if (selected_members.count(mod_name) > 0) return true;
    return false;
}
bool RTLIL::Selection::selected_whole_module(IdString mod_name) const { // 模块是否被整体选中
    ...
    if (selected_modules.count(mod_name) > 0) return true;   // 只看 selected_modules
    return false;
}
bool RTLIL::Selection::selected_member(IdString mod_name, IdString memb_name) const {
    ...
    if (selected_modules.count(mod_name) > 0) return true;   // 整体选中 → 所有成员都中
    if (selected_members.count(mod_name) > 0)
        if (selected_members.at(mod_name).count(memb_name) > 0) return true;
    return false;
}
```

要点：`selected_module`（部分也算）与 `selected_whole_module`（必须整体）的区别，决定了 Pass 能否安全地对「整个模块」做处理（例如 `flatten` 需要整体选中）。

最后看 `selects_all()` 与 `optimize()`。前者只是一个便捷判断；后者是「保洁」函数——删掉不存在的模块/成员、把「所有成员都被选中」的模块从 `selected_members` 升级为 `selected_modules`、把「所有模块都被选中」的选择归约为 `full_selection`。`optimize` 在几乎每次选择变更后都会被调用，保证表示始终最紧凑：

[kernel/rtlil.cc:1093-1161](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L1093-L1161)

其中最后一段（[kernel/rtlil.cc:1153-1160](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L1153-L1160)）体现了「升档」逻辑：当显式枚举的模块数等于设计总模块数时，直接退化成 `full_selection`/`complete_selection`，丢掉明细表。

#### 4.1.4 代码实践

**目标**：在源码层面确认「默认选择 = 全选非黑盒」，并看清三种工厂方法的位组合。

**步骤**：
1. 打开 [kernel/rtlil.cc:1171-1182](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L1171-L1182)，看 `Design` 构造函数里有一句 `push_full_selection();`——这就是「设计刚创建时，当前选择是全选」的源头。
2. 对照 [kernel/rtlil.h:1860-1866](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1860-L1866) 三个工厂方法，手算 `FullSelection()`、`EmptySelection()`、`CompleteSelection()` 各自的 `selects_boxes / full_selection / complete_selection` 取值。

**预期结果**：`FullSelection()` → `full_selection=true`；`EmptySelection()` → 三位皆假（靠空表表示空集）；`CompleteSelection()` → `complete_selection=true, selects_boxes=true`。这一步纯静态阅读，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：若一个选择对象的 `selected_modules` 为空、`selected_members` 也为空、且 `full_selection=false`，它表示什么？
**答案**：空集（什么都没选）。对应 `Selection::empty()` 返回 true 的条件（[kernel/rtlil.h:1851-1854](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1851-L1854)）。

**练习 2**：为什么 `optimize()` 要把「所有成员都被选中」的模块从 `selected_members` 升级进 `selected_modules`？
**答案**：为了把表示归约到最紧凑、最便于查询的形式。升级后，对该模块的 `selected_member()` 直接走 `selected_modules.count() > 0` 这条快速分支返回 true，而不必逐个查成员集合（[kernel/rtlil.cc:1140-1151](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L1140-L1151)）。

### 4.2 选择栈与作用域：Design 如何持有当前选择

#### 4.2.1 概念说明

`RTLIL::Selection` 描述「选了什么」，而 `Design` 需要一个地方来存放「**当前**的选择」。Yosys 把它放在一个栈上：`std::vector<RTLIL::Selection> selection_stack`。栈顶 `selection_stack.back()` 就是当前选择，`Design::selection()` 直接返回它。

为什么要做成**栈**而不是单个变量？因为很多命令需要「临时」缩小作用范围，而**不影响**别的命令看到的全局选择。典型场景就是 `stat t:$dff`：`stat` 命令把「只选 `$dff`」的选择**压栈**，执行完统计再**弹栈**，于是下一条命令看到的仍是原来的全选。栈让「临时覆盖」可以嵌套、自动回收。

与栈并列的还有「作用域」概念。Yosys 有两种上下文：
- **design 上下文**（默认）：选择表达式里的对象名要带 `模块名/` 前缀，例如 `foo/clk`。
- **module 上下文**：用 `cd foo` 或 `select -module foo` 切入后，对象名相对于该模块解释，无需前缀，且所有命令只作用于这个模块。`Design::selected_active_module` 这个字符串就是作用域的载体。

#### 4.2.2 核心流程

一次综合脚本运行期间，选择栈的生命周期大致是：

```text
Design 构造          → push_full_selection()          # 栈底:全选(只有1层)
执行一条命令 cmd:
    记录 orig = selection_stack.size()
    pre_execute / cmd.execute / post_execute
    若 cmd 内部用 extra_args 压了选择 -> 临时层
    收尾: while (size > orig) pop_selection()          # 弹回原深度
命令尾巴带 [selection] 参数(如 stat t:$dff):
    extra_args → handle_extra_select_args → push_selection(算出的选择)
    命令体里 selection() 取到的是这层临时选择
    命令返回后被上面的收尾循环弹掉
cd / select -module   -> 设置 selected_active_module,限制作用域到某模块
select -clear         -> selection() = FullSelection,清空作用域
```

关键不变量：**每条命令执行前后，选择栈深度不变**（除非命令自己就是 `select`，会改写栈顶内容而非深度）。这个不变量由 `Pass::call` 强制保证。

#### 4.2.3 源码精读

先看选择栈在 `Design` 里的声明与几个最常用的访问方法：

[kernel/rtlil.h:1910-1912](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1910-L1912)

```cpp
std::vector<RTLIL::Selection> selection_stack;            // 选择栈
dict<RTLIL::IdString, RTLIL::Selection> selection_vars;   // select -set 存的命名选择
std::string selected_active_module;                       // 模块作用域(cd / -module)
```

`selection()` 就是取栈顶：

[kernel/rtlil.h:1981-1988](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1981-L1988)

压弹实现很直接，注意 `pop_selection` 在栈空时会自动补一个 `full_selection`，保证「永远至少有一层、且默认全选」：

[kernel/rtlil.cc:1435-1462](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L1435-L1462)

```cpp
void RTLIL::Design::push_selection(RTLIL::Selection sel) {
    sel.current_design = this;
    selection_stack.push_back(sel);
}
void RTLIL::Design::pop_selection() {
    selection_stack.pop_back();
    if (selection_stack.empty())        // 栈空 → 补全选
        push_full_selection();
}
```

「深度不变量」的守护在 `Pass::call` 里：执行前记下原始栈深度，执行后把多压出来的层全部弹掉。这就是 `stat t:$dff` 之类用法能「临时生效、用完即焚」的根因：

[kernel/register.cc:299-305](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L299-L305)

```cpp
size_t orig_sel_stack_pos = design->selection_stack.size();
auto state = pass->pre_execute();
pass->execute(args, design);
pass->post_execute(state);
while (design->selection_stack.size() > orig_sel_stack_pos)
    design->pop_selection();
```

那命令尾巴上的选择参数是怎么变成「一层临时选择」的？入口是 `Pass::extra_args`，它把剩余参数交给 `handle_extra_select_args`，后者算出选择并 `push_selection`：

[kernel/register.cc:192-208](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L192-L208)

[passes/cmds/select.cc:1035-1055](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/select.cc#L1035-L1055)

```cpp
void handle_extra_select_args(Pass *pass, const vector<string> &args,
                              size_t argidx, size_t args_size, RTLIL::Design *design) {
    work_stack.clear();
    for (; argidx < args_size; argidx++) {
        ...
        select_stmt(design, args[argidx]);   // 逐个解析选择表达式,压入 work_stack
    }
    while (work_stack.size() > 1)            // 多个表达式 → 求并集
        select_op_union(design, work_stack.front(), work_stack.back()), work_stack.pop_back();
    if (work_stack.empty()) design->push_empty_selection();
    else design->push_selection(work_stack.back());   // 推入选择栈成为"当前选择"
}
```

命令体消费选择时，并不直接碰 `Selection`，而是用 `Module` 提供的便捷函数，它们遍历模块内对象、用 `design->selected(this, obj)` 过滤：

[kernel/rtlil.cc:2841-2849](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L2841-L2849)

```cpp
std::vector<RTLIL::Cell*> RTLIL::Module::selected_cells() const {
    std::vector<RTLIL::Cell*> result;
    for (auto &it : cells_)
        if (design->selected(this, it.second))   // 查当前选择栈顶
            result.push_back(it.second);
    return result;
}
```

例如 `stat` 就是这样只统计被选中的 cell（[passes/cmds/stat.cc:185](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/stat.cc#L185)），所以 `stat t:$dff` 只会数 `$dff`。

最后看「模块作用域」。`cd foo` 等价于 `select -module foo`，它把模块名写进 `selected_active_module`，并让当前选择退回全选后过滤到该模块。一旦设置了它，`Design::selected_module` 等查询会直接对「非当前模块」返回 false：

[kernel/rtlil.cc:1404-1423](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L1404-L1423)

```cpp
bool RTLIL::Design::selected_module(RTLIL::IdString mod_name) const {
    if (!selected_active_module.empty() && mod_name != selected_active_module)
        return false;                       // 处于模块作用域时,别的模块一律不选
    return selection().selected_module(mod_name);
}
```

而 `select -module` 的实现就在 `SelectPass::execute` 里（[passes/cmds/select.cc:1412-1419](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/select.cc#L1412-L1419)）：它只设置 `selected_active_module`，把选择交由 `select_filter_active_mod` 收窄。`cd` 命令则是对此的快捷封装（[passes/cmds/select.cc:1754-1761](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/select.cc#L1754-L1761)）。

#### 4.2.4 代码实践

**目标**：体会「命令尾巴的选择参数是临时的、用完即弹」。

**步骤**（假设你已按 [u1-l2](u1-l2-build-and-run.md) 构建出 `./yosys`）：

```bash
cd examples/cmos
../../yosys -p "read_verilog counter.v; synth; stat; stat t:\$dff; stat"
```

**需要观察的现象**：
- 第一条 `stat`：统计**整个设计**的所有单元（数字较大）。
- 第二条 `stat t:$dff`：只统计 `$dff`（若该计数器带异步复位，可能是 `$adff`，请改用 `t:$adff`；可先跑 `stat` 看实际类型）。注意 `$` 在 shell 里要转义成 `\$`。
- 第三条 `stat`：又回到**整个设计**的统计——证明中间那条的「只选 `$dff`」并未污染全局选择。

**预期结果**：第二与第三条 `stat` 的范围互不影响，第三条输出与第一条一致。若你对具体单元数无把握，记为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `pop_selection()` 在栈空时要补一个 `push_full_selection()`？
**答案**：保证「当前选择永不为空、且默认全选」这一全局不变量。这样任何 Pass 调用 `selection()` 都能拿到合法对象，而不必判空（[kernel/rtlil.cc:1456-1462](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L1456-L1462)）。

**练习 2**：`cd mycpu` 之后执行 `dump reg_*`，为什么 `reg_*` 不用写 `mycpu/reg_*`？
**答案**：`cd` 设置了 `selected_active_module = mycpu`，进入模块作用域。此时选择表达式的成员部分会自动以该模块为上下文解释（见 4.3.3 的 `select_stmt` 里 `if (!design->selected_active_module.empty())` 分支，[passes/cmds/select.cc:827-831](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/select.cc#L827-L831)）。

### 4.3 select 命令语法：一台栈机器

#### 4.3.1 概念说明

`select` 命令的 `<selection>` 参数并不是普通字符串匹配，而是**一台基于栈的小机器**的程序。`select` 命令自带的帮助文本原话（[passes/cmds/select.cc:1178-1181](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/select.cc#L1178-L1181)）：

> The `<selection>` argument itself is a series of commands for a simple stack machine. Each element on the stack represents a set of selected objects. After this commands have been executed, the union of all remaining sets on the stack is computed and used as selection for the command.

这条机器有三类「指令」：

1. **压栈指令（模式 pattern）**：按名字/类型/属性匹配出一组对象，压入栈。例如 `t:$dff`（按类型）、`w:clk`（按 wire 名）、`a:keep`（按属性）。
2. **操作符（`%` 开头）**：对栈顶做集合运算或图遍历。例如 `%i`（交集）、`%u`（并集）、`%d`（差集）、`%ci`（向输入锥扩展）。
3. **引用**：`@name` 压入之前用 `select -set name` 存好的选择；`%` 压入「上一轮的当前选择」。

整条表达式跑完后，栈上剩下的所有集合求**并集**，作为最终结果。

#### 4.3.2 核心流程

`select foo*/t:$mux %ci` 这样一条表达式的求值过程：

```text
work_stack = []
1. foo*/t:$mux   -> select_stmt: 匹配 foo* 模块里的 $mux 单元, 压栈
   work_stack = [ {foo* 里的 $mux} ]
2. %ci           -> select_stmt: 对栈顶做"向输入方向扩展一步", 原地替换栈顶
   work_stack = [ {$mux 及其驱动} ]
结束: work_stack 只剩1个 -> 直接用作结果
(若剩多个 -> 逐个 union 合并成1个)
```

模式串里的「模块部分」和「成员部分」用 `/` 分隔：`<mod_pattern>/<obj_pattern>`。两侧各自支持通配符 `* ? [..]` 和带前缀的特殊匹配：

| 成员前缀 | 含义 |
| --- | --- |
| `c:<pat>` | cell 名匹配 |
| `t:<pat>` | cell **类型**匹配（最常用，如 `t:$dff`） |
| `w:<pat>` | wire 名匹配 |
| `i:` / `o:` / `x:` | 输入 / 输出 / 任意端口 wire |
| `m:<pat>` | memory 名匹配 |
| `p:<pat>` | process 名匹配 |
| `a:<pat>` / `a:<n>=<v>` | 按**属性**匹配（可带值，支持 `= != < <= > >=`） |
| `r:<pat>` | cell **参数**匹配 |
| `s:<n>` / `s:<min>:<max>` | 按位宽匹配 wire |
| `n:<pat>` | 任意对象按名匹配（默认规则，`n:` 可省） |

模块部分的前缀则少一些：`A:` 按模块属性、`N:` 按模块名（默认）。要**纳入黑盒**，在整个模式前加 `=`（如 `=t:DFF`）。

常用的 `%` 操作符：

| 操作符 | 含义 |
| --- | --- |
| `%u` / `%i` / `%d` | 栈顶两元素求并 / 交 / 差（pop 2 push 1） |
| `%n` | 栈顶取反（补集） |
| `%c` | 复制栈顶再压栈 |
| `%` | 把「上一轮当前选择」压栈 |
| `%%` | 把栈上所有元素求并集后替换整个栈 |
| `%x` | 扩展：选中连接到当前所选 wire 的 cell，及连接到所选 cell 的 wire |
| `%ci` / `%co` | 只向**输入锥** / **输出锥**方向扩展（按数据流方向） |
| `%ci<n>` / `%ci*` | 重复扩展 n 次 / 重复到不动 |
| `%s` / `%m` / `%M` / `%C` | 在模块与 cell 实例之间换算 |

`%ci`/`%co`/`%x` 还能带「规则」来精细控制走哪些 cell 类型/端口，语法是 `%ci[次数][.上限][:<+规则>[:<-规则>]]`，规则形如 `+$mux[S]`（只走 `$mux` 的 `S` 端口）或 `-$dff`（排除 `$dff`）。这部分是「逻辑锥选取」的高级用法，官方文档有完整图例。

#### 4.3.3 源码精读

整台机器的核心是 `select_stmt(design, arg)`：每收到一个空白分隔的 token，判断它属于哪一类指令并执行。先看顶部分流——`%` 操作符与 `@name`、普通模式的分派：

[passes/cmds/select.cc:686-819](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/select.cc#L686-L819)

```cpp
static void select_stmt(RTLIL::Design *design, std::string arg, ...) {
    ...
    if (arg[0] == '%') {
        if (arg == "%")  work_stack.push_back(design->selection());     // 压入当前选择
        else if (arg == "%u") { ... select_op_union(...); work_stack.pop_back(); }
        else if (arg == "%i") { ... select_op_intersect(...); ... }
        else if (arg == "%d") { ... select_op_diff(...); ... }
        else if (arg == "%n") { ... select_op_neg(...); }               // 取反
        else if (... "%ci" ...) select_op_expand(design, arg, 'i', false);
        ...
        return;
    }
    if (arg[0] == '@') {                                                // 引用命名选择
        ... work_stack.push_back(design->selection_vars[set_name]); ...
    }
    // 否则按 mod/member 模式解析(见下)
```

普通模式的解析：先把 `foo/bar` 切成 `arg_mod="foo"`、`arg_memb="bar"`（无 `/` 则整体当模块名），然后构造一个空的 `Selection`，遍历所有模块，用 `match_ids` 匹配模块名、再按成员前缀匹配成员并填进 `selected_members`：

[passes/cmds/select.cc:836-880](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/select.cc#L836-L880)

```cpp
size_t pos = arg.find('/');
if (pos == std::string::npos) { arg_mod = arg; ... }
else { arg_mod = arg.substr(0, pos); arg_memb = arg.substr(pos+1); ... }

bool full_selection = (arg == "*" && arg_mod == "*");
work_stack.push_back(RTLIL::Selection(full_selection, select_blackboxes, design));
RTLIL::Selection &sel = work_stack.back();
...
for (auto mod : design->modules()) {
    if (!select_blackboxes && mod->get_blackbox_attribute()) continue;  // 默认跳过黑盒
    if (!match_ids(mod->name, arg_mod)) continue;                       // 模块名匹配
    if (arg_memb == "") { sel.selected_modules.insert(mod->name); continue; }
    // 下面按 arg_memb 的前缀(t:/w:/c:/a:/...)分别匹配成员
```

成员前缀的分派就在紧接其后的代码里（[passes/cmds/select.cc:884-994](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/select.cc#L884-L994)），例如：

```cpp
if (arg_memb.compare(0, 2, "t:") == 0)                  // 按类型选 cell
    for (auto cell : mod->cells())
        if (match_ids(cell->type, arg_memb.substr(2)))
            sel.selected_members[mod->name].insert(cell->name);
else if (arg_memb.compare(0, 2, "a:") == 0)             // 按属性选(任意对象)
    for (auto wire : mod->wires())
        if (match_attr(wire->attributes, arg_memb.substr(2))) ...
    for (auto cell : mod->cells())
        if (match_attr(cell->attributes, arg_memb.substr(2))) ...
```

名字与属性的匹配函数分别是 `match_ids`（支持通配、`\` 公有名前缀、`$` 名后缀匹配）和 `match_attr`（支持 `= != < <= > >=` 比较与通配）：

[passes/cmds/select.cc:30-52](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/select.cc#L30-L52)

[passes/cmds/select.cc:106-139](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/select.cc#L106-L139)

集合运算的实现以「并集」为例，处理了与全选/黑盒的各种组合，最终把 `rhs` 的模块与成员并入 `lhs`：

[passes/cmds/select.cc:312-352](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/select.cc#L312-L352)

`%ci`/`%co`/`%x` 的「图遍历扩展」由 `select_op_expand` 实现：它先解析次数、上限、规则，然后反复调用单步扩展，沿 module 的 `connections()` 与每个 cell 的端口连通地扩选 wire/cell：

[passes/cmds/select.cc:558-660](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/select.cc#L558-L660)

最后，`SelectPass::execute` 决定这条 `select` 命令对「全局当前选择」的影响。几种模式（节选）：

[passes/cmds/select.cc:1499-1508](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/select.cc#L1499-L1508)

```cpp
if (clear_mode) { design->selection() = RTLIL::Selection::FullSelection(design); ... return; } // -clear
if (none_mode)  { design->selection() = RTLIL::Selection::EmptySelection(design); return; }    // -none
```

默认（无 `-add/-del/-set/...`）则是**替换**当前选择：

[passes/cmds/select.cc:1662-1664](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/select.cc#L1662-L1664)

```cpp
design->selection() = work_stack.back();
design->selection().optimize(design);
```

而 `-add`/`-del` 分别对当前选择做并/差（[passes/cmds/select.cc:1546-1562](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/select.cc#L1546-L1562)），`-set name` 把结果存进 `selection_vars` 供 `@name` 引用（[passes/cmds/select.cc:1634-1641](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/select.cc#L1634-L1641)）。`-list`/`-count` 则只展示不修改（[passes/cmds/select.cc:1510-1544](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/select.cc#L1510-L1544)）。

#### 4.3.4 代码实践

**目标**：用 `select` 的栈式语法精确框定 `$dff` 单元并计数；再用属性过滤器选出带特定属性的 wire。

**步骤 1：准备一个带属性的小设计**（新建自己的文件，不改仓库源码）。把下面内容存为 `/tmp/sel_demo.v`：

```verilog
// 示例代码 /tmp/sel_demo.v
module sel_demo(input clk, input rst, input [3:0] d, output reg [3:0] q);
    (* myattr = 42 *) wire [3:0] sum;   // 给这根 wire 打属性 myattr=42
    assign sum = d + 4'd1;
    always @(posedge clk)
        if (!rst) q <= 4'b0;
        else     q <= sum;
endmodule
```

**步骤 2：综合并选择 `$dff`**

```bash
yosys -p "read_verilog /tmp/sel_demo.v; synth; select -list t:\$dff; select -count t:\$dff"
```

- `select -list t:$dff`：列出所有类型为 `$dff` 的单元（可能出现 `$adff`/`$sdff`，按实际类型调整）。
- `select -count t:$dff`：打印数量（4 位寄存器通常对应 1 个位宽参数化的 `$dff`/`$adff`）。结果**待本地验证**。

**步骤 3：属性过滤**

```bash
yosys -p "read_verilog /tmp/sel_demo.v; synth; select -list a:myattr; select -list a:myattr=42"
```

- `a:myattr`：选出所有带 `myattr` 属性的对象（应只命中 `sum` 相关）。
- `a:myattr=42`：进一步要求属性值等于 42。

**步骤 4：组合操作符**（体会栈机器）

```bash
yosys -p "read_verilog /tmp/sel_demo.v; synth; select -list t:\$dff %ci"
```

`%ci` 把选中 `$dff` 的输入锥扩展一步，应额外带出驱动它的 `$add` 等单元与中间 wire。

**预期结果**：步骤 3 的 `a:myattr=42` 至少命中打标的那根 wire；步骤 4 的结果集比单独 `t:$dff` 更大。具体对象名因自动命名而异，记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：写一条选择表达式，选出「所有 `$add` 单元中带属性 `foo` 的那些」。
**答案**：`select -list t:$add a:foo %i`。先压入「全部 `$add`」，再压入「所有带 `foo` 属性的对象」，最后 `%i` 取交集。这正是官方文档强调的「多参数压栈 + 集合运算」范式（[docs/source/using_yosys/more_scripting/selections.rst:124-135](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/using_yosys/more_scripting/selections.rst#L124-L135)）。

**练习 2**：`select t:$add t:$sub` 和 `select t:$add %u t:$sub` 效果一样吗？为什么？
**答案**：一样。因为多个模式参数会分别压栈，表达式结束后 `SelectPass`/`handle_extra_select_args` 会把栈上剩余元素自动求并集（[passes/cmds/select.cc:1492-1495](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/select.cc#L1492-L1495)）；显式写 `%u` 只是把并集提前在栈上做完。两者最终结果相同。

**练习 3**：如何把当前选择存起来，稍后在另一条表达式里复用？
**答案**：`select -set myname <expr>` 把结果存入 `design->selection_vars["myname"]`；之后用 `select @myname` 把它压回栈，可继续叠加 `%i`/`%d` 等（[passes/cmds/select.cc:811-818](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/select.cc#L811-L818)）。

## 5. 综合实践

**任务**：用选择机制做一次「定向清理」——只删除设计里的 `$dff` 单元，再验证其余部分完好。

**背景**：`delete` 命令删除「当前选择」里的对象。结合本讲所学，你可以精确指定删除范围而不伤及无辜。

**操作步骤**：

1. 准备设计（复用上面的 `/tmp/sel_demo.v`）。
2. 先看综合后有什么：

   ```bash
   yosys -p "read_verilog /tmp/sel_demo.v; synth; stat"
   ```

   记下 `$dff`（或 `$adff`）的个数与总单元数。
3. 进入交互式 shell 做定向删除：

   ```bash
   yosys
   ```

   在 `yosys>` 提示符下逐条输入：

   ```text
   read_verilog /tmp/sel_demo.v
   synth
   select -list t:$dff            # 确认选中对象(应只有触发器)
   select t:$dff                   # 把当前选择设为 $dff
   delete                          # 只删除当前选择 = 只删 $dff
   select -clear                   # 恢复全选
   stat                            # 看 $dff 是否归零、其它单元是否还在
   ```

4. 把上面存成一个脚本 `/tmp/clean_dff.ys`，用 `yosys /tmp/clean_dff.ys` 一次跑完。

**需要观察的现象**：
- `select -list t:$dff` 只列出触发器单元。
- `delete` 之后 `stat` 显示 `$dff` 数量为 0，而 `$add` 等组合逻辑单元仍存在（可能因悬空而被 `opt_clean` 清理，但 `delete` 本身不清理组合逻辑）。
- 若把 `select t:$dff` 漏掉，`delete` 会删光整个设计——体会「当前选择」作为默认作用域的影响。

**预期结果**：定向删除后，触发器消失、组合逻辑保留。具体数量**待本地验证**。这个练习把本讲三个模块串起来：`Selection` 数据结构（4.1）描述范围、选择栈（4.2）承载 `delete` 的临时作用域、`select` 语法（4.3）用 `t:$dff` 精确框定对象。

## 6. 本讲小结

- **选择 = 一组对象的紧凑描述**。`RTLIL::Selection` 用「全选/全选非黑盒/显式枚举」三档加两张表表达任意子集，Pass 通过 `selected_module/selected_member` 查询，用 `optimize()` 保持表示最简。
- **当前选择放在选择栈顶**。`Design::selection_stack` 的栈顶就是「当前选择」，默认全选；`pop_selection` 在栈空时自动补全选，保证永不为空。
- **每条命令保持栈深度不变**。`Pass::call` 在执行前后用 `orig_sel_stack_pos` 守护深度，使命令尾巴上的 `[selection]` 参数（经 `extra_args → handle_extra_select_args → push_selection`）成为「临时覆盖、用完即弹」。
- **`select` 参数是一台栈机器**。模式（`t:`/`w:`/`a:`/...）压栈、`%` 操作符做集合运算与图遍历（`%u/%i/%d/%ci/...`）、`@name`/`%` 引用，结束时栈上元素求并集得最终选择。
- **`cd` / `select -module` 切换模块作用域**。设置 `selected_active_module` 后，对象名相对该模块解释、别的模块一律不选。
- **消费端不碰 `Selection` 细节**。Pass 用 `Module::selected_cells()/selected_members()` 等便捷函数，它们用 `design->selected(...)` 过滤——所以 `stat t:$dff`、`delete foo` 都自动支持选择。

## 7. 下一步学习建议

- 想看「选择」在真实综合 Pass 里如何被消费，可读 [u6-l3](u6-l3-opt.md)（`opt` 如何只对选中对象优化）和 `passes/opt/opt.cc`。
- 想深入「逻辑锥选取」的高级用法（`%ci*:-[CLK,S]:+$dff` 这类），精读 [passes/cmds/select.cc:558-660](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/select.cc#L558-L660) 的 `select_op_expand`，并对照官方文档 `docs/source/using_yosys/more_scripting/selections.rst` 的图例。
- 若你想自己写 Pass 并让它支持 `[selection]` 参数，只需在 `execute` 里调用 `extra_args(args, argidx, design)`——它会自动把尾巴上的选择推栈，下一讲 [u9-l1](u9-l1-write-custom-pass.md) 会演示完整的自定义 Pass。
- `cd` 与 `dump`、`show` 配合做交互式设计调查，参见文档 `docs/source/using_yosys/more_scripting/interactive_investigation.rst`。
