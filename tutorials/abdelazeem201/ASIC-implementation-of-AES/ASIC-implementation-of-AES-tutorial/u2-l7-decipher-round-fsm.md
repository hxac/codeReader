# 解密轮控制状态机

> 本讲对应大纲 `u2-l7`，依赖 [u2-l6 解密数据通路逆变换函数](u2-l6-decipher-datapath-inverse-functions.md) 与 [u2-l3 密钥扩展与轮密钥存储](u2-l3-key-expansion-and-round-key-mem.md)。
> 上一讲（u2-l6）只讲了 `aes_decipher_block.v` 里的**纯组合逆变换函数**（`inv_shiftrows`、`inv_mixcolumns`、`addroundkey` + 私挂的逆 S-box），并明确把「时钟级」的细节留给了本讲。本讲就来打开那个时钟级的黑盒——`decipher_ctrl` 状态机和那个**递减**的 `round_ctr`。
> 如果你想把加密侧的对照版先过一遍，建议先读 [u2-l5 加密轮控制状态机](u2-l5-encipher-round-fsm.md)，本讲的「综合实践」就是让你把两篇放在一起对比。

## 1. 本讲目标

u2-l6 让我们弄懂了解密「**会算**」哪些逆变换；本讲要回答的是：**谁来安排它们、按什么顺序、用哪一把轮密钥、在哪些时钟周期执行？** 答案的核心是一个和加密 FSM 几乎同构、却在计数方向上**故意相反**的控制状态机。

本讲学完后，你应该能够：

1. 说清楚 `decipher_ctrl` 这个状态机有哪几个状态、每个状态干什么、状态之间怎么转移，并指出它和 `encipher_ctrl`（u2-l5）结构上的异同。
2. 解释为什么解密的 `round_ctr` 是**递减**的——`round_ctr_set` 把它一次性置成 `num_rounds`（AES-128 为 10、AES-256 为 14），之后每轮 `round_ctr_dec` 减 1，直到归零；并说明这与「解密按倒序使用轮密钥」的因果关系。
3. 说出 `round_ctr_dec` 为什么出现在 `CTRL_SBOX`（`sword_ctr_reg == 3` 那拍）而不是 `CTRL_MAIN`，而加密侧的 `round_ctr_inc` 却同时出现在 `CTRL_INIT` 和 `CTRL_MAIN`。
4. 列出 `INIT/SBOX/MAIN/FINAL` 四种 `update_type` 在解密中分别对应哪一段运算、用哪一把 round key，并算出一次 AES-128/AES-256 解密**和加密一样**需要 51 / 71 个时钟周期。

本讲是解密通路的「指挥层」，把 u2-l6 的「运算层」串成一条按时钟节拍倒着流动的流水线。

## 2. 前置知识

阅读本讲前，请确认你已掌握：

- **u1-l3 的 reg/_new/_we 寄存器模式**：本模块沿用「组合逻辑块算 `_new`/`_we`，时序块在时钟沿 `if (_we) _reg <= _new`」的两段式写法，以及「上升沿触发 + 异步低有效复位」模板。本讲直接套用，不再重复。
- **u2-l6 的四个逆变换**：`inv_shiftrows`、`inv_mixcolumns`（含 `inv_mixw` 与 `gm09/11/13/14`）、`addroundkey`，以及通过私挂的 `inv_sbox_inst`（[rtl/aes_decipher_block.v:205](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L205)）实现的 InvSubBytes。本讲只关心它们「被谁、在何时调用」，不再重推其数学。
- **u2-l3 的轮密钥存储**：`key_mem[0..14]` 在 init 阶段一次性生成，加/解密时按外部给的轮号 `round` **组合（异步）读出**——这意味着本模块只要把正确的轮号摆在 `round` 端口上，对应的那把 round key **当拍就有效**。
- **u2-l5 的加密 FSM**：`encipher_ctrl` 是 4 状态机（IDLE/INIT/SBOX/MAIN），`round_ctr` **递增**（0→N），最终轮由 `CTRL_MAIN` 的 else 分支用 `FINAL_UPDATE` 实现，`CTRL_FINAL` 是死代码。本讲会和它逐项对比。

三个 AES 基本常识（u1-l1 已建立，这里只复述解密视角）：

- AES 解密是加密的**逆过程**。本工程采用 FIPS 197 的「**等价逆密码（equivalent inverse cipher）**」执行顺序，其结构刻意做成与加密对称——每个「解密轮」也是 InvSubBytes → InvShiftRows → InvMixColumns → AddRoundKey 的形态（u2-l6 §4.4 已说明 `InvShiftRows` 在实现里被挪到了上一拍尾部，但数学等价）。
- 解密**按倒序使用轮密钥**：先用最后一把 \(K_N\)（N=10 或 14），最后用第 0 把 \(K_0\)。这是本讲「为什么递减」的根本原因。
- **初始轮**只做 AddRoundKey（解密侧再跟一次 InvShiftRows）；**主轮**做一整套逆变换；**最终轮**省去 InvMixColumns（与加密最终轮省去 MixColumns 对称）。

还有一个 u2-l5 强调过、本讲同样适用的术语区分：`block` 是 **128 位输入端口**（解密时是**密文**），而 `block_w0_reg ... block_w3_reg` 是把 128 位拆成 4 个 32 位「字」后**寄存下来的中间状态**。务必区分「输入端口」和「状态寄存器」。

## 3. 本讲源码地图

本讲只涉及一个文件，它是整个解密通路的「大脑」：

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| [rtl/aes_decipher_block.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v) | 解密轮处理模块（含逆变换函数 + 控制 FSM） | 三个 `always` 块：`round_logic`（按 `update_type` 算什么）、`round_ctr`（**递减**轮计数器）、`decipher_ctrl`（FSM 总控），外加 `reg_update`（寄存器搬运） |

为做「加密递增 vs 解密递减」的对比，本讲会反复引用加密侧的 [rtl/aes_encipher_block.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v)（其 `encipher_ctrl` FSM 见 u2-l5）。两个模块的端口几乎一致，解密侧的关键端口（[rtl/aes_decipher_block.v:10-23](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L10-L23)）：

- `next`（输入）：核心 FSM 给的「开始解密」单拍脉冲。
- `round`（输出，4 位）：当前轮号，送给 `key_mem` 选 round key。注意 `assign round = round_ctr_reg`（[L211](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L211)），与加密侧**逐字符相同**——这正是「靠计数方向换轮密钥顺序」的前提。
- `round_key`（输入，128 位）：`key_mem` 按上面的轮号组合回送的本轮密钥。
- `block`（输入，128 位，**密文**）/ `new_block`（输出，128 位，**明文**）。
- `ready`（输出）：本模块空闲/完成标志。

> 解密模块**没有**加密侧那对 `sboxw`/`new_sboxw` 端口——因为逆向 S-box 是**私挂**在模块内部的 `inv_sbox_inst`（u2-l2、u2-l6），不经过 `aes_core` 的共享 S-box。这是加解密在端口上最显眼的差别。

## 4. 核心概念与源码讲解

本讲把模块拆成 3 个最小模块，按「数据 → 计数器 → 总控」的顺序讲（与 u2-l5 同序），最后在综合实践里把它们串成一条倒着流动的时钟轨迹，并与加密侧逐项对比。

### 4.1 round_logic：逆 update_type 的控制层视角

#### 4.1.1 概念说明

`round_logic` 是一个 **纯组合 `always @*` 块**（[rtl/aes_decipher_block.v:270-358](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L270-L358)）。它和加密侧一样，只负责回答一个问题：**「如果这一拍要更新数据，应该把哪一种逆运算结果写回 4 个字寄存器？」** 至于「现在第几轮、该用哪把 key」由后面的 `round_ctr` 和 `decipher_ctrl` 决定，`round_logic` 只管按给定的 `update_type` 把对应算式接出来。

> 注意：u2-l6 已经把每个分支里调用的**逆函数本身**（`inv_shiftrows`、`inv_mixcolumns`、`addroundkey`、`inv_sbox_inst`）讲透了。本节只从**控制层**再看一遍：哪个 `update_type` 对应解密的哪一段、读的是输入端口还是状态寄存器、用哪把 round key。

四种 `update_type` 编码与加密侧同名同值（[rtl/aes_decipher_block.v:35-39](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L35-L39)）：

| update_type | 值 | 解密中写回的运算 | 对应阶段 | 用到的 round key | 断言的 `_we` |
|-------------|----|------------------|----------|------------------|--------------|
| `INIT_UPDATE` | 3'h1 | `inv_shiftrows(block ^ rkey)` | 初始轮（异或最后一把 + 逆行移位） | \(K_N\)（`round_ctr_reg=N`） | 4 个全开 |
| `SBOX_UPDATE` | 3'h2 | `{new_sboxw ×4}`（只写选中字） | InvSubBytes | 不用 key | 仅 1 个 |
| `MAIN_UPDATE` | 3'h3 | `inv_shiftrows(inv_mixcolumns(old ^ rkey))` | 主轮（异或 → 逆列混淆 → 逆行移位） | \(K_k\)（`round_ctr_reg=k`） | 4 个全开 |
| `FINAL_UPDATE` | 3'h4 | `old ^ rkey` | 最终轮（只异或第 0 把） | \(K_0\)（`round_ctr_reg=0`） | 4 个全开 |
| `NO_UPDATE` | 3'h0 | 不写回 | 空闲 | — | 全关 |

注意上表中「用到的 round key」一列——它把 `update_type` 与 `round_ctr_reg` 的取值**绑定**了起来。这是本讲的枢纽：解密能正确工作，前提是 `INIT_UPDATE` 那拍 `round_ctr_reg` 恰好是 N、`MAIN_UPDATE` 那拍恰好是 k、`FINAL_UPDATE` 那拍恰好是 0。这个保证由 4.2 的递减计数器和 4.3 的 FSM 共同提供。

#### 4.1.2 核心流程

`round_logic` 的写法与加密侧有一个**风格差异**值得先点破：加密侧（u2-l5 §4.1）是「**先全算、再选一个**」——把初始/主/最终三种候选结果在 `case` 之前一次性算好待选；解密侧则是「**用到才算**」——在每个 `case` 分支内部才现算对应的那一种。两种写法都正确，解密侧这种写法只是少算了当前用不上的候选（面积上略省，可读性见仁见智）。

解密侧伪代码（对应 [rtl/aes_decipher_block.v:270-358](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L270-L358)）：

```
old_block = {block_w0_reg, block_w1_reg, block_w2_reg, block_w3_reg}  // 状态寄存器（默认）

case (update_type)
  INIT_UPDATE :
      old_block = block                                   // ← 注意：改读输入端口（密文）
      block_new = inv_shiftrows(addroundkey(old_block, round_key));  开 w0..w3_we
  SBOX_UPDATE :
      block_new = {new_sboxw ×4};  按 sword_ctr 只开 1 个 _we          // 逐字 InvSubBytes
  MAIN_UPDATE :
      block_new = inv_shiftrows(inv_mixcolumns(addroundkey(old_block, round_key)));  开 w0..w3_we
  FINAL_UPDATE:
      block_new = addroundkey(old_block, round_key);      开 w0..w3_we   // 只异或 K0
endcase
```

两个**极易看走眼的细节**（都和加密侧同构，u2-l5/u2-l6 已分别强调过，这里合并复述）：

1. **只有 `INIT_UPDATE` 读输入端口 `block`，其余分支读状态寄存器 `old_block`。** 原因：密文是第一次进入模块、尚未被锁存；`INIT_UPDATE` 那拍把它（异或 \(K_N\) 后）锁进 4 个字寄存器，之后所有轮都只看寄存器。
2. **运算顺序是 `AddRoundKey → InvMixColumns → InvShiftRows`（`MAIN_UPDATE`），不是标准逆密码的 `InvShiftRows → InvSubBytes → ...`。** 这是「等价逆密码」的实现选择（u2-l6 §4.4）——把 `InvShiftRows` 挪到一轮的尾部、`InvSubBytes` 放在下一轮的头部（即 `SBOX_UPDATE`），数学上与 FIPS 197 标准逆密码完全等价，好处是能用和加密一模一样的 `round_logic` 框架。

#### 4.1.3 源码精读

`INIT_UPDATE` 分支（[rtl/aes_decipher_block.v:290-300](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L290-L300)）：

```verilog
INIT_UPDATE:
  begin
    old_block           = block;                              // 改读输入端口（密文）
    addkey_block        = addroundkey(old_block, round_key);  // 此时 round_key = K_N
    inv_shiftrows_block = inv_shiftrows(addkey_block);
    block_new           = inv_shiftrows_block;
    block_w0_we = 1'b1; ... block_w3_we = 1'b1;               // 4 个字全部写回
  end
```

> 中文说明：解密的「初始轮」把**密文**异或上最后一把 round key（\(K_N\)，由 4.2 保证此刻 `round_ctr_reg=N`），再做一次逆行移位，然后一次性锁进 4 个字寄存器。从此密文「住进」了寄存器。

`MAIN_UPDATE` 分支（[rtl/aes_decipher_block.v:333-343](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L333-L343)）：

```verilog
MAIN_UPDATE:
  begin
    addkey_block         = addroundkey(old_block, round_key);          // 此时 round_key = K_k
    inv_mixcolumns_block = inv_mixcolumns(addkey_block);
    inv_shiftrows_block  = inv_shiftrows(inv_mixcolumns_block);
    block_new            = inv_shiftrows_block;
    block_w0_we = 1'b1; ... block_w3_we = 1'b1;
  end
```

> 中文说明：解密主轮的「尾巴」按 `AddRoundKey → InvMixColumns → InvShiftRows` 顺序处理状态寄存器，用的是当前 `round_ctr_reg=k` 对应的 \(K_k\)。对比加密侧 `MAIN_UPDATE` 的 `MC(SR(state)) ^ rk`（u2-l5），可见两侧结构对称、方向相反。

`FINAL_UPDATE` 分支（[rtl/aes_decipher_block.v:345-352](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L345-L352)）只有一句 `block_new = addroundkey(old_block, round_key)`——对应加密最终轮省去 MixColumns，解密最终轮省去 InvMixColumns，只剩与 \(K_0\) 的异或。

`SBOX_UPDATE` 分支（[rtl/aes_decipher_block.v:302-331](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L302-L331)）和加密侧的 `SBOX_UPDATE`（u2-l5 §4.3）几乎逐行相同——都是「广播 `new_sboxw` + 用 `sword_ctr` 选一个字写回」，唯一的差别是：加密侧送出去查的是**正向共享 S-box**，解密侧送出去查的是**本模块私挂的 `inv_sbox_inst`**。解密侧的字计数器 `sword_ctr`（[L366-381](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L366-L381)）与加密侧完全同构（默认不写、`_rst` 写 0、`_inc` 写 +1），故本讲不再为它单开一节（详见 u2-l5 §4.3）。

#### 4.1.4 代码实践

**目标**：确认「`update_type` 与 `round_ctr_reg` 的绑定关系」——即每种写回操作当拍应当出现哪个轮号。

**步骤**：

1. 打开 [round_logic 的四个分支](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L288-L357)。
2. 对照 4.1.1 的表格，自问：若 `INIT_UPDATE` 那拍 `round_ctr_reg` 不是 N 而是 0，会发生什么？
3. **答案**：`round_key` 会变成 \(K_0\) 而不是 \(K_N\)，密文先和错误的密钥异或，后续整个解密全错。这说明「递减计数器必须在 INIT 那拍正好等于 N」是正确性的硬约束——它由 4.2、4.3 保证。

**需要观察的现象 / 预期结果**：你能用一句话讲清「`round_logic` 本身不决定用哪把 key，它只是被动地用 `round` 端口当前选中的那把」。这是一条**待本地验证**的理解性练习（靠阅读即可得出结论）。

#### 4.1.5 小练习与答案

**练习 1**：解密侧的 `round_logic` 为什么不在 `case` 之前把所有候选结果都算好（像加密侧那样），而是每个分支现算？

**参考答案**：两种写法都正确，纯属风格选择。解密侧「用到才算」省下了当前用不上的候选运算（例如做 `FINAL_UPDATE` 时不必算 `inv_mixcolumns`），在面积上略微占优；代价是可读性不如加密侧「先全算、再选一个」那么整齐。功能上二者完全等价。

**练习 2**：`SBOX_UPDATE` 这一拍会用 round key 吗？为什么？

**参考答案**：不会。`SBOX_UPDATE` 只做 InvSubBytes（查逆 S-box），是纯字节替换，不涉及轮密钥；所以表里「用到的 round key」一栏对它是「不用 key」。这也意味着该拍的 `round_ctr_reg` 取什么值**不影响正确性**——这是 4.3 里「把 `round_ctr_dec` 放在 SBOX 末拍」能够成立的前提。

---

### 4.2 递减 round_ctr：从 num_rounds 倒数到 0

这是本讲的核心模块，也是解密 FSM 与加密 FSM 最本质的区别所在。

#### 4.2.1 概念说明

`round_ctr` 仍是一个 **4 位**寄存器（[rtl/aes_decipher_block.v:169-173](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L169-L173)），但它的一对控制脉冲和加密侧**方向相反**：

| | 加密侧（u2-l5） | 解密侧（本讲） |
|---|---|---|
| 起始动作 | `round_ctr_rst`（清 0） | `round_ctr_set`（置 N） |
| 推进动作 | `round_ctr_inc`（+1） | `round_ctr_dec`（−1） |

它的两个对外作用和加密侧**完全相同**：

1. 通过 `assign round = round_ctr_reg`（[L211](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L211)）告诉 `key_mem`「给我第几把 round key」。`key_mem` 是组合读，round_key **当拍就有效**。
2. 在 `CTRL_MAIN` 状态里和一个阈值比较，决定这一轮是「主轮（MAIN_UPDATE）」还是「最终轮（FINAL_UPDATE）」——只不过加密侧比较的是 `round_ctr_reg < num_rounds`（向上到头），解密侧比较的是 `round_ctr_reg > 0`（向下到头）。

**为什么解密要递减？** 一句话：**因为解密按倒序使用轮密钥。** AES 解密先用 \(K_N\)、最后用 \(K_0\)；而 `key_mem` 把 \(K_0..K_N\) 都存好了、按地址 `round` 组合读出。于是最省事的做法就是让 `round_ctr` 从 N 一路减到 0——这样同一句 `assign round = round_ctr_reg`、同一个 `key_mem` 读端口，天然就按 \(K_N, K_{N-1}, \dots, K_0\) 的顺序吐出轮密钥，**完全不需要额外的「反向寻址」逻辑**。这正是加解密能共用同一份 `key_mem` 和同一套轮密钥寻址接口的关键。

> 设计直觉：加密「顺着」用 key（0→N），所以计数器递增；解密「逆着」用 key（N→0），所以计数器递减。计数方向 = 轮密钥使用方向。两者共用 `round_ctr_reg → round → key_mem[round]` 这条数据通路，只是数数的方向不同。

#### 4.2.2 核心流程

`round_ctr` 的组合块（[rtl/aes_decipher_block.v:389-411](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L389-L411)）只有两种动作：**置位**（`round_ctr_set` → N）或**自减**（`round_ctr_dec` → −1）。它本身不做决定，**何时置位、何时自减完全由 FSM `decipher_ctrl` 控制**。注意这里「置成几」是由 `keylen` 在本块内部决定的（AES-256 置 14、否则置 10），所以 `decipher_ctrl` 里**没有**加密侧那个 `num_rounds` 局部变量——这是两侧 FSM 的一个结构差异（见 4.3）。

整个解密过程中 `round_ctr_reg` 的关键节拍（以 AES-128，N=10 为例）：

- `next` 到来那拍，在 `CTRL_IDLE` 里被 `round_ctr_set` 一次性置成 **10**。
- `CTRL_INIT`（初始轮）那拍 `round_ctr_reg = 10`，所以用到 **\(K_{10}\)**；该拍**不自减**。
- 进入第一段 InvSubBytes：4 个 `CTRL_SBOX` 拍期间 `round_ctr_reg` 仍为 10；在第 4 拍（`sword_ctr_reg==3`）末 `round_ctr_dec` 把它减到 **9**。
- 紧随的 `CTRL_MAIN` 看到 `round_ctr_reg = 9`，用 **\(K_9\)** 做 `MAIN_UPDATE`；该拍也不自减。
- 之后每经过一轮，`CTRL_SBOX` 的第 4 拍把 `round_ctr_reg` −1：9→8→…→1。
- 当 `round_ctr_reg` 减到 **0**，`CTRL_MAIN` 改发 `FINAL_UPDATE`，用 **\(K_0\)**，并回到 IDLE。

于是 round key 的使用序列是 \(K_{10}, K_9, K_8, \dots, K_1, K_0\)——正好是加密序列 \(K_0, K_1, \dots, K_{10}\) 的逆序，与 AES 解密的要求严格吻合。

> 一句话：第 \(k\) 把 round key 在 `round_ctr_reg == k` 的那一拍被 `round_logic` 取用（INIT 取 \(k=N\)、MAIN 取 \(k=N{-}1..1\)、FINAL 取 \(k=0\)）。轮密钥与轮号严格对齐，方向倒过来而已。

#### 4.2.3 源码精读

计数器寄存器声明（[rtl/aes_decipher_block.v:169-173](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L169-L173)）：

```verilog
reg [3 : 0]   round_ctr_reg;
reg [3 : 0]   round_ctr_new;
reg           round_ctr_we;
reg           round_ctr_set;   // ← 加密侧这里是 round_ctr_rst
reg           round_ctr_dec;   // ← 加密侧这里是 round_ctr_inc
```

> 中文说明：典型的 `reg/_new/_we` 三件套，外加 `_set`/`_dec` 两个「控制脉冲」输入——对比加密侧的 `_rst`/`_inc`，只换了方向，结构完全对称。

组合计数逻辑（[rtl/aes_decipher_block.v:389-411](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L389-L411)）：

```verilog
always @*
  begin : round_ctr
    round_ctr_new = 4'h0;
    round_ctr_we  = 1'b0;
    if (round_ctr_set)                       // 置位：按 keylen 选 N
      begin
        if (keylen == AES_256_BIT_KEY)
          round_ctr_new = AES256_ROUNDS;     // 14
        else
          round_ctr_new = AES128_ROUNDS;     // 10
        round_ctr_we  = 1'b1;
      end
    else if (round_ctr_dec)                  // 自减：当前值 -1
      begin
        round_ctr_new = round_ctr_reg - 1'b1;
        round_ctr_we  = 1'b1;
      end
  end
```

> 中文说明：默认不写；收到 `round_ctr_set` 就写 N（N 由 `keylen` 定），收到 `round_ctr_dec` 就写「当前值 −1」。置位优先于自减。对比加密侧 [rtl/aes_encipher_block.v:345-360](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L345-L360) 的 `_rst`→0 / `_inc`→+1，就是把「0」换成「N」、「+1」换成「−1」。

FSM 里决定主轮还是最终轮的比较（[rtl/aes_decipher_block.v:464-481](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L464-L481)）：

```verilog
CTRL_MAIN:
  begin
    sword_ctr_rst = 1'b1;
    if (round_ctr_reg > 0)                    // 还没减到 0
      begin update_type = MAIN_UPDATE;  dec_ctrl_new = CTRL_SBOX;  ... end
    else                                      // 已减到 0
      begin update_type = FINAL_UPDATE; ready_new = 1'b1; ... dec_ctrl_new = CTRL_IDLE; ... end
  end
```

> 中文说明：`CTRL_MAIN` 按 `round_ctr_reg > 0` 二选一——还没到底就做主轮回 `CTRL_SBOX` 开下一轮；到底（=0）就做最终轮、拉高 `ready`、回 IDLE。对比加密侧 [rtl/aes_encipher_block.v:425-443](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L425-L443) 的 `round_ctr_reg < num_rounds`，两者是镜像：一个「向上碰到上界停」，一个「向下碰到 0 停」。

#### 4.2.4 代码实践

**目标**：在源码里把 `round_ctr` 的「动」与「不动」全部找出来，并与加密侧对比「推进点」的数量。

**步骤**：

1. 在 [decipher_ctrl 块](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L419-L488)里搜索 `round_ctr_set` 和 `round_ctr_dec` 的所有出现位置。
2. 应当发现：`round_ctr_set` 只在 `CTRL_IDLE`（`next` 到来时）出现一次；`round_ctr_dec` **只在 `CTRL_SBOX` 且 `sword_ctr_reg == 2'h3`** 那拍出现一次；`CTRL_INIT`、`CTRL_MAIN` 里**都没有**任何 `round_ctr` 动作。
3. 对照加密侧 [encipher_ctrl 块](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L368-L450)：`round_ctr_inc` 出现在 `CTRL_INIT` **和** `CTRL_MAIN` 两处（u2-l5 §4.2）。

**需要观察的现象 / 预期结果**：解密的计数器推进点只有 **1 处**（SBOX 末拍），加密却有 **2 处**（INIT、MAIN）。这个「1 处 vs 2 处」的不对称是本讲的一个关键点，原因见 4.3.2。该结论可由静态阅读直接得出，**待本地验证**的是你在仿真波形里看到解密时 `round` 信号从 10 逐拍降到 0（而加密是从 0 升到 10）。

#### 4.2.5 小练习与答案

**练习 1**：`round_ctr` 是 4 位。AES-256 解密置位到 14，之后一路减到 0；这个过程中它会下溢吗？

**参考答案**：不会。`CTRL_MAIN` 在 `round_ctr_reg == 0` 时就改发 `FINAL_UPDATE` 并回 IDLE 了，不会再发 `round_ctr_dec`；所以 0 就是终点，不会出现 0−1 的下溢。下一次 `next` 会把它重新 `set` 到 N。

**练习 2**：如果有人误把解密侧的 `round_ctr_set` 改成 `round_ctr_rst`（清 0，像加密那样），解密会怎样？

**参考答案**：`INIT_UPDATE` 那拍 `round_ctr_reg` 会变成 0 而不是 N，于是初始轮用的不是 \(K_N\) 而是 \(K_0\)；之后 `CTRL_MAIN` 又因为 `0 > 0` 为假**立刻**走 `FINAL_UPDATE` 结束——解密几乎没有真正处理就输出了，结果完全错误。这反过来说明「置位到 N」是解密正确性的硬前提。

**练习 3**：解密用了几把 round key？顺序是什么？和加密比呢？

**参考答案**：同样用 \(N+1\) 把（AES-128 是 11 把、AES-256 是 15 把）。解密顺序是 \(K_N \to K_{N-1} \to \dots \to K_0\)（降序），正好是加密顺序 \(K_0 \to K_1 \to \dots \to K_N\)（升序）的逆。两侧访问的是**同一份** `key_mem`，只是 `round` 端口的数数方向相反。

---

### 4.3 decipher_ctrl：解密总控状态机

#### 4.3.1 概念说明

`decipher_ctrl` 是本模块的「总指挥」（[rtl/aes_decipher_block.v:419-488](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L419-L488)）。它本身**不做任何运算**，只做调度：根据当前状态，去拨动 `round_ctr` 的 `_set`/`_dec`、`sword_ctr` 的 `_rst`/`_inc`、给 `round_logic` 喂 `update_type`、并决定下一状态。它把 4.1 的逆运算、4.2 的递减轮计数、逐字 InvSubBytes 的字计数捏成一条按时钟推进的流程。

**先复述一条和加密侧一模一样的「易混淆点」（以源码为准）**：`localparam CTRL_FINAL` 虽然在 [rtl/aes_decipher_block.v:45](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L45) 被定义了，但**它从未作为 `case(dec_ctrl_reg)` 的分支出现**——整个 FSM 实际只有 **4 个状态**：`CTRL_IDLE`、`CTRL_INIT`、`CTRL_SBOX`、`CTRL_MAIN`（外加 `default` 空分支让综合工具闭嘴）。「最终轮」不是一个独立状态，而是 `CTRL_MAIN` 在 `round_ctr_reg == 0` 时改发 `FINAL_UPDATE` 的那条 else 分支。`CTRL_FINAL` 是一段**死代码**（和 u2-l5 指出的加密侧 `CTRL_FINAL` 同类现象）。记住：状态机有几个状态，要数 `case` 的分支，而不是数 `localparam`。

#### 4.3.2 核心流程

状态转移图（文字版，与 u2-l5 的加密版对照着看）：

```
        next=1 (round_ctr_set→N, ready↓)
 IDLE ──────────────────────────────► INIT
  ▲                                    │ INIT_UPDATE(密文^K_N → inv_shiftrows)，sword_ctr←0
  │                                    │ （注意：本拍 round_ctr 不动）
  │   round_ctr_reg==0:                ▼
  │   FINAL_UPDATE(state^K_0),  ────────► SBOX  ←──────┐
  │   ready↑                          │   │  sword_ctr_inc ×4
  │                                    │   │  （每拍 InvSubBytes 1 个字；
  │                                    │   │   第 4 拍 sword==3 时
  └────────────────────────────────── MAIN     round_ctr_dec：k→k-1）
                                       │  round_ctr_reg > 0:
                                       │  MAIN_UPDATE(ARK→invMC→invSR)，
                                       │  sword_ctr←0 → 回 SBOX
```

各状态职责一览（与加密侧并排列出，差异用粗体标出）：

| 状态 | 解密 `decipher_ctrl` 的动作 | 加密 `encipher_ctrl` 的动作（对照） | 下一状态 |
|------|----------------------------|-------------------------------------|---------|
| `CTRL_IDLE` | 等 `next`；**`round_ctr_set`**（置 N）、`ready↓` | `round_ctr_rst`（清 0）、`ready↓` | →`INIT` |
| `CTRL_INIT` | `INIT_UPDATE`、`sword_ctr_rst`；**round_ctr 不动** | `INIT_UPDATE`、`sword_ctr_rst`、**`round_ctr_inc`**(0→1) | →`SBOX` |
| `CTRL_SBOX` | `SBOX_UPDATE`、`sword_ctr_inc`；**`sword==3` 时 `round_ctr_dec`** 并转走 | `SBOX_UPDATE`、`sword_ctr_inc`；`sword==3` 时转走（**不动 round_ctr**） | →`MAIN`（sword==3） |
| `CTRL_MAIN` | `sword_ctr_rst`；`round_ctr>0` 发 `MAIN_UPDATE` 否则发 `FINAL_UPDATE`+`ready↑` | `sword_ctr_rst`、**`round_ctr_inc`**；`round_ctr<num_rounds` 发 `MAIN_UPDATE` 否则 `FINAL_UPDATE`+`ready↑` | →`SBOX` 或 →`IDLE` |

**关键不对称：计数器推进点的位置不同。** 这正是 4.2.4 留下的问题，值得单独讲清楚：

- **加密**：`round_ctr_inc` 出现在 `CTRL_INIT` 和 `CTRL_MAIN` 两处。直觉是「**用完一把 key 就 +1，为下一轮准备**」——`INIT` 用 \(K_0\) 后 +1→1，于是后面第一段 SBOX 和第一个 `MAIN` 都看到 `round_ctr=1`（用 \(K_1\)）；每个 `MAIN` 用 \(K_k\) 后 +1→k+1，为下一段准备。
- **解密**：`round_ctr_dec` 只出现在 `CTRL_SBOX` 的末拍（`sword==3`）。直觉是「**在两轮 key 之间的间隙里 −1**」——`INIT` 用 \(K_N\) 后**不立即减**（因为后面那段 SBOX 不用 key，`round_ctr` 等于几都无所谓，4.1.5 已说明）；等到那段 SBOX 走完、即将进入下一个 `MAIN` 之前的最后一拍，才把 N→N−1，于是下一个 `MAIN` 看到 `round_ctr=N−1`（用 \(K_{N-1}\)）。后续每个 `MAIN` 用 \(K_k\) 后同样不立即减，而是等下一段 SBOX 末拍再 k→k−1。

为什么解密不能像加密那样把推进动作放在 `MAIN`？因为 `MAIN_UPDATE` 需要 \(K_k\)，而此刻 `round_ctr` 必须等于 k；如果把 `dec` 也放在 `MAIN`（用 key 之前/同时），那 `MAIN` 看到的就是 k−1 而非 k，密钥错位。**`dec` 必须发生在「用 \(K_k\) 的 MAIN」之后、「用 \(K_{k-1}\) 的下一个 MAIN」之前**——而夹在这两个 MAIN 之间的，正好是一整段 SBOX，所以把 `dec` 放在 SBOX 的末拍是最自然的位置。加密侧没有这个约束，是因为它「先 +1 再用 key」也成立（`INIT`/`MAIN` 先把 `round_ctr` 推到 k，随后同一段 SBOX+MAIN 在 `round_ctr=k` 下用 \(K_k\)）。

> 一句话总结这个不对称：加密是「**先把计数器推到本轮号，再做本轮**」（inc 在 INIT/MAIN，即一轮的开头）；解密是「**先做本轮，再把计数器推到下一轮号**」（dec 在 SBOX 末拍，即一轮的结尾）。殊途同归——两种写法都保证了 `round_ctr_reg` 在每次 `INIT/MAIN/FINAL_UPDATE` 时恰好等于正在使用的那把 key 的下标。

#### 4.3.3 源码精读

FSM 默认赋值（[rtl/aes_decipher_block.v:419-430](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L419-L430)）：开头先把所有控制脉冲和 `update_type`、`dec_ctrl_new` 全置成「不变/空」的默认值（避免锁存器，u1-l3 强调的组合块习惯）。注意这里**没有**加密侧那个 `num_rounds` 局部变量与 `if (keylen...)` 选择块——因为「N 是几」已经在 `round_ctr` 块内部按 `keylen` 决定了（4.2.3），FSM 只需发 `round_ctr_set` 脉冲即可。

`CTRL_IDLE`（[rtl/aes_decipher_block.v:432-442](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L432-L442)）：检测到 `next` 就 **`round_ctr_set`**（置 N）、拉低 `ready`、跳 `INIT`。对比加密侧的 `round_ctr_rst`，只差「置 N vs 清 0」。

`CTRL_INIT`（[rtl/aes_decipher_block.v:444-450](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L444-L450)）：

```verilog
CTRL_INIT:
  begin
    sword_ctr_rst = 1'b1;        // InvSubBytes 字计数清 0
    update_type   = INIT_UPDATE;
    dec_ctrl_new  = CTRL_SBOX;
    dec_ctrl_we   = 1'b1;
    // 注意：没有任何 round_ctr 动作！（加密侧这里有 round_ctr_inc）
  end
```

> 中文说明：这一拍用 `round_ctr_reg=N` 对应的 \(K_N\) 做初始的「异或 + 逆行移位」（由 `round_logic` 的 INIT_UPDATE 完成），字计数清 0，下一拍进入 InvSubBytes。**关键：本拍不动 `round_ctr`**——它保持 N，让紧随的 4 拍 SBOX 也在 `round_ctr=N` 下进行（虽然 SBOX 不用 key，无妨）。

`CTRL_SBOX`（[rtl/aes_decipher_block.v:452-462](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L452-L462)）：

```verilog
CTRL_SBOX:
  begin
    sword_ctr_inc = 1'b1;
    update_type   = SBOX_UPDATE;
    if (sword_ctr_reg == 2'h3)        // 第 4 个字替换完
      begin
        round_ctr_dec = 1'b1;          // ← 计数器推进点：k → k-1
        dec_ctrl_new  = CTRL_MAIN;
        dec_ctrl_we   = 1'b1;
      end
  end
```

> 中文说明：每拍替换 1 个字；当 `sword_ctr_reg == 2'h3`（替换完第 4 个字）那拍，**才**发 `round_ctr_dec` 把轮号 −1，并跳 `CTRL_MAIN`。对比加密侧 `CTRL_SBOX`（[rtl/aes_encipher_block.v:414-423](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L414-L423)）转走时**不动** `round_ctr`——这是两侧 FSM 最实质的一行差别。

`CTRL_MAIN`（[rtl/aes_decipher_block.v:464-481](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L464-L481)）：见 4.2.3 已引用，按 `round_ctr_reg > 0` 二选一——还没到底就做主轮回 SBOX，到底就做最终轮、拉高 ready 回 IDLE。对比加密侧 `CTRL_MAIN` 多了一句 `round_ctr_inc = 1'b1`，解密侧这里**没有**任何 `round_ctr` 动作。

所有状态转移都用 `dec_ctrl_new`/`dec_ctrl_we`，最终由 [reg_update 时序块](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L223-L262)在时钟沿搬进 `dec_ctrl_reg`——标准的「组合算下一状态 + 时序搬状态」两段式 FSM（u1-l3）。

#### 4.3.4 代码实践

**目标**：给定状态与输入，预测下一状态与 `update_type`、以及 `round_ctr` 是否会被改动。

**步骤**：对下面 4 个场景（AES-128，N=10），分别写出 `dec_ctrl_new`、`update_type`、以及该拍末 `round_ctr_reg` 会变成什么，然后对照源码核对。

1. `dec_ctrl_reg = CTRL_IDLE`，`next = 1`。
2. `dec_ctrl_reg = CTRL_INIT`，当前 `round_ctr_reg = 10`。
3. `dec_ctrl_reg = CTRL_SBOX`，`sword_ctr_reg = 2'h3`，当前 `round_ctr_reg = 10`。
4. `dec_ctrl_reg = CTRL_MAIN`，`round_ctr_reg = 4'd0`。

**预期结果**：

1. → `CTRL_INIT` / `NO_UPDATE`；拍末 `round_ctr_reg` 被置成 **10**。
2. → `CTRL_SBOX` / `INIT_UPDATE`（用 \(K_{10}\)）；拍末 `round_ctr_reg` **仍为 10**（INIT 不动它）。
3. → `CTRL_MAIN` / `SBOX_UPDATE`；拍末 `round_ctr_reg` **减成 9**（这是唯一的推进点）。
4. → `CTRL_IDLE` / `FINAL_UPDATE`（用 \(K_0\)）且 `ready↑`；拍末 `round_ctr_reg` **仍为 0**。

这是纯阅读题，**待本地验证**的是第 4 个场景正是 AES-128 解密收尾的那一拍。

#### 4.3.5 小练习与答案

**练习 1**：本 FSM 真正使用到的状态有几个？`CTRL_FINAL` 是什么？

**参考答案**：真正用到 4 个——`IDLE`/`INIT`/`SBOX`/`MAIN`。`CTRL_FINAL` 只是被 `localparam` 定义但从未出现在 `case` 分支里的死代码；「最终轮」由 `CTRL_MAIN` 的 else 分支用 `FINAL_UPDATE` 实现。这一点和加密侧 `encipher_ctrl` 完全相同。

**练习 2**：为什么 `decipher_ctrl` 里没有加密侧那个 `num_rounds` 局部变量？

**参考答案**：因为解密的「N 是几」不需要在 FSM 里参与比较——FSM 的判据是 `round_ctr_reg > 0`（与 N 的具体值无关）；而「置位到 N」的 N 值是在 `round_ctr` 块内部按 `keylen` 选定的（4.2.3）。所以 FSM 不必再算一遍 `num_rounds`，结构上比加密侧少一个局部变量。

**练习 3**：FSM 在什么条件下回到 `CTRL_IDLE`？回到 IDLE 时 `ready` 是什么？

**参考答案**：仅在 `CTRL_MAIN` 且 `round_ctr_reg == 0` 时回到 IDLE，同时 `ready_new = 1'b1`（拉高，表示解密完成、明文结果有效）。这一点也和加密侧一致——完成即拉高 `ready`、回 IDLE 等下一次 `next`。

---

## 5. 综合实践：加密递增 vs 解密递减——对比表与时钟轨迹

**任务**：把 `encipher_ctrl`（u2-l5）和 `decipher_ctrl`（本讲）放在一起，写一份「加密递增、解密递减」的对比表，解释为什么方向相反，并给出解密一次完整处理的时钟轨迹与周期数。这是本讲三个最小模块（4.1/4.2/4.3）的汇合点，也是本讲指定的代码实践任务。

### 5.1 对比表（加密递增、解密递减）

| 对比维度 | `encipher_ctrl`（加密，u2-l5） | `decipher_ctrl`（解密，本讲） |
|----------|--------------------------------|-------------------------------|
| **计数方向** | **递增** \(0 \to N\) | **递减** \(N \to 0\) |
| 起始动作（@IDLE） | `round_ctr_rst`（清 0） | `round_ctr_set`（置 N，N 由 keylen 定） |
| 推进动作 | `round_ctr_inc`（+1） | `round_ctr_dec`（−1） |
| **推进点位置** | **2 处**：`CTRL_INIT`、`CTRL_MAIN` | **1 处**：`CTRL_SBOX`（`sword==3` 末拍） |
| 完成判据（@MAIN） | `round_ctr_reg < num_rounds`（碰到上界 N 转 FINAL） | `round_ctr_reg > 0`（碰到下界 0 转 FINAL） |
| **轮密钥使用顺序** | \(K_0 \to K_1 \to \dots \to K_N\)（升序） | \(K_N \to K_{N-1} \to \dots \to K_0\)（降序） |
| `round` 端口 | `assign round = round_ctr_reg`（升序地址） | **同一句** `assign round = round_ctr_reg`（降序地址） |
| `INIT_UPDATE` 数据源 | 输入端口 `block`（明文） | 输入端口 `block`（密文） |
| SubBytes 用的查表 | 经端口的**正向共享 S-box** | 模块内**私挂的 `inv_sbox_inst`** |
| `num_rounds` 局部变量 | 有（FSM 内按 keylen 选） | 无（N 在 `round_ctr` 块内按 keylen 选） |
| `CTRL_FINAL` 状态 | 死代码，未被 case 引用 | 死代码，未被 case 引用 |
| 实际状态数 | 4（IDLE/INIT/SBOX/MAIN） | 4（IDLE/INIT/SBOX/MAIN） |
| 一次处理周期（AES-128 / 256） | 51 / 71 拍 | **51 / 71 拍（相同）** |

**为什么方向相反？** 因为 AES 解密按**倒序**使用轮密钥（先用 \(K_N\)、最后用 \(K_0\)）。工程让加解密共用同一份 `key_mem` 和同一句 `assign round = round_ctr_reg` 的组合读地址；要让它倒序吐 key，最省事的做法就是让计数器从 N 减到 0——计数方向 = 轮密钥使用方向。这就是「加密递增、解密递减」的根本原因。

**为什么周期数相同？** 因为两个 FSM 的「形状」一样：都是 `1 拍 INIT + N 段 ×（4 拍 SBOX + 1 拍 MAIN）`，最终轮只是把最后那个 MAIN 换成 FINAL。数数方向（升/降）只改变每拍用哪把 key，不改变拍数。

### 5.2 解密时钟轨迹（worked example，AES-128，N=10）

下表给出关键拍（每段 InvSubBytes 只列第 1 拍和 MAIN 拍，中间 3 拍形式相同）。\(K_k\) 表示第 k 把 round key；「↑/↓」表示该拍末 `ready` 被置高/低。设第 1 拍为 `CTRL_IDLE` 且 `next` 到来那一拍。

| 拍号 | dec_ctrl_reg | round_ctr_reg | sword_ctr_reg | update_type | round key | 该拍做的事（拍末效果） |
|----|----|----|----|----|----|----|
| 1 | IDLE | (set)→10 | 0 | NO_UPDATE | — | 收到 next，ready↓，置 round_ctr=10 |
| 2 | INIT | 10 | (rst)→0 | INIT_UPDATE | \(K_{10}\) | 密文 ^ \(K_{10}\) → inv_shiftrows，锁入寄存器 |
| 3 | SBOX | 10 | 0→1 | SBOX_UPDATE | — | InvSubBytes 字0 |
| 4 | SBOX | 10 | 1→2 | SBOX_UPDATE | — | InvSubBytes 字1 |
| 5 | SBOX | 10 | 2→3 | SBOX_UPDATE | — | InvSubBytes 字2 |
| 6 | SBOX | 10 | 3→0 | SBOX_UPDATE | — | InvSubBytes 字3，**dec 10→9**，转 MAIN |
| 7 | MAIN | 9 | (rst)→0 | MAIN_UPDATE | \(K_9\) | 第9轮 ARK→invMC→invSR |
| 8–11 | SBOX | 9 | 0→…→0 | SBOX_UPDATE | — | InvSubBytes（4 拍），末拍 dec 9→8 |
| 12 | MAIN | 8 | 0 | MAIN_UPDATE | \(K_8\) | 第8轮 ARK→invMC→invSR |
| … | … | … | … | … | … | 每段 5 拍：4 SBOX + 1 MAIN，round_ctr 逐段 −1 |
| … | MAIN | 1 | 0 | MAIN_UPDATE | \(K_1\) | 第1轮 ARK→invMC→invSR |
| … | SBOX×4 | 1 | 0→…→0 | SBOX_UPDATE | — | InvSubBytes（4 拍），末拍 dec 1→0 |
| 52 | MAIN | 0 | 0 | FINAL_UPDATE | \(K_0\) | 最终轮 state ^ \(K_0\)，**ready↑**，回 IDLE |
| 53 | IDLE | 0 | 0 | NO_UPDATE | — | 空闲，等待下一次 next |

### 5.3 周期数统计

数「活跃处理拍」：1 拍 `INIT` + N 段 ×（4 拍 SBOX + 1 拍 MAIN）：

\[
\text{cycles}_{\text{AES-128}} = 1 + 10 \times (4 + 1) = 1 + 50 = 51
\]

即 **`aes_decipher_block` 自身完成一次 AES-128 解密需要 51 个时钟周期**（从 `next` 之后的 INIT 拍算起，到 `ready` 重新拉高）。这与加密侧的 51 拍（u2-l5 §5.3）**完全相等**。对 AES-256（N=14）：

\[
\text{cycles}_{\text{AES-256}} = 1 + 14 \times 5 = 71
\]

同样与加密侧相等。

**需要观察的现象 / 预期结果**：解密时 `round_ctr_reg`（即 `round` 端口）从 10 **逐段**降到 0（每段 4 拍 SBOX 里保持不变，只在末拍 −1）；`sword_ctr_reg` 在每段 SBOX 里重复 0→1→2→3→(0)。可在仿真里把这三个寄存器加入波形（用 `dump_dut_state` 或层次化引用 `dut.aes_core.dec_block.dec_ctrl_reg` / `round_ctr_reg` / `sword_ctr_reg`，见 u1-l5）逐一核对——**待本地验证**，因为本讲不假设你已经跑过仿真。

> 提示：这里数的是 decipher_block 模块**自身**的耗时。`aes_core` 的 `CTRL_NEXT` 还要再花 1 拍去采样 `dec_ready` 并回 IDLE，所以从主机角度看端到端会多 1 拍，那属于 [u3-l1 端到端追踪](u3-l1-end-to-end-encryption-trace.md) 的范畴，本讲不计入。另请注意：解密的「init 阶段」（密钥扩展）发生在 `aes_key_mem`、与本模块的「next 阶段」分离（u2-l3），不要把二者混淆。

## 6. 本讲小结

- `round_logic`（[L270-358](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L270-L358)）是纯组合「数据层」，按 `update_type`（INIT/SBOX/MAIN/FINAL）挑选对应逆运算写回；与加密侧相比是「用到才算」而非「先全算」，且**初始轮读输入端口 `block`（密文）、其余轮读状态寄存器 `old_block`**。
- `round_ctr`（4 位）是**递减**轮计数器：`round_ctr_set` 一次性置成 N（AES-128 为 10、AES-256 为 14，由 `keylen` 在 [L389-411](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L389-L411) 内选定），之后 `round_ctr_dec` 每轮 −1 直到 0。递减的根本原因是**解密按倒序使用轮密钥** \(K_N \to K_0\)，从而能和加密共用同一句 `assign round = round_ctr_reg` 与同一份 `key_mem`。
- `decipher_ctrl`（[L419-488](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L419-L488)）是 4 状态总控 FSM（IDLE/INIT/SBOX/MAIN），与 `encipher_ctrl` 同构；`CTRL_FINAL` 同样是**死代码**，最终轮由 `CTRL_MAIN` 的 else 分支以 `FINAL_UPDATE` 实现。**数状态看 case 分支，不看 localparam。**
- 两侧最实质的差别是计数器推进点的位置：加密 `round_ctr_inc` 在 **2 处**（INIT、MAIN，即「先把计数器推到本轮号再做本轮」）；解密 `round_ctr_dec` 在 **1 处**（SBOX 的 `sword==3` 末拍，即「先做本轮再把计数器推到下一轮号」）。两种写法都保证 `round_ctr_reg` 在每次 key-using 操作时恰好等于正用的 key 下标。
- 一次 AES-128 解密耗 **51 拍**（\(1 + 10\times5\)），AES-256 耗 **71 拍**（\(1 + 14\times5\)），**与加密完全相同**——因为两个 FSM 形状一样，数数方向只改 key 顺序、不改拍数。
- 整个模块严格沿用 u1-l3 的「reg/_new/_we 两段式 + 异步低有效复位」风格：组合块算 `_new`/`_we`，时序块 `reg_update`（[L223-262](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L223-L262)）在时钟沿搬运。

## 7. 下一步学习建议

- **把加解密串成端到端**：进阶篇已把单模块讲完，下一步 [u3-l1 一次完整加解密的端到端追踪](u3-l1-end-to-end-encryption-trace.md) 会以 `tb_aes` 的 `ecb_mode_single_block_test` 为线索，把主机接口 → `aes_core` → `key_mem` 扩展 → `encipher`/`decipher` 各轮 → `result` 串成一条数据流。届时你会看到本讲的 51 拍如何嵌入 `aes_core` 的 init/next 两阶段时序中。
- **用 NIST 向量验证解密**：[u3-l2 仿真验证与 NIST 测试向量](u3-l2-verification-and-nist-vectors.md) 给出了 AES-128/256 的 ECB 已知应答，包括**解密**用例（用密文反推明文）。带着本讲的轮密钥倒序结论去读 testbench，你会很清楚为什么解密前要把 `CONFIG.encdec` 设成 0、为什么 `ready` 拉高后 `RESULT` 里就是明文。
- **把「递减」放进 ASIC 取舍的视角**：本讲看到的「逐字 SubBytes 占每段 5 拍里的 4 拍」「加解密共用 key_mem 但各占一套 FSM」都是面积/时间的权衡。[u3-l4 面向 ASIC 的设计取舍](u3-l4-asic-design-tradeoffs.md) 会把本讲的 51 拍和「全核吞吐仅 0.06 Gbps」直接挂钩，并讨论若要流水线化提升吞吐需要改动哪些模块。
- **想跑起来验证轨迹**：回到 [u1-l5 运行仿真与阅读波形](u1-l5-run-simulation-and-waveforms.md)，用 iverilog/ModelSim 编译 `rtl/*.v` 跑 `tb_aes`，在波形里把 `dut.aes_core.dec_block.dec_ctrl_reg`、`round_ctr_reg`、`sword_ctr_reg` 三个信号加进去，对照第 5.2 节的轨迹表逐拍核对——重点看 `round` 是不是从 10 降到 0。
