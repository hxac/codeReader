# 性能基准：吞吐、延迟测量与消融基线

## 1. 本讲目标

前几讲我们已经读懂了 BitDecoding 从 Python 接口到 CUDA kernel 的完整实现，u7-l1 也搭好了正确性验证。本讲回答另一个问题：**怎么证明它快、快在哪里、快多少**。读完后你应该能够：

1. 逐行读懂 `bench_throughput.py`：它如何把一次生成切分成 prefill 与 decode 两段分别计时，如何换算吞吐，峰值显存在哪里采样。
2. 说出 `warmup`、`torch.cuda.empty_cache()`、`torch.cuda.reset_peak_memory_stats()`、`torch.cuda.synchronize()` 各自解决什么计量问题，以及这份脚本里隐藏的几个「统计口径」陷阱。
3. 读懂 kernel 级微基准 `bench_single_residual.cu` / `bench_single_packdecode.cu` 的 cudaEvent 计时骨架、min/avg/max 三值统计法，并知道它们当前的编译状态（一个可修、一个是 API 化石）。
4. 理解 `evaluation/ablation/` 下 bitblas 与 marlin 两个脚本各自在「消融」什么，以及为什么它们只能作为打包开销基线、而不是完整注意力基线。
5. 能独立设计一份公平的对比实验：控制哪些变量、重复多少次、报哪些统计量、如何预测并解释结果方向。

## 2. 前置知识

本讲不再涉及新的 kernel 细节，但用到以下测量学概念（初学者不熟悉的术语都在这里解释）：

- **prefill 与 decode**：LLM 生成分两阶段。prefill 一次性处理整段提示（`seqlen_q` 很大，并行度高），decode 每步只处理 1 个新 token（`seqlen_q=1`，访存受限）。两者性能特征完全不同，必须分开计时——混在一起的「总时间」无法定位瓶颈。
- **wall-clock 计时 vs CUDA Event 计时**：
  - `time.perf_counter()`（CPU 墙钟）测的是主机侧经过的时间。CUDA kernel 是异步发射的，所以计时结束前必须 `torch.cuda.synchronize()` 等 GPU 做完，否则只测到了「命令提交」的时间。
  - `cudaEventRecord()` 把事件插进 GPU 流里，`cudaEventElapsedTime` 返回的是**两个事件之间 GPU 实际经过的时间**，天然排除主机发射开销，是 kernel 微基准的标准做法。
- **warmup（预热）**：第一次执行某个 kernel 时，CUDA 要加载模块、分配首次显存、torch 的 caching allocator 要扩块。这些一次性成本不应算进稳态延迟，所以正式计时前先空跑几轮。
- **caching allocator 与 `empty_cache()`**：PyTorch 释放显存时只是把块还回自己的缓存池，并不还给驱动。`torch.cuda.empty_cache()` 把缓存池清空归还驱动，让每轮迭代的显存统计从同一起点开始。
- **`reset_peak_memory_stats()`**：PyTorch 持续跟踪「已分配显存」的峰值；不重置的话，峰值只会单调不降，第二轮以后永远读到历史最大值。每轮开头重置，峰值才反映本轮。
- **统计口径**：一个延迟数字的含义 = 计时边界内包含了哪些开销（张量生成？kernel 发射？同步？）。口径不一致的两个数字不可比较——这是本讲反复强调的主线。
- **有效比特数**（承接 u2-l3）：量化缓存每个元素的等效带宽成本为

  \[ \text{bits/elem} = \text{num\_bits} + \frac{32}{\text{group\_size}} \]

  FP16 是 16 bit/elem。4-bit、group_size=128 时为 \(4.25\) bit，压缩比 \(16/4.25 \approx 3.76\times\)；2-bit、group_size=32 时为 \(3\) bit，压缩比 \(\approx 5.33\times\)。这个比值是 decode 阶段注意力 kernel 加速的上界来源。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [evaluation/bench_throughput.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py) | 模型级基准：加载改造版 Llama，分段测 prefill/decode 延迟、吞吐与峰值显存 |
| [evaluation/scripts/bench_throughput.sh](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/scripts/bench_throughput.sh) | 批量扫描脚本：对多组 context_len × batch_size 逐个调用 bench_throughput.py |
| [evaluation/llama.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py) | 被基准脚本 import 的改造版模型，`config.attn_backend` 在此被消费（u6-l2 已精读） |
| [csrc/bit_decode/src/bench_single_residual.cu](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_residual.cu) | kernel 级微基准：含 FP16 残余区的完整 decode 调用，cudaEvent 计时 |
| [csrc/bit_decode/src/bench_single_packdecode.cu](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_packdecode.cu) | kernel 级微基准：纯打包缓存 decode（旧版 API，当前无法编译） |
| [csrc/bit_decode/CMakeLists.txt](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/CMakeLists.txt) | 独立于 pip 的 C++ 构建通道，两个 bench target 均被注释 |
| [csrc/bit_decode/src/flash_api.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_api.h) | `mha_fwd_kvcache` 当前签名，用来判定两个 bench 文件谁是化石 |
| [evaluation/ablation/test_bitblas.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/test_bitblas.py) | 消融基线：bitblas int4 weight-only GEMM 的权重转换开销 |
| [evaluation/ablation/test_marlin.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/test_marlin.py) | 消融基线：Marlin 风格层的 pack 开销（占位实现） |

两层基准的分工：**模型级**（bench_throughput.py）回答「换上 bit_decoding 后端后端到端快多少、省多少显存」；**kernel 级**（bench_single_*.cu）回答「注意力算子本身快多少」——这正是 README 性能图（imgs/4090.png、imgs/a100.png）里 3-9× 加速的口径。两层缺一不可，原因见 4.2 的算术示例。

## 4. 核心概念与源码讲解

### 4.1 load_model 与 config 注入：基准的「控制变量」入口

#### 4.1.1 概念说明

公平对比的第一原则：**除被试变量外一切保持不变**。在 BitDecoding 里，「被试变量」是注意力后端（`flash_attention_2` / `flash_decoding` / `bit_decoding`）及其量化参数。这些参数不是传给某个函数，而是**注入模型 config**，再由 u6-l3 讲过的双层注册表消费：`LlamaDecoderLayer.__init__` 用 `config.attn_backend` 查 `LLAMA_ATTENTION_CLASSES` 决定实例化哪个注意力类。基准脚本的 `load_model` 就是这条注入链的起点。

#### 4.1.2 核心流程

```text
命令行参数 (--attn_backend/--num_bits/--quant_mode/--group_size)
    │
    ▼
load_model(args)
    ├── LlamaConfig.from_pretrained(model_path)     # 读原始模型配置
    ├── config.attn_backend = ...                   # 注入 5 个字段
    ├── config.num_bits / quant_mode / group_size
    ├── config.residual_block_size = 128 或 256      # 由 num_bits 推导，与 kernel 常量一致
    ▼
LlamaForCausalLM.from_pretrained(..., config=config)  # evaluation/llama.py 的改造版
    ▼
每个 DecoderLayer: LLAMA_ATTENTION_CLASSES[config.attn_backend](config, layer_idx)
```

#### 4.1.3 源码精读

[evaluation/bench_throughput.py:17-35](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L17-L35) 是 `load_model` 全文。关键点逐条：

- [evaluation/bench_throughput.py:22-27](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L22-L27)：从预训练目录读出 `LlamaConfig` 后，硬塞进 5 个自定义字段。注意第 27 行 `config.residual_block_size = 128 if args.num_bits == 4 else 256`——它和 kernel_traits 的编译期常量 `kBlockN_pack`（u5-l1）必须一致，这里是 Python 侧的对齐点。即使跑 `flash_attention_2` 基线，这些字段也会被设置，只是没人消费，保证 config 结构完全相同。
- [evaluation/bench_throughput.py:7](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L7)：`from llama import LlamaForCausalLM`——**不是** `transformers` 里的原版，而是 `evaluation/llama.py` 的改造版。这决定了脚本必须在 `evaluation/` 目录下运行（或把它加进 `PYTHONPATH`）。
- [evaluation/bench_throughput.py:29-34](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L29-L34)：`from_pretrained(..., config=config, device_map="auto")`。`device_map="auto"` 在多卡机上会把大模型（如脚本里用的 70B）切分到多张卡，此时 wall-clock 计时覆盖的是「整个流水线」，包括卡间通信——口径上要心里有数。

注入的消费端在 [evaluation/llama.py:761-766](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L761-L766)（注册表定义）与 [evaluation/llama.py:774](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L774)（按 config 查表实例化）。`bit_decoding` 对应 u6-l2 精读的 `LlamaBitDecoding`；`flash_decoding` 是另一个 split-KV 的 FP16 对照组。

#### 4.1.4 代码实践

1. **实践目标**：确认「同一份脚本、不同 `--attn_backend`，真的实例化了不同的注意力类」——这是公平对比的前提。
2. **操作步骤**（不改源码）：在 `evaluation/` 目录下运行
   ```bash
   python3 -c "
   import sys, argparse
   sys.argv = ['x', '--model_path', 'meta-llama/Llama-3.1-8B-Instruct', '--attn_backend', 'bit_decoding']
   from bench_throughput import load_model
   from llama import LlamaBitDecoding
   args = argparse.Namespace(model_path='meta-llama/Llama-3.1-8B-Instruct', dtype='float16',
                             attn_backend='bit_decoding', num_bits=4,
                             quant_mode='k-channel', group_size=128)
   model = load_model(args)
   print(type(model.model.layers[0].self_attn))
   print(isinstance(model.model.layers[0].self_attn, LlamaBitDecoding))
   print(model.config.num_bits, model.config.group_size, model.config.residual_block_size)
   "
   ```
   把 `attn_backend` 换成 `flash_attention_2` 再跑一次。无 GPU 时改用纯阅读法：对照 [evaluation/llama.py:774](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L774) 手动推演查表结果。
3. **需要观察的现象**：第一次输出 `LlamaBitDecoding` 与 `True`；第二次输出 `LlamaFlashAttention2` 与 `False`；`num_bits/group_size/residual_block_size` 两次都是 `4/128/128`。
4. **预期结果**：如上。本实验只验证实例化链路，**待本地验证**（需要能加载 8B 权重的环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么量化参数走 config 注入，而不是像 `fwd_kvcache_int` 那样作为函数参数逐层传递？
**答案**：基准要对比的基线（`flash_attention_2`）根本没有量化参数；走 config 可以让两类后端接收**结构完全相同**的初始化路径，差异被完全收敛到注册表查表那一行（[llama.py:774](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L774)）。若逐层传参，基线路径就要写一堆无意义的占位参数，破坏公平性也容易出错。

**练习 2**：`--attn_backend flash_decoding` 时 `config.num_bits=4` 仍会被设置。它会改变这个基线的行为吗？
**答案**：不会。`num_bits/quant_mode/group_size/residual_block_size` 只在 `LlamaBitDecoding` 内部被读取（见 u6-l2 的 decode 分支）；`LlamaFlashDecodingAttention` 不读这些字段，注入等于无效赋值。这正是「控制变量」的设计意图：config 形状恒定，消费与否由类决定。

### 4.2 benchmark_throughput 主流程：prefill/decode 分段计时与吞吐计算

#### 4.2.1 概念说明

`benchmark_throughput` 是模型级基准的主体。它模拟真实服务负载：给定长度 `context_len` 的随机输入做一次 prefill，然后连续 decode `decode_len` 个 token，重复 `iteration` 轮取平均。核心设计决策有三个：**分段计时**（prefill 与 decode 分开）、**解码整段计时**（不为每个 token 单独同步，避免同步开销污染）、**吞吐换算**（token 数 / 时间）。

#### 4.2.2 核心流程

每一轮迭代（[bench_throughput.py:67-108](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L67-L108)）：

```text
┌─ 清场：empty_cache() + reset_peak_memory_stats()          (L69-70, 不计时)
├─ Prefill 段：
│    ts = perf_counter()
│    randn 生成 (b, context_len, hidden) 输入               ← 注意：在计时区内！
│    model(inputs_embeds=..., use_cache=True)               ← prefill + （bit 后端时）量化打包
│    torch.cuda.synchronize(); te = perf_counter()          (L73-81)
│    第 0 轮额外打印 memory_allocated / max_memory_allocated (L84-86)
├─ Decode 预热：5 次不计时单步 forward                       (L89-95, 会原地增长缓存!)
└─ Decode 段：
     ts = perf_counter()
     循环 decode_len 次：randn 生成单 token 输入 + forward    ← randn 也在计时区内
     torch.cuda.synchronize(); te = perf_counter()           (L98-108)
```

指标换算（[bench_throughput.py:110-117](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L110-L117)）：

\[ \text{prefill\_throughput} = \frac{\text{batch\_size} \times \text{context\_len}}{\overline{T}_{\text{prefill}}}, \qquad \text{decode\_throughput} = \frac{\text{batch\_size} \times \text{decode\_len}}{\overline{T}_{\text{decode}}} \]

单 token 延迟直接取 [bench_throughput.py:129](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L129) 的 `avg_decode_latency / decode_len`。脚本的默认参数在 [bench_throughput.py:40-50](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L40-L50)：batch 1、context 2048、decode 256、iteration 10、fp16、默认基线 `flash_attention_2`（4-bit k-channel group 128 参数对基线无效）。

#### 4.2.3 源码精读

**（a）清场三件套与显存采样。** [bench_throughput.py:69-70](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L69-L70) 每轮开头清空 caching allocator 并重置峰值计数器；[bench_throughput.py:85-86](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L85-L86) 只在第 0 轮、**且只在 prefill 之后**打印当前分配量与峰值。两个口径含义：

- `memory_allocated`：prefill 刚结束、KV cache（FP16 或打包+残余）刚建立时的常驻量——量化省显存的证据主要看这里；
- `max_memory_allocated`：本轮到目前为止的峰值——但注意打印点在 decode 之前，decode 阶段新分配的 `*_new` 缓冲（u5-l4）与中间累积缓冲**不在这张快照里**。想要 decode 峰值，需要把打印挪到 decode 之后（综合实践会做）。

**（b）计时边界。** [bench_throughput.py:73-81](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L73-L81) 与 [bench_throughput.py:98-108](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L98-L108) 的 `perf_counter → ... → synchronize → perf_counter` 结构保证 GPU 工作被计入；但 `torch.randn` 生成输入在两处都位于计时区内（L74、L100）。对 prefill（一次生成 2048×4096 的张量）这点开销可忽略；对 decode（单 token，kernel 本身可能只有几百微秒）host 侧 randn 与 Python 循环开销会**同量级地计入**单 token 延迟——两个后端都吃同样的开销，对比仍公平，但绝对值偏大，解读时要记得。

**（c）decode 预热的副作用。** [bench_throughput.py:89-95](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L89-L95) 的 5 次预热 forward 带着 `past_key_values=out.past_key_values` 且 `use_cache=True`。cache 对象是**原地更新**的（u6-l1 的 `update_residual` 沿 dim=-3 追加），所以正式计时的 decode 段开始时，KV 长度已经是 `context_len + 5`。对 bit_decoding 还有一个细节：预热 5 步期间残余区在增长，若恰好在正式段内攒满 128，`update_pack` 的 `torch.cat` 拼接成本会被计入某一步的延迟——这是真实负载也会付的成本，属于口径内。

**（d）一处历史痕迹。** [bench_throughput.py:113](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L113) 有一行被注释的 `# avg_decode_latency -= 0.0019366741180 * 32`——作者曾手工扣除每步约 1.94ms 的框架开销再乘 32（可能是旧实验的 decode_len）。它提醒我们：**凡是事后手工扣减的数字，都必须在报告里注明**，否则口径不可复现。当前代码已禁用该修正，报告的是原始 wall-clock。

**（e）输出格式。** [bench_throughput.py:120-137](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L120-L137) 先打印人读表格，再打印一行 CSV（L136-137），方便 shell 脚本批量扫描后拼接汇总——[bench_throughput.sh](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/scripts/bench_throughput.sh#L7-L19) 正是靠嵌套循环把它当批量探针用（当前只留 `BUDGET_POOL=('16384')`、`BATCH_SIZE=('1')` 一组，模型为 Llama-3.1-70B，`--iteration 1`；被注释的第一行保留了 1024→32768 的完整扫描计划，底部 [L23](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/scripts/bench_throughput.sh#L23) 注明三个后端名）。注意 `--iteration 1` 意味着脚本当前配置下的数字是**单次冷启动样本**，含首轮 CUDA 模块加载等一次性成本，只宜作粗筛。

#### 4.2.4 代码实践

1. **实践目标**：亲手得到两个后端的对比数据（或产出一份可执行的实验设计）。
2. **操作步骤**（有 GPU， ≥48GB 单卡建议用 8B 模型避免 device_map 切分干扰）：
   ```bash
   cd evaluation
   python3 bench_throughput.py --model_path meta-llama/Llama-3.1-8B-Instruct \
       --batch_size 1 --context_len 16384 --decode_len 256 --iteration 5 \
       --attn_backend flash_attention_2
   python3 bench_throughput.py --model_path meta-llama/Llama-3.1-8B-Instruct \
       --batch_size 1 --context_len 16384 --decode_len 256 --iteration 5 \
       --attn_backend bit_decoding --num_bits 4 --quant_mode k-channel --group_size 128
   ```
   各记录：CSV 行、`Avg Prefill Latency`、`Avg Decode Latency (per token)`、两条 `GPU Memory` 打印。
3. **需要观察的现象**：填入下表（左列为字段名，右列待填）：

   | 指标 | flash_attention_2 | bit_decoding (4bit k-ch g128) |
   | --- | --- | --- |
   | Avg Prefill Latency (s) | 待填 | 待填（预期略高：多付一次 qpack 打包 kernel） |
   | Avg Decode Latency / token (s) | 待填 | 待填（预期更低，见下方算术） |
   | Prefill Throughput (tok/s) | 待填 | 待填 |
   | Decode Throughput (tok/s) | 待填 | 待填 |
   | GPU Memory Allocated (MB) | 待填 | 待填（预期显著更低） |

4. **预期结果（方向性推算，具体数值待本地验证）**：以 Llama-3.1-8B、context=16384 为例做带宽算术。KV 元素数 = \( 32\_{layers} \times 2_{K,V} \times 16384 \times 8_{kv\_heads} \times 128_{dim} \approx 1.07 \times 10^9 \)。FP16 占 \( \approx 2.15\,\text{GB} \)，4-bit 打包（含 params）按 4.25 bit/elem 占 \( \approx 0.57\,\text{GB} \)。在 ~1TB/s 量级的 HBM 上，decode 每步仅 KV 读取就相差约 1.5ms——**注意力 kernel 自身**的加速潜力约 \( 16/4.25 \approx 3.8\times \)。但端到端单 token 延迟里还有每步必读的 16GB 权重（约 10ms+）与 MLP 计算，所以**模型级 decode 吞吐的提升会明显小于 kernel 级的 3-9×**；context 越长、模型权重占比越小（或多卡切分权重），端到端收益越接近 kernel 级比值。这正是仓库同时维护两级基准的原因。若实测与该方向不符，优先检查是否踩了 4.2.3 的口径陷阱。

#### 4.2.5 小练习与答案

**练习 1**：为什么 prefill 段没有任何 warmup？这会带来什么偏差？如何缓解？
**答案**：prefill 建立新 cache、首次触发 qpack kernel 与 CUDA 模块加载，若先预热就需要丢弃一个 cache 再重建，脚本作者选择了直接测。偏差是 `iteration` 较小时（尤其 `.sh` 里的 `--iteration 1`）prefill 均值被一次性冷启动成本抬高。缓解：用 `--iteration 5` 以上并丢弃第一轮，或自行加一轮不计时的 prefill。

**练习 2**：decode 段为什么每步之间不 `synchronize`、只在最后同步一次？如果想要「每 token 延迟的分布」该怎么办？
**答案**：decode 每步 kernel 只有几百微秒，逐同步会把同步等待（几十微秒级）计入并打断 CPU 发射与 GPU 执行的流水重叠，测出来的是「步进模式」而非连续生成模式，系统性偏慢。要分布的话，改用 CUDA Event 在每步前后 `record`，结束后统一 `elapsed_time`——既得分布又不打断流水（这正是 4.3 kernel 基准的做法）。

**练习 3**：两个后端的 `Avg Decode Latency (per token)` 里都含 host 侧 randn + Python 循环开销。说一个场景，使这个共同开销导致对比结论失真。
**答案**：当 kernel 极快（短 context + batch 1，注意力仅几十微秒）而 host 开销约百微秒时，计时区被 host 开销主导，两个后端的差异被「稀释」到接近噪声——结论会错误地得出「两者差不多」。失真条件是：共同开销 ≥ 被测差异。对策：加大 context（放大差异）、预生成输入把 randn 挪出计时区，或改用 CUDA Event 测纯 GPU 段。

### 4.3 kernel 级微基准：bench_single_*.cu 与 cudaEvent 计时

#### 4.3.1 概念说明

模型级数字混入了权重读取、MLP、Python 框架开销。要单独度量「低比特注意力算子」的性能，就要绕过整个模型，直接在 C++ 里反复调用 `mha_fwd_kvcache`，用 cudaEvent 计时。仓库提供两个微基准：

- `bench_single_residual.cu`：**与当前 API 同步**，测「打包主缓存 + FP16 残余区」的完整 decode 调用（对应真实 decode 路径）；
- `bench_single_packdecode.cu`：**API 化石**，仍按旧版 9 参数签名调用，当前无法编译——它和 u7-l1 发现的 `test_single_packdecode.cu` 是同一批历史遗留。

两个 target 在 CMakeLists 里都被注释，只有 `test_single_residual` 处于启用状态（[CMakeLists.txt:35-46](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/CMakeLists.txt#L35-L46) 启用、[CMakeLists.txt:61-85](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/CMakeLists.txt#L61-L85) 注释两个 bench）。

#### 4.3.2 核心流程

`bench_single_residual.cu` 的测量骨架：

```text
对每个 seqlen_kv ∈ {1024, 2048, ..., 524288}（L141-143 倍增）:
    seqlen_kv += 1                                    (L149，强制产生残余!)
    对 outer_repeat(3) 次:
        TestDecodingKernelPerformance<32,32,128,4>(...)
        ├── 分配 Q/K/V 与 8 个 pack/params 张量（含 *_new）   (L17-41)
        ├── kvcache_qpack<4> 先把 K/V 量化打包（不计入计时）   (L50-58)
        ├── 构造 FP16 残余区：补零到 residual_block_size，
        │   有效 new_lens = (len+1) % 128（或整除时留整块）    (L63-80)
        ├── warmup：空跑 10 次完整 mha_fwd_kvcache            (L87-99)
        └── cudaEvent 计时：连续 repeat(3) 次求平均            (L101-126)
    报告 min / avg / max 三值                              (L162-163)
```

计时核心是标准 cudaEvent 三段式：`cudaEventRecord(start)` → 循环 N 次发射 kernel → `cudaEventRecord(end)` → `cudaEventSynchronize(end)` → `cudaEventElapsedTime`，再除以次数。事件记录在 GPU 流上，测得的是纯 GPU 时间，且 N 次连续发射让kernel 之间无缝衔接（注意：这也意味着测的是**背靠背稳态**，不含真实 decode 里每步一次的发射间隔）。

统计上它用了 **3×3=9 个样本**并报三值：`min` 反映无干扰的理想情况，`max` 暴露时钟漂移/其他扰动，`avg` 是主指标。这比只报均值更能发现测量被污染（max 远大于 avg 时数据不可信）。

#### 4.3.3 源码精读

**（a）残余长度的刻意构造。** [bench_single_residual.cu:10-15](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_residual.cu#L10-L15)：

```cpp
const int residual_block_size = num_bits == 4 ? 128 : 256;
int residual_len = seqlen_kv % residual_block_size == 0 ? residual_block_size : seqlen_kv % residual_block_size;
seqlen_kv = seqlen_kv - residual_len;
```

整除时保留**一整块**作为残余（与 u7-l1 的 `test_single_residual` 同款约定，与 `test.py` 的「余 0 残余为空」刻意不同），保证 `residual` 恒为 true。配合 [L149](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_residual.cu#L149) 的 `seqlen_kv = len_list[j] + 1`，残余路径（FP16 tile + 原位再量化，u5-l4）在**每个**测例中都被执行——因为 real decode 每一步都走这条路，缺了它基准就失真了。

**（b）计时区外完成全部准备。** [bench_single_residual.cu:44-58](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_residual.cu#L44-L58) 把 K/V 折叠成 unpadded 布局并调用 `kvcache_qpack<4>` 完成打包；[L63-80](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_residual.cu#L63-L80) 用 `torch::zeros` 建 `residual_block_size` 大小的补零缓冲、`slice(1, 0, residual_len).copy_()` 填入有效 token（对应 Python 侧 `F.pad` 对齐，u6-l2），`new_lens=residual_len` 告知 kernel 有效长度。**qpack 打包成本不计入计时**——这个基准的口径是「decode 稳态」，与 prefill 一次性打包的真实成本划分一致。

**（c）与当前 API 对齐的调用。** [bench_single_residual.cu:89-99](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_residual.cu#L89-L99)（warmup）与 [L107-117](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_residual.cu#L107-L117)（计时）按位置传入 18 个实参，与 [csrc/bit_decode/src/flash_api.h:315-341](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_api.h#L315-L341) 的现行签名逐位对齐：`Q, k_pack, k_params, v_pack, v_params, k_, v_, seqlens_k_, k_pack_new, k_params_new, v_pack_new, v_params_new, block_table_, sm_scale, quant_mode, group_size, residual_block_size, new_lens`。其中 `opt_seqlens_k` 装的是每 batch 已打包主缓存长度（[L64](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_residual.cu#L64)，即 u3-l2 讲的 `cu_seqlens_k` 复用语义）。计时循环 [L101-126](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_residual.cu#L101-L126) 是 4.3.2 描述的 cudaEvent 三段式，`msec / repeat` 得单次均值。

**（d）固定形状 = 单一变量。** [bench_single_residual.cu:129-136](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_residual.cu#L129-L136) 把 `num_heads=num_heads_kv=32, head_dim=128, k-channel, 4bit, group 128` 写死在 `main` 里，自变量只剩 `seqlen_kv`（[L138-143](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_residual.cu#L138-L143) 从 1024 倍增 10 档到 512K）——这就是 README 里「延迟 vs 上下文长度」曲线的取点方式。注意 `num_heads_kv=32` 是 **MHA 形状**（无 GQA）；Llama-3.1-8B 实际是 32Q/8KV，kernel 级数字不能直接等同于模型级注意力占比。

**（e）化石鉴定。** [bench_single_packdecode.cu:52-60](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_packdecode.cu#L52-L60) 与 [L67-75](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_packdecode.cu#L67-L75) 只传 9 个实参：`Q, k_pack, k_params, v_pack, v_params, opt_block_table, sm_scale, quant_mode, group_size`。对照现行签名，第 6 位参数是 `c10::optional<const at::Tensor> &k_`（新 token K），`opt_block_table` 会错位绑到 K 残余槽位、后续 `float→optional<Tensor>` 类型全不匹配，必然编译失败。它记录的是**加入残余机制之前**的 API 形态——纯打包缓存 decode（所以它的张量分配 [L20-30](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_packdecode.cu#L20-L30) 也没有 `*_new` 四件套、[L106](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_packdecode.cu#L106) 的 `seqlen_kv` 也不加 1）。修复方式与 u7-l1 相同：按 residual 版的 18 参调用改写。另外即使改好源码，还要过构建关：取消 [CMakeLists.txt:74-85](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/CMakeLists.txt#L74-L85) 的注释，并把 [CMakeLists.txt:10](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/CMakeLists.txt#L10) 硬编码的 `INCLUDE_DIR /home/ddy/Projects/BitDecoding/libs/cutlass` 改回本仓库路径（第 9 行留着正确写法的注释）。

#### 4.3.4 代码实践

1. **实践目标**：不运行代码，完成一次「纸面基线设计」——为 kernel 级对比实验算出预期带宽下界，并识别编译障碍。
2. **操作步骤**：
   - 步骤 1：对 `seqlen_kv=16384、num_bits=4、group_size=128、h_kv=32、d=128`，分别计算 FP16 KV 与打包 KV 的字节数（打包侧用 4.25 bit/elem）；
   - 步骤 2：除以你显卡的 HBM 带宽（如 A100-40G 约 1555 GB/s、4090 约 1008 GB/s），得到两种实现的每步注意力时间下界与比值；
   - 步骤 3：阅读 [bench_single_packdecode.cu:53-60](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_packdecode.cu#L53-L60) 与 [flash_api.h:315-341](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_api.h#L315-L341)，列出它会产生的编译错误（参数个数/类型不匹配的具体位置）；
   - 步骤 4（可选，有 GPU 时）：仿照 u7-l1 的做法，取消 [CMakeLists.txt:74-85](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/CMakeLists.txt#L74-L85) 注释、修正 L10 路径后 `cmake --build`，跑 `bench_single_residual` 记录 10 档长度的 min/avg/max。
3. **需要观察的现象**：步骤 2 的比值应稳定在 \( 16/4.25 \approx 3.76 \) 附近（与长度无关，因为 decode 注意力是带宽主导）；步骤 4（若执行）应看到延迟随 `seqlen_kv` 近似线性增长，且同一长度的 max 与 min 差异在 5% 以内（否则测量被扰动污染）。
4. **预期结果**：纸面计算部分可立即完成；实际运行数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么微基准里 `kvcache_qpack` 不计入计时，而模型级基准里 qpack 的成本会体现在 prefill 延迟中？两者矛盾吗？
**答案**：不矛盾，是两种口径各自正确。kernel 级要测的是「decode 稳态注意力算子」，打包是 prefill 期一次性成本（之后每 128 步才由残余 kernel 顺带再做一次，u5-l4），把它混入会污染稳态指标；模型级测的是端到端真实成本，prefill 段天然包含 qpack，bit_decoding 的 prefill 延迟因此略高于基线——这正是 4.2.4 表格里预期 prefill「略高」的原因。

**练习 2**：`bench_single_residual.cu` 若删掉 [L149](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_residual.cu#L149) 的 `+ 1`，测量结果会怎样变化？
**答案**：`len_list` 全是 2 的幂、必被 128 整除，此时 `residual_len` 走「整除留整块」分支恒为 128，残余路径仍会被执行，主缓存长度从 `2^k` 变为 `2^k - 128`。所以删除 `+1` 的实际影响很小（残余仍是整块）；`+1` 的价值在于让 `residual_len=1` 这类**极短残余**的情形也被覆盖，测例更多样。若进一步把 L12-13 改成「整除时残余为空」（test.py 约定），才会真正偏离真实 decode 路径。

**练习 3**：cudaEvent 计时连续发射 3 次再除以 3，为什么不每次发射前后各记一对 event？
**答案**：逐对记录会把每对 event 之间的发射间隔与可能的流水空隙计入单个样本，且 event 本身也有记录开销；连续发射测的是背靠背稳态吞吐，样本更干净。代价是丢失单次延迟的分布信息——但 outer_repeat×inner_repeat=9 个均值样本配合 min/max 报告，已经足以监控测量质量。

### 4.4 ablation 消融基线：bitblas 与 marlin 脚本在对比什么

#### 4.4.1 概念说明

「消融（ablation）」指为论证某个设计选择，去掉或替换该选择后重测性能。BitDecoding 的核心主张是「**在线**把量化 KV 直接喂进 Tensor Core（LOP3 反量化），不需要先解包还原成 FP16」。要支撑这个主张，就要量化「**离线/显式反量化**路线的代价」。`evaluation/ablation/` 下两个脚本分别用两个知名低比特系统作对照：

- `test_bitblas.py`：用 bitblas 的 int4 weight-only GEMM，测其**权重转换**（`transform_weight`）耗时；
- `test_marlin.py`：用（占位复刻的）Marlin 4-bit 层，测其 **pack**（把 FP16 权重打包成 Marlin 位布局）耗时。

注意两者的共同点：它们都在测「**准备量化操作数**」这一步的开销，而不是完整的低比特注意力——这恰好对应 BitDecoding 里 qpack 打包与残余再量化的成本档位。

#### 4.4.2 核心流程

两个脚本共享同一个测量卫生模板：

```text
定义算子/层 → warmup（5 次）→ torch.cuda.synchronize() →
循环 N 次计时（每次前后 sync）→ np.mean 汇总
```

- bitblas 侧：构造 \( M{=}1, N{=}128, K{=}128 \) 的 decode 形状 int4 GEMM 配置 → warmup 5 次 `transform_weight` → 10 次计时取均值；
- marlin 侧：构造 128→1024 的 4-bit 分组层 → warmup 5 次 `pack` → 100 次计时，报平均延迟与 packs/sec。

#### 4.4.3 源码精读

**（a）bitblas：decode 形状的 int4 GEMM 与其打包成本。** [evaluation/ablation/test_bitblas.py:13-28](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/test_bitblas.py#L13-L28) 用 `MatmulConfig` 定义 \( M{=}1 \)（decode 单 token）、\( N{=}n\_heads \times seq\_len{=}128 \)、\( K{=}dim{=}128 \)、`W_dtype="int4"`、无 scale/zero 的对称量化。几何上这正是一个 decode 步骤里 \( Q \cdot K^\top \) 的形状（1×128 乘 128×128）。[L34](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/test_bitblas.py#L34) 生成 int8 权重，[L37-40](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/test_bitblas.py#L37-L40) 预热，[L48-58](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/test_bitblas.py#L48-L58) 每次迭代在前后 `synchronize` 之间只包住 `matmul.transform_weight(weight_tensor)`——**被计时的是 int8→int4 的权重重排，GEMM 前向本身没有测**。[L63-66](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/test_bitblas.py#L63-L66) 取均值。另有一个小 bug 可作练习素材：[L60-61](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/test_bitblas.py#L60-L61) 的进度打印条件是「每 20 次」，而 `num_runs=10`，永远不会触发。

**（b）marlin：一个诚实的占位实现。** [evaluation/ablation/test_marlin.py:8-21](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/test_marlin.py#L8-L21) 开头即声明这是「placeholder」：`_perm/_scale_perm` 是随机排列，`mul()` 内部用的是 `torch.matmul` 加**随机权重**——即真正的 Marlin GPU kernel 并未接入，前向结果无意义。有意义的部分是 [L23-92](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/test_marlin.py#L23-L92) 复刻的 `Layer.pack()`：把 fake-quantized 权重做 round/clamp、按 16×16 tile 重排、逐 4-bit 槽位压进 int32（[L86-89](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/test_marlin.py#L86-L89) 的 `q |= res[:, i::8] << 4*i` 与 u4-l3 的 uint16 打包是同一族位操作）。[L139-158](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/test_marlin.py#L139-L158) 对 `pack` 做 5 次预热 + 100 次计时。注意 `pack` 主体在 **CPU/numpy** 上完成（[L87](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/test_marlin.py#L87) `.cpu().numpy()`），所以这个数字刻画的是「离线位打包的工程成本量级」，且依赖 CPU 性能——跨机器不可比。

**（c）它们在消融论证中的位置。** 两个脚本合起来给出一个对照论点：另一种做法（W4A16 式 weight-only 路线）需要一次显式的操作数变换/打包步骤；而 BitDecoding 把反量化用 LOP3 内联进 Tensor Core 路径（u5-l3），decode 稳态不再有「解包/变换」步骤，代价转嫁为 prefill 一次 qpack + 每 128 步一次 piggyback 再量化（u5-l4）。要完整闭环这个论证，还缺「bitblas/marlin 前向 kernel 时间」的对照——当前脚本未测，属于读者可以补的坑（见综合实践）。

#### 4.4.4 代码实践

1. **实践目标**：把两个消融脚本的「计时对象」钉死，避免日后误读数字。
2. **操作步骤**：
   - 通读 [test_bitblas.py:48-58](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/test_bitblas.py#L48-L58)，在计时区内圈出唯一的被测语句；
   - 通读 [test_marlin.py:150-158](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/test_marlin.py#L150-L158)，确认被测函数是 `pack` 而非 `forward`；再对照 [test_marlin.py:12-21](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/test_marlin.py#L12-L21) 确认 `mul` 是占位；
   - 在有 bitblas 环境的机器上（`pip install bitblas`，需 GPU）运行 `python3 test_bitblas.py` 记录 Mean time；marlin 脚本无外部依赖，可直接 `python3 test_marlin.py`（CPU 亦可运行，注意数字口径是 CPU 打包）。
3. **需要观察的现象**：bitblas 输出 `Mean time: x.xx ms`（transform_weight 单次耗时）；marlin 输出平均 pack 延迟与 packs/sec。两者都不输出任何「注意力延迟」。
4. **预期结果**：确认两个脚本的产出只是**打包/变换开销**基线；绝对数值**待本地验证**。若在报告里引用它们对比 BitDecoding 的端到端性能，属于口径错误。

#### 4.4.5 小练习与答案

**练习 1**：如果要把 bitblas 脚本改造成「真正的 decode 注意力对照基线」，最少要改哪里？
**答案**：把计时区内的 `matmul.transform_weight(weight_tensor)` 换成 `matmul(input_tensor, weight_packed)`（先在计时区外完成一次 transform 并缓存结果），input 为 \( 1\times128 \) 的 FP16 张量。这样测的才是 int4 权重 GEMM 的前向耗时，才能与 `bench_single_*` 的 kernel 时间同口径比较。

**练习 2**：`test_marlin.py` 明知 `mul` 是占位还保留 `Layer.forward`，为什么脚本仍有价值？
**答案**：它的价值在于 `pack()`——位打包的 tile 重排、槽位拼接逻辑（[L81-91](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/ablation/test_marlin.py#L81-L91)）与 BitDecoding 的 pack_Kchannel/pack_Vtensor（u4-l3）解决同一个问题「如何把量化值装进整数容器」，其成本量级可作为「离线打包路线要付多少钱」的参照。`forward` 保留只是为了类的完整性，不是被测对象。

**练习 3**：两个消融脚本的 warmup 都是 5 次，bitblas 计时 10 次、marlin 计时 100 次。哪个的样本量更成问题？为什么？
**答案**：bitblas 的 10 次更成问题。样本均值的标准误为 \( \sigma/\sqrt{N} \)，N=10 时若数据有抖动，均值置信区间很宽；且它只报 mean 不报 min/max/方差，无法判断测量质量（对照 4.3 的三值报告法）。marlin 的 pack 是 CPU 确定性操作、方差极小，100 次足够。

## 5. 综合实践

**任务：产出一份《flash_attention_2 vs bit_decoding 对比报告》，有 GPU 跑实测，无 GPU 交实验设计。**

有 GPU 路线（约 30 分钟）：

1. `cd evaluation`，用 4.2.4 的两条命令分别跑 `flash_attention_2` 与 `bit_decoding`（固定 model/batch/context/decode_len/iteration，只动 `--attn_backend` 与量化参数），各跑 2 遍取第二次（消除冷启动）。
2. 填 4.2.4 的对比表，追加两行：`GPU Memory Allocated 差值` 与「按 4.25 bit/elem 推算的理论显存节省」，核对两者是否同量级（理论 KV 节省 = \( 1.07\times10^9 \times (2 - 0.53125)\,\text{B} \approx 1.57\,\text{GB}\)，8B 权重约 16GB 不变，所以总分配量节省比例不大但 KV 部分应接近 3.8×）。
3. 把 [bench_throughput.py:84-86](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/bench_throughput.py#L84-L86) 的打印复制一份挪到 decode 循环之后（本地临时副本，勿提交），对比「prefill 峰值」与「含 decode 的峰值」差异，验证 u5-l4 讲的 `*_new` 缓冲对峰值的影响。
4. 用 4.3.4 的纸面带宽算式预测 decode 每步注意力节省，再与实测端到端每步节省相除，得到「注意力在端到端中的占比」估计，写成一句结论。

无 GPU 路线：提交一份实验设计文档，必须包含——**自变量**：attn_backend（2 水平）+ num_bits（4/2）；**控制变量**：模型、batch=1、context_len=16384、decode_len=256、dtype=float16、设备、迭代次数=5 且丢弃首轮；**因变量与统计口径**：prefill 延迟（含 randn 与打包）、单 token decode 延迟（含 host 开销，逐字说明计时边界）、两段吞吐、prefill 后与 decode 后两次峰值显存；**报告格式**：每格填 mean±std，附 min/max；**预测**：kernel 级加速上界 \( 16/4.25\approx3.8\times \)（4bit）与 \( 16/3\approx5.3\times \)（2bit g32），端到端 decode 提升等于该值乘以注意力的时间占比、prefill 略慢、显存显著下降——并写明如何用实测数据反过来检验这些预测。所有数值标注「待本地验证」。

## 6. 本讲小结

- 模型级基准 `bench_throughput.py` 的骨架是「config 注入 → prefill 整段计时 → 5 次不计时 decode 预热 → decode 整段计时 → 吞吐 = token 数/均值时间」，CSV 输出配合 `bench_throughput.sh` 的嵌套循环做批量扫描。
- 计量卫生三件套各有分工：warmup 排除一次性成本、`empty_cache` 统一起显存状态、`reset_peak_memory_stats` 让峰值只反映本轮；但脚本存在 randn 计入计时区、预热原地增长缓存、峰值只采到 prefill 后、`--iteration 1` 冷启动等口径陷阱。
- kernel 级微基准用 cudaEvent 三段式测纯 GPU 时间，min/avg/max 三值报告；`bench_single_residual.cu` 用 `seqlen_kv+1` 与「整除留整块」保证残余路径恒被执行，且打包准备全部放在计时区外；`bench_single_packdecode.cu` 是 9 参数旧签名的 API 化石，且两个 bench 的 CMake target 均被注释、include 路径硬编码。
- ablation 脚本只测「量化操作数的准备成本」（bitblas 的 transform_weight、Marlin 的 CPU pack），且 marlin 的 `mul` 是占位实现——它们支撑「在线 LOP3 反量化免解包」的设计论证，但不能当作完整注意力基线引用。
- 端到端 decode 提升被权重读取等非注意力成本稀释，理论上界是有效比特比 \( 16/(\text{num\_bits}+32/\text{group\_size}) \)——kernel 级 3-9× 与模型级个位数百分比完全可以在同一套带宽算术下自洽。

## 7. 下一步学习建议

下一讲 u7-l3「扩展实践：新增一个 group_size/num_bits 配置的完整链路」将把本讲的测量方法当作验收工具：打通 group_size=64 路径后，你需要自己设计对照实验验证新配置的正确性与性能。之后 u7-l4 架构评审会用到本讲的带宽模型去评估 residual_block_size、k-tensor 分支等取舍。建议顺带精读两个外部参照：FlashAttention 官方 benchmark 的计时框架（与本仓库 kernel 基准同源），以及 PyTorch 文档中 `torch.cuda.memory_allocated/max_memory_allocated` 的语义说明，把「统计口径」意识变成习惯。
