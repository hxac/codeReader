# 编写你的第一个自定义 Pass

## 1. 本讲目标

Yosys 的强大不在于它内置了多少命令，而在于「命令」本身是一个开放抽象：任何人都可以用 C++ 写一个继承自 `Pass` 的类，把它注册成一条新命令，然后像 `opt`、`stat` 一样在 shell 或脚本里调用它。本讲就带你亲手完成这件事。

学完本讲，你应当能够：

1. 写出一个最小的自定义 Pass：从 `Pass` 派生、实现 `execute()`、写 `help()`。
2. 在 `execute()` 里用 `extra_args` 处理命令行选项与「选择（selection）」，并遍历 `Design → Module → Cell` 用 `log()` 输出结果。
3. 用 `yosys-config` 把这个 Pass 编译成 `.so` 共享库，再用 `yosys -m` 把它当作「插件」加载并运行。

本讲是「扩展 Yosys」的第一个台阶，后续的 C++ API（u9-l2）与 Python 绑定（u9-l3）都建立在「一条 Pass 如何被定义、注册、调用」这套机制之上。

## 2. 前置知识

本讲默认你已经掌握以下两讲的内容（本讲会直接使用其中的结论，不再重复论证）：

- **u4-l1 Pass / Frontend / Backend 注册机制**：`Pass::execute(args, design)` 是所有命令的唯一业务入口；每条命令是一个全局静态对象，构造时把自己头插进 `first_queued_pass` 链表，`yosys_setup()` 中的 `init_register()` 把链表搬进全局表 `pass_register`；命令分发经 `Pass::call` 完成。如果你忘了这些，先回去看 u4-l1。
- **u3-l1 Module / Cell / Wire 的完整接口**：`design` 拥有若干 `module`，每个 `module` 用 `cells_` 字典存单元；`Cell` 有 `type`（如 `$and`、`$dff`）与 `parameters`；遍历与读写都靠这些字段。

另外补充两个本讲会用到的「日志」小工具（细节见 u4-l4）：

- `log(...)` 是一个 `printf` 风格的函数，所有用户可见的输出都走它（不要用 `printf`/`cout`）。
- `log_id(id)` 把一个 `RTLIL::IdString`（比如 `\\a`、`$and`）转成可直接 `%s` 打印的 C 字符串。
- `log_header(design, "...")` 打印带编号的大标题。

## 3. 本讲源码地图

本讲涉及的文件按职责分成三组：

| 文件 | 作用 |
| --- | --- |
| [kernel/register.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.h) | `Pass` 基类的声明：`execute`、`help`、`extra_args`、注册相关字段。 |
| [kernel/register.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc) | `Pass` 的构造（自动入链）、`init_register`、`extra_args` 与 `Pass::call` 的实现。 |
| [passes/cmds/plugin.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/plugin.cc) | 插件加载机制：`load_plugin` 用 `dlopen` 打开 `.so` 并触发注册；`plugin` 命令。 |
| [kernel/driver.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc) | `-m` 命令行选项解析与 `load_plugin` 调用时机。 |
| [misc/yosys-config.in](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/misc/yosys-config.in) | 安装时生成的 `yosys-config` 脚本，把编译插件所需的编译器/标志/库拼出来。 |
| [docs/source/code_examples/extensions/my_cmd.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/code_examples/extensions/my_cmd.cc) | 官方最小示例：`my_cmd` 列出参数与模块。 |
| [docs/source/code_examples/stubnets/stubnets.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/code_examples/stubnets/stubnets.cc) | 官方完整示例：`stubnets` 遍历单元、用 `extra_args` 处理选项与选择。 |
| [kernel/rtlil.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h) | `Design::selected_modules()`、`Module::cells()`、`Cell::type` 等遍历用接口。 |
| [docs/source/yosys_internals/extending_yosys/extensions.rst](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/yosys_internals/extending_yosys/extensions.rst) | 官方「编写扩展」文档。 |

## 4. 核心概念与源码讲解

### 4.1 Pass 骨架：从派生一个类到注册一条命令

#### 4.1.1 概念说明

一条「命令」在 Yosys 内部就是一个 `Pass` 子类的全局静态实例。你只需要做三件事就能发明一条新命令：

1. **派生**：写一个 `struct XxxPass : public Pass`。
2. **实现 `execute`**：这是唯一的纯虚业务方法，签名固定为 `execute(std::vector<std::string> args, RTLIL::Design *design)`。`args` 是命令行被切好的词（`args[0]` 是命令名本身），`design` 是当前整个设计（一个 `RTLIL::Design`，见 u2-l2）。
3. **声明全局实例**：在文件作用域写一个 `} XxxPass;`，让这个对象在程序启动时被构造。

至于「注册」，你**完全不用手写**——构造函数会自动把对象塞进待注册链表，框架稍后会把它搬进 `pass_register`。这正是 u4-l1 讲过的「去中心化注册」。

#### 4.1.2 核心流程

下图是一条自定义 Pass 从「源码」到「可在 shell 调用」的完整生命周期（与内置 pass 完全一致，只是时机不同）：

```text
编写阶段：  struct XxxPass : Pass { 实现 execute / help }  +  全局实例 XxxPass;
            │
            │  程序/插件库加载时，全局对象的构造函数执行
            ▼
入链阶段：  Pass::Pass() 把 this 头插进 first_queued_pass 链表
            │
            │  yosys_setup() 或 load_plugin() 调用 Pass::init_register()
            ▼
登记阶段：  init_register() 遍历链表 → run_register() → pass_register["xxx"] = this
            │
            ▼
就绪：      用户敲 "xxx ..." → Pass::call 查 pass_register → 调 execute(args, design)
```

#### 4.1.3 源码精读

先看 `Pass` 基类里我们要用到的关键成员（[kernel/register.h:61-125](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.h#L61-L125)）：

- 构造函数接收命令名与一句话简介（[register.h:65-66](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.h#L65-L66)）：`Pass(std::string name, std::string short_help = "** document me **", ...)`。`short_help` 会出现在 `help` 命令的总览列表里。
- `help()` 是虚函数（[register.h:71](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.h#L71)）：默认打印「No help message」，你通常要重写它，用 `log()` 一行行排版出用法说明。
- `execute(...)` 是**纯虚**（[register.h:75](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.h#L75)）：`virtual void execute(std::vector<std::string> args, RTLIL::Design *design) = 0;`。这是你必须实现的唯一方法。
- `extra_args`（[register.h:102](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.h#L102)）：统一处理「选项之后的参数」，默认会处理选择（见 4.2）。
- `next_queued_pass` / `init_register`（[register.h:113-116](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.h#L113-L116)）：注册机制的钩子。

再看构造函数如何「自动入链」（[kernel/register.cc:64-71](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L64-L71)）：

```cpp
Pass::Pass(std::string name, std::string short_help, source_location location) :
    pass_name(name), short_help(short_help), location(location)
{
    next_queued_pass = first_queued_pass;   // 头插
    first_queued_pass = this;
    call_counter = 0;
    runtime_ns = 0;
}
```

也就是说，只要存在一个 `XxxPass` 全局对象，它就把自己挂到全局链表 `first_queued_pass` 头部。链表随后由 `init_register` 清空并搬进 `pass_register`（[register.cc:80-90](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L80-L90)）：先逐个 `run_register()` 写进表，再统一回调 `on_register()`。`run_register` 会查重（[register.cc:73-78](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L73-L78)），所以如果你的命令名和已有命令同名会直接 `log_error`（除非重写 `replace_existing_pass()`）。

最小骨架可以直接照抄官方示例 `my_cmd`（[docs/source/code_examples/extensions/my_cmd.cc:7-20](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/code_examples/extensions/my_cmd.cc#L7-L20)）：

```cpp
#include "kernel/yosys.h"

USING_YOSYS_NAMESPACE
PRIVATE_NAMESPACE_BEGIN

struct MyPass : public Pass {
    MyPass() : Pass("my_cmd", "just a simple test") { }
    void execute(std::vector<std::string> args, RTLIL::Design *design) override
    {
        log("Arguments to my_cmd:\n");
        for (auto &arg : args)
            log("  %s\n", arg);

        log("Modules in current design:\n");
        for (auto mod : design->modules())
            log("  %s (%d wires, %d cells)\n", mod,
                    GetSize(mod->wires()), GetSize(mod->cells()));
    }
} MyPass;

PRIVATE_NAMESPACE_END
```

几个要点：

- `#include "kernel/yosys.h"` 把 `Pass`、`RTLIL::*`、`log`、`NEW_ID` 等全都带进来。
- `USING_YOSYS_NAMESPACE` 相当于 `using namespace Yosys;`，这样可以直接写 `Pass`、`RTLIL::Design`。
- `PRIVATE_NAMESPACE_BEGIN ... PRIVATE_NAMESPACE_END` 把你的符号包进一个匿名命名空间，避免和别的插件撞名——这是写插件的推荐做法。
- 末尾的 `} MyPass;` 就是「声明一个名为 `MyPass` 的全局实例」，这一行是注册的真正触发点。

#### 4.1.4 代码实践

**实践目标**：先不动 RTLIL 遍历，单纯验证「我能发明一条命令」。

**操作步骤**：

1. 把上面的 `my_cmd.cc` 存成 `my_cmd.cc`（或直接用仓库里 `docs/source/code_examples/extensions/my_cmd.cc`）。
2. 编译（4.3 节会解释这条命令）：
   ```bash
   yosys-config --build my_cmd.so my_cmd.cc
   ```
3. 加载并运行（`-m` 加载插件，`-p` 跑一条命令，`-Q` 关掉启动横幅）：
   ```bash
   yosys -m ./my_cmd.so -p 'my_cmd foo bar' -Q
   ```

**需要观察的现象**：日志里依次打印出 `Arguments to my_cmd:` 与三个参数（`my_cmd foo bar`），以及 `Modules in current design:`（此时还没读设计，所以下面是空的）。

**预期结果**：与官方文档 [extensions.rst:101-110](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/yosys_internals/extending_yosys/extensions.rst#L101-L110) 给出的示例输出一致。若编译报缺头文件，请确认是在已安装/已构建的 yosys 环境里调用 `yosys-config`。

#### 4.1.5 小练习与答案

**练习 1**：为什么我们不需要在某处「显式调用 `register("my_cmd", ...)`」来登记这条命令？

**参考答案**：因为 `MyPass` 是一个全局静态对象，它的构造函数（`Pass::Pass`）在加载时就把自己头插进 `first_queued_pass` 链表（[register.cc:64-71](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L64-L71)），随后 `init_register()` 把链表搬进 `pass_register`（[register.cc:80-90](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L80-L90)）。注册是「构造的副作用」，所以中心代码无需知道新命令的存在。

**练习 2**：把命令名从 `my_cmd` 改成 `opt` 会怎样？

**参考答案**：`run_register` 会发现 `pass_register` 里已经有 `opt`，而你没有重写 `replace_existing_pass()`（默认返回 `false`），于是触发 `log_error("Unable to register pass 'opt', pass already exists!")`（[register.cc:73-78](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L73-L78)）。可见命令名必须在全局唯一。

### 4.2 遍历 RTLIL：处理选项、选择与 Design/Module/Cell

#### 4.2.1 概念说明

光会打印参数还不够。绝大多数有用的 Pass 都要做两件事：**解析自己的命令行选项**，以及**在选中的模块上遍历网表**。Yosys 为这两件事都提供了标准做法：

- **选项解析**：用 `for` 循环扫 `args`，识别 `-xxx` 选项，遇到不认识的以 `-` 开头的词或非选项词就 `break`，把剩下的交给 `extra_args`。
- **选择（selection）**：命令末尾允许写一个「选择表达式」（如 `cellstats t:$and` 表示「只在类型为 `$and` 的单元上」）。`extra_args` 默认 `select=true`，会把它压入选择栈，于是 `design->selected_modules()` 就只返回被选中的模块。

这样你的 Pass 就天然和 `select`、`cd` 等机制协同——和内置命令完全一样的体验（u4-l3）。

#### 4.2.2 核心流程

一个「遍历型 Pass」的标准骨架是：

```text
1. log_header(...)            打印标题
2. for 扫 args[1..]：         解析自己的 -选项，遇到未知/非选项就 break，记下 argidx
3. extra_args(args, argidx, design)   处理选择（默认压栈）+ 校验非法选项
4. for (mod : design->selected_modules())     只遍历被选中的、非黑盒模块
5.    for (cell : mod->cells()) ... cell->type ...    读单元信息
```

为什么用 `selected_modules()` 而不是 `modules()`？因为前者**自动尊重当前选择**，并默认排除 blackbox（库单元），这通常正是你想要的；后者会无差别返回所有模块（[rtlil.h:1917](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1917)）。`selected_modules` 有一族变体（[rtlil.h:2019-2053](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2019-L2053)），按「是否含黑盒、是否整体选中」区分。

#### 4.2.3 源码精读

`extra_args` 的实现很简洁（[kernel/register.cc:192-208](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L192-L208)）：

```cpp
void Pass::extra_args(std::vector<std::string> args, size_t argidx, RTLIL::Design *design, bool select)
{
    for (; argidx < args.size(); argidx++)
    {
        std::string arg = args[argidx];
        if (arg.compare(0, 1, "-") == 0)
            cmd_error(args, argidx, "Unknown option or option in arguments."); // 不认识的 -选项 → 报错
        if (!select)
            cmd_error(args, argidx, "Extra argument.");
        handle_extra_select_args(this, args, argidx, args.size(), design);     // 把选择压栈
        break;
    }
}
```

它做两件事：把「你的 for 循环没认掉的 `-xxx`」当成语法错误（这能帮你抓到拼写错误的选项），以及把命令尾部的选择表达式交给 `handle_extra_select_args`（声明见 [register.h:185](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.h#L185)）压入选择栈。注意 `Pass::call` 在命令执行完会自动把选择栈深度恢复到执行前（[register.cc:299-304](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L299-L304)），所以选择是「临时覆盖、用完即弹」的，不会污染后续命令。

遍历部分用到的两个接口（[kernel/rtlil.h:2162-2165](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2162-L2165)）：

```cpp
RTLIL::ObjRange<RTLIL::Wire*> wires() { ... }   // 模块内所有线
RTLIL::ObjRange<RTLIL::Cell*> cells() { ... }   // 模块内所有单元
```

`ObjRange` 可以直接被 range-for 使用。而单元的种类就藏在 `Cell::type`（[rtlil.h:2518](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2518)）：`RTLIL::IdString type;`——它可能是一个 `$and`/`$dff`/`$mux` 这样的内部单元，也可能是 `\FOO` 这样被例化的子模块名（详见 u3-l4）。

完整的「真实」范例是官方的 `stubnets`，它的 `execute` 把上面四步全用上了（[docs/source/code_examples/stubnets/stubnets.cc:99-128](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/code_examples/stubnets/stubnets.cc#L99-L128)）：

```cpp
struct StubnetsPass : public Pass {
    StubnetsPass() : Pass("stubnets") { }
    void execute(std::vector<std::string> args, RTLIL::Design *design) override
    {
        bool report_bits = 0;
        log_header(design, "Executing STUBNETS pass (find stub nets).\n");

        size_t argidx;
        for (argidx = 1; argidx < args.size(); argidx++) {     // ① 解析自己的选项
            std::string arg = args[argidx];
            if (arg == "-report_bits") { report_bits = true; continue; }
            break;
        }
        extra_args(args, argidx, design);                       // ② 处理选择 + 报错

        for (auto &it : design->modules_)                       // ③ 遍历被选中的模块
            if (design->selected_module(it.first))
                find_stub_nets(design, it.second, report_bits);
    }
} StubnetsPass;
```

> 说明：`stubnets` 直接遍历 `design->modules_` 并用 `selected_module(name)` 判断，等价于遍历 `selected_modules()`。注意它还演示了「遍历单元的连接」：`for (auto &conn : cell_iter.second->connections())`（[stubnets.cc:33-44](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/code_examples/stubnets/stubnets.cc#L33-L44)），并用 `SigMap` 把别名信号归一化（u3-l2 讲过 `SigMap` 的原理，此处不展开）。

#### 4.2.4 代码实践

**实践目标**：写一个 `cellstats`，统计当前设计中每种 `$` 单元的数量，并支持用选择缩小范围。

**操作步骤**：新建 `cellstats.cc`（**示例代码**，不在仓库中）：

```cpp
#include "kernel/yosys.h"

USING_YOSYS_NAMESPACE
PRIVATE_NAMESPACE_BEGIN

struct CellstatsPass : public Pass {
    CellstatsPass() : Pass("cellstats", "count cells by type") { }

    void help() override
    {
        log("\n");
        log("    cellstats [selection]\n");
        log("\n");
        log("Print the number of cells of each type in the selected modules.\n");
        log("\n");
    }

    void execute(std::vector<std::string> args, RTLIL::Design *design) override
    {
        log_header(design, "Executing CELLSTATS pass.\n");

        size_t argidx;
        for (argidx = 1; argidx < args.size(); argidx++) {
            std::string arg = args[argidx];
            if (arg == "-h" || arg == "-help") { help(); return; }  // 自己处理 -help
            break;
        }
        extra_args(args, argidx, design);   // 处理尾部选择

        std::map<RTLIL::IdString, int> cell_count;   // 按类型计数
        for (auto mod : design->selected_modules())
            for (auto cell : mod->cells())
                cell_count[cell->type]++;

        log("Number of cells by type:\n");
        for (auto &it : cell_count)
            log("  %-20s %d\n", log_id(it.first), it.second);
    }
} CellstatsPass;

PRIVATE_NAMESPACE_END
```

编译、加载、运行（命令含义见 4.3 节）：

```bash
yosys-config --build cellstats.so cellstats.cc
yos -p 'read_verilog docs/source/code_examples/stubnets/test.v; hierarchy; proc; opt' \
     -m ./cellstats.so -p 'cellstats' -Q
```

（`yos` 是 `yosys` 的常用别名；若你的环境只有 `yosys`，把 `yos` 换成 `yosys`。）

**需要观察的现象**：日志里 `Number of cells by type:` 下面列出综合后每种内部单元（如 `$and`、`$xor`、`$reduce_or`、`$adff` 等）及其数量。再试一次带选择：`cellstats t:$and`，应只统计被选中范围内的单元。

**预期结果**：计数随设计规模与综合阶段变化；带上选择后数字应变小或部分类型消失。具体数值「待本地验证」（取决于该设计综合出的网表）。

#### 4.2.5 小练习与答案

**练习 1**：如果用户敲了 `cellstats -nosuchoption`，会发生什么？为什么这是好事？

**参考答案**：你的 for 循环不认识 `-nosuchoption`，于是 `break`（此时 `argidx` 还停在该选项上）；随后 `extra_args` 发现一个以 `-` 开头且未被消费的词，调用 `cmd_error` 报「Unknown option or option in arguments」（[register.cc:198-199](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L198-L199)）。好处是：拼写错误的选项不会「静默成功」，避免用户以为选项生效了。

**练习 2**：把 `selected_modules()` 换成 `modules()` 会对结果有什么影响？

**参考答案**：`modules()` 会返回**所有**模块，包括综合过程中残留的或库 blackbox 模块，且忽略当前选择；`selected_modules()` 只返回被选中、非黑盒的模块。换用前者后，`cellstats t:$and` 这种「带选择」的调用会失效（因为不再尊重选择栈），且可能多统计黑盒/抽象模块里的内容。

### 4.3 编译与加载插件：yosys-config 与 -m

#### 4.3.1 概念说明

写完 `.cc` 还不能直接用，它要被编译成一份**和 yosys 主程序 ABI 兼容的共享库 `.so`**，再被主程序在运行时加载。这里的难点是「编译标志必须和构建 yosys 时一模一样」（C++20、相同的符号可见性、链接到同一份 `libyosys` 等）。手写这条编译命令很容易出错，所以 yosys 提供了一个脚本 `yosys-config`：它知道自己被构建时用的是什么编译器和标志，能把它们原样吐给你。

加载则由命令行 `-m`（或 shell 内的 `plugin -i`）触发，内部用 `dlopen` 打开 `.so`。关键点：**加载 `.so` 会触发其中所有全局对象的构造**——也就是说，你的 `CellstatsPass` 实例就是在 `dlopen` 时被构造并进入 `first_queued_pass` 链表的，随后 yosys 再调一次 `Pass::init_register()` 把它登记进 `pass_register`，命令就「上线」了。

> 启用插件支持需要构建时开启 `YOSYS_ENABLE_PLUGINS`（默认开启）。若关闭，`load_plugin` 会直接 `log_error`（[plugin.cc:137-146](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/plugin.cc#L137-L146)）。

#### 4.3.2 核心流程

```text
用户:  yosys -m ./cellstats.so -p 'cellstats'
        │
        │  driver.cc 解析 -m，把文件名存进 plugin_filenames
        │  yosys_setup() 之后，逐个 load_plugin(fn, {})
        ▼
load_plugin:  dlopen("./cellstats.so")  →  触发 CellstatsPass 全局对象构造 → 入链
        │
        │  Pass::init_register() 把链表搬进 pass_register
        ▼
就绪:  -p 'cellstats' → Pass::call → 查表命中 → execute(args, design)
```

#### 4.3.3 源码精读

先看 `yosys-config`。它是一个 bash 脚本模板（安装时 `@CXX@` 等占位符被替换成真实值）。它的工作方式是「把特殊参数替换成真实值，其余原样透传」（[misc/yosys-config.in:52-111](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/misc/yosys-config.in#L52-L111)）。最常用的两个用法：

- `--build modname.so cppsources..`：一键编译插件。它等价于把 `--exec --cxx --cxxflags --ldflags -o modname -shared sources --libs` 拼好并直接执行（[yosys-config.in:47-50](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/misc/yosys-config.in#L47-L50)）。
- 手动拼参数：`yosys-config --cxx --cxxflags --ldflags -o x.so -shared x.cc --ldlibs`（[yosys-config.in:21-27](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/misc/yosys-config.in#L21-L27) 的示例）。

`--exec` 表示「不要打印，直接 `exec` 执行拼好的命令」（[yosys-config.in:107-109](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/misc/yosys-config.in#L107-L109)）。`--cxx`/`--cxxflags`/`--ldflags`/`--libs` 分别替换成编译器、编译选项、链接选项、要链接的库（[yosys-config.in:63-75](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/misc/yosys-config.in#L63-L75)）。官方 `stubnets` 的 Makefile 就是这么调的（[docs/source/code_examples/stubnets/Makefile:14-15](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/code_examples/stubnets/Makefile#L14-L15)）：

```makefile
stubnets.so: stubnets.cc
	@$(YOSYS_CONFIG) --exec --cxx --cxxflags --ldflags -o $@ -shared $^ --ldlibs >/dev/null 2>&1
```

再看 driver 如何处理 `-m`。命令行定义里 `-m`/`--plugin` 接收一个可重复的 `<plugin>` 值（[kernel/driver.cc:164-165](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L164-L165)），解析后存进 `plugin_filenames`（[driver.cc:266](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L266)）。加载时机很关键：它在 `yosys_setup()` **之后**、读任何前端文件 **之前**（[driver.cc:449-458](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L449-L458)）：

```cpp
yosys_setup();
...
for (auto &fn : plugin_filenames)
    load_plugin(fn, {});
```

所以插件命令在脚本一开始就可用了。

最后看 `load_plugin` 本身（[passes/cmds/plugin.cc:64-136](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/plugin.cc#L64-L136)）。核心几行：

```cpp
void *hdl = dlopen(filename.c_str(), RTLD_LAZY|RTLD_LOCAL);   // 打开 .so（plugin.cc:108）
// 若带路径找不到，且文件名不含 /，则到搜索路径里找（plugin.cc:112-124）
if (hdl == nullptr)
    log_cmd_error("Can't load module `%s': %s\n", filename, dlerror());
loaded_plugins[orig_filename] = hdl;
Pass::init_register();   // 关键：把刚构造出的全局对象登记成命令（plugin.cc:130）
```

搜索路径由 `get_plugin_search_paths()` 给出（[plugin.cc:47-62](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/plugin.cc#L47-L62)）：先是环境变量 `YOSYS_PLUGIN_PATH`（按 `:`/`;` 分隔的若干目录），最后是 `<share dir>/plugins`。所以你也可以把 `.so` 放进这些目录，然后直接 `-m cellstats.so`（不带路径）。

加载后还能用内置的 `plugin` 命令查看与加载（[passes/cmds/plugin.cc:148-231](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/plugin.cc#L148-L231)）：`plugin -l` 列出已加载插件，`plugin -i xxx.so` 加载，`-a 别名` 注册别名。

> 除了 `-m`，在脚本里也可以用 `plugin -i ./cellstats.so` 动态加载（[plugin.cc:198-199](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/cmds/plugin.cc#L198-L199)），效果相同。

#### 4.3.4 代码实践

**实践目标**：把 4.2 写的 `cellstats.so` 真正跑起来，并验证插件已注册。

**操作步骤**：

1. 编译：
   ```bash
   yosys-config --build cellstats.so cellstats.cc
   ```
2. 用 `-m` 加载并在交互 shell 里验证：
   ```bash
   yosys -m ./cellstats.so
   ```
   进入 `yosys>` 后，先 `plugin -l` 应能看到 `./cellstats.so`；再 `help cellstats` 应打印你在 `help()` 里写的用法。
3. 也可以脱离 shell，一行命令跑完：
   ```bash
   yosys -m ./cellstats.so -p 'read_verilog docs/source/code_examples/stubnets/test.v; hierarchy; proc; opt; cellstats' -Q
   ```

**需要观察的现象**：

- `plugin -l` 列出 `./cellstats.so`。
- `help`（无参数总览）里能看到 `cellstats` 这一行，简述是 `count cells by type`。
- `cellstats` 实际打印出按类型计数的表。

**预期结果**：插件加载后，`cellstats` 与内置命令无差别地出现在命令表里；若 `dlopen` 失败，会得到 `Can't load module ...` 报错（多半是编译器/标志与 yosys 不匹配，请确保用同一份 `yosys-config`）。实际统计数字「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么必须在 `yosys_setup()` **之后**加载插件？如果在之前加载会怎样？

**参考答案**：`yosys_setup()` 负责初始化 `IdString` 内部化表、创建全局 `yosys_design`、并把内置命令登记进 `pass_register`（u1-l2、u4-l1）。`load_plugin` 里插件的全局对象构造会用到这些已初始化的全局设施（比如构造 `Pass` 时操作 `first_queued_pass`），并随后调用 `Pass::init_register()`。若在 `setup` 之前加载，运行时尚未就绪，行为未定义甚至崩溃。driver 把 `load_plugin` 排在 `yosys_setup()` 之后（[driver.cc:449-458](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L449-L458)）正是为此。

**练习 2**：`yosys-config --build cellstats.so cellstats.cc` 这条命令最终执行了什么？

**参考答案**：脚本把 `--build modname src..` 重写为 `--exec --cxx --cxxflags --ldflags -o modname -shared src.. --libs`（[yosys-config.in:47-50](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/misc/yosys-config.in#L47-L50)），再把 `--cxx` 等替换成真实编译器与标志（[yosys-config.in:63-75](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/misc/yosys-config.in#L63-L75)），最后因 `--exec` 直接执行这条 `g++/clang++ ... -shared -o cellstats.so cellstats.cc ...` 命令。换句话说，它替你拼好了一条「与构建 yosys 完全一致」的共享库编译命令。

## 5. 综合实践

把三个最小模块串起来，做一个带选项、带选择、可加载的小工具。

**任务**：扩展 4.2 的 `cellstats`，新增一个 `-top` 选项，只统计 `design->top_module()`（若存在）里的单元；不指定 `-top` 时维持原行为（统计所有被选中模块）。然后：

1. 用 `yosys-config --build` 编译成 `cellstats.so`。
2. 用 `yosys -m ./cellstats.so` 加载。
3. 对 `docs/source/code_examples/stubnets/test.v` 跑 `read_verilog → hierarchy -top uut → proc → opt`。
4. 分别运行 `cellstats` 与 `cellstats -top`，对比两者输出差异。

**提示**：

- 判断 top 模块：`RTLIL::Module *top = design->top_module();`（返回 `nullptr` 表示无法确定，见 u2-l2）。
- 解析 `-top` 时记得 `argidx` 要前移一位（`-top` 后面跟一个值）。
- 加载后可用 `help cellstats` 检查你的 `help()` 文本是否更新了 `-top` 说明。
- 现象上，`-top` 模式应只统计顶层模块 `uut` 的单元；不带 `-top`（且无选择）则统计所有非黑盒模块。具体计数「待本地验证」。

这个任务同时覆盖了「Pass 骨架（构造/help/execute）」「遍历 RTLIL（选项 + 选择 + cells）」「编译与加载（yosys-config + -m）」三个最小模块。

## 6. 本讲小结

- 一条 Yosys 命令 = 一个 `Pass` 子类的全局静态实例；你只需派生、实现纯虚 `execute(args, design)`、声明全局对象，**注册全自动完成**（构造时入 `first_queued_pass` 链表，`init_register` 搬进 `pass_register`）。
- 选项解析用「for 扫 args + break + `extra_args`」的固定模式：`extra_args` 既帮你把拼写错误的 `-选项` 报成语法错，又把命令尾部的选择表达式压入选择栈。
- 遍历网表的标准入口是 `design->selected_modules()`（尊重选择、默认排除 blackbox），再 `mod->cells()` 取单元，`cell->type` 取种类；输出统一走 `log()` / `log_id()` / `log_header()`。
- 用 `yosys-config --build x.so x.cc` 一键编译插件（它替你拼好与 yosys 一致的编译命令）；用 `yosys -m x.so` 或脚本内 `plugin -i` 加载。
- 加载发生在 `yosys_setup()` 之后：`load_plugin` 用 `dlopen` 打开 `.so`，触发其中全局对象构造，再调 `Pass::init_register()` 让命令上线——与内置命令地位完全平等。

## 7. 下一步学习建议

- 想让 Pass **修改**网表（增删单元/连线）而非只读统计：复习 u3-l1 的 `addWire/addCell/connect/fixup_ports` 与 `setPort`，并阅读官方文档里「Modifying modules」一节（[extensions.rst:142-158](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/yosys_internals/extending_yosys/extensions.rst#L142-L158)）给出的 DO/DON'T（例如「不要主动删线，断开即可，交给后续 `clean`」）。
- 想处理信号别名：深入 u3-l2 的 `SigMap`，参考 `stubnets.cc` 里 `SigMap sigmap(module)` 的用法。
- 想把 Yosys 当作**库**嵌入自己的 C++ 程序（而不是写插件）：进入 u9-l2「C++ API：把 Yosys 作为库嵌入」，它讲解 `libyosys` 与 `run_pass()`/`run_frontend()`/`run_backend()` 的库接口。
- 想用 Python 驱动或写 Pass：进入 u9-l3「Python 绑定：pyosys」。
- 想把 Pass 直接**编进** yosys 主程序（而非插件）：在 `passes/` 下新建目录、加 `CMakeLists.txt` 并 `add_subdirectory`，全局对象同样会被 `init_register` 收录（回顾 u1-l3 的目录分层与 u4-l1 的注册机制）。
