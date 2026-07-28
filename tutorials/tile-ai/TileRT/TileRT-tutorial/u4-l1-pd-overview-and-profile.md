# PD 分离架构总览与 ModelProfile 抽象

## 1. 本讲目标

本讲是专家层「PD 分离部署」单元的第一篇。学完后你应该能够：

- 说出 PD（prefill–decode）分离部署里**三个进程**各自的角色、监听端口与相互调用关系，并画出一次请求从客户端到回包的完整数据流。
- 理解 TileRT 为什么把「随模型变化的部分」全部收口到一个叫 `ModelProfile` 的 Protocol，而让框架（connector / receive_server / decode_server / router）保持模型无关。
- 逐方法读懂 `ModelProfile` Protocol 的契约，并把它的方法分成 receive 侧、prefill 侧、engine 侧三类，知道每类被谁调用。
- 掌握 profile 的注册表与别名机制（`register` / `get_profile` / `_ALIASES`），理解它如何用懒加载避免一次性导入重依赖。

本讲只讲**总览与抽象边界**，不展开 RDMA 传输细节（u4-l5）、控制平面握手协议（u4-l3）、解码服务编排（u4-l4）和缓存注入实现（u4-l6）——它们各自有专讲。本讲给你一张地图，让你在后续讲义里随时知道自己站在哪一层。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（对应前置讲义）：

- **Generator 生命周期**（u1-l5）：`DSAv32Generator` / `GLM5Generator` 是「构造 → init 握手 → from_pretrained 加载 8 卡权重 → generate → cleanup」的普通 Python 类，是 TileRT 单进程解码的核心对象。本讲里它会被 `profile.build_engine` 包成一个 `PDEngine` 适配器。
- **三层张量执行契约**（u2-l5）：后端运行时要的是 `params / temp_vars / caches / profile_logs` 四组扁平张量列表，KV 缓存（`caches`）里有 `ki/kv/pe` 三类。本讲里你要看到：vLLM prefill 产出的正是这些 `ki/kv/pe` 缓存，要被「搬运」到 TileRT decode 节点的 `caches` 里。
- **MLA 与 NSA 稀疏注意力**（u2-l6）：KV 被压成 `kv_lora_rank=512` 的潜向量（MLA），另外还有一份 NSA 索引缓存 KI。这正是 PD 之间要传输的「注意力状态」的具体内容。

本讲还会用到几个新术语，先在这里给出口语化解释：

- **PD 分离（prefill–decode disaggregation）**：把一次生成拆成两段——**prefill**（吃进整段 prompt、算出首 token，计算密集、显存吃紧）和 **decode**（逐 token 自回归解码，访存密集、对单 token 延迟敏感）。把它们放在**不同节点**上跑，各自用最合适的硬件/引擎，是当前大模型低延迟服务的常见架构。
- **KV cache / 注意力状态**：transformer 解码时不需要重算历史 token，只要记住每层算出的 K、V（以及 RoPE 相关的位置编码）。把这套状态「搬走」就能在另一台机器上无缝接着解码。本讲里「KV 状态」「注意力状态」指的就是它。
- **RDMA**：远程直接内存访问，一种绕过对方 CPU、直接把数据写进对方显存/内存的高速网络传输方式，是 PD 之间搬 KV 的数据平面。
- **Protocol（结构化子类型）**：Python `typing.Protocol` 定义一组方法签名，任何「长成这样」的对象都算实现了它，不需要显式继承。你可以把它理解成「接口契约」。

## 3. 本讲源码地图

本讲涉及的文件全部在 `tilert/pd_vllm/` 包下（PD 分离的全部代码都打包进 `tilert` wheel，无需 fork vLLM）：

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| [README.md](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md) | 项目文档 | Topology A / B 的三条启动命令、端口约定 |
| [tilert/pd_vllm/profiles/base.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/base.py) | **ModelProfile Protocol + 注册表** | 本讲绝对主角 |
| [tilert/pd_vllm/engine_iface.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/engine_iface.py) | PDEngine Protocol + StubEngine | engine 侧契约、无 GPU 联调 |
| [tilert/pd_vllm/profiles/dsv32.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/dsv32.py) | DeepSeek-V3.2 profile | 一份薄配置如何注册 |
| [tilert/pd_vllm/profiles/glm5.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/glm5.py) | GLM-5 profile | 与 dsv32 镜像、差异仅在常量 |
| [tilert/pd_vllm/profiles/mla_nsa.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py) | 两模型共享的数据平面实现 | Protocol 方法真正落地处 |
| [tilert/pd_vllm/decode_server.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py) | 解码节点 HTTP 服务 | 框架如何调 profile.convert / build_engine |
| [tilert/pd_vllm/receive_server.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/receive_server.py) | 接收侧控制平面 | 框架如何调 buffer_bytes / hello_layout |
| [tilert/pd_vllm/pd_router.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/pd_router.py) | OpenAI 兼容入口 | 客户面、不碰 GPU |
| [tilert/pd_vllm/wire.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/wire.py) | 控制平面协议 | `NUM_RANKS=8` 等全局常量 |

一个贯穿全讲的判别准则：**框架代码里看不到任何「glm5」「dsv32」「kv_lora_rank」之类的模型细节**——凡是模型相关的，都被挤进了 `ModelProfile`。

## 4. 核心概念与源码讲解

### 4.1 PD 三进程拓扑与数据流

#### 4.1.1 概念说明

PD 分离的核心问题是：**prefill 和 decode 的资源画像几乎相反**。prefill 要一次性吞下几百上千 token，算力吃满、显存吃紧；decode 每步只喂一个 token，访存密集但算力富余，对单 token 延迟（TPOT）极度敏感。把它们塞进同一个引擎、同一批 GPU，必然互相挤占。

TileRT 的解法是**物理分离**：用擅长吞吐的 vLLM 做 prefill（产首 token + 填充 KV 缓存），再通过 RDMA 把 KV 状态高速搬到专攻超低延迟的 TileRT decode 节点接着解码。README 把这套方案放在 v0.1.5 发布，支持 GLM-5/5.1 与 DeepSeek-V3.2。

关键在于：整套连接器、接收服务、解码服务、路由都是**模型无关**的框架代码，只有「搬什么、怎么搬、搬到后怎么喂给引擎」这三件随模型变化的事被抽到了 `ModelProfile`。

#### 4.1.2 核心流程

Topology A 是最基本的三进程拓扑（README 第 324 行起）。三个进程与端口约定如下：

```
                         ┌─────────────────────────────────┐
 客户端 ──OpenAI HTTP──▶ │  pd_router  (OpenAI 入口)        │  :23333
                         │  CUDA_VISIBLE_DEVICES="" 不碰 GPU│
                         └──────┬───────────────────┬──────┘
                  ① prefill     │                   │ ③ /pd/decode
                  (max_tokens=1)│                   │ + 流式回包
                                ▼                   ▼
                ┌──────────────────────┐   ┌────────────────────────┐
                │ vLLM prefill (stock) │   │ TileRT decode_server   │
                │  :8000               │   │  ctrl :5556  http :5557│
                │  + TileRTConnector   │◀──│  ReceiveServer         │
                │    (kv_producer)     │   │  (kv_consumer 接收侧)  │
                └──────────────────────┘   └────────────────────────┘
                          │  ② RDMA 写 KV 状态（8 卡 → 8 卡）
                          └──────────────────────────────────────▶
```

一次请求的完整数据流（对照 README 第 364 行的文字描述）：

1. **客户端**把 OpenAI 请求发给 `pd_router:23333/v1/chat/completions`。
2. **router** 先在内存里挑一个空闲 decode 节点（全忙则回 429），再把请求以 `max_tokens=1 + logprobs` 转发给 **vLLM prefill**（[pd_router.py:135](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/pd_router.py#L135) 的 `_prefill`）。
3. **vLLM** 跑完 prefill、采出**首 token**；同时挂在 vLLM 里的 `TileRTConnector`（`prefill_connector.py`）认领该请求，把 KV 状态经 **RDMA** 写到 decode 节点的接收缓冲（控制平面走 TCP，数据走 RDMA）。
4. **router** 从首 token 的 logprobs 里解析出 token id，再调 decode 节点的 `POST /pd/decode`（[pd_router.py:171](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/pd_router.py#L171)）。
5. **decode_server** 收到 `/pd/decode` 后，执行**两阶段**（[decode_server.py:97](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L97)）：
   - **阶段一 prepare**：等 wire 传输完成 → `profile.convert` 把收到的字节缓冲转回原生张量 → `engine.inject` 注入引擎；
   - **阶段二 decode**：`engine.decode` 逐 token 解码，通过流式 ndjson 把 token 边算边吐回 router，router 再拼成 OpenAI 流式响应回客户端。

注意 router 的设计纪律（[pd_router.py:18](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/pd_router.py#L18)）：它必须运行在 `CUDA_VISIBLE_DEVICES=""` 的环境里——**router 永远不许碰 GPU**，它只做转发、调度和 token→text 的解析（`--parser glm47`）。GPU 全部留给 prefill 和 decode 节点。

> **补充：Topology B（选读）。** 当同一套 prefill 要同时喂「TileRT decode」和「原生 vLLM decode」两个池子时，用 vLLM 的 `MultiConnector` 把两个连接器并起来（README 第 366 行起）。每个请求恰好被一个连接器认领：带 `tilert_host` 标记的进 TileRT，其余进原生 vLLM。本讲的抽象边界对 Topology B 同样成立——只是 prefill 侧多挂了一个连接器。

#### 4.1.3 源码精读

三进程的入口都是 `python -m tilert.pd_vllm.<module>`。我们先看 decode_server 的 `main()` 如何用 profile 把「接收 + 引擎」两件事组装起来（[decode_server.py:271-336](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L271-L336)）：

```python
profile = profiles.get_profile(args.model)          # 按名字查注册表
if hasattr(profile, "configure"):
    profile.configure(args.kv_cache_dtype)          # 选 MLA 缓存 dtype（影响缓冲大小）
...
if args.engine == "stub":
    engine = StubEngine()                           # 无 GPU 联调
else:
    engine = profile.build_engine(                  # 委托给 profile 造引擎
        model_weights_dir=args.model_weights_dir,
        max_seq_len=args.max_seq_len,
        with_mtp=args.with_mtp, ar_steps=8)
server = ReceiveServer(profile, max_seq_len=...,    # 接收侧也只收一个 profile
                       ctrl_port=..., transport=...)
app = build_app(server, engine)
```

注意这段**框架代码里完全没有模型名**：`get_profile` 返回什么，下面就用什么。这正是本讲反复强调的判别准则。

再看 `/pd/decode` 的 prepare 阶段如何调用 profile（[decode_server.py:128-134](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L128-L134)）：

```python
conv = server.profile.convert(                      # receive 侧：字节缓冲 → 原生张量
    server.buffer, server.base_ptr, server.max_seq_len, req, server.profile.num_ranks)
engine.inject(conv)                                 # engine 侧：注入引擎
```

以及接收服务在启动时如何用 profile 决定缓冲大小和 hello 布局（[receive_server.py:47-66](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/receive_server.py#L47-L66)）：

```python
total = profile.buffer_bytes(max_seq_len)           # 要开多大的接收缓冲
self._hello_layout = profile.hello_layout(self.base_ptr, max_seq_len)  # 各段写到哪
self._transport.register(self.base_ptr, total, dev_id)                 # 注册给 RDMA
```

控制平面的 hello 握手则把 `profile.layout_version` 与 `_hello_layout` 一并发给发送端（[receive_server.py:122-132](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/receive_server.py#L122-L132)），发送端据此知道「把每一段 KV 写到对方哪个地址」。

#### 4.1.4 代码实践

**实践目标**：不启动任何 GPU 进程，仅凭源码与文档把 Topology A 的三进程端口与调用关系画清楚，并标注数据流的编号。

**操作步骤**：

1. 阅读 README 第 324–364 行（Topology A 的三段启动命令）。
2. 对照上面 4.1.2 的拓扑图，在一张纸上画出三个方框（router / vLLM prefill / decode_server），并在每个方框上标出它监听的端口（router `:23333`、prefill `:8000`、decode `ctrl :5556` + `http :5557`）。
3. 用带编号的箭头把 4.1.2 列的 5 步数据流画出来。
4. 打开 [pd_router.py:159](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/pd_router.py#L159) 的 `_handle`，确认「全忙则 429」与「转发 prefill」的顺序与你画的一致。

**需要观察的现象**：三个进程的端口互不冲突；router 同时连着 prefill 和 decode 两个方向；RDMA 数据流是「prefill → decode」，而控制流却是 decode 的 `ReceiveServer` 先发 hello 给 prefill（因为接收方要先开好缓冲、告诉发送方地址）。

**预期结果**：你应当得到一张与 4.1.2 一致的图，并能解释为什么 hello 是**接收方先发**（接收方要预先 `buffer_bytes` 开缓冲、`hello_layout` 给出每段地址）。

#### 4.1.5 小练习与答案

**练习 1**：Topology A 里，为什么 router 要用 `max_tokens=1` 调 vLLM，而不是让 vLLM 直接把整段回答生成完？
> **答案**：因为 vLLM 在这个拓扑里只负责 prefill（算首 token + 填 KV 缓存），后续逐 token 解码要交给超低延迟的 TileRT decode 节点。让 vLLM 生成完就失去了 PD 分离的意义。

**练习 2**：router 进程为什么必须设 `CUDA_VISIBLE_DEVICES=""`？
> **答案**：router 只做转发、调度和 token→text 解析，不参与任何计算；它若占用 GPU 会与 prefill/decode 节点争抢显存，破坏分离部署的资源隔离。

---

### 4.2 ModelProfile Protocol 方法集

#### 4.2.1 概念说明

PD 数据平面里「随模型变化」的事可以归纳成三组：

- **receive 侧（decode 节点）**：要开多大的接收缓冲？收到字节后怎么还原成原生张量？
- **prefill 侧（vLLM 连接器 worker）**：vLLM 注册的 KV cache 里有哪几层、哪些是 MLA、哪些是 NSA 索引？怎么把每个 rank 的 KV 从 paged cache 里抠出来、排成发送缓冲？怎么算 RDMA 写入计划？
- **engine 侧（decode 节点）**：怎么把一个 ready 的 Generator 包成 `inject/decode/reset` 三方法的引擎适配器？

`ModelProfile` 就是这三组事的**契约清单**。它是一个 `typing.Protocol`（[base.py:16](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/base.py#L16)），框架代码只认这份契约，不认具体模型。这样带来的好处是：**新增一个模型 = 写一个新 profile，框架零改动**。

#### 4.2.2 核心流程

Protocol 把方法显式分成了三段，源码里用注释标了分界（[base.py:24, 39, 61](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/base.py#L24)）。下面用伪代码列出谁在什么时候调谁：

```
# ── 启动期（decode 节点拉起）─────────────────────────
decode_server.main():
    profile = get_profile(model)              # 查注册表
    profile.configure(kv_cache_dtype)         # 选缓存 dtype（可选）
    ReceiveServer.__init__:
        profile.buffer_bytes(max_seq_len)     # → 决定接收缓冲大小
        profile.hello_layout(base_ptr, ...)   # → 生成各段远程地址
    profile.build_engine(...)                 # → 构造 PDEngine 适配器

# ── 连接期（每个请求）─────────────────────────────
receive_server._handle():                    # 每来一个发送端连接
    发 hello(含 layout_version + hello_layout)
    收 rid/rank/seq_len → 累积 done_ranks，直到 sender_ranks 齐全

# ── 解码期（router 调 /pd/decode）──────────────────
decode_server.pd_decode():
    等 wire 传输完成
    profile.convert(buffer, ...)              # → 字节缓冲还原成 ConvertedRequest
    engine.inject(conv)                       # → 写入引擎 KV 缓存
    engine.decode(first_token, max_tokens, ...)  # → 逐 token 解码

# ── prefill 侧（vLLM worker 内）────────────────────
prefill_connector:
    profile.classify_layers(kv_caches, ...)   # → 辨认 MLA / KI 层、校验层数
    profile.staging_bytes(reg, rank, ...)     # → 每 rank 的发送缓冲大小
    profile.extract(reg, meta, rank, ...)     # → 把 KV 从 paged cache 抠进 staging
    profile.rdma_plan(hello, sections, ...)   # → (src, dst, len) 三元组列表
```

几条贯穿全程的属性：

- `name`：profile 的规范化名（`"glm5"` / `"dsv32"`）。
- `num_ranks`：数据平面涉及的卡数，固定为 `wire.NUM_RANKS = 8`（[wire.py:9](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/wire.py#L9)）。
- `sender_ranks`：**哪些 rank 真正发数据**。MLA 的潜 KV 在张量并行里是**复制**的，所以只有 rank 0 发（`frozenset({0})`），其余 7 卡的发送缓冲只占 4 字节占位（见 4.2.3）。
- `layout_version`：线缆布局版本号，是一个**属性**而非普通字段——它的值会随缓存 dtype 变化（fp8 vs bf16 差一个偏移量），用于在 hello 握手时把「两端 dtype 不匹配」这种致命错误**提前拦下**，而不是让它产生损坏的数据。

#### 4.2.3 源码精读

先看 Protocol 的全貌与三段切分（[base.py:16-66](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/base.py#L16-L66)）：

```python
class ModelProfile(Protocol):
    name: str
    num_ranks: int
    sender_ranks: frozenset

    @property
    def layout_version(self) -> int: ...

    # ── receive side (decode node) ──
    def buffer_bytes(self, max_seq_len: int) -> int: ...
    def hello_layout(self, base_ptr: int, max_seq_len: int) -> dict[str, int]: ...
    def convert(self, buffer, base_ptr, max_seq_len, received, num_devices) -> Any: ...

    # ── prefill side (vLLM connector worker) ──
    def classify_layers(self, kv_caches, kv_cache_config) -> Any: ...
    def staging_bytes(self, reg, tp_rank, max_seq_len) -> int: ...
    def extract(self, reg, req_meta, tp_rank, staging, max_seq_len) -> Any: ...
    def rdma_plan(self, hello, sections, tp_rank, seq_len, staging_base) -> tuple[list, list, list]: ...

    # ── engine (decode node) ──
    def build_engine(self, model_weights_dir, max_seq_len, with_mtp, ar_steps) -> Any: ...
```

每个方法的 docstring 都写清了「输入是什么、返回给框架什么」。比如 `hello_layout` 的注释（[base.py:28-32](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/base.py#L28-L32)）明确说它「告诉发送端把每一段 RDMA 写到哪个地址」。

prefill 侧的 `staging_bytes` 有一个反直觉点，看共享实现（[mla_nsa.py:280-283](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L280-L283)）：

```python
def staging_bytes(self, reg, tp_rank, max_seq_len):
    if tp_rank not in self.sender_ranks:   # 非 rank0 不发
        return 4                            # 4 字节占位
    return self.buffer_bytes(max_seq_len)   # rank0 才开全量缓冲
```

这正是 `sender_ranks` 的用途：因为 MLA 潜 KV 在 TP 间复制，只有 rank 0 需要真正发送，其余 7 卡只给一个最小占位缓冲，省下 7/8 的 staging 显存。

`layout_version` 作为属性（property）而非字段，是因为它的值依赖运行期才确定的缓存 dtype（[mla_nsa.py:113-117](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L113-L117)）：

```python
@property
def layout_version(self) -> int:
    # fp8 与 bf16 用不同 wire 版本，dtype 不匹配在 hello 就被拒，而不是产生脏数据
    return self._base_version + (0 if self.mla_fp8 else _VERSION_BF16_OFFSET)
```

与 `ModelProfile` 并列的还有一份 **engine 侧契约** `PDEngine`（[engine_iface.py:12-33](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/engine_iface.py#L12-L33)），它就是 `build_engine` 的返回类型应满足的接口：只有 `inject / decode / reset` 三个方法。`decode` 的签名里带了 `on_token` 回调与 `cancel_event`，这是为流式输出和客户端断连安全收尾留的钩子（u4-l4 会详讲）。

同文件还提供了一个 `StubEngine`（[engine_iface.py:35-56](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/engine_iface.py#L35-L56)）——一个不碰 GPU、不导入 tilert 的「回显引擎」，固定吐 `(11, 22, 33)` 三个 token。它的价值在于：让你在没有 B200 的机器上也能跑通 `receive → convert → inject → decode` 的整条管道，专门用来联调框架代码（decode_server 的 `--engine stub` 即用它，见 [decode_server.py:308-311](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L308-L311)）。

#### 4.2.4 代码实践

**实践目标**：把 `ModelProfile` 的方法按「receive / prefill / engine」三类归档，并验证框架确实只在对应的进程里调用对应类别。

**操作步骤**：

1. 打开 [base.py:16-66](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/base.py#L16-L66)，把 10 个方法分成三列抄进下表。
2. 用 Grep 在 `decode_server.py` / `receive_server.py` 里搜 `profile.`，确认它们只调 receive 类（`buffer_bytes` / `hello_layout` / `convert`）和 engine 类（`build_engine`），**从不**调 prefill 类。
3. 用 Grep 在 `prefill_connector.py` 里搜 `profile.`，确认它只调 prefill 类（`classify_layers` / `staging_bytes` / `extract` / `rdma_plan`），**从不**调 receive 类。

**参考归档表**：

| 类别 | 方法 | 调用方进程 |
|------|------|-----------|
| receive 侧 | `buffer_bytes`、`hello_layout`、`convert` | decode 节点的 ReceiveServer / decode_server |
| prefill 侧 | `classify_layers`、`staging_bytes`、`extract`、`rdma_plan` | vLLM prefill 的 connector worker |
| engine 侧 | `build_engine` | decode 节点的 decode_server（仅启动时一次） |

**预期结果**：三类方法在框架里被调用的位置与上表完全吻合；receive 与 prefill 两类方法**互不交叉**地分布在两个进程里——这就是「模型无关框架」的具体含义。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `layout_version` 是 `@property` 而不是普通的类属性？
> **答案**：因为它的值依赖运行期才确定的 MLA 缓存 dtype（fp8 还是 bf16），同一个 profile 在不同 dtype 下会给出不同的版本号，从而在 hello 握手时把两端 dtype 不匹配提前拦下。

**练习 2**：`sender_ranks = frozenset({0})` 对 staging 缓冲有什么影响？
> **答案**：只有 rank 0 真正发送数据，`staging_bytes` 对其余 7 卡只返回 4 字节占位，从而省下 7/8 的 staging 显存。

**练习 3**：`StubEngine` 满足 `PDEngine` Protocol 吗？为什么它对联调有用？
> **答案**：满足——它实现了 `inject / decode / reset` 三个方法。它不导入 tilert、不碰 GPU，固定回显几个 token，因此能在无 GPU 环境下跑通整条 receive→convert→inject→decode 管道，用来验证框架代码。

---

### 4.3 profile 注册与别名

#### 4.3.1 概念说明

有了 Protocol 契约，还需要一个**注册表**把「模型名」映射到「具体 profile 实例」。这是框架能在启动时仅凭一个 `--model glm5` 字符串就找到正确 profile 的关键。

`base.py` 用三个东西组成了这套机制：

- `_REGISTRY: dict[str, ModelProfile]`：规范化名 → profile 实例的字典。
- `_ALIASES`：把用户可能传入的各种写法（`glm_5` / `glm-5` / `deepseek_v3_2` / `v32` …）归一化到规范化名（`glm5` / `dsv32`）。
- `register(profile)` / `get_profile(name)`：写入与（懒加载）读取。

一个重要设计是**懒加载**：profile 的实现会 import 沉重的模型依赖（torch、generator 等），所以注册表初始为空，只有 `get_profile` 第一次被调用时才 import 对应的 profile 模块、触发其 `base.register(...)`。

#### 4.3.2 核心流程

注册与查找的流程如下：

```
# profile 模块（如 dsv32.py / glm5.py）在被 import 时执行：
base.register(MlaNsaProfile(name="dsv32", num_layers=62, layout_version=11, ...))
        │
        ▼
_REGISTRY["dsv32"] = <profile 实例>


# 框架启动时（decode_server.main）：
profile = get_profile(args.model)        # args.model 可能是 "deepseek_v3_2"
        │
        ▼
canon = _ALIASES["deepseek_v3_2"]        # → "dsv32"   （归一化别名）
if canon not in _REGISTRY:               # 还没注册
    from tilert.pd_vllm.profiles import dsv32   # 懒 import，触发上面的 register
return _REGISTRY["dsv32"]
```

两个具体 profile 的注册几乎是镜像的，差异只在三个常量：

| profile | 规范化名 | `NUM_LAYERS` | `LAYOUT_VERSION` | 引擎工厂构造的 Generator |
|---------|---------|-------------|------------------|------------------------|
| DeepSeek-V3.2 | `dsv32` | 62（61 主 + 1 MTP） | 11 | `DSAv32Generator`（[dsv32.py:11-12](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/dsv32.py#L11-L12)） |
| GLM-5 | `glm5` | 79（78 主 + 1 MTP） | 10 | `GLM5Generator`（[glm5.py:16-17](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/glm5.py#L16-L17)） |

两者都把真正的数据平面逻辑委托给共享的 `MlaNsaProfile`（[mla_nsa.py:81](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L81)），只各自传入「层数 + wire 版本 + 引擎工厂」三件套——因为两个模型都是「MLA 潜 KV + NSA 索引 + MTP 投机」的同族架构，差别仅在规模与 Generator 类。

#### 4.3.3 源码精读

注册表的全部状态只有两个模块级变量（[base.py:68-78](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/base.py#L68-L78)）：

```python
_REGISTRY: dict[str, ModelProfile] = {}
_ALIASES = {
    "glm5": "glm5", "glm_5": "glm5", "glm-5": "glm5",
    "dsv32": "dsv32", "deepseek_v3_2": "dsv32",
    "deepseek-v3.2": "dsv32", "dsv3.2": "dsv32", "v32": "dsv32",
}
```

`get_profile` 的懒加载是这套机制的精髓（[base.py:85-97](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/base.py#L85-L97)）：

```python
def get_profile(name: str) -> ModelProfile:
    canon = _ALIASES.get(name, name)        # 别名归一化
    if canon not in _REGISTRY:
        # 懒 import：profile 的重依赖只在被选中时才加载
        if canon == "glm5":
            from tilert.pd_vllm.profiles import glm5   # noqa: F401
        elif canon == "dsv32":
            from tilert.pd_vllm.profiles import dsv32  # noqa: F401
    if canon not in _REGISTRY:
        raise KeyError(
            f"unknown model profile {name!r}; accepted keys (incl. aliases): {sorted(_ALIASES)}")
    return _REGISTRY[canon]
```

注意三个细节：① `_ALIASES.get(name, name)` 允许直接传规范化名（不在别名表里就用原名）；② import 语句本身没有赋值目标（`noqa: F401`），它的副作用就是触发模块末尾的 `base.register(...)`；③ 如果归一化后的名字既不在注册表也不是已知 canon，就抛 `KeyError` 并列出所有可接受的键，给出友好报错。

再看 dsv32 profile 是如何把自己挂进注册表的（[dsv32.py:34-41](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/dsv32.py#L34-L41)）：

```python
base.register(
    MlaNsaProfile(
        name="dsv32",
        num_layers=NUM_LAYERS,           # 62
        layout_version=LAYOUT_VERSION,   # 11
        engine_factory=_build_engine,    # 局部函数：构造 DSAv32Generator
    )
)
```

`_build_engine`（[dsv32.py:15-31](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/dsv32.py#L15-L31)）做的事就是 u1-l5 讲过的 Generator 生命周期——`load_backend` → 构造 `DSAv32Generator` → `from_pretrained` → 包成 `MlaNsaEngineAdapter`。也就是说，engine 侧 profile 方法 `build_engine` 本质上是「按 profile 配置启动一个 Generator 并包成 PDEngine」。这正是本讲与前置讲义 u1-l5 的衔接点。

#### 4.3.4 代码实践

**实践目标**：在不加载任何后端 `.so`、不碰 GPU 的前提下，验证注册表与别名机制按预期工作。

**操作步骤**：

1. 在已安装 `tilert`（但不必有 B200）的环境里执行下面这段「示例代码」（它不调用 `build_engine`，所以不会触发后端加载）：

   ```python
   # 示例代码：仅验证注册表与别名，不加载后端、不碰 GPU
   from tilert.pd_vllm.profiles import base

   for alias in ["glm5", "glm_5", "glm-5", "deepseek_v3_2", "v32", "deepseek-v3.2"]:
       p = base.get_profile(alias)
       print(f"{alias:>16} -> canon={p.name:<6} num_ranks={p.num_ranks} "
             f"sender_ranks={sorted(p.sender_ranks)} layout_version={p.layout_version}")
   ```

2. 再故意传一个不存在的名字，观察报错：

   ```python
   # 示例代码：观察未知 profile 的报错信息
   try:
       base.get_profile("qwen3")
   except KeyError as e:
       print(e)
   ```

**需要观察的现象**：第一段里所有别名都被归一化成 `glm5` 或 `dsv32` 两个规范名；两个 profile 的 `num_ranks` 都是 8、`sender_ranks` 都是 `{0}`；`layout_version` 在未 `configure` 前是各自的 base 版本（glm5=10、dsv32=11）。第二段会抛 `KeyError` 并打印所有可接受的键。

**预期结果**：

- `glm5` / `glm_5` / `glm-5` → canon=`glm5`，layout_version=10；
- `deepseek_v3_2` / `v32` / `deepseek-v3.2` → canon=`dsv32`，layout_version=11；
- `qwen3` 抛 `KeyError`，消息里含 `accepted keys (incl. aliases)`。

> 若当前环境未安装 `tilert` 或导入 profile 时因缺 torch 报错，则本实践标为「待本地验证」——但注册表与别名的逻辑可直接从 [base.py:68-97](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/base.py#L68-L97) 静态读出，结论不变。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_REGISTRY` 初始为空，而不是在 `profiles/__init__.py` 里一次性导入所有 profile？
> **答案**：为了懒加载。每个 profile 会 import 沉重的模型依赖（torch、具体 Generator），懒加载保证「只用到的 profile 才付出导入成本」，且避免无谓的后端/依赖加载。

**练习 2**：`get_profile("deepseek-v3.2")` 经历了哪两步映射？
> **答案**：① 别名归一化：`_ALIASES["deepseek-v3.2"]` → `"dsv32"`；② 懒 import `profiles.dsv32` 触发 `base.register(...)`，最后从 `_REGISTRY["dsv32"]` 取出实例。

**练习 3**：如果要新增支持第三个同族模型（比如某 MLA+NSA+MTP 架构），需要改框架代码（decode_server / receive_server / router）吗？
> **答案**：不需要。只需新增一个 `profiles/<new>.py`，在其中 `base.register(MlaNsaProfile(name=..., num_layers=..., layout_version=..., engine_factory=...))`，并在 `_ALIASES` 加几个别名即可——这正是 ModelProfile 抽象带来的可扩展性。

---

## 5. 综合实践

把本讲三块内容串起来，完成下面这个「**地图与契约对照**」综合任务：

1. **画拓扑图**：对照 README Topology A，画出 router / vLLM prefill / decode_server 三方框，标全端口（router `:23333`、prefill `:8000`、decode `ctrl :5556` + `http :5557`），并用编号箭头画出一次请求的 5 步数据流（含 RDMA 的方向）。

2. **标注 profile 方法的落点**：在你画的拓扑图上，把 `ModelProfile` 的 10 个方法贴到调用它的进程框里——
   - decode_server 框：`buffer_bytes` / `hello_layout` / `convert`（receive 类）+ `build_engine`（engine 类）；
   - vLLM prefill 框：`classify_layers` / `staging_bytes` / `extract` / `rdma_plan`（prefill 类）；
   - router 框：**不出现任何 profile 方法**（它只做转发与解析）。

3. **走查启动序列**：用伪代码写出 decode 节点从 `python -m tilert.pd_vllm.decode_server --model glm5 ...` 到「准备好接收第一个请求」的全过程，标出每一步调用了 profile 的哪个方法（提示：`get_profile` → `configure` → `buffer_bytes` / `hello_layout` → `build_engine`）。

4. **回答一个综合问题**：如果某天团队要把 GLM-5 的 MLA 缓存从 fp8 换成 bf16，**框架代码（四个进程文件）需要改吗？哪些 profile 层的东西会变？** （提示：`layout_version` 的偏移、`buffer_bytes` 的每 token 字节数都会随 dtype 变，但这些变化都封装在 profile 内部。）

**预期产出**：一张标注完整的拓扑图 + 一份启动序列伪代码 + 一段对综合问题的文字回答。完成后，你应该能自信地解释：为什么 TileRT 把 PD 分离的全部模型差异都收口到了 `ModelProfile` 这一处。

## 6. 本讲小结

- PD 分离用**三个进程**——`pd_router`（OpenAI 入口，不碰 GPU）、vLLM prefill（产首 token + 填 KV）、`decode_server`（TileRT 超低延迟解码）——把 prefill 与 decode 在物理上隔离；首 token 之后的 KV 状态经 RDMA 从 prefill 搬到 decode。
- 一次请求的数据流是：客户端 → router → vLLM prefill（max_tokens=1）→ connector 认领并 RDMA 发 KV → router 调 decode `/pd/decode` → decode 两阶段（等 wire + convert + inject，再 decode 流式回吐）。
- `ModelProfile` Protocol 把「随模型变化的部分」收口到一处，框架（connector / receive_server / decode_server / router）完全模型无关；它的方法可分 receive 侧（`buffer_bytes` / `hello_layout` / `convert`）、prefill 侧（`classify_layers` / `staging_bytes` / `extract` / `rdma_plan`）、engine 侧（`build_engine`）三类。
- `sender_ranks={0}` 反映了 MLA 潜 KV 在 TP 间复制的特性——只有 rank 0 真正发数据，其余 7 卡只占 4 字节 staging 占位。
- `layout_version` 是随缓存 dtype 变化的属性，用于在 hello 握手时把两端 dtype 不匹配提前拦下，避免产生脏数据。
- 注册表用 `_REGISTRY` + `_ALIASES` + 懒加载 `get_profile` 实现：别名归一化 → 按需 import profile 模块 → 触发 `base.register(...)`；新增同族模型无需改框架。

## 7. 下一步学习建议

本讲只给了 PD 分离的「地图与契约边界」。后续讲义会沿着这条数据流逐段深挖：

- **u4-l2 vLLM Prefill 连接器**：精读 `prefill_connector.py`，看 prefill 侧四个方法（`classify_layers` / `staging_bytes` / `extract` / `rdma_plan`）如何被 vLLM 的 KVConnector 钩子驱动，以及 claim 纪律如何在 MultiConnector 下保证安全。
- **u4-l3 接收服务与控制平面协议**：精读 `receive_server.py` 与 `wire.py`，看 hello 握手如何校验 magic / layout_version / transport / max_seq_len，以及多 rank 如何汇聚成一个完整请求。
- **u4-l4 解码服务编排**：精读 `decode_server.py` 的 `/pd/decode` 两阶段、bs=1 互斥锁与流式取消安全。
- **u4-l5 RDMA 传输层**：精读 `transport.py`，对比 mooncake 与 nixl 两种实现。
- **u4-l6 引擎接口与缓存注入**：精读 `engine_iface.py` 的 `PDEngine` 与 `inject_cache`，看 `convert` 产出的 `ConvertedRequest.layers` 如何被逐层写进 Generator 的 `caches`（衔接 u2-l5 的三层张量契约）。

建议在进入 u4-l2 之前，先回到本讲的 4.2.3 把 `ModelProfile` 的方法表再过一遍——后续每篇讲义实质上都是在展开这张表里的某一行。
