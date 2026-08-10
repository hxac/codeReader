# AXI4-Lite 从机接口实现

## 1. 本讲目标

本讲是 Unit 3（AXI-Lite IP 封装与软硬件协同）的第二讲，承接 u3-l1 讲过的「IP 封装骨架」。在 u3-l1 中我们已确认：`AesCryptoCore_v1_0.v` 这个顶层包装例化了两个 AXI 从机子模块，其中 `S00_AXI` 是「寄存器从机」——目前它只是一个能让处理器读写 4 个 32 位寄存器的「空壳」，AES 算法核心尚未接入。

学完本讲，你应当能够：

1. 说出 AXI4-Lite 协议的 **五个通道**（AW/W/B/AR/R）以及每个通道负责什么。
2. 解释 **VALID/READY 握手** 规则，并能判断某一拍是否发生了一次传输。
3. 看懂 `AesCryptoCore_v1_0_S00_AXI.v` 如何用 `aw_en` 这个「单事务屏障」保证一次只处理一笔写事务。
4. 理解 32 位寄存器的 **地址译码** 与 **字节写使能（WSTRB）** 机制。
5. 识别代码里的 `// Add user logic here` 接入点，知道日后要把 AES 核心挂在哪里。
6. 手画一次完整写事务（AW+W+B）和读事务（AR+R）的时序波形图。

---

## 2. 前置知识

本讲默认你已经学过：

- **u1-l3**：Vivado 工程模板、Tcl 重建工程、`ip_repo` 目录的作用。
- **u2-l1**：Verilog 基础（`wire`/`reg`、`always`、非阻塞 `<=`、模块例化）。
- **u3-l1**：Vivado 自定义 IP 的 `component.xml`、顶层包装 `AesCryptoCore_v1_0.v`、VLNV 标识、以及「封装层与算法层目前脱节」这一现状。

在进入源码之前，先用大白话建立三个直觉：

### 直觉一：为什么要有「总线」？

FPGA 里常常有一个「主」（处理器，比如 Zynq 的 ARM 核）和很多「从」（你自己写的硬件加速器）。主和从之间需要一套**约定好的电线和规则**来读写寄存器，这套约定就叫**总线协议**。AXI4-Lite 就是 ARM 设计的一套「轻量级读写寄存器」的总线协议（Lite = 简化版，每次只搬一个数据，不能像完整 AXI4 那样批量突发传输）。

### 直觉二：VALID/READY 握手

AXI 最核心的规则只有一条：**任何一次传输，都必须发送方和接收方同时说「我准备好了」才算数。**

- 发送方把 `VALID` 拉高，表示「我的数据/地址是有效的」。
- 接收方把 `READY` 拉高，表示「我能接收」。
- 只有当 `VALID` 和 `READY` **在同一个时钟上升沿都为 1**，这一次传输才成立，数据才被接收。否则双方继续等。

这就好比递交一份文件：递交人（VALID）和接收人（READY）必须同时在场，文件才能真正交出去。

> 关键约束：发送方一旦把 `VALID` 拉高，在收到 `READY` 之前**不能撤回**，地址/数据也必须保持不变。这是 AXI 协议的硬性规定。

### 直觉三：为什么是「五个通道」？

一次完整的「写」操作，主设备要告诉从设备三件事：**写到哪里（地址）、写什么（数据）、收到了吗（回应）**。AXI 把这三件事拆成三条独立的「通道」，每条通道都有自己的 VALID/READY：

- **AW 通道**（Address Write）：传写地址。
- **W 通道**（Write Data）：传写数据。
- **B 通道**（B = response）：从设备回一个「写完了」的回应。

类似地，一次「读」拆成两条通道：

- **AR 通道**（Address Read）：传读地址。
- **R 通道**（Read Data）：从设备回数据 + 回应。

合起来就是五个通道：**AW、W、B、AR、R**。本讲后面会反复出现这五个名字。

> 名词速查：
> - **主（Master）/从（Slave）**：发起事务的一方叫主，响应的一方叫从。本讲的 `S00_AXI` 模块是「从」（名字里的 `S_` = Slave）。
> - **事务（Transaction）**：一次完整的读或写。
> - **握手（Handshake）**：VALID 与 READY 同时为高的那一拍。

---

## 3. 本讲源码地图

本讲只聚焦两个文件，它们都在 `ip_repo/AesCryptoCore_1.0/hdl/` 目录下：

| 文件 | 作用 | 本讲扮演的角色 |
|------|------|----------------|
| `AesCryptoCore_v1_0.v` | IP 顶层包装 | 「外壳」：把 AXI 端口扁平暴露，例化两个从机子模块。本讲主要看它如何例化 `S00_AXI`。 |
| `AesCryptoCore_v1_0_S00_AXI.v` | AXI4-Lite 寄存器从机 | 「主角」：实现五个通道握手、4 个寄存器读写、留出用户逻辑接入点。 |

回顾 u3-l1 的结论：这两个文件都属于**封装层**（`ip_repo/.../hdl/`），与算法层（`hdl/src/aes_top.v` 等）目前是**脱节**的。也就是说，`S00_AXI` 现在能被处理器读写 4 个寄存器，但寄存器后面什么都没接——这正是本讲要讲清楚、并在最后讨论「如何接上 AES」的起点。

> 提示：这两个文件是 Vivado「Create and Package IP → AXI4 Peripheral」向导**自动生成**的模板代码。所以你会看到很多格式统一的注释、`// Users to add parameters here`、`// Add user logic here` 之类的标记——它们是留给开发者填空的「占位符」。理解这一点后，代码的「死板」就变得合理了。

---

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：

- 4.1 AXI4-Lite 协议与五通道握手模型
- 4.2 写事务：AW/W 通道握手与 `aw_en` 单事务屏障
- 4.3 写寄存器、写回应与读事务的实现
- 4.4 寄存器地址映射与字节使能（WSTRB）
- 4.5 `Add user logic here`：把 AES 核心挂上去

### 4.1 AXI4-Lite 协议与五通道握手模型

#### 4.1.1 概念说明

AXI4-Lite 是 AXI 协议的「精简版」，专门用于**内存映射寄存器（memory-mapped register）**的读写。所谓「内存映射」，就是把硬件里的一组寄存器，每个都分配一个地址；处理器只要像读写内存一样「往某个地址写一个字 / 读某个地址」，就能控制硬件。这正是嵌入式系统（如 Zynq SoC）里「软件控制硬件加速器」的标准做法。

AXI4-Lite 的全部信号就是围绕前述五个通道组织的，每个通道都遵循 VALID/READY 握手。下表把五个通道、它们的方向（从**从机**视角看）和本模块里的信号名列出来：

| 通道 | 功能 | 从机视角的信号 | 本模块信号名 |
|------|------|----------------|--------------|
| AW | 写地址 | `AWADDR/AWPROT`（输入）、`AWVALID`（输入）、`AWREADY`（输出） | `S_AXI_AWADDR` 等 |
| W | 写数据 | `WDATA/WSTRB`（输入）、`WVALID`（输入）、`WREADY`（输出） | `S_AXI_WDATA` 等 |
| B | 写回应 | `BRESP`（输出）、`BVALID`（输出）、`BREADY`（输入） | `S_AXI_BRESP` 等 |
| AR | 读地址 | `ARADDR/ARPROT`（输入）、`ARVALID`（输入）、`ARREADY`（输出） | `S_AXI_ARADDR` 等 |
| R | 读数据 | `RDATA/RRESP`（输出）、`RVALID`（输出）、`RREADY`（输入） | `S_AXI_RDATA` 等 |

除了五个通道，还有两个全局信号：时钟 `S_AXI_ACLK` 和复位 `S_AXI_ARESETN`（**低电平有效**，名字里的 `N` = active low，这点很重要，后面所有复位判断都写 `S_AXI_ARESETN == 1'b0`）。

`BRESP`/`RRESP` 是 2 位的回应码，本模块永远回 `2'b00`（OKAY = 正常完成），不产生错误回应。

#### 4.1.2 核心流程

五个通道的协作关系如下（以一次「写」为主线）：

```text
主设备发起一次写：
  ①  主 → AW 通道：给地址，AWVALID↑
  ②  主 → W 通道：给数据，WVALID↑
  ③  从机同时看到 AWVALID 和 WVALID 后，分别在 AW/W 通道回 AWREADY/WREADY（握手成立）
  ④  从机把数据写进对应地址的寄存器
  ⑤  从机 → B 通道：BVALID↑，回 BRESP=OKAY
  ⑥  主 → B 通道：BREADY↑，表示「收到你的回应了」，事务结束

一次「读」类似：
  ①  主 → AR 通道：给地址，ARVALID↑
  ②  从机回 ARREADY（握手成立），锁存地址
  ③  从机 → R 通道：把该地址寄存器的值放到 RDATA，RVALID↑，RRESP=OKAY
  ④  主 → R 通道：RREADY↑，取走数据，事务结束
```

注意本模板的一个**简化设计**：写地址（AW）和写数据（W）必须在**同一拍**都被主设备置为 VALID，从机才会一起接收（见 4.2）。这比「AW 和 W 可以任意先后到达」的标准 AXI 行为要严格，但实现简单。

#### 4.1.3 源码精读

模块声明与两个关键参数在文件开头：

[AesCryptoCore_v1_0_S00_AXI.v:4-15](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L4-L15) —— 模块名 `AesCryptoCore_v1_0_S00_AXI`，参数 `C_S_AXI_DATA_WIDTH=32`（数据 32 位）、`C_S_AXI_ADDR_WIDTH=4`（地址 4 位）。地址 4 位意味着地址空间只有 \(2^4 = 16\) 字节，刚好够放 4 个 32 位寄存器（\(4 \times 4 = 16\)）。

五个通道的全部端口声明在这里：

[AesCryptoCore_v1_0_S00_AXI.v:22-82](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L22-L82) —— 依次列出时钟/复位（22-25）、AW 通道（26-37）、W 通道（38-49）、B 通道（50-58）、AR 通道（59-70）、R 通道（71-81）。每个信号的注释都写得很清楚（哪个是主发、哪个是从发）。

随后是一段「端口连线赋值」，把内部 `reg`（如 `axi_awready`）接到对外端口（如 `S_AXI_AWREADY`）：

[AesCryptoCore_v1_0_S00_AXI.v:117-126](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L117-L126) —— 这 8 行 `assign` 把内部寄存器原样输出。Vivado 模板习惯把核心逻辑写在内部 `axi_*` 信号上，再用 `assign` 对外暴露，便于阅读。

#### 4.1.4 代码实践

**实践目标**：练就从机视角判断信号方向的能力，这是读懂任何 AXI 代码的前提。

**操作步骤**：

1. 打开 [AesCryptoCore_v1_0_S00_AXI.v:22-82](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L22-L82)。
2. 对每个通道，分别列出：哪些是 `input wire`（从机的输入，即主设备的输出）、哪些是 `output wire`。
3. 特别留意：每个通道的 `VALID` 和 `READY` 各自在哪一边。

**需要观察的现象 / 预期结果**：你会发现一个规律——

- `*_VALID` 的方向总是「发起方」：AW/W/AR 通道的 VALID 是 `input`（主发），B/R 通道的 VALID 是 `output`（从发）。
- `*_READY` 的方向总是「接收方」：AW/W/AR 通道的 READY 是 `output`（从机接收），B/R 通道的 READY 是 `input`（主接收）。

记住「VALID 跟着发起方走，READY 跟着接收方走」这句口诀，就能快速判断任意 AXI 信号方向。

#### 4.1.5 小练习与答案

**练习 1**：`S_AXI_BRESP` 是输入还是输出？它由谁产生？

**参考答案**：是 `output wire [1:0]`（见 [L52](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L52)）。它由**从机**产生，是 B 通道里从机回给主设备的「写事务状态码」，本模块恒为 `2'b00`（OKAY）。

**练习 2**：地址宽度是 4 位，为什么说「刚好够放 4 个 32 位寄存器」？

**参考答案**：4 位地址 → \(2^4 = 16\) 字节空间；每个 32 位寄存器占 4 字节，\(16 / 4 = 4\)，所以最多 4 个寄存器。

---

### 4.2 写事务：AW/W 通道握手与 `aw_en` 单事务屏障

#### 4.2.1 概念说明

写事务的第一步，是主设备在 AW 通道给出地址、在 W 通道给出数据，从机要把它们「收下」。这一节聚焦两个问题：

1. **`AWREADY` 和 `WREADY` 什么时候拉高？** —— 当从机看到地址和数据都有效时，拉高**一拍**表示「我收下了」。
2. **为什么需要一个额外的标志位 `aw_en`？** —— 为了保证「同一时刻只有一笔未完成的写事务」。

`aw_en` 是本模板里最巧妙（也最容易看漏）的一个信号。它的作用像一个**门禁**：收下一笔写事务后就把门关上（`aw_en=0`），直到这笔写事务的 B 通道回应被主设备确认（`BREADY` 握手），才重新开门（`aw_en=1`）允许下一笔。这就避免了「上一笔还没回应，下一笔又来了」导致的寄存器/地址错乱。代码注释把这一点总结为 *"This design expects no outstanding transactions"*（本设计不允许有未完成的事务）。

#### 4.2.2 核心流程

`AWREADY` 与 `aw_en` 的协作（状态机式描述）：

```text
复位（ARESETN=0）：AWREADY=0, aw_en=1（门开着）

每拍判断：
  if (AWREADY==0 且 AWVALID==1 且 WVALID==1 且 aw_en==1):
      → 收下这笔写！AWREADY<=1（拉高一拍），aw_en<=0（关门）
  else if (BREADY==1 且 BVALID==1):
      → 上一笔的回应被确认了，aw_en<=1（重新开门），AWREADY<=0
  else:
      → AWREADY<=0
```

关键点：`AWREADY`/`WREADY` 都只拉高**一个时钟周期**（脉冲式握手），收完即落。而 `aw_en` 是一个持续的门禁状态，跨度可能多个周期。

#### 4.2.3 源码精读

`AWREADY` 与 `aw_en` 的生成（本讲最重要的一段逻辑）：

[AesCryptoCore_v1_0_S00_AXI.v:127-160](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L127-L160) ——
- 复位时 `aw_en<=1`、`axi_awready<=0`（L136-137）。
- 收写条件 `~axi_awready && S_AXI_AWVALID && S_AXI_WVALID && aw_en`（L141）：注意它**同时**要求 AW 和 W 两个通道的 VALID，这就是 4.1.2 提到的「地址数据必须同拍到达」的简化设计。
- 满足后 `axi_awready<=1; aw_en<=0`（L147-148）：拉高 AWREADY 一拍，同时关门。
- 关门期间若 `S_AXI_BREADY && axi_bvalid`（L150）：回应已确认，重新 `aw_en<=1` 开门。

地址锁存：

[AesCryptoCore_v1_0_S00_AXI.v:162-180](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L162-L180) —— 在与上面相同的「收写条件」下，把 `S_AXI_AWADDR` 锁存进内部 `axi_awaddr`（L177）。锁存是因为：真正写寄存器发生在稍后一拍，地址必须留住。

`WREADY` 的生成：

[AesCryptoCore_v1_0_S00_AXI.v:182-208](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L182-L208) —— 条件几乎与 `AWREADY` 相同（L195），同样拉高一拍。所以正常情况下 `AWREADY` 和 `WREADY` 在同一拍为高，AW/W 通道同时握手。

#### 4.2.4 代码实践

**实践目标**：理解 `aw_en` 的「门禁」作用，验证它确实把写事务串行化。

**操作步骤**：

1. 阅读 [L127-160](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L127-L160)。
2. 做一个**思想实验**：假设没有 `aw_en`（把它恒当 1），主设备连续两拍都置 `AWVALID=WVALID=1`，会发生什么？地址 `axi_awaddr` 会不会在寄存器还没写完时被新地址覆盖？
3. 再带着 `aw_en` 重新走一遍：第一笔写之后 `aw_en=0`，即使主设备继续置 VALID，第二个 `if` 分支也不会再触发，直到 B 通道握手把它重新置 1。

**需要观察的现象 / 预期结果**：

- **去掉 `aw_en` 的假想**：`AWREADY` 可能连续两拍为高，`axi_awaddr` 会被第二次覆盖，导致第一笔写的地址丢失——这是 bug。
- **有 `aw_en` 的实际**：`AWREADY` 只在第一笔握手时拉高一拍，之后被锁住，必须等 B 通道确认才解锁。`aw_en` 是「防重入」的关键。

> 本实践为源码阅读型实践，无需运行；若要验证可在仿真里连续发两笔写事务，观察 `aw_en` 的翻转。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `AWREADY` 只拉高一拍，而不是一直保持高？

**参考答案**：因为 AXI 握手是「逐拍成交」的。`AWREADY` 拉高的那一拍若 `AWVALID` 也为高，地址就被收下，这一笔地址传输结束。如果 `AWREADY` 一直高，而主设备只发一个地址，那么在主设备还没撤回 `AWVALID` 的多个拍里，从机会误以为收到多个地址。

**练习 2**：如果把 `aw_en` 的初值改成 `1'b0`（复位时关门），系统会怎样？

**参考答案**：复位后 `aw_en=0`，第一个 `if` 永远不满足（需要 `aw_en==1`），而第二个 `if` 又依赖 `axi_bvalid`（此时也是 0），于是 `aw_en` 永远卡在 0，**任何写事务都进不来**——从机「死锁」。这说明 `aw_en` 复位初值为 1 是必须的。

---

### 4.3 写寄存器、写回应与读事务的实现

#### 4.3.1 概念说明

地址和数据被收下后，要做三件后续工作：

1. **写寄存器**：根据锁存的地址，把数据写进 `slv_reg0~3` 中的某一个；并且要尊重**字节写使能** `WSTRB`（哪个字节才真写）。
2. **回写回应（B 通道）**：写完后拉高 `BVALID`，告诉主设备「写完了」。
3. **读事务**：读路径没有 `aw_en` 那样的门禁，结构更简单——锁存读地址、用组合逻辑选出寄存器值、打一拍寄存器输出到 `RDATA`、拉高 `RVALID`。

读路径用到了一个常见技巧：**组合选择 + 寄存器输出**。即先用一个纯组合的 `always @(*)` 根据地址选出某个寄存器的值（`reg_data_out`），再用一个时钟进程把它打进 `axi_rdata` 寄存器。这样既保证了读数据的稳定（寄存器输出无毛刺），又把选择逻辑和时序逻辑分开。

#### 4.3.2 核心流程

**写寄存器 + B 回应**：

```text
写使能：slv_reg_wren = WREADY & WVALID & AWREADY & AWVALID  （组合）
  当 slv_reg_wren==1 时，根据 axi_awaddr[3:2] 选择：
      2'h0 → 写 slv_reg0（按 WSTRB 逐字节）
      2'h1 → 写 slv_reg1
      2'h2 → 写 slv_reg2
      2'h3 → 写 slv_reg3
  同时 B 通道：BVALID<=1, BRESP<=OKAY
  主设备确认 BREADY 后：BVALID<=0，并（经 aw_en）解锁下一笔
```

**读事务**：

```text
读使能：slv_reg_rden = ARREADY & ARVALID & ~RVALID  （组合）
  组合选择：根据 axi_araddr[3:2] 把 slv_reg0~3 之一送到 reg_data_out
  打拍：axi_rdata <= reg_data_out
  同时 R 通道：RVALID<=1, RRESP<=OKAY
  主设备确认 RREADY 后：RVALID<=0
```

#### 4.3.3 源码精读

写使能（组合）：

[AesCryptoCore_v1_0_S00_AXI.v:217](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L217) —— `assign slv_reg_wren = axi_wready && S_AXI_WVALID && axi_awready && S_AXI_AWVALID;`。只有 AW 和 W **同时**握手的那一拍，写使能才为 1。

寄存器写入（含字节使能与地址译码）：

[AesCryptoCore_v1_0_S00_AXI.v:219-269](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L219-L269) ——
- 复位时 `slv_reg0~3` 清零（L222-226）。
- `case (axi_awaddr[ADDR_LSB+OPT_MEM_ADDR_BITS:ADDR_LSB])`（L231）做地址译码，bits `[3:2]` 选 4 个寄存器（详见 4.4）。
- 每个分支里用 `for` 循环逐字节判断 `S_AXI_WSTRB[byte_index]==1` 才写对应字节（L233-238 等）。
- `default` 分支保持原值（L260-265），防止综合出锁存器（latch）。

B 回应生成：

[AesCryptoCore_v1_0_S00_AXI.v:271-302](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L271-L302) —— 当 `axi_awready && AWVALID && ~axi_bvalid && axi_wready && WVALID`（L286）同时成立，置 `axi_bvalid<=1; axi_bresp<=2'b00`（OKAY）；主设备 `BREADY` 确认后置 `axi_bvalid<=0`（L298）。

读地址锁存：

[AesCryptoCore_v1_0_S00_AXI.v:304-332](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L304-L332) —— 看到 `~axi_arready && S_AXI_ARVALID`（L320）就拉高 `axi_arready` 一拍并锁存 `axi_araddr`。

读有效 + 读回应：

[AesCryptoCore_v1_0_S00_AXI.v:334-363](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L334-L363) —— `axi_arready && ARVALID && ~axi_rvalid`（L351）成立时置 `axi_rvalid<=1; axi_rresp<=OKAY`；`axi_rvalid && RREADY`（L357）时撤回 `axi_rvalid`。

读选择（组合）+ 读数据寄存器输出：

[AesCryptoCore_v1_0_S00_AXI.v:365-398](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L365-L398) —— `slv_reg_rden`（L368）触发组合 `case` 选出 `reg_data_out`（L372-378），再由时钟进程把 `reg_data_out` 打进 `axi_rdata`（L395）。这正是「组合选择 + 寄存器输出」范式。

#### 4.3.4 代码实践

**实践目标**：用仿真确认「读数据在 AR 握手后约一拍出现在 RDATA 上」。

**操作步骤**：

1. 阅读 [L365-398](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L365-L398)。
2. 先写一笔把 `0xDEADBEEF` 写进 `slv_reg2`（地址 `0x8`），再发一笔读地址 `0x8`。
3. 数清楚：从 `ARVALID` 与 `ARREADY` 同时为高的那一拍算起，`RVALID` 和 `RDATA` 是在下一拍还是当拍出现？

**需要观察的现象 / 预期结果**：

- AR 握手在 T 拍；`slv_reg_rden` 在 T 拍为 1（组合），`reg_data_out` 在 T 拍等于 `slv_reg2=0xDEADBEEF`；`axi_rvalid` 与 `axi_rdata` 在 T+1 拍才生效。
- 所以读延迟约为 **1 个时钟周期**（从 AR 握手到 R 通道数据可见）。
- 若无仿真环境，本结论可由 [L393-396](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L393-L396) 的「打一拍」结构直接推断，标注「待本地验证」的具体波形。

#### 4.3.5 小练习与答案

**练习 1**：写路径用 `aw_en` 串行化，读路径却没有类似机制。为什么读路径不需要？

**参考答案**：因为读路径本身就用 `~axi_rvalid` 作为门禁（[L351](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L351) 和 [L368](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L368)）。只要上一笔读的 `RVALID` 还没被 `RREADY` 收走，新的 `ARVALID` 就不会被再次接收（条件里有 `~axi_rvalid`），自然实现了「一次只处理一笔」。写路径之所以额外加 `aw_en`，是因为 B 通道回应是**之后**才产生的，需要一个独立标志记住「上一笔还没回完」。

**练习 2**：`BRESP`/`RRESP` 能不能出现 `2'b10`（SLVERR）？

**参考答案**：在本模块里**不能**。代码里它们只被赋值为 `2'b00`（OKAY），没有任何路径把它们设成错误码。这是模板的简化：它假设所有地址都合法、所有访问都能成功。如果你的 IP 需要报错（比如访问不存在的寄存器），得自己加 SLVERR 逻辑。

---

### 4.4 寄存器地址映射与字节使能（WSTRB）

#### 4.4.1 概念说明

「寄存器映射」回答的是：**处理器看到的地址** ↔ **硬件里的哪个寄存器**。本模块有 4 个 32 位寄存器 `slv_reg0~3`，它们被依次排在地址空间里：

| 处理器地址 | 地址 bit[3:2] | 对应寄存器 |
|-----------|---------------|-----------|
| `0x0` | `00` | `slv_reg0` |
| `0x4` | `01` | `slv_reg1` |
| `0x8` | `10` | `slv_reg2` |
| `0xC` | `11` | `slv_reg3` |

为什么用 `bit[3:2]` 来选？因为：

- 32 位 = 4 字节，所以**地址的最低 2 位 `bit[1:0]`** 是「字内字节偏移」，用来定位一个字里的哪个字节——它们不参与选寄存器。代码里把这个分界线叫 `ADDR_LSB`，值为 2。
- 4 个寄存器需要 2 位来区分，所以再往上取 2 位 `bit[3:2]`。代码里这「再往上取几位」由 `OPT_MEM_ADDR_BITS` 控制，值为 1（即再取 \(1+1=2\) 位）。

**字节使能 WSTRB** 解决另一个问题：处理器可能只想改一个 32 位寄存器里的**某几个字节**，而不是整个字。`WSTRB` 是 4 位（每个 32 位字对应 4 个字节），哪一位为 1，对应字节才被真正写入。例如 `WSTRB=4'b0011` 表示只写低 2 字节、高 2 字节保持不变。

#### 4.4.2 核心流程

地址译码与字节写入的数学关系：

- 寄存器索引 = `addr[ADDR_LSB + OPT_MEM_ADDR_BITS : ADDR_LSB]` = `addr[3:2]`
- 字节写掩码：对 `byte_index = 0..3`，当 `WSTRB[byte_index]==1` 时，把 `WDATA` 的第 `byte_index` 个字节写入寄存器对应字节。

把 32 位地址空间画出来：

\[
\text{地址空间} = 2^{\text{ADDR\_WIDTH}} = 2^4 = 16 \text{ 字节}
\]

\[
\text{可用寄存器数} = \frac{16}{4} = 4
\]

\[
\text{索引位数} = \log_2 4 = 2 = \text{OPT\_MEM\_ADDR\_BITS} + 1
\]

#### 4.4.3 源码精读

`ADDR_LSB` 与 `OPT_MEM_ADDR_BITS` 的定义：

[AesCryptoCore_v1_0_S00_AXI.v:97-102](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L97-L102) ——
- `ADDR_LSB = (C_S_AXI_DATA_WIDTH/32) + 1`：32 位时 = (32/32)+1 = **2**；64 位时 = 3。注释解释了它的含义。
- `OPT_MEM_ADDR_BITS = 1`：决定寄存器数量的「上限位数」——\(2^{(1+1)} = 4\) 个寄存器。如果想要 8 个寄存器，就把它改成 2。

写路径的地址译码：

[AesCryptoCore_v1_0_S00_AXI.v:231](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L231) —— `case (axi_awaddr[ADDR_LSB+OPT_MEM_ADDR_BITS:ADDR_LSB])` 即 `case (axi_awaddr[3:2])`，4 个分支分别写 `slv_reg0~3`。

字节写使能循环（以 reg0 为例）：

[AesCryptoCore_v1_0_S00_AXI.v:232-238](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L232-L238) —— `for` 循环遍历 4 个字节，`if (S_AXI_WSTRB[byte_index]==1)` 时才把 `S_AXI_WDATA[(byte_index*8) +: 8]` 写入 `slv_reg0` 对应字节。`+:` 是 Verilog 的「位宽切片」写法，`[base +: 8]` 表示从 `base` 起 8 位。

读路径的地址译码（组合）：

[AesCryptoCore_v1_0_S00_AXI.v:372](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L372) —— 读用 `axi_araddr[3:2]` 选，把对应寄存器值送到 `reg_data_out`。读是整字读，没有字节使能概念。

#### 4.4.4 代码实践

**实践目标**：掌握「地址 → 寄存器」与「WSTRB → 字节」的两层映射。

**操作步骤**：

1. 静态推演：处理器发起一次写，`AWADDR=0x8`、`WDATA=0x000000AB`、`WSTRB=4'b0001`。问：写到哪个寄存器？写了哪些位？
2. 再推演：`AWADDR=0x4`、`WDATA=0x11223344`、`WSTRB=4'b1100`，写了哪些字节？
3. 对照 [L231-258](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L231-L258) 验证你的答案。

**需要观察的现象 / 预期结果**：

- 情况 1：`0x8` 的 `bit[3:2]=10` → `slv_reg2`；`WSTRB=0001` 只有 byte0 写 → 只有 `slv_reg2[7:0] <= 0xAB`，其余位不变。
- 情况 2：`0x4` 的 `bit[3:2]=01` → `slv_reg1`；`WSTRB=1100` 写 byte2、byte3 → `slv_reg1[31:16] <= 0x1122`，低 16 位不变。

#### 4.4.5 小练习与答案

**练习 1**：若要把寄存器数量从 4 个扩到 8 个，需要改哪几个地方？

**参考答案**：至少改三处：① `OPT_MEM_ADDR_BITS` 从 1 改成 2（[L102](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L102)），使译码位变宽；② 增加 `slv_reg4~7` 的声明并扩展写 `case`（[L107-110](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L107-L110)、[L231-266](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L231-L266)）和读 `case`（[L372-378](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L372-L378)）；③ 顶层 `C_S_AXI_ADDR_WIDTH` 也要够大（8 个 32 位寄存器需要 5 位地址：\(8 \times 4 = 32\) 字节，\(\log_2 32 = 5\)）。实际工程中这种结构通常用参数化的 `generate` 重写，而非手抄 8 份。

**练习 2**：读事务没有 `WSTRB`，如果处理器只想要某个字节怎么办？

**参考答案**：AXI4-Lite 读总是**整字**返回（`RDATA` 是 32 位），没有字节级读使能。处理器读到整个 32 位后，自己在软件里用移位/掩码取出想要的字节。WSTRB 只用于写。

---

### 4.5 `Add user logic here`：把 AES 核心挂上去

#### 4.5.1 概念说明

回顾 u3-l1 的核心结论：**这个 IP 目前是个空壳**——封装层（`ip_repo/hdl`）和算法层（`hdl/src/aes_top.v`）是脱节的。体现在代码上，就是模板故意留出的两个空区域：

1. **子模块里的用户逻辑区**：[L400](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L400) 的 `// Add user logic here`。
2. **顶层包装里的用户逻辑区**：`AesCryptoCore_v1_0.v` 的 `// Add user logic here`。

「挂接 AES 核心」要做的事，本质上就是：**把 4 个寄存器（`slv_reg0~3`）接到 AES 核心的输入/输出上**，让处理器通过读写寄存器来「喂密钥、喂明文、启动、读密文」。例如一种自然的约定：

- `slv_reg0`：控制/状态（bit0 = start，启动一次加密）。
- `slv_reg1`：密钥低 32 位…（密钥 128 位需 4 个寄存器，这里只是示意）。
- `slv_reg2`：明文输入。
- `slv_reg3`：密文输出（只读）。

当然，128 位的 AES 实际需要更多寄存器或更宽接口，这里只是说明思路。

#### 4.5.2 核心流程

把用户逻辑接上的三步法：

```text
① 在 S00_AXI.v 的 "Users to add ports here" 处增加用户端口：
     output wire start, output wire [31:0] key_in, ... input wire [31:0] cipher_out ...
② 在 S00_AXI.v 的 "Add user logic here" 处，把 slv_reg* 连到这些端口：
     assign start = slv_reg0[0];
     assign key_in = slv_reg1;
     让 slv_reg3 作为结果回读：在 default/读路径里把 cipher_out 接到 reg_data_out
③ 在顶层 AesCryptoCore_v1_0.v 的 "Add user logic here" 处例化 aes_top：
     aes_core u_aes (.clk(...), .start(start), .key(key_in), ..., .cipher(cipher_out));
   并把 S00_AXI 实例的新端口 .start(start) 等连上
```

需要特别强调：**这一步目前并没有做**。仓库里的两个 `// Add user logic here` 区域都是空的，`S00_AXI` 的端口里也没有任何用户端口。所以现状是「寄存器能读写，但读写没有效果」。

#### 4.5.3 源码精读

子模块的用户逻辑空区：

[AesCryptoCore_v1_0_S00_AXI.v:400-402](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L400-L402) —— `// Add user logic here` 与 `// User logic ends` 之间是空的。同样，模块参数区 [L6-7](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L6-L7)、端口区 [L17-19](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L17-L19) 也都是「Users to add ... here」的空占位。

顶层包装的例化与用户逻辑空区：

[AesCryptoCore_v1_0.v:79-105](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0.v#L79-L105) —— 例化 `S00_AXI`，把外部 `s00_axi_*` 端口逐根连到子模块的 `S_AXI_*`。注意这里**只连了 AXI 信号**，没有任何用户信号——因为子模块还没声明用户端口。

[AesCryptoCore_v1_0.v:141-143](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0.v#L141-L143) —— 顶层也有一个空的 `// Add user logic here`，本应在这里例化 `aes_top`。

下面是一段**示例代码**（非项目原有，仅说明接法），展示「如果要把 start/key/cipher 接起来」大概长什么样：

```verilog
// ===== 示例代码：仅示意，仓库中并不存在 =====
// 在 S00_AXI.v 的 "Users to add ports here"：
output wire start,
output wire [31:0] plain_in,
input  wire [31:0] cipher_out,

// 在 S00_AXI.v 的 "Add user logic here"：
assign start     = slv_reg0[0];   // 写 slv_reg0 的 bit0 触发一次加密
assign plain_in  = slv_reg1;      // 明文来自 slv_reg1

// 让 slv_reg3 回读为密文（覆盖默认读选择）：
// （实际需修改 reg_data_out 的 case，或在读路径里把 cipher_out 接进去）

// 在 AesCryptoCore_v1_0.v 的 "Add user logic here"：
wire start_w; wire [31:0] plain_w, cipher_w;
aes_top u_aes (            // 假设的例化
    .clk(s00_axi_aclk),
    .start(start_w),
    .plain_in(plain_w),
    .cipher_out(cipher_w)
);
```

> 再次提醒：上面是**示例代码**，仓库里没有，仅供理解「接入点在哪、数据怎么流」。

#### 4.5.4 代码实践

**实践目标**：把本讲的「寄存器从机」与 u2 学过的「AES 数据通路」在脑中接起来，理解软硬件协同的接口设计。

**操作步骤**：

1. 打开 [AesCryptoCore_v1_0.v:79-143](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0.v#L79-L143)，确认两个用户逻辑区都是空的。
2. 回忆 u2-l1：`aes_top.v` 的输入是 128 位 `text_in`/`key`，输出 128 位密文，还有 `encrypt` 选择与轮计数控制。
3. 设计一份**寄存器映射草案**（写在纸上即可）：用 4 个 32 位寄存器够不够装下 128 位密钥 + 128 位明文？如果不够，你会怎么扩展（提示：增加寄存器数量，见 4.4 练习 1；或用更宽的接口）？
4. 思考「启动信号」如何产生：处理器写 `slv_reg0` 的某一 bit → `start` 脉冲 → AES 跑完 → 通过 u3-l3 要讲的中断通知处理器。

**需要观察的现象 / 预期结果**：

- 128 位密钥 + 128 位明文 = 256 位，至少需要 \(256/32 = 8\) 个数据寄存器，再加上控制和状态寄存器，**当前 4 个寄存器根本不够**。这是本 IP「空壳」之外的第二个现实问题：寄存器数量也不足以承载完整 AES 接口。
- 结论：要真正接上 AES，既要填 `Add user logic here`，也要扩寄存器数量。这正是为什么 u3-l1 说它「尚未接上真正的加密运算」。

#### 4.5.5 小练习与答案

**练习 1**：为什么用户逻辑要分「子模块里一段」和「顶层里一段」两个地方，而不是只在一处写？

**参考答案**：分工不同。子模块 `S00_AXI` 「最接近寄存器」，适合把 `slv_reg*` 翻译成用户信号（产生 `start`、给出 `key_in` 等），并把这些用户信号通过新增端口「引出」。顶层 `AesCryptoCore_v1_0` 则负责**例化真正的算法核心**（如 `aes_top`），并把子模块引出的用户信号接到算法核心上。这种分层让「总线接口」与「算法」解耦，更换算法时不必动 `S00_AXI` 的握手代码。

**练习 2**：处理器怎么知道「一次 AES 加密做完了」？

**参考答案**：两种办法。① **轮询**：处理器反复读某个状态寄存器（如 `slv_reg0` 的 done bit）直到为 1；② **中断**：让 AES 完成信号驱动 `S_AXI_INTR` 从机的中断源（u3-l3 会讲），产生 `irq` 通知处理器，处理器在中断服务程序里读结果。中断方式不占 CPU，是更专业的做法。

---

## 5. 综合实践

把本讲的知识串起来，完成下面这个**贯穿性任务**——这也是本讲的核心实践。

### 任务：手画写事务与读事务的完整时序波形图

**目标**：在 `AesCryptoCore_v1_0_S00_AXI.v` 中定位 `slv_reg0~3` 的读写逻辑，画出一次 32 位寄存器**写事务**（AW+W+B）和一次**读事务**（AR+R）的时序波形图。

**操作步骤**：

1. **定位代码**：
   - 写使能 [L217](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L217)、写寄存器 [L219-269](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L219-L269)、写回应 [L271-302](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L271-L302)。
   - 读使能 [L368](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L368)、读选择 [L369-379](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L369-L379)、读数据输出 [L381-398](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S00_AXI.v#L381-L398)。
2. **设定场景**：写 `0xDEADBEEF` 到地址 `0x4`（即 `slv_reg1`），然后读地址 `0x4`。
3. **在纸上（或绘图工具）**画出 `ACLK`、`ARESETN`、`AWVALID/AWADDR/AWREADY`、`WVALID/WDATA/WREADY`、`BVALID/BRESP/BREADY` 的逐拍波形，以及内部 `aw_en`、`slv_reg1` 的变化。
4. 再画读路径：`ARVALID/ARADDR/ARREADY`、`RVALID/RDATA/RREADY`。

### 参考波形（写事务，假设 BREADY 常高）

下表用 `T0..T4` 表示连续时钟周期，每列是「该周期内信号线的值」。设主设备从 T1 起置 `AWVALID=WVALID=1` 并保持到握手完成，`BREADY` 常高。

| 信号 | T0(空闲) | T1 | T2 | T3 | T4(空闲) |
|------|---------|----|----|----|---------|
| `ARESETN` | 1 | 1 | 1 | 1 | 1 |
| `AWVALID` | 0 | 1 | 1 | 0 | 0 |
| `AWADDR` | x | 0x4 | 0x4 | x | x |
| `AWREADY` | 0 | 0 | **1** | 0 | 0 |
| `WVALID` | 0 | 1 | 1 | 0 | 0 |
| `WDATA` | x | 0xDEADBEEF | 0xDEADBEEF | x | x |
| `WREADY` | 0 | 0 | **1** | 0 | 0 |
| `aw_en`(内部) | 1 | 1 | 0 | 0 | 1 |
| `slv_reg1`(内部) | 旧值 | 旧值 | 旧值 | **0xDEADBEEF** | 0xDEADBEEF |
| `BVALID` | 0 | 0 | 0 | **1** | 0 |
| `BRESP` | 0 | 0 | 0 | 00(OKAY) | 0 |
| `BREADY` | 1 | 1 | 1 | 1 | 1 |

**读图要点**：

- **T2** 是 AW/W 同时握手的那一拍（`AWREADY=WREADY=1` 且 `AWVALID=WVALID=1`）。`aw_en` 在 T2 落到 0（关门）。
- `slv_reg1` 在 **T3** 才更新为 `0xDEADBEEF`（写发生在 T2 的握手之后那一拍，因为 `slv_reg_wren` 在 T2 为 1，寄存器在 T2→T3 的上升沿更新）。
- `BVALID` 在 **T3** 拉高，T3 的 `BREADY=1` 握手后，T4 撤回；同时 `aw_en` 在 T4 重新开门为 1。
- 整笔写从 VALID 发起到 B 回应确认，约 **3 个周期**。

### 参考波形（读事务）

设主设备从 T1 起置 `ARVALID=1`、`ARADDR=0x4`，`RREADY` 常高。此时 `slv_reg1` 已是 `0xDEADBEEF`。

| 信号 | T0(空闲) | T1 | T2 | T3 | T4(空闲) |
|------|---------|----|----|----|---------|
| `ARVALID` | 0 | 1 | 1 | 0 | 0 |
| `ARADDR` | x | 0x4 | 0x4 | x | x |
| `ARREADY` | 0 | 0 | **1** | 0 | 0 |
| `RVALID` | 0 | 0 | 0 | **1** | 0 |
| `RDATA` | x | x | x | **0xDEADBEEF** | x |
| `RRESP` | 0 | 0 | 0 | 00(OKAY) | 0 |
| `RREADY` | 1 | 1 | 1 | 1 | 1 |

**读图要点**：

- **T2** 是 AR 握手拍（`ARREADY=1` 且 `ARVALID=1`），地址被锁存。
- `reg_data_out` 在 T2（组合）即等于 `slv_reg1`，但 `RDATA`/`RVALID` 在 **T3** 才出现（打了一拍寄存器）。
- 读延迟 = AR 握手后 **1 个周期** 数据可见。
- T3 的 `RREADY=1` 握手后，T4 撤回 `RVALID`。

> **关于验证**：以上波形是根据源码逻辑逐拍推演得到的，标注为「待本地验证」。若你有 Vivado / Icarus Verilog / Verilator，可写一个最小 testbench 复现这两段时序，对照波形确认每一拍的电平。

---

## 6. 本讲小结

- **AXI4-Lite = 五个通道**：AW（写地址）、W（写数据）、B（写回应）、AR（读地址）、R（读数据），每个通道都靠 **VALID/READY 握手**成交。
- **握手铁律**：只有 `VALID` 和 `READY` 同拍为 1 才算一次传输；发送方拉高 `VALID` 后在收到 `READY` 前不得撤回。
- **写路径的 `aw_en`** 是「单事务屏障」：收下一笔写就关门，直到 B 通道回应被确认才开门，保证「不允许未完成事务」。
- **地址译码**用 `axi_awaddr[3:2]`（即 `addr[ADDR_LSB+OPT_MEM_ADDR_BITS:ADDR_LSB]`）选 `slv_reg0~3`；`ADDR_LSB=2` 跳过字内字节偏移，`OPT_MEM_ADDR_BITS=1` 给出 4 个寄存器。
- **字节写使能 WSTRB** 控制一个 32 位字里哪些字节被写；读则整字返回。
- **`// Add user logic here`** 是把 AES 核心挂上 IP 的两个接入点（子模块 + 顶层），但目前都是空的——IP 仍是「能读写 4 个寄存器、但不做任何运算」的空壳。

---

## 7. 下一步学习建议

本讲把「寄存器从机」`S00_AXI` 讲透了，但顶层包装里还有**另一个**从机——中断从机 `S_AXI_INTR`。下一讲 **u3-l3「AXI 中断接口与寄存器映射」** 将讲解：

- 中断从机的寄存器组（使能 IPIER、挂起 ISR、确认 IAR、全局使能 GIE）。
- `irq` 输出如何由「挂起 ∧ 使能」逻辑产生。
- 中断敏感度（电平 / 边沿）参数的含义。
- 如何把 AES「运算完成」事件接到中断源，让处理器用中断而非轮询获知结果。

学完 u3-l3，你就能理解「处理器写寄存器启动 AES → AES 完成 → 中断通知处理器 → 处理器读密文」这条完整的软硬件协同链路。之后 **u3-l4** 会讲 Vivado 自动生成的 C 驱动（`AesCryptoCore.h/.c`、`selftest`），把硬件接口和软件调用彻底打通。建议阅读顺序：**u3-l3 → u3-l4 → u3-l5（仿真验证）**。
