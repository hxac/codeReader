# 密钥扩展与轮密钥存储

## 1. 本讲目标

在 u2-l1 里我们把 `aes_core` 当作「调度中枢」，看到它在 `init` 脉冲到来后会去驱动一个叫 `key_mem` 的子模块，并在此期间把唯一的正向 S-box 分时让给它用。本讲就打开这个 `key_mem`（即 `aes_key_mem.v`）黑盒，专门讲清**密钥扩展（Key Expansion）**这一条数据通路。

学完本讲，你应当能够：

- 说清楚 AES 为什么需要「密钥扩展」，以及「轮密钥（round key）」是什么。
- 读懂 `aes_key_mem.v` 里 `IDLE/INIT/GENERATE/DONE` 四状态 FSM，并算出一次扩展要花多少个时钟周期。
- 看懂 `round_key_gen` 组合块如何用 `prev_key0/prev_key1` 两个 128 位寄存器和共享 S-box 的回送值 `new_sboxw`，在一个周期内算出新的 128 位轮密钥。
- 理解 `rcon_logic` 里那行看似奇怪的 `0x8d` 初值，如何利用 GF(2⁸) 的 `xtime` 倍乘递推出 AES 标准的轮常数序列。
- 区分 AES-128（只用 `prev_key1`，10 轮，11 把轮密钥）与 AES-256（用 `prev_key0+prev_key1` 滑动窗口，14 轮，15 把轮密钥）在源码分支上的差异。

## 2. 前置知识

### 2.1 为什么 AES 要做密钥扩展

AES 一轮里有一步叫 **AddRoundKey**——把当前状态与一把「这一轮专用的密钥」逐字节异或。AES 标准规定：

- AES-128：原始密钥 128 位，共 10 轮，需要 **11 把** 128 位轮密钥（第 0 把就是原始密钥，用于初始 AddRoundKey；第 1~10 把分别用于 10 轮）。
- AES-256：原始密钥 256 位，共 14 轮，需要 **15 把** 128 位轮密钥。

如果每把轮密钥都独立存放，就要额外存 \(128 \times (15-1)=1792\) 位。AES 的设计者们给出了一个**递推算法**：只存原始密钥，后续每一把轮密钥都由前一把「算」出来，这样硬件只需一个不大的存储和一个计算单元即可——这就是**密钥扩展**。本工程的策略是：**在 `init` 阶段一次性把所有轮密钥算好，存进 `key_mem[0..14]` 数组**；之后真正加/解密时，加/解密引擎只要给出轮号 `round`，就能像查表一样读出对应轮密钥。

### 2.2 把轮密钥想成「字（word）」的序列

把 128 位看成 4 个 32 位的「字」\(w_0, w_1, w_2, w_3\)。AES 密钥扩展本质上是在生成一串字 \(w_0, w_1, w_2, \dots\)，每 4 个字拼成一把 128 位轮密钥。记号上，本工程用 `prev_key1_reg`（128 位）来滚动地保存「最近的 4 个字」，AES-256 还额外用 `prev_key0_reg` 保存「再往前 4 个字」，组成一个 8 字的滑动窗口。这两个寄存器是理解整段 `round_key_gen` 的钥匙，后面会反复用到。

### 2.3 GF(2⁸) 与 `xtime`

AES 的字节运算定义在有限域 GF(2⁸) 上（见 u2-l2、u2-l4）。这里只需知道一个核心操作 **`xtime`（乘 2）**：把一个字节左移一位，若原最高位为 1，则再异或上 AES 的约减多项式 `0x1b`：

\[
\text{xtime}(a) = ((a \ll 1) \,\&\, \text{0xfe}) \oplus (\text{0x1b} \cdot (a \gg 7))
\]

其中 \(a \gg 7\) 取出最高位，最高位为 1 时才异或 `0x1b`。轮常数 `rcon` 正是反复对 `0x01` 做 `xtime` 得到的序列。

### 2.4 承接前面讲义的约定

- u1-l3 讲过本工程统一的 `reg/_new/_we` 两段式寄存器写法：组合块算 `_new` 和 `_we`，时序块在 `posedge clk` 时 `if (_we) _reg <= _new`。本讲会大量遇到这种写法。
- u2-l1 讲过 `aes_core` 在 `CTRL_INIT` 态会把 `init_state` 拉高，`sbox_mux` 据此把共享 S-box 接给 `key_mem`；扩展完成后 `key_mem.ready` 回喂 `aes_core`，使其退回 `IDLE`。
- u2-l2 讲过 S-box 是**纯组合查表**（`assign new_sboxw[...] = sbox[sboxw[...]]`）。这一点对密钥扩展至关重要：`key_mem` 把待替换的字 `w7` 送出去，**同一个周期**就能拿到 `new_sboxw`，从而组合地算出新轮密钥。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的地方 |
|------|------|----------------|
| [rtl/aes_key_mem.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v) | **本讲主角**：密钥扩展 + 轮密钥存储 | 全文 |
| [rtl/aes_core.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v) | 例化 `key_mem`、用 `sbox_mux` 把共享 S-box 接给它 | 例化与 `sbox_mux` |
| [rtl/aes_sbox.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_sbox.v) | 纯组合正向 S-box，提供 `new_sboxw` | 4 个并行 `assign` |
| [rtl/tb_aes_key_mem.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v) | `key_mem` 的独立测试平台，含 NIST 已知轮密钥向量 | 实践依据 |

先看 `key_mem` 的对外端口，建立整体印象：

[rtl/aes_key_mem.v:L9-L24](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L9-L24) —— 定义了模块端口：`key`(256 位原始密钥)、`keylen`(0=128 位、1=256 位)、`init`(扩展启动脉冲)、`round`(加/解密时给定的轮号)、`round_key`(读出的 128 位轮密钥)、`ready`(扩展完成标志)，以及一对共享 S-box 接口 `sboxw`/`new_sboxw`。

注意端口里**没有**「待扩展的字」这种细节——`key_mem` 自己内部决定要把哪个字送进 S-box（即 `sboxw`），S-box 的结果从 `new_sboxw` 送回来。这是一种「**借外部的 S-box 来用**」的设计：`key_mem` 自己不例化 S-box，而是把替换需求通过端口外露，由上层 `aes_core` 的共享 S-box 来满足（见 u2-l1 的资源共享思想）。

## 4. 核心概念与源码讲解

本讲按 4 个最小模块展开：①`key_mem_ctrl` 控制状态机 → ②`round_key_gen` 轮密钥生成 → ③`rcon_logic` 轮常数 → ④AES-128/256 分支差异。它们之间的协作关系如下：

```
            init 脉冲
               │
               ▼
   ┌──────────────────────┐
   │  ① key_mem_ctrl FSM  │  产出 round_key_update / round_ctr_inc / ready
   │  IDLE→INIT→GENERATE  │
   │       →DONE          │
   └──────────┬───────────┘
              │ round_key_update=1 时激活
              ▼
   ┌──────────────────────┐    sboxw(w7)        new_sboxw
   │ ② round_key_gen      │ ─────────────────▶  (经 aes_core 的
   │  用 prev_key0/1 +    │ ◀─────────────────   共享 sbox_inst)
   │  new_sboxw 算新轮密钥│
   └──────────┬───────────┘
              │ 也产出 rcon_next（控制 ③）
              ▼
   ┌──────────────────────┐
   │ ③ rcon_logic          │  维护 rcon_reg（xtime 递推，0x8d 哨兵）
   └──────────────────────┘
              │
   每个周期算出的 key_mem_new 在时钟沿写入 key_mem[round_ctr_reg]
```

### 4.1 key_mem_ctrl 控制状态机

#### 4.1.1 概念说明

`key_mem_ctrl` 是 `key_mem` 的「指挥」：它本身**不做任何密钥运算**，只负责决定「现在该不该算一把新轮密钥」「轮计数器该不该加」「扩展完了没」「ready 该不该拉高」。它是一个四状态有限状态机（FSM），状态用 localparam 定义：

[rtl/aes_key_mem.v:L36-L39](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L36-L39) —— 定义 `CTRL_IDLE=0`、`CTRL_INIT=1`、`CTRL_GENERATE=2`、`CTRL_DONE=3` 四个状态。

注意它和 `aes_core` 的 FSM（u2-l1）是**两个不同粒度**的状态机：`aes_core` 的 FSM 是粗粒度的（IDLE/INIT/NEXT），只管「现在是在扩展还是在加解密」；而 `key_mem` 内部这个 FSM 是细粒度的，专门管扩展过程的几个阶段。`aes_core` 的 `CTRL_INIT` 态会一直维持到 `key_mem.ready` 变高——也就是说，外层粗 FSM 在「等」内层细 FSM 跑完一圈。

#### 4.1.2 核心流程

FSM 的状态转移如下（`num_rounds` 由 `keylen` 决定，AES-128=10、AES-256=14）：

```
            init=1
 IDLE ─────────────▶ INIT
  ▲                    │ round_ctr_rst=1（把轮计数器清 0）
  │ ready=1            ▼
  │                 GENERATE ──┐ 每拍：round_ctr_inc=1、round_key_update=1
  │                    │       │ 当 round_ctr_reg == num_rounds：
  │                    │       │   下一态 = DONE
  └──── DONE ◀─────────┘
       (ready=1)
```

关键点：

1. **IDLE**：等待 `init`。一旦 `init` 有效，拉低 `ready`，进入 INIT。
2. **INIT**：只待一拍，把 `round_ctr` 清零，进入 GENERATE。这是一个「准备」状态。
3. **GENERATE**：这是真正干活的阶段。**每一拍**都同时做两件事——把 `round_ctr` 加 1、把 `round_key_update` 拉高（通知 `round_key_gen`「这拍要算轮密钥」）。当 `round_ctr_reg` 计到 `num_rounds` 时，本拍仍会写最后一把轮密钥，同时下一拍转去 DONE。
4. **DONE**：把 `ready` 拉高（告诉 `aes_core`：扩展好了），返回 IDLE。

#### 4.1.3 源码精读

控制逻辑全部写在一个 `always @*` 组合块里：

[rtl/aes_key_mem.v:L336-L397](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L336-L397) —— `key_mem_ctrl` 块：先给所有输出赋默认值（`ready_new=0`、`round_key_update=0`、各计数器控制信号=0、`key_mem_ctrl_new=CTRL_IDLE`），再按 `key_mem_ctrl_reg` 的当前状态分支处理。这正是 u1-l3 讲过的「组合块开头先写默认值，避免生成锁存器」的写法。

`num_rounds` 的选择：

[rtl/aes_key_mem.v:L349-L352](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L349-L352) —— 根据 `keylen` 把 `num_rounds` 设为 10（AES-128）或 14（AES-256），随后 FSM 用它判断「是否计满」。

GENERATE 状态里最值得品味的是**「同一拍既写又判满」**：

[rtl/aes_key_mem.v:L373-L382](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L373-L382) —— `round_ctr_inc` 和 `round_key_update` 在 GENERATE 里**无条件**拉高；只有当 `round_ctr_reg == num_rounds` 时才把下一态改成 DONE。也就是说，在 `round_ctr_reg` 等于 `num_rounds` 的那一拍，`round_key_update` 仍然有效，会写最后一把轮密钥（见 4.2.3 的 `key_mem_we`），随后才转 DONE。这保证了 `key_mem[0..num_rounds]` 全部被写满。

`ready` 的时序也值得注意。复位时 `ready_reg=0`（见 4.1.4），平时 IDLE 态里默认 `ready_new=0`——等等，那 `aes_core` 怎么知道扩展完成？答案是：只有在 **DONE** 态才会把 `ready_new=1, ready_we=1`：

[rtl/aes_key_mem.v:L384-L390](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L384-L390) —— DONE 态：`ready_new=1`，下一态回 IDLE。于是 `ready_reg` 在 DONE→IDLE 的那次时钟沿被置 1，并向上一路传到 `aes_core` 的 `key_ready`，让 `aes_core` 退出 `CTRL_INIT`。

#### 4.1.4 代码实践

**实践目标**：亲手数清楚一次 AES-128 密钥扩展需要多少个时钟周期，并验证 `key_mem[0..10]` 都会被写满。

**操作步骤**（源码阅读型实践）：

1. 打开 [rtl/aes_key_mem.v:L101-L140](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L101-L140)，确认复位时 `round_ctr_reg<=0`、`key_mem_ctrl_reg<=CTRL_IDLE`、`ready_reg<=0`。
2. 假设 `init` 在某拍出现，画一张表，逐拍记录 `key_mem_ctrl_reg`、`round_ctr_reg`（注意取的是寄存器**当前**值，即上一拍写入的值）、`round_key_update`、`key_mem_we`。
3. 数一下从 IDLE 收到 `init` 到 DONE 拉高 `ready`，共经历几拍；其中 `key_mem_we=1` 的拍数（即实际写入 `key_mem` 的条数）。

**需要观察的现象 / 预期结果**：

- `round_ctr_reg` 在 GENERATE 阶段依次取 0,1,2,…,10（共 11 个值）。
- `key_mem_we=1` 出现 11 次，分别写 `key_mem[0]`~`key_mem[10]`（因为写地址是 `key_mem[round_ctr_reg]`，见 [L128-L129](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L128-L129)）。
- AES-128 共需 11 把轮密钥，全部就位 ✓。
- AES-256 同理，`round_ctr_reg` 取 0~14，写 15 把轮密钥。

> 待本地验证：可用 iverilog/ModelSim 跑 [rtl/tb_aes_key_mem.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v)，在 `dump_dut_state` 输出里数 `key_mem_ctrl` 与 `round_ctr_reg` 的变化拍数，确认与手算一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 INIT 状态是必要的？能不能让 IDLE 在收到 `init` 后直接跳到 GENERATE？

> **参考答案**：INIT 这一拍专门用来做 `round_ctr_rst`（把轮计数器清零）。如果从 IDLE 直跳 GENERATE，那么进入 GENERATE 第一拍时 `round_ctr_reg` 可能不是 0（虽然复位后是 0，但若连续做两次扩展，上一次结束时 `round_ctr_reg` 停在 `num_rounds`，不清零就会从错误位置开始写）。INIT 保证了每次扩展都从 `round_ctr_reg=0` 干净起步。

**练习 2**：DONE 状态能否省掉，让 GENERATE 在计满时直接回 IDLE 并拉高 `ready`？

> **参考答案**：从功能上可以让 GENERATE 在 `round_ctr_reg==num_rounds` 时同时设 `ready_new=1` 并回 IDLE，省掉 DONE 这一拍。当前实现多花一拍（DONE）显式拉高 `ready` 再回 IDLE，是可读性/时序余量的取舍，不影响正确性——多一个时钟周期对一次性的 `init` 阶段来说代价可忽略。

---

### 4.2 round_key_gen 轮密钥生成

#### 4.2.1 概念说明

`round_key_gen` 是 `key_mem` 的「运算核心」。它是一个**纯组合块**（`always @*`），只在一个条件下才真正输出有效结果：当 FSM 给出的 `round_key_update` 为 1 时。它的职责是：**给定上一把轮密钥（在 `prev_key0/prev_key1` 里）和 S-box 的回送值，组合地算出下一把 128 位轮密钥 `key_mem_new`，并决定要更新哪些 `prev_key` 寄存器**。

理解它的关键，是先看懂它如何把 128 位拆成 4 个 32 位「字」：

- `w0,w1,w2,w3` 取自 `prev_key0_reg`（AES-256 才有意义，对应「再前一把」轮密钥）；
- `w4,w5,w6,w7` 取自 `prev_key1_reg`（「最近一把」轮密钥；AES-128 里它就是当前工作密钥）。

然后它把 `w7`（最近一把轮密钥的最后一个字）送进 S-box，拿回 `new_sboxw`，再据此构造两个「修正项」：

- `trw`（带 RotWord + Rcon，用于「整字边界」的扩展步）；
- `tw`（只 SubWord、不旋转、不异或 Rcon，用于 AES-256 的「半字边界」步）。

#### 4.2.2 核心流程

AES 标准的密钥扩展递推（以字为单位），记 \(N_k\) 为密钥字数（AES-128: \(N_k=4\)，AES-256: \(N_k=8\)）：

\[
w_i =
\begin{cases}
w_{i-N_k} \oplus \text{SubWord}(\text{RotWord}(w_{i-1})) \oplus \text{Rcon}[i/N_k] & i \bmod N_k = 0 \\
w_{i-N_k} \oplus \text{SubWord}(w_{i-1}) & (N_k=8)\ \text{且}\ i \bmod N_k = 4 \\
w_{i-N_k} \oplus w_{i-1} & \text{其它}
\end{cases}
\]

硬件每拍生成 4 个字（一把 128 位轮密钥）。把上面式子展开成「一次生成 4 个字」的形式（以 AES-128，\(N_k=4\) 为例，记 `trw` 即上式中的 \(\text{SubWord}(\text{RotWord}(w_{i-1}))\oplus\text{Rcon}\)）：

\[
\begin{aligned}
k_0 &= w_{i-4} \oplus \text{trw} \\
k_1 &= w_{i-3} \oplus k_0 = w_{i-3} \oplus w_{i-4} \oplus \text{trw} \\
k_2 &= w_{i-2} \oplus k_1 = w_{i-2} \oplus w_{i-3} \oplus w_{i-4} \oplus \text{trw} \\
k_3 &= w_{i-1} \oplus k_2 = w_{i-1} \oplus w_{i-2} \oplus w_{i-3} \oplus w_{i-4} \oplus \text{trw}
\end{aligned}
\]

新轮密钥 \(=\{k_0,k_1,k_2,k_3\}\)。这正是源码里 `k0=w4^trw; k1=w5^w4^trw; …` 的来历（AES-128 用 `w4..w7`，即 `prev_key1_reg` 的 4 个字）。把上面的链式异或展开，就得到源码那种「累加式」写法。

#### 4.2.3 源码精读

先看拆字与中间量计算：

[rtl/aes_key_mem.v:L183-L197](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L183-L197) —— 把 `prev_key0_reg` 拆成 `w0..w3`、`prev_key1_reg` 拆成 `w4..w7`；令 `tmp_sboxw=w7`（要送进 S-box 的字）、`rconw={rcon_reg,24'h0}`（把 `rcon` 放到 32 位字的最高字节）、`rotstw`（对 S-box 结果做 RotWord：把最高字节挪到最低）、`trw=rotstw^rconw`（RotWord 后再异或 Rcon）、`tw=new_sboxw`（纯 SubWord 结果，不旋转不加 Rcon）。

注意 `rotstw = {new_sboxw[23:00], new_sboxw[31:24]}`：把 32 位的最高字节换到最低位，其余三个字节整体左移一个字节——这就是 AES 的 **RotWord**。异或上放在最高字节的 `rconw`，得到 **SubWord→RotWord→Rcon** 三合一的 `trw`。

再看整体的外壳：`round_key_update` 为 1 时才允许写：

[rtl/aes_key_mem.v:L200-L204](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L200-L204) —— 只有 `round_key_update=1`（即 FSM 处于 GENERATE）时，才把 `key_mem_we=1`，并按 `keylen` 分支计算 `key_mem_new`。这正是「算与不算由 FSM 说了算」的体现：FSM 在 IDLE/INIT/DONE 时 `round_key_update=0`，本块不写任何东西。

写回逻辑在时序块里：

[rtl/aes_key_mem.v:L128-L129](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L128-L129) —— 时钟沿上，若 `key_mem_we=1`，就把 `key_mem_new` 写入 `key_mem[round_ctr_reg]`。**写地址就是当前轮计数器的值**——这就是为什么 4.1 里强调要数清 `round_ctr_reg` 的取值。

读出轮密钥（供加/解密使用）走另一个组合读口：

[rtl/aes_key_mem.v:L148-L151](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L148-L151) —— `tmp_round_key = key_mem[round]`，把加/解密引擎送来的轮号 `round` 直接当数组下标，**异步读出**对应轮密钥。也就是说 `key_mem` 既是「写入端（扩展时按 `round_ctr_reg` 写）」又是「读出端（加/解密时按外部 `round` 读）」，两个端口地址来源不同。

最后强调一个**贯穿全工程的硬件取舍**（承接 u2-l1/u2-l2）：`key_mem` 自己不例化 S-box，而是通过 `sboxw`/`new_sboxw` 端口「借用」`aes_core` 里那个唯一的正向 S-box：

[rtl/aes_core.v:L121-L135](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L121-L135) —— `aes_core` 例化 `key_mem`，把 `keymem_sboxw` 接出来；而 `sbox_mux`（[aes_core.v:L184-L194](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L184-L194)）在 `init_state` 期间把 `keymem_sboxw` 选送给共享 `sbox_inst`。由于 S-box 是纯组合（[aes_sbox.v:L25-L28](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_sbox.v#L25-L28) 的 4 个 `assign`），`new_sboxw` 与 `sboxw` 同周期成立，`round_key_gen` 才能在**一个周期内**算完一把轮密钥。如果 S-box 是寄存器输出的（延迟一拍），这套组合逻辑就不成立了。

#### 4.2.4 代码实践

**实践目标**：用全零密钥手算 AES-128 的第 1 把轮密钥，并与测试平台里的期望值核对。

**操作步骤**（手算 + 源码核对型）：

1. 取 AES-128 全零密钥：`prev_key1_reg = 0x00000000_00000000_00000000_00000000`（即 `w4=w5=w6=w7=0`）。这是 `key_mem[0]`，对应 [tb_aes_key_mem.v:L410-L411](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v#L410-L411) 的 `key128_0` 与 `expected_00`。
2. 算 `key_mem[1]`：此时 `w7=0`，故 `tmp_sboxw=0`，经 S-box `new_sboxw = sbox[0x00..] = 0x63636363`（S-box 表 `sbox[0x00]=0x63`，见 u2-l2）。`rotstw = {0x636363, 0x63} = 0x63636363`（4 字节相同，旋转后不变）。`trw = 0x63636363 ^ {rcon,0,0,0}`，此处 `rcon=0x01`（见 4.3），`trw = 0x62636363`。
3. 套公式：`k0=w4^trw=0^0x62636363=0x62636363`，`k1=w5^w4^trw=0x62636363`，同理 `k2=k3=0x62636363`，故 `key_mem[1]=0x62636363626363636263636362636363`。
4. 对照 [tb_aes_key_mem.v:L412](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v#L412) 的 `expected_01`。

**预期结果**：`expected_01 = 128'h62636363626363636263636362636363`，与手算完全一致 ✓。

> 待本地验证：可在 [tb_aes_key_mem.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v) 的 `dump_dut_state` 输出里，在生成 `key_mem[1]` 那一拍抓取 `trw`、`key_mem_new` 的实际值，确认与上面手算吻合。

#### 4.2.5 小练习与答案

**练习 1**：为什么源码里 `k1 = w5 ^ w4 ^ trw` 而不是简单的 `k1 = w5 ^ 某个量`？

> **参考答案**：因为 AES 递推是 \(w_i = w_{i-4} \oplus w_{i-1}\)（或带 g 变换）的**链式**关系。新字的第 1 个字 \(k_0=w_{i-4}\oplus\text{trw}\)，第 2 个字 \(k_1=w_{i-3}\oplus k_0=w_{i-3}\oplus w_{i-4}\oplus\text{trw}\)。源码把 \(k_0\) 直接代入，于是每个 \(k_j\) 都是「前面所有相关字异或起来再异或 trw」，写成 `k1=w5^w4^trw` 正是这个展开。这避免了引入中间寄存器，纯组合一拍算完。

**练习 2**：`round_key_gen` 块里 `tmp_sboxw` 为什么固定取 `w7`，而不是根据轮号变化？

> **参考答案**：`w7` 永远是「最近一把轮密钥的最后一个字」，即递推式里的 \(w_{i-1}\)。无论 AES-128 还是 AES-256，无论第几把轮密钥，需要做 SubWord/RotWord 的永远是「上一个字」\(w_{i-1}\)，它在硬件里就固定落在 `prev_key1_reg` 的最低 32 位 `w7`。所以送进 S-box 的字恒为 `w7`，不需要随轮号切换。

---

### 4.3 rcon_logic 轮常数

#### 4.3.1 概念说明

**Rcon（Round Constant，轮常数）** 是 AES 密钥扩展里用来「打破对称性」的固定常数序列。如果没有 Rcon，那么「全零密钥」扩展出来的所有轮密钥都会是 0，加密就毫无意义。Rcon 序列定义为：

\[
\text{Rcon}[i] = 02^{i-1} \in \text{GF}(2^8),\quad i=1,2,3,\dots
\]

也就是反复做 `xtime`（乘 2）：`Rcon[1]=0x01`、`Rcon[2]=0x02`、`Rcon[3]=0x04`、…、`Rcon[8]=0x80`、`Rcon[9]=0x1b`、`Rcon[10]=0x36`。AES-128 用到 `Rcon[1..10]`，AES-256 用到 `Rcon[1..7]`。

#### 4.3.2 核心流程

`rcon_logic` 维护一个 8 位寄存器 `rcon_reg`，靠两个控制信号驱动：

- `rcon_set`：把 `rcon_reg` 置为一个**哨兵值 `0x8d`**；
- `rcon_next`：把 `rcon_reg` 更新为 `xtime(rcon_reg)`。

`0x8d` 这个看似奇怪的初值，是一个**精巧的小技巧**：

\[
\text{xtime}(\text{0x8d}) = \text{0x01}
\]

验证：`0x8d = 1000_1101`，最高位为 1，故 \(\text{xtime}(\text{0x8d}) = (0x8d\ll 1)\,\&\,0xfe \oplus 0x1b = 0x1a \oplus 0x1b = 0x01\)。于是「先 `rcon_set` 到 `0x8d`，再 `rcon_next` 一次」就恰好得到标准序列的第一项 `Rcon[1]=0x01`。这样 `rcon_logic` 的硬件只需要一个 `xtime` 运算器 + 一个初值选择，就能从 `0x8d` 出发递推出整个 Rcon 序列，无需单独存一张 Rcon 表。

`rcon_set` / `rcon_next` 由谁产生？答：由 `round_key_gen` 产生（见 4.2.3 的默认值与各分支）。默认 `rcon_set=1`（让 `rcon` 停在 `0x8d`），只有在需要推进的分支里才把 `rcon_next=1`。

#### 4.3.3 源码精读

[rtl/aes_key_mem.v:L284-L303](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L284-L303) —— `rcon_logic` 块：先算 `tmp_rcon = {rcon_reg[6:0],1'b0} ^ (0x1b & {8{rcon_reg[7]}})`，这正是 `xtime` 的位级实现（左移一位；若最高位为 1，再异或 `0x1b`）。随后 `rcon_set` 分支置 `0x8d`，`rcon_next` 分支置 `tmp_rcon`。

注意两个 `if` 是**顺序执行**的（这是 `always @*` 阻塞赋值的特性）：若同一拍 `rcon_set` 与 `rcon_next` 都为 1，后者会覆盖前者，即 `rcon_next` 优先级更高。但在实际数据流里，`round_key_update=1`（GENERATE）期间 `rcon_set` 已被强制清 0（见 [L202](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L202)），所以二者一般不会冲突；`rcon_set=1` 主要发生在非 GENERATE 的拍（如 IDLE），把 `rcon_reg` 维持在 `0x8d`，为下次扩展做好准备。

那 `rcon_next` 在哪些分支被拉高？查 `round_key_gen`：

- AES-128：`round_ctr_reg==0`（播种原始密钥，[L212](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L212)）和后续每把（[L224](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L224)）都 `rcon_next=1`。
- AES-256：`round_ctr_reg==1`（播种第二个半密钥，[L241](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L241)）和**奇数轮**（`round_ctr_reg[0]==1`，[L258](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L258)）`rcon_next=1`；偶数轮（用 `trw`，真正消耗 Rcon 的那一把）反而不推进。

这里的「奇数轮推进、偶数轮使用」看似反直觉，其实是**为下一把做准备**：偶数轮把当前 `rcon_reg` 用进 `trw`，奇数轮（不消耗 Rcon 的那把）才把 `rcon` 推进一格，供下一个偶数轮使用。详见 4.4 的时序追踪。

#### 4.3.4 代码实践

**实践目标**：验证 `0x8d` 哨兵技巧，并手推 AES-256 用到的 Rcon 子序列。

**操作步骤**：

1. 用 `xtime` 定义手算：`xtime(0x8d)=?`、`xtime(0x01)=?`、…、`xtime(0x80)=?`。
2. 写出 `Rcon[1..7]` 的十六进制值。
3. 对照 4.4 的时序，确认 AES-256 在生成 `key_mem[2]` 时用的是 `Rcon[1]`、`key_mem[4]` 用 `Rcon[2]`、…、`key_mem[14]` 用 `Rcon[7]`。

**预期结果**：

| 步骤 | rcon_reg 值 | 含义 |
|------|------------|------|
| `rcon_set` 后 | `0x8d` | 哨兵 |
| 第 1 次 `rcon_next` | `0x01` | `Rcon[1]` |
| 第 2 次 | `0x02` | `Rcon[2]` |
| 第 3 次 | `0x04` | `Rcon[3]` |
| 第 4 次 | `0x08` | `Rcon[4]` |
| 第 5 次 | `0x10` | `Rcon[5]` |
| 第 6 次 | `0x20` | `Rcon[6]` |
| 第 7 次 | `0x40` | `Rcon[7]` |

> 待本地验证：在仿真里观察 `dut.rcon_reg` 随 `round_ctr_reg` 的变化，确认上表。

#### 4.3.5 小练习与答案

**练习 1**：如果不采用 `0x8d` 哨兵，直接复位 `rcon_reg=0`、并让第一次使用时「特殊处理」给 `0x01`，可行吗？为什么作者偏要用 `0x8d`？

> **参考答案**：可行，但需要在 `round_key_gen` 里加一条「若当前是第一次推进则用 `0x01`」的特殊分支，增加控制复杂度。用 `0x8d` 哨兵后，`rcon_logic` 只需统一的 `xtime` 通路，`round_key_gen` 只需在合适的分支拉 `rcon_next`，第一次推进自然得到 `0x01`。这是用「数据（哨兵初值）换控制逻辑」的典型简化。

**练习 2**：`tmp_rcon` 表达式里 `{8{rcon_reg[7]}}` 起什么作用？

> **参考答案**：它是「把最高位 `rcon_reg[7]` 复制成 8 位掩码」——最高位为 1 时是 `8'hff`，为 0 时是 `8'h00`。`0x1b & {8{rcon_reg[7]}}` 就实现了「最高位为 1 时才异或 `0x1b`，否则异或 0」。这是 `xtime` 中条件异或约减多项式的标准位级写法。

---

### 4.4 AES-128 与 AES-256 的分支差异

#### 4.4.1 概念说明

本工程的运行时通过 `keylen`（CONFIG 寄存器的 bit1，见 u1-l4）在 AES-128 与 AES-256 之间切换。两者在密钥扩展上的根本差异是**「工作窗口」的大小**：

- **AES-128**：密钥 128 位 = 4 个字。递推窗口只需「上一把 4 字轮密钥」，故只用 `prev_key1_reg`（128 位）就够；`prev_key0_reg` **完全不用**（从不写、从不读）。
- **AES-256**：密钥 256 位 = 8 个字。递推窗口需要「上一把 + 再上一把」共 8 个字，故需要 `prev_key0_reg` 与 `prev_key1_reg` 组成**滑动窗口**：每生成一把新轮密钥，就把「老的 `prev_key1`」滑进 `prev_key0`，把「新生成的」放进 `prev_key1`。

此外，AES-256 在 8 字窗口里多了一种「半字边界」步（对应标准里 \(i\bmod 8=4\) 的情形），这一步**只做 SubWord、不做 RotWord、不异或 Rcon**——对应源码里用 `tw` 而非 `trw`。

#### 4.4.2 核心流程（AES-256 的三轮种）

把 AES-256 在 `round_ctr_reg` 不同取值时的行为归纳成下表（这是本讲最重要的实践对象）：

| `round_ctr_reg` | 阶段 | `key_mem_new` 来源 | 更新的 prev 寄存器 | `rcon_next` | 含义 |
|---|---|---|---|---|---|
| 0 | 播种 | `key[255:128]`（密钥高半） | `prev_key0 ← key[255:128]` | 否 | 把密钥前 4 字存为 `key_mem[0]`，并装进 `prev_key0` |
| 1 | 播种 | `key[127:0]`（密钥低半） | `prev_key1 ← key[127:0]` | **是** | 把密钥后 4 字存为 `key_mem[1]`，装进 `prev_key1`；同时把 rcon 从 `0x8d` 推进到 `0x01` |
| 2,4,6,…（偶） | 用 `trw` | `{w0^trw, w1^w0^trw, w2^w1^w0^trw, w3^w2^w1^w0^trw}` | `prev_key1 ← 新值`；`prev_key0 ← 旧 prev_key1` | 否 | 标准的「整字边界」步：SubWord+RotWord+Rcon，消耗当前 rcon |
| 3,5,7,…（奇） | 用 `tw` | `{w0^tw, w1^w0^tw, w2^w1^w0^tw, w3^w2^w1^w0^tw}` | `prev_key1 ← 新值`；`prev_key0 ← 旧 prev_key1` | **是** | AES-256 独有的「半字边界」步：只 SubWord；并推进 rcon 给下一个偶数轮 |

要点解读：

- `prev_key0` 在 AES-256 里扮演 \(w_{i-8..i-5}\)（再前一把），`prev_key1` 扮演 \(w_{i-4..i-1}\)（上一把）。生成新轮密钥时，基础字来自 `prev_key0`（`w0..w3`），修正项（`trw`/`tw`）由 `prev_key1` 的末字 `w7` 经 S-box 得到。
- 每把新轮密钥生成后，**`prev_key1` 更新为新值，`prev_key0` 更新为「更新前的旧 `prev_key1`」**（[L262-L266](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L262-L266)），这就是「滑动窗口」——窗口整体向前挪一把。
- 偶数轮用 `trw`（含 Rcon），但**不**推进 rcon；奇数轮用 `tw`（不含 Rcon），**反而**推进 rcon。结果是：偶数轮（`round_ctr=2,4,…,14`）依次使用 `Rcon[1..7]`，正好对应 AES-256 需要 7 个 Rcon。

#### 4.4.3 源码精读

**AES-128 分支**（注意：128 位密钥放在 256 位 `key` 输入的**高半** `key[255:128]`，这是 `tb` 的约定）：

[rtl/aes_key_mem.v:L205-L226](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L205-L226) —— AES-128：`round_ctr_reg==0` 时把 `key[255:128]` 同时写入 `key_mem[0]` 和 `prev_key1`（播种）；其余轮按 `k0=w4^trw, k1=w5^w4^trw, …` 计算并更新 `prev_key1`。**全程不动 `prev_key0`**，因为 4 字窗口只需要一个 128 位寄存器。

**AES-256 分支**：

[rtl/aes_key_mem.v:L228-L268](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L228-L268) —— AES-256 完整分支，下面分三段看。

**轮 0（播种高半）**：

[rtl/aes_key_mem.v:L230-L235](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L230-L235) —— `key_mem_new = key[255:128]`，写入 `prev_key0`（**注意是 `prev_key0`，不是 `prev_key1`**），不推进 rcon。因为 AES-256 密钥前 4 字要先装进窗口的「前半」位置。

**轮 1（播种低半）**：

[rtl/aes_key_mem.v:L236-L242](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L236-L242) —— `key_mem_new = key[127:0]`，写入 `prev_key1`，并 `rcon_next=1`（把 rcon 从 `0x8d` 推到 `0x01`，为轮 2 准备）。至此窗口 `prev_key0/prev_key1` 装满了原始 256 位密钥。

**轮 ≥2（偶/奇分支 + 滑动窗口）**：

[rtl/aes_key_mem.v:L243-L267](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L243-L267) —— 偶数轮（`round_ctr_reg[0]==0`）用 `trw`，奇数轮用 `tw` 并推进 rcon；随后统一执行滑动：`prev_key1_new={k0,k1,k2,k3}`、`prev_key0_new=prev_key1_reg`（把旧 `prev_key1` 滑进 `prev_key0`）。

**关于 `prev_key0/prev_key1` 的作用，一句话总结**：

- `prev_key1_reg`：滚动保存「最近一把」128 位轮密钥，其末字 `w7` 是送进 S-box 的对象；AES-128 和 AES-256 都用它。
- `prev_key0_reg`：**仅 AES-256 使用**，保存「再前一把」128 位轮密钥（即 \(w_{i-8..i-5}\)），为新轮密钥提供基础字 `w0..w3`；通过 `prev_key0_new=prev_key1_reg` 实现每把的滑动。

为什么 AES-256 偶数轮用 `trw`（基础字是 `w0..w3`，即 `prev_key0`）？因为 AES-256 的 \(N_k=8\)，新字 \(w_i = w_{i-8}\oplus\text{g}(w_{i-1})\)，其中 \(w_{i-8}\) 在窗口里就是 `prev_key0` 的第一个字 `w0`，而 \(w_{i-1}\) 是 `prev_key1` 的末字 `w7`。所以「基础字来自 `prev_key0`，修正项来自 `prev_key1` 的末字」完美对应标准递推。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：对照源码，完整说明 AES-256 在 `round_ctr_reg==0/1` 及偶/奇轮时 `key_mem_new` 的不同计算分支，并解释 `prev_key0/prev_key1` 的作用——即本讲规格要求的核心任务。

**操作步骤**：

1. 打开 [rtl/aes_key_mem.v:L228-L268](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L228-L268)，逐分支填写下表（已在前文 4.4.2 给出，这里要求你**亲自到源码里核对每一格**，确认 `key_mem_new`、被更新的 `prev_key`、`rcon_next` 三列）。
2. 用 NIST AES-256 密钥 [tb_aes_key_mem.v:L585](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v#L585) `603deb1015ca71be…0914dff4` 手算前 3 把轮密钥：
   - `key_mem[0]` 应等于 `key[255:128]` = `603deb1015ca71be2b73aef0857d7781`，对照 [L586](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v#L586) `expected_00`。
   - `key_mem[1]` 应等于 `key[127:0]` = `1f352c073b6108d72d9810a30914dff4`，对照 [L587](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v#L587) `expected_01`。
   - `key_mem[2]`（轮 2，偶数，用 `trw`，`Rcon=0x01`）：`prev_key0=key_mem[0]`、`prev_key1=key_mem[1]`，`w7=prev_key1[31:0]=0914dff4`，经 S-box+RotWord+Rcon 得 `trw`，再 `{w0^trw, w1^w0^trw, …}`（`w0..w3` 取自 `prev_key0`）。手算后对照 [L588](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v#L588) `expected_02 = 9ba354118e6925afa51a8b5f2067fcde`。

**需要观察的现象**：

- 你应能解释「为什么轮 0 写 `prev_key0` 而轮 1 写 `prev_key1`」——因为播种时窗口要先填「前半」再填「后半」，对应 256 位密钥的高低两半。
- 你应能解释「为什么偶数轮不推进 rcon、奇数轮推进」——偶数轮消耗当前 rcon，奇数轮（半字边界，不用 rcon）趁机把 rcon 推进一格给下一个偶数轮。

**预期结果**：手算的 `key_mem[0..2]` 与 NIST 期望值逐一吻合；同时能口述 `prev_key0/prev_key1` 的滑动关系。

> 待本地验证：跑 [tb_aes_key_mem.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v) 的 NIST AES-256 用例，应看到 `test_key_256` 打印全部 `** key 0x? matched expected round key.`，最终 `*** All 09 test cases completed successfully`（共 9 组：5 组 AES-128 + 4 组 AES-256）。

#### 4.4.5 小练习与答案

**练习 1**：AES-256 在 `round_ctr_reg==0` 时写入 `prev_key0`，而在 AES-128 的 `round_ctr_reg==0` 写入 `prev_key1`。为什么有这个区别？

> **参考答案**：AES-128 的窗口只有 4 字（一个 128 位寄存器 `prev_key1`），所以播种时直接把密钥装进 `prev_key1`。AES-256 的窗口是 8 字（两个 128 位寄存器），播种分两拍：轮 0 装密钥高半到 `prev_key0`（窗口的「前半」位置），轮 1 装密钥低半到 `prev_key1`（窗口的「后半」位置）。位置不同，是因为两个算法的工作窗口宽度不同。

**练习 2**：如果有人误把 AES-256 当成 AES-128 来配置（`keylen=0` 但给了 256 位密钥），`key_mem` 会怎样？

> **参考答案**：FSM 的 `num_rounds` 会取 10，于是只生成 `key_mem[0..10]` 共 11 把；而且 `round_key_gen` 走 AES-128 分支，只用 `key[255:128]` 这 128 位、只用 `prev_key1`，忽略 `key[127:0]`。结果得到的是「以密钥高半当作 128 位密钥」的错误扩展，加/解密必然出错。这正说明 `keylen` 必须与实际密钥长度匹配——它同时影响轮数（`num_rounds`）和递推分支（`case(keylen)`）。

**练习 3**：滑动窗口那行 `prev_key0_new = prev_key1_reg`（[L265](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L265)）为什么读的是 `prev_key1_reg`（旧值）而不是 `prev_key1_new`（本拍刚算出的新值）？

> **参考答案**：因为滑动窗口要保留的是「上一把」轮密钥。本拍生成的「新一把」要放进 `prev_key1`，而「上一把」（即更新前的 `prev_key1`）要滑进 `prev_key0`。所以 `prev_key0_new` 必须取 `prev_key1_reg`（本拍开始时的旧值），而不是 `prev_key1_new`（本拍刚算出的新值）。这是「同一拍内 `prev_key1` 既被读（作滑动源）又被写（装入新值）」的典型场景，靠 `_reg`（旧值）与 `_new`（新值）的分离来正确实现。

## 5. 综合实践

**综合任务**：用 [tb_aes_key_mem.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v) 跑通密钥扩展，把本讲四个模块的知识串起来，验证「从原始密钥到全部轮密钥」的整条数据通路。

**步骤**：

1. **编译运行**：用 iverilog 一行编译（需把 S-box 一起带上，因为 `key_mem` 借用外部 S-box）：
   ```bash
   iverilog -o tb_keymem rtl/aes_key_mem.v rtl/aes_sbox.v rtl/tb_aes_key_mem.v
   vvp tb_keymem
   ```
   （ModelSim 则用 `rtl/` 下文件建库，顶层 `tb_aes_key_mem`，见 u1-l5。）
2. **观察 FSM**：在 `dump_dut_state` 输出里，跟踪一次 `init` 后 `key_mem_ctrl` 从 `IDLE→INIT→GENERATE(多拍)→DONE` 的全过程，数 `round_ctr_reg` 从 0 走到 `num_rounds` 的拍数（对应 4.1）。
3. **观察 rcon**：盯住 `dut.rcon_reg`，确认它先停在 `0x8d`，随后按 `0x01→0x02→…` 推进，且推进时机与 4.3/4.4 描述的分支一致。
4. **核对轮密钥**：测试平台的 `check_key` 任务（[tb_aes_key_mem.v:L219-L237](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v#L219-L237)）通过给 `dut.round` 赋值、读 `round_key` 来逐把比对 NIST 已知轮密钥。确认 AES-128（4 组 + NIST）、AES-256（3 组 + NIST）全部 `matched`。
5. **进阶**：仿照 `test_key_256` 的写法（[L293-L340](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v#L293-L340)），用自己算的一把轮密钥替换某组 `expected`，观察 `error_ctr` 是否如预期增加——以此反向确认你对递推公式的理解。

**预期结果**：最终打印 `*** All 09 test cases completed successfully`（9 = 5 组 AES-128 + 4 组 AES-256，各含一组 NIST 用例）。

> 待本地验证：本机若无 iverilog/ModelSim，至少完成步骤 2~4 的「读 `$display` 文字 + 源码核对」部分，这也是有效的源码阅读型实践。

## 6. 本讲小结

- `aes_key_mem` 在 `init` 阶段**一次性**算好所有轮密钥存进 `key_mem[0..14]`，加/解密时按外部轮号 `round` 异步读出；写入地址用内部 `round_ctr_reg`，读出地址用外部 `round`，两个端口互不干扰。
- 控制核心是 `key_mem_ctrl` 这个 `IDLE/INIT/GENERATE/DONE` 四状态 FSM；GENERATE 每拍无条件「计数器+1、`round_key_update=1`」，计到 `num_rounds` 那拍仍写最后一把轮密钥后才转 DONE，保证 `key_mem[0..num_rounds]` 写满。
- 运算核心 `round_key_gen` 是纯组合块，把 `prev_key1` 末字 `w7` 送进**共享** S-box（借 `aes_core` 的 `sbox_inst`，靠 `sbox_mux` 分时），同周期拿回 `new_sboxw`，组合地算出 `trw`（SubWord+RotWord+Rcon）或 `tw`（仅 SubWord）。
- `rcon_logic` 用 `0x8d` 哨兵 + `xtime` 递推，从 `0x8d` 出发第一拍得到标准 `Rcon[1]=0x01`，无需单独存 Rcon 表；这是「用数据哨兵简化控制」的典型技巧。
- AES-128 只用 `prev_key1`（4 字窗口，11 把轮密钥）；AES-256 用 `prev_key0+prev_key1` 组成 8 字滑动窗口（15 把轮密钥），并通过 `prev_key0_new=prev_key1_reg` 每把整体滑动一格。
- AES-256 的「偶数轮用 `trw` 消耗 Rcon、奇数轮用 `tw` 顺便推进 Rcon」的设计，让 7 个 Rcon 恰好覆盖 `key_mem[2,4,…,14]` 这 7 把需要整字变换的轮密钥。

## 7. 下一步学习建议

- **继续向加密通路走**：本讲讲清了「轮密钥从哪来」，下一讲 [u2-l4 加密数据通路四个变换函数](u2-l4-encipher-datapath-functions.md) 将讲清「轮密钥怎么被用进 AddRoundKey」，以及 SubBytes/ShiftRows/MixColumns 的组合实现。
- **回头看共享 S-box 的全貌**：结合 u2-l2 重新体会 `key_mem` 与 `encipher_block` 如何「分时复用」同一个 `sbox_inst`——这正是 u3-l4（ASIC 设计取舍）会重点讨论的「用时间换面积」。
- **端到端串联**：学完加密/解密 FSM（u2-l5/u2-l7）后，可进入 [u3-l1 一次完整加解密的端到端追踪](u3-l1-end-to-end-encryption-trace.md)，把「主机写 KEY → `init` 触发 `key_mem` 扩展 → `next` 触发加/解密读 `round_key` → 结果回写 RESULT」整条链路打通。
- **建议精读的源码**：把本讲的 [aes_key_mem.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v) 与 [tb_aes_key_mem.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v) 对照阅读——后者用 NIST 已知轮密钥逐把自检，是验证你对递推公式理解是否正确的最直接标尺。
