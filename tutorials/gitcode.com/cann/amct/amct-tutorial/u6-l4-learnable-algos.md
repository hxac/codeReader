# 可学习算法：LAC/LWC/FlatQuant/OmniQuant

## 1. 本讲目标

学完本讲，你应该能够：

- 说清「可学习算法」相对于「搜索型/朴素型」算法的区别：它们把量化参数当作可以被反向传播训练的神经网络参数。
- 精读 LWC（裁权重）与 LAC（裁激活）的实现，能对比二者的 `clip_dim` 计算方式、参数形状差异，并解释「为什么权重可以按通道裁、激活却只用一个标量」。
- 精读 FlatQuant 的可学习正交结构变换（含 Kronecker 分解），并推导出它为何必须挂在结构层、靠 `inv_t` 在激活侧与权重侧配对调用以保持 `x @ W⊤` 不变。
- 了解 OmniQuant（对角缩放）与 AutoRound（可学习舍入偏移）的算法定位，理解四种算法在 `target`、可学习参数、`export_deploy` 支持上的差异。

本讲是 u6-l1（算法基类与 `is_observe` 通路）、u6-l2（算法注册表与 `target` 路由）的延续，只讲清楚「可学习这一类算法到底学了什么、怎么学、挂在哪里」。

## 2. 前置知识

### 2.1 什么叫「可学习」

回顾 u2-l3：AMCT 的权重量化算法分三组——朴素组（Min-Max/Cast，无参数）、搜索优化组（AWQ/GPTQ，在校准数据上网格搜索最优 scale，无梯度）、**可学习组**（本讲主角）。

「可学习」指算法带 `requires_grad=True` 的 `torch.nn.Parameter`，这些参数不是手算或搜索出来的，而是由 u4-l3 的 `BlockwiseSolver` 用重建损失（MSE）做反向传播训练出来的。训练目标只有一个：**让量化后的子模块输出尽量逼近原始浮点子模块的输出**。整个训练过程中，原始模型权重被冻结，只有算法参数在更新（详见 u4-l3 的 `trainable_params` 点名机制）。

### 2.2 三类 target 决定挂载点

回顾 u6-l2：每个算法在注册时用 `targets=(...)` 声明自己作用在哪一类对象上：

| target | 含义 | 挂载点 | 代表算法 |
| --- | --- | --- | --- |
| `weight` | 裁剪/改写权重 | 最内层 `WeightQuantizer` | LWC、AutoRound |
| `activation` | 裁剪激活 | 中层 `ActivationQuantizer` | LAC |
| `structure` | 搬运坐标系（激活与权重同时变换） | 最外层 `QuantGatedMLP` 的 `input_transform`/`hidden_transform` | FlatQuant、OmniQuant |

`structure` 这一格**只允许一个算法**，因为变换矩阵 `T` 必须在激活侧正向、权重侧逆转置共用同一个对象，多于一个会在装配期抛 `ValueError`（见 u6-l2）。理解这张表是本讲的前提。

### 2.3 一个贯穿全讲的数学不变式

本讲反复用到一条等式。`nn.functional.linear` 的定义是：

\[ \text{linear}(x, W) = x \cdot W^{\top} \]

结构类算法（FlatQuant/OmniQuant）改造的是「在哪个坐标系里量化」，但**绝不能改变未量化时的线性输出**。因此它们对激活做变换 `T` 的同时，必须对权重做**配平**变换 `T^{-1}`（带转置的细节后文推导），使得：

\[ (x \cdot T) \cdot (W \cdot T^{-\top})^{\top} = x \cdot W^{\top} \]

记住这条不变式，本讲的 `inv_t` 设计就不再神秘。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [amct_pytorch/algorithms/quant/auto_clip.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_clip.py) | LWC（裁权重）与 LAC（裁激活）两个可学习截断算法 |
| [amct_pytorch/algorithms/quant/flatquant.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/flatquant.py) | FlatQuant：可学习正交结构变换（Kronecker 分解） |
| [amct_pytorch/algorithms/quant/omniquant.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/omniquant.py) | OmniQuant：可学习对角缩放（structure target 的简化版） |
| [amct_pytorch/algorithms/quant/auto_round.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_round.py) | AutoRound：可学习舍入偏移（weight target，带 `quantize()` hook） |
| [amct_pytorch/algorithms/quant/base.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/base.py) | `QuantAlgorithmBase` 公共契约（u6-l1 已讲） |
| [amct_pytorch/quantization/modules/quant_base.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py) | `WeightQuantizer`/`ActivationQuantizer`，算法的真实挂载点与 `algo_forward` 循环 |
| [amct_pytorch/quantization/modules/quant_linear.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_linear.py) | `QuantLinear`：在权重侧调用 `structure_transform(weight, inv_t=True)` |
| [amct_pytorch/common/models/llm/common/quant_apply.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py) | `QuantGatedMLP`：structure 算法的挂载处，激活侧调用 `input_transform(x)` |

## 4. 核心概念与源码讲解

### 4.1 可学习截断算法：LWC 与 LAC

#### 4.1.1 概念说明

「截断（clipping）」是量化前最关键的一步。回顾 u2-l1 的量化公式 `int_val = clip(round(float_val/scale), ...)`：`scale` 通常取自张量的最大绝对值。问题在于——少数极端大的 **outlier**（离群点）会撑大 `scale`，导致其余正常值被压缩进很少的几个整数格点，相对误差暴增。

朴素 Min-Max 算法直接用真实最大最小值，毫无抵抗力。**可学习截断**的思路是：引入一个软门 `clip_factor`，让「有效截断边界」从 `max` 收缩为 `sigmoid(clip_factor) * max`。`clip_factor` 越大，`sigmoid` 越接近 1（几乎不裁）；越小（趋于 0），`sigmoid` 越接近 0.5（把幅度砍半）。这个 `clip_factor` 是可训练参数，由 `BlockwiseSolver` 根据重建损失自动学出每一层、每一组该裁多少。

AMCT 把这个思想分成了对偶的两个算法：

- **LWC**（Learnable Weight Clipping，注册名 `lwc`，`target=weight`）：学权重的截断比例。
- **LAC**（Learnable Activation Clipping，注册名 `lac`，`target=activation`）：学激活的截断比例。

#### 4.1.2 核心流程

两者共用一套「软门截断」模板：

```text
1. 取当前张量 x 的逐组最大/最小值 cur_max / cur_min
2. cur_max *= sigmoid(clip_factor_max)   # 软收缩上界
   cur_min *= sigmoid(clip_factor_min)   # 软收缩下界
3. x = clamp(x, cur_min, cur_max)        # 截断后交给后续量化器
```

差异在于「逐组」的粒度与「是否需要校准缓存统计量」：

- **LWC 处理权重**：权重是静态的、训练时已知，可以承担「每个输出通道一个 clip_ratio」的细粒度参数。
- **LAC 处理激活**：激活每个 token 都在变，逐通道学一组比例既容易对校准集过拟合、参数也太贵。于是 LAC 只学**一个全局标量** `clip_factor`，叠在一个**逐 token 现算**的 min/max 之上（或叠在 per-tensor 缓存的 maxval/minval 之上）。

#### 4.1.3 源码精读

先看注册与构造。两者都继承 `QuantAlgorithmBase`，靠装饰器登记进 `ALGO_REGISTRY`：

- [amct_pytorch/algorithms/quant/auto_clip.py:L24-L28](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_clip.py#L24-L28)：LWC 声明 `targets=("weight",)`，初始化值 `init_value = 4.0`（`sigmoid(4)≈0.982`，起点几乎不裁，把「要不要裁」交给训练决定）。
- [amct_pytorch/algorithms/quant/auto_clip.py:L65-L69](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_clip.py#L65-L69)：LAC 声明 `targets=("activation",)`。

**LWC 的 clip_dim 计算**是本讲的第一个关键点。[auto_clip.py:L58-L62](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_clip.py#L58-L62) 中：

```python
def _update_clip_dim(self):
    if self.quant_dtype == "mxfp":
        self.clip_dim = self.w_size[0] * self.w_size[1] // 32
    else:
        self.clip_dim = self.w_size[0]
```

- `w_size` 是权重形状 `[out_features, in_features]`（由 `QuantLinear.__init__` 写入 `args.w_size`）。
- INT 路径：`clip_dim = out_features`，即「每个输出通道一个裁剪比例」。
- MXFP 路径：权重被按 32 个一组切分（见 u2-l2 的 shared exponent），`clip_dim = (out*in)//32`，即「每个 microscaling 组一个比例」。

由此 [auto_clip.py:L38-L43](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_clip.py#L38-L43) 两个可学习参数形状都是 `(clip_dim, 1)`，在 [auto_clip.py:L45-L53](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_clip.py#L45-L53) 的 `apply_clip` 里沿 dim=1 取 max/min 后按行广播：

```python
cur_min, cur_max = x.min(1, keepdim=True)[0], x.max(1, keepdim=True)[0]
cur_max *= self.sigmoid(self.clip_factor_max.to(x.device))
cur_min *= self.sigmoid(self.clip_factor_min.to(x.device))
x = torch.clamp(x, min=cur_min, max=cur_max)
```

注意 LWC **没有** `maxval/minval` 缓存，也**没有** 重写 `calib_forward`——它继承基类的透明实现（`return x`）。因为权重不变，每次前向直接从当前权重张量现算 `min/max` 即可，校准态下它就是个 no-op。

**LAC 走的是另一条路**。[auto_clip.py:L71-L84](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_clip.py#L71-L84) 构造函数里：

```python
self.clip_factor_max = torch.nn.Parameter(torch.ones((1,)) * self.init_value, requires_grad=True)
self.clip_factor_min = torch.nn.Parameter(torch.ones((1,)) * self.init_value, requires_grad=True)
self.register_buffer('maxval', torch.zeros((1,)))
self.register_buffer('minval', torch.zeros((1,)))
```

- 两个 `clip_factor` 形状都是 `(1,)`——**标量**，不是 `(clip_dim, 1)`。LAC 没有 `clip_dim` 这个属性。
- 额外注册了 `maxval`/`minval` 两个 buffer（注意是 `register_buffer` 而非 `Parameter`，不参与训练，只在校准态被更新）。

为什么 LAC 需要 buffer？因为激活在校准阶段是流动的，必须把「跨 batch 看到的历史最大/最小值」记下来。这正是 u6-l1 讲过的 `is_observe` 通路：[auto_clip.py:L132-L137](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_clip.py#L132-L137) 的 `calib_forward` 在 observe 态偷偷更新统计量并原样返回：

```python
def calib_forward(self, x, *args, **kwargs):
    if x.max() > self.maxval.to(x.device):
        self.maxval.data = x.max()
    if x.min() < self.minval.to(x.device):
        self.minval.data = x.min()
    return x
```

到了量化态，[auto_clip.py:L112-L127](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_clip.py#L112-L127) 的 `apply_clip` 有两条分支：

- `is_per_tensor=True`：直接用校准期缓存的 `maxval/minval`，再乘 `sigmoid(clip_factor)`。整张激活一个范围。
- `is_per_tensor=False`（默认）：把激活 reshape 成 `(-1, hidden_dim)`，**逐 token** 现算 `amax/amin`，再乘全局标量 `sigmoid(clip_factor)`。

**正因为 LAC 持有 buffer，它必须重写存取接口**。[auto_clip.py:L86-L92](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_clip.py#L86-L92) 的 `export_ptq_params` 把 `clip_factor` 与 `maxval/minval` 一起导出；而 LWC 用的是基类 [base.py:L38-L39](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/base.py#L38-L39) 的默认实现（只导出 `named_parameters()`），因为它没有需要持久化的 buffer。

下表浓缩两者的差异（这是本讲最该记住的一张表）：

| 维度 | LWC（weight） | LAC（activation） |
| --- | --- | --- |
| 可学习参数形状 | `(clip_dim, 1)`，逐通道/逐组 | `(1,)`，全局标量 |
| `clip_dim` 来源 | `w_size[0]`（INT）或 `w_size[0]*w_size[1]//32`（MXFP） | 无此概念 |
| 校准 buffer | 无 | `maxval`/`minval`，各 `(1,)` |
| `calib_forward` | 继承基类（透明 no-op） | 重写：更新 `maxval/minval` |
| 范围计算时机 | 每次前向从权重现算 min/max | per-token 现算 或 用 per-tensor 缓存 |
| `export_ptq_params` | 基类默认 | 重写（含 buffer） |

#### 4.1.4 代码实践

**实践目标**：亲手验证 LWC 与 LAC 在 `clip_dim`/参数形状上的差异，并理解其背后的「权重静态 vs 激活动态」直觉。

**操作步骤**（参考真实测试 [tests/unit_test/algorithms/test_auto_clip.py:L32-L36](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/unit_test/algorithms/test_auto_clip.py#L32-L36) 与 [test_auto_clip.py:L99-L100](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/unit_test/algorithms/test_auto_clip.py#L99-L100) 的构造方式）：

```python
# 示例代码：用 SimpleNamespace 伪造 args，直接实例化算法
from types import SimpleNamespace
from amct_pytorch.algorithms.quant.auto_clip import LWC, LAC

# LWC：权重形状 (4, 8)，INT 量化
lwc = LWC(SimpleNamespace(w_size=(4, 8), quant_dtype="int"), w_bits=8)
print(lwc.clip_dim)                       # 4  == w_size[0]
print(lwc.clip_factor_max.shape)          # (4, 1)  逐输出通道

# LWC：权重形状 (8, 64)，MXFP 量化
lwc_mx = LWC(SimpleNamespace(w_size=(8, 64), quant_dtype="mxfp"), w_bits=8)
print(lwc_mx.clip_dim)                    # 16 == 8*64//32  逐 microscaling 组

# LAC：激活，无需 w_size
lac = LAC(SimpleNamespace(is_per_tensor=False))
print(lac.clip_factor_max.shape)          # (1,)  全局标量
print(hasattr(lac, "maxval"), hasattr(lac, "clip_dim"))  # True  False
```

**需要观察的现象**：LWC 的参数维度随权重形状/数据类型变化（INT 按输出通道、MXFP 按 32 元素组）；LAC 的参数永远是 `(1,)`，且多出 `maxval/minval` 两个 buffer。

**预期结果**：`4 / (4,1) → 16 / True False`。

**进阶观察**：把 LWC 的 `clip_factor` 全置 0，再喂一个 `[-2,-1,1,2]` 的行向量，验证 [test_auto_clip.py:L77-L87](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/unit_test/algorithms/test_auto_clip.py#L77-L87) 的断言——`sigmoid(0)=0.5`，行最大值 2 被截到 1，最小值 -2 被截到 -1。LAC 在 per-tensor 模式下同理（[test_auto_clip.py:L121-L131](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/unit_test/algorithms/test_auto_clip.py#L121-L131)）。如本地未装 NPU/torch，此步为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 LWC 不需要 `maxval/minval` buffer，而 LAC 必须？

> **答案**：权重在量化流程中是静态张量，每次前向直接对当前权重 `x.min(1)/x.max(1)` 即可得到范围，无需跨样本累积。激活则在校准阶段是流动的，单个 batch 看不到全局极值，必须用 buffer 在 `calib_forward` 里逐步累积 `maxval/minval`，供后续量化态使用。

**练习 2**：把 LWC 的 `init_value` 从 4.0 改成 0.0，训练起点会发生什么变化？

> **答案**：`sigmoid(4)≈0.982`，起点几乎不裁剪，等训练决定要不要裁；`sigmoid(0)=0.5`，起点直接把有效幅度砍半，是一种「上来就激进裁剪」的初始化，可能让重建损失初期偏大、训练更难收敛。`init_value` 控制的是「先验裁剪强度」。

---

### 4.2 可学习结构变换：FlatQuant

#### 4.2.1 概念说明

LWC/LAC 是在**原坐标系**里把 outlier 截掉，本质是「忍痛割爱」——大值被压小，必然丢信息。FlatQuant 走了另一条路：**先把张量旋转到一个新坐标系，让 outlier 被摊平到多个通道上**，再做量化，误差自然变小。

直观比喻：一组数据里有少数几个超大值，直接量化会把整把尺子的刻度拉得很稀；做一个正交旋转（保持长度、只换基），相当于把那几个超大值的能量重新分配到各方向，每个方向的极值都变小了，尺子刻度变密，量化更准。

FlatQuant 改编自开源项目 [FlatQuant](https://github.com/ruikangliu/FlatQuant)（见文件头版权声明），AMCT 只保留了它的核心——「可学习的仿射结构变换」，没有移植其完整的训练脚手架（见 [flatquant.py:L107-L114](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/flatquant.py#L107-L114) 的类注释）。

#### 4.2.2 核心流程

FlatQuant 学的是一个正交矩阵 `M`（以及一个可选的对角缩放 `diag`）。关键约束来自第 2.3 节的不变式：旋转必须同时作用于激活与权重，且互为配平。

- **激活侧**（`inv_t=False`）：`x' = x @ M`（再乘 `diag`）。
- **权重侧**（`inv_t=True`）：`W' = W @ M^{-⊤}`（再除 `diag`）。

推导权重侧为何是 `M^{-⊤}`。由 `linear(x, W) = x @ W⊤` 与不变式要求：

\[
\text{linear}(x', W') = x' \cdot W'^{\top} = (x \cdot M) \cdot (W \cdot M^{-\top})^{\top} = x \cdot M \cdot M^{-1} \cdot W^{\top} = x \cdot W^{\top}
\]

其中用到了 \((M^{-\top})^{\top} = M^{-1}\)。这正是 `inv_t` 这个布尔开关的数学含义：同一个 `M`，激活侧用本阵，权重侧用逆的转置。对角 `diag` 同理——激活侧乘、权重侧除，互为抵消。

由于同一个变换对象必须被激活侧与权重侧**共享**，FlatQuant 只能挂在能同时看到这两者的「结构层」（`QuantGatedMLP`），不能塞进只看得到权重的 `WeightQuantizer`。这也是 u6-l2 里「structure 槽只允许一个算法」的根本原因。

#### 4.2.3 源码精读

FlatQuant 的实现分四个零件。

**(1) 随机正交矩阵初始化**。[flatquant.py:L28-L36](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/flatquant.py#L28-L36) 用 QR 分解生成正交矩阵 `Q`，再用 `r` 对角元的符号做修正，保证数值稳定的正交初值（训练再逐步调整）：

```python
q, r = torch.linalg.qr(matrix)
diag = torch.sign(torch.diag(r))
diag = torch.where(diag == 0, torch.ones_like(diag), diag)
q = q @ torch.diag(diag)
```

**(2) Kronecker 分解乘法**。[flatquant.py:L39-L46](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/flatquant.py#L39-L46) 把一个 `dim×dim` 的大矩阵分解成 `左矩阵 ⊗ 右矩阵` 的形式分块相乘，避免直接存 `dim²` 个参数：

```python
x = x.reshape(-1, left.shape[0], right.shape[0])
x = torch.matmul(x, right.to(x))
x = torch.matmul(left.to(x).transpose(0, 1), x)
```

**(3) 单矩阵变换**。[flatquant.py:L49-L62](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/flatquant.py#L49-L62) 的 `_InvFlatSingleTransform` 在维度不可分解时退化为一个完整的 `dim×dim` 正交矩阵；`inv_t=True` 时取 `linalg.inv(matrix).transpose(0,1)`，正是上面的 `M^{-⊤}`：

```python
def forward(self, x, inv_t=False):
    matrix = self.linear.weight
    if inv_t:
        matrix = torch.linalg.inv(matrix).transpose(0, 1)
    x = x.reshape(-1, matrix.shape[0])
    return x.matmul(matrix).reshape(init_shape)
```

**(4) 分解变换**。[flatquant.py:L65-L98](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/flatquant.py#L65-L98) 的 `_InvFlatDecomposeTransform` 是主路径：`linear_left`（`left_size×left_size`）+ `linear_right`（`right_size×right_size`）+ 可选 `diag_scale`。激活侧（`inv_t=False`）乘 `diag`，权重侧（`inv_t=True`）除 `diag`，两个矩阵在权重侧同样取逆转置。

**(5) 主类装配**。[flatquant.py:L116-L139](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/flatquant.py#L116-L139) 的 `__init__` 决定走分解还是单矩阵：

```python
use_decompose = (self.matrix_size > 1
                 and self.dim_size > self.matrix_size
                 and self.dim_size % self.matrix_size == 0)
if use_decompose:
    left_size = self.dim_size // self.matrix_size
    right_size = self.matrix_size
    self.transform = _InvFlatDecomposeTransform(left_size, right_size, self.add_diag)
else:
    self.transform = _InvFlatSingleTransform(self.dim_size)
```

`dim_size` 与 `matrix_size` 来自一个上下文对象 `ctx`（即 `AlgoBuildContext`，定义在 [amct_pytorch/algorithms/quant/__init__.py:L47-L50](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/__init__.py#L47-L50)），由挂载点 [quant_apply.py:L198-L206](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L198-L206) 构造，`matrix_size` 默认 128（[flatquant.py:L120](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/flatquant.py#L120)）。`dim_size` 缺失则直接抛错（[flatquant.py:L123-L124](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/flatquant.py#L123-L124)）。

**(6) 前向分发**。[flatquant.py:L141-L146](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/flatquant.py#L141-L146) 根据 `is_observe` 切换通路——校准态透明、量化态施加变换（与 u6-l1 的通路开关一致）：

```python
def forward(self, x, inv_t=False, name=None):
    if self.is_observe:
        return self.calib_forward(x, inv_t=inv_t, name=name)
    return self.transform(x, inv_t=inv_t)
```

最后看 `inv_t=True` 是在哪里被调用的——这正是本讲实践任务的题眼。[quant_linear.py:L39-L66](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_linear.py#L39-L66) 的 `QuantLinear.forward` 里，训练态与 eval 态都在量化前对权重施加 `structure_transform(weight, inv_t=True)`：

```python
weight = self.linear.weight
if structure_transform is not None:
    weight = structure_transform(weight, inv_t=True, name=self.name)
weight = self.weight_quantizer(weight)
```

而激活侧的 `inv_t=False` 调用在 [quant_apply.py:L165-L180](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L165-L180) 的 `QuantGatedMLP.forward`：先 `input_states = self.input_transform(input_states)`（默认 `inv_t=False`），再把同一个 `self.input_transform` 作为 `structure_transform=` 传给 `up_proj`/`gate_proj`（这两个是 `QuantLinear`，内部会用 `inv_t=True` 处理权重）。

#### 4.2.4 代码实践

**实践目标**：用真实测试证明「FlatQuant 的 `inv_t` 变换保持 `F.linear` 输出等价」，并据此回答：为什么它必须挂在结构层而不是直接量化。

**操作步骤**：阅读并复现 [tests/unit_test/algorithms/test_flatquant.py:L145-L158](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/unit_test/algorithms/test_flatquant.py#L145-L158) 的等价性测试：

```python
# 示例代码：手工把 left/right 设成置换阵、diag 设成 [1,2,3,4]
flat = _make(dim_size=4, matrix_size=2)
with torch.no_grad():
    flat.transform.linear_left.weight.copy_(torch.tensor([[0.,1.],[1.,0.]]))
    flat.transform.linear_right.weight.copy_(torch.tensor([[0.,1.],[1.,0.]]))
    flat.transform.diag_scale.copy_(torch.tensor([1.,2.,3.,4.]))

x = torch.tensor([[1.,2.,3.,4.],[2.,3.,4.,5.]])
weight = torch.tensor([[1.,0.5,0.25,0.125],[2.,1.,0.5,0.25]])
transformed_x = flat(x)                      # 激活侧 inv_t=False
transformed_weight = flat(weight, inv_t=True) # 权重侧 inv_t=True
assert torch.allclose(
    F.linear(transformed_x, transformed_weight),
    F.linear(x, weight), atol=1e-5)          # 两者相等！
```

**需要观察的现象**：尽管 `transformed_x ≠ x`、`transformed_weight ≠ weight`，但 `F.linear(transformed_x, transformed_weight)` 与 `F.linear(x, weight)` 完全相等（误差仅来自浮点）。这证明 `inv_t` 配平是数学上严格的等价变换，没有引入额外误差——量化前的语义不变。

**预期结果**：断言通过；两次 `F.linear` 输出逐元素近似相等。

**回答实践问题「为何在结构层做正交变换而不是直接量化」**：

1. **目的**：直接量化会让少数 outlier 撑大 scale，扁平化精度；正交旋转把 outlier 能量摊到多通道，每个通道极值都变小，量化刻度变密，误差下降。
2. **必须配平**：旋转只对激活做会改变 `linear` 输出，必须在权重侧用 `M^{-⊤}`（即 `inv_t=True`）配平，保证 `x@W⊤` 不变。这一配平要求「同一个 `M` 同时作用于激活和权重」。
3. **挂载位置**：`WeightQuantizer` 只看得到权重、`ActivationQuantizer` 只看得到激活，谁都拿不到对方；只有结构层 `QuantGatedMLP` 同时持有 `input_transform`/`hidden_transform`（激活侧调用）和下游 `QuantLinear` 的句柄（权重侧通过 `structure_transform=` 参数传入）。因此 FlatQuant 只能挂在结构层，并由 `QuantLinear` 内部用 `inv_t=True` 完成权重侧配平。
4. **唯一性**：正因为配平依赖「同一个变换对象」，两个 structure 算法的 `M` 无法互相配平，所以 structure 槽只允许一个算法（u6-l2 的装配期校验）。

如本地未装 NPU/torch，可只阅读测试断言理解行为（「待本地验证」运行）。

#### 4.2.5 小练习与答案

**练习 1**：`dim_size=8`、`matrix_size=4` 时，FlatQuant 会走单矩阵还是分解路径？分解后的 `linear_left/right` 形状各是多少？

> **答案**：`8 > 4` 且 `8 % 4 == 0`，走分解路径。`left_size = 8//4 = 2`、`right_size = 4`，故 `linear_left.weight` 形状 `(2,2)`、`linear_right.weight` 形状 `(4,4)`、`diag_scale` 形状 `(8,)`。参见 [test_flatquant.py:L81-L86](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/unit_test/algorithms/test_flatquant.py#L81-L86)。

**练习 2**：为什么 FlatQuant 用 Kronecker 分解而不是直接学一个 `dim×dim` 矩阵？

> **答案**：直接学 `dim×dim`（如 4096×4096）有上千万参数，训练慢且易过拟合；分解成 `left_size×left_size` 与 `right_size×right_size` 两个小矩阵（如 32×32 与 128×128）参数量降到几千，且仍能表达丰富的变换。Kronecker 分解是「用少量参数近似大矩阵」的经典降参手段。

---

### 4.3 同族对照：OmniQuant 的对角缩放与 AutoRound 的可学习舍入

本节把另外两个可学习算法放到同一张对照表里，重点是它们的**定位差异**，而非逐行精读。

#### 4.3.1 概念说明

**OmniQuant**（注册名 `omniquant`，`target=structure`）：可以理解成「FlatQuant 去掉旋转、只留对角缩放」。它学一个逐通道的 `log_scale`，激活侧除以 `scale`、权重侧乘以 `scale`——这正是经典 SmoothQuant 的可学习版本（SmoothQuant 的 scale 是一次性算出来的，OmniQuant 的 scale 是训练出来的）。它和 FlatQuant 同属 structure target，因为同样需要激活/权重配平（除与乘互为抵消）。

**AutoRound**（注册名 `autoround`，`target=weight`）：思路完全不同。它不裁边界、也不换坐标系，而是学「**舍入偏移**」。标准量化用 `round()` 四舍五入，AutoRound 给每个权重元素学一个小偏移 `v`，让舍入方向可学，从而把总重建误差降到最低。它还附带学一组 `min_scale/max_scale` 来微调裁剪范围。

#### 4.3.2 核心流程

- **OmniQuant**：校准态用激活逐通道 absmax 的对数初始化 `log_scale`（SmoothQuant 式热启动）；量化态 `scale = exp(log_scale).clamp(...)`，激活 `/scale`、权重 `*scale`。
- **AutoRound**：把权重按 `group_size` 分组（INT 为 -1 即 per-channel、MXFP 为 32、hifp 不支持）；学 `value`（与分组权重同形的偏移）和 `min_scale/max_scale`（逐组）；通过自定义 `quantize()` hook 接管整个权重量化，把偏移 `v` 喂给底层数据类型量化器。

#### 4.3.3 源码精读

**OmniQuant** 的核心在 [omniquant.py:L30-L53](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/omniquant.py#L30-L53)。可学习参数只有一个 `log_scale`，形状 `(1, dim)`：

```python
self.log_scale = torch.nn.Parameter(torch.zeros((1, self.dim)), requires_grad=True)
```

`forward` 用 `inv_t` 切方向——激活侧（`inv_t=False`）除、权重侧（`inv_t=True`）乘，配平关系 `\((x/s) \cdot (W \cdot s)^{\top} = x \cdot W^{\top}\)`（逐通道 `s` 广播）：

```python
scale = self._get_scale(dtype=x.dtype, device=x.device)
if not inv_t:
    x = x / scale
else:
    x = x * scale
```

校准态 [omniquant.py:L55-L67](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/omniquant.py#L55-L67) 用激活 absmax 的对数热启动 `log_scale`，且 `inv_t=True` 时直接返回（权重侧不校准）。注意它有个 `transform` 方法是空的占位符 [omniquant.py:L38-L39](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/omniquant.py#L38-L39)，真正的变换逻辑在 `forward` 里。

**AutoRound** 的核心在 [auto_round.py:L73-L96](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_round.py#L73-L96)。三个可学习参数：

```python
self.value = nn.Parameter(torch.zeros_like(grouped_weight), requires_grad=True)  # 舍入偏移
self.min_scale = nn.Parameter(torch.ones(scale_shape, dtype=torch.float32), requires_grad=True)
self.max_scale = nn.Parameter(torch.ones(scale_shape, dtype=torch.float32), requires_grad=True)
```

分组策略见 [auto_round.py:L98-L107](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_round.py#L98-L107)：`int→-1`、`mxfp→32`、`hifp→直接抛错`。

AutoRound 最大的特点是它定义了 `quantize()` 方法 [auto_round.py:L125-L127](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_round.py#L125-L127)。回顾 u7-l1 / `WeightQuantizer.algo_forward` [quant_base.py:L132-L147](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L132-L147)：一旦发现算法带 `quantize` 方法，就把它收为「自定义量化 hook」，跳过普通 `algo(x)` 调用，并在 `WeightQuantizer.forward` 里改走 `quantize_algo.quantize(x, self.quant_obj)` [quant_base.py:L170-L174](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L170-L174)。所以 AutoRound 的 `forward` 只是 `return weight`（[auto_round.py:L129-L130](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_round.py#L129-L130)），真正干活的是 `quantize`/`export_deploy`。

`_compute_clip_range` [auto_round.py:L137-L154](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_round.py#L137-L154) 用 `min_scale/max_scale`（`clamp` 到 `[0,1]`）分别收缩负侧/正侧范围，最终取对称的 `[-max_abs, max_abs]`。

**四种算法全景对照**：

| 算法 | target | 可学习参数 | 作用对象 | deploy 支持 | 特点 |
| --- | --- | --- | --- | --- | --- |
| LWC | weight | `clip_factor` `(clip_dim,1)` | 权重逐通道/组截断 | 间接（导出参数，部署时重算 clip） | 软门截断 |
| LAC | activation | `clip_factor` `(1,)` + buffer | 激活截断 | 间接（含 buffer） | observe 通路 |
| FlatQuant | structure | 正交矩阵 + 对角 | 激活/权重坐标系旋转 | 通过 `structure_transform(inv_t=True)` 烘焙 | 唯一 structure |
| OmniQuant | structure | `log_scale` `(1,dim)` | 激活/权重对角缩放 | 同上 | 可学习 SmoothQuant |
| AutoRound | weight | `value` + `min/max_scale` | 权重舍入偏移 | 直接（`export_deploy` 带 `v`） | quantize hook |

注：`--algos` 不能同时填两个 structure 算法（如 `flatquant omniquant`），会在装配期报错；但可以 `lwc lac flatquant` 这样 weight+activation+structure 各取一个组合使用。

#### 4.3.4 代码实践

**实践目标**：对照源码理解「AutoRound 通过 `quantize()` hook 接管权重量化」与「OmniQuant 走 structure 通路」的差异。

**操作步骤**（源码阅读型实践）：

1. 打开 [quant_base.py:L132-L147](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L132-L147) 的 `WeightQuantizer.algo_forward`，找到 `getattr(algo, "quantize", None)` 这一行。
2. 追踪：当 `--algos autoround` 时，AutoRound 有 `quantize` 方法 → 被收为 `quantize_algo`；`WeightQuantizer.forward` 随即走 [quant_base.py:L172-L173](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L172-L173) 的 `quantize_algo.quantize(x, self.quant_obj)` 分支，把偏移 `v` 传给底层量化器。
3. 对比 `--algos lwc`：LWC 没有 `quantize` 方法 → 走普通 `algo(x)` 即 `apply_clip` → 再走 `fake_quant`。

**需要观察的现象**：AutoRound 的 `forward` 几乎不干活（`return weight`），全部量化逻辑在 `quantize`/`export_deploy` 里；而 LWC/LAC 的 `forward` 才是主战场。

**预期结果**：能说清「带 `quantize()` 的算法走 hook 通路、不带的走 `algo(x)`+`fake_quant` 通路」这条分叉。这是 u7-l1 会展开的 `WeightQuantizer` 双分支，本讲只需建立直觉。

#### 4.3.5 小练习与答案

**练习 1**：OmniQuant 与 FlatQuant 都是 structure target，本质区别是什么？

> **答案**：OmniQuant 只学逐通道的对角缩放（`log_scale`），等价于可学习的 SmoothQuant，配平方式是激活除、权重乘；FlatQuant 学完整的（Kronecker 分解的）正交矩阵加对角，能把 outlier 在通道间旋转重分配，表达能力更强但参数与计算更重。可把 OmniQuant 视为 FlatQuant「去掉旋转、只留对角」的退化版。

**练习 2**：为什么 AutoRound 的 `forward` 只写 `return weight`，而 LWC 的 `forward` 要做 `apply_clip`？

> **答案**：AutoRound 通过定义 `quantize()` 方法接管了整个权重量化流程，`WeightQuantizer` 检测到 `quantize` 后会改走 hook 通路，`forward` 不会被 `algo_forward` 当作普通算法调用；LWC 没有 `quantize` 方法，必须靠 `forward` 返回截断后的权重，再由 `WeightQuantizer` 接着做 `fake_quant`。

---

## 5. 综合实践

**任务**：给一个 MLP 层假设配置 `--algos lwc lac flatquant`，画出从 `QuantGatedMLP.forward` 出发的完整数据流，标注每个算法挂在哪个 target、用哪个 `inv_t`，并解释三类 target 的协作关系。

**操作步骤**：

1. 阅读挂载与装配代码 [quant_apply.py:L165-L180](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L165-L180) 与 [quant_apply.py:L198-L206](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L198-L206)。
2. 结合 u6-l2 的 `build_algorithms_by_target`，把 `lwc` 路由到 `WeightQuantizer`、`lac` 路由到 `ActivationQuantizer`、`flatquant` 路由到 `input_transform`/`hidden_transform`。
3. 画出下面这条链（伪代码）：

```text
input_states
  → input_transform(input_states)            # FlatQuant, structure, inv_t=False
  → input_quant(x_q)                          # LAC: observe 时更新maxval/minval; 量化时 apply_clip + fake_quant
  → up_proj(x_q, structure_transform=input_transform)   # QuantLinear: 权重先 flatquant(inv_t=True) 再 LWC apply_clip 再 fake_quant
  → gate_proj(x_q, structure_transform=input_transform) # 同上
  → act_fn(gate)*up = hidden
  → hidden_transform(hidden)                  # FlatQuant, structure, inv_t=False (intermediate 维度)
  → hidden_quant(hidden_q)                     # LAC again
  → down_proj(hidden_q, structure_transform=hidden_transform)  # QuantLinear: 权重 flatquant(inv_t=True)+LWC
```

4. 回答三个问题：
   - 为什么 `input_transform` 在激活侧用 `inv_t=False`，而同一个对象在 `up_proj`/`gate_proj` 内部却用 `inv_t=True`？（答：第 2.3/4.2.2 节的配平不变式。）
   - 为什么 `flatquant` 与 `omniquant` 不能同时出现在 `--algos` 里？（答：structure 槽只允许一个，两个 `M` 无法互相配平。）
   - LWC 和 LAC 能否同时用？（答：能，分属 weight/activation 两个独立槽位，互不冲突。）

**预期结果**：得到一张标注了 target、`inv_t`、挂载点的数据流图，并能口述三类 target「structure 先搬坐标系 → activation 裁激活 → weight 裁权重」的协作顺序。如无法本地运行，可只做源码阅读与手绘流程图（「待本地验证」运行类）。

## 6. 本讲小结

- **可学习算法**带 `requires_grad=True` 参数，由 `BlockwiseSolver` 用重建损失反向训练，区别于朴素/搜索型算法。
- **LWC（weight）**按权重输出通道（INT）或 microscaling 组（MXFP）计算 `clip_dim`，参数形状 `(clip_dim,1)`，无 buffer；**LAC（activation）**只有一个标量 `clip_factor (1,)`，靠 `maxval/minval` buffer 在 observe 通路累积激活统计，因此必须重写 `export_ptq_params`。差异根源是「权重静态、激活动态」。
- **FlatQuant（structure）**学一个 Kronecker 分解的正交矩阵 + 对角缩放，靠 `inv_t` 在激活侧（用 `M`）与权重侧（用 `M^{-⊤}`）配平，保持 `x@W⊤` 不变；正交旋转把 outlier 摊平以降低量化误差。
- **OmniQuant** 是 FlatQuant 的对角退化版（可学习 SmoothQuant），**AutoRound** 是 weight target 的可学习舍入偏移，靠自定义 `quantize()` hook 接管整个权重量化。
- 三类 `target` 决定挂载点：weight→`WeightQuantizer`、activation→`ActivationQuantizer`、structure→`QuantGatedMLP`；structure 槽唯一，weight/activation 可各自组合。

## 7. 下一步学习建议

- **u7-l1（QuantLinear 与量化器模块）**：本讲多次引用了 `WeightQuantizer.algo_forward`/`forward` 与 `ActivationQuantizer` 的双通路，下一讲会完整打开 `QuantLinear.forward` 的 eval 缓存机制与 `export_deploy` 落盘细节，建议接着读。
- **u7-l2（量化数据类型与 export_deploy 落盘）**：本讲提到 LWC/LAC 的 deploy 是「间接」（导出参数后重算），AutoRound 是「直接」（带 `v` 的 `export_deploy`），具体的 payload 结构与命名映射在 u7-l2 展开。
- **动手验证**：在本地跑 `pytest tests/unit_test/algorithms/test_auto_clip.py tests/unit_test/algorithms/test_flatquant.py -m cpu`，对照本讲的断言解释，加深对 `clip_dim`、`inv_t` 配平的体感。
