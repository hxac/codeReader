# AXI 中断接口与寄存器映射

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 AXI 中断从机 `AesCryptoCore_v1_0_S_AXI_INTR` 的端口、关键参数与 5 个寄存器各自的作用。
- 看懂中断寄存器组的地址译码（`addr[4:2]`）与读写逻辑，分清 GIE / IER / ISR / IAR / IPR 五者关系。
- 理解中断「检测 → 状态 → 挂起 → irq 输出 → 确认清除」的完整数据流，并说出电平触发与边沿触发的区别。
- 画出从「用户逻辑拉高中断源」到「处理器读 ISR 并写 IAR 清除」的完整时序。
- 认清本 IP 当前的真实状态：中断源是模板自带的倒数计数器，并非 AES 运算完成事件——这是承接 u3-l1/u3-l2「IP 仍是空壳」结论的关键。

## 2. 前置知识

在进入正文前，先用三段话建立直觉。

**为什么需要中断？** 处理器（如 Zynq 的 ARM 核）启动一次 AES 加密后，硬件可能要几十上百个时钟周期才算完。如果处理器一直「轮询」状态寄存器（反复读、判断是否完成），就会把大量 CPU 时间浪费在「干等」上。中断的作用是：**硬件做完事后主动「拍一下」处理器**，处理器在等待期间可以去干别的，被「拍」到时再回来处理。这就把处理器从忙等中解放出来。

**AXI 中断从机是什么？** 它是 Vivado「Create and Package IP」向导在你勾选「Include interrupt」后自动生成的一段 RTL（即本讲的 `..._S_AXI_INTR.v`）。它对外提供两类接口：一面是标准的 AXI4-Lite 从机端口（处理器通过它读写中断寄存器），另一面是一个 `irq`（interrupt request）输出引脚（拉高表示「有中断请求」）。模块内部维护一组「使能 / 状态 / 挂起 / 确认」寄存器，把「用户逻辑产生的原始中断源」整理成规范的、可屏蔽的、可确认的 `irq` 信号。

**电平触发 vs 边沿触发。** 中断源 `intr` 是一根普通信号。我们关心的是「什么时候算一次中断」：
- 电平触发（level）：只要 `intr` 处于有效电平（如高电平），就视为有中断；适合「状态型」事件（比如「FIFO 非空」）。
- 边沿触发（edge）：只在 `intr` 发生跳变（如上升沿）的那一拍记一次；适合「脉冲型」事件（比如「这一拍刚完成一次运算」）。

> 阅读提醒：本模块的 AXI4-Lite 五通道（AW/W/B/AR/R）握手时序，与上一讲 u3-l2 精读的 `S00_AXI` 完全一致（同样用 `aw_en` 做「单事务屏障」、VALID/READY 同拍有效才算一笔传输）。本讲不再重复握手细节，**聚焦于中断寄存器与 irq 生成**。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`.../hdl/AesCryptoCore_v1_0_S_AXI_INTR.v`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v) | 本讲主角：AXI 中断从机。含 5 个中断寄存器、检测逻辑、irq 生成逻辑，以及一段「示例」中断源（倒数计数器）。 |
| [`.../hdl/AesCryptoCore_v1_0.v`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0.v) | IP 顶层包装：例化 `S00_AXI`（寄存器从机）与本讲的 `S_AXI_INTR`（中断从机），并把 `irq` 引到顶层端口。 |

（为简洁，后文引用这两个文件时省略前面的 `HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/` 路径前缀。）

## 4. 核心概念与源码讲解

### 4.1 模块 AesCryptoCore_v1_0_S_AXI_INTR：总览、参数与端口

#### 4.1.1 概念说明

这个模块是 Vivado 自动生成的「AXI4-Lite 中断控制器」模板。它的职责可以用一句话概括：

> 把若干根**原始中断源**信号 `intr`，整理成一根**规范的、可屏蔽的、可确认的** `irq` 输出，并让处理器能通过 AXI 寄存器查询/使能/清除每一个中断。

模板用**参数化**来适配不同场景：中断数量、每个中断的触发方式（电平/边沿、高/低有效）、以及 `irq` 输出本身的触发方式，都可在 IP 打包时配置。理解这些参数是读懂后续 `generate` 分支的前提。

#### 4.1.2 核心流程

模块顶层结构（自上而下）：

1. **AXI4-Lite 接口**：与 u3-l2 的 `S00_AXI` 同构，负责寄存器读写握手。
2. **寄存器组**（5 个）：GIE / IER / ISR / IAR / IPR，由 `generate` 循环按中断位数生成。
3. **中断源**：模板自带的「示例」倒数计数器，产生 `intr`（**注意：当前并非 AES**）。
4. **检测逻辑**：按 `C_INTR_SENSITIVITY` / `C_INTR_ACTIVE_STATE` 把 `intr` 转成 `det_intr`。
5. **irq 生成**：综合「挂起 + 全局使能」，按 `C_IRQ_SENSITIVITY` 产出 `s_irq` → `irq`。

数据流可概括为：

\[ \text{intr} \xrightarrow{\text{检测}} \text{det\_intr} \xrightarrow{\text{锁存}} \text{ISR} \xrightarrow[\text{IER}]{\text{AND}} \text{IPR} \xrightarrow{\text{OR}} \text{intr\_all} \xrightarrow[\text{GIE}]{\text{AND}} \text{irq} \]

#### 4.1.3 源码精读

**参数声明** —— 中断行为全靠这几个参数控制：[AesCryptoCore_v1_0_S_AXI_INTR.v:11-24](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L11-L24)

```verilog
parameter integer C_NUM_OF_INTR       = 1,           // 中断数量
parameter  C_INTR_SENSITIVITY  = 32'hFFFFFFFF,        // 每位: 0=边沿, 1=电平
parameter  C_INTR_ACTIVE_STATE = 32'hFFFFFFFF,        // 子类型(高/低有效 或 升/降沿)
parameter integer C_IRQ_SENSITIVITY   = 1,            // irq: 0=边沿, 1=电平
parameter integer C_IRQ_ACTIVE_STATE  = 1             // irq 子类型
```

读法要点：
- `C_NUM_OF_INTR = 1`：本 IP 只有 **1 个中断**。所以后面所有寄存器都是 1 位宽（`reg [0:0]`）。
- `C_INTR_SENSITIVITY` / `C_INTR_ACTIVE_STATE` 是 **32 位**，每一位对应一个中断的设置；默认全 `1`，所以中断 0 是「电平 + 高有效」。
- `C_IRQ_SENSITIVITY = 1`、`C_IRQ_ACTIVE_STATE = 1`：`irq` 输出本身是「电平 + 高有效」。

> ⚠️ 模板注释里把 LEVEL 的两种子类型都写成了 `LEVEL_LOW`（[第 19、23 行](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L19-L23)），这是 Vivado 模板原文的笔误（应为 `LEVEL_LOW` / `LEVEL_HIGH`）。**以代码逻辑为准**，不要被注释带偏。

**关键端口** —— 除了 AXI4-Lite 五通道，模块多了一个 `irq` 输出：[AesCryptoCore_v1_0_S_AXI_INTR.v:92-93](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L92-L93)

```verilog
// interrupt out port
output wire  irq
```

**顶层如何例化它** —— 顶层把中断相关参数透传，并把 `irq` 接到顶层输出引脚：[AesCryptoCore_v1_0.v:108-139](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0.v#L108-L139)（关键尾部）：

```verilog
.C_NUM_OF_INTR(C_NUM_OF_INTR),
...
.irq(irq)
```

而顶层自身的 `irq` 输出端口声明在 [AesCryptoCore_v1_0.v:77](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0.v#L77)。注意顶层的 `// Add user logic here`（[第 141 行](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0.v#L141)）和中断模块内的 `// Add user logic here`（[第 745 行](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L745)）目前都是空的——这正是「IP 还是空壳」的体现（详见 4.3）。

#### 4.1.4 代码实践

**实践目标**：建立对本模块「参数即配置」的直觉。

**操作步骤**：
1. 打开 [AesCryptoCore_v1_0_S_AXI_INTR.v:11-24](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L11-L24)。
2. 假设要把中断 0 改成「上升沿触发」，回答：`C_INTR_SENSITIVITY` 的 bit0 应改成几？`C_INTR_ACTIVE_STATE` 的 bit0 应改成几？（提示：边沿=0，上升沿=1）
3. 再假设要支持 3 个中断，回答：`C_NUM_OF_INTR` 应改成几？此时 `reg_intr_en` 的位宽会变成多少？

**需要观察的现象 / 预期结果**：你会体会到——**不用改 RTL 逻辑，只改参数**就能切换中断数量与触发方式，这正是模板用 `generate` 的意义。预期答案：边沿触发→`C_INTR_SENSITIVITY` bit0=0、`C_INTR_ACTIVE_STATE` bit0=1；3 个中断→`C_NUM_OF_INTR=3`，`reg_intr_en` 变为 `reg [2:0]`。待本地验证：可在 Vivado 中重新打包 IP 并观察生成报告。

#### 4.1.5 小练习与答案

**练习 1**：`C_NUM_OF_INTR` 最大能设到几？（提示看 `C_INTR_SENSITIVITY` 的位宽。）

> **答案**：32。因为 `C_INTR_SENSITIVITY` / `C_INTR_ACTIVE_STATE` 都是 32 位，每位对应一个中断的设置；超过 32 需要扩展这些参数位宽。

**练习 2**：本模块的 AXI 数据位宽和地址位宽默认是多少？地址位宽为什么是 5？

> **答案**：数据 32 位、地址 5 位（见 [第 12、14 行](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L12-L14)）。地址 5 位（0–31）足以覆盖 5 个寄存器（每个 4 字节，共 20 字节，落在 0x00–0x1F 内）；译码只用 `addr[4:2]`（见 4.2）。

---

### 4.2 中断寄存器组：GIE / IER / ISR / IAR / IPR 与地址映射

#### 4.2.1 概念说明

处理器要能「查询、使能、清除」中断，就必须有一组**可寻址的寄存器**。本模块把中断寄存器空间设计成 5 个 32 位寄存器，每个寄存器的每一位对应一个中断（本 IP 只有 1 个中断，故只有 bit0 有意义）。五个寄存器各司其职：

| 缩写 | 全称（含义） | 变量名 | 处理器视角 | 作用 |
|------|------|------|------|------|
| **GIE** | Global Interrupt Enable（全局中断使能） | `reg_global_intr_en` | 读写 bit0 | 总开关。为 0 时整个 IP 不产生 `irq`。 |
| **IER** | Interrupt Enable Register（中断使能） | `reg_intr_en` | 读写 | 每个中断的单独使能（屏蔽）。 |
| **ISR** | Interrupt Status Register（中断状态） | `reg_intr_sts` | 只读 | 记录「中断源是否被检测到」。 |
| **IAR** | Interrupt Acknowledge Register（中断确认） | `reg_intr_ack` | 写 1 清除 | 处理器写 1 到某位，表示「我处理完了，请清掉它」。 |
| **IPR** | Interrupt Pending Register（中断挂起） | `reg_intr_pending` | 只读 | `ISR & IER`：被使能且确实发生、等待处理的中断。 |

一句话记忆：**ISR 记「发生了」，IER 记「要不要理」，IPR = 两者相与（真正会触发 irq 的），IAR 是「处理完的回执」，GIE 是总闸。**

#### 4.2.2 核心流程

寄存器用 `addr[4:2]`（即地址除以 4）来选择，所以 5 个寄存器落在字对齐的偏移上：

| `addr[4:2]` | 偏移地址 | 命中寄存器 |
|:-:|:-:|:-:|
| `3'h0` | 0x00 | GIE |
| `3'h1` | 0x04 | IER |
| `3'h2` | 0x08 | ISR |
| `3'h3` | 0x0C | IAR |
| `3'h4` | 0x10 | IPR |

读流程：处理器发起 AR+R 事务 → 用 `axi_araddr[4:2]` 在 `case` 里选出对应寄存器 → 寄存一拍后送上 `RDATA`。
写流程：处理器发起 AW+W+B 事务 → 写使能 `intr_reg_wren` 拉高 → 用 `axi_awaddr[4:2]` 决定写入哪个寄存器；其中**只有 GIE、IER、IAR 可写**，ISR/IPR 是硬件内部维护、只读。

**关键清除关系**：写 IAR（确认）会引发连锁清除——`reg_intr_ack[i]` 一旦为 1，会同时把 `det_intr[i]`、`reg_intr_sts[i]`、`reg_intr_pending[i]` 清零（详见 4.3）。这就是「确认即清除」的机制。

#### 4.2.3 源码精读

**写使能信号** —— 仅在「AW 与 W 同时有效且本模块 ready」时拉高一拍：[AesCryptoCore_v1_0_S_AXI_INTR.v:230](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L230)

```verilog
assign intr_reg_wren = axi_wready && S_AXI_WVALID && axi_awready && S_AXI_AWVALID;
```

**`generate` 循环生成每个中断的寄存器**：[AesCryptoCore_v1_0_S_AXI_INTR.v:232-302](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L232-L302)。每个寄存器都按 `axi_awaddr[4:2]` 选择写入，要点摘录：

```verilog
// GIE: 写 addr[4:2]==0 时，WDATA[0] → reg_global_intr_en[0]   (L243-245)
// IER: 写 addr[4:2]==1 时，WDATA[i] → reg_intr_en[i]          (L256-258)
// ISR: 复位或 ack 时清 0，否则跟随 det_intr[i]                (L265-271)
// IAR: 复位或自身为1时清0，写 addr[4:2]==3 时载入 WDATA[i]    (L278-284)
// IPR: 复位或 ack 时清 0，否则 = reg_intr_sts[i] & reg_intr_en[i]  (L291-297)
```

注意三处设计要点：
- **ISR 是「锁存」而非「直通」**：`reg_intr_sts[i] <= det_intr[i]`（[第 271 行](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L271)），一旦置位就保持，直到被 ack 清除——所以处理器「晚点」来读也不会漏。
- **IAR 是「自清零脉冲」**：`if (... || reg_intr_ack[i]==1) reg_intr_ack[i] <= 0`（[第 278-280 行](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L278-L280)）。你写 1 进去，它只高一拍就自动回 0，但这一拍足以触发连锁清除。
- **IPR 由硬件计算**：`reg_intr_pending[i] <= reg_intr_sts[i] & reg_intr_en[i]`（[第 297 行](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L297)），处理器无法直接写它。

**读地址译码** —— 组合逻辑 `case`，按 `axi_araddr[4:2]` 选出要读的寄存器：[AesCryptoCore_v1_0_S_AXI_INTR.v:410-417](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L410-L417)

```verilog
case ( axi_araddr[4:2] )
  3'h0   : reg_data_out <= reg_global_intr_en;   // GIE  @ 0x00
  3'h1   : reg_data_out <= reg_intr_en;          // IER  @ 0x04
  3'h2   : reg_data_out <= reg_intr_sts;         // ISR  @ 0x08
  3'h3   : reg_data_out <= reg_intr_ack;         // IAR  @ 0x0C
  3'h4   : reg_data_out <= reg_intr_pending;     // IPR  @ 0x10
  default : reg_data_out <= 0;
endcase
```

读出的值再寄存一拍送 `RDATA`（[第 422-438 行](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L422-L438)），所以读延迟约一拍，与 u3-l2 的 `S00_AXI` 一致。

> 细节：中断寄存器的写直接用 `WDATA[i]` / `WDATA[0]`（每个中断占一位），**并不使用字节写使能 `WSTRB`**，这与 `S00_AXI` 的逐字节写入不同。

#### 4.2.4 代码实践

**实践目标**：用地址映射表，手算处理器要发起哪些 AXI 事务来「打开中断并查询状态」。

**操作步骤**（假设处理器要把中断 0 完整使能）：
1. **写 GIE**：向偏移 `0x00` 写 `0x1`（开总闸）。
2. **写 IER**：向偏移 `0x04` 写 `0x1`（使能中断 0）。
3. **读 ISR**：从偏移 `0x08` 读，若 bit0=1 说明中断已发生。
4. **写 IAR**：向偏移 `0x0C` 写 `0x1`（确认并清除中断 0）。

**需要观察的现象 / 预期结果**：对照上面的 `case` 表，验证每一步的 `addr[4:2]` 分别是 `0/1/2/3`，与表中偏移一一对应。再问自己：第 4 步写完 IAR 后，如果立刻读 IPR（`0x10`），bit0 应该是几？预期为 0（因为 IAR 的连锁清除把 IPR 清掉了）。待本地验证：可用仿真在写 IAR 后下一拍读 IPR 确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 ISR 和 IPR 是「只读」的？处理器如果想「人为制造」一个中断状态，能做到吗？

> **答案**：ISR 由硬件锁存 `det_intr`、IPR 由硬件算 `ISR & IER`，代码里没有把它们作为写目标的分支，故只读。处理器无法直接写它们；要清除只能通过写 IAR 间接清掉。

**练习 2**：地址 `0x14`（`addr[4:2] == 5`）读回来会是什么？

> **答案**：`case` 的 `default` 分支返回 0（[第 416 行](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L416)），即未定义地址读出 0。

---

### 4.3 irq 生成与完整中断时序：检测、挂起、使能与确认

#### 4.3.1 概念说明

寄存器组是「存储」，而 `irq` 是「输出」。本模块把中断源 `intr` 加工成 `irq` 的过程，经过一条清晰的流水线：

1. **检测（det_intr）**：根据触发方式（电平/边沿、高/低有效）把 `intr` 转成「检测到中断」标志，并锁存直到确认。
2. **状态（ISR）**：锁存 `det_intr`。
3. **挂起（IPR）**：`ISR & IER`，只有「被使能」的才计入。
4. **聚合（intr_all）**：`|IPR`，任意一个挂起即为 1。
5. **irq**：`intr_all & GIE`，再受总闸控制，按 `irq` 自身的触发方式输出。

数学上，最终的电平型 irq 可写成：

\[ \text{irq} \;=\; \underbrace{\left(\bigvee_{i} \text{IPR}_i\right)}_{\text{intr\_all}} \;\wedge\; \underbrace{\text{GIE}}_{\text{reg\_global\_intr\_en[0]}} \;=\; \left(\bigvee_{i} (\text{ISR}_i \wedge \text{IER}_i)\right) \wedge \text{GIE} \]

**当前的真实中断源是谁？** 模板在文件末尾附带了一段「示例用户逻辑」——一个倒数计数器：复位后从 `0xF` 往下数，数到 `10` 时把 `intr` 拉高一拍。**这就是当前 IP 唯一的中断源，并不是 AES 运算完成**。真正的 AES 完成信号需要你自己接进 `// Add user logic here`（[第 745 行](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L745)），目前那里是空的——这再次印证 u3-l1/u3-l2 的结论：**IP 仍是一个能产生中断、但中断源是占位示例的空壳骨架**。

#### 4.3.2 核心流程

一次完整的中断生命周期（默认参数：电平、高有效）：

```
[用户逻辑] intr 拉高(1拍)
     │  (检测: 电平高有效)
     ▼
det_intr 锁存为 1（保持到 ack）
     │  (ISR 锁存)
     ▼
reg_intr_sts = 1
     │  (IPR = ISR & IER，假设 IER=1)
     ▼
reg_intr_pending = 1
     │  (intr_all = |IPR)
     ▼
intr_all = 1
     │  (s_irq = intr_all & GIE，假设 GIE=1)
     ▼
irq 拉高  ─────────────►  [处理器] 进入中断
                                   │ 读 ISR @0x08 → 定位中断源
                                   │ 处理...
                                   │ 写 IAR @0x0C = 1（确认）
                                   ▼
                        reg_intr_ack 脉冲(1拍) ──► 连锁清除 det_intr/ISR/IPR
                                   │ (intr_ack_all 置位)
                                   ▼
                                 irq 拉低
```

> ⚠️ **一处注释与代码不符**（批判阅读）：[第 444 行注释](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L444)说「counts down to zero」时产生中断，但代码（[第 468 行](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L468)）判的是 `intr_counter == 10`，不是 0。以代码为准。

#### 4.3.3 源码精读

**示例中断源：倒数计数器** —— 复位从 `0xF` 递减，到 `10` 时 `intr` 置 1：[AesCryptoCore_v1_0_S_AXI_INTR.v:447-477](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L447-L477)

```verilog
if (intr_counter != 4'h0)
    intr_counter <= intr_counter - 1;          // F,E,D,...,1,0
...
if (intr_counter == 10)
    intr <= {C_NUM_OF_INTR{1'b1}};             // 命中 0xA 那一拍拉高 intr
```

**电平检测（默认分支）**：`C_INTR_SENSITIVITY[0]==1` 且 `C_INTR_ACTIVE_STATE[0]==1`，故走「电平 + 高有效」分支——`intr==1` 即置 `det_intr`，且只在 ack 时清零（无 else 自动回 0，故为锁存）：[AesCryptoCore_v1_0_S_AXI_INTR.v:531-552](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L531-L552)

```verilog
if (C_INTR_SENSITIVITY[i] == 1'b1)  begin: gen_intr_level_detect
    if (C_INTR_ACTIVE_STATE[i] == 1'b1)  begin: gen_intr_active_high_detect
        always @(posedge S_AXI_ACLK) begin
            if (!S_AXI_ARESETN | reg_intr_ack[i]) det_intr[i] <= 1'b0;
            else if (intr[i] == 1'b1)            det_intr[i] <= 1'b1;
        end
    end
end
```

> 对比：边沿检测（[第 576-649 行](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L576-L649)）用两级寄存 `intr_ff`/`intr_ff2` 求出 `intr_edge = intr_ff && !intr_ff2`（上升沿）再锁存到 `det_intr`。默认参数不会走这条分支，但结构对称、值得一读。

**聚合 intr_all** —— 任意一个挂起位为 1 即置位：[AesCryptoCore_v1_0_S_AXI_INTR.v:480-490](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L480-L490)

```verilog
intr_all <= |reg_intr_pending;   // 约简或
```

**irq 生成（默认：电平高有效）** —— 受 `GIE` 控制并在 `intr_ack_all` 时清零：[AesCryptoCore_v1_0_S_AXI_INTR.v:651-672](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L651-L672)

```verilog
if (C_IRQ_SENSITIVITY == 1)  begin: gen_irq_level
    if (C_IRQ_ACTIVE_STATE == 1)  begin: irq_level_high
        always @(posedge S_AXI_ACLK) begin
            if (!S_AXI_ARESETN || intr_ack_all)            s_irq_lvl <= 1'b0;
            else if (intr_all && reg_global_intr_en[0])    s_irq_lvl <= 1'b1;
        end
        assign s_irq = s_irq_lvl;
    end
end
```

最后把内部 `s_irq` 接到输出：[AesCryptoCore_v1_0_S_AXI_INTR.v:741](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L741)

```verilog
assign irq = s_irq;
```

**确认（ack）的连锁清除**：写 IAR 使 `reg_intr_ack[i]=1`（持续一拍），这一拍同时被 `det_intr`、`reg_intr_sts`、`reg_intr_pending` 的清零条件引用（`... || reg_intr_ack[i]==1`），于是整条流水线被一并复位；同时 `intr_ack_all`（[第 493-503 行](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L493-L503)）置位，把 `s_irq_lvl` 拉低，`irq` 随之撤销。

#### 4.3.4 代码实践（本讲主线任务）

**实践目标**：画出从「中断源拉高」到「处理器写 IAR 清除」的完整时序波形。

**操作步骤**：
1. 设定初值：默认参数（电平/高有效、1 个中断）、`GIE=1`、`IER=1`。
2. 标注信号：`intr`、`det_intr[0]`、`reg_intr_sts[0]`、`reg_intr_pending[0]`、`intr_all`、`s_irq_lvl`（=`irq`）、`reg_intr_ack[0]`、`intr_ack_all`。
3. 逐拍推导（CLK 0,1,2,…），假设示例计数器在 **T 拍**让 `intr=1`（仅一拍）。
4. 在 **A 拍**处理器写 IAR=1（确认）。

**需要观察的现象 / 预期结果**（核心时序骨架）：

```
CLK      :  ...  T   T+1 T+2 T+3 T+4 T+5  ...  A   A+1 A+2 ...
intr     :  ...  1    0    0    0    0    0   ...  -    -    -   (T 拍单脉冲)
det_intr :  ...  0    1    1    1    1    1   ...  1    0    0   (T+1 起, A+1 被 ack 清)
ISR(sts) :  ...  0    0    1    1    1    1   ...  1    0    0
IPR(pend):  ...  0    0    0    1    1    1   ...  1    0    0
intr_all :  ...  0    0    0    0    1    1   ...  1    0/1  0
irq      :  ...  0    0    0    0    0    1   ...  1    1    0   (T+5 拉高, A+2 拉低)
ack(IAR) :  ...  0    0    0    0    0    0   ...  1    0    0   (A 拍单脉冲, 自清零)
```

要点：
- `intr` 只在 T 拍为 1，但经检测后 `det_intr`/ISR/IPR **锁存保持**，所以处理器稍后读 ISR 仍能看到。
- 从 `intr` 到 `irq` 有约 5 拍流水线延迟（`det_intr→ISR→IPR→intr_all→s_irq` 各一拍）；**精确延迟待本地仿真确认**。
- 写 IAR（A 拍）→ `reg_intr_ack` 单拍脉冲 → 下一拍（A+1）连锁清除 `det_intr`/ISR/IPR → `irq` 在 A+2 拉低。

**预期结果**：得到一张能解释「中断锁存、延迟触发、确认即清除」三件事的波形图。若用 Vivado/Icarus 仿真，可在 `irq` 拉高后人为驱动一次 AXI 写到 `0x0C`，观察 `irq` 是否在数拍后回落。若无法运行仿真，至少完成上面的手算时序表（本任务为「源码阅读 + 手算」型实践，不要求实际上板）。

#### 4.3.5 小练习与答案

**练习 1**：如果 `GIE=0` 但 `IER=1` 且中断发生，`irq` 会怎样？IPR 又会怎样？

> **答案**：IPR 仍会置 1（因为 `IPR = ISR & IER`，与 GIE 无关）；但 `irq` 保持 0（因为 `s_irq_lvl` 的置位条件要求 `reg_global_intr_en[0]==1`）。即 GIE 是 `irq` 的总闸，不影响挂起状态的记录。

**练习 2**：默认参数下 `intr` 只高了一拍，为什么处理器「晚 10 拍」去读 ISR 仍能看到中断？

> **答案**：因为检测分支对 `det_intr` 采用「锁存」语义——`intr==1` 时置 1，且**没有 else 在 intr 回低时清零**，只有 ack 才清。ISR 又锁存 `det_intr`。所以中断被「记住」了，直到处理器主动确认。

**练习 3**：要把这个示例中断源换成「AES 运算完成」，应该在代码哪里动手？

> **答案**：在 [第 745 行 `// Add user logic here`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L745) 处删除示例计数器逻辑，改为把 AES 核心的 done 信号接到 `intr[0]`（并在顶层 [第 141 行](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0.v#L141)把 AES 核心例化进来）。当前两处钩子都为空，故需自行接入。

## 5. 综合实践

**任务：写一个处理器侧的中断处理伪代码，并标注每一步访问的寄存器偏移。**

背景：假设你已按 4.3 把 AES 的 done 信号接到了 `intr[0]`，现在要写处理器的中断服务程序（ISR）。请结合本讲的寄存器映射与 4.3.4 的时序，完成下面伪代码并回答问题：

```c
// 初始化阶段（开中断）
void aes_intr_init(uint32_t base) {
    // 1. 开 GIE  ：向 base + ?  写 0x1
    // 2. 开 IER  ：向 base + ?  写 0x1
}

// 中断服务程序（irq 触发后由 CPU 调用）
void aes_isr(uint32_t base) {
    // 3. 读 ?    ：判断是不是中断 0
    // 4. 读密文  ：（这一步属于 S00_AXI 寄存器，见 u3-l2/u3-l4）
    // 5. 写 IAR  ：向 base + ?  写 0x1，清除中断
}
```

要求：
1. 填出 `?` 处的偏移地址（参考 4.2.2 的表）。
2. 回答：如果把第 5 步（写 IAR）漏掉，下一次 AES 完成时 `irq` 还会再次拉高吗？为什么？
3. 进阶：如果 `C_INTR_SENSITIVITY` 改成边沿触发（bit0=0），上述伪代码还需要改吗？时序行为有何不同？

> 参考答案要点：① GIE=0x00、IER=0x04、ISR=0x08、IAR=0x0C。② 漏写 IAR 时，`det_intr`/ISR/IPR 不会被清除，`irq` 会一直保持高电平（电平模式下）——后续即使 AES 再次完成也「看不出新的中断」，因为状态一直挂着；所以**确认清除是必需的**。③ 伪代码不用改（寄存器映射不变）；但边沿模式下，每次 AES done 的上升沿会被「记一次」，即便前一次没 ack 也能记录新的边沿事件——时序上更接近「事件计数」，而电平模式更像「状态保持」。

## 6. 本讲小结

- AXI 中断从机 `AesCryptoCore_v1_0_S_AXI_INTR` 把原始中断源 `intr` 整理成规范的 `irq`，并提供 5 个可寻址寄存器：GIE（总闸）、IER（单中断使能）、ISR（状态）、IAR（确认）、IPR（挂起）。
- 寄存器用 `addr[4:2]` 译码，落在 0x00/0x04/0x08/0x0C/0x10；其中 GIE/IER/IAR 可写，ISR/IPR 由硬件维护、只读。
- `irq` 的生成是一条流水线：`intr → det_intr → ISR →(IER)→ IPR →(OR)→ intr_all →(GIE)→ irq`；默认参数为「电平 + 高有效」，由 `C_INTR_SENSITIVITY` 等参数决定。
- 中断被「锁存」直到处理器写 IAR 确认；IAR 是单拍自清零脉冲，触发对 `det_intr`/ISR/IPR 的连锁清除，并最终让 `irq` 拉低。
- **当前 IP 的中断源是模板自带的倒数计数器示例（命中 0xA 时拉高 intr），并非 AES**；真正接入 AES done 需要填充两处空的 `// Add user logic here`（[S_AXI_INTR 第 745 行](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0_S_AXI_INTR.v#L745) 与[顶层第 141 行](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0.v#L141)）。
- 本模块与 u3-l2 的 `S00_AXI` 共享同一套 AXI4-Lite 握手时序（`aw_en` 单事务屏障），中断逻辑是它新增的部分。

## 7. 下一步学习建议

- **下一讲 u3-l4（软件驱动与自检程序）**：本讲的寄存器偏移（GIE/IER/ISR/IAR）会在 Vivado 自动生成的 C 驱动里变成具体的读写宏与 API。学完 u3-l4，你就能把本讲的「写 GIE/IER、读 ISR、写 IAR」翻译成真实的 C 代码。
- **回顾 u3-l2**：如果你对 `intr_reg_wren`、`aw_en`、AW/W/B 握手仍不熟，建议重读 u3-l2 的 `S00_AXI`——中断从机的 AXI 部分与它完全同构。
- **延伸阅读**：Xilinx 的 AXI 中断控制器（AXI INTC, PG099）文档，对照理解 GIE/IER/ISR/IPR/IAR 在工业 IP 中的标准用法；本模块即其简化、自动生成版本。
- **动手验证**：若有 Vivado，可把本 IP 的 `S_AXI_INTR` 单独建一个最小工程，用 AXI VIP（或手写 AXI 主机 BFM）驱动一次「写 GIE→写 IER→等待示例计数器触发→读 ISR→写 IAR」，在波形上验证 4.3.4 的时序。
