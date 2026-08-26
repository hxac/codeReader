# u6-l3 Qwen3 集成与注意力后端选择

## 1. 本讲目标

上一讲（u6-l2）我们逐行精读了 `LlamaBitDecoding` 的 prefill/decode 双路径。本讲换一个视角：**把 BitDecoding 接入一个新模型，到底要改哪些代码？**

我们以 `evaluation/qwen3.py` 为教材——它是作者把同一套 BitDecoding 逻辑「移植」到 Qwen3 模型上的产物。通过与 `llama.py` 做 diff 式对照，你会看到哪些代码是逐字复制的「通用模板」、哪些是模型特有的「适配层」，最终总结出一套可复用的接入流程。学完本讲你应该能：

1. 说出接入一个新 HF 模型必需的三块改动：**attention 类、cache 注入、config 注入**。
2. 理解 `config.num_bits / quant_mode / group_size / residual_block_size` 如何从命令行一路传导到 attention 层内部的 `self.` 属性。
3. 理解 `bit_decoding`、`flash_attention_2`、`flash_decoding`、`eager` 四个后端的切换机制——它由「项目自定义的 `attn_backend` 字段 + 本地类注册表 + transformers 全局注册表」三层共同完成。
4. 独立写出「把 BitDecoding 接入 Qwen2」这类任务的实施步骤文档。

## 2. 前置知识

- **模型文件复制改造法**：BitDecoding 不修改 transformers 安装包，而是把官方 `modeling_*.py` 整文件复制到 `evaluation/` 下，改少量导入与 attention 类。这样既能自由改动前向逻辑，又能用 `from_pretrained` 正常加载官方权重（权重名不变）。
- **attention 后端（attn backend）**：指注意力计算的具体实现（eager 朴素实现、flash_attention_2、flash_decoding、bit_decoding）。HuggingFace 官方字段叫 `_attn_implementation`；BitDecoding 又自定义了一个 `attn_backend` 字段，两者如何配合是本讲重点之一。
- **类注册表（registry）**：一个 `{名字: 类}` 的字典。DecoderLayer 构造时用 `config.attn_backend` 当 key 查表，决定实例化哪个 attention 类。这是 HF 老版 modeling 文件的经典写法。
- **QK-Norm**：Qwen3 特有——对 Query 与 Key 的每个 head 做 RMSNorm（Llama 没有）。这决定了 Qwen3 版代码在投影后多一步 `q_norm/k_norm`。
- **GQA**：Grouped-Query Attention，KV 头数少于 Q 头数。`nheads_k` 指 KV 头数，打包缓存按 KV 头存储，Q 头在 kernel 内按组共享（u3-l1 讲过 `seqlenq_ngroups_swapped` 重排）。

如果残余机制（residual）、`update_pack`/`update_residual`/`clear_residual` 这三个缓存方法你已经忘记，请先回看 u2-l2 与 u6-l1，本讲直接使用这些结论。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `evaluation/qwen3.py` | 复制自新版 transformers 的 Qwen3 modeling 文件，加入 BitDecoding 支持 | `Qwen3BitDecoding`、`QWEN_ATTENTION_CLASSES`、QK-Norm 适配 |
| `evaluation/llama.py` | 复制自旧版 transformers 的 Llama modeling 文件（u6-l2 已精读） | 作为对照基准，找出「同构模板」与「模型特有」部分 |
| `evaluation/example.py` | GSM8K 端到端生成入口 | 猴子补丁、config 注入、模型选择、命令行参数 |
| `evaluation/bench_throughput.py` | 吞吐基准入口 | 同样的 config 注入模式、后端切换做性能对比 |
| `evaluation/scripts/example.sh` | 运行脚本 | 实际运行命令与后端参数 |

## 4. 核心概念与源码讲解

### 4.1 同构改造法：qwen3.py 与 llama.py 的逐段对照

#### 4.1.1 概念说明

「同构」指两份模型文件中 BitDecoding 相关代码结构完全平行：同一个 attention 基类携带同样的量化配置属性、一个几乎逐字相同的 BitDecoding 子类、一个同构的注册表、同一处 DecoderLayer 查表改动。理解同构的意义在于：**接入新模型时，decode 分支的代码可以作为模板直接复制，只需要处理「模型特有的前奏」**（投影、Norm、RoPE 的布局习惯）。

两份文件的世代不同：`llama.py` 基于较老的手写 modeling 风格（attention 返回三元组、用 `_flash_attention_forward` 私有函数），`qwen3.py` 由新版 transformers 的 modular 体系自动生成（文件头有 auto-generated 声明，用 `ALL_ATTENTION_FUNCTIONS` 公开注册表与 `attention_interface` 抽象）。这恰好演示了同一套 BitDecoding 逻辑在两个 transformers API 世代下的两种写法。

#### 4.1.2 核心流程

把两份文件各切成五段，同构关系如下：

```text
段                llama.py                     qwen3.py                    同构程度
─────────────────────────────────────────────────────────────────────────────────────
① 导入与缓存      from bit_decode import       from bit_decode import      完全相同
                  Cache/DynamicCache/...       Cache/DynamicCache/...

② attention 基类  LlamaAttention              Qwen3Attention              模板相同
                  __init__ 末尾读 5 个        __init__ 末尾读 5 个
                  config 量化字段             config 量化字段

③ BitDecoding     LlamaBitDecoding            Qwen3BitDecoding            decode 分支
   子类           (L573-L758)                 (L330-L487)                 逐字级相同；
                                                                           prefill 前奏不同

④ 注册表          LLAMA_ATTENTION_CLASSES     QWEN_ATTENTION_CLASSES      键完全一致
                  (L761-L766)                 (L489-L494)

⑤ DecoderLayer    查表实例化 self_attn        查表实例化 self_attn        一行级相同
   查表           (L774)                      (L500)
```

模型特有的差异只有三处：Qwen3 多了 `q_norm/k_norm`（Llama 没有）；Qwen3 有 `sliding_window` 传参（源码里作者特意注释了 `# diff with Llama`）；两文件的 RoPE 时机与 transpose 顺序略有不同（见 4.2.2）。

#### 4.1.3 源码精读

**① 导入与缓存注入**——两份文件完全一致的三行：

[evaluation/qwen3.py:L47-L49](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L47-L49) 引入 flash-attn 的 `flash_attn_with_kvcache`（flash_decoding 后端用）、`bit_decode` 的两个 kernel 入口，以及改造版的三个 Cache 类。注意第 28 行官方 `from transformers.cache_utils import ...` 被注释掉了——模型文件内部的 `DynamicCache()` 由此全部落到改造版（例如 [evaluation/qwen3.py:L678-L680](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L678-L680) 中 `Qwen3Model.forward` 里新建的缓存就是改造版，附带一行调试 `print`）。`llama.py` 对应位置在 [evaluation/llama.py:L54-L57](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L54-L57)，写法相同。

**② 基类携带量化配置**：

[evaluation/qwen3.py:L204-L208](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L204-L208) 在 `Qwen3Attention.__init__` 末尾把 config 上的量化字段拷贝成实例属性：`num_bits`、`pack_nums = 16 / num_bits`、`quant_mode`、`group_size`、`residual_block_size`。llama.py 的对应段 [evaluation/llama.py:L286-L290](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L286-L290) 逐字相同。这就是「config 注入」的终点站：命令行参数到这里变成 `self.num_bits` 等属性，供 forward 直接使用。

**④ 两张同构的注册表**：

[evaluation/qwen3.py:L489-L494](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L489-L494) 定义 `QWEN_ATTENTION_CLASSES`，四个键：`eager` 与 `flash_attention_2` 都映射到通用 `Qwen3Attention`（靠 transformers 全局注册表二次分发，见 4.3），`flash_decoding` 映射到 `Qwen3FlashDecodingAttention`，`bit_decoding` 映射到 `Qwen3BitDecoding`。[evaluation/llama.py:L761-L766](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L761-L766) 的 `LLAMA_ATTENTION_CLASSES` 键完全一致，差别只在 `flash_attention_2` 映射到专用类 `LlamaFlashAttention2`（老版 API 风格）。

**⑤ DecoderLayer 查表**：

[evaluation/qwen3.py:L500](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L500) 与 [evaluation/llama.py:L774](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L774) 都是 `XXX_ATTENTION_CLASSES[config.attn_backend](config=config, layer_idx=layer_idx)`——后端切换的全部魔法就是这一行查表。llama.py 的 L775-L777 还保留了三行被注释的直接实例化写法，是作者改造前的痕迹。

#### 4.1.4 代码实践

**实践：用 diff 工具量化两份文件的同构程度（源码阅读型，无需 GPU）**

1. 实践目标：用真实 diff 输出验证「decode 分支逐字相同、prefill 前奏不同」的判断，并得到一份可复制的「模板代码」清单。
2. 操作步骤：
   - 进入 `evaluation/` 目录；
   - 提取两个 BitDecoding 类的 decode 分支做比对：
     ```bash
     sed -n '648,683p' llama.py > /tmp/llama_decode.txt
     sed -n '363,409p' qwen3.py  > /tmp/qwen_decode.txt
     diff /tmp/llama_decode.txt /tmp/qwen_decode.txt
     ```
   - 再比对 prefill 分支：`sed -n '689,750p' llama.py` 对 `sed -n '411,483p' qwen3.py`。
3. 需要观察的现象：decode 分支的 diff 应只剩空行与个别变量名差异；prefill 分支的 diff 应明显更大（注意力接口、Norm、transpose 顺序、k-tensor else 分支）。
4. 预期结果：decode 分支差异行数接近 0，prefill 分支差异行数显著更多——这就是「模板部分」与「适配部分」的边界。若 diff 显示 decode 分支也有大量差异，请回头核对是否取错了行段。

#### 4.1.5 小练习与答案

**练习 1**：`qwen3.py` 文件头声明「This file was automatically generated from modular_qwen3.py. Do NOT edit this file manually」。BitDecoding 的作者却直接编辑了它。这带来什么维护成本？

**答案**：升级 transformers 版本时，无法再用官方 modular 流水线重新生成该文件，否则会覆盖 BitDecoding 改动；这份复制品从此变成需要人工同步官方修复（如安全补丁、API 变更）的 fork。这是「复制改造法」的固有代价，换来的是完全自由的修改权。

**练习 2**：两张注册表的 `flash_attention_2` 键映射的类为什么不同（`Qwen3Attention` vs `LlamaFlashAttention2`）？

**答案**：两文件基于的 transformers 世代不同。新版 qwen3.py 用 `ALL_ATTENTION_FUNCTIONS` 全局注册表在运行期选择注意力函数，通用 `Qwen3Attention` 一个类即可承载多种实现；旧版 llama.py 的惯例是为每种实现写一个子类，故 `flash_attention_2` 有专用类。对接入新模型而言两种写法都可行，但应与所选 transformers 版本的官方 modeling 文件风格保持一致，减少 diff 面积。

### 4.2 Qwen3BitDecoding 前向：QK-Norm、RoPE 布局与双路径

#### 4.2.1 概念说明

`Qwen3BitDecoding` 继承 `Qwen3Attention`，是 Qwen3 侧的 bit_decoding 后端。它与 `LlamaBitDecoding`（u6-l2）职责相同：prefill 时先跑 FP16 注意力再量化打包，decode 时走低比特 kernel。本模块聚焦两件事：一是 Qwen3 特有的前奏如何适配（QK-Norm、RoPE 的张量布局）；二是 diff 中发现的与 llama.py 的一处**真实功能差异**（k-tensor 分支缺失）。

#### 4.2.2 核心流程

Qwen3 前奏的布局路线图（对照 llama.py 的差异用 ⚠ 标出）：

```text
hidden_states (b, s, hidden)
  │ q_proj/k_proj/v_proj + view → (b, s, h, d)
  │ ⚠ q_norm/k_norm 仅 Qwen3 有，作用在 (b, s, h, d) 上（V 不过 norm）
  ▼
query/key/value (b, s, h, d)
  │ transpose(1,2) → (b, h, s, d)，apply_rotary_pos_emb
  ▼
RoPE 后的 q/k (b, h, s, d)
  ├─ q_len == 1（decode）：转回 (b, s, h, d) → 拼残余区 → fwd_kvcache_int
  └─ q_len > 1（prefill）：直接以 (b, h, s, d) 调注意力；K 转回 (b, s, h, d) 后切打包区/残余区 → kvcache_pack_int
```

llama.py 的顺序是「投影后立刻 transpose 到 (b,h,s,d) 做 RoPE，再统一转回 (b,s,h,d)」（L619-L639）；qwen3.py 因为 QK-Norm 要求 (b,s,h,d) 布局（norm 作用在最后一维 head_dim 上），先 norm 再 transpose。**两条路线殊途同归：进入 `fwd_kvcache_int` / `kvcache_pack_int` 的张量都是 (b, s, h, d) flash 布局**——这是 kernel 接口对调用方的硬约束。

#### 4.2.3 源码精读

**QK-Norm 前奏**：

[evaluation/qwen3.py:L349-L351](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L349-L351) 依次做 `q_norm(q_proj(...).view(...))`、`k_norm(k_proj(...).view(...))`、`v_proj(...).view(...)`——Q/K 过 RMSNorm、V 不用过，三者都保持 (b, s, h, d)。随后 [evaluation/qwen3.py:L355-L357](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L355-L357) 把 Q/K 转到 (b, h, s, d) 施加 RoPE。对照 llama.py [evaluation/llama.py:L619-L633](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L619-L633)：投影后直接 transpose 再 RoPE，没有 norm 步骤。

**decode 分支（与 llama.py 逐字同构）**：

[evaluation/qwen3.py:L366-L373](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L366-L373) 把 Q/K 转回 flash 布局，`update_pack(None, None, None, None, layer_idx)` 以「全 None 读模式」取出该层四个打包缓存张量，并用 `v_pack.shape[1]` 得到主缓存 token 数构造 `seqlens_k`。[evaluation/qwen3.py:L380-L387](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L380-L387) 分配补零对齐的 `residual_block_size` 长度缓冲，`update_residual` 追加新 token 后把有效区拷入。[evaluation/qwen3.py:L390-L403](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L390-L403) 调 `fwd_kvcache_int`，实参表与 llama.py [evaluation/llama.py:L666-L679](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L666-L679) 完全平行（26 个参数，u2-l3 已逐个精读）。[evaluation/qwen3.py:L405-L407](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L405-L407) 在 `cur_residual_len == residual_block_size` 时用 `*_new` 四件套 `update_pack` 拼回主缓存并 `clear_residual`——残余闭环与 u5-l4 讲的 kernel 侧「原位再量化」首尾相接。

**prefill 分支与 k-tensor 差异**：

[evaluation/qwen3.py:L413-L434](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L413-L434) 先经 `ALL_ATTENTION_FUNCTIONS["flash_attention_2"]` 算出精确的 FP16 注意力输出；随后 [evaluation/qwen3.py:L446-L455](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L446-L455) 按取模切出残余区，并**仅在 `quant_mode == 'k-channel'` 分支内**分配 `k_pack/k_params`。对照 llama.py [evaluation/llama.py:L714-L722](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L714-L722)：llama.py 还带一个 `else` 分支按 k-tensor 布局分配，qwen3.py 没有复制它。也就是说，若对 Qwen3 传 `--quant_mode k-tensor`，prefill 会在 [evaluation/qwen3.py:L467](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L467) 引用未赋值的 `k_pack` 而抛 `UnboundLocalError`。这与第三单元结论一致：当前仓库全链路只真正支持 k-channel，qwen3.py 只是把这个事实写得更「诚实」。[evaluation/qwen3.py:L467-L478](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L467-L478) 调 `kvcache_pack_int` 打包并 `update_pack` 入缓存；[evaluation/qwen3.py:L480-L483](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L480-L483) 预分配 decode 阶段复用的 `*_new` 四个块缓冲（u6-l2 讲过这一设计）。

另有一处小差异值得记录：qwen3.py 把 `sliding_window=self.sliding_window` 传给注意力接口，并在 [evaluation/qwen3.py:L253](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L253) 留注释 `# diff with Llama`。但 BitDecoding kernel 并不消费 sliding window——接入带滑窗的模型时这是需要自行验证的边界（Qwen3-8B 默认 `use_sliding_window=False`，不触发）。

#### 4.2.4 代码实践

**实践：验证 Qwen3 的 k-tensor 路径确实不可用（无 GPU 时为源码阅读型推断）**

1. 实践目标：确认「qwen3.py 只支持 k-channel」不是文档口误而是代码事实，并练习用最小改动定位缺陷。
2. 操作步骤：
   - 在 [evaluation/qwen3.py:L450](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L450) 的 `if self.quant_mode == 'k-channel':` 附近阅读作用域：`k_pack/k_params` 只在该分支内赋值，而 L467 无条件使用；
   - 若有 GPU 与 Qwen3-8B 权重，运行 `python example.py --model_path Qwen/Qwen3-8B --num_bits 4 --quant_mode k-tensor --group_size 128 --attn_backend bit_decoding`；
   - 无 GPU 时，用纯 Python 语义复现：写一个 5 行函数，`if mode == 'a': x = 1`，随后 `print(x)`，以 `mode='b'` 调用。
3. 需要观察的现象：prefill 第一个 token 前向即抛 `UnboundLocalError: local variable 'k_pack' referenced before assignment`（纯 Python 复现版同理）；而不是静默算错。
4. 预期结果：错误发生在 Python 层、尚未进入 CUDA kernel。结论：Qwen3 集成当前仅支持 `--quant_mode k-channel`。GPU 路径**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `q_norm/k_norm` 必须在 transpose 到 (b, h, s, d) **之前**做？

**答案**：RMSNorm 沿最后一维（head_dim）归一化。投影后天然的 (b, s, h, d) 布局最后一维正是 head_dim，可直接 norm；transpose 后最后一维仍是 head_dim，其实也能做——但 `Qwen3Attention` 基类的官方写法是在 view 后立即 norm（见 [evaluation/qwen3.py:L223-L224](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L223-L224)），BitDecoding 子类沿用这一布局习惯，保持与基类、与权重语义一致，也避免在 norm 与 RoPE 之间多做一次无谓的布局往返。

**练习 2**：`fwd_kvcache_int` 的第 14 个实参 `self.residual_block_size` 在 u3-l1 被称为「未被消费的哑参数」。本讲的源码如何印证这一点？

**答案**：[evaluation/qwen3.py:L390-L403](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L390-L403) 里 Python 侧真正决定行为的是 `self.k_pack_new` 等 buffer 的形状（由 prefill 分配时用 `residual_block_size` 算好）与 `cur_residual_len`（new_lens）；而 C++ 侧 kernel 的块大小由编译期常量（kBlockN_pack）决定，与该运行期参数无关。Python 传它是为了对齐 26 参数的位置签名。

### 4.3 后端注册表：QWEN_ATTENTION_CLASSES 与双层注意力选择机制

#### 4.3.1 概念说明

后端切换由两个注册表、两个字段协同完成：

- **本地类注册表**（`QWEN_ATTENTION_CLASSES`）：决定实例化**哪个类**，key 是项目自定义的 `config.attn_backend`；
- **transformers 全局函数注册表**（`ALL_ATTENTION_FUNCTIONS`）：决定调用**哪个注意力函数**，key 官方的是 `config._attn_implementation`，但基类里被作者改成了 `config.attn_backend`。

「双层」是指：选 `eager/flash_attention_2` 时先经本地表落到通用 `Qwen3Attention`，再经全局表选函数；选 `flash_decoding/bit_decoding` 时本地表直接落到专用子类，子类内部自己写死逻辑。这套设计让一个命令行参数同时路由两个注册表。

#### 4.3.2 核心流程

```text
config.attn_backend = "bit_decoding"          # 命令行注入
        │
        ▼  DecoderLayer 查本地表
QWEN_ATTENTION_CLASSES["bit_decoding"] → Qwen3BitDecoding   → 子类自管逻辑
QWEN_ATTENTION_CLASSES["flash_decoding"] → Qwen3FlashDecodingAttention
        │
        ▼  若 key 是 eager / flash_attention_2
Qwen3Attention.forward：
    if config._attn_implementation != "eager":
        attention_interface = ALL_ATTENTION_FUNCTIONS[config.attn_backend]
        #  ↑ 用 attn_backend（而非 _attn_implementation）查全局表
```

`example.py` 恒把 `_attn_implementation` 硬编码为 `"flash_attention_2"`（见 4.4），因此 `eager` 后端实际走不到 `eager_attention_forward`——除非同时改 `_attn_implementation`。两个字段各司其职：`_attn_implementation` 控制 mask 构建（flash 路径不建 4D mask），`attn_backend` 控制类与函数选择。

#### 4.3.3 源码精读

**本地注册表与查表**：

[evaluation/qwen3.py:L489-L494](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L489-L494) 定义四键注册表。[evaluation/qwen3.py:L496-L500](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L496-L500) `Qwen3DecoderLayer.__init__` 用 `QWEN_ATTENTION_CLASSES[config.attn_backend]` 实例化 `self.self_attn`——每层、每个 batch 的后端在模型构造时就固定了。llama.py 侧同构：[evaluation/llama.py:L761-L766](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L761-L766) 与 [evaluation/llama.py:L774](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L774)。

**全局注册表的「借字段」查表**：

[evaluation/qwen3.py:L235-L243](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L235-L243) 是 `Qwen3Attention.forward` 的分发逻辑：默认 `eager_attention_forward`；当 `_attn_implementation != "eager"` 时改用 `ALL_ATTENTION_FUNCTIONS[self.config.attn_backend]`。注意查全局表用的是 `attn_backend` 而非 `_attn_implementation`——因为本地表把 `eager` 与 `flash_attention_2` 两个键都映射到本类，类内部无法从 `_attn_implementation` 区分用户意图（它被 example.py 恒置为 flash_attention_2），只能再借 `attn_backend` 这个字段。也正因如此，`bit_decoding` 与 `flash_decoding` 键**不能**映射到本类——`ALL_ATTENTION_FUNCTIONS` 里没有这两个名字，查表会 KeyError，所以它们必须由专用子类接管。

**flash_decoding 基线类**：

[evaluation/qwen3.py:L301-L312](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L301-L312) 展示 `Qwen3FlashDecodingAttention` 的 decode 快路径：q_len==1 时转回 flash 布局直接调 `flash_attn_with_kvcache`（FP16 cache 的官方 decode kernel）。它存在的意义是提供**同管线公平基线**——与 bit_decoding 唯一的差别是注意力 kernel 与缓存格式，其余（模型文件、生成流程）完全一致，性能对比（README 的 3-9x）即以此为参照系之一。

#### 4.3.4 代码实践

**实践：用纯 Python 复现双层注册表分发（无需 GPU，可直接运行）**

1. 实践目标：以可运行的最小模型理解「本地表选类 + 全局表选函数」的协作，并亲眼看到 KeyError 边界。
2. 操作步骤：新建临时脚本（示例代码，不属于本仓库）：
   ```python
   ALL_ATTENTION_FUNCTIONS = {"eager": "fn_eager", "flash_attention_2": "fn_fa2", "sdpa": "fn_sdpa"}
   LOCAL = {"eager": "GenericAttn", "flash_attention_2": "GenericAttn",
            "flash_decoding": "FlashDecodingAttn", "bit_decoding": "BitDecodingAttn"}

   for backend in ["eager", "flash_attention_2", "flash_decoding", "bit_decoding"]:
       cls = LOCAL[backend]
       if cls == "GenericAttn":                      # 通用类需二次查全局表
           try:
               fn = ALL_ATTENTION_FUNCTIONS[backend]  # ← 用 attn_backend 查
               print(f"{backend:18s} -> {cls} -> {fn}")
           except KeyError as e:
               print(f"{backend:18s} -> KeyError {e}")
       else:
           print(f"{backend:18s} -> {cls} -> (子类自管)")
   ```
3. 需要观察的现象：前两个后端两跳完成分发；后两个直接落子类。若把 `"bit_decoding"` 也映射到 `GenericAttn` 再运行，会得到 `KeyError: 'bit_decoding'`——印证 4.3.3 的分析。
4. 预期结果：输出四行分发路径；改映射后复现 KeyError。此实验不依赖 torch，可在任何 Python 3 环境验证。

#### 4.3.5 小练习与答案

**练习 1**：如果用户把 `attn_backend` 拼错成 `"bit_decoding "`（多个空格），会在哪一步、以什么形式失败？

**答案**：在模型构造阶段、`Qwen3DecoderLayer.__init__` 的查表处（[evaluation/qwen3.py:L500](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L500)）抛 `KeyError: 'bit_decoding '`。这与 CUDA dispatch 的静默落空（u3-l1）不同——注册表是 dict 查找，失败即显式异常，发生在加载权重之前，容易定位。

**练习 2**：为什么 `flash_decoding` 基线类不直接用 transformers 自带的 flash_attention_2 实现，而要单独写一个 `Qwen3FlashDecodingAttention`？

**答案**：transformers 的 `flash_attention_2` 路径面向通用场景（含 prefill 与变长 batch），decode 时未必走 `flash_attn_with_kvcache` 的最优路径；作者单独写一个 q_len==1 快路径类，保证基线在 decode 阶段也用上官方 FP16 kv-cache kernel，使「bit_decoding vs flash_decoding」的对比只差「低比特打包缓存 + 自研 kernel」这一个变量。这是基准实验设计中控制变量的典型手法。

### 4.4 config 注入与后端切换：从命令行到 attention 层

#### 4.4.1 概念说明

「config 注入」指入口脚本在加载模型前，把量化与后端配置作为属性写到 config 对象上，随 `from_pretrained(config=...)` 传入模型，最终在 attention `__init__` 里被读取（4.1 的第②段）。`example.py` 与 `bench_throughput.py` 用同一套注入模式，前者面向正确性/生成质量（Qwen3 与 Llama 都支持），后者面向吞吐测量（当前只支持 Llama）。

#### 4.4.2 核心流程

一条配置的完整传导链（以 `--num_bits 2` 为例）：

```text
命令行 --num_bits 2
  → example.py: args.num_bits = 2
  → group_size 缺省规则：num_bits==2 → 32，否则 128
  → config.num_bits = 2；config.group_size = 32；config.residual_block_size = 256
  → LlamaForCausalLM.from_pretrained(..., config=config)
  → LlamaDecoderLayer.__init__ 查表 → LlamaBitDecoding(config)
  → LlamaAttention.__init__: self.num_bits=2, self.pack_nums=8,
    self.group_size=32, self.residual_block_size=256
  → forward 中决定打包张量形状与 fwd_kvcache_int 的 int2 绑定
```

注意 `residual_block_size` 不接受命令行参数，由 `num_bits` 推导（4-bit→128、2-bit→256），与 kernel 侧编译期常量 kBlockN_pack 的约定一致（u2-l2）。

#### 4.4.3 源码精读

**猴子补丁与模型导入**：

[evaluation/example.py:L8-L16](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L8-L16) 先用改造版三个 Cache 类替换 `transformers.cache_utils` 的同名类（u6-l1 讲过其原理：HF generate 流程经模块属性解析缓存类），再 `from llama import LlamaForCausalLM`、`from qwen3 import Qwen3ForCausalLM`。注意这是顶层同目录导入——脚本预期在 `evaluation/` 目录下运行（`bash scripts/example.sh` 时 Python 把脚本目录加入 `sys.path` 的行为使 `from llama import ...` 成立）。

**命令行参数**：

[evaluation/example.py:L22-L28](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L22-L28) 定义五个关键参数：`--model_path`、`--max_length`、`--num_bits`（默认 4）、`--quant_mode`（默认 k-channel）、`--group_size`（默认 None）、`--attn_backend`（默认 flash_attention_2）。[evaluation/example.py:L34-L35](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L34-L35) 补上 group_size 缺省规则：2-bit 用 32、4-bit 用 128——正是 dispatch（u3-l1）启用的两个组合。

**config 组装**：

[evaluation/example.py:L37-L47](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L37-L47) 是注入的核心七行：按 `model_path` 字符串含 `"Llama"` 或 `"Qwen"` 选择 `LlamaConfig`/`Qwen3Config`；`_attn_implementation` 硬编码 `"flash_attention_2"`（跳过 4D mask 构建）；随后写入 `attn_backend/num_bits/quant_mode/group_size`，并按位宽推导 `residual_block_size`。[evaluation/example.py:L49-L64](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L49-L64) 同样按名字选择模型类并加载。这个「按路径名 if/elif」就是接入新模型时要扩的第三处。

**基准脚本的注入与差异**：

[evaluation/bench_throughput.py:L17-L35](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L17-L35) 的 `load_model` 写入同一组字段（L23-L27），但只 import 了 Llama（[evaluation/bench_throughput.py:L7-L8](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L7-L8)），且**没有**设置 `_attn_implementation`、也没有做 cache 猴子补丁（llama.py 内部直接 import 改造版缓存，模型自身 forward 不依赖补丁；补丁主要服务 generate 流程，而基准脚本直接调 `model(inputs_embeds=...)` 不走 generate，见 [evaluation/bench_throughput.py:L75-L105](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L75-L105) 的 prefill/decode 计时循环）。后端切换方式与 example.py 相同：`--attn_backend bit_decoding`（默认值见 [evaluation/bench_throughput.py:L47-L50](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L47-L50)）。

**运行脚手架**：

[evaluation/scripts/example.sh:L1-L7](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/scripts/example.sh#L1-L7) 给出 Qwen3-8B 的完整命令：4-bit、k-channel、group_size 128、`attn_backend bit_decoding`，注释里列出三个可选后端与 Llama-3.1-8B 备选模型——这就是「切换后端做对比」的官方入口。

#### 4.4.4 代码实践

**实践：同配置三后端对比运行（需 GPU 与模型权重；无 GPU 时做传导追踪）**

1. 实践目标：亲手完成一次后端切换，观察输出质量与显存的定性差异；无 GPU 时完成一条配置的纸面传导。
2. 操作步骤：
   - 在 `evaluation/` 下依次运行（其余参数同 example.sh）：
     ```bash
     for b in flash_attention_2 flash_decoding bit_decoding; do
       python example.py --model_path Qwen/Qwen3-8B --num_bits 4 \
         --quant_mode k-channel --group_size 128 --attn_backend $b
     done
     ```
   - 记录每次的生成文本与 `torch.cuda` 峰值显存（可在脚本里加 `torch.cuda.max_memory_allocated()/1e9` 打印）。
3. 需要观察的现象：三个后端对同一 GSM8K 长提示生成的答案应大体一致（bit_decoding 因 4-bit 量化有轻微差异）；显存上 bit_decoding 的 KV 部分显著低于两个 FP16 基线（提示越长差距越大）。
4. 预期结果：flash_attention_2 与 flash_decoding 输出应逐字一致（同 kernel 不同调用路径）；bit_decoding 输出语义一致、用词可能漂移。**待本地验证**。
5. 无 GPU 替代任务（传导追踪）：任取 `--num_bits 2 --quant_mode k-channel --attn_backend bit_decoding`，在纸上写出该值途经的每个变量（args → config → self → 张量形状/绑定函数名），共约 8 站，对照 4.4.2 的流程图核对。

#### 4.4.5 小练习与答案

**练习 1**：`example.py` 为什么必须把 `_attn_implementation` 硬编码为 `"flash_attention_2"`，而不是交给用户选？

**答案**：`_attn_implementation` 决定 `_update_causal_mask` 的行为——选 flash_attention_2 时跳过 4D因果 mask 的构建（[evaluation/qwen3.py:L746-L757](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/qwen3.py#L746-L757)）。BitDecoding 的 kernel 接口不接收 4D mask（它以 seqlens_k/new_lens 表达长度），若走 sdpa/eager 路径构建出 4D mask，该 mask 无人消费还浪费计算。所以后端选择的自由度全部交给 `attn_backend`，`_attn_implementation` 固定为对 kernel 最友好的值。

**练习 2**：`bench_throughput.py` 没设置 `config._attn_implementation`，从 `LlamaConfig.from_pretrained` 出来的默认值可能是什么？这会带来什么差异？

**答案**：transformers 会按可用性推导，无显式指定时通常落到 `"sdpa"`（若已安装 flash-attn 且 checkpoint 未声明，也可能提示用 flash_attention_2，取决于版本）。落到 sdpa 时 `LlamaModel._update_causal_mask` 会走 4D mask 构建分支，prefill 阶段多一块 mask 构建开销与显存，且传给 `_flash_attention_forward` 的 attention_mask 非 None。对 decode 计时影响很小（q_len==1 时 bit_decoding 分支不消费 mask），但做严格 prefill 对比时应显式对齐两个脚本的 `_attn_implementation`。这属于实验设计细节，**待本地验证**。

## 5. 综合实践

**任务：产出「为 Qwen2 接入 BitDecoding」的实施步骤文档**

本任务把本讲四个模块串起来：对照阅读 → 差异清单 → 迁移方案。建议按以下步骤完成并写成一份 Markdown 文档（放在你自己的笔记目录，不要写入仓库源码目录）：

1. **diff 式对照（4.1）**：完成 4.1.4 的 diff 实践，整理出一张「模板代码 / 模型特有代码」两列清单。模板列应包含：基类 `__init__` 末尾 5 行配置读取、decode 分支全部代码、`kvcache_pack_int` 调用与 `*_new` 缓冲分配、注册表与查表两段。
2. **差异归因（4.2）**：逐条记录 qwen3.py 相对 llama.py 的适配点：QK-Norm 的位置与布局、RoPE 前后的 transpose 顺序、prefill 注意力接口（`ALL_ATTENTION_FUNCTIONS` vs `_flash_attention_forward`）、`sliding_window` 传参、k-tensor else 分支缺失。对每条标注「Qwen2 是否也需要」——提示：Qwen2 与 Llama 同构（无 QK-Norm），应直接以 llama.py 为模板。
3. **迁移清单（4.3、4.4）**：写出必须触碰的文件与位置：
   - 复制官方 `modeling_qwen2.py` → `evaluation/qwen2.py`，顶部改 `from bit_decode import Cache, DynamicCache, StaticCache`；
   - `Qwen2Attention.__init__` 末尾加 5 行量化字段读取；
   - 新增 `Qwen2BitDecoding(Qwen2Attention)`，decode 分支照搬 [evaluation/llama.py:L648-L683](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L648-L683)，prefill 分支保留 Qwen2 原注意力调用后接 [evaluation/llama.py:L705-L750](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L705-L750) 的打包代码；
   - 建 `QWEN2_ATTENTION_CLASSES` 四键注册表，DecoderLayer 查表处改用 `config.attn_backend`；
   - `example.py` 的 config/模型两个 if/elif 各加一条 Qwen2 分支。
4. **验证方案（4.4）**：设计三级验证——先 `attn_backend=flash_attention_2` 确认复制版模型与官方输出一致；再 `bit_decoding` 短提示人工检查生成连贯性；最后用 `bench_throughput.py`（需同样加 Qwen2 分支）对比吞吐。
5. **风险清单**：至少列出三条，例如 head_dim 是否为 128（kernel 只实例化了 hdim128）、`quant_mode` 仅支持 k-channel、Qwen2 的 attention_bias/RoPE 细节与模板的差异。

预期产出：一份 1-2 页的步骤文档，其中「模板代码」部分能直接指到本讲列出的行号。全程无需 GPU；若有 GPU，可执行第 4 步的前两级验证（**待本地验证**）。

## 6. 本讲小结

- BitDecoding 的模型接入采用**复制改造法**：整文件复制官方 modeling 文件，改动集中在五段——bit_decode 导入、基类配置读取、BitDecoding 子类、后端注册表、DecoderLayer 查表；`qwen3.py` 与 `llama.py` 在这五段上逐段同构。
- decode 分支代码在两文件间达到**逐字级相同**，是可直接复用的模板；模型特有的适配只发生在前奏（Qwen3 的 QK-Norm、RoPE 布局、prefill 注意力接口），且无论路线如何，进入 kernel 的张量恒为 (b, s, h, d) flash 布局。
- 后端切换是**双层注册表**机制：`config.attn_backend` 先查本地类注册表选类；落到通用基类时再借同一字段查 transformers 的 `ALL_ATTENTION_FUNCTIONS` 选函数；`bit_decoding/flash_decoding` 则由专用子类自管。
- config 注入链路：命令行参数 → `config.num_bits/quant_mode/group_size/residual_block_size/attn_backend` → attention `__init__` 的 `self.` 属性 → 张量形状与 int2/int4 绑定选择；`_attn_implementation` 被硬编码为 flash_attention_2 以规避 4D mask。
- 两处值得注意的真实差异：`qwen3.py` 的 prefill 只写了 k-channel 分支（传 k-tensor 会 `UnboundLocalError`）；`bench_throughput.py` 仅支持 Llama 且未设 `_attn_implementation`。

## 7. 下一步学习建议

本讲完成了模型集成层的最后一课，第六单元到此结束。接下来进入第七单元「测试、基准与二次开发」：

- **u7-l1 正确性测试体系**：学习 `evaluation/test.py` 的 Python 端到端校验与 `csrc` 下独立 CUDA 测试（`test_single_packdecode.cu` 等），为本讲综合实践中的「三级验证方案」提供工具。
- **u7-l2 性能基准**：精读 `bench_throughput.py` 的计时口径（prefill/decode 分段、warmup、峰值显存），把本讲 4.4.4 的定性对比升级为定量实验。
- **u7-l3 新增量化配置**：如果你的兴趣在 kernel 侧而非模型侧，该讲演示打通 group_size=64 的完整链条（dispatch → genfile 实例化 → setup.py），与本讲的「接入新模型」互为镜像：一个扩展模型维度，一个扩展配置维度。

建议同时动手完成本讲第 5 节的 Qwen2 迁移文档——它是检验你是否真正掌握「三块改动」的最好试金石。
