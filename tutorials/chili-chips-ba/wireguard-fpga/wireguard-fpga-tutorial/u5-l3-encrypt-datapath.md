# 加密数据流

## 1. 本讲目标

本讲聚焦 ChaCha20-Poly1305 **加密**路径的硬件数据流（datapath）。学完后你应当能够：

1. 读懂加密 datapath 的「明文 → ChaCha20 → 密文分叉 → 双路汇合 → 输出」整体走向，说清每一段由哪个模块负责。
2. 理解「密文分叉」为什么必要、以及它如何用一组 `valid/ready` 逻辑实现「一个源同时喂两个接收方且保证原子双投递」。
3. 看懂 `prep_auth_data` 的 4 状态 FSM 如何把 RFC8439 的认证数据格式（`AAD ‖ pad ‖ 密文 ‖ pad ‖ 长度`）逐拍拼出来，特别是它为何要把密文拍的 `tlast` 强行压成 0。
4. 看懂 `append_auth_tag` 的 2 状态 FSM 如何把 Poly1305 算出的 tag「缝」到密文末尾，以及 `tlast` 的抑制与恢复时机。

> 现状提醒（承接 u5-l2）：本讲描述的是 `3.build/pipelinec_build/` 下独立加密变体的完整数据流。该加密核在当前 HEAD 尚未编入 `1.hw/top.filelist`，即「源码完整可仿真、但未焊进 SoC 上板」。本讲只讲数据流本身，核内部的 ChaCha20 轮函数与 Poly1305 数学留到本单元后续讲义。

---

## 2. 前置知识

本讲假定你已掌握以下内容（均来自前置讲义，这里只做最简回顾）：

- **AXI-Stream（AXIS）握手**（u4-l1、u2-l3）：`tvalid` 与 `tready` 同拍为 1 才完成一次 beat 传输；`tdata` 是数据、`tkeep` 是字节使能、`tlast` 标记包尾。本讲的 `axis128_t` 是 128 位（16 字节）的 AXIS。
- **AEAD 构造与认证数据格式**（u5-l1）：ChaCha20-Poly1305 采用「先加密、后认证」。加密后，Poly1305 对如下字节流计算 16 字节 tag：

  \[ \text{auth\_data} = \text{AAD} \,\|\, \text{pad}_{16}(\text{AAD}) \,\|\, C \,\|\, \text{pad}_{16}(C) \,\|\, \text{le}_{64}(|\text{AAD}|) \,\|\, \text{le}_{64}(|C|) \]

  其中 \(C\) 是密文，\(\text{pad}_{16}(x) = (16 - (|x| \bmod 16)) \bmod 16\) 个零字节，\(\text{le}_{64}\) 是小端 64 位编码。最终输出是 \(C \,\|\, \text{tag}\)。
- **counter=0 专用派生 Poly1305 一次性密钥**（u5-l1）：ChaCha20 用 counter=0 跑出一个 64 字节块，前 32 字节作为 Poly1305 的 key；counter 从 1 起才加密明文。
- **PipelineC 约定**（u5-l2）：`#pragma MAIN_MHZ`/`#pragma PART`、`DECL_INPUT/OUTPUT`、`#define INST` + `#include` 的多实例化模式、`stream(T)` 类型、`#pragma FEEDBACK` 组合环路。每个 `MAIN` 函数每拍执行一次，模块间的全局可见 wire 由一个 `*_dataflow` 顶层函数手工「接线」。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/chacha20poly1305/encrypt_dataflow.c` | **接线顶层**：把 chacha20 / prep_auth_data / poly1305_mac / append_auth_tag 四个模块的全局 wire 连起来，核心是「密文分叉」。 |
| `src/chacha20poly1305/chacha20poly1305_encrypt.c` | 实例化清单：用 `#define INST` + `#include` 把四个模块实例摆好，再 `\#include` 进 `encrypt_dataflow.c`。 |
| `src/chacha20/chacha20.c` | ChaCha20 模块的「外壳」：把 `chacha20_fsm`（在 `chacha20.h` 里）的输入输出对接到全局 wire。 |
| `src/chacha20/chacha20.h` | ChaCha20 的 FSM 与轮函数：含「先派生 poly_key、再吐密文」的输入/输出双侧 FSM。 |
| `src/prep_auth_data/prep_auth_data.c` | prep_auth_data 的外壳：把 `prep_auth_data_fsm`（在 `.h` 里）对接到全局 wire。 |
| `src/prep_auth_data/prep_auth_data.h` | **核心**：4 状态 FSM，组装 Poly1305 认证数据流。 |
| `src/auth_tag/append_auth_tag.c` | **核心**：2 状态 FSM，把 tag 追加到密文末尾，含 `tlast` 抑制/恢复。 |
| `src/poly1305/poly1305.h` | Poly1305 的 FSM（仅在本讲作为「黑盒」，取其 key/data/tag 接口）。 |
| `src/chacha20poly1305/encrypt_tb.c` | 加密自检测试台：用 RFC8439 风格的 key/nonce/AAD/明文，比对预期密文+tag。 |

> 以下路径均相对 `3.build/pipelinec_build/`。

---

## 4. 核心概念与源码讲解

### 4.1 密文分叉与整体加密数据流

#### 4.1.1 概念说明

加密的核心矛盾是：**密文既要立刻送出去，又要拿去算认证 tag，而且这两件事节奏完全不同。**

- 密文是流式产出：ChaCha20 是一条 64 级流水线，明文进、密文出，吞吐很高，几乎可以一拍一个 beat 往外送。
- tag 是「收尾」产出：Poly1305 必须先把 `AAD ‖ 密文 ‖ 长度` 这整段认证数据**全部**吃进去、跑完累加，才能在最后吐出一个 16 字节的 tag。

所以同一个「密文流」必须同时供给两个消费者：

1. **直通出口** `append_auth_tag`：密文原样送往最终输出（先到）。
2. **旁路算 tag** `prep_auth_data → poly1305_mac`：把密文（外加前缀 AAD、后缀长度）喂给 Poly1305，最终算出 tag（后到），再由 `append_auth_tag` 把它缝到密文尾巴上。

这就是「密文分叉」（ciphertext fork）：**一份数据，两个去向，节奏不同，最终在输出端汇合。**

#### 4.1.2 核心流程

整个加密 datapath 的信号流如下（方框是模块，箭头是 `stream(axis128_t)` 或标量 wire）：

```
                 CSR: key, nonce, aad, aad_len
                          │
                          ▼
        ┌─────────────  chacha20  ─────────────┐
        │  (先 counter=0 派生 poly_key,         │
        │   再加密明文 → 密文 axis_out)          │
        └──┬───────────────────────┬────────────┘
     poly_key                     密文 axis_out
       (256b)                    （密文分叉 ↓）
           │                 ┌──────┴───────┐
           │                 ▼              ▼
           │          prep_auth_data   append_auth_tag
           │         (加 AAD/长度,      (CIPHERTEXT 态:
           │          压 tlast)          密文直通,
           │                 │            压 tlast)
           │             auth_data         │
           │                 ▼             │
           └────────►  poly1305_mac        │
                       (算 16B tag)        │
                            │ tag          │
                            ▼              ▼
                       append_auth_tag (AUTH_TAG 态:
                          缝 tag 为新最后一拍, tlast=1)
                                   │
                                   ▼
                          最终输出 = 密文 ‖ tag
```

**分叉的握手语义（关键）**：标准 AXIS 是「一发一收」，而分叉要求「一发两收，且必须同时收」。因此：

- 源的 `ready` = 两个接收方的 `ready` **相与**（木桶效应，任一方没准备好都让源停住）。
- 每个接收方的 `valid` 不能简单地等于源 `valid`：必须保证「只有当两边都准备好、真正发生传输时，两边才同时看到有效数据」。否则先就绪的那一方会单独把这一拍「吃掉」，造成另一方漏数据。

这正是 `encrypt_dataflow.c` 里那段看似绕口的 `valid` 门控要解决的问题（见 4.1.3）。

#### 4.1.3 源码精读

**接线顶层**用 `#pragma MAIN_MHZ encrypt_dataflow 80.0` 标记为 80 MHz 顶层，函数体每拍执行一次，纯做 wire 连接。

[3.build/pipelinec_build/src/chacha20poly1305/encrypt_dataflow.c:7-17](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305/encrypt_dataflow.c#L7-L17) 是前三段接线：把入口明文流、key、nonce 喂给 chacha20；再把 chacha20 派生出的 `poly_key` 单独喂给 poly1305_mac 作 key。注意 `poly_key` 是 chacha20 的**旁路输出**（不参与密文分叉），它实现了 u5-l1 讲的「counter=0 派生 Poly1305 key」。

密文分叉的核心在下面这段：

[3.build/pipelinec_build/src/chacha20poly1305/encrypt_dataflow.c:19-36](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305/encrypt_dataflow.c#L19-L36) ——密文分叉逻辑。逐句拆解：

```c
// 先把两路 fork 的输入默认置无效
prep_auth_data_encrypt_axis_in = chacha20_encrypt_axis_out;
prep_auth_data_encrypt_axis_in.valid = 0;          // 默认不给 prep
append_auth_tag_axis_in = chacha20_encrypt_axis_out;
append_auth_tag_axis_in.valid = 0;                 // 默认不给 append

// 源 ready = 两接收方 ready 相与（同时收）
chacha20_encrypt_axis_out_ready =
    prep_auth_data_encrypt_axis_in_ready & append_auth_tag_axis_in_ready;

// 只有「真正传输」或「本方正在阻塞」时, 才让本方看到 valid
if(chacha20_encrypt_axis_out_ready | ~prep_auth_data_encrypt_axis_in_ready){
    prep_auth_data_encrypt_axis_in.valid = chacha20_encrypt_axis_out.valid;
}
if(chacha20_encrypt_axis_out_ready | ~append_auth_tag_axis_in_ready){
    append_auth_tag_axis_in.valid = chacha20_encrypt_axis_out.valid;
}
```

这段门控的真值表（设源 `valid=1` 有数据）：

| prep_ready | append_ready | 源 ready | prep.valid | append.valid | 是否传输 |
|:---:|:---:|:---:|:---:|:---:|:---|
| 0 | 0 | 0 | 1 | 1 | 否（双方都阻塞，都看到待处理数据） |
| 0 | 1 | 0 | 1 | 0 | 否（append 空闲但不许它独吞） |
| 1 | 0 | 0 | 0 | 1 | 否（prep 空闲但不许它独吞） |
| 1 | 1 | 1 | 1 | 1 | **是**（双方同时消费） |

口诀：「真传」或「我正挡着」时才让本方看到 `valid`。当一方阻塞时，阻塞方仍持 `valid=1`（让它知道有数据在等、持续重试），而空闲方被门控成 `valid=0`（防止它单独把这一拍吃掉）。只有两边都 ready，才有真实传输。这保证了一个密文 beat **要么被两边同时收下，要么谁也别动**。

后续接线 [3.build/pipelinec_build/src/chacha20poly1305/encrypt_dataflow.c:42-52](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305/encrypt_dataflow.c#L42-L52) 把四段串完：`prep_auth_data` 出 → `poly1305_mac` 数据入；`poly1305_mac` 出 tag → `append_auth_tag` tag 入；`append_auth_tag` 出 → 顶层密文输出。

**chacha20 外壳**如何产出 poly_key 与密文，见 FSM：

[3.build/pipelinec_build/src/chacha20/chacha20.h:305-332](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20/chacha20.h#L305-L332) ——输入侧 FSM：先在 `POLY_KEY` 态往流水线塞一块**全零数据 + counter=0**（第 310-312 行），派生出 Poly1305 key；被流水线收下后才切到 `PLAINTEXT` 态，把真正的明文块送进去。

[3.build/pipelinec_build/src/chacha20/chacha20.h:345-368](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20/chacha20.h#L345-L368) ——输出侧 FSM：第一块（counter=0）出来的前 32 字节走 `poly_key` 旁路（第 349-357 行），之后的块才是密文，经 512→128 位宽转换成 `axis_out`（第 371-373 行）。

> 这条 `poly_key` 旁路和 `axis_out` 密文分叉是两件不同的事：前者是「ChaCha20 的两种产物」（key vs 密文），后者是「同一份密文的两种用途」（输出 vs 算 tag）。别混淆。

#### 4.1.4 代码实践

**实践目标**：亲手验证上面那张分叉真值表，确认「原子双投递」语义。

**操作步骤**（纯源码阅读，无需工具链）：

1. 打开 [encrypt_dataflow.c:19-36](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305/encrypt_dataflow.c#L19-L36)。
2. 设源 `chacha20_encrypt_axis_out.valid = 1`。
3. 对 `(prep_ready, append_ready)` 的 4 种组合，分别代入代码，算出 `chacha20_encrypt_axis_out_ready`、`prep_auth_data_encrypt_axis_in.valid`、`append_auth_tag_axis_in.valid`。

**需要观察的现象**：只有 `prep_ready=1 且 append_ready=1` 时 `源 ready=1`（发生传输）；其余 3 种组合源 ready 都为 0，密文 beat 被原地保持。

**预期结果**：与你上面那张真值表完全一致。特别确认第 2、3 行：一方空闲时它的 `valid` 被压成 0，不会单独消费。

#### 4.1.5 小练习与答案

**练习 1**：如果把第 30 行的 `&` 改成 `|`（源 ready = 任一方 ready），会发生什么？

> **答案**：源会在只有一方 ready 时就交出 beat，那一拍只被 ready 的一方消费，另一方永久漏掉这个 beat。密文与认证数据会错位，tag 与密文对不上，解密端验证必然失败。`&` 是必须的。

**练习 2**：为什么 `poly_key` 不需要参与这种「相与」分叉？

> **答案**：`poly_key` 是 ChaCha20 的独立输出，只有一个消费者（poly1305_mac 的 key 输入），是一发一收的普通点对点握手，不存在「同时喂两方」的问题。

---

### 4.2 prep_auth_data：组装 Poly1305 认证数据

#### 4.2.1 概念说明

Poly1305 不会自己「知道」要认证哪些字节——它只管对喂进来的字节流做累加。所以需要一个模块把 RFC8439 规定的认证数据格式**逐拍拼好**：在密文前面加 AAD（含填充），在密文后面加长度字段。这个模块就是 `prep_auth_data`。

它是一个有状态的 FSM，输入是「密文流」，输出是「认证数据流」。难点在于：输出的拍数和形状都跟输入不同（多了前缀 AAD、后缀长度，还要补零对齐），而且它必须**把密文拍的 `tlast` 吞掉**——因为对 Poly1305 而言，真正的「最后一拍」是后面的长度拍，不是密文的最后一拍。

#### 4.2.2 核心流程

FSM 有 4 个状态（[prep_auth_data.h:10-15](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/prep_auth_data/prep_auth_data.h#L10-L15)）：`IDLE → AAD_STATE → CIPHERTEXT → LENGTHS → IDLE`。

```
IDLE: 等到第一个有效密文 beat。
      ├─ aad_len>0 ? → 存 AAD 到 reg, counter=aad_len, 进 AAD_STATE
      └─ aad_len=0 ? → 直接进 CIPHERTEXT, counter=0

AAD_STATE: 每拍输出≤16 字节 AAD, 不足补零(tkeep 全 1)。
           counter>16 则移位减 16 继续; 否则进 CIPHERTEXT。

CIPHERTEXT: 透传密文 beat。
            ★ 把 tlast 强压为 0 (后面还有 LENGTHS 拍)
            ★ 部分拍(tkeep 不全)的空字节补 0、tkeep 拉满
            用 axis128_keep_count 累计密文字节数到 counter
            输入 tlast 到达 → 进 LENGTHS

LENGTHS: 输出 1 拍 = le64(aad_len) ‖ le64(counter=密文长度)
         ★ tlast=1 (这才是认证数据的真正末拍)
         传输完成 → 回 IDLE
```

输出字节布局正是 RFC8439 的认证数据格式：

\[ \underbrace{\text{AAD} \,\|\, \text{pad}}_{\text{AAD\_STATE}} \,\|\, \underbrace{\text{密文} \,\|\, \text{pad}}_{\text{CIPHERTEXT}} \,\|\, \underbrace{\text{le}_{64}(\text{aad\_len}) \,\|\, \text{le}_{64}(\text{ct\_len})}_{\text{LENGTHS}} \]

其中 AAD 与密文都补齐到 16 字节边界，使 Poly1305 总能拿到完整的 16 字节块。

#### 4.2.3 源码精读

**外壳** [3.build/pipelinec_build/src/prep_auth_data/prep_auth_data.c:47-58](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/prep_auth_data/prep_auth_data.c#L47-L58) 把纯函数 `prep_auth_data_fsm`（在 `.h`）的输出接回全局 wire，是 PipelineC 典型的「FSM 逻辑放头文件、外壳负责实例化端口」写法。

**CIPHERTEXT 态**是本模块最值得读的一段：

[3.build/pipelinec_build/src/prep_auth_data/prep_auth_data.h:98-123](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/prep_auth_data/prep_auth_data.h#L98-L123) ——关键点：

- 第 101 行 `o.axis = axis_in;` 透传密文；
- **第 104 行 `o.axis.data.tlast = 0;`**：把密文自带的 `tlast` 强行压成 0。原因是这一拍对 Poly1305 不是末拍，末拍是后面的 LENGTHS。若让 `tlast` 漏过去，Poly1305 会以为数据流提前结束。
- 第 107-113 行：对 `tkeep` 不全的部分拍，把空字节填 0 并把 `tkeep` 全部拉成 1，等价于软件里的 `pad16` 补零；
- 第 116-117 行：用 `axis128_keep_count(axis_in.data)`（数 `tkeep` 里 1 的个数）累加真实密文字节数到 `counter`；
- 第 119-121 行：等输入 `tlast` 到达，切到 LENGTHS。注意这里读的是**输入**的 `tlast`，不是被压零的输出 `tlast`。

**LENGTHS 态**拼出最后一个认证数据拍：

[3.build/pipelinec_build/src/prep_auth_data/prep_auth_data.h:124-152](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/prep_auth_data/prep_auth_data.h#L124-L152) ——第 130-135 行写 `le64(aad_len)`，第 137-142 行写 `le64(counter)`（此时 counter 已累加成密文总长度）。第 132、139 行用 `>> (i*8)` 取出小端的每一字节。**第 144 行 `tlast=1`** 是认证数据流真正的末拍标记，Poly1305 见到它才会进入收尾、算最终 tag。

> 小端编码说明：`aad_len >> (i*8)` 取第 i 字节，i=0 是最低位字节先写，正是 little-endian，与 RFC8439 的 `le64` 一致。

#### 4.2.4 代码实践

**实践目标**：用测试台里的真实报文，手工推演 `prep_auth_data` 输出的拍序列与字节数。

**操作步骤**：

1. 打开测试台 [encrypt_tb.c:25-42](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305/encrypt_tb.c#L25-L42)，取第 0 个报文：
   - `AAD = "Additional authenticated data"` = 26 字节，`aad_len = 26`；
   - `PLAINTEXT0 = "Hello CHILIChips ..."` = 64 字节 → 密文也是 64 字节（ChaCha20 是流密码，等长）。
2. 假设下游每拍都 ready，按 FSM 逐拍列出 `prep_auth_data` 输出：
   - IDLE → AAD_STATE（因 aad_len=26>0）；
   - AAD_STATE 拍 1：AAD[0..15]（16 字节），counter 26>16 → 移位、counter=10；
   - AAD_STATE 拍 2：AAD[16..25]（10 字节）+ 6 字节零填充，tkeep 全 1，counter=10 不>16 → 进 CIPHERTEXT，counter 复位 0；
   - CIPHERTEXT 拍 1-4：透传 4 个密文 beat（64 字节，每拍 tlast 被压 0），counter 累加到 64，第 4 拍输入 tlast → 进 LENGTHS；
   - LENGTHS 拍：`le64(26) ‖ le64(64)`，tlast=1 → 回 IDLE。
3. 用 RFC 公式核对总字节数：

   \[ |\text{auth\_data}| = 26 + \text{pad}_{16}(26) + 64 + \text{pad}_{16}(64) + 16 = 26 + 6 + 64 + 0 + 16 = 112 \text{ 字节} \]

**预期结果**：共 7 拍（2 AAD + 4 密文 + 1 长度）= 112 字节，与公式一致。

#### 4.2.5 小练习与答案

**练习 1**：若 `aad_len = 0`（无 AAD），FSM 会跳过哪些状态？输出拍数怎么变？

> **答案**：IDLE 直接进 CIPHERTEXT（见 [prep_auth_data.h:58-63](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/prep_auth_data/prep_auth_data.h#L58-L63)），跳过 AAD_STATE。认证数据变成 `密文 ‖ pad ‖ le64(0) ‖ le64(ct_len)`，少了 AAD 那 2 拍。

**练习 2**：为什么 CIPHERTEXT 态必须读**输入**的 `tlast` 来判切换（第 119 行），却把**输出**的 `tlast` 压成 0（第 104 行）？

> **答案**：输入 `tlast` 是「密文流的真实末拍」信号，FSM 需要靠它知道密文结束了、该进 LENGTHS；但输出给 Poly1305 的 `tlast` 不能在这一拍拉高，否则 Poly1305 会提前收尾。所以「用输入 tlast 做内部控制、用输出 tlast=0 隐藏内部结构」。

---

### 4.3 append_auth_tag：把 tag 缝到密文末尾

#### 4.3.1 概念说明

到这一步，两条路要汇合了：

- **直通路**：密文（来自 4.1 的分叉）正一拍一拍往外送；
- **旁路**：Poly1305 算出的 16 字节 tag，要晚很多拍才到（它得等 `prep_auth_data` 把整段认证数据喂完）。

`append_auth_tag` 负责把两者拼成最终输出 `密文 ‖ tag`。它面对两个时机问题：

1. **密文原本的 `tlast` 现在不是末拍了**——后面还要跟一个 tag 拍。所以透传密文时必须把 `tlast` 抑制掉。
2. **tag 来得晚**：密文最后一拍送出去后，要原地等 tag 的 `valid` 到来，再补上「新末拍」。

#### 4.3.2 核心流程

2 状态 FSM（[append_auth_tag.c:20-23](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/auth_tag/append_auth_tag.c#L20-L23)）：`CIPHERTEXT ⇄ AUTH_TAG`。

```
CIPHERTEXT 态:
   透传密文 → 输出
   ★ 输出 tlast 强压为 0 (后面还有 tag 拍)
   检测到 输入 tlast & valid & ready (密文真末拍已送出)
     → 切 AUTH_TAG

AUTH_TAG 态:
   等 tag 的 valid (poly1305 慢, 在此停顿/反压)
   把 tag 16 字节拆进 tdata, tkeep 全 1
   ★ 输出 tlast=1 (这是最终输出的真末拍)
   传输完成 → 回 CIPHERTEXT, 准备下一个包
```

输出序列：`密文拍1 … 密文拍N（tlast 全 0）│ tag 拍（tlast=1）`，即 \(C \,\|\, \text{tag}\)。

#### 4.3.3 源码精读

**CIPHERTEXT 态**（透传 + 抑制 tlast）：

[3.build/pipelinec_build/src/auth_tag/append_auth_tag.c:37-49](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/auth_tag/append_auth_tag.c#L37-L49) ——第 40 行整拍透传；**第 43 行 `append_auth_tag_axis_out.data.tlast = 0;`** 是抑制点；第 44-48 行在「输入 tlast 且本拍真发生传输」时切到 AUTH_TAG（同样读输入 tlast 做控制）。

**AUTH_TAG 态**（缝 tag + 恢复 tlast）：

[3.build/pipelinec_build/src/auth_tag/append_auth_tag.c:50-61](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/auth_tag/append_auth_tag.c#L50-L61) ——

- 第 53 行 `UINT_TO_BYTE_ARRAY(..., POLY1305_AUTH_TAG_SIZE, ...)` 把 128 位 tag 拆成 16 字节 `tdata`（`POLY1305_AUTH_TAG_SIZE=16`，见 [poly1305.h:13](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305.h#L13)）；
- 第 54 行 `ARRAY_SET(...tkeep, 1, POLY1305_AUTH_TAG_SIZE)` 把 16 个 `tkeep` 全置 1（tag 是完整 16 字节）；
- **第 55 行 `tlast = 1`** 是恢复点：经过前面 N 拍 `tlast=0`，这里终于拉高，标记整个密文+tag 输出的真正末拍；
- 第 56 行 `valid = auth_tag_in.valid`：输出随 tag 的 valid 走。tag 没来时 valid=0，输出端自然停住、向上游反压，**这就是「等 tag」的停顿机制**；
- 第 58-60 行传输完成后回 CIPHERTEXT。

> 时序对比：Poly1305 用的是一个 4 深的多周期流水（MCP，见 [poly1305_mac.c:26](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305_mac.c#L26)），且要吃完整段认证数据才出 tag；而密文是 64 级流式流水几乎即时出。所以 AUTH_TAG 态的「等 tag」停顿是常态，设计正是靠它把两条节奏不同的路对齐。

#### 4.3.4 代码实践

**实践目标**：用 4.2 的同一报文，推演 `append_auth_tag` 的输出拍序列。

**操作步骤**：

1. 输入：4 拍密文（64 字节，第 4 拍带 `tlast=1`），tag 随后某拍 `valid=1`。
2. 推演输出：
   - 拍 1-4：CIPHERTEXT 态透传，每拍输出 `tlast=0`；第 4 拍检测到输入 tlast → 切 AUTH_TAG；
   - 拍 5：AUTH_TAG 态，等 tag valid；tag 到后输出 16 字节、`tkeep` 全 1、`tlast=1` → 回 CIPHERTEXT。
3. 核对总输出长度：4×16 + 16 = 80 字节 = 64 密文 + 16 tag。

**预期结果**：与测试台期望值 [encrypt_tb.c:46-60](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305/encrypt_tb.c#L46-L60) 的 `CIPHERTEXT0_SIZE = 64 + 16 = 80` 一致。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉第 43 行（不抑制 CIPHERTEXT 态的输出 tlast），会出现什么错？

> **答案**：密文第 4 拍会带着 `tlast=1` 送出去，下游以为输出到此结束；随后 AUTH_TAG 态又送出第 5 拍 tag，等于在「包尾」之后多塞了一拍，下游看到两个 tlast，包边界错乱。

**练习 2**：第 56 行让输出 valid 直接跟随 tag 的 valid，这为什么不会丢密文？

> **答案**：这行只在 AUTH_TAG 态生效，此时密文已全部送完，模块只负责输出 tag。tag 的 valid 没来时输出 valid=0、并经 ready 链反压，自然停住等待；不会影响之前已经送出的密文。

---

## 5. 综合实践

把本讲三处 `tlast` 抑制/恢复和两个 FSM 串成一张完整框图。**这是本讲的主实践**。

**实践目标**：画出加密 datapath 完整框图，标注每个 FSM 的状态与 `tlast` 的抑制/恢复时机。

**操作步骤**：

1. 横向画出 4 个模块：`chacha20 → [分叉] → {prep_auth_data → poly1305_mac, append_auth_tag} → 汇合输出`。
2. 在 `prep_auth_data` 框上标出 4 状态序列 `IDLE→AAD_STATE→CIPHERTEXT→LENGTHS`，并用红笔标出两处 tlast 操作：
   - CIPHERTEXT 态：**抑制**（输出 tlast=0，源码第 104 行）；
   - LENGTHS 态：**恢复/置位**（输出 tlast=1，源码第 144 行）。
3. 在 `append_auth_tag` 框上标出 2 状态 `CIPHERTEXT→AUTH_TAG`，标出两处 tlast 操作：
   - CIPHERTEXT 态：**抑制**（输出 tlast=0，源码第 43 行）；
   - AUTH_TAG 态：**恢复/置位**（输出 tlast=1，源码第 55 行）。
4. 在分叉处标注握手公式 `源 ready = prep_ready & append_ready`。
5. 用虚线标出 `poly_key` 旁路（chacha20 → poly1305_mac 的 key）与「等 tag」停顿（AUTH_TAG 态 valid 跟随 tag valid）。

**进阶（可选，需本地工具链）**：若已装好 PipelineC + GHDL + cocotb（且 `$PIPELINEC` 已指向可执行文件），可在 `3.build/pipelinec_build/` 下运行流水线仿真：

```bash
./build_sim_pipe.sh    # 跑加密 TB, 150 拍, 产物在 generated-files-sim-pipe/
```

**需要观察的现象**：仿真 log 里依次出现 `Encrypt: Input Plaintext next 16 bytes`（输入明文）与 `Encrypt: Output Ciphertext next 16 bytes`（输出密文+tag），最后出现 `Encrypt: Test 0 DONE!` 且无 `ERROR: ... mismatch`。

**预期结果**：两个测试串都 `DONE`、无 mismatch，证明整条加密 datapath（含分叉、prep_auth_data、append_auth_tag）输出与软件参考一致。

> 若无法确定运行结果，标注「待本地验证」。本讲义未实际执行该仿真。

---

## 6. 本讲小结

- 加密 datapath 是「明文 → ChaCha20 → 密文分叉 → 双路汇合」：密文一边直通输出、一边经 `prep_auth_data → poly1305_mac` 算 tag，最后由 `append_auth_tag` 把 tag 缝到密文末尾。
- **密文分叉**靠 `源 ready = 两接收方 ready 相与` + 巧妙的 `valid` 门控，实现「一份数据原子地同时投递给两个节奏不同的消费者」。
- **`prep_auth_data`** 是 4 状态 FSM（IDLE/AAD_STATE/CIPHERTEXT/LENGTHS），逐拍拼出 RFC8439 认证数据 `AAD‖pad‖密文‖pad‖长度`，并在 CIPHERTEXT 态**抑制** tlast、在 LENGTHS 态**置位** tlast。
- **`append_auth_tag`** 是 2 状态 FSM（CIPHERTEXT/AUTH_TAG），透传密文时**抑制** tlast、缝 tag 时**置位** tlast，并用「valid 跟随 tag valid」实现对慢 tag 的停顿等待。
- 三处 `tlast` 抑制/恢复是本讲的主线：它们的共同目的都是「重定义包尾」——因为加了 AAD 前缀、长度后缀和 tag 后缀后，原始密文的末拍不再是输出的末拍。

---

## 7. 下一步学习建议

- **解密与验证数据流**（u5-l4）：解密是加密的「镜像 + 安全增强」——`strip_auth_tag` 先剥 tag、`wait_to_verify` 用 128 字 FIFO 缓冲明文，**tag 校验通过才放行**，验证失败则丢弃。对比本讲的「先放行密文、后补 tag」，体会 AEAD 在收发两端的对称与不对称。
- **资源共享共享流水线**（u5-l5）：本讲的 chacha20/poly1305 是独立实例；共享变体用 1 位 `is_encrypt` 标签让加解密复用同一条 ChaCha20 流水线，会重新审视这里的分叉与汇合点。
- **继续阅读源码**：建议精读 [poly1305.h](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305.h) 的 `poly1305_mac` FSM，看清「吃完整段认证数据 → A_PLUS_S → OUTPUT_AUTH_TAG」是如何对应本讲假设的「tag 来得晚」的。
