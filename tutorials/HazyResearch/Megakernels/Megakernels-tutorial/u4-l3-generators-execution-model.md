# Generators 执行模型与 MK_Interpreter

> 本讲对应手册单元 **U4·L3**，承接 [U4·L2：tensorize_instructions——序列化为指令张量]（`u4-l2-tensorize-instructions.md`）。建议你已经知道 `tensorize_instructions` 是如何把各 SM 队列补齐 `NoOp`、张量化成 `[num_sms, max_queue_len, 32]` 的 int32 张量，并挂上 `[num_sms, max_queue_len, 128]` 的 `timings` 张量的；本讲要回答的下一个问题是：**这些张量准备好之后，"解码生成一个 token" 到底是怎么跑起来的？**

---

## 1. 本讲目标

学完本讲后，你应当能够：

1. 画出 `MK_Generator.run` 的 **三段式单步前向**：`embed（填 hidden_states）→ interpret（跑 megakernel）→ argmax（出 token）`，并说清 `globs.hidden_states`、`globs.pos_id`、`globs.logits` 这三个张量分别在哪个环节被写、被读。
2. 说清 `replace_with_noops` / `skip_mk` / `skip_rest` 三个开关各自**砍掉了三段式中的哪一段**，以及它们组合起来如何把一次解码的耗时**拆解**成「纯 megakernel 调度开销」「embed+argmax 开销」「纯 Python 循环开销」。
3. 解释 `replace_with_noops` 为什么能用来**测量纯内核开销**：因为它把整条指令带清零，而 `NoOp` 的 opcode 正是 `0`，于是内核仍在「真的启动、真的走完 VM 调度」，但一条真正的计算都没做。
4. 读懂 `MK_Interpreter` 如何用 `sys.path` + `from mk_llama import mk_llama` **动态加载编译好的 CUDA 扩展**，以及 `LatencyMK_Interpreter.interpret` 如何按**固定位置顺序**把 `globs` 的每个字段喂给 `mk_func`，并理解「参数对齐」是与编译产物绑死的契约。

---

## 2. 前置知识

### 2.1 自回归解码的「单步」

大模型一次只预测下一个 token。所谓「解码一步」，就是：

> 给定**当前位置的输入 token id** 和**当前位置编号 `pos_id`**，算出**下一个 token id**。

这一步在 [U2·L1](u2-l1-three-execution-modes.md) 里已经用三种模式（`torch` / `pyvm` / `mk`）对照讲过。本讲只聚焦 **`mk` 模式**的这一步：它不是用 PyTorch 算的，而是把整个 transformer 层 + lm_head **编译进一个 megakernel**，一次 CUDA launch 跑完。

### 2.2 `globs`：内核的「全局状态对象」

在 [U3·L1](u3-l1-base-globals-and-instruction-set.md) 里我们介绍过 `BaseGlobals`（[megakernels/instructions.py:10-69](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L10-L69)）。你可以把它理解成一块「大黑板」，上面钉着 megakernel 需要的所有东西：

- **权重**：`qkv_proj_weights`、`o_proj_weights`、`up_proj_weights` ……（整层 stack 在一起）。
- **KV cache**：`k_cache`、`v_cache`。
- **激活缓冲**：`hidden_states`、`post_ln_rope_q`、`attn_out`、`silu_out`、`logits` ……（预分配的 scratch buffer）。
- **VM 状态**：`barriers`（跨 SM 同步计数器）、`instructions`（指令带）、`timings`（每条指令的计时槽）。
- **标量**：`pos_id`、`attn_scale`、`rms_norm_eps` ……

megakernel 的设计是：**不接收一堆临时参数，而是直接读写这块黑板**。所以「跑一次解码」≈「把当前输入写进黑板的几个槽位 → 调一次内核读黑板、写回黑板 → 从黑板的 `logits` 槽位读结果」。

### 2.3 三块 VM 张量：`barriers` / `instructions` / `timings`

这三块是「megakernel 当作虚拟机来用」的关键，也是本讲反复出现的对象。它们都由前置步骤准备好：

| 张量 | 形状（latency 设置） | 谁来填 | 作用 |
| --- | --- | --- | --- |
| `instructions` | `[num_sms, max_queue_len, 32]` int32 | `tensorize_instructions`（[scheduler.py:307](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L307)） | 每个 SM 的「指令队列」，每条指令占 32 个 int32 字 |
| `timings` | `[num_sms, max_queue_len, 128]` int32 | `tensorize_instructions`（[scheduler.py:308](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L308)） | 每条指令的计时槽（`TIMING_SLOTS=128`，见 [scheduler.py:14](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L14)） |
| `barriers` | `[num_hidden_layers, 10, num_heads]` int32 | `make_globals` 初始化、`MK_Generator.fill()` 每步重置 | 跨 SM 同步计数器，决定某条指令「能不能开始执行」 |

> 小贴士：`barriers` 的第 2 维是 `10`（[latency/scheduler.py:55](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L55)），注释写「more than the number of opcodes we have」——预留得比当前 opcode 种类多。`barriers` 初始值是 `0`（同文件 [L59](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L59)），每步解码前会被 `fill()` 重新刷成 `barrier_fill_val`。

### 2.4 「位置参数 = 编译契约」

`mk_func` 是一段**编译好的 CUDA 扩展函数**（一个 C 函数包成的 Python callable）。它**不接收关键字参数**，所有张量按**固定顺序**逐个传入。这意味着：Python 这端调用 `mk_func(a, b, c, ...)` 的顺序，必须**逐位**对上 C++ 那头 `mk_llama` 的形参顺序。这就是本讲标题里「参数对齐」的全部含义——它是一份和编译产物绑死的、没有名字只有位置的契约。

---

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [megakernels/generators.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py) | 本讲主角：`MK_Generator`（`run` / `fill` / `replace_with_noops` / `generate`）全在这里 |
| [megakernels/mk.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py) | `MK_Interpreter` 基类 + `get_mk_func`：动态加载编译好的 `mk_llama` |
| [megakernels/demos/latency/mk.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py) | `LatencyMK_Interpreter.interpret`：把 `globs` 按位置顺序喂给 `mk_func` 的具体实现 |
| [megakernels/scripts/generate.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py) | 命令行入口：`noops` / `skip_mk` / `skip_rest` 三个开关在这里接到 `MK_Generator` 上 |
| [megakernels/instructions.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py) | `BaseGlobals` 字段定义、`NoOp.opcode()==0`（解释 `replace_with_noops` 的关键） |
| [megakernels/demos/latency/scheduler.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py) | `make_globals`：预分配 `hidden_states` / `logits` / `barriers` 等缓冲的形状 |

---

## 4. 核心概念与源码讲解

### 4.1 MK_Generator.run：embed → mk → argmax 三段式单步前向

#### 4.1.1 概念说明

`mk` 模式下「解码一步」的全部逻辑浓缩在一个方法里：`MK_Generator.run(input_ids, pos_id)`（[generators.py:121-143](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L121-L143)）。它做三件事，正好对应黑板模型的「写输入 → 跑内核 → 读输出」：

1. **embed**：把当前 token id 查表成 embedding，写进黑板的 `hidden_states` 槽位。
2. **mk**：刷新 `barriers`、设置 `pos_id`，然后调 `interpreter.interpret(globs)` 让 megakernel 把 transformer 主体跑完，结果（logits）落回黑板的 `logits` 槽位。
3. **argmax**：从 `logits` 里取最大值下标，就是下一个 token。

注意一个**关键设计选择**：在 `mk` 模式里，**lm_head（把最后一层 hidden 算成 logits）是被编译进 megakernel 的**，所以 `run` 直接读 `globs.logits` 就行，**不需要**在 Python 里再跑一次 `model.lm_head`。这与 `pyvm` 模式不同（见 4.1.5）。

#### 4.1.2 核心流程

伪代码（省略开关，开关版本见 4.2）：

```
run(input_ids, pos_id):
    # —— 第 1 段：embed ——
    post_embedding = model.model.embed_tokens(BatchState(input_ids))
    globs.hidden_states[:] = post_embedding.hidden_states.squeeze(1)   # 写黑板

    # —— 第 2 段：mk ——
    self.fill()                  # 重置 barriers
    globs.pos_id = pos_id        # 告诉内核「现在是第几步」
    self.interpreter.interpret(globs)   # 一次 CUDA launch 跑完整层 + lm_head
                                        # 结果写回 globs.logits

    # —— 第 3 段：argmax ——
    logits = globs.logits
    output_ids = torch.argmax(logits, dim=-1)
    return output_ids
```

用「黑板读写」的视角串起来：

```
            写 hidden_states, pos_id                 读 logits
  embed  ───────────────────────►  [ 黑板 globs ]  ───────────────────►  argmax
                                   ▲ interpret(megakernel 读黑板、写黑板) ▲
```

#### 4.1.3 源码精读

逐行看 `run`（[generators.py:121-143](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L121-L143)）：

**第 1 段——把 embedding 原地写进预分配缓冲：**

```python
post_embedding: BatchState = self.model.model.embed_tokens(batch_state)   # 查表
hiddens = post_embedding.hidden_states
assert hiddens is not None
self.schedule.globs.hidden_states[:] = hiddens.squeeze(1)                  # 原地拷贝
```

来自 [generators.py:127-130](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L127-L130)。两个细节：

- `hidden_states` 是在 `make_globals` 里预分配的 **1D** 缓冲，形状就是 `[hidden_size]`（[latency/scheduler.py:79](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L79)：`make_buffer(config.hidden_size)`）。
- 用 `[:] =` 而不是 `=`：这是**原地拷贝**到那块固定显存，确保内核读到的是新值而不是一个新分配的临时张量。`.squeeze(1)` 去掉长度为 1 的序列维（解码一步只有 1 个 token）。

**第 2 段——刷新 VM 状态并跑内核：**

```python
self.fill()                              # barriers ← barrier_fill_val
self.schedule.globs.pos_id = pos_id      # 设置位置编号
self.interpreter.interpret(self.schedule.globs)   # 跑 megakernel
```

来自 [generators.py:132-135](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L132-L135)。`fill()` 把 `barriers` 整块刷成 `barrier_fill_val`（默认 `0`），见 [generators.py:115-116](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L115-L116)——**这是每步解码前必须做的「VM 复位」**：上一步内核执行时会把 `barriers` 消费/递减，下一步必须重置成初值，否则同步逻辑会错乱。`pos_id` 是一个普通标量字段（[instructions.py:45](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L45)），内核靠它知道当前序列长度（`seq_len = pos_id + 1`，这点在 [U3·L2](u3-l2-latency-instruction-set.md) 的 `PartialAttention.cost` 里也用到）。

**第 3 段——argmax 出 token：**

```python
logits = self.schedule.globs.logits
output_ids = torch.argmax(logits, dim=-1)
return output_ids
```

来自 [generators.py:140-143](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L140-L143)。`logits` 同样是预分配缓冲（形状 `[vocab_size]`，见 [latency/scheduler.py:91](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L91)），内核把 lm_head 的结果写进它，Python 这头直接 `argmax`。

#### 4.1.4 代码实践：追踪 `generate` → `run` 的调用链

**实践目标**：看清楚「解码 N 步」是如何退化成「调用 N 次 `run`」的，并验证每步的 `pos_id` 计算。

**操作步骤**：

1. 打开 [generators.py:145-163](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L145-L163) 的 `MK_Generator.generate`。
2. 它就是一个 `for i in range(ntok)` 循环，循环体里：
   - `input_token_pos = ntok_already_generated + i - 1`
   - `pos_id = prompt_len + ntok_already_generated + i - 1`
   - `output_ids = self.run(input_ids, pos_id=pos_id)`
   - 把结果写回 `output_tokens[:, output_token_pos]`
3. 手算一个例子：`prompt_len=10`、`ntok_already_generated=1`（prefill 已经吐了第 1 个 token）、`i=0`：
   - `input_token_pos = 1 + 0 - 1 = 0`（读 prefill 产出的那个 token）
   - `pos_id = 10 + 1 + 0 - 1 = 10`（第 11 个位置，0-indexed）
   - `output_token_pos = 1`

**需要观察的现象**：`pos_id` 随 `i` 严格递增，每步 +1；`input_token_pos` 总是「上一步写出的位置」。

**预期结果**：解码第 `i` 步时，`pos_id` 的通项是：

\[
\text{pos\_id}(i) = \text{prompt\_len} + \text{ntok\_already\_generated} + i - 1
\]

这是一条完全自洽的「单步循环 → 单步前向」调用链：`generate`（解码循环）每步调一次 `run`（单步前向），而 `run` 内部就是 embed→mk→argmax。

> 待本地验证：若你有 GPU 且已编出 `mk_llama`，可在 `run` 末尾 `return output_ids` 前加一行 `print(int(pos_id), int(output_ids))`，跑 `mode=mk ntok=3`，核对打印的 `pos_id` 是否与手算一致。

#### 4.1.5 小练习与答案

**练习 1**：`MK_Generator.run` 为什么**没有**出现 `self.model.lm_head(...)` 这样的调用，而 `pyvm` 模式的 `run` 却有？

**参考答案**：因为 `mk` 模式把 lm_head 编译进了 megakernel——内核跑完后 logits 已经在 `globs.logits` 里了，直接 `argmax` 即可（[generators.py:140-141](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L140-L141)）。而 `PyVM_Generator.run` 是用 PyTorch 函数逐条解算指令的参考虚拟机，lm_head 没被「编进」任何内核，所以要在 Python 里显式调 `self.model.lm_head(post_embedding)`（[generators.py:197](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L197)）。

**练习 2**：`self.schedule.globs.hidden_states[:] = hiddens.squeeze(1)` 里，如果把 `[:] =` 改成 `=`，会发生什么？

**参考答案**：`=` 会把 `self.schedule.globs.hidden_states` 这个**属性**指向一个**新的临时张量**，而 megakernel 实际读的是「预分配的那块固定显存地址」（`mk_func` 拿到的是那个地址）。属性换指向后，内核读到的仍是旧缓冲，embedding 更新对内核不可见。`[:] =` 保证**原地写**进内核知道的那块显存。

---

### 4.2 replace_with_noops / skip_mk / skip_rest：三类「空跑」开关与纯内核开销测量

#### 4.2.1 概念说明

`MK_Generator.__init__` 接受三个看似奇怪、实则精心设计的开关（[generators.py:101-103](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L101-L103)）：`skip_mk`、`skip_rest`，外加一个可单独调用的方法 `replace_with_noops`。它们的存在不是为了改变「正确结果」，而是为了**性能分解（performance breakdown）**——把一次解码的耗时拆成几块，定位瓶颈。

回顾 4.1 的三段式：`embed → mk → argmax`。三个开关分别「关掉」其中一部分：

| 开关 | 作用 | 关掉的段落 | 测到的是 |
| --- | --- | --- | --- |
| `replace_with_noops()` | 把 `instructions` 清零 | 不关段，但让内核「全 NoOp 跑一遍」 | **纯 megakernel 调度开销**（启动内核 + 走 VM + 同步，零真实计算） |
| `skip_mk=True` | 跳过 `interpreter.interpret` | 关掉 `mk` 段 | embed + argmax + Python 开销（**不含**内核） |
| `skip_rest=True` | 跳过 embed 段**和** argmax 段 | 关掉 `embed` + `argmax` | 纯 Python 循环 + `fill()` 开销 |

#### 4.2.2 核心流程

带着开关重看 `run`（[generators.py:121-143](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L121-L143)），它其实有三个条件分支：

```
run(input_ids, pos_id):
    if not skip_rest:        # ← embed 段被 skip_rest 包住
        ... embed, 写 hidden_states ...

    self.fill()              # ← 始终执行（VM 复位）
    globs.pos_id = pos_id    # ← 始终执行
    if not skip_mk:          # ← mk 段被 skip_mk 包住
        interpreter.interpret(globs)

    if skip_rest:            # ← argmax 段被 skip_rest 包住
        return input_ids     #    （直接返回输入，根本不读 logits）

    logits = globs.logits
    output_ids = torch.argmax(logits, dim=-1)
    return output_ids
```

于是组合出四种「空跑」profile（设满跑耗时为 \(T_{\text{full}}\)）：

| 组合 | 执行了什么 | 测到的耗时 | 物理含义 |
| --- | --- | --- | --- |
| 全开（默认） | embed + fill + **真内核** + argmax | \(T_{\text{full}}\) | 真实一步解码 |
| `noops` | embed + fill + **全 NoOp 内核** + argmax | \(T_{\text{noop}}\) | 内核调度骨架（零计算） |
| `skip_mk` | embed + fill + argmax（**不启动内核**） | \(T_{\text{no-mk}}\) | 除内核外的一切 |
| `skip_mk + skip_rest` | fill + 立即返回（**连 embed 都不做**） | \(T_{\text{loop}}\) | 纯 Python 循环 |

由此得到一条干净的加减法分解：

\[
T_{\text{full}} - T_{\text{noop}} \;\approx\; \text{真实模型计算耗时}
\]

\[
T_{\text{noop}} - T_{\text{no-mk}} \;\approx\; \text{启动内核 + 走完 VM 指令带 + 同步的开销}
\]

\[
T_{\text{no-mk}} - T_{\text{loop}} \;\approx\; \text{embed + argmax 耗时}
\]

这正是「测量纯内核开销」的做法。

#### 4.2.3 源码精读

**为什么 `replace_with_noops` 等价于「让内核全跑 NoOp」？**

```python
def replace_with_noops(self):
    self.schedule.globs.instructions.zero_()
```

来自 [generators.py:118-119](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L118-L119)。关键是 `instructions` 张量的**每一槽的第一字就是 opcode**。回忆 [U3·L1](u3-l1-base-globals-and-instruction-set.md) 里 `Instruction.serialize()` 的实现：

```python
def serialize(self):
    words = [self.opcode()]      # ← 第一字恒为 opcode
    for field in fields(self):
        ...
```

来自 [instructions.py:97-98](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L97-L98)。而 `NoOp` 的 opcode 正是 `0`：

```python
@dataclass
class NoOp(Instruction):
    @classmethod
    def opcode(cls) -> int:
        return 0
```

来自 [instructions.py:122-126](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L122-L126)。把 `instructions.zero_()` 后，**每个槽的第一字都是 0**，按上述序列化约定，等价于「整条指令带全是 `NoOp`」。

> 因此 `replace_with_noops` 之后，`interpret` 仍会真的启动一次 megakernel、内核仍会逐条读指令、仍会走完所有 `barriers` 同步、仍会写 `timings`——**只是没有一条指令做真实计算**。这正是「纯内核开销」的定义：扣掉所有「算」的成分后，剩下的「调度与同步」成本。

**开关如何接到命令行？** 看 [scripts/generate.py:44-46](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L44-L46)：

```python
noops: bool = False
skip_mk: bool = False
skip_rest: bool = False
```

以及 `match config.mode` 的 `mk` 分支（[generate.py:152-163](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L152-L163)）：

```python
case "mk":
    interpreter = make_mk_interpreter(config.setting, config.mk_dir)
    gen = MK_Generator(
        model, interpreter, schedule,
        barrier_fill_val=config.barrier_fill_val,
        skip_mk=config.skip_mk,
        skip_rest=config.skip_rest,
    )
    if config.noops:
        gen.replace_with_noops()
```

注意 `replace_with_noops` 是**构造后**单独调用的——它清零的是 `globs.instructions`，而这块张量在构造 `MK_Generator` 之前就已经被 `tensorize_instructions` 填好了真实指令（见 [U4·L2](u4-l2-tensorize-instructions.md)）。所以调用顺序是：先张量化出真实指令带 → 再决定要不要用 noops 把它清零。

#### 4.2.4 代码实践：测量纯内核开销

**实践目标**：用四种开关组合，量化「真实计算 / 内核调度 / embed+argmax / Python 循环」各占多少。

**操作步骤**（需要 GPU + 已编译 `mk_llama`）：

```bash
# 1) 满跑（真实一步解码）
python -m megakernels.scripts.generate mode=mk ntok=50

# 2) 全 NoOp 内核（纯内核调度开销）
python -m megakernels.scripts.generate mode=mk ntok=50 noops=True

# 3) 不启动内核（embed + argmax + Python）
python -m megakernels.scripts.generate mode=mk ntok=50 skip_mk=True

# 4) 连 embed 都不做（纯 Python 循环）
python -m megakernels.scripts.generate mode=mk ntok=50 skip_mk=True skip_rest=True
```

每条命令都会打印 `Average time: ...ms`（来自 [generate.py:185](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L185)）。

**需要观察的现象**：

- 第 2 条（noops）应该比第 1 条**显著更小**，但**明显大于**第 3 条（skip_mk）——差额就是「启动内核 + 走 VM + 同步」的纯开销。
- 第 4 条（skip_mk+skip_rest）应该是四者里最小，几乎是纯 Python `for` 循环 + `fill()` 的成本。
- `skip_mk` / `skip_rest` 下产出的 token 是**无意义的**（logits 是旧值/零、或直接返回输入），所以这两条**只看时间、不看输出正确性**。

**预期结果**：用 4.2.2 的三道减法算出三块开销。具体数值与硬件/模型强相关——**待本地验证**，重点是**相对量级**，而非绝对数字。

> 无 GPU 也能做的「源码阅读型实践」：对照 [generators.py:121-143](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L121-L143) 手工模拟四种开关下 `run` 实际会执行哪几行，列出每种的「执行行号集合」，验证上表的「执行了什么」一列。

#### 4.2.5 小练习与答案

**练习 1**：`skip_rest=True` 但 `skip_mk=False` 时，`interpret` 还会跑吗？跑的结果去哪了？

**参考答案**：会跑。`skip_rest` 只包住 embed 段（[generators.py:122](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L122)）和 argmax 段（[generators.py:137-138](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L137-L138)），中间的 `interpret`（[generators.py:134-135](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L134-L135)）不受影响。但内核跑完后，`if self.skip_rest: return input_ids` 直接返回输入，**根本不读 `logits`**，所以结果被丢弃。

**练习 2**：为什么不直接用 `skip_mk` 来测「纯内核开销」，而要专门造一个 `replace_with_noops`？

**参考答案**：`skip_mk` 把内核**整个跳过**了，连「启动内核、走 VM、同步」这些**非计算**成本也没了，所以它测的是「除内核外的一切」。而 `replace_with_noops` 让内核**照常启动并走完整条指令带**，只是每条都是 NoOp、不做真实计算——这才真正隔离出「内核调度本身」的开销。两者测的是不同的东西，`noops - skip_mk` 才是「启动+调度+同步」的纯骨架成本。

---

### 4.3 MK_Interpreter：动态加载编译好的 mk_llama

#### 4.3.1 概念说明

`MK_Generator` 不直接知道 megakernel 是哪段 CUDA 代码——它只持有一个 `MK_Interpreter`，并调它的 `interpret(globs)`（[generators.py:135](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L135)）。`MK_Interpreter` 的职责就一句话：**把编译好的 CUDA 扩展加载进来，并暴露一个 `interpret` 接口去调它**。

把「加载扩展」和「调内核」分开两层，是为了让**不同 setting（latency / throughput）**各自挂上签名不同的内核，而 `MK_Generator` 完全不用改（见 4.4 末尾）。

#### 4.3.2 核心流程

```
MK_Interpreter(mk_dir)
    └── get_mk_func(mk_dir)
            ├── sys.path.append(mk_dir)        # 把扩展所在目录加进搜索路径
            └── from mk_llama import mk_llama  # 导入编译产物里的可调用对象
        self.mk_func = mk_llama                # 存起来

# 子类实现：
interpret(globs):
    raise NotImplementedError                  # 基类只定接口，具体参数顺序交给子类
```

`mk_dir` 指向存放编译产物 `mk_llama` 的目录（默认是 `demos/low-latency-llama`，见 [generate.py:36](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L36)）。`mk_llama` 是用 `setup.py`/`make` 编出来的 Python C 扩展模块，里面的 `mk_llama` 是一个可直接调用的函数对象。

#### 4.3.3 源码精读

整个基类很短（[mk.py:1-17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py#L1-L17)）：

```python
import sys
from pathlib import Path


def get_mk_func(mk_dir: Path):
    sys.path.append(str(mk_dir.expanduser().absolute()))
    from mk_llama import mk_llama  # type: ignore

    return mk_llama


class MK_Interpreter:
    def __init__(self, mk_dir: Path):
        self.mk_func = get_mk_func(mk_dir)

    def interpret(self, globs):
        raise NotImplementedError
```

三个要点：

1. **`sys.path.append`（[mk.py:6](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py#L6)）**：`mk_llama` 是一个**本地编译产物**，不在任何 PyPI 包里。把它所在目录塞进 `sys.path`，下一行的 `import` 才能找到它。`.expanduser().absolute()` 是为了支持 `~` 和相对路径。
2. **`from mk_llama import mk_llama`（[mk.py:7](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py#L7)）**：模块名和函数名同名（都叫 `mk_llama`）。`# type: ignore` 因为这是动态加载的扩展，类型检查器找不到它的 stub。
3. **`interpret` 留作抽象（[mk.py:16-17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py#L16-L17)）**：基类**不规定**「怎么把 `globs` 喂给 `mk_func`」——这正是「参数对齐」差异最大的地方，必须由各 setting 的子类各自写死（见 4.4）。

工厂函数 `make_mk_interpreter`（[dispatch.py:37-38](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py#L37-L38)）按 `setting` 选 `LatencyMK_Interpreter` 或 `ThroughputMK_Interpreter`（映射表 [dispatch.py:22-25](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py#L22-L25)）。

#### 4.3.4 代码实践：验证扩展确已被加载

**实践目标**：确认 `MK_Interpreter.__init__` 真的把 `mk_func` 指向了一个可调用对象。

**操作步骤**：

1. 在已编出 `mk_llama` 的环境里，启动 Python：
   ```python
   from pathlib import Path
   from megakernels.mk import MK_Interpreter
   interp = MK_Interpreter(Path("demos/low-latency-llama"))
   print(type(interp.mk_func), callable(interp.mk_func))
   ```
2. 试着 `interp.interpret(None)`，预期抛 `NotImplementedError`（基类没实现）。

**需要观察的现象**：`callable(interp.mk_func)` 为 `True`，类型是某个 C 扩展函数对象（如 `<built-in function mk_llama>`）。

**预期结果**：基类 `interpret` 抛 `NotImplementedError`，印证「加载」与「调用」是两层。**待本地验证**（需要先 `make` 出扩展）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `MK_Interpreter` 不在 `__init__.py` 里静态 `import mk_llama`，而要用 `sys.path.append` + 运行时导入？

**参考答案**：因为 `mk_llama` 是**用户在本机编译**出来的产物，仓库里并不存在这个模块，位置也由 `mk_dir` 决定（不同 setting/模型可能指向不同目录）。静态 import 会在没编译的机器上直接报错；运行时按 `mk_dir` 动态加载，则把「编译」和「使用」解耦，未编译时只有真正构造解释器那一刻才失败。

**练习 2**：`MK_Interpreter.interpret(globs)` 只有一个参数 `globs`，但内核明明需要几十个张量。这些张量从哪来？

**参考答案**：全在 `globs` 这个对象里。`globs` 就是 2.2 说的那块「大黑板」，子类的 `interpret` 负责从 `globs` 上把每个字段**逐个取出来**、按固定顺序传给 `mk_func`。所以基类只传 `globs` 一个对象即可。

---

### 4.4 LatencyMK_Interpreter.interpret：参数对齐——globs 字段如何逐个喂给 mk_func

#### 4.4.1 概念说明

`LatencyMK_Interpreter`（[latency/mk.py:52-54](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L52-L54)）是 `MK_Interpreter` 在 latency setting 下的实现。它的 `interpret` 做的事，用一句话说就是：

> **把 `globs` 上的每个字段，按编译好的 `mk_llama` 所要求的固定顺序，逐个位置参数地传过去。**

这里的「参数对齐」有两个层面的约束：

1. **顺序约束**：`mk_func` 是位置参数调用，第 1 个实参必须对应 C 形参表的第 1 个，以此类推。顺序错了不会报「参数名不匹配」，只会**把张量喂错位置**，导致内核算出垃圾或崩溃。
2. **签名约束**：这份顺序是与**具体编译产物**绑死的。如果你重新编译了一个签名不同的 `mk_llama`（比如多加了一个激活缓冲），就必须同步改这里的调用。

#### 4.4.2 核心流程

`interpret` 把活儿委托给模块级函数 `interpret_with_mk`（[latency/mk.py:8-49](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L8-L49)）：

```
interpret(globs):
    interpret_with_mk(globs, self.mk_func)

interpret_with_mk(globs, mk_func):
    # 1) 把 KV cache 从 5D 重排成内核期望的 4D（视图操作，零拷贝语义）
    fourD_k_cache = rearrange(globs.k_cache, "l b t h d -> (l b) t h d")
    fourD_v_cache = rearrange(globs.v_cache, "l b t h d -> (l b) t h d")

    # 2) 按固定顺序逐个传参
    mk_func(
        barriers, instructions, timings,            # vm stuff
        qkv_proj_weights, attn_ln_weights, ...,      # weights
        k_cache_4d, v_cache_4d, rope_cos, rope_sin,  # weights + rope
        hidden_states, post_ln_rope_q, ..., logits,  # activations
        pos_id, attn_scale, rms_norm_eps, skip_attn_reduction,  # scalars
        stream=torch.cuda.current_stream(),
    )
```

#### 4.4.3 源码精读：逐组对齐参数与 globs 字段

下面把 [latency/mk.py:15-49](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L15-L49) 的 `mk_func(...)` 调用拆成 6 组，**左侧是位置顺序，右侧是它取的 `globs` 字段**，并标注该字段在黑板里的角色：

| # | 分组（代码注释） | 传入的 globs 字段 | 黑板角色 | 代码行 |
| --- | --- | --- | --- | --- |
| 1 | `# vm stuff` | `barriers`、`instructions`、`timings` | VM 状态（2.3 节三件套） | [L17-L19](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L17-L19) |
| 2 | `# weights` | `qkv_proj_weights`、`attn_ln_weights`、`o_proj_weights`、`mlp_ln_weights`、`up_proj_weights`、`gate_proj_weights`、`down_proj_weights` | 各层 stack 后的权重 | [L21-L26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L21-L26) |
| 3 | `# weights（续）` | `lm_head_norm_weights.data`、`lm_head_weights.data`、`fourD_k_cache`、`fourD_v_cache` | lm_head 权重 + KV cache | [L28-L31](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L28-L31) |
| 4 | `# rope` | `rope_cos`、`rope_sin` | 旋转位置编码表 | [L33-L34](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L33-L34) |
| 5 | `# activations` | `hidden_states`、`post_ln_rope_q`、`attn_out`、`attn_lse_intermediates`、`attn_out_intermediates`、`silu_out`、`logits` | 激活 scratch 缓冲 | [L36-L42](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L36-L42) |
| 6 | `# scalars` | `pos_id`、`attn_scale`、`rms_norm_eps`、`skip_attn_reduction` | 标量参数 | [L44-L47](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L44-L47) |
| — | 关键字参数 | `stream=torch.cuda.current_stream()` | 指定在哪个 CUDA stream 上 launch | [L48](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L48) |

三个值得专门拎出来的细节：

**(a) KV cache 的 5D → 4D 重排（[latency/mk.py:12-13](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L12-L13)）**

```python
fourD_k_cache = rearrange(globs.k_cache, "l b t h d -> (l b) t h d")
```

`globs.k_cache` 在 Python 侧是 5 维 `(layer, batch, time, head, dim)`，但**编译好的内核期望 4 维**——它把 `layer` 和 `batch` 合并成一个维度 `(l b)`。`einops.rearrange` 在能纯 reshape 的情况下是**零拷贝的视图操作**，所以这一步几乎免费。`v_cache` 同理。

**(b) lm_head 权重用 `.data`（[latency/mk.py:28-29](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L28-L29)）**

```python
globs.lm_head_norm_weights.data,
globs.lm_head_weights.data,
```

`.data` 取的是张量**底层裸数据**，绕过 autograd。这两个权重可能是带 `requires_grad` 的叶子张量，而 C 扩展只想拿到裸指针，用 `.data` 能干净地剥离 autograd 包裹。注意**只有 lm_head 的两个权重加了 `.data`**，其它权重直接传——这是与权重对象在模型里如何构造有关的历史细节，读代码时照单全收即可。

**(c) latency 与 throughput 的参数表不同——这就是为什么要子类化**

对比 [throughput/mk.py:14-49](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/mk.py#L14-L49)，throughput 版的 `mk_func` 调用**多出好几个激活缓冲**（`rms_rope_intermediates`、`rms_gate_intermediates`、`rms_lm_head_intermediates` 等），且**标量段不同**（latency 传 `skip_attn_reduction`，throughput 传 `batch_size`）。这说明：

> **latency 和 throughput 是两份分别编译的 `mk_llama`，签名不同。** 所以它们各自有一个 `MK_Interpreter` 子类，各自把 `globs` 字段按自己那份内核的顺序排好。`MK_Generator` 对此一无所知——它只调 `interpreter.interpret(globs)`，由 `dispatch.py` 保证挂上对的子类。

这正是「参数对齐」是**契约**而非「随便传」的最强证据：换一份编译产物，就得改一份 `interpret`。

#### 4.4.4 代码实践：追踪参数顺序与 globs 字段的对应

**实践目标**：把 `interpret_with_mk` 传给 `mk_func` 的**每一个位置参数**，对上它在 `Globals`/`BaseGlobals` 里的字段定义，确认「顺序」与「字段存在性」。

**操作步骤**：

1. 打开 [latency/mk.py:15-49](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L15-L49)，从上到下数位置参数（不含最后的 `stream=`）。
2. 对每个字段，去 [BaseGlobals](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L10-L69)（[instructions.py:10-69](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L10-L69)）或 latency 的 [Globals](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L9-L30)（[latency/instructions.py:9-30](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L9-L30)）里找它的定义，确认字段名存在。
3. 特别注意：`post_ln_rope_q`、`attn_lse_intermediates`、`attn_out_intermediates`、`silu_out`、`logits`、`skip_attn_reduction` 这些是 **latency 专属**字段（定义在 [latency/instructions.py:10-19](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L10-L19)），不在 `BaseGlobals` 里——这解释了为什么 throughput 的 `interpret` 取的是另一组字段。
4. 数一下：latency 的 `mk_func` 一共有多少个**位置参数**（提示：3 个 vm + 7 个 stack 权重 + 2 个 lm_head 权重 + 2 个 kv cache + 2 个 rope + 7 个激活 + 4 个标量 = 27 个位置参数，加上 1 个 `stream=` 关键字参数）。

**需要观察的现象**：每个位置参数都能在 `Globals` 里找到一个同名字段；`skip_attn_reduction` 是 `bool` 标量（[latency/instructions.py:19](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L19)），传给内核用来跳过 attention reduction。

**预期结果**：你能画出一张「位置 i → 字段名」的完整对照表（即 4.4.3 的表）。这张表就是 latency 版 `mk_llama` 的**事实签名文档**——仓库里没有别处显式记录它，它只活在这个调用的顺序里。

> 进阶（无 GPU 也能做）：对比 [throughput/mk.py:14-49](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/mk.py#L14-L49)，列出 throughput 版**比 latency 版多/少**了哪些位置参数、标量段差异在哪，体会「换内核 = 改这张表」。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `interpret_with_mk` 里 `mk_func(...)` 的前两个参数 `globs.barriers` 和 `globs.instructions` 对调，会发生什么？

**参考答案**：类型检查不会拦你（它们都是 `torch.Tensor`/int32 张量）。但内核会**把 instructions 当 barriers、把 barriers 当 instructions** 来读，导致完全错误的 VM 行为（同步计数被当成指令、指令被当成同步计数）。这正是「位置参数 = 契约」的脆弱之处：顺序错了不报错，只产出垃圾。这也说明为什么这份调用顺序必须逐位对齐 C 签名。

**练习 2**：latency 的标量段传了 `skip_attn_reduction`，throughput 传了 `batch_size`。这暗示了两个 setting 在内核层面的什么差异？

**参考答案**：latency setting 是**单 batch、极致低延迟**，内核通过 `skip_attn_reduction` 这个开关跳过 attention 的归约步骤（配合 [latency/scheduler.py:277](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L277) 的 `num_attention_partitions=1`）；throughput setting 是**大 batch 吞吐**，内核需要知道 `batch_size` 来处理多 batch，也不做那种跳过。两者面向不同目标，因此编译出**签名不同**的两份 `mk_llama`，Python 侧也就需要两个 `interpret` 各自对齐。

**练习 3**：为什么 KV cache 要在 `interpret_with_mk` 里 `rearrange` 成 4D 再传，而不是在 `make_globals` 阶段就存成 4D？

**参考答案**：在 `make_globals`/模型侧，KV cache 以 5D `(l, b, t, h, d)` 存储更便于 Python 端的理解和与模型其它部分的交互；而**这一份 `mk_llama` 内核**约定吃 4D `(l b, t, h, d)`。把「适配内核布局」这件事放在调用前的最后一刻（`rearrange`，且是零拷贝视图），让 `globs` 本身保持一个对 Python 友好的形状，是一种常见的「边界处转换」做法。

---

## 5. 综合实践：画出一次 `mk` 解码的完整数据流并标注开关断点

把本讲三个模块串起来，完成下面这个综合任务。

**任务**：在一张图（文字版流程图即可）里画出 `mk` 模式下**解码一个 token** 的完整数据流，要求：

1. 标出 `generate`（解码循环，[generators.py:145-163](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L145-L163)）如何算出 `pos_id` 并调用 `run`。
2. 在 `run`（[generators.py:121-143](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L121-L143)）里标出三段：embed 写 `hidden_states`、`fill()`+`interpret` 跑内核、`argmax` 读 `logits`。
3. 在 `interpret`（[latency/mk.py:52-54](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L52-L54) → [8-49](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L8-L49)）里标出 KV cache 4D 重排、以及参数被分成 6 组喂给 `mk_func`。
4. **用三个标记** 在图上标出 `replace_with_noops`（清零 instructions）、`skip_mk`（跳过 interpret）、`skip_rest`（跳过 embed+argmax）分别作用在哪一段。

**参考答案骨架**（你可以基于它细化）：

```
generate(i):
  pos_id = prompt_len + ntok_already_generated + i - 1
  run(input_ids, pos_id):
    [embed]  ──写──►  globs.hidden_states        ┐ skip_rest 在此之前 return
    fill()   ──写──►  globs.barriers (=fill_val)
    写       ──写──►  globs.pos_id
    [mk] interpret(globs):                       ┐ skip_mk 跳过这一整段
            rearrange k/v_cache → 4D
            mk_func( vm, weights, kv, rope,      │ replace_with_noops:
                     activations, scalars,        │   把 mk_func 读的 instructions
                     stream=... )                 │   全置 0(=NoOp),内核空跑
            ──写回──► globs.logits(及其它缓冲)
    [argmax] globs.logits ──► torch.argmax ──► output_ids
  output_tokens[:, output_token_pos] = output_ids
```

**自我检查**：

- 你能否解释图上**每一个箭头**写的是 `globs` 的哪个字段？
- 你能否说出三种「空跑」开关分别砍掉/改造了图上的哪一段？
- 你能否回答：为什么 `replace_with_noops` 砍掉的是「计算」而不是「调度」？（答：因为它让内核仍启动、仍走完整条全是 NoOp 的指令带，砍掉的是每条指令的真实计算，保留的是调度与同步骨架。）

---

## 6. 本讲小结

- `MK_Generator.run`（[generators.py:121-143](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L121-L143)）是 `mk` 模式的单步前向，三段式：**embed 写 `hidden_states` → `fill()`+`interpret` 跑内核 → `argmax` 读 `logits`**；lm_head 被编进内核，所以 Python 端不再单独跑 lm_head。
- `generate`（[generators.py:145-163](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L145-L163)）是套在 `run` 外的解码循环，每步算出 \( \text{pos\_id} = \text{prompt\_len} + \text{ntok\_already\_generated} + i - 1 \) 并调一次 `run`。
- `replace_with_noops`（[generators.py:118-119](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L118-L119)）把 `instructions` 清零；因为 `NoOp.opcode()==0`（[instructions.py:126](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L126)），全零指令带等价于全 NoOp，于是内核仍启动并走完 VM，但零真实计算——这正是**测量纯内核调度开销**的手段。
- `skip_mk`（跳过 `interpret`）与 `skip_rest`（跳过 embed+argmax）配合，把一次解码耗时拆成「计算 / 调度 / embed+argmax / Python 循环」四块，公式见 4.2.2。
- `MK_Interpreter`（[mk.py:12-17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py#L12-L17)）只负责用 `sys.path.append` + `from mk_llama import mk_llama` **动态加载**编译产物，把「加载」与「调用」解耦；`interpret` 留给子类实现。
- `LatencyMK_Interpreter.interpret`（[latency/mk.py:8-54](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L8-L54)）把 `globs` 的字段按 **6 组固定顺序**（vm / weights / kv+rope / activations / scalars / stream）逐个喂给 `mk_func`；这份顺序是和该 `mk_llama` 编译产物**逐位绑死的位置契约**，latency 与 throughput 因签名不同而各有自己的子类。

---

## 7. 下一步学习建议

1. **读内核侧的「另一半」**：本讲全是 Python 侧的「调用方」。建议去 `demos/low-latency-llama/` 看 `mk_llama` 的 C++/CUDA 源码，确认 4.4.3 那张参数表的顺序与 C 形参表**逐位**对得上——这是验证「参数对齐」最直接的方式。
2. **对比 throughput 全链路**：沿着 [throughput/mk.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/mk.py)、`throughput/scheduler.py`、`throughput/instructions.py` 走一遍，体会「同一套 `MK_Generator` 骨架，挂不同 setting 的 interpreter + schedule」是如何复用代码的。
3. **回到正确性对照**：结合 [U2·L1](u2-l1-three-execution-modes.md) 的三种模式，用 `diff_test.py` 把 `mk` 与 `pyvm` 的逐层中间结果对拍——本讲的 `globs` 黑板模型正好解释了为什么这种对拍可行（中间量都落在固定的 `globs` 字段里）。
4. **进阶性能分析**：用 `noops` / `skip_mk` / `skip_rest` 三档实测后，去读 `timings` 张量（内核写回的逐指令计时），把「宏观四块开销」与「微观每条指令耗时」对上——这将自然引出 U5 系列关于内核级 profiling 的内容。
