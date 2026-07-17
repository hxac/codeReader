# 图形可视化

## 1. 本讲目标

前面几讲里，VPR 的三大 CAD 阶段——打包、布局、布线——都只把结果落成磁盘上的文本文件（`.net`/`.place`/`.route`）和报告。但 FPGA CAD 算法本质上是在一个巨大的二维网格上「挪块、连钱」，纯文本很难让人建立直觉。VPR 提供了一套**交互式图形界面**，让你在算法运行过程中**实时看到**器件网格、当前布局、布线结果、拥塞热图、关键路径，甚至能在指定位置「打断」算法去检查中间状态。

本讲回答三个问题：

1. 这套图形界面是**可选的**——它默认不被编译进 `vpr`。那么「编译时如何开启、运行时如何开启」？为什么不能像普通功能那样一直开着？（**图形开关与构建**）
2. 界面里能画的东西很多：器件、网表、布线资源、关键路径、拥塞……它们分别属于布局阶段还是布线阶段，又用什么颜色/层次区分？（**可视化对象**）
3. 除了「看」，还能「调试」——VPR 内置了一套类似 GDB 的**断点**机制，能在「第 N 次交换」「第 N 轮布线」「某个块被移动时」暂停。这套断点的类型、检查逻辑、与布局布线主循环的衔接是怎样的？（**断点调试**）

学完本讲，你应当能：说清楚 `make ensure-gui`、`--disp`、`--renderer`、`--graphics_commands` 这条「编译→运行→脚本化」的链路；看懂 `draw/` 目录如何用 `update_screen()` 这一个回调驱动整张画布；并理解断点如何通过 `check_for_breakpoints()` 这个函数被布局（`placer_breakpoint.cpp`）和布线（`route_utils.cpp`）两侧复用。

## 2. 前置知识

本讲依赖 u5-l1（布局总览与模拟退火框架）的结论：布局是一个**迭代**过程，每「移动一次」要评估一次代价、决定接受或拒绝，并在外层逐温度退火；u6（布线）则是**逐轮迭代**的 Pathfinder 协商。图形界面与断点的「暂停点」正是插在这些迭代步上的——理解了「算法是循环的」，才能理解「断点插在循环的哪一圈」。

此外请回忆两个贯穿全书的概念：

- **架构驱动**：目标 FPGA 由运行时 XML 决定。图形界面画的器件网格、瓦片、布线导线**全部来自** u2 解析出的 `DeviceGrid` 与 u6-l1 的 RR Graph，GUI 代码本身不硬编码任何架构。
- **全局状态总线 `g_vpr_ctx`**：每个阶段把产物写进自己的子上下文。`draw/` 模块**只是消费者**——它从 `DeviceContext`（网格、RR 图）、`PlacementContext`（块坐标）、`RoutingContext`（路由树）读数据来画，自己几乎不持有算法状态。

两个本讲要用到的 GUI 基础概念：

- **回调式重绘（callback redraw）**：图形库不在每一帧主动问「画什么」，而是注册一个「需要刷新屏幕时调用我」的回调函数；VPR 的这个回调是 `draw_main_canvas()`。算法代码不直接画图，只调用 `update_screen()`「通知库：该刷新了」，刷新时库会回调 `draw_main_canvas()` 按**当前屏幕状态**决定画布局还是画布线。
- **EZGL**：VTR 自己的图形抽象库，位于 `libs/EXTERNAL/libezgl`（属外部子树，不可在 VTR 树内直接改，见 u1-l3）。VPR 的 `draw/` 通过 `ezgl::application`/`ezgl::renderer` 这组 API 作画，底层在历史上是 OpenGL/GTK，**现已迁移到 Qt6**（GPU 走 QRhi，软件走 QPainter）。所以本讲的「OpenGL」更准确的说法是「EZGL 抽象层 + Qt6 后端」。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [`vpr/src/draw/draw.h`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.h) | 绘图模块主头文件：`update_screen`（重绘入口）、`init_graphics_state`（运行时开关）、`notify_stage_complete`（阶段完成栅栏）、各类高亮/取色函数声明；用 `#ifndef NO_GRAPHICS` 把 Qt/EZGL 部分包起来 |
| [`vpr/src/draw/draw.cpp`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.cpp) | 核心实现：`init_graphics_state` 把命令行开关写进绘图状态、`update_screen` 是所有阶段调用的重绘函数、`notify_stage_complete` 配合脚本化命令 |
| [`vpr/src/draw/breakpoint.h`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/breakpoint.h) | 断点类型枚举 `bp_type`、`Breakpoint` 类、`check_for_breakpoints` 等检查/增删函数声明 |
| [`vpr/src/draw/breakpoint.cpp`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/breakpoint.cpp) | 断点检查实现：`check_for_breakpoints` 按类型分派，表达式断点用 `vtr::FormulaParser` 求值 |
| [`vpr/src/place/placer_breakpoint.cpp`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/placer_breakpoint.cpp) | 断点在**布局**侧的衔接点：`stop_placement_and_check_breakpoints` 在每次移动后更新断点状态、检查命中、高亮被移动块 |
| [`vpr/src/route/route_utils.cpp`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_utils.cpp) | 断点在**布线**侧的衔接点：`update_router_info_and_check_bp` 在每轮/每网后检查命中 |
| [`vpr/src/base/read_options.cpp`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp) | 命令行选项注册：`--disp`、`--auto`、`--save_graphics`、`--renderer`、`--graphics_commands` 都在「graphics options」组 |
| [`vpr/src/base/vpr_types.h`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h) | `ScreenUpdatePriority`（MINOR/MAJOR）与 `e_pic_type`（屏幕当前显示哪一阶段）两个枚举 |
| [`Makefile`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/Makefile) | 编译期开关：`make ensure-gui` / `make ensure-headless` 通过 `VTR_GRAPHICS`→`VPR_USE_EZGL` 链路决定是否启用图形 |
| [`vpr/CMakeLists.txt`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/CMakeLists.txt) | 把 `VPR_USE_EZGL=off` 翻译成 `-DNO_GRAPHICS` 宏，并 `find_package(Qt6)` |
| [`doc/src/vpr/graphics.rst`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/src/vpr/graphics.rst) | 官方图形使用手册：启用方法、各 Tab 功能、颜色表、Manual Moves、Pause 按钮 |

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：先讲清图形界面「编译期 + 运行期」的两道开关与底层依赖；再讲它能画哪些对象、如何按阶段与颜色区分；最后讲断点机制如何把 GUI 变成调试器。

---

### 4.1 图形开关与构建

#### 4.1.1 概念说明

VPR 的图形界面是**双重可选**的：编译期可以不开（这样 `vpr` 是个纯命令行程序，便于 CI/服务器运行）、运行期也可以不开（即使编译进去了，默认也不弹窗）。之所以设计成可选，有三个原因：

- **依赖重**：GUI 链接整个 Qt6，还要依赖 OpenGL/EGL/xkbcommon 运行时库。CI 环境、无头服务器往往没有这些。
- **拖慢编译/运行**：Qt6 的 moc/rcc 编译慢，运行时 GUI 事件循环也会拖慢算法。
- **批处理不友好**：回归测试要跑成千上万次 `vpr`，每次弹窗会卡住。

于是 VPR 用一个**编译期宏 `NO_GRAPHICS`** 把整套 GUI 代码「物理切除」：开了宏，`draw/` 里所有 Qt/EZGL 相关声明都消失，调用 `update_screen()` 变成空操作。这样无头构建既不依赖 Qt，也不会因为 GUI 代码里的 bug 崩溃。运行期再用 `--disp on` 决定「编译进去了，这次运行要不要真的开窗」。

#### 4.1.2 核心流程

开关链路从用户命令一路传到 CMake 宏：

```
make ensure-gui                      # 用户层
   └─ Makefile: VTR_GRAPHICS=on
        └─ Makefile L82-83: CMAKE_PARAMS += -DVPR_USE_EZGL=on
             └─ vpr/CMakeLists.txt L20-28: VPR_USE_EZGL=="on" → find_package(Qt6)
                  （不定义 NO_GRAPHICS，draw/ 代码全部生效）
make ensure-headless                 # 反之
   └─ VTR_GRAPHICS=off → -DVPR_USE_EZGL=off
        └─ vpr/CMakeLists.txt L33: GRAPHICS_DEFINES += "-DNO_GRAPHICS"
             （draw/ 中 #ifndef NO_GRAPHICS 段被编译器剔除）
```

运行期：

```
vpr ... --disp on [--renderer rhi|deferred|immediate]
              [--auto 0|1|2] [--save_graphics on]
              [--graphics_commands "set_nets 1; save_graphics out.png"]
   └─ read_options.cpp: 解析进 t_options.show_graphics / graphics_renderer / ...
        └─ vpr_api.cpp:1315  init_graphics_state(...)
             └─ draw.cpp:187  把开关写进 t_draw_state，并 new ezgl::application
```

关键点：

- **编译期开关有两个便捷目标**：`make ensure-gui`（先装 Qt6 SDK，再带 GUI 构建）与 `make ensure-headless`（直接无头构建），它们都委托给正常的 `make all`，只是固定了 `VTR_GRAPHICS` 的值。
- **运行期默认是关的**：`--disp` 默认 `off`；`off` 时 VPR 会把 Qt 切到 `offscreen` 平台——**不弹窗，但仍渲染**，这样 `--save_graphics`/`--graphics_commands` 这类脚本化输出在无头环境也能工作。
- **渲染后端三选一**：`--renderer` 默认 `rhi`（GPU，最快、最吃显存/内存）；`deferred`/`immediate` 是两个纯软件后端（无 GPU 也能跑，`immediate` 最兼容但最慢）。

#### 4.1.3 源码精读

**编译期：Makefile 把目标翻译成 CMake 参数**——[`Makefile:L79-L87`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/Makefile#L79-L87) 是核心：`VTR_GRAPHICS=on` 就往 `CMAKE_PARAMS` 追加 `-DVPR_USE_EZGL=on`，`off` 则追加 `=off`。两个便捷目标见 [`Makefile:L205-L211`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/Makefile#L205-L211)：`ensure-gui` 先跑 `dev/ensure_qt6_sdk.sh` 拉一个仓库内 Qt6 SDK（无需 root），再 `make all VTR_GRAPHICS=on`；`ensure-headless` 直接 `make all VTR_GRAPHICS=off`。注意 `ensure-gui` 构建的是默认 `all` 目标（不只 `vpr`），注释（[`Makefile:L201-L204`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/Makefile#L201-L204)）解释了原因：要让 `run_vtr_flow` 能找到 `yosys`。

**编译期：CMake 把开关翻译成宏**——[`vpr/CMakeLists.txt:L18-L35`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/CMakeLists.txt#L18-L35)：`VPR_USE_EZGL=on` 时 `find_package(Qt6 ...)` 并打印 `EZGL: graphics enabled`；`=off` 时把 `-DNO_GRAPHICS` 塞进 `GRAPHICS_DEFINES`。这个宏就是 `draw.h` / `draw.cpp` 里到处出现的 `#ifndef NO_GRAPHICS` 的来源——切除后所有 Qt/EZGL 声明都不参与编译。注意第 28 行有一个**版本下限** `VTR_QT_MIN_VERSION`，对应 `graphics.rst` 说的 Qt6 ≥ 6.9.3（更早的 Qt6 有 QRhi 渲染 bug）。

**运行期：命令行选项注册**——全部在「graphics options」组，见 [`read_options.cpp:L1692-L1735`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L1692-L1735)。逐一对应：`--disp`（[`L1694-L1706`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L1694-L1706)，默认 `off`，帮助文本明确说明 off 时会设 `QT_QPA_PLATFORM=offscreen`）、`--auto`（[`L1708-L1715`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L1708-L1715)，默认 1，控制暂停频率，只在 `--help` 显示）、`--save_graphics`（[`L1717-L1719`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L1717-L1719)，默认 off，把最终布局/布线存成 `vpr_placement.pdf`/`vpr_routing.pdf`）、`--renderer`（[`L1721-L1735`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L1721-L1735)，默认 `rhi`，三选一）、`--graphics_commands`（[`L1737`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L1737) 起，分号分隔的脚本命令）。这些字段在 [`read_options.h:L63-L66`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.h#L63-L66) 声明为 `ArgValue`（回顾 u1-l5 的选项体系）。

**运行期：开关写入绘图状态**——`init_graphics_state` 见 [`draw.cpp:L187-L233`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.cpp#L187-L233)。它把六个形参逐一写进 `t_draw_state`（[`L202-L208`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.cpp#L202-L208)）：`show_graphics`、`gr_automode`、`draw_route_type`、`save_graphics`、`graphics_commands`、`renderer_type`、`is_flat`。两处关键：当 `--disp off` 时，[`L212-L214`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.cpp#L212-L214) 在 `QApplication` 创建**之前**把 `QT_QPA_PLATFORM` 设成 `offscreen`，这样 Qt 不会去连 X11/Wayland 显示服务器；随后 [`L219-L221`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.cpp#L219-L221) 懒构造全局 `ezgl::application` 对象。整个函数体被 `#ifndef NO_GRAPHICS` 包住，无头构建时只剩 `(void)形参;` 抑制告警（[`L223-L232`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.cpp#L223-L232)）。

**调用点**——主流程在 [`vpr_api.cpp:L1315`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1315) 调用 `init_graphics_state`，把 `vpr_setup.ShowGraphics` 等配置传进去；这是图形子系统在运行期唯一的「上电」点。

#### 4.1.4 代码实践

**实践目标**：把「编译期开关」与「运行期开关」两条链路对应到具体代码行，弄清二者各自控制什么。

**操作步骤**：

1. 打开 [`Makefile:L205`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/Makefile#L205)，确认 `ensure-gui` 先调 `dev/ensure_qt6_sdk.sh` 再 `make all VTR_GRAPHICS=on`；并对照 [`Makefile:L82-L87`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/Makefile#L82-L87) 看 `VTR_GRAPHICS` 如何变成 `-DVPR_USE_EZGL`。
2. 打开 [`vpr/CMakeLists.txt:L20`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/CMakeLists.txt#L20) 与 [`L33`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/CMakeLists.txt#L33)，确认 `on` 走 `find_package(Qt6)`、`off` 定义 `-DNO_GRAPHICS`。
3. 打开 [`draw.h:L28`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.h#L28) 与 [`L105`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.h#L105) 的两处 `#ifndef NO_GRAPHICS`，理解这个宏如何「物理切除」EZGL/Qt 声明。
4. 打开 [`read_options.cpp:L1694`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L1694)，读 `--disp` 的帮助文本，找出「off 时仍渲染、脚本化输出仍有效」对应的那句说明。

**需要观察的现象**：`--disp` 的默认值是 `off`，且帮助文本明确提到它会设 `QT_QPA_PLATFORM=offscreen`——这说明「不开窗」不等于「不渲染」。

**预期结果**：能填出下表：

| 层次 | 控制对象 | 机制 |
|------|---------|------|
| 编译期 | GUI 代码是否进入二进制 | `make ensure-gui/headless` → `VTR_USE_EZGL` → `NO_GRAPHICS` 宏 |
| 运行期 | 这次运行是否真的开窗 | `--disp on/off` → `show_graphics` → `QT_QPA_PLATFORM` |

> 本实践为源码阅读型实践，无需运行。若想实地验证，需在有显示的环境跑 `make ensure-gui`（详见 4.2.4 综合实践），**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `init_graphics_state` 要在 `QApplication` 创建**之前**就把 `QT_QPA_PLATFORM` 设成 `offscreen`，而不是创建之后？

> **答案**：Qt 在 `QApplication` 构造时就根据 `QT_QPA_PLATFORM` 选定底层平台插件（QPA）并尝试连接显示服务器。一旦 `QApplication` 已建好，再改这个环境变量不会生效。所以必须在构造前设置（[`draw.cpp:L212-L214`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.cpp#L212-L214)），让无头运行直接走 offscreen，避免在无 X11/Wayland 的机器上崩溃。

**练习 2**：`--disp off` 时，`--save_graphics on` 还能产出 PDF 吗？为什么？

> **答案**：能。`off` 只是关闭**交互窗口**，渲染管线仍在 offscreen 平台上跑（见 `--disp` 帮助文本）。`save_graphics` 走的是「渲染到图像再写文件」的路径，不依赖交互窗口，所以无头环境也能产出 `vpr_placement.pdf`/`vpr_routing.pdf`。这正是 VTR 回归测试能在 CI 里用 `--graphics_commands` 生成对照图的原理。

---

### 4.2 可视化对象

#### 4.2.1 概念说明

「图形开关」打开后，屏幕上能画什么？VPR 的可视化对象可以按**数据来源**和**所属阶段**分成几层，每层对应算法流程的一个产物：

- **器件层**（来自 u2 的 `DeviceGrid`）：FPGA 的瓦片网格——这是「地板」，布局布线都画在它上面。
- **布局层**（来自 u3-l3 的 `ClusteredNetlist` + `PlacementContext` 的块坐标）：每个聚簇逻辑块（CLB）摆在哪个网格位置，块类型用什么颜色。
- **网表层**（来自网表的 Net/Pin）：网（net）的连接关系，布局阶段画成**飞线（flyline）**（源到汇的直线），布线阶段可画成**真实走线（routing）**。
- **布线资源层**（来自 u6-l1 的 RR Graph）：布线导线段、引脚、开关盒/连接盒——这是「芯片的内部接线图」。
- **分析层**（来自 u7 时序与布线统计）：关键路径、拥塞热图、布线利用率热图。

一个关键设计是：**屏幕一次只显示一个阶段的内容**，由枚举 `e_pic_type` 标记「现在画的是布局还是布线」。`update_screen()` 每次被调用时带上这个值，回调 `draw_main_canvas()` 据此决定调用哪些子绘制函数。这避免了「布局还没完成却试图画布线」的混乱。

#### 4.2.2 核心流程

绘制是「**通知 → 回调 → 按状态分发**」三段式：

```
阶段代码（place.cpp / route.cpp / vpr_api.cpp）
  └─ update_screen(priority, msg, pic_on_screen_val, timing_info)
       └─ draw.cpp:L385
            ├─ if (application == nullptr) return;     # 还没初始化，直接返回
            ├─ if (!show_graphics): 禁用事件循环（仍可离屏渲染）
            ├─ if (pic_on_screen 变了): set_initial_world() + 配置按钮
            │     └─ 首次(NO_PICTURE): add_canvas("MainCanvas", draw_main_canvas)
            └─ 触发 EZGL 刷新 → 库回调 draw_main_canvas()
                 └─ draw_main_canvas 按 pic_on_screen 决定：
                       PLACEMENT → 画网格 + 块 + (可选)飞线/关键路径
                       ROUTING   → 画网格 + 块 + 走线 + (可选)RR资源/拥塞
```

两类「优先级」控制刷新行为（枚举 [`vpr_types.h:L74-L77`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L74-L77)）：

- `MINOR`：小刷新，常用于算法迭代中「画一下当前进度」，不一定要求用户点 Proceed。
- `MAJOR`：大刷新，通常是阶段切换或断点命中，会等待用户交互。

屏幕当前画哪个阶段由 `e_pic_type` 标记（[`vpr_types.h:L389-L394`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L389-L394)）：`NO_PICTURE`、`PLACEMENT`、`ANALYTICAL_PLACEMENT`（u8-l1 的分析式布局）、`ROUTING`。

#### 4.2.3 源码精读

**重绘入口声明**——[`draw.h:L43-L46`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.h#L43-L46) 是所有阶段画图的统一入口：四个形参分别是优先级、状态栏消息、当前屏幕阶段、时序信息（画关键路径用）。注意它**没有**用 `#ifndef NO_GRAPHICS` 包起来——因为无头构建时它在 `draw.cpp` 里被编成空函数，调用方代码不用改。

**重绘入口实现**——[`draw.cpp:L385-L427`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.cpp#L385-L427)。三个要点：①懒初始化保护，若 `application` 还是空指针就直接返回（[`L399-L400`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.cpp#L399-L400)）；②根据 `show_graphics` 决定是否启用事件循环（[`L404-L408`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.cpp#L404-L408)），这是「不开窗但仍渲染」的实现；③**阶段切换检测**——当 `pic_on_screen != pic_on_screen_val`（[`L414-L416`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.cpp#L414-L416)）时，重设画布世界坐标并切换按钮组；首次（从 `NO_PICTURE`）会调用 `add_canvas("MainCanvas", draw_main_canvas, ...)`（[`L426-L427`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.cpp#L426-L427)）注册那个真正的绘制回调，并在其后（[`L429-L450`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.cpp#L429-L450)）按 `--renderer` 选择 rhi/deferred/immediate 后端。

**阶段完成栅栏**——[`draw.h:L48-L61`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.h#L48-L61) 声明的 `notify_stage_complete(stage)` 配合 `--graphics_commands` 的 `wait_for_stage <stage>_done` 屏障：脚本化命令可以「等到某个阶段彻底完成（其全局上下文已 settle）再执行」，而不是卡在每次迭代的 `update_screen()` 上。实现见 [`draw.cpp:L235`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.cpp#L235)，调用点如 [`vpr_api.cpp:L1020`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1020)（布局完成）、[`L1156`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1156)（布线完成）。

**高亮与取色函数**——`draw.h` 声明了一族交互式高亮辅助。三类高亮对象对应三种颜色常量（[`draw.h:L106-L108`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.h#L106-L108)）：`SELECTED_COLOR`（用户选中的对象，绿色）、`DRIVES_IT_COLOR`（驱动选中对象者，红色）、`DRIVEN_BY_IT_COLOR`（被选中对象驱动者，蓝）。这套「选中→红色驱动→蓝色被驱动」的配色就是 `graphics.rst` 里「Highlight Block Fan-in and Fan-out」功能的实现（[`draw.h:L137`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.h#L137) `draw_if_net_highlighted`、[`L154`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.h#L154) `highlight_moved_block_and_its_terminals`）。块类型颜色则由 [`get_block_type_color`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.h#L177)（[`draw.h:L177`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.h#L177)）按物理瓦片类型分配，块类型多于颜色时环绕复用（见其上方注释 [`L175-L176`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.h#L175-L176)）。

**官方手册的功能与颜色表**——[`graphics.rst`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/src/vpr/graphics.rst) 是把上述代码功能翻译成用户视角的权威文档。几个关键对照：

- 布局阶段只画**飞线**网表与飞线关键路径（因为还没布线）；布线阶段才能画**真实走线**与 RR 资源（[`graphics.rst:L96-L128`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/src/vpr/graphics.rst#L96-L128)、[`L141-L142`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/src/vpr/graphics.rst#L141-L142)）。
- RR 节点/边有固定配色表：节点 Channel 黑、Input Pin 紫、Output Pin 粉；边按连接类型区分（[`graphics.rst:L175-L203`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/src/vpr/graphics.rst#L175-L203)）。
- 拥塞/利用率热图用浅黄（高）到深蓝（低）表达（[`L209-L230`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/src/vpr/graphics.rst#L209-L230)）。
- 多层（3D 堆叠）架构有 Layers 下拉，层 0 先画、高层叠在上面（[`L245-L256`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/src/vpr/graphics.rst#L245-L256)）——对应 `draw.h` 里 `get_element_visibility_and_transparency(src_layer, sink_layer)`（[`draw.h:L200`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.h#L200)）那种「按层决定可见性与透明度」的函数。
- 按钮功能总表（[`L258-L329`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/src/vpr/graphics.rst#L258-L329)）逐项标了「哪些阶段可用」。

#### 4.2.4 代码实践

**实践目标**：把 `graphics.rst` 里用户可见的功能，反查到 `draw.h` 里对应的代码符号；并用 `--graphics_commands` 在无头环境跑通一次「脚本化出图」。

**操作步骤**：

1. 阅读启用方法 [`graphics.rst:L13-L55`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/src/vpr/graphics.rst#L13-L55)，确认 `make ensure-gui` 是「带 GUI」的入口（它内部跑 `ensure_qt6_sdk.sh` 装 Qt6，见 [`L29-L31`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/src/vpr/graphics.rst#L29-L31)）。
2. 在 [`draw.h`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.h) 里找出与本讲「三类可高亮对象」对应的符号：
   - 网表高亮 → `draw_if_net_highlighted`（[`L137`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.h#L137)）；
   - 被移动块高亮 → `highlight_moved_block_and_its_terminals`（[`L154`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.h#L154)）；
   - 任意位置指定颜色 → `set_draw_loc_color`（[`L158`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.h#L158)）/ `highlight_loc_with_specific_color`（[`L173`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.h#L173)）。
3. （可选，需已 `make ensure-gui`）用脚本化命令无头出图，验证「`--disp off` 仍渲染」：

   ```shell
   ./build/vpr/vpr <电路.blif> <架构.xml> --route_chan_width 100 \
       --graphics_commands "set_nets 1; save_graphics nets_{i}.png" \
       --disp off
   ```

   `set_nets 1` 打开网表绘制、`save_graphics nets_{i}.png` 存图（`{i}` 每次自增），命令清单见 [`read_options.cpp:L1741-L1764`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L1741-L1764)。

**需要观察的现象**：第 2 步应能列出三类高亮对象——①整个网（含其 fan-in/fan-out）、②一次布局移动涉及的被搬块及其端子、③任意指定坐标按指定颜色高亮。第 3 步（若能运行）即便 `--disp off` 也会在工作目录生成 `nets_*.png`。

**预期结果**：能口述「`make ensure-gui` 编译期开启 GUI、`--disp on` 运行期开窗、`--graphics_commands` 在无头环境也能脚本化出图」，并列出三类可高亮对象。

> 第 3 步需要本机构建好带 GUI 的 `vpr`（`make ensure-gui`）并提供一个真实电路与架构文件。若本地未具备，**待本地验证**；可退化为纯源码阅读：只做第 1、2 步。

#### 4.2.5 小练习与答案

**练习 1**：为什么布局阶段只能画「飞线」网表，而布线阶段才能画「真实走线」？

> **答案**：飞线只是「源引脚到汇引脚的直线」，只要有块坐标就能画；而真实走线需要 RR Graph 上的具体节点序列（路由树），那是布线阶段才产出的（u6-l1/l3）。布局阶段路由树尚不存在，所以 `graphics.rst` 明确「Only the Flylines option is available during placement」。代码层面，`draw_main_canvas()` 会按 `pic_on_screen` 是 `PLACEMENT` 还是 `ROUTING` 选择调用不同的绘制子函数。

**练习 2**：`e_pic_type` 里除了 `PLACEMENT`/`ROUTING`，还有 `ANALYTICAL_PLACEMENT`。它对应哪个流程？为什么单列一项？

> **答案**：对应 u8-l1 的分析式布局（Analytical Placement, AP）支路。AP 用连续坐标做全局布局，其画布世界坐标与默认打包布局不同（块之间不留缝），所以 `update_screen` 在切到 `ANALYTICAL_PLACEMENT` 时会调 `set_initial_world_ap()` 而非 `set_initial_world()`（[`draw.cpp:L419-L423`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.cpp#L419-L423)），故需单独的 pic 类型。

---

### 4.3 断点调试

#### 4.3.1 概念说明

光「看」还不够。布局退火可能跑几百万次移动、布线可能跑几十轮迭代，你很难只靠肉眼盯屏定位「为什么这次布局结果很差」或「为什么这根网布不通」。VPR 借鉴 GDB 的思路，提供了一套**断点（breakpoint）**机制：你设定一个条件（「第 1000 次移动」「温度变到第 5 次」「块 42 被移动时」「第 3 轮布线」），算法每走一步就检查「命中了吗」，命中就暂停、弹出信息窗口、并在画布上高亮相关对象，等你检查完点 Proceed 继续。

断点的核心抽象是 `Breakpoint` 类：每个断点有且仅有一个**类型**（`bp_type` 枚举）和一个对应的**值**，外加一个 `active` 开关。类型决定了「断在什么事件上」以及「这个断点在布局还是布线阶段生效」。

#### 4.3.2 核心流程

断点机制由三方协作：

```
① 用户（GUI 调试窗口）创建断点 → 存进 t_draw_state::list_of_breakpoints
② 算法主循环每步调用衔接函数：
     布局: stop_placement_and_check_breakpoints()  (placer_breakpoint.cpp)
     布线: update_router_info_and_check_bp()        (route_utils.cpp)
   衔接函数先更新「当前状态」(move_num / temp_count / router_iter / ...)，
   再调用统一检查函数 check_for_breakpoints(in_placer)。
③ check_for_breakpoints(in_placer)  (breakpoint.cpp)
     遍历 list_of_breakpoints：
       按类型分派 → 布局类(BT_MOVE_NUM/BT_TEMP_NUM/BT_FROM_BLOCK) 要求 in_placer==true
                   布线类(BT_ROUTER_ITER/BT_ROUTE_NET_ID) 要求 in_placer==false
                   表达式类(BT_EXPRESSION) 两阶段通用
       命中则 print_current_info() 打印状态、返回 true
④ 命中后衔接函数高亮对象 + update_screen(MAJOR, ...) 等待用户交互
```

七种断点类型（[`breakpoint.h:L23-L31`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/breakpoint.h#L23-L31)）：

| 类型 | 触发条件 | 生效阶段 |
|------|---------|---------|
| `BT_MOVE_NUM` | 累计移动次数达到指定值 | 布局 |
| `BT_TEMP_NUM` | 温度变化次数达到指定值 | 布局 |
| `BT_FROM_BLOCK` | 某次移动的首块是指定块 ID | 布局 |
| `BT_ROUTER_ITER` | 布线迭代轮数达到指定值 | 布线 |
| `BT_ROUTE_NET_ID` | 指定网被布线后 | 布线 |
| `BT_EXPRESSION` | 表达式（如 `move_num>3 && block_id==11`）为真 | 两阶段 |
| `BT_UNIDENTIFIED` | 默认构造，永不命中 | — |

表达式断点是「瑞士军刀」：它把断点条件写成字符串，交给 `vtr::FormulaParser`（VPR 自带的公式求值器，回顾 u5-l3 的 `compressed_grid` 也用到这类求值）在当前状态上求值，非零即命中。

#### 4.3.3 源码精读

**断点类型与类**——[`breakpoint.h:L23-L31`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/breakpoint.h#L23-L31) 定义枚举 `bp_type`；[`L33-L100`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/breakpoint.h#L33-L100) 是 `Breakpoint` 类：它把所有类型的值都做成成员（`bt_moves`/`bt_temps`/`bt_from_block`/`bt_router_iter`/`bt_route_net_id`/`bt_expression`），加一个 `active` 开关。三个构造器分工：默认构造（[`L46-L55`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/breakpoint.h#L46-L55)，全置 -1、类型 `BT_UNIDENTIFIED`，即「永不命中」）、`(类型, int)`（[`L58-L70`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/breakpoint.h#L58-L70)，按类型把 int 赋给对应字段）、`(BT_EXPRESSION, string)`（[`L74-L77`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/breakpoint.h#L74-L77)，存表达式串）。`operator==`（[`L80-L99`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/breakpoint.h#L80-L99)）按类型 + 对应字段判等，用于去重/删除。

**统一检查函数**——声明在 [`breakpoint.h:L105`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/breakpoint.h#L105)，实现在 [`breakpoint.cpp:L60-L78`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/breakpoint.cpp#L60-L78)。它遍历 `list_of_breakpoints`，对每个 `active` 的断点按类型分派到对应的 `check_for_*_breakpoints`，并把**阶段匹配**编进条件：布局类要求 `in_placer==true`、布线类要求 `in_placer==false`（[`L64-L73`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/breakpoint.cpp#L64-L73)），`BT_EXPRESSION` 两阶段都查（[`L74-L75`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/breakpoint.cpp#L74-L75)）。这一个 `in_placer` 形参就是「布局/布线复用同一套断点机制」的关键。

**表达式断点求值**——[`breakpoint.cpp:L46-L57`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/breakpoint.cpp#L46-L57)：把表达式交给 `vtr::FormulaParser::parse_formula`，在当前 `BreakpointState`（含 `move_num`/`temp_count`/`router_iter`/`block_id` 等变量）上求值，返回 1 即命中。命中的描述（表达式串）写进 `bp_description` 供信息窗口显示。注意第 49 行第三个参数 `true` 是「允许变量解析」。

**布局侧衔接**——`stop_placement_and_check_breakpoints` 见 [`placer_breakpoint.cpp:L28-L66`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/placer_breakpoint.cpp#L28-L66)。每次移动后调用：先把 `moved_blocks` 转成 int 向量写进断点状态（`transform_blocks_affected`，[`L17-L26`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/placer_breakpoint.cpp#L17-L26)），再 `move_num++`、记下 `from_block`（[`L39-L40`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/placer_breakpoint.cpp#L39-L40)），然后 `check_for_breakpoints(true)`（[`L43`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/placer_breakpoint.cpp#L43)）。命中后：弹出信息窗口（[`L45`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/placer_breakpoint.cpp#L45)）、用 `highlight_moved_block_and_its_terminals` 高亮被搬块及其端子（[`L63`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/placer_breakpoint.cpp#L63)）、`update_screen(MAJOR, ...)` 带上 Δ 代价与接受/拒绝/中止结果等用户（[`L64`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/placer_breakpoint.cpp#L64)）。注意 [`L36`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/placer_breakpoint.cpp#L36) 的早退：若无任何断点，直接设「未命中」返回，几乎零开销——这保证了「装了 GUI 但没设断点」时不拖慢退火。

**布线侧衔接**——`update_router_info_and_check_bp` 见 [`route_utils.cpp:L676-L699`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_utils.cpp#L676-L699)。它区分两种事件：`BP_ROUTE_ITER`（一轮布线结束，`router_iter++` 后 `check_for_breakpoints(false)`，[`L678-L680`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_utils.cpp#L678-L680)）与 `BP_NET_ID`（某网布完，只查 net_id 与表达式类断点，[`L681-L693`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_utils.cpp#L681-L693)）。命中后同样弹窗 + `update_screen(MAJOR, "Breakpoint Encountered", ROUTING, ...)`（[`L695-L698`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_utils.cpp#L695-L698)）。

**状态打印**——命中后 [`print_current_info`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/breakpoint.cpp#L93-L98)（声明 [`breakpoint.h:L130`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/breakpoint.h#L130)）按阶段把 `move_num`/`temp_count`/`block_affected`/`from_block`（布局）或 `router_iter`/`net_id`（布线）打到终端，便于在日志里定位。

#### 4.3.4 代码实践

**实践目标**：理解「同一个 `check_for_breakpoints` 如何被布局和布线复用」，并看清断点命中后的高亮与暂停链路。

**操作步骤**：

1. 打开 [`breakpoint.cpp:L60`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/breakpoint.cpp#L60)，逐类型看清「阶段门控」：哪些类型挂在 `in_placer` 为真的分支、哪些挂在为假的分支。
2. 对照两处调用：布局侧 [`placer_breakpoint.cpp:L43`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/placer_breakpoint.cpp#L43) 传 `true`、布线侧 [`route_utils.cpp:L680`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_utils.cpp#L680) 传 `false`，确认同一个函数靠这一个参数区分阶段。
3. 读 [`placer_breakpoint.cpp:L51-L65`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/placer_breakpoint.cpp#L51-L65)，列出命中后做了哪三件事（弹信息窗 → 高亮被搬块 → MAJOR 重绘带 Δ 代价）。
4. 读 `graphics.rst` 的 Pause 按钮一节（[`L364-L375`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/src/vpr/graphics.rst#L364-L375)）与 Manual Moves 一节（[`L331-L362`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/src/vpr/graphics.rst#L331-L362)），理解断点暂停后用户能做什么（点 Next Step 继续、手动指定下一步移动看 Δ 代价）。

**需要观察的现象**：断点机制的「开销」几乎全在 `list_of_breakpoints` 非空时才发生（[`placer_breakpoint.cpp:L36`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/placer_breakpoint.cpp#L36) 早退）。

**预期结果**：能画出断点的「事件 → 状态更新 → check_for_breakpoints(in_placer) → 命中→高亮+MAJOR 重绘」流程，并说清 `in_placer` 参数如何让一套机制服务布局与布线两个阶段。

> 实地触发断点需要带 GUI 的 `vpr`、一个显示器、并在调试窗口里设断点，**待本地验证**。无 GUI 时，可纯阅读上述代码完成本实践。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `check_for_breakpoints` 要用一个 `bool in_placer` 参数，而不是分成 `check_placer_breakpoints` / `check_router_breakpoints` 两个函数？

> **答案**：因为断点的**检查逻辑是统一的**（遍历 `list_of_breakpoints`、按类型分派），只有「哪些类型在哪个阶段生效」不同。用一个函数 + 一个阶段参数，把「阶段门控」内联进类型分派（[`breakpoint.cpp:L64-L75`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/breakpoint.cpp#L64-L75)），既复用了遍历与表达式求值代码，又让 `BT_EXPRESSION` 这类两阶段通用的断点只写一份。这是「用参数区分分支、而非复制函数」的典型取舍。

**练习 2**：表达式断点 `move_num > 3 && block_id == 11` 是怎么被求值的？`move_num`、`block_id` 这些变量从哪来？

> **答案**：表达式串被传给 `vtr::FormulaParser::parse_formula`（[`breakpoint.cpp:L47-L49`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/breakpoint.cpp#L47-L49)），求值器从「当前状态」`BreakpointState` 里查变量。`move_num` 由布局侧每步 `move_num++` 维护（[`placer_breakpoint.cpp:L39`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/placer_breakpoint.cpp#L39)），`block_id` 来自被移动块。衔接函数在调用 `check_for_breakpoints` **之前**就把这些状态更新好，所以求值器拿到的是「当前这一步」的实时值。

---

## 5. 综合实践

把三个最小模块串起来，做一次「**从开关到调试**的完整追踪」。

**任务**：假设你想在布局退火跑到第 500 次移动、且块 7 被搬动时停下来检查，回答下列问题，每个答案都要给出源码行作证据。

1. **编译**：要让 `vpr` 二进制里**包含** GUI 代码，应当运行哪条命令？它如何确保 `draw/` 的 `#ifndef NO_GRAPHICS` 段生效？（提示：`make ensure-gui` → `Makefile:L205-L207` + `vpr/CMakeLists.txt:L20-L28`）
2. **运行**：启动时除了 `--disp on`，若想用脚本在无头机器上存图，应加哪个选项？它最终被哪个函数写进绘图状态？（提示：`--graphics_commands` → `draw.cpp:L206`）
3. **画布**：屏幕从布局切到布线时，`update_screen` 内部检测到 `pic_on_screen` 变化后做了什么？为什么分析式布局要走不同的初始化？（提示：`draw.cpp:L414-L423`）
4. **断点**：在第 500 次移动、块 7 被搬时暂停，应当用哪种断点类型？命中后布局侧的高亮与重绘链路是哪三步？（提示：`BT_EXPRESSION` 或 `BT_FROM_BLOCK`；`placer_breakpoint.cpp:L45/L63/L64`）

**操作建议**：以 [`draw.cpp:L385`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/draw.cpp#L385) 的 `update_screen` 为中心锚点，向上追到 `init_graphics_state`（开关如何注入）与 `read_options`（开关从哪来），向下追到 `placer_breakpoint.cpp` / `route_utils.cpp`（断点如何接入主循环），画出「开关 → 状态 → 重绘 → 断点」的完整数据流。

**进阶（可选）**：阅读 [`manual_moves.cpp`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/manual_moves.cpp)（Manual Moves 功能的实现），理解断点暂停后用户手动指定一次移动时，GUI 如何回传到布局器去评估 Δ 代价——这把本讲的「断点调试」与 u5-l1/u5-l3 的「移动评估」衔接起来。

## 6. 本讲小结

- **图形界面双重可选**：编译期用 `make ensure-gui`/`ensure-headless` 经 `VTR_GRAPHICS`→`VPR_USE_EZGL`→`NO_GRAPHICS` 宏决定 GUI 代码是否进二进制；运行期用 `--disp on/off` 决定是否开窗，`off` 时切到 Qt `offscreen` 平台——不弹窗但仍渲染，故 `--save_graphics`/`--graphics_commands` 在无头环境也能出图。
- **底层是 EZGL + Qt6**：`draw/` 通过 `ezgl::application`/`renderer` 作画，`libs/EXTERNAL/libezgl` 现驱动 Qt6，渲染后端三选一（`--renderer`：rhi=GPU 默认，deferred/immediate=软件）。
- **重绘是回调式 + 状态分发**：所有阶段统一调 `update_screen(priority, msg, pic_on_screen, timing)`，它按 `e_pic_type`（NO_PICTURE/PLACEMENT/ANALYTICAL_PLACEMENT/ROUTING）决定画布局还是布线，回调 `draw_main_canvas`；`MINOR`/`MAJOR` 两种 `ScreenUpdatePriority` 控制是否等用户交互。
- **可视化对象分阶段**：布局画器件网格 + 块 + 飞线网表/关键路径；布线才能画真实走线 + RR 资源（节点/边有固定配色表）+ 拥塞/利用率热图；多层架构按层叠绘并支持透明度。三类高亮对象（整网、被搬块及其端子、指定坐标）用 `SELECTED`/`DRIVES_IT`/`DRIVEN_BY_IT` 三色与 `highlight_*` 函数族实现。
- **断点机制布局/布线复用**：`Breakpoint` 类有七种类型，`check_for_breakpoints(in_placer)` 用一个布尔参数做阶段门控；布局侧 `placer_breakpoint.cpp` 传 `true`、布线侧 `route_utils.cpp` 传 `false`，命中后都走「弹信息窗 → 高亮 → MAJOR 重绘」三步。表达式断点用 `vtr::FormulaParser` 在实时状态上求值。
- **零开销设计**：无断点时衔接函数早退（`list_of_breakpoints.empty()`），保证「装了 GUI 但没设断点」几乎不拖慢算法。

## 7. 下一步学习建议

- **接 u8-l5（Server 模式与并行布线）**：了解 VPR 在被外部工具驱动（server 模式）时，图形界面如何与之共存（注意 `vpr/CMakeLists.txt:L43-L46` 里「server 依赖 EZGL 开启」的约束），以及并行布线下交互式调试的局限。
- **深入 EZGL**：阅读 `libs/EXTERNAL/libezgl`（属外部子树，改动需走 `dev/external_subtrees.py`，见 u1-l3），理解 `ezgl::application`、`renderer`、`canvas` 三者如何把 VPR 的 `draw_main_canvas` 回调接到 Qt6 的 QRhi/QPainter 后端。
- **Manual Moves 与退火衔接**：精读 [`manual_moves.cpp`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/draw/manual_moves.cpp)，看 GUI 如何把用户指定的 `(block, to_loc)` 回传给布局器，复用 u5-l2 的移动生成器与 u5-l3 的增量代价事务，把本讲的「交互」与第 5 单元的「布局算法」打通。
- **脚本化回归出图**：研究 `vtr_flow` 回归任务如何用 `--graphics_commands` + `--disp off` 在 CI 里批量生成对照图（参考 u9-l3 回归测试），理解图形子系统在自动化流水线里的角色。
