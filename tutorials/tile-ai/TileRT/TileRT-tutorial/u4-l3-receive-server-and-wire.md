# 接收服务与控制平面协议（hello 握手与 wire 消息）

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 PD（prefill-decode）分离里 **接收侧** 的三件事：怎么分配接收缓冲、怎么和发送端协商布局、怎么知道「数据到齐了」。
- 读懂 `receive_server.py` 的 `ReceiveServer` 类，理解它如何同时承担「RDMA 接收缓冲的宿主」「TCP 控制平面服务端」「单槽位（bs=1）请求状态机」三种角色。
- 读懂 `wire.py` 的控制平面协议：长度前缀 JSON 帧、`hello_msg` 信封、`magic / layout_version / transport / max_seq_len` 四项校验分别防止什么故障。
- 理解 `ReceivedRequest` 用 `done_ranks` 累积「完成标志」直到所有 `sender_ranks` 齐了，再交给下游 `convert → inject → decode`。
- 写出接收端「靠地址预约把多个 rank 的 RDMA 写入拼成一份完整请求」的伪代码。

本讲只讲 **接收侧 + 控制平面**。发送端的 KV 抽取与发送链路已在 u4-l2 讲过，传输层（mooncake/nixl）与缓存注入留到 u4-l5、u4-l6。

## 2. 前置知识

### 2.1 控制平面 vs 数据平面

PD 分离把一次推理切成两段，跨进程跨节点：

- **数据平面（data plane）**：搬运 KV 缓存本身的「大块字节流」，走 RDMA，追求带宽（一次几十到几百 MB）。
- **控制平面（control plane）**：交换「谁要发、发给谁、发多大、是否到齐」这类**小消息**，走普通 TCP，追求简单可靠。

本讲的主角 `wire.py` + `receive_server.py` 的 TCP 部分，就是**控制平面**。它只传小 JSON，不传 KV 字节；KV 字节由 RDMA 直接写进显存。这种「控制走 TCP、数据走 RDMA」的双轨设计，是高性能 KV 迁移系统的常见骨架。

### 2.2 长度前缀帧（length-prefixed framing）

TCP 是**字节流**，没有消息边界。如果你直接 `sock.send(json)` 连发两条，接收端 `recv` 可能一次拿到「一条半」或「两条粘一起」。解决办法是**先发 4 字节长度，再发正文**：

```
[ 4字节大端长度 N ][ N 字节 JSON 正文 ]
```

接收端先精确读 4 字节拿到 N，再精确读 N 字节，就一定能切出完整一条消息。`wire.py` 的 `send_msg/recv_msg` 用的就是这个套路。

### 2.3 RDMA 与「地址预约」

RDMA（Remote Direct Memory Access）能让一台机器的 GPU **直接写**另一台机器的 GPU 显存，绕过双方 CPU。但它有一个硬要求：**目标地址必须事先注册并告诉发送端**。发送端不能瞎写，必须知道「写到哪个 GPU 地址、写多少字节」。

所以接收端的工作模式是：

1. 先在自己的 GPU 上分配一块**固定布局**的缓冲区；
2. 把这块缓冲的「分区起始地址」通过控制平面告诉发送端；
3. 发送端用 RDMA 把数据**直接写到对应地址**；
4. 接收端**根本不搬运数据**——数据到齐时，缓冲里已经是正确布局了。

这就是本讲最核心的直觉：**接收端靠「预约地址」把多段写入拼成完整请求，而不是靠拷贝拼接**。

### 2.4 承接前置讲义

- u4-l1 给出了 PD 的三进程拓扑（router / vLLM prefill / decode_server）和 `ModelProfile` 抽象。本讲深入 decode 节点内部那条「等 wire → convert → inject → decode」链路的**第一段**：wire 怎么「等」。
- u4-l2 讲了发送端 `TileRTConnector`：它怎么认领请求、累积 chunked prefill、抽 KV、走 TCP 握手 + RDMA 发送。本讲是它的**对端**——发出来的 hello 和 req，正是本讲要收的。
- 一个关键结论会反复用到：**MLA 潜在 KV 在张量并行间是复制的**，所以 `sender_ranks = frozenset({0})`，只有 rank 0 真正发数据（u4-l2 已说明）。这会让「多 rank 汇聚」在本项目里退化成「等 rank 0 一个人」，但**机制本身是为多 rank 写的**，我们既讲机制也讲这个退化点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tilert/pd_vllm/receive_server.py` | 接收服务：分配缓冲、跑 TCP 控制平面、维护单槽位请求状态机 `ReceivedRequest`。本讲主角。 |
| `tilert/pd_vllm/wire.py` | 控制平面协议原语：`MAGIC`、长度前缀帧 `send_msg/recv_msg`、`hello_msg` 信封、`derive_rid` 请求 id 归一化。 |
| `tilert/pd_vllm/profiles/base.py` | `ModelProfile` 协议：定义 `buffer_bytes / hello_layout / layout_version / sender_ranks` 等「接收侧」接口签名，框架只认这些方法。 |
| `tilert/pd_vllm/profiles/mla_nsa.py` | GLM-5 / DeepSeek-V3.2 共用的具体 profile 实现：真正算出缓冲字节数、算出 kv/pe/ki 三平面地址、给出 `layout_version` 和 `sender_ranks={0}`。 |
| `tilert/pd_vllm/profiles/glm5.py` | GLM-5 profile 的薄配置层：`LAYOUT_VERSION = 10`、`NUM_LAYERS = 79`。 |
| `tilert/pd_vllm/prefill_connector.py` | 发送端对照（u4-l2 已详讲）：本讲只引用它的 `_send` 来印证「校验是在发送端读 hello 时做的」。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **接收缓冲与 hello 布局**——接收端怎么分配显存、怎么算分区地址。
2. **TCP 控制平面协议与校验**——长度前缀帧 + hello 握手 + 四项校验。
3. **多 rank 汇聚**——`ReceivedRequest` 的 `done_ranks` 状态机。

---

### 4.1 接收缓冲与 hello 布局

#### 4.1.1 概念说明

接收端要回答两个问题：

- **要预留多大显存？** 一次请求最多携带 `max_seq_len` 个 token 的 KV 缓存，得按这个上限预留，否则来个长 prompt 就溢出。
- **这块显存内部怎么分区？** KV 缓存不是一整块「裸字节」，而是分若干层、每层又有 `kv / pe / ki` 三种张量。发送端必须知道每种张量写到哪个地址。

`ModelProfile` 协议把这两件事收口到两个方法：`buffer_bytes(max_seq_len)` 算总字节，`hello_layout(base_ptr, max_seq_len)` 给出分区起始地址。框架（`ReceiveServer`）只调用这两个方法，**完全不知道 kv/pe/ki 是什么**——这是 u4-l1 讲的「模型相关逻辑收口到 profile、框架模型无关」的具体落地。

#### 4.1.2 核心流程

`ReceiveServer` 在构造时一次性完成缓冲分配与注册：

```text
profile.buffer_bytes(max_seq_len)            # 1. 算总字节 total
torch.zeros(total, uint8, cuda:0)            # 2. 在 GPU 分配一块连续缓冲
buffer.data_ptr()                            # 3. 拿到 GPU 起始地址 base_ptr
profile.hello_layout(base_ptr, max_seq_len)  # 4. 算出 kv_base / pe_base / ki_base
transport.register(base_ptr, total, dev_id)  # 5. 把这块显存注册给 RDMA 引擎
transport.local_meta()                       # 6. 生成「怎么连我」的名片（session_id 等）
```

对于 GLM-5 / DeepSeek-V3.2 这类 MLA+NSA 模型，缓冲总量是三层平面之和。设层数为 \(L\)、最大序列长为 \(S\)、页大小 \(P=64\)，则：

\[
\text{buffer\_bytes} = \underbrace{L \cdot S \cdot b_{kv}}_{\text{kv 平面}} + \underbrace{L \cdot S \cdot b_{pe}}_{\text{pe 平面}} + \underbrace{L \cdot \lceil S/P \rceil \cdot b_{ki}}_{\text{ki 平面}}
\]

其中 \(b_{kv}\) 取决于 MLA 缓存 dtype（fp8 时 528 字节/token，bf16 时 1024 字节/token），\(b_{pe}=128\)，\(b_{ki}\) 按页算。注意 **kv 平面与 pe 平面按 token 数 \(S\) 计量，而 ki 平面按页数 \(\lceil S/P\rceil\) 计量**——因为 NSA 索引是按页存的。

#### 4.1.3 源码精读

**`ReceiveServer.__init__` 分配缓冲并算布局**：

[tilert/pd_vllm/receive_server.py:47-56](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/receive_server.py#L47-L56)

这段先问 profile 要总字节 `total`，在 `device`（默认 `cuda:0`）上开一块 `uint8` 缓冲，记下起始地址 `base_ptr`，再调 `profile.hello_layout` 得到分区布局。注意缓冲是 `uint8` 视角的「裸字节」——它不区分 dtype，dtype 语义在下游 `convert` 里按偏移重新 view 出来（见 u4-l2 / u4-l6）。

**把缓冲注册给 RDMA 引擎并拿名片**：

[tilert/pd_vllm/receive_server.py:58-67](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/receive_server.py#L58-L67)

`make_transport(transport)` 按 `--transport` 选 mooncake 或 nixl；`init(hostname)` 初始化引擎；`register(base_ptr, total, dev_id)` 把这块显存登记成可被远端 RDMA 写入的区域；`local_meta()` 生成对端连接所需的名片（mooncake 是 `session_id`，nixl 是 base64 的 agent 元数据 + 设备号）。这张名片随后会塞进 hello 发给发送端。

**profile 协议层的接口签名**（框架只认这两个）：

[tilert/pd_vllm/profiles/base.py:25-32](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/base.py#L25-L32)

`hello_layout` 的 docstring 写得很直白：「告诉发送端每个 section 该 RDMA 写到哪」。这就是「地址预约」在协议层的体现。

**具体实现：MLA+NSA 的三平面布局**：

[tilert/pd_vllm/profiles/mla_nsa.py:138-147](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L138-L147)

`buffer_bytes` 把 kv/pe/ki 三个平面相加；`hello_layout` 在 `base_ptr` 之上顺序排布：`kv_base = base_ptr`，`pe_base = kv_base + kv 平面大小`，`ki_base = pe_base + pe 平面大小`。三个地址随 hello 发出去后，发送端的 `rdma_plan` 就能算出每段数据该写到哪个偏移。

> 小贴士：`max_seq_len` 是「预留窗口」，不是「本次实际长度」。本次请求实际只写 `seq_len` 个 token（在请求消息里告知），但缓冲为 `max_seq_len` 预留了空间。下游 `convert` 只切前 `seq_len` 个 token 的有效区域。

#### 4.1.4 代码实践

**实践目标**：亲手验证「缓冲大小随 max_seq_len 与 cache dtype 变化」，建立对布局公式的直觉。

**操作步骤**（源码阅读型，无需真实 GPU）：

1. 打开 [mla_nsa.py 的常量区](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L15-L41)，记录 `KV_LORA_RANK=512`、`QK_ROPE_HEAD_DIM=64`、`INDEX_HEAD_DIM=128`、`PAGE_SIZE=64`、`KV_BYTES_FP8=528`、`PE_BPT=128`、`KI_PAGE_BYTES=8448`。
2. 取 GLM-5 的 `NUM_LAYERS=79`、`max_seq_len=202752`（README 示例值），手算 fp8 模式下三平面大小：
   - kv 平面 = `79 × 202752 × 528` ≈ 8.46 GB
   - pe 平面 = `79 × 202752 × 128` ≈ 2.05 GB
   - ki 平面 = `79 × ceil(202752/64) × 8448` ≈ 2.12 GB
3. 对照 [buffer_bytes](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L138-L141) 的实现确认三者相加。

**需要观察的现象**：总缓冲约 12.6 GB（fp8）；若改 bf16（`KV_BYTES_BF16=1024`），kv 平面几乎翻倍，总量会明显增大——这正是 `layout_version` 要区分 cache dtype 的原因（见 4.2）。

**预期结果**：手算与代码逻辑一致。**待本地验证**：在有 GPU 的环境里跑 `decode_server`，看启动日志 `allocating receive buffer: X.XX GB` 是否与手算吻合。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `hello_layout` 要把 `base_ptr` 也带进返回值，而不是只返回「相对偏移」？
**参考答案**：因为发送端的 `rdma_plan` 需要的是**绝对 GPU 地址**作为 RDMA 写入的目的地址（`dsts`），不是偏移量。RDMA 引擎直接按绝对地址写对端显存。`base_ptr` 是缓冲在接收端 GPU 上的真实起始地址，必须原样传给发送端。

**练习 2**：ki 平面为什么用「页数」而不是「token 数」来计量？
**参考答案**：NSA 的 KI 索引（index key）在 vLLM 里是按页（page）存储的（`PAGE_SIZE=64`），一页 64 个 token 共享一块索引数据，大小为 `KI_PAGE_BYTES=8448`。所以平面大小按页数 `ceil(seq_len/PAGE_SIZE)` 算，而不是按 token 数。

---

### 4.2 TCP 控制平面协议与校验

#### 4.2.1 概念说明

控制平面要解决三件事：

1. **可靠成帧**：TCP 字节流要切出一条条独立消息。
2. **握手校验**：两端必须在**真正发数据之前**确认彼此兼容——magic（是不是 TileRT PD 协议）、layout_version（KV 布局是否同款）、transport（RDMA 引擎是否同款）、max_seq_len（发送端的长度会不会撑爆接收端缓冲）。
3. **请求 id 对齐**：发送端（vLLM 内部 id）和路由层（OpenAI 响应 id）要用同一个 `rid` 才能对上账。

这些原语全部住在 `wire.py`，是发送端和接收端**共用**的。任何一端改了帧格式或 hello 字段，另一端必须同步改——这就是把它独立成 `wire.py` 的意义。

#### 4.2.2 核心流程

一次完整的控制平面交互（从接收端视角）：

```text
[发送端]                              [接收端 ReceiveServer._handle]
                                          |
   ---- TCP connect ---->                 |
                                          |
   <---- hello_msg (JSON 帧) -----        |  发送 magic/layout_version/transport/
                                          |         max_seq_len/busy/transport_meta/三平面地址
   ---- req 帧 (rid/rank/seq_len/...) --> |  接收端校验 seq_len <= max_seq_len
                                          |  维护/创建 ReceivedRequest
                                          |
   ==== RDMA write (数据平面，旁路 TCP) == |  KV 字节直接写进缓冲对应地址
                                          |
   ---- done 帧 {"done":true,rank} --->    |  done_ranks.add(rank)，判完成
                                          |
```

关键点：**RDMA 数据传输与控制平面的 `done` 消息是并行的**——代码注释里写得很明白「wait for this rank's done (RDMA happens meanwhile)」。发送端先发完 RDMA，再发 `done`；接收端收到 `done` 就认为数据已落地。

#### 4.2.3 源码精读

**协议常量与请求 id 归一化**：

[tilert/pd_vllm/wire.py:7-10](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/wire.py#L7-L10)

`MAGIC = "tilert-pd"` 是协议自报家门；`NUM_RANKS = 8` 是张量并行规模（注意它和 `sender_ranks` 不同——前者是 TP 总数，后者是真正发数据的 rank 子集）。

`derive_rid` 把 vLLM 的 `chatcmpl-xxx-abc123` 这类带随机后缀的 id 剥成稳定的 `rid`，让发送端（prefill connector）和路由层（pd_router）对同一次请求用同一个 `rid`：

[tilert/pd_vllm/wire.py:26-43](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/wire.py#L26-L43)

**长度前缀帧 `send_msg` / `recv_msg`**：

[tilert/pd_vllm/wire.py:46-56](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/wire.py#L46-L56)

`send_msg` 用 `struct.pack("!I", len(data))` 打一个 4 字节**大端**无符号整数做长度头，紧跟 JSON 字节。`recv_msg` 先精确读 4 字节拿长度 `n`，再精确读 `n` 字节。注意第 54-55 行的护栏：`n > 16 << 20`（16 MB）就抛错——控制消息永远是小 JSON，超大说明帧错位或被攻击，及时掐断。`_recv_exact` 在循环里 `recv` 直到读够，遇到对端中途关连接抛 `ConnectionError`：

[tilert/pd_vllm/wire.py:59-66](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/wire.py#L59-L66)

**hello 信封**：

[tilert/pd_vllm/wire.py:69-92](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/wire.py#L69-L92)

`hello_msg` 把校验字段（`magic/layout_version/transport/max_seq_len/busy`）、传输名片（`**transport_meta`）、布局地址（`**layout`）合并成一个 dict。发送端读这个 dict 就同时完成了「校验 + 拿到写地址」两件事。

**接收端发出 hello**（在 `_handle` 开头）：

[tilert/pd_vllm/receive_server.py:122-132](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/receive_server.py#L122-L132)

注意 `busy` 字段是**建议性（advisory）**的——它告诉发送端「我现在大概在忙」，但即便 busy，同一 rid 的 rank 仍会被放行；真正的接受/拒绝要等读到 `rid` 之后才定（见 4.3）。注释 [118-119 行](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/receive_server.py#L118-L119) 说得很清楚。

**发送端读 hello 并做四项校验**（对照 u4-l2 的 `_send`）：

[tilert/pd_vllm/prefill_connector.py:301-312](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L301-L312)

这就是「校验发生在发送端读 hello 时」的铁证：

| 校验项 | 防止的问题 |
| --- | --- |
| `magic == "tilert-pd"` | 连错服务——端口 5556 后面坐的不是 TileRT 接收端（比如配错了 IP 连到别的进程），早点断开。 |
| `layout_version == profile.layout_version` | **两端 KV 缓存布局不兼容**。最常见是 prefill 用 fp8 而 decode 用 bf16（或反之），两者的每 token 字节数和分区大小完全不同，RDMA 写进去会按错误偏移覆盖、静默损坏。`layout_version` 为每种 cache dtype 设不同值，把这种错配挡在 hello。 |
| `transport == self._transport.name` | **两端用了不同的 RDMA 引擎**（一端 mooncake、一端 nixl）。两者的连接元数据格式（`session_id` vs `nixl_meta/nixl_dev`）和写接口完全不同，对不上必然失败或乱写。 |
| `seq <= remote_max_seq_len` | **越界写**。发送端的 prompt 长度超过接收端预留的 `max_seq_len` 窗口，RDMA 会写出注册缓冲的边界，损坏显存。 |

`layout_version` 的取值逻辑值得细看——它**随 cache dtype 变化**：

[tilert/pd_vllm/profiles/mla_nsa.py:113-117](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L113-L117)

fp8 用基值（GLM-5 是 `10`），bf16 加 `_VERSION_BF16_OFFSET = 40` 变成 `50`。注释直说意图：「distinct wire version per cache dtype so a mismatched pairing (prefill fp8 vs decode bf16) is rejected at hello, not corrupted」。GLM-5 的基值定义在 [glm5.py:17](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/glm5.py#L17)。

> 补充：接收端收到 req 后还有一道**重复的 seq_len 校验**——[receive_server.py:136-138](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/receive_server.py#L136-L138)，超长直接回 `{"error": "seq_len exceeds max_seq_len"}`。这是纵深防御：即便发送端漏检，接收端也不会让越界写发生。

#### 4.2.4 代码实践

**实践目标**：把上面四项校验「为什么必要」内化为自己的话。

**操作步骤**（源码阅读 + 推理）：

1. 读 [prefill_connector.py:301-312](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L301-L312) 的四条 `assert`。
2. 对每条，写下「如果不校验，会怎样」——重点推演 `layout_version` 失配时数据是怎么被静默写坏的（提示：fp8 每 token 528B，bf16 每 token 1024B，发送端按 528B 算偏移写到 bf16 布局里，偏移全错）。
3. 读 [mla_nsa.py:113-117](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L113-L117)，确认 fp8/bf16 各自的 `layout_version`（10 / 50）。
4. 在 README 搜 `kv-cache-dtype`，确认 prefill 用 `fp8_ds_mla`、decode 用 `fp8`，两者映射到同一个 fp8 布局——这就是「两端必须匹配」的配置实践。

**需要观察的现象**：若故意把 decode 端 `--kv-cache-dtype` 改成 `bf16` 而 prefill 仍是 `fp8_ds_mla`，发送端 `_send` 会在 `assert hello.get("layout_version") == self._profile.layout_version` 处抛 `AssertionError`，连接被拒，不会有任何数据落地。

**预期结果**：能口述「layout_version 失配 → 抛 AssertionError、不写数据」；「transport 失配 → 同理」；「seq 超长 → 发送端 assert 与接收端 error 双重拦截」。

#### 4.2.5 小练习与答案

**练习 1**：为什么把控制平面做成「长度前缀 JSON」而不是直接用 `pickle` 或固定结构体？
**参考答案**：JSON 跨语言、可读、易调试（vLLM 是 Python，但未来接收端未必是）；长度前缀保证在 TCP 字节流上能可靠切分消息且支持变长字段（不同 profile 的 layout 地址数不同）；16 MB 上限护栏防止帧错位时无限读。`pickle` 有安全与跨语言问题，固定结构体则不够灵活。

**练习 2**：`busy` 为什么是 advisory 而不是 authoritative？
**参考答案**：发 hello 时接收端还不知道发送端要发哪个 `rid`。如果是当前正在处理的同一 `rid` 的另一个 rank（理论上 MLA 下不会，但机制要通用），必须放行而不是拒绝。所以 `busy` 只是个提示，真正的接受/拒绝要等读到 `rid` 之后在锁内决定（见 4.3 的 `_current` 状态机）。

---

### 4.3 多 rank 汇聚：ReceivedRequest 与 done_ranks

#### 4.3.1 概念说明

控制平面最后一件事：**怎么判定一次请求的数据「到齐了」**，可以交给下游 `convert → inject → decode`。

朴素思路是「收到一个 done 就算完」。但这只在「单 rank 发送」时成立。一般地，KV 可能在多个 TP rank 上各有一份不同的分片（比如标准 GQA 注意力按头切分），需要**每个发送 rank 都报告 done** 才算齐。所以需要一个状态：

- 记录这次请求的元信息（rid、seq_len、采样参数……）；
- 累积已报告 done 的 rank 集合 `done_ranks`；
- 当 `done_ranks` 覆盖全部 `sender_ranks` 时，标记完成、塞进队列。

这就是 `ReceivedRequest` dataclass 的职责。同时，因为解码引擎是 **bs=1 单槽位**（一次只服务一个请求），接收端还维护一个 `_current` 指针表示「当前槽位在服务谁」，用锁保护读写。

> 本项目的退化点：MLA 潜在 KV 在 TP 间复制，`sender_ranks = frozenset({0})`，只有 rank 0 发数据。所以「多 rank 汇聚」实际退化为「等 rank 0 一个人 done」。但代码用通用的 `done_ranks >= set(sender_ranks)` 判断，**机制是为多 rank 写的**，未来换非复制型注意力也不用改这里。

#### 4.3.2 核心流程

`_handle` 在 hello 之后的状态机（简化）：

```text
recv req = {rid, rank, seq_len, last_prompt_token, sampling, ...}
if req.seq_len > self.max_seq_len: 回 error, return       # 越界拦截

with lock:
    cur = self._current
    if cur is None or cur.rid != rid:                     # 槽位空 或 不同 rid
        if cur 正忙于另一个 rid 且未超时:
            回 {"error":"busy","busy_rid":...}, return     # bs=1 拒绝并发
        else:
            self._current = cur = ReceivedRequest(rid, ...)  # 占槽位

# （此期间 RDMA 数据并行写入缓冲）
done = recv_msg(conn)
if not done.get("done"): return

with lock:
    cur.done_ranks.add(rank)                              # 累积完成标志
    if cur.done_ranks >= set(profile.sender_ranks):       # 全部 sender 到齐？
        cur.t_complete = now
        self.completed.put(cur)                           # 通知下游：可以 convert 了
```

下游 `decode_server` 的 `/pd/decode` 会从 `self.completed` 这个队列里取走完成的 `ReceivedRequest`（见综合实践）。

#### 4.3.3 源码精读

**`ReceivedRequest` 数据类**：

[tilert/pd_vllm/receive_server.py:18-27](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/receive_server.py#L18-L27)

核心字段：`rid` 标识请求、`seq_len` 是实际 prompt 长度、`done_ranks` 是已 done 的 rank 集合、`t_first_conn / t_complete` 用于计时（算一次请求在接收端的总停留时间）。`first_token_id / sampling` 是 prefill 产出的首 token 和采样参数，会原样透传给 decode。

**bs=1 槽位 + 单请求创建**：

[tilert/pd_vllm/receive_server.py:140-160](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/receive_server.py#L140-L160)

这段在锁内决定「接受还是拒绝」。三种情况：

1. 槽位空（`cur is None`）→ 创建新 `ReceivedRequest` 占槽。
2. 槽位的 rid 与本次相同（`cur.rid == rid`）→ 同一请求的另一个 rank，放行（往 `done_ranks` 累积，见下）。
3. 槽位被**另一个**未完成且未超时的 rid 占着 → 回 `{"error":"busy"}` 拒绝。这是 bs=1 的硬约束：一次只伺候一个请求。

**done 累积与完成判定**：

[tilert/pd_vllm/receive_server.py:163-186](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/receive_server.py#L163-L186)

收到 `done` 帧后，`cur.done_ranks.add(rank)`，日志按 `(已到齐数 / sender 总数)` 打印进度。关键一行 [179 行](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/receive_server.py#L179)：`if cur.done_ranks >= set(self.profile.sender_ranks)`——用集合包含比较，**通用支持多 rank**。命中后设 `t_complete`、把 `cur` 塞进 `self.completed` 队列，下游就能取走了。

**为什么是 `{0}`：MLA 复制让 sender_ranks 塌缩成单点**：

[tilert/pd_vllm/profiles/mla_nsa.py:88-89](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L88-L89)

注释直说：「MLA latent replicated across TP」。再看发送端 [prefill_connector.py:257-258](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L257-L258)：`if self._tp_rank not in self._profile.sender_ranks: return`——rank 1..7 直接 return，根本不连。所以每个请求实际只有 rank 0 一条 TCP 连接，`done_ranks` 收到 `{0}` 就立刻满足 `>= {0}`，请求完成。**机制是多 rank 的，部署是单 rank 的**——这是读这段代码最容易看走眼的地方。

**槽位释放**：`release()` 把 `_current` 置空，供下一个请求复用：

[tilert/pd_vllm/receive_server.py:93-96](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/receive_server.py#L93-L96)

由 `decode_server` 在 inject/decode 完成后调用（见 u4-l4）。

#### 4.3.4 代码实践

**实践目标**：写一段伪代码，描述接收端如何用 hello 中的地址把发送端各 rank 的 RDMA 写入「拼」成一份完整请求。注意把「机制是多 rank、MLA 部署是单 rank」表达出来。

**操作步骤**：

1. 重读 [receive_server.py:47-89](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/receive_server.py#L47-L89)（启动期分配）与 [mla_nsa.py:323-342](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L323-L342)（发送端 `rdma_plan` 怎么用 hello 地址算 dst）。
2. 写出下面的伪代码（这是**示例代码**，非项目原码，用于说明原理）：

```python
# ── 启动期：一次性预约整块缓冲的地址（每个 section 都有固定落点）──
total = profile.buffer_bytes(max_seq_len)              # KV+PE+KI 三平面总字节
buf = gpu_zeros(total, uint8)
base = buf.data_ptr()
layout = profile.hello_layout(base, max_seq_len)       # {kv_base, pe_base, ki_base}
transport.register(base, total, dev_id)                # 允许远端 RDMA 写这块显存
my_card = transport.local_meta()                        # session_id / nixl_meta

# ── 每来一个发送 rank 的连接 ──
def on_rank_connect(conn):
    send_msg(conn, hello_msg(transport.name, my_card, max_seq_len,
                             profile.layout_version, layout, busy=advisory_busy))
    req = recv_msg(conn)                                # {rid, rank, seq_len, ...}
    assert req["seq_len"] <= max_seq_len                # 越界拦截
    cur = get_or_create_request(req["rid"], req["seq_len"])

    # 数据平面与控制平面并行：发送端此时正用 rdma_plan(layout) 算出
    # 每段 (src_gpu_addr, dst=layout["kv_base"]+层偏移, len) 并 RDMA 直写。
    # 接收端什么也不搬——数据按预约地址自动落到正确位置。

    done = recv_msg(conn)                               # 等这个 rank 报 done
    cur.done_ranks.add(req["rank"])
    if cur.done_ranks >= set(profile.sender_ranks):     # 全部 sender 到齐？
        completed_queue.put(cur)                        # → 交给 convert → inject → decode
```

3. 在伪代码旁标注：对 MLA+NSA，`sender_ranks = {0}`，所以循环只跑一次（rank 0 一人写完所有层的 KV/PE/KI，然后 done，请求立即完成）。

**需要观察的现象**：「拼接」完全靠 `layout` 里发布的 `kv_base/pe_base/ki_base` 三个地址——不同 rank、不同层、不同 section 写到**互不重叠**的地址区间（由 `rdma_plan` 的层偏移公式保证），到齐时缓冲里天然就是一份布局正确的完整请求。接收端**零拷贝**。

**预期结果**：能说清「为什么接收端不需要 memcpy 来拼请求」——因为地址在 hello 里预约好了，RDMA 直接写到对的位置。

#### 4.3.5 小练习与答案

**练习 1**：`done_ranks` 用集合 `>=` 比较 `sender_ranks`，如果某个 rank 因为网络抖动重复发了 `done`，会出错吗？
**参考答案**：不会。`done_ranks` 是 `set`，重复 `add` 同一个 `rank` 是幂等的，集合大小不变。`>=` 比较只看「是否包含全部 sender」，重复上报不改变结论。

**练习 2**：`NUM_RANKS=8` 但 `sender_ranks={0}`，这两者是什么关系？为什么日志的进度分母是 `len(sender_ranks)` 而不是 `NUM_RANKS`？
**参考答案**：`NUM_RANKS=8` 是 TP 规模（8 张卡都参与 prefill 计算），`sender_ranks={0}` 是「真正往 decode 节点发 KV 的 rank 子集」。因为 MLA 潜在 KV 在 8 卡间复制，只需 rank 0 发一份即可，其余 7 卡的 `wait_for_save` 直接 return。日志分母用 `len(sender_ranks)=1` 才能正确反映「等谁、等几个」；用 8 会永远卡在 1/8。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来，追踪一次 PD 请求在**接收端**从「TCP 连接进来」到「塞进 completed 队列」的完整生命，并对照发送端印证。

**步骤**：

1. **启动期**（模块 4.1）：从 [decode_server.py:326-328](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L326-L328) 看到 `ReceiveServer` 被构造，触发 `__init__`。画出此时 GPU 上发生的事：分配 ~12 GB 缓冲 → 算三平面地址 → 注册给 RDMA → 起 TCP 监听 `:5556`。

2. **握手期**（模块 4.2）：假设 prefill 的 rank 0 连进来。按 [`_handle`](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/receive_server.py#L114-L190) 的顺序，标注每一步交换的消息：`hello → req → (RDMA 并行) → done`。在 hello 上标出四个校验字段，在 req 上标出 `seq_len` 校验。

3. **汇聚期**（模块 4.3）：因为 `sender_ranks={0}`，rank 0 的 `done` 一到，[179 行](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/receive_server.py#L179) 立即满足，`ReceivedRequest` 进 `self.completed`。

4. **下游衔接**：读 [decode_server.py:97-134](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L97-L134) 的 `/pd/decode`，看它如何 `server.completed.get(timeout=...)` 取走这个 `ReceivedRequest`，然后 `profile.convert → engine.inject → engine.decode`。注意它先用 `rid` 匹配，丢弃不匹配的陈旧条目，并设有 `timeout_s`（默认 120s）防卡死。

5. **无 GPU 联调**（可选，呼应 u4-l1 的 StubEngine）：用 `python -m tilert.pd_vllm.decode_server --engine stub --model glm5 --max-seq-len 4096 --ctrl-port 5556 --http-port 5557` 起一个 stub 接收端（不需要真实模型权重），观察启动日志里的 `allocating receive buffer: X.XX GB` 和 `control plane listening on :5556`，验证本讲讲的缓冲分配与 TCP 监听确实发生。**待本地验证**：完整的数据面联调需要 mooncake/nixl 与 RDMA NIC，本步骤只验证控制平面骨架。

**预期产出**：一张标注了消息流、地址、校验点、状态机跳转的时序图，以及一句话总结——「接收端不搬数据，只预约地址 + 等到齐」。

## 6. 本讲小结

- **双轨设计**：控制平面走 TCP 传小 JSON（`wire.py` 的长度前缀帧），数据平面走 RDMA 传 KV 字节，两者并行——`done` 消息到达时数据已落地。
- **缓冲即地址预约**：`ReceiveServer.__init__` 一次性分配一块按 `max_seq_len` 预留的连续显存，用 `profile.hello_layout` 算出 kv/pe/ki 三平面地址，注册给 RDMA 后随 hello 发布。接收端**零拷贝**——数据按预约地址自动落位。
- **hello 四项校验**：`magic`（连对人）、`layout_version`（KV 布局/cache dtype 同款，失配会静默写坏）、`transport`（RDMA 引擎同款）、`max_seq_len`（不越界）。校验发生在发送端读 hello 时，接收端对 seq_len 还有重复拦截。
- **`layout_version` 随 cache dtype 变**：fp8 用基值（GLM-5 为 10），bf16 加 40，专门把「prefill fp8 / decode bf16」这类错配挡在握手期。
- **`ReceivedRequest` 多 rank 汇聚**：用 `done_ranks >= sender_ranks` 通用判定完成，机制支持多 rank；MLA 复制让 `sender_ranks={0}`，实际退化为等 rank 0 一人。
- **bs=1 单槽位**：`_current` + 锁实现一次只服务一个请求，并发来的别的 rid 会被 `{"error":"busy"}` 拒绝；`release()` 在 decode 完成后释放槽位。

## 7. 下一步学习建议

- **u4-l4（解码服务编排）**：本讲的 `self.completed` 队列被谁消费？`/pd/decode` 如何把 `wire_wait → convert → inject → decode` 四段串起来、如何处理 streaming 与取消——这是接收端的直接下游。
- **u4-l5（RDMA 传输层）**：本讲只用了 `transport.register / local_meta / write` 三个接口，mooncake 与 nixl 的 `write` 内部差异（`batch_transfer_sync_write` vs 轮询 `check_xfer_state`）在那里详讲。
- **u4-l6（引擎接口与缓存注入）**：`convert` 把裸字节 buffer 变回原生 `(ki, kv, pe)` 张量后，`engine.inject` 如何逐层写进 TileRT 的 KV 缓存、`set_cur_pos` 如何同步 RoPE 位置——那是数据落地后的最后一公里。
- **回看 u4-l2**：把发送端 `_send` 的四项 assert 与本讲接收端发出的 hello 字段逐项对上，你会对「两端如何靠一份 `wire.py` 契约对齐」有完整闭环的理解。
