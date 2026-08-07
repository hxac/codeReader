# Pypeline Python 前端与 RFC 修正

## 1. 本讲目标

Unit 5 前几讲（u5-l1～u5-l5）我们读完的是 `3.build/pipelinec_build/` 里**用 C 写**的 ChaCha20-Poly1305 硬件设计。本讲转到它的姊妹目录 `3.build/pypeline_build/`——用 **Pypeline（PipelineC 的 Python 前端）** 把同一套设计重写了一遍。

这次重写不是单纯换个语言，而是带着两个明确目的：

1. **修掉 C 版 Poly1305 的「三连数学 bug」与密文长度 padding**，让 AEAD 真正符合 RFC 8439，能和任何合规的 WireGuard peer 互通。
2. **改造硬件接线方式**，从「全局 Wire + 多个 `@MAIN`」的旧风格，迁移到「普通函数调用 + `Feedback` + `@interface` 端口」的新风格，让数据流图像正常调用图一样可读。

学完本讲，读者应该能够：

- 说清 Pypeline 相对 C 版做了哪些接线改进（函数调用替代全局 Wire、`Feedback[T]` 处理反向 ready、`@interface` 端口与接口函数自动生成反向接线）。
- 逐条指出 C 版 `poly1305.h` 的三处 limb 数学 bug（乘法截断、模运算掩码错误、丢弃高位 limb），并解释 Pypeline 版 `poly1305.py` 的对应修正。
- 理解用 `cryptography` 参考模型做「已知答案自校验（known-answer test）」的方法，以及为什么它能让 DUT（被测设计）不用于验证自身。

## 2. 前置知识

本讲假设你已经读过：

- **u5-l1**：ChaCha20-Poly1305 的 AEAD 构造、key/nonce/tag 尺寸、Poly1305 在素数 \(p = 2^{130} - 5\) 上做 MAC 的原理。
- **u5-l2**：PipelineC 的 HLS 工作流（把 C 函数编译成 Verilog 流水线）、`#pragma MAIN_MHZ/PART`、`#define INST + #include` 的多实例化手法、`DECL_INPUT/OUTPUT` 端口展平。
- **u5-l3 / u5-l4**：加密/解密数据流的分叉与汇合，以及 prep_auth_data 如何拼出 `AAD‖pad‖密文‖pad‖le64(aad_len)‖le64(ct_len)` 这条认证数据。

下面补充几个本讲要用、但前面没细讲的术语：

- **Pypeline**：PipelineC 的 Python 前端。你用 Python 写硬件函数（`@hw_func`）、结构体（`@struct`）、状态机，Pypeline 把它编译成可综合的 Verilog/VHDL。它的核心心智模型是：**一次普通的函数调用就等于例化了一个硬件子模块，每个调用点各有一份独立的状态**——不需要像 C 版那样靠全局 Wire 连模块。
- **`Feedback[T]`**：Pypeline 里一种「同一拍（组合）反向信号」——它的驱动赋值在文本上**晚于**它的第一次读取出现。硬件里 ready（反压）是从下游往上游流的，恰好需要这种「先读后写、同一拍」的表达。
- **`@interface` 端口**：把一个握手端口拆成「正向（feedforward，数据）」和「反向（reverse，ready）」两半。同一个端口名既出现在函数参数里（其中一半）也出现在返回结构体里（另一半），用 `_if` 后缀标记「这是一根双向端口，不是两根同名单向线」。
- **limb（肢）表示法**：用一个 `uint64_t` 数组去拼一个超宽整数。本讲里 `u320_t` 是 5 个 64 位 limb 拼出的 320 位数，`limbs[0]` 是最低 64 位、`limbs[2]` 的第 0 位对应 \(2^{128}\)。
- **RFC 8439 §2.8.2 测试向量**：官方给的一组「明文 → 已知密文 + 已知 tag」标准答案（那句著名的 *Ladies and Gentlemen of the class of '99 … sunscreen*）。任何自称合规的实现都必须能对上它。

> 现状提示（承接 u4-l5 / u5-l2）：当前 HEAD 处于 Phase1 PoC，加密核尚未焊进 `top.filelist`。`pypeline_build/` 与 `pipelinec_build/` 一样是**可独立仿真、尚未编入 SoC** 的设计源码。本讲聚焦这两份源码本身的正确性与工程风格。

## 3. 本讲源码地图

本讲涉及的关键文件，按主题分组：

| 文件 | 作用 |
| --- | --- |
| [3.build/pypeline_build/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/README.md) | 本目录的总说明，明确写了「修了 C 版的数学/长度 bug」与「接线风格旧 vs 新」两段，是本讲的纲领。 |
| [3.build/pypeline_build/src/poly1305/poly1305.py](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/poly1305/poly1305.py) | **修正后**的 Poly1305：320 位 limb 数学（`uint320_mul`/`uint320_fold`/`uint320_mod_prime`）+ MAC FSM + `poly1305_mac_instance`。 |
| [3.build/pipelinec_build/src/poly1305/poly1305.h](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305.h) | **有 bug 的 C 原版**，Pypeline 版逐行移植自它，三处 bug 都在这里。对比阅读的核心对象。 |
| [3.build/pypeline_build/src/chacha20/chacha20.py](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/chacha20/chacha20.py) | ChaCha20 核 + `chacha20_instance`（FSM 与私有流水线合并的接口函数）。本讲关注它「只 XOR 保留 lane」的 keep 处理，以及作为接线改进的范例。 |
| [3.build/pypeline_build/src/prep_auth_data/prep_auth_data.py](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/prep_auth_data/prep_auth_data.py) | 认证数据组装 FSM。本讲关注它用 `axis128_keep_count` 累加**真实**密文字节数写入长度字段。 |
| [3.build/pypeline_build/src/chacha20poly1305/aead_ref_model.py](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/chacha20poly1305/aead_ref_model.py) | 纯 Python 参考模型，调 `cryptography` 包做标准 RFC 8439，并在导入时跑 §2.8.2 自检。 |
| [3.build/pypeline_build/src/aead_types.py](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/aead_types.py) | 共享尺寸/类型，含 `axis128_keep_count` 工具。 |
| [3.build/pypeline_build/build.py](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/build.py) | 统一构建脚本，实践的入口。 |

---

## 4. 核心概念与源码讲解

本讲三个最小模块：

1. **Pypeline 接线改进**——从「全局 Wire + `@MAIN`」到「函数调用 + `Feedback` + `@interface`」。
2. **Poly1305 数学三连 bug 的修正**——乘法截断、掩码错误、丢弃高位 limb。
3. **参考模型自校验**——`cryptography` 包 + RFC 8439 §2.8.2 已知答案。

---

### 4.1 Pypeline 接线改进：从全局 Wire 到函数调用

#### 4.1.1 概念说明

在 C 版（`pipelinec_build/`）里，「一个模块」是一组**模块级全局 `Wire[T]` 变量**加上一个或多个 `@MAIN` 函数。模块之间靠一个外层 `@MAIN` 读 A 模块的输出全局变量、再写到 B 模块的输入全局变量来连线。这带来两个工程痛点：

- **跨模块连线不可读**：真正的数据流图藏在「字段命名约定」里，读代码必须追着字段名跑，而不是顺着正常的函数调用图。`encrypt_dataflow.c` 一次要传约 20 个标量全局量。
- **FSM 与数据通路被劈成两个 `@MAIN`**：`chacha20`、`poly1305_mac`、`wait_to_verify` 各自其实是「一个状态机 + 一条流水线/FIFO」，却被迫写成两个 `@MAIN`、再用更多 Wire 拼回去。

Pypeline 的心智模型更干净：**一次普通的（非 `@MAIN`）函数调用就例化了一个硬件子模块，每个调用点有自己独立的状态，不需要全局 Wire**。数据沿调用链正向流，ready（反压）沿调用链反向流。于是产生一个新需求——怎么在「同一拍」把下游的反向 ready 喂给上游？答案就是 `Feedback[T]`。

#### 4.1.2 核心流程

Pypeline 新接线的四件套：

1. **直接调用**：在 `encrypt_dataflow_core.py` / `decrypt_dataflow_core.py` 里直接 `strip = strip_auth_tag.strip_auth_tag(axis_in=...)`、`chacha = chacha_func(...)`、`mac = poly1305.poly1305_mac_instance(...)`，把上一个调用的结构体字段链进下一个调用的参数。数据正向流、ready 反向流。
2. **`Feedback[T]` 补反向边**：当某下游调用的反向输出要当作上游调用的输入时，声明一个 `Feedback[T]` 局部量——它是同一拍的组合信号，驱动赋值文本上**后于**第一次读取。
3. **`@interface` 端口**：握手端口拆成正反两半，两半共用同一名字，`_if` 后缀表示「一根双向端口」。这替换了 C 版乱糟糟的 `ready_for_<name>` 命名。
4. **接口函数自动生成反向接线**：像 `chacha20_instance`、`poly1305_mac_instance` 这种「FSM + 私有流水线/MCP 组成环路」的合并层，**只手写正向方向，反向接线由 pass 自动生成**。

唯一的例外是 `chacha20_pipeline_shared.py`：它真的是一个被加密/解密两条独立数据流共享的、带仲裁的资源，所以它保留 8 根 `Wire` 和独立的 `@MAIN`——这是真实的共享需求，不是旧风格的残留。

#### 4.1.3 源码精读

先看 README 对「旧 vs 新」的总结，它点明了两种旧风格与四项新做法：

- 旧风格与对应的 Pypeline 改造，见 [3.build/pypeline_build/README.md:L342-L401](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/README.md#L342-L401)——说明「跨模块连线」和「FSM+数据通路劈两半」两种旧风格，以及「直接调用 + `Feedback[T]`」「FSM+数据通路合并」「工厂函数」三项新做法。
- `@interface` 端口与自动生成反向接线的规则，见 [3.build/pypeline_build/README.md:L402-L449](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/README.md#L402-L449)——说明端口两半同名、`_if` 后缀、以及「只写正向、反向由 pass 生成」的接口函数机制。

接口函数最典型的例子是 `chacha20_instance`。它的 `_wiring` 函数体**只写了正向**，FSM 消费流水线产物、流水线在 FSM 之后被调用，二者构成环路，于是 pass 在正向边和反向边都放上 `Feedback`：

```python
def chacha20_instance_wiring(key, nonce, axis_in_if: axis128_intrf) -> chacha20_ports:
    fsm_out = chacha20_fsm(
        key=key, nonce=nonce, axis_in_if=axis_in_if, from_pipeline_if=pipe.stream_out_if
    )
    pipe = pipeline_func(stream_in_if=fsm_out.to_pipeline_if)
    return chacha20_ports(key_if=fsm_out.key_if, axis_out_if=fsm_out.axis_out_if)
```

> 代码出自 [3.build/pypeline_build/src/chacha20/chacha20.py:L387-L396](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/chacha20/chacha20.py#L387-L396)。注意 `pipe` 在 `fsm_out` 里被先读（`pipe.stream_out_if`）后才被赋值——这正是「同一拍、先读后写」的组合环路，靠 `Feedback` 表达。`poly1305_mac_instance_wiring` 的结构完全对称，见 [3.build/pypeline_build/src/poly1305/poly1305.py:L375-L390](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/poly1305/poly1305.py#L375-L390)。

数据流核心 `decrypt_dataflow_core` 则展示了「直接调用 + 分叉」的完整调用图（README 转贴如下），从中可读出从 strip → 广播分叉 → chacha / prep → mac → verify → wait_to_verify 的整条链：

```python
def decrypt_dataflow_core(axis_in_if, key, nonce, aad, aad_len) -> ...:
    strip  = strip_auth_tag.strip_auth_tag(axis_in=axis_in_if)
    bcast  = axis128_2broadcast(axis_in=strip.axis_out)
    chacha = chacha_func(key=key, nonce=nonce, axis_in_if=bcast.axis_out[1])
    prep   = prep_auth_data.prep_auth_data_fsm(aad=aad, aad_len=aad_len, axis_in=bcast.axis_out[0])
    mac    = poly1305.poly1305_mac_instance(key_if=chacha.key_if, data_in_if=prep.axis)
    verify = poly1305_verify_decrypt.poly1305_verify_decrypt(auth_tag=strip.auth_tag_out, calc_tag=mac.auth_tag_if)
    wtv    = wait_to_verify.wait_to_verify(axis_in=chacha.axis_out_if, verify_bit=verify.tags_match)
    return ...
```

> 见 [3.build/pypeline_build/README.md:L457-L471](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/README.md#L457-L471)。README 指出这段体里会由 pass 生成 9 个 `Feedback`（加密侧 6 个）。其中 `axis128_2broadcast` 的 `axis_out` 是数组接口端口，两路分叉各自独立反压、反向数组自动拼装——旧版手写的 `sink_ready_s[2]` 数组因此被淘汰。

**工厂函数**解决「唯一真实的变化轴」：只有 chacha20 的具体实例在「独立构建（自带私有流水线）」与「共享构建（用一条带仲裁的流水线）」之间不同。所以两个 core 都是 `make_*_dataflow_core(chacha_func)` 工厂，`encrypt_dataflow.py` 用 `chacha20.chacha20_instance` 实例化它，`encrypt_dataflow_shared.py` 用 `chacha20_pipeline_shared.chacha20_encrypt_shared` 实例化同一个工厂。这与 `chacha20.py` 自己的 `make_quarter_round` 是同一个「编译期闭包」惯用法。

#### 4.1.4 代码实践

**实践目标**：用「调用图可读性」直观感受接线改进。

**操作步骤**：

1. 打开 [3.build/pypeline_build/README.md:L457-L471](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/README.md#L457-L471) 的 `decrypt_dataflow_core` 片段，从上到下顺着变量名画出解密数据流框图：`strip` → `bcast`（一分为二）→ 上支 `prep → mac`、下支 `chacha` → `verify`（把 strip 出来的 tag 与 mac 算出的 tag 比对）→ `wtv`（扣押明文等判决）。
2. 对照 C 版的同名逻辑（在 `pipelinec_build/` 里它分散在一个外层 `@MAIN` 对一组全局 Wire 的赋值中），体会「顺着调用读」与「追字段名读」的差异。
3. 在框图上用红色标出「反向」的 ready 流向（从 `wtv` 一路反压回 `strip`），并圈出哪些点需要 `Feedback`。

**需要观察的现象**：你会看到正向数据流与函数调用顺序一致、非常易读；而 ready 是「逆调用方向」流动的，每一段逆流都需要一个 `Feedback`。这正是 README 所说「9 个 `Feedback`（解密）/ 6 个（加密）由 pass 生成」的来源。

**预期结果**：画出一张「正向调用链 + 红色反向 ready」的图，能指出 `axis128_2broadcast` 处的反压要两路相「与」合并回单根 ready（木桶效应）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `chacha20_instance_wiring` 里 `pipe` 可以在赋值之前就被 `fsm_out` 的构造读取？

**参考答案**：因为 `pipe` 是 `Feedback` 量——Pypeline 允许「同一拍内、驱动赋值文本上晚于第一次读取」的组合信号。`chacha20_fsm` 与 `pipeline_func` 构成一个组合环路（FSM 消费流水线输出，流水线消费 FSM 输出），环路本身就要求这种先读后写的表达；它不是一个时序 bug，而是对真实组合回路的如实描述。

**练习 2**：`chacha20_pipeline_shared.py` 为什么**不**被改成普通函数调用，而保留独立 `@MAIN` 和 8 根 `Wire`？

**参考答案**：因为它是一个真正被加密、解密两条互相独立的数据流共享、且带输入仲裁的资源（round-robin 复用 + ID 解复用）。这种「跨两条独立图的共享态」用全局 Wire + 独立 `@MAIN` 表达才是真实的，不是旧风格的残留，所以被有意保留。

---

### 4.2 Poly1305 数学三连 bug 的修正

#### 4.2.1 概念说明

这是本讲最硬核的部分，也是 Pypeline 版相对 C 版最关键的「正确性」改动。

C 版 `poly1305.h` 是 Pypeline 版逐行移植的源头，但它的 320 位 limb 数学有**三个互相纠缠的 bug**，合在一起使它算出的 tag 是一个「非标准 MAC」——在本设计自己的加密/解密两条路径之间内部自洽（因为两边用同一套错算），但**无法与任何符合 RFC 8439 的 peer 互通**。Pypeline 版把三处都修了，因此**故意**与 C 版产生不同的 tag 和不同的密文长度。

Poly1305 在 \(p = 2^{130} - 5\) 上做运算，每个 16 字节块执行：

\[
a \;\leftarrow\; \big((a + n_i) \cdot r\big) \bmod p
\]

其中 \(n_i\) 是本块数据（高位补一个 \(2^{128}\) 的 set bit），\(r\) 是经 clamping 的密钥半部。难点全在那个 \(\bmod p\)——要把一个可能高达约 \(2^{260}\) 的乘积压回 130 位以内。

#### 4.2.2 核心流程

关键恒等式是：

\[
2^{130} \equiv 5 \pmod p \quad(\text{因为 } p = 2^{130}-5 \equiv 0)
\]

所以对任意 \(x\)，写成 \(x = q\cdot 2^{130} + \text{rem}\)（rem 是低 130 位），就有：

\[
x \equiv \text{rem} + 5q \pmod p
\]

这就是「fold（折叠）」操作——它把 \(2^{130}\) 及以上的位「乘以 5 折回低位」。每 fold 一次，位长大约减少 127 位。对 320 位输入，三次 fold 一定把它压到 \(2^{130}\) 以下，再视情况做一次「减 \(p\)」取规范代表元（落在 \([0, p)\)）：

\[
320\text{ 位} \xrightarrow{\text{fold}} < 2^{193} \xrightarrow{\text{fold}} < 2^{130}+2^{66} \xrightarrow{\text{fold}} < 2^{130} \xrightarrow{\text{必要时减 }p} [0,\,2^{130}-5)
\]

> 位长估算：第一次 fold 后 \(q < 2^{190}\)、\(5q < 2^{193}\)；第二次 \(q < 2^{63}\)、\(5q < 2^{66}\)；第三次输入 \(< 2^{130}+2^{66}\)，其 \(q < 2\)，\(5q\) 已可忽略。README 用同样的推导，见 [3.build/pypeline_build/README.md:L326-L334](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/README.md#L326-L334)。

**位与 limb 的对应**（理解 bug 2、3 的前提）：每个 limb 是 64 位，`limbs[0]`=位 0–63，`limbs[1]`=位 64–127，`limbs[2]`=位 128–191。所以：

- \(2^{128}\) 是 `limbs[2]` 的第 0 位；\(2^{130}\) 是 `limbs[2]` 的**第 2 位**。
- 「低于 \(2^{130}\)」在 `limbs[2]` 内只占**最低 2 位** → 掩码应为 `0x3`。

#### 4.2.3 源码精读

Pypeline 版 `poly1305.py` 的模块 docstring 直接列出了三处 bug 与修正意图，是本模块的「自白书」：见 [3.build/pypeline_build/src/poly1305/poly1305.py:L7-L16](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/poly1305/poly1305.py#L7-L16)。

**bug 1：`uint320_mul` 截断 64×64 乘积**

C 版把每个 limb 对的乘积存进一个 `uint64_t`，于是 64×64 乘法被截断成低 64 位，高 64 位直接丢失，`carry` 只承接「加法进位」（最多 1），从不承接「乘法的高半」：

```c
uint64_t product = a.limbs[i] * b.limbs[j];   // 被截断成低 64 位！
...
low = product + old_value;
high = (low < product) ? 1 : 0;               // 仅加法进位
```

> 见 [3.build/pipelinec_build/src/poly1305/poly1305.h:L121-L145](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305.h#L121-L145)，关键行 L133。

Pypeline 修正：用 `uint128_t` 承接**完整 128 位**乘积，并把高 64 位作为进位传给下一个 limb：

```python
acc: uint128_t = (a.limbs[i] * b.limbs[j]) + temp.limbs[i + j] + carry
temp.limbs[i + j] = acc       # 低 64 位
carry = acc >> 64             # 高 64 位——这就是被 C 版丢掉的那半
```

> 见 [3.build/pypeline_build/src/poly1305/poly1305.py:L124-L141](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/poly1305/poly1305.py#L124-L141)。注释里写明 `acc` 三项之和至多 \(2^{128}-1\)，`uint128_t` 恰好无损容纳。

**bug 2：`uint320_mod_prime` 用错掩码**

C 版声明 `uint64_t mask = 0x3FFFFFFFFFF; // 2^130 - 1`，注释自欺——\(2^{130}-1\) 是个 130 位数，硬塞进 64 位 limb 当掩码就成了 42 位（`0x3FFFFFFFFFF`）。结果是它把 `limbs[2]` 的**低 42 位**都当成「低于 \(2^{130}\)」保留，只把 42 位以上的位折回，完全错位：

```c
uint64_t mask = 0x3FFFFFFFFFF;            // 错！应是 0x3
uint64_t high_bits = a.limbs[2] & ~mask;
...
a.limbs[2] &= mask;
```

> 见 [3.build/pipelinec_build/src/poly1305/poly1305.h:L166-L188](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305.h#L166-L188)，关键行 L169。

Pypeline 修正：正确认识到「\(2^{130}\) 是 limb 2 的第 2 位」，掩码就是 `0x3`：

```python
# 2^130 是 limb 2 的第 2 位，所以 limb 2 内低于 2^130 的只是低两位
_MASK = 0x3
```

> 见 [3.build/pypeline_build/src/poly1305/poly1305.py:L57-L59](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/poly1305/poly1305.py#L57-L59)。

**bug 3：`uint320_mod_prime` 直接丢弃 limb 3/4**

C 版清零 `limbs[3]`、`limbs[4]` 却**不把它们的值折回**低位，只处理 `limbs[2]` 里高于掩码的位：

```c
a.limbs[2] &= mask;
a.limbs[3] = 0;          // 直接清零，不折回
a.limbs[4] = 0;          // 直接清零，不折回
mul5.limbs[0] = (high_bits >> 2) * 5;   // 只折 limb2 高位
```

> 同在 [3.build/pipelinec_build/src/poly1305/poly1305.h:L177-L184](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305.h#L177-L184)。

Pypeline 修正：把折叠操作独立成 `uint320_fold`，它**横跨 limb 2/3/4** 把所有高于 \(2^{130}\) 的位都算进 \(q\)，乘以 5 折回：

```python
q0: uint64_t = (v.limbs[2] >> 2) | (v.limbs[3] << 62)
q1: uint64_t = (v.limbs[3] >> 2) | (v.limbs[4] << 62)
q2: uint64_t = v.limbs[4] >> 2
# mul5 = 5*q，带进位地折回低位
```

> 见 [3.build/pypeline_build/src/poly1305/poly1305.py:L144-L167](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/poly1305/poly1305.py#L144-L167)。注意 `q0/q1/q2` 用「跨 limb 拼接」把 limb 3、limb 4 的位都纳入商，没有任何位被丢弃。

三个 fold + 一次条件减 \(p\)，就是修正后的完整 `uint320_mod_prime`：见 [3.build/pypeline_build/src/poly1305/poly1305.py:L170-L196](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/poly1305/poly1305.py#L170-L196)。

**三个 bug 为何「互相纠缠」**：bug 1 让乘积人为地变小，乘积根本到不了 limb 3，于是 bug 3「丢弃 limb 3/4」长期不暴露（被丢的本就是 0）。一旦把 bug 1 修对、乘积真的进到 limb 3，bug 3 就立刻变成真 bug。所以三处必须一起修，缺一不可。bug 2（掩码）则是独立的位级错误。这三条共同决定了 C 版的 tag 是个自洽但非标准的 MAC。

**配套修复：真实密文长度（keep 处理）**

C 版曾把密文长度向上取整到 16 字节的整数倍（测试台把末 word 的 16 个 lane 全标「保留」，ChaCha20 对整块 64 字节做 XOR，等于加密了自己的零填充），Poly1305 的长度字段于是认证的是「填充后长度」，违反 RFC 8439。

Pypeline 修法贯穿三处：

1. 测试台在末 word 上驱动**精确的逐 lane `keep`**；
2. `keep` 一路透传整个数据通路；
3. `chacha20_loop_body` 只对保留 lane 做 XOR，非保留 lane 强制清零，杜绝原始密钥流字节泄漏：

```python
for i in range(CHACHA20_BLOCK_SIZE):
    axis_out.frag.data[i] = 0
    if inputs.axis_in.frag.keep[i]:
        axis_out.frag.data[i] = inputs.axis_in.frag.data[i] ^ keystream[i]
```

> 见 [3.build/pypeline_build/src/chacha20/chacha20.py:L203-L217](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/chacha20/chacha20.py#L203-L217)。

4. 于是 `prep_auth_data_fsm` 用 `axis128_keep_count` 按 `keep` 位累加**真实**密文字节数，写进 Poly1305 长度字段：

```python
counter = counter + aead_types.axis128_keep_count(axis_in_if.stream.data.frag)
```

> 见 [3.build/pypeline_build/src/prep_auth_data/prep_auth_data.py:L118-L120](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/prep_auth_data/prep_auth_data.py#L118-L120)；`keep_count` 工具定义在 [3.build/pypeline_build/src/aead_types.py:L56-L57](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/aead_types.py#L56-L57)。README 对此修复的说明见 [3.build/pypeline_build/README.md:L293-L307](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/README.md#L293-L307)。

最终每块的处理主体（`a += n` → `a *= r` → `a %= p`）在 [3.build/pypeline_build/src/poly1305/poly1305.py:L217-L237](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/poly1305/poly1305.py#L217-L237)，三步分别调用上面修好的 `uint320_add`/`uint320_mul`/`uint320_mod_prime`。

#### 4.2.4 代码实践

**实践目标**（即本讲义规格指定的实践）：对比 C 与 Pypeline 的 poly1305 实现，定位三处数学差异，并说明为何 C 版产生的 tag 无法与合规 peer 互通。

**操作步骤**：

1. 并排打开两份乘法：
   - C：[poly1305.h:L121-L145](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305.h#L121-L145)
   - Pypeline：[poly1305.py:L124-L141](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/poly1305/poly1305.py#L124-L141)

   找出差异①：C 的 `product` 是 `uint64_t`（乘积被截断），Pypeline 的 `acc` 是 `uint128_t`（完整乘积 + 高 64 位进位）。
2. 并排打开两份模运算：
   - C：[poly1305.h:L166-L188](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305.h#L166-L188)
   - Pypeline：[poly1305.py:L144-L196](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/poly1305/poly1305.py#L144-L196)

   找出差异②（掩码）：C `0x3FFFFFFFFFF`（42 位） vs Pypeline `_MASK = 0x3`（2 位）。
   找出差异③（高位 limb）：C 直接 `limbs[3]=0; limbs[4]=0;` vs Pypeline `uint320_fold` 用 `q0/q1/q2` 跨 limb 2/3/4 全部折回。
3. 用一句话回答「为何无法互通」。

**需要观察的现象**：三处差异都不是「优化」，而是「正确性」分歧。C 版算出的 tag 与 Pypeline 版（及任何合规实现）不同。

**预期结果**：写出三处差异的对照表，并得到结论——ChaCha20-Poly1305 是确定性算法，给定相同 (key, nonce, aad, plaintext)，RFC 8439 规定唯一的 (ciphertext, tag)。C 版的数学偏离了 RFC，故它算出的 tag 与合规 peer 用标准算法算出的 tag 不相等；Poly1305 验证是「收到的 tag == 用收到的密文重算的 tag」的逐位比对（见 u5-l4），两边算法不一致必然比对失败，于是**包被当作篡改丢弃、隧道无法建立**。又因为 C 版的加密与解密共用同一套错算，所以「自己跟自己」能通——这正是 bug 长期没被发现的原因。

> 若想跑通验证，可用本目录的原生仿真（见 4.3.4）。若本地未配置 `$PYPELINEC`，上述对照阅读本身就是合格的「源码阅读型实践」，结论明确写为「待本地运行验证 tag 差异」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `2^{130}` 对应 `limbs[2]` 的「第 2 位」而不是「第 0 位」？

**参考答案**：`limbs[2]` 起始位是 \(2 \times 64 = 2^{128}\)，即它的第 0 位是 \(2^{128}\)、第 1 位是 \(2^{129}\)、第 2 位是 \(2^{130}\)。所以 `limbs[2]` 内「低于 \(2^{130}\)」的只有最低 2 位，掩码 `0x3`。

**练习 2**：bug 1（乘法截断）和 bug 3（丢弃高位 limb）为什么说是「互相掩盖」？

**参考答案**：bug 1 把每个 limb 对乘积截断到 64 位、丢掉高半，使整个 `uint320_mul` 的结果人为变小、几乎到不了 limb 3。既然 limb 3、limb 4 经常是 0，bug 3 把它们清零不折回就「看不出错」。一旦修好 bug 1，乘积恢复正常、真正占用 limb 3，bug 3 立即造成结果错误。所以单独修任何一个都不够，必须三处同修。

**练习 3**：`uint320_fold` 为什么固定做三次、而不是一次或两次？

**参考答案**：单次 fold 让位长从 320 降到约 193，第二次降到约 \(2^{130}+2^{66}\)，第三次才保证严格低于 \(2^{130}\)。少于三次无法保证把任意 320 位输入压到 \(2^{130}\) 以下；多于三次则冗余。

---

### 4.3 参考模型自校验：cryptography 包 + RFC8439 已知答案

#### 4.3.1 概念说明

光把数学改对还不够，还得**证明**它对了。密码学里证明实现正确，标准做法是「已知答案测试（known-answer test, KAT）」：拿官方规格里给定的输入，看你的输出是否等于官方答案。

这里有一个微妙但关键的原则：**被测设计（DUT）不能用来验证它自己**。如果用硬件自己的 poly1305 去生成「期望向量」、又用同一个 poly1305 去比对，那只能证明「它自洽」，不能证明「它合规」（C 版正是「自洽但不合规」的活例子）。

所以本目录引入一个**独立的纯 Python 参考模型** `aead_ref_model.py`：

- 它**不 import** `pypeline`、不 import `chacha20.py`/`poly1305.py`、完全不碰硬件设计。
- 它直接调用业界成熟的 `cryptography` 包里的标准 `ChaCha20Poly1305`——一个独立、经过广泛审计、符合 RFC 8439 的实现。
- 硬件测试台向它要「期望向量」，再把硬件输出与期望向量比对。于是硬件是被一个**外部独立实现**检验的。

#### 4.3.2 核心流程

参考模型工作流：

1. **生成期望向量**：`generate_encrypt_vector(key, nonce, aad, plaintext)` 调 `cryptography` 的 `ChaCha20Poly1305.encrypt`，返回「精确长度密文 + 16 字节 tag」。
2. **导入期自检**：模块一被 import，就立刻用 RFC 8439 §2.8.2 的官方向量跑一次自检；若 `cryptography` 包装坏了或版本不对，**在 elaboration（展开）阶段就报错**，而不是等到测试台里出现莫名其妙的 `ERROR`。
3. **两种测试台共用它**：
   - 可综合风格 `tb_common.py`：在 elaboration 时为 8 条固定明文一次性算好期望密文/tag，烤进定长硬件寄存器数组。
   - 非可综合风格 `tb_common_sim.py`：仿真中**每生成一个随机包就懒加载地调一次**，明文长度完全随机（1–1024 字节）。

#### 4.3.3 源码精读

`generate_encrypt_vector` 把 `cryptography` 的「密文+tag 连体」输出拆成两段，并强调密文是精确长度（`len(ciphertext) == len(plaintext)`）：

```python
def generate_encrypt_vector(key, nonce, aad, plaintext):
    """Returns (ciphertext, tag): the exact-length ciphertext
    (len(ciphertext) == len(plaintext)) and the 16-byte Poly1305 auth tag,
    per RFC 8439."""
    ct_and_tag = ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)
    return ct_and_tag[:-POLY1305_TAG_LEN], ct_and_tag[-POLY1305_TAG_LEN:]
```

> 见 [3.build/pypeline_build/src/chacha20poly1305/aead_ref_model.py:L31-L36](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/chacha20poly1305/aead_ref_model.py#L31-L36)。

导入期的 §2.8.2 已知答案自检——明文正是那句 *Ladies and Gentlemen of the class of '99 … sunscreen*，期望密文前 8 字节 `d31a8d34648e60db`、期望 tag `1ae10b594f09e26a7e902ecbd0600691`，对不上就抛 `AssertionError`：

```python
_ct, _tag = generate_encrypt_vector(
    bytes(range(0x80, 0xA0)),                                  # key
    bytes([0x07,0,0,0, 0x40,0x41,0x42,0x43,0x44,0x45,0x46,0x47]),# nonce
    bytes.fromhex("50515253c0c1c2c3c4c5c6c7"),                  # aad
    b"Ladies and Gentlemen of the class of '99: If I could offer you only one tip ...",
)
assert (_ct[:8].hex() == "d31a8d34648e60db"
        and _tag.hex() == "1ae10b594f09e26a7e902ecbd0600691"), \
       "cryptography package failed the RFC 8439 2.8.2 known-answer test"
```

> 见 [3.build/pypeline_build/src/chacha20poly1305/aead_ref_model.py:L39-L50](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/chacha20poly1305/aead_ref_model.py#L39-L50)。

模块 docstring 还有一段重要的「历史说明」：这个文件**曾经**是从硬件那段 buggy 数学逐字抄过来的（连 padding 长度 bug 一起抄），纯粹为了让旧测试向量继续对得上；硬件 bug 修好后，它才「回归」成标准 AEAD。见 [aead_ref_model.py:L13-L24](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/chacha20poly1305/aead_ref_model.py#L13-L24)。这段历史恰好是「DUT 不能验证自身」原则的反面教材——当参考模型照抄了 DUT 的 bug，它就失去了「独立」二字，测试全过却依然不合规。

非可综合测试台的随机化配置（固定 key/nonce/aad，包长随机但前几个钉死在边界长度，默认种子 8439 致敬 RFC），见 [3.build/pypeline_build/src/chacha20poly1305/tb_common_sim.py:L25-L43](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/chacha20poly1305/tb_common_sim.py#L25-L43)。边界长度 `[16, 17, 64, 128]` 分别覆盖「恰好一个 AXIS word」「一个 word + 一字节」「恰好一个 ChaCha20 块」「word/块公倍数」，正是 4.2 节 keep/长度修复最需要覆盖的场景。

#### 4.3.4 代码实践

**实践目标**：亲手用 `cryptography` 包复算 RFC 8439 §2.8.2 向量，确认参考模型与官方答案一致；再跑一次 Pypeline 原生仿真，确认硬件对得上参考模型。

**操作步骤**：

1. **复算官方向量**（纯 Python，无需任何硬件工具）：
   ```bash
   python3 -c "
   from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
   key=bytes(range(0x80,0xA0))
   nonce=bytes([0x07,0,0,0,0x40,0x41,0x42,0x43,0x44,0x45,0x46,0x47])
   aad=bytes.fromhex('50515253c0c1c2c3c4c5c6c7')
   msg=b\"Ladies and Gentlemen of the class of '99: If I could offer you only one tip for the future, sunscreen would be it.\"
   ct=ChaCha20Poly1305(key).encrypt(nonce,msg,aad)
   print('ct[:8] =', ct[:-16][:8].hex())
   print('tag    =', ct[-16:].hex())
   "
   ```
2. 比对输出：`ct[:8]` 应为 `d31a8d34648e60db`，`tag` 应为 `1ae10b594f09e26a7e902ecbd0600691`——与 [aead_ref_model.py:L47-L50](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/chacha20poly1305/aead_ref_model.py#L47-L50) 的断言一致。
3. **（可选，需 `$PYPELINEC`）跑硬件原生仿真**，最快的是组合非可综合加密 TB：
   ```bash
   export PYPELINEC=<PipelineC 仓库>/src/pypelinec
   cd 3.build/pypeline_build
   ./build.py --enc --sim --comb --native
   ```

**需要观察的现象**：步骤 1 的输出与官方向量逐字节相等，证明参考模型独立可信。步骤 3 的 pass 判据是「构建退出码为 0」——每个检查点都是 `sim_assert(...)`，失败会立即抛 `AssertionError`、退出码非 0，无需肉眼扫日志。

**预期结果**：步骤 1 的两个十六进制串完全匹配官方答案；步骤 3 若环境就绪则退出码 0。若本地未安装 PipelineC，步骤 1 仍可独立完成并验证参考模型的正确性，步骤 3 标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么参考模型必须用 `cryptography` 包，而不能用项目自己的 `poly1305.py`？

**参考答案**：因为「DUT 不能验证自身」。用 `poly1305.py` 算期望向量、又用 `poly1305.py` 比对，只能证明硬件自洽。C 版当年正是因为参考模型（旧版 `aead_ref_model.py`）照抄了硬件的 buggy 数学，才在「测试全过」的情况下依然不合规。只有用独立的、经广泛审计的第三方实现做期望源，才能检出硬件自身算法的偏差。

**练习 2**：把自检放在「模块导入时」而不是「测试运行中」，有什么好处？

**参考答案**：能在 elaboration（展开）阶段就暴露「`cryptography` 包装坏/版本不符」这类环境问题，报错信息明确（带断言消息），而不是让错误潜伏成测试台里一堆无线索的 `ERROR`，难以定位到底是硬件错了还是环境错了。

---

## 5. 综合实践

把三个最小模块串起来，做一次「bug 定位 → 修正 → 独立验证」的完整闭环。建议在一张表上完成：

| 项 | C 版（错） | Pypeline 版（对） | 如何被独立验证 |
| --- | --- | --- | --- |
| 64×64 limb 乘积 | `uint64_t product`，截断高半（[poly1305.h:L133](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305.h#L133)） | `uint128_t acc`，完整乘积 + `acc>>64` 进位（[poly1305.py:L136-L138](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/poly1305/poly1305.py#L136-L138)） | tag 与 `cryptography` 一致 |
| 低于 \(2^{130}\) 掩码 | `0x3FFFFFFFFFF`（42 位）（[poly1305.h:L169](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305.h#L169)） | `_MASK = 0x3`（2 位）（[poly1305.py:L59](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/poly1305/poly1305.py#L59)） | 同上 |
| 高位 limb 3/4 | 清零不折回（[poly1305.h:L177-L184](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305.h#L177-L184)） | `uint320_fold` 跨 limb 2/3/4 全折回（[poly1305.py:L154-L167](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/poly1305/poly1305.py#L154-L167)） | 同上 |
| 密文长度 | 向上取整到 16 字节 | 精确，靠逐 lane `keep`（[chacha20.py:L213-L216](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/chacha20/chacha20.py#L213-L216)）+ `keep_count`（[prep_auth_data.py:L120](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pypeline_build/src/prep_auth_data/prep_auth_data.py#L120)） | 长度字段与真实字节数相等 |

**任务**：

1. 仿照 4.2.4，逐行确认上表「C 版（错）」一栏的四处代码确实如描述。
2. 用 4.3.4 步骤 1 的 Python 命令，独立算出 RFC 8439 §2.8.2 的官方答案，作为你个人的「黄金参考」。
3. 思考题：若把 C 版的 `poly1305.h` 原样用到真实 WireGuard 隧道（对端是 Linux 内核的标准 WireGuard），握手后的数据包会发生什么？用本讲学到的「tag 是确定性算法的唯一输出」+「验证是逐位比对」两点组织你的回答。

**参考答案（思考题）**：握手阶段用的是 X25519（不涉及 poly1305，见 u6-l2），可能照常完成；但一旦进入数据传输，本端用错算的 poly1305 生成 tag，对端用标准 poly1305 重算 tag，两者不等，对端的 verify（u5-l4 的 verify-before-forward）判失败、丢包；反向同理。表现为「握手成功但数据完全不通」。这正说明「自洽但不合规」的密码实现有多危险——单元测试（用同款错算的参考模型）全绿，真网却零通。

---

## 6. 本讲小结

- **Pypeline 接线改进**：用「普通函数调用 = 例化子模块」取代「全局 Wire + 多个 `@MAIN`」；用 `Feedback[T]` 表达同一拍的反向 ready；用 `@interface` 端口（两半同名 + `_if` 后缀）和接口函数（只手写正向、反向由 pass 生成）让数据流图像正常调用图一样可读。唯一保留 `@MAIN`+Wire 的是真正被共享的 `chacha20_pipeline_shared`。
- **三个数学 bug**：① `uint320_mul` 把 64×64 乘积截断成 `uint64_t`（修为 `uint128_t` 全乘积 + `acc>>64` 进位）；② `uint320_mod_prime` 掩码写成 `0x3FFFFFFFFFF`（修为 `_MASK=0x3`，因为 \(2^{130}\) 是 limb 2 的第 2 位）；③ 直接清零 limb 3/4 不折回（修为 `uint320_fold` 跨 limb 2/3/4 全部 `×5` 折回）。bug 1 与 bug 3 互相掩盖，必须同修。
- **真实密文长度**：末 word 驱动精确 `keep`、`chacha20_loop_body` 只 XOR 保留 lane、`prep_auth_data` 用 `axis128_keep_count` 累加真实字节数，使 Poly1305 长度字段认证真实长度而非填充长度。
- **参考模型自校验**：`aead_ref_model.py` 是纯 Python、调 `cryptography` 包的独立实现，DUT 不用于验证自身；导入期跑 RFC 8439 §2.8.2 已知答案自检；它曾经照抄硬件 bug，是「DUT 验证自身」陷阱的反面教材。
- **后果**：C 版的 tag 是「自洽但不合规」的非标准 MAC，能跟自己通、不能跟任何合规 WireGuard peer 通；Pypeline 版故意与 C 版产生不同 tag/长度，换取 RFC 8439 互通性。

## 7. 下一步学习建议

- **回到 SoC 集成**：本讲（及整个 Unit 5）的加密核当前尚未编入 `top.filelist`。可重读 u4-l5 的 WG 加解密数据流，思考「当 Pypeline 版加密核上线后，它会从哪个 AXIS 端口接入 DPE、密钥从 cryptokey_table 的 B 口怎么喂进来」。
- **验证体系**：本讲的参考模型 + 已知答案测试，与 Unit 7 的协同仿真体系（VProc / rv32 ISS / PCAP 回放）一脉相承。建议接着读 u7-l1/u7-l5，看 Pypeline 自带的 native sim 与 cocotb+GHDL 如何对应到 SoC 级的 PCAP 端到端验证。
- **延伸阅读**：若对 limb 数学的正确性证明感兴趣，可自行用 Python 大整数（`int` 即任意精度）写一个独立 Poly1305，对本讲的 `uint320_mul`/`uint320_fold`/`uint320_mod_prime` 在数千组随机输入上比对——这正是 README 声称做过的验证。
