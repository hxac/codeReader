# 单元测试体系 Catch2

## 1. 本讲目标

VTR 是一个由几十万行 C++ 组成的大型 FPGA CAD 框架，每一次算法改动都可能悄悄改变布局布线结果。要安全地演进它，就必须有一套能在分钟级跑完、精确定位到「哪一行断言失败」的自动化测试。本讲聚焦 VTR 的**单元测试体系**——基于第三方框架 Catch2 组织的一组测试二进制。

学完本讲你应当能够：

- 说出 VTR 四个测试二进制各自对应哪个源码目录（**源到目标映射**），并能直接在 `build/` 下找到它们。
- 读懂任意一个 `test_*.cpp` 文件：理解 `TEST_CASE`、`SECTION`、`REQUIRE`、`[标签]` 这些 Catch2 宏的含义与执行方式。
- 自己新增一个单元测试：知道文件放哪、CMake 要不要改、如何用 `make test` 或直接跑二进制执行它，以及如何按标签/名字过滤只跑某几个用例。

本讲是第 9 单元「共享库、测试与工程实践」的第二讲，承接 u1-l2（构建与运行 VTR）建立的「Makefile 包装层 + CMake」认知，并直接依赖 u9-l1 讲过的 `StrongId`、`vtr::vector`、`g_vpr_ctx` 等数据结构——这些正是单元测试最爱直接驱动的小颗粒对象。

## 2. 前置知识

在进入测试代码前，先用三段通俗语言建立直觉。

**什么是单元测试，它和回归测试有何分工。** 单元测试把一个函数或数据结构**单独拎出来**，喂入手工构造的小输入，再用断言（assertion）检查输出是否符合预期。它的特点是快、独立、不需要完整 FPGA。回归测试（regression test）则是把整条 VTR 流水线跑一遍，对比布线结果与「黄金结果」。VTR 官方文档把二者分工讲得很直白：能脱离完整流程跑的、颗粒小的 API/数据结构，用单元测试；而 CAD 算法的行为因为「构造一个真实网表需要大输入文件」，主要靠回归测试覆盖。

**什么是 Catch2。** Catch2 是一个「头文件 + 一个 main」式的 C++ 单元测试框架。你不需要写 `main()`，只要写若干个 `TEST_CASE("名字", "[标签]") { ... }`，框架会自动生成可执行程序，依次运行每个用例，并打印一份绿色/红色的汇总报告。它的断言宏（`REQUIRE`、`REQUIRE_NOTHROW` 等）比 C 标准的 `assert` 友好得多：失败时不仅报行号，还会把表达式两边的实际值都打出来。

**VTR 把测试放在哪、为什么这样分。** VTR 是多组件工作区（见 u1-l3）：`vpr/`、`libs/libarchfpga/`、`libs/libvtrutil/`、`utils/fasm/` 各自是相对独立的库，于是每个组件在**自己的 `test/` 子目录**里放自己的单元测试，并各自编译出一个独立的测试二进制。这种「组件自治」的好处是：改 `libvtrutil` 的容器时只跑 `test_vtrutil`，不必连带重编、重跑整个 VPR。

> 与 u9-l1 的衔接：u9-l1 讲的 `VTR_ASSERT` 是「生产代码里的防御性断言」，运行期常开、失败即崩溃；本讲讲的 `REQUIRE` 是「测试代码里的验证断言」，只在做单元测试时运行、失败只是标红一个用例。两者一防一验，配合使用。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|---|---|
| `doc/agents/testing.md` | 官方测试指南，给出「源到目标映射」和运行命令，是本讲的权威依据 |
| `doc/agents/coding.md` | 编码规范，其中「断言」「致命错误」两节决定测试与生产代码如何分工 |
| `vpr/test/main.cpp` | VPR 测试二进制的入口：用一行宏把 `main()` 交给 Catch2 |
| `vpr/CMakeLists.txt` | 定义 `test_vpr` 这个可执行目标，并把它注册给 CTest |
| `libs/EXTERNAL/CMakeLists.txt` | 引入第三方测试框架 `libcatch2` |
| `libs/libvtrutil/test/main.cpp` | 与 VPR 完全相同的入口套路，证明「每组件一个 main」模式 |
| `libs/libvtrutil/CMakeLists.txt` | `test_vtrutil` 的构建与注册，与 VPR 对照 |
| `vpr/test/test_flat_placement_types.cpp` | 一个干净极小的纯单元测试，适合作为 Catch2 写法的范本 |
| `vpr/test/test_xy_routing.cpp` | NoC 路由算法的单元测试，本讲「读懂断言」的实践对象 |
| `vpr/test/test_compressed_grid.cpp` | 一个偏重的、手工搭建 `g_vpr_ctx` 再断言的范例 |
| `Makefile` | 根目录包装层，`make test` 在这里转交给 CTest |

## 4. 核心概念与源码讲解

### 4.1 测试目录与二进制映射

#### 4.1.1 概念说明

VTR 有 **四个单元测试二进制**，分别属于四个组件。读者最容易踩的坑是：明明记得「VPR 有单元测试」，却不知道编译完以后可执行文件叫什么、躺在哪个目录。官方文档用一张「源到目标映射」表把这件事钉死——记住这张表，就记住了「改哪个目录的代码，要去跑哪个二进制」。

需要特别强调一个**陷阱**：官方 `testing.md` 里给出过一条 `./test_vpr connection_router binary_heap` 的示例命令，但当前仓库里**已经不存在**名为 `connection_router` / `binary_heap` 的测试用例了（在全树搜索无果）。这是文档落后于代码的典型案例。本讲下文会用**真实存在**的 NoC 路由测试来替代它。这也提醒一个工作习惯：遇到文档命令对不上时，用 `--list-tests` 自己列一遍最可靠。

#### 4.1.2 核心流程

四个组件各自走同一条流水线：

1. 源码躺在 `<组件>/test/*.cpp`，其中必有一个 `main.cpp` 提供 Catch2 入口。
2. 组件的 `CMakeLists.txt` 用 `file(GLOB_RECURSE TEST_SOURCES test/*.cpp)` 把目录下所有 `.cpp` 收进一个可执行目标（如 `test_vpr`）。
3. 该目标链接两样东西：被测组件的库（如 `libvpr`）和测试框架 `Catch2::Catch2WithMain`。
4. 用 `add_test(NAME ... COMMAND ...)` 把这个可执行目标注册进 CTest。
5. 用户在根目录敲 `make test`，Makefile 包装层转交给 CMake 生成的 Makefile，进而调用 CTest，CTest 依次跑所有 `add_test` 注册过的二进制。

整条链的关系可以画作：

```
<组件>/test/*.cpp  ──GLOB──▶  test_<comp> 可执行  ──add_test──▶  CTest ──▶  make test
        │                          │
        └─ main.cpp 提供 main()    └─ 链接 lib<comp> + Catch2::Catch2WithMain
```

#### 4.1.3 源码精读

**源到目标映射表**（这是本模块最重要的一段，请记牢）：

[doc/agents/testing.md:38-44](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/testing.md#L38-L44) — 官方列出四个映射：`vpr/test/`→`build/vpr/test_vpr`、`libs/libarchfpga/test/`→`build/libs/libarchfpga/test_archfpga`、`libs/libvtrutil/test/`→`build/libs/libvtrutil/test_vtrutil`、`utils/fasm/test/`→`build/utils/fasm/test_fasm`；并指出「新测试放进对应 `test/` 目录，并在该模块的 `CMakeLists.txt` 里注册」。

**Catch2 框架从哪来**——它属于外部子树（见 u1-l3 的外部子树规则），由外部总入口拉入：

[libs/EXTERNAL/CMakeLists.txt:14](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/EXTERNAL/CMakeLists.txt#L14) — `add_subdirectory(libcatch2)` 把 Catch2 作为子项目纳入构建，于是 `Catch2::Catch2WithMain` 这个链接目标全局可用。

**`test_vpr` 二进制如何定义**——这是「源→二进制」映射在 CMake 层的落点：

[vpr/CMakeLists.txt:272-284](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/CMakeLists.txt#L272-L284) — 用 `GLOB_RECURSE` 收集 `test/*.cpp`、用 `list(FILTER ... EXCLUDE REGEX ".*/test/gui/.*")` 把需要 Qt 的 GUI 测试排除掉，再 `add_executable(test_vpr ...)`、链接 `Catch2::Catch2WithMain` 与 `libvpr`，最后 `add_test(NAME test_vpr COMMAND test_vpr --colour-mode ansi ...)` 注册给 CTest。注意 `WORKING_DIRECTORY` 被设成 `test/` 源码目录，这也是为什么测试里能用相对路径引用 `lut.netlist`、`test_post_verilog_arch.xml` 等数据文件。

**对照：`test_vtrutil` 用的是同一套套路**，只是组件不同：

[libs/libvtrutil/CMakeLists.txt:144-151](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/CMakeLists.txt#L144-L151) — 同样 `GLOB_RECURSE test/*.cpp` → `add_executable(test_vtrutil ...)` → 链接 `Catch2::Catch2WithMain` → `add_test`。libarchfpga（`test_archfpga`）、fasm（`test_fasm`）的写法一致，不再赘述。

**`make test` 为什么能跑起来**——根 Makefile 转发 + 强制打印失败日志：

[Makefile:113-114](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/Makefile#L113-L114) — 注释「Show test log on failures with 'make test'」，下面 `export CTEST_OUTPUT_ON_FAILURE=TRUE`。这意味着 `make test` 失败时会把失败用例的完整输出直接打到终端，不用再手动翻 CTest 日志。`make test` 本身是「转发目标」（见 u1-l2 包装层），它最终调用的就是 CMake 生成的 `test` 目标，即 CTest。

#### 4.1.4 代码实践

**实践目标**：不写一行新代码，仅靠阅读 CMake 与文档，把「源→二进制」映射在自己脑子里跑一遍，并验证 GUI 测试的「条件编译」分支。

**操作步骤**：

1. 打开 `doc/agents/testing.md`，抄下那张四行映射表。
2. 打开 `vpr/CMakeLists.txt` 第 272–284 行，确认 `test_vpr` 的源来自 `GLOB_RECURSE test/*.cpp` 且 GUI 被排除。
3. 打开 `vpr/CMakeLists.txt` 第 289–291 行，阅读 `if (VPR_USE_EZGL STREQUAL "on") add_subdirectory(test/gui) endif()`。这是 GUI 测试（`vpr/test/gui/*.cpp`）只在开启图形支持时才编译、否则根本不进 `test_vpr` 的原因——它解释了为什么 `--list-tests` 的结果会因构建选项不同而不同。

**需要观察的现象**：GUI 测试二进制与主 `test_vpr` 是分离的；不开 EZGL 时，`test/gui/` 下的几十个用例（如 `[vpr_gui]`、`[layer3]`）根本不会被编译。

**预期结果**：你能口头说出「我改了 `libs/libvtrutil/src/vtr_vector.h`，要跑的是 `build/libs/libvtrutil/test_vtrutil`；我改了 `vpr/src/route/*`，要跑的是 `build/vpr/test_vpr`」。本步骤为纯阅读，无需运行，故结论确定。

### 4.2 Catch2 用法

#### 4.2.1 概念说明

有了二进制，下一步是看懂里面的测试代码。Catch2 的心智模型很小，只有四个核心概念：

- **TEST_CASE**：一个测试用例，参数是「自由文本名字 + 一个或多个 `[标签]`」。名字给人看，标签给过滤器用。
- **SECTION**：用例内部的「分场景」。Catch2 有个反直觉但强大的特性——**每个 `SECTION` 都会从所在 `TEST_CASE` 的开头重新执行一遍**，相当于编译器自动帮你把公共构造代码复制到每个分支前。所以把「搭测试夹具」写在 SECTION 之前、把「断言」写在 SECTION 之内，是最地道的写法。
- **REQUIRE / REQUIRE_NOTHROW 等断言宏**：`REQUIRE(expr)` 是硬断言，失败立即终止当前 SECTION；`REQUIRE_NOTHROW(expr)` 断言表达式不抛异常；浮点比较用 `Catch::Approx(x)` 容忍误差。
- **main 入口**：某个 `.cpp` 里写 `#define CATCH_CONFIG_MAIN` 再 `#include "catch2/catch_test_macros.hpp"`，框架就据此生成 `main()`。每个测试二进制有且只有一个这样的 `main.cpp`。

#### 4.2.2 核心流程

一个典型用例的执行流程：

1. Catch2 的 `main()`（由 `CATCH_CONFIG_MAIN` 生成）扫描命令行：`--list-tests` 列出所有用例；`[tag]` 或 `"名字"` 做过滤；无参数则全跑。
2. 对每个被选中的 `TEST_CASE`，从其函数体顶部开始执行。
3. 遇到第一个 `SECTION`，进入并执行其体；体结束后，**回到 `TEST_CASE` 顶部**，跳过已执行过的 SECTION，进入下一个 SECTION。
4. 执行中遇到 `REQUIRE` 失败 → 该 SECTION 标记为失败、立即中断，统计 +1 失败；全部 SECTION 都过 → 用例标绿。
5. 最后打印汇总：通过数、失败数、耗时。

#### 4.2.3 源码精读

**入口：把 main 交给框架**——整份文件只有两行：

[vpr/test/main.cpp:1-2](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/test/main.cpp#L1-L2) — `#define CATCH_CONFIG_MAIN` 紧跟 `#include "catch2/catch_test_macros.hpp"`。这就是 VPR 测试二进制的全部「启动代码」。`libvtrutil` 的入口一字不差：[libs/libvtrutil/test/main.cpp:1-2](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/test/main.cpp#L1-L2)，可见「每组件一个 main.cpp」是机械重复的模式。

**一个干净的范本**——结构体算术运算的纯单元测试：

[vpr/test/test_flat_placement_types.cpp:14-47](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/test/test_flat_placement_types.cpp#L14-L47) — 一个 `TEST_CASE("test_t_flat_pl_loc", "[vpr_flat_pl_types]")` 内含三个 SECTION（加法、除法、二者组合）。它演示了三件事：标签 `[vpr_flat_pl_types]` 可用于过滤；浮点结果用 `Catch::Approx(5.0f)` 而非直接 `==` 比较，避免精度抖动误报；每个 SECTION 共享顶部的构造、互不污染。这是读懂 Catch2 的最佳起点。

**带「黄金路径」比对的算法测试**——本讲实践要精读的对象，NoC 的 XY 路由测试。先看它的辅助比较函数：

[vpr/test/test_xy_routing.cpp:17-33](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/test/test_xy_routing.cpp#L17-L33) — `compare_routes()` 先用 `REQUIRE(found_path.size() == golden_path.size())` 断言两路径等长，再逐条 `REQUIRE` 比对每段 link 的源/宿路由器是否一致。这种「手工构造期望路径（golden），再和算法实际输出逐元素比对」的写法，是算法类单元测试的主流套路。

再看同一个用例的入口与一个分场景：

[vpr/test/test_xy_routing.cpp:35-105](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/test/test_xy_routing.cpp#L35-L105) — `TEST_CASE("test_route_flow", "[vpr_noc_xy_routing]")` 顶部花大量篇幅手工搭一个 4×4 mesh NoC（`NocStorage` 加 16 个路由器、加若干 `add_link`，再 `finished_building_noc()` 冻结），这正是 u9-l1 / u8-l2 里 NoC 模型的迷你版；随后在若干 SECTION 里分别测「同行」「同列」「先横后纵」等走线。第 92–105 行的 SECTION 用 `REQUIRE_NOTHROW(routing_algorithm.route_flow(...))` 断言不抛异常、`REQUIRE(found_path.empty() == true)` 断言起终点相同时路径为空——这两条就是典型的「行为契约」断言。

**一个偏重的「搭全局上下文」测试**：

[vpr/test/test_compressed_grid.cpp:42-48](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/test/test_compressed_grid.cpp#L42-L48) 及 [第 170-182 行的 SECTION](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/test/test_compressed_grid.cpp#L170-L182) — 它直接写 `g_vpr_ctx.mutable_device().logical_block_types`、`g_vpr_ctx.mutable_device().grid`，手工塞入若干 `t_physical_tile_type` 再调用 `create_compressed_block_grids()`，最后用一堆 `REQUIRE(... .size() == 100)` 断言压缩网格的尺寸。这类测试证明：单元测试也能驱动全局上下文 `g_vpr_ctx`（u3-l4），只是夹具更重。

#### 4.2.4 代码实践

**实践目标**：精读 `test_xy_routing.cpp` 的断言逻辑，理解「黄金路径比对」这一算法测试套路。

**操作步骤**：

1. 打开 `vpr/test/test_xy_routing.cpp`，阅读第 17–33 行的 `compare_routes()`：注意它比较的是 link 的源/宿路由器，而非 link 的 ID——因为同一个 NoC 里 link 的存储顺序可能与构造顺序不同，比语义（连接了哪两个路由器）才稳健。
2. 跳到第 107–131 行的「同行」SECTION：先手工用循环构造从路由器 7 到 4 的 `golden_path`，再调用算法得到 `found_path`，最后 `compare_routes(golden_path, found_path, noc_model)` 比对。
3. 问自己一个问题：如果 XY 路由实现里把「先横后纵」写反成「先纵后横」，哪个 SECTION 会失败？（答案：第 157 行那个「先横后纵」的 SECTION，因为它的 golden 是先水平后垂直。）

**需要观察的现象**：每个 SECTION 都「自带完整夹具」地独立运行；断言失败只会红掉当前 SECTION，不会连累其它 SECTION。

**预期结果**：你能向别人解释「这个测试不依赖任何外部文件，它在内存里手工搭了一个 4×4 NoC，然后拿算法输出和手工算出的期望路径逐段比对」。本步骤为纯源码阅读，结论确定。

### 4.3 新增测试流程

#### 4.3.1 概念说明

知道怎么读之后，最后一步是「怎么加」。VTR 的新增流程**比直觉更简单**：因为 `GLOB_RECURSE test/*.cpp` 是按文件名通配收录的，绝大多数情况下你**不需要手动把文件名登记进 CMakeLists**——只要新建一个符合命名约定的 `.cpp` 丢进 `test/` 目录即可。唯一需要手动改 CMake 的特例是 `utils/fasm`，它显式列出了源文件清单。

但有一个**必须注意的 CMake 陷阱**：`file(GLOB_RECURSE ...)` 的结果在 CMake **配置阶段**被求值并写进构建系统。新增/删除文件后，CMake 不会自动察觉，必须**重新跑一次 CMake 配置**（`make` 时 CMake 通常会自动 detect 并重配置，但偶尔需要手动 `cmake build/` 一次）才会把新文件纳入编译。新人最常见的「我加了测试却编译不到」，九成是这个原因。

#### 4.3.2 核心流程

新增一个 VPR 单元测试的标准动作：

1. **确定归属组件**：被测代码在 `vpr/src/` 下 → 测试放 `vpr/test/`；在 `libs/libvtrutil/` 下 → 放 `libs/libvtrutil/test/`。选错目录会导致测试二进制链接不到被测库的内部符号。
2. **新建文件**：命名遵循 `test_<主题>.cpp`（见 u9-l1 提到的 `snake_case` 文件名规范，coding.md 亦有规定）。文件内写 `TEST_CASE`，标签建议复用既有的 `[vpr]` 或新建一个 `[vpr_<主题>]`。
3. **（通常可省略）改 CMake**：VPR/libarchfpga/libvtrutil 用 glob，无需改；唯独 `utils/fasm/CMakeLists.txt` 第 37–41 行显式列了源文件，新增 fasm 测试要把文件名加进 `TEST_SOURCES`。
4. **触发重配置 + 构建**：`make`（必要时先 `cmake build/`），产物会编进 `test_vpr`（或对应组件二进制）。
5. **运行与过滤**：`make test` 全跑；或直接 `cd build/vpr && ./test_vpr "[vpr_<主题>]"` 只跑你的标签，迭代更快。

#### 4.3.3 源码精读

**glob 自动收录（无需登记）**：

[vpr/CMakeLists.txt:272-276](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/CMakeLists.txt#L272-L276) — `file(GLOB_RECURSE TEST_SOURCES test/*.cpp)` + `add_executable(test_vpr ${TEST_SOURCES})`。这就是「丢文件即收录」的依据：任何 `vpr/test/*.cpp` 都会自动成为 `test_vpr` 的源。libarchfpga 第 66–67 行、libvtrutil 第 144–145 行同理。

**例外：fasm 要手动登记**：

[utils/fasm/CMakeLists.txt:37-44](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/utils/fasm/CMakeLists.txt#L37-L44) — 这里把 `test/main.cpp`、`test_fasm.cpp`、`test_lut.cpp`、`test_parameters.cpp`、`test_utils.cpp` 一一列进 `TEST_SOURCES`，再 `add_executable(test_fasm ...)`。所以在 fasm 里加测试，**必须**把新文件名追加进这个列表，否则不会被编译。这是四个组件里唯一的例外，记住即可。

**官方对新测试落点的规定**：

[doc/agents/testing.md:44](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/testing.md#L44) — "New unit tests go in the corresponding `test/` directory and are registered in the module's `CMakeLists.txt`."（新单元测试放进对应的 `test/` 目录，并在该模块的 `CMakeLists.txt` 中注册。）「注册」在 glob 模式下是自动的，在显式列表模式下才是手动的。

**断言风格要与生产代码区分**——coding.md 明确划清 `VTR_ASSERT`（防御性，常开）与 `VPR_FATAL_ERROR`（致命错误）的边界，单元测试里则应一律用 Catch2 的 `REQUIRE`：

[doc/agents/coding.md:46-60](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/coding.md#L46-L60) — 生产代码不得用 `VTR_ASSERT(false)` 当错误处理（那是给「设计上不可达」路径用的）。写测试时同理：用 `REQUIRE` 表达「我期望这个事实成立」，而不是塞 `VTR_ASSERT`——后者失败会直接 `abort` 整个进程，剥夺 Catch2 收集其余用例结果的能力。

#### 4.3.4 代码实践

**实践目标**：亲手新增一个最小单元测试，跑通「写文件 → 自动收录 → 过滤执行」全链路。本实践需要本地构建环境；若当前环境未编译 VTR，标记为「待本地验证」。

**操作步骤**：

1. 在 `vpr/test/` 下新建文件 `test_my_first.cpp`，内容如下（**示例代码**，非项目原有代码）：
   ```cpp
   #include "catch2/catch_test_macros.hpp"
   #include <vector>

   namespace {
   TEST_CASE("my_first_unit_test", "[vpr][vpr_my_first]") {
       std::vector<int> v{1, 2, 3};
       SECTION("size is three") {
           REQUIRE(v.size() == 3);
       }
       SECTION("sum is six") {
           int sum = 0;
           for (int x : v) sum += x;
           REQUIRE(sum == 6);
       }
   }
   } // namespace
   ```
2. 在仓库根目录运行 `make`（若新文件未被识别，运行 `cmake build/` 强制重配置后再 `make`）。
3. 执行 `cd build/vpr && ./test_vpr --list-tests 2>&1 | grep my_first`，确认你的用例已被收录。
4. 只跑你的标签：`cd build/vpr && ./test_vpr "[vpr_my_first]"`。

**需要观察的现象**：第 3 步应列出两条（`my_first_unit_test` 被列出）；第 4 步应报告 2 个 SECTION 全部通过。若第 3 步列不出，说明 CMake 没重配置——回到第 2 步的 `cmake build/`。

**预期结果**：你看到一个绿色汇总，证明新文件被 glob 自动收录并编译。由于依赖本地构建，运行结果为「待本地验证」；但「glob 自动收录、`--list-tests` 可见、按标签过滤」这三条机制由源码决定，结论确定。

#### 4.3.5 小练习与答案

**练习 1**：你在 `libs/libvtrutil/src/` 新增了一个工具函数，想给它写单元测试。新建的 `test_*.cpp` 应放哪个目录？编译后会进哪个二进制？要不要改 CMake？

> **答案**：放 `libs/libvtrutil/test/`；编译后进 `build/libs/libvtrutil/test_vtrutil`；libvtrutil 用 `GLOB_RECURSE`，通常**不用**改 CMake，但若新文件没被编译，跑一次 `cmake build/` 重配置即可。

**练习 2**：同事抱怨「我在 `utils/fasm/` 加了测试文件却编译不到」，最可能的原因是什么？和 vpr 的情况有何不同？

> **答案**：`utils/fasm/CMakeLists.txt` 第 37–41 行是**显式源文件列表**而非 glob，新增 fasm 测试必须把文件名手动加进 `TEST_SOURCES` 才会被编译；而 vpr/libarchfpga/libvtrutil 用 glob 自动收录，无需登记。

**练习 3**：为什么单元测试里应该用 `REQUIRE` 而不是 `VTR_ASSERT(false)` 来表达「预期失败」？

> **答案**：`REQUIRE` 失败只标记当前 SECTION 失败并继续收集其它用例结果；`VTR_ASSERT(false)` 会直接 `abort` 整个测试进程，剥夺 Catch2 汇总报告，也违背 coding.md「断言不是错误处理」的规定。

## 5. 综合实践

把本讲三个模块串起来，完成一次「定位 → 读懂 → 验证」的完整闭环。**目标**：官方文档里那条 `./test_vpr connection_router binary_heap` 示例已失效，请你用真实存在的测试把它替换掉，并证明你的替换有效。

**任务**：

1. **定位**：运行 `cd build/vpr && ./test_vpr --list-tests`，搜索所有与「routing（路由）」相关的用例。你会找到三个 NoC 路由算法的测试标签：`[vpr_noc_xy_routing]`、`[vpr_noc_bfs_routing]`、`[vpr_noc_odd_even_routing]`（对应 u8-l2 讲过的 XY / BFS / 奇偶回转三种 NoC 路由算法）。
2. **读懂**：打开 `vpr/test/test_xy_routing.cpp`，精读第 17–33 行的 `compare_routes()` 与第 35–225 行的若干 SECTION，画出「构造 4×4 mesh → 给定起终点 → 算法求路径 → 与 golden_path 逐段比对」的流程。回答：为什么比对的是 link 的源/宿路由器而不是 link 的 ID？
3. **验证**：只跑这个标签 `./test_vpr "[vpr_noc_xy_routing]"`，记录通过的 SECTION 数；再故意把其中一个 SECTION 的 `golden_path` 构造顺序改反（仅作本地实验，**切勿提交**），重新运行，观察 Catch2 如何精确指出哪一条 `REQUIRE` 失败。

**验收标准**：你能口头说出 (a) 三个路由测试各自的标签；(b) `compare_routes` 为什么比语义而非 ID；(c) 一个被故意改错的 golden 会让 Catch2 报出哪一行。运行部分（步骤 1、3）依赖本地构建，结果「待本地验证」；阅读与推理部分（步骤 2、验收 a/b）结论确定。

> 提醒：本实践只读不改源码；步骤 3 里「改 golden」是本地学习用，实验后务必用 `git checkout -- vpr/test/test_xy_routing.cpp` 还原，不要污染工作区，更不要提交。

## 6. 本讲小结

- VTR 有**四个**单元测试二进制，源到目标映射为：`vpr/test/`→`test_vpr`、`libs/libarchfpga/test/`→`test_archfpga`、`libs/libvtrutil/test/`→`test_vtrutil`、`utils/fasm/test/`→`test_fasm`；GUI 测试 `vpr/test/gui/` 是仅在开启 EZGL 时单独编译的第五支。
- 每个二进制由一个 `main.cpp` 用 `#define CATCH_CONFIG_MAIN` 提供入口，源码用 `GLOB_RECURSE test/*.cpp` 收集，链接被测库与 `Catch2::Catch2WithMain`，再经 `add_test` 注册给 CTest；`make test` 经 Makefile 包装层调用 CTest，并靠 `CTEST_OUTPUT_ON_FAILURE=TRUE` 直接打出失败日志。
- Catch2 的核心是 `TEST_CASE`（名字+`[标签]`）、`SECTION`（每个从用例顶部重新执行）、`REQUIRE`/`REQUIRE_NOTHROW`/`Catch::Approx`（断言）；算法测试常用「手工构造 golden 路径再逐元素比对」的套路（见 `test_xy_routing.cpp` 的 `compare_routes`）。
- 新增测试通常**只需**在对应 `test/` 目录丢一个 `test_*.cpp`，glob 会自动收录（fasm 是需手动登记的例外）；新增文件后若没被编译，跑一次 `cmake build/` 重配置即可。
- 官方文档里的 `connection_router binary_heap` 示例已与当前代码脱节，真实存在的路由单元测试是 NoC 路由那三个；遇文档对不上，以 `--list-tests` 为准。
- 测试里用 `REQUIRE` 表达预期，不要用 `VTR_ASSERT(false)`——后者会 `abort` 整个进程、违背 coding.md 的断言规范。

## 7. 下一步学习建议

- **横向扩展**：VTR 还有一整套**回归测试**体系（`run_vtr_task.py`、黄金结果比对、QoR 对比），那是覆盖端到端 CAD 行为的另一条腿，正好是下一讲 **u9-l3 回归测试与 QoR 评估** 的主题。
- **纵向深挖**：想看「带全局上下文的重量级单元测试」如何手工搭建 `g_vpr_ctx`，精读 `vpr/test/test_compressed_grid.cpp` 与 `vpr/test/test_noc_storage.cpp`；想看纯算法的轻量测试，精读 `vpr/test/test_flat_placement_types.cpp` 与 `vpr/test/test_xy_routing.cpp`。
- **动手建议**：挑一个你在 u3~u7 学过的数据结构（如 `ClusteredNetlist` 的 `find_block_by_name_fragment`），仿照 `test_clustered_netlist.cpp` 的风格，给它补一个 `test_*.cpp`，走通「写文件 → 自动收录 → 标签过滤执行」全链路，把本讲变成肌肉记忆。
