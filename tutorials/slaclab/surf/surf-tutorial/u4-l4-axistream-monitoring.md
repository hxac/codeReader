# AXI-Stream 监控、抽头与测试码型

## 1. 本讲目标

本讲讲解 SURF 在 AXI-Stream 数据平面上提供的三类「旁路」工具。学完后你应该能够：

- 用 `AxiStreamMon` / `AxiStreamMonAxiL` 在不改动数据通路的前提下，统计一条流的**帧数、帧长、帧率、带宽**，并通过 AXI-Lite 读回。
- 理解 `AxiStreamTap` 为什么是一个**非侵入式**的调试观测点，以及它如何既能「抽出」也能「重新注入」某个目的（tDest）的帧。
- 会用 `AxiStreamPrbsFlowCtrl` 产生**伪随机反压**，对上下游握手做压力自检。
- 看懂这三个模块如何复用 u4-l1 的记录化总线与 u1-l5 的双进程骨架。

一句话定位：本讲三个模块都不改变流的「正常数据」，它们只负责**看（Mon）、分流（Tap）、捣乱（PrbsFlowCtrl）**，是调试与回归测试的三大旁路利器。

## 2. 前置知识

本讲假设你已经掌握：

- **AXI-Stream 记录与配置**（u4-l1）：`AxiStreamMasterType` / `AxiStreamSlaveType` 记录、`tValid`/`tReady` 握手、`AxiStreamConfigType`（尤其是 `TDATA_BYTES_C`、`TKEEP_MODE_C`）以及 `getTKeep`、`AXI_STREAM_MASTER_INIT_C` 等包函数。
- **AXI-Lite 寄存器端点**（u3-l2）：内存映射寄存器、地址解码、响应码（OK/SLVERR/DECERR）。本讲的 `AxiStreamMonAxiL` 不是用 helper 过程，而是用一块 `AxiDualPortRam` 把统计值「滚动写入」一个 RAM，再让 CPU 经 AXI-Lite 读这片 RAM。
- **双进程 RTL 风格**（u1-l5）：`RegType` / `REG_INIT_C` / `r` / `rin` / `comb` / `seq` 三明治骨架，以及 `TPD_G`、`RST_POLARITY_G`、`RST_ASYNC_G` 三复位泛型。
- **基础库原语**（u2-l1、u2-l2、u2-l5）：`RstSync`、`SynchronizerFifo`（跨时钟域搬运多比特）、`SyncTrigRate`/`SyncMinMax`（速率与极值统计）、`lfsrShift`（LFSR 伪随机）。

两个本讲会反复用到的关键事实，先点明：

1. **AXI-Stream 的「数据在动」判定**：当且仅当 `axisMaster.tValid = '1'` 且 `axisSlave.tReady = '1'` 时，本拍才真正搬运了一个字（word）的数据。所有统计都建立在这个「握手成功」的判定上。
2. **`AxiStreamConfigType` 决定了「一个字有多少字节」**：`TDATA_BYTES_C` 给出每拍字节数；`tKeep` 在 `TKEEP_NORMAL_C` 模式下是「每字节一位的有效位掩码」（可用 `getTKeep` 把它折算成「本拍有效字节数」），而在 `TKEEP_COUNT_C` 模式下 `tKeep` 本身就直接是「有效字节数」。这正是统计「字节数」时要区分两种模式的原因。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [axi/axi-stream/rtl/AxiStreamMon.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMon.vhd) | **统计核**：在流的时钟域里数帧、数字节、测帧率/带宽，并把结果跨时钟域搬到 `statusClk`。 |
| [axi/axi-stream/rtl/AxiStreamMonAxiL.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMonAxiL.vhd) | **AXI-Lite 外壳**：把 N 个 `AxiStreamMon` 的统计值滚动写入一片 RAM，CPU 经 AXI-Lite 读回。 |
| [axi/axi-stream/rtl/AxiStreamTap.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamTap.vhd) | **调试抽头**：用一对 DeMux/Mux 抽出（并支持重新注入）某个 `tDest` 的帧。 |
| [axi/axi-stream/rtl/AxiStreamPrbsFlowCtrl.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPrbsFlowCtrl.vhd) | **伪随机反压发生器**：用 LFSR 随机拉 `tReady`，给上下游握手施加压力。 |

配套测试（实践会用到）：

| 文件 | 覆盖模块 |
| --- | --- |
| [tests/axi/axi_stream/test_AxiStreamMonAxiL.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/axi_stream/test_AxiStreamMonAxiL.py) | 外壳的配置/调试寄存器读回 |
| [tests/axi/axi_stream/test_AxiStreamTap.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/axi_stream/test_AxiStreamTap.py) | 抽出 + 重新注入 |
| [tests/axi/axi_stream/test_AxiStreamPrbsFlowCtrl.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/axi_stream/test_AxiStreamPrbsFlowCtrl.py) | threshold=0 放行 / threshold=全 1 阻塞 |

---

## 4. 核心概念与源码讲解

### 4.1 流量监控：AxiStreamMon + AxiStreamMonAxiL

#### 4.1.1 概念说明

一条 AXI-Stream 流在网上跑起来后，你常常会想知道三件事：

1. **到现在为止一共传了多少帧？**（`frameCnt`，64 位，自统计复位以来单调递增）
2. **每帧多大？最近一帧 / 历史最大 / 历史最小分别是多少字节？**（`frameSize` / `frameSizeMax` / `frameSizeMin`）
3. **流量有多快？**——帧率（`frameRate`，单位 Hz）与带宽（`bandwidth`，单位 Byte/s），同样带历史最大/最小。

`AxiStreamMon` 就是回答这些问题的**纯旁路统计核**：它只「听」`axisMaster`/`axisSlave` 这对握手信号，**不输出任何回送给数据通路的 `tReady`**，因此把它插进设计里不会改变流的时序与功能。统计在数据时钟域 `axisClk` 里完成，结果再跨时钟域搬到 `statusClk` 输出。

`AxiStreamMonAxiL` 则是套在 `AxiStreamMon` 外面的**AXI-Lite 外壳**：它例化 N 个监控核（`AXIS_NUM_SLOTS_G`），用一个「滚动写入器」把每个核的统计值周期性地刷进一片 `AxiDualPortRam`，CPU 只要像读普通内存映射寄存器那样读这片 RAM，就能拿到所有通道的最新统计。这种「RAM 影子寄存器」做法避免了为每个统计值手写一行 `axiSlaveRegister`。

#### 4.1.2 核心流程

`AxiStreamMon` 内部其实只有两个计数循环，外加一个速率测量器：

```text
每一拍 (axisClk) 在 comb 进程里：
  data_moving  = axisMaster.tValid and axisSlave.tReady   # 本拍是否搬运
  frame_sent   = data_moving and axisMaster.tLast          # 本拍是否结束一帧

  if data_moving:  accum      += 本拍有效字节数(getTKeep/tKeep)   # 累加字节
                   frameAccum += 本拍有效字节数                  # 当前帧累计字节
  if frame_sent:  frameCnt    += 1                              # 帧计数 +1
                   若已 armed: frameSize := frameAccum          # 锁存本帧大小
                   frameAccum := 0;  armed := true              # 复位帧累加，完成「武装」

  timer += 1
  if timer == TIMEOUT_C:   # TIMEOUT_C+1 拍 = 恰好 1 秒（见下）
     bandwidth := accum     # 这 1 秒内的字节数 = Byte/s
     accum 重启；timer 清零；updated := '1'

旁路：
  SyncTrigRate(frame_sent) -> frameRate / Max / Min   (1 秒窗口的帧数 = Hz)
  SyncMinMax(frameSize)    -> frameSize / Min / Max   (跨域 + 极值)
  SyncMinMax(bandwidth)    -> bandwidth / Min / Max
  SynchronizerFifo(frameCnt) -> frameCnt              (64 位跨域)
```

三个关键设计巧思：

- **「1 秒窗口」让计数直接等于速率**。`TIMEOUT_C = getTimeRatio(AXIS_CLK_FREQ_G, 1.0) - 1`，即窗口长度为 `AXIS_CLK_FREQ_G` 拍。因为 `AXIS_CLK_FREQ_G` 的单位是 Hz（每秒时钟数），所以这个窗口**恰好是 1 实秒**。于是 1 秒内数到的字节数 `accum` 直接就是 Byte/s，数到的帧数直接就是 Hz——无需再做除法。
- **第一帧用于「武装」（arming）**。`armed` 初值为 `'0'`，只有在看到第一个完整帧结束后才置 `'1'`。这是为了排除「复位时正卡在一帧中间」造成的半帧统计。代价是：**复位后的第一帧大小不会被上报**，从第二帧起才有有效的 `frameSize`。
- **速率/带宽统计只对真实硬件时钟有意义**。默认 `AXIS_CLK_FREQ_G = 156.25E+6`，意味着 `TIMEOUT_C ≈ 1.56 亿拍`。在 cocotb/GHDL 短仿真里根本跑不到一个窗口，所以 `bandwidth` / `frameRate` 在短测试中通常是 0——这是预期行为，不是 bug（详见 4.1.4）。

#### 4.1.3 源码精读

**端口与泛型**。统计核的输入只有「听」用的 `axisMaster`/`axisSlave`，没有任何回送给数据通路的输出，体现「非侵入」：

见 [AxiStreamMon.vhd:24-51](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMon.vhd#L24-L51)——注意 `axisSlave : in AxiStreamSlaveType` 是**输入**（拿来读 `tReady`），不是输出，所以它不驱动流。`COMMON_CLK_G` 表示 `axisClk` 是否等于 `statusClk`，决定跨域 FIFO 是否退化。

**两个编译期常量**：`TKEEP_C` 记下每拍字节数，`TIMEOUT_C` 是 1 秒窗口的拍数减一：

```vhdl
constant TKEEP_C   : natural := AXIS_CONFIG_G.TDATA_BYTES_C;
constant TIMEOUT_C : natural := getTimeRatio(AXIS_CLK_FREQ_G, 1.0)-1;
```

见 [AxiStreamMon.vhd:55-56](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMon.vhd#L55-L56)。

**统计主循环（comb 进程）**。这是全模块的心脏，先看「帧结束判定 + 帧计数」：

```vhdl
-- Check for end of frame
v.frameSent := axisMaster.tValid and axisMaster.tLast and axisSlave.tReady;
-- Increment frame counter if end of frame detected
if (r.frameSent = '1') then
   v.frameCnt := r.frameCnt + 1;
end if;
```

见 [AxiStreamMon.vhd:206-212](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMon.vhd#L206-L212)。注意 `frameSent` 同时要求 `tValid`、`tLast`、`tReady` 三者——只有「真正被搬走的最后一拍」才算一帧结束。

再看「字节累加 + 帧大小锁存 + 武装逻辑」：

```vhdl
-- Check if last cycle had data moving
if (r.tValid = '1') then
   -- 按 TKEEP_MODE 选「掩码折算」或「直接计数」
   if (AXIS_CONFIG_G.TKEEP_MODE_C = TKEEP_COUNT_C) then
      v.accum      := r.accum + conv_integer(r.tKeep(...));
      v.frameAccum := r.frameAccum + conv_integer(r.tKeep(...));
   else
      v.accum      := r.accum + getTKeep(r.tKeep, AXIS_CONFIG_G);
      v.frameAccum := r.frameAccum + getTKeep(r.tKeep, AXIS_CONFIG_G);
   end if;
   -- Check for end of frame
   if (r.frameSent = '1') then
      v.sizeValid  := r.armed;          -- 只有已武装才上报
      v.frameSize  := v.frameAccum;     -- 锁存本帧大小
      v.frameAccum := (others => '0');  -- 复位帧累加
      v.armed      := '1';              -- 完成武装
   end if;
end if;
```

见 [AxiStreamMon.vhd:222-245](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMon.vhd#L222-L245)。注意 `tKeep` 是在「上一拍有数据移动」时被采样进 `r.tKeep` 的（[L215-220](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMon.vhd#L215-L220)），所以这里用 `r.tValid`/`r.tKeep` 而不是当拍值——典型的「打一拍再算」时序对齐。

**1 秒窗口与带宽刷新**：

```vhdl
v.timer := r.timer + 1;
if r.timer = TIMEOUT_C then
   v.timer     := 0;
   v.updated   := '1';
   v.bandwidth := r.accum;   -- 1 秒窗口内的字节数 = Byte/s
   ...
end if;
```

见 [AxiStreamMon.vhd:247-267](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMon.vhd#L247-L267)。

**双进程骨架**：`comb` 算次态、`seq` 打寄存器，复位走 u1-l5 的标准三泛型约定，见 [AxiStreamMon.vhd:269-286](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMon.vhd#L269-L286)。

**旁路子模块**：帧率由 `SyncTrigRate` 测量（把 `frameSent` 当触发，1 秒窗口计数即 Hz），见 [AxiStreamMon.vhd:116-136](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMon.vhd#L116-L136)；帧大小与带宽的「当前/最小/最大」由两个 `SyncMinMax` 跨域并维护极值，见 [AxiStreamMon.vhd:288-324](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMon.vhd#L288-L324)；`frameCnt` 是 64 位，用 `SynchronizerFifo` 跨域，见 [AxiStreamMon.vhd:182-193](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMon.vhd#L182-L193)。

> 顺带说一句 `SyncMinMax` 的语义：它内部也有一个 `armed` 机制，复位后第一次写入只用来「武装」（建立基准），从第二次写入起才更新 `dataOut/dataMin/dataMax`。这与 `AxiStreamMon` 自己的 `armed` 是**两层独立的武装**，互不干扰。

---

**AxiStreamMonAxiL：RAM 影子寄存器外壳**。它的核心思想是：与其为 12+ 个统计值逐个写 `axiSlaveRegister`，不如让一个小状态机**每拍写一个字**，循环刷新一片 RAM，CPU 读 RAM 即可。

「滚动写入器」状态机每拍把 `cnt` 加 1 当作 RAM 地址，并用 `case` 按 `addr(3 downto 0)` 选出该写哪个统计值：

```vhdl
-- Increment the counter
v.cnt := r.cnt + 1;
-- Write the status counter to RAM
v.we   := '1';
v.addr := v.cnt;
-- Case on the word index
wrd := v.addr(3 downto 0);
case (wrd) is
   when x"0" =>  -- 配置字（TDATA_BYTES/TDEST_BITS/TKEEP_MODE...）
   when x"1" => v.data := frameCnt(r.ch)(31 downto 0);
   when x"2" => v.data := frameCnt(r.ch)(63 downto 32);
   ...
   when x"C" => v.data := frameSize(r.ch);
   ...
end case;
```

见 [AxiStreamMonAxiL.vhd:196-263](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMonAxiL.vhd#L196-L263)。`r.ch` 是当前通道号，写满 16 个字（`wrd = x"F"`）后切换到下一通道，见 [AxiStreamMonAxiL.vhd:265-278](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMonAxiL.vhd#L265-L278)。由此得到一张**固定地址布局表**（每通道占 `0x40` 字节 = 16 个 32 位字）：

| 通道 i 的字节偏移 | 含义 |
| --- | --- |
| `i*0x40 + 0x00` | 配置字（TDATA_BYTES / TDEST_BITS / TUSER_BITS / TID_BITS / TKEEP_MODE / TUSER_MODE / TSTRB_EN / COMMON_CLK） |
| `i*0x40 + 0x04` / `0x08` | `frameCnt` 低 32 位 / 高 32 位 |
| `i*0x40 + 0x0C` / `0x10` / `0x14` | `frameRate` / `frameRateMax` / `frameRateMin` |
| `i*0x40 + 0x18`..`0x2C` | `bandwidth` / `Max` / `Min`（各 64 位，拆成高低两个字） |
| `i*0x40 + 0x30` / `0x34` / `0x38` | `frameSize` / `frameSizeMax` / `frameSizeMin` |
| `i*0x40 + 0x3C` | 调试字（`AXIS_NUM_SLOTS_G`、`ADDR_WIDTH_C`、当前 `ch`） |

RAM 本身由 `AxiDualPortRam` 提供：A 口给 AXI-Lite（只读，`AXI_WR_EN_G => false`），B 口给硬件写入（`SYS_WR_EN_G => true`），见 [AxiStreamMonAxiL.vhd:159-185](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMonAxiL.vhd#L159-L185)。地址宽度由通道数推导：`ADDR_WIDTH_C : positive := bitSize(AXIS_NUM_SLOTS_G*16-1)`，见 [AxiStreamMonAxiL.vhd:51](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMonAxiL.vhd#L51)。

**「软件清零」技巧**。外壳还解码一个特殊的 AXI-Lite 写：向地址 `0x0` 写任意值，会把所有统计计数器复位。实现是「写地址译码 + 本地复位」：

```vhdl
-- Only doing a write address decode of 0x0
rstCnt          <= sAxilWriteMaster.awvalid when(sAxilWriteMaster.awaddr(...) = 0) else '0';
sAxilWriteSlave <= AXI_LITE_WRITE_SLAVE_EMPTY_OK_C;
localReset <= axisRst or rstCnt when(RST_POLARITY_G = '1') else axisRst and not(rstCnt);
```

见 [AxiStreamMonAxiL.vhd:107-111](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMonAxiL.vhd#L107-L111)。`AXI_LITE_WRITE_SLAVE_EMPTY_OK_C` 是一个「永远就绪、永远回 OKAY」的写从机常量，表示「写我不管内容，只看地址」。`rstCnt` 拉起一个本地复位送给 `AxiStreamMon`，于是 `frameCnt` 等全部归零。这样软件无需动硬复位线，就能「开始一段新的测量窗口」。

#### 4.1.4 代码实践

> 这是本讲的主实践，对应任务：在一条流上挂 `AxiStreamMonAxiL`，读出帧计数与字节计数寄存器，验证与激励一致。

**1) 实践目标**：发送已知数量、已知字节数的帧，读回 `frameCnt`（帧计数）与 `frameSize`（字节计数），验证一致；并理解为何 `bandwidth` 在短仿真里读不到。

**2) 操作步骤**：仓库已有现成测试 `tests/axi/axi_stream/test_AxiStreamMonAxiL.py`，先跑通它，再扩展。按 u1-l2 / u9-l1 的回归栈，先做一次 `import` 生成源缓存，再跑该子系统：

```bash
make MODULES=$PWD import
./.venv/bin/python -m pytest -q tests/axi/axi_stream/test_AxiStreamMonAxiL.py
```

该测试（[test_AxiStreamMonAxiL.py:63-80](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/axi_stream/test_AxiStreamMonAxiL.py#L63-L80)）目前只发了 2 帧并读取 `0x00`（配置）和 `0x3C`（调试），断言配置字的高字节等于 `TDATA_BYTES_C`、调试字的通道数 ≥ 4：

```python
await tb.source.send(AxiStreamFrame(b"\x10\x11\x12"))           # 3 字节
await tb.source.send(AxiStreamFrame(b"\x20\x21\x22\x23\x24"))  # 5 字节
await tb.cycle(96)   # 等滚动写入器把统计刷进 RAM（≥1 个完整 16 字循环）
config = await tb.read_reg(0x00)
debug  = await tb.read_reg(0x3C)
assert (config >> 24) & 0xFF == 4      # TDATA_BYTES_C = 4
assert (debug >> 8) & 0xFF >= 4        # ADDR_WIDTH_C ≥ 4（通道数相关）
```

**在此基础上扩展**（示例代码，需自行加入测试文件）：读 `frameCnt` 与 `frameSize`：

```python
# 示例代码：扩展 test_AxiStreamMonAxiL.py 的协程
# frameCnt 在通道 0 的 0x04(低)/0x08(高)；frameSize 在 0x30
frame_cnt_lo = await tb.read_reg(0x04)
frame_cnt_hi = await tb.read_reg(0x08)
frame_size   = await tb.read_reg(0x30)
frame_cnt = frame_cnt_lo | (frame_cnt_hi << 32)
assert frame_cnt == 2, f"期望 2 帧，实读 {frame_cnt}"
# 注意：第一帧用于「武装」，frameSize 上报的是第二帧的大小 = 5 字节
assert frame_size == 5, f"期望末帧 5 字节，实读 {frame_size}"
```

**3) 需要观察的现象**：

- `frame_cnt == 2`：两帧都被计数。
- `frame_size == 5`：**不是 3，也不是 8**——因为第一帧（3 字节）被 `armed` 机制丢弃，`frameSize` 锁存的是最后一帧（5 字节）。
- 读 `0x18`/`0x1C`（`bandwidth`）大概率得到 0 或极小值。

**4) 预期结果**：`frameCnt` 与发送帧数一致；`frameSize` 与最后一帧字节数一致。`bandwidth` / `frameRate` 在默认 `AXIS_CLK_FREQ_G = 156.25E+6` 下需要约 1.56 亿拍（1 实秒）才刷新一次，短仿真跑不到——**待本地验证**：若一定要在仿真里看到带宽，可把 DUT 的 `AXIS_CLK_FREQ_G` 调成与仿真时钟真实频率同量级（例如仿真周期 5 ns ⇒ 真实约 200 MHz ⇒ 设 `AXIS_CLK_FREQ_G = 200.0E+6`），使 1 秒窗口缩短到可仿真规模，但这需要参数化 IP integrator 封装，属于进阶改造。

**5) 一个常见的坑**：读完寄存器前一定要留足 `cycle`。滚动写入器要写满当前通道的 16 个字才算一轮，`tb.cycle(96)` 对单通道足够（≈6 轮）。若你把 `AXIS_NUM_SLOTS_G` 调大，需要按 `16 * 通道数` 留出更多拍数，否则 RAM 里还是旧值。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `AxiStreamMon` 把 `axisSlave` 声明为 `in`（输入）而不是 `out`？如果把它的 `tReady` 接到数据通路上会发生什么？

**参考答案**：因为它只「旁听」握手是否成立（需要知道下游 `tReady` 才能判断本拍是否真的搬了数据），不驱动流。它的 `tReady` 不接进数据通路，正是「非侵入」的体现。如果反客为主去驱动 `tReady`，它就从一个观测点变成了一个会改变流时序的「参与者」，被监控链路的吞吐与反压行为都会被它改写，失去监控意义。

**练习 2**：复位后发送 1 帧（10 字节），读 `frameSize`（`0x30`）得到 0；再发送第 2 帧（7 字节），读 `frameSize` 得到 7。请用源码解释为什么第一帧的大小「丢了」。

**参考答案**：见 [AxiStreamMon.vhd:235-243](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMon.vhd#L235-L243)。帧结束时 `v.sizeValid := r.armed`，而 `armed` 初值为 `'0'`，只有在第一个完整帧结束后才置 `'1'`。所以第一帧结束时 `sizeValid = '0'`，其大小不送出（被「武装」过程丢弃）；第二帧结束时 `armed = '1'`，`frameSize` 才锁存为第二帧的 7 字节。设计目的是排除「复位时卡在一帧中间」的半帧污染。

**练习 3**：`AxiStreamMonAxiL` 里 `bandwidth` 的输出是 64 位，但 `AxiStreamMon` 内部 `bw` 只有 40 位。这两者如何衔接？为什么要垫零？

**参考答案**：见 [AxiStreamMon.vhd:326-328](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMon.vhd#L326-L328)（`bandwidth <= x"000000" & bw;`）。内部累加器 `accum` 是 40 位，足以容纳 1 秒内可能搬运的最大字节数；对外统一成 64 位是为了和外壳的 `Slv64Array` 及寄存器布局（高低两个 32 位字）对齐，便于软件按 64 位整数解析。高 24 位垫零即可。

---

### 4.2 调试抽头：AxiStreamTap

#### 4.2.1 概念说明

调试一条 AXI-Stream 流时，你常想「把其中某一类帧单独拎出来看看」，又不想打断主流。`AxiStreamTap` 就是干这个的：它按 **`tDest`（目的/虚拟通道）** 把流里指定目的地的帧**抽出来**送到一个独立的 tap 输出端口（`tmAxisMaster`），其余帧继续走主流；同时它还提供一个反向的 tap 输入端口（`tsAxisMaster`），允许你**把帧重新注入**回主流。

它和 `AxiStreamMon` 都是「旁路」，但侧重不同：

- `AxiStreamMon` 是**只读观测**：看帧数/带宽，不碰数据。
- `AxiStreamTap` 是**可读写抽头**：能把帧「抄走」也能「塞回来」，常用于把某些帧导到调试逻辑（如 CRC 校验、协议分析、低速dump），处理完再合回主流。

#### 4.2.2 核心流程

`AxiStreamTap` 的实现极其精炼——它**本身不写任何时序逻辑**，纯粹是把 u4-l3 的 `AxiStreamDeMux` 和 `AxiStreamMux` 组合起来：

```text
                 ┌───────────── DeMux (ROUTED) ─────────────┐
  sAxisMaster ─→ │ 0 号口匹配 TAP_DEST_G    → tmAxisMaster  │  抽出
                 │ 1 号口匹配 "--------"(任意) → iAxisMaster  │  旁路
                 └──────────────────────────────────────────┘
                 ┌───────────── Mux (PASSTHROUGH, 交错) ─────┐
  tsAxisMaster → │ 0 号口(tap 注入) ┐                        │
  iAxisMaster  → │ 1 号口(主流)     ┘→ mAxisMaster           │  合回
                 └──────────────────────────────────────────┘
```

两张「路由表」是关键，它们互为镜像：

```vhdl
constant ROUTES_C : Slv8Array := (0 => toSlv(TAP_DEST_G, 8),
                                 1 => "--------");
```

见 [AxiStreamTap.vhd:53-54](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamTap.vhd#L53-L54)。`"--------"` 是 8 位的「全不关心」通配（每位都是 `'-'`），DeMux 用 `std_match` 比较时会匹配任意 `tDest`。所以：

- DeMux 0 号口只收 `tDest = TAP_DEST_G` 的帧 → 抽到 tap 输出。
- DeMux 1 号口收**所有其它** `tDest` → 继续走主流。

> 为什么 0 号口写具体值、1 号口写全通配？因为 DeMux 是按端口顺序优先匹配的：先试 0 号（精确命中 tap 目的），不中再落到 1 号（兜底收剩下的）。这样保证「指定目的去 tap，其余全过」。

「重新注入」靠 Mux 完成：tap 输入（`tsAxisMaster`）与主流（`iAxisMaster`）在 Mux 里合并回 `mAxisMaster`。Mux 用了 `ILEAVE_EN_G => true`（交错模式），允许两路按帧交错合流，而不是锁定单路。

#### 4.2.3 源码精读

整个 `structure` 架构体只有两个实例化，没有任何进程——这是典型的「结构级（structural）」设计：

**DeMux 抽出**：见 [AxiStreamTap.vhd:61-78](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamTap.vhd#L61-L78)。`MODE_G => "ROUTED"` 表示按 `TDEST_ROUTES_G` 表路由，`NUM_MASTERS_G => 2`。

```vhdl
U_DeMux : entity surf.AxiStreamDeMux
   generic map (
      NUM_MASTERS_G  => 2,
      MODE_G         => "ROUTED",
      TDEST_ROUTES_G => ROUTES_C)
   port map (
      sAxisMaster     => sAxisMaster,
      mAxisMasters(0) => tmAxisMaster,   # 抽出
      mAxisMasters(1) => iAxisMaster,    # 旁路继续);
```

**Mux 合回**：见 [AxiStreamTap.vhd:80-100](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamTap.vhd#L80-L100)。`MODE_G => "PASSTHROUGH"`、`ILEAVE_EN_G => true`：

```vhdl
U_Mux : entity surf.AxiStreamMux
   generic map (
      NUM_SLAVES_G   => 2,
      MODE_G         => "PASSTHROUGH",
      ILEAVE_EN_G    => true,
      ILEAVE_ON_NOTVALID_G => ILEAVE_ON_NOTVALID_G,
      ILEAVE_REARB_G       => ILEAVE_REARB_G)
   port map (
      sAxisMasters(0) => tsAxisMaster,   # tap 注入
      sAxisMasters(1) => iAxisMaster,    # 主流
      mAxisMaster     => mAxisMaster);
```

`ILEAVE_ON_NOTVALID_G` 与 `ILEAVE_REARB_G` 两个泛型透传给 Mux，控制「在源无效时是否允许重仲裁」与「多少拍后强制重仲裁」——这是 u4-l3 的内容，这里只把它们当作「抽头对合流公平性的可调旋钮」。

#### 4.2.4 代码实践

**1) 实践目标**：验证 `AxiStreamTap` 能把 `tDest = TAP_DEST_G` 的帧抽到 tap 输出，其余帧走主流，并能从 tap 输入把帧重新注入主流。

**2) 操作步骤**：直接运行现成测试（它已经覆盖了抽出 + 重注入全链路）：

```bash
./.venv/bin/python -m pytest -q tests/axi/axi_stream/test_AxiStreamTap.py
```

该测试（[test_AxiStreamTap.py:63-89](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/axi_stream/test_AxiStreamTap.py#L63-L89)）发了三帧：一帧 `tdest=5`（被抽）、一帧 `tdest=1`（直通）、一帧从 tap 输入 `tdest=5`（重注入），然后分别从 tap sink 与 main sink 收回并断言：

```python
tapped   = AxiStreamFrame(b"\x01\x02\x03\x04"); tapped.tdest   = 5   # 命中 TAP_DEST
normal   = AxiStreamFrame(b"\x10\x11\x12\x13"); normal.tdest   = 1   # 不命中，直通
inserted = AxiStreamFrame(b"\xAA\xBB\xCC\xDD"); inserted.tdest = 5   # 从 tap 注入
await tb.source.send(tapped)
await tb.source.send(normal)
await tb.tap_source.send(inserted)
rx_tap    = await tb.tap_sink.recv()      # 应为 tapped
rx_main_0 = await tb.main_sink.recv()     # 应为 inserted（注入）
rx_main_1 = await tb.main_sink.recv()     # 应为 normal（直通）
assert rx_tap.tdata == tapped.tdata and rx_tap.tdest == 5
assert rx_main_0.tdata == inserted.tdata and rx_main_0.tdest == 5
assert rx_main_1.tdata == normal.tdata   and rx_main_1.tdest == 1
```

**3) 需要观察的现象**：

- tap sink 只收到 `tdest=5` 的那帧（抽出成功）。
- main sink 收到两帧：注入的 `inserted` 和直通的 `normal`，且 `tdest` 各自保持。
- 注意 `rx_main_0` 是**注入帧**而非原 `tapped`——因为原 `tapped` 被抽走了，不再出现在主流；主流上 `tdest=5` 的帧来自 tap 输入。

**4) 预期结果**：三帧分别落到期望的端口，`tdest` 标签全程不丢。**待本地验证**：Mux 交错模式下两路合并的先后顺序受 `ILEAVE_REARB_G` 影响，若你改了注入时机，`rx_main_0` 与 `rx_main_1` 的先后可能互换——但「tapped 去了 tap、inserted 与 normal 留在 main」这一结论不变。

#### 4.2.5 小练习与答案

**练习 1**：如果想让 tap 抽出**两个**目的（比如 `tDest=5` 和 `tDest=6`）的帧，能直接用现在的 `AxiStreamTap` 吗？需要怎么改？

**参考答案**：不能直接用。当前 `ROUTES_C` 只有 2 个表项：0 号口精确匹配单个 `TAP_DEST_G`，1 号口全通配。要抽两个目的，可把 `NUM_MASTERS_G`（DeMux）扩到 3，路由表改成 `0 => toSlv(5,8)、1 => toSlv(6,8)、2 => "--------"`，并相应改 Mux 把这两个 tap 口合并；或者更简单地，串两级 `AxiStreamTap`（分别配 `TAP_DEST_G=5` 和 `=6`）。

**练习 2**：`AxiStreamTap` 的 RTL 里**没有一个进程**，这对它的「非侵入性」意味着什么？

**参考答案**：它纯由 `AxiStreamDeMux` + `AxiStreamMux` 两个已有模块搭成（[L61-100](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamTap.vhd#L61-L100)），自身不引入任何新逻辑或寄存器（除模块内部固有的握手/流水）。这意味着它的行为完全等价于「把流拆开再合上」，对未命中 `tDest` 的帧而言，它们只是穿过了 DeMux 的旁路口再进 Mux——功能上等价于直连，时序上仅多了这两个模块固有的 `PIPE_STAGES_G`。非侵入性正是来自这种「纯结构、无自定义状态机」的设计。

---

### 4.3 PRBS 流控测试码型：AxiStreamPrbsFlowCtrl

#### 4.3.1 概念说明

先澄清一个容易混淆的点：本模块**不是「PRBS 数据发生器」**。它不产生伪随机数据放进 `tData`（那是 `SsiPrbsTx` 的活，属于 u5）。它的名字里 `FlowCtrl` 才是重点——它是一个**伪随机反压（back pressure）发生器**：用 LFSR 产生一个伪随机数，按可调的 `threshold` 随机地「拒绝」搬运数据（拉低 `tReady`），用来**给上下游握手施加压力**，验证你的 FIFO、反压、帧边界处理在被随机「卡」的情况下仍然正确。

文件头一行说得很直白：`Description: Generates pseudo-random back pressure`，见 [AxiStreamPrbsFlowCtrl.vhd:4](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPrbsFlowCtrl.vhd#L4)。

典型用法：在回归测试里把它插在「源」和「被测模块」之间，让数据以随机间隔送达，比「一直满速灌数据」更能暴露反压边界 bug。

#### 4.3.2 核心流程

```text
每拍 (clk)：
  randomData = LFSR(r.randomData, taps=PRBS_TAPS_G)   # 推进一位伪随机数
  pause      = (randomData < threshold)                # DSP 比较器：小于阈值则暂停
  if (txMaster 空) and (上游有数据 rxMaster.tValid) and (not pause):
       rxSlave.tReady := '1'     # 接受这一拍
       txMaster       := rxMaster  # 原样转发（不改数据）
  else:
       rxSlave.tReady := '0'     # 随机反压：不接受
```

关键关系：`pause` 当且仅当 `randomData < threshold`。由于 LFSR 序列在长周期内近似均匀分布，平均暂停概率为

\[
p_{\text{pause}} \;\approx\; \frac{\text{threshold}}{2^{32}}, \qquad
\text{平均通流率} \;\approx\; 1 - p_{\text{pause}}.
\]

两个极端正好对应「完全放行」与「几乎完全阻塞」：

- `threshold = 0`：`randomData < 0` 恒不成立（无符号比较）→ `pause` 恒 0 → 永不反压，全速直通。
- `threshold = 0xFFFFFFFF`：`randomData < 0xFFFFFFFF` 几乎恒成立（只有 LFSR 恰好等于 `0xFFFFFFFF` 那一拍例外）→ 几乎完全阻塞。

中间值则线性控制平均通流率。`SEED_G` 决定 LFSR 初值（避免复位后总是同一相位），`PRBS_TAPS_G` 决定多项式（默认 `(31,6,2,1)` 是一个已知的 32 位最长周期 LFSR）。

#### 4.3.3 源码精读

**泛型与端口**：`SEED_G`（LFSR 种子）、`PRBS_TAPS_G`（抽头多项式）、`threshold`（反压阈值，默认 `0x8000_0000`，即约 50% 通流），见 [AxiStreamPrbsFlowCtrl.vhd:24-42](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPrbsFlowCtrl.vhd#L24-L42)。

**LFSR 推进**：用 u2-l5 提到的 `StdRtlPkg.lfsrShift`，每拍右移一位并按抽头异或回填最低位：

```vhdl
-- Generate new random data
v.randomData := lfsrShift(r.randomData, PRBS_TAPS_G, '0');
```

见 [AxiStreamPrbsFlowCtrl.vhd:95-96](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPrbsFlowCtrl.vhd#L95-L96)。`lfsrShift` 的定义在 [StdRtlPkg.vhd:1105](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd#L1105)（移位 + 按抽头异或）。

**阈值比较**：用 `DspComparator`（Xilinx 上映射到 DSP48 的比较器，资源友好）输出 `ls = (a < b)`：

```vhdl
U_DspComparator : entity surf.DspComparator
   generic map ( WIDTH_G => 32 )
   port map (
      clk => clk,
      ain => r.randomData,
      bin => threshold,
      ls  => pause);                 --  (a <  b)
```

见 [AxiStreamPrbsFlowCtrl.vhd:70-80](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPrbsFlowCtrl.vhd#L70-L80)。

**反压决策**：`comb` 进程里，只有「输出空 + 上游有效 + 不暂停」三条件同时成立才搬数据，否则 `rxSlave.tReady` 保持 0（反压）：

```vhdl
v.rxSlave := AXI_STREAM_SLAVE_INIT_C;        -- 默认 tReady=0（反压基线）
if (txSlave.tReady = '1') then
   v.txMaster.tValid := '0';                  -- 下游收走，清输出有效
end if;
...
if (v.txMaster.tValid = '0') and (rxMaster.tValid = '1') and (pause = '0') then
   v.rxSlave.tReady := '1';                   -- 接受
   v.txMaster       := rxMaster;              -- 原样搬运
end if;
```

见 [AxiStreamPrbsFlowCtrl.vhd:89-104](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPrbsFlowCtrl.vhd#L89-L104)。注意它**原样搬运** `rxMaster`，不改 `tData`/`tKeep`/`tLast` 等任何字段——再次强调它是「流控」而非「数据」模块。

**末级流水**：输出经一个 `AxiStreamPipeline`（`PIPE_STAGES_G` 默认 0，直通），用来在需要时切组合路径，见 [AxiStreamPrbsFlowCtrl.vhd:131-143](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPrbsFlowCtrl.vhd#L131-L143)。

#### 4.3.4 代码实践

**1) 实践目标**：验证 `threshold = 0` 时帧原样通过、`threshold = 0xFFFFFFFF` 时流被完全阻塞（随机反压的两个极端）。

**2) 操作步骤**：运行现成测试：

```bash
./.venv/bin/python -m pytest -q tests/axi/axi_stream/test_AxiStreamPrbsFlowCtrl.py
```

该测试（[test_AxiStreamPrbsFlowCtrl.py:59-74](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/axi_stream/test_AxiStreamPrbsFlowCtrl.py#L59-L74)）分两段：先 `threshold=0` 发一帧验证原样收到，再把 `threshold` 置全 1、发第二帧并等 8 拍，断言 sink 仍为空（被阻塞）：

```python
# threshold = 0：放行
frame = AxiStreamFrame(b"\x10\x11\x12\x13")
await tb.source.send(frame)
rx_frame = await tb.sink.recv()
assert rx_frame.tdata == frame.tdata        # 原样通过

# threshold = 全 1：阻塞
tb.dut.threshold.value = 0xFFFFFFFF
send_task = cocotb.start_soon(tb.source.send(AxiStreamFrame(b"\x20\x21\x22\x23")))
await tb.cycle(8)
assert tb.sink.empty()                      # 这 8 拍内一拍都没通过
send_task.kill()
```

**3) 需要观察的现象**：

- `threshold=0`：帧数据逐字节相等，证明模块不改数据。
- `threshold=0xFFFFFFFF`：8 拍内 sink 为空，证明反压生效。

**4) 预期结果**：两段断言都通过。**待本地验证**：若把 `threshold` 设成中间值（如默认 `0x8000_0000`），你应能观察到帧「断断续续」通过——平均约一半的拍被随机放行，这正是伪随机反压的典型表现；具体每拍是否通过取决于 LFSR 相位，不可预测，故测试只检查两个确定性的极端。

**5) 进阶**：试着把 `AxiStreamPrbsFlowCtrl`（随机反压源）插到一个 `AxiStreamFifoV2`（u4-l2）前面，灌大量帧，观察 FIFO 的 `overflow`/`pause` 信号是否如预期随反压波动——这是真实链路压力测试的典型搭法。

#### 4.3.5 小练习与答案

**练习 1**：把 `threshold` 从 `0` 慢慢调大到 `0xFFFFFFFF`，平均通流率如何变化？为什么？

**参考答案**：平均通流率从 100% 线性下降到接近 0。因为 `pause = (randomData < threshold)`，而 LFSR 序列在长周期内近似均匀分布于 `[0, 2^32)`，所以 `pause` 的概率约为 `threshold / 2^32`，通流率约为 `1 - threshold/2^32`。`threshold=0` 时概率 0（全通），`threshold=0xFFFFFFFF` 时概率几乎 1（几乎全堵）。

**练习 2**：为什么说 `AxiStreamPrbsFlowCtrl` 是「流控」模块而不是「数据」模块？给出源码证据。

**参考答案**：它在搬运时执行的是 `v.txMaster := rxMaster`（[L103](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPrbsFlowCtrl.vhd#L103)），把上游记录**整体原样**赋给下游，没有改写 `tData`/`tKeep`/`tLast` 任何字段；它真正控制的只是 `rxSlave.tReady`（[L101](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPrbsFlowCtrl.vhd#L101)）——即「这一拍接不接受数据」。LFSR 与比较器只驱动 `pause`，进而只影响 `tReady`。因此它施加的是**反压（流控）**，而非注入数据。

**练习 3**：默认 `PRBS_TAPS_G = (0=>31, 1=>6, 2=>2, 3=>1)`、`SEED_G = 0xAAAA_5555`。如果两个不同的 `AxiStreamPrbsFlowCtrl` 实例用**相同**的种子和抽头，它们的反压波形会一样吗？这对测试是好事还是坏事？

**参考答案**：会几乎一样（只要复位时机也一致），因为 LFSR 是确定性的——同种子同抽头产生同一序列。对测试而言这是**好事**：反压波形可复现，失败用例能稳定重现、便于定位；但若你想用多实例模拟「相互独立的随机源」，就必须给它们**不同**的 `SEED_G`，否则它们的反压会同步，失去统计独立性。

---

## 5. 综合实践

把本讲三个模块串成一个最小「带监控与压力测试的数据通路」：

```text
 SsiPrbsTx/Source ──→ AxiStreamPrbsFlowCtrl ──→ AxiStreamTap ──→ Sink
                       (随机反压)                  │        │
                                                  │   (tap 口可挂调试)
                       AxiStreamMonAxiL ◀──监听──┘
                       (统计帧数/带宽)
```

任务：

1. 用 `AxiStreamPrbsFlowCtrl`（`threshold` 取中间值 `0x8000_0000`）作为随机反压源，向下游灌若干帧。
2. 在反压源与下游之间并行挂一个 `AxiStreamMonAxiL`（监听同一段握手），再串一个 `AxiStreamTap`（把某个 `tDest` 的帧抽到 tap sink 做校验）。
3. 跑足够长时间（或调小 `AXIS_CLK_FREQ_G` 使 1 秒窗口可仿真），读 `AxiStreamMonAxiL` 的 `frameCnt`，与你在 tap sink + main sink 收到的**总帧数**比对，二者应相等。
4. 观察反压开启前后 `frameCnt` 增长速率的变化——反压越强，帧率越低，但 `frameCnt` 最终仍应等于发送总帧数（不丢帧）。

这个综合实践把「施压（PrbsFlowCtrl）— 监听— 分流」三者串起来，验证在随机反压下数据不丢、统计准确、抽头不破坏帧序。**待本地验证**：完整链路需要你自行组装一个 TB（可参考三个现成测试的 `TB` 类与 `run_surf_vhdl_test` 调用），`bandwidth` 读数仍受 1 秒窗口限制。

## 6. 本讲小结

- **`AxiStreamMon`** 是纯旁路统计核：只听 `tValid`/`tReady`/`tLast`，数帧、数字节、测帧率/带宽，**不驱动流的 `tReady`**，插进设计不改变流时序。
- **「1 秒窗口」巧思**：`TIMEOUT_C = getTimeRatio(AXIS_CLK_FREQ_G, 1.0) - 1` 使计数窗口恰为 1 实秒，于是字节数直接等于 Byte/s、帧数直接等于 Hz，无需除法；副作用是短仿真里 `bandwidth`/`frameRate` 不会刷新。
- **武装机制**：复位后第一帧用于建立基准，其大小不上报；`frameCnt` 则从第一帧起就计数。
- **`AxiStreamMonAxiL`** 用「滚动写入器 + `AxiDualPortRam`」做成 RAM 影子寄存器，每通道固定 `0x40` 字节布局；向 `0x0` 写可软件清零统计。
- **`AxiStreamTap`** 是纯结构模块（DeMux + Mux，零进程），按 `tDest` 抽出指定帧并支持重新注入，对未命中帧等价直通，是非侵入调试点。
- **`AxiStreamPrbsFlowCtrl`** 是**伪随机反压**（不是 PRBS 数据）发生器：LFSR + 阈值比较随机拉 `tReady`，`threshold` 线性控制平均通流率，用于给握手施压做链路自检。

## 7. 下一步学习建议

- 本讲的「流」还只是裸 AXI-Stream。下一步进入 **u5-l1（SSI 流式协议）**：SSI 在 `tUser` 里编码 SOF/EOF/EOFE 帧边界、用 `tDest` 当虚拟通道，届时你会发现 `AxiStreamMon` 的 `frameSent` 判定（`tValid and tLast and tReady`）正好与 SSI 的 EOF 语义对应。
- 想要**真正的 PRBS 数据**收发与校验，去看 **u5-l2（SSI 测试码型与帧过滤）** 的 `SsiPrbsTx`/`SsiPrbsRx`，与本讲的 `AxiStreamPrbsFlowCtrl`（反压）对照，区分「数据侧 PRBS」与「流控侧 PRBS」。
- 想把监控/抽头用于真实工程，建议接着读 **u9-l4（PyRogue 设备模型）**：`AxiStreamMonAxiL` 的寄存器布局在 `python/surf` 里有对应的 PyRogue 镜像，软件侧读到的 `frameCnt`/`bandwidth` 就是本讲这些寄存器。
- 若要做大规模回归，**u9-l1/u9-l2（cocotb 工具链与编写测试）** 会讲清本讲三个 `run_surf_vhdl_test` 调用、`start_lockstep_clocks`、`axil_read_u32` 背后的机制。
