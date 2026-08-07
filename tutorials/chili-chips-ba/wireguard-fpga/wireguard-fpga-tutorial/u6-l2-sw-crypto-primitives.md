# 软件加密原语库

## 1. 本讲目标

本讲精读运行在 picoRV32 软核上的**软件加密原语库**。读完本讲，你应该能够：

1. 说出 WireGuard 的 Noise 握手中，哪一步用到 curve25519（ECDH）、哪一步用到 BLAKE2s/HKDF（密钥派生），以及随机数/定时器扮演什么角色。
2. 读懂本项目 X25519 的「Montgomery 阶梯 + 常时间选择」实现，理解为什么它对私钥是分支无关（constant-time）的。
3. 读懂 BLAKE2s 的压缩函数与 HMAC，以及套在它之上的 Noise 风格 HKDF（`kdf()`）的「提取—扩展」三段输出。
4. 理解在「无 OS、无 libc、无硬件 RNG」的裸机 RV32 上，如何用 `rdcycle` 指令凑出一个可用熵源，并清楚它的局限。
5. 在主机上亲手运行 `99.warmup/7.curve25519` 的 RFC 7748 已知答案测试，验证 ECDH 共享密钥计算正确。

## 2. 前置知识

本讲承接 u6-l1（软件架构、bare-metal 启动与内存映射）与 u5-l1（AEAD/ChaCha20-Poly1305 原理）。读者应已具备以下认知，本讲不再重复：

- **裸机运行环境**：固件跑在 picoRV32 上，无 OS、无标准 libc，连 `memset`/`memcpy` 都由自研的 `string_bare` 提供；无动态内存分配（u6-l1）。
- **内存映射**：CPU 地址空间分 IMEM/DMEM/CSR 三段，外设经 MMIO 访问（u6-l1）。
- **控制面的职责边界**：软 CPU 只负责低频的 WireGuard **握手**与表更新，线速的 AEAD 加解密转发交给数据面硬件 DPE（u2-l1）。
- **AEAD 理论**：ChaCha20-Poly1305 的「加密—后—认证」构造、key/nonce/tag 尺寸（u5-l1）。本讲聚焦**控制面软件**这一侧的对称密码之外的另一组原语——**非对称（ECDH）+ 散列/HMAC/HKDF + 熵源/定时**。

下面用三段通俗背景，把本讲涉及的密码学名词先讲透。

### 2.1 为什么 WireGuard 需要这么多原语

WireGuard 用 **Noise 协议**（具体是 `Noise_IKpsk2` 变体）做握手。一次成功握手要依次用到：

| 阶段 | 用到的原语 | 作用 |
|------|-----------|------|
| 生成临时密钥对 | **随机数 + curve25519** | 临时私钥靠 RNG 现造，公钥由私钥乘基点得到 |
| 交换公钥、算共享点 | **curve25519（X25519）** | ECDH：双方各自用对方公钥乘自己私钥，得到同一个共享点 |
| 把共享点变成可用的会话密钥 | **BLAKE2s + HKDF（`kdf`）** | 对共享点做 HMAC 链式派生，输出新的链式密钥与传输密钥 |
| 派生出对称加解密密钥后 | **ChaCha20-Poly1305（AEAD）** | 由**数据面硬件**线速执行（见 Unit 5），软件侧仅参与握手 |
| 定时重协商、重传、保活 | **timer** | 基于 `rdcycle` 的忙等延时 |

所以本讲的三组原语——curve25519、(blake2s/hkdf)、(random/timer)——恰好是「握手前 → 握手中 → 握手后维护」这条链上的软件积木。

### 2.2 三个关键工程约束

这三组原语都遵守同一个约束集（在 u6-l1 已建立，这里落实到代码）：

1. **无动态分配**：所有大数运算用栈上定长数组，绝不调用 `malloc`。
2. **常时间**：处理**私钥/标量**的代码不能有依赖秘密数据值的分支或内存访问，以防侧信道泄漏。
3. **可移植、纯整数**：不依赖平台特有指令（如 RV32 的 `M` 乘法扩展、硬件 RNG），只用手写位运算与 `int64_t` 算术，因此既能在主机 `gcc` 上跑测试，也能交叉编进 RV32。

### 2.3 `rdcycle` 是什么

`rdcycle` 是 RISC-V 的一条用户态指令，把 CPU 周期计数器读进一个通用寄存器。本项目没有硬件定时器中断，也没有硬件随机数源，于是 `rdcycle` 一根指令承担了两个角色：**精确延时**（timer）和**凑熵**（random）。它的局限后面会详谈。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [2.sw/app/curve25519.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/curve25519.c) | curve25519 的**薄包装**，提供「生成公钥 / 算共享密钥」两个干净 API |
| [2.sw/app/tweetnacl_x25519.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/tweetnacl_x25519.c) | 真正的 X25519 标量乘实现（Montgomery 阶梯，取自公版 TweetNaCl） |
| [2.sw/app/blake2s.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/blake2s.c) | BLAKE2s 散列 + 一次性 `hash()` + `hmac()` |
| [2.sw/app/hkdf.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/hkdf.c) | Noise 风格 HKDF（`kdf()`），最多一次派生 3 个 32 字节密钥 |
| [2.sw/app/random.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/random.c) | 基于 `rdcycle` 的熵收集 + BLAKE2s 混合的伪随机源 |
| [2.sw/app/timer.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/timer.c) | 基于 `rdcycle` 的忙等延时（us/ms） |
| [99.warmup/7.curve25519/main.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/99.warmup/7.curve25519/main.c) | curve25519 的**主机端**测试，含 RFC 7748 已知答案向量 |
| [99.warmup/7.curve25519/Makefile](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/99.warmup/7.curve25519/Makefile) | 一行 `gcc` 编译并运行上述测试 |

依赖关系上，`curve25519.c` 调 `tweetnacl_x25519.c`；`hkdf.c` 调 `blake2s.c` 的 `hmac()`；`random.c` 调 `blake2s.c` 的 `hash()`；`random.c` 与 `timer.c` 都各自内联了一份 `rdcycle`。注意 `tweetnacl_x25519.c` 是公版代码原样引入（头注释标明来自 TweetNaCl，MIT），其余为本项目自研。

---

## 4. 核心概念与源码讲解

### 4.1 curve25519 / X25519：Noise 握手的 ECDH 引擎

#### 4.1.1 概念说明

Curve25519 是一条 Montgomery 形式的椭圆曲线：

\[
y^2 = x^3 + 486662\,x^2 + x \pmod{p},\qquad p = 2^{255}-19
\]

X25519 是定义在这条曲线上的**标量乘法**函数：给一个 32 字节标量（私钥）\(n\) 和一个 32 字节点的 \(x\) 坐标（公钥），算出 \(n\cdot P\) 的 \(x\) 坐标。它的两个魔法性质让 WireGuard 选中它：

- **ECDH 可行**：Alice 算 \(a\cdot B\)、Bob 算 \(b\cdot A\)，由于 \(A=a\cdot G\)、\(B=b\cdot G\)，两人得到同一个点 \(ab\cdot G\)，这就是共享密钥的来源——而窃听者只看到 \(A,B\)，无法在合理时间反推 \(a\) 或 \(b\)。
- **只算 \(x\) 坐标**：Montgomery 曲线允许只用 \(x\) 坐标做点乘（Montgomery 阶梯），既省运算又天然抗某些侧信道。

在 Noise 里，curve25519 负责把双方临时公钥「揉」成一个共享点；这个点还不是最终会话密钥，要再交给 4.2 的 HKDF 去派生。

#### 4.1.2 核心流程

X25519 由两层组成：

```
应用层 API（curve25519.c）
   curve25519_generate_public_key(pub, priv)  ──► x25519_scalarmult_base(pub, priv)   // 基点 G=9
   curve25519_compute_shared_secret(s, priv, peerPub) ──► x25519_scalarmult(s, priv, peerPub)

底层算子（tweetnacl_x25519.c）x25519_scalarmult(q, n, p):
   1. 标量钳位 (clamp): 把 n 变成 z
        z[31] = (n[31] & 127) | 64   // 清最高位、置次高位
        z[0]  &= 248                  // 清最低 3 位
   2. 解包公钥 p 为 16 个 16 位 limb（gf 类型）
   3. Montgomery 阶梯：i 从 254 到 0，逐位扫描 z
        - 按当前位 r 做常时间条件交换 sel25519
        - 一组固定的加/减/平方/乘（差分加法）
   4. 仿射化：求逆 inv25519，乘回去，打包成 32 字节
```

钳位是 RFC 7748 规定的：清最低 3 位让标量是 8 的倍数（ cofactor 清理），置次高位、清最高位固定了标量的有效位长（防小子群攻击）。

**常时间**是这里最关键的安全性质。注意第 3 步的循环对**每一位**都执行**完全相同**的一组运算，秘密位 `r` 只通过 `sel25519` 的位运算影响结果，而**不**决定「走哪条分支」。这样无论私钥是什么值，执行轨迹与功耗都一样，侧信道无从下手。

#### 4.1.3 源码精读

**应用层包装**——干净 API + 空指针检查，真正干活的是下层：

[2.sw/app/curve25519.c:46-66](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/curve25519.c#L46-L66) —— `curve25519_generate_public_key` 调 `x25519_scalarmult_base`（基点乘），`curve25519_compute_shared_secret` 调 `x25519_scalarmult`（任意点乘）。注意返回 `-1` 表示参数非法，`0` 表示成功，这是全库统一的错误约定。

**底层类型与常量**——这是「无动态分配 + 纯整数」的根基：

[2.sw/app/tweetnacl_x25519.c:15-19](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/tweetnacl_x25519.c#L15-L19) —— `typedef int64_t gf[16]` 把一个域元素表示成 16 个 16 位 limb（共 256 位，留出进位余量）；`_9` 是基点 \(G\) 的 \(x=9\)；`_121665` 是 Montgomery 曲线常数 \((486662-2)/2\)。

**标量钳位**——RFC 7748 强制要求：

[2.sw/app/tweetnacl_x25519.c:142-145](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/tweetnacl_x25519.c#L142-L145) —— 这三行把任意 32 字节私钥规整成合法 X25519 标量，对应 4.1.2 的第 1 步。

**常时间条件交换**（核心安全原语）：

[2.sw/app/tweetnacl_x25519.c:34-44](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/tweetnacl_x25519.c#L34-L44) —— `sel25519(p,q,b)` 当 `b` 的某位为 1 时交换 `p[i]`、`q[i]`。妙处在于 `c = ~(b-1)`：当 `b=1` 时 `c=0`（不交换），当 `b=0` 时 `c=0xFFFF...`（全交换）——全程**没有 `if`**，因此没有依赖秘密的分支。

**Montgomery 阶梯主体**：

[2.sw/app/tweetnacl_x25519.c:155-180](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/tweetnacl_x25519.c#L155-L180) —— `i` 从 254 递减到 0；`r` 取出标量的第 `i` 位；先用 `sel25519` 按位条件交换、再做一组差分运算（`A/Z/S/M` 分别是加、减、平方、域乘），最后再用 `sel25519` 交换回来。这一段无论 `r=0` 或 `1` 都执行相同指令序列，是「常时间」的体现。

**基点乘 = 用基点 9 做一次任意点乘**：

[2.sw/app/tweetnacl_x25519.c:196-199](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/tweetnacl_x25519.c#L196-L199) —— `x25519_scalarmult_base` 只是把第三个参数固定为 `_9`（即 9），复用同一个阶梯。所以「生成公钥」和「算共享密钥」是同一个算子。

#### 4.1.4 代码实践

1. **实践目标**：在主机上编译并运行 `99.warmup/7.curve25519`，确认 X25519 对 RFC 7748 已知向量给出正确结果。
2. **操作步骤**：
   ```bash
   cd 99.warmup/7.curve25519
   make        # 即 gcc -O2 -o main main.c curve25519.c tweetnacl_x25519.c && ./main
   ```
3. **需要观察的现象**：终端会打印四组测试。重点关注：
   - `Test curve25519_generate_public_key()` 的 **Test 2 - RFC 7748 test vector**：`Computed` 与 `Expected` 两串 hex 应完全一致，`Vector Match: PASS`。
   - `Test Complete Key Exchange` 的 **Step 4**：Alice 与 Bob 各自算出的 `Shared secret` 应逐字节相同，打印 `Shared secrets match: SUCCESS`。
   - `Test Multiple Key Exchanges`：10 轮随机密钥对交换应 10/10 通过。
4. **预期结果**：RFC 7748 向量逐一命中，完整密钥交换成功，多轮随机测试 100% 通过。这证明 `tweetnacl_x25519.c` 的 Montgomery 阶梯实现与标准一致。
5. 关于主机端测试与板上固件的关系：`main.c` 里的 `random_bytes` 用的是 libc 的 `rand()`（仅为测试造随机私钥），而板上固件 `2.sw/app/random.c` 用 `rdcycle` 熵源替代它——**X25519 算子本身两处完全相同**，这就是「可移植纯整数」的好处。

#### 4.1.5 小练习与答案

**练习 1**：把 `x25519_scalarmult` 里 `z[0] &= 248;` 这一行注释掉再跑 RFC 7748 测试，会发生什么？为什么？

> **答案**：RFC 7748 的已知向量对应的私钥最低 3 位本就为 0（或恰好不冲突时仍能过），但钳位的本意是「清掉 cofactor 的小子群」。注释后，多数随机私钥仍能算出与合规实现一致的结果（因为 8 的倍数要求主要影响安全性而非结果唯一性），但会失去对小子群攻击的防护。结论：钳位是**安全**要求， correctness 测试未必能抓到它的缺失。

**练习 2**：为什么 `sel25519` 用 `c = ~(b-1)` 而不是 `if (b) swap(...)`？

> **答案**：前者是纯位运算，无论 `b` 为 0 或 1 都执行相同指令、相同内存访问，不泄漏秘密标量的位；后者是数据相关的分支，攻击者可通过分支耗时/功耗推测标量位。常时间是侧信道防御的核心。

---

### 4.2 blake2s / hkdf：散列、MAC 与 Noise 密钥派生

#### 4.2.1 概念说明

BLAKE2s 是一个 32 位字宽的**密码学散列函数**（RFC 7693）：输入任意长度字节，输出最长 32 字节摘要；抗碰撞、抗原像。它在 Noise 里是「万能底座」——既做纯散列，又做 HMAC（消息认证），又做 HKDF（密钥派生）。

为什么 WireGuard 用 BLAKE2s 而非 SHA-256？因为它在软件里**更快**、更紧凑，尤其适合 32 位软核。本项目把它写成纯 C、显式位运算，x86 与 RV32 同一份代码（见 `blake2s.c` 头注释）。

**HKDF（Noise 变体）** 是套在 BLAKE2s-HMAC 之上的密钥派生。给定一个「链式密钥（chaining key）」和一段输入数据，它输出最多 3 个 32 字节密钥。WireGuard 在握手每一轮都用它把上轮的链式密钥「滚」成下一轮的新链式密钥与传输密钥，是 Noise 状态机的发动机。

#### 4.2.2 核心流程

**BLAKE2s 压缩**采用海绵式分块处理：

```
init:   把 8 个 IV 常量赋给状态 h[0..7]；h[0] 再异或 param block（含输出长度、密钥长度）
update: 把输入按 64 字节一块填入缓冲 b[]；满一块就：
          - 累加已处理字节计数 t[]
          - 调 blake2s_compress(0)  （非末块）
final:  末块补零、置 last 标志、调 blake2s_compress(1)，从 h[] 小端输出 outlen 字节
```

压缩函数内部跑 **10 轮**，每轮 8 次 **G 混合函数**，G 内含模 \(2^{32}\) 加、异或与 16/12/8/7 位的循环右移，并用固定的 `sigma[10][16]` 置换决定每轮取哪些消息字。

**HMAC-BLAKE2s** 是标准的「内外两次散列」结构：`HMAC(key, msg) = H((key⊕opad) ‖ H((key⊕ipad) ‖ msg))`，其中 ipad=0x36、opad=0x5c。

**Noise HKDF（`kdf`）** 是「提取 + 三段扩展」：

\[
\begin{aligned}
\text{secret} &= \text{HMAC}(\text{chaining\_key},\ \text{data}) \quad\text{(提取)}\\
\text{out}_1 &= \text{HMAC}(\text{secret},\ \texttt{0x01})\\
\text{out}_2 &= \text{HMAC}(\text{secret},\ \text{out}_1 \,\|\, \texttt{0x02})\\
\text{out}_3 &= \text{HMAC}(\text{secret},\ \text{out}_2 \,\|\, \texttt{0x03})
\end{aligned}
\]

任一输出可省略（传 `NULL` 或长度 0），用 `goto out` 跳到末尾的清零。注意 `output[]` 缓冲被声明为 `BLAKE2S_HASH_SIZE + 1` 字节，正是为了在第 2、3 次扩展时多塞那一个计数器字节。

#### 4.2.3 源码精读

**G 混合函数与字节序宏**——压缩的核心算子：

[2.sw/app/blake2s.c:12-34](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/blake2s.c#L12-L34) —— `ROTR32` 是 32 位循环右移；`B2S_GET32` 手工把 4 个字节按**小端**拼成 `uint32_t`（这是「可移植小端」的关键，不依赖 CPU 字节序）；`B2S_G` 是一轮的混合宏，对 4 个工作变量做加/异或/旋转。

**压缩函数**——10 轮、sigma 置换：

[2.sw/app/blake2s.c:46-89](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/blake2s.c#L46-L89) —— 先把状态 `h[]` 与 IV 装入 16 个工作变量 `v[]`，把字节计数异或进 `v[12]/v[13]`，末块时翻转 `v[14]`；然后取 16 个消息字、跑 10 轮 G；最后 `h[i] ^= v[i] ^ v[i+8]` 回写。`sigma` 是 RFC 7693 规定的固定置换表。

**一次性散列 `hash()`**——init/update/final 的便捷封装：

[2.sw/app/blake2s.c:165-177](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/blake2s.c#L165-L177) —— 这个函数会被 `random.c` 直接复用来「混合」熵采样。`outlen`/`keylen` 都可配，无密钥时就是普通散列。

**HMAC-BLAKE2s**——注意密钥超长时先散列压缩：

[2.sw/app/blake2s.c:181-216](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/blake2s.c#L181-L216) —— `keylen > 64` 时先 `hash` 一次把密钥压成 32 字节；否则直接拷进 64 字节缓冲。随后两次 `0x36` / `0x36^0x5c` 异或做 ipad/opad（第二次复用同一缓冲，巧妙地 `^= 0x36 ^ 0x5c` 把 ipad 还原成原值再变 opad）。末尾 `memset` 清零敏感内存。

**Noise HKDF `kdf()`**——提取 + 三段扩展：

[2.sw/app/hkdf.c:46-87](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/hkdf.c#L46-L87) —— 第 57 行 `hmac(secret, data, chaining_key, ...)` 是「提取」；随后用计数器字节 `0x01/0x02/0x03` 串联上一次输出做三次「扩展」。每次扩展前判 `!dst || !len` 决定是否提前 `goto out`，让调用方可只取前 1 或 2 个密钥。`out:` 标签后清零 `secret` 与 `output`，不留密钥残影。

> 注意 `hmac` 的参数顺序：`hmac(out, in, key, inlen, keylen)`——**输入在前、密钥在后**。`kdf` 里 `hmac(secret, data, chaining_key, data_len, NOISE_HASH_LEN)` 是把 `chaining_key` 当 HMAC 的密钥、`data` 当消息，符合 HKDF-Extract 的语义。

#### 4.2.4 代码实践

1. **实践目标**：在主机上写一个最小程序，验证 `hash()` 与 `hmac()` 可用，并手算一次 `kdf` 的「提取」步骤。
2. **操作步骤**（源码阅读型实践）：
   - 复制 `2.sw/app/blake2s.c`、`blake2s.h`、`hkdf.c`、`hkdf.h`、`string_bare.*` 到一个临时目录，写一个 `mini.c`：
     ```c
     /* 示例代码（非项目原有） */
     #include <stdio.h>
     #include "blake2s.h"
     int main(void) {
         uint8_t out[32];
         const char *msg = "abc";
         hash(out, 32, NULL, 0, msg, 3);
         printf("blake2s(\"abc\") = ");
         for (int i = 0; i < 32; i++) printf("%02x", out[i]);
         printf("\n");
         return 0;
     }
     ```
   - 用 `gcc -O2 -o mini mini.c blake2s.c string_bare.c` 编译运行。
3. **需要观察的现象**：`blake2s("abc")` 的摘要与 RFC 7693 / 公开 BLAKE2s 在线工具一致。
4. **预期结果**：手算或在线工具比对一致即证明 `blake2s.c` 正确。`kdf` 由于无官方 Noise 向量（见 `2.sw/README.md` 明确说明），其正确性靠「BLAKE2s 已验证 + HMAC/KDF 逻辑取自 WireGuard 内核」间接保证。
5. 如果本地缺 `string_bare.c` 的实现，可临时用标准 libc 的 `memset/memcpy/memcmp` 顶替以跑通主机测试；板上固件则必须用 `string_bare`（无 libc）。

#### 4.2.5 小练习与答案

**练习 1**：`kdf()` 里 `output[BLAKE2S_HASH_SIZE] = 2;` 之后调 `hmac(output, output, secret, BLAKE2S_HASH_SIZE + 1, ...)`，为什么要 `+1`？

> **答案**：第 2 次扩展的 HMAC 输入是「上一段密钥 ‖ 计数器字节」，共 32 + 1 = 33 字节。`output` 被声明为 33 字节正是为此，第 33 字节存计数器 `0x02`/`0x03`，故长度传 `BLAKE2S_HASH_SIZE + 1`。

**练习 2**：`blake2s_init` 里 `ctx->h[0] ^= 0x01010000 ^ (keylen << 8) ^ outlen;` 这串魔数在干什么？

> **答案**：这是 BLAKE2s 的「param block」首字：`0x01010000` = 摘要长度 1 字节、密钥长度 1 字节、扇区 1（fanout=1，即单树）；再异或 `(keylen<<8)`（密钥长度字段）与 `outlen`（摘要长度字段）。它把参数烘焙进初始状态，是 BLAKE2s 区别于 BLAKE2b 的配置点。

---

### 4.3 random / timer：`rdcycle` 一指令双用

#### 4.3.1 概念说明

板上没有硬件 RNG（真随机数源），也没有可编程硬件定时器中断。于是本项目用 RISC-V 的 `rdcycle` 一条指令解决两件事：

- **timer**：读周期计数器做精确忙等延时，用于重协商、重传、保活的定时。
- **random**：把周期计数器当作「弱熵源」，多次采样、位运算搅拌、再过 BLAKE2s 收敛成 32 字节伪随机输出，用于生成临时私钥、peer 标识、MAC 后缀。

**`rdcycle` 作为熵源的局限**（务必记牢）：纯周期计数器的低位在确定性执行下高度相关、可预测，单凭它**不是密码学安全的**。本项目在 `2.sw/README.md` 与代码注释里都坦承这一点——它只是「嵌入式可用的凑合熵源」，并依赖执行时序抖动（`small_delay` + 每次采样间的不确定耗时）来增加随机性。对一个 VPN 握手的临时密钥而言这是已知折中；要严格安全应外接硬件 TRNG。

#### 4.3.2 核心流程

**timer**（简单）：

```
delay_us(us):
   start = rdcycle()
   wait_cycles = us * CYCLES_PER_US          // CYCLES_PER_US = 80（80MHz）
   while (rdcycle() - start) < wait_cycles:  // 忙等
        asm volatile("" ::: "memory")        // 防编译器优化掉循环
delay_ms(ms):  循环调 delay_us(1000) ms 次
```

差值比较 `(rdcycle() - start)` 用无符号回绕，天然正确处理计数器溢出。

**random_32bytes**（熵收集 + 混合）：

```
for i in 0..15:
   temp = rdcycle()
   temp ^= temp >> 7          // xorshift 风格搅拌
   temp *= 0x6C078965          // 仿 Mersenne Twister 的「乱化常数」
   temp ^= temp >> 11
   temp += 0x5D588B65
   cycles[i] = temp
   small_delay()               // 人为制造时序抖动
sp = &sp 的地址                 // 把栈指针地址当额外熵
hash(out, 32, NULL, 0, cycles‖sp, ...)   // BLAKE2s 收敛成 32 字节
memset 清零敏感缓冲
```

关键思想：单个 `rdcycle` 样本可预测，但 16 个**带抖动**的样本 + 栈地址 + BLAKE2s 的单向收敛，让输出难以从外部反推。

#### 4.3.3 源码精读

**`rdcycle` 内联**（两文件各有一份）：

[2.sw/app/timer.c:45-50](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/timer.c#L45-L50) 与 [2.sw/app/random.c:47-52](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/random.c#L47-L52) —— 用内联汇编 `asm volatile("rdcycle %0" : "=r"(cycles))` 把周期计数器读进 `cycles`。`volatile` 防止编译器把多次读取合并成一次。

**CPU 频率常量**——延时换算的依据：

[2.sw/app/timer.h:14-16](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/timer.h#L14-L16) —— `CPU_FREQ_HZ = 80000000`（80MHz，承接 u2-l3 的红色控制面时钟域），故 `CYCLES_PER_US = 80`、`CYCLES_PER_MS = 80000`。若改板换频，这里要同步改。

**忙等延时**：

[2.sw/app/timer.c:52-67](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/timer.c#L52-L67) —— `delay_us` 用回绕差值比较；`delay_ms` 是其 1000 倍循环包装。`asm volatile("" ::: "memory")` 是编译器屏障，防止循环被优化掉。

**熵采样与混合**（random 的核心）：

[2.sw/app/random.c:69-93](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/random.c#L69-L93) —— 注意第 75-79 行的 xorshift + 两个魔数 `0x6C078965`/`0x5D588B65`（这两个常量出自 Mersenne Twister 的初始化，用以进一步去相关）；第 83 行 `uintptr_t sp = (uintptr_t)&sp;` 把**自身栈变量的地址**当额外熵——每次调用的栈位置都可能不同；最后 `hash()` 把 16×4 + 指针宽 字节收敛成 32 字节，并 `memset` 清零。

**区间随机（带模偏差注意）**：

[2.sw/app/random.c:99-111](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/random.c#L99-L111) —— `random_range` 取 32 字节里的前 4 字节做 `min + (val % (max-min+1))`。注释自称「rejection sampling to avoid modulo bias」，但实现实际是**取模法**（非拒绝采样），当区间不整除 \(2^{32}\) 时仍有轻微模偏差；好在它只用于 peer 标识、MAC 后缀等非密钥用途，影响可接受。

#### 4.3.4 代码实践

1. **实践目标**：理解板上 RNG 如何被触发，并跟踪一次随机 MAC 生成。
2. **操作步骤**（源码阅读型实践）：
   - 在 `2.sw/app/main.cpp` 中定位 `config network` 的处理分支（搜索 `mac[4]` 与 `random_32bytes`）。
   - 跟踪：用户在 CLI 输入随机 MAC 确认 → 调 `random_32bytes(random_bytes)` → 取前 2 字节填入 `config->mac[4]`、`config->mac[5]`。
3. **需要观察的现象**：每次执行该分支，MAC 后两字节应不同（因为 `rdcycle` 每次采样不同）。
4. **预期结果**：两次 `config network` 得到不同 MAC 后缀，证明 RNG 在板上确实产出变化的输出。
5. **关于「密码学安全性」的诚实标注**：本实践的目的是观察「可用性」而非「安全性」。`rdcycle` 熵源不满足密码学安全标准，不应直接用于长期静态密钥生成；它适合生成临时 ECDH 私钥（一次性、握手期即弃）与显示用的标识符。待本地验证：在有硬件 TRNG 的平台上应替换 `random_32bytes` 的熵采集部分。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `delay_us` 用 `(rdcycle() - start) < wait_cycles` 而不是 `rdcycle() < start + wait_cycles`？

> **答案**：周期计数器会回绕（`uint32_t` 约 53 秒后溢出）。直接 `start + wait_cycles` 在回绕点会得到错误的小值，导致比较提前满足或异常；而 `(rdcycle() - start)` 利用无符号减法的回绕性质，无论是否溢出都给出正确的「经过的周期数」，是处理计数器回绕的标准手法。

**练习 2**：`random_32bytes` 里 `uintptr_t sp = (uintptr_t)&sp;` 为什么能增加熵？

> **答案**：`&sp` 是这个局部变量在栈上的地址，它取决于调用栈深度（谁调了 `random_32bytes`、之前有多少局部变量）。不同调用路径下栈位置不同，而栈地址在硬件上未必固定（取决于编译器布局与运行历史），因此提供了一份与 `rdcycle` 序列弱相关的额外输入，BLAKE2s 把它混进摘要后可略微抬高输出不可预测性。当然这仍是弱熵，不改变「非密码学安全」的定性。

---

## 5. 综合实践

**任务**：把三组原语串成一条「迷你 Noise 派生链」，在主机上跑通并理解每一步。

利用 `99.warmup/7.curve25519` 已有的 `main.c`，在 `test_complete_key_exchange` 末尾（算出 `alice_shared`/`bob_shared` 之后）追加一段**示例代码**（非项目原有），把 ECDH 共享密钥喂给 `kdf`：

```c
/* 示例代码（非项目原有）：演示 ECDH 共享密钥 → Noise HKDF 派生 */
#include "hkdf.h"
#include "blake2s.h"

uint8_t chaining_key[32];   /* 假设的初始链式密钥，实际 Noise 里是握手预设值 */
memset(chaining_key, 0, 32);

uint8_t k1[32], k2[32];     /* 派生出的两个密钥 */
kdf(k1, 32,  k2, 32,        /* 要前两段 */
    NULL, 0,                /* 第三段不要 */
    alice_shared, 32,       /* 输入数据 = ECDH 共享密钥 */
    chaining_key);          /* 链式密钥 */

print_hex("Derived key1", k1, 32);
print_hex("Derived key2", k2, 32);
```

**步骤**：

1. 把 `2.sw/app/blake2s.c`、`hkdf.c`、`string_bare.*` 复制进 `99.warmup/7.curve25519/`（或调整 `Makefile` 的源文件列表与 `-I` 包含路径指向 `2.sw/app`）。
2. 在 `main.c` 顶部 `#include "blake2s.h"` 与 `#include "hkdf.h"`，把上述片段插入密钥交换成功之后。
3. 重新 `make` 运行。

**要回答的问题**：

- Alice 与 Bob 各自用**相同的** `chaining_key` 与**相同的** `alice_shared`（= `bob_shared`）调用 `kdf`，得到的 `k1`/`k2` 是否相同？为什么？这正是 Noise 握手能对齐双方会话密钥的根源。
- 如果把 `alice_shared` 改动 1 个比特再调 `kdf`，`k1`/`k2` 会有多少比特变化？（预期：雪崩效应，约一半比特翻转——BLAKE2s 的设计目标。）

**预期结果**：双方派生出完全一致的 `k1`/`k2`，证明 curve25519（算共享点）→ blake2s/hkdf（派生密钥）这条软件链在控制面上是自洽闭环的；改动输入 1 比特则输出剧烈变化，体现散列的雪崩性。这条链的产物（传输密钥）最终会经 HAL/CSR 下发到数据面，由硬件 AEAD 线速使用——那是 u6-l4 与 Unit 5 的主题。

---

## 6. 本讲小结

- WireGuard 的 Noise 握手在软件侧依次调用：**random 造临时私钥 → curve25519 算公钥/共享点 → blake2s+HKDF 派生密钥**，timer 负责定时维护；线速 AEAD 交硬件。
- **curve25519** 是一层薄包装 + TweetNaCl 公版 X25519：用 Montgomery 阶梯做标量乘，全程常时间（`sel25519` 位运算条件交换、无秘密相关分支），标量钳位满足 RFC 7748，`gf=int64_t[16]` 纯整数无动态分配。
- **blake2s** 是 Noise 的万能底座（散列/HMAC/HKDF），手工小端、10 轮 G 混合、sigma 置换，x86 与 RV32 同码；**hkdf `kdf()`** 做「提取 + 三段扩展」，任一输出可省略，末尾清零敏感内存。
- **random** 用 `rdcycle` + 时序抖动 + 栈地址凑弱熵，经 BLAKE2s 收敛成 32 字节，**非密码学安全**但够嵌入式用；**timer** 用同一个 `rdcycle` 做回绕正确的忙等延时，频率常量 80MHz 承接控制面时钟域。
- 三组原语统一遵守「无 OS、无 libc（`string_bare`）、无动态分配、可移植纯整数」，因此能在主机 `gcc` 与 RV32 交叉编译间共用同一份算子，便于用 RFC 已知向量在主机侧验证。
- `kdf` 因无官方 Noise 向量而靠「BLAKE2s 已验证 + 逻辑取自 WireGuard 内核」间接保证正确性（README 明示）；这是本库已知的验证缺口。

## 7. 下一步学习建议

- **u6-l3 网络栈与 CLI**：看握手报文如何经 `ethernet.c`/`network.c` 收发，理解软件网络栈如何把上层的 crypto 原语与底层 DPE FIFO 衔接起来。
- **u6-l4 软件控制流：收发包与表更新**：本讲派生出的会话密钥如何经 HAL/CSR 写入 `cryptokey_table`，以及改表前如何用 FCR 做 pause/idle 原子更新——把本讲的「算密钥」与数据面的「用密钥」连起来。
- **回看 u5-l1 / u5-l3**：对照软件侧 ChaCha20-Poly1305（含已知 poly1305 数学 bug）与数据面硬件 AEAD，理解为何控制面只跑握手、线速加解密必须靠硬件。
- **延伸阅读**：RFC 7748（Curve25519/X25519）、RFC 7693（BLAKE2）、RFC 5869（HKDF）与 Noise 协议规范，把本讲读到的代码与规范条文一一对照。
