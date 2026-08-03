# 以太网 FCS 计算、校验与插入

## 1. 本讲目标

本讲讲解 verilog-ethernet 中负责以太网**帧校验序列（FCS, Frame Check Sequence）**的一族模块。学完后你应当能够：

- 说清 FCS 与 CRC-32 的关系，以及「逐字节累加—帧尾取反」的标准流程。
- 读懂 `axis_eth_fcs`（计算 FCS）、`axis_eth_fcs_check`（校验并标记坏帧）、`axis_eth_fcs_insert`（在帧尾追加 FCS）三个模块的源码。
- 理解这三个模块如何挂在 AXI-Stream 总线上、如何与上一讲的 `lfsr` 引擎配合。
- 区分 8 位通路与 `_64` 宽位宽变体在 FCS 处理上的实现差异。
- 自己写一个小 testbench，验证「计算得到的 FCS」与「insert 模块实际追加的 4 字节」一致。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**FCS 是什么。** 以太网帧在发送时，会在末尾追加 4 字节的 FCS，它是对「目的 MAC + 源 MAC + 类型 + 载荷」全部内容算出的一个校验值。接收端用同样方法重算一遍，若与收到的 FCS 不一致，就判定这帧在传输中被损坏，直接丢弃。FCS 是以太网链路层完整性保障的最后一道关。

**FCS 就是 CRC-32。** FCS 使用的是 CRC-32（循环冗余校验，32 位），具体参数是本库统一采用的「以太网标准」组合：

| 参数 | 取值 | 含义 |
| --- | --- | --- |
| 多项式 | `0x04C11DB7` | 生成多项式（代码里写 `0x4c11db7`，省略最高次项） |
| 初值 | `0xFFFFFFFF` | CRC 寄存器复位值 |
| 反射（reflected） | 是（`REVERSE=1`） | 字节内位序颠倒处理 |
| 终值 | 取反 | 全部字节处理完后对寄存器取反才得到 FCS |

**逐字节累加 + 帧尾取反。** CRC 本质是把数据看作一个大多项式除以生成多项式取余数。本库的 `lfsr` 模块（上一讲 `u2-l1` 已精读）把这个「除法余数」实现成一个状态机：每喂入一个字节，就把内部 32 位状态推进一次；状态机本身不存数据，状态由调用方寄存后回送。所以一帧的处理过程是：

```
crc_state = 0xFFFFFFFF          # 帧开始前复位
for byte in frame:
    crc_state = CRC_next(crc_state, byte)   # 逐字节推进
fcs = ~crc_state                 # 帧尾取反，得到最终 FCS
```

对标准测试串 `"123456789"`，按上式算出的 CRC-32 是 `0xcbf43926`——这是公认的「校验值」，本讲的模块都应得到这个结果。

> 承接：本讲三个模块全部直接实例化 `rtl/lfsr.v`，参数固定为 `LFSR_WIDTH=32, LFSR_POLY=0x4c11db7, GALOIS, REVERSE=1`。如果你还不清楚 `lfsr` 的「并行展开下一状态」机制，请先复习 `u2-l1`。

## 3. 本讲源码地图

| 文件 | 作用 | 是否本讲精读 |
| --- | --- | --- |
| `rtl/axis_eth_fcs.v` | **计算**：吃入 AXI-Stream 帧，帧尾输出 32 位 FCS | 是 |
| `rtl/axis_eth_fcs_check.v` | **校验**：吃入带 FCS 的帧，判定好坏帧并打 `tuser` 标志（8 位） | 是 |
| `rtl/axis_eth_fcs_insert.v` | **插入**：吃入无 FCS 的帧，帧尾追加 4 字节 FCS（8 位，可选填充） | 是 |
| `rtl/axis_eth_fcs_check_64.v` | `check` 的 64 位宽位宽变体 | 概念对比 |
| `rtl/axis_eth_fcs_insert_64.v` | `insert` 的 64 位宽位宽变体 | 概念对比 |
| `rtl/lfsr.v` | 底层 CRC/LFSR 引擎（上一讲） | 依赖 |
| `tb/test_axis_eth_fcs.py` | 历史遗留 myhdl 测试，演示如何用 `eth_ep.EthFrame` 算期望 FCS | 实践参考 |

注意：`axis_eth_fcs`（纯计算器）**只有 8 位及以上通用位宽版本**，没有专门的 `_64`；而 `check` 和 `insert` 都有独立的 `_64` 变体。

## 4. 核心概念与源码讲解

### 4.1 FCS 计算：axis_eth_fcs

#### 4.1.1 概念说明

`axis_eth_fcs` 是最纯粹的计算器：它像一条「旁路」挂在 AXI-Stream 数据流上，**不修改、不阻塞**数据，只是边看数据边累加 CRC，并在帧尾（`tlast` 拍）把算好的 FCS 通过单独的 `output_fcs` 端口吐出来。它的设计目标是让上层模块（如 MAC）在不改变数据通路的前提下「顺便」拿到发送侧的 FCS。

#### 4.1.2 核心流程

```
复位：crc_state = 0xFFFFFFFF
每个 s_axis_tvalid 拍：
    crc_state ← lfsr(crc_state, tdata 的有效字节)
    若 s_axis_tlast：
        output_fcs       ← ~crc_next     # 取反得到 FCS
        output_fcs_valid ← 1             # 帧尾脉冲
        crc_state ← 0xFFFFFFFF           # 为下一帧复位
```

关键点有三个：

1. **`s_axis_tready` 恒为 1**——它是纯组合旁路，永不反压上游，所以可以随意插入数据通路而不影响时序。
2. **帧尾才输出**——`output_fcs_valid` 只在 `tlast` 那拍拉高一拍。
3. **取反**——`~crc_next` 对应标准 CRC-32 的「终值取反」。

#### 4.1.3 源码精读

模块参数与端口：[rtl/axis_eth_fcs.v:34-63](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs.v#L34-L63)。注意它用 `DATA_WIDTH`/`KEEP_WIDTH` 参数化，输入是标准 AXI-Stream，输出是单独的 `output_fcs[31:0]` 与 `output_fcs_valid`。

状态寄存器与「永不反压」：[rtl/axis_eth_fcs.v:73-81](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs.v#L73-L81)。`crc_state` 初值 `0xFFFFFFFF`，`s_axis_tready = 1`。

核心的 CRC 引擎实例化（generate 循环）：[rtl/axis_eth_fcs.v:83-107](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs.v#L83-L107)。对每个 `KEEP_WIDTH` 槽位实例化一个 `lfsr`，`DATA_WIDTH` 取 `DATA_WIDTH/KEEP_WIDTH*(n+1)`，即「一次处理 n+1 个字节」的并行展开；`state_in` 接当前 `crc_state`，`state_out` 给出 `crc_next[n]`。

帧尾处理（取反输出 + 复位）：[rtl/axis_eth_fcs.v:111-130](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs.v#L111-L130)。其中 `fcs_reg <= ~crc_next[...]`（L120-128）就是「终值取反」；当 `KEEP_ENABLE` 时用 `tkeep` 循环选出真正有效字节对应的 `crc_next[i]`，确保非完整末字也能得到正确 FCS。

#### 4.1.4 代码实践

**目标**：用历史遗留测试 `tb/test_axis_eth_fcs.py` 验证 8 位计算器对变长帧的输出正确。

**操作步骤**：

1. 阅读 [tb/test_axis_eth_fcs.py:124-147](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_axis_eth_fcs.py#L124-L147)：它构造 `eth_ep.EthFrame`，调用 `update_fcs()` 得到期望 FCS，发送后 `await output_fcs_valid.posedge` 并断言 `output_fcs == test_frame.eth_fcs`。
2. （可选）参照 `u1-l4`，把该测试改写为 cocotb 版本：用 `cocotbext-axi` 的 `AXIStreamSource` 发送 `EthFrame.build_axis()` 的字节流，用 `await RisingEdge(dut.output_fcs_valid)` 等待，再 `assert dut.output_fcs.value == expected`。

**需要观察的现象**：对长度 1~17、64~81 的帧，`output_fcs` 都应等于 `eth_ep` 算出的 `eth_fcs`。

**预期结果**：全部断言通过。

> 待本地验证：受限于环境，本讲未实际运行该测试；请在配置好 cocotb/iverilog 后自行确认输出。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `axis_eth_fcs` 把 `s_axis_tready` 恒置 1，而 `axis_eth_fcs_check` 却不能这么做？
**答案**：`axis_eth_fcs` 是纯计算旁路，无需暂存或回看数据，组合逻辑一拍即可算出 CRC，自然不会成为瓶颈；而 `check` 需要把数据延后 4 拍以取出末尾 4 字节 FCS 做比较，必须配合握手/寄存器，因此需要真正的反压控制。

**练习 2**：把输入帧换成 `"123456789"` 的 ASCII 字节，`output_fcs` 应是多少？
**答案**：`0xcbf43926`（标准 CRC-32 校验值）。

---

### 4.2 FCS 校验判定：axis_eth_fcs_check

#### 4.2.1 概念说明

`axis_eth_fcs_check` 解决的是**接收侧**的问题：收到一帧（末尾已带 4 字节 FCS），要判断它是否完好。它的做法是边收边对「除末 4 字节外的内容」累加 CRC，到帧尾时把算出的期望 FCS 与帧里实际携带的 4 字节比对：一致→好帧，否则→坏帧。坏帧通过把输出 `tuser` 拉高来通知下游，同时给出一个 `error_bad_fcs` 脉冲。

#### 4.2.2 核心流程

难点在于「帧到达 `tlast` 时，最后 4 字节（即 FCS）不能算进 CRC」。本模块用**4 级延时流水线**巧妙解决：让 CRC 计算始终比输入数据滞后 4 拍。这样当 `tlast` 到来时，CRC 寄存器恰好只覆盖了「FCS 之前」的全部字节。

```
每拍：
    把当前字节推入 d0→d1→d2→d3 的 4 级移位寄存器
    crc_state ← lfsr(crc_state, d3 字节)     # 始终处理 4 拍前的字节

当 tlast 到达（当前字节是最后一字节 = FCS 的最后一字节）：
    期望 FCS = ~crc_next                       # 此时 crc_next 已覆盖到 d3
    实际 FCS  = {当前字节, d0, d1, d2}          # 最近 4 字节 = 收到的 FCS
    if 实际 FCS != 期望 FCS:
        m_axis_tuser = 1；error_bad_fcs 脉冲
```

为何比较的是 `{s_axis_tdata, s_axis_tdata_d0, s_axis_tdata_d1, s_axis_tdata_d2}`？因为这正好是「当前字节 + 前 3 拍字节」共 4 字节，也就是帧里携带的 FCS 字段（小端拼接，与 FCS 的传输顺序一致）。

#### 4.2.3 源码精读

4 级延时寄存器与状态定义：[rtl/axis_eth_fcs_check.v:64-91](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs_check.v#L64-L91)。注意 `crc_state` 初值 `0xFFFFFFFF`，且有 `d0~d3` 四级数据/valid 寄存器。

CRC 引擎处理的是**延时后**的 `s_axis_tdata_d3`：[rtl/axis_eth_fcs_check.v:107-121](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs_check.v#L107-L121)。这是「滞后 4 拍」的关键。

帧尾的 FCS 比对与坏帧判定（PAYLOAD 状态）：[rtl/axis_eth_fcs_check.v:195-198](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs_check.v#L195-L198)：

```verilog
if ({s_axis_tdata, s_axis_tdata_d0, s_axis_tdata_d1, s_axis_tdata_d2} != ~crc_next) begin
    m_axis_tuser_int = 1'b1;
    error_bad_fcs_next = 1'b1;
end
```

CRC 状态的复位/更新与移位流水线：[rtl/axis_eth_fcs_check.v:235-259](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs_check.v#L235-L259)。`reset_crc` 时回到 `0xFFFFFFFF`，否则 `crc_state <= crc_next`；`shift_in` 时把数据逐级下推。

此外模块还有一段标准的「输出双寄存器 + temp 缓冲」反压逻辑（[rtl/axis_eth_fcs_check.v:289-341](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs_check.v#L289-L341)），保证即使下游反压，帧也不会在模块内部被拆散。这是 verilog-ethernet 中常见的 axis 输出级模板，后续讲义会反复见到。

#### 4.2.4 代码实践

**目标**：用源码阅读理解「滞后 4 拍」如何让帧尾比对恰好排除 FCS。

**操作步骤**：

1. 打开 [rtl/axis_eth_fcs_check.v:76-84](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs_check.v#L76-L84) 与 [rtl/axis_eth_fcs_check.v:241-259](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs_check.v#L241-L259)。
2. 假设一帧共 10 字节（6 字节数据 + 4 字节 FCS），逐拍画出 `s_axis_tdata`、`d0/d1/d2/d3`、`crc_state` 之间的关系。
3. 在 `tlast` 拍确认：`crc_next` 恰好覆盖前 6 字节数据，而 `{data,d0,d1,d2}` 恰好是 4 字节 FCS。

**需要观察的现象**：FCS 的 4 字节在比对发生时正好停在 `d2,d1,d0,当前` 这 4 个位置上，未进入 CRC 累加。

**预期结果**：手工推导应能得出「数据部分参与 CRC、FCS 部分参与比对」的结论。

#### 4.2.5 小练习与答案

**练习 1**：如果把延时流水线从 4 级改成 3 级，会发生什么？
**答案**：`tlast` 时 CRC 会多覆盖 1 个 FCS 字节、少覆盖 1 个数据字节，比对双方错位，所有帧都会被判为坏帧。4 级恰好对应 FCS 的 4 字节长度。

**练习 2**：`error_bad_fcs` 是电平还是脉冲？好帧时它的值是什么？
**答案**：它是逐帧的脉冲——仅在坏帧的 `tlast` 拍拉高一拍；好帧时保持 0（见 [rtl/axis_eth_fcs_check.v:232](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs_check.v#L232) 的 `error_bad_fcs_reg <= error_bad_fcs_next`，默认下一值为 0）。

---

### 4.3 FCS 帧尾插入：axis_eth_fcs_insert

#### 4.3.1 概念说明

`axis_eth_fcs_insert` 用于**发送侧**：收到一帧（不带 FCS，可能还太短），在帧尾追加 4 字节 FCS，必要时先用 0 字节填充到以太网最小帧长。相比 `axis_eth_fcs`（只算不插），它是真正的「会改变数据流长度」的模块——输出帧比输入帧多了 4（或 4+填充）字节。

#### 4.3.2 核心流程

模块用 4 状态有限状态机管理整个过程：

```
IDLE    等待首字节；CRC 复位
PAYLOAD 透传数据，同时 CRC 累加（CRC 算的是「正在输出的字节」）
        tlast 到达：
          若 tuser（上游已标坏帧）→ 直接转发坏帧标志，不插 FCS
          否则 →
            若 ENABLE_PADDING 且帧太短 → STATE_PAD
            否则 → STATE_FCS
PAD     输出 0 字节补齐，这些 0 也参与 CRC
        补到 frame_ptr == MIN_FRAME_LENGTH-5 → STATE_FCS
FCS     依次输出 ~crc_state 的 4 个字节（小端，LSB 先）
        最后一字节拉 m_axis_tlast，复位 CRC → IDLE
```

两处关键设计：

- **CRC 算的是输出字节**：`lfsr` 的 `data_in` 接 `m_axis_tdata_int`（正在输出的字节），所以填充的 0 也被算进 FCS，符合「FCS 覆盖实际发出的全部字节」。
- **填充阈值 `MIN_FRAME_LENGTH-5`**：默认 `MIN_FRAME_LENGTH=64`。模块用 `frame_ptr` 统计已发送字节数，不足该阈值时补 0，再追加 4 字节 FCS，使短帧达到以太网最小帧长要求。

#### 4.3.3 源码精读

参数（填充开关与最小帧长）与 4 状态定义：[rtl/axis_eth_fcs_insert.v:34-71](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs_insert.v#L34-L71)。

CRC 引擎的 `data_in` 接的是**输出**数据 `m_axis_tdata_int`：[rtl/axis_eth_fcs_insert.v:100-114](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs_insert.v#L100-L114)。

填充逻辑（PAD 状态，补 0 至阈值）：[rtl/axis_eth_fcs_insert.v:205-226](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs_insert.v#L205-L226)，判定条件 `frame_ptr_reg < MIN_FRAME_LENGTH-5` 见 [rtl/axis_eth_fcs_insert.v:217](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs_insert.v#L217)。

FCS 输出（FCS 状态，按小端输出 `~crc_state` 四字节）：[rtl/axis_eth_fcs_insert.v:227-256](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs_insert.v#L227-L256)。核心是这段 case：

```verilog
case (frame_ptr_reg)
    2'd0: m_axis_tdata_int = ~crc_state[7:0];     // LSB 先发
    2'd1: m_axis_tdata_int = ~crc_state[15:8];
    2'd2: m_axis_tdata_int = ~crc_state[23:16];
    2'd3: m_axis_tdata_int = ~crc_state[31:24];   // MSB 最后
endcase
```

最后一字节拉 `tlast` 并复位：[rtl/axis_eth_fcs_insert.v:244-251](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs_insert.v#L244-L251)。注意它与 `axis_eth_fcs` 的一致性：两者最终都输出 `~CRC`，只是 insert 把它拆成 4 字节逐拍发送。

#### 4.3.4 代码实践

**目标**：把 `axis_eth_fcs`（计算器）与 `axis_eth_fcs_insert`（插入器）背靠背比较，验证二者对同一帧得到的 FCS 完全一致。本实践为「源码阅读 + 自建 testbench」型。

**操作步骤**（示例代码，需自建 cocotb 工程）：

1. 写一个 Verilog wrapper，把同一输入帧同时喂给两个模块：

```verilog
// 示例代码：双 DUT 比较_wrapper（非项目原有文件）
// input_feed ──► axis_eth_fcs        ──► output_fcs[31:0]
//            └─► axis_eth_fcs_insert ──► m_axis_* （末 4 字节）
```

2. 用 cocotb 发送一帧已知数据（如 `b"\x01\x02\x03\x04\x05\x06"`），分别采集：
   - `axis_eth_fcs` 的 `output_fcs`（32 位整数）；
   - `axis_eth_fcs_insert` 输出帧的末 4 字节，按**小端**拼成 32 位整数。
3. 断言两者相等。

**需要观察的现象**：`insert` 输出帧的末 4 字节按小端解读后，等于 `axis_eth_fcs` 给出的 `output_fcs`。

**预期结果**：断言通过，证明「计算器算出的 FCS」与「插入器实际追加的字节」是同一个值。

> 待本地验证：请在本搭建好 cocotb + iverilog 环境后运行确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `axis_eth_fcs_insert` 的 `lfsr` 用 `m_axis_tdata_int`（输出）作 `data_in`，而 `axis_eth_fcs_check` 用 `s_axis_tdata_d3`（延时输入）？
**答案**：insert 在发送侧，CRC 必须覆盖「实际发出去的字节」（含填充 0），所以算输出；check 在接收侧，需要刻意滞后 4 拍以排除末尾 FCS 字节，所以算延时后的输入。

**练习 2**：若 `s_axis_tuser=1`（上游标记坏帧）的帧到达 insert，会发生什么？
**答案**：insert 不为它计算/追加 FCS，直接把 `tlast`/`tuser` 透传出去并复位，见 [rtl/axis_eth_fcs_insert.v:183-188](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs_insert.v#L183-L188)。坏帧不应被「修复」成带 FCS 的好帧。

---

### 4.4 宽位宽 _64 变体的差异

#### 4.4.1 概念说明

在 10G/25G 通路下，数据位宽是 64 位（每拍 8 字节，配 8 位 `tkeep`）。`check` 和 `insert` 各有 `_64` 变体。宽位宽带来的核心难点是：**一帧可能在任意字节位置结束**（由 `tkeep` 标记），且末 4 字节 FCS 可能跨越两个 64 位字。两个变体采用了不同策略来应对。

#### 4.4.2 核心流程

`axis_eth_fcs_check_64` 采用「**魔数残留（magic residue）**」法，而非 8 位版的「直接比对 FCS」法：把整帧（含 FCS）全部喂进 CRC，正确帧最后应得到一个固定残留值。

```
每拍：crc_state ← lfsr(crc_state, 当前 64 位字的全部 8 字节)   # crc_next7
帧尾：根据 tkeep 选出有效字节数 (1/2/3/4)，用对应 crc_nextX 判断
      正确帧 → crc_nextX == 固定残留 ~0x2144df1c
```

而 `axis_eth_fcs_insert_64`（思路与 8 位版类似）则用 `tkeep` 控制末字对齐，并在帧尾追加 FCS。两者都新增了 `m_axis_tkeep` 输出以反映非完整字。

#### 4.4.3 源码精读

`check_64` 实例化了 5 个 `lfsr`（分别处理 8/16/24/32/64 位），并用固定残留判定好坏帧：[rtl/axis_eth_fcs_check_64.v:95-104](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs_check_64.v#L95-L104)。

```verilog
wire crc_valid0 = crc_next0 == ~32'h2144df1c;
wire crc_valid1 = crc_next1 == ~32'h2144df1c;
// ... 取反后 = 0xDEBB20E3，即该实现下正确帧的固定残留值
```

为处理「FCS 跨两个字」的情况，`check_64` 多了一个 `STATE_LAST` 状态：[rtl/axis_eth_fcs_check_64.v:314-342](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs_check_64.v#L314-L342)。当末字有效字节落在高 4 位（`tkeep[7:4]!=0`）时，先用 `STATE_LAST` 把残留的高半字缓存下来，下一拍再完成比对。

`insert_64`（`rtl/axis_eth_fcs_insert_64.v`）则相对直接：同样用状态机在帧尾插入 FCS，但需按 `tkeep` 对齐末字、并在 FCS 字节后正确生成新的 `tkeep`。建议对照 8 位版阅读。

#### 4.4.4 代码实践

**目标**：对比 8 位与 64 位 check 模块的「判定策略」差异。

**操作步骤**：

1. 阅读 [rtl/axis_eth_fcs_check.v:162-165](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs_check.v#L162-L165)（8 位：直接比对收到的 4 字节 vs `~crc_next`）。
2. 阅读 [rtl/axis_eth_fcs_check_64.v:246-254](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_eth_fcs_check_64.v#L246-L254)（64 位：比对 `crc_nextX` 是否等于固定残留 `~0x2144df1c`）。
3. 用一句话写下两者策略的区别。

**需要观察的现象**：8 位版「算出期望 FCS 再与帧尾比对」；64 位版「把整帧含 FCS 算到底，看残留是否为魔数」。

**预期结果**：理解两种策略数学上等价，但 64 位版更适合处理「FCS 位置由 tkeep 决定、可能跨字」的场景。

#### 4.4.5 小练习与答案

**练习 1**：为什么 64 位 check 改用「魔数残留」而非「直接比对末 4 字节」？
**答案**：64 位通路一拍含 8 字节，帧末 FCS 的起始位置随 `tkeep` 变化且可能横跨两个 64 位字，直接抽出末 4 字节做对齐比对很繁琐；而「整帧含 FCS 喂入 CRC，残留应为固定魔数」与 FCS 的字节位置无关，实现更简洁。

**练习 2**：8 位 check 用了几级延时流水线？64 位 check 还需要同样长的延时吗？
**答案**：8 位用 4 级（对应 4 字节 FCS）。64 位改用魔数法，不再需要为「排除 FCS」而延时，但仍需 1 级（`s_axis_tdata_d0`）用于跨字的 `STATE_LAST` 处理。

---

## 5. 综合实践

把本讲三个模块串起来，构建一个**最小以太网帧发送—校验闭环**（源码阅读 + 设计型实践）：

1. **组装数据通路**：`应用载荷 → axis_eth_fcs_insert（加 FCS + 可选填充）→ 模拟线路 → axis_eth_fcs_check（校验）→ 应用`。
2. **验证一致性**：用 `axis_eth_fcs`（纯计算器）旁路采集发送侧 FCS，确认它等于 insert 实际追加的 4 字节（小端）。
3. **注入错误**：在线路上翻转 payload 中的某一位，观察 check 模块的 `m_axis_tuser` 是否在帧尾拉高、`error_bad_fcs` 是否产生脉冲。
4. **填充实验**：把 `ENABLE_PADDING` 设为 1，发送一个 10 字节短帧，观察 insert 输出的帧是否被 0 填充到接近最小帧长，并确认 FCS 覆盖了填充字节（用 check 校验应仍为好帧）。

完成后再回答：如果去掉 check 的 4 级延时流水线（4.2 练习 1 的假设），这个闭环会在哪一步失败？这能帮你把「CRC 累加范围」「FCS 比对时机」「延时流水线作用」三者真正串起来。

## 6. 本讲小结

- FCS 就是参数固定的 CRC-32（`0x04C11DB7`、初值 `0xFFFFFFFF`、反射、终值取反），底层都由 `rtl/lfsr.v` 实现。
- `axis_eth_fcs` 是**纯计算旁路**：`tready` 恒 1、帧尾输出 `~crc_next`，不改数据流。
- `axis_eth_fcs_check` 用 **4 级延时流水线**让 CRC 滞后 4 拍，使帧尾比对恰好排除 FCS；坏帧通过 `tuser` 与 `error_bad_fcs` 上报。
- `axis_eth_fcs_insert` 用 4 状态机（IDLE/PAYLOAD/PAD/FCS）在帧尾追加 FCS，可选 0 填充至最小帧长；CRC 算的是「实际输出字节」。
- 8 位与 64 位实现策略不同：8 位 check「直接比对末 4 字节」，64 位 check「魔数残留法」以应对 `tkeep` 与跨字场景。
- 三个模块共用同一套 AXI-Stream 输出级（双寄存器 + temp 缓冲）反压模板。

## 7. 下一步学习建议

- 下一讲 `u3-l1` 将进入**以太网成帧层**（`eth_axis_rx/tx`），你会看到 FCS 在完整以太网帧（目的/源 MAC + 类型 + 载荷 + FCS）中的位置，本讲的 `axis_eth_fcs_check/insert` 正是 MAC 收发通路的组成部分。
- 若想立刻看到 FCS 在真实 MAC 中的用法，可先跳读 `rtl/axis_gmii_rx.v`（`u4-l1`）——它在 GMII 接收侧集成了 FCS 校验逻辑。
- 建议把本讲的「双 DUT 比较实践」实际搭起来跑通，因为后续讲义的 cocotb 测试会大量复用 `eth_ep.EthFrame` + AXI-Stream 端点的套路。
