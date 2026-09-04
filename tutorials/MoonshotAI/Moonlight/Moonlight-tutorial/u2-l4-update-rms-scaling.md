# 更新 RMS 一致化：adjust_lr_for_muon 的缩放设计

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚「更新 RMS」的定义：一步优化中参数改动量的均方根，以及它为什么是衡量训练稳定性的关键指标。
2. 独立推导 `adjust_lr_for_muon` 中 \(0.2\sqrt{\max(A,B)}\) 缩放系数的来源：正交化后更新的 RMS 恰为 \(1/\sqrt{\max(A,B)}\)，两者相乘精确抵消，得到与形状无关的更新 RMS \(0.2\,\mathrm{lr}\)。
3. 解释「形状自适应学习率」在源码中的落点：`adjusted_lr` 用于施加更新、原始 `lr` 用于权重衰减的不对称设计。
4. 把本讲的内容与上一讲的权重衰减联系起来，说明论文宣称的两大规模化技术（权重衰减 + 按参数形状调整更新尺度）分别对应哪几行代码，以及它们如何共同支撑「免调参、开箱即用」的大规模训练。

本讲是进阶单元的第四讲，承接 u2-l3 留下的悬念：**为什么更新用 `adjusted_lr`、衰减却用原始 `lr`？**

## 2. 前置知识

本讲需要以下基础概念，均已在前几讲出现，这里用通俗语言再巩固一遍：

- **RMS（Root Mean Square，均方根）**：把一个矩阵（或向量）所有元素平方、求平均、再开方。它衡量的是「这个矩阵里元素的整体典型幅度」，比最大值更稳健（不被个别大元素主导）：

  \[ \mathrm{RMS}(X) = \sqrt{\frac{1}{n}\sum_i x_i^2} \]

- **更新（update）**：优化器一步之内对参数施加的全部改动 \(\Delta W\)。在 Muon 的 Muon 分支里，\(\Delta W = -\mathrm{adjusted\_lr}\cdot u - \mathrm{lr}\cdot \mathrm{wd}\cdot W\)（先正交化更新、再解耦衰减，见 u2-l3）。

- **Frobenius 范数**：矩阵所有元素平方和的平方根，\(\|X\|_F = \sqrt{\sum_{i,j}x_{ij}^2}\)。它与 RMS 的关系是 \(\mathrm{RMS}(X) = \|X\|_F / \sqrt{AB}\)（\(A\times B\) 矩阵共 \(AB\) 个元素）。

- **正交矩阵与 SVD（复习 u2-l2）**：任何矩阵 \(G = USV^\top\)。Newton-Schulz 迭代的输出近似 \(US'V^\top\)，即保留奇异向量、把奇异值拉平到 1 附近（源码 docstring 说约在 0.5~1.5 区间）。

- **学习率调度（复习 u1-l3）**：训练全程中每一步生效的学习率 \(\mathrm{lr}_t\) 由 cosine warmup 调度器改写 `param_groups` 中的 `lr` 得到。本讲推导中的 \(\mathrm{lr}\) 都指「当前这一步生效的学习率」。

- **torch.optim.Optimizer 的 param_groups 机制**：优化器把超参存在分组字典里，`step()` 每次从 `group["lr"]` 现取——这就是调度器能动态改学习率的原因。

一个值得先建立的直觉：**AdamW 的更新 RMS 天生与形状无关**。AdamW 的更新是逐元素的 \(m/\sqrt{v}\)，无论参数是 \(1024\times1024\) 还是 \(4864\times1024\)，每个元素的分布性质相同，更新 RMS 都近似同一个常数乘以 \(\mathrm{lr}\)。而 Muon 的更新是一个正交化的**矩阵**，它的 RMS 会随矩阵形状系统性变化——这就是本讲要解决的问题。

## 3. 本讲源码地图

| 文件 | 本讲关注的行区间 | 作用 |
|---|---|---|
| `examples/toy_train.py` | L142-L148 | `adjust_lr_for_muon`：按形状缩放学习率，本讲主角 |
| `examples/toy_train.py` | L194-L203 | 调用点：正交化 → 取 `adjusted_lr` → 衰减 → 施加更新 |
| `examples/toy_train.py` | L48-L76 | `zeropower_via_newtonschulz5`：产出近似正交矩阵 \(u\)（u2-l2 已精读，本讲只复用其输出性质） |
| `examples/toy_train.py` | L79-L104 | `Muon` 类 docstring：注意 L94 的说法与本讲缩放的关系 |
| `examples/toy_train.py` | L287-L311 | `get_optimizer`：决定哪些参数走 Muon 分支（u2-l1 已讲） |
| `README.md` | L15, L27 | 论文摘要与 Key Ingredients：两大规模化技术的官方表述 |

整个仓库的源码就这一个 Python 文件，本讲聚焦其中短短 7 行的 `adjust_lr_for_muon`——但它是论文两大核心贡献之一在代码中的全部落点。

## 4. 核心概念与源码讲解

### 4.1 更新 RMS 概念

#### 4.1.1 概念说明

「更新 RMS」指一步优化中参数改动的均方根：

\[ \mathrm{RMS}(\Delta W) = \sqrt{\frac{1}{AB}\sum_{i=1}^{A}\sum_{j=1}^{B}\left(\Delta W_{ij}\right)^2} \]

为什么训练的稳定性要看这个量，而不是只看学习率？

- 学习率本身不直接等于「参数动了多少」。同一个 \(\mathrm{lr}\)，乘上不同性质的方向矩阵，参数的实际改动幅度可以差好几倍。
- 神经网络是层层串联的：某一层参数每步被改动 RMS 过大，会持续「震荡」甚至发散；过小则学得比别的层慢，等效于拖后腿。
- 更关键的是**相对尺度**问题。模型变宽（hidden_size 从 1024 涨到几千），各层矩阵形状随之变化。如果不同形状的参数更新 RMS 天然差好几倍，那么同一组 \(\mathrm{lr}\) 超参就无法同时适配所有层——换一个模型规模就得重新调参。这正是论文摘要里「carefully adjusting the per-parameter update scale」要消除的痛点（见 [README.md:L15](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L15)）。

README 的 Key Ingredients 一节把这件事表述为：**通过逐参数的更新尺度调整，让矩阵参数与非矩阵参数保持一致的更新 RMS**，并明确说这一调整「显著增强了训练稳定性」（见 [README.md:L27](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L27)）。

#### 4.1.2 核心流程

要理解「Muon 的更新 RMS 为什么随形状变化」，分三步：

1. Newton-Schulz 输出 \(u \approx US'V^\top\)，奇异值 \(\sigma'_i \approx 1\)，共 \(r = \min(A,B)\) 个。
2. Frobenius 范数平方等于奇异值平方和：\(\|u\|_F^2 = \sum_{i=1}^{r}(\sigma'_i)^2 \approx r = \min(A,B)\)。
3. 于是 RMS 要摊到全部 \(AB\) 个元素上：

   \[ \mathrm{RMS}(u) \approx \sqrt{\frac{\min(A,B)}{AB}} = \frac{1}{\sqrt{\max(A,B)}} \]

   （最后一步用到了 \(\min(A,B)\cdot\max(A,B) = AB\)。）

也就是说：**矩阵越「大」（长边越长），正交化输出的元素平均幅度越小**。若不做任何处理直接乘 \(\mathrm{lr}\)，不同形状参数的更新 RMS 就按 \(1/\sqrt{\max(A,B)}\) 各自漂移。

#### 4.1.3 源码精读

先看本讲要解释的目标——`step()` 中施加更新的那一行。`u` 是正交化输出，`adjusted_lr` 是按形状缩放后的学习率：

```python
# apply update
p.data.add_(u, alpha=-adjusted_lr)
```

这一行在 [examples/toy_train.py:L202-L203](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L202-L203)。它的字面意思：参数减去 \(\mathrm{adjusted\_lr}\cdot u\)。如果 `adjusted_lr` 就等于原始 `lr`（原版 Muon 的做法），那么更新 RMS 就是 \(\mathrm{lr}/\sqrt{\max(A,B)}\)——随形状漂移。

再看 `u` 的来源，确认它「奇异值拉平到 1 附近」的性质（u2-l2 精读过，这里只取结论）：

```python
u = zeropower_via_newtonschulz5(g, steps=group["ns_steps"])
```

见 [examples/toy_train.py:L194](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L194)。函数 docstring 明说输出不是精确的 \(UV^\top\)，而是 \(US'V^\top\)，\(S'\) 对角元约在 0.5~1.5 区间（[examples/toy_train.py:L50-L58](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L50-L58)）。所以 4.1.2 的推导是「近似但很好」的：奇异值不精确为 1，只是在 1 附近的带内。

#### 4.1.4 代码实践

**实践目标**：亲手验证「精确正交矩阵的 RMS = \(1/\sqrt{\max(A,B)}\)」这一条基石。

**操作步骤**：在仓库根目录新建脚本（示例代码，可存为 `check_orth_rms.py`）：

```python
# 示例代码
import torch

torch.manual_seed(0)
for A, B in [(64, 64), (128, 32), (32, 128), (256, 48)]:
    # 用 QR 分解造一个精确的正交矩阵（前 min(A,B) 列标准正交）
    Q = torch.linalg.qr(torch.randn(A, B)).Q
    rms = Q.pow(2).mean().sqrt()
    print(f"{A}x{B}: RMS={rms.item():.5f}  预测 1/sqrt(max)={1/torch.sqrt(torch.tensor(max(A,B), dtype=torch.float32)).item():.5f}")
```

**需要观察的现象**：打印出的 RMS 与 \(1/\sqrt{\max(A,B)}\) 到小数点后四位一致。

**预期结果**：例如 \(128\times32\) 的正交矩阵，\(\mathrm{RMS} = 1/\sqrt{128} \approx 0.08839\)；\(32\times128\) 的结果完全相同（只依赖长边）。

**待本地验证**：具体打印数值请自行运行确认。

#### 4.1.5 小练习与答案

**练习 1**：一个 \(512\times2048\) 的精确正交矩阵，其 RMS 是多少？

**答案**：\(\max(A,B)=2048\)，\(\mathrm{RMS} = 1/\sqrt{2048} = 1/45.25 \approx 0.0221\)。注意它与短边 512 无关。

**练习 2**：为什么衡量「参数这步动了多少」用 RMS，而不是谱范数（最大奇异值）？

**答案**：谱范数只刻画最敏感的一个方向被改了多少，忽略了其余方向；RMS 对全部元素平均，反映参数整体的典型改动量。AdamW 分支的更新是逐元素同分布的，用 RMS 才能与它对齐比较。对正交矩阵来说谱范数恒为 1、完全不随形状变，反而看不出问题所在。

**练习 3**：embedding 与 lm_head 参数不走 Muon 分支（u2-l1），从「更新 RMS」角度看，若让 \(151936\times1024\) 的嵌入矩阵也走正交化，它的未缩放更新 RMS 会是什么量级？

**答案**：\(1/\sqrt{151936} \approx 1/389.8 \approx 0.00257\)，比 \(1024\times1024\) 矩阵的 \(1/32 = 0.03125\) 小约 12 倍——与其它层完全不可比，且这么大的矩阵做 Newton-Schulz 开销也高。这从另一个角度印证了 u2-l1 的分组判据。

### 4.2 0.2·√max(A,B) 缩放公式

#### 4.2.1 概念说明

4.1 节说明了问题：未缩放的 Muon 更新 RMS \(= \mathrm{lr}/\sqrt{\max(A,B)}\)，随形状漂移。解决办法朴素得漂亮——**既然 RMS 被 \(1/\sqrt{\max(A,B)}\) 压低，就把学习率放大 \(\sqrt{\max(A,B)}\) 补回来**：

\[ \mathrm{adjusted\_lr} = \mathrm{lr}\cdot 0.2\sqrt{\max(A,B)} \]

代入 4.1.2 的结果：

\[ \mathrm{RMS}(\Delta W) = \mathrm{adjusted\_lr}\cdot\mathrm{RMS}(u) = \mathrm{lr}\cdot 0.2\sqrt{\max(A,B)}\cdot\frac{1}{\sqrt{\max(A,B)}} = 0.2\,\mathrm{lr} \]

\(\sqrt{\max(A,B)}\) 被**精确抵消**（对精确正交输出而言），更新 RCS 收敛为一个只依赖全局学习率的常数，与参数形状彻底无关。

至于系数为什么是 0.2 而不是 1.0：README 说目标是让「矩阵参数与非矩阵参数」的更新 RMS **一致**（[README.md:L27](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L27)）。非矩阵参数走的是内嵌 AdamW 分支，其更新 \(m/\sqrt{v}\) 逐元素归一，但 RMS 经验上明显低于 1（梯度的随机性使 \(m\) 与 \(\sqrt{v}\) 很少同时达到峰值）。0.2 是论文中把 Muon 的更新 RMS 对齐到 AdamW 分支实际水平所用的经验常数——代码注释也写明这一缩放「as describted in the paper」（原文拼写如此），具体设定依据可对照 `Moonlight.pdf` 相应章节阅读，细节以论文为准（待确认：论文中该常数的精确推导）。

#### 4.2.2 核心流程

用玩具模型（hidden_size=1024，intermediate_size=4864，见 [examples/toy_train.py:L257-L280](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L257-L280)）的真实层形状列一张表，看缩放前后各层更新 RMS 的对比（以 \(\mathrm{lr}=1\) 归一）：

| 层 | 形状 \(A\times B\) | \(\max(A,B)\) | 未缩放 RMS | adjusted_ratio \(0.2\sqrt{\max}\) | 缩放后 RMS |
|---|---|---|---|---|---|
| q/k/v/o_proj | \(1024\times1024\) | 1024 | 1/32 = 0.03125 | 6.40 | **0.2** |
| gate/up_proj | \(4864\times1024\) | 4864 | ≈0.01434 | ≈13.95 | **0.2** |
| down_proj | \(1024\times4864\) | 4864 | ≈0.01434 | ≈13.95 | **0.2** |

要点：

- 不缩放时，MLP 的三个矩阵每步被改动的幅度只有注意力投影的 \(\sqrt{1024/4864}\approx 1/2.18\)——同一种学习率，两种「实际步长」。
- 缩放后全部拉齐到 \(0.2\,\mathrm{lr}\)，长边信息被完全吸收进 `adjusted_ratio`。
- 模型规模再变（比如 16B 模型 hidden 数千、专家矩阵形状各异），公式自动适配，`lr` 无需按形状重调——这就是「免调参规模化」的数学内核。

#### 4.2.3 源码精读

本讲主角，函数本体只有 7 行：

```python
def adjust_lr_for_muon(self, lr, param_shape):
    A, B = param_shape[:2]
    # We adjust the learning rate and weight decay based on the size of the parameter matrix
    # as describted in the paper
    adjusted_ratio = 0.2 * math.sqrt(max(A, B))
    adjusted_lr = lr * adjusted_ratio
    return adjusted_lr
```

见 [examples/toy_train.py:L142-L148](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L142-L148)。逐行拆解：

- `A, B = param_shape[:2]`：取前两维作为行数、列数。`[:2]` 是防御性写法——由于构造函数里 `assert p.ndim == 2`（[examples/toy_train.py:L136](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L136)），传入的形状实际恒为二维（u2-l3 已说明 `step` 里的高维 `view` 展平分支是不可达代码）。
- `adjusted_ratio = 0.2 * math.sqrt(max(A, B))`：只取**长边**。这不是随意选择——由 4.1.2 的推导，正交矩阵的 RMS 恰好是 \(1/\sqrt{\text{长边}}\)，所以只有用 \(\sqrt{\text{长边}}\) 才能精确抵消；用 \(\sqrt{\min}\) 或 \(\sqrt{AB}\) 都会留下残余的形状依赖。
- 注释里说「adjust the learning rate **and weight decay**」，但函数只返回了 `adjusted_lr`，权重衰减并未随之调整——这个不对称在 4.3.3 解释。
- 值得注意的代码细节：函数体完全没有用到 `self`，本质上是个纯函数（可以写成 `@staticmethod`）。这让我们的离线数值实验可以直接借用它。

#### 4.2.4 代码实践

**实践目标**：数值验证「缩放后各形状的等效更新 RMS 都约等于 \(0.2\,\mathrm{lr}\)，而不缩放时差异巨大」。

**操作步骤**：在仓库根目录新建 `verify_update_rms.py`（示例代码）：

```python
# 示例代码：python3 verify_update_rms.py
import torch
from examples.toy_train import Muon, zeropower_via_newtonschulz5

lr = 1e-3
shapes = [
    ("q/k/v/o_proj (hidden=1024)", (1024, 1024)),
    ("gate/up_proj   (hidden=1024)", (4864, 1024)),
    ("down_proj      (hidden=1024)", (1024, 4864)),
    ("q/k/v/o_proj (hidden=896)",  (896, 896)),
    ("gate/up_proj   (hidden=896)", (4864, 896)),
]
torch.manual_seed(0)
print(f"{'层':<30}{'RMS(u)':>10}{'adjusted_lr':>14}{'缩放后RMS':>14}{'未缩放RMS':>14}")
for name, shape in shapes:
    g = torch.randn(*shape)                       # 模拟正交化前的动量矩阵
    u = zeropower_via_newtonschulz5(g, steps=5).float()
    rms_u = u.pow(2).mean().sqrt()
    # adjust_lr_for_muon 不用 self，可用未绑定方式直接调用
    adjusted_lr = Muon.adjust_lr_for_muon(None, lr, shape)
    print(f"{name:<30}{rms_u:>10.5f}{adjusted_lr:>14.5f}"
          f"{adjusted_lr*rms_u:>14.6f}{lr*rms_u:>14.6f}")

print(f"\n参考值: 0.2 * lr = {0.2*lr:.6f}")
```

说明：`from examples.toy_train import ...` 只会导入定义，不会触发训练（`__main__` 守护）；`Muon.adjust_lr_for_muon(None, lr, shape)` 借用了「函数不用 self」这一点。

**需要观察的现象**：「缩放后RMS」一列所有行都聚在 \(0.2\times10^{-3} = 2\times10^{-4}\) 附近（因 NS 奇异值在 1 附近的带内波动，允许 ±20% 左右的散布）；「未缩放RMS」一列则在 \(10^{-5}\sim 3\times10^{-5}\) 之间按形状分层，最大相差约 2.2 倍。

**预期结果**：以理论值（精确正交）计，\(1024\times1024\) 的 RMS(u)=0.03125、adjusted_lr=0.0064，乘积 0.0002；\(4864\times1024\) 的 RMS(u)≈0.01434、adjusted_lr≈0.01395，乘积同为 0.0002。实际运行因 NS 近似会略有偏差。

**待本地验证**：上表具体数值请运行脚本确认；若 CPU 上 `bfloat16` 或 `torch.compile` 报错，可改在有 GPU 的环境运行，或在自己的副本里去掉 `@torch.compile` 装饰器。

#### 4.2.5 小练习与答案

**练习 1**：若把常数 0.2 改成 1.0（其他不变），Muon 分支的更新 RMS 变成多少？会带来什么问题？

**答案**：变成 \(1.0\,\mathrm{lr}\)，仍是形状无关的，但相对 AdamW 分支（以及相对论文调好的 \(\mathrm{lr}\) 量级）放大了 5 倍，等于变相把学习率调大 5 倍，很可能需要重新扫超参——0.2 的意义就是与 AdamW 一侧对齐，让两条分支在同一 \(\mathrm{lr}\) 下协同工作。

**练习 2**：推导 \(A\le B\) 与 \(A>B\) 两种情形下 \(\min(A,B)/(AB) = 1/\max(A,B)\)。

**答案**：若 \(A\le B\)：\(\min=A\)，\(AB/\min = B = \max\)，故 \(\min/(AB)=1/\max\)。若 \(A>B\)：\(\min=B\)，\(AB/\min=A=\max\)，同样成立。两种情形统一为 \(1/\max(A,B)\)。

**练习 3**：为什么 `adjusted_ratio` 用 \(\sqrt{\max(A,B)}\) 而不是 \(\sqrt{A\cdot B}\)（参数元素个数的平方根）？

**答案**：因为要抵消的对象是 \(\mathrm{RMS}(u)=1/\sqrt{\max(A,B)}\)，它来自「奇异值平方和 \(\approx\min(A,B)\) 摊到 \(AB\) 个元素」。若用 \(\sqrt{AB}\)，缩放后 RMS \(=\sqrt{AB}/\sqrt{\max}=\sqrt{\min(A,B)}\)，仍随形状（短边）变化，没有达成一致化。

### 4.3 形状自适应学习率

#### 4.3.1 概念说明

「形状自适应学习率」指：**名义上优化器只有一个 `lr` 超参，但每个参数实际使用的步长由其形状即时推导**。它与常见的「分组学习率」（给不同层配不同超参）有本质区别：

| | 手工分组学习率 | adjust_lr_for_muon |
|---|---|---|
| 来源 | 人工设定超参 | 由形状公式自动推导 |
| 需要调参 | 换模型要重调 | 免调，公式自适配 |
| 目的 | 表达层间重要性先验 | 归一化更新 RMS，保稳定 |

这个设计回答了一个规模化痛点：模型从 0.5B 涨到 16B，矩阵形状全变，若实际步长隐含地依赖形状，旧超参就失效。把形状依赖显式写进公式并令其抵消，一套 `lr` 就能跨规模复用。

#### 4.3.2 核心流程

Muon 分支中与 `adjusted_lr` 相关的一段流水线（承接 u2-l3 的五步）：

```text
for p in muon 参数:
    g = p.grad
    动量累积 / Nesterov 前瞻            → 得到送入正交化的 g
    u = Newton-Schulz(g)                → 近似正交矩阵
    adjusted_lr = 0.2*sqrt(max(A,B)) * lr   ← 本讲：按 p.shape 缩放
    p.data *= (1 - lr*wd)               ← 注意：衰减用的是原始 lr
    p.data += -adjusted_lr * u          ← 更新用的是缩放后 lr
```

两个学习率的分工是本讲最想让你带走的设计决策：

- **更新走 `adjusted_lr`**：目的是让「这一步把参数改多大」在所有矩阵参数上一致（\(0.2\,\mathrm{lr}\)）。
- **衰减走原始 `lr`**：权重衰减是**相对收缩**（每步把权重整体乘 \((1-\mathrm{lr}\cdot\mathrm{wd})\)），它控制的是「权重的稳态规模」。若衰减也用 `adjusted_lr`，大矩阵（如 4864×1024）会以 2.18 倍的速度衰减，不同形状参数的稳态权重规模就被形状扭曲了。保持衰减统一用原始 `lr`，才能让「更新尺度一致化」建立在干净的基准上。

#### 4.3.3 源码精读

调用点上下文（正交化之后、写回参数之前）：

```python
# scale update
adjusted_lr = self.adjust_lr_for_muon(lr, p.shape)

# apply weight decay
p.data.mul_(1 - lr * wd)

# apply update
p.data.add_(u, alpha=-adjusted_lr)
```

见 [examples/toy_train.py:L196-L203](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L196-L203)。三个动作一次完成：

- L197：以 `p.shape` 为入参现场计算 `adjusted_lr`——每个参数、每一步都重算（形状不变，值其实恒定，但不存状态，零成本且与调度器改写的 `group["lr"]` 天然同步）。
- L200：衰减用原始 `lr`（来自 L170 的 `lr = group["lr"]`，见 [examples/toy_train.py:L170](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L170)）。
- L203：更新用 `adjusted_lr`。u2-l3 结尾悬置的「不对称」至此解释完毕：不是笔误，是职责分离。

再对照类的 docstring：「lr: The learning rate. The updates will have spectral norm of `lr`. (0.02 is a good default)」见 [examples/toy_train.py:L92-L97](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L92-L97)。这句描述沿袭自原版 KellerJordan/Muon（[examples/toy_train.py:L46-L47](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L46-L47) 标注了来源）：在原版中更新的谱范数恰为 `lr`。在 Moonlight 改造版里，实际的谱范数变为 \(\mathrm{adjusted\_lr}\cdot\sigma'_{\max}\approx 0.2\sqrt{\max(A,B)}\,\mathrm{lr}\)，元素级 RMS 则是 \(0.2\,\mathrm{lr}\)——docstring 未随改造更新，读源码时要留意这类「注释滞后」。

还要注意哪些参数根本进不了这段代码：`get_optimizer` 的分组判据（`p.ndim >= 2` 且名称不含 `embed_tokens`/`lm_head`，[examples/toy_train.py:L293-L304](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L293-L304)）保证走进 `adjust_lr_for_muon` 的全是二维权重矩阵；norm 向量与嵌入走 AdamW 分支，由逐元素归一天然获得形状无关的更新 RMS，无需此缩放。

#### 4.3.4 代码实践

**实践目标**：把玩具模型真实的参数形状列成表，核对每层的 `adjusted_ratio`，直观感受「同一个 lr、不同的实际步长」。

**操作步骤**：新建脚本（示例代码）：

```python
# 示例代码：python3 show_adjusted_ratio.py
from examples.toy_train import Muon, get_model_and_dataloader

model, _ = get_model_and_dataloader("qwen", "openwebtext-100k", hidden_size=896)
seen = set()
for name, p in model.named_parameters():
    if p.ndim == 2 and "embed_tokens" not in name and "lm_head" not in name:
        key = tuple(p.shape)
        if key in seen:
            continue
        seen.add(key)
        ratio = Muon.adjust_lr_for_muon(1.0, p.shape)  # lr=1，直接看缩放倍率
        print(f"形状 {list(p.shape)}: adjusted_ratio = {ratio:.3f}  (示例参数 {name})")
```

**需要观察的现象**：README 训练命令示例用的正是 `--hidden_size 896`（[README.md:L131-L137](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L131-L137)），此时 \(896\times896\) 的注意力投影 adjusted_ratio≈5.99，\(4864\times896\) 的 MLP 矩阵 adjusted_ratio≈13.95。

**预期结果**：打印出的形状集合为 `{[896,896], [4864,896], [896,4864]}` 三类（对应 q/k/v/o、gate/up、down），倍率分别约 5.99 与 13.95；两类矩阵的实际步长相差 \(13.95/5.99\approx2.33\) 倍。

**待本地验证**：脚本会先下载数据集与 tokenizer（首次较慢）；如只想看形状，也可以直接用 `Qwen2Config` 手工构造模型，跳过数据加载。

#### 4.3.5 小练习与答案

**练习 1**：hidden_size=896 时，q_proj（896×896）与 gate_proj（4864×896）的未缩放更新 RMS 之比是多少？缩放后之比是多少？

**答案**：未缩放之比 \(=\sqrt{4864/896}\approx 2.33\)（MLP 更新更小）；缩放后两者都是 \(0.2\,\mathrm{lr}\)，之比为 1。

**练习 2**：如果权重衰减误用了 `adjusted_lr`（即 `p.data.mul_(1 - adjusted_lr * wd)`），对 4864×896 的矩阵意味着什么？

**答案**：它的每步相对衰减率是注意力层的 \(\sqrt{4864/896}\approx2.33\) 倍。长期训练中 MLP 权重会被压得更小、稳态规模更失衡，恰与「更新 RMS 一致化」的目标背道而驰。当前代码在 [examples/toy_train.py:L200](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L200) 用原始 `lr`，避免了这一点。

**练习 3**：`adjust_lr_for_muon` 每一步都对每个参数重算一遍，为什么不用担心开销或与学习率调度器冲突？

**答案**：计算只是两次标量运算，开销可忽略；且它乘的是 `group["lr"]` 的当前值（L170 现取），调度器每步改写 `group["lr"]` 后，`adjusted_lr` 自动跟随——缩放是「乘性修正」，与调度完全正交。

### 4.4 论文两大贡献的联系

#### 4.4.1 概念说明

论文摘要（[README.md:L15](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L15)）指出让 Muon 规模化的两项关键技术：

1. **加入权重衰减**（adding weight decay）；
2. **仔细调整逐参数的更新尺度**（carefully adjusting the per-parameter update scale）。

并明确说这两者让 Muon 在大规模训练上**开箱即用、无需调参**。scaling law 实验的结论——Muon 达到 AdamW 同等性能只需约 52% 训练 FLOPs（[README.md:L31](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L31)）——就建立在这两项地基上。

为什么这两件事必须**成对**出现？可以做一个稳态估算（以下为本讲基于代码的分析性推导，帮助建立直觉；论文完整论证请读 `Moonlight.pdf`）：

- 每一步，权重被更新项「注入」RMS 约 \(0.2\,\mathrm{lr}\) 的随机改动（本讲的主 题）；
- 同时被衰减项按 \((1-\mathrm{lr}\cdot\mathrm{wd})\) 相对收缩（u2-l3 的主题）。
- 长期看两者平衡，权重的稳态 RMS 量级约为：

  \[ \mathrm{RMS}(W_\infty)\cdot\mathrm{lr}\cdot\mathrm{wd} \approx 0.2\,\mathrm{lr} \quad\Longrightarrow\quad \mathrm{RMS}(W_\infty)\approx \frac{0.2}{\mathrm{wd}} \]

  取代码默认 \(\mathrm{wd}=0.1\)（[examples/toy_train.py:L109](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L109)），稳态权重 RMS ≈ 2——一个**既不依赖形状、也不依赖学习率**的常数。

换言之：更新尺度缩放把「每步注入多快」钉死为 \(0.2\,\mathrm{lr}\)，权重衰减把「盘子里最终装多少」钉死为 \(0.2/\mathrm{wd}\)。前者管稳定性，后者管权重的长期规模与健康度（衰减持续剪除过大的权重、让有效秩不塌缩）。只有更新没有衰减，随机注入会无限累积；只有衰减没有一致化，不同形状参数的注入速度不均。两者合力，才换来「换个模型规模也不用重调参」。

#### 4.4.2 核心流程

把两大贡献映射到代码（都在 `step()` 的 Muon 分支里，相隔三行）：

```text
论文贡献                          代码落点
─────────────────────────────  ─────────────────────────────────────────
(1) 权重衰减                     p.data.mul_(1 - lr * wd)        # L200
(2) 按参数形状调整更新尺度         adjusted_lr = self.adjust_lr_for_muon(lr, p.shape)  # L197
                                 └─ 0.2*sqrt(max(A,B))，定义在 L142-L148
                                p.data.add_(u, alpha=-adjusted_lr)  # L203
─────────────────────────────  ─────────────────────────────────────────
README 摘要 L15 / 关键成分 L27     ← 官方表述
```

注意叙述次序：代码里先算 `adjusted_lr`（L197）再衰减（L200）再更新（L203），但**数学上**衰减与更新是两个独立的作用项（u2-l3 讲过的「解耦」），先后次序不影响结果——先乘 \((1-\mathrm{lr}\cdot\mathrm{wd})\) 再减更新，与反过来只差一个 \(\mathrm{adjusted\_lr}\cdot\mathrm{lr}\cdot\mathrm{wd}\) 的二阶小量。

#### 4.4.3 源码精读

两大贡献在源码中的完整上下文，就是下面这一段（u2-l3 精读过流水线，本讲给它补上「为什么」）：

```python
u = zeropower_via_newtonschulz5(g, steps=group["ns_steps"])   # 正交化更新

# scale update
adjusted_lr = self.adjust_lr_for_muon(lr, p.shape)            # 贡献 (2)：形状自适应尺度

# apply weight decay
p.data.mul_(1 - lr * wd)                                      # 贡献 (1)：解耦权重衰减

# apply update
p.data.add_(u, alpha=-adjusted_lr)                            # 更新 RMS = 0.2*lr
```

见 [examples/toy_train.py:L194-L203](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L194-L203)。

对照 README 的 Key Ingredients 表述（[README.md:L23-L31](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L23-L31)）：第一条 "Analysis for Effective Scaling of Muon" 同时点名了 weight decay 与 update RMS 一致化——7 行的 `adjust_lr_for_muon` 加上一行 `mul_`，就是这条贡献在本仓库的全部代码形态。第三条 "Scaling Law Validation" 的 52% FLOPs 结论（[README.md:L31](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L31)）将在 u3-l1 的对比实验中定性复现。

顺带回应一个常见疑问：既然缩放让更新 RMS 恒为 \(0.2\,\mathrm{lr}\)，那「Muon 的学习率」到底指什么？答案是：`lr` 仍是唯一的全局旋钮（配合 cosine 调度），缩放只是把「形状」这个本不该由用户管的变量从旋钮里拿掉了。

#### 4.4.4 代码实践

**实践目标**：把 README 的论述与代码逐条对上号，做一次「论文→代码」的映射训练。

**操作步骤**：

1. 重读 [README.md:L15](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L15)（摘要）与 [README.md:L23-L31](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L23-L31)（Key Ingredients）。
2. 在 `examples/toy_train.py` 中找出下列每个论断对应的代码行，并写下行号：
   - 「adding weight decay」；
   - 「parameter-wise update scale adjustments」；
   - 「consistent update RMS across matrix and non-matrix parameters」中「非矩阵参数」那半边是哪段代码在负责。
3. 检验你的映射：`git grep -n "0.2"` 与 `git grep -n "weight_decay\|wd"` 对照。

**需要观察的现象**：前两条都落在 L194-L203 这十行之内；第三条的「非矩阵参数」半边其实是下一讲（u2-l5）的 AdamW 分支（[examples/toy_train.py:L215-L237](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L215-L237)）——「一致」是两条分支共同达成的。

**预期结果**：你能画出一张三行映射表（README 论断 → 代码行 → 作用），这将是阅读任何「论文+代码」仓库的基本功。

**待本地验证**：无运行项，纯阅读任务。

#### 4.4.5 小练习与答案

**练习 1**：用一句话向同事解释「Moonlight 对原版 Muon 做了什么」。

**答案**：给 Muon 补上了权重衰减，并把学习率按参数形状乘上 \(0.2\sqrt{\max(A,B)}\)，使所有矩阵参数的更新 RMS 统一为 \(0.2\,\mathrm{lr}\)、稳态权重规模统一为 \(0.2/\mathrm{wd}\)，从而一套超参可以直接放大到 16B MoE 训练。

**练习 2**：只保留权重衰减、去掉形状缩放（`adjusted_ratio=1`），会发生什么？只保留缩放、去掉衰减呢？

**答案**：去掉缩放：MLP 大矩阵每步实际改动比注意力小约 2.2 倍（随规模增大差异加剧），同一 `lr` 无法适配所有层，换宽度就得重调参，稳定性下降。去掉衰减：每步 \(0.2\,\mathrm{lr}\) 的注入长期净累积，权重范数无约束增长，训练后期易失稳——论文正是通过消融发现衰减对 Muon 规模化不可或缺。两种「半套」都动摇「免调参」的结论。

**练习 3**：稳态论证中 \(\mathrm{RMS}(W_\infty)\approx 0.2/\mathrm{wd}\) 与学习率无关，这是否意味着训练时可以随意放大 `lr`？

**答案**：不能。稳态只约束「长期盘子多大」，不保证「短期不震荡」：`lr` 过大时单步改动相对当前权重过大（尤其热身不足时），损失会先发散，根本走不到稳态。该公式只说明**稳态规模**对 `lr` 不敏感，是理解结构的工具，不是调参许可。

## 5. 综合实践：更新 RMS 审计

设计一个贯穿本讲的小任务——给 Muon 的更新 RMS 做一次「体检审计」，分三步，从纯数值到真实训练：

**任务目标**：用实验证明（或修正）本讲的核心论断：「缩放后，所有 Muon 参数每步更新的 RMS ≈ \(0.2\times\)当前学习率」。

### 第一步：离线形状扫描（必做）

运行 4.2.4 的 `verify_update_rms.py`，把结果整理成表格，包含五列：形状、RMS(u)、adjusted_lr、缩放后 RMS、未缩放 RMS。回答：缩放后各行的极差（最大/最小）是多少？未缩放的极差又是多少？

### 第二步：真实训练插桩（必做）

在**你自己复制的** `toy_train_instrumented.py`（不要改原文件）中，在训练循环里对 Muon 参数测量真实单步改动：

```python
# 示例代码：插在 loss.backward() 之后、optimizer.step() 之前
snapshots = {n: p.detach().clone() for n, p in model.named_parameters()}

optimizer.step()

cur_lr = optimizer.param_groups[0]["lr"]  # 本步实际生效的学习率
for n, p in model.named_parameters():
    if p.ndim == 2 and "embed_tokens" not in n and "lm_head" not in n:
        rms = (p.detach() - snapshots[n]).pow(2).mean().sqrt()
        logger.info(f"{n}: ΔW RMS={rms.item():.6f}  预测 0.2*lr={0.2*cur_lr:.6f}")
```

观察要点：

1. **热身期**：前 100 步学习率从 0 线性爬升（u1-l3），ΔW RMS 应紧贴 \(0.2\times\mathrm{lr}_t\) 同步爬升。
2. **测量残差**：ΔW 里还混有权重衰减项 \(-\mathrm{lr}\cdot\mathrm{wd}\cdot W\)。初始化权重 RMS 约 0.02（`initializer_range=0.02`），该项贡献约 \(10^{-3}\times0.1\times0.02=2\times10^{-6}\)，仅为更新的 1% 左右——请用实测核对这个量级。
3. **层间对比**：q_proj 与 gate_proj 的 ΔW RMS 应几乎相同；这正是 u2-l3 那张「悬置的不对称」的最终答案。

### 第三步：消融对照（选做，需 GPU 更佳）

再复制一份，把 [examples/toy_train.py:L146](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L146) 改为 `adjusted_ratio = 1.0`，用小模型（如 `--hidden_size 512`）各训几百步，对比两者 loss 曲线：预期无缩放版本因 MLP 层有效步长偏小而收敛更慢或更差。

**产出**：一份简短报告，包含第一、二步的表格与曲线截图，以及一句结论：本讲推导的 \(0.2\,\mathrm{lr}\) 一致化在你的机器上是否成立（实测数值与预期分布存在多大偏差，来源是什么——NS 近似？衰减项？bfloat16 精度？）。全部运行结果均需以本地实测为准。

## 6. 本讲小结

- **更新 RMS** 是一步优化中参数改动的均方根，是比学习率更贴近「参数实际动了多少」的稳定性指标；AdamW 的逐元素归一使其天然形状无关，Muon 的矩阵正交化输出则不然。
- Newton-Schulz 输出的 RMS \(\approx 1/\sqrt{\max(A,B)}\)（奇异值拉平到 1、共 \(\min(A,B)\) 个，摊到 \(AB\) 个元素），这是形状依赖的根源。
- `adjust_lr_for_muon`（[examples/toy_train.py:L142-L148](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L142-L148)）用 \(0.2\sqrt{\max(A,B)}\) 乘性放大学习率，与上述因子精确抵消，把所有 Muon 参数的更新 RMS 钉在 \(0.2\,\mathrm{lr}\)。
- 更新用 `adjusted_lr`、衰减用原始 `lr` 的不对称是刻意设计：更新需要形状归一，相对收缩必须统一，否则稳态权重规模被形状扭曲；稳态估算给出 \(\mathrm{RMS}(W_\infty)\approx 0.2/\mathrm{wd}\approx 2\)。
- 这与 u2-l3 的解耦权重衰减一起构成论文宣称的两大规模化技术，对应 README 摘要与 Key Ingredients 的官方表述，是「Muon 免调参、约 52% FLOPs」结论的地基。

## 7. 下一步学习建议

- **下一讲 u2-l5（内嵌 AdamW 后备轨道）**：本讲反复说「AdamW 分支的更新 RMS 形状无关、约为 0.2 量级对齐」——下一讲就去逐行验证这半边，并把手写 AdamW 与 `torch.optim.AdamW` 做逐步等价性对照，补全「矩阵与非矩阵参数更新 RMS 一致」的另一半证据。
- **u3-l1（Muon vs AdamW 对比实验）**：把本讲的综合实践扩展成公平的优化器对比（学习率扫描 + loss 曲线），在小规模上定性复现论文 52% FLOPs 的结论。
- **源码再读**：带着本讲结论重读 [examples/toy_train.py:L79-L104](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L79-L104) 的 docstring，找出所有「未随 Moonlight 改造更新」的描述（提示：L94 的谱范数说法），训练自己识别注释滞后的眼力。
- **论文阅读**：对照 `Moonlight.pdf` 中关于 update RMS 与权重衰减的分析章节，看论文如何用实验（而非本讲的分析性推导）支撑 \(0.2\) 这个常数的选择。
