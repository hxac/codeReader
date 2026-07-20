# 仿真脚本与 CI 流程深入

## 1. 本讲目标

本讲是「进阶：CI 深入」单元的第一篇，承接 u1-l3（仿真环境与 CI 构建流程）打下的全局认知，把镜头推近到 **脚本本身的内部结构与扩展点**。学完后你应当能够：

- 说清 `run.tcl` / `runGhdl.tcl` / `interactive.tcl` 三个脚本的**分工与差异**，并能解释为什么「切换 ModelSim↔GHDL 只改一个词」。
- 看懂 `config.tcl` 作为「内容文件」的六段结构，并能**亲手为一个新增 testbench 完成注册**。
- 理解 `compile_suppress` / `run_suppress` 的消息过滤机制，能权衡「降噪」与「漏报」的取舍。
- 复述 `ciFlow.py` 三态退出码（0 / 254 / 255）的判定逻辑，并知道它们各自捕获哪一类故障。
- 理清 `dependencies.py` 为何只是 `PsiFpgaLibDependencies` 的薄壳，以及它如何把 `README.md` 当作依赖的唯一事实来源。

## 2. 前置知识

本讲默认你已读过 u1-l3，已经知道下面这条主线（这里只做一句话回顾，不重复细节）：

> `ci.do`（`onerror {exit}` + `source run.tcl` + `quit`）包住 `run.tcl`；`run.tcl` 的流水线是 `init → source config.tcl → compile_files -all -clean → run_tb -all → run_check_errors "###ERROR###"`；`###ERROR###` 是全库统一的错误前缀；CI 末尾双重检查「无 `###ERROR###`」且「有成功标记」。

如果你对其中任何一处没有把握，请先回看 u1-l3。本讲不再重复「流水线是什么」，而是回答四个更深层的问题：**两条仿真路径如何切换、消息怎么过滤、CI 如何给故障分类、依赖从哪里来**。

还需要一点背景：PsiSim 是一个 **外部 TCL 框架**（仓库 [PsiSim](https://github.com/paulscherrerinstitute/PsiSim)），它提供了 `init`、`compile_files`、`run_tb`、`run_check_errors`、`add_library`、`add_sources`、`compile_suppress`、`run_suppress`、`create_tb_run`、`add_tb_run` 等命令。本仓库只 **调用** 这些命令，不实现它们；因此本讲到这些命令时，只描述它们的可观察行为（从本仓库脚本里能看到的用法），其内部实现属于 PsiSim，不在本仓库内。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [sim/run.tcl](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/run.tcl) | ModelSim 批处理流水线 | 流水线七步、与 GHDL 的唯一差异 |
| [sim/runGhdl.tcl](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/runGhdl.tcl) | GHDL 批处理流水线 | `init -ghdl` 一个词的切换 |
| [sim/interactive.tcl](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/interactive.tcl) | GUI 调试入口 | 只编译、不运行 |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl) | 内容文件（编译/运行什么） | 六段结构、`add_sources` 三 tag、消息抑制 |
| [sim/ci.do](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/ci.do) | CI 入口包装 | `onerror {exit}` 的兜底作用 |
| [scripts/ciFlow.py](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/scripts/ciFlow.py) | CI 判定器 | Transcript 三态检查与退出码 |
| [scripts/dependencies.py](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/scripts/dependencies.py) | 依赖管理薄壳 | 从 README 解析依赖 |
| [README.md](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/README.md) | 人类可读说明 | `Dependencies` 段是依赖事实来源 |

先记住一个目录约定（由脚本里的相对路径反推得出）。所有 `.tcl` 都在 `sim/` 目录下运行，相对路径以此为基准：

```
<root>/                         # 例：psi_fpga_all 检出根（README 指明的约定结构）
├── TCL/
│   └── PsiSim/
│       └── PsiSim.tcl          # run.tcl 第 8 行:  ../../../TCL/PsiSim/PsiSim.tcl
└── VHDL/                       # config.tcl 的 LibPath("../..") 指到这里
    ├── psi_common/             #   下属 hdl/psi_common_*.vhd（tag=lib）
    └── psi_tb/                 #   本仓库
        ├── hdl/                #   "../hdl"  （tag=src）
        ├── testbench/          #   "../testbench" （tag=tb）
        └── sim/                #   所有脚本的工作目录
```

这个布局解释了三件事：PsiSim 在 `sim/` **上三级**（`../../../TCL`）；`psi_common` 是 `psi_tb` 的**同级**目录（`../../`）；`hdl` 与 `testbench` 在 `psi_tb` **内部**（`../hdl`、`../testbench`）。

---

## 4. 核心概念与源码讲解

### 4.1 PsiSim 框架与 `init` / `init -ghdl`：两条仿真路径的分工

#### 4.1.1 概念说明

PsiSim 是一套 TCL 封装层，目的是把「编译 VHDL → 跑 testbench → 查错误」这条流水线抽象成与后端无关的命令。它支持两种仿真后端：

- **ModelSim / Questa**（商业仿真器）：功能最全、波形调试体验最好，是 PSI 的主力。
- **GHDL**（开源仿真器）：免费、轻量，常用于 CI 或无 ModelSim license 的环境。

psi_tb 不关心后端差异，只在初始化时用**一个词**告诉 PsiSim 用哪个后端，后续所有命令（`compile_files`、`run_tb`、`run_check_errors`）都是后端无关的。这就是「切换仿真器只改一个词」的设计根因。

#### 4.1.2 核心流程

`run.tcl` 的七步流水线：

```
1. source <PsiSim.tcl>      # 载入框架，定义 psi::sim::* 命令
2. namespace import psi::sim::*   # 把命令导入当前命名空间
3. init  /  init -ghdl      # 选择后端（本模块的焦点）
4. source ./config.tcl      # 注入「编译什么、跑什么」的内容
5. compile_files -all -clean # 编译全部源（-clean 先清空）
6. run_tb -all              # 运行全部已注册 testbench
7. run_check_errors "###ERROR###"  # 扫描 Transcript，遇错误串则报失败
```

`runGhdl.tcl` 与 `run.tcl` **逐行相同，唯一差别是第 3 步**；`interactive.tcl` 是 `run.tcl` 的**前缀**——只走到第 5 步（`compile_files`），不做第 6、7 步。

#### 4.1.3 源码精读

`run.tcl` 与 `runGhdl.tcl` 的差异只有一行：

- [sim/run.tcl:14-14](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/run.tcl#L14-L14)：`init` —— 选择 ModelSim 后端。
- [sim/runGhdl.tcl:14-14](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/runGhdl.tcl#L14-L14)：`init -ghdl` —— 选择 GHDL 后端。

> 用 `diff sim/run.tcl sim/runGhdl.tcl` 验证，输出只有 `14c14 < init --- > init -ghdl`，确实只差一个词。

两者随后做的事完全一致：载入内容（[sim/run.tcl:17-17](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/run.tcl#L17-L17) 的 `source ./config.tcl`）、编译（[:23-23](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/run.tcl#L23-L23)）、运行（[:27-27](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/run.tcl#L27-L27)）、查错（[:32-32](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/run.tcl#L32-L32)）。这说明 **`config.tcl` 是后端无关的内容描述**，两个入口都能复用它。

`interactive.tcl` 故意「半截」：

- [sim/interactive.tcl:19-19](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/interactive.tcl#L19-L19)：`compile_files -all -clean` 之后就结束了，**没有 `run_tb`，也没有 `run_check_errors`**。

它的用途见文件头注释（[sim/interactive.tcl:7-8](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/interactive.tcl#L7-L8)）：在 ModelSim GUI 里手工调试时，先编译好，然后由你在波形窗口里手动驱动、观察。批处理脚本（`run.tcl`）追求「一键到底」，交互脚本（`interactive.tcl`）追求「编译完就停，把控制权交还给人」——这是两种工作模式的刻意分叉。

#### 4.1.4 代码实践

**实践目标**：用 `diff` 确认三个脚本的差异，建立「它们共享骨架」的直觉。

**操作步骤**（源码阅读型，无需安装仿真器）：

1. 在仓库根目录运行 `diff sim/run.tcl sim/runGhdl.tcl`，确认输出只有第 14 行。
2. 运行 `diff sim/run.tcl sim/interactive.tcl`，观察 interactive 版本相对于 run 版本**缺少了哪些行**（应缺少 `run_tb`、`run_check_errors` 及若干 `puts` 分节）。
3. 在本讲笔记里画一张表：七步流水线中，`run.tcl` / `runGhdl.tcl` / `interactive.tcl` 各自执行到第几步。

**需要观察的现象**：
- `run.tcl` 与 `runGhdl.tcl` 的 diff 只有 1 行。
- `interactive.tcl` 的 diff 集中在文件**末尾**（被截断的部分），而不是开头——说明它复用了相同的初始化与编译逻辑。

**预期结果**：你应当能用一句话总结：「三个脚本共用 PsiSim 的编译骨架，区别只在后端选择（`init` vs `init -ghdl`）和是否走到运行/查错两步。」

#### 4.1.5 小练习与答案

**练习 1**：如果想在 CI 里把后端从 ModelSim 换成 GHDL，需要改 `config.tcl` 吗？
**答案**：不需要。`config.tcl` 是后端无关的内容文件；只需让 CI 调用 `runGhdl.tcl`（`init -ghdl`）而非 `run.tcl`（`init`）即可。

**练习 2**：为什么 `interactive.tcl` 不调用 `run_check_errors`？
**答案**：交互调试时由人观察波形与日志，不需要脚本自动判定通过/失败；而且人会手动 `run` 多次、反复重启仿真，自动查错反而碍事。

---

### 4.2 `config.tcl` 的内容管理：库、源码、TB 注册与消息抑制

#### 4.2.1 概念说明

u1-l3 已点明「`config.tcl` 管内容，`run.tcl` 管流程」。本模块把「内容」拆开看——它由六段组成，每段回答一个问题：

| 段 | 回答的问题 | 关键命令 |
| --- | --- | --- |
| 常量 | 同级库在哪里？ | `set LibPath` |
| 库名 | 编译进哪个 VHDL library？ | `add_library` |
| 编译期抑制 | 编译时屏蔽哪些告警？ | `compile_suppress` |
| 运行期抑制 | 运行时屏蔽哪些告警？ | `run_suppress` |
| 源码（三 tag） | 编译哪些文件、按什么顺序？ | `add_sources -tag lib/src/tb` |
| TB 注册 | 跑哪些 testbench？ | `create_tb_run` + `add_tb_run` |

理解这六段后，你就能自如地**新增一个 testbench**——这是本讲综合实践的核心。

#### 4.2.2 核心流程

**编译顺序 = tag 顺序**。VHDL 要求「被引用的单元先编译」，因此 tag 必须按依赖拓扑排列：

```
tag=lib (psi_common)  →  tag=src (psi_tb 包)  →  tag=tb (testbench)
     ↑ 被引用                    ↑ 引用 lib            ↑ 引用 lib 与 src
```

例如 `psi_tb_compare_pkg`（src）会 `use work.psi_common_logic_pkg`（lib），而 `psi_tb_i2c_pkg_tb`（tb）会 `use work.psi_tb_i2c_pkg`（src）——所以 `lib` 必须在 `src` 之前，`src` 必须在 `tb` 之前。PsiSim 按 tag 出现顺序依次编译，于是拓扑自动满足。

**消息抑制**分两层：编译期（`vcom`）与运行期（`vsim`），各自屏蔽一组 ModelSim 消息编号。

#### 4.2.3 源码精读

**库名**：[sim/config.tcl:14-14](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L14-L14) `add_library psi_tb` 把所有源编译进名为 `psi_tb` 的 VHDL library。这正是源码里 `use work.psi_tb_i2c_pkg;` 之类引用能成立的根因（这里 `work` 即 `psi_tb` 库）。

**消息抑制**：

- [sim/config.tcl:17-17](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L17-L17)：`compile_suppress 135,1236,1370` —— 编译期屏蔽这三个编号。
- [sim/config.tcl:18-18](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L18-L18)：`run_suppress 8684,3479,3813,8009,3812` —— 运行期屏蔽这五个编号。

这些编号是 ModelSim（`vcom`/`vsim`）给特定告警/提示分配的标识（即日志里 `** Warning: (vcom-1236) ...` 这类前缀中的数字）。PsiSim 会把它们透传给后端的 `-suppress` 选项。各编号的**精确文案需对照本地 ModelSim 消息手册确认（待本地验证）**；从 psi_tb 是 testbench 库的语境推断，它们多半对应「testbench 里合法但 RTL 中会告警」的写法——例如 `--synopsys translate off` 包裹的不可综合结构、信号初始值、数值范围提示等。屏蔽它们是为了让 Transcript 聚焦在真正重要的信息上。

> **取舍提醒**：消息抑制是双刃剑。屏蔽过激会**漏掉真实问题**（例如把某个真实的多驱动告警也归并掉了）；屏蔽太少则日志被噪声淹没、`###ERROR###` 反而难找。本仓库的选择偏「适度降噪」，新增 testbench 时一般无需改动这两行，除非你的 TB 触发了大量无害告警。

**源码三段**（注意 `add_sources` 的第一个参数是**目录**，第二个是文件列表）：

- tag=lib（[sim/config.tcl:21-25](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L21-L25)）：目录 `$LibPath`（=`VHDL/`），三个 `psi_common` 包。`LibPath` 由 [sim/config.tcl:8-8](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L8-L8) 定义为 `"../.."`。
- tag=src（[sim/config.tcl:28-33](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L28-L33)）：目录 `"../hdl"`，四个 psi_tb 包（`txt_util`、`compare`、`activity`、`i2c`）。
- tag=tb（[sim/config.tcl:36-38](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L36-L38)）：目录 `"../testbench"`，目前只有 `psi_tb_i2c_pkg_tb.vhd`。

> 注意：u1-l2 已指出，`src` 段目前只编译 4 个包——AXI（`psi_tb_axi_pkg`、`psi_tb_axi_conv_pkg`）与文本（`psi_tb_textfile_pkg`）**没有进 CI 编译清单**，因为它们没有注册的 testbench。这是「CI 只编译有 TB 覆盖的代码」的直接证据。

**TB 注册**：

- [sim/config.tcl:41-41](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L41-L41)：`create_tb_run "psi_tb_i2c_pkg_tb"` 创建一个运行条目，顶层实体名是 `psi_tb_i2c_pkg_tb`。
- [sim/config.tcl:42-42](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L42-L42)：`add_tb_run` 把这个条目加入待运行队列（`run_tb -all` 就会跑它）。

#### 4.2.4 代码实践

**实践目标**：在 `config.tcl` 里新增一个 testbench 条目（不实际创建 TB 文件，只练注册语法）。

**操作步骤**（源码改写型，建议在副本上练习，不要改坏原 `config.tcl`）：

1. 复制 `sim/config.tcl` 到一个临时位置，比如 `sim/config_mine.tcl`。
2. 在 tag=tb 段，给 `add_sources` 的文件列表追加一行假想 TB：
   ```tcl
   add_sources "../testbench" {
       psi_tb_i2c_pkg_tb.vhd \
       my_demo_tb.vhd \
   } -tag tb
   ```
3. 在 TB Runs 段追加一对调用，注册新 TB：
   ```tcl
   create_tb_run "my_demo_tb"
   add_tb_run
   ```
4. 逐行注释每个新增点：哪一行让源码进入编译清单？哪一行让它进入运行队列？

**需要观察的现象**：
- 第 2 步只解决了「**编译**这个文件」；第 3 步才解决「**运行**这个顶层」。
- 两步缺一不可：只 `add_sources` 不 `create_tb_run`，文件被编译但不会跑；只 `create_tb_run` 不 `add_sources`，运行时找不到实体。

**预期结果**：你能口述「新增一个 testbench 需要在 `config.tcl` 改两处：`add_sources` 的 tb 列表 + 一对 `create_tb_run`/`add_tb_run`」。

> 本地真正跑通需要先写好 `my_demo_tb.vhd` 并安装 ModelSim 或 GHDL，这部分留到本讲综合实践。

#### 4.2.5 小练习与答案

**练习 1**：为什么 tag 顺序必须是 `lib → src → tb`？
**答案**：VHDL 要求被引用的库单元先编译。`src` 里的 psi_tb 包 `use` 了 `lib` 里的 `psi_common`，`tb` 又 `use` 了 `src` 里的 psi_tb 包；PsiSim 按 tag 顺序编译，所以必须 `lib → src → tb`。

**练习 2**：如果某个 testbench 编译通过、却没出现在运行结果里，最可能漏了哪一步？
**答案**：漏了 `create_tb_run "<顶层名>"` + `add_tb_run`。`add_sources` 只负责编译，运行队列由 `add_tb_run` 维护。

**练习 3**：`compile_suppress` 与 `run_suppress` 各自作用于流水线的哪一步？
**答案**：`compile_suppress` 作用于第 5 步 `compile_files`（即 `vcom` 阶段）；`run_suppress` 作用于第 6 步 `run_tb`（即 `vsim` 阶段）。

---

### 4.3 `ciFlow.py` 的 Transcript 检查与退出码语义

#### 4.3.1 概念说明

CI 需要一个**机器可判定的通过/失败信号**。psi_tb 的方案是：让仿真器把全部输出写进一个日志文件 `Transcript.transcript`，然后用一段极简的 Python 独立扫描这个文件，按内容给出三种退出码。这套逻辑的核心是**用「两个独立的子串检查」把故障分成两类**：

- **应用错误**：testbench 自己跑完了，但自检发现数值/协议不对 → 打印了 `###ERROR###`。这是 u3（compare）、u4（activity）、u5/u7（BFM）里那些 `assert ... severity error` 的产物。
- **框架错误**：编译失败、仿真崩溃、脚本异常退出 → testbench 根本没机会打印成功标记。

把这两类分开，CI 就能一眼看出「是我的设计错了」还是「环境/脚本坏了」。

#### 4.3.2 核心流程

完整链路：

```
ciFlow.py
  ├─ chdir 到 sim/
  ├─ os.system("vsim -batch -do ci.do -logfile Transcript.transcript")
  │        └─ ci.do:  onerror {exit}  →  source run.tcl  →  quit
  │                       └─ run.tcl:  init → config.tcl → compile → run_tb → run_check_errors
  ├─ 读 Transcript.transcript 全文
  └─ 两步检查 → 退出码：
       含 "###ERROR###"                        → exit(-1)   # shell 255，应用错误
       不含 "SIMULATIONS COMPLETED SUCCESSFULLY" → exit(-2)   # shell 254，框架错误
       否则                                     → exit(0)    # 通过
```

退出码在 Unix shell 里会被取模：

\[
\text{shell\_code} = \text{py\_code} \bmod 256
\]

故 \( -1 \bmod 256 = 255 \)、\( -2 \bmod 256 = 254 \)。两者都非 0，CI 系统据此判失败；但 255 与 254 的区别能告诉你是哪一类故障。

#### 4.3.3 源码精读

**ci.do 的兜底**（[sim/ci.do:7-9](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/ci.do#L7-L9)）：

```tcl
onerror {exit}     ;# 任何 TCL 错误都立即退出 vsim，避免卡死
source run.tcl     ;# 跑完整流水线
quit               ;# 退出仿真器
```

`onerror {exit}` 是关键：如果 `run.tcl` 里某条命令抛错（例如编译失败），vsim 不会停在交互提示符上等输入，而是立刻退出。这保证了 CI 不会因为一个错误而无限挂起。

**ciFlow.py 的启动**（[scripts/ciFlow.py:10-12](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/scripts/ciFlow.py#L10-L12)）：先把工作目录切到 `sim/`（这样 `ci.do` 里的 `source run.tcl` 才能找到文件），再以 batch 模式启动 vsim，把输出重定向到 `Transcript.transcript`。

**两步检查**（[scripts/ciFlow.py:18-26](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/scripts/ciFlow.py#L18-L26)）：

```python
if "###ERROR###" in content:                    # 应用错误
    exit(-1)
if "SIMULATIONS COMPLETED SUCCESSFULLY" not in content:  # 框架错误
    exit(-2)
exit(0)                                          # 通过
```

注意检查的**顺序**：先查 `###ERROR###`。这隐含一个约定——**成功标记由 PsiSim 的 `run_tb -all` 在所有 TB 都跑完后打印**，而 testbench 自检失败的 `###ERROR###` 是在跑的过程中打印的。所以一份既含 `###ERROR###` 又含成功标记的日志，按此顺序会被判为 **255（应用错误）**，这是合理的：自检失败优先于「跑完了」。

> 这段逻辑也解释了 u1-l3 提到的「双重检查构成纵深防御」：`###ERROR###` 抓 testbench 自检失败，缺失成功标记抓编译/崩溃等框架级异常。两者一起，才能把「仿真真的全绿」这件事确认下来。

#### 4.3.4 代码实践

**实践目标**：脱离仿真器，单独验证 `ciFlow.py` 的三态判定逻辑——这是本讲中**唯一无需安装任何仿真器即可运行**的实践。

**操作步骤**：

1. 在仓库根目录新建一个临时目录（不要写进 `psi_tb-tutorial/` 之外的项目目录，建议放 `/tmp`）：
   ```bash
   mkdir -p /tmp/cisim && cd /tmp/cisim
   ```
2. 造三份假 Transcript，分别模拟「通过」「应用错误」「框架错误」：
   ```bash
   printf '... lots of sim output ...\nSIMULATIONS COMPLETED SUCCESSFULLY\n' > ok.transcript
   printf '###ERROR###: IntCompare mismatch\nSIMULATIONS COMPLETED SUCCESSFULLY\n' > apperr.transcript
   printf '** Error: failure in compile_files\n' >框架err.transcript 2>/dev/null || printf '** Error: failure in compile_files\n' > fwerr.transcript
   ```
3. 对每份日志跑下面这段「抽离自 `ciFlow.py`」的判定脚本：
   ```python
   # judge.py —— 与 ciFlow.py 第 18-26 行等价的纯字符串检查
   import sys
   content = open(sys.argv[1]).read()
   if "###ERROR###" in content:
       print("应用错误 -> exit(-1), shell 255")
   elif "SIMULATIONS COMPLETED SUCCESSFULLY" not in content:
       print("框架错误 -> exit(-2), shell 254")
   else:
       print("通过 -> exit(0)")
   ```
   依次：`python3 judge.py ok.transcript`、`python3 judge.py apperr.transcript`、`python3 judge.py fwerr.transcript`。

**需要观察的现象**：
- `ok.transcript` →「通过」。
- `apperr.transcript`（含 `###ERROR###` **且**含成功标记）→「应用错误」，因为第一个 `if` 先命中。
- `fwerr.transcript`（既无 `###ERROR###` 也无成功标记）→「框架错误」。

**预期结果**：你亲眼看到检查顺序的意义——「应用错误」优先级高于「跑完了」。这正对应真实 CI 里「哪怕 TB 跑到最后，只要中途打印过 `###ERROR###`，就是红」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ciFlow.py` 不直接用 vsim 的退出码，而要读 `Transcript.transcript`？
**答案**：vsim 的进程退出码主要反映「vsim 这个程序有没有正常退出」，而 testbench 的自检失败（`severity error` 的 assert）**不会**让 vsim 进程返回非零——它只是往 Transcript 打印 `###ERROR###`。所以必须扫日志才能抓住应用错误。

**练习 2**：`onerror {exit}` 如果删掉，CI 会出什么问题？
**答案**：编译失败等 TCL 错误会把 vsim 留在交互提示符上等待输入，而 batch 模式没有输入 → 仿真器挂死，CI 超时。`onerror {exit}` 保证出错即退。

**练习 3**：一份日志里同时有 `###ERROR###` 和成功标记，`ciFlow.py` 给出哪个退出码？为什么这样设计合理？
**答案**：255（应用错误）。因为「自检失败」比「仿真跑到了最后」更重要——TB 跑完不代表结果对。

---

### 4.4 `dependencies.py` 与 README 依赖解析

#### 4.4.1 概念说明

psi_tb 不孤立存在——它依赖 PsiSim（TCL 框架）和 psi_common（VHDL 库）。要在全新机器上搭建这套环境，需要把这两个依赖按正确版本拉下来、放到第 3 节那张目录树里。这件事由 [scripts/dependencies.py](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/scripts/dependencies.py) 触发。

但本仓库**几乎不实现**依赖管理逻辑——它只是 `PsiFpgaLibDependencies`（一个跨所有 PSI FPGA 仓库共享的外部 Python 包）的**薄壳**。本仓库的唯一贡献是：「我的依赖写在 `README.md` 里，去那里读。」

这样做的好处是 **单一事实来源**：人类在 README 里看到的依赖列表，和机器解析出来的依赖列表，是同一份文本，永远不会漂移。

#### 4.4.2 核心流程

```
dependencies.py
  ├─ from PsiFpgaLibDependencies import *        # 外部共享框架
  ├─ Parse.FromReadme("../README.md")            # 解析 README 的 Dependencies 段
  │      └─ 提取: TCL/PsiSim (>=2.2.0), VHDL/psi_common (>=3.0.0), psi_tb
  └─ Actions.ExecMain(repo, dependencies)        # 执行拉取/链接
```

`PsiFpgaLibDependencies` 的内部实现不在本仓库（它是 psi_fpga_all 工具链的一部分），所以我们只描述它的**可观察契约**：给它一个 README 路径，它返回结构化的依赖列表；给它仓库路径和依赖列表，它把依赖准备就位。

#### 4.4.3 源码精读

`dependencies.py` 全文只有四行有效代码：

- [scripts/dependencies.py:1-1](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/scripts/dependencies.py#L1-L1)：`from PsiFpgaLibDependencies import *` —— 引入外部框架，拿到 `Parse`、`Actions` 两个对象。
- [scripts/dependencies.py:7-7](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/scripts/dependencies.py#L7-L7)：`Parse.FromReadme(THIS_DIR + "/../README.md")` —— 以 `README.md` 为输入解析依赖。`THIS_DIR` 由上一行 [scripts/dependencies.py:5-5](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/scripts/dependencies.py#L5-L5) 定位到 `scripts/`，所以 `../README.md` 指向仓库根的 README。
- [scripts/dependencies.py:9-9](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/scripts/dependencies.py#L9-L9)：`repo = os.path.abspath(THIS_DIR + "/..")` —— 仓库根目录的绝对路径。
- [scripts/dependencies.py:11-11](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/scripts/dependencies.py#L11-L11)：`Actions.ExecMain(repo, dependencies)` —— 用解析出的依赖列表执行主流程（拉取/检出/链接，细节在外部框架）。

它读取的 README 段（[README.md:33-42](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/README.md#L33-L42)）结构清晰、便于机器解析：

```
# Dependencies
...
* TCL
  * PsiSim (2.2.0 or higher)
* VHDL
  * psi_common (3.0.0 or higher)
  * psi_tb
```

`Parse.FromReadme` 消费的就是这种「`# Dependencies` 标题 + TCL/VHDL 分组 + Markdown 链接 + 括号内版本」的约定格式。正因为它依赖 README 的固定写法，**改 README 的 Dependencies 段格式时要格外小心**——格式漂移会让解析失败。

> **待确认**：`PsiFpgaLibDependencies` 具体如何匹配标题、提取版本号、处理「no version」条目（如 `psi_tb` 本身没标版本），需查看该外部包源码；本仓库不包含其实现。

#### 4.4.4 代码实践

**实践目标**：手工模拟 `Parse.FromReadme` 的产出，确认 README 的确是依赖的事实来源。

**操作步骤**（源码阅读型）：

1. 打开 [README.md:33-42](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/README.md#L33-L42)。
2. 用一张表写下你期望 `Parse.FromReadme` 解析出的依赖：
   | 类别 | 名称 | 版本要求 |
   | --- | --- | --- |
   | TCL | PsiSim | ≥ 2.2.0 |
   | VHDL | psi_common | ≥ 3.0.0 |
   | VHDL | psi_tb | （自身，无版本） |
3. 对照 `config.tcl` tag=lib 段（[sim/config.tcl:21-25](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L21-L25)）：那里实际编译了 `psi_common_array_pkg`、`psi_common_math_pkg`、`psi_common_logic_pkg`。确认这与 README 声明的 `psi_common` 依赖一致——README 说「我需要 psi_common」，`config.tcl` 说「我具体用 psi_common 的这三个包」，两者指向同一个依赖。

**需要观察的现象**：
- README 的依赖声明（高层）与 `config.tcl` 的 `add_sources`（落地）是**同一依赖的两个抽象层级**。
- 把版本号写在 README 而不是某个 `requirements.txt`，意味着改依赖必须改人类文档——这正是「单一事实来源」的代价与收益。

**预期结果**：你能解释「为什么改 `config.tcl` 引入新的 psi_common 包不需要改 README（仍是 psi_common 这一个依赖），但把 psi_common 升级到不兼容版本时，README 的版本号与 Changelog 都要同步更新」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `dependencies.py` 把解析逻辑委托给外部的 `PsiFpgaLibDependencies`，而不是自己写？
**答案**： PSI 有几十个 FPGA 仓库，每个都需要同样的「从 README 解析依赖 → 准备就位」流程。把这套逻辑放进共享的 `PsiFpgaLibDependencies`，每个仓库只需写 4 行薄壳，避免重复实现与各仓库不一致。

**练习 2**：如果有人把 README 的 `# Dependencies` 标题改成 `# Prerequisites`，会发生什么？
**答案**：`Parse.FromReadme` 很可能找不到依赖段、返回空列表，导致 `Actions.ExecMain` 不拉取任何依赖、搭建出来的环境缺 psi_common 与 PsiSim，仿真脚本会因 `source ../../../TCL/PsiSim/PsiSim.tcl` 找不到文件而失败。这正是「README 格式是机器契约」的体现。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来——新增一个 testbench，注册进 CI，并理解它在两条仿真路径与 CI 判定中的完整流转。这是 u1-l3 提出的「跑通一个 TB」任务的进阶版，多了「自己注册 + 双后端对比 + 故障分类」三个维度。

**操作步骤**：

1. **写一个最小 TB**（参考 u7-l4 的 I2C TB 风格，但这里只需一个能打印成功标记的空壳）。在 `testbench/` 下新建 `my_demo_tb.vhd`（示例代码，非项目原有文件）：
   ```vhdl
   -- 示例代码：最小 testbench，仅用于演示 CI 注册
   library ieee;
   use ieee.std_logic_1164.all;
   use std.textio.all;
   entity my_demo_tb is end entity;
   architecture sim of my_demo_tb is
   begin
       process
           variable l : line;
       begin
           write(l, string'("my_demo_tb running"));
           writeline(output, l);
           wait;  -- 停在时间尽头
       end process;
   end architecture;
   ```
2. **注册到 `config.tcl`**（参考 4.2.4）：
   - 在 tag=tb 的 `add_sources` 列表追加 `my_demo_tb.vhd`。
   - 在末尾追加 `create_tb_run "my_demo_tb"` 与 `add_tb_run`。
3. **跑 ModelSim 路径**：在 `sim/` 下执行 `vsim -do run.tcl`（或 `do run.tcl`）。在 Transcript 里确认看到 `my_demo_tb running` 与 PsiSim 打印的 `SIMULATIONS COMPLETED SUCCESSFULLY`，且**无** `###ERROR###`。
4. **跑 GHDL 路径**：执行 `vsim -do runGhdl.tcl`（实际 GHDL 入口取决于 PsiSim 的 `init -ghdl` 实现，待本地验证）。对比两条路径的 Transcript 差异——重点看编译期告警与运行期输出在数量/措辞上的不同（这正是 `compile_suppress`/`run_suppress` 在降噪的部分）。
5. **模拟 CI 判定**：用 4.3.4 的 `judge.py` 扫描两份 Transcript，确认都判定为「通过 → exit(0)」。
6. **故障注入**：在 `my_demo_tb.vhd` 里加一句故意失败的比较（如 `assert false report "###ERROR###: demo failure" severity error;`），重跑，确认 `judge.py` 这次给出「应用错误 → 255」。

**需要观察的现象**：
- 第 3、4 步：同一个 `config.tcl`，被两条路径复用，得到结构一致的结果（差异仅在编译器/仿真器自身的提示信息）。
- 第 5 步：成功标记存在且无 `###ERROR###` → 退出码 0。
- 第 6 步：注入 `###ERROR###` 后，即便 TB 跑完，退出码仍是 255（应用错误优先）。

**预期结果**：你完成了一次「新增 TB → 注册 → 双后端跑通 → CI 判定」的完整闭环。如果手头没有仿真器，至少完成第 1、2、5、6 步（注册与判定逻辑都可以脱离仿真器验证），第 3、4 步标注「待本地验证」。

## 6. 本讲小结

- **两条仿真路径只差一个词**：`run.tcl`（`init`，ModelSim）与 `runGhdl.tcl`（`init -ghdl`，GHDL）逐行相同，差异只在第 14 行的后端选择；`interactive.tcl` 是共享骨架的「半截」版，只编译不运行，供 GUI 调试。
- **`config.tcl` 是后端无关的内容文件**：六段结构（常量/库名/编译抑制/运行抑制/源码三 tag/TB 注册）回答「编译什么、跑什么」；tag 顺序即编译顺序，对应 `lib→src→tb` 的依赖拓扑。
- **新增 testbench 改两处**：`add_sources` 的 tb 列表（让其被编译）+ 一对 `create_tb_run`/`add_tb_run`（让其被运行）。
- **消息抑制分两层**：`compile_suppress`（vcom 期）、`run_suppress`（vsim 期）按 ModelSim 消息编号降噪；它是双刃剑，过度抑制会漏报真实问题，具体编号含义待对照本地 ModelSim 手册。
- **CI 三态退出码分类故障**：`exit(-1)`→shell 255（应用错误，含 `###ERROR###`）、`exit(-2)`→shell 254（框架错误，缺成功标记）、`exit(0)`（通过）；检查顺序使「自检失败」优先于「跑完」。
- **依赖来自 README**：`dependencies.py` 是 `PsiFpgaLibDependencies` 的 4 行薄壳，通过 `Parse.FromReadme` 把 `README.md` 的 Dependencies 段作为依赖的唯一事实来源，实现「人机共读、零漂移」。

## 7. 下一步学习建议

- 下一篇 **u8-l2（编码约定、错误消息机制与二次开发指南）** 会从「消费者」视角切到「生产者」视角：教你按 psi_tb 的既有约定（统一 `###ERROR###` 前缀、compare→activity→bfm 的复用链、i2c 的 `GenMessage`）**自己写一个新的 testbench 辅助过程**。本讲的 CI 知识将在那里闭环——你新写的过程只要遵循前缀约定，就能自动被 `run_check_errors "###ERROR###"` 与 `ciFlow.py` 接管判定。
- 想加深对「`###ERROR###` 前缀是怎么被代码打印出来的」的理解，回看 u3-l1（compare 骨架 `assert ... report Prefix & ... severity error`）。
- 想理解「成功标记 `SIMULATIONS COMPLETED SUCCESSFULLY` 是谁、何时打印的」，需要查阅外部 **PsiSim** 仓库的 `run_tb` 实现——这超出本仓库范围，建议作为拓展阅读。
- 如果你要给 psi_tb 贡献一个带 TB 的新 package（例如把 AXI 包正式纳入 CI），本讲的 4.2（注册源码与 TB）加上 u8-l2（编码约定）就是完整的操作手册。
