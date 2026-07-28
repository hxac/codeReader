# 引擎接口与缓存注入：inject_cache / inject_last_hidden_state

## 1. 本讲目标

本讲是 PD（prefill-decode）分离系列的收尾篇。前面几讲已经把 KV 状态从 vLLM prefill 节点经控制平面、RDMA 数据平面送到了 decode 节点的接收缓冲，并在 `convert` 阶段反量化成原生 BF16 张量。本讲要解决最后一个问题：**这些反量化后的张量，如何「灌进」TileRT 的解码引擎，让它像自己做过 prefill 一样继续解码？**

学完后你应该能够：

1. 说出 `PDEngine` 协议的三个方法（`inject` / `decode` / `reset`）各自的职责，以及 `StubEngine` 为什么能在没有 GPU、没有后端 `.so` 的情况下跑通整条服务管道。
2. 理解 `profile.build_engine` 如何把模型差异收口到具体引擎适配器（`MlaNsaEngineAdapter`），并让框架对模型保持无感。
3. 看懂 `inject_cache` 如何把外部 prefill 的 `(ki, kv, pe)` 逐层、逐卡写进扁平的 `caches` 列表。
4. 解释为什么注入缓存之后**必须**调用 `set_cur_pos` 才能正确继续 RoPE，以及 MTP 模式下 `inject_last_hidden_state` 的作用。

## 2. 前置知识

本讲默认你已建立以下认知（见前置讲义摘要）：

- **PD 三进程拓扑**（u4-l1）：vLLM prefill 产出首 token 与 KV 缓存，经 RDMA 搬到 decode 节点；decode_server 是编排器，自身不做模型计算。
- **四段编排**（u4-l4）：`/pd/decode` 分 prepare（wire-wait → convert → inject）与 decode 两阶段；`convert` 在 decode 侧完成 `fp8→bf16` 反量化。
- **四元张量契约与 `Idx`**（u2-l5）：TileRT 后端接收 `params`（权重）、`temp_vars`（56 个激活槽）、`caches`（KV 缓存）、`profile_logs` 四组扁平张量列表；`Idx`（`DsaTempVarIdx`）给扁平下标起名。
- **MLA 三层缓存**（u2-l6）：每层 MLA 持有 `ki_cache / kv_cache / pe_cache` 三个张量，device 0 用 `SparseSelectMlaV2`、device 1..7 用 `PureMlaV2`，但缓存布局一致。

一个关键直觉：**TileRT 的解码引擎「认位置、认缓存」，但不认是谁生产的缓存。** 只要你把正确形状的 `(ki, kv, pe)` 写进正确的 `caches` 槽位、再把 RoPE 位置游标对齐，引擎就分不清这些 KV 是它自己 prefill 出来的、还是外部送进来的——这正是 PD 分离能成立的物理基础。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tilert/pd_vllm/engine_iface.py` | 定义 `PDEngine` 协议（inject/decode/reset）与 `StubEngine`（无 GPU 回声引擎）。 |
| `tilert/pd_vllm/profiles/base.py` | `ModelProfile` 协议的 `build_engine` 方法，框架构造引擎的唯一入口。 |
| `tilert/pd_vllm/profiles/dsv32.py` / `glm5.py` | 各模型的 `engine_factory`：加载后端、构造 Generator、加载权重、包成适配器。 |
| `tilert/pd_vllm/profiles/mla_nsa.py` | `MlaNsaEngineAdapter`——把 `PDEngine` 协议翻译成 Generator 的 `inject_cache / set_cur_pos / decode_layer` 调用。 |
| `tilert/models/deepseek_v3_2/generator.py` | `inject_cache`、`set_cur_pos`、`inject_last_hidden_state` 三个「注入」方法的真实实现。 |
| `tilert/models/deepseek_v3_2/modules/end2end.py` | `_get_device_result` / `forward` 等，提供每卡的 `(intermediates, caches, params, profile_logs)` 四元组。 |
| `tilert/models/deepseek_v3_2/modules/mtp_preprocess.py` | MTP 预处理层如何消费 `last_hidden_states`。 |
| `tilert/pd_vllm/decode_server.py` | `main()` 里 `--engine stub|tilert` 的分支，以及 `/pd/decode` 如何调 `engine.inject`。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：(4.1) `PDEngine` 协议与 `StubEngine`；(4.2) `profile.build_engine` 的适配器构造；(4.3) `inject_cache` 逐层写入；(4.4) `set_cur_pos` 与 `inject_last_hidden_state`。

### 4.1 PDEngine 协议与 StubEngine：decode_server 看到的统一引擎

#### 4.1.1 概念说明

decode_server 是个「编排器」（见 u4-l4），它不想知道 DeepSeek-V3.2 和 GLM-5 在解码上有什么差别，更不想直接碰 `DSAv32Generator` 或 8 卡张量并行的细节。它只想要一个**最小契约**：给我一个能「注入外部状态、解码若干 token、用完复位」的对象。

`PDEngine` 就是这个契约，用 Python 的 `typing.Protocol` 定义。`Protocol` 的特点是**结构化类型**（鸭子类型）：任何对象只要长着 `inject / decode / reset` 三个方法就算 `PDEngine`，不需要显式继承。这让框架可以塞进两种截然不同的实现：

- **真实引擎**（`MlaNsaEngineAdapter`）：内部是加载好权重的 `DSAv32Generator`，跑在 8 张 B200 上。
- **回声引擎**（`StubEngine`）：不加载任何权重、不碰 GPU、不加载后端 `.so`，只把请求原样存下，然后吐固定 token。它的唯一用途是**联调管道**——在没有 GPU 的机器上验证 `receive → convert → inject → decode` 这条链路是否通畅。

#### 4.1.2 核心流程

decode_server 在 `/pd/decode` 里对一个 `engine` 对象的调用顺序固定为：

```text
phase 1 (prepare):
    req   = server.completed.get(...)          # 等 wire 传输完成
    conv  = profile.convert(buffer, ..., req)  # fp8 → bf16，产出 ConvertedRequest
    engine.inject(conv)                         # 把缓存灌进引擎
phase 2 (decode):
    toks  = engine.decode(first_token_id, max_tokens, sampling, on_token, cancel_event)
收尾:
    engine.reset()                              # 释放本次请求状态
```

三方法的语义边界很清晰：

| 方法 | 职责 | 是否做模型计算 |
| --- | --- | --- |
| `inject(req)` | 把引擎状态「还原成已经 prefill 了 `seq_len` 个 token」的样子 | 否（只搬数据） |
| `decode(...)` | 从首 token 开始自回归/MTP 解码，返回补全 token 列表 | 是 |
| `reset()` | 释放本次请求的临时状态 | 否 |

#### 4.1.3 源码精读

`PDEngine` 协议本身只有三个方法签名，文档字符串就是契约的全部：[tilert/pd_vllm/engine_iface.py:12-32](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/engine_iface.py#L12-L32) 定义了「注入外部 prefill 状态」「从首 token 起 AR/MTP 解码」「释放每请求状态」三件事。

`StubEngine` 是它的最小实现，关键在于它**没有任何重依赖**——构造时只记一组固定 token，`inject` 把请求对象存进 `self.injected`，`decode` 用首 token 加固定序列凑出 `max_tokens` 个并回调 `on_token`：

```python
# tilert/pd_vllm/engine_iface.py:35-55（节选）
class StubEngine:
    """Echo engine for plumbing tests: no GPU, no tilert."""
    def __init__(self, fixed_tokens=(11, 22, 33)):
        self._fixed = fixed_tokens
        self.injected = None
        self.last_stats = {}

    def inject(self, req):              # 仅存档，不碰张量
        self.injected = req

    def decode(self, first_token_id, max_tokens, sampling, on_token=None, cancel_event=None):
        out = ([int(first_token_id)] + list(self._fixed))[:max_tokens]
        if on_token:
            for t in out:
                on_token(t)             # 模拟流式回调
        self.last_stats = {"finish_reason": "stop"}
        return out

    def reset(self):
        self.injected = None
```

注意 `decode` 的返回约定：**包含 `first_token_id`、排除停止符**，`on_token` 对停止符永不触发，`last_stats['finish_reason']` 取 `stop|length|cancelled`。这套约定是 `StubEngine` 与真实适配器共享的，decode_server 不区分二者。

decode_server 在启动时按 `--engine` 选项二选一构造引擎：[tilert/pd_vllm/decode_server.py:308-323](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L308-L323) 中，`stub` 分支直接 `StubEngine()`，`tilert` 分支才调 `profile.build_engine(...)` 真正加载模型。

#### 4.1.4 代码实践

**实践目标**：在没有任何 GPU、没有加载后端 `.so` 的纯 Python 环境里，验证 `PDEngine` 三方法契约。

**操作步骤**（如下示例代码为「示例代码」，可在装了 tilert 的 CPU 机器上直接跑；`engine_iface.py` 仅依赖标准库 `collections.abc` 与 `typing`，导入不会触发后端加载）：

```python
# 示例代码：验证 StubEngine 的 PDEngine 契约
from tilert.pd_vllm.engine_iface import StubEngine, PDEngine

eng = StubEngine(fixed_tokens=(7, 8, 9))
# 1) inject 只存档，不报错
fake_req = {"seq_len": 10, "layers": [("ki", "kv", "pe")] * 10}
eng.inject(fake_req)
assert eng.injected is fake_req

# 2) decode 吐出 first_token + fixed，且尊重 max_tokens 上限
got = []
out = eng.decode(first_token_id=1, max_tokens=5, sampling=None, on_token=got.append)
assert out == [1, 7, 8, 9][:5]          # first + 3 个 fixed，截到 5（实际只有 4 个）
assert got == out                        # on_token 与返回一致
assert eng.last_stats["finish_reason"] == "stop"

# 3) reset 清空
eng.reset()
assert eng.injected is None

# 4) 结构化类型校验：StubEngine 长着 inject/decode/reset，就是 PDEngine
assert isinstance(eng, PDEngine)        # Protocol 的运行时鸭子检查
print("StubEngine 契约 OK")
```

**需要观察的现象**：`out` 的长度受 `max_tokens` 截断；`on_token` 被每个非停止 token 回调一次；`isinstance(eng, PDEngine)` 为 `True`（Protocol 支持 `runtime_checkable` 的鸭子判断）。

**预期结果**：脚本打印 `StubEngine 契约 OK`，全部断言通过。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `StubEngine` 不实现任何 `convert` / `inject_cache` 逻辑也能当 `PDEngine` 用？

**参考答案**：因为 `PDEngine` 协议只规定了 `inject/decode/reset` 三个方法；`StubEngine.inject` 只是把请求对象存档（`self.injected = req`），它根本不关心 `req` 里有没有 `layers`、是不是 `ConvertedRequest`。`convert` 是 `ModelProfile` 的职责，`PDEngine` 只接收已经 convert 好的对象，二者解耦。这正是协议分层带来的联调便利：用 `StubEngine` 时即使 `convert` 产出的是占位对象，管道结构也能跑通。

**练习 2**：`decode` 的返回「包含 `first_token_id`、排除停止符」这条约定，对 decode_server 拼响应有什么影响？

**参考答案**：decode_server 直接把 `engine.decode(...)` 的返回塞进响应的 `token_ids`（见 decode_server 的非流式分支）。由于返回已含首 token、不含停止符，路由层/客户端拿到的就是干净的「补全序列」，无需再做去停止符或补首 token 的后处理；流式分支里 `on_token` 也对停止符不触发，保证客户端不会收到结束符。

---

### 4.2 profile.build_engine：从 ModelProfile 到具体引擎适配器

#### 4.2.1 概念说明

`PDEngine` 是「decode_server 想要什么」，但「真实引擎长什么样」是模型相关的。`ModelProfile`（见 u4-l1）用一道缝把所有模型差异收口，其中 `build_engine` 就是这道缝的「engine 侧」方法：框架把权重目录、`max_seq_len`、是否 MTP、`ar_steps` 传给它，它返回一个满足 `PDEngine` 协议的具体适配器。

这样设计的收益是：**框架代码（decode_server）对 DeepSeek-V3.2 与 GLM-5 完全无感**，新增一个同族模型只需要在 profile 里登记一个 `engine_factory`，不改框架。

#### 4.2.2 核心流程

构造真实引擎的链路是三层：

```text
decode_server.main(--engine tilert)
   └─ profile.build_engine(weights, max_seq_len, with_mtp, ar_steps)        # base.py 协议方法
        └─ MlaNsaProfile.build_engine → self._engine_factory(...)            # mla_nsa.py 委托给工厂
             └─ _build_engine(...):                                          # dsv32.py / glm5.py
                  1. tilert.load_backend("deepseek_v3_2" / "glm5")           # 单进程单后端（见 u1-l3）
                  2. gen = DSAv32Generator(...)/GLM5Generator(...)           # 构造 Generator
                  3. gen.from_pretrained()                                   # 加载 8 卡分片权重
                  4. return MlaNsaEngineAdapter(gen, with_mtp)               # 包成 PDEngine 适配器
```

关键点：`build_engine` 是**重操作**——它会加载后端 `.so`、把 61 层权重分到 8 卡、捕获 CUDA Graph（见 u2-l3 的 `prepare_money`）。所以 decode_server 启动时只调一次，之后整个进程生命周期复用同一个引擎实例。

#### 4.2.3 源码精读

`ModelProfile` 协议里 `build_engine` 只是一行声明：[tilert/pd_vllm/profiles/base.py:62-65](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/base.py#L62-L65) 声明「构造解码引擎适配器（inject/decode/reset）」。具体实现委托给各 profile 的工厂函数。

DeepSeek-V3.2 的工厂函数完整展示了「加载后端 → 构造 Generator → 加载权重 → 包适配器」四步：[tilert/pd_vllm/profiles/dsv32.py:15-31](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/dsv32.py#L15-L31)。其中 `hasattr(tilert, "load_backend")` 的判断是为了兼容「多后端构建（有 `load_backend`）」与「单后端构建（import 时自动注册、无该方法）」两种发布形态。GLM-5 的工厂几乎一模一样，只是换成 `GLM5Generator` 与 `glm5` 后端：[tilert/pd_vllm/profiles/glm5.py:20-39](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/glm5.py#L20-L39)。

两个 profile 在注册时只钉三个常量——层数、wire 版本、工厂：[tilert/pd_vllm/profiles/dsv32.py:34-41](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/dsv32.py#L34-L41)（`NUM_LAYERS=62`，`LAYOUT_VERSION=11`）与 [tilert/pd_vllm/profiles/glm5.py:42-49](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/glm5.py#L42-L49)（`NUM_LAYERS=79`，`LAYOUT_VERSION=10`）。其中 `NUM_LAYERS` 含义是「主模型层 + 1 个 MTP draft 层」（dsv32: 61 主 + 1 draft；glm5: 78 主 + 1 draft），这个数同时决定了接收缓冲大小、`convert` 输出层数、`inject_cache` 写入层数——三处必须一致。

`MlaNsaProfile.build_engine` 只是把调用转给工厂：[tilert/pd_vllm/profiles/mla_nsa.py:344-345](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L344-L345)。真正的「协议翻译」在它返回的 `MlaNsaEngineAdapter` 里，这是下一节的主角。

#### 4.2.4 代码实践

**实践目标**：确认两个模型的 `engine_factory` 共享同一个适配器类，差异只在 Generator 与常量。

**操作步骤**：对照 [dsv32.py:15-31](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/dsv32.py#L15-L31) 与 [glm5.py:20-39](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/glm5.py#L20-L39)，列出一张差异表。

**需要观察的现象**：两个工厂函数的差异只在三处——(1) `load_backend` 的 model_type；(2) Generator 类（`DSAv32Generator` vs `GLM5Generator`）与对应 `ModelArgs`；(3) GLM-5 多传一个 `enable_thinking=False`。其余（`max_new_tokens` 公式、`use_topp=True`、`from_pretrained()`、`MlaNsaEngineAdapter(gen, with_mtp)`）完全相同。

**预期结果**：差异表如下——

| 维度 | dsv32 工厂 | glm5 工厂 |
| --- | --- | --- |
| 后端 | `load_backend("deepseek_v3_2")` | `load_backend("glm5")` |
| Generator | `DSAv32Generator(ModelArgs())` | `GLM5Generator(ModelArgsGLM5())` |
| 额外参数 | 无 | `enable_thinking=False` |
| 适配器 | `MlaNsaEngineAdapter(gen, with_mtp)` | 同左 |

这印证了 `MlaNsaEngineAdapter` 的设计前提（见其类 docstring）：两个 Generator 都暴露 `inject_cache / set_cur_pos / decode_layer`，且共享 `TOKEN_OUT` 等下标，所以同一适配器能驱动两个模型。

#### 4.2.5 小练习与答案

**练习**：如果新增一个同族模型（比如某 DeepSeek 变体），需要改 decode_server 的代码吗？

**参考答案**：不需要。只需要新写一个 profile 文件，定义自己的 `engine_factory`（构造对应 Generator 并 `from_pretrained`），然后调用 `base.register(MlaNsaProfile(name=..., num_layers=..., layout_version=..., engine_factory=...))`，并在 `_ALIASES` 里加一个别名即可（见 [base.py:68-97](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/base.py#L68-L97)）。decode_server 通过 `--model <name>` 选 profile，对框架代码零改动。

---

### 4.3 inject_cache：把外部 prefill 的 (ki, kv, pe) 逐层写入 caches

#### 4.3.1 概念说明

`inject_cache` 是 PD 分离的物理核心：它把 `convert` 阶段产出的、逐层的 `(ki, kv, pe)` 三元组，写进 TileRT 解码引擎自己的 `caches` 列表。写完之后，引擎的 KV 缓存里就「已经有了」前 `seq_len` 个 token 的历史，后续 decode 步只需喂数量极少的 token 即可继续生成，从而把 prefill 的算力开销彻底甩给 vLLM 节点。

它要解决两个对齐问题：

1. **层对齐**：外部送来的 `layer_caches` 是「每层一个三元组」的列表；引擎内部的 `caches` 是一个扁平张量列表。必须找到「第 L 层的 ki/kv/pe 对应扁平列表的哪几个下标」。
2. **卡对齐**：MLA 的潜 KV 在张量并行间是**复制**的（见 u2-l6、u4-l1 的 `sender_ranks={0}`），所以 8 张卡的缓存内容相同——`inject_cache` 要把同一份 `(ki, kv, pe)` 复制到全部 8 卡。

#### 4.3.2 核心流程

```text
输入: layer_caches = [(ki[L,128], kv[L,512], pe[L,64])] × num_layers   # BF16，convert 产出
      start_pos, end_pos                                                 # 写入窗口

for device_id in range(8):                       # MLA KV 复制 → 每卡都写
    _, caches, _, _ = decode_layer._get_device_result(device_id)
    for layer_id, (ki, kv, pe) in enumerate(layer_caches):
        base_idx = layer_id * 3                  # 第 L 层占 caches[L*3 .. L*3+2]
        caches[base_idx + 0][0, start_pos:end_pos, :].copy_(ki)   # ki_cache
        caches[base_idx + 1][0, start_pos:end_pos, :].copy_(kv)   # kv_cache
        caches[base_idx + 2][0, start_pos:end_pos, :].copy_(pe)   # pe_cache
```

`base_idx = layer_id * 3` 这个公式源于 MLA 的缓存布局：每层 MLA 的 `get_cache_vars()` 返回 `[ki_cache, kv_cache, pe_cache]` 三个张量（见 [mla_v2.py:94-113](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mla_v2.py#L94-L113) 与 [mla_v2.py:229-248](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mla_v2.py#L229-L248)），`Dsa` 容器递归 `extend` 聚合后，扁平 `caches` 就是按层顺序、每层 3 个排列。因此第 `layer_id` 层的三个缓存恰好落在 `layer_id*3`、`layer_id*3+1`、`layer_id*3+2`。

#### 4.3.3 源码精读

`inject_cache` 的签名与文档说明了对张量形状的严格要求——ki/kv/pe 末维分别是 128/512/64、BF16：[tilert/models/deepseek_v3_2/generator.py:419-450](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L419-L450)。

核心写入循环是「外层遍历 8 卡、内层遍历 num_layers」的双 for：[tilert/models/deepseek_v3_2/generator.py:464-484](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L464-L484)。关键几行：

```python
# generator.py:466-482（节选）
for device_id in range(num_devices):
    _, caches, _, _ = self.decode_layer._get_device_result(device_id)
    for layer_id, (ki, kv, pe) in enumerate(layer_caches):
        if layer_id >= num_layers:
            logger.warning(f"Layer index {layer_id} is out of bounds, skipping.")
            break
        base_idx = layer_id * 3
        ki_src = ki[:cache_len].to(f"cuda:{device_id}")
        kv_src = kv[:cache_len].to(f"cuda:{device_id}")
        pe_src = pe[:cache_len].to(f"cuda:{device_id}")
        caches[base_idx + 0][0, start_pos:end_pos, :].copy_(ki_src)   # ki_cache
        caches[base_idx + 1][0, start_pos:end_pos, :].copy_(kv_src)   # kv_cache
        caches[base_idx + 2][0, start_pos:end_pos, :].copy_(pe_src)   # pe_cache
```

写入顺序 ki→kv→pe 与 mla_v2.py 的 `[ki_cache, kv_cache, pe_cache]` 布局一一对应（[generator.py:480-482](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L480-L482)）。

要点拆解：

- **跨卡复制**：`num_devices` 硬编码为 8（见 [end2end.py:188](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L188) 的 `self.num_devices = 8`）。因为 MLA 潜 KV 在 TP 间复制，8 卡内容相同，所以每卡都 copy 同一份源张量（`.to(f"cuda:{device_id}")` 把源搬到目标卡）。
- **写入窗口 `[0, start_pos:end_pos, :]`**：缓存张量的形状是 `[batch=1, max_seq_len+pad, dim]`（见 mla_v2.py 的 `torch.zeros(...)`）。`start_pos/end_pos` 决定写哪一段，默认 `start_pos=0`、`end_pos=start_pos+seqlen`，即从头写 `seqlen` 个 token。这留出了「分段注入」的能力（例如把一个长 prompt 分多次注入）。
- **层数必须对齐**：循环用 `layer_id >= num_layers` 做越界保护。`num_layers` 来自 `len(layer_caches)`，而 `caches` 列表长度来自引擎构造时 `Dsa` + MTP 的层数。二者必须相等——这正是 4.2 节 `NUM_LAYERS`（含 MTP draft 层）同时被 prefill 与 decode 两端引用的原因。

#### 4.3.4 代码实践

**实践目标**：用一组随机张量模拟「注入 3 层、每层 (ki,kv,pe)」的过程，验证 `base_idx = layer_id * 3` 的索引映射。

**操作步骤**（示例代码，可在 CPU 上运行；不依赖真实引擎，只用一个伪 `caches` 列表模拟布局）：

```python
# 示例代码：模拟 inject_cache 的索引映射（不连真实引擎）
import torch

num_layers, seq_len, num_devices = 3, 5, 8
# 模拟引擎侧的扁平 caches：每层 3 个 [1, max_seq, dim]
dims = {"ki": 128, "kv": 512, "pe": 64}
caches = {
    d: [
        torch.zeros(1, seq_len, dims[d], dtype=torch.bfloat16)
        for _ in range(num_layers)
    ]
    for d in dims
}
flat = []  # 按 [ki, kv, pe] × num_layers 平铺，模拟 Dsa 聚合结果
for lid in range(num_layers):
    flat += [caches["ki"][lid], caches["kv"][lid], caches["pe"][lid]]

# 模拟外部 layer_caches
layer_caches = [
    (torch.randn(seq_len, 128), torch.randn(seq_len, 512), torch.randn(seq_len, 64))
    for _ in range(num_layers)
]

for lid, (ki, kv, pe) in enumerate(layer_caches):
    base = lid * 3
    assert flat[base] is caches["ki"][lid]      # 第 0 槽 = ki
    assert flat[base + 1] is caches["kv"][lid]   # 第 1 槽 = kv
    assert flat[base + 2] is caches["pe"][lid]   # 第 2 槽 = pe
print("base_idx = layer_id * 3 映射正确")
```

**需要观察的现象**：`flat[base+0/1/2]` 恰好是第 `lid` 层的 ki/kv/pe 缓存对象，证明「每层 3 个、按层顺序」的扁平布局与 `base_idx = layer_id*3` 公式自洽。

**预期结果**：打印 `base_idx = layer_id * 3 映射正确`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `inject_cache` 要在 8 张卡上各写一份相同的 `(ki, kv, pe)`，而不是只写 device 0？

**参考答案**：因为 MLA 的潜 KV（`kv_lora_rank=512`）在张量并行间是**复制**而非切分的——每张卡都持有完整的潜 KV 副本（这与普通 MHA 把 KV 按头切分不同）。prefill 侧也因此只让 rank 0 发送（`sender_ranks={0}`，见 u4-l1/u4-l2），省掉 7/8 的跨节点带宽。到了 decode 侧注入时，必须把这一份复制回所有 8 卡，否则其余卡解码时读到的 KV 是空的。注意：这说的是潜 KV 的 ki/kv/pe；NSA 的 KI 索引虽由 device 0 独自选择，但缓存布局在两类 MLA 里一致。

**练习 2**：如果 `layer_caches` 的层数比引擎 `caches` 实际能容纳的层数多 1，会发生什么？

**参考答案**：循环里的 `if layer_id >= num_layers: break` 会触发并打印 `Layer index ... is out of bounds, skipping.` 警告（[generator.py:470-472](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L470-L472)），多余的层被跳过。但更危险的反向情况——`layer_caches` 比引擎层数少——不会报错，却会让后面几层缓存保持初始的零值，导致解码时注意力算到全零 key，输出乱码。所以两端层数对齐是隐性 correctness 约束，靠 `NUM_LAYERS` 常量在 profile 层统一。

---

### 4.4 set_cur_pos 与 inject_last_hidden_state：RoPE 游标同步与 MTP draft 头喂数据

#### 4.4.1 概念说明

注入缓存只完成了「KV 历史就位」，还差两件事引擎才能正确继续解码：

1. **RoPE 位置游标**（`set_cur_pos`）：旋转位置编码 RoPE 对每个 token 按其**绝对位置**施加旋转。注入了前 `seq_len` 个 token 的 KV，但引擎内部记录「当前解码到第几个位置」的游标还停在 0。不修正它，新 token 会被当成位置 0 旋转，与缓存里按真实位置旋转的 key 对不上，注意力全错。
2. **MTP draft 头的隐状态输入**（`inject_last_hidden_state`）：MTP 预处理层（`MTPPreprocessLayer`）在生成 draft token 时，需要**主模型最后一个 token 的隐状态**作为输入之一（与当前 embedding 拼接后投影，见 [mtp_preprocess.py:197-229](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mtp_preprocess.py#L197-L229)）。在本地生成时这个隐状态由主模型上一轮 forward 顺带产出；在 PD 分离里，主模型 prefill 发生在另一台机器，这个隐状态需要单独送过来注入。

#### 4.4.2 核心流程

**set_cur_pos 的两条路径**（取决于是否 MTP）：

```text
非 MTP:
    torch.ops.tilert.dsa_show_hands_set_cur_pos(cur_pos)     # 直接调 C++ 算子改游标

MTP (with_mtp=True):
    for device_id in range(8):
        intermediates[Idx.CUR_POS].fill_(cur_pos)            # 改写 temp_vars[31] 这个标量槽
```

**inject_last_hidden_state**：

```text
若 with_mtp == False: 告警并跳过（MTP 专属）
否则:
    for device_id in range(8):
        intermediates[Idx.LAST_HIDDEN_STATES][0, 0, :].copy_(last_hidden.to(该卡))
```

**适配器如何串起来**（`MlaNsaEngineAdapter.inject`）：

```text
engine.inject(req):
    gen.inject_cache(req.layers, start_pos=0)     # 4.3 节：写 KV
    gen.set_cur_pos(req.seq_len - 1)              # 4.4 节：对齐 RoPE 游标到「最后一个 prefill token」
    self._last_prompt_token = req.last_prompt_token   # MTP 首 draft 的种子（token id）
```

注意一个**精度要点**：`set_cur_pos` 传的是 `seq_len - 1` 而非 `seq_len`。这是因为 PD 解码的第一步要「重新消费最后一个 prefill token」（用它作 draft 种子），该 token 的绝对位置是 `seq_len - 1`（0 基）。这与本地 MTP 生成里的 `self.set_cur_pos(prompt_len - 1)`（[generator.py:331](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L331)）完全一致；`inject_cache` 文档示例里写的 `set_cur_pos(seqlen)` 是口语化表述，实际代码统一用 `len - 1`。

#### 4.4.3 源码精读

**set_cur_pos** 按是否 MTP 分两条路径：[tilert/models/deepseek_v3_2/generator.py:486-508](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L486-L508)。

```python
# generator.py:501-508（节选）
if self.with_mtp:
    num_devices = self.decode_layer.num_devices
    for device_id in range(num_devices):
        intermediates, _, _, _ = self.decode_layer._get_device_result(device_id)
        cur_pos_tensor = intermediates[Idx.CUR_POS]      # temp_vars[31]
        cur_pos_tensor.fill_(cur_pos)
else:
    torch.ops.tilert.dsa_show_hands_set_cur_pos(cur_pos)  # C++ 算子直改
```

MTP 模式下，`CUR_POS`（`Idx.CUR_POS = 31`，见 [temp_var_indices.py:49](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/temp_var_indices.py#L49)）是 `temp_vars` 里的一个标量槽，`fill_` 直接覆盖它，8 卡各改一份。为什么 MTP 要走 `temp_vars` 而 non-MTP 走独立算子？因为 MTP 模式捕获了**两张** CUDA Graph（完整图 + 主模型子图，见 u2-l3），两张图共享同一份 `temp_vars` 视图，改 `Idx.CUR_POS` 一处即对两图同时生效；non-MTP 只有单图，直接用专用算子更直接。

**inject_last_hidden_state** 把外部送来的末 token 隐状态写进 `LAST_HIDDEN_STATES` 槽：[tilert/models/deepseek_v3_2/generator.py:510-539](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L510-L539)。

```python
# generator.py:525-537（节选）
if not self.with_mtp:
    logger.warning("inject_last_hidden_state called but with_mtp is False, skipping")
    return
if last_hidden_state.dim() == 1:
    last_hidden_state = last_hidden_state.unsqueeze(0)
num_devices = self.decode_layer.num_devices
for device_id in range(num_devices):
    intermediates, _, _, _ = self.decode_layer._get_device_result(device_id)
    lhs_tensor = intermediates[Idx.LAST_HIDDEN_STATES]   # temp_vars[33]
    lhs_src = last_hidden_state.to(f"cuda:{device_id}")
    lhs_tensor[0, 0, :].copy_(lhs_src.squeeze(0))
```

`LAST_HIDDEN_STATES`（`Idx.LAST_HIDDEN_STATES = 33`，见 [temp_var_indices.py:51](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/temp_var_indices.py#L51)）正是 `MTPPreprocessLayer` 的输入槽。该层的参考实现展示了它如何把「当前 embedding 的 RMSNorm」与「上一隐状态的 RMSNorm」拼接后投影：[mtp_preprocess.py:216-229](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mtp_preprocess.py#L216-L229)。

**适配器 wiring**——这是把上面两个方法接入 PD 管道的关键一处：[tilert/pd_vllm/profiles/mla_nsa.py:377-381](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L377-L381)。

```python
# mla_nsa.py:377-381
def inject(self, req) -> None:
    self.gen.inject_cache(req.layers, start_pos=0)
    self.gen.set_cur_pos(req.seq_len - 1)
    self._last_prompt_token = req.last_prompt_token
    self._seq_len = req.seq_len
```

需要如实指出的一点：**当前发布的 `MlaNsaEngineAdapter.inject()` 只调了 `inject_cache` 与 `set_cur_pos`，并没有调 `inject_last_hidden_state`。** 它的 MTP 首 draft 种子用的是 `req.last_prompt_token`（一个 token id 整数），由 `_decode_mtp` 把它填进 draft 张量（见 [mla_nsa.py:415](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L415)）。也就是说，`inject_last_hidden_state` 是 Generator 暴露出来的、用于「向 MTP draft 头喂入主模型末 token 隐状态」的接口与扩展点（GLM5Generator 上有镜像实现，见 `tilert/models/glm_5/generator.py:497`），但当前共享适配器走的是 token-id 种子路径。阅读时应区分「Generator 提供的能力」与「适配器当前选择的策略」。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：①讲清「注入缓存后必须 set_cur_pos」的 RoPE 原理；②用 `StubEngine` 设计一个无需真实 GPU 的端到端联调脚本，验证 `receive → convert → inject → decode` 管道结构。

**第一部分：为什么必须 set_cur_pos（源码阅读 + 推理型实践）**

RoPE 对位置 \(m\) 的查询/键施加旋转矩阵 \(R(m)\)。查询在位置 \(i\)、缓存里的键在位置 \(j\) 时，注意力内积依赖：

\[
(q_i)^\top k_j = (R(i)\,q)^\top (R(j)\,k) = q^\top R(i)^\top R(j)\,k = q^\top R(i-j)\,k
\]

即注意力只依赖**相对位置** \(i-j\)。这个良好性质成立的前提是：缓存里位置 \(j\) 的键当时是用 \(R(j)\) 旋转后写入的，而新查询用 \(R(i)\) 旋转——两者用的是同一套绝对位置标尺。

PD 注入破坏了这个前提的「入口」：你把 prefill 在位置 \(0..seq\_len{-}1\) 旋转好的 KV 写进了缓存，但引擎里「当前要给新 token 旋转到哪个位置」的游标 `CUR_POS` 还是 0。于是新 token（本应在位置 \(seq\_len\)）被错误地用 \(R(0)\) 旋转，与缓存键的相对关系整体错位 \(seq\_len\)，注意力彻底失配。

`set_cur_pos(seq_len - 1)` 的作用就是把游标拨到「最后一个 prefill token 的位置」，让随后的解码从正确位置继续旋转。对照 [generator.py:501-508](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L501-L508)：

- 请用一句话写出：若忘了调 `set_cur_pos`，新 token 的旋转位置会是几？答：**0**（游标默认值）。
- 请推断：此时注意力相对位置 \(i-j\) 会被错算成什么？答：本应为 \(seq\_len - j\)，实际算成 \(0 - j = -j\)，整体偏移 \(seq\_len\)。

**第二部分：StubEngine 端到端联调脚本（示例代码）**

下面脚本不连真实引擎、不发 RDMA，直接用一个**伪 ConvertedRequest** 驱动 `StubEngine`，复现 decode_server 的 `inject → decode → reset` 调用顺序，验证管道结构与四段计时逻辑。可在任意装了 tilert 的 CPU 机器上运行：

```python
# 示例代码：用 StubEngine 复现 /pd/decode 的 prepare+decode 编排（无 GPU）
import time
from tilert.pd_vllm.engine_iface import StubEngine
from tilert.pd_vllm.profiles.mla_nsa import ConvertedRequest

# 1) 模拟 receive→convert 之后的产物：num_layers 个 (ki,kv,pe) 占位三元组
NUM_LAYERS = 62
seq_len = 10
fake_layers = [(f"ki{L}", f"kv{L}", f"pe{L}") for L in range(NUM_LAYERS)]
conv = ConvertedRequest(
    rid="req-1", seq_len=seq_len, last_prompt_token=12345,
    first_token_id=12346, sampling={"temperature": 0.7, "top_p": 0.95},
    layers=fake_layers,
)

# 2) 起一个 StubEngine（对应 decode_server --engine stub）
eng = StubEngine()

# 3) 复刻 decode_server 的两阶段（见 decode_server.py:128-160 的四段计时）
t0 = time.time()
eng.inject(conv)                       # phase 1: inject（StubEngine 仅存档）
t_inj = time.time()
tokens = eng.decode(                   # phase 2: decode
    first_token_id=conv.first_token_id,
    max_tokens=20, sampling=conv.sampling,
)
timing = {
    "wire_wait": 0.0, "convert": 0.0,
    "inject": round(1000 * (t_inj - t0), 1),
    "decode": round(1000 * (time.time() - t_inj), 1),
    **eng.last_stats,
}
eng.reset()                            # 收尾：释放
print("tokens:", tokens)
print("timing_ms:", timing)
assert timing["finish_reason"] == "stop"
assert tokens[0] == conv.first_token_id
```

**需要观察的现象**：`tokens` 以 `first_token_id` 开头、后接 `StubEngine` 的固定序列并截到 `max_tokens`；`timing_ms` 里出现 `inject / decode / finish_reason` 三个键——这与真实 decode_server 响应里的 `timing_ms` 结构同构。

**预期结果**：脚本打印类似 `tokens: [12346, 11, 22, 33]`、`timing_ms: {'wire_wait': 0.0, 'convert': 0.0, 'inject': 0.0, 'decode': 0.0, 'finish_reason': 'stop'}`。

> 想进一步联调真实 decode_server（含 ReceiveServer 控制平面）时，可执行 `python -m tilert.pd_vllm.decode_server --engine stub --model glm5 --max-seq-len 4096`。但请注意：在没有 prefill 发送端向控制平面推 KV 时，`/pd/decode` 会一直等到 `timeout_s` 后返回 504（`kv_transfer_timeout`）。这一超时行为本身就是「receive 阶段在等 wire」的正常表现，可作为端口可达性的佐证。完整 receive→convert 链路的真实数据流需配合 vLLM prefill 节点，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`set_cur_pos` 在 MTP 模式下为什么遍历 8 卡改 `temp_vars[Idx.CUR_POS]`，而不像非 MTP 那样调一个 C++ 算子？

**参考答案**：MTP 模式捕获了两张 CUDA Graph（完整图 + 主模型子图），它们共享同一份 `temp_vars` 视图（见 u2-l3/u3-l4）。`Idx.CUR_POS` 是 `temp_vars` 里的标量槽，改它一次即可对两张图同时生效，且 `fill_` 是回放安全的原地操作（不改变图里录的 kernel）。非 MTP 只有一张图，用专用算子 `dsa_show_hands_set_cur_pos` 更直接。两者目的相同：把 RoPE 游标拨到正确位置。

**练习 2**：`inject_last_hidden_state` 在 `with_mtp=False` 时会怎样？为什么？

**参考答案**：会打印告警 `inject_last_hidden_state called but with_mtp is False, skipping` 并直接 return（[generator.py:525-527](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L525-L527)）。因为 `LAST_HIDDEN_STATES` 槽只被 MTP 预处理层消费，非 MTP 解码路径根本不读这个槽，注入它毫无意义。这是一个防御性 guard，避免调用方误用。

**练习 3**：当前 `MlaNsaEngineAdapter.inject()` 是否调用了 `inject_last_hidden_state`？如果不调，MTP 的首 draft 是怎么起的？

**参考答案**：没有调用。它只调 `inject_cache` 与 `set_cur_pos`，并把 `req.last_prompt_token` 存为 `_last_prompt_token`。MTP 首 draft 在 `_decode_mtp` 里用这个 token id 填充 draft 张量作为种子（[mla_nsa.py:415](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L415)）。`inject_last_hidden_state` 是 Generator 暴露的隐状态注入接口，留作更完整的 draft 头喂数据路径使用。

## 5. 综合实践

把本讲四个模块串起来，设计一个「引擎适配器探针」任务：

**任务**：阅读 `MlaNsaEngineAdapter`（[mla_nsa.py:348-486](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L348-L486)），画一张「一次 `/pd/decode` 请求在引擎内部的数据流图」，要求标注：

1. `inject(conv)` 调了哪些 Generator 方法、各写入 `caches` / `temp_vars` 的哪些槽（`ki/kv/pe`、`CUR_POS`）。
2. `decode(...)` 根据 `temperature` 决定走 `_decode_standard` 还是 `_decode_mtp`（提示：看 [mla_nsa.py:383-401](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L383-L401) 里 `temp < 1e-5` 的分支），两条路径分别从哪个 `Idx` 槽读 token（标准路径读 `TOKEN_OUT`，见 [mla_nsa.py:472](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L472)；MTP 路径读 `get_predicted_tokens` / `get_num_accepted`）。
3. `reset()` 做了什么（注意它是 `pass`，结合 u4-l4 的 `_cleanup()` 解释为何真实复位由 `decode_layer.reset_sequence()` 在 decode 内部完成）。

**进阶**：把 4.4.4 的 StubEngine 脚本扩展成一个「假适配器」——继承结构上满足 `PDEngine`，但 `inject` 里打印 `req.seq_len` 与 `len(req.layers)`，`decode` 里打印每步 token。用它驱动一遍脚本，观察「层数 = NUM_LAYERS、seq_len 与 layers 一致」的不变量是否在你的伪数据里成立。

**预期产出**：一张包含 `caches`（ki/kv/pe 三平面）、`temp_vars`（CUR_POS / LAST_HIDDEN_STATES / TOKEN_OUT 等槽）、以及 `inject → decode → reset` 三阶段箭头的草图；以及一段能跑通、且打印出不变量校验结果的脚本。

## 6. 本讲小结

- `PDEngine`（inject/decode/reset）是 decode_server 与引擎之间的唯一契约，`Protocol` 结构化类型让 `StubEngine`（无 GPU 回声引擎）与 `MlaNsaEngineAdapter`（真实 8 卡引擎）可互换，支撑无 GPU 联调。
- `profile.build_engine` 是模型差异的收口点：工厂函数依次「加载后端 → 构造 Generator → `from_pretrained` 加载 8 卡权重 → 包成 `MlaNsaEngineAdapter`」，decode_server 对此无感。
- `inject_cache` 用 `base_idx = layer_id * 3` 把外部 `(ki, kv, pe)` 逐层写入扁平 `caches`，并在 8 卡上各复制一份（因 MLA 潜 KV 在 TP 间复制）；`start_pos/end_pos` 支持分段注入。
- 注入缓存后**必须** `set_cur_pos(seq_len - 1)` 同步 RoPE 游标，否则新 token 被当作位置 0 旋转、相对位置整体错位；MTP 走 `temp_vars[Idx.CUR_POS]`、非 MTP 走专用 C++ 算子。
- `inject_last_hidden_state` 把外部 prefill 的末 token 隐状态写进 `temp_vars[Idx.LAST_HIDDEN_STATES]`，供 MTP 预处理层消费；当前适配器的 inject 只串了 `inject_cache + set_cur_pos`，首 draft 用 token-id 种子，隐状态注入是 Generator 暴露的扩展点。
- 「引擎认位置、认缓存，但不认缓存的来源」是 PD 分离能成立的物理基础——只要槽位与游标对齐，外部 KV 与本地 prefill 的 KV 对引擎不可区分。

## 7. 下一步学习建议

本讲是 PD 分离系列（单元 4）的最后一篇，至此 receive → wire → convert → inject → decode 的完整链路已讲透。后续建议：

1. **横向对照 GLM5Generator 的注入实现**：阅读 `tilert/models/glm_5/generator.py` 的 `inject_cache / set_cur_pos / inject_last_hidden_state`，确认它们与 DeepSeek 版本同构，加深「一套适配器驱动两个模型」的理解。
2. **回到单元 3 复盘解码循环**：带着本讲对 `Idx.TOKEN_OUT`、`CUR_POS`、`reset_sequence` 的认识重读 u3-l2（非 MTP 主循环）与 u3-l3（MTP 投机解码），你会看到 PD 注入与本地生成在读同一套 temp_vars 槽位。
3. **端到端跑通 Topology A**：在有 8× B200 与 vLLM prefill 的环境里，按 README 的命令真正起一次 decode_server（`--engine tilert`），对照 decode_server 日志里的 `REQSTAT` 行（含 `wire_wait/convert/inject/decode` 四段计时）验证本讲的时序模型。
4. **若要做二次开发**：考虑实现一个调用 `inject_last_hidden_state` 的 engine_factory，让 MTP draft 头直接消费 prefill 末 token 隐状态，并与当前 token-id 种子路径做接受长度（`mtp_accept_mean`）的对比。
