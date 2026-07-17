# Design 与 Module：设计的顶层容器

## 1. 本讲目标

上一讲（u2-l1）我们用 `write_rtlil` 看到了 RTLIL 的**文本**长什么样，建立了「module / wire / cell / process」的直观印象。本讲我们跨过文本，进入 Yosys 的 **C++ 内存模型**，回答三个问题：

1. 一个被加载进 Yosys 的「整个设计」在内存里是用什么对象表示的？（答：`RTLIL::Design`）
2. 设计里的每一个「Verilog module」又用什么对象表示？（答：`RTLIL::Module`）
3. 除了模块本身，`Design` 上还挂着哪些**全局状态**？（答：选择栈、scratchpad、`verilog_defines` 等）

学完本讲，你应该能够：

- 画出 `Design → Module → Wire/Cell/...` 的包含关系图；
- 说清 `design->module(name)`、`top_module()`、`selection()` 这三个常用接口分别返回什么；
- 理解为什么「选择（selection）」「scratchpad」「宏定义」这些跨 pass 的状态要挂在 `Design` 而不是某个 `Module` 上。

本讲是后续 u2-l3（Wire/Cell/SigSpec）和整个 Pass 编写实践（u9）的地基——你写的每一个 Pass，第一个参数几乎都是 `RTLIL::Design *design`。

## 2. 前置知识

阅读本讲前，请确认你已了解（来自 u1/u2-l1）：

- **RTLIL**：Yosys 所有前端产出、所有后端消费的统一中间表示。
- **pass**：对 RTLIL 做一次变换的命令，入口是 `Pass::execute(args, design)`，拿到的是指向当前 `Design` 的指针。
- **IdString**：RTLIL 的标识符类型，公有名字以 `\` 开头（来自 HDL，如 `\counter`），Yosys 自动生成的内部名字以 `$` 开头（如 `$techmap\counter.$0`）。
- **`yosys_design`**：`yosys_setup()` 创建的全局 `RTLIL::Design` 实例，shell 里所有命令默认操作的就是它。

如果你还记得上一讲 `write_rtlil` 输出里一个 `module ... end` 块的样子，本讲就是把那串文本「还原」成 C++ 对象的过程。

## 3. 本讲源码地图

本讲只涉及两个文件，它们是 Yosys 整个 IR 体系的「宪法」：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `kernel/rtlil.h` | RTLIL 所有数据结构与接口的声明 | `struct RTLIL::Design`、`struct RTLIL::Module`、`struct RTLIL::Selection` |
| `kernel/rtlil.cc` | 上述结构的实现 | `Design` 的构造/析构、`module()`、`top_module()`、选择栈、scratchpad |

另外会**引用**（不精读）两处佐证全局状态如何被使用：

- `passes/cmds/scratchpad.cc`：`scratchpad` 命令，shell 里读写 scratchpad 的入口。
- `passes/cmds/stat.cc`：`stat` 命令把统计结果写进 scratchpad，是「pass 之间通过 Design 传数据」的真实例子。

## 4. 核心概念与源码讲解

本讲的三个最小模块：

- **4.1 RTLIL::Design**：整个设计的顶层容器。
- **4.2 RTLIL::Module**：一个硬件模块（对应一个 Verilog module）。
- **4.3 Design 全局状态**：选择栈、scratchpad、`verilog_defines` 等跨 pass 状态。

---

### 4.1 RTLIL::Design

#### 4.1.1 概念说明

`RTLIL::Design` 是 Yosys 内存模型的**根**。你可以把它理解成「当前这一份工程」：

- 它**拥有**若干个 `RTLIL::Module`（用名字索引）。
- 它**携带**一组跨 pass 的全局状态（选择栈、scratchpad、Verilog 宏……），下一节细讲。
- 进程里**可以同时存在多个 Design**（通过 `get_all_designs()` 全局表管理），但 shell 默认只操作 `yosys_setup()` 创建的那一个。

为什么需要「根对象」？因为综合过程中，前端、pass、后端都要拿到「同一份正在被处理的设计」并对它读写。把所有状态收拢进一个 `Design *`，使得每个 pass 的签名都能统一成 `execute(args, design)`——这是 Yosys 命令体系能高度一致的关键。

#### 4.1.2 核心流程

一个 `Design` 的生命周期大致是：

```text
yosys_setup()
   └─ new RTLIL::Design            # 构造：初始化 scratchpad/verilog_defines，压入一个「全选」选择
        └─ 注册进 get_all_designs() 全局表

read_verilog / read_rtlil ...       # 前端：design->add(module) 把模块塞进 modules_
synth / opt / proc ...              # pass：遍历 design->modules()，增删改模块
write_verilog / write_rtlil ...     # 后端：遍历 design->modules()，序列化输出

yosys_shutdown()
   └─ delete Design                # 析构：逐个 delete 它拥有的 module 与 binding
```

要点：

1. **Design 拥有 Module 的所有权**。`add(module)` 之后，这些 module 由 design 统一在析构时释放，pass 里通常只拿指针、不 `delete`。
2. **模块用名字做主键**存进 `dict<IdString, Module*>`，因此 `design->module(name)` 是 O(1) 查找。
3. **同时存在多个 Design** 的能力，被 `techmap`、`abc9` 这类需要「临时加载一个模板/库设计」的 pass 使用。

#### 4.1.3 源码精读

先看声明。`Design` 的核心字段与查询接口集中在这一段：

[kernel/rtlil.h:1892-1920](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1892-L1920) —— `struct RTLIL::Design` 的字段与模块查询接口。要点逐条对应：

- `modules_`（L1904）：`dict<RTLIL::IdString, RTLIL::Module*>`，**所有模块按名字存放**的地方，是 Design 的「主体内容」。
- `module(name)`（L1918-1919）：按名字取模块，找不到返回 `NULL`。
- `top_module()`（L1920）：返回「顶层模块」，下一小节专门讲它的判定规则。
- `has(id)`（L1922-1924）：判断某模块是否存在，等价于 `modules_.count(id) != 0`。
- `addModule(name)`（L1929）：new 一个新模块并加进 design，返回指针，是 pass 里「凭空造模块」的标准入口。

构造函数揭示了 Design 的初始状态：

[kernel/rtlil.cc:1171-1182](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L1171-L1182) —— `Design::Design()`。三件事：

1. L1172：用 `new define_map_t` 初始化 `verilog_defines`（Verilog 宏表）。
2. L1179：`push_full_selection()`——一开始就压入一个「全选」选择，保证后续 pass 默认作用于整个设计。
3. L1181：把自己登记进 `get_all_designs()` 全局表。

析构函数体现「所有权」：

[kernel/rtlil.cc:1184-1191](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L1184-L1191) —— `Design::~Design()`，遍历 `modules_` 与 `bindings_` 逐个 `delete`，并从全局表摘除自己。

`module(name)` 的实现极其简单，就是一次字典查找：

[kernel/rtlil.cc:1204-1212](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L1204-L1212) —— 存在则返回指针，否则返回 `NULL`。

`top_module()` 的判定规则值得细看，它**不是**随便返回第一个模块：

[kernel/rtlil.cc:1214-1227](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L1214-L1227) —— `top_module()` 逻辑：

1. 遍历**当前被选中**的模块（`selected_modules()`，注意不是全部模块）。
2. 若某模块带 `top` 属性（L1220 `get_bool_attribute(ID::top)`），立即返回它——这是 `hierarchy -top` 打的标记。
3. 否则计数：若**恰好只有一个**被选中的模块（L1226 `module_count == 1`），就返回它。
4. 否则返回 `nullptr`（无法确定顶层）。

> 含义：`top_module()` 依赖两个条件之一——要么显式标了 `top` 属性，要么当前选区里只剩一个模块。这正是为什么综合脚本里通常要先跑 `hierarchy -top <name>`。

最后看 `add(Module*)`，它建立「Design ↔ Module」的双向引用：

[kernel/rtlil.cc:1229-1243](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L1229-L1243) —— `Design::add(Module*)`：L1233 把模块放进 `modules_`，L1234 关键的一句 `module->design = this` 设置**反向指针**，于是任何 `Module` 都能通过 `design` 字段回到它所属的 Design。

#### 4.1.4 代码实践（源码阅读型）

**目标**：亲手在头文件里定位 `Design` 的接口，并用自己的话描述三个返回值。

**操作步骤**：

1. 打开 `kernel/rtlil.h`，跳到 `struct RTLIL::Design`（约 L1892）。
2. 找到 `RTLIL::Module *module(RTLIL::IdString name);`、`RTLIL::Module *top_module() const;`。
3. 再跳到 `RTLIL::Selection &selection();`（约 L1981）。
4. 对照 `kernel/rtlil.cc` 中它们的实现（L1204、L1214、L1982）。

**需要观察的现象 / 预期结果**：写出一段类似下面的说明（这正是本讲作业）：

- `design->module(name)`：返回名为 `name` 的模块指针，不存在返回 `NULL`；
- `top_module()`：返回带 `top` 属性、或选区中唯一的那个模块，无法判定时返回 `nullptr`；
- `selection()`：返回 `selection_stack.back()`，即当前栈顶的 `RTLIL::Selection` 引用——它描述「下一条 pass 该作用于哪些模块/对象」。

> 待本地验证：如果你已经按 u1-l4 编译出 `./build/yosys`，可以在 shell 里 `read_verilog examples/cmos/counter.v` 后执行 `hierarchy -top counter`，再 `stat`，观察输出里 `top` 是否被识别——这间接验证了 `top_module()` 的判定。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Design` 析构时要遍历 `modules_` 逐个 `delete`，而前端/pass 代码里却很少出现 `delete module`？

**参考答案**：因为 `Design` **拥有**它内部所有 `Module` 的所有权（`add` 时建立归属关系）。所有权集中在一处，可以避免双重释放和泄漏；其他代码只持有裸指针做读写，不负责释放。

**练习 2**：一个进程里能否同时存在多个 `RTLIL::Design`？有什么用？

**参考答案**：能。`get_all_designs()` 维护了一张 `hashidx_ → Design*` 的全局表（见 [rtlil.cc:1193-1197](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L1193-L1197)）。`techmap`、`abc9` 等 pass 会临时新建一个 Design 来加载映射模板/单元库，处理完再用，互不污染主设计。

---

### 4.2 RTLIL::Module

#### 4.2.1 概念说明

`RTLIL::Module` 对应一个 **Verilog module**（或一个内部生成的派生模块）。它是「一个硬件块」的容器，里面装着：

- **wires_**：线网（`RTLIL::Wire`），下一讲主角。
- **cells_**：实例化单元（`RTLIL::Cell`），下一讲主角。
- **memories / processes**：存储器与行为级进程（`always` 块），u6 详讲。
- **connections_**：`assign` 语句产生的「连线」对。
- **avail_parameters / parameter_default_values**：参数表。
- **ports**：端口顺序列表。

它的继承链很简洁，但很关键：

```text
RTLIL::AttrObject   // 持有 attributes 字典（属性系统）
   └─ RTLIL::NamedObject   // 增加 name 字段（IdString）
        └─ RTLIL::Module   // 增加 design 反向指针 + wires/cells/...
```

也就是说，**每个 Module 都「有名字」且「有属性」**。属性（attributes）是 RTLIL 里挂元数据的通用机制——例如 `blackbox`、`whitebox`、`top`、`src`（源码位置）都是属性。`top_module()` 之所以能工作，正是因为 `hierarchy -top` 给某模块打上了 `top` 这个布尔属性。

> 术语解释：**blackbox/whitebox** 是「只有端口、没有内部实现的模块」的黑盒/白盒标记；工艺库单元（如 `NAND2`）就是 blackbox——它告诉 Yosys「这个单元的逻辑功能与面积由外部库提供，别展开它」。

#### 4.2.2 核心流程

一个 `Module` 的典型使用流程：

```text
design->addModule("counter")     // 1. 创建并归属到 design，拿到 module*
   ├─ module->addWire(...)        // 2. 加线网
   ├─ module->addCell(...)        // 3. 加单元
   ├─ module->connect(lhs, rhs)   // 4. 连线（等价于 assign lhs = rhs）
   └─ module->fixup_ports()       // 5. 根据连线整理端口表

# 查询时：
design->module("\\counter")->cells()   // 遍历该模块所有 cell
module->wire("\\clk")                  // 按名取 wire，没有返回 nullptr
module->selected_cells()               # 只取「被当前选择命中」的 cell
```

两个设计要点：

1. **反向指针**：每个 Module 都有 `design` 字段指回所属 Design（见 4.1.3 的 `add`）。这样在 Module 内部也能 `design->selected_member(...)` 查全局选区，无需把 design 当参数到处传。
2. **查询走字典、遍历走 ObjRange**：`wire(id)`/`cell(id)` 是 O(1) 字典查找；`wires()`/`cells()` 返回一个带引用计数的 `ObjRange`，遍历期间若发生增删会触发断言保护。

#### 4.2.3 源码精读

先看继承基类，理解「名字 + 属性」从哪来：

[kernel/rtlil.h:1261-1299](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1261-L1299) —— `AttrObject`（L1263 `attributes` 字典，及一整套 `set_bool_attribute`/`get_string_attribute` 等）与 `NamedObject`（L1298 增加 `IdString name`）。`Module` 继承自 `NamedObject`，因此天然「有名、有属性」。

`Module` 自身的字段集中在这段：

[kernel/rtlil.h:2060-2086](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2060-L2086) —— `struct RTLIL::Module` 的核心字段。逐条对应：

- `design`（L2071）：指回所属 Design 的反向指针。
- `wires_` / `cells_`（L2077-2078）：两个字典，按 `IdString` 存放线网与单元。
- `connections_`（L2080）：`vector<SigSig>`，即一连串 `(lhs, rhs)` 信号对，承载 `assign`。
- `memories` / `processes`（L2085-2086）：存储器与进程（行为级）。
- `avail_parameters` / `parameter_default_values`（L2083-2084）：参数列表与默认值，参数化模块派生（derive）时用。

按名查询的实现非常直白：

[kernel/rtlil.h:2144-2160](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2144-L2160) —— `wire(id)` 与 `cell(id)`：`find` 命中返回指针，否则返回 `nullptr`。

遍历接口返回带引用计数的范围：

[kernel/rtlil.h:2162-2167](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2162-L2167) —— `wires()` / `cells()` 返回 `ObjRange`，`wires_size()` / `cells_size()` 给出数量。`ObjRange` 构造时递增 `refcount_wires_`/`refcount_cells_`，析构时递减；若在持有期间改动底层字典，`log_assert` 会报错——这就是 pass 里「边遍历边删除」常常要先收集到 `vector` 再处理的原因。

#### 4.2.4 代码实践（源码阅读型 + 运行验证）

**目标**：把「文本里的 module」与「C++ 里的 Module」对应起来。

**操作步骤**：

1. 在 `kernel/rtlil.h` 的 `struct RTLIL::Module`（L2060 起）里，圈出 `wires_`、`cells_`、`connections_`、`memories`、`processes` 五个字段。
2. 运行已编译的 yosys（按 u1-l4）：

   ```bash
   ./build/yosys -p "read_verilog examples/cmos/counter.v; write_rtlil" > /tmp/counter.rtlil
   ```

3. 在 `/tmp/counter.rtlil` 里找到一个 `module \counter ... end` 块，数一数里面有多少行 `wire`、多少行 `cell`、多少行 `connect`。
4. 对照上面的字段，把这些文本行分别归到 `wires_` / `cells_` / `connections_`。

**预期结果**：你会清楚地看到，`write_rtlil` 输出的每个 `wire ...` 行 = `wires_` 里一个条目，每个 `cell ...` 行 = `cells_` 里一个条目，每个 `connect ...` 行 = `connections_` 里一个 `SigSig`。文本与内存结构一一对应。

> 待本地验证：`counter.v` 是否在综合前含 `always` 块？如果是，`write_rtlil` 里可能还会出现 `process` 关键字——对应 `processes` 字段（u6-l2 详讲）。

#### 4.2.5 小练习与答案

**练习 1**：`Module` 里 `wires_`/`cells_` 是 `dict`，而 `connections_` 是 `vector`。为什么连线不用字典？

**参考答案**：`assign lhs = rhs` 是**有序、可重复**的语句序列，且同一根线可能被多次驱动（虽然在综合后期会被规范化）。字典要求键唯一，不适合表达「一连串按出现顺序生效的赋值」，所以用 `vector<SigSig>`。

**练习 2**：`top_module()` 依赖模块上的 `top` 属性。这个属性属于哪条继承链带来的能力？

**参考答案**：属于 `AttrObject`（基类）提供的 `attributes` 字典与 `get_bool_attribute()`。`Module` 经 `NamedObject` 继承 `AttrObject`，所以「打属性 / 读属性」是所有 RTLIL 命名对象的通用能力，不只是 Module。

---

### 4.3 Design 全局状态

#### 4.3.1 概念说明

`Design` 不只是一袋 Module，它还挂着好几样**跨 pass 的全局状态**。理解它们，是看懂「为什么一条命令能影响后续所有命令」的关键：

| 字段 | 类型 | 作用 |
| --- | --- | --- |
| `selection_stack` | `vector<RTLIL::Selection>` | **选择栈**：决定下一条 pass 作用于哪些模块/对象 |
| `selection_vars` | `dict<IdString, Selection>` | 命名选区（`select -set name ...` 存在这里） |
| `selected_active_module` | `string` | shell 里当前「活跃」的模块名 |
| `scratchpad` | `dict<string,string>` | **scratchpad**：pass 之间传键值（配置 + 结果） |
| `verilog_defines` | `unique_ptr<define_map_t>` | Verilog 宏表（`` `define ``、命令行 `-D`） |
| `verilog_packages` / `verilog_globals` | `vector<AstNode>` | SystemVerilog package / 全局声明 |
| `monitors` | `pool<Monitor*>` | 设计变更的观察者（GUI/调试用） |

重点讲三个：**选择栈**、**scratchpad**、**`verilog_defines`**。

**为什么这些状态挂在 Design，而不是 Module？** 因为它们是「**设计级**」的横切关注点：

- 选择是面向**整个 design** 的（可以同时选多个模块里的对象）；
- scratchpad 是 pass 之间共享的，不属于任何单个模块；
- 宏定义影响**下一次** `read_verilog`，与具体模块无关。

把它们收拢在 `Design` 上，保证「当前处理上下文」只有一处真相。

#### 4.3.2 核心流程

**选择栈** 的运作像一个上下文栈：

```text
push_full_selection()    # 压入「全选」（不含 blackbox）
push_selection(sel)      # 压入任意选区（如 select 命令构造的）
... 后续 pass 只看栈顶 selection() ...
pop_selection()          # 弹出；若弹空了自动补一个全选
```

很多 pass 在开头 `push` 一个限定选区、结束时 `pop`，从而「只在本 pass 内」缩小作用范围，不污染外层脚本。

**scratchpad** 是一张纯字符串键值表，用法很灵活：

```text
# pass A 写结果
design->scratchpad_set_int("stat.num_cells", 42)

# pass B 或脚本读结果
design->scratchpad_get_int("stat.num_cells")

# shell 里也能直接操作
scratchpad -get stat.num_cells
scratchpad -set my.key value
```

因为值统一存成 `string`，所以 `scratchpad_get_bool` 要把 `"0"/"false"` 解释为假、`"1"/"true"` 解释为真——见下面的源码。

**`verilog_defines`** 在 `read_verilog` 之前生效：命令行 `-D WIDTH=8` 或脚本里 `verilog_defaults` 写入的宏，会作为预处理器输入，影响 HDL 的解析（u5-l2 详讲）。

#### 4.3.3 源码精读

先看这些字段在 `Design` 里的声明位置：

[kernel/rtlil.h:1898-1912](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1898-L1912) —— 全局状态字段：`scratchpad`（L1898）、`verilog_defines`（L1908）、`selection_stack`（L1910）、`selection_vars`（L1911）、`selected_active_module`（L1912）。

**Selection 结构**本身长这样：

[kernel/rtlil.h:1777-1798](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1777-L1798) —— `struct RTLIL::Selection`。三个布尔标志刻画选区的「范围档位」：

- `full_selection`（L1784）：选中整个设计，但**不含** blackbox；
- `complete_selection`（L1782）：选中整个设计，**含** blackbox；
- `selects_boxes`（L1780）：是否把 blackbox 也纳入；
- `selected_modules`（L1785）+ `selected_members`（L1786）：当不是全选时，用「显式名单」记录选中的模块与对象。

`selection()` 永远返回栈顶：

[kernel/rtlil.h:1981-1988](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1981-L1988) —— `selection()` 直接 `return selection_stack.back()`。

压栈/弹栈的实现：

[kernel/rtlil.cc:1446-1462](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L1446-L1462) —— `push_full_selection()` 压入 `Selection::FullSelection(this)`；`pop_selection()` 弹出栈顶，**若栈被弹空则自动补一个全选**（L1460-1461）——这保证「任何时刻都有至少一个选区」。

scratchpad 的字符串语义：

[kernel/rtlil.cc:1312-1327](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L1312-L1327) —— `scratchpad_get_bool`：找不到 key 返回默认值；否则把字符串 `"0"/"false"` → `false`，`"1"/"true"` → `true`，其余无法识别的也返回默认值。

最后看一个「scratchpad 被真实使用」的例子——`stat` 命令把统计结果写进 scratchpad：

[passes/cmds/stat.cc:1043-1053](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/stat.cc#L1043-L1053) —— `stat` 用 `design->scratchpad_set_int("stat.num_wires", ...)` 等把数据写出。这样后续脚本或 pass 就能用 `scratchpad_get_int("stat.num_cells")` 拿到统计值——这就是「pass 之间通过 Design 传数据」的标准做法。

对应的 shell 入口是 `scratchpad` 命令：

[passes/cmds/scratchpad.cc:28](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/scratchpad.cc#L28) —— `ScratchpadPass` 注册名为 `scratchpad` 的命令，提供 `-get/-set/-unset` 等子选项，本质就是读写 `design->scratchpad`。

#### 4.3.4 代码实践（运行验证型）

**目标**：亲眼看到 selection_stack 与 scratchpad 在运行时的变化。

**操作步骤**（假设已按 u1-l4 编译出 `./build/yosys`）：

1. 进 shell：

   ```bash
   ./build/yosys
   ```

2. 依次执行：

   ```yosys
   read_verilog examples/cmos/counter.v
   hierarchy -top counter
   select -list *
   stat
   scratchpad -get stat.num_cells
   ```

**需要观察的现象**：

- `select -list *` 会列出当前选区命中的对象——因为构造时 `push_full_selection()` 了，默认就是「全选」，所以能看到 `counter` 模块及其内部对象。
- `hierarchy -top counter` 之后，`counter` 被打上 `top` 属性；此时 `top_module()` 会返回它（见 4.1.3）。
- `stat` 运行后，`scratchpad -get stat.num_cells` 能打印出一个整数——这正是 `stat` 写进 `design->scratchpad["stat.num_cells"]` 的值。

**预期结果**：你验证了三件事——(1) 选区默认是全选；(2) `hierarchy -top` 影响 `top_module()`；(3) scratchpad 真的把一个 pass 的输出传给了后续查询。

> 待本地验证：具体 `stat.num_cells` 的数值取决于 `counter.v` 与已执行的综合步骤，本讲不要求精确数值，只要「能取到值」即可。

#### 4.3.5 小练习与答案

**练习 1**：`pop_selection()` 为什么在栈弹空时要自动补一个 `push_full_selection()`？

**参考答案**：为了维护不变式「`selection()` 永远返回合法的栈顶选区」。如果允许栈为空，所有调用 `selection().full_selection` 的代码都得先判空；自动补全选让调用方可以无条件访问栈顶，简化了整个 pass 体系。

**练习 2**：scratchpad 的值为什么统一存成 `std::string`，而不是用 `int`/`bool` 联合体？

**参考答案**：统一用字符串让 scratchpad 成为一个**与类型无关**的简单键值表——任何 pass 都能存任意「可序列化成字符串」的值，取值方按需用 `scratchpad_get_int/_bool/_string` 解释。这样既灵活，又避免了为每种类型维护独立的表（见 `scratchpad_get_bool` 对 `"0"/"false"` 的解析）。

**练习 3**：`verilog_defines` 为什么挂在 `Design` 而不是某个 `Module` 上？

**参考答案**：宏定义是给**预处理器**用的，作用于「下一次 `read_verilog`」这一动作，与「已经解析出来的某个模块」无关；而且一个宏可能影响随后读入的多个文件/多个模块。把它放在设计级，符合它「跨模块、面向未来读取」的语义。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**源码阅读 + 运行验证**的小任务：

**任务**：为 `counter` 设计写一份「Design 接口速查卡」。

**步骤**：

1. **定位结构**：在 `kernel/rtlil.h` 里找到 `struct RTLIL::Design`（L1892）与 `struct RTLIL::Module`（L2060），确认它们的字段与继承关系（`Module ← NamedObject ← AttrObject`）。
2. **运行综合**：

   ```bash
   ./build/yosys -p "read_verilog examples/cmos/counter.v; hierarchy -top counter; proc; opt; stat; write_rtlil" 
   ```

3. **回答三个问题**（对照 4.1.3、4.2.3 的源码）：

   - 此时 `design->module("\\counter")` 返回什么？（答：指向 `counter` 模块的 `RTLIL::Module*`）
   - 此时 `design->top_module()` 返回什么？为什么？（答：返回 `counter`，因为 `hierarchy -top` 打了 `top` 属性；即使选区里有多个模块，带 `top` 属性者优先）
   - 此时 `design->selection()` 返回什么？它是全选吗？（答：返回栈顶 `Selection`；没有额外 `select`/`push` 的话，仍是构造时的全选）

4. **观察 scratchpad**：在命令序列里加一条 `scratchpad -get stat.num_cells`，确认 `stat` 把结果写进了 `design->scratchpad`。
5. **画一张关系图**：`Design`（含 `modules_`、`selection_stack`、`scratchpad`、`verilog_defines`）—owns→ 若干 `Module`（含 `wires_`、`cells_`、`connections_`、`memories`、`processes`，并有 `design` 反向指针）。

**验收标准**：你能不看讲义，用自己的话向别人解释 `design->module(name)` / `top_module()` / `selection()` 的返回值与判定规则。

## 6. 本讲小结

- `RTLIL::Design` 是内存模型的**根**，用 `modules_`（`dict<IdString, Module*>`）拥有所有模块，并在析构时统一释放它们。
- `module(name)` 是 O(1) 字典查找；`top_module()` 按「带 `top` 属性优先，否则选区唯一模块」的规则返回顶层，无法判定时返回 `nullptr`。
- `RTLIL::Module` 继承 `NamedObject ← AttrObject`，因此「有名 + 有属性」；内部用 `wires_`/`cells_`（字典）存线网与单元，用 `connections_`（向量）存 `assign`，并有 `design` 反向指针。
- `add(Module*)` 同时建立正向归属与反向指针，是 Design↔Module 双向联系的唯一入口。
- `Design` 还承载**设计级横切状态**：`selection_stack`（选择栈，`selection()` 取栈顶，栈弹空自动补全选）、`scratchpad`（字符串键值表，pass 间传数据）、`verilog_defines`（宏表，影响下次 `read_verilog`）。
- 选择栈与 scratchpad 都是「以 Design 为唯一真相」的全局上下文，这是 Yosys 命令体系签名统一为 `execute(args, design)` 的基础。

## 7. 下一步学习建议

本讲建立了 `Design ↔ Module` 的容器关系，但模块里的 **Wire、Cell、SigSpec** 还是一个黑盒。下一讲 **u2-l3《Wire、Cell 与 SigSpec 初识》** 会打开 `Module` 的内部，讲解：

- `RTLIL::Wire` 如何表示一根/一组线网及其位宽、端口属性；
- `RTLIL::Cell` 如何表示一个实例化单元（类型 + 端口连接 + 参数）；
- `RTLIL::SigSpec` 如何用「chunk/bit 两层结构」描述一段信号（可跨多根线与常数）。

建议阅读的后续源码：`kernel/rtlil.h` 中 `struct RTLIL::Wire`、`struct RTLIL::Cell`、`struct RTLIL::SigSpec` 的声明，以及它们在 `RTLIL::Module` 上的 `addWire/addCell/connect` 接口（本讲已埋下伏笔）。读完 u2-l3，你就可以进入 u3（RTLIL 核心数据结构深入），开始具备直接用 C++ 构造 RTLIL 的能力。
