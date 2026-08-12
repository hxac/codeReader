# AIE 与 PL 仿真流程

## 1. 本讲目标

在前面几讲里，我们已经看清了反投影的整张图：主机怎么喂数据（u3-5）、AIE 图怎么拓扑（u4）、内核怎么算（u5）、PL 包路由器怎么把乱序图像重排回 DDR（u6）。但有一个问题一直被搁置：**在把设计真正烧到 VCK190 板卡之前，怎么验证它是对的、以及跑得多快、耗多少电？**

本讲要回答的就是「验证」这一环。学完后你应当掌握：

1. 三个 AIE 仿真目标 `aiesim` / `aiesim_profile` / `aiesim_xpe` 分别做什么、产出什么文件。
2. `design/aie/graph.cpp` 里那段被 `__AIESIM__` / `__X86SIM__` 宏保护的 `main()` 是一份**独立的仿真测试台（testbench）**，它如何读 CSV、驱动图、跑 `PULSES` 次，以及它的投递顺序与真实主机 `bp()` 的逐行对应关系。
3. `sw_emu` / `hw_emu` / `hw` 三种 `TARGET` 的本质区别，以及 Makefile 为何要拦截 `aiesim + sw_emu` 和 `run + hw` 这两种非法组合。

本讲是「仿真、度量与优化」单元的第一讲，下一讲 u8-l2 会接着讲性能与功耗度量。

## 2. 前置知识

在开始前，请确保你已经理解以下概念（前面讲义已建立）：

- **TARGET 与 AIE_TARGET/PL_TARGET 的映射**：根 Makefile 用一个用户变量 `TARGET`（hw/hw_emu/sw_emu）驱动整个构建，它会被翻译成 AIE 侧的 `AIE_TARGET` 和 PL 侧的 `PL_TARGET`（见 u1-l3）。
- **GMIO 与 PLIO 端口**：GMIO 是 DDR↔AIE、经 NoC 的 DMA 通道；PLIO 是 AIE↔PL 的 128 位 AXI4-Stream 直连（见 u2-l2、u4-l3）。
- **主机 `bp()` 的投递结构**：逐脉冲用 `gm2aie_nb` 非阻塞投递 slowtime / RC / 像素，并用 RTP 在末脉冲触发 dump（见 u3-l5）。
- **PL 包路由器仿真**：它用 aiesim 录制的 PLIO trace CSV 作回放激励（见 u6-l2）。

如果你对「为什么仿真要分这么多档」还没有直觉，记住一句话即可：**越接近真实硬件，越慢但越准；越接近 x86，越快但越不准**。本讲就是在讲这条光谱上的几个刻度。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `Makefile` | 定义 `aiesim` / `aiesim_profile` / `aiesim_xpe` / `plsim_router` / `run` 等仿真目标，并用校验关卡拦截非法 TARGET 组合 |
| `design/aie/graph.cpp` | 含一份被 `__AIESIM__`/`__X86SIM__` 保护的仿真 `main()`，是 aiesimulator/x86sim 的独立测试台 |
| `design/exec_scripts/run_script_sw_emu.sh` | 软件仿真运行脚本，设置 `XCL_EMULATION_MODE=sw_emu` 后调用主机 elf |
| `design/aie/graph.h` | ADF 图声明，仿真 main 与真实主机共用同一张图对象 `bpGraph` |

下面按三个最小模块展开。

---

## 4. 核心概念与源码讲解

### 4.1 AIE 仿真三目标：aiesim / aiesim_profile / aiesim_xpe

#### 4.1.1 概念说明

Vitis 里仿真 AIE 的核心工具是 `aiesimulator`（简称 aiesim）。它是一个**周期级（cycle-accurate）的 AI Engine 阵列模拟器**：会真实建模 tile 的局部存储、流交换网络（stream switch）、cascade 通路与调度时序，因此既能验证功能，也能给出近似真实的吞吐与停顿（stall）报告。

但 aiesim 有一个硬前提：**它只能跑用 `hw` 或 `hw_emu` 编译出来的 AIE 代码**（即 `AIE_TARGET=hw`）。原因是周期级模型需要 AIE 编译器产出的、面向真实硬件的调度与微码；而 `sw_emu` 把 AIE 编译成 `x86sim`（普通 x86 C++ 函数），aiesim 根本无法加载它。这一点是后面 Makefile 校验关卡的根因。

围绕 aiesimulator，本项目提供了三个递进的目标，信息量越来越大、但运行成本也越来越高：

| 目标 | 做什么 | 关键产出 |
| --- | --- | --- |
| `aiesim` | 跑 aiesimulator，不带任何附加选项 | 功能验证 + PLIO 输出 trace CSV（喂给 PL 仿真） |
| `aiesim_profile` | 加 `--profile --dump-vcd aie` | 内核 `printf` 可见、profile 报告、`aie.vcd` 波形 |
| `aiesim_xpe` | 用 `vcdanalyze` 把 `aie.vcd` 转成 XPE 文件 | 供功耗估计的 `.xpe` 文件 |

#### 4.1.2 核心流程

三个目标共用同一条前置依赖 `libadf.a`（AIE 编译产物），区别只在调用 aiesimulator / vcdanalyze 时的选项。流程如下：

```
design/aie/*.cc + common.h
        │  v++ --mode aie -t ${AIE_TARGET}   （AIE_TARGET 必须是 hw）
        ▼
   libadf.a  + Work/  （Work/ 含 aiesimulator 需要的微码与图描述）
        │
        ├──[aiesim]──────────► aiesimulator --pkg-dir=Work --input-dir=...
        │                         │
        │                         ▼
        │                   aiesimulator_output/aie_to_plio_switch_*.csv
        │
        ├──[aiesim_profile]─► aiesimulator --profile --dump-vcd aie ...
        │                         │
        │                         ▼
        │                   aie.vcd  +  profile 报告
        │
        └──[aiesim_xpe]─────► vcdanalyze --vcd aie.vcd --xpe
                                  │
                                  ▼
                            *.xpe （功耗估计输入）
```

注意 `aiesim_xpe` **依赖 `aie.vcd`**，而 `aie.vcd` 只有带 `--dump-vcd` 才会生成，所以必须先跑过一次 profile 档（或由 Makefile 的间接规则自动补跑）。

#### 4.1.3 源码精读

最朴素的 `aiesim` 目标，只是切到构建目录、调用 aiesimulator，并把输出 tee 到日志：

[Makefile:148-156](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L148-L156) — 调用 `aiesimulator`，`--pkg-dir` 指向 AIE 编译产出的 `Work/` 目录，`--input-dir` 指向 PL 仿真的输出目录（见 4.1.4 与模块 4.2 的说明）。这一档**不带**任何 profiling 选项，跑得最快，常用于「我只想要 PLIO trace CSV 去喂 PL 仿真」。

`aiesim_profile` 在此基础上加两个选项：

[Makefile:160-169](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L160-L169) — `--profile` 的额外收益是：**让 AIE 内核代码里的 `printf` 能在控制台出现**（注释里特意说明了这一点），并生成吞吐/停顿分析报告；`--dump-vcd aie` 则把整个阵列的信号翻转录成 `aie.vcd` 波形文件。

`aiesim_xpe` 不再跑仿真，而是**事后处理**波形：

[Makefile:175-184](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L175-L184) — 用 `vcdanalyze --vcd aie.vcd --xpe` 把波形里各 tile 的翻转活动度提取成 XPE（Xilinx Power Estimator）文件。它把 `aie.vcd` 列为依赖；Makefile 为此专门补了一条间接规则来生成 `aie.vcd`：

[Makefile:273-282](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L273-L282) — 这条 `${AIESIM_BUILD_DIR}/aie.vcd` 规则其实就是「带 profile 的 aiesim」的重述，保证 `make aiesim_xpe` 时若 `aie.vcd` 不存在会自动先跑一遍 profile 仿真。

> 这三个目标都是 u1-l3 所说的「验证旁路」：它们不进入 `libadf.a → .xo → XSA → SD 卡镜像` 的主线构建链，只为功能验证、波形/吞吐分析与功耗估计服务。

#### 4.1.4 代码实践

**实践目标**：搞清 `aiesim` 与 `plsim_router` 之间通过文件交接的依赖关系。

**操作步骤**：

1. 阅读 [Makefile:128-138](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L128-L138) 的 `plsim_router` 目标。
2. 找到那条「CSV 存在性守卫」：`if [ ! -f .../aie_to_plio_switch_0_0.csv ]; then $(MAKE) aiesim; fi`。
3. 对照 `aiesim` 目标里 `--input-dir` 指向的 `plsimulator_output` 目录。

**需要观察的现象**：

- `plsim_router` 需要 aiesim 产出的 `aie_to_plio_switch_0_0.csv` 作为 PL 内核 testbench 的回放激励（这正是 u6-l2 讲过的「站在 AIE 视角」思路）。
- 反过来 `aiesim` 的 `--input-dir` 又指向 `plsimulator_output`。两者形成「AIE 仿真的 PLIO 输出 → 喂给 PL 仿真」的交接；首次运行时 PL 侧目录可能为空，但 aiesim 仍会把 AIE→PL 方向的 PLIO 输出写到 `aiesimulator_output/` 下。

**预期结果**：你能用自己的话画出 `design/aie → libadf.a → aiesim → PLIO CSV → plsim_router (csim) → output_img.csv` 这条依赖链，并解释为什么 `plsim_router` 把 `aiesim` 当成「按需」前置（CSV 在就跳过）。

> 待本地验证：是否真的能依次跑通 `make aiesim` 与 `make plsim_router`，需要本机已装好 Vitis 与 VCK190 平台、并 `source helper_scripts/env_setup.sh`。本环境无法实际运行 aiesimulator。

#### 4.1.5 小练习与答案

**练习 1**：如果只想确认 AIE 内核算出来的图像数值对不对（功能正确性），该用哪个目标？如果还想看每个内核跑了多少周期、停顿在哪，又该用哪个？

**参考答案**：功能正确性用 `aiesim` 即可（最快）；要看周期/停顿/吞吐以及让内核 `printf` 生效，用 `aiesim_profile`（带 `--profile --dump-vcd`）。

**练习 2**：`make aiesim_xpe` 在 `aie.vcd` 不存在时会失败吗？

**参考答案**：不会。因为 Makefile 既有 `${AIESIM_BUILD_DIR}/aie.vcd` 这条间接规则，`aiesim_xpe` 又把它列为依赖（[Makefile:175](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L175)），make 会自动先补跑一次带 `--dump-vcd` 的仿真来生成它。

---

### 4.2 graph.cpp 里的仿真 main：一份独立的测试台

#### 4.2.1 概念说明

这是本讲最关键、也最容易被忽略的一个认知：**本项目里有两个 `main()` 函数**。

- `design/host/main.cpp::main` —— 真正的 ARM 主机应用，被交叉编译成 `sar_backproject.elf`，最终跑在板卡上（见 u3-1）。
- `design/aie/graph.cpp::main` —— **一份仿真测试台（testbench）**，只在用 aiesimulator 或 x86sim 编译时才生效。

为什么会存在第二份 `main`？因为 aiesimulator/x86sim 在跑 AIE 图时，需要一个「驱动图」的宿主程序来读数据、调 `gm2aie_nb`、发 `run`。这个驱动程序在仿真里**不能**是那份依赖 XRT 运行库（`xrt::device`、`load_xclbin`…）的 ARM 主机代码——仿真环境里根本没有 xclbin 与真实 device。所以作者在 `graph.cpp` 里另写了一份精简的、用 ADF 原生 `GMIO::malloc` 与 `bpGraph.gm2aie_nb` 的测试台。

这层隔离靠一个宏守卫实现：

[design/aie/graph.cpp:12](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L12) —— `#if defined(__AIESIM__) || defined(__X86SIM__)`。只有当 AIE 编译器为目标 `hw`（aiesimulator，会定义 `__AIESIM__`）或 `x86sim`（sw_emu，会定义 `__X86SIM__`）编译时，这段 `main` 才会被编进去。在构建真实主机时，`graph.cpp` 根本不参与主机编译（主机编译的是 `design/host/*.cpp`），所以两份 `main` 不会冲突。

#### 4.2.2 核心流程

这份仿真 `main` 是一个**自包含的端到端反投影脚本**，它把主机三大函数的功能在内联代码里重做了一遍：

```
仿真 main 流程
─────────────────────────────────────────
1. bpGraph[inst].init()                      ← 初始化图
2. 读 slowtime CSV → broadcast_data_array    ← 复刻 host::fetchRadarData 的 slowtime 段
3. 读 RC CSV（正则拆 a+bi）→ rc_array        ← 复刻 host::fetchRadarData 的 RC 段
4. atan2 + unwrap + az_res/half_az_width     ← 复刻 host::genTargetPixels 的几何推导
5. 生成 xyz_px_array 网格                     ← 复刻 host::genTargetPixels 的像素网格
6. bpGraph[inst].run(PULSES)                 ← 启动图（注意：bounded run N）
7. 逐脉冲投递：
     gmio_in_st.gm2aie_nb(...)               ← slowtime 一次
     for pulse: {
       gmio_in_rc.gm2aie_nb(...)             ← RC 每脉冲
       for sw: gmio_in_xyz_px.gm2aie_nb(...) ← 像素每 switch 每脉冲
       for kern: update(rtp_dump_img_in,...) ← RTP：末脉冲=1 否则=0
     }
8. bpGraph.wait(); bpGraph.end()             ← 等 PLIO 写完文件后结束
```

注意第 6 步：仿真用的是 `run(PULSES)`（有界运行 N 次），而真实主机 `runGraphs()` 用的是 `run(0)`（自由运行，见 u3-l5）。这是仿真与上板的**一个真实差异**：仿真需要一个明确的终止计数让模拟器知道何时收尾。

#### 4.2.3 源码精读

宏守卫与全局图对象（注意 `INSTANCES = 1`，与主机侧一致）：

[design/aie/graph.cpp:9-13](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L9-L13) —— 仿真 main 整段都被宏包住；`bpGraph[INSTANCES]` 与主机侧共用 `design/aie/graph.h` 里定义的同一张图。

读 slowtime 与 RC（复刻 `fetchRadarData`，复数正则与 u3-3 完全同源）：

[design/aie/graph.cpp:96-129](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L96-L129) —— RC 用 `std::regex_search` 配 `a+bi` 正则拆出实虚部，逻辑与主机 `fetchRadarData()` 一模一样，只是这里写进了 `GMIO::malloc` 出来的 `rc_array`，而不是主机的 `xrt::aie::bo`。

几何推导与像素网格（复刻 `genTargetPixels` + 内联的 `unwrap`）：

[design/aie/graph.cpp:131-171](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L131-L171) —— `unwrap()` 在本文件 [design/aie/graph.cpp:18-45](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L18-L45) 内联定义，与主机 `genTargetPixels` 里的解卷绕算法一致；随后生成 `PULSES×RC_SAMPLES` 的 X/Y/Z 像素网格。

**核心：驱动图的投递序列**（与主机 `bp()` 的对应关系就在这一段）：

[design/aie/graph.cpp:174-205](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L174-L205) —— 逐行解读：

- 第 175 行 `bpGraph[inst].run(PULSES)`：启动图，有界运行 602 次（主机是 `run(0)` 自由运行）。
- 第 188 行 `gmio_in_st.gm2aie_nb(broadcast_data_array, ...)`：slowtime 整块投递**一次**（在脉冲循环之外），对应主机 `bp()` 里 slowtime 也只送一次。
- 第 191–196 行的 `for pulse_idx` 循环：每个脉冲做三件事——
  - 第 193 行 `gmio_in_rc.gm2aie_nb(rc_array + pulse_idx*RC_SAMPLES, ...)`：按距离线逐条投递 RC；
  - 第 194–196 行 `for sw_id`：按 switch 切片投递像素，`xyz_px_array + sw_id*px_per_demux_kern*3`，其中 `px_per_demux_kern = (PULSES*RC_SAMPLES)/AIE_SWITCHES`（[design/aie/graph.cpp:179](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L179)）；
  - 第 198–204 行 `for kern_id`：用 RTP `rtp_dump_img_in` 控制 dump——**末脉冲（`pulse_idx == PULSES-1`）置 1 才 dump，其余置 0 只累加**，与主机 `bp()` 的 RTP 逻辑完全一致。
- 第 210–211 行 `bpGraph.wait(); bpGraph.end();`：注释明确说明 `wait()` 是**为了让 PLIO 输出完整写到文件，否则文件会是空的**。这是仿真特有的一步——PLIO 输出最终落到 `aie_to_plio_switch_*.csv`，必须等图彻底跑完才 flush。

#### 4.2.4 代码实践

**实践目标**：把仿真 `main` 的 `gm2aie_nb` 投递顺序与真实主机 `bp()` 逐条对账。

**操作步骤**：

1. 打开本讲的 [design/aie/graph.cpp:188-205](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L188-L205)。
2. 回顾 u3-5 总结的主机 `bp()` 四层循环结构（slowtime 一次 → 每脉冲 RC → 每 switch 像素 → 每 kern RTP）。
3. 填写下表（左侧为仿真，右侧请用一句话写出主机的对应做法）：

| 仿真 main（graph.cpp） | 主机 bp()（sar_backproject.cpp，见 u3-5） |
| --- | --- |
| `gmio_in_st.gm2aie_nb(...)`（循环外一次） | _请你填写_ |
| `gmio_in_rc.gm2aie_nb(...)`（每脉冲） | _请你填写_ |
| `bpCluster[sw_id].gmio_in_xyz_px.gm2aie_nb(...)`（每 switch 每脉冲） | _请你填写_ |
| `update(rtp_dump_img_in[kern_id], 1/0)`（末脉冲=1） | _请你填写_ |
| `bpGraph.run(PULSES)` | 主机用 `run(0)`，差异在哪？ |

**需要观察的现象 / 预期结果**：

- 投递顺序、切片方式（按 switch 切像素）、RTP「末脉冲才 dump」三者在两份代码里**一一对应**——这正说明仿真 main 是主机 `bp()` 的忠实精简复刻。
- 唯一明显差异是 `run(PULSES)` vs `run(0)`：仿真要有界计数以便模拟器收尾，主机靠 `run(0)` 自由运行、由主机自己控制节奏。

> 参考答案（自测后再看）：上表四行依次对应主机 `bp()` 里的「slowtime 整块投一次」「RC 逐距离线投递」「像素按 switch 切片每脉冲重推」「RTP update 在末脉冲置 1」；`run` 差异如上所述。

#### 4.2.5 小练习与答案

**练习 1**：为什么这份仿真 `main` 要把 `unwrap`、读 CSV、生成像素网格这些主机已经写过的逻辑**再写一遍**，而不是直接 `#include` 主机的代码？

**参考答案**：因为主机代码（`design/host/*.cpp`）依赖 XRT 运行库（`xrt::device`、`xrt::bo`、`xrt::graph`），那是面向真实 device/xclbin 的；而 aiesimulator/x86sim 的测试台必须用 ADF 原生 API（`GMIO::malloc`、`bpGraph.gm2aie_nb`）来驱动同一张图，环境完全不同，无法直接复用主机的类。于是作者把纯数值计算（读 CSV、解卷绕、生成像素）这部分与 XRT 无关的逻辑在内联代码里复刻了一份。

**练习 2**：若把 [design/aie/graph.cpp:12](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L12) 的宏守卫去掉，会出什么问题？

**参考答案**：在用 aiesim/x86sim 编译时没区别（宏本就成立）；但这份 `main` 也可能在别的 AIE 编译档里被编入而产生意外符号，更重要的是它失去了「仅在仿真态生效」的语义保护——守卫存在的意义就是确保这段驱动代码只在仿真测试台里出现，绝不污染真实硬件构建。去掉它会让「哪份 main 在哪个档位生效」变得不可判定。

---

### 4.3 三种 TARGET：sw_emu / hw_emu / hw 与 Makefile 校验

#### 4.3.1 概念说明

`TARGET` 是贯穿整个 Makefile 的总开关（[Makefile:20](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L20) 默认 `hw`）。它首先被翻译成 AIE 与 PL 各自的子目标：

[Makefile:27-33](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L27-L33) —— 关键映射：**只有 `sw_emu` 会把 AIE/PL 都降级到 x86**（`AIE_TARGET=x86sim`、`PL_TARGET=x86`）；`hw` 与 `hw_emu` 都让 AIE/PL 保持 `hw`。

三种 TARGET 的本质区别如下表：

| TARGET | AIE_TARGET / PL_TARGET | 跑在哪 | 速度 | 时序精度 | 典型用途 |
| --- | --- | --- | --- | --- | --- |
| `sw_emu` | x86sim / x86 | 开发机 x86 CPU | 最快 | **无**（纯功能性） | 快速验证算法逻辑 |
| `hw_emu` | hw / hw | 开发机上的硬件仿真器 | 慢 | 周期近似 | 验证 PL+AIE 时序行为 |
| `hw` | hw / hw | **真实 VCK190 板卡** | 真实速度 | 真实 | 上板运行、度量、部署 |

> 注意 `hw` 与 `hw_emu` 在 **编译产物**上都是 `hw`，区别在于运行方式：`hw` 烧到板卡真跑，`hw_emu` 在主机上的硬件仿真器里跑。这也解释了为什么 aiesim 只排斥 `sw_emu`，而同时接受 `hw` 与 `hw_emu`。

#### 4.3.2 核心流程：Makefile 的两道校验关卡

Makefile 在文件开头设了两道关卡，拦掉两种「编译能过但运行必错」的非法组合。它们的判定逻辑都是「检查 `MAKECMDGOALS`（命令行上敲的目标）里是否含某个目标，且 `TARGET` 是否为某个值」。

**关卡一：禁止 `aiesim` 配合 `sw_emu`**

[Makefile:59-70](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L59-L70) —— 错误信息本身已经把道理讲清楚：aiesimulator 是 AI Engine 阵列的**周期级**模型，需要 `hw`/`hw_emu` 提供的面向硬件的微码；而 `sw_emu` 把 AIE 编成 `x86sim`（x86 快速功能性仿真，无时序），aiesimulator 根本无法加载它。所以 `make TARGET=sw_emu aiesim` 会被直接 `$(error)` 拦下。

**关卡二：禁止 `run` 配合 `hw`**

[Makefile:72-77](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L72-L77) —— `run` 目标的设计初衷是**在开发机上跑仿真**：

[Makefile:116-117](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L116-L117) —— `run` 调用的是 `${EMU_LAUNCH_FILE}`（即 `launch_${TARGET}.sh`，由 `v++ -p` 打包时生成的仿真启动器）。这套启动器只对 `sw_emu`/`hw_emu` 有意义。`hw` 档意味着真实硬件——在真实板卡上要靠 u7-3 讲的 SD 卡启动 + 直接跑 `sar_backproject.elf`，而不是在开发机上 `make run`，所以 `make TARGET=hw run` 也被拦下。

#### 4.3.3 源码精读：仿真运行脚本如何设置仿真模式

仿真运行脚本通过环境变量 `XCL_EMULATION_MODE` 告诉 XRT 运行库「现在跑的是哪一档仿真」：

[design/exec_scripts/run_script_sw_emu.sh:3](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/exec_scripts/run_script_sw_emu.sh#L3) —— `export XCL_EMULATION_MODE=sw_emu`。对比 [hw_emu 版本](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/exec_scripts/run_script_hw_emu.sh#L3) 把它设成 `hw_emu`，而 [hw 版本](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/exec_scripts/run_script_hw.sh#L3) 根本不设这个变量（真实硬件不需要）。

三个脚本在调用 elf 时都补了一个硬编码的 `a.xclbin` 作为首参（[run_script_sw_emu.sh:16](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/exec_scripts/run_script_sw_emu.sh#L16)），这正是 u3-1 讲过的「参数转交分层」：脚本收用户 4 个参数，替其补上 xclbin 凑满 elf 的 5 个槽位。

最后，`package` 目标会按 TARGET 决定是否打包 DTB，这也是一个 TARGET 相关的细节：

[Makefile:86-90](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L86-L90) —— 只有 `hw` 才打包自定义 DTB；仿真档（QEMU）用自定义 DTB 会让内核崩溃，故不打包。这进一步体现了「hw 与仿真档在打包行为上不同」。

#### 4.3.4 代码实践

**实践目标**：亲手触发两道校验关卡，观察 Makefile 给出的错误信息，并解释其根因。

**操作步骤**：

1. 先设好平台环境（本机若装了 Vitis）：`source helper_scripts/env_setup.sh`（让 `PLATFORM` 不为空，否则会先撞到 [Makefile:55-57](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L55-L57) 的 PLATFORM 校验）。
2. 尝试触发关卡一：`make TARGET=sw_emu aiesim`。
3. 尝试触发关卡二：`make TARGET=hw run`。
4. 阅读两条错误信息原文（就在 [Makefile:62-68](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L62-L68) 与 [Makefile:75](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L75)）。

**需要观察的现象 / 预期结果**：

- 关卡一报错大意：aiesimulator 需要 `hw`/`hw_emu` 的周期级微码，`sw_emu` 的 `x86sim` 无法被它加载。
- 关卡二报错大意：`run` 只能配合 `hw_emu`/`sw_emu`，因为 `run` 跑的是开发机上的仿真启动器，真实硬件（`hw`）要直接在板卡上运行。

> 待本地验证：能否真的看到这两条 `$(error)` 文本，取决于本机是否装了 Vitis 并设好 `PLATFORM`。即便不跑，你也应能从源码读出这两条信息的触发条件与根因。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `make TARGET=hw_emu aiesim` 是**允许**的，而 `make TARGET=sw_emu aiesim` 不允许？

**参考答案**：`hw_emu` 下 `AIE_TARGET=hw`（见 [Makefile:27-33](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L27-L33)），AIE 编译器产出的是面向硬件的微码，aiesimulator 能加载并做周期级仿真；而 `sw_emu` 把 `AIE_TARGET` 设成 `x86sim`，产出的是 x86 函数，aiesimulator 无法加载，故被关卡一拦下。

**练习 2**：`hw` 和 `hw_emu` 在**编译产物**上几乎一样（都是 `hw`），那它们的区别到底体现在哪？

**参考答案**：区别在**运行方式与运行环境**：`hw` 把镜像烧到真实 VCK190 板卡上跑（经 u7-3 的 SD 卡/JTAG 流程），`hw_emu` 则在开发机的硬件仿真器里跑（由 `launch_hw_emu.sh` 启动，靠 `XCL_EMULATION_MODE=hw_emu` 告知 XRT）。所以 `run` 目标只服务于仿真档，真实硬件不走 `run`。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「仿真档位决策」小任务。

**场景**：你刚改了 `img_reconstruct_kern` 里的相位折叠逻辑（u5-4 那段 `INV_TWO_PI` 折叠），想尽快确认「图像数值没算错」，然后再看「每个内核的停顿和吞吐」，最后还想估一下「这次改动对功耗的影响」。

**任务**：

1. 写出你会依次敲的 **三条 make 命令**（含 `TARGET` 取值），分别对应「快速功能验证 → 周期级 profile → 功耗估计」三个阶段，并说明每条命令依赖哪个产物。
2. 指出在这三条命令里，[design/aie/graph.cpp](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp) 的仿真 `main` 分别在哪个档位生效、靠哪个宏生效。
3. 解释为什么这三个阶段里，**没有任何一条**会用 `make TARGET=sw_emu aiesim` 或 `make TARGET=hw run`。

**参考思路**：

1. 阶段一（快速功能验证）：`make TARGET=sw_emu aie` 然后用 `x86sim` 跑（或 `make TARGET=sw_emu run`），靠 `__X86SIM__` 生效，最快但不带时序。阶段二（周期级 profile）：`make TARGET=hw aiesim_profile`（或 `hw_emu`），靠 `__AIESIM__` 生效，产出 `aie.vcd` 与 profile 报告。阶段三（功耗）：`make TARGET=hw aiesim_xpe`，依赖阶段二产出的 `aie.vcd`，产出 `.xpe`。
2. 仿真 main 在 `sw_emu` 靠 `__X86SIM__`、在 `hw`/`hw_emu` 的 aiesim 靠 `__AIESIM__` 生效（同一个 `#if` 守卫，[graph.cpp:12](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp#L12)）；真实硬件构建不编译它。
3. 因为这两条都被 Makefile 的两道关卡（[59-70](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L59-L70) 与 [72-77](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L72-L77)）直接 `$(error)` 拦下，根因分别是「aiesim 不能加载 x86sim」和「run 只服务于仿真档」。

> 待本地验证：上述命令能否真的跑通，取决于本机 Vitis/平台是否就绪。本讲重点是把决策链与根因讲清，而非实际执行。

## 6. 本讲小结

- 项目有 **aiesim / aiesim_profile / aiesim_xpe** 三个递进的 AIE 仿真目标：分别做纯功能仿真、带 profile 与 `aie.vcd` 波形、以及把波形转成 XPE 功耗文件；它们都依赖 `libadf.a`，属「验证旁路」而非主线构建。
- `design/aie/graph.cpp` 里有一份**被 `__AIESIM__`/`__X86SIM__` 宏保护的独立仿真 `main`**，它是 aiesimulator/x86sim 的测试台，内联复刻了主机的「读 CSV + 解卷绕 + 生成像素 + 驱动图」流程。
- 这份仿真 main 的 `gm2aie_nb` 投递顺序与真实主机 `bp()` **一一对应**（slowtime 一次、RC 每脉冲、像素每 switch 每脉冲、RTP 末脉冲置 1）；唯一明显差异是仿真用 `run(PULSES)`、主机用 `run(0)`。
- `sw_emu`/`hw_emu`/`hw` 三档的区别在于：`sw_emu` 把 AIE/PL 降到 x86（快但无时序），`hw_emu` 在开发机做硬件仿真（周期近似），`hw` 真跑在板卡上；`XCL_EMULATION_MODE` 环境变量告知 XRT 当前档位。
- Makefile 设了两道校验关卡：禁止 `aiesim + sw_emu`（aiesim 无法加载 x86sim）、禁止 `run + hw`（run 只服务于仿真启动器）——两者都是「编译能过但运行必错」的组合，故提前拦下。

## 7. 下一步学习建议

- **接下来读 u8-l2（性能与功耗度量）**：那里会把本讲的 `aie.vcd`/XPE 与 `metrics` 目标（Vivado `report_metrics.tcl`）、主机 `CLOCK_MONOTONIC` 分段计时、INA226 实测功耗串成完整的度量体系，并对照性能文档里的三组测试结果。
- **回头深读源码**：若想确认仿真 main 与主机的对应关系，建议并排打开 [design/aie/graph.cpp](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.cpp) 与 `design/host/sar_backproject.cpp`（见 u3-3、u3-4、u3-5），逐函数比对「读 CSV、解卷绕、生成像素、投递序列」四段。
- **仿真到 PL 的衔接**：若你想弄清 aiesim 产出的 PLIO CSV 如何喂给 PL 包路由器仿真，复习 u6-l2 的 testbench，并对照本讲 `plsim_router` 的 CSV 存在性守卫。
