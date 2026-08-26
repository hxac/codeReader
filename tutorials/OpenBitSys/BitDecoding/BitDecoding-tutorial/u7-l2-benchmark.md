# 性能基准：吞吐、延迟测量与消融基线

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐行读懂 `evaluation/bench_throughput.py`，说清 prefill 与 decode 两段各自的计时区间、同步点位置，以及吞吐公式如何从延迟推导出来。
2. 解释计量卫生三件套——warmup、`torch.cuda.empty_cache()`、`torch.cuda.reset_peak_memory_stats()`——分别在消除什么干扰，以及本脚本的显存统计口径到底覆盖了哪些阶段。
3. 独立阅读 kernel 级微基准 `bench_single_residual.cu` / `bench_single_packdecode.cu`：CUDA event 计时、长度倍增扫描、min/avg/max 三值统计，并能动手把它们从 CMake 的注释里恢复出来编译运行。
4. 说清 `evaluation/ablation/` 下两个「外部基线」脚本真正测量的对象是什么、有什么局限，从而能自己设计一份公平的低比特注意力对比实验。

本讲承接 u6-l2（LlamaBitDecoding 前向双路径）与 u7-l1（正确性测试体系）：正确性管「算得对不对」，本讲管「跑得快不快、省不省显存」，两者合起来才是完整的 kernel 评测。

## 2. 前置知识

### 2.1 延迟、吞吐与「分段计时」

- **延迟（latency）**：完成一次操作所耗时间。本脚本区分两个粒度——prefill 一次前向的总延迟（秒），decode 单个 token 的平均延迟（秒/token）。
- **吞吐（throughput）**：单位时间处理的 token 数。若批量大小为 \(B\)、上下文长度为 \(S_{\text{ctx}}\)、prefill 延迟为 \(t_{\text{prefill}}\)，则：

\[ T_{\text{prefill}} = \frac{B \cdot S_{\text{ctx}}}{t_{\text{prefill}}}, \qquad T_{\text{decode}} = \frac{B \cdot S_{\text{dec}}}{t_{\text{decode}}} \]

- **为什么必须分段**：prefill 是 compute-bound（一次算几千 token 的注意力），decode 是 memory-bound（每步只算 1 个 token、却要读完整个 KV cache，见 u1-l1 的算术强度分析）。低比特 KV cache 只加速后者，把两段混在一起测，会完全掩盖收益。

### 2.2 CUDA 异步执行与计时的坑

PyTorch 在 CPU 上发起的 GPU 操作是**异步**的：`model(...)` 返回时 GPU 可能还没算完。所以：

- 用 `time.perf_counter()`（CPU 墙钟）测 GPU 工作时，必须在计时终点前 `torch.cuda.synchronize()` 等待队列排空，否则测到的只是「launch 时间」。
- kernel 级基准更倾向用 `cudaEvent`：事件直接插在 GPU 流上，度量的是两个事件之间的 GPU 时间，天然排除 CPU 侧干扰。

### 2.3 计量卫生三件套

| 手段 | 消除的干扰 |
|---|---|
| warmup（先跑若干次不计分） | 首次调用的一次性开销：kernel 模块加载（JIT/cubin）、caching allocator 首次分配显存、 autotune |
| `torch.cuda.empty_cache()` | 归还上一轮迭代缓存的显存块，避免「第 0 轮分配慢、后面轮次吃免费缓存」的顺序效应 |
| `torch.cuda.reset_peak_memory_stats()` | 把「峰值显存」计数器清零，让本轮测到的 `max_memory_allocated` 只属于本轮，而非进程历史最高 |

### 2.4 你将从本讲读到的三类基准

1. **模型级端到端**：`bench_throughput.py`，加载真实 LLM，测 prefill/decode 延迟、吞吐、峰值显存。
2. **kernel 级微基准**：`bench_single_*.cu`，不加载模型，直接反复调用 `mha_fwd_kvcache`，用 CUDA event 测 kernel 组的毫秒级延迟。
3. **消融（ablation）基线**：`ablation/test_bitblas.py`、`ablation/test_marlin.py`，拿外部位比特 GEMM 方案（BitBLAS、Marlin）的「打包开销」作参照，反衬 BitDecoding 把量化融合进 decode kernel 的设计价值。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [evaluation/bench_throughput.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py) | 模型级基准主脚本：参数解析、config 注入、分段计时、吞吐与显存统计 |
| [evaluation/scripts/bench_throughput.sh](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/scripts/bench_throughput.sh) | 批量扫描脚本：在 batch×上下文长度网格上反复调 bench_throughput.py |
| [csrc/bit_decode/src/bench_single_residual.cu](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_residual.cu) | kernel 级微基准：带 FP16 残余区的完整 decode 路径（与当前 API 同步） |
| [csrc/bit_decode/src/bench_single_packdecode.cu](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_packdecode.cu) | kernel 级微基准：纯打包 KV 的 decode 路径（**调用旧版 9 参数签名，当前无法编译**） |
| [csrc/bit_decode/CMakeLists.txt](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/CMakeLists.txt) | 独立 CMake 构建通道；两个 bench target 均被注释 |
| [evaluation/ablation/test_bitblas.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/test_bitblas.py) | BitBLAS int4 权重打包耗时基线 |
| [evaluation/ablation/test_marlin.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/test_marlin.py) | Marlin 风格 4-bit 层的 pack 耗时基线（mul 为占位实现） |
| [evaluation/llama.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py) | 被基准脚本 import 的改造版 Llama；含注意力后端注册表 |
| [csrc/bit_decode/src/flash_api.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_api.h) | `mha_fwd_kvcache` 当前完整签名（判断 bench .cu 是否过期的依据） |

## 4. 核心概念与源码讲解

### 4.1 模块一：benchmark_throughput 主流程——prefill/decode 分段计时与吞吐计算

#### 4.1.1 概念说明

模型级基准要回答的问题是：「换上 bit_decoding 后端，一次 prefill、一步 decode 各花多久？吞圸多少？峰值显存省多少？」它不区分时间花在注意力还是 MLP，因此测的是**端到端收益**——这正好与 README 声称的 kernel 级 3-9x 加速（u1-l1）形成互补：端到端加速比必然小于 kernel 级加速比，因为 decode 每步还要读全部模型权重（这部分不随注意力后端变化）。

脚本的骨架是「每一轮迭代 = 清场 → 计时 prefill → 打印显存 → 预热 decode → 计时整段 decode」，最后对所有轮次取均值并换算吞吐。

#### 4.1.2 核心流程

```text
for iter_idx in range(iteration):          # 默认 10 轮
    torch.cuda.empty_cache()               # 清掉上一轮缓存显存
    torch.cuda.reset_peak_memory_stats()   # 峰值显存计数器清零

    ts = perf_counter()
    hidden = randn(b, context_len, hidden) # 注意：randn 在计时区间内
    out = model(inputs_embeds=hidden, use_cache=True)
    torch.cuda.synchronize()               # 等 GPU 排空
    prefill_latency.append(perf_counter() - ts)

    if iter_idx == 0: 打印当前显存/峰值显存   # 口径：本轮 reset 之后到 prefill 结束

    for _ in range(5):                     # decode 预热，不计分（但会推进 KV cache 5 步）
        model(inputs_embeds=randn(b,1,hidden), past_key_values=...)

    ts = perf_counter()
    for _ in range(decode_len):            # 默认 256 步
        out = model(inputs_embeds=randn(b,1,hidden), past_key_values=out.past_key_values, ...)
    torch.cuda.synchronize()               # 只在整段结束时同步一次
    decode_latency.append(perf_counter() - ts)

avg = mean(各轮延迟)
吞吐 = B × token数 / avg延迟
```

注意两个容易忽略的口径细节（在 4.1.4 实践中你会亲自观察它们）：

- **`randn` 计入计时区间**：prefill 的 `ts` 在张量生成之前打下（L73-74），decode 的 `randn` 也在循环内（L100），每步多出一个 GPU 随机数 kernel 与一次 CPU launch。
- **decode 用「CPU 墙钟 + 终点单次同步」**：测的是整段 256 步的墙钟吞吐，而非单步 GPU 延迟；若想测纯 GPU 时间，应改用 CUDA event（见 4.3）。

#### 4.1.3 源码精读

**入口与参数表。** 整个脚本只有一个公开函数 `benchmark_throughput`，用 argparse 接收 10 个参数：

[evaluation/bench_throughput.py:37-52](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L37-L52)

这段代码用 `@torch.inference_mode()` 装饰（关闭 autograd 记账，省显存省时间），默认配置为 `batch_size=1`、`context_len=2048`、`decode_len=256`、`iteration=10`、`attn_backend=flash_attention_2`、`num_bits=4`、`quant_mode=k-channel`、`group_size=128`。`--model_path` 默认 `llama3-8b-instruct`。

**清场与 prefill 计时。**

[evaluation/bench_throughput.py:67-81](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L67-L81)

每轮开头 L69-70 就是 2.3 节讲的 `empty_cache` + `reset_peak_memory_stats`；L73 打下时间戳后，L74 生成随机输入（注意它在计时区间内），L75-78 用 `inputs_embeds` 直接喂隐藏状态（跳过 embedding 查表，但**不跳过** RoPE/MLP/注意力全栈），L79 `synchronize` 后收秒。输入是随机的而非真实文本——对计时没有影响，因为计算量只依赖形状。

**显存统计的时机与口径。**

[evaluation/bench_throughput.py:83-86](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L83-L86)

只在第 0 轮打印，且打印点位于 prefill 之后、decode 预热之前。因此 `max_memory_allocated` 反映的是「权重 + prefill 激活 + prefill logits + 此刻的 KV cache」的峰值，**decode 阶段的峰值（如 split 缓冲 `out_accum`/`softmax_lse_accum`，见 u3-l3）不在统计口径内**。做后端显存对比时要知道自己比的是这个口径。

**decode 预热与整段计时。**

[evaluation/bench_throughput.py:88-108](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L88-L108)

L89-95 的 5 步预热不计分，用于消化首次 decode 的 allocator 扩容与 kernel 加载；一个微妙之处是这 5 步**真实推进了 KV cache 5 个 token**（`past_key_values` 被逐层 update），随后的正式计时从「prefill 长度 + 5」开始。L98-107 对 256 步 decode 总计时，`synchronize` 只出现在整段末尾——这保证了吞吐数字包含 CPU launch 开销，与真实服务场景一致。

**指标汇总与一处历史遗留。**

[evaluation/bench_throughput.py:110-137](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L110-L137)

L111-112 对各轮延迟取 `np.mean`；L116-117 用 2.1 节的公式算两段吞吐；L129 给出单 token decode 延迟 `avg_decode_latency / decode_len`；L135-137 额外输出一行 CSV，方便被外层 shell 脚本收集。特别看 L113 被注释掉的一行：

```python
# avg_decode_latency -= 0.0019366741180 * 32
```

这是一个**硬编码常数修正项**的遗迹（疑似想扣掉 32 层的某个固定开销）。它是测量卫生的反面教材：常数修正不可复现、不可审计。正确做法是把要排除的开销做成显式的对照实验（比如单独测「空 cache 的 model 前向」再相减），而不是在结果上减 magic number。

#### 4.1.4 代码实践

**实践目标**：拿到 `flash_attention_2` 与 `bit_decoding` 两个后端在同一配置下的五项指标，并解释差异来源。

**操作步骤**（需 8B 级模型权重量级、≥A100/4090 显存；以下命令在 `evaluation/` 目录下执行，因为脚本用 `from llama import LlamaForCausalLM` 相对导入）：

```bash
cd evaluation

# 基线：FP16 FlashAttention-2
python bench_throughput.py \
    --model_path meta-llama/Llama-3.1-8B-Instruct \
    --batch_size 1 --context_len 16384 --decode_len 256 --iteration 3 \
    --attn_backend flash_attention_2

# 实验组：4-bit k-channel BitDecoding（只需换一个参数）
python bench_throughput.py \
    --model_path meta-llama/Llama-3.1-8B-Instruct \
    --batch_size 1 --context_len 16384 --decode_len 256 --iteration 3 \
    --attn_backend bit_decoding --num_bits 4 --quant_mode k-channel --group_size 128
```

把两组输出的 CSV 行抄进下表（数值待本地验证，此处留空）：

| 指标 | flash_attention_2 | bit_decoding (4bit/k-channel/g128) |
|---|---|---|
| Avg Prefill Latency (s) | | |
| Avg Decode Latency / token (s) | | |
| Prefill Throughput (tokens/s) | | |
| Decode Throughput (tokens/s) | | |
| Peak GPU Memory (MB) | | |

**需要观察的现象与预期结果**（待本地验证）：

1. **prefill**：bit_decoding 略慢——它的 prefill 先跑一遍与基线相同的 FP16 flash-attn，再额外启动 qpack 打包 kernel（u6-l2 的 prefill 分支）。
2. **decode**：bit_decoding 更快，且上下文越长差距越大。KV cache 每 token 有效位宽从 16 bit 降到 \(4 + 32/128 = 4.25\) bit（约 3.8× 压缩），注意力读 KV 的时间按比例下降。
3. **端到端加速比 < kernel 级 3-9×**：decode 每步仍要读全部 8B 权重（约 16 GB，与后端无关），注意力只是总时间的一部分；16K 上下文下这一占比足够大，收益可观测，2K 下则可能被权重读取淹没。
4. **峰值显存**：bit_decoding 低几百 MB 量级——16K 上下文的 FP16 KV cache 每层 \(2 \times 8 \times 128 \times 16384 \times 2\text{B} = 64\text{MB}\)，32 层共 2 GB，压缩后省下约 1.5 GB（注意口径：这是 prefill 时刻的统计，见 4.1.3）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 L79 的 `torch.cuda.synchronize()` 删掉，prefill 延迟会怎么变？为什么？

**答案**：会骤降到接近纯 CPU launch 时间。因为 GPU 操作异步，`perf_counter()` 在 GPU 还没执行完时就收秒了；剩下的 `decode` 循环虽然最终会因依赖同一输出而间接触发同步，但 prefill 的计时点已经错拍。synchronize 是 CPU 墙钟计时的必要收尾。

**练习 2**：decode 预热的 5 步为什么不能省？省掉会高估还是低估 decode 延迟？

**答案**：不能省。首次 decode 会触发 `out_accum`/`softmax_lse_accum` 等新缓冲的 allocator 扩容（u3-l3：每层每步分配、靠 caching allocator 摊销）、残余区 `*_new` 缓冲首次分配（u5-l4）以及 kernel 模块首次加载。省掉则把这些一次性开销摊进前几步，**高估**平均延迟（尤其 decode_len 较小时）。

**练习 3**：为什么脚本用 `np.mean` 而不是报告 min？kernel 级基准 `bench_single_*.cu` 却同时报 min/avg/max？

**答案**：模型级基准模拟真实服务负载，均值对应「用户平均体感」，且模型前向步骤多、单轮噪声被 256 步 decode 摊薄；kernel 级单次调用只有毫秒级，个别调用会被时钟频率漂移、其他进程抢 SM 等干扰拉高，min 是「理想无干扰」的参考、max 提示最坏情况，三者一起才能判断测量稳定性。

### 4.2 模块二：load_model 的 config 注入与后端选择

#### 4.2.1 概念说明

基准脚本的「实验变量」只有一个：`--attn_backend`。但这个字符串参数如何变成前向路径中的不同 kernel？答案在 u6-l2/u6-l3 讲过的双层机制：命令行参数 → `LlamaConfig` 附加字段 → `LLAMA_ATTENTION_CLASSES` 查表实例化注意力类。本模块从基准脚本视角把这条注入链读完整——它是设计公平对比实验的前提：**必须确认两个后端除注意力外一切条件相同**（同一份权重、同一 dtype、同一输入形状）。

#### 4.2.2 核心流程

```text
args.attn_backend / num_bits / quant_mode / group_size
        │
        ▼ 写入 config 附加字段（含 residual_block_size 推导）
LlamaConfig.attn_backend = ...
        │
        ▼ LlamaForCausalLM.from_pretrained(config=..., torch_dtype=fp16)
模型构建期：每个 DecoderLayer 查 LLAMA_ATTENTION_CLASSES[config.attn_backend]
        │
        ├── "flash_attention_2" → LlamaFlashAttention2（FP16 基线）
        ├── "flash_decoding"    → LlamaFlashDecodingAttention
        └── "bit_decoding"      → LlamaBitDecoding（量化 KV 路径）
```

注意注入发生在**构建期**而非运行期：换后端必须重新加载模型，不能在同一个模型实例上来回切。

#### 4.2.3 源码精读

**config 注入。**

[evaluation/bench_throughput.py:17-35](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L17-L35)

L20 `torch.set_default_dtype(dtype)` 让后续 `from_pretrained` 中的空张量默认 FP16；L22-27 是注入本体——`attn_backend`、`num_bits`、`quant_mode`、`group_size` 四个字段直接挂在 config 上，而 `residual_block_size` 不从命令行收，而是**按 num_bits 推导**：4-bit 取 128、否则 256。这正好镜像 kernel 侧的编译期常量 `kBlockN_pack`（u2-l2：两种位宽下打包 tile 同占 32 个 uint16 行）。L29-34 用改造版 `LlamaForCausalLM`（`from llama import ...`，即 `evaluation/llama.py`，而非 transformers 官方版）加载权重，`device_map="auto"` 支持多卡切分（70B 基准就靠它）。

**后端注册表与查表点。**

[evaluation/llama.py:761-766](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L761-L766) 定义四个后端到类的映射；[evaluation/llama.py:774](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L774) 在每个 `LlamaDecoderLayer.__init__` 里查表实例化。

所以 `--attn_backend` 是唯一的实验开关，且 `eager`/`flash_attention_2`/`flash_decoding`/`bit_decoding` 四条路径共享同一套 QKV 投影、RoPE 与 MLP——这正是「公平对比」的结构保证。

**外层扫描脚本。**

[evaluation/scripts/bench_throughput.sh:4-23](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/scripts/bench_throughput.sh#L4-L23)

这是一个收缩到单点的历史配置：注释里保留的 `BUDGET_POOL`（1K~32K 七档上下文）与 `BATCH_SIZE`（1~32 六档）说明作者做过网格扫描，当前只激活 16384×1 一格，模型换成 70B、`decode_len=100`、`iteration=1`（大模型跑满网格太贵）。L18 固定 `flash_attention_2`，L23 注释提醒三个可换后端。要复现论文式扫描，把注释的两个数组恢复、把 L18 换成循环即可。

#### 4.2.4 代码实践

**实践目标**：亲手验证「注入链在构建期生效、拼写错误立刻爆炸」，这能在以后排查「为什么我设的参数没生效」时省很多时间。

**操作步骤**：

1. 在 `evaluation/` 下运行（`--attn_backend` 故意写错）：

   ```bash
   python bench_throughput.py --model_path <任一已下载的HF模型路径> --attn_backend bit_decodingg
   ```

2. 观察报错出现的位置与内容。

**需要观察的现象**：报错不在 argparse（argparse 只校验它声明的类型，`attn_backend` 是自由字符串），而是在模型构建期抛出，形如 `KeyError: 'bit_decodingg'`，指向 `llama.py:774` 的 `LLAMA_ATTENTION_CLASSES[config.attn_backend]`。

**预期结果**：确认后端选择发生在 `LlamaDecoderLayer.__init__` 查表那一刻——即权重加载过程中、任何前向之前。这也解释了为什么基准脚本必须为每个后端完整加载一次模型。

#### 4.2.5 小练习与答案

**练习 1**：`--attn_backend flash_attention_2` 时传 `--num_bits 2` 会怎样？

**答案**：什么也不会发生。`num_bits` 等量化字段仍会写进 config，但 `LlamaFlashAttention2` 类根本不读它们（只有 `LlamaBitDecoding` 读，见 u6-l2）；前向走 FP16 路径。这是「config 附加字段无人消费即静默失效」的典型例子。

**练习 2**：为什么 `residual_block_size` 不做成命令行参数，而是 `128 if num_bits == 4 else 256` 写死？

**答案**：因为它必须等于 kernel 编译期常量 `kBlockN_pack`（u5-l1：由模板参数派生），传别的值 kernel 也不认——u3-l1 已指出 pybind 层的 `residual_block_size` 是未被消费的哑参数。与其暴露一个假自由度，不如在 Python 侧按 `num_bits` 推导出唯一正确值。

**练习 3**：想在同一张表里加 `flash_decoding` 后端做三方对比，最小改动是什么？

**答案**：再跑一次脚本，把 `--attn_backend` 换成 `flash_decoding` 即可（该后端已在注册表中，u6-l3 讲过它是 FP16 的 split-KV 版本，正好把「split 带来的并行收益」与「低比特带来的带宽收益」两个变量拆开）。

### 4.3 模块三：kernel 级微基准 bench_single_*.cu

#### 4.3.1 概念说明

模型级数字混入了权重读取、MLP、launch 开销；要回答「decode 注意力 kernel 本身多快」，需要**直接在 C++ 里反复调用 `mha_fwd_kvcache` 并用 CUDA event 计时**——这就是 `bench_single_residual.cu`（带 FP16 残余区的完整三 kernel 路径：residual + splitkv + combine）和 `bench_single_packdecode.cu`（无残余的纯打包路径）的用途。README 里 3-9× 的 kernel 级加速曲线（`imgs/4090.png`、`imgs/a100.png`）就是这一层测出来的。

两个文件当前的状态并不对等，这是本模块最有工程价值的部分：

- `bench_single_residual.cu` 与**当前** API 同步（18 个位置参数），恢复 CMake target 即可用；
- `bench_single_packdecode.cu` 调用的是**旧版 9 参数签名**，与 [csrc/bit_decode/src/flash_api.h:313-341](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_api.h#L313-L341) 的现行签名（`k_`/`v_`/`seqlens_k_` 三个 optional、四个 `*_new` 张量、`new_lens` 等）不匹配，**直接取消注释编译必然失败**——它是与 u7-l1 中 `test_single_packdecode.cu` 同期的 API 化石。

#### 4.3.2 核心流程

`bench_single_residual.cu` 的测量流程（`TestDecodingKernelPerformance` 模板函数）：

```text
1. 构造形状：bs=1, seqlen_q=1, 32 头, head_dim=128
2. 残余切分：residual_len = seqlen_kv % 128（整除则保留一整块 128）
             打包区长度 = seqlen_kv - residual_len
3. 用 kvcache_qpack<4> 把随机 K/V 打包成 k_pack/k_params/v_pack/v_params
4. 构造残余区：residual_block_size 大小的 FP16 缓冲，前 residual_len 个填随机值，其余补零
             new_lens = residual_len
5. warmup：调 mha_fwd_kvcache<4> 10 次（不计分）
6. 计时：cudaEventRecord(start) → 调 repeat 次 → cudaEventRecord(end) → 同步
7. 返回 平均每次毫秒 = eventElapsed / repeat
main：对 1024~512K 倍增的每个长度，外层 3 轮 × 内层 3 次，打印 min/avg/max
```

一个精巧的测试设计：main 里取 `seqlen_kv = len_list[j] + 1`（长度为 2 的幂加一）。由于 2 的幂必被 `residual_block_size=128` 整除，`+1` 恰好造出 **residual_len = 1** 的极小残余区——既保证 residual 分支被触发，又让打包区长度保持 2 的幂（block 数为整数、无尾块掩码干扰），测的是「稳态 decode + 最小残余」这一最常见形态。

#### 4.3.3 源码精读

**残余切分与张量构造。**

[csrc/bit_decode/src/bench_single_residual.cu:6-41](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_residual.cu#L6-L41)

L8-13 算残余长度并从 `seqlen_kv` 里扣除，与 `test_single_residual.cu` 一致（整除时保留一整块，刻意区别于 `test.py` 的「余 0 则残余为空」，u7-l1 讲过这一差异）；L26-36 按 `quant_mode` 分支分配打包张量——形状正是 u2-l1 推导的两套布局（k-channel：`(b, s/pack, h, d)`；k-tensor：`(b, s, h, d/pack)`），外加四个 `*_new` 输出缓冲（形状按 `residual_block_size` 一块分配）。模板参数 `<num_heads, num_heads_kv, head_dim, num_bits>` 在 main 中固定为 `<32, 32, 128, 4>`（无 GQA）。

**打包与残余区填充。**

[csrc/bit_decode/src/bench_single_residual.cu:44-85](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_residual.cu#L44-L85)

L44-47 把 K/V reshape 成 Python 侧折叠 batch 后的 `(b*s, h, d)` 并构造 `cu_seqlens_k`（u4-l1 讲过 C++ 侧为何要这么恢复 batch）；L50-58 调 `kvcache_qpack<4>` 完成一次性打包；L66-79 构造 `residual_block_size` 大小的 FP16 残余缓冲：`slice(1, 0, residual_len).copy_(...)` 只填前 `residual_len` 个真实 token，其余保持零——正是 u6-l2 讲的「补零对齐」在 C++ 里的翻版；L82-85 用 `std::make_optional` 包装三个 optional 参数。

**CUDA event 计时核心。**

[csrc/bit_decode/src/bench_single_residual.cu:87-126](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_residual.cu#L87-L126)

L88-99 先跑 10 次预热；L102-120 是标准 event 三段式：`cudaEventCreate` → `cudaEventRecord(start)` → 循环 `repeat` 次完整调用 `mha_fwd_kvcache<4>`（每次调用内部都会走 u3-l1 的全链路：校验、params 组装、`num_splits_heuristic`、三个 kernel 启动）→ `cudaEventRecord(end)` → `cudaEventSynchronize(end)`；L123-126 用 `cudaEventElapsedTime / repeat` 得到平均每次毫秒。事件插在流上，测的是两次 record 之间 GPU 流过的时间——CPU 侧的 `torch::empty` 分配（caching allocator 命中后是纯指针操作）不会显著污染它。

**长度扫描与统计。**

[csrc/bit_decode/src/bench_single_residual.cu:129-164](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_residual.cu#L129-L164)

L139-143 生成 `1024 × 2^i` 共 10 档长度；L145 `outer_repeat=3, inner_repeat=3`——外层每轮重新构造张量与打包（隔离 allocator/编译缓存影响），内层在 event 区间内连发 3 次；L149 `seqlen_kv = len_list[j] + 1` 即上述「+1 技巧」；L156-163 汇总 min/avg/max。小瑕疵：L155 变量名叫 `this_sec`，装的其实是毫秒（L161-163 直接以 ms 打印），阅读时不要被误导。

**化石对照：旧签名调用。**

[csrc/bit_decode/src/bench_single_packdecode.cu:52-81](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_packdecode.cu#L52-L81)

这里对 `mha_fwd_kvcache<4>` 只传 9 个参数：`Q, k_pack, k_params, v_pack, v_params, opt_block_table, sm_scale, quant_mode, group_size`——缺少现行签名必需的 `k_`/`v_`/`seqlens_k_` 与四个 `*_new` 张量。对照 [csrc/bit_decode/src/flash_api.h:313-341](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_api.h#L313-L341)（18 个位置参数 + 默认参数），编译器会报「参数不足 / 无匹配重载」。L49 声明了 `K_new_host` 等变量却从未使用，也是半途改造的痕迹。它的计时骨架（event + repeat）与 residual 版完全相同，读懂一个即读懂两个。

**CMake 通道现状。**

[csrc/bit_decode/CMakeLists.txt:61-85](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/CMakeLists.txt#L61-L85)

两个 bench target（`bench_single_packdecode`、`bench_single_residual`）连同 `test_single_packdecode`、`test_batch_packdecode` 都被注释，只有 [test_single_residual（L35-46）](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/CMakeLists.txt#L35-L46) 是启用状态。每个 target 都要链上 5 个 genfile 实例化单元（u1-l2/u7-l1 讲过原因：模板显式实例化与 dispatch 成对）。另外两处恢复编译前必须处理的坑：[L10](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/CMakeLists.txt#L10) 的 include 路径硬编码成作者机器的 `/home/ddy/...`（应改回 L9 注释的 `${PROJECT_SOURCE_DIR}/../../libs/cutlass/include`），[L7](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/CMakeLists.txt#L7) 固定 `CMAKE_CUDA_ARCHITECTURES 80`。

#### 4.3.4 代码实践

**实践目标**：把 kernel 级基准跑起来，得到「decode 延迟 ~ 上下文长度」曲线的原始数据。

**操作步骤**（需 Ampere 及以上 GPU + 已按 u1-l2 初始化 cutlass 子模块）：

1. 编辑 `csrc/bit_decode/CMakeLists.txt`：把 L10 改为 `set(INCLUDE_DIR ${PROJECT_SOURCE_DIR}/../../libs/cutlass/include)`；把 L74-85 的 `bench_single_residual` target 整块取消注释。
2. 构建并运行：

   ```bash
   cd csrc/bit_decode
   cmake -B build . && cmake --build build --target bench_single_residual -j
   ./build/bench_single_residual
   ```

3. 记录 10 档长度各自的 min/avg/max 三值。

**需要观察的现象与预期结果**（待本地验证）：

1. 延迟随长度近似**线性**增长（decode 是 memory-bound，时间 ∝ 读出字节数），长长度端三个值彼此接近（单次调用毫秒级、干扰被摊薄）。
2. 可以顺手做带宽核算：4-bit k-channel、`h_kv=32`、`d=128` 时每 token KV 打包字节数为 \(2 \times 32 \times 128 \times (4 + 32/128)/8 \approx 4.3\text{KB}\)（K、V 各半，含 params），用 `延迟 × 理论HBM带宽 / 字节总数` 估算有效带宽利用率。
3. 若你同时恢复 `bench_single_packdecode`，编译会在 `bench_single_packdecode.cu:54` 处报无匹配函数——请按 4.3.3 的签名对照补齐缺失的 9 个参数（残余张量可仿照 residual 版 L60-85 构造）后再编译。

#### 4.3.5 小练习与答案

**练习 1**：为什么 warmup 10 次之后还要 `repeat=3` 次连发在同一个 event 区间里，而不是每次调用单独计时再平均？

**答案**：单次毫秒级调用单独计时的话，event record 本身的开销与 GPU 频率抖动占比过高；连发 3 次取平均把固定开销摊薄，且同一区间内 GPU 已升频。代价是测到的是「连发吞吐」而非「冷启动单次延迟」，对本基准（模拟 decode 稳态）恰好是对的选择。

**练习 2**：`main` 中把 `seqlen_kv = len_list[j] + 1` 改成 `len_list[j]`，残余区会变成什么样？测的东西变了吗？

**答案**：`1024 % 128 == 0` 时按 L12 的规则保留**一整块 128** 作残余、打包区缩短 128，`new_lens=128` 恰好等于 `residual_block_size`——这会触发 u5-l4 的「攒满即原位再量化」路径，每步多做一次打包落盘，测的就不再是「最小残余稳态」而是「满块再量化稳态」。两种形态都值得各测一遍。

**练习 3**：kernel 级测得的加速比为什么通常高于模型级？

**答案**：kernel 级的分母只有注意力本身（读 KV + 少量 MMA），低比特直接按比例压缩分母；模型级分母还含每步必读的全部模型权重与 launch 开销，这些不随量化变化，按 \( \frac{1}{1 - \alpha + \alpha/r} \)（α 为注意力时间占比、r 为注意力加速比）稀释收益。

### 4.4 模块四：ablation 基线——test_bitblas.py 与 test_marlin.py 到底测什么

#### 4.4.1 概念说明

「消融基线」回答一个设计层面的问题：如果不用 BitDecoding 的融合方案，而是拿**通用低比特 GEMM 库**（BitBLAS、Marlin 这类 weight-only int4 矩阵乘引擎）来做低比特注意力，代价是什么？BitDecoding 的核心主张（u5-l4）是把「量化打包」 piggyback 进 decode kernel——残余块攒满时在 kernel 内原位量化，零额外启动。通用库则必须有**独立的打包/重排步骤**。两个脚本分别给 BitBLAS 的 `transform_weight` 与 Marlin 风格的 `pack()` 计时，量化这一步的外部开销。

必须先讲清两个脚本的**局限**，避免误读为「注意力 kernel 对比」：

- `test_bitblas.py` 计时的对象是权重打包（`transform_weight`），**不是** int4 GEMM 本身；
- `test_marlin.py` 的核心 `mul` 是**占位实现**（作者注释写明 "Placeholder implementation"，内部就是 `torch.matmul` 加随机权重），`_perm` 等重排表也是随机占位（L8-10）——它测的是 `pack()` 重排逻辑的耗时，其结果只能用于「打包成本量级」比较，不能当作 Marlin kernel 性能。

#### 4.4.2 核心流程

**test_bitblas.py**：

```text
1. 用 MatmulConfig 描述一个 decode 形状的 GEMM：M=1（单 token Q），
   N=n_heads×seq_len=128（把 KV 序列折进 N 维），K=dim=128
   A 为 fp16，W 为 int4（无分组、无 scale/zeros）
2. 生成 int8 权重张量 (128, 128)
3. warmup 5 次 transform_weight
4. perf_counter 计时 10 次 transform_weight，取均值（毫秒）
```

**test_marlin.py**：构造 Marlin 风格 `Layer`（4-bit 对称分组线性层，`infeatures=128`、`outfeatures=1024`、`groupsize=128`，含真实的 Marlin 打包逻辑：缩放、平移、tile 重排、4-bit 压包），warmup 5 次后连发 100 次 `pack()`，报告平均毫秒与 packs/sec。运行入口在 [evaluation/ablation/script/test_bitblas.sh](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/script/test_bitblas.sh)（固定 `CUDA_VISIBLE_DEVICES=0`）。

#### 4.4.3 源码精读

**BitBLAS 的 GEMM 形状映射。**

[evaluation/ablation/test_bitblas.py:13-28](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/test_bitblas.py#L13-L28)

`M=1, N=n_heads*seq_len, K=dim` 正是 decode 注意力第一次 GEMM（\(Q \cdot K^\top\)）的形状：单 token 的 Q 乘以「序列维摊平进 N」的 KV。`W_dtype="int4"`、`layout="nt"` 说明意图是把 KV 当 int4「权重」做 W4A16 乘法——这是 BitDecoding 的替身：**同样的数学，不同的执行策略**（独立 GEMM 库 vs 融合注意力 kernel）。

**计时对象是打包而非乘法。**

[evaluation/ablation/test_bitblas.py:38-66](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/test_bitblas.py#L38-L66)

warmup 与计时循环里反复调用的都是 `matmul.transform_weight(weight_tensor)`（L39、L52）——把 int8 权重转成库内部 int4 布局的前置步骤，从没调用过 `matmul(...)` 本体。结论要如实表述：此脚本度量「BitBLAS 路线每引入/更新一段 KV 就要付出的打包税」，与 BitDecoding 的对比点是打包成本的去向（独立 kernel vs 融合进 residual kernel）。

**Marlin 层的骨架与占位 mul。**

[evaluation/ablation/test_marlin.py:12-52](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/test_marlin.py#L12-L52)

L12-21 的 `mul` 明写是占位（内部 `torch.matmul` + 随机矩阵）；L23 起的 `Layer` 结构则忠实复刻了 Marlin 的约束（`infeatures % 128 == 0`、`outfeatures % 256 == 0`、groupsize 只支持 -1/128）与 buffer 布局（`B` 为 `(k//16, n*16//8)` 的 int32 打包矩阵、`s` 为分组 scale、`workspace` 为 kernel 并行 workspace）。

**真实的 pack 逻辑与计时。**

[evaluation/ablation/test_marlin.py:54-92](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/test_marlin.py#L54-L92) 是仿 Marlin 的重排打包：`round(w/s)` 对称量化 → 加 `(maxq+1)//2` 平移到无符号 → clamp → 16×16 tile 置换 → 逐 4-bit 压进 int32。计时部分在 [evaluation/ablation/test_marlin.py:135-166](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/test_marlin.py#L135-L166)：warmup 5 次、`time.time()` 计 100 次 `pack()` 取平均（注意这里用的是 `time.time` 而非 `perf_counter`，精度略差，且 pack 大部分是 torch CPU/numpy 操作，`torch.cuda.synchronize` 仅在首尾各一次）。

#### 4.4.4 代码实践

**实践目标**：源码阅读型实践——给两个 ablation 脚本写「测量对象说明书」，训练「读基准代码先问它到底在计什么」的习惯。

**操作步骤**：

1. 通读两个脚本，为每个脚本填出下表（答案已给，请先自己填再对照）：

| 条目 | test_bitblas.py | test_marlin.py |
|---|---|---|
| 计时对象 | `transform_weight`（int8→int4 布局转换） | `Layer.pack`（量化+tile 重排+压包） |
| 是否测低比特 GEMM 本体 | 否 | 否（mul 为占位） |
| warmup / 计时次数 | 5 / 10 | 5 / 100 |
| 计时器 | `time.perf_counter` + `cuda.synchronize` | `time.time` + 首尾 synchronize |
| 与 BitDecoding 的对比点 | 独立打包步骤的「税」 vs 融合进 residual kernel | 同左 |

2. 思考并书面回答：如果要用这两个脚本支撑「BitDecoding 的融合设计优于独立 GEMM 库路线」的结论，还缺哪块实验？（提示：需要补 `matmul(...)` 本体的延迟，加出「打包 + 乘法」总成本，再与 `mha_fwd_kvcache` 单次延迟对齐形状比较。）

**需要观察的现象与预期结果**：此实践不依赖 GPU 也能完成（纯阅读 + 写说明）。预期你得到的结论是：两个脚本提供的是**打包成本侧写**，不是端到端替身；任何引用它们作「对比基线」的论述都应注明口径。

#### 4.4.5 小练习与答案

**练习 1**：`test_bitblas.py` 里 `group_size=None, with_scaling=False, with_zeros=False`，这与 BitDecoding 的量化配置等价吗？

**答案**：不等价。BitDecoding 用分组仿射量化（每组一对 scale/zero，u4-l3）；这里是无分组、无 scale/zero 的裸 int4——只是形状替身，数值语义更宽松。做严格对比时至少要打开 `with_scaling`/`with_zeros` 并设 `group_size=128`。

**练习 2**：`test_marlin.py` 的 `pack()` 计时为什么大部分落在 CPU 而不是 GPU？

**答案**：pack 的实现以 `torch.round/permute/reshape` 与 numpy 逐位压包（L86-89 的 Python 循环）为主，只有少量 `.to(device)` 搬运；所以 `torch.cuda.synchronize` 首尾各一次即可，GPU 并非瓶颈。这也说明它度量的是「宿主侧重排成本」。

**练习 3**：把这三个层次的基准（模型级 / kernel 级 / ablation）各对应一个必答问题，应该怎么分配？

**答案**：模型级（4.1）答「用户能感到多少加速、省多少显存」；kernel 级（4.3）答「加速来自注意力本身还是环境噪声，带宽利用率多高」；ablation（4.4）答「设计取舍是否成立——融合量化相对独立打包路线省了多少」。三层结论互相印证才构成完整证据链。

## 5. 综合实践

设计并（在有 GPU 的机器上）执行一份**完整的 BitDecoding 性能评审实验**，把本讲三个层次串起来：

1. **实验矩阵**：后端 ∈ {flash_attention_2, bit_decoding(4bit/k-channel/g128)} × 上下文 ∈ {2K, 8K, 16K, 32K}，`batch_size=1`、`decode_len=256`、`iteration≥3`（每格取均值，报告 std；GPU 上建议锁频 `nvidia-smi -lgc` 后测）。
2. **模型级**：用 `bench_throughput.py` 逐格运行，收集 CSV 行，绘制两条曲线：(a) 单 token decode 延迟 vs 上下文长度；(b) 加速比 vs 上下文长度。验证预期：加速比随上下文增长（注意力占比上升），并拟合 \( \text{加速比} \approx \frac{1}{1-\alpha + \alpha/r} \) 估计本机权重读取占比 α 与注意力加速 r。
3. **kernel 级校准**：恢复 `bench_single_residual` target（4.3.4 步骤）跑同长度扫描，把 kernel 延迟与模型级 decode 延迟相减，估算「每步非注意力开销」，检验它是否近似常数（若非常数，说明存在随上下文增长的隐藏开销，例如 split 缓冲）。
4. **口径声明**：在报告开头写明显存口径（prefill 时刻峰值，见 4.1.3）、decode 计时含 `randn` 与 launch 开销、迭代次数与统计方法。
5. **无 GPU 替代**：写出上述实验设计文档（变量、重复次数、统计口径、预期曲线形状与理由），并注明每项「待本地验证」——实验设计本身就是本实践的合格交付物。

## 6. 本讲小结

- `bench_throughput.py` 用「CPU 墙钟 + 终点 synchronize」分段测 prefill 与整段 decode，吞吐由 \( B \cdot S / t \) 换算；输入 `randn` 在计时区间内、显存峰值只覆盖 prefill 阶段，引用数字时必须声明口径。
- 计量卫生三件套各有分工：warmup 消化首次调用的一次性开销，`empty_cache` 消除跨轮显存缓存偏置，`reset_peak_memory_stats` 把峰值统计限定到本轮；脚本里被注释的硬编码修正项是反面教材。
- 后端切换的唯一开关是构建期的 config 注入 + `LLAMA_ATTENTION_CLASSES` 查表，`residual_block_size` 由 `num_bits` 推导以保持与 kernel 编译期常量一致；拼写错误会在模型构建期以 KeyError 暴露。
- kernel 级微基准用 CUDA event 三段式计时（warmup 10 + 连发 3 取平均），对 2 的幂「+1」构造 residual_len=1 的稳态；`bench_single_packdecode.cu` 因旧版 9 参数签名无法编译，且两个 bench target 在 CMake 中均被注释、include 路径硬编码——恢复时三处都要处理。
- `ablation/` 两个脚本测的是外部低比特 GEMM 库（BitBLAS/Marlin）的**打包成本**而非乘法本体，`test_marlin.py` 的 `mul` 更是占位实现；它们支撑的是「融合量化 vs 独立打包」的设计论证，不能当作端到端性能基线。
- 三层基准各答一问：模型级看用户体感，kernel 级看加速来源与带宽利用率，ablation 看设计取舍；端到端加速比恒小于 kernel 级，差值由权重读取等不变成本解释。

## 7. 下一步学习建议

- 下一讲 u7-l3（扩展实践：新增 group_size/num_bits 配置的完整链路）会把本讲的基准方法论用起来：每打通一个新模板配置，都要用 u7-l1 的正确性测试 + 本讲的 kernel 级基准验证「算得对、跑得快」。
- 建议继续精读 [evaluation/example.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py) 与 [evaluation/bench_throughput.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py) 的差异（前者关心生成质量、后者关心速度），体会「评测目标决定脚本结构」。
- 若想深挖计时方法学，可对照 PyTorch 官方 `torch.cuda.Event` / `torch.profiler` 的用法，思考把 `bench_throughput.py` 的 CPU 墙钟替换为 CUDA event 需要改哪些同步点；再阅读 [csrc/bit_decode/src/bench_single_residual.cu](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_residual.cu) 的 event 用法作为参照。
- 最后一讲 u7-l4 会站在架构层面复盘，届时把本讲测得的「端到端 vs kernel 级」差距、ablation 局限一并带入，作为评审报告的证据基础。
