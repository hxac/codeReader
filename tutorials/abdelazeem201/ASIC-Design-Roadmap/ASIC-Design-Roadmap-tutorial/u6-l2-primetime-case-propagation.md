# PrimeTime 实用脚本：case analysis 追踪

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 **case analysis**（事例分析）在静态时序分析（STA）里到底做了什么，以及 `case_value` 与 `user_case_value` 这两个属性的区别——前者是"结果"，后者是"原因"。
- 理解 **timing arc（时序弧）** 的 `from_pin` / `to_pin` 是如何把整张设计连成一张有向图（timing graph）的，并知道为什么要"反向"（`-to`）查询弧。
- 读懂 `report_case_propagation.tcl` 这个约 118 行的 Tcl `proc`：它从一个带 case 值的 pin 出发，沿时序弧**反向回溯**，找出这个常量到底是从哪个 `set_case_analysis` 传播过来的。
- 掌握 Synopsys Tcl 的 **collection（集合）操作范式**：`get_pins`/`get_ports`、`filter_collection`、`foreach_in_collection`、`get_attribute`、`sizeof_collection`、`index_collection`、`remove_from_collection`、`append_to_collection`。
- 看懂用**普通列表模拟栈、实现深度优先回溯**的写法，并能解释 `visited_pairs` 数组如何给回溯提供"终止保证"。
- 理解 `parse_proc_arguments` + `define_proc_attributes` 如何让一个自定义 `proc` 拥有像原生命令那样的参数校验与 `-help` 帮助。

本讲是 **U6 静态时序分析** 单元的第二讲，承接 u6-l1（PrimeTime STA 基本流程）。u6-l1 告诉你 PrimeTime 怎么"读网表 → 反标寄生 → 读约束 → 报时序"；本讲则聚焦在那条流程中的一个**调试利器**——当你在报告里发现某条路径"莫名其妙消失了"、某个 pin"莫名其妙被钉成了常量"，该用哪个脚本、怎么把根因挖出来。

## 2. 前置知识

在进入源码之前，先把四个概念讲清楚。

### 2.1 什么是 case analysis（为什么 STA 里会有"钉死"的常量）

做 STA 时，设计里有些信号在**某种工作模式下永远是固定逻辑值**。例如一块芯片的"测试使能"管脚 `test_mode`，在功能模式下永远为 `0`；CPU 的某个配置寄存器选择位，永远停在 `1`。这些恒定的值会让逻辑里某些支路**根本不可能被走到**。

PrimeTime 提供命令 `set_case_analysis`，让你显式地把某个 pin 或 port"钉"在一个常量上（`0`/`1`/`rise`/`fall`）：

```
set_case_analysis 1 [get_pins u_mux/S]
```

这一钉，工具就会**屏蔽**在该常量下不可能导通的时序弧。比如一个二选一多路选择器（mux），选择端 `S` 被钉成 `1`，那么从 `I0` 输入到输出 `Z` 的那条路径就永远走不通——STA 自然不再检查它。这正是 case analysis 的两大用途：**剔除假路径**、**按模式简化分析**（功能模式只看功能路径，测试模式只看测试路径）。

### 2.2 case_value 与 user_case_value：结果 vs 原因

一个常量被钉下后，会沿组合逻辑**向下游传播**。比如 mux 的 `S` 端被 `set_case_analysis` 钉成 `1`，那么经过这个 mux 后，`I1` 到 `Z` 这一支的输出在一定条件下也会恒定为某个值——这个"派生出来的常量"不是用户直接设的，而是**传播**来的。PrimeTime 用两个属性区分这两类：

| 属性 | 含义 | 是不是"源头" |
|------|------|--------------|
| `user_case_value` | 用户用 `set_case_analysis` **显式设置**的值 | ✅ 是源头（根因） |
| `case_value` | pin 上**实际生效**的常量值（可能是用户设的，也可能是上游传播来的） | 不一定是源头 |

一个 pin 可能有 `case_value`（它确实恒为常量）但 `user_case_value` 为空（这个常量是别处传过来的）。**找到 `user_case_value` 不为空的 pin，就找到了根因。** 这正是本脚本的核心判断依据（见 4.1.3 的 `user_case_value` 判空）。

### 2.3 什么是 timing arc（时序弧）

STA 把每个单元抽象成若干条**有向延迟边**，称为时序弧（timing arc）。一条时序弧从单元的一个 pin（`from_pin`）指向另一个 pin（`to_pin`），表示信号"从这一脚传到那一脚要花多少时间"。例如：

- 一个二输入与门 `AND2` 有两条弧：`A→Z`、`B→Z`。
- 一个寄存器有 `D→Q`（传播弧，clock 触发后数据从 D 到 Q）和 `clock_pin→D`（setup/hold 检查弧）。

把所有单元的时序弧首尾相连，整颗芯片就变成了一张巨大的**有向图（timing graph）**。STA 就是在这张图上做路径搜索。

关键命令 `get_timing_arcs -to $pin` 返回所有"**指向**该 pin"的弧——也就是问"谁会传到这里来"。**反向查询弧，就是反向回溯的入口**（见 4.1.3）。

### 2.4 为什么要"反向回溯"找根因

已知 `case_value`（结果）要找 `user_case_value`（原因），方向是**从下游往上游**走：

- 当前 pin `Z` 有 case 值 → 看哪些弧指向 `Z`（`get_timing_arcs -to Z`）→ 找到 `from_pin` 们 → 看哪个 `from_pin` 自己也有 `case_value` → 它就是嫌疑来源 → 继续往它的上游走……
- 直到走到一个 `user_case_value` 不为空的 pin——那就是源头，回溯在此终止。

这就像顺着河流找源头：站在入海口（带 case 值的 pin），沿支流（时序弧）逆流而上，找到真正的泉眼（`set_case_analysis` 钉的点）。

> 术语速查：case analysis、`set_case_analysis`、`case_value`/`user_case_value`、timing arc、`from_pin`/`to_pin`、timing graph、collection、反向回溯（backtrace）、深度优先（DFS）。

## 3. 本讲源码地图

本讲只涉及**一个文件**，它独立成篇，是一个可直接 `source` 后调用的 Tcl `proc`：

| 文件 | 行数 | 作用 |
|------|------|------|
| [PrimeTime/UsefulScripts/report_case_propagation.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/UsefulScripts/report_case_propagation.tcl) | 118 | 定义 `proc report_case_propagation`：从带 case 值的 pin/port 出发，沿时序弧反向回溯，打印一棵缩进树，标注每个节点的 case 值与分支信息，直到找到 `set_case_analysis` 的源头。 |

几点背景说明：

- 这是 `PrimeTime/UsefulScripts/` 目录下唯一的脚本，属于 Synopsys 风格的**应用脚本（application script）**。文件头注释（[report_case_propagation.tcl:1-7](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/UsefulScripts/report_case_propagation.tcl#L1-L7)）写明作者邮箱 `abdelazeem@synopsys.com`、版本 v1.01（2024-05-31 修复了"两 pin 间存在并行弧"的 bug）。
- 它**不依赖** u6-l1 讲过的 `common_setup.tcl`/`pt_setup.tcl`，自己不带任何变量。但它**必须在一个已经 `link_design` 并且执行过 `set_case_analysis` + `update_timing` 的 PrimeTime 会话里**才能用——因为只有那时，设计里的 pin 才真正带上了 `case_value` 属性。本仓库不提供对应的测试设计数据，所以脚本本身的"运行结果"需要你**待本地验证**（见 4.2.4）。
- 它采用 Synopsys Tcl 的 `collection` API（对象以集合形式返回），并用 `parse_proc_arguments` + `define_proc_attributes` 把自己包装成一条"像原生命令"的命令。这两点正是本讲要重点拆解的工程技巧。

## 4. 核心概念与源码讲解

本讲按规格拆成四个最小模块：① 时序弧与 case 属性；② 深度优先回溯算法；③ `proc` 与 `define_proc_attributes`；④ collection 操作与访问去重。

### 4.1 时序弧与 case 属性

#### 4.1.1 概念说明

这个模块解决的问题是：**脚本怎么知道一个 pin 上有 case 值、又怎么找到它的上游来源？** 答案靠两条 Synopsys Tcl 能力：

1. **属性查询** `get_attribute`：能读出 pin 的 `case_value`、`user_case_value`、`object_class`（是 `pin` 还是 `port`）、所在单元的 `ref_name`（参考单元名）等。
2. **时序弧查询** `get_timing_arcs -to $pin`：返回所有"指向该 pin"的弧，每条弧又有 `from_pin` 属性——这就是上游来源。

把两者结合：对当前 pin 查"谁指过来"（弧），再筛出"上游 pin 自己也有 case 值"的那些，就得到了候选来源。

#### 4.1.2 核心流程

对单个 `to_pin`，脚本这样找候选上游（伪代码）：

```
读取 to_pin 的 user_case_value
如果 user_case_value 为空（即该 pin 的常量是传播来的，不是用户直接设的）：
    对每条 "指向 to_pin" 的时序弧 arc：
        取 arc.from_pin
        过滤：只保留 from_pin 自己 case_value 有定义的那些
    汇总成 valid_from_pins（候选来源集合）
否则（user_case_value 不为空）：
    valid_from_pins = 空   ← 这就是回溯的"叶子/源头"，不再往上找
```

**关键洞察**：当 `user_case_value` 不为空时，候选集合保持为空，回溯就在此终止。所以这棵回溯树的**叶子节点永远是 `set_case_analysis` 的源头**——这正是脚本的目的。

#### 4.1.3 源码精读

整段"找候选上游"的逻辑落在主循环里这几行：

[report_case_propagation.tcl:34-41](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/UsefulScripts/report_case_propagation.tcl#L34-L41) —— 这是 4.1.2 伪代码的直接翻译：

```tcl
set valid_from_pins {}
if {[set user_case_value [get_attribute -quiet $to_pin user_case_value]] eq {}} {
  foreach_in_collection arc [get_timing_arcs -quiet -to $to_pin] {
    if {[set from_pin [filter_collection [get_attribute $arc from_pin] {defined(case_value)}]] ne {}} {
     append_to_collection -unique valid_from_pins $from_pin
    }
  }
}
```

逐句对照：

- `set user_case_value [get_attribute -quiet $to_pin user_case_value]`：读 `to_pin` 的用户设置值。`-quiet` 表示"没有该属性时不报错、返回空字符串"，这正是我们判断"是不是源头"的依据。
- `if {... eq {}}`：如果 `user_case_value` 为空（非源头），才进入"找上游"的分支。**这就是回溯的终止条件之一。**
- `get_timing_arcs -quiet -to $to_pin`：反向查询所有指向 `to_pin` 的时序弧。
- `get_attribute $arc from_pin`：取这条弧的起点 pin。
- `filter_collection ... {defined(case_value)}`：用过滤器表达式 `defined(case_value)` 只留下"自己也有 case 值"的 from_pin——因为只有 case 值的传播才会沿着有常量的支路走，没 case 值的上游与本次回溯无关。
- `append_to_collection -unique valid_from_pins $from_pin`：去重地把候选来源加进 `valid_from_pins` 集合。`-unique` 很重要：两个并行弧可能指向同一个 from_pin（v1.01 正是修这个 bug），去重避免重复回溯。

打印 case 信息时，脚本再次区分了两种属性，这正是输出里"源头"与"中间节点"的视觉差别：

[report_case_propagation.tcl:73-77](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/UsefulScripts/report_case_propagation.tcl#L73-L77)：

```tcl
if {$user_case_value ne {}} {
  set case_info "user-defined case=$user_case_value"
} else {
  set case_info "case=[get_attribute -quiet $to_pin case_value]"
}
```

也就是说，输出文本里：

- **`user-defined case=...`**：这一行就是源头，是某条 `set_case_analysis` 的落点。
- **`case=...`**：这一行只是常量传播途中的"中转 pin"，还要继续往上找。

另外，打印每个 pin 时还要附上它的"参考单元"信息，便于在版图/网表里定位。脚本对 `pin` 和 `port` 两种对象分类处理：

[report_case_propagation.tcl:55-62](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/UsefulScripts/report_case_propagation.tcl#L55-L62)：

```tcl
switch [get_attribute $to_pin object_class] {
 pin {
  if {[set ref_info [get_object_name [get_lib_cells -quiet -of [get_cells -quiet -of $to_pin]]]] eq {}} {
   set ref_info [get_attribute [get_cells -quiet -of $to_pin] ref_name]
  }
 }
 port {set ref_info "[get_attribute $to_pin port_direction] port"}
}
```

它先用 `object_class` 区分对象类型：是 `pin` 就取它所在单元的库单元名（`get_lib_cells -of [get_cells -of $to_pin]`，先由 pin 找 cell、再由 cell 找 lib_cell）；若取不到则退而取 `ref_name`；是 `port`（顶层端口）则打印方向（`inout`/`input`/`output`）。这段体现了 collection API 的"链式 -of 取关联对象"用法（见 4.4.3）。

#### 4.1.4 代码实践

**实践目标**：在不跑 PrimeTime 的前提下，靠阅读源码把"`user_case_value` 是否为空"这条分支在脚本里的"控制权"梳理清楚，并预言脚本在两类 pin 上的不同行为。

**操作步骤**：

1. 打开 [report_case_propagation.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/UsefulScripts/report_case_propagation.tcl)，定位到第 35 行的 `if`。
2. 假设 pin `A` 的 `user_case_value = rise`（用户显式设过），pin `B` 的 `user_case_value` 为空、`case_value = zero`（传播来的）。分别代入这段逻辑，回答：`A` 和 `B` 各自的 `valid_from_pins` 会是什么？谁会成为回溯的叶子？
3. 再看第 73–77 行的 `case_info` 赋值，预测 `A` 和 `B` 在最终输出里分别会印成 `user-defined case=...` 还是 `case=...`。

**需要观察的现象 / 预期结果**：

- pin `A`（user_case_value 非空）：`valid_from_pins` 保持为空 → 第 82–84 行 `if {$unvisited_from_pins eq {}} continue` 命中，主循环**不再为它压入任何上游** → 它是叶子；输出印成 `user-defined case=rise`。
- pin `B`（user_case_value 空、case_value=zero）：进入第 36–40 行，沿 `-to` 弧找上游，`valid_from_pins` 非空 → 继续往上回溯；输出印成 `case=zero`。

> 说明：以上是**源码阅读型实践**的结论。若要在真实 PrimeTime 会话里复现，需要先 `link_design` 一个含 `set_case_analysis` 的设计并 `update_timing`，本仓库未提供该数据，故运行截图**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：脚本为什么用 `defined(case_value)` 作为 `filter_collection` 的过滤表达式，而不是直接判断 `case_value` 等不等于某个固定值？

**参考答案**：因为 case 值可能是 `0`/`1`/`rise`/`fall` 四种之一，脚本要找的是"**存在** case 值的上游"，而非"等于某特定值"。`defined(case_value)` 这个过滤器表达式正是"该属性有定义"的语义，它统一覆盖了四种取值；若写死成某个值，反而会漏掉其它三种 case。

**练习 2**：第 35 行的 `get_attribute` 加了 `-quiet`，如果去掉会怎样？

**参考答案**：不是每个 pin 都有 `user_case_value` 属性（绝大多数 pin 根本没被 `set_case_analysis` 设过）。不加 `-quiet` 时，`get_attribute` 在属性缺失时会**抛错（error）**，整个 `proc` 就中断了。加 `-quiet` 后缺失属性返回空字符串 `""`，正好配合后面的 `eq {}` 判断，使脚本对"没有该属性的 pin"也能正常处理。

---

### 4.2 深度优先回溯算法

#### 4.2.1 概念说明

这个模块解决：**怎么把这棵回溯树"走完"且不重复、不死循环？** 脚本没有用递归，而是用**一个普通 Tcl 列表手动管理待访问节点**。文件头注释里作者特意写了一句自嘲（[report_case_propagation.tcl:16-17](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/UsefulScripts/report_case_propagation.tcl#L16-L17)）：

> "I use a flat context list instead of succumbing to the lure of recursion..."（我用一个扁平的上下文列表，而不是屈服于递归的诱惑……）

这里有一个**必须澄清的关键点**：变量名叫 `queued_contexts`（"队列"），但它的实际行为是**深度优先（DFS）**，不是广度优先（BFS）。原因在于存取都发生在列表的**同一端**——这是**栈（LIFO）**的特征，而非队列（FIFO）。代码自己的注释（[report_case_propagation.tcl:97-99](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/UsefulScripts/report_case_propagation.tcl#L97-L99)）也明说："the fact that we push it last causes the depth-first backtrace"（我们最后才压入它，正是这一点导致了深度优先回溯）。所以本讲以源码为准，按 **DFS** 讲解。

为什么要深度优先？因为回溯一棵"找根因"的树时，DFS 会**沿一条支路一口气走到底（走到源头）再回头**，这样输出树天然就是"一条完整传播链 → 再切下一条"的结构，便于人眼逐条读。BFS 会一层一层铺开，反而把不同支路混在一起。

#### 4.2.2 核心流程

主循环（`while {$queued_contexts ne {}}`）每轮做这些事：

```
从列表头部取出一个 context = [to_pin, indent_level]        # 弹出（栈顶）

计算 valid_from_pins（带 case 的上游，见 4.1）
计算 unvisited_from_pins = 去掉已访问弧后的上游

打印当前 to_pin（带缩进、参考单元、case 信息、分支信息）

若 unvisited_from_pins 为空：continue（处理下一个 context）   # 叶子或已覆盖

取 unvisited_from_pins 的第 1 个 from_pin
若还剩别的 from_pin：把 [to_pin, indent_level] 重新压回头部     # 留着回来走别的分支
若 valid_from_pins > 1：indent_level++                          # 进更深一层
把 [from_pin, indent_level] 压入头部                            # 下一轮就处理它 → DFS
把弧 (from_pin, to_pin) 记入 visited_pairs                      # 永不再走
```

"压入头部 + 从头部取出"= 栈 = DFS；而"先把 to_pin 压回、再把 from_pin 压入"，使得 from_pin 排在 to_pin 前面，下一轮必然先弹出 from_pin——于是沿这条支路继续向下钻，直到这条链走完，才轮到之前压回的 to_pin 处理它的下一个分支。

#### 4.2.3 源码精读

主循环骨架（[report_case_propagation.tcl:25-31](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/UsefulScripts/report_case_propagation.tcl#L25-L31)）：

```tcl
while {$queued_contexts ne {}} {
  set context [lindex $queued_contexts 0]          ;# 从头部取
  foreach {to_pin indent_level} $context {}
  set queued_contexts [lrange $queued_contexts 1 end]   ;# 弹掉头部
  ...
```

注意 `lindex ... 0` 取头部、`lrange ... 1 end` 去掉头部，这一对组合就是"从栈顶弹出"。`foreach {to_pin indent_level} $context {}` 是 Tcl 解包列表的惯用法：把 `[pin 0]` 拆成 `to_pin` 和 `indent_level` 两个变量。

分支处理与"压回 to_pin"（[report_case_propagation.tcl:86-95](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/UsefulScripts/report_case_propagation.tcl#L86-L95)）：

```tcl
set from_pin [index_collection $unvisited_from_pins 0]
set unvisited_from_pins [remove_from_collection $unvisited_from_pins $from_pin]
# 还有别的分支？把当前 to_pin 重新压回头部，稍后回来走剩余分支
if {$unvisited_from_pins ne {}} {
  set queued_contexts [linsert $queued_contexts 0 [list $to_pin $indent_level]]
}
```

`linsert $list 0 ...` 表示在**索引 0（头部）**插入。这里如果 to_pin 还有其它未访问的上游分支，就把它自己重新压回头部，等当前这条支路走完再回来处理剩下的分支——这正是"分支回溯"的实现。

真正决定"深度优先"的那一步（[report_case_propagation.tcl:100-103](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/UsefulScripts/report_case_propagation.tcl#L100-L103)）：

```tcl
if {[sizeof_collection $valid_from_pins] > 1} {
  incr indent_level
}
set queued_contexts [linsert $queued_contexts 0 [list $from_pin $indent_level]]
```

把新的 `from_pin`（更深的节点）压入头部。因为它被压在最前面，下一轮 `lindex ... 0` 立刻就取到它——于是回溯**沿这条支路继续向下钻**。这正是作者注释里说的"push it last causes depth-first"。`incr indent_level` 则让输出缩进+1，使这棵树在视觉上更深一层。

分支信息的打印（[report_case_propagation.tcl:63-72](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/UsefulScripts/report_case_propagation.tcl#L63-L72)）把"分了几支、走到第几支"显示出来：

```tcl
if {[sizeof_collection $valid_from_pins] >= 1 && [sizeof_collection $unvisited_from_pins]==0} {
  set branch_info " (previously covered path)"
} elseif {[sizeof_collection $valid_from_pins] > 1} {
  set branch_info " (branch [expr {1+[sizeof_collection $valid_from_pins]-[sizeof_collection $unvisited_from_pins]}] of [sizeof_collection $valid_from_pins] follows)"
} else {
  set branch_info {}
}
```

三种情况：① 有上游但全走过了 → `(previously covered path)`；② 上游多于 1 个 → `(branch X of Y follows)`，其中 Y=`valid_from_pins` 总数、X=已处理的分支序号；③ 只有一个上游 → 不附加分支信息（单条明确路径）。

最终一行输出（[report_case_propagation.tcl:78](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/UsefulScripts/report_case_propagation.tcl#L78)）把缩进、pin 名、参考单元、case 信息、分支信息拼起来：

```tcl
echo "[string repeat { } $indent_level][get_object_name $to_pin] ($ref_info) $case_info${branch_info}"
```

`[string repeat { } $indent_level]` 按深度生成前导空格，于是回溯深度直接反映为缩进量。下面是一段**示例输出**（说明：本仓库无配套测试设计，此为依据脚本逻辑构造的示意，非真实运行截图，**待本地验证**）：

```
top/u_mux/Z (MUX2X1) case=one
 top/u_mux/I1 (AND2X1) case=one
  top/u_mux/I0 (MUX2X1) user-defined case=one (previously covered path)
 top/u_mux/S (INVX1) user-defined case=zero (previously covered path)
```

读法：`Z` 的常量来自两条上游支路——左边一路最终追到 `I0`（user-defined，源头，标 `previously covered path`），右边一路直接追到 `S`（user-defined，源头）。

#### 4.2.4 代码实践

**实践目标**：手工模拟一遍主循环在"一个 to_pin 有两个上游分支"时的压栈/弹栈过程，体会"深度优先"是怎么从 `linsert ... 0` 的顺序里自然涌现的。

**操作步骤**：

1. 假设初始 `queued_contexts = { [Z, 0] }`（只从 pin `Z` 开始，缩进 0）。
2. 设 `Z` 的 `valid_from_pins = {A, B}`（两个带 case 的上游），且都未访问。
3. 按第 87–103 行的代码，逐步写下每次 `linsert` / `lindex 0` 之后 `queued_contexts` 的内容，预测被打印的顺序是 `Z → A → (A 的上游…) → B → …`，而不是 `Z → A → B → …`。
4. 对照第 68 行的 `branch X of Y`，预言 `Z` 第一次打印时分支信息是 `branch 1 of 2 follows`、被压回后第二次打印时是 `branch 2 of 2 follows`。

**需要观察的现象 / 预期结果**：

- 因为 `from_pin`（A）被 `linsert` 在 `to_pin`（Z）**之前**压入头部，下一轮先取 A，所以会先把 A 这条支路走到底，再回头处理 B——这就是 DFS。
- `Z` 会被打印**两次**：第一次标记 `branch 1 of 2 follows`（此时 unvisited 还有 B），第二次标记 `branch 2 of 2 follows`（此时 unvisited 已空，应配合 `(previously covered path)` 或继续走 B）。

> 说明：这是纯源码追踪，不依赖工具。若要真跑，需 PrimeTime 会话，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果把第 103 行的 `linsert $queued_contexts 0 ...` 改成 `linsert $queued_contexts end ...`（压到尾部），遍历顺序会变成什么？

**参考答案**：压到尾部、而弹出仍从头部（`lindex 0`），就变成了真正的**队列（FIFO）**，遍历会变成**广度优先（BFS）**——一层一层铺开，先把 Z 的所有直接上游（A、B）都打印，再打印它们各自的上游。输出树会变"宽"而非"深"，不利于逐条阅读传播链。

**练习 2**：为什么作者坚持用扁平列表而不是递归？

**参考答案**：Tcl 默认递归深度有限，且每层递归有自己的局部变量栈；遇到规模大、又有大量重汇聚（reconvergent fanout）的设计，递归很容易栈溢出或重复访问同一子树。扁平列表把"待访问节点"全部摊在一个变量里，配合 `visited_pairs` 显式去重，既能走完所有支路，又天然避免了递归深度问题。

---

### 4.3 proc 与 define_proc_attributes

#### 4.3.1 概念说明

这个模块解决：**怎么让一个自定义 Tcl `proc` 用起来像 PrimeTime 原生命令一样——带参数校验、带 `-help`、参数写错会报错？** Synopsys 工具提供了两个配套命令：

- `parse_proc_arguments`：在 `proc` 体内，把调用方传来的 `$args` 按"参数名→值"解析进一个数组（这里是 `results`）。
- `define_proc_attributes`：在 `proc` 定义之后，给这个 `proc` 挂上元数据（`-info` 一句话说明、`-define_args` 参数表）。

有了这两样，调用 `report_case_propagation -pins_ports xxx` 时，工具会自动校验参数名拼写、是否必填，并对 `help report_case_propagation` 给出格式化帮助。这是 Synopsys 应用脚本的"标准包装"。

#### 4.3.2 核心流程

```
proc 报告命令 {args} {
    parse_proc_arguments -args $args results   ;# 把 -pins_ports xxx 塞进 results(pins_ports)
    …… 主体逻辑引用 $results(pins_ports) ……
}
define_proc_attributes 报告命令 \
    -info "一句话功能说明" \
    -define_args { {参数名 说明文 参数名 类型 是否必填} }
```

#### 4.3.3 源码精读

`proc` 定义与参数解析（[report_case_propagation.tcl:9-14](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/UsefulScripts/report_case_propagation.tcl#L9-L14)）：

```tcl
proc report_case_propagation {args} {
 parse_proc_arguments -args $args results

 set objects {}
 append_to_collection objects [get_pins -quiet $results(pins_ports)]
 append_to_collection objects [get_ports -quiet $results(pins_ports)]
```

`{args}` 表示这个 `proc` 接受任意参数列表；`parse_proc_arguments -args $args results` 把它解析进数组 `results`。之后用 `$results(pins_ports)` 取出"用户传进来的 pin/port 描述"。注意第 13–14 行同时尝试用 `get_pins` 和 `get_ports` 去匹配输入——因为用户给的可能是 pin 也可能是 port，两个 `-quiet` 查询保证其中之一命中、另一个空着也不会报错。

参数规格声明（[report_case_propagation.tcl:112-117](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/UsefulScripts/report_case_propagation.tcl#L112-L117)）：

```tcl
define_proc_attributes report_case_propagation \
 -info "trace case analysis back to source(s)" \
 -define_args \
 {
  {pins_ports {pin(s)/port(s) where case value is present} pins_ports string required}
 }
```

`-define_args` 里每一项的格式是：`{名字 {帮助文本} one_value_var 类型 required|optional}`。这里声明了唯一参数 `pins_ports`：类型 `string`、`required`（必填）。声明之后：

- 写错参数名（如 `-pin_ports`）会被工具直接拒绝；
- `help report_case_propagation` 会打印出 `-info` 和这张参数表；
- 调用时既可写 `report_case_propagation [get_pins X]`，也可显式写 `report_case_propagation -pins_ports [get_pins X]`。

#### 4.3.4 代码实践

**实践目标**：通过阅读 `define_proc_attributes`，学会给任意自定义 `proc` 加上参数校验。

**操作步骤**：

1. 在本仓库内再找一个 Synopsys Tcl 脚本对比（例如 `mentor_scripts/createpathgroup.tcl` 或 `IC Compiler II/Vpad.tcl`），看它们是否也用了 `define_proc_attributes`，体会"带参校验"与"裸 proc"的差别。
2. 在本机任意 Tcl 环境（不必是 PrimeTime，普通 `tclsh` 即可验证 `proc`/`define_proc_attributes` 的语法）写一个最小示例（**示例代码，非项目原有**）：

   ```tcl
   proc greet {args} {
       parse_proc_arguments -args $args results
       puts "Hello, $results(name)!"
   }
   # 注意：define_proc_attributes 是 Synopsys 工具命令，
   # 原生 tclsh 没有，这里仅示意 proc 主体结构。
   greet -name Alice
   ```

3. 回到本脚本，回答：为什么主体里取参数用 `$results(pins_ports)` 而不是 `$args`？

**需要观察的现象 / 预期结果**：

- `$args` 是原始参数列表（如 `-pins_ports xxx`），是"没拆封"的；`results` 数组才是"按参数名拆好的"，用 `$results(pins_ports)` 才能稳定取到值，无论用户是否显式写了 `-pins_ports`。
- 真正的 `parse_proc_arguments`/`define_proc_attributes` 只在 Synopsys 工具的 `pt_shell`/`icc2_shell` 里存在；普通 `tclsh` 跑上面示例会报"command not found"，这属于**待本地验证（需 Synopsys 环境）**。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `pins_ports` 的 `required` 改成 `optional`，脚本主体需要相应做什么改动才不会出错？

**参考答案**：改成 optional 后，用户可能不传该参数，`results(pins_ports)` 可能不存在或为空，`get_pins -quiet $results(pins_ports)` 仍可工作（`-quiet` 容错），但 `objects` 会为空、主循环不执行。更稳妥的做法是在 `parse_proc_arguments` 之后加一句 `if {$results(pins_ports) eq ""} { return }` 提前返回并给出提示。

**练习 2**：`-info "trace case analysis back to source(s)"` 这句话在哪里会被用户看到？

**参考答案**：在 PrimeTime 里执行 `help report_case_propagation` 时，工具会把 `-info` 的文本作为一句话摘要打印出来，同时附上 `-define_args` 的参数表。这是 Synopsys 工具命令体系的统一帮助机制。

---

### 4.4 collection 操作与访问去重

#### 4.4.1 概念说明

Synopsys Tcl 的核心数据结构是 **collection（集合）**：`get_pins`/`get_ports`/`get_cells`/`get_timing_arcs` 等查询命令返回的都是集合，而非普通字符串列表。集合有一套专用的操作命令（不能直接用 Tcl 原生的 `foreach`/`lindex`）。这个模块把脚本里用到的全部 collection 操作汇总讲清，并讲透 `visited_pairs` 数组如何为回溯提供"终止保证"。

#### 4.4.2 核心流程

脚本里反复出现的 collection 操作模式：

```
get_pins / get_ports / get_cells / get_lib_cells / get_timing_arcs   # 查询，返回集合
append_to_collection <var> <collection>                              # 往集合变量里追加（去重可选）
filter_collection <collection> {表达式}                              # 按属性过滤
foreach_in_collection <item> <collection> { ... }                    # 遍历集合
get_attribute <obj> <属性>                                           # 取对象属性
sizeof_collection <collection>                                       # 集合大小
index_collection <collection> <i>                                    # 取第 i 个
remove_from_collection <collection> <obj>                            # 删去某些对象
get_object_name <obj>                                                # 取对象的全名（字符串）
```

去重与终止则用一个普通 Tcl 数组 `visited_pairs`（不是 collection）：

```
键：  [list $from_pin_name $to_pin_name]   ← 以"弧"为单位
值：  1
判定：if {![info exists visited_pairs($key)]} { ... 没走过，处理 ... }
标记：set visited_pairs($key) 1
```

#### 4.4.3 源码精读

**初始化**两 个去重数组（[report_case_propagation.tcl:23-24](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/UsefulScripts/report_case_propagation.tcl#L23-L24)）：

```tcl
array unset visited_from_pins
array unset visited_pairs
```

> ⚠️ **代码阅读发现（忠于源码）**：`visited_from_pins` 在这里被 `array unset` 了，但**整个脚本此后再没有对它做任何读写**——既没有 `set visited_from_pins(...)`，也没有 `info exists visited_from_pins(...)`。真正起作用的只有 `visited_pairs`。`visited_from_pins` 很可能是早期版本遗留的"死变量"。这一观察体现了"读源码要带着审视"——不要假定每个变量都有效。

**终止的核心：按"弧"去重**（[report_case_propagation.tcl:46-52](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/UsefulScripts/report_case_propagation.tcl#L46-L52)）：

```tcl
set unvisited_from_pins {}
foreach_in_collection from_pin $valid_from_pins {
  set from_pin_name [get_object_name $from_pin]
  if {![info exists visited_pairs([list $from_pin_name $to_pin_name])]} {
   append_to_collection unvisited_from_pins $from_pin
  }
}
```

注意键是 `[list $from_pin_name $to_pin_name]`——**以"from→to 这条有向弧"为单位**，而不是单独以 from_pin 或 to_pin 为单位。这一点很关键：同一个 from_pin 可能通过不同弧连到不同 to_pin，按"弧"去重才能精确表达"这条边走没走过"。

**标记已访问**（[report_case_propagation.tcl:106](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/UsefulScripts/report_case_propagation.tcl#L106)）：

```tcl
set visited_pairs([list $from_pin_name $to_pin_name]) 1
```

每当决定沿某条弧 `from_pin → to_pin` 回溯，就立刻把这条弧写进 `visited_pairs`。此后任何支路再次试图走这条弧，都会被第 49 行的 `info exists` 判定为"已走过"而被剔除（落入 `unvisited_from_pins` 为空 → `continue`）。

collection 的其余操作散见各处，集中认识一下：

- `append_to_collection -unique valid_from_pins $from_pin`（[第 38 行](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/UsefulScripts/report_case_propagation.tcl#L38)）：把 from_pin 追加进候选集合，`-unique` 保证两个并行弧指向同一 from_pin 时只记一次。
- `index_collection $unvisited_from_pins 0`（[第 87 行](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/UsefulScripts/report_case_propagation.tcl#L87)）：取集合第 0 个元素（collection 版的 `lindex 0`）。
- `remove_from_collection $unvisited_from_pins $from_pin`（[第 89 行](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/UsefulScripts/report_case_propagation.tcl#L89)）：从集合里删掉刚取出的那个，剩下的留作"其它分支"。
- `sizeof_collection ...`（多处）：取集合元素个数，用于分支计数与判空。
- 链式 `-of` 取关联对象（[第 57 行](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/UsefulScripts/report_case_propagation.tcl#L57)）：`get_lib_cells -of [get_cells -of $to_pin]`，由 pin 找 cell、由 cell 找库单元，一步套一步。

**为什么按"弧"去重能给回溯提供终止保证？** 因为整颗设计的时序弧是**有限集**：每条弧 `(from_pin, to_pin)` 最多被记一次。`visited_pairs` 单调增长，迟早所有可走的弧都被标记完，`unvisited_from_pins` 处处为空，主循环的 `continue` 不断命中，队列最终被消耗光，`while {$queued_contexts ne {}}` 自然结束。即便存在组合环或重汇聚扇出，也不会陷入死循环——这是脚本能"必然结束"的数学保证。形式地，设时序弧总数为 \(N\)，则主循环体被执行"压栈"操作（第 106 行）的次数上界为 \(N\)，于是回溯复杂度与 \(N\) 同阶：

\[
\text{压栈次数} \leq N \quad\Rightarrow\quad \text{回溯必然终止}
\]

#### 4.4.4 代码实践（本讲必做的核心实践）

**实践目标**：把"练习任务"——**描述 `visited_pairs` 数组如何避免重复访问同一时序弧、从而终止回溯**——落到具体的源码行上，并用一个构造场景预言脚本行为。

**操作步骤**：

1. 在源码里定位三处与 `visited_pairs` 相关的行：声明/清空（第 24 行）、查询（第 49 行 `info exists visited_pairs(...)`）、标记（第 106 行 `set visited_pairs(...) 1`）。看清键的构成是 `[list $from_pin_name $to_pin_name]`。
2. 构造一个"重汇聚"小场景（**示例场景，非仓库数据**）：假设存在弧 `A→B`、`A→C`、`B→D`、`C→D`，从 `D` 开始回溯。画出 `visited_pairs` 在每一步后的内容。
3. 回答两个问题：
   - 若**没有** `visited_pairs`，从 `D` 出发会怎样？（提示：`D→B→A` 与 `D→C→A` 都到 `A`，`A` 是否会被重复压栈？组合环下会不会无限循环？）
   - 键若改成**只**用 `from_pin_name`（不配 `to_pin_name`），会有什么副作用？（提示：同一个 from_pin 经不同弧连到多个 to_pin 时会怎样？）

**需要观察的现象 / 预期结果**：

- 有 `visited_pairs` 时：`D` 处理时压入弧 `B→D`、`C→D`；走到 `B` 压入 `A→B`；走到 `C` 时 `A→C` 未访问、压入；`A` 为叶子（假设它是 user-defined 源头）。每条弧恰好被标记一次，回溯在走完所有弧后干净结束。
- 若去掉它：`A` 会被两条支路各压一次（重复打印），遇到组合环时更是**无限压栈、永不终止**。
- 若键只用 `from_pin_name`：从 `D` 走到 `B` 时会把 `A`（作为 from_pin）标记，于是从 `D` 走到 `C` 再想看 `A→C` 时，`A` 已"被标记"而被误判为已访问——**漏掉合法的传播支路**。所以"按弧（from+to 二元组）去重"是既不重复、又不漏走的正确粒度。

> 说明：本实践为源码追踪 + 手工推演，结论可直接从第 49、106 行得出。在真实设计上跑出的具体弧集合**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`append_to_collection`（第 38 行）已经带了 `-unique` 做集合内去重，为什么还需要 `visited_pairs` 这第二层去重？

**参考答案**：两者作用层次不同。`-unique` 只在**单次**构造 `valid_from_pins` 时去重——它处理"两个并行弧指向同一 from_pin"（v1.01 修的 bug）。但它管不了"跨主循环多轮、跨不同 to_pin"的重复：同一条弧 `(from,to)` 可能在不同分支的回溯里被反复考虑。`visited_pairs` 是**全局、跨轮次**的"已走过的弧"记录，覆盖了 `-unique` 管不到的范围，是终止保证的真正来源。

**练习 2**：脚本用 `get_object_name $from_pin` 把对象转成字符串再拼成数组键，为什么不直接把对象本身当键？

**参考答案**：Tcl 数组的键必须是字符串。collection 对象不是普通字符串，直接当键会得到不可靠的句柄表示。`get_object_name` 返回的是对象的**层次化全名字符串**（如 `top/u_mux/I0`），稳定且可比较，适合做键。两个不同对象只要名字相同就是同一对象，正好满足"按弧去重"的语义。

**练习 3**：第 49 行用 `info exists visited_pairs(...)` 判存在，第 24 行用 `array unset visited_pairs` 清空。为什么不在循环开始前预先把所有弧塞进数组？

**参考答案**：那样需要先枚举全设计的所有时序弧（一次 `get_timing_arcs` 全量），既慢又违背"按需回溯"的初衷。`visited_pairs` 采用**惰性标记**——只在真要走某条弧时才记它，起点只有用户给的那几个 pin。这样回溯只触及与起点相关的子图，规模远小于全图。

## 5. 综合实践

把本讲四个模块串起来，做一次"读脚本 → 用脚本 → 解释结果"的小任务。

**场景**：你接手一个 PrimeTime 会话，发现某条关键路径在 `report_timing` 里"不见了"，怀疑是被某个 `set_case_analysis` 钉成了常量而屏蔽。你要用 `report_case_propagation` 找出根因。

**操作步骤**：

1. **加载脚本**：在已 `link_design` 且 `update_timing` 的 `pt_shell` 里，`source PrimeTime/UsefulScripts/report_case_propagation.tcl`。此时 `report_case_propagation` 成为可用命令（依靠 4.3 讲的 `define_proc_attributes`）。
2. **定位可疑 pin**：用 `get_attribute [get_pins top/u_mux/Z] case_value` 确认它确实有 case 值（非空）。
3. **运行追踪**：`report_case_propagation [get_pins top/u_mux/Z]`，观察打印出的缩进树。
4. **读树找根因**：沿缩进最深、标记为 `user-defined case=...` 的叶子走，那个 pin 就是某条 `set_case_analysis` 的落点；用 `all_connected`/网表反查它属于哪个配置/模式信号。
5. **解释终止**：对照 4.4.4，说明这棵树为什么一定能走完（`visited_pairs` 按弧去重，弧总数有限），以及为什么叶子必然是 `user-defined`（4.1.3 的 `user_case_value` 判空使源头不再向上回溯）。

**预期结果 / 待本地验证**：

- 输出一棵缩进树，叶子行带 `user-defined case=...`，中间行带 `case=...`，分支处带 `branch X of Y follows`。
- 你能指认出"哪一条 `set_case_analysis` 造成了路径被屏蔽"，并据此决定是修改约束还是调整设计。
- 本仓库未附 PrimeTime 可运行的设计数据，故实际输出**待本地验证**；本任务在"读懂脚本、知道怎么调、会解释输出"的层面已可完成。

## 6. 本讲小结

- **case analysis** 用 `set_case_analysis` 把 pin/port 钉成常量，会屏蔽该常量下不可导通的时序弧；`user_case_value` 是用户设的"源头"，`case_value` 是实际生效的"结果"（含传播值）。
- 时序弧（`from_pin → to_pin`）是 STA 有向图的边；`get_timing_arcs -to $pin` 反向查询指向某 pin 的弧，是回溯的入口。
- `report_case_propagation` 从带 case 值的 pin 出发，沿弧反向回溯找根因；**回溯在 `user_case_value` 非空的 pin 处终止**，因为那里 `valid_from_pins` 保持为空、不再压栈。
- 遍历用扁平列表 `queued_contexts` 手动管理：`lindex 0` 弹出 + `linsert ... 0` 压入 = **栈 = 深度优先（DFS）**（注意：变量名带"queue"但实际是 DFS，以源码注释为准）；多分支时把 to_pin 压回头部，走完一支再回来走下一支。
- `visited_pairs([list from to])` 以**弧**为单位惰性去重，弧总数有限 ⇒ 单调增长 ⇒ **回溯必然终止**；这正是脚本不死循环的数学保证（压栈次数 \(\leq N\)）。
- `parse_proc_arguments` + `define_proc_attributes` 给自定义 `proc` 加上参数校验与 `-help`；collection API（`get_*`/`filter_collection`/`foreach_in_collection`/`sizeof_collection`/`index_collection`/`remove_from_collection`/`append_to_collection`）是 Synopsys Tcl 的核心操作范式。
- 阅读源码时要带审视：`visited_from_pins` 是声明但从未使用的"死变量"，不应想当然地认为它有效。

## 7. 下一步学习建议

- **横向练习 collection API**：回到 `mentor_scripts/createpathgroup.tcl` 与 `IC Compiler II/Vpad.tcl`（u8-l2 会讲），对比它们对 `foreach_in_collection`/`get_object_name`/集合构造的用法，巩固本讲的 collection 范式。
- **深入 STA 调试**：本脚本是 PrimeTime `UsefulScripts` 的典型代表。建议在真实项目里收集同类调试命令（如 `report_case_analysis`、`all_connected`、`report_disabled_timing`），理解它们与 `report_case_propagation` 如何配合定位"路径消失"问题。
- **进入 U7 低功耗**：case analysis 在多电压/多电源域（UPF）设计里大量用于区分"开/关电源域"模式。学完本讲后进入 u7-l1（UPF 电源意图），你会看到 `set_case_analysis` 与电源域 isolation/level-shifter 的配合。
- **算法对照**：若你对遍历算法感兴趣，可把本讲的"列表当栈用实现 DFS"与"列表当队列用实现 BFS"做对比实现，体会数据结构选择如何直接影响输出形态——这正是 u8（自动化与脚本进阶）会反复用到的 Tcl 编程基本功。
