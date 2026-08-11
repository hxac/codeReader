# 仿真环境搭建：iverilog 与 gtkwave

## 1. 本讲目标

学完本讲后，你应该能够：

- 在本地安装并配置好 Icarus Verilog（iverilog）和 gtkwave 两个开源工具。
- 说清楚 `source setenv.sh` 之后环境里多了什么（`OH_HOME`），以及它为什么重要。
- 独立讲出 OH! 仿真的「三步流程」：`build.sh`（编译）→ `sim.sh`（运行 `.emf` 测试）→ `view.sh`（看波形）。
- 读懂 `libs.cmd` 里的 `-y` 和 `+incdir+` 在做什么，并理解 `iverilog -g2005 -DTARGET_SIM=1 -DCFG_ASIC=0` 这几个开关的含义。
- 识别 `run.sh`、`build_all.sh`、`view.sh` 中引用的 `src/`、`common/`、`memory/` 等历史路径在当前仓库里并不存在，遇到报错时能自己定位原因。

> ⚠️ 重要提醒：本讲的脚本和 `libs.cmd` 中存在若干「文档/脚本与实际目录不一致」的历史路径问题。我们不会回避它，而是把它当作一次真实的排错练习——这正是 u1-l2 强调的「代码与协议文件才是事实，文档可能滞后」。

## 2. 前置知识

在动手之前，先建立几个最基础的概念。如果你已经熟悉，可以跳到第 3 节。

- **HDL 仿真（simulation）**：用软件模拟硬件描述语言（HDL）写的电路，观察信号随时间的变化，而不需要真的把电路做进芯片。仿真是在流片（tapeout）之前验证设计正确性的主要手段。
- **Verilog 2005**：Verilog 硬件描述语言的一个标准化版本（IEEE 1364-2005）。OH! 全部用这个版本写成，所以编译时必须告诉 iverilog「按 2005 标准来」。
- **Icarus Verilog（iverilog）**：一个开源的 Verilog 仿真器，分两步工作：先用 `iverilog` 把源码编译成一个可执行文件（仿真模型），再运行这个可执行文件产生结果。它是 OH! 官方推荐的仿真器。
- **gtkwave**：一个开源的波形查看器，用来打开仿真产生的 `.vcd`（Value Change Dump）文件，把信号画成随时间变化的波形图。
- **VCD 文件**：记录「每个时刻哪些信号发生了变化」的标准文本格式，几乎所有 Verilog 仿真器都能输出它。
- **`.emf` 文件**：OH! 自定义的测试激励格式，每一行代表一个对被测器件（DUT）的事务（transaction），例如「向地址 A 写入数据 D」。本讲只需把它理解成「喂给仿真器的一串输入」，详细字段含义留给 u4-l2。
- **环境变量**：操作系统层面的「全局字符串」。`export OH_HOME=...` 就是定义一个名为 `OH_HOME` 的环境变量，后续脚本通过 `$OH_HOME` 读取它。

## 3. 本讲源码地图

本讲涉及的关键文件都在仓库根目录或 `scripts/`、`stdlib/testbench/` 下，全是脚本与配置，不涉及 RTL 设计本身：

| 文件 | 作用 |
| --- | --- |
| `setenv.sh` | 设置 `OH_HOME` 环境变量，指向仓库根目录。一切脚本依赖它。 |
| `scripts/build.sh` | 用 iverilog 把一个 `dut_*.v`（被测器件包装）编译成 `dut.bin`。 |
| `scripts/sim.sh` | 把指定的 `.emf` 测试文件软链接成 `test_0.emf`，然后运行 `dut.bin`。 |
| `scripts/view.sh` | 用 gtkwave 打开 `waveform.vcd` 看波形。 |
| `run.sh` | 顶层「一键编译+仿真」脚本（注意它假设了 `src/` 路径前缀）。 |
| `scripts/build_all.sh` | 批量编译多个模块的 `dut`（同样假设了 `src/` 路径）。 |
| `stdlib/testbench/libs.cmd` | iverilog 的「命令文件」，列出所有 `-y` 库目录与 `+incdir+` 头文件目录。 |

> 这些脚本总共只有几十行，但它们串联起了整个 OH! 的仿真流程，是读懂后续所有模块仿真的钥匙。

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：**4.1 `OH_HOME` 环境变量** → **4.2 iverilog 编译流程** → **4.3 `.emf` 运行与波形查看**。

---

### 4.1 OH_HOME 环境变量

#### 4.1.1 概念说明

OH! 的所有脚本都不写死路径，而是约定一个「根目录」环境变量 `OH_HOME`。只要 `OH_HOME` 指向仓库根目录，脚本就能用 `$OH_HOME/scripts/build.sh`、`$OH_HOME/scripts/libs.cmd` 这样的绝对路径找到所需文件，无论你当前在哪个目录下运行。

这样做的好处是：脚本可以在任意子目录下调用，且方便多版本共存（不同版本的 OH! 用不同 `OH_HOME`）。

#### 4.1.2 核心流程

1. 进入仓库根目录（`OH_HOME` 必须等于根目录，否则相对路径会错位）。
2. **用 `source`（而不是直接执行）运行 `setenv.sh`**，使 `export` 生效于你当前的 shell。
3. 验证：`echo $OH_HOME` 应输出仓库根目录的绝对路径。

> 关键点：`setenv.sh` 里写的是 `export OH_HOME=$PWD`。`$PWD` 是「当前目录」。如果你直接 `./setenv.sh` 执行，它会在一个子 shell 里跑，`export` 只对那个子 shell 生效，你自己的 shell 里 `OH_HOME` 仍然是空的。所以**必须 `source setenv.sh` 或 `. setenv.sh`**。

#### 4.1.3 源码精读

`setenv.sh` 全文只有一行有意义的语句：

- 设置 `OH_HOME` 为当前工作目录（[setenv.sh:4](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/setenv.sh#L4)）。

```bash
#!/bin/bash
#Setting OH_HOME variable
export OH_HOME=$PWD
```

注意它使用了 `$PWD`，这决定了**调用前你必须 `cd` 到仓库根目录**，否则 `OH_HOME` 会指向错误位置。脚本头部第一行 `#!/bin/bash` 是 shebang，告诉系统用 bash 解释。

#### 4.1.4 代码实践

**实践目标**：正确设置 `OH_HOME` 并验证它生效。

**操作步骤**：

1. 在终端进入仓库根目录：

   ```bash
   cd /path/to/oh
   ```

2. 用 `source` 执行：

   ```bash
   source setenv.sh
   ```

3. 验证：

   ```bash
   echo $OH_HOME
   ```

**需要观察的现象**：`echo $OH_HOME` 应打印出仓库根目录的绝对路径。

**预期结果**：路径以 `/.../oh` 结尾。

**对比实验**（理解 `source` 的必要性）：在另一个终端里直接 `./setenv.sh` 然后立刻 `echo $OH_HOME`，你会发现输出为空——这就是「子 shell」效应。

#### 4.1.5 小练习与答案

**练习 1**：如果误用了 `./setenv.sh` 而不是 `source setenv.sh`，接下来运行 `build.sh` 会发生什么？

**参考答案**：`build.sh` 里引用了 `$OH_HOME/scripts/libs.cmd`，由于 `OH_HOME` 为空，它会被展开成 `/scripts/libs.cmd`，iverilog 报「找不到命令文件」之类的错误。所以必须先 `source setenv.sh`。

**练习 2**：为什么 `setenv.sh` 里用 `$PWD` 而不是写死绝对路径？

**参考答案**：用 `$PWD` 让脚本可移植——不同机器、不同克隆位置都能用，只要调用时位于仓库根目录即可。这也意味着调用前的「当前目录」必须正确。

---

### 4.2 iverilog 编译流程

#### 4.2.1 概念说明

iverilog 是「编译型」仿真器：它先把 Verilog 源码编译成一个可执行文件（OH! 里叫 `dut.bin`），再运行这个可执行文件产生仿真结果。这与一些「解释型」仿真器不同。

`build.sh` 就是封装了「调用 iverilog」的这一步。它做了三件事：

1. 指定语言标准为 Verilog 2005。
2. 用两个宏（`-D`）切换代码路径：`TARGET_SIM=1`、`CFG_ASIC=0`。
3. 用 `-f` 传入一份「命令文件」`libs.cmd`，其中列出了所有库目录与头文件目录，再传入用户指定的顶层 `dut_*.v` 文件。

理解这几个开关，是理解整个 OH! 仿真机制的关键。

#### 4.2.2 核心流程

`build.sh` 的伪代码流程：

```
iverilog -g2005                    # 用 Verilog 2005 标准
         -DTARGET_SIM=1            # 启用仿真专用代码分支
         -DCFG_ASIC=0              # 选择 soft(RTL) 实现，而非 hard(ASIC 宏)
         -f $OH_HOME/.../libs.cmd  # 读入命令文件：一堆 -y 和 +incdir+
         -o dut.bin                # 输出可执行仿真模型
         $1                        # 用户传入的顶层 dut 文件
```

关于三个关键开关：

- **`-g2005`**：告诉 iverilog 按 IEEE 1364-2005（Verilog 2005）标准解析源码。OH! 全库刻意只用这个子集，避免厂商方言，保证可移植。
- **`-DTARGET_SIM=1`**：定义宏 `TARGET_SIM`。OH! 里部分代码在「仿真」与「综合（上芯片）」两种场景下行为不同（例如时钟缓冲、延迟单元），用这个宏在编译期切换到仿真分支。
- **`-DCFG_ASIC=0`**：定义宏 `CFG_ASIC` 为 0。OH! 采用 **soft/hard 双实现**策略（详见 u9-l1）：`CFG_ASIC=0` 选可综合 RTL（`stdlib`），`CFG_ASIC=1` 选绑定工艺库的硬核（`asiclib`）。仿真时几乎总是用 `0`。

关于命令文件 `libs.cmd`（iverilog 的 `-f` 机制）：

- `-y <dir>`（library directory）：当一个模块被实例化却没在源码里直接定义时，iverilog 会到 `-y` 指定的目录里，按「文件名 = 模块名」的约定去找。例如实例化 `gpio`，就会去找 `gpio.v`。
- `+incdir+<dir>`（include directory）：源码里 `` `include "xxx.vh" `` 时，iverilog 到这些目录下查找头文件。

> 这两种机制合在一起，让 OH! 可以把几百个分散在十几个子目录里的 `.v` / `.vh` 文件按需自动拼装，而不必在每个编译命令里手写一长串文件名。

#### 4.2.3 源码精读

`build.sh` 的核心就是一条 iverilog 命令（[scripts/build.sh:15-19](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/scripts/build.sh#L15-L19)）：

```bash
iverilog -g2005\
 -DTARGET_SIM=1\
 -DCFG_ASIC=0\
 -f $OH_HOME/scripts/libs.cmd \
 -o dut.bin $1
```

- 脚本头部注释明确说明它依赖 `$OH_HOME`（[scripts/build.sh:6](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/scripts/build.sh#L6)），并给了用法示例（[scripts/build.sh:8](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/scripts/build.sh#L8)）：`./scripts/build.sh elink/hdl/dut_elink.v`。
- `$1` 是脚本的第一个参数，即用户传入的顶层 `dut_*.v` 文件路径。

命令文件 `libs.cmd` 列出了所有库目录与头文件目录，节选如下（[stdlib/testbench/libs.cmd:3-22](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/libs.cmd#L3-L22) 为 `-y` 段，[stdlib/testbench/libs.cmd:23-32](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/libs.cmd#L23-L32) 为 `+incdir+` 段）：

```
-y ../../common/dv
-y ../../emesh/dv
-y ../../memory/dv
...
-y ../../gpio/hdl
...
+incdir+../../emesh/hdl
+incdir+../../elink/hdl
...
```

> ⚠️ **路径排错要点（重要）**：请注意以下三处「文档/脚本与实际目录不一致」的历史遗留问题，这是本讲排错的核心：
>
> 1. **`build.sh` 引用的 `libs.cmd` 路径不存在**：脚本写的是 `-f $OH_HOME/scripts/libs.cmd`，但仓库里 `scripts/` 目录下**没有** `libs.cmd`。唯一存在的 `libs.cmd` 在 `stdlib/testbench/libs.cmd`（见本节上面的源码地图）。直接运行 `build.sh` 会因找不到命令文件而失败。
> 2. **`libs.cmd` 内部的相对路径基于 `stdlib/testbench/`**：其中的 `../../` 指向仓库根目录。所以 `../../gpio/hdl` 实际指向仓库根的 `gpio/hdl`，这是**存在**的。但 `../../common/dv`、`../../memory/dv`、`../../accelerator/hdl` 等指向的 `common/`、`memory/`、`accelerator/` 在当前仓库**并不存在**（见 u1-l2）。好在 iverilog 对 `-y` 指向不存在目录只是告警、不会致命错误，只要被测模块依赖的其它模块都能从**存在**的目录里找到，编译仍能成功。
> 3. **`build.sh` 假定你在仓库根目录运行**：因为它输出固定的 `dut.bin`（相对路径），且 `$1` 也按相对路径理解。

`build_all.sh` 进一步暴露了同样的历史路径假设（[scripts/build_all.sh:3](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/scripts/build_all.sh#L3)、[scripts/build_all.sh:5](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/scripts/build_all.sh#L5)、[scripts/build_all.sh:10](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/scripts/build_all.sh#L10)）：它用 `common/hdl/*.v`、`$OH_HOME/src/$dut/dv/dut_$dut.v`，并遍历 `accelerator`、`pic` 等已不存在的模块——这些路径在当前仓库下都无法直接使用。

#### 4.2.4 代码实践

**实践目标**：亲手编译一个 `dut`，理解每一步在做什么；遇到路径报错时能定位原因。

**操作步骤**：

1. 确保已 `source setenv.sh`（见 4.1.4）。
2. 尝试用官方脚本编译 gpio 的 dut：

   ```bash
   ./scripts/build.sh gpio/dv/dut_gpio.v
   ```

3. 观察报错。预期你会看到与命令文件路径相关的错误（因为 `scripts/libs.cmd` 不存在）。

4. **排错练习**：用真实存在的 `libs.cmd` 手动构造一条等价命令（在仓库根目录运行）：

   ```bash
   iverilog -g2005 -DTARGET_SIM=1 -DCFG_ASIC=0 \
     -f stdlib/testbench/libs.cmd \
     -o dut.bin gpio/dv/dut_gpio.v
   ```

5. 观察这条命令的输出。注意 `libs.cmd` 里的相对路径 `../../` 是相对于 `stdlib/testbench/` 的，所以必须把它作为 `-f` 参数、让 iverilog 在解析该文件时基于该文件所在目录展开相对路径（iverilog 对 `-f` 命令文件中的相对路径，是相对于**当前工作目录**解析的，因此这里在仓库根运行时，`../../emesh/dv` 会被解析成仓库根之上的目录，可能仍报缺目录告警）。

**需要观察的现象**：
- 步骤 2 是否报「cannot open … libs.cmd」之类错误。
- 步骤 4 是否产生 `dut.bin` 文件；`iverilog` 是否打印若干「Unable to find …」告警（对应不存在的 `common/`、`memory/` 等）。

**预期结果**：
- 步骤 2：失败，原因是 `$OH_HOME/scripts/libs.cmd` 不存在。
- 步骤 4：若 gpio 依赖的所有模块都能从存在的目录解析，则生成 `dut.bin`（可能伴随若干非致命告警）。**待本地验证**：实际能否成功取决于 iverilog 对 `-f` 内相对路径的解析行为与你机器上工具版本，请在本地确认。

> 如果步骤 4 仍因相对路径失败，可尝试 `cd stdlib/testbench` 后再以绝对路径传 `dut_gpio.v`，让 `libs.cmd` 里的 `../../` 正确指向仓库根。这正是一次真实的路径排错。

#### 4.2.5 小练习与答案

**练习 1**：把 `-DCFG_ASIC=0` 改成 `-DCFG_ASIC=1`，编译会发生什么变化？

**参考答案**：`CFG_ASIC=1` 会让代码选择 `asiclib` 下的硬核实现（如 `asic_dffq` 代替 `oh_dffq`）。这些硬核单元绑定特定工艺库，仿真时通常缺少对应的库模型，因此很可能报「找不到模块」错误。这也是为什么默认仿真用 `0`。详见 u9-l1。

**练习 2**：`libs.cmd` 里 `-y ../../gpio/hdl` 和 `+incdir+../../gpio/hdl` 同时出现，为什么？

**参考答案**：`-y` 是在实例化某模块时按文件名查找该模块**定义**（`.v`），`+incdir+` 是在源码里 `` `include `` 时查找**头文件**（`.vh`，如 `gpio_regmap.vh`）。gpio 目录里既有 `gpio.v`（定义）又有 `gpio_regmap.vh`（头文件），所以两类目录都要列出。

---

### 4.3 .emf 运行与波形查看

#### 4.3.1 概念说明

编译产出 `dut.bin` 之后，下一步是「运行」。OH! 的运行流程很巧妙：

- `dut.bin` 运行时，其内部的测试平台（testbench）会去读取一个固定名字的文件 `test_0.emf`，把它当作一串事务激励回放给被测器件。
- `sim.sh` 的职责就是把**你指定的** `.emf` 测试文件软链接（`ln -s`）成 `test_0.emf`，然后运行 `dut.bin`。
- 运行结束后会产出一个 `waveform.vcd` 波形文件，用 `view.sh` 调 gtkwave 打开。

这样的设计让你只需更换 `.emf` 文件，就能对同一个 `dut.bin` 跑不同的测试用例，而不必重新编译。

> `.emf` 的字段含义（每行的「数据_数据_地址_控制_访问」十六进制事务）将在 u4-l2 详细讲解。本讲只需理解：每行是一个事务，`sim.sh` 负责把它喂给仿真器。

#### 4.3.2 核心流程

`sim.sh` 的流程：

```
若 test_0.emf 已是软链接 → 先 unlink 删除
ln -s <用户指定的.emf> test_0.emf   # 把测试文件伪装成固定名字
./dut.bin                          # 运行仿真，内部读 test_0.emf，输出 waveform.vcd
```

`view.sh` 的流程：

```
gtkwave -a <gtkw 配置> waveform.vcd   # 用 gtkwave 打开波形
```

把三步串起来（假设路径都已修正）：

```
build.sh  →  dut.bin
sim.sh    →  test_0.emf → dut.bin 运行 → waveform.vcd
view.sh   →  gtkwave 打开 waveform.vcd
```

#### 4.3.3 源码精读

`sim.sh` 全文（[scripts/sim.sh:1-7](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/scripts/sim.sh#L1-L7)）：

```bash
#!/bin/bash
if [ -L "test_0.emf" ]
then
    unlink test_0.emf
fi
ln -s $1 test_0.emf
./dut.bin
```

- `[ -L "test_0.emf" ]` 判断当前目录下是否已存在名为 `test_0.emf` 的软链接；若是则先 `unlink` 删除，避免下次 `ln -s` 因目标已存在而失败。
- `ln -s $1 test_0.emf` 把第一个参数（用户指定的 `.emf`）软链接为 `test_0.emf`。
- `./dut.bin` 运行编译好的仿真模型。

`view.sh` 全文（[scripts/view.sh:3](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/scripts/view.sh#L3)）：

```bash
gtkwave -a $OH_HOME/common/dv/oh.gtkw waveform.vcd
```

- `-a` 让 gtkwave 加载一个 `.gtkw` 配置文件，里面预设了要显示哪些信号、分组、颜色等，省去每次手动添加信号的麻烦。
- ⚠️ 这里又一次出现了历史路径问题：`$OH_HOME/common/dv/oh.gtkw` 不存在（仓库里没有 `common/` 目录）。实际存在的配置文件是 `stdlib/testbench/oh.gtkw`。所以 `view.sh` 直接运行也会失败，需要把路径改成 `stdlib/testbench/oh.gtkw`（或仓库里实际存在的 `.gtkw`）。

顶层 `run.sh` 把 build 和 sim 合二为一（[run.sh:3-4](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/run.sh#L3-L4)）：

```bash
./scripts/build.sh src/$1/dv/dut_$1.v
./scripts/sim.sh src/$1/dv/tests/test_basic.emf
```

- 用法示例：`./run.sh gpio` 会被展开成编译 `src/gpio/dv/dut_gpio.v` 并仿真 `src/gpio/dv/tests/test_basic.emf`。
- ⚠️ 它假设了 `src/` 路径前缀，而当前仓库**没有** `src/` 目录，实际文件在 `gpio/dv/dut_gpio.v`、`gpio/dv/tests/test_basic.emf`。所以 `run.sh` 原样运行必然报「找不到文件」。修正方法是把 `src/` 前缀去掉，或把模块目录软链接到 `src/` 下。

一个真实的 `.emf` 测试文件示例，gpio 的 `test_basic.emf`（[gpio/dv/tests/test_basic.emf](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/tests/test_basic.emf)）节选：

```
DEADBEEF_FFFF0000_00000000_05_0010 // write gpio_dir
DEADBEEF_FFFFFFFF_00000010_05_0010 // write gpio_out
...
DEADBEEF_DEADBEEF_00000008_04_0010 // read gpio_in
```

每行末尾的 `//` 注释说明了这行事务在做什么（写 `gpio_dir` 寄存器、写 `gpio_out`、读 `gpio_in` 等）。`sim.sh` 就是把这样一个文件喂给 `dut.bin`。

#### 4.3.4 代码实践

**实践目标**：把编译好的 `dut.bin` 跑起来，并查看波形。

**前置条件**：4.2.4 已成功生成 `dut.bin`（若未成功，先完成排错或阅读 4.3.5 的源码阅读型练习）。

**操作步骤**：

1. 运行仿真（在 `dut.bin` 所在目录）：

   ```bash
   ./scripts/sim.sh gpio/dv/tests/test_basic.emf
   ```

2. 观察是否生成 `test_0.emf` 软链接和 `waveform.vcd` 文件：

   ```bash
   ls -l test_0.emf waveform.vcd
   ```

3. 修正 `view.sh` 的路径后查看波形：

   ```bash
   gtkwave -a stdlib/testbench/oh.gtkw waveform.vcd
   ```

**需要观察的现象**：
- `sim.sh` 运行时终端会打印仿真器输出（可能包含事务日志、错误计数、仿真结束标志）。
- 运行后当前目录多出 `test_0.emf`（软链接）与 `waveform.vcd`。
- gtkwave 窗口打开后能看到 `clk`、`nreset`、`access_in`、`packet_in`、`access_out`、`packet_out` 等信号的波形。

**预期结果**：仿真正常结束并产出 `waveform.vcd`；gtkwave 能显示信号波形。**待本地验证**：具体终端输出与能否完整跑通，取决于本地工具链与路径修正情况。

> 提示：若你暂时无法在本地装好 iverilog/gtkwave，下面的 4.3.5 给了一个纯源码阅读型的替代实践，同样能达成学习目标。

#### 4.3.5 小练习与答案

**练习 1**：`sim.sh` 为什么先 `unlink` 再 `ln -s`，而不是直接 `ln -sf`？

**参考答案**：两种写法效果相近。脚本作者选择显式 `[ -L ... ]` 判断再 `unlink`，是一种更谨慎的写法：只在 `test_0.emf` 确实是软链接时才删除，避免误删同名普通文件。`ln -sf` 则会强制覆盖。功能上都为了「重复运行不报错」。

**练习 2**：`view.sh` 里 `-a $OH_HOME/common/dv/oh.gtkw` 的 `-a` 选项去掉会怎样？

**参考答案**：去掉 `-a` 后，gtkwave 仍能打开 `waveform.vcd`，但不会预载信号分组配置——你会看到一个空波形窗口，需要自己手动把信号一个个拖进来。`.gtkw` 文件保存的就是这些布局，省去重复劳动。

**练习 3**（源码阅读型，无需运行）：阅读 `run.sh`，说明为什么 `./run.sh gpio` 在当前仓库会失败，以及最小改动方案。

**参考答案**：`run.sh` 把路径写成 `src/$1/dv/dut_$1.v`，即 `src/gpio/dv/dut_gpio.v`，但仓库里没有 `src/` 前缀，真实文件是 `gpio/dv/dut_gpio.v`，所以报「找不到文件」。最小改动是把两行里的 `src/` 前缀去掉，改成 `./scripts/build.sh $1/dv/dut_$1.v` 与 `./scripts/sim.sh $1/dv/tests/test_basic.emf`。

---

## 5. 综合实践

**任务**：把本讲的三步流程完整跑通一次，并产出一份「环境问题清单」。

要求：

1. 安装 iverilog 与 gtkwave（Ubuntu/Debian：`sudo apt install iverilog gtkwave`）。
2. `source setenv.sh`，用 `echo $OH_HOME` 确认。
3. 选定 gpio 为目标模块，尝试按 `build → sim → view` 三步跑通仿真。**记录你遇到的每一个报错**，按下表整理：

   | 步骤 | 命令 | 报错信息 | 根因 | 你的修正办法 |
   | --- | --- | --- | --- | --- |
   | build | `./scripts/build.sh gpio/dv/dut_gpio.v` | （待填） | 如：`scripts/libs.cmd` 不存在 | 如：改用 `stdlib/testbench/libs.cmd` |
   | sim | `./scripts/sim.sh gpio/dv/tests/test_basic.emf` | （待填） | | |
   | view | `./scripts/view.sh` | （待填） | 如：`common/dv/oh.gtkw` 不存在 | 如：改用 `stdlib/testbench/oh.gtkw` |

4. 成功后，在 gtkwave 中找到 `packet_out` 信号，对照 `test_basic.emf` 里第一行 `write gpio_dir` 事务，观察输出包是否反映了该写操作的影响。

5. **进阶**：写一个一行的 shell 命令，把 `run.sh` 修正为不依赖 `src/` 前缀的版本，并验证 `./run.sh gpio` 能工作（提示：用 `sed` 或直接编辑一个本地副本，**不要改动仓库原始文件**）。

> 这个综合实践把「读脚本—跑仿真—排错—看波形」串成一条线。完成后，你就具备了独立仿真 OH! 任意模块的能力，也为 u4（通用测试平台）打下了环境基础。

## 6. 本讲小结

- OH! 的仿真流程是「三步走」：`build.sh` 编译出 `dut.bin` → `sim.sh` 把 `.emf` 喂给 `dut.bin` 运行 → `view.sh` 用 gtkwave 看 `waveform.vcd`。
- `source setenv.sh` 设置 `OH_HOME` 指向仓库根目录，是所有脚本能够互相找到的前提；必须用 `source` 而非直接执行。
- `build.sh` 的核心是 `iverilog -g2005 -DTARGET_SIM=1 -DCFG_ASIC=0 -f libs.cmd -o dut.bin dut.v`：`-g2005` 锁定语言标准，两个 `-D` 分别切换「仿真模式」与「soft 实现」，`-f` 引入库目录列表。
- `libs.cmd` 用 `-y`（按文件名找模块定义）和 `+incdir+`（找 `` `include `` 头文件）把分散在十几个子目录的源码自动拼装起来。
- **关键排错认知**：仓库里 `scripts/libs.cmd` 不存在（实为 `stdlib/testbench/libs.cmd`）、`common/` 与 `memory/` 与 `accelerator/` 目录不存在、`run.sh`/`build_all.sh` 假设的 `src/` 前缀不存在——遇到路径报错时优先怀疑这些历史遗留。
- `.emf` 是 OH! 的事务激励格式，`sim.sh` 通过软链接 `test_0.emf` 让同一个 `dut.bin` 能跑不同测试用例；字段细节留给 u4-l2。

## 7. 下一步学习建议

- 下一讲 **u1-l4（Verilog 2005 与 OH! 编码规范）** 会回到语言层面，结合最简单的 `stdlib` 模块讲解 OH! 的命名、参数化、复位等约定，为你随后阅读 `stdlib` 原语做准备。
- 想提前了解测试平台内部结构的读者，可以跳读 `stdlib/testbench/dv_top.v`、`stdlib/testbench/tb_dut.v`、`stdlib/testbench/testbench.v`，这会是 **u4-l1（通用测试平台架构）** 的主角。
- 想深入了解 `.emf` 字段格式的读者，可先看 `gpio/dv/tests/test_basic.emf` 与 `elink/dv/tests/` 下的测试文件，正式讲解在 **u4-l2（激励驱动与 .emf 测试格式）**。
- 建议在本讲把本地仿真环境真正跑通一次：后续每一讲的代码实践都会依赖它。
