# Pass / Frontend / Backend 注册机制

## 1. 本讲目标

在 [u1-l2](u1-l2-build-and-run.md) 里我们已经有了一个关键印象：Yosys 里每一条命令（`opt`、`read_verilog`、`write_verilog`、`synth`……）背后都是一个 C++ 对象，它在程序启动时把自己挂进一张全局表 `pass_register`，于是命令名就能查到对象并执行。本讲就把这件事彻底讲透。

读完本讲，你应当能够：

- 说清 `Pass` 这个基类为何是「所有命令的统一入口」，以及 `Pass::execute()` 在整条调用链里的位置。
- 区分 `Pass`、`Frontend`、`Backend` 三者的关系：为什么前/后端「也是」Pass，却要多一张 `frontend_register` / `backend_register`。
- 说清 `init_register()` 何时被调用、那张「待注册链表」`first_queued_pass` 是怎么被一步步填满又被清空的。
- 能在源码里追踪一条命令（例如 `opt`）从敲下回车到进入 `execute()` 的完整路径。

## 2. 前置知识

本讲会用到前面几讲建立的几个概念，这里只做最简提醒，不重复展开：

- **RTLIL::Design**：Yosys 内存里的设计树根节点，所有 pass 的输入输出都围着它转（见 [u2-l2](u2-l2-design-module.md)）。
- **pass / 前端 / 后端**：前端把外部格式读成 RTLIL，pass 在 RTLIL 上做变换，后端把 RTLIL 写出去（见 [u1-l1](u1-l1-project-overview.md)）。
- **pass_register / first_queued_pass / yosys_setup**：在 [u1-l2](u1-l2-build-and-run.md) 里已提到「新增 pass 无需修改中心注册代码」，本讲正是要把这句话拆到行级。
- **IdString**：Yosys 的标识符类型，命令名在表里就是以 `std::string` 为 key（见 [u3-l3](u3-l3-idstring-const-hashlib.md)）。

还需要一个 C++ 小知识：**全局静态对象的构造发生在 `main()` 之前**。这是整个自动注册机制能成立的前提——稍后会看到，正是这一点让「声明一个静态变量」等于「注册一条命令」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [kernel/register.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.h) | 声明 `Pass` / `ScriptPass` / `Frontend` / `Backend` 四个类，以及三张全局注册表的 `extern` 声明。 |
| [kernel/register.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc) | 实现注册表的填充、命令的分发（`Pass::call`）、前/后端名字转换与文件 I/O 桥接；并定义了 `help` / `echo` / `license` 等内置命令。 |
| [kernel/yosys.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc) | `yosys_setup()` 里调用 `Pass::init_register()` 完成注册；并提供 `run_pass` / `run_frontend` / `run_backend` 三个库级入口。 |
| [passes/opt/opt.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt.cc) | 一个具体的普通 Pass（`OptPass`，命令名 `opt`），用来做调用链追踪样本。 |
| [frontends/verilog/verilog_frontend.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_frontend.cc) | 一个具体的 Frontend（`VerilogFrontend`），用来说明前端的「一名两表」注册。 |

## 4. 核心概念与源码讲解

### 4.1 Pass 基类：所有命令的统一入口

#### 4.1.1 概念说明

Yosys 里有一条贯穿全局的设计原则：**凡是被用户当作「命令」执行的东西，都是一个 `Pass`。**

不论它是做综合优化的 `opt`、读文件的 `read_verilog`、写文件的 `write_verilog`，还是打印帮助的 `help`——它们都继承自同一个基类 `Pass`，并且都实现同一个纯虚函数：

```cpp
virtual void execute(std::vector<std::string> args, RTLIL::Design *design) = 0;
```

这带来一个巨大的好处：命令调度器只需要认识 `Pass` 这一种类型。调度器拿到命令名 → 在表里查到 `Pass*` → 调用它的 `execute()`，完全不需要知道这个 pass 具体干什么。这正是「开闭原则」的体现：新增一种命令，不需要修改调度器，只要再派生一个 `Pass` 子类即可（具体怎么自动登记，见 4.3）。

`Pass` 基类除了 `execute()`，还提供了一组通用设施：

- `help()`：打印该命令的帮助文本（`help <cmd>` 命令就调它）。
- `extra_args()`：统一处理「选项之后的剩余参数 / 选择表达式」，几乎所有 pass 都在 `execute()` 开头调用它。
- `pre_execute()` / `post_execute()`：每次执行前后的「钩子」，负责计时、维护 `current_pass` 调用栈、清理选择栈。
- `call_counter` / `runtime_ns`：统计该 pass 被调用了多少次、累计耗时多少。

#### 4.1.2 核心流程

当用户在 shell 或脚本里输入一条命令（比如 `opt -full`）时，分发的核心是两个静态重载 `Pass::call`：

1. **`call(design, string)`**：接收一整行字符串。它做「词法层」工作：
   - 按空白切词；
   - 处理 `!`（转交 shell 执行）、`#`（注释，丢到行尾）、`;`（命令分隔符，连续 `;;`/`;;;` 分别等价于 `clean` / `clean -purge`）；
   - 遇到换行或分号就把已收集的词组成一个 `args` 向量，交给下一层。
2. **`call(design, args)`**：接收已经切好的 `args` 向量。它做「派发层」工作：
   - 以 `args[0]`（命令名）查 `pass_register`；
   - 找不到就报 `No such command`；
   - 找到就 `pre_execute()` → `pass->execute(args, design)` → `post_execute()`，并保证选择栈深度回到调用前。

用一个伪流程图表示整条派发链：

```
用户输入 "opt -full"
        │
        ▼
Pass::call(design, string)        ← 切词、处理 ; # !
        │  得到 args = ["opt", "-full"]
        ▼
Pass::call(design, args)          ← 查 pass_register["opt"]
        │  得到 Pass* = OptPass 实例
        ▼
pre_execute()  →  execute(args, design)  →  post_execute()
                         │
                         ▼
                   OptPass 真正的优化逻辑
```

为什么要在 `execute()` 外面套 `pre_execute/post_execute`？因为命令可能**嵌套**调用：`opt` 内部会再 `Pass::call(design, "opt_expr")`（见 4.1.3）。`pre_execute` 把当前 pass 压成一个栈（`current_pass = this`），`post_execute` 再弹出，这样每个 pass 的 `runtime_ns` 才能只统计「自己独占」的时间——子 pass 的耗时会被从父 pass 里扣掉（`subtract_from_current_runtime_ns`）。

#### 4.1.3 源码精读

先看 `Pass` 类的骨架（[kernel/register.h:61-125](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.h#L61-L125)）。关键几行：

```cpp
// 唯一的「业务入口」，子类必须实现
virtual void execute(std::vector<std::string> args, RTLIL::Design *design) = 0;

// 两个派发重载（静态）
static void call(RTLIL::Design *design, std::string command);          // 接收整行
static void call(RTLIL::Design *design, std::vector<std::string> args);// 接收切好的词

// 注册相关（见 4.3）
Pass *next_queued_pass;
virtual void run_register();
static void init_register();
static void done_register();
```

派发的「派发层」在 [kernel/register.cc:276-305](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L276-L305)：

```cpp
void Pass::call(RTLIL::Design *design, std::vector<std::string> args)
{
    if (args.size() == 0 || args[0][0] == '#' || args[0][0] == ':')
        return;
    ...
    if (pass_register.count(args[0]) == 0)
        log_cmd_error("No such command: %s (type 'help' for a command overview)\n", args[0]);
    Pass *pass = pass_register[args[0]];          // ← 查表
    ...
    size_t orig_sel_stack_pos = design->selection_stack.size();
    auto state = pass->pre_execute();
    pass->execute(args, design);                  // ← 真正执行
    pass->post_execute(state);
    while (design->selection_stack.size() > orig_sel_stack_pos)
        design->pop_selection();                  // ← 选择栈兜底清理
}
```

这段就是「命令调度器」的全部核心：查表 → 执行 → 清选择栈。注意 `#` 开头是注释、`:` 开头是标签（`from: ... to:` 脚本分段用），这两类直接 `return` 不执行。

「词法层」`call(design, string)` 在 [kernel/register.cc:210-274](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L210-L274)，其中对分号的特殊处理值得一看：

```cpp
if (tok.back() == ';') {
    int num_semikolon = 0;
    while (!tok.empty() && tok.back() == ';')
        tok.resize(tok.size()-1), num_semikolon++;
    if (!tok.empty())
        args.push_back(tok);
    call(design, args);                 // 分号前的命令立即派发
    args.clear();
    if (num_semikolon == 2) call(design, "clean");        // ;; → clean
    if (num_semikolon == 3) call(design, "clean -purge"); // ;;; → clean -purge
}
```

这正是脚本里 `;;` 能当「优化一轮」用的原因。

而「嵌套调用」的真实样本在 `OptPass::execute()` 里——它本身是一条命令，却在体内反复调用别的命令（[passes/opt/opt.cc:158-170](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt.cc#L158-L170)）：

```cpp
Pass::call(design, "opt_expr" + opt_expr_args);
Pass::call(design, "opt_merge" + opt_merge_args);
...
Pass::call(design, "opt_clean" + opt_clean_args);
```

也就是说，`opt` 这个 pass 的「实现」很大程度上就是「按一定顺序调用一堆别的 pass」。这也是为什么 `pre/post_execute` 必须维护调用栈——否则 `opt` 的 `runtime_ns` 会把所有子 pass 的时间都算进去。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「命令名 → pass_register → execute」这条链，并验证 `;;` 的语法糖。

**操作步骤**：

1. 在项目根目录运行交互式 shell（如果你已按 [u1-l2](u1-l2-build-and-run.md) 构建过）：
   ```bash
   ./build/yosys
   ```
2. 在 `yosys>` 提示符下依次输入：
   ```
   read_verilog examples/cmos/counter.v
   opt
   stat
   ```
3. 退出后，再用单行命令做对比，验证 `;;` 等价于 `clean`：
   ```bash
   ./build/yosys -p "read_verilog examples/cmos/counter.v;; stat"
   ```

**需要观察的现象**：

- 第 2 步每条命令前，日志会打印一行 `-- Running command '<命令>' --`（来自 4.3 里要讲的 `run_pass`），证明每个命令名都被独立派发。
- `opt` 执行时，日志里会陆续出现 `opt_expr`、`opt_merge` 等子 pass 的 header，证明 `opt` 确实在内部嵌套调用了它们。
- 第 3 步的 `;;` 会被拆成 `read_verilog ...`、`clean`、`stat` 三条命令执行（日志里能看到 `clean` 出现）。

**预期结果**：你能从日志里清晰看到「一条用户命令 = 一次 `Pass::call` = 一次 `execute`」的对应关系，并确认分号语法糖被正确展开。

> 若本地尚未构建成功，以上为「待本地验证」；即便不运行，你也可以在 [register.cc:244-256](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L244-L256) 直接读到 `;;` / `;;;` 的展开逻辑。

#### 4.1.5 小练习与答案

**练习 1**：`Pass::call(design, args)` 里为什么要记录 `orig_sel_stack_pos` 并在 `execute()` 之后 `pop_selection()`？

**参考答案**：因为某些命令（如 `select`）会向 `design->selection_stack` 压栈来限定后续 pass 的作用范围；但一条独立的命令执行完后，它临时压入的选择应当被撤销，避免「污染」后续命令。记录进入时的栈深度并在退出时弹回到该深度，就是这种「作用域隔离」的兜底机制。

**练习 2**：为什么 `opt` 的 `runtime_ns` 不会包含它调用的 `opt_expr` 等子 pass 的时间？

**参考答案**：`post_execute` 计算本 pass 的耗时后，会调用 `subtract_from_current_runtime_ns(time_ns)`，把这段耗时从「父 pass」（`current_pass`）的 `runtime_ns` 里扣掉。由于 `pre_execute` 把 `current_pass` 设成了当前 pass，嵌套调用时父 pass 恰好是外层的 `opt`，于是子 pass 的时间被从 `opt` 中剔除，实现「只统计独占时间」。

---

### 4.2 Frontend / Backend：读写文件的特化 Pass

#### 4.2.1 概念说明

`Frontend` 和 `Backend` 并不是和 `Pass` 平级的新东西——它们**继承自 `Pass`**：

```
Pass
 ├── ScriptPass   （编排一串子 pass 的脚本型 pass，见 u4-l2）
 ├── Frontend     （读外部格式 → RTLIL）
 └── Backend      （RTLIL → 写外部格式）
```

既然前/后端也是 Pass，那为什么还要单独搞 `Frontend` / `Backend` 两个子类？因为它们面对的问题有共性：**都要和文件流（`istream` / `ostream`）打交道**。于是基类把这部分公共逻辑抽出来：

- `Frontend` 暴露一个「面向输入流」的纯虚函数：
  ```cpp
  virtual void execute(std::istream *&f, std::string filename,
                       std::vector<std::string> args, RTLIL::Design *design) = 0;
  ```
- `Backend` 暴露一个「面向输出流」的纯虚函数：
  ```cpp
  virtual void execute(std::ostream *&f, std::string filename,
                       std::vector<std::string> args, RTLIL::Design *design) = 0;
  ```

子类（比如 `VerilogFrontend`）只实现这个「流版本」，至于「文件名怎么解析、怎么打开、gzip 怎么解压、多个文件通配怎么排队」这些琐碎但统一的活，全由 `Frontend::extra_args` / `Backend::extra_args` 代劳。

#### 4.2.2 核心流程

前/后端有一个非常巧妙的设计：**一个对象，两个名字，两张表**。

以 Verilog 前端为例。它的构造写法是 `Frontend("verilog", "read modules from Verilog file")`——只给了一个名字 `"verilog"`。但基类构造函数会据此推导出**两个**名字（[kernel/register.cc:425-429](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L425-L429)）：

- `pass_name = "read_verilog"`（对外作为普通命令名）
- `frontend_name = "verilog"`（作为「前端种类」名）

注册时（`Frontend::run_register`）会把它**同时**塞进两张表：

```
pass_register    ["read_verilog"] = VerilogFrontend 实例
frontend_register["verilog"]      = 同一个 VerilogFrontend 实例
```

为什么要两张表、两条入口？因为前端有两种被调用的方式：

1. **当作普通命令**：用户直接敲 `read_verilog counter.v`。此时走 `Pass::call` → `pass_register["read_verilog"]` → 进入 `Frontend::execute(args, design)`（这是 `final` 的桥接方法），由它自己负责打开文件。
2. **当作用流喂入**：程序内部（例如 `script` 命令、或 `read` 这种需要先决定前端种类的封装）已经拿到了一个输入流，只想说「用 verilog 前端解析这个流」。此时走 `Frontend::frontend_call` → `frontend_register["verilog"]` → 直接调用流版本 `execute(istream*, ...)`，跳过文件打开逻辑。

Backend 完全对称：`Backend("verilog")` 推导出 `pass_name="write_verilog"`、`backend_name="verilog"`，同时进 `pass_register` 和 `backend_register`；`Backend::backend_call` 是面向输出流的直连入口。

> 小贴士：构造函数里有个 `=` 前缀的特殊规则——若名字以 `=` 开头（如 `"=myread"`），则 `pass_name` 直接取去掉 `=` 后的部分，不再自动加 `read_`/`write_` 前缀。这是给那些不想遵守 `read_xxx`/`write_xxx` 命名约定的前端用的逃生口。

#### 4.2.3 源码精读

先看类声明，注意 `execute(args, design)` 被标了 `override final`（[kernel/register.h:147-166](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.h#L147-L166)）：

```cpp
struct Frontend : Pass
{
    std::string frontend_name;
    ...
    void execute(std::vector<std::string> args, RTLIL::Design *design) override final;          // 桥接，子类不可改
    virtual void execute(std::istream *&f, std::string filename,
                         std::vector<std::string> args, RTLIL::Design *design) = 0;             // 子类实现这个
    ...
};
```

`final` 的含义是：子类（`VerilogFrontend` 等）**只能**重写流版本，不能重写 `args` 版本——后者是基类锁定的「文件打开 → 调流版本」桥接逻辑。

这个桥接的实现（[kernel/register.cc:446-458](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L446-L458)）：

```cpp
void Frontend::execute(std::vector<std::string> args, RTLIL::Design *design)
{
    log_assert(next_args.empty());
    do {
        std::istream *f = NULL;
        next_args.clear();
        auto state = pre_execute();
        execute(f, std::string(), args, design);   // ← 转交给流版本
        post_execute(state);
        args = next_args;                          // ← 通配匹配出的剩余文件，循环处理
        delete f;
    } while (!args.empty());
}
```

注意 `do...while` 和 `next_args`：当一次 `read_verilog *.v` 被 glob 展开成多个文件时，`extra_args` 会把剩余文件名放进 `next_args`，桥接函数循环地把每个文件喂给流版本 `execute`。这就是为什么前端能「一条命令读多个文件」。

「一名两表」的注册逻辑（[kernel/register.cc:431-440](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L431-L440)）：

```cpp
void Frontend::run_register()
{
    if (pass_register.count(pass_name) && !replace_existing_pass())
        log_error("Unable to register pass '%s', pass already exists!\n", pass_name);
    pass_register[pass_name] = this;            // 表 1：当普通命令

    if (frontend_register.count(frontend_name) && !replace_existing_pass())
        log_error("Unable to register frontend '%s', frontend already exists!\n", frontend_name);
    frontend_register[frontend_name] = this;    // 表 2：当前端种类
}
```

`Backend::run_register` 结构完全相同（[kernel/register.cc:577-586](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L577-L586)），只是把 `frontend_register` 换成 `backend_register`。

那么 `frontend_register` 这第二张表到底谁在用？答案是面向流的直连入口 `Frontend::frontend_call`（[kernel/register.cc:548-569](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L548-L569)），它的核心三分支是：

```cpp
if (frontend_register.count(args[0]) == 0)
    log_cmd_error("No such frontend: %s\n", args[0]);   // 用 frontend_name 查表

if (f != NULL) {                          // 分支 A：已给好输入流，直接解析
    ... frontend_register[args[0]]->execute(f, filename, args, design);
} else if (filename == "-") {            // 分支 B：从 stdin 读
    std::istream *f_cin = &std::cin;
    ... frontend_register[args[0]]->execute(f_cin, "<stdin>", args, design);
} else {                                  // 分支 C：给的是文件名，转交给 args 版本去开文件
    if (!filename.empty()) args.push_back(filename);
    frontend_register[args[0]]->execute(args, design);
}
```

而 `frontend_call` 又被库级入口 `run_frontend` 调用（[kernel/yosys.cc:840-855](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L840-L855)）。整条「读文件」链因此是：

```
driver 决定要用 "verilog" 前端读 counter.v
   → run_frontend(filename="counter.v", command="verilog")
   → Frontend::frontend_call(design, NULL, "counter.v", "verilog")
   → frontend_register["verilog"] = VerilogFrontend
   → 走分支 C → execute(args, design)（桥接）
   → 打开文件 → 流版本 execute(istream*, ...)
   → 真正解析 Verilog，产出 RTLIL
```

具体前端的声明极简，[frontends/verilog/verilog_frontend.cc:72-73](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_frontend.cc#L72-L73)：

```cpp
struct VerilogFrontend : public Frontend {
    VerilogFrontend() : Frontend("verilog", "read modules from Verilog file") { }
    ...
    void execute(std::istream *&f, std::string filename, std::vector<std::string> args, RTLIL::Design *design) override;
} VerilogFrontend;   // ← 注意这个变量名：它是一个全局静态实例
```

注意末尾的 `} VerilogFrontend;`——这是在定义结构体的同时声明了一个全局静态对象。正是这个对象的构造（在 `main` 之前）触发了注册。至于「构造如何触发注册」，是下一节的主题。

#### 4.2.4 代码实践

**实践目标**：验证前端的「一名两表」，并理解 `read_<x>` 命令名是怎么来的。

**操作步骤**（源码阅读型，无需运行）：

1. 打开 [kernel/register.cc:425-429](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L425-L429)，对照 `Frontend("verilog", ...)`，手动推导：`pass_name` 和 `frontend_name` 分别是什么？
2. 打开 [frontends/verilog/verilog_frontend.cc:72-73](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_frontend.cc#L72-L73)，确认 `VerilogFrontend` 是用 `Frontend("verilog", ...)` 构造的，而非 `Pass("read_verilog", ...)`。
3. 若本地已构建，运行下面的命令验证两个名字都生效：
   ```bash
   ./build/yosys -p "help read_verilog"     # 走 pass_register，应打印 read_verilog 帮助
   ```

**需要观察的现象**：

- `help read_verilog` 能打印出帮助，证明 `read_verilog` 确实作为命令名登记进了 `pass_register`。
- 你在源码里却搜不到任何 `Pass("read_verilog", ...)` 的写法——`read_verilog` 这个名字完全是构造函数从 `"verilog"` 拼出来的。

**预期结果**：你能解释「为什么源码里只出现 `Frontend("verilog")`，但用户命令却是 `read_verilog`」——这正是 `read_` 前缀自动拼接 + 双表注册的结果。

#### 4.2.5 小练习与答案

**练习 1**：假如有人想新增一个 YAML 格式的前端，命令名希望叫 `read_yaml`。他的构造函数应该怎么写？会被登记进哪几张表？

**参考答案**：写成 `Frontend("yaml", "read modules from YAML file")`。基类会自动拼出 `pass_name="read_yaml"`、`frontend_name="yaml"`。`Frontend::run_register` 会把它同时登记进 `pass_register["read_yaml"]` 和 `frontend_register["yaml"]` 两张表。用户既能用 `read_yaml x.yml`，程序内部也能用 `frontend_call(..., "yaml", ...)` 直连。

**练习 2**：`Frontend::execute(args, design)` 为什么是 `final` 的？如果允许子类重写它会有什么问题？

**参考答案**：因为它承担的是「打开文件 / 处理通配 / 解压 / 循环喂流」这一整套固定流程，并最终转调流版本 `execute(istream*, ...)`。如果允许子类重写它，子类就得各自重新实现一遍文件打开逻辑，失去抽象的意义，也可能绕过 `pre/post_execute` 的计时与选择栈管理。标 `final` 强制子类只关心「拿到流之后如何解析」，把 I/O 细节统一收口在基类。

---

### 4.3 注册表与 init_register：自动注册机制

#### 4.3.1 概念说明

前面两节反复出现 `pass_register` / `frontend_register` / `backend_register` 三张表。它们是 `std::map<std::string, ...>`，定义在 [kernel/register.cc:41-43](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L41-L43)：

```cpp
std::map<std::string, Frontend*> frontend_register;
std::map<std::string, Pass*>     pass_register;
std::map<std::string, Backend*>  backend_register;
```

整本讲最关键的问题来了：**这几百条命令，是怎么进到表里的？**

答案藏在 [u1-l2](u1-l2-build-and-run.md) 提到的那句话里——「新增 pass 无需修改中心注册代码」。Yosys 用了一个经典的 C++ 惯用法：**全局静态对象的构造发生在 `main()` 之前**。

每条命令都被定义成一个全局静态对象（比如上一节看到的 `} VerilogFrontend;`，以及 `} HelpPass;`、`} EchoPass;` 等）。它的构造函数（也就是 `Pass` 的构造函数）做了一件很简单的事：**把自己头插进一个全局单链表 `first_queued_pass`**。等到 `main()` 里调用 `Pass::init_register()` 时，再一次性把这条链表里的所有对象灌进三张表。

这样设计的好处是「去中心化」：写一个新的 pass，只需要在它自己的源文件里定义一个全局静态实例，编译进二进制即可，**完全不需要在任何「注册中心」添加一行代码**。链表 `first_queued_pass` 就充当了「构造期暂存区」，弥补了「`main` 还没跑、表还没建好」的时间差。

#### 4.3.2 核心流程

整个生命周期分成三个阶段：

**阶段一：构造期（`main` 之前）**——逐个 pass 自登记到链表。

```
定义 struct FooPass : Pass { ... } FooPass;
   │  触发 Pass 构造函数
   ▼
Pass::Pass(name, ...):
   next_queued_pass = first_queued_pass;   // 我接在原链头后面
   first_queued_pass = this;               // 我成为新链头
```

这是「头插法」，所以后构造的 pass 排在链表前部；但这不影响最终表里的内容（map 按名字排）。

**阶段二：初始化期（`yosys_setup` 里）**——`init_register` 把链表搬进三张表。

```
Pass::init_register():
   while (first_queued_pass) {
       added_passes.push_back(first_queued_pass);
       first_queued_pass->run_register();          // 实际写表（含前/后端的双表写入）
       first_queued_pass = first_queued_pass->next_queued_pass;
   }
   for (auto p : added_passes) p->on_register();   // 写完表后的回调
```

注意 `run_register()` 是虚函数：普通 `Pass` 只写 `pass_register`；`Frontend` / `Backend` 的覆写版本会额外写 `frontend_register` / `backend_register`。这正是多态在注册环节的运用。

**阶段三：关闭期（`yosys_shutdown` 里）**——`done_register` 反向清理。

```
Pass::done_register():
   for (auto &it : pass_register) it.second->on_shutdown();  // 逐个回调
   frontend_register.clear();
   pass_register.clear();
   backend_register.clear();
```

时间线对齐如下（与 [u1-l2](u1-l2-build-and-run.md) 的初始化流程呼应）：

```
进程启动
  │  （main 之前）各全局 pass 对象构造 → 头插 first_queued_pass
  ▼
main() → yosys_setup()
  │   IdString::ensure_prepopulated()    （先填好知名标识符）
  │   Pass::init_register()              ← 本讲主角：链表 → 三张表
  │   yosys_design = new RTLIL::Design   （再建设计）
  ▼
... 跑命令（查 pass_register）...
  ▼
yosys_shutdown()
  │   Pass::done_register()              ← 清空三张表
  │   delete yosys_design
```

注册的时机非常讲究：**必须早于 `yosys_design` 的创建**，因为后续一切命令都依赖表已就绪；同时又**必须晚于 `IdString::ensure_prepopulated()`**，因为登记时（如错误信息里的名字）要用到已初始化的标识符体系。`init_register` 恰好被放在这两者之间（[kernel/yosys.cc:264](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L264)）。

#### 4.3.3 源码精读

先看构造函数的头插（[kernel/register.cc:64-71](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L64-L71)）：

```cpp
Pass::Pass(std::string name, std::string short_help, source_location location) :
    pass_name(name), short_help(short_help), location(location)
{
    next_queued_pass = first_queued_pass;
    first_queued_pass = this;     // ← 头插进待注册链表
    call_counter = 0;
    runtime_ns = 0;
}
```

普通 `Pass` 的 `run_register`（[kernel/register.cc:73-78](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L73-L78)）只写一张表，并处理「重名」：

```cpp
void Pass::run_register()
{
    if (pass_register.count(pass_name) && !replace_existing_pass())
        log_error("Unable to register pass '%s', pass already exists!\n", pass_name);
    pass_register[pass_name] = this;
}
```

注意 `replace_existing_pass()`：基类默认返回 `false`（[kernel/register.h:120](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.h#L120)），即重名就报错；但插件可以覆写它返回 `true`，从而**覆盖**同名内置命令——这是 Yosys 插件能「替换」内置 pass 的机制基础（在 [u9-l1](u9-l1-write-custom-pass.md) 会用到）。

核心搬运函数 `init_register`（[kernel/register.cc:80-90](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L80-L90)）：

```cpp
void Pass::init_register()
{
    vector<Pass*> added_passes;
    while (first_queued_pass) {
        added_passes.push_back(first_queued_pass);
        first_queued_pass->run_register();                    // 多态：普通 pass / Frontend / Backend 各写各的表
        first_queued_pass = first_queued_pass->next_queued_pass;
    }
    for (auto added_pass : added_passes)
        added_pass->on_register();                            // 全部写完后，统一回调
}
```

这里有个细节：先把链表存进 `added_passes`，再统一调 `on_register()`，而不是「写一个表就回调一个」。这样保证回调 `on_register()` 时，**所有命令都已在表里**——若某 pass 的初始化逻辑需要查别的命令是否存在，此时可以安全查询。

调用 `init_register` 的位置（[kernel/yosys.cc:236-268](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L236-L268)），关键三行的顺序：

```cpp
void yosys_setup()
{
    ...
    IdString::ensure_prepopulated();   // ① 先填知名标识符
    ...
    Pass::init_register();             // ② 再灌三张表
    yosys_design = new RTLIL::Design;  // ③ 最后建设计
    ...
}
```

对称的清理 `done_register`（[kernel/register.cc:92-101](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L92-L101)），在 `yosys_shutdown` 里被调用（[kernel/yosys.cc:283](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L283)）：

```cpp
void Pass::done_register()
{
    for (auto &it : pass_register)
        it.second->on_shutdown();        // 给每个 pass 一次清理机会
    frontend_register.clear();
    pass_register.clear();
    backend_register.clear();
    log_assert(first_queued_pass == NULL); // 此时期望链表早已搬空
}
```

最后是库级入口 `run_pass`，它把「跑一条命令」简化到一行（[kernel/yosys.cc:857-865](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L857-L865)）：

```cpp
void run_pass(std::string command, RTLIL::Design *design)
{
    if (design == nullptr) design = yosys_design;
    log("\n-- Running command `%s' --\n", command);   // ← 这就是日志里那行 header 的来源
    Pass::call(design, command);                      // ← 最终回到 4.1 的派发链
}
```

至此，把 4.1、4.2、4.3 串起来，一条命令从「敲下回车」到「进入 execute」的完整路径就闭环了。

#### 4.3.4 代码实践

**实践目标**：亲手追踪「一条命令」从 `run_pass` 到 `execute` 的完整调用路径，并解释三张表如何被填充。

**操作步骤**（源码阅读型实践，对照 [kernel/register.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc) 与 [kernel/yosys.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc)）：

1. 假设用户执行 `yosys -p "opt"`。从 `run_pass("opt", design)` 出发（[yosys.cc:857-865](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L857-L865)），它调用 `Pass::call(design, "opt")`。
2. 进入词法层 `call(design, string)`（[register.cc:210-274](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L210-L274)）：`opt` 没有空格/分号，切出 `args=["opt"]`，递归进入派发层。
3. 进入派发层 `call(design, args)`（[register.cc:276-305](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L276-L305)）：`pass_register["opt"]` 命中 `OptPass`，调用其 `execute()`（[opt.cc:69](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt.cc#L69)）。
4. 在一张纸上把上述四个文件、四个函数画成一张顺序图，标出每一步的「文件:行号」。

**解释 `frontend_register` / `backend_register` 如何被填充**：

- 它们**不是**在 `main` 里被手工 `insert` 的。填充发生在 `Pass::init_register()` 遍历 `first_queued_pass` 链表时，对每个对象调用 `run_register()`——由于多态，`Frontend` 对象会执行 `Frontend::run_register`（[register.cc:431-440](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L431-L440)），从而把 `frontend_name → this` 写进 `frontend_register`；`Backend` 同理。
- 而链表本身是在程序启动早期、由各前端/后端源文件里的全局静态实例（如 `} VerilogFrontend;`）在构造时头插填满的。

**需要观察的现象 / 预期结果**：

- 你应当得到一张清晰的调用顺序图，说明「自动注册」靠的是 *全局对象构造 + 头插链表 + init_register 多态搬运* 这三件事，全程没有任何中心化的「命令清单」。
- 你能口头回答：三张表的内容在 `init_register` 之后才完整，在 `done_register` 之后被清空。

> 若想在运行时直观验证表的规模，可在 `yosys>` 里执行 `help`（无参数）——它会遍历 `pass_register` 列出所有命令（[register.cc:1065-1067](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L1065-L1067)），你能看到全部已登记的命令。这部分为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `init_register` 要先 `run_register()` 把所有对象写进表，**之后**再统一调 `on_register()`，而不是「写一个就回调一个」？

**参考答案**：因为某些 pass 的 `on_register()` 初始化逻辑可能需要查询「别的命令是否已注册」（例如检查依赖的子命令是否存在）。如果边写边回调，排在链表前面的 pass 回调时，后面的 pass 尚未入表，查询就会误判为「不存在」。先全部入表、再统一回调，保证回调发生时三张表已完整。

**练习 2**：源码里定义了 `#define MAX_REG_COUNT 1000`（[register.cc:35](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L35)）。结合「全局静态对象在 `main` 之前构造」这一事实，想想这个常量可能在防御什么风险？

**参考答案**：全局静态对象的构造顺序在同一编译单元内是从上到下，但**跨编译单元的构造顺序是未定义的**。Yosys 有大量分布在数百个源文件里的 pass 对象，若存在「某个全局对象的构造依赖 pass_register 已建好」之类的隐含依赖，就可能因顺序不确定而出错。`MAX_REG_COUNT` 这类上限更多是作为一种安全护栏/断言用意（防止异常情况下链表无限增长），核心机制仍依赖于「`first_queued_pass` 只是个链表、真正的写表推迟到 `init_register`」这一去耦设计，从而规避了构造顺序问题。

## 5. 综合实践

把本讲三个最小模块串成一个端到端的小任务：**追踪一条真实命令从「敲下回车」到「进入业务逻辑」的全链路，并定位它属于哪张表、哪种 pass。**

任务步骤：

1. 选一条命令，建议选 `write_verilog`（一个 Backend）。运行：
   ```bash
   ./build/yosys -p "read_verilog examples/cmos/counter.v; write_verilog /tmp/out.v"
   ```
2. 在源码里回答下列问题，每条都给出「文件:行号」证据：
   - `write_verilog` 这个名字是哪段代码拼出来的？（提示：Backend 构造函数 + 声明 `Backend("verilog", ...)`）
   - 它被登记进了哪几张表？由哪个函数写入？
   - 用户敲 `write_verilog` 时，走的是 `Pass::call` 派发链还是 `Backend::backend_call`？为什么？
   - 它最终进入的 `execute` 是「args 版本」还是「流版本」？由谁负责打开 `/tmp/out.v`？
3. 把答案整理成一张表：

   | 环节 | 函数/位置 | 属于哪张表 / 哪种 pass |
   |------|-----------|------------------------|
   | 名字拼接 | … | … |
   | 注册写入 | … | backend_register + pass_register |
   | 用户派发 | … | … |
   | 打开文件 | … | … |

4. **进阶思考**：如果把同样的追踪对 `read_verilog`（Frontend）做一遍，你会发现两条链高度对称。请用一句话概括 Frontend 与 Backend 在「名字拼接、双表注册、流桥接」三点上的对称性。

> 若本地未构建，可改为纯源码阅读：上述每一问都能在 [register.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc) 与 [yosys.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc) 里找到确定答案，标注为「待本地验证」的只是运行观察部分。

## 6. 本讲小结

- **`Pass` 是所有命令的统一抽象**：唯一的业务入口是纯虚 `execute(args, design)`；调度靠静态 `Pass::call`，分词法层（处理 `; # !`、`;;` 语法糖）和派发层（查 `pass_register` → `execute`）两步。
- **每次执行都被 `pre_execute/post_execute` 包裹**，维护 `current_pass` 调用栈与选择栈深度，使嵌套调用时的计时和作用域隔离成为可能。
- **`Frontend` / `Backend` 是特化的 `Pass`**：它们额外暴露「面向流的」纯虚 `execute`，并用一个 `final` 的桥接方法把「文件名 → 打开 → 喂流」统一收口，子类只关心「如何解析流」。
- **「一名两表」是前/后端的精髓**：`Frontend("verilog")` 自动拼出 `read_verilog`，同时登记进 `pass_register`（当普通命令）和 `frontend_register`（当前端种类），从而既能被用户直接调用，也能被程序用流直连。
- **注册是去中心化的**：每条命令是一个全局静态对象，构造时头插进 `first_queued_pass` 链表；`Pass::init_register()`（在 `yosys_setup` 中、`new Design` 之前）通过多态 `run_register()` 把链表搬进三张表，`done_register()` 在 `yosys_shutdown` 里对称清理。
- **新增命令零中心化成本**：定义一个全局静态 `Pass` 子类实例即可，无需改动任何注册中心——这正是 Yosys 高度可扩展的根基，也为后续编写自定义 pass（[u9-l1](u9-l1-write-custom-pass.md)）打下基础。

## 7. 下一步学习建议

本讲把「命令如何被找到并执行」讲清了。接下来的合理走向：

- **横向：选 ScriptPass**——[u4-l2](u4-l2-script-pass-synth-prep.md) 讲 `ScriptPass` 如何用一个 pass 编排一长串子 pass（`synth` / `prep`），你会看到 `Pass::call` 的嵌套调用在真实综合脚本里是如何被有组织地使用的。
- **横向：选 select**——[u4-l3](u4-l3-select-mechanism.md) 讲选择机制，解释本讲里反复出现的选择栈 `selection_stack` 到底装了什么、怎么限定 pass 作用范围。
- **纵向：动手写 pass**——直接跳到 [u9-l1](u9-l1-write-custom-pass.md)，利用本讲学到的「全局静态对象自动注册」机制，亲手写一个能被 `yosys` 加载的自定义 pass。
- **补充阅读**：通读一遍 [kernel/register.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc) 末尾的 `HelpPass` / `EchoPass` / `LicensePass`（约 [register.cc:753-1238](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L753-L1238)），它们是「最小 pass」的最佳范本，几十行就完整展示了「声明即注册」的全貌。
