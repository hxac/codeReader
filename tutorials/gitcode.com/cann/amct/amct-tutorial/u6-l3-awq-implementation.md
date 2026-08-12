# 经典权重量化算法 AWQ 实现

## 1. 本讲目标

本讲精读 AMCT 中 **AWQ（Activation-aware Weight Quantization，感知激活的权重量化）** 算法的实现。读完本讲你应该能够：

- 说清 AWQ 的核心思想：为什么「保护大激活对应的权重通道」能降低量化误差，以及它如何用一个**等价缩放（scale）**在不改变 Linear 输出的前提下调整权重的量化误差分布。
- 推导 `search_scale` 中 `scale_awq = inputs_mean.pow(ratio)` 的几何含义，并解释为何用 4 次方误差 `(ori_out - quant_out).pow(4).mean()` 而不是 MSE 作为搜索损失。
- 读懂 `apply_scale` 与 `process_weights_for_layers` 如何把缩放同时作用到权重（乘）和激活（除）以保持等价。
- 说清 `LinearAWQuant` 在 classic 经典量化流程中的定位：它在第一次前向时校准、之后做伪量化，并与 `NpuWeightQuantizedLinear` 部署模块成对注册在 `AlgorithmRegistry` 里。

本讲只讲 **classic 经典流程中的 AWQ**（函数式实现，被 `LinearAWQuant` 调用），这是 u2-l3 算法选型矩阵里「大模型权重量化首选」之一的落地代码。它和 LLM PTQ 流（`ALGO_REGISTRY` + 可学习算法 LWC/AutoRound）是**两套独立体系**，本讲末尾会点出二者边界，深入对比留给 u7-l3。

## 2. 前置知识

本讲默认你已掌握以下内容（来自前置讲义）：

- **量化的基本公式与误差来源**（u2-l1）：`int_val = clip(round(float_val/scale + offset), ...)`，误差来自 round（舍入）与 clip（饱和）。权重是静态的、可离线量化；本讲的 AWQ 只量化权重（W-only）。
- **量化粒度**（u2-l1）：per-tensor / per-channel / per-group。AWQ 的缩放因子作用在输入通道（input channel，即 `cin`）这一维。
- **算法选型定位**（u2-l3）：AWQ 属于「搜索优化组」——它在校准数据上**搜索**一个好的缩放因子，再量化；不属于「可学习组」（可学习组经 `--algos` 指定、由 BlockwiseSolver 训练，见 u6-l1/u6-l4）。
- **注册表驱动架构**（u3-l3）：AMCT 用全局注册表做插件化。classic 流程用的是 `AlgorithmRegistry`，其 key 设计与 LLM PTQ 的 `ALGO_REGISTRY` 不同。
- **is_observe 通路与 target 路由**（u6-l1/u6-l2）：可学习算法靠 `is_observe` 切换校准/量化态、靠 `targets=(weight/activation/structure)` 路由挂载点。**AWQ 不走这套机制**——它是 classic 流程的「校准一次、烘焙定参」式算法，没有可学习参数、没有 observe 开关，请务必把二者区分开。

一个直觉性的铺垫：为什么权重量化需要「感知激活」？

Linear 层算的是 \(y = x W^\top\)（x 是激活，W 是权重）。量化 W 时，每个权重元素的**绝对误差**大致由量化步长决定，与该权重本身大小关系不大；但这个误差乘到激活上后，对输出的**贡献**却是 \(\Delta y \approx x \cdot \Delta W\)。也就是说：**激活越大的通道，其权重量化误差对输出的污染越严重**。AWQ 的洞察正是：与其对所有权重一视同仁地量化，不如先放大「大激活通道」的权重，让它们落在量化格点更密的有效区间里（相对误差变小），量化后再把放大抵消掉——输出不变，误差却降了。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [amct_pytorch/algorithms/quant/awq.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/awq.py) | AWQ 算法核心：`search_scale` 网格搜索、`apply_scale` 等价缩放、`process_weights_for_layers` 试量化。纯函数库，不含 nn.Module。 |
| [amct_pytorch/classic/quantize_op/linear_awq_module.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/quantize_op/linear_awq_module.py) | `LinearAWQuant`：把 `nn.Linear` 包成 AWQ 伪量化模块，第一次前向校准、之后 `fake_quant_forward`。 |
| [amct_pytorch/classic/quantize_op/utils.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/quantize_op/utils.py) | 量化基础工具：`calculate_scale_offset`（min-max 求 scale/offset）、`get_weight_min_max_by_granularity`（按粒度取极值）。 |
| [amct_pytorch/algorithms/__init__.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/__init__.py) | classic `AlgorithmRegistry` 的填充地：把每个算法按 `(算法名, 源算子类型)` 注册成 `(伪量化模块, 部署模块)` 对。 |
| [amct_pytorch/algorithms/register_algo.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/register_algo.py) | `Algorithm` 类：classic 注册表的实现，两张字典 `algo` 与 `quant_to_deploy`。 |
| [amct_pytorch/classic/deploy_op/weight_npu_quant_module.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/deploy_op/weight_npu_quant_module.py) | `NpuWeightQuantizedLinear`：AWQ 配对的部署模块，把校准得到的 scale 烘焙进权重、落到 NPU 算子。 |

## 4. 核心概念与源码讲解

### 4.1 AWQ 核心思想：感知激活的等价缩放

#### 4.1.1 概念说明

AWQ 要解决的问题：直接对权重做低比特量化（比如 INT4），误差太大，尤其会伤害大模型精度。它观察到——

> **并非所有权重通道都同样重要。连到大激活通道的权重，其量化误差对输出的影响远大于连到小激活通道的权重。**

于是 AWQ 引入一个**逐输入通道**的对角缩放因子 \(s \in \mathbb{R}^{1 \times c_{in}}\)（每个输入通道一个正值），在量化前把「重要通道」的权重放大、量化后再抵消。关键在于这个放大可以被**精确抵消**，从而不改变 Linear 的数学输出——这就是「等价缩放」。

为什么放大能降误差？因为量化是先把权重除以一个 scale 映射到整数格点，放大某些通道的权重后，它们在量化格子上占的「相对范围」更大，舍入误差的**相对值**更小。而那些被放大通道正是激活大、误差影响大的通道——所以净效果是「把量化误差从重要通道挪到了不那么重要的通道」。

#### 4.1.2 核心流程

Linear 的运算是 \(y = x W^\top\)（x 形状 \((\*, c_{in})\)，W 形状 \((c_{out}, c_{in})\)）。设 \(s\) 是长度 \(c_{in}\) 的正向量，把缩放作用到 \(W\) 的输入维（即列方向）和 \(x\) 的最后一维：

\[
\widetilde{W} = W \cdot s,\qquad \widetilde{x} = x / s
\]

那么

\[
\widetilde{x}\,\widetilde{W}^\top = (x/s)(W \cdot s)^\top = \sum_{c} \frac{x_c}{s_c}(W_{*,c}\cdot s_c) = \sum_{c} x_c W_{*,c} = x W^\top = y
\]

输出**逐元素不变**。但此时若对 \(\widetilde{W}\) 做量化，重要通道（\(s_c\) 大）的权重被放大了，量化相对误差变小。量化后这个缩放就被「烘焙」进权重（\(\times s\)）与激活（\(\div s\)）里，长期生效。

伪代码：

```
# 等价缩放（数学上恒等，但改变了量化误差分布）
weight = weight * s        # 沿 cin 放大重要通道
act    = act / s           # 沿 cin 抵消，保持 y 不变
# 对放大后的 weight 做伪量化（quant_dequant），误差落在重要通道上更小
weight_q = fake_quant(weight)
y = act @ weight_q.T
```

剩下的问题只有一个：**怎么选 \(s\)**？这要靠校准数据，下一节 4.2 的 `search_scale` 来回答。本节先把「缩放本身」与「试量化」两件事的代码讲透。

#### 4.1.3 源码精读

**`apply_scale`：把缩放烘焙进权重与激活。**

[amct_pytorch/algorithms/quant/awq.py:L118-L128](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/awq.py#L118-L128) 把 scale 同时作用到权重（乘）和激活（除），正是上面推导的等价变换：

- `ori_module.weight.data.mul_(scale)`：`scale` 形状 `(1, cin)`，权重形状 `(cout, cin)`，按 `cin` 列广播——即对 W 的输入通道维乘 \(s\)。
- `input_data.div_(scale)`：激活最后一维也是 `cin`，按通道除以 \(s\)。
- 二者都是**原地**操作（`mul_`/`div_`），即校准一旦确定 \(s\)，原始模块的权重和后续输入就被永久改写，缩放随之烘焙进模型。

> **要点**：`apply_scale` 只在 `LinearAWQuant` 第一次前向（校准态）调用一次，之后权重就一直带着 \(\times s\)、激活带着 \(\div s\) 跑伪量化。

**`process_weights_for_layers`：在搜索循环里「试量化」。**

[amct_pytorch/algorithms/quant/awq.py:L31-L54](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/awq.py#L31-L54) 做的是「假设采用某个 scale，权重会有多大量化误差」的模拟：

1. `layer.weight.data.mul(scale_awq)`：临时把权重乘上候选 scale。
2. 对放大后的权重做 `quant_dequant_tensor`（伪量化：量化再反量化，得到带舍入误差的浮点权重）。
3. `layer.weight.data = layer.weight.data / scale_awq`：把 scale 除回去。

净效果是：**权重数值量级回到原位，但携带着「如果用这个 scale，量化会引入多少舍入误差」的痕迹**。这正是搜索循环要评估的对象。注意 `MXFP4_E2M1`（Microscaling 浮点）走不带 scale/offset 的分支，整数类型走 `calculate_scale_offset_by_granularity` 求 scale/offset 再伪量化——这两条分支对应 u2-l2 讲过的 INT 家族与 MXFP 家族。

#### 4.1.4 代码实践

下面是一段**示例代码**（非项目原有代码），用最小数据验证「等价缩放」确实保持 Linear 输出不变：

```python
# 示例代码：验证 (x/s) @ (W*s)^T == x @ W^T
import torch
torch.manual_seed(0)
cout, cin = 8, 16
x = torch.randn(4, cin)          # 激活 (batch, cin)
W = torch.randn(cout, cin)       # 权重 (cout, cin)
s = torch.rand(1, cin) + 0.5     # 每个输入通道一个正值 scale

y_ori = x @ W.t()
y_scaled = (x / s) @ (W * s).t()
print("等价缩放最大误差:", (y_ori - y_scaled).abs().max().item())
# 预期：在浮点精度内接近 0（约 1e-6），证明缩放不改变输出
```

**实践步骤**：

1. 实践目标：亲眼确认 AWQ 等价缩放是数学恒等变换。
2. 操作步骤：把上面的示例代码存成 `demo_awq_equiv.py`，`python demo_awq_equiv.py` 运行。
3. 需要观察的现象：打印的「等价缩放最大误差」应在浮点误差量级（远小于 1）。
4. 预期结果：误差数量级约 `1e-6`，说明 `(x/s)(W*s)^T == xW^T` 成立。
5. 进一步思考：把 `s` 里某个通道设成很大的值（比如 100），重跑——输出误差依然约为 0，但若接着对 `W*s` 做 INT4 伪量化，那个通道的相对量化误差会明显比别的通道小。这正是 AWQ 的目的。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `apply_scale` 改成只对权重乘 `s`、不对激活除 `s`，输出会怎样？

**参考答案**：输出会被放大。因为 \(y' = x (W s)^\top = x W^\top \cdot s\)，等价于把每个通道的贡献乘了 \(s_c\)，不再是恒等变换。AWQ 的「等价」正是靠权重乘、激活除这一对操作**配对抵消**来保证的，缺一不可。

**练习 2**：`apply_scale` 用的是原地算子 `mul_` / `div_`，这意味着什么？

**参考答案**：意味着它直接改写 `ori_module.weight.data` 与传入的 `input_data` 张量，不返回新对象。因此它只能在校准时调用一次；调用后原始浮点权重就被永久修改了——这也是 `LinearAWQuant` 在 `__init__` 里 `copy.deepcopy(ori_module.weight)` 留一份干净副本（`self.weight`）的原因，用于第一次前向算浮点参考输出。

---

### 4.2 search_scale 网格搜索：如何挑最优缩放因子

#### 4.2.1 概念说明

`apply_scale` 给出了「缩放怎么做」，但缩放因子 \(s\) 取什么值才好？AWQ 的策略是：

1. **用激活幅值作为通道重要性的代理**：哪个输入通道的激活幅值大，哪个通道就重要，应给更大的 \(s_c\)。
2. **网格搜索**：直接用激活幅值当 \(s\) 太激进，完全不缩放（全 1）又退化成普通量化。于是 AWQ 在「全 1」和「完全按激活幅值」之间取一条**插值曲线**，沿曲线均匀采样若干个候选 scale，逐个试量化并比较输出误差，取最优。

`search_scale` 函数就是这套搜索的实现。它只在校准时跑一次，输出一个固定的 `best_scale`，之后整个模型都用它。

#### 4.2.2 核心流程

`search_scale` 的骨架（伪代码）：

```
1. 校验 inputs / 各层 weight 不含 nan/inf
2. ori_out = block(inputs)               # 用原始浮点权重算参考输出
3. inputs_mean = inputs.abs().view(-1, cin).mean(0)   # 每个输入通道的平均激活幅值 (cin,)
4. 保存 block 当前 state_dict 作为可回退状态
5. for grid in range(grids_num):
       ratio = grid / grids_num
       scale_awq = inputs_mean.pow(ratio)            # 候选 scale
       scale_awq = normalize(scale_awq)              # 几何归一化
       process_weights_for_layers(layers, scale_awq) # 用该 scale 试量化权重
       quant_out = block(inputs)                     # 试量化后的输出
       loss = (ori_out - quant_out).pow(4).mean()    # 4 次方误差
       if loss < min_loss: 记录 best_scale
       block.load_state_dict(ori_state)              # 回退权重，试下一个
6. 返回 best_scale
```

两个关键设计的数学含义：

**(a) `scale_awq = inputs_mean.pow(ratio)` 的几何含义**

设 \(m_c = \text{inputs\_mean}_c\)（第 \(c\) 通道的平均激活幅值）。候选 scale \(s_c = m_c^{\text{ratio}}\)，其中 \(\text{ratio} \in [0, 1)\)。

取对数：

\[
\ln s_c = \text{ratio} \cdot \ln m_c
\]

- 当 `ratio = 0`：\(\ln s_c = 0\)，即 \(s_c = 1\) 对所有通道——**完全不缩放**，退化为普通量化（baseline）。
- 当 `ratio → 1`：\(\ln s_c \to \ln m_c\)，即 \(s_c \to m_c\)——**完全按激活幅值**缩放。
- 中间的 `ratio`：在对数坐标上是 0 与 \(\ln m_c\) 之间的**线性插值**。

所以 `pow(ratio)` 是在「不缩放」与「按幅值缩放」之间做**对数空间（几何）插值**，`ratio` 是「多信任激活幅值」的旋钮。之所以在对数空间而非线性空间插值，是因为 scale 是正值且跨数量级，几何插值更自然、格点分布更合理。

随后还有一步归一化（见源码精读），把 scale 的整体量级规范到围绕 1。

**(b) 为什么损失是 4 次方而不是 MSE**

MSE 用 2 次方：\(\frac{1}{n}\sum (e_i)^2\)；AMCT 的 AWQ 用 4 次方：\(\frac{1}{n}\sum (e_i)^4\)，其中 \(e_i = \text{ori\_out}_i - \text{quant\_out}_i\)。

4 次方对**大误差（异常值）的惩罚远比 2 次方重**：同一个误差从 1 变到 2，2 次方项从 1 涨到 4（×4），4 次方项从 1 涨到 16（×16）。换句话说，4 次方损失逼迫搜索结果优先**压住最差的那些输出维度**，而不是为了讨好多数维度而放任少数维度出错。在大模型量化里，少数异常通道/outlier 往往主导可见的精度下降（这也呼应了 u2-l3「激活比权重难量化、outlier 是误差主源」），因此用一个更「厌恶极端误差」的目标更贴合最终 PPL。这是 AMCT 的一个工程调优选择。

#### 4.2.3 源码精读

`search_scale` 全函数见 [amct_pytorch/algorithms/quant/awq.py:L57-L115](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/awq.py#L57-L115)。逐段拆解：

**前置校验**（L70-L74）：先确认激活和权重都没有 nan/inf，否则网格搜索的 loss 全是 nan，毫无意义。这是 AWQ 对校准数据质量的硬要求。

**参考输出**（L77-L80）：在 `torch.no_grad()` 下用**原始**（尚未缩放的）block 算一次 `ori_out`，作为后面所有候选 scale 比较的「标准答案」。注意它支持 block 返回 tuple（取第 0 个），兼容多种 decoder 子模块。

**激活幅值统计**（L84）：

```python
inputs_mean = inputs.abs().contiguous().view(-1, inputs.shape[-1]).mean(0)  # (cin,)
```

把激活展平成 `(*, cin)`，沿第 0 维求均值，得到每个输入通道的平均绝对激活。这是 AWQ 的「重要性签名」。

**网格循环与候选 scale**（L89-L98）：`ratio = grid / grids_num`，`scale_awq = inputs_mean.pow(ratio)`，再三步处理：

1. `.clamp(min=1e-4)` 防 0 幂；
2. `.to(torch.float64)` 用双精度计算，减小搜索阶段的数值误差；
3. **几何归一化**：`scale_awq = scale_awq / (scale_awq.max() * scale_awq.min()).sqrt()`。

归一化的含义：除以「max 与 min 的几何平均」，使归一化后 \(\max(s)\cdot\min(s)=1\)——大通道 \(>1\)、小通道 \(<1\)，在 log 空间关于 1 对称。这把 scale 的**整体量级**规范住，让网格只搜索 scale 的**形状（哪些通道该放大多少）**，而不被整体放缩干扰（因为等价缩放只关心相对比例，但量化格点的 clip 行为会受绝对量级影响，归一化把这部分稳定下来）。

随后再 clamp 到激活 dtype 的正数范围、搬到 block 所在设备。

**试量化 + 评估**（L99-L108）：

```python
process_weights_for_layers(layers, scale_awq, quant_config)  # 用该 scale 试量化
quant_out = block(inputs, **kwargs)                          # 试量化输出
loss = (ori_out - quant_out).float().pow(4).mean()           # 4 次方误差
if not torch.isnan(loss) and loss < min_loss:
    min_loss, best_scale = loss, scale_awq
```

注意 loss 显式 `.float()`（升到 float32）再算 4 次方，避免低精度 dtype 下 `pow(4)` 溢出。`isnan` 检查跳过坏格点。

**状态回退**（L110）：`block.load_state_dict(ori_state)` 把权重恢复成搜索前的样子，保证下一个格点从干净的原始权重开始。`ori_state` 在 L86-L88 用 `state_dict().items()` 拷到 CPU 保存。

**收尾**（L112-L115）：若全程没拿到合法 loss 抛 `RuntimeError`，否则返回 `best_scale`。

> **一句话总结**：`search_scale` = 用激活幅值的幂次生成一族候选 scale → 每个都试量化一次、用 4 次方误差比对参考输出 → 取误差最小的 scale。它不训练任何参数，是纯搜索。

#### 4.2.4 代码实践

**实践任务（本讲核心实践）**：阅读 [search_scale](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/awq.py#L57-L115)，完成以下两问。

**第 1 问：推导 `scale_awq = inputs_mean.pow(ratio)` 的几何含义。**

操作步骤：

1. 打开 `awq.py` 定位到 L84-L98。
2. 取对数推导 \(\ln s_c = \text{ratio}\cdot \ln m_c\)（见 4.2.2）。
3. 用一段**示例代码**画出不同 `ratio` 下 scale 的形状，直观感受插值：

```python
# 示例代码：可视化 inputs_mean.pow(ratio) 随 ratio 的变化
import torch
m = torch.tensor([0.1, 0.5, 1.0, 2.0, 10.0])  # 5 个通道的inputs_mean
for ratio in [0.0, 0.25, 0.5, 0.75, 1.0]:
    s = m.pow(ratio)
    print(f"ratio={ratio:.2f}  scale={s.tolist()}")
# 预期：ratio=0 全 1；ratio 增大，大通道明显变大、小通道趋近 1，呈对数插值
```

需要观察的现象：`ratio=0` 时 scale 全是 1（无缩放）；`ratio` 越大，scale 越接近 `inputs_mean` 本身（完全按幅值）。
预期结果：你应能用一句话回答「`pow(ratio)` 是在『不缩放』与『按激活幅值缩放』之间的对数空间线性插值」。

**第 2 问：为何用 `(ori_out - quant_out).pow(4).mean()` 而不是 MSE？**

操作步骤：

1. 定位到 L105。
2. 用**示例代码**对比 2 次方与 4 次方对异常值的敏感度：

```python
# 示例代码：对比 2 次方与 4 次方损失对异常值的敏感度
import torch
e = torch.tensor([0.1, 0.1, 0.1, 0.1, 3.0])  # 4 个小误差 + 1 个大异常
print("MSE  (pow2):", e.pow(2).mean().item())   # 大异常被相对容忍
print("4次方(pow4):", e.pow(4).mean().item())   # 大异常主导整个损失
```

需要观察的现象：两种损失下那个 `3.0` 的异常值贡献占比截然不同——4 次方损失里它几乎主导了整个值。
预期结果：你能解释「4 次方损失更厌恶极端误差，逼迫 scale 优先压住最差的输出维度，更贴合大模型量化里 outlier 主导精度下降的现实」。这正是 AMCT 选它而非 MSE 的工程理由。

> 说明：以上示例代码用于辅助理解，**未在仓库内运行验证**；若你要在本地跑，只需 `pip install torch` 即可，无 NPU 依赖。

#### 4.2.5 小练习与答案

**练习 1**：`grids_num` 越大，搜索结果一定越好吗？代价是什么？

**参考答案**：理论上格点越密越可能逼近最优 scale，但代价是**线性增加的前向次数**——每格点都要做一次 `process_weights_for_layers`（含伪量化）+ 一次 `block(inputs)` 前向 + 一次 `load_state_dict` 回退。对大模型这是可观的校准耗时。`grids_num` 是精度与校准时间的权衡旋钮，默认值由 `quant_config['algorithm']['awq']['grids_num']` 给出。

**练习 2**：搜索循环里为什么要 `block.load_state_dict(ori_state)` 回退？不回退会怎样？

**参考答案**：因为 `process_weights_for_layers` 是**原地**修改权重（乘 scale → 伪量化 → 除回去）。虽然除回去后量级复原，但伪量化引入的舍入误差**残留**在权重里了。若不回退，下一个格点会在「已被上一个 scale 污染」的权重上继续试量化，误差累积、结果失真。回退保证每个格点都从干净的原始权重出发。

**练习 3**：为什么 `inputs_mean` 要取 `.abs()` 再 `.mean(0)`，而不是直接 `mean(0)`？

**参考答案**：激活有正有负，直接平均会正负相消、幅值被低估，无法反映通道的「活跃程度」。取绝对值再平均得到的是平均幅值，才是通道重要性的合理代理（与量化误差贡献 \(\Delta y \approx |x|\cdot|\Delta W|\) 中的 \(|x|\) 对应）。

---

### 4.3 classic quantize_op 模块定位：LinearAWQuant 与算法注册表

#### 4.3.1 概念说明

前两节讲了 AWQ 的算法函数（`search_scale`/`apply_scale`/`process_weights_for_layers`），它们是纯函数。但 AMCT 的 classic 流程需要把这些函数**挂到一个 `nn.Module` 上**，让它能替换原始 `nn.Linear`、在前向时自动校准和伪量化——这个 Module 就是 `LinearAWQuant`。

`LinearAWQuant` 的生命周期分两态：

- **校准态**（第一次前向，`calc_done=False`）：跑一次原始 Linear 得到浮点参考输出，同时调用 `search_scale` 搜出最优 scale、用 `apply_scale` 烘焙进权重，并算出权重的 `scale_w/offset_w`（供后续伪量化），最后置 `calc_done=True`。**返回的是浮点参考输出**（校准阶段不应被量化污染）。
- **伪量化态**（之后每次前向，`calc_done=True`）：走 `fake_quant_forward`，用烘焙好的 scale 对激活做 ÷s、对缓存好的伪量化权重做 Linear。

而在整个 classic 流程里，`LinearAWQuant` 还有一个**配对的部署模块** `NpuWeightQuantizedLinear`：校准/伪量化阶段用前者（CPU/GPU 上跑伪量化），部署阶段把参数烘焙后替换成后者（落到 NPU 算子）。这对模块靠 `AlgorithmRegistry` 注册表绑定。这就是 u1-l3 讲过的「quantize_op（伪量化）与 deploy_op（部署）成对」在 AWQ 上的具体体现。

#### 4.3.2 核心流程

`LinearAWQuant.forward` 的两态分支：

```
forward(inputs):
    input_data = inputs.clone()
    output = F.linear(input_data, self.weight, self.bias)   # 用干净 deepcopy 算浮点输出
    if self.calc_done:
        return self.fake_quant_forward(input_data)          # 伪量化态

    # ↓ 校准态（仅第一次）
    scale_awq = search_scale(input_data, [self.ori_module], self.ori_module, quant_config)
    apply_scale(scale_awq, self.ori_module, input_data)     # 烘焙：weight×s, input÷s
    self.scale = 1 / scale_awq.detach()                     # 记录激活缩放(=1/s)
    self.scale_w, self.offset_w = calculate_scale_offset_by_granularity(...)  # 权重 scale/offset
    self.calc_done = True
    return output                                            # 返回浮点参考输出

fake_quant_forward(inputs):
    # 首次进入时缓存伪量化权重，避免每次前向重复 quant_dequant
    cached_dq_w = quant_dequant_weight(self.ori_module.weight.data, ...)   # 已 ×s 的权重
    x = inputs * self.scale                                  # 激活 ÷s（self.scale=1/s）
    return F.linear(x, cached_dq_w, self.bias)
```

注册侧：classic 流程在 [amct_pytorch/algorithms/__init__.py:L72](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/__init__.py#L72) 把 AWQ 注册成「伪量化模块 ↔ 部署模块」一对：

```python
AlgorithmRegistry.register('awq', 'Linear', LinearAWQuant, NpuWeightQuantizedLinear)
```

含义：算法名 `awq` + 源算子类型 `Linear` → 伪量化模块 `LinearAWQuant`，且它配对的部署模块是 `NpuWeightQuantizedLinear`。

#### 4.3.3 源码精读

**`LinearAWQuant` 类定义与 `__init__`。**

[amct_pytorch/classic/quantize_op/linear_awq_module.py:L33-L61](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/quantize_op/linear_awq_module.py#L33-L61) 继承 `BaseQuantizeModule`，关键是：

- `self.weight = copy.deepcopy(ori_module.weight)`（L49）：留一份**未缩放**的干净权重副本，专供第一次前向算浮点参考输出（因为 `apply_scale` 之后 `self.ori_module.weight` 就被改写了）。
- `self.calc_done = False`（L58）：两态分支开关，初始为校准态。

**`forward`：两态分支本体。**

[amct_pytorch/classic/quantize_op/linear_awq_module.py:L63-L97](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/quantize_op/linear_awq_module.py#L63-L97) 整个 `forward` 在 `@torch.no_grad()` 下（校准/伪量化都不需要反传——再次印证 AWQ 不是可学习算法）。L74 先用干净副本算 `output`；L75-L76 若已校准则转 `fake_quant_forward`；L78-L92 是首次校准：`search_scale` → `apply_scale` → 存 `self.scale = 1/scale_awq` → 算 `scale_w/offset_w` → 置 `calc_done=True`。L97 返回浮点 `output`。

注意 L82 `self.scale = 1 / scale_awq.detach()`：存的是倒数。因为 `apply_scale` 已经把激活的 ÷s 烘焙掉了，后续伪量化态用 `self.scale`（=1/s）乘激活来复现 ÷s 的效果。

**`fake_quant_forward`：带缓存的伪量化。**

[amct_pytorch/classic/quantize_op/linear_awq_module.py:L99-L111](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/quantize_op/linear_awq_module.py#L99-L111)。`fake_quant_cache_ready` 标志保证 `quant_dequant_weight` 只算一次、结果缓存进 `self.cached_dq_w`，避免每次前向重复做代价不低的伪量化。注意它伪量化的是 `self.ori_module.weight.data`（已被 `apply_scale` 乘过 s 的权重），与 `x = inputs * self.scale`（激活 ÷s）配对，维持等价缩放。

**classic 注册表 `Algorithm` 类。**

[amct_pytorch/algorithms/register_algo.py:L22-L44](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/register_algo.py#L22-L44) 是 classic 注册表的实现，核心是两张字典：

- `self.algo[name][src_op] = quant_op`：按 `(算法名, 源算子类型)` 查伪量化模块。例如 `algo['awq']['Linear'] = LinearAWQuant`。
- `self.quant_to_deploy[quant_op] = [deploy_op, ...]`：按伪量化模块查它配对的所有部署模块。

这与 LLM PTQ 的 `ALGO_REGISTRY`（key 仅算法名 + `targets` 元数据，见 u6-l2）在 **key 设计上根本不同**：classic 是「算法 × 源算子类型」二维定位一套成对的伪量化/部署模块；LLM PTQ 是「算法名 + target 标签」路由到挂载点。这正是 u7-l3 要展开的「双注册表体系」边界，本节先建立 AWQ 侧的具象认知。

**配对部署模块如何消费 AWQ scale。**

[amct_pytorch/classic/deploy_op/weight_npu_quant_module.py:L66-L71](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/deploy_op/weight_npu_quant_module.py#L66-L71)：部署时若 `quant_module.scale` 存在（即 AWQ 算过），调用 `apply_awq_quantize_weight(weight, scale, group_size)`。该函数（[quant_util.py:L310-L329](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/utils/quant_util.py#L310-L329)）做 `weight = weight / awq_scale`，而 `awq_scale = self.scale = 1/s`，故 `weight / (1/s) = weight * s`——部署时再次把权重乘上保护因子 s，与伪量化态保持一致，并把 scale 存成 `scale_factor` buffer 供 NPU 算子对激活做 ÷s。

#### 4.3.4 代码实践

**实践任务**：跟踪 AWQ 从「注册 → 校准 → 伪量化 → 部署烘焙」的完整调用链，把每一步对应的源码位置串起来。

操作步骤：

1. 从注册入口 [algorithms/__init__.py:L72](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/__init__.py#L72) 出发，确认 `LinearAWQuant` 与 `NpuWeightQuantizedLinear` 已成对注册。
2. 打开 [linear_awq_module.py:L63-L97](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/quantize_op/linear_awq_module.py#L63-L97)，确认第一次前向会调用 `awq.py` 的 `search_scale`（L78）和 `apply_scale`（L81）。
3. 打开 [awq.py:L118-L128](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/awq.py#L118-L128)，确认 `apply_scale` 用 `mul_`/`div_` 原地烘焙。
4. 打开部署侧 [weight_npu_quant_module.py:L66-L71](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/deploy_op/weight_npu_quant_module.py#L66-L71)，确认部署时 `apply_awq_quantize_weight` 又把权重乘回 s。

需要观察的现象：整条链路上，**权重始终带着 ×s、激活始终带着 ÷s**，从校准到部署一致，没有遗漏或重复。
预期结果：你能画出这样一张调用链：

```
注册: AlgorithmRegistry.register('awq','Linear', LinearAWQuant, NpuWeightQuantizedLinear)
校准(首次forward): LinearAWQuant.forward
  → search_scale(...)              # 搜最优 s
  → apply_scale(s, ori_module, x)  # weight×s, x÷s (原地)
  → self.scale = 1/s; calc_done=True
伪量化(后续forward): LinearAWQuant.fake_quant_forward
  → cached_dq_w = quant_dequant_weight(weight×s)
  → F.linear(x×(1/s), cached_dq_w)
部署: NpuWeightQuantizedLinear
  → apply_awq_quantize_weight(weight, 1/s) => weight×s
  → 落到 NPU 算子，scale_factor=1/s 作用于激活
```

> 说明：本实践为**源码阅读型实践**，不需要运行 NPU；若想在 CPU 上感受，可参考 4.1.4 的示例代码自行用 `torch.nn.Linear` 包一个 `LinearAWQuant` 跑前两步（部署步需 torch_npu，待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`LinearAWQuant.forward` 为什么整体在 `@torch.no_grad()` 下？这和 u6-l1 的可学习算法有什么不同？

**参考答案**：因为 AWQ 没有可学习参数——scale 靠搜索得到、权重靠原地烘焙、`scale_w/offset_w` 靠 min-max 解析求出，全程不需要梯度反传。这与 u6-l1 的可学习算法（LWC/LAC 等，靠 BlockwiseSolver 反向训练 `trainable_params`）形成对照：可学习算法必须在梯度态跑、依赖 `is_observe` 切换校准/量化；AWQ 则是「校准一次、定参」，无 observe 开关、无反传。

**练习 2**：`LinearAWQuant` 为什么要在 `__init__` 里 `copy.deepcopy(ori_module.weight)`？

**参考答案**：因为第一次前向的 `apply_scale` 会原地改写 `self.ori_module.weight`（乘上 s）。若不留副本，算浮点参考输出 `output = F.linear(input_data, self.weight, self.bias)` 时就拿到已被改写的权重，参考输出就错了。deepcopy 保住一份未缩放的原始权重专供算参考。

**练习 3**：classic 的 `AlgorithmRegistry` 与 LLM PTQ 的 `ALGO_REGISTRY` 在 key 设计上的根本区别是什么？

**参考答案**：classic `AlgorithmRegistry` 的 key 是 `(算法名, 源算子类型)` 二元组（如 `('awq', 'Linear')`），值为「伪量化模块 + 配对部署模块」；它关注的是「在 classic 图优化流程里，把某个源算子替换成哪个伪量化模块、部署时再换成哪个 NPU 模块」。LLM PTQ 的 `ALGO_REGISTRY` 的 key 是算法名，靠 `targets=(weight/activation/structure)` 元数据路由到 `WeightQuantizer`/`ActivationQuantizer`/`QuantGatedMLP` 三个挂载点；它关注的是「算法挂到哪个量化器上」。前者是「模块替换对」，后者是「挂载点路由」。深入对比见 u7-l3。

## 5. 综合实践

**综合任务：在一个玩具 Linear 上实现迷你 AWQ，对比朴素 min-max 量化与 AWQ 的输出误差。**

目标：把本讲三个模块（等价缩放、网格搜索、模块定位）串起来，亲手验证「AWQ 比不缩放的朴素量化更准」。

下面是**示例代码**（非项目原有代码，需 `pip install torch`，无 NPU 依赖）：

```python
# 示例代码：迷你 AWQ 搜索，对比朴素量化
import torch
torch.manual_seed(42)

def fake_quant_int(w, bits=4):
    # 对权重的每一行(cin维)做对称 per-channel INT 伪量化
    mx = w.abs().max(dim=1, keepdim=True).values.clamp(min=1e-8)
    scale = mx / (2**(bits-1) - 1)
    q = torch.round(w / scale).clamp(-(2**(bits-1)), 2**(bits-1)-1)
    return q * scale

cout, cin = 16, 64
W = torch.randn(cout, cin)
x = torch.randn(8, cin)
# 人造重要通道：让 x 的第 0、1 通道幅值远大
x[:, 0:2] *= 8.0

y_ori = x @ W.t()                          # 浮点参考

# (a) 朴素量化：直接对 W 伪量化
W_naive = fake_quant_int(W.clone(), bits=4)
err_naive = (y_ori - x @ W_naive.t()).pow(4).mean()

# (b) AWQ：网格搜索最优 s
inputs_mean = x.abs().mean(dim=0)          # (cin,)
best_loss, best_s = float('inf'), None
ori_state = W.clone()
for grid in range(20):
    ratio = grid / 20
    s = inputs_mean.pow(ratio).clamp(min=1e-4)
    s = s / (s.max()*s.min()).sqrt()       # 几何归一化
    Wt = W.clone() * s                     # 试量化：×s
    Wt = fake_quant_int(Wt, bits=4)
    Wt = Wt / s                            # 除回去
    y_q = x @ Wt.t()
    loss = (y_ori - y_q).pow(4).mean()
    if loss < best_loss:
        best_loss, best_s = loss.item(), s.clone()

# 用最优 s 做最终伪量化（权重×s, 激活÷s），与朴素对比
W_awq = fake_quant_int(W.clone() * best_s, bits=4)
y_awq = (x / best_s) @ W_awq.t()
err_awq = (y_ori - y_awq).pow(4).mean()

print(f"朴素 INT4 量化 4次方误差: {err_naive.item():.4f}")
print(f"AWQ     INT4 量化 4次方误差: {err_awq.item():.4f}")
```

实践步骤：

1. 把示例代码存为 `mini_awq.py` 并运行（`python mini_awq.py`）。
2. 记录两种误差数值。
3. 把 `x[:, 0:2] *= 8.0` 这行注释掉（去掉人造重要通道），再跑一次，观察 AWQ 的优势是否缩小。

需要观察的现象：带人造重要通道时，AWQ 的 4 次方误差应明显小于朴素量化；去掉重要通道后两者差距应缩小。
预期结果：说明 AWQ 的收益来自「感知激活幅值、保护重要通道」——当激活分布越不均（有显著大通道），AWQ 收益越大。
如果无法确定运行结果，请标注「待本地验证」。

## 6. 本讲小结

- **AWQ 的核心思想**是「感知激活」：用激活幅值代理通道重要性，对重要通道的权重在量化前放大、量化后抵消，把量化误差从重要通道挪到不重要通道，且不改变 Linear 输出。
- **等价缩放** `apply_scale` 靠权重乘 s、激活除 s 这对操作配对抵消，保证 \((x/s)(Ws)^\top = xW^\top\)；这是 AWQ 一切操作的数学前提。
- **`search_scale` 网格搜索**用 `inputs_mean.pow(ratio)` 在「不缩放」与「按幅值缩放」之间做对数空间插值生成候选 s，用 `(ori_out-quant_out).pow(4).mean()` 这个 4 次方损失（比 MSE 更厌恶极端误差）挑最优，全程无梯度、纯搜索。
- **`process_weights_for_layers`** 是搜索循环里的「试量化」：×s → 伪量化 → ÷s，让权重回到原量级但携带该 scale 下的舍入误差痕迹，配合 `load_state_dict` 回退保证每个格点从干净权重出发。
- **`LinearAWQuant`** 是 classic 流程里 AWQ 的伪量化模块：第一次前向校准（search+apply+算 scale_w/offset_w）、之后 `fake_quant_forward` 带缓存伪量化；全程 `@torch.no_grad()`，**没有可学习参数、没有 is_observe 开关**，与 u6-l1 的可学习算法形成对照。
- **模块定位**：AWQ 在 [algorithms/__init__.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/__init__.py) 里以 `('awq','Linear') → (LinearAWQuant, NpuWeightQuantizedLinear)` 成对注册在 classic `AlgorithmRegistry`，这是 classic「quantize_op ↔ deploy_op 成对」的具象实例，与 LLM PTQ 的 `ALGO_REGISTRY` 是两套独立体系。

## 7. 下一步学习建议

- **对比可学习算法**：本讲的 AWQ 是「搜索定参、无梯度」。建议接着读 u6-l4，看 LWC/LAC/FlatQuant 等**可学习算法**如何用 sigmoid+clip_factor 学习截断边界、靠 BlockwiseSolver 反向训练——两套思路（搜索 vs 学习）的对照能加深对量化算法设计的理解。
- **深入双注册表**：本节末尾点出了 classic `AlgorithmRegistry` 与 LLM PTQ `ALGO_REGISTRY` 的 key 设计差异。完整的「双注册表体系」边界（含 quantize_op/deploy_op 成对映射、两套体系适用场景）在 u7-l3 专门展开，建议接着读。
- **看部署侧落地**：若你想知道 AWQ 的 scale 最终如何被 NPU 算子消费，可顺着本讲的 [weight_npu_quant_module.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/deploy_op/weight_npu_quant_module.py) 读 `NpuWeightQuantizedLinear` 的 `get_quantize_weight`，并结合 u4-l4 的 deploy 导出流程理解「伪量化模块 → 部署模块」的烘焙全链路。
- **回到选型**：u2-l3 的算法选型矩阵告诉你「什么时候用 AWQ」，本讲告诉你「AWQ 内部怎么做」。建议回头再看一眼那张矩阵，确认你能在选 AWQ 时说清它的代价（网格搜索的前向开销）与收益（保护重要通道）。
