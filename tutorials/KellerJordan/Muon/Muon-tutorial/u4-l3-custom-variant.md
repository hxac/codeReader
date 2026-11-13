# 二次开发：定制你自己的 Muon 变体

## 1. 本讲目标

学完本讲，你应该能够：

1. 识别 `muon_update` 的三个「安全扩展点」——`beta`、`nesterov`、`ns_steps`——并解释为什么改它们不会破坏「lr 以谱范数为单位」的语义。
2. 推导 `update *= max(1, m/n)**0.5` 尺度因子对更新 RMS 的作用，并预测把它关掉后等效最优 lr 会差多少倍。
3. 画出从 `p.grad` 到 `p.add_` 的完整「改动落点地图」，判断一个新特性（如梯度裁剪）应该插在哪一环：插错了位置，改动会被正交化的尺度不变性完全吞掉，变成无效代码。
4. 复制 `muon.py` 为 `my_muon.py`，实现带梯度裁剪与可关断尺度因子的自定义 Muon，并用最小实验验证每个改动。

本讲是手册收官篇：不再解释 Muon 是什么，而是把它当作一个 290 行的**可改造模板**。

## 2. 前置知识

本讲默认你已读完 u2 与 u3 的核心讲义，以下结论直接拿来用（不再重新推导）：

- **尺度不变性**（u2-l1/u2-l2）：Newton-Schulz 迭代先把矩阵除以 Frobenius 范数再迭代，因此对任意常数 \( c > 0 \)，\( \mathrm{NS}(cG) = \mathrm{NS}(G) \)。**正交化只保留方向结构，完全丢弃幅度信息。**
- **lr 的语义**（u2-l1/u3-l4）：Muon 组的 lr 单位是「每次更新的谱范数」，更新 RMS 约为 \( 1/\sqrt{n} \)（\( n \) 为列数/fan-in）；AdamW 组的 lr 是逐坐标 ±lr 的量纲，两者相差约两个数量级，不可互抄。
- **解耦权重衰减必须放在正交化之外**（u2-l5）：`p.mul_(1 - lr*wd)` 若被并进正交化前的更新，衰减强度会被尺度不变性摧毁。
- **分布式一致性闭环**（u3-l3）：梯度一致 + 确定性计算 + 通信对齐 ⇒ 各 rank 参数逐位一致。任何自定义改动若想保留分布式可用性，必须保持**确定性**。
- **NS 迭代动力系统**（u4-l1）：五次迭代后输出奇异值被关进以 1 为中心、约 ±20% 的窄带（上界约 1.2），加步只抬升最小奇异值。

还要一点 PyTorch 工程常识（u2-l4）：优化器构造函数里的 `defaults` 字典会被复制进每个 `param_group`，因此**往 `defaults` 里加一个键，就等于给每个参数组加了一个可独立配置的超参**。这是本讲「打洞」暴露参数的机制基础。

## 3. 本讲源码地图

本讲涉及的文件极少——这正是 Muon 适合二次开发的原因：

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| `muon.py` | 唯一源码文件（287 行） | `muon_update`（L34-L41）、四个类中调用它并施加更新的代码段、构造函数 `defaults` 组装 |
| `README.md` | 项目说明 | 「Hyperparameter tuning」一节：哪些超参免调、哪些要调 |
| `my_muon.py` | **读者自建**（练习目录中，非仓库文件） | 你的定制版本，实践任务的产物 |

关键行号速查（当前 HEAD `f98f1cac`）：

- `muon_update` 定义：`muon.py` L34-L41
- 四个调用点（全部只传了 `beta`，`ns_steps=5`、`nesterov=True` 靠默认值硬编码）：
  - 分布式 `Muon.step`：L91-L93
  - 单机 `SingleDeviceMuon.step`：L123-L125
  - 分布式 `MuonWithAuxAdam.step` 的 muon 分支：L205-L207
  - 单机 `SingleDeviceMuonWithAuxAdam.step` 的 muon 分支：L267-L269

## 4. 核心概念与源码讲解

### 4.1 三个安全扩展点：`beta`、`nesterov`、`ns_steps`

#### 4.1.1 概念说明

先看被改造对象的签名：

> [muon.py:34-L34](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34-L41) —— `muon_update(grad, momentum, beta=0.95, ns_steps=5, nesterov=True)`：整个 Muon 算法的五步流水线（EMA 动量 → Nesterov → 卷积核展平 → 正交化 → 尺度缩放），全部逻辑集中在这 8 行里。

「安全扩展点」的定义：**改它不会改变 lr 的量纲语义**。三个参数都满足这条性质，因为它们都只影响「正交化之前的方向」，而正交化输出的幅度（奇异值带宽）由 NS 系数决定、与输入幅度无关：

- `beta`：动量 EMA 的衰减率。有效平均窗口约 \( 1/(1-\beta) \) 步，`0.95` 对应约 20 步。它决定「把过去多少步的梯度混进当前方向」，但混出来的矩阵幅度如何，正交化并不关心。
- `nesterov`：切换 Nesterov 动量与普通动量。它只是把送入正交化的矩阵从 `momentum` 换成 `grad.lerp_(momentum, beta)`，同样是「只换方向输入」。
- `ns_steps`：NS 迭代次数。它控制输出奇异值带宽的收紧程度（u4-l1 的结论：加步只抬升最小奇异值，最大值被 1.2 左右的硬上界封顶），同时线性改变计算量（每步 3 次矩阵乘）。

对比之下，「不安全」的改动是任何**直接缩放最终更新**的操作——例如尺度因子或学习率本身，它们改变 lr 的有效含义，需要重新调参。这正是 4.2 的主题。

#### 4.1.2 核心流程

三个参数目前的暴露状况是不对称的：

```text
用户代码
  └─ 优化器构造函数（lr / weight_decay / momentum 暴露）
       └─ defaults 字典 → param_groups
            └─ step() 调用 muon_update(..., beta=group["momentum"])
                 ├─ beta      ← 已从 param_groups 传入（可逐组配置）
                 ├─ ns_steps  ← 未暴露，固定 5
                 └─ nesterov  ← 未暴露，固定 True
```

README 的调参建议也印证了这个不对称——

> [README.md:44-L47](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L44-L47) —— 「Typically, the default values of momentum (0.95), nesterov (True), and ns_steps (5) work well. Only the learning rate and weight decay must be tuned.」：官方立场是这三个参数免调，所以 API 干脆不暴露后两个。

二次开发的第一件事就是把这两个「被硬编码的自由度」打洞暴露出来：在构造函数 `defaults` 里加键 → `param_groups` 自动携带 → 调用点透传。

#### 4.1.3 源码精读

**调用点只传 `beta`（四处完全相同）**，以单机版为例：

> [muon.py:123-L125](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L123-L125) —— `SingleDeviceMuon.step` 中：`muon_update(p.grad, state["momentum_buffer"], beta=group["momentum"])` 之后紧跟施加更新的两行。注意 `ns_steps` 与 `nesterov` 没有出现，落回函数默认值 5 和 True。

其余三个调用点结构相同：[muon.py:91-L93](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L91-L93)（分布式 `Muon`）、[muon.py:205-L207](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L205-L207)（`MuonWithAuxAdam`）、[muon.py:267-L269](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L267-L269)（`SingleDeviceMuonWithAuxAdam`）。

**暴露参数的落点——构造函数的 `defaults`**：

> [muon.py:103-L105](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L103-L105) —— `SingleDeviceMuon.__init__` 只有组装 `defaults = dict(lr=..., weight_decay=..., momentum=...)` 并调用父类两件事。要暴露新参数，在这里加键即可（见 4.1.4 示例代码）。

**被影响的两个分支**（复习，各一行）：

> [muon.py:35-L36](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L35-L36) —— `momentum.lerp_(grad, 1 - beta)` 是 EMA 动量更新（`beta` 的作用点）；`update = grad.lerp_(momentum, beta) if nesterov else momentum` 是 Nesterov 切换（`nesterov` 的作用点）。注意第二行**原地改写了 `grad`**。

> [muon.py:39-L39](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L39-L39) —— `zeropower_via_newtonschulz5(update, steps=ns_steps)`：`ns_steps` 的作用点，也是唯一同时影响精度与计算量的参数。

#### 4.1.4 代码实践：把 `ns_steps` / `nesterov` 打洞暴露出来

**实践目标**：亲手完成一次「参数穿透」改造，并跑一个三因子小消融，体感确认「安全扩展点改了也不用重调 lr」。

**操作步骤**（示例代码，工作在你的练习目录，不要改仓库）：

1. 复制 `muon.py` 为 `my_muon.py`，在 `SingleDeviceMuon` 基础上写一个子类化的定制类：

```python
# 示例代码：my_muon.py —— 暴露 ns_steps / nesterov 的最小改法
from muon import muon_update   # 复用原仓库的数学函数，不改它

class MyMuon(torch.optim.Optimizer):
    def __init__(self, params, lr=0.02, weight_decay=0, momentum=0.95,
                 ns_steps=5, nesterov=True):
        defaults = dict(lr=lr, weight_decay=weight_decay, momentum=momentum,
                        ns_steps=ns_steps, nesterov=nesterov)
        super().__init__(params, defaults)

    @torch.no_grad()
    def step(self, closure=None):
        for group in self.param_groups:
            for p in group["params"]:
                if p.grad is None:
                    p.grad = torch.zeros_like(p)
                state = self.state[p]
                if len(state) == 0:
                    state["momentum_buffer"] = torch.zeros_like(p)
                update = muon_update(p.grad, state["momentum_buffer"],
                                     beta=group["momentum"],
                                     ns_steps=group["ns_steps"],
                                     nesterov=group["nesterov"])
                p.mul_(1 - group["lr"] * group["weight_decay"])
                p.add_(update.reshape(p.shape), alpha=-group["lr"])
```

2. 用一个 3 层 MLP（如 `64→128→128→10`，全部 `bias=False`）在合成回归任务上训练 200 步，固定 `lr=0.02`，分别在 `(ns_steps, nesterov) ∈ {2,5,10} × {True,False}` 六种组合下记录最终 loss。

**需要观察的现象**：六条 loss 曲线都收敛、彼此差距远小于改 lr 造成的差距；`ns_steps=2` 略差（小奇异值还没爬进带宽，见 u4-l1 的 5 步覆盖阈值 1/485），`ns_steps=10` 与 5 几乎重合。

**预期结果**：调 `ns_steps`/`nesterov` 不需要动 `lr=0.02`——这就是「安全扩展点」的操作性含义。具体 loss 数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `beta`、`ns_steps`、`nesterov` 三个参数「安全」，而把尺度因子乘 2「不安全」？

**答案**：前三个参数只改变送入（或走出）正交化的**方向与带宽**，输出更新的幅度量级仍由 NS 归一化钳制，lr 的「谱范数单位」语义不变。而把最终更新乘 2 等效于把 lr 乘 2，直接改变每步步长——沿用旧 lr 会训崩或训慢，属于量纲级改动。

**练习 2**：把 `ns_steps` 从 5 改成 10，每步优化的浮点开销大约变多少倍？

**答案**：约 2 倍。NS 迭代循环体内是 3 次矩阵乘（`X @ X.mT`、`A @ A`、`B @ X`，见 [muon.py:25-L27](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L25-L27)），开销与 `steps` 严格线性；流水线其余部分（EMA、缩放、施加）不随步数变化。

**练习 3**：如果你的定制版要在分布式 `Muon` 里用，给 `muon_update` 加「当梯度 Frobenius 范数超过阈值就整体跳过该参数更新」的分支，会破坏 u3-l3 的一致性闭环吗？

**答案**：不会，只要阈值只依赖各 rank 相同的输入。DDP 下各 rank 梯度逐位一致 ⇒ 各 rank 算出的范数相同 ⇒ 分支走向相同 ⇒ 计算仍是确定且同步的。反过来，若阈值依赖某个 rank 局部状态（如本卡 loss），各 rank 走不同分支，`all_gather` 序列错位，就会死锁或参数发散。

### 4.2 尺度因子：一行代码撑起 muP 式宽度无关性

#### 4.2.1 概念说明

> [muon.py:40-L40](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L40-L40) —— `update *= max(1, update.size(-2) / update.size(-1))**0.5`：正交化之后、返回之前，按矩阵长宽比补一个缩放。

设权重矩阵形状为 \( m \times n \)（\( m \)=行数=输出维，\( n \)=列数=输入维/fan-in），正交化输出 \( Q \) 的奇异值约在 1 附近（带宽 0.5~1.5，均值近似 1）。则：

\[ \|Q\|_F \approx \sqrt{\min(m,n)}, \qquad \mathrm{RMS}(Q) = \frac{\|Q\|_F}{\sqrt{mn}} \approx \frac{1}{\sqrt{\max(m,n)}} \]

乘上尺度因子 \( s = \max(1, m/n)^{1/2} \) 后，两种情况统一为：

\[ \mathrm{RMS}(s \cdot Q) \approx \frac{1}{\sqrt{n}} \]

即**更新 RMS 只由 fan-in（列数）决定**，与输出维无关。这就是 u3-l4 提过的「muP 式宽度无关性」：把隐藏层加宽（\( m \) 变大）时，每 个权重的更新幅度不变，`lr` 不需要重调——README 中「The learning rate should have built-in muP scaling」一句的源码依据正是这一行。

它也是历史上真实翻过车的一行：`git show 6456082` 显示，旧代码误写成 `grad.size(-2) / grad.size(-1)`（用卷积核的四维原始尺寸算长宽比），修复后改为 `update.size(...)`——**缩放必须基于 `view` 展平后的二维尺寸**（见 [muon.py:37-L38](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L37-L38)）。二次开发时改这一行要格外小心，这是有前车之鉴的 Bug 热点。

#### 4.2.2 核心流程

关掉尺度因子（`s=1`）后各形状的变化：

| 形状 (\(m \times n\)) | 开（现状）RMS | 关（\(s=1\)）RMS | 关/开 幅度比 | 等效 lr 倍数 |
|---|---|---|---|---|
| \( m \le n \)（收缩/方阵层） | \( 1/\sqrt{n} \) | \( 1/\sqrt{n} \) | 1（不变） | 1 |
| \( m > n \)（扩张层） | \( 1/\sqrt{n} \) | \( 1/\sqrt{m} \) | \( \sqrt{n/m} \) | \( \sqrt{m/n} \) |

两个可直接检验的推论：

1. **方阵层完全不受影响**——若模型隐藏层全是方阵（多数 transformer 的 hidden 权重），开关尺度因子训练结果应当逐位一致。
2. **扩张层（如 `Linear(64→256)`，权重形状 \( 256 \times 64 \)）关掉后更新缩小 \( \sqrt{64/256}=0.5 \) 倍**，需要约 2 倍（即 \( \sqrt{m/n} \)）的 lr 才能等效。

混合方阵与扩张层的模型，全局等效最优 lr 介于 1 与 \( \sqrt{\max(m/n)} \) 之间，取决于扩张层占比与敏感度。

#### 4.2.3 源码精读

> [muon.py:39-L41](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L39-L41) —— 流水线最后两步：先正交化（`steps=ns_steps`），再乘尺度因子。注意顺序不可交换也无从交换——因子作用的正是正交化输出的形状，且必须在返回**之前**完成，因为返回后调用方只做 `reshape` 和施加。

> [muon.py:61-L61](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L61-L61) —— 类 docstring 写明 `lr: The learning rate, in units of spectral norm per update.`。尺度因子是「lr 语义」承诺的一部分：它把不同形状的更新规整到同一 RMS 尺度，使一个 lr 通吃所有隐藏层。改它就是改契约。

#### 4.2.4 代码实践：数值验证 RMS 表

**实践目标**：用 10 行脚本验证上表的 RMS 列，让「关掉尺度因子会破坏宽度无关性」从结论变成亲眼所见。

**操作步骤**（示例代码）：

```python
# 示例代码：验证尺度因子对更新 RMS 的影响
import torch
from muon import zeropower_via_newtonschulz5 as ns5

torch.manual_seed(0)
for m, n in [(512, 1024), (1024, 512), (256, 256)]:
    G = torch.randn(m, n)
    Q = ns5(G, steps=5)
    on  = Q * max(1, m / n) ** 0.5          # 现状：尺度因子开
    off = Q                                   # 定制：尺度因子关
    print(f"{m}x{n}: ON RMS={on.norm()/ (m*n)**0.5:.4f} "
          f"(预测 {1/n**0.5:.4f})  OFF RMS={off.norm()/(m*n)**0.5:.4f} "
          f"(预测 {1/max(m,n)**0.5:.4f})")
```

**需要观察的现象**：三个形状的 `ON RMS` 分别接近 \( 1/\sqrt{1024} \approx 0.031 \)、\( 1/\sqrt{512} \approx 0.044 \)、\( 1/\sqrt{256} = 0.0625 \)；`OFF` 让 \( 1024 \times 512 \) 的 RMS 从 0.044 掉到约 0.031（缩小 \( \sqrt{512/1024} \approx 0.71 \) 倍），而方阵 \( 256 \times 256 \) 的两种设置完全相同。

**预期结果**：实测值与预测值偏差在 ±10% 内（奇异值带宽 0.5~1.5 且均值略偏 1，见 u4-l1）。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：一个全是方阵隐藏层的 MLP，把尺度因子改为可选关闭后做 lr 网格搜索，开/关的等效最优 lr 应差多少倍？

**答案**：1 倍（完全相同）。方阵 \( m = n \)，`max(1, m/n) = 1`，因子本来就等于 1。这也是实践中判断「尺度因子改动是否生效」的最快 sanity check：方阵模型上开关结果应逐位一致，若不一致说明实现改错了位置。

**练习 2**：为什么不把尺度因子设计成 \( \sqrt{\max(m,n)/\min(m,n)} \)，把所有形状的谱范数都拉到同一尺度？

**答案**：那样 RMS 就不再只依赖 fan-in：\( m>n \) 时谱范数与 Frobenius 范数不可同时归一（二者比值 \( \sqrt{\min(m,n)} \) 随形状变化）。现有因子选择锚定 **RMS ≈ 1/√fan-in**，对齐的是「每个权重受到的相对扰动」（muP 式更新尺度），这与初始化理论（如 He/Kaiming 的 \( 1/\sqrt{n} \)）同构；锚定谱范数则会让宽层的逐权重更新随宽度缩水。

### 4.3 改动落点地图与施加更新的两行：梯度裁剪应该插在哪

#### 4.3.1 概念说明

所有定制最终都要挂到这条流水线上。把 `muon_update` 内部展开，可插入特性的位置只有五个：

```text
p.grad
  │ (A) 裁剪原始梯度 —— 会写入 EMA，影响后续所有步
  ▼
momentum.lerp_(grad, 1-beta)          [muon.py:35]
  ▼
grad.lerp_(momentum, beta) / momentum  [muon.py:36]   ← nesterov 切换点
  │ (B) 裁剪动量合成后的 update —— ⚠ 正交化之前：尺度不变性使其无效
  ▼
update.view(len(update), -1)           [muon.py:37-38] ← 卷积核展平
  ▼
zeropower_via_newtonschulz5(...)       [muon.py:39]
  │ (C) 缩放/裁剪正交化输出 —— 有效，改变最终步长
  ▼
update *= max(1, m/n)**0.5             [muon.py:40]   ← 4.2 的尺度因子
  │ (D) 再裁剪 —— 有效，等价于 C 处乘形状相关常数
  ▼
return update
  ▼
update.reshape(p.shape)                [调用方]
p.mul_(1 - lr*wd); p.add_(update, alpha=-lr)   (E) 施加点
```

三条判据决定你该选哪个落点：

1. **尺度不变性判据**：任何「乘一个数」的改动放在 B 点及以前都会被正交化吞掉（\( \mathrm{NS}(cG)=\mathrm{NS}(G) \)），沦为无效代码；要影响步长必须放 C/D。
2. **状态判据**：放 A 点的改动会改变 `momentum_buffer` 的演化（影响未来所有步）；放 C/D 的改动只影响当前这一步，状态不受污染。
3. **lr 语义判据**：放 C/D 的常数缩放等效于改 lr——若你的本意是「新特性」，别让它顺手改了量纲。

**梯度裁剪落点详解**（本讲实践任务的核心）：把「对动量后的 update 按 Frobenius 范数裁剪」直译到 B 点是初学者最常见的坑——**完全无效**。正确位置在 C/D：裁剪**正交化之后**的最终更新。而 Muon 对极端梯度的鲁棒性其实已经内建：梯度再大，正交化后的更新范数 \( \approx \sqrt{\min(m,n)} \cdot s \) 只由形状决定，与梯度幅度无关。真正需要 A 点入口裁剪的只有一种场景：梯度含 `Inf/NaN`（`Inf` 会让 NS 归一化产出 `NaN`，且裁剪系数 `min(1, c/Inf)=0` 乘到 `Inf` 元素上仍是 `NaN`，救不回来）。

#### 4.3.2 核心流程

以「施加点 E」的视角复习最后两行（它们在四个类中重复出现六次）：

> [muon.py:124-L125](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L124-L125) —— `SingleDeviceMuon.step` 的收尾两行：`p.mul_(1 - lr*wd)` 先做解耦权重衰减，`p.add_(update.reshape(p.shape), alpha=-lr)` 再沿正交化方向走一步。`reshape` 不可省——卷积核在 `muon_update` 内被 `view` 成了矩阵，施加前要还原四维形状。

定制类若新增逐组超参（如 `clip_update_norm`），只需在这两行之前取 `group["clip_update_norm"]` 传给定制版 `muon_update`。注意：**decay 施加顺序与更新计算无关**（先 `mul_` 后 `add_` 与先算更新后衰减等价，因为更新 `u` 不依赖 `p` 的当前值），所以 E 点附近还有最后一个安全插槽：在 `add_` 之前对 `update` 做最终修正。

#### 4.3.3 源码精读

> [muon.py:34-L41](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34-L41) —— 完整流水线原文。改造模板：L35-L36 是 A/B 落点区，L39 是正交化，L40 是 C 落点（尺度因子），L41 之前是 D 落点。

> [muon.py:92-L93](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L92-L93) —— 分布式 `Muon.step` 的施加两行。与单机版唯一的上下文差异：这两行执行完紧接着 `all_gather`（L94），因此任何挂在 C/D 的新特性必须保持确定性，否则破坏 u3-l3 的一致性闭环。

> [muon.py:267-L269](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L267-L269) —— `SingleDeviceMuonWithAuxAdam` 的 muon 分支：`muon_update` 调用加施加两行。本讲综合实践选它做宿主（单机可跑、又能同时练 muon/adam 两组参数）。

#### 4.3.4 代码实践：给 `muon_update` 加 Frobenius 范数裁剪（放对位置）

**实践目标**：实现规格中的定制 (1)——对动量后的更新按 Frobenius 范数裁剪——并先亲测「插错位置无效」这个坑。

**操作步骤**（示例代码）：

```python
# 示例代码：my_muon.py 中的定制版 muon_update（基于 muon.py:34-41 修改，改动点已注释）
def my_muon_update(grad, momentum, beta=0.95, ns_steps=5, nesterov=True,
                   scale_adjust=True, clip_update_norm=None):
    momentum.lerp_(grad, 1 - beta)
    update = grad.lerp_(momentum, beta) if nesterov else momentum
    if update.ndim == 4:
        update = update.view(len(update), -1)
    update = zeropower_via_newtonschulz5(update, steps=ns_steps)
    if scale_adjust:                                    # 定制 (2)：尺度因子可关
        update *= max(1, update.size(-2) / update.size(-1))**0.5
    if clip_update_norm is not None:                    # 定制 (1)：裁剪最终更新
        norm = update.norm()
        if norm > clip_update_norm:
            update *= clip_update_norm / (norm + 1e-12)
    return update
```

裁剪阈值参考：正交化+缩放后的 \( \|update\|_F \approx \sqrt{\min(m,n)} \cdot \max(1, m/n)^{1/2} \)，可取 `1.0 * (min(m,n) ** 0.5) * max(1.0, m/n) ** 0.5` 附近（约等于「不裁」的上界，只在异常时触发）。

**验证 1（插错位置无效）**：把裁剪代码搬到 `zeropower_via_newtonschulz5` 调用**之前**（B 点），设一个很小的阈值（如 1e-3），比较输出与不裁剪版本——应**逐位一致**（尺度不变性，差异只在 1e-7 与 bfloat16 舍入层面）。

**验证 2（极端梯度稳定性）**：构造 `y = W_fixed @ x` 的合成任务，在第 50 步突然把 batch 梯度乘 1e4（模拟 loss spike），分别用无裁剪 `SingleDeviceMuon` 与带裁剪的定制版各跑 100 步，打印每步 loss 与 \( \|p_{t+1} - p_t\|_F \)。

**需要观察的现象**：两种实现的第 50 步参数位移都**不变**（更新范数只由形状决定，正交化天然钳制步长）；loss 曲线都不会爆。只有当梯度本身出现 `Inf/NaN` 时无裁剪版才会崩，那时需要把 `nan_to_num`/入口裁剪加在 A 点。

**预期结果**：Muon 对梯度尖峰的鲁棒性来自正交化本身，事后 Frobenius 裁剪在正常范围内几乎不触发；它的价值是提供「步长绝对上限」的保险丝语义。具体曲线**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：把 `update *= max(1, m/n)**0.5` 改成 `update *= 2`，与直接把该组 lr 乘 2 有何区别？

**答案**：数学上完全等价（施加时 `p.add_(update, alpha=-lr)` 对常数因子可结合），工程上后者更干净——改 lr 保留在超参层、进 `state_dict` 与调度器可见；改魔法常数则把量纲改动藏进算法内部，破坏「lr 以谱范数为单位」的可读契约。

**练习 2**：想在 Muon 上实现「梯度累积 N 步再更新」（不依赖 PyTorch 的 `backward` 累积），新逻辑应挂在落点地图的哪一格？

**答案**：A 点之前（或替换 L35 的 EMA 输入）：把 `p.grad` 先除以 N 或跨步缓存累积，再喂给 `momentum.lerp_`。因为它要改的是「进入动量的梯度」，属于状态输入侧；放在 C/D 只能缩放单步更新，无法实现累积语义。

**练习 3**：定制版在 `MuonWithAuxAdam` 的 adam 分支上加裁剪，需要同样的「落点」分析吗？

**答案**：需要但结论不同。`adam_update`（[muon.py:130-L135](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L130-L135)）没有尺度不变性——更新是 \( \hat{m}/\sqrt{\hat{v}} \)，梯度幅度同样被逐坐标归一化，但在动量上裁剪（B 点等价物）会影响 `exp_avg`/`exp_avg_sq` 两个状态，语义更接近经典梯度裁剪；且各 rank 梯度一致 ⇒ 裁剪分支确定性 ⇒ 不破坏零通信一致性。

## 5. 综合实践：`my_muon.py` 双定制 + 等效 lr 实测

这是本讲的收官任务，把 4.1-4.3 的所有部件组装成一次完整的二次开发迭代。

**任务**：复制 `muon.py` 为 `my_muon.py`（放在你的练习目录），实现并验证两个定制：

1. **梯度裁剪**（4.3.4 的 `my_muon_update`）：挂在正交化与缩放**之后**（D 点），阈值随形状自适应。
2. **尺度因子可关断**（4.2）：`scale_adjust=True/False` 开关，经构造函数 `defaults` 暴露为逐组超参。

宿主类选 `SingleDeviceMuonWithAuxAdam` 的定制版（记作 `MyMuonWithAuxAdam`），这样能在一个优化器里同时验证 muon 组与 adam 组。注意构造函数对参数组有严格的键集合断言（[muon.py:240-L240](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L240-L240)），加新键时要同步修改断言集合，或改用 `defaults` 机制。

**实验设计**（MLP：`64 → 256 → 256 → 10`，全部 `bias=False`，合成数据或 MNIST 风格分类。muon 组取两个隐藏层权重：`Linear(64,256)` 的权重 \( 256 \times 64 \) 是**扩张层**（\( m/n = 4 \)），`Linear(256,256)` 是方阵；输出头 \( 10 \times 256 \) 按惯例归 adam 组）：

| 实验 | 操控变量 | 预期（依 4.2 表） |
|---|---|---|
| E1 基线 | 原版 `SingleDeviceMuonWithAuxAdam`，muon 组 lr 网格 {0.005, 0.01, 0.02, 0.05, 0.1} | 最优 lr 在 0.02 附近 |
| E2 关尺度因子 | `scale_adjust=False`，同 lr 网格 | 方阵层不受影响、扩张层更新缩小 \( \sqrt{64/256}=0.5 \) 倍，全局等效最优 lr 上移，介于 1 与 2（即 \( \sqrt{m/n} \)）倍之间 |
| E3 裁剪 | E1 最优 lr + 极端梯度注入（每 40 步乘 1e4） | 开/关裁剪 loss 曲线几乎一致（正交化天然钳制） |
| E4 sanity | 全方阵模型（如 `128→128→128→10` 的 muon 组全方阵）开/关尺度因子 | 两条曲线应重合；不重合即实现有 Bug |

**交付物**：`my_muon.py`、实验脚本、一张结论表（每个定制：改动落点、是否触碰 lr 语义、实验证据、结论一句话）。

**预期结果**：E1/E2 的等效 lr 比值与 4.2 表的 \( \sqrt{m/n} \) 预测方向一致；E3 展示 Muon 固有的尖峰鲁棒性；E4 全等。所有数值**待本地验证**——这正是二次开发的最后一步：写最小实验，让预测对质证据。

## 6. 本讲小结

- `muon_update` 的 `beta`/`nesterov`/`ns_steps` 是**安全扩展点**：只动方向与带宽、不碰更新幅度，改完无需重调 lr；其中后两个在四个调用点靠默认值硬编码，暴露它们要走「构造函数 `defaults` 加键 → 调用点透传」的打洞路径。
- 尺度因子 `max(1, m/n)**0.5` 把更新 RMS 统一到 \( 1/\sqrt{\text{fan-in}} \)，是 muP 式宽度无关性与「lr 以谱范数为单位」承诺的源码载体；关掉后扩张层更新缩小 \( \sqrt{n/m} \)，等效 lr 需放大 \( \sqrt{m/n} \)，方阵层完全不受影响。
- 「改动落点地图」给出三条判据：缩放类改动放在正交化之前会被尺度不变性吞掉（无效）；放在 A 点会污染动量状态；放在 C/D 才能影响步长但等效于改 lr。梯度裁剪的正确落点是正交化之后。
- Muon 对极端梯度的鲁棒性由正交化内建（更新范数只由形状决定），事后裁剪是「步长保险丝」而非稳定性的来源；`Inf/NaN` 防护必须前移到 A 点入口。
- 定制进分布式版本的前提是保持确定性：所有 rank 输入一致 ⇒ 分支走向一致 ⇒ `all_gather` 一致性闭环不破。

## 7. 下一步学习建议

本讲是项目学习手册的最后一篇，Muon 仓库本身已被读尽。三个方向继续深入：

1. **读真实用法**：README 链接的 [NanoGPT speedrun 用例](https://github.com/KellerJordan/modded-nanogpt/blob/master/records/track_1_short/2025-05-25_MuonWithAuxAdamExample/b01550f9-03d8-4a9c-86fe-4ab434f1c5e0.txt#L470) 与 [CIFAR-10 airbench](https://github.com/KellerJordan/cifar10-airbench)，观察工业级脚本如何划分参数组、调两组 lr、叠加调度器——那是 u3-l4 与本讲定制技能的实战考场。
2. **读理论延伸**：Jeremy Bernstein 的推导博文（`deriving-muon`）与 u4-l1 的 NS 动力系统分析衔接，可进一步研究系数优化（如用更少步数达到同等带宽）——那是 `ns_steps`/系数联合调优的理论基础。
3. **做一个自己的变体并压测**：以本讲 `my_muon.py` 为起点，尝试 u4-l2 的三优化器基准上加入你的变体（多种子、控变量），如果能稳定超越默认配置，你就完成了从「读懂 290 行」到「扩展 290 行」的闭环。
