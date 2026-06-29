# 一次完整加解密的端到端追踪

## 1. 本讲目标

到此为止，你已经分别学过顶层总线接口（u1-l4）、核心调度 FSM（u2-l1）、密钥扩展（u2-l3）和加密轮 FSM（u2-l5）。但这些都是「局部放大镜」——单独看每个模块都能看懂，可一旦主机发出一条「加密这个块」的命令，信号究竟怎么**穿越**这一层又一层的模块、最终把密文送回主机的？本讲就是把这架「显微镜」拉到最远，看清**整条流水线在一次真实加解密里是如何协同运转的**。

本讲学完后，你应该能够：

1. 从主机写第一个寄存器开始，到主机读回 RESULT 为止，把每一个信号穿越的模块、每一步发生的「地点」说清楚。
2. 解释 **init（密钥扩展）** 与 **next（加/解密）** 两个阶段的时序关系：为什么它们必须分两次触发、各自握手完成。
3. 看懂**结果回写**这条最容易被忽略的链路：core 的 `result` 其实是 enc_block 的「实时工作寄存器」，顶层 `result_reg` 每拍都在跟随，`valid` 才是「现在读它有意义」的标志。
4. 用一张多列对照表，标注一个 NIST 明文块从 `BLOCK` 写入到 `RESULT` 读出的每一步发生在哪个模块。

本讲是专家篇的总纲，之后讲验证（u3-l2）、测试策略（u3-l3）、架构取舍（u3-l4）都建立在你对这条端到端数据流的掌握之上。

## 2. 前置知识

阅读本讲前，请确认你已掌握：

- **u1-l4 的地址映射与访问主线**：`0x00~0x33` 分五区段，CTRL 的 `init`(bit0)/`next`(bit1) 是单拍脉冲触发位，CONFIG 的 `encdec`(bit0，1=加密)/`keylen`(bit1，0=128 位/1=256 位) 是持久配置，STATUS 的 `ready`(bit0)/`valid`(bit1) 是状态回读。位定义**以 `aes.v` 为准**。
- **u1-l3 的 reg/_new/_we 两段式**：组合块算 `_new`/`_we`，时序块在时钟沿 `if (_we) _reg <= _new` 搬运。本讲直接套用，不再展开。
- **u2-l1 的 aes_core 调度中枢**：`aes_core_ctrl`（IDLE/INIT/NEXT 三状态）、`encdec_mux`（加/解密二选一）、`sbox_mux`（把唯一正向 S-box 分时给密钥扩展或加密）三块逻辑。
- **u2-l3 的密钥扩展**：`init` 脉冲触发 `key_mem` 一次性生成全部轮密钥存入 `key_mem[0..num_rounds]`，之后按外部轮号 `round` **组合（异步）读出**。
- **u2-l5 的加密轮 FSM**：一次 AES-128 加密耗 51 拍、AES-256 耗 71 拍；`round` 端口随 `round_ctr_reg` 输出，配合 `key_mem` 选 round key。

一个贯穿全讲的关键概念：**脉冲 vs 电平**。主机写 `ADDR_CTRL` 只是把 `init`/`next` 拉高很短一段时间（一两个时钟沿），之后由各 FSM 自主运行到完成。整个工程没有「busy 直到 done」的阻塞握手引脚，靠的是 `ready`/`valid` 状态位 + FSM 自驱。理解了这点，下面的时序才不会乱。

## 3. 本讲源码地图

本讲横跨 4 个文件，从顶层一路下到加密车间：

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| [rtl/aes.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v) | 顶层 wrapper（总线接口 + 寄存器落地） | `api` 命令译码、`reg_update` 寄存器搬运、core 例化、RESULT 读切片 |
| [rtl/aes_core.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v) | 调度中枢（自身不运算） | `aes_core_ctrl` 三状态 FSM、`encdec_mux`、`sbox_mux`、`result`/`result_valid` 输出 |
| [rtl/aes_key_mem.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v) | 密钥扩展 + 轮密钥仓库 | `key_mem_ctrl` 的 init 流程、`key_mem_read` 组合读 |
| [rtl/aes_encipher_block.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v) | 加密车间（含轮 FSM） | `encipher_ctrl` 的 next 流程、`round`/`new_block` 输出 |

本讲还会引用 [rtl/tb_aes.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v) 里的 `ecb_mode_single_block_test` 任务作为追踪线索——它就是「主机」的真实动作脚本。

模块间信号连接的全景图（文字版，**实线 = 数据/控制信号，箭头 = 流向**）：

```
        主机 (tb / 真实总线)
          │ cs, we, address, write_data        ▲ read_data
          ▼                                    │
   ┌─────────────────────── aes.v (顶层) ──────────────────────┐
   │ api 译码 → init_reg/next_reg/config/key_reg/block_reg     │
   │ reg_update 搬运 → core_init/core_next/core_key/.../core_block
   │                          │                  ▲ core_result/core_valid/core_ready
   │            assign result_reg <= core_result (每拍)         │
   │            read_data = result_reg[切片] (组合读)            │
   └─────────────────────────────┼──────────────────────────────┘
                          core 例化 │
        ┌─────────────────── aes_core.v (调度) ──────────────────┐
        │  init ────────────────────────► key_mem.init (直达)     │
        │  next ──encdec_mux──► enc_next / dec_next               │
        │  enc_round_nr ──encdec_mux──► muxed_round_nr ──► key_mem│
        │  enc_new_block ──encdec_mux──► muxed_new_block ──► result│
        │  sbox_mux: init_state 决定 sbox 给 key_mem 还是 enc    │
        └──────┬──────────────────────┬────────────────────┬─────┘
               │                      │                    │
        enc_block              dec_block               key_mem + sbox_inst
       (加密车间)              (解密车间)            (轮密钥 + 共享S盒)
```

这张图是本讲的「地图」，下面四节就是把这张图从左到右、从上到下走一遍。

## 4. 核心概念与源码讲解

本讲把端到端数据流拆成 4 个最小模块，按主机命令的推进顺序讲解：**4.1 主机接口到 core**（脉冲怎么产生）→ **4.2 init 阶段密钥扩展**（第一次握手）→ **4.3 next 阶段加解密**（第二次握手）→ **4.4 结果回写**（密文怎么回到主机）。

### 4.1 主机接口到 core：写操作如何变成 init/next 脉冲

#### 4.1.1 概念说明

主机面对的只有一组总线信号：`cs`、`we`、`address[7:0]`、`write_data[31:0]`、`read_data[31:0]`（端口定义见 [rtl/aes.v:9-22](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L9-L22)）。这组总线要驱动整个 AES 核，靠的是两件事：

1. **命令译码**（组合的 `api` 块）：根据 `address` 把 `write_data` 路由到正确的寄存器，或把正确的寄存器选到 `read_data`。
2. **寄存器落地**（时序的 `reg_update` 块）：在时钟沿真正写入，并把若干关键寄存器连到 core 的输入端口。

本模块要回答的核心问题：**主机写一次 `ADDR_CTRL`，是怎么变成 core 看到的一个 `init` 或 `next` 脉冲的？**

#### 4.1.2 核心流程

以「写 `ADDR_CTRL = 0x01`（触发 init）」为例，逐拍走：

```
主机: cs=1, we=1, address=0x08(CTRL), write_data=0x01   持续约 2 个时钟周期
        │
        ▼  (组合, 同一拍内生效)
api 块: address==ADDR_CTRL && cs && we
        → init_new = write_data[0] = 1
        → next_new = write_data[1] = 0
        │
        ▼  (posedge clk)
reg_update: init_reg <= init_new(=1);  next_reg <= next_new(=0)
        │
        ▼  (组合, 同一拍)
assign core_init = init_reg(=1)  →  送进 aes_core 的 init 端口
```

主机撤掉写操作（`cs=0`）后，下一拍 `init_new` 回到默认值 0，再下一拍 `init_reg` 跟着回 0。所以 `core_init` 是一个**持续时间很短（一两个时钟沿）的脉冲**——这正是下游 FSM 期望的「单拍触发」。`next` 完全同理：写 `ADDR_CTRL = 0x02` 会让 `write_data[1]=1`，产生 `next` 脉冲。

> 关键直觉：`init`/`next` 不是电平开关，而是「敲门一下」的脉冲。敲完之后主机就撒手，由各 FSM 自己跑到完成。这也是为什么工程需要 `ready` 状态位——主机要靠它知道「这次敲门的活儿干完了没」。

#### 4.1.3 源码精读

CTRL 命令译码（[rtl/aes.v:202-206](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L202-L206)）：

```verilog
if (address == ADDR_CTRL)
  begin
    init_new = write_data[CTRL_INIT_BIT];   // bit0
    next_new = write_data[CTRL_NEXT_BIT];   // bit1
  end
```

> 中文说明：只有当写操作命中 `ADDR_CTRL`(0x08) 时，才把 `write_data` 的 bit0/bit1 抽出来作为 `init`/`next` 的下一拍值。`api` 块开头已把两者默认置 0（[rtl/aes.v:191-192](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L191-L192)），所以非命中地址时脉冲自动归零。

寄存器落地与 core 输入连接（[rtl/aes.v:163-179](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L163-L179) 与 [rtl/aes.v:107-110](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L107-L110)）：

```verilog
// reg_update (时序块) —— 关键寄存器每拍搬运
init_reg   <= init_new;
next_reg   <= next_new;
if (config_we)  begin encdec_reg <= write_data[0]; keylen_reg <= write_data[1]; end
if (key_we)     key_reg[address[2:0]] <= write_data;
if (block_we)   block_reg[address[1:0]] <= write_data;

// 组合连线 —— 寄存器到 core 输入端口
assign core_init   = init_reg;
assign core_next   = next_reg;
assign core_encdec = encdec_reg;
assign core_keylen = keylen_reg;
assign core_block  = {block_reg[0], block_reg[1], block_reg[2], block_reg[3]};
```

> 中文说明：`init_reg`/`next_reg` 是「无 `_we`、每拍都搬」的脉冲寄存器（u1-l3 的第三种变体）；`config`/`key`/`block` 则是「带 `_we`、地址命中才搬」的写使能寄存器。注意 KEY/BLOCK 数组用地址低位作下标（`address[2:0]` 选 8 个 key 字、`address[1:0]` 选 4 个 block 字），这正是 u1-l4 地址映射的落地。

core 例化把这一组 `core_*` 信号接到 `aes_core`（[rtl/aes.v:116-131](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L116-L131)）：`init(core_init)`、`next(core_next)`、`key(core_key)`、`block(core_block)` 等一一对应。到此，主机的写操作就「穿」过了顶层，变成了 core 能看到的输入。

#### 4.1.4 代码实践

**目标**：在 testbench 里找到产生 `init`/`next` 脉冲的那两行，并核对它们写入的数值与 bit 位的对应。

**步骤**：

1. 打开 [rtl/tb_aes.v:303-333](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L303-L333)（`init_key` 任务）与 [rtl/tb_aes.v:341-377](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L341-L377)（`ecb_mode_single_block_test` 任务）。
2. 在 `init_key` 里找到 `write_word(ADDR_CTRL, 8'h01)`：`0x01` 的 bit0=1 → `init` 脉冲。
3. 在 `ecb_mode_single_block_test` 里找到 `write_word(ADDR_CTRL, 8'h02)`：`0x02` 的 bit1=1 → `next` 脉冲。
4. 对照 [rtl/aes.v:32-33](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L32-L33) 的 `CTRL_INIT_BIT=0`/`CTRL_NEXT_BIT=1`，确认位定义自洽。

**需要观察的现象 / 预期结果**：你会确认 testbench 写入的 `0x01`/`0x02` 与 `aes.v` 的 bit 定义完全对得上（注意：tb 里另一处 `CTRL_ENCDEC_BIT=2`/`CTRL_KEYLEN_BIT=3` 的 parameter 是 u1-l4 指出过的**死代码**，真正生效的是下面 4.3 节看到的内联表达式）。这是纯阅读题，**待本地验证**的是你在仿真波形里看到 `dut.init_reg` / `dut.next_reg` 各只亮了一两个时钟沿。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `init`/`next` 用「每拍都搬、默认为 0」的脉冲寄存器，而 KEY/BLOCK 用「写使能」寄存器？

**参考答案**：因为 `init`/`next` 表达的是「事件」（敲一下门），主机不写 CTRL 时它们必须自动归零，才能形成短脉冲；而 KEY/BLOCK 表达的是「状态」（记住密钥和明文），一旦写入就应该一直保持，直到下次覆盖，所以需要写使能来「记住」。

**练习 2**：主机连续写两次 `ADDR_CTRL = 0x01`，会不会触发两次 init？

**参考答案**：不会。`init` 脉冲确实会持续两拍，但 `aes_core` 的 FSM 在第一个 `init` 沿就离开了 `CTRL_IDLE` 进入 `CTRL_INIT`，之后即使 `init` 仍为 1 也不会再次响应（FSM 只在 IDLE 检查 init）。脉冲必须等 FSM 回到 IDLE 才能再次触发——这正是「脉冲 + FSM 自驱」模式的安全保证。

---

### 4.2 init 阶段：密钥扩展的第一次握手

#### 4.2.1 概念说明**

AES 加解密每一轮都要用一把「轮密钥」做 AddRoundKey，AES-128 要 11 把、AES-256 要 15 把。本工程把生成这些轮密钥的工作**单独放在 init 阶段一次性做完**，存进 `key_mem[0..num_rounds]`，之后 next 阶段只管按轮号取用。所以一次加解密必须分成**两次主机触发**：先 `init`（造好所有轮密钥），再 `next`（用它们做加/解密）。

init 阶段涉及两个 FSM 的握手：

- **`aes_core` 的 `aes_core_ctrl`**：从 `IDLE` 进入 `INIT`，等 `key_mem` 说「好了」（`key_ready`），再回 `IDLE`。在此期间它把唯一的正向 S-box 通过 `sbox_mux` **让给** key_mem（密钥扩展需要 SubWord）。
- **`aes_key_mem` 的 `key_mem_ctrl`**：从 `IDLE` 进入 `INIT`→`GENERATE`（逐把生成）→`DONE`，完成后拉高 `ready`。

一个**极易忽略的架构细节**：`init` 信号有**两个消费者**。core FSM 读它（用来切状态、驱动 `init_state`），而 key_mem **直接**读它（用来启动扩展）。两者被同一个脉冲同时触发，互不依赖。

#### 4.2.2 核心流程

```
主机: write KEY0..KEY7 → key_reg[0..7]
主机: write ADDR_CONFIG (keylen) → keylen_reg
主机: write ADDR_CTRL=0x01 → init 脉冲 (core_init=1)
        │
        ├──► aes_core.aes_core_ctrl:  IDLE ──(init)──► INIT
        │       同时 init_state=1  → sbox_mux 把 S-box 让给 key_mem
        │       ready_new=0         → 主机看到 STATUS.ready=0 (忙)
        │
        └──► aes_key_mem.key_mem_ctrl: IDLE ──(init)──► INIT ──► GENERATE
                GENERATE 每拍: round_ctr++, round_key_update=1,
                              用共享 S-box 对 w7 做 SubWord, 算出新轮密钥,
                              写 key_mem[round_ctr]
                写到 round_ctr==num_rounds 那拍: → DONE
                DONE: ready=1 (key_ready)
        │
        ▼  key_ready 回到 aes_core
aes_core.aes_core_ctrl: INIT ──(key_ready)──► IDLE,  ready_new=1
        │
        ▼  core_ready 回到 aes.v
主机: 轮询 STATUS.ready, 看到 ready=1 → init 阶段完成
```

握手的核心是两个 ready 信号的「接力」：key_mem 的 `ready` 喂给 core 的 `CTRL_INIT` 判断，core 的 `ready` 喂给主机的 STATUS。一级完成才解锁下一级。

#### 4.2.3 源码精读

core FSM 的 init 分支（[rtl/aes_core.v:245-280](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L245-L280)）：

```verilog
CTRL_IDLE:
  if (init) begin
    init_state = 1'b1;          // ← 让 sbox_mux 把 S-box 交给 key_mem
    ready_new = 1'b0; ready_we = 1'b1;       // 拉低 ready (忙)
    result_valid_new = 1'b0; result_valid_we = 1'b1;  // 清 valid
    aes_core_ctrl_new = CTRL_INIT; aes_core_ctrl_we = 1'b1;
  end
  ...
CTRL_INIT:
  begin
    init_state = 1'b1;          // 整个 init 阶段都让出 S-box
    if (key_ready) begin        // ← 等 key_mem 说"好了"
      ready_new = 1'b1; ready_we = 1'b1;     // 拉高 ready (空闲)
      aes_core_ctrl_new = CTRL_IDLE; aes_core_ctrl_we = 1'b1;
    end
  end
```

> 中文说明：`init` 一来，core 立刻进入 `INIT` 并把 `init_state` 拉高——这正是 `sbox_mux`（[rtl/aes_core.v:184-194](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L184-L194)）把共享 S-box 路由给 key_mem 的开关。core 在 `INIT` 里干等 `key_ready`，等到了就回 `IDLE` 并恢复 `ready`。

key_mem 直连 init（注意是裸 `init`，不经 encdec_mux）——见 [rtl/aes_core.v:121-135](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L121-L135) 的例化：`.init(init)`。所以无论 `encdec` 是什么，init 都能直达 key_mem。

key_mem 的扩展 FSM（[rtl/aes_key_mem.v:354-390](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L354-L390)）：

```verilog
CTRL_IDLE:    if (init) → CTRL_INIT
CTRL_INIT:    round_ctr_rst=1 → CTRL_GENERATE      // 清计数器
CTRL_GENERATE:
  round_ctr_inc=1; round_key_update=1;             // 每拍生成一把并写入
  if (round_ctr_reg == num_rounds) → CTRL_DONE      // 写满 N+1 把就完成
CTRL_DONE:    ready_new=1 → CTRL_IDLE               // key_ready 拉高
```

> 中文说明：`GENERATE` 每拍同时做三件事——计数器 +1、拉高 `round_key_update`（让 `round_key_gen` 组合块算出新轮密钥并写入 `key_mem[round_ctr_reg]`，见 [rtl/aes_key_mem.v:128-129](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L128-L129)）、判断是否写满。`num_rounds` 由 `keylen` 决定（AES-128=10、AES-256=14，[rtl/aes_key_mem.v:349-352](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L349-L352)）。因为 S-box 是组合读，`round_key_gen` 同一拍内就能拿到 SubWord 结果并写回，所以每拍生成一把。

因此 init 阶段的 GENERATE 循环会写满 `num_rounds+1` 把轮密钥（AES-128 写 `key_mem[0..10]` 共 11 把，AES-256 写 `[0..14]` 共 15 把）。整个 init 阶段从 init 脉冲到 `key_ready` 拉高，AES-128 大约十几个时钟周期（1 拍 IDLE→INIT + 1 拍 INIT + 11 拍 GENERATE + 1 拍 DONE），AES-256 约 18 拍。具体拍数**待本地验证**（取决于 init 脉冲被采样的确切时钟沿），本讲关注的是握手结构而非精确拍数。

#### 4.2.4 代码实践

**目标**：确认「init 期间 S-box 归 key_mem 所有」这条资源共享链。

**步骤**：

1. 在 [rtl/aes_core.v:184-194](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L184-L194) 看 `sbox_mux`：`if (init_state) muxed_sboxw = keymem_sboxw; else muxed_sboxw = enc_sboxw;`。
2. 在 [rtl/aes_core.v:245-280](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L245-L280) 确认 `init_state` 在 `CTRL_IDLE`(init 来时) 和整个 `CTRL_INIT` 期间都是 1。
3. 自问：如果 init 期间 enc_block 也想用 S-box 会怎样？**答案**：不会发生——init 阶段主机还没发 `next`，enc_block 停在 `CTRL_IDLE`，根本不产生 `enc_sboxw` 请求；即便产生，`sbox_mux` 也会把它挡掉（选了 key_mem 那路）。

**需要观察的现象 / 预期结果**：你能说清「为什么 init 和 next 必须分两次触发」——因为两者都要用同一个 S-box，分时复用就要求它们在时间上不重叠。这是一条**待本地验证**的理解性结论（仿真里可看到 init 期间 `muxed_sboxw = keymem_sboxw`）。

#### 4.2.5 小练习与答案

**练习 1**：init 阶段 core 的 `result_valid` 是什么值？为什么？

**参考答案**：是 0。core 进入 `INIT` 时把 `result_valid_new` 清成 0（[rtl/aes_core.v:252-253](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L252-L253)），且 `CTRL_INIT` 完成分支不再碰它。因为密钥扩展不产生「结果」，`valid` 只在 next 阶段完成时才置 1。主机若在 init 后误读 RESULT，读到的 valid=0，应据此判断「结果无效」。

**练习 2**：为什么 key_mem 的 `init` 直连裸信号，而不像 `next` 那样经过 `encdec_mux`？

**参考答案**：因为密钥扩展与加/解密方向无关——同一组轮密钥既用于加密也用于解密。所以 init 不需要区分 encdec，直接启动即可；而 next 必须先由 `encdec_mux` 选好走加密车间还是解密车间，再把脉冲分发过去。

---

### 4.3 next 阶段：加/解密的第二次握手

#### 4.3.1 概念说明

init 完成后，`key_mem` 里已经备齐所有轮密钥，core 也回到 `IDLE`、`ready=1`。此时主机就可以发起第二次触发——`next`——让加密（或解密）车间真正运转。next 阶段的数据流比 init 复杂，因为它要同时调度**三个共享资源**：

1. **共享 S-box**：现在归加密车间（`init_state=0`，`sbox_mux` 选 `enc_sboxw`）。
2. **共享轮密钥仓库 `key_mem`**：加密车间每轮通过 `round` 端口告诉它「要第几把」，它组合回送 `round_key`。
3. **加/解密车间二选一**：`encdec_mux` 按 `encdec` 把 `next` 脉冲、轮号、结果、完成信号统一分发到 enc_block 或 dec_block。

本模块要回答：**主机写一次明文 + 一次 `ADDR_CTRL=0x02`，加密车间是怎么跑完 51 拍并把密文交出来的？**

#### 4.3.2 核心流程

```
主机: write BLOCK0..BLOCK3 → block_reg → core_block (明文就位)
主机: write ADDR_CONFIG (encdec=1 加密, keylen) → encdec_reg/keylen_reg
主机: write ADDR_CTRL=0x02 → next 脉冲 (core_next=1)
        │
        ▼  encdec_mux (encdec=1 时)
enc_next = next;  muxed_round_nr = enc_round_nr;
muxed_new_block = enc_new_block;  muxed_ready = enc_ready;
        │
        ├──► aes_core.aes_core_ctrl: IDLE ──(next)──► NEXT
        │       init_state=0 → sbox_mux 把 S-box 让给 enc_block
        │       ready_new=0 → 主机看到 STATUS.ready=0 (忙)
        │
        └──► enc_block.encipher_ctrl: IDLE ──(next)──► INIT ──► SBOX×4 ──► MAIN ──(循环 10 轮)
                每轮: round=round_ctr_reg → 经 encdec_mux → key_mem.round
                      key_mem 组合回送 round_key → enc_block 做 AddRoundKey
                      SBOX 拍: enc_sboxw → sbox_mux → sbox_inst → new_sboxw (4 拍逐字替换)
                最终轮 (round_ctr==num_rounds): FINAL_UPDATE, enc_ready=1
        │
        ▼  enc_ready → encdec_mux → muxed_ready 回到 aes_core
aes_core.aes_core_ctrl: NEXT ──(muxed_ready)──► IDLE
        ready_new=1;  result_valid_new=1   ← 注意: 只有 next 完成才置 valid
        │
        ▼  core_ready/core_valid 回到 aes.v
主机: 轮询 STATUS, 看到 valid=1 → 密文就绪, 去 4.4 读 RESULT
```

注意轮号的路由：`enc_block` 输出 `round = round_ctr_reg`（[rtl/aes_encipher_block.v:172](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L172)）→ `enc_round_nr` → `encdec_mux` 选成 `muxed_round_nr` → `key_mem.round` → `key_mem_read` 组合读出 `key_mem[round]`（[rtl/aes_key_mem.v:148-151](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L148-L151)）→ `round_key` 回送 enc_block。这是一条**纯组合的闭环**，轮号一变，当拍 round_key 就跟着变。

#### 4.3.3 源码精读

core FSM 的 next 分支（[rtl/aes_core.v:257-295](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L257-L295)）：

```verilog
CTRL_IDLE:
  else if (next) begin
    init_state = 1'b0;          // ← S-box 现在归 enc_block
    ready_new = 1'b0; ready_we = 1'b1;
    result_valid_new = 1'b0; result_valid_we = 1'b1;
    aes_core_ctrl_new = CTRL_NEXT; aes_core_ctrl_we = 1'b1;
  end
CTRL_NEXT:
  begin
    init_state = 1'b0;
    if (muxed_ready) begin      // ← 等加密车间说"做完了"
      ready_new = 1'b1; ready_we = 1'b1;
      result_valid_new = 1'b1; result_valid_we = 1'b1;   // ← 置 valid!
      aes_core_ctrl_new = CTRL_IDLE; aes_core_ctrl_we = 1'b1;
    end
  end
```

> 中文说明：与 init 分支镜像对称，唯一区别是完成时**置 `result_valid=1`**——因为 next 阶段才真正产生密文结果。`muxed_ready` 来自 `encdec_mux`，等于当前选中车间的 `ready`。

`encdec_mux` 的分发逻辑（[rtl/aes_core.v:203-224](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L203-L224)）：

```verilog
if (encdec) begin               // 加密
  enc_next = next;  muxed_round_nr = enc_round_nr;
  muxed_new_block = enc_new_block;  muxed_ready = enc_ready;
end else begin                  // 解密
  dec_next = next;  muxed_round_nr = dec_round_nr;
  muxed_new_block = dec_new_block;  muxed_ready = dec_ready;
end
```

> 中文说明：`encdec` 一位决定整条数据通路走加密还是解密。注意 `next` 只发给被选中的那个车间（另一个的 `*_next` 保持 0），所以闲置车间不会误启动。轮号、结果、完成信号也都从选中车间回传。

加密车间的 next 启动与轮循环（[rtl/aes_encipher_block.v:392-443](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L392-L443)）：`CTRL_IDLE` 检测到 `next` 就清 `round_ctr`、拉低 `ready`、进 `CTRL_INIT`；之后按 u2-l5 讲过的 IDLE→INIT→SBOX×4→MAIN 循环跑 10 轮，最终轮 `ready↑` 回 IDLE。具体每一拍的状态轨迹已在 u2-l5 第 5 节给出（AES-128 共 51 拍），本讲不再重复，只强调它在这里扮演的角色：**它是 next 阶段最耗时的部分，也是 `muxed_ready` 迟迟不拉高的原因**。

> 端到端拍数：从 `next` 脉冲到主机看到 `valid=1`，AES-128 约 52 拍（enc_block 自身 51 拍 + core 的 `CTRL_NEXT` 采样 `muxed_ready` 并回 IDLE 再 1 拍）。该数字**待本地验证**（精确值取决于 next 脉冲被采样的时钟沿）；u2-l5 给出的是 enc_block **模块自身**的 51 拍，本讲再加 core 的握手开销。

#### 4.3.4 代码实践

**目标**：在 testbench 里看清楚 next 阶段主机的三步动作（写明文 → 写配置 → 触发 next），并核对配置字节。

**步骤**：

1. 打开 [rtl/tb_aes.v:341-377](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L341-L377)（`ecb_mode_single_block_test`）。
2. 找到 `write_block(block)`（内部调用 4 次 `write_word` 写 BLOCK0..BLOCK3，见 [rtl/tb_aes.v:243-250](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L243-L250)）。
3. 找到 `write_word(ADDR_CONFIG, (8'h00 + (key_length << 1) + encdec))`：注意这个内联表达式——`key_length<<1` 正好落在 bit1（keylen），`encdec` 落在 bit0，与 `aes.v` 的位定义一致。
4. 找到 `write_word(ADDR_CTRL, 8'h02)`：触发 next。
5. 代入 AES-128 加密（`key_length=0, encdec=1`）：配置字节 = `0x00 + 0 + 1 = 0x01`（keylen=0、encdec=1）。对照 `aes.v` 的 `CTRL_ENCDEC_BIT=0`/`CTRL_KEYLEN_BIT=1` 核对。

**需要观察的现象 / 预期结果**：你会发现 testbench 用内联表达式 `key_length<<1 + encdec` 正确地编码了配置字节，而它自己声明的 `CTRL_ENCDEC_BIT=2`/`CTRL_KEYLEN_BIT=3` 死 parameter **根本没被用到**——这是 u1-l4 已指出的现象在 next 阶段的再次印证。结论：**配置位以 `aes.v` 为准，不要信 testbench 的 parameter。** 这是纯阅读题。

#### 4.3.5 小练习与答案

**练习 1**：next 阶段，解密车间（dec_block）的内部逆 S-box 会和工作吗？正向共享 S-box 呢？

**参考答案**：若 `encdec=0`（解密），`encdec_mux` 把 `next` 发给 dec_block，dec_block 内部的私有 `inv_sbox_inst` 会工作；而正向共享 `sbox_inst` 此时**空闲**——因为解密不用正向 S-box，且 `init_state=0` 时 `sbox_mux` 虽然选了 `enc_sboxw`，但 dec_block 根本不产生该信号。这就是 u2-l1/u2-l2 讲的「解密不占共享 S-box」。

**练习 2**：为什么轮号 `muxed_round_nr` 要经 `encdec_mux` 再送给 key_mem，而不是 enc_block 直接连 key_mem？

**参考答案**：因为加密用**递增**轮号（round_ctr 从 0 加到 num_rounds），解密用**递减**轮号（从 num_rounds 减到 0，见 u2-l7），两者都共用同一份 `key_mem`。`encdec_mux` 把选中车间的轮号统一送到 key_mem 的 `round` 端口，这样 key_mem 只需一个组合读端口就能同时服务加/解密，无需知道当前是哪种模式。

---

### 4.4 结果回写：密文如何回到主机

#### 4.4.1 概念说明

加密跑完后，密文怎么从 enc_block 的内部寄存器一路回到主机的 `read_data`？这条链路最容易被忽略，却藏着一个**反直觉的设计**：

- core 的 `result` 输出**不是一个独立的「结果寄存器」**，而是直接等于 enc_block 的「实时工作寄存器」`new_block`（即 4 个字寄存器 `block_w*_reg` 的拼接）。也就是说，**在整个 51 拍加密过程中，`core_result` 每拍都在变**，里面是中间状态，只有最后一拍才是真正的密文。
- 顶层 `aes.v` 的 `result_reg` **每拍无条件**跟随 `core_result`（没有写使能）。所以 `result_reg` 也是一直在变，只有等 `valid=1` 之后读它才有意义。

`valid`（以及 `ready`）就是这套设计的「安全阀」：它告诉主机「现在 result_reg 里的东西是最终密文，可以读了」。主机必须先确认 `valid=1`，再去读 RESULT——这正是 testbench 里 `read_result` 之前先 `#(100*CLK_PERIOD)` 等待（生产代码里应当轮询 `STATUS.valid`）的原因。

#### 4.4.2 核心流程

```
enc_block 完成最终轮: block_w*_reg = 密文, enc_new_block = 密文, enc_ready=1
        │
        ▼  encdec_mux
muxed_new_block = 密文
        │
        ▼  aes_core 组合输出
assign result = muxed_new_block   → core_result = 密文 (这一拍才稳定成密文)
assign result_valid = result_valid_reg   (core FSM 在 NEXT 完成时已置 1)
        │
        ▼  aes.v reg_update (每拍无条件搬运)
result_reg <= core_result   → result_reg 锁住密文
valid_reg  <= core_valid    → valid_reg=1
        │
        ▼  主机读 RESULT0..RESULT3 (组合读)
api 块: tmp_read_data = result_reg[(3-(address-ADDR_RESULT0))*32 +: 32]
        │
        ▼
read_data = tmp_read_data → 主机拿到 32 位切片
读 4 次拼成 128 位密文
```

#### 4.4.3 源码精读

core 的 result 直连（[rtl/aes_core.v:144-146](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L144-L146)）：

```verilog
assign ready        = ready_reg;
assign result       = muxed_new_block;     // ← enc/dec 车间的实时工作寄存器
assign result_valid = result_valid_reg;
```

> 中文说明：`result` 是纯组合输出，等于 `encdec_mux` 选中的 `muxed_new_block`，而后者等于 enc_block 的 `new_block = {block_w0_reg, ..., block_w3_reg}`（[rtl/aes_encipher_block.v:174](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L174)）。所以 core 不缓存结果，直接把车间的「当前状态」暴露出去。

顶层每拍无条件锁存（[rtl/aes.v:163-165](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L163-L165)）：

```verilog
ready_reg  <= core_ready;
valid_reg  <= core_valid;
result_reg <= core_result;      // ← 注意: 没有 if 守卫, 每拍都搬
```

> 中文说明：这三个寄存器是 core 输出的「一拍延迟镜像」。因为没有写使能，加密中途 `result_reg` 会跟着 `core_result` 一起乱跳（中间状态），直到加密完成那一拍才稳定成密文。`valid_reg` 同步变 1，标志「现在 result_reg 可信了」。

RESULT 读切片（[rtl/aes.v:232-233](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L232-L233)）：

```verilog
if ((address >= ADDR_RESULT0) && (address <= ADDR_RESULT3))
  tmp_read_data = result_reg[(3 - (address - ADDR_RESULT0)) * 32 +: 32];
```

> 中文说明：读 `ADDR_RESULT0`(0x30) 取 `result_reg[127:096]`（最高字），读 `ADDR_RESULT3`(0x33) 取 `result_reg[31:000]`（最低字）。这是**组合读**——当拍出值。字序与写入 `BLOCK0..BLOCK3` 的顺序一致（都是大端在前），所以加解密的字节序自洽。

testbench 的读取与比对（[rtl/tb_aes.v:283-294](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L283-L294) 与 [rtl/tb_aes.v:362-375](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L362-L375)）：`read_result` 连读 4 次拼成 `result_data`，再与期望密文 `expected` 逐位比较，不等则 `error_ctr++`。

#### 4.4.4 代码实践

**目标**：验证「字节序自洽」——写入 BLOCK 的字序与读出 RESULT 的字序一致。

**步骤**：

1. 看 [rtl/tb_aes.v:243-250](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L243-L250)：`write_block` 把 `block[127:096]` 写进 BLOCK0、`block[31:000]` 写进 BLOCK3。
2. 看 [rtl/aes.v:105-106](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L105-L106)：`core_block = {block_reg[0], block_reg[1], block_reg[2], block_reg[3]}`，即 BLOCK0 是最高字。
3. 看 [rtl/tb_aes.v:285-292](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L285-L292)：`read_result` 把 RESULT0 读到 `result_data[127:096]`、RESULT3 读到 `result_data[31:000]`。
4. 结论：明文 `block[127:096]` → BLOCK0 → 处理后 → RESULT0 → `result_data[127:096]`，最高字始终在最前面，字序闭环。

**需要观察的现象 / 预期结果**：你能解释为什么 testbench 不需要做任何字节翻转就能直接比对 `result_data == expected`——因为整条通路（写、core 内部、读）都用同一种「字 0 = 最高位」的大端约定。这是纯阅读题。

#### 4.4.5 小练习与答案

**练习 1**：如果主机在加密**中途**（valid 还是 0）就去读 RESULT，会读到什么？

**参考答案**：会读到 enc_block 当前那一刻的中间工作寄存器内容（某轮的中间状态），不是密文，且每次读可能不同。这就是为什么必须等 `valid=1`——`result_reg` 是「实时跟随」而非「一次性快照」，`valid` 是唯一可靠的「结果有效」标志。

**练习 2**：`result_reg`、`valid_reg`、`ready_reg` 为什么都用「每拍无条件搬运」而不是写使能？

**参考答案**：因为它们是 core 状态的「镜像」——core 说啥它们就跟着说啥，一拍延迟。用写使能反而要多一组控制逻辑去决定何时更新，而这里恰恰需要「永远紧跟」。这种镜像寄存器是顶层 wrapper 的常见手法，把核心模块的输出「锁存一拍」以改善时序（避免组合逻辑直接长距离连到输出端口）。

---

## 5. 综合实践：追踪一个 NIST 明文块的端到端全过程

**任务**：以 `tb_aes` 的 `ecb_mode_single_block_test` 为脚本，用一张多列对照表追踪 **AES-128 加密**一个 NIST 明文块从写入 `BLOCK` 到读出 `RESULT` 的全过程，标注每一步发生在哪个模块。这是本讲四个最小模块的汇合点——你需要同时用到 4.1（脉冲产生）、4.2（init 握手）、4.3（next 调度）、4.4（结果回写）。

### 5.1 测试向量（来自源码，真实存在）

取 [rtl/tb_aes.v:405-416](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L405-L416) 的 NIST AES-128 ECB 第一组（即 `aes_test` 里 TC 01，[rtl/tb_aes.v:426-427](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L426-L427)）：

| 项 | 值 |
|----|----|
| 密钥（128 位，高 128 位补 0） | `0x2b7e151628aed2a6abf7158809cf4f3c` |
| 明文 | `0x6bc1bee22e409f96e93d7e117393172a` |
| 期望密文 | `0x3ad77bb40d7a3660a89ecaf32466ef97` |
| 模式 | AES-128（keylen=0）、加密（encdec=1） |

### 5.2 操作步骤

1. 把端到端过程分成三大阶段：**A. init（init_key）**、**B. next（ecb_mode_single_block_test 后半）**、**C. 读结果**。
2. 对每一阶段，列出「主机动作 → aes.v 响应 → core/子模块响应 → 关键信号变化 → 大约拍数」。
3. 在每个箭头上标注「这一步发生在哪个模块」（aes.v / aes_core / aes_key_mem / aes_encipher_block）。
4. 最后确认：读回的 `result_data` 是否等于期望密文 `0x3ad77bb4...`。

### 5.3 参考追踪表（worked example）

> 拍数为「数量级」估计，标注**待本地验证**；重点是模块归属与信号流向，不是精确到每一拍。tb 里每条 `write_word` 本身占用约 2 个时钟周期。

#### 阶段 A：init（密钥扩展），脚本 = `init_key`

| 步 | 主机动作（tb） | aes.v 响应 | core / 子模块响应 | 关键信号变化 | 模块归属 |
|----|----|----|----|----|----|
| A1 | `write_word(ADDR_KEY0..7, key 切片)` ×8 | `key_we=1`，`key_reg[addr] <= 切片` | （core 未参与） | `core_key` 逐字就位 | aes.v |
| A2 | `write_word(ADDR_CONFIG, 8'h00)`（keylen=0） | `config_we=1`，`keylen_reg<=0, encdec_reg<=0` | （core 未参与） | `core_keylen=0` | aes.v |
| A3 | `write_word(ADDR_CTRL, 8'h01)` | `init_new=1`→`init_reg<=1` | `core_init=1` 同时抵达 core FSM 与 key_mem | 脉冲产生 | aes.v → aes_core/ key_mem |
| A4 | （tb `#100`） | — | core FSM `IDLE→INIT`，`init_state=1`；key_mem `IDLE→INIT→GENERATE` 逐把生成，S-box 让给 key_mem | `STATUS.ready=0`（忙）；`key_mem[0..10]` 写满 | aes_core + aes_key_mem |
| A5 | （tb 继续等待） | — | key_mem `GENERATE→DONE`，`key_ready=1`；core FSM `INIT→IDLE`，`ready_new=1` | `STATUS.ready=1`（空闲） | aes_key_mem → aes_core → aes.v |

#### 阶段 B：next（加密），脚本 = `ecb_mode_single_block_test` 后半

| 步 | 主机动作（tb） | aes.v 响应 | core / 子模块响应 | 关键信号变化 | 模块归属 |
|----|----|----|----|----|----|
| B1 | `write_block(plaintext)` = 4×`write_word(BLOCK0..3)` | `block_we=1`，`block_reg[addr] <= 切片` | （core 未参与） | `core_block = 明文` 就位 | aes.v |
| B2 | `write_word(ADDR_CONFIG, 0x01)`（keylen=0,encdec=1） | `encdec_reg<=1, keylen_reg<=0` | （core 未参与） | `core_encdec=1`（加密） | aes.v |
| B3 | `write_word(ADDR_CTRL, 8'h02)` | `next_new=1`→`next_reg<=1` | `core_next=1` 经 `encdec_mux`→`enc_next=1` | next 脉冲产生 | aes.v → aes_core |
| B4 | （tb `#100`） | — | core FSM `IDLE→NEXT`，`init_state=0`（S-box 让给 enc）；enc_block FSM 跑 51 拍：每轮 `round=round_ctr`→`key_mem` 组合回送 round_key，4 拍 SubBytes 经共享 S-box，1 拍 SR+MC+ARK | `STATUS.ready=0`；`core_result` 实时跳变（中间态） | aes_core + aes_encipher_block + aes_key_mem |
| B5 | （tb 继续等待） | — | enc_block 最终轮 `enc_ready=1`→`muxed_ready=1`；core FSM `NEXT→IDLE`，`result_valid_new=1` | `STATUS.valid=1`；`core_result` 稳定成密文 | aes_encipher_block → aes_core |

#### 阶段 C：读结果，脚本 = `read_result`

| 步 | 主机动作（tb） | aes.v 响应 | core / 子模块响应 | 关键信号变化 | 模块归属 |
|----|----|----|----|----|----|
| C1 | （tb 已等 `#100`，生产代码应轮询 `STATUS.valid`） | `valid_reg<=core_valid=1`（每拍镜像） | core 维持 `result_valid=1` | `STATUS.valid=1` | aes.v（镜像 core） |
| C2 | `read_word(RESULT0..3)` ×4 | 组合读 `result_reg[切片]`→`read_data` | （core 已空闲） | 主机逐字收密文 | aes.v |
| C3 | `result_data == expected?` | — | — | 相等→`*** TC 1 successful`；不等→`error_ctr++` | tb（自检） |

### 5.4 预期结果

读回的 `result_data` 应为 `0x3ad77bb40d7a3660a89ecaf32466ef97`，与期望密文逐位相等，testbench 打印 `*** TC 01 successful.`。16 组 NIST 用例全部跑完后，最终打印 `*** All 16 test cases completed successfully`（`tc_ctr` 由 `ecb_mode_single_block_test` 每调用一次 +1，`aes_test` 共调用 16 次，见 [rtl/tb_aes.v:426-478](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L426-L478)；该「16」可由源码直接数出）。

**需要观察的现象 / 预期结果**：你能指着追踪表的任一行，说出这一步信号落在 `aes.v`、`aes_core`、`aes_key_mem`、`aes_encipher_block` 中的哪一个，并解释为什么 init 与 next 必须分两次触发（S-box 分时复用）、为什么必须等 `valid=1` 才能读 RESULT（result_reg 是实时镜像）。精确拍数与波形**待本地验证**：按 u1-l5 用 iverilog/ModelSim 编译 `rtl/*.v` 跑 `tb_aes`，把 `dut.init_reg`、`dut.next_reg`、`dut.aes_core.aes_core_ctrl_reg`、`dut.aes_core.keymem.key_mem_ctrl_reg`、`dut.aes_core.enc_block.enc_ctrl_reg`、`dut.valid_reg` 加入波形逐段核对。

## 6. 本讲小结

- 一次加解密由**两次主机触发**组成：先 `init`（密钥扩展），再 `next`（加/解密），因为两者都要用同一个共享 S-box，必须分时复用。`init`/`next` 都是短脉冲（一两个时钟沿），敲完后由各 FSM 自驱到完成。
- 主机写操作经 `aes.v` 的 `api` 译码 + `reg_update` 落地，变成 core 输入：`init`/`next` 是脉冲寄存器，`config`/`key`/`block` 是写使能寄存器（KEY/BLOCK 用地址低位作下标）。
- **init 阶段**：core FSM `IDLE→INIT` 并把 S-box 让给 key_mem；key_mem 的 `key_mem_ctrl` 跑 `IDLE→INIT→GENERATE→DONE` 写满 N+1 把轮密钥；`key_ready` 回传后 core 回 `IDLE`、`ready=1`。`init` 信号同时直达 key_mem（不经 encdec_mux），因为密钥扩展与方向无关。
- **next 阶段**：core FSM `IDLE→NEXT`，`encdec_mux` 把 `next`/轮号/结果/完成信号统一分发到选中车间；加密车间经 `round` 端口组合向 key_mem 取 round_key，跑完 51 拍（AES-128）；`muxed_ready` 回传后 core 回 `IDLE` 并**置 `result_valid=1`**（只有 next 完成才置 valid）。
- **结果回写**：core 的 `result` 直接等于车间实时工作寄存器（非独立结果寄存器），`aes.v` 的 `result_reg` 每拍无条件镜像，全程在变；只有 `valid=1` 后读 RESULT 才有意义。RESULT0..3 用大端字序组合读出，与 BLOCK 写入字序自洽。
- 整条端到端链路严格沿用 u1-l3 的两段式风格，握手靠 `ready`/`valid` 状态位接力：key_mem.ready → core.CTRL_INIT → core.ready → 主机 STATUS；车间.ready → core.CTRL_NEXT → core.result_valid → 主机 STATUS。

## 7. 下一步学习建议

- **去读验证机制**：下一篇 u3-l2 会专门讲 `tb_aes.v` 的自检式测试方法与 NIST 测试向量。本讲你已经把 `ecb_mode_single_block_test` 当成「主机脚本」用了一遍；u3-l2 会带你细看 `write_word`/`read_word`/`init_key` 这些 task 的总线时序，以及 16 组 NIST 向量是如何逐组比对的。
- **去读分层测试**：u3-l3 讲每个子模块各自的独立 testbench（`tb_aes_core`、`tb_aes_encipher_block` 等）。带着一个问题去读——本讲的端到端追踪依赖顶层 `tb_aes`，但如果只想单独验证 enc_block 的 51 拍轨迹，该怎么不依赖顶层地做？答案就在分层 testbench 里。
- **把端到端时序和架构取舍挂钩**：本讲看到一次加密端到端约 52 拍、其中 SubBytes 占了每轮 5 拍里的 4 拍。在读 u3-l4（ASIC 设计取舍）时，你会把这条「逐字 SubBytes + 共享 S-box」的端到端代价，和「全核吞吐仅 0.06 Gbps、面积小」直接对应起来——本讲的追踪表就是那条取舍的定量证据。
- **想亲手跑通追踪表**：回到 u1-l5，用 iverilog/ModelSim 编译 `rtl/*.v` 跑 `tb_aes`，把第 5.3 节列出的信号加进波形，对照三阶段表逐段走一遍，确认你能指着波形说出每一段落在哪个模块。
