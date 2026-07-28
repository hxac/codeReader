# MLA 解码的 CuTe kv_shared 后端

## 1. 本讲目标

本讲承接 u5-l1（CuTe 后端代码生成）与 u4-l4（MLA 解码与 combine kernel），专门拆解 `backend="cute"` 且 `kv_shared=True` 这条 **MLA 解码的 CuTe 专用路径**。

学完后你应当能够：

1. 说清在 `AttentionEngine.__init__` 里，`cute + kv_shared` 分支与 `cute` 非 kv_shared 分支、与 `tl` MLA 解码分支（u4-l4）各自走哪段代码、产物有何不同。
2. 理解 **paged-kv** 的组织方式：`cache_seqlens`、`block_table`、`page_block_size` 如何把一段变长 KV cache 切成分页块，并在内核里用 `block_table` 做间接寻址。
3. 掌握 `get_mla_metadata` 如何在运行时把整批 KV 工作量切成 `num_sm_parts` 份，产出 `tile_scheduler_metadata` 与 `num_splits`，以及它们如何驱动 split-kv 并行。
4. 看懂 `flash_mla_with_kvcache` 的最终组装：`partial` 绑定了哪些运行期常量、C++ 侧 `fwd_kvcache_mla` 如何启动主内核与 combine 内核。

## 2. 前置知识

- **MLA（Multi-head Latent Attention）**：u4-l4 已介绍，MLA 把 KV 压成一份共享潜变量 `KV`（同一张量既当 Key 打分又当 Value 求和），并额外配一段只参与打分的旋转位置编码 `q_pe`/`k_pe`。总维 `D` 拆成内容维 `DV`（也是输出维）与位置维 `PE_DIM = D - DV`。本讲的示例 `D=576, DV=512`，即 `PE_DIM=64`。
- **split-kv（u4-l3）**：解码时 Query 序列极短（`s_q=1`），沿 Query 方向没有并行度，于是把长 KV 序列切成多段（split），用 GPU 上多余的 SM 并行扫不同段，再用一个 combine 内核做 log-sum-exp 归约合并。
- **CuTe 后端（u5-l1）**：`backend="cute"` 经 `lower_cute` 把符号 DAG 发射成 CuTe C++ 片段，再用 `CuteAttnTemplate` 渲染整目录、用 `importlib` 加载 `flash_attn_interface.py`，内部经 `torch.utils.cpp_extension.load` 做 JIT 编译。
- **paged KV cache（分页显存）**：借鉴操作系统的分页思想，把一段连续逻辑 KV 序列拆成固定大小的「页」（block），物理上散落在显存，再用一张 `block_table` 记录「逻辑块号 → 物理块号」的映射。这样不同 batch 的不同长度 KV 可以紧凑复用显存，避免为最长序列预留整块空间。
- **TMA（Tensor Memory Accelerator）**：Hopper 架构的异步批量拷贝单元，CuTe 内核用它搬运 Q/K/V 分块；paged-kv 的间接寻址需要 TMA 描述符配合 `block_table` 完成。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [attention_engine/attn_engine/attn_engine.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py) | 引擎总入口。`backend=="cute"` 且 `kv_shared=True` 的分支在此：选模板目录、构造 `cache_seqlens`/`block_table`、调用 `get_mla_metadata`、用 `partial` 绑定 `flash_mla_with_kvcache`。 |
| [attn_script/mla_decode_cute.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mla_decode_cute.py) | 用户示例。组装 `qkv_meta`、`OnlineSoftmax`、构造 `mod`，并准备分页布局的 `q`/`KV` 张量。 |
| [attention_engine/core/template/cute_template_kvshared/flash_mla_interface.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/flash_mla_interface.py) | 渲染后的 Python 接口文件。定义 `get_mla_metadata` 与 `flash_mla_with_kvcache`，并 JIT 编译 C++ 扩展。 |
| [.../cute_template_kvshared/flash_api.cpp](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/flash_api.cpp) | C++ 入口。`get_mla_metadata`（计算 `num_sm_parts` 并启动元数据内核）、`mha_fwd_kvcache_mla`（校验、reshape、分配输出、启动主内核与 combine 内核）。 |
| [.../kernels/get_mla_metadata.cu](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/kernels/get_mla_metadata.cu) | 元数据计算内核。把总 KV 块数大致均摊到 `num_sm_parts` 个 SM 分区，写出 `tile_scheduler_metadata` 与 `num_splits` 前缀和。 |
| [.../kernels/params.h](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/kernels/params.h) | 参数结构体 `Flash_fwd_mla_params`、`Mla_metadata_params`，以及 `TileSchedulerMetaDataSize` 常量。 |
| [.../kernels/config.h](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/kernels/config.h) | 编译期常量：`BLOCK_SIZE_M`、`PAGE_BLOCK_SIZE`、`HEAD_DIM_K`、`HEAD_DIM_V`、`FIXED_OVERHEAD_NUM_BLOCKS`。 |
| [.../kernels/splitkv_mla.cu](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/kernels/splitkv_mla.cu) | split-kv 主内核。每个 SM 分区据元数据扫描自己的 KV 段，写部分输出或累加缓冲。 |
| [.../kernels/mla_combine.cu](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/kernels/mla_combine.cu) | combine 内核。沿 split 维做 log-sum-exp 归约，合并各段部分输出。 |

> 说明：本仓库里 `attention_engine/` 是 PYTHONPATH 根（不是 Python 包），因此下面引用源码时，相对路径以 `attention_engine/` 开头，但在仓库内的实际物理路径会再嵌一层 `attention_engine/`。永久链接已使用仓库根的完整相对路径，可直接点击。

## 4. 核心概念与源码讲解

### 4.1 CuTe MLA 解码入口：kv_shared 分流与「近静态内核」特性

#### 4.1.1 概念说明

回顾 u3-l3：引擎 `__init__` 先按 `backend`（`tl`/`cute`）分流，`tl` 路径再做形状分发，`cute` 路径则按 `kv_shared` 二选一。本讲聚焦的就是 `backend="cute"` 且 `kv_shared=True` 这一支——MLA 解码的 CuTe 实现。

这里有一个**贯穿全讲、最容易被忽略的关键事实**：与非 kv_shared 的 CuTe 路径（u5-l1）不同，kv_shared 的内核几乎是**手写静态**的。

- 非 kv_shared 的 [cute_template/](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template) 目录里有 **49 处** Jinja 占位符（分布在 10 个文件），`lower_cute` 产出的 `online_fwd_body`、`score_mod_code`、`final_rowscales_store_code_write` 等降级片段会被真正注入 C++ 内核。
- 而 [cute_template_kvshared/](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared) 整个目录里只有 **3 处**占位符：`{{dimqk}}`、`{{dimv}}`、`{{cutlass_dtype}}`（见 [config.h:L8-L9](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/kernels/config.h#L8-L9) 与 [flash_mla_interface.py:L24](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/flash_mla_interface.py#L24)）。

也就是说，`splitkv_mla.cu`、`mla_combine.cu` 这些 MLA 内核是 FlashMLA 的手写移植，**不消费** `lower_cute` 的符号降级产物；`lower_cute` 虽然仍被调用，但真正影响生成代码的只有编译期头维度与数据类型。`score_mod`（缩放）与 online softmax 的递推逻辑都被硬编码在这些 C++ 内核里。这是它和 `tl` MLA 解码（u4-l4，由 `lower_decode_mla` 渲染 TileLang 模板）最本质的区别：**CuTe MLA 走的是「换维度重编译」的近静态路线，而非「符号描述→整内核生成」的路线**。

#### 4.1.2 核心流程

`cute + kv_shared` 分支在构造期依次做四件事：

1. **选模板目录与输出目录**：模板目录固定为 `cute_template_kvshared`；输出目录按 `(dimqk, dimv)` 命名，避免不同头维度的编译产物互相覆盖。
2. **降级 + 渲染 + JIT**：调 `lower_cute`（实际只让 `dimqk/dimv/dtype` 生效），`CuteAttnTemplate` 渲染整目录，`importlib` 加载 `flash_mla_interface.py`，后者内部把 C++ JIT 编译成 CUDA 扩展。
3. **构造分页与元数据**：从 `qkv_meta` 抽形状，构造 `cache_seqlens`、`block_table`，调 `get_mla_metadata` 得到 `tile_scheduler_metadata` 与 `num_splits`。
4. **绑定可调用对象**：用 `functools.partial` 把上述运行期常量绑到 `flash_mla_with_kvcache` 上，挂成 `self.attention`，`self.block_mask` 置 `None`。

#### 4.1.3 源码精读

先看入口分流。`backend=="cute"` 时进入 [attn_engine.py:L138-L179](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L138-L179)。其中 `kv_shared` 决定模板目录、输出目录与接口文件名：

```python
# attn_engine.py:L145-L162
if not kv_shared:
    template_dir = .../cute_template
    file_path = os.path.join(OUTPUT_DIR, "flash_attn_interface.py")
else:
    template_dir = .../cute_template_kvshared
    dimqk = qkv_meta[0].shape[3]
    dimv  = qkv_meta[2].shape[3]
    OUTPUT_DIR = .../cute_template_output_{dimqk}_{dimv}
    file_path = os.path.join(OUTPUT_DIR, "flash_mla_interface.py")
```

`dimqk = qkv_meta[0].shape[3]`（Query 头维，含 PE，示例为 576），`dimv = qkv_meta[2].shape[3]`（Value 头维，示例为 512）。它们就是注入 [config.h](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/kernels/config.h#L8-L9) 的 `HEAD_DIM_K`/`HEAD_DIM_V`。

接着 [attn_engine.py:L167-L179](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L167-L179) 调 `lower_cute` 并用 `importlib` 加载接口文件。注意 `kv_shared` 分支加载的是 `flash_mla_interface.py`（对应 `flash_mla_with_kvcache`），而非 kv_shared 分支加载的是 `flash_attn_interface.py`（对应 `flash_attn_func`）。

`lower_cute` 本身在 [lower_cute.py:L244-L268](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_cute.py#L244-L268)，依旧把 `score_mod`/`online_func` 跑成 `LowerCuteOutput` 字段并交给 `CuteAttnTemplate` 渲染——但对 kv_shared 目录而言，这些字段没有占位符去接，渲染后等价于「只替换了 dimqk/dimv/dtype」。

> 这种「降级照跑、产物不用」的设计说明：MLA 的 CuTe 路径目前是**以固定内核为骨架、按头维度重编译**的工程快通道，符号可定制性主要留给 `tl` 后端与 non-kv_shared 的 CuTe 路径。

#### 4.1.4 代码实践

**实践目标**：确认「kv_shared 目录近静态」这一判断。

**操作步骤**：

1. 在仓库根用 ripgrep 统计两个目录的 Jinja 占位符数量（示例命令，需本地执行）：

   ```bash
   # kv_shared 目录：应只有 3 处（dimqk/dimv/cutlass_dtype）
   rg -c '\{\{[a-zA-Z_]' attention_engine/core/template/cute_template_kvshared
   # 非 kv_shared 目录：应有数十处
   rg -c '\{\{[a-zA-Z_]' attention_engine/core/template/cute_template
   ```

2. 打开 [splitkv_mla.cu](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/kernels/splitkv_mla.cu)，搜索 `{{`，确认主内核里没有任何降级占位符，online softmax 的 `m`/`r`/`lse` 递推是硬编码的。

**需要观察的现象**：kv_shared 目录占位符仅出现在 `config.h` 与 `flash_mla_interface.py`；主内核与 combine 内核是纯静态 C++。

**预期结果**：验证 4.1.1 的结论——CuTe MLA 的可定制面收敛到「头维度 + 数据类型」，而非用户的 `score_mod`/`online_func` 描述。若你修改用户侧 `score_mod` 的缩放系数，对 cute+kv_shared 路径**不会**改变生成内核（缩放走的是 `softmax_scale` 运行期参数，见 4.4）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 kv_shared 的输出目录要带 `_{dimqk}_{dimv}` 后缀，而非 kv_shared 的不带？

**参考答案**：因为 `HEAD_DIM_K`/`HEAD_DIM_V` 是 `config.h` 里的 `constexpr` 编译期常量，不同头维度会编译出不同的 C++ 模板实例（如 WGMMA 分块数 `HEAD_DIM_K/64` 不同），必须分目录存放各自的 JIT 产物与扩展名（扩展名也含 dimqk/dimv/dtype，见 [flash_mla_interface.py:L24](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/flash_mla_interface.py#L24)），否则会互相覆盖或加载错版本。非 kv_shared 路径头维度的处理方式不同（通过运行期/模板参数），故无需按维度分目录。

**练习 2**：如果用户的 `score_mod` 不是简单的 `score * scale`，而是带了 `tanh`，在 cute+kv_shared 路径下会生效吗？

**参考答案**：不会改变 `splitkv_mla.cu` 的生成代码——该内核没有占位符去接收 `score_mod_code`，打分变换是硬编码的 `Q@K * softmax_scale`。`tanh` 这类自定义只在 `tl` 后端（经 `lower_decode_mla`）或 non-kv_shared 的 CuTe 路径里才会真正注入内核。

---

### 4.2 paged-kv 组织：cache_seqlens / block_table / blocked_k

#### 4.2.1 概念说明

MLA 解码面对的是**一条很长的 KV cache**（示例 `seqlen_k=4096`）。如果为每个 batch 预留「最长序列长度 × 头维」的连续显存，在变长场景下浪费极大。paged-kv 的做法是：

- 把逻辑 KV 序列按 `page_block_size`（本路径固定 64）切成一串**页块**；
- 物理上把所有 batch 的页块紧凑铺在一个大张量 `k_cache`（形状 `num_blocks × page_block_size × num_heads_k × head_dim`）里；
- 用 `block_table`（形状 `batch × max_num_blocks_per_seq`，int32）记录每个 batch 的第几个逻辑块对应 `k_cache` 的哪个物理块；
- 用 `cache_seqlens`（形状 `batch`，int32）记录每个 batch 的**实际有效 KV 长度**，超出部分不参与计算（变长支持）。

这样内核通过 `block_table` 做间接寻址（paged addressing），用 TMA 按页搬运 KV，用 `cache_seqlens` 做越界掩码。

#### 4.2.2 核心流程

引擎构造期，从 `qkv_meta` 抽取形状后，按下列步骤搭出 paged-kv 的「索引侧」（张量数据由用户在调用时传入）：

1. `cache_seqlens = torch.full((b,), seqlen_k, int32)`：示例里所有 batch 等长，故填同一个 `seqlen_k`。
2. `max_seqlen = cache_seqlens.max()`；`max_seqlen_pad = ((max_seqlen + 255)//256)*256`：向上对齐到 256 的倍数（对齐粒度大于页块，便于布局）。
3. `block_size = 64`：与 `PAGE_BLOCK_SIZE` 一致。
4. `block_table = torch.arange(b * max_seqlen_pad // 64).view(b, max_seqlen_pad // 64)`：示例采用**恒等映射**——第 `i` 个 batch 的第 `j` 个逻辑块就是物理块 `i*(max_seqlen_pad//64) + j`，即各 batch 的 KV 在 `k_cache` 里顺序铺开。

用户侧（`mla_decode_cute.py`）则负责把真实数据按这个布局填进 `k_cache`：`KV` 形状 `(num_blocks, 64, h_kv, DV)`，再与 `k_pe` 沿最后一维拼成 `D=576`。

#### 4.2.3 源码精读

引擎侧的索引构造在 [attn_engine.py:L186-L206](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L186-L206)：

```python
# attn_engine.py:L187-L206
b = qkv_meta[0].shape[0]            # 128
s_q = qkv_meta[0].shape[2]          # 1
h_q = qkv_meta[0].shape[1]          # 128
h_kv = qkv_meta[2].shape[1]         # 1
seqlen_k = qkv_meta[2].shape[2]     # 4096
head_dim_v = qkv_meta[2].shape[3]   # 512
cache_seqlens = torch.full((b,), seqlen_k, dtype=torch.int32, device="cuda")
tile_scheduler_metadata, num_split = cute_attn.get_mla_metadata(
    cache_seqlens, s_q * h_q // h_kv, h_kv,
)
max_seqlen = cache_seqlens.max().item()
max_seqlen_pad = ((max_seqlen+255) // 256) * 256
block_size = 64
block_table = torch.arange(
    b * max_seqlen_pad // 64, dtype=torch.int32, device="cuda"
).view(b, max_seqlen_pad // 64)
```

示例形状代入：`max_seqlen_pad = ((4096+255)//256)*256 = 4096`（已是 256 倍数）；`block_table` 形状 `(128, 4096//64) = (128, 64)`，即每个 batch 64 个页块。

用户侧数据准备在 [mla_decode_cute.py:L295-L303](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mla_decode_cute.py#L295-L303)：

```python
# mla_decode_cute.py:L295-L303（示例代码，略改注释）
q   = torch.randn(B, 1, H, D, dtype=dtype, device="cuda")          # (128,1,128,576)
KV  = torch.randn(B*S//64, 64, G, DV, dtype=dtype, device="cuda")  # (8192,64,1,512) = k_cache 的 V 部分
k_pe = torch.randn(B*S//64, 64, G, D-DV, dtype=dtype, device="cuda") # (8192,64,1,64)  = PE 部分
KV = torch.concat([KV, k_pe], dim=-1).contiguous()                 # (8192,64,1,576) = k_cache
o = mod(q, KV)
```

注意 `KV` 的物理块数 `B*S//64 = 128*4096//64 = 8192`，恰等于 `block_table.numel()`——这正是「恒等映射、顺序铺开」布局的自洽条件。`KV` 末维 576 = `DV(512) + PE_DIM(64)`，与 `dimqk=576`、`dimv=512` 对应。

C++ 侧对这些索引的校验与消费在 [flash_api.cpp:L61-L131](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/flash_api.cpp#L61-L131)。其中 `kcache` 形状被解析为 `(num_blocks, page_block_size, num_heads_k, head_size)`（[L111-L114](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/flash_api.cpp#L111-L114)），并把 `block_table`、`page_block_size`、`seqlens_k` 写进参数结构体（[L171-L173](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/flash_api.cpp#L171-L173)）：

```cpp
// flash_api.cpp:L171-L173
params.block_table = block_table.data_ptr<int>();
params.block_table_batch_stride = block_table.stride(0);
params.page_block_size = page_block_size;
```

参数结构体里相关字段见 [params.h:L33-L40](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/kernels/params.h#L33-L40)：`block_table`、`block_table_batch_stride`、`page_block_size`、`seqlens_k_ptr`。主内核随后用 `block_table` 把「逻辑块号」翻译成 `k_cache` 的物理偏移，用 `seqlens_k_ptr` 做有效长度掩码。

#### 4.2.4 代码实践

**实践目标**：手算一组形状下的 paged-kv 索引张量，验证布局自洽。

**操作步骤**：

1. 设 `B=4, S=1024, page=64, h_kv=1, DV=512, D=576`。
2. 仿照 [attn_engine.py:L193-L206](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L193-L206) 写一小段（示例代码）：

   ```python
   import torch
   b, seqlen_k = 4, 1024
   cache_seqlens = torch.full((b,), seqlen_k, dtype=torch.int32)
   max_seqlen_pad = ((seqlen_k + 255)//256)*256
   block_table = torch.arange(b * max_seqlen_pad // 64).view(b, max_seqlen_pad // 64)
   print(cache_seqlens.shape, block_table.shape, block_table.numel())
   ```

3. 计算 `k_cache` 应有的物理块数，确认等于 `block_table.numel()`。

**需要观察的现象**：`cache_seqlens` 形状 `(4,)`；`max_seqlen_pad = 1024`；`block_table` 形状 `(4, 16)`，`numel = 64`；`k_cache` 块数应为 `4*1024//64 = 64`。

**预期结果**：`block_table.numel() == k_cache 的物理块数`，布局自洽。若 `seqlen_k` 改成 1100（非 256 倍数），`max_seqlen_pad` 会跳到 1280，`block_table` 第二维变大，但 `k_cache` 仍需提供 `b*max_seqlen_pad//64` 个物理块——多出的页块在 `cache_seqlens` 掩码下被忽略。**待本地验证**：在没有 GPU 的环境可直接运行上面纯 CPU 的索引构造部分。

#### 4.2.5 小练习与答案

**练习 1**：`block_table` 用 `torch.arange` 顺序生成意味着什么？真实部署里它会是什么样？

**参考答案**：`arange` 表示「逻辑块号 == 物理块号」的恒等映射，即各 batch 的 KV 在 `k_cache` 中顺序紧凑排列——这是示例/benchmark 的简化假设。真实部署里 KV cache 会随 token 增长动态分配页块，`block_table` 记录的是非连续的物理块号（可能复用已释放的页），映射不再是恒等。

**练习 2**：为什么 `cache_seqlens` 要单独传，而不是直接用 `k_cache` 的形状推断？

**参考答案**：因为 paged-kv 把所有 batch 的页块铺在一个大 `k_cache` 里，且按 `max_seqlen_pad` 对齐预留了空间，`k_cache` 的形状只能反映「最大预留」，无法得知每个 batch 的实际有效长度。`cache_seqlens` 显式给出每个 batch 的有效 KV 长度，内核据此做越界掩码，实现变长。

---

### 4.3 mla metadata 计算：tile_scheduler_metadata 与 num_split

#### 4.3.1 概念说明

解码场景 `s_q=1`，沿 Query 没有并行度，必须靠 **split-kv** 把长 KV 切给多个 SM 并行扫描。但 KV 是变长的（`cache_seqlens` 各不相同），如何把「整批所有 batch 的 KV 工作量」尽量均匀地分给 SM？这正是 `get_mla_metadata` 解决的问题。

它产出两样东西：

- **`tile_scheduler_metadata`**：形状 `(num_sm_parts, 8)`，int32。每个 SM 分区（sm_part）一条 8 元组记录，描述「这个分区负责扫描哪一段 KV」。有效字段是前 5 个（见 [params.h:L47-L48](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/kernels/params.h#L47-L48)）：

  ```
  [begin_idx, begin_seqlen, end_idx, end_seqlen, begin_n_split_idx, _, _, _]
  ```

  即「从第 `begin_idx` 个 batch 的第 `begin_seqlen` 个 token 开始，扫到第 `end_idx` 个 batch 的第 `end_seqlen` 个 token」连续的一段，`begin_n_split_idx` 记录起点 batch 内这是第几个 split。
- **`num_splits`**：形状 `(batch_size + 1,)`，int32，是各 batch 的 **split 数前缀和**。batch `i` 的 split 索引区间是 `[num_splits[i], num_splits[i+1])`。combine 内核据此定位每个 batch 的所有 split 去做归约。

#### 4.3.2 核心流程

元数据计算分两层：

**第一层（C++，[flash_api.cpp:L23-L59](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/flash_api.cpp#L23-L59)）**：确定 `num_sm_parts`（分多少个 SM 分区），再启动 GPU 内核。`num_sm_parts` 由 GPU 的 SM 数与头结构决定：

\[
\text{num\_sm\_parts} = \left\lfloor \frac{\text{sm\_count}}{\text{num\_heads\_k} \cdot \lceil \text{num\_heads\_per\_head\_k} \,/\, \text{BLOCK\_SIZE\_M} \rceil} \right\rfloor
\]

其中 `num_heads_per_head_k = s_q * h_q // h_kv`（每个 KV 头摊到的「等效 Query 行数」），`BLOCK_SIZE_M = 64`（见 [config.h:L5](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/kernels/config.h#L5)）。这个公式的直觉：主内核 grid 的前两维（`num_m_block × h_k`）已经占掉了「一个 SM 分区」需要同时处理的最小 Q 头组，剩余的 SM 才用来切 KV。

**第二层（CUDA 内核，[get_mla_metadata.cu:L8-L75](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/kernels/get_mla_metadata.cu#L8-L75)）**：单 warp（32 线程）先归约出整批的总 KV 块数 `total_num_blocks`，算出每个分区的目标工作量 `payload`，再线性扫描把连续的 KV 段分派给各分区，写出 8 元组与前缀和。

`payload` 的计算（[get_mla_metadata.cu:L34](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/kernels/get_mla_metadata.cu#L34)）：

\[
\text{payload} = \max\!\left( \left\lceil \frac{\text{total\_num\_blocks}}{\text{num\_sm\_parts}} \right\rceil + \text{fixed\_overhead},\; 2 \cdot \text{fixed\_overhead} \right)
\]

其中 `fixed_overhead = FIXED_OVERHEAD_NUM_BLOCKS = 5`（[config.h:L11](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/kernels/config.h#L11)）。加 `fixed_overhead` 是为每个分区的边界 batch 预留冗余块（一个分区可能跨 batch，跨 batch 时起点 batch 需要被完整重扫一遍其 Q，故有固定开销）。

#### 4.3.3 源码精读

Python 接口只是透传（[flash_mla_interface.py:L55-L70](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/flash_mla_interface.py#L55-L70)）：

```python
# flash_mla_interface.py:L55-L70
def get_mla_metadata(cache_seqlens, num_heads_per_head_k, num_heads_k):
    return flash_mla_cuda.get_mla_metadata(cache_seqlens, num_heads_per_head_k, num_heads_k)
```

引擎侧调用见 [attn_engine.py:L194-L198](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L194-L198)，第二个参数是 `s_q * h_q // h_kv = 1*128//1 = 128`。

C++ 侧计算 `num_sm_parts` 并启动内核（[flash_api.cpp:L37-L56](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/flash_api.cpp#L37-L56)）：

```cpp
// flash_api.cpp:L38-L42
int sm_count = dprops->multiProcessorCount;
int num_sm_parts = sm_count / num_heads_k
                 / cutlass::ceil_div(num_heads_per_head_k, Config::BLOCK_SIZE_M);
auto tile_scheduler_metadata = torch::empty({num_sm_parts, TileSchedulerMetaDataSize}, options);
auto num_splits = torch::empty({batch_size + 1}, options);
```

内核里的分派循环（[get_mla_metadata.cu:L36-L67](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/kernels/get_mla_metadata.cu#L36-L67)）逐分区消耗 `payload`：能装下整个剩余 batch 就推进 `now_idx` 并累加 `cum_num_splits`；装不下就让当前 batch 产生一个新 split（`++now_n_split_idx`），分区边界落在该 batch 中段。最终 `num_splits_shared[batch_size]` = 全部分区的 split 总数。`tile_scheduler_metadata0[0..3]` 写的就是 `[begin_idx, begin_seqlen, end_idx, end_seqlen]`，第 5 个字段写 `begin_n_split_idx`（[L39-L42, L63-L66](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/kernels/get_mla_metadata.cu#L36-L67)）。

主内核如何消费这些元数据：`partition_idx = blockIdx.z`，每分区读自己的 8 元组确定 KV 扫描区间（[splitkv_mla.cu:L1127-L1148](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/kernels/splitkv_mla.cu#L1127-L1148)）：

```cpp
// splitkv_mla.cu:L1127-L1148
int *tile_scheduler_metadata_ptr = params.tile_scheduler_metadata_ptr
                                 + partition_idx * TileSchedulerMetaDataSize;
int4 tile_scheduler_metadata = *(reinterpret_cast<int4 *>(tile_scheduler_metadata_ptr));
int begin_idx      = tile_scheduler_metadata.x;   // 起点 batch
int begin_seqlen   = tile_scheduler_metadata.y;   // 起点 token
int end_idx        = tile_scheduler_metadata.z;   // 终点 batch
int end_seqlen     = tile_scheduler_metadata.w;   // 终点 token
...
int begin_n_split_idx = *(tile_scheduler_metadata_ptr + 4);
...
const bool is_no_split = start_block_idx == 0 && end_block_idx == cute::ceil_div(seqlen_k, kBlockN);
```

`is_no_split` 表示该分区独占了整个 batch 的 KV（没有跨分区切分），此时可直接写最终输出 `o_ptr`/`softmax_lse_ptr`；否则写累加缓冲 `oaccum_ptr`/`softmax_lseaccum_ptr`，split 索引取 `num_splits_ptr[batch_idx] + n_split_idx`（[splitkv_mla.cu:L1364](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/kernels/splitkv_mla.cu#L1364)），等 combine 内核合并。

#### 4.3.4 代码实践

**实践目标**：手算示例形状下的 `num_sm_parts` 与 `payload`。

**操作步骤**：

1. 取示例 `h_q=128, h_kv=1, s_q=1, B=128, seqlen_k=4096, page=64`，H100 的 `sm_count=132`，`BLOCK_SIZE_M=64`。
2. 算 `num_heads_per_head_k = s_q*h_q//h_kv = 128`。
3. 代入 4.3.2 的 `num_sm_parts` 公式。
4. 算 `total_num_blocks = B * ceil(seqlen_k/page) = 128 * 64 = 8192`，再算 `payload`（`fixed_overhead=5`）。

**需要观察的现象 / 预期结果**：

- `num_sm_parts = 132 / 1 / ceil(128/64) = 132 / 2 = 66`。
- `payload = max(ceil(8192/66) + 5, 10) = max(124 + 5, 10) = 129`（块）。
- 因每个 batch 的 64 块远小于 129，绝大多数分区会独占若干完整 batch（`is_no_split` 命中），只有跨 batch 边界的少数分区会产生 split。**待本地验证**：在带 GPU 的环境运行 `mla_decode_cute.py`，取消注释 [attn_engine.py:L199-L200](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L199-L200) 的 `print("num_split", num_split)` 可观察实际 `num_splits` 张量。

#### 4.3.5 小练习与答案

**练习 1**：`num_splits` 为什么是 `batch_size + 1` 维而不是 `batch_size` 维？

**参考答案**：它是前缀和（inclusive 累加前的哨兵为 0），用「左闭右开」区间 `[num_splits[i], num_splits[i+1])` 表示 batch `i` 的 split 索引范围。多出一个元素存放「batch 0 之前 = 0」与「最后一个 batch 之后 = 总 split 数」，这样 combine 内核用 `start = num_splits[batch]`、`end = num_splits[batch+1]` 即可定位，无需特判边界（见 [mla_combine.cu:L25-L27](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/kernels/mla_combine.cu#L25-L27)）。

**练习 2**：如果 `seqlen_k` 很短（比如 `s_q=1, seqlen_k=64`，正好一个页块），`num_splits` 会是什么？

**参考答案**：每个 batch 只有 1 个 KV 块，`payload` 足够大让多数分区独占若干完整 batch，每个 batch 几乎只产生 1 个 split。于是 `my_num_splits = end - start == 1`，combine 内核的早退分支命中（[mla_combine.cu:L29-L31](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/kernels/mla_combine.cu#L29-L31)），直接跳过合并——这正是 u4-l4 提到的「`num_split==1` 时 combine 退化为恒等而省略」。

---

### 4.4 flash_mla_with_kvcache 组装：partial 绑定、splitkv 主内核与 combine 合并

#### 4.4.1 概念说明

前三模块解决了「索引怎么搭」「工作量怎么分」。最后一步是把它们组装成一个对用户而言只需 `mod(q, KV)` 就能调用的算子。引擎用 `functools.partial` 把所有「构造期就能确定的运行期常量」绑死，只留 `q` 与 `k_cache` 给运行期。被绑定的 `flash_mla_with_kvcache` 内部再做三件事：

1. **reshape Query**：把 `(B, s_q, h_q, D)` 重排成按 KV 头组织的 `(B, s_q*h_q//h_kv, h_kv, D)`，让一个 KV 头的所有 Q 头组连续。
2. **分配输出与累加缓冲**：`out`、`softmax_lse`（最终结果），以及 `out_accum`、`softmax_lse_accum`（各 split 的部分结果，尺寸按 `total_num_splits = batch_size + num_sm_parts` 预留上界）。
3. **依次启动两个内核**：split-kv 主内核 `run_flash_splitkv_mla_kernel`（各分区并行扫 KV 段、算部分 softmax 与部分输出），combine 内核 `run_flash_mla_combine_kernel`（沿 split 维做 log-sum-exp 归约、合并部分输出）。两者用 **PDL（Programmatic Dependent Launch）** 重叠调度，combine 在主内核收尾时即可提前启动。

combine 的数学核心与 u4-l4 一致：对每个 batch 的多个 split，先求各 split 的 `lse` 最大值 `lse_max`，再算全局 `lse` 与每个 split 的权重 `o_scale`：

\[
\text{lse}^* = \log_2\!\left( \sum_s 2^{\,\text{lse}_s - \text{lse}_{\max}} \right) + \text{lse}_{\max}, \qquad
\text{o\_scale}_s = 2^{\,\text{lse}_s - \text{lse}^*}
\]

最终输出 \( O = \sum_s \text{o\_scale}_s \cdot O_s \)。注意 CuTe 内核在 **log2 域**用 `exp2f`/`log2f`（GPU 快速指令），而用户层 `OnlineSoftmax.combine`（[mla_decode_cute.py:L58-L65](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mla_decode_cute.py#L58-L65)）在自然域用 `exp`/`log`——二者数学等价，差异只在换底常数（如 u2-l4 所述）。

#### 4.4.2 核心流程

构造期绑定 → 运行期调用 → C++ 入口 → 双内核：

```
mod(q, KV)
  └─(partial 已绑 cache_seqlens/block_table/head_dim_v/tile_scheduler_metadata/num_splits/causal)
   └─ flash_mla_with_kvcache(q, KV, ...)                 # Python, flash_mla_interface.py:L73
       └─ flash_mla_cuda.fwd_kvcache_mla(...)             # C++, flash_api.cpp:L61 (mha_fwd_kvcache_mla)
           ├─ reshape q → (B, q_seq_per_hk, h_k, D)        # flash_api.cpp:L123-L124
           ├─ alloc out / softmax_lse / out_accum / lse_accum   # L136-L186
           ├─ run_flash_splitkv_mla_kernel<...>           # splitkv_mla.cu  grid: (num_m_block, h_k, num_sm_parts)
           └─ run_flash_mla_combine_kernel<...>           # mla_combine.cu  grid: (B, ceil(h_k*q_seq_per_hk/8), 1)
```

主内核 grid 第三维是 `num_sm_parts`（分区并行），combine 内核 grid 第三维为 1（按 batch × Q 头块合并）。`num_m_block = ceil(q_seq_per_hk / BLOCK_SIZE_M)`。

#### 4.4.3 源码精读

构造期的 `partial` 绑定在 [attn_engine.py:L207-L215](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L207-L215)：

```python
# attn_engine.py:L207-L215
self.attention = partial(
    cute_attn.flash_mla_with_kvcache,
    cache_seqlens=cache_seqlens,
    block_table=block_table,
    head_dim_v=head_dim_v,
    tile_scheduler_metadata=tile_scheduler_metadata,
    num_splits=num_split,
    causal=True if mask_mod is not None else False)
self.block_mask = None
```

注意 `num_splits=num_split`：变量名 `num_split`（单数）是 `get_mla_metadata` 返回的前缀和张量，而形参名 `num_splits`（复数）。`mask_mod=None` 时 `causal=False`（示例即如此）。

Python 接口的默认 `softmax_scale` 与 C++ 调用在 [flash_mla_interface.py:L100-L113](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/flash_mla_interface.py#L100-L113)：

```python
# flash_mla_interface.py:L100-L113
if softmax_scale is None:
    softmax_scale = q.shape[-1] ** (-0.5)     # 默认 1/sqrt(D)
out, softmax_lse = flash_mla_cuda.fwd_kvcache_mla(
    q, k_cache, head_dim_v, cache_seqlens, block_table,
    softmax_scale, causal, tile_scheduler_metadata, num_splits,
)
return out, softmax_lse
```

**关键点**：用户 `score_mod` 里的 `softmax_scale = 1/D**0.5`（[mla_decode_cute.py:L16, L19](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mla_decode_cute.py#L16)）在 cute+kv_shared 路径**并未**进入内核的打分逻辑——打分缩放走的是这里的运行期参数 `softmax_scale`，C++ 再换算成 `scale_softmax_log2 = softmax_scale * M_LOG2E`（[flash_api.cpp:L153-L154](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/flash_api.cpp#L153-L154)）。这再次印证 4.1 的结论：用户符号描述不直接驱动此内核。

C++ 入口 `mha_fwd_kvcache_mla` 做 reshape、分配与启动（[flash_api.cpp:L120-L131](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/flash_api.cpp#L120-L131) 与 [L179-L202](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/flash_api.cpp#L179-L202)）：

```cpp
// flash_api.cpp:L120-L124  reshape q：把 h_q 维拆成 (h_k, h_q/h_k) 再转置
const int num_q_heads_per_hk = num_heads_q / num_heads_k;      // 128/1 = 128
const int q_seq_per_hk = seqlen_q_ori * num_q_heads_per_hk;    // 1*128 = 128
q = q.view({batch_size, seqlen_q_ori, num_heads_k, num_q_heads_per_hk, head_size_k})
     .transpose(2,3).reshape({batch_size, q_seq_per_hk, num_heads, head_size_k});
```

```cpp
// flash_api.cpp:L179-L192  预留累加缓冲并启动双内核
const int total_num_splits = batch_size + params.num_sm_parts;
at::Tensor softmax_lse_accum = torch::empty({total_num_splits, num_heads, q_seq_per_hk}, ...);
at::Tensor out_accum         = torch::empty({total_num_splits, num_heads, q_seq_per_hk, head_size_v}, ...);
...
run_flash_splitkv_mla_kernel<cutlass::bfloat16_t>(params, stream);   // 主内核
run_flash_mla_combine_kernel<cutlass::bfloat16_t>(params, stream);   // combine
```

主内核的 grid 与 launch（[splitkv_mla.cu:L1455-L1467](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/kernels/splitkv_mla.cu#L1455-L1467）：`dim3(num_m_block, params.h_k, params.num_sm_parts)`。combine 内核的早退与归约在 [mla_combine.cu:L25-L104](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/kernels/mla_combine.cu#L25-L104)：

```cpp
// mla_combine.cu:L25-L31  只有多 split 才需要合并
const int my_num_splits = end_split_idx - start_split_idx;
if (my_num_splits == 1) { return; }
```

```cpp
// mla_combine.cu:L80-L99  log2 域的 lse_max → sum → global_lse（等价于 4.4.1 的公式）
float max_lse = -INFINITY;
for (...) max_lse = max(max_lse, local_lse[i]);          // lse_max（warp 归约）
float sum_lse = 0;
for (...) sum_lse = sum_lse + exp2f(local_lse[i] - max_lse);
float global_lse = (sum_lse == 0.f || ...) ? INFINITY : log2f(sum_lse) + max_lse;
if (lane_idx == 0) gLse(warp_idx) = global_lse / (float)M_LOG2E;   // 落盘换回自然域
for (...) sLseScale(warp_idx, split_idx) = exp2f(local_lse[i] - global_lse);  // o_scale_s
```

注意 `gLse(...) = global_lse / M_LOG2E`：内核内部全程 log2 域，落盘 `softmax_lse` 时除以 `log2(e)` 换回自然对数，与 PyTorch 参考实现的 `logsumexp` 对齐。

#### 4.4.4 代码实践

**实践目标**：跟读 `mod(q, KV)` 的完整调用链，确认 partial 绑定与双内核启动。

**操作步骤**：

1. 在 [attn_engine.py:L207-L215](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L207-L215) 确认 `partial` 绑定的 6 个关键字参数；说明运行期 `mod(q, KV)` 只补齐 `q` 与 `k_cache` 两个位置参数。
2. 在 [flash_api.cpp:L188-L202](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/flash_api.cpp#L188-L202) 确认按 dtype 分支依次启动 `run_flash_splitkv_mla_kernel` 与 `run_flash_mla_combine_kernel`。
3. 跟踪 `softmax_scale` 的流向：用户 `score_mod` 的 `1/sqrt(D)` → Python 默认值 → C++ `scale_softmax_log2 = softmax_scale * M_LOG2E` → 主内核打分。

**需要观察的现象**：调用 `mod(q, KV)` 返回 `(out, softmax_lse)`，`out` 形状 `(B, s_q, h_q, DV) = (128,1,128,512)`，`softmax_lse` 形状 `(B, h_q, s_q) = (128,128,1)`（见 [flash_api.cpp:L204-L207](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/flash_api.cpp#L204-L207) 的逆 reshape）。

**预期结果 / 待本地验证**：在 H100（sm_90a）+ bfloat16 环境运行 `mla_decode_cute.py`，应能跑通并打印 latency/tflops；`test_mod` 里的 `cal_diff(lse_flash, lse_torch, "lse")` 应通过（`cos_diff < 1e-5`）。若无 H100，C++ 会因 [flash_api.cpp:L75-L76](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/flash_api.cpp#L75-L76) 的 `is_sm90` 校验失败——这是硬件约束，属正常。

#### 4.4.5 小练习与答案

**练习 1**：为什么累加缓冲 `out_accum`/`softmax_lse_accum` 的第一维是 `total_num_splits = batch_size + num_sm_parts`，而不是「所有 batch 的 split 总数」？

**参考答案**：这是一个**安全上界**。在最坏情况下，每个 batch 都可能被切到多个 split，且分区数有 `num_sm_parts` 个。用 `batch_size + num_sm_parts` 作为缓冲上界（而不是精确的 split 总数）可以提前静态分配、避免逐次动态分配；每个 split 的实际落点由 `num_splits_ptr[batch_idx] + n_split_idx` 索引定位，未用到的槽位被忽略。

**练习 2**：combine 内核里 `gLse(warp_idx) = global_lse / M_LOG2E` 为什么要除以 `M_LOG2E`？

**参考答案**：内核全程在 log2 域计算（`exp2f`/`log2f` 是 GPU 快速指令），得到的 `global_lse` 是 \(\log_2(\sum p)\)。而返回给用户的 `softmax_lse` 约定为自然对数 \(\ln(\sum p)\)（与 PyTorch `logsumexp`、flash-attn 一致）。由 \(\ln x = \log_2 x / \log_2 e\)，除以 `M_LOG2E`（即 \(\log_2 e\)）即换回自然对数，保证与参考实现可直接 `cal_diff` 对齐。

---

## 5. 综合实践

**任务**：完整跟读 `attn_engine.py` 的 `cute + kv_shared` 分支，串起本讲三个最小模块，回答「构造一个 MLA CuTe 引擎时，分页索引与调度元数据是怎么一步步搭出来的，又从哪里来」。

**步骤**：

1. 从 [mla_decode_cute.py:L271-L293](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mla_decode_cute.py#L271-L293) 出发，记录传入的 `qkv_meta` 三个 `meta_tensor` 的形状（`(128,128,1,576)`、`(128,1,4096,576)`、`(128,1,4096,512)`），以及 `kv_shared=True, backend="cute"`。
2. 进入 [attn_engine.py:L138-L162](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L138-L162)，说明 `kv_shared=True` 如何把 `template_dir` 指向 `cute_template_kvshared`、`OUTPUT_DIR` 变成 `cute_template_output_576_512`、`file_path` 指向 `flash_mla_interface.py`。
3. 走 [attn_engine.py:L186-L206](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L186-L206)，逐行解释四个索引张量的构造：
   - `cache_seqlens`：`torch.full((128,), 4096, int32)`，来自 `seqlen_k = qkv_meta[2].shape[2]`；
   - `max_seqlen_pad`：`((4096+255)//256)*256 = 4096`；
   - `block_size`：硬编码 `64`（= `PAGE_BLOCK_SIZE`）；
   - `block_table`：`arange(128*64).view(128,64)`，恒等映射布局。
4. 指出 `tile_scheduler_metadata` 与 `num_split` **来自** [attn_engine.py:L194-L198](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L194-L198) 对 `cute_attn.get_mla_metadata(cache_seqlens, 128, 1)` 的调用，进而由 [flash_api.cpp:L37-L56](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/flash_api.cpp#L37-L56) 算出 `num_sm_parts` 并启动 [get_mla_metadata.cu](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/kernels/get_mla_metadata.cu#L8-L75) 写出。
5. 最后看 [attn_engine.py:L207-L215](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L207-L215) 的 `partial`，说明运行期 `mod(q, KV)` 只需补 `q`/`k_cache`，其余已绑死。

**产出**：一张「字段 → 取值 → 来源行号」的表，例如：

| 字段 | 示例取值 | 来源 |
| --- | --- | --- |
| `cache_seqlens` | `(128,)` 全 4096 | attn_engine.py:L193 |
| `max_seqlen_pad` | 4096 | attn_engine.py:L202 |
| `block_size` | 64 | attn_engine.py:L203 |
| `block_table` | `(128,64)` arange | attn_engine.py:L204-L206 |
| `tile_scheduler_metadata` | `(66, 8)` | get_mla_metadata，经 attn_engine.py:L194-L198 |
| `num_split` | `(129,)` 前缀和 | 同上 |

（`num_sm_parts=66` 见 4.3.4 手算；`num_splits` 形状为 `batch_size+1=129`。）

## 6. 本讲小结

- `cute + kv_shared` 分支在 [attn_engine.py:L186-L215](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L186-L215) 完成：选 kvshared 模板目录（按 `dimqk_dimv` 分输出目录）→ `lower_cute` 渲染 → `importlib` 加载 `flash_mla_interface.py` → 构造分页索引与元数据 → `partial` 绑定 `flash_mla_with_kvcache`。
- **关键特性**：`cute_template_kvshared` 目录只有 3 个 Jinja 占位符（`dimqk/dimv/cutlass_dtype`），主内核与 combine 内核是 FlashMLA 的手写静态移植，**不消费** `lower_cute` 的符号降级产物；这与 non-kv_shared CuTe（49 占位符）和 `tl` MLA 解码（`lower_decode_mla` 渲染）都不同。
- **paged-kv**：用 `cache_seqlens`（每 batch 有效 KV 长度）、`block_table`（逻辑块→物理块映射，示例为恒等映射）、`page_block_size=64` 组织变长 KV cache，内核靠 `block_table` 间接寻址、`seqlens_k` 越界掩码。
- **元数据**：`get_mla_metadata` 由 C++ 算 `num_sm_parts = sm_count / h_k / ceil(num_heads_per_head_k / BLOCK_SIZE_M)`，再由 CUDA 内核把总 KV 块数按 `payload` 均摊到各分区，产出 `tile_scheduler_metadata[num_sm_parts][8]`（每分区的 KV 段）与 `num_splits[batch+1]`（split 前缀和）。
- **组装**：`partial` 绑定 6 个运行期常量；C++ `mha_fwd_kvcache_mla` reshape Q、按 `total_num_splits = batch_size + num_sm_parts` 预留累加缓冲，依次启动 split-kv 主内核（grid 第三维 `num_sm_parts`）与 combine 内核（log2 域 log-sum-exp 归约，`num_splits==1` 时早退）。
- **与用户描述解耦**：`score_mod` 的缩放在此路径不进内核代码，而是走运行期 `softmax_scale` 参数；用户层 `OnlineSoftmax.combine`（自然域）与手写 combine macro（log2 域）数学等价。

## 7. 下一步学习建议

- **回到主内核细节**：本讲把 [splitkv_mla.cu](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/cute_template_kvshared/kernels/splitkv_mla.cu) 当黑盒，只读了它的调度入口。若想深入 Hopper 优化，建议通读该内核的 WGMMA mainloop、TMA 双缓冲（`barriers_K0/K1`）、以及 `get_AorC_row_idx` 等 fragment 布局辅助函数——这属于 CUTLASS/CuTe 的进阶话题。
- **对比 tl MLA 解码**：重读 u4-l4 与 `lower_decode_mla`，对比「符号降级生成 TileLang MLA 内核」与本讲「手写静态 CuTe MLA 内核」两条路线的工程取舍（灵活度 vs. 极致性能）。
- **autotuner 视角**：本路径的 `BLOCK_SIZE_M`、`PAGE_BLOCK_SIZE`、`FIXED_OVERHEAD_NUM_BLOCKS` 都是编译期/硬编码常量。学完 u5-l3（autotuner）后，可思考这些常量能否纳入调参空间，以及 `num_sm_parts` 公式如何随硬件 `sm_count` 变化。
- **正确性与基准**：结合 u5-l4，把 `mla_decode_cute.py` 的 `test_mod`/`test_flash_mla` 作为对齐入口，理解 `cal_diff`（余弦/RMSE/amax）与 `do_bench` 的计时方法在 MLA 解码场景的应用。
