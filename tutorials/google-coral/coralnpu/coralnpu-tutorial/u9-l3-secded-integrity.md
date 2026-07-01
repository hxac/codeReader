# 总线完整性与 SECDED

> 前置讲义：u3-l3（TileLink-UL 与 AXI 桥接）。本讲假设你已经知道 TL-UL 只有 A（请求）、D（响应）两个通道，以及 `Decoupled`（valid/ready/bits）握手语义。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 **SECDED（单纠错 / 双检错）** 编码在总线数据保护里解决什么问题，以及它的数学边界（能纠正几位、能检出几位）。
- 读懂 CoralNPU 为 TL-UL 附加完整性（integrity）的硬件：`SecdedEncoder` 如何生成 7 位 ECC，`Request/ResponseIntegrityGen/Check` 如何把 ECC 挂到 A/D 通道并在接收侧比对。
- 理解 `PortIntegrity.wrapHost/wrapDevice` 为什么把完整性收归 crossbar 边界，让外设产出 / 消费「干净」的 TL-UL。
- 用 `secded_golden.py` 这个 Python 黄金模型为编码器 / 检查器做协同验证，并**如实区分**「本仓库硬件只做检测（fault）」与「SECDED 码本身的纠错能力」。

## 2. 前置知识

### 2.1 为什么总线需要完整性保护

数据在片上总线里传送时，可能因软错误（SEU，宇宙射线击中晶体管）、串扰、时序裕量不足等原因发生比特翻转。如果一条「写 0x1000」的地址被翻成「写 0x3000」，后果可能是写穿到错误的外设。**完整性（integrity）** 的思路是：在数据之外**额外带几校验位**，让接收方能判断「这份数据是否还是发送方当初发出的那份」。

注意一个常见误解：**奇偶校验（parity）只能「检出奇数个错」，且不能定位错误**。1 位翻转能检出，但 2 位翻转会让奇偶位恢复正常 → 漏检。要让总线既「检出」又尽量「定位」，需要更强的编码。

### 2.2 从奇偶到 SECDED

设数据有 \(d\) 位，校验位有 \(r\) 位，码字共 \(d+r\) 位。校验位是数据位的若干奇偶组合。接收方重算校验位，与收到的校验位逐位异或，得到一个 \(r\) 位的**伴随式（syndrome）**：

- syndrome 全 0 → 没出错。
- syndrome 非 0 → 出错了；如果码的最小汉明距离足够大，syndrome 的不同取值还能**一一对应到出错的位置**，从而翻转该位完成**纠正**。

要能纠正 1 位错，校验位数量需满足：

\[
2^r \ge d + r + 1
\]

对 \(d=32\)：取 \(r=7\)，\(2^7=128 \ge 32+7+1=40\)，富富有余。这就是 **39_32 码**（32 数据 + 7 校验 = 39 位码字）。

要进一步做到「纠正 1 位、检出 2 位」（**SECDED = Single-Error Correction, Double-Error Detection**），码的最小汉明距离要达到 **4**。这样：

- 0 位错：syndrome = 0。
- 1 位错：syndrome 非 0 且可定位 → **可纠正**。
- 2 位错：syndrome 非 0 但落在「不可纠正」的情形 → **可检出但不可纠正**。

> ⚠️ **本讲最重要的「实事求是」点**：SECDED 的「单纠」是**这种码的数学能力**；而 CoralNPU 在本仓库里**只把这套码用于「检测」**——硬件重算 ECC、与收到的 ECC 比较，不一致就拉一个 `fault` 标志，**没有**做 syndrome 译码、**没有**纠正数据通路。Python 黄金模型 `secded_golden.py` 也**只提供编码函数（`*_enc`），不提供解码器**。所以本讲里凡是「纠正」二字，都指 SECDED 码的性质或我们自己在练习里做的数学验证，而**不是**仓库里已实现的硬件行为。

### 2.3 OpenTitan 的「带反转」编码（`_inv`）

本仓库注释明确写道：ECC 逻辑基于 OpenTitan 的 `prim_secded_inv_*`，以保证兼容。后缀 `_inv`（inverted）的意思是：算完校验位后，对整个码字异或一个固定常数（把 7 位 ECC 反相）。这让「全 0 数据 + 全 0 ECC」**不再是合法码字**——可以用来抓「未初始化的存储」。对 syndrome 计算而言，反相是透明的（解侧先异或回去即可）；对本仓库的「重算并比较」做法更是无所谓——发送方和接收方都用同一套带反转的编码，比的是最终结果。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [hdl/chisel/src/bus/TlulIntegrity.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulIntegrity.scala) | **核心**。`Secded` 纯组合函数、`SecdedEncoder` 参数化编码器、A/D 通道的 `Gen/Check` 四件套、`PortIntegrity` 端口封装。 |
| [hdl/chisel/src/bus/TileLinkUL.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TileLinkUL.scala) | 定义 A/D 通道的 `user` 捆绑，完整性位（`cmd_intg`/`data_intg`/`rsp_intg`）就挂在这里。 |
| [hdl/chisel/src/bus/SecdedEncoderTestbench.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/SecdedEncoderTestbench.scala) | 把单个 `SecdedEncoder` 包成可被 cocotb 驱动的 DUT（32/57/128 三种宽度）。 |
| [coralnpu_test_utils/secded_golden.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/secded_golden.py) | **Python 黄金模型**。`secded_inv_39_32_enc` / `secded_inv_64_57_enc` 及 `get_cmd_intg` / `get_data_intg` / `get_rsp_intg`。 |
| [tests/cocotb/tlul/test_secded_encoder.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/test_secded_encoder.py) | 用随机数据比对 RTL 编码器与黄金模型。 |
| [tests/cocotb/tlul/test_tlul_integrity.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/test_tlul_integrity.py) | 测 A/D 通道的 Gen/Check；用「翻转 ECC 位」注入错误并断言 `fault` 拉高。 |
| [hdl/chisel/src/soc/CoralNPUXbar.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala) | crossbar 在每个主机 / 从机端口调用 `PortIntegrity.wrapHost/wrapDevice`。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：① SECDED 编码与 `SecdedEncoder`；② TL-UL 通道完整性（Gen/Check 四件套）；③ Crossbar 边界的 `PortIntegrity`；④ `secded_golden.py` 黄金模型与协同验证。

---

### 4.1 SECDED 编码与 SecdedEncoder

#### 4.1.1 概念说明

`SecdedEncoder` 是一个**纯组合、参数化**的编码器：输入 `DATA_W` 位数据，输出「数据 + 7 位 ECC」。它支持 32 / 57 / 64 / 128 / 256 五种宽度，对应三种编码策略：

- **32 位**：直接用 39_32 码（32 数据 + 7 校验）。
- **57 位**：直接用 64_57 码（57 数据 + 7 校验）。
- **64/128/256 位**：用「**折叠（folding）**」方案——把宽数据切成多个 32 位块，每块各算一个 39_32 的 7 位 ECC，再把这些 7 位 ECC **异或**在一起，仍只得到 7 位 ECC。

折叠是个有意思的取舍：128 位数据理论上需要更多校验位才能逐位 SECDED；但项目选择**只用 7 位 ECC 覆盖整段 128 位**，牺牲了「定位到具体 32 位 lane」的精度，换来更少的完整性位开销。它仍能检出错误（任意 lane 的翻转都会改变折叠后的异或结果），这与「完整性 = 检测」的使用场景是匹配的。

#### 4.1.2 核心流程

39_32 编码的算法（57 位同理，只是掩码与反转常数不同）：

1. 取 32 位数据 `data`。
2. 对 7 个固定掩码分别做 `data & mask`，再求整体奇偶（`xorR`），得到 7 个校验位 `checksum(0..6)`。每个掩码就是 H 矩阵的一行，规定「哪些数据位参与这一位校验」。
3. 拼成 39 位 `Cat(checksum, data)`（校验在高位）。
4. 异或反转常数 `0x2A00000000`，得到最终码字；ECC 就是码字最高 7 位。

折叠方案的伪代码：

```
ecc = 0
for chunk in split(data, 32 bits):      # 128 位 → 4 块
    ecc ^= top7(ecc39_32(chunk))        # 每块取 7 位 ECC，累抵异或
return Cat(ecc, data)                    # 7 位 ECC + 原始 128 位数据
```

#### 4.1.3 源码精读

`Secded.ecc39_32` 用 7 个掩码算奇偶，最后异或 `0x2A00000000`：

[hdl/chisel/src/bus/TlulIntegrity.scala:26-46](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulIntegrity.scala#L26-L46) —— `Secded` 对象：7 个掩码（如 `0x002606BD25`）各做 `xorR` 得到 7 位校验，拼装后异或反转常数，返回 39 位码字。

`SecdedEncoder` 的宽度分派与折叠（128 位即切成 4 个 32 位块）：

[hdl/chisel/src/bus/TlulIntegrity.scala:75-119](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulIntegrity.scala#L75-L119) —— 参数化编码器：`IO_W` 由 `DATA_W` 决定（32→39、57→64、其余 → DATA_W+7）；128 位走 `Vec(4, UInt(32.W)).map(...).reduce(_^_)` 的折叠。`io.ecc_o := io.data_o(IO_W-1, DATA_W)` 取最高 7 位作 ECC。

#### 4.1.4 代码实践

**目标**：用 cocotb 回归确认 RTL 编码器在 32 / 57 / 128 三种宽度下都与 Python 黄金模型逐位一致。

**步骤**：

1. 阅读测试 [tests/cocotb/tlul/test_secded_encoder.py:43-65](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/test_secded_encoder.py#L43-L65)：它对 1000 组随机数据驱动 `io_data_i`，按宽度分别调 `secded_inv_39_32_enc` / `secded_inv_64_57_enc` / `get_data_intg`，再 `assert dut_ecc == golden_ecc`。
2. 跑三个目标（见 [tests/cocotb/tlul/BUILD:246-326](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/BUILD#L246-L326)，`hdl_toplevel` 分别是 `SecdedEncoderTestbench128/32/57`）：

   ```bash
   bazel test //tests/cocotb/tlul:secded_encoder_cocotb_test
   bazel test //tests/cocotb/tlul:secded_encoder_32_cocotb_test
   bazel test //tests/cocotb/tlul:secded_encoder_57_cocotb_test
   ```

**需要观察的现象**：每个用例打印 `Successfully compared 1000 random data values for data width ...`。

**预期结果**：三条用例全部 PASS。若失败，`assert` 信息会打印出 `data=... dut_ecc=... golden_ecc=...`，便于定位是哪一位掩码不一致。

> 待本地验证：具体耗时与日志格式取决于本机 Bazel / Verilator 环境。

#### 4.1.5 小练习与答案

**Q1**：`SecdedEncoder` 对 128 位数据只用 7 位 ECC，相比「每 32 位各带 7 位 ECC（共 28 位）」省了多少位？代价是什么？

**答**：省了 21 位完整性开销。代价是丢失了「定位到具体 32 位 lane」的能力——折叠后的异或只能告诉你「128 位里某处错了」，但给不出 syndrome 级别的定位，因此无法纠正。

**Q2**：把 `ecc39_32` 里最后的 `^ "h2A00000000".U` 去掉，编码还能正常工作吗？

**答**：能算出一个合法的 SECDED 码字，但**不再是 OpenTitan `prim_secded_inv` 兼容**的格式——与上游 / 黄金模型的位级结果会对不上，且失去「全 0 非法码字」的反转保护。本仓库靠发送 / 接收双方用同一套编码来比对，去掉反转后双方仍自洽，但跨 IP 互操作会断。

---

### 4.2 TL-UL 通道完整性：Gen / Check 四件套

#### 4.2.1 概念说明

TL-UL 的 A、D 通道各自带一个 `user` 捆绑，完整性位就藏在里面。CoralNPU 用的是 OpenTitan 风格的「**端到端完整性**」：不仅给数据加 ECC，**连「命令本身」（地址 / opcode / mask 等）也加 ECC**。这样既能抓数据翻转，也能抓「命令被篡改」（例如地址位翻转）。

于是每个通道方向各有两个模块：

- **Gen（生成）**：在发送侧，按字段重算 ECC，写进 `user`。
- **Check（检查）**：在接收侧，按同样的字段重算「期望 ECC」，与收到的 `user` 里的 ECC 比较，不一致拉 `fault`。

共四个：`RequestIntegrityGen` / `RequestIntegrityCheck`（A 通道）、`ResponseIntegrityGen` / `ResponseIntegrityCheck`（D 通道）。

#### 4.2.2 核心流程

**A 通道**的完整性分两段：

- **命令完整性 `cmd_intg`**：把 `Cat(instr_type, address, opcode, mask)` 拼成 57 位命令字，过 `ecc64_57` 得 7 位 ECC。
- **数据完整性 `data_intg`**：把 `a.data`（总线宽度，如 128 位）过 `SecdedEncoder(p.w*8)`（折叠）得 7 位 ECC。

**D 通道**类似：

- **响应完整性 `rsp_intg`**：把 `Cat(opcode, size, error)` 拼成 57 位响应字，过 `ecc64_57`。
- **数据完整性 `data_intg`**：同上。

Check 的逻辑只有一行：「期望值 ≠ 收到值」即 `fault`：

```
fault := (expected_cmd_intg =/= a_i.user.cmd_intg) ||
         (expected_data_intg =/= a_i.user.data_intg)
```

注意 `cmd_w` 固定是 57，而 `Cat(...)` 拼出的实际位数会随 `mask` 宽度变化（mask 宽 = `p.w`，即总线字节宽）；不足 57 位时在 MSB 补 0。Check 侧用**完全相同**的拼接与编码，所以「补 0」对双方一致。

#### 4.2.3 源码精读

完整性位在 `user` 捆绑里的位置：

[hdl/chisel/src/bus/TileLinkUL.scala:47-57](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TileLinkUL.scala#L47-L57) —— `OpenTitanTileLink_A_User` 含 `rsvd(5)/instr_type(4)/cmd_intg(7)/data_intg(7)`；`_D_User` 含 `rsp_intg(7)/data_intg(7)`。这就是完整性随每笔事务流动的载体。

A 通道生成：拼 57 位命令字 + 编码 + 写回 `user`：

[hdl/chisel/src/bus/TlulIntegrity.scala:132-164](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulIntegrity.scala#L132-L164) —— `RequestIntegrityGen`：其余字段直通（`io.a_o := io.a_i`），仅重算 `cmd_intg`（`Cat(instr_type, address, opcode, mask)`）与 `data_intg`。

A 通道检查：重算期望值并比较：

[hdl/chisel/src/bus/TlulIntegrity.scala:169-199](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulIntegrity.scala#L169-L199) —— `RequestIntegrityCheck`：用同样的拼接 / 编码算 `expected_cmd_intg` / `expected_data_intg`，二者任一不等即 `io.fault := true`。**只检测，不纠错，无 syndrome 输出**。

D 通道的 Gen / Check 结构与 A 完全对称，只是命令字换成 `Cat(opcode, size, error)`：

[hdl/chisel/src/bus/TlulIntegrity.scala:204-270](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulIntegrity.scala#L204-L270) —— `ResponseIntegrityGen` / `ResponseIntegrityCheck`：响应字 57 位 `Cat(opcode, size, error)`，`rsp_w = 57`，同样 `fault := (expected_rsp_intg =/= ...) || (expected_data_intg =/= ...)`。

#### 4.2.4 代码实践

**目标**：用项目自带的 fault 注入测试，亲眼看到「ECC 位被翻转 → `fault` 拉高」。

**步骤**：

1. 阅读 [tests/cocotb/tlul/test_tlul_integrity.py:98-173](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/test_tlul_integrity.py#L98-L173) 的 `test_request_integrity_check`：它做三笔事务——① 正确 ECC（`assert not fault`）；② 把 `cmd_intg` 取反（`~correct_cmd_intg & 0x7F`）→ `assert fault`；③ 恢复 `cmd_intg`、把 `data_intg` 取反 → `assert fault`。响应通道的测试（`test_response_integrity_check`）同构。
2. 跑回归（DUT 顶层 `TlulIntegrityTestbench`，见 [tests/cocotb/tlul/BUILD:223-244](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/BUILD#L223-L244)）：

   ```bash
   bazel test //tests/cocotb/tlul:tlul_integrity_cocotb_test
   ```

**需要观察的现象**：四个用例（`test_request_integrity_gen/check`、`test_response_integrity_gen/check`）全过；波形里 `io_req_check_fault` / `io_rsp_check_fault` 在注入翻转的周期拉高。

**预期结果**：全部 PASS。这验证了「检测」通路：任何 ECC 位翻转都会被 Check 抓到。

> 待本地验证：波形的具体周期数以本机运行为准。注意测试通过「翻转 ECC 位」注入错误——它验证的是「完整性位本身被篡改会被检出」，这与「数据位翻转」在 Check 侧等价（两者都会让重算值 ≠ 收到值）。

#### 4.2.5 小练习与答案

**Q1**：为什么 `cmd_intg` 要保护 `address / opcode / mask`，而不是只保护 `data`？

**答**：因为「写到哪、写什么、写几个字节」由 address / opcode / mask 决定。若只保护 data，一个地址位翻转（`0x1000→0x3000`）会让「正确的数据」写进错误的外设寄存器，而 ECC 全程不变、毫无察觉。命令完整性正是为了堵住这个口子。

**Q2**：`RequestIntegrityCheck` 发现 `fault` 后，会纠正数据并继续传送吗？

**答**：**不会**。它只输出一个 `fault` 布尔，数据照原样通过（模块本身对数据是直通）。如何处置 `fault`（告警、中断、停机）由上层决定——这是 OpenTitan「检出即告警、不静默纠正」的完整性哲学。本仓库的 `PortIntegrity` 甚至用 `dontTouch(dChk.io.fault)` 把这个信号保留下来供观测（见 4.3）。

---

### 4.3 Crossbar 边界的 PortIntegrity 封装

#### 4.3.1 概念说明

如果每个外设都要自己算 / 验完整性，代码会很啰嗦，而且容易写错。CoralNPU 的设计是：**完整性归 crossbar 边界统一管**——主机侧进入 crossbar 时由 xbar 生成 A 通道完整性、校验 D 通道完整性；从机侧离开 crossbar 时由 xbar 校验 A 通道、生成 D 通道完整性。于是**外设看到的 TL-UL 是「干净的」（不含完整性责任）**，外设只管业务逻辑。

这正是 `PortIntegrity` 的两个方法做的事：

- `wrapHost`：包主机端口。A 通道「进 xbar」时 **Gen**（生成完整性），D 通道「出 xbar」时 **Check**（静默校验）。
- `wrapDevice`：包从机端口。A 通道「出 xbar 进 device」时 **Check**，D 通道「device 回 xbar」时 **Gen**。

#### 4.3.2 核心流程

主机侧数据流（`wrapHost`）：

```
external(host, 干净) ──A──> [RequestIntegrityGen]  ──A'(带intg)──> xbar内部
xbar内部 ──D'(带intg)──> [ResponseIntegrityCheck] ──D──> external(host, 干净)
                                            └──> fault (dontTouch 保留)
```

从机侧数据流（`wrapDevice`）方向相反：

```
xbar内部 ──A'(带intg)──> [RequestIntegrityCheck] ──A──> external(device, 干净)
                                    └──> fault (dontTouch 保留)
external(device, 干净) ──D──> [ResponseIntegrityGen] ──D'(带intg)──> xbar内部
```

两个方法都强调「请在正确的时钟域里用 `withClockAndReset` 实例化」——因为跨时钟域的主机 / 从机要用各自的时钟。

#### 4.3.3 源码精读

`wrapHost`：A 进则 Gen、D 出则 Check，并 `dontTouch` 保留 fault：

[hdl/chisel/src/bus/TlulIntegrity.scala:285-303](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulIntegrity.scala#L285-L303) —— `wrapHost`：实例化 `RequestIntegrityGen`（`aGen`）与 `ResponseIntegrityCheck`（`dChk`），把 `external.a.bits` 经 `aGen` 后送入 wrapped，wrapped.d 经 `dChk` 校验，`dontTouch(dChk.io.fault)` 防 Chisel 优化掉这个告警信号。

`wrapDevice`：A 出则 Check、D 进则 Gen：

[hdl/chisel/src/bus/TlulIntegrity.scala:311-328](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulIntegrity.scala#L311-L328) —— `wrapDevice`：实例化 `RequestIntegrityCheck`（`aChk`）与 `ResponseIntegrityGen`（`dGen`）；`internal.a`（带 intg）经 `aChk` 校验后以干净形式送 `external.a`，`external.d` 经 `dGen` 加上完整性后回送 `internal.d`。

在 crossbar 里的实际调用点（按时钟域分两种实例化方式）：

[hdl/chisel/src/soc/CoralNPUXbar.scala:113-120](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L113-L120) —— 主机端口：主时钟域直接调 `PortIntegrity.wrapHost(...)`，非主时钟域则套 `withClockAndReset(domainPorts.clock, domainPorts.reset)` 再调。从机侧 `wrapDevice` 在 [CoralNPUXbar.scala:188-195](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L188-L195) 同构。

#### 4.3.4 代码实践

**目标**：在 SoC 级 crossbar 测试里观察「宽 → 窄跨桥时完整性仍被正确处理」。

**步骤**：

1. 在 [tests/cocotb/tlul/BUILD:338-351](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/BUILD#L338-L351) 找到用例 `test_wide_to_narrow_integrity`（DUT 顶层 `CoralNPUXbarTestHarness`，依赖 `secded_golden`）。
2. 运行：

   ```bash
   bazel test //tests/cocotb/tlul:coralnpu_xbar_cocotb --test_filter=test_wide_to_narrow_integrity
   ```

**需要观察的现象**：宽主机（128 位）发往窄从机（经 `TlulWidthBridge`）的事务，完整性在边界被 Gen/Check 正确处理，没有误报 `fault`。

**预期结果**：用例 PASS。这说明 `PortIntegrity` 与位宽桥（`TlulWidthBridge`）协同工作——位宽转换发生在「干净 TL-UL」侧，完整性则在统一位宽的 xbar 内部生成 / 校验，互不干扰。

> 待本地验证：`--test_filter` 的精确写法以本机 cocotb 版本为准；也可直接跑整个 `coralnpu_xbar_cocotb`。

#### 4.3.5 小练习与答案

**Q1**：为什么 `wrapDevice` 里 `internal.a.bits` 直接透传给 `external.a.bits`，而 `external.d.bits` 却要经过 `ResponseIntegrityGen`？

**答**：因为完整性由 xbar 拥有、外设产出干净 TL-UL。A 通道方向是「xbar → device」，xbar 内部带着完整性，到 device 前把完整性剥掉（透传净数据，同时 Check 一遍留 fault）；D 通道方向是「device → xbar」，device 给的是干净 D，进 xbar 前要由 xbar **补上**完整性（Gen），后续 xbar 内部与主机侧才能继续校验。

**Q2**：`dontTouch(dChk.io.fault)` / `dontTouch(aChk.io.fault)` 的作用是什么？去掉会怎样？

**答**：防止 Chisel / FIRRTL 综合工具因为 `fault` 没被下游显式消费而把它优化掉。去掉后，告警信号可能在网表里消失，仿真 / 上板都观测不到完整性违例——这对一个「安全检测」信号是危险的，所以显式钉住。

---

### 4.4 secded_golden.py 黄金模型与协同验证

#### 4.4.1 概念说明

ECC 这种「位级精确」的逻辑，最容易在移植（Verilog ↔ Chisel ↔ Python）时抄错一个掩码。CoralNPU 的做法是维护一份 **Python 黄金模型** `secded_golden.py`，与 RTL 用同一套掩码 / 反转常数，再由 cocotb 在仿真里逐笔比对。这样 RTL 与参考模型互相约束，掩码写错会立刻在 1000 组随机向量里暴露。

这份模型提供：

- `secded_inv_39_32_enc(data)` / `secded_inv_64_57_enc(data)`：32 / 57 位数据的 7 位 ECC（与 RTL 的 `Secded.ecc39_32` / `ecc64_57` 位级一致）。
- `get_cmd_intg(a_channel, width)`：拼 A 通道命令字 → `ecc64_57`。
- `get_data_intg(data, width)`：32 位走 `ecc39_32`；128 位走折叠（4 块 ECC 异或）。
- `get_rsp_intg(d_channel, width)`：拼 D 通道响应字 → `ecc64_57`。

> 注意：模型里**只有 `*_enc`（编码）**，没有 `*_dec`（解码 / 纠错）。这再次印证了 2.2 节的结论——本仓库把 SECDED 当作「检测」用，黄金模型也只覆盖到「生成 + 比对」这一层。

#### 4.4.2 核心流程

`secded_inv_39_32_enc` 的步骤与 RTL 完全对应：

1. `_parity(data & mask)` 算每个校验位（`_parity` 就是逐步异或，等价于 Chisel 的 `xorR`）。
2. 把 7 个校验位拼到 32 位数据的高位，组成 39 位。
3. 异或反转常数 `0x2A00000000`。
4. 返回最高 7 位（`>> 32`），即 ECC。

折叠版 `get_data_intg(width=128)`：把 128 位数切成 4 个 32 位 lane，各算 `secded_inv_39_32_enc`，再 `ecc0 ^ ecc1 ^ ecc2 ^ ecc3`——与 RTL `SecdedEncoder(128)` 的 `Vec(4).map(...).reduce(_^_)` 一致。

#### 4.4.3 源码精读

奇偶函数与 39_32 编码（掩码与 RTL 逐一对应）：

[coralnpu_test_utils/secded_golden.py:18-53](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/secded_golden.py#L18-L53) —— `_parity` 逐步异或；`secded_inv_39_32_enc` 用 7 个与 RTL 相同的掩码（`0x002606BD25` 等）算校验位，异或 `0x2A00000000` 后返回高 7 位。

A/D 通道命令字 / 响应字的打包与编码：

[coralnpu_test_utils/secded_golden.py:85-136](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/secded_golden.py#L85-L136) —— `get_cmd_intg` 按 `Cat(instr_type, address, opcode, mask)` 顺序打包（注释里写明位宽），过 `secded_inv_64_57_enc`；`get_data_intg` 对 128 位做 4 路折叠异或；`get_rsp_intg` 按 `Cat(opcode, size, error)` 打包。

这些函数正是 [tests/cocotb/tlul/test_tlul_integrity.py:20](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/test_tlul_integrity.py#L20) 导入、用来构造「正确 ECC」与「翻转 ECC」的依据。

#### 4.4.4 代码实践

**目标**：用黄金模型亲手生成一组编码值，并写一段**示例代码**验证 SECDED「单比特可定位、双比特可区分」的数学性质（注意：这是数学验证，**不是**仓库已有的硬件行为；仓库硬件只检测）。

**步骤**：

1. 直接调用黄金模型生成一对编码值（示例代码，可在任意 Python 环境运行）：

   ```python
   # 示例代码：用 secded_golden 观察「重算 ECC ≠ 收到 ECC」即检出
   import sys
   sys.path.insert(0, "coralnpu_test_utils")
   from secded_golden import secded_inv_39_32_enc

   data = 0xDEADBEEF
   ecc_good = secded_inv_39_32_enc(data)          # 正确 ECC（7 位）
   print(f"data=0x{data:08X}  ecc=0x{ecc_good:02X}")

   # 模拟「数据最低位翻转」的软错误
   data_bad1 = data ^ 0x00000001
   ecc_recomputed = secded_inv_39_32_enc(data_bad1)  # 接收方按损坏数据重算
   print(f"1-bit flip: 重算ecc=0x{ecc_recomputed:02X}, "
         f"与收到ecc相等? {ecc_recomputed == ecc_good}")  # 预期 False → 检出
   ```

   这段脚本的逻辑与 `RequestIntegrityCheck` 完全同构：接收方拿着「收到的 ECC」和「按数据重算的 ECC」比，不等就是 `fault`。区别只是硬件把结果压成一个 `fault` 布尔。

2. （延伸 / 待本地验证）要进一步演示 SECDED 的**纠错**能力，需要实现一个 syndrome 译码器：把「数据 + 收到 ECC」拼成 39 位码字， uninvert 后重算 7 位 syndrome，用 H 矩阵定位翻转位。这超出了 `secded_golden.py` 的范围（它没有 decoder），需要你参照掩码自行实现。你可以预期：1 位翻转 → syndrome 非零且唯一定位到某位（可纠正）；2 位翻转 → syndrome 非零但不对应单一位置（可检出不可纠）。

**需要观察的现象**：步骤 1 中，1 位数据翻转后「重算 ECC ≠ 收到 ECC」为 `True`（即被检出），打印 `False`。

**预期结果**：打印类似 `1-bit flip: ... 与收到ecc相等? False`。

> 待本地验证：具体 ECC 数值以本机 Python 运行为准（依赖 `secded_golden.py` 的掩码实现）。步骤 2 的纠错译码器需自行编写，本仓库不提供。

#### 4.4.5 小练习与答案

**Q1**：`get_data_intg(data, width=128)` 为什么把 4 个 lane 的 ECC 异或在一起，而不是拼接成 28 位？

**答**：为了和 RTL `SecdedEncoder(128)` 的折叠方案保持一致——只产出 7 位 `data_intg` 塞进 `user.data_intg(7.W)`。拼接 28 位会撑爆 `user` 捆绑里 7 位的字段。黄金模型必须与 RTL 的「折叠」选择位级对齐，否则协同验证会假性失败。

**Q2**：如果有人改了 RTL 里 `ecc39_32` 的某个掩码，但忘了同步改 `secded_golden.py`，哪个测试会先红？

**答**：`secded_encoder_*_cocotb_test` 会先红——它对 1000 组随机数据逐组 `assert dut_ecc == golden_ecc`，掩码不一致会让大量断言失败并打印出具体的 `data / dut_ecc / golden_ecc`，直接指向出错的那一位校验。这正是黄金模型的价值。

---

## 5. 综合实践

把四个模块串起来，做一次「**从字段到 fault**」的端到端追踪：

1. **构造一笔 A 通道写请求**：地址 `0x1000`、数据 `0x1122...FF00`（128 位）、`mask=0xFFFF`、`instr_type=0`。用 `secded_golden.get_cmd_intg` 和 `get_data_intg(data, width=128)` 算出正确的 `cmd_intg` / `data_intg`。
2. **画出这笔请求在 crossbar 里的完整性流转**：进入 `wrapHost` 时由 `RequestIntegrityGen` 生成 `cmd_intg` / `data_intg` → 经 Socket 路由 → 到达目标从机前由 `wrapDevice` 的 `RequestIntegrityCheck` 校验。在图上标出「哪一段是干净 TL-UL、哪一段带完整性」。
3. **预测并验证 fault 行为**：若在 `wrapDevice` 的 Check 之前，把 `data_intg` 的最低位翻转，画出 `RequestIntegrityCheck` 的 `expected_data_intg` 与（被篡改的）`a_i.user.data_intg` 的比较结果，写出 `fault` 取值。再用 `tlul_integrity_cocotb_test` 的 `test_request_integrity_check` 印证你的判断。
4. **反思**：用一句话回答——「这笔事务如果发生的是『数据位翻转』而非『ECC 位翻转』，Check 侧的表现有何不同？」（提示：从「重算 ECC ≠ 收到 ECC」这个统一判据出发想。）

> 参考结论：第 4 点——**表现相同**。无论翻转发生在数据位还是 ECC 位，只要「按（损坏后的）数据重算的 ECC」与「收到的 ECC」不一致，`fault` 就会拉高。这也正是「检测」模型简洁的代价：它把单比特 / 双比特 / 多比特错都压成同一个 `fault`，不去区分能否纠正。

## 6. 本讲小结

- SECDED 是最小汉明距离为 4 的编码：**单比特错可纠正、双比特错可检出**；39_32 码用 7 位校验覆盖 32 位数据（\(2^7=128 \ge 40\)）。
- CoralNPU 的完整性基于 OpenTitan `prim_secded_inv_*`，带「反转」以让全零成为非法码字；`SecdedEncoder` 对 128/256 位宽数据采用**折叠**（多块 ECC 异或），只用 7 位 ECC，牺牲定位精度换开销。
- TL-UL 在 `user` 捆绑里带 `cmd_intg` / `data_intg` / `rsp_intg`；A/D 通道各有 Gen/Check 一对，Check 侧「重算并比较」输出 `fault`。**本仓库只检测、不纠错**，黄金模型也只有编码器。
- `PortIntegrity.wrapHost/wrapDevice` 把完整性收归 crossbar 边界，让外设产出 / 消费干净 TL-UL；`fault` 用 `dontTouch` 钉住防被优化。
- RTL 与 `secded_golden.py` 用同一套掩码 / 常数，由 cocotb 用 1000 组随机向量与 fault 注入做协同验证，掩码抄错会立刻暴露。

## 7. 下一步学习建议

- **向「纠错」延伸**：本仓库止步于检测。若想理解 SECDED 的完整译码（syndrome 定位、单纠双检的判定），建议阅读 OpenTitan 上游 `prim_secded_*.sv` 的 `_dec` 实现，并尝试用本讲的掩码写一个 Python decoder，对照 4.4.4 的步骤 2 验证。
- **向「端到端」延伸**：完整性是总线可靠性的横向机制；纵向看，CoralNPU 还在 SRAM 侧做行级 ECC。可阅读 `hdl/chisel/src/coralnpu/TCM.scala`、`SramNx128.scala` 等存储包装，对比「总线完整性（检测）」与「存储 ECC（常含纠错）」的侧重差异。
- **回归验证体系**：本讲的 cocotb 用例属于第 11 单元「验证流程」的一部分。学完本讲后，可继续 u11-l3（cocotb 回归测试体系），了解 `cocotb_test_suite` / `coco_tb.bzl` 如何批量组织这些目标。
