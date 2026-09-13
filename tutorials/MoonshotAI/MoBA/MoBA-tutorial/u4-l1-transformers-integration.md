# transformers 集成：moba_layer 包装器与 prefill/decode 分支

## 1. 本讲目标

前三单元我们一直在「内核层」看 MoBA：naive 实现和高效实现都只认 flash-attn 风格的 varlen 输入（`[total, heads, dim]` + `cu_seqlens`）。但真实用户是在 transformers 里写 `AutoModelForCausalLM.from_pretrained(..., attn_implementation="moba")`，两边隔着一道接口鸿沟。

本讲精读这两道桥：

1. `moba/wrapper.py` 的 `moba_layer` —— 把 HF 注意力接口的参数约定翻译成 MoBA 内核的参数约定，包括布局转换、GQA 头复制、prefill/decode 分支。
2. `moba/__init__.py` 的 `register_moba` —— 通过改写 transformers 的 `ALL_ATTENTION_FUNCTIONS` 注册表，把自定义函数注入模型加载与前向的完整链路。

学完本讲你应该能够：

- 说清 HF 注意力接口的「参数契约」与「返回值契约」，以及 `moba_layer` 如何逐项满足它。
- 解释为什么输出侧那句 `fa_to_hf` 被注释掉反而不影响正确性（形状契约分析）。
- 解释 GQA 下 `repeat_interleave` 复制 KV 的映射关系，以及为什么 decode 分支不需要复制。
- 解释 decode 阶段退回 `flash_attn_func` 全量注意力为什么不破坏 MoBA 语义。
- 独立追踪「`model.generate` → `moba_layer`」的完整调用链，并知道如何在自己安装的 transformers 里定位每一跳。

## 2. 前置知识

本讲建立在已修讲义的结论之上，只做简短回顾，不重复推导：

- **u1-l3**：HF 布局 `[B, H, S, D]` 与 flash-attn 布局 `[B*S, H, D]` 经 `permute + reshape` 互转，打包序 `p = b*S + s`；`cu_seqlens` 是长 B+1、首元素 0 的前缀和，标记每条序列的边界；`hf_to_fa` 与 `fa_to_hf` 互为逆变换。本讲在 **4.2** 节用它分析输出侧契约。
- **u1-l2**：`register_moba(cfg)` 把两个注意力后端写入 transformers 注册表，且必须发生在 `from_pretrained` 之前；`MoBAConfig` 只有 `moba_chunk_size` 与 `moba_topk` 两个字段。本讲在 **4.5** 节拆开这个机制的内部。
- **u3-l1**：`moba_attn_varlen` 的四步心智模型（chunk 元数据 → gate 选块 → varlen 重组 → LSE 合并），以及 `need_moba_attn` 为假时退化为普通因果注意力的兜底。本讲把它当黑盒，只关心它的签名与返回形状。

在此基础上，补充三个本讲用到的通用概念：

- **适配器 / 包装器模式**：两个模块接口不一致时，写一个中间函数负责「翻译」——一侧模仿甲方要的签名，另一侧按乙方的习惯调用。`moba_layer` 就是典型适配器：对上伪装成 HF 注意力函数，对下按 MoBA 内核的 varlen 约定传参。
- **`functools.partial`**：把一个函数的若干参数「预先绑定」，得到一个新的可调用对象。`partial(f, a, b)` 之后调用 `g(c, d)` 等价于 `f(a, b, c, d)`。绑定的是**位置参数的前缀**，这是本讲注册机制的关键。
- **注册表（插件）模式**：框架维护一张「名字 → 函数」的字典，运行时按名字查表分发。transformers 的 `ALL_ATTENTION_FUNCTIONS` 就是注意力算子的注册表；往字典里塞一个新键，就等于给框架装了一个插件，且**不需要改框架源码**。

另外两个模型侧名词：

- **prefill / decode 两阶段**：自回归生成分两步。第一步 prefill 把整条 prompt 一次性算完（`q_len == kv_len`，query 和 key 都是完整序列）；之后每生成一个 token 走一步 decode（`q_len == 1`，key/value 是增长中的 KV cache）。这是 wrapper 分支判定的现实依据。
- **KV cache**：decode 阶段历史 token 的 K/V 不再重算，缓存在显存里，因此 `kv_len` 随生成步数增长，永远比 `q_len` 大。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [moba/wrapper.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py) | HF 注意力接口适配层 | `moba_layer` 全函数：签名适配、布局转换、GQA 复制、prefill/decode 分支、返回值契约 |
| [moba/__init__.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/__init__.py) | 对外出口 + 注册函数 | `register_moba` 如何用 `partial` 写入 `ALL_ATTENTION_FUNCTIONS` |
| [examples/llama.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/examples/llama.py) | 端到端示例 | 注册 → 加载 → 生成的三步时序，`attn_implementation` 的传法 |
| [moba/moba_efficient.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py) | 高效实现（黑盒） | 仅看签名 `moba_attn_varlen(...)` 与返回形状 `[seqlen, head, head_dim]`，以及内部自带的 `softmax_scale` |
| [moba/config.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/config.py) | 配置对象 | 被闭包捕获的两个字段 |
| [requirements.txt](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/requirements.txt) | 依赖锁定 | `transformers>=4.48.3` 与注册表入口的版本约束 |

transformers 侧的代码不在本仓库里，本讲以 4.48.x 的行为为基准描述（`requirements.txt` 第 2 行锁定 `transformers>=4.48.3`），涉及 HF 内部的行号一律标注「以本机安装版本为准」，并在第 5 节综合实践中带读者实地核对。

## 4. 核心概念与源码讲解

### 4.1 moba_layer 接口适配：把 MoBA 内核嫁接到 HF 注意力接口

#### 4.1.1 概念说明

transformers 4.48 起，每个因果模型的注意力层不再硬编码注意力算法，而是从一个注册表里取函数调用（称为 attention interface）。任何注册进去的函数必须遵守一份「接口契约」：

- **入参**：`(module, query, key, value, *args, dropout=..., scaling=..., **kwargs)`。其中 `module` 是注意力层实例（能访问 `config`、`scaling` 等）；`query/key/value` 是 HF 布局 `[B, H, S, D]` 的张量；`*args` 里通常躺着 `attention_mask`，`**kwargs` 里可能有 `sliding_window`、`**kwargs` 透传的其他参数。
- **返回**：`(attn_output, attn_weights)` 二元组。`attn_output` 是注意力输出，`attn_weights` 是注意力概率（拿不到就给 `None`）。

MoBA 内核（`moba_attn_varlen` / `moba_attn_varlen_naive`）的约定则完全不同：flash-attn 布局、varlen 打包、不要 mask、自带缩放。`moba_layer` 的职责就是把前者翻译成后者，并把 HF 塞进来的「用不上」的参数安全地吞掉。

#### 4.1.2 核心流程

`moba_layer` 实际上是三个角色的叠加（前两个由 `partial` 预绑定，见 4.5）：

```text
HF 调用:  attention_interface(module, q, k, v, attention_mask, dropout, scaling, ...)
                        │
                        ▼
moba_layer( moba_impl,      ← 已绑定：moba_attn_varlen 或 moba_attn_varlen_naive
            moba_config,    ← 已绑定：MoBAConfig(chunk_size, topk)
            module, q, k, v, *args, dropout, scaling, **kwargs )
                        │
        ┌───────────────┴────────────────┐
        │ q_len == kv_len?               │
        ├─ 是 → prefill：转布局 → GQA 复制 → 构造 cu_seqlens → 调 moba_impl
        └─ 否 → decode：转置 → flash_attn_func 全量因果注意力（TODO: paged attn）
                        │
                        ▼
            返回 (out, None)
```

参数落点一览（HF 的调用逐一落到哪里）：

| HF 传入 | 落到 moba_layer 的 | 命运 |
| --- | --- | --- |
| `module` | 同名形参 | 只被 `assert module.is_causal` 检查 |
| `query/key/value` | 同名形参 | 两条分支各自消费 |
| `attention_mask`（位置参数） | `*args` | **被忽略**（MoBA 不支持 padding mask） |
| `dropout` | 同名形参 | prefill 忽略；decode 转发给 `flash_attn_func` |
| `scaling` | 同名形参 | prefill 忽略（内核用 \(1/\sqrt{d}\)，见下）；decode 转发 |
| `sliding_window` 等 | `**kwargs` | **被忽略** |

#### 4.1.3 源码精读

先看签名与文档：

[moba/wrapper.py:L29-L50](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L29-L50) —— `moba_layer` 的完整签名：前两个参数 `moba_impl` 与 `moba_config` 供 `partial` 预绑定；`module/query/key/value` 对齐 HF 契约；`*args` 吸收 `attention_mask` 这类位置参数；`dropout=0.0`、`scaling=None` 两个关键字形参对齐 HF 常传的关键字；`**kwargs` 吞掉其余一切。返回类型注解 `Tuple[torch.Tensor, None]` 表明注意力权重恒为 `None`。文档字符串写明输入是 HF 布局、输出是 `[batch, q_len, q_heads, head_dim]`（注意：这是「逻辑形状」，实际张量是它的展平视图，4.2 节详解）。

[moba/wrapper.py:L51-L53](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L51-L53) —— `assert module.is_causal` 只允许因果模型走这条路。这不是例行检查：MoBA 的选块规则（当前块必选、未来块禁选）以因果性为前提，双向注意力模型（如 BERT 类编码器）的语义在这里根本不成立，与其算出错乱结果不如立刻失败。随后两行拆出 `batch, q_heads, q_len, head_dim` 与 `kv_heads, kv_len`，为分支判定与 GQA 复制做准备。

值得注意的一个细节：prefill 分支**不使用** HF 传入的 `scaling`。MoBA 两个内核都在内部自行使用标准缩放：

- [moba/moba_efficient.py:L86](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L86) —— 高效实现里 `softmax_scale = q.shape[-1] ** (-0.5)`，即 \(1/\sqrt{d}\)，直接传给 flash-attn 内核；
- [moba/moba_naive.py:L31](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L31) —— naive 实现同样自行计算。

而 HF 侧 `module.scaling` 对 Llama 默认恰好也是 `head_dim ** -0.5`，两者默认一致，所以平时看不出差异；但若某个模型在 config 里自定义了 `attention_scaling`，prefill 分支不会跟随——这是适配层的一个隐含假设（行为待本地验证：改 `config.attention_scaling` 后对比输出）。

#### 4.1.4 代码实践

**实践目标**：不动模型，只用纯 Python 验证「签名契约 + partial 前缀绑定」的参数落点，确认上表每一行。

**操作步骤**（本实践不需要 GPU 和 flash-attn，是「示例代码」）：

```python
# 示例代码：用桩函数模拟 moba_layer 的参数吞噬行为
from functools import partial

def stub_layer(moba_impl, moba_config, module, query, key, value,
               *args, dropout=0.0, scaling=None, **kwargs):
    print(f"moba_impl={moba_impl}, moba_config={moba_config}")
    print(f"module={module}, query={query}, key={key}, value={value}")
    print(f"*args={args}")           # 预期: ('causal_mask_tensor',)
    print(f"dropout={dropout}")      # 预期: 0.1
    print(f"scaling={scaling}")      # 预期: 0.125
    print(f"**kwargs={kwargs}")      # 预期: {'sliding_window': 4096}
    return "out", None

# 模拟 register_moba 的绑定方式
attention_interface = partial(stub_layer, "moba_attn_varlen", "MoBAConfig(64, 2)")

# 模拟 HF 4.48 注意力层的调用姿势
attention_interface(
    "attn_module",            # module
    "q[B,H,S,D]", "k[B,H,S,D]", "v[B,H,S,D]",
    "causal_mask_tensor",     # attention_mask —— 位置参数，落入 *args
    dropout=0.1, scaling=0.125, sliding_window=4096,
)
```

**需要观察的现象**：`attention_mask` 作为第 5 个位置参数恰好排在 `value` 之后，被 `*args` 接住；`dropout/scaling` 命中具名形参；`sliding_window` 进了 `**kwargs`。

**预期结果**：打印与代码注释中的「预期」完全一致。若把 `attention_mask` 从位置参数改成关键字 `attention_mask=...`，它会改道进入 `**kwargs`——两种情况都不会报错，这就是适配层「吞参数」的容错方式。

#### 4.1.5 小练习与答案

**练习 1**：HF 调用 `attention_interface(module, q, k, v, attention_mask, dropout=0.0, scaling=None)` 时，`attention_mask` 为什么不会挤占 `query/key/value` 的位置？
**答案**：`moba_layer` 的具名形参按位置依次接收 `module, query, key, value`，排在 `*args` 之前；`attention_mask` 是第 5 个位置参数，天然被 `*args` 收集，不影响前面四个。

**练习 2**：为什么用 `assert module.is_causal` 而不是检查 `module.config` 里的某个字段？
**答案**：4.48 的注意力重构把「是否因果」标在注意力模块实例上（因果 LM 如 Llama 为 `True`），`module` 是适配层唯一必然拿到的对象；且因果性是 MoBA 选块规则的语义前提（未来块禁选），放进入口第一行可以最早失败、避免算出无声错误。具体字段定义位置随 transformers 版本而异，可在本机 `modeling_llama.py` 中确认。

**练习 3**：如果用户请求 `output_attentions=True`，经 `moba` 后端能拿到注意力概率吗？
**答案**：不能。`moba_layer` 恒返回 `(out, None)`，HF 侧收集到的注意力权重是 `None`；MoBA 的两种实现都不产出概率矩阵（高效实现基于 flash-attn，根本不物化注意力矩阵）。

### 4.2 布局转换与输出形状契约：为什么 fa_to_hf 被注释掉

#### 4.2.1 概念说明

u1-l3 已经讲过 `hf_to_fa/fa_to_hf` 的互逆转换，本讲换一个角度：**输出侧为什么一个转换都不做**。理解这一点需要先知道 HF 侧拿到 `attn_output` 之后做的第一件事是

```python
attn_output = attn_output.reshape(*input_shape, -1)   # input_shape = (batch, q_len)
```

（摘自 transformers 4.48.x 的 `modeling_llama.py` 注意力 forward，示意；行号以本机安装版本为准。）

这是一个 **reshape 契约**：HF 不检查你返回几维张量，只要求「展平后的内存顺序 = `(batch, q_len, heads, head_dim)`，且总元素数对得上」。flash-attn 布局的输出 `[B*S, H, D]` 展平顺序恰好就是 `(batch, seqlen, head, dim)`——`(b, s)` 两维本来就是按 `b*S + s` 打包折叠进第一维的。所以**直接返回即正确**，任何「好心」的维序调整反而会坏事。

#### 4.2.2 核心流程

用下标语言说清楚。设 FA 输出 `out[p, h, d]`，其中 `p = b*S + s`：

```text
HF 期望:   attn_output.reshape(B, S, -1)[b, s, :] = 展平后偏移 (b*S + s)*H*D 起的 H*D 个元素
实际返回:  out[p, h, d] 的内存偏移 = ((b*S + s)*H + h)*D + d
结论:      两者逐元素一致 → reshape 无损，语义上 out 就是 [B, S, H, D] 的展平视图
```

而如果启用被注释的 `fa_to_hf`，逻辑形状变成 `[B, H, S, D]`，内存顺序变为 `(batch, head, seqlen, dim)`——`head` 与 `seqlen` 两维错位，HF 的 reshape 会把不同位置的元素搅在一起，输出立刻错乱。

#### 4.2.3 源码精读

[moba/wrapper.py:L7-L15](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L7-L15) —— `hf_to_fa`：`[B, H, S, D]` 经 `permute(0,2,1,3)` 变 `[B, S, H, D]`，再 `reshape(-1, H, D)` 把前两维折叠成 `B*S`。仅在输入侧（prefill）使用。

[moba/wrapper.py:L18-L26](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L18-L26) —— `fa_to_hf`：逆操作，`[B*S, H, D]` 还原为 `[B, H, S, D]`。注意它还原的是**输入侧**布局；输出侧 HF 要的却是 `[B, S, H, D]` 序——这正是它被注释的深层原因。

[moba/wrapper.py:L83-L84](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L83-L84) —— 被注释的 `# out = fa_to_hf(out, batch)` 与最终 `return out, None`。`out` 保持 flash-attn 布局直接返回；`None` 是注意力权重占位。

[moba/moba_efficient.py:L270-L297](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L270-L297) —— `moba_attn_varlen` 的签名与文档：输入 `q/k/v` 均为 `[seqlen, head, head_dim]`（这里的 `seqlen` 是 varlen 打包后的总数，即 `batch*seqlen`），返回 `attn_output` 同样是 `[seqlen, head, head_dim]`。这与 wrapper 直接返回它的做法闭环吻合。naive 实现的返回形状同理（u2-l1 已验证）。

于是文档字符串里的「Returns: `[batch, q_len, q_heads, head_dim]`」应理解为**逻辑形状**：实际张量是它的展平视图 `[batch*q_len, q_heads, head_dim]`。写适配层时这种「形状注记内存序」的约定很常见，也是本讲最值得记住的工程技巧。

#### 4.2.4 代码实践

**实践目标**：用纯 PyTorch（CPU 即可，「示例代码」）复现 reshape 契约，亲眼验证「直接返回对、套 fa_to_hf 反而错」。

**操作步骤**：

```python
# 示例代码
import torch

B, S, H, D = 2, 4, 3, 5
# 逻辑上正确的注意力输出 [B, S, H, D]，用 arange 保证每个元素可追溯
logical = torch.arange(B * S * H * D, dtype=torch.float32).reshape(B, S, H, D)

# 场景 A：内核返回 FA 布局（直接折叠前两维）
fa_out = logical.reshape(B * S, H, D)          # 模拟 moba_attn_varlen 的返回
hf_a = fa_out.reshape(B, S, -1)                # HF 侧的 reshape(*input_shape, -1)
print("A 直接返回是否正确:", torch.equal(hf_a, logical))

# 场景 B：取消注释 fa_to_hf（先还原成 [B, H, S, D] 再交回 HF）
fa_to_hf = lambda x, b: x.view(b, -1, H, D).permute(0, 2, 1, 3)
hf_b = fa_to_hf(fa_out, B).reshape(B, S, -1)   # permute 后 reshape 会按逻辑序拷贝
print("B 套 fa_to_hf 是否正确:", torch.equal(hf_b, logical))
```

**需要观察的现象**：场景 A 打印 `True`；场景 B 打印 `False`，且 `hf_b` 与 `logical` 形状相同但元素错位（可用 `(hf_b - logical).abs().max()` 观察错位幅度）。

**预期结果**：A `True`、B `False`。这从内存序上证明了 [moba/wrapper.py:L83](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L83) 的注释不是可省的优化，而是正确性的必要条件。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `[B*S, H, D]` 的 `reshape(B, S, -1)` 无损，而 `[B, H, S, D]` 的同样 reshape 有害？
**答案**：reshape 只保证「按展平内存序重新分组」。`[B*S, H, D]` 的内存序是 `(batch, seqlen, head, dim)`，与目标 `(B, S, H*D)` 的分组边界一致；`[B, H, S, D]` 的内存序是 `(batch, head, seqlen, dim)`，`head` 插在了 `seqlen` 前面，重新分组时两组下标交错，元素全部错位。

**练习 2**：decode 分支的输出 `[B, 1, H, D]` 也满足这个契约吗？
**答案**：满足。它的展平序同样是 `(batch, seqlen=1, head, dim)`，HF 侧 `reshape(B, 1, -1)` 无损，无需任何转换。

**练习 3**：既然 `fa_to_hf` 在输出侧有害，它为什么还留在文件里？
**答案**：它是输入侧转换 `hf_to_fa` 的对称伙伴，文档价值与可读性考虑（也曾在开发期被尝试过）；保留被注释的调用恰恰提醒读者：输出侧依赖的是 reshape 契约，不是这对互逆函数。

### 4.3 GQA 的 KV 复制：repeat_interleave

#### 4.3.1 概念说明

GQA（Grouped-Query Attention）让多个 query 头共享一个 KV 头：`q_heads > kv_heads`，典型如 Llama-3.1-8B 是 32 个 Q 头共享 8 个 KV 头。flash-attn 的 `flash_attn_func` 原生支持这种不对称；但 **MoBA 的两个内核都要求 Q/KV 头数一致**（高效实现里 Q 与「块 × head」配对计算、naive 实现里 gate 矩阵按头对齐）。因此在 prefill 进内核前，必须把 KV 头沿 head 维复制 `q_heads // kv_heads` 份，扩张成与 Q 同头数。

#### 4.3.2 核心流程

```text
kv_replicas = q_heads // kv_heads           # 每组多少个 Q 头共享 1 个 KV 头
key   = repeat_interleave(key,   kv_replicas, dim=1)   # head 维连续复制
value = repeat_interleave(value, kv_replicas, dim=1)
```

`repeat_interleave` 是「相邻重复」：`[k0, k1]` 复制 2 份得 `[k0, k0, k1, k1]`。于是 Q 头 `h` 对应 KV 头 `h // kv_replicas`——与 transformers 内置 `repeat_kv` 的分组约定一致，保证扩张后每个 Q 头用的还是它本来该用的那份 K/V。

#### 4.3.3 源码精读

[moba/wrapper.py:L59-L61](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L59-L61) —— 先算 `kv_replicas = q_heads // kv_heads`，再对已经是 FA 布局（head 在 dim=1）的 K/V 做 `torch.repeat_interleave`。当模型是 MHA（`q_heads == kv_heads`）时 `kv_replicas == 1`，复制退化为恒等操作，无需分支。

对比 decode 分支（[moba/wrapper.py:L76-L82](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L76-L82)）：那里**没有**复制——`flash_attn_func` 原生接受 `q_heads != kv_heads`，内核内部按组共享计算。一个要手工扩张、一个不用，原因正是两个内核的能力差异。

代价提示：复制是**实体拷贝**，prefill 时 KV 的显存占用与搬运量都乘上 `q_heads/kv_heads`。对 8B 级模型（32/8）是 4 倍 KV 代价——这是当前实现为「内核头数一致」约束付出的工程成本。

#### 4.3.4 代码实践

**实践目标**：验证 `repeat_interleave` 的分组映射确实是「Q 头 `h` ↔ KV 头 `h // kv_replicas`」，并与 HF 的 `repeat_kv` 语义对齐（「示例代码」，CPU 可运行）。

**操作步骤**：

```python
# 示例代码
import torch

S, Hq, Hkv, D = 6, 8, 2, 4
k = torch.arange(S * Hkv * D, dtype=torch.float32).reshape(S, Hkv, D)  # FA 布局的 K
replicas = Hq // Hkv
k_exp = torch.repeat_interleave(k, replicas, dim=1)                    # [S, Hq, D]

ok = all(torch.equal(k_exp[:, h], k[:, h // replicas]) for h in range(Hq))
print("映射 h -> h//replicas 是否成立:", ok)
print("扩张后形状:", tuple(k_exp.shape), "显存倍数:", k_exp.numel() / k.numel())

# 对照 transformers 的 repeat_kv（若本机已安装 transformers，取消下行注释）
# from transformers.models.llama.modeling_llama import repeat_kv
# k_hf = k.permute(1, 0, 2).unsqueeze(0)                  # [1, Hkv, S, D]
# print("与 repeat_kv 等价:", torch.equal(repeat_kv(k_hf, replicas).squeeze(0).permute(1, 0, 2), k_exp))
```

**需要观察的现象**：映射检查打印 `True`；扩张后形状 `[6, 8, 4]`，显存倍数 `4.0`。

**预期结果**：如上。若取消最后两行注释且本机装了 transformers，`repeat_kv` 对照也应为 `True`（待本地验证——`repeat_kv` 的导入路径随版本可能变化）。

#### 4.3.5 小练习与答案

**练习 1**：`q_heads=32, kv_heads=8` 时，第 13 个 Q 头使用哪个 KV 头？
**答案**：`kv_replicas = 4`，`13 // 4 = 3`，即第 3 个 KV 头（0 起编）。

**练习 2**：为什么用 `repeat_interleave` 而不是 `repeat`（tile）？
**答案**：两者的排列顺序不同：`repeat_interleave` 得 `[k0,k0,k0,k0,k1,...]`，与「连续 4 个 Q 头一组共享一个 KV 头」的标准 GQA 分组一致；`repeat` 会得 `[k0,k1,...,k7,k0,k1,...]`，映射变成 `h % Hkv`，与 Q 投影权重的实际分组不符，注意力结果错误。

**练习 3**：decode 分支不复制 KV，除了内核支持外还有什么好处？
**答案**：decode 的 KV 来自 cache，避免复制既省显存也省一次拷贝内核启动；头数不对称直接交给 flash-attn 处理是零成本的正确路径。

### 4.4 prefill/decode 分支：q_len == kv_len 的判定与 decode 兜底

#### 4.4.1 概念说明

生成式推理的两个阶段（见第 2 节）在张量形状上有清晰指纹：

- **prefill**：无 KV cache 的整段前向，`q_len == kv_len`（query 和 key/value 都是完整序列）。
- **decode**：带 KV cache 的单步生成，`q_len == 1 < kv_len`。

wrapper 用 `q_len == kv_len` 这一个比较做分支——这是一个**启发式**而非严格判据：它把「q 与 kv 等长」等价于「完整序列的首次前向」。绝大多数场景成立；边界情形（如带 cache 的多 token 前向）会落入 decode 支路，仍得到正确（全量因果）结果，只是不享受稀疏加速。

decode 支路目前**没有** MoBA：直接退回 `flash_attn_func` 做全量因果注意力，源码里留着 `TODO release paged attn implementation`——即计划中的版本是把块稀疏接到 paged KV cache 上，这是仓库尚未完成的部分。

#### 4.4.2 核心流程

```text
if q_len == kv_len:                     # prefill
    q, k, v = hf_to_fa(...)             # [B,H,S,D] -> [B*S,H,D]
    k, v   = repeat_interleave(...)     # GQA 扩张到与 Q 同头数
    cu_seqlens = [0, kv_len, 2*kv_len, ..., B*kv_len]     # 假设 batch 内等长、无 padding
    out = moba_impl(q, k, v, cu_seqlens, max_seqlen=kv_len,
                    moba_chunk_size=cfg.moba_chunk_size, moba_topk=cfg.moba_topk)
else:                                   # decode（或任何 q_len != kv_len 的前向）
    q, k, v = transpose(1, 2)           # [B,H,S,D] -> [B,S,H,D]
    out = flash_attn_func(q, k, v, dropout, scaling, causal=True)   # 全量因果注意力
return out, None
```

prefill 侧构造 `cu_seqlens` 的方式值得注意：`[0] + [kv_len] * batch` 再 cumsum，即**假定 batch 内每条序列都恰好长 `kv_len`、没有 padding**。这与 `attention_mask` 被丢弃是同一个假设的两面——当前 wrapper 只服务「等长无 padding 批次」（示例里 batch=1 是天然安全的）。

#### 4.4.3 源码精读

[moba/wrapper.py:L54-L66](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L54-L66) —— prefill 支路：三个张量依次过 `hf_to_fa`；GQA 复制（4.3）；随后用 `torch.tensor([0] + [kv_len] * batch)` 加 `cumsum` 生成 int32 的 `cu_seqlens_k`。批量等长假设就在这个列表构造里。

[moba/wrapper.py:L67-L75](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L67-L75) —— 以关键字参数调用 `moba_impl`：`cu_seqlens` 与 `max_seqlen` 来自上一步，`moba_chunk_size/moba_topk` 来自被 `partial` 绑定的 `moba_config`。注意 `moba_attn_varlen` 的签名正是这五个参数加 q/k/v（[moba/moba_efficient.py:L270-L278](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L270-L278)），naive 实现同构——所以换后端（`moba` ↔ `moba_naive`）只需换 `partial` 绑定的第一个参数，wrapper 一行不改。

[moba/wrapper.py:L76-L82](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/wrapper.py#L76-L82) —— decode 支路：注释 `# TODO release paged attn implementation` 直白说明这是未完成的占位；三个张量仅 `transpose(1, 2)` 到 `[B, S, H, D]`（`flash_attn_func` 的布局，注意与 varlen 布局不同，这里没有折叠 batch）；调用 `flash_attn_func(query, key, value, dropout, scaling, True)`，第 6 个位置参数 `True` 即 `causal=True`（flash-attn 2.6.3 的签名依次是 `dropout_p`、`softmax_scale`、`causal`）。这里 `dropout` 与 `scaling` 终于被用上了。

**为什么 decode 用全量注意力不破坏 MoBA 语义？** 三层理由：

1. **设计层面**：MoBA 的卖点之一是全量/稀疏无缝切换（[README.md:L15](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md#L15)），且模型经过 continue training 才能发挥稀疏收益（[README.md:L21](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md#L21)）——训练本身就要求模型在两种模式间兼容。decode 切回全量，属于模型见过的合法模式。
2. **算符层面**：`q_len == 1` 时，这个 query 的「当前块」就是 cache 尾部包含它的块；MoBA 规定当前块必选。全量因果注意力等价于把 topk 拉满选中所有块——是 MoBA 算符在 \(k \to N\)（块总数）时的极限情形，输出仍是模型可接受的分布。
3. **工程层面**：decode 每步只有 1 个 query，块稀疏节省的计算本就有限，而 KV 分散在 paged cache 中、无法按整块 gather，直接复用 `flash_attn_func` 是正确性优先的最短路径。

#### 4.4.4 代码实践

**实践目标**：用 [examples/llama.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/examples/llama.py) 走一遍两阶段的真实切换，并核对「短提示下 prefill 也退化」的结论（u1-l2）与 decode 兜底的衔接。

**操作步骤**（需要 GPU 与已 `pip install .` 的环境；无 GPU 时做源码推演并将运行部分标注待本地验证）：

1. 阅读 [examples/llama.py:L31-L34](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/examples/llama.py#L31-L34)：prompt 为 `"how are you?"`，`max_length=32`。先用 tokenizer 数一下 prompt 的 token 数（约 12 个），对比默认 `--moba-chunk-size 4096`。
2. 运行 `python3 examples/llama.py --attn moba` 与 `--attn moba_naive`，比较两次生成文本。
3. 推演：第一次前向（prefill）时 `q_len == kv_len == 约12`，进入 moba_impl 后因序列不足一块而走 [moba/moba_efficient.py:L314-L321](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L314-L321) 的 `need_moba_attn=False` 兜底；其后每步 `q_len=1 < kv_len`，走 decode 支路。
4. （可选，待本地验证）在 `moba_layer` 的两个分支各临时加一行 `print(q_len, kv_len)`（在自己的副本脚本里，勿改源码），运行示例并记录每步的 `(q_len, kv_len)` 序列。

**需要观察的现象**：终端按生成步输出形如 `(12, 12), (1, 13), (1, 14), ...` 的序列——第一项证明 prefill 只发生一次，其后恒为 decode。

**预期结果**：`moba` 与 `moba_naive` 两个后端生成文本一致（都退化为全量注意力，u1-l2 的结论）；`(q_len, kv_len)` 序列如上。

#### 4.4.5 小练习与答案

**练习 1**：什么情况下 `q_len != kv_len` 但 `q_len > 1`？会走哪条支路、结果如何？
**答案**：带 KV cache 的多 token 前向，例如 chunked prefill、投机解码（assistant tokens）一次验证多个候选。按判定走 decode 支路，`flash_attn_func(causal=True)` 给出**正确**的全量因果注意力——语义无误，只是没有稀疏加速。

**练习 2**：batch 内两条序列长度不同（用 padding 对齐）时，这个 wrapper 会怎样？
**答案**：prefill 支路按 `[kv_len] * batch` 均匀构造 `cu_seqlens`，把 padding token 当成有效内容划进各序列边界，块划分与 batch 边界错位，输出错误。`attention_mask` 被丢弃意味着没有救场通道——当前实现假设等长无 padding 批次（示例 batch=1 天然满足）。

**练习 3**：decode 支路把 `scaling` 传给了 `flash_attn_func`，prefill 支路却没有用 `scaling`。两者对标准 Llama 为何无感差异？
**答案**：HF 的 `module.scaling` 默认是 `head_dim ** -0.5`，decode 转发的就是这个值；prefill 侧 MoBA 内核内部固定使用 \(1/\sqrt{d}\)（[moba/moba_efficient.py:L86](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L86)）。默认 config 下两者数值相同，差异只在模型自定义缩放时暴露。

### 4.5 ALL_ATTENTION_FUNCTIONS：自定义注意力后端的注册机制

#### 4.5.1 概念说明

transformers 4.48 的注意力重构把「算法选择」变成一次字典查询：模型注意力 forward 里大致是

```python
# 摘自 transformers 4.48.x modeling_llama.py（示意，行号以本机为准）
attention_interface: Callable = eager_attention_forward
if self.config._attn_implementation != "eager":
    attention_interface = ALL_ATTENTION_FUNCTIONS[self.config._attn_implementation]

attn_output, attn_weights = attention_interface(
    self, query_states, key_states, value_states,
    attention_mask, dropout=..., scaling=self.scaling, sliding_window=..., **kwargs,
)
attn_output = attn_output.reshape(*input_shape, -1).contiguous()
```

`ALL_ATTENTION_FUNCTIONS` 是 `transformers.modeling_utils` 里的全局字典（内置 `eager`、`sdpa`、`flash_attention_2` 等键）。MoBA 的接入方式是**直接往这张表里写两个新键**——不 fork 框架、不继承模型类，这正是注册表模式的威力：框架按名字查表，谁写进表谁就生效。

#### 4.5.2 核心流程

```text
register_moba(cfg)
  ├─ ALL_ATTENTION_FUNCTIONS["moba_naive"] = partial(moba_layer, moba_attn_varlen_naive, cfg)
  └─ ALL_ATTENTION_FUNCTIONS["moba"]       = partial(moba_layer, moba_attn_varlen,      cfg)

用户: from_pretrained(..., attn_implementation="moba")
  └─ 名字 "moba" 被写入 config._attn_implementation（加载时校验该名字已在注册表中 → 注册必须在前）

每次注意力 forward:
  ALL_ATTENTION_FUNCTIONS["moba"](module, q, k, v, mask, dropout=..., scaling=...)
  = moba_layer(moba_attn_varlen, cfg, module, q, k, v, ...)
```

`partial` 在这里的分工：绑定「**内核函数**」与「**配置**」这两个 MoBA 内部参数，让包装器对外呈现 HF 要的 `(module, query, key, value, ...)` 形状；两个后端共享同一个 `moba_layer`，仅内核不同——换后端零代码改动的根源在此（u1-l2 的 `--attn` 选项就是换注册键名）。

#### 4.5.3 源码精读

[moba/__init__.py:L1-L6](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/__init__.py#L1-L6) —— 依赖关系一目了然：从 `transformers.modeling_utils` 导入注册表（这也是 [requirements.txt:L2](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/requirements.txt#L2) 锁定 `transformers>=4.48.3` 的原因——4.48 重构后注册表才落在这个位置、注意力接口才有本讲的契约）；同时导入 wrapper 与两个内核，`from moba import register_moba, MoBAConfig` 的对外出口就是这两者。

[moba/__init__.py:L9-L11](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/__init__.py#L9-L11) —— `register_moba` 全部两行：`partial(moba_layer, 内核, cfg)` 分别以 `"moba_naive"`、`"moba"` 为键写入注册表。三个推论值得记住：

1. **进程级全局**：写的是 transformers 模块里的字典，本进程内所有 HF 模型共用；对 `moba` 键重复调用 `register_moba` 会静默覆盖，后写者生效。
2. **配置被闭包捕获**：`cfg` 与特定模型实例无关——两个模型想用不同 `chunk_size/topk` 时，须交替「注册 → 加载 → 再注册 → 再加载」，且同键只有一个当前值；改 `model.config` 不会影响已绑定的 `cfg`。
3. **内置后端不受影响**：MoBA 增加新键而非改写 `sdpa`/`flash_attention_2`，[examples/llama.py:L13-L18](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/examples/llama.py#L13-L18) 的 `choices` 里保留 `flash_attention_2` 作全量基线正是利用这一点。

[moba/config.py:L4-L7](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/config.py#L4-L7) —— `MoBAConfig` 仅有两个字段；它不进模型 config、不进 checkpoint，只活在 `partial` 的闭包里。

[examples/llama.py:L21-L34](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/examples/llama.py#L21-L34) —— 端到端时序：第 21 行**先**注册（此时 `MoBAConfig(4096, 12)` 被绑定）；第 22-28 行 `from_pretrained` 以 `attn_implementation=args.attn` 选中后端——顺序颠倒会在名字校验处失败（u1-l2 已验证）；第 34 行 `model.generate` 触发前向，注意力层每次按注册键查到我们的 `partial`。

#### 4.5.4 代码实践

**实践目标**：观察注册表的真实结构，验证「写键 → 查表 → partial 解包」三步（「示例代码」，需已 `pip install .`；无 GPU 时 import 可能失败，可仅作代码阅读并标注待本地验证）。

**操作步骤**：

```python
# 示例代码
from functools import partial
from transformers.modeling_utils import ALL_ATTENTION_FUNCTIONS

print("注册前:", sorted(ALL_ATTENTION_FUNCTIONS))

from moba import register_moba, MoBAConfig
register_moba(MoBAConfig(moba_chunk_size=64, moba_topk=2))

print("注册后:", sorted(ALL_ATTENTION_FUNCTIONS))

fn = ALL_ATTENTION_FUNCTIONS["moba"]
print("本质是 partial:", isinstance(fn, partial))
print("原函数:", fn.func.__name__)                  # 预期 moba_layer
print("绑定参数:", fn.args[0].__name__, fn.args[1])  # 预期 moba_attn_varlen MoBAConfig(...)
# 再注册一次不同配置，观察覆盖
register_moba(MoBAConfig(moba_chunk_size=128, moba_topk=3))
print("覆盖后:", ALL_ATTENTION_FUNCTIONS["moba"].args[1])
```

**需要观察的现象**：注册前列表含 `eager`、`flash_attention_2`、`sdpa` 等内置键；注册后多出 `moba` 与 `moba_naive`；`fn.func` 是 `moba_layer`，`fn.args` 恰为 `(moba_attn_varlen, MoBAConfig(64, 2))`；二次注册后配置变成 `(128, 3)`。

**预期结果**：如上。`partial` 对象的 `.func/.args/.keywords` 属性让「注册了什么」完全可透视——排查「为什么我的 topk 没生效」时，先查这张表。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `register_moba` 必须在 `from_pretrained` 之前调用？
**答案**：`from_pretrained(attn_implementation="moba")` 在加载时校验该名字是受支持的注意力实现并把名字写入 `config._attn_implementation`；名字尚未入表则校验失败。注册在前，名字才是「已支持」。

**练习 2**：`partial(moba_layer, moba_attn_varlen, cfg)` 之后，HF 调用 `fn(module, q, k, v, mask, dropout=..., scaling=...)`，实际执行的完整调用是什么？
**答案**：`moba_layer(moba_attn_varlen, cfg, module, q, k, v, mask, dropout=..., scaling=...)`——绑定的两个参数占据位置参数前缀，HF 的实参从 `module` 起顺延。

**练习 3**：进程里已加载了一个用 `moba` 后端的模型，此刻再调用 `register_moba(新配置)`，对已加载模型有影响吗？
**答案**：有。模型 forward 每次都现场查表 `ALL_ATTENTION_FUNCTIONS["moba"]`，重新注册改变了这个键的值，已加载模型的下一次前向立即使用新配置（闭包中的 `cfg` 被替换）。这一「热改」特性可用来实验不同 chunk/topk，但也意味着注册表是全局可变状态，需谨慎管理。

## 5. 综合实践

**任务**：把「`model.generate` → `moba_layer`」的完整调用链落到你自己安装的 transformers 上，写成一份时序说明，并回答两个关键问题。这是本讲规格指定的主实践。

**步骤 1：定位并阅读 transformers 侧代码（纯阅读，无环境要求）**

1. 找到本机 transformers 安装目录（如 `python -c "import transformers; print(transformers.__file__"` 所示路径的父目录）。
2. 打开 `models/llama/modeling_llama.py`，找到 `LlamaAttention.forward`（4.48.x 中通常位于类内，含 `eager_attention_forward` 兜底），确认三件事并记下行号：① `input_shape = hidden_states.shape[:-1]`；② `ALL_ATTENTION_FUNCTIONS[self.config._attn_implementation]` 的查表分发；③ `attn_output.reshape(*input_shape, -1).contiguous()`。这正是 4.2 节 reshape 契约的出处。
3. 打开 `modeling_utils.py`，找到 `ALL_ATTENTION_FUNCTIONS` 的定义处，列出内置键；再找到 `from_pretrained` 中对 `attn_implementation` 的校验逻辑，确认「注册必须在前」。
4. 顺着 `generation/utils.py`（`GenerationMixin.generate` → 采样循环 → `model(...)`）与 `modeling_llama.py`（`LlamaForCausalLM.forward` → `LlamaModel.forward` → decoder layer → attention）补全调用链。

**步骤 2：写出时序说明（交付物）**

参考答案（以 4.48.x 为准，行号请以步骤 1 的实测为准）：

```text
model.generate(input_ids, max_length=32, do_sample=False)
 └─ GenerationMixin.generate → 贪心采样循环 _sample
     └─ LlamaForCausalLM.forward(input_ids)              # logits [B, S, vocab]
         └─ LlamaModel.forward                            # 构造 causal_mask
             └─ LlamaDecoderLayer.forward (×num_layers)
                 └─ LlamaAttention.forward
                     1) q/k/v = proj(hidden).view(B,S,H,D).transpose(1,2)   # [B,H,S,D]
                     2) attention_interface = ALL_ATTENTION_FUNCTIONS["moba"]
                        #  = partial(moba_layer, moba_attn_varlen, cfg)
                     3) attn_output, _ = attention_interface(self, q, k, v,
                            attention_mask, dropout=0.0, scaling=self.scaling, ...)
                          └─ moba_layer(impl, cfg, module, q, k, v, mask, ...)
                               ├─ prefill (q_len==kv_len): hf_to_fa → GQA 复制
                               │    → cu_seqlens=[0,kv_len,...] → moba_attn_varlen
                               └─ decode  (q_len<kv_len):  transpose → flash_attn_func(causal=True)
                     4) attn_output = attn_output.reshape(B, S, -1)          # reshape 契约
                     5) o_proj → 返回 logits
     └─ argmax → 追加下一个 token → 下一轮 q_len=1 → decode 支路
```

**步骤 3：回答两个问题（写进交付物）**

1. **为什么 decode 阶段（`q_len=1`）直接用 `flash_attn_func` 不会破坏 MoBA 语义？** 参考要点见 4.4.3：无缝切换是 MoBA 的训练目标之一（[README.md:L15](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md#L15)、[L21](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md#L21)）；`q_len=1` 时全量因果注意力是「topk 拉满所有块」的极限情形，当前块（cache 尾块）必然被覆盖；工程上 decode 单 query 收益小且 KV 在 cache 中，TODO 指向 paged attention 实现。
2. **为什么注释掉的 `fa_to_hf` 不影响正确性？** 参考要点见 4.2：HF 侧 `attn_output.reshape(B, S, -1)` 只依赖展平内存序；FA 布局 `[B*S, H, D]` 的内存序恰是 `(batch, seqlen, head, dim)`，与逻辑 `[B, S, H, D]` 一致；反而启用 `fa_to_hf` 会得到 `[B, H, S, D]`，head 与 seqlen 错位导致输出错乱。

**步骤 4（可选，需 GPU，待本地验证）**：不改源码，用「再注册」技巧插桩——写一个包装函数打印 `(q_len, kv_len, query.shape)` 后调用真 `moba_layer`，`register_moba` 后用它覆盖 `ALL_ATTENTION_FUNCTIONS["moba"]`，运行 [examples/llama.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/examples/llama.py) 验证步骤 2 时序中每一步的真实形状。

**验收标准**：时序图每一跳都能对应到本机 transformers 的具体文件与行号；两个问题能用「reshape 契约」「无缝切换/极限情形」这两个本讲概念独立复述。

## 6. 本讲小结

- `moba_layer` 是标准适配器：对上满足 HF 注意力接口契约 `(module, q, k, v, *args, dropout, scaling, **kwargs) → (out, None)`，对下按 MoBA 内核的 varlen 约定（`q/k/v + cu_seqlens + max_seqlen + chunk/topk`）传参，多余参数经 `*args/**kwargs` 静默丢弃。
- 输出侧依赖 **reshape 契约**而非形状转换：flash-attn 布局 `[B*S, H, D]` 的展平内存序恰是 `(batch, seqlen, head, dim)`，直接返回即正确；被注释的 `fa_to_hf` 若启用反而会因 head/seqlen 错位破坏输出。
- prefill 分支三件事：`hf_to_fa` 转布局、`repeat_interleave` 按 `h → h//kv_replicas` 把 GQA 的 KV 扩张到与 Q 同头数（MoBA 内核要求头数一致，代价是 KV 显存 ×q_heads/kv_heads）、按 `[0, kv_len, 2·kv_len, ...]` 构造 cu_seqlens（隐含等长无 padding 假设）。
- decode 分支以 `q_len == kv_len` 的反例兜底：转置后直接 `flash_attn_func(causal=True)` 全量注意力——正确性由 MoBA 的全量/稀疏无缝切换训练目标与「topk 拉满」极限情形保证，效率优化留给 TODO 的 paged attention。
- 注册机制一句话：`partial(moba_layer, 内核, cfg)` 写入全局字典 `ALL_ATTENTION_FUNCTIONS`，新增 `moba`/`moba_naive` 两键；注册必须先于 `from_pretrained`，配置活在闭包里、重注册即热改全局。

## 7. 下一步学习建议

本讲把「内核」与「框架」之间的桥走通了。接下来：

- **u4-l2（正确性测试）**：适配层声称「换后端零改动」，`tests/test_moba_attn.py` 用参数化网格与双容忍度断言实证 naive 与 efficient 的前向/反向对齐——那里能看到这条桥两端必须严丝合缝的量化标准。
- **u4-l3（性能基准）**：`tests/test_moba_speedup.py` 讲解预热、`cuda.synchronize` 计时与加速比统计；读完可自行测量 GQA 复制与索引重组在多大序列上摊薄。
- **回看 u3 系列的一个新视角**：本讲确认了 decode 走全量注意力后，可以带着「paged KV cache 下如何按块 gather」的问题重读 u3-l4 的 varlen trick，思考 TODO 中 paged attention 实现的难点（块不再连续、需经 block table 间接寻址）。
- 若你打算做二次开发：仿照 4.5 的「再注册插桩」与 u4-l4 的改造任务，先在注册表层做实验，再下沉到内核层，是改动面最小的路径。
