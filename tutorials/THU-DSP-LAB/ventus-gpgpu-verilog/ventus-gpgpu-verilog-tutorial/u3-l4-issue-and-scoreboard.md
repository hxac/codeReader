# 发射 issue 与记分板 scoreboard

## 1. 本讲目标

本讲承接 u3-l3（译码与 ibuffer），回答 SM 流水线中段最关键的两个问题：

1. 一条指令从 ibuffer「流」出来之后，**怎么被送到正确的执行单元**？这是 `issue` 模块的工作。
2. 当后一条指令要用到前一条指令**还没算完**的结果时，**谁来阻止它过早执行**？这是 `scoreboard`（记分板）的工作。

学完本讲，你应当能够：

- 说清 `issue.v` 在流水线中的位置——它是「单输入、按指令类型做 one-hot 路由」的组合分发器，并掌握它的优先级判定顺序。
- 说清 `scoreboard.v` 如何用「每寄存器一个忙位（busy bit）」的位图，在指令进入操作数采集器时置忙、在写回时清忙，从而检测并阻止 RAW/WAW 等数据冒险。
- 理解 `NUM_ISSUE`（=1）与 `NUM_COLLECTORUNIT`（=`NUM_WARP`）这两个规模参数如何分别决定「每拍路由几条指令」和「同时可采集操作数的 warp 数」，进而理解 SM 的并发发射能力。

## 2. 前置知识

- **数据冒险（hazard）**：流水线里，下一条指令需要上一条指令的结果，但上一条还没写回寄存器堆。最常见的是 **RAW**（Read After Write，先写后读）——后指令读、前指令写。如果不处理，后指令会读到旧值。
- **记分板（scoreboard）**：一种最朴素的冒险检测机制。给每个可能「正在被写、但还没写回」的寄存器立一个「忙」标志；新指令发射前先查标志，命中则暂停。
- **one-hot / 优先级路由**：`issue` 收到一条指令后，根据指令的控制信号（是不是浮点、是不是访存……），**只把握手**（valid/ready）接通到唯一一个执行单元。这本质上是一个带优先级的「多路选择开关」。
- **握手 fire**：在 valid/ready 接口中，当 `valid & ready` 同时为 1，称这一拍发生一次 fire，数据真正被对方取走。
- 本讲默认你已读过 u3-l1（pipe.v 总览）、u3-l3（decodeUnit / ibuffer / ibuffer2issue）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/gpgpu_top/sm/pipeline/issue.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v) | **发射/路由单元**：把单条带操作数的指令，按类型路由到 10 个执行单元之一，纯组合逻辑。 |
| [src/gpgpu_top/sm/pipeline/scoreboard.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/scoreboard.v) | **记分板**：用位图记录每个寄存器的「忙」状态，检测头指令的冒险，输出 `delay_o`。 |
| [src/gpgpu_top/sm/pipeline/pipe.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v) | SM 流水线顶层，**每个 warp 例化一个 scoreboard**，并例化唯一的 `issue`、`operand_collector`，把它们连起来。 |
| [src/define/define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v) | 定义 `NUM_ISSUE`、`NUM_COLLECTORUNIT`、寄存器号位宽、`A1/A2/A3` 操作数选择码、分支类型 `B_*` 等宏。 |
| [src/gpgpu_top/sm/pipeline/ibuffer2issue.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/ibuffer2issue.v) | 上游：用轮询仲裁在 `NUM_WARP` 个 warp 间选出一条指令，经操作数采集后送入 `issue`。 |

---

## 4. 核心概念与源码讲解

### 4.1 issue：把「带好操作数的指令」路由到执行单元

#### 4.1.1 概念说明

先看 `issue` 在整条流水线里的位置。从 u3-l1 我们知道主干是：

```
取指 icache → 译码 decodeUnit → ibuffer → ibuffer2issue(选 warp) → operand_collector(取操作数) → issue → 执行单元 → 写回 writeback
```

`issue` 的**输入**来自 `operand_collector`（操作数采集器）的输出，**输出**接到 10 个执行单元：

```
                       ┌─→ vALU  (向量整数 ALU)
                       ├─→ vFPU  (向量浮点)
                       ├─→ vMUL  (向量乘法)
operand_collector ──→ issue ─┼─→ LSU   (访存)
                       ├─→ SFU   (除法/开方等慢速运算)
                       ├─→ TC    (张量核)
                       ├─→ sALU  (标量 ALU)
                       ├─→ CSR   (CSR 读写)
                       ├─→ SIMT  (SIMT 栈控制)
                       └─→ warp_sche (barrier 栅栏处理)
```

注意一个容易混淆的点：本项目的 `issue` **不是**「从一堆就绪指令里挑一条」的选择器。真正做「选择」的是上游的 `ibuffer2issue`（轮询选 warp）和 `operand_collector`（仲裁各 collector unit）。到了 `issue`，**只剩一条**操作数已就绪的指令；`issue` 要做的只是——**判断这条指令属于哪一类，把它的 valid/ready 接通到对应执行单元**。所以它更像一个**按类型的解复用器（demux）**。

#### 4.1.2 核心流程

`issue` 的行为可以概括为三步（全部组合逻辑，当拍完成）：

1. **接收**：`issue_in_valid_i` 来自 `operand_collector_out_valid`；当 `issue_in_ready_o & issue_in_valid_i` 同时为 1，一条指令被取走（fire）。见 pipe.v 的连线 [pipe.v:1465-1466](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1465-L1466)。
2. **判定类型**：读指令的控制信号位（`tc / sfu / fp / csr / mul / mem / isvec / simt_stack / barrier`），用 `if-else if` 链按**优先级**判断它该去哪个执行单元。
3. **接通握手**：把**唯一**一个目标执行单元的 `valid` 拉成 `inputBuf_valid`，其余 9 个清 0；同时 `inputBuf_ready`（即对上游的反压）直接接到该目标执行单元的 `ready`。这样上游 ↔ issue ↔ 目标执行单元三者串成一条无缓冲的握手通道。

#### 4.1.3 源码精读

**(1) 输入输出与命名约定**。模块端口很长，但模式一致：`issue_out_<单元>_valid_o` / `issue_out_<单元>_ready_i` 是 10 个执行单元的握手对，`issue_out_<单元>_*` 是发往该单元的数据与控制信号。模块内部把这些都重命名为 `inputBuf_*` 前缀（注释说明这是从 Chisel 版本翻译来的命名）：

[issue.v:283-286](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L283-L286) —— `inputBuf_valid` 直接等于输入 valid，`issue_in_ready_o` 直接等于 `inputBuf_ready`（`inputBuf_ready` 是个 `reg`，在下面的 always 块里按目标单元赋值）。

后续 [issue.v:300-337](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L300-L337) 把输入端口原样赋给 `inputBuf_*` 内部线网；[issue.v:340-450](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L340-L450) 把 `inputBuf_*` 的数据/控制信号原样分发到 10 个执行单元的输出端口。这些赋值是无条件的「穿线」，**真正的路由决策**只体现在对 valid/ready 的控制上。

**(2) 优先级路由主体**。核心是 [issue.v:452-643](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L452-L643) 的 `always @(*)` 块。文件末尾 [issue.v:644](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L644) 专门留了一句注释 `attention :区分优先级顺序`（注意：要区分优先级顺序），提醒读者 if-else 的先后就是优先级。

下面这张表把判定顺序、判定信号、路由目标、对应行号整理出来：

| 优先级 | 判定信号 | 路由目标 | 行号 |
|---|---|---|---|
| 1 | `tc` | 张量核 TC | [454-467](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L454-L467) |
| 2 | `sfu` | 特殊功能单元 SFU | [468-481](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L468-L481) |
| 3 | `fp` | 向量浮点 vFPU | [482-495](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L482-L495) |
| 4 | `\|csr`（任一位为 1） | CSR 单元 | [496-509](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L496-L509) |
| 5 | `mul` | 向量乘法 vMUL | [510-523](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L510-L523) |
| 6 | `mem` | 访存 LSU | [524-537](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L524-L537) |
| 7a | `isvec` 且 `simt_stack` 且 opcode==0 | **vALU + SIMT 同时** | [540-555](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L540-L555) |
| 7b | `isvec` 且 `simt_stack` 且 opcode!=0 | 仅 SIMT | [556-569](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L556-L569) |
| 7c | `isvec`（非 simt） | 向量整数 vALU | [585-598](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L585-L598) |
| 8 | `barrier` | warp_sche（栅栏） | [600-613](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L600-L613) |
| 9 | `!barrier`（默认） | 标量 ALU（sALU） | [614-628](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L614-L628) |

以「张量核」这一支（最高优先级）为例，看它如何只接通一个目标：

[issue.v:454-467](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L454-L467)：当 `inputBuf_warps_control_Signals_tc` 为 1 时，`issue_out_TC_valid_o = inputBuf_valid`，`inputBuf_ready = issue_out_TC_ready_i`，而其余 9 个执行单元的 valid 全部清 0。其余分支结构完全相同，只是「被点亮」的目标不同。

**(3) 一个特殊情况：vALU 与 SIMT 联动**。当一条向量指令同时是 SIMT 栈操作（`isvec && simt_stack`）且 `opcode==0`（这是 JOIN 汇合类，见 u5-l3）时，[issue.v:545-547](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L545-L547) 会**同时**把 vALU 和 SIMT 的 valid 都点亮，并把 `inputBuf_ready` 设为「两者 ready 相与」。这是 `issue` 里**唯一**一拍驱动两个执行单元的情形——因为 JOIN 既要执行向量运算、又要通知 SIMT 栈恢复线程掩码，两者必须同步完成。

#### 4.1.4 代码实践

**实践目标**：验证 `issue` 每拍只点亮一个目标（除 JOIN 特例外），理解优先级的实际效果。

**操作步骤**（源码阅读型 + 波形观察型）：

1. 打开 `issue.v`，对照上表，在 [issue.v:452-643](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L452-L643) 里数一数每个分支：被赋值为 `inputBuf_valid` 的 valid 有几个？（答：除 7a 分支有两个外，其余都只有一个）。
2. 思考：如果一条指令的 `tc=1` 且 `fp=1`（编码出错时），按表它会去 TC 还是 vFPU？（答：TC，因为 `tc` 优先级更高，先匹配的分支先生效）。
3. 进阶（波形验证，待本地验证）：按 u1-l4 的方法跑 `make run-vcs-4w4t`，用 Verdi 打开 `test.fsdb`，在 `issue` 实例里观察 `issue_out_*_valid_o` 这 10 个信号。喂入不同类型指令时，确认任一时刻只有一个 valid 为 1（JOIN 指令除外）。

**需要观察的现象**：每拍最多一个执行单元的 valid 被点亮；当目标执行单元反压（`ready_i=0`）时，`issue_in_ready_o` 也跟着为 0，指令停在 `issue` 入口等待。

**预期结果**：`issue` 表现为无状态、无缓冲的优先级路由器；它本身不存指令，也不做冒险判断——冒险全交给 4.2 节的 `scoreboard`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `issue` 里标量 ALU（sALU）被放在优先级最低的 `else` 默认分支？

**答案**：因为大量普通标量整数指令（ADDI、ADD 等）不置位任何 `tc/sfu/fp/csr/mul/mem/isvec/barrier` 专属标志，自然落到最后的默认分支。把它作为默认，可以省去为每条标量指令单独译出一个 `is_sALU` 信号。

**练习 2**：`issue` 模块里有 `clk` / `rst_n` 端口吗？它内部有寄存器吗？

**答案**：在 pipe.v 例化时 `clk/rst_n` 被注释掉了（[pipe.v:1462-1463](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1462-L1463)）；模块内部除几个输出 valid 声明为 `reg`（仅因 `always @(*)` 赋值需要）外，没有任何状态寄存器。它是纯组合逻辑。

---

### 4.2 scoreboard：每个 warp 的「寄存器忙闲位图」

#### 4.2.1 概念说明

`issue` 自己不做冒险检测。如果一条向量加法 `v3 = v1 + v2` 紧跟着 `v4 = v3 + v5`，第二条要用第一条的结果 `v3`，而第一条可能还在 vALU 里没写回——直接放行第二条，它就会从寄存器堆读到 `v3` 的**旧值**。

本项目用一个朴素而有效的机制来挡住这种情况：**给每个寄存器配一个「忙」位**。

- 一条指令**进入操作数采集器**时，把它**要写的目的寄存器**对应的忙位置 1。
- 这个忙位一直保持 1，直到这条指令**写回**寄存器堆，才清 0。
- 任何后续指令，如果它的**源操作数或目的操作数**命中了某个忙位，就被判定为「有冲突」，不允许从 ibuffer 进入采集器。

这种「忙位」机制就叫**记分板（scoreboard）**。它粒度粗（只看寄存器号）、实现简单，代价是会保守地暂停一些其实可以并行的指令——但对单发射的 SM 流水线已经够用。

关键设计：**每个 warp 拥有自己独立的 scoreboard**。因为不同 warp 的寄存器堆逻辑上隔离，冒险只在同一 warp 内部发生。

#### 4.2.2 核心流程

记分板围绕「**置忙—检冲突—清忙**」三个动作运转：

```
指令 A 从 ibuffer 被 ibuffer2issue 选中，进入 operand_collector（fire）
        │  if_fire_i & (wvd|wxd)  ──►  把 A 的目的寄存器 R 在位图中置 1（busy）
        ▼
A 在采集器→issue→执行单元里流转（耗时若干拍，期间 R 一直 busy）
        │
        │  同时：warp 内下一条指令 B 停在 ibuffer 头部，scoreboard 持续检查它
        │  若 B 读/写 R ──► delay_o = 1 ──► warp_sche 暂停该 warp ──► B 进不了采集器
        ▼
A 执行完毕，写回寄存器堆（wb_fire）
        │  wb_v_fire_i & wvd  ──►  把 R 在位图中清 0（free）
        ▼
B 的冲突消失，delay_o = 0，B 被放行
```

这里有一个**贯穿全执行延迟**的关键：忙位在「进采集器」时置位、在「写回」时清除。所以 \(R\) 保持 busy 的时间，等于该指令从采集操作数、经过执行单元、直到写回的**整段流水延迟**。对于除法（SFU）、访存（LSU，可能等 dcache 缺失）这类长延迟操作，相关寄存器会长时间占住忙位，把所有依赖指令都挡住——这正是 4.4 节练习要观察的现象。

#### 4.2.3 源码精读

**(1) 位图状态寄存器**。[scoreboard.v:64-69](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/scoreboard.v#L64-L69) 定义了记分板的核心存储：

```verilog
reg [(1<<(`REGIDX_WIDTH+`REGEXT_WIDTH))-1:0] vectorReg;  // 向量寄存器忙闲位图
reg [(1<<(`REGIDX_WIDTH+`REGEXT_WIDTH))-1:0] scalarReg;  // 标量寄存器忙闲位图
reg beqReg;      // 分支/栅栏在途标志
reg opcolReg;    // 操作数采集器占用标志
reg fenceReg;    // fence 在途标志
```

其中 `REGIDX_WIDTH=5`、`REGEXT_WIDTH=3`（见 [define.v:61](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L61)、[define.v:63](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L63)），所以每个位图宽 \(2^{5+3}=256\) 位，每个寄存器号对应一位。

**(2) 向量忙位的置位与清除**。[scoreboard.v:81-93](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/scoreboard.v#L81-L93) 用 `generate` 为每一位生成一个 `always`，核心是这一句（第 89 行）：

```verilog
vectorReg[j] <= (if_fire_i && if_wvd_i && (j==if_reg_idxw_i)) ? 1'b1 :
                ((wb_v_fire_i && wb_v_wvd_i && (j==wb_v_reg_idxw_i)) ? 1'b0 : vectorReg[j]);
```

读法：当指令 fire 且它写向量寄存器（`if_wvd_i`）且目的号正是 `j`，则置 1；当写回 fire（`wb_v_fire_i`）且写向量寄存器且号是 `j`，则清 0；否则保持。

**(3) 标量忙位的特殊处理**。[scoreboard.v:95-107](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/scoreboard.v#L95-L107) 与向量同理，但第 103 行多了一个保护：

```verilog
scalarReg[i] <= (if_fire_i && if_wxd_i && (i==if_reg_idxw_i)) ? ((if_reg_idxw_i=='h0) ? 1'b0 : 1'b1) :
                ((wb_x_fire_i && wb_x_wxd_i && (i==wb_x_reg_idxw_i)) ? 1'b0 : scalarReg[i]);
```

注意 `((if_reg_idxw_i=='h0) ? 1'b0 : 1'b1)`：**写标量寄存器 x0 永远不置忙**。这与 RISC-V 语义一致——x0 是硬连线常量 0，写它无效，所以没必要为它挡住后续指令。

**(4) 冲突检测（读侧）**。这是记分板「判定是否放行」的部分。[scoreboard.v:148-174](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/scoreboard.v#L148-L174) 检查三个源操作数读端口 `read_rs1/rs2/rs3`，每个端口根据操作数选择码（`A1_*/A2_*/A3_*`）决定是查向量位图还是标量位图。以 `read_rs3` 为例：

[scoreboard.v:165-174](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/scoreboard.v#L165-L174) 中，`A3_SD`（store data）分支会根据 `isvec` 与 `readmask` 决定数据来自 v2 还是 v3、是向量还是标量；`A3_PC`（跳转目标用 PC）分支只在 `B_R`（jalr 间接跳转）时才检查 rs1。这些选择码定义在 [define.v:425-437](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L425-L437)，分支类型 `B_R=2'b11` 在 [define.v:421-424](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L421-L424)。

随后 [scoreboard.v:176-183](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/scoreboard.v#L176-L183) 汇总另外几类冲突：

```verilog
read_mask  = (ibuffer_if_mask_i) ? vectorReg[0] : 1'b0;   // 读掩码寄存器 v0
read_wb    = (wxd ? scalarReg[idxw] : 0) | (wvd ? vectorReg[idxw] : 0); // WAW：目的寄存器正忙
read_beq   = beqReg;        // 已有分支/栅栏在途
read_opcol = opcolReg;      // 本 warp 采集器已被占用
read_fence = ibuffer_if_mem_i && fenceReg;  // fence 期间禁止再发访存
```

要点解读：
- `read_mask`：很多向量指令带「条件执行掩码」，掩码来自向量寄存器 **v0**（即 `vectorReg[0]`），所以读掩码等价于依赖 v0。
- `read_wb`：检查的是**目的寄存器**是否正忙，这防的是 **WAW**（写后写）——上一条还没写回 R，这一条又要写 R，必须排队，否则写回顺序错乱。
- `read_opcol`：与 [scoreboard.v:122-133](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/scoreboard.v#L122-L133) 的 `opcolReg` 配合——它在指令**进采集器时置 1、离开采集器（被 issue 取走）时清 0**，从而保证「同一 warp 同时只有一条指令在采集操作数」。

**(5) 输出 delay_o**。[scoreboard.v:185](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/scoreboard.v#L185) 把所有冲突位「或」起来：

```verilog
assign delay_o = read_rs1|read_rs2|read_rs3|read_mask|read_wb|read_beq|read_opcol|read_fence;
```

只要有任何一种冲突，`delay_o=1`。

**(6) 在 pipe.v 中的例化与反馈**。这是理解记分板「怎么挡住指令」的关键一环。pipe.v 用 `generate` **为每个 warp 例化一个 scoreboard**（[pipe.v:1269-1338](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1269-L1338)）。几个关键连线（[pipe.v:1271-1278](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1271-L1278)）：

```verilog
assign scoreb_if_fire[i]       = (i == ibuffer2issue_warps_control_Signals_wid) ? ibuffer2issue_out_fire : 'b0;
assign scoreb_op_col_in_fire[i]= (i == ibuffer2issue_warps_control_Signals_wid) ? ibuffer2issue_out_fire : 'b0;
assign scoreb_op_col_out_fire[i]= (i == operand_collector_out_wid)              ? operand_collector_out_fire : 'b0;
assign scoreb_wb_v_fire[i]     = (i == wb_out_v_warp_id) ? wb_out_v_fire : 'b0;   // 向量写回
assign scoreb_wb_x_fire[i]     = (i == wb_out_x_warp_id) ? wb_out_x_fire : 'b0;   // 标量写回
```

注意 `scoreb_if_fire[i]` 与 `scoreb_op_col_in_fire[i]` 接的是**同一个信号** `ibuffer2issue_out_fire`（即 [pipe.v:762](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L762) 定义的「指令被 operand_collector 接收」那一刻）。这就是「进采集器即置忙」的来源。

最后，`delay_o` 汇成 `scoreb_delay[NUM_WARP]`，回送给 `warp_scheduler` 作为 `scoreboard_busy_i`（[pipe.v:937](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L937)）；warp_scheduler 综合它产生 `warp_sche_warp_ready`，而 [pipe.v:761](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L761) 用它门控 `ibuffer2issue_in_valid`：

```verilog
assign ibuffer2issue_in_valid = ibuffer_out_valid & warp_sche_warp_ready;
```

于是闭环成立：**记分板检测到冲突 → 该 warp 的 ready 拉低 → ibuffer2issue 不再选它 → 冲突指令停在 ibuffer，进不了采集器**。等到写回清除忙位、`delay_o` 回 0，warp 才恢复就绪。

#### 4.2.4 代码实践

**实践目标**：构造一个 RAW 冒险序列，在源码层面推演 scoreboard 的忙位变化，确认它如何阻止后指令过早发射、并在写回后释放。

**操作步骤**（源码阅读/推演型）：

1. 设想 warp 0 顺序执行两条向量指令（示例序列，非项目自带用例）：
   - I1: `vadd.vv v3, v1, v2`（v3 ← v1+v2，写 v3）
   - I2: `vadd.vv v4, v3, v5`（v4 ← v3+v5，读 v3）
2. 推演 scoreboard[wid=0] 的状态（参考 [scoreboard.v:89](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/scoreboard.v#L89)、[scoreboard.v:151-154](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/scoreboard.v#L151-L154)）：

| 时刻 | 事件 | vectorReg[3] | I2 能否发射？ | 原因 |
|---|---|---|---|---|
| t0 | I1 fire 进采集器（`if_fire & if_wvd`，idxw=3） | 0 → **1** | — | I1 占住 v3 |
| t1 | I2 到 ibuffer 头部，`read_rs1` 检查 v3 | 1 | **否**，`delay_o=1` | RAW：v3 仍忙 |
| t2… | I1 在 vALU 流水中 | 1 | 否 | 同上 |
| tk | I1 写回（`wb_v_fire & wb_v_wvd`，idxw=3） | 1 → **0** | — | v3 释放 |
| tk+1 | I2 重新评估 `read_rs1` | 0 | **是**，`delay_o=0` | 冲突消失，warp 恢复就绪 |

3. 波形验证（待本地验证）：跑 `make run-vcs-4w4t`，在 Verdi 里盯住 `scoreb_delay[0]`、写回信号 `wb_out_v_fire` 与 `issue_out_vALU_valid_o`，确认 vALU 上两条 add 之间出现了「等待写回」的空拍。

**需要观察的现象**：I2 不会紧接 I1 发射，中间隔了 I1 的执行+写回延迟；写回那一拍之后，`scoreb_delay[0]` 才回落为 0。

**预期结果**：RAW 冒险被正确挡住，且在写回后立即释放——这正是 scoreboard 的全部职责。

#### 4.2.5 小练习与答案

**练习 1**：scoreboard 同时检查源操作数（`read_rs1/2/3/mask`）和目的操作数（`read_wb`）。它们分别防的是哪种冒险？

**答案**：检查源操作数防 **RAW**（先写后读——后指令读、前指令还没写回）；检查目的操作数 `read_wb` 防 **WAW**（写后写——两条都写同一寄存器，需保证写回顺序，故第二条要等第一条写回）。本项目流水线结构下，WAR（先读后写）不会出问题，所以没专门检测。

**练习 2**：为什么 `opcolReg` 这一位也并进了 `delay_o`？它和 `vectorReg/scalarReg` 防的是同一类问题吗？

**答案**：不是。`vectorReg/scalarReg` 防的是**数据冒险**；`opcolReg` 防的是**结构冒险（资源冲突）**——操作数采集器每 warp 只有一份（见 4.3 节），同一 warp 不能同时有两条指令在采集，所以前一条没离开采集器前，后一条必须等。

---

### 4.3 NUM_ISSUE 与 NUM_COLLECTORUNIT：并发能力的两个旋钮

#### 4.3.1 概念说明

理解了 `issue` 与 `scoreboard`，再看两个规模参数，就能看清 SM 的「并发发射」能力上限：

- **`NUM_ISSUE`**：SM 里 `issue` 单元的个数。[define.v:25](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L25) 定义为 **1**。即每拍最多把 1 条指令路由到执行单元。
- **`NUM_COLLECTORUNIT`**：操作数采集器（collector unit）的个数。[define.v:21](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L21) 定义为 **`NUM_WARP`**——**每个 warp 配一个采集器**。

这两个数刻画的是一个**漏斗**：

```
NUM_WARP 个 warp  ──┐  （每个 warp 一个 collector，可并行采集操作数）
                    │
   NUM_COLLECTORUNIT = NUM_WARP 个采集器
                    │   （内部仲裁：选出一个操作数就绪的）
                    ▼
            1 个 issue（NUM_ISSUE = 1）
                    │   （每拍只路由 1 条）
                    ▼
        10 个执行单元（每拍只点亮 1 个）
```

也就是说：**采集端是「多」的（每 warp 一个），发射端是「一」的**。多个 warp 的指令可以在各自采集器里**同时**准备操作数（这是并发的来源）；但每拍只能有一个采集器的成果穿过唯一的 `issue`，被送到执行单元（这是瓶颈）。

#### 4.3.2 核心流程

把 4.1、4.2 串起来，一条指令从就绪到执行的完整接力是：

1. **就绪**：某 warp 在 ibuffer 有指令，且 scoreboard 对它判 `delay_o=0`（无冒险）。
2. **选 warp**：`ibuffer2issue` 用轮询仲裁（[ibuffer2issue.v:149-158](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/ibuffer2issue.v#L149-L158) 的 `round_robin_arb`）在多个就绪 warp 里挑一个。
3. **采集**：被选中的指令进入该 warp 的 collector unit，从标量/向量寄存器堆读出操作数（占若干拍）。期间 `opcolReg` 置位，挡住同 warp 的下一条。
4. **仲裁出队**：collector 之间再仲裁一次，把一个「操作数齐备」的指令送出（`operand_collector_out_valid`，[pipe.v:1466](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1466)）。
5. **路由**：`issue` 把它发往对应执行单元；执行结果最终写回，清除 scoreboard 忙位，释放等待中的后续指令。

因此，「同时有几条指令在执行」取决于：多少个 warp 的采集器**同时**准备好了操作数。warp 越多（`NUM_WARP` 越大、`NUM_COLLECTORUNIT` 随之越大），潜在并发越高，越能掩盖 `issue` 单发射的瓶颈与各执行单元（尤其 LSU/SFU）的长延迟。这也是 GPU 靠「多 warp 切换」隐藏延迟的同一思想。

#### 4.3.3 源码精读

参数定义：

- [define.v:21](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L21)：`NUM_COLLECTORUNIT` = `NUM_WARP`；[define.v:23](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L23)：`DEPTH_COLLECTORUNIT` = $clog2(NUM_COLLECTORUNIT)（collector 编号位宽）。
- [define.v:25](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L25)：`NUM_ISSUE` = 1。

漏斗的「窄口」连线在 pipe.v：`operand_collector` 的输出（单路 `out_valid_o`）直接喂给唯一的 `issue` 的 `issue_in_valid_i`（[pipe.v:1465-1466](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1465-L1466)），反向的 `out_ready_i` 接 `issue_in_ready`（[pipe.v:1410](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1410)）。这就是「多采集器 → 单 issue」的物理收敛点。

> 说明：`NUM_ISSUE`/`NUM_COLLECTORUNIT` 这两个宏在本讲所引文件里主要用于表达规模语义与文档约束；pipe.v 里 `issue` 与 `operandcollector_top` 的例化数量是硬编码为 1 份的，相关宏的具体消费点可在后续阅读 operand_collector 系列时进一步对照（待确认）。

#### 4.3.4 代码实践

**实践目标**：体会「collector 多、issue 少」对并发的影响。

**操作步骤**（源码阅读 + 配置对比型）：

1. 在 [define.v:21](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L21) 与 [define.v:25](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L25) 处确认两个参数取值；并回忆 u1-l3：`NUM_WARP` 是每核 warp 数。
2. 推理：若把 `NUM_WARP` 从 4 调到 8（`NUM_COLLECTORUNIT` 随之变 8），但 `NUM_ISSUE` 仍是 1，那么「同时能在采集器里准备操作数的指令」从 4 条变 8 条，但「每拍真正送进执行单元的」仍是 1 条。
3. 波形验证（待本地验证）：对比 `4w4t` 与更高 warp 配置的同一用例，观察 `issue` 入口 valid 的连续程度与执行单元的气泡数；warp 更多时，长延迟指令（如 LSU miss）造成的停顿更易被其他 warp 的指令填满。

**需要观察的现象**：`issue` 入口每拍至多一条指令；不同 warp 的指令在波形上交错出现在 `issue` 入口，这正是多 collector + 轮询仲裁的效果。

**预期结果**：`NUM_COLLECTORUNIT` 决定「能并行准备多少」，`NUM_ISSUE` 决定「每拍能发射多少」；后者是硬瓶颈。

#### 4.3.5 小练习与答案

**练习 1**：如果想让 SM 每拍发射 2 条指令，最小要改动哪些地方？

**答案**：不能只改 `NUM_ISSUE`。需要：(a) 把 `issue` 做成 2 路输入、每拍可路由 2 条；(b) 上游 `operand_collector` 的仲裁每拍要能输出 2 条；(c) 下游执行单元要有足够端口接收 2 条（且两条不能竞争同一执行单元）。这是结构性的扩展，牵涉 scoreboard、collector、执行单元多处。

**练习 2**：`NUM_COLLECTORUNIT` 为什么取 `NUM_WARP` 而不是 1？

**答案**：取 `NUM_WARP` 是为了让每个 warp 都能**独立、并行**地采集操作数，从而在一条 warp 被长延迟指令卡住（scoreboard 置忙）时，硬件能立刻切换到另一个已就绪的 warp 继续喂 `issue`，隐藏延迟。若只取 1，则所有 warp 抢一个采集器，并发性退化为单 warp，SM 会频繁停顿。

---

## 5. 综合实践

把 4.1～4.3 串成一个端到端的小任务：**用 scoreboard 的忙位视角，跟踪一条「读后写」依赖链穿过整条前端，并解释为何 `issue` 端会出现气泡、又如何被多 warp 填补。**

1. **构造序列**（示例序列，可在 tc_vecadd 框架下手工构造类似依赖，待本地验证）：
   - warp0：`I1: v3=v1+v2 (vadd.vv)` → `I2: v4=v3+v5 (vadd.vv)`（RAW on v3）
   - warp1：`J1: v6=v7+v8 (vadd.vv)`（与 warp0 无依赖）
2. **回答下列问题**（每问都给出对应的源码依据）：
   - (a) I1 fire 时，scoreboard[wid=0] 哪一位被置 1？依据 [scoreboard.v:89](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/scoreboard.v#L89)。
   - (b) I2 为何不能紧跟 I1 进入采集器？依次写出它命中的是 `read_rs1` 还是 `read_wb`，依据 [scoreboard.v:150-154](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/scoreboard.v#L150-L154) 与 [scoreboard.v:185](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/scoreboard.v#L185)。
   - (c) I1 写回那一拍，忙位如何变化？依据 [scoreboard.v:89](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/scoreboard.v#L89) 清除分支。
   - (d) I1 在 vALU 流水期间，warp0 被挡，此时 warp1 的 J1 为何仍能进入 `issue`？结合 4.3 的「多采集器 + 轮询仲裁」与 [ibuffer2issue.v:149-158](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/ibuffer2issue.v#L149-L158) 解释。
   - (e) I1 与 J1 都要去 vALU，但 `NUM_ISSUE=1`，它们如何排序？依据 [issue.v:452-643](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L452-L643)（单输入单输出）。
3. **产出**：画一张时序图，横轴为时钟拍，画出 `scoreb_delay[0]`、`issue_in_valid`、`issue_out_vALU_valid`、`wb_out_v_fire` 四条线，标注 I1/I2/J1 各自在哪一拍穿过 `issue`。

这个任务把「scoreboard 检测冒险 → warp 切换隐藏延迟 → issue 单发射串行化」三件事连成一条因果链，是理解 SM 前端调度的核心。

## 6. 本讲小结

- `issue` 是**纯组合的优先级路由器**：单输入（来自 operand_collector），按 `tc/sfu/fp/csr/mul/mem/isvec(+simt)/barrier/默认→sALU` 的优先级，把一条指令的握手接通到唯一一个执行单元（JOIN 指令例外，同时点亮 vALU+SIMT）。
- `scoreboard` 是**每 warp 一份**的寄存器忙闲位图（256 位向量/标量各一份）：指令进采集器时把目的寄存器置忙、写回时清忙；忙位贯穿整段执行延迟。
- 冲突检测在**读侧**（查 ibuffer 头指令的源/目寄存器是否命中忙位）汇总成 `delay_o`，回送 `warp_scheduler` 暂停该 warp，从而阻止冒险指令过早进入采集器——挡 RAW（源）与 WAW（目的）。
- 额外三位 `opcolReg`/`beqReg`/`fenceReg` 分别防「采集器结构冲突」「多分支在途」「fence 期间访存」。
- 规模上是一个**漏斗**：`NUM_COLLECTORUNIT=NUM_WARP`（每 warp 一个采集器，并发采集）收敛到 `NUM_ISSUE=1`（每拍单发射）。「多 warp 切换」是隐藏长延迟停顿的关键。

## 7. 下一步学习建议

- **向执行单元深入**：本讲停在「指令被送到哪个执行单元」。下一讲 u4-l1 将打开 `operand_collector` 内部，看 collector unit / operand_arbiter / 标量向量寄存器堆 bank 如何真正把操作数读出来；之后 u4-l2/u4-l4 分别精读 vALU、vFPU 等执行单元。
- **回看写回端**：scoreboard 的清除依赖写回信号 `wb_*_fire`，建议结合 u3-l1 里 `writeback` 模块（仲裁标量 6 路、向量 6 路）理解忙位为何能精确在写回当拍释放。
- **延伸到长延迟单元**：等学到 LSU（u5-l1）与 SFU（u4-l5）后，回头重做本讲的 RAW 实践，体会这些单元如何让忙位长时间置 1、又如何靠多 warp 切换把气泡填满。
- **SIMT 与分支**：本讲提到 JOIN 指令会同时驱动 vALU+SIMT；完整背景在 u5-l3（SIMT 栈与分支发散），可对照阅读。
