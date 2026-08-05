# SystemRDL 寄存器规格 csr.rdl

## 1. 本讲目标

本讲是 Unit 3「CSR——软硬件的唯一桥梁」的开篇，聚焦于那份**单一真源**规格文件 `csr.rdl`。

读完本讲，你应当能够：

1. 读懂 SystemRDL 的 `addrmap` / `regfile` / `reg` / `field` 四级语法层次，以及数组复制、位域 `[hi:lo]` 写法。
2. 理解字段的 `sw` / `hw` 读写方向属性，以及 `singlepulse`、`swacc`、`swmod`、`we` 等修饰符如何改变一个寄存器的硬件行为。
3. 认识 `routing_table` / `cryptokey_table` 这类 `external` 表声明的含义——它们由谁来实现、谁只做地址译码。
4. 能够把 `csr.rdl` 里的一行描述，对应到 PeakRDL 自动生成的 RTL（`csr.sv`）与软件 HAL（`csr_hw.h`），真正体会「一份规格喂两边」。

## 2. 前置知识

本讲假定你已经读过：

- **u2-l1（HW/SW 分区）**：知道控制面（软 CPU 固件）与数据面（DPE 硬件）分离，二者靠 CSR 通信。
- **u2-l4（SoC fabric 与 soc_if 总线）**：知道 CPU 经 `soc_fabric` 地址译码后，命中 `addr[31:29]==1` 的窗口就是 CSR 区，并对这片窗口发起 `vld/rdy` 读写。
- **u1-l4（构建流程总览）**：知道构建第一步是 CSR，输入是 `csr.rdl`，输出是 RTL 和 HAL。

下面用三段话把本讲需要的背景补齐。

**什么是 CSR？** CSR（Control and Status Register，控制与状态寄存器）是软 CPU「指挥」硬件、硬件「回报」状态的寄存器集合。CPU 往某个地址写一个值，硬件某个引脚就跟着变（控制）；CPU 读某个地址，拿到的是硬件当前状态（状态）。在 wireguard-fpga 里，CSR 是控制面和数据面之间**唯一的通信桥梁**。

**什么是 SystemRDL？** SystemRDL（Register Description Language，寄存器描述语言）是一种 IEEE 标准（IEEE 1685-2014）的领域专用语言，专门用来描述寄存器地图：哪个寄存器在哪个地址、有哪些字段、每个字段几位、谁能读谁能写。它的核心价值是**与编程语言、与硬件描述语言都无关**——它只描述「寄存器长什么样」，由工具（这里用的是 PeakRDL）再翻译成 RTL 和 C 头文件。

**什么是单一真源（single source of truth）？** 在没有 SystemRDL 的项目里，硬件工程师手写一份 Verilog 寄存器、软件工程师手写一份 C 头文件，两边各维护各的，地址和位域一旦对不上就是一个难调的 bug。单一真源的做法是：只维护**一份** `csr.rdl`，由工具自动派生出所有产物。改一处，处处改，不可能漂移。这是本讲最想传达的思想。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用法 |
|------|------|----------|
| [3.build/csr_build/csr.rdl](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl) | **单一真源**：用 SystemRDL 描述全部寄存器 | 本讲的主角，逐段精读 |
| [3.build/csr_build/generated-files/csr.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr.sv) | PeakRDL 生成的硬件 RTL | 用来验证「RDL 里的属性如何在硬件里实现」 |
| [3.build/csr_build/generated-files/csr_hw.h](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr_hw.h) | PeakRDL 生成的软件 HAL（C++ 头） | 用来验证「RDL 里的寄存器如何被软件访问」 |
| [3.build/MakefileCSR](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileCSR) | 编排 PeakRDL 生成流程的 Makefile | 用来理解 RDL → 双产物的生成命令 |

> 提示：`generated-files/` 下的文件是**构建产物**，不要手改——它们由 `csr.rdl` 经 `make -f MakefileCSR` 重新生成。手改会在下次构建时被覆盖。

---

## 4. 核心概念与源码讲解

### 4.1 SystemRDL 是什么：从单一真源到双产物

#### 4.1.1 概念说明

wireguard-fpga 的所有 CSR 只有一个定义来源：`csr.rdl`。这个文件描述了两样东西：

1. **寄存器地图本身**（`csr` addrmap）：cpu_fifo、uart、gpio、ethernet、dpe、routing_table、cryptokey_table 等寄存器。
2. **整个 SoC 的内存映射**（`wireguard` addrmap）：指令存储 imem、数据存储 dmem、以及上面的 csr 三块的基地址。

PeakRDL 读入这份 RDL，产出两路派生物：

- **硬件路 RTL**：`csr.sv` + `csr_pkg.sv`，综合进 FPGA，负责把 CPU 总线读写翻译成寄存器翻转。
- **软件路 HAL**：`csr_hw.h`（上板用）、`csr_cosim.h`（仿真用），是一组 C++ 类，让固件代码用「对象.字段()」的方式访问寄存器，无需手算地址。

于是软件和硬件永远指向同一份规格，这就是「单一真源」。

#### 4.1.2 核心流程

生成流程由 `MakefileCSR` 编排，关键三步：

```
csr.rdl  ──peakrdl regblock──▶  csr.sv / csr_pkg.sv        (硬件 RTL)
          ──peakrdl c-header──▶  csr.h / csr_hw.h / csr_cosim.h  (软件 HAL)
          ──peakrdl html/md───▶  文档
```

注意中间还有一步 `sed`：在生成协同仿真规格前，把 `buffer_writes` / `wbuffer_trigger` 关键字删掉——这是本项目用 FCR（Flow Control Register）做原子更新、刻意不用 PeakRDL 内建写缓冲的设计取舍（详见 u3-l4）。

#### 4.1.3 源码精读

**顶层 addrmap `csr` 与默认属性**。整个寄存器地图包在一个 `addrmap` 里：

[csr.rdl:43-51](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L43-L51) —— 定义顶层 `addrmap csr`，并设五条全局默认值。这几行是整份文件的「编译选项」：

| 默认属性 | 值 | 含义 |
|----------|-----|------|
| `littleendian` | — | 小端字节序（与 RISC-V、与 WG 头一致） |
| `default accesswidth = 32` | 32 | 一次总线访问 32 位 |
| `default regwidth = 32` | 32 | 每个寄存器 32 位宽 |
| `default alignment = 4` | 4 | 寄存器按 4 字节对齐 |
| `addressing = compact` | — | 紧凑寻址：跳过空洞，地址连续 |

`addressing = compact` 很关键——它让工具**压掉空隙**，使地址尽可能密集。例如 cpu_fifo.rx 的 7 个寄存器会排成 0x00、0x04、…、0x18 连续 7 个字，而不是按某种稀疏规则散开。

**顶层内存映射 addrmap `wireguard`**。在文件末尾还有第二个 addrmap，它把 imem / dmem / csr 三块拼成整个 SoC 地址空间：

[csr.rdl:930-952](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L930-L952) —— `wireguard` addrmap 声明三块：

| 区块 | 类型 | 基地址 | 大小 |
|------|------|--------|------|
| `imem` | external mem | `0x0000_0000` | 8192×32 位（指令存储） |
| `dmem` | external mem | `0x1000_0000` | 8192×32 位（数据存储） |
| `csr` | （上一个 addrmap） | `0x2000_0000` | 本讲主角 |

这三段基地址正好对应 u2-l4 里 fabric 的地址译码：`addr[31:28]==1` 命中 DMEM（0x1…），`addr[31:29]==1` 命中 CSR（0x2…）。现在你看到了——那个译码窗口的根源就在这里。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「一份 RDL → 两路产物」。

**操作步骤**（需本地具备 PeakRDL 环境；若未装可跳到「源码阅读型」部分）：

1. 进入 `3.build/` 目录。
2. 运行 `make -f MakefileCSR`。
3. 观察 `csr_build/generated-files/` 下新生成/刷新的文件。

**需要观察的现象**：`csr.sv`（RTL）与 `csr_hw.h`（HAL）都应被刷新，时间戳一致。

**源码阅读型补充**（不依赖运行）：打开 `MakefileCSR`，定位第 34–35 行的 `rtl` 目标，以及第 31–32 行的 `c-header` 目标，确认这两条 `peakrdl` 命令的输入都是同一份 `csr.rdl`（变量 `RDLSRC`）。

[MakefileCSR:31-35](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileCSR#L31-L35) —— 两条命令同一输入，产出分赴硬件与软件。

> 若无法本地运行，明确标注「待本地验证」生成步骤是否成功。

#### 4.1.5 小练习与答案

**练习 1**：为什么本项目要维护 `csr.rdl` 一份文件，而不是让硬件工程师写 Verilog、软件工程师写 C 头各维护一份？

**参考答案**：因为软件头和硬件 RTL 必须在地址、位域、读写方向上严格一致，两份手写副本极易漂移，产生难调的协议 bug。单一真源保证改一处处处改，由工具保证两边一致。

**练习 2**：`addressing = compact` 对地址布局有什么实际影响？

**参考答案**：它让工具压掉寄存器之间的空洞，使地址连续紧凑（如 0x00、0x04、0x08…），既节省地址空间，也让生成的地址译码逻辑更简单。

---

### 4.2 SystemRDL 语法层次与位域声明

#### 4.2.1 概念说明

SystemRDL 的描述像一棵四层树：

```
addrmap          ← 顶层地址地图（如 csr、wireguard）
 └─ regfile      ← 寄存器分组（如 cpu_fifo、uart、dpe）
     └─ reg      ← 一个 32 位寄存器（如 control、status）
         └─ field ← 寄存器内的若干位字段（如 tkeep、tlast、tuser_dst）
```

每个 `reg` 默认 32 位宽，里面的 `field` 用位域 `[hi:lo]` 声明它占哪几位。注意区分两种「位域」写法：

- `field {...} tdata[32] = 0;` —— 单个数表示「从最低位起占 32 位」，即 `[31:0]`。
- `field {...} tkeep[31:16] = 0;` —— `[hi:lo]` 显式给出起止位，表示占第 16～31 位。

一个 `reg` 内多个 `field` 的位域必须**互不重叠**，合起来可以不足 32 位（剩余位保留为 0）。

数组复制用后缀 `[N]`：`ethernet[4]` 表示复制 4 份，`entry[64]` 表示复制 64 份，地址自动按份步进。

#### 4.2.2 核心流程

以 `cpu_fifo.rx` 为例，它的 7 个寄存器在 RDL 里按声明顺序排列，配合 `compact` 寻址，每个占 4 字节，地址依次为 0x00、0x04、0x08、0x0c、0x10、0x14、0x18。这个顺序会**原样**反映到生成的 RTL 地址译码里。

#### 4.2.3 源码精读

**一个最简单的寄存器：rx.data_31_0**。先看最直白的例子，建立直觉：

[csr.rdl:61-71](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L61-L71) —— 一个 `reg` 里只有一个 `field tdata[32]`，占满整个 32 位。`name`/`desc` 是给人看的文档字符串；`sw = rw; hw = r;` 是访问属性（下一节细讲）。

**本讲的主角寄存器：rx.control**。它把多个字段挤进一个 32 位字，是本讲代码实践的对象：

[csr.rdl:109-154](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L109-L154) —— `cpu_fifo.rx.control` 寄存器，含 6 个字段。把它画成一张位图（bit 31 在左，bit 0 在右）：

| 位域 | 字段名 | 含义 |
|------|--------|------|
| `[31:16]` | `tkeep` | 16 位字节使能，对应 128 位 TDATA 的 16 个字节 |
| `[15:15]` | `tlast` | 包边界，置 1 表示本次传输是包的最后一拍 |
| `[7:7]` | `tuser_bypass_all` | 置 1 则整个 DPE 被旁路（直通） |
| `[6:6]` | `tuser_bypass_stage` | 置 1 则旁路 DPE 的下一级 |
| `[5:3]` | `tuser_src` | 3 位源地址（0=CPU, 1-4=eth1-4） |
| `[2:0]` | `tuser_dst` | 3 位目的地址（0=CPU, 1-4=eth1-4, 7=广播） |

注意 `[15:8]` 这一段（bit 15 除外的 8～14 位）没有字段，保留为 0。这就是「字段合起来可以不足 32 位」的实例。

这 6 个字段加起来是 16+1+1+1+3+3 = 25 位，散布在 25 个有效位上，中间留了保留位。

**RDL → RTL 地址映射的验证**。control 是 rx 的第 5 个寄存器，按 compact 寻址应在 0x10。看生成的 RTL：

[csr.sv:149](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr.sv#L149) —— RTL 里 control 的地址译码正是 `cpuif_addr == 14'h10`，与手算一致。

**RDL → HAL 地址映射的验证**。同一寄存器在软件 HAL 里：

[csr_hw.h:186](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr_hw.h#L186) —— HAL 把 control 的指针算成 `base_addr + 0x10/4`（除以 4 是因为这是 `uint32_t*`，按字而非字节步进）。同一地址，两边自动一致。

**数组复制：ethernet[4]**。四口以太网用数组复制避免重复声明：

[csr.rdl:417-456](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L417-L456) —— `regfile ethernet {...} ethernet[4];` 末尾的 `[4]` 把整组寄存器复制 4 份，对应 4 个网口；`name` 里的 `[0..3]` 是给人看的下标提示。

#### 4.2.4 代码实践（本讲指定实践）

**实践目标**：在 `csr.rdl` 中定位 `cpu_fifo.rx.control` 的各字段位域，并解释 `tuser_dst` 各取值的含义。

**操作步骤**：

1. 打开 `3.build/csr_build/csr.rdl`，定位 `csr.cpu_fifo.rx.control`（约第 109 行起）。
2. 逐字段记录其位域声明（如 `tkeep[31:16]`、`tuser_dst[2:0]`）。
3. 读 `tuser_dst` 字段的 `desc`，列出它每个取值代表的目的地。
4. 交叉验证：打开生成的 `csr_hw.h`，找到 `csr__cpu_fifo__rx__control_vp_t` 类，确认它提供了 `tuser_dst()` / `tuser_dst(data)` 等访问器。

**需要观察的现象 / 预期结果**：

`tuser_dst` 是 `[2:0]` 共 3 位，取值 0–7。根据其 `desc`（[csr.rdl:148-153](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L148-L153)），含义如下：

| 取值 | 含义 |
|------|------|
| 0 | 送往 CPU |
| 1 | 送往 eth1 |
| 2 | 送往 eth2 |
| 3 | 送往 eth3 |
| 4 | 送往 eth4 |
| 5、6 | 未定义（保留） |
| 7 | 广播（broadcast） |

这与 u1-l5 介绍的 DPE 接口地址编码完全一致（0=CPU、1-4=eth1-4、7=广播）。HAL 类 [csr_hw.h:114-138](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr_hw.h#L114-L138) 会为每个字段生成一对内联 getter/setter，固件写 `control->tuser_dst(2)` 即可把目的设成 eth2，无需手算「2 要放到 bit[2:0]」。

> 此实践为源码阅读型，无需运行硬件；地址与位域结论可静态确认。

#### 4.2.5 小练习与答案

**练习 1**：`field {...} tdata[32] = 0;` 和 `field {...} tkeep[31:16] = 0;` 这两种位域写法分别表示字段占哪些位？

**参考答案**：`[32]` 是单个数，表示从 bit 0 起占 32 位，即 `[31:0]`；`[31:16]` 显式给出 `[hi:lo]`，表示占 bit 16～31 共 16 位。

**练习 2**：control 寄存器里 `tuser_src` 占 `[5:3]`，软件要把源设成 eth3（编码 3），应往这个字段写什么值？整个 32 位字里该字段对应的原始比特是什么？

**参考答案**：字段值就是 3（`3'b011`）。它在 32 位字里位于 bit 5～3，所以整字里这部分比特是 `3 << 3 = 0b0011000 = 0x18`。用 HAL 时直接调 `control->tuser_src(3)`，无需手算移位。

---

### 4.3 字段读写属性：sw/hw 与 singlepulse / swacc / swmod

#### 4.3.1 概念说明

光知道字段占几位还不够，还得说清「谁能读、谁能写」。SystemRDL 用两个独立维度描述：

- **`sw`（software）**：软件（CPU）侧的访问权限，取值 `r`（只读）、`w`（只写）、`rw`（读写）、`na`（不可访问）。
- **`hw`（hardware）**：硬件（RTL）侧的访问权限，同样取 `r`/`w`/`rw`/`na`。

二者**独立**组合，决定了数据流向。本项目最常见的三种组合：

| 组合 | 数据流向 | 典型用途 |
|------|----------|----------|
| `sw = rw; hw = r;` | CPU 写 → 硬件读 | **控制**：CPU 设置参数，硬件照做（如 tkeep、tuser_dst） |
| `sw = r; hw = w;` | 硬件写 → CPU 读 | **状态**：硬件汇报，CPU 只读（如 status.tready） |
| `sw = r; hw = r;` | 两边都只读 | **常量**：如 hw_id，综合期固定 |

记住一句口诀：**「谁是写者，谁就是数据源」**。`hw = w` 表示硬件是写者（数据从硬件来），不是「硬件可被写」。

除了基本 `sw`/`hw`，还有几个改变行为的修饰符：

- **`singlepulse = true`**：单脉冲触发。软件写 1 后，该位只在**一个时钟周期**内为 1，随后自动回 0。用来产生「触发」事件（如 AXIS 的 TVALID 拍）。
- **`swacc = true`**（software-access-clear）：软件读该字段后，硬件自动清零。常用于「读后即清」的中断/数据标志。
- **`swmod = true`**（software-modify）：软件写该字段时，会产生一个内部「被写过」的脉冲，可被别的逻辑利用。
- **`we`**（write enable）：硬件侧带写使能，硬件可写也可读（`hw = rw` 的带使能变体）。

#### 4.3.2 核心流程

这些属性不是注释，它们**直接决定**生成 RTL 的形态。PeakRDL 对每种属性生成不同的 `always_comb` / `always_ff` 逻辑：

- 普通 `sw = rw; hw = r` 字段 → 「读改写（RMW）」逻辑：用 `decoded_wr_biten` 位使能掩码，只改软件写到的那些字节。
- `singlepulse` 字段 → 多一个 `else` 分支：写过后**下一拍强制清零**。
- `sw = r; hw = w` 字段 → 没有「SW write」分支，软件写不进去，只读硬件的值。

#### 4.3.3 源码精读

**单脉冲触发：rx.trigger.tvalid**。这是理解 singlepulse 的最佳样本：

[csr.rdl:156-167](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L156-L167) —— `tvalid` 字段带 `singlepulse = true`，`desc` 明说它是「single pulse trigger」。CPU 写 1 表示「我现在要把 Rx FIFO 里组装好的一拍数据推出去」。

看它生成的 RTL，验证「自动清零」：

[csr.sv:655-663](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr.sv#L655-L663) —— 注意第 658 行的 `else` 分支：`// singlepulse clears back to 0`，下一拍 `next_c = '0; load_next_c = '1;`。也就是说，软件写 1 那拍寄存器为 1，**再下一拍无条件回 0**，正好形成一个单周期脉冲。这就是 cpu_fifo 把 CSR 写入转换成 AXIS 的 TVALID 拍的原理（详见 u3-l3）。

**读后即清：uart.rx.data 的 swacc**。UART 接收数据寄存器用 swacc 实现「CPU 读走一个字节、硬件补下一个」：

[csr.rdl:330-336](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L330-L336) —— `data[7:0]` 带 `swacc = true`。CPU 每读一次 data，硬件就把它清掉、推进下一个待读字节。

**写带副作用：uart.tx.data 的 swmod**。UART 发送寄存器用 swmod 把「写 data」和「触发发送」绑定：

[csr.rdl:362-368](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L362-L368) —— `data[7:0]` 带 `swmod = true`。它的作用通过下面这行「赋值联动」接上：

[csr.rdl:349](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L349) 与 [csr.rdl:381](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L381) —— 这两行是 SystemRDL 的「信号赋值」：把 `tx.data` 被 swmod 触发的脉冲，接到一个隐藏的 `tx_trigger.write` 上；把 `rx.data` 被 swacc 触发的脉冲，接到 `rx_trigger.read` 上。于是「CPU 写 tx.data」自动等价于「触发一次 UART 发送」，软件无需单独写触发位。这两个 trigger 寄存器的 `desc` 也明确标注「used internally - don't try to read or write!」。

**FCR：一对控制+状态字段**。DPE 流控寄存器 dpe.fcr 是「控制位 + 状态位」的典型组合：

[csr.rdl:507-524](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L507-L524) —— `pause` 是 `sw = rw; hw = r`（CPU 写、硬件读，控制位），`idle` 是 `sw = r; hw = w`（硬件写、CPU 读，状态位）。CPU 写 `pause=1` 请求暂停 DPE，然后轮询读 `idle`，等它变 1 表示 DPE 真的停下了。这正是 u3-l4 原子更新握手的规格源头。

#### 4.3.4 代码实践

**实践目标**：对比「单脉冲字段」与「普通字段」在生成的 RTL 里有何不同。

**操作步骤**：

1. 在 `csr.sv` 中定位 `// Field: csr.cpu_fifo.rx.trigger.tvalid`（约第 649 行），观察它的 `else` 清零分支。
2. 再定位 `// Field: csr.cpu_fifo.rx.control.tuser_dst`（约第 511 行），观察它**没有**这个 `else` 清零分支。

**需要观察的现象 / 预期结果**：

- `tvalid`（singlepulse）：[csr.sv:658-661](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr.sv#L658-L661) 有 `else begin next_c = '0; load_next_c = '1; end`，每拍自动回零。
- `tuser_dst`（普通 rw 字段）：[csr.sv:512-522](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr.sv#L512-L522) 只有 `if(... && decoded_req_is_wr)` 一个分支，软件不写就保持原值。

结论：**`singlepulse` 属性 = 多生成一个强制清零的 else 分支**。这就是 RDL 属性驱动 RTL 行为的实证。

> 此实践为源码阅读型，静态对比即可，无需运行仿真。

#### 4.3.5 小练习与答案

**练习 1**：`sw = r; hw = w` 表示「软件可写、硬件可读」吗？

**参考答案**：不是。`hw = w` 表示**硬件是写者**（数据从硬件流向寄存器），软件侧 `sw = r` 是只读。整体方向是「硬件写 → 软件读」，是状态/汇报类寄存器。口诀：谁是写者谁是就是数据源。

**练习 2**：为什么 cpu_fifo 的 `tvalid` 要用 `singlepulse`，而不是普通 `rw` 字段让软件写完再手动写 0 清掉？

**参考答案**：因为 tvalid 要模拟 AXIS 的 TVALID 单拍握手：硬件只在它为 1 的那一拍取走数据，下一拍必须回 0，否则会被误认为「再推一拍同样的数据」。让软件手动清零会多花一次总线写、且时序上难以保证恰好一拍；singlepulse 由硬件自动在一拍后清零，既省一次访问又保证时序正确。

---

### 4.4 external 表声明：routing_table 与 cryptokey_table

#### 4.4.1 概念说明

到目前为止，所有寄存器都由 PeakRDL **全自动生成** RTL。但本项目有两张「大表」不适合让 PeakRDL 生成：

- **routing_table**：64 条路由表项（目的网段 → peer → 出口）。
- **cryptokey_table**：64 条 WG peer 条目（含本地/远端身份、加解密 256 位密钥、收发计数器等，每条 30 个寄存器）。

这两张表有两个共同点：

1. **条目多、结构重复**——适合用存储器（RAM）实现，而不是逐个寄存器展开。
2. **数据面要高速查表**——DPE 流水线要在一个时钟周期内读出表项，必须用双口 RAM 接到数据面，而不是走 CPU 总线那一套。

于是本项目用 SystemRDL 的 **`external`** 关键字：告诉 PeakRDL「这张表的**寄存器布局和地址**由我（RDL）定义，但**底层存储 RTL 由用户自己实现**」。PeakRDL 只为它生成地址译码和总线握手，存储体本身（一个 `tdp_ram` 双口 RAM）由手写 RTL 提供（详见 u4-l6）。

`external` 还能修饰存储器：`external mem imem` / `external mem dmem` 表示指令/数据存储器也由外部实现（就是手写的 imem.sv / dmem.sv）。

#### 4.4.2 核心流程

当 CPU 访问一个落在 `external` 表里的地址时，生成的 CSR RTL 不会直接翻转寄存器，而是：

1. 地址译码判定 `is_external = 1`。
2. 拉高 `external_req`，同时通过 `cpuif_req_stall` 让 CPU 总线**暂停等待**（stall）。
3. 等外部存储器（tdp_ram）完成读写，回一个 `external_wr_ack` / `external_rd_ack` 应答脉冲。
4. 撤销 stall，CPU 总线继续。

这套「请求—应答」握手是因为外部存储器的访问延迟不确定（数据面可能正在占用另一端口），必须挂起 CPU 直到完成。下面会看到它在 RTL 里如何实现。

#### 4.4.3 源码精读

**routing_table：external regfile + 基地址**。

[csr.rdl:527-579](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L527-L579) —— 关键三处：

- 第 527 行 `external regfile routing_table`：`external` 关键字声明存储由外部实现。
- 内部 `regfile entry {...} entry[64];`：64 条表项的布局模板，每条含 `ip`、`mask`、`peer_idx`、`dst` 四个寄存器。
- 第 579 行 `} routing_table @ 0x0400;`：`@ 0x0400` 给整张表一个**基地址**，让它从 0x400 开始排（前面 0x0～0x3FF 留给 cpu_fifo/uart/gpio/ethernet/hw_id/dpe 等小寄存器）。

**cryptokey_table：更大的 external 表**。

[csr.rdl:581-927](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L581-L927) —— 同样 `external regfile`，64 条 entry，但每条 entry 多达 30 个寄存器（local/remote 的 MAC/IP/port/id、8 段 encrypt_key、8 段 decrypt_key、收发计数器）。末尾 `@ 0x2000` 给它一个更高的基地址。注意其中计数器字段带 `we`（如第 887、899 行），表示硬件也能写回（数据面更新计数）。

可以核算两表规模：

- routing_table：64 条 × 4 寄存器 × 4 字节 = 1024 字节 = 0x400，恰好填满 0x400–0x7FF。
- cryptokey_table：64 条 × 30 寄存器 × 4 字节 = 7680 字节 = 0x1E00，落在 0x2000–0x3DFF。

**RTL 验证：external 地址窗口与 stall 握手**。看生成的 CSR RTL 怎么识别这两张外部表：

[csr.sv:173-177](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr.sv#L173-L177) —— `is_external` 由两个地址区间判定：routing_table 在 `14'h400`～`14'h7ff`，cryptokey_table 在 `14'h2000`～`14'h3dff`。这正是上面手算的两个窗口。命中即拉 `external_req`。

[csr.sv:61-83](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr.sv#L61-L83) —— external 访问的状态机：`external_req` 置位后，若还没收到 `external_wr_ack`/`external_rd_ack`，就拉高 `external_pending`；而 `external_pending` 又通过 `cpuif_req_stall_rd/wr`（第 82–83 行）让 CPU 总线 stall 等待。直到外部 RAM 回 ack，pending 才清零，CPU 解除等待。注释第 80–81 行写得很直白：「Read & write latencies are balanced. Stalls not required except if external」——普通寄存器不 stall，只有 external 才 stall。

**external mem：imem / dmem**。

[csr.rdl:937-949](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L937-L949) —— `external mem` 声明指令/数据存储器也由外部实现（`mementries = 8192; memwidth = 32;`），分别给定基地址。这呼应 u1-l4 讲的 imem.INIT.vh 综合期焊死、以及 u2-l5 讲的 UART 在线烧写 imem。

#### 4.4.4 代码实践

**实践目标**：确认 external 表的「地址窗口」与「stall 等待」在 RTL 里真实存在，且与 RDL 的基地址一致。

**操作步骤**：

1. 在 `csr.rdl` 中记录 routing_table 的基地址 `@ 0x0400` 和 cryptokey_table 的基地址 `@ 0x2000`。
2. 打开 `csr.sv`，定位 `is_external` 译码（约第 173 行），核对其窗口下界与 RDL 基地址一致。
3. 核对窗口上界与「条目数 × 每条寄存器数 × 4 字节」的估算一致。

**需要观察的现象 / 预期结果**：

- routing_table 窗口 `0x400`–`0x7ff`：下界 0x400 = RDL 的 `@ 0x0400` ✓；窗口大小 0x400 = 64×4×4 字节 ✓。
- cryptokey_table 窗口 `0x2000`–`0x3dff`：下界 0x2000 = RDL 的 `@ 0x2000` ✓；窗口大小 0x1e00 = 64×30×4 字节 ✓。
- 在第 61–83 行能看到 external 访问会触发 `cpuif_req_stall`，即 CPU 访问外部表时会被挂起直到 RAM 应答。

> 此实践为源码阅读型，结论可静态确认。

#### 4.4.5 小练习与答案

**练习 1**：为什么 routing_table / cryptokey_table 要用 `external`，而不是让 PeakRDL 像普通寄存器那样自动生成存储？

**参考答案**：因为这两张表条目多（各 64 条）、且数据面 DPE 流水线要高速查表（每拍读出一项），必须用双口 RAM 实现：一个端口接 CPU（写表），另一个端口接数据面（查表）。PeakRDL 自动生成的逐寄存器逻辑无法提供这种双口高速访问，所以只让它生成地址译码与总线握手，存储体由手写的 tdp_ram 提供。

**练习 2**：CPU 读一个 routing_table 表项时，总线为什么会 stall？

**参考答案**：因为该地址命中 `is_external`，CSR RTL 拉高 `external_req` 与 `external_pending`，进而拉高 `cpuif_req_stall_rd`，让 CPU 总线挂起等待；直到外部 tdp_ram 完成读、回一个 `external_rd_ack` 脉冲，pending 才清零、stall 解除。普通寄存器无此握手，故不 stall。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「从需求到规格」的小设计。

**任务**：假设要给 wireguard-fpga 新增一个「心跳」寄存器，需求如下——

1. CPU 可以读写一个 8 位的「心跳间隔」字段（单位：秒）。
2. 硬件每倒数到 0 就发一个单脉冲「tick」给软件，软件读后自动清零。
3. 还要一个只读的「已发送 tick 计数」16 位字段，由硬件累加。

**请写出**：

(a) 这三个字段的 `sw`/`hw` 属性分别该是什么？
(b) 哪个字段该加 `singlepulse`？哪个该加 `swacc`？
(c) 用 SystemRDL 语法写出这个 `reg` 的骨架（参考本讲引用的真实写法）。

**参考答案**：

(a)

| 字段 | sw | hw | 说明 |
|------|----|----|------|
| 间隔 interval[7:0] | `rw` | `r` | CPU 设定，硬件读 |
| tick 标志 | `r` | `w` | 硬件写脉冲，CPU 读 |
| 计数 count[15:0] | `r` | `w`（或 `rw`+`we` 若硬件带写使能） | 硬件累加，CPU 只读 |

(b) tick 标志本身已经是硬件写的脉冲，若想让它「软件读后清零」则加 `swacc = true`；若 tick 直接由硬件单拍产生、不需软件读清，则硬件侧用 `singlepulse` 思路（但 singlepulse 只对 sw 写有效，故此处更合适的是 hw 侧脉冲 + swacc）。interval 是配置值，不加任何修饰符。

(c) 示例代码（**注意：本段不是项目原有代码，是按本讲语法写的示例**）：

```
reg {
   name = "csr.heartbeat";
   desc = "Heartbeat Register";

   field {
      name = "csr.heartbeat.interval[7:0]";
      desc = "Heartbeat interval in seconds";
      sw = rw;
      hw = r;
   } interval[7:0] = 0;

   field {
      name = "csr.heartbeat.tick";
      desc = "Heartbeat tick, cleared on read";
      sw = r;
      hw = w;
      swacc = true;
   } tick[15:15] = 0;

   field {
      name = "csr.heartbeat.count[15:0]";
      desc = "Number of ticks sent";
      sw = r;
      hw = w;
   } count[31:16] = 0;
} heartbeat;
```

做完后，对照本讲 4.2/4.3 节的真实 cpu_fifo.rx.control 写法，检查位域是否重叠、属性方向是否正确。若本地有 PeakRDL，可把它加进 `csr.rdl` 跑 `make -f MakefileCSR`，观察生成的 `csr.sv` 是否为 tick 字段生成了 swacc 清零逻辑、为 interval 字段生成了 RMW 写逻辑。

## 6. 本讲小结

- **SystemRDL 是单一真源**：`csr.rdl` 一份文件，经 PeakRDL 同时派生出硬件 RTL（`csr.sv`）和软件 HAL（`csr_hw.h`），从根上消除软硬协议漂移。
- **四级语法层次**：`addrmap` > `regfile` > `reg` > `field`；字段用 `[hi:lo]` 或 `[N]` 声明位域，一个 reg 内字段不可重叠；数组后缀 `[N]` 复制同类条目（如 ethernet[4]、entry[64]）。
- **`sw`/`hw` 是两个独立方向**：`sw=rw;hw=r` 是控制类（CPU 写硬件读），`sw=r;hw=w` 是状态类（硬件写 CPU 读）；口诀「谁是写者谁是就是数据源」。
- **修饰符改变行为**：`singlepulse` 让软件写的位一拍后自动清零（生成 RTL 的 else 清零分支），`swacc` 读后清，`swmod` 写带副作用，三者都能在 `csr.sv` 里找到对应实现。
- **`external` 表由用户实现存储**：routing_table（@0x0400）与 cryptokey_table（@0x2000）用 `external regfile` 声明，PeakRDL 只生成地址译码与请求—应答 stall 握手，存储体是手写的双口 RAM（见 u4-l6）；`external mem` 同理用于 imem/dmem。
- **地址布局可核算**：`addressing = compact` 下地址连续，RDL 里声明的顺序与基地址直接对应到 `csr.sv` 的 `cpuif_addr` 译码和 `csr_hw.h` 的指针偏移。

## 7. 下一步学习建议

本讲只读了「规格源头」。接下来：

- **u3-l2（PeakRDL 自动生成 RTL 与 HAL）**：深入生成流程，看 `MakefileCSR` 如何调 `peakrdl regblock` / `c-header`，以及 `sysrdl_cosim.py` 如何用 `VPROC` 宏在上板头（`csr_hw.h`）与协同仿真头（`csr_cosim.h`）间切换。
- **u3-l3（CPU FIFO：AXIS 到 CSR 的映射）**：本讲的 cpu_fifo.rx 字段（tdata×4、tkeep、tlast、tuser_*、singlepulse tvalid）如何被软件用约 10 步组装成一拍 128 位 AXIS 传输。
- **u3-l4（FCR 流控寄存器与原子更新）**：本讲的 `dpe.fcr`（pause/idle）如何用于 routing/cryptokey 表的原子更新握手。
- **u4-l6（路由表与密钥表的 tdp_ram 实现）**：本讲声明为 `external` 的两张表，在 RTL 里如何由 `tdp_ram` 双口 RAM 实例化、A 端接 CSR、B 端接数据面查表。

建议在进入 u3-l2 前，先动手做完本讲第 5 节的综合实践，确保自己能从需求反推出 SystemRDL 字段属性。
