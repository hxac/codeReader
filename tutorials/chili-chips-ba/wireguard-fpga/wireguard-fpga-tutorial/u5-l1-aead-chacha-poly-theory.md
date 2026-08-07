# AEAD/ChaCha20-Poly1305 原理与 RFC8439

## 1. 本讲目标

WireGuard 之所以又快又安全，核心在于它用 **ChaCha20-Poly1305** 这一套 AEAD（Authenticated Encryption with Associated Data，带相关数据的认证加密）算法来保护每一个用户数据包。本讲是 Unit 5「ChaCha20-Poly1305 加密硬件」的**理论地基**，目标是：

- 理解什么是 AEAD 的「**加密—后—认证**」构造，以及它为什么是安全的默认姿势。
- 拆开 ChaCha20 **流密码**的内部结构，看懂它如何用一段「密钥流」与明文做异或。
- 拆开 Poly1305 **消息认证码（MAC）**，看懂它如何在一棵多项式求值树上算出 16 字节的 tag。
- 读懂 RFC8439 规定的「**AAD ‖ 密文 ‖ 长度**」认证数据组装格式，以及 256 位 key / 96 位 nonce / 16 字节 tag 之间的关系。
- 用一个真实软件库复现 RFC8439 §2.8.2 的已知答案测试向量，建立「能被独立验证」的直觉。

本讲只讲**算法原理**，不碰硬件实现（那是 u5-l2 之后的 PipelineC/Pypeline 数据流）。学完后你会带着一份正确的密码学心智模型，进入后面几讲去读 RTL。

> **承接 u2-l1**：项目把系统分成**控制面**（软 CPU 跑 WireGuard 协议、握手）和**数据面**（RTL 线速转发）。两个面**都需要**这套 AEAD：控制面在握手报文里用它，数据面在每一个用户包里用它。正因为两个面都要用，本项目里存在**两份**实现——一份可移植 C（控制面，本讲引用），一份由 PipelineC/Pypeline 生成的 RTL（数据面，Unit 5 后续讲义）。两份实现背后是**同一套 RFC8439 原理**，也就是本讲的内容。

## 2. 前置知识

本讲是密码学理论讲，不需要你会 Verilog，但有几个术语先约定清楚：

- **对称密码（symmetric cipher）**：加密和解密用**同一把密钥**。WireGuard 用的 ChaCha20 就是对称的——对方要解密，必须持有你加密时用的那把 key。
- **流密码（stream cipher）**：把密钥扩展成一段看起来随机的「密钥流（keystream）」，再用它与明文**逐字节异或**得到密文。ChaCha20 是流密码。
- **MAC（Message Authentication Code，消息认证码）**：用密钥对一段数据算出一个固定长度的「指纹」（tag）。接收方用**同一把密钥**重算指纹，若与收到的 tag 不一致，就说明数据被篡改过。Poly1305 是一种 MAC。
- **AEAD**：把「加密」和「认证」**绑在一起**做成一个算法。它同时保证**机密性**（别人看不到内容）和**完整性/真实性**（别人改了会被发现）。
- **nonce（一次性随机数）**：每次加密都用一个**不重复**的值。对 ChaCha20 这种流密码来说，**同一把 key + 同一个 nonce 绝不能用第二次**，否则密钥流会重复，攻击者用两段密文异或就能抵消掉密钥流、泄露明文。
- **AAD（Additional Authenticated Data，相关认证数据）**：一段**需要被认证、但不需要被加密**的数据。WireGuard 把外层包头放进 AAD——包头要明文传输（路由器才能转发），但绝不能被篡改。

> **小端字节序提醒**：ChaCha20 和 Poly1305 在 RFC8439 里**全部**按小端（least-significant byte first）解释多字节整数。这一点和本项目的 AXIS 数据面总线（小端）一致，但和网络头（大端）相反。本讲遇到的字节序默认是小端。

## 3. 本讲源码地图

本讲引用的关键文件如下。前三个是项目的**软件控制面**可移植 C 加密库，第四个是**数据面** PipelineC 工程的统一头文件，后两个是验证用的参考模型与测试向量——它们共同定义了「正确答案长什么样」。

| 文件 | 作用 |
|------|------|
| [`2.sw/app/chacha20.c`](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/chacha20.c) | ChaCha20 流密码的纯 C 实现：常量、quarter round、block、init、encrypt。 |
| [`2.sw/app/poly1305.c`](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/poly1305.c) | Poly1305 MAC 的纯 C 实现：r 钳位、320 位 limb 数学、按块累加—乘—取模。 |
| [`2.sw/app/chacha20poly1305.c`](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/chacha20poly1305.c) | AEAD 组装层：用 ChaCha20(counter=0) 派生 Poly1305 一次性密钥、加密(counter=1)、拼装认证数据、算 tag、验 tag。 |
| [`3.build/pipelinec_build/src/chacha20poly1305/chacha20poly1305.h`](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305/chacha20poly1305.h) | 数据面工程统一包含的 ChaCha20+Poly1305 子模块头。 |
| [`3.build/pypeline_build/src/chacha20poly1305/aead_ref_model.py`](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/chacha20poly1305/aead_ref_model.py) | 用 `cryptography` 包做的 RFC8439 参考模型，内嵌 §2.8.2 已知答案自检。 |
| [`3.build/pypeline_build/src/chacha20poly1305/tb_common.py`](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/chacha20poly1305/tb_common.py) | 仿真测试台的共享测试向量（KEY/NONCE/AAD/明文），刻意覆盖各种长度边界。 |

> **一个必须先说清的诚实提示**：项目里这份 C 版 `poly1305.c` 的 320 位 limb 数学（`uint320_mul` / `uint320_mod_prime`）**存在已知的精度缺陷**——它截断了 64×64 位乘积的高位、取模掩码写错（用了 `0x3FFFFFFFFFF` 而非 `0x3`）、并丢弃了高位 limb。因此它产出的 tag **不能**与合规 peer 互通。这个 bug 已在 Python 重写版 [`3.build/pypeline_build/src/poly1305/poly1305.py`](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/poly1305/poly1305.py) 里修掉，**修复细节是 u5-l6 的主题**。本讲讲的是**RFC8439 的正确算法**，C 代码只用来展示**结构骨架**（哪些函数、怎么编排）；凡是涉及 limb 数学正确性的地方，都以 RFC 与参考模型为准。

---

## 4. 核心概念与源码讲解

### 4.1 ChaCha20 流密码

#### 4.1.1 概念说明

ChaCha20 是 Daniel J. Bernstein 设计的流密码，属于 **ARX**（Add-Rotate-Xor，加法—循环移位—异或）家族。它的核心思想分两步：

1. **扩展**：把 256 位 key、96 位 nonce、一个 32 位计数器，喂进一个固定函数，**生成 64 字节（512 位）的伪随机密钥流**。
2. **异或**：把这 64 字节密钥流与 64 字节明文**逐字节异或**，得到 64 字节密文。

因为是异或，所以**解密就是再异或一次同样的密钥流**——加密和解密是同一个操作。明文每 64 字节为一块，每处理完一块，计数器加 1，生成下一段密钥流，如此循环。

ARX 的好处是：**只用到加法、按位异或、循环移位**这三种对硬件/软件都极友好的运算，没有任何查表（S-box），因此在 CPU 上抗缓存时序攻击、在 FPGA 上面积小、好流水。

#### 4.1.2 核心流程

ChaCha20 维护一个 **4×4 矩阵**，共 16 个 32 位字（512 位）。矩阵初始化布局如下：

```
┌────────────┬────────────┬────────────┬────────────┐
│  常量 c0   │  常量 c1   │  常量 c2   │  常量 c3   │   ← "expand 32-byte k"
├────────────┼────────────┼────────────┼────────────┤
│   key[0]   │   key[1]   │   key[2]   │   key[3]   │   ← 256 位密钥，8 个字
├────────────┼────────────┼────────────┼────────────┤
│   key[4]   │   key[5]   │   key[6]   │   key[7]   │
├────────────┼────────────┼────────────┼────────────┤
│  counter   │  nonce[0]  │  nonce[1]  │  nonce[2]  │   ← 计数器 + 96 位 nonce
└────────────┴────────────┴────────────┴────────────┘
```

四个常量是 ASCII 字符串 `"expand 32-byte k"` 的小端编码。

生成一个密钥流块的流程：

```
1. 用 key/nonce/counter 填好初始矩阵 state
2. 在 state 上跑 20 轮（= 10 个"双轮"：4 个列轮 + 4 个对角轮）
   每个 quarter round 改 4 个字：a+=b; d=rotl(d^a,16); c+=d; b=rotl(b^c,12);
                                  a+=b; d=rotl(d^a, 8); c+=d; b=rotl(b^c, 7);
3. 把"轮变换后的矩阵" + "初始矩阵" 逐字相加 → 64 字节密钥流块
4. 密文 = 明文 XOR 密钥流块；counter++，处理下一块
```

> **为什么第 3 步还要加回初始矩阵？** 因为 20 轮 ARX 变换是单向的（难以逆推），但 ChaCha20 要的是一个「看起来随机」的输出。直接输出轮变换结果会让初始 key 通过线性关系泄露；加回初始状态这一步（称为 *final addition*）把可逆的线性叠加和不可逆的混淆混在一起，才得到安全的密钥流。

#### 4.1.3 源码精读

**常量与初始矩阵**——注意这些常量是 `"expand 32-byte k"` 的小端编码（代码注释里 `"apxe"` 等是内存中的字节序，读成 ASCII 时要反过来看成 `"expa"`、`"nd 3"`、`"2-by"`、`"te k"`）：

[chacha20.c:58-81](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/chacha20.c#L58-L81) 中，`chacha20_init` 把 4 个常量、8 个 key 字、1 个 counter、3 个 nonce 字填进 16 字状态：

```c
state->state[0] = 0x61707865; // "apxe" → 小端读即 "expa"
state->state[1] = 0x3320646e; // "3 dn" → "nd 3"
...
state->state[12] = counter;                 // 计数器
for (int i = 0; i < 3; i++)
    state->state[13 + i] = ((uint32_t *)nonce)[i]; // nonce 占 3 个字
```

**quarter round（四分之一轮）**——这是 ChaCha20 唯一的核心运算，`a/b/c/d` 是矩阵里选出的 4 个字：

[chacha20.c:20-30](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/chacha20.c#L20-L30)

```c
static void quarter_round(uint32_t *a, uint32_t *b, uint32_t *c, uint32_t *d) {
    *a += *b; *d = rotate_left(*d ^ *a, 16);
    *c += *d; *b = rotate_left(*b ^ *c, 12);
    *a += *b; *d = rotate_left(*d ^ *a, 8);
    *c += *d; *b = rotate_left(*b ^ *c, 7);
}
```

**block 函数**——跑 10 个双轮（每个双轮 = 4 个列 quarter round + 4 个对角 quarter round，共 8 次，乘 10 次 = 80 次 quarter round = 20 轮），最后做 *final addition* 加回初始状态：

[chacha20.c:33-56](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/chacha20.c#L33-L56)

```c
for (int i = 0; i < 10; i++) {
    // 4 个列轮
    quarter_round(&t[0], &t[4], &t[8],  &t[12]);
    quarter_round(&t[1], &t[5], &t[9],  &t[13]);
    quarter_round(&t[2], &t[6], &t[10], &t[14]);
    quarter_round(&t[3], &t[7], &t[11], &t[15]);
    // 4 个对角轮
    quarter_round(&t[0], &t[5], &t[10], &t[15]);
    quarter_round(&t[1], &t[6], &t[11], &t[12]);
    quarter_round(&t[2], &t[7], &t[8],  &t[13]);
    quarter_round(&t[3], &t[4], &t[9],  &t[14]);
}
for (int i = 0; i < 16; i++)
    output->state[i] = temp_state[i] + state->state[i]; // final addition
```

**加解密主循环**——每 64 字节一块，异或，counter 自增。注意 `length > 64 ? 64 : length` 处理最后不足一块的尾块：

[chacha20.c:84-106](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/chacha20.c#L84-L106)

```c
while (length > 0) {
    chacha20_init(&state, key, nonce, counter);
    chacha20_block(&state, &block);
    size_t chunk_size = length > 64 ? 64 : length;
    for (size_t i = 0; i < chunk_size; i++)
        out[i] = in[i] ^ block_bytes[i];   // 密文 = 明文 XOR 密钥流
    counter++;
    ...
}
```

而 [chacha20.h:55](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/chacha20.h#L55) 把解密直接定义成加密——`#define chacha20_decrypt chacha20_encrypt`——这正是流密码「解密=再异或一次」的直接体现。同时该头还给出了三个尺寸常量：`CHACHA20_KEY_SIZE 32`（256 位 key）、`CHACHA20_NONCE_SIZE 12`（96 位 nonce）、`CHACHA20_BLOCK_SIZE 64`（每块 64 字节）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：建立「状态矩阵 → 20 轮 → final addition → 异或」的完整心智链路。
2. **操作步骤**：
   - 打开 [`chacha20.c`](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/chacha20.c)。
   - 在 `chacha20_block` 里数清楚：`for (i=0; i<10)` 循环体里一共有**几次** `quarter_round` 调用？乘以 10 后，总 quarter round 次数是多少？据此验证「ChaCha20 = 20 轮」。
   - 在 `chacha20_encrypt` 里追踪：当 `length = 100` 时，循环跑几次？每次 `counter` 分别是多少？（答案：2 次，counter = 起始值、起始值+1；第二次只异或前 36 字节有效。）
3. **需要观察的现象**：`temp_state`（轮变换结果）与 `state->state`（初始矩阵）在 final addition 处逐字相加；`block_bytes` 把这 16 个 32 位字按小端当成 64 字节直接与明文异或。
4. **预期结果**：你应能在纸上把 `quarter_round` 的 4 行运算对应到 ARX 的「加—移—异或」，并理解为什么 `chacha20_decrypt` 不需要单独实现。

#### 4.1.5 小练习与答案

- **练习 1**：ChaCha20 矩阵里 `state[12]` 放的是什么？为什么它每生成一块就要加 1？
  - **答案**：放 32 位块计数器（counter）。它保证每块的初始矩阵都不同，从而每块的密钥流都不同；若两块 counter 相同，就会生成**相同的密钥流**，破坏安全性。
- **练习 2**：为什么 `chacha20_decrypt` 可以直接 `#define` 成 `chacha20_encrypt`？
  - **答案**：因为流密码的密文 = 明文 ⊕ 密钥流，而密钥流只依赖 key/nonce/counter。解密时用同样的 key/nonce/counter 重算密钥流，再做一次异或：\( (\text{明文} \oplus \text{流}) \oplus \text{流} = \text{明文} \)。

---

### 4.2 Poly1305 MAC

#### 4.2.1 概念说明

Poly1305 是一种**一次性**消息认证码（one-time MAC），属于 Wegman–Carter 构造。它的密钥是 32 字节，拆成两半：

- **r**（前 16 字节）：多项式求值的「底数」，会被**钳位（clamp）**——清掉若干位，使乘法在弱平台上更快、更抗时序攻击。
- **s**（后 16 字节）：最后一次性「加」上去的掩码，**不**钳位、**不**取模。

它工作在素数域上，素数为：

\[
p = 2^{130} - 5
\]

算法把消息切成 16 字节的块，每块解释成一个小端整数，再在尾部「贴一个 1 比特」做定界（区分 `0x00 0x01` 和 `0x01` 等不同长度），然后做一轮「**累加 → 乘 r → 模 p**」。等价地，整个消息被当成一个多项式在 \(r\) 上求值：

\[
\text{acc} = \bigl(\,c_1 r^{q} + c_2 r^{q-1} + \dots + c_q r\bigr) \bmod p
\]

其中每个 \(c_i\) 是「块 + 贴 1 比特」得到的小端整数。最后把一次性掩码 `s` 加上去，取低 128 位作为 16 字节 tag：

\[
\text{tag} = (\text{acc} + s) \bmod 2^{128}
\]

关键直觉：tag 是消息在「密钥底数 r」下的多项式求值，**改一个比特都会让结果剧变**；而 `s` 保证即使求值结果被猜到，没有 s 也伪造不出合法 tag。

> **为什么是一次性？** r 和 s 每条消息都要换新的。在 ChaCha20-Poly1305 里，这个「每消息一次性密钥」是用 ChaCha20 自己派生的（见 4.3）。

#### 4.2.2 核心流程

代码用 Horner 法（秦九韶算法）迭代，与上面的多项式等价但更省存储：

```
acc = 0
对消息的每一个 16 字节块 n（最后一块可能不足 16 字节）:
    n = 小端解释成整数
    在 n 的"数据末尾"贴一个 1 比特          ← 定界
        · 满 16 字节块: 设第 128 位 = 1（即 |上 2^128）
        · 不满 16 字节块: 在数据后那一字节写 0x01
    acc = acc + n                            ← 累加
    acc = (acc * r) mod p                    ← 乘底数、取模
acc = acc + s                                ← 一次性掩码
tag = acc 的低 128 位（16 字节，小端）
```

**钳位（clamp）r 的规则**：把 r 的 4 个最高 4 位（每 32 位字的高 4 位）清零，并清掉 3 个最低 2 位。这让 r 在某些 limb 上只有少量比特，乘法更快。

**取模 \(p = 2^{130}-5\) 的原理**：因为 \(2^{130} \equiv 5 \pmod p\)，所以任何高于 \(2^{130}\) 的部分都可以「乘 5 折回低位」。这正是下文 `uint320_mod_prime` 想做的事（虽然 C 版的掩码写错了）。

#### 4.2.3 源码精读

**素数常量与 320 位大整数类型**——用 5 个 64 位 limb 表示一个最高 320 位的中间值，用来容纳「累加器(≤130 位) × r(≤128 位)」的乘积：

[poly1305.c:46-59](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/poly1305.c#L46-L59)

```c
static const uint64_t PRIME[3] = {0xFFFFFFFFFFFFFFFB, 0xFFFFFFFFFFFFFFFF, 0x3}; // 2^130-5
typedef struct { uint64_t limbs[5]; } uint320_t; // 5×64=320 位
```

**r 的钳位**——清掉 4 个高 4 位与 3 个低 2 位：

[poly1305.c:66-75](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/poly1305.c#L66-L75)

```c
static void clamp(uint8_t r[16]) {
    r[3] &= 15;  r[7] &= 15;  r[11] &= 15;  r[15] &= 15; // 清高 4 位
    r[4] &= 252; r[8] &= 252; r[12] &= 252;             // 清低 2 位
}
```

**主循环（满块 + 尾块）**——这就是 Horner 法的逐块落地。注意满块与尾块「贴 1 比特」的方式不同：

[poly1305.c:236-276](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/poly1305.c#L236-L276)

```c
for (size_t i = 0; i < blocks; i++) {
    bytes_to_uint320(&n, message + i*16, 16);
    n.limbs[2] |= 0x1;          // 满块: 设第 128 位 = 1（2^128）
    uint320_add(&a, &a, &n);    // acc += n
    uint320_mul(&a, &temp, &r); // acc *= r   ← ⚠ C 版此处有截断 bug
    uint320_mod_prime(&a);      // acc %= p   ← ⚠ C 版此处掩码有 bug
}
if (remain > 0) {               // 尾块: 在数据后写 0x01
    uint8_t last_block[16] = {0};
    memcpy(last_block, message + blocks*16, remain);
    last_block[remain] = 0x01;  // 贴 1 字节(=贴 1 比特的字节版)
    ...                         // 同样 add → mul → mod
}
```

**加一次性掩码 s，输出 tag**——注意 `acc + s` 之后**不再取模 p**，而是直接取低 16 字节：

[poly1305.c:279-282](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/poly1305.c#L279-L282)

```c
uint320_add(&a, &a, &s);          // acc += s
memcpy(auth_tag, &a, 16);         // 取低 128 位
```

**恒定时间比较**——验证 tag 时用两个 64 位字的 `==` 比较，避免逐字节短路带来的时序泄露：

[poly1305.c:292-300](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/poly1305.c#L292-L300)

> ⚠️ **再次诚实提示**：[poly1305.c:122-150](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/poly1305.c#L122-L150) 的 `uint320_mul` 用 `uint64_t product = a->limbs[i] * b->limbs[j]`，**两个 64 位数相乘会溢出成 64 位**，丢了高 64 位；[poly1305.c:157-203](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/poly1305.c#L157-L203) 的 `uint320_mod_prime` 用了 `mask = 0x3FFFFFFFFFF`（应为 `0x3`）并丢弃了 limb 3/4。这两处使本文件产出的 tag **不合规**。**正确的取模应该是：把 ≥ \(2^{130}\) 的部分乘 5 折回低位**（见 u5-l6 修复版 `_MASK = 0x3`）。本模块讲算法骨架时以 RFC8439 与参考模型为准。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：把「Horner 多项式求值」与代码循环一一对应。
2. **操作步骤**：打开 [`poly1305.c`](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/poly1305.c)，在 `poly1305_mac` 里：
   - 找到 `clamp(r_bytes)`，确认它发生在**读消息之前**（r 先钳位再用）。
   - 找到满块 `n.limbs[2] |= 0x1` 与尾块 `last_block[remain] = 0x01`，对比这两种「贴 1 比特」的写法。
   - 数清楚每块的运算顺序是 `add → mul → mod`（而不是 `mul → add → mod`）。
3. **需要观察的现象**：累加器 `a` 初始化为 0，每块都被 `r` 乘一次；最后 `s` 只加一次、不参与取模。
4. **预期结果**：你能口头复述「tag = ((…((c1·r + c2)·r + c3)·r + … ) mod p) + s」这条 Horner 链，并指出 C 版的 bug 出在「乘」和「模」这两步的数值实现上，而非算法骨架。

#### 4.2.5 小练习与答案

- **练习 1**：为什么满块用「设第 128 位 = 1」、尾块用「写一个 0x01 字节」来「贴 1 比特」？它们等价吗？
  - **答案**：等价，都是「在数据之后追加一个值为 1 的字节」。满块数据正好 16 字节（占满第 0~127 位），追加的 1 比特落在第 128 位，即 limb[2] 的 bit0；尾块数据不足 16 字节，1 字节 `0x01` 直接贴在数据末尾的字节位置上。两种写法都实现了 RFC8439 的「pad with 0x01」。
- **练习 2**：r 为什么要 clamp？s 为什么不 clamp？
  - **答案**：clamp r 可清掉某些高位、低位，使 limb 乘法中涉及的有效比特更少，既加速又便于恒定时间实现；s 只是最后一次性相加的掩码，不参与乘法，钳位它没有意义，反而会削弱它的熵。

---

### 4.3 AEAD 认证数据格式（ChaCha20-Poly1305 组合）

#### 4.3.1 概念说明

把 ChaCha20 和 Poly1305 拼成一个 AEAD，RFC8439 采用的是「**加密—后—认证**」（Encrypt-then-MAC）构造。直觉上：

1. 先用 ChaCha20 把明文加密成密文。
2. 再用 Poly1305 对「**AAD + 密文 + 它们的长度**」算一个 tag。
3. 把 tag 附在密文后面一起发出。

**为什么是「先加密、再对密文认证」，而不是反过来？** 因为这样才能保证**只有看到完整、未被篡改的密文后，接收方才会去解密**——任何对密文或 AAD 的篡改都会让 tag 校验失败，从而密文根本不会被解密，攻击者拿不到任何关于明文的信息。这是被证明安全的构造（比「先认证后加密」更稳健）。

这里有一个精妙的**密钥派生**：Poly1305 需要「每消息一次性密钥」，RFC8439 规定**用 ChaCha20 自己来派生**它——用 counter=0 生成第一个 64 字节密钥流块，取前 32 字节作为 Poly1305 的 r‖s。于是：

- **counter = 0**：派生 Poly1305 一次性密钥（**不**用于加密明文）。
- **counter = 1 起**：才用于加密明文。

这就是为什么 `chacha20poly1305_encrypt` 里两次调用 ChaCha20 用了**不同的起始 counter**。

#### 4.3.2 核心流程

ChaCha20-Poly1305 加密一帧数据：

```
输入: key(32B), nonce(12B), aad, aad_len, plaintext, plaintext_len
1. poly_key = ChaCha20(key, nonce, counter=0) 的前 32 字节   ← 派生一次性 MAC 密钥
2. ciphertext = plaintext XOR ChaCha20(key, nonce, counter=1) ← 真正加密
3. 组装认证数据 auth_data:
       auth_data = AAD ‖ pad16(AAD) ‖ ciphertext ‖ pad16(ciphertext) ‖ le64(aad_len) ‖ le64(ct_len)
   其中 pad16(x) = 补 0 到 16 的整数倍
4. tag = Poly1305_MAC(poly_key, auth_data)
输出: ciphertext, tag(16B)
```

**认证数据的拼接格式**（每段都 16 字节对齐，最后跟两个 64 位小端长度）：

```
┌───────────────────┬──────────┬──────────────────┬──────────┬────────┬────────┐
│       AAD         │ pad16    │     ciphertext   │ pad16    │ le64    │ le64    │
│                   │ (补0)    │                  │ (补0)    │aad_len  │ct_len   │
└───────────────────┴──────────┴──────────────────┴──────────┴────────┴────────┘
   ← 16 字节对齐 →              ← 16 字节对齐 →               ← 各 8 字节 →
```

**为什么要把长度也认证进去？** 为了防「截断/拼接攻击」。如果不认证长度，攻击者可能把两条消息的块拼起来、或截掉尾巴，构造出另一个能通过校验的消息。把 `aad_len` 和 `ct_len` 用小端 64 位写进认证数据，就彻底锁死了「数据是什么、多长」。

**解密侧的「先验后解」**：解密时先**重新算 tag 并比对**，**通过之后才解密**。代码里对应 `chacha20poly1305_decrypt` 的顺序：先 `poly1305_mac` + `poly1305_verify`，验证失败直接 `return -1`，验证通过才 `chacha20_decrypt`。这一点在硬件数据面里被强化为「verify-before-forward」（明文先进 FIFO 缓冲，tag 校验通过才放行，见 u5-l4）。

#### 4.3.3 源码精读

**派生 Poly1305 一次性密钥**——用 counter=0 生成一块，取前 32 字节：

[chacha20poly1305.c:62-70](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/chacha20poly1305.c#L62-L70)

```c
static void poly1305_key_gen(uint8_t *poly1305_key, const uint8_t *key, const uint8_t *nonce) {
    uint8_t block[CHACHA20_BLOCK_SIZE] = {0};
    chacha20_encrypt(block, block, CHACHA20_BLOCK_SIZE, key, nonce, 0); // counter=0
    memcpy(poly1305_key, block, 32);  // 前 32 字节 = r‖s
}
```

**加密主流程**——注意 counter=1 加密、认证数据组装、最后算 tag：

[chacha20poly1305.c:82-116](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/chacha20poly1305.c#L82-L116)

```c
poly1305_key_gen(poly1305_key, key, nonce);                       // counter=0 派生
chacha20_encrypt(ciphertext, plaintext, plaintext_len, key, nonce, 1); // counter=1 加密

// 认证数据 = AAD ‖ pad16 ‖ 密文 ‖ pad16 ‖ 长度
size_t aad_padding        = (16 - (aad_len % 16)) % 16;
size_t ciphertext_padding = (16 - (plaintext_len % 16)) % 16;
size_t auth_data_len = aad_len + aad_padding + plaintext_len + ciphertext_padding + 16;

memcpy(auth_data, aad, aad_len);                                        // AAD
memcpy(auth_data + aad_len + aad_padding, ciphertext, plaintext_len);   // 密文
encode_le64(lengths,     aad_len);                                      // le64(aad_len)
encode_le64(lengths + 8, plaintext_len);                                // le64(ct_len)

poly1305_mac(auth_tag, poly1305_key, auth_data, auth_data_len);         // 算 tag
```

其中 [`encode_le64`](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/chacha20poly1305.c#L49-L59) 把 64 位整数按小端写成 8 字节，正是 RFC8439 要求的长度字段编码。

**解密主流程**——先验后解，验证失败直接返回：

[chacha20poly1305.c:160-168](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/chacha20poly1305.c#L160-L168)

```c
poly1305_mac(calculated_tag, poly1305_key, auth_data, auth_data_len);
if (!poly1305_verify(auth_tag, calculated_tag))
    return -1;                              // 认证失败，绝不解密
chacha20_decrypt(plaintext, ciphertext, ciphertext_len, key, nonce, 1); // 通过才解密
```

**数据面工程的统一头**——PipelineC 工程用一份公共头把 ChaCha20 与 Poly1305 子模块拉到一起，体现了「同一套算法、两份实现」：

[chacha20poly1305.h:6-9](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305/chacha20poly1305.h#L6-L9)

```c
#pragma once
#include "chacha20/chacha20.h"
#include "poly1305/poly1305.h"
```

#### 4.3.4 代码实践（源码阅读型 + 尺寸核算）

1. **实践目标**：理解 counter=0 派生密钥与 counter=1 加密的分工，并能算出认证数据总长。
2. **操作步骤**：
   - 打开 [`chacha20poly1305.c`](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/chacha20poly1305.c)。
   - 在 `chacha20poly1305_encrypt` 里找到两处 `chacha20_encrypt`/`chacha20` 调用，确认一处 counter=0（在 `poly1305_key_gen` 内）、一处 counter=1。
   - **手算**：设 `aad_len = 29`（即 `"Additional authenticated data"` 的字节数），`plaintext_len = 114`（RFC §2.8.2 明文），求 `aad_padding`、`ciphertext_padding` 与 `auth_data_len`。
3. **需要观察的现象**：`aad_padding = (16 - 29%16) % 16 = (16-13)%16 = 3`；`ciphertext_padding = (16 - 114%16) % 16 = (16-2)%16 = 14`；`auth_data_len = 29 + 3 + 114 + 14 + 16 = 176`。
4. **预期结果**：认证数据共 176 字节、恰是 16 的整数倍（11 × 16），Poly1305 会把它切成 11 个满块处理。这个「永远是 16 的整数倍」是设计上的刻意保证。

#### 4.3.5 小练习与答案

- **练习 1**：为什么派生 Poly1305 密钥用 counter=0，而加密明文从 counter=1 开始？
  - **答案**：RFC8439 规定 block 0（counter=0）的密钥流**专用**于派生 Poly1305 一次性密钥，**不**用于加密；明文加密从 counter=1 开始。这样 MAC 密钥与加密密钥流来自**不同的块**，互不干扰。
- **练习 2**：在「加密—后—认证」构造里，如果接收方先解密、再验 tag，会有什么风险？
  - **答案**：未经验证的密文可能被攻击者篡改，先解密会让**被篡改的（可能产生明文）的数据**进入下游，泄露关于明文/密钥的信息；正确做法是**先验 tag、通过后才解密**（即代码里的顺序，也是硬件 verify-before-forward 的依据）。

---

## 5. 综合实践

**实践任务**：用一个成熟软件库对一段短明文做 ChaCha20-Poly1305 加密，并对照 RFC8439 §2.8.2 的已知答案测试向量验证密文与 tag。这**正是项目参考模型** [`aead_ref_model.py`](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/chacha20poly1305/aead_ref_model.py) 所做的事——用一个**独立的、合规的**实现来给硬件/被测代码提供「正确答案」，避免「用被测对象自己验证自己」的循环。

### 5.1 实践目标

- 亲手跑通一次完整的 AEAD 加密；
- 看到 256 位 key / 96 位 nonce / 16 字节 tag 在真实库里的形态；
- 用 RFC8439 §2.8.2 的固定向量确认你的环境产出的密文与 tag **完全合规**——为后续读硬件 RTL 建立「正确答案」基准。

### 5.2 操作步骤

1. 确认装有 Python 的 `cryptography` 包（项目参考模型就依赖它）：`pip install cryptography`。
2. 新建一个 `aead_kat.py`，照抄 RFC8439 §2.8.2 的固定输入（这些值也正是 [`aead_ref_model.py:40-46`](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/chacha20poly1305/aead_ref_model.py#L40-L46) 用的自检输入）：

```python
# 示例代码：复现 RFC8439 §2.8.2 AEAD 测试向量
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

key   = bytes(range(0x80, 0xA0))                                   # 32 字节 key
nonce = bytes([0x07,0,0,0, 0x40,0x41,0x42,0x43,0x44,0x45,0x46,0x47])# 12 字节 nonce
aad   = bytes.fromhex("50515253c0c1c2c3c4c5c6c7")                  # 12 字节 AAD
pt    = (b"Ladies and Gentlemen of the class of '99: "
         b"If I could offer you only one tip for the future, "
         b"sunscreen would be it.")

ct_and_tag = ChaCha20Poly1305(key).encrypt(nonce, pt, aad)
ciphertext, tag = ct_and_tag[:-16], ct_and_tag[-16:]

print("ct[:8] =", ciphertext[:8].hex())
print("tag    =", tag.hex())
```

3. 运行 `python aead_kat.py`。
4. （可选）把 `key`、`nonce`、`aad` 换成 [`tb_common.py:13-20`](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/chacha20poly1305/tb_common.py#L13-L20) 里项目自用的测试向量（KEY=`0x80..0x9f`、NONCE 同上、AAD=`"Additional authenticated data"`），对几条不同长度的明文加密，观察**密文长度恒等于明文长度**（流密码特性），tag 恒为 16 字节。

### 5.3 需要观察的现象

- 密文长度**严格等于**明文长度（114 字节），因为 ChaCha20 是流密码，不做填充扩张。
- tag 是固定 16 字节（128 位）。
- 同样的 `(key, nonce, aad, pt)` 每次运行结果**完全相同**——AEAD 是确定性的（不带随机性），安全性来自「nonce 不重复」而非随机。

### 5.4 预期结果

按 RFC8439 §2.8.2，应得到（这也是项目参考模型在导入时断言的值，见 [`aead_ref_model.py:47-50`](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/chacha20poly1305/aead_ref_model.py#L47-L50)）：

- 密文前 8 字节：`d31a8d34648e60db`
- 完整 16 字节 tag：`1ae10b594f09e26a7e902ecbd0600691`

若你的输出与上述一致，说明你的环境产出的 AEAD **合规**，可以作为后续验证硬件 RTL（u5-l2 起）与读项目 C 库（注意其 Poly1305 已知 bug）时的「正确答案」基准。

> 如果你拿项目自带的 C 库 `2.sw/app/chacha20poly1305.c` 跑同一组输入，**tag 会对不上**——这不是你的错，而是 4.2 中提到的 C 版 Poly1305 limb 数学缺陷。这正是项目额外维护一个 `cryptography` 参考模型的原因：**绝不用被测对象验证它自己**。修复细节见 u5-l6。

---

## 6. 本讲小结

- **AEAD = 机密性 + 完整性**。ChaCha20-Poly1305 用「**加密—后—认证**」构造：先加密、再对「AAD ‖ 密文 ‖ 长度」算 Poly1305 tag，接收方**先验 tag、通过才解密**。
- **ChaCha20 是 ARX 流密码**：把 key/nonce/counter 填进 4×4 矩阵，跑 20 轮（quarter round）+ final addition 得 64 字节密钥流，与明文异或；解密 = 再异或一次，所以 `chacha20_decrypt` 直接宏定义成 `chacha20_encrypt`。
- **Poly1305 是一次性 MAC**：在素数域 \(p = 2^{130}-5\) 上，用 Horner 法对消息块做「累加 → 乘 r → 模 p」，最后加一次性掩码 s，取低 128 位为 tag；r 要钳位、s 不钳位。
- **counter 的分工**：ChaCha20 的 **counter=0 专用于派生** Poly1305 一次性密钥（取前 32 字节），**counter=1 起**才加密明文——这是 RFC8439 的硬性规定。
- **认证数据格式**：`AAD ‖ pad16(AAD) ‖ 密文 ‖ pad16(密文) ‖ le64(aad_len) ‖ le64(ct_len)`，恒为 16 字节整数倍；把长度也认证进去可防截断/拼接攻击。
- **三处尺寸**：key 256 位（32 字节）、nonce 96 位（12 字节）、tag 128 位（16 字节）。项目里这份 C 版 `poly1305.c` 的 limb 数学有已知 bug，**合规参考**以 `cryptography` 参考模型与 RFC8439 为准（修复见 u5-l6）。

## 7. 下一步学习建议

本讲建立了**算法原理**。接下来沿着 Unit 5 继续往下，看这套算法如何变成**硬件数据流**：

- **u5-l2（PipelineC HLS 工作流）**：看 PipelineC 怎么把 C 编译成可综合 Verilog，以及「独立加密 / 独立解密 / 共享」三种设计变体——这是 ChaCha20-Poly1305 从软件算法到 RTL 的第一道桥梁。
- **u5-l3 / u5-l4（加密/解密数据流）**：看明文→ChaCha20→密文分叉→prep_auth_data→Poly1305→append/strip tag 的真实流水线，以及解密侧「verify-before-forward」如何与本讲的「先验后解」对应。
- **u5-l6（Pypeline Python 前端与 RFC 修正）**：本讲反复提到的 C 版 Poly1305 三个数学 bug（`uint320_mul` 截断、`uint320_mod_prime` 掩码错误、丢弃 limb）在那里被逐一定位与修复，是理解「为什么必须用独立参考模型」的关键。
- 若你对**控制面软件**里的加密用法（X25519、BLAKE2s、HKDF 等如何与 ChaCha20-Poly1305 一起支撑 Noise 握手）更感兴趣，可跳到 **u6-l2（软件加密原语库）**，那里讲这些原语在 bare-metal RISC-V 上的可移植实现。
