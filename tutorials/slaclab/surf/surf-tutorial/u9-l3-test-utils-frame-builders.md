# 测试辅助与帧构造器

## 1. 本讲目标

学完本讲后，你应当能够：

- 理解 `tests/axi/utils.py` 提供的三件 AXI 原语（`axil_read_u32` / `axil_write_u32` / `wait_sampled_ready`），并能解释「采样握手」为什么必须等一个时钟沿确认 `TREADY`。
- 说清为什么 `COMMON_CLK_G=true` 的被测件必须用 `start_lockstep_clocks`，而不是开两个同周期的独立时钟协程。
- 看懂各子系统 `*_test_utils.py` 的分工：低层字节/校验和工具放在 `ethmac_test_utils.py`，高层帧构造器在它之上分层（IPv4、UDP、SRP 等），并能用它们拼出一帧合法的以太网/IPv4/UDP 帧。
- 掌握「扁平数据通路里第一字节落在最低有效字节通道（小端排列）」这一贯穿全仓库的字节序约定，以及它如何反过来影响 MAC 配置寄存器的字节顺序。

本讲承接 u9-l2（一个 cocotb 测试的五段式骨架）与 u6-l1（EthMacCore 的收发通路、PAUSE、校验和、过滤），把焦点从「如何写一个测试」转到「测试之间如何复用激励与校验代码」。

## 2. 前置知识

在继续前，请确认你已理解下列概念（若陌生，先回看对应讲义）：

- **AXI / AXI-Lite / AXI-Stream 握手**：VALID/READY 在同一个采样时钟沿同时为 1 才完成一次搬运（u3-l1、u4-l1）。本讲大量依赖这条铁律。
- **SSI 侧带**：SOF 写在帧首拍首字节、EOFE 写在末拍末有效字节，靠 TUSER 编码（u5-l1）。
- **EthMacCore 的字节序**：帧首字节落在 `tData` 最低字节通道，故目的 MAC 在 `tData(47:0)`、EtherType 在 `tData(111:96)`（u6-l1）。
- **cocotb 协程与时钟**：`cocotb.start_soon`、`RisingEdge`、`Timer`、`with_timeout` 的基本用法（u9-l1、u9-l2）。
- **回归公共件**：`run_surf_vhdl_test` 如何把 pytest 反过来启动 GHDL（u9-l1）。

> 术语提示：本讲反复出现「beat（拍）」「lane（字节通道）」「sink（消费方）/source（生产方）」。一次 AXI-Stream 传输的一拍叫一个 beat；128 位数据平面里有 16 个字节通道（lane）。

## 3. 本讲源码地图

| 文件 | 角色 | 关键内容 |
| --- | --- | --- |
| `tests/axi/utils.py` | 全仓库 AXI 原语 | `axil_read_u32` / `axil_write_u32` / `wait_sampled_ready` / `ring_buffer_axil_addr` |
| `tests/common/regression_utils.py` | 回归公共件 | `start_lockstep_clocks` / `env_flag` 系列 / `build_vhdl_sources` / `run_surf_vhdl_test` |
| `tests/ethernet/EthMacCore/ethmac_test_utils.py` | 以太网底座工具 | 字节打包、internet 校验和、以太网/IPv4/UDP/PAUSE 帧构造、扁平 EMAC 源/汇驱动 |
| `tests/ethernet/IpV4Engine/ipv4_test_utils.py` | IPv4 高层帧 | 复用 ethmac 底座，叠加 ARP/ICMP/IGMP/UDP 帧构造 |
| `tests/protocols/srp/srp_test_utils.py` | SRPv3 协议模型 | `SrpV3Request`、`FlatSrpAxis` 收发驱动、`srpv3_header`、响应断言 |
| `tests/README.md` | 风格指南 | 明确 helper 的放置位置与「采样握手 / 公共时钟」两条硬约定 |

总体分工可以用一句话概括：**`axi/utils.py` 是搬运寄存器的「手」，`regression_utils.py` 是建时钟与跑仿真的「腿」，`*_test_utils.py` 是拼协议帧与核对响应的「嘴」**。三者层层叠加，越靠协议层越具体。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 AXI-Lite 原语与 AXI-Stream 采样握手**——`axil_read_u32` / `axil_write_u32` / `wait_sampled_ready`。
2. **4.2 公共时钟启动与回归公共件**——`start_lockstep_clocks`、`env_flag`、`build_vhdl_sources`。
3. **4.3 子系统帧构造器与记分板**——ethmac / ipv4 / srp 三层 `*_test_utils.py`。

### 4.1 AXI-Lite 原语与 AXI-Stream 采样握手

#### 4.1.1 概念说明

写 cocotb 测试时，最高频的两个动作是「读写一段 AXI-Lite 寄存器」和「往 AXI-Stream 源口推一拍数据并等它被吃掉」。这两件事看似琐碎，但**几乎所有子系统测试都要做**。如果每个测试文件各写一遍循环，既容易写错（忘记断言响应码、字节序写反），也会让风格指南形同虚设。于是 SURF 把它们收敛成 `tests/axi/utils.py` 里三个极短的协程函数。

这里有一个关键的握手细节，叫**采样握手（sampled handshake）**。AXI 规定：一次传输在第 k 个时钟沿完成，当且仅当该沿上 VALID 与 READY 同时为 1。

\[ \text{transfer}(k) \iff \text{VALID}(k)=1 \;\land\; \text{READY}(k)=1 \]

这意味着一个**源（source）必须在「确认 READY 的那个沿之前」把当前 beat 稳住**，而不能用软件的「我推完数据就立刻撤」的直觉去驱动。GHDL 是按 delta cycle 推进的，如果你在 `await RisingEdge` 之后立刻读 `TREADY`，可能读到的是上一拍的旧值；如果立刻撤掉 VALID，又可能在 DUT 还没采样到时就把它弄没了。两类错误都会制造「假失败」。`wait_sampled_ready` 就是用来钉死这个时序的。

#### 4.1.2 核心流程

`axil_read_u32` / `axil_write_u32` 的流程：

1. 调用 `cocotbext.axi` 提供的 master 发起一次 4 字节读/写。
2. 断言响应 `resp == AxiResp.OKAY`（不是 OKAY 直接让测试挂掉）。
3. 读：把返回的字节按 **little-endian** 解成 32 位整数返回；写：把整数按 little-endian 编码成 4 字节再发。

`wait_sampled_ready` 的流程（最多 `timeout_cycles` 拍）：

1. `await RisingEdge(clk)`——在沿之后才看，保证沿已经发生。
2. `await Timer(settle_time_ns)`——再等一小段，让 GHDL 把组合逻辑的 delta cycle 跑完，读到「沿之后」的真实 READY。
3. 若 `READY == 1`，说明这一拍传输已完成，返回。
4. 否则重复；超时则抛 `AssertionError`。

> 它与 u9-l2 里「开放式等待都要套 `with_timeout`」一脉相承：这里的 `timeout_cycles`（默认 1024）就是那个上界，避免死等。

#### 4.1.3 源码精读

寄存器读写两个协程都把「断言 OKAY + 字节序」收在一处：

- [tests/axi/utils.py:23-28](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/utils.py#L23-L28) —— `axil_read_u32`：读 4 字节、断言 `OKAY`、按 little-endian 解码返回。函数体内 `from cocotbext.axi import AxiResp` 是延迟导入，避免在模块加载期强制依赖。
- [tests/axi/utils.py:31-35](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/utils.py#L31-L35) —— `axil_write_u32`：把整数编成 4 字节 little-endian 写下、断言 `OKAY`。

`wait_sampled_ready` 的循环骨架：

```python
for _ in range(timeout_cycles):
    await RisingEdge(clk)
    await Timer(settle_time_ns, unit="ns")
    if int(ready_signal.value) == 1:
        return
...
raise AssertionError(f"Timed out waiting for sampled handshake on {label}")
```

- [tests/axi/utils.py:38-57](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/utils.py#L38-L57) —— 这是「沿 → 沉降 → 读 READY」的三段式，注释明确指出：源必须把当前 beat 稳住，直到某个沿确认 DUT 抬起了 `TREADY`。

风格指南把这条约定钉成了硬规则——见 [tests/README.md:99-103](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/README.md#L99-L103)：扁平 ready/valid 源要用 `wait_sampled_ready()`，它在 `cocotbext.axi` 的现成源不合适时使用；返回时传输**已经完成**，应立即推进或撤销源。

#### 4.1.4 代码实践

**实践目标**：体会「采样握手」相对于「立刻读 READY」的区别。

**操作步骤（源码阅读型实践）**：

1. 打开 [tests/protocols/srp/test_SrpV3AxiLite.py:46-63](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/protocols/srp/test_SrpV3AxiLite.py#L46-L63)，观察 `TB.__init__` 如何用 `AxiLiteRam.from_prefix` 建一个 AXI-Lite 从机模型。
2. 在该文件里搜索 `axil_read_u32` / `axil_write_u32` 的调用点，确认它们都被 `await`。
3. 回到 `wait_sampled_ready` 的循环体，把第 50 行的 `await Timer(settle_time_ns, unit="ns")` **在脑中删除**，思考：若 GHDL 在沿后还有未结算的 delta cycle，`ready_signal.value` 可能读到上一拍的值，从而让一个「本应在这一拍完成」的传输被误判成未完成、白白多等一拍——严重时会让背压场景的时序断言错位。

**需要观察的现象**：理解为什么风格指南反复强调「沿之后要沉降一小段」。

**预期结果**：你能用自己的话说出 `RisingEdge` 与 `Timer(1ns)` 这两步各自防的是哪类时序假象。

**待本地验证**：若你已在本地 `import` 过源码，可把 `settle_time_ns` 调成 `0` 跑一次 SRP 回归，看是否出现偶发的握手错拍（结果取决于 GHDL 版本与调度，故标注待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`axil_read_u32` 为什么用 `int.from_bytes(txn.data, "little")` 而不是 `"big"`？
**答案**：cocotbext.axi 的 AXI-Lite master 把 32 位数据按字节通道的小端序呈现（最低有效字节在前），与 SURF RTL 里数据字节的字节通道排列一致，故用 little-endian 解码。

**练习 2**：`wait_sampled_ready` 返回后，调用方为什么应当「立即推进或撤销源」，而不是再等一拍？
**答案**：返回意味着已经有一个沿确认了 `READY=1`，当前 beat 已被采样搬走。若继续把同一 beat 保持有效，下一个沿会被 DUT 当作**新的**一拍再次采样，造成重复传输。

---

### 4.2 公共时钟启动与回归公共件

#### 4.2.1 概念说明

很多 SURF 模块带一个 `COMMON_CLK_G` 泛型：当它为 `true` 时，数据时钟与 AXI-Lite 时钟在 RTL 里**逻辑上是同一根线**（同一个 buffer 的输出）。仿真时若偷懒，给两个时钟各起一个同周期的 `cocotb.clock.Clock` 协程，会得到两个**周期相同但相位可相对漂移**的振荡器——这并不能真正复现「共用时钟」的时序契约，反而可能让本应同时跳变的两个沿错开半拍，制造假失败或假通过。

正确做法是**用一个协程同时驱动多个信号**，让它们严格同步翻转。`start_lockstep_clocks` 就是干这件事的。它与回归公共件（`env_flag`、`build_vhdl_sources`、`run_surf_vhdl_test`）同住 `regression_utils.py`，共同构成「时钟 + 环境 + 源清单」的腿脚。

#### 4.2.2 核心流程

`start_lockstep_clocks(*signals, period_ns)`：

1. 定义内部协程 `drive()`：先把所有信号置 0。
2. 死循环里，`Timer(半周期)` → 所有信号置 1 → `Timer(半周期)` → 所有信号置 0。这样所有传入信号由**同一个协程、同一组 Timer** 驱动，沿严格对齐。
3. `cocotb.start_soon(drive())` 启动它。

`env_flag` / `env_sl` / `env_hex` / `env_float` / `env_int`：统一从环境变量读回泛型值（对应 u9-l2 讲过的「参数字典走两条通道：`_G` 键进 HDL 泛型，其余进 `extra_env` 由测试侧 `env_*` 读回」）。

`build_vhdl_sources()`：读取 `build/SRC_VHDL/{surf,ruckus}` 缓存（即 u9-l1 的 `make ... import` 产物），缺失时报错并提示先 import。

`run_surf_vhdl_test`：把测试文件、toplevel、参数、额外源清单等组装成一次 `cocotb_test.simulator.run(...)` 调用，是所有 pytest 包装函数的最终出口。

#### 4.2.3 源码精读

`start_lockstep_clocks` 的核心是一个驱动多信号的协程：

- [tests/common/regression_utils.py:72-92](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/common/regression_utils.py#L72-L92) —— 注释一针见血：「用一个协程驱动逻辑上共用的时钟，让 COMMON_CLK_G 测试真正复用同一根时钟，而不是两个周期相同却会相对漂移的振荡器」。

真实使用见一个 DSP 测试的 `TB.__init__`：

- [tests/dsp/generic/test_FirFilterSingleChannel.py:54](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/dsp/generic/test_FirFilterSingleChannel.py#L54) —— `start_lockstep_clocks(dut.clk, dut.S_AXI_ACLK, period_ns=5.0)`，数据时钟与 AXI-Lite 时钟由同一协程驱动；该文件方法学头第 21-24 行明确写了「DUT 处于 `COMMON_CLK_G=true`，故两路时钟 lockstep 驱动」。

`env_flag` 与源缓存：

- [tests/common/regression_utils.py:95-105](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/common/regression_utils.py#L95-L105) —— `env_flag`：环境变量缺失取默认值，否则归一化后只认 `1/true` 与 `0/false`。
- [tests/common/regression_utils.py:160-172](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/common/regression_utils.py#L160-L172) —— `build_vhdl_sources`：读 `build/SRC_VHDL/{surf,ruckus}`，缺失即抛错提示 `make ... import`。

风格指南把公共时钟也钉成硬规则——[tests/README.md:105-107](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/README.md#L105-L107)：`COMMON_CLK_G` 类被测件用 `start_lockstep_clocks()`，不要开两个独立的同周期时钟协程。

#### 4.2.4 代码实践

**实践目标**：体会「lockstep」相对于「两个独立时钟」的差异。

**操作步骤（源码阅读型实践）**：

1. 读 `start_lockstep_clocks` 的 `drive()` 协程，确认它在一个循环里**对同一个 `Timer` 等待后，依次给所有信号赋值**，而非每个信号各自 `await`。
2. 打开 [tests/dsp/generic/test_FirFilterMultiChannel.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/dsp/generic/test_FirFilterMultiChannel.py)（同样使用 lockstep 的多通道 FIR 测试），对照它的方法学头「Timing」段，看它如何描述「按对齐的 wrapper 可见时延而非硬编码拍数来等待」。
3. **思想实验**：若把 `start_lockstep_clocks(dut.clk, dut.S_AXI_ACLK, ...)` 换成两行 `cocotb.start_soon(Clock(dut.clk, 5.0, ...).start())` / `cocotb.start_soon(Clock(dut.S_AXI_ACLK, 5.0, ...).start())`，两个协程各自从自己的 `Timer` 起步，由于协程调度顺序非确定，两个上升沿会随机错开 0~若干 delta cycle。

**需要观察的现象**：理解「两个同周期时钟」≠「一个公共时钟」。

**预期结果**：你能解释为什么 COMMON_CLK_G 的时序契约要求两路沿**同源**，而非仅仅是周期相同。

**待本地验证**：lockstep 是仿真建模选择，不影响 RTL 综合，故无需在硬件验证；若本地跑 DSP 回归，可观察 lockstep 下波形两路沿完全重合。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `start_lockstep_clocks` 用**一个**协程驱动**多个**信号，而不是每个信号一个协程？
**答案**：一个协程里对同一组 `Timer` 等待后再统一赋值，能保证多个信号在同一调度点翻转，沿严格对齐；多协程则各自独立调度，沿会相对漂移。

**练习 2**：`build_vhdl_sources()` 缺失缓存时报什么错？该怎么修？
**答案**：抛 `FileNotFoundError("Missing imported HDL sources. Run make MODULES=\"$PWD\" import first.")`。修法是先跑一次 `make MODULES="$PWD" import` 生成 `build/SRC_VHDL/{surf,ruckus}` 缓存（见 u9-l1）。

---

### 4.3 子系统帧构造器与记分板

#### 4.3.1 概念说明

有了「手」和「腿」，还缺「嘴」——把一帧合法的协议字节流拼出来、喂进 DUT、再把出来的帧核对掉。SURF 的做法是：**把可复用的协议帧构造器收进每个子系统目录下的 `*_test_utils.py`**（风格指南 [tests/README.md:15-21](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/README.md#L15-L21) 明确要求）。

这里有一条贯穿全部以太网工具的字节序约定（承接 u6-l1）：**扁平数据通路把帧的第一字节放在数据字的最低有效字节通道**。于是 `pack_bytes` 把字节按 `index` 从低位往高位塞；MAC 在 wire 上是 big-endian 的 `aa:bb:cc:dd:ee:ff`，但塞进数据字后变成了「`ff ee dd cc bb aa`」这样的低位在前形态。这条约定还反过来影响 MAC 配置寄存器：往 `localMac` 这类配置口写值时，要先把 wire 序 MAC 字节反转（`mac_config_word_from_wire`）。

帧构造器是**分层**的：

- 底座层 `ethmac_test_utils.py`：字节打包、internet 校验和、以太网/IPv4/UDP/PAUSE 帧、扁平 EMAC 源/汇驱动。
- 上层 `ipv4_test_utils.py`：直接 `from ...ethmac_test_utils import build_ethernet_frame, build_ipv4_header, ...`，在底座上叠 ARP/ICMP/IGMP 帧，**绝不重写**校验和与字节打包。
- 协议层 `srp_test_utils.py`：把 SRPv3 请求建模成 `SrpV3Request` 数据类，提供 `srpv3_header`/`srpv3_frame` 构造与 `assert_srpv3_response` 记分板。

#### 4.3.2 核心流程

**构造一帧 IPv4/UDP 以太网帧**（`build_ipv4_udp_frame`）自下而上：

1. `build_udp_header`：拼 UDP 头（源/目的端口、长度），并按「伪头部（src IP + dst IP + 0x00 + 协议号 + UDP 长度）+ UDP 头 + payload」计算 UDP 校验和。
2. `build_ipv4_header`：拼 20 字节 IPv4 头（version/IHL=0x45、总长度、TTL、协议号、地址），用 internet 校验和填校验位。
3. `build_ethernet_frame`：目的 MAC + 源 MAC + EtherType（0x0800）+ 上述 payload。
4. （可选）`pad_ethernet_frame_to_min_size`：不足 60 字节补零到以太网最小帧长。

**internet 校验和**（IPv4/UDP/ICMP/IGMP 共用）是标准的反码和：

\[ \text{checksum} = \sim\left( \text{fold}_{16}\!\left( \sum_{i} w_i \right) \right) \]

其中 \(\text{fold}_{16}\) 把高于 16 位的进位反复折回低位，奇数字节数末尾补一个 0 字节。

**记分板**（`assert_srpv3_response`）核对三段：响应头 5 字（与请求头一致）、payload（与预期逐字相等）、footer（按掩码检查错误位），并校验 TDEST/TKEEP/TUSER 侧带。

#### 4.3.3 源码精读

字节序底座（ethmac_test_utils.py）：

- [tests/ethernet/EthMacCore/ethmac_test_utils.py:180-182](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/EthMacCore/ethmac_test_utils.py#L180-L182) —— `keep_mask`：每个有效字节通道对应 TKEEP 的一位。
- [tests/ethernet/EthMacCore/ethmac_test_utils.py:185-191](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/EthMacCore/ethmac_test_utils.py#L185-L191) —— `pack_bytes`：把第一字节塞进**最低**字节通道（小端排列），注释点明这就是 SURF EMAC 数据通路的约定。
- [tests/ethernet/EthMacCore/ethmac_test_utils.py:194-206](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/EthMacCore/ethmac_test_utils.py#L194-L206) —— `payload_from_beat` / `payload_from_beats`：按 TKEEP 把一拍/多拍数据解包回字节流，是收向记分板的逆操作。
- [tests/ethernet/EthMacCore/ethmac_test_utils.py:209-234](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/EthMacCore/ethmac_test_utils.py#L209-L234) —— `frame_deats_from_bytes`：把字节流切成 `beat_bytes` 一拍的 `EmacBeat` 列表，首拍带 SOF、末拍带 TLAST/EOFE。

校验和与帧构造：

- [tests/ethernet/EthMacCore/ethmac_test_utils.py:258-269](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/EthMacCore/ethmac_test_utils.py#L258-L269) —— `internet_checksum`：16 位字累加 + 进位回卷 + 取反，奇数补零。
- [tests/ethernet/EthMacCore/ethmac_test_utils.py:272-273](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/EthMacCore/ethmac_test_utils.py#L272-L273) —— `build_ethernet_frame`：`dst_mac + src_mac + eth_type + payload`。
- [tests/ethernet/EthMacCore/ethmac_test_utils.py:282-304](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/EthMacCore/ethmac_test_utils.py#L282-L304) —— `build_ipv4_header`：拼最小 IPv4 头并填校验和（可用 `checksum_override` 注入错误值做负面测试）。
- [tests/ethernet/EthMacCore/ethmac_test_utils.py:307-331](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/EthMacCore/ethmac_test_utils.py#L307-L331) —— `build_udp_header`：含伪头部的 UDP 校验和计算。
- [tests/ethernet/EthMacCore/ethmac_test_utils.py:334-366](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/EthMacCore/ethmac_test_utils.py#L334-L366) —— `build_ipv4_udp_frame`：组装完整以太网+IPv4+UDP 帧。

> 顺带一提 MAC 配置字节的反转——[tests/ethernet/EthMacCore/ethmac_test_utils.py:247-251](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/EthMacCore/ethmac_test_utils.py#L247-L251)：`mac_config_word_from_wire` 把 wire 序 MAC 反转成「最低字节通道在前」，与数据通路一致，故驱动 `localMac` 等配置口要用它。

分层复用（ipv4_test_utils.py 直接 import 底座）：

- [tests/ethernet/IpV4Engine/ipv4_test_utils.py:16-23](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/IpV4Engine/ipv4_test_utils.py#L16-L23) —— 从 ethmac 复用 `build_ethernet_frame` / `build_ipv4_header` / `build_udp_header` / `internet_checksum` 等，自身只叠 ARP/ICMP/IGMP 逻辑，绝不重复造字节序与校验和。

SRPv3 协议模型（srp_test_utils.py）：

- [tests/protocols/srp/srp_test_utils.py:33-61](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/protocols/srp/srp_test_utils.py#L33-L61) —— `SrpV3Request`：把一次 SRPv3 请求建模成 `frozen` 数据类；`req_size` 属性实现 u5-l3 讲过的「reqSize = 字节数 − 1」；`response_header` 复用 `srpv3_header` 生成期望响应头。
- [tests/protocols/srp/srp_test_utils.py:114-147](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/protocols/srp/srp_test_utils.py#L114-L147) —— `FlatSrpAxis.send_packed_words`：把 32 位字列表按 `data_bytes/4` 个字一拍塞进 AXI-Stream，首拍 TUSER 写 `0x2`（SSI 的 SOF 侧带），末拍 TLAST=1，每拍都用 `wait_sampled_ready` 等握手（即 4.1 的原语）。
- [tests/protocols/srp/srp_test_utils.py:195-221](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/protocols/srp/srp_test_utils.py#L195-L221) —— `srpv3_header`：按 SRPv3 帧格式打包 5 个头字（version/opCode/spare/ignore_mem_resp/prot/timeout 拼成 word0，tid、64 位 addr 拆两字、req_size）。
- [tests/protocols/srp/srp_test_utils.py:239-258](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/protocols/srp/srp_test_utils.py#L239-L258) —— `assert_srpv3_response`：记分板，核对头/payload/footer/侧带。

#### 4.3.4 代码实践

**实践目标**：用 `ethmac_test_utils` 构造一帧 IPv4/UDP 以太网帧，并说明 EthMacRx 会对其中的哪些字段做过滤或校验（承接 u6-l1）。

**操作步骤**：

1. 在本地仓库根目录的 Python 里（或一个临时脚本/REPL）执行下面的**示例代码**（非项目原有代码，仅演示调用）：

   ```python
   from tests.ethernet.EthMacCore.ethmac_test_utils import (
       build_ipv4_udp_frame, pack_bytes, frame_beats_from_bytes,
   )

   frame = build_ipv4_udp_frame(
       dst_mac=0x020000000000, src_mac=0x021111111111,
       src_ip="192.168.1.100", dst_ip="192.168.1.1",
       src_port=1234, dst_port=5678,
       payload=b"\x01\x02\x03\x04",
   )
   print(frame.hex())                 # 完整以太网帧字节（wire 序，big-endian MAC/IP）
   beats = frame_beats_from_bytes(frame, beat_bytes=16)
   print([(hex(b.data), hex(b.keep), b.sof, b.last) for b in beats])
   ```

2. 逐字节定位以下字段在帧里的偏移（从 0 计）：目的 MAC（0–5）、源 MAC（6–11）、EtherType（12–13，应为 `08 00`）、IPv4 头（14 起）、UDP 头、payload。

3. 对照 u6-l1 讲过的 EthMacRx 行为，标注哪些字段会被 RX 处理：

   - **目的 MAC 过滤**：RX 末端按本机/组播/广播 MAC 过滤；若 `dst_mac` 不匹配本机且非广播/组播，帧被丢弃。
   - **EtherType 摘流**：`0x0800`（IPv4）会被识别，但 EthMacCore 只做 Bypass 摘流与 IPv4 识别，把完整帧交给上层 `IpV4Engine`。
   - **校验和卸载**：`EthMacRxCsum` 边走边算 IP/TCP/UDP 反码和，把结果写进末拍 TUSER 的 `IpErr/TcpErr/UdpErr`（对应 `EmacBeat.iperr/tcperr/udperr` 字段），出错同时拉 `EOFE`。因此 `build_ipv4_header` / `build_udp_header` 算出的校验和必须正确，否则 RX 会在 TUSER 里置错。

**需要观察的现象**：

- `frame.hex()` 里目的 MAC 显示为 `02 00 00 00 00 00`（wire 序），但 `beats[0].data` 的最低字节通道是 `0x02`——印证「第一字节落在最低有效字节通道」。
- 故意把 UDP 校验和改错（传 `udp_checksum_override=0xDEAD`）再喂帧，预期 RX 末拍 `udperr=1`、`eofe=1`。

**预期结果**：你能列出至少三类会被 EthMacRx 处理的字段（目的 MAC、EtherType、IP/UDP 校验和），并指出它们的字节在数据字里的位置受小端字节通道约定支配。

**待本地验证**：上述 Python 调用需要 `tests/` 在 `PYTHONPATH` 中（仓库以 `tests` 为顶层包）；若直接运行报导入错误，可在仓库根用 `python -c "..."` 或在已激活 `.venv` 的环境里运行。实际「喂帧看 TUSER 错误位」需配合 EthMacCore 的 cocotb wrapper 仿真，属完整回归流程，标注待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ipv4_test_utils.py` 要从 `ethmac_test_utils` import `internet_checksum`，而不是自己写一份？
**答案**：风格指南要求复用最近的 helper，避免重复实现协议代码。internet 校验和是 IPv4/UDP/ICMP/IGMP 共用的同一算法，集中维护一份既保证一致、又便于一处修复全局生效。

**练习 2**：`build_ipv4_udp_frame` 里 UDP 校验和的「伪头部」包含哪些字段？为什么需要它？
**答案**：伪头部含源 IP、目的 IP、一个 0 字节、协议号（0x11）、UDP 长度。UDP 头本身不含 IP 信息，伪头部让校验和覆盖到「这条 UDP 属于哪对 IP 端点」，防止错配的 IP 分组被当作合法 UDP 接收。

**练习 3**：`FlatSrpAxis.send_packed_words` 为什么在首拍把 TUSER 写成 `0x2`？
**答案**：`0x2` 是 SSI 在 TUSER 里编码的 SOF（帧起始）位（承接 u5-l1）。SRPv3 帧承载在 SSI 流上，首拍必须标 SOF 让接收端识别帧边界；末拍则靠 TLAST=1 标 EOF。

## 5. 综合实践

把本讲三个模块串起来，完成一个「读寄存器 → 构造协议帧 → 收响应 → 记分板核对」的最小闭环（源码阅读 + 思想设计型）：

1. **手**：阅读 [tests/protocols/srp/test_SrpV3AxiLite.py:46-84](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/protocols/srp/test_SrpV3AxiLite.py#L46-L84)，画出 `TB` 如何建 `FlatSrpAxis`（SSI 侧）与 `AxiLiteRam`（AXI-Lite 侧），并在 `reset()` 里用 `init_source`/`init_sink` 把流口置静止。
2. **腿**：确认该测试的 pytest 包装函数最终走 `run_surf_vhdl_test`（u9-l1），并理解它依赖 `build_vhdl_sources` 读 import 缓存。
3. **嘴**：在测试体里找一个「发 WRITE 帧再收响应」的协程，确认它依次调用 `srpv3_frame(SrpV3Request(opcode=SRP_WRITE, ...))` 构造请求、`axis.send_packed_words(...)` 推流、`axis.recv_response(...)` 收响应、`assert_srpv3_response(...)` 核对头/payload/footer。
4. **设计题**：若你要为新模块写一个 `*_test_utils.py`，请按本讲的分层原则回答三问——(a) 哪些属于「字节序/校验和底座」应放进 ethmac 或新底座？(b) 哪些是「本协议独有帧」应放本子系统？(c) 收向记分板要核对哪几类字段（payload / TKEEP / TLAST / TUSER 侧带 / 响应码）？

> 这个闭环正好是下一篇 u9-l4（PyRogue 设备模型）的对照面：RTL 寄存器布局既要被这里的 AXI-Lite 读写验证，也要被 PyRogue 的 `RemoteVariable` 逐字段镜像，三者（RTL 偏移、测试 helper、PyRogue 变量）必须一致。

## 6. 本讲小结

- `tests/axi/utils.py` 收敛了三个全仓库 AXI 原语：`axil_read_u32`/`axil_write_u32`（断言 OKAY + little-endian 字节序）、`wait_sampled_ready`（沿→沉降→读 READY 的采样握手）。
- 采样握手的核心是「传输在 VALID∧READY 同时为 1 的那个沿完成」，源必须把 beat 稳住到该沿之后；GHDL 的 delta cycle 调度要求沿后加一小段 `Timer` 沉降。
- `COMMON_CLK_G=true` 的被测件必须用 `start_lockstep_clocks` 用**一个协程驱动多根时钟线**，严禁开两个同周期的独立时钟协程，否则沿会相对漂移、破坏共用时钟契约。
- 子系统帧构造器按底座（ethmac：字节序/校验和/以太网帧）→ 高层（ipv4：ARP/ICMP/IGMP）→ 协议（srp：SRPv3 请求模型 + 记分板）分层，下层的字节序与校验和只写一遍、上层只叠协议逻辑。
- 贯穿全仓库的字节序约定：扁平数据通路第一字节落在最低有效字节通道（小端排列），它还反过来要求 MAC 配置寄存器用 `mac_config_word_from_wire` 反转字节序。
- 风格指南（`tests/README.md`）把「helper 放置位置、采样握手、公共时钟」钉成硬规则，复用优先于复制。

## 7. 下一步学习建议

- **u9-l4 PyRogue 设备模型**：本讲的 AXI-Lite 寄存器读写与寄存器偏移，正是 PyRogue `RemoteVariable` 要镜像的同一份布局。建议对照 `python/surf/axi/_AxiVersion.py` 与 u3-l4 的 `AxiVersion.vhd` 偏移表，验证三者一致。
- **继续阅读源码**：
  - 想看更多帧构造器范式，读 `tests/protocols/packetizer/packetizer_test_utils.py`、`tests/protocols/batcher/batcher_test_utils.py`、`tests/protocols/pgp/pgp4/pgp4_test_utils.py`。
  - 想看 SSI beat/frame 帮助，读 `tests/protocols/ssi/ssi_test_utils.py`（风格指南特别点名）。
  - 想看 RAW 二层帧构造，读 `tests/ethernet/RawEthFramer/raw_eth_test_utils.py`。
- **动手**：仿照 `srp_test_utils.py` 的 `SrpV3Request` + `assert_srpv3_response` 模式，为你熟悉的一个协议核写一个最小的「请求模型 + 记分板」骨架（不必跑通，重在分层）。
