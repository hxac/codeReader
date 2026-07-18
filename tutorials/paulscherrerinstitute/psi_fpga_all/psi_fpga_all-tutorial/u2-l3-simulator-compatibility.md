# 仿真器兼容性矩阵

## 1. 本讲目标

学完本讲，你应当能够：

- 仅凭肉眼读懂 `scripts/runGhdl.tcl`、`scripts/runVivado.tcl`、`scripts/runModelsim.tcl` 里「哪些行被注释掉了」，并据此判断一个库在某仿真器下是否会被仿真。
- 说出 GHDL 与 Vivado 各自不兼容哪些库，以及为什么 ModelSim 是「最全的基线」。
- 解释 `vivadoIP_power_sink` 为什么在三种仿真器下都不跑——而且原因和其他库完全不同（不是「不兼容」，而是「根本没有 self-checking TB」）。
- 把「库 × 仿真器」整理成一张完整的兼容性矩阵表，并理解这张表对实际选型的意义。

本讲不引入新脚本，只换一个视角重新审视 u2-l2 里那三个仿真驱动脚本：上一讲看的是「它们怎么跑」，这一讲看的是「它们各自故意不跑什么、为什么」。

## 2. 前置知识

在进入矩阵之前，先把 u2-l2 已经建立的几个结论压缩成判定规则。

### 2.1 「注释掉 = 跳过」这一条规则

三个仿真脚本的 configure 阶段，对每个库都是固定两行成对出现：

```tcl
cd $myPath/../<类>/<库>/sim
source config.tcl
```

- 这两行**都没有** `#` 时，这个库会在本次仿真里被登记并跑它的 testbench。
- 这两行**前面都加了 `#`**（被注释掉）时，这个库就被本次仿真跳过。

所以「兼容性」在本仓库里不是一份独立文档，而是**直接编码在脚本注释里**：哪一行被 `#` 掉，就代表维护者认为那个库在那个仿真器下不能（或不应）跑。判定兼容性，本质就是数 `#`。

### 2.2 ModelSim 是默认基线

三个脚本唯一的结构性差异在 `init` 这一行：

- `scripts/runModelsim.tcl` 里写的是 `init`（无参数），ModelSim 是 PsiSim 的**默认后端**。
- `scripts/runGhdl.tcl` 里写的是 `init -ghdl`。
- `scripts/runVivado.tcl` 里写的是 `init -vivado`。

因此 ModelSim 脚本可以理解为「参考实现」——它登记的库最全、注释掉的库最少。后面会看到，GHDL 和 Vivado 都是在这份基线上「再做减法」。

### 2.3 self-checking TB 与 `###ERROR###`

u2-l2 已经讲过：仿真跑完不等于通过。每个 testbench（TB）若是 *self-checking*（自检型）的，会在断言失败时主动打印 `###ERROR###`，脚本最后用 `run_check_errors "###ERROR###"` 扫描日志，出现这个串才算失败。本讲 4.3 会用到这个概念——有个库连自检 TB 都没有，所以三种仿真器都跳过它。

## 3. 本讲源码地图

本讲只看三个文件，且都是 u2-l2 已经读过的，这里换个角度重新读：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `scripts/runModelsim.tcl` | ModelSim（默认）仿真驱动 | 作为「最全基线」，看哪些库被启用 |
| `scripts/runGhdl.tcl` | GHDL 仿真驱动 | 哪些库被 `#TB not GHDL compatible!` 注释掉 |
| `scripts/runVivado.tcl` | Vivado 仿真驱动 | 哪些库被 `#TB not Vivado compatible!` 注释掉，以及缺位的库 |

三份脚本的业务逻辑（init→configure→compile→run→check）完全一致，差别只集中在 configure 阶段「哪些 `cd`+`source` 被注释」。

## 4. 核心概念与源码讲解

### 4.1 GHDL 不兼容的库（runGhdl.tcl）

#### 4.1.1 概念说明

GHDL 是一个开源的 VHDL 仿真器。PSI 的大多数库都能在 GHDL 下跑 self-checking TB，但有少数库的 TB 用到了 GHDL 不支持的写法（例如某些 VHDL 特性、浮点包或外部依赖），于是维护者在 `runGhdl.tcl` 里把这些库的 `cd`+`source` 两行整块注释掉，并在上方写一行 `#TB not GHDL compatible!` 说明原因。

注意：**「不兼容」是库与仿真器之间的属性，不是库本身的属性**。同一个库完全可能在 ModelSim 下跑得好好的——本讲的矩阵就是要把这种「库 × 仿真器」的二维关系摊开来看。

#### 4.1.2 核心流程

GHDL 不兼容的判定流程：

1. 维护者发现某库 TB 在 GHDL 下编译不过或行为异常。
2. 在 `runGhdl.tcl` 的 configure 段，把该库对应的 `cd .../sim` 和 `source config.tcl` 两行前面都加 `#`。
3. 在这两行上方加注释 `#TB not GHDL compatible!` 作为留档。
4. 仿真时 PsiSim 自然跳过该库，既不编译它的源文件，也不跑它的 TB。

`runGhdl.tcl` 里共有 **2 个库** 因 GHDL 不兼容被跳过。

#### 4.1.3 源码精读

第一个被跳过的是 `psi_multi_stream_daq`：

> [scripts/runGhdl.tcl:23-25](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runGhdl.tcl#L23-L25) — 这两行被 `#` 注释掉，上方写着 `#TB not GHDL compatible!`，表示 `psi_multi_stream_daq` 的 TB 在 GHDL 下不兼容，本次仿真跳过。

第二个被跳过的是 `vivadoIP_data_rec`：

> [scripts/runGhdl.tcl:33-35](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runGhdl.tcl#L33-L35) — 同样是 `cd`+`source` 两行被注释并标注 `#TB not GHDL compatible!`，`vivadoIP_data_rec` 在 GHDL 下被跳过。

作为对照，这两个库在 ModelSim 基线里都是**启用**的：

> [scripts/runModelsim.tcl:23-24](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl#L23-L24) — `psi_multi_stream_daq` 在 ModelSim 下正常 `cd`+`source`，没有被注释。

这正好印证 4.1.1 的判断：不兼容是「库 × 仿真器」的关系，不是库的固有缺陷。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证「GHDL 跳过、ModelSim 启用」这一对差异。
2. **操作步骤**：
   - 打开 `scripts/runGhdl.tcl`，定位第 23–25 行与第 33–35 行，确认这两组都是 `cd`+`source` 两行被注释、上方有 `#TB not GHDL compatible!`。
   - 打开 `scripts/runModelsim.tcl`，定位 `psi_multi_stream_daq`（约第 23–24 行）与 `vivadoIP_data_rec`（约第 32–33 行），确认它们都是启用状态。
3. **需要观察的现象**：同样两个库名，在 GHDL 脚本里带 `#`，在 ModelSim 脚本里不带 `#`。
4. **预期结果**：你能指着这两处说清楚——这两个库不是「坏了」，而是只在 GHDL 下被人为跳过。
5. **说明**：本实践是源码阅读型，不需要运行仿真。

#### 4.1.5 小练习与答案

**练习 1**：`runGhdl.tcl` 里 `#TB not GHDL compatible!` 这条注释一共出现几次？分别对应哪两个库？

**参考答案**：出现 2 次，分别对应 `psi_multi_stream_daq`（第 23 行附近）和 `vivadoIP_data_rec`（第 33 行附近）。

**练习 2**：如果某天 `psi_multi_stream_daq` 的 TB 被改造成 GHDL 兼容了，应该怎么改 `runGhdl.tcl`？

**参考答案**：把第 24–25 行的 `#cd ...` 和 `#source config.tcl` 前面的 `#` 去掉（建议同时删掉第 23 行的 `#TB not GHDL compatible!` 留档注释），让 PsiSim 重新登记并仿真这个库。

---

### 4.2 Vivado 不兼容的库（runVivado.tcl）

#### 4.2.1 概念说明

Vivado 自带 xsim 仿真器，但它对 VHDL 的支持不如 ModelSim/GHDL 全面。结果是：在 `runVivado.tcl` 里，绝大多数库的 TB 都被注释掉了，只保留 `psi_common` 这一个底层公共库在跑。

这是一种典型的「**能力受限的仿真器只能跑最基础的一层**」的现实：Vivado 仿真在这里更多是用来确认公共基础库能在 Xilinx 工具链下编译通过，而不是用来跑全套回归。

> 小提示：`runVivado.tcl` 用的是 `source -quiet config.tcl`（带 `-quiet`），而 GHDL/ModelSim 用的是 `source config.tcl`。`-quiet` 让 source 在文件不存在时不报错。这只是容错写法的差异，**不影响兼容性判定**——判定兼容性依然只看 `cd`+`source` 两行有没有被 `#` 掉。

#### 4.2.2 核心流程

Vivado 不兼容的判定流程与 GHDL 完全同构，只是规模大得多：

1. 维护者发现某库 TB 在 Vivado/xsim 下不兼容。
2. 把该库 `cd`+`source` 两行整块注释，上方写 `#TB not Vivado compatible!`。
3. 仿真时 PsiSim 跳过该库。

`runVivado.tcl` 里**只有 `psi_common` 是启用的**，其余 8 个库都被标注不兼容而跳过；此外还有一个库（`vivadoIP_axi_mm_reader`）干脆没有出现在脚本里——既没启用，也没注释。

#### 4.2.3 源码精读

唯一启用的库是 `psi_common`：

> [scripts/runVivado.tcl:19-20](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runVivado.tcl#L19-L20) — `psi_common` 是 `runVivado.tcl` 里**唯一**没有被注释的库，`cd`+`source -quiet config.tcl` 两行都处于激活状态。

紧接着的第一个被跳过的库是 `psi_fix`：

> [scripts/runVivado.tcl:22-24](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runVivado.tcl#L22-L24) — `#TB not Vivado compatible!` 注释下，`psi_fix` 的 `cd`+`source` 两行被整块注释，Vivado 下跳过。

这条 `#TB not Vivado compatible!` 模式在脚本里**连续重复了 8 次**，最后一个被跳过的是 `vivadoIP_i2c_devreg`：

> [scripts/runVivado.tcl:50-52](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runVivado.tcl#L50-L52) — `vivadoIP_i2c_devreg` 同样被 `#TB not Vivado compatible!` 注释掉（注意第 52 行 `#source config.tcl` 也是带 `#` 的），Vivado 下跳过。

而 `vivadoIP_axi_mm_reader` 在 `runVivado.tcl` 中**根本没有出现**——对比 `runGhdl.tcl` 第 46–47 行和 `runModelsim.tcl` 第 44–45 行，这个库在另外两个脚本里都是启用的。它在这里是「缺位」而非「注释」，具体原因（遗漏、待支持，还是有意为之）**待确认**，不应臆断。

#### 4.2.4 代码实践

1. **实践目标**：用计数的方式确认 Vivado 的兼容面有多窄。
2. **操作步骤**：
   - 在 `scripts/runVivado.tcl` 里数 `#TB not Vivado compatible!` 出现的次数。
   - 确认除了 `psi_common`（第 19–20 行）之外，是否还有任何 `cd .../sim` + `source ... config.tcl` 两行都不带 `#` 的库。
   - 用编辑器的查找功能搜 `axi_mm_reader`，确认它在 `runVivado.tcl` 里是否出现。
3. **需要观察的现象**：`#TB not Vivado compatible!` 应出现 8 次；唯一启用的是 `psi_common`；`axi_mm_reader` 搜不到。
4. **预期结果**：Vivado 实际只跑 1 个库（`psi_common`），外加一个缺位的 `axi_mm_reader`。
5. **说明**：源码阅读型实践，不需要安装 Vivado。

#### 4.2.5 小练习与答案

**练习 1**：为什么说 Vivado 在这套脚本里几乎只能「跑基础层」？

**参考答案**：因为 `runVivado.tcl` 里 8 个库都被 `#TB not Vivado compatible!` 注释掉，唯一启用的是底层公共库 `psi_common`，说明 Vivado/xsim 在这里主要用于确认公共库能在 Xilinx 工具链下跑通，而非跑全套回归。

**练习 2**：`vivadoIP_axi_mm_reader` 在 GHDL 和 ModelSim 下都启用，但在 `runVivado.tcl` 里既没启用也没注释。这种「缺位」和「被注释」在含义上有什么区别？

**参考答案**：被注释（`#TB not Vivado compatible!`）是维护者**明确判定**该库不兼容并留档；而缺位（axi_mm_reader）没有任何注释说明，可能是后来新增库时漏改了 Vivado 脚本，也可能是有意暂不支持——脚本本身没有给出答案，需要结合提交历史或问维护者确认，不能凭空假定。

---

### 4.3 power_sink：没有 self-checking TB 的特殊库

#### 4.3.1 概念说明

到目前为止，被跳过的库都是「不兼容」——TB 写了，但在某仿真器下跑不了。`vivadoIP_power_sink` 是第三种情况，也是最值得区分的一种：**它根本没有 self-checking TB**，所以三种仿真器都跳过它。

这不是兼容性问题，而是这个库的目标本身就不适合用功能仿真来验证。power_sink 的用途是把信号「吃掉」、制造翻转活动，用来做**功耗分析**（给 Vivado 的功耗估算工具喂激励）。而功耗、信号翻转率、综合后的优化效果，都是**功能仿真器（GHDL/ModelSim/xsim）无法模拟的**——它们只能模拟逻辑行为，给不出有意义的功耗数字。

所以三种仿真器跳过它的理由完全一致，而且和 4.1、4.2 的「不兼容」是两码事。

#### 4.3.2 核心流程

power_sink 的跳过流程：

1. 维护者判断该库无法用功能仿真自检（功耗不可仿真）。
2. 在三个脚本里都把它的 `cd .../sim` 行注释掉。
3. 与其他被注释库不同，注释说明写的是 `#Does not have a self-checking TB because power consumption/toggling/optimization cannot be simulated!`，强调「没有自检 TB」而非「不兼容」。
4. 三种仿真器都不会跑它。

注意一个细节：power_sink 这里只注释了 `cd` 那一行，并没有配套的 `source config.tcl` 行——因为这个库压根没有 `config.tcl`（没有 TB 就没有要登记的源文件）。

#### 4.3.3 源码精读

三处注释的文字完全相同，分别位于三个脚本：

> [scripts/runGhdl.tcl:49-50](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runGhdl.tcl#L49-L50) — GHDL 脚本里 `power_sink` 的注释，写明「没有 self-checking TB，因为功耗/翻转/优化无法仿真」。

> [scripts/runModelsim.tcl:47-48](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl#L47-L48) — ModelSim 基线脚本里 `power_sink` 的同一句注释。注意即使是「最全」的 ModelSim，也照样跳过了它。

> [scripts/runVivado.tcl:54-55](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runVivado.tcl#L54-L55) — Vivado 脚本里 `power_sink` 的同一句注释。

对比一下注释措辞，就能看出维护者在区分两类原因：

| 库 | 注释措辞 | 含义 |
| --- | --- | --- |
| `psi_multi_stream_daq`（GHDL） | `#TB not GHDL compatible!` | TB 存在，但 GHDL 跑不了 |
| `psi_fix`（Vivado） | `#TB not Vivado compatible!` | TB 存在，但 Vivado 跑不了 |
| `power_sink`（三者） | `#Does not have a self-checking TB ...` | 根本没有自检 TB，与仿真器无关 |

#### 4.3.4 代码实践

1. **实践目标**：确认 power_sink 的「无 TB」属性与仿真器无关。
2. **操作步骤**：
   - 在三个脚本里分别定位 power_sink 的注释（行号见 4.3.3）。
   - 对比它的注释措辞与 4.1、4.2 里「not XX compatible」的措辞。
   - 注意 power_sink 处只有一行被注释的 `cd`，没有对应的 `source config.tcl`。
3. **需要观察的现象**：三处注释文字一致，且都不是「不兼容」措辞；且没有 `source config.tcl` 行。
4. **预期结果**：得出结论——power_sink 三种仿真器都不跑，原因统一是「功耗不可仿真、没有自检 TB」，与具体仿真器的能力无关。
5. **说明**：源码阅读型实践。

#### 4.3.5 小练习与答案

**练习 1**：用一句话解释为什么 power_sink 在 ModelSim（能力最强的基线）下也不跑。

**参考答案**：因为 power_sink 的用途是做功耗分析，而功耗、翻转率、综合优化都是功能仿真器无法模拟的，所以它根本没有 self-checking TB，跟 ModelSim 支不支持无关。

**练习 2**：如果把 power_sink 的 `cd` 注释去掉、并加一行 `source config.tcl`，会发生什么？

**参考答案**：PsiSim 会尝试 `cd` 到 `vivadoIP_power_sink/sim` 并 source 那里的 `config.tcl`；但该子模块大概率没有 `sim/config.tcl`（因为它没有 TB），source 会报文件不存在（Vivado 脚本因为有 `-quiet` 可能静默失败，GHDL/ModelSim 则会直接报错）。所以这种改动没有意义，也违背了维护者注释的本意。

---

### 4.4 完整兼容性矩阵

把 4.1–4.3 的结论汇总成一张表，就是本讲的核心产出。判定口径：某个库在脚本里 `cd`+`source` 两行都未注释即为「✅ 跑」；被整块注释即为「❌ 跳过」；既未启用也未注释即为「— 缺位」。

| 库（submodule） | GHDL | ModelSim | Vivado | 不跑的原因 |
| --- | :---: | :---: | :---: | --- |
| `psi_common` | ✅ | ✅ | ✅ | — |
| `psi_fix` | ✅ | ✅ | ❌ | TB not Vivado compatible |
| `psi_multi_stream_daq` | ❌ | ✅ | ❌ | GHDL 与 Vivado 均不兼容 |
| `vivadoIP_axis_data_gen` | ✅ | ✅ | ❌ | TB not Vivado compatible |
| `vivadoIP_clock_measure` | ✅ | ✅ | ❌ | TB not Vivado compatible |
| `vivadoIP_data_rec` | ❌ | ✅ | ❌ | GHDL 与 Vivado 均不兼容 |
| `vivadoIP_mem_test` | ✅ | ✅ | ❌ | TB not Vivado compatible |
| `vivadoIP_spi_simple` | ✅ | ✅ | ❌ | TB not Vivado compatible |
| `vivadoIP_i2c_devreg` | ✅ | ✅ | ❌ | TB not Vivado compatible |
| `vivadoIP_axi_mm_reader` | ✅ | ✅ | — | Vivado 脚本中缺位（待确认） |
| `vivadoIP_power_sink` | ❌ | ❌ | ❌ | 无 self-checking TB（功耗不可仿真） |

几个一眼可见的结论：

- **ModelSim 是最全的基线**：除 `power_sink` 外，其余 10 个库全部启用（共跑 10 个）。这也呼应了 2.2——`init` 不带参数即默认 ModelSim。
- **GHDL 做了少量减法**：跑 8 个，比 ModelSim 少 `psi_multi_stream_daq`、`vivadoIP_data_rec`（外加 `power_sink`）。
- **Vivado 几乎只跑基础层**：仅 `psi_common` 一个库启用（跑 1 个），其余 8 个被注释、1 个缺位。
- **power_sink 是唯一「全列红」的库**，但原因与其他库不同：不是兼容性，而是「没有自检 TB」。

> 选型含义：如果你想用开源工具跑最大覆盖率的回归，选 GHDL（差 2 个）；如果要把仿真和综合放在同一个 Xilinx 工具链里，选 Vivado（但只能验公共库）；要跑最全的 self-checking 回归，还是得用 ModelSim。

## 5. 综合实践

**任务**：亲手产出本讲的兼容性矩阵，并解释 power_sink。

1. **实践目标**：不依赖本讲已给的表，自己从三个脚本里读出整张矩阵，验证 4.4 的结论。
2. **操作步骤**：
   - 打开 `scripts/runModelsim.tcl`，把 configure 段（约第 17–45 行）里所有未注释的 `cd $myPath/../...` 路径中的库名记下来，作为「全集」。
   - 打开 `scripts/runGhdl.tcl`，对全集里的每个库，判断它在 GHDL 下是启用还是被 `#TB not GHDL compatible!` 注释。
   - 打开 `scripts/runVivado.tcl`，对全集里的每个库，判断它在 Vivado 下是启用、被 `#TB not Vivado compatible!` 注释，还是缺位。
   - 把三者交叉填进一张「库 × 仿真器」表。
   - 在表下方用一句话解释 power_sink 为什么三种仿真器都不跑。
3. **需要观察的现象**：ModelSim 列几乎全绿；GHDL 列有两个红；Vivado 列几乎全红、且缺一个 `axi_mm_reader`；power_sink 整行红。
4. **预期结果**：你得到的表应与 4.4 一致（统计：ModelSim 跑 10、GHDL 跑 8、Vivado 跑 1），并能写出「power_sink 没有自检 TB，因为功耗/翻转/优化无法用功能仿真器模拟」这一句解释。
5. **说明**：纯源码阅读型实践，无需运行任何仿真器；若想在真实环境验证，需先按 u1-l2 用 `--recurse-submodules` 克隆全部子模块，并在装有对应仿真器的 Vivado/ModelSim/GHDL 的 Tcl 控制台里 `source` 对应脚本（完整运行「待本地验证」）。

## 6. 本讲小结

- 兼容性在本仓库里**编码在脚本注释里**：某库的 `cd`+`source` 两行被 `#` 掉，就代表它在那个仿真器下被跳过。
- **ModelSim 是默认基线**（`init` 无参数），登记库最全，除 `power_sink` 外全部启用（跑 10 个）。
- **GHDL** 因 `#TB not GHDL compatible!` 跳过 `psi_multi_stream_daq` 和 `vivadoIP_data_rec`（跑 8 个）。
- **Vivado** 最受限，只有 `psi_common` 启用，其余 8 个被 `#TB not Vivado compatible!` 注释，`vivadoIP_axi_mm_reader` 缺位（跑 1 个）。
- `power_sink` 是特例：三种仿真器都跳过它，但原因是**没有 self-checking TB**（功耗不可仿真），与兼容性无关。
- 「不兼容」是**库 × 仿真器**的二维属性，不是库的固有缺陷——同一个库可以在一个仿真器下红、在另一个下绿。

## 7. 下一步学习建议

- 下一讲 **u2-l4（Vivado IP 批量打包脚本）** 会转向 `scripts/packageAllIp.tcl`，看本讲提到的那些 `vivadoIP_*` 库是如何被打包成可被 Vivado 调用的 IP 核的——注意「能跑仿真的 IP」和「会被打包的 IP」是两个不同的集合。
- 进阶 **u3 单元** 会回到版本与维护：当你想升级某个库以修复兼容性问题时，可参考 **u3-l1（发布管理与 submodule 版本固定）** 里 Changelog 的版本固定机制。
- 想自己加一个带 self-checking TB 的新库？学完 u2-l4 后，可以参考 **u3-l2（维护与扩展集合仓库）** 里新增 submodule 的约定，并回头按本讲的「`cd`+`source` 两行」模式把它登记进三个仿真脚本。
