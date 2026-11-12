# 评估工具链：准确率、混淆矩阵与失败案例

## 1. 本讲目标

学完本讲，你应该能够：

1. 用仓库共享的 `compute_confusion_matrix` 计算混淆矩阵，并用 `plot_confusion_matrix` 把它画成带数字标注的热力图，从矩阵直接读出「模型最常把谁认成谁」。
2. 用 `show_examples` 展示一批「预测值 vs 真实值」对照的样本图片，并在此基础上编写自己的「只展示误分类样本」函数。
3. 说清 torchmetrics 中 `.update()` 与 `.forward()`（即 `__call__`）两种调用方式的语义差异：为什么前者返回 `None`、后者返回当前 batch 的指标值，而两者最终的 `.compute()` 结果完全一致；并掌握「分子/分母分别累计」这一指标累计的正确姿势。

本讲是第 7 单元（训练评估与技巧）的第一讲。上一单元你已经把训练循环交给了 `helper_train` 的共享工具链（u4-l4），本讲把「训练完之后怎么评估、怎么诊断错误」这块拼图补齐。

## 2. 前置知识

本讲默认你已完成 u4-l4（公共训练评估工具链与确定性），以下概念直接沿用，不再重复展开：

- **五步训练范式**：forward → loss → zero_grad → backward → step；评估阶段则统一在 `model.eval()` + `torch.no_grad()` 下进行。
- **`compute_accuracy` / `compute_epoch_loss`**：`helper_evaluate.py` 中的一对评估函数，前者内部自带 `eval()` 与 `no_grad()`，后者用 `reduction='sum'` 先求和再除以样本数。
- **`log_dict` 与最优 checkpoint**：`train_classifier_simple_v1/v2` 返回的训练日志；报告成绩前应重载最优 checkpoint。
- **`UnNormalize`**（u4-l3）：标准化张量直接画图会失真，须乘 `std` 加 `mean` 还原到 [0,1] 再交给 matplotlib。

本讲新引入的术语，先用一段话建立直觉：

- **混淆矩阵（confusion matrix）**：一个 K×K 的表格，第 i 行第 j 列存放「真实标签为 i、被预测成 j」的样本数。对角线是预测正确的样本，非对角线格子则是「失败案例」的聚集地。准确率只告诉你「错了多少」，混淆矩阵还告诉你「错成了什么」。
- **行归一化（row normalization）**：把每一行除以该行总和，得到「真实类别 i 的样本中被预测为 j 的比例」。对角线上的归一化值就是类别 i 的召回率（recall）。
- **torchmetrics**：一个把「指标累计」封装成对象的库。你创建一个 `Accuracy()` 对象，逐 batch 喂数据，最后一次性 `.compute()`。它的核心价值是：不需要把所有预测堆在内存里，只需要累计少量统计量（正确数、样本数）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pytorch_ipynb/helper_evaluate.py` | 评估指标函数库：`compute_accuracy`、`compute_epoch_loss`、`compute_confusion_matrix` |
| `pytorch_ipynb/helper_plotting.py` | 可视化函数库：`plot_training_loss`、`plot_accuracy`、`show_examples`、`plot_confusion_matrix` |
| `pytorch_ipynb/helper_data.py` | `UnNormalize` 类所在的共享模块，`show_examples` 的反标准化参数由它构造（本讲只用到这一个类） |
| `pytorch_ipynb/related-libraries/torchmetrics-update-forward.ipynb` | 作者写的 torchmetrics 专题 notebook：用 10 个模拟 minibatch 对比手工计算、`.update()`、`.forward()` 三种方式 |
| `pytorch_ipynb/cnn/cnn-mobilenet-v3-small-cifar10.ipynb` | 仓库中真实调用这套评估工具链的范例 notebook，本讲引用它的 Evaluation 段作为「标准用法」 |

注意引用方式：这些 notebook 都通过 `sys.path.insert(0, "..")` 把 `pytorch_ipynb/` 目录加入搜索路径后再 `from helper_evaluate import ...`（[pytorch_ipynb/cnn/cnn-mobilenet-v3-small-cifar10.ipynb:L71-L79](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v3-small-cifar10.ipynb#L71-L79)）。你在自己的 notebook 里复现时也要保证能找到这两个 helper 文件。

## 4. 核心概念与源码讲解

本讲的三个最小模块：**混淆矩阵**、**样本展示工具**、**torchmetrics 指标**。

### 4.1 混淆矩阵：从 `compute_confusion_matrix` 到 `plot_confusion_matrix`

#### 4.1.1 概念说明

准确率是一个把所有信息压缩成单个数字的指标：\[ \text{Accuracy} = \frac{1}{N}\sum_{n=1}^{N} \mathbb{1}[\hat{y}_n = y_n] \] 它回答「平均对了多少」，但掩盖了错误的结构。比如 MNIST 上 97.5% 的准确率，剩下的 2.5% 错误可能集中在「4 认成 9」「3 认成 5」这几对形近数字上——这正是改进模型时最该先知道的信息。

混淆矩阵把这些错误结构完整保留下来：\[ C_{ij} = \sum_{n=1}^{N} \mathbb{1}[y_n = i,\ \hat{y}_n = j] \] 约定**行是真实标签、列是预测标签**（仓库的 `plot_confusion_matrix` 在坐标轴上明确标注了这一点）。于是：

- 对角线元素之和 = 预测正确的样本总数，除以 N 就是准确率——混淆矩阵是准确率的「无损上游」。
- 每一行除以行和得到行归一化矩阵 \(\tilde{C}_{ij} = C_{ij} / \sum_j C_{ij}\)，其第 i 行对角线值即类别 i 的召回率。

#### 4.1.2 核心流程

`compute_confusion_matrix(model, data_loader, device)` 的执行过程：

1. 准备两个空列表 `all_targets`、`all_predictions`。
2. 在 `torch.no_grad()` 下遍历整个 data_loader：前向得到 logits，`torch.max(logits, 1)` 取预测标签，把 targets 与预测标签逐 batch 搬到 CPU 后 `extend` 进列表。
3. 两个列表转成 NumPy 数组。
4. 用 `np.unique` 在「真实标签 ∪ 预测标签」上取类别集合，处理「只出现一个类别」的退化情形。
5. 用 `itertools.product` 枚举所有 (真实, 预测) 类别组合，统计每对组合的出现次数。
6. 把长度为 K² 的计数列表 reshape 成 K×K 矩阵返回。

随后 `plot_confusion_matrix(conf_mat, ...)` 负责画图：行归一化 → `matshow` 热力图 → 每格写上绝对计数（可选加归一化比例）→ 贴类别名。

#### 4.1.3 源码精读

**第一步：收集全部预测。** [pytorch_ipynb/helper_evaluate.py:L43-L55](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_evaluate.py#L43-L55) 中，函数在 `no_grad` 下逐 batch 前向，`torch.max(logits, 1)` 沿类别维取 argmax，随后把 targets 与预测标签都搬到 CPU 再 extend——注意这一步与 `compute_accuracy` 不同：混淆矩阵必须**保留每个样本的 (真实, 预测) 对**，不能在循环内就地累加，所以这里选择了「先收集、后统计」的方案。

**第二步：确定类别集合与退化处理。** [pytorch_ipynb/helper_evaluate.py:L57-L67](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_evaluate.py#L57-L67) 把列表转 NumPy 数组，用 `np.unique(np.concatenate((all_targets, all_predictions)))` 取类别标签。两个细节值得注意：

- 类别集合取自「真实 ∪ 预测」的并集，因此矩阵维度由实际出现的标签决定，而不是硬编码 10；
- 若并集里只有一个类别（比如二分类任务恰好全部预测正确且只出现一个标签），L62-L66 会补一个 0 或 1 凑成 2×2，避免产出 1×1 的「假矩阵」。

**第三步：暴力计数。** [pytorch_ipynb/helper_evaluate.py:L68-L73](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_evaluate.py#L68-L73) 是这个函数最有教学价值的一段：先把 (target, prediction) 打包成元组列表 `z`，再用 `product(class_labels, repeat=2)` 按行主序枚举所有 (i, j) 组合，用 `z.count(combi)` 数出现次数，最后 reshape 成 K×K。`product` 的枚举顺序是 (c₀,c₀), (c₀,c₁), …, (c₁,c₀), …，与 reshape 的行主序一致，所以**第 i 行对应真实标签 i、第 j 列对应预测标签 j**。这种写法是 O(K²·N) 的暴力扫描（K 是类别数），对 MNIST/CIFAR 的 10 类、万级样本完全够用；生产环境通常会换成一维 `np.bincount` 式向量化计数，但教学上「直接数元组出现次数」最透明。

**第四步：画图。** [pytorch_ipynb/helper_plotting.py:L130-L137](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_plotting.py#L130-L137) 先做参数断言（两种显示模式不能同时关闭；`class_names` 长度必须等于矩阵维度），随后 `conf_mat.sum(axis=1)[:, np.newaxis]` 沿**行方向**求和——每行总和即真实类别 i 的样本数——再相除得到行归一化矩阵。也就是说，归一化基准是「每个真实类别的样本数」，归一化后的第 i 行是类别 i 的样本被分到各类的比例。

[pytorch_ipynb/helper_plotting.py:L147-L171](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_plotting.py#L147-L171) 决定「画什么、写什么」：`show_normed` 控制用绝对计数还是归一化矩阵喂给 `matshow`（默认 `plt.cm.Blues` 色表）；双重循环给每个格子写注释——绝对计数总是整数格式，若同时开启 `show_absolute` 与 `show_normed` 则在数字下方追加括号里的两位小数比例。L171 有个贴心细节：格子归一化值超过 0.5（底色已很深）时文字用白色，否则用黑色，保证任何底色上文字都可读。

**第五步：坐标轴与返回值。** [pytorch_ipynb/helper_plotting.py:L173-L191](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_plotting.py#L173-L191) 用 `class_names` 设置刻度（x 轴刻度旋转 90 度），最后 `plt.xlabel('predicted label')` / `plt.ylabel('true label')` 明确了行列语义，并 `return fig, ax`——返回画布对象而不是内部 `plt.show()`，方便调用方进一步定制或保存。

**仓库中的标准用法**（MobileNet-v3-small 在 CIFAR-10 上的 Evaluation 段）：[pytorch_ipynb/cnn/cnn-mobilenet-v3-small-cifar10.ipynb:L1289-L1291](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v3-small-cifar10.ipynb#L1289-L1291) 先 `model.cpu()`，再一行算矩阵、一行画图：

```python
mat = compute_confusion_matrix(model=model, data_loader=test_loader, device=torch.device('cpu'))
plot_confusion_matrix(mat, class_names=class_dict.values())
plt.show()
```

#### 4.1.4 代码实践

**实践目标**：验证「混淆矩阵的对角线之和除以总数 = `compute_accuracy` 的返回值」，亲手确认混淆矩阵是准确率的无损上游。

**操作步骤**：

1. 挑一个你已经训练好的模型（推荐复用 u2-l3 的 `mlp-basic` 或 u3-l1 的 `cnn-basic`，任一 MNIST 模型均可），把本讲源码地图中的 import 段抄到 notebook 里。
2. 运行下面的对账代码（示例代码）：

```python
import torch, numpy as np
from helper_evaluate import compute_accuracy, compute_confusion_matrix
from helper_plotting import plot_confusion_matrix
import matplotlib.pyplot as plt

test_acc = compute_accuracy(model, test_loader, device)          # 百分比
mat = compute_confusion_matrix(model, test_loader, device)       # 10x10 numpy 数组

acc_from_mat = np.trace(mat) / mat.sum() * 100
print(f'compute_accuracy : {test_acc:.2f}%')
print(f'对角线/总数     : {acc_from_mat:.2f}%')

plot_confusion_matrix(mat, class_names=[str(i) for i in range(10)],
                      show_normed=True, figsize=(8, 8))
plt.show()
```

3. 找出混淆最严重的一对类别：对非对角线元素做 `np.unravel_index(mat.argmax() ...)` 前先把对角线置零（示例代码）：

```python
off_diag = mat.copy()
np.fill_diagonal(off_diag, 0)
i, j = np.unravel_index(off_diag.argmax(), off_diag.shape)
print(f'最常混淆：真实 {i} → 预测 {j}，共 {off_diag[i, j]} 个样本')
```

**需要观察的现象**：两个准确率数字完全一致（浮点误差内）；`show_normed=True` 的图上每个格子显示「计数 + 括号比例」，对角线括号值即各类召回率。

**预期结果**：MNIST 上 MLP/CNN 的混淆矩阵高度对角占优，最混淆的格子通常落在 (4,9)、(3,5)、(7,2) 这类形近数字对上；对角线和 / 总数与 `compute_accuracy` 一致。

**待本地验证**：具体哪一对类别混淆最多取决于你的训练运行，请以本地结果为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `compute_confusion_matrix` 必须把所有预测收集完再统计，而 `compute_accuracy` 可以在循环内边走边加？

**答案**：准确率只需要两个标量——累计正确数与累计样本数，循环内 `correct_pred += (...)sum()` 即可完成 O(1) 内存累计。混淆矩阵需要 K² 个格子的计数，其中每个计数依赖「真实 i 且预测 j」的联合事件，理论上也可以循环内维护 K×K 张量逐个 `+=`，但仓库实现选择了「先收集 (target, pred) 对再统一计数」的写法，用内存换代码透明度；这也正是 4.3 节 torchmetrics 用「指标状态」把这类累计封装起来的动机。

**练习 2**：`plot_confusion_matrix(mat, show_normed=True, show_absolute=False)` 与 `show_absolute=True, show_normed=True` 画出的图有何区别？归一化的分母是什么？

**答案**：前者只在格子里显示两位小数的比例、热力图底色用归一化矩阵；后者格子显示整数计数（下面附括号比例）、底色仍用绝对计数（L147-L150 只有 `show_normed` 单独为 True 时才用归一化矩阵喂 `matshow`）。归一化分母是 `conf_mat.sum(axis=1)`，即每个**真实类别**的样本总数（行和），所以第 i 行对角线比例就是类别 i 的召回率。

**练习 3**：如果测试集的 10 个类别中类别 6 一次也没被预测到，`compute_confusion_matrix` 返回的矩阵还是 10×10 吗？

**答案**：不一定。类别集合来自 `np.unique(concatenate((all_targets, all_predictions)))`（L61）——只要类别 6 作为**真实标签**出现过，它就保留在并集里，矩阵仍是 10×10，只是「预测为 6」的那一列全为零；但若某个类别既不在真实标签也不在预测中出现，它会被 `np.unique` 剔除，矩阵维度随之缩小。极端情形（并集只有一个类别）由 L62-L66 补齐成 2×2。

### 4.2 样本展示工具：`show_examples` 与失败案例

#### 4.2.1 概念说明

混淆矩阵告诉你「数字 4 被认成 9 有 23 次」，但它没回答下一个自然的问题：**那 23 张图长什么样？** 是书写潦草，还是模型真的瞎了？把误分类样本连同预测值、真实值一起画出来，是图像分类调试中最直接的一步——这就是「失败案例分析」。

仓库为此提供了 `show_examples`：从 data_loader 取**第一个 batch**，逐样本画图，标题写成 `P: 预测 / T: 真实` 两行。它本身**不做任何筛选**——对错混排，靠标题对照肉眼找错；要「只看误分类样本」，需要你在它基础上做一个小改造（见 4.2.4 实践）。

#### 4.2.2 核心流程

1. 遍历 data_loader，取第一个 batch 就 `break`；
2. 在 `no_grad` 下前向，`torch.argmax(logits, dim=1)` 得到预测标签；
3. 若传入 `unnormalizer`，对 batch 内每张图逐样本反标准化（乘 std 加 mean）；
4. `np.transpose(features, (0, 2, 3, 1))` 把 NCHW 转成 NHWC——matplotlib 的 `imshow` 期待通道在最后；
5. 按 3×5 的子图网格画 15 张图：单通道图走灰度分支（`cmap='binary'`），RGB 图走彩色分支；
6. 每张图标题写 `P: xxx / T: xxx`（传入 `class_dict` 时显示类别名，否则显示数字索引）。

#### 4.2.3 源码精读

**只取第一个 batch。** [pytorch_ipynb/helper_plotting.py:L77-L84](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_plotting.py#L77-L84) 的 for 循环体末尾有一个孤零零的 `break`——整个函数只消费 data_loader 的第一个 batch。这决定了它的两个使用特征：展示的样本取决于 loader 的取样顺序（测试 loader 通常不 shuffle，展示的是数据集开头的一批）；且第一个 batch 必须至少有 15 个样本，否则后面 `enumerate(axes.ravel())` 按 15 个子图索引会越界。仓库 notebook 的 batch_size 普遍远大于 15，所以不曾触发。

**反标准化与通道转换。** [pytorch_ipynb/helper_plotting.py:L86-L92](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_plotting.py#L86-L92) 建 3×5 子图网格后，若给了 `unnormalizer` 就逐样本调用（就是 u4-l3 讲过的 `UnNormalize`，每个通道 `t * std + mean`），再转置维度。没有这一步，标准化后的张量（均值 0、有负值）直接 `imshow` 会得到一张过曝失真的图。

**灰度/彩色双分支与标题。** [pytorch_ipynb/helper_plotting.py:L94-L104](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_plotting.py#L94-L104) 处理单通道（MNIST）情形：先 `np.squeeze(..., axis=3)` 压掉大小为 1 的通道维，`cmap='binary'` 画灰度，标题里 `class_dict[predictions[idx].item()]` 把张量里的整数标签翻译成类别名——注意 `.item()` 把 0 维张量转成 Python int 用于字典查找。RGB 分支（L106-L115）逻辑相同只是少了 squeeze、`imshow` 不传 cmap。

**仓库中的标准用法。** [pytorch_ipynb/cnn/cnn-mobilenet-v3-small-cifar10.ipynb:L1267-L1280](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v3-small-cifar10.ipynb#L1267-L1280) 演示了完整调用：`model.cpu()` 后构造 CIFAR-10 的 `UnNormalize((0.5,0.5,0.5), (0.5,0.5,0.5))`（该 notebook 的标准化参数是均值 0.5、标准差 0.5）与 `class_dict` 字典，然后 `show_examples(model=model, data_loader=test_loader, unnormalizer=unnormalizer, class_dict=class_dict)`。CIFAR-10 的 transform 在这个 notebook 里挂在 `get_dataloaders_cifar10` 内部，所以反标准化参数要与数据管线里实际用的 Normalize 参数严格互逆。

#### 4.2.4 代码实践

**实践目标**：编写 `show_misclassified`——从测试集中筛出**前 16 个误分类样本**，画成 4×4 网格（`show_examples` 固定 3×5=15 张且不筛选，本实践是它的「失败案例版」改造）。

**操作步骤**：

1. 在训练好模型之后，加入下面的示例代码（改造自 `show_examples`，核心是收集 `predicted_labels != targets` 的样本）：

```python
# 示例代码：只展示误分类样本（4.2 节实践）
import torch, numpy as np
import matplotlib.pyplot as plt
from helper_data import UnNormalize

def show_misclassified(model, data_loader, unnormalizer=None,
                       class_dict=None, max_count=16):
    mis_imgs, mis_preds, mis_targets = [], [], []
    model.eval()
    with torch.no_grad():
        for features, targets in data_loader:          # 扫完整个测试集
            logits = model(features)
            preds = torch.argmax(logits, dim=1)
            mask = preds != targets                    # 布尔掩码：错分的样本
            for img, p, t in zip(features[mask], preds[mask], targets[mask]):
                if len(mis_imgs) < max_count:
                    mis_imgs.append(img)
                    mis_preds.append(int(p))
                    mis_targets.append(int(t))
            if len(mis_imgs) >= max_count:
                break
    n = len(mis_imgs)
    n_cols = 4
    n_rows = int(np.ceil(n / n_cols))
    fig, axes = plt.subplots(nrows=n_rows, ncols=n_cols,
                             figsize=(2.2*n_cols, 2.4*n_rows))
    features = torch.stack(mis_imgs)
    if unnormalizer is not None:
        for idx in range(features.shape[0]):
            features[idx] = unnormalizer(features[idx])
    nhwc_img = np.transpose(features, axes=(0, 2, 3, 1))
    for idx, ax in enumerate(axes.ravel()):
        if idx >= n:
            ax.axison = False
            continue
        img = nhwc_img[idx]
        if img.shape[-1] == 1:
            ax.imshow(img.squeeze(-1), cmap='binary')
        else:
            ax.imshow(img)
        ax.set_title(f'P: {class_dict[mis_preds[idx]]}\nT: {class_dict[mis_targets[idx]]}')
        ax.axison = False
    plt.tight_layout()
    plt.show()

unnormalizer = UnNormalize((0.5,), (0.5,))   # 按你的 Normalize 参数修改
class_dict = {0: '0', 1: '1', 2: '2', 3: '3', 4: '4',
              5: '5', 6: '6', 7: '7', 8: '8', 9: '9'}
show_misclassified(model, test_loader, unnormalizer, class_dict)
```

2. 若你的数据管线没有做 Normalize（例如直接用 ToTensor），传 `unnormalizer=None` 即可。
3. 再调用一次原生 `show_examples(model, test_loader)`，对比「对错混排」与「只看错误」两种视角。

**需要观察的现象**：4×4 网格里每张图的标题都是 `P ≠ T`；灰度数字图中被认错的样本往往字形倾斜、笔画粘连，或带有断裂。

**预期结果**：16 张图全部是误分类样本，且错误对（如 P:9/T:4）与 4.1 节混淆矩阵中最大的非对角线格子对应得上。

**待本地验证**：具体是哪些样本被错分取决于你训练出的模型权重。

#### 4.2.5 小练习与答案

**练习 1**：`show_examples` 为什么不能直接用来展示「误分类样本」？

**答案**：它的循环里没有任何筛选逻辑——取第一个 batch 后对所有样本一律画图（L77-L104），对错混排只能靠标题肉眼分辨；而且只看第一个 batch，若开头这批恰好全对，一张失败案例都看不到。要专门看错误，必须像 4.2.4 那样加 `preds != targets` 掩码并扫多个 batch。

**练习 2**：`np.transpose(features, axes=(0, 2, 3, 1))` 这一步如果省略，直接 `ax.imshow(features[idx])` 会发生什么？

**答案**：PyTorch 图像 batch 是 NCHW（通道维在第 1 位），而 matplotlib 的 `imshow` 期待 HWC（通道在最后）。省略转置时，对单通道图 `imshow` 收到的是 (1, 28) 的形状，画出来的是转置的细条；对 RGB 则是 (3, 32) 的形状且 imshow 会按 2D 数组配默认色表渲染，完全不是原图。灰度分支里随后的 `np.squeeze(..., axis=3)` 也依赖转置后通道在最后这个前提。

**练习 3**：`show_examples` 里为什么必须传 `unnormalizer` 才能正常显示 CIFAR-10 图片，而显示 MNIST 时经常可以不传？

**答案**：是否需要反标准化取决于数据管线是否做了 `Normalize`。`imshow` 把数值线性映射到色阶，标准化后张量以 0 为中心、含负值，画出来会失真（过暗/过曝）。CIFAR-10 的 notebook 标准化了（均值 0.5、标准差 0.5），所以必须乘 std 加 mean 还原回 [0,1]；而不少 MNIST notebook 只用 ToTensor（像素天然落在 [0,1]），没有 Normalize 也就不需要反标准化，`unnormalizer=None` 即可。

### 4.3 torchmetrics 指标：`update()` 与 `forward()` 的语义差异

#### 4.3.1 概念说明

前两节的 helper 函数都是「自己写循环、自己累计」。torchmetrics 把这套模式**对象化**了：一个 `Accuracy()` 对象内部维护累计状态（正确数、样本数），你对它逐 batch 喂数据，最后 `.compute()` 一次性取结果。好处是不用手写累计变量，且天然兼容 PyTorch Lightning 的日志体系。

但 torchmetrics 的 metric 对象有两种「喂法」，初学者常困惑：

- `.update(y_true, y_pred)`：只累计，**返回 `None`**；
- `.forward(y_true, y_pred)`，等价于直接 `metric(y_true, y_pred)`：累计之余，还**返回当前 batch 的指标值**。

仓库的专题 notebook `torchmetrics-update-forward.ipynb` 用一个 10-batch 的模拟实验把两者的关系讲透了。理解它的钥匙是一条更根本的原则——**指标累计的正确姿势是分子/分母分别累计**：\[ \text{acc} = \frac{\sum_b \text{correct}_b}{\sum_b n_b} \] 而不是「先算每个 batch 的准确率再取平均」。当各 batch 大小相同时两者数值巧合相等；一旦最后一个 batch 不满（drop_last=False 时很常见），「批均值的均值」会给小 batch 与大 batch 同等权重，产生偏差。

#### 4.3.2 核心流程

notebook 的实验设计：固定 `torch.manual_seed(123)`，用 `torch.randint` 模拟 10 个 batch，每 batch 10 个 0/1 标签，总共 100 个样本。三条路线对照：

1. **手工基线 A（收集后统一算）**：把 100 个真实标签、100 个预测全部存进列表，`torch.cat` 后比较取均值。
2. **手工基线 B（分子/分母累计）**：循环内 `correct += (y_true == y_pred).float().sum()`、`num += y_true.numel()`，最后相除——这正是 `compute_accuracy` 的做法。
3. **torchmetrics**：`update()` 或 `forward()` 喂数据，`.compute()` 取结果。

`forward()` 的官方内部流程（notebook 引自 torchmetrics 文档）：一次 `forward` 调用会 ① `update()` 更新全局累计状态 → ② 缓存全局状态 → ③ `reset()` 清空 → ④ 对**当前 batch** 再 `update()` → ⑤ `compute()` 得到本 batch 指标 → ⑥ 恢复全局状态。代价是 `update` 被调用了两次，收益是既拿到 batch 值又不污染累计。

#### 4.3.3 源码精读

**手工基线 A：收集后统一算。** [pytorch_ipynb/related-libraries/torchmetrics-update-forward.ipynb:L83-L91](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/related-libraries/torchmetrics-update-forward.ipynb#L83-L91) 把 10 个 batch 的 `y_true`、`y_pred` 全部 append 进列表，循环外 `torch.cat` 拼接、逐元素比较后 `torch.mean`，得到总准确率 0.56。notebook 随即指出这种「全存下来」的做法在大数据集上浪费内存（最坏情况撑爆 GPU 显存）——这正是要换成累计式的原因。

**手工基线 B：分子/分母累计。** [pytorch_ipynb/related-libraries/torchmetrics-update-forward.ipynb:L131](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/related-libraries/torchmetrics-update-forward.ipynb#L131) 所在单元格在循环内做两件事：`correct += (y_true == y_pred).float().sum()`（分子，累计正确数）和 `num += y_true.numel()`（分母，累计样本数），循环外 `correct / num`。结果同样是 0.56，内存占用却是 O(1)。`compute_accuracy`（u4-l4）内部就是这一模式。

**`update()`：只累计，返回 None。** [pytorch_ipynb/related-libraries/torchmetrics-update-forward.ipynb:L186-L200](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/related-libraries/torchmetrics-update-forward.ipynb#L186-L200) 创建 `Accuracy()` 后在循环里 `abc = train_acc.update(y_true, y_pred)`，打印出来的 `abc` 十行全是 `None`——`update` 是纯粹的累计操作，没有返回值；循环结束后的 `train_acc.compute()` 才给出 0.56，与手工基线一致。

**`forward()`：累计 + 返回 batch 值。** [pytorch_ipynb/related-libraries/torchmetrics-update-forward.ipynb:L247-L252](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/related-libraries/torchmetrics-update-forward.ipynb#L247-L252) 把 `update` 换成 `abc = train_acc(y_true, y_pred)`（注释里写明等价于 `train_acc.forward(...)`）。这次每行打印出了 0.70、0.70、0.50、0.60……——当前 batch 的准确率；而最终的 `.compute()` 依旧是 0.56。也就是说 `forward` 的累计语义与 `update` 完全相同，只是额外附赠了 batch 级中间值。作者在 [L331](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/related-libraries/torchmetrics-update-forward.ipynb#L331) 给出选型建议：不需要中间值就用 `update()`；`forward` 的额外开销在深度学习训练的尺度上可忽略，默认用它也无妨。[L342](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/related-libraries/torchmetrics-update-forward.ipynb#L342) 起引用的官方文档摘录则解释了内部六步机制（见 4.3.2），`update` 因此被调用两次。

**Bonus：循环内 `compute()` 得到运行均值。** [pytorch_ipynb/related-libraries/torchmetrics-update-forward.ipynb:L395-L410](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/related-libraries/torchmetrics-update-forward.ipynb#L395-L410) 展示第三种组合：循环内 `update()` 之后立刻 `compute()`，打印的是「截至当前 batch 的累计准确率」——0.70、0.70、0.6333、0.6250、0.5800……单调收敛到 0.56。这不是 batch 值（batch 值是 0.5、0.4 这样跳变的），而是运行中的总体均值，适合只扫一遍数据就同时记录中间过程与最终值。

一个值得你自己推导的细节（本讲分析，非 notebook 原文）：本实验 10 个 batch 恰好等大（各 10 个样本），所以「10 个 batch 准确率取平均 = (0.7+0.7+0.5+0.6+0.4+0.4+0.6+0.6+0.5+0.6)/10 = 0.56」与总体准确率巧合相等。若 batch 大小不齐（例如最后一个不满批），两者就会分道扬镳——这正是要坚持分子/分母累计的原因。

**版本兼容提示**：notebook 成稿较早，当时 `Accuracy()` 无参可用（默认按二分类处理 0/1 标签）。较新版本的 torchmetrics（1.x 起）要求显式指定任务参数，如 `Accuracy(task='binary')` 或 `Accuracy(task='multiclass', num_classes=10)`，否则构造时会报错。安装新版后请按此调整（待本地验证你环境中的具体行为）。

#### 4.3.4 代码实践

**实践目标**：在同一个模型、同一个测试集上，分别用 torchmetrics 的 `update()` 路线与 `forward()` 路线复现 `compute_accuracy` 的结果，验证两种调用语义殊途同归。

**操作步骤**：

1. 安装 torchmetrics（示例命令：`pip install torchmetrics`）。
2. 训练好任一 MNIST/CIFAR 模型后，运行下面的示例代码：

```python
# 示例代码：torchmetrics 两种调用方式对账（4.3 节实践）
import torch
from torchmetrics.classification import Accuracy
from helper_evaluate import compute_accuracy

# 新版 torchmetrics 需要显式 task 参数；旧版可写作 Accuracy()
metric = Accuracy(task='multiclass', num_classes=10)

# 路线一：update() —— 只累计
metric.reset()
with torch.no_grad():
    for features, targets in test_loader:
        logits = model(features.to(device))
        preds = torch.argmax(logits, dim=1)
        metric.update(preds.cpu(), targets.cpu())
acc_update = metric.compute()

# 路线二：forward() —— 累计 + batch 值
metric.reset()
batch_accs = []
with torch.no_grad():
    for features, targets in test_loader:
        logits = model(features.to(device))
        preds = torch.argmax(logits, dim=1)
        batch_acc = metric(preds.cpu(), targets.cpu())   # __call__ 即 forward
        batch_accs.append(batch_acc)
acc_forward = metric.compute()

helper_acc = compute_accuracy(model, test_loader, device)

print(f'update()  路线 compute(): {acc_update.item()*100:.2f}%')
print(f'forward() 路线 compute(): {acc_forward.item()*100:.2f}%')
print(f'helper compute_accuracy : {helper_acc.item():.2f}%')
print(f'第一个 batch 的准确率    : {batch_accs[0].item()*100:.2f}%')
```

3. 注意细节：torchmetrics 的 `Accuracy` 返回 0-1 的小数，而 `compute_accuracy` 乘了 100（[pytorch_ipynb/helper_evaluate.py:L22](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_evaluate.py#L22)），对比时先统一口径；两次路线之间要 `metric.reset()`，否则第二趟会在第一趟的累计上继续累加。

**需要观察的现象**：两个 `compute()` 值完全相等，且乘 100 后与 `compute_accuracy` 一致；`batch_accs` 里存的是每个 batch 的准确率（跳动值），而不是累计值。

**预期结果**：三条数字一一对应（例如都是 97.5% 上下，取决于你的模型）。

**待本地验证**：具体数值取决于你的模型与数据；若你的 torchmetrics 版本拒绝无参 `Accuracy()`，请按 4.3.3 的版本提示加 `task` 参数。

#### 4.3.5 小练习与答案

**练习 1**：为什么 notebook 里 `train_acc.update(...)` 的返回值打印出来全是 `None`，而 `train_acc(...)` 的返回值是数字？这暗示了两个方法各自的定位是什么？

**答案**：`update` 被设计成纯累计操作——把 (y_true, y_pred) 并入内部状态（正确数、样本数），没有返回值，所以接住它的变量是 `None`。`forward`（即 `__call__`）是更上层的复合操作：内部先更新全局状态，再对当前 batch 单独算一份指标返回，兼顾「累计」与「即时报告」。定位上，`update` 是底层原语，`forward` 是带日志能力的封装。

**练习 2**：官方文档说 `forward` 内部会把 `update` 调用两次。为什么这不会导致样本被重复计数、最终 `compute()` 被污染？

**答案**：因为六步流程里有「缓存全局状态 → `reset()` 清空 → 对当前 batch 单独 `update` → `compute()` → **恢复全局状态**」的保护：第二次 `update` 作用在清空后的局部状态上，算完 batch 值后就把之前的全局累计原样恢复。全局状态被准确写入一次、读出一次，`compute()` 因此不受影响。

**练习 3**：你有 3 个 batch，大小分别为 100、100、40，前两个 batch 各对 50 个，第三个对 4 个。比较「分子/分母累计」与「batch 准确率平均」两种算法的结果，说明哪种是正确姿势。

**答案**：分子/分母累计：(50+50+4)/(100+100+40) = 104/240 ≈ 0.4333，每 个样本权重相同。batch 均值：(0.5+0.5+0.1)/3 ≈ 0.3667，只 有 40 个样本的第三批与有 100 个样本的前两批拿到了同等投票权，小 batch 的低准确率被等权放大。正确姿势是前者——torchmetrics 的内部状态按分子/分母累计，所以 `.compute()` 天然给出 0.4333；这也解释了为什么不要手工收集各 batch 准确率再求平均。

## 5. 综合实践

把本讲三个模块串成一条完整的「模型诊断流水线」。以 u2-l3 的 MNIST MLP（或你手头任何已训练的 MNIST/CIFAR 模型）为对象，完成以下四步：

1. **训练并重载最优权重**：用 u4-l4 的 `train_classifier_simple_v2` 训练（或复用已有模型），报告成绩前重载验证集最优 checkpoint。
2. **混淆矩阵**：`compute_confusion_matrix` + `plot_confusion_matrix(show_normed=True)`，找出最大的非对角线格子 (i, j)，并用 `np.trace(mat)/mat.sum()` 与 `compute_accuracy` 对账。
3. **失败案例**：运行 4.2.4 的 `show_misclassified` 展示 16 个误分类样本，肉眼检查其中有多少恰好是第 2 步发现的 (i → j) 混淆对；再对比原生 `show_examples` 的对错混排视角。
4. **torchmetrics 交叉验证**：按 4.3.4 分别用 `update()` 与 `forward()` 两条路线算总体准确率，确认三条途径（helper、update、forward）数值一致；顺手用循环内 `compute()` 打印运行均值，观察它从第一个 batch 的值单调逼近最终值的过程。

最后写三五行「诊断结论」：模型最薄弱的类别是什么、失败样本有什么共性（笔画粘连？偏斜？）、下一步你打算用什么手段改进（数据增强？针对混淆对加样本？）。这份结论才是评估的真正产出——指标只是手段，行动建议才是目的。

## 6. 本讲小结

- **混淆矩阵是准确率的无损上游**：`compute_confusion_matrix` 收集全部 (真实, 预测) 对，用 `itertools.product` + `z.count` 暴力统计成 K×K 矩阵（行=真实、列=预测），对角线和除以总数即准确率；`np.unique` 取并集决定维度，并有单类别退化兜底。
- **`plot_confusion_matrix` 按行归一化**：分母是每个真实类别的样本数，归一化对角线即各类召回率；格子里可同时显示计数与比例，文字颜色随底色深浅自动切换保证可读。
- **`show_examples` 只画第一个 batch 的前 15 张、不筛选对错**：靠 `P:/T:` 标题肉眼找错；专门的失败案例分析需要自己加 `preds != targets` 掩码扫描整个测试集。
- **画图前必须反标准化并转置通道**：`UnNormalize` 乘 std 加 mean 还原 [0,1]，NCHW 转 NHWC 才能喂给 matplotlib 的 `imshow`。
- **torchmetrics 两种喂法的差异**：`update()` 只累计返回 `None`；`forward()`/`__call__` 额外返回当前 batch 的指标值，内部经「缓存-清空-局算-恢复」六步保证全局状态不被污染；两者 `.compute()` 结果恒等。
- **指标累计的正确姿势是分子/分母分别累计**：等大 batch 时「批均值再平均」巧合等于总体值，batch 不等大时会产生偏差；循环内 `compute()` 得到的是运行均值而非 batch 值。

## 7. 下一步学习建议

- **下一讲（u7-l2）**：K 折交叉验证。单次 train/valid/test 划分的测试准确率本身有随机波动，本讲的评估结论有多可信？K 折把「一次评估」升级为「多次评估取均值与标准差」，是对本讲诊断方法的统计加强。
- **继续阅读源码**：`pytorch_ipynb/lightning/lightning-mlp.ipynb` 与 `pytorch-lightning_ipynb/mlp/mlp-basic.ipynb` 里 torchmetrics 与 Lightning 的日志体系深度集成（`self.accuracy(y, y_pred)` 直接在 `training_step` 中调用，用的正是本讲的 `forward` 语义），可提前体会生产代码中的标准写法。
- **延伸练习方向**：把 4.1 节的混淆矩阵对账扩展到宏平均 F1——numpy 手写「每类的 precision/recall 再平均」，与 torchmetrics 的 `F1Score(task='multiclass', num_classes=10, average='macro')` 对账，检验你是否真正掌握了行/列归一化的语义。
