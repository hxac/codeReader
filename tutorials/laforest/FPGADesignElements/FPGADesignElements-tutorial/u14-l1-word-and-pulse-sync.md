# 字同步与脉冲同步

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚为什么多位的**数据字**可以「不经过同步器」直接跨时钟域，而只同步一个 `valid` 控制位，并且这样做是安全的。
- 把一个发送时钟域里的**单周期脉冲**可靠地送到接收时钟域，并讲明白「把脉冲变成电平翻转（toggle）再过 CDC」的原理。
- 区分**2 相握手**与**4 相握手**两种异步握手，能算出它们各自每传输一笔数据用掉几个信号边沿、最大脉冲速率差多少。
- 把 `CDC_Word_Synchronizer`、`CDC_Pulse_Synchronizer_2phase`、`CDC_Pulse_Synchronizer_4phase` 三个模块的源码串成一条调用链，并定位每个关键代码点。

## 2. 前置知识

本讲是 CDC（Clock Domain Crossing，时钟域穿越）单元的进阶篇，承接你已经学过的两讲：

- **u13-l1（亚稳态与 CDC 基本理论）**：你已知异步采样会撞出**亚稳态**，`CDC_Bit_Synchronizer` 用一串紧挨摆放的寄存器把亚稳态传播概率压到指数级低；并有一条铁律——**每次跨越只能同步一个位**，并行同步多位无法保证同延迟，会撕裂数据。本讲的一切都建立在这条铁律之上。
- **u13-l2（复位同步与标志同步）**：你已知 `CDC_Flag_Bit` 用「两个翻转寄存器的相对差」表达标志值，天然避开 setup/hold 竞争。本讲的「脉冲同步」其实是同一套 toggle 思想的标准化封装。
- **u9-l1（握手接口与握手过程）**：你已知 ready/valid 握手三信号、握手完成条件 `(ready && valid)`，以及 source 不得等 ready 才拉 valid 的防死锁约束。`CDC_Word_Synchronizer` 在两端都用 ready/valid 包裹，对外看起来就像一段普通流水线。

一个直觉式的复习：同步器只认「电平」，而且一个同步器通道一次只认一个位。那么问题来了——真实设计要传 32 位数据、要传「滴」一下就消失的脉冲，怎么办？这就是本讲要回答的两个问题。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `CDC_Word_Synchronizer.v` | 把一个 ready/valid 数据字从发送域送到接收域；只同步 1 个 `valid` 位，数据直接采样。 |
| `CDC_Pulse_Synchronizer_2phase.v` | 把发送域的单周期脉冲可靠送到接收域，采用 2 相异步握手（toggle）。 |
| `CDC_Pulse_Synchronizer_4phase.v` | 同样送脉冲，但采用 4 相异步握手（set/clear），用于对照。 |
| `cdc.html` | CDC 理论正文，其中一段直接给出了「字同步」的根本思路。 |
| 辅助原语（已在前序讲义学过，本讲直接复用） | `CDC_Bit_Synchronizer`、`Register_Toggle`、`Pulse_Generator`、`Pulse_Latch`、`Pipeline_to_Pulse`、`Pulse_to_Pipeline`。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**字同步**、**脉冲同步**、**2/4 相握手**。三者层层递进——字同步回答「多位数据怎么过」，脉冲同步回答「瞬时事件怎么过」，2/4 相握手则把脉冲同步的两种实现摆在一起对比。

### 4.1 字同步：多位数据只同步一个 valid

#### 4.1.1 概念说明

先复习 u13-l1 的铁律：**每次跨越只能同步一个位**。这条规矩的代价是「同步一个翻转位，接收时钟至少要比发送时钟快 1.5 倍」（即两次跳变之间至少要有 3 个接收时钟边沿）。如果我们要传一个 32 位的字，难道要付出 32 倍的代价吗？

`cdc.html` 给出了破局的一句话——把代价**摊到多个位上**：

> 代价可以被摊到多个位上：在一个时钟域里收齐多个位，然后把整个字**直接**（不经过同步！）送到另一个时钟域，只把一个 `valid` 位同步过去，让它在接收时钟域里充当锁存触发信号。

[CDC 理论正文 cdc.html:151-156](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/cdc.html#L151-L156)——这段话是整个 `CDC_Word_Synchronizer` 的设计依据。

为什么「数据直接过、只同步 valid」是安全的？关键在于 **ready/valid 握手 + 数据保持稳定**：

1. 数据在发送域被**锁进一个寄存器**，在整个握手往返期间**不动**。
2. 只有 `valid`（以 toggle 形式）这一个位穿越同步器。
3. 接收域等到同步过来的 `valid` 到了，再**采样**那个早已稳定的数据字。此刻数据已经稳定了远超同步器延迟的时间，不会有亚稳态。
4. 接收握手完成的信息再 toggle 回发送域，**重新允许**下一次发送——在此之前数据寄存器绝不会变。

于是亚稳态风险被完全关进那一个 `valid` 位的同步器里，数据多位只是「在它该被读的时候恰好一直稳着」。这就是「只同步一个 valid、直接采样数据」的全部秘密。

#### 4.1.2 核心流程

`CDC_Word_Synchronizer` 用一次 **2 相异步握手的往返**完成一笔数据传输，按传输顺序分四段：

```text
[发送域]                              [接收域]
 sending_valid/ready/data ──┐
                            ▼
              ① Pipeline_to_Pulse：握手完成 → 脉冲 + 数据
                            │
              ② Register：把数据锁存（之后整段往返都不变）
                            │
              ③ Register_Toggle：握手完成 → 翻转一个 toggle  ───CDC───▶  ④ Pulse_Generator：toggle→脉冲「新数据到」
                                                                        │
                                          ⑥ Pulse_to_Pipeline ◀────────┤  ⑤ 数据线直接接过来（无同步器！）
                                              接收 valid/ready/data      │
                                                │                        │
              ⑧ Pulse_Generator ◀──CDC─── ⑦ Register_Toggle：接收完成 → 翻转回
              toggle→脉冲 = accept_next_word
              （重新允许下一次发送）
```

要点：**数据线（多位）从发送域寄存器直达接收域模块，中间没有任何同步器**（图中第 ⑤ 步）；穿越 CDC 的只有两个单 bit 的 toggle（第 ③、⑦ 步）。

延迟与吞吐（来自源文件注释）：

- 一次完整传输耗时 = 发送侧 2~4 周期 + 接收侧 2~4 周期（两个方向的 CDC 各 1\*~3 周期，加两次 toggle/脉冲各 1 周期）。
- 其中带 `*` 的「1 周期」角落情形在两个方向上互斥，若不确知时钟近似同步（plesiochronous），保守按每方向 2~3 周期估算。

\[ \text{一次传输时间} \approx (2\sim4)\,T_{\text{send}} + (2\sim4)\,T_{\text{recv}} \]

#### 4.1.3 源码精读

**① 发送握手完成 → 拿到数据与「完成」脉冲。** 用 `Pipeline_to_Pulse` 把 ready/valid 接口转成脉冲接口，握手完成时吐出 `sending_handshake_complete` 与数据：

[CDC_Word_Synchronizer.v:130-150](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Word_Synchronizer.v#L130-L150)——`Pipeline_to_Pulse` 实例 `sending_handshake`，`module_ready` 接的是 `accept_next_word`（第 149 行），它决定何时允许收下一个字。

**② 锁存数据，整段往返期间保持稳定。** 这是「直接采样」安全性的物理保证：

```verilog
Register
#(.WORD_WIDTH(WORD_WIDTH), .RESET_VALUE(WORD_ZERO))
sending_data_storage
(
    .clock          (sending_clock),
    .clock_enable   (sending_handshake_complete),  // 仅握手完成时才改
    ...
    .data_in        (sending_handshake_data),
    .data_out       (sending_handshake_data_latched)
);
```

[CDC_Word_Synchronizer.v:156-168](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Word_Synchronizer.v#L156-L168)——`clock_enable` 只在握手完成时为 1，故 `sending_handshake_data_latched` 在两次发送之间恒定不变。

**③ 把「完成」变成一个 toggle（要过 CDC 的那一个位）。** 用 `Register_Toggle` 每完成一次握手翻转一次：

[CDC_Word_Synchronizer.v:179-192](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Word_Synchronizer.v#L179-L192)——`start_async_handshake`，`toggle` 端接 `sending_handshake_complete`，输出反馈回输入以保持静态。注释（170-175 行）特别强调：这个电平在下次握手完成前不会再翻转，而下次握手又只能发生在接收握手完成之后，所以它「恒定得足够久，能穿过 CDC」。

**④ toggle 过 CDC，并在接收域变回脉冲（锁存触发信号）。**

[CDC_Word_Synchronizer.v:199-208](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Word_Synchronizer.v#L199-L208)——`CDC_Bit_Synchronizer` 实例 `into_receiving`，这是整个模块**唯一**对 valid 信号做的同步。

[CDC_Word_Synchronizer.v:217-227](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Word_Synchronizer.v#L217-L227)——`Pulse_Generator` 用 `pulse_anyedge_out` 把任何一次 toggle 边沿变回单周期脉冲 `sending_handshake_data_latched_valid`，正是 cdc.html 说的「锁存触发信号」。

**⑤ 数据直接接进接收域（注意：没有同步器）。**

```verilog
Pulse_to_Pipeline receiving_handshake
(
    .clock                  (receiving_clock),
    ...
    .module_data_out        (sending_handshake_data_latched),  // 直连！多位跨域无同步
    .module_data_out_valid  (sending_handshake_data_latched_valid),
    ...
);
```

[CDC_Word_Synchronizer.v:234-258](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Word_Synchronizer.v#L234-L258)——第 253 行 `module_data_out` 直接接到第 ② 步那个发送域寄存器的输出。这一行就是「直接采样数据」的落点。`OUTPUT_BUFFER_TYPE` 决定接收端用 Half/Skid/FIFO 缓冲（注释 34-50 行详述），但与本讲的 CDC 正确性无关。

**⑦⑧ 接收完成 → toggle 回 → 重新允许发送。** 对称地再来一遍：接收握手完成翻转另一个 toggle（[269-282](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Word_Synchronizer.v#L269-L282)），过 CDC 回发送域（[289-298](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Word_Synchronizer.v#L289-L298)），变回脉冲 `accept_next_word`（[305-315](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Word_Synchronizer.v#L305-L315)），从而放开第 ① 步的 `module_ready`。闭环完成 2 相握手。

#### 4.1.4 代码实践

**实践目标**：亲手验证「只同步一个 valid、数据直接采样」是安全的，并指出风险被关在了哪里。

**操作步骤（源码阅读型）**：

1. 打开 `CDC_Word_Synchronizer.v`，从 `sending_data`（第 108 行）出发，追这条多位信号：它先到 `Pipeline_to_Pulse`，再被 `sending_data_storage` 锁存，最后在第 253 行**直接**进入 `receiving_handshake`。确认这条路径上**没有任何 `CDC_Bit_Synchronizer`**。
2. 再追 `sending_handshake_complete`（握手完成）：它去触发两件事——锁存数据（②）、翻转 toggle（③）。注意它能不能在 `accept_next_word` 拉高之前再次发生。
3. 数一数整个模块实例化了**几个** `CDC_Bit_Synchronizer`，分别同步的是哪两个信号。

**需要观察的现象 / 预期结果**：

- 多位数据线确实「裸奔」跨域，但它的更新被握手往返死死卡住：在接收域采样它的那一刻（`sending_handshake_data_latched_valid` 脉冲），它至少已经稳定了「toggle 过 CDC 的 1~3 个接收周期」，远超任何亚稳态窗口。
- 模块里恰好有 **2 个** `CDC_Bit_Synchronizer`（`into_receiving` 与 `into_sending`），各同步 1 个单 bit toggle——正好满足「每次只同步一个位」的铁律。**亚稳态风险全部被关在这两个同步器里。**

> 待本地验证：若你能在仿真里把发送时钟设得很慢、接收时钟很快，注入一个数据字，可观察到 `receiving_data` 在 `receiving_valid` 拉高那一拍与 `sending_data` 完全一致；若故意在往返途中改 `sending_data`（绕过握手），则不会影响已锁存值。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `sending_data_storage` 的 `clock_enable` 从 `sending_handshake_complete` 改成常 `1'b1`，「直接采样数据」还安全吗？为什么？

**参考答案**：不安全。`clock_enable` 常 1 意味着数据每拍都可能变，接收域就可能在数据正在翻转的那一拍去采样它，多位之间会撕裂（各位经不同路径延迟不同），而且失去握手保证的稳定窗口。安全性完全依赖「数据在往返期间不动」这个不变量。

**练习 2**：为什么本模块用 toggle（`Register_Toggle`）来表示 valid，而不是直接同步一个 0/1 电平的 valid？

**参考答案**：单次电平 valid 只能表达「一次」事件，且无法区分「新的有效」与「还没撤下的旧有效」。toggle 每发生一次事件就翻转一次，接收域用 `Pulse_Generator` 的 `anyedge` 把「任何一次翻转」还原成一个脉冲——这样每一笔数据都对应恰好一个 toggle 边沿、恰好一个接收脉冲，连续多笔传输也不会混淆。这正是下一节「脉冲同步」的同款思想。

---

### 4.2 脉冲同步：把瞬时事件变成 toggle 再过 CDC

#### 4.2.1 概念说明

现在换个问题：发送域有一个**单周期脉冲**（比如「计数器到顶了」「收到一个字节了」），要送到接收域。能不能直接用 `CDC_Bit_Synchronizer`？

不能。回忆 u13-l1 的 1.5× 频率约束：接收时钟若不够快，单周期脉冲可能根本采不到。`CDC_Bit_Synchronizer` 只保证「电平最终被正确采样」，不保证「短脉冲不被吞」。脉冲的持续时间未知、两域频率关系未知时，直接同步会丢事件。

解决办法（与字同步里的 toggle 同源）：**把脉冲变成一次电平翻转（toggle）**。toggle 是个电平，翻转后会一直保持新值，于是它「恒定得足够久」，必定能被接收时钟采到。接收域再把 toggle 的**任何一次边沿**还原成一个单周期脉冲。为保证下一次脉冲不被混在一起，这次 toggle 在收到「对岸已收到」的回执前，不许再翻——这就构成了一次往返的 2 相异步握手。

> 这个 toggle 思想你在 u13-l2 的 `CDC_Flag_Bit` 里已经见过（「两个翻转寄存器的相对差」）。区别是：`CDC_Flag_Bit` 表达一个持续的标志电平；这里的 `CDC_Pulse_Synchronizer` 表达一个瞬时事件，并在两端各还原成脉冲。

#### 4.2.2 核心流程

`CDC_Pulse_Synchronizer_2phase` 的握手往返：

```text
[发送域]                                    [接收域]
 sending_pulse_in
      │
 ① Pulse_Generator：清理成单周期脉冲 cleaned_pulse_in
      │
 ② Register_Toggle：脉冲来一次翻转一次（enable_toggle 为 0 时锁住不翻）
      │ sending_toggle
      ├──▶ ③ CDC_Bit_Synchronizer to_receiving ──▶ receiving_toggle
      │                                            │
 ⑤ enable_toggle =                    ④ Pulse_Generator(anyedge) → receiving_pulse_out
    (sending_toggle == toggle_response)
    (=sending_ready，系统静止时可再收)
      ▲
 ⑥ CDC_Bit_Synchronizer to_sending ◀── toggle_response（receiving_toggle 同步回来）
```

每来一个输入脉冲，`sending_toggle` 翻转一次；这个翻转过 CDC 后在接收域产生一个输出脉冲；接收域的 toggle 值再过 CDC 回到发送域，当 `sending_toggle == toggle_response` 时系统回到「静止」状态，`sending_ready` 拉高，允许下一个脉冲。

#### 4.2.3 源码精读

**① 清理输入脉冲为严格单周期。** 防止一个长输入脉冲在握手完成后仍为高、造成第二次翻转：

[CDC_Pulse_Synchronizer_2phase.v:109-121](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Pulse_Synchronizer_2phase.v#L109-L121)——`Pulse_Generator` 取 `pulse_posedge_out` 作为 `cleaned_pulse_in`。注释（105-107 行）说明用几个与门/非门也能做，但那样更难读懂、且不省逻辑。

**② toggle 寄存器：脉冲来一次翻一次。**

```verilog
Register_Toggle
#(.WORD_WIDTH(1), .RESET_VALUE(1'b0))
start_handshake
(
    .clock          (sending_clock),
    .clock_enable   (enable_toggle),   // 静止时才允许翻
    .clear          (1'b0),             // 见下方重要注解
    .toggle         (cleaned_pulse_in),
    .data_in        (sending_toggle),
    .data_out       (sending_toggle)
);
```

[CDC_Pulse_Synchronizer_2phase.v:134-151](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Pulse_Synchronizer_2phase.v#L134-L151)——注意 `clear` 接死 `1'b0`。源码注释（127-132 行）给出一条**反直觉但关键**的告诫：这里**不能用 clear**。若 toggle 寄存器当前为高、你用 clear 把它清零，这一下清零本身会被对岸当成一次握手，凭空产生一个假脉冲。

**⑤ ready = 系统静止。**

[CDC_Pulse_Synchronizer_2phase.v:156-159](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Pulse_Synchronizer_2phase.v#L156-L159)——`enable_toggle = (sending_toggle == toggle_response)`，二者相等意味着「我发的翻转，对岸已经收到了并翻转回来」，系统回到两个静止态之一，可以收下一个脉冲。`sending_ready` 直接等于它。

**③⑥ 两个方向的 CDC + ④ 接收域还原脉冲。**

[CDC_Pulse_Synchronizer_2phase.v:165-188](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Pulse_Synchronizer_2phase.v#L165-L188)——两个 `CDC_Bit_Synchronizer`：`to_receiving` 把 toggle 送过去，`to_sending` 把对岸的 toggle 值取回来作 `toggle_response`。

[CDC_Pulse_Synchronizer_2phase.v:193-203](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Pulse_Synchronizer_2phase.v#L193-L203)——接收域用 `Pulse_Generator` 的 **`pulse_anyedge_out`**：toggle 的**上升沿和下降沿都各产一个脉冲**。这是 2 相握手的核心——每个边沿都携带一次事件。

**仿真陷阱（务必了解）**：因为存在 toggle→CDC→toggle 的环且没有 clear，仿真中一旦有 X 值进入，会把 toggle 寄存器锁死在 X 态无法逃脱。源码注释（43-54 行）给出的 FPGA 解法是：操作开始前把 `sending_pulse_in` 用 `1'bX & 1'b0 = 1'b0` 关断，喂进一个确定的 0。

#### 4.2.4 代码实践

**实践目标**：跟踪一个输入脉冲走完整个 toggle 往返，并数清它用掉几个信号边沿。

**操作步骤（源码阅读 + 推演型）**：

1. 假设初始 `sending_toggle = 0`、`toggle_response = 0`（静止，`sending_ready = 1`）。
2. 来一个 `sending_pulse_in`。推演：`cleaned_pulse_in` 拉高一拍 → `sending_toggle` 翻成 1 → `enable_toggle` 变 0（不再收新脉冲）。
3. `sending_toggle` 过 `to_receiving`，1~3 拍后 `receiving_toggle` 变 1 → `receiving_pulse_out` 产出一个脉冲。
4. `receiving_toggle` 过 `to_sending`，再 1~3 拍后 `toggle_response` 变 1 → `enable_toggle` 重新为 1，`sending_ready` 拉高。
5. 数一数：**整个往返，`sending_toggle`/`receiving_toggle` 这些信号一共发生了几次翻转（边沿）？**

**预期结果**：`sending_toggle` 翻了 1 次（0→1），`toggle_response`（即同步回来的 `receiving_toggle`）也变了 1 次（0→1）。**每传输一个脉冲，系统消耗 2 个信号边沿**（一去一回）。这是 2 相握手「省」的根源，下节与 4 相对照时再用。

> 待本地验证：若在仿真中按上述步骤驱动，应观察到每输入一个脉冲、`receiving_pulse_out` 恰好输出一个脉冲，且二者之间隔了一次往返延迟（最坏约 4 个发送周期）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Register_Toggle` 的 `clear` 必须接死 `1'b0`？

**参考答案**：清零会改变 toggle 的电平值，而对岸是靠「检测 toggle 的任何边沿」来产生脉冲的。所以一次 clear 会被对岸当成一次合法的握手事件，凭空生成一个假脉冲。2 相握手没有「清零回到初值」的概念，只有「翻转/不翻转」。

**练习 2**：接收域为什么用 `pulse_anyedge_out`（任意边沿）而不是 `pulse_posedge_out`（仅上升沿）？

**参考答案**：2 相握手里 toggle 的上升沿和下降沿**都代表一次事件**（第一次传输是 0→1，第二次是 1→0，第三次又是 0→1……）。若只取上升沿，会丢掉所有 1→0 那次传输。所以必须用任意边沿。

---

### 4.3 2 相与 4 相握手：toggle vs set/clear

#### 4.3.1 概念说明

上一节的 2 相握手是异步握手的一种实现。经典异步握手理论里有两套约定：

- **2 相握手（non-return-to-zero，NRZ）**：每个事件用一次**翻转**表示，无论上升沿还是下降沿都算一次事件。每传输一笔数据，信号翻转**两次**（去一次、回一次）。
- **4 相握手（return-to-zero，RTZ）**：每个事件用一次「**置位再清零**」表示，只有上升沿算事件，之后必须**归零**才能开始下一次。每传输一笔数据，信号经历**四次**边沿（置位→回执→清零→回执）。

本仓库同时提供了两种脉冲同步器用于对照：`CDC_Pulse_Synchronizer_2phase`（上节）用 `Register_Toggle`；`CDC_Pulse_Synchronizer_4phase` 用 `Pulse_Latch`（置位/清零）。源码注释直言：除非你真的要省几个门，否则推荐用 2 相版，因为它能以约两倍的速率接收脉冲。

> 名词对照：`Pulse_Latch`（[Pulse_Latch.v:10-35](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Latch.v#L10-L35)）本质上是一个 `Register`：`pulse_in` 当 `clock_enable`、`data_in` 恒为 `1'b1`，于是来一个脉冲就把输出**置 1**并保持，直到 `clear` 把它清回 0。这正好是 4 相握手需要的「置位/清零」语义。

#### 4.3.2 核心流程

`CDC_Pulse_Synchronizer_4phase` 的握手（注意它要「来回两趟」）：

```text
[发送域]                                     [接收域]
 sending_pulse_in
      │
 ① Pulse_Latch：脉冲来 → sending_level 置 1（并保持）
      │ sending_level
      ├──▶ ② CDC_Bit_Synchronizer to_receiving ──▶ receiving_level
      │                                            │
      │                                 ③ Pulse_Generator(posedge) → receiving_pulse_out
      │                                            │
      │                                            （只用上升沿！清零回程不产脉冲）
      │ ◀── ④ CDC_Bit_Synchronizer to_sending ─── level_response（receiving_level 同步回来）
      │
 ⑤ 当 level_response=1 且脉冲已结束 → clear_sending → sending_level 清 0
      │ （清零这一程再次 ②→③，但因为是下降沿、不产接收脉冲）
      │ ◀── 再一次 ④：level_response 随之变 0
      │
 ⑥ sending_ready = (sending_level==0)&&(level_response==0)  // 双双归零才算静止
```

**关键差异**：4 相在置位（产生脉冲）之后，还必须再走一整趟「清零」往返，让电平归零，才能开始下一次。所以它每笔数据用掉 **4 个边沿**（置位、清零各一去，各带一程回执），是 2 相（2 个边沿）的两倍。

两种握手的边沿与速率对照：

| 维度 | 2 相（toggle） | 4 相（set/clear） |
| --- | --- | --- |
| 事件表示 | 一次翻转（任意边沿） | 置位后归零（仅上升沿为事件） |
| 核心原语 | `Register_Toggle` | `Pulse_Latch` |
| 接收端取边沿 | `pulse_anyedge_out` | `pulse_posedge_out` |
| 每笔数据用掉的信号边沿数 | **2** | **4** |
| 静止（ready）判定 | `sending_toggle == toggle_response` | `sending_level==0 && level_response==0` |
| 最大输入脉冲速率（接收「无限快」时） | 每 4 个发送周期 1 个 | 每 9 个发送周期 1 个 |
| 能否用 clear 复位 toggle/latch | 不能（会造假脉冲） | 能（清零本是流程一部分） |

#### 4.3.3 源码精读

**① 置位锁存 + ⑤ 受控清零。** 4 相用 `Pulse_Latch` 把脉冲变电平，清零时机很讲究：

```verilog
always @(*) begin
    clear_sending = (level_response == 1'b1) && (sending_pulse_in == 1'b0);
end

Pulse_Latch #(.RESET_VALUE(1'b0))
sending_pulse_capture
(
    .clock      (sending_clock),
    .clear      (clear_sending),
    .pulse_in   (sending_pulse_in),
    .level_out  (sending_level)
);
```

[CDC_Pulse_Synchronizer_4phase.v:96-114](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Pulse_Synchronizer_4phase.v#L96-L114)——`clear_sending` 必须**同时**满足「对岸已回执高」与「输入脉冲已结束」两个条件。注释（89-94 行）解释：若不卡 `sending_pulse_in == 0`，一个比往返延迟还长的输入脉冲会让锁存器在「置位-清零」之间反复横跳，在接收域生成一串脉冲。

**②④ 两个方向的 CDC。**

[CDC_Pulse_Synchronizer_4phase.v:118-143](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Pulse_Synchronizer_4phase.v#L118-L143)——结构与 2 相完全一样：`to_receiving` 把电平送过去，`to_sending` 把对岸电平取回作 `level_response`。

**⑥ ready = 双双归零。**

[CDC_Pulse_Synchronizer_4phase.v:150-152](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Pulse_Synchronizer_4phase.v#L150-L152)——`sending_ready = (sending_level == 1'b0) && (level_response == 1'b0)`。注释（145-148 行）警告：ready 为低时送进来的脉冲**会被丢**。

**③ 接收端只取上升沿。**

[CDC_Pulse_Synchronizer_4phase.v:157-167](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Pulse_Synchronizer_4phase.v#L157-L167)——`Pulse_Generator` 取 `pulse_posedge_out`（仅上升沿）。清零那趟回程是下降沿，不产生接收脉冲，这正是「归零」不致多产一个脉冲的原因。对照 2 相用的是 `pulse_anyedge_out`（[CDC_Pulse_Synchronizer_2phase.v:193-203](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Pulse_Synchronizer_2phase.v#L193-L203)），差异一目了然。

**速率推导（来自源码注释 49-68 行）**：接收「无限快」时，4 相的最坏情况为——锁存/清零各 1 周期、每个方向 CDC 回程 3 周期，且清零与锁存不可重叠（Register 里 clear 优先于数据），故至少 8 个空闲发送周期，**每 9 个发送周期 1 个脉冲**。2 相则是 **每 4 个发送周期 1 个脉冲**（[CDC_Pulse_Synchronizer_2phase.v:56-83](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Pulse_Synchronizer_2phase.v#L56-L83)）。两者之比约 \(9/4 \approx 2.25\)，源码概括为「2 相约两倍速率」。

#### 4.3.4 代码实践

**实践目标**：把「边沿数」与「最大速率」的对比落到一张可核对的表上。

**操作步骤**：

1. 分别打开两个脉冲同步器，确认：2 相用 `Register_Toggle` + `pulse_anyedge_out`；4 相用 `Pulse_Latch` + `pulse_posedge_out`。
2. 用上面 4.3.2 的流程图，各画一张时序草图，标出**一笔数据传输**期间所有 toggle/level 信号的每一次翻转。
3. 数边沿：2 相 = ? 次；4 相 = ? 次。
4. 核对源码注释给出的最大速率：2 相每 ? 个发送周期 1 个脉冲；4 相每 ? 个发送周期 1 个脉冲。

**预期结果**：

- 2 相：2 个边沿 / 笔；最大速率约每 4 个发送周期 1 个脉冲。
- 4 相：4 个边沿 / 笔；最大速率约每 9 个发送周期 1 个脉冲。
- 结论：边沿数翻倍 ⇒ 往返延迟翻倍 ⇒ 吞吐减半。除非要省几个门，优先选 2 相。

> 待本地验证：若用两个不同频率时钟在仿真里分别驱动两个模块，按 `sending_ready` 节拍尽可能快地喂脉冲，应观测到 2 相版能在更密的间隔下不丢脉冲。

#### 4.3.5 小练习与答案

**练习 1**：同样是「送一个脉冲」，为什么 4 相版要用掉 4 个边沿而 2 相只用 2 个？

**参考答案**：4 相用「置位」表示事件，事件之后还必须把电平「清零」回到静止态，否则下一次没法区分新旧事件——于是除「置位一去一回」外，还要「清零一去一回」，共 4 个边沿。2 相用「翻转」表示事件，翻转本身不依赖初值，下一次再翻即可，省掉了归零那一趟，只要「翻转一去一回」共 2 个边沿。

**练习 2**：4 相版的 `clear_sending` 为什么要加上 `(sending_pulse_in == 1'b0)` 这个条件？去掉会怎样？

**参考答案**：若输入脉冲比往返延迟还长，去掉该条件后，锁存器会在 `level_response` 一变高就立刻清零，而输入脉冲还在，于是又被置位……形成「置位-清零-置位-清零」的振荡，在接收域产生一串脉冲（一次事件被放大成多次）。加上该条件，保证清零发生在输入脉冲真正结束之后，杜绝这种「事件列车」。

---

## 5. 综合实践

把本讲三块内容串成一个设计任务。

**场景**：你有一个运行在 100 MHz 的传感器，每次完成测量会拉高一个单周期 `done` 脉冲，并同时在 32 位 `reading` 上给出新读数；后端处理逻辑跑在另一个时钟域（频率未知、可能更慢），用 ready/valid 接口收数据。

**任务**：

1. **选型**：你应该用 `CDC_Pulse_Synchronizer`（只传事件）还是 `CDC_Word_Synchronizer`（传数据+事件）？为什么？（提示：你需要的不只是「事件到了」，还有「这一笔的读数」。）
2. **解释安全性**：用本讲 4.1 的论证，说明为什么 `CDC_Word_Synchronizer` 让 32 位 `reading` 直接跨域、只同步一个 valid 是安全的——指明数据稳定窗口由谁保证、亚稳态风险被关在哪里。
3. **握手对照**：若你退而求其次、只传 `done` 脉冲，分别估算用 2 相和 4 相脉冲同步器时，100 MHz 侧每秒最多能可靠传多少个脉冲；说明为何本书推荐 2 相。
4. **复位注意**：读源码注释，说出为什么 2 相脉冲同步器**不能**用 clear 复位 toggle 寄存器，而 `CDC_Word_Synchronizer` 却有 `sending_clear`/`receiving_clear`（提示：字同步器的 clear 是作用于数据寄存器与 toggle 的复位初值，需配合 3 周期等待；见 [CDC_Word_Synchronizer.v:26-33](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CDC_Word_Synchronizer.v#L26-L33)）。

**参考要点**：

1. 选 `CDC_Word_Synchronizer`——因为每笔事件都带着 32 位数据，需要 valid 与数据一起过。
2. 数据在发送域被 `sending_data_storage` 锁存，在整个 2 相握手往返期间不变；接收域只在同步后的 valid 脉冲到达时采样它，此刻它已稳定远超同步器延迟；亚稳态风险全部关在唯一的 1 位 valid toggle 的 `CDC_Bit_Synchronizer` 里。
3. 2 相约每 4 个 100 MHz 周期 1 个 ⇒ 约 25 M pulses/s 量级；4 相约每 9 个周期 1 个 ⇒ 约 11 M pulses/s 量级。2 相边沿数减半、吞吐约翻倍，故推荐。
4. 2 相里清零 toggle 会改变其电平、被对岸当合法事件采到，凭空生成假脉冲；故 toggle 的 `clear` 接死 0。字同步器的 clear 作用于会保存数据的寄存器与 toggle 的初值复位，且要求两侧 clear 至少保持 3 个周期，让任何在途 toggle 都能到达对岸——这与「清零 toggle 寄存器」是两回事。

## 6. 本讲小结

- **字同步**解决「多位数据怎么过 CDC」：让数据在发送域锁存、整个握手往返期间保持稳定后**直接**跨域采样，**只同步一个 valid（以 toggle 形式）** 当锁存触发——这同时遵守了「每次只同步一个位」的铁律，又把 1.5× 频率代价摊到了整个字上。
- **脉冲同步**解决「瞬时事件怎么过 CDC」：把单周期脉冲变成一次 **toggle**，toggle 是电平、恒定得足够久能被采到，接收域再把任意边沿还原成脉冲。
- 脉冲同步用一次往返的 **2 相异步握手**保证两次事件不混淆：toggle 去、toggle 回，`sending_ready` 在系统静止时拉高。
- **2 相 vs 4 相**：2 相用 toggle（任意边沿）、每笔 2 个边沿、约每 4 周期 1 脉冲；4 相用 set/clear（仅上升沿）、每笔 4 个边沿、约每 9 周期 1 脉冲。边沿数翻倍导致吞吐减半，故默认选 2 相。
- 两个反直觉但重要的工程细节：2 相里 **toggle 寄存器不可用 clear**（会造假脉冲）；2 相存在 **X 值锁死**的仿真陷阱，启动前需把输入关断成确定 0。
- 三者共享同一套 CDC 纪律：亚稳态靠 `CDC_Bit_Synchronizer` 压概率，正确性靠「结构纪律」（数据稳定窗口、单 bit 同步、往返握手）而非仿真保证。

## 7. 下一步学习建议

- 本讲的脉冲/字同步都是**「请求-应答」往返式**传输，吞吐受往返延迟限制。要突破这个上限、连续突发传输多位数据，下一讲 **u14-l2（Flancter 与 CDC FIFO）** 讲 `Weinstein_Flancter`（跨域事件计数）与 `CDC_FIFO_Buffer`（用 Gray 码指针做读写指针跨域的异步 FIFO），是更高吞吐的方案。
- 想再把脉冲接口与 ready/valid 弹性流水线互转，可读 **u15-l2（脉冲与流水线互转）** 的 `Pulse_to_Pipeline` / `Pipeline_to_Pulse`——本讲的字同步器内部正是用这两个模块做接口转换的。
- 想深入理解本讲反复出现的 toggle/握手理论，可回看 **u9-l1（握手接口）** 与 **u11-l2（Muller C 元素与会合）**，把 ready/valid、2/4 相握手、Muller C 元素统一到「会合同步」这一个视角下。
