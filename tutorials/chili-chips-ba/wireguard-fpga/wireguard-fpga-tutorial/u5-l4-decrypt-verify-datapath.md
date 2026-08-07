# 解密与验证数据流

## 1. 本讲目标

本讲是 Unit 5（ChaCha20-Poly1305 加密硬件）的解密篇，承接 u5-l3（加密数据流）。学完本讲，你应当能够：

- 说清**解密 datapath** 与加密 datapath 的根本差异：为什么解密必须「先验证、后放行」（verify-before-forward）。
- 读懂 `strip_auth_tag` 如何用一拍「前瞻缓冲」（early-tlast look-ahead）把「标签在最后」的输入成帧，改写成「密文在最后」的输出成帧，并把认证标签单独剥离出来。
- 读懂 `poly1305_verify` 这台 4 状态 FSM 如何在收到两个 128 位标签后做**单周期原子比对**。
- 读懂 `wait_to_verify` 如何用一个 128 项深的 FIFO 把先于标签产生的明文「扣押」住，直到比对结果到位才决定是否放行，从而保证**未经验证的明文绝不流出芯片**。
- 把整条链路（剥离→分叉→解密/算 MAC→比对→放行）串成一个完整的心智模型。

## 2. 前置知识

本讲默认你已经掌握以下内容（若不熟请先回看对应讲义）：

- **ChaCha20-Poly1305 与 AEAD**（u5-l1）：AEAD 采用「加密—后—认证」构造。发送方先加密得到密文，再对 `AAD ‖ pad ‖ 密文 ‖ pad ‖ le64(aad_len) ‖ le64(ct_len)` 算 Poly1305 得到 16 字节 tag，把 tag 追加在密文后面发出。接收方必须**先重算 tag 并与收到的 tag 比对，通过后才解密/放行**。
- **PipelineC 工作流与 AXIS 约定**（u5-l2）：每个 C 函数被编译成一条流水线；数据用 `stream(axis128_t)` 传递，靠 `valid`/`ready` 同拍为 1 完成一次 beat 握手；`axis128_t` 含 16 字节 `tdata`、字节使能 `tkeep`、包尾标志 `tlast`。
- **加密数据流**（u5-l3）：明文→ChaCha20→密文分叉→（直通出口 + prep_auth_data→poly1305_mac）→append_auth_tag。加密时密文可以边算边流出，因为密文本身就是受保护形态；tag 在最后才追加。**解密正好相反**，这是本讲的主线。

一个关键直觉：ChaCha20 是对称流密码，**解密就是把密文再喂进同一套加密逻辑**（用相同 key/nonce 生成密钥流再异或）。所以明文几乎是「立刻、逐拍」地从 ChaCha20 出口涌出来的——比 Poly1305 把整段密文都吃进去、算完 MAC、再比 tag 要**早得多**。这个时序错位，是本讲一切设计的根源。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [decrypt_dataflow.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305/decrypt_dataflow.c) | 解密顶层连线函数，把 strip/fork/chacha20/prep_auth_data/poly1305_mac/verify/wait_to_verify 七块「摆好、接上线」。本讲的「地图」就在这里。 |
| [strip_auth_tag.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/auth_tag/strip_auth_tag.c) | 输入流「剥离器」：把密文与末尾的认证标签拆成两路，并用 early-tlast 前瞻把密文包尾提前一拍。 |
| [poly1305_verify_decrypt.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305_verify_decrypt.c) | 认证标签比对器：4 状态 FSM 收两个 128 位 tag，做单周期 `==` 比对，输出 1 比特结果。 |
| [wait_to_verify.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/auth_tag/wait_to_verify.c) | 验证闸门：128 项 FIFO 扣押明文 + 2 状态 FSM，等比对结果到位再放行。 |

辅助理解（非本讲精读对象，已在 u5-l3 讲过或作为黑盒）：

- `prep_auth_data.c`：把密文组装成 Poly1305 认证数据（解密路径复用，实例名带 `_decrypt` 后缀）。
- `poly1305_mac.c`：迭代算出 16 字节 tag（解密路径实例名 `poly1305_mac_decrypt`）。
- `chacha20.c`：生成密钥流、解密密文，并用 counter=0 的首块派生 Poly1305 一次性密钥 `poly_key`（解密路径实例名 `chacha20_decrypt`）。
- `decrypt_tb.c`：解密自检测试台，内含两包 RFC8439 风格的密文+tag 测试向量，是本讲实践的依据。

## 4. 核心概念与源码讲解

### 4.1 解密数据流全景：先验证后放行

#### 4.1.1 概念说明

加密 datapath（u5-l3）的输出是「密文 ‖ tag」，密文可以边算边送出，因为它是受保护的。**解密 datapath 的核心难题正好相反**：解密用的是同一套 ChaCha20，明文会几乎立刻、逐拍地从解密器出口冒出来；可此时 Poly1305 的 tag 还没算完——MAC 要等整段密文都吃进去才能给出结果。

如果我们像加密那样让明文「边解边流出」，那么当攻击者篡改了密文时，**错误的明文会在我们察觉 tag 不匹配之前就已经泄出芯片**。AEAD 的安全契约要求：**只有 tag 验证通过的明文才允许被下游见到**。这就叫 **verify-before-forward（先验证后放行）**。

实现这个契约的办法是：把先到的明文「扣押」在一个 FIFO 里，同时另一条路径并行地重算 tag 并比对；比对结果（1 比特）到达后，才打开 FIFO 的输出闸门。于是整条解密链天然分成两条并行支路，最终在 `wait_to_verify` 汇合：

- **数据支路**（解密）：密文 → `strip_auth_tag` → 密文分叉 → `chacha20` → `wait_to_verify`（扣押/放行）→ 明文输出。
- **验证支路**（算 MAC）：密文 → 同一个分叉 → `prep_auth_data` → `poly1305_mac` → `poly1305_verify` →（1 比特结果）→ `wait_to_verify`。
- **剥离支路**（接收到的 tag）：`strip_auth_tag` 把输入流末尾的 tag 单独抽出来 → `poly1305_verify` 的另一个输入端。

`poly1305_verify` 比较的两位操作数含义不同，务必分清：

- \(T_{recv}\)（received）：发送方追加、被 `strip_auth_tag` 从输入流末尾剥离下来的「收到的 tag」。
- \(T_{calc}\)（calculated）：本芯片用收到的密文重新算出来的「计算 tag」。

只有当 \(T_{recv} = T_{calc}\)，才说明密文未被篡改且密钥正确。

#### 4.1.2 核心流程

把 `decrypt_dataflow.c` 这一个顶层连线函数看懂，整张图就清楚了。它的执行流程（每个时钟拍都是纯组合连线，无顶层状态机）：

1. **入口接 strip**：顶层密文+tag 输入流直接喂给 `strip_auth_tag`。
2. **派生 Poly1305 密钥**：顶层 key/nonce 喂给 `chacha20`；`chacha20` 用 counter=0 首块算出 `poly_key`，喂给 `poly1305_mac`（与加密完全相同）。
3. **密文分叉**：`strip_auth_tag` 吐出的「纯密文」同时喂给两个消费者——`prep_auth_data`（验证支路）和 `chacha20`（数据支路）。源端 `ready` = 两消费者 `ready` 相与，保证「要么两边都收、要么都不动」（原子分叉）。
4. **算 MAC**：`prep_auth_data` 把密文组装成认证数据 → `poly1305_mac` 逐块迭代 → 输出计算 tag。
5. **比对**：`poly1305_verify` 收「收到的 tag」（来自 strip）与「计算 tag」（来自 poly1305_mac），比对后输出 1 比特 `tags_match`。
6. **扣押与放行**：`chacha20` 解出的明文进 `wait_to_verify` 的 FIFO；`tags_match` 作为闸门触发进 `wait_to_verify`。FIFO 在结果到达前排空闸门，到达后才放行。
7. **出口**：`wait_to_verify` 的输出即顶层明文输出，另有一根并行线 `is_verified_out` 标注本次明文是否通过验证。

#### 4.1.3 源码精读

`decrypt_dataflow.c` 用 `#pragma MAIN_MHZ decrypt_dataflow 80.0` 声明为 80 MHz 顶层，函数体全是连线：

入口直接接 strip（[decrypt_dataflow.c:11-13](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305/decrypt_dataflow.c#L11-L13)）——把顶层输入流接到剥离器，ready 反向回传。

key/nonce 与 poly_key 的连接（[decrypt_dataflow.c:19-24](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305/decrypt_dataflow.c#L19-L24)）：顶层 key/nonce → `chacha20_decrypt`；`chacha20` 产出的 `poly_key` → `poly1305_mac_decrypt`。这与加密路径一致，counter=0 专用派生 MAC 密钥。

**密文分叉**是全图的关键（[decrypt_dataflow.c:32-50](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305/decrypt_dataflow.c#L32-L50)）：

```c
// Default: no data passing
prep_auth_data_decrypt_axis_in.valid = 0;
chacha20_decrypt_axis_in.valid = 0;
// The source is ready only if both sinks are ready
strip_auth_tag_axis_out_ready = prep_auth_data_decrypt_axis_in_ready & chacha20_decrypt_axis_in_ready;
if (strip_auth_tag_axis_out.valid){
  if (strip_auth_tag_axis_out_ready | ~prep_auth_data_decrypt_axis_in_ready){
    prep_auth_data_decrypt_axis_in.valid = 1;
  }
  if (strip_auth_tag_axis_out_ready | ~chacha20_decrypt_axis_in_ready ){
    chacha20_decrypt_axis_in.valid = 1;
  }
}
```

要点：源的 `ready` 取两消费者 `ready` 的**与**；某个消费者若暂时没准备好，它那一路的 `valid` 保持挂起（下一拍源整体未 ready 时谁也不传输），从而实现「同一份密文要么同时喂给解密器和认证器、要么都不喂」的**原子双投递**——这与 u5-l3 加密的分叉是同一套写法。

比对与扣押的汇合（[decrypt_dataflow.c:62-85](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305/decrypt_dataflow.c#L62-L85)）：收到的 tag（来自 strip）与计算 tag（来自 poly1305_mac）分别接 `poly1305_verify` 的两输入；`chacha20` 的明文输出接 `wait_to_verify` 的 FIFO 写口；`poly1305_verify` 的 1 比特结果接 `wait_to_verify` 的触发口；`wait_to_verify` 的输出即顶层明文输出，`is_verified_out` 并行引出。

#### 4.1.4 代码实践

**实践目标**：在一张图上把「两条支路 + 三个汇合点」画出来，建立全局心智模型。

**操作步骤**：

1. 打开 [decrypt_dataflow.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305/decrypt_dataflow.c)。
2. 用三种颜色笔，分别描出：
   - **数据支路**：`chacha20poly1305_decrypt_axis_in` → strip → fork → `chacha20_decrypt` → `wait_to_verify` → `chacha20poly1305_decrypt_axis_out`。
   - **验证支路**：fork → `prep_auth_data_decrypt` → `poly1305_mac_decrypt` → `poly1305_verify` → `wait_to_verify_verify_bit`。
   - **剥离支路**：strip 的 `auth_tag_out` → `poly1305_verify_auth_tag`。
3. 在图上标出三个汇合点：① 密文分叉（strip 出口）；② 两个 tag 在 `poly1305_verify` 汇合；③ 明文流与验证比特在 `wait_to_verify` 汇合。

**需要观察的现象**：你会发现数据支路与验证支路**完全并行**，唯一的同步点是 `wait_to_verify`——这正是 verify-before-forward 的物化。

**预期结果**：一张清晰的「Y 型分叉 + 两路汇合」框图，左路算 MAC 慢、右路解密快，右路被 FIFO 扣住等左路。

#### 4.1.5 小练习与答案

**练习 1**：为什么不能用加密那种「边算边流出」的方式做解密？

> **参考答案**：加密流出的是密文（受保护形态），即使边算边送也无妨；解密流出的是明文（明文形态），而 tag 要等整段密文吃完才能算完。若明文边解边送，篡改导致的错误明文会在 tag 比对完成前就泄出芯片，破坏 AEAD 的认证契约。

**练习 2**：`poly1305_verify` 比较的「两个 tag」分别来自哪里？为什么必须比这两个、而不是别的？

> **参考答案**：\(T_{recv}\) 来自 `strip_auth_tag` 从输入流末尾剥离的发送方 tag；\(T_{calc}\) 来自本芯片对**收到的密文**重新算的 tag。比这两者才能同时保证「密文未被篡改」和「密钥正确」——任一条件不满足，两者就不等。

---

### 4.2 strip_auth_tag：剥离认证标签与 early-tlast 前瞻缓冲

#### 4.2.1 概念说明

`strip_auth_tag` 是加密端 `append_auth_tag` 的镜像：加密把 tag 追加在密文末尾，解密则要把末尾的 tag **剥离**下来，恢复出「纯密文」。

它面对一个棘手的成帧问题。输入流的形态是：

```
[ C0 ][ C1 ][ C2 ] ... [ Cn-1 ][ TAG ]   ← TAG 这一拍 tlast=1
```

下游的 `chacha20` 和 `poly1305_mac` 需要的是「**密文**在最后」的成帧——它们靠 `tlast` 判断密文何时结束。可是输入流里 `tlast=1` 的那一拍是 **TAG，不是最后一拍密文**。如果原样透传，下游会把 tag 当成密文的一部分，MAC 和解密全错。

所以 `strip_auth_tag` 必须做两件互相矛盾的事：

1. 把 **TAG 那一拍**（输入 `tlast=1` 的拍）**改道**到单独的 `auth_tag_out` 端口，不让它进密文流。
2. 把**最后一拍密文**（Cn-1）的 `tlast` **提前一拍置 1**——尽管它在输入流里 `tlast=0`。

第 2 点就是 **early-tlast（提前包尾）**：要给当前正在输出的那拍密文打上包尾，就必须**往前看一拍**，判断「下一拍进来的会不会是 TAG」。这就是 `strip_auth_tag.c` 里那个 `axis128_early_tlast` 辅助函数存在的理由——它是一个 1 拍深的前瞻缓冲（look-ahead buffer）。

#### 4.2.2 核心流程

`axis128_early_tlast` 的机制（一拍延迟线 + 前瞻）：

- 维护一个单拍缓冲 `buffer_reg`，存「上一拍输入」。
- 输出端呈现 `buffer_reg`（即延迟一拍的输入）。
- 每拍用 `next_axis_out_is_tlast = axis_in.valid & axis_in.data.tlast` 判断：**如果当前新输入的那拍是 TAG（tlast=1），那么此刻正从缓冲送出的那拍密文就是最后一拍密文，应被打上包尾。**
- 用这个前瞻信号**覆盖**输出 beat 原本的 `tlast` 位。

`strip_auth_tag` 主体再叠一层判断：

- 默认把（已延迟、已覆盖 tlast 的）流当作密文输出。
- 一旦发现当前输出 beat 本身就是 TAG（它的原始 `tlast=1`），就**抑制密文输出**，改把这拍数据通过 `poly1305_auth_tag_uint_from_bytes`（16 字节 → 128 位整数）转到 `auth_tag_out` 端口。

用一个三拍密文 `[C0][C1][TAG]` 的例子把时序走一遍（设下游始终 ready）：

| 拍 | 输入 axis_in | buffer_reg | 密文输出 | auth_tag 输出 | 说明 |
|---|---|---|---|---|---|
| 1 | C0 | 空→C0 | 无效 | 无效 | C0 入缓冲 |
| 2 | C1 | C0→清空 | C0（tlast=0） | 无效 | 送出 C0；前瞻看 C1 非 tag，故 tlast=0 |
| 3 | C1（未消费） | 空→C1 | 无效 | 无效 | C1 入缓冲 |
| 4 | TAG | C1→清空 | C1（tlast=1） | 无效 | 送出 C1；前瞻看 TAG 是 tag，**提前置 tlast=1** |
| 5 | TAG（未消费） | 空→TAG | 无效 | 无效 | TAG 入缓冲 |
| 6 | — | TAG | 抑制 | TAG（有效） | TAG 本身 tlast=1，改道到 auth_tag 端口 |

下游 `chacha20`/`prep_auth_data` 看到的是干净的密文 `[C0(tlast=0)][C1(tlast=1)]`；`poly1305_verify` 则从 `auth_tag_out` 拿到收到的 tag。

#### 4.2.3 源码精读

前瞻缓冲 `axis128_early_tlast`（[strip_auth_tag.c:27-66](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/auth_tag/strip_auth_tag.c#L27-L66)）核心几行：

```c
static stream(axis128_t) buffer_reg;            // 单拍延迟线
...
uint1_t buffer_is_tlast = buffer_reg.valid & buffer_reg.data.tlast;
uint1_t buff_to_out_connected =
    buffer_is_tlast |          // 缓冲里就是最后一拍（如 tag），即使没有新输入也要送出
    axis_in.valid;             // 或者有新输入进来，可以前瞻
if(buff_to_out_connected){
    o.axis_out = buffer_reg;
    o.next_axis_out_is_tlast = axis_in.valid & axis_in.data.tlast; // 前瞻：下一拍是不是 tag？
}
...
o.ready_for_axis_in = ~buffer_reg.valid;        // 缓冲空才能收新输入
if(axis_in.valid & o.ready_for_axis_in){
    buffer_reg = axis_in;                        // 新输入入缓冲
}
```

`buffer_is_tlast` 这条支路很关键：当缓冲里存的就是最后一拍（典型即 TAG）且后面没有新输入时，`axis_in.valid` 为 0，正常支路送不出数据；靠 `buffer_is_tlast` 才能让这拍「挤」出缓冲，交给主体去改道。

主体 `strip_auth_tag`（[strip_auth_tag.c:70-105](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/auth_tag/strip_auth_tag.c#L70-L105)）：

```c
// 默认把（已前瞻的）流当密文输出
strip_auth_tag_axis_out = axis_in;
ready_for_axis_in = strip_auth_tag_axis_out_ready;
// 用前瞻信号覆盖密文包尾
strip_auth_tag_axis_out.data.tlast = early_tlast.next_axis_out_is_tlast;
// 默认不往 auth_tag 端口送
stream(poly1305_auth_tag_uint_t) auth_tag_null = {0};
strip_auth_tag_auth_tag_out = auth_tag_null;

// 若当前输出拍本身就是 tag（原始 tlast=1），则改道
if (axis_in.valid & axis_in.data.tlast)
{
   strip_auth_tag_axis_out.valid = 0;                       // 抑制密文输出
   strip_auth_tag_auth_tag_out.data = poly1305_auth_tag_uint_from_bytes(axis_in.data.tdata);
   strip_auth_tag_auth_tag_out.valid = axis_in.valid;       // 改道到 auth_tag 端口
   ready_for_axis_in = strip_auth_tag_auth_tag_out_ready;
}
```

注意 `poly1305_auth_tag_uint_from_bytes` 在 [poly1305.h:15](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305.h#L15) 被定义为 `uint8_array16_le`，即把 16 字节小端拼成一个 `uint128_t`——与认证标签的 128 位宽度对齐。

#### 4.2.4 代码实践

**实践目标**：亲手走一遍 early-tlast 时序，理解「提前一拍打包尾」。

**操作步骤**：

1. 打开 [strip_auth_tag.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/auth_tag/strip_auth_tag.c)。
2. 假设输入是一个**单拍密文 + 单拍 tag** 的最小包 `[C0][TAG(tlast=1)]`，仿照 4.2.2 的表格，逐拍填出 `buffer_reg`、密文输出、auth_tag 输出。
3. 回答：C0 在第几拍被送出？它的 `tlast` 被覆盖成了什么值？TAG 在第几拍改道？

**需要观察的现象**：即便密文只有一拍，C0 仍会被打上 `tlast=1`（因为下一拍就是 tag），TAG 则被改道而非进入密文流。

**预期结果**：C0 的 tlast 被前瞻信号置 1；TAG 不进密文流而进 auth_tag 端口。**待本地验证**：若跑 `./build_sim_pipe_dec.sh` 仿真，可在波形上确认密文支路的 `tlast` 比输入流提前一拍。

#### 4.2.5 小练习与答案

**练习 1**：如果没有 early-tlast 前瞻（直接把输入 tlast 透传给密文流），会出什么问题？

> **参考答案**：下游 `chacha20`/`poly1305_mac` 会在 TAG 那拍才看到 `tlast=1`，于是把 TAG 当成最后一拍密文去解密/算 MAC，密文成帧错位，解密结果与 MAC 全错；而且真正的最后一拍密文 Cn-1 不会被标记包尾。

**练习 2**：`axis128_early_tlast` 的 `buffer_reg` 引入了多少额外延迟？为什么这个延迟是必要的？

> **参考答案**：引入恰好 1 拍延迟。必要是因为「要给当前输出拍打上正确的包尾，必须知道下一拍是不是 tag」，而「下一拍」只有在把当前拍先存进缓冲、腾出位置看下一拍时才能看到——这就是 look-ahead 的代价。

**练习 3**：`buffer_is_tlast` 这条支路什么时候起作用？去掉它会怎样？

> **参考答案**：当缓冲里存的就是最后一拍（如 TAG）且后面没有新输入（`axis_in.valid=0`）时起作用，让这拍能送出缓冲。去掉它，TAG（或任何末拍）会永远卡在缓冲里送不出来，流停滞。

---

### 4.3 poly1305_verify：128 位原子比对状态机

#### 4.3.1 概念说明

`poly1305_verify` 的任务看似简单：比较两个 128 位 tag 是否相等，输出 1 比特。但它的输入是**两个独立的流握手**——收到的 tag（来自 strip，到得早）和计算 tag（来自 poly1305_mac，到得晚），两者到达时机不同。因此不能写成一个纯组合比较，而要写成一台小 FSM：先把先到的那个 tag 锁存进寄存器，再等后到的那个，最后比、再输出。

「**128 位原子比对**」指的是比较本身用 PipelineC 内建的 `uint128_t == uint128_t` 在**单个时钟周期内组合完成**，对全部 128 位一次性判定，而不是软件里常见的逐字节循环。单周期、全位宽的等值比较天然是常时间的，避免了「比较到第几字节才不等」这类时序侧信道。

#### 4.3.2 核心流程

4 状态 FSM：

1. **TAKE_AUTH_TAG**：拉高 `auth_tag_ready`，等收到的 tag 到达，存入 `auth_tag_reg`，转下一态。
2. **TAKE_CALC_TAG**：拉高 `calc_tag_ready`，等计算 tag 到达，存入 `calc_tag_reg`，转下一态。
3. **COMPARE_TAGS**：`tags_match_reg = (auth_tag_reg == calc_tag_reg);` 组合比对，转下一态。
4. **OUTPUT_COMPARE_RESULT**：把 `tags_match_reg` 经 1 比特流送出；被下游（`wait_to_verify`）收走后，回 TAKE_AUTH_TAG 处理下一个包。

两个 tag 谁先到不影响正确性——FSM 各自独立捕获。实际上由于计算 tag 要等整段密文算完 MAC，**收到的 tag 几乎总是先到**，所以常态是 TAKE_AUTH_TAG → TAKE_CALC_TAG 的顺序。

#### 4.3.3 源码精读

状态枚举与寄存器（[poly1305_verify_decrypt.c:25-43](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305_verify_decrypt.c#L25-L43)）：四个状态 `TAKE_AUTH_TAG / TAKE_CALC_TAG / COMPARE_TAGS / OUTPUT_COMPARE_RESULT`，三个静态寄存器 `auth_tag_reg`、`calc_tag_reg`、`tags_match_reg`。

捕获与比对（[poly1305_verify_decrypt.c:51-81](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305_verify_decrypt.c#L51-L81)）：

```c
if (state == TAKE_AUTH_TAG) {
    poly1305_verify_auth_tag_ready = 1;
    if (poly1305_verify_auth_tag.valid & poly1305_verify_auth_tag_ready) {
        auth_tag_reg = poly1305_verify_auth_tag.data;   // 锁存收到的 tag
        state = TAKE_CALC_TAG;
    }
}
else if (state == TAKE_CALC_TAG) {
    poly1305_verify_calc_tag_ready = 1;
    if (poly1305_verify_calc_tag.valid & poly1305_verify_calc_tag_ready) {
        calc_tag_reg = poly1305_verify_calc_tag.data;   // 锁存计算 tag
        state = COMPARE_TAGS;
    }
}
else if (state == COMPARE_TAGS) {
    tags_match_reg = (auth_tag_reg == calc_tag_reg);    // 单周期 128 位原子比对
    state = OUTPUT_COMPARE_RESULT;
}
```

结果输出（[poly1305_verify_decrypt.c:82-94](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305_verify_decrypt.c#L82-L94)）：把 `tags_match_reg` 经 1 比特流送出，下游收走后回 TAKE_AUTH_TAG。

端口连线在顶层（[decrypt_dataflow.c:64-68](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305/decrypt_dataflow.c#L64-L68)）：`auth_tag` 端接 strip 剥离的「收到的 tag」，`calc_tag` 端接 poly1305_mac 算出的「计算 tag」。

#### 4.3.4 代码实践

**实践目标**：确认「先到的 tag 被锁存、后到的 tag 到齐后才比对」的时序鲁棒性。

**操作步骤**：

1. 打开 [poly1305_verify_decrypt.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305_verify_decrypt.c)。
2. 假设「计算 tag」比「收到的 tag」**先**到（与常态相反），手动走一遍 FSM，看结果是否仍正确。
3. 数一数：从两个 tag 都到齐，到 `tags_match` 比特送出，最少经历几拍？

**需要观察的现象**：无论两个 tag 谁先到，FSM 都能各自独立捕获，比对结果不受到达顺序影响。

**预期结果**：两 tag 到齐后，COMPARE_TAGS 1 拍 + OUTPUT_COMPARE_RESULT 至少 1 拍 = 结果至少 2 拍后送出。**待本地验证**：跑解密仿真观察 `poly1305_verify_tags_match` 的拉高时刻。

#### 4.3.5 小练习与答案

**练习 1**：为什么用 `uint128_t == uint128_t` 而不是逐字节比较？

> **参考答案**：单周期全位宽等值比较是原子的、常时间的，既省状态又避免「比较到第几字节不等」的时序侧信道；逐字节比较会引入多拍且耗时与不匹配位置相关。

**练习 2**：若两个 tag 永远不到达（上游挂了），FSM 会怎样？

> **参考答案**：FSM 会停在 TAKE_AUTH_TAG（或 TAKE_CALC_TAG）死等，`ready` 一直拉高但 `valid` 不来，`tags_match` 永不输出。本模块自身没有超时，依赖上游保证最终送达。

---

### 4.4 wait_to_verify：128 字 FIFO 与验证闸门

#### 4.4.1 概念说明

`wait_to_verify` 是 verify-before-forward 的**物化核心**。它做两件事：

1. **扣押明文**：把 `chacha20` 解出、但 tag 还没验的明文，逐拍写进一个 128 项深的 FIFO；在验证结果到来之前，**FIFO 的读口关死**，明文不许流出。
2. **据结果放行**：等 `poly1305_verify` 的 1 比特结果到位后，才打开读口放行；同时用并行线 `is_verified_out` 标注本次明文是否通过验证，供下游决定接受还是丢弃。

安全含义有两层：

- **不提前泄漏**：结果到位前的整段时间里，`axis_out.valid = 0`，芯片对外「沉默」——攻击者篡改的密文所产生的错误明文，在比对完成前**一个字节都出不去**。
- **失败即标注丢弃**：若 `tags_match=0`，明文虽然从 FIFO 排出（FIFO 必须腾空才能处理下一包），但带着 `is_verified_out=0` 的标记；下游（在完整 SoC 中是 DPE 的 WG 解封/装配级）据此丢弃，错误明文**不会进入受信网络**。换言之，`wait_to_verify` 自身负责「扣押 + 标注」，真正的「丢弃」是与下游协作完成的系统级效果。

#### 4.4.2 核心流程

2 状态 FSM（[wait_to_verify.c:22-25](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/auth_tag/wait_to_verify.c#L22-L25)）：

- **WAIT_TO_VERIFY_BIT**：
  - FIFO 写口正常接 `chacha20` 明文（明文持续入队）。
  - FIFO 读口关（`verify_fifo_out_ready = 0`），输出 `valid = 0`——明文被扣押。
  - 拉高 `verify_bit_ready`，等比对结果比特到来。
  - 比特一到，锁存进 `tags_match_reg`，转 OUTPUT_PLAINTEXT。
- **OUTPUT_PLAINTEXT**：
  - 打开 FIFO 读口（`verify_fifo_out_ready = wait_to_verify_axis_out_ready`），明文排出。
  - `is_verified_out = tags_match_reg` 随流送出。
  - 排到 `tlast`（缓冲的最后一拍）被消费，回 WAIT_TO_VERIFY_BIT 处理下一包。

FIFO 声明（[wait_to_verify.c:28](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/auth_tag/wait_to_verify.c#L28)）：`GLOBAL_STREAM_FIFO(axis128_t, verify_fifo, 128)`——深度 128 项，每项一个 `axis128_t`（16 字节），即最多扣押 128 × 16 = **2048 字节明文**。

**容量即包长上限**（重要设计约束）：由于比对结果只有在整段密文的 `tlast` 被 `poly1305_mac` 吃到后才会产生，FIFO 必须**装下整包明文**。若一包明文超过 128 拍（2048 字节），FIFO 会写满；写满后 `chacha20` 被反压，进而通过密文分叉的「源 ready = 两消费者 ready 相与」把 `prep_auth_data` 也卡住——MAC 永远吃不到 `tlast`、永不算完、比对比特永不到来、FIFO 永不排空，形成**死锁**。因此该核隐含「单包明文 ≤ 2048 字节」的上限（测试台用的明文仅 64、80 字节，远低于上限）。

#### 4.4.3 源码精读

WAIT_TO_VERIFY_BIT 状态（[wait_to_verify.c:59-79](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/auth_tag/wait_to_verify.c#L59-L79)）：

```c
if(state == WAIT_TO_VERIFY_BIT) {
    verify_fifo_out_ready = 0;                 // 读口关，明文扣押
    wait_to_verify_axis_out.valid = 0;         // 对外沉默
    wait_to_verify_verify_bit_ready = 1;       // 等比对结果
    if(wait_to_verify_verify_bit.valid) {
        tags_match_reg = wait_to_verify_verify_bit.data;  // 锁存结果
        state = OUTPUT_PLAINTEXT;
    }
}
```

OUTPUT_PLAINTEXT 状态（[wait_to_verify.c:81-105](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/auth_tag/wait_to_verify.c#L81-L105)）：

```c
else { // OUTPUT_PLAINTEXT
    verify_fifo_out_ready = wait_to_verify_axis_out_ready;   // 读口随下游 ready 开放
    wait_to_verify_axis_out.valid = verify_fifo_out.valid;   // 明文排出
    wait_to_verify_is_verified_out = tags_match_reg;         // 验证结果随流送出
    if(wait_to_verify_axis_out.valid && wait_to_verify_axis_out_ready) {
        if (wait_to_verify_axis_out.data.tlast) {
            state = WAIT_TO_VERIFY_BIT;                      // 整包排完，回等待态
        }
    }
    wait_to_verify_verify_bit_ready = 0;
}
```

注意：OUTPUT_PLAINTEXT 里 FIFO 的排出**不取决于 `tags_match_reg`**——无论验证成败，缓冲的明文都会排空（FIFO 必须腾空才能接下一包）；成败只体现在并行线 `is_verified_out` 上，由下游决定去留。这是对本模块最忠实的解读。

写口与顶层连线（[wait_to_verify.c:40-43](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/auth_tag/wait_to_verify.c#L40-L43) 与 [decrypt_dataflow.c:72-85](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305/decrypt_dataflow.c#L72-L85)）：明文写口接 `chacha20` 输出，触发口接 `poly1305_verify` 的 `tags_match`，输出口接顶层明文输出。

#### 4.4.4 代码实践

**实践目标**：解释「明文为何先于 tag 产生」以及「wait_to_verify 如何防止未验证明文流出芯片」——这是本讲的核心问题。

**操作步骤**：

1. 打开 [wait_to_verify.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/auth_tag/wait_to_verify.c) 与 [decrypt_dataflow.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305/decrypt_dataflow.c)。
2. 用文字回答两个问题：
   - **(a)** 为什么明文会比 tag 结果先到 `wait_to_verify`？（提示：ChaCha20 是流密码逐拍出明文；Poly1305 要等整段密文吃完才出 tag。）
   - **(b)** 在结果到来之前，`wait_to_verify` 的 `axis_out.valid` 是什么？结果到来后若 `tags_match=0`，`is_verified_out` 是什么？下游该怎么做？
3. **（进阶分析）** 假设攻击者把某拍密文改了一个字节，请描述：明文会变成什么？tag 还会匹配吗？最终芯片对外表现如何？

**需要观察的现象**：

- 结果到来前，输出始终 `valid=0`，外部看不到任何明文。
- 结果到来后，明文排出，`is_verified_out` 与 `tags_match` 一致。

**预期结果**：

- (a) ChaCha20 解密是流式的，密文进一拍出明文一拍；Poly1305 的 tag 必须等密文 `tlast` 被消费后才算完，所以明文远早于 tag 结果。
- (b) 结果到来前 `axis_out.valid=0`（扣押）；若 `tags_match=0`，`is_verified_out=0`，下游应丢弃这包（错误明文不进受信网络）。
- (c) 改一字节 → 对应拍明文变成错误明文（密钥流异或被破坏）；计算 tag 偏离收到的 tag → 不匹配 → `is_verified_out=0` → 下游丢弃。芯片对外「不泄漏有意义的错误明文」。

**待本地验证**：跑 `./build_sim_pipe_dec.sh`（解密流水线测试台，约 175 步），观察波形中 `chacha20poly1305_decrypt_axis_out.valid` 在 `is_verified_out` 拉高之前是否为 0。

#### 4.4.5 小练习与答案

**练习 1**：FIFO 深度为什么是 128 项？它隐含了什么限制？

> **参考答案**：要扣押整包明文直到 tag 验完，故 FIFO 须装得下一整包。128 项 × 16 字节 = 2048 字节，隐含「单包明文 ≤ 2048 字节」的上限；超出会让 FIFO 写满、反压传播回 `prep_auth_data`，MAC 永远吃不到 `tlast`，导致死锁。

**练习 2**：验证失败（`tags_match=0`）时，`wait_to_verify` 自己有没有「删掉」明文字节？真正的丢弃发生在哪里？

> **参考答案**：没有。本模块无论成败都把 FIFO 排空，只是把 `is_verified_out` 置 0 随流送出。真正的丢弃是下游（DPE 的 WG 解封/装配级）根据 `is_verified_out=0` 决定不转发。`wait_to_verify` 负责「扣押 + 标注」，丢弃是与下游协作的系统级效果。

**练习 3**：如果去掉 WAIT_TO_VERIFY_BIT 状态、让明文直通输出，会破坏哪条安全性质？

> **参考答案**：会破坏 verify-before-forward——错误明文会在 tag 比对完成前就流出芯片，攻击者篡改的密文所产生的明文会泄漏到受信网络，AEAD 的认证契约失效。

## 5. 综合实践

**任务**：把本讲四个模块串成一条端到端的「解密一次受篡改的包」推演，并用测试台向量验证正常路径。

**步骤**：

1. **读测试台**：打开 [decrypt_tb.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305/decrypt_tb.c)。注意它的输入是「密文 ‖ tag」拼成一条流（见 `input_ciphertext0`，前 64 字节密文 + 后 16 字节 tag），`tlast` 打在最后一拍（即 tag 那拍）——这正是 `strip_auth_tag` 要处理的成帧。
2. **追踪正常包**：以 `input_ciphertext0` 为例，口述一拍拍流过：strip 剥离 tag 并给末拍密文提前打 tlast → 密文分叉 → 一路 chacha20 出明文进 FIFO 扣押、一路 prep_auth_data→poly1305_mac 出计算 tag → 与剥离的收到 tag 在 poly1305_verify 比对 → 匹配 → wait_to_verify 放行 → 输出预期明文 `"Hello CHILIChips ..."` 且 `is_verified_out=1`。
3. **手造篡改**：把 `input_ciphertext0` 的某个密文字节（如第一个 `0xd7`）改成 `0x00`，推演：strip 不受影响（成帧没变）→ chacha20 出的明文首字节错 → 计算 tag 偏离 → poly1305_verify 比对不等 → wait_to_verify 放行时 `is_verified_out=0` → 下游丢弃。
4. **（可选，待本地验证）** 跑解密仿真：设置 `$PIPELINEC` 环境变量后执行 `./build_sim_pipe_dec.sh`，观察正常包是否输出预期明文且 `is_verified_out=1`。

**预期结果**：你能用一句话讲清「正常包放行、篡改包标注丢弃」的完整因果链，并把链路上每一段对应到 `strip_auth_tag / 分叉 / chacha20 / prep_auth_data / poly1305_mac / poly1305_verify / wait_to_verify` 中的具体代码。

## 6. 本讲小结

- 解密与加密的根本差异在于 **verify-before-forward**：明文会先于 tag 结果产生，必须先验证后放行。
- 整条解密链是「密文+tag → strip_auth_tag → 密文分叉 →（chacha20 解密 + prep_auth_data→poly1305_mac 算 tag）→ poly1305_verify 比对 → wait_to_verify 放行」两条并行支路在 `wait_to_verify` 汇合。
- `strip_auth_tag` 用一拍 **early-tlast 前瞻缓冲**把「tag 在最后」改写成「密文在最后」，并把 tag 改道到单独端口。
- `poly1305_verify` 是 4 状态 FSM，用 `uint128_t ==` 做**单周期 128 位原子比对**，独立捕获先到/后到的两个 tag。
- `wait_to_verify` 用 128 项 FIFO **扣押明文**，结果到来前 `valid=0` 不泄漏；结果到来后随流送 `is_verified_out`，失败由下游丢弃——这隐含单包 ≤ 2048 字节的容量上限，超限会死锁。
- `poly1305_verify` 比的是「收到的 tag」与「用收到的密文重算的 tag」，相等才意味着未篡改且密钥正确。

## 7. 下一步学习建议

- **u5-l5（资源共享：共享 ChaCha20/Poly1305 流水线）**：本讲的解密 datapath 与 u5-l3 的加密 datapath 各自独占一条 ChaCha20 流水线，面积大。下一讲讲如何用 1 比特 `is_encrypt` 标签穿越 64 级流水线，让加解密**共享**一条昂贵的 ChaCha20——届时你会重新审视本讲的分叉与放行如何在共享调度下保持正确。
- **回看 u5-l6（Pypeline Python 前端与 RFC 修正）**：本讲依赖的 Poly1305 limb 数学（`uint320_mul`/`uint320_mod_prime`）在 C 版有已知 bug；若你关心「为什么算出来的 tag 与合规 peer 对不上」，那是 u5-l6 的主题。
- **延伸阅读源码**：若想确认「下游如何根据 `is_verified_out` 丢弃」，可对照 u4-l5 讲的 `dpe_wg_decryptor`——那里的 verify-before-forward 机制（验证门 + sync_fifo）与本讲 `wait_to_verify` 是同一思想在 DPE 层的落地。
