# 环境搭建与运行第一个 softmax attention

## 1. 本讲目标

学完本讲后，你应该能够：

- 按照官方文档完成 AttentionEngine 的依赖安装与 `PYTHONPATH` / `LD_PRELOAD` 等环境变量配置。
- 跑通 `attn_script/mha.py`，理解一次「定义注意力 → 生成 kernel → 调用 → 反向」的最小调用流程。
- 理解 `meta_tensor` 作为「形状元信息占位」的作用，并能解释它**为什么不是** PyTorch 的 meta device 张量。

本讲承接 [u1-l1](./u1-l1-project-overview.md)：你已经知道 AttentionEngine 是一个「编译式注意力框架」，本讲就带你把这套编译链在本机真正跑起来。

## 2. 前置知识

在开始前，建议你具备以下基础：

- **Python 包与 `PYTHONPATH`**：`PYTHONPATH` 告诉 Python 解释器「除了已安装的包，还要去哪些目录找模块」。AttentionEngine 把自身源码以「目录路径」形式挂载进 `PYTHONPATH`，而不是 `pip install`，所以这一步是跑通的关键。
- **CUDA 与 PyTorch**：AttentionEngine 生成的 kernel 运行在 NVIDIA GPU 上，需要与本机 CUDA 驱动匹配的 PyTorch。本讲所有「实际运行」步骤都需要一张 NVIDIA GPU（官方在 H100 上测试）。
- **注意力计算的形状**：标准的 transformer 注意力有三个输入 `q`、`k`、`v`，本讲用 `B`（batch）、`H`（head 数）、`S`（序列长度）、`D`（head 维度）、`DV`（value 的 head 维度）来描述它们的形状。
- **在线 softmax（online softmax）**：分块累加的 softmax 算法，不必物化整张 scores 矩阵。这是 `mha.py` 里 `OnlineSoftmax` 类描述的逻辑，本讲只要求你「认识它」，详细原理留给后续讲义。

> 提醒：如果你的机器没有合适的 NVIDIA GPU，本讲中「运行 mha.py 并记录延迟」的部分将无法复现，相关数值结果会标注为「待本地验证」。但「理解源码」和「实例化 `meta_tensor`」这两类实践不需要 GPU，可以照常完成。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/README.md) | 项目说明、安装步骤、Quick Start 示例 |
| [attn_script/mha.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py) | 跑通因果 softmax attention 的标准样例脚本 |
| [attention_engine/core/utils.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/utils.py) | `meta_tensor` 占位类的定义 |
| [attention_engine/attn_engine/attn_engine.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py) | `AttentionEngine` 引擎入口，消费 `qkv_meta`、编译并返回可调用模块 |
| [attention_engine/benchmark/bench_utils.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py) | `do_bench_attention` 性能基准与正确性校验 |

心智地图：你写的 Python 描述 → `AttentionEngine(...)` 在构造时把它**编译成 GPU kernel** → 返回的 `mod` 像普通 PyTorch 算子一样 `mod(q,k,v)` 调用、`.backward()` 反向。本讲的重点是「把这条链路跑通」，至于编译内部细节，留给第二、三单元。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**① 安装与环境变量**、**② `meta_tensor` 形状元信息**、**③ 运行 `mha.py`**。

### 4.1 安装与环境变量

#### 4.1.1 概念说明

AttentionEngine 不是一个普通的「`pip install` 包」。它由两部分组成：

- **框架本体**：仓库里的 `attention_engine/` 目录（Python 源码，负责编译/降级/模板）。
- **第三方依赖 TileLang**：以 git submodule 形式放在 `3rd_parties/tilelang`，需要从源码编译——它是真正把生成的 TileLang 代码编译成 GPU kernel 的后端。

因此安装的核心是：① 准备 CUDA + PyTorch 环境；② 带子模块克隆仓库；③ 编译 TileLang；④ 用 `PYTHONPATH` 把「框架本体」和「TileLang」都挂载进 Python 的模块搜索路径。

#### 4.1.2 核心流程

```
准备 CUDA 12.4 + PyTorch（或用官方推荐 docker 镜像）
        │
        ▼
git clone --recursive  （连同 submodule 一起拉取）
        │
        ▼
cd 3rd_parties/tilelang && 按官方文档从源码构建
        │
        ▼
export PYTHONPATH="<repo>/attention_engine:<repo>/3rd_parties/tilelang:$PYTHONPATH"
export LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libcuda.so
```

#### 4.1.3 源码精读

安装与导出环境变量的官方步骤见 README 的 Installation 段：

[README.md:32-52](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/README.md#L32-L52) — 官方安装步骤，涵盖 docker 镜像、`--recursive` 克隆、TileLang 源码构建，以及两条关键的 `export`。

其中最关键的两行环境变量：

```bash
export PYTHONPATH="$(pwd)/attention_engine:$(pwd)/3rd_parties/tilelang:$PYTHONPATH"
export LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libcuda.so
```

要点解释：

- `PYTHONPATH` 里加入 `$(pwd)/attention_engine`：这让仓库根目录下的 `attention_engine/` 目录成为模块搜索根。于是 `attention_engine/attn_engine/`、`attention_engine/core/`、`attention_engine/benchmark/`、`attention_engine/autotuner/` 这些子目录就能被 `import attn_engine`、`import core`、`import benchmark`、`import autotuner` 直接命中——这正是 `mha.py` 里那些「看起来很奇怪」的 import 能工作的原因。
- `LD_PRELOAD` 预加载 `libcuda.so`：让进程在启动时就绑定 CUDA 驱动库，避免 TileLang 编译/运行时找不到 CUDA 驱动符号。

> 另外注意 README 里的一条迁移提示：项目正在迁移到更新版本的 TileLang，部分示例可能需要安装 `smallscientist1/tilelang` 的 `attnengine_upstream_new` 分支。如果默认 TileLang 跑示例报错，可以回看这条提示。

#### 4.1.4 代码实践

**实践目标**：在不动 GPU 的前提下，验证环境变量配置是否让框架本体可被导入。

**操作步骤**：

1. 在仓库根目录执行 README 给出的两条 `export`（请把 `$(pwd)` 理解为仓库根目录）。
2. 用一行 Python 检查模块能否被找到（不需要 GPU）：
   ```bash
   python -c "import attn_engine; import core; import core.utils; print('ok')"
   ```

**需要观察的现象**：

- 若 `PYTHONPATH` 配置正确，会打印 `ok`。
- 若配置错误，会抛出 `ModuleNotFoundError: No module named 'attn_engine'` 之类。

**预期结果**：`PYTHONPATH` 包含 `attention_engine/` 目录后，导入成功。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `PYTHONPATH` 里的 `attention_engine` 这一段删掉，`import attn_engine` 会发生什么？为什么？

**参考答案**：会抛 `ModuleNotFoundError`。因为 `attn_engine` 包实际位于 `attention_engine/attn_engine/`，只有把 `attention_engine/` 加进模块搜索路径，`attn_engine` 才能作为顶层包被找到。

**练习 2**：`LD_PRELOAD` 这一步如果省略，最可能在哪个环节出问题？

**参考答案**：在 TileLang 编译或运行生成的 GPU kernel、加载 CUDA 驱动符号时可能报找不到 CUDA 驱动相关的错误；预加载 `libcuda.so` 是为了让 CUDA 驱动符号在进程启动时就绑定好。

---

### 4.2 `meta_tensor` 形状元信息

#### 4.2.1 概念说明

`AttentionEngine` 在构造时需要知道 `q`、`k`、`v` 的形状与数据类型，但它**不需要真实的张量数据**——因为构造阶段只是在「编译/生成代码」，还没有要计算的数据。于是框架需要一个轻量的「形状占位符」，这就是 `meta_tensor`。

一个**非常关键、容易误解**的点：这个 `meta_tensor` **不是** PyTorch 的 meta device 张量。它是一个纯 Python 的小类，只记录形状参数和关键字参数。理解这一点，能避免你到处去找 `torch.device("meta")` 的用法。

#### 4.2.2 核心流程

```
meta_tensor(B, H, S, D, dtype=torch.float16)
        │   把位置参数原样存到 self.shape，关键字存到 self.kargs
        ▼
   只是一个「形状描述对象」，不分配显存、不持有数据
        │
        ▼
   三个 meta_tensor 组成 qkv_meta 元组，传给 AttentionEngine(...) 做编译
```

特别地，因为它只存字符串/数字，所以它甚至可以接受**字符串形状**（用于动态形状场景）：

```
meta_tensor("B", "H", "S", D, dtype=dtype)   # 形状维度用字符串占位 → 动态形状
```

#### 4.2.3 源码精读

`meta_tensor` 的定义非常短，值得完整读一遍：

[attention_engine/core/utils.py:40-48](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/utils.py#L40-L48) — `meta_tensor` 类：构造时把位置参数存进 `self.shape`，关键字存进 `self.kargs`，并通过 `dtype` 属性读出 `kargs["dtype"]`。

```python
class meta_tensor:
    def __init__(self, *args, **kargs):
        self.shape = args
        self.kargs = kargs

    @property
    def dtype(self):
        return self.kargs["dtype"]
```

可以看到：它没有任何显存分配，`shape` 只是「把传入的位置参数打包成一个 tuple」。这就是它能同时接受整数维度和字符串维度的原因。

而它「不是 torch meta 张量」的最直接证据，就在它上方那段**被注释掉**的旧实现：

[attention_engine/core/utils.py:37-38](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/utils.py#L37-L38) — 注释掉的旧版本 `return torch.empty(*args, **kargs, device="meta")`，说明早期确实用过 torch 的 meta device，但当前实现已改为纯 Python 占位类。

再看 `mha.py` 里如何用它构造 `qkv_meta`（注意它既演示了静态形状，也演示了动态形状分支）：

[attn_script/mha.py:88-101](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L88-L101) — 用 `meta_tensor` 构造 `(q_meta, k_meta, v_meta)` 三元组；`dynamic_shape=True` 时传入字符串 `"B","H","S"`，否则传入真实整数 `B,H,S`。

```python
if dynamic_shape:
    qkv_meta = (
        meta_tensor("B", "H", "S", D, dtype=dtype),
        meta_tensor("B", "H", "S", D, dtype=dtype),
        meta_tensor("B", "H", "S", DV, dtype=dtype),
    )
else:
    qkv_meta = (
        meta_tensor(B, H, S, D, dtype=dtype),
        meta_tensor(B, H, S, D, dtype=dtype),
        meta_tensor(B, H, S, DV, dtype=dtype),
    )
```

这个三元组随后整体作为第一个参数 `qkv_meta` 传给 `AttentionEngine(...)`，引擎在编译阶段从中读取形状/类型来生成对应 kernel（具体如何消费形状，会在 [u3-l3](./u3-l3-engine-entry-dispatch-cache.md) 详讲）。

#### 4.2.4 代码实践

**实践目标**：亲手实例化 `meta_tensor`，验证它只是一个纯 Python 形状占位对象（本实践不需要 GPU）。

**操作步骤**：

```python
# 在仓库根目录，确保已 export PYTHONPATH 包含 attention_engine
from core.utils import meta_tensor

m = meta_tensor(1, 128, 2048, 128, dtype="float16")
print("shape =", m.shape)      # 期望: (1, 128, 2048, 128)
print("dtype =", m.dtype)      # 期望: float16

# 验证它能接受「字符串维度」（动态形状占位）
md = meta_tensor("B", "H", "S", 128, dtype="float16")
print("dynamic shape =", md.shape)  # 期望: ('B', 'H', 'S', 128)
```

**需要观察的现象**：

- `m.shape` 就是你传入的位置参数组成的 tuple。
- `md.shape` 里出现了字符串，证明它对维度值不做任何数值约束。

**预期结果**：`meta_tensor` 既不分配显存也不依赖 CUDA；字符串维度被原样保留。

#### 4.2.5 小练习与答案

**练习 1**：为什么构造 `AttentionEngine` 时用 `meta_tensor` 而不是直接传一个真实的 `torch.randn(...)` 张量？

**参考答案**：构造阶段只做「代码生成/编译」，只需要形状和数据类型信息；用真实张量会无谓地分配显存、绑定具体数据。`meta_tensor` 是一个零开销的形状描述符，恰好满足「只描述、不分配」的需求。

**练习 2**：如果调用 `meta_tensor(1, 128, 2048, 128)`（不传 `dtype`），随后访问 `.dtype` 会发生什么？为什么？

**参考答案**：会抛 `KeyError: 'dtype'`。因为 `dtype` 属性实现为 `return self.kargs["dtype"]`，未传 `dtype` 时 `kargs` 里没有这个键。使用时必须显式传入 `dtype=...`。

---

### 4.3 运行 `mha.py`

#### 4.3.1 概念说明

`attn_script/mha.py` 是贯穿全册的「标准 softmax 参照样例」。它把本单元前面讲的几个组件（`score_mod`、`mask_mod`、`OnlineSoftmax`、`CustomIO`）组装起来，构造一个 `AttentionEngine`，得到一个可调用的 `mod`，再用 `do_bench_attention` 同时做「正确性校验」和「性能基准」。

这一节的目标不是让你立刻读懂 `OnlineSoftmax` 的每行符号代码（那是 [u2-l6](./u2-l6-lower-online-func.md) 的任务），而是让你看清**整条调用链的骨架**：

```
定义 score_mod / mask_mod / OnlineSoftmax / CustomIO
        │
        ▼
qkv_meta = (meta_tensor(...), meta_tensor(...), meta_tensor(...))
        │
        ▼
mod = AttentionEngine(qkv_meta, custom_fwd_inputs, score_mod, mask_mod, online_func, tune=..., ...)
        │   ↑ 构造时完成「编译」，生成并加载 GPU kernel
        ▼
do_bench_attention(mod, B, H, S, D, DV, ...)   # 校验正确性 + 测延迟/tflops
```

#### 4.3.2 核心流程

`mha.py` 的 `__main__` 大致顺序为：

1. 设定形状 `B, H, S, D, DV = 1, 128, 2048, 128, 128` 与 `dtype`。
2. 用 `meta_tensor` 构造 `qkv_meta`（含动态形状开关）。
3. 构造空的 `CustomIO({})`（本例没有额外自定义输入）。
4. 实例化 `OnlineSoftmax()`。
5. 构造 `mod = AttentionEngine(...)`——**这一步触发编译**。
6. 调用 `do_bench_attention(mod, ...)`：内部会构造真实 `q/k/v`，跑前向（和反向），与 flash-attn 参考实现比对，并打印延迟与 tflops。

其中「算力（tflops）」的估算来自标准注意力前向的浮点运算量。对因果（causal）注意力，由于只算下三角，运算量约为非因果的一半。设前向的浮点运算数为 \(F\)，延迟为 \(t\)（毫秒），则吞吐（TFLOPS）为：

\[
\text{TFLOPS} = \frac{F}{t \times 10^{-3}} \times 10^{-12}
            = \frac{F}{t} \times 10^{-9}
\]

这正是基准代码里 `tflops / latency * 1e-9` 的来历（`latency` 单位为毫秒）。前向 \(F\) 取：

\[
F = 2 \cdot B \cdot H \cdot S_q \cdot S \cdot (D + DV), \quad \text{causal 时再} \times 0.5
\]

其中两项分别对应 `Q@K^T`（乘加算 2 次浮点）与 `P@V`。

#### 4.3.3 源码精读

先看 `mha.py` 顶部的导入，注意这些 import 之所以成立，全靠 4.1 讲的 `PYTHONPATH`：

[attn_script/mha.py:1-8](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L1-L8) — 从 `attn_engine`、`core`、`core.utils` 导入 `AttentionEngine`、`OnlineFunc`、`CustomIO`、符号类型与 `meta_tensor`。

```python
from attn_engine import AttentionEngine
from attn_engine import OnlineFunc
from core import CustomIO
from core import SymbolicArray, SymbolScalar, SymbolicTensor
from core import Var
from core.utils import meta_tensor
```

再看 `__main__` 的形状设定与 `AttentionEngine` 构造：

[attn_script/mha.py:86-117](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L86-L117) — 设定形状、构造 `qkv_meta` 与 `CustomIO`，并构造 `mod`（`tune=True` 表示启用自动调优）。

```python
B, H ,S, D, DV = 1,128,2048,D, 128
...
mod = AttentionEngine(
    qkv_meta,
    custom_fwd_inputs, score_mod=score_mod, mask_mod=causal_mask,
    online_func=online,
    tune=True, tune_file="attn_tl.json",
    tune_bwd=True,
    tune_file_bwd="attn_tl_bwd.json",
    infer_mask=False if dynamic_shape else True,
)
```

这里出现的构造参数，对应 `AttentionEngine.__init__` 的真实签名：

[attention_engine/attn_engine/attn_engine.py:109-115](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L109-L115) — `AttentionEngine` 构造函数签名，可见 `tune`、`tune_file`、`tune_bwd`、`tune_file_bwd`、`infer_mask`、`backend`（默认 `"tl"`）等参数。

```python
def __init__(self, qkv_meta, custom_fwd_inputs, score_mod, mask_mod,
             online_func, mask_value="-inf", device=H100(), backend="tl",
             tune=False, tune_file="",
             tune_bwd=False, tune_file_bwd="",
             infer_mask=True, infer_mask_block_M=128, infer_mask_block_N=128,
             extern_block_mask=False,
             kv_shared=False):
```

可以看到：默认 `backend="tl"`（TileLang 后端）、`device=H100()`。本讲的 `mha.py` 没有显式传 `backend`，因此走默认的 TileLang 路径。

最后是基准与校验调用：

[attn_script/mha.py:119-120](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L119-L120) — 调用 `do_bench_attention` 同时完成正确性比对（与 flash-attn）和性能测量。

```python
from benchmark.bench_utils import do_bench_attention
do_bench_attention(mod, B, H, S, D, DV, dtype=dtype, require_grad=True)
```

`do_bench_attention` 内部会：构造真实 `q/k/v`、跑 `mod(q,k,v)`、与 flash-attn（fa2，可选 fa3）参考输出做 `print_debug` 误差比对，再用 `do_bench` 测延迟并按上面的公式打印 tflops。它的输出形如：

[attention_engine/benchmark/bench_utils.py:1340-1345](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L1340-L1345) — 分别打印生成 kernel（`tl:`）与参考（`flash:`）的延迟（ms）和吞吐（tflops）。

```python
latency = do_bench(run, warmup=50, rep=100)
print("tl: {:.2f} ms".format(latency))
print("tflops: {:.2f}".format(tflops / latency * 1e-9))
latency_ref = do_bench(run_ref, warmup=50, rep=100)
print("flash: {:.2f} ms".format(latency_ref))
print("tflops: {:.2f}".format(tflops / latency_ref * 1e-9))
```

> 提示：README 的 Quick Start 段给了一个更「极简」的版本（`S=32768`、直接 `out = mod(q,k,v)` + `out.backward(...)`，不调用 `do_bench_attention`），适合用来理解「最小调用」；而 `attn_script/mha.py` 是带基准和正确性校验的完整版本（`S=2048`）。两者描述注意力的部分（`score_mod`/`OnlineSoftmax`/`causal_mask`）一致。Quick Start 见 [README.md:54-164](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/README.md#L54-L164)。

#### 4.3.4 代码实践

**实践目标**：跑通 `mha.py`，记录延迟与 tflops，并理解形状是如何经 `qkv_meta` 传入的。（需要 NVIDIA GPU + CUDA + 已编译的 TileLang。）

**操作步骤**：

1. 按 4.1 完成 `PYTHONPATH` / `LD_PRELOAD` 导出。
2. 在仓库根目录运行：
   ```bash
   python attn_script/mha.py
   ```
3. 观察终端输出里的 `tl:` 与 `flash:` 两行延迟与 tflops，以及 `print_debug` 打印的最大误差。
4. 把 `mha.py` 里的 `S` 从 `2048` 改大（例如 `4096`），重新运行，对比 tflops 变化。

**需要观察的现象**：

- 构造 `AttentionEngine(...)` 时会有一次「编译」耗时（首次生成并编译 kernel）。
- `tl:` 行给出 AttentionEngine 生成 kernel 的延迟/吞吐；`flash:` 行给出 flash-attn 参考的延迟/吞吐。
- `print_debug` 会打印「不接近元素占比」「最大误差及其位置」等正确性信息。
- 增大 `S` 后，由于 \(F\) 随 \(S^2\) 增长而延迟增长更慢，tflops 通常会上升（更接近算力上限）。

**预期结果**：生成 kernel 的输出与 flash-attn 参考在 fp16 下误差应处于可接受范围（如最大相对误差在 1e-2 量级）；tflops 具体数值**待本地验证**（取决于你的 GPU 型号与 TileLang 版本）。

> 注意：首次运行 `tune=True` 会触发自动调优搜索，耗时较长；想快速验证连通性，可先把 `tune`、`tune_bwd` 改为 `False`（具体调优机制留到 [u5-l3](./u5-l3-autotuner-and-hw-modeling.md)）。

#### 4.3.5 小练习与答案

**练习 1**：`mha.py` 里把 `S` 从 2048 改成 4096，为什么前向 tflops 通常会升高？

**参考答案**：因果注意力的浮点量 \(F \propto S^2\)，而 kernel 执行时间随 \(S\) 的增长亚平方（受限于访存/并行度，规模越大计算密度越高），所以 \(F/t\) 的比值——即 tflops——通常随 \(S\) 增大而上升，更逼近硬件算力上限。

**练习 2**：`AttentionEngine(...)` 返回的 `mod`，在 `do_bench_attention` 里被当作普通函数 `attn(query, key, value)` 调用，还能 `.backward()`。这暗示 `mod` 本质上是什么？

**参考答案**：`mod` 本质是一个被框架「包装成可调用、可反向」的 PyTorch 模块/算子——构造阶段已把用户的符号描述编译成 GPU kernel，并挂上 autograd 逻辑，所以它能像原生 PyTorch 算子一样前向调用与反向传播。

## 5. 综合实践

**任务**：以 `mha.py` 为模板，完成一次「环境 → 编译 → 校验 → 改形状」的完整闭环，并用本讲学到的知识解释每一步。

建议步骤：

1. **配环境**：按 4.1 导出两条环境变量，并用 4.1.4 的 `python -c "import attn_engine..."` 验证框架本体可导入。
2. **理解形状入口**：在脚本里找到 `qkv_meta` 的构造处，明确 `B/H/S/D/DV` 各自进入哪一个 `meta_tensor`（参考 4.2.3）。
3. **跑通基准**：运行 `python attn_script/mha.py`，记录 `tl:` 与 `flash:` 的延迟和 tflops，以及 `print_debug` 的最大误差。
4. **改形状对比**：把 `S` 改为 4096 重跑，记录新的 tflops，验证「tflops 随 \(S\) 上升」的判断（若与你预期不符，记下实际现象，留待 [u5-l4](./u5-l4-benchmark-and-correctness.md) 深入分析）。
5. **解释一句话**：用本讲的语言说明「为什么改 `S` 只需要改 `meta_tensor` 的参数、而不需要改 `score_mod`/`OnlineSoftmax`」——答案是这些用户描述与具体长度解耦，形状只通过 `qkv_meta` 喂给编译器。

> 若无可用 GPU，第 3、4 步的实际数值无法复现，请至少完成第 1、2、5 步（这些不依赖 GPU），并把第 3、4 步标注为「待本地验证」。

## 6. 本讲小结

- AttentionEngine 不是 `pip install` 包：需要带 submodule 克隆、源码编译 TileLang，并用 `PYTHONPATH` 同时挂载 `attention_engine/` 与 `3rd_parties/tilelang`。
- `LD_PRELOAD` 预加载 `libcuda.so`，保证 CUDA 驱动符号在进程启动时绑定。
- `meta_tensor` 是一个**纯 Python 形状占位类**（只存 `shape`/`kargs`），不是 torch 的 meta device 张量；它甚至能接受字符串维度来描述动态形状。
- 三个 `meta_tensor` 组成 `qkv_meta`，是 `AttentionEngine(...)` 编译阶段的唯一形状来源。
- `attn_script/mha.py` 把 `score_mod`/`mask_mod`/`OnlineSoftmax`/`CustomIO` 组装后构造 `mod`；构造即编译，`mod` 可像普通 PyTorch 算子一样前向与反向。
- `do_bench_attention` 同时做「与 flash-attn 的正确性比对」和「延迟/吞吐测量」，tflops 由 \(F/t \times 10^{-9}\) 得到。

## 7. 下一步学习建议

本讲之后，你已经能跑通样例并理解了「形状占位 → 编译 → 调用」的骨架。建议接下来：

- 阅读 [u1-l3 目录结构与代码地图](./u1-l3-directory-and-code-map.md)：建立「某个功能落在哪个文件」的心智地图，为后续深入编译链做准备。
- 阅读 [u1-l4 用户 API 全景](./u1-l4-user-api-overview.md)：系统理解 `score_mod`/`mask_mod`/`online_func`/`CustomIO` 四个组件的签名与组合方式——本讲只是「认识」它们，下一讲会「讲透」。
- 进阶好奇：想看 `AttentionEngine(...)` 构造时究竟如何消费 `qkv_meta` 并分发到不同降级函数，可提前浏览 [u3-l3 引擎入口：分发、编译与缓存](./u3-l3-engine-entry-dispatch-cache.md)（建议在学完第二单元符号 IR 之后再深入）。
