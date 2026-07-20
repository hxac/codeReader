# Block Design 集成与 CI 流水线

## 1. 本讲目标

本讲是进阶单元（u5）的收尾，也是整本手册的「交付链闭环」。读完本讲，你应该能够：

- 说清楚一个 RTL 改动如何走完「改代码 → 跑仿真 → 重新封装 IP → 在 Block Design 中使用」的完整闭环，并指出每一步对应的脚本与工具。
- 读懂 `bd/bd.tcl` 中 `init`、`pre_propagate`、`propagate` 三个回调，理解 `C_S00_AXI_ID_WIDTH` 这个参数为何在 Block Design（BD）里「不用手填、自动跟着上游走」。
- 读懂 `scripts/ciFlow.py` 如何在命令行驱动 Modelsim 跑回归、并用**两条规则**解析 Transcript 判定通过/失败。
- 把 `sim/ci.do`、`sim/run.tcl`、`scripts/ciFlow.py` 串成一条 CI 调用链，理解每一层各自负责什么。

本讲与 [u5-l2 Vivado IP 封装流程](u5-l2-ip-packaging.md) 紧密承接：u5-l2 产出的是一个可被 Vivado 消费的「封装好的 IP」（含 `component.xml`、xgui、驱动）；本讲回答两个遗留问题——这个 IP 进了 Block Design 之后参数怎么自动配？以及，每次改动 RTL 之后用什么自动化手段保证「不破坏既有行为」？

## 2. 前置知识

在进入源码前，先用通俗语言澄清几个本讲反复出现的概念。

**Block Design（BD，块设计）**：Vivado 里一种「画图式」搭系统的方式。你把一个个 IP 当作方框拖进画布，用连线（net）把它们的总线接口连起来，Vivado 帮你生成顶层 RTL。PSI 这类 IP 最终几乎都是被拖进某个 BD 里使用的，而不是直接例化 VHDL。

**AXI 总线接口（bus interface）与 ID_WIDTH**：AXI4 事务里有一组 `*id` 信号（`awid/arid/bid/rid`），宽度由 `ID_WIDTH` 决定。不同上游主机的 ID 宽度可能不同（例如 Zynq PS 的 HP 口、AXI Interconnect 的输出）。一个 AXI **从机**端口（slave）要想正确接收这些 `*id` 信号，它的 `ID_WIDTH` 必须和连过来的主机一致。

**参数自动传播（parameter propagation）**：在 BD 里，Vivado 会沿着总线连线自动「传递」某些参数——例如把主机的 `ID_WIDTH` 推给连在同一根线上的从机。这样用户不用手工把两边对齐，连上就自动匹配。本讲的 `bd.tcl` 就是插进这个传播过程中的「钩子脚本」，让本 IP 在传播的特定时刻读/写自己的参数。

**TCL 回调（callback）**：Vivado 在 BD 引擎的固定时刻会去调用几个约定名字的 TCL 过程（`init`、`pre_propagate`、`propagate`）。你只要把这些过程写在一个约定位置的 `bd/bd.tcl` 文件里，封装 IP 时它会被打包进去，BD 引擎就会在对应时刻调用它们。

**Transcript**：Modelsim 运行时把所有输出（编译信息、`puts` 打印、断言失败、测试报告）写进一个文本文件（本项目里是 `sim/Transcript.transcript`）。CI 不去看图形界面，而是等仿真跑完后**读这个文本文件**、用字符串匹配来判定成败。

**`###ERROR###` 约定**：本项目 testbench 在发现错误时会主动往 Transcript 里打印这个魔法字符串（详见 [u1-l3 运行仿真](u1-l3-running-simulation.md)）。它是软硬之间的一个契约：TB 说「我错了」就打这个串，脚本据此判失败。

## 3. 本讲源码地图

本讲只涉及三个文件，但它们分别属于三个不同目录，各司其职：

| 文件 | 目录 | 作用 | 被谁调用 |
|---|---|---|---|
| `bd/bd.tcl` | `bd/` | BD 参数传播钩子：声明 `C_S00_AXI_ID_WIDTH` 为「仅传播」，并在传播前后读写它 | Vivado BD 引擎（在 BD 校验时自动调用） |
| `scripts/ciFlow.py` | `scripts/` | CI 入口：切目录、调 `vsim`、读 Transcript、按两条规则判定退出码 | CI 流水线（命令行 `python scripts/ciFlow.py`） |
| `sim/ci.do` | `sim/` | Modelsim 的 do 文件：`source run.tcl` 后 `quit` | 被 `ciFlow.py` 里的 `vsim -c -do ci.do` 调用 |

理解这三个文件的关键是「调用链」：**CI 系统 → `ciFlow.py` → `vsim -c -do ci.do` → `ci.do` → `run.tcl` → PsiSim → 编译并跑 `top_tb`**。`bd.tcl` 则是另一条独立的链：**Vivado 用户在 BD 里连接本 IP → BD 引擎校验 → 自动调用 `bd.tcl` 的三个回调**。

> 提示：`run.tcl` 与 `config.tcl` 已在 [u1-l3 运行仿真](u1-l3-running-simulation.md) 详细讲过，本讲引用但不重复展开。

## 4. 核心概念与源码讲解

### 4.1 BD 参数传播回调

#### 4.1.1 概念说明

本 IP 的控制面端口 `S00_AXI` 是一个 AXI **从机**（见 [u3-l1 顶层 wrapper](u3-l1-wrapper-architecture.md)）。它的 `*id` 信号宽度由 generic `C_S00_AXI_ID_WIDTH` 决定，在 wrapper 实体里声明、默认值为 1：

[mem_test_wrapper.vhd:18](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L18) — 声明 `C_S00_AXI_ID_WIDTH : integer := 1`，并被 [mem_test_wrapper.vhd:37](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L37) 的 `s00_axi_arid` 等端口用作位宽。

问题来了：这个 IP 被拖进不同 BD 时，上游主机的 ID 宽度可能各不相同（有的 4 位、有的 8 位）。如果让用户在 IP 参数界面里手填，很容易填错、造成位宽不匹配。`bd.tcl` 解决的就是这件事——**让 `C_S00_AXI_ID_WIDTH` 在 BD 里自动等于「连过来的那个主机」的 ID 宽度，不需要手填**。

这里有一个容易混淆的点要先澄清：在 [u5-l2 IP 封装](u5-l2-ip-packaging.md) 里我们讲过，`C_S00_AXI_ID_WIDTH` **不是** 在 `package.tcl` 里用 `gui_create_parameter` 显式声明的用户参数，而是 Vivado 在封装时**从 AXI4 总线接口自动提取**出来的。`component.xml` 里它确实存在，标记为 `resolve="user"`、默认值 1：

[component.xml:2049-2051](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/component.xml#L2049-L2051) — `C_S00_AXI_ID_WIDTH` 作为 user 参数存在，默认值 1。

但「能在 GUI 显示」和「在 BD 里要不要用户填」是两件事。`bd.tcl` 的 `init` 回调会在 BD 场景下把这个参数标记成「仅传播（propagate only）」，于是 BD 引擎就不会让用户去改它，而是用传播机制自动赋值。两者并不矛盾。

#### 4.1.2 核心流程

Vivado BD 引擎在校验/激活一个 BD 时，对每个带钩子的 IP 按固定顺序调用三个过程：

```
┌─────────────────────────────────────────────────────────────┐
│ 1) init            标记哪些参数「只允许被传播赋值」          │
│                    （C_S00_AXI_ID_WIDTH 被标记）             │
│                                                             │
│ 2) pre_propagate   【传播之前】处理 master 接口             │
│                    把 cell 上的参数值 → 推到 master 引脚上   │
│                    方向：cell 参数 → 接口（出方向）          │
│                                                             │
│      ▼▼▼ Vivado 沿总线连线做标准参数传播 ▼▼▼               │
│                                                             │
│ 3) propagate       【传播之后】处理 slave 接口              │
│                    把 slave 引脚上到达的值 → 写回 cell 参数  │
│                    方向：接口 → cell 参数（入方向）          │
└─────────────────────────────────────────────────────────────┘
```

具体到本 IP：

- `init`：把 `C_S00_AXI_ID_WIDTH` 标记为 propagate-only。
- `pre_propagate`：遍历 **master**（`M00_AXI`，数据面）的 AXI4 接口，本想把 cell 的 ID 宽度往外推。但本 IP 的 master **没有** `C_M00_AXI_ID_WIDTH` 这个参数（master 不带 `*id` 信号），读到空值，写入被跳过——所以这一步对本 IP 实际上是**空操作**，代码只是保留了 Xilinx 通用模板的对称性。
- `propagate`：遍历 **slave**（`S00_AXI`，控制面）的 AXI4 接口，把上游主机沿连线传播到 `S00_AXI` 引脚上的 `ID_WIDTH` 值，**写回**到 cell 参数 `C_S00_AXI_ID_WIDTH`。这是本 IP 真正起作用的一步。

一句话总结方向感：**`pre_propagate` 管「出去」（master），`propagate` 管「进来」（slave）**。对本 IP 而言，起作用的是「进来」这一路——`S00_AXI` 的 ID 宽度自动跟随连接到它的上游主机。

#### 4.1.3 源码精读

文件顶部声明作者与版权，随后是三个过程。先看 `init`：

[bd/bd.tcl:7-27](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/bd/bd.tcl#L7-L27) — `init` 回调：把从机接口 `S00_AXI` 的 `ID_WIDTH` 参数标记为 propagate-only。

关键片段与含义：

- 第 11 行 `set axi_standard_param_list [list ID_WIDTH]`：定义「要处理的 AXI 标准参数清单」，目前只有 `ID_WIDTH` 一个。写成列表是为以后扩展（比如要传播 `DATA_WIDTH`、`PROTOCOL`）留接口。
- 第 12 行 `set full_sbusif_list [list S00_AXI]`：列出**需要参与自动传播的从机接口名**。本 IP 只有一个从机 `S00_AXI`。
- 第 14-26 行的循环：遍历本 IP 所有总线接口，挑出**名字在 `full_sbusif_list` 里且 MODE 为 slave** 的接口，为每个标准参数拼出形如 `C_<接口名>_<参数名>` 的字符串（这里就是 `C_S00_AXI_ID_WIDTH`）。
- 第 24 行 `bd::mark_propagate_only $cell_handle $busif_param_list`：调用 Vivado BD 命令，把 `C_S00_AXI_ID_WIDTH` 标记成「仅传播」。标记之后，BD 用户在 GUI 里无法手填它，BD 引擎改用传播机制给它赋值。

再看 `pre_propagate`（出方向，处理 master）：

[bd/bd.tcl:30-58](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/bd/bd.tcl#L30-L58) — `pre_propagate`：标准传播发生前，把 master 接口的 ID_WIDTH 推到接口引脚上。

关键判据：

- 第 37-39 行先过滤协议——只处理 `CONFIG.PROTOCOL == "AXI4"` 的接口，跳过 AXI-Lite 等（`S00_AXI` 虽然也是 slave，但它是 AXI-Lite，所以这条 master/slave 两条路都只认 AXI4 Full 接口）。
- 第 40-42 行再过滤方向——`pre_propagate` 只看 `MODE == "master"`，对本 IP 就是 `M00_AXI`。
- 第 48-49 行分别取两个值：`val_on_cell_intf_pin`（接口引脚上的 `ID_WIDTH`）与 `val_on_cell`（cell 上的 `C_M00_AXI_ID_WIDTH`）。
- 第 51-55 行：两者不等且 cell 值非空时，把 cell 值写到接口引脚上。**对本 IP，cell 上没有 `C_M00_AXI_ID_WIDTH`，`val_on_cell` 为空，这段写入被跳过**——这是诚实的「此处对本 IP 无效但代码通用」的细节，不要在阅读时假装它做了什么。

最后看 `propagate`（入方向，处理 slave）：

[bd/bd.tcl:61-90](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/bd/bd.tcl#L61-L90) — `propagate`：标准传播发生后，把到达 slave 接口引脚的 ID_WIDTH 写回 cell 参数。

它和 `pre_propagate` 几乎对称，只有两处关键差异：

- 第 71-73 行：方向过滤改为只看 `MODE == "slave"`，对本 IP 就是 `S00_AXI`（但注意第 68 行仍要求 `PROTOCOL == "AXI4"`，而 `S00_AXI` 实际是 AXI-Lite——这里能否命中取决于 BD 引擎给该接口打的 PROTOCOL 属性，阅读时不必纠结，重点是「master/slave 两个回调各自管一个方向」的对称设计）。
- 第 82-87 行：写入**方向反过来**——当接口引脚值（`val_on_cell_intf_pin`，即上游主机传播过来的值）与 cell 值不等且接口值非空时，把接口值写到 cell 参数 `C_S00_AXI_ID_WIDTH` 上。这一步正是「上游 ID 宽度自动落到本 IP slave 端口」的实现。

> 阅读提示：这三段代码是 Xilinx 官方推荐的 BD 钩子模板（PSI 几乎所有带 AXI 的 IP 都复用它）。读它时抓住「**三个回调、两个方向、一个参数清单**」即可：`init` 标记、`pre_propagate` 出、`propagate` 入。

#### 4.1.4 代码实践

**实践目标**：不依赖 Vivado 图形界面，靠源码阅读把 `bd.tcl` 三个回调的「方向」与「对本 IP 是否真起作用」梳理清楚，画出数据流向。

**操作步骤（源码阅读型实践）**：

1. 打开 [bd/bd.tcl](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/bd/bd.tcl)，对照 [hdl/mem_test_wrapper.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd) 的实体端口表，确认：本 IP 有几个 AXI 接口？分别叫什么？哪个是 master、哪个是 slave？
2. 在 `init` 中找到 `full_sbusif_list`，确认它只含 `S00_AXI`；再确认拼出来的参数名是 `C_S00_AXI_ID_WIDTH`。
3. 在 `pre_propagate` 中定位方向过滤行（master），结合 wrapper 实体确认 `M00_AXI` 是否存在 `C_M00_AXI_ID_WIDTH` 参数（提示：wrapper generic 区只有 `C_S00_AXI_ID_WIDTH`，没有 master 的 ID 宽度）。
4. 在 `propagate` 中定位方向过滤行（slave），确认写入目标是 cell 参数 `C_S00_AXI_ID_WIDTH`。

**需要观察的现象**（在脑中或纸上画出）：

```
pre_propagate(master=M00_AXI):  cell[C_M00_AXI_ID_WIDTH]  --(空)-->  接口引脚   【实际跳过】
propagate  (slave =S00_AXI ):  接口引脚(上游ID宽)  ----------->  cell[C_S00_AXI_ID_WIDTH]  【真正生效】
```

**预期结果**：你应该能得出结论——对本 IP 真正起作用的是 `propagate` 的 slave 分支，它让 `C_S00_AXI_ID_WIDTH` 在 BD 里自动跟随上游主机；`pre_propagate` 的 master 分支因 cell 上无对应参数而成为空操作。

**待本地验证**：如果你手头有 Vivado + 本 IP 的封装产物，可以做一个验证实验——在一个 BD 里把一个 ID 宽度为 8 的 AXI 主机连到 `S00_AXI`，校验 BD 后查看本 IP 的 `C_S00_AXI_ID_WIDTH` 是否自动变成 8。无 Vivado 环境时此步标注为「待本地验证」，不强行模拟。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `bd.tcl` 要把 `C_S00_AXI_ID_WIDTH` 用 `bd::mark_propagate_only` 标记成「仅传播」，而不是让用户在 GUI 里手填？

**参考答案**：因为这个参数必须和「连过来的那个上游主机」的 ID 宽度严格一致，否则 AXI `*id` 信号位宽对不上、连不上线或综合报错。让用户手填既容易错也多余——传播机制能从连线本身推出正确值。标记成 propagate-only 后，BD 引擎不再要求用户输入，而是自动赋值。

**练习 2**：`pre_propagate` 和 `propagate` 两个回调，哪个处理 master、哪个处理 slave？对本 IP 哪一个真正改写了 cell 参数？

**参考答案**：`pre_propagate` 处理 master（出方向：cell → 接口），`propagate` 处理 slave（入方向：接口 → cell）。对本 IP，`propagate`（slave 分支）真正把上游传来的 ID_WIDTH 写回了 cell 参数 `C_S00_AXI_ID_WIDTH`；`pre_propagate` 因 master 无 `C_M00_AXI_ID_WIDTH` 参数而是空操作。

**练习 3**：`axi_standard_param_list` 写成列表 `[list ID_WIDTH]` 而不是直接写死 `ID_WIDTH`，这种写法带来什么好处？

**参考答案**：可扩展性。将来如果还需要自动传播别的 AXI 标准参数（如 `DATA_WIDTH`、`PROTOCOL`），只需在列表里追加一项，三个回调的循环体会自动处理新增参数，无需复制粘贴代码。这是把「要处理的东西」数据化、与「处理逻辑」分离的常见工程惯用法。

---

### 4.2 CI 仿真驱动脚本

#### 4.2.1 概念说明

`scripts/ciFlow.py` 是整个项目的**持续集成（CI）入口**。它解决的问题是：每次有人改了 RTL（修 bug、加功能），怎么在「不打开图形界面、不需要人盯着」的前提下，自动确认这一版还能跑通全部回归测试？

它的设计哲学很简单——**把人手动做的事拆成三步并自动化**：

1. 切到正确目录（`sim/`）。
2. 用命令行模式启动 Modelsim 跑回归（`vsim -c -do ci.do`）。
3. 跑完之后读 Transcript 文本，用**两条规则**判定成败，并用进程退出码（exit code）告诉 CI 系统结果。

退出码是 CI 的「通用语言」：`0` 表示成功、非 `0` 表示失败。CI 系统（GitHub Actions、Travis、Jenkins……）只看这个码来给这次构建打绿勾或红叉。

> 术语：`vsim -c` 中的 `-c` 表示「命令行模式（command-line mode）」，Modelsim 不弹图形窗口、纯文本运行，适合在无显示器的 CI 服务器上跑。

#### 4.2.2 核心流程

`ciFlow.py` 的执行流可以用下面的伪代码描述（对应真实代码行号）：

```
THIS_DIR = 本脚本所在目录(scripts/)
os.chdir(THIS_DIR + "/../sim")          # 切到 sim 目录
os.system("vsim -c -do ci.do")          # 启动 Modelsim，跑 ci.do
content = open("Transcript.transcript") # 等仿真结束后读 Transcript

# 规则一：有用例失败（TB 主动报错）
if "###ERROR###" in content:  exit(-1)

# 规则二：没跑完（连成功标志都没打出来）
if "SIMULATIONS COMPLETED SUCCESSFULLY" not in content:  exit(-2)

# 否则视为成功
exit(0)
```

这两条规则的优先级和含义非常关键：

- **规则一（`###ERROR###` → 退出 -1）**：testbench 在比对失败时主动打印这个魔法串（见 [u1-l3](u1-l3-running-simulation.md)、[u5-l1](u5-l1-testbench-and-axi-emulation.md)）。出现它意味着「至少有一个用例没通过」——这是**业务意义上的失败**，需要人去看哪个用例错了。
- **规则二（缺 `SIMULATIONS COMPLETED SUCCESSFULLY` → 退出 -2）**：这条是兜底。如果连这个成功标志都没出现，说明仿真**根本没跑完**——可能是编译报错、脚本中途崩了、`vsim` 没装好、依赖库缺失等。用不同的退出码（-2 而非 -1）区分，方便排查时一眼看出是「测试失败」还是「根本没跑起来」。

注意两条规则的**顺序**：先查错误标记、再查成功标记。即使 Transcript 里同时出现了成功标志和错误标记（理论上不应该），规则一也会先命中并返回 -1——保守地把「有任何错误」判为失败，是正确的策略。

#### 4.2.3 源码精读

文件只有 27 行，逐段看：

[scripts/ciFlow.py:7-13](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/ciFlow.py#L7-L13) — 定位自身目录、切到 `sim/`、用 `vsim -c` 跑 `ci.do`。

含义：

- 第 9 行 `THIS_DIR = os.path.dirname(os.path.abspath(__file__))`：用 Python 内置 `__file__`（本脚本路径）算出 `scripts/` 的绝对路径。这样**无论从哪个工作目录调用 `python scripts/ciFlow.py`，都能正确定位**——这是写「可被任意位置调用」脚本的标准技巧。
- 第 11 行 `os.chdir(THIS_DIR + "/../sim")`：切到相邻的 `sim/` 目录。因为 `run.tcl`、`config.tcl`、`ci.do` 都假定工作目录是 `sim/`（`config.tcl` 里 `LibPath` 是相对路径 `../../..`，见 [u1-l2](u1-l2-repo-structure-and-dependencies.md)）。
- 第 13 行 `os.system("vsim -c -do ci.do")`：以命令行模式启动 Modelsim 并执行 `ci.do`。`os.system` 会**阻塞**直到 `vsim` 退出，保证下一行读 Transcript 时仿真已经跑完。

[scripts/ciFlow.py:15-16](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/ciFlow.py#L15-L16) — 读取 Modelsim 写出的 Transcript 文本。

这一步把整个 Transcript 文件读进一个字符串 `content`，后续两条规则都是对这个字符串做子串匹配。

[scripts/ciFlow.py:19-20](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/ciFlow.py#L19-L20) — **规则一**：出现 `###ERROR###` 即判失败，退出码 -1。

[scripts/ciFlow.py:22-23](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/ciFlow.py#L22-L23) — **规则二**：没有成功标志即判「没跑完」，退出码 -2。

[scripts/ciFlow.py:27](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/ciFlow.py#L27) — 两条规则都没命中，判成功，退出码 0。

> 设计观察：这个脚本故意写得极简——没有 try/except、没有日志框架、没有配置文件。好处是「任何人读一眼就懂、几乎不会出 bug」；代价是它假定 `vsim` 已装好、依赖库已就位、`Transcript.transcript` 一定会生成。这些前置条件由 [u1-l2](u1-l2-repo-structure-and-dependencies.md) 讲过的依赖获取脚本与目录布局保证。

#### 4.2.4 代码实践

**实践目标**：在不一定有 Modelsim 的环境下，亲手验证 `ciFlow.py` 的「两条规则」判定逻辑，并理解退出码的语义。

**操作步骤**：

1. **纯逻辑验证（任何环境都能做）**：把 `ciFlow.py` 的判定部分抄成一个最小 Python 片段，构造三种 Transcript 文本，观察退出码：

   ```python
   # 示例代码（非项目原有代码，仅用于演示判定逻辑）
   def judge(content: str) -> int:
       if "###ERROR###" in content:
           return -1
       if "SIMULATIONS COMPLETED SUCCESSFULLY" not in content:
           return -2
       return 0

   print(judge("...SIMULATIONS COMPLETED SUCCESSFULLY..."))        # 期望 0
   print(judge("...###ERROR### at test 3..."))                      # 期望 -1
   print(judge("Error: could not compile psi_common"))             # 期望 -2
   ```
2. **本地真跑（需要 Modelsim + 依赖库）**：确认依赖已按 [u1-l2](u1-l2-repo-structure-and-dependencies.md) 拉到公共根目录后，在仓库根执行 `python scripts/ciFlow.py`，观察末尾退出码（`echo $?`）。

**需要观察的现象**：

- 纯逻辑验证：三行打印分别输出 `0`、`-1`、`-2`，且「同时含成功标志和错误标记」时返回 `-1`（规则一优先）。
- 本地真跑：Transcript 里应出现 `SIMULATIONS COMPLETED SUCCESSFULLY`，退出码为 0。

**预期结果**：判定逻辑与真实 `ciFlow.py` 完全一致；本地无 Modelsim 时第 2 步标注「待本地验证」，不假装已运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么规则一用退出码 `-1`、规则二用 `-2`，而不是统一用 `1`？

**参考答案**：用不同退出码可以区分两种截然不同的失败——`-1` 表示「测试真的跑出了错（有用例失败）」，`-2` 表示「根本没跑完（编译错、环境缺、脚本崩）」。CI 报错时人能一眼判断该去看 testbench 还是去看环境/脚本，缩短排障时间。

**练习 2**：如果有人不小心把 testbench 里的 `###ERROR###` 打印改成了别的字符串，CI 会怎样？

**参考答案**：规则一会失效——即使有用例失败、打印了新字符串，`ciFlow.py` 也认不出来。此时只要 Transcript 里仍出现了 `SIMULATIONS COMPLETED SUCCESSFULLY`，CI 就会误判为通过（退出 0）。这说明 `###ERROR###` 是脚本与 TB 之间一个**必须共同遵守的契约**，不能随意改动。

**练习 3**：`ciFlow.py` 用 `os.system` 调 `vsim`。如果 `vsim` 命令不存在（没装 Modelsim），脚本会怎样？

**参考答案**：`os.system` 不会抛异常，它只返回一个状态码，而本脚本并没有检查这个返回值。接着第 15 行 `open("Transcript.transcript")` 会因为文件不存在而抛 `FileNotFoundError`，脚本异常退出（非 0 退出码），CI 判失败。结论是：缺 Modelsim 时 CI 会红，但失败原因不在两条规则里，而是一个未捕获异常——这是该脚本「故意极简」带来的已知短板。

---

### 4.3 CI do 文件

#### 4.3.1 概念说明

`sim/ci.do` 是 Modelsim 的 **do 文件**——一种 Modelsim 原生的命令脚本，用 `vsim -do <文件>` 即可让 Modelsim 顺序执行其中的命令。它是 `ciFlow.py`（Python 世界）与 `run.tcl`（PsiSim/TCL 世界）之间的**桥接层**。

它的存在回答了一个问题：`run.tcl` 本来就是给人「在 Modelsim 里 `source` 一下」用的（见 [u1-l3](u1-l3-running-simulation.md)），那 CI 在命令行模式（`vsim -c`）下怎么复用同一套逻辑？答案是不复制、不重写，而是用一个极薄的 do 文件**转一下手**：`source run.tcl` 跑完回归后立刻 `quit` 退出 Modelsim，把控制权交还给 `ciFlow.py`。

这种「薄桥接」的好处是：人工跑（`source ./run.tcl`）和 CI 跑（`vsim -c -do ci.do`）走的是**完全相同的回归逻辑**，不会出现「我本地能过、CI 过不了」或反之的分歧。

#### 4.3.2 核心流程

`ci.do` 的执行流只有两步：

```
source run.tcl     # 载入并执行 run.tcl（加载 PsiSim、读 config.tcl、编译、跑 TB、查错）
quit               # 立即退出 Modelsim，让 ciFlow.py 继续往下读 Transcript
```

但「`source run.tcl`」这一行背后展开的是 [u1-l3](u1-l3-running-simulation.md) 讲过的完整 PsiSim 流程：

```
source ../../../TCL/PsiSim/PsiSim.tcl   # 载入 PsiSim 框架
psi::sim::init                          # 初始化
source ./config.tcl                     # 声明库与源文件、注册 top_tb
psi::sim::compile -all -clean           # 全量干净编译
psi::sim::run_tb -all                   # 跑所有 TB 用例
psi::sim::run_check_errors "###ERROR###" # 扫描 ###ERROR### 标记
```

也就是说，`ci.do` 把这些通通串起来跑完，再 `quit`。注意 `run.tcl` 自己**只负责跑、不负责判定退出码**——它内部用 `psi::sim::run_check_errors` 扫描错误标记并打印结果（成功会打印 `SIMULATIONS COMPLETED SUCCESSFULLY`，失败则 TB 已打印 `###ERROR###`），但脚本本身不 `exit`。判定退出码这件事被刻意留给了 `ciFlow.py`，做到了「跑测试」与「判结果」的职责分离。

#### 4.3.3 源码精读

[sim/ci.do:7-8](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/ci.do#L7-L8) — `source run.tcl` 后 `quit`，是 `ciFlow.py` 与 PsiSim 之间的桥接。

逐行含义：

- 第 7 行 `source run.tcl`：把同目录下的 [sim/run.tcl](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/run.tcl) 载入并顺序执行。`run.tcl` 内部会完成编译（[run.tcl:20](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/run.tcl#L20) `compile -all -clean`）、跑 TB（[run.tcl:24](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/run.tcl#L24) `run_tb -all`）、查错（[run.tcl:29](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/run.tcl#L29) `run_check_errors "###ERROR###"`）。
- 第 8 行 `quit`：立即退出 Modelsim。没有这一行，`vsim -c` 会停在命令行提示符等输入，`os.system` 永远不返回，`ciFlow.py` 卡死。所以这行看似可有可无，实则是 CI 模式下「能自动结束」的关键。

> 对比记忆：仓库里还有一个 [sim/interactive.tcl](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/interactive.tcl)，它只 `source PsiSim` + `init` + `source config.tcl` + `compile_files -all -clean`，**不跑 TB、不 `quit`**——它是给人开 Modelsim 图形界面、在 TCL 控制台里手动调试单个用例用的。`ci.do` 则相反：跑全部、立即退出。两者对照能很好理解「调试入口」与「CI 入口」的区别。

#### 4.3.4 代码实践

**实践目标**：把 `ci.do` 放回整条 CI 调用链里，理解它是如何被上层调用、又如何调用下层的。

**操作步骤（源码阅读型实践）**：

1. 从 [scripts/ciFlow.py:13](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/ciFlow.py#L13) 找到 `vsim -c -do ci.do`，确认 `ci.do` 是被这一行调用的。
2. 打开 [sim/ci.do](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/ci.do)，确认它只做两件事：`source run.tcl` 与 `quit`。
3. 再打开 [sim/run.tcl](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/run.tcl)，确认它内部依次调用了 PsiSim 的 `init`、`compile -all -clean`、`run_tb -all`、`run_check_errors`。
4. 画一条从「CI 系统」到「top_tb 用例执行」的完整调用链。

**需要观察的现象**（画出调用链）：

```
CI 系统
  └─ python scripts/ciFlow.py            (scripts/ciFlow.py)
       └─ vsim -c -do ci.do              (切到 sim/ 后启动)
            └─ ci.do                     (sim/ci.do)
                 ├─ source run.tcl       (sim/run.tcl)
                 │    ├─ source PsiSim.tcl
                 │    ├─ psi::sim::init
                 │    ├─ source config.tcl
                 │    ├─ compile -all -clean
                 │    ├─ run_tb -all  ── 跑 top_tb 全部用例
                 │    └─ run_check_errors "###ERROR###"
                 └─ quit                 (退出 Modelsim)
       └─ 读 Transcript.transcript
       └─ 两条规则 → exit(0 / -1 / -2)
```

**预期结果**：你能清晰指出每一层负责什么——`ciFlow.py` 负责「环境与判定」，`ci.do` 负责「桥接与退出」，`run.tcl` 负责「编译与跑测」，PsiSim 负责具体编译/仿真机制，`top_tb` 负责用例与断言。

**待本地验证**：在「有 Modelsim + 依赖」的环境下，把这条链人工跑一遍（`python scripts/ciFlow.py`），对照 Transcript 验证调用顺序与最终退出码；无环境则止步于源码阅读。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉 `ci.do` 里的 `quit` 这一行，CI 还能正常工作吗？

**参考答案**：不能。`vsim -c` 跑完 `source run.tcl` 后会停在 Modelsim 命令行提示符等待输入，`os.system` 调用永远不会返回，`ciFlow.py` 会卡死在那一行，既不会读 Transcript、也不会给 CI 退出码，CI 任务会一直挂着直到超时。`quit` 是「能自动结束」的必要条件。

**练习 2**：为什么不直接让 `ciFlow.py` 用 `vsim -c -do "source run.tcl; quit"` 内联命令，而要单独写一个 `ci.do` 文件？

**参考答案**：单独文件有两个好处。一是**可读性与可维护性**：把 Modelsim 侧的启动序列集中在一个 `.do` 文件里，便于单独审查和修改，命令行引号转义也更简单。二是**复用**：`ci.do` 这条「跑完即退」的序列是 CI 专用入口，与人手调试用的 `interactive.tcl`（不跑 TB、不退出）区分开，职责清晰。内联写法虽也能工作，但会把两套逻辑混在 `ciFlow.py` 一行里，不利于维护。

**练习 3**：`run.tcl` 末尾的 `psi::sim::run_check_errors "###ERROR###"` 和 `ciFlow.py` 里 `if "###ERROR###" in content` 是不是重复检查？

**参考答案**：不是重复，而是**两层不同机制**。`run.tcl` 里的 `run_check_errors` 是 PsiSim 提供的**运行时**检查——它在仿真刚跑完时扫描输出、把错误情况**打印**到 Transcript（成功时打印 `SIMULATIONS COMPLETED SUCCESSFULLY`）。`ciFlow.py` 的字符串匹配则是**事后**检查——它读最终的 Transcript 文件、据此**决定进程退出码**。前者负责「报告」，后者负责「据此判通过/失败」，是职责分离而非重复。

---

## 5. 综合实践

**任务**：梳理一条贯穿本讲的「修改 RTL → 跑 CI 仿真 → 重新封装 IP → 在 Block Design 中使用」的完整开发闭环，标出每一步对应的脚本/工具，并说明 `bd.tcl` 中 `pre_propagate` 与 `propagate` 在 master/slave 两个方向上各自处理什么。

**参考闭环（你应当能自己复述出来）**：

| 阶段 | 做什么 | 对应脚本/工具 | 本讲涉及要点 |
|---|---|---|---|
| ① 改 RTL | 修改 `hdl/*.vhd`（核心逻辑、wrapper、package）或 `tb/top_tb.vhd` | 编辑器 | 改完后必须自证「没破坏既有行为」 |
| ② 跑 CI 仿真 | 命令行跑回归，拿退出码 | `python scripts/ciFlow.py` → `vsim -c -do ci.do` → `ci.do` → `run.tcl` → PsiSim → `top_tb` | 两条规则：`###ERROR###`→-1、缺成功标志→-2、否则 0 |
| ③ 重新封装 IP | 把 RTL 重新打包成 Vivado IP | `vivado -mode batch -source scripts/package.tcl`（PsiIpPackage 框架，见 [u5-l2](u5-l2-ip-packaging.md)） | 产出含 `bd/bd.tcl`、`component.xml`、xgui、驱动的 IP |
| ④ 在 BD 中使用 | 把 IP 拖进 Block Design、连线 | Vivado BD 引擎 | 连线后 BD 引擎自动调用 `bd.tcl` 的三个回调 |

**`bd.tcl` 两个方向的处理（本任务要求回答的核心）**：

- `pre_propagate`（标准传播**之前**）：遍历 **master** 方向（`MODE == "master"` 且 `PROTOCOL == "AXI4"`，即 `M00_AXI`），方向是 **cell 参数 → 接口引脚**，把 cell 上配置的 ID 宽度推到 master 引脚上，供下游传播。**对本 IP，因 master 无 `C_M00_AXI_ID_WIDTH` 参数、读到空值，这一步实际跳过。**
- `propagate`（标准传播**之后**）：遍历 **slave** 方向（`MODE == "slave"`，即 `S00_AXI`），方向是 **接口引脚 → cell 参数**，把上游主机沿连线传播到 slave 引脚上的 ID 宽度，写回 cell 参数 `C_S00_AXI_ID_WIDTH`。**这是本 IP 真正生效的一步——`S00_AXI` 的 ID 宽度自动跟随上游主机。**

**进阶思考（可选）**：如果你要给本 IP 新增一个「测试超时阈值」参数（已在 [u5-l2](u5-l2-ip-packaging.md) 综合实践中设计过），它需要走 BD 传播吗？

- 如果它只是个**普通配置参数**（用户在 GUI 填、写进 generic），那么**不需要**进 `bd.tcl`——`bd.tcl` 只处理需要「跨 IP 自动对齐」的总线标准参数（如 ID_WIDTH）。普通参数走 `package.tcl` 的 `gui_create_parameter` + xgui 的 `update_MODELPARAM_VALUE` 即可（见 [u5-l2](u5-l2-ip-packaging.md)）。
- 只有当某个参数**必须与连线对端保持一致**（典型是 AXI 的 `ID_WIDTH`、`DATA_WIDTH`、`PROTOCOL`）时，才需要写进 `bd.tcl` 的 `axi_standard_param_list`，让 BD 引擎自动传播。

## 6. 本讲小结

- 本讲把整本手册的知识串成一条「交付链」：改 RTL → CI 仿真 → 重新封装 → BD 集成，每一步都有明确对应的脚本/工具。
- `bd/bd.tcl` 是 Vivado BD 引擎的钩子脚本，含 `init`、`pre_propagate`、`propagate` 三个回调；`init` 把 `C_S00_AXI_ID_WIDTH` 标记为「仅传播」，`pre_propagate` 处理 master（出方向）、`propagate` 处理 slave（入方向）。
- 对本 IP 真正生效的是 `propagate` 的 slave 分支——它让 `S00_AXI` 的 ID 宽度在 BD 里自动跟随上游主机；`pre_propagate` 的 master 分支因 master 无对应参数而成为空操作（保留通用模板的对称性）。
- `scripts/ciFlow.py` 是 CI 入口：切到 `sim/`、`vsim -c -do ci.do`、读 Transcript、按两条规则判定——`###ERROR###`→退出 -1、缺 `SIMULATIONS COMPLETED SUCCESSFULLY`→退出 -2、否则退出 0。
- `sim/ci.do` 是 Python（`ciFlow.py`）与 TCL（`run.tcl`/PsiSim）之间的薄桥接：`source run.tcl` 后 `quit`，保证人工与 CI 走完全相同的回归逻辑。
- 关键设计理念：**职责分离**——`run.tcl` 只跑不判、`ci.do` 只桥接不判、`ciFlow.py` 才判退出码；`bd.tcl` 只处理需跨 IP 对齐的总线参数，普通用户参数走 xgui（u5-l2）。

## 7. 下一步学习建议

到这里，你已经读完了 `vivadoIP_mem_test` 从「项目是什么」（u1）到「寄存器与驱动」（u2）、「核心 RTL 与状态机」（u3）、「AXI 集成」（u4）、「验证与交付」（u5）的完整链条。建议下一步：

1. **横向扩展到 PSI 同库的其他 IP**：本讲的 `bd.tcl` 三回调模板、`ciFlow.py` + `ci.do` + `run.tcl` 的 CI 三件套、`package.tcl` 的封装七步，都是 PSI 全家桶的通用脚手架。挑一个更复杂的 PSI IP（如 `psi_common` 里的 AXI 组件）对照阅读，你会看到同一套机制在更大规模下的用法。
2. **亲手做一次端到端闭环**：在有 Vivado + Modelsim 的环境里，故意改一行 RTL（比如给 [mem_test.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd) 某个 pattern 种子加一），观察 CI 是否能抓住行为变化（参考 [u3-l4 pattern 生成](u3-l4-pattern-generation-and-check.md) 与 [u5-l1 testbench](u5-l1-testbench-and-axi-emulation.md)），再走封装与 BD 流程。
3. **回看核心**：如果对 BD 里 `S00_AXI`/`M00_AXI` 两个接口的握手细节还不够熟，建议重读 [u4-l1 AXI-Lite 从机](u4-l1-axi-lite-slave.md) 与 [u4-l2 AXI4 主机](u4-l2-axi4-master.md)，把「BD 层的参数传播」与「RTL 层的通道握手」两层视角对齐。
4. **研究依赖与分发**：结合 [u1-l2](u1-l2-repo-structure-and-dependencies.md) 的依赖机制与 [u5-l2](u5-l2-ip-packaging.md) 的 `component.xml`，思考「一个 PSI IP 如何被组合进更大的 `psi_fpga_all` 工程并在团队中分发」——这是从「读懂单个 IP」走向「维护一整套 FPGA 库」的下一步。
