# 简单双口 RAM：sdp_ram 与 sp_ram_be

## 1. 本讲目标

本讲进入 psi_common 的**存储层（Memory）**。学完后你应当能够：

- 读懂 `psi_common_sdp_ram` 与 `psi_common_sp_ram_be` 两个 RAM 实体的全部端口与 generic，并能用 `log2ceil` 推导地址位宽。
- 解释 `depth_g`/`width_g` 与地址总线宽度的数学关系。
- 区分**同步读**与**异步读**两种工作模式（`is_async_g`）。
- 说清**读前写 RBW** 与**写前读 WBR** 的行为差异，以及为何要提供两种模型。
- 理解 `shared variable` + `ram_style` 综合属性如何把一段纯 VHDL 描述映射到 FPGA 的 Block-RAM / Distributed-RAM 资源。

本讲是 u4（FIFO）、u7（delay）的前置：同步 FIFO 和延迟线都直接把 `sdp_ram` 当作底层存储来例化。

## 2. 前置知识

在开始之前，你需要具备以下概念（若不熟悉，可先阅读前置讲义 u1-l4、u2-l1）：

- **VHDL entity / architecture / generic**：实体声明对外接口，架构描述实现，generic 是编译期可配置参数。
- **可综合（synthesizable）**：一段 VHDL 能被综合工具翻译成真实硬件逻辑（门、触发器、BRAM）。
- **同步 RAM**：数据在时钟有效沿后才出现在读端口——即“给出地址后，下一拍才能读到数据”。
- **`std_logic_vector` / `unsigned` / `to_integer`**：用 `unsigned(addr)` 把地址向量转成整数，再作为数组下标访问存储。
- **`log2ceil`**：来自 `psi_common_math_pkg` 的编译期函数（u2-l1 已讲），返回 \( \lceil \log_2(n) \rceil \)，本讲用它推导地址位宽。
- **AXI-S 握手（VLD/RDY）**：本讲两个 RAM 本身不握手，但它们的上游 FIFO 是 AXI-S 接口。

一个核心直觉：**FPGA 内部并没有“一个通用 RAM 器件”，只有 Block-RAM（BRAM，大容量、同步）和 Distributed-RAM（LUT-RAM，小容量、可异步读）两种物理资源**。不同资源对“同一地址同时读写”的默认行为不同，所以本库提供 RBW/WBR 两种模型，让你写出的描述能精确匹配目标资源。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [hdl/psi_common_sdp_ram.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd) | **简单双口 RAM**（Simple Dual Port）：一个写口 + 一个读口，可选独立读时钟，可选 RBW/WBR，可选 RAM 资源风格。是全库 FIFO/延迟线的底层存储。 |
| [hdl/psi_common_sp_ram_be.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sp_ram_be.vhd) | **单口 RAM + 字节使能**（Single Port with Byte Enable）：读写共用同一地址/时钟，可按字节选择写入。 |
| [hdl/psi_common_math_pkg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd) | 数学工具包，本讲用到其中的 `log2ceil`（地址位宽推导）。 |

参考用法（不在 `source_files` 列表，但用来理解 RAM 被如何例化）：

- [hdl/psi_common_sync_fifo.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_fifo.vhd) 中的 `i_ram` 例化了 `sdp_ram`（同步、`ram_style` 透传）。
- [hdl/psi_common_delay.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_delay.vhd) 中的 `i_bram` 例化了 `sdp_ram`（作为环形缓冲实现延迟线）。

> 说明：这两个 RAM **没有专属自校验测试平台**（官方文档标注 *Testbench: N.A.*），它们通过 `sync_fifo`、`async_fifo`、`delay` 等上层组件的回归测试被间接覆盖。

---

## 4. 核心概念与源码讲解

### 4.1 端口、generic 与地址位宽推导

#### 4.1.1 概念说明

一个 RAM 有两个最基本的形状参数：

- **depth（深度）**：存储单元的个数，即能存多少个“字”。
- **width（宽度）**：每个字多少 bit。

地址总线要能唯一指向 depth 个字里的任意一个，所以地址位宽由 depth 决定，与 width 无关。具体关系是：

\[
\text{addr\_width} = \lceil \log_2(\text{depth}) \rceil
\]

psi_common 用 `psi_common_math_pkg` 里的 `log2ceil` 来算这个值，并在**端口声明里直接调用函数**——这样地址位宽会随 `depth_g` 自动推导，使用者无需手算。

#### 4.1.2 核心流程

`log2ceil` 的实现非常精巧，它复用了向下取整的 `log2`：

\[
\text{log2ceil}(n) = \text{log2}(2n - 1)
\]

直觉：把 \( n \) 放大一倍再减 1（\( 2n-1 \)），然后向下取整取 log2，等价于对原值向上取整。例如：

- depth = 1024 → `log2ceil(1024)` = 10 位地址。
- depth = 512 → `log2ceil(512)` = 9 位地址。
- depth = 1000（非 2 的幂）→ `log2ceil(1000)` = 10 位地址（因为 \( 2^9=512 \) 不够，需 \( 2^{10}=1024 \)）。

数据宽度则直接等于 `width_g`，无需换算。

#### 4.1.3 源码精读

先看 `sdp_ram` 的实体声明，generic 与端口都在这里：

[psi_common_sdp_ram.vhd:18-32](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd#L18-L32) —— 定义 depth/width/is_async/ram_style/ram_behavior 五个 generic，以及写口（wr_clk_i/wr_addr_i/wr_i/wr_dat_i）和读口（rd_clk_i/rd_addr_i/rd_i/rd_dat_o）。

关键看地址位宽如何在端口里被函数推导出来：

[psi_common_sdp_ram.vhd:25](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd#L25) —— `wr_addr_i` 的宽度写成 `log2ceil(depth_g) - 1 downto 0`，地址位宽随 depth 自动变化。

[psi_common_sdp_ram.vhd:29](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd#L29) —— `rd_addr_i` 用同样的写法，读写地址位宽一致。

再看 `log2ceil` 本体的实现：

[psi_common_math_pkg.vhd:164-170](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L164-L170) —— `log2ceil(arg)` 对 0 特判返回 0，否则返回 `log2(arg*2-1)`。

[psi_common_math_pkg.vhd:152-161](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L152-L161) —— 被复用的 `log2`：循环除以 2 计数，是经典的向下取整 log2。

`sp_ram_be` 的端口结构类似，但只有**一个地址口**（读写共用），并多了字节使能 `be_i`：

[psi_common_sp_ram_be.vhd:19-29](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sp_ram_be.vhd#L19-L29) —— generic 只有 depth/width/ram_behavior 三项（**没有** is_async、**没有** ram_style）；端口为 clk_i/addr_i/be_i/wr_i/dat_i/dat_o。

[psi_common_sp_ram_be.vhd:25](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sp_ram_be.vhd#L25) —— 字节使能宽度 `width_g/8`，即每 8 bit 一个使能位。

#### 4.1.4 代码实践

**目标**：体会“地址位宽随 depth 自动推导”。

**步骤**：

1. 打开 [hdl/psi_common_sdp_ram.vhd:18-32](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd#L18-L32)。
2. 在纸上对下列 depth 手算 `log2ceil(depth)`：512、1024、1000、33。
3. 对照 `log2ceil(arg*2-1)` 的定义验证。

**需要观察的现象 / 预期结果**：

| depth | log2ceil(depth) | 说明 |
|-------|-----------------|------|
| 512 | 9 | \( 2^9=512 \) 刚好够 |
| 1024 | 10 | \( 2^{10}=1024 \) |
| 1000 | 10 | 非 2 的幂，向上取整到 1024 |
| 33 | 6 | \( 2^5=32 \) 不够，需 \( 2^6=64 \) |

> 结论：地址位宽只跟 depth 有关，与 width 无关。这是后面 FIFO 指针宽度推导的同一套方法。

#### 4.1.5 小练习与答案

**练习 1**：若要让一个 RAM 存 300 个 12-bit 的字，地址总线至少需要几位？`wr_dat_i` 多宽？

**答案**：地址位宽 = `log2ceil(300)` = 9（\( 2^9=512 \ge 300 \)）；`wr_dat_i` 宽度 = width = 12 bit。

**练习 2**：`sp_ram_be` 的 `width_g = 32` 时，`be_i` 是几位？为什么？

**答案**：`be_i` 宽度 = `width_g/8` = 4 位。因为每 8 bit 一个字节使能，32 bit 共 4 个字节。

---

### 4.2 同步读与异步读时钟（is_async_g）

#### 4.2.1 概念说明

`sdp_ram` 是“简单双口”：一个写口、一个读口。读口到底用哪个时钟，由 `is_async_g` 决定：

- **同步模式（`is_async_g = false`，默认）**：读写共用 `wr_clk_i` 一个时钟。`rd_clk_i` 被忽略。
- **异步模式（`is_async_g = true`）**：写用 `wr_clk_i`，读用独立的 `rd_clk_i`——两个时钟可以是不同频率、不同相位，甚至无相位关系。

异步模式是**异步 FIFO 跨时钟域传递数据**的物理基础（读指针在写时钟域、读数据在读时钟域）。

#### 4.2.2 核心流程

```
is_async_g = false (同步):
  单进程 ram_p(wr_clk_i):
    上升沿 → [按 ram_behavior 读] → [按 wr_i 写]
  → 读写在同一时钟、同一进程内顺序执行

is_async_g = true (异步):
  进程 write_p(wr_clk_i): 上升沿 → 按 wr_i 写
  进程 read_p(rd_clk_i):  上升沿 → 按 rd_i 读
  → 读写分离到两个时钟域、两个进程
```

注意一个重要推论：**异步模式下不存在 RBW/WBR 的选择**。因为读写发生在两个独立时钟进程里，无法在同一拍内排序先后，读就是直接读当前 `mem(rd_addr)`。`ram_behavior_g` 只对同步模式有意义。

#### 4.2.3 源码精读

同步分支用 `generate` 选择：

[psi_common_sdp_ram.vhd:44-63](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd#L44-L63) —— `g_sync` 分支：单进程 `ram_p`，敏感 `wr_clk_i`，读写在同一上升沿内顺序执行（RBW/WBR 的分支见 4.3）。

异步分支：

[psi_common_sdp_ram.vhd:66-86](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd#L66-L86) —— `g_async` 分支：拆成 `write_p`（敏感 wr_clk_i）与 `read_p`（敏感 rd_clk_i）两个进程，读进程直接 `rd_dat_o <= mem(rd_addr)`，没有 RBW/WBR 分支。

[psi_common_sdp_ram.vhd:68-75](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd#L68-L75) —— 异步写进程：仅按 `wr_i` 写入，与读时钟无关。

[psi_common_sdp_ram.vhd:77-84](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd#L77-L84) —— 异步读进程：按 `rd_i` 读取，时钟是 `rd_clk_i`。

真实例化可参考 `delay.vhd`，它显式把 `is_async_g => false`：

[psi_common_delay.vhd:98-115](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_delay.vhd#L98-L115) —— 延迟线在 BRAM 模式下例化 `sdp_ram`，`is_async_g => false`，读写共用 `clk_i`，`rd_clk_i` 接到常量 `'0'`（同步模式下该端口不使用）。

#### 4.2.4 代码实践

**目标**：对比同步与异步例化时 `rd_clk_i` 的接法。

**步骤**：

1. 阅读 [psi_common_delay.vhd:106-114](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_delay.vhd#L106-L114)：同步模式下 `rd_clk_i => ground_c`（接到 `'0'`，因为不用）。
2. 设想一个异步 FIFO 场景：写时钟 100 MHz、读时钟 50 MHz。你会把 `wr_clk_i` 接 100 MHz、`rd_clk_i` 接 50 MHz、`is_async_g => true`。

**需要观察的现象 / 预期结果**：同步模式只跑一个时钟，资源/时序更简单；异步模式允许两个时钟域，但读数据相对读地址有一拍延迟（同步 RAM 本性）。

> 待本地验证：若你有 Modelsim/GHDL 环境（见 u1-l3），可写一个最小 TB，给 `is_async_g = true` 的实例分别喂两个不同频率时钟，观察读数据出现在 `rd_clk_i` 的上升沿后。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `sp_ram_be` 没有 `is_async_g`？

**答案**：`sp_ram_be` 是单口 RAM，读写共用同一个 `clk_i` 和同一个 `addr_i`，根本不存在第二时钟，自然没有异步读选项。

**练习 2**：异步模式下，若 `wr_clk_i` 和 `rd_clk_i` 完全无相位关系，读到的数据是否可靠？

**答案**：对**存储内容本身**可靠（写已落到 `wr_clk_i` 的上升沿后稳定）。但要注意：读地址 `rd_addr_i` 若来自另一个时钟域，本身需要做跨时钟域同步（这正是异步 FIFO 用格雷码指针解决的问题，见 u4-l2）。`sdp_ram` 只保证存储读写本身的正确性，不负责地址的 CDC。

---

### 4.3 ram_behavior：读前写 RBW 与写前读 WBR

#### 4.3.1 概念说明

考虑同步模式下、**同一时钟周期内对同一地址既读又写**的极端情况：

- **RBW（Read-Before-Write，读前写）**：这一拍读到的是**旧值**（写入之前的内容）。
- **WBR（Write-Before-Read，写前读）**：这一拍读到的是**新值**（刚刚写入的内容）。

为什么两种都要提供？因为**不同 FPGA 资源原生实现的语义不同**：

- 多数 Block-RAM（如 Xilinx BRAM）在“同地址同拍读写”时返回**旧值**（RBW 语义）。
- 部分 Distributed-RAM / LUT-RAM 或某些工艺库返回**新值**（WBR 语义）。

如果你的 RTL 描述与底层资源语义不一致，综合后行为会和仿真不一致——这是 RAM 建模最隐蔽的坑。所以库让你用 `ram_behavior_g` 显式声明，使描述匹配目标资源。

#### 4.3.2 核心流程

利用 VHDL **信号（signal）**与**变量（variable）**更新时机不同的特性来实现两种语义：

- `mem` 是 `shared variable`：变量赋值 `:=` **立即生效**。
- `rd_dat_o` 是 `signal`：信号赋值 `<=` 在进程**挂起时（本拍末）**才生效，但右值表达式在赋值语句执行那一刻就被求值。

于是同一进程内语句顺序决定一切：

```
RBW:  先 rd_dat_o <= mem(addr)   ← 此时 mem 是旧值 → 读到旧值
      后 mem(addr) := data        ← 写入新值

WBR:  先 mem(addr) := data        ← 立即写入新值
      后 rd_dat_o <= mem(addr)   ← 此时 mem 已是新值 → 读到新值
```

#### 4.3.3 源码精读

同步进程里通过 `ram_behavior_g` 字符串选择读发生的时机：

[psi_common_sdp_ram.vhd:45-62](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd#L45-L62) —— `ram_p` 进程：RBW 分支的读在写**之前**，WBR 分支的读在写**之后**，写夹在中间。

[psi_common_sdp_ram.vhd:48-52](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd#L48-L52) —— RBW：先 `rd_dat_o <= mem(rd_addr_i)`（旧值），受 `rd_i` 门控。

[psi_common_sdp_ram.vhd:53-55](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd#L53-L55) —— 写：`mem(wr_addr_i) := wr_dat_i`，变量立即更新。

[psi_common_sdp_ram.vhd:56-60](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd#L56-L60) —— WBR：写之后才读，此时 `mem(rd_addr_i)` 已是新值。

`sp_ram_be` 的逻辑完全同构，只是写变成了“按字节循环写”：

[psi_common_sp_ram_be.vhd:46-63](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sp_ram_be.vhd#L46-L63) —— `porta_p`：RBW 读在前、字节写在中间、WBR 读在后。

[psi_common_sp_ram_be.vhd:52-58](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sp_ram_be.vhd#L52-L58) —— 字节写循环：只有 `be_i(byte)='1'` 的字节才被覆盖，其余字节保留原值（这必须靠变量实现，信号无法做“部分位写入”）。

#### 4.3.4 代码实践

**目标**：用一个具体的同地址读写场景，说清 RBW 与 WBR 的输出差异。

**场景设定**（示例代码，非项目原有）：

- depth = 512，width = 16，同步模式。
- 初始 `mem(0) = 0x1111`。
- 某一拍：`wr_i='1'`、`wr_addr_i=0`、`wr_dat_i=0x2222`；同时 `rd_i='1'`、`rd_addr_i=0`。

**步骤**：

1. 跟踪 RBW（`ram_behavior_g = "RBW"`）：进程先执行 `rd_dat_o <= mem(0)`，此时 `mem(0)` 仍是 `0x1111` → 下一拍 `rd_dat_o = 0x1111`（旧值）；然后 `mem(0) := 0x2222`。
2. 跟踪 WBR（`ram_behavior_g = "WBR"`）：进程先执行 `mem(0) := 0x2222`；然后 `rd_dat_o <= mem(0)`，此时已是 `0x2222` → 下一拍 `rd_dat_o = 0x2222`（新值）。

**需要观察的现象 / 预期结果**：

| 配置 | 本拍 `rd_dat_o` 下一拍的值 | 含义 |
|------|--------------------------|------|
| RBW | `0x1111` | 读到写入前的旧值 |
| WBR | `0x2222` | 读到刚写入的新值 |

> 待本地验证：可在 Modelsim/GHDL 中跑两次仿真（仅改 `ram_behavior_g`），对比波形确认。

#### 4.3.5 小练习与答案

**练习 1**：若 `wr_i='0'`（本拍不写），RBW 和 WBR 的输出有区别吗？

**答案**：没有区别。因为写语句被 `if wr_i='1'` 门控跳过，`mem` 未变，无论读在前在后，读到的都是同一旧值。RBW/WBR 的差异**仅在“同地址同拍既读又写”时才显现**。

**练习 2**：为什么读要用 `rd_i` 门控（`if rd_i='1' then rd_dat_o <= ...`）？

**答案**：`rd_i` 是读使能。当 `rd_i='0'` 时本拍不更新 `rd_dat_o`（保持上一拍值），这对应 BRAM 的低功耗“不读则输出保持”行为，也避免无谓的输出翻转。

---

### 4.4 ram_style 综合属性与 shared variable 存储建模

#### 4.4.1 概念说明

要把一段 VHDL 数组描述真正变成 FPGA 上的 BRAM/LUT-RAM，靠两件事：

1. **可综合的存储建模写法**：用数组类型 + 同步读（时钟进程里读），综合器才能识别为 RAM（而不是一堆触发器）。
2. **综合属性（attribute）**：给存储对象贴一个提示，告诉工具用哪种资源。

psi_common 用 `shared variable` 建模存储，并贴上 Xilinx 风格的 `ram_style` 属性。

**为什么是 `shared variable` 而不是普通 `signal`？** 两个原因：

- 它支持 4.3 节的“变量立即生效”语义，从而能区分 RBW/WBR。
- 在异步模式下，存储被 `write_p` 和 `read_p` **两个进程**共享——普通 signal 不能被多个进程驱动写入，而 `shared variable` 可以被多进程访问（异步双口 RAM 的标准建模手法）。

#### 4.4.2 核心流程

```
1. 声明数组类型:     type mem_t is array (depth-1 downto 0) of slv(width-1 downto 0);
2. 声明共享变量:     shared variable mem : mem_t := (others => (others => '0'));
3. 贴综合属性:       attribute ram_style of mem : variable is ram_style_g;
4. 在时钟进程里读写: mem(to_integer(unsigned(addr))) := data;   -- 变量写
                     rd_dat_o <= mem(to_integer(unsigned(addr))); -- 信号读
```

`ram_style_g` 接受三个字符串值（Xilinx 词汇，其他厂商有类似属性）：

| 值 | 含义 |
|----|------|
| `"auto"`（默认） | 让工具按规模自动选 BRAM 或 Distributed-RAM |
| `"block"` | 强制用 Block-RAM |
| `"distributed"` | 强制用 Distributed-RAM（LUT-RAM） |

#### 4.4.3 源码精读

`sdp_ram` 的存储与属性声明：

[psi_common_sdp_ram.vhd:37-40](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd#L37-L40) —— 声明数组类型 `mem_t`、`shared variable mem`（初值全 0），并把 `ram_style` 属性绑定到 `mem`，取值来自 generic `ram_style_g`。

注意第 40 行 `attribute ram_style of mem : variable is ram_style_g;`——属性贴在 **variable** 上（不是 signal），与存储对象类型一致。

`sp_ram_be` 的存储声明（**没有** `ram_style` 属性，让工具自动推断）：

[psi_common_sp_ram_be.vhd:34-39](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sp_ram_be.vhd#L34-L39) —— `BeCount_c` 常量、`mem_t`/`shared variable mem`，未声明任何综合属性。

此外，`sp_ram_be` 用两条 `assert` 在编译期做参数校验（设计防护的好例子）：

[psi_common_sp_ram_be.vhd:43-44](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sp_ram_be.vhd#L43-L44) —— 断言 `ram_behavior_g` 必须是 RBW/WBR；断言 `width_g` 必须是 8 的倍数（否则字节使能无意义）。

真实使用：`sync_fifo` 把自己的 `ram_style_g` 透传给内部的 `sdp_ram`：

[psi_common_sync_fifo.vhd:170-184](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_fifo.vhd#L170-L184) —— FIFO 的 `i_ram` 例化 `sdp_ram`，把 `ram_style_g` 与 `ram_behavior_g` 一路传下去，让 FIFO 使用者能控制底层 RAM 资源。

#### 4.4.4 代码实践

**目标**：实例化一个 512×16 的 `sdp_ram`，并指定 RAM 资源风格。

**步骤**（示例代码，非项目原有，仅演示例化模板）：

```vhdl
-- 示例代码：512 个字、每字 16 bit 的同步双口 RAM，强制用 Block-RAM
i_my_ram : entity work.psi_common_sdp_ram
  generic map(
    depth_g        => 512,            -- log2ceil(512) = 9 位地址
    width_g        => 16,
    is_async_g     => false,          -- 同步：读写共用 wr_clk_i
    ram_style_g    => "block",        -- 强制 BRAM
    ram_behavior_g => "RBW"           -- 匹配多数 BRAM 的“同地址读旧值”语义
  )
  port map(
    wr_clk_i  => clk,
    wr_addr_i => my_wr_addr,          -- 9 bit
    wr_i      => my_wr_en,
    wr_dat_i  => my_wr_dat,           -- 16 bit
    rd_clk_i  => '0',                 -- 同步模式不使用
    rd_addr_i => my_rd_addr,          -- 9 bit
    rd_i      => my_rd_en,
    rd_dat_o  => my_rd_dat            -- 16 bit
  );
```

**需要观察的现象 / 预期结果**：

1. 地址端口 `my_wr_addr`/`my_rd_addr` 应声明为 9 bit（`log2ceil(512)=9`）。若你错写成 8 bit，综合/仿真会因宽度不匹配报错。
2. 综合后查看资源报告：`ram_style_g => "block"` 应使该 RAM 落到 Block-RAM 而非 LUT。
3. 把 `ram_behavior_g` 在 `"RBW"` 与 `"WBR"` 间切换，对同一地址同拍读写时，`rd_dat_o` 的值会不同（见 4.3.4）。

> 待本地验证：资源占用（BRAM vs LUT）需在真实综合工具（Vivado/Quartus）中确认；纯仿真无法看出资源类型。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `shared variable mem` 改成普通 `signal mem`，4.3 节的 WBR 还能正确实现吗？

**答案**：不能。signal 赋值要到进程挂起才生效，写 `mem(addr) <= data` 后立刻读 `mem(addr)`，读到的仍是旧值——于是 WBR 会退化成 RBW。变量赋值的“立即生效”是实现 WBR 的关键。

**练习 2**：`sp_ram_be` 为什么不贴 `ram_style` 属性？

**答案**：作者选择让综合工具对带字节使能的单口 RAM 自动推断资源（不同工具对 byte-enable RAM 的资源支持差异较大，硬贴属性反而可能约束失效）。`sdp_ram` 作为更通用的底层存储则暴露了该旋钮，供 FIFO/延迟线等上层组件透传控制。

---

## 5. 综合实践

把本讲四个模块串起来：**设计一个 512×16 的同步双口 RAM“写后立即回读”自测**。

任务：

1. 用 `log2ceil` 推导出地址位宽（应为 9），声明相应宽度的地址信号。
2. 例化 `psi_common_sdp_ram`（`is_async_g => false`、`ram_style_g => "auto"`）。
3. 编写一个最小激励进程（示例代码）：
   - 复位后向地址 0 写入 `0x1111`；
   - 下一拍再向地址 0 写入 `0x2222`，**同时**从地址 0 读。
4. 分别用 `ram_behavior_g => "RBW"` 和 `"WBR"` 跑两次，记录读端口输出。

预期结论：

- RBW 下，那拍读到的是 `0x1111`（旧值）。
- WBR 下，那拍读到的是 `0x2222`（新值）。
- 两次仿真只有 `ram_behavior_g` 一行不同，却产生不同的回读值——这正是 RAM 建模语义必须匹配底层资源的原因。

> 若无仿真环境，可改为**源码阅读型实践**：在 [psi_common_sdp_ram.vhd:45-62](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd#L45-L62) 中用笔跟踪 4.3.4 的场景，写出两种配置下 `rd_dat_o` 的寄存值，与上面的预期对照。

## 6. 本讲小结

- `psi_common_sdp_ram` 是**简单双口 RAM**（一写一读），`psi_common_sp_ram_be` 是**带字节使能的单口 RAM**；两者都是纯 VHDL、可综合、厂商无关。
- 地址位宽由 `log2ceil(depth_g)` 自动推导，在端口声明里直接调用函数，与 `width_g` 无关。
- `is_async_g` 切换**同步读**（读写同钟）与**异步读**（独立 `rd_clk_i`，双进程共享 `mem`）；异步模式无 RBW/WBR 之分。
- `ram_behavior_g` 选择 **RBW（读旧值）/ WBR（读新值）**，靠 `shared variable` 的“立即生效”语义在同一进程内通过语句顺序实现，用于匹配不同 FPGA 资源的原生语义。
- 存储用 `shared variable mem` 建模，并贴 `ram_style` 综合属性（`auto`/`block`/`distributed`）控制资源类型；`sp_ram_be` 不暴露该属性、并加 `assert` 校验 width 为 8 的倍数与 behavior 合法性。
- 这两个 RAM 是 `sync_fifo`、`async_fifo`、`delay` 等上层组件的底层存储，本身无专属测试平台。

## 7. 下一步学习建议

- **u3-l2 真双口 RAM**：继续阅读 `psi_common_tdp_ram` / `psi_common_tdp_ram_be`，理解两个**独立**读写端口的模型，以及它为何是异步 FIFO 的底层存储。
- **u4-l1 同步 FIFO**：看 `psi_common_sync_fifo` 如何在 `sdp_ram` 之上加读写指针、满空标志、几乎满空与 level 输出，把裸 RAM 变成带握手的缓冲。
- **u4-l2 异步 FIFO**：看 `sdp_ram`/`tdp_ram` 的异步模式如何配合格雷码指针实现跨时钟域缓冲。
- **u7-l3 delay**：看 `delay.vhd` 如何把 `sdp_ram` 当作环形缓冲，实现可配置深度的延迟线（并对比 SRL 实现）。
