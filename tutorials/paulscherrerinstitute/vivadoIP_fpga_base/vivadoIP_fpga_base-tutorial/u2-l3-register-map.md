# 寄存器映射：从偏移到功能

## 1. 本讲目标

上一篇（u2-l2）我们把 `psi_common_axi_slave_ipif` 这个「AXI 协议 → 寄存器数组」的适配器讲透了：它对外暴露三个长度为 64 的数组 `reg_rdata` / `reg_wdata` / `reg_wr`，而 fpga_base 自己要做的，仅仅是「把每个数组下标接到正确的功能上」。

本讲要回答的核心问题是：

> **这 64 个寄存器下标（`reg_rdata(0)`、`reg_rdata(1)`……`reg_rdata(63)`）各自对应什么功能？软件用哪个字节地址去访问它们？哪些能读、哪些能写？**

学完本讲，你应当能够：

1. 默写出 fpga_base 的**完整寄存器映射表**：版本、固件日期、软件日期、项目串、设施串、LED、DIP 开关分别落在哪个字节偏移。
2. 区分**只读寄存器**（版本、固件日期、DIP 开关、字符串）与**可读写寄存器**（软件日期、LED），并说出判定依据是 `reg_rdata` 的接法。
3. 解释为什么 HDL 的寄存器下标与 C 驱动头文件 `fpga_base.h` 里的偏移宏必须**严格一致**，否则会出现「写错了地方、读错了数据」的诡异 bug。

本讲是 u5（软件栈与系统集成）里 C 驱动、JTAG 调试、EPICS 集成三篇讲义的**共同基础**——它们全都建立在同一张寄存器映射表之上。

---

## 2. 前置知识

本讲假设你已经读过 u2-l2，知道：

- **三个寄存器数组**：`reg_rdata`（用户→适配器，每个寄存器当前的读出值）、`reg_wdata`（适配器→用户，主机刚写入的 32 位数据）、`reg_wr`（适配器→用户，单拍写脉冲）。
- **地址换算**：`NumReg_g=64`、`AxiAddrWidth_g=8`，即 256 字节寻址空间、64 个 32 位寄存器，**寄存器下标 = 字节地址的高 6 位 `A[7:2]`**。
- **复位默认值**：`ResetVal_g` 是一个手写的 64 项全 0 数组，给未被业务驱动的位提供确定初值。

本讲还会用到几个概念，先做个通俗解释：

- **字节偏移（byte offset）**：软件眼里「寄存器的地址」。本 IP 每个寄存器 4 字节，所以第 `n` 号寄存器的字节偏移就是 \( n \times 4 \)，即 `0x00, 0x04, 0x08, ...`。这也是 C 头文件里 `*_OFS` 宏的取值。
- **直通回显（passthrough echo）**：上一篇提过的接法——把 `reg_rdata(n)` 直接接到 `reg_wdata(n)`，主机写入什么，下次就读回什么。这正是「可读写寄存器」的实现方式。
- **只读寄存器**：不是指 AXI 从机不允许写（实际上适配器照样会接受写、照样会拉 `reg_wr` 脉冲），而是指 `reg_rdata(n)` 被**另一个独立信号源**驱动，主机的写入值 `reg_wdata(n)` 被丢弃、读不回来。从软件视角看就是「写了没用」。
- **大端打包（big-endian packing）**：把一个字符串的若干字符塞进一个 32 位字时，「第 0 个字符放最高字节」的排布方式。本讲的项目串、设施串就是这么存的。

> 提示：本讲的「读写权限」是从**软件可见的效果**来定义的（写入是否影响读出值），并非 AXI 协议层面的访问保护。AXI 层面这个 IP 不做读写权限校验，任何地址都可写。

---

## 3. 本讲源码地图

| 文件 | 在本讲的作用 |
| --- | --- |
| `hdl/fpga_base_v1_0.vhd` | **第一主角**。它的 `architecture` 里逐条 `reg_rdata(...) <= ...` 赋值，定义了每个寄存器的功能与读写属性。本讲绝大多数源码引用来自这里。 |
| `drivers/fpga_base/src/fpga_base.h` | **第二主角**。C 驱动头文件，用一串 `#define C_*_OFS` 宏把字节偏移「钉死」，是软件侧的寄存器映射契约。 |
| `drivers/fpga_base/src/fpga_base.c` | 证据：`fpga_base_version()` 在处理器启动时往 `0x18~0x28` 写软件日期，证明这组寄存器是可写的、且由软件写入。 |

外部依赖（不在本仓库，仅承接 u2-l2）：

- `psi_common_axi_slave_ipif` —— 提供三个寄存器数组接口；本讲只关心 fpga_base **怎么用**这三个数组，不关心适配器内部实现。

---

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. **寄存器地址映射**：从 `reg_rdata` 下标到字节偏移，再到功能含义，给出完整映射表（含字符串的大端打包）。
2. **读写权限**：只读 vs 可读写，判定依据是 `reg_rdata` 的接法（独立源 vs 直通回显）。
3. **硬件/软件契约**：HDL 寄存器下标与 C 头文件偏移宏必须严格一致，否则错位。

---

### 4.1 寄存器地址映射

#### 4.1.1 概念说明

上一篇讲过地址换算公式：**寄存器下标 = 字节地址的 `A[7:2]`**。换个等价的说法，因为每个寄存器占 4 字节，所以：

\[
\text{字节偏移} = \text{寄存器下标} \times 4
\]

地址的低 2 位 `A[1:0]` 用于在 4 字节内选择字节通道（配合 AXI 的 `wstrb` 写使能位掩码），在寄存器级别可以忽略。因此 `reg_rdata(0)` 对应 `0x00`、`reg_rdata(1)` 对应 `0x04`、`reg_rdata(6)` 对应 `0x18`……依次类推。

fpga_base 把 64 个寄存器划分成了若干功能组，每组占据一段连续偏移。我们的任务是把这些功能组与下标、偏移一一对应起来。

#### 4.1.2 核心流程

整理寄存器映射的标准流程：

1. 打开 `fpga_base_v1_0.vhd` 的 `architecture`，逐行扫描所有 `reg_rdata(n) <= ...` 赋值，记录「下标 `n` ← 数据源」。
2. 用公式 \( \text{偏移} = n \times 4 \) 把下标换算成字节偏移。
3. 打开 `fpga_base.h`，把每个 `C_*_OFS` 宏与上一步算出的偏移对照，确认两边一致。
4. 对字符串类寄存器，额外标注「字符在 32 位字内的字节排布」。

完整映射表（本讲最重要的产出）：

| 字节偏移 | 寄存器下标 | 名称（C 宏） | 读写 | 有效宽度 | 数据来源 / 说明 |
| --- | --- | --- | --- | --- | --- |
| `0x00` | 0 | `C_VERSION_OFS` | 只读 | 32 | `C_VERSION` 或 `BuildGitHash_c`（由 `C_USE_INFO_FROM_SCRIPT` 选择） |
| `0x04` | 1 | `C_FW_DATE_YEAR_OFS` | 只读 | 32 | 固件编译**年**（综合期 TCL 写入 FDPE 初值） |
| `0x08` | 2 | `C_FW_DATE_MONTH_OFS` | 只读 | 32 | 固件编译**月** |
| `0x0C` | 3 | `C_FW_DATE_DAY_OFS` | 只读 | 32 | 固件编译**日** |
| `0x10` | 4 | `C_FW_DATE_HOUR_OFS` | 只读 | 32 | 固件编译**时** |
| `0x14` | 5 | `C_FW_DATE_MINUTE_OFS` | 只读 | 32 | 固件编译**分** |
| `0x18` | 6 | `C_SW_DATE_YEAR_OFS` | **读写** | 32 | 软件编译**年**（直通回显 `reg_wdata(6)`） |
| `0x1C` | 7 | `C_SW_DATE_MONTH_OFS` | **读写** | 32 | 软件编译**月** |
| `0x20` | 8 | `C_SW_DATE_DAY_OFS` | **读写** | 32 | 软件编译**日** |
| `0x24` | 9 | `C_SW_DATE_HOUR_OFS` | **读写** | 32 | 软件编译**时** |
| `0x28` | 10 | `C_SW_DATE_MINUTE_OFS` | **读写** | 32 | 软件编译**分** |
| `0x2C`~`0x3C` | 11~15 | （保留） | — | 32 | 未使用，读回 `0`（来自 `ResetVal_g`） |
| `0x40` | 16 | `C_PROJECT_OFS` | 只读 | 32 | 项目串第 0~3 字符（大端打包） |
| `0x44` | 17 | `C_PROJECT_OFS+4` | 只读 | 32 | 项目串第 4~7 字符 |
| `0x48` | 18 | `C_PROJECT_OFS+8` | 只读 | 32 | 项目串第 8~11 字符 |
| `0x4C` | 19 | `C_PROJECT_OFS+0xC` | 只读 | 32 | 项目串第 12~15 字符 |
| `0x50` | 20 | `C_FACILITY_OFS` | 只读 | 32 | 设施串第 0~3 字符（大端打包） |
| `0x54` | 21 | `C_FACILITY_OFS+4` | 只读 | 32 | 设施串第 4~7 字符 |
| `0x58` | 22 | `C_FACILITY_OFS+8` | 只读 | 32 | 设施串第 8~11 字符 |
| `0x5C` | 23 | `C_FACILITY_OFS+0xC` | 只读 | 32 | 设施串第 12~15 字符 |
| `0x60` | 24 | `C_LED_OFS` | **读写** | 低 8 位 | LED，低 8 位直通回显并驱动 `o_led`，高 24 位读 `0` |
| `0x64` | 25 | `C_DIP_SW_OFS` | 只读 | 低 8 位 | DIP 开关，低 8 位 = `i_sw`，高 24 位读 `0` |
| `0x68`~`0xFC` | 26~63 | （保留） | — | 32 | 未使用，读回 `0`（来自 `ResetVal_g`） |

> 提示：项目串、设施串的 C 宏只定义了**起始偏移**（`C_PROJECT_OFS = 0x40`、`C_FACILITY_OFS = 0x50`），后续 3 个字通过 `+4`、`+8`、`+0xC` 偏移访问（见 4.1.3 的 C 代码）。这是字符串类寄存器的常见写法。

#### 4.1.3 源码精读

**版本寄存器 `0x00`**——根据信息源开关二选一：

[fpga_base_v1_0.vhd:233-234](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L233-L234) —— 默认（`C_USE_INFO_FROM_SCRIPT=false`）返回用户在泛型里指定的 `C_VERSION`（默认值 `X"FFFFFFFF"`）；若启用脚本化信息（u3-l3 详讲），则返回 git 哈希 `BuildGitHash_c`。

**固件日期 `0x04~0x14`**——由 `fpga_base_date` 实例驱动 5 个寄存器：

[fpga_base_v1_0.vhd:241-264](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L241-L264) —— 实例化 `fpga_base_date`，把它的 `o_year/o_month/o_day/o_hour/o_minute` 五个输出分别接到 `reg_rdata(1)~reg_rdata(5)`，即偏移 `0x04~0x14`。这组值在综合阶段由 TCL 钩子写进 FDPE 触发器初值（u3-l1、u3-l2 详讲），每次重新编译都会更新。

**软件日期 `0x18~0x28`**——直通回显，5 个寄存器：

[fpga_base_v1_0.vhd:271-275](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L271-L275) —— `reg_rdata(6)~reg_rdata(10)` 逐个接到对应的 `reg_wdata`，这就是上一篇说的「直通回显」接法，使这组寄存器可读可写。

**项目串 `0x40~0x4C`**——用 `generate` 循环把字符串打包进 4 个寄存器：

[fpga_base_v1_0.vhd:280-282](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L280-L282) —— 对 16 字符串 `C_VERSION_MAJOR` 的每个字符 `i`（从 0 开始），算出目标寄存器下标 `16 + (i/4)`，并把字符的 ASCII 码放进该字的某个字节。

字节位置由这段位切片决定：

\[
\text{字节号} = 3 - (i \bmod 4)
\]

也就是说每个 32 位字存 4 个字符，**第 0 个字符放最高字节（byte 3，bits 31:24）**，第 3 个字符放最低字节（byte 0，bits 7:0）。这就是「大端打包」。设施串的处理完全一样，只是起始下标换成 20（偏移 `0x50`）：

[fpga_base_v1_0.vhd:284-286](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L284-L286)

**LED 寄存器 `0x60`**——低 8 位直通回显，同时驱动物理端口：

[fpga_base_v1_0.vhd:291-292](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L291-L292) —— `reg_rdata(24)` 的低 8 位接到 `reg_wdata(24)` 低 8 位（可读写），同一段数据又接到 `o_led` 物理端口。高 24 位未被赋值，按 `ResetVal_g` 读回 `0`。

**DIP 开关 `0x64`**——只读，直接采样物理输入：

[fpga_base_v1_0.vhd:318](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L318) —— `reg_rdata(25)` 的低 8 位由物理输入 `i_sw` 驱动，主机无法改变它，所以是只读。

**复位默认值**——保证保留寄存器读回确定的 `0`：

[fpga_base_v1_0.vhd:151-169](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L151-L169) —— `ResetVal_g` 是一个手写的 64 项全 `X"00000000"` 数组，覆盖所有未被 `reg_rdata(n) <= ...` 显式赋值的下标（如 11~15、26~63，以及 LED/DIP 的高位字节）。

**C 头文件侧的偏移宏**——软件访问寄存器的「地址簿」：

[fpga_base.h:29-40](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.h#L29-L40) —— 版本与固件日期偏移；[fpga_base.h:84-88](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.h#L84-L88) —— 软件日期偏移；[fpga_base.h:93-100](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.h#L93-L100) —— 项目/设施/LED/DIP 偏移。把它们与上表逐行对比，数值完全吻合。

**C 侧读取字符串时为何要「字节倒序」**——正是因为 HDL 做了大端打包：

[fpga_base.c:68-74](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.c#L68-L74) —— 读取项目串时，对每个 4 字节字按 `+ 3 - byte_index` 的顺序取字节，把「最高字节先存」还原成「字符串原始顺序」。这与 HDL 的 `3 - (i rem 4)` 是同一件事的两面。

#### 4.1.4 代码实践

**实践目标**：亲手验证「下标 → 偏移」换算，并核对 HDL 与 C 头文件的一致性。

**操作步骤**：

1. 打开 [fpga_base_v1_0.vhd](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd)，搜索所有 `reg_rdata(` 出现的位置，列出「下标 n」。
2. 用公式 \( \text{偏移} = n \times 4 \) 把它们换算成十六进制偏移。
3. 打开 [fpga_base.h](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.h)，把每个 `C_*_OFS` 宏的值与你算出的偏移对照。

**需要观察的现象**：HDL 里下标 6 对应 `0x18`，而 C 头文件里 `C_SW_DATE_YEAR_OFS` 也恰好是 `0x00000018`；下标 24 对应 `0x60`，`C_LED_OFS` 也恰好是 `0x00000060`。

**预期结果**：两边偏移逐一吻合，没有错位。这正是 4.3 节要强调的「硬件/软件契约」。

> 待本地验证：如果你手头有 Vivado 工程，可在地址编辑器里看到该 IP 的 `range=256`、`width=32`，与本表的 64 个 32 位寄存器一致（u1-l2 已从 `component.xml` 侧佐证）。

#### 4.1.5 小练习与答案

**练习 1**：软件想读取「固件编译的小时数」，应该访问哪个字节偏移？对应 HDL 里哪个 `reg_rdata` 下标？

> **答案**：偏移 `0x10`（`C_FW_DATE_HOUR_OFS`），对应 `reg_rdata(4)`。由 `fpga_base_date` 实例的 `o_hour` 输出驱动（见 fpga_base_v1_0.vhd:262）。

**练习 2**：项目串最多多少个字符？占据哪些字节偏移？

> **答案**：最多 16 个字符（`C_VERSION_MAJOR` 长度），占据 `reg_rdata(16)~reg_rdata(19)`，即偏移 `0x40, 0x44, 0x48, 0x4C`（每个字存 4 个字符）。

**练习 3**：访问偏移 `0x70`（即 `reg_rdata(28)`）会读到什么？为什么？

> **答案**：读到 `0x00000000`。因为下标 28 属于 26~63 这段未使用区间，`reg_rdata(28)` 没有被任何赋值驱动，按 `ResetVal_g` 复位为全 0。

---

### 4.2 读写权限

#### 4.2.1 概念说明

很多外设手册会用「R/W」「RO」标注每个寄存器的权限，初学者常以为这是 AXI 协议强制的访问控制。但在 fpga_base 里，**权限不是协议层强制的，而是由 `reg_rdata` 的接法「自然形成」的**。判定方法只有一条：

> 看 `reg_rdata(n)` 被谁驱动。
> - 若被 `reg_wdata(n)`（或其一部分）驱动 → **可读写**（写入会被回显，下次读得到）。
> - 若被一个**独立信号源**（常量、`fpga_base_date` 输出、物理输入 `i_sw`、字符串 `generate`）驱动 → **只读**（写入值被丢弃，读不到）。

这套判定法直接来自上一篇 u2-l2 讲的「直通回显」接法。

#### 4.2.2 核心流程

给任意一个下标 `n` 判定读写权限：

1. 在 `architecture` 里找到 `reg_rdata(n) <= ???` 的右边。
2. 若右边是 `reg_wdata(n)` 或其切片 → 标记 **读写**。
3. 若右边是 `C_VERSION`、`fpga_base_date` 的某个输出、`i_sw`、字符串 `generate` 的产物等独立源 → 标记 **只读**。
4. 若根本找不到 `reg_rdata(n) <= ...` 这一行 → 该下标走 `ResetVal_g`，恒为 `0`，属「保留/未使用」。

把本 IP 的 64 个寄存器按此流程分类：

- **只读**：版本（0）、固件日期（1~5）、项目串（16~19）、设施串（20~23）、DIP 开关（25）。
- **可读写**：软件日期（6~10）、LED（24，低 8 位）。
- **保留（读 0）**：11~15、26~63，以及 LED/DIP 的高位字节。

> 注意一个细节：LED 寄存器 `reg_rdata(24)` 只回显了**低 8 位**（`(7 downto 0)`），高 24 位没有 `<=` 赋值，走 `ResetVal_g` 读回 `0`。所以严格说，`0x60` 是「低 8 位可读写、高 24 位读 0」。

#### 4.2.3 源码精读

**只读的典型：固件日期**——`reg_rdata(1)~(5)` 由 `fpga_base_date` 实例的输出驱动，与 `reg_wdata` 无关：

[fpga_base_v1_0.vhd:259-263](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L259-L263) —— 即使软件往 `0x04` 写入一个值，`reg_wdata(1)` 会变化、`reg_wr(1)` 会拉一个脉冲，但 `reg_rdata(1)` 始终由 `o_year` 驱动，写入值读不回来，所以是只读。

**只读的典型：DIP 开关**——`reg_rdata(25)` 由物理输入驱动：

[fpga_base_v1_0.vhd:318](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L318) —— 同理，软件写 `0x64` 不会改变拨码开关的电平。

**可读写的典型：软件日期**——`reg_rdata(6)~(10)` 直接回显 `reg_wdata`：

[fpga_base_v1_0.vhd:271-275](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L271-L275) —— 写入会被保存并读回，因此可读写。

**可读写的典型：LED**——低 8 位回显，并驱动物理端口：

[fpga_base_v1_0.vhd:291-292](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L291-L292) —— 写 `0x60` 的低字节会同时改变读回值和 `o_led` 引脚电平。

**C 侧的写操作验证可读写性**——`fpga_base_set_led` 往 `0x60` 写一个字节：

[fpga_base.c:17-20](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.c#L17-L20) —— 用 `Xil_Out8` 写 `C_LED_OFS`；由于 HDL 侧 `0x60` 是直通回显，这次写入会立刻反映到 `o_led` 与读回值。

#### 4.2.4 代码实践

**实践目标**：通过「读改写回读」的思维实验，体会只读与可读写寄存器的差异。

**操作步骤**：

1. 假设软件执行：先 `Xil_Out32(base+0x04, 0xDEADBEEF)`（往固件日期年寄存器写），再 `val = Xil_In32(base+0x04)`（读回）。
2. 再假设执行：先 `Xil_Out32(base+0x18, 0x000007E4)`（往软件日期年寄存器写 2024），再读回。
3. 对照 4.1.3 的 HDL 接法，预测两次读回的值。

**需要观察的现象**：

- 第 1 步：写入 `0xDEADBEEF` 后读回，**不是** `0xDEADBEEF`，而是 `fpga_base_date` 实例 `o_year` 的当前值（固件编译年份）。
- 第 2 步：写入 `0x000007E4` 后读回，**正是** `0x000007E4`。

**预期结果**：固件日期寄存器「写了等于没写」（只读），软件日期寄存器「写了能读回」（可读写）。差异完全源于 `reg_rdata` 接的是独立源还是 `reg_wdata`。

> 待本地验证：以上为依据 HDL 接法推断的结果。若有硬件或仿真平台，可用 JTAG-to-AXI（见 u5-l3）实测确认。

#### 4.2.5 小练习与答案

**练习 1**：LED 寄存器 `0x60` 写入 `0x000000FF` 后，读回是多少？`o_led` 引脚电平如何？

> **答案**：读回 `0x000000FF`（低 8 位回显，高 24 位本就是 0）；`o_led` 全 8 位为 1（8 个 LED 全亮）。

**练习 2**：LED 寄存器写入 `0x0000FF00`（即只置 bit8~bit15）后，读回是多少？`o_led` 如何？为什么？

> **答案**：读回 `0x00000000`，`o_led` 全灭。因为 HDL 只回显并驱动了**低 8 位** `(7 downto 0)`，bit8~bit15 没有被赋值，走 `ResetVal_g` 读回 0，也不会影响 `o_led`。

**练习 3**：为什么说本 IP 的「只读」并非 AXI 协议层的访问保护？

> **答案**：因为适配器 `psi_common_axi_slave_ipif` 对任何地址都会接受写、拉 `reg_wr` 脉冲、更新 `reg_wdata`；所谓「只读」只是因为 `reg_rdata(n)` 被独立源驱动、忽略了 `reg_wdata(n)`，从软件视角「写了读不回」。协议层并没有拒绝写事务（也不会回 SLVERR）。

---

### 4.3 硬件/软件契约

#### 4.3.1 概念说明

寄存器映射是 HDL 与软件之间最重要的**契约（contract）**：HDL 决定「哪个下标存什么」，软件必须用**完全相同的偏移**去访问，否则就会张冠李戴。这份契约在仓库里有**两份独立的表达**：

- **HDL 侧**：`fpga_base_v1_0.vhd` 里 `reg_rdata(n) <= ...` 的下标 `n`（隐式定义偏移 \( n \times 4 \)）。
- **软件侧**：`fpga_base.h` 里一串 `#define C_*_OFS` 宏（显式定义偏移）。

这两份表达**没有任何编译期联动**——HDL 综合时不会去读 `fpga_base.h`，C 编译时也不会去读 `.vhd`。它们的一致性完全靠**人工维护**。一旦有人改了 HDL 里某个 `reg_rdata(n)` 的下标 `n`，却忘了同步修改 `fpga_base.h` 里对应的宏，软件就会访问到错误的寄存器，产生极难排查的 bug。

#### 4.3.2 核心流程

维护这份契约的要点：

1. **改 HDL 寄存器布局时，必须同步改 `fpga_base.h`**（以及 EPICS 模板 `FPGA_BASE.template`、JTAG 调试脚本里的偏移，见 u5-l3）。
2. **C 宏只用绝对偏移，不依赖结构体布局**——因为不同编译器的结构体对齐规则不同，用 `#define C_*_OFS` 显式写死最稳妥。
3. **字符串类寄存器只定义起始偏移**，后续字用 `+4` 偏移访问，避免重复定义。
4. **保留区间的读回值（0）也要文档化**，让软件知道哪些偏移是「未定义行为」。

契约一致性的「双向自检」：

\[
\forall n,\ \ \text{HDL 中 } \texttt{reg\_rdata}(n) \text{ 的功能} \;\Longleftrightarrow\; \texttt{fpga\_base.h} \text{ 中值为 } n\times 4 \text{ 的宏}
\]

#### 4.3.3 源码精读

**软件侧契约的完整表达**——`fpga_base.h` 的偏移宏集中区：

[fpga_base.h:29-100](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.h#L29-L100) —— 从 `C_VERSION_OFS` 到 `C_DIP_SW_OFS`，每个宏都对应 HDL 里一个 `reg_rdata` 下标，数值与 \( n \times 4 \) 严格一致。

**软件日期「由谁在何时写入」的铁证**——`fpga_base_version()` 用预处理器宏解析 `__DATE__`/`__TIME__`，再写入 `0x18~0x28`：

[fpga_base.c:27-41](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.c#L27-L41) —— 函数体里 5 条 `Xil_Out32` 分别写到 `0x18/0x1C/0x20/0x24/0x28`（注意这里直接用了字面常量，而不是 `C_SW_DATE_*_OFS` 宏——一个轻微的契约不一致，但数值正确）。这组写入发生在**处理器启动时**（u5-l1 详讲），把「软件编译时间」回写到固件，从而让固件日期（`0x04~0x14`）与软件日期（`0x18~0x28`）可以对照，判断固件与软件是否配套。

**月份解析宏**——`DATE_MONTH` 从 `__DATE__` 字符串识别月份：

[fpga_base.h:58-70](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.h#L58-L70) —— 一长串三元运算符，把 `"Jul"` 识别为 `7`。这是把 C 预处理器提供的字符串日期转成数值的典型技巧（u5-l1 详讲）。

**读取侧也依赖同一份契约**——`fpga_base_print()` 用宏读回各寄存器并打印：

[fpga_base.c:50-66](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.c#L50-L66) —— 用 `C_VERSION_OFS`、`C_FW_DATE_*_OFS`、`C_SW_DATE_*_OFS` 读回并格式化打印。若这些宏与 HDL 错位，打印出的「版本」「日期」就全是错的。

#### 4.3.4 代码实践

**实践目标**：做一次契约一致性的「交叉审计」，体会两边为何必须同步。

**操作步骤**：

1. 在 [fpga_base_v1_0.vhd](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd) 中定位 LED 寄存器（`reg_rdata(24)`），记下其下标 24、偏移 `0x60`。
2. 在 [fpga_base.h](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.h) 中找到 `C_LED_OFS`，确认它等于 `0x00000060`。
3. 做一个「破坏性思维实验」：假设有人把 HDL 里的 LED 从 `reg_rdata(24)` 改到 `reg_rdata(30)`，却忘了改 `C_LED_OFS`。预测 `fpga_base_set_led(base, 0xAA)` 的后果。

**需要观察的现象**：HDL 下标与 C 宏数值在当前 HEAD 下完全一致；思维实验中，软件以为 LED 在 `0x60`，但硬件已把 LED 挪到 `0x78`（下标 30）。

**预期结果**：

- 当前 HEAD：`C_LED_OFS = 0x60` = 下标 24 × 4，一致，`fpga_base_set_led` 正常点灯。
- 思维实验：写入 `0x60` 实际落到了**保留寄存器** `reg_rdata(24)`（读 0，无物理效果），真正的 LED 寄存器 `reg_rdata(30)` 不被写入，`o_led` 引脚不变化——表现为「调用成功但灯不亮」。

> 待本地验证：可在仿真里人为制造这种错位观察现象。结论是——**改 HDL 寄存器布局必须同步改所有软件侧偏移定义**。

#### 4.3.5 小练习与答案

**练习 1**：本 IP 的寄存器映射契约有几份独立表达？为什么说它们「没有编译期联动」？

> **答案**：至少两份——HDL 侧 `reg_rdata(n)` 的下标，软件侧 `fpga_base.h` 的 `C_*_OFS` 宏（实际上 EPICS 模板、JTAG 脚本里还有第三、第四份）。它们没有编译期联动，是因为 VHDL 综合与 C 编译是两个独立的工具链，互不读取对方的源文件，一致性靠人工维护。

**练习 2**：为什么 `fpga_base.h` 用一串 `#define` 显式写死偏移，而不是定义一个 C 结构体然后用 `offsetof`？

> **答案**：因为不同编译器/平台的 C 结构体有各自的对齐（alignment）与填充（padding）规则，用结构体偏移不可移植、容易出错。显式 `#define` 把每个寄存器的字节偏移钉死，与硬件手册完全对应，最稳妥。

**练习 3**：`fpga_base_version()`（fpga_base.c:27-41）往 `0x18~0x28` 写软件日期，这组寄存器在 HDL 里为什么必须是可读写的？

> **答案**：因为这组寄存器的**数据源就是软件自己**（软件编译时间），HDL 无法预先知道。HDL 用「直通回显」接法 `reg_rdata(6~10) <= reg_wdata(6~10)`，让处理器启动时把 `__DATE__`/`__TIME__` 解析出的年月日时分写进去并读回。若 HDL 把它们接成只读（独立源），软件就写不进去，软件日期永远是复位值 0。

---

## 5. 综合实践

**任务**：亲手制作一张完整的 fpga_base 寄存器映射表，并回答「软件日期寄存器为何可写、由谁在何时写入」。

**步骤**：

1. 基于 4.1.2 的表格，整理一张属于你自己的映射表，**至少包含**这些列：字节偏移、寄存器下标、名称（C 宏）、读写、有效宽度、说明。
2. 把表分成四组：版本与日期区（`0x00~0x28`）、字符串区（`0x40~0x5C`）、物理 IO 区（`0x60~0x64`）、保留区。
3. 在表下用一段话回答核心问题：
   - **为何可写**：因为 HDL 用「直通回显」接法 `reg_rdata(6~10) <= reg_wdata(6~10)`（[fpga_base_v1_0.vhd:271-275](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L271-L275)），写入值会被回显，所以可读可写。
   - **由谁写入**：由**处理器上运行的软件**写入，具体是 C 驱动函数 `fpga_base_version()`（[fpga_base.c:27-41](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/drivers/fpga_base/src/fpga_base.c#L27-L41)），它用 `Xil_Out32` 把从 C 预处理器宏 `__DATE__`/`__TIME__` 解析出的年月日时分写入 `0x18~0x28`。
   - **何时写入**：在**处理器启动时**写入（即软件运行的早期，通常在初始化阶段调用一次）。这样固件里就同时保存了「固件编译时间」（综合期写入，`0x04~0x14`）和「软件编译时间」（启动期写入，`0x18~0x28`），两者可对照判断固件与软件是否配套。

**预期产出**：一张与 4.1.2 表格内容一致的映射表，以及一段逻辑清晰的论述。

> 待本地验证：若有 Vivado/Vitis 环境，可运行一个最小裸机程序调用 `fpga_base_version(base); fpga_base_print(base);`，观察串口打印的 FW date/time 与 SW date/time 是否分别反映「上次综合」与「本次编译运行」的时间。

---

## 6. 本讲小结

- 寄存器字节偏移 = 寄存器下标 × 4，即 8 位地址的高 6 位 `A[7:2]`；本 IP 共 64 个 32 位寄存器，覆盖 `0x00~0xFF`。
- 完整映射分四区：版本与日期（`0x00~0x28`）、项目/设施字符串（`0x40~0x5C`）、LED 与 DIP（`0x60~0x64`）、保留区（读 0）。
- **读写权限由 `reg_rdata` 的接法决定**：接 `reg_wdata` 的可读写（软件日期、LED 低 8 位），接独立源（版本、固件日期、字符串、DIP）的只读；这不是 AXI 协议层的访问保护。
- 字符串寄存器采用**大端打包**（第 0 字符放最高字节），C 侧读取时用 `+3-byte_index` 倒序还原。
- HDL 下标与 C 头文件 `C_*_OFS` 宏是**两份无编译期联动**的契约，改一边必须同步另一边（以及 EPICS、JTAG 脚本）。
- 软件日期寄存器（`0x18~0x28`）可写是因为 HDL 直通回显；由 `fpga_base_version()` 在处理器启动时把 `__DATE__`/`__TIME__` 写入。

---

## 7. 下一步学习建议

本讲建立了「地址偏移 ↔ 功能」的完整映射，这是后续三篇讲义的共同地基：

- **想看软件侧如何使用这张表**：进入 **u5-l1 裸机 C 驱动与 `__DATE__`/`__TIME__` 宏**，精读 `fpga_base.c` 如何用 `Xil_Out32`/`Xil_In32` 访问各偏移、如何解析预处理器日期宏。
- **想看硬件侧「固件日期」是怎么在综合期被写进寄存器的**：进入 **u3-1 用 FDPE 触发器存储固件编译日期** 与 **u3-l2 综合 TCL 钩子**，理解 `fpga_base_date` 实例背后的 FDPE 初值写入机制。
- **想看调试与系统集成如何复用这张表**：进入 **u5-l3 硬件调试 TCL 与 EPICS 集成**，看 JTAG-to-AXI 脚本与 EPICS `regDev` 模板如何用同一套偏移读写版本、日期、字符串。

建议先做 u3-l1（固件日期的硬件实现），因为它直接解释了本表里 `0x04~0x14` 这组「只读却每次编译都变」的寄存器是怎么来的。
