# Sparse decode 接口与 DecodeFeatures 派发

## 1. 本讲目标

本讲聚焦 FP8 稀疏解码（sparse decode）的 C++ 接口函数 `sparse_attn_decode_interface`，它是 Python 端 `flash_mla_with_kvcache(..., indices=...)` 与底层 SM90/SM100 kernel 之间的「校验 + 编排 + 派发」中枢。学完后你应当能够：

- 看懂接口函数对张量形状、dtype、layout、FP8 字节布局的层层校验，并能讲清「需求 feature 集合」是如何从输入张量构造出来的。
- 理解 `DecodeImplBase` / `ImplBase` 派发框架在 sparse decode 上的具体落地：四个实现类各自声明的能力清单，以及 `arch × h_q × d_qk` 如何选中其中一个。
- 说清楚为什么 SM100f + `h_q=128` + `d_qk=576`（即 V3.2 形状）会被 `Decode_Sm100_Head64x2_Impl` 接管，以及它如何把 q / lse / out 按头切成两段、各跑一次 head64 kernel。
- 掌握 `extra_kv` / `extra_indices` / `topk_length` / `extra_topk_length` 的语义、配对约束与对应的 feature 开关。

本讲承接 [u2-l4 ImplBase 派发框架](./u2-l4-implbase-dispatcher.md)（理解 `ImplBase`、`DECLARE_SUPPORTED_FEATURES`、feature 子集校验）与 [u5-l3 Crossover 与 DSM](./u5-l3-crossover-dsm.md)（理解 sparse decode 的 kernel 内部机制），是它们的「对外接口层」收尾。

## 2. 前置知识

阅读本向前，请确认你已了解以下概念（在 u1/u2/u5 前置讲义中已建立）：

- **四类 kernel 与支持矩阵**：sparse decode 在 SM90（Hopper）与 SM100（Blackwell）两代架构上都有实现，但实现方式不同。
- **MLA 的两种模式**：V3.2 风格 `d_qk=576`（含 64 维 RoPE，记为 `ModelType::V32`）与 MODEL1 风格 `d_qk=512`（记为 `ModelType::MODEL1`）；两者 FP8 KV cache 的字节布局不同（详见 [u5-l1 FP8 KV Cache 布局](./u5-l1-fp8-kvcache-format.md)）。
- **MQA 约束**：sparse decode 强制 `h_kv == 1`，即所有 query 头共享同一份 KV。
- **ImplBase 派发框架**：每个实现 = 它所支持的 feature 子集；派发 = 校验「需求集合 ⊆ 支持集合」。
- **DISPATCH 宏**：把运行时值（如 `model_type`、`num_heads`）编译期化，为每个合法取值实例化一份模板 kernel。
- **三段式解码**：`get_decoding_sched_meta → 主 kernel → combine`，主 kernel 多 split 写 accumulate 缓冲，combine 归并（详见 [u4-l1](./u4-l1-splitkv-buffers.md) / [u4-l2](./u4-l2-combine-kernel.md)）。

> 名词速查：**lse** = log-sum-exp；**attn_sink** = 注意力汇点，用于把输出按 \( \frac{\exp(\text{lse})}{\exp(\text{lse})+\exp(\text{sink})} \) 缩放；**topk** = 每个 query 实际参与的 KV block 数；**split-KV** = 把长 KV 切给多个 SM partition 并行。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [csrc/api/sparse_decode.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h) | 本讲主角。定义 `DecodeFeatures` 枚举、四个 `DecodeImplBase` 实现类，以及接口函数 `sparse_attn_decode_interface`。 |
| [csrc/api/common.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h) | 提供 `Arch`（架构检测）、`int64_stride_to_int`、`DISPATCH_*` 宏、`ImplBase` 基类与 `DECLARE_SUPPORTED_FEATURES` 宏。 |
| [csrc/params.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h) | 定义 `SparseAttnDecodeParams`（接口→kernel 的数据契约）、`ModelType`、`DecodingSchedMeta` 等。 |
| [flash_mla/flash_mla_interface.py](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py) | Python 包装层，按是否传 `indices` 二分派发到 `sparse_decode_fwd`。 |
| [csrc/sm100/decode/head64/kernel.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/decode/head64/kernel.h) | SM100 head64 sparse decode kernel 的启动声明，head64 与 head64x2 两条路径都最终调用它。 |

## 4. 核心概念与源码讲解

### 4.1 接口函数：校验与 feature 构造

#### 4.1.1 概念说明

`sparse_attn_decode_interface` 是一个纯 host 函数，职责是：拿到 Python 传来的张量 → 严格校验 → 装配 `SparseAttnDecodeParams` → 选实现并启动 kernel → 调 combine → 返回结果。它**不直接 launch kernel**，而是把「这次调用到底需要哪些能力」打包成一个 `DecodeFeatures` 集合，交给派发器去匹配实现。

关键直觉：**「需求 feature 集合」是对输入张量形态的精确描述**。比如传了 `attn_sink` 张量，就往集合里加 `ATTN_SINK`；`h_q==128` 就加 `HEAD_128`。这样派发器只要问「哪个实现的能力清单 ⊇ 需求集合」即可，不必在每个分支里重复写 if-else。

#### 4.1.2 核心流程

接口函数的执行顺序（对应源码行号见 4.1.3）：

1. **架构检测**：构造 `Arch`，缓存 `major/minor/num_sms`。
2. **维度抽取**：从 `q.size(...)`、`kv.size(...)`、`indices.size(...)` 读出 `b / s_q / h_q / d_qk / num_blocks / page_block_size / h_kv / topk` 等。
3. **元数据健全性检查**（`TORCH_CHECK`）：`h_kv==1`、`d_qk∈{576,512}`、`d_v==512`、`topk>0`，以及 extra KV 的配对约束。
4. **device / dtype / layout / shape 校验**（`KU_CHECK_*` 宏），其中 FP8 字节布局由 `bytes_per_token` 公式校验。
5. **分配输出**：`out[b,s_q,h_q,d_v]`、`lse[b,s_q,h_q]`。
6. **推断 `model_type`**：`d_qk==576→V32`，`d_qk==512→MODEL1`。
7. **构造需求 feature 向量**：逐项 `push_back`。
8. **选实现**（4.2 详述）。
9. **装配 `params`、生成 sched_meta、分配 accumulate 缓冲、`impl->run`、combine**。

#### 4.1.3 源码精读

函数签名与参数注释（每个张量的形状与含义都写在这里，是阅读本讲最重要的入口）：

[csrc/api/sparse_decode.h:183-197](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L183-L197) —— 接口签名：`q[b,s_q,h_q,d_qk]`、`kv[num_blocks,page_block_size,h_kv,d_qk]`、`indices[b,s_q,topk]`，以及一系列 `optional` 张量（`topk_length` / `attn_sink` / `tile_scheduler_metadata` / `num_splits` / `extra_kv` / `extra_indices` / `extra_topk_length`）。

架构检测与基本维度抽取：

[csrc/api/sparse_decode.h:200-214](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L200-L214) —— 构造 `Arch`，校验三者的 ndim，抽出 `b/s_q/h_q/d_qk/num_blocks/page_block_size/h_kv/topk`。

元数据健全性检查（硬约束在此体现）：

[csrc/api/sparse_decode.h:231-244](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L231-L244) —— `h_kv==1`（仅支持 MQA）、`d_qk∈{576,512}`、`d_v==512`；以及 extra KV 的配对规则：`extra_kv` 必须与 `extra_indices` 同生共灭，`extra_topk_length` 不得脱离 `extra_kv` 单独出现。

FP8 字节布局校验（接口层强制 [u5-l1](./u5-l1-fp8-kvcache-format.md) 定义的字节结构）：

[csrc/api/sparse_decode.h:288-305](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L288-L305) —— 按 `(d_qk,d_v)` 算出 `bytes_per_token`：V3.2 为 \(512+64\times2+(512/128)\times4=656\) 字节，MODEL1 为 \(448+64\times2+(448/64)\times1+1=584\) 字节（与 [u5-l1](./u5-l1-fp8-kvcache-format.md) 的布局定义一致）；并要求整个 block 在 `stride(1)` 上连续。

`model_type` 推断与需求 feature 向量构造（本模块的核心）：

[csrc/api/sparse_decode.h:318-360](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L318-L360) —— 先由 `d_qk` 定 `model_type`，再把「头数 / 头维 / 模型类型 / 是否有 attn_sink / 是否有 topk_length / 是否有 extra KV / 是否有 extra topk_length」逐项翻译成 `DecodeFeatures` 枚举压入 `features`。注意三处 `TORCH_CHECK(false)`：`h_q` 只允许 64 或 128，`d_qk` 只允许 576 或 512——这就是「合法配置空间」的边界。

> **关于 `topk_length` 的形状**：C++ 参数注释写作 `// [b, s_q]`，但实际校验是 `KU_CHECK_SHAPE(topk_length, b)`，要求形状恰为 `[b]`（即每个 batch 一个有效长度）。该校验宏定义见 [csrc/kerutils/include/kerutils/supplemental/torch_tensors.h:62](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/supplemental/torch_tensors.h#L62)，参考实现 `ref.py` 也按 `[b]` 使用（`.view(b,1,1)`）。注释已过时，以代码为准。

#### 4.1.4 代码实践

**实践目标**：亲手把「输入张量 → 需求 feature 集合」的翻译逻辑在 Python 里复刻一遍，验证你理解了 4.1.3 的构造过程。本实践纯 CPU 可运行，不需要 GPU。

**操作步骤**：

1. 阅读上面的 [sparse_decode.h:318-360](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L318-L360)。
2. 运行下面这段「需求 feature 构造器」示例代码（与源码逻辑一一对应）：

```python
# 示例代码：复刻 sparse_decode.h 的 feature 构造逻辑（纯 CPU）
def build_required_features(h_q, d_qk, have_attn_sink, have_topk_length,
                            have_extra_kcache, have_extra_topk_length):
    model_type = "V32" if d_qk == 576 else "MODEL1" if d_qk == 512 else None
    assert model_type is not None, f"Unsupported d_qk: {d_qk}"

    feats = []
    # 头数
    feats.append("HEAD_64" if h_q == 64 else "HEAD_128" if h_q == 128
                 else (_ for _ in ()).throw(AssertionError(f"h_q={h_q}")))
    # 头维
    feats.append("HEAD_DIM_576" if d_qk == 576 else "HEAD_DIM_512")
    # 模型类型 / KV 布局
    feats.append("V32_KVCACHE_FORMAT" if model_type == "V32" else "MODEL1_KVCACHE_FORMAT")
    # 可选能力
    if have_attn_sink:        feats.append("ATTN_SINK")
    if have_topk_length:      feats.append("TOPK_LENGTH")
    if have_extra_kcache:     feats.append("EXTRA_KVCACHE")
    if have_extra_topk_length:feats.append("EXTRA_TOPK_LENGTH")
    return feats

print(build_required_features(128, 576, True, False, False, False))
```

**需要观察的现象**：输出应为 `['HEAD_128', 'HEAD_DIM_576', 'V32_KVCACHE_FORMAT', 'ATTN_SINK']`。

**预期结果**：当你把 `h_q=64, d_qk=512` 代入，应得到 `HEAD_64 / HEAD_DIM_512 / MODEL1_KVCACHE_FORMAT`；这正是 MODEL1 配置会产生的需求集合。这个集合随后会被拿去和每个实现类的 `DECLARE_SUPPORTED_FEATURES` 清单做子集判定。

### 4.2 多实现派发：DecodeImplBase 与四个实现

#### 4.2.1 概念说明

sparse decode 的派发框架是 [u2-l4](./u2-l4-implbase-dispatcher.md) 讲过的 `ImplBase` 的具体实例化版本。这里把「运行参数类型」绑死为 `SparseAttnDecodeParams`、「feature 枚举类型」绑死为 `DecodeFeatures`，得到专属基类 `DecodeImplBase`，再派生出四个具体实现：

| 实现类 | 架构 | 支持的头数 | 支持的头维 | 用途 |
|--------|------|-----------|-----------|------|
| `Decode_Sm90_Impl` | SM90a | 64, 128 | 512, 576 | Hopper 上的通用 sparse decode（[u5-l3](./u5-l3-crossover-dsm.md) 的 crossover kernel） |
| `Decode_Sm100_Head64_Impl` | SM100f | 64 | 512, 576 | Blackwell 上 `h_q=64` 的路径 |
| `Decode_Sm100_Head64x2_Impl` | SM100f | 128 | 512, 576 | Blackwell 上 `h_q=128` 的「折中」路径（跑两遍 head64） |
| `Decode_Sm100_Head128_Impl` | SM100f | 128 | **仅 512** | Blackwell 上 `h_q=128 + d_qk=512`（MODEL1）的原生 head128 路径 |

注意 `Decode_Sm100_Head128_Impl` 的能力清单里**没有 `HEAD_DIM_576`**——这正是 V3.2（`d_qk=576`）在 SM100f 上无法走原生 head128、只能退回 head64x2 的根本原因（详见 4.3）。

#### 4.2.2 核心流程

派发的完整链路：

1. 接口函数按 `arch.is_sm100f() / is_sm90a()` 与 `(h_q, d_qk)` 直接 `new` 一个实现对象（**显式选择**，而非遍历尝试）。
2. 调 `impl->get_meta(h_q, s_q)` 得到 `DecodeImplMeta{num_sm_parts, fixed_overhead_num_blocks, block_size_topk}`。
3. 装配 `params`、生成 sched_meta、分配 accumulate 缓冲。
4. 调 `impl->run(params, features)`——这是 `ImplBase::run`，它**先做 feature 子集校验**（`check_if_all_features_are_supported_and_abort`），**通过后才调** `run_` 真正启动 kernel。这一步是「显式选择之外的安全网」：即便选择逻辑有 bug 选错了实现，feature 校验也会拦住并打印三段诊断。

`get_meta` 的差异体现了各实现切 KV 的粒度不同：

- SM90：`num_sm_parts = max(num_sms / s_q / (h_q/64), 1)`——SM90 kernel 把 64 个头捆在一个 partition 里，所以头数越多、可用 partition 越少。
- SM100 Head64 / Head64x2：`num_sm_parts = max(num_sms / s_q, 1)`。
- SM100 Head128：`num_sm_parts = max(num_sms / s_q / 2, 1)`，且 `fixed_overhead_num_blocks=3`（更小）。

#### 4.2.3 源码精读

`DecodeFeatures` 枚举——所有可能的能力开关都在这里：

[csrc/api/sparse_decode.h:14-28](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L14-L28) —— 头数（`HEAD_64/HEAD_128`）、头维（`HEAD_DIM_576/HEAD_DIM_512`）、KV 布局（`V32_KVCACHE_FORMAT/MODEL1_KVCACHE_FORMAT`）、`ATTN_SINK`、`TOPK_LENGTH`、`EXTRA_KVCACHE`、`EXTRA_TOPK_LENGTH`。枚举从 0 连续编号，是 [u2-l4](./u2-l4-implbase-dispatcher.md) 里 enum 名字反射（错误诊断打印可读名）的前提。

派发基类与 `DECLARE_SUPPORTED_FEATURES` 声明能力清单：

[csrc/api/sparse_decode.h:36-56](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L36-L56) —— `DecodeImplBase` 继承 `ImplBase<SparseAttnDecodeParams, DecodeFeatures>`，新增纯虚 `get_meta`；`Decode_Sm90_Impl` 用 `DECLARE_SUPPORTED_FEATURES(...)` 一次性声明它支持的全部 10 个 feature。

四个实现的 `run_`（实际启动 kernel 的地方）：

[csrc/api/sparse_decode.h:69-75](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L69-L75) —— SM90 实现：嵌套 `DISPATCH_MODEL_TYPE` × `DISPATCH_NUM_HEADS`，调用 `sm90::decode::sparse_fp8::run_flash_splitkv_mla_fp8_sparse_kernel<MODEL_TYPE, NUM_HEADS>`。

[csrc/api/sparse_decode.h:102-106](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L102-L106) —— SM100 Head64 实现：只 `DISPATCH_MODEL_TYPE`（头数已被「选了 Head64 实现」这件事隐含确定），调用 `sm100::decode::head64::run_flash_splitkv_mla_fp8_sparse_kernel<MODEL_TYPE>`。

派发选择逻辑（本模块的「路由表」）：

[csrc/api/sparse_decode.h:362-381](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L362-L381) —— `is_sm100f()` 分支下：`h_q==64→Head64`；`h_q==128` 再按 `d_qk`：`576→Head64x2`、`512→Head128`。`is_sm90a()` 分支统一用 `Decode_Sm90_Impl`。其他架构直接 `TORCH_CHECK(false)`。

安全网：`ImplBase::run` 先校验后执行：

[csrc/api/common.h:226-229](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L226-L229) —— `run` 内部先 `check_if_all_features_are_supported_and_abort(required_features)` 再 `run_`。校验失败时（见 [common.h:192-224](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L192-L224)）会打印「需求 / 支持 / 差集」三段诊断和当前 GPU 信息，再抛 `TORCH_CHECK` 异常。

接口层调用点：

[csrc/api/sparse_decode.h:383](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L383) —— `DecodeImplMeta impl_meta = impl->get_meta(h_q, s_q);`

[csrc/api/sparse_decode.h:468](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L468) —— `impl->run(params, features);`（注意传入的是 `features`，即需求集合，供安全网校验）。

#### 4.2.4 代码实践

**实践目标**：把 4.2.3 的路由表 [sparse_decode.h:362-381](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L362-L381) 翻译成 Python 决策函数，输入 `(arch, h_q, d_qk)`，输出选中的实现类名，并自动用 feature 子集校验复刻安全网。

**操作步骤**：运行下面的示例代码（与源码路由表一一对应，并附带 feature 子集校验）：

```python
# 示例代码：复刻 sparse decode 的派发路由表 + feature 安全网
SUPPORTED = {
    "Decode_Sm90_Impl":          {"HEAD_64","HEAD_128","HEAD_DIM_512","HEAD_DIM_576",
                                  "V32_KVCACHE_FORMAT","MODEL1_KVCACHE_FORMAT","ATTN_SINK",
                                  "TOPK_LENGTH","EXTRA_KVCACHE","EXTRA_TOPK_LENGTH"},
    "Decode_Sm100_Head64_Impl":  {"HEAD_64","HEAD_DIM_512","HEAD_DIM_576","V32_KVCACHE_FORMAT",
                                  "MODEL1_KVCACHE_FORMAT","ATTN_SINK","TOPK_LENGTH",
                                  "EXTRA_KVCACHE","EXTRA_TOPK_LENGTH"},   # 不支持 HEAD_128
    "Decode_Sm100_Head64x2_Impl":{"HEAD_128","HEAD_DIM_512","HEAD_DIM_576","V32_KVCACHE_FORMAT",
                                  "MODEL1_KVCACHE_FORMAT","ATTN_SINK","TOPK_LENGTH",
                                  "EXTRA_KVCACHE","EXTRA_TOPK_LENGTH"},
    "Decode_Sm100_Head128_Impl": {"HEAD_128","HEAD_DIM_512","MODEL1_KVCACHE_FORMAT","ATTN_SINK",
                                  "TOPK_LENGTH","EXTRA_KVCACHE","EXTRA_TOPK_LENGTH"},  # 不支持 HEAD_DIM_576 / V32
}

def select_impl(arch, h_q, d_qk):
    if arch == "sm100f":
        if h_q == 64:   return "Decode_Sm100_Head64_Impl"
        if h_q == 128:
            return "Decode_Sm100_Head64x2_Impl" if d_qk == 576 else "Decode_Sm100_Head128_Impl"
    elif arch == "sm90a":
        return "Decode_Sm90_Impl"
    raise ValueError("Unsupported architecture")

def safe_run(arch, h_q, d_qk, required):
    impl = select_impl(arch, h_q, d_qk)
    missing = set(required) - SUPPORTED[impl]
    assert not missing, f"安全网拦截：{impl} 不支持 {missing}"
    return impl

# V3.2 形状在 Blackwell 上
req = ["HEAD_128","HEAD_DIM_576","V32_KVCACHE_FORMAT","ATTN_SINK"]
print(safe_run("sm100f", 128, 576, req))   # 预期 Decode_Sm100_Head64x2_Impl
```

**需要观察的现象**：最后一行应打印 `Decode_Sm100_Head64x2_Impl`。

**预期结果 / 待本地验证**：若你把 `select_impl` 里 `h_q==128, d_qk==576` 的分支误改成 `Decode_Sm100_Head128_Impl`，`safe_run` 的安全网会立刻报 `不支持 {'HEAD_DIM_576', 'V32_KVCACHE_FORMAT'}`——这正是 [common.h:205-218](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L205-L218) 打印的「差集」诊断。这说明：**显式路由表负责选实现，feature 子集校验负责兜底防错**，两层共同保证不会把不支持的配置送进 kernel。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Decode_Sm100_Head64_Impl` 的能力清单里没有 `HEAD_128`，但它的 `run_` 却连 `DISPATCH_NUM_HEADS` 都不需要？

**答案**：能被选中进入 `Decode_Sm100_Head64_Impl` 的前提是路由表判定 `h_q==64`（见 [sparse_decode.h:364-365](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L364-L365)），头数在进入 `run_` 之前已确定为 64，无需再分派；而清单里不写 `HEAD_128` 是为了让安全网拒绝任何误传的 128 头请求。

**练习 2**：`ImplBase::run` 为什么不直接调 `run_`，而非要先 `check_if_all_features_are_supported_and_abort`？

**答案**：路由表是手工 if-else，存在写错或漏判组合的风险；feature 子集校验是一道独立的安全网，保证「即便选错实现，也不会把 kernel 不认识的能力（如把 `HEAD_DIM_576` 送进只支持 512 的实现）传进去」，并在失败时给出可读诊断（[common.h:192-224](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L192-L224)）。

### 4.3 head64x2：用 head64 kernel 跑两遍处理 head128

#### 4.3.1 概念说明

`Decode_Sm100_Head64x2_Impl` 是本讲最精巧的设计，源码注释一句话点明了它的用意：

> An implementation that calls the head64 kernel twice to process head128. Necessary for running V3.2 shape (i.e. h = 128, d_qk = 576) on SM100f.

直觉是：SM100f 上**没有**一个同时支持「`h_q=128` + `d_qk=576`（V32 布局）」的原生 sparse decode kernel——`Decode_Sm100_Head128_Impl` 只支持 `HEAD_DIM_512` / `MODEL1`。但 head64 kernel 是有的（且支持 V32）。于是作者把 128 个头**纵向切成两段 64 头**，每段复用 head64 kernel 跑一次，两次结果写入同一组输出张量的不同头区。

数学上这完全等价：注意力是逐头独立的（每个 query 头 \(i\) 只用自己的 \(Q_i\) 与共享 KV 算 \(O_i\)），所以把 \([0,128)\) 拆成 \([0,64)\) 和 \([64,128)\) 各算各的，拼接后就是完整的 128 头输出。

#### 4.3.2 核心流程

`Decode_Sm100_Head64x2_Impl::run_` 的循环（`start_head_idx` 取 0 和 64）对每次迭代做：

1. **复制一份 `params`**（值语义，不污染原始参数）。
2. **平移所有「按头索引」的指针**，让它们指向当前 64 头段的起点。
3. **把 `cur_params.h_q` 改成 64**，伪装成一次 head64 调用。
4. 调 `sm100::decode::head64::run_flash_splitkv_mla_fp8_sparse_kernel<MODEL_TYPE>(cur_params)`。

需要平移的指针有六个：`q`、`attn_sink`、`lse`、`out`、`lse_accum`、`o_accum`。其中 `q / out / o_accum` 的头维 stride 各不相同，平移量分别是 `stride_q_h_q`、`stride_o_h_q`、`stride_o_accum_h_q`；`attn_sink / lse / lse_accum` 的头维 stride 恰为 1（每个头一个标量），所以直接 `+= start_head_idx`。

> 一个容易忽略的点：`indices / kv / extra_*` 等「按 KV 而非按头」的张量**不需要平移**——因为 MQA 下所有头共享同一份 KV 与索引，两段 64 头用的是完全相同的 KV 数据。

#### 4.3.3 源码精读

实现类整体与设计意图注释：

[csrc/api/sparse_decode.h:110-123](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L110-L123) —— 注释说明「跑两遍 head64 处理 head128，是 V3.2 形状跑在 SM100f 上所必需」；能力清单含 `HEAD_128` + `HEAD_DIM_576`，正是 V3.2 需要的。

核心循环与指针平移（本模块的灵魂）：

[csrc/api/sparse_decode.h:136-151](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L136-L151) —— `for (start_head_idx = 0; start_head_idx < 128; start_head_idx += 64)`：
- `cur_params.q += start_head_idx * params.stride_q_h_q;`
- `if (cur_params.attn_sink) cur_params.attn_sink += start_head_idx;`（注意判空，因为 attn_sink 可选）
- `cur_params.lse += start_head_idx;`
- `cur_params.out += start_head_idx * params.stride_o_h_q;`
- `cur_params.lse_accum += start_head_idx;`
- `cur_params.o_accum += start_head_idx * params.stride_o_accum_h_q;`
- `cur_params.h_q = 64;` 后调 head64 kernel。

被复用的 head64 kernel 声明：

[csrc/sm100/decode/head64/kernel.h:5-10](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/decode/head64/kernel.h#L5-L10) —— `sm100::decode::head64::run_flash_splitkv_mla_fp8_sparse_kernel<MODEL_TYPE>(params)`，只按 `MODEL_TYPE` 模板化（头数固定为 64，写死在 kernel 内部）。

对照：原生 head128 实现的能力缺口：

[csrc/api/sparse_decode.h:156-165](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L156-L165) —— `Decode_Sm100_Head128_Impl` 的清单里**只有 `HEAD_DIM_512` 与 `MODEL1_KVCACHE_FORMAT`**，没有 576 / V32。它的 `run_` 调的是 `sm100::fwd_for_small_topk::head128::run_fwd_for_small_topk_phase1_kernel<DecodeWithSplitKV, 512>`——即复用了 [u6-l3](./u6-l3-sm100-sparse-prefill.md) 将要讲的 small_topk prefill kernel 的解码模式。

#### 4.3.4 代码实践

**实践目标**：追踪「SM100f + `h_q=128` + `d_qk=576`」这一V3.2 生产形状的完整派发与指针切分，亲手算出第二次迭代各指针的偏移量。本实践为源码阅读型，无需 GPU。

**操作步骤**：

1. 确认路由：在 [sparse_decode.h:366-368](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L366-L368) 中，`is_sm100f() && h_q==128 && d_qk==576` 命中 `new Decode_Sm100_Head64x2_Impl()`。
2. 阅读循环体 [sparse_decode.h:138-150](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L138-L150)，按下面假设的 stride 填表。

假设某次调用的张量布局为（仅头维 stride 与本实践相关，其余省略）：

| 指针 | 头维 stride（元素数） | 第一次迭代 (`start=0`) 偏移 | 第二次迭代 (`start=64`) 偏移 |
|------|----------------------|----------------------------|------------------------------|
| `q`        | `stride_q_h_q = 576`  | 0 | `64 * 576 = 36864` |
| `out`      | `stride_o_h_q = 512`  | 0 | `64 * 512 = 32768` |
| `o_accum`  | `stride_o_accum_h_q = 512` | 0 | `64 * 512 = 32768` |
| `lse`      | 1 | 0 | 64 |
| `lse_accum`| 1 | 0 | 64 |
| `attn_sink`| 1（若提供） | 0 | 64 |

> stride 数值取自 `params` 的实际布局：`q` 形状 `[b,s_q,128,576]` 故头维 stride=576；`out` 形状 `[b,s_q,128,512]` 故头维 stride=512。这些 stride 在 [sparse_decode.h:404-408](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L404-L408) 由 `int64_stride_to_int(q.stride(2))` 等填入。

**需要观察的现象**：第二次迭代把 `q` 指针后移 36864 个 bf16 元素，正好落在第 64 个 query 头的起点；`out` 后移 32768，落在第 64 个输出头的起点；两次写入的头区 `[0,64)` 与 `[64,128)` 互不重叠。

**预期结果**：两次 head64 kernel 调用结束后，`out[b,s_q,0:128,0:512]` 被完整填满，等价于一次 128 头的 sparse decode。**待本地验证**：可在 SM100f GPU 上对该形状跑 `tests/test_flash_mla_sparse_decoding.py`，并对照 `ref.ref_sparse_attn_decode` 检查 `out` 的 cos_diff。

#### 4.3.5 小练习与答案

**练习 1**：既然 head64x2 要跑两遍 kernel，岂不比原生 head128 慢一倍？

**答案**：不一定。两次 head64 各只处理一半的头，单次工作量是 head128 的一半；且 sparse decode 在 MQA 下是 dequantization-bound（见 [u5-l2](./u5-l2-dequant-flow.md)）/ 受限于 KV 访存，头数翻倍并不线性翻倍耗时。head64x2 是「没有原生 V32+head128 kernel 时的可用折中」，而非最优解——当 `d_qk=512`（MODEL1）时就会改用原生 `Decode_Sm100_Head128_Impl`。

**练习 2**：为什么 `attn_sink` 的平移要包在 `if (cur_params.attn_sink)` 里，而 `lse / out` 不用？

**答案**：`attn_sink` 是可选张量（不传时为 `nullptr`，对应不启用 `ATTN_SINK` feature），对空指针做算术并解引用是未定义行为，必须先判空；而 `lse / out / lse_accum / o_accum` 是每次调用必有的输出缓冲，绝不為空，故无需判空（见 [sparse_decode.h:141-147](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L141-L147)）。

### 4.4 extra KV 与 topk_length 的语义

#### 4.4.1 概念说明

sparse decode 除了主 KV cache + `indices`，还支持一组「附加 KV」机制，用于在已索引的稀疏 token 之外再额外关注一批 token（典型场景：把某些始终需要关注的 token 放在 extra KV 里，与稀疏选中的 token 分开管理）。涉及四个可选张量：

- **`extra_kv`**：附加 KV cache，形状 `[extra_num_blocks, extra_page_block_size, h_kv, d_qk]`，FP8 布局要求与主 `kv` 完全一致。
- **`extra_indices`**：附加索引，形状 `[b, s_q, extra_topk]`，编码方式与主 `indices` 相同。
- **`extra_topk_length`**：`[b]`，作用同 `topk_length` 但针对 extra 部分。
- **`topk_length`**：`[b]`，限制主 `indices` 中每个 batch 实际生效的索引数（前 `topk_length[b]` 个有效，其余当作无效）。

两条硬配对约束（见 4.1.3 的 `TORCH_CHECK`）：
- `extra_kv` ⇔ `extra_indices`：必须同时出现或同时缺席。
- `extra_topk_length`：只有提供了 `extra_kv` 时才允许提供。

每个可选张量是否出现，都会被翻译成一个 feature 开关（`EXTRA_KVCACHE` / `EXTRA_TOPK_LENGTH` / `TOPK_LENGTH`），从而让 kernel 在编译期决定是否走「带 extra / 带 topk_length」的特化分支——避免在 kernel 内部用运行时分支拖慢热路径。

#### 4.4.2 核心流程

`topk_length` 的掩码语义（参考实现 [tests/ref.py](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/ref.py) 的等价逻辑）：

1. 对第 \(b\) 个 batch，只有 `indices[b, s_q, 0:topk_length[b]]` 这前若干个索引是有效的。
2. 第 `k >= topk_length[b]` 个索引即使不是 `-1`，也按无效处理（掩码掉对应的 KV）。
3. 这与「把多余索引填成 -1」等价，但避免了在 host 侧改写 `indices`——只需多传一个长度向量。

`extra_kv` 的语义：最终注意力 = 对「主 KV 中被 `indices` 选中的 token ∪ extra KV 中被 `extra_indices` 选中的 token」一起做 softmax。两套索引在 kernel 内部合并参与同一个 online softmax，由 combine 阶段统一归并。

#### 4.4.3 源码精读

extra KV 的配对约束：

[csrc/api/sparse_decode.h:239-244](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L239-L244) —— `extra_kv` 与 `extra_indices` 同生共死；`extra_topk_length` 不得脱离 `extra_kv`。

extra 维度抽取：

[csrc/api/sparse_decode.h:221-228](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L221-L228) —— `extra_num_blocks / extra_page_block_size / extra_topk` 仅在 `extra_kv` / `extra_indices` 存在时才读取。

形状校验（注意 `topk_length` 是 `[b]`）：

[csrc/api/sparse_decode.h:306-310](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L306-L310) —— `indices[b,s_q,topk]`、`topk_length[b]`、`attn_sink[h_q]`、`extra_indices[b,s_q,extra_topk]`、`extra_topk_length[b]`。

feature 开关翻译：

[csrc/api/sparse_decode.h:352-360](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L352-L360) —— `have_topk_length→TOPK_LENGTH`、`have_extra_kcache→EXTRA_KVCACHE`、`have_extra_topk_length→EXTRA_TOPK_LENGTH`。

`params` 装配中的 extra 字段：

[csrc/api/sparse_decode.h:399-402](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L399-L402) —— `extra_num_blocks / extra_page_block_size / extra_topk` 与三个可选指针（`extra_kv / extra_indices / extra_topk_length`，未提供时 `get_optional_tensor_ptr` 返回 `nullptr`）。

`SparseAttnDecodeParams` 中 extra 与 topk_length 的字段定义：

[csrc/params.h:74-83](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L74-L83) —— `topk_length`、`extra_kv`、`extra_indices`、`extra_topk_length` 均标注 `may be nullptr`，这就是「可空张量用 nullptr 表示禁用」的契约（详见 [u2-l2](./u2-l2-params-structs.md)）。

topk_length 的掩码语义在参考实现中的等价写法：

[tests/ref.py:72-73](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/ref.py#L72-L73) —— `invalid_mask |= arange(topk) >= topk_length.view(b,1,1)`，即第 `k` 个索引若 `k >= topk_length[b]` 则判为无效。

#### 4.4.4 代码实践

**实践目标**：用纯 PyTorch 复刻 `topk_length` 的掩码语义，验证「前 N 个有效、其余无效」与「直接把多余索引置 -1」数值等价。CPU 可运行。

**操作步骤**：运行下面示例代码，模拟一个 batch 的索引有效性：

```python
# 示例代码：复刻 topk_length 的掩码语义（CPU）
import torch

b, s_q, topk = 2, 1, 8
indices = torch.tensor([[[0, 1, 2, 3, 4, 5, 6, 7]],
                        [[10, 11, 12, 13, 14, 15, 16, 17]]])  # [b, s_q, topk]
topk_length = torch.tensor([3, 5])                              # [b]，第 0 个 batch 只用前 3 个

# 方式 A：用 topk_length 生成掩码（kernel 的做法）
ar = torch.arange(topk).view(1, 1, topk)
invalid = ar >= topk_length.view(b, 1, 1)                       # [b, s_q, topk]
indices_A = indices.clone(); indices_A[invalid] = -1

# 方式 B：手工置 -1，应当与 A 完全一致
indices_B = torch.tensor([[[-1, -1, -1, -1, -1, -1, -1, -1]],
                          [[-1, -1, -1, -1, -1, -1, -1, -1]]])
indices_B[0, 0, 0:3] = torch.tensor([0, 1, 2])
indices_B[1, 0, 0:5] = torch.tensor([10, 11, 12, 13, 14])

print("等价：", torch.equal(indices_A, indices_B))
print(indices_A)
```

**需要观察的现象**：第 0 个 batch 仅 `[0,1,2]` 保留、其余置 -1；第 1 个 batch 仅 `[10,11,12,13,14]` 保留。

**预期结果**：打印 `等价： True`。这说明 `topk_length` 的好处——当不同 batch 的真实 topk 不同时，无需在 host 侧改写 `indices`，只传一个 `[b]` 长度向量即可，省去数据搬运与改写开销（见 Python 文档 [flash_mla_interface.py:90](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L90) 的说明）。

#### 4.4.5 小练习与答案

**练习 1**：如果用户只传了 `extra_indices` 却没传 `extra_kv`，会发生什么？

**答案**：会被 [sparse_decode.h:241-243](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L241-L243) 的 `TORCH_CHECK(!extra_indices.has_value(), ...)` 拦下并抛异常。extra 三件套里 `extra_kv` 是主，`extra_indices` 与 `extra_topk_length` 都依附于它。

**练习 2**：为什么要把「是否有 topk_length」做成一个 feature（`TOPK_LENGTH`），而不是在 kernel 里用一个 `if (topk_length != nullptr)` 分支？

**答案**：sparse decode 是 dequantization-bound 的热路径（[u5-l2](./u5-l2-dequant-flow.md)），kernel 内部的分支会打乱指令流水、拖慢每个 token 的处理；把它编译期化为模板特化（`DISPATCH_BOOLEAN_FLAG` 思路，见 [u2-l3](./u2-l3-arch-and-dispatch-macros.md)），就能为「有 / 无 topk_length」各生成一份无分支的 kernel，运行时只走其中一条。

## 5. 综合实践

把本讲四个模块串起来，完成一次「端到端派发追踪」。假设你在 SM100f（Blackwell）上跑 DeepSeek-V3.2 的生产形状：`b=128, s_q=2, h_q=128, d_qk=576, d_v=512, topk=2048`，并启用了 `attn_sink`，未启用 extra KV / topk_length。

请按顺序回答并验证：

1. **需求 feature 集合**：用 4.1.4 的 `build_required_features(128, 576, True, False, False, False)` 算出，应为 `{HEAD_128, HEAD_DIM_576, V32_KVCACHE_FORMAT, ATTN_SINK}`。
2. **路由选择**：用 4.2.4 的 `select_impl("sm100f", 128, 576, ...)` 确认选中 `Decode_Sm100_Head64x2_Impl`，并用 `safe_run` 验证安全网通过。
3. **指针切分**：用 4.3.4 的表格，算出第二次迭代（`start_head_idx=64`）时 `q` 指针的后移量 = `64 × 576 = 36864` 个 bf16，`out` 后移 `64 × 512 = 32768`。
4. **extra / topk_length**：说明本例因未传 extra KV，`EXTRA_KVCACHE / TOPK_LENGTH` 均不在需求集合里；若你后来加上 `extra_k_cache`，必须同时提供 `extra_indices_in_kvcache`，否则会被 4.4.3 的 `TORCH_CHECK` 拦截。
5. **（可选，需 SM100f GPU）**：参照 [tests/test_flash_mla_sparse_decoding.py](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_sparse_decoding.py) 中 `base_and_bszs` 的 V3.2 用例（`RawTestParam(0, 128, 2, 1, 32768, True, topk=2048, d_qk=576)`），跑一次并对照 `ref.ref_sparse_attn_decode` 检查 `out` 的 `cos_diff_tol=5e-6`。**待本地验证**。

通过这五步，你就把「输入张量 → feature 集合 → 路由 → 指针切分 → kernel 启动」整条 sparse decode 接口链路完整走了一遍。

## 6. 本讲小结

- `sparse_attn_decode_interface` 是「校验 + 编排 + 派发」中枢：层层 `KU_CHECK_*` / `TORCH_CHECK` 防御后，把输入张量形态翻译成 `DecodeFeatures` 需求集合。
- 四个实现类（`Decode_Sm90_Impl` / `Decode_Sm100_Head64_Impl` / `Decode_Sm100_Head64x2_Impl` / `Decode_Sm100_Head128_Impl`）各自由 `DECLARE_SUPPORTED_FEATURES` 声明能力清单；接口用显式 if-else 路由表选其一，`ImplBase::run` 的 feature 子集校验作安全网。
- SM100f + `h_q=128` + `d_qk=576`（V3.2）走 `Decode_Sm100_Head64x2_Impl`：因为原生 `Decode_Sm100_Head128_Impl` 不支持 `HEAD_DIM_576`；它把 128 头切成两段 64 头，平移 `q/out/o_accum/lse/lse_accum/attn_sink` 指针后各跑一次 head64 kernel。
- `extra_kv` ⇔ `extra_indices` 必须配对，`extra_topk_length` 依附于 `extra_kv`；`topk_length` 是 `[b]` 向量，掩码掉每个 batch 多余的索引，等价于把它们置 -1。
- 每个可选能力（attn_sink / topk_length / extra KV / extra topk_length）都被翻译成 feature 开关，目的是让 kernel 在编译期特化出无分支版本，保护 dequantization-bound 热路径。

## 7. 下一步学习建议

- **进入 SM100 sparse prefill**：本讲的 `Decode_Sm100_Head128_Impl` 复用了 small_topk prefill kernel 的解码模式，其原理在 [u6-l3 SM100 sparse prefill 与 small_topk 变体](./u6-l3-sm100-sparse-prefill.md) 展开，建议接着读。
- **对比 sparse fwd 接口**：[u6-l4 Sparse fwd 接口与实现选择](./u6-l4-sparse-fwd-interface.md) 讲同一套 `ImplBase` 框架在 prefill 侧的「优先 / 兜底」选择逻辑，可与本讲的「显式路由」对照。
- **架构取舍全景**：把本讲的「SM100 上 head128 缺原生 V32 实现 → 退回 head64x2」放进全局，读 [u9-l1 SM90 与 SM100 的架构取舍](./u9-l1-arch-tradeoffs-sm90-sm100.md)。
- **想扩展能力**：若要为 sparse decode 新增一个 head_dim 或新 feature，参考 [u9-l2 扩展点](./u9-l2-extension-points.md)，改动面横跨 `DISPATCH` 宏、`params.h`、`DECLARE_SUPPORTED_FEATURES`、kernel 实例化与 `setup.py`。
