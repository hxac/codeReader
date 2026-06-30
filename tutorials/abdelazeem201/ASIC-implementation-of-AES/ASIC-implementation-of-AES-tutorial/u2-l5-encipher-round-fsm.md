# 加密轮控制状态机

## 1. 本讲目标

上一讲（u2-l4）我们把 `aes_encipher_block.v` 里的四个变换**函数**（SubBytes、ShiftRows、MixColumns、AddRoundKey）逐个拆解清楚了。但那些函数只是「会算」，本讲要回答的是：**谁来安排它们、按什么顺序、在哪些时钟周期执行？**

本讲学完后，你应该能够：

1. 说清楚 `encipher_ctrl` 这个控制状态机有哪几个状态、每个状态干什么、状态之间怎么转移。
2. 解释为什么 16 字节的 SubBytes 要被拆成 **4 个时钟周期**（`sword_ctr` 的作用），以及它和「全核只共享一个 S-box」的关系。
3. 解释 `round_ctr` 轮计数器如何与 `key_mem` 的轮密钥配合，每轮用到哪一把 round key。
4. 画出 AES-128 一次完整加密时 `enc_ctrl_reg`、`round_ctr_reg`、`sword_ctr_reg` 随时钟变化的轨迹，并算出总共需要 **51 个时钟周期**。

本讲是加密通路的「指挥层」，把 u2-l4 的「运算层」串成一条按时钟节拍流动的流水线。

## 2. 前置知识

阅读本讲前，请确认你已掌握：

- **u1-l3 的 reg/_new/_we 寄存器模式**：本模块大量使用「组合逻辑块算 `_new`/`_we`，时序块在时钟沿 `if (_we) _reg <= _new`」的两段式写法。本讲不会再重复讲这套模板，只直接套用。
- **u2-l4 的四个变换函数**：`shiftrows`、`mixcolumns`/`mixw`、`addroundkey` 以及 SubBytes 经共享 S-box 的外接端口（`sboxw` 输出 / `new_sboxw` 输入）。
- **u2-l3 的轮密钥存储**：`key_mem[0..14]` 在 init 阶段一次性生成，加/解密时按外部给的轮号 `round` **组合（异步）读出**。
- **u2-l1 的共享 S-box 与多路选择**：全核只有 1 个正向 S-box，由 `aes_core` 的 `sbox_mux` 分时喂给密钥扩展或加密通路；本模块只是 S-box 的「消费者」之一。

两个 AES 基本常识（u1-l1 已建立）：

- AES-128 共 **10 轮**（初始轮 + 9 个完整主轮 + 1 个最终轮），AES-256 共 **14 轮**。
- **初始轮**只做 AddRoundKey；**主轮**做 SubBytes→ShiftRows→MixColumns→AddRoundKey；**最终轮**省去 MixColumns。

还有一个关键术语：本模块的 `block` 是 **128 位输入端口**（明文），而 `block_w0_reg ... block_w3_reg` 是把 128 位拆成 4 个 32 位「字」后**寄存下来的中间状态**。务必区分「输入端口」和「状态寄存器」。

## 3. 本讲源码地图

本讲只涉及一个文件，但它是整个加密通路的「大脑」：

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| [rtl/aes_encipher_block.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v) | 加密轮处理模块（含数据函数 + 控制 FSM） | 四个 `always` 块：`round_logic`（算什么）、`round_ctr`/`sword_ctr`（两个计数器）、`encipher_ctrl`（FSM 总控），外加 `reg_update`（寄存器搬运） |

为对照「S-box 是怎么被本模块消费的」，可顺带回顾 [rtl/aes_core.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v) 里 `enc_block` 的例化与 `sbox_mux`，但本讲不再展开 core 的逻辑。

模块对外的关键端口先记一笔（来自 [rtl/aes_encipher_block.v:11-27](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L11-L27)）：

- `next`（输入）：核心 FSM 给的「开始加密」单拍脉冲。
- `round`（输出，4 位）：当前轮号，送给 `key_mem` 选 round key。
- `round_key`（输入，128 位）：`key_mem` 按上面的轮号组合回送的本轮密钥。
- `sboxw`（输出，32 位）/ `new_sboxw`（输入，32 位）：本模块把待替换的字送给共享 S-box，S-box 把替换结果送回来。
- `block`（输入，128 位，明文）/ `new_block`（输出，128 位，密文）。
- `ready`（输出）：本模块空闲/完成标志。

## 4. 核心概念与源码讲解

本讲把模块拆成 4 个最小模块，按「数据 → 计数器 → 总控」的顺序讲，最后在综合实践里把它们串成一条时钟轨迹。

### 4.1 round_logic：四种 update_type 的数据运算

#### 4.1.1 概念说明

`round_logic` 是一个 **纯组合 `always @*` 块**，它不关心「现在第几轮、第几拍」，只负责回答一个问题：**「如果这一拍要更新数据，应该把哪一种运算结果写回 4 个字寄存器？」**

AES 一轮的三种形态（初始/主/最终）对应三种「写回内容」，外加一种 SubBytes 专用的写回，一共用 4 个 `update_type` 编码（定义在 [rtl/aes_encipher_block.v:39-43](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L39-L43)）：

| update_type | 值 | 写回内容 | 对应 AES 阶段 | 断言的 `_we` |
|-------------|----|---------|--------------|--------------|
| `INIT_UPDATE` | 3'h1 | `block ^ round_key`（明文 ^ rk[0]） | 初始轮（仅 AddRoundKey） | 4 个全开 |
| `SBOX_UPDATE` | 3'h2 | `{new_sboxw ×4}`（只写选中字） | 主/最终轮的 SubBytes | 仅 1 个 |
| `MAIN_UPDATE` | 3'h3 | `MC(SR(state)) ^ round_key` | 主轮尾（SR+MC+ARK） | 4 个全开 |
| `FINAL_UPDATE` | 3'h4 | `SR(state) ^ round_key` | 最终轮尾（SR+ARK，无 MC） | 4 个全开 |
| `NO_UPDATE` | 3'h0 | 不写回 | 空闲 | 全关 |

关键直觉：`round_logic` 一上来就把三种候选结果**全都算出来**，再由 `update_type` 当多路选择器挑一个赋给 `block_new`，同时打开对应的写使能。这是「先全算、再选一个」的典型组合风格——面积换简单。

#### 4.1.2 核心流程

伪代码（对应 [rtl/aes_encipher_block.v:232-314](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L232-L314)）：

```
old_block          = {block_w0_reg, block_w1_reg, block_w2_reg, block_w3_reg}  // 当前状态寄存器
shiftrows_block    = shiftrows(old_block)                                      // 行移位
mixcolumns_block   = mixcolumns(shiftrows_block)                               // 列混淆
addkey_init_block  = addroundkey(block,     round_key)   // 注意：用输入端口 block（明文）
addkey_main_block  = addroundkey(mixcolumns_block, round_key)
addkey_final_block = addroundkey(shiftrows_block,  round_key)

case (update_type)
  INIT_UPDATE : block_new = addkey_init_block;   开 w0..w3_we
  SBOX_UPDATE : block_new = {new_sboxw ×4};      按 sword_ctr 只开 1 个 _we
  MAIN_UPDATE : block_new = addkey_main_block;   开 w0..w3_we
  FINAL_UPDATE: block_new = addkey_final_block;  开 w0..w3_we
endcase
```

注意一个**极易看走眼的细节**：`addkey_init_block` 用的是输入端口 `block`（明文），而 `addkey_main_block`/`addkey_final_block` 用的是 `old_block`（状态寄存器）。原因见下文源码精读。

#### 4.1.3 源码精读

先看三种候选结果的同时计算（[rtl/aes_encipher_block.v:244-249](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L244-L249)）：

```verilog
old_block          = {block_w0_reg, block_w1_reg, block_w2_reg, block_w3_reg};
shiftrows_block    = shiftrows(old_block);
mixcolumns_block   = mixcolumns(shiftrows_block);
addkey_init_block  = addroundkey(block, round_key);       // ← 输入端口 block（明文）
addkey_main_block  = addroundkey(mixcolumns_block, round_key);
addkey_final_block = addroundkey(shiftrows_block, round_key);
```

> 中文说明：这段把当前状态 `old_block` 先做 ShiftRows、再做 MixColumns，分别配上 round_key 得到「主轮结果」与「最终轮结果」；而初始轮结果直接对**输入明文 `block`** 做 AddRoundKey。三者同时算好待选。

再看 `INIT_UPDATE` 分支（[rtl/aes_encipher_block.v:252-259](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L252-L259)）：

```verilog
INIT_UPDATE:
  begin
    block_new = addkey_init_block;   // 明文 ^ round_key
    block_w0_we = 1'b1; ... block_w3_we = 1'b1;   // 4 个字全部写回
  end
```

> 中文说明：初始轮把明文异或上 round_key[0] 后，一次性锁进 4 个字寄存器——从此明文「住进」了寄存器，后续轮都读 `old_block` 而不再读输入端口。

`MAIN_UPDATE`（[rtl/aes_encipher_block.v:292-299](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L292-L299)）写回 `MC(SR(state)) ^ rk`，`FINAL_UPDATE`（[rtl/aes_encipher_block.v:301-308](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L301-L308)）写回 `SR(state) ^ rk`（省去 MixColumns）。这两者都打开全部 4 个写使能。

`SBOX_UPDATE` 分支（[rtl/aes_encipher_block.v:261-290](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L261-L290)）稍复杂，留到 4.2 专讲。

#### 4.1.4 代码实践

**目标**：确认「初始轮读输入端口、其余轮读状态寄存器」这条关键区分。

**步骤**：

1. 打开 [rtl/aes_encipher_block.v:244-249](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L244-L249)。
2. 找到 `addkey_init_block` 的实参是 `block`，而 `addkey_main_block`/`addkey_final_block` 的实参都基于 `old_block`。
3. 自问：如果作者不小心把 `addkey_init_block` 也写成 `addroundkey(old_block, round_key)`，第一次加密会得到什么？**答案**：复位后 `old_block`（4 个字寄存器）是 0，初始轮就会变成 `0 ^ rk[0] = rk[0]`，明文被彻底丢掉，加密结果全错。

**需要观察的现象 / 预期结果**：你能用一句话解释「为什么只有初始轮用 `block`」——因为明文是第一次进入模块、尚未被锁存；锁存之后就只看寄存器了。这是一条**待本地验证**的理解性练习（无需运行，靠阅读即可得出结论）。

#### 4.1.5 小练习与答案

**练习 1**：`FINAL_UPDATE` 为什么不像 `MAIN_UPDATE` 那样套一层 `mixcolumns`？

**参考答案**：因为 AES 标准规定**最终轮省去 MixColumns**，目的是让解密过程的逆变换结构与之对称（否则解密的等效实现会更复杂）。源码里 `addkey_final_block = addroundkey(shiftrows_block, round_key)` 正好跳过了 `mixcolumns_block`。

**练习 2**：四种 `update_type` 里，哪一种只断言 1 个 `_we`？为什么？

**参考答案**：`SBOX_UPDATE`。因为共享 S-box 一次只替换 1 个 32 位字，所以这一拍只能更新 4 个字寄存器中的 1 个（由 `sword_ctr` 选中）。

---

### 4.2 round_ctr：轮计数器与轮密钥的配合

#### 4.2.1 概念说明

`round_ctr` 是一个 **4 位**计数器，记录「当前在第几轮」。它有两个对外作用：

1. 通过输出端口 `round`（`assign round = round_ctr_reg`，见 [rtl/aes_encipher_block.v:172](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L172)）告诉 `key_mem`「给我第几把 round key」。`key_mem` 是组合读，所以 round_key **当拍就有效**。
2. 在 `CTRL_MAIN` 状态里和 `num_rounds` 比较，决定这一轮是「主轮（MAIN_UPDATE）」还是「最终轮（FINAL_UPDATE）」。

`num_rounds` 由 `keylen` 决定（[rtl/aes_encipher_block.v:383-390](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L383-L390)）：AES-128 为 10，AES-256 为 14。

#### 4.2.2 核心流程

`round_ctr` 的组合块（[rtl/aes_encipher_block.v:345-360](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L345-L360)）只有两种动作：复位（`round_ctr_rst` → 0）或自增（`round_ctr_inc` → +1）。它本身不做决定，**何时复位、何时自增完全由 FSM `encipher_ctrl` 控制**。整个加密过程中 `round_ctr_reg` 的关键节拍：

- 复位值 0；`next` 到来那拍在 `CTRL_IDLE` 里被再次清 0。
- `CTRL_INIT`（初始轮）那拍 `round_ctr_reg = 0`，所以用到 **round_key[0]**；该拍末自增到 1。
- 进入第 1 个主轮：`round_ctr_reg = 1`，SubBytes 与紧随的 `CTRL_MAIN` 都在 `round_ctr_reg = 1` 下进行，`CTRL_MAIN` 用 **round_key[1]**。
- 之后每经过一轮，`CTRL_MAIN` 末把 `round_ctr_reg` +1。
- 当 `round_ctr_reg` 到达 `num_rounds`（如 AES-128 的 10），`CTRL_MAIN` 改发 `FINAL_UPDATE`，用 **round_key[num_rounds]**，并回到 IDLE。

> 一句话：第 \(k\) 轮的 SubBytes（SBOX 拍）和它的 ShiftRows+MixColumns+AddRoundKey（MAIN 拍）**共享同一个 `round_ctr_reg = k`**，对应 round_key[k]。轮密钥与轮号严格对齐。

#### 4.2.3 源码精读

计数器寄存器声明（[rtl/aes_encipher_block.v:137-141](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L137-L141)）：

```verilog
reg [3 : 0] round_ctr_reg;
reg [3 : 0] round_ctr_new;
reg         round_ctr_we;
reg         round_ctr_rst;
reg         round_ctr_inc;
```

> 中文说明：典型的 `reg/_new/_we` 三件套，外加 `_rst`/`_inc` 两个「控制脉冲」输入——它们由 FSM 驱动，决定这一拍是清 0 还是 +1。

组合计数逻辑（[rtl/aes_encipher_block.v:345-360](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L345-L360)）：

```verilog
always @*
  begin : round_ctr
    round_ctr_new = 4'h0;
    round_ctr_we  = 1'b0;
    if (round_ctr_rst)        begin round_ctr_new = 4'h0;          round_ctr_we = 1'b1; end
    else if (round_ctr_inc)   begin round_ctr_new = round_ctr_reg + 1'b1; round_ctr_we = 1'b1; end
  end
```

> 中文说明：默认不写；收到 `round_ctr_rst` 就写 0，收到 `round_ctr_inc` 就写「当前值 +1」。复位优先于自增。

FSM 里决定主轮还是最终轮的比较（[rtl/aes_encipher_block.v:425-443](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L425-L443)）：

```verilog
CTRL_MAIN:
  begin
    sword_ctr_rst = 1'b1;
    round_ctr_inc = 1'b1;
    if (round_ctr_reg < num_rounds)        // 还没到最后一轮
      begin update_type = MAIN_UPDATE;  enc_ctrl_new = CTRL_SBOX;  ... end
    else                                    // 已到最后一轮
      begin update_type = FINAL_UPDATE; ready_new = 1'b1; ... enc_ctrl_new = CTRL_IDLE; ... end
  end
```

> 中文说明：`CTRL_MAIN` 每拍都把 `round_ctr` 自增（为下一轮准备），同时按 `round_ctr_reg < num_rounds` 二选一——还没到头就做主轮并回 `CTRL_SBOX` 开启下一轮的 SubBytes；到头了就做最终轮、拉高 `ready`、回 IDLE。

#### 4.2.4 代码实践

**目标**：在源码里把 `round_ctr` 的「动」与「不动」全部找出来。

**步骤**：

1. 在 [encipher_ctrl 块](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L368-L450)里搜索 `round_ctr_inc` 和 `round_ctr_rst` 的所有出现位置。
2. 应当发现：`round_ctr_rst` 只在 `CTRL_IDLE`（`next` 到来时）出现一次；`round_ctr_inc` 在 `CTRL_INIT` 与 `CTRL_MAIN` 各出现一次；`CTRL_SBOX` 里**两者都没有**。

**需要观察的现象 / 预期结果**：因为 `CTRL_SBOX` 不碰 `round_ctr`，所以一轮的 4 个 SubBytes 拍 + 1 个 MAIN 拍期间 `round_ctr_reg` 始终等于同一个轮号 \(k\)。这正是「同一轮共用同一把 round key」的硬件保证。该结论可由静态阅读直接得出，**待本地验证**的是你在仿真波形里实际看到 `round` 信号在一轮内保持不变。

#### 4.2.5 小练习与答案

**练习 1**：`round_ctr` 为什么是 4 位而不是 3 位？

**参考答案**：AES-256 最多需要 14 轮，3 位最多表示到 7，不够；4 位可表示 0–15，正好覆盖 0–14（且完成最终轮后 `round_ctr_reg` 会变成 15，但此时已回 IDLE，下一次 `next` 会把它清 0）。

**练习 2**：AES-128 加密结束时，`CTRL_MAIN`（FINAL_UPDATE）那拍的 `round_ctr_reg` 是多少？用到了第几把 round key？

**参考答案**：是 10，用到 round_key[10]。这也是 AES-128 第 10 轮（最终轮）的正确密钥。

---

### 4.3 sword_ctr：把 SubBytes 拆成 4 拍

#### 4.3.1 概念说明

SubBytes 要对 16 个字节全部做 S-box 替换。但本工程**全核只有 1 个正向 S-box**，且它一次只处理 1 个 32 位字（4 字节，见 u2-l2）。所以 128 位状态必须分 **4 次**送进 S-box，每次替换 1 个字。

`sword_ctr`（SubBytes word counter）就是那个 **2 位**计数器，记录「当前在替换第几个字（0/1/2/3）」。它直接体现了 u2-l1/u2-l4 反复强调的「**用时间换面积**」——多花 4 个时钟周期，省下 3 份 S-box 硬件。

#### 4.3.2 核心流程

`SBOX_UPDATE` 这一拍做的事（对应 [rtl/aes_encipher_block.v:261-290](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L261-L290)）：

1. `sword_ctr_reg` 选中当前要替换的字：0→w0、1→w1、2→w2、3→w3。
2. 把这个字送到输出端口 `sboxw`（`muxed_sboxw = block_w{sword_ctr}_reg`），经 `aes_core` 的 `sbox_mux` 喂给共享 S-box。
3. S-box 同周期返回 `new_sboxw`。
4. `block_new` 被设成 `{new_sboxw, new_sboxw, new_sboxw, new_sboxw}`（4 个字都填同一个替换值），但**只有被选中那一个字的 `_we` 打开**，所以只有它被写回。
5. `sword_ctr` 自增；当 `sword_ctr_reg == 3` 那拍，FSM 顺便跳到 `CTRL_MAIN`，结束这一轮的 SubBytes。

> 关键技巧：`block_new` 的 4 个字都赋成 `new_sboxw`，是一种「**广播 + 选择性写使能**」的写法——不必为每个字单独拼一个 `block_new`，只需让 4 个候选字都等于 S-box 输出，再用 `_we` 决定谁真正接收。因为写回时 `reg_update` 取的是 `block_new` 对应的 32 位切片（如 `block_w0_reg <= block_new[127:096]`），而该切片恰好等于 `new_sboxw`。

#### 4.3.3 源码精读

`SBOX_UPDATE` 分支（[rtl/aes_encipher_block.v:261-290](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L261-L290)）：

```verilog
SBOX_UPDATE:
  begin
    block_new = {new_sboxw, new_sboxw, new_sboxw, new_sboxw};  // 广播
    case (sword_ctr_reg)
      2'h0: begin muxed_sboxw = block_w0_reg; block_w0_we = 1'b1; end  // 选 w0 送出 + 只写 w0
      2'h1: begin muxed_sboxw = block_w1_reg; block_w1_we = 1'b1; end
      2'h2: begin muxed_sboxw = block_w2_reg; block_w2_we = 1'b1; end
      2'h3: begin muxed_sboxw = block_w3_reg; block_w3_we = 1'b1; end
    endcase
  end
```

> 中文说明：`muxed_sboxw` 是要送进 S-box 的输入字（`sboxw` 端口），由 `sword_ctr_reg` 从 4 个字寄存器里挑一个；同时只打开对应字的写使能。4 个字的新值都设成 S-box 的输出 `new_sboxw`，靠 `_we` 选择真正写入谁。

`muxed_sboxw` 经端口连到外面（[rtl/aes_encipher_block.v:173](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L173)）：`assign sboxw = muxed_sboxw;`。

`sword_ctr` 的计数逻辑（[rtl/aes_encipher_block.v:322-337](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L322-L337)）与 `round_ctr` 完全同构：默认不写，`_rst` 写 0、`_inc` 写 +1。FSM 在 `CTRL_INIT` 和 `CTRL_MAIN` 里给它 `_rst`（每轮开始前清 0），在 `CTRL_SBOX` 里给它 `_inc`（每替换一个字 +1）。

#### 4.3.4 代码实践

**目标**：把 4 拍 SubBytes 的数据流走一遍。

**步骤**：

1. 假设进入某轮时 4 个字寄存器为 `(W0, W1, W2, W3)`。
2. 对照 [SBOX_UPDATE 分支](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L261-L290)填下表：

| 拍（sword_ctr_reg） | 送进 S-box 的字 | 写回的寄存器 | 4 个字寄存器新值 |
|----|----|----|----|
| 0 | W0 | block_w0 | (S(W0), W1, W2, W3) |
| 1 | ? | ? | (S(W0), S(W1), W2, W3) |
| 2 | ? | ? | ? |
| 3 | ? | ? | (S(W0), S(W1), S(W2), S(W3)) |

其中 \(S(x)\) 表示「x 经 S-box 替换」。

**需要观察的现象 / 预期结果**：第 3 拍结束时，4 个字全部替换完毕，得到完整的 SubBytes 结果。这正是「同一轮 4 拍 SubBytes」的全部工作量。该表可纯靠阅读填出，**待本地验证**的是仿真中 `sword_ctr_reg` 按 0→1→2→3 翻动且每拍只有 1 个 `_we` 为 1。

#### 4.3.5 小练习与答案

**练习 1**：`sword_ctr` 为什么是 2 位？

**参考答案**：因为 128 位正好被拆成 4 个 32 位字，需要计数 0–3，2 位刚好编码这 4 个值。

**练习 2**：`block_new` 被设成 `{new_sboxw, new_sboxw, new_sboxw, new_sboxw}`，会不会把 4 个字都改成同一个值？

**参考答案**：不会。因为 `block_new` 只是「候选值」，真正写入还要看 `_we`。每拍只有 `sword_ctr_reg` 选中那一个字的 `_we` 为 1，`reg_update` 只更新它，其余 3 个字寄存器保持原值。

---

### 4.4 encipher_ctrl：加密总控状态机

#### 4.4.1 概念说明

`encipher_ctrl` 是本模块的「总指挥」。它本身**不做任何运算**，只做调度：根据当前状态，去拨动 `round_ctr`/`sword_ctr` 的 `_rst`/`_inc`、给 `round_logic` 喂 `update_type`、并决定下一状态。它把 4.1 的数据运算、4.2 的轮计数、4.3 的字计数捏成一条按时钟推进的流程。

**先纠正一个容易混淆的点（以源码为准）**：`localparam CTRL_FINAL` 虽然在 [rtl/aes_encipher_block.v:49](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L49) 被定义了，但**它从未作为 `case(enc_ctrl_reg)` 的分支出现**——整个 FSM 实际只有 **4 个状态**：`CTRL_IDLE`、`CTRL_INIT`、`CTRL_SBOX`、`CTRL_MAIN`（外加 `default` 空分支让综合工具闭嘴）。「最终轮」不是一个独立状态，而是 `CTRL_MAIN` 在 `round_ctr_reg == num_rounds` 时改发 `FINAL_UPDATE` 的那条 else 分支。`CTRL_FINAL` 是一段**死代码**（和 u1-l4 提到的 testbench 死 parameter 同类现象）。记住：状态机有几个状态，要数 `case` 的分支，而不是数 `localparam`。

#### 4.4.2 核心流程

状态转移图（文字版）：

```
        next=1 (round_ctr_rst, ready↓)
 IDLE ─────────────────────────► INIT
  ▲                                 │ INIT_UPDATE(明文^rk[0])，round_ctr 0→1，sword_ctr←0
  │                                 ▼
  │   round_ctr==num_rounds:        SBOX  ←─────┐
  │   FINAL_UPDATE(SR^rk[num]),  ────┐  │  sword_ctr_inc ×4
  │   ready↑                          │  │  (每拍替换 1 个字)
  │                                   │  ▼
  └───────────────────────────────── MAIN
                                      │  round_ctr < num_rounds:
                                      │  MAIN_UPDATE(MC(SR)↑rk)，
                                      │  round_ctr+1，sword_ctr←0 → 回 SBOX
```

各状态职责一览：

| 状态 | 触发/动作 | 关键输出 | 下一状态 |
|------|----------|---------|---------|
| `CTRL_IDLE` | 等 `next` | `round_ctr_rst`、`ready↓` | →`CTRL_INIT`（next 到来） |
| `CTRL_INIT` | 做初始轮 AddRoundKey | `update_type=INIT_UPDATE`、`round_ctr_inc`(0→1)、`sword_ctr_rst` | →`CTRL_SBOX` |
| `CTRL_SBOX` | SubBytes 1 个字 | `update_type=SBOX_UPDATE`、`sword_ctr_inc`；`sword_ctr==3` 时转走 | →`CTRL_MAIN`（sword_ctr==3） |
| `CTRL_MAIN` | 主/最终轮尾 | `sword_ctr_rst`、`round_ctr_inc`；`round_ctr<num_rounds` 发 `MAIN_UPDATE` 否则发 `FINAL_UPDATE`+`ready↑` | →`CTRL_SBOX` 或 →`CTRL_IDLE` |

#### 4.4.3 源码精读

FSM 默认赋值与 `num_rounds` 选择（[rtl/aes_encipher_block.v:372-390](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L372-L390)）：开头先把所有控制脉冲和 `update_type`、`enc_ctrl_new` 全置成「不变/空」的默认值（避免锁存器，这是 u1-l3 强调的组合块习惯），再按 `keylen` 选 `num_rounds`。

`CTRL_IDLE`（[rtl/aes_encipher_block.v:393-403](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L393-L403)）：检测到 `next` 就清 `round_ctr`、拉低 `ready`、跳 `INIT`。

`CTRL_INIT`（[rtl/aes_encipher_block.v:405-412](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L405-L412)）：

```verilog
CTRL_INIT:
  begin
    round_ctr_inc = 1'b1;     // 0 → 1（为下一轮的轮号做准备）
    sword_ctr_rst = 1'b1;     // SubBytes 字计数清 0
    update_type   = INIT_UPDATE;
    enc_ctrl_new  = CTRL_SBOX;
    enc_ctrl_we   = 1'b1;
  end
```

> 中文说明：这一拍用 `round_ctr_reg=0` 对应的 round_key[0] 做初始 AddRoundKey（由 `round_logic` 的 INIT_UPDATE 完成），同时把轮号推进到 1、字计数清 0，下一拍进入 SubBytes。

`CTRL_SBOX`（[rtl/aes_encipher_block.v:414-423](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L414-L423)）：每拍替换 1 个字；当 `sword_ctr_reg == 2'h3`（替换完第 4 个字）那拍顺便跳 `CTRL_MAIN`。

`CTRL_MAIN`（[rtl/aes_encipher_block.v:425-443](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L425-L443)）：见 4.2.3 已引用，按 `round_ctr_reg < num_rounds` 二选一——主轮回 SBOX 开下一轮，最终轮拉高 ready 回 IDLE。

注意所有状态转移都用 `enc_ctrl_new`/`enc_ctrl_we`，最终由 [reg_update 时序块](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L185-L224)在时钟沿搬进 `enc_ctrl_reg`——标准的「组合算下一状态 + 时序搬状态」两段式 FSM（u1-l3）。

#### 4.4.4 代码实践

**目标**：给定状态与输入，预测下一状态与 `update_type`。

**步骤**：对下面 4 个场景，分别写出 `enc_ctrl_new` 和 `update_type`，然后对照源码核对。

1. `enc_ctrl_reg = CTRL_IDLE`，`next = 1`。
2. `enc_ctrl_reg = CTRL_INIT`。
3. `enc_ctrl_reg = CTRL_SBOX`，`sword_ctr_reg = 2'h2`（keylen=128）。
4. `enc_ctrl_reg = CTRL_MAIN`，`round_ctr_reg = 4'd10`，keylen=128（num_rounds=10）。

**预期结果**：1 → `CTRL_INIT` / `NO_UPDATE`；2 → `CTRL_SBOX` / `INIT_UPDATE`；3 → `CTRL_SBOX`（不跳）/ `SBOX_UPDATE`；4 → `CTRL_IDLE` / `FINAL_UPDATE` 且 `ready↑`。这是纯阅读题，**待本地验证**的是第 4 个场景正是 AES-128 收尾的那一拍。

#### 4.4.5 小练习与答案

**练习 1**：本 FSM 真正使用到的状态有几个？`CTRL_FINAL` 是什么？

**参考答案**：真正用到 4 个——`IDLE`/`INIT`/`SBOX`/`MAIN`。`CTRL_FINAL` 只是被 `localparam` 定义但从未出现在 `case` 分支里的死代码；「最终轮」由 `CTRL_MAIN` 的 else 分支用 `FINAL_UPDATE` 实现。

**练习 2**：FSM 在什么条件下回到 `CTRL_IDLE`？回到 IDLE 时 `ready` 是什么？

**参考答案**：仅在 `CTRL_MAIN` 且 `round_ctr_reg == num_rounds` 时回到 IDLE，同时 `ready_new = 1'b1`（拉高，表示加密完成、结果有效）。

---

## 5. 综合实践：画出 AES-128 完整加密的时钟轨迹

**任务**：画出 AES-128（num_rounds=10）一次完整加密时，`enc_ctrl_reg`、`round_ctr_reg`、`sword_ctr_reg` 随时钟周期变化的轨迹，并标注总共需要多少个时钟周期。这是本讲所有最小模块的汇合点——你需要同时用到 4.1（update_type）、4.2（round_ctr）、4.3（sword_ctr）、4.4（FSM）。

### 5.1 操作步骤

1. 设第 1 拍为 `CTRL_IDLE` 且 `next` 到来那一拍（`round_ctr_reg` 此时被清成 0）。
2. 从第 2 拍开始按 FSM 逐拍推导，记录每拍的 `enc_ctrl_reg`、`round_ctr_reg`、`sword_ctr_reg`、`update_type`、所用 round key。
3. 一直推到 `ready` 重新拉高、状态回到 IDLE 为止。
4. 数出「从 INIT 开始到 ready 重新拉高」经过了多少拍。

### 5.2 参考轨迹（worked example）

下表给出关键拍（每轮只列 SubBytes 的第 1 拍和 MAIN 拍，中间 3 拍 SubBytes 形式相同）。`rk[k]` 表示第 k 把 round key；「↑/↓」表示该拍末 `ready` 被置高/低。

| 拍号 | enc_ctrl_reg | round_ctr_reg | sword_ctr_reg | update_type | round key | 该拍做的事（拍末效果） |
|----|----|----|----|----|----|----|
| 1 | IDLE | 0→0 | 0 | NO_UPDATE | — | 收到 next，ready↓，清 round_ctr |
| 2 | INIT | 0→1 | 0 | INIT_UPDATE | rk[0] | 明文 ^ rk[0] 锁入寄存器 |
| 3 | SBOX | 1 | 0→1 | SBOX_UPDATE | — | 替换字0 |
| 4 | SBOX | 1 | 1→2 | SBOX_UPDATE | — | 替换字1 |
| 5 | SBOX | 1 | 2→3 | SBOX_UPDATE | — | 替换字2 |
| 6 | SBOX | 1 | 3→0 | SBOX_UPDATE | — | 替换字3，转 MAIN |
| 7 | MAIN | 1→2 | 0 | MAIN_UPDATE | rk[1] | 第1轮 SR+MC+ARK |
| 8–11 | SBOX | 2 | 0→…→0 | SBOX_UPDATE | — | 第2轮 SubBytes（4 拍） |
| 12 | MAIN | 2→3 | 0 | MAIN_UPDATE | rk[2] | 第2轮 SR+MC+ARK |
| … | … | … | … | … | … | 每轮 5 拍：4 SBOX + 1 MAIN |
| 第 k 轮 | SBOX×4 + MAIN×1 | k→k+1 | 0→…→0 | SBOX/MAIN_UPDATE | rk[k] | 主轮 |
| … | … | … | … | … | … | |
| 48–51 | SBOX | 10 | 0→…→0 | SBOX_UPDATE | — | 第10轮 SubBytes（4 拍） |
| 52 | MAIN | 10→11 | 0 | FINAL_UPDATE | rk[10] | 最终轮 SR+ARK，**ready↑**，回 IDLE |
| 53 | IDLE | 11 | 0 | NO_UPDATE | — | 空闲，等待下一次 next |

### 5.3 周期数统计

数「活跃处理拍」：1 拍 `INIT` + 10 轮 ×（4 拍 SBOX + 1 拍 MAIN）：

\[
\text{cycles}_{\text{AES-128}} = 1 + 10 \times (4 + 1) = 1 + 50 = 51
\]

即 **`aes_encipher_block` 自身完成一次 AES-128 加密需要 51 个时钟周期**（从 `next` 之后的 INIT 拍算起，到 `ready` 重新拉高）。`ready` 在这 51 拍内保持低电平。

对 AES-256（num_rounds=14）同理：

\[
\text{cycles}_{\text{AES-256}} = 1 + 14 \times 5 = 71
\]

**需要观察的现象 / 预期结果**：在一轮的 4 个 SBOX 拍 + 1 个 MAIN 拍里 `round_ctr_reg` 恒为 k，只在 MAIN 拍末跳到 k+1；`sword_ctr_reg` 在每轮 SBOX 段重复 0→1→2→3→(0)。可在仿真里把这三个寄存器加入波形（用 `dump_dut_state` 或层次化引用 `dut.aes_core.enc_block.enc_ctrl_reg` 等，见 u1-l5）逐一核对——**待本地验证**，因为本讲不假设你已经跑过仿真。

> 提示：这里数的是 encipher_block 模块**自身**的耗时。`aes_core` 的 `CTRL_NEXT` 还要再花 1 拍去采样 `enc_ready` 并回 IDLE，所以从主机角度看端到端会多 1 拍，那属于 u3-l1 端到端追踪的范畴，本讲不计入。

## 6. 本讲小结

- `round_logic` 是纯组合「数据层」：一次性算好初始/主/最终三种候选结果，由 `update_type`（INIT/SBOX/MAIN/FINAL）挑一个写回；注意**初始轮读输入端口 `block`，其余轮读状态寄存器 `old_block`**。
- `round_ctr`（4 位）是轮计数器，通过端口 `round` 选 round key；同一轮的 SubBytes 拍与 MAIN 拍共享同一个 `round_ctr_reg = k`，对应 round_key[k]。
- `sword_ctr`（2 位）把 128 位 SubBytes 拆成 4 拍，每拍只经共享 S-box 替换 1 个 32 位字——这是「用时间换面积」在控制层的直接体现。
- `encipher_ctrl` 是 4 状态总控 FSM（IDLE/INIT/SBOX/MAIN）；`CTRL_FINAL` 是**从未被 case 用到的死代码**，最终轮由 `CTRL_MAIN` 的 else 分支以 `FINAL_UPDATE` 实现。**数状态看 case 分支，不看 localparam。**
- 一次 AES-128 加密耗 51 拍（\(1 + 10\times5\)），AES-256 耗 71 拍（\(1 + 14\times5\)）。
- 整个模块严格沿用 u1-l3 的「reg/_new/_we 两段式 + 异步低有效复位」风格：组合块算 `_new`/`_we`，时序块 `reg_update` 在时钟沿搬运。

## 7. 下一步学习建议

- **对称地去读解密 FSM**：下一篇 u2-l6 + u2-l7 会讲 `aes_decipher_block.v` 的逆变换函数与 `decipher_ctrl`。建议你带着一个问题去读——**解密的轮计数器是递减的**（`round_ctr_set` 到 num_rounds 后逐轮 -1），想想为什么解密要「从最后一轮倒推」，而加密是递增。对比二者的 FSM 是巩固本讲的最佳方式。
- **关注共享 S-box 的时序后果**：本讲看到 SubBytes 占了每轮 5 拍里的 4 拍。在读 u3-l4（ASIC 设计取舍）时，你会把「逐字 SubBytes」和「全核吞吐仅 0.06 Gbps」直接挂钩——本讲给出的 51 拍就是那条因果链的定量起点。
- **想跑起来验证轨迹**：回到 u1-l5，用 iverilog/ModelSim 编译 `rtl/*.v` 跑 `tb_aes`，在波形里把 `dut.aes_core.enc_block.enc_ctrl_reg`、`round_ctr_reg`、`sword_ctr_reg` 三个信号加进去，对照第 5 节的轨迹表逐拍核对。
