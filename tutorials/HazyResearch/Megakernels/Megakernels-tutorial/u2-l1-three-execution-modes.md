# 三种执行模式 torch / pyvm / mk

> 本讲对应手册单元 U2·L1，承接 [U1·L3]（编译并运行 low-latency-llama demo）。建议你已经能用 `make` 编出 `mk_llama` 扩展，并跑通过 `generate.py mode=mk`，再进入本讲。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清 **三种执行模式 `torch` / `pyvm` / `mk` 各自是什么**：它们对同一份输入（prompt + 生成长度）产出 token，但**执行"同一个模型"的方式完全不同**。
2. 区分 `Generator` 基类与三个子类（`PyTorchGenerator` / `PyVM_Generator` / `MK_Generator`）的**职责边界**：基类提供"按 EOS 提前停止"的共享循环，三个子类各自实现一次"解码出下一个 token"的 `generate`。
3. 看懂 [generate.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py) 里的 `match config.mode` 分支，以及 [dispatch.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py) 里的工厂函数如何按 `mode`/`setting` 拼装出正确的解释器。
4. 理解三种模式在**正确性对照（cross-check）**中的角色：`torch` 是模型的"金标准"，`pyvm` 是"调度后的参考虚拟机"，`mk` 是"真正的高性能内核"。并能回答：**为什么 `pyvm` 是 `mk` 最理想的对照基准**。

## 2. 前置知识

- **自回归生成（autoregressive generation）**：大模型一次只预测"下一个 token"。把刚预测出的 token 拼回输入，再预测下一个，循环往复，直到达到指定长度或遇到结束符（EOS）。
- **前向计算（forward）**：给定一段 token id，模型输出每个位置"下一个 token"的概率分布（logits）。生成时我们只关心 `argmax(logits)` 得到的那个 token。
- **指令（instruction）/ 调度（schedule）**：在 Megakernels 里，一次前向计算会被"编译"成一串指令（你可以类比成 CPU 的机器码）。这串指令连同所有权重、激活张量，被装进一个叫 `globs`（globals）的全局状态对象。这一步在 U1 系列讲义里已有铺垫，本讲直接使用。
- **解释器（interpreter）**：一个"吃进 `globs`、按某种方式执行指令、把结果写回 `globs`"的对象。本讲的主角就是**三种不同的解释器**。
- **PyTorch 参考实现（reference implementation）**：用最直白、最不容易出错的方式（直接调 PyTorch 模型）算出"正确答案"，用来给更快的实现当对拍基准。

如果上面某几个词还陌生，记住一句话即可：**三个模式都试图回答"下一个 token 是什么"，区别只在于它们用什么"计算器"来算这一次前向**。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [megakernels/generators.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py) | `Generator` 基类 + 三个子类，三种模式的"生成器"全在这里 |
| [megakernels/scripts/generate.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py) | 命令行入口；用 `match config.mode` 选择生成器并跑基准 |
| [megakernels/dispatch.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py) | 按 `setting`（latency/throughput）选择调度器与解释器的工厂 |
| [megakernels/python_vm.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/python_vm.py) | `pyvm` 模式背后的 Python 参考虚拟机（逐条用 PyTorch 函数解算指令） |
| [megakernels/mk.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py) | `mk` 模式背后解释器的基类（动态加载编译好的 CUDA 扩展 `mk_llama`） |
| [megakernels/demos/latency/mk.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py) | 低延迟场景下 `mk` 解释器的具体实现：一次调用把整个 `globs` 喂给 megakernel |
| [megakernels/scheduler.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py) | `Schedule` 与 `get_linear_instructions`：把指令"拉平"成有序列表供 `pyvm` 使用 |

## 4. 核心概念与源码讲解

### 4.1 Generator 基类与 generate_with_eos：三种模式共享的生成骨架

#### 4.1.1 概念说明

无论用哪种"计算器"（PyTorch / Python 虚拟机 / CUDA megakernel），"生成一段文本"这件事的外壳是一样的：

> 拿到一个初始 token，循环地预测下一个 token，直到生成够多、或遇到结束符。

所以 Megakernels 把这层**外壳**抽到一个公共基类 `Generator` 里，而把"**单步前向**"留给三个子类各自实现。这是一种典型的**模板方法（template method）**设计：基类定义"怎么循环、怎么判 EOS"，子类只管"这一步怎么算下一个 token"。

#### 4.1.2 核心流程

基类对外暴露两个方法：

1. `generate(...)`：**抽象方法**（基类里直接 `raise NotImplementedError`）。含义是"给定已生成的 token、prompt 长度、本次要生成的步数，把后续 token 填好"。三个子类各自实现它。
2. `generate_with_eos(...)`：**具体方法**。它在 `generate` 外面套一个循环，**每生成 `eos_token_check_interval` 个 token 就停下来检查有没有出现 EOS**，一旦出现就提前返回（返回"第一个 EOS 出现的位置"和"总共生成了多少 token"）。

`generate_with_eos` 的循环骨架（伪代码）：

```
for 起点 in range(1, ntok, eos_token_check_interval):
    本批步数 = min(eos_token_check_interval, 剩余所需步数)
    self.generate(..., ntok=本批步数, ntok_already_generated=起点)   # ← 调子类
    把刚生成的这批 token 搬到 CPU
    for 每个 token:
        if token 属于 eos_token_ids:
            return (它出现的位置, 本批末尾位置)
```

关键点：基类**不知道也不关心** `self.generate` 内部是用 PyTorch 还是 CUDA——它只调用接口。这就是三种模式能共享这一套 EOS 逻辑的原因。

#### 4.1.3 源码精读

`Generator` 基类与抽象 `generate`：

[megakernels/generators.py:11-19](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L11-L19) —— 定义 `Generator` 基类，`generate` 直接抛 `NotImplementedError`，强迫子类实现。

`generate_with_eos` 的分块循环：

[megakernels/generators.py:21-58](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L21-L58) —— 用 `range(1, ntok, eos_token_check_interval)` 切块，每块调一次 `self.generate(...)`（第 42 行），再把这段 token `.cpu()` 出来逐个比对 `eos_token_ids`（第 52–56 行）。注意第 32 行断言 `batch size must be 1`——这个带 EOS 的路径目前只支持单条样本。

谁在用哪个方法？基准脚本直接调 `generate`：

[megakernels/scripts/generate.py:174](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L174) —— `gen.generate(output_tokens, prompt_len, config.ntok - 1)`，固定生成满 `ntok-1` 步，不做 EOS 判定（基准测试要稳定的步数）。

交互式对话用 `generate_with_eos`：

[megakernels/scripts/llama_repl.py:110](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/llama_repl.py#L110) —— `gen.generate_with_eos(...)`，聊到模型输出 EOS 就停。两种调用方式都能用在三种模式上，因为外壳是共享的。

#### 4.1.4 代码实践

**目标**：确认"基类负责循环、子类负责单步"这一分工。

1. 打开 [generators.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py)，在 `generate_with_eos` 的第 42 行 `self.generate(...)` 调用处**打一个断点或在它前面加一行临时 `print("chunk start", ntok_already_generated)`**（仅用于阅读理解，不要提交）。
2. 阅读第 39–41 行：`ntok_for_chunk` 是如何用 `min(...)` 保证最后一块不超出的。
3. 想象 `eos_token_check_interval=1` 的极端情况：此时循环退化为"每生成 1 个 token 就检查一次 EOS"。

**预期结果**：你会清楚看到，无论底层是 `torch`/`pyvm`/`mk`，外层循环代码完全不变；要切换"计算器"，只需要换掉 `self` 这个对象的类。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `generate_with_eos` 里第 32 行要断言 `batch size must be 1`？
**参考答案**：因为 EOS 提前停止是"一条序列里碰到结束符就停"的逻辑。一旦 batch 里有多个样本，它们各自碰到 EOS 的时机不同，需要更复杂的 mask/padding 处理。这条路径目前只服务于单样本交互（repl），所以直接断言为 1。

**练习 2**：如果 `eos_token_check_interval` 设得非常大（比如大于 `ntok`），`generate_with_eos` 的行为会退化成什么？
**参考答案**：`range(1, ntok, 很大的数)` 只会产生一个起点 `1`，`ntok_for_chunk = min(很大, ntok-1) = ntok-1`，于是只调一次 `generate` 生成全部 token，再做一次 EOS 检查——等价于"不提前停止"。

---

### 4.2 PyTorchGenerator：torch 模式 —— 纯 PyTorch 参考实现

#### 4.2.1 概念说明

`torch` 模式是三种里**最简单、最可信**的一种：它**完全不用指令、不用调度、不用 megakernel**，而是直接调用 PyTorch 定义的 `LlamaForCausalLM` 模型做前向。它的产出就是"模型本该给出的答案"，因此被当作**正确性金标准（ground truth）**。

它的意义不是快，而是**准**：当 `mk` 或 `pyvm` 的结果和它不一致时，说明那两个路径里有 bug。

#### 4.2.2 核心流程

`PyTorchGenerator.generate` 一步生成一个 token，循环 `ntok` 次：

```
for i in range(ntok):
    构造 BatchState(input_ids=上一个 token, position_ids=当前位置, seq_len=...)
    decode_output = self.model(decode_inp)      # ← 直接整模型前向
    output_tokens[下一个位置] = decode_output.output_ids
```

注意它**绕过了 schedule / globs / instructions**——模型怎么算，完全由 `LlamaForCausalLM` 的 `forward` 决定。

#### 4.2.3 源码精读

构造与单步前向：

[megakernels/generators.py:61-92](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L61-L92) —— `__init__` 只存一个 `model`（第 66 行）；`generate` 在循环里构造 `BatchState` 并调用 `self.model(decode_inp)`（第 89 行），把 `output_ids` 写回 `output_tokens`（第 92 行）。**全程没有 `schedule`、没有 `interpreter`、没有 `globs`**。

对比点（先记住，4.3/4.4 会用到）：`torch` 模式拿到的就是"模型正确输出"，但它**没有验证"把模型编译成指令"这一步是否正确**——因为它压根没走指令。验证那一环，是 `pyvm` 的职责。

#### 4.2.4 代码实践

**目标**：把 `torch` 模式当作金标准，记录它的输出。

1. 用 `torch` 模式跑一次（pydra 的参数用 `key=value` 形式传）：

   ```bash
   # 仓库根目录
   python megakernels/scripts/generate.py mode=torch \
       prompt="tell me a funny joke about cookies" ntok=20
   ```

2. 观察脚本末尾打印的 `Output ids:` 那一行，把这一串 token id **抄下来**（后面综合实践要对比）。
3. 注意：这一步**不需要编译 megakernel**（`torch` 模式不碰 CUDA 扩展），所以即使你还没 `make`，也能跑通。

**预期结果**：得到一串确定的 token id。同一 prompt + 同一模型权重下，`torch` 模式的输出是稳定的，可作为后续对拍的基准。

> 如果本地没有 GPU 或权重，可改为**源码阅读型实践**：阅读第 89 行 `self.model(decode_inp)`，确认它调用的是 `LlamaForCausalLM.__call__`（即 `forward`），从而确认 `torch` 模式 = 标准 PyTorch 推理。

#### 4.2.5 小练习与答案

**练习 1**：`PyTorchGenerator` 的 `__init__` 为什么只接收 `model`，而不像 `MK_Generator` 那样还要 `interpreter` 和 `schedule`？
**参考答案**：因为它不执行"指令流"，而是直接跑整个 PyTorch 模型。模型本身已经包含了所有计算逻辑，不需要外部调度或解释器。

**练习 2**：如果 `mk` 模式和 `torch` 模式输出不一致，能直接断定是 megakernel 写错了吗？
**参考答案**：不能。`torch` 与 `mk` 之间隔着两层：①调度器把模型编译成指令；②megakernel 执行这些指令。两者不一致，可能是调度器错了，也可能是内核错了。要定位，需要先引入 `pyvm`（见 4.3）。

---

### 4.3 PyVM_Generator：pyvm 模式 —— Python 参考虚拟机

#### 4.3.1 概念说明

`pyvm`（Python VM）模式是三种模式里的**关键桥梁**。它的做法是：

> 先用调度器把模型编译成一串**指令**（和 `mk` 共用同一份调度结果），然后**在 Python 里逐条"解释执行"这些指令**——每条指令对应一个用 PyTorch 写的小函数（称为 solver）。

换句话说，`pyvm` 走的是"**指令流**"这条路（和 `mk` 一样），但执行器是"**慢但好懂的 PyTorch 函数**"（和 `mk` 不同）。

这就带来了它最核心的价值——**正确性对照的分层定位**：

| 对比 | 两端 | 结论 |
| --- | --- | --- |
| `torch` vs `pyvm` | "整模型前向" vs "指令流逐条执行" | 若一致 → **调度器把模型编译成指令是对的** |
| `pyvm` vs `mk` | "指令流 + PyTorch 执行" vs "指令流 + CUDA 执行" | 若一致 → **megakernel 的 CUDA 实现是对的** |

也就是说，`pyvm` 把"模型→指令"和"指令→结果"这两件事拆开验证。**`pyvm` 之所以是 `mk` 的理想对照基准**，是因为它和 `mk` 共享同一份调度（schedule）和几乎相同的 `globs` 初始化，**唯一的差别就是执行器**：PyTorch 的逐条 solver vs. 一个融合的 CUDA megakernel。于是 `mk`↔`pyvm` 的任何数值差异，都可以锁定到"内核执行"这一层，而排除调度器嫌疑。

#### 4.3.2 核心流程

`PyVM_Generator` 继承自 `MK_Generator`（共享构造与 `generate` 循环），只**重写了 `run`**（单步前向）：

```
run(input_ids, pos_id):
    hidden_states = embed_tokens(input_ids)        # 词嵌入
    globs.hidden_states = hidden_states
    globs.barriers.zero_()                          # 屏障清零
    globs.pos_id = pos_id                           # 写入当前位置
    self.interpreter.interpret(globs, self.instructions)   # ← 逐条执行指令(PyTorch)
    output_hiddens = globs.hidden_states            # 读回输出隐状态
    output_ids = lm_head(output_hiddens)            # 最后一步 lm_head 仍在 PyTorch 里算
    return output_ids
```

"逐条执行"的真相在参考虚拟机里：

```
for instruction in instructions:          # 拓扑序的线性指令列表
    instruction_to_solver[type(instruction)](globs, instruction)
```

即：**按指令的类型**，查表找到一个 PyTorch 函数（solver），让它读写 `globs`。例如"矩阵×向量"指令会调用 `matvec`，"RMSNorm"指令会调用 `rms_norm`（见 [python_vm.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/python_vm.py)）。

#### 4.3.3 源码精读

继承关系与指令列表：

[megakernels/generators.py:166-177](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L166-L177) —— `class PyVM_Generator(MK_Generator)`，第 177 行 `self.instructions = self.schedule.get_linear_instructions()` 把 DAG 拓扑序拉平成线性指令列表，供 Python 虚拟机遍历。

`get_linear_instructions` 的实现：

[megakernels/scheduler.py:55-57](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L55-L57) —— 直接返回 `[node.instruction for node in self.dag_nodes]`，注释明确"假设已是拓扑序"。

`run` 的单步前向：

[megakernels/generators.py:179-201](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L179-L201) —— 注意它与 `MK_Generator.run`（4.4）的异同：
- **相同**：都先 `embed_tokens`→写 `globs.hidden_states`，都清零/填充 `barriers`，都写 `globs.pos_id`，都调用 `self.interpreter.interpret(...)`。
- **不同①（执行器）**：这里第 191 行 `self.interpreter.interpret(self.schedule.globs, self.instructions)` 走的是 **Python 虚拟机**（逐条 PyTorch solver）。
- **不同②（输出收尾）**：`pyvm` 执行完得到的是 `hidden_states`，第 197 行还要在 PyTorch 里单独跑一次 `self.model.lm_head(...)` 才得到 `output_ids`；而 `mk` 是内核直接产出 `logits`（见 4.4）。

Python 虚拟机本体：

[megakernels/python_vm.py:83-95](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/python_vm.py#L83-L95) —— `interpret_with_pyvm` 就是那个 `for instruction in instructions: solver(globs, instruction)` 循环；`PyVM_Interpreter` 只是把"指令类型→solver 函数"的字典包了一层。具体的 solver 表在 `demos/latency/python_vm.py`（`INSTRUCTION_TO_SOLVER`），由 `dispatch.py` 注入。

#### 4.3.4 代码实践

**目标**：确认 `pyvm` 走的是"指令流"，且产出应与 `torch` 高度一致。

1. 用 `pyvm` 模式跑同一 prompt：

   ```bash
   python megakernels/scripts/generate.py mode=pyvm \
       prompt="tell me a funny joke about cookies" ntok=20
   ```

   注意 `generate.py` 的 `finalize` 里有一条断言：latency 场景下 `mk`/`pyvm` 必须 `interleave_rope=True`（见 [generate.py:51-53](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L51-L53)），而 `interleave_rope` 默认就是 `True`，所以正常传参即可。

2. 把打印的 `Output ids:` 与 4.2 里 `torch` 模式的结果**逐位对比**。
3. 阅读时注意：`pyvm` 模式**同样不需要编译 megakernel**（它用的是 Python 里的 PyTorch solver），所以即便没 `make` 也能跑。

**预期结果**：`pyvm` 与 `torch` 的 token 序列在绝大多数（理想情况下全部）位置一致。若一致，说明"模型→指令"的调度是正确的。

> 若本地无法运行，改为**源码阅读型实践**：跟踪 `run` 第 191 行进入 [python_vm.py:86-87](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/python_vm.py#L86-L87)，确认每条指令都映射到一个 PyTorch solver；这解释了为什么 `pyvm` "慢但可信"。

#### 4.3.5 小练习与答案

**练习 1**：为什么说 `pyvm` 是 `mk` 的"理想对照基准"，而不是 `torch`？
**参考答案**：因为 `pyvm` 与 `mk` **共用同一份调度结果和几乎相同的 `globs` 初始化**，唯一差别是执行器（PyTorch solver vs CUDA megakernel）。所以 `mk`↔`pyvm` 的差异能精准定位到"内核执行"层。而 `torch` 根本不走指令流，`mk`↔`torch` 的差异无法区分是"调度错了"还是"内核错了"。

**练习 2**：`pyvm` 的 `run` 里为什么要在最后单独调一次 `lm_head`，而不是让指令流直接产出 token？
**参考答案**：在 `pyvm` 这条参考路径里，指令流解算到的是最终的 `hidden_states`，最后的 `lm_head`（hidden→logits→argmax）在 PyTorch 里单独完成，便于复用模型里现成、可信的 `lm_head` 实现。而 `mk` 路径则把 `lm_head` 也纳入 megakernel，直接产出 `logits`。两种收尾方式都正确，只是分工不同。

---

### 4.4 MK_Generator：mk 模式 —— 真实 megakernel 执行

#### 4.4.1 概念说明

`mk` 模式才是这个项目"真正想做的事"：**用一个大而融合的 CUDA megakernel 一次性执行整串指令**，从而把解码延迟压到极低。它和 `pyvm` 消费同一份调度、同一套 `globs`，但执行器换成了编译好的 `mk_llama` 扩展。

正因为如此，`mk` 模式还提供了几个**调试开关**，让你能在不删代码的情况下"关掉一部分计算"来定位问题：

- `skip_mk`：跳过 megakernel 调用（只做嵌入等 Python 侧准备）。
- `skip_rest`：跳过嵌入与收尾，直接把输入原样返回（用于纯粹压测内核启动开销）。
- `noops` / `replace_with_noops`：把所有指令清零，变成"空操作"，用来测**纯框架开销**（调度、launch、barrier，不含真实计算）。
- `barrier_fill_val`：给 `barriers` 张量填一个初值，用于同步语义的实验。

#### 4.4.2 核心流程

`MK_Generator.run` 单步前向：

```
run(input_ids, pos_id):
    if not skip_rest:
        hidden_states = embed_tokens(input_ids)
        globs.hidden_states = hidden_states.squeeze(1)   # 注意比 pyvm 多了 squeeze
    self.fill()                              # 用 barrier_fill_val 填充 barriers
    globs.pos_id = pos_id
    if not skip_mk:
        self.interpreter.interpret(globs)    # ← 调用 CUDA megakernel
    if skip_rest: return input_ids
    logits = globs.logits                    # 内核直接产出 logits
    return argmax(logits)
```

megakernel 那一步，本质上是**一次函数调用把整个 `globs` 喂给 GPU**：

```
mk_func(globs.barriers, globs.instructions, globs.timings,
        ... 各种权重 ..., fourD_k_cache, fourD_v_cache,
        globs.rope_cos, globs.rope_sin,
        ... 各种激活 ..., globs.logits,            # 写回
        globs.pos_id, globs.attn_scale, ...)       # 标量
```

控制器 warp 在 GPU 上从 `globs.instructions` 逐条取指令、各 warp 分工执行、最终把 `logits` 写回 `globs`。Python 侧只需读 `globs.logits` 做 `argmax` 即可。

#### 4.4.3 源码精读

构造与调试开关：

[megakernels/generators.py:95-120](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L95-L120) —— `__init__` 接收 `interpreter`、`schedule` 以及 `barrier_fill_val`/`skip_mk`/`skip_rest`（第 101–103 行），第 113 行构造时立刻调 `self.fill()`。`fill`（第 115–116 行）和 `replace_with_noops`（第 118–119 行）就是两个调试辅助：前者填 barriers，后者把指令清零。

`run` 单步前向：

[megakernels/generators.py:121-143](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L121-L143) —— 注意第 130 行 `globs.hidden_states[:] = hiddens.squeeze(1)`（`pyvm` 没有这个 squeeze，是 batch 维度的形状约定差异，可作观察点）；第 135 行 `self.interpreter.interpret(self.schedule.globs)` 触发 megakernel；第 140–141 行直接读 `globs.logits` 并 `argmax`。

megakernel 解释器的真正调用：

[megakernels/demos/latency/mk.py:8-54](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L8-L54) —— `interpret_with_mk` 把 `globs` 里的 barriers/instructions/权重/激活/标量**一口气**传给 `mk_func`（第 15–49 行），第 48 行还把当前 CUDA stream 传进去。`LatencyMK_Interpreter.interpret`（第 52–54 行）就是调它。而 `mk_func` 来自 [megakernels/mk.py:5-14](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py#L5-L14)：动态 `import mk_llama` 拿到那个 pybind11 扩展（U1·L3 里 `make` 出来的产物）。

#### 4.4.4 代码实践

**目标**：用 `mk` 的调试开关，体会"分层压测"。

1. 前置：先按 U1·L3 在 `demos/low-latency-llama` 执行 `make`，编出 `mk_llama`。
2. 跑真实 `mk`，记录 token 与耗时（README 给出的标准命令）：

   ```bash
   python megakernels/scripts/generate.py mode=mk \
       prompt="tell me a funny joke about cookies" ntok=20
   ```

3. 把输出 id 与 4.3 的 `pyvm` 结果对比——**它们应当一致**（这验证了 megakernel 正确）。
4. 再跑一次"空操作"基准，观察**纯框架开销**：

   ```bash
   python megakernels/scripts/generate.py mode=mk noops=true ntok=20
   ```

   它会把指令清零（[generate.py:162-163](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L162-L163) 调用 `replace_with_noops`），token 输出会变成无意义内容，但 `Average time` 反映的是"调度+launch+同步"本身的开销。

**预期结果**：
- `mk` 与 `pyvm` 输出 token 一致 → megakernel 正确；
- `noops=true` 的耗时应明显小于真实 `mk`，差额就是"真实计算"的代价。

> 若未编译 megakernel，`mk` 模式无法运行；此时改为**源码阅读型实践**：对照 4.4.3，把 `run` 里 `skip_mk`/`skip_rest`/`fill` 三个分支各自画出"会跳过哪些步骤"的表格。

#### 4.4.5 小练习与答案

**练习 1**：`skip_mk=True` 但 `skip_rest=False` 时，`run` 会返回什么？这有什么用？
**参考答案**：会跳过 `self.interpreter.interpret(globs)`，但仍然读取 `globs.logits` 并 `argmax`（因为 `skip_rest` 为假）。由于 megakernel 没跑，`globs.logits` 里是上次/初始的值，返回的 token 无意义。它的用途是**单独测量"非内核的 Python 侧开销"**（嵌入、barrier 填充、argmax、数据搬运），把内核时间从总时间里剥离。

**练习 2**：`mk` 与 `pyvm` 的 `run` 都调用 `self.interpreter.interpret(...)`，这俩 `interpret` 是同一个函数吗？
**参考答案**：不是。`MK_Generator` 的 interpreter 是 `MK_Interpreter` 子类（调用 CUDA `mk_func`，签名是 `interpret(globs)`）；`PyVM_Generator` 的 interpreter 是 `PyVM_Interpreter`（逐条调 PyTorch solver，签名是 `interpret(globs, instructions)`）。名字相同、行为完全不同——这正是三种模式"同一接口、不同执行器"的体现。

---

### 4.5 generate.py 的 mode 分支与 dispatch 工厂

#### 4.5.1 概念说明

前面三节讲了"三种计算器"本身。本节看**谁在选计算器、怎么选**。入口脚本 [generate.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py) 用 `config.mode`（命令行传入 `mode=...`）做一次 `match` 分支，构造对应的 `Generator`；而解释器/调度器的具体类型则由 [dispatch.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py) 按 `setting`（latency/throughput）查表决定。

两层选择的关系：

- `mode`（torch/pyvm/mk）→ 决定**执行器**（PyTorch 模型 / Python 虚拟机 / CUDA megakernel）。
- `setting`（latency/throughput）→ 决定**调度器和解释器的具体实现族**（低延迟版 vs 高吞吐版）。

#### 4.5.2 核心流程

`generate.py` 的 `main` 大致分四段：

```
1. 加载 tokenizer + LlamaForCausalLM 模型
2. prefill：跑一次完整前向拿到"第一个新 token"，放进 output_tokens[0]
3. 构建调度：schedule = schedule_builder.build(model)
            → assign_to_sms(...)   ← 把指令分配到不同 SM
            → tensorize_instructions(...) ← 把 Python 指令对象张量化(供 mk 读取)
4. match config.mode:
       "torch" -> gen = PyTorchGenerator(model)
       "pyvm"  -> gen = PyVM_Generator(model, make_pyvm_interpreter(...), schedule)
       "mk"    -> gen = MK_Generator(model, make_mk_interpreter(...), schedule, ...)
                  if noops: gen.replace_with_noops()
       其它     -> raise ValueError
5. 计时循环：反复调 gen.generate(...)，统计平均耗时与 tokens/sec
```

注意第 3 段**对所有模式都执行**（哪怕 `torch` 用不到 schedule）——这是有意为之：保证三种模式跑在完全一致的"调度结果"之上，便于对拍。`torch` 只是构造后不去读 schedule 而已。

#### 4.5.3 源码精读

关键配置字段：

[megakernels/scripts/generate.py:28-49](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L28-L49) —— `mode` 默认是 `"model"`（第 34 行），但 `match` 只认 `torch`/`pyvm`/`mk`，所以**实际使用时必须显式传 `mode=...`**（README 的例子都是 `mode=mk`）；另有 `barrier_fill_val`/`noops`/`skip_mk`/`skip_rest`（第 41、44–46 行）对应 4.4 的调试开关。

调度构建（三模式共享）：

[megakernels/scripts/generate.py:139-144](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L139-L144) —— `make_schedule_builder(config.setting).build(model)` 生成调度，再 `assign_to_sms` + `tensorize_instructions`。`tensorize_instructions` 把指令变成 `globs.instructions` 张量，这是 `mk` 内核读取的形态；`pyvm` 则用 `get_linear_instructions()` 的对象列表——两者源自同一个 DAG。

mode 分支（本讲核心）：

[megakernels/scripts/generate.py:146-165](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L146-L165) —— 三个 `case` 各自构造对应 `Generator`：`torch` 只传 model；`pyvm` 经 `make_pyvm_interpreter`；`mk` 经 `make_mk_interpreter` 并把调试开关透传；`case _` 抛 `ValueError`。

计时与输出：

[megakernels/scripts/generate.py:167-190](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L167-L190) —— `num_warmup` 次预热 + `num_iters` 次计时，用 CUDA event 测 GPU 时间、`time.time()` 测 CPU 时间，最后打印 token 序列与 tokens/sec。三种模式走的是**同一段计时代码**。

dispatch 工厂：

[megakernels/dispatch.py:17-42](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py#L17-L42) —— 三个字典 `BUILDER_MAP`/`MK_INTERPRETER_MAP`/`INSTRUCTION_TO_SOLVER_MAP` 按 `setting`（latency/throughput）选实现；`make_schedule_builder`/`make_mk_interpreter`/`make_pyvm_interpreter` 三个函数封装查表。注意 `mk` 解释器还要额外传 `mk_dir`（指向编译好的 `mk_llama` 所在目录），`pyvm` 只需 solver 表。

#### 4.5.4 代码实践

**目标**：亲手切换 `mode`，确认三种模式都走同一段主干代码。

1. 阅读第 146–165 行，确认三个分支**只在"构造哪个 Generator"上有别**，之后的计时循环（第 169 行 `gen.generate(...)`）完全一致。
2. 分别用 `mode=torch`、`mode=pyvm`、`mode=mk` 跑同一 prompt（命令见 4.2/4.3/4.4.4），观察脚本**末尾打印的 `Average time` 与 `Tokens per second`**：
   - `torch` 通常最慢（未融合、Python 循环开销大）；
   - `pyvm` 也很慢（逐条 PyTorch solver）；
   - `mk` 最快（一个融合内核）。
3. 故意传一个非法值，观察报错：

   ```bash
   python megakernels/scripts/generate.py mode=zzz ntok=5
   ```

**预期结果**：第 3 步应抛出 `ValueError: Invalid mode: zzz`（来自 `case _`），印证"只有 torch/pyvm/mk 三种合法执行器"。

> 若本地无法运行，改为**源码阅读型实践**：在 dispatch.py 三个 map 里各找一项，画出 `setting=latency` 时 `pyvm` 与 `mk` 分别拿到哪个解释器类，确认二者来自同一族（都是 latency 版）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `generate.py` 在 `torch` 模式下也执行了 `make_schedule_builder` + `tensorize_instructions`（第 139–144 行）？这不是浪费吗？
**参考答案**：从 `torch` 单次运行看确实"用不上" schedule。但保留它保证了**三种模式在完全相同的调度结果上对拍**，也让脚本结构统一、减少分支。代价是多花一点构建时间，对基准测试的"生成阶段"计时无影响（计时只包住 `gen.generate`）。

**练习 2**：`make_mk_interpreter(mode, mk_dir)` 需要传 `mk_dir`，而 `make_pyvm_interpreter(mode)` 不需要。为什么？
**参考答案**：`mk` 解释器要动态加载编译好的 CUDA 扩展 `mk_llama`，该扩展存在于具体目录（默认 `demos/low-latency-llama`），所以需要路径；`pyvm` 解释器只用纯 Python 的 solver 函数表，没有外部编译产物，故无需路径。

---

## 5. 综合实践：三模式对拍，定位正确性

**任务**：用同一 prompt 在三种模式下生成，逐 token 对比，并据此回答"`pyvm` 为什么是 `mk` 的理想对照基准"。

**操作步骤**：

1. 准备：完成 U1·L3 的 `make`，确保 `mk_llama` 已编译（否则 `mk` 步骤跳过，仅做 torch/pyvm 对拍）。
2. 固定 prompt 与长度，依次运行（`ntok` 用小一点便于人工核对，如 20）：

   ```bash
   python megakernels/scripts/generate.py mode=torch prompt="The capital of France is" ntok=20 tokens=false
   python megakernels/scripts/generate.py mode=pyvm prompt="The capital of France is" ntok=20 tokens=false
   python megakernels/scripts/generate.py mode=mk   prompt="The capital of France is" ntok=20 tokens=false
   ```

   （`tokens=false` 关掉文本解码、只看 id 也行；若想看文本就去掉该参数。）
3. 把三组 `Output ids:` 抄成三行，逐位比较。

**需要观察的现象 / 预期结果**：

| 对比 | 期望 | 若不符，bug 大概率在 |
| --- | --- | --- |
| `torch` vs `pyvm` | 逐位一致（或仅在浮点误差边缘偶发不同） | **调度器**（把模型编译成指令的过程） |
| `pyvm` vs `mk` | 逐位一致 | **CUDA megakernel** 的执行 |
| `torch` vs `mk` | 间接一致（由上面两条传递保证） | —— |

**解释 `pyvm` 为何是 `mk` 的理想对照基准**（用你自己的话写下来，再对照 4.3.1 核对）：因为 `pyvm` 与 `mk` **共享同一份调度与几乎相同的 `globs` 初始化**，唯一差别是执行器（PyTorch solver vs CUDA megakernel），所以 `mk`↔`pyvm` 的差异可以**排除调度器因素、直接锁定内核**。`torch` 则不走指令流，无法做这种精细定位。

> 若本地无 GPU/权重：改为**源码阅读型综合实践**——画出 `output_tokens` 在三种模式下被填充的调用链（`main`→`match`→`Generator.generate`→子类单步），标出三者"在哪一行分叉、又在哪一行汇合（计时循环）"。

## 6. 本讲小结

- 三种模式 `torch` / `pyvm` / `mk` 对**同一输入**产出 token，区别只在"**用什么执行器算一次前向**"：PyTorch 整模型、Python 逐条虚拟机、CUDA 融合 megakernel。
- `Generator` 基类用**模板方法**把"按 EOS 提前停止"的循环抽到 `generate_with_eos`，三个子类只需实现单步 `generate`；基准脚本 [generate.py:174](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L174) 调 `generate`，交互脚本调 `generate_with_eos`。
- `torch` 是**金标准**（直接跑模型，不碰指令）；`pyvm` 是**调度后的参考虚拟机**（逐条用 PyTorch solver 执行指令）；`mk` 是**真实高性能内核**（一个 megakernel 吃下整个 `globs`）。
- 正确性对照分两层：`torch`↔`pyvm` 验证**调度器**，`pyvm`↔`mk` 验证**内核**；`pyvm` 是 `mk` 的理想基准，因为两者共享调度、只差执行器。
- `generate.py` 的 `match config.mode` 选择生成器，`dispatch.py` 按 `setting` 选择调度器/解释器实现族；`mk` 模式还带 `skip_mk`/`skip_rest`/`noops`/`barrier_fill_val` 等调试开关用于分层压测。

## 7. 下一步学习建议

- **下一讲 U2·L2（LlamaForCausalLM 模型定义与权重堆叠）**：去读 `llama.py`，弄清 `torch` 模式里 `self.model(...)` 到底算了什么、各层权重是如何"堆叠"成单张大张量供 megakernel 一次性读取的——这将解释 `mk` 模式为何能把 `globs` 一口气喂给内核。
- **深入 pyvm 的 solver**：读 `megakernels/demos/latency/python_vm.py` 的 `INSTRUCTION_TO_SOLVER`，看每种指令分别对应哪个 PyTorch 函数（`matvec`/`rms_norm` 等），理解"指令流"的颗粒度。
- **深入 mk 的内核侧**：进入 `include/megakernel.cuh` 与 `demos/low-latency-llama/llama.cu`，看控制器 warp 如何从 `globs.instructions` 取指、各 warp 如何分工执行（U5 系列）。
- **对拍工具**：留意仓库里的 diff testing 改进（见 git log 的 "diff testing improvements"），那是把本讲的"三模式对拍"自动化、规模化的工程化做法。
