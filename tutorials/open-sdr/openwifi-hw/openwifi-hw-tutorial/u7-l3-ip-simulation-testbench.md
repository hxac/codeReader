# IP 仿真与 testbench 实践

> 承接：本讲假设你已读过 [u4-l2](u4-l2-openofdm-tx-signal-processing.md)（`dot11_tx` 内部信号处理链）与 [u3-l2](u3-l2-rx-intf-submodules.md)（`rx_intf` 子模块，含 `adc_intf`）。本讲不再重复这些模块的算法，而是回答「**怎么把它们单独拎出来跑一遍、看波形、对结果**」。

## 1. 本讲目标

读完本讲，你应该能够：

- 说清 openwifi-hw「单 IP 仿真」的工程组织：`create_vivado_proj.sh` 是通用启动器，`*_tb.tcl` 是仿真工程重建脚本。
- 看懂 testbench 用 `$fopen` / `$fscanf` / `$fwrite` 读写文本测试向量、用 `$readmemh` 装载 BRAM 镜像的两种典型写法。
- 区分仓库里四种 testbench 风格：文件 IO 激励（`mv_avg_tb`）、自激励计数（`fifo_sample_delay_tb`）、双时钟域 CDC（`adc_intf_tb`）、`$readmemh` 全链路回放（`dot11_tx_tb`）。
- 在 Vivado 里把一个 IP 工程跑起行为级仿真（XSim），并知道遇到「找不到测试向量文件」时该改哪里。

## 2. 前置知识

- **testbench（测试平台）**：一段「只为仿真存在、不会被综合成硬件」的 Verilog。它给被测模块（DUT, Design Under Test）喂激励（时钟、复位、数据）、观测输出。本仓库约定文件名以 `_tb.v` 结尾。
- **XSim**：Xilinx Vivado 自带的功能仿真器。本仓库所有 `*_tb.tcl` 都把 `target_simulator` 设成了 `XSim`。
- **行为级仿真（Behavioral Simulation）**：只看 RTL 逻辑、不带时序反标的仿真，跑得快，是验证算法正确性的第一关（对应还有带时序的门级/布局后仿真，本讲不涉及）。
- **文件 IO 系统任务**：Verilog 2001 起支持在 testbench 里读写宿主机文件——`$fopen` 打开、`$fscanf` 按格式读、`$fwrite` 按格式写、`$readmemh`/`$readmemb` 批量装载十六进制/二进制到存储器、`$fclose` 关闭、`$fflush` 强制落盘。这是把「真实抓包/计算出的向量」灌进仿真的关键手段。
- **VCD（Value Change Dump）**：波形文件，`$dumpfile`/`$dumpvars` 把全体信号变化写进去，供 GTKWave 等离线查看。

> 提醒：`$fopen`/`$readmemh` 里的相对路径，是相对**仿真器的运行目录**解析的，**不是相对 `.v` 源文件所在目录**。这是本讲最重要的一个坑，见 4.3.4。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [README.md](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md) | 「Modify IP cores」「Simulate IP cores」两节给出官方仿真流程 |
| [ip/create_vivado_proj.sh](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/create_vivado_proj.sh) | 通用启动器：喂 Xilinx 路径 + 任意 `.tcl` + 最多 7 个参数 |
| [ip/xpu/unit_test/mv_avg/mv_avg_tb.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/unit_test/mv_avg/mv_avg_tb.v) | **文件 IO 激励**的范例 testbench（本讲精读对象） |
| [ip/xpu/unit_test/mv_avg/mv_avg_tb.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/unit_test/mv_avg/mv_avg_tb.tcl) | 仿真工程重建脚本：建工程、加源、设顶层、配 XSim |
| [ip/xpu/unit_test/fifo_sample_delay/fifo_sample_delay_tb.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/unit_test/fifo_sample_delay/fifo_sample_delay_tb.v) | **自激励** testbench：不用文件，内部计数器造数据 |
| [ip/rx_intf/unit_test/adc_intf/adc_intf_tb.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/unit_test/adc_intf/adc_intf_tb.v) | **双时钟域** testbench：40 MHz ADC 域 → 100 MHz 基带域 |
| [ip/openofdm_tx/src/dot11_tx_tb.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx_tb.v) | **`$readmemh` 全链路回放**：把整包字节灌进 BRAM，抓 I/Q 落盘 |
| [ip/xpu/src/mv_avg.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/mv_avg.v) | `mv_avg_tb` 的被测对象（滑动平均，u5-l5 讲过其用途） |

## 4. 核心概念与源码讲解

### 4.1 为什么要仿真、openwifi 的仿真工程是怎么组织的

#### 4.1.1 概念说明

openwifi-hw 是一个上板才能完整运行的 Wi-Fi 物理层（要有 AD9361 射频、要有 PS 驱动）。**上板调试代价极高**（一次综合实现要几十分钟到几小时，且抓信号靠 ILA，只能看片段）。所以开发期需要一种「**把单个 IP 单独拎出来，在 PC 上几秒钟跑完、看完整波形、用脚本比对数值**」的手段——这就是单 IP 行为级仿真。

它带来的好处：

- **快**：XSim 跑 `mv_avg_tb` 这种小模块是秒级。
- **可观测**：所有内部信号都能拖进波形，不像 ILA 受 BRAM 深度限制。
- **可复现**：用固定测试向量文件喂同样的输入，便于回归对比。
- **可数值校验**：把输出 `$fwrite` 落盘，用 MATLAB/Python 脚本算理论值做差。

openwifi 的仿真工程组织有三层，认清它们的关系是本讲的前提：

1. **被测模块源码**（DUT）：在 `ip/<ip_name>/src/` 下，比如 `mv_avg.v`。
2. **testbench**（`*_tb.v`）：在 `ip/<ip_name>/unit_test/<dut>/` 下，只管激励与观测。
3. **工程重建脚本**（`*_tb.tcl`）：由 Vivado「Write Project Tcl」导出，记录了「建哪个工程、part 是什么、加哪些源、顶层是谁、仿真器配置」。`source` 它就能在任意机器上重建出当初那个仿真工程。

> 注意：`*_tb.tcl` 里的 `part`（如 `xczu9eg-…`，zcu102 的器件）只是**仿真宿主器件**，并不要求你真有这块板子——功能仿真不依赖具体器件。openwifi 统一用 zcu102 的 part 来跑这些单元仿真。

#### 4.1.2 核心流程

一次完整的「单 IP 仿真」可概括为：

```text
cd ip/<ip_name>/unit_test/<dut>/
        │  （这里 *_tb.tcl 与 *_tb.v、../../src/<dut>.v 的相对关系已固化）
        ▼
source *_tb.tcl  ──►  Vivado 建出 <dut>_tb 工程
        │  （顶层 = *_tb，target_simulator = XSim）
        ▼
Run Simulation → Run Behavioral Simulation
        │  （首次会编译依赖的子 IP，慢；之后增量快）
        ▼
波形窗口：Run All (F3) → 拖信号 / 查 *_tb.v 落盘的结果文件
```

#### 4.1.3 源码精读

官方流程写在 README 的「Simulate IP cores」一节，要点是把仿真和「Modify IP cores」的建工程步骤绑在一起：

[README.md:113-124](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L113-L124) —— 明确了「先建 IP 工程、再看 Sources → Simulation Sources 里的 `_tb.v`、再 Run Behavioral Simulation」，并且第 124 行专门点名：**请查看 `_tb.v`，看我们是怎么用 `$fopen`、`$fscanf`、`$fwrite` 读写测试向量、保存变量供事后核对的**。这句话就是本讲 4.3 的总纲。

而「Modify IP cores」给出的是创建**任意单 IP 工程**的通用命令：

[README.md:99-102](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L99-L102) —— `cd ip/ip_name` 后执行 `../create_vivado_proj.sh $XILINX_DIR ip_name.tcl`。这条命令创建的是「**带综合/实现 run 的完整 IP 工程**」（顶层是该 IP，可综合可上板）；而 `*_tb.tcl` 创建的是「**纯仿真工程**」（顶层是 testbench，只用于仿真）。两者用途不同，别混淆。

#### 4.1.4 代码实践

- **目标**：在源码里把「IP 工程」与「仿真工程」两套入口分清楚。
- **步骤**：
  1. 打开 `ip/xpu/xpu.tcl`，看它如何 `create_project` 并把 `src/xpu.v` 设为顶层——这是 IP 工程脚本的样子。
  2. 打开 `ip/xpu/unit_test/mv_avg/mv_avg_tb.tcl`，对比它如何把 `mv_avg_tb` 设为顶层——这是仿真工程脚本的样子。
  3. 在仓库里统计：`find ip -name '*_tb.tcl'`，看看哪些 IP 自带了仿真工程重建脚本。
- **观察**：IP 工程脚本的顶层是硬件模块名，仿真工程脚本的顶层是 `*_tb` 模块名；`*_tb.tcl` 会同时把 DUT 源码（`../../src/<dut>.v`）和 testbench（`*_tb.v`）一起 `add_files`。
- **预期**：仓库内自带 `*_tb.tcl` 的目前只有 `mv_avg`、`adc_intf`（`fifo_sample_delay`、`dot11_tx` 只给了 `_tb.v`，需要你按 4.2 的办法自己挂进工程）。

#### 4.1.5 小练习与答案

1. **练习**：`*_tb.tcl` 里把仿真宿主 part 设成了哪颗器件？它和你手上必须有的板子是什么关系？
   **答**：设成了 `xczu9eg-ffvb1156-2-e`（zcu102）。行为级仿真只验证逻辑、不依赖具体器件资源，所以你**不需要**真有 zcu102 板子也能跑这些单元仿真。
2. **练习**：README 说跑仿真前要先做什么？
   **答**：先按「Modify IP cores」用 `create_vivado_proj.sh` 建出 IP 的 Vivado 工程；testbench 会出现在该工程的 `Sources → Simulation Sources → sim_1` 下。

---

### 4.2 用 create_vivado_proj.sh 与 *_tb.tcl 创建单 IP 仿真工程

#### 4.2.1 概念说明

`create_vivado_proj.sh` 本身**不认识**「IP」或「仿真」——它只是一个「`source` Vivado 环境后再 `vivado -source <你给的.tcl>`」的**通用启动器**。所有 IP 相关的语义都在它调用的那个 `.tcl` 里。理解了这点，就知道它既能建 IP 工程（喂 `xpu.tcl`）、又能（在原理上）驱动任意脚本。

它对参数的处理是本讲与 [u7-l2](u7-l2-conditional-compile-macros.md) 条件编译体系的衔接点：最多 7 个额外参数会被透传给 `.tcl`，由 `.tcl` 翻译成 `<IP>_pre_def.v` 里的 `` `define ``。

#### 4.2.2 核心流程

`create_vivado_proj.sh` 的执行链：

```text
$1 XILINX_DIR   $2 TCL_FILENAME   [$3..$9 最多 7 个透传参数]
        │
        ├─ 校验 $XILINX_DIR/Vivado 与 $TCL_FILENAME 存在
        ├─ source $XILINX_DIR/Vivado/2022.2/settings64.sh   （设环境）
        └─ vivado -source $TCL_FILENAME -tclargs $ARG1..$ARG7
                                                            │
                                                            ▼
                                            .tcl 内部：建工程 / 写 _pre_def.v / add_files
```

而把一个 testbench 挂成仿真工程的，是 `*_tb.tcl` 的固定五步：

```text
① create_project <name>_tb ./<name>_tb -part <part>
② set target_simulator XSim
③ add_files:  ../../src/<dut>.v  +  <dut>_tb.v
④ set sources_1.top = <name>_tb      （综合顶层=仿真顶层=testbench）
⑤ set sim_1.top = <name>_tb;  xsim.simulate.runtime = 1000ns
```

#### 4.2.3 源码精读

先看通用启动器。它做三件事：参数校验、设 Vivado 环境、透传参数跑脚本。

[ip/create_vivado_proj.sh:7-16](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/create_vivado_proj.sh#L7-L16) —— 打印用法：至少 2 个参数（`XILINX_DIR`、`TCL_FILENAME`），第 1 个额外参数是 `BOARD_NAME`、第 2 个是 `NUM_CLK_PER_US`、第 3～7 个是用户条件编译宏（对 `openofdm_rx` 第 3 个例外是仿真用的 `SAMPLE_FILE`）。

[ip/create_vivado_proj.sh:44](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/create_vivado_proj.sh#L44) —— `source $XILINX_DIR/Vivado/2022.2/settings64.sh`，版本被写死成 2022.2（呼应 [u1-l3](u1-l3-boards-and-environment.md) 的环境要求）。

[ip/create_vivado_proj.sh:46-77](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/create_vivado_proj.sh#L46-L77) —— 把 `$3..$9` 取到 `ARG1..ARG7`，最终用 `vivado -source $TCL_FILENAME -tclargs $ARG1 … $ARG7` 透传。注意它是「透传」，自身不做宏翻译——翻译在 `.tcl` 里。

再看仿真工程脚本 `mv_avg_tb.tcl` 的关键行，印证上面的五步：

[ip/xpu/unit_test/mv_avg/mv_avg_tb.tcl:90](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/unit_test/mv_avg/mv_avg_tb.tcl#L90) —— `create_project ${_xil_proj_name_} ./${_xil_proj_name_} -part xczu9eg-ffvb1156-2-e`，工程名默认 `mv_avg_tb`（见第 30 行 `set _xil_proj_name_ "mv_avg_tb"`）。

[ip/xpu/unit_test/mv_avg/mv_avg_tb.tcl:143](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/unit_test/mv_avg/mv_avg_tb.tcl#L143) —— `target_simulator` 设为 `XSim`，锁定了用 Vivado 自带仿真器。

[ip/xpu/unit_test/mv_avg/mv_avg_tb.tcl:167-172](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/unit_test/mv_avg/mv_avg_tb.tcl#L167-L172) —— `add_files` 一次性加入被测模块 `../../src/mv_avg.v`、`../../src/mv_avg_dual_ch.v` 和 testbench `mv_avg_tb.v`。这里的相对路径 `../../src/` 是相对 `*_tb.tcl` 所在目录（`unit_test/mv_avg/`），指向 `ip/xpu/src/`。

[ip/xpu/unit_test/mv_avg/mv_avg_tb.tcl:255](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/unit_test/mv_avg/mv_avg_tb.tcl#L255) 与 [ip/xpu/unit_test/mv_avg/mv_avg_tb.tcl:354](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/unit_test/mv_avg/mv_avg_tb.tcl#L354) —— `sources_1` 的 `top` 和 `sim_1` 的 `top` 都设成了 `mv_avg_tb`，说明这个工程的**综合顶层就是 testbench**（典型的仿真工程做法，不会拿去上板）。

[ip/xpu/unit_test/mv_avg/mv_avg_tb.tcl:381](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/unit_test/mv_avg/mv_avg_tb.tcl#L381) —— `xsim.simulate.runtime = 1000ns` 是 Vivado 默认仿真时长；但 `mv_avg_tb.v` 内部用 `$finish` 自行终止（见 4.3），所以这个默认值只是「兜底」。

#### 4.2.4 代码实践

- **目标**：把 `mv_avg_tb` 仿真工程建出来（**待本地验证**：需本机装有 Vivado 2022.2）。
- **步骤**：
  1. `cd ip/xpu/unit_test/mv_avg`
  2. 在 Vivado Tcl Shell 里 `source mv_avg_tb.tcl`（或在系统 shell 里先 `source <XILINX_DIR>/Vivado/2022.2/settings64.sh` 再 `vivado -source mv_avg_tb.tcl`）。注意：`*_tb.tcl` 不走 `create_vivado_proj.sh`，因为它不需要传 `BOARD_NAME` 等参数，直接 source 即可。
  3. 工程生成后，在 Vivado 左侧 PROJECT MANAGER 点 **Run Simulation → Run Behavioral Simulation**。
- **观察**：首次编译 `mv_avg` 依赖的 Xilinx FIFO IP 会慢一些；之后增量快。波形窗口出现后，把 `mv_avg_inst` 下的 `data_in`、`data_out`、`data_in_valid`、`data_out_valid` 拖进波形。
- **预期**：能看到 `data_in_valid` 每 5 个 `clock` 拉高一次（100 MHz 下即 20 MHz 采样），`data_out_valid` 在 FIFO 攒满后开始节拍输出。

#### 4.2.5 小练习与答案

1. **练习**：为什么 `mv_avg_tb.tcl` 不像 `xpu.tcl` 那样去写 `_pre_def.v`？
   **答**：`mv_avg_tb` 只测 `mv_avg`/`mv_avg_dual_ch` 这两个纯组合时序小模块，它们不依赖任何板级宏（`NUM_CLK_PER_US`、`SMALL_FPGA` 等），所以仿真工程不需要条件编译注入。
2. **练习**：想给 `mv_avg` 在独立 IP 工程里加调试宏（如 `XPU_ENABLE_DBG`），该用哪条命令？
   **答**：`cd ip/xpu && ../create_vivado_proj.sh $XILINX_DIR xpu.tcl zc706_fmcs2 100 ENABLE_DBG`（第 3 个额外参数 `ENABLE_DBG` 会被 `xpu.tcl` 写成 `` `define XPU_ENABLE_DBG ``，见 u7-l2）。

---

### 4.3 文件 IO 测试向量：$fopen / $fscanf / $fwrite（以 mv_avg_tb 为例）

#### 4.3.1 概念说明

很多 DSP 模块（滑动平均、FIR、RSSI 换算）的「正确输出」需要用 MATLAB/Python 离线算出来当黄金参考。openwifi 的做法是：

- 把输入样点存成**一列十进制整数**（`data_in.txt`，每行一个数）。
- testbench 用 `$fscanf` 按节拍读一行、喂给 DUT。
- 把 DUT 输出用 `$fwrite` 写成 `data_out_new.txt`，再与离线参考比对。

`mv_avg_tb.v` 就是这套「文件 IO 三件套」的标准范例，同时测了 `mv_avg`（单通道）和 `mv_avg_dual_ch`（双通道）两个 DUT。

#### 4.3.2 核心流程

```text
复位释放 ──► 首个 posedge clock 触发 $fopen(读文件,"r") / $fopen(写文件,"w")
     │
     ▼  （主 always，每个 posedge clock）
clk_count 数到 CLK_COUNT_TOP_FOR_VALID：
     ├─ data_in_valid <= 1
     ├─ $fscanf(fd,"%d",x) 读一个数 → data_in      （读到尾 → run_out_of_iq_sample=1）
     └─ clk_count 归零
否则： data_in_valid <= 0；并在 data_in 上做点变化（模拟采样间扰动）
     │
     ├─ 若 data_out_new_valid：$fwrite(out,"%d\n",data_out_new) + $fflush
     ├─ 若 data_out_dual_ch_valid：$fwrite(...,"%d %d\n", out0, out1)
     └─ 若 run_out_of_iq_sample 且 data_in_valid：$fclose(全部) + $finish
```

采样率是「自动对齐」到 20 MHz 的：`data_in_valid` 每 `(CLK_COUNT_TOP_FOR_VALID+1)` 个时钟拉高一次，于是

\[
f_{\text{sample}} = \frac{f_{\text{clk}}}{\text{CLK\_COUNT\_TOP\_FOR\_VALID}+1}
\]

100 MHz 时取 \(100/5 = 20\) MHz，200 MHz 时取 \(200/10 = 20\) MHz——正好是 openwifi 的基带采样率（[u2-l4](u2-l4-board-config-clock.md) 的 `SAMPLING_RATE_MHZ=20`）。这是 testbench「用两个不同的宏值、保持同一 20 MHz 节拍」的小技巧。

#### 4.3.3 源码精读

文件路径用 `` `define `` 集中声明，方便改：

[ip/xpu/unit_test/mv_avg/mv_avg_tb.v:43-54](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/unit_test/mv_avg/mv_avg_tb.v#L43-L54) —— `` `define SPEED_100M `` 选时钟；`INPUT_FILE`/`OUTPUT_NEW_FILE`/`OUTPUT_DUAL_CH_FILE` 指向 `../../../../../test_vec/...`；`NUM_SAMPLE 1999`。（注意：注释掉的那些行是历史遗留的多套输入输出，作者保留下来作切换参考。）

时钟与复位由 `initial` + `always` 产生：

[ip/xpu/unit_test/mv_avg/mv_avg_tb.v:56-88](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/unit_test/mv_avg/mv_avg_tb.v#L56-L88) —— `initial` 里先 `$dumpfile/$dumpvars` 开 VCD，然后给 `reset`/`sim_reset` 一套波形（含多个「5 拍复位」脉冲，模拟运行中软复位）；`always` 用 `` `ifdef SPEED_100M `` 在 `#5`（100 MHz）与 `#2.5`（200 MHz）间切换。

文件打开用了一个「只开一次」的小机关 `file_open_trigger`：

[ip/xpu/unit_test/mv_avg/mv_avg_tb.v:90-105](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/unit_test/mv_avg/mv_avg_tb.v#L90-L105) —— 复位期间 `file_open_trigger=0`；`sim_reset` 释放后的第一个 `posedge clock` 里 `trigger==0` 命中，于是 `$fopen(INPUT_FILE,"r")`、`$fopen(OUTPUT_NEW_FILE,"w")`、`$fopen(OUTPUT_DUAL_CH_FILE,"w")` 各执行一次，随后 `trigger` 自增、不再重开。这样避免了「每个时钟都 fopen」的常见错误。

读输入与产生 valid 节拍的核心：

[ip/xpu/unit_test/mv_avg/mv_avg_tb.v:107-135](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/unit_test/mv_avg/mv_avg_tb.v#L107-L135) —— `CLK_COUNT_TOP_FOR_VALID` 在 100 M 下为 4、200 M 下为 9（注释写明推导 `100/20=5`、`200/20=10`）。`clk_count` 数到顶时：拉高 `data_in_valid`、用 `$fscanf(data_in_fd,"%d",file_data_in)` 读一行（第 122 行）、把值同时塞给 `data_in`/`data_in0`/`-data_in1` 喂两路 DUT；`$fscanf` 返回成功项数给 `iq_count_tmp`，若不为 1 说明读到文件尾，置 `run_out_of_iq_sample=1`（第 127–128 行）。未到顶的节拍里，作者还故意让 `data_in` 在 valid 之间变化，模拟「采样间样点也在变」。

输出落盘与仿真终止：

[ip/xpu/unit_test/mv_avg/mv_avg_tb.v:137-151](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/unit_test/mv_avg/mv_avg_tb.v#L137-L151) —— 每 100 个样点 `$display` 一次进度；一旦「读到文件尾」且当前是 valid 拍，就 `$fclose` 所有句柄并 `$finish`，干净收尾。

[ip/xpu/unit_test/mv_avg/mv_avg_tb.v:158-175](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/unit_test/mv_avg/mv_avg_tb.v#L158-L175) —— `data_out_new_valid` 有效时 `$fwrite(fd,"%d\n",data_out_new)` + `$fflush`；双通道同理写成 `"%d %d\n"`。`$fflush` 保证仿真中途被 Ctrl-C 也能拿到已写数据。

DUT 例化（注意参数）：

[ip/xpu/unit_test/mv_avg/mv_avg_tb.v:190-210](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/unit_test/mv_avg/mv_avg_tb.v#L190-L210) —— `mv_avg` 取 `LOG2_AVG_LEN=5`（即 32 点滑动平均），`mv_avg_dual_ch` 取 `LOG2_AVG_LEN=4`（16 点）。对照 DUT 源码 [ip/xpu/src/mv_avg.v:6](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/mv_avg.v#L6) 的默认参数 `LOG2_AVG_LEN = 5` 与 [ip/xpu/src/mv_avg.v:19](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/mv_avg.v#L19) 的 `FIFO_SIZE = 1<<LOG2_AVG_LEN`，可算出平均窗长就是 32。

#### 4.3.4 关键陷阱：相对路径相对的是「仿真运行目录」

`mv_avg_tb.v` 写的是 `../../../../../test_vec/data_in.txt`（5 层上）。但你打开仓库会发现，实际的输入向量就在 testbench **旁边**：`ip/xpu/unit_test/mv_avg/test_vec/data_in.txt`（2000 行，每行一个有符号整数，如 `-491`、`658`、`1436`…）。

这两个事实并不矛盾——因为 `$fopen`/`$fscanf` 的相对路径是相对**仿真器的工作目录**解析的，不是相对 `.v` 文件。Vivado「Run Behavioral Simulation」默认把工作目录设在 `<工程>/<工程>.sim/sim_1/behav/xsim/` 下，作者就是按那个深度回退 `../` 来对齐向量位置的。

> 实践后果：如果你换一种方式启动仿真（比如改了 `xsim.simulate.runtime` 的运行目录，或在命令行裸跑 `xsim`），那串 `../` 就对不上了，仿真会报「打不开 `data_in.txt`」。**解决办法**：要么从 Vivado GUI 的标准入口跑（路径自洽），要么把 `INPUT_FILE` 宏改成绝对路径或正确的相对路径。**这一点的确切行为待本地验证**——以你本机 Vivado 实际的工作目录为准。

旁边那个 `test_data_in_out.m` 是 MATLAB 脚本，正是「离线算参考、和 `data_out_new.txt` 做差」用的，体现了 4.3.1 的验证闭环。

#### 4.3.5 代码实践（本讲主实践）

- **目标**：把 `mv_avg_tb` 的「读向量→驱动 DUT→写结果」三段读通，并能改一个参数观察行为。
- **步骤**：
  1. 打开 [ip/xpu/unit_test/mv_avg/mv_avg_tb.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/unit_test/mv_avg/mv_avg_tb.v)，定位三件事：`$fopen`（第 96/98/101 行）、`$fscanf`（第 122 行）、`$fwrite`（第 159/173 行）。
  2. 看一眼输入向量 `ip/xpu/unit_test/mv_avg/test_vec/data_in.txt` 的前几行，确认是「每行一个十进制整数」。
  3. **修改实验（仅本地、改完记得还原）**：把第 190 行 DUT 的 `.LOG2_AVG_LEN(5)` 临时改成 `7`（128 点平均），重新跑仿真，对比 `data_out_new.txt` 的平滑程度变化。
- **观察**：窗长变大后，输出对输入跳变的响应更迟钝、曲线更平滑——这正是滑动平均的特性。窗长 \(N=2^{\text{LOG2\_AVG\_LEN}}\)，所以 5→32、7→128。
- **预期**：`data_out_new.txt` 行数与 `data_in.txt` 的有效样点数对应（因 FIFO 填充会有起始延迟）；若读到文件尾 `$finish` 正常触发，说明文件 IO 闭环正确。**运行结果待本地验证**。

#### 4.3.6 小练习与答案

1. **练习**：`$fscanf(data_in_fd, "%d", file_data_in)` 的返回值被存到了哪个变量？用来判断什么？
   **答**：存到 `iq_count_tmp`（第 122 行）。`$fscanf` 返回成功匹配的项数；读到文件尾时返回 0（≠1），于是第 127–128 行置 `run_out_of_iq_sample=1`，随后触发 `$fclose` + `$finish`。
2. **练习**：为什么作者要在 valid 之间的那些节拍里写 `data_in <= -data_in;`？
   **答**：为了在「两个有效采样之间」也让 `data_in` 端口出现可见的变化，模拟真实采样链路里「valid 无效时数据仍可能在抖」的情形，便于在波形上肉眼区分 valid 节拍与非 valid 节拍，也顺便检验 DUT 不会在 valid 无效时误吞数据。
3. **练习**：为什么每个 `$fwrite` 后面都跟一句 `$fflush`？
   **答**：强制把缓冲区立刻写盘。仿真可能运行很久或被中途打断，`$fflush` 保证即便异常终止，已产生的输出也已落盘可供核对。

---

### 4.4 四种 testbench 风格对比

#### 4.4.1 概念说明

openwifi 仓库里的 testbench 并非一种模式。按「激励来源」可分四类，理解它们的差异，能帮你为新模块选合适的写法：

| 风格 | 代表文件 | 激励来源 | 输出校验 | 适合场景 |
| --- | --- | --- | --- | --- |
| 文件 IO 激励 | `mv_avg_tb.v` | `$fscanf` 读文本向量 | `$fwrite` 落盘 + 离线比对 | 有黄金参考的 DSP 小模块 |
| 自激励计数 | `fifo_sample_delay_tb.v` | 内部计数器造数据 | 看波形 | 无需真实数据、看时序/延迟 |
| 双时钟域 CDC | `adc_intf_tb.v` | 两路独立时钟 + 计数 | 看波形 | 跨时钟域接口（ADC→基带） |
| `$readmemh` 全链路 | `dot11_tx_tb.v` | 整包字节装载进 BRAM | `$fwrite` 抓 I/Q | 大模块整链路回放 |

#### 4.4.2 核心流程

- **自激励**：`clk_count` 数到顶就让 `data_in <= data_in + 1`，跑满 `NUM_SAMPLE` 个样点 `$finish`。最简单，不依赖任何外部文件。
- **双时钟域**：用两个 `always` 产生不同频率的时钟（这里是 40 MHz ADC 与 100 MHz 基带），各自带独立复位；DUT 在两域之间做 CDC。重点验证「跨域后数据不丢、不错拍」。
- **`$readmemh` 全链路**：把一整帧（甚至聚合帧）的字节预先存成 `.mem`（十六进制），`$readmemh` 一次性装载到一个 `reg [63:0] Memory [0:1023]` 数组，再由 DUT 按地址回读；DUT 输出的 I/Q 用 `$fwrite` 落盘，可离线画频谱/星座图。

#### 4.4.3 源码精读

**自激励：`fifo_sample_delay_tb.v`**

[ip/xpu/unit_test/fifo_sample_delay/fifo_sample_delay_tb.v:60-73](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/unit_test/fifo_sample_delay/fifo_sample_delay_tb.v#L60-L73) —— 到达 `CLK_COUNT_TOP_FOR_VALID` 时 `data_in <= data_in + 1`（第 63 行，单调递增的斜坡，方便在波形上认）、`sample_count++`，到 `NUM_SAMPLE`(1000) 即 `$finish`（第 66–67 行）。无需任何文件。

[ip/xpu/unit_test/fifo_sample_delay/fifo_sample_delay_tb.v:75-84](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/unit_test/fifo_sample_delay/fifo_sample_delay_tb.v#L75-L84) —— DUT `fifo_sample_delay` 被设成 `delay_ctl=4`、`LOG2_FIFO_DEPTH=7`，即「延迟 4 个样点、FIFO 深 128」。看波形时量 `data_in_valid` 上升沿到 `data_out_valid` 上升沿的样点差即可验证延迟。

**双时钟域：`adc_intf_tb.v`**

[ip/rx_intf/unit_test/adc_intf/adc_intf_tb.v:42-49](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/unit_test/adc_intf/adc_intf_tb.v#L42-L49) —— 两个时钟：`acc_clk_raw` 半周期 `#5`（100 MHz），再用 `assign #3.3 acc_clk = acc_clk_raw;` 人为加 3.3 ns 延迟，模拟板级时钟树偏斜；`adc_clk` 半周期 `#12.5`（40 MHz，正是 AD9361 的 ADC 采样率）。

[ip/rx_intf/unit_test/adc_intf/adc_intf_tb.v:51-57](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/unit_test/adc_intf/adc_intf_tb.v#L51-L57) —— 在 `adc_clk` 域让 `adc_data <= adc_data + 1`（斜坡激励），跨域后看 `data_to_bb_valid` 是否仍然连续、不错拍。`bb_gain=0`（第 41 行）表示不施加数字增益。这个 testbench 验证的是 [u3-l2](u3-l2-rx-intf-submodules.md) 讲过的 `adc_intf` 的 2:1 抽取与跨域。

**`$readmemh` 全链路：`dot11_tx_tb.v`**

[ip/openofdm_tx/src/dot11_tx_tb.v:21-30](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx_tb.v#L21-L30) —— 声明 `reg [63:0] Memory [0:1023]`（1024 个 64 bit 字，即 8 KiB BRAM 镜像），用 `$readmemh` 把 `ht_tx_intf_mem_mcs7_gi1_aggr0_byte8176.mem`（MCS7、短 GI、非聚合、8176 字节的整帧）一次性装载进去（第 28 行）。`.mem` 文件每行 16 个十六进制字符（= 64 bit），如 `00000000010002AB`。同时 `$fopen("dot11_tx.txt","w")` 准备抓 I/Q。

[ip/openofdm_tx/src/dot11_tx_tb.v:36-58](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx_tb.v#L36-L58) —— 复位后给 `phy_tx_start` 一个脉冲（第 36–38 行）触发发射；每个 `posedge clock`（200 MHz）：`result_iq_valid` 有效时 `$fwrite(result_fd,"%d %d\n", result_i, result_q)` 抓一对 I/Q（第 49–50 行），并把 `Memory[bram_addr]` 回读给 `bram_din`（第 52 行，即模拟 `tx_intf` 把 DMA 数据写进 BRAM、`dot11_tx` 按地址读出，见 [u4-l1](u4-l1-openofdm-tx-overview.md)）；`phy_tx_done==1` 时 `$fclose` + `$finish`（第 53–56 行）。注意这个 testbench 没有用 `SPEED_100M` 宏，直接用 `#2.5` 跑 200 MHz。

[ip/openofdm_tx/src/dot11_tx_tb.v:61-79](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx_tb.v#L61-L79) —— DUT `dot11_tx`，`result_iq_ready` 恒置 1（不背压），两个扰码初值都给 `7'b1111111`。抓到的 `dot11_tx.txt` 可送进 MATLAB 画频谱/星座图，验证 [u4-l2](u4-l2-openofdm-tx-signal-processing.md) 讲的编码/交织/IFFT/前导拼接是否正确。

#### 4.4.4 代码实践

- **目标**：对比「文件 IO」与「`$readmemh`」两种装载方式的差异。
- **步骤**：
  1. 打开 `ip/openofdm_tx/unit_test/test_vec/`，看 `.mem` 文件格式（每行 16 个 hex 字符）。
  2. 对比 `mv_avg_tb.v` 的 `data_in.txt`（每行一个十进制数）。
  3. 想一想：为什么 `dot11_tx_tb` 用 `$readmemh` 而不是 `$fscanf`？
- **观察与预期**：`$readmemh` 一次性把整块 BRAM 装满、DUT 按地址随机访问——适合「整包数据预先就位」的场景；`$fscanf` 是流式逐行读——适合「样点随时间逐个到达」的场景。二者本质区别是「随机访问的存储模型」vs「顺序流」。

#### 4.4.5 小练习与答案

1. **练习**：`adc_intf_tb` 里 `assign #3.3 acc_clk = acc_clk_raw;` 的 `#3.3` 起什么作用？
   **答**：给基带时钟人为加一个 3.3 ns 的延迟，模拟真实板级时钟分配网络带来的相移/偏斜，让 CDC 的验证更贴近上板情况，避免「理想对齐时钟」掩盖问题。
2. **练习**：`dot11_tx_tb` 仿真结束后，去哪里看发射出来的基带波形？
   **答**：看 testbench 写出的 `dot11_tx.txt`（每行一对十进制 I/Q），可导入 MATLAB/Python 画时域、频谱或星座图；也可以在 Vivado 波形窗口看 `result_i`/`result_q`。
3. **练习**：`fifo_sample_delay_tb` 不读任何文件，怎么判断延迟对不对？
   **答**：激励是 `data_in` 单调 `+1` 的斜坡，在波形上直接量「`data_in_valid` 上升沿 → `data_out_valid` 上升沿」之间的样点数，应等于 `delay_ctl`(4) 设定的延迟。

---

## 5. 综合实践

**任务：为 `mv_avg` 跑一次完整仿真，并用脚本核对滑动平均的数值。**

1. **建工程**：`cd ip/xpu/unit_test/mv_avg`，`source mv_avg_tb.tcl`（或经 `vivado -source`），得到 `mv_avg_tb` 工程。
2. **跑仿真**：Vivado 里 Run Simulation → Run Behavioral Simulation，Run All 直到 `$finish`。
3. **核对输入**：确认 `test_vec/data_in.txt` 被 `$fscanf` 正确读取（若报「打不开文件」，按 4.3.4 调整 `INPUT_FILE` 宏或从 GUI 标准入口启动）。
4. **核对输出**：仿真结束应生成 `test_vec/data_out_new.txt`。用 Python/MATLAB 读 `data_in.txt`，按窗长 \(N=32\)（`LOG2_AVG_LEN=5`）算滑动平均，与 `data_out_new.txt` 逐行做差，最大绝对误差应为 0 或仅 1 LSB（定点取整）。
5. **进阶**：把 DUT 参数改回 `LOG2_AVG_LEN=7` 重跑，用同一脚本（窗长改 128）核对，体会「参数化窗长」对结果的影响。

> 说明：第 1–3 步需要本机装有 Vivado 2022.2，**运行结果待本地验证**；第 4 步的离线比对脚本可参照仓库自带的 `test_vec/test_data_in_out.m` 编写。

## 6. 本讲小结

- openwifi 的「单 IP 仿真」分三层：DUT 源码（`src/`）、testbench（`unit_test/`）、工程重建脚本（`*_tb.tcl`）；`*_tb.tcl` 把 testbench 设成顶层、配 XSim、part 用 zcu102 仅作仿真宿主。
- `create_vivado_proj.sh` 是「`source` Vivado 环境 + `vivado -source <tcl>`」的**通用启动器**，参数透传给 `.tcl`；建 IP 工程喂 `xpu.tcl`，纯仿真工程直接 `source *_tb.tcl`。
- 文件 IO 三件套 `$fopen`/`$fscanf`/`$fwrite` 是把真实向量灌进仿真、把输出落盘比对的标准手段（README 第 124 行专门点名）；`mv_avg_tb.v` 是范例，并用 `CLK_COUNT_TOP_FOR_VALID` 把采样节拍自动对齐到 20 MHz。
- 最大陷阱：`$fopen`/`$readmemh` 的相对路径相对**仿真器工作目录**解析，不是相对 `.v` 文件；换启动方式时需调整 `INPUT_FILE` 宏。
- 仓库里有四种 testbench 风格：文件 IO（`mv_avg`）、自激励（`fifo_sample_delay`）、双时钟域 CDC（`adc_intf`）、`$readmemh` 全链路（`dot11_tx`），按激励来源选用。

## 7. 下一步学习建议

- 想给 IP 加调试探针再仿真：读 [u7-l2](u7-l2-conditional-compile-macros.md) 的 `*_ENABLE_DBG` 宏，结合本讲的 `create_vivado_proj.sh` 多参数用法，在仿真里打开 `mark_debug`。
- 想改 IP 源码并重新集成回顶层：接着读 [u7-l4](u7-l4-modify-package-custom-ip.md)（修改并打包自定义 IP）。
- 想验证更完整的收发链路：以 `dot11_tx_tb` 为模板，尝试为 `tx_intf` 或 `rx_intf` 的子模块仿照 `mv_avg_tb` 写一个文件 IO testbench，把 u3/u4 讲的数据流在仿真里跑通。
