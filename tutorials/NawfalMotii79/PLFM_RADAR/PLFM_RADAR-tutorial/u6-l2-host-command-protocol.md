# 主机命令协议与 Opcode 映射

## 1. 本讲目标

学完本讲后，你应当能够：

- 用一句话说清楚「主机怎样给 FPGA 下一条命令、FPGA 怎样把配置回传给主机」；
- 看懂 `RadarProtocol.build_command` 如何把 `{opcode, addr, value}` 拼成一个 32 位字、再拆成 4 个字节；
- 看懂 `RadarProtocol.parse_status_packet` 如何从 26 字节状态包里拆出 6 个 32 位字、再按位域还原出雷达模式、CFAR 阈值、AGC 指标、自测试结果；
- 把 Python 的 `Opcode` 枚举与 Verilog 的 `case(usb_cmd_opcode)` 分支一一对应起来，理解这条「跨层硬契约」为何是整个系统的命脉；
- 独立构造一条「设置 CFAR alpha」命令，并指出 AGC 饱和计数在状态包里的精确比特位置。

## 2. 前置知识

本讲是 u6-l1（USB 数据接口）的直接下篇。在进入命令协议前，先回顾几个关键事实：

- **两个方向，两种包**：USB 是双向总线。FPGA→主机方向走的是 11 字节**数据包**（`0xAA` 头 + 距离/多普勒/检测字段 + `0x55` 尾）和 26 字节**状态包**（`0xBB` 头 + 6 个 32 位字 + `0x55` 尾）；主机→FPGA 方向走的是 4 字节**命令**。本讲聚焦「命令怎么拼」和「状态包怎么拆」。
- **字节序**：本项目所有多字节字段一律**大端（big-endian，MSB first）**。这在 Python 端用 `struct` 的格式串 `">I"`（`>` 表示大端，`I` 表示 4 字节无符号整型）和 `">H"`（2 字节）实现，在 Verilog 端用「先发 `[31:24]`，再发 `[23:16]`，……」的字节选择器实现。两端必须同序，否则数值会错乱。
- **位域（bit field）**：一个 32 位字里常常塞好几个字段，例如 `[31:28]` 放增益、`[19:12]` 放饱和计数。提取某个字段的标准套路是「右移到最低位、再与掩码相与」，写作 `(word >> shift) & mask`。本讲会反复用到。
- **Opcode（操作码）**：命令的第一个字节，用来告诉 FPGA「这条命令要改哪个寄存器」。你可以把它理解成函数名：`opcode=0x23` 大致等价于调用 `set_cfar_alpha(value)`。

如果你对 4 字节命令是怎么从 USB 字节流里拼出来的（Read FSM、`cmd_valid` 脉冲、toggle-CDC 跨时钟域）还不太清楚，建议先回到 u6-l1 复习「主机读路径」一节；本讲默认你已经知道 `usb_cmd_opcode / usb_cmd_addr / usb_cmd_value` 这三组信号是怎么来的。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的地方 |
|------|------|----------------|
| [9_Firmware/9_3_GUI/radar_protocol.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py) | 纯 Python 协议层：命令构造、数据/状态包解析 | `Opcode` 枚举、`build_command`、`parse_status_packet`、`StatusResponse` |
| [9_Firmware/9_2_FPGA/radar_system_top.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v) | FPGA 顶层：命令译码 `case` 表与 `host_*` 寄存器 | `host_*` 寄存器声明、复位默认值、`case(usb_cmd_opcode)` |
| [9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v) | FT2232H USB 接口：状态字拼装（验证用） | `status_words[0..5]` 的位域拼装 |

> 说明：本讲指定的关键源码是前两个文件。第三个文件（USB 接口）用于验证「Python 拆包」与「FPGA 打包」两侧位域一致，是理解状态包布局不可或缺的另一半真相。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**命令构造** → **状态解析** → **opcode 映射**。三者合起来就是主机与 FPGA 之间的完整「请求—回读」闭环。

---

### 4.1 命令构造：`build_command` 的 32 位字拼装

#### 4.1.1 概念说明

主机对 FPGA 的全部控制——切模式、设阈值、开 CFAR、触发自测试——都被压缩成同一种格式：**一条 4 字节命令**。为什么不给每个功能设计独立的包格式？因为 USB 读路径（u6-l1）用一个固定 FSM 逐字节收齐 4 字节就产生一次 `cmd_valid` 脉冲，统一格式让硬件极简：收到 4 字节 → 跨时钟域 → 查 `case` 表 → 写寄存器。复杂度全部交给 opcode 的语义去承担。

这 4 字节在逻辑上被切成三段：`opcode`（做什么）、`addr`（对哪个子地址做，本项目多数命令不用）、`value`（写成多少）。注意 Python 接口把 `addr` 放在参数列表最后并给默认值 0，因为它很少被用到。

#### 4.1.2 核心流程

命令构造的本质是一次「移位 + 或运算」的位拼装，把三个字段塞进同一个 32 位整数，再用大端序拆成 4 字节：

```
  31      24 23      16 15            0
 ┌──────────┬──────────┬──────────────┐
 │  opcode  │   addr   │    value     │
 └──────────┴──────────┴──────────────┘
       │         │          │
       │         │          └─ 16 位数值（如阈值、alpha、计数）
       │         └──────────── 8 位子地址（多数命令为 0）
       └────────────────────── 8 位操作码（决定写哪个寄存器）

word = (opcode << 24) | (addr << 16) | value
bytes = 大端拆 word → [opcode, addr, value_hi, value_lo]
```

大端拆字后，第 0 字节永远是 opcode——这正是 USB 读 FSM 收到的**第一个**字节，也是 Verilog `case(usb_cmd_opcode)` 直接拿来查表的字节，两端在「opcode 优先」这一点上天然对齐。

#### 4.1.3 源码精读

构造逻辑全部集中在一个静态方法里：

[9_Firmware/9_3_GUI/radar_protocol.py:168-175](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L168-L175) —— 把 `opcode/addr/value` 拼成 32 位字，再用 `struct.pack(">I", word)` 大端拆成 4 字节返回。注意每个字段都先 `& 0xFF`（或 `& 0xFFFF`）做掩码，防止调用方传入越界值污染相邻字段。

这条命令随后会经 `FT2232HConnection.write` / `FT601Connection.write` 直接送进 USB（见 u6-l1）。文件顶部的协议注释也写明了 RX 命令格式，作为「单一真相」：

[9_Firmware/9_3_GUI/radar_protocol.py:15-16](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L15-L16) —— 注释声明「主机→FPGA 命令为 4 字节，顺序接收 `{opcode, addr, value_hi, value_lo}`」，与上面的位拼装一致。

#### 4.1.4 代码实践

**目标**：亲手用 `build_command` 构造一条命令，验证它的字节布局。

**操作步骤**（源码阅读 + 本地可选运行）：

1. 在仓库根目录打开 Python，导入协议层：

   ```python
   # 示例代码（非项目原有）
   import sys
   sys.path.insert(0, "9_Firmware/9_3_GUI")
   from radar_protocol import RadarProtocol, Opcode

   cmd = RadarProtocol.build_command(Opcode.CFAR_ALPHA, 0x30)
   print(cmd.hex())          # 期望: 23000030
   print(list(cmd))          # 期望: [0x23, 0x00, 0x00, 0x30]
   ```

2. 对照位拼装公式手算一遍：`(0x23 << 24) | (0 << 16) | 0x30 = 0x23000030`，再大端拆成 `23 00 00 30`。

**需要观察的现象**：

- 第 0 字节 `0x23` 正是 `Opcode.CFAR_ALPHA` 的值，验证了「opcode 优先、占据命令首字节」。
- `addr=0` 占满第 1 字节，`value=0x30` 被拆成 `00 30` 占第 2、3 字节。
- 顺带留意：`0x30` 恰好等于 FPGA 上电默认的 `host_cfar_alpha`（见 4.3.3，复位值 `8'h30`，即 Q4.4 定点下的 3.0）。所以这条命令「写了等于没写」，但它验证了整条链路。

**预期结果**：`cmd.hex()` 输出 `23000030`。

> 待本地验证：若你未安装 `numpy`（`radar_protocol.py` 顶部 `import numpy as np`），上述 import 会失败。可先 `uv sync --group dev` 安装依赖，或单独 `pip install numpy` 后再试。

#### 4.1.5 小练习与答案

**练习 1**：用 `build_command` 构造一条「触发一次脉冲」的命令（opcode `0x02`，无 value）。期望字节序列是什么？

**答案**：`RadarProtocol.build_command(0x02, 0)` → `(0x02 << 24) | 0 = 0x02000000` → 大端拆为 `02 00 00 00`。

**练习 2**：如果调用方误把 `value=0x130`（超过 16 位）传进去，`build_command` 会输出什么？为什么不会污染 opcode 字节？

**答案**：输出 `cmd` 的 value 段为 `0x130 & 0xFFFF = 0x0130`，即字节 `... 01 30`。因为代码里有 `(value & 0xFFFF)` 掩码，高出的 `0x1` 被截断，不会窜进 addr 或 opcode。

---

### 4.2 状态解析：`parse_status_packet` 的 6 字段解析

#### 4.2.1 概念说明

命令是「主机→FPGA」的请求，状态包则是「FPGA→主机」的回读。它解决一个核心问题：**主机怎么知道 FPGA 当前的真实配置和工作状态？** 比如 CFAR 阈值到底被设成了多少？AGC 现在增益是几？自测试跑完了吗、各项通过没有？

状态包固定 26 字节：`0xBB` 头 + 6 个 32 位字（共 24 字节）+ `0x55` 尾。6 个字里塞了**全部**主机关心的回读量，一次性快照发出。设计上有两个关键决策：

- **请求驱动（status_request）**：状态包不是自发流式发送的，而是主机用 opcode `0xFF`（或 `0x31`）「按一下」才发一次。这样 USB 带宽主要留给数据包，状态回读按需进行。
- **位域压缩**：每个 32 位字都尽量塞满，比如 word 4 同时放了 AGC 的 4 个指标 + range_mode，避免状态包膨胀。

#### 4.2.2 核心流程

解析分两步：先按大端序把 24 字节重组成 6 个 32 位字，再对每个字按位域切分：

```
raw[0] == 0xBB ?  否 → 返回 None
raw[25] == 0x55 ? 否 → 返回 None

for i in 0..5:
    words[i] = struct.unpack(">I", raw, 1 + i*4)   # 大端重组

# 按位域切字段（右移 + 掩码）：
word0: threshold = w0 & 0xFFFF
       stream    = (w0 >> 19) & 0x07
       mode      = (w0 >> 22) & 0x03
word1: long_listen = w1 & 0xFFFF ;  long_chirp = (w1 >> 16) & 0xFFFF
word2: short_chirp = w2 & 0xFFFF ;  guard      = (w2 >> 16) & 0xFFFF
word3: chirps_per_elev = w3 & 0x3F ; short_listen = (w3 >> 16) & 0xFFFF
word4: range_mode         = w4 & 0x03
       agc_enable         = (w4 >> 11) & 0x01
       agc_saturation_cnt = (w4 >> 12) & 0xFF
       agc_peak_magnitude = (w4 >> 20) & 0xFF
       agc_current_gain   = (w4 >> 28) & 0x0F
word5: self_test_flags  = w5 & 0x1F
       self_test_detail = (w5 >> 8) & 0xFF
       self_test_busy   = (w5 >> 24) & 0x01
```

注意 word 4 是「最拥挤」的一个字——4 个 AGC 字段加 range_mode 挤在 32 位里，其中 `agc_saturation_count` 恰好占 8 位 `[19:12]`。

#### 4.2.3 源码精读

解析入口先做帧头/帧尾校验，再循环重组 6 个字：

[9_Firmware/9_3_GUI/radar_protocol.py:217-234](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L217-L234) —— 校验 `0xBB` 头与 `0x55` 尾，再用 `struct.unpack_from(">I", raw, 1 + i*4)` 大端读出 6 个 32 位字。任何一项校验失败都返回 `None`，交由上层（采集线程）把这段字节当残差丢弃。

然后是位域切分，AGC 指标在 word 4：

[9_Firmware/9_3_GUI/radar_protocol.py:250-256](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L250-L256) —— 从 word 4 切出 `range_mode / agc_enable / agc_saturation_count / agc_peak_magnitude / agc_current_gain`。注释里写明了每个字段的比特位置，与 FPGA 打包侧逐位对应（见下方验证）。

自测试结果在 word 5：

[9_Firmware/9_3_GUI/radar_protocol.py:257-261](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L257-L261) —— 切出 `self_test_flags`（5 位 PASS/FAIL）、`self_test_detail`（8 位诊断码）、`self_test_busy`（1 位忙标志）。

解析出的字段被装进 `StatusResponse` 数据类，字段命名与位宽注释一一对应：

[9_Firmware/9_3_GUI/radar_protocol.py:128-149](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L128-L149) —— `StatusResponse` 把 6 个字拆出的全部回读量集中存放，每个字段后的注释标明比特宽（如 `agc_saturation_count # 8-bit saturation count [7:0]`，指字段自身 8 位，位于 word 4 的 `[19:12]`）。

**验证：FPGA 打包侧的位域**。Python 拆包对不对，必须去 FPGA 打包侧核对。两套 USB 模块（FT601 与 FT2232H）的打包逻辑完全一致，以 FT2232H 为例：

[9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v:382-390](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L382-L390) —— word 4 拼装为 `{agc_current_gain[31:28], agc_peak_magnitude[27:20], agc_saturation_count[19:12], agc_enable[11], 9'd0[10:2], range_mode[1:0]}`，word 5 拼装为 `{7'd0, self_test_busy[24], 8'd0, self_test_detail[15:8], 3'd0, self_test_flags[4:0]}`。逐位对照即可确认 Python 的移位量与掩码完全正确。

#### 4.2.4 代码实践

**目标**：手工伪造一个状态包，验证解析结果与位域设计一致。

**操作步骤**：

1. 构造一个 word 4 里 `agc_saturation_count = 0xAB`、其余字段为 0 的状态包（示例代码）：

   ```python
   # 示例代码（非项目原有）
   import struct
   from radar_protocol import RadarProtocol, STATUS_HEADER_BYTE, FOOTER_BYTE

   sat = 0xAB
   word4 = (sat & 0xFF) << 12          # 放到 [19:12]
   raw = bytes([STATUS_HEADER_BYTE])
   raw += struct.pack(">I", 0)         # word0
   raw += struct.pack(">I", 0)         # word1
   raw += struct.pack(">I", 0)         # word2
   raw += struct.pack(">I", 0)         # word3
   raw += struct.pack(">I", word4)     # word4
   raw += struct.pack(">I", 0)         # word5
   raw += bytes([FOOTER_BYTE])
   assert len(raw) == 26

   sr = RadarProtocol.parse_status_packet(raw)
   print(hex(sr.agc_saturation_count))  # 期望: 0xab
   ```

**需要观察的现象**：

- 把 `0xAB` 放进 `[19:12]` 后，`parse_status_packet` 用 `(w4 >> 12) & 0xFF` 又把它取回 `0xab`，证明移位量无误。
- 试着把 `sat` 改成 `0x1AB`（超过 8 位），观察解析结果仍是 `0xab`——因为 FPGA 端 `status_agc_saturation_count` 本身只有 8 位，超出部分不会出现在这个字段里。

**预期结果**：`sr.agc_saturation_count == 0xab`，且 `sr.agc_current_gain / agc_peak_magnitude / agc_enable / range_mode` 全为 0。

> 待本地验证：运行环境同 4.1.4，需 `numpy`。

#### 4.2.5 小练习与答案

**练习 1**：状态包 word 0 的 `[31:24]` 字节是什么？为什么恒为 `0xFF`？

**答案**：是 `0xFF`（见 FPGA 打包 `{8'hFF, ...}`）。它是一个固定标记字节，方便主机在原始字节流里识别「这是一个状态包的开头字」。注意真正的帧头是 `0xBB`（在 word 0 之前），`0xFF` 只是 word 0 内部的填充/标记。

**练习 2**：若 `raw[0]` 是 `0xBB` 但 `raw[25]` 不是 `0x55`，`parse_status_packet` 返回什么？为什么必须同时校验头和尾？

**答案**：返回 `None`。同时校验头尾是为了抵抗「数据流里恰好出现 `0xBB` 字节」造成的误对齐——只有头尾都符合才认定这是一个完整、可信的状态包，否则当作噪声丢弃，避免把错误配置显示给用户。

---

### 4.3 Opcode 映射：Python 枚举与 Verilog case 表的一一对应

#### 4.3.1 概念说明

opcode 是整个跨层协议的「脊梁」。每条 4 字节命令的第 0 字节是 opcode，FPGA 顶层用一个 `case(usb_cmd_opcode)` 把它译成「写哪个 `host_*` 寄存器」；Python 侧用一个 `Opcode(IntEnum)` 给同一个数字起人类可读的名字。**这两张表必须逐项一致**——任何一侧新增、删除或改号，另一侧不动就会产生「主机以为在调 CFAR，FPGA 其实改了 MTI」的隐性故障。

这就是为什么 Python 的 `Opcode` 枚举文档串里直接写了一句硬话：「must match `radar_system_top.v` `case(usb_cmd_opcode)`」。这条「跨层硬契约」是 u11-l3（跨层契约测试）要自动校验的对象，本讲先建立直觉。

#### 4.3.2 核心流程

命令从字节到寄存器值的全链路如下（u6-l1 已讲过前半段，这里收尾）：

```
主机: build_command(opcode, value, addr)
        └─ 4 字节 [opcode, addr, value_hi, value_lo]  (USB 写)
FPGA USB 模块: Read FSM 收齐 4 字节 → 产生 usb_cmd_valid 脉冲
                (ft601_clk 域)
顶层 toggle-CDC: cmd_valid 翻转 → 3 级同步 → 边沿检测  (→ clk_100m 域)
顶层命令译码: case(usb_cmd_opcode)
                0x23 : host_cfar_alpha <= usb_cmd_value[7:0]
                ...
        └─ 写入 host_* 配置寄存器 (clk_100m 域)
各 DSP 子模块: 实时读取 host_* 寄存器作为工作参数
```

回读则是反向：`0xFF` 命令 → `host_status_request` 脉冲 → USB 模块把 `host_*` 当前值打包成 6 字状态包 → 主机 `parse_status_packet` 还原。

#### 4.3.3 源码精读

**Python 侧：Opcode 枚举**。枚举按功能分组编号，每个值后面都有注释说明它写哪个寄存器：

[9_Firmware/9_3_GUI/radar_protocol.py:53-103](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L53-L103) —— `Opcode(IntEnum)`。文档串（54-65 行）直接抄录了 FPGA 真值表并标了 Verilog 行号，是两侧同步的「契约文档」。注意 `CFAR_ALPHA = 0x23`、`SELF_TEST_TRIGGER = 0x30`、`STATUS_REQUEST = 0xFF` 等关键编号。

**Verilog 侧：命令译码 `case` 表**。这是整个映射的硬件真相：

[9_Firmware/9_2_FPGA/radar_system_top.v:949-999](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L949-L999) —— `case (usb_cmd_opcode)` 把每个 opcode 译成对某个 `host_*` 寄存器的赋值，数据源是跨域过来的 `usb_cmd_value`。例如 `8'h23: host_cfar_alpha <= usb_cmd_value[7:0];`、`8'h30: host_self_test_trigger <= 1'b1;`。

**Verilog 侧：复位默认值**。每个 `host_*` 寄存器在复位时被赋予一个「安全默认值」，这些默认值决定了上电时的雷达行为：

[9_Firmware/9_2_FPGA/radar_system_top.v:911-944](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L911-L944) —— 复位块。注意 `host_cfar_alpha <= 8'h30`（即上面命令实践的默认值 3.0）、`host_radar_mode <= 2'b01`（默认自动扫描）、CFAR/MTI/AGC 默认全部关闭（向后兼容旧上位机）。

下表把三处真相（Python 枚举、Verilog case、复位默认值）合并，给出完整的 opcode 映射。**只列本系统实际存在的 opcode，未在 case 表出现的编号不会做任何事**（落入 `default: ;` 空分支）。

| Opcode | Python 名 | 目标寄存器（Verilog） | value 位宽 | 复位默认 | 说明 |
|--------|-----------|------------------------|------------|----------|------|
| `0x01` | `RADAR_MODE` | `host_radar_mode` | `[1:0]` | `2'b01` | 雷达模式（00 直通/01 自动/10 单 chirp） |
| `0x02` | `TRIGGER_PULSE` | `host_trigger_pulse` | — | `0` | **自清零脉冲**，触发一次 |
| `0x03` | `DETECT_THRESHOLD` | `host_detect_threshold` | `[15:0]` | `10000` | 简单门限阈值 |
| `0x04` | `STREAM_CONTROL` | `host_stream_control` | `[2:0]` | `3'b111` | 数据流使能掩码 |
| `0x10` | `LONG_CHIRP` | `host_long_chirp_cycles` | `[15:0]` | `3000` | 长 chirp 样本数 |
| `0x11` | `LONG_LISTEN` | `host_long_listen_cycles` | `[15:0]` | `13700` | 长监听周期 |
| `0x12` | `GUARD` | `host_guard_cycles` | `[15:0]` | `17540` | 保护间隔 |
| `0x13` | `SHORT_CHIRP` | `host_short_chirp_cycles` | `[15:0]` | `50` | 短 chirp 样本数 |
| `0x14` | `SHORT_LISTEN` | `host_short_listen_cycles` | `[15:0]` | `17450` | 短监听周期 |
| `0x15` | `CHIRPS_PER_ELEV` | `host_chirps_per_elev` | `[5:0]` | `32` | **受钳制**，见下文 |
| `0x16` | `GAIN_SHIFT` | `host_gain_shift` | `[3:0]` | `0` | 数字增益移位 |
| `0x20` | `RANGE_MODE` | `host_range_mode` | `[1:0]` | `2'b00` | 距离模式（预留） |
| `0x21` | `CFAR_GUARD` | `host_cfar_guard` | `[3:0]` | `2` | CFAR 保护单元 |
| `0x22` | `CFAR_TRAIN` | `host_cfar_train` | `[4:0]` | `8` | CFAR 训练单元 |
| `0x23` | `CFAR_ALPHA` | `host_cfar_alpha` | `[7:0]` | `0x30` | CFAR 门限系数（Q4.4） |
| `0x24` | `CFAR_MODE` | `host_cfar_mode` | `[1:0]` | `2'b00` | CA/GO/SO |
| `0x25` | `CFAR_ENABLE` | `host_cfar_enable` | `[0]` | `0` | CFAR 使能 |
| `0x26` | `MTI_ENABLE` | `host_mti_enable` | `[0]` | `0` | MTI 使能 |
| `0x27` | `DC_NOTCH_WIDTH` | `host_dc_notch_width` | `[2:0]` | `0` | DC 陷波宽度 |
| `0x28` | `AGC_ENABLE` | `host_agc_enable` | `[0]` | `0` | AGC 使能 |
| `0x29` | `AGC_TARGET` | `host_agc_target` | `[7:0]` | `200` | AGC 目标峰值 |
| `0x2A` | `AGC_ATTACK` | `host_agc_attack` | `[3:0]` | `1` | AGC 增益下降步长 |
| `0x2B` | `AGC_DECAY` | `host_agc_decay` | `[3:0]` | `1` | AGC 增益上升步长 |
| `0x2C` | `AGC_HOLDOFF` | `host_agc_holdoff` | `[3:0]` | `4` | AGC 增益上升保持帧数 |
| `0x30` | `SELF_TEST_TRIGGER` | `host_self_test_trigger` | — | `0` | **自清零脉冲**，触发自测试 |
| `0x31` | `SELF_TEST_STATUS` | `host_status_request` | — | `0` | 自测试回读（状态请求别名） |
| `0xFF` | `STATUS_REQUEST` | `host_status_request` | — | `0` | **自清零脉冲**，请求状态包 |

**两类特殊 opcode 值得单独看**：

第一类是**自清零脉冲**（`0x02 / 0x30 / 0x31 / 0xFF`）。它们写到寄存器里的是 `1'b1`，但复位块里每一拍都把这几个寄存器清回 `1'b0`：

[9_Firmware/9_2_FPGA/radar_system_top.v:946-948](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L946-L948) —— `host_trigger_pulse / host_status_request / host_self_test_trigger` 每个时钟拍都被清零，只有在 `case` 命中那一拍为 1。效果上就是一个「单拍脉冲」，用来触发一次性动作而不必主机再发「关闭」命令。

第二类是**受钳制的 `0x15`（CHIRPS_PER_ELEV）**。Doppler 处理用固定的「16 长 + 16 短 = 32」双 16 点 FFT 架构（见 u4-l4），chirp 数改了会让 Doppler 累加错乱，所以硬件强行钳制：

[9_Firmware/9_2_FPGA/radar_system_top.v:961-975](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L961-L975) —— 若主机写入值 `>32` 或 `==0`，强制钳到 `DOPPLER_FRAME_CHIRPS=32` 并置 `chirps_mismatch_error`；写 1..31 会放行但仍置错误标志，只有恰好写 32 才清错误。这是「主机请求」与「硬件能力」谈判的典型例子。

#### 4.3.4 代码实践

**目标**：完成规格指定的实践——构造「设置 CFAR alpha 为 0x30」的命令字节序列，并指出 status 包 word 4 里 `agc_saturation_count` 的比特位。

**操作步骤**：

1. 构造命令（示例代码）：

   ```python
   # 示例代码（非项目原有）
   from radar_protocol import RadarProtocol, Opcode
   cmd = RadarProtocol.build_command(Opcode.CFAR_ALPHA, 0x30)
   # Opcode.CFAR_ALPHA == 0x23, addr 默认 0
   ```

2. 手算：`(0x23 << 24) | (0x00 << 16) | 0x0030 = 0x23000030`，大端拆 4 字节。

3. 查表确认 `agc_saturation_count` 在 status 包 word 4 的位置。

**需要观察的现象**：

- 命令字节序列为 `23 00 00 30`（hex 写作 `23000030`）。第 0 字节 `0x23` = CFAR_ALPHA；末两字节 `00 30` = value `0x30`。
- status 包 word 4 中，`agc_saturation_count` 占据 **bit [19:12]**（共 8 位）。证据有三处一致：
  - Python 解析：`(words[4] >> 12) & 0xFF`（radar_protocol.py:254）；
  - Python 数据类注释：`agc_saturation_count  # 8-bit saturation count`（radar_protocol.py:148）；
  - FPGA 打包：`status_words[4] <= {..., status_agc_saturation_count, ...}` 紧跟在 `[27:20]` 的 peak 之后、`[11]` 的 enable 之前（usb_data_interface_ft2232h.v:384），正落在 `[19:12]`。

**预期结果**：

- 命令字节序列：`23000030`（即 `bytes([0x23, 0x00, 0x00, 0x30])`）。
- `agc_saturation_count`：word 4 的 **bit 19 到 bit 12**（`[19:12]`），8 位无符号。

> 待本地验证：可在 Python 里实际执行 `RadarProtocol.build_command(Opcode.CFAR_ALPHA, 0x30).hex()` 对照。

#### 4.3.5 小练习与答案

**练习 1**：主机想「打开 MTI 并把 CFAR 切到 SO 模式」，需要发哪两条命令？value 各是多少？

**答案**：第一条 `build_command(Opcode.MTI_ENABLE, 0x01)`（打开 MTI，`host_mti_enable <= value[0]`，bit0=1）；第二条 `build_command(Opcode.CFAR_MODE, 0x02)`（SO 模式，查 cfar_ca 的模式编码 00=CA/01=GO/10=SO，故 value=2）。两条命令都还要配合 `CFAR_ENABLE=1` 才会真正生效。

**练习 2**：为什么 `0x02 / 0x30 / 0xFF` 这类命令不需要主机再发一条「关闭」命令？如果忘了自清零设计、把它们改成普通电平寄存器，会出现什么故障？

**答案**：因为复位块每拍把它们清回 0（radar_system_top.v:946-948），命中 `case` 那一拍为 1、下一拍自动回 0，天然形成单拍脉冲。若改成电平寄存器，一次触发后会一直保持 1，比如 `host_status_request` 一直为 1 会让 USB 模块每帧都发状态包、挤占数据带宽，或 `host_self_test_trigger` 一直为 1 导致自测试反复重启。

**练习 3**：主机写 `0x15` 把 `chirps_per_elev` 设成 16，FPGA 实际存多少？`chirps_mismatch_error` 是什么状态？

**答案**：16 落在 1..31 区间，不被钳制，`host_chirps_per_elev` 实际存 16；但因为 16 ≠ `DOPPLER_FRAME_CHIRPS(32)`，`chirps_mismatch_error` 被置 1（radar_system_top.v:973）。主机可通过状态包感知到这次配置与硬件架构不一致，Doppler 累加可能出错。

---

## 5. 综合实践

把三个最小模块串起来，完成一次完整的「请求—回读」闭环模拟。

**任务**：用 Python 协议层模拟「配置 CFAR 并验证回读」的完整流程，全程不接真实硬件。

**步骤**：

1. 构造三条命令，分别设置：CFAR alpha=`0x28`、CFAR 使能=`1`、CFAR 模式=`CA(0)`。打印每条命令的 hex。
2. 假装你是 FPGA，把这些配置「打包」进一个 status 包 word 0/word 4：因为 alpha 没有直接回读字段（status 包回读的是 `cfar_threshold` 即 `host_detect_threshold`，不是 alpha），所以改为把 `range_mode=1`、`agc_enable=1`、`agc_saturation_count=0x05`、`agc_peak_magnitude=0x40`、`agc_current_gain=0x3` 手工拼进 word 4。
3. 用 `parse_status_packet` 解析这个伪造包，断言 5 个 AGC/range 字段全部还原正确。
4. 写一段话回答：为什么 alpha 的当前值无法从 status 包直接读到？如果产品真的需要回读 alpha，要在哪两处（Python + Verilog）同时改动？（提示：扩展 opcode 表 + 在某个 status word 里挤出位域。）

**参考答案要点**：

- 第 1 步命令 hex 依次为 `23000028`、`25000001`、`24000000`。
- 第 2 步 word 4 = `(0x3 << 28) | (0x40 << 20) | (0x05 << 12) | (0x1 << 11) | 0x1`，即 `0x34005111`（range_mode=1 放 `[1:0]`）。解包后应得 `current_gain=3, peak=0x40, sat=0x05, enable=1, range_mode=1`。
- 第 4 步：status 包里没有 alpha 字段，因为 6 个字已被「模式/阈值/chirp 时序/AGC/自测试」占满，设计者没给 alpha 留回读位域。若要回读，需在 Verilog `status_words[]` 某个字的保留位（如 word 4 的 `[10:2]` 9 个保留位）里挤出 8 位放 alpha，并在 Python `parse_status_packet` 与 `StatusResponse` 同步新增字段——这正是「跨层硬契约」要两侧联动改的典型场景，也是 u14-l2（二次开发扩展点）会展开的内容。

> 待本地验证：综合实践可在 `uv run python` 交互环境下完成；若仅做源码阅读，也可手算 hex 与 word 4 值后对照。

## 6. 本讲小结

- 主机对 FPGA 的全部控制统一为 **4 字节命令**：`{opcode[31:24], addr[23:16], value[15:0]}`，由 `build_command` 大端拼装，opcode 永远是首字节。
- 状态回读统一为 **26 字节状态包**：`0xBB` + 6 个 32 位字 + `0x55`，由 `parse_status_packet` 大端重组后按位域切分；word 4 的 `[19:12]` 是 `agc_saturation_count`，word 5 是自测试结果。
- **opcode 映射是跨层硬契约**：Python `Opcode` 枚举与 Verilog `case(usb_cmd_opcode)` 必须逐项一致，Python 枚举文档串直接引用了 Verilog 真值表作为契约。
- 三类 opcode 行为不同：普通配置寄存器（多数）、自清零脉冲（`0x02/0x30/0x31/0xFF`，每拍自动回 0）、受钳制值（`0x15` 被钳到 `DOPPLER_FRAME_CHIRPS=32`）。
- Python 拆包与 FPGA 打包的位域**两侧一致**，可在 `usb_data_interface_ft2232h.v` 的 `status_words[]` 拼装处逐一核对，这是排查「回读值错乱」的第一现场。
- 命令实践的答案：设 CFAR alpha=0x30 的命令是 `23000030`；`agc_saturation_count` 在 word 4 的 `[19:12]`（8 位）。

## 7. 下一步学习建议

- **进入 U7（STM32 固件）**：本讲讲的是 GUI↔FPGA 的直接命令通道。实际系统里 STM32 也会经 GPIO（如 DIG_5/DIG_6）旁路读取 AGC 状态，下一讲可看 STM32 如何作为系统管理者配合这条协议。
- **阅读 u7-l1（STM32 main）与 u9-l1（混合 AGC）**：理解 `agc_saturation_count / agc_current_gain` 这些本讲回读的字段，是如何在 FPGA 内环、STM32 外环、GUI 监控三层之间闭环的。
- **回到 u6-l1 复习 USB 读路径**：如果对 `usb_cmd_valid` 脉冲如何跨时钟域、Read FSM 如何收齐 4 字节还不够清晰，结合本讲的 opcode 视角再看一遍 toggle-CDC 会更有体感。
- **预习 u11-l3（跨层契约测试）**：本讲反复强调的「Opcode 枚举必须匹配 case 表」会被自动化测试强制校验，那里有专门的三层契约验证机制，是本讲硬契约思想的工程落地。
