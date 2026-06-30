# aes_core 顶层控制与状态机

## 1. 本讲目标

入门篇我们看懂了「主机如何通过总线驱动 AES 核」和「如何跑仿真」，但始终把 `aes_core` 当成一个黑盒。本讲打开这个黑盒。

读完本讲，你应当能够：

- 说出 `aes_core` 在整个工程中的定位：它是顶层 wrapper `aes` 与具体算法子模块之间的「调度中枢」。
- 读懂 `aes_core_ctrl` 这个三状态 FSM（`IDLE`/`INIT`/`NEXT`）如何区分「密钥扩展」和「加/解密」两个阶段，并在每个阶段把不同的子模块接到共享资源上。
- 读懂 `encdec_mux` 如何根据 `encdec` 配置在加密通路与解密通路之间二选一，并理解两条通路如何**共享同一份轮密钥存储**。
- 读懂 `sbox_mux` 如何把「全工程唯一的 1 个正向 S-box」在密钥扩展与加密之间**分时复用**，并理解「解密为什么不用这个共享 S-box」。
- 在脑中（或纸上）画出 `init` 与 `next` 两种情况下，各子模块信号走向的连接示意图。

本讲只进入 `aes_core.v`，不深入 `aes_encipher_block` / `aes_decipher_block` / `aes_key_mem` 的内部 FSM——那是 u2-l3 ~ u2-l7 的事。本讲只关心 **core 怎么把它们调度起来**。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自入门篇 u1-l3 ~ u1-l5）：

- **两段式寄存器约定**：组合逻辑块算出「下一个值 `_new`」和「写使能 `_we`」，时序块在时钟沿执行 `if (_we) _reg <= _new`。所有寄存器都在 `always @(posedge clk or negedge reset_n)` 里，采用异步低有效复位、非阻塞赋值。
- **顶层接口主线**：主机写 `KEY` → 写 `CONFIG` → 写 `CTRL.init`（触发密钥扩展）→ 写 `BLOCK` → 写 `CTRL.next`（触发加/解密）→ 轮询 `STATUS.ready` → 读 `RESULT`。其中 `CTRL.init`/`CTRL.next` 是**单拍脉冲**。
- **testbench 层次化引用**：`tb_aes` 里实例名是 `dut`，即 `aes dut(...)`；而 `aes` 内部把 core 实例化为 `core`，所以 core 内部寄存器的仿真路径形如 `dut.core.xxx_reg`。

补充两个本讲要用到的小概念：

- **时分复用（time-multiplexing）**：一份硬件资源（这里是 1 个 S-box）在不同时刻分配给不同使用者，从而省下面积。代价是使用者不能同时用，吞吐会下降。
- **轮密钥（round key）**：AES 每一轮都要和一个 128 位的轮密钥做异或（AddRoundKey）。这些轮密钥由「密钥扩展」从原始密钥一次性算出来，存进 `key_mem[0:14]` 数组里，加/解密时按轮号取用。

## 3. 本讲源码地图

本讲只涉及 1 个核心源码文件，但会引用 2 个上下文文件：

| 文件 | 作用 | 本讲怎么用 |
| --- | --- | --- |
| [rtl/aes_core.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v) | AES 核心调度中枢：例化 4 个子模块，含 1 个控制 FSM + 2 个多路选择 | **本讲全部精读对象** |
| [rtl/aes.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v) | 顶层 wrapper，把 `init`/`next` 脉冲和配置传给 core | 只看它如何调用 core（上下文） |
| [rtl/tb_aes.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v) | 自检式测试平台 | 代码实践时用来观察 core 内部状态 |

`aes_core.v` 内部的结构可以预先在脑里分成三块：

1. **4 个子模块例化**（`enc_block`、`dec_block`、`keymem`、`sbox_inst`）——这是「硬件资源」。
2. **3 个组合逻辑 `always @*` 块**（`sbox_mux`、`encdec_mux`、`aes_core_ctrl`）——这是「连线与控制」，本讲的三个最小模块。
3. **1 个时序逻辑块**（`reg_update`）——把控制块算出的 `_new/_we` 落地到寄存器。

---

## 4. 核心概念与源码讲解

### 4.1 aes_core 的定位与子模块连线总览

#### 4.1.1 概念说明

如果把整个 AES 核比作一个小工厂：

- `aes_key_mem` 是**仓库**：存着所有轮密钥，别人报一个轮号，它吐出对应的 128 位轮密钥；另外它还负责「密钥扩展」这道工序（把原始密钥加工成一堆轮密钥）。
- `aes_encipher_block` 是**加密车间**：吃进明文 + 轮密钥，吐出密文。
- `aes_decipher_block` 是**解密车间**：吃进密文 + 轮密钥，吐出明文。
- `aes_sbox` 是一台**昂贵而唯一的专用设备**（正向 S-box 查表），加密车间和仓库都需要它，但全厂只有 1 台。

`aes_core` 就是这个工厂的**车间主任 + 调度员**：它不亲自做加密运算，它只决定「现在让谁干活」「让谁用到那台唯一的 S-box」「把谁的结果交出去」。理解了这层定位，后面三个模块（FSM + 两个 mux）就只是这个调度员的具体规则。

#### 4.1.2 核心流程

core 对外的端口非常精简，来自顶层 `aes`（见 [rtl/aes.v:116-131](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L116-L131)）：

```text
输入：  clk, reset_n
        encdec  (1=加密, 0=解密)        ← 来自 CONFIG
        init    (密钥扩展脉冲)          ← 来自 CTRL.init
        next    (加/解密脉冲)           ← 来自 CTRL.next
        key[255:0], keylen              ← 密钥与长度
        block[127:0]                    ← 明文/密文
输出：  ready                           ← 回给 STATUS.ready
        result[127:0], result_valid     ← 回给 RESULT 与 STATUS.valid
```

core 内部用三条「总线式连线」把子模块串起来：

- **轮密钥线 `round_key`**：`key_mem` 输出 → 同时喂给 `enc_block` 和 `dec_block`。两条通路**共享同一份轮密钥存储**。
- **S-box 输入线 `muxed_sboxw`**：由 `sbox_mux` 在 `keymem_sboxw` 与 `enc_sboxw` 之间二选一 → 喂给唯一的 `sbox_inst`。
- **结果/轮号线**：由 `encdec_mux` 在 enc 与 dec 之间二选一，选出 `muxed_new_block`（结果）、`muxed_round_nr`（喂回 key_mem 决定取哪把轮密钥）、`muxed_ready`（谁干完了）。

调度规则只有一句话：**「现在是 init 阶段还是 next 阶段」决定 S-box 给谁；「encdec 是 1 还是 0」决定加/解密通路给谁。**

#### 4.1.3 源码精读

先看 4 个子模块是怎么连起来的（这是后续两个 mux 操作的「物理基础」）：

[rtl/aes_core.v:86-102](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L86-L102) —— **加密车间**。注意它有 `sboxw`（输出要查表的字）和 `new_sboxw`（查表结果输入）两个端口，说明它**依赖外部共享 S-box**：

```verilog
aes_encipher_block enc_block(
    .next(enc_next),          // 由 encdec_mux 决定是否给它 next
    .round(enc_round_nr),     // 它告诉 core 当前算到第几轮
    .round_key(round_key),    // ← 共享的轮密钥
    .sboxw(enc_sboxw),        // → 要查 S-box 的字（候选）
    .new_sboxw(new_sboxw),    // ← S-box 查表结果
    .block(block), .new_block(enc_new_block), .ready(enc_ready));
```

[rtl/aes_core.v:105-118](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L105-L118) —— **解密车间**。关键区别：它的端口里**没有** `sboxw`/`new_sboxw`！也就是说解密不走那个共享 S-box（它内部自己例化了逆 S-box，见 u1-l2 的结论）。它同样从 `round_key` 拿轮密钥。

[rtl/aes_core.v:121-135](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L121-L135) —— **仓库（密钥存储 + 扩展器）**。它吃 `init`、吃 `round`（轮号，来自 `muxed_round_nr`），吐 `round_key`、`ready`（重命名为 `key_ready`）。它也有 `sboxw`/`new_sboxw` 端口——密钥扩展需要用正向 S-box 做 SubWord：

```verilog
aes_key_mem keymem(
    .init(init),              // ← 直接接顶层 init 脉冲
    .round(muxed_round_nr),   // ← 由 encdec_mux 选出的轮号
    .round_key(round_key),    // → 喂给 enc 与 dec 两条通路
    .ready(key_ready),
    .sboxw(keymem_sboxw),     // → 密钥扩展要查 S-box 的字（候选）
    .new_sboxw(new_sboxw));   // ← S-box 查表结果
```

[rtl/aes_core.v:138](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L138) —— **全工程唯一的正向 S-box**。输入是 `muxed_sboxw`（被两个 mux 之一选出来的），输出 `new_sboxw` 同时回连到 `enc_block` 和 `keymem`：

```verilog
aes_sbox sbox_inst(.sboxw(muxed_sboxw), .new_sboxw(new_sboxw));
```

最后是三条对外的连续赋值（[rtl/aes_core.v:144-146](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L144-L146)）：`result = muxed_new_block`（结果直接取自 encdec_mux 的输出）、`ready = ready_reg`、`result_valid = result_valid_reg`。注意 **`result` 是纯组合输出**，它实时跟随被选中的那个车间。

> 小结这一节：`round_key` 这根线把仓库和两个车间连成「一存多读」；`new_sboxw` 这根线把唯一 S-box 的结果同时回灌给加密车间与仓库；而到底谁在用 S-box、谁的结果被交出去，由下面 4.2 ~ 4.4 的三块逻辑决定。

#### 4.1.4 代码实践

**目标**：在不运行仿真的前提下，纯靠阅读源码把 core 的连线关系画出来。

**步骤**：

1. 打开 [rtl/aes_core.v:59-80](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L59-L80)，这是 core 的全部 wire/reg 声明区。
2. 给每一个 wire 标注「谁驱动它（输出端）」和「谁消费它（输入端）」。例如：
   - `round_key`：驱动者 = `keymem.round_key`；消费者 = `enc_block.round_key` + `dec_block.round_key`。
   - `new_sboxw`：驱动者 = `sbox_inst.new_sboxw`；消费者 = `enc_block.new_sboxw` + `keymem.new_sboxw`。
3. 找出哪些 wire 是「悬空候选」（需要在 mux 里被赋值才能确定）：`muxed_sboxw`、`muxed_round_nr`、`muxed_new_block`、`muxed_ready`、`enc_next`、`dec_next`。

**预期结果**：你会得到一张「4 个子模块 + 6 个 muxed/next 变量」的连线表。这张表就是 4.2~4.4 三个 mux/FSM 要填空的「插槽」。

#### 4.1.5 小练习与答案

**练习 1**：`block`（明文/密文输入）同时连到了 `enc_block` 和 `dec_block`，这样做会不会导致加密和解密同时发生？

**答案**：不会。虽然 `block` 同时接到两个车间，但只有收到 `next` 脉冲的那个车间才会启动内部 FSM。`enc_next` 与 `dec_next` 由 `encdec_mux` 互斥地赋值（同一拍只有一个为 `next`，另一个被钳为 0，见 4.3.3），所以同一时刻只有一个车间真正干活。

**练习 2**：解密车间 `dec_block` 没有 `sboxw`/`new_sboxw` 端口，那它怎么做 SubBytes 的逆运算？

**答案**：它在模块内部自己例化了一个逆 S-box（`aes_inv_sbox`，见 u1-l2）。正向 S-box 全工程只有 1 个、且被加密与密钥扩展共享；逆向 S-box 是解密车间私有的，不参与 core 层的资源共享。这是 4.4 节 sbox_mux 只在 `keymem_sboxw` 与 `enc_sboxw` 之间二选一的根本原因。

---

### 4.2 aes_core_ctrl：IDLE/INIT/NEXT 三状态 FSM

#### 4.2.1 概念说明

`aes_core` 这个「调度员」其实只有一个很简单的脑子里循环：**我现在在干什么？** 答案只有三种：

- `CTRL_IDLE`：闲着，等命令。
- `CTRL_INIT`：正在做密钥扩展（让 `key_mem` 把原始密钥展开成一堆轮密钥）。
- `CTRL_NEXT`：正在做一次加/解密（让被选中的车间跑完整轮变换）。

注意这是一个**粗粒度**的状态机：它不知道「加密第 3 轮」还是「加密第 7 轮」，那种细粒度的轮循环在 `enc_block`/`dec_block`/`key_mem` 各自的内部 FSM 里（u2-l3、u2-l5、u2-l7）。core 层只负责「启动谁、等谁完成、收回 ready/valid」。

#### 4.2.2 核心流程

状态定义见 [rtl/aes_core.v:33-35](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L33-L35)：

```verilog
localparam CTRL_IDLE = 2'h0;
localparam CTRL_INIT = 2'h1;
localparam CTRL_NEXT = 2'h2;
```

状态转移规则（用伪代码描述）：

```text
IDLE:
    if (init 脉冲):      → INIT      # 主机要扩展密钥
                          同时拉低 ready（开始忙）
    else if (next 脉冲): → NEXT      # 主机要加/解密
                          同时拉低 ready

INIT:                              # 密钥扩展进行中
    if (key_ready):      → IDLE     # key_mem 算完了
                          拉高 ready（闲下来了）

NEXT:                               # 加/解密进行中
    if (muxed_ready):    → IDLE     # 被选中的车间算完了
                          拉高 ready，并置 result_valid=1
```

三条关键观察：

1. **`init` 与 `next` 是互斥的单拍脉冲**。顶层 `aes` 把 `CTRL.init`/`CTRL.next` 写进寄存器后，下一拍就清零（脉冲寄存器，见 u1-l3）。所以 FSM 必须在 `IDLE` 那一拍「抓住」脉冲并完成转移。
2. **进入 `INIT`/`NEXT` 的那一拍，`ready` 立刻被拉低**，告诉主机「我忙起来了，别再下发命令」。这就是 `STATUS.ready` 的来源。
3. **只有 `NEXT` 完成时才置 `result_valid=1`**。密钥扩展完成（`INIT`）不产生结果，所以不置 valid。这与顶层 `STATUS.valid` 的语义一致：valid 表示「RESULT 寄存器里有一个可读的有效结果」。

#### 4.2.3 源码精读

FSM 用本工程标准的「两段式」写法：组合块 `aes_core_ctrl` 算下一状态和各 `_new/_we`，时序块 `reg_update` 落地。先看时序块 [rtl/aes_core.v:156-175](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L156-L175)：

```verilog
always @ (posedge clk or negedge reset_n)
  begin: reg_update
    if (!reset_n) begin
      result_valid_reg  <= 1'b0;
      ready_reg         <= 1'b1;     // 复位后默认 ready=1（可接受命令）
      aes_core_ctrl_reg <= CTRL_IDLE;
    end
    else begin
      if (result_valid_we) result_valid_reg <= result_valid_new;
      if (ready_we)        ready_reg        <= ready_new;
      if (aes_core_ctrl_we) aes_core_ctrl_reg <= aes_core_ctrl_new;
    end
  end
```

要点：复位时 `ready_reg <= 1`（上电即可用），状态回 `IDLE`，三个寄存器都靠各自的 `_we` 独立门控——这是 u1-l3 讲过的「reg/_new/_we 三件套」。

组合块 `aes_core_ctrl`（[rtl/aes_core.v:234-303](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L234-L303)）。块开头先给所有输出写默认值（防 latch，u1-l3 约定）：

```verilog
always @*
  begin : aes_core_ctrl
    init_state        = 1'b0;   // 默认 S-box 给加密
    ready_new         = 1'b0;
    ready_we          = 1'b0;
    result_valid_new  = 1'b0;
    result_valid_we   = 1'b0;
    aes_core_ctrl_new = CTRL_IDLE;
    aes_core_ctrl_we  = 1'b0;
    case (aes_core_ctrl_reg) ...
```

`IDLE` 分支（[rtl/aes_core.v:245-267](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L245-L267)）——抓脉冲：

```verilog
CTRL_IDLE:
  begin
    if (init) begin
      init_state        = 1'b1;          // ★ S-box 让给 key_mem
      ready_new         = 1'b0; ready_we = 1'b1;   // 开始忙
      aes_core_ctrl_new = CTRL_INIT; aes_core_ctrl_we = 1'b1;
    end
    else if (next) begin
      init_state        = 1'b0;          // ★ S-box 让给 enc
      ready_new         = 1'b0; ready_we = 1'b1;
      aes_core_ctrl_new = CTRL_NEXT; aes_core_ctrl_we = 1'b1;
    end
  end
```

注意 `init` 优先于 `next`（`if ... else if`）。`init_state` 这个信号就是 4.4 节 `sbox_mux` 的选择开关——它在 `IDLE→INIT` 这一转移拍就被置 1 了，于是在整个 `INIT` 阶段 S-box 都归 key_mem 使用。

`INIT` 分支（[rtl/aes_core.v:269-280](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L269-L280)）——等密钥扩展完成：

```verilog
CTRL_INIT:
  begin
    init_state = 1'b1;                   // 整个 INIT 阶段 S-box 都给 key_mem
    if (key_ready) begin                 // key_mem 说"轮密钥都算好了"
      ready_new = 1'b1; ready_we = 1'b1; // 拉高 ready
      aes_core_ctrl_new = CTRL_IDLE; aes_core_ctrl_we = 1'b1;
    end
  end
```

`NEXT` 分支（[rtl/aes_core.v:282-295](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L282-L295)）——等加/解密完成：

```verilog
CTRL_NEXT:
  begin
    init_state = 1'b0;                   // 整个 NEXT 阶段 S-box 都给 enc
    if (muxed_ready) begin               // 被选中的车间说"算完了"
      ready_new        = 1'b1; ready_we         = 1'b1;
      result_valid_new = 1'b1; result_valid_we  = 1'b1;  // ★ 置结果有效
      aes_core_ctrl_new = CTRL_IDLE; aes_core_ctrl_we = 1'b1;
    end
  end
```

> 这里有一个容易忽略的细节：`NEXT` 阶段 `init_state=0`，所以 S-box 永远给加密车间。如果这次是**解密**（`encdec=0`），加密车间收到 `enc_next=0`（见 4.3）根本不干活，共享 S-box 实际上是「闲置」的——解密车间用它内部私有的逆 S-box。换言之，解密期间那台唯一的正向 S-box 是空闲的。这是「以面积换吞吐」的一个直接后果（详见 u3-l4）。

#### 4.2.4 代码实践

**目标**：在仿真里亲眼看到 `aes_core_ctrl_reg` 在三个状态之间跳转。

**步骤**：

1. 复制一份 `rtl/tb_aes.v` 为 `tb_aes_trace.v`（**只改副本，不动原 testbench**），把顶层模块名也改成 `tb_aes_trace`。
2. 在 `init_sim` 之后、主测试开始之前，加一个监控进程，用层次化路径打印 core 的状态（这是仿真特权，仅限 testbench）：

   ```verilog
   // 示例代码：仅在 testbench 副本中添加，用于观察 core 内部状态
   always @ (posedge clk) begin
     $display("t=%0t state=%0d ready=%0d valid=%0d key_ready=%0d",
              $time, dut.core.aes_core_ctrl_reg, dut.core.ready_reg,
              dut.core.result_valid_reg, dut.core.key_ready);
   end
   ```

3. 用 iverilog 编译运行：

   ```bash
   iverilog -o sim_trace -g2012 rtl/aes.v rtl/aes_core.v rtl/aes_sbox.v \
       rtl/aes_inv_sbox.v rtl/aes_encipher_block.v rtl/aes_decipher_block.v \
       rtl/aes_key_mem.v tb_aes_trace.v
   vvp sim_trace | grep -E "state="
   ```

**需要观察的现象**：每完成一个 NIST 用例，状态序列应当是 `0(IDLE) → 1(INIT) → 0(IDLE) → 2(NEXT) → 0(IDLE)`。`INIT` 持续若干拍（密钥扩展），`NEXT` 持续更多拍（加/解密各轮）。`ready` 在 `INIT`/`NEXT` 期间为 0，回到 `IDLE` 时为 1。`valid` 仅在 `NEXT` 结束那一拍才出现 1。

**预期结果**：16 组用例（AES-128 各 8 组 + AES-256 各 8 组，详见 tb_aes.v:426-477）每组的 state 轨迹都符合上述模式。**待本地验证**：具体每个状态停留多少拍取决于 key_mem/encipher/decipher 内部 FSM，会在 u2-l3、u2-l5、u2-l7 给出精确周期数。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ready_reg` 复位初值是 `1`，而 `result_valid_reg` 复位初值是 `0`？

**答案**：复位后核应当立即「可接受命令」，所以 `ready=1`。但此时还没有任何加/解密结果，RESULT 寄存器内容无效，所以 `result_valid=0`。这两个初值保证了上电后主机看到「ready 但 not valid」的正确语义。

**练习 2**：如果主机在 `INIT` 阶段（ready=0）又写了一次 `CTRL.next`，会发生什么？

**答案**：什么都不会发生。`CTRL_INIT` 分支里只检查 `key_ready`，根本不看 `init`/`next`。顶层 `aes` 把脉冲写进 `next_reg` 后，由于 core 还在 `INIT`、没回 `IDLE`，这次 `next` 脉冲会被「错过」（脉冲只活一拍）。这就是为什么主机必须**轮询 STATUS.ready 等到 1** 才能下发下一条命令——这是 u1-l4 强调的握手规则在 core 层的体现。

**练习 3**：`aes_core_ctrl` 组合块开头为什么要把所有 `_we` 先置 0、`aes_core_ctrl_new` 先置 `CTRL_IDLE`？

**答案**：防止生成锁存器（latch）。`always @*` 块必须给每个被赋值的 reg 在所有分支路径下都有确定的值；先写默认值再用 `case` 覆盖，是最稳妥的写法。即使 `default` 分支什么都不做（[rtl/aes_core.v:297-300](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L297-L300)），由于默认值已就位，输出也始终确定。

---

### 4.3 encdec_mux：加密与解密通路的多路选择

#### 4.3.1 概念说明

core 例化了加密和解密**两个**车间，但同一时刻只能让一个干活、也只能把一个的结果交出去。`encdec_mux` 就是这个「二选一」开关。它的选择信号就是 `encdec`（来自 CONFIG 寄存器的 bit0，1=加密、0=解密）。

需要特别理解的是：`encdec_mux` 不只是选「结果」，它同时管四件事：

1. **把 `next` 脉冲发给谁**（`enc_next` 还是 `dec_next`）。
2. **把谁的轮号喂回 `key_mem`**（`muxed_round_nr`），从而决定仓库吐出哪把轮密钥。
3. **把谁的结果接到顶层 `result`**（`muxed_new_block`）。
4. **把谁的完成信号上报给 FSM**（`muxed_ready`）。

第 2 点尤其关键：它解释了「为什么加密和解密能共享同一份 `key_mem`」——因为轮密钥是按轮号索引的，只要把正确的轮号喂进去，同一块存储就能服务任一条通路。

#### 4.3.2 核心流程

```text
默认：enc_next = 0; dec_next = 0;    // 先钳零，防误触发

if (encdec == 1):    # 加密
    enc_next        = next            # 把启动脉冲只给加密车间
    muxed_round_nr  = enc_round_nr    # 加密报的轮号 → key_mem
    muxed_new_block = enc_new_block   # 加密结果 → 顶层 result
    muxed_ready     = enc_ready       # 加密的完成信号 → FSM
else:                # 解密
    dec_next        = next            # 启动脉冲只给解密车间
    muxed_round_nr  = dec_round_nr    # 解密报的轮号 → key_mem
    muxed_new_block = dec_new_block   # 解密结果 → 顶层 result
    muxed_ready     = dec_ready       # 解密的完成信号 → FSM
```

#### 4.3.3 源码精读

[rtl/aes_core.v:203-224](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L203-L204)（完整范围 L203-L224）：

```verilog
always @*
  begin : encdec_mux
    enc_next = 1'b0;          // ★ 默认钳零，确保只有一边收到脉冲
    dec_next = 1'b0;

    if (encdec) begin
      // Encipher operations
      enc_next        = next;
      muxed_round_nr  = enc_round_nr;
      muxed_new_block = enc_new_block;
      muxed_ready     = enc_ready;
    end
    else begin
      // Decipher operations
      dec_next        = next;
      muxed_round_nr  = dec_round_nr;
      muxed_new_block = dec_new_block;
      muxed_ready     = dec_ready;
    end
  end
```

几个要点：

- `enc_next`/`dec_next` 在块开头**先置 0**，再在对应分支里赋 `next`。这保证未选中的车间一定收到 0，不会误启动。这也是 `always @*` 防 latch 的标准做法。
- 注意 `encdec` 是**纯组合**选择，**与 FSM 状态无关**。也就是说，即使在 `IDLE` 空闲时，`muxed_*` 也在实时跟随 `encdec` 切换——只是此时没有 `next` 脉冲、没有结果产生而已。
- `muxed_round_nr` 是 4 位（[rtl/aes_core.v:74](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L74)），它最终接到 `keymem.round`（[rtl/aes_core.v:129](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L129)）。轮号范围 0~14 正好覆盖 AES 的最多 14 轮 + 初始轮。

> 联动 4.2：`muxed_ready` 正是 `CTRL_NEXT` 分支里 `if (muxed_ready)` 判断的那个信号。所以 encdec_mux 选了哪条通路，FSM 就等哪条通路的 ready——三者环环相扣。

#### 4.3.4 代码实践

**目标**：验证「解密时加密车间不动」与「轮号正确路由」。

**步骤**（纯源码阅读型实践）：

1. 在 [rtl/aes_core.v:62-71](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L62-L71) 找到 `enc_next/dec_next`、`enc_round_nr/dec_round_nr`、`enc_new_block/dec_new_block`、`enc_ready/dec_ready` 的声明。
2. 假设当前 `encdec=0`（解密），手动代入 `encdec_mux`：写出 `enc_next`、`dec_next`、`muxed_round_nr`、`muxed_new_block`、`muxed_ready` 各等于什么。
3. 沿着这些值追踪：`dec_next` → `dec_block.next`（解密车间启动）；`muxed_round_nr` → `keymem.round`（按解密轮号取轮密钥）；`muxed_new_block` → 顶层 `result`；`muxed_ready` → FSM 的 `CTRL_NEXT` 判断。
4. 对照 [rtl/tb_aes.v:439-448](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L439-L448) 的解密用例（`AES_DECIPHER`），确认它们用的密钥与对应加密用例相同——这正是因为轮密钥存储被两条通路共享。

**预期结果**：解密时 `enc_next=0`（加密车间静默）、`dec_next=next`、`muxed_round_nr=dec_round_nr`，且解密能复用加密阶段已经扩展好的同一份轮密钥。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `encdec_mux` 要把 `enc_next` 和 `dec_next` 在块开头都先置 0，而不是只赋值被选中的那一个？

**答案**：因为 `always @*` 块里被赋值的 reg 如果在某些路径下没有赋值，就会综合出锁存器。两个 `_next` 信号各只在一个分支里被赋成 `next`，若不在开头先钳零，未选中的那个就会保持上一次的值（锁存），可能导致两个车间同时收到脉冲。开头置 0 保证未选中者恒为 0。

**练习 2**：加密通路和解密通路共享 `round_key` 这一份存储，会不会有冲突？

**答案**：不会。`round_key` 是组合输出：`key_mem` 根据 `round` 输入（即 `muxed_round_nr`）即时给出对应轮的密钥。由于 `muxed_round_nr` 由 encdec_mux 选自当前激活通路的轮号，同一时刻只有一条通路在请求轮密钥，所以无冲突。代价是：加/解密不能并行（共用同一块存储和同一个 S-box 资源池）。

---

### 4.4 sbox_mux：共享 S-box 的时分复用

#### 4.4.1 概念说明

正向 S-box（`aes_sbox`）是 AES 硬件里最「重」的常量资源之一（256 字节 ROM × 4 路并行）。本工程为了省面积，**全工程只例化了 1 个** `aes_sbox`（[rtl/aes_core.v:138](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L138)）。

但有两个模块都需要正向 S-box：

- **密钥扩展**（`key_mem`）：每扩展一把新轮密钥，都要对 4 字节做 SubWord，即查正向 S-box。
- **加密**（`enc_block`）：SubBytes 步骤要查正向 S-box。

`sbox_mux` 的职责就是：在「密钥扩展阶段」把 S-box 接给 `key_mem`，在「加/解密阶段」把 S-box 接给 `enc_block`。选择开关是 `init_state`——而这个信号恰好由 4.2 的 FSM 在进入 `INIT` 时置 1、其它时候置 0。

> 这就回答了入门篇留下的一个问题：解密为什么不用这个共享 S-box？因为解密用的是**逆向** S-box，它由 `dec_block` 内部私有例化（`aes_inv_sbox`）。共享的正向 S-box 在解密阶段是空闲的。

#### 4.4.2 核心流程

```text
if (init_state == 1):    # 密钥扩展阶段（CTRL_INIT，或 IDLE→INIT 转移拍）
    muxed_sboxw = keymem_sboxw     # S-box 的输入取自 key_mem
else:                    # 加/解密阶段（CTRL_NEXT / IDLE 空闲）
    muxed_sboxw = enc_sboxw        # S-box 的输入取自 enc_block

# 不管哪种情况，S-box 输出 new_sboxw 都同时回灌给 key_mem 和 enc_block
# （只有当前激活的使用者会真正消费它）
```

数据通路（用伪连线表示）：

```text
key_mem.sboxw ──┐
                ├──► [sbox_mux] ──► muxed_sboxw ──► aes_sbox.sboxw
enc_block.sboxw ┘                                       │
                                                       ▼
                                             aes_sbox.new_sboxw
                                                       │
                              ┌────────────────────────┤
                              ▼                         ▼
                       key_mem.new_sboxw        enc_block.new_sboxw
```

#### 4.4.3 源码精读

[rtl/aes_core.v:184-194](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L184-L185)（完整范围 L184-L194）：

```verilog
always @*
  begin : sbox_mux
    if (init_state)
      muxed_sboxw = keymem_sboxw;     // 密钥扩展用 S-box
    else
      muxed_sboxw = enc_sboxw;        // 加密用 S-box（解密时 enc 不工作，S-box 空闲）
  end
```

逻辑极其简洁，但它的正确性依赖两个前提，都已在前面验证过：

1. `init_state` 由 FSM 在 `CTRL_INIT` 全程以及 `IDLE→INIT` 转移拍置 1（[rtl/aes_core.v:249,271](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L249-L249)），其余时刻置 0。
2. `new_sboxw`（S-box 输出）在例化处同时连到 `key_mem.new_sboxw` 和 `enc_block.new_sboxw`（[rtl/aes_core.v:97,134](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L97-L97)），所以「给谁用」完全由输入侧（`muxed_sboxw`）决定。

注意 `sbox_mux` **不依赖 `encdec`**：在加/解密阶段（`init_state=0`）S-box 一律接加密车间。解密车间根本不连这个 S-box，所以即使解密时 S-box 输入取自 `enc_sboxw`，也不会影响解密结果——因为加密车间此刻 `enc_next=0`、不产出有意义的数据，而 S-box 的输出也没人（解密侧）在用。

#### 4.4.4 代码实践

**目标**：确认「密钥扩展期间 S-box 归 key_mem，加/解密期间 S-box 归 enc」这一时分关系。

**步骤**（结合 4.2.4 的 trace testbench 副本，无需新建文件）：

1. 在 4.2.4 添加的 `$display` 里再追加上述 mux 的输入：

   ```verilog
   // 示例代码：在 testbench 副本的监控进程里追加
   $display("t=%0t state=%0d init_state=%0d muxed_sboxw=%h",
            $time, dut.core.aes_core_ctrl_reg, dut.core.init_state,
            dut.core.muxed_sboxw);
   ```

2. 重新仿真，重点看 `state=1`（INIT）的那些拍。
3. 对比 `state=1` 时 `muxed_sboxw` 是否等于 `keymem_sboxw`（密钥扩展的查表字），`state=2`（NEXT 且 encdec=1）时是否等于 `enc_sboxw`。

**需要观察的现象**：

- `state=1` 期间，`muxed_sboxw` 跟随 `keymem_sboxw` 变化（密钥扩展在逐字查表）。
- `state=2` 且 `encdec=1`（加密）期间，`muxed_sboxw` 跟随 `enc_sboxw` 变化。
- `state=2` 且 `encdec=0`（解密）期间，`muxed_sboxw` 仍跟随 `enc_sboxw`，但此时加密车间不工作，这些值无意义——正向 S-box 实际闲置。

**预期结果**：与上述描述一致。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `sbox_mux` 的选择信号从 `init_state` 改成 `encdec`（即用「加密还是解密」来决定 S-box 归属），会出什么问题？

**答案**：会破坏密钥扩展。密钥扩展（`CTRL_INIT` 阶段）发生在加/解密之前，与 `encdec` 无关。若用 `encdec` 选择，则解密配置（`encdec=0`）下做密钥扩展时，S-box 会被错误地接给加密车间而不是 key_mem，导致轮密钥算错。正确做法是用「当前是否在 init 阶段」即 `init_state` 来选——这也体现了 FSM 与 mux 的配合：FSM 产生 `init_state`，mux 消费它。

**练习 2**：为什么解密阶段（`init_state=0`、`encdec=0`）共享 S-box 是空闲的，却仍保留在电路里？

**答案**：因为同一份硬件要服务「加密 + 密钥扩展」两个场景，不能为解密单独移除。解密阶段 S-box 空闲是「时分复用」带来的必然空闲期，是「用时间换面积」的代价之一。若要消除这种空闲，需要给解密也接共享 S-box（但解密需要的是逆 S-box，方向不同），或采用更复杂的双口/流水线设计——这属于 u3-l4 讨论的设计取舍范畴。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面的「端到端信号走向图」绘制任务。这是本讲的收口练习，也是 u3-l1（一次完整加解密端到端追踪）的预演。

**任务**：针对下面两种情况，分别画出 `aes_core` 内部各子模块的信号走向示意图（手绘或用文字框图均可）。

**情况 A：`init` 阶段（密钥扩展，以 AES-128 为例）**

提示——按以下顺序标注：

1. `init` 脉冲进入 → FSM：`IDLE → INIT`，`init_state=1`，`ready` 拉低。
2. `sbox_mux`：因 `init_state=1`，`muxed_sboxw ← keymem_sboxw`，S-box 服务 key_mem。
3. `encdec_mux`：此刻 `next=0`，`enc_next=dec_next=0`，两个车间静默。
4. `key_mem`：在 `init` 驱动下逐轮扩展轮密钥，期间通过 `round_key` 输出（但此时无人取用结果），`keymem_sboxw` 不断送出待查字。
5. `key_ready=1` → FSM：`INIT → IDLE`，`ready` 拉高。

**情况 B：`next` 阶段（加密，`encdec=1`）**

提示：

1. `next` 脉冲进入 → FSM：`IDLE → NEXT`，`init_state=0`，`ready` 拉低。
2. `encdec_mux`：因 `encdec=1`，`enc_next=next`、`muxed_round_nr=enc_round_nr`、`muxed_new_block=enc_new_block`、`muxed_ready=enc_ready`；`dec_next=0`。
3. `sbox_mux`：因 `init_state=0`，`muxed_sboxw ← enc_sboxw`，S-box 服务加密车间。
4. `enc_block`：逐轮加密，每轮把 `enc_round_nr` 回报给 core → 经 `muxed_round_nr` → `key_mem.round`，取回对应 `round_key`，做 AddRoundKey。
5. `enc_ready=1` → 经 `muxed_ready` → FSM：`NEXT → IDLE`，`ready` 拉高、`result_valid` 置 1。
6. 顶层 `result ← muxed_new_block = enc_new_block`。

**完成标准**：

- 两张图都能体现「FSM 的状态 → 两个 mux 的选择 → 子模块的激活」这条因果链。
- 能在图上标出 `init_state` 和 `encdec` 这两个关键控制信号的作用点。
- 能指出情况 B 中若 `encdec=0`（解密），图上哪些箭头会改变（S-box 空闲、`dec_next` 接管、`dec_round_nr` 喂回 key_mem、`dec_new_block` 作为结果）。

**进阶（可选）**：把 4.2.4 的 trace testbench 跑起来，用仿真波形校验你画的图——看 `state`、`init_state`、`muxed_sboxw` 的实际取值是否与你画的走向一致。

## 6. 本讲小结

- `aes_core` 是顶层 wrapper 与算法子模块之间的**调度中枢**，自身不做加密运算，只例化 4 个子模块（enc/dec/key_mem/sbox）并用 3 个组合块把它们调度起来。
- **`aes_core_ctrl`** 是一个粗粒度三状态 FSM（`IDLE`/`INIT`/`NEXT`）：`init` 脉冲触发密钥扩展、`next` 脉冲触发一次加/解密；进入忙态时拉低 `ready`，完成时拉高 `ready`，且只有 `NEXT` 完成才置 `result_valid`。
- **`encdec_mux`** 按 `encdec` 在加密/解密两条通路间二选一，统一管理 `next` 分发、轮号回送（`muxed_round_nr`）、结果输出（`muxed_new_block`）、完成上报（`muxed_ready`），从而让两条通路**共享同一份 `key_mem` 轮密钥存储**。
- **`sbox_mux`** 按 `init_state` 在 `key_mem` 与 `enc_block` 之间分时复用**全工程唯一的正向 S-box**；密钥扩展归 key_mem，加/解密归 enc；解密不用这个共享 S-box（它有内部私有逆 S-box），故解密期间正向 S-box 空闲。
- 三块逻辑环环相扣：FSM 产生 `init_state` → 喂给 `sbox_mux`；`encdec_mux` 产生的 `muxed_ready` → 喂回 FSM 的 `CTRL_NEXT` 判断；`encdec_mux` 产生的 `muxed_round_nr` → 决定 `key_mem` 吐出哪把轮密钥。
- 本讲是「调度层」的全貌；每个子模块**内部**的细粒度轮循环 FSM（key_mem/encipher/decipher）留给 u2-l3 ~ u2-l7。

## 7. 下一步学习建议

本讲把 `aes_core` 的调度看透了，但每个子模块内部仍是黑盒。建议按数据流方向继续：

1. **u2-l2（S-box 与逆 S-box 的 ROM 实现）**：先看那台「唯一 S-box」内部长什么样——256 字节常量数组如何查表、如何 4 路并行处理 32 位字。这是最简单的子模块，适合紧接着读。
2. **u2-l3（密钥扩展与轮密钥存储）**：进入 `key_mem`，看 `CTRL_INIT` 阶段它如何把 256/128 位密钥展开成 `key_mem[0:14]`，以及 `rcon` 轮常数怎么递推。
3. **u2-l4 → u2-l5（加密数据通路 + 加密轮 FSM）**：进入 `enc_block`，先看四个纯组合变换函数（SubBytes/ShiftRows/MixColumns/AddRoundKey），再看它的细粒度 `encipher_ctrl` FSM 如何在 `CTRL_NEXT` 期间逐轮推进。
4. **u2-l6 → u2-l7（解密数据通路 + 解密轮 FSM）**：进入 `dec_block`，对照加密理解逆变换与「递减轮计数」的对称差异。

读完 u2-l2 ~ u2-l7 后，再回来看本讲的「综合实践」示意图，你会发现每个箭头都能落实到具体的寄存器与状态——届时就可以进入 u3-l1 的端到端追踪了。
