# Lightning 模型库对照与官方模板

## 1. 本讲目标

上一讲（u8-l1）我们已经在一份 notebook 里学会了 LightningModule、DataModule、Trainer 与 ModelCheckpoint 的用法。本讲回答三个更「工程化」的问题：

1. **对照迁移**：给定一份纯 PyTorch 版 notebook（`pytorch_ipynb/`），如何机械地把它改写成 Lightning 版（`pytorch-lightning_ipynb/`）？两套风格的每一行代码如何一一映射？
2. **模板化**：作者如何保证几十份 Lightning notebook 结构完全一致？学会使用 `templates/pytorch_lightning/` 的分类模板 notebook，把「填空题」做出来。
3. **脚本化**：如何脱离 Jupyter，用命令行脚本批量启动超参扫描实验？

学完后，你应该能独立把 zoo 中任何一个「尚无 Lightning 版」的模型（例如 ResNet）迁移成 Lightning 版并跑通训练。

## 2. 前置知识

- **u8-l1 的 Lightning 三件套**：`LightningModule`（写每一步的训练逻辑）、`LightningDataModule`（封装数据下载/划分/加载）、`Trainer`（接管 epoch 循环、反向传播、设备搬运）。本讲不重复其内部机制，直接使用。
- **同名对齐约定（u1-l1）**：同一模型在 `pytorch_ipynb/` 与 `pytorch-lightning_ipynb/` 下文件名相同，例如 `cnn/cnn-basic.ipynb` 在两个目录各有一份，是同一模型的两种框架风格实现。README 中徽章数量即版本数。
- **五步训练范式（u2-l1 起）**：forward → loss → zero_grad → backward → step。Lightning 把后四步藏进 `Trainer`，但范式没变。
- **`%load` 魔法命令**：Jupyter 中 `%load path.py` 会把指定文件的内容载入当前 cell（原命令变为 `# %load ...` 注释）。后文会讲到，仓库模板中该命令引用的目录如今已不存在，需要手动处理。
- **argparse**：Python 标准库的命令行参数解析器，`parser.add_argument("--epochs", type=int, default=10)` 定义参数，`args = parser.parse_args()` 解析。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `pytorch_ipynb/cnn/cnn-basic.ipynb` | 纯 PyTorch 版基础 CNN，本讲的「对照物」 |
| `pytorch-lightning_ipynb/cnn/cnn-basic.ipynb` | Lightning 版同名 notebook，对照迁移的目标形态 |
| `pytorch-lightning_ipynb/mlp/template_classification_basic.ipynb` | zoo 目录内的分类模板 notebook（占位符版） |
| `templates/pytorch_lightning/template_classification_basic.ipynb` | 官方模板目录下的同一份模板（与上一份几乎相同） |
| `templates/pytorch_lightning/tune_classification_basic.py` | 命令行训练/调参脚本（模板的 .py 化） |
| `templates/pytorch_lightning/submit_command.sh` | 在 Gradient 云平台批量提交超参网格的命令 |

> 提示：`.ipynb` 是 JSON 文件，本文给出的行号均指该 JSON 源文件中的行号（可在 GitHub 页面或文本编辑器中直接定位）。

## 4. 核心概念与源码讲解

### 4.1 风格对照迁移：同一模型的两套写法

#### 4.1.1 概念说明

仓库里 121 份纯 PyTorch notebook 与 39 份 Lightning notebook 覆盖的模型高度重叠。把前者改写成后者，不需要重写模型——**模型本体保持纯 PyTorch，Lightning 只负责包装**。这是 `pytorch-lightning_ipynb` 所有 notebook 的统一架构决策：

```
纯 PyTorch notebook                    Lightning notebook
─────────────────                      ──────────────────────
ConvNet(nn.Module)          ──保留──▶  PyTorchModel(nn.Module)   ← 模型本体，几乎原样
model.to(device) 手工搬运    ──消失──▶  Trainer(accelerator="auto") 自动搬运
手写 epoch×batch 双层循环    ──消失──▶  LightningModel.training_step + trainer.fit
compute_accuracy 手写函数    ──替换──▶  torchmetrics.Accuracy 指标对象
train/test 两个 loader      ──升级──▶  DataModule 三 loader（含验证集）
优化器与训练循环纠缠         ──解耦──▶  configure_optimizers 单独声明
训练结束测最终权重           ──升级──▶  ModelCheckpoint 按验证准确率存最优
print 打日志                ──替换──▶  CSVLogger + metrics.csv + pandas 画图
```

理解这张映射表后，迁移就变成机械操作：**抄模型、删循环、包 LightningModule、换 DataModule、一行 fit**。

#### 4.1.2 核心流程

以 cnn-basic 为例，迁移的执行顺序：

1. **搬模型**：把纯 PyTorch 版的模型类复制过来，命名为 `PyTorchModel`，`forward` 只返回 logits（不再返回 probas）。
2. **写包装**：定义 `LightningModel(pl.LightningModule)`，把模型对象与学习率作为构造参数传入；实现 `_shared_step`、`training_step`、`validation_step`、`test_step`、`configure_optimizers`。
3. **换数据**：把手写的 `train_loader`/`test_loader` 改写为 `DataModule(pl.LightningDataModule)`，`setup` 里用 `random_split` 切出验证集。
4. **配外设**：`ModelCheckpoint` 回调 + `CSVLogger`。
5. **一键训练**：`trainer = pl.Trainer(...)` → `trainer.fit(model, datamodule=...)` → `trainer.test(..., ckpt_path='best')`。

#### 4.1.3 源码精读

**（a）对照物：纯 PyTorch 版的「一切都要自己管」**

纯 PyTorch 版在同一个 cell 里完成模型定义、设备搬运与优化器构造：device 需要手工选择（作者机器上是 `cuda:3`），模型与优化器绑定在一起，见 [pytorch_ipynb/cnn/cnn-basic.ipynb:L102-L105](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb#L102-L105) 中 device 与 random_seed 的设置。

模型类 `ConvNet` 定义于 [pytorch_ipynb/cnn/cnn-basic.ipynb:L163-L224](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb#L163-L224)：`__init__` 里逐层创建 conv/pool/linear，`forward` 末尾 `return logits, probas` 同时返回 logits 和 softmax 概率；L220-L224 紧接着实例化模型并创建 SGD 优化器。注意它还演示了不推荐的高斯初始化（`m.weight.data.normal_(0.0, 0.01)`）。

训练循环完全手写：`compute_accuracy` 自行统计正确数（[L368](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb#L368)），epoch×batch 双层 `for` 循环起于 [L381](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb#L381)，循环体内每一步都要手工 `features.to(device)`、前向（L389）、`optimizer.zero_grad()`（L391）、backward、step、print 日志。评估阶段在 [L436](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb#L436) 直接对**最终权重**测一次准确率（97.97%），没有验证集、没有最优 checkpoint。

**（b）Lightning 版：模型本体原样保留**

Lightning 版的第一个关键点：**模型类不继承 LightningModule，仍是纯 PyTorch**。`PyTorchModel` 定义于 [pytorch-lightning_ipynb/cnn/cnn-basic.ipynb:L149-L197](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/cnn/cnn-basic.ipynb#L149-L197)，网络结构与纯 PyTorch 版完全相同（同样的 8/16 通道两层卷积、`7*7*16` 分类头），只有两处差异：层用 `nn.Sequential` 组织得更紧凑；**`forward` 只返回 logits**。这是因为下游的 `cross_entropy` 与 `argmax` 都只需要 logits，softmax 概率是多余的（softmax 是单调的，argmax 结果不变——u2-l2 已论证）。

**（c）LightningModel：把「训练一台机器需要的所有零件」装进一个类**

[pytorch-lightning_ipynb/cnn/cnn-basic.ipynb:L213-L282](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/cnn/cnn-basic.ipynb#L213-L282) 是 Lightning 版的包装类，逐段看：

- 构造函数接收 `(model, learning_rate)`，用 `save_hyperparameters(ignore=["model"])`（L225）把超参记入日志目录但跳过模型参数本身；再创建三个 `torchmetrics.Accuracy` 实例分别服务 train/valid/test 三阶段。
- `_shared_step`（L239-L245）把「前向 → 交叉熵 → argmax」打包成一个函数，三个 step 方法复用它——这正是 u8-l1 讲过的 `_shared_step` 复用模式。
- `training_step`（L247-L261）：算损失并 `self.log("train_loss", loss)`；随后有个值得注意的细节——为了在训练曲线上画出「eval 模式下的训练准确率」，它临时 `self.model.eval()` + `torch.no_grad()` 再前向一次，用完切回 `self.model.train()`。这相当于把 u2-l4 讲的 train/eval 双模式切换写进了训练步。
- `validation_step`（L263）与 `test_step`（L275）结构相同，只更新各自的指标对象并以 `on_epoch=True` 记录 epoch 级指标。
- `configure_optimizers`（L280-L282）单独声明 Adam 优化器。注意对照差异：**纯 PyTorch 版用 SGD + lr 0.05，Lightning 版用 Adam + lr 0.005**，两版超参并不对齐（学习率见 [L105-L108](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/cnn/cnn-basic.ipynb#L105-L108) 的 `LEARNING_RATE = 0.005`），对照实验时须留意。

**（d）DataModule：两个 loader 变三个**

[pytorch-lightning_ipynb/cnn/cnn-basic.ipynb:L515-L572](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/cnn/cnn-basic.ipynb#L515-L572) 的 `DataModule`：`prepare_data`（L520）只负责下载；`setup`（L524）加载数据并用 `random_split` 把 60000 张训练图切成 55000/5000 的训练/验证两份——**纯 PyTorch 版根本没有验证集**，这是 Lightning 版新增的实验纪律；`train_dataloader`/`val_dataloader`/`test_dataloader`（L543/L553/L563）分别返回三个 loader，训练 loader `shuffle=True, drop_last=True`，其余两个保持确定顺序。

**（e）Trainer：五行换掉四十行循环**

训练配置在 [pytorch-lightning_ipynb/cnn/cnn-basic.ipynb:L637-L645](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/cnn/cnn-basic.ipynb#L637-L645)（`LightningModel` 实例化 + `ModelCheckpoint(save_top_k=1, mode="max", monitor="valid_acc")` + `CSVLogger`）与 [L1006-L1018](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/cnn/cnn-basic.ipynb#L1006-L1018)（`pl.Trainer(max_epochs=..., accelerator="auto", devices="auto", ...)` 与 `trainer.fit`）。对比纯 PyTorch 版 L381-L417 的手写循环：设备搬运、epoch 迭代、backward 三件套、模式切换全部消失。评估用 [L1167](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/cnn/cnn-basic.ipynb#L1167) 的 `trainer.test(..., ckpt_path='best')` 自动加载验证准确率最高的 checkpoint（测试准确率 98.79%）；推理则可用 [L1213](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/cnn/cnn-basic.ipynb#L1213) 的 `LightningModel.load_from_checkpoint(path, model=pytorch_model)` 从文件重建。

#### 4.1.4 代码实践

**实践目标**：通过逐 cell 对照，验证 4.1.1 的映射表，体会「删了什么、去了哪里」。

**操作步骤**：

1. 在浏览器中并排打开两份 notebook（GitHub 上同一 commit 的 `pytorch_ipynb/cnn/cnn-basic.ipynb` 与 `pytorch-lightning_ipynb/cnn/cnn-basic.ipynb`）。
2. 按下表逐行核对，把右列补完：

| 纯 PyTorch 版位置 | 代码内容 | 在 Lightning 版中的去向 |
|---|---|---|
| L102 | `device = torch.device(...)` | ？ |
| L163-L224 | `ConvNet` + 实例化 + SGD | ？ |
| L368-L379 | `compute_accuracy` | ？ |
| L381-L391 起 | 手写双层循环、`.to(device)`、`zero_grad` | ？ |
| L436 | 直接测最终权重 | ？ |

3. 特别核对两处「非平移」差异：模型 forward 的返回值从 `(logits, probas)` 变成 `logits`；优化器从 SGD(lr=0.05) 变成 Adam(lr=0.005)。

**需要观察的现象**：纯 PyTorch 版约 15 个 cell 的训练/评估代码，在 Lightning 版被压缩为「类定义 + 5 行 Trainer 配置」；模型本体几乎逐字相同。

**预期结果**：映射表右列依次为——device 选择 → `Trainer(accelerator="auto")`；ConvNet → `PyTorchModel`（L149-L197）+ `configure_optimizers`（L280-L282）；compute_accuracy → `torchmetrics.Accuracy`；手写循环 → `training_step` + `trainer.fit`（L1018）；直接测最终权重 → `ModelCheckpoint` + `trainer.test(ckpt_path='best')`（L1167）。本实践为纯源码阅读，无需运行，结果可直接从源码确认。

#### 4.1.5 小练习与答案

**练习 1**：纯 PyTorch 版 forward 返回 `(logits, probas)`，Lightning 版只返回 `logits`。为什么可以这样删？删掉后哪两处代码不受影响、哪一处受益？

**答案**：`F.cross_entropy` 只接受 logits（内部做数值稳定的 log_softmax，u2-l2）；`torch.argmax(logits, dim=1)` 与对 softmax 概率取 argmax 结果完全相同（softmax 单调保序）。因此损失计算与预测都不受影响。受益的是 `forward` 若要被 `Trainer.predict` 复用（[L213-L282](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/cnn/cnn-basic.ipynb#L213-L282) 中 LightningModel.forward 直接转发模型输出），返回单一 logits 接口更干净。

**练习 2**：Lightning 版为什么能在纯 PyTorch 版的基础上「凭空」多出一个验证集，而模型与损失函数一行不改？

**答案**：因为验证集只改变数据供给（`DataModule.setup` 里 `random_split` 切出 5000 张、`val_dataloader` 提供加载）与训练调度（`validation_step` 在每个 epoch 后被 Trainer 调用、`ModelCheckpoint` 据此选优），都属于 Trainer 与 DataModule 的职责范围；模型 forward 与损失计算对「数据来自哪个 loader」无感知。

**练习 3**：两版 notebook 的测试准确率分别是 97.97% 与 98.79%，能否据此得出「Lightning 训练效果更好」？

**答案**：不能。两版至少有三处混淆变量：优化器不同（SGD vs Adam）、学习率不同（0.05 vs 0.005）、epoch 数不同（10 vs 20），且 Lightning 版报告的是验证集选优后的 best checkpoint 成绩。这与 u3-l5 的结论一致：单次运行、配方不对齐的架构（框架）对比不可轻信。

### 4.2 分类模板 notebook：把新模型做成 Lightning 版的标准流水线

#### 4.2.1 概念说明

39 份 Lightning notebook 结构高度一致，因为它们都从**同一份模板**复制而来。模板把 4.1 节对照中「与具体模型无关的部分」全部固化（LightningModel 包装、DataModule、Trainer 配置、评估与可视化流程），只在「模型相关处」留下 `???` 占位符。作者新增一个模型时，只需做填空题：

- `# TITLE`、`DESCRIPTION`、`References` → 标题、描述、参考文献
- `# UNIQUE MODEL CODE` → 粘贴纯 PyTorch 模型类
- `%load ../code_dataset/dataset_???_check.py` → 数据检查代码
- `%load ../code_lightningmodule/datamodule_???_basic.py` → 数据模块
- `pytorch_model = PyTorchModel(???)` → 模型实例化参数
- `class_dict = {???}` → 类别索引到名字的映射

仓库里有两份几乎相同的模板：zoo 内的 [pytorch-lightning_ipynb/mlp/template_classification_basic.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/template_classification_basic.ipynb) 与官方模板目录的 [templates/pytorch_lightning/template_classification_basic.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/templates/pytorch_lightning/template_classification_basic.ipynb)。对两者做逐行 diff，差异仅限 cell 的 `id` 字段、flake8 忽略项、一处 `%load` 路径写法（`plot_visual-check_basic.py` 是否带 `../code_dataset/` 前缀）、末尾 `%watermark` cell 与 kernelspec 版本——**正文骨架逐字相同**。也就是说 zoo 里那份就是官方模板的工作副本，放在 mlp 目录里方便随手复制。

#### 4.2.2 核心流程

用模板新增一个 Lightning 模型的完整流程：

```text
复制模板 ──▶ 填标题/描述/参考文献
         ──▶ 替换 UNIQUE MODEL CODE（粘贴纯 PyTorch 模型类）
         ──▶ 展开各 %load cell（LightningModel / DataModule / Trainer 等）
         ──▶ 填 pytorch_model = PyTorchModel(具体参数)
         ──▶ 填 class_dict
         ──▶ 逐 cell 运行验证
```

一个重要的历史注脚：模板中大量 cell 是 `%load ../code_lightningmodule/xxx.py` 形式（如 [pytorch-lightning_ipynb/mlp/template_classification_basic.ipynb:L141](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/template_classification_basic.ipynb#L141)），但 `code_lightningmodule/` 与 `code_dataset/` 目录**在当前仓库中已不存在**（可用 `find` 验证）。这些目录曾是共享代码库，后来代码被内联进各 notebook，zoo 中现役 notebook 保留了 `# %load ...` 形式的注释作为来源痕迹（例如 Lightning 版 cnn-basic 的 [L207](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/cnn/cnn-basic.ipynb#L207)）。**今天使用模板时，这些 cell 无法靠 %load 自动展开，需从任一现役 Lightning notebook（如 cnn-basic）把对应代码段复制过来。**

#### 4.2.3 源码精读

**（a）模板骨架与占位符**

模板的骨架就是 u1-l2 讲过的五段式结构（Imports → Settings/Dataset → Model → Training → Evaluation）的 Lightning 化版本。关键占位 cell：

- 标题占位于 [pytorch-lightning_ipynb/mlp/template_classification_basic.ipynb:L53](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/template_classification_basic.ipynb#L53)（`# TITLE`），紧随其后的 DESCRIPTION/References 同样用 `???` 占位。
- 超参 cell 固定为 `BATCH_SIZE/NUM_EPOCHS/LEARNING_RATE/NUM_WORKERS` 四个全大写变量（模板 L79-L90 一带，与 cnn-basic 的 [L105-L108](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/cnn/cnn-basic.ipynb#L105-L108) 同构）。
- **模型唯一入口**：`# UNIQUE MODEL CODE` 占位于 [L131](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/template_classification_basic.ipynb#L131)——这是整份模板里唯一「每个模型都不同」的代码 cell。模板约定模型类必须叫 `PyTorchModel`，这样后面的 `LightningModel` 包装与实例化代码零修改。
- LightningModule 包装靠 [L141](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/template_classification_basic.ipynb#L141) 的 `%load ../code_lightningmodule/lightningmodule_classifier_basic.py` 载入——即 4.1.3（c）精读过的那段 `LightningModel` 代码（cnn-basic L213-L282 中已内联展开，两者一致）。
- 数据模块占位于 [L250](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/template_classification_basic.ipynb#L250)（`%load ../code_lightningmodule/datamodule_???_basic.py`），`???` 处填数据集名（如 mnist）。
- 模型实例化占位于 [L298-L300](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/template_classification_basic.ipynb#L298-L300)：`pytorch_model = PyTorchModel(???)`，填入构造参数。
- Trainer 配置靠 [L328](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/template_classification_basic.ipynb#L328) 的 `%load ../code_lightningmodule/trainer_nb_basic.py` 载入，内容即 cnn-basic L1006-L1018 的 Trainer + fit。
- 收尾的测试加载器、失败案例与混淆矩阵 cell（如 [L430](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/mlp/template_classification_basic.ipynb#L430) 的 `%load ../code_lightningmodule/datamodule_testloader.py`）对应 cnn-basic 后段的 `show_examples`（含 `sys.path.append("../../pytorch_ipynb")` 复用 helper_plotting）与 `ConfusionMatrix` 可视化。

**（b）模板与成品的关系：cnn-basic 就是「填好的模板」**

对照 Lightning 版 cnn-basic 的实际 cell 顺序——超参 → `PyTorchModel`（L149）→ `LightningModel`（L213）→ 数据检查/基线/可视化 → `DataModule`（L515）→ checkpoint/logger（L637）→ Trainer/fit（L1006）→ 画图 → test → predict → 失败案例 → 混淆矩阵——与模板骨架**逐段对应**。模板里每个 `%load` 在成品中都变成了带 `# %load` 注释的内联代码。这说明：读模板 = 读所有 Lightning notebook 的公共结构；读任何一份成品 = 读模板的一次实例化。

**（c）官方模板目录的第三个文件**

`templates/pytorch_lightning/` 下除了模板 notebook 还有 `tune_classification_basic.py` 与 `submit_command.sh`，它们把同一套流水线搬出 Jupyter，见 4.3 节。

#### 4.2.4 代码实践

**实践目标**：体验「填模板」流程中最核心的一步——只换 UNIQUE MODEL CODE，其余全部复用。

**操作步骤**：

1. 复制 `pytorch-lightning_ipynb/mlp/template_classification_basic.ipynb` 为 `my-densenet.ipynb`（放在 `pytorch-lightning_ipynb/mlp/` 或本地任意可运行目录；不要提交到仓库）。
2. 打开 `pytorch-lightning_ipynb/cnn/cnn-densenet121-mnist.ipynb`，找到其中纯 PyTorch 的 DenseNet 模型类，整体复制到模板的 `# UNIQUE MODEL CODE` cell，类名改为 `PyTorchModel`。
3. 从 Lightning 版 cnn-basic（L213-L282）复制 `LightningModel` 代码，替换模板中对应 `%load` cell；同样替换 DataModule cell（MNIST 版即可，复制 cnn-basic L515-L572）。
4. 修改 `pytorch_model = PyTorchModel(...)` 的构造参数为 DenseNet 的参数；`class_dict` 填 0-9 数字类名。
5. 依次运行到 `trainer.fit`，设 `NUM_EPOCHS = 1` 先跑通。

**需要观察的现象**：除了 UNIQUE MODEL CODE 与实例化参数，整份 notebook 没有任何其他逻辑改动；训练进度条、`valid_acc` 日志、checkpoint 保存行为与 cnn-basic 完全一致。

**预期结果**：1 个 epoch 训练与验证正常完成，`trainer.test(ckpt_path='best')` 输出 `test_acc`。若本机未装 pytorch_lightning/torchmetrics，先 `pip install` 对应包；仓库 notebook 基于 Lightning 1.6.0.dev0（cnn-basic watermark 所示），新装 2.x 版本可能需要把 `progress_bar_refresh_rate` 换成 `TQDMProgressBar` 回调（运行时会给出弃用警告，u8-l1 已提示）。

#### 4.2.5 小练习与答案

**练习 1**：模板为什么强制约定模型类名叫 `PyTorchModel`、且 forward 只返回 logits？

**答案**：因为模板后半段所有公共代码都按这两个约定书写：`LightningModel` 的构造参数名是 `model`、`pytorch_model = PyTorchModel(???)` 直接实例化、`_shared_step` 里 `cross_entropy(logits, true_labels)` 与 `argmax(logits, dim=1)` 假设输出是 logits。遵守约定，新增模型时公共段零修改；违反约定则每个下游 cell 都要改。

**练习 2**：模板中 `%load ../code_lightningmodule/...` 引用的目录已不存在。如果你直接运行模板里这个 cell 会发生什么？正确的做法是什么？

**答案**：`%load` 找不到目标文件会抛出文件不存在错误，cell 不会载入任何内容。正确做法是打开一份现役 Lightning notebook（如 cnn-basic），把带 `# %load` 注释的那段内联代码复制进模板对应 cell——两段代码本就同源（cnn-basic L207/L511 的注释可作佐证）。

**练习 3**：模板把「数据检查 → 性能基线 → 可视化抽查」也固化成了标准 cell（多数类别基线、标签分布 Counter）。这类代码对训练本身没有贡献，为什么值得保留在流水线里？

**答案**：它们是实验纪律的一部分：多数类基线给出「模型至少要超过的准确率」（MNIST 上约 11.35%，cnn-basic 的输出可见）；标签分布检查可在训练前发现数据管线错误（路径、transform、shuffle 配置）；可视化抽查能在第一时间发现 NCHW/NHWC 混淆、归一化方向颠倒等问题。这些检查在批量做实验（4.3 节的脚本化场景）时尤其重要——出错越晚发现，浪费的算力越多。

### 4.3 tune 脚本：脱离 notebook 的命令行批量实验

#### 4.3.1 概念说明

notebook 适合探索，但批量实验（例如「6 个学习率 × 3 个 batch size = 18 组训练」）需要在服务器/集群上用命令行反复启动。`templates/pytorch_lightning/tune_classification_basic.py` 就是模板 notebook 的 .py 化：**三件套（PyTorchModel/LightningModel/DataModule）逐字相同，只是超参来源从硬编码变量变成 argparse 命令行参数，并去掉了画图/失败案例等交互部分**。配套的 `submit_command.sh` 展示了作者在 Gradient 云平台上用 `grid run` 把参数列表交给脚本做网格搜索的方式。

#### 4.3.2 核心流程

```text
python tune_classification_basic.py --batch_size 64 --epochs 1 --learning_rate 1 --workers 0
        │
        ├─ argparse 解析 → BATCH_SIZE/NUM_EPOCHS/LEARNING_RATE/NUM_WORKERS
        ├─ 自动 pip install torchmetrics（幂等）
        ├─ PyTorchModel(MLP) + LightningModel 包装 + DataModule(MNIST)
        ├─ Trainer(deterministic=True) → trainer.fit
        └─ trainer.test(ckpt_path="best")   # 直接打印 test_acc 后退出

# 批量模式（Gradient 平台）：参数传 JSON 列表 → 自动展开为网格
grid run ... tune_classification_basic.py \
    --learning_rate "[0.0001, ..., 0.05]" --batch_size "[64, 128, 256]" --epochs 20
```

#### 4.3.3 源码精读

**（a）自举依赖与 argparse**

脚本开头先定义 `install()` 并执行 `install("torchmetrics")`（[templates/pytorch_lightning/tune_classification_basic.py:L18-L25](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/templates/pytorch_lightning/tune_classification_basic.py#L18-L25)），用 `subprocess.check_call([sys.executable, "-m", "pip", "install", ...])` 保证远程环境缺包也能自愈。参数定义在 [L29-L39](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/templates/pytorch_lightning/tune_classification_basic.py#L29-L39)：`--batch_size`、`--epochs`、`--workers`、`--learning_rate` 四个参数。

**注意一个真实的坑**：[L34](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/templates/pytorch_lightning/tune_classification_basic.py#L34) 写的是 `parser.add_argument("--learning_rate", type=int, default=0.005, ...)`——`type=int`！argparse 对非字符串的 default 不做类型转换，所以不传参时 `0.005` 有效；但从命令行传小数（如 `--learning_rate 0.001`）会触发 `int("0.001")` 抛 `ValueError`，只有整数能通过。这是「读源码以代码为准」的又一实例（u3-l5、u7-l4 已遇过同类问题），复用该脚本时应改成 `type=float`。

**（b）三件套与 notebook 逐字对应**

- `PyTorchModel`（[L47-L69](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/templates/pytorch_lightning/tune_classification_basic.py#L47-L69)）：一个按 `hidden_units` 元组循环搭建 `Linear+ReLU` 的通用 MLP，`torch.flatten(x, start_dim=1)` 让它能吃图像输入——演示了「模板换模型只换这一段」。
- `LightningModel`（[L77-L146](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/templates/pytorch_lightning/tune_classification_basic.py#L77-L146)）：与 4.1.3（c）精读的 notebook 版逐字相同（save_hyperparameters、三个 torchmetrics.Accuracy、_shared_step、三个 step、configure_optimizers）。
- `DataModule`（[L154-L210](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/templates/pytorch_lightning/tune_classification_basic.py#L154-L210)）：MNIST + random_split(55000/5000) + 三 loader，与 cnn-basic 的 L515-L572 相同。

**（c）初始化、Trainer 与收尾**

[L217-L227](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/templates/pytorch_lightning/tune_classification_basic.py#L217-L227) 实例化 `PyTorchModel(input_size=28*28, hidden_units=(128, 256), num_classes=10)`、`LightningModel`、`ModelCheckpoint(monitor="valid_acc")`、`CSVLogger` 与 `DataModule`；[L229-L238](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/templates/pytorch_lightning/tune_classification_basic.py#L229-L238) 的 Trainer 与 notebook 版几乎相同，唯一实质差异是 **`deterministic=True`**（cnn-basic notebook 里是 `False`）——批量调参追求可比性，牺牲一点速度换取逐位复现（u4-l4 讲过该权衡：仓库实测开确定性对耗时几乎无影响）。最后 [L244-L250](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/templates/pytorch_lightning/tune_classification_basic.py#L244-L250)：`trainer.fit` → 打印训练耗时 → `trainer.test(ckpt_path="best")` 直接输出最优模型成绩，脚本结束。没有画图、没有失败案例展示——批量场景下结论只看数字。

**（d）submit_command.sh：网格扫描的提交方式**

[templates/pytorch_lightning/submit_command.sh:L1-L8](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/templates/pytorch_lightning/submit_command.sh#L1-L8) 用 Gradient 平台的 `grid run` 提交：`--instance_type 2_m60_8gb --gpus 2` 指定双 M60 GPU 的实例，随后把 `--learning_rate` 传成 6 个值的 JSON 列表、`--batch_size` 传成 3 个值的列表、`--epochs 20`。grid 会把列表参数做笛卡尔积，自动展开成 \(6 \times 3 = 18\) 个独立实验，每个实验各自的 `logs/my-model/version_N/metrics.csv` 记录了完整曲线，事后汇总即可选出最优超参组合。没有 Gradient 账户也不影响——`grid run` 只是批量启动器，本地用 `python tune_classification_basic.py` 配 shell 循环可达到同样效果。

#### 4.3.4 代码实践

**实践目标**：验证 tune 脚本可在本地命令行跑通，并亲手复现 4.3.3（a）指出的 argparse 类型坑。

**操作步骤**：

1. 在本地（或 CPU 环境）进入 `templates/pytorch_lightning/` 目录，先运行最小配置：
   ```bash
   python tune_classification_basic.py --batch_size 64 --epochs 1 --workers 0 --learning_rate 1
   ```
   （MNIST 会自动下载到 `./data`；`--workers 0` 规避多进程数据加载的文件句柄问题。）
2. 观察 `logs/my-model/version_0/` 下生成的 `metrics.csv` 与 checkpoint 文件。
3. 再故意运行 `python tune_classification_basic.py --epochs 1 --learning_rate 0.005`，观察报错。
4. 把 L34 的 `type=int` 改为 `type=float`（改自己机器上的副本），重跑第 3 步命令。

**需要观察的现象**：第 1 步正常训练 1 个 epoch 并打印 `test_acc`（MLP + Adam 在 MNIST 上 1 个 epoch 通常可达 95% 左右，具体数值**待本地验证**）；第 3 步抛出 `ValueError: invalid literal for int() with base 10: '0.005'`；第 4 步修复后可正常传入小数学习率。

**预期结果**：确认「default 不经 type 转换、命令行值必经 type 转换」的 argparse 行为；确认脚本的 CSVLogger 产物结构与 notebook 版一致。

#### 4.3.5 小练习与答案

**练习 1**：tune 脚本与模板 notebook 的三件套代码逐字相同，除了超参来源和结尾部分。列出所有让你确认「这是同一套代码」的证据，以及两处实质差异。

**答案**：证据——`LightningModel`（脚本 L77-L146）与 cnn-basic notebook L213-L282 内容一致；`DataModule`（L154-L210）与 notebook L515-L572 一致；`ModelCheckpoint(monitor="valid_acc")` + `CSVLogger("logs/", "my-model")` 的组织方式一致。实质差异：①超参从 notebook 顶部硬编码变量变为 argparse 参数（L29-L39）；②Trainer 的 `deterministic` 从 notebook 版的 `False` 变为 `True`（L236），且脚本结尾只 fit + test，省去画图与失败案例分析。

**练习 2**：为什么脚本要在文件开头 `pip install torchmetrics`，而 notebook 不用？

**答案**：notebook 有人交互，缺包时报错可以现场处理；脚本的典型运行环境是云端刚启动的裸实例（见 submit_command.sh 的 `--instance_type`），无人值守，缺包即整个实验失败。`subprocess.check_call([sys.executable, "-m", "pip", "install", ...])` 用当前解释器自举安装，保证批量任务不因环境缺包而中断。这也是一种幂等设计——已安装时 pip 快速跳过。

**练习 3**：如果想在 18 组网格结果中选最优超参，你应该比较哪个数字：每组实验的 `test_acc`，还是 `valid_acc`？为什么？

**答案**：选超参应看 `valid_acc`（或最优 checkpoint 对应的验证指标）。`test_acc` 只应在最终选定超参、全量重训后评估一次（u7-l2 的纪律：测试集只用一次）。若直接用 18 组的 test_acc 挑最大，等于让测试集参与了超参选择，其成绩会对真实泛化性能给出乐观偏倚的估计。脚本的 CSVLogger 恰好把 `valid_acc` 逐 epoch 记入 metrics.csv，方便事后比较。

## 5. 综合实践

**任务：把 `cnn-resnet34-mnist` 从纯 PyTorch 版迁移为 Lightning 版并跑通。**

背景核查（已由本讲验证）：`pytorch-lightning_ipynb/cnn/` 目录下**没有**任何 resnet notebook（现有 18 份：alexnet、allconv、basic、densenet121、he-init、lenet5、mobilenet、nin、vgg 等），而 `pytorch_ipynb/cnn/` 下有 `cnn-resnet18/34/50-mnist.ipynb` 等多份——这正是「尚无 Lightning 版本的模型」。

实施步骤：

1. **复制模板**：把 `pytorch-lightning_ipynb/mlp/template_classification_basic.ipynb` 复制为工作目录下的 `cnn-resnet34-mnist-lightning.ipynb`（不要提交到仓库）。
2. **搬模型**：打开 [pytorch_ipynb/cnn/cnn-resnet34-mnist.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-resnet34-mnist.ipynb)（其中 `class ResNet` 定义在 JSON 源文件 L362 起），把整套 ResNet 模型代码（含 BasicBlock 与 `ResNet` 类）复制进模板的 `# UNIQUE MODEL CODE` cell，顶层模型类改名为 `PyTorchModel`，确认其 forward 只返回 logits（若原版返回多个值则删减）。
3. **填公共段**：从 Lightning 版 cnn-basic 复制 `LightningModel`（L213-L282）与 MNIST `DataModule`（L515-L572）替换模板中对应 `%load` cell；`pytorch_model = PyTorchModel(num_blocks=[3,4,6,3], ...)` 按原 notebook 的 ResNet-34 配置填参。
4. **填杂项**：标题、描述、`class_dict = {0:'0', ..., 9:'9'}`。
5. **跑通**：`NUM_EPOCHS = 1` 先验证整条流水线（fit → test → 失败案例 → 混淆矩阵）；成功后再训练完整 epoch 数。
6. **对照**：与纯 PyTorch 版 cnn-resnet34-mnist 的测试准确率对比（u3-l4 提到 ResNet-18 在 MNIST 约 99.06%，可作参考量级），并按 4.1.5 练习 3 的标准检查两版配方是否对齐。

预期结果：1 个 epoch 内 `valid_acc` 正常爬升、`trainer.test(ckpt_path='best')` 输出 `test_acc`（具体数值待本地验证）；整份 notebook 除 UNIQUE MODEL CODE 与实例化参数外与 cnn-basic 结构逐段相同——这正说明你已掌握本讲的模板化工作流。

## 6. 本讲小结

- **风格对照迁移**：Lightning 版与纯 PyTorch版的模型本体几乎逐字相同（保持 `nn.Module`、forward 只返回 logits）；消失的是设备搬运、手写双层循环、手写准确率函数，它们分别变成 `Trainer(accelerator="auto")`、`training_step + trainer.fit`、`torchmetrics.Accuracy`；新增的是验证集（`random_split`）、`ModelCheckpoint` 按验证准确率选优与 CSVLogger 结构化日志。
- **模板即公共结构**：zoo 内 `pytorch-lightning_ipynb/mlp/template_classification_basic.ipynb` 与官方 `templates/pytorch_lightning/template_classification_basic.ipynb` 正文逐字相同（diff 仅 cell id 等细节）；每份 Lightning notebook 都是模板的一次实例化，唯一模型相关入口是 `# UNIQUE MODEL CODE` cell 与 `PyTorchModel(???)` 实例化。
- **模板中的 `%load` 引用的 `code_lightningmodule/`、`code_dataset/` 目录在当前仓库已不存在**，使用模板时需从现役 notebook（如 cnn-basic）复制对应代码段；现役 notebook 中的 `# %load` 注释是共享代码时代的来源痕迹。
- **tune 脚本是模板的 .py 化**：三件套逐字相同，超参改由 argparse 传入，开头自举安装 torchmetrics，`deterministic=True` 换取逐位复现，结尾只 fit + test；配合 `submit_command.sh` 的 `grid run` 可把参数列表展开成 6×3=18 组网格实验。
- **读源码以代码为准**：`--learning_rate` 的 `type=int`（L34）会让命令行传小数学习率直接崩溃——default 不经 type 转换、命令行值必经转换，这类细节只有读源码才能发现。
- **对照两版成绩时先查配方**：cnn-basic 两版的优化器（SGD/Adam）、学习率（0.05/0.005）、epoch 数（10/20）均不相同，97.97% vs 98.79% 的差异不能归因于框架。

## 7. 下一步学习建议

- **下一讲（u8-l3）**：数据增强实战（AutoAugment 与 TrivialAugment）——同样基于 pytorch-lightning 目录，将看到 Lightning 版实验如何组织「无增强/两种增强」的三方对照，本讲的模板化视角会让你一眼看出三份 notebook 只差 transform 一处。
- **回到 zoo 练迁移**：用综合实践的工作流再迁移一个更复杂的模型（如 `pytorch_ipynb/cnn/cnn-vgg16.ipynb` 或 `ordinal/baseline_cement.ipynb` 的回归模板需求），体会「填空题」的复用边界。
- **延伸阅读源码**：`templates/pytorch_lightning/tune_classification_basic.py` 中 `save_hyperparameters(ignore=["model"])` 的产物可在 `logs/my-model/version_*/hparams.yaml` 中查看；对照 Lightning 官方文档的 ModelCheckpoint 与 CSVLogger 章节，理解 1.x → 2.x 的 API 更名（如 `progress_bar_refresh_rate` → `TQDMProgressBar`）会如何影响本仓库代码的迁移。
