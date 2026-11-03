# 项目结构、模型保存与加载

> 前置讲义：u13-l1 动态计算图与自定义层（你已会写 `NoisyLinear` 这类自定义层、理解参数注册）；u12-l3（你已初步见过 `torch.save` 的两种用法）。

## 1. 本讲目标

学完本讲，你应该能够：

1. 把一个「从头写到尾」的训练脚本，按**数据准备 → 模型构建 → 训练循环 → 评估 → 保存**五段流水线来阅读和拆分。
2. 说清 `torch.save(model, path)`（保存整个模型）与 `torch.save(model.state_dict(), path)`（只存参数）的差别，以及两者对加载端代码结构的**不同要求**。
3. 理解张量的 `device` 属性，会用 `map_location` 解决「GPU 上保存的权重、无 GPU 机器上加载」的问题。
4. 按书中建议把训练工程拆成「模型构建工具 / 数据加载工具 / 训练循环」等 Python 模块，并在 notebook 中调用。

## 2. 前置知识

- **序列化（serialization）**：把内存中的 Python 对象转成可写盘的字节流，之后还能原样恢复。PyTorch 的 `torch.save` 底层用 Python 标准库 **pickle** 做序列化，保存成 `.pt` / `.pth` 文件。理解「pickle 保存的是对象的类引用 + 数据」是本讲 4.2 节的关键。
- **有序字典（OrderedDict）**：记住插入顺序的字典。`model.state_dict()` 返回的就是一个「参数名 → 张量」的有序字典。
- **device（设备）**：PyTorch 中每个张量都带一个 `device` 属性，标明它住在 CPU 内存还是某块 GPU 显存上（如 `cuda:0`）。上一讲 u12-l1 已见过 `to(device)` 的复制语义：跨设备搬运会生成新张量。
- **checkpoint（检查点）**：训练过程中定期写盘的存档，用来断点续训或部署推理。保存的内容可以是「只有权重」，也可以是「权重 + 优化器状态 + 训练轮数」的字典。
- **模块（module）与包**：一个 `.py` 文件就是一个模块，文件名即模块名；`import` 让一个模块使用另一个模块里定义的类和函数。这是 4.4 节拆分工程的基础。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ch13/ch13_part2.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py) | 本讲主角一：两个完整训练项目（汽车燃油效率回归、MNIST 分类），是「五段流水线」的活教材 |
| [ch13/ch13_part1.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py) | 本讲主角二：提供 XOR 数据、`train` 函数、自定义层 `NoisyLinear` 与模型 `MyNoisyModule`，是综合实践的载体 |
| [ch12/ch12_part2.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py) | 两种保存/加载方式的**真实代码**所在地（「Saving and reloading the trained model」小节），本讲 4.2 节精读 |
| [ch14/ch14_part1.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py) | 后续章节对 `device` 与「建目录再保存」的真实用法，为 4.3 节提供证据 |
| [ch13/ch13_part4_ignite.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py) | Ignite 的 `Checkpoint` 处理器：把「保存什么」扩展成字典，为下一讲铺垫 |

> 说明：`map_location` 参数与书中示意的一些工具函数名（如 `load_data`）在仓库代码里**并未出现**，属于书中文字讲解的内容。本讲凡引用这类内容都会明确标注「示例代码」，不冒充仓库原有代码。

## 4. 核心概念与源码讲解

### 4.1 文件/模块化拆分动机

#### 4.1.1 概念说明

前两讲你已经在 notebook 里跑通了训练循环，但随着项目变大，把所有代码堆在一个文件里会遇到三个问题：

1. **不可复用**：下个项目要重写一遍几乎相同的训练循环；
2. **不可测试**：一个几千行的脚本无法单独测试某一段逻辑；
3. **不可交接**：协作者难以定位「数据在哪准备、模型在哪定义」。

解决办法是承认一个事实：**任何训练工程本质上都是同一条五段流水线**——数据获取 → 预处理 → 模型构建 → 训练循环 → 评估与保存。先把脚本按这五段「读」，再按这五段「拆」，工程化就有了抓手。

#### 4.1.2 核心流程

以第 13 章的汽车燃油效率项目为例，五段流水线如下：

```text
① 数据获取     读 CSV → ② 预处理   缺失值/标准化/分箱/独热 → 张量
③ 模型构建     nn.Linear 堆叠成 nn.Sequential
④ 训练循环     前向 → 损失 → backward → step → zero_grad，逐 epoch 重复
⑤ 评估与保存   torch.no_grad() 下算测试指标 → torch.save 存档
```

其中②③④⑤在仓库的两个项目里**几乎逐段同构**——这正是「可以抽取成公共模块」的直接证据（详见 4.4 节）。

#### 4.1.3 源码精读

**第①段：数据获取。** 从 UCI 下载 auto-mpg 数据并读入 DataFrame，`na_values="?"` 把问号识别为缺失值：

- [ch13/ch13_part2.py:72-78](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L72-L78) — 定义 URL 与列名，用 `pd.read_csv` 读入数据。

**第②段：预处理。** 探查并删除缺失行、按训练集统计量标准化、对「车型年份」做分箱、对「产地」做独热编码，最后拼成浮点张量：

- [ch13/ch13_part2.py:85-89](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L85-L89) — `df.isna().sum()` 探查缺失，`dropna` 删除后 `reset_index` 重排下标。
- [ch13/ch13_part2.py:96-98](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L96-L98) — `train_test_split` 划分，`describe().transpose()` 得到**训练集**统计表 `train_stats`（统计量只从训练集算，与 u3-l1 的防泄漏原则一致）。
- [ch13/ch13_part2.py:103-113](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L103-L113) — 用 `train_stats` 里的均值和标准差同时标准化训练集与测试集。
- [ch13/ch13_part2.py:118-126](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L118-L126) — `torch.bucketize` 按边界 `[73, 76, 79]` 把年份离散成桶。
- [ch13/ch13_part2.py:133-141](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L133-L141) — `one_hot` 编码产地，`torch.cat` 与数值列拼成特征张量 `x_train` / `x_test`。

**第③段：模型构建。** 用循环把 `Linear + ReLU` 堆起来，最后接一个输出 1 维的线性层（回归）：

- [ch13/ch13_part2.py:162-174](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L162-L174) — `hidden_units = [8, 4]`，循环里 `input_size` 逐层传递，`nn.Sequential(*all_layers)` 组装成模型。

**第④段：训练循环。** 选损失与优化器后，逐 epoch 逐批执行五步骨架（u12-l3 建立的通用骨架）：

- [ch13/ch13_part2.py:181-182](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L181-L182) — 回归用 `nn.MSELoss()` + `SGD(lr=0.001)`。
- [ch13/ch13_part2.py:187-200](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L187-L200) — 训练 200 轮，每 20 轮打印一次平均损失；注意 `pred = model(x_batch)[:, 0]` 把输出形状从 `(batch, 1)` 压成 `(batch,)` 以对齐目标。

**第⑤段：评估。** 关闭梯度追踪后在测试集上算指标：

- [ch13/ch13_part2.py:206-210](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L206-L210) — `with torch.no_grad():` 内计算测试 MSE 与 MAE。

同一文件的后半部分是**第二个项目**（MNIST 手写数字分类），五段结构完全平行：[ch13/ch13_part2.py:219-233](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L219-L233)（数据）、[ch13/ch13_part2.py:238-250](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L238-L250)（同样的 `all_layers` 循环）、[ch13/ch13_part2.py:260-273](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L260-L273)（几乎相同的训练循环，只是损失换成 `CrossEntropyLoss`、优化器换成 Adam）、[ch13/ch13_part2.py:278-280](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L278-L280)（测试评估）。**两个项目、两种任务，骨架相同**——这就是拆分动机的最有力证据。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：建立「读任何训练脚本先找五段」的肌肉记忆。
2. **操作步骤**：
   - 打开 [ch13/ch13_part2.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py)，用编辑器的折叠或书签，把 auto-mpg 项目（约 L72-L210）切成①②③④⑤五段，记下每段的起止行号；
   - 再对 MNIST 项目（约 L219-L280）做同样切分；
   - 列一张表：两段流水线中，哪些行几乎可以复制粘贴（模型构建循环、训练循环），哪些行必须随任务改写（损失函数、输出维度）。
3. **需要观察的现象**：MNIST 版训练循环 [L260-273](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L260-L273) 与 auto-mpg 版 [L187-200](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L187-L200) 的差异只在损失、指标与 `[:, 0]` 的有无，五步骨架逐字相同。
4. **预期结果**：你会得到两个几乎平行的五段清单；差异集中在②（预处理）与损失/输出维度上。
5. 本实践为纯阅读，无需运行环境。

#### 4.1.5 小练习与答案

**练习 1**：auto-mpg 项目中 `train_stats` 只由 `df_train` 计算而不用全量 `df`，为什么？

**答案**：测试集的统计量一旦参与标准化，测试集信息就泄漏进了训练流程，评估会乐观偏准；这与 u3-l1「StandardScaler 只在训练集 fit」、u6-l1「Pipeline 防泄漏」是同一条原则在纯 PyTorch 手写流程中的体现。

**练习 2**：如果要把 auto-mpg 的模型构建段抽成一个函数 `build_model(input_size, hidden_units, output_size)`，函数体应包含哪几行代码？

**答案**：对应 [ch13/ch13_part2.py:162-174](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L162-L174)：初始化 `all_layers = []`、循环追加 `nn.Linear` 与 `nn.ReLU` 并传递 `input_size`、末尾追加输出层、返回 `nn.Sequential(*all_layers)`。`hidden_units = [8, 4]` 与 `input_size` 都改为由参数传入。

### 4.2 torch.save 两类保存对象

#### 4.2.1 概念说明

训练出的模型本质上是**一堆参数张量**（权重与偏置）。`torch.save` 有两类保存对象：

| | 保存整个模型 `torch.save(model, path)` | 保存状态字典 `torch.save(model.state_dict(), path)` |
| --- | --- | --- |
| 存了什么 | pickle 序列化**整个模型对象**：类的引用 + 所有参数 + 结构 | 只存「参数名 → 张量」的 OrderedDict |
| 加载方式 | `model_new = torch.load(path)`，直接得到可调用模型 | 先 `model_new = Model(...)` 手工搭好同结构，再 `model_new.load_state_dict(torch.load(path))` |
| 对代码结构的要求 | 加载环境必须能 **import 到模型类的定义**（自定义类尤其如此）；类改名、移动文件都会让旧存档失效 | 只要求加载端搭出**同名同形**的参数结构；模型代码可以自由重构 |
| 官方立场 | 书中明确提示：代码重构后易失效，不推荐用于长期保存 | 书中与 PyTorch 官方文档均推荐 |

一句话：**整模型存档「自包含但脆弱」，state_dict 存档「轻量但要求先有骨架」**。

#### 4.2.2 核心流程

```text
路线 A（整模型）：
  model ──torch.save(model, path)──▶ model.pt（含类引用+参数）
  model.pt ──torch.load(path)──▶ model_new（可直接 model_new(x)），随后 model_new.eval()

路线 B（state_dict）：
  model.state_dict()：OrderedDict{'layer1.weight': ..., 'layer1.bias': ..., ...}
  ──torch.save(state_dict, path)──▶ state.pt（纯参数）
  state.pt ──torch.load(path)──▶ 字典 ──load_state_dict──▶ 注入预先搭好的同结构新模型
```

`state_dict()` 的键名由「子模块属性路径 + 参数注册名」拼接而成。这一点在含自定义层的模型上看得很清楚：`NoisyLinear` 把参数注册为 `self.w`、`self.b`（见 4.2.3），而内置 `nn.Linear` 的参数名是 `weight`、`bias`；于是 `MyNoisyModule`（属性名 `l1/l2/l3`）的 state_dict 键将是 `l1.w`、`l1.b`、`l2.weight`、`l2.bias`、`l3.weight`、`l3.bias`（依据 PyTorch 命名规则推得，你将在 4.2.4 的实践中打印验证）。这也解释了为什么 `load_state_dict` 要求**两边结构同名同形**——键对不上就会报错，这层「严格」是保护而不是麻烦。

#### 4.2.3 源码精读

两种保存/加载的真实代码在第 12 章「Saving and reloading the trained model」小节：

- [ch12/ch12_part2.py:341-342](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L341-L342) — **路线 A 保存**：`torch.save(model, path)` 把 iris 分类器整个对象写入 `iris_classifier.pt`。
- [ch12/ch12_part2.py:347-348](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L347-L348) — **路线 A 加载**：`torch.load(path)` 直接得到 `model_new`，随后 `model_new.eval()` 切到评估模式。
- [ch12/ch12_part2.py:353-358](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L353-L358) — 用加载的模型对同一测试张量推理，测试准确率应与保存前一致（同一模型、同一权重）。
- [ch12/ch12_part2.py:363-364](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L363-L364) — **路线 B 保存**：`torch.save(model.state_dict(), path)` 只存参数字典。
- [ch12/ch12_part2.py:369-370](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L369-L370) — **路线 B 加载**：先用**同样的构造参数** `Model(input_size, hidden_size, output_size)` 搭出空骨架，再把参数字典灌进去。

被保存的 `Model` 类长这样——注意构造函数带三个尺寸参数，这正是路线 B 加载端必须「先搭同结构」的依据：

- [ch12/ch12_part2.py:252-263](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L252-L263) — 两层 `nn.Linear`（`layer1`/`layer2`）加 Sigmoid/Softmax 的 `Model` 类。

第 13 章的自定义层则展示了参数命名如何进入 state_dict：

- [ch13/ch13_part1.py:448-464](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L448-L464) — `NoisyLinear` 把权重注册为 `nn.Parameter` 类型的 `self.w` 与 `self.b`（L452、L455）。参数注册名直接决定 state_dict 的键名。
- [ch13/ch13_part1.py:486-508](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L486-L508) — `MyNoisyModule` 以属性 `l1`（NoisyLinear）、`l2`、`l3`（Linear）组装模型；若对它做路线 A 保存，加载环境必须能 import 到 `NoisyLinear` 与 `MyNoisyModule` 两个类的定义——**自定义类让整模型存档的脆弱性暴露得最明显**。

两个补充证据：

- [ch14/ch14_part1.py:488-492](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L488-L492) — 后续 CNN 章节复用整模型保存，且先 `os.mkdir('models')` 建目录再写盘，是工程化的好习惯。
- [ch13/ch13_part4_ignite.py:245-247](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py#L245-L247) — 注释明确说之后用 `torch.load` 加载最佳模型，指回第 12 章该小节：保存/加载是贯穿后续所有章节的基础设施。

另外提醒：pickle 反序列化会执行文件中的对象构造逻辑，加载来源不明的 `.pt` 文件有安全风险；较新版本的 PyTorch 为 `torch.load` 增加了 `weights_only` 参数来限制可反序列化的对象类型，加载不可信存档时应优先使用（本书基于 torch 1.8，代码中未涉及）。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到 `state_dict()` 的键名结构，并体会两条加载路线对代码的不同要求。
2. **操作步骤**（在 `ch13/` 目录下以 notebook 或脚本执行；示例代码，基于 [ch13/ch13_part1.py:486-511](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L486-L511)）：

   ```python
   # 示例代码：探查自定义模型的 state_dict 键名
   import torch
   from ch13_part1 import NoisyLinear, MyNoisyModule   # 需要 ch13_part1.py 可被 import

   torch.manual_seed(1)
   model = MyNoisyModule()
   for name, param in model.state_dict().items():
       print(f'{name:<12} shape={tuple(param.shape)}')
   ```

3. **需要观察的现象**：打印出的键应形如 `l1.w`、`l1.b`、`l2.weight`、`l2.bias`、`l3.weight`、`l3.bias`——`l1` 是 NoisyLinear（参数名 `w`/`b`），`l2`、`l3` 是内置 Linear（参数名 `weight`/`bias`）。
4. **预期结果**：6 个键，形状分别为 `(2,4)`、`(4,)`、`(4,4)`、`(4,)`、`(4,1)`、`(1,)`。待本地验证。
5. 若 import 报错，请先确认工作目录与 `sys.path`（ch13 脚本开头都有 `sys.path.insert(0, '..')` 的先例）。

#### 4.2.5 小练习与答案

**练习 1**：同事把 `MyNoisyModule` 改名为 `MyModel` 并删除了旧类，半年前的 `torch.save(model, 'old.pt')` 存档还能加载吗？`state_dict` 存档呢？

**答案**：整模型存档大概率失效——pickle 里记录的是「模块路径 + 类名 `MyNoisyModule`」，环境中找不到该类时 `torch.load` 抛错。`state_dict` 存档仍可用：只要用新类搭出同名同形的结构（把属性改回 `l1/l2/l3`、`NoisyLinear` 内参数仍叫 `w/b`），`load_state_dict` 照常灌入。这正是官方推荐 state_dict 的原因。

**练习 2**：为什么 [ch12/ch12_part2.py:348](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch12/ch12_part2.py#L348) 加载后要调用 `model_new.eval()`？对第 13 章的 `NoisyLinear` 又如何？

**答案**：`eval()` 把模型（及其内置层）的 `self.training` 标志置为 False，使 Dropout、BatchNorm 这类「训练/评估行为不同」的内置层切换到推断行为，保证推理确定性。而 `NoisyLinear`（[ch13/ch13_part1.py:458-464](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L458-L464)）没有读取 `self.training`，它靠 forward 的显式 `training` 参数控制加噪与否——所以对它而言 `eval()` 不起作用，调用方必须显式传 `training=False`（u13-l1 已分析过这一设计差异）。

### 4.3 map_location 跨设备加载

#### 4.3.1 概念说明

每个张量都记得自己住在哪个 `device` 上。模型在 GPU 上训练，`torch.save` 连同参数的设备信息一起写入存档；到一台没有 GPU 的机器上 `torch.load`，PyTorch 默认会试图把张量恢复到**原设备**，于是报错（找不到 `cuda:0`）。`map_location` 参数就是搬运指令：**把存档里的张量统一映射到指定设备**再返回。书中据此给出「CPU 上训练、GPU 上推理（或反过来）」的标准做法。

必须说明：**仓库代码中没有出现 `map_location`**（已用全文检索确认），它是书中讲解的 API；仓库中真实的设备相关代码见下方引用。

#### 4.3.2 核心流程

```text
GPU 保存 → CPU 加载：
  torch.save(model.state_dict(), 'gpu_trained.pt')     # 权重带 cuda:0 设备信息
  state = torch.load('gpu_trained.pt', map_location='cpu')  # 强制落到 CPU
  cpu_model.load_state_dict(state)

CPU 保存 → GPU 加载（推理）：
  state = torch.load(path, map_location='cuda:0')
  model.load_state_dict(state); model = model.to('cuda:0')
```

要点两条：① `map_location` 只负责**读入时**张量落在哪里，加载后模型要推理还需自己 `to(device)`（张量与模型、输入三者的设备必须一致，否则报设备不匹配错误）；② 存档里若混有 `cuda` 与 `cpu` 张量，可用一个字典逐设备指定，如 `map_location={'cuda:1': 'cuda:0', 'cuda:2': 'cpu'}`。

#### 4.3.3 源码精读

仓库中「设备」概念的真实写法（后续章节广泛使用）：

- [ch14/ch14_part1.py:376-379](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac337afc3c0a91/ch14/ch14_part1.py#L376-L379) — `device = torch.device("cuda:0")`（下一行注释给出 CPU 备选），然后 `model = model.to(device)` 把参数搬到 GPU。
- [ch14/ch14_part1.py:394-396](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L394-L396) — 训练循环里每个 batch 也要 `x_batch.to(device)`、`y_batch.to(device)`：模型在 GPU 而数据在 CPU 时前向会报错，这是理解「存档设备信息为何需要 map_location」的最好铺垫。
- [ch13/ch13_part4_ignite.py:152](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py#L152) — 第 13 章自己的设备选择惯用法：`device = "cuda" if torch.cuda.is_available() else "cpu"`，让同一份代码自适应有无 GPU。

`map_location` 的典型用法（**示例代码**，仓库未包含）：

```python
# 示例代码：GPU 训练的存档在无 GPU 机器上加载
state = torch.load('gpu_trained.pt', map_location=torch.device('cpu'))
model.load_state_dict(state)
```

#### 4.3.4 代码实践

1. **实践目标**：验证「无 GPU 环境无法默认加载 GPU 存档、`map_location` 可以解决」。
2. **操作步骤**（**示例代码**；需要一台有 CUDA 的机器做前半段，没有则做思想实验）：

   ```python
   # 示例代码
   import torch
   m = torch.nn.Linear(4, 2)
   if torch.cuda.is_available():
       m_gpu = m.to('cuda:0')
       torch.save(m_gpu.state_dict(), 'gpu_w.pt')      # 存档记录了 cuda:0
       # 换到 CPU 机器后：
       s = torch.load('gpu_w.pt', map_location='cpu')  # 不加 map_location 会尝试恢复到 cuda:0 而报错
       m.load_state_dict(s)
       print('loaded on cpu ok')
   else:
       print('本机无 CUDA，跳过 GPU 段；可用 torch.device("cpu") 保存/加载做同构验证')
   ```

3. **需要观察的现象**：去掉 `map_location='cpu'` 直接 `torch.load` 时应出现指向 `cuda` 设备不可用的错误；加上后正常加载，且 `m` 的参数 `device` 为 `cpu`。
4. **预期结果**：如上；本实践依赖真实 GPU 环境，**待本地验证**。无 GPU 时可只完成 CPU↔CPU 对照：保存时带设备信息、加载时不加 `map_location` 也能成功（同设备无需映射），据此理解设备不匹配才需要映射。

#### 4.3.5 小练习与答案

**练习 1**：`map_location='cpu'` 加载后，直接把 CPU 输入喂给仍在 `cuda:0` 的模型会怎样？

**答案**：不会发生——`map_location='cpu'` 读入的张量在 CPU 上，`load_state_dict` 把它们**复制进**目标模型的参数；但若目标模型本身已 `to('cuda:0')`，其参数在 GPU，随后喂 CPU 输入会报「设备不匹配」运行时错误。正确做法是三者同设备：`model.to(device)` 且输入 `x.to(device)`。`load_state_dict` 会把 CPU 张量拷到模型参数所在设备，所以更省心的顺序是先摆好模型设备、再加载。

**练习 2**：为什么 [ch13/ch13_part4_ignite.py:152](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py#L152) 用 `torch.cuda.is_available()` 做条件而不是写死 `"cuda"`？

**答案**：让同一份训练脚本在有 GPU 与纯 CPU 的机器上都能跑；写死 `"cuda"` 会在无 GPU 机器上直接报错。这种「自适应设备」写法与 `map_location` 配合，构成跨设备部署的完整习惯。

### 4.4 utils 工具函数封装

#### 4.4.1 概念说明

书中在本章开头建议：当项目从 notebook 原型走向工程时，把代码按职责拆成几类工具模块——**模型构建工具（model construction utilities）**、**数据加载工具（data loading utilities）**、**训练循环**，示意图中还出现 `load_data`、`input_to_tensor` 这类小函数名。要点不是这些具体名字，而是划分方式：

- 模型构建：只管「给定超参数返回模型」，不碰数据；
- 数据加载：只管「给定路径/配置返回 DataLoader」，不碰模型；
- 训练循环：只管「给定模型、DataLoader、损失、优化器跑完训练并返回历史」，对任务保持中立；
- notebook：只做「组装与叙事」，import 上面的模块。

需要如实说明：**配套仓库为教学便利没有做这个拆分**（全文检索无 `load_data`、`input_to_tensor` 等函数），各章保持单脚本形态；本节的拆分演示均为示例代码。

#### 4.4.2 核心流程

```text
拆分三步走：
① 函数化   把脚本中重复出现的段落包成函数（build_model / get_dataloader / train）
② 模块化   按职责把函数放进 model.py / data_utils.py / train_utils.py
③ 组装     训练脚本或 notebook 里：from model import build_model; ... 串起流水线
```

判断拆分是否合格的标准：换一个数据集时，只改 data_utils 与组装层；换模型结构时，只改 model 层；训练循环两者都不用改。

#### 4.4.3 源码精读

仓库虽然没有拆分，但「该拆什么」的证据写在代码的重复里：

- [ch13/ch13_part2.py:162-174](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L162-L174) 与 [ch13/ch13_part2.py:238-250](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L238-L250) — 同一个 `all_layers` 循环在 auto-mpg（`[8, 4]`，输出 1）与 MNIST（`[32, 16]`，输出 10，多一个 `nn.Flatten()`）里写了两遍，仅数字不同 → 抽成 `build_model()`。
- [ch13/ch13_part2.py:187-200](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L187-L200) 与 [ch13/ch13_part2.py:260-273](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L260-L273) — 两遍训练循环骨架完全相同 → 抽成 `train()`。第 13 章自己已有先例：[ch13/ch13_part1.py:282-306](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L282-L306) 把 XOR 训练包成了函数 `train(model, num_epochs, train_dl, x_valid, y_valid)`，并在 [L348](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L348) 对第二个模型直接复用——**函数化复用在同一章内已经发生**，模块化只是把它推进到跨文件。
- [ch13/ch13_part4_ignite.py:231-242](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py#L231-L242) — Ignite 把「保存」也工程化：`to_save = {"model": model, "optimizer": optimizer, "trainer": trainer}` 一次存三样，`Checkpoint` + `DiskSaver` 挂到 `Events.EPOCH_COMPLETED`，每轮自动写盘。这正是 4.4 的思想在框架层的体现（下一讲详讲）。

按书中建议拆分后的形态（**示例代码**，仓库未提供）：

```python
# 示例代码：mlp_utils.py —— 模型构建 + 数据 + 训练 三类工具
import torch, torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

def build_mlp(input_size, hidden_units, output_size):     # 模型构建工具
    layers, size = [], input_size
    for h in hidden_units:
        layers += [nn.Linear(size, h), nn.ReLU()]
        size = h
    layers.append(nn.Linear(size, output_size))
    return nn.Sequential(*layers)

def make_dataloader(X, y, batch_size=8, shuffle=True):    # 数据加载工具
    return DataLoader(TensorDataset(X, y), batch_size, shuffle=shuffle)

def train(model, num_epochs, train_dl, loss_fn, optimizer, log_epochs=20):  # 训练循环
    for epoch in range(num_epochs):
        total = 0
        for x_batch, y_batch in train_dl:
            pred = model(x_batch)[:, 0]
            loss = loss_fn(pred, y_batch)
            loss.backward(); optimizer.step(); optimizer.zero_grad()
            total += loss.item()
        if epoch % log_epochs == 0:
            print(f'Epoch {epoch}  Loss {total/len(train_dl):.4f}')
    return model
```

然后在 notebook 或 `main.py` 中组装：`model = build_mlp(x_train.shape[1], [8, 4], 1)`，三行完成过去三十行的流水线。对照 [ch13/ch13_part2.py:162-200](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L162-L200) 可以看到逐行对应关系。

#### 4.4.4 代码实践

1. **实践目标**：亲手完成一次「脚本 → 函数 → 模块」拆分，体会复用。
2. **操作步骤**：
   - 新建 `my_utils.py`（放在你自己的练习目录，**不要**改动仓库源码），把上面的示例代码抄入；
   - 写一段组装代码（示例代码）：

     ```python
     # 示例代码：组装 auto-mpg 流水线
     import torch, torch.nn as nn
     from my_utils import build_mlp, make_dataloader, train
     torch.manual_seed(1)
     # x_train/y_train 沿用 ch13_part2.py L133-L147 的产物
     model = build_mlp(x_train.shape[1], [8, 4], 1)
     train_dl = make_dataloader(x_train, y_train, batch_size=8)
     train(model, 200, train_dl, nn.MSELoss(), torch.optim.SGD(model.parameters(), lr=0.001))
     ```

   - 再用同一套工具组装 MNIST 版本（`build_mlp(28*28, [32, 16], 10)`），训练循环零改动。
3. **需要观察的现象**：两个项目共用同一个 `train()`；替换数据集时训练工具一行未改。
4. **预期结果**：auto-mpg 的损失曲线趋势与 [ch13/ch13_part2.py:199-200](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L199-L200) 打印的量级一致（首轮约几十、随后下降）。MNIST 版需改用 `CrossEntropyLoss` 并去掉 `[:, 0]`——这提示你 `train()` 想真正中立还需把「输出整理」参数化，是很好的改进练习。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：把 `train()` 抽成通用函数时，`loss_fn` 和 `optimizer` 应该在函数内创建还是由外部传入？为什么？

**答案**：由外部传入。损失与优化器是「任务与实验的选择」，训练循环是「中立骨架」；外部传入（依赖注入）使同一循环可用于 MSE 回归与交叉熵分类（仓库两段循环的差异恰好只剩这两样，见 4.4.3 的对照）。`optimizer` 绑定 `model.parameters()`，更必须在模型创建之后再由组装层构造。

**练习 2**：如果按书中方式把工程拆成多个 `.py` 模块，notebook 里应该保留什么？

**答案**：只保留「组装与叙事」——import 工具模块、设定超参数与随机种子、调用流水线、画图与解读；定义性代码（模型、数据、循环）全部进模块。这样 notebook 退化成实验记录，代码进版本库被复用与测试。仓库本身正是「notebook 为源、脚本为生成物」的双轨结构（见 u1-l1），工程化后这个关系反转为「模块为源、notebook 调用」。

## 5. 综合实践

**任务**：把第 13 章 XOR 多层模型训练若干轮，分别用「整个模型」与「state_dict」两种方式保存到磁盘，再在 CPU 上加载两者、对同一输入推理，验证输出完全一致，并回答两种方式对代码结构的不同要求。

完整脚本（**示例代码**，数据与模型取自 [ch13/ch13_part1.py:228-238](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L228-L238) 与 [ch13/ch13_part1.py:331-341](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L331-L341)）：

```python
# 示例代码：save_load_demo.py —— 两种保存方式的对照实验
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

# ① 数据：XOR（同 ch13_part1.py L228-238）
np.random.seed(1); torch.manual_seed(1)
x = np.random.uniform(low=-1, high=1, size=(200, 2))
y = np.ones(len(x)); y[x[:, 0] * x[:, 1] < 0] = 0
x_train = torch.tensor(x[:100], dtype=torch.float32)
y_train = torch.tensor(y[:100], dtype=torch.float32)
train_dl = DataLoader(TensorDataset(x_train, y_train), batch_size=2, shuffle=True)

# ② 模型：多层版（同 ch13_part1.py L331-338）
torch.manual_seed(1)
model = nn.Sequential(
    nn.Linear(2, 4), nn.ReLU(),
    nn.Linear(4, 4), nn.ReLU(),
    nn.Linear(4, 1), nn.Sigmoid())

# ③ 训练若干轮（五步骨架，同 ch13_part1.py L287-296）
loss_fn = nn.BCELoss()
optimizer = torch.optim.SGD(model.parameters(), lr=0.015)
num_epochs = 50
for epoch in range(num_epochs):
    for xb, yb in train_dl:
        pred = model(xb)[:, 0]
        loss = loss_fn(pred, yb)
        loss.backward(); optimizer.step(); optimizer.zero_grad()

# ④ 两种保存（对照 ch12_part2.py L341-342 与 L363-364）
torch.save(model, 'xor_full.pt')                 # 路线 A：整个模型
torch.save(model.state_dict(), 'xor_state.pt')   # 路线 B：参数字典

# ⑤ 加载并对同一输入推理比对
x_probe = torch.tensor([[0.5, -0.5], [-0.5, -0.5], [0.3, 0.3]], dtype=torch.float32)

m_a = torch.load('xor_full.pt'); m_a.eval()                    # 路线 A 加载
m_b = nn.Sequential(                                           # 路线 B：先搭同结构
    nn.Linear(2, 4), nn.ReLU(),
    nn.Linear(4, 4), nn.ReLU(),
    nn.Linear(4, 1), nn.Sigmoid())
m_b.load_state_dict(torch.load('xor_state.pt', map_location='cpu')); m_b.eval()

with torch.no_grad():
    out_a, out_b = m_a(x_probe), m_b(x_probe)
print('最大绝对差:', (out_a - out_b).abs().max().item())
print('整模型文件 loaded from pickle, state_dict loaded into new skeleton')
```

**操作步骤**：

1. 把脚本存到自己的练习目录并运行（需已安装 torch；数据本地生成，无需下载）。
2. 运行后检查 `最大绝对差` 是否为 `0.0`（同权重、同输入、同为 eval 且无随机层，输出应逐位一致）。
3. 再做一个结构实验：把脚本拆成两个文件——`model_def.py`（定义模型结构）与 `load_only.py`（只做第⑤步加载）。在 `load_only.py` 里加载 `xor_full.pt` 时 `import` 与不 `import` `model_def` 各试一次（本例模型是纯内置层的 `nn.Sequential`，同进程内通常都能加载；若把模型换成 u13-l1 的 `MyNoisyModule`，自定义类必须在加载环境可见——可参考 4.2.4 的 import 方式）。
4. 回答问题：两种方式分别要求加载端具备什么？

**预期结果**（**待本地验证**）：

- `最大绝对差` 为 0，两条路线恢复的是同一个函数；
- 探针点 `[0.5, -0.5]`、`[-0.5, -0.5]`（异号，XOR 类 0）与 `[0.3, 0.3]`（同号，类 1）的 sigmoid 输出应分别偏向 0、0、1（50 轮训练后方向正确即可，数值不必极端）；
- 结论：路线 A 要求加载环境能定位模型类定义（对自定义类即必须 import），路线 B 要求加载端**手写**出同名同形的骨架——前者省一行代码、后者换来对代码重构的免疫力，这也是官方与书中推荐 state_dict 的原因。

## 6. 本讲小结

- 任何训练工程都是五段流水线：数据获取 → 预处理 → 模型构建 → 训练循环 → 评估与保存；第 13 章两个项目（auto-mpg、MNIST）的骨架逐段同构，是拆分动机的直接证据。
- `torch.save(model, path)` 用 pickle 存整个对象，加载端必须能 import 到模型类，重构后易失效；`torch.save(model.state_dict(), path)` 只存「参数名 → 张量」的有序字典，加载端先搭同名同形骨架再 `load_state_dict`，是官方与书中推荐的方式。
- state_dict 的键由「子模块属性路径 + 参数注册名」拼成（如 `l1.w`、`l2.weight`），键名对不上即报错——严格匹配是保护而非麻烦。
- 张量携带 device 信息；跨设备加载存档用 `torch.load(..., map_location=...)` 指定落点，加载后模型与输入仍需统一 `to(device)`（仓库代码中无 map_location 实例，属书中讲解内容）。
- 工程化三步走：函数化（`build_mlp` / `make_dataloader` / `train`）→ 模块化（按模型/数据/训练分文件）→ notebook 只留组装与叙事；仓库保持教学用单脚本，拆分演示为本讲示例代码。
- 保存的最佳实践还包括：先建目录再写盘（ch14 的 `os.mkdir('models')`）、加载后 `eval()`（对内置 Dropout/BatchNorm 生效；`NoisyLinear` 靠显式 `training` 参数）、以及用字典一次保存 model+optimizer+trainer 以支持断点续训（Ignite `Checkpoint` 的做法，下一讲主角）。

## 7. 下一步学习建议

下一讲 **u13-l3 高层训练接口：PyTorch Lightning 与 Ignite** 会把本讲的「手工组织」交给框架：Lightning 的 `LightningModule` 把训练/验证步骤收进类、`ModelCheckpoint` 自动管理存档（[ch13/ch13_part3_lightning.py:273-274](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L273-L274) 的 `load_from_checkpoint` 一行完成本讲的路线 A+B）；Ignite 的 `Checkpoint` 处理器（[ch13/ch13_part4_ignite.py:231-263](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py#L231-L263)）正是「model+optimizer+trainer 打包保存」的落地。建议先消化本讲的 state_dict 机制，再看框架如何封装它；之后进入 u14 卷积神经网络时，你会再次用到本讲的设备管理与模型保存代码。
