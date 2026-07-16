# VPR 命令行与参数体系

## 1. 本讲目标

在前一讲（u1-l4）中，我们用 `run_vtr_flow.py` 一行命令跑通了整条 VTR 流水线。那是一条「全自动」的快车道：脚本替我们拼好了 VPR 的命令行。但在真实工程里，你常常需要**单独调用 `vpr`**，只跑某一阶段、固定一个通道宽度、或打开图形界面调试。

本讲的目标是让你看懂 VPR 自己的命令行是怎么一回事。读完本讲你应当能够：

- 说清 `vpr` 可执行程序的入口在哪里、启动后依次做了哪几件事；
- 看懂 `t_options` 这个「参数总仓库」是怎么按阶段分组组织的；
- 理解 VPR 用 `libargparse` 注册命令行选项的方式，以及「条件默认值（Provenance）」这套精巧机制；
- 掌握 `e_stage_action`（DO / LOAD / SKIP / SKIP_IF_PRIOR_FAIL）四态枚举，明白为什么 `--pack`、`--place`、`--route` 这些布尔开关最终会被翻译成「运行 / 加载 / 跳过」。

本讲只讲「参数怎么进来、怎么被组织」，**不**展开各阶段算法本身——那是第 4～7 单元的事。

## 2. 前置知识

- **命令行参数（argv）**：C/C++ 程序的 `main(int argc, const char** argv)` 会收到命令行单词列表。`argc` 是单词个数，`argv[0]` 通常是程序名，`argv[1..]` 是真正的参数。
- **FPGA CAD 阶段**：回顾 u1-l1/u1-l4，VPR 内部数据流是 `AtomNetlist → 打包 → 布局 → 布线 → 时序分析`。每个箭头对应一个「阶段（stage）」。
- **通道宽度（channel width）**：FPGA 布线通道里每段能容纳的线数。VPR 默认会用二分搜索找「能布通的最小通道宽度」（见 u1-l4）。
- **类型安全的 ID（StrongId）**：`argparse::ArgValue<T>` 用模板把「一个命令行值」和「它的来源」绑在一起，避免把字符串当成数字乱用。这是 u3 单元 StrongId 思想的同源小应用。
- **libargparse**：VTR 自带的外部子树库，位于 `libs/EXTERNAL/libargparse/`，提供类似 Python `argparse` 的 C++ 命令行解析。注意它是**外部代码**，不能在 VTR 树里直接改（见 u1-l3）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [vpr/src/main.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/main.cpp) | `vpr` 程序入口，`main()` 在这里，负责初始化、跑流程、收尾与异常处理。 |
| [vpr/src/base/read_options.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.h) | 声明「参数总仓库」`struct t_options`，以及 `read_options`、`create_arg_parser` 等函数。 |
| [vpr/src/base/read_options.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp) | 参数解析的全部实现：构造解析器、注册每个 `--xxx` 选项、设置条件默认值、校验。 |
| [vpr/src/base/vpr_types.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h) | VPR 核心数据结构集中地，本讲关注其中的 `e_stage_action` 阶段动作枚举。 |
| [vpr/src/base/setup_vpr.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_vpr.cpp) | 把布尔开关 `do_packing`/`do_placement`/... 翻译成 `e_stage_action` 的关键逻辑。 |
| [vpr/src/base/vpr_api.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp) | `vpr_init` 在此调用 `read_options`；各阶段流程也在此按 `e_stage_action` 分派。 |
| [libs/EXTERNAL/libargparse/src/argparse_value.hpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/EXTERNAL/libargparse/src/argparse_value.hpp) | 定义 `ArgValue<T>` 与「来源（Provenance）」枚举，是条件默认值的基石。 |

## 4. 核心概念与源码讲解

### 4.1 main.cpp 顶层流程：vpr 启动后做了什么

#### 4.1.1 概念说明

一个 CAD 工具的 `main()` 通常只做三件事：**读入配置 → 执行核心流程 → 收尾**。VPR 的 `main()` 就是这种「瘦入口」典范——它本身几乎不含业务逻辑，真正的活都委托给 `vpr_api` 里的函数。理解这一点很重要：以后想改 VPR 行为，几乎不会动 `main.cpp`，而是动 `vpr_api.cpp` 或具体阶段代码。

`main.cpp` 文件头注释还贴心地列出了「理解 VPR 的关键文件」，把 `physical_types.h`、`vpr_types.h`、`globals.h` 标为重点——这恰好对应本手册第 2、3 单元。

#### 4.1.2 核心流程

`main()` 的执行顺序可以画成：

```
main(argc, argv)
  │
  ├─ 声明三个「全局容器」：t_options / t_arch / t_vpr_setup
  │
  ├─ vpr_install_signal_handler()        # 注册 Ctrl-C 等信号处理
  │
  ├─ vpr_init(argc, argv, &Options, &vpr_setup, &Arch)
  │      └─ 内部调用 read_options(argc, argv)  ← 命令行在这里被解析！
  │
  ├─ 若 --version  → 打印版本后退出
  ├─ 若 --show_arch_resources → 打印架构资源后退出
  │
  ├─ flow_succeeded = vpr_flow(vpr_setup, Arch)   # 打包/布局/布线/分析全在这
  │
  ├─ print_timing_stats(...)             # 打印时序统计
  ├─ vpr_free_all(Arch, vpr_setup)       # 释放内存
  │
  └─ 三层 try/catch 兜底异常：tatum::Error / VprError / vtr::VtrError
```

注意 `main()` 顶部的 `vtr::ScopedFinishTimer t("The entire flow of VPR")`：这是一个 RAII 计时器，程序结束时自动打印「整个 VPR 流程耗时」。VPR 里到处都是这种用作用域自动计时的写法。

#### 4.1.3 源码精读

入口与三件套容器声明：

[vpr/src/main.cpp:L44-L55](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/main.cpp#L44-L55) —— 这是 `main()` 的开头：先建一个总计时器，再声明 `t_options Options`、`t_arch Arch`、`t_vpr_setup vpr_setup` 三个对象。命令行解析发生在随后的 `vpr_init(...)` 里。

[vpr/src/main.cpp:L54-L72](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/main.cpp#L54-L72) —— 先 `vpr_init`（含解析参数），再处理两个「打印即退出」的开关 `show_version` / `show_arch_resources`，最后才调用 `vpr_flow(...)` 真正干活。`vpr_flow` 返回 `bool` 表示是否成功实现。

[vpr/src/main.cpp:L82-L102](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/main.cpp#L82-L102) —— 三层异常捕获：Tatum（时序库）错误、VPR 自身 `VprError`、底层 `vtr::VtrError`。每层都先打印错误、调用 `vpr_free_all` 清理，再返回不同的退出码（`ERROR_EXIT_CODE` / `INTERRUPTED_EXIT_CODE`）。这正是 u1-l1 所说「VPR 行为反常先看错误来源」的代码落点。

把 `main → vpr_init → read_options` 这条链补全：[vpr/src/base/vpr_api.cpp:L185-L192](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L185-L192) —— `vpr_init` 先初始化日志，然后在第 192 行 `*options = read_options(argc, argv);` 把命令行解析结果写回 `main()` 的 `Options`。

#### 4.1.4 代码实践

1. **目标**：在不编译运行的前提下，徒手画出 `vpr` 启动到退出的调用链。
2. **步骤**：打开 `main.cpp`，依次标记 `vpr_init`、`vpr_flow`、`vpr_free_all` 三个调用点；再打开 `vpr_api.cpp` 确认 `vpr_init` 内部确实调用了 `read_options`。
3. **观察**：注意 `main()` 里没有任何 `if (argc > 1)` 之类的手写参数判断——全部交给 `read_options`。
4. **预期结果**：你应得到一条 `main → vpr_init → read_options → (返回) → vpr_flow → vpr_free_all` 的纵向链。
5. 待本地验证：若你已按 u1-l2 构建，可用 `./build/vpr/vpr --version` 触发 `show_version` 分支，观察它是否在 `vpr_flow` 之前就退出。

#### 4.1.5 小练习与答案

- **练习 1**：`main()` 为什么要把 `t_arch Arch` 和 `t_vpr_setup vpr_setup` 都在 `main()` 里声明，而不是藏在 `vpr_flow` 内部？
  - **答案**：因为它们是跨阶段共享的「全局状态载体」，需要在初始化、流程、收尾、异常清理四个环节都被访问；放在 `main()` 作用域里，才能保证哪怕抛异常时 `catch` 块也能调用 `vpr_free_all(Arch, vpr_setup)` 正确释放。这是 VPR 用「显式传递状态」而非「隐式全局变量」的设计（更现代的全局化方案见 u3-4 的 `g_vpr_ctx`）。
- **练习 2**：`vpr --version` 为什么能不运行打包/布局/布线就退出？
  - **答案**：`main.cpp` 在调用 `vpr_flow` **之前**就检查了 `Options.show_version`，命中后直接 `vpr_free_all` 并 `return SUCCESS_EXIT_CODE`，根本走不到 `vpr_flow`。

### 4.2 t_options：参数的总仓库（字段分类）

#### 4.2.1 概念说明

`vpr` 有几百个命令行选项，如果把它们散落在各处代码里会无法维护。VPR 的做法是定义一个**巨大的聚合结构体 `t_options`**，每个命令行选项对应它的一个字段。解析完成后，这个结构体就像一张「填好的订单」被传递给后续阶段。

关键设计：每个字段不是裸的 `int`/`std::string`，而是 `argparse::ArgValue<T>`——一个同时记录「值」和「这个值从哪来」的包装类型。这一点我们在 4.3 节展开。

#### 4.2.2 核心流程

`t_options` 的字段在 `read_options.h` 里**按用途分组**排列，每组前面有注释。整体可归为七大类：

| 分组 | 注释关键词 | 典型字段 | 例子 |
| --- | --- | --- | --- |
| 文件名 | `// File names` | 输入/输出文件路径 | `ArchFile`、`CircuitName`、`NetFile`、`PlaceFile`、`RouteFile`、`SDCFile` |
| 阶段开关 | `// Stage Options` | 决定跑哪些阶段 | `do_packing`、`do_placement`、`do_routing`、`do_analysis` |
| 图形 | `// Graphics Options` | 可视化 | `show_graphics`、`save_graphics` |
| 通用 | `// General options` | 全局行为 | `num_workers`、`timing_analysis`、`device_layout` |
| 打包/聚类 | `// Clustering options` | 打包参数 | `timing_driven_clustering`、`cluster_seed_type` |
| 布局 | `// Placement options` | 模拟退火参数 | `place_algorithm`、`place_init_t`、`seed` |
| 布线 | `// Router Options` | 布线参数 | `RouteType`、`RouteChanWidth`、`RouterAlgorithm` |

此外还有 NoC、功耗、分析（Analysis）、Server、原子网表（Atom netlist）等更细的分组。本讲练习要求你重点看**打包、布局、布线**三组。

#### 4.2.3 源码精读

结构体起点与文件名字段：

[vpr/src/base/read_options.h:L11-L23](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.h#L11-L23) —— `struct t_options` 开始，`// File names` 段列出所有输入输出文件：架构 `ArchFile`、电路 `CircuitName`、以及 `.net`/`.place`/`.route` 三个实现文件对应的 `NetFile`/`PlaceFile`/`RouteFile`（呼应 u1-l4 产出的三个文件）。

阶段开关组（本讲核心之一）：

[vpr/src/base/read_options.h:L53-L60](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.h#L53-L60) —— `// Stage Options` 段：`do_packing`、`do_legalize`、`do_placement`、`do_analytical_placement`、`do_routing`、`do_analysis`、`do_power` 全是 `ArgValue<bool>`。它们对应命令行的 `--pack`/`--place`/`--route`/`--analysis` 等开关。注意这些是**布尔**，但 4.4 节会看到它们会被进一步翻译成更细的 `e_stage_action`。

布线组的关键三字段：

[vpr/src/base/read_options.h:L229-L242](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.h#L229-L242) —— `// Router Options` 段开头，`RouteType`（global/detailed）、`RouteChanWidth`（通道宽度，默认 -1 表示自动搜索）、`RouterAlgorithm`（路由算法）都在这里。`RouteChanWidth` 默认 `-1` 正是 u1-l4「默认二分搜索最小通道宽度」的源头。

文件末尾的四个对外函数声明：

[vpr/src/base/read_options.h:L320-L323](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.h#L320-L323) —— 声明了 `create_arg_parser`（构造解析器并注册选项）、`read_options`（解析入口）、`set_conditional_defaults`（条件默认值）、`verify_args`（校验）。这四个函数构成参数处理的完整流水线。

#### 4.2.4 代码实践

1. **目标**：从源码中摘出打包/布局/布线三组最具代表性的参数。
2. **步骤**：在 `read_options.h` 里定位三段注释 `// Clustering options`、`// Placement options`、`// Router Options`，分别挑出最能代表该阶段的 2 个字段。
3. **观察**：注意每个字段的模板参数 `ArgValue<T>` 中 `T` 的不同——`bool`、`int`、`float`、`std::string`，还有自定义枚举如 `e_place_algorithm`、`e_route_type`。
4. **预期结果**：例如打包组挑 `timing_driven_clustering`(bool) + `cluster_seed_type`(枚举)；布局组挑 `place_algorithm`(枚举) + `seed`(int)；布线组挑 `RouteChanWidth`(int) + `RouterAlgorithm`(枚举)。
5. 待本地验证：若本地已构建，运行 `./build/vpr/vpr --help`，把上面六个字段名（加 `--` 前缀）在帮助文本里逐一找到。

#### 4.2.5 小练习与答案

- **练习 1**：`t_options` 里为什么用 `ArgValue<int> RouteChanWidth` 而不是直接 `int RouteChanWidth`？
  - **答案**：`ArgValue<T>` 除了存值，还存「值的来源（Provenance）」——用户是否在命令行显式指定过。这决定 `set_conditional_defaults` 能否安全地给它推断一个默认值（见 4.3.3）。裸 `int` 无法区分「用户写了 0」和「根本没写」。
- **练习 2**：`do_power` 字段属于哪一组？它和 `--analysis` 是什么关系？
  - **答案**：`do_power` 在 `// Stage Options` 段（read_options.h:60）。它是一个独立开关，用来启用功耗估算；和 `do_analysis`（时序分析）并列，但功耗估算还需要活动性文件（`ActFile`）和工艺文件（`CmosTechFile`），见 u8-3。

### 4.3 argparse 注册与条件默认值（Provenance）

#### 4.3.1 概念说明

光有 `t_options` 结构体还不够——必须有人告诉解析器「`--pack` 这个词对应 `do_packing` 字段、是布尔、默认 off、出现就置真」。这个「登记」工作在 `create_arg_parser()` 里完成，VPR 用的是自带的外部库 `libargparse`，风格类似 Python 的 `argparse`：**每个选项注册到一个「参数组（argument group）」**，并链式设置 `.help()` / `.default_value()` / `.choices()` / `.action()`。

VPR 命令行有一个特别聪明的设计叫 **Provenance（来源追溯）**：每个 `ArgValue` 不仅记住自己的值，还记住这值是怎么来的——`UNSPECIFIED`（默认构造）、`DEFAULT`（注册时给的默认值）、`SPECIFIED`（用户在命令行写了）、`INFERRED`（程序根据别的参数推断出来的）。有了它，VPR 才能做到「用户没指定 `.net` 文件名？那我用电路名自动拼一个」这种条件默认值，而不会覆盖用户显式给的值。

#### 4.3.2 核心流程

参数从命令行到 `t_options` 的完整四步：

```
read_options(argc, argv)
  ├─ 1. create_arg_parser(prog_name, args)   # 建解析器、把每个 --xxx 绑到字段
  ├─ 2. parser.parse_args(argc, argv)         # 真正切分 argv，填值 + 标 SPECIFIED
  ├─ 3. set_conditional_defaults(args)        # 按 Provenance 补 INFERRED 默认值
  └─ 4. verify_args(args)                     # 校验互斥/范围
```

对布尔开关 `--pack`/`--place` 等，VPR 不直接接受 `true/false`，而是用 `ParseOnOff` 转换器接受 `on/off`，再配 `.action(STORE_TRUE)` 让「只要出现就为真」。

#### 4.3.3 源码精读

解析四步主函数：

[vpr/src/base/read_options.cpp:L19-L31](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L19-L31) —— `read_options()` 正是上面四步的直译：建 `t_options` → `create_arg_parser` → `parse_args` → `set_conditional_defaults` → `verify_args` → 返回。

on/off 转换器：

[vpr/src/base/read_options.cpp:L33-L58](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L33-L58) —— `ParseOnOff` 把字符串 `on`/`off` 转成 `bool`，非法值报错。所有 `<bool, ParseOnOff>` 类型的选项（如 `--timing_analysis on`）都走它。

构造解析器与用法示例：

[vpr/src/base/read_options.cpp:L1607-L1640](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L1607-L1640) —— `create_arg_parser` 先写一段 `description`（解释 VPR 干什么）和 `epilog`（用法示例，如「固定通道宽度 100：`--route_chan_width 100`」「只做打包布局：`--pack --place`」）。这段 epilog 就是 `vpr --help` 最末尾的示例。

位置参数（必填）：

[vpr/src/base/read_options.cpp:L1642-L1650](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L1642-L1650) —— 「positional arguments」组注册两个必填位置参数：`architecture`（绑到 `ArchFile`）和 `circuit`（绑到 `CircuitName`）。这就是为什么 `vpr my_arch.xml my_circuit.blif` 里两个文件不带 `--` 前缀。

阶段开关注册：

[vpr/src/base/read_options.cpp:L1654-L1677](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L1654-L1677) —— `--pack`、`--place`、`--route`、`--analysis` 等被注册成 `STORE_TRUE` 动作、默认 `off`。注意链式调用 `.help(...).action(...).default_value("off")` 的 libargparse 风格。

布线两个标志位参数：

[vpr/src/base/read_options.cpp:L3055-L3066](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L3055-L3066) —— `--route_type`（默认 `detailed`，可选 `global`/`detailed`）与 `--route_chan_width`（默认 `-1`，即自动搜索最小通道宽度）。后者正是 u1-l4「默认跑两次布线」的根源。

Provenance 枚举本体：

[libs/EXTERNAL/libargparse/src/argparse_value.hpp:L28-L33](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/EXTERNAL/libargparse/src/argparse_value.hpp#L28-L33) —— 四种来源：`UNSPECIFIED`/`DEFAULT`/`SPECIFIED`/`INFERRED`。这是条件默认值能「不覆盖用户输入」的根本依据。

条件默认值的真实用法：

[vpr/src/base/read_options.cpp:L3798-L3849](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L3798-L3849) —— `set_conditional_defaults` 开头：对每个文件名字段，**仅当它 `!= Provenance::SPECIFIED`**（用户没显式给）时，才用电路名拼出默认的 `.net`/`.place`/`.route`/`.sdc` 等文件名，并标为 `INFERRED`。比如没给 `NetFile` 就用 `<电路名>.net`。这段完美演示了 Provenance 的价值。

#### 4.3.4 代码实践

1. **目标**：验证「用户未指定时，文件名会被自动推断」这一行为。
2. **步骤**：阅读 `set_conditional_defaults`（read_options.cpp:3798 起），跟踪 `args.NetFile` 是如何从 `CircuitName` + `out_file_prefix` 拼出来的。
3. **观察**：注意每个 `if (... .provenance() != Provenance::SPECIFIED)` 守卫——它保证了即使用户给的电路名恰好等于某个默认名，也不会被错误覆盖。
4. **预期结果**：你能用一句话描述「`.place` 文件名的三种可能来源」：用户 `--place_file X`（SPECIFIED）→ 取 X；否则用 `out_file_prefix + 电路名 + ".place"`（INFERRED）；若连电路名都没有则报错。
5. 待本地验证：本地构建后跑 `./build/vpr/vpr <arch> <circuit> --pack --place`，在日志里查找 VPR 打印的 `.net`/`.place` 文件名，确认与推断规则一致。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `set_conditional_defaults` 要逐个字段检查 `provenance() != SPECIFIED`，而不是无条件覆盖？
  - **答案**：为了尊重用户显式输入。如果无条件覆盖，用户用 `--net_file special.net` 指定的文件名会被推断逻辑改回 `<电路名>.net`，导致读错文件。Provenance 让「自动推断」只在「用户没说」时介入。
- **练习 2**：`--num_workers` 和 `-j` 是什么关系？看哪一行代码？
  - **答案**：它们是同一个选项的「长/短」两个名字。见 [read_options.cpp:L1824](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L1824) `gen_grp.add_argument<size_t>(args.num_workers, "--num_workers", "-j")`，一个字段绑了两个名字。

### 4.4 阶段动作枚举 e_stage_action：DO / LOAD / SKIP

#### 4.4.1 概念说明

这是本讲最巧妙的设计。命令行上的 `--pack`/`--place`/`--route` 看起来只是「做 / 不做」的二元开关，但 VPR 内部对每个阶段其实有**四种**动作：

- **DO**：从头运行这个阶段（真正去算）。
- **LOAD**：不计算，直接从磁盘加载该阶段已有的结果文件（例如加载现成的 `.place` 接着布线）。
- **SKIP**：既不算也不加载，完全跳过。
- **SKIP_IF_PRIOR_FAIL**：仅当上游成功时才继续（用于分析阶段——布线失败就没必要做时序分析）。

为什么需要这四态？因为 VPR 支持「从中间阶段接着跑」：你只想重新布线，就不该重跑打包布局，而应 `LOAD` 已有的 `.net`/`.place`。`e_stage_action` 就是把「用户给的简单布尔开关」翻译成「每阶段的精细动作」的桥梁。

#### 4.4.2 核心流程

翻译发生在 `setup_vpr.cpp` 的 `setup_vpr` 中，规则是「**运行到指定阶段为止，更早的阶段改为 LOAD**」，并且**从后往前**检查，保证早阶段能覆盖晚阶段设的 LOAD：

```
若 6 个 do_* 全为 false（用户啥都没指定）：
    → 默认走 Analytical Placement 流程
      doPacking=SKIP, do_placement=SKIP, doAP=DO,
      doRouting=DO, doAnalysis=SKIP_IF_PRIOR_FAIL

否则从后往前判断：
  if do_analysis        → pack=LOAD, place=LOAD, route=LOAD, analysis=DO
  if do_routing         → pack=LOAD, place=LOAD, route=DO
  if do_placement       → pack=LOAD, place=DO
  if do_analytical_placement → pack=SKIP, place=SKIP, AP=DO
  if do_packing         → pack=DO
  if do_legalize        → pack=LOAD (并 load_flat_placement=true)
```

举例：用户只写 `--route`，则打包=LOAD、布局=LOAD、布线=DO——也就是「加载已有打包布局结果，只重新布线」。

#### 4.4.3 源码精读

枚举定义与字符串：

[vpr/src/base/vpr_types.h:L712-L722](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L712-L722) —— `enum class e_stage_action { SKIP=0, LOAD, DO, SKIP_IF_PRIOR_FAIL, NUM_STAGE_ACTIONS }`，配套 `stage_action_strings` 数组 `{"DISABLED", "LOAD", "ENABLED", "SKIP IF PRIOR_FAIL"}`，后者供 `show_setup` 打印每阶段动作（这就是日志里 `Packer: ENABLED` 这类输出的来源）。

布尔开关 → 阶段动作的核心翻译：

[vpr/src/base/setup_vpr.cpp:L283-L338](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_vpr.cpp#L283-L338) —— 这段是 4.4.2 伪代码的真实出处。重点看两点：(1) 全空时默认走 AP 流程（`doPacking=SKIP` 等）；(2) 「从后往前」覆盖逻辑——注释明确说「by checking in reverse order ... earlier stages override the default 'LOAD' action set by later stages」。

阶段流程按动作分派（以打包为例）：

[vpr/src/base/vpr_api.cpp:L684-L701](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L684-L701) —— 打包流程的真实分支：`SKIP` 直接返回；`DO` 调用 `try_pack` 真正打包；并用 `VTR_ASSERT(... == LOAD)` 断言剩下的情况就是「从文件加载」。布线、布局、分析阶段在同文件里有结构相同的分派代码（如 `vpr_api.cpp:872` 的布局、`vpr_api.cpp:1038/1070` 的布线、`vpr_api.cpp:1512-1517` 的分析，后者正是 `SKIP_IF_PRIOR_FAIL` 的落点）。

#### 4.4.4 代码实践（本讲主实践）

1. **目标**：把命令行三组开关映射到阶段动作，并说出每组代表参数的默认值来源。
2. **步骤**：
   - 在 `setup_vpr.cpp:283-338` 静态推演三种命令的翻译结果：
     - `vpr arch.xml c.blif --pack` → 打包动作？
     - `vpr arch.xml c.blif --route` → 打包/布局/布线动作各是什么？
     - `vpr arch.xml c.blif`（全空）→ 走哪条流程？
   - 在 `read_options.cpp` 里找出与「打包、布局、布线」对应的三组代表参数及其 `.default_value(...)`：
     - 打包：`--timing_driven_clustering`（聚类组）
     - 布局：`--place_algorithm`、`--seed`
     - 布线：`--route_chan_width`、`--route_type`、`--router_algorithm`
3. **观察**：注意布线三参数的默认值来源各不相同——`--route_chan_width` 默认 `-1`（read_options.cpp:3065，触发自动搜索）、`--route_type` 默认 `detailed`（read_options.cpp:3057）、`--router_algorithm` 默认 `timing driven`（read_options.cpp:3081 段）。
4. **预期结果**：你能填出下表（待本地验证帮助文本）：

   | 命令 | 打包 | 布局 | 布线 | 分析 |
   | --- | --- | --- | --- | --- |
   | `--pack` | DO | SKIP | SKIP | SKIP |
   | `--route` | LOAD | LOAD | DO | (默认 SKIP_IF_PRIOR_FAIL) |
   | (全空) | SKIP | SKIP | DO (走 AP) | SKIP_IF_PRIOR_FAIL |

5. 待本地验证：若已构建，运行 `./build/vpr/vpr --help | grep -A2 -E 'route_chan_width|route_type|router_algorithm|place_algorithm|timing_driven_clustering'`，比对默认值与你在源码读到的是否一致。

#### 4.4.5 小练习与答案

- **练习 1**：用户执行 `vpr arch.xml c.blif --analysis`，会发生什么？打包、布局、布线是 DO 还是 LOAD？
  - **答案**：见 `setup_vpr.cpp:304-309`：`do_analysis` 命中后，打包=LOAD、布局=LOAD、布线=LOAD、分析=DO。也就是从磁盘加载已有的 `.net`/`.place`/`.route`，只重新做时序分析。前提是这些文件已存在，否则 LOAD 会失败。
- **练习 2**：`SKIP_IF_PRIOR_FAIL` 用在哪个阶段？为什么专门为它设计这个状态？
  - **答案**：用在分析（analysis）阶段（`setup_vpr.cpp:297` 与 `vpr_api.cpp:1512-1517`）。因为如果布线都没成功，做时序分析毫无意义还会报一堆误导性错误；这个状态让分析「看上游脸色」——上游成功才跑，失败则自动跳过。
- **练习 3**：为什么「全空」命令行默认走 Analytical Placement（AP）流程而不是传统打包布局？
  - **答案**：见 `setup_vpr.cpp:290-297` 的注释——AP 流程把打包和布局集成在一起，因此传统 `doPacking`/`do_placement` 被设为 SKIP。这是 VPR 较新的默认路径（详见 u8-1 分析式布局），用户若想走传统流程需显式写 `--pack --place --route`。

## 5. 综合实践

把本讲四个模块串起来，完成一次「命令行侦探」任务：

1. **阅读** `main.cpp`，确认命令行解析发生在 `vpr_init` 内部（模块 4.1）。
2. **打开** `read_options.h`，从 `t_options` 里挑出你认为「最影响结果质量」的三个参数，各写明它属于哪个分组、模板类型 `T` 是什么（模块 4.2）。
3. **追踪** 这三个参数在 `read_options.cpp` 的 `create_arg_parser` 中如何注册：找到它们的 `.default_value(...)`，记录默认值字符串（模块 4.3）。
4. **推演** 如果用 `vpr arch.xml c.blif --route --route_chan_width 100` 这条命令：
   - 用 `setup_vpr.cpp` 的规则写出打包/布局/布线/分析四个动作（模块 4.4）；
   - 解释为什么这次布线**不会**触发 u1-l4 里说的「二分搜索最小通道宽度」（提示：`RouteChanWidth` 从默认 `-1` 变成了 `SPECIFIED` 的 100）。
5. **输出**：一份一页纸的「VPR 命令行速查」，包含「必填位置参数」「常用阶段开关」「三组阶段代表参数及默认值来源」三张小表。

> 待本地验证：若已按 u1-l2 构建出 `./build/vpr/vpr`，把第 4 步的命令真实跑一次（用一个示例架构和电路），观察日志开头 `show_setup` 打印的每阶段动作（`Packer/Placer/Router/Analysis: ENABLED|LOAD|DISABLED|...`），与你推演的结果对照。

## 6. 本讲小结

- `vpr` 的入口 `main()`（`main.cpp:44`）是个瘦壳：建三个状态容器 → `vpr_init`（含 `read_options`）→ `vpr_flow` → `vpr_free_all`，并用三层 `catch` 兜底异常。
- 所有命令行选项汇聚在一个巨型结构体 `t_options`（`read_options.h:11`），字段按文件名/阶段/图形/通用/打包/布局/布线等分组，每字段是 `argparse::ArgValue<T>`。
- 参数处理四步流水线：`create_arg_parser` 注册 → `parse_args` 切分 → `set_conditional_defaults` 补默认 → `verify_args` 校验（`read_options.cpp:19`）。
- `libargparse` 的 **Provenance**（UNSPECIFIED/DEFAULT/SPECIFIED/INFERRED）让条件默认值只在用户未指定时介入，不覆盖显式输入（`argparse_value.hpp:28`、`read_options.cpp:3798`）。
- 命令行布尔开关 `--pack/--place/--route/--analysis` 会被 `setup_vpr.cpp:283-338` 翻译成 `e_stage_action`（DO/LOAD/SKIP/SKIP_IF_PRIOR_FAIL），实现「运行到指定阶段为止、更早阶段 LOAD」的语义。
- 布线参数 `--route_chan_width` 默认 `-1`（自动搜索最小通道宽度），这正是 u1-l4「默认跑两次布线」的根源；一旦显式给值就关闭搜索。

## 7. 下一步学习建议

- 本讲只讲了「参数怎么进来」。参数被填进 `t_options` 后，如何被搬运到 `t_vpr_setup` 的各阶段选项结构体（`PackerOpts`/`PlacerOpts`/`RouterOpts`）并驱动流程，将在 **u3-5 主流程编排 vpr_api** 中展开。
- 想理解 `t_options` 字段引用的那些枚举（如 `e_route_type`、`e_router_algorithm`）的真实含义，可先翻 **u3-4 VprContext 与全局状态管理**，再到第 6 单元（布线）深入。
- 若你对「默认走 AP 流程」感到好奇，那是 **u8-1 分析式布局引擎** 的内容，可按需跳读。
- 推荐直接对照源码阅读：`read_options.cpp` 虽然有 4000 多行，但它是「VPR 全部可调旋钮」的权威清单，将来调试任何阶段参数都绕不开它。
