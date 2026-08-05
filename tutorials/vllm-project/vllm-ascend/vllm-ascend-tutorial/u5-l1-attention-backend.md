# AscendAttentionBackend 注册与元数据

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `AscendAttentionBackend` 是「什么」：一个向上游 vLLM 注册的注意力后端（attention backend），它把 NPU 上的注意力算子接入 vLLM 的注意力抽象。
- 区分两件事：用 `@register_backend` 把后端「登记」进 vLLM 注册表，与用 `NPUPlatform.get_attn_backend_cls` 在运行期「选中」这个后端。
- 读懂 `get_impl_cls` / `get_builder_cls` 的分支逻辑，理解上下文并行（DCP）如何切换实现。
- 解释 `get_kv_cache_shape` 返回的 `(2, num_blocks, block_size, num_kv_heads, head_size)` 每一维的含义，尤其「为什么是 2」。
- 理解 `AscendMetadata`、`AscendAttentionState` 状态机，以及 `AttentionMaskBuilder` 如何缓存注意力掩码。

本讲是 u5 注意力后端单元的总纲，后续 u5-l2（MLA/SFA/DSA）、u5-l3（上下文并行）都建立在本讲的注册与元数据机制之上。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，什么是「注意力后端」。** Transformer 的核心运算是注意力（Attention）。vLLM 把「注意力该怎么算」抽象成一个接口 `AttentionBackend`：它声明了若干静态方法（如「KV 缓存长什么样」「给我一个真正干活的实现类」「给我一个构造元数据的 builder 类」），具体的后端（FlashAttention、ROCm 的 Attention、以及这里的 Ascend）各自实现这套接口。模型代码里写的是 `qkv_proj -> attention -> o_proj`，其中 `attention` 这一步会被 vLLM 路由到「当前平台选中的那个后端」。这样模型代码与硬件解耦——同一份模型，在 GPU 上走 FlashAttention，在 NPU 上走 Ascend 后端。

**第二，什么是「KV 缓存（KV cache）」与「分页（Paged）」。** 自回归生成时，每生成一个 token 都要对「之前所有 token」做注意力。为避免重复计算，把每层的 Key、Value 缓存下来复用，这就是 KV cache。vLLM 用「分页」方式管理它：把 KV cache 切成固定大小的「块（block）」，用一个 `block_table`（块表）把「逻辑序列」映射到「物理块」。这与操作系统的分页内存思想一致——序列只持有块指针，物理块可被多个序列复用（prefix caching）或换入换出（swap）。本讲反复出现的 `num_blocks`、`block_size`、`block_table`、`slot_mapping` 都服务于这套分页机制。

**第三，什么是「注意力元数据（attention metadata）」。** 一次前向里，注意力算子需要很多「这次怎么算」的上下文信息：这批有多少个请求、每个请求的序列长度、query 长度、块表、掩码、当前是 prefill 还是 decode……这些信息被打包成一个对象传给每一层的注意力算子，称为注意力元数据。vLLM 把它分成两层：

- `CommonAttentionMetadata`（上游定义）：跨后端共享的通用字段；
- 每个后端自己的 metadata（本讲的 `AscendMetadata`）：该后端专属的字段。

而「builder」就是负责把通用元数据「翻译/组装」成后端专属元数据的对象。

> 承接前置讲义：u4-l2 讲到 `NPUModelRunner` 一次前向主链路里会调用 `_build_attention_metadata`，并由 `set_ascend_forward_context` 注入前向上下文；本讲就解释那个 attention metadata 是怎么被构建出来的、背后端是谁。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `vllm_ascend/attention/attention_v1.py` | 本讲主角：`AscendAttentionBackend`（后端身份）、`AscendAttentionBackendImpl`（真正算注意力的实现）、`AscendAttentionMetadataBuilder`（元数据构建器）、`AscendMetadata`（每层元数据）、`AscendAttentionState`（状态枚举）。 |
| `vllm_ascend/attention/attention_mask.py` | `AttentionMaskBuilder`：单例掩码构建器，缓存因果掩码、MLA 掩码、splitfuse 掩码。 |
| `vllm_ascend/attention/utils.py` | `AscendCommonAttentionMetadata`（通用元数据）、`enable_dcp()`（是否开启上下文并行的开关）、`split_decodes_and_prefills`（把批次拆成 decode/prefill）等辅助函数。 |
| `vllm_ascend/platform.py` | `NPUPlatform.get_attn_backend_cls`：运行期选中本后端的平台钩子（见 4.1）。 |

## 4. 核心概念与源码讲解

### 4.1 注意力后端的注册与实现选择

#### 4.1.1 概念说明

让 NPU 跑注意力，需要回答两个问题：

1. **「登记」问题**：怎么让 vLLM 知道「存在一个叫 Ascend 的注意力后端」，使它成为一个合法的可选项？答案是用上游 vLLM 提供的 `register_backend` 装饰器，把 `AscendAttentionBackend` 类登记到 vLLM 的后端注册表里。
2. **「选中」问题**：vLLM 启动加载模型时，怎么决定「这次推理就用 Ascend 这个后端」？答案是平台钩子 `NPUPlatform.get_attn_backend_cls` 返回一个类路径字符串，vLLM 据此延迟 import 并实例化。

这两件事是分开的：装饰器只负责「登记在册」，平台钩子负责「在册的后端里挑哪一个」。理解这一点非常关键——`AscendAttentionBackend`、`AscendMLABackend`、`AscendSFABackend`、`AscendDSABackend` 都会被各自登记，而 `get_attn_backend_cls` 根据模型是否用 MLA、是否用稀疏注意力（sparse）、是否用压缩（compress）来决定挑哪一个。

> 术语：MLA = Multi-head Latent Attention（DeepSeek 的 KV 压缩注意力，u5-l2 详讲）；SFA = 稀疏/分片注意力；DSA = 压缩注意力。本讲聚焦标准注意力（三者都为否）对应的 `AscendAttentionBackend`。

#### 4.1.2 核心流程

从 vLLM 视角看一次后端选择，流程如下（伪代码）：

```
vLLM 启动
  └─ import vllm_ascend （插件被发现，见 u1-l5）
  └─ 选中 NPUPlatform
  └─ 加载模型，需要确定注意力后端
        └─ 调用 NPUPlatform.get_attn_backend_cls(...)
              └─ 根据 (use_mla, use_sparse, use_compress) 查 backend_map
              └─ 返回类路径字符串，例如
                 "vllm_ascend.attention.attention_v1.AscendAttentionBackend"
        └─ vLLM 延迟 import 该类 → 得到 AscendAttentionBackend
        └─ vLLM 调 backend.get_impl_cls()     → 拿到真正算注意力的实现类
        └─ vLLM 调 backend.get_builder_cls()   → 拿到元数据构建器类
        └─ 实例化它们，注入到每层 AttentionLayer
```

注意：后端类（`AscendAttentionBackend`）本身通常**不算注意力**，它更像「工厂 + 配置」；真正算注意力的是 `get_impl_cls()` 返回的 `AscendAttentionBackendImpl`。这是一种「后端（工厂）/实现（干活）/构建器（拼元数据）」三件套设计。

#### 4.1.3 源码精读

**① 用装饰器登记后端。** 类定义正上方的装饰器把它登记进 vLLM 注册表，登记键为 `AttentionBackendEnum.CUSTOM`，附加标识为字符串 `"ASCEND"`：

注册后端并声明身份：[vllm_ascend/attention/attention_v1.py:72-74](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/attention_v1.py#L72-L74)。这里 `@register_backend(AttentionBackendEnum.CUSTOM, "ASCEND")` 表示这是一个「自定义（树外）后端」，`class AscendAttentionBackend(AttentionBackend)` 表示它实现了上游的注意力后端接口，`accept_output_buffer: bool = True` 告诉 vLLM「本后端接受外部预分配的输出缓冲」。

**② `get_name`：对外的后端名字（含一个 HACK）。** vLLM 在某些路径会断言注意力名字，因此这里要小心：

[vllm_ascend/attention/attention_v1.py:76-81](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/attention_v1.py#L76-L81)。默认返回 `"CUSTOM"`；但当启用 v2 model runner（`VLLM_USE_V2_MODEL_RUNNER`）时返回 `"FLASH_ATTN"`。注释说明：v2 model runner 的 `initialize_kv_cache` 会做注意力名字断言，临时伪装成 `FLASH_ATTN` 绕过，待上游去掉该断言后再改回。这是一个典型的「插件为绕过上游假设而做的兼容处理」。

**③ `get_impl_cls` / `get_builder_cls`：按是否上下文并行分流。** 这两个方法各自根据 `enable_dcp()` 在「普通实现」与「上下文并行实现」间二选一：

[vllm_ascend/attention/attention_v1.py:83-97](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/attention_v1.py#L83-L97)。两段逻辑完全对称：`enable_dcp()` 为真时返回 `AscendAttentionDCPImpl` 与 `AscendAttentionDCPMetadataBuilder`（来自 `context_parallel` 子包，u5-l3 详讲），否则返回本文件的 `AscendAttentionBackendImpl` 与 `AscendAttentionMetadataBuilder`。注意返回的是**类**（type）而非实例，由 vLLM 负责实例化；并且这两个 import 写在函数体内（延迟 import），避免在模块加载时就把 CP 相关重型模块拉进来。

**④ `enable_dcp`：开关本身。** 它判断「解码上下文并行尺寸是否大于 1」：

[vllm_ascend/attention/utils.py:181-184](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/utils.py#L181-L184)。`@lru_cache(maxsize=1)` 保证它在进程内只算一次（配置不变），读取的是 `parallel_config.decode_context_parallel_size`。这就是 ③ 中分流的总开关。

**⑤ 平台钩子：在多个已登记后端里挑中本后端。** `get_impl_cls` 解决「本后端内部用哪个实现」，而「vLLM 一开始怎么挑到本后端」由平台钩子回答：

[vllm_ascend/platform.py:796-822](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/platform.py#L796-L822)。它构造一张 `backend_map`，键是三元组 `(use_mla, use_sparse, use_compress)`，值是类路径字符串。标准注意力 `(False, False, False)` 对应本讲的 `vllm_ascend.attention.attention_v1.AscendAttentionBackend`；MLA/SFA/DSA 各有对应后端（u5-l2）。注意它返回的是**字符串**而非类——这是 vLLM 的延迟 import 约定，避免在一启动就触发重型 import（见 u2-l1 关于钩子统一返回类路径字符串的说明）。310P 硬件另有一张更小的 `backend_map_310`（见 u11-l2）。

#### 4.1.4 代码实践

**实践目标**：用源码阅读的方式，验证「装饰器登记」与「平台选中」是两套独立机制。

**操作步骤**：

1. 打开 `vllm_ascend/attention/attention_v1.py:72`，确认 `AscendAttentionBackend` 头顶的 `@register_backend(...)` 装饰器——这是「登记」。
2. 打开 `vllm_ascend/platform.py:803-808`，确认标准注意力 `(False, False, False)` 这一项指向 `AscendAttentionBackend`——这是「选中」。
3. 打开 `vllm_ascend/attention/mla_v1.py`、`sfa_v1.py`、`dsa_v1.py` 的类定义处，确认它们**各自**也有 `@register_backend` 装饰器，但平台钩子在标准注意力场景下不会选中它们。

**需要观察的现象**：你会看到至少 4 个 Ascend 注意力后端类都被登记，但一次推理只会被选中一个。

**预期结果**：能用自己的话说出——「装饰器让所有 Ascend 后端都成为合法候选，平台钩子根据注意力类型挑出当前要用的那一个」。本实践为「待本地验证」（无需 NPU，纯阅读）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `get_impl_cls` / `get_builder_cls` 里的 `from ... import ...` 要写在函数体内部，而不是写在文件顶部？

> **参考答案**：为了延迟 import。其一，CP（上下文并行）相关模块较重，不在 CP 场景下不应被加载；其二，避免模块加载期的循环依赖与不必要的初始化开销。只有在真正需要 DCP 实现时才把对应类拉进来。

**练习 2**：`get_name()` 在 v2 model runner 下为何要伪装成 `"FLASH_ATTN"`？

> **参考答案**：上游 v2 model runner 的 `initialize_kv_cache` 对注意力名字做了断言，会拒绝非预期的名字。临时返回 `"FLASH_ATTN"` 是为绕过该断言的兼容 HACK，注释里也标注了等上游去掉断言后要修正。

### 4.2 KV 缓存形状约定与块管理

#### 4.2.1 概念说明

vLLM 在分配 KV cache 前，会问后端一个问题：「你的 KV cache 张量长什么样？」后端用 `get_kv_cache_shape` 回答。这个形状直接决定了 vLLM 分配多大的显存、按什么布局存放 K/V。

本模块要回答本讲的核心实践问题：`AscendAttentionBackend.get_kv_cache_shape` 返回 `(2, num_blocks, block_size, num_kv_heads, head_size)`，每一维是什么、**为什么第一维是 2**。

直觉答案是：**NPU 上的 K 和 V 被存在同一个张量里，第一维的「2」就是用来区分 Key 与 Value 的——索引 0 是 Key 缓存，索引 1 是 Value 缓存。** 这与某些后端「K、V 分开成两个独立张量」的做法不同：Ascend 选择把它们叠在一起，便于成对搬运与图捕获时统一管理。后续所有用到 KV cache 的代码，都通过 `kv_cache[0]` / `kv_cache[1]` 把它们取出来。

#### 4.2.2 核心流程

KV cache 形状的生命周期：

```
vLLM 分配 KV cache 前
  └─ 调 AscendAttentionBackend.get_kv_cache_shape(num_blocks, block_size, num_kv_heads, head_size)
  └─ 得到 (2, num_blocks, block_size, num_kv_heads, head_size)
  └─ 按此形状分配一个张量（或等价的两个张量列表）

每层注意力 forward
  └─ 收到 kv_cache（形状如上）
  └─ self.key_cache, self.value_cache = kv_cache[0], kv_cache[1]   # 拆出 K/V
  └─ 用 block_table + slot_mapping 把新算出的 K/V 写进对应槽位
  └─ 用 block_table 读取历史 K/V 做注意力
```

五个维度含义对照表：

| 维度 | 名称 | 含义 |
|------|------|------|
| 0 | `2` | Key 缓存与 Value 缓存叠在一起：`[0]`=Key，`[1]`=Value |
| 1 | `num_blocks` | 物理块总数（KV cache 被分成多少个块） |
| 2 | `block_size` | 每个块的 token 数（本后端要求 128，见 4.2.3 ④） |
| 3 | `num_kv_heads` | KV 头数（GQA/MQA 下可少于 query 头数） |
| 4 | `head_size` | 每个头的隐藏维度 |

#### 4.2.3 源码精读

**① 形状定义本身。**

[vllm_ascend/attention/attention_v1.py:99-107](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/attention_v1.py#L99-L107)。返回 `(2, num_blocks, block_size, num_kv_heads, head_size)`——本模块要解释的对象。注意它**忽略了** `cache_dtype_str` 参数（数据类型不参与形状），形状只与几何尺寸有关。

**② 「为什么是 2」的证据一：`copy_blocks` 按 `[0]`/`[1]` 拆 K/V。** 块复制（用于 prefix caching 复用）时，正是用第一维区分 K 和 V：

[vllm_ascend/attention/attention_v1.py:123-135](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/attention_v1.py#L123-L135)。`key_caches = kv_cache[0]`、`value_caches = kv_cache[1]`——`[0]` 取出 Key，`[1]` 取出 Value，再分别做块级别的 `dst = src` 复制。这就是第一维为 2 的直接用途。

**③ 「为什么是 2」的证据二：块交换（swap）同样按 `[0]`/`[1]`。** KV 跨设备搬运时（PD 分离等场景），按相同约定拆分：

[vllm_ascend/attention/attention_v1.py:109-121](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/attention_v1.py#L109-L121)。`src_key_cache, src_value_cache = src_kv_cache[0], src_kv_cache[1]`，与 ② 完全一致。

**④ 「为什么是 2」的证据三：实现类里反复 `kv_cache[0]/[1]` 拆分。** 真正算注意力时，`forward` 入口就把 KV cache 拆成两份：

[vllm_ascend/attention/attention_v1.py:1646-1654](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/attention_v1.py#L1646-L1654)。`self.key_cache, self.value_cache = kv_cache[0], kv_cache[1]`——再次印证 `[0]` 是 Key、`[1]` 是 Value。而 `forward` 的文档字符串也把形状白纸黑字写了出来：

[vllm_ascend/attention/attention_v1.py:1625-1627](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/attention_v1.py#L1625-L1627)。注释明确：`kv_cache: shape = [2, num_blocks, block_size, num_kv_heads, head_size]`。

**⑤ 支持的块大小。** 本后端只接受一种内核块大小：

[vllm_ascend/attention/attention_v1.py:137-139](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/attention_v1.py#L137-L139)。返回 `[128]`——即 NPU 注意力算子按 128 个 token 一块来组织 KV cache。这也是为什么元数据构建器里用 `get_supported_kernel_block_sizes()[0]` 来估算「每个序列最多需要多少块」（见 4.3.3）。

> 小结：第一维的 2 不是「两份副本」，而是「Key 与 Value 两个分量」的叠放维度。所有读写 KV cache 的代码都通过这一维取 K 或取 V。

#### 4.2.4 代码实践（本讲核心实践）

**实践目标**：亲手「拆解」KV cache 形状，验证 `[0]`=Key、`[1]`=Value 的约定。

**操作步骤**：

1. 阅读上面的三处证据（`copy_blocks`、`swap_blocks`、`forward`），记录每一处取出 K/V 的写法。
2. 在仓库内全局搜索 `kv_cache[0]` 与 `self.key_cache` 的赋值点，确认它们成对出现（有 `[0]` 取 Key 的地方，紧挨着就有 `[1]` 取 Value）。
3. 写一段**示例代码**（非项目原有代码）模拟这个形状与拆分（可在纯 CPU 的 PyTorch 下运行，无需 NPU）：

```python
# 示例代码：模拟 Ascend KV cache 形状与 [0]/[1] 拆分约定
import torch

num_blocks, block_size = 4, 128
num_kv_heads, head_size = 8, 128

# 模拟 get_kv_cache_shape 的返回
shape = (2, num_blocks, block_size, num_kv_heads, head_size)
kv_cache = torch.zeros(shape, dtype=torch.float16)

# 约定：[0] = Key 缓存，[1] = Value 缓存
key_cache = kv_cache[0]
value_cache = kv_cache[1]
assert key_cache.shape == (num_blocks, block_size, num_kv_heads, head_size)
assert value_cache.shape == key_cache.shape
print("Key 与 Value 形状一致，第一维 2 用于区分二者：", kv_cache.shape)
```

**需要观察的现象**：`kv_cache[0]` 与 `kv_cache[1]` 形状完全相同，去掉第一维后剩下 `(num_blocks, block_size, num_kv_heads, head_size)`。

**预期结果**：能口头回答——「第一维 2 = Key 与 Value 两个分量；剩下四维是块数、块大小、KV 头数、头维度」。示例代码可在普通 CPU 环境跑通（待本地验证 NPU 路径下的实际分配）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 KV cache 改成「Key、Value 分成两个独立张量」的设计，本后端里哪些代码必须同步修改？

> **参考答案**：所有 `kv_cache[0]` / `kv_cache[1]` 的拆分点都要改：`get_kv_cache_shape` 的返回、`copy_blocks`、`swap_blocks`，以及 `forward` / `do_kv_cache_update` / `reshape_and_cache` 里 `self.key_cache, self.value_cache = kv_cache[0], kv_cache[1]` 的赋值。改造成本不小，这正是统一叠放带来的便利。

**练习 2**：`get_supported_kernel_block_sizes` 返回 `[128]`，这对调度器意味着什么？

> **参考答案**：意味着 NPU 注意力内核按 128 token 一块组织 KV cache；`block_size` 必须能与之配合（参见 builder 里 `cdiv(max_model_len, 128)` 估算每序列最大块数），调度器的 `block_size` 配置需要与该内核约束兼容。

### 4.3 注意力元数据与掩码构建

#### 4.3.1 概念说明

后端被选中、KV cache 也分配好之后，每一步前向还需要一份「这一步怎么算」的说明——注意力元数据。本模块讲三层东西：

- **`AscendAttentionState`（状态枚举）**：这一步处于注意力的哪种形态？是「无缓存的 prefill」「命中缓存的 prefill」「纯 decode」「分块 prefill」还是「投机解码」？不同形态走不同的算子分支。
- **`AscendCommonAttentionMetadata`（通用元数据）**：上游 `CommonAttentionMetadata` 的 Ascend 扩展，补上 NPU 需要的 CPU 端字段（如 `seq_lens_cpu`、`num_computed_tokens_cpu`），并保留 padding、图捕获等信息。
- **`AscendMetadata`（每层元数据）+ builder**：最终喂给每层注意力算子的对象，由 `AscendAttentionMetadataBuilder.build()` 把通用元数据「翻译」而来；翻译过程中还会向 `AttentionMaskBuilder` 要一份缓存的注意力掩码。

> 为什么 NPU 要额外保留 CPU 端的 `seq_lens_cpu`？因为 NPU 注意力算子（FIA）很多参数需要 host 端的 Python list/张量（如 `actual_seq_lengths`、`actual_seq_lengths_kv`），从 device 取回会触发同步、拖慢流水线。这与 u4-l2 讲过的「携带 CPU 端 seq_lens 以避免 device→host 同步」一脉相承。

#### 4.3.2 核心流程

元数据构建流程（`build`）：

```
每步前向
  └─ model runner 准备好 AscendCommonAttentionMetadata（含 seq_lens_cpu 等）
  └─ 对每个注意力层组调用 builder.build(common_prefix_len, common_attn_metadata)
        ├─ 用 split_decodes_and_prefills 把批次拆成 decode / prefill 两段
        ├─ 取出 block_table、seq_lens、slot_mapping
        ├─ 向 AttentionMaskBuilder 索取（缓存的）注意力掩码
        ├─ 处理 SP/图捕获带来的 padding 请求
        └─ 组装成 AscendMetadata（含 attn_state、attn_mask、各长度列表…）
  └─ AscendMetadata 被注入前向上下文，供每层算子读取
```

#### 4.3.3 源码精读

**① 状态枚举：五种注意力形态。**

[vllm_ascend/attention/attention_v1.py:142-147](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/attention_v1.py#L142-L147)。`PrefillNoCache`（首次 prefill、无需读 cache）、`PrefillCacheHit`（prefill 且命中已有 cache）、`DecodeOnly`（纯解码）、`ChunkedPrefill`（分块 prefill，可能混 decode）、`SpecDecoding`（投机解码）。实现类 `_get_fia_params` 等方法就是按 `attn_state` 分支选择不同的算子参数（见 u4-l2 关于状态机的说明）。

**② `AscendMetadata`：每层元数据。** 这是一个 dataclass，字段分两类——基础属性与 KV cache 相关属性：

[vllm_ascend/attention/attention_v1.py:150-201](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/attention_v1.py#L150-L201)。重点字段：`attn_mask`（掩码张量）、`attn_state`（当前形态）、`num_actual_tokens`/`num_decode_tokens`/`num_prefills`/`num_decodes`（token 与请求计数）、`seq_lens`/`seq_lens_cpu`/`seq_lens_list`（序列长度的 device/CPU/list 三种表示，注释也承认它们冗余、待统一）、`actual_seq_lengths_q`（每段 query 的累积长度，喂给 FIA 的 `actual_seq_lengths`）、`block_tables`（块表）、`slot_mapping`（新 K/V 写入哪些槽）、`causal`（是否因果）。这些就是每层算注意力需要的全部上下文。

**③ 通用元数据 `AscendCommonAttentionMetadata`。** 它继承上游 `CommonAttentionMetadata`，补上 NPU 专属字段：

[vllm_ascend/attention/utils.py:199-243](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/utils.py#L199-L243)。注意它显式保留了 `seq_lens_cpu`、`num_computed_tokens_cpu`、`actual_seq_lengths_q`、`positions_cpu`、`graph_pad_size`、`context_parallel_metadata` 等 CPU 端/图/CP 字段。类的 docstring 点明设计意图：「For many of the tensors we keep both NPU and CPU versions」——同一信息常备 device 与 host 两份，host 份用于喂给需要 Python list 的 NPU 算子。

**④ builder 的核心：`build`。** 这是把通用元数据翻译成 `AscendMetadata` 的主方法，逻辑较长但脉络清晰：

[vllm_ascend/attention/attention_v1.py:291-395](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/attention_v1.py#L291-L395)。关键步骤：

- L301-303：调 `split_decodes_and_prefills` 把批次拆成 decode/prefill，得到四元组（请求数、token 数）。
- L308-313：**优先用 `_seq_lens_cpu`**（注释说它在 draft 迭代期间也总可用），其次 `seq_lens_cpu`，最后才退回 device 的 `seq_lens`——体现「优先 host、避免同步」原则。
- L327：向 `AttentionMaskBuilder` 索取掩码（见 ⑤）。
- L354-366：处理 SP/图捕获引入的「dummy padding 请求」——把 `seq_lens` 与 `block_table` 补齐到与 query 段数一致，否则 FIA 会因长度不匹配报错（注释详细解释了 dummy 请求指向 block 0 且其输出无害的原因）。
- L376-394：组装出 `AscendMetadata` 实例返回。

**⑤ 掩码构建器 `AttentionMaskBuilder`（单例，缓存掩码）。** 它用 `@singleton` 装饰，进程内只有一个实例，生成的掩码会被缓存复用：

[vllm_ascend/attention/attention_mask.py:33-49](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/attention_mask.py#L33-L49)。`@singleton`（实现见 `vllm_ascend/utils.py:1359-1367`，按类缓存实例）保证全局唯一；`get_attn_mask` 在「还没缓存」或「需要更长序列」时才重新生成下三角因果掩码，并按 `max_seq_len` 切片返回。底层 `_generate_attn_mask`（[attention_mask.py:21-30](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/attention_mask.py#L21-L30)）构造下三角矩阵，fp16 用 `-inf`、其他用 1 标记被遮蔽位置。

对外入口 `get_attention_mask` 按场景选不同掩码：

[vllm_ascend/attention/attention_mask.py:68-81](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/attention/attention_mask.py#L68-L81)。非因果（双向）注意力返回 `None`（注释解释：FIA 在 `sparse_mode=0` 下会把上三角也遮掉，对双向注意力是错的，所以干脆不传掩码）；pooling 模型用 `get_attn_mask(2048, bool)`；生成式模型用 `get_splitfuse_attn_mask()`。这正是 builder 在 ④ L327 调用的方法。

> 串起来：`build()` → `attn_mask_builder.get_attention_mask(...)` →（按需缓存）→ 返回掩码，最终塞进 `AscendMetadata.attn_mask`，供算子使用。

#### 4.3.4 代码实践

**实践目标**：跟踪一条「通用元数据 → 每层元数据 → 掩码」的调用链，理解各字段的来源。

**操作步骤**：

1. 在 `AscendAttentionMetadataBuilder.build`（attention_v1.py:291）里，定位 `seq_lens` 的三种取值优先级（`_seq_lens_cpu` → `seq_lens_cpu` → device `seq_lens`）。
2. 顺着 L327 找到 `get_attention_mask`，再进 `attention_mask.py:68`，对照三种返回（`None` / `get_attn_mask` / `get_splitfuse_attn_mask`），说明每种对应什么模型场景。
3. 在 `AscendMetadata`（attention_v1.py:150）里数一数 `seq_lens` 相关字段有几个（`seq_lens`、`seq_lens_cpu`、`seq_lens_list`、`actual_seq_lengths_q`），并对照 docstring 里「冗余、待统一」的 TODO。

**需要观察的现象**：同一份「序列长度」信息以多种表示并存；掩码是按需生成并被单例缓存的。

**预期结果**：能画出「`AscendCommonAttentionMetadata` → `build()` → `AscendMetadata`（含掩码）」的数据流草图。本实践为「待本地验证」（源码阅读型，无需 NPU）。

#### 4.3.5 小练习与答案

**练习 1**：`build()` 为什么优先用 `_seq_lens_cpu` 而不是 device 上的 `seq_lens`？

> **参考答案**：NPU 注意力算子需要 host 端的序列长度（Python list / CPU 张量）作为参数。优先用已存在的 CPU 副本可避免从 device 取回（会触发同步、打断异步流水）。此外 `_seq_lens_cpu` 在 draft 迭代期间也总是可用，比 `seq_lens_cpu`（在异步投机解码模式下可能为 None）更可靠。

**练习 2**：非因果（双向）注意力为何 `get_attention_mask` 返回 `None`？

> **参考答案**：FIA 在 `sparse_mode=0`（defaultMask）下会把上三角也遮蔽，这对双向注意力是错误的。所以双向注意力干脆不传掩码，由算子按「不遮蔽」处理。（注释也提到 310P 因其算子要求显式掩码，会另作覆盖。）

## 5. 综合实践

把本讲三个模块串起来，完成一个「后端选择 + KV cache 形状 + 元数据」的综合阅读任务：

1. **注册与选中**：从 `NPUPlatform.get_attn_backend_cls`（platform.py:796）出发，确认标准注意力场景返回 `AscendAttentionBackend` 的类路径；再进入该类，列出它作为「工厂」对外提供的四个关键静态方法（`get_name`、`get_impl_cls`、`get_builder_cls`、`get_kv_cache_shape`）。
2. **KV cache 形状**：写出 `(2, num_blocks, block_size, num_kv_heads, head_size)` 各维含义；在源码里找出**三处**用 `kv_cache[0]`/`kv_cache[1]` 区分 K/V 的代码，作为「为什么是 2」的证据。
3. **元数据链路**：画出 `AscendCommonAttentionMetadata` → `AscendAttentionMetadataBuilder.build` → `AscendMetadata` 的数据流，并标注掩码来自单例 `AttentionMaskBuilder`。

**交付物**：一张时序/数据流草图 + 一段 200 字说明，解释「一个标准注意力请求，从被平台选中、到拿到 KV cache 形状、再到构建出每层元数据」的完整过程。

> 提示：本实践无需 NPU，全部基于源码阅读即可完成；若要验证运行期行为，可结合 u11-l4 介绍的单测框架写一个最小构造用例（待本地验证）。

## 6. 本讲小结

- `AscendAttentionBackend` 通过 `@register_backend(AttentionBackendEnum.CUSTOM, "ASCEND")` **登记**进 vLLM 后端注册表；`NPUPlatform.get_attn_backend_cls` 用 `(use_mla, use_sparse, use_compress)` 三元组查表**选中**它（标准注意力场景）——登记与选中是两套独立机制。
- 它是「工厂 + 配置」：真正算注意力的是 `get_impl_cls()` 返回的 `AscendAttentionBackendImpl`，拼元数据的是 `get_builder_cls()` 返回的 `AscendAttentionMetadataBuilder`；两者都按 `enable_dcp()` 在普通实现与上下文并行实现间分流。
- KV cache 形状 `(2, num_blocks, block_size, num_kv_heads, head_size)` 中，**第一维的 2 用于叠放 Key（`[0]`）与 Value（`[1]`）**，其余四维是块数、块大小、KV 头数、头维度；本后端内核块大小固定为 128。
- 注意力元数据分两层：`AscendCommonAttentionMetadata`（含 NPU 必需的 CPU 端字段）经 `build()` 翻译成 `AscendMetadata`（喂给每层算子）；`AscendAttentionState` 五种枚举驱动算子分支选择。
- `AttentionMaskBuilder` 是进程级单例（`@singleton`），按需生成并缓存因果/MLA/splitfuse 掩码，避免每步重算。
- `get_name()` 在 v2 model runner 下伪装成 `"FLASH_ATTN"` 以绕过上游断言，是一个典型的兼容 HACK。

## 7. 下一步学习建议

- **u5-l2 MLA / SFA / DSA 与稀疏注意力**：本讲只讲了标准注意力后端；下一讲深入 `mla_v1`/`sfa_v1`/`dsa_v1` 这些被同一平台钩子选中的「兄弟后端」，理解它们与 `AscendAttentionBackend` 的实现差异。
- **u5-l3 上下文并行注意力（CP）**：本讲反复出现的 `enable_dcp()` 分流，下一讲详细展开 DCP/MLA-CP/SFA-CP 如何把长序列切分到多卡。
- **延伸阅读**：`AscendAttentionBackendImpl.forward` 及其 `_get_fia_params`、`forward_fused_infer_attention` 等方法（attention_v1.py:849 起）展示了 `AscendMetadata` 与 `attn_state` 如何真正驱动 NPU 注意力算子（`npu_fused_infer_attention_score`），可作为进阶阅读。
