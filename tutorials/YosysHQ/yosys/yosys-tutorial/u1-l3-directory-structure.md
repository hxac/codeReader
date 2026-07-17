# 顶层目录结构地图

## 1. 本讲目标

本讲带你建立一张「Yosys 源码的全局地图」。学完后你应该能够：

- 说出 Yosys 顶层每一个目录（`kernel`、`frontends`、`passes`、`backends`、`techlibs`、`libs`、`tests`、`docs`、`examples` 等）的职责。
- 理解为什么 Yosys 要把代码分成「核心 IR 层」与「前端 / 后端 / Pass 三层」。
- 当你听到一个功能名（例如「读 Verilog」「优化」「写网表」「Xilinx 工艺库」）时，能立刻定位到它对应的源码目录，并能在该目录中找到一个代表文件加以确认。

本讲不深入任何一个模块的内部实现，而是给你一张「导航图」。后续每一讲都是在地图上点亮某一块区域。

## 2. 前置知识

在开始前，请确认你理解上一讲（u1-l1）引入的几个词。这里只做最简短的回顾，不展开：

- **HDL / RTL**：硬件描述语言（如 Verilog）与寄存器传输级，是设计的输入形式。
- **综合（synthesis）**：把 RTL 翻译成门级或工艺门级网表的过程。
- **RTLIL**：Yosys 内部的统一中间表示（Intermediate Representation），所有前端读进来的东西、所有后端要写出去的东西，中间都被表达成 RTLIL。
- **Pass（变换算法）**：一段接受 RTLIL、输出 RTLIL 的代码，比如 `opt`（优化）、`techmap`（工艺映射）、`proc`（把 `always` 块变成网表）。综合就是「把许多 pass 串起来跑」。
- **前端（Frontend）/ 后端（Backend）**：前端负责把外部格式（Verilog、JSON…）读成 RTLIL；后端负责把 RTLIL 写成外部格式（Verilog、SMT2、JSON…）。

一个贯穿全讲的关键直觉是「**数据流分层**」：

```text
   外部 HDL                 内部表示                   外部格式
 ┌─────────┐   读入    ┌──────────┐   一串 pass   ┌──────────┐   写出   ┌─────────┐
 │ Verilog │ ────────▶ │  RTLIL   │ ────────────▶ │  RTLIL   │ ───────▶ │ 网表/…  │
 └─────────┘  frontends └──────────┘    passes    └──────────┘ backends └─────────┘
                                 ▲
                                 │ 由 kernel 定义与维护
```

`kernel` 定义并维护中间的 RTLIL，`frontends`/`passes`/`backends` 三个目录分别对应「读入」「变换」「写出」三个阶段。本讲要做的，就是把这张图落到磁盘上的真实目录里。

## 3. 本讲源码地图

本讲主要阅读以下文件（重点是各目录的 `CMakeLists.txt`，因为它们是该目录「装了哪些东西」的权威清单）：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/README.md#L1-L22) | 项目自述，定位 Yosys 是「RTL 综合框架」，并提到第三方代码在 `abc` 与 `libs` 子目录 |
| [CMakeLists.txt](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/CMakeLists.txt#L393-L400) | 顶层构建脚本，用 `add_subdirectory` 把各大目录挂到一起，是「目录总目录」 |
| [kernel/CMakeLists.txt](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/CMakeLists.txt#L17-L155) | 核心层清单：RTLIL、命令注册、日志、线程、SAT 等全在这里登记 |
| [frontends/CMakeLists.txt](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/CMakeLists.txt#L1-L13) | 前端清单：verilog、slang、rtlil、json、blif、liberty… |
| [passes/CMakeLists.txt](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/CMakeLists.txt#L1-L11) | Pass 清单：opt、proc、memory、techmap、fsm、sat、equiv… |
| [backends/CMakeLists.txt](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/CMakeLists.txt#L1-L18) | 后端清单：verilog、rtlil、json、smt2、cxxrtl、aiger… |

为了让讲解落到实处，本讲还会各挑一个「代表文件」加以确认：读 Verilog 用 [frontends/verilog/verilog_frontend.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_frontend.cc#L72-L73)、优化用 [passes/opt/opt.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt.cc#L28-L29)、写网表用 [backends/verilog/verilog_backend.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/verilog/verilog_backend.cc#L2494-L2495)。

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：

- **4.1** `kernel` 核心层：IR、运行时与命令注册都在这里。
- **4.2** 前端 / 后端 / Pass 三层：实现「读入 / 变换 / 写出」。
- **4.3** `techlibs` / `libs` 资源层：提供工艺库与第三方依赖。

### 4.1 kernel 核心层

#### 4.1.1 概念说明

`kernel` 是整个 Yosys 的「地基」。无论你用哪种前端、哪种后端、哪些 pass，它们都在和 `kernel` 里定义的同一套东西打交道：

- **RTLIL 数据结构**：`Design`、`Module`、`Wire`、`Cell`、`SigSpec` 等（详见 u2、u3）。
- **命令系统**：`Pass`/`Frontend`/`Backend` 基类与全局注册表 `pass_register`（u1-l2 已提到它的自动注册机制）。
- **运行时基础设施**：日志（`log.cc`）、线程（`threading.cc`）、SAT 编码（`satgen.cc`）、自研高性能容器（`hashlib.h`）等。

正因为 `kernel` 同时持有「数据表示」和「命令系统」，所以它是三层（前端/后端/Pass）共同的依赖：三层都 `#include "kernel/rtlil.h"`，并通过 `kernel/register.h` 注册自己的命令。这就解释了为什么 `kernel` 在顶层 `CMakeLists.txt` 里必须比三层更早被构建。

#### 4.1.2 核心流程

`kernel` 自身产出两个目标（target）：

1. **核心库 / 组件**：以 `yosys_core(kernel ...)` 登记的那一大堆文件，是 `libyosys` 的主体。
2. **可执行入口**：`yosys_core(driver driver.cc ...)`，即上一讲（u1-l2）分析的 `main()` 所在文件，负责命令行解析与调度。

这两者合起来构成「`yosys` 可执行程序 + `libyosys` 库」。

#### 4.1.3 源码精读

顶层构建脚本用 `add_subdirectory` 把各大目录按依赖顺序串起来，`kernel` 排在三层之前：

```cmake
# CMakeLists.txt（顶层）
add_subdirectory(libs)      # 先建第三方依赖
add_subdirectory(kernel)    # 再建核心（IR + 命令系统）
add_subdirectory(passes)    # 三层都依赖 kernel
add_subdirectory(frontends)
add_subdirectory(backends)
add_subdirectory(techlibs)
```

这几行的顺序就是「依赖自底向上」的写照：[CMakeLists.txt:L393-L398](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/CMakeLists.txt#L393-L398)。紧随其后的 [CMakeLists.txt:L400](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/CMakeLists.txt#L400) 是条件挂载的 `pyosys`（Python 绑定），[CMakeLists.txt:L533](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/CMakeLists.txt#L533) 是条件挂载的单元测试 `tests/unit`。

在 `kernel/CMakeLists.txt` 里，核心组件用一个 `yosys_core(kernel ...)` 调用集中登记，开头几项就是本系列后续要逐个深入的对象：

```cmake
# kernel/CMakeLists.txt
yosys_core(kernel
	binding.cc
	cellaigs.cc        # 单元 → AIG 转换
	celltypes.h        # 内部单元库的端口定义
	constids.inc       # 标识符登记（ID 宏）
	functional.cc      # functional IR
	hashlib.h          # 自研 dict/pool 容器
	log.cc             # 日志系统
	register.cc        # Pass/Frontend/Backend 注册表
	rtlil.cc           # ★ RTLIL 数据结构核心实现
	satgen.cc          # RTLIL → SAT 编码
	threading.cc       # 多线程支持
	yosys.cc           # setup/shutdown 生命周期
	...                # 还有几十个文件
	ESSENTIAL          # 标记为「核心必需」组件
)
```

完整清单见 [kernel/CMakeLists.txt:L17-L155](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/CMakeLists.txt#L17-L155)，注意末尾的 `ESSENTIAL` 关键字，表示无论用户如何裁剪组件，这一块都会被保留。

驱动入口 `driver.cc` 单独成块，并且只在「不是只构建 Python」时编译：

```cmake
# kernel/CMakeLists.txt
if (NOT YOSYS_BUILD_PYTHON_ONLY)
	yosys_core(driver
		driver.cc        # main() 所在
		...
		BOOTSTRAP
	)
endif()
```

见 [kernel/CMakeLists.txt:L176-L190](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/CMakeLists.txt#L176-L190)。这说明 `libyosys`（核心库）可以脱离命令行 `driver.cc` 单独存在——这正是后续 u9「把 Yosys 当作库嵌入」的前提。

#### 4.1.4 代码实践

> 实践目标：用眼睛在 `kernel/CMakeLists.txt` 里「点名」，建立「核心层包含哪些大件」的直觉。

操作步骤：

1. 打开 [kernel/CMakeLists.txt](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/CMakeLists.txt#L17-L155)。
2. 在 `yosys_core(kernel ...)` 的文件列表里，找到下面这些后续要学的文件，并在心里标记它属于哪一类：
   - `rtlil.cc` / `rtlil.h` → RTLIL 数据结构（u2、u3）
   - `register.cc` / `register.h` → 命令注册（u4）
   - `log.cc` / `log.h` → 日志（u4-l4）
   - `satgen.cc` → SAT 编码（u10）
   - `hashlib.h` → 高性能容器（u3-l3）
   - `cellaigs.cc` / `functional.cc` → 高级表示（u10）
3. 确认 `driver.cc` 出现在另一个 `yosys_core(driver ...)` 块里，而不是核心块里。

需要观察的现象：核心块里没有任何具体前端/后端/pass 的实现文件（找不到 `verilog_frontend.cc`、`opt.cc` 等），全是「被共享的基础设施」。这就是 `kernel` 作为「地基」的体现。

预期结果：你能一句话回答「`rtlil.cc` 为什么在 `kernel` 而不是在 `frontends`？——因为 RTLIL 是所有前端/后端/pass 共享的表示，归属核心层。」

#### 4.1.5 小练习与答案

**练习 1**：为什么顶层 `CMakeLists.txt` 里 `add_subdirectory(kernel)` 必须在 `add_subdirectory(frontends)` 之前？

> 参考答案：因为前端、后端、Pass 三层都依赖 `kernel` 定义的 RTLIL 数据结构与命令注册基类（`Pass`/`Frontend`/`Backend`）。CMake 中被依赖的目标需要先被加入构建，所以 `kernel` 必须排在前面。

**练习 2**：`driver.cc` 和 `rtlil.cc` 同样在 `kernel` 目录下，为什么 CMake 里把它们放进两个不同的 `yosys_core(...)` 块？

> 参考答案：`rtlil.cc` 等构成核心库 `libyosys`（标记 `ESSENTIAL`，始终构建）；`driver.cc` 是命令行可执行入口，仅在 `NOT YOSYS_BUILD_PYTHON_ONLY` 时构建。把入口与核心库分离，使得 `libyosys` 可以被其他程序（如 Python 绑定、嵌入式 API）单独链接。

### 4.2 前端 / 后端 / Pass 三层

#### 4.2.1 概念说明

围绕 `kernel` 的是三个并列的目录，各自承担数据流的一个阶段：

- **`frontends/`**：把外部格式读成 RTLIL。`frontends/CMakeLists.txt` 列出全部前端，包括经典的 `verilog`、用于 SystemVerilog 的 `slang`（依赖 sv-elab + slang）、商业的 `verific`，以及 `rtlil`/`json`/`blif`/`liberty`/`aiger` 等。
- **`passes/`**：在 RTLIL 上做变换。`passes/CMakeLists.txt` 列出 `opt`（优化）、`proc`（行为级转网表）、`memory`（存储器）、`techmap`（工艺映射）、`fsm`（状态机）、`hierarchy`（层次）、`sat`/`equiv`（形式验证）、`cmds`（通用命令）、`pmgen`（模式匹配生成器）等。
- **`backends/`**：把 RTLIL 写成外部格式。`backends/CMakeLists.txt` 列出 `verilog`、`rtlil`、`json`、`smt2`（形式验证）、`cxxrtl`（C++ 仿真）、`aiger`、`btor`、`edif`、`firrtl` 等。

三层的关系是「平级 + 都依赖 `kernel`」，它们之间通过 RTLIL 这一公共语言协作。

#### 4.2.2 核心流程

三层在 CMake 里都是「一个目录 = 一组 `add_subdirectory`」，每个子目录通常对应一类前端/后端/pass。你可以把 `passes/CMakeLists.txt` 这样的文件当成「该层的目录索引」来读：

```text
frontends/CMakeLists.txt   →  verilog, slang, verific, rtlil, json, blif, liberty, aiger, aiger2, ast, rpc
passes/CMakeLists.txt      →  cmds, hierarchy, proc, opt, memory, techmap, fsm, sat, equiv, pmgen, tests
backends/CMakeLists.txt    →  verilog, rtlil, json, smt2, cxxrtl, aiger, aiger2, btor, edif, firrtl, functional, simplec, smv, spice, table, intersynth, jny
```

注意 `frontends/CMakeLists.txt` 里的 `slang` 子目录是条件加入的——这呼应了 u1-l1 讲到的「SystemVerilog 支持依赖 slang，可关闭」。

#### 4.2.3 源码精读

前端清单（注意 `slang` 被 `if (NOT YOSYS_WITHOUT_SLANG)` 包裹）：

```cmake
# frontends/CMakeLists.txt
add_subdirectory(verilog)      # 经典 Verilog 前端（read_verilog）
add_subdirectory(ast)          # Verilog 用的 AST 节点
add_subdirectory(rtlil)        # read_rtlil
add_subdirectory(json)         # read_json
add_subdirectory(blif)         # read_blif
add_subdirectory(liberty)      # read_liberty
add_subdirectory(aiger)
add_subdirectory(aiger2)
add_subdirectory(rpc)
if (NOT YOSYS_WITHOUT_SLANG)
    add_subdirectory(slang)    # SystemVerilog（依赖 slang/sv-elab），可关闭
endif()
add_subdirectory(verific)      # 商业 Verific 前端
```

见 [frontends/CMakeLists.txt:L1-L13](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/CMakeLists.txt#L1-L13)。

Pass 清单（每个都是一类综合变换）：

```cmake
# passes/CMakeLists.txt
add_subdirectory(cmds)       # select/show/stat/clean 等通用命令
add_subdirectory(hierarchy)  # hierarchy/flatten/submod
add_subdirectory(proc)       # always 块 → 网表
add_subdirectory(opt)        # 网表优化
add_subdirectory(memory)     # 存储器推断与映射
add_subdirectory(techmap)    # 工艺映射（含 abc9）
add_subdirectory(fsm)        # 状态机
add_subdirectory(sat)        # SAT 相关命令
add_subdirectory(equiv)      # 等价检查
add_subdirectory(pmgen)      # 模式匹配生成器
add_subdirectory(tests)      # 仅供测试的 pass
```

见 [passes/CMakeLists.txt:L1-L11](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/CMakeLists.txt#L1-L11)。

后端清单（数目最多，覆盖各种输出格式）：

```cmake
# backends/CMakeLists.txt
add_subdirectory(verilog)    # write_verilog
add_subdirectory(rtlil)      # write_rtlil
add_subdirectory(json)       # write_json
add_subdirectory(smt2)       # write_smt2（形式验证）
add_subdirectory(cxxrtl)     # write_cxxrtl（C++ 仿真）
add_subdirectory(aiger)      # write_aiger
add_subdirectory(btor)       # write_btor
add_subdirectory(edif)
add_subdirectory(firrtl)
...
```

见 [backends/CMakeLists.txt:L1-L18](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/CMakeLists.txt#L1-L18)。

为了让「目录 → 命令」的对应落到具体代码，各看一行「代表文件」里的命令注册。它们都继承自 `kernel` 里的基类，并在构造时把自己的名字登记进去：

```cpp
// frontends/verilog/verilog_frontend.cc ——「读 Verilog」就来自这里
struct VerilogFrontend : public Frontend {
	VerilogFrontend() : Frontend("verilog", "read modules from Verilog file") { }
```

见 [frontends/verilog/verilog_frontend.cc:L72-L73](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_frontend.cc#L72-L73)。

```cpp
// passes/opt/opt.cc ——「优化」就来自这里
struct OptPass : public Pass {
	OptPass() : Pass("opt", "perform simple optimizations") { }
```

见 [passes/opt/opt.cc:L28-L29](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt.cc#L28-L29)。

```cpp
// backends/verilog/verilog_backend.cc ——「写网表」就来自这里
struct VerilogBackend : public Backend {
	VerilogBackend() : Backend("verilog", "write design to Verilog file") { }
```

见 [backends/verilog/verilog_backend.cc:L2494-L2495](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/verilog/verilog_backend.cc#L2494-L2495)。

这三行是「三层划分」最直接的证据：一个继承 `Frontend`、一个继承 `Pass`、一个继承 `Backend`，三个基类都定义在 `kernel`。这就是分层在源码里的物理体现。

#### 4.2.4 代码实践

> 实践目标：验证「读 Verilog / 优化 / 写网表」三件事分别落在三个不同目录，且都通过 `kernel` 的基类注册。

操作步骤：

1. 在仓库根目录运行下面的查找，分别定位三类命令的注册点（示例代码，可在本地执行）：

   ```bash
   # 「读 Verilog」属于前端
   grep -rn 'Frontend("verilog"' frontends/
   # 「优化」属于 pass
   grep -rn 'Pass("opt"' passes/
   # 「写网表」属于后端
   grep -rn 'Backend("verilog"' backends/
   ```

2. 确认三条命令的目录归属，填入下表（参考答案见本讲末「综合实践」）：

   | 功能 | 目录 | 代表文件 | 继承的基类 |
   | --- | --- | --- | --- |
   | 读 Verilog（read_verilog） | ? | ? | ? |
   | 优化（opt） | ? | ? | ? |
   | 写网表（write_verilog） | ? | ? | ? |

3. 进一步验证 `Frontend`/`Pass`/`Backend` 三个基类确实都在 `kernel`：

   ```bash
   grep -n 'struct Pass\|class Pass\|struct Frontend\|struct Backend' kernel/register.h
   ```

需要观察的现象：三个 `grep` 命中的文件分别落在 `frontends/`、`passes/`、`backends/` 三个不同目录；而三个基类的定义都集中在 `kernel/register.h`。

预期结果：你会得到一张「功能 → 目录 → 基类」的对照表，证明「三层划分」不是文档说法，而是源码事实。（若本地未安装 `grep`，也可用编辑器全局搜索代替。）

#### 4.2.5 小练习与答案

**练习 1**：有人说「Yosys 没有 X 前端，因为 `frontends/CMakeLists.txt` 里没有 X」。这种说法可靠吗？为什么？

> 参考答案：部分可靠但要注意条件编译。`frontends/CMakeLists.txt` 确实是「装了哪些前端」的权威清单，但其中有的子目录被 `if (...)` 包裹（如 `slang`、`verific`），是否真正编入还取决于构建选项。判断时要同时看清单与条件宏。

**练习 2**：`passes/opt/` 目录下既有 `opt.cc`，也有 `opt_expr.cc`、`opt_merge.cc`、`opt_dff.cc` 等一堆文件。它们是什么关系？

> 参考答案：`opt` 是一个「大流程」pass，内部会调用若干子 pass（`opt_expr`/`opt_merge`/`opt_dff`/`opt_clean` 等）。这些子 pass 的实现就放在同一个 `passes/opt/` 目录里。一个「功能类别」对应一个目录、内含一个编排者加多个子模块，是 `passes/` 的常见组织方式（后续 u6-l3 会专门讲 `opt` 的编排）。

### 4.3 techlibs / libs 资源层

#### 4.3.1 概念说明

除了「写代码的」目录，Yosys 还有两个「提供资源」的目录：

- **`techlibs/`**：工艺库与厂商综合脚本。这里主要是 `.v` 模板文件（不是被综合的设计，而是用来做工艺映射的「模板模块」）和厂商专属的 `synth_xilinx`/`synth_ice40` 等 ScriptPass。子目录按厂商/平台划分：`common`（公共）、`xilinx`、`ice40`、`intel`、`efinix`、`gowin`、`lattice`、`anlogic`、`gatemate`、`quicklogic` 等。
- **`libs/`**：第三方依赖（多为 git submodule）。`README.md` 明确说第三方代码的许可证要看 `abc` 和 `libs` 子目录。这里有 `bigint`、`ezsat`/`minisat`（SAT 求解）、`json11`、`sha1`、`fst`（波形）、`slang`（SystemVerilog 解析）、`subcircuit`、`cxxopts`（命令行解析）、`fmt`、`tomlplusplus` 等。

这两个目录的共同点是：它们不直接实现综合算法，而是给算法「喂料」——`techlibs` 喂的是单元定义与映射模板，`libs` 喂的是底层库。

#### 4.3.2 核心流程

`techlibs/common/` 是理解全体的钥匙，因为它提供了所有平台都会用到的基础模板：

- `simlib.v` / `simcells.v`：Yosys 内部 `$` 单元的仿真/行为级定义。
- `techmap.v`：`techmap` pass 默认使用的映射模板。
- `abc9_map.v`：`abc9` 做逻辑映射时用的单元衔接。
- `synth.cc` / `prep.cc`：通用综合脚本 `synth` / `prep` 的实现（u1-l1 提到的两条内置脚本）。

而各厂商子目录（如 `techlibs/xilinx`）则在其基础上追加 LUT/BRAM/DSP 等平台相关阶段。`libs/` 里的库则被 `kernel` 等组件 `REQUIRES` 引用（例如 `kernel` 依赖 `bigint`、`ezsat`、`json11`、`sha1`）。

#### 4.3.3 源码精读

README 把「第三方代码在 `abc` 和 `libs`」写得很清楚：

```text
# README.md
Third-party software distributed alongside this software
is licensed under compatible licenses.
Please refer to `abc` and `libs` subdirectories for their license terms.
```

见 [README.md:L20-L22](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/README.md#L20-L22)。

`techlibs/common/CMakeLists.txt` 把关键 `.v` 模板当作数据文件（`DATA_FILES`）安装：

```cmake
# techlibs/common/CMakeLists.txt
	DATA_FILES
		simlib.v
		simcells.v
		techmap.v
		...
		abc9_map.v
```

见 [techlibs/common/CMakeLists.txt:L30-L42](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/CMakeLists.txt#L30-L42)。注意它们是「数据文件」而非「被编译的源码」——`techmap`、`abc9` 等 pass 在运行时读取这些 `.v` 作为映射模板。这就是为什么 `techlibs` 属于「资源层」。

`kernel` 对 `libs/` 的依赖则写在 `kernel/CMakeLists.txt` 的 `REQUIRES` 段：

```cmake
# kernel/CMakeLists.txt
	REQUIRES
		bigint
		ezsat
		json11
		sha1
```

见 [kernel/CMakeLists.txt:L101-L105](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/CMakeLists.txt#L101-L105)。这四项正是 `libs/` 下的子目录，说明核心层依赖这些第三方库。

> 提示：`abc/`（ABC 逻辑综合工具）虽然也在仓库根目录下，但它是独立的子项目（git submodule），由 `YosysAbc`/`YosysAbcSubmodule` 这套 CMake 模块单独管理，不归入 `libs/`。它和 `libs/` 一起被 README 点名为「看第三方许可证的地方」。

#### 4.3.4 代码实践

> 实践目标：区分「资源文件」与「源码文件」，体会 `techlibs` 提供的是「运行时被读取的模板」。

操作步骤：

1. 列出 `techlibs/common/` 的内容，确认里面是大量 `.v`、`.cc`（如 `synth.cc`/`prep.cc`/`opensta.cc`）与脚本：

   ```bash
   ls techlibs/common/
   ```

2. 列出 `libs/` 的内容，确认里面是若干独立的第三方库目录：

   ```bash
   ls libs/
   ```

3. 对照 `kernel/CMakeLists.txt` 的 `REQUIRES`，确认 `bigint/ezsat/json11/sha1` 确实都在 `libs/` 下（这些库的源码本讲不展开，只需确认它们「在那里」）。

需要观察的现象：`techlibs/common/` 里既有 `.v`（数据），也有 `.cc`（如 `synth.cc`，它是 `synth` 命令的实现，属于代码）；而 `libs/` 下每个子目录都像一个小型独立项目（有自己的 `README`/`Makefile`/`CMakeLists.txt`）。

预期结果：你能分清——`techlibs` 是「Yosys 自己的资源（映射模板 + 厂商脚本）」，`libs` 是「别人写好、Yosys 拿来用的第三方库」。（若你尚未 `git submodule update --init`，`libs/` 下部分目录可能是空的，这属于正常现象。）

#### 4.3.5 小练习与答案

**练习 1**：`techlibs/common/techmap.v` 是一段 Verilog，但它不是「待综合的设计」。它的真实作用是什么？

> 参考答案：它是 `techmap` pass 使用的「映射模板」——pass 在运行时读取它，把高层 `$` 单元按模板替换成更底层的单元。它属于「资源文件」，在 CMake 里以 `DATA_FILES` 安装，而不是被当作源码编译。

**练习 2**：为什么 `bigint`、`ezsat`、`json11`、`sha1` 这些库放在 `libs/` 而不是直接写在 `kernel/` 里？

> 参考答案：它们是独立的第三方项目（多为 git submodule），有自己的许可证和版本。放在 `libs/` 集中管理第三方依赖，便于更新与合规（README 也专门指出 `libs` 的许可证要看子目录本身），同时让 `kernel` 只需通过 `REQUIRES` 声明依赖，保持核心代码与第三方代码的清晰边界。

## 5. 综合实践

把本讲三大模块串起来，画一张你自己的「Yosys 目录关系图」，并验证它。

**任务**：完成下面这张「数据流 → 目录」对照图（建议手绘或在文档里画）：

```text
                         ┌─────────────────────────────────────────────┐
                         │              kernel（核心层）                 │
                         │   RTLIL 数据结构 + 命令系统 + 日志/线程/SAT   │
                         │   代表：rtlil.cc / register.cc / driver.cc   │
                         └─────────────────────────────────────────────┘
                                  ▲ 依赖        ▲ 依赖        ▲ 依赖
                ┌─────────────────┘             │             └─────────────────┐
   读入 HDL  →  │ frontends/        变换  →  passes/        ← 写出  │  backends/
                │ verilog/slang/…            │ opt/proc/techmap/…       │ verilog/json/smt2/cxxrtl/…
                │ 代表：verilog_frontend.cc   │ 代表：opt.cc              │ 代表：verilog_backend.cc
                └────────────────────────────┴──────────────────────────┘
                                          ▲ 喂料
                ┌─────────────────────────┴──────────────────────────┐
                │  techlibs/（Yosys 自有资源）        libs/（第三方依赖）  │
                │  common/xilinx/ice40/…               bigint/ezsat/slang/…
                │  代表：techmap.v、synth.cc            代表：被 kernel REQUIRES
                └─────────────────────────────────────────────────────┘
```

然后在仓库里逐项验证（每项给出一个文件即可）：

1. **「读 Verilog」** → 打开 [frontends/verilog/verilog_frontend.cc:L72-L73](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_frontend.cc#L72-L73)，确认 `VerilogFrontend : public Frontend`。
2. **「优化」** → 打开 [passes/opt/opt.cc:L28-L29](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt.cc#L28-L29)，确认 `OptPass : public Pass`。
3. **「写网表」** → 打开 [backends/verilog/verilog_backend.cc:L2494-L2495](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/verilog/verilog_backend.cc#L2494-L2495)，确认 `VerilogBackend : public Backend`。
4. **「三个基类都在 kernel」** → 在 `kernel/register.h` 里找到 `Frontend`/`Pass`/`Backend` 的定义。
5. **「资源层」** → 确认 `techlibs/common/CMakeLists.txt` 把 `techmap.v` 当 `DATA_FILES`，`kernel/CMakeLists.txt` 的 `REQUIRES` 含 `libs/` 下的库。

预期产物：一张标好「功能 → 目录 → 代表文件」的图，外加一份说明，解释为什么三条命令落在三个不同目录却共享同一套基类（因为它们都建立在 `kernel` 的 RTLIL 与命令系统之上）。完成后，你就拥有了后续所有讲义的导航图。

## 6. 本讲小结

- Yosys 顶层目录按「核心 + 数据流三阶段 + 资源」分层：`kernel` 是地基，`frontends`/`passes`/`backends` 是三个阶段，`techlibs`/`libs` 是资源与依赖。
- `kernel` 同时持有「RTLIL 数据结构」和「命令注册基类（`Pass`/`Frontend`/`Backend`）」，所以三层都必须依赖它，这也决定了顶层 `CMakeLists.txt` 里 `add_subdirectory` 的顺序。
- 每个层的 `CMakeLists.txt`（如 [frontends/CMakeLists.txt](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/CMakeLists.txt#L1-L13)、[passes/CMakeLists.txt](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/CMakeLists.txt#L1-L11)、[backends/CMakeLists.txt](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/CMakeLists.txt#L1-L18)）是该层「装了哪些东西」的权威索引。
- 「读 Verilog / 优化 / 写网表」分别对应 `frontends/verilog/verilog_frontend.cc`、`passes/opt/opt.cc`、`backends/verilog/verilog_backend.cc`，三者分别继承 `Frontend`/`Pass`/`Backend`。
- `techlibs` 提供映射模板（`.v`，作为 `DATA_FILES` 被运行时读取）与厂商脚本；`libs` 提供第三方依赖（如 `bigint`/`ezsat`/`slang`），二者都属资源层。
- `kernel` 的入口 `driver.cc` 与核心库分离，使 `libyosys` 可被独立链接——这是后续「把 Yosys 当库用」的前提。

## 7. 下一步学习建议

有了这张地图，接下来可以从两个方向切入：

- **走「先跑通一次综合」的路线**：进入 [u1-l4 第一次综合](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cmos/counter.ys)，亲手把 `frontends → passes → backends` 跑一遍，把本讲的静态地图变成动态数据流。
- **走「先理解中间表示」的路线**：进入第 2 单元（u2）开始读 `kernel/rtlil.h`，看看 `kernel` 里 RTLIL 到底长什么样——因为后续每一篇讲义最终都在和 RTLIL 打交道。

无论选哪条路，建议先把本讲的目录对照图放在手边，遇到任何讲义提到的文件，都先在图上定位它属于哪一层，再深入细节。
