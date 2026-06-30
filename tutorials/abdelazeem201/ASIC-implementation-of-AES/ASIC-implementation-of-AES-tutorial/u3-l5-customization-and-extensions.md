# 二次开发与扩展实践

## 1. 本讲目标

本讲是专家篇的最后一讲，也是整本手册的收尾。前面你已经把 AES 核从顶层总线、核心状态机、密钥扩展、加解密数据通路、端到端时序、NIST 验证、分层测试到 ASIC 取舍都走了一遍。本讲的目标是把这些"读懂"的能力转化成"改得动、改得对"的能力：

- 学会向 `tb_aes.v` 的 `aes_test` 任务里**新增一组自检用例**，并用 NIST/FIPS 已知应答驱动它。
- 理解**改动地址映射或总线接口会牵动哪些地方**，避免"改一处、崩一片"。
- 掌握**用时间换面积**的反向操作——流水线化提升吞吐的思路，以及它要付出什么代价。
- 学会用仓库自带的 5 个 testbench 做**回归测试**，确保每次改动后核心仍然正确。

本讲不引入新的硬件模块，而是把已有模块当作"可改造的对象"，强调改动的**依赖链**与**回归验证**。

## 2. 前置知识

在学习本讲前，你应当已经掌握（对应前置讲义）：

- **u3-l1 端到端追踪**：一次加解密必须分两次主机触发——`init`（密钥扩展）和 `next`（加/解密），二者因共享同一个正向 S-box 必须分时复用。
- **u3-l4 ASIC 取舍**：全核只例化一个正向 S-box，由 `sbox_mux` 分时复用；SubBytes 被拆成 4 拍逐字处理；统一"异步低有效复位 + 每寄存器配写使能 `_we`"风格；AES-128 的 `next` 阶段约 51 拍、AES-256 约 71 拍。
- **u1-l4 地址映射**：`aes.v` 用 `localparam` 定义地址，是权威版本；`tb_aes.v` 里同名 `parameter` 必须与之匹配。
- **u2-l5 加密 FSM**：`sword_ctr` 把 128 位 SubBytes 拆成 4 拍，是"用时间换面积"的关键时钟级体现。

几个本讲会反复用到的术语：

| 术语 | 含义 |
|------|------|
| 已知应答向量（KAT） | 一组标准的"输入明文+密钥 → 期望密文"，来自 NIST SP 800-38A 或 FIPS-197，用来核对实现是否正确。 |
| 自检式 testbench | testbench 自己算/持有期望值，运行中自动比对，结束时用计数器报告通过/失败数。 |
| 回归测试 | 每次改动后重新运行全部测试，确认旧行为没有被破坏。 |
| 流水线（pipeline） | 把一个多拍的处理拆成若干级，每级寄存器锁存，使多个数据块能重叠处理，提高吞吐。 |

## 3. 本讲源码地图

本讲主要围绕三个文件展开，它们恰好覆盖了"测试—接口—核心"三层：

| 文件 | 在本讲中的作用 |
|------|----------------|
| `rtl/tb_aes.v` | 自检式 testbench。本讲把它当作**第一个被改造的对象**——新增测试用例就改这里。 |
| `rtl/aes.v` | 顶层 wrapper，定义总线端口与地址映射。改接口/地址就改这里。 |
| `rtl/aes_core.v` | 核心调度中枢，例化唯一的正向 S-box。讨论流水线化时，这里是"吞吐瓶颈"所在。 |

讨论回归测试时还会涉及 `rtl/` 下的另外 4 个 testbench（`tb_aes_core.v`、`tb_aes_encipher_block.v`、`tb_aes_decipher_block.v`、`tb_aes_key_mem.v`），它们与 `tb_aes.v` 共同构成分层验证网。

---

## 4. 核心概念与源码讲解

### 4.1 增加 NIST 测试用例

#### 4.1.1 概念说明

`tb_aes.v` 的核心设计是：**数据与流程分离**。

- 流程被封装成一个参数化模板任务 `ecb_mode_single_block_test`，它知道"如何驱动一次单块 ECB 加/解密并自检"。
- 数据则集中放在 `aes_test` 任务开头的若干 `reg` 里（密钥、明文、期望密文），全部取自 NIST SP 800-38A 附录 F 的标准向量。

这种分离带来的好处是：**新增一组测试用例，通常不需要写任何驱动逻辑，只需要加一行模板调用（必要时再加一两个数据 `reg`）**。这正是"二次开发"里最安全、最推荐的入口——先从加测试用例开始练手，因为它不会改动设计源码，风险最低。

#### 4.1.2 核心流程

一次"新增用例"的流程可以归纳为三步：

1. **判断方向**：加密用例把明文作输入、密文作期望；解密用例则**反过来**——把密文作输入、明文作期望。因为解密就是加密的逆运算，所以输入与期望对调即可。
2. **准备数据**：若新用例的数据已在作用域内（如复用 `nist_plaintext0`），直接引用；否则在 `aes_test` 里新增一个 `reg` 并赋一个来自标准的已知应答值。
3. **追加调用**：在 `aes_test` 末尾追加一行 `ecb_mode_single_block_test(...)`，给它一个**尚未使用**的 `tc_number`（测试编号）。

模板内部每被调用一次就会 `tc_ctr = tc_ctr + 1`（统计用例数），失败时 `error_ctr = error_ctr + 1`。所以新用例加完后，最终报告的通过数会自动 +1，`error_ctr` 仍应为 0。

#### 4.1.3 源码精读

先看模板任务本身——[rtl/tb_aes.v:L341-L377](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L341-L377)，这就是"一次单块 ECB 加/解密 + 自检"的全部驱动逻辑：

关键片段（精简后）：

```verilog
task ecb_mode_single_block_test(input [7:0]   tc_number,
                                input           encdec,
                                input [255:0]   key,
                                input           key_length,
                                input [127:0]   block,
                                input [127:0]   expected);
  tc_ctr = tc_ctr + 1;
  init_key(key, key_length);          // 写密钥 + 设 keylen + 触发 CTRL.init
  write_block(block);                 // 写入待处理块
  write_word(ADDR_CONFIG, (8'h00 + (key_length << 1) + encdec));  // 设方向+密钥长度
  write_word(ADDR_CTRL, 8'h02);       // 触发 CTRL.next（bit1）
  #(100 * CLK_PERIOD);                // 固定等待处理完成
  read_result();                      // 读回 128 位结果
  if (result_data == expected) ...    // 自检：相等则通过
  else error_ctr = error_ctr + 1;     // 否则失败计数 +1
```

注意第 355 行 `ADDR_CONFIG` 的位编码：`(key_length << 1) + encdec`，即 bit1=keylen、bit0=encdec，与 `aes.v` 中 `CTRL_KEYLEN_BIT=1`、`CTRL_ENCDEC_BIT=0` 一致（见 [rtl/aes.v:L39-L41](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L39-L41)）。

再看解密用例是如何"对调"的——[rtl/tb_aes.v:L468-L478](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L468-L478)：

```verilog
// 加密：明文进，期望密文出
ecb_mode_single_block_test(8'h10, AES_ENCIPHER, nist_aes256_key, AES_256_BIT_KEY,
                           nist_plaintext0, nist_ecb_256_enc_expected0);
// 解密：密文进，期望明文出（输入与期望正好对调）
ecb_mode_single_block_test(8'h14, AES_DECIPHER, nist_aes256_key, AES_256_BIT_KEY,
                           nist_ecb_256_enc_expected0, nist_plaintext0);
```

可以看到，TC `0x14` 就是 TC `0x10` 的"逆"：同一把密钥，把 `0x10` 的期望密文当作输入，把 `0x10` 的输入明文当作期望。这就是解密用例的标准写法。

数据定义集中在 [rtl/tb_aes.v:L404-L421](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L404-L421)，例如：

```verilog
nist_aes256_key = 256'h603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4;
nist_plaintext0 = 128'h6bc1bee22e409f96e93d7e117393172a;
nist_ecb_256_enc_expected0 = 128'hf3eed1bdb5d2a03c064b5a7e3db181f8;
```

> 重要约定：AES-128 的密钥统一放进 256 位容器，真实密钥在**高 128 位**、低 128 位填 0（见 [rtl/tb_aes.v:L405](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L405)），因为 `init_key` 总是写满 KEY0..KEY7 共 256 位。

#### 4.1.4 代码实践

> 实践目标：在 `aes_test` 中新增一个 **ECB 解密**用例，复用已有密钥、用一个**新的已知应答**反推明文，跑通后确认 `error_ctr` 仍为 0。

**背景说明（务必先读懂）**：现有 16 组用例已经把 4 个明文 × {AES-128, AES-256} × {加密, 解密} 全部覆盖了。也就是说，"现有密钥 + 现有密文"的解密组合已经被 TC `0x05`~`0x08`、`0x14`~`0x17` 占满。因此要新增一个**有意义的、非重复的**解密用例，最稳妥的做法是引入一组**新的标准已知应答**。下面采用 AES 标准本身（FIPS-197 附录 B）的 AES-128 示例向量——它与本 testbench 使用的 SP 800-38A 向量是**不同来源**的数据，绝不会与现有用例重复。

FIPS-197 附录 B 的 AES-128 已知应答（可在 AES 标准 FIPS-197 文档中查到，可信引用）：

```
key       = 000102030405060708090a0b0c0d0e0f
plaintext = 00112233445566778899aabbccddeeff
ciphertext= 69c4e0d86a7b0430d8cdb78070b4c55a
```

**操作步骤**（以下是你需要在 `rtl/tb_aes.v` 里做的改动——这是练习任务，由你来改，本讲义不替你改源码）：

1. 在 `aes_test` 任务的声明区（[rtl/tb_aes.v:L386-L402](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L386-L402)）新增三个局部 `reg`，并在任务体开头（[rtl/tb_aes.v:L421](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L421) 之后）赋值。注意 256 位容器约定：高 128 位是真实密钥，低 128 位填 0：

   ```verilog
   // 声明区新增（与 nist_aes128_key 等并列）
   reg [255:0] fips_aes128_key;
   reg [127:0] fips_plaintext;
   reg [127:0] fips_ciphertext;

   // 任务体赋值：FIPS-197 Appendix B AES-128 example（新引入的已知应答）
   fips_aes128_key = 256'h000102030405060708090a0b0c0d0e0f00000000000000000000000000000000;
   fips_plaintext  = 128'h00112233445566778899aabbccddeeff;
   fips_ciphertext = 128'h69c4e0d86a7b0430d8cdb78070b4c55a;
   ```

2. 在 `aes_test` 末尾（[rtl/tb_aes.v:L478](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L478) 之后）追加**解密**用例。解密把密文作输入、明文作期望：

   ```verilog
   // 新增：FIPS-197 AES-128 ECB 解密用例（密文反推明文）
   ecb_mode_single_block_test(8'h20, AES_DECIPHER, fips_aes128_key, AES_128_BIT_KEY,
                              fips_ciphertext, fips_plaintext);
   ```

   编号 `0x20` 故意选大值，避开现有 `0x01`~`0x17`。

3. （可选，便于交叉印证）同时追加一个**加密**用例作为对照：

   ```verilog
   ecb_mode_single_block_test(8'h21, AES_ENCIPHER, fips_aes128_key, AES_128_BIT_KEY,
                              fips_plaintext, fips_ciphertext);
   ```

**需要观察的现象**：

- 仿真结束时的总结报告，用例计数 `tc_ctr` 应从原来的 **16** 变成 **17**（若同时加了加密对照，则是 18）。
- `error_ctr` 应保持 **0**。

**预期结果**：终端应打印类似

```
*** All 17 test cases completed successfully
*** AES simulation done. ***
```

> 待本地验证：上述 FIPS-197 向量为标准公开值，可放心引用；但"17 个用例全通过"这一运行结论需你本地编译运行 `tb_aes` 后确认（参见 4.4 节的编译命令）。本讲义不假装已替你跑过。

#### 4.1.5 小练习与答案

**练习 1**：为什么不建议直接复制 TC `0x05` 那一行、只改个编号当作"新用例"？
<details><summary>参考答案</summary>
因为 TC `0x05` 已经覆盖了"用 `nist_aes128_key` 解密 `nist_ecb_128_enc_expected0` 得到 `nist_plaintext0`"这一组合。原样复制只是把同一组数据再跑一遍，除了让 `tc_ctr` 多 1，并不能增加任何测试覆盖；真正的"新用例"应当引入新的数据（新明文/密钥/密文），像本节那样从 FIPS-197 取一组不同来源的向量。</details>

**练习 2**：如果要把一个加密用例改写成对应的解密用例，模板调用的哪两个实参需要互换？
<details><summary>参考答案</summary>
第 5 个实参 `block`（输入块）和第 6 个实参 `expected`（期望结果）互换，同时把第 2 个实参 `encdec` 从 `AES_ENCIPHER` 改成 `AES_DECIPHER`。密钥与密钥长度保持不变。</details>

---

### 4.2 地址映射与接口改动

#### 4.2.1 概念说明

`aes.v` 对外是一个**内存映射的 32 位总线接口**：主机靠 `address` 区分要访问的是控制寄存器、密钥、明文还是结果。所谓"地址映射"，就是"地址号 ↔ 寄存器"的对照表。

改动地址映射或接口是**高风险操作**，因为这张表在工程里**被重复定义**：

- `aes.v` 用 `localparam` 定义地址（**权威版本**，硬件真正按它译码）。
- `tb_aes.v` 用 `parameter` 定义**同名**地址（驱动测试时用，必须与硬件一致）。

两边任何一边改了、另一边没跟上，就会出现"测试驱动写到了错误的地址、硬件收不到"的隐蔽 bug。因此本节的核心教训是：**改地址/接口必须同时改两个文件，并把所有引用点都过一遍**。

#### 4.2.2 核心流程

地址译码的执行流程（一次总线写为例）：

1. 主机拉高 `cs`、`we`，给出 `address` 与 `write_data`。
2. `aes.v` 的 `api` 组合块（`always @*`）根据 `address` 决定拉高哪个写使能：`init_new/next_new`（CTRL）、`config_we`（CONFIG）、`key_we`（KEY 区段）、`block_we`（BLOCK 区段）。
3. `reg_update` 时序块在时钟沿根据这些写使能把 `write_data` 落地到对应寄存器。
4. 读操作则由 `api` 块里的 `case` 与 RESULT 区段范围检查，把对应寄存器选通到 `read_data`。

若要改动地址（例如把 RESULT 区段从 `0x30~0x33` 挪到 `0x40~0x43`），需要同步修改：

- `aes.v`：`ADDR_RESULT0/RESULT3` 两个 localparam + `api` 块里 RESULT 的范围判断 + RESULT 读出时的下标计算。
- `tb_aes.v`：`ADDR_RESULT0..RESULT3` 这 4 个 parameter。

#### 4.2.3 源码精读

**地址映射的权威定义**在 [rtl/aes.v:L27-L50](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L27-L50)：

```verilog
localparam ADDR_CTRL    = 8'h08;   // 控制寄存器：init/next 触发位
localparam ADDR_STATUS  = 8'h09;   // 状态寄存器：ready/valid
localparam ADDR_CONFIG  = 8'h0a;   // 配置：encdec/keylen
localparam ADDR_KEY0    = 8'h10;   // 密钥区段 0x10~0x17（256 位）
localparam ADDR_BLOCK0  = 8'h20;   // 明文区段 0x20~0x23（128 位）
localparam ADDR_RESULT0 = 8'h30;   // 结果区段 0x30~0x33（128 位）
```

**译码逻辑**在 `api` 块——[rtl/aes.v:L189-L236](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L189-L236)。它分两半：写半（`if (we)`）按地址置写使能；读半（`else`）用 `case` 选通身份/控制/状态，再用范围检查选通 RESULT：

```verilog
// 写：按地址置写使能
if (address == ADDR_CTRL)   begin init_new = ...; next_new = ...; end
if (address == ADDR_CONFIG) config_we = 1'b1;
if ((address >= ADDR_KEY0)   && (address <= ADDR_KEY7))   key_we   = 1'b1;
if ((address >= ADDR_BLOCK0) && (address <= ADDR_BLOCK3)) block_we = 1'b1;
// 读：RESULT 区段按地址低位选 32 位字
if ((address >= ADDR_RESULT0) && (address <= ADDR_RESULT3))
  tmp_read_data = result_reg[(3 - (address - ADDR_RESULT0)) * 32 +: 32];
```

注意最后这行 [rtl/aes.v:L232-L233](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L232-L233)：RESULT 的字选下标是用 `(address - ADDR_RESULT0)` 算的。**这就是为什么改 `ADDR_RESULT0` 必须连同这一行一起检查**——它硬编码了基地址参与下标运算。

**寄存器落地**在 [rtl/aes.v:L140-L181](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L140-L181)，KEY/BLOCK 用地址低位作数组下标：

```verilog
if (key_we)   key_reg[address[2:0]]   <= write_data;  // KEY：低 3 位选 8 个字
if (block_we) block_reg[address[1:0]] <= write_data;  // BLOCK：低 2 位选 4 个字
```

这两行说明：KEY/BLOCK 的"字内偏移"是直接从 `address` 低位取的，**只要基地址对齐不变**（KEY 在 0x10 起、BLOCK 在 0x20 起），字内顺序就自动正确；若把 BLOCK 基地址改到一个非 4 字节对齐的位置，`address[1:0]` 的取值就会乱套。

**testbench 侧的镜像定义**在 [rtl/tb_aes.v:L25-L58](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L25-L58)。这里有一个值得警惕的"死参数"陷阱：

```verilog
parameter CTRL_INIT_BIT   = 0;  // 与 aes.v 一致，有效
parameter CTRL_NEXT_BIT   = 1;  // 与 aes.v 一致，有效
parameter CTRL_ENCDEC_BIT = 2;  // ⚠ aes.v 的 CTRL 里没有这一位，本 testbench 也从未使用
parameter CTRL_KEYLEN_BIT = 3;  // ⚠ 同上，死参数
```

`tb_aes.v` 把 `ENCDEC`/`KEYLEN` 当作 CTRL 的 bit2/bit3，但 `aes.v` 实际把它们放在 **CONFIG 寄存器**（[rtl/aes.v:L39-L41](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L39-L41)），且 testbench 自己也是写 `ADDR_CONFIG` 来设置方向（[rtl/tb_aes.v:L355](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L355)）。所以这两个 parameter 是**历史遗留的死代码**，改它们不会有任何效果——这是"接口定义被重复"时最坑人的地方：你以为改对了，其实改的是一份没人用的副本。

#### 4.2.4 代码实践

> 实践目标：通过"思想实验 + 局部核对"理解改地址的连锁影响，不实际改坏设计源码。

**操作步骤**（源码阅读型实践，不修改设计文件）：

1. 打开 [rtl/aes.v:L232-L233](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L232-L233)，确认 RESULT 读出下标依赖 `ADDR_RESULT0`。
2. 假设要把 `ADDR_RESULT0` 从 `0x30` 改成 `0x40`、`ADDR_RESULT3` 从 `0x33` 改成 `0x43`。请列出**所有需要同步修改的位置**。
3. 同样地，假设要把总线从 32 位加宽到 64 位（`write_data`/`read_data` 变 64 位，KEY 用 4 个字、BLOCK 用 2 个字）。请列出牵连范围。

**需要观察的现象（思考结论）**：

- 改 RESULT 基地址至少要动 4 处：`aes.v` 的 `ADDR_RESULT0`/`ADDR_RESULT3` 两个 localparam、`api` 块的范围判断、`api` 块的下标计算；外加 `tb_aes.v` 的 `ADDR_RESULT0..3` 四个 parameter。
- 加宽数据总线牵连更广：`aes.v` 端口（[rtl/aes.v:L9-L22](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L9-L22)）→ `reg_update` 的 KEY/BLOCK 数组位宽与下标 → `core_key`/`core_block` 拼接（[rtl/aes.v:L102-L106](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L102-L106)）→ `aes_core` 端口 → `tb_aes` 的 `write_word`/`read_word`/`write_block`/`read_result` 任务（全部按 32 位字拆装，见 [rtl/tb_aes.v:L243-L294](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L243-L294)）。

**预期结果**：你能画出一张"改动涟漪图"，证明接口/地址的改动会从顶层一路波及 testbench 的驱动任务。结论是——**能不动接口就不动；非要动，先更新两个文件的地址表，再全局搜索旧地址值确认没有遗漏**。

> 待本地验证：若你确实尝试改地址，需重新运行 `tb_aes`（见 4.4）确认 16 组用例仍全通过；本讲义不替你执行改动。

#### 4.2.5 小练习与答案

**练习 1**：`tb_aes.v` 里的 `CTRL_ENCDEC_BIT=2` 为什么是"死参数"？请给出两点理由。
<details><summary>参考答案</summary>
（1）硬件侧 `aes.v` 的 CTRL 寄存器只有 bit0(init)/bit1(next)，根本没有 bit2/bit3；方向位实际在 CONFIG 寄存器（`CTRL_ENCDEC_BIT=0` 定义在 ADDR_CONFIG 段）。（2）testbench 自己设置方向时写的是 `ADDR_CONFIG`（见 ecb_mode_single_block_test 第 355 行），从未把 `CTRL_ENCDEC_BIT` 用在任何 `write_word` 调用里，所以它从未参与驱动。</details>

**练习 2**：KEY 区段为什么能直接用 `address[2:0]` 选 8 个字，而不用担心基地址变化？
<details><summary>参考答案</summary>
因为 KEY 区段被设计成 8 字节对齐（基地址 0x10 = 0b0001_0000，低 3 位为 0），所以 `address[2:0]` 正好在 0x10~0x17 范围内取到 0~7，与 `key_reg[0:7]` 一一对应。如果把 KEY 基地址挪到一个低 3 位非 0 的地址（如 0x13），这个下标就会错位，写入会落到错误的字里。</details>

---

### 4.3 流水线化提升吞吐的思路

#### 4.3.1 概念说明

u3-l4 已经讲清：本核是**迭代式**结构——一个数据块要在同一个 S-box 和同一套轮逻辑上反复跑几十拍。这种结构面积小、功耗低，但吞吐被"串行拍数"卡死。流水线化（pipelining）就是反过来用"面积换时间"：把处理拆成若干级，每级之间插寄存器，让多个数据块重叠流动，从而提升吞吐。

本节不教你立刻动手改（那是另一个工程），而是帮你**看懂瓶颈在哪、有哪些改造方向、各自要付出什么代价**。这是"二次开发"里风险最高、收益也最大的一类改动。

#### 4.3.2 核心流程

先量化现状。一次 AES-128 加密的 `next` 阶段约需 \(N_{cycles}=51\) 拍（u2-l5 结论：1 拍初始轮 + 10 轮 × 5 拍）。吞吐率与时钟频率的关系为：

\[
\text{Throughput} = \frac{128 \cdot f_{clk}}{N_{cycles}}
\]

README 宣称吞吐 0.06 Gbps，可反推时钟频率：

\[
f_{clk} = \frac{0.06 \times 10^{9} \times 51}{128} \approx 23.9 \,\text{MHz}
\]

（该频率为按 README 数值反推的估计，待本地用综合/仿真工具验证。）

三个主流改造方向，按"改动量从小到大"排列：

| 方向 | 做法 | 吞吐收益 | 面积代价 | 改动范围 |
|------|------|----------|----------|----------|
| A. SubBytes 全并行 | 例化 16 个 S-box，1 拍替换完 128 位 | 每轮 5→2 拍，吞吐约 2.4× | S-box 数量 ×16 | 改 `encipher/decipher` 的 FSM（删 `sword_ctr` 4 拍循环） |
| B. 轮流水线 | 每轮一级寄存器，10 级流水 | 稳态每拍处理一级，吞吐大增 | 每级都要 S-box + 轮逻辑 | 大改 `aes_core` 与加解密模块，破坏共享 S-box |
| C. 全展开 | 10 轮全部展开成组合逻辑 + 级间寄存器 | 理论上每周期出一个块 | 面积最大 | 几乎重写数据通路 |

关键认识：**方向 B/C 直接与"共享单个 S-box"的核心取舍冲突**——一旦流水线化，每个流水级都需要自己的 S-box（否则各级会在 S-box 上撞车），S-box 数量会从 1 个暴涨到十几甚至几十个，u3-l4 精心换来的面积/功耗优势会被还回去。

#### 4.3.3 源码精读

**吞吐瓶颈的根源**——全核只例化**一个**正向 S-box，见 [rtl/aes_core.v:L138](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L138-L138)：

```verilog
aes_sbox sbox_inst(.sboxw(muxed_sboxw), .new_sboxw(new_sboxw));
```

它由 `sbox_mux` 在密钥扩展与加密之间分时复用——[rtl/aes_core.v:L184-L194](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L184-L194)：

```verilog
always @* begin : sbox_mux
  if (init_state) muxed_sboxw = keymem_sboxw;  // init 阶段：给密钥扩展用
  else           muxed_sboxw = enc_sboxw;      // next 阶段：给加密用
end
```

这正是"init 与 next 必须分阶段互斥"的硬件根因（u3-l1 结论）：**S-box 同一时刻只能服务一个消费者**。流水线化若想让 init 和 next 重叠、或让多个块的 next 重叠，就必须打破这个单点共享。

**结果端口的直接镜像**也限制了流水线化——[rtl/aes_core.v:L144-L146](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L144-L146)：

```verilog
assign ready        = ready_reg;
assign result       = muxed_new_block;   // 结果直接等于车间实时工作寄存器
assign result_valid = result_valid_reg;
```

`result` 不是独立的"结果寄存器"，而是车间（enc/dec）的实时工作寄存器裸接出来（u3-l1 已强调：全程在变，只有 `result_valid=1` 后读才有意义）。流水线化通常需要在输出端加一级结果寄存器/FIFO，这就要把这条 `assign` 改成寄存输出，连带 `aes.v` 的 `result_reg` 镜像逻辑（[rtl/aes.v:L163-L165](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L163-L165)）一起调整。

**加解密车间在核心里的并行存在**——[rtl/aes_core.v:L86-L118](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L86-L118) 同时例化了 `enc_block` 和 `dec_block`，由 `encdec_mux`（[rtl/aes_core.v:L203-L224](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L203-L224)）二选一激活。注意：当前设计里**加密与解密绝不同时工作**（`enc_next` 与 `dec_next` 互斥）。如果想做"加密流水线 + 解密流水线"双通道，这里也要从"二选一"改成"两路独立"，面积翻倍。

#### 4.3.4 代码实践

> 实践目标：用纸笔量化"SubBytes 全并行"方向的收益与代价，不改源码。

**操作步骤**（分析型实践）：

1. 复习 u2-l5：当前每轮 SubBytes 由 2 位 `sword_ctr` 拆成 **4 拍**（128 位 / 每拍 32 位 / 共用 1 个 S-box）。一轮共 5 拍 = 4 拍 SubBytes + 1 拍 ShiftRows+MixColumns+AddRoundKey。
2. 假设把 S-box 例化数从 1 个增加到 16 个（每个字节一个），SubBytes 缩成 **1 拍**。计算新的每轮拍数与 AES-128 总拍数。
3. 用本节的吞吐公式估算新吞吐（假设时钟频率不变）。

**需要观察的现象（推导结论）**：

- 每轮从 5 拍降到 2 拍（1 拍 SubBytes + 1 拍其余）。
- AES-128 的 `next` 阶段从 \(1 + 10 \times 5 = 51\) 拍降到 \(1 + 10 \times 2 = 21\) 拍。
- 吞吐提升约 \(51/21 \approx 2.4\times\)（频率不变前提下）。

**预期结果**：你应当得出结论——**方向 A 是性价比最高的吞吐改造**，但它要求改写 `encipher_ctrl`/`decipher_ctrl` 状态机（删掉 `sword_ctr` 的 4 拍循环），并且 S-box 面积 ×16。是否划算，取决于目标工艺下 S-box 的面积成本与你对吞吐的需求。

> 待本地验证：以上拍数与吞吐比为按 u2-l5 时序结论的纸面推导；实际改完后需用 `tb_aes_encipher_block`/`tb_aes` 回归验证功能不变，再用综合工具核对面积与频率。

#### 4.3.5 小练习与答案

**练习 1**：为什么"轮流水线（方向 B）"会破坏 `sbox_mux` 的工作前提？
<details><summary>参考答案</summary>
`sbox_mux` 的前提是"init 与 next 分阶段、同一时刻只有一个消费者"。轮流水线让多个块同时处于不同轮次，每轮都需要 S-box 做替换；若仍只有一个共享 S-box，多块会在同一拍争抢它。所以轮流水线要求每级配备自己的 S-box，单点共享的前提不再成立。</details>

**练习 2**：方向 A（SubBytes 全并行）能把 AES-128 的 next 阶段从 51 拍降到多少拍？吞吐提升约几倍？
<details><summary>参考答案</summary>
每轮从 5 拍降到 2 拍（1 拍 SubBytes + 1 拍 ShiftRows+MixColumns+AddRoundKey），AES-128 的 next 阶段从 \(1+10\times5=51\) 拍降到 \(1+10\times2=21\) 拍。在时钟频率不变的前提下，吞吐提升约 \(51/21 \approx 2.4\times\)。</details>

---

### 4.4 回归测试

#### 4.4.1 概念说明

二次开发最大的风险不是"改不动"，而是"改完不知道有没有改坏别的地方"。回归测试（regression test）就是每次改动后**重新运行全部测试**，确认所有旧行为依然正确。

本仓库为回归测试准备了得天独厚的条件：作者为**每个设计模块都配了独立 testbench**（u3-l3 详述），加上 NIST/FIPS 已知应答作为"金标准"。这意味着：

- 任何一次改动，都可以用**同一批标准向量**重新验证。
- 因为这些向量来自 AES 标准本身，**一个正确的 AES 核永远应当产出这些结果**——如果改完后某组向量不再通过，几乎可以断定改动引入了 bug。

#### 4.4.2 核心流程

一次完整的回归测试流程：

1. **编译并运行 `tb_aes`**（顶层）：确认端到端 16 组（改完后是 17/18 组）NIST 向量全通过、`error_ctr=0`。
2. **逐层运行其余 4 个 testbench**：`tb_aes_core`、`tb_aes_encipher_block`、`tb_aes_decipher_block`、`tb_aes_key_mem`，每个都应报告 0 错误。
3. **按失败模式定位**：若只有顶层失败而子模块 testbench 全过，问题多半在顶层接线/接口；若某个子模块 testbench 失败，bug 就被隔离到那个模块（u3-l3 的分层定位思想）。
4. **记录基线**：把"改动前的通过数与关键波形"作为基线，改动后与之对照。

#### 4.4.3 源码精读

**自检计数的核心**——`error_ctr` 与 `tc_ctr`，声明在 [rtl/tb_aes.v:L70-L72](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L70-L72)：

```verilog
reg [31:0] cycle_ctr;   // 周期计数（调试用）
reg [31:0] error_ctr;   // 失败用例数
reg [31:0] tc_ctr;      // 已执行用例数
```

`init_sim` 把它们清零——[rtl/tb_aes.v:L196-L210](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L196-L210)；每个用例失败时 `error_ctr` 自增——[rtl/tb_aes.v:L374](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L374-L374)。

**最终定论**由 `display_test_results` 给出——[rtl/tb_aes.v:L175-L187](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L175-L187)：

```verilog
if (error_ctr == 0)
  $display("*** All %02d test cases completed successfully", tc_ctr);
else
  $display("*** %02d tests completed - %02d test cases did not complete successfully.",
           tc_ctr, error_ctr);
```

**主流程串起这一切**——`main` initial 块，[rtl/tb_aes.v:L488-L506](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L488-L506)：`init_sim → reset_dut → aes_test → display_test_results → $finish`。这就是回归判定的执行骨架。

**全部 testbench 清单**（`rtl/` 目录，5 个）：

| 文件 | 验证对象 | 用途 |
|------|----------|------|
| `tb_aes.v` | 顶层 `aes`（含总线） | 端到端 NIST 回归 |
| `tb_aes_core.v` | `aes_core`（无总线 wrapper） | 核心调度 + 子模块簇 |
| `tb_aes_encipher_block.v` | 加密车间 | 单模块加解密正确性 |
| `tb_aes_decipher_block.v` | 解密车间 | 单模块解密正确性 |
| `tb_aes_key_mem.v` | 密钥扩展 | 11/15 把轮密钥逐把核对 |

#### 4.4.4 代码实践

> 实践目标：用 Icarus Verilog（`iverilog`）命令行编译并运行 `tb_aes`，把它作为你后续所有改动的回归基线。

**操作步骤**：

1. 确认 `iverilog` 已安装（`iverilog -V`）。若没有，可用 ModelSim 打开 `Pre-Synthesis Simulation/simulation.mpf`（注意 u1-l5 提醒：该文件路径写死成作者 Windows 绝对路径，换机需修正）。
2. 编译顶层 testbench 及其依赖的全部设计文件，并指定顶层模块为 `tb_aes`：

   ```bash
   iverilog -s tb_aes -o aes_sim.out \
     rtl/aes.v rtl/aes_core.v \
     rtl/aes_encipher_block.v rtl/aes_decipher_block.v \
     rtl/aes_sbox.v rtl/aes_inv_sbox.v rtl/aes_key_mem.v \
     rtl/tb_aes.v
   ```

3. 运行：

   ```bash
   vvp aes_sim.out
   ```

4. 观察终端最后的总结行。

**需要观察的现象**：

- 终端依次打印各用例的 `TC ... started` / `TC ... successful`。
- 最后打印 `*** All 16 test cases completed successfully`（若已完成 4.1 节新增用例，则为 17 或 18）。

**预期结果**：`error_ctr` 为 0，全部用例通过。这就是你的**回归基线**——以后每次改完，都应重新得到同样口径的"全通过"结论。

> 待本地验证：本环境未替你执行上述命令，编译是否一次通过、是否报出 16/17 个通过，需你本地运行确认。命令中文件列表为本仓库 `rtl/` 实际存在的设计文件，可信。

#### 4.4.5 小练习与答案

**练习 1**：如果改完代码后 `tb_aes` 失败、但 `tb_aes_encipher_block` 和 `tb_aes_key_mem` 都通过，bug 最可能在哪一层？
<details><summary>参考答案</summary>
最可能在**顶层接线/接口层**（`aes.v` 的地址译码、寄存器落地、`core_key`/`core_block` 拼接，或 `aes_core` 的多路选择接线），因为加解密车间与密钥扩展这两个叶子模块本身已被各自的 testbench 证明正确。这正是分层测试把 bug 锁定到具体模块的价值。</details>

**练习 2**：为什么 NIST/FIPS 已知应答向量适合作为长期回归基线？
<details><summary>参考答案</summary>
因为它们来自 AES 标准本身，是"正确实现应当产出的确定值"，与具体实现无关。任何一次改动只要没有破坏算法正确性，就必定仍产出这些值；一旦不通过，就能立刻判定改动引入了功能性 bug。它们如同不变的标尺，适合长期复用。</details>

---

## 5. 综合实践

把本讲四个模块串起来，完成一次"最小闭环"的二次开发：

**任务**：为 AES 核**新增一个 FIPS-197 AES-128 加密 + 解密往返用例对**，并跑通全套回归。

要求：

1. **新增用例（4.1）**：在 `tb_aes.v` 的 `aes_test` 中，新增 FIPS-197 AES-128 的加密用例（明文 → 期望密文）与解密用例（密文 → 期望明文），数据见 4.1.4 节。给它们分配新编号（如 `0x20`/`0x21`）。
2. **接口一致性自查（4.2）**：在改动前后各读一遍 [rtl/aes.v:L27-L50](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L27-L50) 与 [rtl/tb_aes.v:L25-L58](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L25-L58)，确认你**没有**误改地址表（本次只加测试用例，不应动设计源码）。
3. **回归验证（4.4）**：用 4.4.4 的 `iverilog` 命令编译运行 `tb_aes`，确认输出变成 `*** All 18 test cases completed successfully`（原 16 + 新增 2），`error_ctr=0`。
4. **吞吐反思（4.3）**：在交付说明里写一段——如果后续要把吞吐提到当前的 2 倍以上，你会选 4.3 表格里的哪个方向？为什么？需要改动哪些模块、会牺牲什么？

**交付物**：

- 改动后的 `tb_aes.v`（仅新增用例，不动设计文件）。
- 一次回归运行的终端输出截图/文本。
- 一段 4.3 的取舍说明。

> 待本地验证：综合实践的运行结论（18 个用例全通过）需你本地执行确认；本讲义只提供步骤与判断标准。

## 6. 本讲小结

- **加测试用例是最低风险的二次开发入口**：得益于"数据与流程分离"，新增 NIST/FIPS 用例只需在 `aes_test` 末尾追加一行模板调用，加密/解密靠输入与期望是否对调来区分。
- **改地址/接口是高风险操作**：地址表在 `aes.v`（`localparam`，权威）与 `tb_aes.v`（`parameter`，镜像）里被重复定义，且有 `CTRL_ENCDEC_BIT/KEYLEN_BIT` 这样的死参数陷阱；改动必须两边同步并搜索所有引用点。
- **流水线化是"用面积换时间"的反向操作**：方向 A（SubBytes 全并行）性价比最高，但要把 `sword_ctr` 的 4 拍循环改掉、S-box 面积 ×16；方向 B/C 直接与"共享单个 S-box"的核心取舍冲突。
- **回归测试是二次开发的安全网**：仓库自带的 5 个分层 testbench + NIST/FIPS 金标准向量，能在每次改动后把 bug 锁定到具体模块。
- **顶层 `result` 是车间实时寄存器的裸镜像**（[rtl/aes_core.v:L145](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L145-L145)），流水线化时需要改成寄存输出，这是常被忽略的连带改动。
- **诚实结论**：本讲的所有"运行结果"均标注了"待本地验证"，因为讲义不替你执行命令；判断改对与否的唯一标准是 `tb_aes` 报告 `error_ctr=0`。

## 7. 下一步学习建议

本讲是手册的终点，但不是学习的终点。建议你沿以下方向继续：

1. **动手做综合实践**：把第 5 节的闭环任务完整跑一遍，得到自己的回归基线。这是把"读懂"转成"会改"的关键一步。
2. **深入 SubBytes 流水化**：若对吞吐有需求，尝试实现 4.3 的方向 A——改写 `aes_encipher_block.v` 的 `encipher_ctrl` FSM，删掉 `sword_ctr` 循环、例化多个 S-box，用 `tb_aes_encipher_block` 回归。这是从"改测试"进阶到"改设计"的练手项目。
3. **接其他总线**：尝试把 `aes.v` 的简单 `cs/we/address` 接口封装成 AXI4-Lite 或 APB 从接口，练习 4.2 的接口改动涟漪控制。
4. **对照其他开源 AES 核**：如 OpenCores 上的 aes_core、或加法流水线化的 high-throughput 版本，对比它们在 S-box 复用与流水深度上的不同取舍，加深对 u3-l4 与本讲 4.3 的理解。
5. **回到源码**：重新通读 `rtl/aes_core.v` 的三个组合块（`sbox_mux`/`encdec_mux`/`aes_core_ctrl`），体会"调度中枢"如何用最少的逻辑把 4 个子模块拧成一个完整的 AES 核——这是本工程最值得借鉴的架构精髓。
