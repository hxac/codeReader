# 多 GPU 并行：DataParallel 与流水线并行

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚「为什么要多卡」的两个不同动机：**算力不够想提速**（数据并行）与**单卡显存装不下模型**（模型并行）。
2. 精读 `nn.DataParallel` 的用法：batch 如何沿第 0 维拆分到各卡、损失为何在主卡 `cuda:0` 上计算、训练循环为什么一行都不用改。
3. 掌握 CelebA 这类 20 万张图的大数据集从原始 txt 清单到三个 CSV 划分文件的磁盘准备流程。
4. 理解流水线并行的核心思想：把模型**按层切成多个 stage 分放到不同卡**，再用 microbatch 往流水线里「灌水」以减少空闲气泡，并会用公式估算气泡占比。
5. 养成「读代码以代码为准」的习惯——本讲的两个 notebook 里各藏着一处容易看走眼的真实代码细节（`num_features` 未被使用、变量名覆盖），我们会逐一拆穿。

## 2. 前置知识

本讲建立在前面讲义已建立的概念之上，先用两段话把最需要的背景补齐。

**单卡训练的五步范式与显存构成。** 从 u2-l2 开始，所有训练循环都是同一副骨架：`forward → loss → zero_grad → backward → step`。u8-l4（梯度检查点）讲过训练显存由四部分组成：参数、梯度、优化器状态是**常数项**，而中间激活随「层数 × batch size」线性膨胀。当模型大（VGG-16 有上亿参数）或图大（CelebA 缩放后仍是 128×128）时，激活显存会把单卡 batch size 压得很小，训练又慢又碎——这是多卡动机之一。另一动机纯粹是算力：20 万张图 × 多个 epoch 的计算量，单卡要跑很久。

**数据并行 vs 模型并行。** 这是本讲最重要的直觉区分：

| 维度 | 数据并行（DataParallel） | 模型并行 / 流水线并行（Pipe） |
|---|---|---|
| 切分对象 | 切**数据**：batch 拆成 N 份 | 切**模型**：层拆成 N 段（stage） |
| 每张卡上有什么 | 完整模型的一份副本 + 1/N 的 batch | 模型的一段（不完整）+ 完整流过它的数据 |
| 主要收益 | 提速（吞吐量） | 突破单卡显存上限 |
| 典型代价 | 主卡汇总负载、线程同步开销 | 流水线气泡（部分时刻有卡在空等） |

几个本讲会用到的术语：

- **cuda:0**：第一块 GPU 的设备名；`cuda:1`、`cuda:2` 依次类推；`torch.cuda.device_count()` 返回可见 GPU 数量。
- **reduce（归约）**：把分散在各卡上的同类中间结果（这里是梯度）汇总合并到一张卡的操作。
- **replica（副本）**：DataParallel 在每次前向时广播模型参数，为每张卡生成的模型副本。
- **microbatch（微批）**：流水线并行中，把一个 minibatch 再切成的小块；流水线靠多个 microbatch 交错执行来填满各卡。
- **stage（阶段）**：流水线并行中，被切分出的模型段；一个 stage 驻留在一张卡上。
- **RPC**：Remote Procedure Call，PyTorch 的 `torch.distributed.rpc` 模块，torch 1.8 的 `Pipe` 实验特性建立在它之上。

## 3. 本讲源码地图

本讲涉及三个 notebook（均为 Jupyter JSON 文件，**下文所有行号均指 `.ipynb` 原始 JSON 文件中的行号**）：

| 文件 | 作用 | 关键内容 |
|---|---|---|
| `pytorch_ipynb/cnn/cnn-vgg16-celeba-data-parallel.ipynb` | 主讲义：VGG-16 在 CelebA 上做性别分类 + DataParallel | DataParallel 概念图解、`BATCH_SIZE=64*device_count()`、`cuda:0` 约束、保存了作者 4 卡运行的真实输出 |
| `pytorch_ipynb/cnn/cnn-resnet18-celeba-dataparallel.ipynb` | 姊妹篇：同一套数据管线换 ResNet-18 骨架 | `BATCH_SIZE=256*device_count()` 对照；带 BatchNorm 的网络在数据并行下的注意点 |
| `pytorch_ipynb/mechanics/model-pipeline-vgg16.ipynb` | 流水线并行：torch 1.8 实验 API `Pipe` 拆 VGG-16 到多卡 | 1-GPU 基线 → block 拆分 → RPC 环境准备 → `Pipe(model, chunks=8)` |

三者关系：前两个是「数据并行」的两次受控实例化（只换模型骨架），第三个是同一 VGG-16 的「流水线并行」写法，与第一节形成方法论对照。此外 pipeline notebook 通过 `sys.path.insert(0, "..")` 复用了 u4-l4 精读过的 `helper_train.train_classifier_simple_v1`，正好检验你对共享工具链的掌握。

## 4. 核心概念与源码讲解

### 4.1 DataParallel 数据并行

#### 4.1.1 概念说明

`nn.DataParallel` 是 PyTorch 中**门槛最低**的多卡方案：模型定义、损失函数、训练循环全部保持单卡写法，只在模型实例化后包一层即可。它解决的问题是**算力**——用 N 张卡近似把每步训练加速到接近 N 倍（实际远达不到，后面讲为什么）。

它的核心机制是**单进程多线程**地在多卡间做「拆数据—复制模型—并行计算—汇总结果」：

1. 输入张量沿第 0 维（batch 维）**均分**（scatter）到各卡；
2. 模型参数**复制**（replicate）成 N 份副本，每卡一份；
3. 各卡在自己的子批上**并行前向**；
4. 各卡输出被**收集**（gather）回 `devices[0]`（即 `cuda:0`，下称主卡）；
5. 损失在主卡上对收集到的完整输出计算——因为像 `F.cross_entropy` 这样的损失函数定义在模型**外面**；
6. 反向传播时各卡并行算梯度，梯度被 **reduce**（求和）回主卡，优化器在主卡上更新参数，下一轮再把新参数广播出去。

注意一个容易忽略的数学事实：DataParallel 一步更新用的梯度，等价于「整个大 batch 一次前向反向」的梯度（`reduction='mean'` 时即全批平均），而**不是**「把大 batch 切成 N 个小 batch 依次更新 N 次」。所以 notebook 作者提醒：与单卡小 batch 训练相比，DataParallel 得到的模型会略有不同——有效 batch 变大了，学习率等超参可能需要相应调整。

#### 4.1.2 核心流程

用一个伪代码流程描述 DataParallel 下「取一个 batch」的完整生命周期（设 N 张卡，总 batch 为 B）：

```text
输入 features[B,...], targets[B]  （已在 cuda:0）
   │
   ├─ scatter：features 沿 dim=0 均分 → 每卡拿到 B/N 个样本
   ├─ replicate：模型参数广播 → 每卡一份副本
   ├─ 各卡并行：logits_i = 副本_i(features_i)        # i = 0..N-1
   ├─ gather：logits_0..N-1 拼回主卡 → logits[B,...]
   ├─ 主卡：cost = F.cross_entropy(logits, targets)   # 损失在模型外，故在主卡算
   ├─ cost.backward()：各卡并行算梯度 → 梯度求和 reduce 回主卡
   └─ optimizer.step()：主卡更新参数（下轮再广播）
```

两个关键约束直接从流程推出：

- **数据必须先搬到 `cuda:0`**。scatter 从主卡出发，如果输入在 `cuda:1`，会直接得到 `RuntimeError: all tensors must be on devices[0]`。
- **每卡子批不能太小**，尤其对含 BatchNorm 的网络：BN 的批统计是**每卡各算各的**（副本之间互不知晓），子批只有两三个样本时统计量噪声极大。本讲 VGG-16 版没有 BN（classifier 是纯 Linear+ReLU），ResNet-18 版有 BN，所以后者把每卡批量基准从 64 提到 256。

#### 4.1.3 源码精读

**(a) 作者的概念图解：五步描述 DataParallel**

[cnn-vgg16-celeba-data-parallel.ipynb:L77-L90](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16-celeba-data-parallel.ipynb#L77-L90) 这段 markdown 用编号列表写出 DataParallel 的概念步骤：每卡对 minibatch 的一块做前向（L79）、主卡汇总预测并计算损失（因为损失定义在模型外，L80 附近）、各卡反传、主卡把各卡梯度**求和**（reduce，L82）、主卡更新权重后再把副本发给各卡。这与 4.1.1 的流程一一对应，建议对照阅读原文。

**(b) `cuda:0` 约束与「换一行」的最小改动**

[cnn-vgg16-celeba-data-parallel.ipynb:L120-L146](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16-celeba-data-parallel.ipynb#L120-L146) 「Implementation Details」一节明确写出两件事：一是用 `if torch.cuda.device_count() > 1: model = nn.DataParallel(model)` 替换单卡写法（L129-L133 附近）；二是 `device` **必须**是 `torch.device("cuda:0")`（L138）而**不能**是 `cuda:1`（L144），否则报 `RuntimeError: all tensors must be on devices[0]`——这正是 4.1.2 推出的第一个约束。

**(c) 核心包装代码：`#### DATA PARALLEL START ####` 标记块**

[cnn-vgg16-celeba-data-parallel.ipynb:L1064-L1080](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16-celeba-data-parallel.ipynb#L1064-L1080) 这是本模块的代码核心：

```python
torch.manual_seed(random_seed)

#### DATA PARALLEL START ####
model = VGG16(num_features=num_features, num_classes=num_classes)
if torch.cuda.device_count() > 1:
    print("Using", torch.cuda.device_count(), "GPUs")
    model = nn.DataParallel(model)
#### DATA PARALLEL END ####

model.to(device)
optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
```

要点：

- 包装发生在 `model.to(device)` **之前**，`DataParallel` 对象本身也是个 `nn.Module`，后续 `.to()`、`.parameters()`、`.train()`/`.eval()` 全部照常工作；
- `device_count() <= 1` 时自动退化为单卡，同一份代码两种环境都能跑；
- 优化器拿的是 `DataParallel` 包装后模型的参数——参数对象与原模型是同一批，所以更新照常生效；
- 作者保存的真实输出（L1059）是 `Using 4 GPUs`：这份 notebook 是在 4 张卡上实际运行过的。

顺带一个「以代码为准」的小发现：设置单元里 `num_features = 128*128`（[L859](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16-celeba-data-parallel.ipynb#L859)）虽然传进了构造函数，但 `forward` 里实际用的是 `x.view(-1, 512*4*4)`（[L1036-L1047](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16-celeba-data-parallel.ipynb#L1036-L1047)）——`num_features` 形参从未被使用。读 notebook 时不要想当然认为「传了就生效」。

**(d) batch size 随卡数自动放大**

[cnn-vgg16-celeba-data-parallel.ipynb:L752](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16-celeba-data-parallel.ipynb#L752) 一行 `BATCH_SIZE=64*torch.cuda.device_count()`，配合 [L755-L768](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16-celeba-data-parallel.ipynb#L755-L768) 的三个 DataLoader，把「总 batch = 每卡 batch × 卡数」写成公式。作者的显存测算依据在 [L722](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16-celeba-data-parallel.ipynb#L722) 的 markdown：单卡 batch 64 约 6.6 Gb，拆到 4 卡后总 batch 可以放心开到 64×4=256——**每卡显存压力不变、吞吐量提升**，这是数据并行放 batch 的正确姿势（区别于单卡硬开大 batch 把显存撑爆）。

**(e) 训练循环一行不改**

[cnn-vgg16-celeba-data-parallel.ipynb:L1189-L1225](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16-celeba-data-parallel.ipynb#L1189-L1225) 是完整训练循环：`features/targets` 搬到 `device`（即 `cuda:0`）、五步范式原样执行（`cost = F.cross_entropy` 在 L1200）。对照 [L1175-L1186](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16-celeba-data-parallel.ipynb#L1175-L1186) 的 `compute_accuracy` 同样不改。这就是 DataParallel 的卖点：**并行逻辑全部藏在 `model(features)` 这一次调用里**。作者保存的真实输出（L1136-L1170）：batch 256 时每个 epoch 636 个 batch，约 6.4 分钟，3 个 epoch 共 19.24 分钟，最终 Train 95.13% / Valid 95.66%，[L1270](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16-celeba-data-parallel.ipynb#L1270) 记录 Test accuracy 94.96%。

**(f) 姊妹篇对照：ResNet-18 版本**

[cnn-resnet18-celeba-dataparallel.ipynb:L901-L910](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-resnet18-celeba-dataparallel.ipynb#L901-L910) 与 (c) 完全同构：`resnet18(NUM_CLASSES)` → `device_count() > 1` 则包 `DataParallel` → `model.to(DEVICE)`，L887 保存的输出同样是 `Using 4 GPUs`。两处实质差异值得注意：一是 [L208-L211](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-resnet18-celeba-dataparallel.ipynb#L208-L211) 把每卡批量基准提到 `BATCH_SIZE = 256*torch.cuda.device_count()`——ResNet-18（[L755-L795](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-resnet18-celeba-dataparallel.ipynb#L755-L795) 的 `BasicBlock`/`ResNet` 含 BatchNorm，骨架即 u3-l4 讲过的结构）在数据并行下 BN 统计逐卡独立，子批太小会让统计量失真；二是这里损失用 `torch.nn.CrossEntropyLoss()` 模块化定义（L914），与 VGG 版的函数式 `F.cross_entropy` 只是风格差异，都在主卡计算。

**(g) 推理阶段照旧**

[cnn-vgg16-celeba-data-parallel.ipynb:L1333-L1334](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16-celeba-data-parallel.ipynb#L1333-L1334) 对单张测试图 `model(features.to(device)[0, None])`，保存输出为 `Probability Female 97.02%`——推理时 DataParallel 同样工作（batch=1 也走拆分-汇总流程），`model.eval()` 与 `torch.set_grad_enabled(False)`（[L1275-L1278](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16-celeba-data-parallel.ipynb#L1276-L1278)）的纪律与单卡一致。

#### 4.1.4 代码实践

**实践目标**：不动源码，通过「注释 + 单卡模拟」把 DataParallel 的数据流向彻底搞清楚；有条件的再实测吞吐。

**操作步骤**：

1. 打开 `pytorch_ipynb/cnn/cnn-vgg16-celeba-data-parallel.ipynb`，在以下五处添加中文注释（注释写在 notebook 副本里，不动仓库文件）：L129 附近（为什么先判 `device_count()`）、L752（batch 为何乘卡数）、L1066-L1072（包装时机在 `.to()` 之前）、L1199-L1205（五步范式哪些步发生在多卡上：forward 各卡并行、loss 在主卡、backward 各卡并行、step 在主卡）、L138/L144（device 为何必须是 `cuda:0`）。
2. 在单卡（甚至 CPU）环境跑一段最小模拟脚本（**示例代码**，非仓库原有）：

   ```python
   import torch, torch.nn as nn
   device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
   model = nn.Linear(4, 2)
   dp = nn.DataParallel(model, device_ids=[0] if device.type == "cuda" else None)
   dp.to(device)
   x = torch.randn(8, 4, device=device)
   print(dp(x).shape)          # [8, 2]：输出已 gather 回主卡
   print(dp.module is model)   # True：.module 暴露被包装的原始模型
   ```

3. 若有 ≥2 卡环境：把 `BATCH_SIZE` 分别设为 `64`、`64*torch.cuda.device_count()`，各跑 1 个 epoch，用 `time.time()` 记录耗时，仿照 [L193-L202](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16-celeba-data-parallel.ipynb#L193-L202) 作者的「Speed Comparison」记录你的数字。作者在 1080Ti×4 上的经验是：同 batch（每卡 16）约 2 倍速，batch×4（每卡 64）约 3 倍速。

**需要观察的现象**：

- 步骤 2 中输出形状与单卡完全一致（gather 对使用者透明）；`dp.module` 确实是原始 `model`——这也提示保存权重时 `state_dict` 的键会带 `module.` 前缀，跨单卡/多卡加载时要注意。
- 步骤 3 中「同 batch」加速比明显低于卡数（主卡汇总与线程同步是串行瓶颈），「batch×4」加速比更接近卡数。

**预期结果**：单卡模拟脚本应原样跑通（CPU 上 `device_ids=None` 即单设备退化）；吞吐数字因硬件而异，属「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 DataParallel 要求输入数据先放到 `cuda:0`，放到 `cuda:1` 会发生什么？

**参考答案**：DataParallel 以 `devices[0]`（默认 `cuda:0`）为主卡，scatter 从主卡分发数据、gather 把输出收回主卡、损失与参数更新也都发生在主卡。若输入在其他卡上，gather 到的输出与损失所在设备、参与运算的张量设备不一致，触发 `RuntimeError: all tensors must be on devices[0]`。notebook 的 Implementation Details（L120-L146）对此有明确说明。

**练习 2**：`BATCH_SIZE=64*torch.cuda.device_count()` 后，有效 batch 变成 256（4 卡），这和单卡直接用 batch=256 有什么区别？

**参考答案**：数学上前向/损失的梯度等价（各卡子批梯度 reduce 后等于全批一次更新的梯度）；显存上每卡只需容纳 64 个样本的激活，单卡 256 则要全部装下；但对含 BatchNorm 的模型（如 ResNet-18 版）有实质差异：BN 统计是逐卡计算的，每卡 64 样本的统计与单卡 256 样本的统计不同。此外大 batch 通常需要配合调大学习率。

**练习 3**：为什么作者说「DataParallel 得到的模型与常规反传略有不同」？

**参考答案**：DataParallel 一步更新等价于对整个大 batch 做一次梯度更新；常规单卡训练会把同样多的样本切成 N 个 minibatch 依次更新 N 次。两者每步看到的梯度尺度与更新次数都不同，即使总样本数相同，训练出的最终权重也会有差异（L91-L100 附近 markdown，配 minibatch-update 对比图）。

### 4.2 CelebA 数据准备

#### 4.2.1 概念说明

CelebA 是约 20 万张名人人脸图（约 1.3 Gb）的数据集，u4-l2 已见过「图片目录 + 标签清单」的通用形态。本模块聚焦它特有的**磁盘准备流程**：官方发布的三个原始文件——`img_align_celeba.zip`（全部 jpg 图片）、`list_attr_celeba.txt`（40 个属性标签）、`list_eval_partition.txt`（官方 train/valid/test 划分）——需要用 pandas 整理成三个 CSV 清单，才能交给自定义 Dataset 使用。

为什么值得单独讲？因为这是你第一次遇到「**数据不在 torchvision 内置、划分由官方文件规定**」的情形：划分不是随机切，而是严格按 `list_eval_partition.txt` 的 0/1/2 标记执行，保证与论文、与他人实验可比。这套流程对任何「目录 + 两个 txt 清单」式的数据集都可复用。

#### 4.2.2 核心流程

```text
list_attr_celeba.txt ──读取 Male 列（-1/1 → 0/1）──> df1（性别标签）
list_eval_partition.txt ──读为 Filename→Partition 索引表──> df2（官方划分）
        │
        ├── df1.merge(df2) → celeba-gender-partitions.csv（标签+划分合并表）
        │
        └── 按 Partition ∈ {0,1,2} 过滤导出三个清单：
            celeba-gender-train.csv / celeba-gender-valid.csv / celeba-gender-test.csv
                 │
                 ▼
        CelebaDataset(csv_path=清单, img_dir='img_align_celeba/')
            transform: CenterCrop(178,178) → Resize(128,128) → ToTensor
```

注意这里**不需要 shuffle 划分**：与 u4-l2 的 Quickdraw（按类别连续生成、必须打乱后再切）不同，CelebA 的划分由官方清单显式规定，pandas 按标记过滤即可，天然无泄漏。

#### 4.2.3 源码精读

**(a) 读取属性清单，重编码标签**

[cnn-vgg16-celeba-data-parallel.ipynb:L389-L394](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16-celeba-data-parallel.ipynb#L389-L394) 用 `pd.read_csv(..., sep="\s+", skiprows=1, usecols=['Male'])` 读属性清单（跳过头部说明行、只取 Male 列），随后把 `-1/1` 重编码为 `0/1`（女性/男性）。保存的输出显示 df1 以文件名为索引、Male 取值 0/1。

**(b) 合并官方划分并导出三个 CSV**

[cnn-vgg16-celeba-data-parallel.ipynb:L643-L645](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16-celeba-data-parallel.ipynb#L643-L645) 三行 `df4.loc[df4['Partition'] == 0].to_csv('celeba-gender-train.csv')`（0/1/2 对应 train/valid/test）：按官方划分标记过滤、各自落盘。上游的 merge（`df1.merge(df2, left_index=True, right_index=True)`，[L553](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16-celeba-data-parallel.ipynb#L553)）把标签表与划分表按文件名索引对齐。

**(c) CelebaDataset：u4-l2 三要素模板的再实例化**

[cnn-vgg16-celeba-data-parallel.ipynb:L692-L720](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16-celeba-data-parallel.ipynb#L692-L720) 是标准的 Dataset 三要素：`__init__` 读 CSV 存文件名数组与标签数组、`__getitem__`（L704）拼路径后用 PIL 惰性读图并应用 transform、`__len__` 返回样本数。与 u4-l2 的 CelebaDataset 逐字相同——数据准备一次成型的产物，可被任意模型 notebook 复用。

**(d) 图像尺寸链：218×178 → 178×178 → 128×128**

[cnn-vgg16-celeba-data-parallel.ipynb:L734-L740](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16-celeba-data-parallel.ipynb#L734-L740) 的 `custom_transform` 先 `CenterCrop((178,178))`（ CelebA 原图 218×178，先裁正方形去掉高出的部分）再 `Resize((128,128))`，最后 `ToTensor()`（注释提醒它内部已除以 255）。128×128 经过 VGG-16 五次减半池化得 4×4，这正是 4.1.3(c) 中 classifier 输入 `512*4*4` 的来源——**数据尺寸与模型结构在这里对上**。

#### 4.2.4 代码实践

**实践目标**：不下载 1.3 Gb 数据也能吃透「清单整理」这一步。

**操作步骤**（**示例代码**，可在任意目录以纯 pandas 完成）：

1. 手工构造两个小文件模拟官方清单：`list_attr_celeba.txt` 头两行数据 + `list_eval_partition.txt` 若干行，例如六张图、Partition 取 0/1/2 各两个。
2. 依次运行 notebook L389-L394、L553、L632-L645 的等价代码，观察 merge 后的合并表和导出的三个 CSV。
3. 打印 `pd.read_csv('celeba-gender-train.csv', index_col=0)` 验证：索引是文件名、只有 Male 列、行数与 Partition==0 的数量一致。

**需要观察的现象**：merge 按**文件名索引**对齐（不是按行号）；三个 CSV 的行数之和等于总图数且互不重叠；`-1` 标签已被替换为 `0`。

**预期结果**：六个样本的小模拟应精确产出 2+2+2 的三份清单；换成真实 CelebA 后数量级约为 16 万/2 万/2 万——「待本地验证」（需下载数据集）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 CelebA 这里按 `Partition` 过滤导出，而 u4-l2 的 Quickdraw 必须先打乱再切分？

**参考答案**：CelebA 的划分由官方 `list_eval_partition.txt` 显式给定，每张图的归属是外部规定的，按标记过滤天然保证与官方基准可比；Quickdraw 是我们自己按类别连续生成的目录，不打乱就直接切会把整个类别整段划进同一个子集，造成分布泄漏。

**练习 2**：若把 `Resize((128,128))` 改成 `Resize((64,64))`，下游模型代码必须同步改哪里？

**参考答案**：VGG-16 五个 block 各有一次减半池化，64×64 最后得到 2×2 特征图，`forward` 中的 `x.view(-1, 512*4*4)` 与 `classifier` 首层 `nn.Linear(512*4*4, 4096)`（L1016）都要改成 `512*2*2`（见 L1036-L1047）。这正是 u3-l1 讲过的「flatten 维度与 C×H×W 精确匹配」约束在真实 notebook 里的落点。

### 4.3 流水线并行

#### 4.3.1 概念说明

数据并行要求**每张卡装得下完整模型**。如果模型大到单卡放不下（或激活大到单卡 batch 只能开到 1），就要反过来**切模型**：把层序列切成若干段（stage），每段驻留一张卡，数据像流过流水线一样依次经过各卡。`model-pipeline-vgg16.ipynb` 演示的是 PyTorch 1.8 加入的实验性 `torch.distributed.pipeline.sync.Pipe`。

流水线并行的头号敌人是**气泡（bubble）**：如果只有一个 batch，那么 stage 1 计算时 stage 2..N 全在空等，串行毫无收益。解法是把一个 minibatch 切成 m 个 **microbatch**，让它们交错灌入流水线——stage 2 处理 microbatch 1 时，stage 1 已在处理 microbatch 2。经典（GPipe 式）的气泡占比公式为：

\[
\text{bubble 占比} = \frac{p-1}{m+p-1}
\]

其中 \(p\) 是 stage 数（本 notebook 把 VGG-16 切成 6 段：block_1..block_5 + classifier，\(p=6\)），\(m\) 是 microbatch 数（`Pipe(..., chunks=8)`，\(m=8\)）。\[ \frac{6-1}{8+6-1} = \frac{5}{13} \approx 38.5\% \] 的时刻存在空等——**m 越大越接近满载，但 m 受通信与调度开销限制**。

必须先说清本 notebook 的定位（notebook 自己的结尾也是这么交代的，[L1039](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/model-pipeline-vgg16.ipynb#L1039)）：**流水线并行的主卖点是用更多卡绕开显存约束，不是提速**；实测反而更慢。另外 `torch.distributed.pipeline` 是实验性 API，在较新的 PyTorch 版本中已被移除，复现本 notebook 需要旧的 PyTorch 1.8/1.9 环境（具体可运行版本「待本地验证」）。因此本模块以**读懂数据结构与执行模型**为目标，不要求跑通。

#### 4.3.2 核心流程

流水线执行模型（前向）示意，p=2、m=4 的最简情形：

```text
时间 ──────────────────────────────────────────▶
stage 0:  [F(mb1)] [F(mb2)] [F(mb3)] [F(mb4)]  idle...
stage 1:   idle   [F(mb1)] [F(mb2)] [F(mb3)] [F(mb4)]
                    ↑ mb1 从卡0 流到卡1：同一时刻两卡都在干活
```

整个 notebook 的执行链条：

```text
1) 单卡基线：VGG16 整模型放 cuda:0 训练（作为对照）
2) 拆块：block_1..block_5、classifier 六个独立 Sequential
3) 环境准备：torch.distributed.is_available() 检查
   → 设置 MASTER_ADDR / MASTER_PORT 环境变量
   → torch.distributed.rpc.init_rpc(name='node1', rank=0, world_size=1)
4) 装配：先把各 block .cuda(n) 放到不同卡 → 依序装入一个 Sequential
   → model_parallel = Pipe(sequential, chunks=8)
5) 训练：train_classifier_simple_v1(model=model_parallel, device=cuda:0)
   —— 训练循环仍是 u4-l4 那套共享工具，接口不变
```

#### 4.3.3 源码精读

**(a) 单卡基线：与流水线版对照的参照物**

[model-pipeline-vgg16.ipynb:L154-L295](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/model-pipeline-vgg16.ipynb#L154-L295) 定义常规版 `VGG16`（CIFAR-10 版：32×32 输入经五次池化到 1×1，classifier 是 `Flatten + Linear(512,4096) + ...`，与 CelebA 版的 `512*4*4` 输入不同——再次体现「输入尺寸决定分类头」），L291-L295 把它整体放到 `cuda:0` 并建 Adam 优化器。L562-L564 用 u4-l4 精读过的 `train_classifier_simple_v1` 训练 50 个 epoch——注意 [L67-L72](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/model-pipeline-vgg16.ipynb#L67-L72) 的 `sys.path.insert(0, "..")`：notebook 位于 `mechanics/` 子目录，靠它向上找到 `pytorch_ipynb/` 里的共享 helper 模块；数据来自 [L123-L126](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/model-pipeline-vgg16.ipynb#L123-L126) 的 `get_dataloaders_cifar10(batch_size=128, num_workers=2, validation_fraction=0.1)`。

**(b) 拆块：六个独立 Sequential**

[model-pipeline-vgg16.ipynb:L588-L717](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/model-pipeline-vgg16.ipynb#L588-L717) 把 L154 类里的 `self.block_1..block_5` 与 `classifier` **原样复制**为六个顶层变量。流水线并行的第一件事就是「把模型拆成可独立放置的段」——VGG 的 block 式定义（u3-l3 讲过的配置化组织）让这一步变成体力活。

**(c) 分布式环境检查与 RPC 初始化**

[model-pipeline-vgg16.ipynb:L730](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/model-pipeline-vgg16.ipynb#L730) 的 `torch.distributed.is_available()` 应返回 `True`；[L753](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/model-pipeline-vgg16.ipynb#L753) 与 [L777](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/model-pipeline-vgg16.ipynb#L777) 用 `%env` 魔法设置 `MASTER_ADDR`（本机 IP）与 `MASTER_PORT=8891`；[L794-L800](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/model-pipeline-vgg16.ipynb#L794-L800) 以单机模式 `init_rpc(name='node1', rank=0, world_size=1)` 启动 RPC，`try/except` 容忍重复运行时的「Address already in use」。`Pipe` 基于 RPC 传输跨卡中间激活，所以这一步是前置条件。

**(d) 装配 Pipe：本讲最重要的「以代码为准」案例**

[model-pipeline-vgg16.ipynb:L818-L831](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/model-pipeline-vgg16.ipynb#L818-L831)：

```python
from torch.distributed.pipeline.sync import Pipe

block1 = block_1.cuda(0)
block2 = block_2.cuda(0)
block3 = block_3.cuda(2)
block4 = block_4.cuda(2)     # 注意：block4 在下面被连续覆盖两次
block4 = block_5.cuda(3)
block4 = classifier.cuda(0)

model_parallel = torch.nn.Sequential(
    block_1, block_2, block_3, block_4, block_5, classifier)
model_parallel = Pipe(model_parallel, chunks=8)
optimizer = torch.optim.Adam(model_parallel.parameters(), lr=learning_rate)
```

这段代码有两层值得拆穿的事实：

- **变量名陷阱**：`block1/block2/block3` 这三个小写变量从未被使用；`block4` 被赋值三次，最终指向 `classifier`。但 Sequential 装入的是**原始变量** `block_1..block_5, classifier`，所以这些名字错乱不影响装配的正确性。
- **`.cuda()` 是原地操作**：`nn.Module.cuda()` 把模块参数就地搬到目标卡并返回 `self`。因此 `block_3.cuda(2)` 虽然把返回值赋给了没人用的 `block3`，`block_3` 本身的参数已经搬到 `cuda:2` 了。最终六个 stage 的实际驻留是：block_1→cuda:0、block_2→cuda:0、block_3→cuda:2、block_4→cuda:2、block_5→cuda:3、classifier→cuda:0（`cuda:1` 全程空闲）。若误以为「Sequential 里装的是没搬过的模块」，会得出全在 CPU 的错误结论。`Pipe` 依据各模块当前所在设备自动切分 stage，`chunks=8` 即把每个 batch（128）切成 8 个 microbatch（每个 16）注入流水线。

**(e) 训练与结论：更慢，但省显存**

[model-pipeline-vgg16.ipynb:L1029-L1031](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/model-pipeline-vgg16.ipynb#L1029-L1031) 仍用 `train_classifier_simple_v1`，只是 `model=model_parallel`、`device=torch.device('cuda:0')`——**对训练循环而言，Pipe 包装后的模型与普通模型接口一致**（这一点与 DataParallel 相同）。[L1039](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/model-pipeline-vgg16.ipynb#L1039) 的结论 markdown 说得很坦率：训练比单卡更慢是预期内的，流水线并行的卖点是「在显存受限时利用更多 GPU」，不是加速。本 notebook 的运行输出为空，未保存多卡实测日志——多卡实验结果「待本地验证」。

#### 4.3.4 代码实践

**实践目标**：不求跑通（需多卡 + 旧版 PyTorch），通过画图与计算把流水线执行模型内化。

**操作步骤**：

1. **画时空图**：在纸上或表格里画 p=6、m=8 的前向时空图（行=stage 0..5，列=时间片），按「microbatch i 在 stage j 的开始时刻 = i+j」填充（假设各 stage 各 microbatch 等耗时）。数一数多少时间片是空的。
2. **算气泡**：用公式 \( \frac{p-1}{m+p-1} \) 计算气泡占比，与图中空白格占比对照；再算 m=2、4、16、64 时的占比，观察 m 增大时占比如何趋近 0。
3. **标注设备流**：对照 L818-L831 的代码，在时空图左侧标注每个 stage 实际驻留的设备（0,0,2,2,3,0），指出哪张卡完全空闲。
4. （可选，有旧版 torch 1.8 环境）按 L730-L800 的顺序跑环境准备，确认 `is_available()` 返回 True、`init_rpc` 不报错——这步单机即可完成，不需要多卡。

**需要观察的现象**：时空图呈「阶梯形」——前 5 个时间片流水线在逐渐灌满（fill），最后 5 个时间片在逐渐排空（drain），只有中间段是 6 卡全忙；m 翻倍时气泡占比近似减半（5/13≈38.5% → 5/19≈26.3%）。

**预期结果**：手算与图解一致即达标；RPC 初始化行为因环境而异，「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：本 notebook 中 `Pipe(model_parallel, chunks=8)`、batch_size=128，每个 microbatch 含多少样本？若想把气泡占比压到 20% 以下，m 至少取多少？

**参考答案**：128/8=16 个样本。由 \( \frac{6-1}{m+5} \le 0.2 \) 解得 \( m \ge 20 \)，即 chunks 至少 21（microbatch 数为整数且越多气泡越小）。注意 microbatch 太小会重复承担 kernel 启动与通信开销，实际要权衡。

**练习 2**：如果删掉 L823-L828 的六行 `.cuda()` 调用、直接 `Pipe(torch.nn.Sequential(block_1, ..., classifier), chunks=8)`，会发生什么？

**参考答案**：六个模块全部留在 CPU，`Pipe` 将把它们划入同一个（CPU）stage，等于没有做任何跨卡切分——流水线并行退化为普通的 CPU Sequential（且 Pipe 对设备放置有要求，可能直接报错）。这段代码再次印证：`nn.Module.cuda()` 是原地移动参数，赋值给谁只是决定返回值别名，真正生效的是「模块自身被搬到了哪张卡」。

**练习 3**：数据并行和流水线并行可以同时用吗？什么场景下需要？

**参考答案**：可以，经典大模型训练通常「流水线/张量并行切模型 + 数据并行复制多份」。当单份模型需要多卡才装得下时，先用模型并行（如本 notebook的 Pipe）把一份模型摊到几张卡上，再把这套流水线复制到多组卡上做数据并行。本仓库只演示了两种技术各自的最小形态，组合用法属于 PyTorch 官方 `torch.distributed` 与后续生态（如 DeepSpeed、Megatron）的范畴。

## 5. 综合实践

**任务：为「单卡读者」制作一份多 GPU 训练方案说明书。**

以 `cnn-vgg16-celeba-data-parallel.ipynb` 为对象，完成三件事：

1. **注释版数据流图**：绘制从 `train_loader` 取出一个 256 样本 batch 开始，到 `optimizer.step()` 结束的完整数据流图，标出每一步发生在哪张卡（用 4 卡、DataParallel 场景），并注明拆分/汇总发生的维度与设备。要求覆盖：scatter、replicate、各卡前向、gather、主卡损失、各卡反传、梯度 reduce、主卡 step。
2. **决策清单**：假设你要在「2×24 Gb 显存、模型 5 Gb、单卡最多 batch=48」的环境下训练，写出选择数据并行还是流水线并行的判断依据与 batch 设置建议（提示：每卡装得下完整模型时优先数据并行；模型或激活装不下才考虑切模型）。
3. **气泡核算**：对 pipeline notebook 的 6-stage、chunks=8 配置计算气泡占比，并给出把占比压到 25% 的最小 chunks 值（答案：m=15，因 \( \frac{5}{m+5} \le 0.25 \Rightarrow m \ge 15\)）。

完成后自查：能否不看讲义向同伴解释「为什么 DataParallel 下 BN 要看每卡子批大小」「为什么 Pipe 训练反而更慢」。

## 6. 本讲小结

- 多卡训练有两个不同动机：**数据并行**（切 batch，求吞吐）与**模型/流水线并行**（切模型，求显存）；`nn.DataParallel` 是前者的最简实现，PyTorch 1.8 的实验 API `Pipe` 是后者的最小演示。
- DataParallel 的机制是 scatter → replicate → 并行前向 → gather 回主卡 → 主卡算损失（损失在模型外定义）→ 各卡反传 → 梯度 reduce → 主卡 step；对训练循环完全透明，`model(features)` 一行之内完成所有并行逻辑。
- 两条硬约束：输入数据必须先在 `cuda:0`（否则 `all tensors must be on devices[0]`）；含 BatchNorm 的网络每卡子批不能太小（BN 统计逐卡独立）——ResNet-18 版把每卡基准提到 256 的原因。
- batch 随卡数放大的正确姿势是 `BATCH_SIZE=64*torch.cuda.device_count()`：总 batch 变大、每卡显存压力不变；作者实测 4 卡（1080Ti）batch×4 后约 3 倍速。
- CelebA 准备流程 = 两个官方 txt 清单 merge 成「文件名 → 标签+划分」合并表，再按 Partition 标记导出三个 CSV；划分由官方规定、无需打乱，产物交给三要素 Dataset 模板消费。
- 流水线并行气泡占比 \( \frac{p-1}{m+p-1} \)：本 notebook 6 stage、chunks=8 时约 38.5%；它更慢但能装下更大的模型。两处真实代码细节再次验证「读代码以代码为准」：`num_features` 形参未被使用、`block4` 连续被覆盖但 `.cuda()` 原地生效。

## 7. 下一步学习建议

- 下一讲 u8-l6 转向图神经网络（`gnn-basic-1.ipynb` 等），把 MNIST 像素当图节点做消息传递——与本章的工程化主题互补，回到模型结构创新。
- 想继续深挖多卡训练的读者：本仓库停留在 `DataParallel` 与实验性 `Pipe`（torch 1.0/1.8 时代）。现代 PyTorch 的推荐方案是 `DistributedDataParallel`（多进程、无 GIL 瓶颈，官方文档明确建议替代 DataParallel）与 `torch.distributed` 系列原语，可对照 u8-l1/u8-l2 的 Lightning `Trainer`（其 `devices`/`accelerator` 参数内部即封装 DDP）建立概念映射。
- 复习建议：回到 u4-l4 重读 `train_classifier_simple_v1`，体会「共享训练工具链让 pipeline notebook 只需换模型对象」的工程价值；回到 u8-l4 对比「梯度检查点（单卡省显存）」与「流水线并行（多卡省显存）」——两者都牺牲时间换显存，适用条件不同。
