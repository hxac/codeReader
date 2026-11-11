# PyTorch Lightning 基础

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 PyTorch Lightning 的分工哲学：**你只写「每个 batch 要做什么」，epoch 循环、设备搬运、进度条、日志与 checkpoint 全部交给 `Trainer`**。
2. 会实现 `LightningModule`：`__init__` / `forward` / `training_step` / `validation_step` / `test_step` / `configure_optimizers` 六个挂钩点。
3. 会用 `LightningDataModule` 把「下载 → 划分 → 三个 DataLoader」封装成可复用组件。
4. 会配置 `Trainer`（epoch 数、回调、logger、确定性），会用 `%tensorboard` / `CSVLogger` 看训练曲线。
5. 会用 `ModelCheckpoint` 保存验证集上最优的模型，会用 `ckpt_path='best'` 评估最优权重、用 `load_from_checkpoint` 做推理，并从 checkpoint 断点续训。

本讲是第 8 单元（工程化）的第一讲：它把此前 7 个单元里反复手写的训练循环「外包」给框架，是理解 Lightning 版模型库、官方模板与批量实验脚本的钥匙。

## 2. 前置知识

本讲默认你已掌握以下内容（均来自前面讲义）：

- **五步训练循环**（u2-l2、u4-l4）：`forward → loss → zero_grad → backward → step`。本讲要做的，就是把这套循环连同 epoch 层、日志层一起交给 Lightning。
- **共享工具链**（u4-l4）：`helper_train.train_classifier_simple_v1` 返回记录损失的 `log_dict`，v2 增加「按验证准确率保存最优 checkpoint」。本讲的 `self.log` 与 `ModelCheckpoint` 就是这两件事的框架化版本。
- **train/eval 双模式**（u2-l4）：`model.train()` / `model.eval()` 翻转 `self.training` 标志，Dropout 与 BatchNorm 的行为随之改变。Lightning 在训练/验证/测试阶段会自动切换模式。
- **torchmetrics 的两种调用语义**（u7-l1）：`update()` 只累计、`forward()` 额外返回当前 batch 的指标值，两者最终 `compute()` 结果一致。Lightning 与 torchmetrics 是姊妹项目，配合极深。
- **验证集划分**（u4-l1）：`helper_data.get_dataloaders_mnist` 用两个索引区间加 `SubsetRandomSampler` 切验证集；本讲会看到另一种做法——`random_split` 直接把 `Dataset` 对象切开。
- **交叉熵只吃 logits**（u2-l2）：`F.cross_entropy` 内部做数值稳定的 `log_softmax`，喂概率是错的。本讲会看到一份 notebook 恰好踩在这条边界上，正好用来练批判性读码。

术语预热：

| 术语 | 一句话解释 |
|---|---|
| `LightningModule` | `nn.Module` 的超集：既装模型，也装「这一步怎么训」的逻辑 |
| `LightningDataModule` | 把下载、划分、三个 DataLoader 打包成一个类的数据组件 |
| `Trainer` | 框架的「发动机」：跑循环、搬设备、写日志、触发回调 |
| `self.log` | 把标量/指标对象写入 logger 与进度条的统一出口 |
| callback | 训练过程中被 `Trainer` 周期性调用的插件，如 `ModelCheckpoint` |
| checkpoint（ckpt） | 保存权重、超参数与训练状态的 `.ckpt` 文件，可恢复可推理 |

**版本说明（重要）**：仓库三份 notebook 的实际运行环境分别是 `pytorch_lightning 1.5.2`（两份 lightning-mlp）与 `1.5.1`（mlp-basic），均属 Lightning 1.x。若你在新环境（Lightning 2.x）复现，若干 API 名称已变（后文逐处标注），这也是本讲「版本意识」训练的一部分。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [pytorch_ipynb/lightning/lightning-mlp.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/lightning/lightning-mlp.ipynb) | Lightning 入门导论：单类混合式 `LightningModule` + `MnistDataModule` + `Trainer` + TensorBoard + 断点续训，10 epoch 跑通全流程 |
| [pytorch_ipynb/lightning/lightning-mlp-best-model.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/lightning/lightning-mlp-best-model.ipynb) | 上一份的「最优 checkpoint」变体：`ModelCheckpoint` 回调 + `ckpt_path='best'`，用 300 epoch 实验证明 best ≠ last |
| [pytorch-lightning_ipynb/mlp/mlp-basic.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/mlp-basic.ipynb) | Lightning 专库里的工程化写法：纯 `nn.Module`（PyTorchModel）与训练逻辑（LightningModel）分离、`save_hyperparameters`、CSVLogger、`load_from_checkpoint` 推理 |

三份 notebook 的关系：前两份是同一份代码的受控对比（模型与 DataModule 两个 cell 完全相同，只差 checkpoint 相关几行——md5 可验证）；第三份则是同一任务的「工程版重写」，用来对照两种代码组织风格。另外注意：mlp-basic 中的代码 cell 首行有 `# %load ../code_lightningmodule/xxx.py` 魔法命令痕迹，但仓库中该目录已不存在，实际内容以 cell 内保存的代码为准（这本身就是「以代码为准」的一课）。

## 4. 核心概念与源码讲解

### 4.1 LightningModule：把「模型 + 训练逻辑」装进一个类

#### 4.1.1 概念说明

手写训练循环（u2-l2 到 u7 全部 notebook）时，你需要自己维护：epoch 双层循环、把数据搬到 GPU、`model.train()`/`eval()` 切换、`no_grad` 评估、损失记录、最优权重保存。这些代码每个 notebook 都长得很像——**重复正是框架的入场券**。

LightningModule 的设计是：你继承 `pl.LightningModule`（它本身就是 `nn.Module` 的子类），只回答三个问题——

1. **模型长什么样**（`__init__` 与 `forward`，和普通 `nn.Module` 一样）；
2. **每个 batch 的一步怎么做**（`training_step` / `validation_step` / `test_step`，返回损失即可，**不要**自己写 backward/step）；
3. **用什么优化器**（`configure_optimizers`）。

其余循环、设备搬运、模式切换全部由 `Trainer` 接管。对应关系表：

| 手写循环里的代码 | Lightning 里的归宿 |
|---|---|
| `for epoch: for batch` | `trainer.fit()` 内部 |
| `optimizer.zero_grad(); loss.backward(); optimizer.step()` | `training_step` 返回 loss 后，框架自动执行 |
| `model.train()` / `model.eval()` + `torch.no_grad()` | 框架在训练/验证/测试阶段自动切换 |
| `log_dict` 手工记录 | `self.log(...)` 一行 |
| 按 `valid_acc` 手工存最优权重 | `ModelCheckpoint` 回调（见 4.4） |
| `torch.cuda.is_available()` 选设备 | `Trainer` 的 `gpus`/`accelerator` 参数 |

#### 4.1.2 核心流程

`trainer.fit(model, datamodule)` 的一次训练可以概括为（伪代码）：

```text
调用 datamodule.prepare_data()          # 下载，仅一次
调用 datamodule.setup('fit')            # 构造 train/valid 数据集
for epoch in range(max_epochs):
    model.train()                        # 框架自动
    for batch in train_dataloader:
        loss = model.training_step(batch, batch_idx)   # 你写的一步
        optimizer.zero_grad(); loss.backward(); optimizer.step()
        # training_step 里的 self.log 被收集
    model.eval()                         # 框架自动
    with no_grad():
        for batch in val_dataloader:
            model.validation_step(batch, batch_idx)
    # 触发回调：ModelCheckpoint 按 monitor 指标决定是否存盘
    # 触发 *_epoch_end 钩子（1.x）
```

`trainer.test()` 则只跑 `test_step`；`trainer.predict()` 只跑 `forward`。

#### 4.1.3 源码精读

**（a）入门版：模型与训练逻辑一体**

先看 lightning-mlp.ipynb 的导入与类定义骨架。导入部分引入 `pytorch_lightning`、`torch`、`nn` 与 torchmetrics 的 `Accuracy`：[pytorch_ipynb/lightning/lightning-mlp.ipynb:L63-L68](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/lightning/lightning-mlp.ipynb#L63-L68)（这段代码导入 Lightning、torch 与 Accuracy 指标类，是全 notebook 仅有的依赖）。

完整的 `MultiLayerPerceptron` 类在这里：[pytorch_ipynb/lightning/lightning-mlp.ipynb:L78-L142](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/lightning/lightning-mlp.ipynb#L78-L142)（定义了一个 28×28→32→16→10 的 MLP 并附带全部训练钩子）。逐段看关键部分：

`__init__` 前半部分是 Lightning 新增内容——三个 Accuracy 指标实例：[pytorch_ipynb/lightning/lightning-mlp.ipynb:L79-L85](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/lightning/lightning-mlp.ipynb#L79-L85)

```python
def __init__(self, image_shape=(1, 28, 28), hidden_units=(32, 16)):
    super().__init__()
    # new PL attributes:
    self.train_acc = Accuracy()
    self.valid_acc = Accuracy()
    self.test_acc = Accuracy()
```

这行代码为训练/验证/测试各建一个独立的 torchmetrics 指标对象（分开建是为了互不污染累计值；新版 torchmetrics 需要 `Accuracy(task="multiclass", num_classes=10)` 的写法）。

`__init__` 后半部分与 u2-l3 的普通 MLP 无异——`nn.Flatten` + 循环搭 `Linear`+`ReLU` + 输出层：[pytorch_ipynb/lightning/lightning-mlp.ipynb:L87-L98](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/lightning/lightning-mlp.ipynb#L87-L98)（这段代码用 Sequential 堆出 784→32→16→10 的网络，注意末端多了一个 `nn.Softmax(dim=1)`——后面会专门讨论它）。

`training_step` 是本类的心脏：[pytorch_ipynb/lightning/lightning-mlp.ipynb:L104-L111](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/lightning/lightning-mlp.ipynb#L104-L111)

```python
def training_step(self, batch, batch_idx):
    x, y = batch
    logits = self(x)
    loss = nn.functional.cross_entropy(self(x), y)
    preds = torch.argmax(logits, dim=1)
    self.train_acc.update(preds, y)
    self.log("train_loss", loss, prog_bar=True)
    return loss
```

这段代码只做「一步」：解包 batch、前向、算损失、累计指标、记日志、**返回损失**。没有 `zero_grad`/`backward`/`step`——框架拿到返回值后自动执行三件套。两个值得注意的读码细节：

1. `self(x)` 被调用了两次（`logits = self(x)` 与损失行内各一次），属于冗余前向，功能无碍但多算一遍。
2. 模型末端已有 `Softmax`，而 `cross_entropy` 期望的输入是 **logits**（u2-l2 的结论）。把 softmax 概率喂进去，等价于对概率再做一次 `log_softmax`，严格来说不是标准的交叉熵——但训练仍能收敛（作者环境 10 epoch 后测试准确率 93.90%），且 `argmax` 在 softmax 前后不变、指标不受影响。这是教学代码的一处不严谨，工程版（下面的 mlp-basic）已改回「模型返回 logits」的标准写法。对照 u3-l5、u7-l4 的案例，**读 notebook 也要带批判眼光**这条方法论第三次应验。

1.x 版本用 `*_epoch_end` 钩子在 epoch 末结算指标：[pytorch_ipynb/lightning/lightning-mlp.ipynb:L113-L115](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/lightning/lightning-mlp.ipynb#L113-L115)（这段代码在训练 epoch 结束时 `compute()` 出全 epoch 准确率、写入日志并 `reset()` 指标，为下个 epoch 清零——正是 u7-l1 讲过的累计-结算节奏）。`validation_step`、`validation_epoch_end` 结构完全对称：[pytorch_ipynb/lightning/lightning-mlp.ipynb:L117-L128](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/lightning/lightning-mlp.ipynb#L117-L128)，`test_step` 略有不同——直接在步内 `compute()` 并同时记录损失与准确率：[pytorch_ipynb/lightning/lightning-mlp.ipynb:L130-L138](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/lightning/lightning-mlp.ipynb#L130-L138)。最后 `configure_optimizers` 返回一个 Adam：[pytorch_ipynb/lightning/lightning-mlp.ipynb:L140-L142](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/lightning/lightning-mlp.ipynb#L140-L142)（这段代码把「优化器选型」也从 notebook 主流程挪进了类里，lr=0.001）。

**（b）工程版：模型与训练逻辑分离**

mlp-basic.ipynb 把同一个 MLP 拆成两个类。`PyTorchModel` 是纯 `nn.Module`，直接返回 logits：[pytorch-lightning_ipynb/mlp/mlp-basic.ipynb:L234-L256](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/mlp-basic.ipynb#L234-L256)（这段代码定义 784→128→256→10 的 MLP——隐藏层比入门版宽得多，`forward` 里 `flatten` 后过层，注释明确写着返回值是 logits）。

`LightningModel` 则是「训练逻辑包装器」，接收任意模型与学习率：[pytorch-lightning_ipynb/mlp/mlp-basic.ipynb:L300-L369](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/mlp-basic.ipynb#L300-L369)（这段代码用装饰器模式把任意 `nn.Module` 升级成可训练的 LightningModule）。三个进阶点：

1. **`save_hyperparameters`**：[pytorch-lightning_ipynb/mlp/mlp-basic.ipynb:L309-L312](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/mlp-basic.ipynb#L309-L312)（这段代码把 `__init__` 的参数（此处即 `learning_rate`，`ignore=["model"]` 跳过模型对象）自动存进 checkpoint 旁的 `hparams.yaml`，之后 `load_from_checkpoint` 才能无参重建对象）。
2. **`_shared_step` 复用**：[pytorch-lightning_ipynb/mlp/mlp-basic.ipynb:L326-L332](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/mlp-basic.ipynb#L326-L332)（这段代码把「前向 → 交叉熵 → argmax」抽出为训练/验证/测试共用的一步，消除三份重复代码——入门版三份 step 几乎逐行相同的问题就此解决）。
3. **log 指标对象本身**：[pytorch-lightning_ipynb/mlp/mlp-basic.ipynb:L350-L360](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/mlp-basic.ipynb#L350-L360)（这段代码在 `validation_step` 里直接 `self.log("valid_acc", self.valid_acc, on_epoch=True, ...)`——传入的是 metric **对象**而非数值，Lightning 会在 epoch 末自动 `compute()` 并 `reset()`，因此**不再需要** `*_epoch_end` 钩子；`on_step=False, on_epoch=True` 表示只记 epoch 级聚合值）。

它的 `training_step` 还有一个精细处理：[pytorch-lightning_ipynb/mlp/mlp-basic.ipynb:L334-L348](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/mlp-basic.ipynb#L334-L348)（这段代码在训练步内额外做一次 `model.eval()` + `no_grad` 的前向来计算**训练**准确率，以排除 Dropout/BatchNorm 在训练模式下的行为干扰，算完再切回 `model.train()`——把 u2-l4 讲的模式切换知识用在了指标口径上）。

两种风格对照总结：

| 维度 | 入门版（lightning-mlp） | 工程版（mlp-basic） |
|---|---|---|
| 类结构 | 一个类包揽模型+训练 | PyTorchModel + LightningModel 分离 |
| 输出层 | Softmax 进模型，损失吃概率（不严谨） | 模型出 logits，损失吃 logits（标准） |
| 三步复用 | 三份近似复制的 step | `_shared_step` 一份 |
| 指标聚合 | `*_epoch_end` 手动 compute/reset（1.x 专属，2.0 已移除） | log metric 对象 + `on_epoch=True` |
| 超参持久化 | 无 | `save_hyperparameters` |
| 测试集准确率 | 93.90%（32-16，10 epoch） | 97.32%（128-256，10 epoch） |

#### 4.1.4 代码实践

**实践目标**：亲眼确认 `training_step` 的调用节奏 = 「每 epoch 每 batch 一次」，并体会框架自动做了哪些事。

**操作步骤**：

1. 打开 lightning-mlp.ipynb，在 `training_step` 的第一行临时加一行（示例代码）：

   ```python
   def training_step(self, batch, batch_idx):
       print(f"train step {batch_idx}, x.shape={batch[0].shape}")  # 临时观测
       ...
   ```

2. 把 `max_epochs` 临时改成 1，运行 4.3 节的训练 cell。
3. 数一下打印行数，并与 `len(trainer.datamodule.train_dataloader())` 对比。
4. 观察完后删掉 print，恢复 `max_epochs=10`。

**需要观察的现象**：每个 batch 打一行，`x.shape` 为 `[64, 1, 28, 28]`（DataModule 的 batch_size=64）；打印总次数 ≈ 860。

**预期结果**：训练集 55000 个样本、batch 64、未设 `drop_last`，故每 epoch 批数为 ⌈55000/64⌉ = 860。一个旁证：4.3 节续训 cell 里的 checkpoint 文件名是 `epoch=9-step=8599.ckpt`——10 个 epoch 共 8600 步，文件名记录的是最后一步的 0 基序号 8599，与 860 批/epoch 自洽。你的环境若 MNIST 下载完整、batch_size 未改，打印行数应为 860。待本地验证（不同 Lightning 版本对进度打印的缓冲方式不同，行数一致即可）。

#### 4.1.5 小练习与答案

**练习 1**：`training_step` 里为什么不能自己写 `loss.backward()` 和 `optimizer.step()`？

**参考答案**：因为框架在拿到 `training_step` 返回的 loss 后会自动执行 `zero_grad → backward → step`（见 4.1.2 的伪代码）。自己再写一遍会造成同一批梯度被反向传播两次、参数被更新两次（甚至梯度累加），破坏训练语义。Lightning 的契约就是「你返回 loss，我来优化」。

**练习 2**：入门版模型末端有 `nn.Softmax(dim=1)`，为什么最终 `test_acc` 仍然可信？

**参考答案**：softmax 是逐行单调变换，不改变每行最大值的位置，因此 `torch.argmax(softmax(logits)) == torch.argmax(logits)`，预测标签与标准写法完全一致；受影响的只是损失数值的口径（等于对概率再做一次 `log_softmax`+NLL），不影响准确率的计算。

**练习 3**：mlp-basic 的 `training_step` 里为什么要专门 `self.model.eval()` 再前向一次算训练准确率，而不是直接用训练前向的预测？

**参考答案**：训练前向发生在 `train()` 模式下，Dropout 随机失活、BatchNorm 用批统计，得到的预测是「带噪声版模型」的输出；用 `eval()` + `no_grad()` 再前向一次，得到的是与验证/测试同口径的预测，三种准确率才可比。这正是 u2-l4 双模式知识的落地。

### 4.2 DataModule：把数据管线封装成可复用组件

#### 4.2.1 概念说明

`LightningDataModule` 把「数据从哪来、怎么切、怎么装进 DataLoader」打包成一个类，让模型代码与数据代码彻底解耦——换数据集只需换 DataModule，换模型只需换 LightningModule。它有六个可覆写方法，按职责分两组：

- **数据获取**：`prepare_data()`（只做下载等一次性动作，**每个进程只调用一次**，只应在单进程执行——避免多卡重复下载）与 `setup(stage)`（构造 `Dataset` 对象，`stage` 为 `'fit'`/`'validate'`/`'test'`/`'predict'`，分布式下**每个进程**都会调用）。
- **供给接口**：`train_dataloader()` / `val_dataloader()` / `test_dataloader()`（可选 `predict_dataloader()`），`Trainer` 在相应阶段来取。

与 u4-l1 的 `helper_data.get_dataloaders_mnist` 对照：函数式方案用 `SubsetRandomSampler` 在**同一份** `Dataset` 上按索引区间切出训练/验证子集，好处是训练子集可挂随机增强 transform、验证子集挂确定性 transform；本讲两份 notebook 用的是 `random_split`，直接把 `Dataset` 切成两个独立的 `Subset` 对象——更直观，但两个子集共享同一个 transform（这里只有 `ToTensor`，无随机增强，所以没有区别）。两种方案都正确，选型取决于是否需要差异化 transform（u4-l3 的双 transform 模式）。

#### 4.2.2 核心流程

`trainer.fit(model, datamodule=dm)` 触发数据侧的时序：

```text
prepare_data()                 # 进程级一次：下载 MNIST 到 data_path
setup('fit')                   # 建 train(55000) + val(5000)
循环中按需调用 train_dataloader() / val_dataloader()
trainer.test(datamodule=dm) 时 → setup('test') 不再调用（已 setup），
                                  取 test_dataloader()
```

#### 4.2.3 源码精读

入门版的 `MnistDataModule` 全文在这里：[pytorch_ipynb/lightning/lightning-mlp.ipynb:L174-L211](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/lightning/lightning-mlp.ipynb#L174-L211)（这段代码完成下载、55000/5000 划分并暴露三个 DataLoader）。四个要点：

1. `prepare_data` 只下载：[pytorch_ipynb/lightning/lightning-mlp.ipynb:L180-L181](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/lightning/lightning-mlp.ipynb#L180-L181)（这段代码用 `download=True` 触发一次下载；后续 `setup` 里全部 `download=False`，复用本地缓存）。
2. `setup` 里训练数据切两半：[pytorch_ipynb/lightning/lightning-mlp.ipynb:L183-L202](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/lightning/lightning-mlp.ipynb#L183-L202)（这段代码先加载完整 6 万训练集，再 `random_split` 出 55000/5000，`generator=torch.Generator().manual_seed(1)` 固定划分种子；测试集另取 `train=False` 的一万张）。注意 `transform` 在 `__init__` 里定义为 `Compose([ToTensor()])`，train/val/test 三者共用。
3. 三个 dataloader 方法：[pytorch_ipynb/lightning/lightning-mlp.ipynb:L204-L211](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/lightning/lightning-mlp.ipynb#L204-L211)（这段代码返回 batch_size=64、num_workers=4 的三个 loader；都没有显式设置 `shuffle`/`drop_last`，属最简写法）。
4. 实例化前固定全局种子：[pytorch_ipynb/lightning/lightning-mlp.ipynb:L214-L215](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/lightning/lightning-mlp.ipynb#L214-L215)（这段代码 `torch.manual_seed(1)` 后创建 `mnist_dm`——u4-l4 讲过的「建模型前先固定种子」同样适用于建 DataModule）。

工程版 `DataModule` 更规范：[pytorch-lightning_ipynb/mlp/mlp-basic.ipynb:L718-L774](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/mlp-basic.ipynb#L718-L774)（这段代码把 batch_size 等改为引用全局常量，并给三个 loader 明确设置 shuffle 与 drop_last）。其中 `train_dataloader` 是最佳实践写法：[pytorch-lightning_ipynb/mlp/mlp-basic.ipynb:L746-L755](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/mlp-basic.ipynb#L746-L755)（这段代码对训练 loader 设 `drop_last=True, shuffle=True`，对验证/测试 loader 设 `drop_last=False, shuffle=False`——正是 u2-l2 讲过的规则；`BATCH_SIZE=256`、`NUM_WORKERS=4` 定义在超参 cell：[pytorch-lightning_ipynb/mlp/mlp-basic.ipynb:L161-L165](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/mlp-basic.ipynb#L161-L165)）。工程版实例化时传入自定义数据目录：[pytorch-lightning_ipynb/mlp/mlp-basic.ipynb:L821-L822](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/mlp-basic.ipynb#L821-L822)（这段代码固定种子后 `DataModule(data_path='./data')`——路径可配置化让组件可复用）。

#### 4.2.4 代码实践

**实践目标**：验证 DataModule 的三个 loader 规模符合 55000/5000/10000 的划分。

**操作步骤**（示例代码，可在 notebook 末尾新建 cell 运行）：

```python
dm = MnistDataModule()
dm.prepare_data()
dm.setup('fit')

for name in ['train', 'val', 'test']:
    loader = {'train': dm.train_dataloader, 'val': dm.val_dataloader,
              'test': dm.test_dataloader}[name]()
    n_batches = len(loader)
    ds = loader.dataset
    print(f"{name}: 样本数={len(ds)}, 批数={n_batches}, "
          f"batch_size={loader.batch_size}")
```

**需要观察的现象**：三个 loader 的样本数分别为 55000、5000、10000；train 批数为 ⌈55000/64⌉=860，val 为 ⌈5000/64⌉=79，test 为 ⌈10000/64⌉=157。

**预期结果**：如上数字吻合（入门版）。若在工程版上做同样实验，batch_size=256 且 train 设 `drop_last=True`，train 批数应为 ⌊55000/256⌋=214。待本地验证（`len(loader)` 是批数而非样本数——u4-l1 的结论在此复用）。

#### 4.2.5 小练习与答案

**练习 1**：`prepare_data` 和 `setup` 都能加载数据，为什么拆成两个方法？

**参考答案**：职责与调用时机不同。`prepare_data` 在每个**进程**只调用一次且只在主进程执行，适合下载、解压这类只需做一次的动作；`setup` 会在每个进程（多卡时每张卡的进程）被调用、且可能按 `stage` 多次调用，适合构造 `Dataset`、做划分。若把下载写进 `setup`，多卡训练时多个进程会同时抢写同一份数据。

**练习 2**：本讲的 `random_split` 与 u4-l1 `helper_data` 的 `SubsetRandomSampler` 方案相比，牺牲了什么能力？

**参考答案**：`random_split` 切出的是两个独立 `Subset`，各自绑定同一个 `transform`；因此无法给训练子集挂随机增强、给验证子集挂确定性变换（u4-l3 的双 transform 模式）。`SubsetRandomSampler` 方案里训练/验证两个 loader 共享同一 `Dataset` 实例但可分别定义 transform，天然支持差异化。本讲数据只用 `ToTensor`，无随机增强，故无差别。

**练习 3**：入门版三个 dataloader 都没有写 `shuffle=True`，训练为什么不崩？

**参考答案**：`torch.utils.data.DataLoader` 的默认值是 `shuffle=False`，但 `random_split` 返回的 `Subset` 包装的 MNIST 内部按标签大致有序，不 shuffle 意味着每个 epoch 都以相同（近似有序）顺序喂 batch——训练**能跑但不理想**（梯度估计变差、可能被类别顺序带偏）。工程版显式 `shuffle=True` 修正了这一点。这是一个「能跑 ≠ 对」的例子，也是两份 notebook 的真实差异之一。

### 4.3 Trainer 与日志：一键训练、TensorBoard 与断点续训

#### 4.3.1 概念说明

`Trainer` 是 Lightning 的发动机：你把 `LightningModule` 与 `DataModule` 交给它，它负责组装循环、搬运设备、切换模式、收集 `self.log` 的数值写入 logger、在恰当时机触发 callback。常用入口三个：`fit()`（训练+验证）、`test()`（测试）、`predict()`（推理）。

日志系统是「可插拔」的：不给 `logger` 参数时默认用 `TensorBoardLogger`，把事件写到 `lightning_logs/version_N/`（N 自动递增，每次 `fit` 一个新版本目录）；换成 `CSVLogger` 则把指标写成 csv 文件，适合用 pandas 直接画图。checkpoint 也默认开启——即使你不配置任何回调，Trainer 也会在 `lightning_logs/version_N/checkpoints/` 存下最后一个 epoch 的权重（4.4 节会看到文件名）。

#### 4.3.2 核心流程

```text
构造 Trainer(max_epochs=..., callbacks=[...], logger=...)
trainer.fit(model, datamodule=dm)
    → 产出 lightning_logs/version_N/（事件文件 + checkpoints/ + hparams.yaml）
    → 进度条上显示 self.log(prog_bar=True) 的指标
trainer.test(model, datamodule=dm)          # 默认用当前（最后）权重
trainer.test(model, datamodule=dm, ckpt_path='best')   # 用最优权重（4.4）
```

#### 4.3.3 源码精读

**（a）最小 Trainer 与默认 TensorBoard**

入门版的训练 cell 只有六行：[pytorch_ipynb/lightning/lightning-mlp.ipynb:L423-L430](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/lightning/lightning-mlp.ipynb#L423-L430)

```python
mnistclassifier = MultiLayerPerceptron()

if torch.cuda.is_available(): # if you have GPUs
    trainer = pl.Trainer(max_epochs=10, gpus=1)
else:
    trainer = pl.Trainer(max_epochs=10)

trainer.fit(model=mnistclassifier, datamodule=mnist_dm)
```

这段代码把 u1-l2 讲过的「device 自动选择」上移到 Trainer 参数：有 GPU 则 `gpus=1`（1.x 写法；2.x 应为 `accelerator="gpu", devices=1`）。整个训练过程——外层 epoch 循环、内层 batch 循环、backward、验证、日志——压缩成一次 `trainer.fit()` 调用。运行后测试一次：[pytorch_ipynb/lightning/lightning-mlp.ipynb:L483](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/lightning/lightning-mlp.ipynb#L483)（这行代码用 `trainer.test` 在测试集上跑 `test_step`，作者环境输出 `test_acc 0.9390`）。

TensorBoard 通过 notebook 魔法命令内嵌查看：[pytorch_ipynb/lightning/lightning-mlp.ipynb:L552-L554](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/lightning/lightning-mlp.ipynb#L552-L554)（这段代码加载 tensorboard 扩展并指向 `lightning_logs/` 目录，notebook 内即可交互式查看 train_loss/valid_acc 曲线——注意此时已存在两个 version 目录，续训那次 fit 是 version_1）。

**（b）断点续训：从 checkpoint 继续训练**

入门版最后演示了「接着练」：[pytorch_ipynb/lightning/lightning-mlp.ipynb:L702-L707](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/lightning/lightning-mlp.ipynb#L702-L707)

```python
if torch.cuda.is_available():
    trainer = pl.Trainer(max_epochs=15, resume_from_checkpoint='./lightning_logs/version_0/checkpoints/epoch=9-step=8599.ckpt', gpus=1)
else:
    trainer = pl.Trainer(max_epochs=15, resume_from_checkpoint='./lightning_logs/version_0/checkpoints/epoch=9-step=8599.ckpt')

trainer.fit(model=mnistclassifier, datamodule=mnist_dm)
```

这段代码把 `max_epochs` 从 10 提到 15，并指向第一次训练存的 checkpoint。三个读码要点：

1. `max_epochs=15` 是**绝对总轮数**而非「再练 5 轮」——恢复后从第 10 轮继续到第 15 轮。
2. `resume_from_checkpoint` 恢复的不只是权重，还有优化器状态与 epoch 计数（Adam 的动量等，u7-l2 讲过为何这很重要）。
3. checkpoint 路径里的 `epoch=9-step=8599` 与 4.1.4 的批数推算自洽。另外作者环境实际输出过弃用警告：1.5 起推荐改用 `trainer.fit(model, datamodule=dm, ckpt_path=...)`；到 2.x `resume_from_checkpoint` 参数已被移除。**用新版复现时按警告改写即可**。

**（c）工程版 Trainer：显式配置一切**

mlp-basic 把 Trainer 配置写成表驱动风格：[pytorch-lightning_ipynb/mlp/mlp-basic.ipynb:L1181-L1196](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/mlp-basic.ipynb#L1181-L1196)

```python
trainer = pl.Trainer(
    max_epochs=NUM_EPOCHS,
    callbacks=callbacks,
    progress_bar_refresh_rate=50,  # recommended for notebooks
    accelerator="auto",  # Uses GPUs or TPUs if available
    devices="auto",  # Uses all available GPUs/TPUs if applicable
    logger=logger,
    deterministic=True,
    log_every_n_steps=10,
)

start_time = time.time()
trainer.fit(model=lightning_model, datamodule=data_module)
```

这段代码展示了四个进阶配置：`accelerator="auto"`/`devices="auto"`（新一代设备 API，自动发现硬件——与入门版的 `gpus=1` 新旧对照）；`deterministic=True`（对应 u4-l4 的 `set_deterministic`，用性能换逐位复现）；`log_every_n_steps=10`（中途日志的采样密度）；`logger=logger` 接入自定义日志器。其中 logger 与回调在这里组装：[pytorch-lightning_ipynb/mlp/mlp-basic.ipynb:L920-L932](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/mlp-basic.ipynb#L920-L932)（这段代码创建 `LightningModel`、配置 `ModelCheckpoint` 回调并把 logger 换成 `CSVLogger(save_dir="logs/", name="my-model")`——指标会写进 `logs/my-model/version_N/metrics.csv`，notebook 后半段正是用 pandas 读取它画训练/验证准确率曲线）。模型本体在回调之前实例化：[pytorch-lightning_ipynb/mlp/mlp-basic.ipynb:L878-L882](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/mlp-basic.ipynb#L878-L882)（这行代码把 784/超参元组/10 传给 PyTorchModel，随后包进 LightningModel——「纯模型」与「训练逻辑」在此汇合）。

#### 4.3.4 代码实践

**实践目标**：完整复现「训练 10 轮 → 续训到 15 轮」并观察日志目录的版本递增。

**操作步骤**：

1. 运行 4.3.3(a) 的训练 cell（10 epoch）。
2. 查看 `lightning_logs/` 目录，确认出现 `version_0/`，内含 `checkpoints/` 与事件文件。
3. 运行 4.3.3(b) 的续训 cell（15 epoch）。
4. 再看 `lightning_logs/`——确认新增 `version_1/`。
5. 运行 TensorBoard cell，在界面左侧勾选两个 version 对比曲线。

**需要观察的现象**：续训日志显示从 epoch 10 附近恢复而非从 0 开始；TensorBoard 中 version_1 的 valid_acc 曲线起点衔接 version_0 的终点附近。

**预期结果**：如上两条。若你用 Lightning 2.x，`resume_from_checkpoint` 会直接报错，按 4.3.3(b) 的说明改成 `trainer.fit(..., ckpt_path='./lightning_logs/version_0/checkpoints/...')` 即可（目录里的 ckpt 文件名可能因版本而略有差异，以实际文件为准）。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`max_epochs=15` 配合 `resume_from_checkpoint`（此前练了 10 轮），实际会再练几轮？

**参考答案**：5 轮。`max_epochs` 是整个训练过程的总轮数上限，checkpoint 恢复了 epoch 计数（从 10 继续），所以到 15 为止再练 5 轮。若误以为 15 是「额外轮数」会得到 25 轮。

**练习 2**：默认 TensorBoardLogger 与 CSVLogger 各自适合什么场景？

**参考答案**：TensorBoardLogger 是默认值，写到 `lightning_logs/version_N/`，适合交互式查看曲线、对比标量（本讲入门版用）；CSVLogger 把指标落成 csv，适合后续用 pandas/matplotlib 做自定义统计图表或纳入自动化分析（工程版用）。两者都只是 `self.log` 的下游，模型代码不用改一行。

**练习 3**：为什么工程版 Trainer 要显式传 `deterministic=True`，而入门版没有？

**参考答案**：入门版面向教学、追求最少配置；工程版面向可复现实验。`deterministic=True` 让框架挑选确定性算法实现（u4-l4 讲过 `torch.use_deterministic_algorithms` 一族开关），保证同种子同结果，代价是可能变慢、个别算子不受支持。做对照实验（u2-l4 的受控对比方法论）时这一项几乎是必需品。

### 4.4 最优 checkpoint：ModelCheckpoint 与 `ckpt_path='best'`

#### 4.4.1 概念说明

u4-l4 的 `train_classifier_simple_v2` 手工实现了「验证准确率创新高就存权重」。Lightning 把它标准化为 `ModelCheckpoint` 回调，三个核心参数：

- `monitor`：盯哪个指标（须与 `self.log` 的名字一致，如 `"valid_acc"`）；
- `mode`：`'max'`（越大越好）或 `'min'`；
- `save_top_k`：保留最优的 k 份（k=1 即只留冠军）。

关键概念是 **best 与 last 的区别**：训练结束时模型处于「最后一轮」的状态，但验证指标最优的可能出现在中途（过拟合发生后尤其如此）。因此严肃的评估（u6-l5 的实验纪律）应加载 **best** 权重，而非顺手用 last。Lightning 提供两种取法：`trainer.test(..., ckpt_path='best')` 一步到位；或 `trainer.checkpoint_callback.best_model_path` 拿到路径后 `load_from_checkpoint` 重建模型做推理。

#### 4.4.2 核心流程

```text
每个 validation epoch 结束：
    ModelCheckpoint 读取 monitor 指标
    若优于历史最优 → 保存 ckpt（权重 + hparams + 训练状态）到
       lightning_logs/version_N/checkpoints/epoch=A-step=B.ckpt
trainer.test(ckpt_path='best')   → 自动定位最优那份并加载后测试
推理期：
    path = trainer.checkpoint_callback.best_model_path
    model = LightningModel.load_from_checkpoint(path, model=pytorch_model)
```

#### 4.4.3 源码精读

best-model notebook 与入门版的**全部差异**只有一个 cell——先配置回调：[pytorch_ipynb/lightning/lightning-mlp-best-model.ipynb:L241-L243](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/lightning/lightning-mlp-best-model.ipynb#L241-L243)（这段代码创建 `ModelCheckpoint(save_top_k=1, mode='max', monitor="valid_acc")`——盯验证准确率、越大越好、只留一份），再把回调挂上 Trainer 训练 300 轮：[pytorch_ipynb/lightning/lightning-mlp-best-model.ipynb:L4504-L4510](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/lightning/lightning-mlp-best-model.ipynb#L4504-L4510)（这段代码与入门版唯一的差别就是 `callbacks=callbacks` 与 `max_epochs=300`——一次只改一处的受控对比再次出现；顺带一提，GPU 分支里写的是 `gpu=1` 而非 `gpus=1`，作者环境为 CPU（输出显示 `GPU available: False`），该分支未被执行，GPU 用户复现时需留意这处拼写）。

训练结束后先按默认（last）测一次：[pytorch_ipynb/lightning/lightning-mlp-best-model.ipynb:L4571](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/lightning/lightning-mlp-best-model.ipynb#L4571)（这行代码用第 300 轮的权重测试，作者环境 `test_acc 0.9601`），notebook 的 markdown 随即点破主题——TensorBoard 曲线显示最优模型不在最后一步，于是换 best 再测：[pytorch_ipynb/lightning/lightning-mlp-best-model.ipynb:L4713](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/lightning/lightning-mlp-best-model.ipynb#L4713)（这行代码加 `ckpt_path='best'`，加载验证准确率最高的那份权重再测，作者环境 `test_acc 0.9628`）。**0.9601 对 0.9628：同一个训练过程，评估口径不同差 0.27 个百分点**——300 轮的小模型早已过拟合，best 权重优于 last。

工程版 mlp-basic 走的是「取出路径、重建模型」路线：先测：[pytorch-lightning_ipynb/mlp/mlp-basic.ipynb:L1385](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/mlp-basic.ipynb#L1385)（这行代码 `trainer.test(..., ckpt_path='best')`，作者环境 `test_acc 0.9732`）；推理时取最优路径：[pytorch-lightning_ipynb/mlp/mlp-basic.ipynb:L1447-L1448](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/mlp-basic.ipynb#L1447-L1448)（这段代码从 checkpoint 回调对象读出最优 ckpt 的完整路径并打印——注意是问回调要路径，而非自己猜文件名），然后从 checkpoint 重建：[pytorch-lightning_ipynb/mlp/mlp-basic.ipynb:L1486-L1487](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/mlp-basic.ipynb#L1486-L1487)（这段代码 `LightningModel.load_from_checkpoint(path, model=pytorch_model)` 重建对象并 `eval()`——4.1.3 讲的 `save_hyperparameters` 在这里兑现：`learning_rate` 从 hparams 自动恢复，`model` 因为当时被 `ignore` 排除、所以现在要显式传入；最后在测试 loader 上手工累计 Accuracy 得出相同准确率收尾：[pytorch-lightning_ipynb/mlp/mlp-basic.ipynb:L1610-L1611](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/mlp-basic.ipynb#L1610-L1611)）。

#### 4.4.4 代码实践

**实践目标**：亲手验证「best ≠ last」，并从 best checkpoint 出发继续训练。

**操作步骤**：

1. 打开 lightning-mlp-best-model.ipynb，把 `max_epochs=300` 临时改为 `30`（节省时间；模型与 DataModule cell 无需动）。
2. 训练后先运行不带 `ckpt_path` 的 `trainer.test`，记下 `test_acc`（last）。
3. 再运行带 `ckpt_path='best'` 的 `trainer.test`，记下 `test_acc`（best）。
4. 打印 `trainer.checkpoint_callback.best_model_path`，看最优模型停在第几轮（文件名 `epoch=A-step=B`）。

**需要观察的现象**：两种口径的 `test_acc` 数值不同；best 路径的 `epoch=A` 明显小于 `max_epochs-1`。

**预期结果**：30 轮内小模型（32-16 隐藏层）通常在第 10~25 轮之间达到验证最优，best 的 `test_acc` ≥ last 的（作者环境 300 轮版本为 0.9628 对 0.9601）。若你的曲线仍在单调上升，best 可能等于 last——把轮数调大再试。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`ModelCheckpoint(monitor="valid_acc")` 里的字符串写错了（比如写成 `"validation_accuracy"`），会发生什么？

**参考答案**：回调在每个验证 epoch 末去取名为 `valid_acc` 的日志值，取不到会按「严格模式」直接抛错（`MisconfigurationException`：monitor 键不存在），提示你检查 `self.log` 的名字。它必须与 `self.log("valid_acc", ...)` 完全一致——这是一条字符串契约。

**练习 2**：`load_from_checkpoint(path, model=pytorch_model)` 里为什么还要传 `model`？checkpoint 里没有模型吗？

**参考答案**：checkpoint 里存的是模型的**权重与超参数**，但重建对象需要先有一个同结构的壳。`save_hyperparameters(ignore=["model"])` 当时刻意没把模型对象存进 hparams，因此重建时须显式传入一个 `PyTorchModel` 实例（这里复用了已实例化的 `pytorch_model`），权重随后从 ckpt 加载进它。若当时没有 ignore，Lightning 也能从 hparams 反序列化模型，但把大对象塞进 yaml 既臃肿又易出兼容问题。

**练习 3**：既然 `ckpt_path='best'` 一步就能测最优模型，为什么工程版还要演示 `best_model_path` + `load_from_checkpoint`？

**参考答案**：`trainer.test` 只服务评估；而生产/推理场景需要的是一个**独立于 Trainer 存在的模型对象**——部署时没有 trainer、没有训练数据，只有一份 ckpt 文件。`best_model_path` 给你文件的落点（也是断点续训、模型分享的输入），`load_from_checkpoint` 教你从文件 standalone 重建模型。两者是「评估便利」与「推理必需」的关系。

## 5. 综合实践

**任务**：把 u2-l3 / u4-l4 中手写的 MNIST MLP 训练循环，完整迁移为 Lightning 版本：`LightningModule + DataModule + Trainer`，训练后加载**最优** checkpoint 再继续训练 5 个 epoch，全程用日志曲线监控。这是本讲规格指定的代码实践任务。

**操作步骤**：

1. **准备**：完整跑通 lightning-mlp-best-model.ipynb（可先把 300 轮改小），确认环境里 `pytorch_lightning`、`torchmetrics` 可用（版本注意事项见 4.1.3 与 4.3.3）。
2. **迁移模型**（示例代码——以仓库代码为蓝本改写）：

   ```python
   import pytorch_lightning as pl
   import torch
   import torch.nn as nn
   from torchmetrics import Accuracy

   class MLPLightning(pl.LightningModule):
       def __init__(self, input_size=784, hidden_units=(128, 256), num_classes=10,
                    learning_rate=0.001):
           super().__init__()
           self.save_hyperparameters()
           layers = [nn.Flatten()]
           in_dim = input_size
           for h in hidden_units:
               layers += [nn.Linear(in_dim, h), nn.ReLU()]
               in_dim = h
           layers.append(nn.Linear(in_dim, num_classes))   # 返回 logits，不接 Softmax
           self.model = nn.Sequential(*layers)
           self.train_acc = Accuracy()   # 新版 torchmetrics: Accuracy(task="multiclass", num_classes=10)
           self.valid_acc = Accuracy()

       def forward(self, x):
           return self.model(x)

       def _shared_step(self, batch):
           x, y = batch
           logits = self(x)
           loss = nn.functional.cross_entropy(logits, y)   # 损失吃 logits（对照 4.1.3 的批判点）
           preds = torch.argmax(logits, dim=1)
           return loss, y, preds

       def training_step(self, batch, batch_idx):
           loss, y, preds = self._shared_step(batch)
           self.train_acc(preds, y)
           self.log("train_loss", loss, prog_bar=True)
           self.log("train_acc", self.train_acc, on_epoch=True, on_step=False)
           return loss

       def validation_step(self, batch, batch_idx):
           loss, y, preds = self._shared_step(batch)
           self.valid_acc(preds, y)
           self.log("valid_loss", loss, prog_bar=True)
           self.log("valid_acc", self.valid_acc, on_epoch=True, on_step=False, prog_bar=True)

       def configure_optimizers(self):
           return torch.optim.Adam(self.parameters(), lr=self.hparams.learning_rate)
   ```

   写法上请对照工程版（4.1.3(b)）：模型出 logits、`_shared_step` 复用、log 指标对象、`save_hyperparameters`，四处全部落实。
3. **迁移数据**：直接复用 `MnistDataModule`（4.2.3），或按工程版补上 `shuffle`/`drop_last` 的显式设置。
4. **首轮训练**（示例代码）：

   ```python
   from pytorch_lightning.callbacks import ModelCheckpoint

   dm = MnistDataModule()
   model = MLPLightning()
   callbacks = [ModelCheckpoint(save_top_k=1, mode="max", monitor="valid_acc")]
   trainer = pl.Trainer(max_epochs=10, callbacks=callbacks)   # 2.x 加 accelerator="auto", devices="auto"
   trainer.fit(model=model, datamodule=dm)
   ```
5. **从最优续训 5 轮**（示例代码）：

   ```python
   best_path = trainer.checkpoint_callback.best_model_path
   print("best ckpt:", best_path)

   trainer2 = pl.Trainer(max_epochs=15, callbacks=callbacks)          # 10 + 5
   trainer2.fit(model=model, datamodule=dm, ckpt_path=best_path)      # 1.5+ 写法；
                                                                       # 1.5.2 也可用 resume_from_checkpoint
   trainer2.test(model=model, datamodule=dm, ckpt_path="best")
   ```

**验收清单**（需要逐项确认的现象与结果）：

1. 首轮 `trainer.fit` 后，`lightning_logs/` 出现 version 目录，checkpoints 子目录里只有一份 ckpt（`save_top_k=1`）。
2. `best_model_path` 的文件名 `epoch=A-...` 中 A < 9（best 早于 last）。
3. 续训日志显示从 epoch 10 恢复、总共跑到 15；TensorBoard 中新一轮的 valid_acc 曲线从上次终点附近继续。
4. 最终 `trainer.test(ckpt_path='best')` 的准确率与手写循环版 MLP（u2-l3 约 97.5%、u4-l4 的 helper 工具链版）处于同一量级（97% 上下），因为网络结构（128-256）与数据划分（55000/5000）对齐了。
5. 若数值明显偏低（如 <95%），优先检查三处：损失是否吃的是 logits、`valid_acc` 的 monitor 字符串是否一致、续训时 `max_epochs` 是否写成了绝对总轮数。

以上运行结果与具体准确率**待本地验证**（不同库版本、是否有 GPU 都会影响输出，但清单中的结构性现象应稳定成立）。

## 6. 本讲小结

- Lightning 的分工：你写 `training_step`/`validation_step`/`test_step`（每 batch 一步、返回损失）与 `configure_optimizers`，`Trainer` 接管 epoch 循环、`zero_grad→backward→step` 三件套、设备搬运与 train/eval 模式切换——手写五步循环被「返回 loss」一个动作取代。
- `LightningModule` 两种组织风格：入门版单类混装（三份近似 step + `*_epoch_end` 手动结算指标）；工程版 `PyTorchModel`（纯模型、出 logits）+ `LightningModel`（训练逻辑包装器、`_shared_step` 复用、log 指标对象自动按 epoch 聚合、`save_hyperparameters` 持久化超参）。
- `LightningDataModule` 用 `prepare_data`（一次性下载）/`setup`（建数据集与 55000/5000 划分）/三个 dataloader 方法封装数据管线；与 u4-l1 的 `SubsetRandomSampler` 方案相比少了差异化 transform 的能力，工程版补上了 `shuffle`/`drop_last` 的显式最佳实践。
- `Trainer` 的 `fit/test/predict` 三个入口 + 可插拔日志：默认 TensorBoard（`lightning_logs/version_N/`）或 CSVLogger（csv 落盘配 pandas）；断点续训用 `ckpt_path`（旧写法 `resume_from_checkpoint`），`max_epochs` 是绝对总轮数，恢复内容包括优化器状态。
- `ModelCheckpoint(save_top_k=1, mode='max', monitor='valid_acc')` 自动留最优；评估用 `trainer.test(ckpt_path='best')`，推理用 `best_model_path` + `load_from_checkpoint` 重建。best-model notebook 用 300 轮实验给出 0.9601（last）对 0.9628（best）的实证。
- 版本意识：仓库基于 Lightning 1.5.x，`gpus=1`、`*_epoch_end`、`resume_from_checkpoint` 在 2.x 分别变为 `accelerator/devices`、指标对象 log、`fit(ckpt_path=)`；另有两处批判性读码点——softmax 概率喂 `cross_entropy`、训练 loader 未 shuffle。

## 7. 下一步学习建议

- **下一讲 u8-l2「Lightning 模型库对照与官方模板」**：对照 `pytorch_ipynb` 与 `pytorch-lightning_ipynb` 的同名 cnn-basic notebook 学习两种风格的映射，并研究 `templates/pytorch_lightning/` 下的分类模板与 `tune_classification_basic.py` 批量调参脚本——本讲的 LightningModule/DataModule/Trainer 三件套正是模板的骨架。
- **回看已学**：`pytorch-lightning_ipynb/transformer/distilbert-finetuning-ii.ipynb`（u6-l6 已精读其 PyTorch 版）用 Lightning 手写训练循环微调 DistilBERT，可检验你能否独立读懂它的 `training_step`；`pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb`（u7-l2）展示 DataModule 如何被 K 折循环复用。
- **延伸阅读**：Lightning 官方文档的 LightningModule 与 checkpoints 章节，把本讲的 1.x → 2.x API 对照表补全；`pytorch-lightning_ipynb/data-augmentation/`（u8-l3 的素材）演示在 DataModule 的 transform 里接入 AutoAugment/TrivialAugment。
