# 实战：自定义流水线模块与正确性验证

## 1. 本讲目标

前面几讲我们已经拆解了 DualPipe / DualPipeV 的调度引擎：rank 拓扑、状态缓冲、计算/通信原语、八步调度，以及 V 型转折。但这些都是「框架内部」。本讲换一个视角——**站在使用者一侧**：我要把自己写的神经网络模块塞进 DualPipeV，让它被正确调度，并且能**证明**调度结果与单进程串行训练完全一致。

学完本讲，你应当能够：

1. 说出一个「可被 DualPipe / DualPipeV 调度的模块」必须满足哪些硬约束，以及违反约束会发生什么。
2. 读懂 `ref_step` 这个单进程参考实现，理解它为何能作为分布式结果的「标准答案」。
3. 解释 `cal_diff` 这个余弦差异度量的数学含义，理解为什么 loss 用 `torch.equal` 做位级精确比较、而梯度却要用 `cal_diff < 1e-13`。
4. 独立修改 `example_dualpipev.py` 中的 `PipelineStage`（换激活、改层数），并让全套校验继续通过。

本讲是整个学习手册的收尾实战，依赖 [u4-l1（DualPipeV 的 V 型调度）](./u4-l1-dualpipev-v-schedule.md) 与 [u3-l3（overlapped_forward_backward）](./u3-l3-overlapped-forward-backward.md)。

## 2. 前置知识

本讲假定你已经掌握以下概念（这些在前置讲义中建立）：

- **overlapped_forward_backward 钩子**（u3-l3）：引擎把「一次前向 + 一次反向」合并为一个调度单元时，会把数据打包成八参数传给模块上的 classmethod 钩子，钩子返回 `(outputs0, loss0)`。两镜像模块若类型相同且定义了该钩子，引擎才启用重叠路径，否则降级为串行前向+反向（仍正确，但失去 F&B 重叠收益）。
- **WeightGradStore 握手**（u2-l4、u3-l3）：零气泡靠把权重梯度 `W` 延后入队实现。要让 `W` 可分离，线性层不能用 PyTorch 原生 `nn.Linear`，而要用自定义 `autograd.Function`——`backward` 里输入梯度 `B` 立即算并返回，权重梯度 `W` 依 `WeightGradStore.enabled` 决定立即算或入队。
- **DualPipeV 的 V 型拓扑**（u4-l1）：只在 first rank 喂数据，前向跑到 last rank 折返，反向跑回 first rank 算 loss；`phase` 含义对所有 rank 全局统一（0=前向、1=反向），last rank 处 phase0 输出经 `detach().requires_grad_()` 折成 phase1 输入。
- **微批次**（u2-l1、u2-l3）：一个 batch 切成多个 chunk 依次灌入流水线，引擎用 `scatter/gather` 在「整批」与「chunk 列表」之间转换。

本讲新引入的关键直觉是：**分布式正确性不能靠「跑通」来保证，必须用一份独立实现做对照**。DualPipe 的示例代码就是这套对照范式的最佳教材。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [examples/example_dualpipev.py](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py) | 本讲的**主模板**：自定义 `PipelineStage`、`overlapped_forward_backward` 钩子、`ref_step`、`cal_diff`、`main` 全流程校验 |
| [examples/example_dualpipe.py](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py) | DualPipe 版示例，结构与 V 版几乎相同，梯度校验多了 `all_gather` 步骤，用于对比 |
| [dualpipe/utils.py](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/utils.py) | `WeightGradStore`（钩子要握手的零气泡缓存）与 `run_backward`（钩子内部调用） |
| [dualpipe/dualpipev.py](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py) | 引擎侧：钩子的启用判定、调用点、张量生命周期与 `_free_tensors` 视图断言 |

---

## 4. 核心概念与源码讲解

本讲三个最小模块：**PipelineStage 自定义**、**ref_step 校验**、**cal_diff 度量**。三者恰好对应「造一个模块 → 跑出参考答案 → 量化误差」的实战闭环。

### 4.1 PipelineStage 自定义：可被调度的模块约束

#### 4.1.1 概念说明

DualPipeV 的 `__init__` 只接收一个 `(module0, module1)` 二元组，它**不关心**你的模块内部是什么结构——线性层、Transformer block、卷积都可以。但它要求模块满足一组**契约**，契约的核心是：让引擎能在「不知道你模块细节」的前提下，自由地把一次前向与一次反向重叠到一起，并在重叠时仍然正确地分摊梯度。

这套契约不是文档里的文字规矩，而是**散落在引擎代码里的隐式假设**。本节把它们逐条挖出来，并对应到真实代码。

#### 4.1.2 核心流程

一个可调度的 `PipelineStage` 由三层组成，自底向上：

```
LinearFunc (自定义 autograd.Function)   ← 把 W 从 B 中拆出来，交给 WeightGradStore
   ↑ 被 MyLinear.forward 调用
MyLinear (nn.Linear 子类)               ← 让线性层走自定义反向
   ↑ 被 PipelineStage.forward 组装
PipelineStage (nn.Module)               ← 定义 forward + overlapped_forward_backward 钩子
```

引擎侧的对接流程（一次 `_forward_backward_compute_chunk`）：

1. 引擎按 chunk 取出 inputs0/outputs1 等，**打包成八参数**。
2. 调用 `type(module0).overlapped_forward_backward(module0, inputs0, criterion0, labels0, module1, loss1, outputs1, output_grads1)`。
3. 钩子内部：先做前向得到 `outputs0`，再做反向（last stage 用 `loss1.backward()`，中间 stage 用 `run_backward`）。
4. 反向传播到 `LinearFunc.backward` 时，`B` 立即返回上游，`W` 依 `WeightGradStore.enabled` 立即算或入队。
5. 钩子返回 `(outputs0, loss0)`，引擎写入对应缓冲。

#### 4.1.3 源码精读

**(1) LinearFunc：把权重梯度从反向中剥离。** 这是整个自定义模块的「地基」——标准 `nn.Linear` 在反向时把 `grad_input` 与 `grad_weight` 绑在一起算，无法把 `W` 单独延后。`LinearFunc` 手写前向与反向，在 `backward` 中分离两者：

[examples/example_dualpipev.py:13-34](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L13-L34) 定义了 `LinearFunc`。关键点：

- `forward` 保存 `input, weight`，做标准 `F.linear`。
- `backward` 中先把 `grad_weight_fn` 封装成闭包（它累加 `weight.grad`），然后：

```python
if WeightGradStore.enabled:
    WeightGradStore.put(grad_weight_fn)   # W 延后入队
else:
    grad_weight_fn()                       # W 立即算
grad_input = grad_output @ weight          # B 立即算并返回上游
return grad_input, None                    # 对 weight 返回 None，避免重复累积
```

注意最后一行 `return grad_input, None`——对 `weight` 返回 `None`，是因为权重梯度已经通过 `weight.grad += ...` 手动累加，不能再让 autograd 再算一次。这正是 [u3-l3](./u3-l3-overlapped-forward-backward.md) 讲过的握手：`B` 立即、`W` 延后。

**(2) MyLinear：让线性层走自定义反向。** 仅一行 `forward` 改写：

[examples/example_dualpipev.py:37-39](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L37-L39) 把 `nn.Linear` 的前向替换为 `LinearFunc.apply(input, self.weight)`。注意它只传 `weight`、不传 `bias`（示例用 `bias=False`），如果你想支持 bias，需要在 `LinearFunc` 中相应处理。

**(3) PipelineStage：模块本体 + 钩子。** 一个前馈 block（两层线性 + gelu）：

[examples/example_dualpipev.py:42-52](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L42-L52) 是模块本体。注意所有线性层都用 `MyLinear`（而非原生 `nn.Linear`），这是约束 (3) 的体现——否则零气泡握手失效。

钩子是约束的核心，[examples/example_dualpipev.py:54-83](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L54-L83) 给出了标准实现，必须严格匹配引擎调用点的参数契约。引擎侧的调用在 [dualpipe/dualpipev.py:165-168](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L165-L168)：

```python
outputs0, loss0 = type(module0).overlapped_forward_backward(
    module0, inputs0, criterion0, labels0,
    module1, loss1, outputs1, output_grads1,
)
```

钩子八参数契约：

| 参数 | 含义 | 何时非空 |
|------|------|----------|
| `module0` / `module1` | 本次前向 / 反向用到的镜像模块 | 恒非空 |
| `inputs0` | 前向输入张量列表 | 恒非空（list） |
| `criterion0` / `labels0` | 损失函数与标签 | 仅当 module0 处于 last stage（first rank 且 phase0==1）时 criterion0 非空，否则 criterion0=None、labels0=[] |
| `loss1` | 反向要回传的标量损失 | 仅当 module1 处于 last stage 时非空 |
| `outputs1` / `output_grads1` | 反向的输出张量与其梯度 | 非 last stage 时非空（已剔除 `None` 梯度项） |

钩子返回 `(outputs0, loss0)`：`outputs0` 为前向输出（单张量会自动包成 list），`loss0` 仅在 last stage 非 `None`。钩子内部的反向分叉——`loss1.backward()`（last stage，标量作种子）vs `run_backward(outputs1, output_grads1)`（中间 stage，下游回传梯度作种子）——与引擎 [dualpipe/dualpipev.py:84-118](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L84-L118) 的 `_backward_compute_chunk` 完全一致，只是把这段逻辑从引擎搬到了用户钩子里。

**(4) 引擎侧的启用判定与隐式约束。** 引擎在初始化时推断是否启用重叠路径：

[dualpipe/dualpipev.py:23](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L23) 写明 `self.overlapped_forward_backward = type(modules[0]) == type(modules[1]) and hasattr(type(modules[0]), "overlapped_forward_backward")`。这暴露两条约束：

- **两镜像模块必须是同一类型**（`type(...) == type(...)`）。若两个镜像用不同类，重叠降级为串行。
- **钩子必须挂在类型上**（`hasattr(type(...), ...)`），且通常是 `@classmethod`，因为引擎用 `type(module0).overlapped_forward_backward(...)` 调用。

还有一条容易被忽视的约束藏在显存回收里。当 `return_outputs=False` 时，发送出去的输出张量会被排入 `to_free`，在下次提交后软释放（`tensor.data = torch.Tensor()`）。释放前引擎断言这些张量**不是视图**：

[dualpipe/dualpipev.py:227-231](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L227-L231) 的 `_free_tensors` 断言 `tensor._base is None`。这意味着**模块的 `forward` 不能返回输入的视图或切片**（如 `return x[:, :5]` 这类会产生 `._base` 的操作）。`F.linear` 与 `F.gelu` 都产生新张量，故 `PipelineStage` 合规。

> **约束清单小结（自定义模块必须满足）：**
> 1. 两镜像模块同类型，且类型上定义 `overlapped_forward_backward` classmethod（否则失去 F&B 重叠）。
> 2. 钩子严格匹配八参数契约，返回 `(outputs0, loss0)`，内部按 last stage 分叉反向。
> 3. 线性层用 `LinearFunc`/`MyLinear` 这类自定义 autograd，使 `W` 可延后、对 `weight` 返回 `None`（否则零气泡握手失效、或权重梯度被重复累积）。
> 4. `forward` 不返回视图张量（否则 `_free_tensors` 断言失败）。
> 5. 模块在 CUDA 上、输入输出形状与 `set_p2p_tensor_shapes` 一致（否则 P2P 通信形状不匹配）。

#### 4.1.4 代码实践

**实践目标**：直观感受约束 (1) 的后果——关闭钩子后，模块仍能正确训练，只是失去重叠。

**操作步骤**：

1. 阅读 [dualpipe/dualpipev.py:120-128](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L120-L128)，确认当 `self.overlapped_forward_backward` 为 `False` 时，引擎走 `_forward_compute_chunk(phase0)` + `_backward_compute_chunk(phase1)` 的串行分支。
2. 在本地 GPU 环境复制 `example_dualpipev.py`，把 `PipelineStage.overlapped_forward_backward` 方法**重命名**（例如改成 `overlapped_fb`），使 `hasattr` 判定为 `False`。
3. 运行 `python examples/example_dualpipev.py`。

**需要观察的现象**：程序**仍然通过全部校验**（loss 的 `torch.equal`、梯度的 `cal_diff < 1e-13`），因为串行分支在数值上等价；但训练吞吐会下降（失去前向/反向重叠）。

**预期结果**：校验全部 `assert` 通过，无任何报错。**待本地验证**（需要多 GPU）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `MyLinear` 换回原生 `nn.Linear`，校验还会通过吗？为什么？

> **答案**：从纯数值正确性看，前向与完整反向（含权重梯度）的结果与参考一致，loss 校验会通过。但问题出在零气泡握手：原生 `nn.Linear` 在反向时一次性算出 `grad_input` 和 `grad_weight`，无法把 `W` 单独延后。引擎在 step 8 会 `assert WeightGradStore.funcs_queue.empty()`——若 `WeightGradStore.put` 从未被调用，队列为空、断言恰好通过，所以校验**可能仍通过**，但 `WeightGradStore` 实际上没有起任何作用（W 没被延后，零气泡优化失效）。也就是说：换回原生 `nn.Linear` 不破坏正确性，但破坏零气泡能力。

**练习 2**：为什么钩子是 `@classmethod` 而不是普通方法？引擎为什么用 `type(module0).overlapped_forward_backward(...)` 调用，而不是 `module0.overlapped_forward_backward(...)`？

> **答案**：钩子需要同时操作两个实例 `module0` 与 `module1`（一次前向、一次反向分别用不同实例），普通绑定方法只能绑定到一个 `self`。做成 classmethod、由引擎显式传入两个实例作为参数，最自然。用 `type(module0).` 取类上的未绑定方法，与 `self.overlapped_forward_backward` 启用判定 (`hasattr(type(...), ...)`) 在判定与调用上保持一致。

**练习 3**：若某模块的 `forward` 末尾是 `return self.norm(x)` 而 `norm` 是 `LayerNorm`，会违反哪条约束？

> **答案**：不违反视图约束——`LayerNorm` 返回新张量（`._base is None`），与 `F.linear` 类似。视图约束只针对切片/reshape/view 等「复用底层存储」的操作。所以这个 `forward` 是合规的。

---

### 4.2 ref_step：单进程参考实现与对照验证

#### 4.2.1 概念说明

`ref_step` 是「标准答案发生器」：它在**单进程、串行**地按微批次顺序跑一遍完整模型，得到 loss 与梯度。随后分布式 `step` 跑出来的 loss、output、grad 都要和它逐项核对。这套「独立实现做对照」是验证分布式并行正确性的标准范式——因为分布式调度极其复杂（八步循环、P2P 通信、零气泡延后），任何一处 off-by-one 或张量生命周期错误，都很难直接推理发现，但与一份简单参考实现一比就会立刻暴露。

#### 4.2.2 核心流程

```
ref_step(x, l, full_modules, num_chunks):
  for 每个微批次 (micro_x, micro_l) in zip(x.chunk, l.chunk):
      micro_y = full_modules(micro_x)      # 完整模型前向
      loss    = criterion(micro_y, micro_l)
      loss.backward()                       # 梯度累加到 full_modules.parameters()
      收集 micro_y、loss
  返回 (stack(losses), cat(outputs))
```

它做的事和普通单卡训练**完全一样**：微批次循环、前向、算 loss、反向（梯度自动累加）、收集输出。`full_modules` 是把 `pp_size*2` 个 `PipelineStage` 串成的 `nn.Sequential`（见 [examples/example_dualpipev.py:127](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L127)），代表「没有被切分、没有流水线」的完整模型。

#### 4.2.3 源码精读

[examples/example_dualpipev.py:90-100](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L90-L100) 是 `ref_step` 主体。两个细节值得注意：

- 用 `x.chunk(chunks)` 切微批次（与引擎 `scatter` 的 `tensor_split` 在可整除时等价）。
- `loss.backward()` **不** `zero_grad`，所以梯度是各微批次的**累加和**——这与分布式侧「多 chunk 的梯度累加到同一参数」语义一致，是后续梯度可比的基础。

**对照的两层检查**：

- **loss / output 校验用位级精确**：[examples/example_dualpipev.py:151-156](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L151-L156) 训练步后用 `torch.equal(loss, loss_ref)`、`assert outputs is None`（因 `return_outputs=False`）。推理步 [examples/example_dualpipev.py:167-173](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L167-L173) 再用 `torch.equal` 比输出。loss 与每个微批次的输出都是**自包含的标量/张量计算**，不涉及跨微批次累加，示例又把它们按一致的顺序排列，故可位级比较。
- **梯度校验用相对度量**：[examples/example_dualpipev.py:158-161](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L158-L161) 遍历每个参数，`assert cal_diff(p.grad, p_ref.grad) < 1e-13`。梯度是「多个微批次贡献的累加和」，而分布式侧的累加顺序（受八步调度与 `WeightGradStore` FIFO 影响）与参考侧的串行顺序不同；浮点加法**不满足结合律**，故梯度无法位级相等，只能用 `cal_diff`（详见 4.3）。

**模型切分与权重搬运**：参考用 `full_modules`（`pp_size*2` 个 stage 串联），分布式侧每个 rank 只持有其中两个镜像 stage。为了对照，示例把对应 stage 的权重 `load_state_dict` 到本地模块：

[examples/example_dualpipev.py:126-141](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L126-L141)。注意 `local_full_modules = nn.Sequential(full_modules[rank], full_modules[pp_size*2-1-rank])`——rank r 持有 stage `r` 与 stage `pp_size*2-1-r` 这一对镜像（V 型对称），且每个 stage 在全局唯一出现一次，所以梯度对照是**逐 rank 独立**的，无需跨 rank 聚合。

> **与 DualPipe 版的对比**：DualPipe（双向）中同一个 stage 会被两个 rank 各持一次（正向流与反向流各用一次），其梯度分布在两个 rank 上，因此梯度校验前要先 `dist.all_gather` 把两份梯度收集相加，见 [examples/example_dualpipe.py:168-177](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L168-L177)。DualPipeV 因 V 型折叠、stage 全局唯一，省去了这一步——这也是用 V 版做本讲模板的原因之一。

#### 4.2.4 代码实践

**实践目标**：把 `ref_step` 当成「可独立运行的小工具」单独理解，不依赖任何分布式。

**操作步骤**：

1. 在单 GPU / CPU 上写一段脚本：构造 `full_modules = nn.Sequential(PipelineStage(512), PipelineStage(512))`，随机生成 `x, l`，调用 `ref_step(x, l, full_modules, chunks=4)`。
2. 打印返回的 `loss`（形状应为 `(4,)`）与 `output`（形状应为 `(N, seq, hidden)`）。
3. 改变 `chunks`（如 4 → 8），观察：输出 `y` 是否与 `chunks` 无关？loss 总和是否与 `chunks` 无关？

**需要观察的现象**：

- `output` 与 `chunks` 无关（前向是确定性的，切分只影响反向累加顺序）。
- loss 的逐元素值也与 `chunks` 无关（每个微批次 loss 独立）。

**预期结果**：两次 `chunks` 得到的 `output` `torch.equal` 为 `True`，`loss` `torch.equal` 为 `True`。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ref_step` 里不调用 `optimizer.zero_grad()`，而是让梯度累加？

> **答案**：参考实现要复现「整批训练一次」的梯度语义——整批所有微批次的梯度之和。`loss.backward()` 默认累加到 `.grad`，循环跑完 `num_chunks` 次后，`.grad` 正是全部微批次之和，与分布式侧「各 chunk 梯度累加」一致。若中途 `zero_grad`，就只会保留最后一个微批次的梯度，失去对照意义。

**练习 2**：`ref_step` 返回的 `loss` 是 `torch.stack(losses)`（形状 `(num_chunks,)`），而非标量平均。引擎侧 `DualPipeV.step` 返回的 loss 也是逐微批次的吗？

> **答案**：是的。[dualpipe/dualpipev.py:401-403](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L401-L403) 中 first rank 用 `torch.stack(self.loss_chunks)` 返回逐微批次 loss，与 `ref_step` 的 `torch.stack(losses)` 对齐，所以才能 `torch.equal` 位级比较。实际训练时使用者可再对它 `.mean()` 或加权求和。

**练习 3**：若把 `criterion` 改成对输出做了 `in-place` 操作，会发生什么？

> **答案**：`ref_step` 与分布式侧都会受影响，可能破坏 autograd 的版本追踪、报 `in-place` 错误。示例中 [examples/example_dualpipev.py:86-87](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L86-L87) 的 `criterion` 特意用了 `.clone()`，正是为了规避 in-place 风险、保证 loss 张量可安全 `backward` 与 `detach_`。

---

### 4.3 cal_diff：余弦差异度量

#### 4.3.1 概念说明

`cal_diff` 是一个衡量两个张量「相对差异」的标量度量，专门用来对照梯度。它的设计动机是：

- 梯度因浮点累加顺序不同，**不可能位级相等**，`torch.equal` 永远 `False`。
- 直接用 `(x - y).abs().max()`（最大绝对误差）会受梯度数值尺度影响——梯度大时误差也大，难以定一个普适阈值。
- 需要一个**尺度无关**（scale-invariant）、**对方向敏感**的度量：两个张量「几乎平行且长度相近」时差异接近 0。

`cal_diff` 正是这样一个度量：它本质上刻画两个张量的「夹角偏离 + 长度偏离」的综合，与余弦相似度同源。

#### 4.3.2 核心流程

给定两个展平为向量的张量 \(x, y\)，`cal_diff` 定义为：

\[
d(x, y) \;=\; 1 \;-\; \frac{2\,(x\cdot y)}{\lVert x\rVert^{2} + \lVert y\rVert^{2}}
\]

其中 \(x\cdot y = \sum_i x_i y_i\)，\(\lVert x\rVert^{2} = \sum_i x_i^{2}\)。

**一个关键恒等式**：注意 \(\lVert x-y\rVert^{2} = \lVert x\rVert^{2} + \lVert y\rVert^{2} - 2(x\cdot y)\)，于是

\[
d(x, y) \;=\; \frac{\lVert x-y\rVert^{2}}{\lVert x\rVert^{2} + \lVert y\rVert^{2}}
\]

也就是说 `cal_diff` 等价于「平方欧氏距离」除以「两向量范数平方之和」。由此可直接读出它的性质：

- \(d=0\)：\(x=y\)，完全一致。
- \(d=2\)：\(x=-y\)，方向完全相反（此时 \(\lVert x-y\rVert^{2} = 4\lVert x\rVert^{2}\)，分母 \(=2\lVert x\rVert^{2}\)，比值 \(=2\)）。
- 它是尺度无关的：把 \(x, y\) 同乘一个常数，\(d\) 不变。

**与余弦相似度的关系**：当 \(\lVert x\rVert=\lVert y\rVert\) 时，分母 \(=2\lVert x\rVert\lVert y\rVert\)，此时

\[
d \;=\; 1 \;-\; \frac{x\cdot y}{\lVert x\rVert\lVert y\rVert} \;=\; 1 - \cos\theta
\]

即退化为 \(1-\) 余弦相似度。一般情况下，它兼顾了「角度」与「长度」两方面的偏离，所以称「余弦差异度量」是对其精神（强调方向）的准确描述。

**为什么阈值取 \(10^{-13}\)？** float32 的相对精度约 \(10^{-7}\)（约 7 位有效数字）。参考与分布式路径的逐元素差异量级 \(\epsilon \sim 10^{-7}\)。由于度量是**逐元素平方再求和之比**：

\[
d \;\approx\; \frac{\sum_i (\epsilon\, x_i)^{2}}{\sum_i x_i^{2} + \sum_i x_i^{2}} \;\sim\; \frac{\epsilon^{2}\sum_i x_i^{2}}{2\sum_i x_i^{2}} \;\sim\; \frac{\epsilon^{2}}{2} \;\sim\; \frac{10^{-14}}{2} \;\sim\; 5\times10^{-15}
\]

所以即便逐元素有 \(10^{-7}\) 的相对误差，聚合后 \(d\) 也只有 \(10^{-15}\) 量级，**远小于 \(10^{-13}\)**。换言之，\(10^{-13}\) 是一个「既卡得住真实 bug（差异会瞬间飙升到 \(10^{-2}\) 以上），又不被浮点噪声误伤」的宽松-紧致平衡点。`cal_diff` 内部还会先把两个张量升到 `double` 再算（见 4.3.3），避免度量自身的 float32 舍入污染判断。

#### 4.3.3 源码精读

[examples/example_dualpipev.py:103-106](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L103-L106) 是全部实现，只有 3 行：

```python
def cal_diff(x: torch.Tensor, y: torch.Tensor) -> float:
    x, y = x.double(), y.double()                                  # 升 double，避免度量自身舍入
    cos_diff = 1 - 2 * (x * y).sum().item() / (x * x + y * y).sum().item()
    return cos_diff
```

逐项对照：

- `(x * y).sum()` = \(x\cdot y\)。
- `(x * x + y * y).sum()` = \(\lVert x\rVert^{2} + \lVert y\rVert^{2}\)。
- `.item()` 把标量张量转 Python float。

**调用点**见 [examples/example_dualpipev.py:159-161](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L159-L161)：

```python
for ((n, p), p_ref) in zip(local_modules.named_parameters(), local_full_modules.parameters()):
    assert cal_diff(p.grad, p_ref.grad) < 1e-13
```

这里 `local_modules` 是分布式侧本 rank 的两个镜像 stage，`local_full_modules` 是参考模型中对应的那两个 stage。逐参数对照梯度。注意 `named_parameters()` 与 `parameters()` 的迭代顺序一致（都按注册顺序），故 `zip` 配对正确——这要求**自定义模块的参数注册顺序在分布式侧与参考侧一致**（用 `load_state_dict` 搬运保证了这一点）。

#### 4.3.4 代码实践

**实践目标**：脱离分布式，单独验证 `cal_diff` 的数学性质。

**操作步骤**（单 GPU/CPU 即可）：

```python
import torch
def cal_diff(x, y):
    x, y = x.double(), y.double()
    return 1 - 2 * (x * y).sum().item() / (x * x + y * y).sum().item()

x = torch.randn(1000)
print(cal_diff(x, x))                       # 预期 ~0
print(cal_diff(x, -x))                      # 预期 ~2
print(cal_diff(x, x * 3))                   # 预期 1 - 2*3/(1+9) = 0.4
print(cal_diff(x, x + 1e-7 * torch.randn_like(x)))  # 预期 ~5e-15
```

**需要观察的现象**：

- `cal_diff(x, x)` ≈ 0（可能因 double 运算有 ~1e-30 量级的舍入）。
- `cal_diff(x, -x)` ≈ 2。
- `cal_diff(x, x*3)` = 0.4，验证「同向但长度不同」也有差异。
- 加 \(10^{-7}\) 噪声后，`cal_diff` ≈ \(5\times10^{-15}\)，验证 4.3.2 的量级估算。

**预期结果**：四项均符合上述数值。本实践可在 CPU 直接运行验证。

#### 4.3.5 小练习与答案

**练习 1**：把 `cal_diff` 里的 `x.double(), y.double()` 去掉，对很大的 float32 张量会发生什么？

> **答案**：`cal_diff` 自身会引入 float32 的累加舍入误差。当张量元素很多时，`(x*x).sum()` 等大数求和在 float32 下精度损失明显，可能让原本应接近 0 的 \(d\) 偏移到 \(10^{-7}\) 量级，反而**触发误报**（超过 \(10^{-13}\) 阈值）。升 double 是为了让「度量本身」的精度远高于被测误差，不污染判断。

**练习 2**：为什么不用 `torch.allclose(p.grad, p_ref.grad, atol=..., rtol=...)` 而要自造 `cal_diff`？

> **答案**：`allclose` 的判定是逐元素的（`|x-y| <= atol + rtol*|y|`），需要为不同尺度、不同形状的参数各定一套 `atol/rtol`，且对「个别元素小偏差」敏感。`cal_diff` 是**全局聚合**的单一标量，尺度无关、形状无关，一个阈值 \(10^{-13}\) 通吃所有参数，工程上更省心、也更贴合「整体方向是否一致」的语义。

**练习 3**：若两个梯度方向正确但整体被错误地放大了 2 倍（\(y=2x\)），`cal_diff` 报多少？这说明它对「幅度错误」敏感吗？

> **答案**：\(d(x, 2x) = 1 - 2\cdot(2\lVert x\rVert^{2})/(\lVert x\rVert^{2}+4\lVert x\rVert^{2}) = 1 - 4/5 = 0.2\)。这是一个远大于 \(10^{-13}\) 的值，会立刻被断言抓住。说明 `cal_diff` **对幅度错误同样敏感**（因为它同时包含长度信息），不仅对方向错误敏感——这正是一个好的梯度校验度量应有的性质。

---

## 5. 综合实践

把三个最小模块串成一个完整任务：**修改 `PipelineStage` 并让全套校验继续通过，从而内化自定义模块的全部约束。**

### 实践目标

在不破坏 DualPipeV 正确性的前提下，自定义一个新结构的前馈模块，证明只要满足 4.1 的约束清单，引擎就能正确调度它。

### 操作步骤

1. **复制模板**：复制 `examples/example_dualpipev.py` 为 `my_example.py`（放任意目录，不要改原文件）。

2. **改造 PipelineStage**（任选其一，由易到难）：

   - **换激活函数**：把 [examples/example_dualpipev.py:50](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L50) 的 `F.gelu` 换成 `F.relu` 或 `F.silu`。
   - **增减层数**：在 `PipelineStage.__init__` 里加一个 `self.linear3 = MyLinear(hidden_size, hidden_size, bias=False)`，并在 `forward` 末尾接 `x = self.linear3(x)`（注意保持输入输出 hidden_size 一致，否则会破坏 P2P 张量形状）。
   - **加残差**：在 `forward` 末尾 `return x + identity`（`identity` 保存输入）。注意：`x + identity` 产生新张量（非视图），满足约束 (4)。

3. **同步修改参考模型**：因为你改了 `PipelineStage`，`full_modules`（[examples/example_dualpipev.py:127](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L127)）和分布式侧 `local_modules`（[examples/example_dualpipev.py:138](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L138)）会自动用新结构——因为它们都是 `PipelineStage(hidden_size)` 实例，无需额外改动。这正是把模块封装成 `PipelineStage` 的好处。

4. **运行**：`python my_example.py`（需多 GPU）。入口 [examples/example_dualpipev.py:180-183](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L180-L183) 会从 GPU 总数向下逐个尝试不同 `pp_size`。

### 需要观察的现象

- 三类断言全部通过：
  - first rank：`torch.equal(loss, loss_ref)`（[L153](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L153)、[L169](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L169)）、推理步 `torch.equal(outputs, output_ref)`（[L170](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L170)）。
  - 所有 rank、所有参数：`cal_diff(p.grad, p_ref.grad) < 1e-13`（[L160](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L160)）。
- 若你不慎把线性层换回原生 `nn.Linear`、或让 `forward` 返回了切片视图，会看到相应的 `assert funcs_queue.empty()` 或 `_free_tensors` 的视图断言报错（或零气泡静默失效）。

### 预期结果

模块结构改变后，loss 与梯度的对照**仍全部通过**，证明：只要满足「同类型 + 钩子契约 + LinearFunc 握手 + 非视图输出 + 形状一致」五条约束，DualPipeV 对模块内部结构完全透明。**待本地验证**（多 GPU 环境）。

### 拓展思考

- 把新增线性层的 `MyLinear` 换成原生 `nn.Linear`，观察 step 8 的 `assert WeightGradStore.funcs_queue.empty()` 是否仍通过，体会「零气泡失效但不报错」的隐蔽性。
- 尝试让 `forward` 返回 `x[:, 0]`（切片视图），触发 `_free_tensors` 的 `tensor._base is None` 断言，直观理解约束 (4)。

---

## 6. 本讲小结

- DualPipe/DualPipeV 对被调度模块「内部结构透明」，但要求满足一组契约：**两镜像同类型 + `overlapped_forward_backward` classmethod + 线性层用 `LinearFunc` 握手 `WeightGradStore` + forward 不返回视图 + 形状与 P2P 约定一致**。
- `LinearFunc` 是自定义模块的地基：它把反向拆成「立即返回的输入梯度 B」与「依 `WeightGradStore.enabled` 决定立即算或延后入队的权重梯度 W」，对 `weight` 返回 `None` 防止重复累积。
- `ref_step` 是单进程串行参考实现，复现「整批微批次累加梯度」的语义，作为分布式结果的标准答案。
- 对照验证分两层：loss/输出因自包含、顺序一致，用 `torch.equal` 位级比较；梯度因跨微批次累加顺序不同、浮点不满足结合律，用 `cal_diff < 1e-13` 相对度量比较。
- `cal_diff` 等价于 \(\lVert x-y\rVert^{2}/(\lVert x\rVert^{2}+\lVert y\rVert^{2})\)，尺度无关、对方向与幅度都敏感；\(10^{-13}\) 阈值正好卡在「真实 bug 会飙升、浮点噪声不误伤」的区间。
- 修改 `PipelineStage` 后校验仍全通过，证明只要守住约束清单，就能放心地把自己的模型接入 DualPipeV。

## 7. 下一步学习建议

- **回到真实模型**：本讲的 `PipelineStage` 只是玩具 block。建议尝试把一个真实的小 Transformer block（含 attention + MLP）封装成满足上述五约束的 `PipelineStage`，特别注意 attention 里的线性层都要走 `LinearFunc`，并实现对应的 `overlapped_forward_backward`。
- **对比 DualPipe 版**：阅读 [examples/example_dualpipe.py](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py) 的梯度校验（[L168-177](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L168-L177)），理解双向调度为何需要 `all_gather` 跨 rank 聚合梯度，与本讲 V 版的逐 rank 独立校验形成对照。
- **深入通信与显存**：若想进一步优化自定义模块，可重读 [u3-l4（通信原语）](./u3-l4-comm-primitives-and-composite-ops.md) 与 `dualpipe/comm.py`，理解 `set_p2p_tensor_shapes` 如何约束你的输入输出形状，以及 `to_free` 的显存回收时机如何影响模块输出的张量生命周期。
- **数值确定性**：本讲的校验依赖固定随机种子与 `CUBLAS_WORKSPACE_CONFIG`（[L114-115](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L114-L115)）。在真实验证你的自定义模块时，务必复用这些设置，否则连「参考实现」自身都不可复现。
