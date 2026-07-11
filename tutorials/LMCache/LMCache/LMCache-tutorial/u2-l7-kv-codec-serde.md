# KV 编解码与 SERDE

## 1. 本讲目标

在 [u2-l3（存储后端层次结构）](u2-l3-storage-backend-hierarchy.md) 里我们提到：`RemoteBackend` 在「写之前序列化、读之后反序列化」，并且故障一律降级为未命中。当时我们把序列化当成黑盒一带而过。本讲就打开这个黑盒。

学完本讲，你应该能够：

- 说清 **SERDE（Serialization / Deserialization，序列化与反序列化）** 在 LMCache 的 store/retrieve 主链路里处于什么位置、为什么需要它；
- 讲明白 legacy **CacheGen** serde 的两步压缩思路：先把浮点 KV **量化（quantization）** 成整数，再用**算术熵编码（arithmetic entropy coding）** 进一步压成短字节流；
- 读懂 v1 **`AsymK16V8Codec`（不对称 K16/V8）** 编解码：为什么 K 不量化、只把 V 压成 FP8，以及它如何用「自描述头部 + 哈希门禁 + CRC」保证跨模型/跨后端不串味；
- 对比这两套编码路径，独立列出「原始 KV → 编码 → 存储 → 解码」各阶段的输入输出。

## 2. 前置知识

- **KV cache 的物理形态**：在引擎里它是 `[num_layers, 2, num_tokens, num_heads, head_size]` 的浮点张量（第 1 维的 `2` 表示 Key 和 Value 拼在一起），dtype 通常是 `fp16` 或 `bf16`。详见 [u1-l1](u1-l1-project-overview.md)。
- **MemoryObj**：LMCache 内部存放一块 KV 的连续内存对象（`KV_2LTD` 布局），是引擎分页 KV 与存储层之间的统一中间表示。详见 [u2-l2（GPU 连接器层）](u2-l2-gpu-connector-layer.md)。
- **三层存储后端**：`LocalCPUBackend`（热缓存 + 内存分配器）、`LocalDiskBackend`（落盘）、`RemoteBackend`（跨实例共享）。详见 [u2-l3](u2-l3-storage-backend-hierarchy.md)。
- **几个术语**：
  - **量化（quantization）**：把高精度浮点（如 fp16，每个元素 2 字节）映射到低精度表示（如 int8 / FP8，每元素 1 字节），用牺牲一点点精度换一半的存储与带宽。
  - **熵编码（entropy coding）**：出现频率高的符号用短码、频率低的用长码，从而把一段符号流压成更短的字节流（算术编码是其中一种）。
  - **FP8 e4m3fn**：一种 8 位浮点格式（1 位符号 + 4 位指数 + 3 位尾数），是现代 GPU 上 KV 量化的常见目标。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|---|---|
| [lmcache/storage_backend/serde/serde.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/serde.py) | legacy SERDE 的抽象基类 `Serializer` / `Deserializer`，定义 `to_bytes` / `from_bytes` 契约。 |
| [lmcache/storage_backend/serde/cachegen_encoder.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_encoder.py) | legacy CacheGen **编码器**：量化 + 算术熵编码，产出压缩字节流。 |
| [lmcache/storage_backend/serde/cachegen_decoder.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_decoder.py) | legacy CacheGen **解码器**：熵解码 + 反量化，还原 KV 张量。 |
| [lmcache/storage_backend/serde/cachegen_basics.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_basics.py) | CacheGen 的配置（每层量化档位 `bins`）与编码产出的数据结构。 |
| [lmcache/v1/kv_codec/asym_k16_v8.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/asym_k16_v8.py) | v1 **不对称 K16/V8 编解码器** `AsymK16V8Codec`：K 原样、V 压成 FP8。 |
| [lmcache/v1/kv_codec/encoded_kv.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/encoded_kv.py) | v1 编码产物的**自描述头部**格式：magic、dtype、scale、哈希门禁、CRC32。 |
| [lmcache/v1/storage_backend/remote_backend.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/remote_backend.py) | `RemoteBackend`：SERDE 在 store/retrieve 主链路中的实际挂载点。 |

> 提示：legacy `lmcache/storage_backend/serde/` 是原始实现；v1 还有一份几乎同构的镜像在 `lmcache/v1/storage_backend/naive_serde/`，被 v1 的 `RemoteBackend` 实际 import（见 [naive_serde/__init__.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/naive_serde/__init__.py)）。本讲以 legacy 版本为「标准教科书实现」来讲解，因为它结构最清晰。

## 4. 核心概念与源码讲解

本讲拆成三个递进的小节：先讲 SERDE 的抽象契约和它挂在哪里；再深入 legacy CacheGen 的两步压缩；最后讲 v1 的不对称编解码与自描述格式。

### 4.1 SERDE 抽象契约与其在存储链路中的位置

#### 4.1.1 概念说明

**SERDE = Serialization / Deserialization**，即「把内存里的 KV 张量变成一段字节流」和「把字节流还原回张量」。

为什么要做这一步？因为 `LocalCPUBackend` 直接持有 `MemoryObj`（就是一块内存），无需转换；但一旦数据要离开本进程——写到磁盘、发给远端实例——就必须变成一段**与设备无关、与进程无关的字节**。此外，KV cache 体积很大（一条 8K 请求约 1 GiB），直接搬字节既慢又占带宽，所以 SERDE 通常还会顺手做**压缩**。

因此 SERDE 在 LMCache 里的定位是：**「离开本机内存之前的最后一道变换」**。它只服务于 `LocalDiskBackend` 与 `RemoteBackend`，本地热缓存不经过它。

#### 4.1.2 核心流程

legacy SERDE 的抽象极其简单，只有两个方法：

```
Serializer:      torch.Tensor  ──to_bytes──►  bytes        （把张量连同 shape/dtype 一起打包成字节）
Deserializer:    bytes         ──from_bytes──► torch.Tensor （从字节还原出张量，dtype 由构造时指定）
```

它被挂在 `RemoteBackend` 的 put/get 路径上：

```
store 路径：
  engine.store ─► StorageManager.batched_put ─► RemoteBackend._put
                                                   │
                                                   ▼
                                    memory_obj ──serializer.serialize──► compressed bytes
                                                   │
                                                   ▼
                                          RemoteConnector 把字节发到远端

retrieve 路径：
  远端字节 ─► RemoteConnector ─► memory_obj(bytes) ──deserializer.deserialize──► KV 张量 ─► 回填 LocalCPU
```

注意：编码（serialize）在**离开本地之前**做，解码（deserialize）在**到达本地之后**做——两端各一次，对称。

#### 4.1.3 源码精读

抽象基类定义在 [serde.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/serde.py)。`Serializer` 只有一个抽象方法：

[lmcache/storage_backend/serde/serde.py:L16-L30](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/serde.py#L16-L30) — 定义 `Serializer.to_bytes`：要求序列化结果**同时包含数据和元信息（shape/dtype）**，这样反序列化端无需外部传参就能还原形状。`Deserializer` 接收一个 `dtype` 参数（决定还原成什么精度），其 `from_bytes` 见 [serde.py:L46-L61](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/serde.py#L46-L61)。

两个 `DebugWrapper`（[serde.py:L33-L43](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/serde.py#L33-L43) 与 [serde.py:L64-L75](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/serde.py#L64-L75)）是装饰器模式的典型用法：包一层就能计时，不污染真实实现。

再看 SERDE 在主链路里的挂载点。`RemoteBackend.__init__` 调用工厂 `CreateSerde` 创建一对 serializer/deserializer：

[lmcache/v1/storage_backend/remote_backend.py:L69-L72](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/remote_backend.py#L69-L72) — `CreateSerde(config.remote_serde, ...)` 按配置字符串（`"naive"` / `"kivi"` / `"cachegen"`）选具体实现，见工厂 [naive_serde/__init__.py:L21-L41](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/naive_serde/__init__.py#L21-L41)。

`_put` 在发送前序列化：

[lmcache/v1/storage_backend/remote_backend.py:L256](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/remote_backend.py#L256) — `compressed_memory_obj = self.serializer.serialize(memory_obj)`：把 MemoryObj 压成字节，准备交给 connector 发走（批量版见 [remote_backend.py:L311](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/remote_backend.py#L311)）。

`_get` 在收到后反序列化：

[lmcache/v1/storage_backend/remote_backend.py:L380](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/remote_backend.py#L380) — `decompressed_memory_obj = self.deserializer.deserialize(memory_obj)`：把字节还原回 KV 张量（批量版见 [remote_backend.py:L497](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/remote_backend.py#L497)）。

#### 4.1.4 代码实践

**实践目标**：亲眼确认 SERDE 只在远端/磁盘路径出现，本地热缓存不经过它。

**操作步骤**：

1. 在 [remote_backend.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/remote_backend.py) 中确认 `_put`/`_get` 调用了 `self.serializer` / `self.deserializer`。
2. 用 Grep/搜索在 [local_cpu_backend.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/local_cpu_backend.py) 中查找 `serialize` / `to_bytes`。

**需要观察的现象**：`LocalCPUBackend` 里**搜不到**任何 serialize/deserialize 调用——它直接持有 `MemoryObj`，不做字节转换。

**预期结果**：你会得到一张结论——SERDE 是「出本机内存」的边界变换，本地路径绕过它。

> 本实践为源码阅读型，无需运行命令。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Deserializer` 要在构造时传入 `dtype`，而 `Serializer` 不用？

**参考答案**：`Serializer` 把张量连同其**原始** dtype 一起编码进字节（`to_bytes` 契约要求「包含元信息」），所以无需外部告知。`Deserializer` 还原时，往往需要还原成引擎当前使用的目标精度（例如存的是 fp16、但当前引擎用 bf16），所以由调用方在构造时指定输出 dtype。

**练习 2**：如果把 SERDE 的位置从「RemoteBackend 内部」挪到「StorageManager 统一做一次」，会有什么问题？

**参考答案**：那 `LocalDiskBackend` 和 `RemoteBackend` 就被迫共用同一种编码与同一个序列化时机，无法各自选择压缩算法或缓冲策略；而且本地热缓存本不该被序列化，统一做一次会破坏「本地零拷贝」的优势。放在每个「出本机」的后端内部，才能让每层后端按自己的介质（磁盘/网络）选最合适的编码。

---

### 4.2 legacy CacheGen serde：量化 + 算术熵编码压缩

#### 4.2.1 概念说明

`CacheGenSerializer` / `CacheGenDeserializer` 是 legacy 路径默认的压缩 serde（配置 `remote_serde: "cachegen"`）。它的压缩分两步，思路来自 CacheGen 论文：

1. **量化（Quantization）**：把 fp16 的 KV 值按层分成几组，每组用不同的「桶数 `bins`」对称量化成 int8。桶数越少越省空间、但精度损失越大；不同层对误差的敏感度不同，所以用配置表给不同层分配不同 `bins`。
2. **算术熵编码（Arithmetic Entropy Coding）**：对量化后的 int8 符号，按「每个通道」统计直方图得到概率分布（CDF），再用算术编码把符号流压成短字节流——出现多的值占的位更少。

两步合起来，远比「直接 `tensor.numpy().tobytes()`」省空间。解码是其精确逆过程：熵解码还原 int8 → 反量化还原浮点。

#### 4.2.2 核心流程

**编码（`to_bytes`）**：

```
输入张量 [num_layers, 2, num_tokens, num_heads, head_size]
   │
   │  _split_kv: 把第 1 维的 2 拆开、heads*head_size 合并成 channels
   ▼
fp_k, fp_v : [num_layers, num_tokens, num_channels]
   │
   │  torch_quant_vectorized: 按层组用不同 bins 对称量化
   ▼
new_key, new_value : int8，范围 [-bins//2+1, bins//2-1]
   │
   │  lmc_ops.calculate_cdf: 每(层,通道)统计直方图 → CDF
   ▼
cdf_int  （熵编码的「码表」）
   │
   │  lmc_ops.encode_fast_new: 用 CDF 做算术编码
   ▼
bytestream（压缩字节）+ cdf + max_tensors（反量化用的最大值）+ num_heads/head_size
   │
   │  CacheGenGPUEncoderOutput.to_bytes: pickle 打包
   ▼
bytes
```

量化数学：设某层组的桶数为 \(b\)，记 \(C = b/2 - 1\)，该层 token 在 channel 维上的绝对值最大值为 \(m\)，则量化与反量化为

\[
x_q = \mathrm{round}\!\left(x \cdot \frac{C}{m}\right),\qquad \hat{x} = \frac{x_q}{C}\cdot m
\]

\(x_q\) 落在 \([-C, C]\) 内（例如 \(b=32\) 时为 \([-15,15]\)，正好放进 int8）。反量化时把 \(m\)（`max_tensors`）逐层逐 token 存下来即可还原尺度。

**解码（`from_bytes`）** 为逆过程：unpickle → `decode_fast_prefsum` 熵解码 → `do_dequantize` 反量化 → 把 K/V 合并回 `[num_layers, 2, num_tokens, num_heads, head_size]`。

#### 4.2.3 源码精读

**(1) 拆分 K/V 与合并 channels**。KV 张量第 1 维是 K、V 拼一起，先拆开并把 `num_heads * head_size` 合并成一个 `nchannels`：

[lmcache/storage_backend/serde/cachegen_encoder.py:L86-L101](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_encoder.py#L86-L101) — `_split_kv`：把 `[L, 2, T, H, D]` reshape 成 `[L, 2, T, H*D]` 再 `unbind(dim=1)`，得到 K 与 V 两个 `[L, T, C]` 张量。

**(2) 向量化解化**。核心量化函数：

[lmcache/storage_backend/serde/cachegen_encoder.py:L47-L71](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_encoder.py#L47-L71) — `torch_quant_vectorized`：`bins` 形状为 `[nlayers]`，每层一个桶数；`MAX = bins//2 - 1`；对每层取 channel 维绝对值最大值 `max1`，再 `round(x * MAX/max1)`。这就是上式 \(x_q = \mathrm{round}(x\cdot C/m)\) 的批量实现。单层版本见 [cachegen_encoder.py:L26-L44](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_encoder.py#L26-L44) 的 `torch_quant`。

**(3) 每层的 `bins` 从哪来**。`CacheGenConfig.from_model_name` 按模型名给出每层量化档位：

[lmcache/storage_backend/serde/cachegen_basics.py:L41-L60](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_basics.py#L41-L60) — 对 7B/8B 模型，Key 前 10 层用 32 桶、其余用 16 桶；Value 前 2 层用 32 桶、其余 16 桶。`make_key_bins` / `make_value_bins`（[cachegen_encoder.py:L348-L358](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_encoder.py#L348-L358)）把这张表展平成长度为 `nlayers` 的 `bins` 向量。未被硬编码的模型走 `AutoConfig` 自动推断层数并套用同样的「前几层多桶、后面少桶」规则（[cachegen_basics.py:L88-L132](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_basics.py#L88-L132)）。

**(4) 熵编码**。算术编码这一步由 C 内核承担：

[lmcache/storage_backend/serde/cachegen_encoder.py:L252-L275](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_encoder.py#L252-L275) — `encode_ntokens`：调用 `lmc_ops.encode_fast_new(cdf_int, encode_input, output_buffer, output_lengths)`（[L267](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_encoder.py#L267)），用 CDF 对 int8 符号做算术编码，再由 `collect_bytes`（[L236-L249](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_encoder.py#L236-L249)）按每通道的实际输出长度把有效字节紧凑拼出。CDF 本身由 `lmc_ops.calculate_cdf` 从量化结果统计（见 `encode_function` [L300-L302](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_encoder.py#L300-L302)）。

为避免单次编码块过大，token 按 `CACHEGEN_GPU_MAX_TOKENS_PER_CHUNK = 256`（[cachegen_basics.py:L18](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_basics.py#L18)）切片循环编码（`encode_function` 的 for 循环 [L313-L329](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_encoder.py#L313-L329)）。

**(5) 打包成字节**。`encode_function` 把压缩字节块、CDF、`max_tensors`、`num_heads/head_size` 装进 `CacheGenGPUEncoderOutput`（[L331-L338](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_encoder.py#L331-L338)），`to_bytes` 用 `pickle.dump` 一次性序列化（[cachegen_basics.py:L189-L200](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_basics.py#L189-L200)）。`CacheGenSerializer.to_bytes` 是入口（[cachegen_encoder.py:L360-L397](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_encoder.py#L360-L397)），它在调用 `encode_function` 前还会修正 GPU 设备号（Ray worker 上的已知问题，注释见 [L373-L381](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_encoder.py#L373-L381)）。

**(6) 解码逆过程**。熵解码用 `lmc_ops.decode_fast_prefsum`：

[lmcache/storage_backend/serde/cachegen_decoder.py:L58-L74](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_decoder.py#L58-L74) — `decode_chunk`：用 CDF 把字节流算术解码回 int8，写入 `target_buffer`。反量化 `do_dequantize` 见 [cachegen_decoder.py:L33-L43](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_decoder.py#L33-L43)（即 \(\hat{x}=x_q/C\cdot m\)），最后把 K/V 合并回原始五维形状（[cachegen_decoder.py:L194-L208](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_decoder.py#L194-L208)）。`CacheGenDeserializer.from_bytes` 串起整条解码链（[cachegen_decoder.py:L153-L208](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_decoder.py#L153-L208)）。

#### 4.2.4 代码实践

**实践目标**：理解「桶数 `bins` 越小、压缩率越高但误差越大」的取舍，并定位算术编码在源码中的 C 内核调用点。

**操作步骤**：

1. 阅读 [cachegen_basics.py:L41-L86](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_basics.py#L41-L86)，记录 7B/8B 模型各层 Key/Value 的 `bins` 取值。
2. 在仓库中搜索 `encode_fast_new` 与 `decode_fast_prefsum`，找到它们的 C 实现（通常在 `csrc/`）。
3. 思考：把所有层的 `bins` 都从 16 改成 8，存储体积会怎么变？反量化误差会怎么变？

**需要观察的现象 / 预期结果**：
- `bins=16` 时 \(C=7\)，符号范围 \([-7,7]\)（15 个值）；`bins=8` 时 \(C=3\)，范围 \([-3,3]\)（7 个值）。桶数减半 → 每个符号的可能取值更少 → 熵更低 → 算术编码输出更短 → **存储更省**；但量化阶梯变粗 → **反量化误差更大**，可能影响生成质量。这就是「精度 vs 体积」的权衡。

> 若想运行验证：可参考 [tests/test_serde.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/test_serde.py) 与 [tests/benchmarks/test_cachegen.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/benchmarks/test_cachegen.py) 构造一个 `[L,2,T,H,D]` 张量，分别用 `bins=16` 和 `bins=8` 跑 `CacheGenSerializer` + `CacheGenDeserializer`，对比 `len(to_bytes)` 与 `||KV - KV_hat||`。具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：CacheGen 为什么要把 token 切成每块 256 个再编码，而不是一次性编码整条序列？

**参考答案**：算术编码的状态会随符号累积，单块过大会让中间张量（`output_buffer` 形状 `[nlayers, nchannels, BUFFER_SIZE]`）占用过多显存；256 是显存与编码效率的折中（见 `encode_function` 的切片循环）。切片也便于流式处理超长上下文。

**练习 2**：反量化时为什么必须保存 `max_tensors`（每层每 token 的 \(m\)）？

**参考答案**：因为量化用的是「对称、按 token 归一化」的尺度 \(m=\max|x|\)。不同 token、不同层的取值范围不同，只有把每个 token 的 \(m\) 一并存下来，解码端才能用 \(\hat{x}=x_q/C\cdot m\) 还原出原始尺度。丢掉 \(m\) 就无法还原。

---

### 4.3 v1 kv_codec：AsymK16V8Codec 不对称 K16/V8 编解码

#### 4.3.1 概念说明

v1 的 `kv_codec` 是一套**独立于存储后端**的编解码层（见包说明 [kv_codec/__init__.py:L1-L18](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/__init__.py#L1-L18)）：它接收 K、V 张量与 scale，产出一个**自描述（self-describing）字节 blob**，任何后端（本地盘、GDS、NIXL、Redis 等）都能「按字节不透明地」写入。

旗舰实现是 `AsymK16V8Codec`——「不对称」是关键词：

- **K 不量化**：保留模型原生 dtype（fp16/bf16），原样拷字节。
- **V 量化到 FP8 e4m3fn**：每元素从 2 字节降到 1 字节。

为什么不对称？因为在 attention 里，**K 直接参与 \(QK^\top\) 的点积**，量化误差会被放大成注意力分布的偏差，对质量影响大；而 **V 只是按注意力权重加权求和**，对单元素误差更鲁棒。所以宁可省 V 的空间、也要保住 K 的精度。

此外，这个 blob 是**自描述 + 防串味 + 可校验**的：头部带 magic、dtype、scale 信息、一组识别哈希（模型/分词器/…）和 CRC32。这样从远端取回的字节即使来源复杂，也能在解码前自检「是不是我预期的配置产生的」。

#### 4.3.2 核心流程

**V 的 FP8 量化**（核心三函数 `compute_v_scales` / `quantize_v_fp8` / `dequantize_v_fp8`）。设 FP8 能表示的最大绝对值为 \(q_{\max}=\text{finfo(fp8).max}\)，按 `scale_scope` 选定一个缩放粒度（整张量一个 scale / 每个 head 一个 / 每个 (page,head) 一个），在该范围内取 \(m=\max|V|\)，则

\[
s = \frac{m}{q_{\max}},\qquad q = \mathrm{clamp}\!\left(\frac{V}{s},\,-q_{\max},\,q_{\max}\right).\text{to(fp8)},\qquad \hat{V} = q\cdot s
\]

全零张量时用哨兵 scale `1.0`，保证 \(\text{dequant}(\text{quant}(0))=0\) 精确且避免 \(0/0\) 的 NaN（见 [asym_k16_v8.py:L37-L40](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/asym_k16_v8.py#L37-L40)）。

**编码（`encode`）**：

```
(K, V) 原生 dtype
   │
   │  compute_v_scales(V) → scales；quantize_v_fp8(V, scales) → V_fp8
   ▼
K_bytes（原生 dtype 原样）+ V_bytes（fp8）+ scale_bytes
   │
   │  serialize_header: 拼 magic+version+dtype+shape+hashes+CRC32 的头部
   ▼
header_bytes ‖ K_bytes ‖ V_bytes ‖ scale_bytes   = 一个 EncodedKV blob
```

**解码（`decode`）**：反着来——`deserialize_header` 校验 magic/CRC 并切出三段 payload；K 直接 `torch.frombuffer` 还原；V 读成 fp8，若调用方要求更高精度则 `dequantize_v_fp8` 反量化。

#### 4.3.3 源码精读

**(1) scale 计算（三种粒度）**。

[lmcache/v1/kv_codec/asym_k16_v8.py:L69-L148](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/asym_k16_v8.py#L69-L148) — `compute_v_scales`：按 `ScaleScope` 分支——`PER_TENSOR` 对整张量取一个标量（[L98-L104](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/asym_k16_v8.py#L98-L104)）；`PER_LAYER_HEAD` 每个 head 一个（[L106-L117](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/asym_k16_v8.py#L106-L117)）；`PER_PAGE_HEAD` 每个 (page,head) 一个（[L119-L140](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/asym_k16_v8.py#L119-L140)，文档说明它是对分页 KV 的推荐默认，见 `ScaleScope` 枚举 [encoded_kv.py:L43-L61](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/encoded_kv.py#L43-L61)）。粒度越细、scale 越贴合局部动态范围、精度越高，但 scale 张量本身的开销也越大。

**(2) 量化与反量化**。

[lmcache/v1/kv_codec/asym_k16_v8.py:L151-L197](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/asym_k16_v8.py#L151-L197) — `quantize_v_fp8`：按 scope 把 scale 广播到 V 的形状，做 `V/s` 后 `clamp(±qmax)` 再 cast 到 fp8。注意 [L196](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/asym_k16_v8.py#L196) 的 `torch.clamp(..., min=-qmax, max=qmax)` 实现「饱和」，防止超出 FP8 表示范围。反量化 `dequantize_v_fp8` 见 [L200-L236](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/asym_k16_v8.py#L200-L236)（`out = q * s`）。

**(3) encode：拼装 blob**。

[lmcache/v1/kv_codec/asym_k16_v8.py:L271-L372](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/asym_k16_v8.py#L271-L372) — `AsymK16V8Codec.encode`：
- K/V 形状必须一致（[L302-L306](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/asym_k16_v8.py#L302-L306)）；
- 若调用方已预先量化好 V（`precomputed_v_quant`），直接拷字节不再重量化（[L308-L320](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/asym_k16_v8.py#L308-L320)），否则现场算 scale + 量化（[L321-L336](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/asym_k16_v8.py#L321-L336)）；
- 三段 payload 用 `_tensor_to_bytes_fast`（[L43-L57](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/asym_k16_v8.py#L43-L57)，`view(uint8).numpy().tobytes()` 的单次 memcpy）转成字节，拼成 `payload = k_bytes + v_bytes + s_bytes`（[L346-L370](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/asym_k16_v8.py#L346-L370)）；
- 最后 `serialize_header(enc)` 生成头部（[L371](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/asym_k16_v8.py#L371)）。

**(4) 自描述头部：magic / dtype / 哈希门禁 / CRC**。头部格式注释写得非常清楚：

[lmcache/v1/kv_codec/encoded_kv.py:L64-L97](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/encoded_kv.py#L64-L97) — 固定头是 76 字节小端结构：8 字节 magic `LMCKV\x01\x00\x01`（[L26](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/encoded_kv.py#L26)，可读、版本化）、各 dtype/scope 的短整型、layer/page 形状元信息、三段 payload 长度、一组 `(key,value)` 哈希串、最后 4 字节 payload CRC32。`serialize_header` 落实这套打包（[encoded_kv.py:L236-L283](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/encoded_kv.py#L236-L283)，CRC 在 [L280](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/encoded_kv.py#L280)）。

**(5) 防串味（cache poisoning）门禁**。`CodecHashes` 携带 `model_id` / `model_revision_hash` / `tokenizer_hash` / `rope_config_hash` / `attention_backend` / `kv_layout` 六项（[encoded_kv.py:L130-L154](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/encoded_kv.py#L130-L154)）。解码时若调用方给了 `expected_hashes`，任何非空字段不匹配就抛 `CodecMismatchError`：

[lmcache/v1/kv_codec/asym_k16_v8.py:L402-L410](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/asym_k16_v8.py#L402-L410) — `_check_hash_match`：逐一比对，空串当通配符。这能挡住「把 B 模型产生的 KV 当成 A 模型的复用」这类隐蔽错误。完整性校验（magic/version/CRC/截断）由 `deserialize_header` 完成（[encoded_kv.py:L286-L403](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/encoded_kv.py#L286-L403)），失败抛 `CorruptEncodedKVError`（错误类型见 [errors.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/errors.py)）。

**(6) decode：按偏移切三段**。

[lmcache/v1/kv_codec/asym_k16_v8.py:L412-L473](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/asym_k16_v8.py#L412-L473) — `decode`：用 `k_payload_len` / `v_payload_len` 算出三段偏移切出 K/V/scale 字节（[L434-L439](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/asym_k16_v8.py#L434-L439)），K 直接 `torch.frombuffer`（[L443](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/asym_k16_v8.py#L443)），V 读成 fp8 后按需 `dequantize_v_fp8`（[L459-L472](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/asym_k16_v8.py#L459-L472)）。若 `out_v_dtype=None` 则保留 fp8（不对称的「存储侧不反量化」原生路径）。

> **如何接入分布式存储**：`AsymK16V8Codec` 是纯编解码器；`lmcache/v1/distributed/serde/asym_k16_v8.py` 把它包装成 `MultiSerializer` / `MultiDeserializer`（见 [distributed/serde/asym_k16_v8.py:L69-L131](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/asym_k16_v8.py#L69-L131) 的 `AsymK16V8MultiSerializer`），使它能被 L2 存储管线按 `(K, V)` 元组调用。这是 u4 单元「SERDE 变换与压缩」的内容，这里只需知道「codec 是内核、serde wrapper 是粘合层」。

#### 4.3.4 代码实践

**实践目标**：用 `AsymK16V8Codec` 跑一遍 encode→to_bytes→from_bytes→decode 的完整往返，体会「K 精确还原、V 有量化误差」，并验证哈希门禁与 CRC 的作用。

**操作步骤**（示例代码，需在有 torch 的环境运行）：

```python
# 示例代码：非项目原有代码，仅为演示 AsymK16V8Codec 的往返
import torch
from lmcache.v1.kv_codec import AsymK16V8Codec, ScaleScope, CodecHashes

torch.manual_seed(0)
shape = (2, 64, 8, 128)  # (n_layers, n_tokens, n_heads, head_dim) 举例
k = torch.randn(*shape, dtype=torch.bfloat16)
v = torch.randn(*shape, dtype=torch.bfloat16)

codec = AsymK16V8Codec(scale_scope=ScaleScope.PER_TENSOR)
enc = codec.encode(k, v, hashes=CodecHashes(model_id="my-model"))
blob = codec.to_bytes(enc)            # 这就是写入 L2 的字节
print("blob bytes =", len(blob))

restored = codec.from_bytes(blob, expected_hashes=CodecHashes(model_id="my-model"))
k2, v2, scales = codec.decode(restored, out_v_dtype=torch.bfloat16)

print("K 还原误差:", (k.float() - k2.float()).abs().max().item())   # 预期 ~0（K 未量化）
print("V 还原误差:", (v.float() - v2.float()).abs().max().item())   # 预期 > 0（V 经 FP8）
```

**需要观察的现象**：
- K 的最大还原误差应当≈0（字节级精确拷贝）；
- V 的还原误差为非零小量（FP8 量化引入）；
- `len(blob)` 远小于原始 `k`+`v` 的字节数（V 省了一半）。

**进阶观察**：把 `from_bytes` 的 `expected_hashes` 改成另一个 `model_id`，应抛 `CodecMismatchError`；手动翻转 blob 中某字节，`deserialize_header` 应抛 `CorruptEncodedKVError`（CRC 校验失败）。

**预期结果**：确认「不对称」——K 无损、V 有损但体积更小；自描述头部能在解码前拦截配置不匹配与数据损坏。具体误差数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `PER_PAGE_HEAD` 被推荐为分页 KV 的默认 scale 粒度？

**参考答案**：分页 KV 的每个 page 是独立的小块，不同 page 的数值动态范围可能差异很大。用「整层一个 scale」会 forced 用最大范围覆盖所有 page，小数值 page 的精度被浪费；「每 (page,head) 一个 scale」让每个小块都有贴合自己的动态范围，精度更高，而 scale 张量相对 V 字节体积极小（见 `ScaleScope` 枚举注释）。

**练习 2**：`encode` 里 `precomputed_v_quant` 这条「跳过重量化」的分支有什么实际意义？

**参考答案**：有些上游（如引擎自身已经产出 FP8 的 V）不想被再量化一次（再量化会引入额外误差、也浪费算力）。这条分支允许调用方把「已经量化好的 V + 对应 scale」直接喂进来，codec 只负责拷字节、不再碰数值，既快又准（见 [asym_k16_v8.py:L308-L320](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/asym_k16_v8.py#L308-L320)）。

**练习 3**：相比 legacy CacheGen，`AsymK16V8Codec` 没有「算术熵编码」这一步，压缩靠什么？

**参考答案**：只靠 FP8 量化（V 从 2 字节/元素降到 1 字节/元素），不做熵编码。这是「简单、可并行、解码快」的取舍——FP8 量化是纯逐元素运算、天然适合 GPU 并行，省去了 CacheGen 那种需要 C 内核算术编码/解码的开销与串行性，代价是没有进一步压掉统计冗余。两者代表两种压缩哲学：CacheGen「量化 + 熵编码」（压缩率更高、实现更重）vs K16/V8「不对称量化」（更轻、更快、对 K 无损）。

## 5. 综合实践

把本讲两套编码路径串起来对比。请完成下面这张「编码流水线对照表」，全部基于真实源码填写：

| 阶段 | legacy CacheGen | v1 AsymK16V8 |
|---|---|---|
| 输入 | `[L,2,T,H,D]` 浮点张量 | `(K, V)` 两个浮点张量 |
| K 处理 | ?（量化吗？几桶？） | ?（量化吗？） |
| V 处理 | ?（量化吗？几桶？） | ?（量化吗？目标 dtype？） |
| 进一步压缩 | ?（用了什么编码？） | ?（有没有？） |
| 打包格式 | ?（用什么库序列化？） | ?（自描述头吗？含哪些校验？） |
| 还原 | ?（逆过程两步） | ?（K 怎么还原？V 怎么还原？） |

**任务**：

1. 通读 [cachegen_encoder.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/storage_backend/serde/cachegen_encoder.py) 与 [asym_k16_v8.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/kv_codec/asym_k16_v8.py)，把每个 `?` 填成「源码里的真实行为 + 对应行号」。
2. 画出两条路径并排的数据流图：`原始 KV → 编码 → 存储（字节）→ 解码 → 还原 KV`。
3. 用一句话总结：**什么场景下你会选 CacheGen，什么场景下选 AsymK16V8？**（提示：从「压缩率 vs 解码速度 vs K 的精度」三个维度想。）

**参考结论**：追求极致压缩率、能接受 C 内核编解码开销和 K 的有损量化时选 CacheGen；追求解码快、GPU 友好、且希望 K 无损（保住 attention 质量）时选 AsymK16V8。具体选型取舍**待结合你的模型与硬件本地验证**。

## 6. 本讲小结

- **SERDE 是「离开本机内存」的边界变换**：只有 `LocalDiskBackend` / `RemoteBackend` 需要，本地热缓存零拷贝绕过；抽象契约就两个方法 `to_bytes` / `from_bytes`。
- **legacy CacheGen = 量化 + 算术熵编码**：按层组用不同 `bins` 对称量化成 int8（\(\hat{x}=x_q/C\cdot m\)），再按通道统计 CDF、用 C 内核 `encode_fast_new` 做算术编码；解码用 `decode_fast_prefsum` 熵解码 + `do_dequantize` 反量化。
- **v1 AsymK16V8 = 不对称量化**：K 保留原生 dtype 原样拷字节，V 量化到 FP8 e4m3fn（\(s=m/q_{\max},\,q=\mathrm{clamp}(V/s)\)），不做熵编码——以「轻量、并行、K 无损」换取略低的压缩率。
- **v1 blob 是自描述的**：头部含 magic `LMCKV...`、dtype/scope/shape、六项识别哈希（防跨模型串味）和 CRC32（防损坏），解码前能完整自检。
- **两者代表两种压缩哲学**：CacheGen「量化 + 熵编码」（压得更狠、更重）vs AsymK16V8「不对称量化」（更快、K 无损、实现更简）。

## 7. 下一步学习建议

- 想看 v1 如何把 codec 接入**分布式 L2 存储**：阅读 [distributed/serde/asym_k16_v8.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/serde/asym_k16_v8.py)（`MultiSerializer` 粘合层）与设计文档 [docs/design/v1/distributed/serde/README.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/distributed/serde/README.md)，这正是 **u4-l4（SERDE 变换与压缩）** 的主题。
- 想了解 c_ops 这些 C 内核（`encode_fast_new` / `decode_fast_prefsum` / `calculate_cdf`）如何编译与加载：回顾 [u1-l2（安装、构建与运行）](u1-l2-install-and-run.md) 里 `c_ops` 扩展的构建，并去 `csrc/` 读它们的 C++ 实现。
- 想看存储层如何在并发取回时调度 SERDE：回顾 [u2-l4（StorageManager 与异步序列化）](u2-l4-storage-manager.md) 的 `AsyncSingleSerializer` / `AsyncMultiSerializer` 与 `WeightedSemaphore`。
- 下一讲 [u3-l1（多进程架构总览）](u3-l1-mp-architecture-overview.md) 将转入 LMCache 的多进程架构，SERDE 在跨进程 KV 传递中同样扮演关键角色。
