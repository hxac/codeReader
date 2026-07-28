# 基准测试与正确性验证

## 1. 本讲目标

AttentionEngine 把一段 Python 描述编译成高性能 GPU kernel。但「kernel 能跑」和「kernel 跑得对、跑得快」是两回事：你生成的 online softmax kernel，既可能因为某个 `o_scale` 重缩放写错而数值发散，也可能因为 tile 选得不好而比 FlashAttention 慢一倍。`attention_engine/benchmark/bench_utils.py` 就是用来回答这两个问题的工具箱。

学完本讲，你应该能够：

- 理解 GPU kernel 计时的特殊性（异步执行、L2 缓存、warmup），并读懂 AttentionEngine 自带的 `do_bench` 是如何处理这些坑的。
- 理解相对误差（rtol）与绝对误差（atol）的组合校验，掌握 `check_close` 与 `print_debug` 的区别，能根据错误定位输出判断「是否可接受」。
- 为不同的自定义注意力选择合适的参考实现（softmax 用 flash-attn、relu 用纯 torch、线性注意力用 fla/mamba_ssm），并知道在 `bench_utils.py` 里该复用哪个 `do_bench_*` 入口。

本讲依赖 u3-l3（引擎入口、`mod` 可前向可反向），不再重复编译链路细节。

## 2. 前置知识

### 2.1 GPU kernel 为什么不能直接用 `time.time()` 计时

PyTorch 的 CUDA 调用是**异步**的：你调用 `o = attn(q, k, v)` 后，Python 立刻返回，kernel 只是「提交」到了 GPU 的命令队列里，真正的计算可能在几毫秒后才完成。如果你写：

```python
t0 = time.time()
o = attn(q, k, v)
t1 = time.time()
```

测到的只是「提交命令」的耗时（通常微秒级），而不是 kernel 真正的运行时间。正确的做法是用 **CUDA Event**：在命令队列里打两个时间戳 `start.record()` / `end.record()`，再 `torch.cuda.synchronize()` 强制等 GPU 把队列跑空，最后用 `start.elapsed_time(end)` 读出 GPU 侧的真实毫秒数。`do_bench` 就是围绕这套机制设计的。

### 2.2 L2 缓存会污染计时

GPU 有片上 L2 cache（H100 上约 50MB）。如果连续跑同一个 kernel，第二次跑时 Q/K/V 已经驻留在 L2 里，访存会异常快——这是「缓存命中」的假象。为了测到稳定的「冷数据」性能，经典做法是每次正式测量前，先用一个大约 256MB 的缓冲区把 L2 冲干净（`cache.zero_()`），逼着下一次 kernel 重新从显存读数据。这就是「L2 flush」。

### 2.3 相对误差与绝对误差

数值校验一般用两个阈值：

- 绝对误差 \( |x - y| \)（atol）：当真值本身接近 0 时，只有它能判定对错。
- 相对误差 \( |x - y| / |y| \)（rtol）：当真值很大时，绝对误差天生就大，必须用相对比例衡量。

PyTorch 的 `torch.isclose(a, b, rtol, atol)` 判定规则是「**绝对误差 < atol 或 相对误差 < rtol**」，二者满足其一即视为「接近」。AttentionEngine 的两个校验函数遵循同样的语义，但实现细节略有不同（见 4.2）。

## 3. 本讲源码地图

本讲主要围绕一个文件：

| 文件 | 作用 |
| --- | --- |
| `attention_engine/benchmark/bench_utils.py` | 全部基准与校验工具。内含通用计时器 `do_bench`、张量校验 `check_close`/`print_debug`、以及针对各类注意力的 `do_bench_*` 入口（`do_bench_attention` 对标 flash-attn、`do_bench_reluattn` 对标纯 torch、`do_bench_retention_linear`/`do_bench_simple_gla`/`do_bench_mamba` 对标 fla/mamba_ssm 等）。 |
| `attn_script/mha.py` | 标准 softmax 参照脚本，结尾调用 `do_bench_attention`，是本讲实践的运行入口。 |
| `attn_script/reluattn.py` | relu 注意力脚本，调用 `do_bench_reluattn`，展示了「没有现成库可对标时怎么办」。 |

阅读建议：先读 `do_bench`（L14–L99）建立计时心智模型，再读 `check_close`/`print_debug`（L133–L192）看校验细节，最后跳到 `do_bench_attention`（L1179–L1360）看一个完整入口如何把计时、校验、参考实现串起来。

## 4. 核心概念与源码讲解

### 4.1 do_bench：GPU kernel 计时

#### 4.1.1 概念说明

`do_bench(fn, ...)` 是一个通用的 kernel 计时器：传入一个无参可调用对象 `fn`，返回它每次调用的平均毫秒数。它要解决三个问题：

1. **异步性**：用 CUDA Event 而非 `time.time()`，并在关键位置 `synchronize`。
2. **自适应重复次数**：kernel 太快（几微秒）时，必须重复成千上万次才能凑够稳定的测量窗口；kernel 太慢（几十毫秒）时，重复几次就够。不能写死。
3. **L2 污染**：估算阶段先 flush 一次 L2，让估算更接近真实冷启动。

它的签名是 `do_bench(fn, warmup=25, rep=100, ...)`，其中 `warmup`/`rep` 的单位是**毫秒**（不是次数），函数内部会把它换算成实际次数。

#### 4.1.2 核心流程

`do_bench` 的执行分四个阶段：

1. **预跑**：调用一次 `fn()` 并 `synchronize`，确保 kernel 已编译、上下文已就绪（JIT kernel 第一次跑会触发编译，绝不能计入测量）。
2. **估算**：分配 256MB 的 flush 缓冲，连续跑 5 次「flush + fn」，用 CUDA Event 测出单次大约耗时 `estimate_ms`，据此推算要 warmup 多少次、重复多少次。
3. **预热**：空跑 `n_warmup` 次（不计时），让缓存与调度进入稳态。
4. **测量**：`start.record()` → 连续 `n_repeat` 次 `fn()` → `end.record()` → `synchronize`，总耗时除以 `n_repeat` 得到平均每次的毫秒数。

> ⚠️ 一个容易被忽略的细节：在**测量循环**里，逐次 flush L2 的 `cache.zero_()` 被注释掉了（见 4.1.3 源码）。也就是说，正式测量的其实是「warmup 之后的稳态性能」，而非每次冷启动。这与 Triton 官方 `do_bench`「每次都 flush」的做法不同，读源码时务必留意。

#### 4.1.3 源码精读

预跑 + 分配 flush 缓冲（注意 `fast_flush=True` 走 `int` 数组的 `zero_()`，这是一个高度优化的 memset kernel，比 `int8` 写入更快）：

[attention_engine/benchmark/bench_utils.py:L43-L52](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L43-L52) — 预跑 `fn` 并 `synchronize`；按 `fast_flush` 开关分配 256MB 的 L2 flush 缓冲（`int` 64M 元素 vs `int8` 256M 元素，二者字节数相同）。

估算阶段，用 5 次「flush + fn」推算单次耗时：

[attention_engine/benchmark/bench_utils.py:L54-L63](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L54-L63) — 在两次 CUDA Event 之间循环 5 次 `cache.zero_()` + `fn()`，`elapsed_time/5` 得到 `estimate_ms`。

由估算耗时把「毫秒预算」换算成「次数」：

[attention_engine/benchmark/bench_utils.py:L66-L71](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L66-L71) — `n_warmup = max(1, int(warmup/estimate_ms))`，`n_repeat = max(1, int(rep/estimate_ms))`；下划线开头的 `_n_warmup`/`_n_repeat` 用于强制覆盖。

正式测量（注意循环内 `cache.zero_()` 被注释、`grad_to_none` 仅用于反向测梯度）：

[attention_engine/benchmark/bench_utils.py:L78-L99](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L78-L99) — 预热 `n_warmup` 次后，`start.record()` 包住 `n_repeat` 次 `fn()`，`end.record()`、`synchronize`，返回 `time / n_repeat`（平均每次毫秒数）。

把平均毫秒延迟换算成 TFLOPS 用的是：

\[ \text{TFLOPS} = \frac{F}{t_{\text{ms}}} \times 10^{-9} \]

其中 \(F\) 是该 kernel 的浮点运算量、\(t_{\text{ms}}\) 是 `do_bench` 返回的毫秒数。因为 1 ms = \(10^{-3}\) s、1 TFLOPS = \(10^{12}\) FLOP/s，所以 \(F / t_{\text{ms}} \times 10^{-9}\) 正好得到 TFLOPS。前向的 \(F\) 为：

\[ F_{\text{fwd}} = 2BHS_qS_{kv}D + 2BHS_qS_{kv}D_V \quad (\text{causal 时乘 }0.5) \]

反向（见 `do_bench_attention` 中的 `bwd_tflops`）为：

\[ F_{\text{bwd}} = 4BHSSD_V + 6BHSSD \quad (\text{causal 时乘 }0.5) \]

> 提示：`do_bench` 签名里还有 `quantiles` 和 `return_mode` 两个参数，但实现里 `return_mode` 只做了 `assert` 校验、并未真正改变返回值，函数永远返回「平均每次毫秒数」。这是读源码时的一个小提醒：参数存在不等于已被使用。

#### 4.1.4 代码实践

**实践目标**：亲手用 `do_bench` 给一个 PyTorch 算子计时，观察「同步」「warmup 次数自适应」两个现象。

**操作步骤**（示例代码，非项目原有）：

```python
# 示例代码：感受 do_bench 的计时机制
import torch
from benchmark.bench_utils import do_bench

q = torch.randn(1, 2048, 128, 128, device="cuda", dtype=torch.float16)
k = torch.randn(1, 2048, 128, 128, device="cuda", dtype=torch.float16)

# 用一个轻量算子做被测函数
def fn():
    return torch.matmul(q, k.transpose(-1, -2))

ms = do_bench(fn, warmup=25, rep=100)
print(f"avg latency = {ms:.4f} ms")
```

**需要观察的现象**：
- 改成「不调用 `do_bench`、直接用 `time.time()` 包住一次 `fn()`」对比，会发现 `time.time()` 测出的值明显偏小且抖动剧烈——这就是异步提交的假象。
- 把 `fn` 换成一个极快的小 matmul（如 `D=8`），打印 `do_bench` 内部的 `n_repeat`（可临时加日志），会看到重复次数自动变大；换成大 matmul，次数自动变小。

**预期结果**：`do_bench` 返回一个稳定的毫秒数；同形状多次调用波动很小。具体数值「待本地验证」（取决于你的 GPU）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `do_bench` 在测量前要先 `fn()` 预跑一次？
**答案**：第一次调用会触发 kernel 编译（TileLang/CuTe 都是 JIT），这步可能耗时数秒。预跑把编译开销挡在测量之外，避免污染结果。

**练习 2**：`do_bench` 测量循环里的 `cache.zero_()` 被注释了，意味着测出的是「冷启动」还是「热稳态」性能？
**答案**：热稳态。warmup 之后 L2 已被填充，测量的是缓存命中后的稳态吞吐；这与「每次都 flush」的冷启动测法不同，对比不同实现时要确保都用同一套测法。

---

### 4.2 正确性校验：check_close 与 print_debug

#### 4.2.1 概念说明

生成 kernel 的输出必须与「参考实现」逐元素对齐。`bench_utils.py` 提供两个校验函数：

- `check_close(o, O_ref, rtol, atol)`：返回布尔值，适合放在 `assert` 里做「通过/失败」的硬判定。
- `print_debug(o, O_ref, rtol, atol, save_file)`：只打印、不返回布尔判定（它内部调了 `torch.allclose` 打印，但函数本身不返回），并给出**最大绝对误差**、**最大相对误差**及其位置，适合定位「哪里、差多少」。

两者的核心区别：`check_close` 是断言式把关，`print_debug` 是诊断式排错。在本项目的 `do_bench_*` 入口里，几乎都用 `print_debug` 打前向/反向误差，让你一眼看到最大误差点和占比；少数测试（如 `test_mamba_simple_gla`）用 `check_close` 做硬断言。

#### 4.2.2 核心流程

`check_close` 的判定逻辑：

1. 算绝对误差 \( |o - O_{\text{ref}}| \)。
2. 算相对误差 \( |o - O_{\text{ref}}| / (|O_{\text{ref}}| + 10^{-6}) \)（分母加 \(10^{-6}\) 防 0 除）。
3. 「绝对误差 < atol **或** 相对误差 < rtol」即视为通过。
4. 统计不通过元素占比；若失败，打印前 10 个出错位置（含参考值与实测值）。

`print_debug` 的诊断逻辑：

1. 用 `torch.isclose`（同样的 or-语义）算不通过数量与占比。
2. 找**最大绝对误差**及其多维下标（用 `torch.unravel_index` 把一维 argmax 还原成多维索引），打印该位置的参考值与实测值。
3. 找**最大相对误差**及其位置（注意：这里分母是 \( |O_{\text{ref}}| \)，**没有**加 epsilon，若参考值含 0 可能出现 `inf`，读输出时要当心）。
4. 可选 `save_file=True`，把参考张量和实测张量分别落盘成 `o_ref.txt` / `o.txt`，供离线对比。

#### 4.2.3 源码精读

`check_close` 的核心判定（注意是「或」语义、分母带 epsilon）：

[attention_engine/benchmark/bench_utils.py:L133-L155](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L133-L155) — 计算 absolute_error / relative_error，`tolerance_check = (abs_err < atol) | (rel_err < rtol)`，失败时打印出错元素的占比与前 10 个索引，返回 `torch.all(tolerance_check)`。

`print_debug` 的诊断（最大绝对误差 + 最大相对误差 + 落盘）：

[attention_engine/benchmark/bench_utils.py:L158-L192](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L158-L192) — 用 `torch.isclose` 统计不通过数量；`(o-O_ref).abs().max()` 找最大绝对误差并用 `unravel_index` 定位；`((o-O_ref).abs()/O_ref.abs()).max()` 找最大相对误差；`save_file=True` 时写 `o_ref.txt`/`o.txt`。

两个函数有一个值得注意的细节差异：`check_close` 的相对误差分母加了 `1e-6` 保护，而 `print_debug` 的最大相对误差分母是裸的 `O_ref.abs()`。所以当参考实现里有 0 元素时，`print_debug` 打印的「Max rel diff」可能是 `inf`——这是观察输出时必须知道的，不要误判成「数值炸了」。

#### 4.2.4 代码实践

**实践目标**：用 `print_debug` 对比一个「正确 kernel」与一个「人为引入误差的 kernel」，学会读它的诊断输出。

**操作步骤**（示例代码，非项目原有）：

```python
# 示例代码：理解 print_debug 的输出含义
import torch
from benchmark.bench_utils import print_debug

torch.manual_seed(0)
O_ref = torch.randn(2, 64, 8, 64, device="cuda", dtype=torch.float16)
o = O_ref.clone()
o[0, 10, 3, 20] += 0.5   # 人为制造一个明显误差点

print_debug(o, O_ref, rtol=1e-3, atol=1e-3)
```

**需要观察的现象**：
- 输出会显示 `Max diff: 0.5 at index (tensor(0), tensor(10), tensor(3), tensor(20))`，正好对应你制造误差的位置。
- 「elements are not close」的百分比可能很小（只有一个点错），说明误差集中。
- 若把 `O_ref` 某处置 0，`Max rel diff` 一行可能打印 `inf`——印证 4.2.3 提到的「裸分母」行为。

**预期结果**：`print_debug` 能精确定位最大误差点。具体百分比「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `check_close` 的相对误差分母要加 `1e-6`，而 `print_debug` 的不加？
**答案**：`check_close` 要对每个元素做布尔判定，若参考值含 0、不加 epsilon 会出现 `inf < rtol` 永远为假，导致本应通过的 0 附近元素误判失败；`print_debug` 只是「找最大值」做展示，0 附近产生 `inf` 不影响定位绝对误差，故未加保护——但读输出时要意识到这点。

**练习 2**：`check_close` 的通过条件是「绝对误差 < atol **与** 相对误差 < rtol」还是「**或**」？这对校验意味着什么？
**答案**：是「**或**」（代码里是 `|`）。意味着：真值大时靠 rtol 放过、真值接近 0 时靠 atol 放过。这与 `torch.isclose` 的语义一致，是数值校验的惯例。

---

### 4.3 参考实现选择：用哪个 ground truth 对齐

#### 4.3.1 概念说明

校验的前提是有一个可信的「参考实现」。难点在于：**不同注意力变体对应不同的参考实现**，且并非每种都有现成的高质量库。

- **标准 softmax（MHA/GQA 训练与解码）**：参考实现是 `flash-attn`（fa2 通用版）和 Hopper 上的 `flash_attn_interface`（fa3）。`do_bench_attention` 会优先尝试 fa3、再用 fa2，fa2 不可用时回退到一个**纯 torch softmax**。
- **sigmoid attention**：标准 flash-attn 不支持，需要专门的 `flash_sigmoid` fork（`do_bench_sigmoidattn`）。
- **relu attention / retention（transformer 式）**：没有库支持，参考实现是项目里手写的**纯 torch einsum** 程序（`do_bench_reluattn`/`do_bench_retention` 里的 `ref_program`）。
- **线性注意力**：参考实现来自 [flash-linear-attention](https://github.com/sustcsonglin/flash-linear-attention)（`fla.ops.*`）和 [mamba_ssm](https://github.com/state-spaces/mamba)，分别对应 `do_bench_retention_linear`、`do_bench_simple_gla`、`do_bench_mamba`。

选错参考实现（比如拿标准 softmax 去对齐 sigmoid attention）会得到「100% 不通过」的假错误，所以这一步至关重要。

另一个值得注意的工程差异：**计时器也不一样**。`do_bench_attention` 用的是 `bench_utils.py` **本模块内**定义的 `do_bench`（4.1 那个）；而 `do_bench_reluattn`、`do_bench_retention_linear`、`do_bench_simple_gla`、`do_bench_mamba` 等线性/自定义变体都 `from tilelang.profiler import do_bench`，用的是 TileLang 自带的计时器。两者测法略有差别，跨变体比较延迟时要心里有数。

#### 4.3.2 核心流程

以 `do_bench_attention` 为例，一个完整入口的流程是：

1. **算 FLOPs 预算**：根据 `(B, H, S, D, DV)` 与 `causal` 算出前向 `tflops`、反向 `bwd_tflops`，用于把延迟换算成吞吐。
2. **构造随机输入**：Q/K/V 用固定种子（`torch.cuda.manual_seed(0)`）生成，保证可复现。
3. **跑被测 kernel**：`o = attn(q, k, v)`；若 `require_grad`，再 `o.backward(do)`，并先保存 `dQ/dK/dV` 再清空 `.grad`（为后续与参考的反向对比做准备）。
4. **跑参考实现**：尝试 fa3 → 尝试 fa2（含纯 torch 回退）→ `print_debug(o, o_ref)` 对齐前向；若 `require_grad`，再对比 `dQ/dK/dV`。
5. **计时**：对被测、参考分别 `do_bench(run)`，打印 `latency` 与 `tflops`，前向/反向/fa3 各一组。

#### 4.3.3 源码精读

FLOPs 预算（causal 时整体乘 0.5，因为下三角只有一半元素）：

[attention_engine/benchmark/bench_utils.py:L1185-L1188](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L1185-L1188) — 前向 `tflops = 2*B*H*seqlenq*S*D + 2*B*H*seqlenq*S*DV`，反向 `bwd_tflops = 4*B*H*S*S*DV + 6*B*H*S*S*D`，causal 时乘 0.5。

参考实现 fa3 的尝试（Hopper 专用，需要把 head_dim 补齐到 64/128/256 之一）：

[attention_engine/benchmark/bench_utils.py:L1214-L1219](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L1214-L1219) — 尝试 `from flash_attn_interface import flash_attn_func`，失败则置 `flash_attn_func_hopper = None`、`enable_fa3 = False`，后续 fa3 分支被整体跳过。

参考实现 fa2 的尝试 + 纯 torch 回退（这是「无 flash-attn 也能跑」的关键）：

[attention_engine/benchmark/bench_utils.py:L1247-L1275](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L1247-L1275) — 尝试 `from flash_attn import flash_attn_func`；`except` 里**就地定义**一个手写 softmax（`einsum` 算 scores、`tril` 做因果掩码、`F.softmax`、再 `einsum` 算输出），保证即使没装 flash-attn 也有参考实现。

前向校验 + 反向校验（`print_debug` 同时打 `o`、`dQ`、`dK`、`dV`）：

[attention_engine/benchmark/bench_utils.py:L1300-L1306](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L1300-L1306) — `o_ref = fa2(...)` 后 `print_debug(o, o_ref)`；若 `require_grad`，先 `o_ref.backward(do)`，再分别 `print_debug(query.grad, dQ)` 等对比反向梯度。

计时与吞吐打印（注意这里用的是**本模块**的 `do_bench`，不是 `tilelang.profiler.do_bench`）：

[attention_engine/benchmark/bench_utils.py:L1340-L1345](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L1340-L1345) — 被测 `latency = do_bench(run, warmup=50, rep=100)`、参考 `latency_ref = do_bench(run_ref, ...)`，各自打印 `ms` 与 `tflops/latency*1e-9`。

对比一下「没有现成库」时的处理——`do_bench_reluattn` 直接在函数内手写一个纯 torch 参考程序：

[attention_engine/benchmark/bench_utils.py:L979-L984](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L979-L984) — relu 注意力的 `ref_program`：`einsum` 算 qk → 除以 √D → `F.relu` → `einsum` 算输出。这是「无第三方库可用」时的标准做法。

#### 4.3.4 代码实践

**实践目标**：跑通 `mha.py`，对比生成 kernel 与 flash-attn（fa2/fa3）的前向吞吐与误差，判断是否在可接受范围。

**操作步骤**：

1. 按 u1-l2 配好环境（`PYTHONPATH`、`LD_PRELOAD`、TileLang 编译）。
2. 运行 `attn_script/mha.py`，它结尾会调用：

[attn_script/mha.py:L119-L120](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L119-L120) — `from benchmark.bench_utils import do_bench_attention` 后 `do_bench_attention(mod, B, H, S, D, DV, dtype=dtype, require_grad=True)`。

3. 关注终端输出里的两类信息：
   - **正确性**：`print_debug` 打印的 `Max diff`、`percentage not close`（前向 o、以及 dQ/dK/dV 各一组）。
   - **性能**：`tl: X ms / tflops`、`flash: X ms / tflops`、`flash fa3: X ms / tflops`（若装了 fa3）。

**需要观察的现象**：
- 前向 `percentage not close` 应该很低（fp16 下通常 < 1%，`Max diff` 在 1e-2 量级以内）。
- `tl` 与 `flash` 的 tflops 应在同一量级（AttentionEngine 的卖点是「对标 FlashAttention」），tl 不应明显慢于 fa2。
- 反向 dQ/dK/dV 的误差通常比前向略大，但仍应在 rtol/atol 范围内。

**预期结果**：前向误差占比很低、tflops 与 flash-attn 相当。具体数值「待本地验证」（依赖硬件与 flash-attn 版本）。若 `percentage not close` 很高，多半是降级/模板层有 bug，应回到 u2/u3 定位。

#### 4.3.5 小练习与答案

**练习 1**：如果你新实现了一种「gated attention」（score = scale · (q@k) · gate），`do_bench_attention` 里的 fa2 回退参考能直接用吗？为什么？
**答案**：不能直接用。fa2 回退参考是**标准 softmax**（`F.softmax(scores*scale)`），不含 gate 变换，拿它对齐 gated attention 会全部不通过。应仿照 `do_bench_reluattn` 在函数内手写一个等价的纯 torch 参考程序（`einsum` → 缩放 → 乘 gate → softmax → `einsum`）。

**练习 2**：`do_bench_attention` 用的 `do_bench` 和 `do_bench_retention_linear` 用的 `do_bench` 是同一个吗？
**答案**：不是。前者用本文件定义的 `do_bench`（4.1 讲的那个，L14），后者 `from tilelang.profiler import do_bench` 用的是 TileLang 自带计时器。两者都基于 CUDA Event + warmup，但 L2 flush 等细节不同，跨变体比较延迟时要意识到这一点。

---

## 5. 综合实践

**任务**：为你在 u5-l5 即将实现的「自定义注意力」（如 relu attention 或带 bias 的线性注意力），**挑选并接入合适的基准与校验入口**，完成一次完整的前向 + 反向对齐。

要求：

1. **选参考实现**：判断你的注意力属于哪一类——是 softmax 族（用 `do_bench_attention`）、sigmoid 族（`do_bench_sigmoidattn`）、纯逐元素族（仿 `do_bench_reluattn` 手写 `ref_program`）、还是线性注意力族（`do_bench_retention_linear`/`do_bench_simple_gla`/`do_bench_mamba`）。
2. **跑校验**：用 `print_debug` 检查前向 `o` 与反向 `dQ/dK/dV`，记录 `Max diff` 与 `percentage not close`。若误差过大，先用 u5-l6 的分层定位法判断错误在 IR、降级还是模板层。
3. **跑性能**：用对应的 `do_bench_*` 打印 tl 与参考实现的 `tflops`，评估是否达到「对标参考实现」的目标。
4. **改 rtol/atol 复跑**：把 `rtol`/`atol` 从默认的 `1e-3` 放宽到 `1e-2` 再收紧到 `1e-4`，观察 `percentage not close` 的变化，建立对你 kernel 精度的直觉。

**交付**：一段表格，记录前向/反向的 `Max diff`、`percentage not close`、tl 与参考的 `tflops`，并给出「是否可接受」的判断。

## 6. 本讲小结

- `do_bench` 用 **CUDA Event** + `synchronize` 解决异步计时问题，用「估算→自适应 warmup/repeat 次数」适应快慢 kernel，并用 256MB 缓冲做 L2 flush；但测量循环里的逐次 flush 被注释，测的是热稳态性能。
- TFLOPS 由 \( F / t_{\text{ms}} \times 10^{-9} \) 换算，前向/反向 FLOPs 各有固定公式，causal 时乘 0.5。
- `check_close` 是断言式硬判定（返回布尔、分母带 epsilon）、`print_debug` 是诊断式排错（定位最大绝对/相对误差点、可落盘），两者都是「绝对误差 < atol **或** 相对误差 < rtol」的 or-语义。
- 参考实现要按注意力类型选：softmax→flash-attn（fa2/fa3，含纯 torch 回退）、sigmoid→flash_sigmoid、relu/retention→手写 torch einsum、线性注意力→fla/mamba_ssm。
- `do_bench_attention` 用本模块的 `do_bench`，而线性/自定义变体用 `tilelang.profiler.do_bench`，跨变体比延迟时要留意测法差异。

## 7. 下一步学习建议

- 掌握了「怎么测、怎么校验」之后，u5-l5 会带你**综合实战**：从零实现一种新注意力并走通「设计 online_func → 四段降级 → 前向反向对齐」全流程，本讲的 `do_bench_*` 与 `print_debug` 是那场实战的验收工具。
- 如果在校验中发现误差过大，直接进入 u5-l6（测试与调试技巧）：学会把错误定位到 IR 层、降级层还是模板层，并利用 `code_hash` 缓存命中机制与导出生成代码来调试。
- 对「性能不达标」的情况，结合 u5-l3（autotuner 与硬件建模）理解 `tune=True`/`tune_file` 如何让 TileLang 自动搜索 tile/stages 配置，把 tflops 提上去。
