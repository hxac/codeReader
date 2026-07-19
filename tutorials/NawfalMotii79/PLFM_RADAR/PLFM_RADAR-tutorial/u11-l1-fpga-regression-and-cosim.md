# FPGA 回归测试与协同仿真

## 1. 本讲目标

AERIS-10 的 FPGA 承担了从 DDC 到 CFAR 的整条接收信号处理链（见 u4 系列），任何一处定点运算或状态机的改动都可能让雷达「看得到数据但测不准目标」。本讲解决一个问题：**在不依赖昂贵的 Vivado 许可证的前提下，如何用开源工具 iverilog 把一整套 FPGA 回归测试跑起来，并且让它能抓到 Vivado 才会报的错、以及自生成黄金值抓不到的架构错误。**

学完本讲，读者应该能够：

- 说清楚 `run_regression.sh` 的五个阶段（Phase 0 lint + 四个测试阶段）各自测什么、PASS/FAIL 如何判定。
- 理解「Vivado 级 lint」的核心思想：用 iverilog 的 `-Wall` 输出 + 自定义正则，把 Vivado 综合才报的 ERROR/SYNTH 警告在免费的 iverilog 里提前拦截。
- 区分两类黄金值——`gen_mf_golden_ref.py` 这类「自生成自比对」黄金值，与 `tb_fullchain_realdata.v` 这类「真实雷达数据 exact-match」黄金值——并解释为什么后者能抓到前者抓不到的架构错误。
- 独立运行回归脚本并读懂它的输出表。

## 2. 前置知识

- **回归测试（regression test）**：每次代码改动后自动重跑的一整套测试，目的是「保证新改动没有破坏旧功能」。本讲的回归跑的是 FPGA 的 RTL（寄存器传输级）仿真。
- **iverilog / vvp**：Icarus Verilog 是一个开源 Verilog 仿真器。`iverilog` 负责把 `.v` 编译成可执行仿真映像（`.vvp`），`vvp` 负责运行它。它比 Vivado 快、免费，但不认识 Xilinx 专属原语（如 `IBUFDS`、`BUFG`、`MMCM`）。
- **Vivado**：Xilinx 的官方综合/实现工具。它会在综合阶段报一类「严重警告」（如位宽不匹配、`case` 缺 `default` 推断出锁存器），这些在 iverilog 里常常被静默接受。Vivado 跑一次综合要几十分钟到几小时，而 iverilog 仿真只要几秒。
- **黄金值（golden reference）**：一份「已知正确」的期望输出。测试时把 RTL 仿真输出和黄金值逐比特比对，对不上就是 FAIL。
- **协同仿真（co-simulation，cosim）**：用一种语言（这里是 Python/NumPy）建一个「参考模型」，用另一种语言（这里是 Verilog）实现真实硬件，两边喂同样的输入、比输出。本讲的 cosim 还进一步用**真实雷达采集数据**作为输入。
- **定点运算（fixed-point）**：FPGA 里小数用固定位宽的整数表示（如 Q15）。`golden_reference.py` 必须精确复现 RTL 的位宽、截断、舍入、饱和，才能做 bit-for-bit 比对——这一点在 u4-l1（DDC）、u4-l4（Doppler）里已建立认知，本讲直接承接。

本讲依赖 u4-l4（Doppler 双 16 点 FFT 架构，这是真实数据 cosim 的核心被测对象）和 u6-l1（USB 数据包格式，理解 11 字节数据包如何承载 range/Doppler bin）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [9_Firmware/9_2_FPGA/run_regression.sh](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/run_regression.sh) | 回归测试总入口，一个 bash 脚本。定义 RTL 文件清单、五阶段流程、lint 判定、PASS/FAIL 统计。 |
| [9_Firmware/9_2_FPGA/tb/tb_fullchain_realdata.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/tb/tb_fullchain_realdata.v) | 「真实数据全链路」testbench：把 range_bin_decimator 和 doppler_processor 串起来，喂真实 ADI CN0566 雷达数据，与 Python 黄金值逐比特比对。 |
| [9_Firmware/9_2_FPGA/tb/cosim/real_data/golden_reference.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/tb/cosim/real_data/golden_reference.py) | 独立的 NumPy 位精确参考模型，复现整条 DSP 链的定点运算，产出并冻结真实数据黄金 hex。 |
| [9_Firmware/9_2_FPGA/tb/gen_mf_golden_ref.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/tb/gen_mf_golden_ref.py) | 「自生成」黄金值示例：用合成信号（DC/单频/脉冲）生成匹配滤波器的输入与期望输出 hex。本讲用它说明「自生成黄金值」的局限。 |
| [9_Firmware/9_2_FPGA/tb/cosim/validate_mem_files.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/tb/cosim/validate_mem_files.py) | 校验所有 `.mem` 数据文件（FFT 旋转因子、chirp LUT、latency 参数）是否符合雷达系统参数。 |
| [9_Firmware/9_2_FPGA/tb/tb_doppler_realdata.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/tb/tb_doppler_realdata.v) | `tb_fullchain_realdata` 的姊妹 testbench，只测 Doppler 段（不含 decimator），结构相同，可对照阅读。 |

## 4. 核心概念与源码讲解

### 4.1 回归阶段划分与 run_test 机制

#### 4.1.1 概念说明

`run_regression.sh` 是一个 bash 脚本，它把「编译每个 testbench → 运行 vvp → 数 PASS/FAIL」这套机械流程封装成一条命令。它的存在意义是：**让 FPGA 验证像 Python 的 `pytest` 一样一条命令跑完，且退出码能直接被 CI 判定**（退出 0 = 全过，退出 1 = 有失败）。

脚本头部说明了它的自我定位与用法：

[run_regression.sh:1-12](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/run_regression.sh#L1-L12) —— 注释写明 Phase 0 做 Vivado 风格 lint、Phase 1+ 编译运行所有 iverilog testbench，支持 `--quick`（跳过长测试）和 `--skip-lint`（跳过 lint，不推荐）两个开关，退出码 0/1 反映成败。

脚本用 `set -euo pipefail` 开启严格模式（任一命令失败立即终止、未定义变量报错、管道任一环节失败即算失败），保证不会「静默出错还假装通过」。

#### 4.1.2 核心流程

整个回归分为 **Phase 0（lint）+ 四个测试阶段**，按从快到慢、从独立到集成的顺序排列：

```
Phase 0  LINT        —— Vivado 级静态检查（iverilog -Wall + 正则），失败则直接 exit 1
Phase 1  单元测试    —— 改动频繁的核心模块（CIC/chirp/Doppler/CFAR/MTI/AGC/self-test 等 9 个）
Phase 2  集成测试    —— 多模块串联（DDC 链、真实数据 Doppler、真实数据全链路、接收机 golden、系统 E2E）
Phase 3  信号处理    —— FFT/NCO/FIR/匹配滤波等底层 DSP 单元
Phase 4  基础设施    —— CDC/边沿检测/USB/距离抽取/模式控制器
```

> 说明：脚本里实际有 **五个** `PHASE` 标号（0~4）。大纲里说的「四阶段」指的是 lint 之后的四个**测试**阶段；本讲按脚本真实标号讲解。

每个 testbench 的「编译+运行+计数」被抽成一个公用函数 `run_test`，这是理解整个回归的关键：

[run_regression.sh:271-316](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/run_regression.sh#L271-L316) —— `run_test <名字> <vvp路径> <iverilog参数...>`：先用 `iverilog -g2001 -DSIMULATION` 编译，再用 `timeout 120 vvp` 运行（120 秒超时保护），最后用两个 `grep -Ec` 数输出里的 `[PASS]`/`[FAIL]` 标记。

它的 PASS/FAIL 判定逻辑值得细看：

1. **编译失败** → 直接 `COMPILE FAIL`，计入 `FAIL`。
2. **运行后统计标记**：testbench 自己用 `$display("[PASS] ...")` / `$display("[FAIL] ...")` 打印结果，脚本用正则 `^\[PASS([^]]*)\]` 和 `^\[FAIL([^]]*)\]` 计数。有 FAIL 标记 → FAIL；否则有 PASS 标记 → PASS。
3. **无任何标记** → 检查输出里有没有 `finish/complete/done`，有则算 PASS（completed），都没有则记 `UNKNOWN` 并**当作 FAIL**。

把「UNKNOWN 也算 FAIL」是刻意的保守策略：一个既不报 PASS 也不报 FAIL 的 testbench 很可能是中途死掉或忘了写断言，宁可信其坏。

#### 4.1.3 源码精读

RTL 文件清单集中维护，新增模块只需改一处。生产 RTL 清单与「为何排除真实 ADC 接口」的注释：

[run_regression.sh:51-88](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/run_regression.sh#L51-L88) —— `PROD_RTL` 数组列出全部生产 RTL；注意第 83-85 行注释：真实的 `ad9484_interface_400m.v` 因为用了 `IBUFDS/BUFIO/BUFG/IDDR` 等 Xilinx 原语，iverilog 编不了，所以仿真改用 stub `tb/ad9484_interface_400m_stub.v`。这正是 iverilog 与 Vivado 差异的具体体现。

四个测试阶段的入口与真实数据 cosim 的定位注释：

[run_regression.sh:417-427](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/run_regression.sh#L417-L427) —— Phase 2 里两句关键注释（417-419 行）：**「真实数据协同仿真：用已提交的黄金 hex 对比 RTL（要求精确匹配）。这些测试能抓到自生成黄金生成/比对测试抓不到的架构错误（例如 32 点 → 双 16 点 Doppler FFT）。」** 这句话是本讲第 4.3、4.4 节的纲领，先用一句话点出，后文展开。

`--quick` 开关跳过的是 Phase 2 里最慢的六个集成测试（接收机 golden 生成/比对、系统顶层、系统 E2E，以及它们的 FT2232H 变体），真实数据 cosim 两个测试**不在跳过之列**，因为它们是「快速且高价值」的：

[run_regression.sh:429-464](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/run_regression.sh#L429-L464) —— `--quick` 分支只 `SKIP += 6`，注释写明「skipped receiver golden + system top + E2E — use without --quick」。

最后的汇总与退出码：

[run_regression.sh:519-551](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/run_regression.sh#L519-L551) —— 打印 lint 与 `Tests: $PASS passed, $FAIL failed, $SKIP skipped`，`FAIL > 0` 则 `exit 1`。

#### 4.1.4 代码实践

**实践目标**：把回归脚本跑起来，读懂它的输出表，确认五个阶段的边界。

**操作步骤**（注意：以下命令需本地已安装 `iverilog`；本讲义作者未在此环境实跑，结果记为「待本地验证」）：

1. 进入 FPGA 目录：`cd 9_Firmware/9_2_FPGA`
2. 快速跑一遍：`./run_regression.sh --quick`
3. 观察输出里 `--- PHASE 0/1/2/3/4: ... ---` 这些分隔行。
4. 跑完整版（含慢测试）：`./run_regression.sh`
5. 想单独看 lint：`./run_regression.sh --skip-lint`（仅用于对比，官方不推荐）。

**需要观察的现象**：

- 每个测试一行，形如 `  CIC Decimator                                PASS (N checks)`，对齐到 45 列宽（来自 `printf "  %-45s "`）。
- `--quick` 模式下应看到 `(skipped receiver golden + system top + E2E — use without --quick)`，且最后 `SKIP` 计数 +6。
- 最后的 `RESULTS` 块给出 `Tests: X passed, Y failed, Z skipped / W total`。

**预期结果**：在一个健康的 HEAD 上，`--quick` 模式应 0 failed；完整模式可能因为机器性能需要数分钟。**待本地验证**：实际通过数与耗时。

#### 4.1.5 小练习与答案

**练习 1**：如果一个 testbench 既不打印 `[PASS]` 也不打印 `[FAIL]`，但在输出里有 `$finish`，`run_test` 会判它 PASS 还是 FAIL？

**参考答案**：判 PASS。因为 `run_test` 在「无 PASS/FAIL 标记」分支里会再 `grep -qi 'finish|complete|done'`，命中就按「completed」计 PASS。只有连这些关键词都没有时才记 UNKNOWN（并当 FAIL）。

**练习 2**：为什么脚本用 `timeout 120 vvp` 包裹运行？

**参考答案**：防止一个挂死的 testbench（例如状态机死锁、等一个永远不来的握手信号）把整个回归卡住。120 秒还没结束就强杀，避免一条坏测试阻塞所有后续测试。

---

### 4.2 Vivado 级 Lint：用 iverilog 模拟 Vivado 错误

#### 4.2.1 概念说明

iverilog 是免费的，但它「太宽容」——很多 Vivado 综合阶段会报 ERROR 或严重 WARNING 的问题，iverilog 只是默默编译通过。如果只靠 iverilog 做 CI，这些隐患就会一路溜到上板综合时才暴露，那时已经浪费了几十分钟。

`run_regression.sh` 的 Phase 0 用**两层叠加**的办法把 iverilog 改造成「穷人版 Vivado lint」：

- **Layer A**：用 `iverilog -Wall` 编译全设计，然后把它的 warning 文本**按正则分类**——把 Vivado 视为 ERROR 的那几类（位选择越界、端口位宽不匹配）单独挑出来判失败。
- **Layer B**：自定义正则/awk 静态检查，去抓那些 iverilog 根本不报、但 Vivado 会报（如 `case` 缺 `default` 推断锁存器）的模式。

这就是「Vivado 级 lint」的含义：**不是真的跑 Vivado，而是用 iverilog 的输出加上手写规则，模拟 Vivado 综合的错误判定**，让问题在秒级的 iverilog 阶段就被拦截。

#### 4.2.2 核心流程

```
Layer A (run_lint_iverilog):
  iverilog -g2001 -DSIMULATION -Wall -o /dev/null <所有生产RTL>  2> warn.log
  ├─ 编译失败(真错误)        → COMPILE ERROR，LINT_ERR++，致命
  └─ 逐行读 warn.log，正则分类:
       ├─ "Part select ... after the vector" / "out of bound bits" → err_count  (Vivado Synth 8-524 ERROR)
       ├─ "port ... does not match" / "Port ... mismatch"          → err_count  (Vivado 位宽/连接 ERROR)
       ├─ "timescale" / "dangling" / "sensitive to all"            → info_count (非阻塞)
       └─ 其他非空行                                                → info_count
  err_count > 0 → FAIL；否则 PASS

Layer B (run_lint_static):
  对每个生产 RTL 文件跑 awk，找 case/casex/casez 块，检查块内是否有 default:
       没有 default → 记 [SYNTH-6] warning（Vivado 会警告推断锁存器）
  err_count > 0 → FAIL；warn_count > 0 → WARN（非阻塞）；否则 PASS

Phase 0 汇总: LINT_ERR > 0 → echo "Fix lint errors before pushing to Vivado" → exit 1（整回归中止）
```

关键设计：**lint 失败会直接 `exit 1` 中止整个回归**，因为带 Vivado 级错误的代码送进 Vivado 也必然失败，没必要再浪费时间跑测试。

#### 4.2.3 源码精读

Layer A 的编译与 warning 分类：

[run_regression.sh:118-169](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/run_regression.sh#L118-L169) —— 第 126 行 `iverilog -g2001 -DSIMULATION -Wall -o /dev/null`：注意 `-o /dev/null`，说明这一步**只关心能不能编译通过、有多少 warning，不产生可运行映像**。第 144-150 行把「Part select 越界」和「端口不匹配」两类 warning 标成 `[VIVADO-ERR]` 计入 `err_count`，对应注释里写的 Vivado `Synth 8-524` 错误码。

Layer B 的 awk 多行检查（抓 `case` 缺 `default`）：

[run_regression.sh:216-248](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/run_regression.sh#L216-L248) —— awk 状态机：遇到 `case/casex/casez (` 记下行号并置 `in_case=1`、`has_default=0`；遇到 `default :` 置 `has_default=1`；遇到 `endcase` 时若 `!has_default` 就输出 `case statement without default`。这模拟的是 Vivado 的 `SYNTH-6`「推断出锁存器」警告——在纯组合 `case` 里漏掉 `default`，综合后可能生成锁存器，是时序隐患。

值得注意的诚实标注：`run_lint_static` 里有几条注释（第 190-211 行）坦白说明哪些检查「对逐行正则太复杂、本版跳过」（如时钟块里的阻塞赋值 `=`、多驱动寄存器），并指引「交给 Vivado lint 或 testbench 覆盖」。这说明作者清楚正则 lint 的边界，没有假装它能替代真 Vivado。

Phase 0 的中止逻辑：

[run_regression.sh:330-362](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/run_regression.sh#L330-L362) —— 先对生产 RTL 跑 Layer A，再对 `EXTRA_RTL` 里「不在顶层但也要保持干净」的模块单独跑，最后跑 Layer B；`LINT_ERR > 0` 时打印「Fix lint errors before pushing to Vivado. Aborting regression.」并 `exit 1`。

#### 4.2.4 代码实践

**实践目标**：亲手触发一条 Vivado 级 lint 错误，观察它如何在 iverilog 阶段被拦截。

**操作步骤**（这是「源码阅读 + 思想实验」型实践，**不要真的改源码入库**，可在临时副本上做）：

1. 复制一个简单模块到 `/tmp/lint_test.v`，写一个 `case(sel)` 但**故意不写 `default`**，且把某个输出在 `case` 外没赋初值（模拟推断锁存器）。
2. 手动跑 Layer B 的 awk 片段：把 `run_regression.sh` 第 223-240 行的 awk 单独抽出来对 `/tmp/lint_test.v` 运行，观察是否输出 `lint_test.v:N: case statement without default`。
3. 再写一段 `wire [7:0] a; assign a[16:8] = ...;`（位选择越界），跑 `iverilog -Wall`，观察 warning 里是否出现 `Part select ... is selecting after the vector` 字样，理解它如何被 Layer A 正则命中。

**需要观察的现象**：

- 第 2 步应打印出缺失 `default` 的告警，证明 awk 能定位到具体文件行号。
- 第 3 步 iverilog 只给 warning（不致命），但 Layer A 会把它升级成 `[VIVADO-ERR]` 并让 lint FAIL。

**预期结果**：两个构造的缺陷都能在「不打开 Vivado」的前提下被发现。这正是 Phase 0 的价值。**待本地验证**：iverilog 不同版本对 `Part select` 警告的措辞可能略有差异，若正则没命中，需对照实际 warning 文本调整 grep 串。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `case` 缺 `default` 在 FPGA 里是个隐患？

**参考答案**：如果 `case` 用于纯组合逻辑（如多路选择器）却没覆盖所有分支、又没 `default`，综合工具为了「在没有命中的分支里保持上一次的值」，会推断出锁存器（latch）。锁存器不受时钟控制、对毛刺敏感，是时序和功能 bug 的常见来源，所以 Vivado 用 `SYNTH-6` 警告提醒。

**练习 2**：Layer A 用 `-o /dev/null`，Layer B 用 awk，两者覆盖的问题集重叠吗？

**参考答案**：基本不重叠，是互补关系。Layer A 靠 iverilog 自己的分析抓「位选择越界、端口位宽不匹配」这类 iverilog **能发现但默认不致命**的问题；Layer B 靠外部正则抓 iverilog **根本不分析**的「case 缺 default」这类问题。脚本注释里也提到部分检查（如时钟块阻塞赋值）两者都难覆盖，留给了 testbench 和 Vivado。

---

### 4.3 真实数据协同仿真：exact-match 黄金比对

#### 4.3.1 概念说明

`tb_fullchain_realdata.v` 是本讲的高价值测试。它做了一件很硬核的事：**把真实雷达采集的数据（ADI CN0566 Phaser，10.525 GHz X 波段 FMCW）喂进 RTL 的「距离抽取 + Doppler」全链路，然后把 RTL 输出和一份独立 Python 模型算出的黄金值逐比特比对，要求 2048 个输出全部精确相等。**

这里的三个关键词：

- **真实数据**：输入不是合成的正弦波或脉冲，而是真实雷达看真实场景录下来的距离-FFT 结果，含有真实的距离-多普勒耦合、加窗边缘效应、目标与杂波混合。
- **全链路**：被测的不止单个模块，而是 `range_bin_decimator`（1024→64 峰值抽取）串接 `doppler_processor_optimized`（Hamming 加窗 + 双 16 点 FFT），即 u4-l3、u4-l4 讲过的两段。
- **exact-match（精确匹配）**：容差 `MAX_ERROR = 0`，不是「差不多对」，而是「一个比特都不能差」。这要求 Python 参考模型 `golden_reference.py` 精确复现 RTL 的所有定点细节（位宽、截断、舍入、饱和）。

#### 4.3.2 核心流程

```
离线（一次性，结果提交进仓库）:
  golden_reference.py (NumPy 位精确模型)
    + 真实 ADI CN0566 雷达数据
    → 产出并冻结 hex:
         fullchain_range_input.hex   (32768 x 32-bit 输入: 32 chirps x 1024 bins)
         fullchain_doppler_ref_i/q.hex (2048 x 16-bit 期望输出: 64 range x 32 Doppler)

每次回归（在线）:
  tb_fullchain_realdata.v
    1. $readmemh 读入输入 hex → 喂给 range_bin_decimator
    2. decimator 输出直连 doppler_processor 输入
    3. 收集 2048 个 Doppler 输出到 cap_out_i/q[]
    4. 逐点: diff = cap_out - ref;  check(|diff| <= MAX_ERROR=0)
    5. 全部 2048 点 |diff|==0 → RESULT: ALL TESTS PASSED
```

黄金 hex 是**冻结**的：它由 `golden_reference.py` 一次性生成后提交进仓库（见 `tb/cosim/real_data/hex/STALE_NOTICE.md`），回归时只读不改。这一点是它区别于「自生成」黄金值的关键，下一节详述。

#### 4.3.3 源码精读

testbench 头部把整个实验的设计意图、输入输出布局、通过判据说得很清楚：

[tb_fullchain_realdata.v:1-34](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/tb/tb_fullchain_realdata.v#L1-L34) —— 注释写明：喂真实 ADI CN0566 数据（post-range-FFT，32 chirps × 1024 bins）穿过 `range_bin_decimator`（峰值检测 1024→64）和 `doppler_processor_optimized`（Hamming + 双 16 点 FFT），与 Python 黄金值逐比特比对；通过判据是 **ALL 2048 个 Doppler 输出精确匹配**。

最关键的一行参数——精确匹配的「零容差」：

[tb_fullchain_realdata.v:41-57](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/tb/tb_fullchain_realdata.v#L41-L57) —— 第 56 行注释「Error tolerance: 0 = exact match required」，第 57 行 `localparam integer MAX_ERROR = 0;`。这是「精确匹配」的字面来源：任何一点偏差都判 FAIL。

两个 DUT 的串联例化（decimator 输出用 `assign` 直连 Doppler 输入）：

[tb_fullchain_realdata.v:103-138](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/tb/tb_fullchain_realdata.v#L103-L138) —— `range_bin_decimator` 用 `.decimation_mode(2'b01)`（峰值检测模式）、`INPUT_BINS=1024/OUTPUT_BINS=64/DECIMATION_FACTOR=16`；第 87-88 行 `assign range_data_32bit = {decim_q_out, decim_i_out}` 把 decimator 输出打包成 Doppler 期望的 32 位 `{Q,I}`，完美对应 u4-l3/u4-l4 的接口契约。

输入与期望输出都从冻结 hex 读入：

[tb_fullchain_realdata.v:148-163](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/tb/tb_fullchain_realdata.v#L148-L163) —— `input_mem` 用 `$readmemh("tb/cosim/real_data/hex/fullchain_range_input.hex", ...)` 载入 32768 个 32 位样本；`ref_i/ref_q` 用 `$readmemh` 载入 2048 个 16 位有符号期望值。

逐比特比对与 check 任务：

[tb_fullchain_realdata.v:379-416](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/tb/tb_fullchain_realdata.v#L379-L416) —— 第一个循环（379-406）统计 `n_exact`（I/Q 都为 0 偏差）、`n_within_tol`、`max_err_i/q`，并打印前 20 个不匹配点用于调试；第二个循环（409-416）对每个样本调 `check(abs_diff_i <= MAX_ERROR && abs_diff_q <= MAX_ERROR)`，把每个点变成一条独立断言。第 186-198 行的 `check` task 负责 `[PASS]/[FAIL]` 计数——这正是 `run_test` 用 grep 数的标记。

结果判定与看门狗：

[tb_fullchain_realdata.v:442-461](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/tb/tb_fullchain_realdata.v#L442-L461) —— `fail_count==0` 打印 `RESULT: ALL TESTS PASSED`，否则 `RESULT: N TESTS FAILED`；独立的 `initial` 看门狗块在 `CLK_PERIOD * MAX_CYCLES * 2` 后强杀仿真，防卡死。

Python 参考模型的「位精确」承诺——它是独立于 RTL 的第二份实现：

[golden_reference.py:1-19](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/tb/cosim/real_data/golden_reference.py#L1-L19) —— 文档字符串明确：模型「**复制 RTL 的精确定点运算（位宽、截断、舍入、饱和）**，以便输出可与 Icarus Verilog 仿真结果逐比特比对」，且用真实 ADI CN0566 数据逐级验证 `ADC → DDC → Range FFT → Doppler FFT → Detection`。这是「两份独立实现必须对真实数据达成一致」的契约。

黄金 hex 的「何时重生成」纪律：

[tb/cosim/real_data/hex/STALE_NOTICE.md:1-15](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/tb/cosim/real_data/hex/STALE_NOTICE.md#L1-L15) —— 这些 hex 是「**已提交的黄金参考**，用于严格的逐比特真实数据回归」；只有当 Doppler 处理流水线改变（`doppler_processor.v` 的 FFT 点数/窗/子帧结构、`xfft_16/fft_engine` 的蝶形、`range_bin_decimator` 的抽取模式、`fft_twiddle_16.mem`）时才重生成。换句话说，**正常改动 RTL 不会触发重生成黄金值，而是会让比对失败**——这正是它「能抓架构错误」的前提。

#### 4.3.4 代码实践

**实践目标**：单独编译运行 `tb_fullchain_realdata`，读懂它的 SUMMARY 输出。

**操作步骤**（需本地有 iverilog；**待本地验证**）：

1. `cd 9_Firmware/9_2_FPGA`
2. 用 testbench 头部给的命令编译运行（见 [tb_fullchain_realdata.v:26-33](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/tb/tb_fullchain_realdata.v#L26-L33)）：
   ```bash
   iverilog -Wall -DSIMULATION -g2012 -o tb/tb_fullchain_realdata.vvp \
     tb/tb_fullchain_realdata.v range_bin_decimator.v doppler_processor.v xfft_16.v fft_engine.v
   vvp tb/tb_fullchain_realdata.vvp
   ```
3. 观察 `SUMMARY` 块里的 `Exact match: X / 2048`、`Max error (I/Q)`、`Pass/Fail`。

**需要观察的现象**：

- 若 RTL 与冻结黄金一致，`Exact match` 应为 `2048 / 2048`，`Max error` 为 0，结尾 `RESULT: ALL TESTS PASSED`。
- 若有人改了 Doppler 架构（如把双 16 点改回单 32 点），会看到大量不匹配点（前 20 个会逐点打印 `RTL=(...) REF=(...) ERR=(...)`），`Exact match` 远小于 2048。

**预期结果**：当前 HEAD 下应全部通过。**待本地验证**：真实通过数与耗时。

#### 4.3.5 小练习与答案

**练习 1**：为什么这个 testbench 把 decimator 的 `decimation_mode` 固定成 `2'b01`（峰值检测）？

**参考答案**：因为冻结的黄金 hex 是用同样的峰值检测模式生成的。抽取模式决定了 1024→64 时「保留哪 64 个、值是多少」，换一种模式（如均匀抽取）输出就完全不同，必然对不上黄金值。固定模式是为了让被测 RTL 与生成黄金值的模型在同一种算法下比对。

**练习 2**：`check` 任务打印的 `[PASS]/[FAIL]` 和 `run_test` 里 `grep -Ec '^\[PASS...\]'` 是什么关系？

**参考答案**：前者是 testbench 内部的逐条断言输出，后者是 bash 脚本读这些输出做计数。`run_test` 不自己判断对错，它完全信任 testbench 打印的标记——所以 testbench 必须用 `[PASS]/[FAIL]` 这种可被正则稳定识别的前缀。这是一种「测试脚本与 testbench 之间的简单契约」。

---

### 4.4 两类黄金值：自生成 vs 真实数据

#### 4.4.1 概念说明

本节回答实践任务里的核心问题：**为什么「真实数据 exact-match」能抓到「自生成黄金比对」抓不到的架构错误？**

先定义两类黄金值：

- **自生成黄金值（self-blessing）**：用一个数学模型**同时**生成「输入信号」和「期望输出」，再让 RTL 跑同一个输入、比输出。代表是 `gen_mf_golden_ref.py`——它用 DC、单频、脉冲等合成信号，按 `output = IFFT(FFT(sig) * conj(FFT(ref)))` 算出期望值。
- **真实数据黄金值**：用一份**独立**的参考模型（`golden_reference.py`）跑**真实雷达数据**，把结果冻结成 hex；RTL 只负责被比对，不参与生成黄金值。代表是 `tb_fullchain_realdata.v`。

「自生成自比对」的根本缺陷是**循环论证（tautology）**：模型用它自己的数学来生成期望值，又用同一个数学来评判。如果模型对算法的理解本身就错了（比如把共轭取反了、IFFT 忘了缩放、用了错的 FFT 点数），那么「生成的期望」和「RTL 若照着同一个错误实现」会**一起错、却互相一致**——测试照样 PASS。它只能证明「RTL 和模型用了同样的算法」，不能证明「这个算法是对的」。

#### 4.4.2 核心流程

用一个具体例子说明两类测试的差异——**Doppler 从「单 32 点 FFT」改成「双 16 点 FFT」**（这正是 `run_regression.sh` 注释里举的架构改动）：

```
场景: 工程师把 Doppler 从 single-32pt 改成 dual-16pt (staggered PRI)，但 RTL 里忘了
      在两个子帧之间正确切换，导致子帧1 用错了 chirp 序号。

自生成黄金值 (gen_mf_golden_ref 风格):
  合成信号 = 单频 tone (bin 5)
  模型按 "它以为的" 算法生成期望值
  RTL 按同一个 "以为的" 算法实现
  → 两者都把 tone 算成一个尖峰，互相一致 → PASS  (但真实雷达会测错速度!)

真实数据 exact-match (tb_fullchain_realdata):
  冻结黄金 = golden_reference.py 用 [正确] 的 dual-16pt 模型跑真实 ADI 数据生成
  RTL 用了 [错误] 的子帧切换
  → 真实数据有 staggered PRI 结构、有真实目标 → 错误切换让 Doppler bin 错位
  → RTL 输出 ≠ 冻结黄金 → 大量不匹配 → FAIL  ✓ 抓到了
```

合成单频信号「太干净」：它的 FFT 就是一个尖峰，无论你怎么切子帧，尖峰还是尖峰，掩盖了架构错误。真实数据「脏」：它有真实的 chirp 结构、PRI 交替、目标/杂波/噪声混合，**任何架构层面的错误都会让输出在真实数据上显形**。

#### 4.4.3 源码精读

自生成黄金值的工作方式——合成信号 + 同模型算期望：

[gen_mf_golden_ref.py:1-20](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/tb/gen_mf_golden_ref.py#L1-L20) —— 文档字符串写明：用合成 test case（DC/单频/脉冲自相关与互相关）为 `matched_filter_processing_chain` 生成 hex，每个 case 产出 6 个 hex（`sig_i/q`、`ref_i/q`、`out_i/q`）。注意「输入」和「期望输出」是**同一个脚本、同一次运行**产出的。

匹配滤波的数学定义（这就是「模型以为的算法」）：

[gen_mf_golden_ref.py:49-66](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/tb/gen_mf_golden_ref.py#L49-L66) —— `matched_filter` 函数：`S=FFT(sig)`、`R=FFT(ref)`、`product = S * conj(R)`、`result = IFFT(product)`。如果这个公式里 `conj` 写错、或漏了某个归一化，**生成的 out hex 会带着同一个错误**，而用它做基准的比对仍会 PASS。

四个合成 case 的本质——都是「可预测的干净信号」：

[gen_mf_golden_ref.py:129-210](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/tb/gen_mf_golden_ref.py#L129-L210) —— Case 1 DC（全 0x1000）、Case 2 单频（bin 5）、Case 3 延迟单频、Case 4 脉冲（delta）。这些信号的共同点是「频谱结构简单、可手算」，适合验证「RTL 的 FFT/乘法/IFFT 数据通路是否接通」，但**不适合验证「架构选择是否正确」**——因为它们没有真实雷达的 staggered PRI、距离-多普勒耦合等结构。

对照：真实数据黄金值的独立性与真实锚点：

[golden_reference.py:29-82](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/tb/cosim/real_data/golden_reference.py#L29-L82) —— 这里把 RTL 的所有关键参数（NCO 相位字 `0x4CCCCCCD`、CIC 5 级 / 抽取 4 / 增益移位 10、FIR 32 抽头系数、Doppler `dual 16-pt` + Hamming 窗系数）逐项列成 Python 常量，注释反复强调「exact match to RTL parameters」。它是一份**独立重写**的 NumPy 实现，且（按其文档字符串）用真实 ADI CN0566 数据做过检测验证。两份独立实现（Python 模型 + Verilog RTL）在真实数据上 bit-for-bit 一致，是比「一份实现和自己生成的副本一致」强得多的证据。

`validate_mem_files.py` 则是第三类校验——**不比对运算输出，而是比对静态数据文件本身是否符合雷达物理参数**：

[validate_mem_files.py:23-39](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/tb/cosim/validate_mem_files.py#L23-L39) —— 把 `F_IF=120MHz`、`CHIRP_BW=20MHz`、`FS_ADC=400MHz`、`FS_SYS=100MHz`、`T_LONG_CHIRP=30µs`、`CIC_DECIMATION=4`、`FFT_SIZE=1024`、`DOPPLER_FFT_SIZE=16` 等系统参数作为基准。

它最硬的一条检查是 FFT 旋转因子的「数学定义比对」：

[validate_mem_files.py:117-154](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/tb/cosim/validate_mem_files.py#L117-L154) —— 对 `fft_twiddle_1024.mem` 的每个值，用 `round(cos(2π·k/1024)·32767)` 重算，要求 `max_err ≤ 1 LSB`。这里「期望值」来自**纯数学公式**（余弦的定义），不依赖任何雷达模型，是不可辩驳的 ground truth——这是比真实数据更底层的一种独立校验。

三类校验的关系可以这样总结：

| 校验类型 | 代表 | 期望值来源 | 能抓什么 |
|----------|------|-----------|----------|
| 自生成黄金 | `gen_mf_golden_ref.py` | 同一个模型自己算 | 数据通路是否接通、明显接线错 |
| 真实数据 exact-match | `tb_fullchain_realdata.v` | 独立模型跑真实雷达数据 | 架构错误、定点细节偏差 |
| 数学定义校验 | `validate_mem_files.py` | 余弦等纯数学公式 | 数据文件本身的正确性 |

#### 4.4.4 代码实践

**实践目标**：亲手对比两类黄金值的「抓错能力」，理解真实数据的不可替代性。

**操作步骤**（思想实验 + 源码阅读，不实跑）：

1. 读 `gen_mf_golden_ref.py` 的 Case 2（单频自相关，第 152-165 行）。回答：如果把 `matched_filter` 里的 `np.conj(R)` 改成 `R`（取错了共轭），生成的 `out_i/q` hex 会变，但「用这个错误 hex 做基准去比对一个同样取错共轭的 RTL」会 PASS 还是 FAIL？
2. 读 `tb_fullchain_realdata.v` 第 379-416 行的比对逻辑。回答：如果有人把 Doppler 的双 16 点改回单 32 点，`cap_out` 和冻结的 `ref` 会在哪一维度最先出现偏差？（提示：子帧打包格式 `{sub_frame, bin[3:0]}`，见 u4-l4。）
3. 读 `validate_mem_files.py` 的 `test_twiddle_16`（第 140-154 行）。回答：16 点 FFT 旋转因子表只有 4 项（四分之一波长 ROM），这 4 项期望值是怎么算出来的？它依赖任何「模型」吗？

**需要观察的现象 / 思考结论**：

- 第 1 步：会 **PASS**。这正是自生成黄金值的致命缺陷——模型和 RTL 共享同一个错误时，错误被「自我证明」为正确。
- 第 2 步：最先在 Doppler bin 的子帧分布上偏差。真实数据里不同距离门的目标在两个子帧里应有不同的能量分布，单 32 点 FFT 会把这种结构抹平，与冻结黄金的 2048 个点大面积不符。
- 第 3 步：4 项期望值是 `round(cos(2π·k/16)·32767)`，k=0..3，来自余弦的数学定义，**不依赖任何模型**，是不可错的标准。

**预期结果**：通过这三个问题，读者应能用自己的话讲清楚「真实数据 + 独立模型」为何比「自生成」更可信。无需本地运行即可得出结论。

#### 4.4.5 小练习与答案

**练习 1**：用一句话概括「自生成黄金值」为什么抓不到架构错误。

**参考答案**：因为它用同一个模型（带着同一个对架构的错误理解）既生成期望值又评判结果，模型错则期望值与 RTL 一起错、却互相一致，形成「自己给自己盖章」的循环论证。

**练习 2**：如果把 `tb_fullchain_realdata.v` 的 `MAX_ERROR` 从 0 改成 5（允许 5 LSB 偏差），这个测试还能保证「架构正确」吗？

**参考答案**：不能保证同等强度。`MAX_ERROR=0` 时，任何一个 bin 偏 1 LSB 都 FAIL，能发现微小的定点偏差或架构漂移；放宽到 5 会放过小范围错误。对于「验证架构没被偷偷改掉」这个目的，零容差是有意为之的保守选择——放宽容差等于主动放弃一部分检出能力。真实数据 exact-match 之所以敢用零容差，是因为 `golden_reference.py` 已经精确复现了所有定点细节，理论上不该有任何偏差。

**练习 3**：`validate_mem_files.py` 检查旋转因子用 `max_err ≤ 1 LSB` 而不是 `==0`，为什么允许 1 LSB？

**参考答案**：因为 `.mem` 文件里存的是 `round(cos(θ)·32767)` 的整数结果，而 Python 侧也是 `round(...)`，两边的四舍五入在边界上（如 0.5 附近）可能因浮点细节差 1 个最低位。允许 1 LSB 是对「定点量化舍入误差」的合理容忍，而不是对「算法错误」的让步。

## 5. 综合实践

把本讲的知识串起来，完成一个「回归套件体检」小任务：

1. **跑全量回归**：在 `9_Firmware/9_2_FPGA/` 下运行 `./run_regression.sh`（不带 `--quick`），记录五个阶段的测试数与通过数，以及总耗时。
2. **定位真实数据测试**：在输出里找到 `Doppler Real-Data (ADI CN0566, exact match)` 和 `Full-Chain Real-Data (decim→Doppler, exact match)` 两行，确认它们在 `--quick` 模式下也必须通过（不被跳过）。
3. **手动单跑全链路 testbench**：按 4.3.4 的命令单独编译运行 `tb_fullchain_realdata`，把 `SUMMARY` 里的 `Exact match: X / 2048` 和 `Max error (I/Q)` 抄下来。
4. **构造一个「架构错误」思想实验**：假设你要把 Doppler 从双 16 点改回单 32 点，列出：(a) 哪些 Phase 1/3 的合成信号单元测试**可能仍然 PASS**（因为单频/脉冲信号对子帧结构不敏感）；(b) 哪个测试**一定会 FAIL**（真实数据全链路）；(c) 这时 `validate_mem_files.py` 会不会报警（提示：它查的是 `fft_twiddle_16.mem`，不会因为 Doppler 架构改动而变）。
5. **写一段结论**：用本讲的术语（自生成 / 真实数据 / exact-match / Vivado 级 lint）说明，为什么这个回归套件「Phase 0 lint + 多层次测试 + 真实数据 exact-match」的组合，比单靠任何一类测试都更可靠。

预期：第 4 步的结论应能体现「真实数据 exact-match 是唯一能稳定抓架构错误的那一层，其余层各自覆盖别的盲区」。若本地没有 iverilog，第 1-3 步标注「待本地验证」，第 4-5 步是纯分析，可直接完成。

## 6. 本讲小结

- `run_regression.sh` 把 FPGA 回归组织成 **Phase 0 lint + 四个测试阶段**（单元/集成/信号处理/基础设施），公用 `run_test` 函数完成「编译→运行→数 `[PASS]/[FAIL]` 标记」，退出码 0/1 直接反映成败，`--quick` 只跳过六个最慢的集成测试。
- **Vivado 级 lint** 的精髓是用 iverilog 的 `-Wall` 输出加自定义正则/awk，把 Vivado 综合才报的 ERROR（位选择越界、端口位宽不匹配）和 `SYNTH-6`（`case` 缺 `default`）在免费的 iverilog 阶段提前拦截，lint 失败直接中止回归。
- `tb_fullchain_realdata.v` 是高价值测试：用真实 ADI CN0566 雷达数据跑「距离抽取 + Doppler」全链路，与冻结的 Python 黄金值逐比特比对，`MAX_ERROR=0` 要求 2048 个输出全部精确相等。
- 黄金值分两类：**自生成**（`gen_mf_golden_ref.py`，合成信号 + 同模型算期望，易陷入循环论证）与**真实数据**（`golden_reference.py` 独立实现 + 真实雷达数据锚点，能抓架构错误）；`validate_mem_files.py` 是第三类——用纯数学定义校验静态数据文件。
- 「真实数据 exact-match」能抓自生成抓不到的架构错误，因为真实数据的 staggered PRI、距离-多普勒耦合等结构会放大架构偏差，而合成单频/脉冲信号「太干净」会掩盖它。

## 7. 下一步学习建议

- 顺着「测试与验证体系」继续读 **u11-l3（跨层契约测试）**：那里把 Python↔Verilog↔C 三层的契约一致性也做成了独立真值推导，与本讲「两份独立实现互相比对」的思想一脉相承，但跨了更多语言。
- 若对被测的信号链本身感兴趣，回看 **u4-l3（距离抽取与 MTI）** 和 **u4-l4（Doppler 双 16 点 FFT）**，理解 `tb_fullchain_realdata` 里 decimator 的峰值模式与 Doppler 的子帧打包格式 `{sub_frame, bin[3:0]}` 是怎么来的。
- 想了解形式化验证如何补充仿真覆盖，预习 **u14-l1（形式化验证 SymbiYosys）**：仿真是「跑有限个用例」，形式化是「证明性质恒成立」，两者与本讲的回归测试共同构成 FPGA 的完整验证网。
- 若要为新增功能加测试，可参照 `gen_mf_golden_ref.py` 为新模块造合成单元测试（快速、覆盖通路），并尽量为关键架构决策补一个真实数据 exact-match 用例（慢、但能护住架构不被偷偷改坏）。
