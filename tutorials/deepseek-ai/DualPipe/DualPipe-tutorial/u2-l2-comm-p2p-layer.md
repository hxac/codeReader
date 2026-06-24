# 通信层 comm.py：P2P 收发

## 1. 本讲目标

`dualpipe/comm.py` 是整个 DualPipe 里最短的一个文件（不到 40 行），却承担了分布式训练最核心的一件事：**让相邻的 GPU 之间点对点地交换张量**。本讲学完后，你应当能够：

- 说清 `set_p2p_tensor_shapes` / `set_p2p_tensor_dtype` 这两个公共 API 的作用，以及它们为什么必须是「全局状态」。
- 理解 `append_irecv` / `append_isend` 是如何**累积** `dist.P2POp` 到一个列表里，而不是立刻发送的。
- 理解 `get_global_rank` 为什么要把「组内 rank」翻译成「全局 rank」。
- 把这条链路串起来：先累积 P2P 操作，最后由 `dist.batch_isend_irecv` 一次性提交——这正是 DualPipe「计算/通信重叠」的底层基石。

本讲只聚焦通信层本身；这些原语如何被 8 步调度驱动，留给后续的 `u3-l4`（通信原语与组合操作）和 `u3-l5`（八步调度）讲义承接。

## 2. 前置知识

在进入源码前，先用通俗语言补齐几个 PyTorch 分布式概念（部分在 `u1-l2` 已建立，这里复习并聚焦到 P2P）：

- **进程组（process group）与 rank**：分布式训练里，每个 GPU 上跑一个进程，每个进程在组内有一个编号 `rank`，组的大小叫 `world_size`。DualPipe 里「进程数 = 流水线阶段数 `pp_size` = GPU 数」三者相等（见 `u1-l2`）。
- **集合通信 vs 点对点通信（P2P）**：`all-reduce`、`broadcast` 这类是集合通信，所有进程一起参与；而流水线并行里，每个阶段只需要和**相邻**的前一个、后一个阶段交换数据，这种「一个发、一个收」就是 P2P（point-to-point）通信。`comm.py` 只负责 P2P。
- **阻塞 vs 非阻塞**：`send`/`recv` 是阻塞的，发完才返回；`isend`/`irecv` 中的 `i` 表示 immediate（非阻塞），调用后立刻返回一个「Work 句柄」，真正的数据搬运在后台进行，之后你对句柄调用 `.wait()` 才保证完成。非阻塞是「通信与计算重叠」的前提。
- **`dist.P2POp` 与 `dist.batch_isend_irecv`**：`P2POp` 是对「一次 P2P 操作」的描述（是发还是收、用哪个张量、对端是谁）。`batch_isend_irecv` 接收一个 `P2POp` 列表，**一次性提交**里面所有操作并返回一组 Work 句柄。批量提交的好处是：多个收发请求会被一起调度，从而能彼此重叠，而不是一个等一个串行排队。

> 一个关键直觉：DualPipe 不在「想发的时候」就发，而是先把若干个收发请求攒到一个列表里，挑一个合适时机一次提交。`comm.py` 提供的就是「攒请求」的工具，「提交」则在引擎层完成。这一点贯穿全讲。

## 3. 本讲源码地图

本讲只涉及一个核心源码文件，外加它在引擎层被调用的几处：

| 文件 | 作用 |
| --- | --- |
| [dualpipe/comm.py](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/comm.py) | 通信层全部内容：两个全局变量、两个 setter、`build_from_tensor_shapes`、`append_irecv`、`append_isend` |
| dualpipe/__init__.py | 把 `set_p2p_tensor_shapes` / `set_p2p_tensor_dtype` 作为公共 API 导出 |
| dualpipe/dualpipe.py | 引擎层：以 `import dualpipe.comm as comm` 引入通信层，用 `append_irecv`/`append_isend` 攒操作，用 `_commit_and_wait_comm` 调 `dist.batch_isend_irecv` 一次性提交 |
| examples/example_dualpipev.py | 示例里如何调用两个 setter 初始化全局形状与 dtype |

依赖关系（承接 `u1-l3`）：`comm.py` 处于叶子层，只依赖 PyTorch（`torch` 与 `torch.distributed`），不依赖包内其它文件。引擎层 `dualpipe.py` 通过 `import dualpipe.comm as comm` 使用它。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：①全局形状/dtype 状态；②接收缓冲区工厂 `build_from_tensor_shapes`；③累积 P2P 操作的 `append_irecv`/`append_isend`（含全局 rank 映射）。最后在「综合实践」里把三个模块与 `batch_isend_irecv` 的批量提交串成完整链路。

### 4.1 全局张量形状与类型：TENSOR_SHAPES / TENSOR_DTYPE

#### 4.1.1 概念说明

做一次 P2P **接收**（`irecv`），本进程必须**预先准备好一块缓冲区**，把数据接进来。但问题是：数据还在别的 GPU 上、尚未到达，本进程怎么知道该开多大、什么类型的缓冲区？

DualPipe 的解法是**约定**：所有相邻阶段之间传递的张量，形状和类型在启动时就固定下来，写成两个全局变量；`irecv` 时照着这两个变量分配缓冲区即可。这就是 [dualpipe/comm.py](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/comm.py) 顶部 `TENSOR_SHAPES` 与 `TENSOR_DTYPE` 存在的意义。

为什么用**模块级全局变量**而不当作参数到处传？因为：

- 这两个值在整个训练过程中不变，且被引擎层频繁读取（`append_irecv` 每次接收都要读）。
- 引擎层以 `import dualpipe.comm as comm` 引入后，直接用 `comm.TENSOR_SHAPES` 访问（见下文 4.1.3 的断言）。全局变量让通信层的各个函数共享同一份「约定」，无需把形状参数层层传递。

#### 4.1.2 核心流程

初始化阶段（每个进程各做一次）：

1. 用户在示例里调用 `set_p2p_tensor_shapes([(micro_batch_size, seq_len, hidden_size)])`，传入一个形状元组的列表（列表长度 = 一次收发包含几个张量）。
2. 用户调用 `set_p2p_tensor_dtype(torch.float32)`，传入统一的 dtype。
3. 两个 setter 把值写入模块级全局变量 `TENSOR_SHAPES` / `TENSOR_DTYPE`。
4. 之后所有 P2P 收发都默认使用这两个全局值。

注意第一个参数是**列表**：因为一个阶段一次可能传递多个张量（例如多个输入/输出），列表里每个元组描述其中一个张量的形状。

#### 4.1.3 源码精读

两个全局变量声明为模块级，初值都是 `None`，表示「尚未设置」：

```python
TENSOR_SHAPES: List[Tuple[int]] = None
TENSOR_DTYPE: torch.dtype = None
```

[dualpipe/comm.py:7-8](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/comm.py#L7-L8) —— 定义两个全局变量，初值 `None`。

两个 setter 的实现极简，都是 `global` 声明后赋值：

```python
def set_p2p_tensor_shapes(shapes: List[Tuple[int]]):
    global TENSOR_SHAPES
    TENSOR_SHAPES = shapes

def set_p2p_tensor_dtype(dtype: torch.dtype):
    global TENSOR_DTYPE
    TENSOR_DTYPE = dtype
```

[dualpipe/comm.py:11-13](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/comm.py#L11-L13) —— `set_p2p_tensor_shapes`。  
[dualpipe/comm.py:16-18](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/comm.py#L16-L18) —— `set_p2p_tensor_dtype`。

这两个函数被 `dualpipe/__init__.py` 提升为公共导出（见 `u1-l3`），所以在示例里可以直接 `from dualpipe import set_p2p_tensor_shapes, set_p2p_tensor_dtype`：

```python
set_p2p_tensor_shapes([(micro_batch_size, seq_len, hidden_size)])
set_p2p_tensor_dtype(torch.float32)
```

[examples/example_dualpipev.py:123-124](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L123-L124) —— 示例在 `main` 里设置全局形状与 dtype。

引擎层在 `step()` 开头会断言这两个全局量已被设置，否则接收时无法分配缓冲区：

```python
assert comm.TENSOR_SHAPES is not None and comm.TENSOR_DTYPE is not None, \
    "You need to call set_p2p_tensor_shapes and set_p2p_tensor_dtype before doing a step."
```

[dualpipe/dualpipe.py:325-326](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L325-L326) —— `step` 入口断言两个全局量已初始化（注意引擎以 `comm.TENSOR_SHAPES` 形式读取）。

#### 4.1.4 代码实践

1. **实践目标**：验证两个 setter 确实改写了模块级全局变量。
2. **操作步骤**：在任意能 `import dualpipe` 的环境（单进程、无需 GPU）执行：

   ```python
   # 示例代码
   import dualpipe.comm as comm
   from dualpipe import set_p2p_tensor_shapes, set_p2p_tensor_dtype
   import torch

   print("before:", comm.TENSOR_SHAPES, comm.TENSOR_DTYPE)   # 期望 None None
   set_p2p_tensor_shapes([(3, 256, 512)])
   set_p2p_tensor_dtype(torch.float32)
   print("after :", comm.TENSOR_SHAPES, comm.TENSOR_DTYPE)   # 期望 [(3, 256, 512)] torch.float32
   ```

3. **需要观察的现象**：调用前两个全局变量为 `None`；调用后分别是你传入的列表和 dtype。
4. **预期结果**：输出形如 `before: None None` 与 `after: [(3, 256, 512)] torch.float32`。
5. 这一步无需 GPU，设置全局变量本身不分配显存，可放心运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `set_p2p_tensor_shapes` 的参数是「形状元组的**列表**」而不是单个形状元组？

> **参考答案**：因为一次相邻阶段间的收发可能包含**多个**张量（例如模型有多个输入或多个输出），列表里的每个元组描述其中一个张量的形状，列表长度等于一次收发的张量个数。`build_from_tensor_shapes` 会为列表里的每个形状各分配一个缓冲区（见 4.2）。

**练习 2**：如果在调用 `dualpipe_model.step(...)` 之前忘记调用两个 setter，会发生什么？

> **参考答案**：`step()` 开头的断言（dualpipe.py:325-326）会失败并抛出 `AssertionError`，提示信息为 `"You need to call set_p2p_tensor_shapes and set_p2p_tensor_dtype before doing a step."`。因为接收时需要依据全局形状分配缓冲区，未设置则无从分配。

### 4.2 接收缓冲区的工厂：build_from_tensor_shapes

#### 4.2.1 概念说明

上一节确立了「约定好的形状/dtype」。本节是消费这个约定的工厂函数：`build_from_tensor_shapes` 负责照着全局形状，**在当前 GPU 上分配一组空缓冲区**，专门用于「接收」。

它不直接收数据，而是产出「准备好被写入」的张量；真正的 `irecv` 由 `append_irecv`（4.3 节）挂上去。这种「先造缓冲区、再挂操作」的分工，正是非阻塞接收的标准写法。

#### 4.2.2 核心流程

对 `TENSOR_SHAPES` 中的每一个形状 `s`：

1. 用 `torch.empty(s, ...)` 分配一块未初始化的显存（`empty` 不清零，比 `zeros` 快，因为反正马上要被接收的数据覆盖）。
2. 指定 `dtype=TENSOR_DTYPE`、`device="cuda"`（放到当前 GPU）、`requires_grad=True`（接收的是前向激活，后续反向需要梯度）。
3. 把所有分配出的张量收集成一个列表返回。

设一次收发有 \( N \) 个非 None 形状，则返回列表长度为 \( N \)，即一次「接收」会生成 \( N \) 个 `P2POp`（见 4.3）：

\[ N = \bigl|\{\, s \in \text{TENSOR\_SHAPES} \mid s \neq \text{None} \,\}\bigr| \]

#### 4.2.3 源码精读

整个函数就是一行列表推导：

```python
def build_from_tensor_shapes():
    return [torch.empty(s, dtype=TENSOR_DTYPE, device="cuda", requires_grad=True) for s in TENSOR_SHAPES]
```

[dualpipe/comm.py:21-22](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/comm.py#L21-L22) —— 按全局形状在 GPU 上分配接收缓冲区。

四个要点：
- `torch.empty`：分配未初始化内存，省去清零开销。
- `dtype=TENSOR_DTYPE`：与发送端类型一致，否则收发类型不匹配会出错。
- `device="cuda"`：硬编码到 CUDA，因为 DualPipe 面向多 GPU 训练。
- `requires_grad=True`：接收到的张量要参与反向，因此默认开启求导。注意，该函数只在 4.3 的 `append_irecv`（接收）路径调用；**发送**用的张量是引擎算出来的真实激活，不需要这里造。

#### 4.2.4 代码实践

1. **实践目标**：观察 `build_from_tensor_shapes` 产出的缓冲区结构。
2. **操作步骤**：先设好全局形状/dtype（同 4.1.4），再调用：

   ```python
   # 示例代码（需要可用 CUDA 设备，否则 device="cuda" 报错）
   buffers = comm.build_from_tensor_shapes()
   for i, t in enumerate(buffers):
       print(i, t.shape, t.dtype, t.device, t.requires_grad)
   ```

3. **需要观察的现象**：返回一个列表，长度等于 `TENSOR_SHAPES` 的长度；每个张量形状、dtype 与设定一致，`device` 为 `cuda:x`，`requires_grad=True`。
4. **预期结果**：对于 `set_p2p_tensor_shapes([(3, 256, 512)])`，输出一行，形状 `torch.Size([3, 256, 512])`，dtype `torch.float32`，`requires_grad=True`。
5. 若无 GPU，`device="cuda"` 会抛错——此时本步**待本地验证**；可改为阅读源码理解，或临时把 `device="cuda"` 换成 `device="cpu"` 自行实验（注意这不是项目原有写法）。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `torch.empty` 而不是 `torch.zeros`？

> **参考答案**：这些缓冲区唯一用途是「被接收的数据覆盖」，初始值无所谓。`empty` 跳过清零步骤，比 `zeros` 更快。在频繁的微批次收发中，这个开销差异会被放大。

**练习 2**：为什么缓冲区要设 `requires_grad=True`？

> **参考答案**：接收的是上游阶段传来的前向激活，它是本阶段前向计算与后续反向求导的输入；要让它成为计算图的一部分以回传输入梯度（`input_grads`，见 `dualpipe.py` 的 `_backward_compute_chunk`），就必须开启求导。发送方向用的张量是真实激活，自带求导属性，故 `build_from_tensor_shapes` 只服务接收路径。

### 4.3 累积 P2P 操作：append_irecv / append_isend 与全局 rank 映射

#### 4.3.1 概念说明

这是 `comm.py` 的重头戏。两个函数名里的 `append` 点明了设计意图：它们**不立即收发**，而是把一个个 `dist.P2POp` 追加到传入的 `ops` 列表里，等引擎攒够一批后再统一提交。

这样做的原因是 `dist.batch_isend_irecv` 的存在：把「同一段时机内的多个收发」攒成一批一次性提交，这些操作才能彼此重叠、与计算重叠——这正是 DualPipe 压缩气泡的关键。

另一个要点是**全局 rank 映射**。引擎里 `prev_rank`/`next_rank` 这些邻居是「进程组内的局部 rank」（见 `u3-l1`），而 PyTorch 的 P2P 操作要求给出**全局 rank**。当进程组就是整个 world 时两者相等，但为了在子组场景也正确，必须用 `get_global_rank` 做一次翻译。

#### 4.3.2 核心流程

**接收 `append_irecv(ops, src, group)`**：

1. 调用 `build_from_tensor_shapes()` 分配接收缓冲区列表 `tensors`（复用 4.2）。
2. `src = dist.distributed_c10d.get_global_rank(group, src)`，把组内 src 翻译成全局 src。
3. 对 `tensors` 中每个**非 None** 的张量，构造 `dist.P2POp(dist.irecv, tensor, src)` 追加进 `ops`。
4. 返回 `tensors`（调用方靠它拿到接收到的数据）。

**发送 `append_isend(ops, tensors, dst, group)`**：

1. `dst = dist.distributed_c10d.get_global_rank(group, dst)`，把组内 dst 翻译成全局 dst。
2. 对 `tensors` 中每个**非 None** 的张量，构造 `dist.P2POp(dist.isend, tensor, dst)` 追加进 `ops`。
3. 无返回值（发送的张量本就归调用方所有，`tensors` 由外部传入）。

**批量提交（引擎层，非 comm.py）**：攒完一批后，引擎调用 `dist.batch_isend_irecv(ops)` 一次性提交，再对每个返回的 Work 句柄 `.wait()`，最后清空 `ops`。

> 「非 None 才发」是为了支持「这一侧没有这个张量」的稀疏情形——例如某个阶段对某条信号不感兴趣，对应位置为 `None`，就跳过，不产生无意义的收发请求。

#### 4.3.3 源码精读

接收：

```python
def append_irecv(ops: List[dist.P2POp], src: int, group: dist.ProcessGroup) -> List[torch.Tensor]:
    tensors = build_from_tensor_shapes()
    src = dist.distributed_c10d.get_global_rank(group, src)
    for tensor in tensors:
        if tensor is not None:
            ops.append(dist.P2POp(dist.irecv, tensor, src))
    return tensors
```

[dualpipe/comm.py:25-31](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/comm.py#L25-L31) —— `append_irecv`：造缓冲区 → 映射全局 rank → 累积 `irecv` 操作 → 返回缓冲区。

发送：

```python
def append_isend(ops: List[dist.P2POp], tensors: List[torch.Tensor], dst: int, group: dist.ProcessGroup) -> None:
    dst = dist.distributed_c10d.get_global_rank(group, dst)
    for tensor in tensors:
        if tensor is not None:
            ops.append(dist.P2POp(dist.isend, tensor, dst))
```

[dualpipe/comm.py:34-38](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/comm.py#L34-L38) —— `append_isend`：映射全局 rank → 累积 `isend` 操作（无返回）。

两个函数的关键差异与共性：

| 维度 | `append_irecv` | `append_isend` |
| --- | --- | --- |
| 张量来源 | 自己用 `build_from_tensor_shapes` 现造 | 由调用方作为参数 `tensors` 传入 |
| 是否返回 | 返回造出的缓冲区（供后续读取） | 不返回 |
| 对端参数 | `src`（从谁收） | `dst`（发给谁） |
| 全局 rank 映射 | 有 | 有 |
| 累积进 `ops` | 是 | 是 |

接下来看引擎层如何调用这两个函数，以及如何一次性提交。以「接收前向输入」为例：

```python
def _recv_forward(self, phase: int) -> None:
    phase ^= self.is_in_second_half
    is_first_stage = (self.is_first_rank and phase == 0) or (self.is_last_rank and phase == 1)
    if is_first_stage:
        return
    self.current_recv_f_chunk_id[phase] += 1
    tensors = comm.append_irecv(self.comm_ops, self.prev_rank if phase == 0 else self.next_rank, self.group)
    self.input_chunks[phase].append(tensors)
```

[dualpipe/dualpipe.py:231-239](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L231-L239) —— `_recv_forward`：非首阶段时调 `append_irecv` 攒一个「从前一阶段接收」的请求，把返回的缓冲区登记到 `input_chunks`。

注意它传的是 `self.comm_ops`（引擎在 `_reset_states` 里初始化为空列表，见 [dualpipe/dualpipe.py:64](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L64)），`prev_rank`/`next_rank` 是组内 rank，`self.group` 是进程组。多个 `_recv_forward`/`_recv_backward`/`_send_forward`/`_send_backward` 可以往同一个 `comm_ops` 里攒多条操作。

攒够之后，由 `_commit_and_wait_comm` 一次性提交并等待：

```python
def _commit_and_wait_comm(self) -> None:
    if not self.comm_ops:
        return
    reqs = dist.batch_isend_irecv(self.comm_ops)
    for req in reqs:
        req.wait()
    self.comm_ops = []
    self._free_tensors()
```

[dualpipe/dualpipe.py:285-292](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L285-L292) —— 把攒好的 `comm_ops` 用 `dist.batch_isend_irecv` 一次性提交，逐个 `wait`，再清空列表、释放待回收张量。

这就形成了「累积 → 批量提交」的完整闭环。引擎里像 `_forward_backward_chunk`（[dualpipe/dualpipe.py:205-214](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L205-L214)）会先调 `_recv_forward`+`_recv_backward`（各攒一条），再一次 `_commit_and_wait_comm` 提交——两条接收请求被打包成一次批量调用，从而能重叠进行。这正是 DualPipe 计算/通信重叠的底层机制（完整调度见 `u3-l4`/`u3-l5`）。

#### 4.3.4 代码实践

1. **实践目标**：亲手用 `append_irecv` / `append_isend` 构造一组 `P2POp`，看清它们如何被累积、如何被 `batch_isend_irecv` 一次性提交。
2. **操作步骤**（需要多 GPU + NCCL；若无此环境，请看「源码阅读型实践」替代）：

   ```python
   # 示例代码：两个进程互发互收。需用 torchrun / spawn 拉起 world_size=2、NCCL、每进程绑一张卡。
   import torch, torch.distributed as dist
   import dualpipe.comm as comm
   from dualpipe import set_p2p_tensor_shapes, set_p2p_tensor_dtype

   def run(rank, world_size):
       dist.init_process_group("nccl", init_method="env://", world_size=world_size, rank=rank)
       torch.cuda.set_device(rank)
       set_p2p_tensor_shapes([(4,)])          # 约定收发一个长度 4 的 1D 张量
       set_p2p_tensor_dtype(torch.float32)

       ops = []                               # <-- 这就是引擎里的 comm_ops
       if rank == 0:
           send_tensor = [torch.ones(4, device="cuda")] * 10  # 假数据
           comm.append_isend(ops, send_tensor, dst=1, group=dist.group.WORLD)
           recv = comm.append_irecv(ops, src=1, group=dist.group.WORLD)
       else:
           recv = comm.append_irecv(ops, src=0, group=dist.group.WORLD)
           send_tensor = [torch.ones(4, device="cuda") * 2]
           comm.append_isend(ops, send_tensor, dst=0, group=dist.group.WORLD)

       print(f"rank {rank} 攒了 {len(ops)} 个 P2POp")  # 期望 2：1 发 + 1 收
       reqs = dist.batch_isend_irecv(ops)     # 一次性提交
       for r in reqs:
           r.wait()
       print(f"rank {rank} 收到:", recv[0])   # 0 号收到全 2，1 号收到全 10
       dist.destroy_process_group()
   ```

3. **需要观察的现象**：每个 rank 的 `ops` 长度为 2（一个 `isend`、一个 `irecv`）；提交并 `wait` 后，rank 0 收到全 2 的张量，rank 1 收到全 10 的张量。
4. **预期结果**：两进程都打印「攒了 2 个 P2POp」，且各自打印出对端发来的值。
5. 若无多 GPU 环境，本步**待本地验证**。改做下面的**源码阅读型实践**：
   - 在 [dualpipe/dualpipe.py:205-214](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L205-L214) 的 `_forward_backward_chunk` 中，跟踪一次调用：它先 `_recv_forward(phase0)` → `_recv_backward(phase1)`（两次 `append_irecv`，各往 `self.comm_ops` 攒一条），再 `_commit_and_wait_comm()` 一次性提交。请把这条「攒 2 条 → 提交 1 次」的顺序写下来，并指出 `comm_ops` 在 [dualpipe/dualpipe.py:64](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L64) 被初始化、在 [dualpipe/dualpipe.py:291](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L291) 被清空。

#### 4.3.5 小练习与答案

**练习 1**：`append_irecv` 和 `append_isend` 调用之后，数据真的已经发出/收到了吗？

> **参考答案**：没有。它们只是把 `dist.P2POp` 追加进 `ops` 列表，属于「登记请求」。真正的收发发生在引擎调用 `dist.batch_isend_irecv(ops)` 并对返回句柄 `wait()` 之后（见 dualpipe.py:288-290）。这就是「累积」与「提交」的两段式设计。

**练习 2**：为什么要在 `append_irecv`/`append_isend` 里调 `get_global_rank`，而不是直接用传入的 `src`/`dst`？

> **参考答案**：传入的 `src`/`dst` 是**进程组内**的局部 rank（引擎里的 `prev_rank`/`next_rank` 见 dualpipe.py:38-39），而 PyTorch 的 P2P 操作需要**全局 world rank**。当 DualPipe 用的是默认全 world 组时两者相等；但一旦用子组（subgroup）训练，局部 rank 与全局 rank 错位，不翻译就会发给错误的进程。`dist.distributed_c10d.get_global_rank(group, rank)` 正是做这层翻译。

**练习 3**：`append_irecv` 的循环里有 `if tensor is not None` 的判断，什么时候会出现 `None`？

> **参考答案**：当 `TENSOR_SHAPES` 中某个形状被显式设为 `None`（表示这一路没有张量要收）时，`build_from_tensor_shapes` 会产出 `None`（`torch.empty(None, ...)` 实际不可行，故这属于约定上的稀疏位）。判断的作用是跳过这些位，不为它们生成无意义的 `P2POp`。这为「某阶段不关心某条信号」的稀疏收发留了扩展空间。

## 5. 综合实践

把三个最小模块串成一个端到端理解任务。

**任务**：写一份说明文档（或注释），把下面这条完整链路用你自己的话讲清楚——

> 设置全局约定 → 分配接收缓冲 → 累积 P2POp → 批量提交

具体步骤：

1. 指出 `set_p2p_tensor_shapes` / `set_p2p_tensor_dtype`（comm.py:11-18）写入了哪两个全局变量，并解释为什么必须是全局共享。
2. 说明 `build_from_tensor_shapes`（comm.py:21-22）如何消费这两个全局变量、产出的缓冲区为何 `requires_grad=True`。
3. 画出一次 `_forward_backward_chunk(0, 1)`（dualpipe.py:205-214）内部 `comm_ops` 列表的「生长 → 清空」过程：哪些调用向列表里 `append` 了 `P2POp`、何时由 `batch_isend_irecv` 提交、提交后列表在 dualpipe.py:291 被清空。
4. 在说明里明确回答：**为什么 DualPipe 选择「先攒一批 P2POp 再一次性 `batch_isend_irecv`」，而不是每收到一个请求就单独 `dist.irecv`/`dist.isend`？**（提示：与计算/通信重叠、减少调度开销有关。）

**验收标准**：你的说明应能让一个没读过 DualPipe 的人明白——`comm.py` 的三个模块如何协作，把「想收发的意图」变成「一批被高效提交的 P2P 操作」。如果手头有多 GPU 环境，可额外运行 4.3.4 的示例代码佐证；否则以源码阅读为主即可。

## 6. 本讲小结

- `comm.py` 用两个模块级全局变量 `TENSOR_SHAPES` / `TENSOR_DTYPE`（comm.py:7-8）记录所有 P2P 收发统一的形状与类型，由公共 API `set_p2p_tensor_shapes` / `set_p2p_tensor_dtype`（comm.py:11-18）在启动时设置。
- `build_from_tensor_shapes`（comm.py:21-22）照全局形状在当前 GPU 上用 `torch.empty` 分配接收缓冲区，`requires_grad=True` 以支持反向。
- `append_irecv` / `append_isend`（comm.py:25-38）**不立即收发**，而是把 `dist.P2POp` 累积进传入的 `ops` 列表；接收现造缓冲区并返回，发送用调用方传入的张量。
- 两者都先用 `dist.distributed_c10d.get_global_rank` 把组内 rank 翻译成全局 rank，保证在子组场景下也能正确寻址对端。
- 引擎把多条收发攒进同一个 `comm_ops`（dualpipe.py:64），由 `_commit_and_wait_comm` 用 `dist.batch_isend_irecv` 一次性提交（dualpipe.py:288）并 `wait`，再清空列表——「累积 → 批量提交」是 DualPipe 计算/通信重叠的底层基石。

## 7. 下一步学习建议

本讲只讲了通信层「怎么攒、怎么造缓冲区」，但还没有讲「谁在什么时机攒、攒完怎么和计算交错」。建议按以下顺序继续：

- **`u2-l3` 微批次切分 scatter/gather**：看 `utils.py` 如何把整批输入切成微批次，这是流水线里要在相邻阶段间被这些 P2P 操作来回传递的「货物」。
- **`u3-l4` 通信原语与组合操作**：直接承接本讲，看引擎如何把 `_recv_forward`/`_recv_backward`/`_send_forward`/`_send_backward` 组合成 `_forward_chunk`/`_backward_chunk`/`_forward_backward_chunk`，以及 `phase ^= is_in_second_half` 的方向翻转。
- **`u3-l5` DualPipe 八步调度 step()**：看所有通信与计算原语如何在 8 步调度里被编排成「零气泡」流水线，体会本讲的「批量提交」如何真正换来气泡压缩。

阅读源码时，建议带着一个问题回头看 `comm.py`：**这一批 `comm_ops` 里，最多可能同时包含几种方向的请求（前向收/发、反向收/发）？** 这会自然把你引向 `u3-l4` 的组合操作设计。
