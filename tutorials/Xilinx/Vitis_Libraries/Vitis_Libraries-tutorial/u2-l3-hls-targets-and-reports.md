# HLS TARGET 流程与综合报告解读

## 1. 本讲目标

上一讲你已经用 `make run TARGET=csim` 跑通了 `stream_dup` 用例并看到了 `PASS`。但 `csim` 只是 HLS（High Level Synthesis，高层综合）流程里最轻量的一档——它只是把 C++ 当普通程序编译运行，根本不产生任何硬件信息。

本讲要回答三个问题：

1. Makefile 里那个 `TARGET` 除了 `csim` 还能取哪些值？每一档到底做了什么、产出什么、要付出多大代价？
2. 一句 `make run TARGET=csynth` 是怎么被 Makefile「翻译」成底层工具 `v++` / `vitis-run` 的实际调用的？
3. 综合跑完后，如何在报告里读懂 **II（Initiation Interval，启动间隔）**、**latency（延迟）** 和 **资源利用率（BRAM/DSP/FF/LUT）**？

学完本讲，你应该能：自主选择合适的 `TARGET`、说清 Makefile 的分发逻辑、并能打开 `csynth` 报告读懂内核的时序与资源画像。

## 2. 前置知识

- **HLS（高层综合）**：把 C/C++ 描述的算法自动翻译成 RTL（Verilog/VHDL）的工具流程。Vitis 里的入口是 `v++ --mode hls` 或 `vitis-run --mode hls`。
- **DUT（Design Under Test，待测设计）**：用 `extern "C"` 包起来、会被综合成硬件的顶层函数。在 `stream_dup` 里它叫 `dut0`，内部调用模板原语 `streamDup`。
- **csim**：C 仿真，把 testbench 和 DUT 当普通 C++ 编译运行，只验功能、不综合。这是上一讲的内容。
- **顶层函数（top function）**：综合的「入口」。HLS 只会把 top 函数及其调用的子函数翻译成 RTL，testbench 里的 `main` 不会进硬件。
- **大写 TARGET 与小写 target 的区别**（来自 u1-l3）：L1 的 HLS 流程用五个**大写** `TARGET`（`csim/csynth/cosim/vivado_syn/vivado_impl`）；L2/L3 的 Vitis 流程用**小写** `target`（`sw_emu/hw_emu/hw`）。两套流程不要混淆，本讲只讲前者。

如果你对 `hls::stream`、end-flag 流、`stream_dup` 的功能还不熟，请先复习 u2-l2。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [utils/L1/tests/stream_dup/Makefile](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile) | 用例的驱动入口：把 `make run TARGET=...` 翻译成 `v++` / `vitis-run` 调用，是本讲的核心。 |
| [utils/L1/tests/stream_dup/hls_config.tmpl](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/hls_config.tmpl) | HLS 配置文件模板，声明 top 函数、时钟、源文件；里面 `${VIVADO_FLOW}` 占位符会被环境变量替换。 |
| [utils/L1/tests/stream_dup/description.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/description.json) | 用例元数据，记录每个 `TARGET` 的耗时/内存上限——是「代价递增」的硬证据。 |
| [utils/L1/tests/stream_dup/run_hls.tcl](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/run_hls.tcl) | 另一条 Tcl 驱动流程，把五个 `TARGET` 一一映射到 `csim_design/csynth_design/cosim_design/export_design`，是理解每档 TARGET「底层在干什么」的最佳参照。 |
| [utils/L1/include/xf_utils_hw/stream_dup.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_dup.hpp) | 被综合的算法原语，里面的 `#pragma HLS pipeline II = 1` / `unroll` 决定了报告中的 II 与资源数字。 |
| [utils/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/README.md) / [blas/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/README.md) | 官方对五个 TARGET 的说明，并（blas）对照展示了 L2/L3 的小写 target。 |

---

## 4. 核心概念与源码讲解

### 4.1 五个 TARGET：含义、产物与代价

#### 4.1.1 概念说明

HLS 把「验证一个 C++ 内核能不能变成好硬件」拆成了**保真度逐级递增、代价也逐级递增**的五个阶段。你可以把它想成一条「从便宜到昂贵」的阶梯：先在最便宜的档确认功能对，再逐级向上确认时序对、资源装得下、最终能在真实芯片上跑。

这五个阶段就是五个 `TARGET`：`csim` → `csynth` → `cosim` → `vivado_syn` → `vivado_impl`。每往上一档，工具就离「真实硅片行为」更近一步，但耗时也更长。一个成熟的开发节奏是：日常开发只用 `csim` + `csynth`（分钟级），只在里程碑时才跑 `cosim`/`vivado_syn`/`vivado_impl`（小时级）。

#### 4.1.2 核心流程

五个 TARGET 沿「C 源码 → RTL → 网表 → 布局布线」的链条逐级深入：

```
test.cpp (C/C++)
   │
   ├── csim        纯 C++ 编译运行，验功能        → 控制台 PASS/FAIL
   │
   ├── csynth      HLS 高层综合 C→RTL，做调度绑定 → RTL + 综合报告(II/latency/资源)
   │
   ├── cosim       软件testbench 驱动RTL 周期级仿真 → 仿真日志/波形 + 协同仿真报告
   │
   ├── vivado_syn  把RTL交给Vivado做RTL综合        → 网表 + 真实资源/时序估计
   │
   └── vivado_impl Vivado布局布线                  → 最终资源/时序、利用率报告
```

关键直觉：

- **csim 不综合**，所以它最快、也不产生任何硬件报告——这就是上一讲几秒就跑完的原因。
- **csynth 是第一个产生硬件信息的档**：II、latency、资源估计都在这里首次出现。**本讲最关心的报告就来自 csynth。**
- **cosim** 才真正用 RTL 跑周期级仿真，能抓出「C 看起来对、但硬件时序不对」的 bug。
- **vivado_syn / vivado_impl** 已经离开 HLS、进入 Vivado 的地盘，数字最接近真实上板结果，但耗时也最长。

#### 4.1.3 源码精读

五个 TARGET 的取值首先写在 Makefile 的帮助文本里：

> [utils/L1/tests/stream_dup/Makefile:23-26](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L23-L26) 告诉用户：`make run TARGET=<cosim/csim/csynth/vivado_syn/vivado_impl>`，并列出合法任务。

`utils/README.md` 把每一档的含义说得很清楚，且点明了报告所在目录：

> [utils/README.md:71-77](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/README.md#L71-L77) 枚举五个 TARGET：csim（高层仿真）、csynth（高层综合到 RTL）、cosim（软件 testbench 与生成 RTL 间的协同仿真）、vivado_syn（Vivado 综合）、vivado_impl（Vivado 实现）。

> [utils/README.md:92-94](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/README.md#L92-L94) 说明：流程产生的报告会给出逻辑利用率、时序、延迟与吞吐，相关输出文件位于路径名为 `test.prj` 的测试工程下。这句直接指明了「去哪里找报告」。

「代价递增」不是空话——`description.json` 给每个 TARGET 标了硬性的时间/内存上限：

> [utils/L1/tests/stream_dup/description.json:42-54](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/description.json#L42-L54) 记录 `max_time_min`：`hls_csim` 与 `hls_csynth` 限 **60 分钟**，而 `hls_cosim`、`vivado_syn`、`vivado_impl` 都限 **420 分钟**。这正是「csim/csynth 是日常档、后三档是重型档」的官方量化。

如果想看每档「底层到底调了哪个 HLS 命令」，最直观的参照是 `run_hls.tcl`（这是与 Makefile 平行的另一条 Tcl 驱动流程，五个阶段一一对应）：

> [utils/L1/tests/stream_dup/run_hls.tcl:46-64](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/run_hls.tcl#L46-L64) 用 `if {$CSIM == 1}` 等开关分别调用 `csim_design`、`csynth_design`、`cosim_design`、`export_design -flow syn`、`export_design -flow impl`。这段 Tcl 就是五个 TARGET 的「真身」：`csynth` 对应 `csynth_design`，`vivado_syn`/`vivado_impl` 对应两次 `export_design`（flow 分别为 `syn`/`impl`）。

> [utils/L1/tests/stream_dup/run_hls.tcl:24-44](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/run_hls.tcl#L24-L44) 设定 part 为 `xcu200-fsgd2104-2-e`、工程名 `test.prj`、solution `solution1`、时钟周期 `2.5ns`——这些就是后续报告文件的目录骨架。

最后，对照 `blas/README.md` 可以巩固「大写 TARGET 属于 HLS 流程、小写 target 属于 Vitis 流程」这条边界：

> [blas/README.md:87-94](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/README.md#L87-L94) 同样列出五个大写 TARGET，明确它们是 **HLS Cases**（只在 `L1/tests`）的命令行流程。

> [blas/README.md:121-124](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/README.md#L121-L124) 而 L2/L3 的 Vitis Cases 用小写 `hw_emu`（硬件仿真）/`hw`（上板），并提醒「编译成 hw 往往需要数小时」。这正是 u1-l3 强调的两套流程之分。

#### 4.1.4 代码实践

**实践目标**：不跑任何重型命令，仅靠源码读懂五个 TARGET 的代价排序。

**操作步骤**：

1. 打开 `utils/L1/tests/stream_dup/description.json`，定位 `testinfo.jobs[0].max_time_min`。
2. 把五个 TARGET 按其时间上限排成两档：「60 分钟档」与「420 分钟档」。
3. 打开 `run_hls.tcl`，把每个 TARGET 名字对应到它实际调用的那条 `*_design` / `export_design` 命令。

**需要观察的现象**：`csim`、`csynth` 同属轻量档；`cosim`、`vivado_syn`、`vivado_impl` 同属重型档。

**预期结果**：60 分钟档 = {csim, csynth}；420 分钟档 = {cosim, vivado_syn, vivado_impl}。对应关系：csim→`csim_design`、csynth→`csynth_design`、cosim→`cosim_design`、vivado_syn→`export_design -flow syn`、vivado_impl→`export_design -flow impl`。

（本实践是纯源码阅读，不依赖工具链，可在任何环境完成。）

#### 4.1.5 小练习与答案

**练习 1**：为什么日常开发推荐用 `csim` + `csynth`，而不是直接跑 `vivado_impl`？

> **答案**：`csim`/`csynth` 是分钟级（60 分钟上限），能快速验证功能与初步的时序/资源；`vivado_impl` 要做完整布局布线，属于 420 分钟级的重型流程，只适合在里程碑时做最终确认。

**练习 2**：`cosim` 比 `csynth` 多做了什么事？为什么需要它？

> **答案**：`csynth` 只把 C 综合成 RTL 并估计时序/资源，并不真正运行 RTL；`cosim` 用软件 testbench 去驱动生成的 RTL 做周期级仿真，能发现「C 语义正确但硬件时序/握手不对」的问题。代价是它属于 420 分钟档。

---

### 4.2 从 hls_config.tmpl 到 vitis-run：Makefile 的翻译机制

#### 4.2.1 概念说明

`make run TARGET=csynth` 这一句话背后，Makefile 做了两件事：**生成配置文件**，再**分发到正确的工具命令**。

Vitis HLS 的新流程（`v++ --mode hls`）用一个 `.cfg` 配置文件来描述「综合哪个 top、时钟多快、源文件和 cflags 是什么、Vivado flow 走 syn 还是 impl」。但仓库里存的是带占位符的**模板** `hls_config.tmpl`，真正的 `hls_config.cfg` 是在 make 时由一段内嵌 Python 脚本把环境变量替换进去后生成的。理解这条「模板 → 配置 → 工具调用」的链路，是看懂任何 L1 用例构建的钥匙。

#### 4.2.2 核心流程

```
make run TARGET=<X>
        │
        ├─① check_vivado / check_vpp / check_part   环境与平台检查
        │
        ├─② 生成 hls_config.cfg
        │      hls_config.tmpl  ──(Python string.Template 替换 ${XF_PROJ_ROOT} / ${VIVADO_FLOW})──▶  hls_config.cfg
        │
        ├─③ 目标 all：
        │      若 TARGET_REL != csim  →  v++ -c --mode hls --config hls_config.cfg --work_dir hls --part <XPART>
        │
        └─④ 目标 run：
               若 TARGET_REL != csynth →  vitis-run --mode hls --config hls_config.cfg --<TARGET_REL> --work_dir hls --part <XPART>
```

这里有三个关键变量（都由 Makefile 根据 `TARGET` 推导）：

- `TARGET_REL`：传给 `vitis-run` 的 `--<TARGET_REL>` 参数。注意 `vivado_syn` 和 `vivado_impl` 都被映射成 `impl`，二者真正的区别在下一个变量。
- `VIVADO_FLOW`：写入配置文件的 `vivado.flow` 字段，取值 `syn`（对应 vivado_syn）或 `impl`（对应 vivado_impl）。
- `XPART`：目标 FPGA part，由 `PLATFORM` 经 `platforminfo` 反查得到，或直接由 `XPART` 变量指定。

最巧妙的设计在 ③ 和 ④ 的两个条件判断：它们让五个 TARGET 走**不同的命令组合**。

#### 4.2.3 源码精读

先看模板本身。`hls_config.tmpl` 声明了综合所需的全部要素：

> [utils/L1/tests/stream_dup/hls_config.tmpl:1-9](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/hls_config.tmpl#L1-L9) 设定 `clock=2.5`（ns）、`flow_target=vivado`、综合源文件 `test.cpp`（带 `-I${XF_PROJ_ROOT}/L1/include`）、**top 函数 `dut0`**。这里 `${XF_PROJ_ROOT}` 就是待替换的占位符之一。

> [utils/L1/tests/stream_dup/hls_config.tmpl:11-17](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/hls_config.tmpl#L11-L17) 设定 `csim.argv=0`、`cosim.argv=0`（把 argv[1] 传成 "0"，于是 test.cpp 的 main 跑 `test_dut0`），并把 `vivado.flow` 设成 `${VIVADO_FLOW}`——这个占位符就是 vivado_syn 与 vivado_impl 的分叉点。

再看 Makefile 怎么推导 `TARGET_REL` 与 `VIVADO_FLOW`：

> [utils/L1/tests/stream_dup/Makefile:62-72](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L62-L72) 核心分派逻辑：`vivado_syn` → `TARGET_REL=impl` 且 `VIVADO_FLOW=syn`；`vivado_impl` → `TARGET_REL=impl` 且 `VIVADO_FLOW=impl`；其余 TARGET → `TARGET_REL=自身`、`VIVADO_FLOW=impl`（此时 vivado.flow 不被使用）。

然后是「模板 → 配置」的生成规则。Makefile 用一段内嵌 Python（`string.Template`）把环境变量替换进模板：

> [utils/L1/tests/stream_dup/Makefile:160-167](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L160-L167) 定义 `CONFIG_GEN_PY`：读取 `hls_config.tmpl`，用 `string.Template(...).substitute(**os.environ)` 把所有 `${VAR}` 替换成同名环境变量的值（包括上面 export 的 `VIVADO_FLOW` 和 `XF_PROJ_ROOT`），写入 `hls_config.cfg`。注意它跑的是 Vitis 自带的 Python（`TAPYTHON`，见 [Makefile:169-170](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L169-L170)）。

> [utils/L1/tests/stream_dup/Makefile:172-173](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L172-L173) 生成规则：`hls_config.cfg` 依赖 `hls_config.tmpl`，通过管道把 `CONFIG_GEN_PY` 喂给 Vitis 的 python3 执行。

最后是两个真正调用工具的目标：

> [utils/L1/tests/stream_dup/Makefile:178-181](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L178-L181) 目标 `all`：**只有当 `TARGET_REL != csim` 时**才运行 `v++ -c --mode hls --config $(CONFIG_FILE) --work_dir $(WORK_DIR) --part $(XPART)`。这一步会创建 HLS 工程并完成高层综合。

> [utils/L1/tests/stream_dup/Makefile:183-187](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L183-L187) 目标 `run`：**只有当 `TARGET_REL != csynth` 时**才运行 `vitis-run --mode hls --config $(CONFIG_FILE) --$(TARGET_REL) --work_dir $(WORK_DIR) --part $(XPART)`。

把这两条规则与 4.2.2 的推导结合，就得到一张完整的分派表（**本讲最重要的结论之一**）：

| TARGET | TARGET_REL | 是否跑 `v++ -c` | 是否跑 `vitis-run` | 为什么 |
|---|---|---|---|---|
| `csim` | csim | **否** | 是（`--csim`） | 纯 C 仿真，无需建 HLS 工程 |
| `csynth` | csynth | 是 | **否** | `v++ -c` 本身就完成综合并产出报告 |
| `cosim` | cosim | 是 | 是（`--cosim`） | 先综合出 RTL，再做周期级仿真 |
| `vivado_syn` | impl | 是 | 是（`--impl`，`flow=syn`） | 综合后交 Vivado 做 RTL 综合 |
| `vivado_impl` | impl | 是 | 是（`--impl`，`flow=impl`） | 综合后交 Vivado 做布局布线 |

> [utils/L1/tests/stream_dup/Makefile:189-190](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L189-L190) `clean` 目标删除 `hls_config.cfg`、`*_hls.log` 与工作目录 `hls/`（即 `WORK_DIR`）——报告就长在这个工作目录里。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「模板里的占位符被替换成真实值」的过程。

**操作步骤**：

1. 进入 `utils/L1/tests/stream_dup/`，确认 `hls_config.cfg` 还不存在（`clean` 后的干净状态）。
2. 执行 `make run TARGET=csynth`（需要工具链；若无工具链，改为阅读下面的预期结果）。
3. 综合结束后，用文本查看器打开刚生成的 `hls_config.cfg`，与 `hls_config.tmpl` 逐行对比。
4. 重新 `make clean`，再分别执行 `make run TARGET=vivado_syn` 与 `make run TARGET=vivado_impl` 前，提前 `export VIVADO_FLOW` 的值你猜会是 `syn` 还是 `impl`，然后检查生成的 `hls_config.cfg` 里 `vivado.flow` 那一行验证。

**需要观察的现象**：模板里的 `${XF_PROJ_ROOT}` 变成了库根目录的绝对路径；`vivado.flow=${VIVADO_FLOW}` 这一行在 `vivado_syn` 时变成 `vivado.flow=syn`，在 `vivado_impl` 时变成 `vivado.flow=impl`。

**预期结果**：`csynth` 时 `vivado.flow=impl`（csynth 不使用该字段，但默认值被写入，无害）；`vivado_syn` 时为 `syn`；`vivado_impl` 时为 `impl`。

> 若本地没有 Vitis 工具链，**待本地验证**上述文件内容差异；理解替换机制本身不依赖运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `csim` 时 Makefile **不**调用 `v++ -c`？

> **答案**：`csim` 只是把 testbench 当普通 C++ 编译运行来验证功能，根本不需要创建 HLS 工程或做综合。`v++ -c` 是用来建工程并综合的，对 csim 是多余开销，故用 `ifneq ($(TARGET_REL), csim)` 跳过。

**练习 2**：`vivado_syn` 和 `vivado_impl` 的 `TARGET_REL` 都是 `impl`，Makefile 靠什么区分这两档？

> **答案**：靠 `VIVADO_FLOW` 变量。它被写进 `hls_config.cfg` 的 `vivado.flow` 字段（`syn` 或 `impl`），底层 `export_design -flow syn|impl` 据此决定只做综合还是一路做到布局布线。参见 run_hls.tcl 的两次 `export_design`。

**练习 3**：`hls_config.tmpl` 里的 `syn.top=dut0` 决定了什么？

> **答案**：它告诉 HLS 把 `dut0` 作为顶层函数综合——只有 `dut0` 及其调用的 `streamDup` 会被翻译成 RTL，而 `main`/testbench 不会进硬件。报告文件名也会以 top 名命名（见 4.3）。

---

### 4.3 综合报告精读：II、latency 与资源利用率

#### 4.3.1 概念说明

跑完 `csynth`，HLS 会产出一份综合报告，用三类数字刻画这个内核「作为硬件长什么样」：

- **II（Initiation Interval，启动间隔）**：内核（或某循环）能以多快的频率接受新输入。II=1 表示每个时钟周期都能吞一个新输入——这是流式内核追求的目标。
- **Latency（延迟）**：从第一个输入被接受到最后一个输出产生，总共花了多少个时钟周期。延迟关心「一次处理要多久」，II 关心「能多密集地重复处理」。
- **资源利用率（Utilization）**：综合出的电路要占用多少 FPGA 资源，主要是 **BRAM**（块存储）、**DSP**（乘加单元）、**FF**（触发器）、**LUT**（查找表），某些器件还有 **URAM**。

一句话区分：**II 决定吞吐，latency 决定单次延迟，资源决定「装不装得下」。**

#### 4.3.2 核心流程

对一个**流水线化的循环**（`streamDup` 主循环就是），II 与 latency 的关系是：

\[
\text{吞吐（样本/周期）} \approx \frac{1}{\text{II}}
\]

\[
\text{循环总延迟} \approx \text{单次迭代延迟} + (\text{循环次数} - 1) \times \text{II}
\]

直观理解：II=1 意味着新数据每周期都能进场，多条数据像流水线一样交叠处理，所以吞吐最高；II 越大，相邻两次启动间隔越远，吞吐越低。而 latency 的「\((\text{循环次数}-1)\times\text{II}\)」项正是流水线交叠带来的吞吐收益的体现。

读报告时按这个顺序看：

1. 先看 **top 函数的达成 II** 是否等于目标 II（流式内核通常目标 II=1）。
2. 再看各 **循环的 trip count（循环次数）、迭代延迟、达成 II**，定位哪个循环拖慢了吞吐。
3. 最后看 **资源利用率**：BRAM/DSP/FF/LUT 各占多少、占器件总量的百分比，判断是否塞得下、有没有超标。

#### 4.3.3 源码精读

报告里的 II 与资源数字，根源在被综合的源码及其 pragma。`streamDup` 的核心实现：

> [utils/L1/include/xf_utils_hw/stream_dup.hpp:87-108](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_dup.hpp#L87-L108) 主循环 `while (!e)` 上写着 `#pragma HLS pipeline II = 1`，要求 HLS 把该循环流水线化、目标 II=1；内层 `for (int i = 0; i < _NStrm; i++)` 写着 `#pragma HLS unroll`，要求把「写到 _NStrm 个输出流」的循环完全展开成并行写。

在 testbench 里，这个模板被实例化为 `streamDup<TYPE, NUM_COPY>`，其中 `TYPE=uint32_t`、`NUM_COPY=16`：

> [utils/L1/tests/stream_dup/test.cpp:28-38](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp#L28-L38) 定义 `NUM_COPY 16`，并把 `dut0` 封装为调用 `streamDup<TYPE, NUM_COPY>` 的 `extern "C"` 顶层函数。于是综合目标是「把 1 路输入复制成 16 路输出」的硬件。

由此可以**预判**报告长什么样（实际数值以本地综合为准）：

| 报告字段 | 预判 | 依据 |
|---|---|---|
| 达成 II | 应为 **1** | `#pragma HLS pipeline II = 1`，且纯流式复制无反馈/依赖，通常能满足 |
| BRAM | 预计 **0 或极少** | 纯流式 pass-through，无大数组；`hls::stream` 多实现为握手/FIFO，不一定占用块存储 |
| DSP | 预计 **0** | 全程只是复制数据，没有乘法/加法，不触发 DSP48 |
| FF / LUT | 与流接口数量成正比 | 共 1 入 + 16 出数据流 + 17 个 end-flag 流，每个 32 位 + 握手信号都要 FF/LUT |

> 上表是**基于源码的预判**，真实数值取决于 Vitis 版本、目标 part 与综合策略，**待本地验证**。

报告文件的位置：top 函数是 `dut0`，所以 csynth 报告文件名以 top 命名，形如 `dut0_csynth.rpt`，长在 Makefile 设定的工作目录 `hls/`（即 `WORK_DIR`，见 [Makefile:53](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L53)）下的工程目录里，典型路径形如：

```
hls/<工程目录>/solution1/syn/report/dut0_csynth.rpt
```

`utils/README.md` 把这个工程目录笼统称为 `test.prj`：

> [utils/README.md:92-94](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/README.md#L92-L94) 报告（逻辑利用率、时序、延迟、吞吐）位于路径名含 `test.prj` 的测试工程下。

> 不同 Vitis 版本下，`v++ --mode hls` 与 Tcl 流的工程目录命名略有差异。若找不到，在 `hls/` 下搜索 `*_csynth.rpt` 即可定位（**待本地确认**确切目录名）。

#### 4.3.4 代码实践

**实践目标**：亲自跑出一份 csynth 报告，并从中读出 II 与资源。

**操作步骤**：

1. `cd utils/L1/tests/stream_dup/`
2. `make run TARGET=csynth`（若上一讲已 `make clean`，先确保环境已 source；csynth 属 60 分钟档，通常远快于此）。
3. 在 `hls/` 下找到 `dut0_csynth.rpt`（或 `*_csynth.rpt`），打开它。
4. 在报告里定位三类信息并记录：
   - **Performance & Resource Estimates** 区块里的 **Achieved II**（对照 Target II）；
   - **Latency** 区块里 top 函数与主循环的延迟（周期数）；
   - **Utilization Estimates** 里的 **BRAM_18K / DSP / FF / LUT** 估计值。
5. 把读到的数值与 4.3.3 的预判表对照。

**需要观察的现象**：Achieved II 应为 1；BRAM 与 DSP 应为 0 或接近 0；FF/LUT 为非零且与 16 路输出正相关。

**预期结果**：II=1、BRAM≈0、DSP=0、FF/LUT 为较小非零值（具体数字 **待本地验证**）。若 Achieved II > 1，说明流水线未达成目标，需回头检查数据依赖或 pragma。

> 若本地没有 Vitis 工具链无法运行，可改为阅读任一已存在的 `*_csynth.rpt` 样例（如有），重点练习「在报告里定位 II/Latency/Utilization 三个区块」这项技能。

#### 4.3.5 小练习与答案

**练习 1**：某内核报告显示 Target II=1 但 Achieved II=3，这对吞吐意味着什么？

> **答案**：吞吐降为原本的 \(1/3\)。因为吞吐 \(\approx 1/\text{II}\)，II 从 1 变 3，单位周期能处理的样本数从 1 降到约 0.33。通常要检查循环里是否有数据依赖、内存端口冲突或资源瓶颈。

**练习 2**：`streamDup` 为什么预计不占用 DSP？

> **答案**：DSP48 单元用于乘加运算，而 `streamDup` 只做数据复制（读一个值、写到多个输出流），没有任何乘法/加法，所以综合器不会映射出 DSP。占用主要是 FF/LUT（寄存器与流握手逻辑）。

**练习 3**：如果把 `NUM_COPY` 从 16 改大（例如 32），报告里哪类资源会明显增长？II 会变吗？

> **答案**：FF 与 LUT 会明显增长，因为内层 `unroll` 的写循环要并行驱动更多输出流，每路都要数据寄存器与握手信号。只要仍满足 II=1 的时序约束，II 不一定变；但若资源/布线压力导致频率掉得太多，可能间接迫使 II 增大——这正是「面积换吞吐」要权衡之处（待本地验证）。

---

## 5. 综合实践

把本讲三节串起来，完成一次「从命令到报告」的完整闭环。以 `stream_dup` 为对象：

1. **选档与预测**：阅读 `description.json` 的 `max_time_min`，写出五个 TARGET 的代价排序；预测 `csynth` 后报告里 `streamDup` 的 II 与 BRAM/DSP 是否为 0。
2. **跑通 csim**（复现上一讲）：`make run TARGET=csim`，确认 `PASS`。这是功能基线。
3. **跑通 csynth**：`make run TARGET=csynth`。结合 4.2 的分派表，说清这次 Makefile 调了 `v++ -c` 还是 `vitis-run`、为什么。
4. **读报告**：在 `hls/` 下打开 `dut0_csynth.rpt`，记录 `streamDup` 的 **Achieved II** 与 **BRAM/LUT/FF** 估计值，填入下表（数值待本地验证）：

   | 指标 | 你的预测 | 报告实测 |
   |---|---|---|
   | Achieved II | 1 | ____ |
   | BRAM | 0 | ____ |
   | DSP | 0 | ____ |
   | FF | 较小非零 | ____ |
   | LUT | 较小非零 | ____ |

5. **反思**：若实测 II≠1 或资源与预测出入较大，回到 `stream_dup.hpp` 的 pragma 与 `test.cpp` 的 `NUM_COPY`，用 4.3.2 的公式解释原因。

> 这个任务覆盖了本讲全部三个最小模块：TARGET 选档（4.1）、Makefile 分派（4.2）、报告解读（4.3）。若无工具链，步骤 2-4 的「运行」部分标注 **待本地验证**，但步骤 1 与 5 的分析可独立完成。

## 6. 本讲小结

- HLS 的五个大写 `TARGET`（`csim/csynth/cosim/vivado_syn/vivado_impl`）是保真度与代价逐级递增的阶梯：`csim`/`csynth` 是 60 分钟级的日常档，`cosim`/`vivado_syn`/`vivado_impl` 是 420 分钟级的重型档（`description.json` 的 `max_time_min` 是硬证据）。
- `csim` 只验功能、不综合；**`csynth` 是第一个产出硬件报告（II/latency/资源）的档**，本讲重点即在此。
- Makefile 用一段内嵌 Python 把 `hls_config.tmpl` 里的 `${VAR}` 占位符替换成环境变量，生成 `hls_config.cfg`；其中 `${VIVADO_FLOW}` 是 `vivado_syn` 与 `vivado_impl` 的分叉点。
- 一句 `make run TARGET=X` 被两个条件判断分派：`csim` 只跑 `vitis-run --csim`、`csynth` 只跑 `v++ -c`，其余三档两者都跑（`vivado_syn`/`vivado_impl` 共用 `--impl` 但 `flow` 不同）。
- 读 csynth 报告按「II → latency → 资源」顺序：II 决定吞吐、latency 决定单次延迟、BRAM/DSP/FF/LUT 决定装不装得下；报告文件以 top 函数命名（`dut0_csynth.rpt`），长在 `hls/` 工作目录下。
- `streamDup` 因 `#pragma HLS pipeline II = 1` 预计达成 II=1，且因纯复制无算术预计 DSP=0、BRAM≈0——这些是「待本地验证」的预判，不是已测数字。

## 7. 下一步学习建议

- **横向打通 HLS pragma**：本讲只点了 `pipeline II=` 和 `unroll`。下一讲 u3-l2「HLS pragma 如何映射硬件」会系统讲解 `pipeline/unroll/dataflow` 如何决定吞吐、并行度与面积，并把「改 II 看报告变化」做成可重复实验。
- **进入主机侧**：csynth 只告诉你「内核硬件长什么样」；要让内核真正上板，还需要主机程序通过 XRT/OpenCL 喂数据。u4 单元（从 `xcl2` 到原生 XRT API）是下一步。
- **对比 L2/L3 流程**：本讲的五个大写 TARGET 属 L1 HLS 流程；u5-l1 会讲 L2/L3 的 `v++ -c/-l/--package` 三段式与小写 `sw_emu/hw_emu/hw` 流程，正好和本讲形成「两套流程」的完整对照。
- **建议阅读的源码**：想更扎实地理解每档 TARGET 的底层命令，精读 [utils/L1/tests/stream_dup/run_hls.tcl](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/run_hls.tcl) 的五个 `*_design`/`export_design` 调用；想理解被综合内核本身，回到 [stream_dup.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_dup.hpp)。
