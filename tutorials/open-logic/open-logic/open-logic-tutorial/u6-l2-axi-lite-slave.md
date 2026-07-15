# AXI4-Lite 从机与寄存器接口

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `olo_axi_lite_slave` 在系统中扮演的角色——它是一座把 **AXI4-Lite 总线**翻译成**简单地址/读/写接口（Rb 接口）**的「桥」。
- 理解它是如何用**单 FSM**逐拍处理一次访问、并完成**地址映射（含地址屏蔽）**的。
- 掌握**写寄存器接入**：写脉冲 `Rb_Wr`、字节使能 `Rb_ByteEna`、写数据 `Rb_WrData` 的含义与用法。
- 掌握**读寄存器接入**：读脉冲 `Rb_Rd`、读数据 `Rb_RdData`、读有效 `Rb_RdValid` 的握手约定，以及**读超时**保护机制。
- 学会参照官方 `RbExample.vhd` 把一组**寄存器（或一块存储）**挂到这座桥上，构造出自己的寄存器组。

本讲承接 [u6-l1 AXI 流水线阶段](u6-l1-axi-pipeline-stage.md)：上一讲已经确立「同一份 AXI4 实体可直接用于 AXI4-Lite，因为所有 AXI4 专有端口都带默认值」这一结论。本讲正是把这个「Lite 语义」用到一个真正的 Lite 从机上。

## 2. 前置知识

在开始前，先用一句话复习几个关键概念（详细版见 [u1-l5](u1-l5-conventions-and-anatomy.md)、[u2-l2](u2-l2-pipeline-stage-handshake.md)）：

- **AXI4-Lite**：AXI4 的「精简版」。它保留五条独立的握手通道（读地址 AR、读数据 R、写地址 AW、写数据 W、写响应 B），但每次事务只传输**一个数据字**（没有突发 burst）。它是 CPU 配置外设寄存器最常用的总线。
- **主/从（Master/Slave）**：主机发起读写，从机响应。本讲的实体是**从机**，被动接受主机（通常是 CPU / 软核）的访问。
- **握手**：每条通道都用 `Valid`/`Ready` 握手——双方都为 `1` 的那一拍，事务才算成交（见 [u1-l5](u1-l5-conventions-and-anatomy.md) 的 AXI-S 约定，AXI4-Lite 的五通道同理）。
- **两进程法（two-process）**：组合进程 `p_comb` 只算「下一拍状态 `r_next`」，时序进程 `p_seq` 只负责打拍与复位，状态收进一个 `record`。这是 Open Logic 全库统一的写法（见 [u2-l2](u2-l2-pipeline-stage-handshake.md)）。
- **寄存器组（Register Bank）**：FPGA 设计里一群可被 CPU 读写的寄存器，例如「控制寄存器」「状态寄存器」「分频系数」等。把它们挂到一条总线上，CPU 就能像读写内存一样读写它们。
- **字节使能（Byte Enable / WStrb）**：写事务里每个字节对应一个使能位，为 `0` 的字节不被写入，从而支持「只改一个字节」的部分写。

> 一个直觉比喻：`olo_axi_lite_slave` 就像一个**前台接待员**。外面的访客（AXI 主机）讲的是一套复杂的「AXI4-Lite 礼仪」（五个通道、各种握手）；接待员把它们逐条记下，转成一张**内部工单**（`Rb_Addr` + `Rb_Wr`/`Rb_Rd` + 数据），递给后台的**业务员**（你自己写的寄存器组进程）。业务员不用懂 AXI，只需按工单读写自己的抽屉（寄存器）。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [src/axi/vhdl/olo_axi_lite_slave.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_lite_slave.vhd) | 本讲主角，AXI4-Lite 从机实体。用一个 FSM 把 AXI4-Lite 五通道翻译成 Rb 接口。 |
| [doc/axi/olo_axi_lite_slave.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/axi/olo_axi_lite_slave.md) | 官方文档，含接口表、写/读时序图与寄存器组代码片段。 |
| [doc/axi/slave/RbExample.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/axi/slave/RbExample.vhd) | 官方寄存器组示例，演示「字节使能写」「整字写」「写 1 清零」「读时清零」四种典型寄存器模式。 |
| [src/axi/vhdl/olo_axi_pkg_protocol.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_pkg_protocol.vhd) | AXI 协议常量包，定义了响应码 `Resp_t`（OKAY/SLVERR 等），从机用它报告读超时错误。 |
| [test/axi/olo_axi_lite_slave/olo_axi_lite_slave_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_lite_slave/olo_axi_lite_slave_tb.vhd) | 配套 VUnit 测试台，演示如何用 AXI 主机验证组件（VC）驱动从机，是本讲综合实践的样板。 |

## 4. 核心概念与源码讲解

### 4.1 模块定位：从 AXI4-Lite 到 Rb 接口的「桥」

#### 4.1.1 概念说明

`olo_axi_lite_slave` **本身不存储任何寄存器**。它的全部职责是**协议翻译**：

- **左侧（总线侧）**：标准的 AXI4-Lite 从机接口 `S_AxiLite_*`，对外像任何一个 AXI4-Lite 从机。
- **右侧（用户侧）**：一组极简的「地址 + 读/写脉冲 + 数据」信号，统称 **Rb 接口**（Rb = Register Bank）。

用户代码（你的寄存器组）只需盯住 Rb 接口，完全不必关心 AXI 五通道的握手细节。这正是 [u1-l1](u1-l1-project-overview.md) 讲到的「Ease of Use」哲学——一个实体只做一件事，把复杂协议挡在墙外。

#### 4.1.2 核心流程

从机的顶层接口可以分成三组：

```text
        AXI4-Lite 主机                        你的寄存器组
              │                                     │
   S_AxiLite_AR/AW/W/B/R  ───►  [olo_axi_lite_slave]  ───►  Rb_Addr / Rb_Wr / Rb_Rd
              │                  (协议翻译 + FSM)            Rb_WrData / Rb_ByteEna
              │                                               Rb_RdData / Rb_RdValid  ◄──
              │                                     │
```

- **控制**：`Clk`、`Rst`（同步、高有效复位，遵循全库约定）。
- **AXI4-Lite 从机接口**：`S_AxiLite_...`，五通道齐全。
- **Rb 接口**：地址、写相关、读相关共 7 个信号。

三个泛型控制其行为：地址宽度、数据宽度（必须是 2 的幂字节数）、读超时周期数。

#### 4.1.3 源码精读

泛型定义见 [src/axi/vhdl/olo_axi_lite_slave.vhd:36-40](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_lite_slave.vhd#L36-L40)：`AxiAddrWidth_g`（默认 8）、`AxiDataWidth_g`（默认 32）、`ReadTimeoutClks_g`（默认 100）。

AXI4-Lite 五通道端口见 [src/axi/vhdl/olo_axi_lite_slave.vhd:46-67](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_lite_slave.vhd#L46-L67)——AR（读地址）、R（读数据）、AW（写地址）、W（写数据）、B（写响应），每条通道都是 `Valid`/`Ready` 配对。

右侧 Rb 接口见 [src/axi/vhdl/olo_axi_lite_slave.vhd:68-75](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_lite_slave.vhd#L68-L75)：

| 信号 | 方向 | 含义 |
| :--- | :--- | :--- |
| `Rb_Addr` | out | 要访问的寄存器地址（字节地址） |
| `Rb_Wr` | out | 写脉冲（单周期） |
| `Rb_ByteEna` | out | 写字节使能（来自 AXI 的 `WStrb`） |
| `Rb_WrData` | out | 写数据 |
| `Rb_Rd` | out | 读脉冲（单周期） |
| `Rb_RdData` | in | 读数据，`Rb_RdValid='1'` 时有效 |
| `Rb_RdValid` | in | 读有效握手，每个 `Rb_Rd` 脉冲必须用它应答一次 |

注意一个关键不对称：**写侧没有「写完成」应答**（从机总是回 OKAY），而**读侧必须用 `Rb_RdValid` 应答**。这是因为写事务即使丢失也不易察觉，而读事务若永远等不到数据，主机会一直挂死——所以读侧需要握手 + 超时双重保护（见 4.4）。

#### 4.1.4 代码实践

**目标**：在源码里确认「协议翻译」的边界。

1. 打开 [src/axi/vhdl/olo_axi_lite_slave.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_lite_slave.vhd)。
2. 在实体的 `port` 声明里画一条竖线，把 `S_AxiLite_*`（左侧）和 `Rb_*`（右侧）分开。
3. 数一下：左侧有多少个 AXI 信号？右侧有多少个 Rb 信号？

**预期结果**：左侧 5 通道共约 12 个信号，右侧仅 7 个信号。你会直观感受到「桥」把复杂度压缩了多少。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Rb 接口里没有 `Rb_Clk`、`Rb_Rst`？

**参考答案**：因为寄存器组与从机处在**同一个时钟域**，共享顶层的 `Clk` 和 `Rst`。Rb 接口只是「内部工单」，不需要重复传递时钟与复位。

**练习 2**：`AxiDataWidth_g` 为什么必须是 2 的幂字节数？

**参考答案**：见 [src/axi/vhdl/olo_axi_lite_slave.vhd:113-118](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_lite_slave.vhd#L113-L118) 的两条 `assert`：数据宽度必须是 8 的倍数、且字节数必须是 2 的幂。因为地址屏蔽需要 `log2(字节数)` 为整数（见 4.2）。

---

### 4.2 寄存器映射：地址、字节使能与地址屏蔽

#### 4.2.1 概念说明

**寄存器映射（Register Mapping）**回答一个问题：「CPU 用地址 X 访问时，到底落在哪个寄存器上？」

在 `olo_axi_lite_slave` 的设计里，映射规则极其简单——**它不做译码，只做地址透传（外加屏蔽）**：

- `Rb_Addr` 直接给出主机送来的字节地址。
- **译码工作完全交给用户代码**：你的寄存器组进程用一个 `case Rb_Addr is` 来决定改哪个寄存器。

这种「桥只搬运、用户自译码」的分工，让从机本身保持极简，也让你能自由设计任意映射（连续映射、稀疏映射、存储区映射都行）。

#### 4.2.2 核心流程

地址屏蔽（address masking）发生在**写地址**捕获时。设数据字节数为 \(B = \text{AxiDataWidth\_g}/8\)，则：

\[
\text{UnusedBits} = \log_2(B)
\]

这 \( \log_2(B) \) 个最低位是「字内字节偏移」，对一个**整字寄存器**没有意义，所以写通路把它们强制清零，保证 `Rb_Addr` 总是**字对齐**的。例如 `AxiDataWidth_g=32` 时 \(B=4\)，\(\text{UnusedBits}=2\)，地址 `0x0C` 与 `0x0F` 写入都会被屏蔽成 `0x0C`。

> 一处需要留意的细节：写通路做了地址屏蔽，但**读通路没有做**——读地址 `S_AxiLite_ArAddr` 被原样透传到 `Rb_Addr`（见 4.4 源码精读）。在规范用法下主机总是发对齐地址，二者没有差别；但如果你写寄存器组时对非对齐读地址做了 `case` 判断，要意识到读侧可能收到未对齐的低位。最稳妥的做法是：寄存器组只按**字地址**（如 `X"00"`、`X"04"`、`X"08"`）做 `case`，忽略低位。

#### 4.2.3 源码精读

地址屏蔽用的常量定义在 [src/axi/vhdl/olo_axi_lite_slave.vhd:108](https://github.com/open-logic/open-logic/blob/ecca8af952798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_lite_slave.vhd#L108)：

```vhdl
constant UnusedBits_c : natural := log2(AxiDataWidth_g/8);
```

实际的屏蔽动作在写命令状态 `WrCmd` 里，见 [src/axi/vhdl/olo_axi_lite_slave.vhd:147-152](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_lite_slave.vhd#L147-L152)：

```vhdl
when WrCmd =>
    -- Latch write command
    v.Addr := S_AxiLite_AwAddr(S_AxiLite_AwAddr'high downto UnusedBits_c)
              & zerosVector(UnusedBits_c);
    -- Get ready for write data
    v.WReady := '1';
    v.State  := WrData;
```

这段代码做两件事：① 把写地址的低 `UnusedBits_c` 位清零后存入 `v.Addr`（地址屏蔽）；② 拉高 `WReady` 进入 `WrData` 状态，准备接收写数据。

官方文档的接口表也说明了 `Rb_Addr` 是字节地址，见 [doc/axi/olo_axi_lite_slave.md:64-72](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/axi/olo_axi_lite_slave.md#L64-L72)。

#### 4.2.4 代码实践

**目标**：用官方测试台验证地址屏蔽的行为。

1. 阅读 [test/axi/olo_axi_lite_slave/olo_axi_lite_slave_tb.vhd:255-275](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_lite_slave/olo_axi_lite_slave_tb.vhd#L255-L275) 的 `AddressMasking` 用例。
2. 该用例对地址 `X"8F"` 发起一次写，并按不同的 `AxiDataWidth_g`（8/16/32/64/128）检查 `Rb_Addr` 是否被屏蔽成 `8F/8E/8C/88/80`。
3. 运行该用例（运行方式见第 5 节综合实践）。

**预期结果**：数据越宽，被屏蔽掉的低位越多——8 位时全保留（`0x8F`），128 位时低 4 位清零（`0x80`）。**待本地验证**（具体命令见第 5 节）。

#### 4.2.5 小练习与答案

**练习 1**：`AxiDataWidth_g=64` 时，地址 `0x1234_5678_90AB_CDEF` 写入后，`Rb_Addr` 是多少？

**参考答案**：64 位 = 8 字节，\(\text{UnusedBits}=3\)，低 3 位清零。`0xEF` → 二进制低 3 位 `111` 清零得 `0xE8`，故 `Rb_Addr = 0x1234_5678_90AB_CDE8`。

**练习 2**：为什么把译码（`case Rb_Addr`）放在用户代码、而不是从机内部？

**参考答案**：这样从机保持通用——同一份从机既能挂「3 个寄存器」也能挂「一块 4 KB RAM」。映射规则由用户决定，符合「一实体只做一事」的哲学。

---

### 4.3 写寄存器接入：写通路与字节使能

#### 4.3.1 概念说明

**写寄存器接入**指：当主机发起一次 AXI4-Lite 写，你的寄存器组如何接住它。

从机的写通路向你暴露三个信号——

- `Rb_Wr`：写脉冲，**仅拉高一拍**。这一拍里 `Rb_Addr`/`Rb_WrData`/`Rb_ByteEna` 同时有效。
- `Rb_WrData`：要写入的数据。
- `Rb_ByteEna`：字节使能，告诉寄存器「这 4 个字节里哪些要改」。

关键约束（来自官方文档 [doc/axi/olo_axi_lite_slave.md:20-22](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/axi/olo_axi_lite_slave.md#L20-L22)）：**写事务没有应答，你的代码必须能跟上主机送达的速度**。换句话说，`Rb_Wr` 一来你就得当场写完，没有「等我有空再写」的余地。

#### 4.3.2 核心流程

一次写事务在 FSM 里走四拍（约 4 个时钟周期，符合文档 [doc/axi/olo_axi_lite_slave.md:35-37](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/doc/axi/olo_axi_lite_slave.md#L35-L37) 所述）：

```text
Idle ──AwValid/AwReady──► WrCmd ──► WrData ──WValid──► WrResp ──BReady──► Idle
   （捕获写地址）       （屏蔽地址）  （产生 Rb_Wr 脉冲）  （回 B 通道 OKAY）
```

在 `WrData` 状态，当 `WValid=1` 时，从机在同一拍里：锁存字节使能与写数据、拉高 `Rb_Wr`、置 `BValid=1`。你的寄存器组就是在这一拍「看到」写请求的。

#### 4.3.3 源码精读

写数据状态见 [src/axi/vhdl/olo_axi_lite_slave.vhd:154-163](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_lite_slave.vhd#L154-L163)：

```vhdl
when WrData =>
    if S_AxiLite_WValid = '1' then
        v.State   := WrResp;
        v.WReady  := '0';
        v.ByteEna := S_AxiLite_WStrb;   -- 字节使能原样透传
        v.WrData  := S_AxiLite_WData;   -- 写数据原样透传
        v.Wr      := '1';               -- 产生单周期写脉冲
        v.BValid  := '1';               -- 准备写响应
    end if;
```

注意 `Rb_ByteEna` 直接来自 AXI 的 `WStrb`，没有做任何加工——主机想写哪些字节，就如实告诉你的寄存器组。写响应恒为 OKAY，见 [src/axi/vhdl/olo_axi_lite_slave.vhd:214](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_lite_slave.vhd#L214)（`S_AxiLite_BResp <= AxiResp_Okay_c; -- Writes can't fail`）。

那么寄存器组怎么用字节使能？官方 `RbExample.vhd` 给了「按字节循环、逐字节选择写入」的范例，见 [doc/axi/slave/RbExample.vhd:7-11](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/doc/axi/slave/RbExample.vhd#L7-L11)：

```vhdl
when X"00" =>
    -- Register with byte enables
    for i in 0 to 3 loop
        SomeReg(8*(i+1)-1 downto 8*i) <= Rb_WrData(8*i-1 downto 8*i);
    end loop;
```

> 说明：该片段直接取自仓库 `doc/axi/slave/RbExample.vhd`，为聚焦主流程略有删节（省略了 `if Rb_Wr='1'` 的外层与复位）。

#### 4.3.4 代码实践

**目标**：阅读测试台，理解字节使能如何被逐字节检查。

1. 阅读 [test/axi/olo_axi_lite_slave/olo_axi_lite_slave_tb.vhd:178-197](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_lite_slave/olo_axi_lite_slave_tb.vhd#L178-L197) 的 `WriteStrobes` 用例。
2. 它循环遍历每个字节（`byteIdx`），构造只有该字节使能为 1 的 `Strb_v`，发一次写，再检查 `Rb_ByteEna` 是否与之一致。
3. 思考：如果你的寄存器组忽略 `Rb_ByteEna`、总是整字写入，会有什么后果？

**预期结果**：测试通过说明字节使能被正确透传。若忽略 `Rb_ByteEna`，CPU 想「只改第 0 字节」时会把其它 3 个字节也覆盖成旧写数据的值——这是常见 bug。

#### 4.3.5 小练习与答案

**练习 1**：`Rb_Wr` 为什么必须是**单周期脉冲**，而不是一个电平？

**参考答案**：因为每次写事务只对应一次寄存器更新。如果 `Rb_Wr` 是持续电平，寄存器组会在多个周期重复写入同一个值（或在下一次事务到来前提前写入）。单周期脉冲保证「一次事务 = 一次写动作」，见测试台对 `Rb_Wr` 拉低后的检查 [olo_axi_lite_slave_tb.vhd:142-143](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_lite_slave/olo_axi_lite_slave_tb.vhd#L142-L143)。

**练习 2**：写响应 `BResp` 永远是 OKAY，这是否意味着写一定成功？

**参考答案**：不是。从机无法知道你的寄存器组是否「真的」写了（比如地址落在未实现的 `when others` 分支）。OKAY 只表示「从机收到了这笔写并转成了 Rb 工单」。写是否生效，取决于你的 `case` 是否覆盖了那个地址。

---

### 4.4 读寄存器接入：读通路、Rb_RdValid 握手与读超时

#### 4.4.1 概念说明

**读寄存器接入**指：主机发起一次读，你的寄存器组如何把数据交回去。

读通路的关键是**握手**：从机给你一个单周期读脉冲 `Rb_Rd`（连同 `Rb_Addr`），你必须**最终**回一个单周期脉冲 `Rb_RdValid`（连同 `Rb_RdData`）。重要性质（见文档 [doc/axi/olo_axi_lite_slave.md:25-28](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/doc/axi/olo_axi_lite_slave.md#L25-L28)）：

- 每个 `Rb_Rd` 脉冲**必须且只能**用一个 `Rb_RdValid` 应答一次。
- 从 `Rb_Rd` 到 `Rb_RdValid` 的延迟**不必固定**——你可以当拍回（0 拍延迟），也可以几拍后回（比如等 RAM 读出）。

但如果你的代码**永远不回** `Rb_RdValid`（例如读了一个未实现的地址），主机会一直等待而挂死。为此从机内置了**读超时**：超过 `ReadTimeoutClks_g` 拍还没等到数据，它就替你向主机回一个 `SLVERR`（从机错误响应），让主机解脱。

#### 4.4.2 核心流程

读事务的 FSM 走向：

```text
Idle ──ArValid/ArReady──► RdCmd ──► RdData ──Rb_RdValid（或超时）──► RdResp ──RReady──► Idle
   （捕获读地址）        （发 Rb_Rd 脉冲）  （等待/倒计时）         （回 R 通道数据）
```

`Rb_Rd` 脉冲在 `RdCmd` 状态产生（进入 `RdData` 后即恢复为 0）；`RdData` 状态里一边等 `Rb_RdValid`，一边用 `ToCnt` 倒计时。两者谁先到，就带着对应的响应码（OKAY 或 SLVERR）进入 `RdResp`。

读延迟可变这一点在测试台里被显式验证——它对 0~3 拍四种延迟各跑一次，见 [olo_axi_lite_slave_tb.vhd:151-173](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_lite_slave/olo_axi_lite_slave_tb.vhd#L151-L173)。

#### 4.4.3 源码精读

读命令状态（产生 `Rb_Rd` 脉冲、装载超时计数器）见 [src/axi/vhdl/olo_axi_lite_slave.vhd:173-178](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_lite_slave.vhd#L173-L178)：

```vhdl
when RdCmd =>
    v.Addr  := S_AxiLite_ArAddr;     -- 注意：读地址不做屏蔽，原样透传
    v.Rd    := '1';                  -- 单周期读脉冲
    v.State := RdData;
    v.ToCnt := ReadTimeoutClks_g-1;  -- 装载超时初值
```

等待读数据 + 超时处理见 [src/axi/vhdl/olo_axi_lite_slave.vhd:180-195](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_lite_slave.vhd#L180-L195)：

```vhdl
when RdData =>
    if Rb_RdValid = '1' then                 -- 正常应答
        v.RData  := Rb_RdData;
        v.RValid := '1';
        v.RResp  := AxiResp_Okay_c;
        v.State  := RdResp;
    end if;
    if v.ToCnt = 0 then                       -- 超时
        v.RValid := '1';
        v.RResp  := AxiResp_SlvErr_c;         -- 回从机错误
        v.State  := RdResp;
    else
        v.ToCnt := v.ToCnt - 1;
    end if;
```

`AxiResp_Okay_c` 与 `AxiResp_SlvErr_c` 这两个响应码定义在协议包 [src/axi/vhdl/olo_axi_pkg_protocol.vhd:27-31](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_pkg_protocol.vhd#L27-L31)。

超时行为在测试台 `ReadTimeout` 用例中被验证：它故意不回 `Rb_RdValid`，期望主机收到 `SLVERR`，见 [olo_axi_lite_slave_tb.vhd:200-204](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_lite_slave/olo_axi_lite_slave_tb.vhd#L200-L204)。

#### 4.4.4 代码实践

**目标**：追踪一次「读超时」的完整时序。

1. 阅读 [olo_axi_lite_slave_tb.vhd:200-204](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_lite_slave/olo_axi_lite_slave_tb.vhd#L200-L204)。
2. 该用例调用 `push_ar` 发出读地址，然后 `expect_r(... resp => AxiResp_SlvErr_c ...)` 期望收到一个错误响应——注意它**完全不驱动** `Rb_RdValid`。
3. 对照 [olo_axi_lite_slave.vhd:180-195](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_lite_slave.vhd#L180-L195) 想象：`ToCnt` 从 `ReadTimeoutClks_g-1` 逐拍减到 0，第 100 拍触发 SLVERR。

**预期结果**：主机在约 `ReadTimeoutClks_g`（默认 100）拍后收到 `SLVERR` 而非挂死。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：如果你的某个寄存器需要 2 拍才能读出（比如它存在一块 RAM 里），可以接这个从机吗？

**参考答案**：可以。读延迟可变——你只需在 `Rb_Rd` 到来后的第 2 拍才拉高 `Rb_RdValid` 并给出 `Rb_RdData`，从机的 `RdData` 状态会一直等，只要不超过 `ReadTimeoutClks_g` 即可。

**练习 2**：超时计数器 `ToCnt` 的类型为什么是 `natural range 0 to ReadTimeoutClks_g-1`？

**参考答案**：见 [olo_axi_lite_slave.vhd:102](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_lite_slave.vhd#L102)。把范围限定到 `[0, ReadTimeoutClks_g-1]` 既省寄存器位宽，又能在综合时让工具知道这是一个有界计数器，便于优化。

---

### 4.5 存储接入与 RbExample：寄存器组的常见模式

#### 4.5.1 概念说明

掌握了写、读两条通路后，把它们组合起来就是**一个完整的寄存器组**。`RbExample.vhd` 不是可综合的完整实体（它省略了声明与复位），而是一段**示范进程**，集中展示四种最常见的寄存器模式：

| 模式 | 地址 | 行为 |
| :--- | :--- | :--- |
| 带字节使能的读/写寄存器 | `0x00` | 写时按 `Rb_ByteEna` 逐字节更新；读时返回当前值 |
| 整字读/写、读时清零 | `0x04` | 写时整字更新；读出后自动清零（典型「读后清」状态寄存器） |
| 写 1 清零（write-1-to-clear） | `0x08` | 写入值为 1 的位会把对应寄存器位清零（典型中断挂起寄存器） |

「存储接入」与「寄存器接入」用的是**同一套 Rb 接口**——区别只在于你的 `case Rb_Addr` 是落到几个寄存器上，还是落到一段连续地址（RAM）上。例如要挂一块 RAM，你只需把 `Rb_Addr` 当作 RAM 的字节地址（除以字节数得到字下标），用 `Rb_Wr`/`Rb_Rd` 读写 RAM 即可。

#### 4.5.2 核心流程

寄存器组进程的标准骨架（**示例代码**，整理自 `RbExample.vhd`）：

```vhdl
-- 示例代码：寄存器组进程骨架（简化自 doc/axi/slave/RbExample.vhd）
p_rb : process(Clk) is
begin
    if rising_edge(Clk) then
        -- 1) 写处理：按地址译码，应用字节使能或特殊模式
        if Rb_Wr = '1' then
            case Rb_Addr is
                when X"00"   => ... -- 带字节使能写
                when X"04"   => ... -- 整字写
                when X"08"   => ... -- 写1清零
                when others  => null;
            end case;
        end if;

        -- 2) 读处理：默认拉低 Rb_RdValid，按地址给出读数据并拉高
        Rb_RdValid <= '0';
        if Rb_Rd = '1' then
            case Rb_Addr is
                when X"00"   => Rb_RdData <= ...; Rb_RdValid <= '1';
                when others  => null;  -- 未实现地址：不回 RdValid → 触发读超时
            end case;
        end if;

        -- 3) 同步复位（省略）
    end if;
end process;
```

两个要点：

- **`Rb_RdValid` 必须有默认值 `'0'`**：每拍先把它置 0，仅在命中地址时改写为 1。这是产生单周期应答脉冲的标准手法。
- **未实现地址的读会触发超时**：`when others => null` 意味着不回 `Rb_RdValid`，从机等满超时就回 SLVERR。这是有意的——可用它区分「合法地址」与「非法地址」。

#### 4.5.3 源码精读

官方示例的写处理见 [doc/axi/slave/RbExample.vhd:4-20](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/axi/slave/RbExample.vhd#L4-L20)，三种模式一目了然：

```vhdl
when X"00" =>  -- 带字节使能
    for i in 0 to 3 loop
        SomeReg(8*(i+1)-1 downto 8*i) <= Rb_WrData(8*i-1 downto 8*i);
    end loop;
when X"04" =>  -- 整字写（忽略字节使能）
    OtherReg <= Rb_WrData;
when X"08" =>  -- 写1清零
    VectorReg <= VectorReg and not Rb_WrData;
```

读处理见 [doc/axi/slave/RbExample.vhd:22-39](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/axi/slave/RbExample.vhd#L22-L39)，注意 `0x04` 的「读时清零」——读出旧值的同时把寄存器清零：

```vhdl
Rb_RdValid <= '0'; -- Default value
if Rb_Rd = '1' then
    case Rb_Addr is
        when X"04" =>
            Rb_RdData <= OtherReg;          -- 返回当前值
            OtherReg  <= (others => '0');   -- 同时清零（读后清）
            Rb_RdValid <= '1';
        ...
        when others => null; -- 非法地址：Fail by timeout
    end case;
end if;
```

最后回顾从机内部的复位策略，见 [src/axi/vhdl/olo_axi_lite_slave.vhd:232-247](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_lite_slave.vhd#L232-L247)：复位只清状态位（`State`、各 `Ready`、`Wr`、`Rd`、`BValid`、`RValid`），不清数据通路（`Addr`/`WrData`/`RData` 等）。这正是 [u2-l2](u2-l2-pipeline-stage-handshake.md) 与 [u1-l5](u1-l5-conventions-and-anatomy.md) 讲过的「只复位状态、降低复位扇出」约定——你的寄存器组也应照此办理，只复位需要确定初值的控制/状态寄存器。

#### 4.5.4 代码实践

**目标**：把 `RbExample` 的 `0x00`（带字节使能）和 `0x04`（整字、读时清零）两个模式，浓缩成一个「2 个可读写寄存器」的最小寄存器组。

1. 仿照 [doc/axi/slave/RbExample.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/axi/slave/RbExample.vhd) 写一个 `p_rb` 进程，只保留 `0x00`（带字节使能的 `RegA`）和 `0x04`（整字、读时不清零的 `RegB`）两个寄存器。
2. 思考：`RegB` 若不想「读时清零」，相比 `RbExample` 要删掉哪一行？
3. 完整的连线与仿真见第 5 节综合实践。

**预期结果**：你应当能写出一段不到 30 行、覆盖两个寄存器读写的 `p_rb` 进程。

#### 4.5.5 小练习与答案

**练习 1**：「写 1 清零」寄存器（`0x08`）为什么用 `VectorReg and not Rb_WrData`，而不是 `VectorReg <= Rb_WrData`？

**参考答案**：写 1 清零（write-1-to-clear）是中断挂起寄存器的典型语义——CPU 向某位写 1 表示「清除该中断」，写 0 表示「不动」。`and not Rb_WrData` 恰好实现「写 1 的位被清零、写 0 的位保持」。若直接赋值 `Rb_WrData`，就成了普通寄存器，失去「只清不清」的语义。

**练习 2**：把一块 1 KB 的 RAM 挂到 Rb 接口，`Rb_Addr` 的哪几位用作 RAM 字下标（设 `AxiDataWidth_g=32`）？

**参考答案**：32 位 = 4 字节，故 `Rb_Addr` 的低 2 位是字内字节偏移（写时已被屏蔽），高位 `Rb_Addr(高 downto 2)` 才是 RAM 的字下标。1 KB = 256 个 32 位字，需要 8 位字下标。

---

## 5. 综合实践

**任务**：参照 `RbExample.vhd` 设计一个含 **2 个可读写寄存器**的寄存器组，挂到 `olo_axi_lite_slave` 上，并用 AXI 主机验证组件（VC）仿真验证读写正确。

这个任务把本讲的全部模块串起来：地址映射（4.2）、写接入（4.3）、读接入（4.4）、寄存器组模式（4.5）。最省力的做法是**改造官方测试台**——它已经搭好了时钟、复位、AXI 主机 VC，你只需把「测试台里手动驱动 `Rb_RdData`/`Rb_RdValid` 的那几行」替换成「一个真正的寄存器组进程」。

### 5.1 操作步骤

1. **复制样板**：把 [test/axi/olo_axi_lite_slave/olo_axi_lite_slave_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_lite_slave/olo_axi_lite_slave_tb.vhd) 复制为一份新的测试台文件（例如 `my_lite_slave_tb.vhd`），放进自己的工作目录。注意它 `-- vunit: run_all_in_same_sim` 的注释行和 `runner_cfg` generic 是 VUnit 约定，要保留。

2. **新增寄存器组进程**：在 architecture 里加两个信号和一段进程（**示例代码**）：

   ```vhdl
   -- 示例代码：2 个可读写寄存器
   signal RegA : std_logic_vector(AxiDataWidth_g-1 downto 0) := (others => '0');
   signal RegB : std_logic_vector(AxiDataWidth_g-1 downto 0) := (others => '0');

   p_rb : process(Clk) is
   begin
       if rising_edge(Clk) then
           -- 写：0x00 带字节使能，0x04 整字
           if Rb_Wr = '1' then
               case Rb_Addr is
                   when X"00" =>
                       for i in 0 to (AxiDataWidth_g/8)-1 loop
                           if Rb_ByteEna(i) = '1' then
                               RegA(8*(i+1)-1 downto 8*i) <= Rb_WrData(8*(i+1)-1 downto 8*i);
                           end if;
                       end loop;
                   when X"04" =>
                       RegB <= Rb_WrData;
                   when others => null;
               end case;
           end if;
           -- 读：默认 RdValid=0，命中则回数据
           Rb_RdValid <= '0';
           if Rb_Rd = '1' then
               case Rb_Addr is
                   when X"00" => Rb_RdData <= RegA; Rb_RdValid <= '1';
                   when X"04" => Rb_RdData <= RegB; Rb_RdValid <= '1';
                   when others => null; -- 触发读超时
               end case;
           end if;
           -- 复位
           if Rst = '1' then
               RegA <= (others => '0');
               RegB <= (others => '0');
           end if;
       end if;
   end process;
   ```

   > 关键改动：原测试台由 `p_control` 进程**手动**给 `Rb_RdData`/`Rb_RdValid` 赋值（见 [olo_axi_lite_slave_tb.vhd:147-175](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_lite_slave/olo_axi_lite_slave_tb.vhd#L147-L175)）；现在改成由 `p_rb` 进程**自动**驱动，于是 `Rb_RdData`/`Rb_RdValid` 不再是测试台直接驱动的信号，而要由你的寄存器组驱动（注意 VHDL 不能两个源驱动同一信号，需删掉 `p_control` 里对它们的手动赋值）。

3. **编写一个验证用例**：参照 [olo_axi_lite_slave_tb.vhd:134-144](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_lite_slave/olo_axi_lite_slave_tb.vhd#L134-L144) 的 `SingleWrite` 与 `SingleReads`，写一段：

   ```vhdl
   -- 示例代码：验证用例骨架
   if run("MyRegTest") then
       -- 写 RegB = 0xCAFE，再读回
       push_single_write(net, AxiMaster_c, to_unsigned(16#04#, AddrWidth_c), X"CAFE");
       expect_single_read (net, AxiMaster_c, to_unsigned(16#04#, AddrWidth_c), X"CAFE");
   end if;
   ```

   `push_single_write` 与 `expect_single_read` 的接口见 [test/tb/olo_test_axi_master_vc.vhd:98-117](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/tb/olo_test_axi_master_vc.vhd#L98-L117)：前者发起一次写（`addr`/`data` 用 `unsigned`），后者发起一次读并**自动比对**读回值——若 `RegB` 不是 `0xCAFE`，`expect_single_read` 会报错。

4. **注册并运行**：参考 [sim/test_configs/olo_axi.py:21-26](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_axi.py#L21-L26) 把你的新测试台注册到 VUnit，然后在 `sim/` 目录运行（详见 [u1-l4](u1-l4-run-first-simulation.md) 与 [u10-l2](u10-l2-sim-runner-codegen-config.md)）：

   ```bash
   cd sim
   python3 run.py --ghdl    # 默认 GHDL；也可 --nvc / --modelsim
   ```

   可用 `*MyRegTest*` 之类过滤器只跑你的用例。

### 5.2 需要观察的现象

- **写生效**：`push_single_write` 后，`Rb_Wr` 应出现一个单周期脉冲，`Rb_Addr=0x04`，`RegB` 被更新为 `0xCAFE`。
- **读回一致**：`expect_single_read` 不报错，说明 `Rb_Rd` 脉冲→`Rb_RdValid` 应答→AXI R 通道数据全链路正确。
- **字节使能**（可选）：对 `0x00` 用 `strb => "0010"` 只写第 1 字节，再读回，应只有该字节变化。

### 5.3 预期结果

仿真正常结束时 VUnit 报告所有用例通过（`MyRegTest` 为绿色）。若 `expect_single_read` 报「值不匹配」，多半是 `p_rb` 的读译码写错了地址常量，或忘了在 `Rb_Rd` 命中时拉高 `Rb_RdValid`。

> 若你无法本地运行仿真（缺 GHDL/VUnit 环境），本实践也可降级为**源码阅读型实践**：对照 `RbExample.vhd` 与测试台，在纸上追踪「`push_single_write(0x04, 0xCAFE)`」从 AXI 五通道一路到 `RegB`、再从 `RegB` 一路到 `expect_single_read` 比对成功」的完整数据流，画出每一拍的信号波形。仿真层面的逐拍结果**待本地验证**。

## 6. 本讲小结

- `olo_axi_lite_slave` 是一座**协议翻译桥**：把 AXI4-Lite 五通道翻译成极简的 Rb 接口（`Rb_Addr` + 读/写脉冲 + 数据），自身不存任何寄存器。
- **地址映射**采用「桥只搬运、用户自译码」：写地址做 `log2(字节数)` 位屏蔽以保证字对齐，译码（`case Rb_Addr`）完全交给用户代码。
- **写通路**：`WrData` 状态产生单周期 `Rb_Wr` 脉冲，透传 `Rb_WrData` 与字节使能 `Rb_ByteEna`；写无应答、恒回 OKAY，用户代码必须实时跟进。
- **读通路**：`RdCmd` 产生单周期 `Rb_Rd` 脉冲，用户必须用 `Rb_RdValid`（+ `Rb_RdData`）应答，延迟可变；超时 `ReadTimeoutClks_g` 未应答则回 `SLVERR`，防止主机挂死。
- **寄存器组模式**由 `RbExample.vhd` 示范：带字节使能写、整字写、写 1 清零、读时清零——存储接入与寄存器接入共用同一套 Rb 接口。
- 复位遵循全库约定：只复位状态位、不复位数据通路；该实体用单 FSM 串行处理读写，每笔事务约 4 拍。

## 7. 下一步学习建议

- **继续 AXI 主机**：本讲的验证组件用到了 AXI 主机 VC，下一讲 [u6-l3 AXI4 简单主机](u6-l3-axi-master-simple.md) 会从「被驱动方」翻到「驱动方」，讲解 `olo_axi_master_simple` 如何发起 AXI 突发读写。
- **深入测试台与 VC**：若你对综合实践里的 VUnit 用法感兴趣，可跳读 [u10-l1 VUnit 测试台结构与验证组件](u10-l1-vunit-tb-and-vcs.md)，系统了解 `runner_cfg`、`run_all_in_same_sim` 与 VC 命名约定。
- **扩展阅读**：阅读 [doc/axi/olo_axi_lite_slave.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/axi/olo_axi_lite_slave.md) 的写/读时序图，对照本讲的 FSM 讲解，把「文字描述」与「波形图」一一对应起来。
