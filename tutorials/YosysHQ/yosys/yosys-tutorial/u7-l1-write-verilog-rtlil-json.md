# write_verilog / write_rtlil / write_json

## 1. 本讲目标

综合流水线的终点是「把内存里的 `RTLIL::Design` 写到磁盘」。本讲围绕三个最常用的文本后端，带你回答这些问题：

- 为什么有三种后端？它们各自写给谁看？
- 「后端」在 C++ 里到底是什么？为什么所有 `write_*` 命令长得这么像？
- 同一个 `$and`、同一个 `$dff`，在三份输出里分别长什么样？

学完本讲，你应当能够：

1. 说清 `Backend` 基类、`execute(ostream)` 桥接方法与 `extra_args` 文件打开逻辑三者的分工；
2. 在 `write_verilog` / `write_rtlil` / `write_json` 之间做出正确选择，并解释各自输出的结构；
3. 读懂三份输出里同一个 `$and` / `$dff` 的不同表示，理解 `write_json` 为什么被 nextpnr 等下游工具使用。

## 2. 前置知识

本讲假设你已经掌握（来自 u2、u3、u4）：

- **RTLIL 内存模型**：`Design → Module → Wire / Cell`，`Cell` 用 `connections_`（端口名→SigSpec）和 `parameters` 描述实例（见 u2-l3、u3-l1）。
- **内部单元库**：`$and`、`$dff`、`$adff`、`$mux` 等以 `$` 开头的参数化高层单元，以及 `$_AND_`、`$_DFF_P_` 等单位宽门级原语（见 u3-l4）。
- **Pass / Frontend / Backend 注册机制**：所有命令都继承自 `Pass`，`Frontend`/`Backend` 是它的子类；一条命令同时登记进 `pass_register` 与 `frontend_register`/`backend_register`，命令名由 `write_` 前缀拼接而来（见 u4-l1）。

两个对本讲特别关键的概念：

- **选择（selection）**：后端可以只写出设计里被选中的部分。多数后端都有 `-selected` 选项，调用 `design->selected(module)` / `module->selected(cell)` 逐个判断（见 u4-l3）。
- **流（stream）抽象**：后端的真正业务方法签名是 `execute(std::ostream *&f, …)`，它拿到的是一个「输出流指针」而非文件名。文件名到流的转换由基类统一完成——这正是三个后端代码如此相似的原因。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [kernel/register.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.h) | 声明 `Backend` 基类，定义「两个 `execute`」的桥接关系。 |
| [kernel/register.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc) | 实现 `Backend::execute`（桥接）、`Backend::extra_args`（打开文件）、构造函数（拼 `write_` 前缀）。 |
| [backends/rtlil/rtlil_backend.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/rtlil/rtlil_backend.cc) | `write_rtlil`：把 RTLIL 拍扁成它自己的人类可读文本。 |
| [backends/verilog/verilog_backend.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/verilog/verilog_backend.cc) | `write_verilog`：把网表反向「美化」成可读 Verilog（`assign` 表达式 + `always` 块）。 |
| [backends/json/json.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/json/json.cc) | `write_json`：把网表序列化为结构化 JSON，供 nextpnr 等下游工具消费。 |

## 4. 核心概念与源码讲解

### 4.1 Backend 基类：所有 write_* 命令的共同骨架

#### 4.1.1 概念说明

后端（Backend）是 Yosys 命令体系里「输出」一侧的统一抽象。它继承自 `Pass`，因此仍是一条普通命令（能进 `pass_register`、能用 `help` 查看）；但它额外约定了一条面向输出流的业务接口。

关键在于 `Backend` 定义了**两个** `execute`：

- `execute(args, design)`：来自 `Pass` 的旧接口，标记为 `final`（子类不能改）。它负责「开文件 → 调真正的 execute → 关文件」。
- `execute(ostream *&f, filename, args, design)`：纯虚，**这才是后端真正要实现的方法**。它只关心「把 design 写到流 `f`」，不操心文件从哪来。

这种「模板方法」设计把所有后端共同的部分（解析选项、打开文件、处理 `-` 表示标准输出、处理 `.gz`、收尾统计）收进基类，让三个后端各自只需专注序列化逻辑。

#### 4.1.2 核心流程

一条 `write_xxx file.v` 命令的执行链：

```text
Pass::call(design, "write_xxx file.v")
   │  词法切词、查 pass_register
   ▼
Backend::execute(args, design)            ← final 桥接方法（基类）
   │  1. pre_execute()（计时、压栈）
   │  2. f = NULL
   │  3. 子类 execute(f, "", args, design)
   │        └─ 子类先解析自己的 -选项
   │           再调 extra_args(f, filename, args, argidx)
   │              └─ 基类打开文件，给 f 赋值（stdout / ofstream / gzip）
   │           然后真正写 design 到 *f
   │  4. post_execute(state)
   │  5. 若 f != &std::cout 则 delete f
   ▼
（栈深度恢复、选择恢复）
```

注意「名字」这一步：构造函数把 `Backend("rtlil")` 的内部名 `rtlil` 自动拼成命令名 `write_rtlil`，同时登记进 `pass_register`（当命令）与 `backend_register`（当后端种类）——这是 u4-l1 所述「一名两表」在 Backend 上的体现。

#### 4.1.3 源码精读

`Backend` 基类的声明，两个 `execute` 并列，第二个是纯虚（[kernel/register.h:168-182](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.h#L168-L182)）。这是理解三个后端为何同构的总开关。

构造函数拼 `write_` 前缀、拆出 `backend_name`（[kernel/register.cc:571-575](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L571-L575)）：

```cpp
Backend::Backend(std::string name, ...) :
        Pass(name.rfind("=", 0) == 0 ? name.substr(1) : "write_" + name, ...),
        backend_name(...)  { }
```

桥接方法 `execute(args, design)`，`final`、`[[noreturn]]`-free，负责开/关流（[kernel/register.cc:592-600](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L592-L600)）：

```cpp
void Backend::execute(std::vector<std::string> args, RTLIL::Design *design) {
    std::ostream *f = NULL;
    auto state = pre_execute();
    execute(f, std::string(), args, design);   // 调子类
    post_execute(state);
    if (f != &std::cout) delete f;
}
```

`extra_args` 把「文件名 → 流」集中处理：`-` 表示标准输出，`.gz` 走 gzip 流，否则普通 `ofstream`；无文件名时默认也落到标准输出（[kernel/register.cc:615-655](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L615-L655)）。核心片段：

```cpp
if (arg == "-") { filename = "<stdout>"; f = &std::cout; continue; }
...
filename = arg; rewrite_filename(filename);
if (... ".gz") { /* gzip_ostream */ }
else {
    std::ofstream *ff = new std::ofstream;
    ff->open(filename.c_str(), ...);
    f = ff;
}
...
if (f == NULL) { filename = "<stdout>"; f = &std::cout; }  // 没给文件名→stdout
```

> 这一小节是后三节的「公共地基」。后面三个后端，差别只在子类那个 `execute(ostream)` 里写了什么。

#### 4.1.4 代码实践

**目标**：验证「命令名 = `write_` + 后端名」的拼接，并观察桥接方法的文件处理。

**步骤**：

1. 在源码里确认：`Backend("rtlil")`、`Backend("verilog")`、`Backend("json")` 三处构造，对应命令分别是 `write_rtlil`、`write_verilog`、`write_json`。
2. 在交互 shell 里执行 `help write_json`，对照 [backends/json/json.cc:340](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/json/json.cc#L340) 的 `JsonBackend() : Backend("json", ...)`，确认 help 文本来自该后端的 `help()`。
3. 执行 `write_verilog`（不带文件名），观察输出到终端；再执行 `write_verilog -` 显式走标准输出，确认二者一致（`extra_args` 的 `-` 与「无文件名默认」两条路径）。

**预期现象**：两次都把网表打到屏幕；日志里出现 `Output filename: <stdout>` 之类信息（具体措辞待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Backend::execute(args, design)` 要标记 `final`？
**答**：保证「开文件→写流→关文件」的骨架不可被子类改写，子类只能改写面向流的 `execute(ostream)`，避免每个后端各自实现一遍文件/标准输出/gzip 处理。

**练习 2**：`Backend("=foo", ...)` 会注册出什么命令名？
**答**：见构造函数的 `name.rfind("=", 0) == 0` 分支——以 `=` 开头时不加 `write_` 前缀，直接用去掉 `=` 后的名字。这是给那些命名不符合 `write_xxx` 约定的后端留的逃生口。

---

### 4.2 write_rtlil：RTLIL 自己的文本

#### 4.2.1 概念说明

`write_rtlil` 是「最老实」的后端：它不翻译、不美化，只是把内存里的 RTLIL 结构**按原样**拍扁成它自己定义的文本格式。这与 u2-l1 介绍的 `read_rtlil` 严格互逆，是 Yosys 内部表示的「标准件」。

因为不丢失任何信息，`write_rtlil` 适合两个场景：① 调试时看综合某一步之后设计的**精确**样貌；② 把设计存档，后续用 `read_rtlil` 完整还原（前后端不经过任何语义翻译）。

#### 4.2.2 核心流程

整个后端是「一棵 dump 函数树」，根为 `dump_design`，递归向下：

```text
dump_design(f, design)
  ├── 写 "autoidx N"
  └── for each module: dump_module(f, "", module, ...)
        ├── dump_attributes (模块属性)
        ├── 写 "module \name"
        ├── 写 parameter 默认值
        ├── for each wire:   dump_wire   → "wire width N input 1 \name"
        ├── for each memory: dump_memory → "memory width N size M \name"
        ├── for each cell:   dump_cell   → "cell $and $0" + "connect \\A ..." + "end"
        ├── for each process:dump_proc   → (always 的中间形态)
        └── for each connection: dump_conn → "connect lhs rhs"
```

每个 dump 函数都用同一个 `stringf`（带缓冲的 `printf`）往流里写，缩进靠 `indent` 字符串层层传递。

#### 4.2.3 源码精读

`RTLILBackend` 类与命令注册，`execute` 解析 `-selected`/`-sort` 后调用 `dump_design`（[backends/rtlil/rtlil_backend.cc:409-457](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/rtlil/rtlil_backend.cc#L409-L457)）：

```cpp
struct RTLILBackend : public Backend {
    RTLILBackend() : Backend("rtlil", "write design to RTLIL file") { }
    void execute(std::ostream *&f, std::string filename,
                 std::vector<std::string> args, RTLIL::Design *design) override {
        ...
        extra_args(f, filename, args, argidx);          // 复用基类开文件
        *f << stringf("# Generated by %s\n", yosys_maybe_version());
        RTLIL_BACKEND::dump_design(*f, design, selected, true, false);
    }
} RTLILBackend;
```

`dump_cell` 是看「RTLIL 怎么表示一个单元」的最佳窗口：先属性，再 `cell <type> <name>`，再每个 `parameter`、每个 `connect <端口> <信号>`，最后 `end`（[backends/rtlil/rtlil_backend.cc:173-192](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/rtlil/rtlil_backend.cc#L173-L192)）：

```cpp
f << stringf("%s" "cell %s %s\n", indent, cell->type, cell->name);
for (... param ...) { f << "parameter ... "; dump_const(...); }
for (... [port, sig] ...) { f << "connect "; dump_sigspec(port/sig); }
f << stringf("%s" "end\n", indent);
```

`dump_const` 决定常数怎么写：32 位且可表示为非负整数时直接写十进制（`autoint`），否则写 `宽度'位串`，位状态含 `0/1/x/z/-/m`（[backends/rtlil/rtlil_backend.cc:44-104](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/rtlil/rtlil_backend.cc#L44-L104)）。例如一个 4 位常数 `1011` 可能写成 `11`（autoint）或 `4'1011`。

于是同一个 `$and` 在 RTLIL 文本里长这样：

```text
  cell $and $and$demo.v:3$1
    connect \A { \a [3] \a [2] \a [1] \a [0] }
    connect \B { \b [3] \b [2] \b [1] \b [0] }
    connect \Y \y [3:0]
  end
```

#### 4.2.4 代码实践

**目标**：用 `write_rtlil` 看一个含 `$and` 与 `$dff` 的设计的精确内部表示。

**步骤**：

1. 准备 `demo.v`：

   ```verilog
   module demo(input clk, input [3:0] a, input [3:0] b, output reg [3:0] q);
     wire [3:0] y;
     assign y = a & b;
     always @(posedge clk) q <= y;
   endmodule
   ```

2. 在 yosys shell 里：

   ```text
   read_verilog demo.v
   proc
   opt
   write_rtlil demo.rtlil
   ```

3. 打开 `demo.rtlil`，找到 `cell $and …` 与 `cell $dff …` 两段，逐行对照 4.2.3 的字段。

**观察重点**：`$dff` 的端口是 `CLK/D/Q`，参数里有 `WIDTH` 与 `CLK_POLARITY`；这些信息在 RTLIL 文本里**原封不动**保留。

**预期结果**：`demo.rtlil` 中能看到 `cell $and`、`cell $dff`、`wire`、`connect` 等关键字；具体行号与 `autoidx` 数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么说 `write_rtlil` 不做「翻译」？
**答**：它的 dump 函数是 RTLIL 内存结构的一一映射（module→`module`、wire→`wire`、cell→`cell`+`connect`），不改变语义、不合并表达式、不推断寄存器，只是序列化。

**练习 2**：`write_rtlil -selected` 与不带选项的输出有何不同？
**答**：见 `dump_design`/`dump_module` 的 `only_selected` 分支——开启后只写出当前选择命中的模块/线/单元，并按 `design->selected(...)` 逐项过滤（[rtlil_backend.cc:320-366](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/rtlil/rtlil_backend.cc#L320-L366)）。

---

### 4.3 write_verilog：把网表「美化」回可读 Verilog

#### 4.3.1 概念说明

`write_verilog` 做的是三件事里**最复杂**的：它不是机械序列化，而是把以 `$` 单元搭成的网表**反向重建**成人类熟悉的 Verilog——

- 组合的 `$and/$or/$mux/…` 被还原成 `assign y = a & b;` 这样的表达式；
- 时序的 `$dff/$adff/…` 被还原成 `always @(posedge clk)` 块，对应的 wire 被声明为 `reg`；
- 实在没法还原的单元，才退化成结构化例化 `celltype #(.P(v)) inst (.A(a), .Y(y));`。

正因为它要重建行为级语义，源码里有一个重要警告：**它要求设计已经跑过 `proc`**，否则残留的 RTLIL Process 没法可靠地映射回 `always` 块。

#### 4.3.2 核心流程

`VerilogBackend::execute` 在真正写之前，会先做一点「预处理」——调用几条小 pass 把不利于「表达式化」的单元拆掉，再排序，最后逐模块 dump（[backends/verilog/verilog_backend.cc:2710-2732](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/verilog/verilog_backend.cc#L2710-L2732)）：

```text
execute(args, design)
  ├── 重置一堆全局开关（norename/noexpr/noattr/decimal/…）
  ├── 解析自己的 ~20 个 -选项
  ├── extra_args(...)                      开文件
  ├── Pass::call "bmuxmap"; "demuxmap";    把 $bmux/$demux 拆成可表达式化的形式
  │   Pass::call "clean_zerowidth"
  ├── design->sort_modules()
  └── for each module:
        ├── 检测 FF：把 $dff 的 Q 线记进 reg_wires   → 这些 wire 声明成 reg
        ├── dump_module：端口/线/例化/表达式赋值/always 块
```

写每个 cell 时，`dump_cell` 先尝试「表达式化」——成功就写成 `assign`，失败才结构化例化（[backends/verilog/verilog_backend.cc:1988-1994](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/verilog/verilog_backend.cc#L1988-L1994)）：

```cpp
if (cell->type[0] == '$' && !noexpr) {
    if (dump_cell_expr(f, indent, cell))   // 还原成 a & b 之类
        return;
}
// 退化路径：结构化例化 celltype #(...) name (.Port(sig));
f << stringf("%s" "%s", indent, id(cell->type, false));
```

FF 的 `reg` 化发生在模块开头：扫描所有「内置 FF 且带 Q 端」的单元，把它们的 Q 位收集进 `reg_bits`，整根线都是 reg 位的就加入 `reg_wires`，声明时用 `reg`（[backends/verilog/verilog_backend.cc:2416-2434](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/verilog/verilog_backend.cc#L2416-L2434)）；`dump_wire` 据 `reg_wires` 决定写 `wire` 还是 `reg`（[verilog_backend.cc:451](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/verilog/verilog_backend.cc#L451)）。

于是同一个 `$and` 在 Verilog 输出里变成一句：

```verilog
assign y = a & b;
```

而 `$dff` 不再作为 cell 出现，而是变成 `reg [3:0] q;` 加一个 `always @(posedge clk) q <= y;` 块。

#### 4.3.3 源码精读

`VerilogBackend` 类，命令名 `write_verilog`（[backends/verilog/verilog_backend.cc:2494-2496](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/verilog/verilog_backend.cc#L2494-L2496)）：

```cpp
struct VerilogBackend : public Backend {
    VerilogBackend() : Backend("verilog", "write design to Verilog file") { }
```

`help()` 里明确提醒：RTLIL Process 不总能映射成 `always`，应先 `proc`（[verilog_backend.cc:2585-2589](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/verilog/verilog_backend.cc#L2585-L2589)）。

`execute` 顶部的预处理与主循环（[verilog_backend.cc:2709-2732](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/verilog/verilog_backend.cc#L2709-L2732)）：

```cpp
log_push();
if (!noexpr) { Pass::call(design, "bmuxmap"); Pass::call(design, "demuxmap"); }
Pass::call(design, "clean_zerowidth");
log_pop();
design->sort_modules();
*f << stringf("/* Generated by %s */\n", yosys_maybe_version());
for (auto module : design->modules()) {
    if (module->get_blackbox_attribute() != blackboxes) continue;   // 默认不写 blackbox
    ...
    dump_module(*f, "", module);
}
```

`dump_cell` 的「先表达式、后例化」分支已见 4.3.2。`-noexpr` 选项可强制走结构化例化路径，便于某些只接受网表的下游仿真器。

#### 4.3.4 代码实践

**目标**：对比 `write_verilog` 默认输出（表达式化）与 `-noexpr` 输出（结构化），体会「重建」的含义。

**步骤**：

1. 复用 4.2.4 的 `demo.v` 与 `read_verilog/proc/opt` 流程。
2. 分别执行：

   ```text
   write_verilog demo_v.v
   write_verilog -noexpr demo_v_struct.v
   ```

3. 用文本对比工具看两份输出里 `y = a & b` 那部分。

**观察重点**：

- `demo_v.v` 里是 `assign y = a & b;`，寄存器是 `reg [3:0] q;` 配 `always @(posedge clk)`；
- `demo_v_struct.v` 里则是 `$and`、`$dff` 单元的例化（带 `#(.WIDTH(4))` 参数与 `.A(...)/.Y(...)` 端口连接）。

**预期结果**：两份文件功能等价，但形态完全不同——这正是 write_verilog「美化」与「结构化」两条路径的差别。具体标识符名（如自动改名后的 `_<n>_`）待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `write_verilog` 默认要先跑 `bmuxmap`/`demuxmap`？
**答**：`$bmux`/`$demux` 没有直接的 Verilog 一元/二元运算符可对应，`dump_cell_expr` 难以把它们还原成表达式。先拆成 `$mux`/基础门（见 [verilog_backend.cc:2710-2713](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/verilog/verilog_backend.cc#L2710-L2713)），才便于表达式化输出。

**练习 2**：`-norename` 影响什么？
**答**：默认情况下 `$` 开头的内部名会被改写成 `_<数字>_` 这种短名（避免 Verilog 转义标识符满天飞）；`-norename` 关闭改名，保留原始 `$…` 名（[verilog_backend.cc:2507-2510](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/verilog/verilog_backend.cc#L2507-L2510) 的 help 说明）。

---

### 4.4 write_json：写给下游工具的结构化网表

#### 4.4.1 概念说明

`write_json` 输出的是一份**结构化 JSON 网表**——既不像 RTLIL 文本那样面向人调试，也不像 Verilog 那样要重建行为级语义，而是给程序消费的：模块、端口、单元、连线、存储器都被编成嵌套的 JSON 对象。

它最重要的应用场景是 FPGA 流程：Yosys 综合 + nextpnr 布局布线。`write_json` 产出的就是 nextpnr 直接读取的网表格式（见 [docs/source/introduction.rst:22](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/introduction.rst#L22) 提到的「Yosys + nextpnr」组合，以及 [docs/source/getting_started/example_synth.rst:845](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/getting_started/example_synth.rst#L845) 说明综合产物可交给 nextpnr）。

#### 4.4.2 核心流程

JSON 后端把「连接」做了一个关键转换：**用整数编号代表每一根网（net）**。

设计里每根信号位（SigBit）在首次出现时被分配一个递增整数 id（常量位 `0/1/x/z` 用字符串表示，wire 位用整数）。之后所有端口连接都用「位向量」`[2, "1", 3, 3]` 描述——同一个整数出现在两处，就表示这两处连同一根网。

```text
write_design(design)
  ├── "creator" / "modules": {
  │     for each module: write_module
  │       ├── 用 SigMap 归一化信号，清空 sigids，sigidcounter=2（避开 0/1）
  │       ├── "ports":   每个端口 → direction + bits(位向量)
  │       ├── "cells":   每个 cell → type + parameters + attributes
  │       │               + port_directions + connections(端口→位向量)
  │       ├── "memories":每个 memory → width/start_offset/size
  │       └── "netnames":每根 wire → bits + 属性
  └── (可选) "models": -aig 模式下的 AIG 模型
```

`get_bits` 是这套编号机制的核心：逐位查 `sigids`，没有就分配（[backends/json/json.cc:82-102](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/json/json.cc#L82-L102)）：

```cpp
string get_bits(SigSpec sig) {
    string str = "[";
    for (auto bit : sigmap(sig)) {           // 先 SigMap 归一化
        if (sigids.count(bit) == 0) {
            if (bit.wire == nullptr)
                s = (bit==S0)?"\"0\"":(bit==S1)?"\"1\"":(bit==Sz)?"\"z\"":"\"x\"";
            else
                s = stringf("%d", sigidcounter++);   // wire 位发整数号
        }
        str += sigids[bit];
    }
    return str + " ]";
}
```

于是同一个 `$and` 在 JSON 里长这样（端口连接是位向量）：

```json
"and$0": {
  "hide_name": 1,
  "type": "$and",
  "parameters": { "WIDTH": 4 },
  "attributes": {},
  "port_directions": { "A": "input", "B": "input", "Y": "output" },
  "connections": { "A": [ 3, 4, 5, 6 ], "B": [ 7, 8, 9, 10 ], "Y": [ 11, 12, 13, 14 ] }
}
```

`$dff` 同理，端口 `CLK/D/Q` 各自映射到位向量，`port_directions` 标出输入输出。

#### 4.4.3 源码精读

`JsonBackend` 类与命令注册（[backends/json/json.cc:338-339](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/json/json.cc#L338-L339)）：

```cpp
struct JsonBackend : public Backend {
    JsonBackend() : Backend("json", "write design to a JSON file") { }
```

`execute` 解析 `-aig/-compat-int/-selected/-noscopeinfo` 后构造 `JsonWriter` 并 `write_design`（[json.cc:601-635](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/json/json.cc#L601-L635)）：

```cpp
void execute(std::ostream *&f, std::string filename,
             std::vector<std::string> args, RTLIL::Design *design) override {
    ... 解析选项 ...
    extra_args(f, filename, args, argidx);
    JsonWriter json_writer(*f, use_selection, aig_mode, compat_int_mode, scopeinfo_mode);
    json_writer.write_design(design);
}
```

`JsonWriter` 持有 `SigMap sigmap` 与 `dict<SigBit,string> sigids`，是整份输出的状态机（[json.cc:31-49](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/json/json.cc#L31-L49)）。

cell 的写法：先 `hide_name/type`，可选 AIG `model`，再 `parameters/attributes`，已知单元还写 `port_directions`，最后 `connections`（[json.cc:191-239](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/json/json.cc#L191-L239)）。`hide_name` 当名字以 `$` 开头（自动生成）时为 1，提示下游「这个名字对用户不重要」。

完整的 JSON 顶层结构（`creator/modules/…/models`）由 `write_design` 拼出（[json.cc:288-335](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/json/json.cc#L288-L335)）；`help()` 里给出了与代码一一对应的格式说明（[json.cc:362-461](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/json/json.cc#L362-L461)）。

> 另有同文件的 `JsonPass`（普通 Pass，命令名 `json`，见 [json.cc:638](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/json/json.cc#L638)）：它默认只写选中对象、可用 `-o` 指定文件，本质复用同一个 `JsonWriter`，是 `write_json` 的「选择友好版」。

#### 4.4.4 代码实践

**目标**：用 `write_json` 产出结构化网表，理解整数编号的连接表示，并说明它的下游用途。

**步骤**：

1. 复用 `demo.v`、`read_verilog/proc/opt`。
2. 执行：

   ```text
   write_json demo.json
   ```

3. 用 `python3 -m json.tool demo.json` 美化查看，定位 `"type": "$and"` 和 `"type": "$dff"` 两段。
4. 验证「同一根网用同一整数」：找 `$and` 输出 `Y` 的某位整数，再在下游 `$dff` 的 `D` 位向量里找同一整数。

**观察重点**：

- `port_directions` 标出每个端口是 input/output；
- 常量位显示为 `"0"/"1"/"x"/"z"`，wire 位显示为整数；
- 顶层的 `netnames` 段把每根 wire 的编号列全，是 nextpnr 建立「网名↔编号」映射的依据。

**预期结果**：`demo.json` 可被标准 JSON 解析器读取；`$and` 与 `$dff` 的连接均为位向量。具体编号数值待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `sigidcounter` 从 2 开始（而不是 0）？
**答**：见 [json.cc:151-152](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/json/json.cc#L151-L152) 的注释——0、1 留空，避免和字符串常量 `"0"`/`"1"` 混淆。

**练习 2**：`write_json` 遇到含 Process 的模块会怎样？
**答**：直接报错，提示先跑 `proc`（[json.cc:154-156](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/json/json.cc#L154-L156)）。JSON 只表达门级网表，不接受行为级 Process。

**练习 3**：谁会消费 `write_json` 的输出？
**答**：主要是布局布线工具 **nextpnr**（iCE40/ECP5/gowin 等流程）；JSON 是结构化、易解析、含网名与端口方向的格式，适合作为综合与布线之间的交换格式。

---

## 5. 综合实践

把三个后端串起来对比。目标：用**同一个**设计、**同一份** RTLIL，产出三份输出，亲手验证「同一个 `$and` / `$dff`」在三份文件里的三种面孔。

1. 准备 `demo.v`（4.2.4）。建议用一个稍大点的设计以让差异更明显，例如把计数器也加进来：

   ```verilog
   module top(input clk, input rst, input en, input [3:0] a, b, output reg [3:0] q, output reg [2:0] cnt);
     wire [3:0] y; assign y = a & b;
     always @(posedge clk) q <= y;
     always @(posedge clk) if (rst) cnt <= 0; else if (en) cnt <= cnt + 1;
   endmodule
   ```

2. 跑一条脚本（存为 `cmp.ys`）：

   ```text
   read_verilog demo.v
   synth -run coarse          # 得到 $and / $dff 等高层单元，但还没 techmap 成 $_门
   write_verilog  out.v
   write_rtlil    out.rtlil
   write_json     out.json
   stat                      # 顺带统计 $and/$dff 数量，作为对照基准
   ```

3. 填写对比表（关键：定位同一个 `$and` 与同一个 `$dff`）：

   | 后端 | `$and` 的样子 | `$dff` 的样子 | 形态 |
   | --- | --- | --- | --- |
   | write_rtlil | `cell $and …`+`connect` | `cell $dff …`+`connect CLK/D/Q` | 原样序列化 |
   | write_verilog | `assign y = a & b;` | `reg q; always @(posedge clk) q<=…;` | 重建行为级 |
   | write_json | `"type":"$and"`+`connections` 位向量 | `"type":"$dff"`+`connections` 位向量 | 结构化、整数编号 |

4. 用 `python3 -m json.tool out.json | head -60` 看 JSON 顶层；确认 `$and` 输出位与 `$dff` 输入位在两处用了相同整数（同一根网）。

5. 写一段话回答：如果下一步要 (a) 给同事看网表、(b) 喂给 nextpnr 布线、(c) 用 `read_rtlil` 完整还原设计，分别该选哪个后端？

   参考答案：(a) write_verilog（最可读）；(b) write_json（nextpnr 的输入格式）；(c) write_rtlil（与 read_rtlil 严格互逆，无信息损失）。

> 提示：综合后具体出现哪些 `$` 单元、`stat` 报告的单元名与数量，取决于本机 yosys 版本，相关数字请以本地输出为准（待本地验证）。

## 6. 本讲小结

- 三个后端都继承自 `Backend`，复用基类的文件打开（`extra_args`，含 `-`/`.gz`/stdout 处理）与桥接方法 `execute(args,design)`；子类只实现 `execute(ostream)`。命令名由 `Backend("x")` 自动拼成 `write_x`。
- `write_rtlil` 是「最老实」的序列化：RTLIL 内存结构一一映射为文本，无翻译、无损失，与 `read_rtlil` 互逆，适合调试与存档还原。
- `write_verilog` 是「美化重建」：组合 `$` 单元还原成 `assign` 表达式，时序单元还原成 `reg` + `always` 块；要求先 `proc`，`-noexpr` 可退化成结构化例化。
- `write_json` 是「结构化网表」：用整数编号表示每一根网，端口连接写成位向量，主要供 nextpnr 等下游布局布线工具消费。
- 同一个 `$and`/`$dff` 在三份输出里形态迥异：RTLIL 是 `cell`/`connect`、Verilog 是表达式/`always`、JSON 是带 `connections` 位向量的对象。
- 选择后端的判据：给人看用 Verilog，给 nextpnr 用 JSON，给 `read_rtlil` 还原用 RTLIL。

## 7. 下一步学习建议

- **u7-l2（cxxrtl）**：同样是「写 RTLIL 出去」，但产物是可编译的 C++ 仿真模型，可对照本讲的「文本后端」体会「代码生成后端」的差别。
- **u7-l3（smt2/aiger/btor）**：形式验证后端，关注的是如何把 `$dff`/`$and` 编码成 SMT 公式或 AIG，而非人类可读性，可顺带回顾 u10-l1 的 satgen。
- **想动手扩展**：参考 [kernel/register.h:168](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.h#L168) 的 `Backend` 基类与 u9-l1，试着自己写一个最小的 `write_*` 后端（例如「只打印每个模块的单元计数」），巩固本讲的桥接机制。
