# 波形脚本与 CI/CD 验证闭环

## 1. 本讲目标

前两讲（u11-l1、u11-l2）解决了「测试台怎么写」「激励怎么造、结果怎么判」这两件事，本讲把视角从**单机、命令行、一次仿真**抬升到**工程化、可视化、持续集成**这一层。读完本讲，你应当能够：

- 看懂 ModelSim/QuestaSim 的 `.do` Tcl 波形脚本，理解 `-divider` 分隔线、`-group` 折叠分组、`-radix` 进制显示与 `/tb_name/DUT/...` 层次路径的用法，并能照葫芦画瓢为新测试台写一个。
- 说清 `test_runner.py` / `test_runner_ci_cd.py` → `run_all_testbenches_lib` → VUnit 这条三层调用链的分工，明白自己日常只面对最上层。
- 理解 CI 流水线为何选用 NVC 仿真器、为何要克隆 grlib/gplgpu 为厂商原语提供「纯 VHDL 行为模型」，以及 PLL 为何被排除。
- 掌握 `excluded_list` 排除机制、`--xunit-xml` 测试报告产出、以及报告如何被发布回 PR/commit 的完整闭环。

本讲是验证方法学单元（u11）的收尾，也是整套手册的最后一篇——它把前面所有讲义里「跑测试」这个反复出现的动作，最终落定到「团队级、自动化的验证闭环」上。

## 2. 前置知识

在进入源码前，先用通俗语言把三个容易混淆的概念分清楚。

**波形（waveform）与波形窗口。** 数字仿真本质上是「按时间一拍一拍推进、每个信号在每个时刻都有一个值」。波形就是把这条时间轴画出来、把每个信号随时间变化的电平画成方波。ModelSim/QuestaSim 这类商业仿真器带一个图形波形窗口（wave pane），可以缩放时间、加游标（cursor）量时间间隔、分组折叠信号——这是调试时序逻辑最直观的工具。

**Tcl 与 `.do` 脚本。** ModelSim/QuestaSim 的命令行和菜单背后都是 Tcl（一种脚本语言）。你每次在波形窗口里手动加信号、调颜色、设游标，仿真器其实都在生成对应的 Tcl 命令。`.do` 文件就是把这一串 Tcl 命令存成文本，下次 `do tb_xxx.do` 一键重放，省得每次手动摆波形。`.do` 是约定俗成的扩展名，本质是 Tcl 脚本。

**CI/CD（持续集成 / 持续交付）。** 团队里每个人都在改代码，如果只靠「记得自己跑一下仿真」，迟早有人忘了跑、把坏代码合进主干。CI 的做法是：每当代码 push 或发 PR，一台云端机器（GitHub Actions 的 runner）自动把整个仓库 checkout 出来、按一份写在 YAML 里的菜谱搭环境、编译、跑全部测试、产出报告并贴回 PR。这样「测试通过」不再是口头承诺，而是机器强制门槛。本仓库的 CI 菜谱就是 `.github/workflows/vunit.yml`。

> 与 u1-l4 的衔接：u1-l4 已经带你鸟瞰过 CI 的全貌（vhdl_ls.toml、NVC、vunit.yml、test_runner_ci_cd.py 四个模块），本讲不再泛泛重复，而是**深入每条命令的源码细节**，并把 `.do` 波形脚本与 xunit 报告这两块 u1-l4 没展开的内容补齐。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲解读重点 |
|------|------|--------------|
| `ip/communication/spi/tb/tb_spi_tx.do` | SPI 发送的波形脚本（最复杂的一份，含 `-group`） | `.do` 的标准四段结构与 `-group` 折叠 |
| `ip/debouncer/tb/tb_debouncer.do` | 消抖器的波形脚本（最简单的一份） | `.do` 的最小骨架，作为实践模板 |
| `ip/memories/fifo/tb/tb_fifo_sync.do` | 同步 FIFO 的波形脚本（含多 DUT 对比分组） | 用 divider 并列展示多套厂商实现 |
| `ip/test_runner.py` | 本地仿真入口（VUnit 薄包装） | 三层调用链的最上层与各参数含义 |
| `ip/test_runner_ci_cd.py` | CI 专用入口（NVC + 厂商库 + 排除） | 与本地脚本的差异、`excluded_list`、`--xunit-xml` |
| `.github/workflows/vunit.yml` | CI 流水线菜谱 | NVC 安装、厂商库准备、报告发布闭环 |

> 注意：`run_all_testbenches_lib` 实际来自 `ip/vhdl_utils` 子模块（VHDL-Utils 仓库），本仓库未检入其源码，其函数体的具体实现（如它如何把 `excluded_list` 翻译成 VUnit 的 `exclude_*` 调用、如何把 `tb_pattern` 传给 `add_files`）标注为「待确认（位于子模块）」。本讲以**调用方**（两个 `test_runner_*.py`）的源码为主来讲解参数语义。

## 4. 核心概念与源码讲解

### 4.1 test_runner 包装层：从用户命令到 VUnit 的三层调用链

#### 4.1.1 概念说明

你可能在 u1-l3 已经跑过 `python ./ip/test_runner.py`，但当时只关注「能跑起来」。本节要拆开这行命令背后到底发生了什么。

核心结论是：**用户永远不必直接面对 VUnit。** VUnit 本身是一个功能强大但 API 庞杂的 Python 框架——要手动 `from vunit import VUnit`、`add_library`、`add_source_files`、配 generic、选仿真器、处理厂商库……这对只想跑测试的使用者太重了。于是本库在 VUnit 之上包了两层：

- **最上层**：`test_runner.py`（本地用）/ `test_runner_ci_cd.py`（CI 用）。它们只做两件事——给一堆参数填默认值、调用下层函数。读起来像一张配置表。
- **中间层**：`run_all_testbenches_lib`（来自 `vhdl_utils` 子模块）。它才是真正「懂 VUnit」的那一层：扫描 `tb_*.vhd`、建库、加载厂商库、配仿真器选项、跑仿真、汇总返回码。

最底层是 VUnit 框架自身（编译、调度仿真器、收集 pass/fail）。于是形成一条清晰的三层链：

```
python test_runner.py        ← 用户只敲这一行（第 1 层：薄包装）
      │
      ▼
run_all_testbenches_lib(...)  ← 子模块，懂 VUnit（第 2 层：编排）
      │
      ▼
VUnit (compile + simulate)   ← 框架（第 3 层：执行）
```

为什么要分这么多层？因为「参数默认值」与「VUnit 编排逻辑」是两类会以不同频率变化的东西：默认值经常被使用者改（开 GUI、改超时、排除某 tb），而 VUnit 编排逻辑相对稳定。把它们分到不同文件/层，使用者只碰最上层那张「配置表」，子模块升级时上层基本不用动。

#### 4.1.2 核心流程

`test_runner.py` 的执行流程用伪代码描述如下：

```
1. 从子模块导入 run_all_testbenches_lib（即 main 函数）和 bcolours（终端彩色）
2. 定义 run_all_testbenches():
   a. 调用 run_all_testbenches_lib( path, tb_pattern, timeout_ms,
                                     gui, compile_only, clean, debug,
                                     use_xilinx_libs, use_intel_altera_libs,
                                     excluded_list, xunit_xml )
      —— 把一张参数表原样交给中间层
   b. 中间层返回一个 returncode（0=通过，非 0=失败）
   c. 用 bcolours 给返回值上色，打印 "Passed"/"Failed"
   d. return returncode
3. if __name__ == "__main__": exit(run_all_testbenches())
```

注意第 2 步：`test_runner.py` 自己**不做任何 VUnit 调用**，它甚至没有 `import vunit`。它只是一个「填默认值 + 转发 + 染色打印」的转接头。真正的扫描、建库、仿真全在中间层。

#### 4.1.3 源码精读

先看导入与函数骨架。两个导入都指向子模块里的同一个文件 `vhdl_utils/run_all_testbenches_lib`：一个是「主函数」（别名 `run_all_testbenches_lib`），一个是「彩色打印用的常量对象」`bcolours`。

[ip/test_runner.py:16-17](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner.py#L16-L17)：从子模块导入主函数与彩色常量。

再看函数体——它本质就是一张参数表，每个参数旁都有注释说明用途：

[ip/test_runner.py:20-32](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner.py#L20-L32)：把所有参数以默认值形式传给 `run_all_testbenches_lib`。逐行解读这些参数（它们是本层暴露给用户的全部旋钮）：

| 参数 | 默认值 | 含义 |
|------|--------|------|
| `path` | `"./ip/"` | 扫描测试台的根目录（递归找 `tb_*.vhd`） |
| `tb_pattern` | `"**"` | 匹配所有测试台（`**` 是通配） |
| `timeout_ms` | `1.0` | 单个用例看门狗超时（毫秒） |
| `gui` | `False` | `True` 则打开 ModelSim/QuestaSim 图形界面（配合 `.do` 调试） |
| `compile_only` | `False` | 只编译不仿真 |
| `clean` | `False` | 先清理旧编译产物再重建 |
| `debug` | `False` | 打开调试日志 |
| `use_xilinx_libs` | `True` | 加载 Xilinx 仿真库 + glbl 模块（解决 `glbl.GSR` 报错，见 u2-l2） |
| `use_intel_altera_libs` | `False` | 加载 Intel/Altera 仿真库 |
| `excluded_list` | `[]` | 要排除的测试台文件名列表（本讲重点） |
| `xunit_xml` | `None` | 若给路径，则产出 JUnit 风格 XML 测试报告 |

最后是返回码染色与退出，这正是你在终端看到绿色 `Passed` / 红色 `Failed` 的来源：

[ip/test_runner.py:33-39](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner.py#L33-L39)：根据 `returncode` 染色打印结果，并以该码退出进程（CI 据此判定 job 成败）。

> **一个关键认知**：`test_runner.py`（本地）与下一节要讲的 `test_runner_ci_cd.py`（CI）调用的都是**同一个** `run_all_testbenches_lib`，差别只在「填的参数值不同」。所以本讲标题里的「三层关系」在本地和 CI 里是同一套，只是最上层那个薄包装换了一份、参数换了一组。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是让你亲手验证「三层分工」的边界。

1. **实践目标**：确认 `test_runner.py` 自己从不直接 `import vunit`，VUnit 调用全部封装在子模块里。
2. **操作步骤**：
   - 打开 `ip/test_runner.py`，通读全部 40 行，确认它的 import 区只有 `sys`、`os` 和 `vhdl_utils.*`，没有 `vunit`。
   - 用 `grep -rn "import vunit" ip/test_runner.py ip/test_runner_ci_cd.py` 检查（应为空）。
   - 再用 `grep -rn "from vunit" ip/test_runner.py` 复核。
3. **需要观察的现象**：上述 grep 在两个 `test_runner_*.py` 里都搜不到 `vunit` 的导入。
4. **预期结果**：两个最上层脚本都不直接依赖 VUnit，证明它们确实是「薄包装」，真正的 VUnit 编排在子模块内。
5. **待本地验证**：若你已 `git submodule update --init`，可以进一步 `grep -rn "from vunit" ip/vhdl_utils/`，应能在子模块里看到 VUnit 的真实调用点（这证实了「第 2 层才懂 VUnit」）。

#### 4.1.5 小练习与答案

**练习 1**：如果你想本地只编译、不仿真（用来快速检查语法错误），该改 `test_runner.py` 的哪个参数？

> **答案**：把 `compile_only=False` 改成 `compile_only=True`。它会被原样传给 `run_all_testbenches_lib`，中间层据此跳过仿真阶段。

**练习 2**：为什么 `test_runner.py` 把 `use_xilinx_libs` 默认设为 `True`，而 u2-l2 说它是用来解决 `glbl.GSR` 报错的？删掉它会怎样？

> **答案**：本库大量模块例化了 Xilinx `xpm` 原语（如 `xpm_cdc_single`、`xpm_fifo_sync`），这些原语的 Verilog 仿真模型会引用全局复位信号 `glbl.GSR`；不加载 glbl 模块就会编译报错。`use_xilinx_libs=True` 让中间层同时加载预编译库与 glbl 模块。设为 `False` 后，凡例化了 xpm 的测试台都会因找不到 `glbl` 而编译失败。

---

### 4.2 .do 波形脚本：信号分组与内部信号探查

#### 4.2.1 概念说明

当仿真跑出来的结果不对（比如某个 `check_equal` 失败，见 u11-l2），你往往需要「看波形」来定位是哪一拍、哪个信号出了问题。命令行仿真（`gui=False`）只给你一行 pass/fail，看不到信号随时间的变化；只有 `gui=True` 打开波形窗口，你才能逐拍追踪。

但每次打开波形窗口，默认是空的——你得手动把信号一个个拖进来、分组、设进制。如果每次调试都重做一遍，极其繁琐。`.do` 脚本就是用来**固化这套波形布局**的：它是一份 Tcl 命令清单，`do tb_xxx.do` 一执行，信号就自动按预想的分组、进制、游标位置摆好，你直接看就行。

本仓库为每个测试台都配了一份 `.do`（共 12 份，见源码地图），它们共享一套**四段式**约定：

1. **DUT 接口段**（`-divider Interface`）：放测试台顶层驱动 DUT 端口的那些信号（`clk`、`rst_n`、数据/握手线）。
2. **DUT 内部段**（`-divider Internal`）：深入到 `/tb_name/DUT/...` 层次，把设计**内部**的关键寄存器挖出来看（如计数器、状态机状态、指针）——这是 `.do` 最有价值的部分，因为它能直接探查源码里的中间变量。
3. **测试台内部段**（`-divider {tb - Internal}`）：放测试台自己的辅助信号，典型是 `clk_enable`（时钟使能）和 `simulation_done`（结束标志）。
4. **视图设置段**（`configure wave ...` + `WaveRestoreZoom`）：列宽、对齐、时间网格、缩放范围等纯显示参数。

#### 4.2.2 核心流程

一份 `.do` 的执行流程（ModelSim/QuestaSim 读到后逐行执行）：

```
onerror {resume}                  ← 出错时继续而非中止（脚本容错）
quietly WaveActivateNextPane {} 0 ← 静默激活波形面板

# —— 逐个添加信号 / 分隔线 ——
add wave -divider DuT             ← 一条带标签的水平分隔线（段落标题）
add wave -divider Interface
add wave /tb_xxx/clk              ← 添加测试台顶层信号 clk
add wave -radix unsigned /tb_xxx/tx_data   ← 以无符号十进制显示
...
add wave -divider Internal
add wave -expand -group fsm /tb_xxx/DUT/spi_fsm/state   ← 折叠分组 + 默认展开
...
add wave -divider {tb - Internal} ← 多词分隔线须用 { } 包裹
add wave /tb_xxx/simulation_done

# —— 还原视图状态 ——
TreeUpdate [SetDefaultTree]       ← 刷新波形树
WaveRestoreCursors {...}          ← 把游标放回上次保存的时间点
configure wave -namecolwidth 202  ← 一串显示参数
update                            ← 触发一次重绘（前面 -noupdate 都攒到这）
WaveRestoreZoom {0 ps} {...ps}    ← 设置可见时间范围
```

关键命令速查：

| Tcl 命令 / 开关 | 作用 |
|-----------------|------|
| `add wave -divider <name>` | 加一条带文字标签的分隔线，把信号分段 |
| `add wave -group <name> <sig...>` | 把若干信号收进一个可折叠的分组节点 |
| `-expand` | 让该分组默认展开（写在 `-group` 前） |
| `-radix binary/unsigned/hex` | 设定信号显示进制 |
| `-noupdate` | 暂不重绘，攒到最后 `update` 一次性刷新（性能） |
| `/tb/a/b` | 层次路径，`/` 是分隔符；`/tb_xxx/DUT/...` 指向例化体内部 |

#### 4.2.3 源码精读

**最复杂的一份：`tb_spi_tx.do`。** 先看它的四段分隔线如何切分界面。注意单词分隔线（`DuT`）可不加括号，而带空格的（`tb - Internal`）必须用 `{ }`：

[ip/communication/spi/tb/tb_spi_tx.do:3-4](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.do#L3-L4)：开两条分隔线 `DuT` 与 `Interface`，作为接口信号段的标题。

接着是接口信号，注意 `-radix` 的灵活使用——位宽意义明确的用 `binary`（如片选位图 `selected_chips`），数值含义的用 `unsigned`（如待发数据 `tx_data`）：

[ip/communication/spi/tb/tb_spi_tx.do:5-13](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.do#L5-L13)：列出 SPI 接口侧信号，逐个标注进制。这里能看到 `spi_clk`、`rst_n`、`spi_clk_out`、`tx_data`、`tx_data_valid`、`serial_data_out`、`spi_chip_select_n`、`tx_is_ongoing` 等顶层驱动信号。

本份 `.do` 的精华在「内部段」——它用 `-expand -group fsm` 把状态机相关的内部信号收进一个折叠分组，这是探查设计内部状态的标准手法：

[ip/communication/spi/tb/tb_spi_tx.do:14-23](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.do#L14-L23)：先加 `-divider Internal`，再用 `-expand -group fsm` 把 `state`、`bit_index`、`current_chip_index`、`selected_chips_reg`、`tx_data_reg` 五个状态机内部寄存器收进名为 `fsm` 的折叠节点（默认展开）。后面还把串行数据/片选对齐逻辑的中间信号 `spi_chip_select_n_assertion` / `_deassertion`、`serial_data_out_internal` 等挖出来。注意路径都形如 `/tb_spi_tx/DUT/spi_fsm/...`，正是通过 `DUT` 例化名钻进设计内部。

视图设置段则是纯显示参数，与逻辑无关，但能让波形「开箱即好看」——把列宽、对齐、时间网格、缩放范围一次性还原：

[ip/communication/spi/tb/tb_spi_tx.do:28-44](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.do#L28-L44)：`WaveRestoreCursors` 把游标放回 `790376 ps`；一串 `configure wave -...` 设列宽 202、值列宽 100、左对齐、时间单位 ns 等；最后 `WaveRestoreZoom {0 ps} {2111550 ps}` 把可见范围定在 0~2.1 ns。

**最简单的一份：`tb_debouncer.do`。** 它只有三个内部信号，正好作为你写新 `.do` 的最小模板：

[ip/debouncer/tb/tb_debouncer.do:3-14](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/tb/tb_debouncer.do#L3-L14)：四段一应俱全——`Interface` 段放 `clk`/`input`/`output` 三个端口；`Internal` 段深入 `/tb_debouncer/DUT/` 把消抖计数器 `debounce_counter`（无符号显示）、`input_sync`、`input_sync_d` 三个内部寄存器挖出来；`tb - Internal` 段放 `clk_enable` 与 `simulation_done`。这正是消抖原理（计数器稳定判定，见 u4-l1）在波形上的直观体现。

**对比多 DUT 的一份：`tb_fifo_sync.do`。** 它展示了如何用分隔线**并列对比多套厂商实现**。同步 FIFO 测试台同时例化了 Xilinx 版与自研版两套 DUT（见 u9-l2），`.do` 用两条 divider 把它们的 `full`/`empty` 标志分开展示，便于肉眼对照行为是否一致：

[ip/memories/fifo/tb/tb_fifo_sync.do:8-20](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.do#L8-L20)：`-divider Xilinx` 下放 `full_xilinx`/`empty_xilinx`，`-divider Own` 下放 `full_own`/`empty_own`，再做一条 `DuT Own - Internal` 把自研版的水位 `fifo_fill_level`、读写指针、请求屏蔽信号挖出来。这就是「同一 entity 多架构」在波形调试时的配套呈现方式。

#### 4.2.4 代码实践

这是一个**动手创作型实践**：为你（在 u11-l1 里）新写的 debouncer 测试台配套一个 `.do`。仓库里其实已有一份 `tb_debouncer.do` 可作参照，但你应尝试自己从零写一份，再与官方版比对。

1. **实践目标**：掌握 `.do` 的四段式骨架，能独立为新测试台产出波形脚本。
2. **操作步骤**：
   - 新建文件 `ip/debouncer/tb/tb_my_debouncer.do`（与你自己的测试台 `tb_my_debouncer` 同名）。
   - 按四段式填入。第一段接口信号至少包含 `clk`、`input`、`output`：

     ```tcl
     onerror {resume}
     quietly WaveActivateNextPane {} 0
     add wave -noupdate -divider DuT
     add wave -noupdate -divider Interface
     add wave -noupdate /tb_my_debouncer/clk
     add wave -noupdate /tb_my_debouncer/input
     add wave -noupdate /tb_my_debouncer/output
     ```

   - 第二段内部信号，参照 `tb_debouncer.do` 把消抖计数器与同步寄存器挖出来（DUT 例化名按你测试台里的 label，官方版用 `DUT`）：

     ```tcl
     add wave -noupdate -divider Internal
     add wave -noupdate -radix unsigned /tb_my_debouncer/DUT/debounce_counter
     add wave -noupdate /tb_my_debouncer/DUT/input_sync
     add wave -noupdate /tb_my_debouncer/DUT/input_sync_d
     ```

   - 第三段测试台辅助信号：

     ```tcl
     add wave -noupdate -divider {tb - Internal}
     add wave -noupdate /tb_my_debouncer/clk_enable
     add wave -noupdate /tb_my_debouncer/simulation_done
     ```

   - 末尾照搬官方版的视图设置段（`TreeUpdate` / `configure wave` / `update` / `WaveRestoreZoom`），把缩放范围先随便填一个 `{0 ps} {2000000 ps}`。
   - 在 ModelSim/QuestaSim 里加载你的测试台后执行 `do tb_my_debouncer.do`，运行 `run -all`。
3. **需要观察的现象**：波形窗口自动出现四段分隔线；接口段的 `input` 抖动时，`Internal` 段的 `debounce_counter` 不断被清零、只有输入稳定满 `2**DEBOUNCE_SYNC_BITS` 拍后才累加上来，随后 `output` 才翻转。
4. **预期结果**：你能在一张波形图上同时看到「输入毛刺 → 计数器清零 → 稳定窗口满 → 输出翻转」这条因果链，这正是 u4-l1 讲的消抖原理的可视化。
5. **待本地验证**：`.do` 需要 ModelSim/QuestaSim 图形界面；若本地只有 NVC（命令行、无 GUI，见 4.3），则 `.do` 无法可视化运行，此时本实践退化为「写出脚本、用文本审查其层次路径是否与测试台信号名一一对应」。

#### 4.2.5 小练习与答案

**练习 1**：为什么所有 `add wave` 都带 `-noupdate`，而最后单独有一条 `update`？

> **答案**：每加一个信号就重绘一次波形会非常慢。`-noupdate` 把重绘推迟，攒到所有信号加完后由 `update` 一次性刷新，显著加快脚本执行。

**练习 2**：`-divider DuT` 和 `-divider {tb - Internal}` 一个有花括号、一个没有，区别是什么？

> **答案**：分隔线标签含空格时必须用 `{ }` 把整串括起来，否则 Tcl 会把空格后的部分当成下一个参数；单个不含空格的词（`DuT`）可省略花括号。

**练习 3**：要在波形里看 `/tb_spi_tx/DUT/spi_fsm/bit_index`，这个路径分成几层？各指什么？

> **答案**：四层。`/tb_spi_tx` 是测试台顶层实体；`DUT` 是测试台里例化被测设计时所用的标签（label）；`spi_fsm` 是被测设计内部进一步例化的某子模块标签；`bit_index` 是该子模块内的一个信号。`.do` 正是靠这条层次路径钻进设计内部探查信号的。

---

### 4.3 NVC 仿真器与厂商库 CI 策略

#### 4.3.1 概念说明

本节回答一个关键问题：**CI 跑在云端 Ubuntu 机器上，没有 Vivado、没有 Quartus、没有 ModelSim，它用什么仿真？** 答案是 **NVC**——一个开源的 VHDL 仿真器。

NVC 的特点与本库 CI 选它密不可分：

- **完整支持 VHDL-2008**：本库大量使用 VHDL-2008 特性（generic package、层次化信号引用 `<<signal>>`、非约束数组元素等），必须用支持 2008 的仿真器。
- **纯 VHDL，不能编译 Verilog**：这是它最关键的局限。Xilinx/Intel 厂商原语的**仿真模型**很多是用 Verilog 写的（如 `secureip`、`unisims_ver`），NVC 没法用。
- **开源、可装在 GitHub runner 上**：通过 `nickg/setup-nvc` 一键装好，无需授权、无需商业 EDA 工具。

由此引出 CI 的核心矛盾与对策：本库的 Xilinx/Intel 架构例化了厂商原语（`PLLE2_BASE`、`xpm_*`、`scfifo` 等），这些原语需要仿真模型才能跑。NVC 用不了 Verilog 模型，怎么办？对策是——**给每个厂商原语配一份纯 VHDL 的行为模型**。具体做法在下面源码里：CI 克隆两个第三方仓库（`grlib`、`gplgpu`），从里面取出厂商库的 VHDL 源码，用 `nvc --install` 编译进 NVC 的库缓存。

而 **PLL 是唯一的例外**：`PLLE2_BASE` 没有可用的纯 VHDL 行为模型（它是模拟硬核，无法用 RTL 精确建模，见 u5-l2），所以 CI 干脆把 `pll.vhd` / `tb_pll.vhd` 排除掉，PLL 只能靠本地厂商工具验证。这就是 u1-l4 提到的「PLL 是全库唯一被 CI 排除的模块」的根因。

#### 4.3.2 核心流程

CI 流水线（`.github/workflows/vunit.yml`）的步骤，按执行顺序：

```
触发: push 到 ip/** 或改 workflow、或发 PR
  │
  ├─ checkout（submodules: recursive）← 必须递归拉 vhdl_utils 子模块
  ├─ setup Python 3.9
  ├─ setup-nvc（装 NVC 仿真器）
  │
  ├─ 【准备厂商库 —— 本节重点】
  │    ├─ Intel/Quartus: 克隆 gplgpu → 拷 sim_lib 到 /opt/intelFPGA/20.1/quartus/eda/
  │    │                → touch 一批空的 *_atoms.vhd / *_components.vhd（NVC 兼容性补丁）
  │    ├─ Xilinx/UNISIM: 克隆 grlib → 拷 unisim_VPKG.vhd / unisim_VCOMP.vhd
  │    │                → 建符号链接 + 空 vhdl_analyze_order 文件
  │    └─ nvc --install xpm_vhdl / quartus / vivado  ← 编译进 NVC 库缓存
  │
  ├─ pip install vunit-hdl==5.0.0.dev6
  ├─ mkdir test-reports
  ├─ python ./ip/test_runner_ci_cd.py --xunit-xml=test-reports/vunit_results.xml
  │    （环境变量 VUNIT_CI_MODE=true）
  ├─ 生成 XUnit HTML 报告（if: always()）
  ├─ 发布测试结果到 PR/commit（if: always()）
  └─ 上传 test-reports/ 为 artifact（if: always()）
```

几个要点先记下，源码精读里逐一印证：

- **`submodules: recursive`**：不拉 `vhdl_utils` 子模块，`test_runner_ci_cd.py` 一导入就崩（见 u3-l2）。
- **`nvc --install <lib>`**：把厂商库源码编译进 NVC 的本地库缓存，之后仿真器才能 `library xpm; use ...`。
- **空文件补丁**：Quartus 的某些 atom 库（如 `fiftyfivenm_atoms`）只有 Verilog 版、无 VHDL 版，NVC 又编译不了 Verilog，于是 `touch` 出**空 `.vhd`** 文件骗过 NVC 的库结构检查（这些库本库代码也没真正用到，空文件不影响仿真）。
- **`if: always()`**：即使测试步骤失败，报告与上传步骤照样执行——否则失败时你反而拿不到报告，无法定位。

#### 4.3.3 源码精读

先看 checkout 必须递归拉子模块——这是 CI 能跑起来的前提，与 u3-l2 讲的「子模块机制」直接呼应：

[.github/workflows/vunit.yml:21-24](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/.github/workflows/vunit.yml#L21-L24)：`actions/checkout@v4` 带 `submodules: recursive`，确保 `ip/vhdl_utils`（含 `run_all_testbenches_lib`、`utils_pkg`、`tb_utils`）被填充，否则 `test_runner_ci_cd.py` 导入即 `ModuleNotFoundError`。

接着是 NVC 安装：

[.github/workflows/vunit.yml:31-33](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/.github/workflows/vunit.yml#L31-L33)：用 `nickg/setup-nvc@v1` 装 NVC latest。这是整条流水线选择的开源仿真器。

然后是最长的「准备厂商库」块。先看 Intel/Quartus 部分——克隆 gplgpu、把它的 `sim_lib` 拷到 Quartus 标准安装路径，再 `touch` 一批空 `.vhd`：

[.github/workflows/vunit.yml:34-52](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/.github/workflows/vunit.yml#L34-L52)：克隆 `nselvara/gplgpu`，把 `hdl/sim_lib/` 拷到 `/opt/intelFPGA/20.1/quartus/eda/`；随后 `touch` 出 `fiftyfivenm_atoms.vhd`、`cyclonev_atoms.vhd` 等一批**空文件**——注释明确写「Create empty files to avoid errors with NVC」。这正是应对「NVC 编译不了 Verilog atom 库」的兼容性补丁：这些库本仓库代码并未实际引用，但 NVC 在安装 Quartus 库时会校验它们的存在，空文件足以令校验通过。

再看 Xilinx/UNISIM 部分——克隆 grlib、取出两个核心 VHDL 文件，建符号链接与空 `vhdl_analyze_order`：

[.github/workflows/vunit.yml:54-75](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/.github/workflows/vunit.yml#L54-L75)：克隆 `nselvara/grlib`，把 `unisim_VPKG.vhd`（包）与 `unisim_VCOMP.vhd`（元件声明）拷到 Vivado 标准路径；为兼容 NVC 安装脚本再建一个 `unisim_retarget_VCOMP.vhd` 符号链接；并 `touch` 出若干空的 `vhdl_analyze_order` 文件（NVC 安装库时需要这个清单文件，空清单表示「按默认顺序」）。这一步让 Xilinx 原语（如 `BUFGCE`、`xpm_*`）有了纯 VHDL 的行为模型供 NVC 仿真。

最后，把上述厂商库源码一次性编译进 NVC 库缓存——三条 `nvc --install`：

[.github/workflows/vunit.yml:76-79](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/.github/workflows/vunit.yml#L76-L79)：`nvc --version` 确认安装，随后 `nvc --install xpm_vhdl`、`nvc --install quartus`、`nvc --install vivado` 分别把三家厂商库编译进 NVC 的库缓存。此后 NVC 仿真时遇到 `library xpm;` / `library altera_mf;` / `library unisim;` 就能找到对应行为模型。

> **为什么不能直接 `nvc --install vivado` 就完事？** 因为 NVC 的安装脚本会去**真实 Vivado 安装目录**下找源码；CI 机器上没有 Vivado，所以才要先用 grlib/gplgpu 把 VHDL 源码「伪造」到那些标准路径下，再让 `nvc --install` 去读取它们。这是一套「没有 Vivado 却要骗过 NVC」的变通方案。

#### 4.3.4 代码实践

这是一个**源码阅读 + 推理型实践**，目标是让你理解「NVC + 厂商库」这套变通方案的边界。

1. **实践目标**：解释为什么 PLL 必须被 CI 排除，而 `xpm_cdc_single`、`scfifo` 不必。
2. **操作步骤**：
   - 回顾 u5-l2：PLL 用 `PLLE2_BASE`（Xilinx）/ `altclklock`（Intel），是模拟硬核。
   - 在本节源码里确认：CI 通过 grlib 只提供了 `unisim_VPKG.vhd` + `unisim_VCOMP.vhd`（包与元件声明），gplgpu 提供了 Quartus 的 sim_lib。
   - 思考：这些纯 VHDL 行为模型覆盖了哪些原语、漏掉了哪些？
3. **需要观察的现象**：在 `test_runner_ci_cd.py` 的 `excluded_list` 里（见 4.4.3），`tb_pll.vhd` 和 `pll.vhd` 被明确排除，注释写「Exclude PLL due to missing VHDL binding for PLLE2_BASE」。
4. **预期结果**：你能用一句话解释——`PLLE2_BASE` 没有可用的纯 VHDL 行为模型（grlib 不提供它），而 NVC 又用不了 Verilog 模型，故 PLL 无法在 CI 仿真；`xpm_*` 与 `scfifo` 则有 grlib/gplgpu 提供的 VHDL 模型，能正常仿真，故无需排除。
5. **待本地验证**：若有本地 NVC，可尝试 `nvc --install vivado` 后写一个只例化 `BUFGCE` 的小测试台，确认能跑通；再换成 `PLLE2_BASE`，预期会因缺绑定而报错。

#### 4.3.5 小练习与答案

**练习 1**：为什么 CI 要 `touch` 那些空的 `fiftyfivenm_atoms.vhd` 等文件，而不是直接不管它们？

> **答案**：`nvc --install quartus` 在编译 Quartus 库时会按其库清单去检查/分析这些 atom 库文件是否存在；若文件缺失，安装步骤会报错中断。这些 atom 库本仓库代码并未真正引用，所以用空 `.vhd` 骗过存在性检查即可，不影响实际仿真结果。

**练习 2**：本地用 ModelSim 跑时，厂商库从哪来？和 CI 一样吗？

> **答案**：不一样。本地 ModelSim 通常用 Vivado/Quartus **预编译**好的仿真库（编译后的二进制库，含 Verilog 模型，靠 `-L xpm -L unisims_ver` 绑定），而 CI 的 NVC 用不了这些预编译库（也不编译 Verilog），改用 grlib/gplgpu 的**纯 VHDL 源码**重新编译。同一段 RTL，在本地和 CI 里实际跑的是**不同的仿真模型**，但行为应当一致——这也是「同一设计多形态库供给」的体现（见 u2-l2）。

---

### 4.4 xunit 报告、excluded_list 与结果发布闭环

#### 4.4.1 概念说明

前几节解决了「用什么仿真（NVC）」「仿真什么（厂商库 + 排除 PLL）」。本节收尾，讲最后两个工程化要件：**如何把测试结果变成机器可读、人可读的报告**，以及**如何临时跳过不稳定测试**。

**xunit 报告（JUnit XML）。** VUnit 仿真结束后，每个用例的 pass/fail/耗时可以导出成一种叫 JUnit xunit 的 XML 格式——它是测试报告界的「通用语」，几乎所有的 CI 平台、报告工具都认。本仓库 CI 让 `test_runner_ci_cd.py` 接收 `--xunit-xml=...` 参数，把结果写到 `test-reports/vunit_results.xml`，再由两个第三方 Action 把这份 XML 转成「PR 上贴的检查状态」和「可点击的 HTML 报告」。

**excluded_list（排除清单）。** 有时某个测试台暂时不稳定（比如依赖某厂商库、或正在重写），你不希望它把整条 CI 拖红。`excluded_list` 就是为此而生：它是一个文件名列表，传给 `run_all_testbenches_lib` 后，匹配的测试台会被跳过、不参与编译与仿真。本仓库 CI 用它排除了 PLL；你也可以临时往里加任何 tb。

把这两者与 4.3 的 NVC 策略合起来，就构成了完整的**验证闭环**：

```
代码 push
  → CI 搭 NVC + 厂商库
  → test_runner_ci_cd.py 跑全部 tb（按 excluded_list 排除）
  → 产出 xunit XML
  → 第三方 Action 发布回 PR（绿勾/红叉 + 详情）
  → 失败则开发者修代码、再 push、CI 再跑……
```

#### 4.4.2 核心流程

`test_runner_ci_cd.py` 比 `test_runner.py` 多做了三件事，构成它与本地脚本的三大差异：

```
1. 检测 CI 模式: is_ci_mode = (VUNIT_CI_MODE == "true")
   → test_path = "./" （CI） 或 "./ip/" （本地借用此脚本时）
2. 从命令行解析 --xunit-xml 参数: xunit_xml_path = ...
3. 硬编码 excluded_list = ["tb_pll.vhd", "pll.vhd"]
4. 调用 run_all_testbenches_lib(..., use_intel_altera_libs=True,
                                  excluded_list=excluded_list,
                                  xunit_xml=xunit_xml_path)
```

与本地 `test_runner.py` 的差异对照：

| 维度 | 本地 `test_runner.py` | CI `test_runner_ci_cd.py` |
|------|----------------------|---------------------------|
| 扫描根 `path` | `"./ip/"` | `"./"`（CI 模式） |
| `use_intel_altera_libs` | `False` | `True`（CI 同时验 Intel 架构） |
| `excluded_list` | `[]`（空） | `["tb_pll.vhd", "pll.vhd"]` |
| `xunit_xml` | `None`（不产出） | 由 `--xunit-xml` 注入 |
| 仿真器 | ModelSim/QuestaSim（本地有） | NVC（CI 装） |

注意：两者调用的仍是**同一个** `run_all_testbenches_lib`，所以「三层调用链」没变，只是最上层换了脚本、换了一组参数。

#### 4.4.3 源码精读

先看 `test_runner_ci_cd.py` 如何检测 CI 模式并切换扫描根：

[ip/test_runner_ci_cd.py:22-26](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner_ci_cd.py#L22-L26)：读环境变量 `VUNIT_CI_MODE`，为 `"true"` 时把 `test_path` 设为 `"./"`，否则 `"./ip/"`。CI 流水线在「Run VUnit tests」步骤里设了 `VUNIT_CI_MODE: "true"`（见下文），故 CI 中扫描根是仓库根。

再看 `--xunit-xml` 的命令行解析——它没用 argparse，而是手工扫 `sys.argv`，因为本脚本刻意保持轻量：

[ip/test_runner_ci_cd.py:29-33](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner_ci_cd.py#L29-L33)：在 `sys.argv` 里找 `--xunit-xml`，取其下一个元素作为输出路径。CI 传 `--xunit-xml=test-reports/vunit_results.xml`（注意是 `=` 连写形式，故实际匹配到的是 `--xunit-xml=test-reports/...` 这一整项；这里解析逻辑对 `=` 连写形式的处理属于边界情况，待本地验证其确切行为——稳妥写法是 `--xunit-xml test-reports/...` 空格分隔）。

然后是排除清单——本讲 `excluded_list` 机制的实物：

[ip/test_runner_ci_cd.py:45-48](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner_ci_cd.py#L45-L48)：定义 `excluded_list = ["tb_pll.vhd", "pll.vhd"]`，注释说明排除原因——「missing VHDL binding for PLLE2_BASE」。这正对应 4.3 讲的「PLL 无纯 VHDL 行为模型」。注意 `excluded_list` 是**文件名**列表，匹配粒度是文件而非用例。

最后把这些差异参数传给中间层：

[ip/test_runner_ci_cd.py:50-62](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner_ci_cd.py#L50-L62)：`run_all_testbenches_lib(...)` 调用，与本地脚本相比关键差异是 `use_intel_altera_libs=True`、`excluded_list` 非空、`xunit_xml=xunit_xml_path` 非空。

回到 CI 菜谱，看「Run VUnit tests」步骤如何调用这个脚本并注入环境变量：

[.github/workflows/vunit.yml:96-105](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/.github/workflows/vunit.yml#L96-L105)：先 `rm -rf ./gplgpu ./grlib`（清掉克隆的厂商库源码，避免被当成项目源码扫描编译），再 `python ./ip/test_runner_ci_cd.py --xunit-xml=test-reports/vunit_results.xml`；环境变量 `VUNIT_CI_MODE=true`，并带 `timeout-minutes: 15` 防止单步卡死。注意它与 u1-l3 讲的本地 `test_runner.py` 不同——CI 用的是 `test_runner_ci_cd.py`。

最后是报告发布的三连步骤，全部带 `if: always()`，确保**即便测试失败也能拿到报告**：

[.github/workflows/vunit.yml:107-126](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/.github/workflows/vunit.yml#L107-L126)：三个步骤——(1) `AutoModality/action-xunit-viewer@v1` 把 XML 渲染成可读 HTML 报告（标题「HDL Core Library Test Results」）；(2) `EnricoMi/publish-unit-test-result-action@v2` 把每个用例的 pass/fail 作为检查状态贴回 PR/commit（`check_name: VUnit Test Results`）；(3) `actions/upload-artifact@v4` 把整个 `test-reports/` 目录存为可下载 artifact（`name: test-results`）。三者都 `if: always()`，意味着测试步骤即便失败也照样执行——否则失败时反而无报告可看。

> **关于实践任务里「用 test_runner.py 的 excluded_list」的澄清**：题目说「用 test_runner.py 的 excluded_list 在 CI 中临时跳过」，但需注意——**CI 实际跑的是 `test_runner_ci_cd.py`，不是 `test_runner.py`**。两个脚本都有同名的 `excluded_list` 参数（都转发给同一个 `run_all_testbenches_lib`），机制完全一样，但「在 CI 中生效」只能改 `test_runner_ci_cd.py` 里那个列表；改 `test_runner.py` 只影响本地。这是「以源码为准」的一处典型细节。

#### 4.4.4 代码实践

这是一个**工程操作型实践**，目标是让你掌握「临时跳过不稳定测试」与「产出本地报告」两个动作。

1. **实践目标**：学会用 `excluded_list` 在 CI 中临时跳过一个尚不稳定的测试台（如 `tb_spi_interface.vhd`），并理解其对报告的影响。
2. **操作步骤**：
   - 打开 `ip/test_runner_ci_cd.py`，定位到 `excluded_list = [...]`（4.4.3 已给行号）。
   - 临时追加你的目标文件名，例如：

     ```python
     excluded_list = [
         "tb_pll.vhd",
         "pll.vhd",
         "tb_spi_interface.vhd",   # 临时跳过：当前不稳定，待修复
     ]
     ```

   - 提交并 push，触发 CI。
   - 在 PR 的「VUnit Test Results」检查里观察：被排除的测试台既不会出现在通过列表，也不会出现在失败列表——它被彻底跳过。
3. **需要观察的现象**：CI 不再因 `tb_spi_interface.vhd` 失败而整体变红；xunit 报告里该 tb 的用例数为 0。
4. **预期结果**：CI 维持绿色（前提是其余测试都过），xunit 报告里看不到被排除的 tb；修复后**记得把文件名从 `excluded_list` 移除**，否则该测试会长期处于「不被验证」的盲区。
5. **待本地验证**：本地可类比——在 `test_runner.py` 的 `excluded_list=[]` 里加入文件名，跑 `python ./ip/test_runner.py`，确认该 tb 不再被编译。另外可本地试产 xunit：在 `test_runner.py` 里把 `xunit_xml=None` 改成 `xunit_xml="local_results.xml"`，跑完后用浏览器打开该 XML 查看结构。

#### 4.4.5 小练习与答案

**练习 1**：CI 里三个报告/上传步骤都带 `if: always()`，为什么？

> **答案**：默认情况下，GitHub Actions 某步失败后后续步骤会跳过。如果「Run VUnit tests」失败后报告步骤也被跳过，开发者就拿不到失败详情，无法定位。`if: always()` 强制报告与上传步骤无论成败都执行，保证失败时也能看到是哪个用例挂了。

**练习 2**：`excluded_list` 匹配的是「用例名」还是「文件名」？往里加 `"test_clean_transition"`（一个用例名）会有效吗？

> **答案**：匹配的是**测试台文件名**（如 `tb_pll.vhd`），不是用例名。`run_all_testbenches_lib` 在扫描文件阶段就按文件名过滤，被排除的整个文件不参与编译与仿真。加用例名 `"test_clean_transition"` 无效——它不是文件名，不会被任何文件匹配。

**练习 3**：为什么本地 `test_runner.py` 默认 `xunit_xml=None`，而 CI 设了路径？

> **答案**：本地交互跑测试时，pass/fail 直接在终端彩色打印即可，无需 XML；CI 是无人值守、要把结果喂给后续报告 Action 和 PR 检查，必须有机读的 xunit XML，故 CI 传 `--xunit-xml` 产出文件。

---

## 5. 综合实践

把本讲四个模块串起来，模拟一次真实的「测试台从新增到 CI 闭环」全流程。假设你在 u11-l1 里为 `debouncer` 新写了一个测试台 `tb_my_debouncer.vhd`，现在要让它进入团队验证闭环。

**任务**：完成下面五步，每步对应本讲一个模块。

1. **本地发现与运行（对应 4.1 三层链）**：
   - 把 `tb_my_debouncer.vhd` 放进 `ip/debouncer/tb/`（遵循 u1-l2 的 `tb/tb_*.vhd` 约定，含 `runner_cfg` generic）。
   - 跑 `python ./ip/test_runner.py`，确认 `test_runner.py` → `run_all_testbenches_lib` → VUnit 这条链自动发现了你的新测试台（凭 `runner_cfg` 识别，见 u1-l3、u11-l1）。

2. **图形化调试（对应 4.2 `.do`）**：
   - 按 4.2.4 写一份 `tb_my_debouncer.do`（四段式 + 内部信号 `debounce_counter`）。
   - 把 `test_runner.py` 的 `gui=True` 改开（或直接在 ModelSim 里 `do tb_my_debouncer.do`），确认波形按分组自动摆好。

3. **CI 接入（对应 4.3 NVC 策略）**：
   - push 后观察 CI：`submodules: recursive` 拉了子模块、NVC 装好、厂商库经 grlib/gplgpu 准备就绪、`test_runner_ci_cd.py` 跑了你的新 tb。
   - 确认你的测试台**没有**例化 `PLLE2_BASE` 之类无 VHDL 模型的原语（debouncer 是纯行为级，应能通过）。

4. **报告查看（对应 4.4 xunit 闭环）**：
   - 在 PR 上点开「VUnit Test Results」检查，确认你的用例出现在通过列表、`vunit_results.xml` 里有对应条目。

5. **故障演练（对应 4.4 excluded_list）**：
   - 故意在测试台里写一个会失败的 `check_equal`，push，看 CI 变红、报告标出失败用例。
   - 然后按 4.4.4 把 `tb_my_debouncer.vhd` 临时加进 `test_runner_ci_cd.py` 的 `excluded_list`，push，看 CI 恢复绿色、报告里该 tb 消失。
   - 最后**改回正确断言、移除排除项**，push，确认 CI 绿且测试台重新被验证。

**交付物**：一份简短记录，说明每步你观察到的现象，并特别标注「第 5 步如果忘记移除 excluded_list 会造成什么长期危害」（答案：该测试台会长期处于不被验证的盲区，回归无法被发现）。

> 本综合实践大部分步骤依赖本地 ModelSim/QuestaSim GUI 与 GitHub Actions 实际触发；若环境不全，可退化为「在源码层面定位每个改动点并口述预期」的阅读型实践。

## 6. 本讲小结

- **`.do` 是 Tcl 波形脚本**，用 `-divider` 切分段落、`-group`/`-expand` 折叠分组、`-radix` 设进制、`/tb/DUT/...` 层次路径探查设计内部信号；本库每个测试台都配一份，遵循「Interface → Internal → tb-Internal → 视图设置」四段式。
- **三层调用链**：`test_runner(_ci_cd).py`（薄包装、填参数）→ `run_all_testbenches_lib`（子模块、懂 VUnit、扫描建库仿真）→ VUnit（执行）。用户只碰最上层；两个最上层脚本都不直接 `import vunit`。
- **CI 选 NVC**：开源、支持 VHDL-2008，但不能编译 Verilog；故用 grlib/gplgpu 为厂商原语提供纯 VHDL 行为模型，再用 `nvc --install` 编译进库缓存。
- **PLL 是唯一例外**：`PLLE2_BASE` 无纯 VHDL 模型，故 `tb_pll.vhd`/`pll.vhd` 进 `excluded_list`，PLL 只能本地厂商工具验证。
- **xunit 报告闭环**：`test_runner_ci_cd.py` 经 `--xunit-xml` 产出 JUnit XML，再由三个 `if: always()` 的 Action（HTML 渲染、PR 检查发布、artifact 上传）把结果回贴到 PR，失败时也能拿到报告。
- **excluded_list 匹配文件名**：临时跳过不稳定 tb 就把文件名加进 `test_runner_ci_cd.py` 的列表；改完要记得移除，否则该 tb 长期脱离验证。

## 7. 下一步学习建议

本讲是验证方法学单元的收尾，也是整套学习手册的最后一篇。到这里你已完整走过：从项目概览（u1）→ 核心设计模式（u2）→ 工具包（u3）→ 各类 IP（u4–u10）→ 验证方法学（u11）。建议接下来的学习方向：

- **横向贯通**：挑一个你最喜欢的 IP（比如异步 FIFO u9-l3 或 SPI 顶层 u10-l4），把它的「设计源码 → 测试台（u11-1/2）→ `.do` 波形（本讲 4.2）→ CI 验证（本讲 4.3/4.4）」整条链亲手走一遍，作为结业练习。
- **补测试盲区**：本手册多处指出尚未被仿真覆盖的角落——SPI 模式 2/3（u10-1/2/3）、ff_synchroniser 的 `own_behavioural` 实现（u2-1）、双口 RAM 的同周期读写（u6-2）。挑一个，仿照 u11-1/2 写测试台、配套 `.do`、放进 CI，是对本套方法学的最佳实战。
- **深入子模块**：本讲多处标注 `run_all_testbenches_lib`、`utils_pkg`、`tb_utils` 位于 `ip/vhdl_utils` 子模块且「待确认」。clone 子模块后阅读 `run_all_testbenches_lib` 的真实实现（它如何把 `excluded_list`、`tb_pattern`、`use_xilinx_libs` 翻译成 VUnit API 调用），能把本讲「中间层」的黑盒彻底打开。
- **跨厂商等价性验证**：u2-1 提到同一测试台可同时例化多套架构做等价回归（如 `tb_fifo_sync.do` 里 Xilinx 版与 Own 版并排展示）。尝试为新 IP 写一份三架构对照测试台，是迈向「IP 核库维护者」的高阶练习。
