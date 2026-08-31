# Coefficient：Neural Decaying Function 网络

## 1. 本讲目标

本讲进入 4C4D 的论文核心——Neural Decaying Function 的网络实现 `Coefficient`。学完后你应该能够：

1. 说出 `Coefficient` 网络输入的 7+2 = 9 个维度分别对应高斯的哪个属性；
2. 解释 `forward` 中三段预处理各自的作用：opacity 的 \([-1,1]\) 映射、positions 的逐维标准化、scales 的「取对数再标准化」；
3. 理解 Sigmoid 输出 \((0,1)\) 如何被调用方线性映射到 \([f_\min, f_\max]=[0.996, 0.998]\)，并解释这个「极窄区间」为何仍能产生足够强的衰减效果；
4. 为 `Coefficient` 写出一个可运行的单元测试，覆盖输入维度、输出范围、梯度回传三个检查点。

本讲只讲「网络本身」。衰减如何接入渲染循环、写回 `_opacity` 的细节、以及各衰减模式的对比，分别属于 u6-l2 与 u6-l3。

## 2. 前置知识

- **多层感知机（MLP）**：若干 `Linear`（线性层）与非线性激活函数堆叠而成的小网络。`nn.Linear(in, out)` 的参数量是 \(in \times out + out\)（权重加偏置）。
- **四个组件的行为**：
  - `nn.Linear`：线性变换；
  - `nn.ReLU`：把负数截断为 0，提供非线性；
  - `nn.Dropout(p)`：训练模式下以概率 \(p\) 随机把输入置零（并放大其余值），是正则化手段；`eval()` 模式下关闭；
  - `nn.Sigmoid`：把任意实数压到 \((0,1)\)。
- **z-score 标准化**：对每个维度减去均值、除以标准差，使数据零均值、单位方差。
- **承接 u3-l1 / u3-l2 的「裸值存储 + 读时激活」**：`GaussianModel` 的可优化属性存裸值，读取时才激活——`get_opacity = sigmoid(_opacity)`、空间尺度过 `exp`、`get_xyzt = cat(_xyz, _t)`、`get_scaling_xyzt` 是过 `exp` 后的 4 维尺度。本讲网络的输入正是这些**激活后**的属性。
- **承接 u1-l1 的动机**：稀疏视角下几何学习远难于外观学习，因此用一个可学习的小网络给每个高斯的 opacity 乘上一个略小于 1 的「衰减因子」，引导梯度聚焦几何。
- **autograd 基础**：对 `loss.backward()` 后，`param.grad` 中存放梯度；梯度非空意味着该参数参与了损失的计算图。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `module/__init__.py` | 本讲主角，仅 49 行，定义 `Coefficient` 网络 |
| `scene/gaussian_model.py` | 调用方：`opacity_decay` 的 `net` 分支把高斯属性喂给网络；`training_setup` 为它建独立优化器 |
| `train.py` | 构造点：`opacity_decay` 开启时实例化 `Coefficient()`；训练循环里双优化器交替 step |
| `arguments/__init__.py` | `coefficient_lr` 与 `coefficient_weight_decay` 的默认值 |
| `utils/general_utils.py` | `inverse_sigmoid`，衰减结果写回裸值时使用（本讲只定位） |
| `gaussian_renderer/__init__.py` | 渲染入口处调用 `opacity_decay`（u6-l3 详讲） |

`Coefficient` 在整个仓库的使用链条非常短：`train.py` 构造 → 存进 `GaussianModel.coefficient` → 渲染时经 `opacity_decay(mode='net')` 调用 `forward` → 检查点里随 `capture()` 一起保存。

## 4. 核心概念与源码讲解

### 4.1 模块一：`Coefficient.__init__`——一个 353 参数的小网络

#### 4.1.1 概念说明

`Coefficient` 就是论文中 Neural Decaying Function 的具体实现：一个两隐藏结构极简的 MLP。它**不是**对整个场景学一个全局函数，而是对**每个高斯**独立输出一个衰减系数——输入是单个高斯的属性向量，输出是一个标量。

它在训练脚本里只有一处构造点：当命令行开关 `opacity_decay` 开启（本仓库默认开启）时创建，否则整个衰减机制不参与训练：

- [train.py:57-63](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L57-L63)：`args.opacity_decay` 为真则 `coefficient = Coefficient().cuda()`，否则为 `None`；随后作为 `coefficient=` 参数传入 `GaussianModel` 构造函数。
- [scene/gaussian_model.py:95](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L95)：`GaussianModel` 只是原样保存引用 `self.coefficient = coefficient`，网络本体不挂在 `GaussianModel` 的参数表里。

由于 `GaussianModel` 的高斯属性走自建的 `self.optimizer`，`Coefficient` 作为标准 `nn.Module` 需要一个**独立的优化器**，否则它的参数永远得不到更新：

- [scene/gaussian_model.py:501-503](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L501-L503)：`training_setup` 里为 `coefficient.parameters()` 单独建 `Adam`，学习率 `coefficient_lr`、权重衰减 `coefficient_weight_decay`。
- [arguments/__init__.py:92-93](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L92-L93)：两个超参默认值分别为 `1e-5` 与 `1e-4`。
- [train.py:460-461](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L460-L461)：若命令行给了 `--weight_decay`（默认 `1e-4`，见 [train.py:417](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L417)），会覆盖 `coefficient_weight_decay`——默认情况下两者恰好相等。
- [train.py:263-265](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L263-L265)：每次迭代高斯优化器 step 之后，`coef_optimizer` 也 step 并清梯度——这就是「联合优化」的实现落点（策略联动详见 u6-l4）。

#### 4.1.2 核心流程

```text
__init__(hidden_dim=32, use_4d_features=True, dropout_rate=0.1, opacity_only=False)
    ├─ 记录两个开关：use_4d_features、opacity_only
    ├─ input_dim = _calculate_input_dim()        # 4.2 模块详解
    └─ net = Linear(input_dim, 32) → ReLU → Dropout(0.1) → Linear(32, 1) → Sigmoid
```

训练脚本用的是全默认构造 `Coefficient()`，因此实际生效配置是：输入 9 维、隐藏 32 维、dropout 0.1、`opacity_only=False`。

#### 4.1.3 源码精读

- [module/__init__.py:6-15](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/module/__init__.py#L6-L15)：构造函数签名与两个开关的登记，`input_dim` 交给 `_calculate_input_dim` 计算。
- [module/__init__.py:17-23](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/module/__init__.py#L17-L23)：`nn.Sequential` 五层堆叠——注意 Dropout 位于两个 Linear **之间**，Sigmoid 是最后一层，保证输出天然落在 \((0,1)\)。

参数量可以直接手算（默认 9 维输入）：

\[
\text{Linear}(9{\to}32):\ 9\times 32+32=320,\qquad
\text{Linear}(32{\to}1):\ 32\times 1+1=33
\]

两者合计 **353 个可训练参数**——这个网络小到可以人工检查每一条前向计算，这正是「Neural Decaying Function」选择用极小 MLP 而非大网络的原因：它只需要学一个「什么形状的高斯该被压制」的先验，而不是重建外观。

#### 4.1.4 代码实践

**实践一：清点网络结构与参数量**

1. **实践目标**：确认默认构造下的层堆叠、输入维度与参数量。
2. **操作步骤**：在仓库根目录新建 `inspect_coef.py`（示例代码，跑完可删除，不要提交）：

   ```python
   # 示例代码：检查 Coefficient 的结构与参数量
   from module import Coefficient

   coef = Coefficient()                     # 全默认：hidden 32 / 4d 特征开 / dropout 0.1
   print(coef.net)
   total = sum(p.numel() for p in coef.parameters())
   print("input_dim =", coef.net[0].in_features)   # 预期 9
   print("params   =", total)                      # 预期 353
   ```

   运行 `python inspect_coef.py`（纯 CPU 即可，`Coefficient` 本身不依赖 CUDA）。
3. **需要观察的现象**：打印出的 `net` 是否是 `Linear(9,32) → ReLU → Dropout(0.1) → Linear(32,1) → Sigmoid`。
4. **预期结果**：`input_dim=9`、`params=353`。若把构造改为 `Coefficient(hidden_dim=64)`，参数量应变为 \(9\times64+64+64+1=705\)。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`Coefficient` 为什么需要独立的 `coef_optimizer`，而不能复用 `GaussianModel.optimizer`？

**答案**：`GaussianModel.optimizer` 的参数组是逐属性手工登记的（`_xyz`、`_opacity` 等，每组学习率不同），并且增删高斯时要对这些参数组做 `cat`/`prune` 状态迁移；`Coefficient` 的参数形状固定、不属于高斯属性序列，混入反而会破坏「每参数组恰好一个张量」的约定（`cat_tensors_to_optimizer` 里有 `len(group["params"])==1` 断言）。独立优化器让两者生命周期互不干扰。

**练习 2**：把 `hidden_dim` 从 32 改成 16，参数量变为多少？

**答案**：\(9\times16+16+16\times1+1 = 144+16+16+1 = 177\)。

**练习 3**：Dropout 在推理阶段（`render.py` 加载 checkpoint 后）会怎样？

**答案**：`nn.Dropout` 只在 `module.training=True` 时生效，调用 `coef.eval()` 或 `coef.train(False)` 后变为恒等映射。注意：本仓库渲染路径读取的是已写回 `_opacity` 的数值（衰减结果被 `inverse_opacity_activation` 持久化），并不在前向中重新调用 `Coefficient`，所以推理时 Dropout 是否关闭对渲染结果无影响；这一点在 u6-l2 展开。

### 4.2 模块二：`_calculate_input_dim`——9 维输入的账本

#### 4.2.1 概念说明

这个 5 行的小函数是网络输入的「账本」。源码注释写 `input_dim = 7  # opacity + positions + scales`，即 opacity 1 维 + 空间位置 3 维 + 空间尺度 3 维；`use_4d_features=True` 时再加 2 维。

**关键的账目核对**：默认构造下实际传入的并不是 3 维位置和 3 维尺度，而是 4 维的 `get_xyzt` 与 4 维的 `get_scaling_xyzt`，因此真实维度构成是：

\[
\underbrace{1}_{\text{opacity}} + \underbrace{4}_{xyz + t} + \underbrace{4}_{s_x,s_y,s_z,s_t} = 9
\]

「+2」多出来的两维，正是第四维的时间中心 `_t` 与时间尺度 `_scaling_t`。7 是按 3D 语义写的底账，2 是 4D 化的增量——账本与实际喂入的张量在 `net` 模式下恰好对齐。

#### 4.2.2 核心流程

```text
_calculate_input_dim():
    if opacity_only:      return 1        # 退化为只看 opacity
    input_dim = 7                          # 1 + 3 + 3（3D 语义底账）
    if use_4d_features:  input_dim += 2   # 时间中心 1 维 + 时间尺度 1 维
    return input_dim                       # 默认 9
```

#### 4.2.3 源码精读

- [module/__init__.py:25-31](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/module/__init__.py#L25-L31)：账本本体。
- [scene/gaussian_model.py:602-605](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L602-L605)：`opacity_decay` 的 `net` 分支调用 `self.coefficient(old_opacity, self.get_xyzt, self.get_scaling_xyzt)`——三个实参依次是 \((N,1)\)、\((N,4)\)、\((N,4)\)，拼接后 \((N,9)\)，与账本一致。
- [scene/gaussian_model.py:222-223](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L222-L223)：`get_xyzt = torch.cat([self._xyz, self._t], dim=1)`，位置与时间中心直接用裸值拼接（位置属性本来就不激活）。
- [scene/gaussian_model.py:202-203](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L202-L203)：`get_scaling_xyzt` 先把 `_scaling` 与 `_scaling_t` 拼接再过 `scaling_activation`（即 `exp`，见 [scene/gaussian_model.py:50](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L50)）——喂给网络的是**激活后**的尺度。
- [scene/gaussian_model.py:598-601](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L598-L601)：`mlp` 分支只传 `self.coefficient(old_opacity)` 一个参数。

**一个由静态分析得出的陷阱**：`mlp` 分支只给 `opacity` 一个输入，而默认构造下 `opacity_only=False`，`forward` 走到 `positions.mean(0)` 时 `positions` 是 `None`，会抛出 `AttributeError`。也就是说，`mode='mlp'` 只有在以 `Coefficient(opacity_only=True)` 构造时才能运行。渲染器实际调用的是默认 `mode='net'`（见 [gaussian_renderer/__init__.py:74](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L74)，未显式传 `mode`，落到 [scene/gaussian_model.py:584](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L584) 签名的默认值 `'net'`），所以正常训练路径不受影响。待本地验证（可按实践二复现）。

#### 4.2.4 代码实践

**实践二：核对账本——三种构造的维度断言**

1. **实践目标**：验证 `_calculate_input_dim` 的返回值与 `forward` 实际接受的张量维度一致，并亲眼确认 `mlp` 式调用的陷阱。
2. **操作步骤**（示例代码）：

   ```python
   # 示例代码：维度账本核对
   import torch
   from module import Coefficient

   N = 100
   opacity = torch.rand(N, 1)              # 模拟 get_opacity，值域 (0,1)
   xyzt    = torch.randn(N, 4)             # 模拟 get_xyzt
   sca     = torch.rand(N, 4) * 0.1        # 模拟 get_scaling_xyzt（激活后为正）

   for kw, expect in [({}, 9), ({"use_4d_features": False}, 7), ({"opacity_only": True}, 1)]:
       c = Coefficient(**kw)
       assert c.net[0].in_features == expect, (kw, c.net[0].in_features)
       out = c(opacity, xyzt, sca)
       assert out.shape == (N, 1)
       print(kw, "->", expect, "维输入 OK")

   # 复现 mlp 式调用在默认构造下的报错
   try:
       Coefficient()(opacity)
   except AttributeError as e:
       print("默认构造只喂 opacity 报错：", e)
   ```

3. **需要观察的现象**：三种构造的 `in_features` 分别为 9/7/1；最后一行捕获到 `AttributeError`。
4. **预期结果**：与上表一致；`opacity_only=True` 时 7 维构造喂 4 维张量会在 `cat` 处报维度不匹配（`1+4+4=9≠7`），说明 7 维模式要求喂 3 维位置 + 3 维尺度。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`use_4d_features=False`（7 维输入）时，`net` 分支的调用 `coefficient(opacity, get_xyzt, get_scaling_xyzt)` 会发生什么？

**答案**：`torch.cat((opacity, pos, sca), dim=1)` 得到 \((N,9)\)，而第一层 `Linear(7,32)` 期望 7 维输入，抛出矩阵维度不匹配的运行时错误。也就是说仓库里 `use_4d_features` 的默认值 `True` 与 `net` 模式是绑定的。

**练习 2**：为什么输入要包含位置和尺度，而不只看 opacity？

**答案**：只看 opacity 的衰减（`opacity_only=True` 或各解析模式）对所有高斯使用同一条一维曲线；加入位置与尺度后，网络能表达「某个区域、某种大小的高斯应被更强压制」的**空间-形状先验**——这正是论文「用神经网络函数替代手工衰减函数」的意义：衰减强度成为逐高斯、可被梯度塑造的量。

**练习 3**：`get_scaling_xyzt` 喂入的是激活后（`exp` 后）的尺度，这会不会让网络输入的数值范围失控？

**答案**：会在原始量级上失控（`exp` 后可能跨多个数量级），但 `forward` 里紧接着做 `log` + z-score 标准化（见 4.3 模块），最终输入仍被压回零均值、单位方差附近——数值范围由标准化兜底。

### 4.3 模块三：`Coefficient.forward`——预处理、拼接与 Sigmoid

#### 4.3.1 概念说明

`forward` 是「属性张量 → 衰减系数」的完整流水线：三段各自独立的预处理（opacity 重映射、位置标准化、尺度取对数后标准化）、一次拼接、一次网络前传。它解决的问题是：高斯的原始属性量纲各异（opacity 在 \((0,1)\)、位置是世界坐标、尺度跨数量级），直接喂给线性层会让训练失衡；同时输出的衰减系数必须严格有界，否则逐迭代相乘会数值爆炸——末端 Sigmoid 保证了这一点。

#### 4.3.2 核心流程

```text
forward(opacity, positions, scales):
    ① opacity = opacity*2 - 1                     # (0,1) → [-1,1]，零中心
    ② 若 opacity_only：直接 net(opacity)
    ③ pos = (positions - μ) / (σ + 1e-6)          # 逐维 z-score（对当前全部 N 个高斯）
    ④ sca = log(scales + 1e-6)，再做逐维 z-score   # 先回到 log 空间再标准化
    ⑤ x = cat(opacity, pos, sca) → (N, 9)
    ⑥ return net(x) ∈ (0,1)
```

调用方随后把输出线性映射为衰减因子：

\[
f = f_\min + (f_\max - f_\min)\cdot \text{coef},\qquad f_\min=0.996,\ f_\max=0.998
\]

（`f_min`/`f_max` 默认值见 [train.py:413-414](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L413-L414)，由 [gaussian_renderer/__init__.py:74](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L74) 传入 `opacity_decay`。）

**窄区间的复利效应**：单次衰减最多把 opacity 乘上 0.998（只减弱 0.2%），看似微不足道；但衰减从 `decay_from_iter=500`（[train.py:419](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L419)）起**每次迭代都执行**，因子连乘：

\[
f^{k} \approx e^{k\ln f}:\quad
0.998^{500}\approx 0.367,\quad
0.996^{500}\approx 0.135,\quad
0.998^{2000}\approx 0.018
\]

即网络输出的系数只要持续偏向 \(f_\min\)，几百到几千次迭代后就能把一个高斯的有效不透明度压到接近 0（等价于软删除）；反过来系数若学到 0.998 一侧，衰减几乎不生效。窄区间保证单步稳定，复利保证长期有力和可学习。

#### 4.3.3 源码精读

- [module/__init__.py:35](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/module/__init__.py#L35)：`opacity = opacity.mul(2).sub(1)`。`get_opacity` 是 sigmoid 输出，取值 \((0,1)\) 且训练后期常堆在 0 或 1 附近；线性映射到 \([-1,1]\) 让输入零中心、对称，对第一层 Linear 更友好。
- [module/__init__.py:37-38](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/module/__init__.py#L37-L38)：`opacity_only` 的捷径分支。
- [module/__init__.py:40-41](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/module/__init__.py#L40-L41)：位置逐维标准化。注意两点：其一，均值/方差是**对当前全部 N 个高斯现算的批统计**（`dim=0`），致密化增删点后，同一个高斯的标准化坐标会随种群分布漂移——网络看到的是「相对位置」而非绝对世界坐标；其二，这些运算都是可导的，渲染损失反传时梯度也会经此路径流回 `_xyz`/`_t`（静态事实，仓库未做 detach）。`unbiased=False` 用总体标准差，`+1e-6` 防除零。
- [module/__init__.py:43-45](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/module/__init__.py#L43-L45)：尺度先 `log(scales + 1e-6)` 再 z-score。由于传入的 `scales` 是过 `exp` 激活的（见 4.2.3），`log(exp(_scaling))` 近似还原出 log 空间裸值——网络实际消费的正是 u3-l1 讲过的「log 空间尺度」，量纲天然紧致，再标准化后更稳。
- [module/__init__.py:47-49](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/module/__init__.py#L47-L49)：`cat` 成 \((N,9)\) 后过 `net`，Sigmoid 保证输出严格落在 \((0,1)\)。
- [scene/gaussian_model.py:605](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L605)：调用方把输出嵌进 \( \text{opacity} \times (f_\min + (f_\max-f_\min)\cdot\text{coef}) \)，即上面的线性映射。
- [scene/gaussian_model.py:615-619](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L615-L619)：衰减结果经 `mask` 选择后，用 `inverse_opacity_activation`（即 [utils/general_utils.py:19-20](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py#L19-L20) 的 `log(x/(1-x))`）写回 `_opacity.data`——这条「持久化」路径属于 u6-l2 的主题，本讲只需知道：**本次迭代的渲染与反传用的是返回值（带计算图），写回的是数值**。

#### 4.3.4 代码实践

**实践三：输出范围与梯度回传检查**

1. **实践目标**：验证 Sigmoid 输出落在 \((0,1)\)，且 `loss.backward()` 后网络参数拿到非空梯度。
2. **操作步骤**（示例代码，在仓库根目录运行）：

   ```python
   # 示例代码：输出范围 + 梯度检查
   import torch
   from module import Coefficient

   torch.manual_seed(0)
   N = 5000
   coef = Coefficient()
   opacity = torch.rand(N, 1)                 # (0,1)
   xyzt    = torch.randn(N, 4) * 2            # 模拟世界坐标
   scales  = torch.rand(N, 4) * 0.05          # 模拟激活后尺度

   out = coef(opacity, xyzt, scales)
   assert out.shape == (N, 1)
   assert out.min() > 0 and out.max() < 1, "Sigmoid 输出应落在开区间 (0,1)"
   print("输出范围:", out.min().item(), out.max().item())

   loss = out.mean()                          # 任意依赖于全部输出的标量损失
   loss.backward()
   grads = [p.grad for p in coef.parameters()]
   assert all(g is not None for g in grads), "所有参数都应有梯度"
   assert any(g.abs().sum() > 0 for g in grads), "至少部分梯度非零"
   print("梯度检查通过，第一层权重梯度范数:", grads[0].norm().item())
   ```

3. **需要观察的现象**：输出最小值严格大于 0、最大值严格小于 1；四组参数（两层权重 + 两层偏置）的 `grad` 均非 None 且范数非零。
4. **预期结果**：全部断言通过。若把 `coef.eval()` 后再前向，Dropout 关闭，多次前向输出将完全一致（train 模式下则有随机抖动）。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么末端用 Sigmoid 而不是 ReLU 或恒等映射？

**答案**：衰减因子必须落在 \((0,1)\) 内才有「乘法衰减」的语义：输出 \(\ge 1\) 会变成放大（可能数值发散），输出 \(<0\) 会让 opacity 变负（物理无意义）。Sigmoid 把任意实数压入 \((0,1)\)，再由调用方线性映射到 \([f_\min,f_\max]\) 这个更窄的安全带内。

**练习 2**：位置的标准化用的是「当前全部高斯」的统计量，这带来什么行为？

**答案**：网络学到的是相对场景分布的位置先验，而非绝对坐标；同时随着致密化增删高斯，同一段世界坐标对应的标准化输入会缓慢漂移，网络相当于在一个非平稳输入分布上训练。另外该路径可导，渲染梯度会额外流回 `_xyz`/`_t`，与主梯度路径叠加。

**练习 3**：假如把 `f_min`/`f_max` 改成 0.5/1.0，会带来什么风险？

**答案**：单步最多衰减 50%，复利下 \(0.5^{10}\approx 0.001\)，几十次迭代就能把高斯压到不可见——衰减过猛会让大量高斯在几何学习到位之前就被「软删除」，训练失稳。0.996/0.998 的窄带是「单步温和、长期可控」的折中，也可以通过 `--f_min`/`--f_max` 命令行参数调整后实验观察。

## 5. 综合实践

把三个模块的检查合成一个完整的单元测试脚本 `test_coefficient.py`（示例代码，建议放仓库根目录以便 `from module import Coefficient` 能解析，跑完删除或加入个人忽略清单，不要提交），并补上规格要求的散点曲线：

```python
# 示例代码：Coefficient 单元测试（综合实践）
import torch
import matplotlib
matplotlib.use("Agg")                     # 无显示环境也能保存图片
import matplotlib.pyplot as plt
from module import Coefficient

torch.manual_seed(0)

# ---- 用例 1：输入维度拼接逻辑 ----
N = 5000
coef = Coefficient()
assert coef.net[0].in_features == 9
opacity = torch.rand(N, 1)
xyzt    = torch.randn(N, 4) * 2
scales  = torch.rand(N, 4) * 0.05
out = coef(opacity, xyzt, scales)
assert out.shape == (N, 1)

# ---- 用例 2：输出落在 (0,1) ----
assert out.min() > 0 and out.max() < 1

# ---- 用例 3：梯度回传非空 ----
loss = out.mean()
loss.backward()
assert all(p.grad is not None for p in coef.parameters())
assert any(p.grad.abs().sum() > 0 for p in coef.parameters())

# ---- 用例 4：衰减因子关于 opacity 的散点曲线 ----
coef.eval()                               # 关 Dropout，曲线才确定
f_min, f_max = 0.996, 0.998
with torch.no_grad():
    op = torch.linspace(1e-4, 1 - 1e-4, N).unsqueeze(1)   # 均匀扫过 (0,1)
    pos = xyzt[:N]                                        # 固定位置/尺度，只变 opacity
    sca = scales[:N]
    factor = f_min + (f_max - f_min) * coef(op, pos, sca)

plt.figure(figsize=(6, 4))
plt.scatter(op.squeeze(1).numpy(), factor.squeeze(1).numpy(), s=2, alpha=0.3)
plt.xlabel("opacity"); plt.ylabel("decay factor")
plt.ylim(f_min - 0.0005, f_max + 0.0005)
plt.title("Neural Decaying Function: factor vs opacity")
plt.savefig("coef_vs_opacity.png", dpi=150)

# ---- 用例 5：复利效应曲线 ----
import numpy as np
k = np.arange(0, 3000)
plt.figure(figsize=(6, 4))
for f, name in [(0.996, "f_min=0.996"), (0.998, "f_max=0.998")]:
    plt.plot(k, f ** k, label=name)
plt.xlabel("applied iterations k"); plt.ylabel("opacity scale f^k")
plt.legend(); plt.title("compounding of decay factor")
plt.savefig("compounding.png", dpi=150)
print("全部用例通过，图片已保存：coef_vs_opacity.png / compounding.png")
```

**任务清单与观察点**：

1. 运行 `python test_coefficient.py`，确认全部断言通过（待本地验证）。
2. 打开 `coef_vs_opacity.png`：散点是否呈现清晰的函数带？由于位置/尺度也在变，点会围绕「opacity 主趋势」形成一条有宽度的带——带宽本身就说明网络确实用了 4D 特征，而不是退化成 opacity 的一元函数。对比实验：用 `Coefficient(opacity_only=True)` 重画一次，带宽应消失。
3. 打开 `compounding.png`：验证 4.3.2 节的手算数值（\(0.998^{500}\approx0.37\)、\(0.996^{2000}\approx0.018\)）。
4. 把 `coef.eval()` 改回训练模式再画一次散点，观察 Dropout 造成的随机抖动，体会「衰减因子在训练中是随机过程」这一事实。
5. 写一段 200 字结论：解释「为什么输出区间只有 0.002 宽的网络能主导几十万高斯的生死」——答案应落在「复利 × 逐迭代执行 × 梯度联合优化」三点上。

## 6. 本讲小结

- `Coefficient` 是 Neural Decaying Function 的实现：`Linear(9,32) → ReLU → Dropout(0.1) → Linear(32,1) → Sigmoid`，全仓库仅 353 个可训练参数，在 `train.py` 中当 `opacity_decay` 开启时构造。
- 输入 9 维的账目：opacity 1 + `get_xyzt` 4（xyz + 时间中心 `_t`）+ `get_scaling_xyzt` 4（三个空间尺度 + 时间尺度 `_scaling_t`）；源码注释的「7」是 3D 语义底账，「+2」是第四维增量，`mode='mlp'` 只传 opacity，需 `opacity_only=True` 构造才可运行。
- `forward` 三段预处理：opacity 重映射到 \([-1,1]\)；位置按当前全部高斯的批统计做 z-score（相对坐标、非平稳、可导）；尺度先 `log` 还原 log 空间再 z-score。
- Sigmoid 输出 \((0,1)\) 被调用方线性映射到 \([0.996, 0.998]\)：单步最多衰减 0.4%，但从第 500 次迭代起逐迭代连乘，复利效应（\(0.998^{2000}\approx0.018\)）使其足以软删除高斯。
- 网络有独立优化器 `coef_optimizer`（Adam，lr=1e-5、weight_decay=1e-4），与高斯优化器在每个迭代末交替 step——这是「联合优化」的机制落点。

## 7. 下一步学习建议

下一讲 **u6-l2《opacity_decay：从常数衰减到神经网络衰减》**将把视角从网络本身拉到调用方：精读 `scene/gaussian_model.py` 的 `opacity_decay` 模式族（`const`、`exp_asc/exp_desc`、`power_asc/power_desc`、`mlp`、`net`），对比各模式的函数形状，并深入本讲只点到为止的两个细节——`mask` 的选择性衰减，以及 `_opacity.data = inverse_opacity_activation(...)` 原位写回如何绕过 autograd。建议先带着综合实践里画出的散点曲线去读，你会看到 `net` 模式只是这张曲线族的「可学习版本」。随后 u6-l3 讲衰减接入渲染的 `time_aware` 可见性掩码，u6-l4 讲双优化器的训练策略联动。
