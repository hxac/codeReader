# 动态计算图与自定义层

## 1. 本讲目标

上一讲(u12-l3)我们已经会用 `nn.Linear`/`nn.Sequential` 搭网络、用 `loss.backward()` + `optimizer.step()` 训练 XOR 分类器,但一直在「使用」PyTorch 给好的积木。本讲把积木拆开看内部:PyTorch 的**动态计算图**是怎么在运行中长出来的,以及当现成积木不够用时,如何亲手写一个**自定义层**。学完本讲你应该能够:

1. 说清「动态图(define-by-run)」与静态图的差别:PyTorch 的计算图由每一次前向**实际执行的运算**即时构建,`forward` 里写 `if` 分支、写循环都完全合法。
2. 用 `requires_grad`、`.grad_fn`、`.grad` 三个属性诊断一张计算图:哪些张量是叶子、哪些运算被记录、梯度回传到了谁。
3. 解释**参数注册**的规则:只有被包成 `nn.Parameter`(或经由子模块)的张量才会被 `model.parameters()`、`model.to(device)`、`state_dict()` 认领;把普通张量直接挂在属性上会「静默失踪」。
4. 掌握自定义层的固定套路:`__init__` 里创建并注册参数、`forward` 里定义计算,并以 `NoisyLinear` 为例理解训练/评估两种模式下行为不同的层。
5. 完成综合实践:仿照 `NoisyLinear` 实现一个 `NoisyReLU` 层,验证它在 train/eval 模式下输出不同,并把它组装进自定义模型。

## 2. 前置知识

- **autograd 五步循环(u12-l3)**:前向、算损失、`loss.backward()`、`optimizer.step()`、`optimizer.zero_grad()`。上一讲我们把 `backward()` 当黑盒用;本讲要看它沿着什么样的图回传。
- **计算图与叶子张量(u12-l3)**:叶子张量是直接创建(而非运算得到)的张量,通常就是权重与偏置;`backward()` 把梯度**累加**进每个 `requires_grad=True` 叶子的 `.grad`。本讲新增两个观察工具:`.grad_fn`(中间节点身上的「出生证明」)与动态图的分支能力。
- **张量的 `to()`(u12-l1)**:`to(device)` 把张量搬到 CPU/GPU,跨设备是**复制**语义、返回新张量。本讲把它升级到模块级别:`model.to(device)` 能搬走哪些张量,取决于参数注册。
- **权重形状约定(u11-l1、u12-l3)**:`nn.Linear(in, out)` 的权重形状是 `(out, in)`;手写 `NeuralNetMLP` 的 `weight_h` 也是 `(num_hidden, num_features)`、靠 `.T` 对齐。本讲的 `NoisyLinear` 故意换了一种存法 `(in, out)`,数学上完全等价——帮你破除「只有一种形状约定」的错觉。
- **训练/评估模式**:很多层(Dropout、BatchNorm)在训练和评估时行为不同。PyTorch 内置层靠 `model.train()`/`model.eval()` 切换内部的 `self.training` 标志;本书第 13 章为了把机制讲透,用**显式 `training` 参数**演示同一思想。
- **XOR 数据集(u12-l3)**:200 个二维点、按异或关系打标签,`train_dl` 是 `batch_size=2` 的 DataLoader。本讲的 `MyModule`、`MyNoisyModule` 都在这份数据上训练。

## 3. 本讲源码地图

本讲主线是 [ch13/ch13_part1.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py)(第 13 章「PyTorch 机制深入」的第 1 部分);CPU/GPU 转移的工程示例在同章第 4 部分:

| 源码位置 | 作用 |
| --- | --- |
| [ch13/ch13_part1.py:L86-L94](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L86-L94) | `compute_z`:用 `torch.sub/mul/add` 搭一条最简单的计算链,演示动态图对任意形状输入的适应 |
| [ch13/ch13_part1.py:L101-L133](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L101-L133) | `requires_grad` 的创建时指定、事后查询与原地开关 `requires_grad_()`;Xavier 初始化 |
| [ch13/ch13_part1.py:L139-L145](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L139-L145) | 反例 `MyModule`:普通张量属性**不会**被注册为参数 |
| [ch13/ch13_part1.py:L155-L174](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L155-L174) | 裸 autograd 最小示例:`backward()` 填 `.grad`,并用手算式验证 |
| [ch13/ch13_part1.py:L228-L308](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L228-L308) | XOR 数据、DataLoader 与通用 `train` 函数(u12-l3 已精读,本讲复用) |
| [ch13/ch13_part1.py:L375-L398](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L375-L398) | 正例 `MyModule`:`nn.ModuleList` 组织子模块 + 自定义 `forward`/`predict` |
| [ch13/ch13_part1.py:L448-L480](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L448-L480) | 本讲主角 `NoisyLinear` 自定义层(`nn.Parameter` 注册 + 带条件分支的 `forward`)及其验证代码 |
| [ch13/ch13_part1.py:L486-L544](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L486-L544) | `MyNoisyModule`:把自定义层组装成模型,训练循环显式传 `True` |
| [ch13/ch13_part4_ignite.py:L152-L154](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py#L152-L154) | `device = "cuda" if torch.cuda.is_available() else "cpu"` 与 `model.to(device)` 的工程写法 |

## 4. 核心概念与源码讲解

### 4.1 动态计算图:`compute_z` 如何在运行中建图

#### 4.1.1 概念说明

**计算图**(computational graph)把一次计算记成有向无环图:节点是张量,边是「谁参与了哪个运算」。自动微分的全部准备 work,就是在前向计算时把这张图记下来。

框架分两大流派:

- **静态图**(define-then-run,如早期 TensorFlow 1.x):先声明一张固定的图,再反复喂数据执行。图结构在声明时就定死,想按数据改结构得用特殊的控制流算子。
- **动态图**(define-by-run,PyTorch):**每次前向运算时即时建图**。Python 代码跑到哪,图就建到哪;这次走 `if` 的真分支、下次走假分支,两次得到的干脆是两张不同的图。因为图随 Python 控制流自然生长,它又叫「命令式/define-by-run」风格。

动态图对写模型的人意味着:你可以用最普通的 Python——`if`、`for`、函数递归——来定义网络结构,调试时随时 `print` 任何中间张量。本讲 4.5 的 `NoisyLinear.forward` 里的 `if training:` 分支,正是动态图最典型的用法。

#### 4.1.2 核心流程

`compute_z` 是一条三步链:

```
输入 a, b, c
   │
   ├─ r1 = a − b        (torch.sub)
   ├─ r2 = r1 × 2       (torch.mul,标量 2 被广播)
   └─ z  = r2 + c       (torch.add)
```

- 若输入中有任一张量 `requires_grad=True`,则 `r1、r2、z` 身上会各挂一个 `.grad_fn`(记录自己由哪个运算生成),一张小图就建好了。
- 若所有输入都不要求梯度(仓库示例的默认调用),PyTorch 不记录任何东西,`z.grad_fn` 为 `None`——**图是否被构建取决于参与运算的张量,而不是运算本身**。
- 动态图的「动态」还体现在:同一份 `compute_z` 代码,标量、1 阶、2 阶张量输入都能跑,每次调用各建各的图,互不相干。

#### 4.1.3 源码精读

[对应位置 1] [ch13/ch13_part1.py:L86-L90](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L86-L90) 定义了 `compute_z`:三个函数式的张量运算 `torch.sub(a, b)` → `torch.mul(r1, 2)` → `torch.add(r2, c)` 串成一条链。这就是「前向代码即建图代码」——没有任何单独的「声明图」步骤。

[对应位置 2] [ch13/ch13_part1.py:L92-L94](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L92-L94) 用三种形状的输入分别调用它:`torch.tensor(1)`(标量)、`torch.tensor([1])`(1 阶)、`torch.tensor([[1]])`(2 阶),输出形状随输入自动适配。注意这里创建输入时**没有**指定 `requires_grad`,所以三次调用只是算了数值、并未真正建图——这正好留给我们下面做实验。

#### 4.1.4 代码实践

1. **实践目标**:亲眼看到「有梯度需求的输入才会触发建图」,并学会用 `.grad_fn` 检查图的结构。
2. **操作步骤**(示例代码,可在 notebook 里直接运行):

   ```python
   import torch

   def compute_z(a, b, c):           # 与 ch13_part1.py L86-L90 相同
       r1 = torch.sub(a, b)
       r2 = torch.mul(r1, 2)
       z = torch.add(r2, c)
       return z

   # 情形一:所有输入都不要求梯度(与仓库 L92-L94 的调用一致)
   z0 = compute_z(torch.tensor(1.), torch.tensor(2.), torch.tensor(3.))
   print(z0.requires_grad, z0.grad_fn)   # 预期:False None

   # 情形二:给 a 打开梯度需求
   a = torch.tensor(1., requires_grad=True)
   z1 = compute_z(a, torch.tensor(2.), torch.tensor(3.))
   print(z1.requires_grad)               # 预期:True(梯度需求沿图传播)
   print(z1.grad_fn)                     # 预期:AddBackward0
   print(z1.grad_fn.next_functions)      # 沿图回走一步,能看到 MulBackward0
   ```

3. **需要观察的现象**:`z0` 与 `z1` 数值相同,但只有 `z1` 挂着 `grad_fn`;`z1.grad_fn` 是最后一步运算(加法)的记录节点,顺着 `next_functions` 可以走回乘法、减法。
4. **预期结果**:情形一打印 `False None`;情形二打印 `True` 与 `AddBackward0` 一类的对象(具体类名以你本地 PyTorch 版本的输出为准)。
5. 本实践结论可由 autograd 语义直接推出;若想核对具体类名,待本地验证。

#### 4.1.5 小练习与答案

**练习 1**:如果给 `b` 也设置 `requires_grad=True`,而 `a` 不设,`z` 会有 `grad_fn` 吗?
**答案**:会。建图条件是「参与运算的张量中**至少有一个**要求梯度」,梯度需求会沿运算链向下游传播;至于梯度回传时流向谁(`a` 还是 `b` 的 `.grad`),则取决于谁自己是叶子且 `requires_grad=True`。

**练习 2**:`compute_z` 里把 `torch.mul(r1, 2)` 改成 `r1 * 2`,计算图会变吗?
**答案**:不会。`r1 * 2` 只是 `torch.mul(r1, 2)` 的运算符写法(语法糖),两者记录到图里的运算是同一个,反向传播行为一致。

**练习 3**:为什么说 PyTorch 是「动态图」?用一句话说给没学过框架的人听。
**答案**:PyTorch 的计算图不是提前声明好的固定结构,而是每次运行前向代码时**临时**搭出来的——Python 这次执行了哪些运算,图里就只有哪些节点,所以 `if`/`for` 可以直接改变图的结构。

### 4.2 `requires_grad` 与自动微分:`backward()` 如何填 `.grad`

#### 4.2.1 概念说明

上一讲(u12-l3)已经跑通过裸 autograd 训练循环,这里把同一机制放在显微镜下,聚焦三个属性:

| 属性/方法 | 挂在谁身上 | 含义 |
| --- | --- | --- |
| `requires_grad` | 任何张量 | 「我需要梯度吗」。创建时指定,或事后用 `requires_grad_()` 原地打开 |
| `.grad_fn` | 由运算得到的中间张量 | 「我由哪个运算生成」。是沿着图回走的路标,叶子张量没有它(为 `None`) |
| `.grad` | `requires_grad=True` 的叶子 | `backward()` 回传后累积的梯度,初值为 `None`,**不清零会累加** |

规则一句话:**前向时,梯度需求沿运算向下传播、`grad_fn` 逐节点记账;反向时,`backward()` 从损失出发沿 `grad_fn` 链条回走,把 \(\partial L/\partial\theta\) 累加进每个需要梯度的叶子的 `.grad`**。

#### 4.2.2 核心流程

书中的最小示例就是一个参数各一的线性模型加平方损失:

\[ z = wx + b, \qquad L = (y - z)^2 \]

对两个参数手算梯度(链式法则):

\[ \frac{\partial L}{\partial w} = -2(y-z)\,x = 2x\,(wx+b-y), \qquad \frac{\partial L}{\partial b} = -2(y-z) = 2\,(wx+b-y) \]

流程:创建 `w、b`(带 `requires_grad=True`)与数据 `x、y`(不需要)→ 前向得 `z`、`loss` → `loss.backward()` → 读 `w.grad、b.grad`,并用上面的手算式核对。

#### 4.2.3 源码精读

[对应位置 1] [ch13/ch13_part1.py:L101-L109](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L101-L109) 演示创建时声明梯度需求:`torch.tensor(3.14, requires_grad=True)` 与向量版 `b`;打印张量时输出里会带 `requires_grad=True` 字样,`a.requires_grad` 直接返回布尔值。

[对应位置 2] [ch13/ch13_part1.py:L114-L123](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L114-L123) 演示**事后开关**:普通创建的 `w` 其 `requires_grad` 为 `False`;调用 `w.requires_grad_()`(注意末尾下划线,PyTorch 惯例表示原地修改)后变为 `True`。这在对「先加载数据、再决定哪些要训练」的场景很有用。

[对应位置 3] [ch13/ch13_part1.py:L155-L168](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L155-L168) 是与第 12 章末尾同源的最小 autograd 示例:`w=1.0`、`b=0.5` 为叶子参数,`x=[1.4]`、`y=[2.1]` 为数据;`z = torch.add(torch.mul(w, x), b)` 即 \(wx+b\),`loss = (y-z).pow(2).sum()` 即 \(L=(y-z)^2\);`loss.backward()` 之后打印 `w.grad` 与 `b.grad`。

[对应位置 4] [ch13/ch13_part1.py:L173-L174](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L173-L174) 用 `print(2 * x * ((w * x + b) - y))` 手算验证 \(\partial L/\partial w\)——autograd 的结果应与之完全一致。

#### 4.2.4 代码实践

1. **实践目标**:核对 autograd 梯度与手算公式,并体会「不要求梯度的叶子拿不到 `.grad`」。
2. **操作步骤**(示例代码):

   ```python
   import torch
   w = torch.tensor(1.0, requires_grad=True)
   b = torch.tensor(0.5, requires_grad=True)
   x = torch.tensor([1.4])
   y = torch.tensor([2.1])

   z = torch.add(torch.mul(w, x), b)
   loss = (y - z).pow(2).sum()
   loss.backward()

   print('dL/dw:', w.grad)          # 与 L167 对应
   print('dL/db:', b.grad)          # 与 L168 对应
   print('手算 dL/dw:', 2 * x * ((w * x + b) - y))   # 与 L174 对应
   print('x.grad:', x.grad)         # x 不要求梯度
   ```

3. **需要观察的现象**:`w.grad` 与手算式输出逐位相同;`x.grad` 为 `None`。
4. **预期结果**:代入 \(w=1.0, b=0.5, x=1.4, y=2.1\):\(z = 1.9\),\(y - z = 0.2\),故 \(\partial L/\partial w = 2 \times 1.4 \times (1.9 - 2.1) = -0.56\),\(\partial L/\partial b = 2 \times (1.9-2.1) = -0.4\)。即 `w.grad` 应为 `tensor([-0.56])`、`b.grad` 为 `tensor([-0.4000])`。这些数值由公式直接算出,可与本地运行输出核对(具体小数位待本地验证)。
5. 附加观察:再执行一次 `loss.backward()` 会直接报错——默认情况下 `backward()` 会**释放**已回传过的图(除非 `retain_graph=True`),这是「每次前向建一张新图」的另一面。

#### 4.2.5 小练习与答案

**练习 1**:`torch.tensor([1.0, 2.0], requires_grad=True)` 与 `torch.tensor([1, 2], requires_grad=True)` 哪个能创建成功?
**答案**:前者可以;后者会报错——整数张量不可导,`requires_grad=True` 只对浮点(等可导 dtype)张量合法。

**练习 2**:为什么训练循环里每批之后必须 `optimizer.zero_grad()`?
**答案**:`.grad` 是**累加**语义,`backward()` 只往上加、不清空;不清零会把上一批的梯度叠进本批更新(u12-l3 已建立这一认知,本讲的 `.grad` 语义是它的根源)。

**练习 3**:中间张量 `z` 有 `.grad_fn`,那它有 `.grad` 吗?`backward()` 之后它的梯度存在哪?
**答案**:默认情况下中间张量的 `.grad` 不会被保留(`backward()` 只把梯度写进叶子,中间结果用完即释放,除非对它调用 `.retain_grad()`);`.grad_fn` 只负责记账、指路,不负责存梯度。

### 4.3 参数注册:`nn.Parameter` 与「普通张量」的天壤之别

#### 4.3.1 概念说明

`nn.Module` 之所以能当「模型容器」,靠的是一套**注册**(registration)机制:子模块、参数、缓冲区挂上去之后,PyTorch 就「认识」它们,于是才能:

- `model.parameters()` 把它们列出来交给优化器;
- `model.to(device)` 把它们**整体搬到 GPU**;
- `model.state_dict()` 把它们存盘、`load_state_dict()` 恢复(下一讲 u13-l2 主题)。

注册的第一条规则:**只有 `nn.Parameter` 类型的张量属性会被自动注册**。`nn.Parameter` 本身就是 `Tensor` 的子类(仓库代码 L452 的行内注释原话是 "nn.Parameter is a Tensor that's a module parameter"),区别只在「身份」:挂上它,`nn.Module.__setattr__` 才会把这张量登记进参数表。

反例是把普通张量直接写成属性——它要求梯度、能参与运算,但 `parameters()` 看不见它、`to(device)` 搬不动它、`state_dict()` 也不存它,错误**静默发生**,直到优化器报「参数列表为空」或 GPU 上报设备不匹配才暴露。

#### 4.3.2 核心流程

```
class MyLayer(nn.Module):
    __init__:
        self.w = nn.Parameter(张量)     → 注册成功:parameters() 可见
        self.w = 普通张量               → 静默失踪:parameters() 不可见
        self.linear = nn.Linear(...)    → 子模块整体注册,其参数间接可见
    forward:
        定义用这些参数做什么计算
```

#### 4.3.3 源码精读

[对应位置 1] [ch13/ch13_part1.py:L139-L145](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L139-L145) 是书里埋的**反例** `MyModule`:`__init__` 里用 `torch.empty(2, 3, requires_grad=True)` 创建 `self.w1、self.w2` 并做 Xavier 初始化。它们是合法张量、也要求梯度,但因为是**普通张量**而非 `nn.Parameter`,这个模块的 `parameters()` 是空的。注意这个类甚至没写 `forward`——它只用于展示「这样写参数会丢」,并不是可训练模型。

[对应位置 2] [ch13/ch13_part1.py:L130-L133](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L130-L133) 顺带演示了参数初始化的常用姿势:`torch.empty(2, 3)` 先开出未初始化的内存,`nn.init.xavier_normal_(w)` 原地填入 Xavier 正态分布的值。「创建」与「初始化」是两步,这一模式在 `NoisyLinear` 里还会出现。

[对应位置 3] [ch13/ch13_part1.py:L448-L456](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L448-L456) 是**正例**:`NoisyLinear.__init__` 中 `self.w = nn.Parameter(w)`、`self.b = nn.Parameter(b)` 完成注册(L452 的行内注释点明 nn.Parameter 的身份),再分别用 `nn.init.xavier_uniform_` 与 `fill_(0)` 初始化。对比反例,差别只有「包没包 `nn.Parameter`」这一步。`noise_stddev` 是普通浮点数(不是张量),作为超参数直接存成属性即可,不需要注册。

> 版本说明:部分印刷版/在线版把「创建 + 初始化」封装成独立的 `reset_parameters` 方法;**仓库当前代码以 `__init__` 内联完成为准**,没有该方法。引用时请以上方链接的仓库代码为准。

[对应位置 4] [ch13/ch13_part4_ignite.py:L152-L154](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py#L152-L154) 展示注册机制在设备转移上的回报:`device = "cuda" if torch.cuda.is_available() else "cpu"` 之后 `model = get_model().to(device)`,一行就把**所有已注册参数**搬到目标设备(u12-l1 讲过张量级 `to()` 的复制语义;模块级 `to()` 是递归地对每个注册参数/子模块执行)。若参数没注册,`to(device)` 会「漏搬」它们,前向时便出现 CPU 张量与 GPU 张量相乘的设备不匹配错误——这是自定义层忘记 `nn.Parameter` 时最典型的翻车现场。

#### 4.3.4 代码实践

1. **实践目标**:用 `parameters()` 直接量化「注册与不注册」的差别。
2. **操作步骤**(示例代码,`MyModule` 反例照抄 L139-L145,`NoisyLinear` 照抄 L448-L464):

   ```python
   import torch, torch.nn as nn

   class MyModule(nn.Module):                 # 反例:普通张量属性
       def __init__(self):
           super().__init__()
           self.w1 = torch.empty(2, 3, requires_grad=True)
           nn.init.xavier_normal_(self.w1)
           self.w2 = torch.empty(1, 2, requires_grad=True)
           nn.init.xavier_normal_(self.w2)

   class NoisyLinear(nn.Module):              # 正例:nn.Parameter 注册
       def __init__(self, input_size, output_size, noise_stddev=0.1):
           super().__init__()
           w = torch.Tensor(input_size, output_size)
           self.w = nn.Parameter(w)
           nn.init.xavier_uniform_(self.w)
           b = torch.Tensor(output_size).fill_(0)
           self.b = nn.Parameter(b)
           self.noise_stddev = noise_stddev
       def forward(self, x, training=False):
           return x                           # 本实践只查参数,不关心计算

   print(list(MyModule().parameters()))       # 预期: []
   print(len(list(NoisyLinear(4, 2).parameters())))  # 预期: 2
   ```

   再试一步:`torch.optim.SGD(MyModule().parameters(), lr=0.1)`。

3. **需要观察的现象**:反例的参数列表是空的;正例恰好列出 `w、b` 两个参数;把空列表交给 `SGD` 会抛出「optimizer got an empty parameter list」一类的 ValueError。
4. **预期结果**:如上。两个结论都由注册规则直接决定,可本地复跑核对。

#### 4.3.5 小练习与答案

**练习 1**:把 `noise_stddev` 也写成 `nn.Parameter(torch.tensor(0.1))` 有什么后果?
**答案**:它会被 `parameters()` 列出、被优化器当作**可训练参数**更新。噪声强度是设计者定的超参数,不该由梯度学习——普通 Python 属性(仓库的做法)才是正确选择。

**练习 2**:`model.to('cuda')` 之后,反例 `MyModule` 的 `self.w1` 在哪个设备?
**答案**:还在 CPU。`nn.Module.to()` 只搬运注册在案的参数/子模块,普通张量属性不归它管——前向时会发生设备不匹配错误。

**练习 3**:除了 `nn.Parameter`,还有什么途径让参数出现在 `model.parameters()` 里?
**答案**:把另一个 `nn.Module`(如 `nn.Linear`)写成属性——子模块整体注册,其内部参数间接可见。4.4 的 `MyModule` 用 `nn.ModuleList` 批量做这件事。

### 4.4 `MyModule` 与 `nn.ModuleList`:自定义模型的组装术

#### 4.4.1 概念说明

`nn.Sequential` 像「串珠管道」:输入从头流到尾,不许分岔、不许条件。一旦需要更灵活的结构(跳连、多输入、按数据走分支),就要继承 `nn.Module` 自己写 `forward`。

自定义模型的固定套路只有两件事:

1. `__init__`:把要用的子层写成**属性**(完成注册,4.3);
2. `forward`:用普通 Python 代码描述数据怎么流过这些层。

当子层很多、想按列表管理时,直接用 Python `list` 存会**丢注册**——列表是普通容器,`nn.Module` 不认识里面的模块。`nn.ModuleList` 就是「会注册的 list」:像 list 一样迭代、索引,同时让每个成员的参数都被模型认领。它**只负责收纳**,不定义顺序——顺序由你在 `forward` 里怎么迭代决定。

#### 4.4.2 核心流程

```
__init__:  造 6 个子层 → 收进 nn.ModuleList(参数全部注册)
forward:   for f in self.module_list: x = f(x)   # 顺序 = 列表顺序
predict:   输入转张量 → forward → 阈值 0.5 → 0/1 标签
训练/画图:  model(x) 走 forward;mlxtend 的 plot_decision_regions 调 model.predict
```

#### 4.4.3 源码精读

[对应位置 1] [ch13/ch13_part1.py:L375-L390](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L375-L390) 定义了正例 `MyModule`:`__init__` 创建三个 `nn.Linear(2→4、4→4、4→1)` 与 ReLU×2、Sigmoid×1 共 6 个子层,收进 `self.module_list = nn.ModuleList(l)`;`forward` 就一个循环 `for f in self.module_list: x = f(x)`。它与 u12-l3 里那个 `nn.Sequential` 多层 XOR 网络([ch13/ch13_part1.py:L331-L338](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L331-L338))结构完全相同,只是换了更灵活的组织方式——两条路在 XOR 数据上收敛行为一致。

[对应位置 2] [ch13/ch13_part1.py:L392-L398](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L392-L398) 定义 `predict`:先把输入转成 `torch.float32` 张量,`forward` 后取 `[:, 0]`,再 `(pred >= 0.5).float()` 输出 0/1。这个方法不是 PyTorch 要求的,而是为 mlxtend 的 `plot_decision_regions` 准备的——它只会调用 `clf.predict(X)`(u2-l1 首次用过这个函数画感知机决策区域)。随后 [L403-L407](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L403-L407) 用与 Sequential 版相同的 `BCELoss` + SGD(lr=0.015) 调用通用 `train` 函数训练。

[对应位置 3] [ch13/ch13_part1.py:L419-L440](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L419-L440) 训练后画损失/准确率曲线,并把 `clf=model` 传给 `plot_decision_regions`——绘出的决策区域就是 `predict`(即确定性 forward)的输出。

#### 4.4.4 代码实践

1. **实践目标**:证明「Python list 会丢注册、`nn.ModuleList` 不会」。
2. **操作步骤**(示例代码):把 `MyModule.__init__` 中的 `self.module_list = nn.ModuleList(l)` 改成 `self.module_list = l`(普通列表),其余不动:

   ```python
   model = MyModule()
   print(len(list(model.parameters())))          # 数一数注册到的参数
   optimizer = torch.optim.SGD(model.parameters(), lr=0.015)
   ```

3. **需要观察的现象**:普通列表版 `parameters()` 数量为 0,`SGD` 抛「empty parameter list」;改回 `nn.ModuleList` 后数量恢复。
4. **预期结果**:`nn.ModuleList` 版应有 6 个参数张量(3 个 Linear 各带 weight 与 bias,激活层无参数)。数量由结构直接决定,可本地核对;训练曲线与 Sequential 版的一致性待本地验证。

#### 4.4.5 小练习与答案

**练习 1**:`nn.ModuleList` 与 `nn.Sequential` 的本质区别是什么?
**答案**:`ModuleList` 只收纳、不定义计算顺序(顺序由你的 `forward` 决定,可以任意跳用、复用、条件调用);`Sequential` 收纳的同时固定了「依次穿过」的计算顺序,省去写 forward 但丧失灵活性。

**练习 2**:`MyModule` 里为什么还写一个 `predict`?`forward` 不够用吗?
**答案**:`forward` 是 PyTorch 的接口;`predict` 是给 mlxtend `plot_decision_regions` 用的适配器(它只认 `predict`),顺便完成了「转张量 + 阈值化」两步,让绘图库拿到 0/1 标签。

**练习 3**:如果 `forward` 里不迭代 `self.module_list`、而是只返回 `x`,模型还能训练吗?
**答案**:能「跑」但毫无意义——损失对参数的梯度链断了(参数没参与前向),`backward()` 之后所有 `.grad` 仍为 `None`,参数从不更新。注册只解决「可见性」,参与计算还得靠 `forward`。

### 4.5 `NoisyLinear`:自定义层与 `forward` 中的条件分支

#### 4.5.1 概念说明

自定义层(custom layer)与自定义模型是同一套套路,只是粒度更细:`__init__` 注册参数(4.3)、`forward` 定义计算(4.4)。`NoisyLinear` 在这两步之外多讲了一件事——**层的训练/评估双重行为**:

- **训练时**:给输入注入高斯噪声,等价于一种原地数据增强,迫使模型不依赖输入的精确取值,起到正则化作用;
- **评估/推理时**:关闭噪声,输出确定、可复现。

公式化地,该层计算

\[ \boldsymbol{Y} = (\boldsymbol{X} + \boldsymbol{\epsilon})\,\boldsymbol{W} + \boldsymbol{b}, \qquad \epsilon_{ij} \sim \mathcal{N}(0, \sigma^2) \]

训练时 \(\sigma\) 取 `noise_stddev`(默认 0.1,`MyNoisyModule` 中取 0.07),评估时 \(\sigma = 0\)。

两个值得咀嚼的细节:

1. **权重形状换了一种约定**:`w` 的形状是 `(input_size, output_size)`,前向用 `torch.mm(x_new, self.w)`(x 乘 w,无转置)。与 `nn.Linear`/`NeuralNetMLP` 的 `(out, in)` + `x @ W.T` 相比只是权重的存放排布不同,数学完全等价——读别人的自定义层时,先核对它的形状约定再核对公式。
2. **动态图落点**:`forward` 里的 `if training:` 分支意味着「这次调用走哪条路」可以每次不同,两条路对应两张不同的图。这正是 4.1 所说的 define-by-run:静态图框架里这种「随开关变化的图」要绕弯子实现,PyTorch 里就是一个普通 `if`。

顺带对照:PyTorch 内置的双行为层(如 `nn.Dropout`、BatchNorm)不用显式传参,而是由 `model.train()`/`model.eval()` 切换内置的 `self.training` 标志。本书用显式 `training` 参数把机制裸露出来,两种写法思想相同;第 14 章 CNN 里你会看到内置标志的用法。

#### 4.5.2 核心流程

```
__init__(input_size, output_size, noise_stddev):
    w ← empty(input_size, output_size) → 包成 nn.Parameter → xavier_uniform_ 初始化
    b ← empty(output_size) → fill_(0) → 包成 nn.Parameter
    记下 noise_stddev(普通属性,超参数)

forward(x, training=False):
    if training:  x_new = x + N(0, noise_stddev²)   # 每次调用重新采样
    else:         x_new = x                          # 确定性路径
    return x_new @ w + b

验证逻辑(手动调用模块):
    module(x, training=True)  两次 → 输出不同(两次独立采样)
    module(x, training=False) → 输出固定
```

注意 `noisy_layer(x, training=True)` 这种「像函数一样调模块」的写法:`nn.Module` 实现了 `__call__`,它会把额外参数原样转交给 `forward`(并顺带执行钩子等框架逻辑)——所以 `model(x_batch, True)` 与 `forward(x_batch, True)` 等价,`MyNoisyModule` 的训练循环正是靠它把 `True` 一路传进 `NoisyLinear`。

#### 4.5.3 源码精读

[对应位置 1] [ch13/ch13_part1.py:L448-L464](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L448-L464) 是完整的 `NoisyLinear`:`__init__`(L449-L456)创建并用 `nn.Parameter` 注册 `w、b`,分别以 Xavier 均匀分布与 0 初始化;`forward`(L458-L464)中 `if training:` 分支用 `torch.normal(0.0, self.noise_stddev, x.shape)` 采样与输入同形的噪声、`torch.add(x, noise)` 注入,否则直通;最后 `torch.add(torch.mm(x_new, self.w), self.b)` 完成线性变换。噪声是在**图内**生成的:反向传播时它被当作常数,梯度照常流过加法节点流向 `x` 与 `w`。

[对应位置 2] [ch13/ch13_part1.py:L469-L480](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L469-L480) 是作者留好的验证实验:固定 `torch.manual_seed(1)` 保证可复现,`noisy_layer = NoisyLinear(4, 2)`,`x = torch.zeros((1, 4))`,然后分别以 `training=True` 调两次、`training=False` 调一次。因为零输入过线性层本应得到常量 `b`,任何输出波动都来自噪声——这使「两次 True 结果不同、False 结果固定且等于 b 的影响」一目了然。

[对应位置 3] [ch13/ch13_part1.py:L486-L508](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L486-L508) 定义 `MyNoisyModule`:结构与 4.4 的 `MyModule` 相同(2→4→4→1 + ReLU×2 + Sigmoid),只是第一层换成 `NoisyLinear(2, 4, 0.07)`。`forward(x, training=False)` 把这个开关一路传给 `self.l1(x, training)`;`predict` 与 4.4 相同(阈值 0.5 出 0/1 标签,供 mlxtend 绘图),且**不传 training**,绘图走确定性路径。

[对应位置 4] [ch13/ch13_part1.py:L517-L544](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L517-L544) 是它的训练循环:注意它没有复用 L282-L306 的通用 `train` 函数,而是把循环**展开重写**——因为通用函数只调用 `model(x_batch)`,而这里训练批要调 `model(x_batch, True)`(L528,带噪声),验证集调 `model(x_valid)`(L540,默认不带噪声)。训练/评估两种模式在同一循环里前后切换,正是 4.5.1 所说双重行为的落地。其余五步(算损失、`backward`、`step`、`zero_grad`、按 epoch 统计)与 u12-l3 的骨架完全一致。

#### 4.5.4 代码实践(本讲核心实践)

1. **实践目标**:亲手写一个 `NoisyReLU` 自定义层——训练时先给输入加噪声再做 ReLU,评估时只做普通 ReLU——验证 train/eval 两种模式下输出不同,并组装进模型跑前向。
2. **操作步骤**(示例代码,依 `NoisyLinear` 仿写):

   ```python
   import torch
   import torch.nn as nn

   class NoisyReLU(nn.Module):
       def __init__(self, noise_stddev=0.1):
           super().__init__()
           self.noise_stddev = noise_stddev      # 无参数层:没有可训练权重

       def forward(self, x, training=False):
           if training:                          # 条件分支:动态图的用武之地
               noise = torch.normal(0.0, self.noise_stddev, x.shape)
               x = torch.add(x, noise)
           return torch.relu(x)                  # relu(max(0,·)) 不可导处 autograd 有定义

   torch.manual_seed(1)
   layer = NoisyReLU(noise_stddev=1.0)           # 噪声调大便于观察
   x = torch.tensor([[-1.0, -0.5, 0.0, 0.5, 1.0]])

   print(layer(x, training=True))                # 第 1 次:噪声 + ReLU
   print(layer(x, training=True))                # 第 2 次:应与第 1 次不同
   print(layer(x, training=False))               # 确定性:恰为 [0, 0, 0, 0.5, 1.0]

   # 组装进模型,跑一次前向
   class MyNoisyModule(nn.Module):
       def __init__(self):
           super().__init__()
           self.l1 = nn.Linear(2, 4)
           self.a1 = NoisyReLU(0.5)
           self.l2 = nn.Linear(4, 1)
           self.a2 = nn.Sigmoid()
       def forward(self, x, training=False):
           x = self.l1(x)
           x = self.a1(x, training)              # 开关逐层传递
           x = self.l2(x)
           return self.a2(x)

   torch.manual_seed(1)
   model = MyNoisyModule()
   xb = torch.randn(3, 2)
   print(model(xb, training=True))               # 两次前向结果不同
   print(model(xb, training=True))
   print(model(xb, training=False))              # 两次确定性前向结果相同
   ```

3. **需要观察的现象**:层级别——两次 `training=True` 输出不同(负半轴被噪声「救活」成非零值是关键证据),`training=False` 输出恰为 `[0, 0, 0, 0.5, 1.0]`;模型级别——噪声版两次前向不同,确定性版两次前向逐位相同。
4. **预期结果**:如上;`NoisyReLU` 无参数,`len(list(layer.parameters()))` 应为 0(可与 4.3 的结论互相印证)。具体数值取决于随机种子与版本,待本地验证。
5. **进阶**(可选):把 `MyNoisyModule` 换到仓库的 XOR 数据上,套用 [L522-L544](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L522-L544) 的训练循环(训练传 `True`、验证不传),观察噪声对训练曲线的影响,即本讲综合实践。

#### 4.5.5 小练习与答案

**练习 1**:`NoisyLinear` 的噪声加在**输入**上而不是权重上,这两种做法正则化效果有何不同直觉?
**答案**:加在输入上≈对输入做数据增强,让输出对输入的邻域稳定,类似样本抖动;加在权重上(权重噪声)则约束**模型函数本身**的平滑性。两者都抑制过拟合,作用位置不同,本仓库采用的是输入噪声方案。

**练习 2**:为什么 `MyNoisyModule` 的验证评估(L540)调 `model(x_valid)` 而不是 `model(x_valid, True)`?
**答案**:验证要估计的是「部署时(无噪声)的性能」,若验证也加噪,指标既不稳定也不代表推理行为;训练加噪是为了正则化,评估关噪才是对真实泛化的度量。

**练习 3**:`predict`(L505-L508)没有 `training` 参数,若 mlxtend 绘图时走了带噪路径会发生什么?
**答案**:每次绘图采样不同的噪声,决策区域每次都变,还可能因带噪预测越过 0.5 阈值而出现毛刺边界。仓库让 `predict` 走默认 `training=False`,保证图确定、可复现。

## 5. 综合实践

**任务:噪声强度扫描实验——把本讲的参数注册、动态图、训练/评估双重行为串成一条线。**

改造 [ch13/ch13_part1.py:L486-L544](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L486-L544),步骤如下:

1. **体检参数注册**:实例化 `MyNoisyModule`,打印 `len(list(model.parameters()))`,并核对 `NoisyLinear` 的 `w、b` 与两个 `nn.Linear` 的参数是否全部在列(预期共 6 个张量:NoisyLinear 2 个 + `nn.Linear(4,4)` 2 个 + `nn.Linear(4,1)` 2 个,激活层无参数;数量可本地核对)。
2. **复用训练循环**:把 [L522-L544](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L522-L544) 的循环包成函数 `train_noisy(model, noise_stddev, ...)`,其中 `NoisyLinear(2, 4, noise_stddev)` 的噪声强度由参数传入。为省时间可把 `num_epochs` 从 200 降到 50(先跑通再说)。
3. **三档扫描**:对 `noise_stddev = 0.0、0.07、0.5` 各训练一次,分别记录:
   - 训练/验证准确率曲线(照抄 L551-L561 的画法);
   - 决策区域图(照抄 L564-L567,`plot_decision_regions(clf=model)`,注意绘图走的是无噪声 `predict`)。
4. **观察与解释**:预期 `0.0` 档与 u12-l3 的普通多层网络行为接近(可能轻微过拟合、边界贴样本);`0.07` 档边界更平滑;`0.5` 档噪声过大、训练损失下降变慢甚至欠拟合。训练曲线波动幅度应随噪声强度增大。
5. **验证双行为**:任选一档模型,对同一批输入分别以 `model(xb, training=True)` 与 `model(xb)` 前向,确认前者两次不同、后者两次相同(呼应 4.5.4)。

具体数值(准确率、曲线形态)随随机种子与 PyTorch 版本浮动,**待本地验证**;但「噪声越大、训练越抖、边界越平滑」的定性趋势由该层定义直接保证。

## 6. 本讲小结

- **动态图(define-by-run)**:PyTorch 的计算图由每次前向**实际执行**的运算即时构建;`forward` 里写 `if`/`for` 完全合法,两次调用可以走两张不同的图。`compute_z` 演示了同一份代码对任意阶输入各建各的图。
- **三个诊断属性**:`requires_grad`(是否需要梯度,可用 `requires_grad_()` 原地打开)、`.grad_fn`(中间节点的运算记账,可沿 `next_functions` 回走)、`.grad`(叶子上的累计梯度)。手算式 \(2x(wx+b-y)\) 与 autograd 输出逐位一致。
- **参数注册是生死线**:普通张量属性会从 `parameters()`、`to(device)`、`state_dict()` 中**静默失踪**;必须包成 `nn.Parameter`(或经由子模块/`nn.ModuleList` 间接注册)。`model.to(device)` 一行搬走全部注册参数。
- **自定义模型/层两步走**:`__init__` 注册子层与参数,`forward` 用普通 Python 描述数据流;`nn.ModuleList` 是「会注册的列表」,顺序由 forward 决定。
- **`NoisyLinear` = 三件套示范**:`nn.Parameter` 注册(含 Xavier 初始化与 `(in, out)` 形状约定)、`if training:` 条件分支(动态图优势)、训练加噪/评估关噪的双重行为——与内置 Dropout 的 `model.train()/eval()` 机制思想相同。
- **`model(x, True)` 的传参通道**:`nn.Module.__call__` 把额外实参转交给 `forward`,`MyNoisyModule` 的训练循环因此展开重写,训练批传 `True`、验证集走默认 `False`。

## 7. 下一步学习建议

本讲解决了「怎么写自己的层和模型」,下一讲 **u13-l2 项目结构、模型保存与加载** 解决「怎么把训练成果管起来」:`state_dict()` 为什么恰好只序列化**注册过**的参数(正是 4.3 注册机制的直接回报)、`torch.save`/`torch.load` 的两种方式与跨设备加载的 `map_location`,以及把训练代码拆成 model/data/training 模块的工程组织。建议先精读 [ch13/ch13_part2.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py)。若你想先见识注册机制在更大模型上的规模效应,也可以提前翻看第 14 章的 ResNet 微调([ch14/ch14_part2.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py)),那里的 `param.requires_grad = False` 冻结操作与本讲的 `requires_grad_()` 开关互为镜像。
