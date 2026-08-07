# 形式化验证体系（SymbiYosys）

## 1. 本讲目标

读完本讲后，你应该能够：

- 说清楚「形式化验证（formal verification）」和「仿真（simulation）」的根本区别，理解 **prove（证明）** 与 **cover（覆盖）** 两种模式各在回答什么问题。
- 读懂 ZipCPU 在 `bench/formal/` 下用 **SymbiYosys（.sby）** 组织的整套证明体系：`TESTS` 列表覆盖了哪些模块、`make formal` 是怎么把它们串起来的、一个 `.sby` 文件里的 `[tasks]`/`[options]`/`[engines]`/`[script]`/`[files]` 各自做什么。
- 看懂一份 `.sby` 如何用 **tags + pycode** 把同一份 RTL 在多种参数（指令宽度、总线宽度、大小端）下逐一证明。
- 理解 `fwb_master` / `fwb_slave` 这两个「属性封装」**为何只含断言而没有任何功能逻辑**，以及它们如何把 Wishbone 总线契约从主/从两个视角写成可机器检验的规则。
- 学会为一个模块**运行**一个子证明、**解读**通过/失败的含义，并知道**新增**一个证明需要改动哪几处。

## 2. 前置知识

本讲是专家层（advanced）内容，假定你已经学过：

- **u3-l1 zipcore 总体结构与流水线阶段**：知道 ZipCPU 是一份 Verilog RTL，内核 `zipcore` 与取指/访存控制器分离。
- **u4-l1 Wishbone 封装 zipwb 与 zipbones** 与 **u4-l4 总线支持模块 rtl/ex**：知道 Wishbone 的 `cyc/stb/we/addr/data/sel`（主设备输出）与 `ack/stall/idata/err`（从设备输出）信号，以及 `fwb_master`/`fwb_slave` 是夹在主/从设备与总线之间的形式化属性封装。

下面用最少的篇幅补三个本讲要用、但前面讲义没细讲的概念。

### 2.1 仿真 vs 形式化验证

**仿真**是「我先给你一个具体的输入，你跑一遍，看输出对不对」。它的致命弱点是：你只能测到你想到的那些输入，测不到的输入里可能藏着 bug。对一个 32 位地址、32 位数据的总线接口，可能的输入组合是天文数字，仿真永远跑不完。

**形式化验证**换了一个问法：它不跑具体输入，而是让数学求解器（SMT solver）去**搜索**——「是否存在任何一种输入序列，能让某条 `assert` 断言被违反？」如果求解器证明了「不存在」，那就是一个**数学证明**，对所有输入成立，而不只是你测过的那些。ZipCPU 用 **SymbiYosys**（简称 sby）作为前端，底层调用 yosys 把 RTL 综合成逻辑公式，再交给 SMT 求解器（如 smtbmc 用的 Z3/Boolector/Yices 等）去判定。

### 2.2 两种最关键的模式：prove 与 cover

| 模式 | 中文 | 回答的问题 | 通过（PASS）意味着 | 失败（FAIL）意味着 |
|------|------|-----------|-------------------|-------------------|
| `mode prove` | 证明 | 「有没有任何输入能让 `assert` 失败？」 | 设计**永远不会**违反断言 | 找到了反例，有 bug |
| `mode cover` | 覆盖 | 「能不能找到一组输入让 `cover` 语句被命中？」 | 设计**确实能**到达这个状态 | 这段逻辑是死代码，永远走不到 |

二者互补：`prove` 防止「做错」，`cover` 防止「做不到」（比如某个 `ack` 分支永远没被触发，说明硬件漏了一条通路）。ZipCPU 对很多模块同时跑 `prf`（prove）和 `cvr`（cover）两套任务。

### 2.3 depth（证明深度）

形式化求解通常不能无限往未来看，必须设一个**边界**：只检查从复位开始的头 N 个时钟周期。这个 N 就是 `depth`。`depth 8` 表示「证明在第 8 拍之内，没有任何反例」。这不是完整证明，但对流水线这种状态有限的电路，足够深的 `depth` 在实践中等价于完整证明。`depth` 越大越慢、越可信。

### 2.4 四个 SystemVerilog/Verilog 形式化原语

| 原语 | 含义 |
|------|------|
| `assert(cond)` | 「设计必须保证 cond 恒真」，违反即 bug |
| `assume(cond)` | 「我假定环境会保证 cond 恒真」，用来约束对端的合法行为 |
| `cover(cond)` | 「希望至少存在一种输入让 cond 真过」，验证可达性 |
| `$past(sig)` | 信号 `sig` 在**上一个时钟沿**的值；`$stable(sig)` 等价于 `sig == $past(sig)` |

关键直觉：**`assert` 是对自己（被验证方）的承诺，`assume` 是对别人（环境/对端）的要求。** 这一点在 4.4 节会反复用到。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [Makefile](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/Makefile#L70-L76)（仓库根） | 顶层调度的 `formal` 目标，转发到 `bench/formal/` |
| [bench/formal/Makefile](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/Makefile#L42-L49) | 形式化验证的「司令部」：`TESTS` 列表 + 每个模块的证明规则 |
| [bench/formal/prefetch.sby](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/prefetch.sby) | 取指模块 `prefetch` 的 SymbiYosys 证明配置（本讲主讲示例） |
| [bench/formal/pfcache.sby](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/pfcache.sby) | 指令缓存 `pfcache` 的证明配置，含 prove+cover 两种模式 |
| [bench/formal/icontrol.sby](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/icontrol.sby) | 中断控制器证明配置，演示从端口（slave）视角 |
| [bench/formal/ffetch.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/ffetch.v) | 「取指契约」属性封装（CPU 与取指模块之间的接口契约） |
| [rtl/ex/fwb_master.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/fwb_master.v) | Wishbone **主设备**视角的契约属性（无功能逻辑） |
| [rtl/ex/fwb_slave.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/fwb_slave.v) | Wishbone **从设备**视角的契约属性（无功能逻辑） |
| [rtl/peripherals/icontrol.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/icontrol.v#L213-L340) | 中断控制器：在 `\`ifdef FORMAL` 块里实例化 `fwb_slave`，演示绑定模式 |
| [rtl/core/prefetch.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/prefetch.v#L494-L512) | 取指模块：在 FORMAL 块里实例化 `fwb_master`，演示绑定模式 |

---

## 4. 核心概念与源码讲解

### 4.1 形式化验证入门：从「跑输入」到「找反例」

#### 4.1.1 概念说明

前面三讲（u3、u4）你读的都是「怎么实现」CPU 与总线。但 RTL 写出来后，怎么知道它是对的？ZipCPU 给出的答案有两套，对应 `sim/` 和 `bench/formal/` 两个目录：

- **仿真**（`sim/verilator`、`sim/cpp`）：跑具体程序，测「典型情况」。
- **形式化验证**（`bench/formal`）：让求解器搜「**任何**情况」，证「无反例」。

二者不是替代关系，而是互补。形式化验证特别擅长抓仿真很难构造的边角案例——例如「总线 `cyc` 拉高的同时从设备返回 `err`，且上一拍正好是复位后第一拍」这种罕见组合，仿真几乎不会主动构造，但形式化验证会系统性地搜索到。

#### 4.1.2 核心流程

一个形式化证明的执行过程可以概括为：

1. **加载**：SymbiYosys 按 `.sby` 的 `[script]`，用 yosys 的 `read -formal` 读入 RTL，自动定义预处理宏 `FORMAL`，于是源码里 `\`ifdef FORMAL ... \`endif` 包起来的断言块被编译进来。
2. **建模**：yosys 把电路（寄存器 + 组合逻辑 + assert/assume）翻译成 SMT 公式。每个时钟沿变成一次状态转移。
3. **求解**（prove 模式）：求解器从复位状态出发，逐拍展开到 `depth` 指定的深度，检查「是否存在一条路径使任何 `assert` 为假」。
   - 若**不存在** → 输出 `PASS`（已证明无反例）。
   - 若**存在** → 输出 `FAIL`，并给出一组能触发违例的输入波形（反例，counterexample），可在 GTKWave 里看。
4. **覆盖**（cover 模式）：求解器反过来找「是否存在一条路径使命中的 `cover` 语句为真」，用来证明某段逻辑不是死代码。

#### 4.1.3 源码精读

证明的「结果产物」是一个名为 `PASS` 的空文件。看 `bench/formal/Makefile` 如何判定一个子证明成功——每个目标都依赖一个 `<模块>_<任务>/PASS` 文件：

```makefile
$(PFONE)_prf/PASS: $(PFONE).sby $(RTL)/core/$(PFONE).v $(MASTER) $(IFETCH)
	$(NOJOBSERVER) sby -f $(PFONE).sby prf
```

这段规则（[bench/formal/Makefile:126-127](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/Makefile#L126-L127)）说明：运行 `sby -f prefetch.sby prf`，sby 会在 `prefetch_prf/` 目录里跑证明；若全部断言成立，sby 就在该目录下生成 `PASS` 文件，Make 据此认定这个子目标达成。`prf` 是 `.sby` 里 `[tasks]` 定义的任务名（见 4.3 节）。

#### 4.1.4 代码实践

**实践目标**：直观感受「证明失败」长什么样，而不是只看成功。

1. 操作步骤：
   - 在 `bench/formal/` 下，挑一个最简单的模块先跑通，例如除法器：`sby -f div.sby prf`（需要本机已装 yosys + 一个 SMT 求解器；若没装，见 4.2.4 的源码阅读型替代实践）。
   - 观察输出目录 `div_prf/`：里面有 `PASS` 文件即成功。
   - **人为制造一次失败**：临时拷贝一份 `div.v` 到 `/tmp`，在某个 `always` 块里把一个本该是 `+` 的地方改成 `-`，改写一份本地的 `.sby` 指向它，重跑 `sby`。
2. 需要观察的现象：失败时 sby 不生成 `PASS`，而是在 `engine_0/` 下生成一个反例波形文件（`.vcd`/`.json`），并打印类似 `Assert failed ...` 的信息和到达反例的时钟拍数。
3. 预期结果：原版 `div` 应 `PASS`；被你改坏的版本 `FAIL` 并给出反例。**若本地未安装 yosys，本步骤标注「待本地验证」。**

#### 4.1.5 小练习与答案

**练习 1**：有人说「仿真测了一百万个随机输入都没 bug，就可以放心了」。形式化验证的哪个特性使得它比这种说法更可信？

> **答案**：形式化验证（prove 模式）是对**所有**合法输入序列的数学证明（在 `depth` 边界内），而一百万个随机输入只是无穷集合里的极小样本，无法排除未采样到的反例。

**练习 2**：`depth 8` 通过了，能保证第 9 拍之后也没 bug 吗？

> **答案**：不能。`depth 8` 只证明了前 8 拍无反例。要更强保证需加大 `depth`；对状态有限的模块，达到一定深度后可视为完整证明（归纳），但 sby 的 `mode prove` 默认是 bounded model checking，不自动做归纳。

---

### 4.2 bench/formal 证明体系：`TESTS` 列表与 `make formal`

#### 4.2.1 概念说明

`bench/formal/` 不是一堆散落的 `.sby`，而是一套被 Makefile 严格编排的体系。要理解它，抓住三个东西：

- **`TESTS`**：一份「要证明哪些模块」的总清单。
- **每个模块的规则**：清单里每个名字都对应一组「子任务」，每个子任务跑 `.sby` 的一个 task，全部通过才算这个模块通过。
- **`make formal`**：顶层入口，把整套清单跑一遍并出报告。

#### 4.2.2 核心流程

```
make formal（根 Makefile）
   └─► bench/formal/  make all   （跑 TESTS 里每个模块）
   │      ├─ prefetch  → 5 个子任务（prf/prf8b/prf8ble/prf64b/prf128b）全 PASS
   │      ├─ memops    → 十多个子任务（不同总线宽/锁定/本地总线组合）全 PASS
   │      ├─ zipcore   → piped/nopipe/lowlogic/ice40 … 多配置全 PASS
   │      └─ …（共 30+ 模块）
   └─► bench/formal/  make report （perl genreport.pl → report.html）
```

`TESTS` 覆盖范围非常广：取指族（prefetch/dblfetch/pffifo/pfcache）、访存族（memops/pipemem/dcache）、AXI 族（axilfetch/axilops/axilpipe…）、外设（ziptimer/zipcounter/zipjiffies/wbwatchdog/icontrol/wbdmac）、总线胶水（busdelay/wbpriarbiter/wbdblpriarb）、执行单元（cpuops/div）、DMA 子模块（mm2s/s2mm/txgears/rxgears）、乃至整个内核与三种顶层封装（zipcore/zipbones/zipaxil/zipaxi）。注释掉的是尚未启用的（如 `# axiicache`、`# axidcache`）。

#### 4.2.3 源码精读

先看总清单（[bench/formal/Makefile:42-49](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/Makefile#L42-L49)）：

```makefile
TESTS := prefetch dblfetch pffifo pfcache memops pipemem idecode div # axiicache
TESTS += axilfetch axilops axilpipe axilperiphs # axiops axipipe # axidcache
TESTS += zipmmu ziptimer zipcounter zipjiffies wbwatchdog icontrol wbdmac
TESTS += busdelay wbpriarbiter wbdblpriarb cpuops cpu dcache zipcore
TESTS += zipbones zipaxil zipaxi
TESTS += txgears rxgears s2mm mm2s memdev
.PHONY: $(TESTS)
all: $(TESTS)
```

- `all: $(TESTS)`：`make all`（在 `bench/formal/` 里）就是跑完整个 `TESTS` 清单。
- `cpu: zipcore`（[第 99-100 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/Makefile#L99-L100)）：`cpu` 只是 `zipcore` 的别名。
- 第 104-105 行定义了两个反复出现的依赖文件——主/从属性封装：

```makefile
MASTER := $(RTL)/ex/fwb_master.v
SLAVE  := $(RTL)/ex/fwb_slave.v
```

注意一个规律：**带总线主端口的模块**（prefetch、memops、wbdmac…）依赖 `$(MASTER)`，**带总线从端口的模块**（icontrol、ziptimer…）依赖 `$(SLAVE)`。这正是 4.4 节要讲的两面性。

再看一个典型模块的规则——取指 `prefetch`（[bench/formal/Makefile:122-136](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/Makefile#L122-L136)）：

```makefile
$(PFONE) : $(PFONE)_prf/PASS $(PFONE)_prf64b/PASS $(PFONE)_prf128b/PASS
$(PFONE) : $(PFONE)_prf8b/PASS $(PFONE)_prf8ble/PASS
```

含义：模块名 `prefetch` 这个目标，必须 5 个子任务（`prf`/`prf8b`/`prf8ble`/`prf64b`/`prf128b`）**全部**生成 `PASS` 才算通过。这 5 个任务分别对应不同的指令宽度（32/8 位）×总线宽度（32/64/128 位）×字节序（大小端），见 4.3 节。

顶层入口在根 [Makefile:70-76](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/Makefile#L70-L76)：

```makefile
formal:
	@echo "Running formal proofs";
	+@$(SUBMAKE) bench/formal/
	+@$(SUBMAKE) bench/formal/ report
```

`report` 目标（[bench/formal/Makefile:824-826](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/Makefile#L824-L826)）调用 `genreport.pl` 扫描所有 `*/PASS` 文件，生成一份 HTML 汇总，一眼看出哪些模块/任务已证明、哪些失败或缺失。

#### 4.2.4 代码实践

**实践目标**：不动手跑（可能没装 yosys），改为「读 Makefile 画证明矩阵」，理解覆盖范围。

1. 操作步骤：
   - 打开 [bench/formal/Makefile](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/Makefile)，对 `TESTS` 里每个模块，数一下它依赖了几个 `*_*/PASS` 子目标（例如 `memops` 有十几个，`zipcounter` 只有 1 个 `prf`）。
   - 列一张表：模块名 | 依赖 `MASTER` 还是 `SLAVE` | 子任务数 | 是否有 `cvr`（cover）任务。
2. 需要观察的现象：执行单元（div/cpuops）子任务少（配置维度低）；访存/缓存类（memops/dcache）子任务极多（因为要组合「总线宽/流水/锁定/本地总线」多维度）；只有部分模块（pfcache/icontrol/memops…）同时有 `prf` 和 `cvr`。
3. 预期结果：你能据此说出「`memops` 为什么要跑这么多子任务」——因为它要把每种总线宽度、是否流水、是否锁定、是否本地总线都各证明一遍。

**进阶（若本地已装 yosys+sby）**：在 `bench/formal/` 下执行 `make prefetch`，观察它依次跑 5 个子任务并在 `prefetch_prf/` 等目录留下 `PASS` 文件。**若未安装，标注「待本地验证」。**

#### 4.2.5 小练习与答案

**练习 1**：`TESTS` 里 `zipcounter` 只有一个 `prf` 子任务（[第 535-537 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/Makefile#L535-L537)），而 `memops` 有十几个。为什么差这么多？

> **答案**：子任务数等于「需要枚举的配置维度数」。`zipcounter` 是个简单的从端口计数器，没有可配置的总线宽度/流水/锁定维度，所以只需一次证明；`memops` 的行为随总线宽度（32/64/128）、是否流水访问、是否锁定、是否命中本地总线而变化，每个维度组合都要单独证明。

**练习 2**：`MASTER` 和 `SLAVE` 这两个变量分别指向哪个文件？一个模块该依赖哪个，由什么决定？

> **答案**：`MASTER = rtl/ex/fwb_master.v`，`SLAVE = rtl/ex/fwb_slave.v`。由该模块在 Wishbone 总线上扮演的角色决定：若模块是**主设备**（主动发起 `cyc/stb`，如取指/访存控制器、DMA），依赖 `MASTER`；若是**从设备**（响应别人的访问，如中断控制器、定时器等外设），依赖 `SLAVE`。

---

### 4.3 `.sby` 证明配置详解：以 `prefetch.sby` 为例

#### 4.3.1 概念说明

`.sby` 是 SymbiYosys 的配置文件，用 INI 风格的分节写成。它告诉 sby 一切：要证明哪个顶层、用哪个引擎、读哪些文件、以及——最有特色的一点——**用一份配置派生出多个「任务（task）」**，每个任务在不同的参数下证明同一份 RTL。本节用 `prefetch.sby` 把每一节讲透。

#### 4.3.2 核心流程

一份 `.sby` 的五个标准节：

```
[tasks]    定义若干任务名，每个任务继承一组 tags（标签）
[options]  每个（或所有）任务的 mode（prove/cover）和 depth
[engines]  用哪个求解后端（smtbmc / abc / smtbmc boolector …）
[script]   yosys 脚本：read 哪些 .v、设哪些参数（chparam）、prep 哪个 top
[files]    依赖的源文件清单（sby 会把它们拷进工作目录）
```

`prefetch.sby` 一口气定义了 5 个任务，对应 5 种「指令宽度 × 总线宽度 × 字节序」组合：

| 任务名 | tags | 含义 |
|--------|------|------|
| `prf` | insn32, bus32 | 32 位指令、32 位总线（默认） |
| `prf8b` | prf, insn8, bus32 | 8 位指令、32 位总线 |
| `prf8ble` | prf, insn8, bus32, lilend | 同上，但小端 |
| `prf64b` | prf, insn32, bus64 | 32 位指令、64 位总线 |
| `prf128b` | prf, insn32, bus128 | 32 位指令、128 位总线 |

tags 不是 yosys 原生概念，而是 sby 的机制：任务声明的第二列以后都是 tags，它们会被喂进 `[script]` 里的 `--pycode--` Python 片段，用来**动态生成** `chparam` 命令。

#### 4.3.3 源码精读

逐节看 [bench/formal/prefetch.sby](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/prefetch.sby)。

**`[tasks]` 与 `[options]`（[第 1-11 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/prefetch.sby#L1-L11)）**：

```ini
[tasks]
prf         insn32 bus32
prf8b   prf insn8  bus32
...
[options]
prf: mode prove
depth 8
prf128b: depth 14
```

- `mode prove`：证明模式（找反例）。
- `depth 8`：写在 `[options]` 顶格表示对所有任务的默认深度；`prf: mode prove` 前缀 `prf:` 表示只对 `prf` 任务（及其继承者）生效。
- `prf128b: depth 14`：128 位总线状态空间更大，单独把深度提到 14。

**`[engines]`（[第 13-14 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/prefetch.sby#L13-L14)）**：`smtbmc` 是基于 SMT 的有界模型检测引擎，最常用。

**`[script]`（[第 16-35 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/prefetch.sby#L16-L35)）**——这是 `.sby` 最核心、也最值得读的一节：

```ini
read -formal fwb_master.v
read -formal ffetch.v
read -formal -DPREFETCH prefetch.v
--pycode-begin--
cmd = "hierarchy -top prefetch"
if ("insn8" in tags):
    cmd += " -chparam INSN_WIDTH 8"
elif ("insn32" in tags):
    cmd += " -chparam INSN_WIDTH 32"
if ("bus32" in tags):
    cmd += " -chparam DATA_WIDTH 32"
...
cmd += " -chparam OPT_LITTLE_ENDIAN %d" % (1 if "lilend" in tags else 0)
output(cmd)
--pycode-end--
prep -top prefetch
```

要点：
1. `read -formal fwb_master.v` 先读入主设备契约属性；`ffetch.v` 读入取指接口契约；`-DPREFETCH` 给 `prefetch.v` 定义宏 `PREFETCH`（模块内部用 `\`ifdef PREFETCH` 区分自己是被当作 prefetch 还是 pfcache 复用）。
2. `--pycode--` 段是一段 Python：它检查当前任务的 `tags`，**拼出**对应的 `hierarchy ... -chparam ...` 命令字符串，再用 `output(cmd)` 喂回给 yosys。于是同一份 `prefetch.v` 会被以 `INSN_WIDTH=8/32`、`DATA_WIDTH=32/64/128`、`OPT_LITTLE_ENDIAN=0/1` 等不同参数综合，分别证明。
3. `prep -top prefetch`：把 `prefetch` 确立为顶层，准备送给求解器。

**`[files]`（[第 37-40 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/prefetch.sby#L37-L40)）**：列出 sby 需要拷进每个任务工作目录的文件。注意路径是相对 `.sby` 所在目录的，所以 RTL 用 `../../rtl/core/prefetch.v`。

对比一份更完整的 [pfcache.sby](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/pfcache.sby#L7-L11)，它同时有 prove 和 cover：

```ini
[options]
prf: mode prove
prf: depth  5
cvr: mode cover
cvr: depth 60
```

`cvr`（cover）用 `depth 60`，因为覆盖往往需要更多拍才能「走到」目标状态。这说明作者既证明「不会出错」，又确认「该走的路走得通」。

#### 4.3.4 代码实践

**实践目标**：读懂 pycode 参数化机制，并预测一个新任务的参数。

1. 操作步骤：
   - 假设你想新增一个任务 `prf256b`，证明 256 位总线下的 `pfcache`。阅读 [pfcache.sby 的 pycode（第 20-32 行）](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/pfcache.sby#L20-L32)，写出你需要在 `[tasks]` 加的一行，以及 pycode 已经能处理它（注意第 28-29 行已有 `bus256` 分支）。
   - 再看 `prefetch.sby`：它的 pycode **没有** `bus256` 分支，所以若你给 `prefetch` 加 `prf256b`，会发生什么？
2. 需要观察的现象：`pfcache.sby` 的 pycode 会把 `bus256` 翻译成 `-chparam BUS_WIDTH 256`；而 `prefetch.sby` 的 pycode 没处理 `bus256`，所以加了也不会改 `DATA_WIDTH`（默认值生效），任务会「跑但参数没变」，等于白跑。
3. 预期结果：你能复述「tags → pycode → chparam」这条链路，并解释为什么新增一个总线宽度任务时，往往要同时改 pycode。
4. 若本地已装 sby：运行 `sby -f pfcache.sby cvr`，观察 cover 任务输出（会列出各 `cover` 语句是否被命中）。**未安装则标注「待本地验证」。**

#### 4.3.5 小练习与答案

**练习 1**：`prefetch.sby` 里 `prf128b: depth 14`，为什么单独把这一个任务的深度调高？

> **答案**：128 位总线一个时钟周期能搬 4 个 32 位字，缓存行/缓冲相关的状态更多、需要更多拍才能展开到稳定行为，默认 `depth 8` 可能不足以覆盖关键状态转移，所以单独加深到 14。

**练习 2**：`read -formal -DPREFETCH prefetch.v` 里的 `-DPREFETCH` 起什么作用？去掉会怎样？

> **答案**：它给 `prefetch.v` 定义预处理宏 `PREFETCH`。`prefetch.v` 内部可能用 `\`ifdef PREFETCH` 来启用「单条取指」专属的代码路径（与被当作 `pfcache` 复用时区分）。去掉后那些受保护路径不会被编译，证明的对象就变了。

---

### 4.4 `fwb_master` / `fwb_slave`：把 Wishbone 契约写成断言

#### 4.4.1 概念说明

这是本讲最核心、也最精妙的部分。Wishbone 总线有一份「君子协定」（spec）：比如「`stb` 为真时 `cyc` 必须也为真」「`ack` 和 `err` 不能同拍为真」「一个请求发出后若被 `stall`，其地址/数据/方向不能变」……这些规则谁负责遵守？

答案是：**主设备**和**从设备**各自负责一半。`fwb_master` 和 `fwb_slave` 就是把这份契约**从两个视角分别写成 assert/assume**，且——注意——**它们不含任何功能逻辑**，纯断言。为什么没有功能逻辑？因为它们不是电路，而是「检查器」。在 4.4.3 你会看到，这两个模块的唯一「输出」`f_nreqs`/`f_nacks`/`f_outstanding` 只是几个计数器，专门给后续断言当探针用，综合进真实硬件时会被 `FORMAL` 宏隔离掉。

两个文件的视角分工（见 [fwb_master.v 头注释第 18-24 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/fwb_master.v#L18-L24)）：

| | 对主设备输出（cyc/stb/we/addr/data/sel） | 对主设备输入（ack/stall/idata/err） |
|---|---|---|
| **fwb_master**（被主设备实例化） | `assert`（我保证自己输出合法） | `assume`（我假定从设备回的应答合法） |
| **fwb_slave**（被从设备实例化） | `assume`（我假定主设备的请求合法） | `assert`（我保证自己输出合法） |

直觉记忆：**「assert 管『我说出去的话』，assume 管『别人对我说的话』。」** 主设备实例化 `fwb_master`，于是「我说出去的 `cyc/stb`」是我的输出，要 assert；而「从设备回我的 `ack`」是我的输入，只能 assume（约束环境）。从设备正好相反。

#### 4.4.2 核心流程

绑定与证明的完整链条：

```
模块源码（如 icontrol.v）
  └─ \`ifdef FORMAL 块里：
       实例化 fwb_slave，把自己的 o_wb_ack/i_wb_cyc 等接进去
       再写几条模块特有的 assert（如 f_outstanding==0）
.sby 配置
  └─ read -formal 读入 模块.v + fwb_slave.v（或 fwb_master.v）
  └─ sby 求解：对模块的所有输入（受 fwb_slave 的 assume 约束），
            检查模块自己的 assert + fwb_slave 的 assert 是否恒成立
```

`fwb_master`/`fwb_slave` 内部维护三个探针（[fwb_master.v:131-133](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/fwb_master.v#L131-L133)）：

- `f_nreqs`：累计「已被接受」的请求数（`stb && !stall` 时 +1）。
- `f_nacks`：累计收到的应答数（`ack || err` 时 +1）。
- `f_outstanding = f_nreqs - f_nacks`：当前在途（发了但没收到应答）的请求数。

它们让上层模块能写出像「我这个从端口不应该有在途请求」（`assert(f_outstanding == 0)`）这样的性质。

#### 4.4.3 源码精读

**（1）「无功能逻辑」的明证**

[fwb_master.v 头注释第 12-16 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/fwb_master.v#L12-L16) 直接声明：

> *This module contains no functional logic. It is intended for formal verification only. The outputs returned ... are designed for further formal verification purposes \*only\*.*

模块端口（[第 66-134 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/fwb_master.v#L66-L134)）只有 `input`（总线信号）和三个 `output` 探针——没有任何驱动真实电路的输出。注意它带了一串参数（[第 66-116 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/fwb_master.v#L66-L116)），用来**调档契约的松紧**：

| 参数 | 含义 |
|------|------|
| `F_MAX_STALL` | 假定从设备最多连续 stall 多少拍 |
| `F_MAX_ACK_DELAY` | 假定从设备最多延迟多少拍才 ack |
| `F_LGDEPTH` | 计数器位宽（\(2^{\text{F\_LGDEPTH}}\) 是最大可追踪在途数） |
| `F_MAX_REQUESTS` | 限定在途请求上限 |
| `F_OPT_RMW_BUS_OPTION` | 是否允许「读-改-写」时 cyc 常高 |
| `F_OPT_DISCONTINUOUS` | 是否允许不连续请求 |
| `F_OPT_SOURCE` | 是否为事务的最初发起方 |

**（2）一个最直观的契约：`stb` 必须伴随 `cyc`**

[fwb_master.v:234-236](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/fwb_master.v#L234-L236)：

```verilog
// STB can only be true if CYC is also true
always @(*)
if (i_wb_stb)
    `SLAVE_ASSUME(i_wb_cyc);
```

在 `fwb_master` 里，`SLAVE_ASSUME` 被重定义为 `assert`（[第 136-137 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/fwb_master.v#L136-L137)）：`define SLAVE_ASSUME assert`。所以这里主设备在**断言**「只要我发出 `stb`，我一定也拉着 `cyc`」。

而同样这一行，在 [fwb_slave.v:225-227](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/fwb_slave.v#L225-L227) 里，`SLAVE_ASSUME` 就是 `assume`（[fwb_slave.v:127-128](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/fwb_slave.v#L127-L128)）：从设备**假定**「主设备发 `stb` 时一定拉着 `cyc`」。

这就是头注释里说的「`SLAVE_ASSUME`/`SLAVE_ASSERT` 宏把两个文件的对齐，使 diff 只显示真正的视角差异」——同一行文字，在主设备里是 assert，在从设备里是 assume，纯粹靠宏切换。

**（3）互斥与计数**

`ack` 和 `err` 不能同拍为真（[fwb_master.v:324-325](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/fwb_master.v#L324-L325)）：

```verilog
always @(*)
    `SLAVE_ASSERT((!i_wb_ack)||(!i_wb_err));
```

在途请求计数器（[fwb_master.v:398-422](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/fwb_master.v#L398-L422)）：

```verilog
always @(posedge i_clk)
if ((i_reset)||(!i_wb_cyc))
    f_nreqs <= 0;
else if ((i_wb_stb)&&(!i_wb_stall))
    f_nreqs <= f_nreqs + 1'b1;     // 请求被接受，计数+1
...
assign f_outstanding = (i_wb_cyc) ? (f_nreqs - f_nacks):0;
```

并由 `F_MAX_REQUESTS` 约束在途上限（[第 424-435 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/fwb_master.v#L424-L435)）：若设置了上限，则 `assert(f_nacks <= f_nreqs)`（应答不会比请求多）、`assert(f_outstanding < MAX_OUTSTANDING)`。

**（4）绑定：模块如何挂上这个检查器**

以中断控制器 `icontrol`（从设备视角）为例。它的整个形式化段被 `\`ifdef FORMAL` 包住（[icontrol.v:213](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/icontrol.v#L213)），里面实例化 `fwb_slave`（[icontrol.v:331-337](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/icontrol.v#L331-L337)）：

```verilog
fwb_slave #(.DW(DW), .AW(1), .F_MAX_STALL(0), .F_MAX_ACK_DELAY(1),
    .F_LGDEPTH(2), .F_MAX_REQUESTS(1), .F_OPT_MINCLOCK_DELAY(0))
    fwb(i_clk, i_reset,
        i_wb_cyc, i_wb_stb, i_wb_we,
        1'b0, i_wb_data, 4'hf,
        o_wb_ack, o_wb_stall, o_wb_data, 1'b0,
        f_nreqs, f_nacks, f_outstanding);

always @(*)
    assert(f_outstanding == 0);
```

注意：`icontrol` 把自己的**输出** `o_wb_ack/o_wb_stall/o_wb_data` 接到 `fwb_slave` 的应答输入，把**输入** `i_wb_cyc/i_wb_stb` 接到请求输入——这正是「从设备视角」。最后一条 `assert(f_outstanding == 0)` 是模块特有性质：「我作为一个寄存器型外设，每个请求都当拍应答，永远不该有在途请求」。

主设备侧看取指模块 `prefetch`（[prefetch.v:494-512](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/prefetch.v#L494-L512)）：

```verilog
fwb_master #(
    .AW(AW-$clog2(DATA_WIDTH/8)), .DW(DW),.F_LGDEPTH(F_LGDEPTH),
    .F_MAX_REQUESTS(1), .F_OPT_SOURCE(1),
    .F_OPT_RMW_BUS_OPTION(0),
    .F_OPT_DISCONTINUOUS(0)
) f_wbm(
    .i_clk(i_clk), .i_reset(i_reset),
    .i_wb_cyc(o_wb_cyc), .i_wb_stb(o_wb_stb), .i_wb_we(o_wb_we), ...   // 主设备的输出
    .i_wb_ack(i_wb_ack), .i_wb_stall(i_wb_stall), ...                   // 主设备的输入
    .f_nreqs(f_nreqs), .f_nacks(f_nacks), .f_outstanding(f_outstanding)
);
```

这里把 `prefetch` 的**输出** `o_wb_cyc/o_wb_stb` 接到 `fwb_master`——「主设备视角」。`F_OPT_SOURCE(1)` 表示它是事务的最初发起方，于是启用了「开 cyc 时必须同时拉 stb」的额外断言（[fwb_master.v:535-546](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/fwb_master.v#L535-L546)）。

#### 4.4.4 代码实践

**实践目标**：解释「`fwb_master` 为何只含断言而无功能逻辑」，并亲手读懂一条契约。

1. 操作步骤：
   - 打开 [rtl/ex/fwb_master.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/fwb_master.v)，通览整个模块体，统计里面出现了多少个 `assert`/`assume`（通过 `SLAVE_ASSUME`/`SLAVE_ASSERT` 宏），以及多少个会对真实硬件输出产生影响的赋值。
   - 在 [icontrol.sby](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/icontrol.sby) 中确认：它 `read -formal` 了 `icontrol.v` 和 `fwb_slave.v` 两个文件（[第 14-17 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/icontrol.sby#L14-L17)），证明顶层是 `icontrol`。
2. 需要观察的现象：
   - `fwb_master` 模块体里**几乎只有** `always @(*) assert(...)` 和 `always @(posedge i_clk) ...` 形式的断言/计数；唯一的「输出赋值」是给 `f_nreqs`/`f_nacks`/`f_outstanding` 这三个探针，而它们只服务于断言，不接到任何功能通路。文件末尾甚至有一段 `wire unused; assign unused = &{ 1'b0, f_request };` 仅仅是为了「让 Verilator 不报 UNUSED 警告」（[第 549-555 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/fwb_master.v#L549-L555)）——这反证了它没有功能输出。
   - `icontrol.sby` 里 `mode prove`、`depth 5`，且 `icontrol` 实例化了 `fwb_slave`，于是求解器在 `icontrol` 的所有合法输入（受 `fwb_slave` 的 assume 约束）下，检查 `icontrol` 自己的 assert + `fwb_slave` 的 assert 是否恒真。
3. 预期结果：你能用自己的话回答「为什么 `fwb_master` 没有功能逻辑」——因为它是**检查器**而非**电路**；它的存在只是为了把 Wishbone 契约变成可被求解器检验的数学断言；综合进真实 FPGA 时，`\`ifdef FORMAL` 会把它整段排除，对硬件零面积、零时序影响。
4. **若本地已装 yosys+sby**：运行 `sby -f icontrol.sby prf`，确认中断控制器在 prove 模式下 PASS；再读 `icontrol_prf/` 目录里的日志，找到求解器检查过的断言列表。**未安装则标注「待本地验证」。**

#### 4.4.5 小练习与答案

**练习 1**：为什么 `fwb_master` 把 `SLAVE_ASSUME` 定义成 `assert`，而 `fwb_slave` 把它定义成 `assume`？

> **答案**：宏名里的「SLAVE」表示「这句话原本是从设备视角下的假设」。当我们在 `fwb_master`（主设备视角）里复用同一行文字时，从设备「假设主设备请求合法」的那句话，对主设备而言就变成了「断言自己请求合法」（assert）；反之在 `fwb_slave` 里它保持 `assume`。用宏切换可以让两个文件逐行对齐，diff 时只显示真正的视角差异，便于审阅。

**练习 2**：`icontrol` 在实例化 `fwb_slave` 后，紧接着写了 `assert(f_outstanding == 0)`。这条断言表达了 `icontrol` 的什么设计性质？如果它被违反，意味着什么？

> **答案**：表达「`icontrol` 作为寄存器型从设备，对每个请求都当拍应答（`ack` 与 `stb` 同拍），永远不会留下在途请求」。若被违反，说明存在某个输入序列下 `icontrol` 收了请求却没当拍回 `ack`，即它违背了「单拍应答」的承诺——这正是形式化要抓的 bug。

**练习 3**：如果把 `fwb_master.v` 综合进真实 FPGA（不靠 `\`ifdef FORMAL` 排除），会综合出什么电路？

> **答案**：`assert`/`assume` 在常规综合里会被忽略（它们不是硬件原语），剩下的只有 `f_nreqs`/`f_nacks`/`f_outstanding` 这几个计数寄存器和纯内部的 `f_request` 拼接——它们不驱动任何对外输出，是死逻辑，综合器会优化掉。所以即使不排除，它对功能也无影响；但用 `\`ifdef FORMAL` 排除是为了干净（不让这些断言文本干扰 lint/仿真）。这也再次说明它是「检查器」而非「电路」。

---

## 5. 综合实践

**任务**：为一个新的从设备外设「接上」形式化证明，跑通一个 prove 子任务。

假定你要给一个假想的 Wishbone 从设备 `myreg.v`（只有一个可读寄存器，当拍应答）加证明。请按本讲所学，写出最小改动方案：

1. **在 `myreg.v` 末尾加形式化段**（参考 `icontrol.v` 的模式）：
   - 用 `\`ifdef FORMAL ... \`endif` 包住。
   - 声明 `wire f_nreqs, f_nacks, f_outstanding;`。
   - 实例化 `fwb_slave`，把你的 `i_wb_cyc/i_wb_stb/...` 与 `o_wb_ack/o_wb_stall/o_wb_data/...` 按从设备视角接进去（请求信号接 `i_wb_*`，应答信号接 `i_wb_ack/i_wb_stall/i_wb_idata/i_wb_err`）。
   - 加一条 `assert(f_outstanding == 0);`（因为你当拍应答）。
2. **新建 `bench/formal/myreg.sby`**（参考 `icontrol.sby`）：
   ```ini
   [tasks]
   prf
   [options]
   prf: mode prove
   prf: depth 5
   [engines]
   smtbmc
   [script]
   read -formal -DMYREG myreg.v
   read -formal fwb_slave.v
   prep -top myreg
   [files]
   ../../rtl/peripherals/myreg.v
   ../../rtl/ex/fwb_slave.v
   ```
3. **在 `bench/formal/Makefile` 注册**：在 `TESTS` 里加 `myreg`，并仿照 `zipcounter` 的规则（[第 533-538 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/Makefile#L533-L538)）加一条：
   ```makefile
   .PHONY: myreg
   myreg: myreg_prf/PASS
   myreg_prf/PASS: myreg.sby ../../rtl/peripherals/myreg.v $(SLAVE)
       sby -f myreg.sby prf
   ```
4. **运行与解读**：`make myreg`。
   - 通过：`myreg_prf/PASS` 生成，求解器证明了「在所有合法 Wishbone 输入下，`myreg` 的应答行为与 `fwb_slave` 契约都不被违反」。
   - 失败：打开 `myreg_prf/engine_0/` 下的反例波形，定位是哪条 assert 被违反（是 `fwb_slave` 的通用契约，还是你写的 `f_outstanding==0`），据此修 bug。

> 说明：本实践是「设计型」任务，不要求你真有一个 `myreg.v`；关键是把「源码加 FORMAL 段 → 写 .sby → 注册 Makefile → 解读 PASS/FAIL」这条链路走通。若手头有真实模块（例如照抄 `zipcounter`），可替换进去实跑。**能否本地实跑取决于是否已安装 yosys 与 sby。**

## 6. 本讲小结

- ZipCPU 用 **SymbiYosys（.sby）** 在 `bench/formal/` 下对 30+ 个模块做形式化证明，与 `sim/` 的仿真互补：仿真测「典型」，形式化证「无反例」。
- `bench/formal/Makefile` 的 **`TESTS`** 是总清单，每个模块又拆成多个**子任务**（不同总线宽/流水/锁定/大小端组合），全部生成 `*/PASS` 才算通过；顶层 `make formal` 跑完全部并出 HTML 报告。
- 一份 `.sby` 用 **`[tasks]` tags + `--pycode--`** 把同一份 RTL 在多种参数下逐一证明；`mode prove` 找反例、`mode cover` 验可达，二者互补；`depth` 限定证明的时间边界。
- `fwb_master`/`fwb_slave` 是**纯断言、零功能逻辑**的「契约检查器」，把 Wishbone 协议从主/从两个视角写成 assert/assume，靠 `SLAVE_ASSUME`/`SLAVE_ASSERT` 宏让两份文件逐行对齐。
- 模块在自己的 `\`ifdef FORMAL` 块里实例化 `fwb_master`（主设备）或 `fwb_slave`（从设备），把总线信号接进去，再加几条模块特有断言（如 `f_outstanding==0`）；三个探针 `f_nreqs`/`f_nacks`/`f_outstanding` 供这些断言使用。
- 新增一个证明只需三步：源码加 FORMAL 段、写 `.sby`、在 Makefile 注册规则。

## 7. 下一步学习建议

- **横向读完所有 `.sby`**：按 `bench/formal/` 里的文件，对比 `memops.sby`、`dcache.sby`、`zipcore.sby` 的 `[tasks]` 维度，体会「配置空间越大、子任务越多」的规律，并试着读懂 `zipcore.sby` 如何把整个 CPU 核心当作一个顶层来证明。
- **纵向读 `ffetch.v` / `fmem.v` / `fdebug.v`**：这三个是「接口契约」属性封装（分别对应取指、访存、调试端口），思路与 `fwb_master`/`fwb_slave` 一脉相承——把一段接口协议写成 assert/assume。读懂 `ffetch.v` 里用 `(* anyconst *)` 声明的 `r_fc_pc/r_fc_insn` 如何作为「任意但固定」的参考值来校验取指结果。
- **动手装一套环境**：按 `INSTALL.md` 装好 yosys、Z3/boolector、sby 与 Verilator，然后跑 `make formal` 的一个子集（如 `make div cpuops icontrol`），亲眼看 `PASS` 文件生成与 `report.html` 汇总。
- **结合 u5-l3（Verilator 测试框架）**：对比「形式化证明」与「Verilator 仿真测试台」两套手段各自能抓什么 bug，建立「仿真 + 形式化」的双保险验证观。
