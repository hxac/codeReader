# LOPT 并行 tokenizer

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚长 prompt 的 tokenize 为什么会成为 PD 分离服务的首字延迟（TTFT）瓶颈，以及 LOPT（Lossless Parallel Tokenizer，无损并行分词器）用什么思路解决它。
2. 逐行读懂 `LoptParallelTokenizer` 的三段式流程：**分块 → 进程池并行 tokenize → C++ 匹配合并**，并能解释「无损」二字的来源。
3. 说出 `--lopt-pool-size` 与 `--lopt-chunk-size` 各自控制什么，以及 `maybe_get_lopt_tokenizer` 背后的三道启用门禁。
4. 理解 `OMNI_SKIP_DECODE_TOKENIZE` 等配套环境变量如何让 Decode 节点直接复用 Prefill 节点的 tokenize 结果，彻底跳过重复分词。

本讲是「性能与功能机制」单元的第三讲。前面 u5-l1 讲了模型最佳实践配置、u5-l2 讲了图编译，本讲关注的是推理主链路更前端的一环：**文本进模型之前的那一步**。

## 2. 前置知识

### 2.1 tokenize 在推理链路中的位置

大模型推理服务的输入是文本，但模型只认 token id。因此每个请求到达后、真正开始 prefill 计算之前，必须先把完整 prompt 文本送进 tokenizer（分词器）转成 token id 序列。对 openPangu-2.0 这类支持超长上下文的模型，prompt 动辄数万甚至数十万字符，而 HF tokenizer 的 `__call__` 是**单线程、纯 CPU** 的——文本越长，这一次 tokenize 的墙钟时间就越长。

在 PD 分离架构（见 u1-l1、u4-l1）里问题被放大了：请求先到 Prefill 节点，prefill 计算本身在 NPU 上是高度并行的，而 tokenize 串行地卡在 CPU 上，形成「CPU 等待 → NPU 空转」的流水线断点，直接推高 TTFT。

### 2.2 为什么不能「随便切开并行分词」

BPE/tokenizer 的分词结果依赖上下文：把文本从中间任意一刀切开、各自分词再拼接，**拼接边界处的 token 序列通常不等于**整体分词的结果（一个词可能被劈成两半，两边各自变成残缺 token）。所以朴素并行分词是有损的——这就是 LOPT 名字里「Lossless（无损）」要解决的问题：它通过**重叠区 + 匹配合并**保证最终 token 序列与整体分词一致。

### 2.3 你需要已经了解的概念

- **进程池（`multiprocessing.Pool`）**：Python 里把一批任务分发给多个 worker 进程并行执行的常用工具；绕开 GIL，利用多核 CPU。
- **vLLM 补丁机制**：omni-npu 用 `PatchManager` 在运行时替换 vLLM 的类与方法，不改 vLLM 源码（详见 u2-l4）。
- **pybind11**：把 C++ 函数编译成 Python 可 import 的扩展模块的工具，本讲中对应 `Cpp_match_merge` 模块。
- **kv_transfer_params**：PD 分离里随请求在 P/D 节点之间透传的参数字典（详见 u4-l2），本讲的 token 复用机制会借用这个通道。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `components/omni-npu/src/omni_npu/lopt/lopt_wrapper.py` | LOPT 核心：`LoptParallelTokenizer` 类与工厂函数 `maybe_get_lopt_tokenizer` |
| `components/omni-npu/src/omni_npu/lopt/lopt_utils.py` | 工具函数：`chunks`（分块）、`pairs`（相邻对）、`flatten`（展平） |
| `components/omni-npu/src/omni_npu/lopt/csrc/match_merge.cpp` | C++ 扩展：`match`（重叠区最长公共 token 串）与 `merge`（无损拼接） |
| `components/omni-npu/src/omni_npu/lopt/__init__.py` | 对外导出三个符号 |
| `components/omni-npu/setup.py` | `Cpp_match_merge` 扩展的**可选**构建逻辑 |
| `components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_lopt.py` | 把 LOPT 接进 vLLM：CLI 参数、ModelConfig 字段、OpenAIServing 替换 |
| `components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_prefilled_token_skip_tokenize.py` | 配套的 token 复用补丁（消费 `OMNI_SKIP_DECODE_TOKENIZE`） |
| `tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml` | 生产部署模板：`--enable-lopt` 参数与环境变量的注入位置 |
| `components/omni-npu/tests/unit/lopt/` | 无需 NPU 的单元测试，是本讲代码实践的主战场 |

一条链路概括：**ansible 模板的 EXTRA_ARGS → pd_run.sh → start_api_servers.py → `vllm serve --enable-lopt ...` → patch_lopt.py 注册的 CLI 参数 → ModelConfig 三个新字段 → OpenAIServing.__init__ 里调 maybe_get_lopt_tokenizer → 请求到达时用 LoptParallelTokenizer 并行分词**。

## 4. 核心概念与源码讲解

### 4.1 并行 tokenizer：从瓶颈到三段式流程

#### 4.1.1 概念说明

LOPT 的解法可以概括成三步：

1. **分块（chunk）**：把长文本按 `chunk_size` 字符切成多块，相邻块之间保留一段**重叠区（overlap）**。
2. **并行 tokenize**：把各块文本分发进进程池，每块独立调用 HF tokenizer；由于每块都多带了一段重叠区，块边界处的「上下文」被保留了下来。
3. **匹配合并（match & merge）**：对每对相邻块，在重叠区里找一段**两块分词结果完全一致的 token 串**（以 token 的字符偏移为纽带），在一致的位置上裁剪拼接，重叠区只保留一份。

「无损」的关键在第 3 步：拼接点两侧的 token 序列在两块中逐 token 相同（覆盖相同的字符区间），因此合并结果等价于对整段文本的分词。若某对相邻块找不到足够长的公共串，LOPT 不硬拼，而是**整体回退**到标准 tokenizer，宁可慢也不能错。

理想情况下的加速比：设文本被切成 \( n \) 块、进程池有 \( P \) 个 worker、每块 tokenize 耗时约 \( \bar{t} \)，则

\[ T_{\text{串行}} \approx n \cdot \bar{t}, \qquad T_{\text{LOPT}} \approx \left\lceil \frac{n}{P} \right\rceil \cdot \bar{t} + T_{\text{match/merge}} \]

重叠区带来约 `overlap_ratio`（默认 0.125，即 12.5%）的额外 tokenize 计算量，换来的是并行度与无损保证。

#### 4.1.2 核心流程

```text
请求文本 text（len(text) ≥ 2 × chunk_size 才走 LOPT，否则直通标准 tokenizer）
   │
   ▼
chunks(text, chunk_size, overlap)          # 切成 n 块，相邻块共享 overlap 字符
   │
   ▼
pool.map(_tokenize_chunk, 块列表)           # 进程池并行分词，每块返回
   │                                        # input_ids + offset_mapping
   ▼
tokens_shards = 各块 offset_mapping 展平后取 [::2]   # 每个 token 的起始字符偏移
   │
   ▼
对每对相邻块调用 Cpp_match_merge.match      # 找重叠区最长公共 token 串
   │  ├─ 成功：拼出裁剪点表 matches
   │  └─ 失败（返回 -1,-1 → RuntimeError）：整体回退标准 tokenizer
   ▼
Cpp_match_merge.merge(各块 token, matches)  # 按裁剪点拼接，重叠区只留一份
   │
   ▼
如需 special tokens：build_inputs_with_special_tokens 补齐
   │
   ▼
返回 BatchEncoding（与 HF tokenizer 返回格式兼容）
```

#### 4.1.3 源码精读

先看入口判定的短路逻辑：

[lopt_wrapper.py:93-101](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/lopt/lopt_wrapper.py#L93-L101)：`__call__` 里 `len(text) < self.chunk_size * 2` 时直接调标准 tokenizer。注意这里的长度是**字符数**而非 token 数——按默认 `chunk_size=4096`，短于 8192 字符的文本完全不进并行路径，避免「并行开销大于收益」。恰好等于 `2 × chunk_size` 的文本则会进并行路径（单元测试对这条边界有专门断言，见 4.1.4）。

再看分块策略：

[lopt_utils.py:10-26](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/lopt/lopt_utils.py#L10-L26)：`chunks` 对字符串的处理是一个 while 循环——只要剩余文本比 `chunk_size` 还多出 100 字符以上，就 `yield sentence[:overlap_length + chunk_size]`（本块正文 + 重叠 lookahead），然后 `sentence = sentence[chunk_size:]`（游标前进一个 chunk_size）。效果是：**第 i 块覆盖字符区间 `[i·chunk_size, (i+1)·chunk_size + overlap)`**，相邻两块恰好共享 `overlap` 个字符；尾巴不足时整段作为最后一块。这个「游标前进 chunk_size、每块多带 overlap」的设计，正是后面 C++ `match` 里 `chunk_size` 偏移对齐的依据。

[lopt_wrapper.py:35-57](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/lopt/lopt_wrapper.py#L35-L57)：构造函数里四个关键参数——`pool_size`（进程数）、`chunk_size`（分块字符数）、`overlap_ratio`（重叠比例，默认 0.125）、由前两者算出的 `self.overlap = int(chunk_size * overlap_ratio)`（重叠**字符数**，4096×0.125=512）；以及 `self.threshold = 2`（匹配失败的最小公共串阈值）。同时还加载了一份**主进程自己的** tokenizer（`self.tokenizer`），用于短文本直通与回退路径。

#### 4.1.4 代码实践：亲手切一次块

**实践目标**：直观验证 `chunks` 的重叠区行为，为理解后续 `match` 的 `chunk_size` 偏移打好基础。

**操作步骤**（在已部署容器内，或任何装有 `torch`、`numpy`、`transformers` 并可 `import omni_npu` 的环境；`omni_npu/__init__.py` 是空的，CPU 环境即可运行）：

```python
# 示例代码：手工验证分块
from omni_npu.lopt.lopt_utils import chunks

text = "甲" * 300          # 300 字符
cs, ov = 100, 12           # chunk_size=100, overlap=12
result = list(chunks(text, cs, ov))
for i, c in enumerate(result):
    print(i, len(c))
```

**需要观察的现象**：

1. 第 0 块长度应为 112（100 正文 + 12 lookahead），第 1 块长度 112，最后一块是剩余部分；
2. 第 i 块的起点恰好是第 i−1 块起点的 +100 处，即相邻两块共享 12 个字符；
3. 把文本换成 190 字符（= chunk_size + 90，尾部不足 100）时只会得到 1 块，不触发切分。

**预期结果**：切块数 ≈ `ceil((len - overlap) / chunk_size)`；总 tokenize 字符量比原文多约 `块数 × overlap`。具体数字待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `__call__` 的短路阈值是 `chunk_size * 2` 而不是 `chunk_size`？

**答案**：文本必须至少能切成两块，并行才有意义；若阈值设在 `chunk_size`，一段 1.5 倍 chunk_size 的文本会被切成「一块正文 + 一小块尾巴」，两块各自还要多 tokenize 一段重叠区，并行收益为负。

**练习 2**：`overlap_ratio` 调大（比如 0.5）会发生什么？

**答案**：相邻块共享的字符更多，重叠区里找到足够长公共 token 串的概率上升（回退更少），但每块额外 tokenize 的计算量从 12.5% 涨到 50%，并行有效吞吐下降。这是一个「成功率 vs 计算量」的权衡，默认 0.125 是折中值。

### 4.2 进程池管理：mp.Pool、worker 初始化与 C++ 扩展构建

#### 4.2.1 概念说明

`LoptParallelTokenizer` 不是每次请求临时建进程，而是在**构造时一次性**创建 `multiprocessing.Pool`，让每个 worker 进程在启动时各自加载一份独立的 tokenizer 实例，之后所有请求复用这个池。这解决两个问题：

1. **tokenizer 加载成本**：`AutoTokenizer.from_pretrained` 要读词表文件、构建 BPE 状态机，加载一次可能上百毫秒级，绝不能每块文本付一次。
2. **GIL 限制**：HF fast tokenizer虽然底层是 Rust 实现，但调度上仍是单请求串行；多进程池把不同块的 tokenize 真正分摊到多个 CPU 核。

同时，C++ 扩展 `Cpp_match_merge` 是**可选依赖**：构建 omni-npu 时若缺 pybind11，扩展被跳过，LOPT 在运行时自动退化为不可用状态（而不是让整个安装失败）。

#### 4.2.2 核心流程

```text
LoptParallelTokenizer.__init__(model_path, pool_size, chunk_size, overlap_ratio)
   ├─ self.tokenizer = AutoTokenizer.from_pretrained(...)        # 主进程兜底用
   ├─ self.pool = mp.Pool(pool_size,
   │       initializer=_init_worker, initargs=(model_path,))     # 每个 worker 启动即加载 tokenizer
   └─ self._finalizer = weakref.finalize(self, self.close)       # 对象被 GC 时自动回收池

请求到来 → pool.map(_tokenize_chunk, text_chunks)
   ├─ worker 进程各自调用「本进程全局」_worker_tokenizer(text)
   └─ 结果（numpy 数组字典）pickle 回主进程

服务下线 → close(): pool.close() + pool.join(), pool = None
```

#### 4.2.3 源码精读

[lopt_wrapper.py:66-81](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/lopt/lopt_wrapper.py#L66-L81)：`_init_worker` 是 `mp.Pool` 的 initializer——在每个 worker 进程里执行一次，把 tokenizer 存进**模块级全局变量** `_worker_tokenizer`；`_tokenize_chunk` 是静态方法，worker 收到文本后调用本进程的这份全局 tokenizer，并要求返回 `return_tensors="np"`（numpy 格式便于 C++ 扩展消费）、`return_offsets_mapping=True`（每个 token 的 `(起始, 结束)` 字符偏移，匹配阶段的纽带）、`add_special_tokens=False`（special tokens 必须等合并完成后统一添加，否则每块都会带一份）。注意 `__init__` 里 [lopt_wrapper.py:48-50](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/lopt/lopt_wrapper.py#L48-L50) 用了 `use_fast=True`——offset_mapping 只有 fast tokenizer 才支持，这是硬前提。

[lopt_wrapper.py:52-64](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/lopt/lopt_wrapper.py#L52-L64)：进程池创建与 `weakref.finalize(self, self.close)`——当 tokenizer 包装对象被垃圾回收时自动触发 `close()`（`pool.close()` 让 worker 处理完手头任务后退出，`pool.join()` 等待收尾，最后置 `pool = None` 防重复关闭）。这保证了即使没人显式调用 `close`，worker 进程也不会泄漏。

[lopt_wrapper.py:13-19](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/lopt/lopt_wrapper.py#L13-L19)：模块顶部 try-import `Cpp_match_merge`，成功则 `LOPT_AVAILABLE = True`。这个布尔标志是后面启用门禁的第二道闸。

[setup.py:93-119](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/setup.py#L93-L119)：LoPT 的 C++ 扩展用 pybind11 定义为顶层模块 `Cpp_match_merge`（源码只有一个 `match_merge.cpp`）；整个定义包在 `try: import pybind11 ... except ImportError` 里，缺 pybind11 时只打一行警告并跳过，注释明确写着「LoPT falls back to standard tokenization」——可选依赖 + 运行时降级是贯穿 LOPT 全链路的容错哲学。

#### 4.2.4 代码实践：跑通无需 NPU 的单测

**实践目标**：用仓库自带的 mock 单测验证你对进程池生命周期的理解。

**操作步骤**：

1. 进入 `components/omni-npu` 目录；
2. 执行 `bash tests/run_tests.sh unit -- tests/unit/lopt -q`（或直接 `pytest tests/unit/lopt -q`）；
3. 重点读 [test_lopt_wrapper.py:34-62](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/lopt/test_lopt_wrapper.py#L34-L62)：测试用 `patch("multiprocessing.Pool")` 把池换成 MagicMock，断言池确实以 `(pool_size, initializer=..., initargs=(model_path,))` 创建。

**需要观察的现象**：全部用例通过；`test_init_basic` 断言 `mock_pool_cls.assert_called_once_with(4, initializer=..., initargs=("/fake/model",))`。

**预期结果**：`tests/unit/lopt` 下三份测试文件全绿。本环境是否装有 pytest 及 omni-npu 依赖待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 worker 里用模块级全局变量 `_worker_tokenizer`，而不是把 tokenizer 对象当参数传进 `pool.map`？

**答案**：`mp.Pool` 的任务参数与返回值都要经过 pickle 序列化。tokenizer 对象不可 pickle（或序列化代价极高），所以用 `initializer + initargs` 让每个 worker 自己 `from_pretrained` 加载一份，任务里只传纯字符串文本、返回 numpy 数组，都是可高效 pickle 的类型。

**练习 2**：`close()` 里 `pool.close()` 和 `pool.terminate()` 有什么区别？这里为什么选前者？

**答案**：`terminate()` 立即杀掉 worker，正在处理的任务结果丢失；`close()` 停止接收新任务、让 worker 做完手头的块再退出，随后 `join()` 收尸。分词服务下线时选 `close()` 更稳妥（源码注释写的是 "Terminate the worker processes immediately"，但实现实际是优雅关闭——以代码为准）。

### 4.3 无损合并：C++ match/merge 与重叠区的 token 复用

#### 4.3.1 概念说明

并行的代价是重叠区被两块各分词了一次，合并阶段必须决定「重叠区里保留哪一份、从哪里切开」。LOPT 的做法是以 **token 的字符偏移（offset）** 为纽带：

- 第 i 块与第 i+1 块的重叠区，在两块里分别产生了一段 token 序列；
- 在重叠区中找一串**连续的 token**，它们在第 i 块和第 i+1 块中的字符偏移完全相同（即两块对同一段字符的分词结果逐 token 一致）；
- 在这串「共识 token」处拼接：重叠区只保留一份，其余裁掉。

为什么找**最长**公共串、还要求长度 `> threshold`（阈值 2，即至少 3 个 token）？因为越长的共识串说明两块分词在这段字符上越「稳定」，拼接点落在其中任何位置都无损；而太短的匹配（比如单个常见字）可能是巧合，用它拼接有风险——此时宁可整体回退标准 tokenizer。这就是「token 复用」在 LOPT 内部的第一层含义：**重叠区的 token 被复用（去重）而不是重复输出**。

#### 4.3.2 核心流程

```text
 Parallel 主进程（_parallel_encode）
   ├─ tokens_shards[i] = flatten(shard.offset_mapping)[::2]   # 每块每个 token 的起始偏移
   ├─ for (a, b) in pairs(tokens_shards):                     # 相邻块两两一组
   │     res = Cpp_match_merge.match(a, b, chunk_size, threshold)
   │     # C++ 从两段偏移序列的尾部反向同步扫描：
   │     # 当 chunks0[i0] == chunks1[i1] + chunk_size（同一字符位置）时累计公共串
   ├─ matches = [首块全长] + 所有 (a,b) 对拼接 + [0]           # 每块一对裁剪点
   └─ for key（input_ids 等非 offset/mask 字段）:
         merged[key] = Cpp_match_merge.merge([各块该字段], matches)
```

注意 `match` 的对齐条件 `chunks0[i0] == chunks1[i1] + chunk_size`：第 i+1 块的文本起点在原文的第 `chunk_size`（相对第 i 块）个字符处，所以把第 i+1 块内的偏移**加上 chunk_size** 后与第 i 块的偏移比较，才是「同一个字符」。这也是为什么 C++ 文档字符串要求两个输入序列**单调不减**（token 起始偏移天然递增）。

#### 4.3.3 源码精读

[lopt_wrapper.py:110-145](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/lopt/lopt_wrapper.py#L110-L145)：`_parallel_encode` 主体。四个关键点：

1. L113 `shards = self.pool.map(self._tokenize_chunk, text_chunks)`——并行分发；
2. L115-L117 `flatten(shard["offset_mapping"])[::2]`——offset_mapping 是 `(start, end)` 对的列表，展平成 `[s0, e0, s1, e1, ...]` 后 `[::2]` 只取起始偏移；
3. L119-L131 对相邻块逐对调用 `_cpp_match_wrapper`，任何一个失败（`RuntimeError`）则整段回退 `self.tokenizer(text, ...)` 并打 warning——**正确性优先于速度**；
4. L133-L143 对除 `offset_mapping`/`attention_mask` 外的每个字段（`input_ids` 等）调用 `Cpp_match_merge.merge` 拼接；若 `add_special_tokens=True`，再对 `input_ids` 调 `build_inputs_with_special_tokens` 统一补 special tokens（呼应 4.2.3 中每块分词时关掉 special tokens 的设计）。

[lopt_wrapper.py:83-91](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/lopt/lopt_wrapper.py#L83-L91)：`_cpp_match_wrapper` 先用 `np.ascontiguousarray(..., dtype=np.int64)` 把偏移数组整理成 C 连续内存再进 C++；返回值 `res[0] < 0` 时抛 `RuntimeError`。

[match_merge.cpp:28-58](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/lopt/csrc/match_merge.cpp#L28-L58)：`match` 模板函数。双指针 `i0`、`i1` 分别从两段偏移序列**末尾**向左扫描；`chunks0[i0] > chunks1[i1] + chunk_size` 时左移 `i0`（跳过第 0 块中超出重叠区的部分）；相等时累计 `current_match_len`，同时维护历史最长串的记录位置；L52-L53 若最长串长度 `<= Mismatch_thres`（即传入的 threshold=2）返回 `(-1, -1)` 表示匹配失败。最终返回值被换算成「从各自数组末尾数起」的裁剪距离，供 merge 使用。

[match_merge.cpp:62-77](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/lopt/csrc/match_merge.cpp#L62-L77)：`merge` 对每块 i 取迭代器区间 `[chunks[i].end() - matches[2i], chunks[i].end() - matches[2i+1])`——即按 match 算出的两个「距末尾」裁剪点截取保留段并顺序拼接。Python 侧组装的裁剪点表（[lopt_wrapper.py:124-128](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/lopt/lopt_wrapper.py#L124-L128)）是「首块全长 + 各相邻对的 (a, b) + 末尾 0」，长度恰为 `2 × 块数`，与 merge 的 L68-L71 校验一致。

[lopt_utils.py:29-44](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/lopt/lopt_utils.py#L29-L44)：`pairs` 生成相邻块对 `(chunk_list[i], chunk_list[i+1])`；`flatten` 兼容 torch 张量、numpy 数组和嵌套 list 三种类型统一展平——兼容三种类型是因为测试和不同 tokenizer 后端返回结构可能不同。

#### 4.3.4 代码实践：验证「无损」

**实践目标**：用单测断言理解边界行为，特别是「多短算短文本」「多长触发并行」。

**操作步骤**：

1. 阅读 [test_lopt_wrapper.py:312-352](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/lopt/test_lopt_wrapper.py#L312-L352)：`test_parallel_encode_success` 用两个手工构造的 shard（input_ids 分别为 `[1,2,3]` 与 `[3,7,8,9]`，注意 token `3` 是两块重叠区的重复 token），mock 掉 `Cpp_match_merge` 与 `pool.map`，断言 match 与 merge 都被调用；
2. 再看 [test_lopt_wrapper.py:391-406](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/lopt/test_lopt_wrapper.py#L391-L406)：`boundary_text = "a" * 20`（`chunk_size=10` 时恰为 2 倍），断言 `_parallel_encode` 被调用；
3. 回答：若把 20 改成 19，`_parallel_encode` 还会被调用吗？

**需要观察的现象**：mock 的 `merge` 返回 `[1,2,3,7,8,9]`——重叠 token `3` 只出现一次，这就是合并去重的直观体现。

**预期结果**：19 字符时走短路路径，`_parallel_encode` 不会被调用（因为 `19 < 10*2`）。可用 pytest 临时改写该用例验证，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `merge` 只拼接「非 offset_mapping、非 attention_mask」的字段？

**答案**：offset_mapping 是各块**局部坐标**，拼接后不再有意义；attention_mask 对纯文本输入全为 1，合并后的序列不再需要。保留 `input_ids` 等真正进入引擎的字段即可。

**练习 2**：如果某对相邻块的 match 返回 `(-1, -1)`，LOPT 的行为是什么？为什么这样设计？

**答案**：`_cpp_match_wrapper` 检测到 `res[0] < 0` 抛 `RuntimeError`，`_parallel_encode` 捕获后打 warning 并对**整段文本**回退标准 tokenizer。因为找不到足够长共识串时强行拼接可能有损，而 LOPT 的承诺是无损——宁可这一条请求慢，也不能让 token 序列出错。

### 4.4 接入 vLLM：patch_lopt 的三个补丁与 maybe_get_lopt_tokenizer 的启用条件

#### 4.4.1 概念说明

LOPT 本体只是一个「像 tokenizer 的对象」，要让它生效，还须解决三个接入问题（全部由 u2-l4 讲过的 PatchManager 完成，无需改 vLLM 源码）：

1. **参数从哪来**：vLLM 原生没有 `--enable-lopt` 这些 CLI 参数——补丁给 `EngineArgs` 注册新参数、给 `ModelConfig` 加新字段并打通传递；
2. ** tokenizer 何时构建**：在 `OpenAIServing.__init__`（API server 启动时）构建一次，随服务常驻；
3. **请求路径怎么换**：替换 `OpenAIServing._normalize_prompt_text_to_input`（vLLM 把提示文本归一化为 input_ids 的入口），使文本 prompt 走 LOPT。

而工厂函数 `maybe_get_lopt_tokenizer` 是**启用条件的收口点**——三道门禁任何一道不过都返回 `None`（静默降级为普通 tokenization）。

#### 4.4.2 核心流程

```text
vllm serve --enable-lopt --lopt-pool-size 16 --lopt-chunk-size 4096
   │  （参数由 patch_lopt 注册进 argparse）
   ▼
EngineArgs.from_cli_args ──► EngineArgs.create_model_config ──► ModelConfig.{enable_lopt, lopt_pool_size, lopt_chunk_size}
   │
   ▼
OpenAIServing.__init__（被补丁替换）
   ├─ enable_lopt=False → 打日志 "Not Enabled"，lopt_tokenizer = None
   └─ enable_lopt=True  → maybe_get_lopt_tokenizer(model_path, ...)
        ├─ 门 1：enable_lopt 为假 → None
        ├─ 门 2：LOPT_AVAILABLE 为假（Cpp_match_merge 没编出来）→ warning + None
        └─ 门 3：构造抛异常（如模型路径加载失败）→ warning + None
   │
   ▼
请求到达 → _normalize_prompt_text_to_input（被补丁替换）
   ├─ enable_lopt 且 lopt_tokenizer 非 None → lopt_tokenizer(prompt, add_special_tokens)
   │      → 截断处理（truncate_prompt_tokens / max_model_len）→ _validate_input
   └─ 否则 → 回落 vLLM 原生逻辑
```

#### 4.4.3 源码精读

[patch_lopt.py:29-39](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_lopt.py#L29-L39)：补丁一给 `ModelConfig` 挂上三个类属性：`enable_lopt: bool = False`、`lopt_pool_size: int = 16`、`lopt_chunk_size: int = 4096`——注意这里的默认值（16/4096）与 `LoptParallelTokenizer.__init__` 的默认值（8/2048）**不同**，经由 CLI/ansible 部署时用的是补丁的默认值。

[patch_lopt.py:80-106](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_lopt.py#L80-L106)：补丁二在 `add_cli_args` 里追加三个参数：`--enable-lopt`（store_true 开关）、`--lopt-pool-size`（进程数，默认 16）、`--lopt-chunk-size`（分块字符数，默认 4096）。[L108-L121](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_lopt.py#L108-L121) 的 `from_cli_args`/`create_model_config` 把值从 EngineArgs 一路搬进 ModelConfig。这里用了「补丁链」手法：apply 时先保存上游已被其他补丁改过的版本（`_upstream_add_cli_args`），自己的实现先调上游再追加，避免覆盖别的补丁。

[patch_lopt.py:150-192](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_lopt.py#L150-L192)：补丁三替换 `OpenAIServing.__init__`——先调上游原构造，再读 `model_config.enable_lopt`；为真时打 warning 级日志 `Lossless Parallel Tokenizer Enabled! pool size=..., chunk length=...`（这条日志就是线上确认 LOPT 生效的第一依据），随后调用 `maybe_get_lopt_tokenizer` 构建。为假时也打一条 `Not Enabled` 日志。

[lopt_wrapper.py:148-172](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/lopt/lopt_wrapper.py#L148-L172)：工厂函数的三道门禁——`if not enable_lopt: return None`（门 1，开关没开）；`if not LOPT_AVAILABLE: warning + return None`（门 2，C++ 扩展缺失）；`try: return LoptParallelTokenizer(...) except Exception: warning + return None`（门 3，构造失败如模型路径非法）。三道门全部只降级、不抛异常——LOPT 是纯优化，任何问题都不应阻断服务启动。

[patch_lopt.py:194-218](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_lopt.py#L194-L218)：请求路径替换。`_normalize_prompt_text_to_input` 里若 LOPT 可用则 `encoded = self.lopt_tokenizer(prompt, add_special_tokens)`，随后手动复刻 vLLM 的截断逻辑（`truncate_prompt_tokens` 为负截到 `max_model_len`，为正取 `min(truncate_prompt_tokens, max_model_len)`），最后走 `_validate_input` 返回；否则回落上游原函数。另外 [patch_lopt.py:226-234](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_lopt.py#L226-L234) 补丁四把 `maybe_get_lopt_tokenizer` 重新导出到 `vllm.tokenizers` 模块命名空间，供其他 vLLM 侧代码调用。

#### 4.4.4 代码实践：追踪参数从模板到引擎的完整链路

**实践目标**：把 `--enable-lopt` 从 ansible 模板一路追到 `LoptParallelTokenizer` 构造，并回答规格里的两个问题。

**操作步骤**：

1. 读模板 [omni_infer_server_template_performance1P1D_92B_bf16_open.yml:92](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L92)（P 侧）与 [L202](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L202)（D 侧）的 `EXTRA_ARGS`，找到 `--enable-lopt --lopt-pool-size 16 --lopt-chunk-size 4096`；
2. 顺着 [L144](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L144) 的 `--extra-args "${EXTRA_ARGS}"` 进入 `tools/scripts/pd_run.sh`，找到它转发给 `start_api_servers.py` 的位置（`pd_run.sh` 中 `common_operations` 函数把 `--extra-args "$EXTRA_ARGS"` 传给 python 脚本）；
3. 读 `start_api_servers.py` 的 `process_extra_args`（按 `--` 切分再逐个拼回）与把参数追加进 `vllm serve` 命令的位置；
4. 回答：**`--lopt-pool-size 16` 控制什么？`--lopt-chunk-size 4096` 控制什么？**

**需要观察的现象**：P、D 两侧模板都带同一组 LOPT 参数（说明两侧 API server 都启用了 LOPT）。

**预期结果（答案）**：`--lopt-pool-size 16` = 进程池 worker 数，即最多 16 个 CPU 进程并行分词（对应 `mp.Pool(pool_size)`）；`--lopt-chunk-size 4096` = 分块字符数，超过 `2×4096=8192` 字符的 prompt 才会被切成约 4096 字符的块并行处理，相邻块重叠 `4096×0.125=512` 字符。

#### 4.4.5 小练习与答案

**练习 1**：线上如何确认 LOPT 真的生效了，而不是被静默降级？

**答案**：看 API server 启动日志。启用成功必有 `Lossless Parallel Tokenizer Enabled! pool size=..., chunk length=...`；若出现 `LOPT was requested but Cpp_match_merge module is not available` 或 `Failed to initialize LOPT tokenizer` 的 warning，说明被门 2/门 3 拦下、已降级为标准分词（服务仍正常）。

**练习 2**：`maybe_get_lopt_tokenizer` 为什么捕获 `Exception` 后只打 warning 而不抛出？

**答案**：LOPT 是纯加速优化，产出必须与标准分词等价。若因它启动失败就拒绝拉起服务，等于把「可选优化」变成了「单点故障」，违背降级哲学；返回 `None` 后请求路径自动回落 vLLM 原生逻辑。

### 4.5 配套环境变量：OMNI_SKIP_DECODE_TOKENIZE 与跨节点 token 复用

#### 4.5.1 概念说明

LOPT 解决的是「单节点上 tokenize 慢」；PD 分离下还有一个更隐蔽的浪费：请求经 proxy 先到 P 节点完成 prefill，随后**同一个 prompt 又被送到 D 节点**再做一次完整 decode 侧准备——其中就包括把整段 prompt 重新 tokenize 一遍。对超长 prompt，这次重复分词同样是纯 CPU 串行开销。

omni-npu 的解法是**跨节点 token 复用**：P 节点（用 LOPT 或普通方式）算出的 `prompt_token_ids` 塞进 `kv_transfer_params`，随请求一起传给 D 节点；D 节点发现参数里带了这个字段，就直接用 token ids 构造引擎输入，**完全跳过 tokenize**。开关就是 `OMNI_SKIP_DECODE_TOKENIZE=1`。这是「token 复用」的第二层含义：**P 算过的 token，D 不再算**。

同族还有一个更进一步的 `OMNI_REUSE_PREFILLED_TOKENS=1`：P 节点 prefill 产出的**首个生成 token 及其 logprobs** 也随 kv_transfer_params 回传复用（避免 P/D 首 token 不一致），两者常一起开启。

#### 4.5.2 核心流程

```text
P 节点（OMNI_SKIP_DECODE_TOKENIZE=1）
  chat_completion_full_generator（被补丁替换）
    └─ final_res.kv_transfer_params["prompt_token_ids"] = final_res.prompt_token_ids
       （把 tokenize 结果挂到随请求透传的参数字典上）
          │  请求经 proxy 转发到 D 节点，kv_transfer_params 原样随行
          ▼
D 节点
  OpenAIServing._preprocess_chat（被补丁替换）
    └─ if "prompt_token_ids" in request.kv_transfer_params:
           engine_prompt = PrefilledTextPrompt(prompt_token_ids=...)   # 直接用，不再 tokenize

Scheduler.add_request 守卫（两侧均生效）
  └─ pd_flags_enabled = OMNI_REUSE_PREFILLED_TOKENS 或 OMNI_SKIP_DECODE_TOKENIZE 为 1
       → 按 prompt_len + max_tokens ≤ 有效 max_model_len 提前校验/拒绝
```

#### 4.5.3 源码精读

[omni_infer_server_template_performance1P1D_92B_bf16_open.yml:74-75](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L74-L75)：P 侧模板 `export OMNI_REUSE_PREFILLED_TOKENS=1`、`export OMNI_SKIP_DECODE_TOKENIZE=1`；[L161-L162](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L161-L162) D 侧同样两个导出——两侧都要认识这些标志。值得注意的是，omni-cache 相关模板（如 `performance3P1D_92B_w8a8_open_omni_cache.yml`）把 `OMNI_SKIP_DECODE_TOKENIZE` 设为 0，推测与 omni-cache 场景下 D 侧本地流程有关，具体原因**待确认**（不要在没证据时下结论）。

[patch_prefilled_token_skip_tokenize.py:427-452](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_prefilled_token_skip_tokenize.py#L427-L452)：P 侧出口。`chat_completion_full_generator` 里读两个环境变量（L427-L428）；`skip_decode_tokenize` 为真且响应带 `kv_transfer_params` 时，把 `final_res.prompt_token_ids` 写进去（L450-L452，注释原文："In Prefill node, the response will carry prompt_token_ids with kv_transfer_params"）。紧随其后的 L453-L468 是 `OMNI_REUSE_PREFILLED_TOKENS` 分支——把首个生成 token、其 logprobs、stop_reasons 等也塞进同一字典。

[patch_prefilled_token_skip_tokenize.py:392-397](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_prefilled_token_skip_tokenize.py#L392-L397)：`PrefilledTextPrompt`——继承 vLLM 的 `TokensPrompt`，专用于「token 已就绪、无需再分词」的请求。

[patch_prefilled_token_skip_tokenize.py:604-639](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_prefilled_token_skip_tokenize.py#L604-L639)：D 侧入口。补丁替换 `OpenAIServing._preprocess_chat`：先照常调上游拿到 `engine_prompt`，若 `request.kv_transfer_params` 里有 `prompt_token_ids` 就直接用 `PrefilledTextPrompt` 覆盖（L630-L632）——这就是「跳过 decode 侧 tokenize」的落点；随后 `_reject_if_prompt_overflows_max_model_len` 做长度校验。

[patch_prefilled_token_skip_tokenize.py:770-787](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_prefilled_token_skip_tokenize.py#L770-L787)：Scheduler 侧守卫。两个标志任一开启即 `pd_flags_enabled`，触发更严格的长 度预检（prompt + max_tokens 超出有效 `max_model_len` 时提前拒绝并打 error 日志），避免 token 复用路径把超长请求拖到引擎深处才失败。

[patch_input_ids_piggyback.py:153-156](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_input_ids_piggyback.py#L153-L156)：互斥约束的例子——`OMNI_PIGGYBACK_INPUT_IDS=1` 要求 `OMNI_SKIP_DECODE_TOKENIZE=0`，否则直接 assert 失败。说明这些 token 通道类环境变量之间存在组合约束，不能随手混开。

#### 4.5.4 代码实践：定位环境变量的消费点

**实践目标**：建立「改模板环境变量 → 找源码消费点」的排查能力。

**操作步骤**：

1. 在仓库根目录执行（示例命令，待本地验证）：

   ```bash
   grep -rn "OMNI_SKIP_DECODE_TOKENIZE" components/omni-npu/src tools/ansible | grep -v test
   ```

2. 对 `components/` 下的每处命中，记录：所在补丁类、替换的 vLLM 方法、读到的分支行为；
3. 对照 1P1D BF16 模板 L74-L75 与 omni-cache 模板中该变量的取值差异，列成一张表。

**需要观察的现象**：源码消费点集中在 `patch_prefilled_token_skip_tokenize.py` 与 `patch_input_ids_piggyback.py`；ansible 侧大多数模板为 1、omni-cache 模板为 0。

**预期结果**：得到一张「环境变量 × 消费点 × 作用」清单；能指出 P 侧写入点在 L450-L452、D 侧消费点在 L630-L632。

#### 4.5.5 小练习与答案

**练习 1**：`OMNI_SKIP_DECODE_TOKENIZE=1` 时，D 节点的 `_preprocess_chat` 还会执行 tokenize 吗？

**答案**：会执行一次上游 `_preprocess_chat`（渲染对话模板得到文本），但只要 `kv_transfer_params` 里带了 `prompt_token_ids`，`engine_prompt` 就会被 `PrefilledTextPrompt` 整体覆盖——引擎拿到的直接是 token ids，文本分词结果被丢弃，等于跳过了 tokenize 的实际开销。

**练习 2**：为什么这两个 token 复用机制要挂在 `kv_transfer_params` 上，而不是新增一个 HTTP 头？

**答案**：`kv_transfer_params` 是 PD 分离里 P/D 之间**已有的随请求透传通道**（u4-l2 讲过的「取件码」机制就走它），proxy 对其内容原样转发。复用这个通道意味着不需要改 proxy 与协议，是零侵入设计的延续。

## 5. 综合实践

综合实践围绕规格中的任务展开：**回答两个参数的作用，并设计一个 LOPT 开关的 prefill 耗时对比实验**。

### 5.1 第一问：参数语义（可直接作答）

- `--lopt-pool-size 16`：进程池大小，即最多 16 个 worker 进程并行分词（[lopt_wrapper.py:52-56](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/lopt/lopt_wrapper.py#L52-L56) 的 `mp.Pool(pool_size, ...)`）。块数少于 16 时并行度取块数；多于 16 时按批轮转。
- `--lopt-chunk-size 4096`：分块字符数（[lopt_utils.py:10-15](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/lopt/lopt_utils.py#L10-L15)）。生效阈值是文本 ≥ 8192 字符；每块约 4096 字符正文 + 512 字符重叠（`overlap_ratio=0.125`）。

### 5.2 第二问：对比实验设计

**假设**：长 prompt 下，开启 LOPT 缩短的是「请求到达 → prefill 开始」之间 CPU 串行 tokenize 的时间，因此 TTFT（首 token 延迟）应下降；短 prompt 下两者应无差异（短路路径相同）。

**实验步骤**：

1. **准备对照组**：用 u1-l4 的流程拉起两套仅 LOPT 不同的服务——A 套模板原样（带 `--enable-lopt --lopt-pool-size 16 --lopt-chunk-size 4096`）；B 套把 `run_vllm_server_prefill_cmd` 的 EXTRA_ARGS 中三个 LOPT 参数删掉，其余不动，重跑 `--tags run_server`。
2. **构造数据集**：准备三组 prompt——短（约 2k 字符，低于 8192 阈值，作阴性对照）、中（约 32k 字符，约 8 块）、长（约 128k 字符，约 31 块），每组同一文本重复 ≥ 20 次。
3. **测量**：对每组分别请求，记录 `curl -w '%{time_starttransfer}'`（即 TTFT）与总耗时；同时确认 A 套 P 侧启动日志有 `Lossless Parallel Tokenizer Enabled!`、B 套有 `Not Enabled`。
4. **变量控制**：两次实验间重启服务清空 prefix cache（或每组都换新文本），避免 `--enable-prefix-caching` 的命中干扰；`max_tokens` 设为 1 以聚焦 prefill 阶段。
5. **预期现象**：短文本组 A/B 基本持平；中长文本组 A 的 TTFT 显著低于 B，且文本越长差距越大（串行 \( n\bar{t} \) vs 并行 \( \lceil n/16 \rceil\bar{t} \)）；若 A 出现大量 `Fall back to standard tokenizer on match failure` warning，说明该文本重叠区匹配失败率高，需检查 chunk_size 设置。

**预期结果**：得到一张「prompt 长度 × LOPT 开关 → 平均 TTFT」的表。具体数值依赖硬件与模型，待本地验证。

## 6. 本讲小结

- **LOPT 解决的是 CPU 串行 tokenize 瓶颈**：长 prompt 在 prefill 前的分词是纯 CPU 单线程工作，PD 分离下直接推高 TTFT；LOPT 用「重叠分块 + 进程池并行 + 匹配合并」把它摊到多核。
- **三段式流程**：`chunks` 按 `chunk_size` 切块并保留 `overlap_ratio×chunk_size` 字符重叠（默认 12.5%）→ `mp.Pool` 并行分词（每块关闭 special tokens、返回字符偏移）→ C++ `match` 在重叠区找最长公共 token 串（阈值 2，失败即整体回退标准分词），`merge` 按裁剪点无损拼接。
- **进程池一次构建、长期复用**：initializer 让每个 worker 自加载 tokenizer（避免 pickle），`weakref.finalize` 保证进程池随对象回收而优雅关闭；`Cpp_match_merge` 是 pybind11 可选构建，缺失只降级不报错。
- **三道启用门禁**：`maybe_get_lopt_tokenizer` 依次检查开关、C++ 扩展可用性、构造是否成功，任一不过都返回 `None` 静默降级；生效与否以启动日志 `Lossless Parallel Tokenizer Enabled!` 为准。
- **接入全靠补丁**：`patch_lopt.py` 给 vLLM 注册 `--enable-lopt/--lopt-pool-size/--lopt-chunk-size` 三个 CLI 参数与 ModelConfig 字段，并替换 `OpenAIServing` 的构造与 `_normalize_prompt_text_to_input`；ansible 模板经 EXTRA_ARGS → pd_run.sh → start_api_servers.py 注入。
- **token 复用有两层**：块间重叠区去重（merge 阶段）；跨节点复用（`OMNI_SKIP_DECODE_TOKENIZE=1` 让 P 把 `prompt_token_ids` 挂进 kv_transfer_params、D 侧用 `PrefilledTextPrompt` 直接跳过分词），且这类环境变量之间存在组合约束（如与 `OMNI_PIGGYBACK_INPUT_IDS` 互斥）。

## 7. 下一步学习建议

- **u5-l4（推理解析器与思考输出控制）**：本讲看了请求「进」模型之前的文本处理，下一讲看输出「出」模型之后的 `<think>` 切分与工具调用解析，两者共同构成 vLLM serving 层的文本前后处理全景。
- **回读 u2-l4（PatchManager）**：本讲的 `patch_lopt.py` 是「补丁链」手法（保存上游版本再追加）的典型样本，结合 patch_manager 的两级去重机制再读一遍会有更深体会。
- **延伸阅读**：`components/omni-npu/tests/unit/lopt/` 下三份测试是理解 LOPT 边界行为（短文本直通、2 倍阈值、threshold=2、close 幂等）的最快途径；有兴趣可对照 `match_merge.cpp` 手推一遍裁剪点的换算。
