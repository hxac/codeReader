# RTLIL 文本格式：Yosys 的通用语言

## 1. 本讲目标

本讲带你认识 Yosys 的「通用语言」——RTLIL 文本格式。学完本讲，你应该能够：

1. 说清楚 RTLIL 作为「统一中间表示」的意义：为什么所有前端都向它靠拢、所有后端都从它出发。
2. 读懂一段真实的 RTLIL 文本，识别其中的 `module` / `wire` / `cell` / `connect` / `process` / `sync` 等结构。
3. 熟练使用 `read_rtlil`、`write_rtlil`、`dump` 三条命令，把内存中的设计「拍扁」成文本、再「读回」内存。

本讲是 u2 单元（RTLIL 内部表示入门）的起点。后面 u2-l2、u2-l3 会深入 Design/Module、Wire/Cell/SigSpec 的 C++ 数据结构，而本讲先让你用「眼睛」看到 RTLIL 长什么样，建立最直观的感性认识。

## 2. 前置知识

在继续之前，请确认你已经了解以下概念（均来自 u1 单元）：

- **综合流水线**：Yosys 的数据流是 `前端（读 HDL）→ 一串 Pass 变换 → 后端（写出网表）`。参见 u1-l1、u1-l4。
- **Pass / Frontend / Backend**：一切命令都是 Pass；负责读入外部格式的是 Frontend，负责写出的是 Backend。它们都登记在全局 `pass_register` 表里。参见 u1-l2、u1-l4。
- **cmos 计数器示例**：`examples/cmos/counter.v` + `counter.ys`，演示了一条完整综合流水线。参见 u1-l4。

本讲需要补充一个术语：

- **中间表示（Intermediate Representation, IR）**：编译器/综合器内部用来表示程序或电路的数据结构。就像 C 编译器有 AST、LLVM IR 一样，Yosys 的 IR 就是 RTLIL（RTL Intermediate Language）。

打个比方：如果把 Yosys 比作一个「翻译工厂」，那么各种 HDL（Verilog、SystemVerilog）是「原料」，各种目标格式（Verilog 网表、JSON、SMT2）是「产品」，而 RTLIL 就是工厂内部的「标准件」——所有原料先被加工成标准件，所有产品都由标准件组装而成。`write_rtlil` / `read_rtlil` 就是把这套标准件「打包成文本」和「从文本拆包」的两个工具。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [backends/rtlil/rtlil_backend.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/rtlil/rtlil_backend.cc) | 实现 `write_rtlil` 与 `dump`：把内存中的 RTLIL 设计**序列化**成文本。 |
| [frontends/rtlil/rtlil_frontend.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/rtlil/rtlil_frontend.cc) | 实现 `read_rtlil`：手写的递归下降解析器，把 RTLIL 文本**反序列化**回内存。 |
| [backends/rtlil/rtlil_backend.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/rtlil/rtlil_backend.h) | 后端序列化函数（`dump_const` / `dump_wire` / `dump_cell` …）的声明，能看到 `autoint` 等默认参数。 |
| [docs/source/yosys_internals/formats/rtlil_rep.rst](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/yosys_internals/formats/rtlil_rep.rst) | 官方文档，权威描述 RTLIL 的实体关系与语法，本讲大量引用其中的实例。 |

一句话记住分工：**后端负责「写」，前端负责「读」，二者必须严格对应**——后端怎么打印出来的，前端就能怎么读回去。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：4.1 RTLIL 文本结构（整体长什么样）、4.2 读写命令（怎么用）、4.3 RTLIL 词汇表（关键字速查）。

### 4.1 RTLIL 文本结构

#### 4.1.1 概念说明

RTLIL 文本格式是 RTLIL 内存数据结构（`RTLIL::Design` → `RTLIL::Module` → `Wire`/`Cell`/`Process`/`Memory`）的**可读序列化**。

它有几个关键特点：

1. **行导向（line-oriented）**：基本每一行就是一条语句，以关键字开头，以换行结束。
2. **缩进仅用于美观**：解析器会跳过空格、制表符和 `#` 开头的注释（详见 4.1.3 的 `consume_whitespace_and_comments`），缩进多少无所谓。
3. **用 `end` 显式闭合嵌套**：`module … end`、`cell … end`、`process … end`、`switch … end` 都是这样配对的。
4. **顶层只有一个 design**：一个文件里可以有多个 `module`，顶部可能有一行 `autoidx` 记录自动命名计数器。

整体的嵌套层级如下（伪代码）：

```
autoidx <数字>                 # 可选：自动命名计数器（见 4.3）
module <模块名>
  attribute <名> <常量>          # 可选：模块属性
  parameter <名> <常量>          # 可选：模块参数
  wire  <选项> <线名>            # 线网/端口
  memory <选项> <存储器名>        # 存储器
  cell  <类型> <实例名>           # 实例化单元
    parameter <选项> <名> <常量>
    connect <端口名> <信号>
  end
  process <进程名>               # 行为级进程（always 块）
    assign <左值> <右值>
    switch <控制信号>
      case <比较值>
        …
      end
    sync <同步类型> <信号>
      update <左值> <右值>
  end
  connect <信号A> <信号B>        # 模块级连线
end
```

这就是 RTLIL 文本的「骨架」。我们后面会逐块拆解。

#### 4.1.2 核心流程

序列化（写）与反序列化（读）是一对镜像过程。

**写（dump_design 自顶向下）**：先写 `autoidx`，再遍历每个 module 调用 `dump_module`；`dump_module` 再分别调用 `dump_wire` / `dump_memory` / `dump_cell` / `dump_proc` / `dump_conn` 把模块里的对象依次写出来。

```
dump_design(design)
  └─ 打印 "autoidx N"
  └─ for each module: dump_module(module)
       ├─ "module <name>"
       ├─ for each wire:     dump_wire()
       ├─ for each memory:   dump_memory()
       ├─ for each cell:     dump_cell()
       ├─ for each process:  dump_proc()
       └─ for each conn:     dump_conn()
       └─ "end"
```

**读（parse 自顶向下）**：解析器逐行读关键字，遇到 `module` 就进入 `parse_module`；`parse_module` 内部再用一个循环依次识别 `wire` / `cell` / `connect` / `process` / `memory` / `attribute` / `parameter` / `end`，直到读到 `end` 闭合模块。

#### 4.1.3 源码精读

先看「写」这一侧。整个文件的最外层调度函数是 `dump_design`：

[backends/rtlil/rtlil_backend.cc:373-404](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/rtlil/rtlil_backend.cc#L373-L404) —— 这是 `dump_design`，它先打印一行 `autoidx %d`（第 392 行），然后倒序遍历 `design->modules_`，对每个模块调用 `dump_module`。

[backends/rtlil/rtlil_backend.cc:291-371](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/rtlil/rtlil_backend.cc#L291-L371) —— 这是 `dump_module`。它打印 `module %s`（第 300 行），然后依次遍历并打印 `wires_`（第 320 行起）、`memories`（第 327 行）、`cells_`（第 334 行）、`processes`（第 341 行），最后打印模块级的 `connections()`（第 349 行），并用 `end` 闭合。注意遍历都用了 `reversed(...)`，所以输出顺序是「插入顺序的逆序」——这是 RTLIL 文件里对象排列看起来「倒着」的原因。

再看「读」这一侧。最外层调度是 worker 的 `parse` 方法：

[frontends/rtlil/rtlil_frontend.cc:868-892](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/rtlil/rtlil_frontend.cc#L868-L892) —— 顶层 `parse`。它在一个循环里只识别三种顶层关键字：`attribute`（第 875 行，属性缓冲，会附给下一个对象）、`module`（第 879 行，交给 `parse_module`）、`autoidx`（第 883 行，更新全局自动命名计数器）。其它内容都会报错。

[frontends/rtlil/rtlil_frontend.cc:83-98](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/rtlil/rtlil_frontend.cc#L83-L98) —— `consume_whitespace_and_comments`。这正说明了 RTLIL 文本里**空格、制表符被忽略、`#` 之后到行尾是注释**。所以缩进纯粹是给人看的。

[frontends/rtlil/rtlil_frontend.cc:411-484](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/rtlil/rtlil_frontend.cc#L411-L484) —— `parse_module`。它先解析模块名（第 413 行），创建 `RTLIL::Module`，然后在第 439–474 行的循环里依次识别模块体关键字：`attribute`/`parameter`/`connect`/`wire`/`cell`/`memory`/`process`/`end`，直到 `end`（第 469 行）跳出。最后第 478 行调用 `current_module->fixup_ports()` 根据端口号给端口排序。

#### 4.1.4 代码实践

> **实践目标**：亲眼看到 RTLIL 文本的「骨架」结构。

操作步骤（在已构建好 `yosys` 的环境里，从仓库根目录执行）：

1. 启动 shell：

   ```bash
   ./build/yosys
   ```

2. 在 `yosys>` 提示符下依次执行：

   ```
   read_verilog examples/cmos/counter.v
   write_rtlil
   ```

   `write_rtlil` 不带文件名时会把结果直接打印到终端。

需要观察的现象：

- 输出的第一行是 `# Generated by Yosys ...`，紧接着是 `autoidx <数字>`，然后是 `module \counter` … `end`。
- 模块体里能看到若干 `wire ... \clk`、`wire ... \count` 行，以及一段缩进的 `process $proc$... ... end`（因为此时还没跑 `synth`/`proc`，`always` 块仍是 Process）。

预期结果：你能用肉眼把输出划分成「autoidx 行」「module 头」「若干 wire」「一段 process」「end」这几个区块。

> ⚠️ 具体的 `$` 开头自动名字（如 `$proc$counter.v:6$1`）会随运行环境变化，请以你本地输出为准。

#### 4.1.5 小练习与答案

**练习 1**：如果在一个 RTLIL 文件里把某个 `module` 的 `end` 删掉，解析器会怎样？

**答案**：解析器在 `parse_module` 的循环里找不到 `end`，会一直读到文件末尾（`f->good()` 为假）后退出循环；若循环中遇到无法识别的关键字则会在 [rtlil_frontend.cc:473](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/rtlil/rtlil_frontend.cc#L473) 报 `Unexpected token in module body`。结论：`end` 是必不可少的闭合标记。

**练习 2**：为什么 `write_rtlil` 输出里，模块内的对象常常看起来是「倒序」的？

**答案**：因为 `dump_module` 对 `wires_`/`cells_` 等容器用了 `reversed(...)` 遍历（见 [rtlil_backend.cc:320](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/rtlil/rtlil_backend.cc#L320) 等行）。这只影响可读性，不影响语义。

---

### 4.2 读写命令（read_rtlil / write_rtlil / dump）

#### 4.2.1 概念说明

围绕 RTLIL 文本，Yosys 提供三条命令：

| 命令 | 类型 | 作用 |
| --- | --- | --- |
| `write_rtlil [文件名]` | Backend | 把当前 design 写成 RTLIL 文本文件（或打印到终端）。 |
| `read_rtlil [选项] 文件名` | Frontend | 从 RTLIL 文本文件加载模块到当前 design。 |
| `dump [选项] [选择]` | Pass | 把**选中的部分**以 RTLIL 格式打印到控制台或文件。 |

它们的区别很实用：

- `write_rtlil` 写**整个** design 到文件（也可加 `-selected` 只写选中部分）。
- `dump` 默认只写**选中部分**，且默认输出到控制台——适合在 shell 里快速「瞄一眼」某个 cell/wire，而不必生成文件。
- `read_rtlil` 是唯一能把 RTLIL 文本「吃」回来的命令，是做 RTLIL 文本往返（round-trip）的关键。

回忆 u1-l2/u1-l4：每个 Backend/Frontend 都是一个继承自基类的全局静态对象，构造时自动登记进 `pass_register`。本讲的 `RTLILBackend`、`RTLILFrontend`、`DumpPass` 就是三个这样的对象。

#### 4.2.2 核心流程

以 `write_rtlil` 为例，它的 `execute` 干三件事：解析命令行选项（`-selected` / `-sort`）、用 `extra_args` 打开输出文件、调用 `dump_design` 把 design 序列化写出去。

```
write_rtlil 文件名
   └─ execute()
        ├─ 解析 -selected / -sort
        ├─ extra_args(...)           # 打开文件，得到 ostream *f
        ├─ 打印 "# Generated by ..."
        └─ dump_design(*f, design, selected, true, false)
```

`read_rtlil` 的 `execute` 则创建一个 `RTLILFrontendWorker`，把输入流交给它的 `parse()` 方法逐行解析，把文本「重建」成内存里的 `RTLIL::Module`。

#### 4.2.3 源码精读

`write_rtlil` 命令本体：

[backends/rtlil/rtlil_backend.cc:409-457](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/rtlil/rtlil_backend.cc#L409-L457) —— `RTLILBackend` 结构体。它在第 410 行用名字 `"rtlil"` 注册（命令名即 `write_rtlil`）；`help()`（第 411–426 行）说明了 `-selected`（只写选中部分）和 `-sort`（原地排序后再写）两个选项；`execute()`（第 427–456 行）在解析选项后，第 454 行先写一行生成注释，第 455 行调用 `dump_design`。

`dump` 命令本体（功能相似，但面向「选择」）：

[backends/rtlil/rtlil_backend.cc:459-540](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/rtlil/rtlil_backend.cc#L459-L540) —— `DumpPass`。注意它的 `dump_design(*f, design, true, flag_m, flag_n)` 第三个参数固定为 `true`（即 `only_selected`，第 532 行），所以 `dump` 总是按「选中范围」工作；它支持 `-o 文件` / `-a 文件`（追加）、`-m`（连模块头一起打）、`-n`（只打模块头）。

`read_rtlil` 命令本体：

[frontends/rtlil/rtlil_frontend.cc:895-958](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/rtlil/rtlil_frontend.cc#L895-L958) —— `RTLILFrontend` 结构体。第 896 行以名字 `"rtlil"` 注册（命令名 `read_rtlil`）。`help()`（第 897–922 行）列出了四个关键选项，务必理解：

- `-nooverwrite`：遇到同名模块时**忽略**新定义（保留旧的）。
- `-overwrite`：**强制覆盖**同名模块。
- `-lib`：只创建空的黑盒（blackbox）模块——常用于只关心端口、不关心实现的库单元。
- `-legalize`：遇到语义错误（引用未知 wire、重定义等）时，**确定性地改写**成合法输入而非报错，主要用于模糊测试生成「随机但合法」的 RTLIL。

`execute()`（第 923–957 行）解析这些选项后，第 925 行创建 `RTLILFrontendWorker`，第 956 行调用 `worker.parse(f)`。

#### 4.2.4 代码实践

> **实践目标**：完成一次 RTLIL 文本「往返」——写出、再读回，验证它能被还原。

操作步骤（shell 内）：

1. 读取设计并写出为 RTLIL 文件：

   ```
   read_verilog examples/cmos/counter.v
   write_rtlil counter.rtlil
   ```

2. 退出 yosys 后，在外部查看（或用 `dump` 在内部查看）确认文件已生成。

3. **重新启动**一个干净的 yosys 会话，只读回这个 RTLIL 文件：

   ```
   read_rtlil counter.rtlil
   stat
   ```

需要观察的现象与预期结果：

- 第 1 步后，`counter.rtlil` 文件存在，内容以 `module \counter` 开头。
- 第 3 步的 `stat` 应能正常报告模块 `counter` 及其内部对象数量，说明 RTLIL 文本被完整读回。这证明 `write_rtlil` 的输出与 `read_rtlil` 的输入是**双向兼容**的。
- 你还可以试试 `read_rtlil -lib counter.rtlil`，再 `stat`，观察模块变成黑盒（对象变少）。

> ⚠️ 不同机器上 `autoidx` 数值和 `$` 名字可能不同，只要结构能读回即算成功。

#### 4.2.5 小练习与答案

**练习 1**：`write_rtlil` 和 `dump` 都能把 RTLIL 写出来，它们最核心的差异是什么？

**答案**：`write_rtlil` 是 Backend，默认写**整个** design 到文件；`dump` 是普通 Pass，默认按**选中范围**工作并默认输出到控制台。详见 [rtlil_backend.cc:455](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/rtlil/rtlil_backend.cc#L455)（`only_selected` 来自用户 `-selected`）与 [rtlil_backend.cc:532](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/rtlil/rtlil_backend.cc#L532)（`only_selected` 固定为 `true`）。

**练习 2**：为什么做模糊测试（fuzzing）时会用到 `read_rtlil -legalize`？

**答案**：模糊测试生成的随机 RTLIL 文本常含语义错误（未知 wire、重名等），`-legalize` 会把这些错误**确定性地改写成合法输入**（如把未知 wire 哈希映射到一个已存在的 wire），从而能生成「随机但合法」的设计用于测试。参见 [rtlil_frontend.cc:917-920](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/rtlil/rtlil_frontend.cc#L917-L920)。

---

### 4.3 RTLIL 词汇表

#### 4.3.1 概念说明

这一节是一份「关键字速查表」。掌握了它，你就能像读程序一样读 RTLIL。

**标识符命名约定**（非常重要）：所有 RTLIL 标识符必须以反斜杠 `\` 或美元符 `$` 开头。

- `\name`：**公有**标识符，通常直接来自 HDL 源码（如 Verilog 里的信号 `count` → `\count`）。
- `$name`：**自动生成**的内部标识符，由 Yosys 自己造出来（如 `$proc$...`、`$add$...`）。

这样设计的好处是：自动名永远不会和用户名冲突；优化 pass 还能据此判断「这条线是用户命名的还是临时的」（例如 `opt_clean` 更倾向于保留用户命名的线）。详见 [docs/source/yosys_internals/formats/rtlil_rep.rst:57-99](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/yosys_internals/formats/rtlil_rep.rst#L57-L99)。

> 小贴士：在文本里写 `\count` 时，反斜杠是名字的一部分；在 shell 或文档里讨论时，常写作 `\count` 表示「名为 count 的公有标识符」。

**常量写法**：RTLIL 的位向量常量格式是 `宽度'位串`，位串从高位到低位书写，每位取 `0/1/x/z`（以及内部用的 `m/-`）。例如：

- `1'1` —— 1 位，值为 1。
- `3'001` —— 3 位，值为 1（即 Verilog 的 `3'd1`）。
- `4'10x1` —— 4 位，含一个 x。
- `8's00000001` —— 带 `s` 表示有符号（signed）。
- 单独一个十进制数 `42` —— 会被当作 32 位整数常量。

**状态（State）含义**：`0`=逻辑0，`1`=逻辑1，`x`=未知，`z`=高阻，`m`/`-` 是 Yosys 内部使用的状态。

#### 4.3.2 核心流程

下面这张表把最常见的 RTLIL 关键字集中起来，方便速查：

| 关键字 | 语法示例 | 含义 |
| --- | --- | --- |
| `autoidx` | `autoidx 12` | 自动命名计数器，保证 `$` 名唯一。 |
| `module` / `end` | `module \counter … end` | 定义一个模块。 |
| `attribute` | `attribute \src "counter.v:1"` | 给**下一个**对象附加属性（属性会缓冲）。 |
| `parameter` | `parameter \WIDTH 3` | 模块/单元的参数。 |
| `wire` | `wire width 3 output 4 \count` | 声明线网/端口。 |
| `memory` | `memory width 8 size 256 \mem` | 声明存储器（数组）。 |
| `cell` / `end` | `cell $mux $procmux$3 … end` | 实例化一个单元。 |
| `connect` | `connect \A \count` | 把单元端口/模块连线接到一个信号。 |
| `process` / `end` | `process $proc$… … end` | 行为级进程（来自 always 块）。 |
| `assign` | `assign $0\q[0:0] \q` | 在某个 case 内做条件赋值。 |
| `switch` / `end` | `switch \reset … end` | 决策树（类似 C 的 switch）。 |
| `case` | `case 1'1` | 一个分支，后跟比较值；无值即为 default。 |
| `sync` | `sync posedge \clk` | 同步规则（时钟边沿/电平/always/init）。 |
| `update` | `update \q $0\q[0:0]` | 在 sync 触发时更新信号。 |

**wire 行的选项**（顺序见 `dump_wire`）：`width N`（位宽，默认 1）、`input N` / `output N` / `inout N`（端口方向 + 端口号）、`offset N`、`upto`、`signed`。例如 `\count`（3 位输出端口）写成 `wire width 3 output 4 \count`。

**信号（SigSpec）写法**：单根线 `\clk`；某一位 `\count [2]`；某一段 `\count [2:0]`；常数 `3'001`；拼接多段用 `{ … }`，如 `{ \a \b [3:0] }`。

#### 4.3.3 源码精读

**常量的解析与打印**：

[frontends/rtlil/rtlil_frontend.cc:261-322](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/rtlil/rtlil_frontend.cc#L261-L322) —— `parse_const`。第 263 行先判断字符串常量；否则第 267 行读宽度，若后面不是 `'`（或为负）则当作普通整数（第 273 行）；否则第 281 行起逐字符读位串，把 `0/1/x/z/m/-` 映射成对应 State（第 288–295 行），最后在第 302 行**反转**位序（因为文本从高位写，内部从低位存）。这也解释了为什么 `3'001` 在内存里是「低位=1」。

[backends/rtlil/rtlil_backend.cc:44-104](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/rtlil/rtlil_backend.cc#L44-L104) —— `dump_const`，`parse_const` 的逆过程。注意第 49–62 行：当宽度恰好为 32 且 `autoint` 为真时，会尝试直接打印成十进制整数（如 `1337`）；否则按 `宽度'位串` 打印（第 64–66 行的 `width'` 前缀、第 67 行的有符号 `s`）。位状态映射在第 75–82 行。

**标识符解析**：

[frontends/rtlil/rtlil_frontend.cc:161-177](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/rtlil/rtlil_frontend.cc#L161-L177) —— `try_parse_id`。第 164 行强制要求首字符是 `\` 或 `$`，否则返回空——这就是「标识符必须带前缀」规则的代码体现。

**wire / cell 的解析**：

[frontends/rtlil/rtlil_frontend.cc:505-572](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/rtlil/rtlil_frontend.cc#L505-L572) —— `parse_wire`。第 531–556 行依次识别 `width`/`upto`/`signed`/`offset`/`input`/`output`/`inout` 等选项，最后第 518 行的 `try_parse_id` 读到线名时跳出循环。

[frontends/rtlil/rtlil_frontend.cc:637-701](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/rtlil/rtlil_frontend.cc#L637-L701) —— `parse_cell`。先读单元类型和实例名（第 639–640 行），然后在循环里识别 `parameter`（第 661 行）和 `connect`（第 682 行），直到 `end`（第 694 行）。一个 cell 的「身体」就是若干 parameter 加若干 connect。

**process 的解析**：

[frontends/rtlil/rtlil_frontend.cc:785-864](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/rtlil/rtlil_frontend.cc#L785-L864) —— `parse_process`。它解析根 case（第 805 行的 `parse_case_body`），然后第 807 行起循环读 `sync` 规则。第 811–818 行把 `low/high/posedge/negedge/edge/always/global/init` 映射到同步类型；第 829 行的 `update` 是触发时的赋值。

**一段权威的真实例子**（来自官方文档，强烈建议对照阅读）：

[docs/source/yosys_internals/formats/rtlil_rep.rst:244-260](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/yosys_internals/formats/rtlil_rep.rst#L244-L260) —— 这是一个带异步复位 D 触发器的 `process` 文本。你能看到 `assign`、嵌套的 `switch`/`case`/`end`、以及 `sync posedge` 和 `update` 的真实组合，是理解 Process 结构的最佳范例。

#### 4.3.4 代码实践

> **实践目标**：把 4.1.4 产生的 counter RTLIL 输出，逐行用本节的词汇表注释。

操作步骤：

1. 在 shell 里执行：

   ```
   read_verilog examples/cmos/counter.v
   write_rtlil
   ```

2. 把输出复制出来，对照词汇表为每一行写中文注释。下面是一个**示意性**的注释样本（`$` 名字以你本地输出为准，结构应一致）：

   ```
   autoidx 1                         # 自动命名计数器
   module \counter                    # 模块 \counter（公有名）
     wire input 1 \clk                # 输入端口，端口号 1，1 位
     wire input 2 \rst                # 输入端口 2
     wire input 3 \en                 # 输入端口 3
     wire width 3 output 4 \count     # 3 位输出端口，端口号 4
     process $proc$counter.v:6$1      # always 块翻译出的进程（自动名）
       assign $0\count[2:0] \count    # 默认：保持原值
       switch \rst                    # 对 rst 做决策
         case 1'1                     # rst==1：清零
           assign $0\count[2:0] 3'000
         case                         # default 分支
           switch \en                 # 嵌套：再判断 en
             case 1'1
               assign $0\count[2:0] { ... }   # count+1（此处含 $add 单元输出）
             case
           end
       end
       sync posedge \clk              # 时钟上升沿触发
         update \count[2:0] $0\count[2:0]   # 把算好的下一拍值写回 count
     end
   end
   ```

需要观察的现象：

- `wire` 行的 `input N` / `output N` 中 N 是端口序号，与 `counter.v` 端口列表 `(clk, rst, en, count)` 一一对应。
- `case 1'1` 用了常量语法 `宽度'位串`；空 `case` 是 default。
- `$0\count[2:0]` 是 Yosys 为「count 的下一拍值」自动生成的内部线，`$` 表示自动名、`\count` 部分源自用户名。

预期结果：你能为输出中每一行都给出合理的中文解释，并指出哪些名字是 `\` 公有名、哪些是 `$` 自动名。

> ⚠️ 这是基于源码格式重建的示意，不是真实运行的逐字节拷贝；其中 `count + 1` 会生成一个 `$add` 单元（及其输出线），具体名字请以本地 `write_rtlil` 输出为准。

#### 4.3.5 小练习与答案

**练习 1**：在 RTLIL 里，`\count` 和 `$count` 有什么区别？

**答案**：`\count` 是**公有**名，来自 HDL 源码用户命名；`$count` 是 Yosys **自动生成**的内部名。两者前缀不同可避免冲突，并让优化 pass 区分「用户关心的线」与「临时线」。规则在 [rtlil_frontend.cc:164](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/rtlil/rtlil_frontend.cc#L164) 强制。

**练习 2**：`3'001` 表示什么？为什么内部存储时要把位序反转？

**答案**：`3'001` 是 3 位常量、值为 1（等价于 Verilog `3'd1`）。文本按从高位到低位书写，而 RTLIL 内部 `RTLIL::Const` 用向量下标 0 表示最低位，所以 `parse_const` 在 [rtlil_frontend.cc:302](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/rtlil/rtlil_frontend.cc#L302) 处把读入的位串反转，使下标 0 对应 LSb。

**练习 3**：一个 `cell … end` 块的「身体」由哪两类语句构成？

**答案**：由若干 `parameter …`（单元参数）和若干 `connect …`（端口连接）构成，直到 `end`。见 [rtlil_frontend.cc:659-700](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/rtlil/rtlil_frontend.cc#L659-L700)。

## 5. 综合实践

现在把三个模块串起来，完成本讲的核心任务：**读入 counter.v，导出 RTLIL，逐行注释，再读回验证**。

1. **导出**。在 `yosys>` 中：

   ```
   read_verilog examples/cmos/counter.v
   write_rtlil counter.rtlil
   ```

2. **注释**。打开 `counter.rtlil`，参照 4.3.4 的样本，为其中每个 `module`/`wire`/`cell`（若有）/`process`/`switch`/`sync` 行写一句中文注释。重点回答：
   - 哪些标识符是 `\` 公有名，哪些是 `$` 自动名？
   - 每条 `wire` 的位宽和端口方向分别是什么？
   - `always @(posedge clk)` 体现在 RTLIL 的哪一行（提示：找 `sync`）？

3. **读回验证**。退出并用干净会话执行：

   ```
   read_rtlil counter.rtlil
   stat
   check
   ```

   预期 `stat` 能报告模块 `counter`，`check` 不报致命错误，证明文本往返成功。

4. **进阶（可选）**：跑一遍 `proc` 再 `write_rtlil counter_proc.rtlil`，对比 `counter.rtlil`：观察 `process … end` 区块消失、被 `$mux` / `$dff` 等 cell 取代。这正是下一讲（u6-l2 proc）要深入的内容，这里先建立直观感受。

> ⚠️ 本实践依赖一个已构建好的 `yosys` 可执行程序（构建方式见 u1-l2）。若环境暂不可用，可先做「源码阅读型实践」：对照 4.3.3 引用的 `dump_module` 与 `parse_module`，手动推断 `write_rtlil` 会为 `counter.v` 生成哪些行。

## 6. 本讲小结

- RTLIL 是 Yosys 的**统一中间表示**：所有前端产出它，所有后端消费它，`write_rtlil`/`read_rtlil` 是它的文本序列化与反序列化工具。
- RTLIL 文本是**行导向**的，靠关键字（`module`/`wire`/`cell`/`connect`/`process`/`sync`/`end` 等）组织，缩进与 `#` 注释不影响解析。
- 顶层结构是 `autoidx` + 若干 `module … end`；模块体内含 `wire`/`memory`/`cell`/`process` 及模块级 `connect`。
- 标识符必须以 `\`（公有，来自源码）或 `$`（自动生成）开头；常量写成 `宽度'位串`，位状态有 `0/1/x/z`。
- `write_rtlil`（Backend，写整个 design）、`dump`（Pass，写选中部分到控制台）、`read_rtlil`（Frontend，读回文本，支持 `-lib`/`-overwrite`/`-legalize` 等选项）三者配套使用。
- 解析器是手写递归下降（`RTLILFrontendWorker`），与后端的 `dump_*` 函数严格互逆，因此文本可完整往返。

## 7. 下一步学习建议

本讲只让你「看懂」了 RTLIL 文本的皮相。接下来建议：

1. **u2-l2 Design 与 Module**：从文本深入到 C++ 数据结构，理解 `RTLIL::Design` 如何容纳多个 `RTLIL::Module`，以及选择栈、scratchpad 等全局状态。
2. **u2-l3 Wire、Cell 与 SigSpec 初识**：搞清楚文本里的 `wire`/`cell`/`connect` 在内存里对应什么对象，以及 `SigSpec` 如何表达「一段信号」。
3. 想提前理解「process 怎么变成门」的读者，可以跳到 **u6-l2 proc**；但建议先完成 u2 单元，打好数据结构基础。

继续阅读建议：先精读 [docs/source/yosys_internals/formats/rtlil_rep.rst](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/yosys_internals/formats/rtlil_rep.rst) 的 Cell/Wire/SigSpec/Process 四节，再带着问题进入 u2-l2。
