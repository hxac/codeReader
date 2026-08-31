# opacity_decay：从常数衰减到神经网络衰减

## 1. 本讲目标

上一讲（u6-l1）我们精读了 Neural Decaying Function 的网络本体 `Coefficient`。本讲把镜头拉远，看它的「调用方」——`GaussianModel.opacity_decay` 这个不到 40 行的函数。读完本讲你应该能够：

1. 说出 `opacity_decay` 的七种模式（`const`、`exp_asc`、`exp_desc`、`power_asc`、`power_desc`、`mlp`、`net`）各自的 `factor(opacity)` 函数形状，并解释 `asc`/`desc` 家族保护谁、加速清除谁。
2. 解释 `f_min + (f_max - f_min) · σ(·)` 这一线性映射如何把 Sigmoid 输出压进 `[0.996, 0.998]` 的窄带，以及逐迭代连乘产生的「复利式软删除」效应。
3. 说明 `mode='net'` 与 `mode='mlp'` 在 `Coefficient` 输入上的差异（9 维 vs 1 维）。
4. 读懂 `mask` 的选择性衰减语义，并指出 `mask=None` 时代码实际行为与直觉相反这一细节。
5. 解释为什么写回用的是 `self._opacity.data = ...` 而不是重新绑定 Parameter——即绕过 autograd 的三点工程意图。

## 2. 前置知识

- **裸值存储 + 读时激活**（u3-l1）：`GaussianModel` 的可优化属性存的是无约束裸值，读取时才过激活函数。对不透明度而言，`_opacity` 存裸值，`get_opacity = sigmoid(_opacity)` 才是渲染用的有效值（(0,1) 区间）。`inverse_opacity_activation`（即 `inverse_sigmoid`，`log(x/(1-x))`）是它的反函数。
- **第四维属性**（u3-l2）：4D 高斯除了 xyz 还有时间中心 `_t` 与时间尺度 `_scaling_t`；`get_xyzt` 拼出 (N,4) 时空位置，`get_scaling_xyzt` 拼出 (N,4) 时空尺度。它们是 `mode='net'` 的额外输入。
- **Coefficient 网络**（u6-l1）：两层 MLP（Linear→ReLU→Dropout→Linear→Sigmoid），输出 (0,1)。`opacity_only=True` 时输入仅 1 维（不透明度），否则 9 维（不透明度 1 + xyzt 位置 4 + xyzt 尺度 4）。
- **autograd 基础**：PyTorch 中对叶子张量（`requires_grad=True` 的 `nn.Parameter`）做原地索引赋值会报错；对 `param.data` 赋值则完全绕过 autograd 图。本讲 4.4 会展开。
- **`torch.where(mask, a, b)`**：逐元素选择器，`mask` 为 True 取 `a`、False 取 `b`，三者形状必须可广播。

## 3. 本讲源码地图

| 文件 | 关键位置 | 作用 |
|---|---|---|
| [scene/gaussian_model.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L584-L620) | `opacity_decay`（L584-620） | 本讲主角：七种衰减模式 + mask 选择 + `.data` 写回 |
| [scene/gaussian_model.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L58-L59) | `setup_functions`（L58-59） | 登记激活函数对：`sigmoid` / `inverse_sigmoid` |
| [scene/gaussian_model.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L201-L203) | `get_scaling_xyzt` / `get_xyzt`（L201-223） | `mode='net'` 的 4 维位置与 4 维尺度输入 |
| [module/__init__.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/module/__init__.py#L4-L48) | `Coefficient`（L4-48） | 衰减因子网络，`forward` 三段预处理 |
| [gaussian_renderer/__init__.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L63-L75) | 衰减接入段（L63-75） | 全仓库唯一的 `opacity_decay` 调用点 |
| [utils/general_utils.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py#L19-L20) | `inverse_sigmoid`（L19-20） | `log(x/(1-x))`，写回裸值空间的桥梁 |
| [train.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L411-L419) | CLI 参数（L411-419） | `--f_min 0.996`、`--f_max 0.998`、`--decay_from_iter 500` |

先给出一句话总纲：**`opacity_decay` 做的事是「把每个高斯的有效不透明度乘上一个落在 `[f_min, f_max]` 内的因子，把结果写回裸值参数，并把衰减后的不透明度返回给渲染器」**。七种模式只是「因子怎么算」的七种答案。

## 4. 核心概念与源码讲解

### 4.1 模式族全景：factor(opacity) 的函数形状

#### 4.1.1 概念说明

把衰减统一写成：

\[
o_{\text{new}} \;=\; o \cdot \underbrace{\bigl[f_{\min} + (f_{\max}-f_{\min})\, g(o)\bigr]}_{\text{factor}(o)}
\]

其中 \(o\) 是当前有效不透明度（sigmoid 后，取值 (0,1)），\(g(o)\) 是每种模式自己的「归一化整形函数」。固定模式（const / exp / power 家族）的 \(g\) 都满足 \(g(o)\in[0,1]\)，因此 factor 被严格限制在 \([f_{\min}, f_{\max}]\) 内——**衰减永远不会把不透明度变大，也不会一次乘到 0**。这一点是「软删除」而非「硬剪枝」的关键。

七种模式的 \(g\) 一览：

| mode | \(g(o)\) | \(g(0)\) | \(g(1)\) | 曲线形状 | 衰减倾向 |
|---|---|---|---|---|---|
| `const` | \(0\)（factor 恒为 \(f_{\min}\)） | — | — | 水平线 | 一视同仁 |
| `exp_asc` (p≠0) | \(\frac{1-e^{po}}{1-e^{p}}\) | 0 | 1 | 单调升、凸 | 低不透明度衰减更狠 |
| `exp_desc` (p≠0) | \(\frac{e^{-po}-e^{-p}}{1-e^{-p}}\) | 1 | 0 | 单调降、凸 | 高不透明度衰减更狠 |
| `power_asc` | \(o^{p}\) | 0 | 1 | 单调升（p>1 时凸） | 低不透明度衰减更狠 |
| `power_desc` | \((1-o)^{p}\) | 1 | 0 | 单调降 | 高不透明度衰减更狠 |
| `mlp` | 网络输出 \(\sigma(\cdot)\)，输入仅 \(o\) | 学习 | 学习 | 数据驱动 | 网络决定 |
| `net` | 网络输出 \(\sigma(\cdot)\)，输入 9 维 | 学习 | 学习 | 数据驱动 | 网络决定 |

「asc / desc」指的是 factor 随 opacity 的单调方向。`asc` 家族编码的先验是：**不透明度低 = 重建信心低的高斯，应当更快被衰减清除**；`desc` 家族则相反。论文最终采用的是 `net`——把这个选择交给一个从数据中学习的小网络。

另外注意 `exp_asc` 与 `exp_desc` 在中点处交汇：\(g(0.5)=\frac{1}{1+e}\approx 0.269\)（两者相等）；`power_asc` 与 `power_desc` 同样对称，\(g(0.5)=0.25\)。

#### 4.1.2 核心流程

```text
opacity_decay(f_min, mode, p, f_max, mask)
  ├─ old_opacity = sigmoid(_opacity)          # (N,1)，读时激活
  ├─ 按 mode 计算 curr_opacity = old_opacity × factor(old_opacity)
  │    ├─ const      : factor = f_min
  │    ├─ exp_asc    : factor = f_min + (f_max-f_min)·(1-e^{po})/(1-e^p)   # p=0 退化为线性
  │    ├─ exp_desc   : factor = f_min + (f_max-f_min)·(e^{-po}-e^{-p})/(1-e^{-p})
  │    ├─ mlp        : factor = f_min + (f_max-f_min)·Coefficient(o)        # 1 维输入
  │    ├─ net        : factor = f_min + (f_max-f_min)·Coefficient(o, xyzt, s_xyzt)  # 9 维输入
  │    ├─ power_desc : factor = f_min + (f_max-f_min)·(1-o)^p               # 要求 p>0
  │    ├─ power_asc  : factor = f_min + (f_max-f_min)·o^p                   # 要求 p>0
  │    └─ 其它       : raise NotImplementedError
  ├─ opacity = old_opacity；若 mask 非空则 opacity = where(mask, curr, old)
  ├─ _opacity.data = inverse_sigmoid(opacity)   # 写回裸值空间（见 4.4）
  └─ return opacity                              # 衰减后的有效不透明度，交给渲染器
```

以默认参数 \(f_{\min}=0.996,\ f_{\max}=0.998,\ p=2\) 代入几个采样点（factor = 0.996 + 0.002·g）：

| \(o\) | const | exp_asc | exp_desc | power_asc | power_desc |
|---|---|---|---|---|---|
| 0.0 | 0.996 | 0.99600 | 0.99800 | 0.99600 | 0.99800 |
| 0.5 | 0.996 | 0.99654 | 0.99654 | 0.99650 | 0.99650 |
| 1.0 | 0.996 | 0.99800 | 0.99600 | 0.99800 | 0.99600 |

区间只有 0.002 宽，但这个区间会逐迭代连乘（见 4.2），差别会被指数放大。

#### 4.1.3 源码精读

函数签名与模式分发：

[scene/gaussian_model.py:584-587](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L584-L587) —— 签名：默认 `f_min=0.99, mode='net', p=2, f_max=1.0, mask=None`；`const` 模式直接把 factor 取为常数 `f_min`，与不透明度取值无关。

[scene/gaussian_model.py:588-597](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L588-L597) —— `exp_asc` / `exp_desc` 两支。注意每支内部都特判了 `p == 0`：此时通式会变成 0/0，于是退化到线性插值 \(g(o)=o\)（asc）或 \(g(o)=1-o\)（desc）。

[scene/gaussian_model.py:606-613](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L606-L613) —— `power_desc` / `power_asc` 两支，各自 `assert p > 0`（`power` 家族在 p≤0 时无定义或发散）；末尾的 `else: raise NotImplementedError` 是整个函数的输入检查兜底——传入未知模式名会立刻报错而不是静默跳过。

`old_opacity` 的来源是读时激活的 property：

[scene/gaussian_model.py:231-233](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L231-L233) —— `get_opacity` 返回 `sigmoid(_opacity)`，即 (N,1) 的有效不透明度；整条衰减链都在「激活后的空间」里做乘法，最后才经 `inverse_sigmoid` 写回裸值空间。

还要澄清一个「可达性」事实：全仓库唯一的调用点只传了 `f_min`、`f_max`、`mask` 三个关键字参数（见 4.3.3 的渲染器代码），因此 **`mode` 恒为默认值 `'net'`、`p` 恒为 2，CLI 上没有任何开关能改它们**。其余六种模式是作者内置的「消融实验工具箱」，只能通过改代码启用——这正是本讲实践的切入点。

#### 4.1.4 代码实践

**实践：画出五种固定模式的 factor(opacity) 曲线**

1. **实践目标**：直观掌握七种模式中五种固定模式的曲线形状与端点行为，确认 factor 全部落在 `[f_min, f_max]` 窄带内。
2. **操作步骤**：新建 `plot_decay_modes.py`（放在仓库外任意目录均可，不需要 GPU、不需要安装 CUDA 扩展，只需 numpy 与 matplotlib），内容如下（**示例代码**，公式逐条翻译自源码）：

   ```python
   import numpy as np
   import matplotlib.pyplot as plt

   f_min, f_max, p = 0.996, 0.998, 2
   o = np.linspace(0, 1, 400)

   g = {
       'const':      np.zeros_like(o),                       # factor 恒为 f_min
       'exp_asc':    (1 - np.exp(p*o)) / (1 - np.exp(p)),
       'exp_desc':   (np.exp(-p*o) - np.exp(-p)) / (1 - np.exp(-p)),
       'power_asc':  o**p,
       'power_desc': (1 - o)**p,
   }

   plt.figure(figsize=(6, 4))
   for name, gi in g.items():
       factor = f_min + (f_max - f_min) * gi if name != 'const' \
                else np.full_like(o, f_min)
       plt.plot(o, factor, label=name)
   plt.xlabel('opacity'); plt.ylabel('factor'); plt.legend(); plt.grid(True)
   # 窄带只有 0.002 宽，直接画会挤成一条线，建议放大纵轴：
   plt.ylim(f_min - 0.0005, f_max + 0.0005)
   plt.savefig('decay_modes.png', dpi=150)
   ```

   若嫌纵轴太窄看不清，可临时把 `f_min, f_max = 0.0, 1.0` 再画一版看 \(g(o)\) 本身的形状。
3. **需要观察的现象**：`exp_asc` 与 `power_asc` 从 0.996 单调升到 0.998；`exp_desc` 与 `power_desc` 反向；两对曲线各自关于中点近似对称；`exp` 家族前段比 `power` 家族更陡（`exp_asc` 在 \(o\to 0\) 处斜率约 \(2/(e^2-1)\approx 0.31\)，而 \(o^2\) 在 0 处斜率为 0）。
4. **预期结果**：五条曲线全部落在 [0.996, 0.998] 内；与 4.1.1 表格的采样点数值一致。图形渲染结果**待本地验证**（本讲义没有替你运行过该脚本）。

**追加实践：实现一个自定义模式**

在你的本地副本的 `opacity_decay` 中（学习用途的实验，不建议提交）加一个按对数加权的平滑升模式，并让它通过现有的输入检查约定：

```python
# 示例代码：加在 power_asc 分支之后。g(o) = log(1+p·o)/log(1+p)，
# g(0)=0、g(1)=1、凹函数——前段比 power_asc 更陡，对低不透明度高斯衰减更激进。
elif mode == 'log_asc':
    assert p > 0, "p should be greater than 0"
    curr_opacity = old_opacity * (f_min + (f_max - f_min)
                                  * torch.log1p(p * old_opacity) / math.log1p(p))
```

「通过输入检查」指两点：`assert p > 0` 与函数末尾的 `NotImplementedError` 兜底——传错模式名或非法 p 应当立刻报错。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `exp_asc` 在 `p == 0` 时必须特判？特判后退化成什么？
**答案**：\(p=0\) 时分子分母同时为 0（\(1-e^{0}=0\)），通式变成 0/0；特判后退化为线性插值 \(g(o)=o\)，即 factor 线性地从 \(f_{\min}\) 升到 \(f_{\max}\)。这也说明线性模式被隐含在 exp 家族里，没有单独的 `linear` 分支。

**练习 2**：`mode='desc'` 家族对高不透明度的高斯衰减更强，这和 `asc` 家族的先验正好相反。结合 4DGS 训练动态，哪种先验更合理？为什么论文最终选了 `net`？
**答案**：`asc` 更符合直觉——低不透明度意味着该高斯对重建贡献小、几何不可信，应加速清除；`desc` 会优先削弱那些已经学得很自信的高斯，容易破坏已收敛区域。但固定形状毕竟是手工先验：真实场景中「该不该衰减」取决于高斯的时空位置与尺度（例如运动区域的时间拉长高斯），因此 `net` 让一个以 9 维时空状态为输入的网络从重建损失中学习每个高斯该落在 \([f_{\min}, f_{\max}]\) 的哪个位置，比任何固定曲线都更自适应。

**练习 3**：把 `f_min` 设成 0.5、`f_max` 设成 1.5 会发生什么？
**答案**：代码不会报错——线性映射本身允许任何实数。但 factor 会超过 1，衰减变成「增强」，且幅度远超软删除的设计范围，训练大概率崩溃于不透明度震荡。`[0.996, 0.998]` 这个默认窄带是论文调出来的安全护栏，函数签名里的默认 `f_min=0.99, f_max=1.0` 只在不传参时生效，而渲染器调用总是显式传入 CLI 值。

### 4.2 线性映射、窄带与复利效应：`mlp` 与 `net`

#### 4.2.1 概念说明

`mlp` 与 `net` 两支的结构完全相同，唯一区别是喂给 `Coefficient` 的输入：

- `mlp`：只喂 `old_opacity`，对应 `Coefficient(opacity_only=True)` 构造出的 1 维输入网络。它等价于「一个可学习的 `factor(opacity)` 曲线」——把固定模式的手工曲线换成神经网络拟合的曲线。
- `net`：喂 `old_opacity` + `get_xyzt`（时空位置 4 维）+ `get_scaling_xyzt`（时空尺度 4 维），共 9 维。factor 不再只看当前不透明度，而是看每个高斯的**完整时空状态**——这正是论文 Neural Decaying Function 的形态。

两支共用同一个「安全带」：`f_min + (f_max - f_min) · 网络输出`。`Coefficient` 末端的 Sigmoid 输出 \(\sigma\in(0,1)\)，经这层线性映射后：

\[
\text{factor} = f_{\min} + (f_{\max}-f_{\min})\,\sigma \;\in\; (f_{\min},\, f_{\max}) = (0.996,\, 0.998)
\]

网络学到的只是「每个高斯在这条窄带里的相对位置」——它可以让某个高斯衰减得快一点（输出趋 0，factor≈0.996）或慢一点（输出趋 1，factor≈0.998），但**永远不能不衰减，更不能放大**。

窄到 0.002 的单次因子如何产生实质影响？答案是**复利**。设某高斯每次被衰减都乘同一因子 \(f\)，且忽略重建梯度的反作用，则 \(T\) 次后 \(o_T = o_0 f^{T}\)：

\[
T_{\text{半衰}} = \frac{\ln 2}{-\ln f} \approx \begin{cases} 173 \text{ 次} & f = 0.996 \\ 346 \text{ 次} & f = 0.998 \end{cases}
\]

从初始不透明度 0.1 衰减到剪枝阈值 0.005（即 `densify_and_prune` 的 `min_opacity`，见 [scene/gaussian_model.py:761](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L761)）：

\[
T_{\text{到剪枝}} = \frac{\ln(0.05)}{\ln f} \approx \begin{cases} 747 \text{ 次} & f = 0.996 \\ 1496 \text{ 次} & f = 0.998 \end{cases}
\]

也就是说：一个持续可见、却始终得不到重建梯度「救援」的高斯，会在几百到一千多次迭代内被连乘压到剪枝线以下——这就是「软删除」。而真正有用的高斯，其 `_opacity` 会不断收到指向更大不透明度的梯度，与衰减压力形成动态平衡。`net` 模式下网络本身也在被联合优化（u6-l1 已讲 `coef_optimizer`），它学到的就是「在哪里施加压力、在哪里松手」。

#### 4.2.2 核心流程

```text
mode='mlp':  factor = f_min + (f_max-f_min) · Coefficient(o)                       # 1 维输入
mode='net':  factor = f_min + (f_max-f_min) · Coefficient(o, get_xyzt, get_scaling_xyzt)  # 9 维输入
             └─ Coefficient.forward 内部：
                 o'  = 2o - 1                                # 重映射到 (-1,1)
                 pos = (xyzt - mean) / std                   # 按全体高斯的批统计 z-score
                 sca = zscore(log(scales + 1e-6))            # 先取 log 再 z-score
                 σ   = Sigmoid(Linear₃₂→ReLU→Dropout→Linear₁)
```

注意 `mlp` 分支若在 `Coefficient` 缺省构造（`opacity_only=False`）下调用，`forward` 会走到需要 `positions` 的路径而报 `TypeError`——所以 `mlp` 模式与 `opacity_only=True` 是绑定的配对，`net` 模式与缺省构造配对。两支开头都检查 `self.coefficient is None`，未构造网络就调用会得到清晰的 `ValueError`。

#### 4.2.3 源码精读

[scene/gaussian_model.py:598-605](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L598-L605) —— `mlp` 与 `net` 两支：先做 `coefficient is None` 检查，再套同一层线性映射；`net` 比 `mlp` 多传了 `self.get_xyzt` 与 `self.get_scaling_xyzt`。注释 `# [f_min, f_max]` 标出的就是这层映射的含义。

[scene/gaussian_model.py:216-223](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L216-L223) —— `net` 模式的位置输入：`get_t` 直接返回裸值 `_t`，`get_xyzt` 把 `_xyz` 与 `_t` 在维度 1 上拼成 (N,4)。

[scene/gaussian_model.py:197-203](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L197-L203) —— 尺度输入：`get_scaling_xyzt` 先拼接 `_scaling` 与 `_scaling_t` 再整体过 `exp`，得到激活后的 (N,4) 时空尺度（`Coefficient.forward` 内部会再取一次 log 抵消）。

[module/__init__.py:33-48](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/module/__init__.py#L33-L48) —— `Coefficient.forward` 的三段预处理：不透明度重映射、位置 z-score、尺度 log+z-score（u6-l1 已逐行精读，本讲只需记住「进入网络前所有输入都被压到可比拟的数值范围」，否则 32 维隐藏层会被大数值位置主导）。

[scene/gaussian_model.py:501-503](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L501-L503) —— `training_setup` 中为 `coefficient` 单独创建的 `coef_optimizer`（Adam，带 `coefficient_lr` 与 `weight_decay`）：联合优化的「另一条腿」，与本讲的 factor 计算共同构成 Neural Decaying Function 的完整闭环。

[scene/gaussian_model.py:112-114](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L112-L114) 与 [scene/gaussian_model.py:130-138](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L130-L138) —— 检查点协议里 `coef_optimizer.state_dict()` 与 `coefficient.state_dict()` 各占一席（4D 元组的索引 12 与 20），衰减网络因此可以随训练一起保存与恢复。

#### 4.2.4 代码实践

**实践：CPU 上验证 `Coefficient` 输出区间与梯度连通性**

`module/__init__.py` 只依赖 torch，不需要任何 CUDA 扩展，可以在 CPU 上直接实验。

1. **实践目标**：确认 `net` 模式输入拼接正确、Sigmoid 输出落在 (0,1)、factor 落在 (f_min, f_max)，且损失反传后网络参数拿到梯度。
2. **操作步骤**（**示例代码**）：

   ```python
   import torch
   from module import Coefficient          # 只依赖 torch，CPU 即可

   torch.manual_seed(0)
   coef = Coefficient()                    # 缺省：9 维输入（opacity_only=False）
   N = 5000
   opacity = torch.rand(N, 1)              # 模拟 sigmoid 后的有效不透明度
   xyzt    = torch.randn(N, 4) * 2         # 模拟 get_xyzt
   scales  = torch.rand(N, 4) * 0.1 + 0.01 # 模拟 get_scaling_xyzt（全正数，可取 log）

   f_min, f_max = 0.996, 0.998
   sigma = coef(opacity, xyzt, scales)
   factor = f_min + (f_max - f_min) * sigma

   loss = (opacity * factor).sum()         # 模拟"衰减后不透明度参与渲染损失"
   loss.backward()

   print(sigma.shape, sigma.min().item(), sigma.max().item())          # (5000,1) 且在 (0,1)
   print(factor.min().item(), factor.max().item())                     # 应在 (0.996, 0.998)
   print(coef.net[0].weight.grad is not None,
         coef.net[0].weight.grad.abs().sum().item() > 0)               # 梯度非空且非零
   ```

3. **需要观察的现象**：`sigma` 严格落在 (0,1)（Sigmoid 的开区间）；`factor` 落在 (0.996, 0.998) 内，永远碰不到端点；第一层权重梯度非零——证明梯度能从「衰减后的不透明度」流回网络。
4. **预期结果**：三个断言全部成立。数值结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：既然 `f_min`/`f_max` 已经把 factor 限死在窄带里，网络还有自由度吗？它的「决策空间」到底是什么？
**答案**：有。网络决定的是每个高斯在窄带内的**相对衰减速率**。0.996 与 0.998 对应的半衰期分别是约 173 次与 346 次衰减——差了一倍。在数万次迭代的尺度上，这个相对差异足以决定一个高斯是被软删除还是被保留，所以「窄」不等于「没有影响力」。

**练习 2**：为什么 `Coefficient.forward` 里对位置做 z-score、对尺度先取 log？如果不做会怎样？
**答案**：位置的世界坐标量级可能是米级（若干单位），而重映射后的不透明度只有 (-1,1)，第一层 Linear 的加权和会被位置项主导，网络实际上退化为只看位置的函数。z-score 把三路输入都压到单位量级；尺度本身是偏态分布的正数，取 log 后更接近对称分布再标准化，避免少数巨大高斯拉爆统计量。注意这里用的是**当前全体高斯的批统计**（而非固定常数），所以分布会随训练漂移。

**练习 3**：`mode='mlp'` 与固定模式相比有什么本质优势？它又比 `net` 缺了什么？
**答案**：`mlp` 把 `factor(opacity)` 从手工设计的几条曲线换成可学习的单调性自由曲线，是对 `exp_asc`/`power_asc` 这类先验的数据驱动替代，天然构成消融实验的中间档。但它仍然只看当前不透明度这一个标量——两个不透明度相同的高斯必然得到相同因子；`net` 额外看时空位置与尺度，能区分「同一不透明度、但位于运动区域 vs 静态背景」的高斯，这才是 4D 场景下衰减真正需要的判别力。

### 4.3 mask 的选择性衰减与调用上下文

#### 4.3.1 概念说明

`opacity_decay` 的最后一个参数 `mask` 是一个 (N,1) 的布尔张量，语义是**「这次迭代只衰减 mask 为 True 的高斯」**。它在渲染器里由两个可见性判据取交集构成：

- **空间可见性**：`rasterizer.markVisible(means3D)`——CUDA 侧的视锥粗筛（u4-l2 讲过：判据为近裁剪面深度）；
- **时间可见性**：`get_marginal_t(timestamp) > 0.05`——渲染时刻附近的一维时间高斯衰减（u3-l3 讲过：约 ±2.45 个时间标准差以内才算「此刻在场」）。

为什么只衰减「当前视角真正看得见」的高斯？因为衰减是一种单向压力：看不见的高斯不会出现在本次渲染中，也就**收不到任何重建梯度来对抗衰减**。若对它们也施加衰减，它们的不透明度只降不升，等于无条件处刑——连改正的机会都没有。而可见的高斯至少同时在接受「重建需要它」的梯度反馈，衰减与梯度形成公平竞争：赢的高斯留下，输的被软删除。

于是掩码 + 复利共同定义了 Neural Decaying Function 的实际作用对象：**在多个视角、多个时刻上都反复可见、却始终得不到重建梯度支持的高斯**——典型例子就是浮在半空、遮挡关系错误的「Floater」。

#### 4.3.2 核心流程

```text
render() 内（每个视角调用一次）：
  if 开启 opacity_decay 且 iteration > decay_from_iter:
      if time_aware:
          space_visibility = markVisible(means3D)                     # (N,) bool
          time_visibility  = get_marginal_t(timestamp)[:,0] > 0.05    # (N,) bool
          visibility = (space & time).view(-1, 1)                     # (N,1) bool
      else:
          visibility = None
      opacity = pc.opacity_decay(f_min, f_max, mask=visibility)
  # opacity 随后作为光栅化器的输入
```

两个要点：

1. `decay_from_iter`（默认 500）是延迟启动阀：前 500 次迭代完全不衰减，让高斯先完成初始的几何粗定位，避免在「大家都还是 0.1 不透明度」的懵懂期就开始互相倾轧。
2. `mask` 形状是 (N,1)：渲染器里特意做了 `visibility.view(-1, 1)`，因为 `old_opacity` 是 (N,1)，`torch.where` 要求三者形状一致。

#### 4.3.3 源码精读

[gaussian_renderer/__init__.py:63-75](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L63-L75) —— 全仓库唯一的调用点：三重门（`args` 非空、`opacity_decay` 开启、`iteration > decay_from_iter`）→ `time_aware` 分支构造可见性交集 → 调用 `opacity_decay(f_min=args.f_min, f_max=args.f_max, mask=visibility)`，返回值直接覆盖第 61 行取到的 `opacity = pc.get_opacity`，作为本次渲染的不透明度输入。注意这里**没有传 `mode` 与 `p`**——所以管线里跑的永远是 `net` 模式。

[scene/gaussian_model.py:615-620](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L615-L620) —— mask 的消费点：`torch.where(mask, curr_opacity, old_opacity)` 逐元素选择；随后写回并返回。

**必须指出的一个反直觉细节**：细读上面四行——`opacity = old_opacity` 先被赋为**未衰减值**，只有 `mask is not None` 时才被 `where` 覆盖。因此当 `mask=None`（即 `time_aware=False`）时：

- 返回值是 `old_opacity`，没有衰减；
- 写回的是 `inverse_sigmoid(sigmoid(_opacity))`，解析上是恒等变换。

也就是说，**在当前实现里 `mask=None` 意味着衰减完全不生效，而不是「对全部高斯衰减」**。这与「不传 mask 就全体衰减」的常规 API 直觉相反，更像是实现上的疏漏（自然的写法应是 `opacity = curr_opacity` 起步）。它带来一个隐蔽的后果：`--time_aware` 关闭后（该开关默认 True），`--opacity_decay` 会静默失效。作为练习，读者可以在本地把初始行改成 `opacity = curr_opacity` 再验证行为差异（见 4.3.4）。此外还有个数值边角：若某高斯裸值已饱和（`sigmoid` 在 float32 下精确等于 1.0），`inverse_sigmoid(1.0) = log(1/0) = inf`——即便走 `mask=None` 的「恒等」路径也可能写出 inf，**待本地验证**。

关于调用频率：`render()` 每个视角调用一次，而 batch 训练的内层循环逐视角渲染（u5-l1/u5-l3），所以 **`batch_size=B` 时一次迭代会对不透明度施加 B 次衰减**，复利速度随 batch_size 加快——这是把 batch 内「每个视角各自处刑自己看得见的高斯」语义保持一致的自然结果。

#### 4.3.4 代码实践

**实践：用小张量复现 mask 语义，并对照 `mask=None` 的行为**

1. **实践目标**：验证 `torch.where` 的选择性；亲手确认「`mask=None` 时函数是 no-op」这一源码事实。
2. **操作步骤**（**示例代码**，纯 CPU，直接翻译源码语义而不导入 gaussian_model——后者依赖 CUDA 扩展才能 import）：

   ```python
   import torch

   old_opacity = torch.tensor([[0.10], [0.50], [0.90]])   # 3 个高斯的有效不透明度
   factor      = torch.tensor([[0.996], [0.996], [0.996]])# 假设 net 输出恒为 0
   curr_opacity = old_opacity * factor

   # ① 传入 mask：只衰减第 0、2 个高斯
   mask = torch.tensor([[True], [False], [True]])
   out_masked = torch.where(mask, curr_opacity, old_opacity)
   print(out_masked)   # [[0.0996],[0.5000],[0.8964]]

   # ② 模拟 mask=None 分支（照抄源码 L615-617 的控制流）
   opacity = old_opacity
   if mask is not None:
       opacity = torch.where(mask, curr_opacity, old_opacity)
   none_mask = None
   opacity2 = old_opacity
   if none_mask is not None:
       opacity2 = torch.where(none_mask, curr_opacity, old_opacity)
   print(opacity2)     # [[0.10],[0.50],[0.90]] —— 未衰减
   ```

3. **需要观察的现象**：①中被 mask 选中的两个高斯乘了 0.996、未选中的原样保留；②模拟的 `mask=None` 路径返回值与输入完全相同。
4. **预期结果**：与打印注释一致。进阶：在你的本地副本把 L615 改为 `opacity = curr_opacity`，重跑②，观察 `mask=None` 变成全体衰减——这正是「修复 no-op」的最小改动。渲染端行为**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`mask` 为什么要与「时间可见性」取交集，而不是只用空间视锥可见性？
**答案**：4D 高斯在时间轴上有分布。一个高斯可能在当前视角的视锥内（空间可见），但其时间中心远离渲染时刻（`marginal_t` 很小），此刻它根本没有参与成像——对它衰减同样是没有对抗压力的单向处刑。取交集保证「衰减的对象恰好是此刻真正影响这张图的高斯」，它们也必然同时在接受这张图的重建梯度，竞争才公平。

**练习 2**：假设某高斯在连续 1000 次渲染中每次都满足 mask、且 factor 恒为 0.996，初始不透明度 0.1。它会到达剪枝阈值 0.005 吗？
**答案**：纯连乘下 \(0.1 \times 0.996^{1000} \approx 0.1 \times e^{-4.008} \approx 0.0018\)，已经低于 0.005。但实际不会这么快：渲染损失对可见高斯的 `_opacity` 也在施加反向梯度（把它往「图里需要更亮」的方向推），两者是动态拔河。上面的计算给出的是「无救援时的上界速度」。

**练习 3**：为什么 `decay_from_iter` 默认是 500 而不是 0？
**答案**：训练初期所有高斯共享 0.1 的初始不透明度（u3-l1），此时「不透明度低」不包含任何「不可信」信息；且几何尚未粗定位，衰减会随机处刑大量其实有用的高斯。延迟 500 次迭代让位置与尺度先收敛到一个粗合理的状态，衰减网络（`net` 模式）也能先观察到有意义的时空统计再开始决策。

### 4.4 `_opacity.data` 原位写回：绕过 autograd 的设计意图

#### 4.4.1 概念说明

函数最后三行是全讲最精妙的部分：

```python
self._opacity.data = self.inverse_opacity_activation(opacity)
```

这里同时发生两件事，必须分开看：

1. **返回值 `opacity` 携带梯度图**：在 `net`/`mlp` 模式下，`curr_opacity = old_opacity × (f_min + (f_max-f_min) · coefficient(...))`，其中 `old_opacity = sigmoid(self._opacity)` 与网络输出都在 autograd 图内。渲染器把这个返回值直接喂给光栅化器，损失反传时梯度沿两条路走：一条到 `_opacity`（穿过 sigmoid），一条到 `Coefficient` 的权重（穿过 factor）。**这是衰减网络获得梯度的唯一通路。**
2. **`.data` 写回不带梯度**：把衰减结果持久化进裸值参数时刻意切断了图。写回的目的不是求导，而是「状态更新」——让下一次迭代的 `sigmoid(_opacity)` 从衰减后的值出发，实现复利。

为什么持久化必须绕过 autograd？对比三种写法就清楚了：

| 写法 | 后果 |
|---|---|
| `self._opacity = inverse_sigmoid(opacity)`（直接重绑） | `_opacity` 变成非叶子计算结果；优化器 `state` 里还持有旧 Parameter 对象，`optimizer.step()` 更新的是旧对象，**新值静默失效** |
| `self._opacity[:] = inverse_sigmoid(opacity)`（索引原地写） | 对 `requires_grad=True` 的叶子做原地操作，直接 `RuntimeError` |
| `self._opacity.data = inverse_sigmoid(opacity)`（实际写法） | 替换底层存储、绕过 autograd、**Parameter 对象身份不变**——优化器键、Adam 动量形状全部原样保留 |

第三点尤其关键：本仓库的优化器状态以 Parameter 对象为键（u5-l4 的 `cat_tensors_to_optimizer`/`_prune_optimizer`/`replace_tensor_to_optimizer` 三件套全在处理这个问题）。`.data` 赋值保持了对象同一性，让「每迭代一次的状态压缩」与「优化器正常工作」互不干扰。这种「前向用带梯度的值、状态更新用无梯度的写」的手法，与 DQN 里 target 网络的软更新、BYOL 的 stop-gradient 属于同一家族——**把「会累积的计算」与「可微的通路」解耦**。

仓库里还有一个现成的对照：`reset_opacity`（继承自 3DGS）同样要做「inverse_sigmoid 后写回」，但它走的是 `replace_tensor_to_optimizer` 的重绑路线（[scene/gaussian_model.py:523-526](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L523-L526)）——把新张量包成新 Parameter 并迁移 Adam 状态。两条路线都能保持优化器一致，`.data` 写法更轻（形状不变、动量不清零），重绑写法更通用（`reset_opacity` 沿用了 3DGS 的基础设施）。

最后补一个时序细节：`old_opacity` 在函数开头就已计算，之后才发生 `.data` 写回，二者不互相污染；且写回的是**新分配**的张量（`inverse_sigmoid` 的结果），此前从旧存储派生的图节点仍指向旧值，反传安全。

#### 4.4.2 核心流程

```text
返回值通路（可微）：
  _opacity ─sigmoid→ old ─×factor(net含Coefficient)→ opacity ─→ 光栅化器 ─→ loss
      ↑                                                                │ backward
      └──────────────────── 梯度 ────────────────────────────────────┘
                                   （另一条分支进 Coefficient 权重 → coef_optimizer）

状态通路（不可微）：
  opacity ─inverse_sigmoid→ 裸值 ─赋给 _opacity.data→ 下一迭代的起点
```

两条通路共享同一次前向计算的结果，却在 autograd 层面完全隔离。

#### 4.4.3 源码精读

[scene/gaussian_model.py:619](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L619) —— 本讲核心一行：`.data` 赋值把激活空间的衰减结果经 `inverse_sigmoid` 折回裸值空间持久化，绕过 autograd、保持 Parameter 身份。

[scene/gaussian_model.py:58-59](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L58-L59) —— 激活函数对的登记处：`opacity_activation = torch.sigmoid`、`inverse_opacity_activation = inverse_sigmoid`，一对可配置的反函数，让「裸值 ⇄ 有效值」的往返有统一入口。

[utils/general_utils.py:19-20](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py#L19-L20) —— `inverse_sigmoid(x) = log(x/(1-x))`，即 logit 函数；与 sigmoid 严格互逆（解析上）。

[scene/gaussian_model.py:523-526](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L523-L526) —— 对照组 `reset_opacity`：clip 到 0.01 后同样 inverse_sigmoid 写回，但走 `replace_tensor_to_optimizer` 重绑路线（Adam 动量清零重建）。

[gaussian_renderer/__init__.py:74-75](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L74-L75) —— 返回值的消费点：`visible_opacities` 直接覆盖 `opacity` 变量进入光栅化器，即 4.4.2 图中「可微通路」的入口。

[train.py:254](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L254) —— 联动逻辑：`opacity_decay` 开启时 `opacity_reset_interval` 的定期重置被禁用——若一边软删除、一边定期把全体不透明度压回 0.01，两种机制会互相打架（u6-l4 展开）。

#### 4.4.4 代码实践

**实践：亲手复现 `.data` 写回，验证「梯度通路与状态通路隔离」**

1. **实践目标**：证明 (a) `.data` 赋值后 autograd 图照常反传；(b) 直接重绑会让优化器「失联」；(c) 写回确实让下一轮的有效不透明度变小。
2. **操作步骤**（**示例代码**，CPU 即可）：

   ```python
   import torch
   from torch import nn

   N = 4
   raw = nn.Parameter(torch.full((N, 1), 0.0))        # sigmoid(0)=0.5，模拟 _opacity
   opt = torch.optim.Adam([raw], lr=0.1)               # 优化器以 raw 这个对象为键

   old = torch.sigmoid(raw)                            # 有效不透明度，在图内
   decayed = old * 0.996                               # 模拟 factor=0.996 的衰减
   loss = decayed.sum()
   loss.backward()
   g1 = raw.grad.clone()

   # ① .data 写回（源码做法）
   raw.data = torch.log(decayed.detach() / (1 - decayed.detach()))
   print(torch.sigmoid(raw.detach()).min())            # ≈0.498，下一轮起点已被压低
   opt.zero_grad(); opt.step()                         # 优化器仍认得 raw，正常 step
   print(raw.grad is not None)                         # True：反传没被破坏

   # ② 对照：直接重绑（错误做法）
   raw2 = nn.Parameter(torch.zeros(N, 1))
   opt2 = torch.optim.Adam([raw2], lr=0.1)
   old2 = torch.sigmoid(raw2)
   raw2 = torch.log(old2 * 0.996 / (1 - old2 * 0.996)) # 重绑成非叶子张量
   opt2.step()                                         # step 更新的是 state 里的旧对象
   print(torch.equal(opt2.param_groups[0]['params'][0], raw2))  # False：已失联

   # ③ 对照：叶子原地索引赋值
   raw3 = nn.Parameter(torch.zeros(N, 1))
   try:
       raw3[0] = 1.0                                   # RuntimeError 预期
   except RuntimeError as e:
       print('in-place error:', str(e)[:60])
   ```

   注意①中我们对 `decayed.detach()` 取 logit——源码里不需要显式 detach，因为 `.data =` 赋值本身就等价于切断。
3. **需要观察的现象**：①写回后 sigmoid 值确实降到约 0.498，且 backward/step 均正常；②重绑后优化器参数表里的对象与新变量不相等；③抛出「a leaf Variable that requires grad is being used in an in-place operation」。
4. **预期结果**：三条全部命中，即证明 `.data` 写法是三种方案里唯一同时满足「持久化 + 不破坏优化器 + 不报错」的。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：如果把 L619 改成 `self._opacity = nn.Parameter(self.inverse_opacity_activation(opacity.detach()))`，训练会发生什么？
**答案**：短期内可能看不出来——新建的 Parameter 值是对的。但优化器 `state` 与 `param_groups` 里持有的还是旧 Parameter 对象，此后每一步 `optimizer.step()` 更新的都是孤儿对象，`_opacity` 再也不会被梯度更新（除非像 `reset_opacity` 那样用 `replace_tensor_to_optimizer` 同步迁移优化器状态）。这是一种典型的静默失效 bug。

**练习 2**：`.data` 写回没有清零 Adam 对 `_opacity` 的一阶/二阶动量，这有问题吗？
**答案**：基本没有。动量形状不变（高斯数量 N 未变），且衰减是小幅连续修改（每次 ≤0.4%），旧动量对新目标的偏差很小，Adam 的自适应机制会很快跟上。相比之下 `reset_opacity` 把全体不透明度猛压到 0.01，属于大幅跳变，沿用的重绑路线顺带清零动量反而更稳妥。可见两种写法各自匹配自己的修改幅度。

**练习 3**：衰减写回发生在渲染之前（L74 在光栅化器调用之前执行），同一次渲染中光栅化器拿到的不透明度与写回后的 `sigmoid(_opacity)` 一致吗？
**答案**：一致（对被 mask 选中的高斯而言）。因为返回值 `opacity` 与写回值满足 `opacity = sigmoid(_opacity_new)` 的关系——这正是「先算 curr、再 inverse 写回」这对反函数保证的。区别只在于：返回值在图内（可反传），写回值在图外（只作状态）。同一个数值，两种身份。

## 5. 综合实践

**任务：写一个 50 行的「衰减模拟器」，把本讲四个模块串起来。**

不用 GPU、不用数据集，纯 CPU 模拟一个 N 高斯系统在衰减机制下的不透明度演化：

1. **准备**：`o = 0.1 × ones(N)`；给每个高斯随机分配 xyzt 位置与尺度（供 `net` 模式用）；构造 `Coefficient()`（CPU 版）。
2. **每个模拟迭代**：
   - 以概率 0.7 为每个高斯抽一个可见性布尔值，组成 `mask`（模拟「视角 × 时刻」的可见性）；
   - 依次用 `const`、`exp_asc`、`net` 三种模式计算 `curr_opacity`（公式照抄源码，`net` 用真实 `Coefficient` 前向）；
   - 按 L615-619 的控制流做 `torch.where` + `.data` 写回（维护一个裸值张量即可）；
   - 模拟「有用高斯的梯度救援」：随机选 20% 的高斯，每轮把其有效不透明度乘 1.002（上限 1.0）——模拟重建梯度把有用高斯往上推；
   - 低于 0.005 的高斯记为「被剪枝」，从统计中移除。
3. **跑 3000 轮**，输出三条曲线：存活高斯数、平均不透明度、「救援组 vs 非救援组」的存活率对比。
4. **再跑两组对照**：`mask=None`（验证 4.3 的 no-op 结论——曲线应与不衰减完全重合）；把 L615 换成 `opacity = curr_opacity` 后重跑 `mask=None`（全体衰减，非救援组应迅速清零）。
5. **观察与思考**：救援组是否在三种模式下都存活？`net` 模式初期 factor 接近哪个端点（未训练的网络输出约 0.5，factor≈0.997，恰在窄带中央）？把观察写成 200 字结论。

这个模拟复现了本讲的全部核心机制：模式族（4.1）、窄带复利与救援平衡（4.2）、mask 选择性（4.3）、写回持久化（4.4）。预期现象：救援组存活率接近 100%，非救援组在几百轮内全部触底；`mask=None` 组与「不衰减」基准重合。**待本地验证**。

## 6. 本讲小结

- `opacity_decay` 统一形式为 \(o_{\text{new}} = o \cdot [f_{\min} + (f_{\max}-f_{\min})\,g(o)]\)：七种模式只是 \(g\) 的七种答案——固定模式（const/exp/power）是手工先验曲线，`mlp`/`net` 是数据驱动曲线；`asc` 家族加速清除低不透明度高斯，`desc` 相反，`const` 一视同仁。
- `f_min + (f_max-f_min)·σ` 把网络输出压进 `[0.996, 0.998]` 窄带：单次因子虽小，但逐迭代连乘形成复利——无救援的高斯约 747~1496 次衰减即触达 0.005 剪枝线，实现「软删除」。
- `net` 与 `mlp` 的差异在输入维度：`net` 喂 9 维（不透明度 + xyzt 位置 + xyzt 尺度），能区分同不透明度但时空状态不同的高斯；`mlp` 只喂 1 维。渲染器不传 `mode`，管线里恒为 `net`，其余六种模式只能改代码启用，是内置的消融工具箱。
- `mask` 是 (N,1) 的「空间可见 ∧ 时间可见」交集，保证只衰减当前视角真正成像、同时也在接收重建梯度的高斯——衰减与梯度公平竞争；注意源码中 `mask=None` 实际使衰减完全失效（含恒等写回），与直觉相反，疑似疏漏。
- `self._opacity.data = inverse_sigmoid(opacity)` 一行实现「双通路解耦」：返回值带 autograd 图进渲染器（`Coefficient` 获得梯度的唯一通路），`.data` 写回绕过 autograd 做状态持久化，同时保持 Parameter 对象身份不破坏优化器键——与直接重绑（优化器失联）、原地索引赋值（RuntimeError）相比是唯一正确解。
- 衰减自 `decay_from_iter`（默认 500）后启用，每个视角调用一次（batch_size=B 则每次迭代连乘 B 次），并与 `densify_until_iter=iterations`、禁用 `reset_opacity` 等训练策略联动（u6-l4 展开）。

## 7. 下一步学习建议

- **u6-l3《衰减如何接入渲染：time_aware 可见性掩码》**是本讲的直接续篇：深入 `markVisible` 的 CUDA 视锥判据、`get_marginal_t` 的时间衰减阈值 0.05 的由来，以及 `decay_from_iter` 延迟启用的消融设计。
- 之后读 **u6-l4《联合优化》**，看 `coef_optimizer` 与高斯优化器如何逐迭代交替 step、衰减开启后训练策略的四处联动，以及「梯度聚焦几何学习」的完整论证。
- 想动手做消融，回到本讲 4.1.4 的自定义模式实践：在本地副本把 `mode` 换成 `exp_asc`/`power_desc` 跑短训练，对照 TensorBoard 的 `opacity_histogram` 与 `total_points` 曲线，验证各先验曲线对高斯存活的影响（无 GPU 时写实验设计文档即可）。
- 若对 `.data` 写回的 autograd 语义还想深挖，可阅读 PyTorch 文档中关于 tensor attributes 与 in-place operations 的章节，再回头看 [scene/gaussian_model.py:523-526](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L523-L526) 的 `reset_opacity` 重绑路线作对比。
