# 注意力后端抽象与 Hybrid

## 1. 本讲目标

本讲是「注意力后端」单元（u7）的第一篇，聚焦**抽象层**，不深挖任何一个具体后端的实现细节（FlashInfer 的实现留到 u7-l2）。

读完本讲，你应该能够：

1. 说清 `BaseAttnBackend` / `BaseAttnMetadata` 这对抽象接口定义了哪几个方法、各自在何时被谁调用。
2. 解释 `HybridBackend` 为什么能让 prefill 与 decode 用不同的底层 kernel，以及它如何把 CUDA Graph 相关调用全部转发给 decode 后端。
3. 描述 `SUPPORTED_ATTENTION_BACKENDS` 注册表 + `create_attention_backend` 工厂如何把一个字符串（如 `"fa,fi"`）变成一个后端对象，以及「懒导入」为什么重要。
4. 复述 `_adjust_config` 里 `auto` 的选择规则，能预测 SM100（H200）、SM90（H100）、SM80（A100）上分别会选到什么后端。

本讲承接 u5-l2（Engine forward 与采样）——那里我们只把 `model.forward()` 当作一个黑盒，本讲打开黑盒里**注意力这一环**的「调度入口」。

## 2. 前置知识

在进入抽象之前，先用最通俗的话对齐几个概念。

- **注意力（Attention）与 Q/K/V**：Transformer 每一层都会算注意力。可以把它想象成「对历史信息做一次加权检索」——当前 token 生成三份向量 `Q`（查询）、`K`（键）、`V`（值），用 `Q` 去和所有历史位置的 `K` 算相似度，得到权重后再对 `V` 加权求和。这里的关键是：**K/V 可以被反复复用**，不必每次重算，这就是 KV Cache 的动机。
- **Prefill 与 Decode 两阶段**（u1-l1、u2-l1）：Prefill 是「读 prompt」，一次吃进一长串 token，计算形状大且每批都不同；Decode 是「逐个吐 token」，每条请求每轮只新增 1 个 token，形状小且固定。两者的计算特征截然不同，所以**常常用不同的 kernel 才能各自最优**——这是本讲 `HybridBackend` 存在的根本原因。
- **KV Cache 池与 page_table**（u6）：所有层的 K/V 被装进一块分页式的显存池（`MHAKVCache`），用一张 `page_table` 把「某请求第 i 个位置」映射到「池子里的某个槽位」。注意力后端要读取 K/V，就必须看懂这张表。
- **CUDA Graph**（u5-l3）：把 decode 每轮前向录制成一张图反复回放，省下 CPU 逐个 launch kernel 的开销。但图要求**形状固定**，所以只对 decode 生效，prefill 不参与。
- **SM 架构版本（compute capability）**：NVIDIA GPU 的「代际编号」，形如 `(major, minor)`。例如 SM90 = Hopper（H100），SM100 = Blackwell（H200/B200），SM80 = Ampere（A100）。不同代际支持的注意力 kernel 不同，这是 `auto` 选择的依据。

一句话定位：**注意力后端 = 一组「给定 Q/K/V 和 batch，算出注意力输出」的可替换实现**。本讲讲的是这组实现的「接口、组合方式与选择策略」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [python/minisgl/attention/base.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/base.py) | 定义 `BaseAttnMetadata`、`BaseAttnBackend` 两个抽象基类，以及组合器 `HybridBackend`。本讲的核心。 |
| [python/minisgl/attention/\_\_init\_\_.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/__init__.py) | 注册表 `SUPPORTED_ATTENTION_BACKENDS`、校验函数 `validate_attn_backend`、工厂 `create_attention_backend`。 |
| [python/minisgl/engine/engine.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py) | `Engine.__init__` 里实例化后端、`_adjust_config` 里实现 `auto` 选择。 |
| [python/minisgl/utils/arch.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/arch.py) | `is_sm90_supported` / `is_sm100_supported`，按 GPU 代际做判断。 |
| [python/minisgl/server/args.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py) | CLI 参数 `--attn` / `--attention-backend` 的定义与默认值。 |

调用方（帮助理解接口「被谁调用」，本讲会点到为止）：

| 调用点 | 位置 |
| --- | --- |
| 调度器在算前向前预计算元数据 | `scheduler/scheduler.py` 的 `prepare_metadata` 调用 |
| 每层注意力真正算前向 | `layers/attention.py` 的 `attn_backend.forward` 调用 |
| prefill 时只取每条请求最后一个 token 的 logits | `layers/embedding.py` 的 `get_last_indices` 调用 |
| CUDA Graph 捕获/回放的三段协议 | `engine/graph.py` |

## 4. 核心概念与源码讲解

### 4.1 BaseAttnBackend 与 BaseAttnMetadata：注意力后端的统一契约

#### 4.1.1 概念说明

Mini-SGLang 支持三种底层注意力实现：FlashAttention（`fa`）、FlashInfer（`fi`）、TensorRT-LLM（`trtllm`）。它们的输入输出语义相同——给定 `Q/K/V` 和一个 batch，返回注意力输出——但内部调用的 kernel、需要的元数据、对 page 表的处理方式各不相同。

为了让上层（Engine、Scheduler、模型层）**完全不关心**你用的是哪种 kernel，项目抽出了两个抽象基类：

- `BaseAttnBackend`：**「做注意力」的对象**。定义了 5 个必须实现的方法。
- `BaseAttnMetadata`：**「这一批的注意力参数」**。每个后端用自己的子类存自己需要的张量（如 `cu_seqlens`、`page_table`），但都实现一个公共方法 `get_last_indices`。

这是一种典型的**依赖倒置**：上层依赖抽象，底层实现细节可替换。

#### 4.1.2 核心流程

一个后端对象在整个生命周期里会经历两类调用，时序如下：

```
① 一次性初始化（Engine.__init__）
   create_attention_backend(...)  →  得到一个 BaseAttnBackend 实例

② 每一轮调度（每个 forward batch）
   Scheduler 调 prepare_metadata(batch)   ← CPU 侧，算前向前预计算
        ↓ 把结果写到 batch.attn_metadata
   Engine.forward_batch → model.forward → 每层 AttentionLayer 调
        attn_backend.forward(q, k, v, layer_id, batch)   ← GPU 侧，逐层

③ CUDA Graph 一次性捕获 + 每轮回放（仅 decode）
   init_capture_graph(max_seq_len, bs_list)        ← 一次性
   对每个 bs：prepare_for_capture(batch)           ← 捕获时
   每轮回放：prepare_for_replay(batch) → graph.replay()
```

关键设计：`prepare_metadata` 和 `forward` 被刻意拆成两步。元数据（哪些请求、各自多长、page 表切片）**只依赖于 batch 结构、不依赖于具体某一层**，所以提前算一次，所有层共享同一份 `batch.attn_metadata`；而 `forward` 每层都要调一次（因为 K/V 是逐层不同的）。

#### 4.1.3 源码精读

先看元数据基类（[python/minisgl/attention/base.py:12-15](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/base.py#L12-L15)）：

```python
@dataclass
class BaseAttnMetadata(ABC):
    @abstractmethod
    def get_last_indices(self, bs: int) -> torch.Tensor: ...
```

`get_last_indices(bs)` 返回「每条请求最后一个 token 在压平的 prefill 序列里的下标」。为什么需要它？Prefill 时一个 batch 里多条 prompt 被拼成一条长序列一起算注意力（为了并行），但采样只需要**每条 prompt 的最后一个 token** 来预测下一个词。所以 `ParallelLMHead` 在 prefill 时会用它做一次索引gather（见 [python/minisgl/layers/embedding.py:92-94](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/embedding.py#L92-L94)）：

```python
if batch.is_prefill:
    indices = batch.attn_metadata.get_last_indices(bs)
    x = x[indices].contiguous()
```

这个方法之所以能跨后端多态工作，是因为**三种后端的元数据子类都存了一个 `cu_seqlens_q` 字段**，实现都是 `cu_seqlens_q[1:1+bs] - 1`（见 fa.py / trtllm.py / fi.py 的同名方法）。`cu_seqlens_q` 是「每条请求 query 长度的前缀和」，它的第 i 项减 1 正好是第 i 条请求最后一个 query 的位置。

再看后端基类（[python/minisgl/attention/base.py:18-34](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/base.py#L18-L34)）：

```python
class BaseAttnBackend(ABC):
    @abstractmethod
    def forward(self, q, k, v, layer_id, batch) -> torch.Tensor: ...

    @abstractmethod
    def prepare_metadata(self, batch) -> None: ...

    @abstractmethod
    def init_capture_graph(self, max_seq_len, bs_list) -> None: ...

    @abstractmethod
    def prepare_for_capture(self, batch) -> None: ...

    @abstractmethod
    def prepare_for_replay(self, batch) -> None: ...
```

五个方法的分工：

| 方法 | 调用时机 | 调用者 | 作用 |
| --- | --- | --- | --- |
| `prepare_metadata` | 每批，前向前 | Scheduler | 把 batch 的长度/page 信息算成后端专属元数据，写入 `batch.attn_metadata` |
| `forward` | 每批、每层 | `AttentionLayer` | 先 `store_kv` 写入新 K/V，再用 kernel 算注意力 |
| `init_capture_graph` | 启动一次 | `GraphRunner` | 为 CUDA Graph 预分配固定形状的缓冲 |
| `prepare_for_capture` | 捕获每个 bs 时 | `GraphRunner` | 给捕获用的 dummy batch 填上合法元数据 |
| `prepare_for_replay` | 每次回放前 | `GraphRunner` | 把真实 batch 的元数据拷进固定缓冲 |

`forward` 的签名里带了 `layer_id` 和 `batch`：`layer_id` 用来定位该写/读哪一层的 K/V 池切片；`batch` 用来取出之前 `prepare_metadata` 存好的 `attn_metadata`。以 FlashAttention 后端为例（[python/minisgl/attention/fa.py:48-65](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fa.py#L48-L65)）：

```python
def forward(self, q, k, v, layer_id, batch):
    metadata = batch.attn_metadata
    assert isinstance(metadata, FAMetadata)
    self.kvcache.store_kv(k, v, batch.out_loc, layer_id)   # 先写入 K/V 池
    return _fa_sgl_impl(q=q, k_cache=self.kvcache.k_cache(layer_id), ...)
```

注意「先 `store_kv` 再算注意力」这个顺序是跨后端不变量——所有后端都先把本轮新算的 K/V 落池，再让 kernel 从池里读，保证读到的是最新值（u6-l1 讲过的 store_kv）。

#### 4.1.4 代码实践

**实践目标**：验证「`prepare_metadata` 在前向前由 Scheduler 调用，`forward` 由模型层逐层调用」这一分工。

**操作步骤**：

1. 打开 [python/minisgl/scheduler/scheduler.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py)，找到第 211 行附近的 `self.engine.attn_backend.prepare_metadata(batch)`。确认它位于 `_schedule_next_batch` 里、在构造 `ForwardInput` **之前**。
2. 打开 [python/minisgl/layers/attention.py:56](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/attention.py#L56)，确认 `AttentionLayer.forward` 里调用的是 `ctx.attn_backend.forward(q, k, v, self.layer_id, ctx.batch)`。

**需要观察的现象**：`prepare_metadata` 只在一批里被调用 1 次（Scheduler 侧），而 `forward` 会被调用 `num_layers` 次（每层一次）。

**预期结果**：你会清楚看到「元数据算一次、前向算 N 层」的分工。这正是把两者拆成两个方法的理由。

> 说明：本实践是源码阅读型，不依赖 GPU，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：`BaseAttnMetadata` 只强制要求一个方法 `get_last_indices`。为什么把它放进基类、而不是每个后端各写各的？

**参考答案**：因为调用方 `ParallelLMHead`（embedding.py:93）只认识抽象类型 `BaseAttnMetadata`，不认识 `FAMetadata`/`FIMetadata` 等子类。把方法放进基类，调用方就能用统一接口 `batch.attn_metadata.get_last_indices(bs)` 多态调用，新增后端时上层零改动。

**练习 2**：如果把 `prepare_metadata` 和 `forward` 合并成一个方法（每层都重新算元数据），会带来什么问题？

**参考答案**：① 重复计算——元数据只依赖 batch 结构、与具体层无关，逐层重算纯属浪费；② 破坏 overlap scheduling——元数据里的 CPU→GPU 拷贝本可以在 Scheduler 侧提前overlap 掉，合并后只能串行在 GPU 侧逐层做。

---

### 4.2 HybridBackend：让 prefill 与 decode 各用最优后端

#### 4.2.1 概念说明

不同阶段最适合的 kernel 不同：FlashAttention 的 prefill kernel 在长 prompt 上很快，但它的 decode 路径在某些卡上不如 FlashInfer 的 paged decode；反之亦然。一个朴素办法是「全局选一个折中后端」，但这放弃了「各阶段最优」的可能。

`HybridBackend` 给出了更灵活的方案：**同时持有两个后端**——一个专门伺候 prefill、一个专门伺候 decode，运行时按当前 batch 的阶段二选一。对上层而言，它仍然是一个 `BaseAttnBackend`，完全透明。

#### 4.2.2 核心流程

```
HybridBackend.forward / prepare_metadata(batch):
    if batch.is_prefill:
        用 prefill_backend
    else:
        用 decode_backend

HybridBackend.init_capture_graph / prepare_for_capture / prepare_for_replay(batch):
    一律转发给 decode_backend   # 因为 CUDA Graph 只对 decode 生效
```

分发依据是 `batch.is_prefill`（[python/minisgl/core.py:83-85](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py#L83-L85)），它就是判断 `phase == "prefill"`。

为什么三个 capture 方法只转发给 decode 后端？因为 u5-l3 讲过：**CUDA Graph 只对 decode 且 `size <= max_graph_bs` 时生效**，prefill 形状每批皆变、根本不进捕获路径。所以 prefill 后端永远不会被 `GraphRunner` 调用到 capture 相关方法。

#### 4.2.3 源码精读

构造与两个会分阶段的方法（[python/minisgl/attention/base.py:37-54](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/base.py#L37-L54)）：

```python
class HybridBackend(BaseAttnBackend):
    def __init__(self, prefill_backend, decode_backend) -> None:
        self.prefill_backend = prefill_backend
        self.decode_backend = decode_backend

    def forward(self, q, k, v, layer_id, batch):
        backend = self.prefill_backend if batch.is_prefill else self.decode_backend
        return backend.forward(q, k, v, layer_id, batch)

    def prepare_metadata(self, batch):
        backend = self.prefill_backend if batch.is_prefill else self.decode_backend
        return backend.prepare_metadata(batch)
```

三个 capture 方法直接转发给 decode 后端（[python/minisgl/attention/base.py:56-63](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/base.py#L56-L63)）：

```python
def init_capture_graph(self, max_seq_len, bs_list):
    self.decode_backend.init_capture_graph(max_seq_len, bs_list)

def prepare_for_capture(self, batch):
    self.decode_backend.prepare_for_capture(batch)

def prepare_for_replay(self, batch):
    self.decode_backend.prepare_for_replay(batch)
```

这里有一个**隐含契约**值得注意：当 `forward`/`prepare_metadata` 被调用时，`batch` 上挂的 `attn_metadata` 必须是「被分发到的那个后端」对应的子类。这个一致性是由「同一个 batch 里 `prepare_metadata` 和后续的每层 `forward` 都走同一个分发分支（都看 `is_prefill`）」自然保证的——只要 `is_prefill` 在一批内不变（它确实不变，`phase` 是 batch 创建时就定好的），就不会错配。

#### 4.2.4 代码实践

**实践目标**：验证「`fa,fi` 这种 hybrid 配置下，prefill 走 FA、decode 走 FI」的运行时分发。

**操作步骤**：

1. 阅读 [python/minisgl/attention/fa.py:48-65](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fa.py#L48-L65) 的 `FlashAttentionBackend.forward`，注意它 `assert isinstance(metadata, FAMetadata)`。
2. 同理打开 `fi.py` 的 `forward`（[python/minisgl/attention/fi.py:176](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fi.py#L176)），看它断言的是哪种 metadata。
3. 结合 4.2.2 的分发逻辑推理：一个 prefill batch 进来时，`HybridBackend.prepare_metadata` 调的是 FA，于是 `batch.attn_metadata` 是 `FAMetadata`；接着每层 `HybridBackend.forward` 也分发到 FA，断言通过。

**需要观察的现象**：两端的 `isinstance` 断言之所以不会在 hybrid 下炸，是因为 prepare 与 forward 走的是**同一个后端实例**。

**预期结果**：你能用一句话说清「分阶段分发保证元数据类型自洽」。

> 说明：本实践为源码阅读型推理。若本地有 GPU 且安装了 flashinfer/sgl-kernel，可用 `--attn fa,fi` 启动后从日志确认。

#### 4.2.5 小练习与答案

**练习 1**：如果用户写 `--attn fi,fi`（prefill 和 decode 都指定 fi），`HybridBackend` 还会被创建吗？

**参考答案**：不会。工厂函数 `create_attention_backend` 发现两端相同时会走 single backend 分支并打 warning（见 4.3.3）。所以 `fi,fi` 等价于单个 `fi`，不会产生 `HybridBackend` 的分发开销。

**练习 2**：为什么 `prepare_for_capture` 不需要按 `is_prefill` 分发？

**参考答案**：因为 CUDA Graph 只捕获 decode 路径（u5-l3），`GraphRunner` 永远不会对 prefill batch 调 capture 方法，所以无条件转发给 decode 后端即可。

---

### 4.3 注册表与工厂：create_attention_backend 与 SUPPORTED_ATTENTION_BACKENDS

#### 4.3.1 概念说明

有了抽象基类，还需要一个机制把「字符串名字」（如 `"fa"`）映射到「构造具体后端的函数」。Mini-SGLang 用一个泛型 `Registry` 来做这件事，配合一个工厂函数 `create_attention_backend` 完成从字符串到对象的全部转换，包括 hybrid 的递归构造。

两个设计要点：

- **懒导入（lazy import）**：每个后端的构造函数体里才 `from .fa import FlashAttentionBackend`。这样即使用户只装了 flashinfer 没装 sgl-kernel，`import minisgl.attention` 也不会因 FA 导入失败而崩——只有真正选用 FA 时才报错。
- **校验前置**：CLI 解析阶段就用 `validate_attn_backend` 拦截非法名字，而不是等到 Engine 初始化时才炸。

#### 4.3.2 核心流程

```
① 注册（模块加载时）
   @SUPPORTED_ATTENTION_BACKENDS.register("fa")
   def create_fa_backend(config): ...   # 注册的是一个「构造函数」

② CLI 解析（args.py）
   --attn 的 type=validate_attn_backend
        → 非法名字立即 ArgumentTypeError

③ Engine 初始化
   create_attention_backend("fa,fi", config)
        ├─ 含逗号 → 拆成 ("fa","fi")
        │    ├─ 递归 create_attention_backend("fa") → FlashAttentionBackend
        │    ├─ 递归 create_attention_backend("fi") → FlashInferBackend
        │    └─ return HybridBackend(fa, fi)
        └─ 无逗号 → SUPPORTED_ATTENTION_BACKENDS[name](config)
```

#### 4.3.3 源码精读

注册表本身是个极简的字典包装（[python/minisgl/utils/registry.py:6-23](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/registry.py#L6-L23)），`register` 返回装饰器、`__getitem__` 按名字取值。

注意力包里实例化它并注册三个后端（[python/minisgl/attention/\_\_init\_\_.py:19-40](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/__init__.py#L19-L40)）：

```python
SUPPORTED_ATTENTION_BACKENDS = Registry[BackendCreator]("Attention Backend")

@SUPPORTED_ATTENTION_BACKENDS.register("trtllm")
def create_trtllm_backend(config):
    from .trtllm import TensorRTLLMBackend   # 懒导入
    return TensorRTLLMBackend(config)

@SUPPORTED_ATTENTION_BACKENDS.register("fi")
def create_fi_backend(config):
    from .fi import FlashInferBackend
    return FlashInferBackend(config)

@SUPPORTED_ATTENTION_BACKENDS.register("fa")
def create_fa_backend(config):
    from .fa import FlashAttentionBackend
    return FlashAttentionBackend(config)
```

注意 `BackendCreator` 是一个 `Protocol`（[python/minisgl/attention/\_\_init\_\_.py:15-16](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/__init__.py#L15-L16)），约定每个注册项都是「接收 `ModelConfig`、返回 `BaseAttnBackend`」的可调用对象。

校验函数处理 `auto` 与普通名字（[python/minisgl/attention/\_\_init\_\_.py:43-49](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/__init__.py#L43-L49)）：

```python
def validate_attn_backend(backend, allow_auto=True):
    if backend != "auto":
        required_backends = backend.split(",") if "," in backend else [backend]
        SUPPORTED_ATTENTION_BACKENDS.assert_supported(required_backends)
    else:
        assert allow_auto, "auto is not allowed here"
    return backend
```

注意 `allow_auto` 这个开关：CLI 解析时 `allow_auto=True`（允许用户传 `auto`），但工厂里 `allow_auto=False`——也就是说**工厂永远不会直接处理 `auto`**，`auto` 必须先在 `_adjust_config` 里被解析成具体名字（见 4.4）。

工厂的核心是 hybrid 递归（[python/minisgl/attention/\_\_init\_\_.py:52-68](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/__init__.py#L52-L68)）：

```python
def create_attention_backend(backend, config):
    validate_attn_backend(backend, allow_auto=False)
    if "," in backend:
        assert backend.count(",") == 1, "Only one comma is allowed in hybrid backend"
        p_backend, d_backend = backend.split(",", 1)
        if p_backend != d_backend:
            logger.info(f"Using hybrid attention backend: prefill={p_backend}, decode={d_backend}")
            p_backend = create_attention_backend(p_backend, config)
            d_backend = create_attention_backend(d_backend, config)
            return HybridBackend(p_backend, d_backend)
        backend = p_backend  # 两端相同，降级为单后端
        logger.warning(f"P/D attention backends are the same: {backend}, using single backend.")
    return SUPPORTED_ATTENTION_BACKENDS[backend](config)
```

三个细节：

1. **只允许一个逗号**：`backend.count(",") == 1`，所以 `"fa,fi,xx"` 会直接报错。hybrid 只能是「prefill, decode」两段。
2. **递归构造**：`"fa,fi"` 会分别递归调用 `create_attention_backend("fa")` 和 `create_attention_backend("fi")`，各自落到最后的 `SUPPORTED_ATTENTION_BACKENDS[...](config)`。递归保证每段都走完整校验。
3. **同名降级**：`"fi,fi"` 不会建 Hybrid，直接当单后端 `"fi"` 处理并 warning。

Engine 里实际调用点（[python/minisgl/engine/engine.py:76-78](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L76-L78)），注意它发生在 KV cache 与 page_table 建好**之后**：

```python
self.ctx.attn_backend = self.attn_backend = create_attention_backend(
    config.attention_backend, config.model_config
)
```

之所以顺序在 KV cache 之后，是因为每个后端的 `__init__` 都会从全局 ctx 里取 `kv_cache` 和 `page_size`（如 [python/minisgl/attention/fa.py:37-41](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/fa.py#L37-L41)）：

```python
def __init__(self, config: ModelConfig):
    ctx = get_global_ctx()
    self.kvcache = ctx.kv_cache        # 需要池子已建好
    self.page_size = ctx.page_size
```

#### 4.3.4 代码实践

**实践目标**：手动跟踪 `"fa,fi"` 这一字符串在工厂里的完整转换路径。

**操作步骤**：

1. 假设传入 `create_attention_backend("fa,fi", config)`。
2. 走到 `if "," in backend:` 分支，`split(",", 1)` 得到 `("fa", "fi")`。
3. 递归 `create_attention_backend("fa", config)`：无逗号，落到 `SUPPORTED_ATTENTION_BACKENDS["fa"](config)` → 执行 `create_fa_backend` → `from .fa import ...` → 返回 `FlashAttentionBackend(config)`。
4. 递归 `create_attention_backend("fi", config)`：同理返回 `FlashInferBackend(config)`。
5. 返回 `HybridBackend(FlashAttentionBackend 实例, FlashInferBackend 实例)`。

**需要观察的现象**：最终对象是一个 `HybridBackend`，它的两个属性分别指向 FA 和 FI 实例。

**预期结果**：你能画出「字符串 → 两个递归调用 → HybridBackend」的调用树。

> 说明：本实践为源码阅读型跟踪，无需 GPU。

#### 4.3.5 小练习与答案

**练习 1**：为什么三个 `create_xxx_backend` 里都用函数内 `from .xxx import ...` 而不是在文件顶部 import？

**参考答案**：为了懒导入。`flashinfer`、`sgl-kernel` 等是重型 CUDA 依赖，并非所有环境都装齐。放函数体内后，`import minisgl.attention` 本身不会触发这些导入；只有真正选用某个后端时才导入，失败信息也更精确（明确指向缺失的那个后端）。

**练习 2**：如果用户在 CLI 传了一个拼错的 `--attn abc`，会在哪一步报错？

**参考答案**：在 CLI 解析阶段就报错。`--attn` 的 `type=validate_attn_backend` 会在 argparse 调用该校验函数，`assert_supported(["abc"])` 找不到该名字，抛出 `ArgumentTypeError`，argparse 会直接拒绝并提示支持的名字列表。错误不会拖到 Engine 初始化。

---

### 4.4 auto 自动选择：按 SM 架构挑后端

#### 4.4.1 概念说明

`EngineConfig.attention_backend` 的默认值是 `"auto"`（[python/minisgl/engine/config.py:21](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/config.py#L21)）。也就是说，用户什么都不指定时，系统会根据当前 GPU 的代际（SM 版本）自动挑一个最合适的后端组合。

`auto` 不是一种后端，而是一个**占位符**：它在 Engine 初始化早期（`_adjust_config`）被改写成具体名字（如 `"fa,fi"`），之后才进入 4.3 的工厂。这与 u2-l2 讲过的「`_adjust_config` 用 `object.__setattr__` 绕过 frozen 锁做归一化」是同一机制。

#### 4.4.2 核心流程

```
Engine.__init__ 开头：
   _adjust_config(config)
        └─ if config.attention_backend == "auto":
              根据 SM 版本改写：
                SM100 (Blackwell/H200)  → "trtllm"
                SM90  (Hopper/H100)     → "fa,fi"   (hybrid)
                其它（如 SM80 Ampere）  → "fi"
              object.__setattr__(config, "attention_backend", 改写后的值)
        └─ if "trtllm" in ... 且 page_size 非法：page_size 改成 64
   ...（随后正常走 create_attention_backend）
```

判断 SM 版本的依据是 `torch.cuda.get_device_capability()`，它返回形如 `(9, 0)` 的元组（major, minor）。

#### 4.4.3 源码精读

SM 检测函数（[python/minisgl/utils/arch.py:17-29](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/arch.py#L17-L29)）：

```python
@functools.cache
def _get_torch_cuda_version():
    import torch, torch.version
    if not torch.cuda.is_available() or not torch.version.cuda:
        return None
    return torch.cuda.get_device_capability()

def is_arch_supported(major, minor=0):
    arch = _get_torch_cuda_version()
    if arch is None:
        return False
    return arch >= (major, minor)

def is_sm90_supported():
    return is_arch_supported(9, 0)

def is_sm100_supported():
    return is_arch_supported(10, 0)
```

注意三点：

1. **`@functools.cache`**：GPU 代际在一次进程里不变，缓存避免反复查询。
2. **无 CUDA 时返回 `False`**：`arch is None` 时两个判断都为假，`auto` 会落到最后的 `"fi"` 分支（当然，没 GPU 时后续也跑不起来，但逻辑自洽）。
3. **`>=` 比较**：`is_sm90_supported()` 在 SM100 上也返回 `True`（因为 10 ≥ 9）。这一点在 4.4.4 的实践里很关键——选择顺序是「先判 100，再判 90」，否则会被前面的分支抢先。

`_adjust_config` 的核心逻辑（[python/minisgl/engine/engine.py:218-229](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L218-L229)）：

```python
def _adjust_config(config: EngineConfig):
    def override(attr, value):  # this is dangerous, use with caution
        object.__setattr__(config, attr, value)

    if config.attention_backend == "auto":
        backend = "trtllm" if is_sm100_supported() else ("fa,fi" if is_sm90_supported() else "fi")
        override("attention_backend", backend)
        logger.info_rank0(f"Auto-selected attention backend: {config.attention_backend}")

    if "trtllm" in config.attention_backend and config.page_size not in [16, 32, 64]:
        override("page_size", 64)
        logger.warning_rank0("Page size is overridden to 64 for TRTLLM backend")
```

把它翻译成决策表：

| GPU 代际 | `is_sm100_supported()` | `is_sm90_supported()` | 选中后端 | 说明 |
| --- | --- | --- | --- | --- |
| SM100（H200/B200，Blackwell） | True | True | `trtllm` | 单后端，TensorRT-LLM，且 page_size 被强制为 64 |
| SM90（H100，Hopper） | False | True | `fa,fi` | hybrid：FA 做 prefill、FI 做 decode |
| SM80 及更早（A100 等） | False | False | `fi` | 单后端，FlashInfer |

补充一个连带副作用：选了 `trtllm` 后，如果用户的 `page_size` 不在 `{16, 32, 64}` 里（比如默认的 1），会被 `override` 成 64 并打 warning。这是因为 TRT-LLM 的 paged 注意力 kernel 只接受这几个 block size。

CLI 侧的入口（[python/minisgl/server/args.py:188-195](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L188-L195)）：

```python
parser.add_argument(
    "--attention-backend",
    "--attn",
    type=validate_attn_backend,
    default=ServerArgs.attention_backend,   # 即 "auto"
    help="...If two backends are specified, "
         "the first one is used for prefill and the second one for decode.",
)
```

`--attn` 是 `--attention-backend` 的短别名，默认 `"auto"`。help 文本也点明了「逗号分隔时前者 prefill、后者 decode」的语义。

#### 4.4.4 代码实践（本讲指定的核心实践）

**实践目标**：阅读 `_adjust_config` 中 `auto` 的选择逻辑，说明在 SM100（H200）与 SM90（Hopper）上默认分别选什么 prefill / decode 后端。

**操作步骤**：

1. 打开 [python/minisgl/engine/engine.py:222-225](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L222-L225)，找到那条三元表达式：

   ```python
   backend = "trtllm" if is_sm100_supported() else ("fa,fi" if is_sm90_supported() else "fi")
   ```

2. 对照 [python/minisgl/utils/arch.py:24-29](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/arch.py#L24-L29) 理解 `is_sm100_supported` / `is_sm90_supported` 的语义（`get_device_capability() >= (10,0)` / `>= (9,0)`）。
3. 填下面这张表的「选中后端」列。

**需要观察的现象**：

| 平台 | `get_device_capability()` | 命中分支 | 选中后端 | prefill 后端 | decode 后端 |
| --- | --- | --- | --- | --- | --- |
| SM100（H200，Blackwell） | `(10, 0)` | 第 1 个 `if` | `trtllm` | ? | ? |
| SM90（H100，Hopper） | `(9, 0)` | 第 2 个 `if` | `fa,fi` | ? | ? |

**预期结果（答案）**：

- **SM100（H200）**：`is_sm100_supported()` 为 True → 选中 `"trtllm"`。它**没有逗号**，是单后端，所以 prefill 与 decode **都用 TensorRT-LLM**（`TensorRTLLMBackend` 在自己的 `forward` 内部再按 `batch.is_prefill` 分流到 `trtllm_batch_context_with_kv_cache` / `trtllm_batch_decode_with_kv_cache`，见 [python/minisgl/attention/trtllm.py:60-89](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/trtllm.py#L60-L89)）。附带副作用：page_size 被强制改成 64。
- **SM90（H100/Hopper）**：`is_sm100_supported()` 为 False、`is_sm90_supported()` 为 True → 选中 `"fa,fi"`。有逗号 → hybrid：**prefill 用 FlashAttention（`fa`），decode 用 FlashInfer（`fi`）**，最终对象是 `HybridBackend(FlashAttentionBackend, FlashInferBackend)`。

> 说明：本实践为源码阅读型，结论可直接从代码推出，无需 GPU。若想本地验证，可在有相应 GPU 的机器上启动服务，观察日志里 `Auto-selected attention backend: ...` 那一行（由 `logger.info_rank0` 打印）。

#### 4.4.5 小练习与答案

**练习 1**：如果把三元表达式里两个判断的顺序对调（先判 `is_sm90_supported` 再判 `is_sm100_supported`），会出什么问题？

**参考答案**：在 SM100 机器上，`is_sm90_supported()` 也为 True（10 ≥ 9），会先命中 `fa,fi` 分支，永远选不到 `trtllm`。所以「先判更高代际」的顺序是必需的，这是一个典型的「用 `>=` 比较时必须从严到松排列」的陷阱。

**练习 2**：`auto` 这个字符串有可能原封不动传到 `create_attention_backend` 吗？

**参考答案**：不会。`_adjust_config` 在 `Engine.__init__` 最开头（[engine.py:33](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L33)）就被调用，一定把 `auto` 改写成具体名字。即便万一漏改，`create_attention_backend` 里也调了 `validate_attn_backend(backend, allow_auto=False)`，`auto` 会被 `assert allow_auto` 直接挡住报错。这是双重保险。

---

## 5. 综合实践

**任务**：从零接入一个「假想的」新注意力后端 `myattn`，把本讲四个模块的知识串起来。

请按下面的 checklist，逐项指出**要改哪个文件、改什么、为什么**：

1. **实现后端类**：你需要新建 `python/minisgl/attention/myattn.py`，写一个 `MyAttnBackend(BaseAttnBackend)`。它必须实现哪 5 个方法？（参考 [base.py:18-34](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/base.py#L18-L34)）分别对应什么时机被调用？
2. **实现元数据类**：你还需要一个 `MyAttnMetadata(BaseAttnMetadata)`，它至少要实现哪个方法？为了让 `ParallelLMHead` 在 prefill 时能取到最后 token，你需要在元数据里存什么字段？（提示：参考 `cu_seqlens_q` 的用法）
3. **注册**：在 [python/minisgl/attention/\_\_init\_\_.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/attention/__init__.py) 里加一个 `@SUPPORTED_ATTENTION_BACKENDS.register("myattn")` 的构造函数，并说明为什么要用**懒导入**。
4. **让 auto 也能选到它（可选）**：如果你想让它成为某代 GPU 的默认，该改 [engine.py:222-225](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L222-L225) 的哪一行？为什么改 `auto` 顺序时要「从严到松」？
5. **用作 hybrid 的一段**：如果用户写 `--attn myattn,fi`，工厂会怎么构造？（画出递归调用树，确认最终得到 `HybridBackend(MyAttnBackend, FlashInferBackend)`，且 capture 三方法只转发给 FI。）

**验收标准**：你不应该改动源码（本讲禁止），只需在笔记里写出每一项的「文件 + 改动 + 理由」。如果某一项你不确定（比如 `MyAttnMetadata` 里除 `cu_seqlens_q` 外还需哪些字段），可以标注「待确认」并说明你会去读哪个现有后端（如 `fi.py`）作为模板。

> 这个综合练习直接对应 u10-l3「支持新模型架构」的同类思路：注册表 + 抽象接口 + 工厂是 Mini-SGLang 里「可扩展点」的通用模式。

## 6. 本讲小结

- `BaseAttnBackend` 用 5 个抽象方法定义了注意力后端的统一契约：`prepare_metadata`（每批一次，Scheduler 调）、`forward`（每批每层，模型层调）、`init_capture_graph` / `prepare_for_capture` / `prepare_for_replay`（CUDA Graph 三段协议，仅 decode）。
- `BaseAttnMetadata` 只强制一个 `get_last_indices`，让 `ParallelLMHead` 能跨后端多态地取出 prefill 时每条请求最后一个 token。
- `HybridBackend` 同时持有 prefill 与 decode 两个后端，按 `batch.is_prefill` 分发 `forward`/`prepare_metadata`，把 capture 三方法无条件转发给 decode 后端。
- `SUPPORTED_ATTENTION_BACKENDS` 注册表 + `create_attention_backend` 工厂把字符串变成对象：含逗号则递归构造两端并包成 `HybridBackend`，同名则降级为单后端；构造函数用懒导入隔离重型 CUDA 依赖。
- `auto` 是占位符，在 `_adjust_config` 里按 SM 代际改写：SM100→`trtllm`、SM90→`fa,fi`、其它→`fi`；选 trtllm 时还会把 page_size 强制成 64。
- 后端实例化必须在 KV cache 与 page_table 建好之后，因为后端 `__init__` 要从全局 ctx 取 `kv_cache` 和 `page_size`。

## 7. 下一步学习建议

- **u7-l2 FlashInfer 后端实现**：本讲把 `FlashInferBackend` 当黑盒，下一讲打开它，看 `prepare_metadata` 如何把 batch 的长度信息转成 flashinfer 需要的 `indptr/indices`、`forward` 如何先 store_kv 再 paged run、以及它专用的 CUDA Graph 捕获 wrapper。
- **重读 u5-l3（CUDA Graph）**：带着本讲对 `init_capture_graph/prepare_for_capture/prepare_for_replay` 三段协议的理解，回去看 `GraphRunner` 会更清晰。
- **u6（KV Cache）**：本讲多次提到 `store_kv` 和 page_table，若想彻底理解后端 `forward` 里「先写池再读池」的数据流，建议（重）读 u6-l1。
- **动手预习**：做一遍第 5 节的综合实践，对照真实的 `fa.py` / `trtllm.py` 检查你的 checklist，能为 u10-l3「接入新模型/新后端」打下基础。
