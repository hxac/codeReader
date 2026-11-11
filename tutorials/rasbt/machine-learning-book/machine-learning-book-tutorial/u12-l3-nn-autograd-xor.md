# nn.Module、autograd 与 XOR 训练

## 1. 本讲目标

第 11 章我们用 numpy 手写了 `NeuralNetMLP` 的前向与反向传播——四组梯度公式全靠手工推导。本讲学习 PyTorch 如何把这两件事「外包」出去:`torch.nn` 负责搭模型,`autograd` 负责算梯度。学完本讲你应该能够:

1. 说清「forward 定义计算图、`loss.backward()` 沿图反向填充每个参数的 `.grad`」这一 autograd 机制,并能用书中线性回归例子验证自动微分与手算梯度一致。
2. 理解 `optimizer.zero_grad()` 为什么必不可少:PyTorch 的 `.grad` 是**累加**语义,不清零就会把上一批的梯度叠进这一批。
3. 用 `nn.Linear`、`nn.Sequential` 与自定义 `nn.Module` 子类三种方式搭建网络,并说清 `nn.CrossEntropyLoss` 与 forward 里显式 `Softmax` 搭配的细节。
4. 解释为什么多分类输出层要用 softmax:对多个输出单元各自套 sigmoid 得到的「概率」互相独立、加起来不等于 1,不能当作概率分布使用。
5. 掌握 `torch.save`/`torch.load` 的两种方式(保存整个模型对象 vs 保存 `state_dict`),并了解跨设备加载的官方做法。
6. 完成综合实践:把书中 XOR 网络的隐藏单元宽度从 4 改成 1/2/4/8 分别训练,用柱状图验证「XOR 至少需要 2 个隐藏单元」。

## 2. 前置知识

- **梯度与链式法则(u2-l2、u11-l2)**:梯度是代价函数对参数的偏导数;反向传播就是链式法则的逐层展开。第 11 章我们手推过 \(\partial L/\partial \boldsymbol{W}\) 的矩阵公式,本讲把这些公式全部交给 autograd。
- **张量与维度约定(u12-l1)**:批数据的第一维是 batch 维(`dim=0`),特征维在其后;`softmax(dim=1)` 的 `dim=1` 正是「对每个样本自己的特征维归一化」。
- **Dataset 与 DataLoader(u12-l2)**:训练循环的最内层 `for x_batch, y_batch in train_dl:` 由 DataLoader 驱动,每轮自动洗牌、切批。
- **权重矩阵形状约定(u11-l1)**:`nn.Linear(in, out)` 内部的权重形状是 `(out, in)`——「行 = 目标单元、列 = 来源特征」,与手写 `NeuralNetMLP` 的 `weight_h` 完全一致,本讲会再次验证。
- **计算图(computational graph)**:把一次前向计算中「哪个张量由哪些张量经过什么运算得到」记成一张有向无环图。**叶子张量**(leaf tensor)指直接创建、不是由运算得到的张量——通常就是权重和偏置。autograd 的全部工作就是:你写前向,它记图;你调 `backward()`,它沿图反向把 \(\partial L/\partial\theta\) 写进每个叶子的 `.grad` 属性。
- **logits**:网络最后一个线性层的未归一化输出。激活函数(sigmoid/softmax)把 logits 变换为概率或近概率值。

## 3. 本讲源码地图

本讲主线在 `ch12/ch12_part2.py`(第 12 章下半部分:建模、训练、激活函数、保存加载);XOR 的完整示例(数据、单层失败模型、多层成功模型、`train` 函数)实际位于下一章 `ch13/ch13_part1.py`,本讲综合实践将直接复用它的代码:

| 源码位置 | 作用 |
| --- | --- |
| [ch12/ch12_part2.py:L109-L137](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L109-L137) | 不用 `torch.nn`:纯张量 + `requires_grad` + `loss.backward()` 手写线性回归训练循环,展示 autograd 最小机制 |
| [ch12/ch12_part2.py:L170-L201](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L170-L201) | `nn.Linear` + `nn.MSELoss` + `torch.optim.SGD` 的五步训练循环 |
| [ch12/ch12_part2.py:L252-L275](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L252-L275) | 自定义 `Model(nn.Module)` 两层网络(Iris 三分类),`CrossEntropyLoss` + `Adam` |
| [ch12/ch12_part2.py:L280-L334](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L280-L334) | Iris 训练循环(损失/准确率按 epoch 统计)与测试集评估 |
| [ch12/ch12_part2.py:L341-L370](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L341-L370) | `torch.save`/`torch.load` 两种保存与加载方式 |
| [ch12/ch12_part2.py:L381-L501](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L381-L501) | 激活函数一节:手写 logistic/softmax/tanh 对比,`torch.sigmoid`/`torch.tanh`/`torch.relu` API |
| [ch13/ch13_part1.py:L228-L259](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L228-L259) | XOR 数据生成(200 个点、异或标签)与 DataLoader |
| [ch13/ch13_part1.py:L264-L308](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L264-L308) | `nn.Sequential` 单层模型(学不动 XOR)与通用 `train` 函数 |
| [ch13/ch13_part1.py:L331-L348](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L331-L348) | `nn.Sequential` 多层 ReLU 网络(隐藏宽度 4),本讲综合实践的改造对象 |

## 4. 核心概念与源码讲解

### 4.1 autograd:forward 定义计算图,`loss.backward()` 填充 `.grad`

#### 4.1.1 概念说明

在第 11 章,训练一个两层 MLP 需要手推四组梯度(输出层权重/偏置、隐藏层权重/偏置),再在 `backward` 里逐条写成矩阵运算。autograd 的承诺是:**你只写前向,梯度自动来**。

机制分三步:

1. **建图**:对一个 `requires_grad=True` 的张量做任何运算,PyTorch 都会记录「结果由谁经过什么运算得到」,形成计算图。
2. **反向**:对图的终点(通常是标量损失)调用 `.backward()`,autograd 从终点出发、按链式法则逐节点回传,把 \(\partial L/\partial\theta\) **累加**到每个叶子的 `.grad` 属性里。
3. **更新**:你自己(或优化器)拿 `.grad` 去更新参数。

书中用一个不含任何 `torch.nn` 组件的线性回归把这套机制裸露出来:参数是两个普通张量,模型和损失是两个普通函数。

#### 4.1.2 核心流程

对单样本线性回归 \(\hat{y} = w x + b\),损失 \(L = (\hat{y} - y)^2\),解析梯度为:

\[ \frac{\partial L}{\partial w} = 2x\,(wx+b-y), \qquad \frac{\partial L}{\partial b} = 2\,(wx+b-y) \]

autograd 版训练循环伪代码:

```text
weight, bias = 叶子张量(requires_grad=True)
for epoch:
    for x_batch, y_batch in DataLoader:
        pred = x_batch @ weight + bias   # 前向,同时建图
        loss = MSE(pred, y_batch)        # 图的终点
        loss.backward()                  # 反向:填充 weight.grad、bias.grad
        with torch.no_grad():            # 更新不记图
            weight -= lr * weight.grad   # 手动更新
            weight.grad.zero_()          # 清零,防累积
```

#### 4.1.3 源码精读

**参数的两个 `requires_grad` 写法**:

[ch12/ch12_part2.py:L109-L112](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L109-L112) —— `torch.randn(1)` 创建的 `weight` 默认不追踪梯度,用带下划线的原地方法 `weight.requires_grad_()` 事后开启;`bias` 则在创建时直接写 `torch.zeros(1, requires_grad=True)`。两种写法等价,都是声明「这是需要学习的叶子张量」。

**模型与损失就是两个普通函数**:

[ch12/ch12_part2.py:L114-L118](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L114-L118) —— `loss_fn` 计算 `(input-target).pow(2).mean()`(MSE);`model(xb)` 计算 `xb @ weight + bias`。因为 `weight`、`bias` 追踪梯度,这两次函数调用同时也在建计算图。

**训练循环的三处关键**:

[ch12/ch12_part2.py:L124-L137](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L124-L137) —— ① L128 的 `loss.backward()` 从标量损失反向回传,把梯度写进 `weight.grad` 与 `bias.grad`;② L130 的 `with torch.no_grad():` 让更新操作**不**被记进计算图——参数更新是普通数值运算,不是模型结构的一部分,若不包住,对要求梯度的叶子张量做原地减法会被 autograd 拦下报错;③ L133-L134 的 `weight.grad.zero_()`、`bias.grad.zero_()` 在每批更新后清零,原因见 4.3 节( `.grad` 是累加语义)。

**脱离计算图取数值**:

[ch12/ch12_part2.py:L148](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L148) —— 训练后画图时用 `model(X_test_norm).detach().numpy()`:`detach()` 返回一个脱离计算图的普通张量,才能调用 `.numpy()` 交给 matplotlib。这正是 u12-l1 讲过的「张量三属性」中 device/grad 追踪在工程上的落点。

#### 4.1.4 代码实践

1. **实践目标**:亲眼验证 autograd 算出的梯度与手算公式一致,并观察 `.grad` 的累加行为。
2. **操作步骤**(在装好 PyTorch 的环境里执行,**示例代码**,基于书中 L114-L137 精简):

```python
import torch
torch.manual_seed(1)
weight = torch.randn(1); weight.requires_grad_()
bias = torch.zeros(1, requires_grad=True)

x = torch.tensor([2.0]); y = torch.tensor([4.0])
pred = x @ weight + bias
loss = (pred - y).pow(2).mean()
loss.backward()
print('autograd dL/dw:', weight.grad, ' dL/db:', bias.grad)
print('手算     dL/dw:', 2 * x * (x * weight + bias - y))
print('手算     dL/db:', 2 * (x * weight + bias - y))

# 再 backward 一次,观察 .grad 累加
loss2 = (x @ weight + bias - y).pow(2).mean()
loss2.backward()
print('第二次 backward 后 dL/dw:', weight.grad)
```

3. **需要观察的现象**:`backward()` 之后 `weight.grad`、`bias.grad` 从 `None` 变成具体数值;连续两次 `backward()` 后 `.grad` 变成原来的两倍。
4. **预期结果**:autograd 输出与两条手算公式**逐位一致**;第二次 `backward()` 后梯度翻倍——这就是必须 `zero_grad` 的原因。
5. 上述数值结论待本地验证(逻辑上由 autograd 文档保证,实际打印值依赖随机种子)。

#### 4.1.5 小练习与答案

**练习 1**:把 L111 的 `weight.requires_grad_()` 注释掉,程序会在哪一步出问题?

> **答案**:`loss.backward()` 时,`weight` 不在计算图中,`weight.grad` 保持 `None`;到 L131 的 `weight -= weight.grad * learning_rate` 时,`None * float` 抛出 `TypeError`。autograd 只为 `requires_grad=True` 的叶子求梯度。

**练习 2**:`with torch.no_grad():` 包住更新操作,除了「不记图」还有什么作用?

> **答案**:对要求梯度的叶子张量做原地更新(`weight -= ...`),若不被 `no_grad` 包住,新版 PyTorch 会直接抛出 `RuntimeError`(a leaf Variable that requires grad is being used in an in-place operation)。`no_grad` 既省去无谓的记图开销,也让原地更新合法。

**练习 3**:为什么 `loss.backward()` 要求 loss 是标量?

> **答案**:反向传播从 \(\partial L/\partial L = 1\) 起步,这个「1」只对标量有定义。若 loss 是向量,需要显式传入与它同形的权重向量(`loss.backward(v)`),即加权求和后再回传。书中 `loss_fn` 用 `.mean()` 保证损失是标量。

### 4.2 nn.Module、nn.Linear 与 nn.Sequential 搭建

#### 4.2.1 概念说明

4.1 节的写法有个明显痛点:参数要自己创建、自己更新、自己清零。`torch.nn` 把这些收进三个抽象:

- **`nn.Module`**:所有网络的基类。子类在 `__init__` 里把层赋给 `self`,Module 就自动登记这些层的参数;调用 `model(x)` 会自动转发到你自己写的 `forward`;`model.parameters()` 产出全部参数,直接喂给优化器,不必逐个列举。
- **`nn.Linear(in_features, out_features)`**:一个仿射变换层 \(\boldsymbol{y} = \boldsymbol{x}\boldsymbol{W}^\top + \boldsymbol{b}\),内部权重形状 `(out, in)`、偏置形状 `(out,)`——与 u11-l1 手写网络的形状约定一致。
- **`nn.Sequential`**:按顺序串接各层的容器,`forward` 就是「数据依次穿过每一层」,连类都可以不写。

#### 4.2.2 核心流程

用 `nn.Module` 搭一个「输入层 → 隐藏层 → 输出层」网络的固定套路:

```text
class Model(nn.Module):
    def __init__(self, 输入维, 隐藏维, 输出维):
        super().__init__()              # 必须先调父类初始化
        self.layer1 = nn.Linear(输入维, 隐藏维)
        self.layer2 = nn.Linear(隐藏维, 输出维)
    def forward(self, x):
        x = self.layer1(x)              # 线性变换
        x = 激活函数(x)                 # 非线性
        x = self.layer2(x)
        x = 输出层激活(x)               # 视任务而定
        return x
```

等价的 `nn.Sequential` 写法:`nn.Sequential(nn.Linear(2, 4), nn.ReLU(), nn.Linear(4, 1), nn.Sigmoid())`——层对象按位置索引(`model[0]` 即第一个 Linear)。

#### 4.2.3 源码精读

**Iris 三分类的自定义 Model 类**:

[ch12/ch12_part2.py:L252-L263](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L252-L263) —— `__init__` 里两个 `nn.Linear` 被赋给 `self.layer1`、`self.layer2`,从此它们的参数归 Module 托管;`forward` 依次做「线性 → `nn.Sigmoid()` → 线性 → `nn.Softmax(dim=1)`」。注意 `nn.Sigmoid()` 与 `nn.Softmax(dim=1)` 在这里是**层的形式**(带括号实例化后调用),与后面 4.4 节的函数形式 `torch.sigmoid`、`torch.softmax` 等价。`dim=1` 表示沿特征维归一化——每个样本自己的三个输出加起来为 1。

[ch12/ch12_part2.py:L265-L275](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L265-L275) —— Iris 有 4 个特征、3 个品种,故 `input_size=4, hidden_size=16, output_size=3`;损失用 `nn.CrossEntropyLoss()`,优化器用 `torch.optim.Adam`,`model.parameters()` 一行把两个层的全部参数交给它——对比 4.1 节手工维护 `weight`/`bias` 两个张量,参数再多也不怕漏。

**XOR 的两个 Sequential 模型(下一章代码,本讲综合实践的素材)**:

[ch13/ch13_part1.py:L264-L267](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L264-L267) —— 单层版本 `nn.Sequential(nn.Linear(2, 1), nn.Sigmoid())`:没有隐藏层,本质仍是 4.1 节的线性模型 + sigmoid,训练 200 轮也学不动 XOR(线性不可分)。

[ch13/ch13_part1.py:L331-L338](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L331-L338) —— 多层版本 `Linear(2,4) → ReLU → Linear(4,4) → ReLU → Linear(4,1) → Sigmoid`:两个宽度为 4 的隐藏层,隐藏层激活换成了 ReLU,输出层 sigmoid 配合二分类。同一个 `train` 函数重训,准确率接近 100%。**隐藏单元宽度正是本讲综合实践要扫描的量。**

> **注意(关于 Softmax + CrossEntropyLoss 的搭配)**:PyTorch 官方文档明确 `nn.CrossEntropyLoss` 内部已组合 `LogSoftmax` 与 `NLLLoss`,规范用法是把**未归一化的 logits** 直接喂给它。书在 L262 的 `forward` 末尾显式加了 `Softmax(dim=1)` 再传入 `CrossEntropyLoss`,代码可以运行(`argmax` 预测因 softmax 单调而不受影响),但梯度并非教科书式推导,属于本书为教学直观采用的简化写法。第 13 章起改用更规范的 `Sigmoid + BCELoss` 搭配(BCELoss 的输入才是概率)。

#### 4.2.4 代码实践

1. **实践目标**:验证 `nn.Linear` 的参数形状符合「行 = 目标单元、列 = 来源特征」,并学会数参数量。
2. **操作步骤**(**示例代码**;`Model` 取书中 L252-L263 的定义):

```python
import torch, torch.nn as nn
torch.manual_seed(1)
model = Model(input_size=4, hidden_size=16, output_size=3)
for name, p in model.named_parameters():
    print(name, p.shape)
print('参数总数:', sum(p.numel() for p in model.parameters()))
```

3. **需要观察的现象**:打印出的四个形状分别是 `layer1.weight (16,4)`、`layer1.bias (16,)`、`layer2.weight (3,16)`、`layer2.bias (3,)`。
4. **预期结果**:参数总数 = \(16\times4+16+3\times16+3 = 131\)。若把 `hidden_size` 改成 32,总数变为 \(32\times4+32+3\times32+3=259\)。
5. 精确的初始化数值随种子变化,但形状与计数是确定性的;待本地验证。

#### 4.2.5 小练习与答案

**练习 1**:`nn.Sequential` 与自定义 `nn.Module` 子类各适合什么场景?

> **答案**:`Sequential` 适合纯直线堆叠的结构(如本讲 XOR 网络),几行搞定、按下标访问层;自定义子类适合有分支、拼接、条件判断的结构(第 13 章的 `NoisyLinear` 与动态图示例就是后者),也更方便命名、复用与调试。

**练习 2**:为什么 `forward` 里隐藏层之后可以没有激活,而书上强调多层网络必须有非线性?

> **答案**:两个线性层复合 \(W_2(W_1 x + b_1) + b_2 = (W_2 W_1)x + (W_2 b_1 + b_2)\) 仍是一个线性变换,多层塌缩成一层——这正是 u11-l1 讲过的结论在 PyTorch 里的体现。非线性激活是深度网络表达能力的来源。

**练习 3**:把书中 Iris 模型的 `hidden_size` 从 16 增大到 32,参数量增加多少?

> **答案**:增加 \(259-131=128\)。隐藏层加宽同时影响两侧 Linear:输入侧 \(32\times4+32=160\) 对 \(16\times4+16=80\),输出侧 \(3\times32+3=99\) 对 \(3\times16+3=51\),合计多 128 个参数。

### 4.3 训练三步:`loss_fn + model(xb)` 与 backward/step/zero_grad

#### 4.3.1 概念说明

`torch.optim` 的优化器(如 `SGD`、`Adam`)持有模型参数的引用和学习率,把 4.1 节手动做的「参数减学习率乘梯度」批量化:`optimizer.step()` 一次更新它管理的**所有**参数;`optimizer.zero_grad()` 一次清空**所有**参数的 `.grad`。

`zero_grad` 为什么必不可少:**PyTorch 的 `.grad` 是累加(accumulate)语义**——每次 `backward()` 把新梯度**加**到 `.grad` 上而不是覆盖它。这个设计为「梯度累积」(多个小批合并成一次逻辑更新)这类高级用法留了口子,代价是常规训练循环里每批更新后必须清零,否则梯度越滚越大、方向失真。

#### 4.3.2 核心流程

几乎全书通用的五步训练内循环(书中注释即编号 1~5):

```text
for x_batch, y_batch in train_dl:
    pred  = model(x_batch)          # ① 前向:预测,同时建图
    loss  = loss_fn(pred, y_batch)  # ② 损失:图的终点
    loss.backward()                 # ③ 反向:填充所有参数的 .grad
    optimizer.step()                # ④ 更新:θ ← θ − lr·θ.grad
    optimizer.zero_grad()           # ⑤ 清零:防止下次 backward 累加
```

`zero_grad` 的合法位置是「**上一次 step 之后、下一次 backward 之前**」的任意点——书里放在 step 之后,很多项目放在循环开头,两者等价;唯独不能插在 backward 与 step 之间(见练习 3)。

#### 4.3.3 源码精读

**`torch.nn` + `torch.optim` 重写线性回归**:

[ch12/ch12_part2.py:L170-L176](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L170-L176) —— `nn.Linear(1, 1)` 一个层替代手写的 `weight`/`bias`;`nn.MSELoss(reduction='mean')` 替代手写 `loss_fn`;`torch.optim.SGD(model.parameters(), lr=learning_rate)` 替代手动的 `no_grad` 更新块。三行各司其职:模型、损失、优化器。

[ch12/ch12_part2.py:L178-L196](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L178-L196) —— 五步循环的标准形态,书中注释 `# 1. Generate predictions` 到 `# 5. Reset the gradients to zero` 与上面流程一一对应;L201 的 `model.weight.item()` 说明 `nn.Linear` 的参数以属性形式暴露(`weight`、`bias`),`.item()` 把单元素张量转成 Python 数值。

**Iris 训练循环与逐 epoch 统计**:

[ch12/ch12_part2.py:L284-L298](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L284-L298) —— 同样是「前向 → 损失 → backward → step → zero_grad」五步;L288 的 `y_batch.long()` 把标签转为整型(`CrossEntropyLoss` 的要求);L293 用 `loss.item()*y_batch.size(0)` 把批损失按批大小加权累计,L297 除以 `len(train_dl.dataset)`(样本总数)得到该 epoch 的平均损失——这样最后不满一批的尾巴也不会被高估;L294 用 `torch.argmax(pred, dim=1)` 把概率向量变回类别编号再统计准确率。

**XOR 的通用 `train` 函数(综合实践复用)**:

[ch13/ch13_part1.py:L282-L306](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L282-L306) —— 同一套三步(`backward`/`step`/`zero_grad` 在 L291-L293)被封装成接受任意模型的 `train` 函数,并额外在每个 epoch 结束后在验证集上评估(`pred>=0.5` 二值化后比对标签)。模型换了、损失换了(`BCELoss`),训练循环骨架不变——这正是 4.3.2 通用流程的价值。

#### 4.3.4 代码实践

1. **实践目标**:体会 `zero_grad` 缺席的后果,确认五步顺序中 ③④⑤ 的可交换边界。
2. **操作步骤**(基于书中 Iris 循环 L284-L298 改两处,**示例代码**):
   - 实验 A:把 L291 的 `optimizer.zero_grad()` 注释掉,跑完 100 个 epoch,保存 `loss_hist`;
   - 实验 B:把 `optimizer.zero_grad()` 从 step 之后移到 `loss.backward()` **之前**,再跑一遍。
3. **需要观察的现象**:实验 A 的损失曲线相对正常版本明显偏高或剧烈震荡(梯度按批累加,等效学习率不断放大);实验 B 与正常版本**几乎一致**(清零发生在 backward 之前同样满足「下一次 backward 前已清零」)。
4. **预期结果**:实验 A 训练变差甚至发散;实验 B 结果不变。具体曲线形态待本地验证。
5. 若想快速看到效果,可把 `num_epochs` 降到 10、只比较最终损失量级。

#### 4.3.5 小练习与答案

**练习 1**:既然 `zero_grad` 这么重要,PyTorch 为什么不默认每次 `backward()` 自动清零?

> **答案**:累加语义是有意设计:梯度累积(gradient accumulation) technique 依赖它——显存不够时用多个小批的梯度之和模拟一个大批,中途不能清零。PyTorch 把「何时清零」交给使用者,换来这类高级用法的自由。

**练习 2**:`optimizer.step()` 与 `optimizer.zero_grad()` 可以交换顺序吗?

> **答案**:不能随便换。若先 `zero_grad` 再 `step`,刚由 backward 算出的梯度会被清掉,step 拿到全零梯度、参数原地不动。正确的约束是:③ backward 必须在 ④ step 之前;⑤ zero_grad 必须在 ④ step 之后(等价地,在下一轮 ③ backward 之前)。书中「step 后紧跟 zero」以及「循环开头先 zero」都是满足约束的安全写法。

**练习 3**:Iris 循环统计准确率时为什么写 `torch.argmax(pred, dim=1) == y_batch`,而不是直接比较概率向量?

> **答案**:`pred` 是每样本 3 个类别的概率(softmax 输出),`y_batch` 是类别编号;`argmax(dim=1)` 把概率最大的列号取出来,才与整型标签同构可比。这与 u3-l1 中 sklearn `predict` 内部「逐行取最大」的逻辑相同。

### 4.4 激活函数对比:logistic、softmax 与 tanh

#### 4.4.1 概念说明

激活函数分两个岗位:**隐藏层激活**提供非线性(否则多层塌缩成一层);**输出层激活**负责把 logits 变成与任务匹配的形式——二分类要一个 `(0,1)` 概率(logistic),多分类要一**组**加和为 1 的概率(softmax),回归要不设限的数值(不用激活)。

本模块的核心问题是学习目标里的那一条:**多分类为什么必须 softmax,而不能对每个输出单元各自套 sigmoid?** 因为 sigmoid 把每个单元**独立地**压到 `(0,1)`,三个输出之间没有任何约束——三个「概率」加起来可能是 2.38,这不是概率分布。softmax 用分母 \(\sum_k e^{z_k}\) 把所有输出**联动归一**,保证 \(\sum_j p_j = 1\) 且保序(`argmax` 不变)。

#### 4.4.2 核心流程

三个激活函数的数学定义与关键性质:

\[ \phi_{\text{logistic}}(z) = \frac{1}{1+e^{-z}}, \qquad \mathrm{softmax}(z_j) = \frac{e^{z_j}}{\sum_{k=1}^{K} e^{z_k}}, \qquad \tanh(z) = \frac{e^{z}-e^{-z}}{e^{z}+e^{-z}} \]

- logistic:值域 `(0,1)`,输出可读作 \(P(y=1|x)\);导数为 \(\phi(1-\phi)\)。
- softmax:\(K\) 个输出归一成分布,具有平移不变性 \(\mathrm{softmax}(z+c)=\mathrm{softmax}(z)\)(加减常数不影响结果)。
- tanh:值域 `(-1,1)`,**零中心**;与 logistic 有恒等关系 \(\tanh(z) = 2\,\phi_{\text{logistic}}(2z) - 1\)。零中心让隐藏层输出的均值更接近 0,历史上比 logistic 更受青睐;现代隐藏层默认 ReLU(书中 L497 起介绍,第 13 章的 XOR 网络即用 ReLU)。

#### 4.4.3 源码精读

**单个 sigmoid 输出没问题**:

[ch12/ch12_part2.py:L381-L394](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L381-L394) —— 手写 `net_input`(点积)、`logistic`(\(1/(1+e^{-z})\))、`logistic_activation` 三函数,对一个 3 维输入输出单个值 \(P(y=1|x)\)。二分类场景下一个概率自洽。

**多个 sigmoid 输出「加不起来」——书中关键的对比实验**:

[ch12/ch12_part2.py:L399-L414](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L399-L414) —— 构造形状 `(3, 4)` 的权重 `W`(3 个输出单元、4 个隐藏输入,首列是偏置单元)与输入 `A`,算净输入 `Z = np.dot(W, A[0])` 得 3 个数,再逐个过 `logistic` 得 `y_probas`。三个输出互相独立,手算约为 `[0.86, 0.68, 0.84]`,**和约 2.38,远大于 1**——它们不是概率分布。

[ch12/ch12_part2.py:L419-L420](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L419-L420) —— 妥协做法:只能退回对未激活的净输入 `Z` 做 `argmax` 选标签,此时「概率」的意义已经丢失。

**softmax 修复归一性**:

[ch12/ch12_part2.py:L427-L438](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L427-L438) —— 手写 `softmax`(\(e^z / \sum e^z\))作用于同一个 `Z`,输出约为 `[0.45, 0.16, 0.39]`,`np.sum(y_probas)` 精确等于 1;L438 用 `torch.softmax(torch.from_numpy(Z), dim=0)` 验证 PyTorch 官方实现与手写一致。

**tanh 与 logistic 并排对比**:

[ch12/ch12_part2.py:L446-L472](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L446-L472) —— 手写 `tanh`(用 \(e^z, e^{-z}\) 的表达式),在 \(z\in(-5,5)\) 网格上把 tanh 与 logistic 两条曲线画进同一张图:logistic 夹在 `(0,1)`、tanh 夹在 `(-1,1)` 且过原点,形状是「拉大再下移」的同一族 S 曲线(即恒等式 \(\tanh = 2\phi(2z)-1\))。

[ch12/ch12_part2.py:L477-L501](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L477-L501) —— API 对照:`np.tanh` / `torch.tanh` / `expit`(scipy 的 sigmoid)/ `torch.sigmoid` / `torch.relu` 一行一个验证等价,其中 `torch.relu` 与随后的 ReLU 示意图引出下一章隐藏层的主力激活。

#### 4.4.4 代码实践

1. **实践目标**:亲手复现「多 sigmoid 不可归一、softmax 可归一」的对比。
2. **操作步骤**(直接运行书中代码 L399-L438,或抄成脚本,**示例代码**):

```python
import numpy as np, torch
W = np.array([[1.1, 1.2, 0.8, 0.4],
              [0.2, 0.4, 1.0, 0.2],
              [0.6, 1.5, 1.2, 0.7]])
A = np.array([[1, 0.1, 0.4, 0.6]])
Z = np.dot(W, A[0])

sig = 1.0/(1.0 + np.exp(-Z))
sm  = np.exp(Z)/np.sum(np.exp(Z))
print('sigmoid 各自输出:', sig, ' 和 =', sig.sum())
print('softmax   输出:', sm,  ' 和 =', sm.sum())
print('torch.softmax:', torch.softmax(torch.from_numpy(Z), dim=0))
```

3. **需要观察的现象**:`sig.sum()` 明显大于 1(约 2.38);`sm.sum()` 为 1.0(浮点误差量级);`torch.softmax` 与手写逐位一致。
4. **预期结果**:如上;两组概率的 `argmax` 相同(softmax 保序,第一个单元最大)。
5. 具体小数位待本地验证(整值可手算核对:Z = [1.78, 0.76, 1.65])。

#### 4.4.5 小练习与答案

**练习 1**:既然 `argmax` 结果一样,多分类直接用「每单元 sigmoid + argmax」不行吗?

> **答案**:预测标签确实一样(两种激活都保序),但**训练**不一样:softmax 的梯度显式包含「其它类别的竞争」(分母联动所有单元),交叉熵 + softmax 是配套推导的凸组合;逐单元 sigmoid 把多分类拆成多个独立二分类,概率不归一,损失语义与「互斥类别」不符。推理看 argmax、训练看梯度,不能只顾前者。

**练习 2**:验证 \(\tanh(z) = 2\phi_{\text{logistic}}(2z) - 1\) 在 \(z=0\) 与 \(z\to\infty\) 两处成立。

> **答案**:\(z=0\):左边 \(\tanh(0)=0\),右边 \(2\times0.5-1=0\);\(z\to\infty\):左边趋于 1,右边 \(2\times1-1=1\)。该恒等式说明 tanh 就是「斜率加倍、下移到零中心」的 logistic,这也解释了为什么两者曲线形状同族。

**练习 3**:书中 Iris 模型的隐藏层用 `nn.Sigmoid()`,第 13 章 XOR 用 `nn.ReLU()`,隐藏层激活的取舍趋势是什么?

> **答案**:sigmoid/tanh 在两侧饱和(导数趋于 0),深网络里梯度连乘后容易「消失」;ReLU 对正输入导数恒为 1,缓解梯度消失且计算便宜,是现代隐藏层的默认起点。浅层教学网络上两者都能训练,但 XOR 这类需要折线边界的任务 ReLU 更直接(每个 ReLU 单元贡献一个折点)。

### 4.5 torch.save / torch.load:两种保存方式与 device 转移

#### 4.5.1 概念说明

训练好的模型要能存盘复用。PyTorch 允许保存两种对象:

- **整个模型对象**(`torch.save(model, path)`):用 pickle 把「类的结构 + 参数」一起序列化。加载时 `torch.load(path)` 直接得到现成模型,不用重新搭;但序列化内容依赖类的定义位置与版本,跨项目/跨目录结构容易失效。
- **仅参数字典 `state_dict`**(`torch.save(model.state_dict(), path)`):`state_dict` 是「参数名 → 张量」的有序字典(如 `layer1.weight`),只存权重。加载时先按原结构实例化模型,再 `model_new.load_state_dict(torch.load(path))` 灌入权重。这是**官方推荐**方式:文件小、与代码结构解耦,便于迁移。

加载后调用 `model_new.eval()` 把模块切到评估模式(影响 dropout、批归一化等层的行为;本讲模型只有 Linear 与激活,行为不变,但这是值得养成的习惯)。

**关于 device 转移**:`state_dict` 里的张量带着保存时所在的设备信息。若在 GPU 上保存、CPU 上加载,PyTorch 官方提供 `torch.load(path, map_location='cpu')`(或反向 `'cuda'`)在加载时搬运;张量本身的跨设备搬运则用 u12-l1 讲过的 `to(device)`(复制语义)。**注意:书中本节的保存/加载代码全在 CPU 上运行,仓库代码未演示 `map_location`**——该参数属 PyTorch 官方 API,此处作为知识补充,后续在第 13 章工程化讲义中还会回到设备话题。

#### 4.5.2 核心流程

```text
# 方式一:保存整个模型
torch.save(model, 'model.pt')
model_new = torch.load('model.pt')     # 无需事先搭结构
model_new.eval()

# 方式二:保存 state_dict(官方推荐)
torch.save(model.state_dict(), 'model_state.pt')
model_new = Model(同样的构造参数)      # 必须先搭同结构
model_new.load_state_dict(torch.load('model_state.pt'))
```

#### 4.5.3 源码精读

[ch12/ch12_part2.py:L341-L348](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L341-L348) —— 方式一:`torch.save(model, path)` 把整个 Iris 模型对象写入 `iris_classifier.pt`;`torch.load(path)` 读回 `model_new`,紧接着 `model_new.eval()` 切评估模式。

[ch12/ch12_part2.py:L353-L358](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L353-L358) —— 用读回的模型对同一份测试数据重新预测并打印准确率,验证「存取无损」。

[ch12/ch12_part2.py:L363-L370](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L363-L370) —— 方式二:先 `torch.save(model.state_dict(), path)` 只存参数字典;加载侧必须先用**相同的构造参数**(`input_size, hidden_size, output_size`)新建 `Model`,再 `load_state_dict(torch.load(path))` 把权重灌进去。结构对不上会直接报尺寸不匹配错误。

两种方式的对比小结:

| 维度 | 整个模型 | state_dict |
| --- | --- | --- |
| 保存内容 | 类结构 + 参数(经 pickle) | 参数名 → 张量 的字典 |
| 加载代码 | `torch.load(path)` 一步到位 | 先 `Model(...)` 再 `load_state_dict` |
| 对类定义的依赖 | 强(加载环境须能找到原类) | 弱(只需结构一致) |
| 官方态度 | 不推荐用于长期保存 | 推荐 |

#### 4.5.4 代码实践

1. **实践目标**:跑通两种保存/加载,确认精度一致;再观察结构不匹配时的报错。
2. **操作步骤**(基于书中 L326-L370,**示例代码**):先按 4.3 训练好 Iris 模型并评估测试准确率;依次执行两种保存与加载,分别再次评估;最后故意构造 `Model(input_size, hidden_size=8, output_size)` 并对其 `load_state_dict`,观察报错。
3. **需要观察的现象**:两种方式加载后的测试准确率与训练结束时的值**完全相同**;`hidden_size=8` 的模型加载 16 宽的权重时报 `size mismatch` 类错误。
4. **预期结果**:同一模型两次加载精度一致(书中该模型测试准确率约 0.98,具体数值待本地验证);错配加载必然失败。
5. `state_dict` 文件通常远小于整模型文件,可在实践时用 `os.path.getsize` 对比(待本地验证)。

#### 4.5.5 小练习与答案

**练习 1**:为什么官方推荐 `state_dict` 而不是整个模型?

> **答案**:整模型经 pickle 序列化,与类的定义路径、代码版本强耦合——重构改名、换环境都可能加载失败,还有反序列化任意代码的安全风险;`state_dict` 只是张量字典,结构由当前代码决定,兼容性好、文件也更小。

**练习 2**:加载之后为什么建议 `model_new.eval()`?

> **答案**:`eval()` 把模块标记为推理模式,改变 dropout(停止随机丢弃)、批归一化(用累计统计量)等层的前向行为。本讲的 Linear+Sigmoid/Softmax 在两种模式下行为相同,但训练更深的网络(第 14 章 CNN 含 dropout)时忘记 `eval()` 会导致评估结果随机波动。

**练习 3**:在 GPU 上训练并保存的模型,搬到只有 CPU 的机器上推理,正确的姿势是什么?

> **答案**:加载时用 `torch.load(path, map_location='cpu')`,把权重直接映射到 CPU(也可用 `map_location=torch.device('cuda')` 反向);之后如需设备间搬运用 `tensor.to(device)`。注意仓库书中代码未演示该参数,此处依据 PyTorch 官方文档。

## 5. 综合实践:XOR 隐藏单元宽度实验

**任务**:书中第 13 章用两个宽度为 4 的隐藏层解决了 XOR 分类。本实践把隐藏单元宽度(两个隐藏层同时)改成 \(h \in \{1, 2, 4, 8\}\) 各训练一次,记录最终训练准确率并画柱状图,验证理论结论:**XOR 至少需要 2 个隐藏单元**。

**为什么 1 个隐藏单元注定失败?** 宽度为 1 时,所有中间激活都是标量:输入先被压到一维(\(h_1=\mathrm{relu}(w^\top x + b)\)),再怎么组合,最终决策边界只能退化回**一条直线**(或半平面)——而 XOR 的两块正类区域位于对角象限、互不相邻,一条直线无论如何切不开。宽度 ≥ 2 时,两个 ReLU 单元可以拼出「折线/带状」区域,才有表示 XOR 的可能。

**操作步骤**(以下为**示例代码**,基于 [ch13/ch13_part1.py:L228-L341](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L228-L341) 的数据生成、`train` 循环骨架与模型结构改写):

```python
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import TensorDataset, DataLoader
import matplotlib.pyplot as plt

# 1) XOR 数据:与书 L228-L238 相同
np.random.seed(1)
torch.manual_seed(1)
x = np.random.uniform(low=-1, high=1, size=(200, 2))
y = np.ones(len(x))
y[x[:, 0] * x[:, 1] < 0] = 0          # 两坐标异号 → 类 0
n_train = 100
x_train = torch.tensor(x[:n_train, :], dtype=torch.float32)
y_train = torch.tensor(y[:n_train], dtype=torch.float32)

train_dl = DataLoader(TensorDataset(x_train, y_train), 2, shuffle=True)
loss_fn = nn.BCELoss()

def make_model(h):
    """对应书 L331-L338,隐藏层宽度改为 h"""
    return nn.Sequential(
        nn.Linear(2, h), nn.ReLU(),
        nn.Linear(h, h), nn.ReLU(),
        nn.Linear(h, 1), nn.Sigmoid())

def train(model, optimizer, num_epochs, train_dl):
    for epoch in range(num_epochs):
        for x_batch, y_batch in train_dl:
            pred = model(x_batch)[:, 0]     # 与书 train() L289 相同
            loss = loss_fn(pred, y_batch)
            loss.backward()                 # 三步:反向
            optimizer.step()                # 更新
            optimizer.zero_grad()           # 清零
    with torch.no_grad():                   # 评估不需要梯度
        pred = model(x_train)[:, 0]
        return (((pred >= 0.5).float() == y_train).float()).mean().item()

results = {}
for h in [1, 2, 4, 8]:
    torch.manual_seed(1)                    # 每次相同初始化运气
    model = make_model(h)
    optimizer = torch.optim.SGD(model.parameters(), lr=0.015)  # 书 L341 的 lr
    results[h] = train(model, optimizer, 200, train_dl)
    print(f'hidden={h}: 最终训练准确率 = {results[h]:.3f}')

# 2) 柱状图
plt.bar([f'h={h}' for h in results], list(results.values()))
plt.ylabel('Training accuracy'); plt.ylim(0, 1.05)
plt.axhline(1.0, color='gray', linestyle=':')
plt.show()
```

**需要观察的现象与预期结果**:

| 隐藏宽度 h | 预期最终训练准确率 | 原因 |
| --- | --- | --- |
| 1 | 约 0.5 ~ 0.7,柱子明显矮 | 一维瓶颈,边界退化为一条直线,学不动 XOR |
| 2 | 通常 0.9 ~ 1.0 | 理论最小配置;个别种子/初始化可能收敛慢,可换种子复跑 |
| 4 | 接近 1.0 | 书中原配置(figure 13_04 中训练准确率接近 100%) |
| 8 | 接近 1.0,通常最稳 | 容量充裕 |

上表为理论预期与书中图形的定性推断,**具体数值待本地验证**;200 epoch × 4 个模型在 CPU 上通常几分钟内完成,可先把 `num_epochs` 降到 50 试跑流程。

**延伸思考**(选做):把 `lr` 从 0.015 改成 0.001 重跑,观察小学习率下各宽度的差距是否被「训练不充分」掩盖——这会把本讲与 u2-l2 的学习率实验串起来。

## 6. 本讲小结

- **autograd 机制**:前向计算自动建计算图;`loss.backward()` 沿图反向、按链式法则把梯度**累加**进每个 `requires_grad=True` 叶子张量的 `.grad`;手动更新须放在 `torch.no_grad()` 里,取数值给 numpy/matplotlib 前用 `.detach()`。
- **`zero_grad` 的存在理由**:`.grad` 是累加语义(为梯度累积等用法保留),每批更新后不清零,梯度会跨批叠加、训练失真;它的合法位置是「上一次 step 之后、下一次 backward 之前」。
- **五步训练循环**:前向 `model(xb)` → `loss_fn(pred, yb)` → `loss.backward()` → `optimizer.step()` → `optimizer.zero_grad()`,这是贯穿全书(ch12 Iris、ch13 XOR 乃至后续 CNN/RNN)的通用骨架。
- **搭建方式**:`nn.Linear(in, out)` 权重形状 `(out, in)`;`nn.Sequential` 适合直线堆叠,自定义 `nn.Module` 子类适合带分支/条件的前向;书中 Iris 在 forward 里显式 `Softmax` 又用 `CrossEntropyLoss`(后者内含 log-softmax),属可运行的教学简化写法。
- **输出层激活匹配任务**:二分类 sigmoid(配 BCELoss)、多分类 softmax(配交叉熵);对多个输出单元各自套 sigmoid 得到的值互相独立、和不为 1,不能当概率分布——softmax 用联动归一修复这一点。
- **模型保存**:整个模型对象(pickle,依赖类定义)与 `state_dict`(参数字典,官方推荐、需先搭同结构再 `load_state_dict`)两种方式;加载后 `eval()`;跨设备用 `torch.load(..., map_location=...)`(书中未演示,官方 API)。

## 7. 下一步学习建议

本讲把「搭模型 + 自动求导 + 优化器 + 保存加载」的 PyTorch 基本盘配齐了。下一讲 **u13-l1(动态计算图与自定义层)** 将深入本讲埋下的三条线索:① `requires_grad` 与 `.grad_fn` 的更多细节,以及用 `compute_z` 式例子理解自动微分;② 仿照 `NoisyLinear` 写带自定义参数与初始化的层,并体会 `training`/`eval` 模式的行为差异;③ 在 `forward` 里写 `if` 分支,理解 PyTorch「动态图」相对静态框架的优势。之后再进入 **u13-l2(项目结构与 checkpoint)**——那会把本讲的 `state_dict` 保存扩展成完整的训练工程实践。建议阅读顺序:`ch13/ch13_part1.py` 前半部分(autograd 与 `MyModule`/`NoisyLinear`)→ 本讲引用过的 XOR 一节完整上下文 → `ch13/ch13_part2.py` 的模块化组织。
