# FPGA 板级自测试 fpga_self_test

## 1. 本讲目标

本讲解决一个很现实的工程问题：**雷达板第一次上电，我怎么知道 FPGA 里的关键数字通路是好的？**

学完本讲，你应当能够：

- 说清 `fpga_self_test` 模块的存在意义，以及它与 `run_regression.sh`（仿真验证）、formal（形式化验证）这三层验证体系各自的位置。
- 画出 5 项测试的状态机执行顺序，并解释哪几项是「真测试」、哪几项是「算术占位测试」。
- 准确描述 5 位 `result_flags`、8 位 `result_detail`、`busy`、`result_valid` 四个信号的含义与生成时机。
- 讲清楚「opcode 0x30 触发 → 自动跑完 → 结果锁存 → 0x31/0xFF 状态包回读」这条闭环，并理解自清零脉冲（self-clearing pulse）的写法。
- 写出一段在主机端解析状态包 word 5、判定各子系统是否通过的检查脚本。

本讲是 U10「板级 Bring-up 与 FPGA 自测试」单元的第一讲，依赖你已经读过 [u3-l1 FPGA 顶层](u3-l1-fpga-top-module.md)（顶层例化与命令译码）和 [u6-l2 主机命令协议](u6-l2-host-command-protocol.md)（opcode 映射与状态包格式）。

## 2. 前置知识

在进入源码前，先用通俗语言铺垫三个概念。

### 2.1 什么是「板级 Bring-up」与「Smoke Test」

把一块新焊接好的雷达板第一次通电，工程上叫 **bring-up（点亮）**。这一步风险最高：电源可能短路、芯片可能虚焊、时钟可能没起来、FPGA 可能没配置成功。没人敢在这种情况下直接跑雷达波形——你看到的「没目标」可能是天线问题、也可能是 FPGA 根本没工作。

所以工程师会在 FPGA 里放一个**烟雾测试（smoke test）**：上电后用一段独立的、不依赖完整雷达流水线的小程序，挨个戳一下关键资源（BRAM 能不能存数？加减法饱和对不对？ADC 有没有数据流过来？），把「明显坏了」的情况先排掉。`fpga_self_test` 就是这个角色。它**不替代**完整的仿真回归或形式化验证——那些在交付前就做完了——它只回答「这块具体的物理板子，此刻是不是活的」。

### 2.2 自清零脉冲（Self-Clearing Pulse）

主机下发的命令大多是「写一个配置值」，但触发类命令（如「开始扫描」「跑自测试」）需要的是**一个时钟周期的窄脉冲**，而不是一直拉高的电平。AERIS-10 用一种固定写法实现它（详见 [u6-l2](u6-l2-host-command-protocol.md)）：

```verilog
always @(posedge clk) begin
    host_self_test_trigger <= 1'b0;        // 默认每拍清零
    if (cmd_valid_100m)
        case (usb_cmd_opcode)
            8'h30: host_self_test_trigger <= 1'b1;  // 命中时拉高一拍
        endcase
end
```

因为 Verilog 非阻塞赋值（`<=`）在同一 `always` 块里**后写的覆盖先写的**，命中 `0x30` 那一拍 trigger=1，下一拍没有命中就回到默认的 0。于是产生一个单周期脉冲。本讲的触发信号就是这种写法。

### 2.3 状态包是「请求驱动」的

FPGA 不会主动往外推状态，而是主机发一个 `0xFF`（或别名 `0x31`）请求，FPGA 才回一个 26 字节状态包。自测试结果就藏在这个状态包的 **word 5** 里（详见 [u6-l2](u6-l2-host-command-protocol.md)）。这意味着：触发自测试后，主机要**轮询**状态包，先等 `busy` 变 0，再读 `flags`。

## 3. 本讲源码地图

本讲涉及的真实源码文件及其作用：

| 文件 | 作用 |
| --- | --- |
| [fpga_self_test.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/fpga_self_test.v) | 自测试模块本体：12 状态 FSM，跑 5 项测试，产出 flags/detail/busy |
| [radar_system_top.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v) | 顶层：例化自测试模块、把 opcode 0x30 接成触发、把结果锁存并接入状态包 word 5 |
| [radar_protocol.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py) | 主机端协议：`Opcode` 枚举、`build_command` 拼命令、`parse_status_packet` 解析 word 5 |
| [radar_receiver_final.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v) | 接收链：暴露 `dbg_adc_i` 调试抽头，被自测试 Test 4 复用 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**自测试 FSM** → **结果锁存** → **状态回读**。

### 4.1 自测试 FSM：上电后逐个测试关键子系统

#### 4.1.1 概念说明

`fpga_self_test` 的使命是：收到一个触发脉冲后，自动按顺序跑完 5 项独立的小测试，每项往 `result_flags` 的对应比特写 PASS(1)/FAIL(0)。5 项测试分别是：

| 测试 | 目标 | 验证方式 | 「真」/「占位」 |
| --- | --- | --- | --- |
| Test 0 | BRAM 读写 | 写 walking-1 图案再读回比对 | 真测试 |
| Test 1 | CIC 抽取器核心算术 | 只验证累加逻辑（未例化真 CIC） | 算术占位 |
| Test 2 | FFT 蝶形核心算术 | 只验证 `100+100=200, 100-100=0` | 算术占位 |
| Test 3 | 饱和加法 | 验证 `sat_add` 三组用例 | 真测试 |
| Test 4 | ADC 数据流 | 从 DDC 抽头抓 256 个样本，看是否超时 | 真测试 |

为什么 Test 1、Test 2 是「占位」？文件头注释里写明：实例化完整的 CIC 或 16 点 FFT「太重」，自测试要控制在 **约 200 LUT、1 块 BRAM、0 个 DSP** 的资源预算内。完整的 CIC/FFT 正确性已经由 `run_regression.sh` 的协同仿真和 formal 验证负责（见 [u11-l1](u11-l1-fpga-regression-and-cosim.md)、[u14-l1](u14-l1-formal-verification.md)），板级自测试不必重复造轮子，只验证它们依赖的**底层算术原语**还活着。

#### 4.1.2 核心流程

状态机用一个 `state` 寄存器在 12 个状态间线性流转，没有分支跳转（失败也不中断，跑完所有测试再统一汇报）。其执行序列可用下面的文字流程图表示：

```
trigger 脉冲
   │
   ▼
ST_IDLE ──► ST_BRAM_WR ──► ST_BRAM_GAP ──► ST_BRAM_RD ──► ST_BRAM_CHK   (Test 0)
                                                                  │
   ◄──────────────────────────────────────────────────────────────┘
   ▼
ST_CIC_SETUP  (Test 1, 等 8 拍, 置 flags[1])
   ▼
ST_FFT_SETUP  (Test 2, 等 4 拍, 验证蝶形, 置 flags[2])
   ▼
ST_ARITH      (Test 3, 三组 sat_add 用例, 置 flags[3])
   ▼
ST_ADC_CAP    (Test 4, 抓 256 样本 / 超时, 置 flags[4])
   ▼
ST_DONE ──► busy=0, result_valid=1(单拍) ──► ST_IDLE
```

关键设计点：

1. **失败不中断**。即使 Test 0 失败，后续 4 项照跑，让你一次看到所有坏的地方——bring-up 时这是最高效的。
2. **Test 4 带超时**。如果 ADC 抽头在 1000 个时钟周期（100 MHz 下即 10 µs）内没吐出任何有效样本，直接判 FAIL 并写 `8'hAD` 标记，避免 FSM 卡死。
3. **`result_valid` 是单拍脉冲**。完成后只拉高一拍，所以顶层必须**锁存**结果（见 4.2），否则主机轮询时早就过了。

#### 4.1.3 源码精读

**端口定义**——控制类信号与 ADC 抽头类信号分两组，方向清晰：

[fpga_self_test.v:21-38](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/fpga_self_test.v#L21-L38) 定义了 `trigger`（输入脉冲）、`busy`/`result_valid`（状态）、`result_flags[4:0]`/`result_detail[7:0]`（结果），以及 Test 4 专用的 `adc_data_in`/`adc_valid_in` 输入与 `capture_*` 输出。

**状态编码**——12 个状态用 4 位编码：

[fpga_self_test.v:43-54](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/fpga_self_test.v#L43-L54) 用 `localparam` 列出全部状态，其中 `ST_BRAM_GAP` 是特意留的 1 拍间隔，保证最后一次 BRAM 写完成后再开始读（同步 BRAM 写有建立时间）。

**Test 0：walking-1 图案**——地址决定哪个比特为 1：

[fpga_self_test.v:82-88](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/fpga_self_test.v#L82-L88) 的 `walking_one` 函数把地址低 4 位翻译成「第几位为 1」的 16 位数：

```verilog
walking_one = 16'd1 << (addr[3:0]);
```

地址 0 → `0x0001`，地址 1 → `0x0002`，…，地址 15 → `0x8000`，地址 16 又回到 `0x0001`（因为只看低 4 位）。这种图案的好处是：如果某根数据线虚焊（恒 0）或短路（恒 1），读回值会与期望不符，能定位到具体的比特。读回比对在 FSM 主进程里：

[fpga_self_test.v:321-327](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/fpga_self_test.v#L321-L327) 在 `ST_BRAM_RD` 期间逐地址把读回值与 `walking_one(bram_rd_addr_d)` 比对，一旦不等就把 `bram_pass` 拉低，并把失败地址低 4 位写进 `result_detail`。注意这里用了 `bram_rd_addr_d`（打一拍的地址），是为了对齐同步 BRAM 读的 1 拍延迟。

**Test 3：饱和加法**——和 `mti_canceller` 共用同一套算术：

[fpga_self_test.v:94-107](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/fpga_self_test.v#L94-L107) 的 `sat_add` 函数先扩展到 17 位求和，再做上下限饱和：

```verilog
sum_full = {a[15], a} + {b[15], b};      // 符号位扩展到 17 位
if (sum_full > 17'sd32767)  sat_add = 16'sd32767;   // 正向饱和
else if (sum_full < -17'sd32768) sat_add = -16'sd32768; // 负向饱和
else sat_add = sum_full[15:0];
```

这正是 MTI 对消器、Doppler 等定点运算的基础原语。FSM 在 `ST_ARITH` 里用三组用例检验它：`32767+1` 应饱和为 `32767`（而不是回绕成 `-32768`）、`-32768+(-1)` 应饱和为 `-32768`、`100+200=300`，见 [fpga_self_test.v:258-281](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/fpga_self_test.v#L258-L281)。

**Test 4：ADC 抓取与超时**——这是最值得精读的一段，它体现了「复用调试抽头」的工程思路：

[fpga_self_test.v:286-307](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/fpga_self_test.v#L286-L307) 在 `ST_ADC_CAP` 里：每收到一个 `adc_valid_in` 就抓一个样本、计数器加 1，凑满 `ADC_CAP_SAMPLES = 256` 个就判 PASS；同时 `step_cnt` 每拍自增，达到 1000 且一个样本都没收到就判 FAIL 并写 `8'hAD`：

```verilog
if (adc_valid_in) begin
    capture_data  <= adc_data_in;
    capture_valid <= 1'b1;
    adc_cap_cnt   <= adc_cap_cnt + 1;
    if (adc_cap_cnt >= ADC_CAP_SAMPLES - 1) begin
        result_flags[4] <= 1'b1;   // 抓够 256 个，PASS
        state           <= ST_DONE;
    end
end
step_cnt <= step_cnt + 1;
if (step_cnt >= 10'd1000 && adc_cap_cnt == 0) begin
    result_flags[4] <= 1'b0;        // 超时，FAIL
    result_detail   <= 8'hAD;       // ADC 超时标记
    state           <= ST_DONE;
end
```

`ADC_CAP_SAMPLES = 256` 见 [fpga_self_test.v:116](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/fpga_self_test.v#L116)。

**「读代码，不读注释」的一个重要事实**：模块头注释（[fpga_self_test.v:3-17](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/fpga_self_test.v#L3-L17)）说 Test 4 会「dump N samples to host」。但当你去顶层搜 `capture_data` 的去向（见 4.3.3），会发现它在当前 HEAD **并未接入 USB 数据通路**——只有 PASS/FAIL 标志被回读。也就是说，此刻 Test 4 实际只回答「ADC→DDC 这条链路有没有在 10 µs 内吐出有效样本」，并不能把原始样本导给主机看。注释描述的是设计意图，代码描述的是当前行为，两者不一致时以代码为准（这也是 u1-l4 强调过的原则）。

#### 4.1.4 代码实践

**实践目标**：通过阅读 FSM，确认 5 项测试各自设置了 `result_flags` 的哪一位、`result_detail` 在什么情况下被写值。

**操作步骤**：

1. 打开 [fpga_self_test.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/fpga_self_test.v)。
2. 全文搜索 `result_flags[`，列出每一处赋值所在的测试与状态。
3. 全文搜索 `result_detail`，列出所有写 `result_detail` 的位置。

**需要观察的现象**：

- `result_flags[0]` 在 `ST_BRAM_CHK` 写入（Test 0）。
- `result_flags[1]` 在 `ST_CIC_SETUP` 写入（Test 1）。
- `result_flags[2]` 在 `ST_FFT_SETUP` 写入（Test 2）。
- `result_flags[3]` 在 `ST_ARITH` 写入（Test 3）。
- `result_flags[4]` 在 `ST_ADC_CAP` 写入（Test 4），且分 PASS/FAIL 两条路径。
- `result_detail` 只被两处写值：BRAM 失败时（`{4'd0, bram_rd_addr_d[3:0]}`）与 ADC 超时时（`8'hAD`）；CIC/FFT/算术测试**不写** detail。

**预期结果**：你会得到一张「bit → 测试 → 状态 → detail 是否写」的对照表。这说明 `result_detail` 不是「每个失败测试都填」，而是只有 BRAM 与 ADC 两项能提供定位信息——其余三项只有 pass/fail 一个比特。这是后续写主机判定脚本时要记住的限制。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ST_BRAM_GAP` 这个状态是必要的？删掉它会怎样？

> **答案**：同步 BRAM 的写在时钟上升沿才生效。如果写完最后一个地址（`bram_addr == 63`）的同一拍立刻进入读阶段并发出地址，读到的可能是**写之前**的旧值。留 1 拍间隔，让第 63 号地址的写真正落地，读回比对才可靠。

**练习 2**：Test 2 的蝶形检查是 `(16'sd100 + 16'sd100 == 16'sd200) && (16'sd100 - 16'sd100 == 16'sd0)`。这个检查几乎不可能失败——既然如此，它存在的意义是什么？

> **答案**：它是「算术占位」。完整的 FFT 由 `xfft_16`/`fft_engine` 实现，资源太重不能塞进自测试。这里只验证 FFT 依赖的最底层蝶形 `(A+B, A-B)` 在综合后没有出错（比如工具把符号位搞反、把减法综合成加法）。它能抓的是「综合/实现级别的低级错误」，而非「FFT 算法实现错误」——后者归 `run_regression.sh` 与 formal。

---

### 4.2 结果锁存：5 位 flags + 8 位 detail

#### 4.2.1 概念说明

`result_valid` 只有**一拍**高电平。如果让主机直接去抓这一拍，几乎不可能撞上——USB 状态包是请求驱动的，主机什么时候发请求、FPGA 什么时候回，和自测试完成的那一拍完全不同步。

解决方案是顶层加一段**锁存逻辑**：`result_valid` 一拉高，就把 `result_flags` 和 `result_detail` 抄进两个保持寄存器 `self_test_flags_latched` / `self_test_detail_latched`，一直保持到下一次触发。主机随后任何时刻读状态包，拿到的都是最近一次自测试的结果。

#### 4.2.2 核心流程

锁存/读取的时序关系如下：

```
自测试 FSM                      顶层锁存器                 主机
─────────────                  ────────────              ─────────
ST_DONE:                       result_valid 到来:
  result_valid ─┐ 一拍          flags_latched  ◄── flags    (发 0xFF)
  busy=0        │               detail_latched ◄── detail   (解析 word 5)
                ▼               保持不变, 直到下次 trigger   得到 flags/detail/busy
             ST_IDLE
```

注意 `busy` 信号**不锁存**——它直接从 FSM 引出（`self_test_busy`），因为它表达的是「此刻正在跑」，锁存了就失去实时意义。主机靠 `busy` 判断「自测试跑完没」，靠锁存的 `flags`/`detail` 判断「跑出来啥结果」。

#### 4.2.3 源码精读

**顶层寄存器与 wire 声明**——区分「模块输出 wire」与「顶层锁存 reg」：

[radar_system_top.v:280-292](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L280-L292) 声明了 `self_test_result_flags`/`result_detail`/`busy`（wire，接模块输出）和 `self_test_flags_latched`/`detail_latched`（reg，锁存用）。`host_self_test_trigger`（opcode 0x30 的触发）也在此声明。

**模块例化**——`trigger` 输入接 opcode 译码，ADC 输入接 DDC 抽头：

[radar_system_top.v:671-684](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L671-L684) 关键接线：

```verilog
fpga_self_test self_test_inst (
    .clk(clk_100m_buf),                  // 跑在 100 MHz 系统域
    .reset_n(sys_reset_n),
    .trigger(host_self_test_trigger),    // opcode 0x30 译出的单拍脉冲
    ...
    .adc_data_in(rx_dbg_adc_i),          // 复用 DDC 输出 I 通道（见 4.2.4）
    .adc_valid_in(rx_dbg_adc_valid),
    .capture_active(self_test_capture_active),
    .capture_data(self_test_capture_data),
    .capture_valid(self_test_capture_valid)
);
```

**锁存 always 块**——结果就地保持：

[radar_system_top.v:686-697](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L686-L697)：

```verilog
always @(posedge clk_100m_buf or negedge sys_reset_n) begin
    if (!sys_reset_n) begin
        self_test_flags_latched  <= 5'b00000;
        self_test_detail_latched <= 8'd0;
    end else begin
        if (self_test_result_valid) begin
            self_test_flags_latched  <= self_test_result_flags;
            self_test_detail_latched <= self_test_result_detail;
        end
    end
end
```

注意这是 `if` 而非 `if/else`——当 `result_valid` 为 0 时**不写**，寄存器保持原值。这正是「锁存」的写法：只在有效脉冲到来时刷新，其余时间冻结。

#### 4.2.4 代码实践（ADC 抽头复用追踪）

**实践目标**：搞清 Test 4 用的 ADC 数据到底从哪儿来、是不是真的 ADC 原始样本。

**操作步骤**：

1. 在 [radar_system_top.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v) 搜 `rx_dbg_adc_i`，找到它的声明、来源、消费点。
2. 进入 [radar_receiver_final.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v)，看 `dbg_adc_i` 被赋成什么。

**需要观察的现象**：

- [radar_receiver_final.v:69-72](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L69-L72) 把 `dbg_adc_i/dbg_adc_q/dbg_adc_valid` 声明为输出，注释写「DDC output I/Q (16-bit signed, 100 MHz)」。
- [radar_receiver_final.v:491-494](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L491-L494) `assign dbg_adc_i = adc_i_scaled;` —— 它接的是 DDC 输出**经过缩放**的 I 通道，不是 AD9484 的原始 400 MHz 实样本。

**预期结果**：Test 4 的「ADC 抓取」名义上测 ADC，实际测的是 **ADC → DDC → 缩放** 这整条 100 MHz 域链路「有没有数据在流」。命名（`adc_data_in`）是历史遗留，真实信号是后 DDC 的 I 通道。这也是「读代码不读名字」的一例。

> 待本地验证：若要在仿真里观察 Test 4 的抓取波形，可在 `tb_fullchain_realdata.v`（见 [u11-l1](u11-l1-fpga-regression-and-cosim.md)）里激励 `rx_dbg_adc_valid`，再 dump `self_test_inst` 内部的 `adc_cap_cnt` 与 `result_flags[4]`。

#### 4.2.5 小练习与答案

**练习 1**：锁存 always 块里为什么用 `if (!sys_reset_n) ... else if (result_valid)` 而不是 `if/else if/else`（即不给 else 分支）？

> **答案**：不给 else 分支，意味着 `result_valid == 0` 时寄存器**不写**，综合出的是带使能的 D 触发器（CE= result_valid），保持原值。若写成 `else <= 某值`，每拍都会刷新，就失去「锁存最近一次结果」的语义。

**练习 2**：`busy` 为什么不锁存？

> **答案**：`busy` 表达「正在跑」，是实时状态。锁存它会让主机永远看到某个旧值（比如一直 busy=1 或一直 busy=0），无法判断当前这次自测试是否已结束。所以 `busy` 直接从 FSM 引出，只有结果（flags/detail）才锁存。

---

### 4.3 状态回读：opcode 0x30 触发与 0x31/0xFF 回读

#### 4.3.1 概念说明

自测试的「触发」与「回读」是两条独立的命令路径，对应不同 opcode：

- **opcode 0x30**：触发自测试（self-clearing pulse）。
- **opcode 0x31** 或 **0xFF**：请求状态包。`0xFF` 是通用状态请求，`0x31` 是它的别名（alias），两者在 Verilog 里都译成同一个 `host_status_request` 脉冲。状态包的 word 5 里就带着锁存的自测试结果。

这条闭环把本讲和 [u6-l2](u6-l2-host-command-protocol.md) 紧密连起来：opcode 是跨层硬契约，Python 的 `Opcode` 枚举必须和 Verilog 的 `case` 表一一对应。

#### 4.3.2 核心流程

一次完整的「触发 → 等待 → 读结果」交互如下：

```
主机                                 FPGA (100 MHz 域)
────                                 ────────────────
发 build_command(0x30, 0, 0)  ──►    opcode 0x30 → host_self_test_trigger 单拍脉冲
                                     self_test_inst 进入 ST_BRAM_WR, busy=1

轮询: 发 build_command(0xFF,..) ──►  host_status_request → 回 26 字节状态包
     ◄── busy=1                  循环...
发 0xFF ...                        ◄── busy=1
...
发 0xFF                            ◄── busy=0, flags=0b11111, detail=0x00
                                      (自测试已完成, 5 项全过)
解析 word 5, 判定通过
```

word 5 的位布局（来自 [radar_protocol.py:257-261](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L257-L261) 的注释与代码）：

\[
\text{word5} = \{\,7'd0,\ \text{busy},\ 8'd0,\ \text{detail}[7{:}0],\ 3'd0,\ \text{flags}[4{:}0]\,\}
\]

即从低位到高位：bit[4:0] = flags，bit[15:8] = detail，bit[24] = busy。所以 Python 解析时：

```python
sr.self_test_flags  = words[5] & 0x1F            # 低 5 位
sr.self_test_detail = (words[5] >> 8) & 0xFF     # 第 8..15 位
sr.self_test_busy   = (words[5] >> 24) & 0x01    # 第 24 位
```

#### 4.3.3 源码精读

**opcode 译码**——触发与回读在同一张 case 表：

[radar_system_top.v:993-997](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L993-L997)：

```verilog
// Board bring-up self-test opcodes
8'h30: host_self_test_trigger  <= 1'b1;  // 触发自测试
8'h31: host_status_request     <= 1'b1;  // 自测试回读（状态别名）
// 0x31: readback handled via status mechanism (latched results)
8'hFF: host_status_request     <= 1'b1;  // 通用状态回读
```

注意 `0x31` 与 `0xFF` 走的是**同一条机制**（都拉 `host_status_request`），区别仅在语义。注释也写明：0x31 的回读就是「走状态机制读锁存结果」，没有单独的回读通路。

**自清零写法**——本讲信号如何变成单拍脉冲：

[radar_system_top.v:946-948](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L946-L948) 在 `else`（非复位）分支顶部先把三个触发类信号默认清零：

```verilog
host_trigger_pulse <= 1'b0;
host_status_request <= 1'b0;
host_self_test_trigger <= 1'b0;   // 默认每拍清零
```

由于同一 `always` 块内 case 里的 `8'h30: ... <= 1'b1` 写在更后面，命中那拍覆盖为 1，下一拍回到 0——单拍脉冲由此而来（见 2.2 节）。复位默认值见 [radar_system_top.v:944](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L944)。

**状态包接入**——锁存结果与 busy 都送进 USB 模块：

[radar_system_top.v:774-777](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L774-L777)（FT601 分支）与 [radar_system_top.v:841-844](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L841-L844)（FT2232H 分支）都把同一组信号接到 USB 模块的 `status_self_test_*` 端口：

```verilog
.status_self_test_flags(self_test_flags_latched),
.status_self_test_detail(self_test_detail_latched),
.status_self_test_busy(self_test_busy),
```

这正是 [u6-l1](u6-l1-usb-data-interface.md) 讲的「两套 USB 模块经 generate 二选一、共用同一组内部信号」的体现——换板（FT601↔FT2232H）不影响自测试回读。

**Python 端 Opcode 枚举与解析**——跨层契约的另一侧：

[radar_protocol.py:100-103](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L100-L103) 定义了三个 opcode，与 Verilog 的 case 表逐项对应：

```python
# --- Board self-test / status (0x30-0x31, 0xFF) ---
SELF_TEST_TRIGGER   = 0x30
SELF_TEST_STATUS    = 0x31
STATUS_REQUEST      = 0xFF
```

[radar_protocol.py:141-144](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L141-L144) 在 `StatusResponse` 数据类里声明三个字段（注释「added in Build 26」说明这是后期补丁新增的）。命令拼装与状态解析见 [radar_protocol.py:168-175](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L168-L175) 的 `build_command`（大端拼 32 位 `{opcode, addr, value}`）与 [radar_protocol.py:257-261](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L257-L261) 的 word 5 切分。

#### 4.3.4 代码实践

**实践目标**：编写一段主机端判定脚本，触发自测试、轮询状态包、判定 5 项子系统是否通过。本实践为「源码阅读 + 伪代码」型，因为没有真实硬件时无法运行（待本地验证）。

**操作步骤**：

1. 确认本机已 `uv sync --group dev`（见 [u1-l4](u1-l4-toolchain-and-running.md)），`radar_protocol.py` 可被导入。
2. 阅读下述伪代码，对照 4.3.2 的 word 5 位布局，理解每一行在解什么。
3. 若有真实板子与 USB 连接，把 `transport.write` / `transport.read` 换成实际 FT601/FT2232H 读写（见 [u6-l1](u6-l1-usb-data-interface.md)）。

**检查脚本伪代码**（示例代码，非项目原有文件）：

```python
# 示例代码：板级自测试主机判定脚本（伪代码）
from radar_protocol import RadarProtocol, Opcode

TEST_NAMES = ["BRAM", "CIC-arith", "FFT-butterfly", "Saturating-add", "ADC-tap"]

def run_self_test(transport, timeout_s=2.0):
    # 1. 触发自测试（opcode 0x30，value/addr 无关紧要）
    transport.write(RadarProtocol.build_command(Opcode.SELF_TEST_TRIGGER, 0, 0))

    # 2. 轮询状态包，等 busy 变 0
    status = None
    while timeout_s > 0:
        transport.write(RadarProtocol.build_command(Opcode.STATUS_REQUEST, 0, 0))
        raw = transport.read(26)                       # 26 字节状态包
        status = RadarProtocol.parse_status_packet(raw)
        if status is None:
            continue                                   # 帧头/尾校验失败，重读
        if not status.self_test_busy:
            break                                      # 自测试已完成
        timeout_s -= poll_interval
    else:
        raise TimeoutError("self-test did not finish (busy stuck high)")

    # 3. 判定每一项子系统
    flags = status.self_test_flags          # 5 位
    detail = status.self_test_detail        # 8 位
    all_pass = True
    for i, name in enumerate(TEST_NAMES):
        ok = (flags >> i) & 0x1
        print(f"Test {i} {name:16s}: {'PASS' if ok else 'FAIL'}")
        if not ok:
            all_pass = False

    # 4. 解释 detail（只有 BRAM 失败与 ADC 超时会写 detail）
    if not (flags & 0x1):                                # Test 0 FAIL
        print(f"  BRAM 失败地址低 4 位: 0x{detail & 0x0F:X}")
    if not (flags & 0x10):                               # Test 4 FAIL
        if detail == 0xAD:
            print("  ADC 抽头超时：1000 周期内无 valid 样本（检查 ADC/DDC/时钟）")
        else:
            print(f"  ADC 抓取失败，detail=0x{detail:02X}")

    return all_pass
```

**需要观察的现象 / 预期结果**：

- 板子正常时，`flags == 0b11111`（0x1F），`detail == 0x00`，脚本打印 5 行 PASS。
- 若 ADC 链路没起来（如 AD9484 没时钟、DDC 没使能），`flags` 低位（bit0..3）为 1 但 bit4 为 0，`detail == 0xAD`。
- 若 BRAM 物理故障（极少见），bit0 为 0，`detail` 低 4 位给出失败地址。

> 待本地验证：无硬件时，可用仿真 testbench（如 `tb_fullchain_realdata.v`）在波形里手动核对 `self_test_inst` 的 `result_flags`/`result_detail`，验证上述判定逻辑的位映射正确。

#### 4.3.5 小练习与答案

**练习 1**：opcode `0x31` 和 `0xFF` 在 Verilog 里走的是同一个信号（`host_status_request`）。既然如此，为什么要保留两个 opcode？

> **答案**：语义区分与可读性。`0xFF` 是「通用状态请求」，`0x31` 是「自测试回读」的语义化别名。两者此刻行为相同，但保留独立 opcode 给未来留出分化空间——例如将来可能让 `0x31` 触发一次「自测试专用」的扩展状态包，而不影响 `0xFF` 的通用用途。这也是跨层契约里「先占编号、再分化行为」的常见做法。

**练习 2**：如果主机在 `busy` 还是 1 的时候就解析 word 5，会发生什么？

> **答案**：会读到**上一次**自测试的锁存结果（或复位默认值 `flags=0, detail=0`），而不是当前正在跑的这次。因为锁存器只在 `result_valid`（即 ST_DONE 那拍）刷新。所以脚本必须先轮询到 `busy==0` 再读 flags——这也是 4.3.4 脚本里 while 循环存在的理由。

---

## 5. 综合实践

把三个最小模块串起来，完成一次「自测试全链路」走读。

**任务**：假设你拿到一块新板子，怀疑 ADC 链路有问题。请设计一套排查流程，把本讲的知识用上。

**建议步骤**：

1. **触发**：用 `RadarProtocol.build_command(Opcode.SELF_TEST_TRIGGER, 0, 0)` 下发 opcode 0x30。回到 [radar_system_top.v:994](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L994) 确认它被译成 `host_self_test_trigger` 单拍脉冲。
2. **追踪触发到 FSM**：脉冲进入 [fpga_self_test.v:153-164](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/fpga_self_test.v#L153-L164) 的 `ST_IDLE`，置 `busy=1`，跳到 `ST_BRAM_WR`。
3. **理解 Test 4 数据来源**：在 [radar_system_top.v:679-680](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L679-L680) 确认 `adc_data_in = rx_dbg_adc_i`，再追到 [radar_receiver_final.v:491-494](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L491-L494) 确认它是后 DDC 的 I 通道——所以「Test 4 失败」可能源自 ADC、DDC、或时钟任一环节。
4. **轮询回读**：用 4.3.4 的脚本反复发 `0xFF`，等 `busy==0`。
5. **判定**：若 `flags & 0x10 == 0` 且 `detail == 0xAD`，定位到 ADC 超时。结合 [fpga_self_test.v:299-306](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/fpga_self_test.v#L299-L306) 的超时阈值（1000 拍 = 10 µs @ 100 MHz），说明 DDC 在 10 µs 内一个有效样本都没吐——下一步就该去查 AD9523 时钟（[u7-l2](u7-l2-clock-and-frequency-synthesis.md)）或 AD9484 接口（[u4-l1](u4-l1-ddc-digital-downconversion.md)）。

**交付物**：一张「症状 → flags/detail → 可能故障域 → 下一讲排查入口」的对照表。

## 6. 本讲小结

- `fpga_self_test` 是板级 **smoke test**，上电后挨个戳 BRAM、算术原语、ADC 抽头，回答「这块物理板此刻是否活着」，与仿真回归、formal 三层验证各司其职。
- 12 状态 FSM 跑 5 项测试，**失败不中断**、一次汇报全部结果；Test 0/3/4 是真测试，Test 1/2 是受资源预算（~200 LUT）限制的算术占位。
- 结果编码：`result_flags[4:0]` 每位对应一项测试（PASS=1），`result_detail[7:0]` 仅在 BRAM 失败（地址低 4 位）与 ADC 超时（`0xAD`）时给定位信息。
- 顶层用带使能的锁存器在 `result_valid` 单拍脉冲时冻结 flags/detail，`busy` 不锁存以保持实时性。
- 闭环：opcode `0x30` 触发（self-clearing 单拍脉冲）→ FSM 自动跑完 → 结果锁存 → opcode `0x31`/`0xFF` 请求 26 字节状态包，从 word 5 的 `{busy[24], detail[15:8], flags[4:0]}` 读回。
- **读代码不读注释**：Test 4 的 `capture_data` 在当前 HEAD 未接 USB（只回读 PASS/FAIL），且 `adc_data_in` 实为后 DDC 的 I 通道而非原始 ADC——命名与注释会滞后，以代码为准。

## 7. 下一步学习建议

- 继续本单元：读 [u10-l2 硬件 Bring-up 流程与构建产物](u10-l2-board-bringup.md)，把自测试放进首次上电的分阶段检查清单里，理解 heartbeat `.bit` 与开发板构建产物的关系。
- 横向对照验证体系：读 [u11-l1 FPGA 回归测试与协同仿真](u11-l1-fpga-regression-and-cosim.md)，理解自测试（板级、运行时）与 `run_regression.sh`（交付前、仿真时）的分工与互补。
- 若想扩展自测试：参考 [u14-l2 二次开发扩展点](u14-l2-extension-points.md)，练习「在 FSM 加一项新测试 + 分配新的 flags 位 + 同步更新 word 5 位布局与 Python 解析」的端到端改动。
- 想要证明「状态机某些性质恒成立」（而不仅仅是跑一次看结果），进入 [u14-l1 形式化验证](u14-l1-formal-verification.md)。
