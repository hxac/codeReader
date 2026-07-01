# 目录结构与代码导航

## 1. 本讲目标

学完本讲，你应该能够：

- 看懂 Bedrock 顶层目录是怎么划分的，每个子目录大概负责什么。
- 说出 `dir_list.mk` 是干什么的，以及它如何用「绝对路径变量」把整个仓库串起来。
- 拿到任意一个子目录的 `Makefile`，能认出它的标准骨架（include 哪几个文件、各自作用）。
- 仅凭文件名后缀（`_tb.v`、`.gtkw`、`rules.mk` 等）判断一个文件的职责。
- 用 `grep` / `glob` 在这个大型 Verilog 库里快速定位「某个模块定义在哪、被谁实例化、有没有测试台」。

本讲是入门层的第三讲，承接 u1-l1（项目总览）和 u1-l2（构建与运行）。u1-l1 让你知道 Bedrock 有哪些子系统，u1-l2 让你能跑通测试，本讲则教你「在这座大城市里怎么认路、怎么找门牌号」。

## 2. 前置知识

本讲假设你已经：

- 读过 README 顶层说明，知道 Bedrock 是一个由多个子系统（dsp、cordic、localbus、badger……）聚合而成的 Verilog HDL 库（见 u1-l1）。
- 大致了解 GNU Make 的概念：`make <目标>` 会按 `Makefile` 里的规则去执行命令；知道 `include` 表示把另一个文件的内容插入进来。
- 知道 Verilog 的基本文件类型：`.v` 是源码（模块定义），`.vh` 是头文件（宏、任务、参数的集合，用 `` `include `` 引入）。

如果你对 Make 完全陌生，只需先记住一句话即可跟上本讲：**Makefile 里一行 `变量名 = 值` 定义变量，`$(变量名)` 引用它；`include 文件` 把那个文件的内容原样搬过来。** Bedrock 的目录组织大量依赖这两个机制。

下面几个术语会反复出现，先解释清楚：

| 术语 | 含义 |
| --- | --- |
| 子系统（subsystem） | Bedrock 顶层的一个目录，比如 `dsp/`、`cordic/`，各自负责一类功能。 |
| 测试台（testbench） | 一段不带端口的 Verilog 代码，专门用来给被测模块喂激励、检查输出，文件名约定为 `<模块>_tb.v`。 |
| 波形配置（`.gtkw`） | GTKWave 波形查看器的「保存文件」，记录你想看哪些信号、怎么分组。 |
| 模式规则（pattern rule） | Make 的一种规则，用 `%` 作通配符，比如 `%_tb: %_tb.v` 表示「任何 `xxx_tb` 都由 `xxx_tb.v` 编译而来」。 |
| 自动生成文件 | 不是人手写、而是由脚本（Python）在构建时生成的文件，比如 `cordicg_b22.v`。它们**不进 git**。 |

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [dir_list.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dir_list.mk) | 仓库根目录的「地址簿」：自定位仓库根，然后为每个子系统定义一个绝对路径变量。 |
| [dsp/Makefile](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/Makefile) | `dsp/` 子目录的 Makefile，是「标准骨架」最精简的代表。 |
| [cordic/Makefile](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/Makefile) | `cordic/` 子目录的 Makefile，演示「带代码生成的叶子子系统」如何扩展标准骨架。 |
| [dsp/rules.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rules.mk) | `dsp/` 的本地规则文件：列出本目录所有测试台、定制目标、清理清单。 |
| [build-tools/top_rules.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk) | 全仓库共享的构建框架：工具名、编译/仿真变量、以及 `%_tb`/`%_check`/`%.vcd`/`%_view` 等模式规则。 |
| [build-tools/bottom_rules.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/bottom_rules.mk) | 共享框架的收尾：定义 `clean` 目标和清理校验。 |
| [dsp/mixer.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v) / [dsp/rot_dds_tb.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rot_dds_tb.v) / [dsp/rot_dds.gtkw](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rot_dds.gtkw) | 三个用于讲解「命名约定」的真实样本。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：前两个是规定要掌握的核心（`dir_list.mk` 与子目录 Makefile），后两个（命名约定、grep/glob 导航）是达成学习目标「学会定位模块」所必需的工具。

### 4.1 dir_list.mk：用绝对路径变量串联整个仓库

#### 4.1.1 概念说明

Bedrock 是一个「子系统聚合体」：`dsp/`、`cordic/`、`localbus/`、`badger/`……几十个子目录分散在仓库各处，而且彼此会互相调用（比如 `dsp/` 里的模块会实例化 `cordic/` 里的 CORDIC 核）。当一个子目录的 Makefile 想引用另一个子目录里的文件时，它需要知道对方的**完整路径**。

如果每个 Makefile 都自己写死路径（比如 `/home/xxx/Bedrock/cordic`），那只要仓库换个位置、换台机器、或者被别的工程以子模块方式引用，所有路径就全错了。

`dir_list.mk` 就是为了解决这个问题而存在的「地址簿」。它只有 19 行，做两件事：

1. **自己找到仓库根目录在哪**（不依赖任何外部环境变量）。
2. **为每个子系统定义一个绝对路径变量**（比如 `CORDIC_DIR = <仓库根>/cordic`）。

之后任何子目录只要 `include ../dir_list.mk`，就能用 `$(CORDIC_DIR)` 这种稳健的方式引用别的子系统，完全不用关心仓库被放在磁盘的什么地方。

#### 4.1.2 核心流程

`dir_list.mk` 的自定位逻辑可以表示为下面这段伪流程：

```
1. 取「当前正在被 include 的文件列表」的最后一个  →  就是 dir_list.mk 自己的路径
2. 对它取 abspath（转成绝对路径）                   →  MAKEF_PATH = .../Bedrock/dir_list.mk
3. 对它取 dir（去掉文件名，留目录，含末尾斜杠）       →  MAKEF_DIR  = .../Bedrock/
4. BEDROCK_DIR = MAKEF_DIR                          →  仓库根
5. 其余每个子系统变量 = BEDROCK_DIR + 子目录名
```

关键在于第 1 步用的 `$(MAKEFILE_LIST)`：这是 GNU Make 的内置变量，保存「make 当前已经读入的所有 Makefile 文件，按读取顺序排列」。当某子目录 `include ../dir_list.mk` 时，`dir_list.mk` 是最后一个被读入的，所以 `$(lastword $(MAKEFILE_LIST))` 就准确地指向它自己——**无论从哪个子目录 include 都成立**。这就是它能在任何位置自定位的原理。

#### 4.1.3 源码精读

先看自定位的头两行：

[dir_list.mk:1-5](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dir_list.mk#L1-L5) —— 这五行先取到自己所在的绝对路径，再算出仓库根 `BEDROCK_DIR`。注意第 4 行注释特意说明 `$(MAKEF_DIR)` 末尾**带斜杠**，所以后面拼接子目录时直接写字符串即可，不必再加 `/`。

接着是地址簿本体：

[dir_list.mk:5-18](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dir_list.mk#L5-L18) —— 这 14 个变量就是整个 Bedrock 的「子系统清单」。变量名本身就是文档：`BUILD_DIR` 指向 `build-tools/`（构建框架），`DSP_DIR` 指向 `dsp/`，`PICORV_DIR` 指向 `soc/picorv32/`（注意它跨了一层目录），以此类推。

把这张表和 u1-l1 里的子系统清单对照看：**dir_list.mk 就是 README 子系统说明的「可执行版本」**——README 用自然语言告诉你有哪些子系统，dir_list.mk 用 Make 变量把它们一一钉死成绝对路径。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**（不需要运行仿真）：

1. **目标**：验证 `dir_list.mk` 的自定位确实不依赖工作目录。
2. **操作步骤**：
   - 进入 `cordic/` 子目录，打开它的 `Makefile`，确认第一行是 `include ../dir_list.mk`（见 4.2.3）。
   - 在 `cordic/` 下执行 `make -p 2>/dev/null | grep '^DSP_DIR'`，查看 `DSP_DIR` 这个变量被展开成了什么值。
3. **需要观察的现象**：打印出的 `DSP_DIR` 应当是仓库里 `dsp/` 的**绝对路径**，而不是相对路径 `../dsp`。
4. **预期结果**：无论你在 `cordic/`、`dsp/` 还是 `badger/` 里查询，`$(DSP_DIR)` 都指向同一个绝对位置——这正是自定位的意义。具体打印值取决于你本地仓库路径，**待本地验证**。
5. **延伸**：试想如果有人把 `dir_list.mk` 复制到别处单独使用，第 1 行的 `$(lastword $(MAKEFILE_LIST))` 还会指向它自己吗？想清楚这点，你就理解了为什么它必须被 `include` 才能工作。

#### 4.1.5 小练习与答案

**练习 1**：`SOC_DIR` 这个变量在 dir_list.mk 里叫什么名字？指向哪个目录？

**答案**：变量名叫 `PICORV_DIR`，指向 `$(BEDROCK_DIR)soc/picorv32`（见 [dir_list.mk:18](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dir_list.mk#L18)）。这说明 Bedrock 的 SoC 目前只有 PicoRV32 一种软核，所以直接用软核名命名变量。

**练习 2**：为什么 dir_list.mk 用 `$(lastword $(MAKEFILE_LIST))` 而不是直接写 `dir_list.mk`？

**答案**：直接写文件名只能得到相对路径，且依赖当前工作目录；而 `$(lastword $(MAKEFILE_LIST))` 在被 `include` 时总是指向 dir_list.mk 自身的完整路径，配合 `$(abspath ...)` 就能得到稳健的绝对路径，让仓库可以放在磁盘任意位置。

---

### 4.2 子目录 Makefile 的标准骨架

#### 4.2.1 概念说明

Bedrock 有几十个子目录，每个都有自己的 `Makefile`。如果每个 Makefile 都从零写一遍「怎么调用 iverilog、怎么跑 vvp、怎么清理」，既重复又容易出错。Bedrock 的做法是**把公共逻辑抽到 `build-tools/` 里，让每个子目录的 Makefile 只写「自己独有的那点东西」**。

于是几乎所有子目录的 Makefile 都长一个样，称为「标准骨架」。以 `dsp/Makefile` 为例，它只有 7 行，却完成了「引入地址簿、声明自动生成文件、定义默认目标、引入公共规则、引入本地规则、引入收尾规则」全部工作。学会这 7 行的骨架，你就能读懂 Bedrock 里任何一个子目录的 Makefile。

#### 4.2.2 核心流程

标准骨架由「三段 include」构成，执行（读取）顺序如下：

```
子目录 Makefile 的读取顺序：
 ┌─────────────────────────────────────────────────────────────┐
 │ 1. include ../dir_list.mk        ← 拿到所有子系统的绝对路径变量 │
 │                                   （BUILD_DIR 等）              │
 │ 2. （本目录的局部变量声明，比如 VERILOG_AUTOGEN）              │
 │ 3. all: <默认目标>                ← 定义 make 不带参数时干什么   │
 │ 4. include $(BUILD_DIR)/top_rules.mk   ← 公共规则框架（工具名、 │
 │                                          %_tb/%_check 等模式规则）│
 │ 5. include rules.mk               ← 本目录独有的目标、测试台清单 │
 │ 6. include $(BUILD_DIR)/bottom_rules.mk ← clean 目标、清理校验  │
 └─────────────────────────────────────────────────────────────┘
```

三个 include 的分工可以记成一句话：**top_rules.mk 管「怎么做」（公共方法），rules.mk 管「做什么」（本目录的任务清单），bottom_rules.mk 管「怎么收拾」（清理）**。

注意第 4 步用 `$(BUILD_DIR)` 而不是写死 `../build-tools`——这正是 4.1 里 dir_list.mk 的价值：`BUILD_DIR` 来自地址簿，所以无论本目录在仓库里多深，都能正确找到 `build-tools/`。

#### 4.2.3 源码精读

先看 `dsp/Makefile`——最精简的标准骨架：

[dsp/Makefile:1-7](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/Makefile#L1-L7) —— 逐行解读：
- 第 1 行 `include ../dir_list.mk`：引入地址簿，从此 `$(BUILD_DIR)` 等变量可用。
- 第 2 行 `VERILOG_AUTOGEN += "cordicg_b22.v"`：声明 `cordicg_b22.v` 是**自动生成文件**（由 `cordicgx.py` 按数据位宽生成，见 u3-l1），不进 git。把它登记在这里，依赖推导时 make 就知道要先把它生成出来。
- 第 3-4 行：声明 `all` 是个伪目标（`.PHONY`），它依赖 `targets`。
- 第 5 行 `include $(BUILD_DIR)/top_rules.mk`：引入公共规则框架。
- 第 6 行 `include rules.mk`：引入本目录独有的规则（见下文 [dsp/rules.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rules.mk)）。
- 第 7 行 `include $(BUILD_DIR)/bottom_rules.mk`：引入收尾规则。

再看 `cordic/Makefile`——一个「扩展版」骨架，用来演示叶子子系统如何加自己的目标：

[cordic/Makefile:1-10](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/Makefile#L1-L10) —— 注意三处不同：
- 第 1-2 行先把 `BUILD_DIR`/`CORDIC_DIR` 本地设成 `.`，第 3 行注释解释：**当 cordic 被别的工程「树外（out-of-tree）」引用时**，`../dir_list.mk` 可能不存在，所以用 `-include`（前置减号表示「找不到也不报错」）；找到时它会把这两个变量覆盖成正确的绝对路径。
- 第 5 行照常引入 `top_rules.mk`。
- 第 7 行的 `all` 直接列出本目录要跑的具体检查目标，比 dsp 的 `all: targets` 更「手写」。
- 第 10 行 `include rules.mk` 同样引入本地规则。

最后看本地规则文件 `dsp/rules.mk` 里最关键的一行——本目录的「测试台清单」：

[dsp/rules.mk:8](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rules.mk#L8) —— `TEST_BENCH` 是一个很长的列表，**穷举了 dsp/ 下所有「有独立测试台」的模块名**（去掉 `_tb` 后缀）。比如 `rot_dds_tb`、`complex_mul_tb`、`half_filt_tb` 都在里面。这个清单随后被第 10、23 行用来生成 `targets`/`checks` 等聚合目标。**记住这张清单的作用**——它是 4.4 里判断「某模块有没有独立测试台」的权威依据。

补充：公共框架里定义了驱动仿真的一系列模式规则，这是整个 Bedrock 测试方法的「发动机」：

[build-tools/top_rules.mk:102-126](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L102-L126) —— 这段定义了 `%_tb`（编译测试台）、`%.vcd`（跑仿真生成波形）、`%_view`（用 GTKWave 看波形，依赖 `.vcd` 和 `.gtkw`）、`%_check`（跑测试台做自检）等核心模式规则。**这些规则之所以能跨所有子目录复用，前提正是骨架把 `top_rules.mk` 统一 include 进来**。本讲只需知道它们的存在与命名；具体每条规则如何驱动 iverilog/vvp，将在 u2-l1「基于 Make 的 HDL 仿真测试方法」中精读。

收尾规则：

[build-tools/bottom_rules.mk:1-13](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/bottom_rules.mk#L1-L13) —— 定义 `clean` 目标：删掉 `$(CLEAN)` 里登记的文件和 `$(CLEAN_DIRS)` 里的目录（如 `_dep`、`_autogen`、`_xilinx`、`__pycache__`），最后还跑一个 `check_clean` 校验源码是否干净（无隐藏文件、无尾随空格等）。注意 `clean` 用的是双冒号 `clean::`，允许其他文件再往上追加清理动作。

#### 4.2.4 代码实践

1. **目标**：用「骨架对照法」快速读懂一个陌生子目录的 Makefile。
2. **操作步骤**：
   - 打开 [dsp/Makefile](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/Makefile)，在三段 include 旁各写一句中文批注（top_rules=公共方法、rules=本地任务、bottom=清理）。
   - 再打开 [cordic/Makefile](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/Makefile)，圈出它与 dsp 骨架的不同之处（`-include`、`all` 写法、本地变量）。
3. **需要观察的现象**：两个 Makefile 的「骨架形状」一致（都包含 top_rules/rules/bottom 三件套），区别只在「本地扩展」部分。
4. **预期结果**：你能用一句话说出 dsp 的 `all` 最终会做什么——「`all: targets`，而 `targets` 依赖 `$(TEST_BENCH)`，即编译 [dsp/rules.mk:8](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rules.mk#L8) 列出的全部测试台」。
5. 这一连串依赖关系如何驱动真实仿真，**待本地验证**（建议在 u2-l1 学完模式规则后再回头实跑）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `dsp/Makefile` 用 `include ../dir_list.mk`，而 `cordic/Makefile` 用 `-include ../dir_list.mk`（前面带减号）？

**答案**：dsp 始终在 Bedrock 仓库内被构建，`../dir_list.mk` 一定存在，用普通 `include` 即可；cordic 是一个可被别的工程「树外」引用的叶子子系统，那种情况下 `../dir_list.mk` 可能不存在，`-include` 让 make 在找不到时不报错、转而使用本地第 1-2 行设的 `BUILD_DIR = .` 兜底（见 [cordic/Makefile:1-4](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/Makefile#L1-L4)）。

**练习 2**：如果一个新模块加了测试台 `foo_tb.v`，却忘了把 `foo_tb` 加进 [dsp/rules.mk:8](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rules.mk#L8) 的 `TEST_BENCH`，会发生什么？

**答案**：你仍然可以单独 `make foo_tb` / `make foo_check`（因为 [top_rules.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L102-L105) 的模式规则 `%_tb: %_tb.v` 对任何名字都成立），但 `make`（默认 `all` → `targets`）和 `make checks` 不会自动跑它——它会从批量测试里「漏网」。所以 `TEST_BENCH` 清单是「批量回归」的登记表，而非「能不能跑」的开关。

---

### 4.3 文件命名约定：从文件名读懂文件职责

#### 4.3.1 概念说明

Bedrock 是个有二十多年积累的库，文件数以千计。能快速认路，靠的是一套**稳定的文件名约定**：看到文件名后缀，就知道它属于哪一类、该用什么 make 目标去处理。这套约定不是某个工具强制规定的，而是社区（和 `top_rules.mk` 的模式规则）长期形成的默契。

掌握这套约定，你就能「看名识职责」，而不必打开每个文件去读内容。

#### 4.3.2 核心流程

下表是 Bedrock 最常见的文件名约定（命名速查表）：

| 文件名模式 | 职责 | 对应的 make 目标（来自模式规则） |
| --- | --- | --- |
| `<模块>.v` | 模块定义（可综合 RTL） | `make <模块>.v` 可单独语法编译 |
| `<模块>_tb.v` | 测试台（testbench，给模块喂激励） | `make <模块>_tb` 编译，`make <模块>_check` 跑自检 |
| `<模块>.gtkw` | GTKWave 波形配置（记下要看哪些信号） | `make <模块>_view` 打开波形（先要有 `<模块>.vcd`） |
| `<模块>.vh` | Verilog 头文件（宏、任务、参数） | 被 `` `include `` 引入，无独立 make 目标 |
| `Makefile` | 本目录构建入口 | `make` |
| `rules.mk` | 本目录独有规则（测试台清单、定制目标、清理项） | 被 `Makefile` include |
| `dir_list.mk` | 仓库根的地址簿 | 被各 `Makefile` include |
| `top_rules.mk` / `bottom_rules.mk` | 公共构建框架 | 被各 `Makefile` include |
| `<某物>.py` | 构建脚本 / 校验脚本 / 代码生成器 | 由 Make 调用，如 `$(PYTHON) cordic_check.py ...` |
| `*.bit` | FPGA 比特流（综合产物，不进 git） | `make <顶层>.bit` |
| `*.vcd` | 仿真波形数据（不进 git） | `make <模块>.vcd` |

另外有一类**目录**约定，它们都是构建产物、不进 git：

| 目录 | 含义 |
| --- | --- |
| `_xilinx/` | Xilinx 工具（ISE/Vivado）的综合输出目录 |
| `_autogen/` | 自动生成的 Verilog 实体 |
| `_dep/` | make 的自动依赖文件 |
| `__pycache__/` | Python 字节码缓存 |

这些带下划线前缀的目录都登记在各 `rules.mk` 的 `CLEAN_DIRS` 里，`make clean` 会删掉它们（见 [bottom_rules.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/bottom_rules.mk#L1-L13)）。

#### 4.3.3 源码精读

看一个「模块定义」样本——`mixer`（混频器）的开头：

[dsp/mixer.v:3-19](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v#L3-L19) —— 文件名 `mixer.v`，内部 `module mixer ...`，名字一致。这是 Bedrock 的命名基线：**文件名 = 模块名**。

再看一个「测试台」样本——`rot_dds` 的测试台开头：

[dsp/rot_dds_tb.v:1-19](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rot_dds_tb.v#L1-L19) —— 文件名 `rot_dds_tb.v`，内部 `module rot_dds_tb;`（注意测试台模块**没有端口列表**，以分号结尾）。第 10-13 行用 `$test$plusargs("vcd")` 判断是否要 dump 波形——这正解释了为什么 `make xxx.vcd` 能产生波形：它给 vvp 传 `+vcd` 参数，测试台看到后就调用 `$dumpfile/$dumpvars`。

最后看「波形配置」样本——`rot_dds.gtkw` 的开头：

[dsp/rot_dds.gtkw:1-11](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rot_dds.gtkw#L1-L11) —— 这是一个 GTKWave 保存文件，第 5 行 `[treeopen] rot_dds_tb.` 指明它配套的测试台是 `rot_dds_tb`，下面逐行列出要显示的信号（如 `rot_dds_tb.dut.phase_step_h[19:0]`）。`dut` 是 testbench 里给「被测器件（Device Under Test）」实例起的名字，这是 Bedrock 测试台的又一常见约定。

把三者并排看：`rot_dds.v`（定义）＋ `rot_dds_tb.v`（测试台）＋ `rot_dds.gtkw`（波形配置）构成一个**完整的「三件套」**。这正是 4.4 实践里用来对照的标杆。

#### 4.3.4 代码实践

1. **目标**：仅凭文件名，在 `dsp/` 里给一个模块配齐「三件套」。
2. **操作步骤**：以 `complex_mul`（复数乘法）为例，用 Glob 分别查找 `dsp/complex_mul.v`、`dsp/complex_mul_tb.v`、`dsp/complex_mul.gtkw` 是否都存在。
3. **需要观察的现象**：三个文件是否齐全；测试台文件里的 `module` 名是否叫 `complex_mul_tb`、波形文件里 `[treeopen]` 是否指向 `complex_mul_tb.`。
4. **预期结果**：三者齐全，且命名严格遵循 `<模块>` / `<模块>_tb` / `<模块>.gtkw` 约定——这就是 Bedrock 风格一致的体现。
5. 这种「看名识职责」的能力会在 4.4 的导航实践中直接派上用场。

#### 4.3.5 小练习与答案

**练习 1**：看到 `dsp/upconv.gtkw` 这个文件名，你能推断出什么？

**答案**：它是模块 `upconv` 的波形配置；同目录里大概率有 `upconv.v`（定义）和 `upconv_tb.v`（测试台）；想看波形就 `make upconv.vcd` 再 `make upconv_view`。

**练习 2**：`cordicg_b22.v` 这个文件名里的 `b22` 是什么意思？为什么它不进 git？

**答案**：`b22` 表示数据位宽（Data Path Width = 22），这个文件是由 `cordicgx.py` **按位宽生成**的（生成器逻辑见 [cordic/Makefile:17-19](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/Makefile#L17-L19) 的 `CORDIC_BASE_V = cordicg_b$(DPW).v`）。因为是生成物，所以不进 git，且被登记在 `dsp/Makefile` 的 `VERILOG_AUTOGEN` 里（见 4.2.3）。（生成机制细节将在 u3-l1 精讲。）

---

### 4.4 用 grep/glob 在大型 Verilog 库中定位模块

#### 4.4.1 概念说明

在一个有几千个 `.v` 文件的库里，你常会问三个问题：

1. **某模块定义在哪个文件？** —— 搜 `module <模块名>`。
2. **它被谁实例化了？** —— 搜 `<模块名>` 出现的所有位置（实例化语句会写出模块名）。
3. **它有没有独立测试台 / 波形配置？** —— 按命名约定找 `<模块>_tb.v` / `<模块>.gtkw`，并核对 `rules.mk` 的 `TEST_BENCH` 清单。

前两问用 `grep`（或 Grep 工具），第三问用 `glob`（按文件名模式查找）＋ 看 `rules.mk`。本节用一个**真实而有点意外的例子**把这套方法走一遍：定位 `mixer` 模块。

#### 4.4.2 核心流程

定位一个模块的「三步法」：

```
第 1 步：找定义
   Glob  dsp/<模块>*      →  看有没有 <模块>.v
   （或 Grep  "^module <模块名>"  找定义所在文件）

第 2 步：找测试台与波形（按命名约定）
   Glob  dsp/<模块>_tb.v
   Glob  dsp/<模块>.gtkw
   并在 dsp/rules.mk 的 TEST_BENCH 列表里搜 <模块>_tb

第 3 步：找实例化（理解它在系统里的位置）
   Grep  "<模块名>"  在 dsp/ 下的所有 .v 文件
   →  出现在别的模块里 = 被实例化；出现在 *_tb.v 里 = 被测试
```

#### 4.4.3 源码精读（也是本讲的主实践）

我们用 `mixer` 走一遍这三步，结论可能和你预期不一样。

**第 1 步——找定义：成功。**

用 Glob 查 `dsp/mixer*`，命中：

[dsp/mixer.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v) —— 这就是 `mixer` 的定义文件，内部 `module mixer`（端口见 [dsp/mixer.v:3-19](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v#L3-L19)）。文件名约定直接命中，无需打开内容。

**第 2 步——找测试台与波形：扑空。**

按约定找 `dsp/mixer_tb.v` 和 `dsp/mixer.gtkw`——**两个都不存在**（用 `find . -name mixer_tb.v`、`find . -name mixer.gtkw` 在全仓库都查无此文件）。再到权威清单里核对：

[dsp/rules.mk:8](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rules.mk#L8) —— `TEST_BENCH` 列表里**没有** `mixer_tb`。

这是一个重要结论：**「文件名约定」告诉你「该去哪找」，但并不保证每个模块都有独立测试台。** `mixer` 是一个被复用的叶子运算单元，Bedrock 没有给它单独写测试台。

**第 3 步——找实例化：解释了第 2 步的扑空。**

用 Grep 搜 `mixer` 在 `dsp/` 下 `.v` 文件里的出现，命中 `dsp/iq_mixer_multichannel.v` 等。也就是说，`mixer` 被**上层模块**（如 `iq_mixer_multichannel`）实例化，它的正确性是通过**测试那个上层模块**来间接覆盖的——而不是单测。

> 这正是大型库的常见模式：**底层运算单元靠组合进上层后被间接测试**。所以「找不到某模块的测试台」不等于「它没被测试」，你要顺着实例化链往上层找。

作为对照，`rot_dds` 就是个「完整三件套」模块：`rot_dds.v`（定义）＋ `rot_dds_tb.v`（测试台）＋ `rot_dds.gtkw`（波形）齐全，且 `rot_dds_tb` 在 [dsp/rules.mk:8](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rules.mk#L8) 的清单里。把 `mixer`（扑空）和 `rot_dds`（齐全）放一起，你就完整理解了 Bedrock 的命名约定与「有没有独立测试台」之间的关系。

#### 4.4.4 代码实践

这是本讲的主实践任务（**源码阅读 + 命令验证型**）：

1. **实践目标**：在 `dsp/` 下找到 `mixer` 模块的定义文件、测试台文件、波形配置文件各是哪一个；并说明你用什么方式（文件名约定还是搜索）定位到的。
2. **操作步骤**：
   - 用 Glob `dsp/mixer*` 找定义。
   - 用 `find . -name "mixer_tb.v"` 和 `find . -name "mixer.gtkw"` 找测试台与波形。
   - 打开 [dsp/rules.mk:8](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rules.mk#L8)，确认 `mixer_tb` 是否在 `TEST_BENCH` 列表里。
   - 用 Grep 搜 `mixer`，找出谁实例化了它。
   - 作为对照，对 `rot_dds` 重复以上步骤。
3. **需要观察的现象**：
   - `mixer`：定义存在；测试台与波形**不存在**；不在 `TEST_BENCH`；被 `iq_mixer_multichannel.v` 等实例化。
   - `rot_dds`：定义、测试台、波形**三者齐全**；在 `TEST_BENCH` 列表里。
4. **预期结果**（已通过 Glob/find/git 在本仓库核实，可直接采信）：
   - `mixer` 的定义是 [dsp/mixer.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v)；它**没有**专属测试台和波形配置，靠上层模块间接测试。
   - `rot_dds` 三件套齐全，是「有独立测试」的标杆例子。
5. **关于定位方式**：定义用「文件名约定」一步命中；测试台/波形的「不存在」则必须靠「按约定查找＋核对清单」才能确认——单看文件名约定会误以为它一定存在，这正是本实践要纠正的直觉。

> 说明：本实践不涉及编译或仿真，所有结论都基于文件是否存在以及源码内容，已核实。若你想进一步验证 `mixer` 确实被间接测试，可在 u2-l1 学完后尝试 `make -C dsp iq_chain4_check` 之类依赖 `mixer` 的上层目标，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：你想确认 `half_filt`（半带滤波器）有没有独立测试台，最快的方法是什么？

**答案**：两步——(a) 看 [dsp/rules.mk:8](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rules.mk#L8) 的 `TEST_BENCH` 里有没有 `half_filt_tb`（有）；(b) 用 Glob 确认 `dsp/half_filt_tb.v` 和 `dsp/half_filt.gtkw` 存在。两者都成立，说明它有完整三件套。

**练习 2**：如果 Grep 显示某模块名**只**出现在它自己的 `xxx.v` 和 `xxx_tb.v` 里，没有任何别的文件实例化它，说明什么？

**答案**：说明它目前是个「顶层/孤立的被测模块」，没有被其他 RTL 实例化复用——它的唯一用途就是被自己的测试台验证。这在库的「对外入口模块」里常见；而像 `mixer` 这种只出现在别处实例化列表、自己没有测试台的，则是「纯叶子运算单元」。两者都是库的正常组成部分。

## 5. 综合实践

把本讲四个模块串起来，完成一次「新手上路式的目录勘探」：

**任务**：假设你要给 Bedrock 的 `dsp/` 子目录写一份「目录说明书」，请收集以下信息并整理成一张表/一张图：

1. **地址簿**：从 [dir_list.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dir_list.mk) 抄出 `DSP_DIR`、`CORDIC_DIR`、`BUILD_DIR` 三个变量，说明它们各自指向哪个子目录、`dsp/` 会用到哪几个。
2. **骨架**：画出 [dsp/Makefile](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/Makefile) 的「三段 include」结构图，标注每段引入的文件来自「地址簿 / 公共框架 / 本地规则 / 收尾」中的哪一类。
3. **命名约定抽样**：从 [dsp/rules.mk:8](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rules.mk#L8) 的 `TEST_BENCH` 里任选 3 个模块，用 Glob 核实它们是否都有 `<模块>.v` / `<模块>_tb.v` / `<模块>.gtkw` 三件套。
4. **导航应用**：选一个**不在** `TEST_BENCH` 列表里的 dsp 模块（例如 `mixer`），用 Grep 找出谁实例化了它，解释它如何被间接测试。

**产出**：一张「dsp 子系统导航卡」，含上述四项。这张卡也是你日后阅读任何 Bedrock 子目录的通用模板——换个子目录名，同样的四步照样适用。

## 6. 本讲小结

- `dir_list.mk` 是仓库的「地址簿」：用 `$(MAKEFILE_LIST)` 自定位仓库根，再为 14 个子系统各定义一个绝对路径变量，让任何子目录都能稳健地引用别的子系统。
- 子目录 Makefile 有统一「标准骨架」：`include ../dir_list.mk` → 声明本地变量/默认目标 → `include top_rules.mk`（公共方法）→ `include rules.mk`（本地任务）→ `include bottom_rules.mk`（清理）。`cordic/Makefile` 演示了叶子子系统如何用 `-include` 和本地变量扩展这个骨架。
- 文件名约定是认路的关键：`<模块>.v` 是定义、`<模块>_tb.v` 是测试台、`<模块>.gtkw` 是波形配置；`rules.mk` 是本地规则、`*_tb`/`%_check`/`%.vcd`/`%_view` 是对应的 make 模式目标；`_xilinx/`、`_autogen/`、`_dep/` 是不进 git 的构建产物目录。
- 定位模块的「三步法」：Glob/Grep 找定义 → 按约定找测试台/波形并核对 `rules.mk` 的 `TEST_BENCH` 清单 → Grep 找实例化。
- 重要反直觉点：**不是每个模块都有独立测试台**。`mixer` 只有定义、被上层模块（如 `iq_mixer_multichannel.v`）间接测试；`rot_dds` 才是「三件套齐全」的标杆。命名约定告诉你「该去哪找」，但不保证「一定存在」。

## 7. 下一步学习建议

- **下一讲 u1-l4（RTL 编码规范）**：本讲教你「文件叫什么、放哪」，u1-l4 教你「文件里面的信号/参数该怎么命名」，两者合起来就是 Bedrock 的代码风格全貌。建议接着读 [guidelines/rtl_guidelines.md](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/guidelines/rtl_guidelines.md)。
- **进入单元 2**：当你想真正「跑」本讲提到的 `%_tb`/`%_check`/`%.vcd` 等目标时，去读 u2-l1「基于 Make 的 HDL 仿真测试方法」，它会逐条拆解 [build-tools/top_rules.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk) 里的模式规则和 iverilog/vvp 调用细节。
- **延伸阅读**：[build-tools/makefile.md](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/makefile.md) 是 Bedrock 自带的 Make 构建系统教程，可作为 u2-l1 的预习材料。
