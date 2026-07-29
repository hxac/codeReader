# 控制流、线程组与 assume 提示

## 1. 本讲目标

学完本讲，你应当能够：

- 用 `self.range(...)` 控制内核里的 `for` 循环，并通过 `unroll` 提示循环展开方式；
- 理解 `self.thread_group / single_thread / single_warp / warp_group` 如何把一个线程块里的线程「切分」成若干子组，并掌握嵌套划分与 `elect-any` 语义；
- 掌握 `self.assume(...)` 如何向编译器声明「某个内核参数能被某常数整除」，并了解这条提示如何流入 IR 元数据、影响标量分析与界感知化简。

本讲承接 [u2-l2](u2-l2-instructions-and-instruction-groups.md) 建立的「通用指令 + 硬件指令组」分层模型：控制流、线程组与 `assume` 都属于通用指令层面的能力，它们决定了「哪些线程、在什么条件下、循环多少次地」执行你写下的那些指令。

## 2. 前置知识

在阅读本讲前，请先建立以下直觉（若不熟悉，可先看 [u1-l3](u1-l3-first-kernel-vector-add.md) 与 [u2-l2](u2-l2-instructions-and-instruction-groups.md)）：

- **GPU 的 SIMT 执行模型**：一个线程块（thread block）里的所有线程默认执行同一份代码。Tilus 让你以「整个线程块做什么」的视角书写内核，但底层仍是所有线程齐头并进。
- **Script 骨架**：内核是一个继承 `tilus.Script` 的类，`__init__` 设编译期超参，`__call__` 写算子逻辑；`attrs.warps` 决定每块的 warp 数（1 warp = 32 线程），是编译期常量。
- **指令即语句**：你在 `__call__` 里调用 `self.xxx(...)` 时，转译器（transpiler）会把它翻译成一条 `InstStmt` 追加到当前函数体里。本讲要讲的「控制流」「线程组」「assume」同样是这条翻译流水线的一部分，只是它们产生的是 *结构性语句*（`ForStmt`、`ThreadGroupStmt`）或 *提示性指令*（`AssumeInst`），而非数据搬运指令。
- **`int32` 参数**：`__call__` 签名里标注为 `int32` 的参数是运行时传入的整数（如矩阵维度 `n`），它不是编译期常量；`assume` 最常见的用途就是告诉编译器「这个运行时参数满足某种整除关系」。

一个贯穿全讲的背景知识：**线程索引的绝对性**。线程块内线程按 `0, 1, 2, ...` 编号。所谓「线程组」就是从某个起始线程开始、连续取若干个线程构成的子集。本讲会反复用到「相对父组」与「绝对线程号」这两个概念。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/tilus/lang/instructions/root.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py) | 通用指令组 `RootInstructionGroup`，定义了 `range`、`thread_group`、`single_thread`、`single_warp`、`warp_group`、`assume`、`static_assert` 等本讲全部 API 的用户面。 |
| [python/tilus/lang/constructs/loops.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/constructs/loops.py) | `RangeLoop` 与 `range(...)` 工厂：把 `self.range(...)` 包装成可在 `for` 循环里使用、并能生成 `ForStmt`（含 `unroll` 提示）的迭代器。 |
| [python/tilus/lang/constructs/contexts.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/constructs/contexts.py) | `ThreadGroupContext`：`with self.thread_group(...)` 上下文管理器的薄封装，进入时压栈、退出时弹栈并生成语句。 |
| [python/tilus/ir/stmt.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/stmt.py) | IR 侧语句节点：`ForStmt`（循环）、`ThreadGroupStmt`（线程组划分）、`IfStmt`（条件）等。 |
| [python/tilus/ir/builders/stmt_builder.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/builders/stmt_builder.py) | 语句构建器：`ThreadGroupContext` 在此压入 `tg_stack` 并在退出时发出 `ThreadGroupStmt`。 |
| [python/tilus/ir/utils/thread_group_stack.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/utils/thread_group_stack.py) | `ThreadGroupStack`：维护当前线程组的绝对起止线程号、校验子组不越界、处理 `elect-any`（`thread_begin=-1`）。 |
| [python/tilus/ir/instructions/hints.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/hints.py) | `AssumeInst`：assume 提示在 IR 里的数据结构。 |
| [python/tilus/transforms/lower_assume.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_assume.py) | `lower_assume` 变换：把 `AssumeInst` 解析成参数整除性，写入函数元数据并删除该指令。 |
| [docs/source/programming-guides/thread-group.rst](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/thread-group.rst) | 官方线程组编程指南，含 producer-consumer 流水线范例。 |
| examples/quantization/per_token_cast.py | 真实使用 `self.assume(...)` 的范例。 |
| examples/hopper_matmul/matmul_v0.py | 真实使用 `with self.single_thread():` + `for ... in range(...)` 的范例。 |

---

## 4. 核心概念与源码讲解

### 4.1 控制流：range、unroll 与 for 循环

#### 4.1.1 概念说明

在 `__call__` 里写循环，有两种写法：

```python
for i in range(10):          # Python 内置 range
    ...
for i in self.range(10):     # Tilus 的 self.range
    ...
```

二者都会被转译器识别并翻译成 IR 里的 `ForStmt`。区别在于 `self.range` 多一个 `unroll` 参数，让你向编译器声明循环的展开方式：

- `unroll=None`（默认）：不附加任何展开提示；
- `unroll="all"`：完全展开；
- `unroll=n`（正整数）：按因子 `n` 展开。

为什么需要 `unroll`？展开能减少分支开销、增加指令级并行，常用于循环次数很小且体内容易并行的场景（例如对 `block_k` 维度的若干段累加）。但全展开会成倍放大代码体积，所以它是一个 *提示*，由后续 IR 变换与代码生成阶段决定如何落地。

> 小贴士：`range` / `self.range` 的 `start`、`stop`/`end`、`step` 都可以是整数或符号表达式（如 `k_size`、`blockIdx.x`）。当边界不是编译期常量时，循环就以符号 `extent` 形式保留在 IR 里，运行时再确定次数。

#### 4.1.2 核心流程

`for i in self.range(...)` 的翻译流程：

1. `self.range(...)` 返回一个 `RangeLoop` 对象（不是真的去迭代数字）；
2. 转译器遇到 `for` 语句时，识别迭代对象是 `RangeLoop`，调用其 `generate_loop_statement(loop_vars, body)`；
3. 该方法分两种情况生成 `ForStmt`：
   - **`range(stop)` 形式**（`start==0` 且 `step==1`）：直接生成 `ForStmt(iter_var, extent=stop, body, unroll_factor)`；
   - **`range(start, stop, step)` 一般形式**：生成一个内部循环变量 `i`，循环次数为 \(\lceil (\text{stop}-\text{start})/\text{step}\rceil\)，再用一条 `DeclareStmt` 把真正的循环变量初始化为 \(\text{start} + i \times \text{step}\)；
4. `unroll` 被规范化：`None→None`、`"all"→-1`、整数原样保留，存入 `ForStmt.unroll_factor`；
5. IR 打印器（`IRPrinter`）会把 `unroll_factor` 渲染成 C 风格的 `#pragma unroll` 注释，方便你用 `dump_ir` 观察。

一般形式下的循环次数（向上取整）：

\[
\text{extent} = \left\lceil \frac{\text{stop}-\text{start}}{\text{step}} \right\rceil
   = \frac{(\text{stop}-\text{start}) + (\text{step}-1)}{\text{step}}
\]

#### 4.1.3 源码精读

**用户面 API** —— [root.py:96-159](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L96-L159) 定义了 `RootInstructionGroup.range`。它只是把参数转发给 constructs 层的 `range` 工厂，并做类型标注：

```python
def range(self, start, end=None, step=None, /, *, unroll=None):
    from tilus.lang.constructs.loops import range
    return typing.cast(Iterable[Var], range(start, end, step, unroll=unroll))
```

**核心翻译逻辑** —— [loops.py:53-92](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/constructs/loops.py#L53-L92) 的 `RangeLoop.generate_loop_statement`。先规范化 `unroll`，再分两种情况：

```python
match self.unroll:
    case None:        unroll_factor = None
    case "all":       unroll_factor = -1
    case factor:      unroll_factor = factor

if start==0 and step==1:                 # range(stop)
    return ForStmt(iter_var=loop_vars[0], extent=self.stop,
                   body=body, unroll_factor=unroll_factor)
else:                                    # range(start, stop, step)
    # extent = ceil((stop-start)/step), 真正的循环变量 = start + i*step
    ...
```

**IR 节点** —— [stmt.py:40-50](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/stmt.py#L40-L50) 的 `ForStmt` 是一个 frozen dataclass，字段注释明确写了 `unroll_factor` 的三种取值含义（`None`/`-1`/`n`）。

**打印渲染** —— [printer.py:256-265](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/printer.py#L256-L265) 把 `ForStmt` 渲染成：

```
#pragma unroll          # 当 unroll_factor == -1
for i in range(extent):
    <body>
```

**真实范例** —— [examples/quantization/per_token_cast.py:72](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/quantization/per_token_cast.py#L72) 用 Python 内置 `range` 写循环；[examples/hopper_matmul/matmul_v0.py:57](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v0.py#L57) 则用 `for offset_k in range(0, k_size, block_k)` 的三参数形式遍历 K 维。

#### 4.1.4 代码实践

**实践目标**：直观感受 `unroll` 提示如何反映到 IR 文本里。

**操作步骤**：

1. 新建一个最小内核（示例代码，非项目原有文件）：

```python
# 示例代码
import tilus
from tilus import int32

class LoopUnrollDemo(tilus.Script):
    def __init__(self):
        super().__init__()

    def __call__(self, out_ptr: ~tilus.float32):
        self.attrs.blocks = 1
        self.attrs.warps = 1
        g = self.global_view(out_ptr, dtype=tilus.float32, shape=[4])
        acc = self.register_tensor(dtype=tilus.float32, shape=[1], init=0.0)
        # 对比：把 unroll 分别改成 None / "all" / 2 重新编译
        for i in self.range(4, unroll="all"):
            acc = self.add(acc, acc, out=acc)   # 仅用于产生循环体
        self.store_global(g, acc, offsets=[0])
```

2. 在调用前开启 IR 转储并指定缓存目录：

```python
tilus.option.debug.dump_ir()
tilus.option.cache_dir("/tmp/loop-unroll-demo")
```

3. 触发一次编译后，打开缓存目录里的 `ir/0_Original.txt`（dump_ir 会在每个变换前后各存一份，`0_Original.txt` 是转译后、变换前的 IR）。

**需要观察的现象**：找到对应循环的 `for` 行，其上方是否出现 `#pragma unroll`。

**预期结果**：

- `unroll="all"` → IR 文本里循环上方有 `#pragma unroll`；
- `unroll=2` → 出现 `#pragma unroll 2`；
- `unroll=None` → 没有任何 pragma 注释。

> 运行时数值正确性「待本地验证」（本例循环体仅为演示，未做有意义的计算）；本实践要确认的 deliverable 是 **IR 文本里 `#pragma unroll` 的有无与取值**，这部分由 `IRPrinter` 确定性地渲染，可稳定观察。

#### 4.1.5 小练习与答案

**练习 1**：`self.range(1, 10, 2)` 会生成几次循环？真正的循环变量在第 `i` 次取什么值？

**参考答案**：次数为 \(\lceil(10-1)/2\rceil = 5\)；第 `i` 次的值为 \(1 + 2i\)，即 1、3、5、7、9。

**练习 2**：为什么不直接用 Python 内置 `range`，而要专门提供 `self.range`？

**参考答案**：两者都能被转译成 `ForStmt`；但 `self.range` 额外携带 `unroll` 提示，能把展开意图写进 `ForStmt.unroll_factor`，而内置 `range` 没有这个口子。

---

### 4.2 线程组：thread_group / single_thread / single_warp

#### 4.2.1 概念说明

GPU 线程块默认「全员齐步走」，但高性能内核经常需要 *差异化分工*：

- **满足硬件要求**：TMA 搬运需要 warp 对齐的一组线程；WGMMA 需要一整个 warp-group（128 线程）；mbarrier 的 `arrive` 往往只需要 1 个线程去发信号。
- **并行流水线**：一组线程负责搬数据（producer），另一组负责算（consumer）。
- **避免重复劳动**：例如给 barrier 计数减一只需一个线程做，让 128 个线程都做反而会把计数减 128 次，得到错误结果。

**线程组（thread group）** 就是 Tilus 提供的「在线程块内部切分线程」的抽象。你用一个上下文管理器圈出一段代码，声明「这段只由从 `thread_begin` 起的连续 `num_threads` 个线程执行」。

四个 API 的关系：

| API | 等价于 | 含义 |
| --- | --- | --- |
| `self.thread_group(thread_begin, num_threads)` | — | 原语：从 `thread_begin` 起取 `num_threads` 个线程 |
| `self.single_thread(thread=-1)` | `thread_group(thread, 1)` | 只用 1 个线程；默认 `thread=-1` 即 elect-any |
| `self.single_warp(warp=0)` | `thread_group(warp*32, 32)` | 只用第 `warp` 个 warp（32 线程） |
| `self.warp_group(warp_begin, num_warps)` | `thread_group(warp_begin*32, num_warps*32)` | 用连续 `num_warps` 个 warp |

#### 4.2.2 核心流程

进入 `with self.thread_group(thread_begin=B, num_threads=N):` 时：

1. 校验 `B`、`N` 合法（`B>=0`、`B+N` 不超过父组大小，或 `B=-1` 走 elect-any）；
2. 把「相对父组」的 `B` 换算成「绝对线程号」：`abs_begin = parent_abs_begin + B`，`abs_end = abs_begin + N`，压入 `ThreadGroupStack`；
3. 上下文内的所有指令都「挂着」当前线程组的起止范围；
4. 退出时弹栈，并把体内语句包成一个 `ThreadGroupStmt(thread_begin=B, num_threads=N, body=...)` 追加到外层。

线程组可以**嵌套**：在父组（比如 0–63 号线程）里再开两个子组（0–31、32–63），子组的 `thread_begin` 是相对父组而言的。这一点在 producer-consumer 流水线里非常常见。

**elect-any 语义**（`thread_begin=-1`，`single_thread()` 默认走这条路）：把「具体哪个线程执行」交给硬件自由选择。`num_threads=1` 时对应 PTX 的 `elect.sync`——warp 内任意一个线程执行；`num_threads=32` 时后端可以用「统一谓词（uniform predicate）」选一个 warp，而不必发散地比较 `threadIdx / 32 == N`。要求 `num_threads` 是 2 的幂。

> 关于 `self.sync()` 的作用域：它只同步 **当前线程组** 内的线程，不是整个块。出了线程组上下文再调 `self.sync()` 才同步整块。这让你能在不打扰其它组的前提下做组内同步。

#### 4.2.3 源码精读

**用户面 API** —— [root.py:161-235](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L161-L235) 定义了 `thread_group` 与 `single_thread`。注意 `single_thread` 默认 `thread=-1` 并直接转发：

```python
def single_thread(self, thread: int = -1) -> ThreadGroupContext:
    ...
    return self.thread_group(thread_begin=thread, num_threads=1)
```

即 `single_thread()` ≡ `thread_group(-1, 1)`。`single_warp` / `warp_group`（[root.py:237-273](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L237-L273)）同理，只是把 warp 数换算成线程数。`root.py:44-62` 还提供了 `current_thread_begin / current_thread_end / current_num_threads` 三个属性，它们读取栈顶得到当前组的绝对范围。

**上下文管理器** —— [contexts.py:28-40](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/constructs/contexts.py#L28-L40) 的 `ThreadGroupContext` 把工作委托给 builder 的 `thread_group`：

```python
class ThreadGroupContext(TilusContext):
    def __init__(self, builder, thread_begin, num_threads):
        self.ctx = self.builder.thread_group(thread_begin=..., num_threads=...)
```

**真正发语句的地方** —— [stmt_builder.py:294-306](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/builders/stmt_builder.py#L294-L306)：进入时 `tg_stack.push(...)`，退出时 `tg_stack.pop()` 并把体内语句包成 `ThreadGroupStmt`。

**线程号换算与校验** —— [thread_group_stack.py:42-73](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/utils/thread_group_stack.py#L42-L73) 的 `push`。关键逻辑：父组存在时，`thread_begin` 是相对量，要加上 `parent_thread_begin` 得到绝对量；`thread_begin==-1`（elect-any）则记录为从父组起点开始，真正的选择延迟到代码生成：

```python
if thread_begin == -1:               # elect-any
    self.thread_begin.append(parent_thread_begin)
    self.thread_end.append(parent_thread_begin + num_threads)
else:                                # 显式指定
    self.thread_begin.append(parent_thread_begin + thread_begin)
    self.thread_end.append(parent_thread_begin + thread_begin + num_threads)
```

**IR 节点** —— [stmt.py:53-92](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/stmt.py#L53-L92) 的 `ThreadGroupStmt`，其文档串详细说明了 `thread_begin=-1` 的 elect-any 语义：`num_threads=1` 对应 `elect.sync`，`num_threads=32` 让后端用 uniform 谓词选 warp。

**打印渲染** —— [printer.py:267-279](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/printer.py#L267-L279) 把 `thread_begin==-1` 渲染成可读的 `elect_any`：

```
with thread_group(thread_begin=elect_any, num_threads=1):
    <body>
```

**真实范例** —— [examples/hopper_matmul/matmul_v0.py:57-72](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v0.py#L57-L72) 把「单个线程发 mbarrier 信号 + 发起 TMA 搬运」包在 `with self.single_thread():` 里，这正是避免 128 个线程重复递减 barrier 计数的典型用法：

```python
for offset_k in range(0, k_size, block_k):
    with self.single_thread():
        self.mbarrier.arrive_and_expect_tx(tma_barrier, transaction_bytes=...)
        self.tma.global_to_shared(src=ga, dst=sa, offsets=[offset_m, offset_k], mbarrier=tma_barrier)
        ...
        self.mbarrier.wait(tma_barrier, phase=phase)
    self.sync()   # 同步整块，确保共享内存数据就绪
```

官方文档 [thread-group.rst:126-155](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/thread-group.rst#L126-L155) 还给出了 producer/consumer 双 warp 用 mbarrier 协同的完整范例，建议结合阅读。

#### 4.2.4 代码实践

**实践目标**：用 `self.single_thread()` 让单个线程发起一次全局写，并用 `dump_ir` 观察生成的线程划分语句。

**操作步骤**：

1. 编写如下内核（示例代码，非项目原有文件）。思路：整块先协作算出一个标量结果，再让单个线程把它写回全局内存的某一个位置。

```python
# 示例代码
import tilus

class SingleThreadWrite(tilus.Script):
    def __init__(self):
        super().__init__()

    def __call__(self, out_ptr: ~tilus.float32):
        self.attrs.blocks = 1
        self.attrs.warps = 1            # 32 个线程
        g_out = self.global_view(out_ptr, dtype=tilus.float32, shape=[1])

        # 单个线程（elect-any）发起这次全局写
        with self.single_thread():
            r_val = self.register_tensor(dtype=tilus.float32, shape=[1], init=123.0)
            self.store_global(g_out, r_val, offsets=[0])
```

2. 开启 IR 转储并指定缓存目录：

```python
tilus.option.debug.dump_ir()
tilus.option.cache_dir("/tmp/single-thread-write")
# 触发一次编译：
import torch
out = torch.empty(1, dtype=torch.float32, device="cuda")
SingleThreadWrite()(out)
```

3. 打开缓存目录下 `ir/0_Original.txt`，定位 `store_global` 所在位置。

**需要观察的现象**：`store_global` 是否被包在一段 `with thread_group(thread_begin=elect_any, num_threads=1):` 里。

**预期结果**：

- IR 文本里出现
  ```
  with thread_group(thread_begin=elect_any, num_threads=1):
      ...register_tensor...
      ...store_global...
  ```
  这正是 `single_thread()` → `thread_group(-1, 1)` → `ThreadGroupStmt(thread_begin=-1, num_threads=1)` 的渲染结果。
- 在后续代码生成阶段，该段会被守护为「仅一个线程执行」（对应 `elect.sync` 语义的 uniform 谓词代码）。

> 运行时正确性「待本地验证」：单线程下对一个 `[1]` 标量张量做 `store_global`，其布局推理（`spatial_size` 是否落在 1 个线程上）需在你本机确认；本实践确定性可观察的 deliverable 是 **IR 里 `thread_group(... elect_any ... num_threads=1)` 这条划分语句**，以及它对 `store_global` 的包裹关系。若布局推理报错，可改为在 `single_thread` 内只放 `printf`/`mbarrier` 这类天然单线程的指令来观察同样的划分结构（参考 [hopper_matmul/matmul_v0.py:59](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v0.py#L59) 的成熟用法）。

#### 4.2.5 小练习与答案

**练习 1**：`attrs.warps = 4`（即 128 线程）时，`with self.single_warp(warp=2):` 实际由哪些绝对线程号执行？

**参考答案**：`single_warp(warp=2)` ≡ `thread_group(2*32, 32)`，即绝对线程号 64–95。

**练习 2**：`single_thread()` 默认用 `thread=-1`（elect-any），而不是固定 `thread=0`，为什么？

**参考答案**：elect-any 让硬件自由挑选一个线程，后端可发射 `elect.sync`/统一谓词代码，避免 `threadIdx == 0` 这类会引发线程发散的比较分支，更高效。

**练习 3**：在一个 `thread_group(0, 64)` 内部再写 `thread_group(0, 32)` 与 `thread_group(32, 32)`，这两个内层组的绝对线程号分别是？

**参考答案**：父组绝对范围 0–63；内层 `thread_group(0, 32)` 的 `thread_begin=0` 相对父组 → 绝对 0–31；内层 `thread_group(32, 32)` → 绝对 32–63。

---

### 4.3 assume 提示与整除性约束

#### 4.3.1 概念说明

`self.assume(cond)` 是一条 **编译期提示**，不是运行时检查。它告诉编译器「我可以保证 `cond` 为真，请据此优化」。`cond` 失败时不会有任何运行时报错（与断言不同）——你只是向编译器做了一次承诺。

当前 `assume` 唯一被消费的形式是 **参数整除性**：形如

\[
a \bmod c = 0
\]

其中 \(a\) 是某个 `int32` 内核参数，\(c\) 是编译期常数。多个这样的条件可以用 `and` 连接。例如：

```python
self.assume(hidden % self.num_per_channels == 0)
```

承诺「`hidden` 能被 `num_per_channels` 整除」。这条信息会进入函数元数据，供标量分析（scalar analysis）使用：当你写下 `sf_col = offset_n // self.num_per_channels` 或 `hidden // self.num_per_channels` 这类除法/模运算时，编译器知道除法是「整除无余数」，从而可以做更激进的化简（用乘法替代除法、消去余数计算等）。

> 与 `static_assert` 区分：[root.py:275-292](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L275-L292) 的 `static_assert(cond, msg)` 是 *编译期断言*，`cond` 必须是编译期常量，为假时立即抛 `AssertionError` 终止编译；而 `assume` 是单向承诺，不检查、只利用。

#### 4.3.2 核心流程

`self.assume(cond)` 的生命周期：

1. **生成指令**：`root.py` 的 `assume` 把 `cond` 交给 builder，builder 创建 `AssumeInst(output=None, inputs=(), condition=cond)` 追加到函数体。此时它还只是一条普通指令，会出现在转译后的 IR 里。
2. **lower_assume 变换**：`LowerAssumePass` 遍历所有 `AssumeInst`：
   - 把 `condition` 按 `and` 拆成若干合取项；
   - 对每个形如 `a % c == 0` 的项，提取参数 `a` 与除数 `c`，若同一参数出现多次则取最小公倍数 lcm；
   - 把结果写入 `Function.metadata.param2divisibility`（一个 `{参数Var: 整除数}` 的映射）；
   - `AssumeInst` 本身被「消除」（访问器返回 `None` → 语句塌缩为空），从此 IR 里不再有这条指令。
3. **被标量分析消费**：`analyze_scalar` 读取 `metadata.param2divisibility`，为每个带整除性的参数构造一个 `ScalarSet(divisibility=c, lower_bound=0)`，作为后续界感知化简（bound-aware simplify）的事实来源。
4. （补充）转译器在构造函数元数据时，也会把 `__init__` 里声明过的参数整除性一并写入 `param2divisibility`，与 `assume` 殊途同归。

支持的 `cond` 语法（见 `lower_assume` 的解析）：

```
cond      ::= term | term and term and ...
term      ::= a % c == 0      # a 为内核参数(int32)，c 为编译期常数
```

不符合上述形式（例如 `a > 0`、`a % c == 1`、或 `a` 不是参数）会在 `lower_assume` 阶段抛 `RuntimeError`。布尔字面量 `True` 被当作空承诺（无操作），`False` 直接报错。

#### 4.3.3 源码精读

**用户面 API** —— [root.py:64-94](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L64-L94) 的 `assume`。注意它接受 `bool` 与 `Expr` 两种输入，`bool` 为 `True` 时直接返回（空承诺），最后调用 `self._builder.assume(cond)`：

```python
def assume(self, cond):
    if isinstance(cond, bool):
        if not cond: raise InstructionError("The condition must be True")
        return
    self._builder.assume(cond)
```

其 docstring 明确限定了 `cond` 的合法形式（`a % c == 0` 及其合取）。

**IR 数据结构** —— [hints.py:40-46](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/hints.py#L40-L46) 的 `AssumeInst`，极简：只有 `condition` 一个字段，`output=None, inputs=()`。

**核心变换** —— [lower_assume.py:29-63](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_assume.py#L29-L63) 的 `ApplyAssumeRewriter.visit_AssumeInst`。它用栈把 `and` 展开成合取项列表，再逐项匹配 `Equal(Mod(a, c), 0)`：

```python
for term in terms:
    if (isinstance(term, Equal) and isinstance(term.a, Mod)
        and isinstance(term.a.b, Constant) and isinstance(term.a.a, Var)
        and isinstance(term.b, Constant) and term.b.value == 0):
        a, divisor = term.a.a, int(term.a.b.value)
        param2divisibility[a] = lcm(param2divisibility[a], divisor)  # 多次取 lcm
    else:
        raise RuntimeError("Can not recognize the condition in assume: {}".format(term))
```

随后 [lower_assume.py:65-80](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/lower_assume.py#L65-L80) 的 `visit_Function` 把收集到的整除性合并进 `metadata.param2divisibility`（同样取 lcm）。由于 `visit_AssumeInst` 返回 `None`，`AssumeInst` 语句会被消除。

**元数据落点** —— [func.py:50](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/func.py#L50) 定义 `param2divisibility: frozendict[Var, int]`，[func.py:79-80](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/func.py#L79-L80) 提供 `with_param2divisibility` 更新它。`AssumeInst` 的成果最终就停在这个字段里。

**被标量分析消费** —— [scalar_analyzer.py:433-438](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/analyzers/scalar_analyzer.py#L433-L438)：对每个出现在 `param2divisibility` 里的参数，初始化其取值集合为「可被 `c` 整除且非负」：

```python
for param in func.params:
    if param in metadata.param2divisibility:
        var2set[param] = ScalarSet(divisibility=metadata.param2divisibility[param], lower_bound=0)
```

转译器侧同样会把 `__init__` 声明的整除性写入元数据，见 [transpiler.py:197-207](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/transpiler.py#L197-L207)。

**真实范例** —— [examples/quantization/per_token_cast.py:51](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/quantization/per_token_cast.py#L51)：

```python
self.assume(hidden % self.num_per_channels == 0)
```

该内核随后用 `cdiv(hidden, self.num_per_channels)`、`offset_n // self.num_per_channels` 等表达式，`assume` 让编译器确信这些整除关系成立，从而化简运算。

#### 4.3.4 代码实践

**实践目标**：观察 `AssumeInst` 如何在 `lower_assume` 变换前后「从指令变成元数据」。

**操作步骤**：

1. 直接复用项目自带范例 [examples/quantization/per_token_cast.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/quantization/per_token_cast.py)，在 `main` 调用内核前加上：

```python
tilus.option.debug.dump_ir()
tilus.option.cache_dir("/tmp/assume-demo")
```

2. 触发一次编译（用 `PerTokenCast(num_per_channels=128)` 调用一次即可，不必跑完 benchmark）。

3. 在缓存目录 `ir/` 下，依次打开 `0_Original.txt`（变换前）与编号最大的 `*_lower_assume.txt`（即 `lower_assume` 变换后的那份）。

**需要观察的现象**：

- 在 `0_Original.txt` 里搜索 `Assume`，应能看到一条形如 `AssumeInst` / `assume(...)` 的指令（其 `condition` 为 `hidden % 128 == 0`）。
- 在 `lower_assume` 之后的文件里，这条指令消失了；转而在函数元数据区出现一行 `param_divisibility = { hidden: 128 }`（由 [printer.py:205-206](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/printer.py#L205-L206) 渲染）。

**预期结果**：`assume` 提供的整除性信息从「函数体里的一条指令」迁移到了「函数元数据里的 `param_divisibility`」，印证了 4.3.2 描述的流程。

> 该范例依赖 `tile_kernels` 包与一块 Hopper/Blackwell 级别的 GPU；若本机环境不全，「源码阅读型实践」同样成立：直接用 `IRPrinter` 在内存里打印变换前后的 `Program` 文本对照即可。运行结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：写 `self.assume(n % 8 == 0)` 之后又写 `self.assume(n % 12 == 0)`，最终 `param2divisibility[n]` 是多少？

**参考答案**：两次取 lcm：`lcm(8, 12) = 24`，所以记录为 24（即承诺 `n` 能被 24 整除）。

**练习 2**：`self.assume(n > 0)` 合法吗？会发生什么？

**参考答案**：不合法。`lower_assume` 只识别 `a % c == 0` 形式的合取项，`n > 0` 无法匹配，会在该变换阶段抛 `RuntimeError("Can not recognize the condition in assume: ...")`。

**练习 3**：为什么说 `assume` 是「承诺」而非「检查」？

**参考答案**：`assume` 不生成任何运行时校验代码，运行时 `cond` 为假也不会报错；它只在编译期把信息写进元数据供优化使用。若实际参数不满足你承诺的整除性，内核会基于错误前提被优化，可能得到错误结果但不报错。真正的编译期检查请用 `static_assert`。

---

## 5. 综合实践

把本讲三块内容串起来：基于 [examples/quantization/per_token_cast.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/quantization/per_token_cast.py) 的结构，做一个「源码阅读 + 小幅修改 + IR 观察」的综合任务。

**任务**：

1. **定位三要素**：在该范例的 `__call__` 中找到
   - 一个 `for ... in range(...)` 循环（控制流）；
   - 一条 `self.assume(...)`（整除性提示）；
   - （若该范例没有现成的线程组）参考 [hopper_matmul/matmul_v0.py:57-72](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v0.py#L57-L72)，在其 `store_global` 之外设计一个由 `with self.single_thread():` 守护的 `printf`，用于打印某个调试标量。

2. **开启转储**：`tilus.option.debug.dump_ir()` + `tilus.option.cache_dir(...)`，触发一次编译。

3. **在 `ir/0_Original.txt` 里一次性找到本讲三种产物的 IR 形态**：
   - `for i in range(...):`（可能带 `#pragma unroll`）——来自控制流；
   - `with thread_group(thread_begin=elect_any, num_threads=1):`——来自 `single_thread`；
   - 一条 `AssumeInst`（及其 `condition`）——来自 `assume`。

4. **跟踪演变**：翻到 `lower_assume` 之后的 IR，确认 `AssumeInst` 消失、`param_divisibility` 出现；翻到 `lower_load_store` 之后的 IR，观察循环体如何被进一步具象化。

**预期结果**：你能用一张表把「Python 写法 → 生成的 IR 节点 → 起作用的变换」三列对应起来，例如：

| Python 写法 | IR 节点 | 关键变换 |
| --- | --- | --- |
| `for i in self.range(n, unroll="all")` | `ForStmt`（带 `#pragma unroll`） | 代码生成阶段落地展开 |
| `with self.single_thread():` | `ThreadGroupStmt(begin=-1, n=1)` | 代码生成阶段发 `elect.sync`/统一谓词 |
| `self.assume(n % c == 0)` | `AssumeInst` → 消除 | `lower_assume` → `param2divisibility` → `analyze_scalar` |

> 运行时数值正确性「待本地验证」（依赖具体 GPU 与 `tile_kernels`）；本综合实践的 deliverable 是 **IR 层面的对照表**，这部分可稳定观察。

## 6. 本讲小结

- **控制流**：`for i in self.range(start, end, step, unroll=...)` 被转译成 `ForStmt`；`unroll` 取 `None / "all" / n` 三档，经 `IRPrinter` 渲染成 `#pragma unroll`，是给编译器的展开提示而非强制。内置 `range` 同样可被转译，但没有 `unroll` 口子。
- **线程组**：`with self.thread_group(thread_begin, num_threads):` 把体内语句的执行权收窄到一段连续线程；`single_thread / single_warp / warp_group` 都是其快捷方式。`thread_begin` 是相对父组的，嵌套时由 `ThreadGroupStack` 累加成绝对线程号。
- **elect-any**：`single_thread()` 默认 `thread=-1`，对应 `ThreadGroupStmt(thread_begin=-1, ...)`，让硬件用 `elect.sync`/统一谓词自由挑选线程，避免发散分支；要求 `num_threads` 为 2 的幂。
- **sync 作用域**：`self.sync()` 只同步当前线程组，出了上下文才同步整块。
- **assume**：`self.assume(a % c == 0)` 是单向编译期承诺，经 `lower_assume` 解析为 `metadata.param2divisibility`（多次取 lcm），指令本身被消除；该整除性随后被 `analyze_scalar` 消费，支撑界感知化简。运行时不检查、不报错。
- **与断言的区别**：`static_assert` 是编译期断言（为假即中止编译），`assume` 是单向承诺（只利用不检查），二者不可混用。

## 7. 下一步学习建议

- 下一篇 [u2-l4 自动调优：@autotune 与调度空间](u2-l4-autotune-and-schedule-space.md) 会讲 `@autotune` 如何定义调度搜索空间；线程组与循环展开正是 autotune 里常见的可调维度（如 producer/consumer 各占几个 warp、`unroll` 取值），本讲是其前置。
- 想看线程组在真实高性能内核里的复杂用法，可直接跳读 [examples/hopper_matmul/matmul_v3.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v3.py)（producer warp 128、consumer warp-group 0–127 的双线程组结构），不过它会用到本系列后续才详细展开的 wgmma/tma 指令组。
- 想深入理解 `assume` 产出的整除性如何变成优化，可在学习 [u5 变换（Transforms）](u5-l3-dead-code-elimination-and-scalar-analysis.md) 时回到本讲，对照 `scalar_analyze` 与 `bound_aware_simplify` 阅读。
- 若你想自定义类似的「提示性指令」或新控制结构，可先读 [u8-l5 扩展开发：自定义 Pass 与新增指令](u8-l5-writing-custom-pass-and-extension.md)，了解新增一条指令需要同步提供的 IR 定义、变换与发射器。
