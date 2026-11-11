# 高层训练接口：PyTorch Lightning 与 Ignite

## 1. 本讲目标

学完本讲,你应该能够:

1. 说出「原始 PyTorch 训练循环」里的哪些活儿(双层 for 循环、`zero_grad`/`backward`/`step`、设备转移、指标累计、checkpoint 保存)被 Lightning 和 Ignite 分别接管了,哪些仍要你自己写。
2. 读懂 `MultiLayerPerceptron(pl.LightningModule)` 中 `training_step` / `validation_step` / `test_step` / `configure_optimizers` 四类方法的分工,理解「把训练逻辑收进类」这一设计。
3. 读懂 `MnistDataModule(pl.LightningDataModule)` 的 `prepare_data` / `setup` / 三个 `*_dataloader` 方法,理解「数据准备收进类」带来的复用与防泄漏好处。
4. 会用 `pl.Trainer(max_epochs=..., callbacks=..., gpus=...)`、`trainer.fit` / `trainer.test(ckpt_path='best')` 完成训练、测试与最佳模型挑选。
5. 理解 Ignite 的核心抽象:`Engine`(对循环的封装)+ `Events`(事件)+ handler(回调函数),会用 `create_supervised_trainer` / `create_supervised_evaluator` 搭一套带日志、checkpoint、TensorBoard 的训练流程。
6. 能横向对比同一 MNIST 任务在「原始 PyTorch / Lightning / Ignite」三种写法下的代码量与结构差异,并说出各自适合的场景。

## 2. 前置知识

本讲是第 13 章的收官,建立在前面几讲的基础上。先把几个概念用通俗语言说清楚:

- **训练循环的五步骨架**(u12-l3、u13-l2 已建立):对每个 batch 执行「前向算损失 → `loss.backward()` → `optimizer.step()` → `optimizer.zero_grad()`,外面包一层 epoch 循环」。这五步是所有框架要替你重复写的部分。
- **epoch 与 iteration**:一个 epoch 是把训练集完整过一遍;一个 iteration 是处理一个 batch。55000 个样本、batch_size=64 时,每个 epoch 约 860 个 iteration(859 个满批 + 1 个 24 样本的尾批,因为 `DataLoader` 默认 `drop_last=False`,见 u12-l2)。
- **样板代码(boilerplate)**:几乎每个项目都要抄一遍、但又不含任何「你的模型特有信息」的代码,比如把张量搬去 GPU、每个 epoch 结束时算一遍验证集、定期保存 checkpoint。高层接口的卖点就是把这些样板收走。
- **回调(callback)与事件(event)**:一种「好莱坞原则」的编程模式——别来调用框架,框架会调用你。你先把「某件事发生时该执行什么函数」注册好,框架运行到那个时点就替你调用。Ignite 里的 `Events.EPOCH_COMPLETED`(一个 epoch 结束)就是事件,你注册的函数就是 handler(处理器/回调)。
- **指标(metric)的累计式计算**(u11-l2 的 `compute_mse_and_acc` 已见过雏形):不能简单地对「每个 batch 的准确率」取平均——尾批只有 24 个样本,权重应当更小。正确做法是累计所有样本再整体计算:

\[ \text{Acc}=\frac{1}{\sum_b n_b}\sum_b \#\{i\in b:\hat{y}_i=y_i\} \quad\text{而非}\quad \frac{1}{B}\sum_b \text{acc}_b \]

  torchmetrics 的 `Accuracy` 就是「`update` 累计 → `compute` 总算 → `reset` 清零」的三段式。
- **checkpoint 与 state_dict**(u13-l2):存档里保存的是参数字典(以及优化器状态),用于断点续训和最佳模型恢复。本讲会看到两个框架如何把「定期保存、按验证指标挑最佳」自动化。
- **TensorBoard**:一个把标量(损失、准确率)随训练进程画成曲线的看板工具,`pip install tensorboard` 后用 `tensorboard --logdir=目录` 启动。

**版本提醒**:仓库代码基于 PyTorch Lightning 1.5.0、torchmetrics 0.6.2(见 [ch13/ch13_part3_lightning.py:L36-L43](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L36-L43) 的版本检查字典)。若你安装的是 Lightning 2.x,`gpus=1`、`resume_from_checkpoint`、`training_epoch_end` 等 API 已有变动(后文相应处会标注),建议按 u1-l2 的方法建书配钉版本环境再跑。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [ch13/ch13_part3_lightning.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py) | 用 PyTorch Lightning 训练 MNIST 多层感知机 | `MultiLayerPerceptron`、`MnistDataModule`、`Trainer` 的全部用法 |
| [ch13/ch13_part4_ignite.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py) | 用 PyTorch-Ignite 训练同一个 MNIST 模型 | `create_supervised_trainer/evaluator`、`Events` 回调、`Checkpoint`、`TensorboardLogger` |
| [ch13/ch13_part2.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py) | 原始 PyTorch 写法的 MNIST 训练(对照基准) | L219-L280 的数据、模型与手写训练循环 |
| [ch13/ch13_part1.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py) | 第 13 章前半部分:XOR 项目的通用 `train()` 函数(对照基准) | L281-L306 的双层循环与五步骨架 |

三个文件训练的是**同一个任务、几乎同一个模型**(Flatten → Linear(784,32) → ReLU → Linear(32,16) → ReLU → Linear(16,10)),正好构成一场控制变量实验:模型和数据不变,只变「训练代码的组织方式」。

## 4. 核心概念与源码讲解

### 4.1 LightningModule:把训练逻辑收进类

#### 4.1.1 概念说明

u12-l3 和 u13-l2 里,模型只是 `nn.Module`(定义 `forward`),训练循环是一个独立的函数或脚本段落——模型与训练流程是分离的。Lightning 的第一个核心思想是:**把「一个 batch 的训练/验证/测试该怎么算」和「用哪个优化器」写成模型类的成员方法**,框架拿着这些方法去替你组装完整循环。

这样做的直接收益:

- 这些方法变成可复用、可单测的单元,换数据集、换 Trainer 配置都不用改;
- Lightning 通过方法名就能知道你的意图,从而自动决定何时 `model.train()` / `model.eval()`、何时关梯度、把设备转移、日志记录统一处理。

#### 4.1.2 核心流程

Trainer 每个 iteration 大致执行(伪代码,标注「你写」/「框架代劳」):

```
for epoch in range(max_epochs):                     # 框架代劳
    for batch_idx, batch in enumerate(train_dl):    # 框架代劳
        loss = model.training_step(batch, batch_idx)  # 你写:前向 + 算损失 + 记指标
        loss.backward()                             # 框架代劳
        optimizer.step()                            # 框架代劳(优化器来自 configure_optimizers)
        optimizer.zero_grad()                       # 框架代劳
    model.training_epoch_end(outs)                  # 你写(旧版 API):整个 epoch 的指标汇总
```

对比 [ch13/ch13_part1.py:L287-L293](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L287-L293) 中手写的同一段,你会发现 `training_step` 就是把五步骨架里「除了三行梯度代码」之外的部分抽成了方法。

#### 4.1.3 源码精读

类的定义与构造:[ch13/ch13_part3_lightning.py:L72-L97](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L72-L97) 定义 `MultiLayerPerceptron(pl.LightningModule)`。构造函数里做两件事:

1. 创建三个 torchmetrics `Accuracy` 指标对象([L78-L85](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L78-L85))——用 `parse_version` 比较 torchmetrics 版本,高于 0.8 时必须传 `task="multiclass", num_classes=10`,否则用旧的无参构造。这是仓库为兼容新版 torchmetrics 打的补丁,也印证了「版本钉死」的必要性。
2. 搭建与 ch13_part2 完全相同的 `nn.Sequential` 模型([L88-L97](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L88-L97)),注意与 [ch13/ch13_part2.py:L242-L250](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L242-L250) 逐行同构。

`training_step`:[ch13/ch13_part3_lightning.py:L103-L110](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L103-L110)

```python
def training_step(self, batch, batch_idx):
    x, y = batch
    logits = self(x)
    loss = nn.functional.cross_entropy(logits, y)
    preds = torch.argmax(logits, dim=1)
    self.train_acc.update(preds, y)
    self.log("train_loss", loss, prog_bar=True)
    return loss
```

五个动作:解包 batch → 前向 → 算交叉熵损失 → 取 argmax 得预测 → 把 (预测, 标签) 喂给指标对象累计。`self.log("train_loss", loss, prog_bar=True)` 把当前损失登记到 Lightning 的日志系统并显示在进度条上;**返回值就是 loss,框架拿它去 `backward()`**。多分类输出层用交叉熵、输出层不套 Softmax(logits 进 `CrossEntropyLoss`),这与 u12-l3 讲的「输出层按任务匹配」一致。

`training_epoch_end`:[ch13/ch13_part3_lightning.py:L112-L114](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L112-L114) 在整个 epoch 结束时调用 `self.train_acc.compute()` 得到按样本加权的整体准确率(见第 2 节公式),登记为 `"train_acc"`,再 `reset()` 清空累计、供下个 epoch 使用。**若用 Lightning 2.x,此方法已被移除**,替代做法是在 `self.log(...)` 中加 `on_epoch=True` 并改用 `on_train_epoch_end`。

`validation_step` / `validation_epoch_end`:[ch13/ch13_part3_lightning.py:L116-L127](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L116-L127) 与训练版逐行对称,唯一差别是不做参数更新(框架已保证在 `no_grad` 与 `eval` 模式下执行)且记录的键名为 `"valid_loss"` / `"valid_acc"`。记住 `"valid_acc"` 这个键名——4.3 节的 ModelCheckpoint 靠它挑最佳模型。

`test_step`:[ch13/ch13_part3_lightning.py:L129-L137](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L129-L137) 结构相同,小差异是它直接在 L136 每批之后 `compute()` 并登记 `test_acc`,没有单独的 epoch_end。

`configure_optimizers`:[ch13/ch13_part3_lightning.py:L139-L141](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L139-L141) 返回 `torch.optim.Adam(self.parameters(), lr=0.001)`,与 [ch13/ch13_part2.py:L258](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L258) 完全一致——优化器的选择权始终在你手里。

#### 4.1.4 代码实践(源码阅读型)

1. **实践目标**:亲手验证「training_step 就是手写循环的内层」。
2. **操作步骤**:打开 [ch13/ch13_part1.py:L282-L306](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L282-L306) 的 `train()` 函数与 `training_step`,做一张左右对照表:内层循环的每一行对应 `training_step` 的哪一行、或被框架的哪项代劳(如 L291 `loss.backward()` → Trainer 代劳;L294 的 `loss_hist_train[epoch] += loss.item()` → `self.log` 代劳)。
3. **需要观察的现象**:手写版 14 行内层循环,`training_step` 只有 6 行,而且**一行梯度代码都没有**。
4. **预期结果**:对照表中「框架代劳」应覆盖 backward / step / zero_grad / batch 转移设备 / epoch 边界管理五项;`training_step` 只保留「你的模型特有」的前向与损失定义。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `training_step` 里不写 `optimizer.zero_grad()`?
**答案**:`zero_grad`/`backward`/`step` 是所有模型都相同的骨架,由 Trainer 统一执行;`training_step` 只负责返回 loss。这也解释了 u12-l3 强调的「梯度是累加语义」在此处为何不再是隐患——清零由框架保证。

**练习 2**:如果去掉 `self.train_acc.reset()`([L114](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L114)),第 2 个 epoch 登记的 `train_acc` 会是什么?
**答案**:前两个 epoch **全部样本**的整体准确率(累计未清空),数值会与第 1 个 epoch 的曲线不衔接,第 2 个 epoch 的「自身准确率」无法单独读出。

**练习 3**:三个 `Accuracy` 对象为什么分开建 `train_acc`/`valid_acc`/`test_acc`,而不是共用一个?
**答案**:metric 对象内部有累计状态;共用会让训练批的样本混进验证/测试指标,验证与测试就不再「干净」。分开建并各自 reset,保证三条曲线各自统计各自的样本。

### 4.2 LightningDataModule:数据准备也收进类

#### 4.2.1 概念说明

LightningModule 收编了「训练逻辑」,`LightningDataModule` 收编「数据逻辑」:下载、切分、变换、三个 DataLoader 的构造全部封装成一个可复用对象。好处有三:

1. **一处定义、处处复用**:fit / validate / test / predict 都从同一个对象取数,保证各阶段用的是同一套切分与变换;
2. **防数据泄漏的结构保障**:切分只发生在 `setup` 一处,且可以按 stage 区分,不会在笔记本里散落多处 `random_split`;
3. **多进程安全**:`prepare_data`(只在该进程执行一次的下载)与 `setup`(每个进程都要做的切分)分离,为分布式训练铺路。

#### 4.2.2 核心流程

```
实例化 mnist_dm = MnistDataModule()
trainer.fit(model, datamodule=mnist_dm)
 ├─ prepare_data()      # 下载 MNIST(只在主进程执行一次)
 ├─ setup('fit')        # 加载训练数据 → random_split 出 train/val → 存为 self.train/self.val
 ├─ train_dataloader()  # 每个阶段取各自的 DataLoader
 └─ val_dataloader()
trainer.test(..., datamodule=mnist_dm)
 └─ setup('test') 等价路径 → test_dataloader()
```

#### 4.2.3 源码精读

类定义:[ch13/ch13_part3_lightning.py:L153-L190](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L153-L190)。

`prepare_data`([L159-L160](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L159-L160)):只负责 `MNIST(root=..., download=True)`,即「数据不在就下载」。

`setup`([L162-L181](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L162-L181))做三件事:加载 6 万张训练图([L165-L170](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L165-L170)) → 用 `random_split` 按 55000/5000 切出训练/验证集,并用 `torch.Generator().manual_seed(1)` 钉死随机种子保证可复现([L172-L174](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L172-L174)) → 单独加载 1 万张测试图([L176-L181](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L176-L181))。注释 L163 写明 `stage` 可取 `'fit'/'validate'/'test'/'predict'`,可用它按阶段走不同分支。

三个 dataloader 方法([L183-L190](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L183-L190)):train/val/test 各返回一个 `DataLoader(..., batch_size=64, num_workers=4)`,batch_size 与 num_workers 的语义正是 u12-l2 讲过的内容。

**与原始写法、Ignite 写法的三方对照**:[ch13/ch13_part2.py:L219-L233](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L219-L233) 里数据和 DataLoader 是散在脚本顶层的裸代码,且**没有验证集**(训练全程只看训练准确率);Ignite 版 [ch13/ch13_part4_ignite.py:L120-L125](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py#L120-L125) 直接把测试集当验证集用(源码注释 L161 明说这是为简化而为之,若用它做模型选择会损失无偏性);只有 Lightning 版真正切出了 5000 张验证集。这是 DataModule「结构防泄漏」的最好注脚。

#### 4.2.4 代码实践(阅读 + 本地验证型)

1. **实践目标**:理解切分比例如何影响每个 epoch 的 iteration 数。
2. **操作步骤**:把 [L172-L174](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L172-L174) 的 `[55000, 5000]` 改成 `[50000, 10000]`,重跑训练,观察进度条上的总 iteration 数。
3. **需要观察的现象**:每 epoch iteration 数从约 860(55000/64,含尾批)变为约 782(50000/64)。
4. **预期结果**:总 iteration 数按比例下降;`valid_acc` 的数值可能因验证集变大而略有变化。**待本地验证**(需要安装 pytorch_lightning 与下载 MNIST)。

#### 4.2.5 小练习与答案

**练习 1**:`prepare_data` 和 `setup` 为什么拆成两个方法,而不是都在 `__init__` 里做完?
**答案**:`__init__` 只应做轻量配置(存路径、定义 transform);下载放在 `prepare_data` 保证多进程/分布式下只执行一次;切分放在 `setup` 保证每个进程各自持有 `self.train` 等对象。混在 `__init__` 里两者都无法保证。

**练习 2**:`random_split` 里的 `generator=torch.Generator().manual_seed(1)` 若删掉,会有什么后果?
**答案**:每次运行切出的 55000/5000 划分不同,`valid_acc` 不可复现、不同实验之间不可比——相当于丢掉了 u6-l1 强调的「固定随机种子」原则。

### 4.3 Trainer 与 ModelCheckpoint:一行 fit 背后的调度

#### 4.3.1 概念说明

`pl.Trainer` 是 Lightning 的「总调度」:你把 LightningModule 和 DataModule 交给它,它负责循环、设备分配、日志聚合、进度条、checkpoint 回调、 TensorBoard 写入。你配置它的方式就是构造参数:`max_epochs`、`callbacks`、`gpus`(设备)等。这一节的重点是看清 **Trainer 参数、模块里的 `self.log` 键名、checkpoint 文件** 三者如何串成一条链。

#### 4.3.2 核心流程

```
callbacks = [ModelCheckpoint(save_top_k=1, mode='max', monitor="valid_acc")]
trainer = pl.Trainer(max_epochs=10, callbacks=callbacks)   # 有 GPU 时再加 gpus=1
trainer.fit(model, datamodule)          # 训练;每个 epoch 末用 valid_acc 挑最佳并存档
trainer.test(model, datamodule, ckpt_path='best')          # 加载验证准确率最高的存档测试
```

#### 4.3.3 源码精读

创建回调与 Trainer:[ch13/ch13_part3_lightning.py:L203-L212](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L203-L212)。

- L205 `ModelCheckpoint(save_top_k=1, mode='max', monitor="valid_acc")`:监控的名字必须与 `validation_epoch_end` 里 `self.log("valid_acc", ...)` 的键**完全一致**;`mode='max'` 表示准确率越大越好;`save_top_k=1` 只保留最好的一个存档。
- L207-L210 用 `torch.cuda.is_available()` 分支:有 GPU 时传 `gpus=1`(Lightning 2.x 改为 `accelerator="gpu", devices=1`)。
- L212 `trainer.fit(model=mnistclassifier, datamodule=mnist_dm)` 一行启动训练——对比 [ch13/ch13_part2.py:L260-L273](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part2.py#L260-L273) 的 14 行手写循环,循环本身被压缩成了一个方法调用。

用最佳存档测试:[ch13/ch13_part3_lightning.py:L219](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L219) `trainer.test(..., ckpt_path='best')` 会先从磁盘加载验证准确率最高的 checkpoint 再测试,这正是 u6-l1「调参用验证集、最终成绩用测试集」思想的自动化版本。

断点续训:[ch13/ch13_part3_lightning.py:L239-L250](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L239-L250) 演示从 `lightning_logs/version_0/checkpoints/epoch=8-step=7739.ckpt` 恢复,新建 `max_epochs=15` 的 Trainer 并传 `resume_from_checkpoint=path` 再 `fit`。文件名自带两个信息:epoch 序号(0 基,`epoch=8` 是第 9 个 epoch)与全局 step 数——7739 与「每 epoch 约 860 个 iteration × 9 个 epoch ≈ 7740」大体对应(差 1 来自计数起点)。**Lightning 2.x 中 `resume_from_checkpoint` 已移除**,改为 `trainer.fit(model, datamodule, ckpt_path=path)`。

单独加载模型:[ch13/ch13_part3_lightning.py:L273-L274](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L273-L274) `MultiLayerPerceptron.load_from_checkpoint(path)` 不需要 Trainer 就能重建模型——它与 u13-l2 的 `load_state_dict` 同源,只是 Lightning 替你完成了「先搭骨架再注入参数」两步。

#### 4.3.4 代码实践(本地验证型)

1. **实践目标**:确认 checkpoint 文件名与 epoch/step 的对应关系,以及 `ckpt_path='best'` 的挑选行为。
2. **操作步骤**:先把 [L208/L210](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L207-L210) 的 `max_epochs=10` 改为 5,运行脚本;训练结束后查看 `lightning_logs/version_N/checkpoints/` 下的文件名,再对比 `trainer.test(ckpt_path='best')` 与 `trainer.test()`(不传,用当前权重)两次输出的 `test_acc`。
3. **需要观察的现象**:存档文件名形如 `epoch=X-step=Y.ckpt`,X 通常不是最后一个 epoch(而是 valid_acc 最高的那个);两次 test 的准确率可能不同。
4. **预期结果**:`ckpt_path='best'` 版本 ≥ 当前权重版本(因为选的是验证集上最好的历史存档)。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**:若把 `monitor="valid_acc"` 误写成 `"valid_accuracy"`,训练会怎样?
**答案**:ModelCheckpoint 找不到该键名对应的指标,通常会在第一个 epoch 结束时告警/报错且不保存存档,`ckpt_path='best'` 随后无从加载。键名是 `self.log` 与回调之间的隐式契约。

**练习 2**:为什么续训时新建的 Trainer 用 `max_epochs=15` 而不是接着 10 再加 5?
**答案**:`max_epochs` 指的是「整个训练过程的总轮数上限」,从 `epoch=8` 的存档恢复后继续训练到第 15 轮为止;若写 10 则恢复后发现已接近目标轮数而几乎不再训练。

### 4.4 Ignite:用 Engine 保留循环、用回调拼装行为

#### 4.4.1 概念说明

Ignite 与 Lightning 的哲学不同。Lightning 把循环**藏起来**(你只写 step);Ignite 把循环**封装但保持可见**——核心类 `Engine` 接收一个「处理单个 batch 的函数」,替你跑双层循环并在各个时点触发事件,你再用回调把日志、验证、存档挂上去。书中源码注释([ch13/ch13_part4_ignite.py:L70-L79](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py#L70-L79))总结为四件套:引擎与事件系统、开箱即用指标、内置 handlers、分布式支持。设计取向是「比纯 PyTorch 少写代码,但保留 PyTorch 式的完全控制」。

#### 4.4.2 核心流程

数据与模型准备仍是纯 PyTorch(见 [ch13/ch13_part4_ignite.py:L105-L156](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py#L105-L156),`get_model` 函数 + 手动 `.to(device)`),然后:

```
trainer = create_supervised_trainer(model, optimizer, loss_fn, device=device)   # 训练引擎
evaluator = create_supervised_evaluator(model, metrics={...}, device=device)    # 评估引擎
# 给 trainer 挂事件回调(日志、checkpoint)→ 给 evaluator 挂回调(验证指标、最佳模型)
trainer.run(train_loader, max_epochs=5)                                          # 一行启动
```

`create_supervised_trainer` 内部的循环等价于(伪代码):

```
while epoch < max_epochs:
    for batch in data:
        model.train(); x, y = move_to_device(batch)
        y_pred = model(x); loss = loss_fn(y_pred, y)
        optimizer.zero_grad(); loss.backward(); optimizer.step()
        触发 Events.ITERATION_COMPLETED(把 loss 存进 trainer.state.output)
    触发 Events.EPOCH_COMPLETED
触发 Events.COMPLETED
```

也就是 u12-l3 的五步骨架原封不动,只是由引擎执行、并在固定时点发事件。

#### 4.4.3 源码精读

创建两个引擎:[ch13/ch13_part4_ignite.py:L171-L182](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py#L171-L182)。

- `create_supervised_trainer(model, optimizer, loss_fn, device=device)`:三样你熟悉的原料 + 设备,返回一个 `Engine` 实例;
- `val_metrics = {"accuracy": Accuracy(), "loss": Loss(loss_fn)}`:评估用的指标表,键名是自定义的,之后从 `state.metrics` 按键取值;
- `create_supervised_evaluator(model, metrics=val_metrics, device=device)`:评估引擎,跑完一个 loader 后自动算好表里的全部指标。

**trainer 与 evaluator 都是 `Engine`**(源码注释 L185 点明),区别只在内部包装的 update 函数:一个做参数更新,一个只前向并更新指标。这种「同一抽象两种用途」是 Ignite 设计的关键。

模型定义的一个细节:[ch13/ch13_part4_ignite.py:L137-L149](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py#L137-L149) 的 `get_model` 在输出层后追加了 `nn.Softmax(dim=1)`(L147),而 part2 原始版与 Lightning 版都输出 logits。严格说 `nn.CrossEntropyLoss` 期望未归一化的 logits(内部自带 log-softmax,见 u12-l3),先 Softmax 再进交叉熵属于重复归一化——数值上仍可训练,但不是推荐写法,读者复现时以「输出 logits」为准。这是同一仓库内三种实现的一处不一致,读码时值得留意。

启动训练:[ch13/ch13_part4_ignite.py:L300](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py#L300) `trainer.run(train_loader, max_epochs=5)` 一行完成,`max_epochs` 是 `run` 的参数而非构造参数(与 Lightning 不同)。

#### 4.4.4 代码实践(阅读 + 本地验证型)

1. **实践目标**:把「手写循环」逐行映射到 `create_supervised_trainer` 的内部行为。
2. **操作步骤**:对照 [ch13/ch13_part1.py:L287-L293](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L287-L293) 与 4.4.2 的伪代码,写一张「手写行 → 引擎行为」映射表;然后把 [ch13_part4_ignite.py:L300](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py#L300) 的 `max_epochs` 从 5 改成 2,实际运行脚本。
3. **需要观察的现象**:终端每 100 个 iteration 打印一次训练损失、每个 epoch 结束打印一行验证结果(来自 4.5 节的回调);改成 2 后只打印两轮。
4. **预期结果**:2 个 epoch 的验证准确率略低于 5 个 epoch(约低零点几个百分点)。**待本地验证**(需 `pip install pytorch-ignite`,见源码注释 L86-L94 的安装说明)。

#### 4.4.5 小练习与答案

**练习 1**:Ignite 的 trainer 和 evaluator 为什么要分成两个 Engine,而不是一个?
**答案**:训练与评估的 update 逻辑不同(是否更新参数、是否 `no_grad`、算哪些指标),拆成两个引擎各配各的指标表,职责单一;两个引擎又能通过事件互相触发(见 4.5 的 `log_validation_results`),组合出「每轮训练后评估」。

**练习 2**:与 Lightning 相比,Ignite 版里设备管理是更省心还是更操心?
**答案**:更操心。Ignite 版要自己写 `device = "cuda" if torch.cuda.is_available() else "cpu"` 并把 `device` 传给两个引擎([L152-L182](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py#L152-L182));Lightning 只需 Trainer 传 `gpus=1`,模块与数据的转移全部自动。

### 4.5 Ignite 的 Events 事件系统与 handlers

#### 4.5.1 概念说明

`Engine` 在固定时点触发事件(事件是一个枚举 `Events`,如 `ITERATION_COMPLETED`、`EPOCH_COMPLETED`、`COMPLETED`),你把普通 Python 函数挂到事件上,它们就成了回调。挂接有两种等价写法:**装饰器** `@trainer.on(Events.XXX)` 与显式调用 `trainer.add_event_handler(Events.XXX, fn)`。此外 Ignite 内置了一类现成回调——handlers,如 `Checkpoint`(存档)、`DiskSaver`(写磁盘)、`TensorboardLogger`(写 TensorBoard),直接拿来挂即可,不必自己写函数。

#### 4.5.2 核心流程

```
@trainer.on(Events.ITERATION_COMPLETED(every=100))   # 带过滤器:每 100 个 iteration
def log_training_loss(): ...                          # 打印 trainer.state.output 里的 batch 损失

@trainer.on(Events.EPOCH_COMPLETED)                   # 每个 epoch 结束
def log_validation_results():
    evaluator.run(val_loader)                          # 跑评估引擎 → 读 state.metrics

trainer.add_event_handler(Events.EPOCH_COMPLETED, checkpoint_handler)   # 每 epoch 存档
evaluator.add_event_handler(Events.COMPLETED, best_model_handler)       # 每次评估后按准确率存最佳
tb_logger.attach_output_handler(...)                  # 指标写入 TensorBoard
```

`trainer.state` 是引擎的「仪表盘」:`state.epoch`、`state.max_epochs`、`state.iteration`、`state.output`(当前 batch 的损失)在回调里随取随用。

#### 4.5.3 源码精读

训练日志回调:[ch13/ch13_part4_ignite.py:L193-L202](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py#L193-L202)。`Events.ITERATION_COMPLETED(every=log_interval)` 是「事件 + 过滤器」的组合,`log_interval=100` 意味着并非每个 batch 都打印(否则 938 个 iteration/epoch 会刷屏);函数体从 `trainer.state` 取 epoch、iteration 和当前批损失并格式化打印。

验证回调:[ch13/ch13_part4_ignite.py:L212-L220](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py#L212-L220)。挂在 **trainer** 的 `EPOCH_COMPLETED` 上,函数体却去 `evaluator.run(val_loader)`——「训练引擎的 epoch 结束」触发「评估引擎完整跑一遍验证集」,这是两个 Engine 协作的典型写法;`eval_state.metrics['accuracy']` 与 `['loss']` 正是 4.4 建指标表时自定义的键名。

每 epoch 存档:[ch13/ch13_part4_ignite.py:L230-L242](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py#L230-L242)。`to_save = {"model": model, "optimizer": optimizer, "trainer": trainer}` 说明存档内容不止模型参数(与 u13-l2 的 checkpoint 概念对齐:含优化器状态才能严格续训);`DiskSaver(dirname="./output", require_empty=False)` 负责写盘;`Checkpoint(to_save, save_handler, filename_prefix="training")` 组装后用**显式** `trainer.add_event_handler(Events.EPOCH_COMPLETED, checkpoint_handler)` 挂上——源码注释 L205 特意说明这与装饰器写法等价。

按验证准确率存最佳模型:[ch13/ch13_part4_ignite.py:L253-L263](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py#L253-L263)。又一个 `Checkpoint`,但 `n_saved=1`、`score_name="accuracy"`、`score_function=Checkpoint.get_default_score_fn("accuracy")`(从 evaluator 的指标表里取 accuracy 打分),并且挂在 **evaluator** 的 `COMPLETED` 上——每次评估完就「以准确率为分数」覆盖式保存,与 Lightning `ModelCheckpoint(monitor="valid_acc", mode='max', save_top_k=1)` 目的相同,只是这里需要你亲手接线。

TensorBoard 日志:[ch13/ch13_part4_ignite.py:L266-L291](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py#L266-L291)。`TensorboardLogger(log_dir=output_path)` 之后调用两次 `attach_output_handler`:一次挂 trainer 的 `ITERATION_COMPLETED(every=100)` 写批损失(`output_transform=lambda loss: {"batch_loss": loss}` 把引擎输出变换为要记录的字典),一次挂 evaluator 的 `EPOCH_COMPLETED` 写全部指标(`metric_names="all"`),`global_step_transform=global_step_from_engine(trainer)` 让验证曲线的横坐标对齐训练引擎的全局步数而非评估引擎自己的步数——否则两条曲线在 TensorBoard 里对不齐。启动看板:`tensorboard --logdir='./output'`(源码注释 L303-L305)。

#### 4.5.4 代码实践(本地验证型)

1. **实践目标**:体会「回调挂接点」与「执行频率」的关系。
2. **操作步骤**:把 [L194](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py#L194) 的 `log_interval` 从 100 改为 10,运行脚本;再把 L212 的装饰器从 `Events.EPOCH_COMPLETED` 改成 `Events.ITERATION_COMPLETED(every=200)`,观察验证输出的频率变化。
3. **需要观察的现象**:第一步打印行数变为原来的 10 倍;第二步验证评估从每 epoch 一次变成每 200 个 iteration 一次(一个 epoch 内出现多行验证结果,且每行都要把 10000 张验证图整个跑一遍,训练明显变慢)。
4. **预期结果**:确认「事件决定挂接点、过滤器决定频率、评估本身有成本」三点。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**:`log_training_loss` 挂在 trainer 上、`best_model_handler` 挂在 evaluator 上,为什么不能反过来?
**答案**:`best_model_handler` 的分数函数要从评估指标里取 accuracy,而只有 evaluator 跑完验证集后指标才就绪;挂在 trainer 的 `EPOCH_COMPLETED` 上时指标表还没更新,分数无从谈起(且那一刻 `COMPLETED` 尚未触发)。

**练习 2**:Ignite 的 `Checkpoint(to_save={...})` 与 u13-l2 讲的 `torch.save(model.state_dict())` 有什么关系?
**答案**:前者是后者的自动化包装——按事件定时触发、可同时保存 model/optimizer/trainer 多个对象、还能按分数保留最佳;底层仍是把各对象的 state_dict 序列化到磁盘。

## 5. 综合实践

**任务:同一模型的三种训练写法对比实验。**

> 说明:大纲原始任务写的是「把 Lightning 模型的 epoch 从 200 降到 50」。核对当前 HEAD 源码,Lightning 脚本的轮数实为 `max_epochs=10`([L208/L210](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py#L207-L210));「200」其实是原始 PyTorch XOR 项目的 `num_epochs=200`([ch13/ch13_part1.py:L281](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part1.py#L281))。实践按真实代码执行。

**步骤 1(Lightning 轮数扫描)**:在 [ch13_part3_lightning.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part3_lightning.py) 中把 `max_epochs` 分别设为 5、10、15 各跑一次,每次记录:(a) 进度条/日志里的 `valid_acc`;(b) `trainer.test(ckpt_path='best')` 输出的 `test_acc`;(c) checkpoints 目录里最终存档文件名中的 epoch 号。

**步骤 2(Ignite 运行)**:安装 `pytorch-ignite`(版本 0.4.x,见源码注释 L86),运行 [ch13_part4_ignite.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch13/ch13_part4_ignite.py)(`max_epochs=5`),记录每个 epoch 打印的 `Avg Accuracy`,并检查 `./output` 目录下生成的 `training_*.ckpt` 与 `best_*.ckpt` 文件。

**步骤 3(代码量与结构对比)**:用统一口径(只统计与「训练执行」直接相关的代码,排除数据下载与可视化)填写下表并数行数:

| 维度 | 原始 PyTorch(part2) | Lightning(part3) | Ignite(part4) |
| --- | --- | --- | --- |
| 训练循环谁写 | 自己写(L260-L273,14 行) | 框架(`training_step` 6 行/步) | 框架(`trainer.run` 1 行启动) |
| 设备转移 | 无(CPU)/自己 `.to(device)` | `gpus=1` 一个参数 | 手动传 `device` 给两个引擎 |
| 验证集 | 无 | `random_split` 55000/5000 | 直接用测试集(源码已注明) |
| 指标累计 | 手写 `accuracy_hist_train` | torchmetrics 三段式 | `Accuracy`/`Loss` 指标表 |
| 最佳模型存档 | 无 | `ModelCheckpoint` 一行配置 | `Checkpoint` + 分数函数手动接线 |

**步骤 4(写总结)**:用 200 字左右回答:三种写法各自的总行数是多少?在小模型场景下「行数更少」是否是选择框架的主要理由?如果不是,是什么?

**预期结果**(待本地验证):Lightning 与 Ignite 的验证/测试准确率都应在 0.93~0.96 区间(与 u11 手写 MLP 的 94%~95% 相当,模型结构相同量级);`max_epochs` 从 5 到 15,`valid_acc` 前期上升明显、后期趋平甚至过拟合。总结的参考要点:这个规模下三种写法总行数相差不大(Lightning 甚至因类定义更多而更长),框架的真实收益在于**结构**:验证集切分、最佳模型挑选、断点续训、TensorBoard 这些「容易忘写的工程项」被标准化了;Lightning 适合想要「约定优于配置」的快速实验,Ignite 适合想要「循环可见、按事件精细控制」的场景。

## 6. 本讲小结

- `LightningModule` 把「单 batch 的训练/验证/测试怎么算」(`training_step` / `validation_step` / `test_step`)和「用哪个优化器」(`configure_optimizers`)收进模型类,梯度三件套(backward/step/zero_grad)全部交给 Trainer。
- torchmetrics 指标对象用 `update → compute → reset` 三段式累计,epoch 末得到的是按样本数加权的整体准确率,而非批准确率的平均。
- `LightningDataModule` 用 `prepare_data`(下载一次)/ `setup`(切分)/ 三个 `*_dataloader` 统一数据准备;它是三种实现中唯一真正切出 5000 张验证集的版本。
- `ModelCheckpoint(monitor="valid_acc", mode='max', save_top_k=1)` 与 `self.log` 的键名构成隐式契约;`trainer.test(ckpt_path='best')` 自动加载验证集上最优的存档。
- Ignite 的核心是 `Engine` + `Events`:循环被封装但仍可见,日志、验证、checkpoint、TensorBoard 全部以回调/ handler 的形式挂接到具体事件上,`trainer.run(data, max_epochs=5)` 一行启动。
- 框架的价值不在于少写几行代码,而在于把验证、存档、续训、日志这些工程项标准化;Lightning 藏循环、Ignite 露循环,选型取决于你想要的控制粒度。

## 7. 下一步学习建议

- 下一单元进入第 14 章(卷积神经网络):建议先读 [ch14/ch14_part1.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py) 中手写的 `conv1d`/`conv2d`,CNN 的训练循环仍可用本讲的三种方式书写——试着用 Lightning 改写 ch14 的训练代码是最好的巩固练习。
- 想深入 Lightning:对照阅读 `training_epoch_end` 在 Lightning 2.x 中的替代写法(`on_train_epoch_end` + `self.log(on_epoch=True)`),体会框架 API 演进对复现代码的影响。
- 想深入 Ignite:官方文档的 `Engine` 自定义 update 函数示例(不使用 `create_supervised_trainer`,直接 `Engine(step_fn)`)能帮你彻底看清「引擎只是循环 + 事件」这一本质。
- 建议把本讲的对比表扩展到 ch14 的 ResNet 微调场景:当模型更大、训练更久时,`ModelCheckpoint` 与断点续训的价值会远超本讲的小实验。
