# 零气泡机制 WeightGradStore

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出「零气泡（zero bubble）」的本质：把一次反向（backward）拆成「传给下游的输入梯度 \(B\)」和「本层权重的梯度 \(W\)」两部分，并把 \(W\) 从原来的位置**延后**，塞进流水线的空闲气泡里。
- 读懂 `dualpipe/utils.py` 里的 `WeightGradStore` 类，准确描述 `put / flush / pop / clear` 四个方法的**两级缓存 + FIFO 队列**语义。
- 解释 `dualpipe/dualpipe.py` 引擎中的 `enable_zb` 标志如何通过 `WeightGradStore.enabled` 这一全局开关，让用户自定义的 `autograd.Function` 在「立即算」和「丢进队列等会儿算」之间二选一。
- 读懂 `run_backward` 这个薄封装，说清它四个关键字参数的含义，以及为什么中间 stage 用它、而最后一个 stage 用 `loss.backward()`。

本讲只讲这两件工具本身；至于它们被放进「8 步调度」的哪一步、哪一格气泡，会在 u3-l5「DualPipe 八步调度引擎 step()」中承接。

---

## 2. 前置知识

在进入本讲前，请确认你已经具备下列概念（其中第 1、2 项来自 u1-l1 与 u2-l1）：

1. **流水线气泡**：流水线并行在「灌水（fill）」和「排水（drain）」阶段会有设备空闲，这段空闲叫气泡。u1-l1 给出过气泡大小公式：DualPipe 为 \((PP/2-1)(F\&B+B-3W)\)，其中 \(F\) 是一个前向微批次的时间、\(B\) 是一个反向微批次（只算输入梯度）的时间、\(W\) 是「只算权重梯度」的时间、\(F\&B\) 是一次前向与一次反向重叠的时间。
2. **双向流水线与微批次**：每个进程持有两个对称 stage，数据从两端相向喂入（见 u2-l1）。本讲讨论的 `WeightGradStore` 对两个方向一视同仁。
3. **PyTorch autograd 基础**：
   - 前向时 PyTorch 自动建立「计算图」，记录每个算子的反向规则；
   - 调用 `loss.backward()` 会沿图反向传播，把每个 `requires_grad=True` 的叶子张量的 `.grad` 填上；
   - 自定义 `torch.autograd.Function` 通过 `ctx.save_for_backward(...)` 暂存前向需要的张量，在 `backward(ctx, grad_output)` 里用它们手写反传公式。
   > 如果你对最后一点不熟，没关系，本讲 4.1.3 会结合示例代码讲一遍。

一句话点题：**零气泡 = 让权重的梯度 \(W\) 晚点算，用这段计算去填气泡。** `WeightGradStore` 就是用来「攒着、批量、稍后重放」这些被延后的 \(W\) 计算的容器。

---

## 3. 本讲源码地图

本讲只涉及一个源码文件，外加它的两处调用现场：

| 文件 | 作用 | 本讲关注 |
| --- | --- | --- |
| `dualpipe/utils.py` | 通用工具层 | 定义 `WeightGradStore` 类（行 8–33）与 `run_backward`（行 36–43） |
| `dualpipe/dualpipe.py` | DualPipe 引擎 | 看 `enable_zb` 如何驱动 `WeightGradStore`，以及 `pop()` 在何处重放 |
| `examples/example_dualpipe.py` | 示例 | 看 `LinearFunc.backward` 如何检查 `WeightGradStore.enabled` 决定是否延后 |

`dualpipev.py` 里的同名代码与 `dualpipe.py` 几乎逐行一致，本讲以 `dualpipe.py` 为例，结论对 DualPipeV 同样成立。

---

## 4. 核心概念与源码讲解

### 4.1 零气泡的动机：为什么要把权重梯度延后

#### 4.1.1 概念说明

先回忆一次普通反向传播做了什么。对一层线性层 \(y = xW\)，反向时要算两样东西：

- **输入梯度** \(g_x = g_y W\)：必须**立刻**算出来，因为它要传给**上游** stage，上游等着它继续反传。这部分对应公式里的 \(B\)。
- **权重梯度** \(g_W = x^\top g_y\)：只用来更新**本设备**的权重，**没有下游在等它**。这部分对应公式里的 \(W\)。

关键洞察：\(B\) 有「截止时间」（下游在催），\(W\) 没有。于是可以把 \(W\) 从它原本的位置**抠出来**，像一块可移动的拼图，塞进流水线里任何一段空闲气泡。只要在优化器 step 之前算完、累加进 `.grad` 即可，结果分毫不差（因为梯度是累加 `+=`，与执行顺序无关）。

把所有 \(W\) 都这样处理，气泡里就被填满了计算，从外部看「气泡消失了」——这就是 **zero bubble**。这也是为什么 DualPipe 的气泡公式里 \(W\) 前面带负号：每填进去一个 \(W\)，气泡就缩一段。

> 命名提示：本仓库里「zero bubble」与「weight grad 延后」是同一件事的两面。引擎里用 `enable_zb`（zb = zero bubble）作为开关，工具类叫 `WeightGradStore`。

#### 4.1.2 核心流程

把一次反向 chunk 拆成「\(B\) 现场算、\(W\) 延后算」，需要三方配合：

```text
  ┌─ 引擎 (dualpipe.py) ─────────────────────────────────┐
  │  _backward_compute_chunk(phase, enable_zb=True):     │
  │     1. WeightGradStore.enabled = True   ← 打开开关    │
  │     2. run_backward(...) 或 loss.backward()           │
  │           ↓ 触发 autograd，进入用户写的 backward       │
  │     3. WeightGradStore.enabled = False  ← 关掉开关    │
  │     4. WeightGradStore.flush()          ← 封箱进队列   │
  └───────────────────────────────────────────────────────┘
                          │
  ┌─ 用户的 autograd.Function (示例) ──────────────────────┐
  │  backward(ctx, grad_output):                          │
  │     if WeightGradStore.enabled:   ← 开关开着？         │
  │         WeightGradStore.put(grad_weight_fn)  ← 延后    │
  │     else:                                              │
  │         grad_weight_fn()               ← 立刻算        │
  │     grad_input = grad_output @ weight   ← 输入梯度必算 │
  └───────────────────────────────────────────────────────┘
                          │
  ┌─ 引擎：稍后在气泡里 ────────────────────────────────────┐
  │  _weight_chunk():                                     │
  │     WeightGradStore.pop()   ← 开箱，把延后的 W 跑掉    │
  └───────────────────────────────────────────────────────┘
```

要点：

1. **`enable_zb` 是「每 chunk 一个」的引擎参数**，决定这次反向要不要启用零气泡。
2. **`WeightGradStore.enabled` 是「全局」开关**，引擎在调用反向前后把它设 `True/False`，用户的 `backward` 读它来分流。两者一传一接，是引擎与用户代码的「握手协议」。
3. **「立刻算的 \(B\)」永远算**（输入梯度必须给下游），**「可延后的 \(W\)」才看开关**。
4. 一次 `enable_zb=True` 的反向结束后，引擎调一次 `flush()`，把这期间攒下的所有 \(W\) 函数「封成一箱」塞进队列；之后某个气泡里调一次 `pop()`，把这箱原样跑掉。`flush` 次数与 `pop` 次数严格一一对应。

#### 4.1.3 源码精读

引擎里驱动整个机制的，是 `_backward_compute_chunk` 的三行（节选关键部分）：

[dualpipe/dualpipe.py:87-119](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L87-L119) —— `_backward_compute_chunk` 的完整逻辑：进函数先置 `WeightGradStore.enabled = enable_zb`，再做反向，反向结束后立刻置回 `False`，最后若 `enable_zb` 则 `flush()`。

其中最核心的开关与封箱两步：

```python
WeightGradStore.enabled = enable_zb          # 第 97 行：打开/关闭零气泡
...
run_backward(outputs, output_grads)          # 第 111 行：中间 stage 的反向
...
WeightGradStore.enabled = False              # 第 112 行：反向结束，关掉
if enable_zb:
    WeightGradStore.flush()                  # 第 114 行：把这批 W 函数封箱进队列
```

> 注意：`WeightGradStore.enabled` 的开关窗口**恰好包住反向调用**。这样只有这一次反向里产生的 \(W\) 会被延后；窗口一关，即使后面有别处触发 autograd，也不会误投进队列。

「稍后在气泡里重放」的地方是 `_weight_chunk`：

[dualpipe/dualpipe.py:216-223](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L216-L223) —— `_weight_chunk`：先 `_commit_and_wait_comm()`（把此前攒的 P2P 通信提交并等它完成，正是「气泡」的来源），再 `WeightGradStore.pop()` 把一箱延后的 \(W\) 跑掉，填满这段等待。

```python
def _weight_chunk(self) -> None:
    ...
    self._commit_and_wait_comm()   # 等 P2P 通信完成 = 设备原本会空转的气泡
    # Assume FIFO
    WeightGradStore.pop()          # 把延后的权重梯度计算塞进来
```

那么 8 步调度里，到底哪几步会 `enable_zb=True`、哪几步在 `pop` 呢？源码里可以直接数出来：

[dualpipe/dualpipe.py:373-379](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L373-L379) —— Step 3 `nB1W1F1`：`_backward_chunk(1, enable_zb=True)` 后紧跟 `_weight_chunk()`，即「这步反向启用零气泡，下一步立刻 pop 把 \(W\) 填进气泡」。这是零气泡的典型用法。

[dualpipe/dualpipe.py:404-419](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L404-L419) —— Step 6/7：在后半段反向（`nB1B0`）和最后的 `nWB0` 中持续 `enable_zb=True`。

[dualpipe/dualpipe.py:421-425](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L421-L425) —— Step 8 `nW`：纯 `_weight_chunk()` 排水，把队列里剩下的 \(W\) 全部跑完，并用 `assert WeightGradStore.funcs_queue.empty()` 收尾——确保没有任何一次延后的 \(W\) 被遗忘。

读者现在只需记住「step 3/6/7 开启零气泡、step 8 收尾排空」这个结论；具体每步循环几次、对应调度图哪一格，留待 u3-l5。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认「一次 `enable_zb=True` 的反向，恰好对应后续一次 `pop`」这个一一对应关系。

**操作步骤**：

1. 打开 [dualpipe/dualpipe.py:373-425](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L373-L425)。
2. 只数两类调用的**出现次数**，不关心循环次数：
   - 出现 `_backward_chunk(..., enable_zb=True)` 的代码行（会产生一次 `flush`）；
   - 出现 `_weight_chunk()`（即一次 `pop`）的代码行。
3. 分别在 step 3、step 6、step 7、step 8 四个循环里，列出「每个循环迭代内 flush 几次、pop 几次」。

**需要观察的现象**：在每一个使用了零气泡的循环里，`enable_zb=True` 的反向与 `_weight_chunk()` 在数量上保持平衡；最后 step 8 用纯 `_weight_chunk()` 把欠的 `pop` 补齐，直到队列为空。

**预期结论**：整个 `step()` 跑完，`flush` 的总次数等于 `pop` 的总次数，所以末尾 `funcs_queue.empty()` 的断言一定成立。这正是「调度平衡」的体现。

**待本地验证**：若你想用真实日志确认，可在 `flush` 与 `pop` 内各加一行计数打印（仅用于学习，勿提交），跑一次 `examples/example_dualpipev.py`，观察两个计数是否相等。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `_backward_compute_chunk` 里 `WeightGradStore.enabled = False`（第 112 行）删掉，会发生什么？

**参考答案**：反向窗口永远不关闭。那么 `flush()` 之后的**任何**一次 autograd 触发（哪怕来自别处）都会把 \(W\) 函数继续投进 `cache`，却不会被 `flush` 成箱，最终 `funcs_queue` 与 `cache` 对不上，断言失败或梯度漏算。这两行开关必须成对、紧贴反向调用。

**练习 2**：为什么是「输入梯度 \(B\) 必须立刻算、权重梯度 \(W\) 才能延后」，而不是反过来？

**参考答案**：输入梯度 \(g_x\) 要传给**上游 stage**，上游在等它才能继续反传，有硬截止时间；权重梯度 \(g_W\) 只服务**本设备**的优化器更新，没有人在调度上等它，且梯度是累加（`+=`），延后累加与立即累加数值相同。所以只有 \(W\) 具备「可移动、可延后」的自由度。

---

### 4.2 WeightGradStore：两级缓存与 FIFO 队列

#### 4.2.1 概念说明

`WeightGradStore` 是一个**全静态类**：所有方法都是 `@classmethod`，所有状态都挂在类本身上（没有 `self`）。这意味着**每个进程只有一份全局存储**——这恰好契合分布式训练的事实：每个进程独立跑自己那段流水线，互不干扰，各自管理自己 stage 的 \(W\) 延迟队列。

它的数据结构是**两级**：

| 层级 | 字段 | 类型 | 角色 |
| --- | --- | --- | --- |
| 第 1 级 | `cache` | `List[Callable]` | 「正在攒」的当前这一箱：一次反向过程中陆续 `put` 进来的 \(W\) 函数 |
| 第 2 级 | `funcs_queue` | `queue.Queue` | 「已封箱、等重放」的队列：每一项是 `cache` 的一整个快照 |

外加一个布尔开关 `enabled`，控制用户代码是否往 `cache` 里投递。

之所以分两级，是因为「攒」和「重放」是**解耦的两个节奏**：

- 一次反向 chunk 内，会陆续产生多个 \(W\) 函数（一层可能有多个权重），它们先在 `cache` 里**横向攒齐**；
- chunk 结束时 `flush` 把这一整箱**纵向**推进队列；
- 之后调度在某个气泡里 `pop` 出**最老的一箱**，原样执行。

队列用 `queue.Queue`（线程安全的 FIFO），故「先封箱的先重放」。源码注释 `# Assume FIFO`（[dualpipe/dualpipe.py:222](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L222)）即指此。

> 关于顺序与正确性：权重梯度是 `weight.grad += ...` 的累加，**满足交换律和结合律**，所以即便重放顺序改变，最终 `.grad` 数值也不变。FIFO 的真正意义在于**调度平衡**——保证每一次 `pop` 都恰好取走某一次 `flush` 封好的一整箱，使「封箱节奏」与「重放节奏」对齐，从而让 `step()` 末尾 `funcs_queue.empty()` 成为可断言的不变量。

#### 4.2.2 核心流程

四个方法的协作（伪代码）：

```text
put(f):        cache.append(f)              # 攒进当前箱（横向）
flush():       funcs_queue.put(cache)       # 当前箱封好，整体入队（纵向）
               cache = []                   # 开一个新空箱
pop():         funcs = funcs_queue.get()    # 取最老的一箱（FIFO）
               for f in funcs: f()          # 依原顺序执行里面每个 W
clear():       cache = []                   # 清当前箱
               funcs_queue = queue.Queue()  # 清整条队列
```

一次 `enable_zb` 反向的生命周期：

```text
反向开始 → enabled=True
  (autograd 触发 N 次 backward)
    每次 backward 内: put(w_i)      # cache = [w_1, w_2, ..., w_N]
反向结束 → enabled=False
flush()                            # 队列里多了一箱 [w_1..w_N]，cache 清空
  ...
气泡到来:
pop()                              # 取出 [w_1..w_N]，依次 w_1()..w_N()
```

#### 4.2.3 源码精读

整个类只有 26 行，一次性看全：

[dualpipe/utils.py:8-33](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/utils.py#L8-L33) —— `class WeightGradStore` 全貌：类级三字段 + 四个 classmethod。

逐段看：

```python
class WeightGradStore:
    enabled: bool = False            # 全局开关，引擎按 enable_zb 置位
    cache: List[Callable] = []       # 第 1 级：当前正在攒的一箱
    funcs_queue = queue.Queue()      # 第 2 级：已封箱、待重放的队列

    @classmethod
    def put(cls, func):              # 不立即执行，只往当前箱追加
        cls.cache.append(func)

    @classmethod
    def flush(cls):                  # 封箱：把当前箱整体入队，再开新空箱
        cls.funcs_queue.put(cls.cache)
        cls.cache = []

    @classmethod
    def pop(cls):                    # 重放：取最老一箱，原序执行
        assert not cls.funcs_queue.empty(), "Pop empty queue."
        funcs = cls.funcs_queue.get()
        for func in funcs:
            func()

    @classmethod
    def clear(cls):                  # 复位：两个层级都清空
        cls.cache = []
        cls.funcs_queue = queue.Queue()
```

三个值得注意的设计细节：

1. **`put` 不执行，只记账**。真正的 \(W\) 计算被「冻结」进一个闭包里，延迟到 `pop`。这就是延后的物理实现。
2. **`flush` 用赋值而非原地清空**：`cls.cache = []` 把类属性**重新绑定**到一个新空列表，而旧列表（已被 `funcs_queue` 引用）原样保留在队列里。这一点很关键——若写成 `cls.cache.clear()`，就会把队列里那一箱也清空，导致 `pop` 拿到空箱。赋值=`[]` 巧妙地避开了这个陷阱。
3. **`clear` 的存在**：因为状态是类级共享的，每次 `step()` 开头必须复位，否则上一次训练的残留会污染本次。引擎确实在 `_reset_states` 里第一时间调用它：

[dualpipe/dualpipe.py:47-48](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L47-L48) —— `_reset_states` 第一行就是 `WeightGradStore.clear()`，确保每次 step 从干净状态开始。

而用户侧的「投递点」，正是示例里的自定义 autograd Function：

[examples/example_dualpipe.py:20-34](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L20-L34) —— `LinearFunc.backward`：把「算权重梯度」包成 `grad_weight_fn`，根据 `WeightGradStore.enabled` 决定 `put`（延后）还是直接调用（立即）；输入梯度 `grad_input` 无条件立即算。

```python
@staticmethod
def backward(ctx, grad_output):
    input, weight = ctx.saved_tensors
    if weight.grad is None:
        weight.grad = torch.zeros_like(weight)

    def grad_weight_fn():                                   # 把 W 计算包成闭包
        weight.grad += grad_output.flatten(0, -2).T @ input.flatten(0, -2)

    if WeightGradStore.enabled:
        WeightGradStore.put(grad_weight_fn)                 # 开关开着 → 延后
    else:
        grad_weight_fn()                                    # 开关关着 → 立即算
    grad_input = grad_output @ weight                       # 输入梯度 B，永远立即算
    return grad_input, None
```

这段是引擎与用户代码「握手」的另一端：用户**必须**遵守这个约定——把可延后的 \(W\) 包成函数交给 `WeightGradStore`，把必须立刻的 \(B\) 直接算掉。这也是 README 里那句「For real-world applications, you will need to implement a custom `overlapped_forward_backward`」（见 u1-l1）背后的硬约束之一。

#### 4.2.4 代码实践（可运行，无需 GPU）

**目标**：用纯 Python 直接观察 `WeightGradStore` 的两级 FIFO 行为，验证「先封箱的先重放，箱内按 put 顺序」。

**操作步骤**：把下面这段**示例代码**存成 `zb_demo.py` 并运行（`from dualpipe.utils import WeightGradStore` 需要已 `pip install -e .` 安装本包，见 u1-l2；本脚本**不**需要 GPU，也**不**需要分布式）。

```python
# 示例代码：模拟 WeightGradStore 的两级缓存与 FIFO 重放
from dualpipe.utils import WeightGradStore

WeightGradStore.clear()                      # 干净起步（引擎每次 step 也这么做）

def make_fn(tag):
    def fn():
        print(f"    执行: {tag}")
    return fn

# 模拟三次 enable_zb 反向 chunk：每次 put 若干 W 函数后 flush（封箱）
for chunk_id in range(3):
    WeightGradStore.put(make_fn(f"chunk{chunk_id}-fnA"))
    WeightGradStore.put(make_fn(f"chunk{chunk_id}-fnB"))
    WeightGradStore.flush()
    print(f"chunk {chunk_id} 已 flush（一箱入队）")

# 模拟 _weight_chunk：连续 pop，观察顺序
print("开始 pop：")
while not WeightGradStore.funcs_queue.empty():
    WeightGradStore.pop()

assert WeightGradStore.funcs_queue.empty()    # 这正是 step() 末尾的断言
print("队列已空，所有延后的 W 都已执行")
```

> 这里的 `make_fn` 返回一个闭包，闭包捕获了循环变量 `tag`（这里 `tag` 是每次迭代新建的字符串，不会被后续迭代覆盖）。真正项目里的 `grad_weight_fn` 捕获的是 `input`、`grad_output`、`weight` 等张量——只要这些张量在 `pop` 之前还存活，闭包就能正确执行。这也是零气泡的隐含代价：被延后的 \(W\) 所依赖的激活张量要多活一段时间。

**需要观察的现象**：`pop` 的输出严格是 `chunk0-fnA, chunk0-fnB, chunk1-fnA, chunk1-fnB, chunk2-fnA, chunk2-fnB`——既体现箱与箱之间的 FIFO（chunk0 先于 chunk1 先于 chunk2），也体现箱内 `fnA` 先于 `fnB`。

**预期结果**：打印顺序与 put 顺序完全一致；最后断言通过。这说明两级结构整体上是**单一 FIFO**（箱间 FIFO + 箱内有序）。

**联系 `enable_zb`**：在这个模拟里，`flush` 就代表「一次 `enable_zb=True` 反向结束」，`pop` 就代表「一次 `_weight_chunk`」。三次 flush 恰好被三次 pop 消费干净——与 4.1.4 数出来的引擎调用次数一一对应。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `flush` 里的 `cls.cache = []` 改成 `cls.cache.clear()`，4.2.4 的脚本输出会变成什么？

**参考答案**：`cls.cache.clear()` 是**原地清空同一个列表对象**，而 `funcs_queue` 里存的就是这个对象的引用。于是三次 flush 入队的其实是**同一个**已被清空的列表，最终队列里三个「箱子」都指向空列表。`pop` 时每个箱子里没有任何函数可执行，什么都不会打印，最后断言虽然通过（队列空了），但所有 `W` 都**没被真正计算**——梯度全部丢失。这正是为什么源码用赋值 `cls.cache = []`。

**练习 2**：`WeightGradStore` 为什么设计成全静态（无实例、无 `self`），而不是普通类？

**参考答案**：两个原因。其一，分布式训练里**每个进程**独立管理自己 stage 的延迟队列，进程内只需要**唯一一份**全局存储，静态类天然满足「单例」语义，无需传递实例。其二，用户写的 `autograd.Function`（如 `LinearFunc`）要在 `backward` 里直接 `WeightGradStore.put(...)`，静态类让用户无需持有任何引用、直接用类名调用最方便。

---

### 4.3 run_backward：受控的反向触发器

#### 4.3.1 概念说明

`run_backward` 是 `WeightGradStore` 的「搭档」：它负责**真正触发反向传播**，而 `WeightGradStore` 负责决定反向过程中产生的 \(W\) 是立即算还是延后。两者一前一后，共同服务于零气泡。

为什么需要一个自定义的 `run_backward`，而不直接用 `torch.autograd.backward` 或 `tensor.backward()`？因为引擎需要在反向时**精细控制四个开关**，并把反向限定在「指定的输出张量」上、用「指定的种子梯度」启动。`run_backward` 就是把这些控制项固定成适合流水线并行的取值后的薄封装。

#### 4.3.2 核心流程

```text
run_backward(outputs, output_grads):
    kwargs = {keep_graph=False, create_graph=False,
              allow_unreachable=True, accumulate_grad=True}
    Variable._execution_engine.run_backward(outputs, output_grads, **kwargs)
```

- `outputs`：要**从哪些张量开始**反传（中间 stage 的前向输出）。
- `output_grads`：喂给这些输出的**种子梯度**（从下游 stage 收到的输出梯度）。
- 反向一旦触发，会走进用户自定义的 `backward`，于是 `WeightGradStore.enabled` 的开关在此刻生效，决定 \(W\) 去留。

#### 4.3.3 源码精读

[dualpipe/utils.py:36-43](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/utils.py#L36-L43) —— `run_backward`：构造固定 kwargs，调用 PyTorch 内部反向引擎 `Variable._execution_engine.run_backward`。

```python
def run_backward(tensors, grad_tensors):
    kwargs = dict(
        keep_graph=False,         # 反向后释放计算图中间激活，省显存（= retain_graph=False）
        create_graph=False,       # 不为反向本身建图，不支持二阶导
        allow_unreachable=True,   # 某些输出在 autograd 图中不可达时不报错，直接跳过
        accumulate_grad=True,     # 梯度累加进 .grad 而非覆盖
    )
    Variable._execution_engine.run_backward(tensors, grad_tensors, **kwargs)
```

四个关键字参数的含义与选型理由：

| 参数 | 取值 | 含义 | 为什么流水线要这么选 |
| --- | --- | --- | --- |
| `keep_graph` | `False` | 反向后丢弃计算图 | 微批次多、stage 深，保留图会爆显存；反传完即释放 |
| `create_graph` | `False` | 不构造反向的计算图 | 训练只需一阶梯度，建二阶图纯浪费 |
| `allow_unreachable` | `True` | 不可达的 roots 不报错 | 流水线里某些 stage 的部分输出可能没有连到任何需要梯度的叶节点，必须容忍 |
| `accumulate_grad` | `True` | 梯度 `+=` 进 `.grad` | 同一权重在多个微批次间共享，梯度必须累加而非覆盖；也支撑 `WeightGradStore` 的 `weight.grad += ...` |

> `Variable._execution_engine.run_backward` 是 PyTorch autograd 的底层 C++ 引擎入口（`Variable` 来自 `torch.autograd`）。`run_backward` 这个封装本质上是「把高级 API `torch.autograd.backward` 不易直接控制的开关，用底层入口一次性固定好」。

**谁调用它**：`_backward_compute_chunk` 对**中间 stage** 用 `run_backward(outputs, output_grads)`；对**最后一个 stage**（拿到 loss 的那个）则直接 `loss.backward()`：

[dualpipe/dualpipe.py:97-114](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L97-L114) —— 同一段代码里，`is_last_stage` 分支用 `loss.backward()`（loss 是标量，无需种子梯度），其余分支用 `run_backward(outputs, output_grads)`（从输出张量、用收到的输出梯度启动反传）。两者前后都被 `WeightGradStore.enabled` 开关与 `flush` 包裹。

这条 if/else 正是 u3-l2 会展开的「last stage 的 `loss.backward` 与中间 stage 的 `run_backward` 差异」——本讲先建立直觉：**反传的起点不同，但零气泡的开关与封箱流程完全相同**。

#### 4.3.4 代码实践（源码阅读型）

**目标**：理解 `run_backward` 与 `loss.backward()` 的等价点与差异点。

**操作步骤**：

1. 打开 [dualpipe/dualpipe.py:97-114](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L97-L114)。
2. 对照两个分支：
   - last stage：`loss.backward()`，没有第二个参数；
   - 中间 stage：`run_backward(outputs, output_grads)`，传了「起点张量 + 种子梯度」。
3. 思考：为什么 last stage 不需要 `output_grads`？

**需要观察的现象**：last stage 持有的是一个标量 `loss`，标量反向天然不需要外部种子梯度（默认为 1）；中间 stage 持有的是一批输出张量，它们在调度上不是损失，必须用「从下游收到的输出梯度」作为种子，才能正确反传。

**预期结论**：`run_backward` 之所以比 `loss.backward()` 多一个参数，是因为它要支持「从任意中间张量、用任意种子梯度」启动反向——这正是流水线中间 stage 的需求。

#### 4.3.5 小练习与答案

**练习 1**：`allow_unreachable=True` 这个参数，在普通单卡训练里通常不重要，为什么在流水线并行里被显式打开？

**参考答案**：单卡训练里，`outputs` 一般都连到 loss，几乎不会有「不可达」的张量。但流水线并行的中间 stage，`run_backward` 传入的 `outputs` 是本 stage 的前向输出，其中**某些输出可能并不连到任何需要梯度的叶节点**（例如只是中间缓冲、或该微批次的某些路径不需要梯度）。底层引擎默认遇到不可达 roots 会报错，`allow_unreachable=True` 让它静默跳过，避免误报。

**练习 2**：如果把 `accumulate_grad` 设成 `False`，`WeightGradStore` 的 `grad_weight_fn` 里 `weight.grad += ...` 还能正常工作吗？

**参考答案**：会出问题。`accumulate_grad=False` 表示每次反向**覆盖** `.grad`；而 `LinearFunc.backward` 里先手动 `weight.grad = torch.zeros_like(weight)`，多个微批次/多个 \(W\) 函数之间靠 `+=` 累加。一旦引擎层面改成覆盖，时序上可能互相冲掉。`accumulate_grad=True` 与 `WeightGradStore` 的累加语义是一致的配套设计。

---

## 5. 综合实践

把本讲三块知识串起来，完成一个「延迟链路追踪」任务：

**任务**：跟踪一次 `enable_zb=True` 的反向 chunk，画出从「引擎设开关」到「队列被排空」的完整链路，并解释每一步谁在干活。

**操作步骤**：

1. 在 [dualpipe/dualpipe.py:97-114](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L97-L114) 标出四个时序点：① `enabled=True` ② `run_backward`/`loss.backward` ③ `enabled=False` ④ `flush`。
2. 在 [examples/example_dualpipe.py:29-30](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L29-L30) 标出用户的投递点 `put(grad_weight_fn)`，说明它发生在时序点 ② 内部（由 autograd 自动触发）。
3. 在 [dualpipe/dualpipe.py:216-223](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L216-L223) 标出重放点 `pop()`，说明它发生在**另一个时间、另一个气泡里**。
4. 画出一张时序图：横轴是时间，标出「反向 chunk（B 立即算、W 被 put 进 cache）→ flush（封箱入队）→ ……气泡到来…… → pop（开箱跑 W）」。
5. 用一句话回答：`WeightGradStore.enabled` 为什么必须是一个**全局类变量**而不是局部变量？

**预期结果**：你的时序图应清楚显示 \(W\) 计算在时间上被「平移」到了气泡里；第 5 问的答案是——因为 `enabled` 需要被**引擎代码**（`dualpipe.py`）写入、又被**用户代码**（`autograd.Function.backward`）读取，两者没有实例共享的通道，只能借助类级全局变量来「握手」。

**待本地验证**：如有 GPU 环境，在 `flush`、`pop` 内各加计数打印，跑一次 `examples/example_dualpipev.py`，确认 `flush` 总次数 == `pop` 总次数，且 `cal_diff` 校验仍 `< 1e-13`（校验方法见 u1-l2），证明零气泡不改变数值正确性。

---

## 6. 本讲小结

- **零气泡的本质**是把一次反向拆成「必须立即传给下游的输入梯度 \(B\)」与「可延后的权重梯度 \(W\)」，把 \(W\) 塞进流水线气泡，对应 README 气泡公式中 \(W\) 前的负号。
- `WeightGradStore` 是一个**全静态类**，用**两级结构**管理延迟：`cache`（当前箱，`List`）攒函数，`flush` 把整箱推进 `funcs_queue`（`queue.Queue`），`pop` 按先进先出取一箱执行，`clear` 在每次 `step()` 开头复位。
- `put/flush/pop` 是「攒—封箱—重放」三段式；箱间箱内都 FIFO，且因梯度累加满足交换律，重放顺序不影响数值，FIFO 的意义在于**调度平衡**。
- 引擎用**每 chunk 一个**的 `enable_zb` 控制**全局** `WeightGradStore.enabled`，二者构成引擎与用户 `autograd.Function` 的握手协议：用户须把 \(W\) 包成函数 `put`、把 \(B\) 立即算。
- `run_backward` 是对 PyTorch 底层反向引擎的薄封装，固定了 `keep_graph/create_graph/allow_unreachable/accumulate_grad` 四个开关；中间 stage 用它（带种子梯度），last stage 用 `loss.backward()`。
- step 3/6/7 启用零气泡，step 8 纯 `pop` 排空，末尾 `assert funcs_queue.empty()` 保证「封箱数 == 重放数」，无任何 \(W\) 遗漏。

---

## 7. 下一步学习建议

本讲把零气泡的两件工具讲透了，但还没把它们放进完整的调度时间线。建议：

1. **u3-l2 状态管理与计算原语**：精读 `_backward_compute_chunk` 全文，弄清 `outputs/output_grads` 如何从缓冲里取出并置 `None`，以及 last stage 与中间 stage 的反向起点差异。
2. **u3-l3 前反向重叠与 `overlapped_forward_backward`**：看自定义 `LinearFunc` 如何被提升为「一次前向 + 一次反向重叠」的钩子，进一步与 `WeightGradStore.put` 联动。
3. **u3-l5 DualPipe 八步调度引擎 step()**：把本讲的 step 3/6/7/8 放回完整 8 步循环，算清每步循环次数，看零气泡到底填进了调度图的哪几格气泡。
4. **动手验证**：在 `flush`/`pop` 加计数日志，跑通 `examples/example_dualpipev.py`，用 `cal_diff < 1e-13` 确认零气泡不破坏数值正确性（校验方法见 u1-l2）。
