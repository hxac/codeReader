# 仿真环境与 CI 构建流程

## 1. 本讲目标

上一讲（u1-l2）我们看懂了仓库的目录布局，以及 `sim/config.tcl` 怎样用 `add_sources ... -tag lib/src/tb` **描述**要编译什么。但「描述」不等于「执行」——config.tcl 自己并不会编译任何东西。本讲就补上这一环：**这些脚本到底是怎么被跑起来的，CI 又凭什么判定一次仿真通过还是失败**。

学完本讲，你应该能做到：

- 拿着 `sim/run.tcl`（ModelSim）或 `sim/runGhdl.tcl`（GHDL），讲清 `init → compile_files → run_tb → run_check_errors` 这条流水线每一站做了什么。
- 理解 `config.tcl` 里的 `compile_suppress` / `run_suppress` 消息抑制，以及末尾 `run_check_errors "###ERROR###"` 的作用。
- 理清 `ci.do → run.tcl → ciFlow.py` 三者如何串联，以及 ciFlow.py 用 **`###ERROR###`** 与 **`SIMULATIONS COMPLETED SUCCESSFULLY`** 两个标记做「双重检查」并给出退出码。
- 能够在本机（或容器）里尝试跑通 `psi_tb_i2c_pkg_tb`，或至少能基于源码**预测**三种不同结局下 CI 的退出码。

> 提示：本讲会承接 u1-l2 对 config.tcl 的讲解，但**不重复** `add_sources` / `tag` 的细节。如果你忘了 tag 的含义，先回去看 u1-l2 的 4.3 节。

## 2. 前置知识

本讲假设你已读过 u1-l1、u1-l2，理解以下概念：

- **PsiSim**：PSI 自研的一套 **TCL 仿真框架**（不是仿真器本身）。它把 ModelSim / GHDL 这些底层仿真器的编译、运行命令封装成 `init`、`compile_files`、`run_tb` 等统一命令，让我们用同一套脚本切换不同仿真器。
- **ModelSim / vsim**：Mentor（现 Siemens）出品的主流商业 VHDL 仿真器，命令行入口是 `vsim`。
- **GHDL**：开源的 VHDL 仿真器，可作为 ModelSim 的免费替代。
- **Transcript**：ModelSim 把仿真过程中所有输出（编译信息、`print` 打印、`assert`/`report` 消息）写进的一个日志文件/窗口。本讲里「Transcript」既指 ModelSim 的输出窗口，也指它落盘的 `Transcript.transcript` 文件。
- **CI（持续集成）**：每次提交代码后，服务器自动跑一遍编译+仿真，根据退出码判定「通过/失败」。
- **`###ERROR###`**：psi_tb 全库统一使用的错误消息前缀（详见 u3 比较检查、u8 编码约定）。testbench 里的检查过程一旦发现实际值与期望不符，就用这个前缀打印一行错误。

还有一个**仿真器为什么必须按顺序编译**的事实（u1-l2 已述）：VHDL 要求被引用的 package 先编译。所以本讲里的 `compile_files` 实际是按 config.tcl 里 tag 的顺序（`lib → src → tb`）逐个编译的。

最后一句直觉：**testbench 自己不会主动喊「我成功了」，是仿真框架在所有 TB 都正常跑到 `wait;` 停下后，替它打印一句成功标记**。理解这一点，就能理解 CI 为什么要有两道独立的检查。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `sim/config.tcl` | PsiSim 编译配置（u1-l2 已详述） | 本讲只关注其中的消息抑制 `compile_suppress`/`run_suppress` 与 TB 注册 `create_tb_run` |
| `sim/run.tcl` | ModelSim 批处理运行脚本 | `init` / `source config.tcl` / `compile_files` / `run_tb` / `run_check_errors` 这条主线 |
| `sim/runGhdl.tcl` | GHDL 批处理运行脚本 | 与 run.tcl 的唯一区别：`init -ghdl` |
| `sim/interactive.tcl` | ModelSim 交互式调试入口 | 只编译不运行，用于在 GUI 里手动调试 |
| `sim/ci.do` | CI 用的 do 文件（ModelSim 入口） | `onerror {exit}` + `source run.tcl` + `quit` |
| `scripts/ciFlow.py` | CI 判定脚本（Python） | 启动 vsim、读 `Transcript.transcript`、两道检查、给退出码 |
| `sim/.gitignore` | sim 目录的忽略规则 | 说明 `*.transcript` 为何不入库 |

## 4. 核心概念与源码讲解

### 4.1 PsiSim 仿真执行流程：init → compile_files → run_tb

#### 4.1.1 概念说明

`sim/run.tcl` 和 `sim/runGhdl.tcl` 是两个**几乎一模一样**的批处理脚本。它们的任务很单纯：把「初始化 → 编译 → 运行 → 检查」四步用 PsiSim 的命令串成一条流水线，一次性跑完整个 testbench。

这两个脚本本身**不含任何文件清单**——文件清单全在 `config.tcl` 里。run.tcl 只负责「流程」，config.tcl 只负责「内容」。两者通过 run.tcl 里的一句 `source ./config.tcl` 衔接起来。这种「流程与配置分离」的写法，使得同一份 config.tcl 既能被 ModelSim（run.tcl）用，也能被 GHDL（runGhdl.tcl）用，还能被交互调试（interactive.tcl）用。

run.tcl 与 runGhdl.tcl 的**唯一实质区别**是初始化命令：ModelSim 用 `init`，GHDL 用 `init -ghdl`。PsiSim 用这一个开关决定后端调用哪个仿真器。

#### 4.1.2 核心流程

run.tcl（同样适用于 runGhdl.tcl）的执行流程可以画成 5 步：

```
┌─────────────────────────────────────────────────────────────┐
│ 1. source PsiSim.tcl        载入 PsiSim 框架（提供 psi::sim 命令）│
│ 2. namespace import psi::sim::*   把 init/compile_files/run_tb │
│                                    等命令导入当前命名空间        │
│ 3. init  (或 init -ghdl)     初始化仿真器环境                   │
│ 4. source ./config.tcl       载入编译配置（库、源文件、TB 注册）  │
│ 5. compile_files -all -clean  按 config 的 tag 顺序全部（干净）编译│
│ 6. run_tb -all               运行所有注册的 testbench           │
│ 7. run_check_errors "###ERROR###"  扫描 Transcript，遇错误标记  │
└─────────────────────────────────────────────────────────────┘
```

把它和 config.tcl 的分工对照一下：

| 步骤 | 谁来做 | 做什么 |
|---|---|---|
| 决定「编译哪些文件、什么顺序」 | config.tcl | `add_sources ... -tag lib/src/tb` |
| 决定「怎么编译、怎么运行」 | run.tcl | `compile_files` / `run_tb` |
| 决定「用哪个仿真器」 | run.tcl 第 14 行 | `init`（ModelSim）或 `init -ghdl`（GHDL） |

第 5 步里的 `-clean` 表示「先清掉旧的编译产物再重新编译」，保证每次都是干净构建（CI 友好，避免旧产物干扰）。`-all` 表示「把 config 里所有 tag 的文件都编译」（相对于「只编译某个 tag」）。

第 6 步 `run_tb -all` 的「all」指 config.tcl 里通过 `create_tb_run` + `add_tb_run` 注册的**所有** testbench 运行。当前项目只注册了一个（`psi_tb_i2c_pkg_tb`），所以实际只跑它。

#### 4.1.3 源码精读

**run.tcl 的完整骨架**（它是本讲最重要的文件）：

- [sim/run.tcl:8](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/run.tcl#L8)：`source ../../../TCL/PsiSim/PsiSim.tcl`——载入 PsiSim 框架。注意这个相对路径：从 `sim/` 往上三级到 workspace 根，再进 `TCL/PsiSim/`，印证了 u1-l2 讲的目录结构（PsiSim 是仓库外的同级依赖）。
- [sim/run.tcl:11](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/run.tcl#L11)：`namespace import psi::sim::*`——把 PsiSim 定义在 `psi::sim` 命名空间下的所有命令（`init`、`compile_files`、`run_tb` 等）导入当前作用域，这样后面能直接写 `init` 而不是 `psi::sim::init`。
- [sim/run.tcl:14](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/run.tcl#L14)：`init`——初始化 ModelSim 仿真环境（创建 work 库等）。
- [sim/run.tcl:17](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/run.tcl#L17)：`source ./config.tcl`——**这里是流程与配置的衔接点**。config.tcl 在此刻被求值，它内部的 `add_library` / `add_sources` / `create_tb_run` 把配置登记进 PsiSim 的内部状态。
- [sim/run.tcl:20-23](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/run.tcl#L20-L23)：打印分节标题 `-- Compile`，然后 `compile_files -all -clean` 真正触发编译。
- [sim/run.tcl:24-27](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/run.tcl#L24-L27)：打印 `-- Run`，然后 `run_tb -all` 运行所有注册的 testbench。
- [sim/run.tcl:28-32](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/run.tcl#L28-L32)：打印 `-- Check`，然后 `run_check_errors "###ERROR###"` 做错误扫描（4.2 节详述）。

> 注意 run.tcl 里那些 `puts "------------------------------"` 只是给人看的分节标题，让 Transcript 输出更易读，本身不影响仿真。

**runGhdl.tcl 的差异**——只有第 14 行不同：

- [sim/runGhdl.tcl:14](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/runGhdl.tcl#L14)：`init -ghdl`——改用 GHDL 作为后端仿真器。除此之外，[sim/runGhdl.tcl:17](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/runGhdl.tcl#L17) 的 `source ./config.tcl`、[sim/runGhdl.tcl:23](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/runGhdl.tcl#L23) 的 `compile_files -all -clean`、[sim/runGhdl.tcl:27](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/runGhdl.tcl#L27) 的 `run_tb -all`、[sim/runGhdl.tcl:32](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/runGhdl.tcl#L32) 的 `run_check_errors` 都与 run.tcl 逐字相同。这就是 PsiSim「一份配置、两个仿真器」的价值。

**interactive.tcl：交互调试入口**——它只做编译、不自动运行：

- [sim/interactive.tcl:15-19](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/interactive.tcl#L15-L19)：`init` + `source ./config.tcl` + `compile_files -all -clean`，然后就停下。它不调用 `run_tb`，目的是让你在 ModelSim 的 GUI / TCL 控制台里**手动**启动仿真、加波形、单步调试。注意第 7 行注释明确写了「setps up Modelsim for interactively working from the TCL console」。

#### 4.1.4 代码实践

**实践目标**：不依赖任何仿真器，仅靠「读脚本 + 对照」，验证 run.tcl 与 runGhdl.tcl 的差异确实只有一处。

**操作步骤**：

1. 同时打开 [sim/run.tcl](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/run.tcl) 与 [sim/runGhdl.tcl](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/runGhdl.tcl)。
2. 逐行对比，找出所有不同的行。
3. 把不同的行抄下来，标注各自用的仿真器。

**需要观察的现象**：两个文件除了第 14 行（`init` vs `init -ghdl`），其余完全相同。

**预期结果**：

| 文件 | 第 14 行 | 仿真器 |
|---|---|---|
| `run.tcl` | `init` | ModelSim |
| `runGhdl.tcl` | `init -ghdl` | GHDL |

> 进阶（**待本地验证**，需安装 ModelSim/GHDL + psi_common + PsiSim）：在 `sim/` 目录下分别执行 `vsim -do run.tcl`（ModelSim）与对应 GHDL 命令，跑通 `psi_tb_i2c_pkg_tb`，对比两条路径下 Transcript 的差异。这是本讲的**主实践任务**，详见第 5 节综合实践。

#### 4.1.5 小练习与答案

**练习 1**：run.tcl 为什么不把文件清单（`psi_tb_i2c_pkg.vhd` 等）直接写在自己里面，而要 `source ./config.tcl`？

**答案**：为了「流程与配置分离」。run.tcl 只描述怎么跑（流程），config.tcl 描述跑什么（内容）。这样同一份 config.tcl 能被 run.tcl、runGhdl.tcl、interactive.tcl 三个入口复用，改文件清单只需改一处。

**练习 2**：如果想在 ModelSim 里手动调试 `psi_tb_i2c_pkg_tb`（看波形、单步），应该用哪个脚本？为什么不用 run.tcl？

**答案**：用 [sim/interactive.tcl](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/interactive.tcl)。因为它只编译（`compile_files -all -clean`）不自动运行（没有 `run_tb`），编译完就把控制权交还给你，方便在 GUI 里手动启动仿真、加波形。run.tcl 会一口气 `run_tb -all` 跑完并退出，不适合调试。

---

### 4.2 消息抑制与 run_check_errors "###ERROR###"

#### 4.2.1 概念说明

仿真器在编译和运行时会产生**大量**信息：有些是真错误，有些是无害的告警/提示（比如「某信号未驱动」「某延时被舍入」）。如果把这些噪音都留在 Transcript 里，真正的错误会被淹没，CI 也难以自动判定。

psi_tb 用两个手段治理这个问题：

1. **消息抑制**：在 config.tcl 里用 `compile_suppress` / `run_suppress` 告诉 PsiSim/ModelSim「这几个编号的消息别打印」，从源头降噪。
2. **错误标记扫描**：在 run.tcl 末尾用 `run_check_errors "###ERROR###"`，让 PsiSim 扫描整个 Transcript，如果出现 `###ERROR###` 这个前缀，就认为 testbench 报告了错误。

`###ERROR###` 这个前缀是 psi_tb 全库的**约定**：所有比较检查过程（u3 详述）、活动检查（u4）、BFM（u5/u7）在发现实际值与期望不符时，都用 `print` 打印以 `###ERROR###` 开头的一行消息。因此「Transcript 里有没有 `###ERROR###`」就成了「testbench 自检有没有发现错误」的统一信号。

#### 4.2.2 核心流程

消息抑制与错误扫描配合的逻辑：

```
config.tcl 里:
  compile_suppress 135,1236,1370    编译阶段屏蔽这 3 个编号的提示/警告
  run_suppress    8684,3479,3813,8009,3812   运行阶段屏蔽这 5 个编号

run.tcl 末尾:
  run_check_errors "###ERROR###"   扫描 Transcript:
        ┌─ 出现 "###ERROR###"  → PsiSim 报告发现错误（本讲末讨论 CI 如何处理）
        └─ 未出现               → 本轮自检无应用错误
```

为什么把抑制和扫描放在一起讲？因为它们是**互补**的：抑制把仿真器自带的噪音去掉，让 Transcript 只剩下「人写的 `print`」和「真正的仿真器错误」；而 `run_check_errors` 再在这份干净的日志里找 `###ERROR###`。如果不去噪，扫描可能被无关信息干扰；如果不扫描，光去噪也无法判断 testbench 自检的结果。

> 关于抑制编号的含义：`compile_suppress 135,1236,1370` 与 `run_suppress 8684,...` 里的数字是 **ModelSim 的消息编号**（ModelSim 每条警告/提示都有一个唯一编号）。这些具体编号对应哪些消息，需要查阅 ModelSim 文档或在本机观察 Transcript 确认，**待本地验证**。讲义不臆测每个编号的语义，只需理解「这是一组按编号屏蔽的消息」即可。

#### 4.2.3 源码精读

**config.tcl 的消息抑制**：

- [sim/config.tcl:17](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L17)：`compile_suppress 135,1236,1370`——编译阶段屏蔽 3 个编号的消息。
- [sim/config.tcl:18](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L18)：`run_suppress 8684,3479,3813,8009,3812`——运行阶段屏蔽 5 个编号的消息。

**config.tcl 的 TB 注册**（决定 `run_tb -all` 实际跑什么）：

- [sim/config.tcl:41](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L41)：`create_tb_run "psi_tb_i2c_pkg_tb"`——声明一个 TB 运行，顶层实体是 `psi_tb_i2c_pkg_tb`。
- [sim/config.tcl:42](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L42)：`add_tb_run`——把上面声明的这个运行**加入待执行列表**。所以 `run_tb -all` 跑的就是这里登记的运行。

**run.tcl 末尾的错误扫描**：

- [sim/run.tcl:32](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/run.tcl#L32)：`run_check_errors "###ERROR###"`——PsiSim 提供的命令，参数是要扫描的错误标记字符串。

> 说明：`run_check_errors`、`create_tb_run`、`add_tb_run`、`compile_suppress`、`run_suppress` 都是 **PsiSim 框架**（外部依赖 `TCL/PsiSim/PsiSim.tcl`）提供的命令，其实现不在本仓库内。本讲基于它们在脚本中的**用法**和 CI 脚本的**消费方式**来描述行为。PsiSim 是否在发现 `###ERROR###` 时直接令 TCL 进程以非零状态退出，这一细节**待本地验证**；但可以确定的是——CI 的**权威判定**并不依赖这一点，而是由下一节 ciFlow.py 独立完成。

#### 4.2.4 代码实践

**实践目标**：理解「`###ERROR###` 前缀 = testbench 自检失败信号」这一约定，并能在源码里找到它的踪迹。

**操作步骤**：

1. 在仓库内检索（只读）`###ERROR###` 的来源：

   ```
   # 用 ripgrep（只读，不改源码）
   rg -n "###ERROR###" hdl/
   ```

2. 观察哪些 package、在什么默认值里出现了这个字符串（提示：它通常作为比较过程的 `Prefix` 参数默认值）。

**需要观察的现象**：`###ERROR###` 应当集中出现在 `psi_tb_compare_pkg.vhd`（以及间接复用它的 activity / i2c）里，作为过程参数的默认前缀。这正是 run.tcl 要扫描 `###ERROR###` 的原因——这些过程一旦发现不一致，就会用此前缀打印一行。

**预期结果**：检索结果会让你看到「比较失败 → 打印 `###ERROR###` → run_check_errors 扫描到 → CI 判失败」这条链的证据链起点。具体每个比较过程如何拼接消息，留给 u3 详述。

> **待本地验证**：在本机跑通仿真后，可临时把某个期望值改成错误值（例如把 testbench 里某次 `I2cMasterSendAddr(16#12#, ...)` 的地址改成 `16#13#`），观察 Transcript 里是否真的出现一行 `###ERROR### ...`，并留意 run_check_errors 的输出。注意：修改 testbench 仅用于本机学习，**不要提交**。

#### 4.2.5 小练习与答案

**练习 1**：为什么需要在 config.tcl 里用 `compile_suppress` / `run_suppress` 屏蔽消息，而不是让它们全部打印出来？

**答案**：仿真器会产生大量无害的告警/提示（按 ModelSim 消息编号）。全部打印会淹没真正的人写消息和真正的错误，也干扰 `run_check_errors` 的可读性。屏蔽掉已知无害的编号，Transcript 就只剩下有用信息。

**练习 2**：`###ERROR###` 这个字符串是由仿真器（ModelSim/GHDL）产生的，还是由 psi_tb 的 testbench 代码产生的？

**答案**：由 **psi_tb 的代码**产生。它是 psi_tb 比较检查等过程的统一错误前缀（见 `hdl/psi_tb_compare_pkg.vhd`），不是仿真器内置的。所以「扫描 `###ERROR###`」本质是在扫描「我们的 testbench 自己报告的错误」。

---

### 4.3 CI 判定：ci.do → ciFlow.py 的双重检查

#### 4.3.1 概念说明

到目前为止，run.tcl 只是在**本地**跑了一遍仿真。CI（持续集成）要做的事是：在服务器上**无人值守**地跑完它，并根据结果给一个机器可读的「通过/失败」信号——也就是进程的**退出码**。

psi_tb 的 CI 由两个文件配合完成：

- `sim/ci.do`：一个 ModelSim 的 do 文件，是 CI 进入 ModelSim 的入口。它把 run.tcl 包起来，加上错误即退出和退出 ModelSim 的逻辑。
- `scripts/ciFlow.py`：一个 Python 脚本，是 CI 的**总指挥**。它启动 ModelSim、把输出重定向到 `Transcript.transcript`，然后**独立地**读这个日志文件做两道检查，最后给出退出码。

关键设计：ciFlow.py 的检查**不依赖** run.tcl 里的 `run_check_errors` 是否令进程退出，而是自己重新读日志、自己判。这是「纵深防御（defense in depth）」——两道独立检查，任何一道发现失败，CI 都会失败。

#### 4.3.2 核心流程

整个 CI 链路是这样的：

```
ciFlow.py (scripts/)
   │
   │  1. os.chdir(../sim)
   │  2. os.system("vsim -batch -do ci.do -logfile Transcript.transcript")
   │           │
   │           ▼
   │     ci.do  (sim/)
   │       onerror {exit}      ← 任何 TCL 错误立即退出 vsim
   │       source run.tcl      ← 跑完整条 init→compile→run→check
   │       quit                ← 退出 ModelSim
   │           │
   │           ▼  (所有输出写进 Transcript.transcript)
   │
   │  3. 读取 Transcript.transcript 全文
   │
   │  4. 检查一：包含 "###ERROR###"        → exit(-1)   应用错误
   │  5. 检查二：不含 "SIMULATIONS COMPLETED SUCCESSFULLY" → exit(-2)  未正常完成
   │  6. 都通过                            → exit(0)    成功
```

**两道检查分工明确**：

| 检查 | 找什么 | 失败退出码 | 抓的是哪类问题 |
|---|---|---|---|
| 检查一 | Transcript 里**有** `###ERROR###` | `exit(-1)` | testbench 自检发现实际≠期望（应用级错误） |
| 检查二 | Transcript 里**没有** `SIMULATIONS COMPLETED SUCCESSFULLY` | `exit(-2)` | 仿真没跑到终点（编译失败、崩溃、卡死、断言失败等框架级问题） |
| 都通过 | — | `exit(0)` | 一切正常 |

关于退出码有个**操作系统细节**：进程退出码是 8 位无符号数，Python 的 `exit(-1)`、`exit(-2)` 在到达 shell/CI 时会被取模到 0–255 区间：

\[
\text{shell 看到的退出码} = n \bmod 256
\]

所以 `exit(-1)` → 255、`exit(-2)` → 254、`exit(0)` → 0。CI 服务器通常只区分「0 = 通过，非 0 = 失败」，所以 254/255 都算失败；但这两个不同的非零值能帮人在排查日志时区分「是 testbench 自检挂了（255）」还是「仿真压根没跑完（254）」。

> 关于 `SIMULATIONS COMPLETED SUCCESSFULLY` 的来源：这句话**不在** psi_tb 仓库的任何源码里（testbench 末尾只有 `wait;`，见 [testbench/psi_tb_i2c_pkg_tb.vhd:267](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L267)）。它是由 **PsiSim 框架**在所有注册的 testbench 都正常跑完后打印的成功标记。PsiSim 的源码不在本仓库内，故该标记的精确打印条件**待本地验证**——但可以确定：只要仿真中途崩溃、断言失败、或编译失败，这句标记就不会出现，检查二就会让 CI 以 `exit(-2)` 失败。这正是检查二的价值——兜住一切「没跑到终点」的异常。

#### 4.3.3 源码精读

**ci.do：CI 的 ModelSim 入口**（只有 3 行有效代码）：

- [sim/ci.do:7](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/ci.do#L7)：`onerror {exit}`——告诉 ModelSim：一旦 TCL 命令出错就立即退出，而不是继续往下跑。这样编译失败时不会继续去 `run_tb`，避免连锁报错淹没根因。
- [sim/ci.do:8](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/ci.do#L8)：`source run.tcl`——复用本讲的 run.tcl，跑完 init→compile→run→check 全流程。
- [sim/ci.do:9](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/ci.do#L9)：`quit`——退出 ModelSim，把控制权还给 Python。

> 注意 ci.do **没有**重新写一遍仿真流程，而是 `source run.tcl`。这意味着「本地手动跑」和「CI 跑」用的是**同一条** run.tcl 流程，区别只在 CI 多包了一层 `onerror {exit}` + `quit`，并由 Python 接管判定。

**ciFlow.py：CI 总指挥**：

- [scripts/ciFlow.py:6](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/scripts/ciFlow.py#L6)：`import os`——只用标准库，无需额外依赖。
- [scripts/ciFlow.py:8](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/scripts/ciFlow.py#L8)：`THIS_DIR = os.path.dirname(os.path.abspath(__file__))`——定位脚本自身所在目录（`scripts/`），使得无论从哪里调用都能找到 sim 目录。
- [scripts/ciFlow.py:10](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/scripts/ciFlow.py#L10)：`os.chdir(THIS_DIR + "/../sim")`——切换到 `sim/` 目录。因为 ci.do 与 run.tcl 里用了相对路径（`source ./config.tcl`、`../../../TCL/...`），必须在 `sim/` 下执行才正确。
- [scripts/ciFlow.py:12](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/scripts/ciFlow.py#L12)：`os.system("vsim -batch -do ci.do -logfile Transcript.transcript")`——以**批处理模式**（`-batch`，无 GUI）启动 ModelSim，执行 ci.do，并把所有输出重定向到 `Transcript.transcript`。`vsim` 必须在 PATH 里。
- [scripts/ciFlow.py:14-15](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/scripts/ciFlow.py#L14-L15)：打开 `Transcript.transcript` 并读入全文 `content`。
- [scripts/ciFlow.py:17-19](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/scripts/ciFlow.py#L17-L19)：**检查一**——`if "###ERROR###" in content: exit(-1)`。发现应用级错误，立即以 -1 退出。
- [scripts/ciFlow.py:20-22](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/scripts/ciFlow.py#L20-L22)：**检查二**——`if "SIMULATIONS COMPLETED SUCCESSFULLY" not in content: exit(-2)`。没看到成功标记（说明仿真没正常结束），以 -2 退出。
- [scripts/ciFlow.py:25](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/scripts/ciFlow.py#L25)：`exit(0)`——两道检查都过，CI 成功。

**`Transcript.transcript` 为何不入库**：

- [sim/.gitignore:12-15](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/.gitignore#L12-L15)：`*.transcript` 等被忽略。每次 CI 跑出的 `Transcript.transcript` 是临时产物，不进版本库，但 ciFlow.py 会在当次运行里读取它做判定。

#### 4.3.4 代码实践（本讲的主实践任务之一）

**实践目标**：不装任何仿真器，仅靠阅读 ciFlow.py，**预测**三种不同仿真结局下 CI 的退出码。这是验证你真的看懂了判定逻辑的最佳方式。

**操作步骤**：

1. 重新读一遍 [scripts/ciFlow.py:17-25](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/scripts/ciFlow.py#L17-L25)。
2. 针对下面三个场景，分别推断 `Transcript.transcript` 里会出现/不出现哪些标记，并写出 ciFlow.py 的退出码与 shell 看到的码值。

**三个场景**：

| 场景 | Transcript 内容 | ciFlow.py 退出码 | shell 码值 |
|---|---|---|---|
| A. 一切正常 | 含 `SIMULATIONS COMPLETED SUCCESSFULLY`，无 `###ERROR###` | ? | ? |
| B. testbench 自检失败 | 含 `###ERROR###`（也含成功标记） | ? | ? |
| C. 编译失败/仿真崩溃 | 既无 `###ERROR###`，也无成功标记 | ? | ? |

**预期结果（参考答案）**：

| 场景 | ciFlow.py 退出码 | shell 码值 | 解释 |
|---|---|---|---|
| A | `exit(0)` | 0 | 两道检查都过 |
| B | `exit(-1)` | 255 | 检查一命中（先于检查二执行） |
| C | `exit(-2)` | 254 | 检查一未命中，检查二命中 |

**需要观察的现象**：你能解释为什么场景 B 即使含成功标记也会失败——因为检查一在检查二**之前**，`exit(-1)` 一旦触发就直接结束了，根本走不到检查二。

> 真正在 CI 服务器上跑这一套需要：ModelSim（或 GHDL，但 ciFlow.py 当前写死了 `vsim`，即 ModelSim 路径）、`psi_common`、`PsiSim` 三者都就位。本环境未检出这三者，故实跑**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：ciFlow.py 已经在检查二里兜底了「仿真没正常完成」，为什么 run.tcl 末尾还要再写一句 `run_check_errors "###ERROR###"`？两者不是重复吗？

**答案**：不完全重复，作用层次不同。run.tcl 里的 `run_check_errors` 是**本地/即时**的检查，让你手动跑 run.tcl 时就能立刻在 Transcript 看到「发现 N 处 `###ERROR###`」的提示，方便调试；而 ciFlow.py 的检查一是**CI/事后**的检查，读落盘的 `Transcript.transcript` 给退出码。即便 PsiSim 的 `run_check_errors` 没有令进程非零退出，ciFlow.py 也能独立兜住。两者是纵深防御，不是冗余浪费。

**练习 2**：ciFlow.py 第 12 行用的是 `os.system(...)` 而不是 `subprocess.run(...)`。这会带来什么潜在问题？

**答案**：`os.system` 只返回退出状态码、不捕获子进程的标准输出（这里靠 `-logfile` 把输出写进文件来补偿），且它在不同平台上会经过 shell 解释，参数里的特殊字符可能被 shell 误读。对于当前这条无特殊字符的 `vsim` 命令尚可工作，但更健壮的写法是用 `subprocess.run([...], capture_output=True)`。这是一个可改进点，**待确认**项目是否在更新版本里调整过。

**练习 3**：如果某次 CI 失败，退出码是 254（即 `exit(-2)`），你最应该先去 Transcript 里找什么？

**答案**：找编译错误、`assert ... severity error/failure`、或仿真卡死/崩溃的迹象——因为 254 对应「没看到 `SIMULATIONS COMPLETED SUCCESSFULLY`」，说明仿真没跑到终点，问题出在框架/编译/运行阶段，而不是 testbench 的数值自检（那会是 255）。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个**端到端**任务。

### 实践目标

把 `psi_tb_i2c_pkg_tb` 仿真跑通（条件允许时），并用本讲授的「双重检查」逻辑解释你看到的结果。若本机不具备仿真条件，则完成「源码阅读型」替代任务。

### 操作步骤

**A. 源码阅读型（任何环境都能做，必做）**

1. 按「文件」顺序，口述一次 CI 的完整调用链：`ciFlow.py` 启动 → `ci.do` → `run.tcl` → `config.tcl` → `compile_files` → `run_tb` → 回到 `ciFlow.py` 读 Transcript。在每一步旁边标注它由哪个文件、哪一行触发。
2. 用第 4.3.4 节的三场景表，自测一遍退出码推断。
3. 回答：为什么 CI 的成功判定**同时**需要「没有 `###ERROR###`」**和**「有 `SIMULATIONS COMPLETED SUCCESSFULLY`」两个条件，缺一不可？

**B. 实跑型（需 ModelSim + psi_common + PsiSim，待本地验证）**

1. 按 README 的 Dependencies 章节摆好目录结构（`psi_tb`、`psi_common`、`TCL/PsiSim` 同级），或直接用 `psi_fpga_all` 仓库。
2. 进入 `psi_tb/sim/`，执行 ModelSim：`vsim -do run.tcl`（等价于本地手动跑）。
3. 在 Transcript 里确认依次出现 `-- Compile`、`-- Run`、`-- Check` 三段分节标题。
4. 确认看到 `SIMULATIONS COMPLETED SUCCESSFULLY`（由 PsiSim 在 TB 跑完后打印）。
5. 确认**没有**任何 `###ERROR###` 行。
6. （可选）再跑一次 CI 路径：在仓库根执行 `python scripts/ciFlow.py`，检查其退出码是否为 0（用 `echo $?` 查看）。

### 需要观察的现象

- Transcript 干净（因为 `compile_suppress`/`run_suppress` 屏蔽了噪音编号），能清楚看到 testbench 里那些 `print(">> Addressing")` 之类的分节文字（见 [testbench/psi_tb_i2c_pkg_tb.vhd:44](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L44) 等）。
- 仿真末尾出现成功标记，无 `###ERROR###`。
- ciFlow.py 的退出码为 0。

### 预期结果

- 阅读型：能画出完整的 CI 调用链图，并正确解释「两道检查缺一不可」——前者抓 testbench 自检失败，后者抓仿真没跑完的框架级异常。
- 实跑型：Transcript 以 `SIMULATIONS COMPLETED SUCCESSFULLY` 收尾，CI 退出码 0。若你看到的与预期不符，以本机实际 Transcript 为准，并据此回头核对讲义。

> 说明：本环境未安装 ModelSim/GHDL，也未检出 `psi_common` 与 `PsiSim`，故实跑步骤**待本地验证**。阅读型任务已覆盖本讲全部学习目标。

## 6. 本讲小结

- psi_tb 的本地仿真由 `sim/run.tcl`（ModelSim）或 `sim/runGhdl.tcl`（GHDL）驱动，两者只差一个 `init` vs `init -ghdl`，其余流程逐字相同：`init → source config.tcl → compile_files -all -clean → run_tb -all → run_check_errors`。
- run.tcl 只描述「流程」，config.tcl 只描述「内容」（文件清单、TB 注册、消息抑制），二者通过 `source ./config.tcl` 衔接；`interactive.tcl` 则只编译不运行，供 GUI 调试。
- config.tcl 用 `compile_suppress` / `run_suppress` 按 ModelSim 消息编号屏蔽噪音；用 `create_tb_run` + `add_tb_run` 注册要跑的 testbench（当前仅 `psi_tb_i2c_pkg_tb`）。
- `###ERROR###` 是 psi_tb 全库统一的错误前缀，由比较检查等过程在自检失败时打印；run.tcl 末尾的 `run_check_errors "###ERROR###"` 在 Transcript 里扫描它。
- CI 由 `sim/ci.do`（`onerror {exit}` + `source run.tcl` + `quit`）与 `scripts/ciFlow.py` 配合：后者启动 vsim、读 `Transcript.transcript`，做两道独立检查。
- ciFlow.py 的退出码语义：发现 `###ERROR###` → `exit(-1)`（shell 255，应用错误）；没看到 `SIMULATIONS COMPLETED SUCCESSFULLY` → `exit(-2)`（shell 254，仿真未完成）；都通过 → `exit(0)`。两道检查分别抓「自检失败」与「没跑完」两类问题，缺一不可。

## 7. 下一步学习建议

至此，第一单元（u1）三讲完成：你已了解 psi_tb 的定位（u1-l1）、仓库结构（u1-l2）、仿真与 CI 流程（本讲）。接下来的建议：

- **第二单元 u2（psi_tb_txt_util）**：从库的公共底座开始读源码。`print`、`str`、`hstr` 这些函数正是本讲 Transcript 里所有输出的源头，也是 `###ERROR###` 消息拼接的基础。
- **第三单元 u3（psi_tb_compare_pkg）**：专门讲「`###ERROR###` 是怎么被拼出来的」——各种 Compare 过程如何比较期望值与实际值、如何带容差、如何输出统一前缀的可读错误。读完你就能完整理解本讲的检查一。
- **第八单元 u8-l1**：若你想更深入 PsiSim 的 `compile_suppress`/`run_suppress` 取舍、ModelSim 与 GHDL 路径差异、以及 `dependencies.py` 如何从 README 解析依赖，那是 CI 主题的进阶讲义。

如果你想在进入 u2 前再巩固本讲，最好的练习是：合上讲义，凭记忆向同事画出「ciFlow.py → ci.do → run.tcl → config.tcl」这条链，并说出 `###ERROR###` 与 `SIMULATIONS COMPLETED SUCCESSFULLY` 各自负责抓哪类失败。
