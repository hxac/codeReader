# latency 指令集：7 个 opcode

> 本讲对应手册单元 U3·L2，承接 U3·L1（latency 模式整体架构：`globs` / 调度器 / 解释器三件套）。建议你已经知道"一次前向会被编译成一串指令、装进 `globs`、分别交给 `pyvm` 或 `mk` 执行"这件事。本讲我们钻进指令本身：**latency 模式到底定义了哪 7 条指令、它们各自负责什么、按什么顺序串起来、以及调度器怎么估每条指令有多"重"**。

## 1. 本讲目标

学完本讲，你应当能够：

1. **背出 7 个 opcode 的编号与语义**：1 QKV、2 PartialAttention、3 AttentionReduction、4 O_Proj、5 UpGate/SiLU、6 DownProj、7 RMS_LM_Head，并说出每一条对应 Transformer 解码层的哪一段计算。
2. **说清 `opcode` 与 `prev_opcode` 的成对关系**：`opcode` 是指令的"类型编号"，`prev_opcode` 指向"我这条指令要消费谁的产出"；二者一起构成跨指令的 **barrier（屏障）同步协议**。
3. **画出单层的执行顺序图**：明白 7 条 opcode 如何排成一条流水线，以及当前 `skip_attn_reduction=True` 路径下实际发射的是 `1→2→4→5→6`（第 3 条被折叠）。
4. **理解 `cost()` 估算模型**：明白调度器为什么用"块数 × 维度"这种简单乘积来近似一条指令的耗时，以及为什么这种近似就够用了。

## 2. 前置知识

- **Transformer 解码层（decoder block）**：一层大致分两段——**注意力（attention）**和**前馈（MLP / FFN）**，各带一次残差连接和一次归一化。本讲的 7 条 opcode 就是对"一层解码"的细粒度拆分。如果你不熟悉，记住这条主线即可：
  ```
  hidden → [LayerNorm → QKV → Attention → O_Proj → +残差]
        → [LayerNorm → Up/Gate → SiLU → DownProj → +残差] → next hidden
  ```
- **矩阵-向量乘（matvec / matrix-vector product）**：\( y = Wx \)，\( W \) 是权重矩阵，\( x \) 是输入向量。Megakernels 把大 matvec **按输出维度切成很多小块（block）**，每块由不同的 SM（流多处理器，GPU 的计算单元）算。本讲会反复出现"块数 × 块大小 × 归约维度"这种量。
- **指令（instruction）/ opcode**：把一次前向"编译"成一串指令后，每条指令都需要一个**类型编号（opcode）**来标识"我是哪一种计算"。这和 CPU 的机器码 opcode 是同一个意思。
- **`globs`（globals）**：装载所有权重、激活张量、以及指令本身的全局状态对象。本讲的每条指令都从 `globs` 里读输入、往 `globs` 里写输出。详见 U3·L1。

一句话定位本讲：**U3·L1 讲的是"指令流怎么跑"，本讲讲的是"指令流里的指令长什么样、怎么排序、怎么估重"。**

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [megakernels/instructions.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py) | **基类**：`Instruction`（定义 `opcode`/`prev_opcode`/`serialize` 接口）、`NoOp`（opcode 0）、`BaseGlobals`。本讲的"地基"。 |
| [megakernels/demos/latency/instructions.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py) | **本讲主角**：latency 模式的 `Globals` 与全部 7 个指令类、各自的 `opcode()`/`prev_opcode()`/`cost()`。 |
| [megakernels/demos/latency/scheduler.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py) | 把指令拼成 DAG 的地方，`make_dag_layer` 决定了**单层 7 种 op 的实际发射顺序**。 |
| [megakernels/demos/latency/python_vm.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py) | 每条指令对应的 PyTorch solver；这里的 barrier check/update 最直观地展现了 `opcode`/`prev_opcode` 的成对用法。 |
| [megakernels/scheduler.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py) | 通用调度器：`assign_dag_to_sms` / `wave_assign_to_sms` 用 `cost()` 做 SM 负载均衡；`collect_into_waves` 用 `opcode()` 给指令分组。 |

## 4. 核心概念与源码讲解

### 4.1 Instruction 基类：一条指令的四个接口

#### 4.1.1 概念说明

在钻进 7 个具体指令之前，先看它们共同的"骨架"——基类 `Instruction`。所有 latency 指令都继承它，并按统一约定暴露四个能力：

| 能力 | 形式 | 作用 |
| --- | --- | --- |
| `opcode()` | `@classmethod → int` | 这条指令的**类型编号**（1~7）。既是序列化时的"类型标签"，也是 barrier 数组的**下标依据**。 |
| `prev_opcode()` | `@classmethod → int` | 这条指令**消费的前一条指令的 opcode**。用于 barrier 检查：确认上游已完工。 |
| `serialize()` | 实例方法 → `list[int]` | 把指令编码成一串整数（供 megakernel 从 `globs.instructions` 张量里读取）。 |
| `cost(globs)` | 实例方法 → `int` | 估算这条指令的"工作量"，供调度器做负载均衡。**基类不实现它**，由各子类按需定义。 |

注意：`opcode` 和 `prev_opcode` 都是 `@classmethod`——也就是说，**编号属于"这一类指令"，而不是某一条具体指令**。同一个 opcode 的所有实例共享同一个编号。这一点很关键，因为编号被用来索引 barrier 数组：同一 opcode 的所有实例都往同一个"槽"里累加进度。

#### 4.1.2 核心流程

一条指令"被使用"时，编号出现在两个截然不同的场合：

```
场合一：序列化（Python → megakernel）
    serialize() 返回 [opcode, 字段1, 字段2, ...]
    → 第一个整数永远是 opcode，megakernel 控制器靠它知道"这条该做什么"

场合二：barrier 同步（指令之间互相等待）
    运行 opcode=N 的指令时：
      ① 读  barriers[layer, prev_opcode() - 1]   ← 检查上游(prev_opcode)是否完工
      ② 写  barriers[layer, opcode()     - 1]   ← 给自己的槽累加进度，供下游检查
```

所以 `opcode` 一身二任：**类型标签 + 同步槽位编号**。`prev_opcode` 则只服务于同步——它告诉当前指令"你的输入由谁产出，去查谁的槽"。

#### 4.1.3 源码精读

基类 `Instruction` 与四个接口：

[megakernels/instructions.py:83-95](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L83-L95) —— `opcode()`/`prev_opcode()` 在基类里直接 `raise NotImplementedError`（强迫子类实现）；`tags()` 默认返回空字典（给指令打可选标签用，本讲不展开）。**注意这里没有 `cost()`**——基类根本不定义它。

`serialize()` 的实现：

[megakernels/instructions.py:97-119](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L97-L119) —— 第 98 行 `words = [self.opcode()]`，**opcode 永远是编码后的第一个整数**；随后逐个字段追加（`int` 直接加，`list`/`tuple` 先压长度再压元素，`None` 压 0）。这就是 megakernel 控制器"取指译码"的依据。

opcode 0 的特例 `NoOp`：

[megakernels/instructions.py:122-125](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L122-L125) —— `NoOp` 的 opcode 是 0，表示"空操作"。它在 DAG 末尾当哨兵（见 4.3），也被 `noops=true` 调试开关用来把所有指令清零、压测纯框架开销（回顾 U2·L1 的 4.4）。

#### 4.1.4 代码实践

**目标**：亲手验证"opcode 是序列化的第一个整数"。

1. 打开 Python，构造一条 `RMS_LM_Head` 指令并调用 `serialize()`（仅阅读理解，不改源码）：

   ```python
   from megakernels.demos.latency.instructions import RMS_LM_Head
   ins = RMS_LM_Head(start_output_block_idx=0, end_output_block_idx=4)
   print(ins.opcode())          # 应为 7
   print(ins.serialize())       # 第一个数应为 7，后跟 start/end 两个块索引
   ```

2. 再换一条 `LayerNorm_QKV_MatVecRopeAppend`，观察 `serialize()` 第一个数变成 `1`。

**预期结果**：无论哪条指令，`serialize()[0]` 恒等于该指令的 `opcode()`。这印证了"opcode = 类型标签"。

> 若本地没装好环境，改为**源码阅读型实践**：对照 [instructions.py:97-119](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L97-L119)，把 `serialize()` 对 `int` / `list` / `None` 三种字段的编码方式各画一行伪代码。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `opcode()` 是 `@classmethod` 而不是普通实例属性？
**参考答案**：因为编号描述的是"这一**类**指令"的类别，所有同类实例编号相同；而编号被用作 barrier 数组的下标依据，需要"按类"而不是"按实例"来索引。用 classmethod 表达"这是类级别的常量"最自然，也便于跨类互相引用（如 `prev_opcode` 返回 `DownProjResidual.opcode()`）。

**练习 2**：基类 `Instruction` 没有 `cost()`。如果一个子类忘记实现 `cost()`，但又被调度器排进了 DAG，会发生什么？
**参考答案**：调度器会调用 `node.instruction.cost(globs)`，由于实例和类都没有 `cost`，会抛 `AttributeError`。正因如此，**当前不会被调度到的指令（如 `AttentionReduction`，见 4.4）才被允许不写 `cost()`**——它根本不会进入 DAG。

---

### 4.2 7 个 opcode 的语义、编号与职责

#### 4.2.1 概念说明

latency 模式把"一层 Transformer 解码 + 最后的 lm_head"拆成了 7 条指令。编号 1~7 恰好就是**单层执行的自然先后顺序**。先上一张总表（这是本讲要背下来的核心）：

| opcode | 指令类 | 计算语义（Transformer 的哪一段） | `prev_opcode` | 有 `cost()`？ |
| --- | --- | --- | --- | --- |
| 1 | `LayerNorm_QKV_MatVecRopeAppend` | 注意力入口：RMSNorm + QKV matvec + 对 Q/K 做 RoPE + 把 K/V 追加进 KV cache | 6（上一层 DownProj） | ✅ |
| 2 | `PartialAttention` | 注意力前段：取一段 KV，算 \(QK^\top V\) 的一个分片（partial） | 1 | ✅ |
| 3 | `AttentionReduction` | 注意力归约：把多个 partial 用 log-sum-exp 合并成最终 attn_out | 2 | ❌（当前路径不发射） |
| 4 | `O_ProjResidual` | 注意力出口：O matvec + 残差累加回 hidden | 3 | ✅ |
| 5 | `LayerNormDoubleMatVecSiLU` | MLP 入口：RMSNorm + Up/Gate 两个 matvec + SiLU 门控 | 4 | ✅ |
| 6 | `DownProjResidual` | MLP 出口：Down matvec + 残差累加回 hidden | 5 | ✅ |
| 7 | `RMS_LM_Head` | 末尾：对最终 hidden 做 RMSNorm + lm_head matvec，产出 logits | 6（最后一层） | ✅ |

> 记忆口诀：**QKV(1) → Attn(2/3) → O(4) ‖ UpGate(5) → Down(6) → LM_Head(7)**。前 4 条属于注意力段，5/6 属于 MLP 段，7 是全模型末尾只算一次的"出口"。

#### 4.2.2 核心流程

每个 opcode 对应一个 dataclass，字段就是这条指令的"参数"（该算哪些 block、第几层、第几个 partial 等）。所有指令都通过 `@classmethod opcode()` 返回固定编号，`@classmethod prev_opcode()` 返回上游编号。我们逐条看关键字段。

#### 4.2.3 源码精读

**opcode 1 — `LayerNorm_QKV_MatVecRopeAppend`（QKV）**
[megakernels/demos/latency/instructions.py:32-48](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L32-L48) —— 字段 `start_output_block_idx`/`end_output_block_idx` 圈定这条指令负责 QKV 输出的哪些块；`opcode()` 返回 1，`prev_opcode()` 返回 `DownProjResidual.opcode()`=6（即"上一层 DownProj 的产出"）。docstring 说清了它一气呵成的四件事：layernorm + qkv matvec + 对 q/k 做 rope + 把 k/v 追加进 cache。

**opcode 2 — `PartialAttention`**
[megakernels/demos/latency/instructions.py:61-74](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L61-L74) —— 字段 `kv_head_idx`（第几个 KV 头，用于 GQA）、`num_partials`/`partial_idx`（把序列切成 `num_partials` 段，本条算第 `partial_idx` 段）。`prev_opcode()` 返回 1（消费 QKV 产出的 Q/K/V）。

**opcode 3 — `AttentionReduction`**
[megakernels/demos/latency/instructions.py:84-102](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L84-L102) —— 字段含 `reduction_list`（要合并哪些 partial）、`is_terminal`（是不是归约树的最后一跳）。`prev_opcode()` 返回 2。**注意它没有 `cost()`**——因为当前 latency 路径 `skip_attn_reduction=True`，它不会进 DAG（4.4 详述）。

**公共父类 `MatVecAdd` 与"为什么 O_Proj / DownProj 要分开编号"**
[megakernels/demos/latency/instructions.py:105-113](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L105-L113) —— `O_ProjResidual` 和 `DownProjResidual` 都继承 `MatVecAdd`（同样是"matvec + 残差累加"）。第 113 行注释一语道破：**之所以给它们不同 opcode，是为了让运行时知道"该读哪个输入缓冲、查哪个 barrier 槽"**——O_Proj 读 `attn_out`、DownProj 读 `silu_out`，二者必须区分。

**opcode 4 — `O_ProjResidual`**
[megakernels/demos/latency/instructions.py:116-124](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L116-L124) —— `opcode()`=4，`prev_opcode()`=3（消费注意力的产出）。即使第 3 条被折叠，它仍查 slot 3（4.3 解释）。

**opcode 5 — `LayerNormDoubleMatVecSiLU`（Up/Gate/SiLU）**
[megakernels/demos/latency/instructions.py:134-149](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L134-L149) —— `block_idxs` 是它负责的输出块列表；docstring 说明它一次做 layernorm + 两个 matvec（up 和 gate）+ SiLU。`prev_opcode()`=4。

**opcode 6 — `DownProjResidual`**
[megakernels/demos/latency/instructions.py:160-168](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L160-L168) —— MLP 出口 matvec + 残差。`prev_opcode()`=5。

**opcode 7 — `RMS_LM_Head`**
[megakernels/demos/latency/instructions.py:178-189](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L178-L189) —— 注意它**没有 `layer_idx` 字段**——因为 lm_head 全模型只算一次（在所有层之后）。`prev_opcode()`=6，消费最后一层的 DownProj 产出。

#### 4.2.4 代码实践

**目标**：用一张表把"opcode ↔ 类 ↔ 语义"对应死，并验证编号。

1. 阅读时在 4.2.1 的总表旁标注每个类的 `@dataclass` 字段，理解"这条指令的参数在描述什么"。
2. 在 Python 里把 7 个类的 `opcode()` / `prev_opcode()` 一次性打印出来（仅阅读理解）：

   ```python
   from megakernels.demos.latency import instructions as I
   for cls in [I.LayerNorm_QKV_MatVecRopeAppend, I.PartialAttention,
               I.AttentionReduction, I.O_ProjResidual,
               I.LayerNormDoubleMatVecSiLU, I.DownProjResidual, I.RMS_LM_Head]:
       print(cls.__name__, "opcode=", cls.opcode(), "prev=", cls.prev_opcode())
   ```

**预期结果**：`prev_opcode()` 恰好比自己的 `opcode()` 小 1（opcode 1 除外，它的 prev 是 6）——这正是 4.3 要讲的"成对链"。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `RMS_LM_Head` 没有 `layer_idx` 字段，而其它 6 条都有？
**参考答案**：前 6 条 opcode 是**逐层**重复的（每层都跑一遍 1→6），需要 `layer_idx` 区分第几层；而 lm_head（opcode 7）是**全模型唯一的出口**，所有层跑完只算一次，没有"第几层"的概念，自然不需要该字段。

**练习 2**：`O_ProjResidual` 和 `DownProjResidual` 的计算结构几乎一样（都是 matvec + 残差），为什么不让它们共用一个 opcode？
**参考答案**：因为运行时要靠 opcode 决定**读哪个输入缓冲**（O_Proj 读 `attn_out`，DownProj 读 `silu_out`）和**查/写哪个 barrier 槽**。共用 opcode 会让二者在 barrier 数组里抢同一个槽、读错输入。源码第 113 行注释明确点出了这一点。

---

### 4.3 opcode / prev_opcode 的成对关系：单层执行顺序与 barrier 协议

#### 4.3.1 概念说明

`opcode` 和 `prev_opcode` 不是两个孤立的编号，它们共同定义了一条**生产-消费链**：

> 每条指令"产出"写到自己 opcode 对应的 barrier 槽；"下游"指令通过自己的 `prev_opcode` 知道该去查哪个槽，确认上游完工后才动手。

这套机制让"指令之间互相等待"变成纯粹的整数计数：**没有锁、没有显式信号量，只有一个 `barriers` 张量**。形状是 `[层数, opcode 数, 头相关维度]`，下标 `[layer, opcode-1]` 就是"第 layer 层、第 opcode 条指令的进度槽"。

#### 4.3.2 核心流程

把 7 条 opcode 的 `prev_opcode` 串起来，就得到**单层（以及跨层）的执行顺序**：

```
... 上一层 opcode 6 (DownProj)
        │  prev_opcode=6
        ▼
opcode 1  QKV                 (消费上一层 DownProj 的 hidden)
        │  prev_opcode=1
        ▼
opcode 2  PartialAttention    (消费 Q/K/V)
        │  prev_opcode=2
        ▼
opcode 3  AttentionReduction  (消费各 partial)        ← 当前路径被折叠，见下
        │  prev_opcode=3
        ▼
opcode 4  O_Proj + 残差        (消费 attn_out)
        │  prev_opcode=4
        ▼
opcode 5  Up/Gate/SiLU        (消费 hidden)
        │  prev_opcode=5
        ▼
opcode 6  DownProj + 残差      (消费 silu_out)
        │  prev_opcode=6
        ▼
opcode 7  RMS_LM_Head         (只在一层：消费最后一层 DownProj 的 hidden) → logits
```

两条要点：

1. **同一层内**：`prev_opcode` 几乎总是 `opcode - 1`，形成 `1→2→3→4→5→6→7` 的直线链。
2. **跨层衔接**：opcode 1（QKV）的 `prev_opcode=6`，且查的是 `barriers[layer_idx - 1, ...]`——即**上一层的 DownProj 槽**。这正是"上一层算完 hidden，下一层才能开始算 QKV"的同步表达。

barrier 协议在每个 solver 里都是同一个套路：

```
def 某条指令的 solver(globs, instruction):
    # ① 读上游槽，断言上游完工
    op_barriers = globs.barriers[layer, instruction.prev_opcode() - 1]
    assert op_barriers[...] == 期望值
    # ② 干活（读输入、写输出）
    ...
    # ③ 给自己的槽累加进度，供下游检查
    next_op_barriers = globs.barriers[layer, instruction.opcode() - 1]
    next_op_barriers[...] += 本次完成量
```

**当前路径的简化**：`make_dag_layer` 一开始就 `assert globs.skip_attn_reduction`（见源码），此时 `num_attention_partitions=1`，opcode 3（AttentionReduction）**根本不会被实例化进 DAG**。PartialAttention（opcode 2）算完直接把结果写进 `attn_out`，并顺手往 **slot 3** 累加进度。于是 O_Proj（opcode 4）照旧查 slot 3（它的 `prev_opcode=3`）——**barrier 槽的编号约定没变，只是 slot 3 的"生产者"从 AttentionReduction 换成了 PartialAttention**。所以实际发射的每层链是 `1→2→4→5→6`，外加全模型末尾一次 `7`。

#### 4.3.3 源码精读

**QKV 的跨层 barrier（opcode 1 查上一层 slot 6）**
[megakernels/demos/latency/python_vm.py:174-176](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L174-L176) —— `if layer_idx > 0: op_barriers = globals.barriers[layer_idx - 1, instruction.prev_opcode() - 1]`。`prev_opcode()`=6，所以查的是 `barriers[上一层, 5]`，断言它等于 512（= hidden_size/块大小，即上一层 DownProj 全部完工的进度）。这是"跨层衔接"的最直接证据。

**O_Proj 的标准 barrier 三段式（opcode 4 查 slot 3、写 slot 4）**
[megakernels/demos/latency/python_vm.py:85-86](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L85-L86) —— 读 `barriers[layer, prev_opcode()-1]`（slot 3），断言等于 `num_attention_heads`（注意力各头都完工）；[第 103-104 行](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L103-L104) 写 `barriers[layer, opcode()-1]`（slot 4）累加自己完成的块数。

**skip_attn_reduction 时，opcode 2 顶替 opcode 3 的槽**
[megakernels/demos/latency/python_vm.py:327-334](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L327-L334) —— `if globals.skip_attn_reduction:` 分支里，PartialAttention 直接写 `attn_out`，并 `barriers = globals.barriers[layer, AttentionReduction.opcode() - 1]`（即 slot 3）累加头数。这就是"slot 3 的生产者换了人"。

**单层实际发射顺序（调度器视角）**
[megakernels/demos/latency/scheduler.py:270-386](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L270-L386) —— `make_dag_layer` 依次创建：qkv（opcode 1，[282 行](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L282)）→ partial（opcode 2，[300-336 行](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L300-L336)）→ oproj（opcode 4，[346-354 行](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L346-L354)）→ upgate（opcode 5，[362-365 行](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L362-L365)）→ downproj（opcode 6，[374-377 行](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L374-L377)）。**全程没有创建 opcode 3**。每段之间用"依赖前一段全部节点"的方式串成 DAG。

**lm_head 只在全模型末尾加一次**
[megakernels/demos/latency/scheduler.py:256-263](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L256-L263) —— `if nlayers == globs.num_hidden_layers:` 时才 `schedule_lm_head`，把 opcode 7 接在最后一层 DownProj 之后；最后 [265 行](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L265)用一个 `NoOp` 当末尾哨兵。

#### 4.3.4 代码实践

**目标**：动手确认"slot 3 的生产者在 skip_attn_reduction 下换成了 opcode 2"。

1. 阅读 [python_vm.py:327-346](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L327-L346) 的 `partial_attention`：在 `skip_attn_reduction` 分支里，它写哪个槽？在非该分支里，又写哪个槽？
2. 对照 [python_vm.py:85](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L85) 的 `o_proj_residual`：它查的 `prev_opcode()-1` 是 slot 几？
3. 自己用一句话回答："为什么删掉 opcode 3 之后，O_Proj 的 barrier 检查依然成立？"

**预期结果**：PartialAttention 在 `skip_attn_reduction` 时直接往 `attn_out` 写，并累加 slot 3；O_Proj 仍查 slot 3（`prev_opcode`=3）。两者槽位对得上，故省略 opcode 3 不破坏同步。

#### 4.3.5 小练习与答案

**练习 1**：opcode 1（QKV）的 `prev_opcode=6`，但它在 solver 里查的是 `barriers[layer_idx - 1, 5]`（上一层）。为什么是"上一层"而不是"本层"？
**参考答案**：因为 opcode 1 是一层的开头，它消费的 hidden 是**上一层** opcode 6（DownProj）刚写完的残差结果。所以必须等上一层的 DownProj 完工，下标自然落到 `layer_idx - 1`。这把"层与层之间的数据依赖"也编码进了 barrier 协议。

**练习 2**：如果不设置 `skip_attn_reduction`（即真的发射 opcode 3），单层的 opcode 序列会变成什么样？
**参考答案**：会变成完整的 `1→2→3→4→5→6`：PartialAttention（2）只算分片并写中间结果，AttentionReduction（3）再把多个分片用 log-sum-exp 合并进 `attn_out`，然后 O_Proj（4）才消费。当前路径用 `num_partials=1` 让"分片=整体"，于是第 3 步退化成恒等，直接折叠进第 2 步。

---

### 4.4 cost() 估算模型：用块数 × 维度近似指令耗时

#### 4.4.1 概念说明

调度器要把一堆指令分配到有限数量的 SM 上，**尽量让各 SM 的总工作量均衡**。为此它需要一个"这条指令有多重"的指标——这就是 `cost(globs)`。

关键认知：**`cost()` 不是精确的墙钟耗时模型，而是一个"与工作量大致成正比"的整数代理量**。调度器只用它做相对比较（贪心地往当前最闲的 SM 上塞），所以它只要"大的指令别估成小的"就行，绝对值无关紧要。

#### 4.4.2 核心流程

`cost()` 在调度器里被用在三处，全是"比较大小 / 累加均衡"的场合：

1. **优先级计算** `calc_priority`：沿 DAG 反向把下游 `cost` 累加给上游，估出"关键路径权重"。
2. **贪心 SM 分配** `assign_dag_to_sms`：用 `cost` 当 key 的最小堆，每次挑当前累计 cost 最小的 SM 接新指令。
3. **波次分配** `wave_assign_to_sms`：同一 opcode 的一组指令（一个 wave）内，按 `cost` 降序逐个塞给最闲的 SM。

各 opcode 的 `cost()` 形式高度统一——**块数 × 块大小 × 归约维度**。直觉是：这些指令本质都是 matvec（或注意力），其计算量正比于"输出元素数 × 每个输出元素要乘加多少次"，即乘加（MAC）数。

对一条 matvec \( y = Wx \)，\( W\in\mathbb{R}^{O\times I} \)，\( x\in\mathbb{R}^{I} \)，其 MAC 数为：

\[
\text{MAC} = O \times I
\]

若把输出维度 \( O \) 切成大小为 \( B \) 的块、本指令负责 \( k \) 块，则 \( O \approx k\cdot B \)，于是：

\[
\text{cost} \approx (k\cdot B)\times I = \text{块数}\times\text{块大小}\times\text{归约维度}
\]

这正是源码里反复出现的乘积形式。

#### 4.4.3 源码精读

**QKV 的 cost（opcode 1）**
[megakernels/demos/latency/instructions.py:53-58](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L53-L58) —— `(end - start) * qkv_block_size * hidden_size`。`(end-start)`=块数 `k`，`qkv_block_size`=块大小 `B`，`hidden_size`=归约维度 `I`（QKV matvec 在 hidden 维度上归约）。三者相乘 ≈ 这段 QKV 的 MAC 数。

**PartialAttention 的 cost（opcode 2）——换成了"访存量"模型**
[megakernels/demos/latency/instructions.py:76-81](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L76-L81) —— `loaded_seq_len * head_dim * 2`。其中 `seq_len = pos_id + 1`（当前序列长度），`loaded_seq_len = seq_len / num_partials`（本 partial 只读其中一段），`*2` 是 K 和 V 两个向量。**这里估的不是 FLOPs，而是从 KV cache 里加载的元素数**——因为长上下文注意力是**访存受限（memory-bound）**的，运行时间主要由"搬多少 KV 数据"决定。这是 7 条里唯一一个非"块数×维度"的模型，体现了"按瓶颈选代理量"的思想。

**O_Proj / UpGate / DownProj / LM_Head 的 cost（opcode 4/5/6/7）**
- O_Proj：[instructions.py:126-131](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L126-L131) —— `(end - start) * o_proj_block_size * hidden_size`。
- UpGate/SiLU：[instructions.py:151-157](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L151-L157) —— `len(block_idxs) * up_gate_proj_block_size * hidden_size * 2`。末尾 `*2` 是因为要算 **up 和 gate 两个** matvec。
- DownProj：[instructions.py:170-175](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L170-L175) —— `(end - start) * down_proj_block_size * hidden_size`。
- LM_Head：[instructions.py:191-196](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L191-L196) —— `(end - start) * lm_head_block_size * hidden_size`。

形式完全一致，差别只在"用哪个块大小"和 UpGate 的 `*2`。

**AttentionReduction 没有 cost（opcode 3）**——如前所述，它不进 DAG，无需估重；这也解释了为什么基类可以不提供 `cost()`。

**调度器如何消费 cost**
- 贪心分配：[scheduler.py:117-149](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L117-L149) —— `ready_heap` 用 `-cost` 当 key（大 cost 优先，第 121 行），每条指令的 `end_time = start_time + cost`（第 136 行），SM 的累计耗时就是它名下指令 cost 之和。这是一个以 cost 为"时间"的最小堆均衡。
- 波次内排序：[scheduler.py:208-215](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L208-L215) —— 同一 opcode 的 wave 内按 `cost` 降序（第 209 行），重的先分配。
- opcode 用来分组 wave：[scheduler.py:178-191](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L178-L191) —— `collect_into_waves` 用 `cur[-1].opcode() == instruction.opcode()` 判定"是否还是同一类指令"，把连续同 opcode 的指令聚成一个 wave。这里再次用到 `opcode()`。

#### 4.4.4 代码实践

**目标**：解释"为什么 cost() 用块数 × 维度近似耗时就够"，并量化感受。

1. 选 opcode 1（QKV）：设 `hidden_size=H`、`qkv_block_size=16`、某条指令负责 `k` 块。写出 cost = `k·16·H`。
2. 把它和"真实 MAC 数"对比：这段 QKV 的输出元素数 = `k·16`，每个输出元素要在 `hidden_size` 上乘加一次，所以真实 MAC = `k·16·H`。**两者完全相等**——说明对纯 matvec，"块数 × 块大小 × 归约维度"就是 MAC 数本身，是相当准确的代理量。
3. 再看 PartialAttention：它的 cost 用的是"加载元素数"而非 FLOPs。思考：当序列很长时，注意力的瓶颈是算 \(QK^\top\) 的浮点量，还是反复搬 K/V cache 的访存量？据此理解为什么它换了模型。
4. （可选）在 [make_globals](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L103-L112) 里各块大小都设成了 16，思考：如果只改 `qkv_block_size` 这个**常数**，会不会改变各指令 cost 的**相对**大小？（提示：它是公因子，会等比例放大所有 QKV 的 cost，不影响 SM 间的均衡。）

**预期结果**：你能用自己的话讲清——① 对 matvec 类指令，`cost ≈ MAC 数`，是计算量的天然度量；② 对访存受限的注意力，改用"加载元素数"度量；③ 因为 `cost` 只用于调度器的**相对**均衡，绝对尺度无关紧要，常数因子（如块大小）可被约掉。

#### 4.4.5 小练习与答案

**练习 1**：`DownProj` 的真实 matvec 是在 `intermediate_size` 维度上归约（输入是 `silu_out`），但它的 `cost()` 用的是 `hidden_size` 而非 `intermediate_size`。这会让 cost 偏小吗？为什么依然可接受？
**参考答案**：会偏小（漏乘了 `intermediate_size/hidden_size` 倍）。但 `cost()` 只需"相对正比于工作量"即可驱动均衡——只要所有 DownProj 指令用同一个口径估、且和其它 opcode 的相对量级不离谱，调度器就能合理分配。精确性被牺牲，换来的是简单一致的公式。

**练习 2**：调度器 [scheduler.py:121](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L121) 用 `-cost` 做堆 key，意思是"cost 大的先分配"。如果反过来（cost 小的先分配），会出什么问题？
**参考答案**：会把轻指令先塞满某些 SM、重指令堆到少数 SM 上，导致负载极度不均、关键路径拉长。这正是为什么要"重的优先"——先把难啃的活均匀摊开，再用轻活填缝。

---

## 5. 综合实践：画出单层 7 op 执行顺序图 + 论证 cost 近似

**任务**：把本讲两根主线（执行顺序、cost 模型）串成一份小报告。

**操作步骤**：

1. **画顺序图**。基于 4.3.2，画两张图：
   - **设计版（7 op 全集）**：`1 QKV → 2 PartialAttention → 3 AttentionReduction → 4 O_Proj → 5 UpGate/SiLU → 6 DownProj`，并在末尾用虚线标出"跨到下一层时，下一层 opcode 1 的 prev 指向本层 opcode 6"；最后单独画出全模型末尾的 `7 RMS_LM_Head`。
   - **实际版（当前 `skip_attn_reduction=True`）**：把 3 划掉，标注"opcode 2 顶替 slot 3"，得到 `1 → 2 → 4 → 5 → 6`，末尾 `7`。
   在每条边上标出 `prev_opcode()` 的值，并注明 barrier 槽下标 = `opcode - 1`。

2. **挑一条边做 barrier 追踪**。以 `2 → 4` 这条边为例（中间隔着被折叠的 3）：写明谁写 slot 3、谁读 slot 3、各自在 [python_vm.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py) 的第几行。

3. **论证 cost 近似**。回答两个问题（各写 3~5 句）：
   - 为什么对 opcode 1/4/5/6/7，"块数 × 块大小 × 归约维度" ≈ 真实 MAC 数，因而是合理的耗时代理？（用 4.4.4 的推导。）
   - 为什么 opcode 2 改用"加载元素数"，而 opcode 3 干脆没有 cost？（结合"访存受限"和"不进 DAG"。）

**需要观察的现象 / 预期结果**：

- 顺序图里，每条 `prev_opcode` 边都能在 `barriers` 张量里找到对应的 slot 读写，形成闭环；尤其是跨层那条（opcode 1 → 上一层 opcode 6）和折叠那条（opcode 2 顶替 opcode 3）要特别标注。
- cost 论证里，你能点明"代理量只需相对正确、绝对值无关"这一调度器的核心容忍点。

> 若本地无 GPU/无法运行：以上全程是**源码阅读 + 推导**型实践，不依赖运行环境。可把顺序图画成文本框图（如本讲 4.3.2 的样式）直接写在笔记里。

## 6. 本讲小结

- latency 模式定义 **7 个 opcode**：1 QKV、2 PartialAttention、3 AttentionReduction、4 O_Proj、5 UpGate/SiLU、6 DownProj、7 RMS_LM_Head，分别对应 Transformer 解码层 + lm_head 的各段计算。
- `opcode` 是**类级别的类型编号**，一身二任：既是 `serialize()` 的第一个整数（megakernel 取指译码的标签），又是 `barriers[layer, opcode-1]` 的同步槽下标。
- `prev_opcode` 指向"我消费谁的产出"，与 `opcode` 构成**生产-消费 barrier 链**；同层内基本是 `opcode-1`，opcode 1 的 `prev=6` 且查上一层，编码了跨层依赖。
- 当前 `skip_attn_reduction=True` 路径**不发射 opcode 3**：PartialAttention 直接写 `attn_out` 并顶替 slot 3，实际每层链为 `1→2→4→5→6`，末尾全模型加一次 `7`。
- `cost(globs)` 是调度器用的**工作量代理量**：matvec 类用"块数 × 块大小 × 归约维度"≈ MAC 数，PartialAttention 用"加载元素数"（访存受限），AttentionReduction 没有 cost（不进 DAG）。它只需相对正比即可驱动 SM 负载均衡。

## 7. 下一步学习建议

- **下一讲建议（U3·L3 方向）**：进入 `make_dag_layer` / 各 `schedule_*` 函数的细节，看**同一种 opcode 的多条指令如何被切成块、如何分配到不同 SM**——即把本讲的"指令定义"推进到"指令的并行切分"。
- **深入 solver**：读 [demos/latency/python_vm.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py) 的每个 solver，对照本讲的 opcode 语义，理解每条指令"具体在 PyTorch 里怎么算"——这也能巩固你对 barrier 三段式（check → compute → update）的直觉。
- **对照 throughput 模式**：读 [demos/throughput/instructions.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/instructions.py)，比较它和 latency 的 opcode 集有何异同，体会"不同场景定义不同指令集"的设计。
- **通向内核**：本讲的 `serialize()` 把 opcode 编进 `globs.instructions` 张量；后续 U5 系列会进入 CUDA megakernel，看控制器 warp 如何读这个 opcode、据此分发到各 warp 执行。
