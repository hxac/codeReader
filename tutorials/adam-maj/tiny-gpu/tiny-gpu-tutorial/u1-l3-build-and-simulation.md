# 构建与仿真工具链

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 tiny-gpu 把 SystemVerilog 源码「跑起来」需要哪几件工具，以及它们各自的分工。
- 跟着 `make test_matadd` 这一条命令，完整复述它背后的三步流水线：`sv2v` 转换 → `iverilog` 编译 → `vvp` + `cocotb` 仿真。
- 读懂 `Makefile` 里 `test_%`、`compile`、`compile_%` 三个目标之间的依赖与模式匹配关系。
- 知道仿真日志写到 `test/logs/` 下的哪个文件，并能在日志里找到「初始数据内存」「执行轨迹」「最终数据内存」三段内容。
- 独立完成一次矩阵加法仿真，并验证计算结果是否正确。

本讲是入门单元的最后一讲。在 [u1-l1](u1-l1-project-overview.md) 我们知道了 tiny-gpu 是什么，在 [u1-l2](u1-l2-repo-structure.md) 我们画出了源码地图，本讲则解决一个最实际的问题：**这些 `.sv` 文件到底怎么跑起来？** 后续所有讲义（无论是读 core 还是写内核）都建立在「你能跑通仿真」的前提上。

## 2. 前置知识

在正式讲工具链之前，先用通俗语言建立 5 个最小概念。如果你已经熟悉硬件仿真，可以跳过本节。

1. **硬件描述语言（HDL）源码不能直接「运行」**：`src/` 下的 `.sv` 文件描述的是电路结构，不是一段会被 CPU 执行的程序。要让它在电脑上「跑」，需要一个**仿真器（simulator）**来模拟这些电路在时钟驱动下的行为。
2. **SystemVerilog vs Verilog**：SystemVerilog（`.sv`）是 Verilog 的超集，语法更丰富（`parameter`、`generate`、`always_ff` 等）。tiny-gpu 用 SystemVerilog 写，但并不是所有工具都吃得下完整 SystemVerilog，所以经常需要先「降级」成纯 Verilog 再仿真。
3. **仿真器（simulator）**：这里用的是 [Icarus Verilog（iverilog）](https://steveicarus.github.io/iverilog/)。`iverilog` 负责**编译**（把源码编译成一个可仿真的中间文件 `.vvp`），`vvp` 负责**运行**这个中间文件。
4. **测试台（testbench）**：光有电路不够，还得有人给电路喂时钟、复位、装数据、观察输出。在 tiny-gpu 里这件事用 **[cocotb](https://docs.cocotb.org/)** 完成——它让你用 Python 写测试，通过一个叫 VPI 的接口去驱动 Verilog 仿真器。
5. **工具链（toolchain）**：上面这套 `sv2v + iverilog + vvp + cocotb` 的组合，统称仿真工具链。本讲的核心就是讲清楚它们怎么串在一起。

> 一句话直觉：**源码（`.sv`）→ 降级（`sv2v`）→ 编译（`iverilog`）→ 运行 + 测试（`vvp` + `cocotb`/`.py`）→ 日志（`test/logs`）**。先记住这条主线，后面每一节都是在展开它。

## 3. 本讲源码地图

本讲涉及的关键文件如下表。注意本讲的核心「源码」其实不是硬件逻辑，而是**构建脚本与测试台**：

| 文件 | 类型 | 作用 |
| --- | --- | --- |
| `Makefile` | 构建 | 整条工具链的入口：定义 `test_%`、`compile`、`compile_%` 等目标 |
| `test/helpers/setup.py` | 仿真测试 | cocotb 测试台的「启动函数」：建时钟、复位、装内存、写 DCR、拉 start |
| `test/test_matadd.py` | 仿真测试 | 矩阵加法内核的完整测试，含主循环与结果断言 |
| `test/helpers/memory.py` | 仿真测试 | 用 Python 模拟外部 DRAM，每个周期响应硬件的读写请求 |
| `test/helpers/logger.py` | 仿真测试 | 把日志写到 `test/logs/log_<时间戳>.txt` |
| `test/helpers/format.py` | 仿真测试 | 把硬件内部信号翻译成可读的「指令 / 状态 / 寄存器」轨迹 |
| `README.md` | 文档 | 工具安装说明、仿真方法 |

## 4. 核心概念与源码讲解

本讲按最小模块拆成 4 节：先看 `Makefile` 的总体结构（4.1），再分别讲三个工具环节——`sv2v` 转换（4.2）、`iverilog`+`cocotb` 仿真（4.3）、日志输出（4.4）。

---

### 4.1 Makefile：目标与依赖关系

#### 4.1.1 概念说明

`Makefile` 是整条工具链的「总指挥」。我们平时只敲一条 `make test_matadd`，它就会自动去调 `sv2v`、`iverilog`、`vvp`，把源码一路变成仿真结果。理解 `Makefile`，就是理解这条命令背后到底按什么顺序做了什么。

tiny-gpu 的 `Makefile` 非常短（只有 25 行），但用了两个值得注意的 Make 特性：

- **模式规则（pattern rule）**：`test_%` 和 `compile_%` 里的 `%` 是通配符。`make test_matadd` 会匹配 `test_%`，其中 `%` = `matadd`；这个 `%` 在命令里用 `$*`（叫「茎」）取出来。
- **目标依赖**：`test_%` 依赖 `make compile`，所以每次跑测试前都会先重新编译，保证用的是最新源码。

#### 4.1.2 核心流程

`make test_matadd` 的完整流程可以用下面这段伪代码概括：

```text
make test_matadd
 │
 ├─① make compile            （编译目标，4.2 节详解）
 │    ├─ make compile_alu     → 匹配 compile_% → sv2v 单独转 alu.sv  → build/alu.v
 │    ├─ sv2v -I src/* ...    → 把其余 .sv 转成 Verilog               → build/gpu.v
 │    └─ cat build/alu.v >> build/gpu.v，并补 timescale 头            （最终 build/gpu.v）
 │
 ├─② iverilog -o build/sim.vvp -s gpu -g2012 build/gpu.v   （4.3 节详解）
 │       编译成可仿真中间文件 build/sim.vvp
 │
 └─③ MODULE=test.test_matadd vvp ... build/sim.vvp          （4.3 节详解）
         用 cocotb 驱动 vvp 运行，跑 test_matadd.py 里的测试
         日志写入 test/logs/log_<时间戳>.txt                  （4.4 节详解）
```

三步之间是严格的顺序依赖：没有 `build/gpu.v` 就没法 `iverilog`，没有 `build/sim.vvp` 就没法 `vvp`。`Makefile` 把这条链编进了一条命令里。

#### 4.1.3 源码精读

先看 `Makefile` 的开头与 `test_%` 目标。第 1 行声明 `test`、`compile` 是「伪目标」（不对应真实文件），第 3 行导出一个环境变量 `LIBPYTHON_LOC`（4.3 节解释）：

[Makefile:1-3](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/Makefile#L1-L3) — 声明伪目标，并用 `cocotb-config --libpython` 探测 Python 库路径，导出为环境变量供仿真器使用。

接下来是整条链的入口 `test_%`：

[Makefile:5-8](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/Makefile#L5-L8) — `test_%` 模式目标。三行命令分别对应流程图里的 ①②③：先 `make compile`，再用 `iverilog` 编译，最后用 `vvp` 配合 cocotb 运行。注意 `$*` 会取到 `%` 匹配的部分（例如 `matadd`），于是 `MODULE=test.test_$*` 展开成 `MODULE=test.test_matadd`。

`compile` 目标负责把 SystemVerilog 源码加工成单个纯 Verilog 文件 `build/gpu.v`：

[Makefile:10-17](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/Makefile#L10-L17) — `compile` 目标：先单独编译 `alu`，再用 `sv2v` 转换主设计，然后把两份输出拼成一个文件，并在最前面补一行 `` `timescale 1ns/1ns ``。这一段是 4.2 节的重点。

最后是模式规则 `compile_%`，它会被 `make compile_alu` 命中（`%` = `alu`）：

[Makefile:19-20](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/Makefile#L19-L20) — `compile_%` 模式规则。`make compile_alu` 会匹配它，执行 `sv2v -w build/alu.v src/alu.sv`，把 `alu.sv` 单独转成 `build/alu.v`。

> 提示：`Makefile` 里命令行的缩进必须是 **Tab**，不是空格。这是 Make 的新手常见坑，如果你手改 `Makefile` 后报 `missing separator` 错误，多半是缩进被换成了空格。

#### 4.1.4 代码实践

**实践目标**：把 `make test_matadd` 的三步流水线和 `Makefile` 里的命令行一一对应起来。

**操作步骤**：

1. 打开 `Makefile`，定位到 `test_%` 目标（第 5–8 行）。
2. 在第 6、7、8 行旁边分别用注释笔标注：`① compile`、`② iverilog 编译`、`③ vvp 运行`。
3. 回答：如果你运行 `make test_matmul`，第 8 行的 `MODULE=test.test_$*` 最终会变成什么字符串？

**需要观察的现象**：仅做源码阅读分析，不运行命令。

**预期结果**：`$*` 取到 `matmul`，于是该字符串变成 `MODULE=test.test_matmul`。这也解释了为什么测试文件必须命名为 `test/test_<名字>.py`——`make test_<名字>` 会去找 `test.test_<名字>` 这个 Python 模块。

#### 4.1.5 小练习与答案

**练习 1**：`make compile_alu` 这个命令，`Makefile` 里并没有写一个叫 `compile_alu` 的显式目标，它为什么能工作？

**参考答案**：因为 `compile_%` 是一条**模式规则**，`%` 匹配 `alu`，于是 `make compile_alu` 命中它，执行 `sv2v -w build/alu.v src/alu.sv`。

**练习 2**：`test_%` 目标里调了 `make compile`，而 `compile` 又调了 `make compile_alu`。这种「一个目标内部再 make 另一个目标」的做法有什么好处？为什么不直接把所有命令平铺写进 `test_%`？

**参考答案**：好处是**复用与解耦**。`compile` 这套「转换 + 拼接」的逻辑独立成一个目标后，既被 `test_%` 复用，将来也可以被别的目标（例如 `show_%`、波形查看）复用；同时把「准备 Verilog 文件」和「运行仿真」两件事分开，便于单独排查问题（例如只跑 `make compile` 看转换是否成功，不跑仿真）。

---

### 4.2 sv2v：把 SystemVerilog 转成 Verilog

#### 4.2.1 概念说明

`sv2v`（[SystemVerilog to Verilog](https://github.com/zachjs/sv2v)）是一个把 SystemVerilog 翻译成普通 Verilog 的工具。tiny-gpu 用它的原因是：项目源码是 `.sv`，而选用的仿真器 `iverilog` 对某些 SystemVerilog 特性支持有限，先用 `sv2v` 把语法「降级」成更通用的 Verilog，仿真会更稳妥。

`compile` 目标里 `sv2v` 相关的命令其实做了两件容易看漏的事：

1. **单独编译 `alu.sv`**：通过 `make compile_alu` 把 `alu.sv` 单独转成 `build/alu.v`。
2. **转换主设计并拼接**：用 `sv2v -I src/* ...` 转换其余源码，再把 `alu.v` 拼回去，最终得到一个完整的 `build/gpu.v`。

#### 4.2.2 核心流程

`compile` 目标对 `build/gpu.v` 的加工可以拆成 5 步：

```text
① make compile_alu
       sv2v -w build/alu.v src/alu.sv          → 生成 build/alu.v

② sv2v -I src/* -w build/gpu.v                → 生成 build/gpu.v（注意：不含 alu，见 4.2.3）

③ echo "" >> build/gpu.v                       → 在 gpu.v 末尾加一个空行（模块分隔）

④ cat build/alu.v >> build/gpu.v               → 把 alu 模块拼到末尾

⑤ 把 `timescale 1ns/1ns 写到最前面              → 通过 temp.v 暂存再覆盖回来
   echo '`timescale 1ns/1ns' > build/temp.v
   cat build/gpu.v >> build/temp.v
   mv build/temp.v build/gpu.v
```

最终产物 `build/gpu.v` 是一个**自包含的纯 Verilog 文件**：顶部有时钟刻度声明，主体是除 `alu` 外的全部模块，末尾是 `alu` 模块。这个文件就是下一步 `iverilog` 的唯一输入。

#### 4.2.3 源码精读

先看第 ② 步的主转换命令。这一行非常容易看漏一个细节：

[Makefile:12](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/Makefile#L12) — `sv2v -I src/* -w build/gpu.v`。`-w build/gpu.v` 指定输出文件。关键在 `-I src/*`：`-I` 会「吃掉」紧跟在它后面的**一个**参数作为 include 搜索路径。而 `src/*` 会被 shell 先展开成按字母序排列的全部 `.sv` 文件，第一个就是 `src/alu.sv`。

这一行展开后等价于：

```bash
sv2v -I src/alu.sv src/controller.sv src/core.sv ... src/scheduler.sv -w build/gpu.v
#     └─ 被 -I 当成 include 路径吃掉 ──┘ └──────── 真正的输入文件 ────────┘
```

也就是说，**`alu.sv` 被 `-I` 当作 include 路径「吃掉」了，并没有作为输入文件被编译进 `build/gpu.v`**。这就是为什么 `compile` 必须先用 `make compile_alu` 单独生成 `build/alu.v`，再在第 ④ 步用 `cat` 把它拼回来——否则最终设计里会缺 `alu` 模块，仿真时 `iverilog` 会报未定义模块。

> 说明：以上是基于 shell 通配符展开规则与 `sv2v`/`-I` 参数语义的分析，`Makefile` 本身没有注释解释这一点，但它与「为什么要单独编译 alu」的现象完全吻合。

接着看拼接与补 timescale 的几步：

[Makefile:13-14](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/Makefile#L13-L14) — 第 13 行在 `gpu.v` 末尾加一个空行，第 14 行把 `build/alu.v`（即 `alu` 模块的纯 Verilog 代码）追加拼接到 `gpu.v` 末尾。这样最终的 `gpu.v` 就包含了全部模块。

[Makefile:15-17](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/Makefile#L15-L17) — 通过一个临时文件 `temp.v`，把 `` `timescale 1ns/1ns `` 写在最前面，再把 `gpu.v` 的内容追加其后，最后覆盖回 `gpu.v`。这一步保证仿真器有一个明确的时钟刻度（1 纳秒精度），`sv2v` 的输出本身不一定带这个声明。

#### 4.2.4 代码实践

**实践目标**：亲手验证「`-I src/*` 会吃掉 `src/alu.sv`」这一推断。

**操作步骤**：

1. 先按 README 的说明 `mkdir build`。
2. 单独运行 `make compile_alu`，确认生成了 `build/alu.v`。
3. 单独运行 `make compile`，确认生成了 `build/gpu.v`。
4. 在 `build/gpu.v` 里搜索 `alu` 模块定义（形如 `module alu (`）。它应该出现在文件**末尾**（被 `cat` 拼上去的那段），而不是出现在主体里。

**需要观察的现象**：

- `build/alu.v` 里有一个完整的 `module alu` 定义。
- `build/gpu.v` 顶部第一行是 `` `timescale 1ns/1ns ``。
- `build/gpu.v` 末尾能找到 `module alu`，主体（`gpu`、`core`、`controller` 等）在它之前。

**预期结果**：与上面三点一致。如果在 `build/gpu.v` 主体部分（非末尾）找不到 `alu` 模块、却在末尾能找到，就印证了 4.2.3 的分析。**待本地验证**：`sv2v` 的具体版本可能会让输出排版略有差异，但「alu 在末尾、timescale 在开头」的整体结构应当稳定。

#### 4.2.5 小练习与答案

**练习 1**：如果要把第 ② 步改成「`alu.sv` 也一起作为输入编译进 `gpu.v`」（从而不需要第 ④ 步拼接），`Makefile` 该怎么改？

**参考答案**：不要让 `-I` 吃掉 `alu.sv`。例如可以把 `src/*` 显式写成输入文件列表、再把 `-I` 指向一个真实目录：`sv2v -I src build/alu.sv build/controller.sv ... build/scheduler.sv -w build/gpu.v`，或者干脆用 `sv2v src/*.sv -w build/gpu.v`（让 `-I` 不出现在开头吃文件）。改完后 `make compile_alu` 与第 ④ 步 `cat` 就可以省掉。

**练习 2**：为什么第 ⑤ 步要专门在最前面补 `` `timescale 1ns/1ns ``？

**参考答案**：仿真器需要一个时钟刻度（timescale）来确定 `#延迟`、时钟周期等的时间单位。`sv2v` 输出的纯 Verilog 不一定保留 timescale 声明，所以手动在最前面补一行，保证后续 `iverilog`/`vvp` 仿真时所有时间量都有明确的尺度（这里用 1ns 精度）。

---

### 4.3 iverilog 编译 + cocotb 仿真启动

#### 4.3.1 概念说明

经过 4.2，我们得到了一个纯 Verilog 文件 `build/gpu.v`。接下来分两步把它「跑」起来：

- **`iverilog` 编译**：把 `build/gpu.v` 编译成可仿真的中间文件 `build/sim.vvp`。
- **`vvp` 运行**：执行 `build/sim.vvp`，同时在 cocotb 的配合下用 Python 测试台驱动仿真、检查结果。

`cocotb` 的核心思想是：**Verilog 仿真器通过一个叫 VPI 的标准接口，与 Python 进程双向通信**。Python 这边给电路喂时钟、复位、数据，并读取电路内部信号；Verilog 这边每到一个时钟沿就推进一拍仿真。`Makefile` 第 3 行的 `LIBPYTHON_LOC` 和第 8 行的一长串 `vvp` 参数，就是在搭这条 Python↔VPI 的桥。

#### 4.3.2 核心流程

`test_%` 目标的后两步可拆成下面这条链：

```text
② iverilog -o build/sim.vvp -s gpu -g2012 build/gpu.v
       -o build/sim.vvp  : 输出中间文件
       -s gpu            : 指定顶层模块为 gpu
       -g2012            : 启用 IEEE 1364-2012 标准（支持更多语法）
       build/gpu.v       : 唯一的输入文件
       → 生成 build/sim.vvp

③ MODULE=test.test_matadd \
   vvp -M $(cocotb-config --prefix)/cocotb/libs -m libcocotbvpi_icarus build/sim.vvp
       MODULE=test.test_matadd              : 告诉 cocotb 加载哪个 Python 测试模块
       vvp build/sim.vvp                     : 运行仿真中间文件
       -M .../cocotb/libs                    : 把 cocotb 的库目录加入 VPI 搜索路径
       -m libcocotbvpi_icarus               : 加载 cocotb 针对 Icarus 的 VPI 模块（关键桥接）
       → 仿真开始，Python 测试台接管
```

cocotb 接管后，会去 `test/test_matadd.py` 里找用 `@cocotb.test()` 装饰的测试函数 `test_matadd` 来执行。这个函数就是「测试台主程序」，它通过 `setup.py` 完成启动序列。

启动序列（在 `setup.py` 的 `setup()` 函数里）一共五件事：

```text
1. 建时钟（25us 周期）并立刻开始
2. 给一拍复位（reset=1 → 一个上升沿 → reset=0）
3. 装载程序内存（kernel 指令）
4. 装载数据内存（输入矩阵）
5. 写设备控制寄存器（设 thread_count）→ 拉高 start
```

#### 4.3.3 源码精读

先看 `iverilog` 编译这一行：

[Makefile:7](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/Makefile#L7) — `iverilog -o build/sim.vvp -s gpu -g2012 build/gpu.v`。`-s gpu` 把顶层模块定为 `gpu`（仿真从 `gpu` 开始实例化整棵模块树）；`-g2012` 开启 2012 标准；输出 `build/sim.vvp`。

再看运行这一行，以及它依赖的环境变量：

[Makefile:3](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/Makefile#L3) — 用 `cocotb-config --libpython` 自动探测本机 Python 动态库（libpython）的路径，导出成 `LIBPYTHON_LOC`。cocotb 的 VPI 模块需要它才能连上 Python 解释器。

[Makefile:8](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/Makefile#L8) — `MODULE=test.test_$* vvp -M $$(cocotb-config --prefix)/cocotb/libs -m libcocotbvpi_icarus build/sim.vvp`。`MODULE` 告诉 cocotb 加载 `test.test_matadd` 这个 Python 模块；`-M` 添加 cocotb 库目录；`-m libcocotbvpi_icarus` 载入 cocotb 针对 Icarus 的 VPI 桥接模块；最后跑 `build/sim.vvp`。注意 `$$` 在 Makefile 里转义成 shell 的单个 `$`。

接下来看 cocotb 接管后，Python 测试台是怎么启动电路的。`setup.py` 的 `setup()` 函数是核心：

[test/helpers/setup.py:7-17](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/setup.py#L7-L17) — 函数签名接收两个 `Memory` 对象（程序内存、数据内存）及其内容、线程数。前两段是「建时钟」与「复位」：用 `cocotb.clock.Clock` 建一个 25us 周期的时钟并 `start_soon`；然后把 `dut.reset` 拉高，等一个 `RisingEdge(dut.clk)`（上升沿），再拉低，完成一次复位脉冲。

`RisingEdge(dut.clk)` 是 cocotb 的「触发器」：它会暂停这个 Python 协程，直到 `clk` 出现下一个上升沿再继续。这是 Python 测试台与硬件时钟同步的基本手段。

[test/helpers/setup.py:24-34](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/setup.py#L24-L34) — 「装载内存」与「写 DCR」：`program_memory.load(program)` / `data_memory.load(data)` 把指令和数据灌进 Python 模拟的外部内存（详见 [u3-l1](u3-l1-memory-model-interface.md)，本讲只需知道它把数据填进去）；然后 `device_control_write_enable=1`、`device_control_data=threads`，等一个上升沿把 `thread_count` 写进设备控制寄存器，再撤销写使能。

[test/helpers/setup.py:36-37](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/setup.py#L36-L37) — 「拉 start」：把 `dut.start` 拉高，正式触发 dispatcher 开始派发线程、启动 kernel。注意 `setup()` 到这里就返回了，**主仿真循环并不在这里**，而在各个测试文件（例如 `test_matadd.py`）里。

主循环长这样（节选自 `test_matadd.py`，这里只看它与工具链相关的部分）：

[test/test_matadd.py:50-58](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L50-L58) — 主循环：只要 `dut.done != 1`，就每个周期调用 `data_memory.run()` / `program_memory.run()` 让外部内存响应硬件请求，`await cocotb.triggers.ReadOnly()` 等到本拍信号稳定，`format_cycle(dut, cycles)` 把本周期状态写进日志，最后 `await RisingEdge(dut.clk)` 推进一个时钟沿、`cycles += 1`。这就是「Python 每拍驱动一次、Verilog 推进一拍」的循环结构。

#### 4.3.4 代码实践

**实践目标**：理解 cocotb「Python 协程 + 时钟沿」的驱动方式。

**操作步骤**：

1. 打开 `test/helpers/setup.py`，在 `setup()` 里找到 5 个 `await RisingEdge(dut.clk)` 或 `cocotb.start_soon(...)` 调用。
2. 对照 4.3.2 的五步启动序列，在每一步旁边标注它是第几步。
3. 回答：`setup()` 函数里，从「拉高 reset」到「拉低 reset」之间，发生了几次时钟上升沿？

**需要观察的现象**：纯源码阅读，不运行命令。

**预期结果**：

| 启动序列步骤 | 对应代码 |
| --- | --- |
| 1. 建时钟 | `Clock(...)` + `cocotb.start_soon(clock.start())` |
| 2. 复位 | `reset=1` → `await RisingEdge` → `reset=0` |
| 3. 装程序内存 | `program_memory.load(program)` |
| 4. 装数据内存 | `data_memory.load(data)` |
| 5. 写 DCR + start | `device_control_write_enable=1`... → `start=1` |

「拉高 reset」到「拉低 reset」之间只等了**一次** `RisingEdge(dut.clk)`，所以是 1 个时钟上升沿——也就是一个单周期的复位脉冲。

#### 4.3.5 小练习与答案

**练习 1**：`iverilog` 命令里 `-s gpu` 这个参数可以省掉吗？为什么？

**参考答案**：不建议省。`-s` 显式指定顶层模块为 `gpu`，让仿真器知道从哪个模块开始实例化整棵电路。如果省略，`iverilog` 会按自己的规则（通常是文件里最后定义的、或某个自动推断的顶层）来选顶层，可能选错模块导致仿真行为异常。显式写 `-s gpu` 更稳妥。

**练习 2**：如果某台机器上同时装了多个 Python 版本，`cocotb-config --libpython` 这个命令的意义是什么？

**参考答案**：它返回**当前 cocotb 所绑定的那个 Python 解释器**对应的 libpython 路径。cocotb 的 VPI 模块必须和一个具体的 Python 动态库对接，才能在仿真过程中执行 Python 测试代码。把这个路径通过 `LIBPYTHON_LOC` 传给仿真器，就保证了 Verilog 仿真器加载的是「和 cocotb 同一个 Python」，避免版本不匹配导致桥接失败。

---

### 4.4 日志输出位置 test/logs

#### 4.4.1 概念说明

仿真跑完后，我们怎么知道硬件「干了什么」？tiny-gpu 的做法是把每个周期的内部状态都写进一个文本日志文件，放在 `test/logs/` 目录下。这份日志是后续所有讲义（尤其是 [u6-l2 执行轨迹格式化与阅读](u6-l2-execution-trace.md)）的基础，本讲先解决最基本的问题：**日志在哪、叫什么名字、分几段**。

一份完整的日志包含三段：

1. **初始数据内存**：仿真开始时、kernel 执行前的数据内存快照。
2. **执行轨迹**：逐周期记录每个 core、每个线程的指令、PC、状态、寄存器值。
3. **最终数据内存**：kernel 执行完、`done=1` 后的数据内存快照（包含计算结果）。

#### 4.4.2 核心流程

日志的写入完全由 Python 端控制，链路如下：

```text
test/helpers/logger.py
   定义 Logger 类，文件名 = test/logs/log_<YYYYMMDDHHMMSS>.txt
   logger.info/debug(...) → 以追加模式写入该文件
        ▲
        │ 调用
test/helpers/format.py
   format_cycle(dut, cycle) → 把每个周期的信号格式化成文本 → logger.debug(...)
test/helpers/memory.py
   Memory.display(rows)   → 打印内存表（初始/最终快照） → logger.info(...)
        ▲
        │ 调用
test/test_matadd.py
   data_memory.display(24)  ← 在主循环「之前」调用一次（初始）
   data_memory.display(24)  ← 在 done 之后调用一次（最终）
```

关键点：日志文件名带**时间戳**，所以每次仿真都会生成一个新文件，不会覆盖历史日志。

#### 4.4.3 源码精读

日志文件的命名规则定义在 `logger.py`：

[test/helpers/logger.py:1-17](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/logger.py#L1-L17) — 第 5 行用 `datetime.datetime.now()` 生成时间戳，文件名形如 `test/logs/log_20260728000000.txt`。`info()` 方法以 `"a"`（追加）模式打开文件写一行。`debug()` 在 `level=="debug"` 时直接调 `info()`，所以默认级别下所有 `format_cycle` 的输出都会落盘。

「初始/最终数据内存」这两段快照来自 `Memory.display()`：

[test/helpers/memory.py:79-99](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/memory.py#L79-L99) — `display(rows)` 打印一张内存表：表头是 `{name.upper()} MEMORY`（即 `DATA MEMORY`），然后每行 `| 地址 | 数据 |`，最多打印 `rows` 行。这就是日志里「数据内存」段落的来源。

而它在 `test_matadd.py` 里被调用两次，正好对应「初始」和「最终」：

[test/test_matadd.py:47](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L47) — 主循环**之前**的 `data_memory.display(24)`，打印 kernel 执行前的数据内存（初始快照）。

[test/test_matadd.py:61](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L61) — 主循环**之后**（`done=1`）的 `data_memory.display(24)`，打印 kernel 执行完的数据内存（最终快照，含结果）。

> 小贴士：日志里数据内存段落的标题是英文 **`DATA MEMORY`**，不是中文。本讲实践任务要找的「初始数据内存」「最终数据内存」，对应的就是日志里**第一段**和**最后一段** `DATA MEMORY` 表格。

#### 4.4.4 代码实践

**实践目标**：在日志里定位三段内容，验证你能读懂「数据内存」表格。

**操作步骤**：

1. 假设你已经跑过 `make test_matadd`，进入 `test/logs/` 找到最新的一份 `log_*.txt`。
2. 打开它，从头往下找第一段 `DATA MEMORY` 表格（这是「初始数据内存」）。
3. 在文件**末尾**找最后一段 `DATA MEMORY` 表格（这是「最终数据内存」）。
4. 对比两段表格：地址 0–7 和 8–15 行在两段里应该**一样**（分别是矩阵 A 和矩阵 B）；地址 16–23 行在「最终」段里应该出现计算结果。

**需要观察的现象**：

- 初始段：地址 16–23 都是 `0`（结果区还没被写入）。
- 最终段：地址 16–23 出现非零的值。

**预期结果**：矩阵 A = `[0,1,2,3,4,5,6,7]`（地址 0–7），矩阵 B = `[0,1,2,3,4,5,6,7]`（地址 8–15），二者逐元素相加，结果应写到地址 16–23：

| 地址 | 含义 | 初始 | 最终（A[i]+B[i]） |
| --- | --- | --- | --- |
| 16 | C[0] = 0+0 | 0 | 0 |
| 17 | C[1] = 1+1 | 0 | 2 |
| 18 | C[2] = 2+2 | 0 | 4 |
| 19 | C[3] = 3+3 | 0 | 6 |
| 20 | C[4] = 4+4 | 0 | 8 |
| 21 | C[5] = 5+5 | 0 | 10 |
| 22 | C[6] = 6+6 | 0 | 12 |
| 23 | C[7] = 7+7 | 0 | 14 |

上表的「最终」列就是 `test_matadd.py` 末尾断言检查的 `expected_results`（见 [test/test_matadd.py:63-66](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L63-L66)）。**待本地验证**：实际数值取决于运行时数据，但因为数据在源码里写死，结果应当与上表完全一致。

#### 4.4.5 小练习与答案

**练习 1**：每次运行 `make test_matadd`，日志文件名都一样吗？这样设计有什么好处？

**参考答案**：不一样。文件名带时间戳 `log_YYYYMMDDHHMMSS.txt`，每次仿真生成一个新文件。好处是历史日志不会被覆盖，方便对比多次运行（例如改了源码或内核后，对比新旧日志看变化）。

**练习 2**：日志里地址 0–15 在「初始」和「最终」两段里应该相同，为什么？

**参考答案**：因为 matadd 内核只**读取**矩阵 A（地址 0–7）和矩阵 B（地址 8–15）作为输入，**写入**的是结果区地址 16–23。输入数据在 kernel 执行过程中不会被修改，所以两段快照里地址 0–15 保持一致。

---

## 5. 综合实践

把本讲学到的工具链知识串起来，完成一次「端到端」的矩阵加法仿真，并亲手定位日志里的结果。

**任务**：安装工具链 → 跑通 `make test_matadd` → 在日志里找到矩阵加法的计算结果，并用源码解释为什么是这个结果。

**操作步骤**：

1. **安装工具链**（依据 README 的 Simulation 一节）：
   - `brew install icarus-verilog`（安装 iverilog/vvp）
   - `pip3 install cocotb`（安装 cocotb，提供 `cocotb-config`）
   - 从 [sv2v releases](https://github.com/zachjs/sv2v/releases) 下载二进制，解压后放进 `$PATH`
   - 在仓库根目录 `mkdir build`
2. **运行仿真**：`make test_matadd`。如果报错，按 `Makefile` 的三步（`make compile` / `iverilog` / `vvp`）分别单独运行，定位是哪一步失败。
3. **打开日志**：进入 `test/logs/`，打开最新的一份 `log_*.txt`。
4. **定位三段内容**：
   - 找到第一段 `DATA MEMORY`（初始），记录地址 0–7、8–15 的值。
   - 找到最后一段 `DATA MEMORY`（最终），记录地址 16–23 的值。
   - 浏览中间的 `Cycle N` 段落，挑一个周期，看看 `Core`、`Thread`、`Core State`、`PC` 这些字段长什么样（详细读法留到 [u6-l2](u6-l2-execution-trace.md)）。
5. **验证结果**：确认最终段地址 16–23 分别是 `0, 2, 4, 6, 8, 10, 12, 14`，即 `A[i] + B[i]`。

**需要观察的现象**：

- 终端里能看到 `Completed in N cycles` 之类的输出（来自 [test/test_matadd.py:60](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L60)）。
- `test/logs/` 下出现一个新的 `log_*.txt` 文件。
- 该日志包含初始/最终两段 `DATA MEMORY` 表格，以及中间大量 `Cycle N` 段落。

**预期结果**：最终段地址 16–23 = `[0, 2, 4, 6, 8, 10, 12, 14]`。如果断言通过（仿真无报错退出），就说明硬件正确完成了 8 个线程的逐元素加法。

**如果环境装不起来怎么办**：即使无法运行，你也可以完成「源码阅读型」版本——直接读 `test/test_matadd.py` 的 `program` 列表与 `data` 列表，手工模拟「8 个线程各算一个 C[i] = A[i] + B[i]」，得出地址 16–23 的预期值，并与本节给出的表格核对。这也是本讲所要求的最低限度。

## 6. 本讲小结

- tiny-gpu 的仿真工具链由四件工具串成：`sv2v`（SystemVerilog→Verilog）→ `iverilog`（编译成 `.vvp`）→ `vvp`（运行仿真）→ `cocotb`（用 Python 写测试台驱动仿真）。
- 入口是 `make test_<名字>`，它命中 `Makefile` 的 `test_%` 模式规则，依次执行「`make compile` → `iverilog` → `vvp`+cocotb」三步。
- `compile` 目标里 `sv2v -I src/*` 会让 `-I` 吃掉字母序最前的 `src/alu.sv`，所以 `alu` 要用 `make compile_alu` 单独编译、再用 `cat` 拼回 `build/gpu.v`，最后补一行 `` `timescale ``。
- cocotb 通过 VPI 把 Python 和 Verilog 仿真器连起来；`Makefile` 第 3 行的 `LIBPYTHON_LOC` 与第 8 行的 `-m libcocotbvpi_icarus` 就是搭桥的关键。
- Python 测试台（`setup.py`）用 `RisingEdge(dut.clk)` 这个触发器与硬件时钟同步，完成「建时钟→复位→装内存→写 DCR→拉 start」五步启动。
- 仿真日志写到 `test/logs/log_<时间戳>.txt`，包含「初始数据内存」「逐周期执行轨迹」「最终数据内存」三段；数据内存段落的标题是英文 `DATA MEMORY`。

## 7. 下一步学习建议

本讲让你跑通了仿真，但日志中间那一大段「逐周期执行轨迹」我们只是扫了一眼，还没真正读懂。建议：

1. **紧接着读 [u6-l2 执行轨迹格式化与阅读](u6-l2-execution-trace.md)**：它专门讲解 `format.py` 如何把硬件信号翻译成可读的指令/状态/寄存器轨迹，让你能像看调试器一样看每个周期发生了什么。虽然它在进阶单元，但作为入门读者，你在本讲已经握有了打开它的钥匙（一份真实的日志）。
2. **或者按「自顶向下」主线进入 [u2-l1 gpu.sv 顶层架构](u2-l1-gpu-top-level.md)**：从 `gpu.sv` 开始下探，看顶层模块是如何把 DCR、内存控制器、dispatcher、core 连起来的——本讲提到的 `start`、`done`、`device_control_*` 等端口，都将在那里得到硬件层面的解释。
3. **想动手写代码的读者**：可以直接跳到 [u6-l3 编写与仿真内核](u6-l3-writing-kernels.md)，仿照 `test_matadd.py` 写一个自己的最小内核，并用本讲学到的工具链跑起来。

无论选哪条路，记得保留本讲生成的 `test/logs/log_*.txt`——它是后续所有讲义最趁手的「实验数据」。
