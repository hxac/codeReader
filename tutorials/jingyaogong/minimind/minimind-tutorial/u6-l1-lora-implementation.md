# LoRA 原理与从 0 实现

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 LoRA（Low-Rank Adaptation）为什么用两个低秩矩阵 `B·A` 去逼近全量权重更新 `ΔW`，以及为什么这样能省参数。
- 读懂 MiniMind 里 `model/model_lora.py` 全部 66 行代码：`LoRA` 模块、`apply_lora` 的 monkey-patch 注入、`save_lora`/`load_lora` 的增量存读、`merge_lora` 的合并导出。
- 动手对一个加载好的模型调用 `apply_lora`，统计 LoRA 可训练参数占比，并用 `save_lora`/`load_lora` 验证增量权重能精确复现输出。

本讲只讲 **LoRA 算法本身与它的从 0 实现**，不讲 LoRA 的训练流程（那是 u6-l2 的内容），也不讲模型结构（已在 u3 系列讲过）。

## 2. 前置知识

在进入 LoRA 之前，先建立三点直觉。

### 2.1 全参数微调的"贵"在哪

Full SFT（u5-l2）会更新模型里 **每一个** 参数。对一个约 64M 参数的小模型，这意味着反向传播时要为每个参数都存一份梯度、走一次优化器状态更新。当模型大到几十亿、几百亿参数时，显存和算力都吃不消。

但研究表明：微调时权重的实际更新量 `ΔW` 往往是 **低秩**（low-rank）的——也就是说，虽然 `ΔW` 是个 `d×d` 的方阵，它的有效信息可以用一个远小于 `d` 的秩 `r` 来表达。LoRA 就是利用这一点，不去学完整的 `ΔW`，而是学它的低秩近似。

### 2.2 矩阵的低秩分解

一个 `d×d` 的矩阵 `ΔW`，如果它的秩不超过 `r`（`r ≪ d`），就可以分解成两个小矩阵的乘积：

\[
\Delta W \approx B \cdot A,\quad B \in \mathbb{R}^{d\times r},\ A \in \mathbb{R}^{r\times d}
\]

参数量从 `d×d = d²` 降到 `d×r + r×d = 2dr`。以 MiniMind 默认 `d=768`、`r=16` 为例：

\[
d^2 = 768^2 = 589824,\qquad 2dr = 2 \times 768 \times 16 = 24576
\]

参数量减少为原来的 `r/d = 16/768 ≈ 2%`，即 **24 倍压缩**。

### 2.3 原模型怎么"不受影响"

LoRA 的精髓是：**基模权重 `W` 完全冻结**，只额外训练 `A` 和 `B`。前向时输出变成：

\[
y = Wx + \Delta W x = Wx + BAx
\]

为了让训练第 0 步时模型行为与原模型 **完全一致**（避免一上来就把基模搞坏），LoRA 把 `B` 初始化为全 0，于是 `BA = 0`，`ΔW` 贡献为零；`A` 用小高斯初始化，保证 `B` 一旦开始学习，梯度能正常回传。这是 LoRA 能"无缝接力"已训练好基模的关键。

> 术语速查：**rank（秩）** = 低秩矩阵的中间维度，越大表达能力越强、参数越多；**monkey-patch** = 运行时替换某个对象的方法（这里替换 `Linear.forward`）；**tie / merge** = 把 `BA` 算进 `W` 里，得到一个不再依赖 LoRA 分支的完整权重。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到 |
|------|------|---------|
| `model/model_lora.py` | LoRA 的全部实现，仅 66 行 | 全部精读 |
| `trainer/train_lora.py` | LoRA 训练脚本（u6-l2 详讲） | 仅看它如何调用 `apply_lora`/`save_lora` |
| `eval_llm.py` | 推理入口，演示 LoRA 增量加载 | 仅看 `init_model` 里 4 行调用 |
| `model/model_minimind.py` | 模型结构定义 | 确认哪些 `nn.Linear` 是方阵 |

永久链接统一前缀为：

```
https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/
```

## 4. 核心概念与源码讲解

本讲把 `model_lora.py` 拆成四个最小模块：`LoRA` 模块、`apply_lora` 注入、`save_lora`/`load_lora` 增量存读、`merge_lora` 合并回基模。其中前三个对应大纲指定的最小模块（`LoRA`、`apply_lora`、`merge_lora`）。

### 4.1 LoRA 低秩分支模块

#### 4.1.1 概念说明

`LoRA` 是一个独立的 `nn.Module`，它包装了两个无偏置的 `nn.Linear`：`A` 把输入从 `d` 维压到 `r` 维，`B` 再从 `r` 维升回 `d` 维。它本身不持有原权重 `W`，只是一个"旁路"——计算 `BAx` 这个增量。

为什么要拆成"先降后升"两个 `Linear`，而不是直接学一个 `d×d` 的 `ΔW`？因为 `rank=r` 的瓶颈结构 **强制** 让学到的更新是低秩的，这正是 LoRA 省参数和防过拟合的核心约束。

#### 4.1.2 核心流程

```
输入 x  (..., d)
  │
  ▼
A: Linear(d → r)   # 高斯初始化 N(0, 0.02)
  │                 输出 (..., r)
  ▼
B: Linear(r → d)   # 全 0 初始化
  │                 输出 (..., d)
  ▼
返回 B(A(x))       # 即 ΔW·x
```

初始化的"非对称"是关键：
- `A` 用小随机值（`normal_(0, 0.02)`），保证输入端有信号进入瓶颈层；
- `B` 全 0（`zero_()`），保证 `B(A(x)) = 0`，训练起点与基模完全一致。

#### 4.1.3 源码精读

LoRA 模块的定义与初始化：[model/model_lora.py:6-18](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_lora.py#L6-L18)

```python
class LoRA(nn.Module):
    def __init__(self, in_features, out_features, rank):
        super().__init__()
        self.rank = rank
        self.A = nn.Linear(in_features, rank, bias=False)   # (rank, in_features)
        self.B = nn.Linear(rank, out_features, bias=False)  # (out_features, rank)
        self.A.weight.data.normal_(mean=0.0, std=0.02)      # A 高斯初始化
        self.B.weight.data.zero_()                          # B 全 0 初始化

    def forward(self, x):
        return self.B(self.A(x))
```

要点逐行：

- `self.A = nn.Linear(in_features, rank, bias=False)`：注意 `nn.Linear` 的权重形状是 `(out_features, in_features)`，所以 `A.weight` 形状为 `(rank, in_features)`，即 `(r, d)`。
- `self.B = nn.Linear(rank, out_features, bias=False)`：`B.weight` 形状为 `(out_features, rank)`，即 `(d, r)`。`A` 与 `B` 都不加偏置（`bias=False`），与基模里 `q_proj`/`o_proj` 也是无偏置保持一致。
- `self.A.weight.data.normal_(...)` 与 `self.B.weight.data.zero_()`：直接在 `.data` 上原地改写初始化值，绕过 autograd。
- `forward` 就是 `B(A(x))`，等价于 `ΔW·x`。

#### 4.1.4 代码实践

**目标**：验证 `B` 全 0 时，LoRA 分支输出恒为 0；手算一个 LoRA 的参数量。

**操作步骤**：

```python
import torch
from model.model_lora import LoRA

lora = LoRA(in_features=768, out_features=768, rank=16)
x = torch.randn(1, 10, 768)
print("LoRA 输出:", lora(x).sum().item())        # 期望 0.0
print("A 参数量:", lora.A.weight.numel())         # 16*768 = 12288
print("B 参数量:", lora.B.weight.numel())         # 768*16 = 12288
print("总参数量:", sum(p.numel() for p in lora.parameters()))  # 24576
```

**需要观察的现象**：
- LoRA 输出应为 `0.0`（因 `B` 全 0）。
- 单个 LoRA 总参数量为 `24576 = 2×768×16`。

**预期结果**：输出 `0.0`，总参数量 `24576`。（数值为确定性计算，可直接预期；具体运行请待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：如果把 `rank` 从 16 改成 32，单个 LoRA 参数量变为多少？是原来的几倍？

**答案**：`2×768×32 = 49152`，是 `rank=16` 时的 2 倍。参数量随 `rank` 线性增长。

**练习 2**：为什么不能把 `A` 也初始化为全 0？

**答案**：若 `A` 与 `B` 都为 0，则 `B(A(x))` 对 `A` 的梯度也恒为 0（链式法则中 `B` 全 0 把梯度截断了），`A` 永远学不动。必须至少有一个非零初始化打破对称；LoRA 选择让 `A` 非零、`B` 为零，从而同时满足"起点 ΔW=0"和"梯度可回传"两个要求。

---

### 4.2 apply_lora：用 monkey-patch 把旁路挂到方阵 Linear 上

#### 4.2.1 概念说明

光有 `LoRA` 模块还不够，得把它"挂"到模型里已有的 `nn.Linear` 上，并让前向变成 `original(x) + lora(x)`。MiniMind 的做法是 **monkey-patch**：遍历模型所有子模块，给符合条件的 `Linear` 动态挂一个 `lora` 属性，并用一个新函数替换它的 `forward`。

"符合条件的 `Linear`" 指什么？代码用一行条件筛选：

```python
isinstance(module, nn.Linear) and module.in_features == module.out_features
```

即 **只对方阵 Linear 注入 LoRA**（输入维度等于输出维度）。为什么要加这个限制？

- 对非方阵（如 `k_proj` 是 768→384），`ΔW` 形状是 `384×768`，低秩分解成 `B(384×r)·A(r×768)` 当然也行，但 MiniMind 选择只注入方阵以保持实现极简。
- 在默认配置（`hidden_size=768`、`num_attention_heads=8`、`head_dim=96`）下，模型里 **恰好只有** `q_proj`（768→768）和 `o_proj`（768→768）是方阵；`k_proj`/`v_proj` 是 768→384，FFN 的 `gate_proj`/`up_proj` 是 768→`intermediate_size`，`lm_head` 是 768→6400，都不是方阵，会被自动跳过。

> 可对照 [model/model_minimind.py:100-103](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L100-L103) 验证：`q_proj`/`o_proj` 用 `num_attention_heads * head_dim = hidden_size` 作输出/输入维度，天然是方阵；`k_proj`/`v_proj` 用 `num_key_value_heads * head_dim`，是方阵的一半。

#### 4.2.2 核心流程

```
for 每个 module in model.named_modules():
    if module 是 nn.Linear 且 in_features == out_features:
        1. 新建 lora = LoRA(in, out, rank) 并搬到 model.device
        2. setattr(module, "lora", lora)        # 挂为子模块，进入 state_dict
        3. original_forward = module.forward    # 保存原前向
        4. module.forward = forward_with_lora   # 替换前向：原(x) + lora(x)
```

第 3、4 步是经典的 monkey-patch：不改动 `nn.Linear` 源码、不改模型类定义，运行时把实例的 `forward` 换掉。第 2 步 `setattr(module, "lora", lora)` 很关键——只有作为子模块挂上去，`LoRA` 的 `A`/`B` 参数才会被 `model.parameters()` 收集到，从而能进入优化器与 `state_dict`。

#### 4.2.3 源码精读

apply_lora 的注入逻辑：[model/model_lora.py:21-32](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_lora.py#L21-L32)

```python
def apply_lora(model, rank=16):
    for name, module in model.named_modules():
        if isinstance(module, nn.Linear) and module.in_features == module.out_features:
            lora = LoRA(module.in_features, module.out_features, rank=rank).to(model.device)
            setattr(module, "lora", lora)
            original_forward = module.forward

            def forward_with_lora(x, layer1=original_forward, layer2=lora):
                return layer1(x) + layer2(x)

            module.forward = forward_with_lora
```

逐行要点：

- `rank=16` 是默认秩，调用方可覆盖（`apply_lora(model, rank=8)`）。
- `.to(model.device)`：把 LoRA 搬到与基模相同的设备，避免后续前向因设备不一致报错。
- `setattr(module, "lora", lora)`：挂为 `module` 的属性。因为 `LoRA` 是 `nn.Module`，`nn.Linear` 实例被这样设置子模块后，它的参数会进入父模块的参数树。**注意**：`nn.Linear` 默认不是容器，但 PyTorch 允许运行时给它挂子模块，这些子模块仍会被外层 `model.named_modules()` 枚举到。
- `forward_with_lora(x, layer1=original_forward, layer2=lora)`：这里用 **默认参数** 显式捕获当前的 `original_forward` 与 `lora`，是 Python 闭包的一个经典技巧。

为什么要用默认参数 `layer1=original_forward, layer2=lora` 而不是直接闭包引用？因为 `for` 循环里 `original_forward` 与 `lora` 每轮都会被重新赋值；若闭包延迟引用它们，所有被注入的 `forward_with_lora` 最后都会指向 **最后一轮** 的 `lora`，导致每个 Linear 的旁路都串到同一个 LoRA 上。用默认参数在函数定义时就把值"钉死"，每个 `forward_with_lora` 都绑定自己那一轮的正确实例。

#### 4.2.4 代码实践

**目标**：对一个真实模型调用 `apply_lora`，统计 LoRA 可训练参数占比，并打印被注入的模块名。

**操作步骤**（写成脚本 `inspect_lora.py` 放在仓库根目录运行，或直接在 Python 交互式环境）：

```python
import torch
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM
from model.model_lora import apply_lora

lm_config = MiniMindConfig(hidden_size=768, num_hidden_layers=8, use_moe=False)
model = MiniMindForCausalLM(lm_config).to('cuda' if torch.cuda.is_available() else 'cpu')

total_params = sum(p.numel() for p in model.parameters())

apply_lora(model)   # 默认 rank=16

lora_params = sum(p.numel() for n, p in model.named_parameters() if 'lora' in n)
print(f"总参数量:  {total_params / 1e6:.3f} M")
print(f"LoRA参数量: {lora_params / 1e6:.3f} M")
print(f"LoRA占比:  {lora_params / total_params * 100:.2f}%")

# 打印被注入 LoRA 的方阵 Linear 名称
for name, module in model.named_modules():
    if hasattr(module, 'lora'):
        print("已注入:", name)
```

**需要观察的现象**：
- "已注入" 列表应只包含每层的 `q_proj` 与 `o_proj`（共 `8 层 × 2 = 16` 个），不会出现 `k_proj`/`v_proj`/FFN/`lm_head`。
- LoRA 参数量约为 `0.39 M`，占比约 `0.6%`。

**预期结果**：手算口径——每个 LoRA `24576` 参数，每层 2 个（`q_proj`、`o_proj`），8 层共 `8×2×24576 = 393216 ≈ 0.39 M`；相对约 64M 总参数占比约 `0.61%`。（具体占比随 `get_model_params` 报告的总参数量略有浮动，待本地验证精确值。）

> 这一统计逻辑与训练脚本里的完全一致，可对照 [trainer/train_lora.py:133-137](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_lora.py#L133-L137)：训练日志里也会打印这三行（总参数量、LoRA 参数量、LoRA 占比）。

#### 4.2.5 小练习与答案

**练习 1**：如果把模型的 `num_attention_heads` 改成 16（`head_dim` 随之变为 48），`q_proj`/`o_proj` 还会被注入 LoRA 吗？

**答案**：仍会被注入。因为 `q_proj` 的输出维度是 `num_attention_heads * head_dim`，而 `head_dim = hidden_size // num_attention_heads`，所以 `num_attention_heads * head_dim = hidden_size` 恒成立，`q_proj` 始终是 `hidden_size → hidden_size` 的方阵，与头数无关。`o_proj` 同理。

**练习 2**：为什么 `apply_lora` 里 `original_forward = module.forward` 要在循环内、紧挨着 `forward_with_lora` 之前赋值，而不能提到循环外？

**答案**：因为每个 `module` 是不同的 `Linear` 实例，各自的 `forward` 不同。必须在每个被命中的 `module` 上单独抓取它的原始 `forward`，并通过默认参数钉住，才能保证替换后的新前向里 `layer1` 调用的是"这个 module 自己"的原前向，而不是别的 module 的。

---

### 4.3 save_lora / load_lora：只存增量

#### 4.3.1 概念说明

LoRA 的一大优势是 **权重极小**——只存 `A`/`B` 这点增量（几 MB），而不必存几十上百 MB 的基模。MiniMind 用两个对称的函数实现：

- `save_lora`：从模型里挑出所有 `.lora.` 开头的参数，转成 fp16 存盘。
- `load_lora`：读取这个增量文件，挂回对应的 `module.lora`。

因为 LoRA 必须叠加在某个基模之上才有意义，所以推理时永远是一个 **基模 + 一个 LoRA 增量** 的组合。

#### 4.3.2 核心流程

保存：

```
raw_model = model._orig_mod or model      # 剥掉 torch.compile 外壳
for name, module in raw_model.named_modules():
    if hasattr(module, 'lora'):
        收集 {f'{name}.lora.{k}': v.cpu().half()}  # 仅 LoRA 参数，转 fp16
torch.save(state_dict, path)
```

加载：

```
state_dict = torch.load(path)
去掉 'module.' 前缀   # 兼容 DDP 包装
for name, module in model.named_modules():
    if hasattr(module, 'lora'):
        从 state_dict 中筛出 f'{name}.lora.' 的参数
        module.lora.load_state_dict(lora_state)
```

#### 4.3.3 源码精读

save_lora 的实现：[model/model_lora.py:45-53](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_lora.py#L45-L53)

```python
def save_lora(model, path):
    raw_model = getattr(model, '_orig_mod', model)
    state_dict = {}
    for name, module in raw_model.named_modules():
        if hasattr(module, 'lora'):
            clean_name = name[7:] if name.startswith("module.") else name
            lora_state = {f'{clean_name}.lora.{k}': v.cpu().half() for k, v in module.lora.state_dict().items()}
            state_dict.update(lora_state)
    torch.save(state_dict, path)
```

要点：

- `raw_model = getattr(model, '_orig_mod', model)`：剥掉 `torch.compile` 产生的 `OptimizedModule` 外壳，拿到真正的模型（命名规则见 u4-l3）。LoRA 训练默认禁用 `torch.compile`（见 4.3.4 末尾），这行是为兼容性留的兜底。
- `clean_name = name[7:] if name.startswith("module.") else name`：剥掉 DDP 包装带的 `module.` 前缀，让保存的 key 与单卡裸模型一致。
- `v.cpu().half()`：转 CPU、转 fp16。这样保存出的 `.pth` 体积小、可跨设备加载。
- 只收集 `hasattr(module, 'lora')` 的模块，所以基模那 64M 参数 **一个都不存**。

load_lora 的实现：[model/model_lora.py:35-42](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_lora.py#L35-L42)

```python
def load_lora(model, path):
    state_dict = torch.load(path, map_location=model.device)
    state_dict = {(k[7:] if k.startswith('module.') else k): v for k, v in state_dict.items()}

    for name, module in model.named_modules():
        if hasattr(module, 'lora'):
            lora_state = {k.replace(f'{name}.lora.', ''): v for k, v in state_dict.items() if f'{name}.lora.' in k}
            module.lora.load_state_dict(lora_state)
```

要点：

- `map_location=model.device`：把权重搬到目标设备。
- 第二行再次剥 `module.` 前缀，与 `save_lora` 对称。
- 对每个挂了 `lora` 的 `module`，从大 `state_dict` 里筛出本模块的 key（`f'{name}.lora.' in k`），把前缀去掉后喂给 `module.lora.load_state_dict`。这种"按名字前缀筛"的写法保证了即便 LoRA 文件里混了多个模块的参数，也能精确分发。

#### 4.3.4 代码实践

**目标**：用 `save_lora` 落盘、再用 `load_lora` 读回，验证两个模型的输出完全一致（即增量权重被无损复现）。

**操作步骤**（脚本示例）：

```python
import torch
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM
from model.model_lora import apply_lora, save_lora, load_lora

dev = 'cuda' if torch.cuda.is_available() else 'cpu'
cfg = MiniMindConfig(hidden_size=768, num_hidden_layers=8, use_moe=False)

# --- 模型 A：注入 LoRA 后随机走几步，让它学到非零的 BA ---
model_a = MiniMindForCausalLM(cfg).to(dev)
apply_lora(model_a)
# 人为把 B 设成非零，确保 BA != 0，否则看不出差异
for _, m in model_a.named_modules():
    if hasattr(m, 'lora'):
        m.lora.B.weight.data.normal_(0, 0.01)

save_lora(model_a, '/tmp/lora_test.pth')

# --- 模型 B：同结构、同种子，注入一个全新的 LoRA，再 load ---
torch.manual_seed(0)
model_b = MiniMindForCausalLM(cfg).to(dev)
apply_lora(model_b)
load_lora(model_b, '/tmp/lora_test.pth')

# --- 对比：固定输入下两个模型输出应完全相同 ---
x = torch.randint(0, cfg.vocab_size, (1, 8)).to(dev)
with torch.no_grad():
    ya = model_a(x).logits
    yb = model_b(x).logits
print("最大输出差:", (ya - yb).abs().max().item())   # 期望 ~0（fp16 下极小）
```

**需要观察的现象**：
- 保存出的 `/tmp/lora_test.pth` 体积很小（约 1 MB 量级），远小于完整模型权重。
- `load_lora` 后，`model_b` 的输出与 `model_a` 几乎完全一致，最大差在 fp16 精度量级（`1e-3` 量级或更小）。

**预期结果**：最大输出差应接近 0。若把 `B` 保持全 0 不改，则增量恒为 0、两个模型都退化成纯基模，输出差同样为 0——所以本实践故意把 `B` 设成非零来放大信号。（精确数值待本地验证。）

> 关于 `torch.compile` 不兼容：LoRA 训练脚本里有这样一段——[trainer/train_lora.py:164-166](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_lora.py#L164-L166)：即使传 `--use_compile 1`，也会被强制关掉并打印"monkey-patch forward 与 torch.compile 不兼容"。原因正是 `apply_lora` 在运行时改写了 `nn.Linear` 实例的 `forward`，而 `torch.compile` 在编译期就把 forward 图冻结了，二者冲突。详细的训练流程留到 u6-l2 讲。

#### 4.3.5 小练习与答案

**练习 1**：`save_lora` 存出的文件里，key 长什么样？请举一个具体例子。

**答案**：形如 `model.layers.0.self_attn.q_proj.lora.A.weight` 和 `model.layers.0.self_attn.q_proj.lora.B.weight`。即"原模块路径" + `.lora.` + "LoRA 内部子参数名"。其中 `model`、`layers`、`self_attn` 等前缀来自 `MiniMindModel` 的结构命名。

**练习 2**：为什么 `load_lora` 里要对每个 `module` 单独筛 `f'{name}.lora.' in k`，而不是直接 `model.load_state_dict(state_dict)`？

**答案**：因为 LoRA 的 `lora` 子模块是 monkey-patch 挂上去的，且 `load_lora` 接收的 `state_dict` 只含 LoRA 参数、不含基模参数。直接对整个模型 `load_state_dict` 会因缺大量基模 key 而报错（除非 `strict=False`）。逐模块筛出本模块的 key、再喂给 `module.lora.load_state_dict`，既精确又安全。

---

### 4.4 merge_lora：把 BA 合并回基模

#### 4.4.1 概念说明

LoRA 推理时每次前向都要额外算一遍 `BAx`，略有开销；而且很多第三方框架（llama.cpp、ollama、vllm）并不认识"基模 + LoRA 旁路"这种结构。`merge_lora` 的作用是：把训练好的 `BA` **永久加进** 基模的 `W`，得到一个不再依赖 LoRA 分支的完整权重：

\[
W_{\text{merged}} = W + BA
\]

合并后模型结构与原始基模完全一样，可直接当作普通权重部署，但行为已携带 LoRA 微调的效果。

#### 4.4.2 核心流程

```
1. load_lora(model, lora_path)          # 先把增量读进 module.lora
2. 收集基模自身的全部权重（排除 .lora. 参数）
3. for 每个方阵 nn.Linear（不含 .lora.）:
       W = module.weight.data.clone()
       if 模块挂了 lora:
           W += module.lora.B.weight @ module.lora.A.weight   # W + BA
       state_dict[f'{name}.weight'] = W.cpu().half()
4. torch.save(state_dict, save_path)
```

注意第 3 步的矩阵乘法：`B.weight` 形状 `(out, r)`，`A.weight` 形状 `(r, in)`，所以 `B @ A` 形状 `(out, in)`，正好与 `module.weight` 的形状 `(out, in)` 对齐，可以直接相加。对于没有挂 LoRA 的方阵（理论上不存在，但代码兜底），就只 clone 不加。

#### 4.4.3 源码精读

merge_lora 的实现：[model/model_lora.py:56-65](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_lora.py#L56-L65)

```python
def merge_lora(model, lora_path, save_path):
    load_lora(model, lora_path)
    raw_model = getattr(model, '_orig_mod', model)
    state_dict = {k: v.cpu().half() for k, v in raw_model.state_dict().items() if '.lora.' not in k}
    for name, module in raw_model.named_modules():
        if isinstance(module, nn.Linear) and '.lora.' not in name:
            state_dict[f'{name}.weight'] = module.weight.data.clone().cpu().half()
            if hasattr(module, 'lora'):
                state_dict[f'{name}.weight'] += (module.lora.B.weight.data @ module.lora.A.weight.data).cpu().half()
    torch.save(state_dict, save_path)
```

要点：

- 先 `load_lora`：合并的前提是把 LoRA 增量读进内存。
- `state_dict = {k: v ... if '.lora.' not in k}`：先拷一份基模全量权重，但 **排除** 所有 `.lora.` 参数（这些不需要进合并后的权重）。
- 第二个循环遍历所有 `nn.Linear`（且自身不是 `lora` 子模块）：对每个方阵，`clone()` 出它的权重；若挂了 `lora`，就 `+= B@A`。
- 最终 `torch.save`：得到的就是一个标准基模结构的 `.pth`，可直接被 `eval_llm.py --weight xxx` 加载，无需再 `apply_lora`。

调用方在 `convert_model.py` 里的典型用法：[scripts/convert_model.py:107-112](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/convert_model.py#L107-L112)

```python
lm_model = MiniMindForCausalLM(lm_config).to(device)
state_dict = torch.load(base_torch_path, map_location=device)
lm_model.load_state_dict(state_dict, strict=False)
apply_lora(lm_model)
merge_lora(lm_model, lora_path, merged_torch_path)
```

即"加载基模 → 注入空 LoRA 壳 → 读增量并合并 → 落盘"。

#### 4.4.4 代码实践

**目标**：验证 `merge_lora` 后的合并权重，与"基模 + LoRA 旁路"在同一输入下输出完全一致。

**操作步骤**（脚本示例，承接 4.3.4 的 `/tmp/lora_test.pth`）：

```python
import torch
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM
from model.model_lora import apply_lora, load_lora, merge_lora

dev = 'cuda' if torch.cuda.is_available() else 'cpu'
cfg = MiniMindConfig(hidden_size=768, num_hidden_layers=8, use_moe=False)

# --- 方式一：基模 + LoRA 旁路 ---
m1 = MiniMindForCausalLM(cfg).to(dev)
apply_lora(m1)
load_lora(m1, '/tmp/lora_test.pth')

# --- 方式二：合并后的纯基模 ---
m2 = MiniMindForCausalLM(cfg).to(dev)
merge_lora(m2, '/tmp/lora_test.pth', '/tmp/merged.pth')
m2.load_state_dict(torch.load('/tmp/merged.pth', map_location=dev))

x = torch.randint(0, cfg.vocab_size, (1, 8)).to(dev)
with torch.no_grad():
    diff = (m1(x).logits - m2(x).logits).abs().max().item()
print("旁路 vs 合并 最大输出差:", diff)   # 期望极小（fp16 精度量级）
```

**需要观察的现象**：
- 两种方式输出几乎完全一致，说明合并等价于旁路。
- 合并后的 `m2` 不再含任何 `lora` 子模块，`hasattr(m2.layers[0].self_attn.q_proj, 'lora')` 为 `False`。

**预期结果**：最大输出差在 fp16 精度量级（`1e-3` 量级或更小）。（精确数值待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：合并后，模型的可训练参数量变了吗？为什么？

**答案**：合并后模型里 **不再有** `A`/`B` 这些 LoRA 参数，参数量退回到基模的原值（`BA` 已加进 `W`，但 `W` 本来就在，形状没变）。也就是说，合并是把"增量"折进了"存量"，参数总量等于基模。

**练习 2**：如果同一个基模先后训练了两个不同的 LoRA（比如 `lora_medical` 和 `lora_identity`），能否都 `merge_lora` 进同一个基模得到"既懂医学又有自我认知"的模型？

**答案**：理论上可以连续合并（`W + B1A1 + B2A2`），因为加法可交换。但前提是两个 LoRA 学到的更新方向不冲突；若二者在相同参数上往相反方向拉，合并效果会互相抵消，实际表现未必理想。MiniMind 的设计意图是"一次基模挂一个 LoRA"，多 LoRA 叠加属于进阶用法，效果待本地验证。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个"零训练"的 LoRA 全链路验证：

1. **建模型 + 注入**：实例化一个默认 `MiniMindConfig` 的 `MiniMindForCausalLM`，调用 `apply_lora(model)`。
2. **体检**：按 4.2.4 的脚本，打印 LoRA 参数占比，并确认只注入了 `q_proj`/`o_proj`。
3. **人为造增量**：遍历所有 `module.lora`，把 `B` 设成非零小随机（模拟"训练后"状态）。
4. **存增量**：`save_lora(model, 'lora_probe.pth')`，观察文件大小。
5. **读回校验**：新建同结构模型，`apply_lora` 后 `load_lora`，按 4.3.4 比对输出差。
6. **合并校验**：再新建一个模型，按 4.4.4 调 `merge_lora`，比对"旁路"与"合并"输出差。
7. **记录结论**：用一句话写下"LoRA 参数占比"、"存盘体积相对全量权重的比值"、"两次输出差的数量级"。

完成这个流程后，你应该能向别人讲清楚：LoRA 为什么省、怎么挂、怎么存、怎么合，且每一步都能用真实数字佐证。

## 6. 本讲小结

- LoRA 用两个低秩矩阵 `B(d×r)`、`A(r×d)` 逼近全量更新 `ΔW`，把参数量从 `d²` 降到 `2dr`；`B` 全 0、`A` 高斯初始化，保证起点 `ΔW=0` 且梯度可回传。
- `LoRA` 模块只有两个无偏置 `Linear`，前向 `B(A(x))`；它只是旁路，不持有基模权重。
- `apply_lora` 用 monkey-patch：遍历所有 **方阵** `nn.Linear`（默认配置下即每层的 `q_proj`/`o_proj`），挂一个 `lora` 子模块，并用默认参数技巧把原前向与新旁路"钉死"进替换后的 `forward_with_lora`。
- `save_lora`/`load_lora` 只存读 `.lora.` 参数（转 fp16、剥 `module.`/`_orig_mod` 外壳），文件极小；推理时永远是"基模 + LoRA 增量"组合。
- `merge_lora` 把 `BA` 加进基模 `W`（`W += B.weight @ A.weight`），导出一个无 LoRA 分支的标准权重，便于部署到第三方框架。
- LoRA 与 `torch.compile` 不兼容（运行时改 `forward` 冲突编译期图），故训练脚本强制关闭 compile。

## 7. 下一步学习建议

- 下一讲 **u6-l2 LoRA 训练流程与垂域适配** 会把本讲的 `apply_lora`/`save_lora` 放进真正的训练循环：看 [trainer/train_lora.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_lora.py) 如何冻结 base、只把含 `'lora'` 的参数喂给优化器，以及 `lora_{name}_{dim}.pth` 的命名规则。
- 想理解 LoRA 推理加载的组合方式，可回看 [eval_llm.py:24-26](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L24-L26) 的 `apply_lora` + `load_lora` 两行。
- 若对"为什么方阵筛选恰好命中 `q_proj`/`o_proj`"还想深究，建议重读 u3-l2 的 Attention 结构与 [model/model_minimind.py:100-103](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L100-L103) 的投影维度定义。
